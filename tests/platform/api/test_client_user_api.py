from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, cast
from unittest.mock import Mock
from urllib.parse import unquote
from uuid import uuid4
from zipfile import ZipFile

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from backend.api import app
from backend.http.client_user import (
    attributed_body,
    client_user_service,
    create_client_runtime_http,
)
from backend.services.client_user import ClientUserService
from backend.services.client_user_store import ClientUserStore
from tests.backend.model_platform.test_application_access import _discovery
from tests.backend.model_platform.test_client_user_service import PlatformSubscriptionKeys
from tests.platform.api.api_support import OWNER_SESSION, client
from turnstile_core.config import Settings
from turnstile_core.domain.enterprise import enterprise_catalog
from turnstile_core.domain.models import EnterpriseEntity
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.application_access import ApplicationAccessService

pytest_plugins = [
    "tests.platform.api.api_fixtures",
    "tests.backend.persistence.test_client_user_grants",
]
USER = "00000000-0000-4000-8000-000000000001"


def test_budget_capability_read_disables_uninstalled_usd_without_disabling_tokens() -> None:
    response = client.get("/api/v1/budgets/capabilities")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["token_available"] is True
    assert response.json()["usd_available"] is False
    assert response.json()["reason_code"] == "execution_disabled"


def test_shared_native_connection_pool_never_replays_upstream_cookies_or_user_headers() -> None:
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"set-cookie": "identity=private; Path=/"}, json={})

    async def run() -> None:
        connection = create_client_runtime_http(transport=httpx.MockTransport(upstream))
        async with connection:
            await connection.get("https://gateway.example.com/one", headers={"x-user-id": "one"})
            await connection.get("https://gateway.example.com/two", headers={"x-user-id": "two"})
            assert not list(connection.cookies.jar)

    asyncio.run(run())
    assert [request.headers["x-user-id"] for request in requests] == ["one", "two"]
    assert all("cookie" not in request.headers for request in requests)


@pytest.fixture
def desktop(desktop_postgres: ClientUserStore) -> Any:
    with desktop_postgres._connection() as c:
        c.execute(
            "INSERT INTO app_user(id,email,role) VALUES(%s,'owner@contoso.com','owner') "
            "ON CONFLICT DO NOTHING",
            (USER,),
        )
    repo = InMemoryRepository()
    repo.upsert_token_budget(
        datetime.now(UTC).date().replace(day=1), "user", "owner@contoso.com",
        None, 1_000_000, 80, "fixture",
    )
    gateway = repo.gateways[0]
    gateway["base_url"] = "https://gateway.example.com/turnstile/llm"
    publication = uuid4()
    repo.effective_gateway_releases[gateway["id"]] = publication
    repo.gateway_publications.append(
        {
            "id": publication,
            "desired_spec": {
                "bindings": [],
                "discovery_models": [{"id": x["model_key"]} for x in repo.models],
            },
        }
    )
    applications = ApplicationAccessService(
        repo,
        sync_available=True,
        key_management_available=True,
        key_client=PlatformSubscriptionKeys(),
    )
    applications.sync_discovery(_discovery("cc-switch", "turnstile-dashboard"), "test")
    svc = ClientUserService(
        desktop_postgres,
        repo,
        applications,
        Settings(
            credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
            apim_gateway_url=str(gateway["base_url"]),
            apim_dashboard_subscription_key=SecretStr("platform-primary"),
        ),
        enabled=True,
        origin="https://platform.example",
    )
    app.dependency_overrides[client_user_service] = lambda: svc
    yield svc
    app.dependency_overrides.pop(client_user_service, None)
    with desktop_postgres._connection() as c:
        c.execute("DELETE FROM client_user_installation WHERE user_id=%s", (USER,))
        c.execute("DELETE FROM client_user_grant WHERE user_id=%s", (USER,))
        c.execute("DELETE FROM client_device_authorization WHERE user_id=%s", (USER,))


def begin(service: ClientUserService, purpose: str = "import") -> dict[str, Any]:
    response = client.post(
        "/api/v1/client-auth/device-authorizations",
        json={
            "user_id": USER,
            "binding": service._binding(USER),
            "installation_id": str(uuid4()),
            "purpose": purpose,
        },
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def test_browser_approval_and_native_catalog(
    desktop: ClientUserService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = client.get("/api/v1/client-downloads/user-options")
    assert options.status_code == 200
    assert options.headers["cache-control"] == "no-store"
    device = begin(desktop)
    uri = "/api/v1/client-auth/verification?user_code=" + device["user_code"]
    response = client.get(uri)
    assert response.status_code == 200, response.text
    chosen = response.json()["subscription"]
    fields = {k: chosen[k] for k in ("application_id", "subscription_id", "gateway_id")}
    approved = client.post(
        "/api/v1/client-auth/decision",
        json={
            "user_code": device["user_code"],
            "approve": True,
            **fields,
        },
    )
    assert approved.status_code == 200, approved.text
    token = client.post(
        "/api/v1/client-auth/token",
        json={
            "grant_type": "device_code",
            "device_code": device["device_code"],
        },
    )
    assert token.status_code == 200, token.text
    access = token.json()["access_token"]
    auth = {"Authorization": "Bearer " + access}
    models = client.get("/api/v1/client-runtime/catalog", headers=auth)
    assert models.status_code == 200, models.text
    assert models.json()["models"] and models.json()["aliases"]
    assert models.headers["cache-control"] == "no-store"
    usage = client.get("/api/v1/client-runtime/usage", headers=auth)
    assert usage.status_code == 200 and "windows" in usage.json()
    spy = Mock(wraps=desktop.usage)
    monkeypatch.setattr(desktop, "usage", spy)
    fresh = client.get("/api/v1/client-runtime/usage?fresh=true", headers=auth)
    assert fresh.status_code == 200 and fresh.headers["cache-control"] == "no-store"
    assert spy.call_args.kwargs == {"fresh": True}
    assert client.get("/api/v1/client-runtime/usage?fresh=true").status_code == 401
    assert "subscriptionKey" not in models.text and "primary-secret" not in models.text
    revoked = client.post("/api/v1/client-auth/revoke", headers=auth)
    assert revoked.status_code == 200
    assert client.get("/api/v1/client-runtime/catalog", headers=auth).status_code == 401


@pytest.mark.parametrize("purpose", ["login", "import", "reauth"])
def test_browser_string_body_needs_json_content_type(
    desktop: ClientUserService, purpose: str
) -> None:
    device = begin(desktop, purpose)
    payload = json.dumps({"user_code": device["user_code"], "approve": False})
    rejected = client.post(
        "/api/v1/client-auth/decision",
        content=payload,
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"][0]["type"] == "model_attributes_type"
    status = client.get(
        "/api/v1/client-auth/verification", params={"user_code": device["user_code"]}
    )
    assert status.json()["state"] == "pending"
    decided = client.post(
        "/api/v1/client-auth/decision",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "denied"


def test_user_binding_session_and_sensitive_validation(desktop: ClientUserService) -> None:
    oversized = client.post(
        "/api/v1/client-auth/token",
        content=b"x" * (16 * 1024 + 1),
        headers={"content-type": "application/json"},
    )
    assert oversized.status_code == 413
    invalid = client.post(
        "/api/v1/client-auth/device-authorizations",
        json={
            "user_id": str(uuid4()),
            "binding": desktop._binding(USER),
            "installation_id": str(uuid4()),
        },
    )
    assert invalid.status_code == 400
    malformed = client.post(
        "/api/v1/client-auth/token",
        json={
            "grant_type": "unknown",
            "refresh_token": "private-fixture-value",
        },
    )
    assert malformed.status_code == 422 and "private-fixture-value" not in malformed.text
    device = begin(desktop, "reauth")
    status = client.get(
        "/api/v1/client-auth/verification", params={"user_code": device["user_code"]}
    )
    assert status.json()["reauth_required"]
    denied = client.post(
        "/api/v1/client-auth/decision",
        json={
            "user_code": device["user_code"],
            "approve": True,
        },
    )
    assert denied.status_code == 409
    client.cookies.clear()
    try:
        assert client.post("/api/v1/client-downloads/magpie-user").status_code == 401
        assert client.get("/api/v1/client-runtime/catalog").status_code == 401
    finally:
        client.cookies.set("turnstile_session", OWNER_SESSION)


def test_download_is_authorized_and_refresh_keeps_binding(desktop: ClientUserService) -> None:
    assert client.post("/api/v1/client-downloads/magpie-user", json={}).status_code == 422
    assert (
        client.post(
            "/api/v1/client-downloads/magpie-user", json={"acknowledged": False}
        ).status_code
        == 422
    )
    response = client.post("/api/v1/client-downloads/magpie-user", json={"acknowledged": True})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert 'filename="aigateway-proxy-user-Owner.zip"' in response.headers["content-disposition"]
    assert response.headers["access-control-expose-headers"] == "Content-Disposition"
    assert USER not in response.headers["content-disposition"]
    assert USER.replace("-", "") not in response.headers["content-disposition"]
    with ZipFile(BytesIO(response.content)) as archive:
        assert {name.split("/", 1)[0] for name in archive.namelist()} == {
            "aigateway-proxy-user-Owner"
        }
        cfg = json.loads(archive.read("aigateway-proxy-user-Owner/aigateway-user.config.json"))
    auth = {"Authorization": "Bearer " + cfg["authorization"]["access"]}
    initial = client.get("/api/v1/client-runtime/catalog", headers=auth)
    assert initial.status_code == 200 and initial.json()["models"]
    refreshed = client.post("/api/v1/client-runtime/refresh-models", headers=auth)
    assert refreshed.status_code == 200
    assert refreshed.json()["selection"] == initial.json()["selection"]
    repository = cast(InMemoryRepository, desktop.repository)
    repository.models.clear()
    refreshed = client.post("/api/v1/client-runtime/refresh-models", headers=auth)
    assert refreshed.status_code == 200 and refreshed.json()["models"] == {}
    assert refreshed.json()["selection"]["gateway_id"] == cfg["selection"]["gateway_id"]
    client.post("/api/v1/client-auth/revoke", headers=auth)
    assert client.post("/api/v1/client-runtime/refresh-models", headers=auth).status_code == 401


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("pooled", [False, True])
def test_proxy_reuses_platform_subscription_and_verified_personal_identity(
    desktop: ClientUserService,
    monkeypatch: pytest.MonkeyPatch,
    delegated: bool,
    pooled: bool,
) -> None:
    response = client.post("/api/v1/client-downloads/magpie-user", json={"acknowledged": True})
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        cfg = json.loads(archive.read("aigateway-proxy-user-Owner/aigateway-user.config.json"))
    model = next(iter(cfg["catalog"]["models"]))
    catalog = enterprise_catalog()
    catalog.users.append(
        EnterpriseEntity(
            id="owner@contoso.com",
            name="研发用户" if delegated else "Edwin",
            parent_id="department-platform",
        )
    )
    monkeypatch.setattr("backend.services.client_user.catalog_for", lambda *_: catalog)
    if delegated:
        monkeypatch.setattr(
            "backend.services.client_user.prepare_budget_delegation",
            lambda *a, **kw: "backend-token",
        )
    sent: list[httpx.Request] = []
    closed: list[bool] = []

    class Upstream:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def build_request(self, method: str, url: str, **kwargs: Any) -> httpx.Request:
            return httpx.Request(method, url, **kwargs)

        async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
            sent.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=httpx.ByteStream(b'{"result":"ok"}'),
            )

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr("backend.http.client_user.httpx.AsyncClient", Upstream)
    if pooled:
        monkeypatch.setattr(app.state, "client_runtime_http", Upstream(), raising=False)
    reply = client.post(
        "/api/v1/client-runtime/responses",
        headers={
            "Authorization": "Bearer " + cfg["authorization"]["access"],
            "Ocp-Apim-Subscription-Key": "forged-key",
            "x-user-id": "forged-user",
            "x-agent-name": "WorkBuddy",
            "x-magpie-session": "native-session",
            "x-hive-turn-index": "2",
        },
        json={
            "model": model,
            "input": "Hello",
            "user": "forged-user",
        },
    )
    assert reply.status_code == 200, reply.text
    assert len(closed) == (0 if pooled else 1)
    assert len(sent) == 1
    outbound = sent[0]
    assert outbound.headers["ocp-apim-subscription-key"] == "platform-primary"
    assert "authorization" not in outbound.headers
    assert outbound.headers["x-agent-name"] == "WorkBuddy"
    assert outbound.headers["x-hive-run-id"] == "native-session"
    assert outbound.headers["x-hive-turn-index"] == "2"
    assert json.loads(outbound.content)["user"] == "owner@contoso.com"
    if delegated:
        assert "x-turnstile-client-context" not in outbound.headers
        assert outbound.headers["x-turnstile-delegation-token"] == "Bearer backend-token"
        assert unquote(outbound.headers["x-user-id"]) == "owner@contoso.com"
        assert outbound.headers["x-turnstile-metadata-encoding"] == "percent-v1"
        assert unquote(outbound.headers["x-user-name"]) == "研发用户"
        return
    assert "x-user-id" not in outbound.headers
    claims = jwt.decode(
        outbound.headers["x-turnstile-client-context"],
        desktop.signing_key,
        algorithms=["HS256"],
        audience="turnstile-client-runtime",
    )
    assert "scope" not in claims
    assert claims["preferred_username"] == "owner@contoso.com"
    assert claims["subscription"] == "turnstile-dashboard"
    assert claims["jti"] == outbound.headers["x-request-id"]
    assert claims["application_id"] == cfg["selection"]["application_id"]


def test_approval_rejects_a_different_default_application(desktop: ClientUserService) -> None:
    device = begin(desktop, "login")
    result = client.post(
        "/api/v1/client-auth/decision",
        json={
            "user_code": device["user_code"],
            "approve": True,
            "application_id": str(uuid4()),
        },
    )
    assert result.status_code == 409
    status = client.get(
        "/api/v1/client-auth/verification", params={"user_code": device["user_code"]}
    )
    assert status.json()["state"] == "pending"


@pytest.mark.parametrize("protocol", ["openai_responses", "openai_chat", "openai_images"])
def test_openai_user_is_verified_not_agent_supplied(protocol: str) -> None:
    original = {"model": "test", "user": "forged", "input": "hello"}
    forwarded = json.loads(attributed_body(original, "verified-user", protocol))
    assert forwarded["user"] == "verified-user"
    assert forwarded["input"] == "hello"
    assert original["user"] == "forged"


def test_anthropic_user_id_is_verified_without_unsupported_user_field() -> None:
    forwarded = json.loads(
        attributed_body(
            {"model": "test", "user": "forged", "metadata": {"user_id": "forged"}},
            "verified-user",
            "anthropic_messages",
        )
    )
    assert "user" not in forwarded
    assert forwarded["metadata"]["user_id"] == hashlib.sha256(b"verified-user").hexdigest()
