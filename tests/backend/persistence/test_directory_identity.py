from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import pytest

from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from turnstile_core.persistence.auth_store import AuthStore, ExternalIdentityConflict

TENANT = UUID("10000000-0000-4000-8000-000000000001")


def test_verified_object_login_does_not_adopt_owner_or_password_by_email(
    directory_database: str,
) -> None:
    store = AuthStore(directory_database)
    try:
        owner = store.create_password_user("owner@example.com", "Owner", "existing-hash", "owner")
        with pytest.raises(ExternalIdentityConflict):
            store.resolve_entra_identity(
                cloud="public",
                tenant_id=TENANT,
                object_id=uuid4(),
                email="owner@example.com",
                display_name="External",
            )
        assert store.find_user_by_email("owner@example.com") == owner
        oid = uuid4()
        with store._connection() as db:
            db.execute(
                """INSERT INTO app_user_external_identity(cloud,tenant_id,object_id,app_user_id)
                   VALUES ('public',%s,%s,%s)""",
                (TENANT, oid, owner["id"]),
            )
        bound = store.resolve_entra_identity(
            cloud="public",
            tenant_id=TENANT,
            object_id=oid,
            email="renamed@example.com",
            display_name="Verified",
        )
        assert bound["id"] == owner["id"]
        assert bound["role"] == "owner" and bound["password_hash"] == "existing-hash"
        assert bound["email"] == "owner@example.com"
        assert bound["display_name"] == owner["display_name"]
        with pytest.raises(ExternalIdentityConflict):
            store.resolve_entra_identity(
                cloud="public",
                tenant_id=uuid4(),
                object_id=oid,
                email="owner@example.com",
                display_name="Another tenant",
            )
    finally:
        store.close()


def test_concurrent_first_login_creates_one_member_and_preserves_disabled_state(
    directory_database: str,
) -> None:
    store = AuthStore(directory_database)
    oid = uuid4()

    def login() -> dict[str, Any]:
        return store.resolve_entra_identity(
            cloud="public",
            tenant_id=TENANT,
            object_id=oid,
            email="member@example.com",
            display_name="Member",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            users = list(executor.map(lambda _: login(), range(2)))
        assert users[0]["id"] == users[1]["id"]
        assert users[0]["role"] == "member" and users[0]["password_hash"] is None
        with store._connection() as db:
            db.execute("UPDATE app_user SET enabled=FALSE WHERE id=%s", (users[0]["id"],))
        assert login()["enabled"] is False
    finally:
        store.close()
