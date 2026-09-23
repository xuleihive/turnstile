from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.api import app, get_entra_verifier
from backend.http.session import get_auth_store
from backend.services.auth_service import EntraIdentity, hash_password
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings, get_settings

client = TestClient(app)
USER_ID = UUID("00000000-0000-4000-8000-000000000001")


class CapturingAuthStore:
    def __init__(self, role: str = "owner") -> None:
        self.sessions: list[dict[str, Any]] = []
        self.entra_upserts: list[dict[str, Any]] = []
        self.expired_cleanup_calls = 0
        self.role = role

    def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        if email != "owner@contoso.com":
            return None
        return self._user(hash_password("correct-password"))

    def upsert_entra_user(
        self, email: str, display_name: str | None, role: str | None = None
    ) -> dict[str, Any]:
        self.entra_upserts.append({"email": email, "role": role})
        return self._user(None, email=email, display_name=display_name)

    def delete_expired_sessions(self) -> int:
        self.expired_cleanup_calls += 1
        return 0

    def create_session(
        self,
        user_id: UUID,
        token_sha256: str,
        method: str,
        authenticated_at: datetime,
        expires_at: datetime,
    ) -> None:
        self.sessions.append(
            {
                "user_id": user_id,
                "token_sha256": token_sha256,
                "method": method,
                "authenticated_at": authenticated_at,
                "expires_at": expires_at,
            }
        )

    def session_owner(self, token_sha256: str) -> dict[str, Any] | None:
        session = next(
            (item for item in self.sessions if item["token_sha256"] == token_sha256),
            None,
        )
        if session is None:
            return None
        return {
            "id": USER_ID,
            "email": "owner@contoso.com",
            "display_name": "Owner",
            "role": self.role,
            "method": session["method"],
            "created_at": session["authenticated_at"],
            "expires_at": session["expires_at"],
        }

    def touch_last_login(self, user_id: UUID) -> None:
        assert user_id == USER_ID

    def _user(
        self,
        password_hash: str | None,
        *,
        email: str = "owner@contoso.com",
        display_name: str | None = "Owner",
    ) -> dict[str, Any]:
        return {
            "id": USER_ID,
            "email": email,
            "display_name": display_name,
            "password_hash": password_hash,
            "role": self.role,
            "enabled": True,
        }


class AcceptingEntraVerifier:
    def verify(self, token: str) -> EntraIdentity:
        assert token == "verified-id-token"
        return EntraIdentity(
            email="owner@contoso.com",
            display_name="Owner",
            tenant_id="00000000-0000-4000-8000-000000000002",
        )


def test_default_session_policy_is_fixed_and_role_based() -> None:
    settings = Settings()

    assert settings.session_ttl_hours_for("member") == 24
    assert settings.session_ttl_hours_for("owner") == 12
    assert not hasattr(settings, "member_session_idle_hours")
    assert not hasattr(settings, "owner_session_idle_minutes")
    assert not hasattr(settings, "sensitive_reauthentication_minutes")


def test_auth_store_uses_only_absolute_expiry_and_revokes_on_password_update() -> None:
    source = (REPOSITORY_ROOT / "turnstile_core/persistence/auth_store.py").read_text(
        encoding="utf-8"
    )

    assert "s.expires_at > now()" in source
    assert "last_seen_at" not in source
    assert 'connection.execute("DELETE FROM user_session WHERE user_id = %s"' in source


@pytest.fixture
def auth_dependencies() -> Iterator[CapturingAuthStore]:
    store = CapturingAuthStore()
    settings = Settings(
        owner_session_ttl_hours=2,
        member_session_ttl_hours=4,
        production=True,
    )
    app.dependency_overrides[get_auth_store] = lambda: store
    app.dependency_overrides[get_entra_verifier] = AcceptingEntraVerifier
    app.dependency_overrides[get_settings] = lambda: settings
    yield store
    client.cookies.clear()
    app.dependency_overrides.pop(get_auth_store, None)
    app.dependency_overrides.pop(get_entra_verifier, None)
    app.dependency_overrides.pop(get_settings, None)


def test_password_and_entra_issue_the_same_absolute_session_ttl(
    auth_dependencies: CapturingAuthStore,
) -> None:
    before = datetime.now(UTC)
    password = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost:5173"},
        json={"email": "owner@contoso.com", "password": "correct-password"},
    )
    client.cookies.clear()
    entra = client.post(
        "/api/v1/auth/entra",
        headers={"Origin": "http://localhost:5173"},
        json={"id_token": "verified-id-token"},
    )
    after = datetime.now(UTC)

    assert password.status_code == entra.status_code == 200
    assert [session["method"] for session in auth_dependencies.sessions] == [
        "password",
        "entra",
    ]
    assert auth_dependencies.expired_cleanup_calls == 2

    for response, session in zip(
        (password, entra), auth_dependencies.sessions, strict=True
    ):
        expires_at = datetime.fromisoformat(response.json()["session_expires_at"])
        assert expires_at == session["expires_at"]
        assert datetime.fromisoformat(response.json()["session_expires_at"]) == expires_at
        assert "session_idle_timeout_seconds" not in response.json()
        assert "session_idle_expires_at" not in response.json()
        assert before.replace(microsecond=0).timestamp() + 7200 <= expires_at.timestamp()
        assert expires_at.timestamp() <= after.timestamp() + 7200
        set_cookie = response.headers["set-cookie"]
        assert "Max-Age=7200" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=lax" in set_cookie


def test_profile_reports_absolute_expiry_without_renewing_session(
    auth_dependencies: CapturingAuthStore,
) -> None:
    login = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost:5173"},
        json={"email": "owner@contoso.com", "password": "correct-password"},
    )
    assert login.status_code == 200
    session_cookie = login.cookies.get("turnstile_session")
    assert session_cookie is not None
    headers = {"Cookie": f"turnstile_session={session_cookie}"}

    first = client.get("/api/v1/auth/me", headers=headers)
    second = client.get("/api/v1/auth/me", headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["session_expires_at"] == second.json()["session_expires_at"]
    assert "set-cookie" not in first.headers
    assert "set-cookie" not in second.headers
    assert len(auth_dependencies.sessions) == 1


def test_member_login_uses_the_longer_member_policy() -> None:
    store = CapturingAuthStore(role="member")
    settings = Settings(
        owner_session_ttl_hours=2,
        member_session_ttl_hours=4,
        production=True,
    )
    app.dependency_overrides[get_auth_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://localhost:5173"},
            json={"email": "owner@contoso.com", "password": "correct-password"},
        )
    finally:
        client.cookies.clear()
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 200
    assert response.json()["role"] == "member"
    assert "Max-Age=14400" in response.headers["set-cookie"]
