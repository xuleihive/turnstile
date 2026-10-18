"""Automatic row scope, independent of navigation and APIM admission."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from .models import EnterpriseEntityCatalog, UsageQueryEntityCatalog

Catalog = TypeVar("Catalog", EnterpriseEntityCatalog, UsageQueryEntityCatalog)


@dataclass(frozen=True)
class DataAccessScope:
    department_ids: tuple[str, ...] = ()
    user_ids: tuple[str, ...] = ()
    visible_department_ids: tuple[str, ...] = ()
    organization_ids: tuple[str, ...] = ()
    team_ids: tuple[str, ...] = ()

    def allows(self, department_id: str, user_id: str) -> bool:
        return department_id in self.department_ids or user_id in self.user_ids

    def snapshot(self) -> list[str]:
        return [
            "scope:v2",
            *(f"department:{value}" for value in self.department_ids),
            *(f"user:{value}" for value in self.user_ids),
        ]

    def covers(self, recorded: Any) -> bool:
        if not isinstance(recorded, list) or not recorded:
            return False
        if "scope:v2" not in recorded:
            return set(recorded).issubset(self.department_ids)
        if recorded == ["scope:v2"]:
            return not self.department_ids and not self.user_ids
        return set(recorded).issubset(self.snapshot())

    def filter_catalog(self, catalog: Catalog) -> Catalog:
        departments = [row for row in catalog.departments if row.id in self.visible_department_ids]
        projects = [row for row in catalog.projects if row.parent_id in self.department_ids]
        project_ids = {row.id for row in projects}
        return catalog.model_copy(
            update={
                "organizations": [
                    row for row in catalog.organizations if row.id in self.organization_ids
                ],
                "departments": departments,
                "projects": projects,
                "agents": [row for row in catalog.agents if row.parent_id in project_ids],
                "users": [row for row in catalog.users if self.allows(row.parent_id or "", row.id)],
                "invocation_testers": [],
            }
        )

    def filter_budget_rows(self, rows: Any) -> list[Mapping[str, Any]]:
        return [
            row
            for row in rows
            if (
                row["scope_type"] == "user"
                and self.allows(str(row.get("parent_scope_id") or ""), str(row["scope_id"]))
            )
            or (row["scope_type"] == "department" and row["scope_id"] in self.department_ids)
        ]
