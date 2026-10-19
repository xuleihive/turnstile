from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from backend.services.auth_service import hash_password, verify_password
from turnstile_core.persistence.auth_store import (
    AccountSessionInvalid,
    AuthStore,
    PasswordMismatch,
    PasswordRateLimited,
)

DATABASE_URL = os.environ.get("TEST_USER_SETTINGS_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="Requires an explicitly supplied isolated PostgreSQL test database"
)


@pytest.fixture
def account() -> Iterator[tuple[AuthStore, UUID, str, str]]:
    assert DATABASE_URL
    store = AuthStore(DATABASE_URL)
    password_hash = hash_password("current-password")
    user = store.create_password_user(f"{uuid4()}@example.com", None, password_hash)
    user_id = UUID(str(user["id"]))
    digest = str(uuid4())
    now = datetime.now(UTC)
    assert store.create_session(
        user_id,
        digest,
        "password",
        now,
        now + timedelta(hours=1),
        expected_password_hash=password_hash,
    )
    yield store, user_id, digest, password_hash
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute("DELETE FROM app_user WHERE id = %s", (user_id,))
    store.close()


def test_name_and_avatar_changes_are_private_persistent_and_audited(
    account: tuple[AuthStore, UUID, str, str],
) -> None:
    store, user_id, digest, _ = account
    store.update_display_name(user_id, digest, "Updated")
    owner = store.session_owner(digest)
    assert owner is not None and owner["display_name"] == "Updated"
    row = store.update_avatar(user_id, digest, "image/png", b"normalized-image")
    saved = store.avatar(user_id)
    assert row is not None and saved is not None and saved["revision"] == row["revision"]
    assert store.avatar(uuid4()) is None
    store.update_avatar(user_id, digest, None, None)
    assert store.avatar(user_id) is None
    assert DATABASE_URL
    with psycopg.connect(DATABASE_URL) as connection:
        events = connection.execute(
            "SELECT action FROM account_security_event WHERE app_user_id = %s ORDER BY created_at",
            (user_id,),
        ).fetchall()
    assert events == [("display_name_updated",), ("avatar_updated",), ("avatar_removed",)]


def test_wrong_password_commits_failures_and_rate_limit(
    account: tuple[AuthStore, UUID, str, str],
) -> None:
    store, user_id, digest, _ = account
    for _ in range(5):
        with pytest.raises(PasswordMismatch):
            store.change_password(
                user_id, digest, "wrong", "new-password-123", verify_password, hash_password
            )
    with pytest.raises(PasswordRateLimited) as error:
        store.change_password(
            user_id, digest, "current-password", "new-password-123", verify_password, hash_password
        )
    assert 0 < error.value.retry_after <= 900
    assert store.session_owner(digest) is not None


def test_password_change_revokes_password_and_entra_sessions(
    account: tuple[AuthStore, UUID, str, str],
) -> None:
    store, user_id, digest, old_hash = account
    now = datetime.now(UTC)
    entra = str(uuid4())
    assert store.create_session(user_id, entra, "entra", now, now + timedelta(hours=1))
    store.change_password(
        user_id, digest, "current-password", "new-password-123", verify_password, hash_password
    )
    assert store.session_owner(digest) is None and store.session_owner(entra) is None
    assert not store.create_session(
        user_id,
        "stale-login",
        "password",
        now,
        now + timedelta(hours=1),
        expected_password_hash=old_hash,
    )
    assert DATABASE_URL
    with psycopg.connect(DATABASE_URL) as connection:
        stored_row = connection.execute(
            "SELECT password_hash FROM app_user WHERE id = %s",
            (user_id,),
        ).fetchone()
    assert stored_row is not None
    assert verify_password("new-password-123", stored_row[0])


def test_concurrent_password_replacements_cannot_both_commit(
    account: tuple[AuthStore, UUID, str, str],
) -> None:
    store, user_id, digest, _ = account

    def change(value: str) -> str:
        try:
            store.change_password(
                user_id, digest, "current-password", value, verify_password, hash_password
            )
            return "success"
        except AccountSessionInvalid:
            return "revoked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(change, ["new-password-111", "new-password-222"]))
    assert sorted(results) == ["revoked", "success"]


def test_avatar_database_constraint_failure_keeps_previous_image(
    account: tuple[AuthStore, UUID, str, str],
) -> None:
    store, user_id, digest, _ = account
    previous = store.update_avatar(user_id, digest, "image/png", b"previous")
    with pytest.raises(psycopg.errors.CheckViolation):
        store.update_avatar(user_id, digest, "text/html", b"bad")
    saved = store.avatar(user_id)
    assert saved is not None and previous is not None
    assert saved["revision"] == previous["revision"]


def test_microsoft_or_other_account_digest_cannot_mutate_local_profile(
    account: tuple[AuthStore, UUID, str, str],
) -> None:
    store, user_id, _, _ = account
    now = datetime.now(UTC)
    assert store.create_session(user_id, "entra", "entra", now, now + timedelta(hours=1))
    for token in ("entra", "foreign-session"):
        with pytest.raises(AccountSessionInvalid):
            store.update_display_name(user_id, token, "Unauthorized")


def test_password_and_audit_roll_back_together(
    account: tuple[AuthStore, UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, user_id, digest, _ = account

    def fail(*args: Any) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(store, "_record_event", fail)
    with pytest.raises(RuntimeError):
        store.change_password(
            user_id, digest, "current-password", "new-password-123", verify_password, hash_password
        )
    assert store.session_owner(digest) is not None
