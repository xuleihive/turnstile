from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from backend.api import app, runtime_service
from backend.http.model_platform import _bind_invocation_identity
from backend.http.session import require_authenticated_session
from backend.services.runtime_diagnostics import DIAGNOSTIC_PREFIX, RuntimeDiagnostics
from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.test_apim_target_client_factory import registered
from tests.backend.model_platform.test_gateway_targets import TARGET, deployment, target
from tests.platform.api.test_model_management_api import publication_api as publication_api
from turnstile_core.config import Settings
from turnstile_core.domain.application_access import application_id_for
from turnstile_core.domain.gateway_targets import GatewayDeployment
from turnstile_core.domain.runtime_models import (
    HealthStatus,
    RuntimeCheckPlan,
    RuntimeCheckSelection,
    RuntimeDiagnosticAccess,
    RuntimeHealth,
)
from turnstile_core.integrations.apim_target_client_factory import AzureApimProbeAccessChecker
from turnstile_core.integrations.gateway import GatewayInvocationError
from turnstile_core.persistence.in_memory import InMemoryRepository


def test_diagnostic_contract_requires_explicit_selection_and_fixed_request_limits() -> None:
    with pytest.raises(ValidationError):
        RuntimeCheckSelection.model_validate({"model_id": str(UUID(int=1))})
    selection = RuntimeCheckSelection(model_id=UUID(int=1), gateway_deployment_id=UUID(int=2))
    assert set(selection.model_dump()) == {"model_id", "gateway_deployment_id"}
    plan = RuntimeCheckPlan(runtime_id=UUID(int=3), options=[])
    assert plan.max_output_tokens == 64 and plan.request_limit == 1
    for override in ({"request_limit": 2}, {"max_output_tokens": 1024}):
        with pytest.raises(ValidationError):
            RuntimeCheckPlan.model_validate({**plan.model_dump(), **override})
    legacy = RuntimeHealth(
        runtime_id=UUID(int=3),
        status=HealthStatus.UNKNOWN,
        message="Legacy check",
        checked_at=datetime.now(UTC),
    )
    assert legacy.check_kind == "legacy" and legacy.configuration_status == "unverified"
    assert legacy.valid_until is None and legacy.outcome is None


def test_diagnostic_claim_cooldown_and_completion_require_current_owner() -> None:
    repository = InMemoryRepository()
    runtime = next(row for row in repository.runtimes if row["enabled"])
    runtime["last_checked_at"] = None
    now = datetime.now(UTC)
    before = now - timedelta(seconds=60)
    assert repository.claim_runtime_diagnostic(runtime["id"], "first", now, before)
    assert not repository.claim_runtime_diagnostic(runtime["id"], "second", now, before)
    assert not repository.finish_runtime_diagnostic(
        runtime["id"],
        "second",
        "available",
        "bad",
        now,
    )
    assert runtime["health_message"] == "first"
    assert repository.finish_runtime_diagnostic(runtime["id"], "first", "unknown", "observed", now)
    assert not repository.claim_runtime_diagnostic(runtime["id"], "second", now, before)


def test_unit_diagnostic_context_resolves_only_requested_selection() -> None:
    repository = InMemoryRepository()
    model = next(row for row in repository.models if row["enabled"])
    runtime = next(row for row in repository.runtimes if row["id"] == model["runtime_id"])
    context = repository.runtime_diagnostic_contexts(
        [
            (runtime["id"], model["id"], runtime["gateway_profile_id"], UUID(int=75)),
        ]
    )[0]
    assert context["model"]["id"] == model["id"]
    assert context["runtime"]["id"] == runtime["id"]
    assert context["deployment"] is None
    assert context["runtime"] is runtime
    assert context["model"] is model
    assert context["gateway_deployment_id"] == UUID(int=75)


def test_explicit_diagnostic_route_does_not_fall_back_to_another_gateway() -> None:
    repository = registered()
    model = next(row for row in repository.models if row["enabled"])
    runtime_id = model["runtime_id"]
    selected = repository.invocation_route(
        runtime_id,
        model["id"],
        gateway_profile_id=TARGET,
        gateway_deployment_id=deployment().id,
    )
    assert selected is not None and selected["gateway_deployment_id"] == deployment().id
    assert str(selected["gateway_base_url"]).rstrip("/") == "https://unit.azure-api.net"
    assert repository.invocation_route(runtime_id, model["id"], gateway_profile_id=TARGET) is None
    assert (
        repository.invocation_route(
            runtime_id,
            model["id"],
            gateway_profile_id=UUID(int=123),
            gateway_deployment_id=deployment().id,
        )
        is None
    )
    repository.gateway_deployments[deployment().id]["retired_at"] = datetime.now(UTC)
    assert (
        repository.invocation_route(
            runtime_id,
            model["id"],
            gateway_profile_id=TARGET,
            gateway_deployment_id=deployment().id,
        )
        is None
    )


@pytest.mark.parametrize("authorized", [False, True])
def test_diagnostic_access_preflight_is_read_only_and_scoped_to_system_subscription(
    authorized: bool,
) -> None:
    selected = target(probe_subscription_id="unit-probe")
    claims = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "oid": str(UUID(int=11)),
                    "tid": str(selected.tenant_id),
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    tokens = Mock()
    tokens.token.return_value = f"unit.{claims}.unit"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "actions": list(AzureApimProbeAccessChecker.REQUIRED_ACTIONS)
                        if authorized
                        else [],
                    }
                ]
            },
        )

    checker = AzureApimProbeAccessChecker(
        tokens, httpx.Client(transport=httpx.MockTransport(handler))
    )
    access = checker.check(selected)
    assert access.status == ("ready" if authorized else "missing_permissions")
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.path.endswith(
        "/subscriptions/unit-probe/providers/Microsoft.Authorization/permissions",
    )
    assert access.api_principal_id == UUID(int=11)
    assert not any("listSecrets" in str(request.url) for request in requests)


@pytest.mark.parametrize("kind,native_id", [("managed", "managed"), ("self_hosted", "unit-edge")])
@pytest.mark.parametrize("port,api_path,valid", [
    ("", None, True), (":8443", None, False), ("", "another-api", False),
])
def test_inventory_endpoint_trust_is_bound_to_port_and_api_path(
    kind: str, native_id: str, port: str, api_path: str | None, valid: bool,
) -> None:
    repository = registered()
    apim = target()
    hostname = f"{apim.service_name}.azure-api.net"
    selected = GatewayDeployment.model_validate({
        **{
            key: value for key, value in repository.gateway_deployments[deployment().id].items()
            if key in GatewayDeployment.model_fields
        },
        "kind": kind,
        "native_gateway_id": native_id,
        "hosting_mode": "azure_managed" if kind == "managed" else "other",
        "endpoint_url": f"https://{hostname}{port}/{api_path or apim.api_path.strip('/')}",
    })
    diagnostics = RuntimeDiagnostics(repository, Settings(
        apim_target_registry_enabled=True, self_hosted_gateway_enabled=True,
    ))
    inventory = {
        "associated_api_ids": [apim.api_id],
        "hostname_configurations": [{"hostname": hostname}],
    }
    with patch.object(repository, "get_gateway_deployment_inventory", return_value=inventory):
        if valid:
            diagnostics._validate_endpoint(apim, selected)
        else:
            with pytest.raises(ValueError):
                diagnostics._validate_endpoint(apim, selected)


def diagnostic_fixture() -> tuple[InMemoryRepository, RuntimeDiagnostics, Mock, Mock, UUID]:
    repository = registered()
    repository.apim_targets[TARGET]["drift_state"] = "in_sync"
    selected = target(probe_subscription_id="unit-probe")
    deployment_row = repository.gateway_deployments[deployment().id]
    deployment_row["endpoint_url"] = (
        f"https://{selected.service_name}.azure-api.net/{selected.api_path}"
    )
    model = next(row for row in repository.models if row["enabled"])
    runtime_id = model["runtime_id"]
    runtime = next(row for row in repository.runtimes if row["id"] == runtime_id)
    runtime["last_checked_at"] = None
    application_id = application_id_for(TARGET, "unit-probe")
    repository.gateway_applications.append(
        {
            "id": application_id,
            "gateway_profile_id": TARGET,
            "application_type": "system",
            "system_managed": True,
            "status": "active",
        }
    )
    repository.gateway_application_subscriptions.append(
        {
            "application_id": application_id,
            "gateway_profile_id": TARGET,
            "apim_subscription_id": "unit-probe",
            "state": "active",
            "scope_exists": True,
            "scope_type": "api",
            "scope_id": selected.api_id,
        }
    )
    access = RuntimeDiagnosticAccess(
        status="ready",
        scope=selected.arm_resource_id,
        tenant_id=selected.tenant_id,
        subscription_id=selected.subscription_id,
        resource_group=selected.resource_group,
        apim_name=selected.service_name,
        probe_subscription_id="unit-probe",
        required_actions=[],
        checked_at=datetime.now(UTC),
    )
    credentials, router = Mock(), Mock()
    credentials.resolve.return_value = SecretStr("unit-diagnostic-key")
    router.adapter.return_value.invoke.return_value = Mock(correlation_id="gateway-correlation")
    service = RuntimeDiagnostics(
        repository, Settings(), credentials, router, access_checker=lambda _: access
    )
    return repository, service, credentials, router, runtime_id


def test_diagnostic_dispatch_is_single_bounded_and_records_historical_configuration() -> None:
    repository, service, credentials, router, runtime_id = diagnostic_fixture()
    plan = service.plan(runtime_id)
    assert plan.options and plan.access is not None and plan.access.status == "ready"
    credentials.resolve.assert_not_called()
    selected = plan.options[0]
    result = service.check(
        runtime_id,
        RuntimeCheckSelection(
            model_id=selected.model_id,
            gateway_deployment_id=selected.gateway_deployment_id,
        ),
        "unit-owner",
    )
    assert result.outcome == "passed" and result.configuration_status == "current"
    router.adapter.return_value.invoke.assert_called_once()
    request, route = router.adapter.return_value.invoke.call_args.args
    assert (
        request.max_output_tokens == 64 and request.stream is False and request.temperature is None
    )
    assert request.metadata.request_source == "runtime-diagnostic"
    assert route["gateway_credential"] == "unit-diagnostic-key"
    assert "provider_credential_ciphertext" not in route
    runtime = next(row for row in repository.runtimes if row["id"] == runtime_id)
    assert "unit-diagnostic-key" not in runtime["health_message"]
    runtime["name"] = "A display-only rename"
    assert service.project_results([runtime])[runtime_id].configuration_status == "current"
    runtime["config"]["path"] = "/changed"
    historical = service.project_results([runtime])[runtime_id]
    assert historical.outcome == "passed" and historical.configuration_status == "changed"
    assert historical.valid_until is None
    assert runtime["health_message"].startswith(DIAGNOSTIC_PREFIX)


def test_diagnostic_permission_denial_has_no_credential_read_or_inference() -> None:
    repository, service, credentials, router, runtime_id = diagnostic_fixture()
    plan = service.plan(runtime_id)
    assert plan.access is not None
    blocked = plan.access.model_copy(update={"status": "missing_permissions"})
    with (patch.object(service, "_access_checker", return_value=blocked),
          pytest.raises(HTTPException) as error):
        service.check(
            runtime_id,
            RuntimeCheckSelection(
                model_id=plan.options[0].model_id,
                gateway_deployment_id=plan.options[0].gateway_deployment_id,
            ),
            "unit-owner",
        )
    assert error.value.status_code == 409
    credentials.resolve.assert_not_called()
    router.adapter.assert_not_called()
    assert all(
        not (row.get("health_message") or "").startswith(DIAGNOSTIC_PREFIX)
        for row in repository.runtimes
    )


def test_diagnostic_configuration_change_during_credential_read_prevents_dispatch() -> None:
    repository, service, credentials, router, runtime_id = diagnostic_fixture()
    selected = service.plan(runtime_id).options[0]

    def changed(_target: object) -> SecretStr:
        repository.gateway_deployments[selected.gateway_deployment_id]["retired_at"] = datetime.now(
            UTC
        )
        return SecretStr("unit-diagnostic-key")

    credentials.resolve.side_effect = changed
    result = service.check(
        runtime_id,
        RuntimeCheckSelection(
            model_id=selected.model_id,
            gateway_deployment_id=selected.gateway_deployment_id,
        ),
        "unit-owner",
    )
    assert result.outcome == "configuration_error"
    assert result.configuration_status == "changed"
    router.adapter.assert_not_called()


def test_diagnostic_result_remains_historical_without_time_expiry() -> None:
    repository, service, _credentials, _router, runtime_id = diagnostic_fixture()
    selected = service.plan(runtime_id).options[0]
    result = service.check(
        runtime_id,
        RuntimeCheckSelection(
            model_id=selected.model_id,
            gateway_deployment_id=selected.gateway_deployment_id,
        ),
        "unit-owner",
    )
    past = result.model_copy(
        update={
            "checked_at": datetime.now(UTC) - timedelta(days=30),
            "valid_until": datetime.now(UTC) - timedelta(days=29),
        }
    )
    runtime = next(row for row in repository.runtimes if row["id"] == runtime_id)
    runtime["health_message"] = DIAGNOSTIC_PREFIX + past.model_dump_json()
    registry = ModelRuntimeService(repository, Settings()).registry()
    projected = next(row for row in registry.runtimes if row.id == runtime_id).diagnostic
    assert registry.runtime_diagnostics_supported is True
    assert projected is not None and projected.outcome == "passed"
    assert projected.configuration_status == "current"
    assert projected.checked_at == past.checked_at


def test_diagnostic_owner_api_requires_selection_and_disables_caching(
    publication_api: InMemoryRepository,
) -> None:
    runtime_id = publication_api.runtimes[0]["id"]
    service = Mock()
    service.runtime_check_plan.return_value = RuntimeCheckPlan(runtime_id=runtime_id, options=[])
    service.check_runtime.return_value = RuntimeHealth(
        runtime_id=runtime_id,
        status=HealthStatus.UNKNOWN,
        message="Inconclusive",
        checked_at=datetime.now(UTC),
    )
    owner = app.dependency_overrides[require_authenticated_session]()
    with patch.dict(app.dependency_overrides, {runtime_service: lambda: service}):
        client = TestClient(app)
        path = f"/api/v1/model-management/runtimes/{runtime_id}/check"
        plan = client.get(path)
        assert plan.status_code == 200 and plan.headers["cache-control"] == "no-store"
        assert client.post(path, json={}).status_code == 422
        checked = client.post(
            path,
            json={
                "model_id": str(UUID(int=1)),
                "gateway_deployment_id": str(UUID(int=2)),
            },
        )
        assert checked.status_code == 200 and checked.headers["cache-control"] == "no-store"
        assert service.check_runtime.call_args.kwargs["actor"] == owner.email
        service.authorize.assert_not_called()
        with patch.dict(
            app.dependency_overrides,
            {
                require_authenticated_session: lambda: replace(owner, role="member"),
            },
        ):
            assert client.get(path).status_code == 403
            assert client.post(path, json={}).status_code == 403


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", "system-runtime-diagnostic"),
        ("request_source", "runtime-diagnostic"),
        ("user_id", "system-runtime-health-check"),
        ("request_source", "runtime-health-check"),
        ("user_id", "SYSTEM-RUNTIME-DIAGNOSTIC"),
        ("user_id", "System-Runtime-Health-Check"),
        ("request_source", "RUNTIME-DIAGNOSTIC"),
        ("request_source", "Runtime-Health-Check"),
    ],
)
def test_normal_invocation_cannot_spoof_diagnostic_identity(field: str, value: str) -> None:
    metadata = Mock(user_id="unit-user", request_source="unit-invoke")
    setattr(metadata, field, value)
    with pytest.raises(HTTPException) as error:
        _bind_invocation_identity(Mock(metadata=metadata), Mock(role="owner"), Mock(), Settings())
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    "status,code,outcome",
    [
        (403, "model_not_assigned", "blocked"),
        (429, "rate_limit_exceeded", "blocked"),
        (503, "budget_ledger_unavailable", "blocked"),
        (502, "untrusted-upstream-detail", "upstream_error"),
        (400, "untrusted-upstream-detail", "inconclusive"),
    ],
)
def test_diagnostic_classifies_failure_without_replay_or_raw_error_disclosure(
    status: int,
    code: str,
    outcome: str,
) -> None:
    repository, service, _credentials, router, runtime_id = diagnostic_fixture()
    selected = service.plan(runtime_id).options[0]
    response = httpx.Response(
        status,
        json={"error": {"code": code}},
        request=httpx.Request("POST", "https://unit.example.test"),
    )
    error = GatewayInvocationError(
        "untrusted-upstream-detail", headers={"x-correlation-id": "unit-correlation"}
    )
    error.__cause__ = httpx.HTTPStatusError(
        "untrusted-upstream-detail", request=response.request, response=response
    )
    router.adapter.return_value.invoke.side_effect = error
    selection = RuntimeCheckSelection(
        model_id=selected.model_id, gateway_deployment_id=selected.gateway_deployment_id
    )
    result = service.check(runtime_id, selection, "unit-owner")
    assert result.outcome == outcome and result.http_status == status
    assert result.correlation_id == "unit-correlation"
    assert "untrusted-upstream-detail" not in result.model_dump_json()
    router.adapter.return_value.invoke.assert_called_once()
    with pytest.raises(HTTPException) as cooldown:
        service.check(runtime_id, selection, "unit-owner")
    assert cooldown.value.status_code == 429
    router.adapter.return_value.invoke.assert_called_once()
    stored = next(row for row in repository.runtimes if row["id"] == runtime_id)
    assert "untrusted-upstream-detail" not in stored["health_message"]


def test_diagnostic_does_not_dispatch_after_deadline() -> None:
    _repository, service, _credentials, router, runtime_id = diagnostic_fixture()
    selected = service.plan(runtime_id).options[0]
    with patch("backend.services.runtime_diagnostics.time.monotonic", side_effect=[0, 46, 46]):
        result = service.check(
            runtime_id,
            RuntimeCheckSelection(
                model_id=selected.model_id,
                gateway_deployment_id=selected.gateway_deployment_id,
            ),
            "unit-owner",
        )
    assert result.outcome == "configuration_error"
    router.adapter.assert_not_called()


def test_diagnostic_transport_timeout_is_inconclusive_without_retry() -> None:
    _repository, service, _credentials, router, runtime_id = diagnostic_fixture()
    selected = service.plan(runtime_id).options[0]
    router.adapter.return_value.invoke.side_effect = httpx.ReadTimeout("untrusted-timeout-text")
    result = service.check(
        runtime_id,
        RuntimeCheckSelection(
            model_id=selected.model_id,
            gateway_deployment_id=selected.gateway_deployment_id,
        ),
        "unit-owner",
    )
    assert result.outcome == "inconclusive" and result.status is HealthStatus.UNKNOWN
    assert "untrusted-timeout-text" not in result.model_dump_json()
    router.adapter.return_value.invoke.assert_called_once()
