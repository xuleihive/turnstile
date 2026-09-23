"""Entra access tokens in place of a session, for scripts and automation.

Accepted only when the deployment is admin-only (ENTRA_ADMIN_ROLE) and tenant-pinned
(ENTRA_TENANT_IDS), and only for a token carrying the admin role. A person's token must
also carry the API scope and an allowed mail domain; a workload's token has no address, so
the role and the pinned tenant are its whole authorization.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from backend.api import app
from backend.http.session import get_auth_store, get_entra_access_verifier
from backend.services.auth_service import EntraAccessTokenVerifier
from tests.platform.api.test_auth_session import CapturingAuthStore
from turnstile_core.config import Settings, get_settings

client = TestClient(app)
TENANT = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "99999999-8888-7777-6666-555555555555"
CLIENT_ID = "3ff03051-0000-4000-8000-000000000000"
ROLE = "Turnstile.Admin"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _StubSigningKey:
    key = _KEY.public_key()


class _StubJwks:
    def get_signing_key_from_jwt(self, token: str) -> _StubSigningKey:
        return _StubSigningKey()


def _verifier() -> EntraAccessTokenVerifier:
    verifier = EntraAccessTokenVerifier(CLIENT_ID, ("contoso.com",), (TENANT,))
    verifier._jwks = _StubJwks()  # type: ignore[assignment]
    return verifier


def _token(*, tenant: str = TENANT, delegated: bool = True, **claims: Any) -> str:
    now = int(time.time())
    body: dict[str, Any] = {
        "aud": CLIENT_ID,
        "iss": f"https://login.microsoftonline.com/{tenant}/v2.0",
        "tid": tenant,
        "oid": "aaaaaaaa-0000-4000-8000-00000000000a",
        "iat": now,
        "exp": now + 600,
        "roles": [ROLE],
        "azp": "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
    }
    if delegated:
        body |= {
            "scp": "Turnstile.Manage",
            "preferred_username": "admin@contoso.com",
            "name": "Admin",
        }
    body |= claims
    return jwt.encode(body, _KEY, algorithm="RS256", headers={"kid": "test"})


def _me(token: str | None = None, scheme: str = "Bearer") -> Any:
    headers = {"Authorization": f"{scheme} {token}"} if token else {}
    return client.get("/api/v1/auth/me", headers=headers)


def _configure(**overrides: Any) -> CapturingAuthStore:
    store = CapturingAuthStore(role="owner")
    values: dict[str, Any] = {
        "entra_client_id": CLIENT_ID,
        "entra_allowed_email_domains": ["contoso.com"],
        "entra_tenant_ids": [TENANT],
        "entra_admin_role": ROLE,
    }
    settings = Settings(**(values | overrides))
    app.dependency_overrides[get_auth_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_entra_access_verifier] = _verifier
    return store


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    client.cookies.clear()
    for dependency in (get_auth_store, get_settings, get_entra_access_verifier):
        app.dependency_overrides.pop(dependency, None)


def test_an_administrators_token_acts_as_that_administrator() -> None:
    store = _configure()
    response = _me(_token())
    assert response.status_code == 200
    assert response.json()["role"] == "owner"
    assert response.json()["email"] == "admin@contoso.com"
    # Provisioned exactly as signing in would, so the identity maps to a real account.
    assert store.entra_upserts == [{"email": "admin@contoso.com", "role": "owner"}]


def test_a_workload_token_acts_as_the_workload() -> None:
    store = _configure()
    response = _me(_token(delegated=False))
    assert response.status_code == 200
    assert response.json()["email"] == "app:04b07795-8ddb-461a-bbee-02f9e1bf7b46"
    assert store.entra_upserts == []


def test_the_api_uri_audience_is_accepted() -> None:
    _configure()
    assert _me(_token(aud=f"api://{CLIENT_ID}")).status_code == 200


@pytest.mark.parametrize(
    ("token", "status"),
    [
        (lambda: _token(roles=[]), 403),
        (lambda: _token(roles=["Turnstile.Reader"]), 403),
        (lambda: _token(scp="User.Read"), 401),
        (lambda: _token(preferred_username="admin@fabrikam.com"), 401),
        (lambda: _token(tenant=OTHER_TENANT), 401),
        (lambda: _token(aud="00000000-0000-0000-0000-000000000000"), 401),
        (lambda: _token(exp=int(time.time()) - 60), 401),
    ],
    ids=[
        "no-role",
        "other-role",
        "missing-scope",
        "other-domain",
        "other-tenant",
        "other-audience",
        "expired",
    ],
)
def test_a_token_that_is_not_an_administrators_is_refused(token: Any, status: int) -> None:
    store = _configure()
    assert _me(token()).status_code == status
    assert store.entra_upserts == []


def test_tokens_are_refused_unless_a_tenant_is_pinned() -> None:
    _configure(entra_tenant_ids=[])
    assert _me(_token()).status_code == 401


def test_tokens_are_refused_unless_the_deployment_is_admin_only() -> None:
    _configure(entra_admin_role="")
    assert _me(_token()).status_code == 401


def test_another_authorization_scheme_is_not_a_token() -> None:
    store = _configure()
    response = _me("dXNlcjpwYXNz", scheme="Basic")
    # Not signed in, rather than a token that failed verification: a Basic credential
    # must never reach the token verifier at all.
    assert response.status_code == 401
    assert response.json()["detail"] == "未登录。"
    assert store.entra_upserts == []
