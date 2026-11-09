from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api import app
from backend.http.dependencies import get_repository
from backend.http.session import SessionIdentity, require_authenticated_session
from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.test_apim_target_client_factory import registered
from tests.backend.model_platform.test_gateway_targets import TARGET, deployment, target
from tests.backend.model_platform.test_model_runtime import _foundry_route, request
from turnstile_core.config import Settings
from turnstile_core.domain.gateway_targets import ApimTargetRouteBinding, GatewayDeploymentScope
from turnstile_core.domain.models import TokenUsageRecord, UsageRequestDetail
from turnstile_core.domain.runtime_models import (
    ChatMessage,
    InvocationMetadata,
    ModelInvocationRequest,
    PlatformInvocationAttempt,
    PlatformInvocationCompletion,
)
from turnstile_core.integrations.gateway import GatewayInvocationError, GatewayRouter
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy, UsageFilters


def invocation_attempt(request_id: str = "unit-attempt") -> PlatformInvocationAttempt:
    metadata = InvocationMetadata.model_validate(
        {
            field: "unit"
            for field in InvocationMetadata.model_fields
            if field not in {"usage_domain", "turn_index"}
        }
    )
    return PlatformInvocationAttempt(
        request_id=request_id,
        started_at=datetime.now(UTC),
        metadata=metadata,
        model_id=UUID(int=1),
        model_key="unit-model",
        model_name="Unit model",
        runtime_id=UUID(int=2),
        runtime_name="Unit runtime",
        provider_name="Unit provider",
    )


def test_platform_attempt_contains_identity_but_no_billable_usage_or_content() -> None:
    attempt = invocation_attempt()
    assert attempt.schema_version == 1
    assert not {"messages", "content", "usage", "input_tokens", "estimated_cost"} & set(
        attempt.model_dump(),
    )
    with pytest.raises(ValidationError):
        PlatformInvocationAttempt.model_validate({**attempt.model_dump(), "messages": ["private"]})


@pytest.mark.parametrize(
    "updates",
    [
        {"outcome": "failed"},
        {"status_code": 500},
        {"latency_ms": -1},
        {"failure_kind": "timeout"},
        {"gateway_correlation_id": ""},
    ],
)
def test_platform_completion_rejects_inconsistent_outcomes(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PlatformInvocationCompletion.model_validate(
            {
                "request_id": "unit-attempt",
                "completed_at": datetime.now(UTC),
                "outcome": "succeeded",
                "status_code": 200,
                "latency_ms": 1,
                **updates,
            }
        )


def test_platform_store_is_nonbilling_and_completion_cannot_be_changed() -> None:
    repository = InMemoryRepository()
    attempt = invocation_attempt()
    repository.begin_platform_invocation(attempt)
    with pytest.raises(ValueError, match="already been used"):
        repository.begin_platform_invocation(attempt)
    completion = PlatformInvocationCompletion(
        request_id=attempt.request_id,
        completed_at=datetime.now(UTC),
        outcome="failed",
        status_code=504,
        latency_ms=12,
        failure_kind="timeout",
    )
    repository.finish_platform_invocation(completion)
    repository.finish_platform_invocation(completion)
    with pytest.raises(ValueError, match="immutable"):
        repository.finish_platform_invocation(completion.model_copy(update={"status_code": 502}))
    with pytest.raises(ValueError, match="missing or immutable"):
        repository.finish_platform_invocation(
            completion.model_copy(
                update={"request_id": "missing"},
            )
        )
    attempt.metadata.user_id = "changed-after-start"
    assert repository.invocation_attempts[attempt.request_id].metadata.user_id == "unit"
    assert repository.usage_records == [] and repository.billable_requests == {}


def test_postgresql_completion_is_conditional_and_never_writes_usage() -> None:
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    context, connection = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    completion = PlatformInvocationCompletion(
        request_id="unit-attempt",
        completed_at=datetime.now(UTC),
        outcome="succeeded",
        status_code=200,
        latency_ms=1,
    )
    with patch.object(repository, "_connection", return_value=context):
        repository.begin_platform_invocation(invocation_attempt())
        repository.finish_platform_invocation(completion)
        connection.execute.return_value.fetchone.return_value = None
        with pytest.raises(ValueError, match="immutable"):
            repository.finish_platform_invocation(completion)
    statements = [call.args[0] for call in connection.execute.call_args_list]
    assert "completion IS NULL OR completion = %s" in statements[-1]
    assert all("token_usage" not in statement for statement in statements)


def formal_usage(attempt: PlatformInvocationAttempt) -> TokenUsageRecord:
    return TokenUsageRecord(
        id="unit-usage",
        request_id=attempt.request_id,
        correlation_id="unit-correlation",
        ts=attempt.started_at,
        team="unit",
        user="unit",
        user_id=attempt.metadata.user_id,
        agent="unit",
        workflow="unit",
        run_id="unit",
        turn_index=1,
        provider=attempt.provider_name,
        model=attempt.model_name,
        model_id=str(attempt.model_id),
        runtime=attempt.runtime_name,
        input_tokens=17,
        cached_tokens=0,
        output_tokens=3,
        et=20,
        et_coeff_m=1,
        latency_ms=5,
        status="ok",
        status_code=200,
        estimated_cost=0.001,
        input_price_per_million=10,
        output_price_per_million=20,
        estimated=False,
        ingest_source="eventhub",
    )


def test_usage_request_counts_include_platform_failures_without_rewriting_usage() -> None:
    repository = InMemoryRepository()
    attempt = invocation_attempt()
    repository.begin_platform_invocation(attempt)
    repository.finish_platform_invocation(PlatformInvocationCompletion(
        request_id=attempt.request_id, completed_at=datetime.now(UTC),
        outcome="failed", status_code=502, latency_ms=15, failure_kind="invalid_response",
        gateway_correlation_id="unit-correlation",
    ))
    usage = formal_usage(attempt)
    repository.write_token_usage(usage)
    for index in range(4):
        repository.write_token_usage(usage.model_copy(update={
            "id": f"denied-{index}", "request_id": f"denied-{index}",
            "correlation_id": f"denied-correlation-{index}", "status_code": 403,
            "input_tokens": 0, "output_tokens": 0, "et": 0, "estimated_cost": 0,
        }))

    counts = repository.usage_request_counts(
        attempt.started_at - timedelta(seconds=1), datetime.now(UTC), UsageFilters(),
    )

    assert counts == {
        "total_requests": 5, "success_requests": 0,
        "client_error_requests": 4, "server_error_requests": 1,
    }
    assert len(repository.usage_records) == 5
    assert repository.usage_records[0].status_code == 200
    assert repository.usage_records[0].estimated_cost == usage.estimated_cost
    owner = SessionIdentity(
        id="unit-owner", email="owner@example.test", name="Owner", role="owner",
        method="password", session_expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    parameters = {
        "from": (attempt.started_at - timedelta(seconds=1)).isoformat(),
        "to": datetime.now(UTC).isoformat(), "limit": "1",
    }
    with patch.dict(app.dependency_overrides, {
        get_repository: lambda: repository, require_authenticated_session: lambda: owner,
    }):
        client = TestClient(app)
        first_page = client.get("/api/v1/observability/requests", params=parameters)
        assert first_page.status_code == 200
        body = first_page.json()
        assert len(body["items"]) == 1 and body["page"]["next_cursor"] is not None
        assert body["counts"] == counts
        second_page = client.get("/api/v1/observability/requests", params={
            **parameters, "cursor": body["page"]["next_cursor"],
        })
        assert second_page.status_code == 200 and second_page.json()["counts"] == counts
        denied = client.get("/api/v1/observability/requests", params={
            **parameters, "status_code": "403",
        })
        assert denied.status_code == 200
        assert denied.json()["counts"] == {
            "total_requests": 4, "success_requests": 0,
            "client_error_requests": 4, "server_error_requests": 0,
        }


def test_postgresql_usage_request_counts_use_the_same_trace_scope_as_the_list() -> None:
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    context, connection = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    expected = {"total_requests": 5, "success_requests": 0,
                "client_error_requests": 4, "server_error_requests": 1}
    connection.execute.return_value.fetchone.return_value = expected
    start = datetime.now(UTC) - timedelta(hours=1)
    end = datetime.now(UTC)
    with patch.object(repository, "_connection", return_value=context):
        assert repository.usage_request_counts(
            start, end, UsageFilters(organization_id="unit-organization"),
        ) == expected

    statement, parameters = connection.execute.call_args.args
    assert "FROM invocation_request_trace usage" in statement
    assert "usage.platform_outcome = 'failed'" in statement
    assert repository._REQUEST_TRACE_ROW_FILTER_SQL in statement
    assert parameters == [start, end, "unit-organization"]


def test_trace_is_immediate_nonbilling_and_merges_only_unique_formal_correlation() -> None:
    repository = InMemoryRepository()
    attempt = invocation_attempt()
    repository.begin_platform_invocation(attempt)
    pending = UsageRequestDetail.model_validate(repository.get_usage_request(attempt.request_id))
    assert pending.trace_id == "platform:" + attempt.request_id
    assert pending.status_code is None and pending.correlation_id is None
    assert pending.platform_outcome == "dispatched" and not pending.usage_available
    assert not pending.cost_available and repository.usage_records == []
    repository.finish_platform_invocation(
        PlatformInvocationCompletion(
            request_id=attempt.request_id,
            completed_at=datetime.now(UTC),
            outcome="failed",
            status_code=504,
            latency_ms=15,
            failure_kind="timeout",
            gateway_correlation_id="unit-correlation",
            upstream_request_id="upstream-id",
        )
    )
    record = formal_usage(attempt)
    repository.write_token_usage(record)
    merged = UsageRequestDetail.model_validate(repository.get_usage_request(pending.trace_id))
    assert merged.trace_id == pending.trace_id and merged.usage_available
    assert merged.total_tokens == 20 and merged.estimated_cost == 0.001
    assert merged.status_code == 504 and merged.gateway_status_code == 200
    assert merged.upstream_request_id == "upstream-id"
    assert merged.error_message == "Platform invocation timeout"
    rows = repository.list_usage_requests(
        attempt.started_at - timedelta(seconds=1),
        datetime.now(UTC),
        UsageFilters(status_code=504),
        10,
    )
    assert len(rows) == 1
    assert len(repository.usage_records) == 1 and repository.usage_records[0].status_code == 200


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", "another-user"),
        ("model_id", "another-model"),
        ("runtime", "another-runtime"),
        ("correlation_id", "another-attempt"),
        ("usage_domain", "github_copilot"),
    ],
)
def test_unrelated_usage_cannot_complete_a_platform_trace(field: str, value: str) -> None:
    repository = InMemoryRepository()
    attempt = invocation_attempt()
    repository.begin_platform_invocation(attempt)
    repository.finish_platform_invocation(
        PlatformInvocationCompletion(
            request_id=attempt.request_id,
            completed_at=datetime.now(UTC),
            outcome="succeeded",
            status_code=200,
            latency_ms=1,
            gateway_correlation_id="unit-correlation",
        )
    )
    repository.usage_records.append(formal_usage(attempt).model_copy(update={field: value}))
    detail = repository.get_usage_request("platform:" + attempt.request_id)
    assert detail is not None
    assert detail["usage_available"] is (field == "runtime")
    assert detail["cost_available"] is (field == "runtime")


@pytest.mark.parametrize("evidence", ["missing_correlation", "ambiguous", "wrong_application"])
def test_platform_trace_does_not_guess_missing_or_ambiguous_attribution(evidence: str) -> None:
    repository = InMemoryRepository()
    attempt = invocation_attempt()
    if evidence == "wrong_application":
        attempt.application_id = UUID(int=33)
    repository.begin_platform_invocation(attempt)
    repository.finish_platform_invocation(
        PlatformInvocationCompletion(
            request_id=attempt.request_id,
            completed_at=datetime.now(UTC),
            outcome="succeeded",
            status_code=200,
            latency_ms=1,
            gateway_correlation_id=None
            if evidence == "missing_correlation"
            else "unit-correlation",
        )
    )
    repository.usage_records.append(formal_usage(attempt))
    if evidence == "ambiguous":
        repository.usage_records.append(formal_usage(attempt).model_copy(update={"id": "second"}))
    detail = repository.get_usage_request("platform:" + attempt.request_id)
    assert detail is not None and detail["usage_available"] is False


@pytest.mark.parametrize("estimated", [False, True])
def test_trace_distinguishes_measured_zero_from_unknown_usage(estimated: bool) -> None:
    repository = InMemoryRepository()
    record = formal_usage(invocation_attempt()).model_copy(
        update={
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost": 0,
            "estimated": estimated,
        }
    )
    repository.usage_records.append(record)
    detail = UsageRequestDetail.model_validate(repository.get_usage_request(record.correlation_id))
    assert detail.usage_available is not estimated
    assert detail.cost_available is not estimated


def test_postgresql_trace_queries_preserve_unique_trace_identity_and_filters() -> None:
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    context, connection = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    now = datetime.now(UTC)
    with patch.object(repository, "_connection", return_value=context):
        repository.list_usage_requests(now, now, UsageFilters(status_code=504), 20)
        repository.get_usage_request("platform:unit-request")
    listing, detail = connection.execute.call_args_list
    assert "FROM invocation_request_trace usage" in listing.args[0]
    assert listing.args[1][-2:] == [504, 20]
    assert "usage.usage_available AND" in listing.args[0]
    assert "usage.trace_id = %s" in detail.args[0]
    assert detail.args[1] == ("platform:unit-request",) * 6


@pytest.mark.parametrize("api_format", ["openai_chat", "anthropic_messages"])
@pytest.mark.parametrize("invalid", [False, True])
def test_gateway_preserves_real_correlation_separately_from_upstream_headers(
    api_format: str,
    invalid: bool,
) -> None:
    attempt = invocation_attempt()
    request = ModelInvocationRequest(
        metadata=attempt.metadata, messages=[ChatMessage(role="user", content="Unit test")]
    )
    route = {
        "model_id": attempt.model_id,
        "runtime_id": attempt.runtime_id,
        "model_key": attempt.model_key,
        "runtime_name": attempt.runtime_name,
        "runtime_kind": "foundry",
        "runtime_config": {"api_format": api_format},
        "gateway_implementation": "apim",
        "gateway_base_url": "https://unit.example.test",
    }
    payload = (
        {"choices": [{"message": {"content": "OK"}}]}
        if api_format == "openai_chat"
        else {
            "content": [{"type": "text", "text": "OK"}],
        }
    )
    calls = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        headers = {"request-id": "provider-only", "x-secret": "never-propagate"}
        return (
            httpx.Response(200, headers=headers, content=b"invalid-json")
            if invalid
            else (httpx.Response(200, headers=headers, json=payload))
        )

    router = GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler)))
    if invalid:
        with pytest.raises(GatewayInvocationError) as error:
            router.adapter(route).invoke(request, route)
        assert error.value.failure_kind == "invalid_response"
        assert error.value.headers == {"request-id": "provider-only"}
    else:
        result = router.adapter(route).invoke(request, route)
        assert result.correlation_id is None
        assert result.upstream_request_id == "provider-only"
    assert len(calls) == 1


@pytest.mark.parametrize("failed", [False, True])
def test_actual_invocation_records_before_dispatch_and_keeps_real_request_identity(
    failed: bool,
) -> None:
    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)

    def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.headers["x-request-id"] == "unit-platform-call"
        assert "unit-platform-call" in repository.invocation_attempts
        assert repository.usage_records == []
        headers = {"x-request-id": "upstream-request", "x-correlation-id": "real-correlation"}
        return httpx.Response(
            403 if failed else 200,
            headers=headers,
            json={
                "error": {"code": "model_not_assigned"},
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    service = ModelRuntimeService(
        repository, Settings(), GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler)))
    )
    invocation = request(model=model["model_key"], runtime=runtime["name"]).model_copy(
        update={"model_id": model["id"]},
    )
    if failed:
        with pytest.raises(HTTPException) as error:
            service.invoke(invocation, request_id="unit-platform-call")
        assert error.value.headers is not None
        assert error.value.headers["x-request-id"] == "unit-platform-call"
        assert error.value.headers["x-upstream-request-id"] == "upstream-request"
        assert error.value.headers["x-platform-trace-state"] == "recorded"
    else:
        result = service.invoke(invocation, request_id="unit-platform-call")
        assert result.correlation_id == "real-correlation"
        assert result.upstream_request_id == "upstream-request"
    completion = repository.invocation_completions["unit-platform-call"]
    assert completion.outcome == ("failed" if failed else "succeeded")
    assert completion.gateway_correlation_id == "real-correlation"
    assert repository.usage_records == []


def test_unavailable_invocation_recording_prevents_any_outbound_request() -> None:
    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    router = MagicMock()
    service = ModelRuntimeService(repository, Settings(), router)
    with (
        patch.object(repository, "begin_platform_invocation", side_effect=RuntimeError("offline")),
        pytest.raises(HTTPException) as error,
    ):
        service.invoke(
            request(model=model["model_key"], runtime=runtime["name"]).model_copy(
                update={"model_id": model["id"]},
            )
        )
    assert error.value.status_code == 503
    router.adapter.assert_not_called()


def test_explicit_selection_requires_model_and_both_gateway_identifiers() -> None:
    original = request().model_dump()
    for selection in (
        {"gateway_profile_id": TARGET},
        {"gateway_deployment_id": UUID(int=1)},
        {"gateway_profile_id": TARGET, "gateway_deployment_id": UUID(int=1)},
    ):
        with pytest.raises(ValidationError):
            ModelInvocationRequest.model_validate({**original, **selection})


@pytest.mark.parametrize("endpoint", [
    "http://gateway.test", "https://user:secret@gateway.test", "https://gateway.test:8443",
    "https://gateway.test?key=secret", "https://gateway.test#fragment", "",
])
def test_legacy_apim_route_must_be_https_before_credentials(endpoint: str) -> None:
    service = ModelRuntimeService(InMemoryRepository(), Settings())
    with pytest.raises(HTTPException) as error:
        service._validate_invocation_endpoint({
            "gateway_implementation": "apim", "gateway_base_url": endpoint,
        })
    assert error.value.status_code == 503


@pytest.mark.parametrize("explicit", [False, True])
def test_actual_invocation_uses_only_the_selected_serving_gateway(explicit: bool) -> None:
    repository = registered()
    model = next(row for row in repository.models if row["enabled"])
    runtime = next(row for row in repository.runtimes if row["id"] == model["runtime_id"])
    edge = deployment(
        id=UUID(int=86),
        native_gateway_id="unit-edge",
        kind="self_hosted",
        hosting_mode="docker",
        declared_state="present",
        reachability_state="reachable",
        last_arm_sync_at=datetime.now(UTC),
        last_probe_at=datetime.now(UTC),
        endpoint_url="https://edge.example.test/unit/llm",
    )
    repository.upsert_gateway_deployment(edge)
    repository.gateway_deployment_inventories[edge.id] = {"associated_api_ids": [target().api_id]}
    publication_id = UUID(int=87)
    repository.gateway_publications.append(
        {"id": publication_id, "gateway_profile_id": TARGET, "status": "active",
         "desired_spec": {"bindings": []}}
    )
    repository.effective_gateway_releases[TARGET] = publication_id
    repository.upsert_apim_target_route_binding(
        ApimTargetRouteBinding(
            id=UUID(int=88),
            logical_model_id=model["id"],
            runtime_id=runtime["id"],
            apim_target_id=TARGET,
            current_publication_id=publication_id,
            is_default_serving=True,
            deployment_scope=GatewayDeploymentScope.model_validate(
                {
                    "mode": "selected",
                    "deployment_ids": [str(edge.id)],
                }
            ),
        )
    )
    calls = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        assert incoming.url.host == "edge.example.test"
        return httpx.Response(
            200,
            headers={"x-correlation-id": "edge-correlation"},
            json={"choices": [{"message": {"content": "EDGE_OK"}}]},
        )

    assert edge.endpoint_url is not None
    settings = Settings(
        apim_target_registry_enabled=True,
        self_hosted_gateway_enabled=True,
        self_hosted_gateway_trusted_endpoints={edge.id: edge.endpoint_url},
    )
    service = ModelRuntimeService(
        repository, settings, GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler)))
    )
    selected = request(model=model["model_key"], runtime=runtime["name"]).model_copy(
        update={
            "model_id": model["id"],
            **(
                {"gateway_profile_id": TARGET, "gateway_deployment_id": edge.id} if explicit else {}
            ),
        }
    )
    result = service.invoke(selected)
    assert result.content == "EDGE_OK" and len(calls) == 1
    assert repository.invocation_attempts[result.request_id].gateway_deployment_id == edge.id
    repository.gateway_deployments[edge.id]["retired_at"] = datetime.now(UTC)
    with pytest.raises(HTTPException):
        service.invoke(selected)
    assert len(calls) == 1
