"""Admin-only Microsoft sign-in.

With ENTRA_ADMIN_ROLE set, only a token carrying that app role signs in, it signs in as
Owner, and a token without it creates no account. With ENTRA_TENANT_IDS set, a correctly
signed token from any other tenant is refused. Both settings empty keeps the upstream
multi-tenant, automatic-Member behaviour unchanged.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from backend.api import app, get_entra_verifier
from backend.http.session import get_auth_store
from backend.services.auth_service import AuthError, EntraIdentity, EntraTokenVerifier
from tests.platform.api.test_auth_session import CapturingAuthStore
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings, get_settings

client = TestClient(app)
ORIGIN = {"Origin": "http://localhost:5173"}
TENANT = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "99999999-8888-7777-6666-555555555555"
CLIENT_ID = "3ff03051-0000-4000-8000-000000000000"
ADMIN_ROLE = "Turnstile.Admin"


class RoleVerifier:
    """Stands in for a verified token carrying the given app roles."""

    def __init__(self, roles: tuple[str, ...]) -> None:
        self._roles = roles

    def verify(self, token: str) -> EntraIdentity:
        return EntraIdentity(
            email="admin@contoso.com", display_name="Admin", tenant_id=TENANT, roles=self._roles
        )


@pytest.fixture
def admin_only() -> Iterator[CapturingAuthStore]:
    store = CapturingAuthStore(role="owner")
    settings = Settings(entra_admin_role=ADMIN_ROLE, entra_tenant_ids=[TENANT])
    app.dependency_overrides[get_auth_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: settings
    yield store
    client.cookies.clear()
    for dependency in (get_auth_store, get_settings, get_entra_verifier):
        app.dependency_overrides.pop(dependency, None)


def test_a_sign_in_without_the_admin_role_is_refused_and_leaves_no_account(
    admin_only: CapturingAuthStore,
) -> None:
    app.dependency_overrides[get_entra_verifier] = lambda: RoleVerifier(())
    response = client.post("/api/v1/auth/entra", headers=ORIGIN, json={"id_token": "t"})

    assert response.status_code == 403
    assert response.json()["detail"] == "此控制台仅限管理员使用。"
    assert admin_only.entra_upserts == []
    assert admin_only.sessions == []
    assert "set-cookie" not in response.headers


def test_a_different_role_is_not_the_admin_role(admin_only: CapturingAuthStore) -> None:
    app.dependency_overrides[get_entra_verifier] = lambda: RoleVerifier(("Turnstile.Reader",))
    response = client.post("/api/v1/auth/entra", headers=ORIGIN, json={"id_token": "t"})

    assert response.status_code == 403
    assert admin_only.entra_upserts == []


def test_the_admin_role_signs_in_as_owner(admin_only: CapturingAuthStore) -> None:
    app.dependency_overrides[get_entra_verifier] = lambda: RoleVerifier((ADMIN_ROLE,))
    response = client.post("/api/v1/auth/entra", headers=ORIGIN, json={"id_token": "t"})

    assert response.status_code == 200
    assert admin_only.entra_upserts == [{"email": "admin@contoso.com", "role": "owner"}]
    assert [session["method"] for session in admin_only.sessions] == ["entra"]


def test_without_an_admin_role_setting_members_are_still_provisioned() -> None:
    store = CapturingAuthStore(role="member")
    app.dependency_overrides[get_auth_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: Settings()
    app.dependency_overrides[get_entra_verifier] = lambda: RoleVerifier(())
    try:
        response = client.post("/api/v1/auth/entra", headers=ORIGIN, json={"id_token": "t"})
    finally:
        client.cookies.clear()
        for dependency in (get_auth_store, get_settings, get_entra_verifier):
            app.dependency_overrides.pop(dependency, None)

    assert response.status_code == 200
    assert store.entra_upserts == [{"email": "admin@contoso.com", "role": None}]


# --- the verifier, against real RS256 tokens -------------------------------------------

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _StubSigningKey:
    key = _KEY.public_key()


class _StubJwks:
    def get_signing_key_from_jwt(self, token: str) -> _StubSigningKey:
        return _StubSigningKey()


def _token(tenant: str, **claims: Any) -> str:
    now = int(time.time())
    body = {
        "aud": CLIENT_ID,
        "iss": f"https://login.microsoftonline.com/{tenant}/v2.0",
        "tid": tenant,
        "iat": now,
        "exp": now + 600,
        "preferred_username": "admin@contoso.com",
        "name": "Admin",
        **claims,
    }
    return jwt.encode(body, _KEY, algorithm="RS256", headers={"kid": "test"})


def _verifier(tenant_ids: tuple[str, ...] = ()) -> EntraTokenVerifier:
    verifier = EntraTokenVerifier(CLIENT_ID, ("contoso.com",), tenant_ids)
    verifier._jwks = _StubJwks()  # type: ignore[assignment]
    return verifier


def test_the_verifier_reads_app_roles_from_the_token() -> None:
    identity = _verifier((TENANT,)).verify(_token(TENANT, roles=[ADMIN_ROLE, 7]))

    assert identity.roles == (ADMIN_ROLE,)
    assert identity.tenant_id == TENANT


def test_a_token_without_roles_has_none() -> None:
    assert _verifier().verify(_token(TENANT)).roles == ()
    assert _verifier().verify(_token(TENANT, roles="Turnstile.Admin")).roles == ()


def test_a_pinned_tenant_refuses_a_correctly_signed_token_from_another_tenant() -> None:
    with pytest.raises(AuthError):
        _verifier((TENANT,)).verify(_token(OTHER_TENANT))


def test_no_pinned_tenant_keeps_the_multi_tenant_behaviour() -> None:
    assert _verifier().verify(_token(OTHER_TENANT)).tenant_id == OTHER_TENANT


def test_tenant_pinning_ignores_case() -> None:
    assert _verifier((TENANT.upper(),)).verify(_token(TENANT)).tenant_id == TENANT


def test_tenant_ids_must_be_guids() -> None:
    assert Settings(entra_tenant_ids=[TENANT.upper()]).entra_tenant_ids == [TENANT]
    with pytest.raises(ValueError):
        Settings(entra_tenant_ids=["contoso.onmicrosoft.com"])


def test_the_store_writes_the_role_the_token_proved() -> None:
    # The store is PostgreSQL-only, so like the session-expiry check above this is read
    # from the source: with a role, the insert carries it and a returning admin is set to
    # it again, and sign-in never touches `enabled` either way.
    source = (REPOSITORY_ROOT / "turnstile_core/persistence/auth_store.py").read_text(
        encoding="utf-8"
    )
    assert "INSERT INTO app_user (email, display_name, role)" in source
    assert "role = EXCLUDED.role" in source
    assert "enabled = EXCLUDED" not in source
