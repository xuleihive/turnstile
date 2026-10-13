from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from backend.api import (
    app,
    control_plane_service,
    runtime_service,
)
from backend.http.dependencies import get_repository
from backend.http.publication_auth import require_publication_owner
from backend.http.session import SessionIdentity, require_authenticated_session
from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.control_plane_support import (
    activate_publication,
    transition_publication,
)
from tests.backend.model_platform.test_image_publication import image_publication
from turnstile_core.config import Settings
from turnstile_core.domain.control_plane import GatewayPublication, GatewayPublicationCreate
from turnstile_core.integrations.apim_control_plane import ApimPolicyCompiler
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.control_plane import GatewayControlPlaneService

client = TestClient(app)


@pytest.fixture
def publication_api() -> Iterator[InMemoryRepository]:
    repository = InMemoryRepository()
    owner = SessionIdentity(
        id="owner-id",
        email="owner@contoso.com",
        name="Owner",
        role="owner",
        permissions=frozenset({"platform.manage"}),
        method="password",
        session_expires_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    service = GatewayControlPlaneService(
        repository,
        CredentialCipher(Fernet.generate_key()),
        apim_principal_id="39deeba0-9799-4806-96c4-2f65eb0f22d1",
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[control_plane_service] = lambda: service
    app.dependency_overrides[require_authenticated_session] = lambda: owner
    app.dependency_overrides[require_publication_owner] = lambda: "owner@contoso.com"
    yield repository
    app.dependency_overrides.pop(get_repository, None)
    app.dependency_overrides.pop(control_plane_service, None)
    app.dependency_overrides.pop(require_authenticated_session, None)
    app.dependency_overrides.pop(require_publication_owner, None)


@pytest.mark.parametrize("deployment_name", ["   ", "\t\n"])
def test_invalid_image_deployment_returns_conflict_without_publication(
    publication_api: InMemoryRepository, deployment_name: str
) -> None:
    service = GatewayControlPlaneService(
        publication_api, image_generation_enabled=True, apim_principal_id="unit-principal"
    )
    app.dependency_overrides[control_plane_service] = lambda: service
    payload = image_publication(publication_api).model_dump(mode="json")
    payload["model"]["deployment_name"] = deployment_name
    response = client.post("/api/v1/model-management/publications", json=payload)
    assert response.status_code == 409
    assert publication_api.gateway_publications == []
    assert publication_api.gateway_publication_outbox == []


@pytest.fixture
def management_runtime_service(
    publication_api: InMemoryRepository,
) -> Iterator[ModelRuntimeService]:
    service = ModelRuntimeService(
        publication_api,
        Settings(
            management_api_key=SecretStr("management-secret"),
            credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
            apim_principal_id="39deeba0-9799-4806-96c4-2f65eb0f22d1",
        ),
    )
    app.dependency_overrides[runtime_service] = lambda: service
    yield service
    app.dependency_overrides.pop(runtime_service, None)


def bedrock_publication(gateway_id: str) -> dict[str, object]:
    return {
        "gateway_profile_id": gateway_id,
        "provider": {
            "template": "amazon_bedrock",
        },
        "runtime": {
            "bedrock_runtime_url": "https://bedrock-runtime.ap-southeast-2.amazonaws.com",
            "api_key": "test-only-bedrock-api-key",
        },
        "model": {
            "model_key": "claude-sonnet-4-6-bedrock",
            "display_name": "Claude Sonnet 4.6 - Amazon Bedrock",
            "upstream_model_id": "au.anthropic.claude-sonnet-4-6",
        },
    }


def foundry_publication(gateway_id: str) -> dict[str, object]:
    return {
        "gateway_profile_id": gateway_id,
        "provider": {"template": "microsoft_foundry"},
        "runtime": {
            "foundry_project_endpoint": (
                "https://contoso-ai.services.ai.azure.com/api/projects/finops"
            ),
        },
        "model": {
            "deployment_name": "gpt-5-mini-deployment",
        },
    }


def gateway_id(repository: InMemoryRepository) -> str:
    return str(
        next(
            gateway["id"]
            for gateway in repository.gateways
            if gateway["implementation"] == "apim"
        )
    )


def foundry_provider_id(repository: InMemoryRepository) -> str:
    return str(
        next(
            provider["id"]
            for provider in repository.providers
            if provider["brand_key"] == "microsoft_foundry"
        )
    )


def test_databricks_connection_api_creates_metadata_without_model_publication(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    model_count = len(publication_api.models)
    response = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"template": "azure_databricks"},
            "databricks_workspace_url": "https://adb-unit.1.azuredatabricks.net",
            "auth_mode": "managed_identity",
        },
    )
    assert response.status_code == 200
    registry = response.json()
    assert registry["databricks_connections_supported"] is True
    runtime = next(row for row in registry["runtimes"] if row["config"].get("workspace_url"))
    assert runtime["config"]["authorization"]["role_name"] == "CAN_QUERY"
    assert len(publication_api.models) == model_count
    assert publication_api.gateway_publications == []


@pytest.mark.parametrize(("origin", "workspace", "expected_status"), (
    ("http://localhost:5173", "https://adb-unit.1.azuredatabricks.net", 404),
    ("https://other.example", "https://adb-unit.1.azuredatabricks.net", 403),
    ("http://localhost:5173", "https://adb-unit.1.azuredatabricks.net/path", 422),
))
def test_databricks_adoption_api_validates_target_and_origin(
    publication_api: InMemoryRepository, origin: str, workspace: str, expected_status: int,
) -> None:
    response = client.post(
        f"/api/v1/model-management/connections/{UUID(int=99)}/adopt",
        headers={"Origin": origin}, json={"workspace_url": workspace},
    )
    assert response.status_code == expected_status
    assert not publication_api.gateway_publications


@pytest.mark.parametrize("case", ("mixed-credentials", "secret-type", "secret-length"))
def test_oauth_validation_errors_never_return_credential_input(
    publication_api: InMemoryRepository, case: str,
) -> None:
    secret = "unit-validation-oauth-secret"
    payload = bedrock_publication(gateway_id(publication_api))
    runtime: dict[str, object] = {
        "existing_id": str(publication_api.runtimes[0]["id"]), "oauth_client_secret": secret,
    }
    if case == "mixed-credentials":
        runtime["api_key"] = secret
    elif case == "secret-type":
        runtime["oauth_client_secret"] = {"credential": secret}
    else:
        runtime["oauth_client_secret"] = secret * 200
    payload["runtime"] = runtime
    response = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"}, json=payload,
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert not publication_api.gateway_publications


def test_gateway_release_reads_are_available_to_members(
    publication_api: InMemoryRepository,
) -> None:
    queued = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json=bedrock_publication(gateway_id(publication_api)),
    )
    assert queued.status_code == 202
    release_id = queued.json()["publication"]["id"]
    member = SessionIdentity(
        id="member-id",
        email="member@contoso.com",
        name="Member",
        role="member",
        permissions=frozenset(),
        method="password",
        session_expires_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    app.dependency_overrides[require_authenticated_session] = lambda: member

    listing = client.get("/api/v1/model-management/releases")
    detail = client.get(f"/api/v1/model-management/releases/{release_id}")
    diff = client.get(f"/api/v1/model-management/releases/{release_id}/diff")
    integrity = client.get(
        f"/api/v1/model-management/releases/{release_id}/integrity"
    )

    assert listing.status_code == 200
    assert listing.json()["items"][0]["role"] == "in_progress"
    assert detail.status_code == 200
    assert detail.json()["id"] == release_id
    assert diff.status_code == 200
    assert diff.json()["release_id"] == release_id
    assert integrity.status_code == 200
    assert integrity.json()["dependencies"]["live_status"] == "not_checked"
    assert integrity.json()["rollback_eligible"] is False


def test_gateway_release_reads_require_authentication(
    publication_api: InMemoryRepository,
) -> None:
    del publication_api
    def reject_session() -> None:
        raise HTTPException(status_code=401, detail="Authentication required")

    app.dependency_overrides[require_authenticated_session] = reject_session

    response = client.get("/api/v1/model-management/releases")

    assert response.status_code == 401


def test_gateway_release_mutations_are_owner_only_and_audited(
    publication_api: InMemoryRepository,
) -> None:
    queued = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json=bedrock_publication(gateway_id(publication_api)),
    )
    assert queued.status_code == 202
    release_id = queued.json()["publication"]["id"]
    member = SessionIdentity(
        id="member-id",
        email="member@contoso.com",
        name="Member",
        role="member",
        permissions=frozenset(),
        method="password",
        session_expires_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    app.dependency_overrides[require_authenticated_session] = lambda: member

    assert client.put(
        f"/api/v1/model-management/releases/{release_id}/protection",
        headers={"Origin": "http://localhost:5173"},
        json={"pinned": True},
    ).status_code == 403
    assert client.post(
        f"/api/v1/model-management/releases/{release_id}/integrity-checks",
        headers={"Origin": "http://localhost:5173"},
    ).status_code == 403
    assert client.post(
        f"/api/v1/model-management/releases/{release_id}/rollback",
        headers={"Origin": "http://localhost:5173"},
        json={"confirmation_sha256": "a" * 64},
    ).status_code == 403
    assert client.post(
        f"/api/v1/model-management/gateways/{gateway_id(publication_api)}/gc-plans",
        headers={"Origin": "http://localhost:5173"},
    ).status_code == 403

    owner = SessionIdentity(
        id="owner-id",
        email="owner@contoso.com",
        name="Owner",
        role="owner",
        permissions=frozenset({"platform.manage"}),
        method="password",
        session_expires_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    app.dependency_overrides[require_authenticated_session] = lambda: owner
    protected = client.put(
        f"/api/v1/model-management/releases/{release_id}/protection",
        headers={"Origin": "http://localhost:5173"},
        json={"pinned": True},
    )
    assert protected.status_code == 200
    assert protected.json()["pinned"] is True
    detail = client.get(f"/api/v1/model-management/releases/{release_id}")
    assert detail.status_code == 200
    assert detail.json()["pinned"] is True
    assert detail.json()["protection_audit"][-1]["actor"] == "owner@contoso.com"

    accepted = client.post(
        f"/api/v1/model-management/releases/{release_id}/integrity-checks",
        headers={"Origin": "http://localhost:5173"},
    )
    assert accepted.status_code == 202
    operation_id = accepted.json()["operation"]["id"]
    app.dependency_overrides[require_authenticated_session] = lambda: member
    operation = client.get(
        f"/api/v1/model-management/release-operations/{operation_id}"
    )
    assert operation.status_code == 200
    assert operation.json()["status"] == "queued"

    app.dependency_overrides[require_authenticated_session] = lambda: owner
    removed = client.delete(
        f"/api/v1/model-management/releases/{release_id}/protection",
        headers={"Origin": "http://localhost:5173"},
    )
    assert removed.status_code == 204
    detail = client.get(f"/api/v1/model-management/releases/{release_id}")
    assert detail.json()["pinned"] is False
    assert detail.json()["protection_audit"][-1]["pinned"] is False


def test_release_operation_queueing_fails_fast_when_worker_is_disabled(
    publication_api: InMemoryRepository,
) -> None:
    cipher = CredentialCipher(Fernet.generate_key())
    enabled = GatewayControlPlaneService(publication_api, cipher)
    publication = enabled.publish(
        GatewayPublicationCreate.model_validate(
            bedrock_publication(gateway_id(publication_api))
        ),
        "owner@contoso.com",
    )
    disabled = GatewayControlPlaneService(
        publication_api,
        cipher,
        release_worker_enabled=False,
    )
    app.dependency_overrides[control_plane_service] = lambda: disabled

    release_list = client.get("/api/v1/model-management/releases?limit=100")
    response = client.post(
        f"/api/v1/model-management/releases/{publication.id}/integrity-checks",
        headers={"Origin": "http://localhost:5173"},
    )

    assert release_list.status_code == 200
    assert release_list.json()["operations_enabled"] is False
    assert response.status_code == 503
    assert "worker is not enabled" in response.json()["detail"]
    assert publication_api.gateway_release_operations == []


def test_managed_identity_connection_does_not_create_a_model(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    before_models = list(publication_api.models)

    response = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"existing_id": foundry_provider_id(publication_api)},
            "foundry_project_endpoint": (
                "https://connection-mi.services.ai.azure.com/api/projects/finops"
            ),
            "auth_mode": "managed_identity",
        },
    )

    assert response.status_code == 200
    assert publication_api.models == before_models
    runtime = next(
        item
        for item in response.json()["runtimes"]
        if item["name"] == "Microsoft Foundry connection-mi/finops via APIM"
    )
    assert runtime["config"]["project_endpoint"].endswith("/api/projects/finops")
    assert runtime["config"]["auth_strategy"] == "managed_identity"
    assert "inference_endpoint" not in runtime["config"]


def test_first_model_on_managed_identity_connection_uses_observer(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    connection = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"existing_id": foundry_provider_id(publication_api)},
            "foundry_project_endpoint": (
                "https://connection-mi-model.services.ai.azure.com/api/projects/finance"
            ),
            "auth_mode": "managed_identity",
        },
    ).json()
    runtime = next(
        item
        for item in connection["runtimes"]
        if item["name"]
        == "Microsoft Foundry connection-mi-model/finance via APIM"
    )

    queued = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"existing_id": foundry_provider_id(publication_api)},
            "runtime": {"existing_id": runtime["id"]},
            "model": {"deployment_name": "gpt-finance"},
        },
    )

    assert queued.status_code == 202
    publication = GatewayPublication.model_validate(
        publication_api.get_gateway_publication(
            UUID(queued.json()["publication"]["id"])
        )
    )
    compiled = ApimPolicyCompiler(
        usage_observer_url="https://observer.example.com",
        usage_observer_key_named_value="turnstile-envoy-adapter-key",
    ).compile(publication)
    assert compiled.backends[-1].url == (
        "https://observer.example.com/api/projects/finance"
    )
    assert dict(compiled.backends[-1].headers)["x-turnstile-upstream-host"] == (
        "connection-mi-model.services.ai.azure.com"
    )
    assert (
        '<authentication-managed-identity resource="https://ai.azure.com" '
        'output-token-variable-name="turnstileProviderAccessToken" '
        'ignore-error="false" />'
        in compiled.chat_completions_policy
    )
    assert 'context.Variables["turnstileProviderAccessToken"]' in (
        compiled.chat_completions_policy
    )


def test_connection_update_preserves_routing_identity(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    created = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"existing_id": foundry_provider_id(publication_api)},
            "foundry_project_endpoint": (
                "https://edit-connection.services.ai.azure.com/api/projects/finops"
            ),
            "auth_mode": "managed_identity",
        },
    ).json()
    runtime = next(
        item
        for item in created["runtimes"]
        if item["config"].get("project_endpoint", "").endswith("/projects/finops")
    )
    identity = {
        "provider_id": runtime["provider_id"],
        "gateway_profile_id": runtime["gateway_profile_id"],
        "runtime_kind": runtime["runtime_kind"],
        "config": runtime["config"],
    }

    response = client.put(
        f"/api/v1/model-management/connections/{runtime['id']}",
        headers={"Origin": "http://localhost:5173"},
        json={"name": "Foundry Finance via APIM", "enabled": True, "is_default": False},
    )

    assert response.status_code == 200
    updated = next(item for item in response.json()["runtimes"] if item["id"] == runtime["id"])
    assert updated["name"] == "Foundry Finance via APIM"
    assert {key: updated[key] for key in identity} == identity


def test_connection_update_rejects_routing_fields(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    runtime = publication_api.runtimes[0]

    response = client.put(
        f"/api/v1/model-management/connections/{runtime['id']}",
        headers={"Origin": "http://localhost:5173"},
        json={
            "name": runtime["name"],
            "enabled": runtime["enabled"],
            "is_default": runtime["is_default"],
            "config": {"backend_url": "https://unpublished.example"},
        },
    )

    assert response.status_code == 422


def test_connection_metadata_update_keeps_existing_default_model(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    runtime = next(item for item in publication_api.runtimes if item["is_default"])
    model_defaults = {
        str(item["id"]): item["is_default"] for item in publication_api.models
    }

    response = client.put(
        f"/api/v1/model-management/connections/{runtime['id']}",
        headers={"Origin": "http://localhost:5173"},
        json={
            "name": f"{runtime['name']} updated",
            "enabled": runtime["enabled"],
            "is_default": True,
        },
    )

    assert response.status_code == 200
    assert {
        str(item["id"]): item["is_default"] for item in publication_api.models
    } == model_defaults


def test_first_connection_creates_the_foundry_provider_from_template(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    foundry_provider_ids = {
        provider["id"]
        for provider in publication_api.providers
        if provider["brand_key"] == "microsoft_foundry"
    }
    publication_api.models[:] = [
        model
        for model in publication_api.models
        if model["provider_id"] not in foundry_provider_ids
    ]
    publication_api.runtimes[:] = [
        runtime
        for runtime in publication_api.runtimes
        if runtime["provider_id"] not in foundry_provider_ids
    ]
    publication_api.providers[:] = [
        provider
        for provider in publication_api.providers
        if provider["id"] not in foundry_provider_ids
    ]

    response = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"template": "microsoft_foundry"},
            "foundry_project_endpoint": (
                "https://first-foundry.services.ai.azure.com/api/projects/first"
            ),
            "auth_mode": "managed_identity",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    provider = next(
        item
        for item in payload["providers"]
        if item["brand_key"] == "microsoft_foundry"
    )
    runtime = next(
        item
        for item in payload["runtimes"]
        if item["config"].get("project_endpoint", "").endswith(
            "/api/projects/first"
        )
    )
    assert runtime["provider_id"] == provider["id"]


def test_connection_template_reuses_an_existing_provider(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    provider_id = foundry_provider_id(publication_api)
    provider_count = len(publication_api.providers)

    response = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"template": "microsoft_foundry"},
            "foundry_project_endpoint": (
                "https://reuse-foundry.services.ai.azure.com/api/projects/reuse"
            ),
            "auth_mode": "managed_identity",
        },
    )

    assert response.status_code == 200
    assert len(publication_api.providers) == provider_count
    runtime = next(
        item
        for item in response.json()["runtimes"]
        if item["config"].get("project_endpoint", "").endswith("/api/projects/reuse")
    )
    assert runtime["provider_id"] == provider_id


def test_same_project_name_across_foundry_accounts_has_distinct_connections(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    for account in ("tenant-a-resource", "tenant-b-resource"):
        response = client.post(
            "/api/v1/model-management/connections",
            headers={"Origin": "http://localhost:5173"},
            json={
                "gateway_profile_id": gateway_id(publication_api),
                "provider": {"existing_id": foundry_provider_id(publication_api)},
                "foundry_project_endpoint": (
                    f"https://{account}.services.ai.azure.com/api/projects/shared"
                ),
                "auth_mode": "managed_identity",
            },
        )
        assert response.status_code == 200

    names = {
        runtime["name"]
        for runtime in publication_api.runtimes
        if str((runtime.get("config") or {}).get("project_endpoint", "")).endswith(
            "/api/projects/shared"
        )
    }
    assert names == {
        "Microsoft Foundry tenant-a-resource/shared via APIM",
        "Microsoft Foundry tenant-b-resource/shared via APIM",
    }


def test_repository_rejects_a_duplicate_foundry_project_connection(
    publication_api: InMemoryRepository,
) -> None:
    provider_id = UUID(foundry_provider_id(publication_api))
    gateway_profile_id = UUID(gateway_id(publication_api))
    runtime_values = {
        "gateway_profile_id": gateway_profile_id,
        "name": "Duplicate Foundry Project via APIM",
        "runtime_kind": "foundry",
        "enabled": True,
        "is_default": False,
        "brand_key": "microsoft_foundry",
        "allowed_roles": ["owner", "admin", "member"],
        "config": {
            "project_endpoint": (
                "https://repository-duplicate.services.ai.azure.com/api/projects/shared"
            )
        },
    }
    publication_api.create_connection(provider_id, None, runtime_values)

    with pytest.raises(ValueError, match="This connection already exists"):
        publication_api.create_connection(
            provider_id,
            None,
            {
                **runtime_values,
                "name": "Duplicate Foundry Project Again via APIM",
                "config": {
                    "project_endpoint": (
                        "https://repository-duplicate.services.ai.azure.com/"
                        "api/projects/shared/"
                    )
                },
            },
        )


def test_api_key_connection_defers_the_one_time_key_until_first_model(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    response = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"existing_id": foundry_provider_id(publication_api)},
            "foundry_project_endpoint": (
                "https://connection-key.services.ai.azure.com/api/projects/external"
            ),
            "foundry_inference_endpoint": (
                "https://connection-key.openai.azure.com/openai/v1"
            ),
            "auth_mode": "api_key",
        },
    )

    assert response.status_code == 200
    runtime = next(
        item
        for item in response.json()["runtimes"]
        if item["name"] == "Microsoft Foundry connection-key/external via APIM"
    )
    assert runtime["config"]["auth_strategy"] == "named_value_api_key"
    assert runtime["config"]["credential_provisioned"] is False
    assert runtime["config"]["inference_endpoint"].endswith("/openai/v1")
    assert "credential_ciphertext" not in response.text


@pytest.mark.parametrize(
    "connection",
    [
        {
            "foundry_project_endpoint": (
                "https://field-matrix.services.ai.azure.com/api/projects/finops"
            ),
            "foundry_inference_endpoint": (
                "https://field-matrix.openai.azure.com/openai/v1"
            ),
            "auth_mode": "managed_identity",
        },
        {
            "foundry_project_endpoint": (
                "https://field-matrix.services.ai.azure.com/api/projects/finops"
            ),
            "auth_mode": "api_key",
        },
    ],
)
def test_connection_auth_mode_rejects_the_wrong_endpoint_shape(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
    connection: dict[str, object],
) -> None:
    del management_runtime_service
    response = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"existing_id": foundry_provider_id(publication_api)},
            **connection,
        },
    )

    assert response.status_code == 422


def test_first_model_on_api_key_connection_requires_and_consumes_one_time_key(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    connection = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"existing_id": foundry_provider_id(publication_api)},
            "foundry_project_endpoint": (
                "https://deferred-key.services.ai.azure.com/api/projects/external"
            ),
            "foundry_inference_endpoint": (
                "https://deferred-key.openai.azure.com/openai/v1"
            ),
            "auth_mode": "api_key",
        },
    ).json()
    runtime = next(
        item
        for item in connection["runtimes"]
        if item["name"] == "Microsoft Foundry deferred-key/external via APIM"
    )
    payload = {
        "gateway_profile_id": gateway_id(publication_api),
        "provider": {"existing_id": foundry_provider_id(publication_api)},
        "runtime": {"existing_id": runtime["id"]},
        "model": {"deployment_name": "gpt-5-external"},
    }

    missing = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json=payload,
    )
    payload["runtime"] = {
        "existing_id": runtime["id"],
        "api_key": "first-model-one-time-key",
    }
    queued = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json=payload,
    )

    assert missing.status_code == 409
    assert queued.status_code == 202
    assert "first-model-one-time-key" not in queued.text
    publication_id = queued.json()["publication"]["id"]
    assert publication_api.gateway_publication_secrets[UUID(publication_id)]
    publication = GatewayPublication.model_validate(
        publication_api.get_gateway_publication(UUID(publication_id))
    )
    compiled = ApimPolicyCompiler(
        usage_observer_url="https://observer.example.com",
        usage_observer_key_named_value="turnstile-envoy-adapter-key",
    ).compile(publication)
    assert compiled.backends[-1].url == "https://observer.example.com"
    assert dict(compiled.backends[-1].headers) == {
        "x-adapter-key": "{{turnstile-envoy-adapter-key}}",
        "x-turnstile-upstream-host": "deferred-key.openai.azure.com",
    }
    assert '<set-header name="api-key" exists-action="override">' in (
        compiled.chat_completions_policy
    )
    assert 'template="/openai/v1/chat/completions"' in compiled.chat_completions_policy
    activate(publication_api, publication_id)
    activated = next(
        item for item in publication_api.runtimes if str(item["id"]) == runtime["id"]
    )
    assert activated["config"]["credential_provisioned"] is True


def test_connection_with_models_cannot_be_deleted(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    provider_id = foundry_provider_id(publication_api)
    created = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"existing_id": provider_id},
            "foundry_project_endpoint": (
                "https://delete-guard.services.ai.azure.com/api/projects/guarded"
            ),
            "auth_mode": "managed_identity",
        },
    ).json()
    runtime = next(
        item for item in created["runtimes"]
        if item["name"] == "Microsoft Foundry delete-guard/guarded via APIM"
    )
    publication_api.create_registry_item(
        "model",
        {
            "provider_id": UUID(provider_id),
            "runtime_id": UUID(runtime["id"]),
            "model_key": "guarded-model",
            "display_name": "Guarded model",
            "family_key": "generic",
            "upstream_model_id": "guarded-model",
            "assignment_required": True,
            "enabled": True,
            "is_default": False,
            "capabilities": ["chat"],
            "context_window": None,
            "input_cost_per_million": None,
            "output_cost_per_million": None,
            "cached_cost_per_million": None,
            "cache_write_cost_per_million": None,
            "allowed_roles": ["owner", "admin", "member"],
        },
    )

    response = client.delete(
        f"/api/v1/model-management/connections/{runtime['id']}",
        headers={"Origin": "http://localhost:5173"},
    )

    assert response.status_code == 409
    assert "Remove every model" in response.json()["detail"]
    assert any(str(item["id"]) == runtime["id"] for item in publication_api.runtimes)


def test_zero_model_managed_connection_can_be_deleted(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    provider_id = foundry_provider_id(publication_api)
    created = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"existing_id": provider_id},
            "foundry_project_endpoint": (
                "https://delete-connection.services.ai.azure.com/api/projects/cleanup"
            ),
            "auth_mode": "managed_identity",
        },
    )
    runtime = next(
        item
        for item in created.json()["runtimes"]
        if item["name"] == "Microsoft Foundry delete-connection/cleanup via APIM"
    )

    deleted = client.delete(
        f"/api/v1/model-management/connections/{runtime['id']}",
        headers={"Origin": "http://localhost:5173"},
    )

    assert deleted.status_code == 200
    assert all(item["id"] != runtime["id"] for item in deleted.json()["runtimes"])
    assert any(item["id"] == provider_id for item in deleted.json()["providers"])


def test_unused_non_default_gateway_can_be_deleted(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del publication_api, management_runtime_service
    created = client.post(
        "/api/v1/model-management/gateways",
        headers={
            "Origin": "http://localhost:5173",
            "Authorization": "Bearer management-secret",
        },
        json={
            "name": "Unused APIM gateway",
            "implementation": "apim",
            "base_url": "https://unused.example.test/api",
            "auth_type": "none",
            "enabled": True,
            "is_default": False,
            "config": {},
        },
    )
    gateway = next(
        item for item in created.json()["gateways"] if item["name"] == "Unused APIM gateway"
    )

    deleted = client.delete(
        f"/api/v1/model-management/gateways/{gateway['id']}",
        headers={"Origin": "http://localhost:5173"},
    )

    assert deleted.status_code == 200
    assert all(item["id"] != gateway["id"] for item in deleted.json()["gateways"])


def test_gateway_with_runtime_cannot_be_deleted(publication_api: InMemoryRepository) -> None:
    gateway_id = next(
        item["id"] for item in publication_api.gateways if item["is_default"]
    )

    response = client.delete(
        f"/api/v1/model-management/gateways/{gateway_id}",
        headers={"Origin": "http://localhost:5173"},
    )

    assert response.status_code == 409
    assert "default gateway" in response.json()["detail"]


def test_recreated_api_key_connection_gets_a_new_named_value(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    payload = {
        "gateway_profile_id": gateway_id(publication_api),
        "provider": {"existing_id": foundry_provider_id(publication_api)},
        "foundry_project_endpoint": (
            "https://recreate-connection.services.ai.azure.com/api/projects/cleanup"
        ),
        "foundry_inference_endpoint": (
            "https://recreate-connection.openai.azure.com/openai/v1"
        ),
        "auth_mode": "api_key",
    }
    first = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json=payload,
    ).json()
    first_runtime = next(
        item
        for item in first["runtimes"]
        if item["name"] == "Microsoft Foundry recreate-connection/cleanup via APIM"
    )

    deleted = client.delete(
        f"/api/v1/model-management/connections/{first_runtime['id']}",
        headers={"Origin": "http://localhost:5173"},
    )
    assert deleted.status_code == 200

    second = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json=payload,
    ).json()
    second_runtime = next(
        item
        for item in second["runtimes"]
        if item["name"] == "Microsoft Foundry recreate-connection/cleanup via APIM"
    )

    assert (
        second_runtime["config"]["named_value_name"]
        != first_runtime["config"]["named_value_name"]
    )


def model_write_payload(model: dict[str, object]) -> dict[str, object]:
    fields = (
        "provider_id",
        "runtime_id",
        "model_key",
        "display_name",
        "family_key",
        "upstream_model_id",
        "assignment_required",
        "enabled",
        "is_default",
        "capabilities",
        "context_window",
        "input_cost_per_million",
        "output_cost_per_million",
        "cached_cost_per_million",
        "cache_write_cost_per_million",
        "allowed_roles",
    )
    return {
        field: str(model[field]) if isinstance(model[field], UUID) else model[field]
        for field in fields
    }


def queue_model(repository: InMemoryRepository) -> dict[str, object]:
    response = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json=bedrock_publication(gateway_id(repository)),
    )
    assert response.status_code == 202
    return cast(dict[str, object], response.json()["publication"])


def activate(repository: InMemoryRepository, publication_id: str) -> None:
    item_id = UUID(publication_id)
    current = "queued"
    for status in (
        "validating",
        "provisioning",
        "building_revision",
        "verifying",
        "promoting",
    ):
        assert transition_publication(repository, item_id, current, status, {}, "worker")
        current = status
    activate_publication(repository, item_id, "worker")


@pytest.mark.parametrize("affinity", (False, True))
def test_model_backend_pool_configuration_and_removal_are_publications(
    publication_api: InMemoryRepository,
    affinity: bool,
) -> None:
    assert client.get("/api/v1/model-management").json()[
        "backend_pool_session_affinity_supported"
    ] is True
    provider = next(
        item
        for item in publication_api.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    primary = next(
        item
        for item in publication_api.runtimes
        if item["provider_id"] == provider["id"]
    )
    primary_url = "https://pool-primary.services.ai.azure.com/api/projects/main"
    primary["config"].update(
        control_plane_managed=True,
        project_endpoint=primary_url,
        backend_url=primary_url,
        backend_path="/openai/v1/chat/completions",
        api_format="openai_chat",
        auth_strategy="managed_identity",
        managed_identity_resource="https://ai.azure.com",
        streaming_mode="native",
    )
    queued = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"existing_id": str(provider["id"])},
            "runtime": {"existing_id": str(primary["id"])},
            "model": {"deployment_name": "gpt-5.6-native-pool"},
        },
    )
    assert queued.status_code == 202
    publication_id = queued.json()["publication"]["id"]
    activate(publication_api, publication_id)
    model = next(
        item
        for item in publication_api.models
        if str(item.get("publication_id")) == publication_id
    )
    secondary_id = UUID("30000000-0000-4000-8000-000000000099")
    secondary_url = (
        "https://pool-secondary.services.ai.azure.com/api/projects/main"
    )
    publication_api.runtimes.append(
        {
            **primary,
            "id": secondary_id,
            "name": "Secondary Foundry pool runtime",
            "config": {
                **primary["config"],
                "project_endpoint": secondary_url,
                "backend_url": secondary_url,
                "auth_strategy": "named_value_api_key",
                "named_value_name": "secondary-foundry-key",
                "managed_identity_resource": None,
                "credential_provisioned": True,
            },
        }
    )
    publication_api.models.append(
        {
            **model,
            "id": UUID("40000000-0000-4000-8000-000000000099"),
            "runtime_id": secondary_id,
            "model_key": "gpt-5.6-native-pool-secondary",
        }
    )
    write: dict[str, object] = {
        "session_affinity": affinity,
        "members": [
            {"runtime_id": str(primary["id"]), "priority": 0, "weight": 3},
            {"runtime_id": str(secondary_id), "priority": 0, "weight": 1},
        ],
        "rate_limit": {
            "max_attempts_per_request": 2,
            "retry_interval_seconds": 1,
            "first_fast_retry": True,
            "circuit_breaker": {
                "failure_count": 1,
                "interval_seconds": 60,
                "trip_duration_seconds": 60,
                "accept_retry_after": True,
            },
        },
    }

    unsafe_rate_limit = dict(cast(dict[str, object], write["rate_limit"]))
    unsafe_rate_limit["max_attempts_per_request"] = 3
    unsafe_write = dict(write)
    unsafe_write["rate_limit"] = unsafe_rate_limit
    unsafe = client.put(
        f"/api/v1/model-management/models/{model['id']}/backend-pool",
        headers={"Origin": "http://localhost:5173"},
        json=unsafe_write,
    )
    assert unsafe.status_code == 422

    configured = client.put(
        f"/api/v1/model-management/models/{model['id']}/backend-pool",
        headers={"Origin": "http://localhost:5173"},
        json=write,
    )

    assert configured.status_code == 202
    configured_id = configured.json()["publication"]["id"]
    publication = GatewayPublication.model_validate(
        publication_api.get_gateway_publication(UUID(configured_id))
    )
    assert publication.publication_kind.value == "route_reconcile"
    binding = next(
        item
        for item in publication.desired_spec.bindings
        if item.model.model_key == model["model_key"]
    )
    pool = binding.runtime_config["apim_backend_pool"]
    assert isinstance(pool, dict)
    assert "session_affinity" not in pool
    if affinity:
        assert binding.runtime_config["apim_backend_pool_session_affinity"] is True
    else:
        assert "apim_backend_pool_session_affinity" not in binding.runtime_config
    assert [item["weight"] for item in pool["members"]] == [3, 1]
    assert [item["auth_strategy"] for item in pool["members"]] == [
        "managed_identity",
        "named_value_api_key",
    ]
    compiled = ApimPolicyCompiler().compile(publication)
    assert any(item.__class__.__name__ == "BackendPoolResource" for item in compiled.backends)
    assert any(
        dict(item.headers).get("api-key") == "{{secondary-foundry-key}}"
        for item in compiled.backends
    )
    activate(publication_api, configured_id)
    effective_pool = client.get(
        f"/api/v1/model-management/models/{model['id']}/backend-pool"
    )
    assert effective_pool.status_code == 200
    assert effective_pool.json()["session_affinity"] is affinity
    assert [item["runtime_id"] for item in effective_pool.json()["members"]] == [
        str(primary["id"]),
        str(secondary_id),
    ]
    assert effective_pool.json()["rate_limit"] == {
        "max_attempts_per_request": 2,
        "retry_interval_seconds": 1,
        "first_fast_retry": True,
        "backend_timeout_seconds": 120,
        "circuit_breaker": {
            "failure_count": 1,
            "interval_seconds": 60,
            "trip_duration_seconds": 60,
            "accept_retry_after": True,
            "status_code_ranges": [
                {"minimum": 429, "maximum": 429},
                {"minimum": 408, "maximum": 408},
                {"minimum": 500, "maximum": 599},
            ],
            "error_reasons": ["BackendConnectionFailure", "Timeout"],
        },
    }

    member = SessionIdentity(
        id="member-id",
        email="member@contoso.com",
        name="Member",
        role="member",
        permissions=frozenset(),
        method="password",
        session_expires_at=datetime(2026, 8, 17, tzinfo=UTC),
    )

    def deny_publication_write() -> str:
        raise HTTPException(status_code=403, detail="Owner role is required")

    app.dependency_overrides[require_authenticated_session] = lambda: member
    app.dependency_overrides[require_publication_owner] = deny_publication_write
    member_read = client.get(
        f"/api/v1/model-management/models/{model['id']}/backend-pool"
    )
    member_write = client.put(
        f"/api/v1/model-management/models/{model['id']}/backend-pool",
        headers={"Origin": "http://localhost:5173"},
        json=write,
    )
    member_remove = client.delete(
        f"/api/v1/model-management/models/{model['id']}/backend-pool",
        headers={"Origin": "http://localhost:5173"},
    )
    assert member_read.status_code == 200
    assert member_write.status_code == 403
    assert member_remove.status_code == 403
    app.dependency_overrides[require_publication_owner] = (
        lambda: "owner@contoso.com"
    )

    removed = client.delete(
        f"/api/v1/model-management/models/{model['id']}/backend-pool",
        headers={"Origin": "http://localhost:5173"},
    )

    assert removed.status_code == 202
    removal = GatewayPublication.model_validate(
        publication_api.get_gateway_publication(
            UUID(removed.json()["publication"]["id"])
        )
    )
    removed_binding = next(
        item
        for item in removal.desired_spec.bindings
        if item.model.model_key == model["model_key"]
    )
    assert "apim_backend_pool" not in removed_binding.runtime_config
    assert "apim_backend_pool_session_affinity" not in removed_binding.runtime_config


def test_model_backend_pool_rejects_incompatible_runtime_path(
    publication_api: InMemoryRepository,
) -> None:
    provider = next(
        item
        for item in publication_api.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    model = next(
        item
        for item in publication_api.models
        if item["provider_id"] == provider["id"] and item["enabled"]
    )
    primary = next(
        item for item in publication_api.runtimes if item["id"] == model["runtime_id"]
    )
    primary_url = "https://path-primary.services.ai.azure.com/api/projects/main"
    primary["config"].update(
        control_plane_managed=True,
        project_endpoint=primary_url,
        backend_url=primary_url,
        backend_path="/openai/v1/chat/completions",
        api_format="openai_chat",
        auth_strategy="managed_identity",
        managed_identity_resource="https://ai.azure.com",
        streaming_mode="native",
    )
    effective = GatewayControlPlaneService(
        publication_api,
        apim_principal_id="39deeba0-9799-4806-96c4-2f65eb0f22d1",
    ).publish(
        GatewayPublicationCreate.model_validate(
            {
                "gateway_profile_id": gateway_id(publication_api),
                "provider": {"existing_id": str(provider["id"])},
                "runtime": {"existing_id": str(primary["id"])},
                "model": {"deployment_name": "gpt-5.6-path-check"},
            }
        ),
        "owner@contoso.com",
    )
    activate(publication_api, str(effective.id))
    target_model = next(
        item
        for item in publication_api.models
        if item.get("publication_id") == effective.id
    )
    primary = next(
        item
        for item in publication_api.runtimes
        if item["id"] == target_model["runtime_id"]
    )
    secondary_id = UUID("30000000-0000-4000-8000-000000000098")
    publication_api.runtimes.append(
        {
            **primary,
            "id": secondary_id,
            "name": "Incompatible pool runtime",
            "config": {
                **primary["config"],
                "backend_url": "https://incompatible.services.ai.azure.com",
                "backend_path": "/different/path",
            },
        }
    )
    publication_api.models.append(
        {
            **target_model,
            "id": UUID("40000000-0000-4000-8000-000000000098"),
            "runtime_id": secondary_id,
            "model_key": "gpt-5.6-path-check-secondary",
        }
    )

    response = client.put(
        f"/api/v1/model-management/models/{target_model['id']}/backend-pool",
        headers={"Origin": "http://localhost:5173"},
        json={
            "members": [
                {"runtime_id": str(primary["id"]), "priority": 0, "weight": 1},
                {"runtime_id": str(secondary_id), "priority": 0, "weight": 1},
            ],
            "rate_limit": {
                "max_attempts_per_request": 2,
                "retry_interval_seconds": 1,
                "first_fast_retry": True,
                "circuit_breaker": {
                    "failure_count": 1,
                    "interval_seconds": 60,
                    "trip_duration_seconds": 60,
                    "accept_retry_after": True,
                },
            },
        },
    )

    assert response.status_code == 409
    assert "protocol, path, and streaming" in response.text


def test_model_backend_pool_rejects_runtime_without_equivalent_deployment(
    publication_api: InMemoryRepository,
) -> None:
    provider = next(
        item
        for item in publication_api.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    primary = next(
        item
        for item in publication_api.runtimes
        if item["provider_id"] == provider["id"]
    )
    primary_url = "https://pool-primary.services.ai.azure.com/api/projects/main"
    primary["config"].update(
        control_plane_managed=True,
        project_endpoint=primary_url,
        backend_url=primary_url,
        backend_path="/openai/v1/chat/completions",
        api_format="openai_chat",
        auth_strategy="managed_identity",
        managed_identity_resource="https://ai.azure.com",
        streaming_mode="native",
    )
    effective = GatewayControlPlaneService(
        publication_api,
        apim_principal_id="39deeba0-9799-4806-96c4-2f65eb0f22d1",
    ).publish(
        GatewayPublicationCreate.model_validate(
            {
                "gateway_profile_id": gateway_id(publication_api),
                "provider": {"existing_id": str(provider["id"])},
                "runtime": {"existing_id": str(primary["id"])},
                "model": {"deployment_name": "gpt-5.6-no-counterpart"},
            }
        ),
        "owner@contoso.com",
    )
    activate(publication_api, str(effective.id))
    target_model = next(
        item
        for item in publication_api.models
        if item.get("publication_id") == effective.id
    )
    secondary_id = UUID("30000000-0000-4000-8000-000000000097")
    publication_api.runtimes.append(
        {
            **primary,
            "id": secondary_id,
            "name": "Protocol-compatible runtime without deployment",
            "config": {
                **primary["config"],
                "backend_url": "https://empty-secondary.services.ai.azure.com",
            },
        }
    )

    response = client.put(
        f"/api/v1/model-management/models/{target_model['id']}/backend-pool",
        headers={"Origin": "http://localhost:5173"},
        json={
            "members": [
                {"runtime_id": str(primary["id"]), "priority": 0, "weight": 1},
                {"runtime_id": str(secondary_id), "priority": 1, "weight": 1},
            ],
            "rate_limit": {
                "max_attempts_per_request": 2,
                "retry_interval_seconds": 1,
                "first_fast_retry": True,
                "circuit_breaker": {
                    "failure_count": 1,
                    "interval_seconds": 60,
                    "trip_duration_seconds": 60,
                    "accept_retry_after": True,
                },
            },
        },
    )

    assert response.status_code == 409
    assert "enabled equivalent model deployment" in response.text


def test_publication_status_is_safe_and_does_not_materialize_draft(
    publication_api: InMemoryRepository,
) -> None:
    before = len(publication_api.models)
    publication = queue_model(publication_api)

    assert publication["status"] == "queued"
    assert publication["model_key"] == "claude-sonnet-4-6-bedrock"
    assert len(publication_api.models) == before
    assert not {
        "desired_spec",
        "resource_manifest",
        "policy_sha256",
        "apim_revision",
    }.intersection(publication)
    assert "test-only-bedrock-api-key" not in str(publication)


def test_route_reconcile_api_queues_same_model_set(
    publication_api: InMemoryRepository,
) -> None:
    initial = queue_model(publication_api)
    activate(publication_api, str(initial["id"]))
    model_keys_before = sorted(model["model_key"] for model in publication_api.models)

    response = client.post(
        f"/api/v1/model-management/gateways/{gateway_id(publication_api)}/reconcile",
        headers={"Origin": "http://localhost:5173"},
    )

    assert response.status_code == 202
    publication = response.json()["publication"]
    assert publication["publication_kind"] == "route_reconcile"
    assert publication["model_key"] == "all-models"
    assert sorted(model["model_key"] for model in publication_api.models) == model_keys_before


def test_foundry_authorization_handoff_is_safe_and_resumable(
    publication_api: InMemoryRepository,
) -> None:
    foundry_provider_ids = {
        item["id"]
        for item in publication_api.providers
        if item["brand_key"] == "microsoft_foundry"
    }
    publication_api.models = [
        item
        for item in publication_api.models
        if item["provider_id"] not in foundry_provider_ids
    ]
    publication_api.runtimes = [
        item
        for item in publication_api.runtimes
        if item["provider_id"] not in foundry_provider_ids
    ]
    publication_api.providers = [
        item
        for item in publication_api.providers
        if item["id"] not in foundry_provider_ids
    ]
    queued = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json=foundry_publication(gateway_id(publication_api)),
    )
    assert queued.status_code == 202
    publication_id = UUID(queued.json()["publication"]["id"])
    transition_publication(
        publication_api,
        publication_id,
        "queued",
        "awaiting_authorization",
        {
            "error_code": "provider_authorization_required",
            "error_message": "Provider authorization is required",
        },
        "worker",
    )

    waiting = client.get(
        f"/api/v1/model-management/publications/{publication_id}"
    )
    resumed = client.post(
        f"/api/v1/model-management/publications/{publication_id}/authorization/resume",
        headers={"Origin": "http://localhost:5173"},
    )

    assert waiting.status_code == 200
    assert waiting.json()["status"] == "awaiting_authorization"
    assert waiting.json()["authorization"] == {
        "kind": "azure_rbac",
        "principal_id": "39deeba0-9799-4806-96c4-2f65eb0f22d1",
        "resource_endpoint": (
            "https://contoso-ai.services.ai.azure.com/"
        ),
        "role_id": "a97b65f3-24c7-4388-baec-2e87135dc908",
        "role_name": "Cognitive Services User",
    }
    assert "desired_spec" not in waiting.json()
    assert resumed.status_code == 200
    assert resumed.json()["id"] == str(publication_id)
    assert resumed.json()["status"] == "verifying"


def test_foundry_authorization_handoff_can_be_cancelled_before_promotion(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    foundry_provider_ids = {
        item["id"]
        for item in publication_api.providers
        if item["brand_key"] == "microsoft_foundry"
    }
    publication_api.models = [
        item
        for item in publication_api.models
        if item["provider_id"] not in foundry_provider_ids
    ]
    publication_api.runtimes = [
        item
        for item in publication_api.runtimes
        if item["provider_id"] not in foundry_provider_ids
    ]
    publication_api.providers = [
        item
        for item in publication_api.providers
        if item["id"] not in foundry_provider_ids
    ]
    connection = client.post(
        "/api/v1/model-management/connections",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"template": "microsoft_foundry"},
            "auth_mode": "managed_identity",
            "foundry_project_endpoint": (
                "https://contoso-ai.services.ai.azure.com/api/projects/finops"
            ),
        },
    )
    assert connection.status_code == 200
    runtime = next(
        item
        for item in connection.json()["runtimes"]
        if item["config"].get("project_endpoint")
        == "https://contoso-ai.services.ai.azure.com/api/projects/finops"
    )
    queued = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json={
            "gateway_profile_id": gateway_id(publication_api),
            "provider": {"existing_id": runtime["provider_id"]},
            "runtime": {"existing_id": runtime["id"]},
            "model": {"deployment_name": "gpt-5-mini-deployment"},
        },
    )
    assert queued.status_code == 202
    publication_id = UUID(queued.json()["publication"]["id"])
    transition_publication(
        publication_api,
        publication_id,
        "queued",
        "awaiting_authorization",
        {
            "error_code": "provider_authorization_required",
            "error_message": "Provider authorization is required",
        },
        "worker",
    )

    cancelled = client.delete(
        f"/api/v1/model-management/publications/{publication_id}",
        headers={"Origin": "http://localhost:5173"},
    )
    deleted_connection = client.delete(
        f"/api/v1/model-management/connections/{runtime['id']}",
        headers={"Origin": "http://localhost:5173"},
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "rolled_back"
    publication = publication_api.get_gateway_publication(publication_id)
    assert publication is not None
    assert publication["error_code"] == "cancelled_by_owner"
    assert deleted_connection.status_code == 200
    assert all(
        item["id"] != runtime["id"]
        for item in deleted_connection.json()["runtimes"]
    )


def test_unstarted_gateway_publication_can_be_cancelled(
    publication_api: InMemoryRepository,
) -> None:
    queued = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json=bedrock_publication(gateway_id(publication_api)),
    )
    assert queued.status_code == 202
    publication_id = UUID(queued.json()["publication"]["id"])

    cancelled = client.delete(
        f"/api/v1/model-management/publications/{publication_id}",
        headers={"Origin": "http://localhost:5173"},
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "rolled_back"
    assert cancelled.json()["attempt_count"] == 0
    assert cancelled.json()["started_at"] is None
    publication = publication_api.get_gateway_publication(publication_id)
    assert publication is not None
    assert publication["error_code"] == "cancelled_by_owner"
    assert publication["error_message"] == "Publication cancelled before processing"
    assert publication_api.gateway_publication_outbox[0]["status"] == "completed"
    assert publication_api.gateway_publication_audit[-1]["actor"] == (
        queued.json()["publication"]["created_by"]
    )


def test_publication_request_rejects_client_supplied_apim_metadata(
    publication_api: InMemoryRepository,
) -> None:
    payload = bedrock_publication(gateway_id(publication_api))
    runtime = payload["runtime"]
    assert isinstance(runtime, dict)
    runtime["named_value_name"] = "client-controlled-secret"

    response = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json=payload,
    )

    assert response.status_code == 422
    assert publication_api.gateway_publications == []


def test_failed_publication_retries_with_only_a_replacement_key(
    publication_api: InMemoryRepository,
) -> None:
    publication = queue_model(publication_api)
    publication_id = UUID(str(publication["id"]))
    transition_publication(
        publication_api,
        publication_id,
        "queued",
        "failed",
        {"error_code": "expired_key", "error_message": "expired"},
        "worker",
    )

    failed = client.get(
        f"/api/v1/model-management/publications/{publication_id}"
    ).json()
    retried = client.post(
        f"/api/v1/model-management/publications/{publication_id}/retry",
        headers={"Origin": "http://localhost:5173"},
        json={"api_key": "replacement-test-key"},
    )

    assert failed["error_code"] == "publication_failed"
    assert failed["error_message"] is None
    assert "expired" not in str(failed)
    assert retried.status_code == 200
    assert retried.json()["id"] == str(publication_id)
    assert retried.json()["status"] == "queued"
    assert "replacement-test-key" not in retried.text


def test_active_model_can_queue_release_scoped_credential_rotation(
    publication_api: InMemoryRepository,
) -> None:
    publication = queue_model(publication_api)
    activate(publication_api, str(publication["id"]))

    rotation = client.post(
        f"/api/v1/model-management/gateways/{gateway_id(publication_api)}/credentials/rotate",
        headers={"Origin": "http://localhost:5173"},
        json={
            "model_key": "claude-sonnet-4-6-bedrock",
            "api_key": "rotation-test-key",
        },
    )

    assert rotation.status_code == 202
    assert rotation.json()["publication_kind"] == "credential_rotation"
    assert rotation.json()["status"] == "queued"
    assert "rotation-test-key" not in rotation.text


def test_delete_model_queues_removal_before_physically_deleting_registry_row(
    publication_api: InMemoryRepository,
) -> None:
    model = next(
        item
        for item in publication_api.models
        if item["model_key"] == "gpt-5.6-luna"
    )

    response = client.delete(
        f"/api/v1/model-management/models/{model['id']}",
        headers={"Origin": "http://localhost:5173"},
    )

    assert response.status_code == 202
    publication = response.json()["publication"]
    assert publication["publication_kind"] == "model_remove"
    assert publication["model_key"] == model["model_key"]
    assert "desired_spec" not in publication
    assert any(item["id"] == model["id"] for item in publication_api.models)

    activate(publication_api, publication["id"])

    assert all(item["id"] != model["id"] for item in publication_api.models)


def test_owner_session_can_update_model_prices_without_management_bearer(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    model = next(
        item
        for item in publication_api.models
        if item["model_key"] == "gpt-5.6-luna"
    )
    payload = model_write_payload(model)
    payload.update(
        input_cost_per_million=1.25,
        cached_cost_per_million=0.125,
        cache_write_cost_per_million=1.5625,
        output_cost_per_million=7.5,
    )

    response = client.put(
        f"/api/v1/model-management/models/{model['id']}",
        headers={"Origin": "http://localhost:5173"},
        json=payload,
    )

    assert response.status_code == 200
    updated = next(
        item
        for item in response.json()["models"]
        if item["id"] == str(model["id"])
    )
    assert updated["input_cost_per_million"] == 1.25
    assert updated["cached_cost_per_million"] == 0.125
    assert updated["cache_write_cost_per_million"] == 1.5625
    assert updated["output_cost_per_million"] == 7.5


def test_model_routing_identity_change_requires_management_bearer(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    model = next(
        item
        for item in publication_api.models
        if item["model_key"] == "gpt-5.6-luna"
    )
    payload = model_write_payload(model)
    payload["model_key"] = "gpt-5.6-luna-rerouted"
    path = f"/api/v1/model-management/models/{model['id']}"
    origin = "http://localhost:5173"

    denied = client.put(path, headers={"Origin": origin}, json=payload)

    assert denied.status_code == 401
    assert model["model_key"] == "gpt-5.6-luna"

    allowed = client.put(
        path,
        headers={
            "Origin": origin,
            "Authorization": "Bearer management-secret",
        },
        json=payload,
    )

    assert allowed.status_code == 200
    assert model["model_key"] == "gpt-5.6-luna-rerouted"


def test_non_owner_cannot_read_or_write_publications(
    publication_api: InMemoryRepository,
) -> None:
    model = next(
        item
        for item in publication_api.models
        if item["model_key"] == "gpt-5.6-luna"
    )

    def deny_member() -> None:
        raise HTTPException(status_code=403, detail="Owner role is required")

    member = SessionIdentity(
        id="member-id",
        email="member@contoso.com",
        name="Member",
        role="member",
        permissions=frozenset(),
        method="password",
        session_expires_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    app.dependency_overrides[require_authenticated_session] = lambda: member
    app.dependency_overrides[require_publication_owner] = deny_member
    queued = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"},
        json={},
    )
    listed = client.get("/api/v1/model-management/publications")
    updated = client.put(
        f"/api/v1/model-management/models/{model['id']}",
        headers={
            "Origin": "http://localhost:5173",
            "X-Hive-Role": "owner",
        },
        json=model_write_payload(model),
    )

    assert queued.status_code == 403
    assert listed.status_code == 403
    assert updated.status_code == 403
