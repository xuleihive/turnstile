from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from backend.api import app, control_plane_service
from backend.http.publication_auth import require_publication_owner
from backend.http.service_dependencies import apim_onboarding_service
from backend.http.session import SessionIdentity, require_authenticated_session
from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.control_plane_support import TEST_CIPHER
from tests.backend.model_platform.test_apim_onboarding import (
    onboarding_request,
    onboarding_service_fixture,
)
from tests.backend.model_platform.test_apim_target_inventory import (
    lifecycle_service,
    self_hosted_write,
)
from tests.backend.model_platform.test_gateway_targets import TARGET, deployment, target
from tests.backend.model_platform.test_multi_target_rollout import multi_request, rollout_fixture
from tests.platform.api.test_model_management_api import (
    client,
)
from tests.platform.api.test_model_management_api import (
    management_runtime_service as management_runtime_service,
)
from tests.platform.api.test_model_management_api import (
    publication_api as publication_api,
)
from turnstile_core.config import Settings
from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.control_plane import GatewayControlPlaneService


@pytest.mark.parametrize("registry_enabled", [False, True])
@pytest.mark.parametrize("self_hosted_enabled", [False, True])
def test_control_plane_wires_source_capabilities_and_existing_gateway_probe(
    registry_enabled: bool, self_hosted_enabled: bool,
) -> None:
    settings = Settings.model_construct(
        apim_target_registry_enabled=registry_enabled,
        self_hosted_gateway_enabled=self_hosted_enabled,
    )
    dependencies = "backend.http.service_dependencies"
    with (
        patch(f"{dependencies}.get_settings", return_value=settings),
        patch(f"{dependencies}.CredentialCipher.from_settings", return_value=TEST_CIPHER),
        patch(f"{dependencies}.price_catalog"),
        patch(f"{dependencies}.GatewayConnectionProbe") as probe,
        patch(f"{dependencies}.GatewayControlPlaneService") as service,
    ):
        control_plane_service(InMemoryRepository())
    options = service.call_args.kwargs
    assert options["target_registry_enabled"] is registry_enabled
    assert options["deployment_scoping_enabled"] is self_hosted_enabled
    assert options["gateway_connection_probe"] is probe.return_value
    probe.assert_called_once_with()


@pytest.mark.parametrize("enabled", [False, True])
def test_registry_declares_source_topology_and_self_hosted_capabilities(enabled: bool) -> None:
    service = ModelRuntimeService(InMemoryRepository(), Settings.model_construct(
        apim_target_registry_enabled=enabled, self_hosted_gateway_enabled=enabled,
    ))
    registry = service.registry()
    assert registry.apim_target_topology_available is enabled
    assert registry.self_hosted_gateway_enabled is enabled


def test_apim_removal_http_preserves_history_and_rejects_dependencies(
    publication_api: InMemoryRepository,
) -> None:
    publication_api.upsert_apim_target(target())
    path = f"/api/v1/model-management/apim-targets/{TARGET}"
    assert client.get(path + "/removal-preview").json()["removal_allowed"] is False
    assert client.delete(path).status_code == 409
    publication_api.models = []
    publication_api.runtimes = []
    assert client.delete(path).status_code == 200
    assert client.get(path).status_code == 404
    assert publication_api.get_apim_target(TARGET) is not None


def test_multi_target_http_create_is_idempotent_and_credential_safe(
    publication_api: InMemoryRepository,
) -> None:
    del publication_api
    repository, children = rollout_fixture()
    request = multi_request([child["gateway_profile_id"] for child in children])
    write = request.model_dump(mode="json")
    path = "/api/v1/model-management/rollouts"
    assert client.post(path, json=write).status_code == 503
    service = GatewayControlPlaneService(
        repository, TEST_CIPHER, multi_target_rollout_enabled=True,
    )
    with patch.dict(app.dependency_overrides, {control_plane_service: lambda: service}):
        assert client.get(path + f"/{TARGET}").status_code == 404
        response = client.post(path, json=write)
        assert response.status_code == 202, response.text
        accepted = response.json()
        assert accepted["rollout"]["status"] == "queued"
        assert len(accepted["publications"]) == 2
        assert "credential_ciphertext" not in response.text and "**********" not in response.text
        assert all("api_key" not in item for item in accepted["publications"])
        assert all("desired_spec" not in item for item in accepted["publications"])
        assert client.post(path, json=write).json()["rollout"]["id"] == accepted["rollout"]["id"]
        assert client.get(accepted["status_url"]).json() == accepted["rollout"]
        assert client.get(path).json()["items"] == [accepted["rollout"]]
        assert client.get(path, params={"limit": 101}).status_code == 422
        invalid = {"children": [write["children"][0], write["children"][0]]}
        assert client.post(path, json=invalid).status_code == 422
    assert len(repository.gateway_publications) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_multi_target_http_routes_preserve_publication_owner_dependency(
    publication_api: InMemoryRepository, status: int,
) -> None:
    del publication_api
    service = MagicMock()

    def denied() -> str:
        raise HTTPException(status_code=status, detail="Denied")

    with patch.dict(app.dependency_overrides, {
        require_publication_owner: denied, control_plane_service: lambda: service,
    }):
        path = "/api/v1/model-management/rollouts"
        assert client.get(path).status_code == status
        assert client.get(path + f"/{TARGET}").status_code == status
        assert client.post(path, json={"children": []}).status_code == status
    assert service.mock_calls == []


def test_self_hosted_http_lifecycle_is_metadata_only_and_name_acknowledgement_is_minimal(
    publication_api: InMemoryRepository,
) -> None:
    del publication_api
    repository, service = lifecycle_service()
    add_path = f"/api/v1/model-management/apim-targets/{TARGET}/self-hosted-gateways"
    values = self_hosted_write().model_dump(mode="json")
    assert client.post(add_path, json=values).status_code == 404
    with patch.dict(app.dependency_overrides, {control_plane_service: lambda: service}):
        added = client.post(add_path, json=values)
        assert added.status_code == 201
        identity = added.json()["deployment"]["id"]
        path = f"/api/v1/model-management/self-hosted-gateways/{identity}"
        assert client.post(add_path, json=values).status_code == 409
        renamed = client.patch(path + "/name", json={"display_name": "  Confirmed name  "})
        assert renamed.status_code == 204 and not renamed.content
        assert client.patch(path + "/name", json={"display_name": " "}).status_code == 422
        assert client.patch(path + "/name", json={
            "display_name": "No routing changes", "endpoint_url": "https://other.example.test",
        }).status_code == 422
        preview = client.get(path + "/removal-preview")
        assert preview.status_code == 200 and preview.json()["removal_allowed"]
        assert preview.json()["display_name"] == "Confirmed name"
        assert client.put(path + "/apim-association", json={
            **values, "apim_target_id": str(TARGET),
        }).status_code == 409
        assert client.delete(path).status_code == 200
        assert client.get(path + "/removal-preview").status_code == 404
        assert client.post(add_path, json=values).json()["deployment"]["id"] == identity
    assert len(repository.gateway_deployments) == 1


@pytest.mark.usefixtures("management_runtime_service")
def test_self_hosted_http_validation_uses_owner_session_without_extra_credential(
    publication_api: InMemoryRepository,
) -> None:
    del publication_api
    _, service = lifecycle_service()
    expected = service.add_self_hosted_gateway(TARGET, self_hosted_write())
    path = f"/api/v1/model-management/self-hosted-gateways/{TARGET}/validations"
    with (
        patch.dict(app.dependency_overrides, {control_plane_service: lambda: service}),
        patch.object(service, "validate_self_hosted_gateway", return_value=expected) as validate,
    ):
        assert client.post(path).status_code == 200
        validate.assert_called_once_with(TARGET)


@pytest.mark.parametrize("access", ["anonymous", "member", "foreign-origin"])
def test_gateway_lifecycle_http_methods_preserve_owner_and_origin_guards(
    publication_api: InMemoryRepository, access: str,
) -> None:
    del publication_api
    owner = app.dependency_overrides[require_authenticated_session]()
    service = MagicMock()

    def identity() -> SessionIdentity:
        if access == "anonymous":
            raise HTTPException(status_code=401, detail="Sign in required")
        return SessionIdentity(
            id=owner.id, email=owner.email, name=owner.name, method=owner.method,
            role="member" if access == "member" else "owner",
            session_expires_at=owner.session_expires_at,
        )

    path = f"/api/v1/model-management/self-hosted-gateways/{TARGET}"
    values = self_hosted_write().model_dump(mode="json")
    headers = {"Origin": "https://foreign.example.test"} if access == "foreign-origin" else {}
    add_path = f"/api/v1/model-management/apim-targets/{TARGET}/self-hosted-gateways"
    apim_path = f"/api/v1/model-management/apim-targets/{TARGET}"
    with patch.dict(app.dependency_overrides, {
        control_plane_service: lambda: service, require_authenticated_session: identity,
    }):
        for method, url, body in (
            ("POST", add_path, values),
            ("PATCH", path + "/name", {"display_name": "Name"}),
            ("POST", path + "/validations", {}), ("DELETE", path, {}),
            ("PUT", path + "/apim-association", {**values, "apim_target_id": str(TARGET)}),
            ("DELETE", apim_path, {}),
        ):
            response = client.request(method, url, json=body, headers=headers)
            assert response.status_code == (401 if access == "anonymous" else 403)
        if access != "foreign-origin":
            assert client.get(path + "/removal-preview").status_code in {401, 403}
            assert client.get(apim_path + "/removal-preview").status_code in {401, 403}
    assert service.mock_calls == []


@pytest.mark.usefixtures("management_runtime_service")
def test_onboarding_http_preview_start_idempotency_and_linked_retry(
    publication_api: InMemoryRepository,
) -> None:
    del publication_api
    repository, state, service, _ = onboarding_service_fixture()
    path = "/api/v1/model-management/apim-onboarding"
    with patch.dict(app.dependency_overrides, {apim_onboarding_service: lambda: service}):
        assert client.get(path).json()["available"] is True
        preview = client.post(path + "/preview", json=state.plan.request.model_dump(mode="json"))
        assert preview.status_code == 200
        plan = preview.json()
        write = {"request": plan["request"], "confirmation_sha256": plan["confirmation_sha256"]}
        started = client.post(path, json=write)
        assert started.status_code == 202
        operation = started.json()["operation"]
        repeated = client.post(path, json=write)
        assert repeated.json()["operation"]["id"] == operation["id"]
        listed = client.get(path).json()["items"]
        assert len(listed) == 1 and listed[0]["id"] == operation["id"]
        assert "primary" not in started.text and "ciphertext" not in started.text
        changed = client.post(path, json={
            **write, "confirmation_sha256": "b" * 64,
        })
        assert changed.status_code == 409
        assert client.post(f"{path}/{operation['id']}/retry").status_code == 409
        row = repository.claim_gateway_release_operation("worker", 180)
        assert row is not None
        repository.transition_gateway_release_operation(row["id"], "queued", "failed", {}, "worker")
        retried = client.post(f"{path}/{operation['id']}/retry")
        assert retried.status_code == 202
        assert retried.json()["operation"]["checkpoint"]["retry_of"] == operation["id"]


def test_onboarding_preview_names_a_tenant_mismatch(publication_api: InMemoryRepository) -> None:
    # A wrong tenant ID used to surface as a generic 502 "could not validate" error.
    del publication_api
    _, state, service, _ = onboarding_service_fixture()
    request = state.plan.request.model_dump(mode="json")
    request["tenant_id"] = "00000000-0000-4000-8000-0000000000ff"
    with patch.dict(app.dependency_overrides, {apim_onboarding_service: lambda: service}):
        response = client.post("/api/v1/model-management/apim-onboarding/preview", json=request)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "The tenant ID does not match the tenant of the Turnstile management identity"
    }
    assert state.requests == []


@pytest.mark.usefixtures("management_runtime_service")
@pytest.mark.parametrize("failure", [
    PolicyCompilationError("unit-secret"), httpx.ConnectError("unit-secret"),
])
@pytest.mark.parametrize("action", ["preview", "start", "retry"])
def test_onboarding_http_dependency_failures_are_safe(
    publication_api: InMemoryRepository, failure: Exception, action: str,
) -> None:
    del publication_api
    service = MagicMock()
    getattr(service, action).side_effect = failure
    path = "/api/v1/model-management/apim-onboarding"
    body = onboarding_request().model_dump(mode="json")
    if action == "preview":
        path += "/preview"
    elif action == "retry":
        path += f"/{TARGET}/retry"
        body = {}
    else:
        body = {"request": body, "confirmation_sha256": "a" * 64}
    with patch.dict(app.dependency_overrides, {apim_onboarding_service: lambda: service}):
        response = client.post(
            path, json=body, headers={"Authorization": "Bearer management-secret"},
        )
    assert response.status_code == 502 and "unit-secret" not in response.text


@pytest.mark.parametrize("access", ["anonymous", "member", "foreign-origin"])
def test_onboarding_operations_preserve_owner_session_and_origin_guards(
    publication_api: InMemoryRepository, access: str,
) -> None:
    del publication_api
    owner = app.dependency_overrides[require_authenticated_session]()
    service = MagicMock()

    def identity() -> SessionIdentity:
        if access == "anonymous":
            raise HTTPException(status_code=401, detail="Sign in required")
        return SessionIdentity(
            id=owner.id, email=owner.email, name=owner.name, method=owner.method,
            role="member" if access == "member" else "owner",
            session_expires_at=owner.session_expires_at,
        )

    headers = {"Origin": "https://foreign.example.test"} if access == "foreign-origin" else {}
    request = onboarding_request().model_dump(mode="json")
    path = "/api/v1/model-management/apim-onboarding"
    with patch.dict(app.dependency_overrides, {
        apim_onboarding_service: lambda: service, require_authenticated_session: identity,
    }):
        for url, body in (
            (path + "/preview", request),
            (path, {"request": request, "confirmation_sha256": "a" * 64}),
            (f"{path}/{TARGET}/retry", {}),
        ):
            response = client.post(url, json=body, headers=headers)
            assert response.status_code == (401 if access == "anonymous" else 403)
        if access != "foreign-origin":
            assert client.get(path).status_code == (401 if access == "anonymous" else 403)
    assert service.mock_calls == []


def test_target_inventory_sync_is_a_durable_owner_operation(
    publication_api: InMemoryRepository,
) -> None:
    path = f"/api/v1/model-management/apim-targets/{TARGET}/inventory-syncs"
    assert client.post(path).status_code == 503
    publication_api.upsert_apim_target(target())
    service = GatewayControlPlaneService(publication_api, target_inventory_sync_enabled=True)
    with patch.dict(app.dependency_overrides, {control_plane_service: lambda: service}):
        response = client.post(path)
        assert response.status_code == 202
        assert response.json()["operation"]["operation_kind"] == "target_inventory_sync"
        assert response.json()["operation"]["status"] == "queued"
        assert client.post(path).status_code == 409
    assert len(publication_api.gateway_release_operations) == 1


def payload() -> dict[str, str]:
    return {
        "gateway_profile_id": str(TARGET),
        "name": "Unit APIM",
        "tenant_id": str(target().tenant_id),
        "subscription_id": str(target().subscription_id),
        "resource_group": "unit-group",
        "service_name": "unit-service",
        "api_id": "unit-api",
        "management_endpoint": "https://management.azure.com" + target().arm_resource_id,
        "gateway_endpoint": "https://unit.azure-api.net/unit/llm",
    }


def test_target_api_returns_persisted_topology_and_not_found(
    publication_api: InMemoryRepository,
) -> None:
    assert client.get("/api/v1/model-management/apim-targets").json()["total"] == 0
    assert client.get(f"/api/v1/model-management/apim-targets/{TARGET}").status_code == 404
    publication_api.upsert_apim_target(target())
    publication_api.upsert_gateway_deployment(deployment())
    response = client.get("/api/v1/model-management/apim-targets")
    assert response.status_code == 200 and response.json()["total"] == 1
    assert response.json()["items"][0]["connection_state"] == "unknown"


@pytest.mark.usefixtures("management_runtime_service")
def test_target_registration_honors_feature_and_owner_session_gates(
    publication_api: InMemoryRepository,
) -> None:
    path = "/api/v1/model-management/apim-targets"
    with patch("backend.http.model_platform.get_settings", return_value=Settings.model_construct()):
        response = client.post(path, json=payload())
        assert response.status_code == 503
    publication_api.upsert_apim_target(target())
    expected = GatewayControlPlaneService(publication_api).apim_target_topology(TARGET)
    with (
        patch(
            "backend.http.model_platform.get_settings",
            return_value=Settings.model_construct(
                apim_target_registry_enabled=True,
            ),
        ),
        patch("backend.http.model_platform.register_existing_apim_target", return_value=expected),
    ):
        response = client.post(path, json=payload())
    assert response.status_code == 201
    assert response.json()["target"]["gateway_profile_id"] == str(TARGET)


@pytest.mark.usefixtures("management_runtime_service")
@pytest.mark.parametrize("access", ["anonymous", "member", "foreign-origin"])
def test_target_registration_preserves_session_owner_and_origin_guards(
    publication_api: InMemoryRepository,
    access: str,
) -> None:
    del publication_api
    owner = app.dependency_overrides[require_authenticated_session]()

    def identity() -> SessionIdentity:
        if access == "anonymous":
            raise HTTPException(status_code=401, detail="Sign in required")
        return SessionIdentity(
            id=owner.id,
            email=owner.email,
            name=owner.name,
            method=owner.method,
            role="member" if access == "member" else "owner",
            session_expires_at=owner.session_expires_at,
        )

    headers = {}
    if access == "foreign-origin":
        headers["Origin"] = "https://foreign.example.test"
    with (
        patch.dict(app.dependency_overrides, {require_authenticated_session: identity}),
        patch("backend.http.model_platform.register_existing_apim_target") as register,
    ):
        response = client.post(
            "/api/v1/model-management/apim-targets", json=payload(), headers=headers
        )
    assert response.status_code == (401 if access == "anonymous" else 403)
    register.assert_not_called()
