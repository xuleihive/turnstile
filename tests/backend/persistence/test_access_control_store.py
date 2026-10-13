from __future__ import annotations

from typing import Any
from uuid import UUID

from turnstile_core.persistence.access_control_store import AccessControlStore


class CapturingRows:
    def fetchone(self) -> dict[str, Any]:
        return {"count": 2}

    def fetchall(self) -> list[dict[str, int]]:
        return [{"id": 1}, {"id": 2}]


class CapturingConnection:
    def __init__(self) -> None:
        self.query = ""
        self.parameters: tuple[Any, ...] = ()

    def execute(self, query: str, parameters: tuple[Any, ...]) -> CapturingRows:
        self.query = query
        self.parameters = parameters
        return CapturingRows()


def test_enabled_owner_count_locks_rows_to_prevent_concurrent_last_owner_loss() -> None:
    connection = CapturingConnection()

    count = AccessControlStore.count_enabled_owners(
        connection, UUID("10000000-0000-4000-8000-000000000001")
    )

    assert count == 2
    assert "FOR UPDATE" in connection.query.upper()
    assert "ORDER BY ID" in connection.query.upper()


def test_changing_user_department_updates_owned_application_attribution_atomically() -> None:
    connection = CapturingConnection()
    tenant_id = UUID("10000000-0000-4000-8000-000000000001")
    user_id = UUID("00000000-0000-4000-8000-000000000002")
    department_id = UUID("10000000-0000-4000-8000-000000000003")

    AccessControlStore.update_user(
        connection,
        tenant_id,
        user_id,
        {"organization_unit_id": department_id},
        actor="owner@contoso.com",
    )

    assert "UPDATE GATEWAY_APPLICATION" in connection.query.upper()
    assert "OWNER_USER_ID = %S" in connection.query.upper()
    assert "TENANT_ID = %S" in connection.query.upper()
    assert connection.parameters == (
        department_id,
        str(department_id),
        "owner@contoso.com",
        tenant_id,
        user_id,
    )
