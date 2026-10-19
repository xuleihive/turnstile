from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

import pytest
from PIL import Image

from backend.api import app
from backend.http.session import get_auth_store
from backend.services.auth_service import hash_password, hash_session_token, verify_password
from tests.platform.api.api_support import _usage_record, client
from turnstile_core.persistence.auth_store import PasswordMismatch, PasswordRateLimited

pytest_plugins = ["tests.platform.api.api_fixtures"]

USER_ID = UUID("00000000-0000-4000-8000-000000000001")


class PersonalStore:
    def __init__(self) -> None:
        self.method = "password"
        self.role = "member"
        self.name: str | None = None
        self.image: dict[str, Any] | None = None
        self.password_hash = hash_password("current-password")
        self.revoked = False
        self.failures = 0

    def session_owner(self, token_sha256: str) -> dict[str, Any] | None:
        if self.revoked or token_sha256 != hash_session_token("personal-session"):
            return None
        return {
            "id": USER_ID,
            "email": "test.user01@contoso.com",
            "display_name": self.name,
            "role": self.role,
            "method": self.method,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
            "avatar_revision": self.image["revision"] if self.image else None,
        }

    def update_display_name(self, user_id: UUID, digest: str, name: str | None) -> None:
        assert user_id == USER_ID and digest == hash_session_token("personal-session")
        self.name = name

    def avatar(self, user_id: UUID) -> dict[str, Any] | None:
        assert user_id == USER_ID
        return self.image

    def update_avatar(
        self, user_id: UUID, digest: str, media_type: str | None, image_bytes: bytes | None
    ) -> dict[str, Any] | None:
        assert user_id == USER_ID and digest == hash_session_token("personal-session")
        self.image = (
            {
                "revision": uuid4(),
                "updated_at": datetime.now(UTC),
                "media_type": media_type,
                "image_bytes": image_bytes,
            }
            if image_bytes
            else None
        )
        return self.image

    def change_password(
        self, user_id: UUID, digest: str, current: str, new: str, verifier: Any, hasher: Any
    ) -> None:
        assert user_id == USER_ID and digest == hash_session_token("personal-session")
        if self.failures >= 5:
            raise PasswordRateLimited(60)
        if not verifier(current, self.password_hash):
            self.failures += 1
            raise PasswordMismatch()
        self.password_hash = hasher(new)
        self.revoked = True


@pytest.fixture
def store() -> Iterator[PersonalStore]:
    instance = PersonalStore()
    app.dependency_overrides[get_auth_store] = lambda: instance
    client.cookies.set("turnstile_session", "personal-session")
    yield instance


def avatar_url() -> str:
    image = BytesIO()
    Image.new("RGB", (128, 128), "green").save(image, "PNG")
    return "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()


@pytest.mark.parametrize("role", ["owner", "member"])
def test_name_is_self_service_for_both_roles(store: PersonalStore, role: str) -> None:
    store.role = role
    response = client.patch("/api/v1/user-settings/me/profile", json={"display_name": "  新名称  "})
    assert response.status_code == 200
    assert response.json()["name"] == "新名称"
    assert response.json()["id"] == str(USER_ID)
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert (
        client.patch("/api/v1/user-settings/me/profile", json={"display_name": ""}).json()["name"]
        is None
    )


@pytest.mark.parametrize(
    "body",
    [
        {"display_name": "x" * 161},
        {"display_name": "name\n"},
        {},
        {"display_name": "ok", "role": "owner"},
        {"display_name": "ok", "email": "other@example.com"},
    ],
)
def test_profile_rejects_restricted_fields(store: PersonalStore, body: dict[str, Any]) -> None:
    assert client.patch("/api/v1/user-settings/me/profile", json=body).status_code == 422
    assert store.name is None


def test_microsoft_profile_and_avatar_remain_read_only(store: PersonalStore) -> None:
    store.method = "entra"
    for method, path, body in [
        ("patch", "profile", {"display_name": "Wrong"}),
        ("put", "avatar", {"avatar_data_url": avatar_url()}),
        (
            "post",
            "password",
            {
                "current_password": "current-password",
                "new_password": "new-password-123",
                "confirm_password": "new-password-123",
            },
        ),
    ]:
        response = getattr(client, method)(f"/api/v1/user-settings/me/{path}", json=body)
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/user-settings/me/avatar").status_code == 403
    assert client.get("/api/v1/auth/me").json()["avatar_url"] is None
    assert client.get("/api/v1/user-settings/me/account").json()["editable"]["avatar"] is False


def test_avatar_round_trip_versioning_and_removal(store: PersonalStore) -> None:
    response = client.put("/api/v1/user-settings/me/avatar", json={"avatar_data_url": avatar_url()})
    assert response.status_code == 200
    first_url = response.json()["avatar_url"]
    assert client.get("/api/v1/auth/me").json()["avatar_url"] == first_url
    read = client.get(first_url)
    assert read.status_code == 200
    assert read.headers["content-type"] == "image/webp"
    assert read.headers["cache-control"] == "no-store"
    assert read.headers["x-content-type-options"] == "nosniff"
    with Image.open(BytesIO(read.content)) as image:
        assert image.size == (128, 128)
        assert not image.getexif()
    assert client.put("/api/v1/user-settings/me/avatar", json={}).status_code == 422
    assert store.image is not None
    replaced = client.put("/api/v1/user-settings/me/avatar", json={"avatar_data_url": avatar_url()})
    assert replaced.json()["avatar_url"] != first_url
    assert client.get(first_url).status_code == 404
    removed = client.put("/api/v1/user-settings/me/avatar", json={"avatar_data_url": None})
    assert removed.json()["avatar_url"] is None
    assert client.get("/api/v1/user-settings/me/avatar").status_code == 404


@pytest.mark.parametrize(
    "value",
    [
        "data:image/svg+xml;base64,PHN2Zy8+",
        "data:image/png;base64,eA==",
        "data:image/png;base64,INVALID",
        "https://example.com/photo.png",
    ],
)
def test_invalid_avatar_does_not_replace_or_echo(store: PersonalStore, value: str) -> None:
    client.put("/api/v1/user-settings/me/avatar", json={"avatar_data_url": avatar_url()})
    before = store.image
    response = client.put("/api/v1/user-settings/me/avatar", json={"avatar_data_url": value})
    assert response.status_code == 422
    assert store.image is before
    assert value not in response.text


def test_large_chunked_upload_is_bounded(store: PersonalStore) -> None:
    response = client.put(
        "/api/v1/user-settings/me/avatar",
        content=iter([b"x" * 50_000, b"x" * 50_000]),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert store.image is None


def test_password_errors_do_not_expire_or_echo_credentials(store: PersonalStore) -> None:
    bad = {
        "current_password": "incorrect-secret",
        "new_password": "new-password-123",
        "confirm_password": "new-password-123",
    }
    assert client.post("/api/v1/user-settings/me/password", json=bad).status_code == 400
    assert client.get("/api/v1/auth/me").status_code == 200
    bad["new_password"] = "short-secret"
    invalid = client.post("/api/v1/user-settings/me/password", json=bad)
    assert invalid.status_code == 422
    assert all(secret not in invalid.text for secret in bad.values())
    assert store.failures == 1


def test_login_validation_does_not_echo_the_new_password() -> None:
    secret = "private-login-secret" * 20
    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": "test.user01@contoso.com",
            "password": secret,
        },
    )
    assert response.status_code == 422
    assert secret not in response.text


def test_successful_password_change_revokes_session(store: PersonalStore) -> None:
    response = client.post(
        "/api/v1/user-settings/me/password",
        json={
            "current_password": "current-password",
            "new_password": "new-password-123",
            "confirm_password": "new-password-123",
        },
    )
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert verify_password("new-password-123", store.password_hash)
    assert not verify_password("current-password", store.password_hash)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_wrong_password_attempts_are_rate_limited(store: PersonalStore) -> None:
    body = {
        "current_password": "incorrect-secret",
        "new_password": "new-password-123",
        "confirm_password": "new-password-123",
    }
    assert [
        client.post("/api/v1/user-settings/me/password", json=body).status_code for _ in range(5)
    ] == [400] * 5
    response = client.post("/api/v1/user-settings/me/password", json=body)
    assert response.status_code == 429 and response.headers["retry-after"] == "60"


@pytest.mark.parametrize("path", ["account", "budget", "models", "usage", "avatar"])
def test_personal_queries_reject_other_user_filters(store: PersonalStore, path: str) -> None:
    response = client.get(f"/api/v1/user-settings/me/{path}?user_id=someone@example.com")
    assert response.status_code == 422


def test_personal_usage_is_not_organization_wide(store: PersonalStore) -> None:
    from backend.http.dependencies import get_repository

    repository = app.dependency_overrides[get_repository]()
    now = datetime.now(UTC)
    mine = _usage_record("personal-mine", "Foundry", 200).model_copy(update={"ts": now})
    theirs = _usage_record("personal-theirs", "Foundry", 900).model_copy(
        update={"user_id": "someone@example.com", "ts": now}
    )
    repository.usage_records = [mine, theirs]
    response = client.get(
        f"/api/v1/user-settings/me/usage?period={now:%Y-%m}&timezone=Asia/Shanghai"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"]["total_tokens"] == 200
    assert payload["totals"]["requests"] == 1
    assert payload["totals"]["cost_state"] == "unpriced"
    assert payload["totals"]["estimated_cost_usd"] is None
    assert "someone@example.com" not in response.text


def test_personal_queries_require_a_session(store: PersonalStore) -> None:
    client.cookies.clear()
    assert client.get("/api/v1/user-settings/me/account").status_code == 401


def test_profile_writes_reject_foreign_origins(store: PersonalStore) -> None:
    response = client.patch(
        "/api/v1/user-settings/me/profile",
        json={"display_name": "Changed"},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403 and store.name is None
