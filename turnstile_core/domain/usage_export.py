from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from turnstile_core.persistence.repository import UsageFilters

UsageExportStatus = Literal["queued", "running", "ready", "failed", "expired"]


class UsageExportFilterSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: str | None = None
    department_id: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    model_id: str | None = None
    user_id: str | list[str] | None = None
    runtime: list[str] | None = None
    status_code: int | None = Field(default=None, ge=100, le=599)

    def to_usage_filters(self) -> UsageFilters:
        return UsageFilters(
            organization_id=self.organization_id,
            department_id=self.department_id,
            project_id=self.project_id,
            agent_id=self.agent_id,
            model_id=self.model_id,
            user_id=tuple(self.user_id) if isinstance(self.user_id, list) else self.user_id,
            runtime=tuple(self.runtime) if self.runtime else None,
            status_code=self.status_code,
        )


class UsageExportJobCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_: datetime = Field(alias="from")
    to: datetime
    filters: UsageExportFilterSet = Field(default_factory=UsageExportFilterSet)


class UsageExportJobResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    status: UsageExportStatus
    from_: datetime = Field(alias="from")
    to: datetime
    filters: UsageExportFilterSet
    row_count: int | None
    error_code: str | None
    created_at: datetime
    expires_at: datetime
    status_url: str


class UsageExportJobList(BaseModel):
    items: list[UsageExportJobResponse]


class UsageExportJobError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
