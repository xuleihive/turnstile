from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

import backend.http.user_management as user_management_module
from backend.api import app
from backend.http.security_audit_support import (
    get_optional_security_audit_service,
    get_security_audit_service,
)
from backend.http.session import get_auth_store
from backend.http.user_management import get_access_control_store
from tests.platform.api.api_support import OWNER_SESSION, StubAuthStore, client
from turnstile_core.config import Settings

ROOT_ID = UUID("10000000-0000-4000-8000-000000000002")


class AuditContextStore:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    @contextmanager
    def transaction(self) -> Iterator[AuditContextStore]:
        yield self

    def primary_tenant_id(self, connection: object) -> UUID:
        return UUID("10000000-0000-4000-8000-000000000001")

    def organization_unit_by_id(
        self, connection: object, tenant_id: UUID, unit_id: UUID
    ) -> dict[str, object] | None:
        if unit_id != ROOT_ID:
            return None
        return {
            "id": ROOT_ID,
            "parent_id": None,
            "code": "root",
            "display_name": "Organization",
            "enabled": True,
        }

    def create_organization_unit(
        self,
        connection: object,
        tenant_id: UUID,
        *,
        parent_id: UUID | None,
        code: str,
        display_name: str,
    ) -> dict[str, object]:
        return {
            "id": uuid4(),
            "parent_id": parent_id,
            "code": code,
            "display_name": display_name,
            "enabled": True,
        }

    def write_audit_event(self, connection: object, **event: Any) -> None:
        self.events.append(event)


class FakeSecurityAuditService:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.filters: list[tuple[Any, Any, int]] = []
        self.recorded: list[Any] = []

    def list_events(self, filters: Any, cursor: Any, limit: int) -> list[dict[str, Any]]:
        self.filters.append((filters, cursor, limit))
        return self.rows[:limit]

    def record(self, event: Any, connection: object | None = None) -> None:
        self.recorded.append(event)


def _create_department(store: AuditContextStore, headers: dict[str, str]) -> None:
    app.dependency_overrides[get_access_control_store] = lambda: store
    app.dependency_overrides[get_auth_store] = lambda: StubAuthStore()
    client.cookies.set("turnstile_session", OWNER_SESSION)
    try:
        response = client.post(
            "/api/v1/admin/organization-units",
            headers=headers,
            json={"parent_id": str(ROOT_ID), "code": "security", "display_name": "Security"},
        )
        assert response.status_code == 201
    finally:
        client.cookies.clear()
        app.dependency_overrides.pop(get_access_control_store, None)
        app.dependency_overrides.pop(get_auth_store, None)


def test_security_audit_events_require_an_authenticated_session() -> None:
    client.cookies.clear()
    app.dependency_overrides[get_auth_store] = lambda: StubAuthStore()

    try:
        response = client.get("/api/v1/security/audit-events")
    finally:
        app.dependency_overrides.pop(get_auth_store, None)

    assert response.status_code == 401


def test_audit_ignores_untrusted_forwarded_for(monkeypatch: pytest.MonkeyPatch) -> None:
    store = AuditContextStore()
    monkeypatch.setattr(
        user_management_module,
        "get_settings",
        lambda: Settings(trust_azure_arr_client_ip=False),
    )

    _create_department(store, {"X-Forwarded-For": "203.0.113.77"})

    assert store.events[0]["client_ip"] != "203.0.113.77"


def test_audit_uses_azure_arr_client_ip_only_when_explicitly_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AuditContextStore()
    monkeypatch.setattr(
        user_management_module,
        "get_settings",
        lambda: Settings(trust_azure_arr_client_ip=True),
    )

    _create_department(store, {"X-ARR-ClientIP": "203.0.113.42"})

    assert store.events[0]["client_ip"] == "203.0.113.42"


def test_owner_can_filter_and_page_audit_events() -> None:
    first_id = UUID("20000000-0000-4000-8000-000000000001")
    row = {
        "id": first_id,
        "created_at": datetime(2026, 10, 12, tzinfo=UTC),
        "actor_user_id": UUID("00000000-0000-4000-8000-000000000001"),
        "actor_email": "owner@contoso.com",
        "actor_role": "owner",
        "action": "user.updated",
        "resource_type": "user",
        "resource_id": "member-id",
        "result": "success",
        "request_id": "request-id",
        "correlation_id": "correlation-id",
        "client_ip": "203.0.113.42",
        "user_agent": "browser",
        "before_data": {"role": "member"},
        "after_data": {"role": "owner"},
    }
    service = FakeSecurityAuditService([row, {**row, "id": uuid4()}])
    app.dependency_overrides[get_auth_store] = lambda: StubAuthStore()
    app.dependency_overrides[get_security_audit_service] = lambda: service
    client.cookies.set("turnstile_session", OWNER_SESSION)
    try:
        response = client.get(
            "/api/v1/security/audit-events",
            params={
                "from": "2026-10-01T00:00:00Z",
                "to": "2026-10-13T00:00:00Z",
                "actor_id": "00000000-0000-4000-8000-000000000001",
                "action": "user.updated",
                "result": "success",
                "limit": 1,
            },
        )
    finally:
        client.cookies.clear()
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_security_audit_service, None)

    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["action"] == "user.updated"
    assert response.json()["next_cursor"] == str(first_id)
    filters, cursor, limit = service.filters[0]
    assert filters.from_ == datetime(2026, 10, 1, tzinfo=UTC)
    assert filters.to == datetime(2026, 10, 13, tzinfo=UTC)
    assert filters.actor_id == UUID("00000000-0000-4000-8000-000000000001")
    assert filters.action == "user.updated"
    assert filters.result == "success"
    assert cursor is None
    assert limit == 2


def test_member_cannot_read_audit_and_the_denial_is_recorded() -> None:
    service = FakeSecurityAuditService()
    app.dependency_overrides[get_auth_store] = lambda: StubAuthStore(role="member")
    app.dependency_overrides[get_security_audit_service] = lambda: service
    app.dependency_overrides[get_optional_security_audit_service] = lambda: service
    client.cookies.set("turnstile_session", OWNER_SESSION)
    try:
        response = client.get("/api/v1/security/audit-events")
    finally:
        client.cookies.clear()
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_security_audit_service, None)
        app.dependency_overrides.pop(get_optional_security_audit_service, None)

    assert response.status_code == 403
    assert len(service.recorded) == 1
    assert service.recorded[0].action == "authorization.denied"
    assert service.recorded[0].after == {"permission": "security_audit.read"}


def test_audit_csv_escapes_formula_values_and_uses_no_store() -> None:
    row = {
        "id": uuid4(),
        "created_at": datetime(2026, 10, 12, tzinfo=UTC),
        "actor_user_id": None,
        "actor_email": '=HYPERLINK("https://example.invalid")',
        "actor_role": "owner",
        "action": "user.updated",
        "resource_type": "user",
        "resource_id": "member-id",
        "result": "success",
        "request_id": "request-id",
        "correlation_id": "correlation-id",
        "client_ip": "203.0.113.42",
        "user_agent": "browser",
        "before_data": {"role": "member"},
        "after_data": {"role": "owner"},
    }
    service = FakeSecurityAuditService([row])
    app.dependency_overrides[get_auth_store] = lambda: StubAuthStore()
    app.dependency_overrides[get_security_audit_service] = lambda: service
    app.dependency_overrides[get_optional_security_audit_service] = lambda: service
    client.cookies.set("turnstile_session", OWNER_SESSION)
    try:
        response = client.get("/api/v1/security/audit-events/export")
    finally:
        client.cookies.clear()
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_security_audit_service, None)
        app.dependency_overrides.pop(get_optional_security_audit_service, None)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-export-truncated"] == "false"
    assert "'=HYPERLINK" in response.text
    assert "password" not in response.text.lower()
    assert len(service.recorded) == 1
    assert service.recorded[0].action == "security_audit.exported"
    assert service.recorded[0].after == {"row_count": 1, "truncated": False}


def test_audit_time_filters_require_timezone_offsets() -> None:
    service = FakeSecurityAuditService()
    app.dependency_overrides[get_auth_store] = lambda: StubAuthStore()
    app.dependency_overrides[get_security_audit_service] = lambda: service
    client.cookies.set("turnstile_session", OWNER_SESSION)
    try:
        response = client.get(
            "/api/v1/security/audit-events",
            params={"from": "2026-10-01T00:00:00", "to": "2026-10-13T00:00:00Z"},
        )
    finally:
        client.cookies.clear()
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_security_audit_service, None)

    assert response.status_code == 422
