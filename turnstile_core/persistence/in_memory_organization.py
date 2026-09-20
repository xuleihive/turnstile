from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from ..domain.models import TokenUsageRecord


class InMemoryOrganizationRepositoryMixin:
    org_units_by_id: dict[str, dict[str, Any]]
    org_unit_audit: list[dict[str, Any]]
    gateway_applications: list[dict[str, Any]]
    token_budgets: dict[tuple[date, str, str], dict[str, Any]]
    usage_records: list[TokenUsageRecord]

    def org_units(self) -> Sequence[dict[str, Any]]:
        return sorted(
            self.org_units_by_id.values(),
            key=lambda row: (row["unit_type"] != "organization", row["display_name"], row["id"]),
        )

    def create_org_unit(
        self,
        unit_id: str,
        unit_type: str,
        parent_id: str | None,
        display_name: str,
        actor: str,
    ) -> dict[str, Any] | None:
        if unit_id in self.org_units_by_id:
            raise ValueError(f"An organization unit already exists with id {unit_id}")
        if parent_id is not None and parent_id not in self.org_units_by_id:
            raise ValueError(f"Unknown parent: {parent_id}")
        now = datetime.now(UTC)
        row = {
            "id": unit_id,
            "unit_type": unit_type,
            "parent_id": parent_id,
            "display_name": display_name,
            "status": "active",
            "created_by": actor,
            "created_at": now,
            "updated_by": actor,
            "updated_at": now,
        }
        self.org_units_by_id[unit_id] = row
        self.org_unit_audit.append({
            "id": uuid4(),
            "unit_id": unit_id,
            "operation": "created",
            "before_state": None,
            "after_state": {"display_name": display_name, "parent_id": parent_id},
            "actor": actor,
            "created_at": now,
        })
        return row

    def rename_org_unit(
        self, unit_id: str, display_name: str, actor: str
    ) -> dict[str, Any] | None:
        row = self.org_units_by_id.get(unit_id)
        if row is None:
            return None
        if row["display_name"] == display_name:
            return row
        now = datetime.now(UTC)
        self.org_unit_audit.append({
            "id": uuid4(),
            "unit_id": unit_id,
            "operation": "renamed",
            "before_state": {"display_name": row["display_name"]},
            "after_state": {"display_name": display_name},
            "actor": actor,
            "created_at": now,
        })
        row.update(display_name=display_name, updated_by=actor, updated_at=now)
        return row

    def set_org_unit_status(
        self, unit_id: str, status: str, actor: str
    ) -> dict[str, Any] | None:
        row = self.org_units_by_id.get(unit_id)
        if row is None:
            return None
        if row["status"] == status:
            return row
        if status == "retired" and any(
            child["parent_id"] == unit_id and child["status"] == "active"
            for child in self.org_units_by_id.values()
        ):
            raise ValueError("Retire the units under this one first")
        now = datetime.now(UTC)
        self.org_unit_audit.append({
            "id": uuid4(),
            "unit_id": unit_id,
            "operation": "retired" if status == "retired" else "restored",
            "before_state": {"status": row["status"]},
            "after_state": {"status": status},
            "actor": actor,
            "created_at": now,
        })
        row.update(status=status, updated_by=actor, updated_at=now)
        return row

    def org_unit_references(self, unit_id: str) -> dict[str, int]:
        budgets = sum(
            1
            for (_, _, scope_id), row in self.token_budgets.items()
            if scope_id == unit_id or row.get("parent_scope_id") == unit_id
        )
        usage = sum(
            1
            for record in self.usage_records
            if getattr(record, "department_id", None) == unit_id
            or getattr(record, "organization_id", None) == unit_id
        )
        applications = sum(
            1 for row in self.gateway_applications if row.get("department_id") == unit_id
        )
        return {
            "budgets": budgets,
            "usage_records": usage,
            "applications": applications,
        }

    def list_org_unit_audit(self, unit_id: str, limit: int = 20) -> Sequence[dict[str, Any]]:
        return sorted(
            (row for row in self.org_unit_audit if row["unit_id"] == unit_id),
            key=lambda row: row["created_at"],
            reverse=True,
        )[:limit]
