import ast
import inspect
import json
import textwrap
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import HttpUrl, SecretStr, ValidationError

from backend.api import app
from backend.http.service_dependencies import runtime_service
from backend.http.session import require_authenticated_session
from backend.services.budget_diagnostics import BudgetDiagnostics
from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.test_gateway_targets import TARGET, target
from tests.backend.model_platform.test_runtime_diagnostics import diagnostic_fixture
from tests.platform.api.test_model_management_api import publication_api as publication_api
from turnstile_core.config import Settings
from turnstile_core.domain.budget_delegation import (
    BudgetDelegationReadiness,
    BudgetDelegationTarget,
    BudgetExecutionVerification,
    BudgetExecutionVerificationRequest,
)
from turnstile_core.domain.ledger import LedgerAttempt, LedgerAttemptIdentity, LedgerSummary
from turnstile_core.domain.runtime_models import GatewayKind
from turnstile_core.integrations.atomic_ledger import AtomicTableLedger
from turnstile_core.integrations.gateway import (
    GatewayInvocationError,
    GatewayRouter,
    OpenAICompatibleGatewayAdapter,
)
from turnstile_core.persistence.in_memory import InMemoryRepository


@pytest.mark.parametrize(
    "extra",
    [
        {"messages": []},
        {"max_output_tokens": 1000},
        {"revision": "other"},
        {"gateway_url": "https://other.example.test"},
        {"person_id": "other"},
    ],
)
def test_budget_verification_request_cannot_override_server_owned_inputs(
    extra: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        BudgetExecutionVerificationRequest.model_validate(
            {
                "application_id": str(UUID(int=1)),
                "model_id": str(UUID(int=2)),
                **extra,
            }
        )


def test_unconfigured_budget_checks_stop_before_any_resource_access() -> None:
    store_factory, client_factory = Mock(), Mock()
    tokens = Mock()
    service = BudgetDiagnostics(
        ModelRuntimeService(InMemoryRepository(), Settings()),
        store_factory=store_factory,
        client_factory=client_factory,
        token_provider=tokens,
    )
    for execute in (False, True):
        with pytest.raises(HTTPException) as error:
            if execute:
                service.execute(
                    UUID(int=1), BudgetExecutionVerificationRequest(
                        application_id=UUID(int=2), model_id=UUID(int=3),
                    ), "owner", Mock(),
                )
            else:
                service.readiness(UUID(int=1), "owner")
        assert error.value.status_code == 409
    store_factory.assert_not_called()
    client_factory.assert_not_called()
    tokens.token.assert_not_called()


@pytest.mark.parametrize("governance_user_id", [None, "stable-directory-person-id"])
def test_budget_owner_endpoints_bind_session_identity_and_disable_caching(
    publication_api: InMemoryRepository, governance_user_id: str | None,
) -> None:
    owner = replace(
        app.dependency_overrides[require_authenticated_session](),
        governance_user_id=governance_user_id,
    )
    base = f"/api/v1/model-management/gateways/{TARGET}"
    service = MagicMock()
    with patch.dict(app.dependency_overrides, {
        runtime_service: lambda: service, require_authenticated_session: lambda: owner,
    }):
        service.check_budget_delegation.return_value = BudgetDelegationReadiness(
            gateway_profile_id=TARGET,
            native_gateway_id="managed",
            revision="candidate",
            checked_at=datetime.now(UTC),
            ready=False,
            checks=[],
        )
        service.check_budget_execution.return_value = BudgetExecutionVerification(
            gateway_profile_id=TARGET,
            native_gateway_id="managed",
            revision="candidate",
            checked_at=datetime.now(UTC),
            request_id="unit",
            actual_status=504,
            invocation=None,
            scopes=[],
            settled=False,
        )
        client = TestClient(app)
        result = client.post(base + "/budget-readiness", json={})
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        service.check_budget_delegation.assert_called_once_with(TARGET, owner.effective_user_id)
        assert client.post(base + "/budget-execution-verification", json={}).status_code == 422
        result = client.post(
            base + "/budget-execution-verification",
            json={
                "application_id": str(UUID(int=1)),
                "model_id": str(UUID(int=2)),
            },
        )
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert service.check_budget_execution.call_args.args[2] == owner.effective_user_id
        with patch.dict(
            app.dependency_overrides,
            {
                require_authenticated_session: lambda: replace(owner, role="member"),
            },
        ):
            assert client.post(base + "/budget-readiness", json={}).status_code == 403
            assert (
                client.post(
                    base + "/budget-execution-verification",
                    json={
                        "application_id": str(UUID(int=1)),
                        "model_id": str(UUID(int=2)),
                    },
                ).status_code
                == 403
            )


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    repository, *_ = diagnostic_fixture()
    apim = target()
    application_id, subscription_id, epoch = UUID(int=40), UUID(int=41), UUID(int=42)
    repository.gateway_applications.append(
        {
            "id": application_id,
            "gateway_profile_id": TARGET,
            "status": "active",
            "system_managed": False,
        }
    )
    repository.gateway_application_subscriptions.append(
        {
            "id": subscription_id,
            "application_id": application_id,
            "gateway_profile_id": TARGET,
            "apim_subscription_id": "unit-candidate",
            "state": "active",
            "scope_exists": True,
        }
    )
    model = next(item for item in repository.models if item["enabled"])
    repository.user_model_policies["unit-owner"] = {
        "user_id": "unit-owner",
        "model_ids": [model["id"]],
    }
    fence = {
        "protocol_version": 2,
        "gateway_profile_id": str(TARGET),
        "api_id": apim.api_id,
        "apim_service_id": apim.service_name,
        "mode": "active",
        "epoch": str(epoch),
        "gateways": {"managed": ["candidate"]},
        "validation_revision": "candidate",
        "validation_person_id": "unit-owner",
        "validation_application_id": str(application_id),
        "validation_subscription_id": "unit-candidate",
    }
    store = MagicMock(spec=AtomicTableLedger)
    store.__enter__.return_value = store
    store.read_entity.side_effect = lambda _partition, row: (
        {"Document": json.dumps(fence)} if row == "F" else None
    )
    store.read_summary.side_effect = lambda scope: LedgerSummary(
        scope=scope,
        epoch=epoch,
        token_limit=1000,
        enforce=True,
        updated_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        repository,
        "budget_execution_status",
        lambda _scope: {
            "state": "active",
            "epoch": epoch,
        },
        raising=False,
    )
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            if fence.get("transport_failure"):
                raise httpx.ReadTimeout("unit-sensitive-transport-detail", request=request)
            return httpx.Response(
                200,
                headers={"x-correlation-id": "unit-correlation"},
                json={
                    "choices": [{"message": {"content": "OK"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                },
            )
        if fence["mode"] == "paused":
            return httpx.Response(
                503, json={"error": {"code": fence.get("response_error", "budget_protocol_fenced")}}
            )
        code = (
            401
            if request.headers.get("x-turnstile-delegation-token") == "Bearer wrong"
            else (403 if "x-user-id" not in request.headers else 200)
        )
        return httpx.Response(code, json={"data": []})

    transport = httpx.MockTransport(respond)
    settings = Settings(
        apim_target_registry_enabled=True,
        budget_execution_enabled=True,
        budget_execution_target_ids=[TARGET],
        ledger_table_endpoint="https://ledger.example.test",
        budget_delegation_targets={
            TARGET: BudgetDelegationTarget(
                audience="api://unit-budget",
                allowed_origins=(HttpUrl(f"https://{apim.service_name}.azure-api.net"),),
            )
        },
    )
    runtime = ModelRuntimeService(
        repository, settings, GatewayRouter(httpx.Client(transport=transport))
    )
    monkeypatch.setattr(
        runtime,
        "_decrypt_route",
        lambda route: {
            **route,
            "gateway_auth_type": "api_key",
            "gateway_credential": "unit-key",
            "gateway_config": {"header_name": "Ocp-Apim-Subscription-Key"},
        },
    )
    tokens = Mock()
    tokens.token.side_effect = lambda audience: (
        "valid" if audience == "api://unit-budget" else "wrong"
    )
    diagnostics = BudgetDiagnostics(
        runtime,
        store_factory=lambda: store,
        client_factory=lambda: httpx.Client(transport=transport),
        token_provider=tokens,
    )
    write = BudgetExecutionVerificationRequest(application_id=application_id, model_id=model["id"])
    applications = Mock()
    applications.reveal_application_subscription_key.return_value = Mock(value="unit-candidate-key")
    return diagnostics, fence, requests, write, applications, repository, store


def test_readiness_checks_only_catalog_with_exact_identity_cases(
    configured: tuple[Any, ...],
) -> None:
    service, _fence, requests, *_ = configured
    result = service.readiness(TARGET, "unit-owner")
    assert result.ready and result.verification is None
    assert [check.actual_status for check in result.checks] == [200, 401, 403]
    assert len(requests) == 3
    assert all(
        request.method == "GET" and request.url.path.endswith(";rev=candidate/v1/models")
        for request in requests
    )
    assert not service.repository.invocation_attempts


@pytest.mark.parametrize("matching", [True, False])
def test_paused_readiness_requires_fence_error_evidence(
    configured: tuple[Any, ...], matching: bool
) -> None:
    service, fence, requests, *_ = configured
    fence["mode"] = "paused"
    fence["response_error"] = "budget_protocol_fenced" if matching else "unrelated"
    result = service.readiness(TARGET, "unit-owner")
    assert result.ready is matching
    assert len(requests) == 2
    assert bool(result.verification) is matching


@pytest.mark.parametrize(
    "change",
    [
        {"validation_person_id": "another-owner"},
        {"validation_revision": None},
        {"validation_application_id": str(UUID(int=99))},
    ],
)
def test_execution_checks_candidate_identity_before_credentials(
    configured: tuple[Any, ...],
    change: dict[str, object],
) -> None:
    service, fence, requests, write, applications, *_ = configured
    fence["mode"] = "paused"
    fence.update(change)
    with pytest.raises(HTTPException) as error:
        service.execute(TARGET, write, "unit-owner", applications)
    assert error.value.status_code == 409
    applications.reveal_application_subscription_key.assert_not_called()
    assert not requests


def test_execution_records_single_bounded_call_and_does_not_invent_settlement(
    configured: tuple[Any, ...],
) -> None:
    service, fence, requests, write, applications, repository, _store = configured
    fence["mode"] = "paused"
    result = service.execute(TARGET, write, "unit-owner", applications)
    assert result.actual_status == 200 and not result.settled
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["max_tokens"] == 32 and not payload.get("stream", False)
    assert "temperature" not in payload
    assert requests[0].headers["ocp-apim-subscription-key"] == "unit-candidate-key"
    assert requests[0].headers["x-turnstile-delegation-token"] == "Bearer valid"
    assert len(result.scopes) == 2 and all(item.attempt is None for item in result.scopes)
    assert repository.invocation_attempts[result.request_id].application_id is None
    assert not repository.usage_records


@pytest.mark.parametrize("mismatch", [None, "scope", "epoch", "model", "correlation", "tokens"])
def test_settlement_requires_matching_evidence_in_both_scopes(
    configured: tuple[Any, ...], mismatch: str | None,
) -> None:
    service, fence, requests, write, applications, repository, store = configured
    fence["mode"] = "paused"

    def read_evidence(partition: str, row: str) -> dict[str, str] | None:
        if row == "F":
            return {"Document": json.dumps(fence)}
        scope = next(
            call.args[0] for call in store.read_summary.call_args_list
            if call.args[0].partition == partition
        )
        moment = datetime.now(UTC)
        changed = mismatch if scope.scope_type == "application" else None
        identity = LedgerAttemptIdentity(
            scope=scope.model_copy(update={"scope_id": str(UUID(int=90))})
            if changed == "scope" else scope,
            gateway_profile_id=TARGET,
            native_gateway_id="managed",
            correlation_id="another-attempt" if changed == "correlation" else "unit-correlation",
            model_id=str(UUID(int=91) if changed == "model" else write.model_id),
            admitted_at=moment,
        )
        attempt = LedgerAttempt(
            identity=identity,
            epoch=UUID(int=92) if changed == "epoch" else UUID(fence["epoch"]),
            state="settled",
            reserved_tokens=32,
            actual_tokens=4 if changed == "tokens" else 3,
            evidence_revision=1,
            evidence_key="unit-settlement-evidence",
            evidence_rank=3,
            updated_at=moment,
        )
        return {"Document": attempt.model_dump_json()}

    store.read_entity.side_effect = read_evidence
    result = service.execute(TARGET, write, "unit-owner", applications)
    assert result.settled is (mismatch is None)
    assert result.scopes[0].attempt is not None
    if mismatch not in (None, "tokens"):
        assert result.scopes[1].attempt is None
    assert len(requests) == 1
    assert not repository.usage_records


def test_changed_fence_stops_dispatch_after_credential_resolution(
    configured: tuple[Any, ...],
) -> None:
    service, fence, requests, write, applications, repository, _store = configured
    fence["mode"] = "paused"

    def token(_audience: str) -> str:
        fence["epoch"] = str(UUID(int=93))
        return "valid"

    service.tokens.token.side_effect = token
    with pytest.raises(HTTPException, match="Candidate fence changed") as error:
        service.execute(TARGET, write, "unit-owner", applications)
    assert error.value.status_code == 409
    applications.reveal_application_subscription_key.assert_called_once()
    assert not requests
    assert not repository.invocation_attempts


def test_unknown_dispatch_is_not_replayed_or_reported_as_settlement(
    configured: tuple[Any, ...], caplog: pytest.LogCaptureFixture,
) -> None:
    service, fence, requests, write, applications, repository, _store = configured
    fence.update(mode="paused", transport_failure=True)
    result = service.execute(TARGET, write, "unit-owner", applications)
    assert result.actual_status == 504
    assert result.invocation is None and not result.settled
    assert all(item.attempt is None for item in result.scopes)
    assert len(requests) == 1 and len(repository.invocation_attempts) == 1
    assert not repository.usage_records
    assert "unit-sensitive-transport-detail" not in caplog.text


@pytest.mark.parametrize(
    "implementation,delegation",
    [
        (GatewayKind.DIRECT, SecretStr("unit")),
        (GatewayKind.APIM, "unit"),
    ],
)
def test_delegation_never_reaches_untrusted_transports(
    configured: tuple[Any, ...],
    implementation: GatewayKind,
    delegation: object,
) -> None:
    service, *_ = configured
    route = service._route(TARGET)
    request = service._request(route, "unit-owner", "unit-request")
    route["budget_delegation_token"] = delegation
    with pytest.raises(GatewayInvocationError, match="trusted APIM"):
        OpenAICompatibleGatewayAdapter(implementation)._headers(request, route)


def test_execution_verification_reuses_the_configured_inference_timeout() -> None:
    source = ast.parse(textwrap.dedent(inspect.getsource(
        ModelRuntimeService.check_budget_execution,
    )))
    invocations = [node for node in ast.walk(source) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute)
                   and node.func.attr == "_invoke_resolved"]
    assert len(invocations) == 1
    assert not any(value.arg == "timeout_ms" for value in invocations[0].keywords)
