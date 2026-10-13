from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from backend.api import app, runtime_service
from backend.http.dependencies import get_repository
from backend.http.session import get_auth_store
from backend.services.runtime_service import ModelRuntimeService
from tests.platform.api.api_support import MEMBER_SESSION, OWNER_SESSION, StubAuthStore
from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.runtime_models import ProviderWrite, RuntimeWrite
from turnstile_core.persistence.in_memory import InMemoryRepository

GATEWAY_ID = "10000000-0000-4000-8000-000000000001"
MANAGEMENT_KEY = "test-management-key"


@pytest.fixture
def registry_api() -> Iterator[tuple[TestClient, InMemoryRepository]]:
    repository = InMemoryRepository()
    settings = Settings(
        production=True,
        management_api_key=SecretStr(MANAGEMENT_KEY),
        credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
        apim_principal_id="test-apim-principal",
    )
    service = ModelRuntimeService(repository, settings)
    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_auth_store] = StubAuthStore
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[runtime_service] = lambda: service
    client = TestClient(app)
    try:
        yield client, repository
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


def gateway_payload(*, enabled: bool = True) -> dict[str, object]:
    return {
        "name": "Azure API Management",
        "implementation": "apim",
        "base_url": "https://gateway.example.test/turnstile/llm",
        "auth_type": "api_key",
        "credential": "test-apim-subscription-key",
        "enabled": enabled,
        "is_default": True,
        "config": {"header_name": "Ocp-Apim-Subscription-Key"},
    }


def foundry_connection(
    client: TestClient,
) -> tuple[dict[str, object], dict[str, object]]:
    response = client.post(
        "/api/v1/model-management/connections",
        json={
            "gateway_profile_id": GATEWAY_ID,
            "provider": {"template": "microsoft_foundry"},
            "auth_mode": "managed_identity",
            "foundry_project_endpoint": (
                "https://foundry-test.services.ai.azure.com/api/projects/test-project"
            ),
        },
    )
    assert response.status_code == 200, response.text
    registry = response.json()
    runtime = next(
        row for row in registry["runtimes"]
        if row["name"] == "Microsoft Foundry foundry-test/test-project via APIM"
    )
    provider = next(row for row in registry["providers"] if row["id"] == runtime["provider_id"])
    return runtime, provider


@pytest.mark.parametrize("resource", ["providers", "runtimes"])
def test_owner_cookie_updates_connection_registry_without_management_bearer(
    registry_api: tuple[TestClient, InMemoryRepository],
    resource: str,
) -> None:
    client, repository = registry_api
    client.cookies.set("turnstile_session", OWNER_SESSION)
    runtime, provider = foundry_connection(client)
    row = runtime if resource == "runtimes" else provider
    schema = RuntimeWrite if resource == "runtimes" else ProviderWrite
    payload = {key: value for key, value in row.items() if key in schema.model_fields}
    payload["enabled"] = False
    response = client.put(f"/api/v1/model-management/{resource}/{row['id']}", json=payload)
    assert response.status_code == 200, response.text
    saved = next(item for item in response.json()[resource] if item["id"] == row["id"])
    assert saved["enabled"] is False
    stored = next(item for item in getattr(repository, resource) if str(item["id"]) == row["id"])
    assert stored["enabled"] is False
    if resource == "runtimes":
        assert saved["provider_id"] == provider["id"]
        assert saved["config"] == runtime["config"]
    assert MANAGEMENT_KEY not in response.text


@pytest.mark.parametrize("resource", ["providers", "runtimes"])
def test_owner_cookie_creates_connection_registry_without_management_bearer(
    registry_api: tuple[TestClient, InMemoryRepository],
    resource: str,
) -> None:
    client, repository = registry_api
    client.cookies.set("turnstile_session", OWNER_SESSION)
    runtime, provider = foundry_connection(client)
    row = runtime if resource == "runtimes" else provider
    schema = RuntimeWrite if resource == "runtimes" else ProviderWrite
    payload = {key: value for key, value in row.items() if key in schema.model_fields}
    payload["name"] = "Another registry connection"
    if resource == "runtimes":
        payload["is_default"] = False
    before = len(getattr(repository, resource))
    response = client.post(f"/api/v1/model-management/{resource}", json=payload)
    assert response.status_code == 200, response.text
    assert len(getattr(repository, resource)) == before + 1
    assert any(item["name"] == "Another registry connection" for item in response.json()[resource])


@pytest.mark.parametrize("resource", ["providers", "runtimes"])
@pytest.mark.parametrize(
    ("session", "headers", "expected_status"),
    [
        (None, {"Authorization": f"Bearer {MANAGEMENT_KEY}", "X-Hive-Role": "owner"}, 401),
        ("invalid-session", {"Authorization": f"Bearer {MANAGEMENT_KEY}"}, 401),
        (MEMBER_SESSION, {"Authorization": f"Bearer {MANAGEMENT_KEY}"}, 403),
        (OWNER_SESSION, {"Origin": "https://attacker.example"}, 403),
        (OWNER_SESSION, {"Authorization": "Bearer wrong-management-key"}, 401),
    ],
)
def test_connection_registry_write_denies_unauthorized_changes(
    registry_api: tuple[TestClient, InMemoryRepository],
    resource: str,
    session: str | None,
    headers: dict[str, str],
    expected_status: int,
) -> None:
    client, repository = registry_api
    client.cookies.set("turnstile_session", OWNER_SESSION)
    runtime, provider = foundry_connection(client)
    client.cookies.clear()
    if session:
        client.cookies.set("turnstile_session", session)
    row = runtime if resource == "runtimes" else provider
    schema = RuntimeWrite if resource == "runtimes" else ProviderWrite
    payload = {key: value for key, value in row.items() if key in schema.model_fields}
    payload["enabled"] = False
    before = deepcopy(getattr(repository, resource))
    response = client.put(
        f"/api/v1/model-management/{resource}/{row['id']}", headers=headers, json=payload,
    )
    assert response.status_code == expected_status, response.text
    assert getattr(repository, resource) == before


def test_owner_cookie_updates_gateway_without_exposing_management_key(
    registry_api: tuple[TestClient, InMemoryRepository],
) -> None:
    client, repository = registry_api
    client.cookies.set("turnstile_session", OWNER_SESSION)
    response = client.put(
        f"/api/v1/model-management/gateways/{GATEWAY_ID}",
        json=gateway_payload(enabled=False),
    )
    assert response.status_code == 200, response.text
    gateway = next(g for g in response.json()["gateways"] if g["id"] == GATEWAY_ID)
    assert gateway["enabled"] is False
    assert gateway["base_url"] == "https://gateway.example.test/turnstile/llm"
    assert gateway["credential"] is None
    assert gateway["credential_configured"] is True
    assert MANAGEMENT_KEY not in response.text
    stored = next(g for g in repository.gateways if str(g["id"]) == GATEWAY_ID)
    assert stored["enabled"] is False


def test_owner_cookie_creates_gateway_without_management_bearer(
    registry_api: tuple[TestClient, InMemoryRepository],
) -> None:
    client, repository = registry_api
    client.cookies.set("turnstile_session", OWNER_SESSION)
    payload = gateway_payload()
    payload.update(name="Another APIM gateway", is_default=False)
    response = client.post("/api/v1/model-management/gateways", json=payload)
    assert response.status_code == 200, response.text
    assert any(g["name"] == "Another APIM gateway" for g in repository.gateways)


@pytest.mark.parametrize(
    ("session", "expected_status"),
    [(None, 401), ("invalid-session", 401), (MEMBER_SESSION, 403)],
)
def test_management_key_and_role_header_cannot_replace_owner_session(
    registry_api: tuple[TestClient, InMemoryRepository],
    session: str | None,
    expected_status: int,
) -> None:
    client, repository = registry_api
    if session:
        client.cookies.set("turnstile_session", session)
    before = deepcopy(repository.gateways)
    response = client.put(
        f"/api/v1/model-management/gateways/{GATEWAY_ID}",
        headers={"Authorization": f"Bearer {MANAGEMENT_KEY}", "X-Hive-Role": "owner"},
        json=gateway_payload(enabled=False),
    )
    assert response.status_code == expected_status
    assert repository.gateways == before


def test_owner_write_from_disallowed_origin_is_rejected(
    registry_api: tuple[TestClient, InMemoryRepository],
) -> None:
    client, repository = registry_api
    client.cookies.set("turnstile_session", OWNER_SESSION)
    before = deepcopy(repository.gateways)
    response = client.put(
        f"/api/v1/model-management/gateways/{GATEWAY_ID}",
        headers={"Origin": "https://attacker.example"},
        json=gateway_payload(enabled=False),
    )
    assert response.status_code == 403
    assert repository.gateways == before


def test_explicit_wrong_management_bearer_is_still_rejected(
    registry_api: tuple[TestClient, InMemoryRepository],
) -> None:
    client, repository = registry_api
    client.cookies.set("turnstile_session", OWNER_SESSION)
    before = deepcopy(repository.gateways)
    response = client.put(
        f"/api/v1/model-management/gateways/{GATEWAY_ID}",
        headers={"Authorization": "Bearer wrong-management-key"},
        json=gateway_payload(enabled=False),
    )
    assert response.status_code == 401
    assert repository.gateways == before
