"""Versioned employee identity projection, separate from budget ledger entities."""

from __future__ import annotations

from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field

from .directory import Cloud, DirectoryModel


class DirectoryIdentity(DirectoryModel):
    cloud: Cloud
    tenant_id: UUID
    object_id: UUID
    governance_user_id: str = Field(min_length=3, max_length=255)
    display_name: str = Field(min_length=1, max_length=160)
    organization_id: str
    organization_name: str
    department_id: str
    department_name: str
    disabled: bool
    directory_version: int = Field(ge=0)
    projection_sequence: int = Field(ge=1)
    valid_until: int | None = None

    @property
    def partition(self) -> str:
        return f"I|1|{self.cloud}|{self.tenant_id}"

    @property
    def row_key(self) -> str:
        return str(self.object_id)

    def entity(self, *, now: int, ttl_seconds: int = 300) -> dict[str, Any]:
        expires = min(
            now + ttl_seconds,
            self.valid_until if self.valid_until is not None else now + ttl_seconds,
        )
        return {
            "ProtocolVersion": 1,
            "Cloud": self.cloud,
            "TenantId": str(self.tenant_id),
            "ObjectId": str(self.object_id),
            "UserId": self.governance_user_id,
            "UserName": self.display_name,
            "OrganizationId": self.organization_id,
            "OrganizationName": self.organization_name,
            "DepartmentId": self.department_id,
            "DepartmentName": self.department_name,
            "Disabled": self.disabled,
            "DirectoryVersion": str(self.directory_version),
            "DirectoryVersion@odata.type": "Edm.Int64",
            "ProjectionSequence": str(self.projection_sequence),
            "ProjectionSequence@odata.type": "Edm.Int64",
            "ExpiresAt": str(expires),
            "ExpiresAt@odata.type": "Edm.Int64",
        }


ProjectionOutcome = Literal["confirmed", "superseded"]


class DirectoryIdentityWriter(Protocol):
    def project(self, identity: DirectoryIdentity, *, now: int) -> ProjectionOutcome: ...
