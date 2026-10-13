from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from uuid import UUID

import pytest

from backend.api import app
from backend.http.service_dependencies import application_access_service
from backend.http.session import get_auth_store
from backend.http.user_management import get_access_control_store
from backend.services.auth_service import verify_password
from tests.platform.api.api_support import MEMBER_SESSION, OWNER_SESSION, StubAuthStore, client

pytest_plugins = ("tests.platform.api.api_fixtures",)
OWNER_ID = UUID("00000000-0000-4000-8000-000000000001")
MEMBER_ID = UUID("00000000-0000-4000-8000-000000000002")
ORG_ID = UUID("10000000-0000-4000-8000-000000000002")
ROOT_ORG_ID = UUID("10000000-0000-4000-8000-000000000003")
TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")


class FakeAccessControlStore:
    def __init__(self) -> None:
        self.users: dict[UUID, dict[str, Any]] = {
            OWNER_ID: {
                "id": OWNER_ID,
                "email": "owner@contoso.com",
                "display_name": "Owner",
                "role": "owner",
                "enabled": True,
                "organization_unit_id": None,
                "password_hash": None,
            },
            MEMBER_ID: {
                "id": MEMBER_ID,
                "email": "member@contoso.com",
                "display_name": "Member",
                "role": "member",
                "enabled": True,
                "organization_unit_id": ORG_ID,
                "password_hash": None,
            },
        }
        self.organization_units: dict[UUID, dict[str, Any]] = {
            ROOT_ORG_ID: {
                "id": ROOT_ORG_ID,
                "parent_id": None,
                "code": "root",
                "display_name": "Organization",
                "enabled": True,
            },
            ORG_ID: {
                "id": ORG_ID,
                "parent_id": ROOT_ORG_ID,
                "code": "platform",
                "display_name": "Platform",
                "enabled": True,
            }
        }
        self.audit_events: list[dict[str, Any]] = []
        self.revoked_users: list[UUID] = []
        self.auth_store: StubAuthStore | None = None
        self.fail_audit = False

    @contextmanager
    def transaction(self) -> Iterator[FakeAccessControlStore]:
        previous = deepcopy(
            (self.users, self.organization_units, self.audit_events, self.revoked_users)
        )
        try:
            yield self
        except Exception:
            self.users, self.organization_units, self.audit_events, self.revoked_users = previous
            raise

    def primary_tenant_id(self, connection: object) -> UUID:
        return TENANT_ID

    def list_users(
        self, connection: object, tenant_id: UUID, **filters: Any
    ) -> list[dict[str, Any]]:
        rows = list(self.users.values())
        if filters.get("search"):
            query = filters["search"].lower()
            rows = [row for row in rows if query in row["email"].lower()]
        if filters.get("role"):
            rows = [row for row in rows if row["role"] == filters["role"]]
        if filters.get("enabled") is not None:
            rows = [row for row in rows if row["enabled"] is filters["enabled"]]
        if filters.get("organization_unit_id"):
            rows = [
                row
                for row in rows
                if str(row["organization_unit_id"]) == filters["organization_unit_id"]
            ]
        return [
            {key: value for key, value in row.items() if key != "password_hash"}
            | {"organization_unit_name": "Platform" if row["organization_unit_id"] else None}
            for row in rows
        ]

    def list_roles(self, connection: object) -> list[dict[str, Any]]:
        return [
            {
                "code": "owner",
                "display_name": "Owner",
                "is_builtin": True,
                "permissions": ["platform.manage"],
            },
            {
                "code": "member",
                "display_name": "Member",
                "is_builtin": True,
                "permissions": ["usage.read.own"],
            },
        ]

    def list_organization_units(self, connection: object, tenant_id: UUID) -> list[dict[str, Any]]:
        return list(self.organization_units.values())

    def user_by_email(
        self, connection: object, tenant_id: UUID, email: str
    ) -> dict[str, Any] | None:
        return next((row for row in self.users.values() if row["email"] == email), None)

    def user_by_id(
        self, connection: object, tenant_id: UUID, user_id: UUID
    ) -> dict[str, Any] | None:
        row = self.users.get(user_id)
        return deepcopy(row) if row else None

    def role_exists(self, connection: object, role: str) -> bool:
        return role in {"owner", "member"}

    def count_enabled_owners(self, connection: object, tenant_id: UUID) -> int:
        return sum(row["role"] == "owner" and row["enabled"] for row in self.users.values())

    def create_user(self, connection: object, tenant_id: UUID, **values: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": UUID(int=3),
            "enabled": True,
            **values,
            "created_at": "2026-10-12T00:00:00Z",
        }
        self.users[row["id"]] = row
        return row

    def update_user(
        self,
        connection: object,
        tenant_id: UUID,
        user_id: UUID,
        patch: dict[str, Any],
        *,
        actor: str,
    ) -> dict[str, Any] | None:
        row = self.users.get(user_id)
        if row is None:
            return None
        row.update(patch)
        return row

    def revoke_user_sessions(self, connection: object, user_id: UUID) -> None:
        self.revoked_users.append(user_id)
        if self.auth_store is not None:
            self.auth_store.enabled = False

    def write_audit_event(self, connection: object, **event: Any) -> None:
        if self.fail_audit:
            raise RuntimeError("audit insert failed")
        self.audit_events.append(event)

    def organization_unit_by_id(
        self, connection: object, tenant_id: UUID, unit_id: UUID
    ) -> dict[str, Any] | None:
        return self.organization_units.get(unit_id)

    def create_organization_unit(
        self,
        connection: object,
        tenant_id: UUID,
        *,
        parent_id: UUID | None,
        code: str,
        display_name: str,
    ) -> dict[str, Any]:
        unit_id = UUID(int=4)
        row: dict[str, Any] = {
            "id": unit_id,
            "parent_id": parent_id,
            "code": code,
            "display_name": display_name,
            "enabled": True,
        }
        self.organization_units[unit_id] = row
        return row

    def organization_unit_in_use(self, connection: object, tenant_id: UUID, unit_id: UUID) -> bool:
        has_user = any(row["organization_unit_id"] == unit_id for row in self.users.values())
        has_child = any(row.get("parent_id") == unit_id for row in self.organization_units.values())
        return has_user or has_child

    def update_organization_unit(
        self,
        connection: object,
        tenant_id: UUID,
        unit_id: UUID,
        patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        row = self.organization_units.get(unit_id)
        if row is None:
            return None
        row.update(patch)
        return row

    def delete_organization_unit(self, connection: object, tenant_id: UUID, unit_id: UUID) -> bool:
        return self.organization_units.pop(unit_id, None) is not None


class StubApplicationMappingProjection:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.owner_ids: list[UUID | None] = []

    def refresh_application_mappings(self, owner_user_id: UUID | None = None) -> int:
        self.calls += 1
        self.owner_ids.append(owner_user_id)
        if self.fail:
            raise RuntimeError("table credentials must not be returned")
        return 1


@pytest.fixture
def access_store() -> Iterator[FakeAccessControlStore]:
    store = FakeAccessControlStore()
    app.dependency_overrides[get_access_control_store] = lambda: store
    yield store
    app.dependency_overrides.pop(get_access_control_store, None)


def test_user_directory_requires_an_authenticated_session() -> None:
    client.cookies.clear()

    response = client.get("/api/v1/admin/users")

    assert response.status_code == 401


def test_owner_can_read_user_directory_without_exposing_password_hash(
    access_store: FakeAccessControlStore,
) -> None:
    response = client.get("/api/v1/admin/users")

    assert response.status_code == 200
    assert {row["email"] for row in response.json()} == {
        "owner@contoso.com",
        "member@contoso.com",
    }
    assert all("password_hash" not in row for row in response.json())


def test_member_cannot_read_admin_user_directory() -> None:
    app.dependency_overrides[get_auth_store] = lambda: StubAuthStore(role="member")
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        response = client.get("/api/v1/admin/users")
    finally:
        client.cookies.clear()
        app.dependency_overrides.pop(get_auth_store, None)

    assert response.status_code == 403


def test_member_cannot_read_role_permission_matrix() -> None:
    app.dependency_overrides[get_auth_store] = lambda: StubAuthStore(role="member")
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        response = client.get("/api/v1/admin/roles")
    finally:
        client.cookies.clear()
        app.dependency_overrides.pop(get_auth_store, None)

    assert response.status_code == 403


def test_owner_creation_hashes_initial_password_and_never_returns_or_audits_it(
    access_store: FakeAccessControlStore,
) -> None:
    secret = "correct horse battery staple"
    response = client.post(
        "/api/v1/admin/users",
        json={
            "email": "New.User@Contoso.com",
            "display_name": "New User",
            "role": "member",
            "initial_password": secret,
        },
    )

    assert response.status_code == 201
    assert response.json()["email"] == "new.user@contoso.com"
    assert secret not in response.text
    created = access_store.user_by_email(access_store, TENANT_ID, "new.user@contoso.com")
    assert created is not None and created["password_hash"] != secret
    assert verify_password(secret, created["password_hash"])
    assert secret not in str(access_store.audit_events)


def test_invalid_initial_password_is_not_echoed_in_validation_response(
    access_store: FakeAccessControlStore,
) -> None:
    secret = "short"
    response = client.post(
        "/api/v1/admin/users",
        json={"email": "invalid.password@contoso.com", "initial_password": secret},
    )

    assert response.status_code == 422
    assert secret not in response.text


def test_last_enabled_owner_cannot_be_demoted_or_disabled(
    access_store: FakeAccessControlStore,
) -> None:
    response = client.patch(
        f"/api/v1/admin/users/{OWNER_ID}",
        json={"role": "member", "enabled": False},
    )

    assert response.status_code == 409
    assert access_store.users[OWNER_ID]["role"] == "owner"
    assert access_store.users[OWNER_ID]["enabled"] is True
    assert access_store.audit_events == []


def test_organization_unit_with_users_cannot_be_deleted(
    access_store: FakeAccessControlStore,
) -> None:
    response = client.delete(f"/api/v1/admin/organization-units/{ORG_ID}")

    assert response.status_code == 409
    assert ORG_ID in access_store.organization_units


def test_tenant_root_organization_unit_cannot_be_deleted(
    access_store: FakeAccessControlStore,
) -> None:
    access_store.users[MEMBER_ID]["organization_unit_id"] = None
    access_store.organization_units = {ROOT_ORG_ID: access_store.organization_units[ROOT_ORG_ID]}

    response = client.delete(f"/api/v1/admin/organization-units/{ROOT_ORG_ID}")

    assert response.status_code == 409
    assert ROOT_ORG_ID in access_store.organization_units


def test_department_creation_requires_an_existing_parent(
    access_store: FakeAccessControlStore,
) -> None:
    response = client.post(
        "/api/v1/admin/organization-units",
        json={"code": "security", "display_name": "Security"},
    )

    assert response.status_code == 422
    assert len(access_store.organization_units) == 2


def test_owner_can_read_the_organization_tree(
    access_store: FakeAccessControlStore,
) -> None:
    response = client.get("/api/v1/admin/organization-units")

    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == {str(ROOT_ORG_ID), str(ORG_ID)}


def test_updating_an_organization_unit_refreshes_apim_application_mappings(
    access_store: FakeAccessControlStore,
) -> None:
    projection = StubApplicationMappingProjection()
    app.dependency_overrides[application_access_service] = lambda: projection

    try:
        response = client.patch(
            f"/api/v1/admin/organization-units/{ORG_ID}",
            json={"display_name": "Platform Services"},
        )
    finally:
        app.dependency_overrides.pop(application_access_service, None)

    assert response.status_code == 200
    assert response.json()["display_name"] == "Platform Services"
    assert projection.calls == 1
    assert projection.owner_ids == [None]


def test_organization_update_reports_pending_apim_refresh_when_projection_fails(
    access_store: FakeAccessControlStore,
) -> None:
    projection = StubApplicationMappingProjection(fail=True)
    app.dependency_overrides[application_access_service] = lambda: projection

    try:
        response = client.patch(
            f"/api/v1/admin/organization-units/{ORG_ID}",
            json={"enabled": False},
        )
    finally:
        app.dependency_overrides.pop(application_access_service, None)

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Organization unit state was saved, but APIM access mapping refresh is pending."
    )
    assert access_store.organization_units[ORG_ID]["enabled"] is False
    assert projection.calls == 1
    assert projection.owner_ids == [None]


def test_disabling_a_user_revokes_their_existing_session_immediately(
    access_store: FakeAccessControlStore,
) -> None:
    auth_store = StubAuthStore()
    projection = StubApplicationMappingProjection()
    access_store.auth_store = auth_store
    app.dependency_overrides[get_auth_store] = lambda: auth_store
    app.dependency_overrides[application_access_service] = lambda: projection
    client.cookies.set("turnstile_session", OWNER_SESSION)
    try:
        updated = client.patch(
            f"/api/v1/admin/users/{MEMBER_ID}", json={"enabled": False}
        )
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False
        assert access_store.revoked_users == [MEMBER_ID]
        assert projection.calls == 1
        assert projection.owner_ids == [MEMBER_ID]

        auth_store.role = "member"
        client.cookies.set("turnstile_session", MEMBER_SESSION)
        current_profile = client.get("/api/v1/auth/me")

        assert current_profile.status_code == 401
    finally:
        app.dependency_overrides.pop(application_access_service, None)


def test_disabled_user_stays_disabled_when_apim_projection_fails(
    access_store: FakeAccessControlStore,
) -> None:
    projection = StubApplicationMappingProjection(fail=True)
    app.dependency_overrides[application_access_service] = lambda: projection

    try:
        response = client.patch(
            f"/api/v1/admin/users/{MEMBER_ID}", json={"enabled": False}
        )

        assert response.status_code == 503
        assert response.json()["detail"] == (
            "User state was saved, but APIM access mapping refresh is pending."
        )
        assert access_store.users[MEMBER_ID]["enabled"] is False
        assert access_store.revoked_users == [MEMBER_ID]
        assert projection.calls == 1
        assert projection.owner_ids == [MEMBER_ID]
    finally:
        app.dependency_overrides.pop(application_access_service, None)


def test_audit_insert_failure_rolls_back_user_creation(
    access_store: FakeAccessControlStore,
) -> None:
    access_store.fail_audit = True
    with pytest.raises(RuntimeError, match="audit insert failed"):
        client.post(
            "/api/v1/admin/users",
            json={"email": "rolled.back@contoso.com", "role": "member"},
        )

    assert access_store.user_by_email(access_store, TENANT_ID, "rolled.back@contoso.com") is None
    assert access_store.audit_events == []
