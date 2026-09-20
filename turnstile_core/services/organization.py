from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..domain.organization import (
    OrganizationDirectory,
    OrgUnit,
    OrgUnitCreate,
    OrgUnitReferences,
    OrgUnitRename,
    OrgUnitStatusUpdate,
)
from ..persistence.repository import QueryRepository


class OrganizationNotFoundError(Exception):
    pass


class OrganizationService:
    """Read and edit the organization and its departments.

    Ids are created and retired, never renamed. Budgets, usage, channels and enforcement all
    reference a department by its id as text, and the gateway policy and the Entra app roles
    carry the same string outside this database -- so changing one would mean a coordinated
    rewrite across four tables and two systems, with a window in between where attribution
    lands nowhere. The display name carries none of that and is free.
    """

    def __init__(self, repository: QueryRepository) -> None:
        self._repository = repository

    def _unit(self, row: Mapping[str, Any]) -> OrgUnit:
        return OrgUnit.model_validate({
            "id": row["id"],
            "unit_type": row["unit_type"],
            "parent_id": row["parent_id"],
            "display_name": row["display_name"],
            "status": row["status"],
            "references": OrgUnitReferences.model_validate(
                self._repository.org_unit_references(str(row["id"]))
            ),
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
        })

    def directory(self) -> OrganizationDirectory:
        rows = list(self._repository.org_units())
        units = [self._unit(row) for row in rows]
        organization = next((item for item in units if item.unit_type == "organization"), None)
        departments = [item for item in units if item.unit_type == "department"]
        return OrganizationDirectory(
            organization=organization,
            departments=departments,
        )

    def create_department(self, request: OrgUnitCreate, actor: str) -> OrganizationDirectory:
        rows = list(self._repository.org_units())
        organization = next(
            (row for row in rows if row["unit_type"] == "organization"), None
        )
        if organization is None:
            raise ValueError("This deployment has no organization to add a department to")
        self._repository.create_org_unit(
            request.id, "department", str(organization["id"]), request.display_name, actor
        )
        return self.directory()

    def rename(self, unit_id: str, request: OrgUnitRename, actor: str) -> OrganizationDirectory:
        if self._repository.rename_org_unit(unit_id, request.display_name, actor) is None:
            raise OrganizationNotFoundError(f"No organization unit with id {unit_id}")
        return self.directory()

    def set_status(
        self, unit_id: str, request: OrgUnitStatusUpdate, actor: str
    ) -> OrganizationDirectory:
        rows = {str(row["id"]): row for row in self._repository.org_units()}
        row = rows.get(unit_id)
        if row is None:
            raise OrganizationNotFoundError(f"No organization unit with id {unit_id}")
        if row["unit_type"] == "organization" and request.status == "retired":
            raise ValueError(
                "The organization cannot be retired; rename it to this install's own name"
            )
        if self._repository.set_org_unit_status(unit_id, request.status, actor) is None:
            raise OrganizationNotFoundError(f"No organization unit with id {unit_id}")
        return self.directory()
