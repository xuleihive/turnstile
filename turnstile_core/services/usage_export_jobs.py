from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from turnstile_core.domain.access_control import AccessControlError
from turnstile_core.domain.usage_export import (
    UsageExportJobError,
    UsageExportJobResponse,
)
from turnstile_core.persistence.export_jobs import (
    ExportJobIdempotencyConflict,
    ExportJobLimitReached,
    ExportJobStore,
)
from turnstile_core.persistence.repository import UsageFilters
from turnstile_core.services.access_control import scope_usage_filters


class ExportJobIdentity(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def email(self) -> str: ...

    @property
    def permissions(self) -> frozenset[str]: ...


class UsageExportJobService:
    def __init__(
        self,
        store: ExportJobStore,
        *,
        max_range_days: int = 366,
        max_active_per_user: int = 2,
        retention_days: int = 7,
    ) -> None:
        self._store = store
        self._max_range_days = max_range_days
        self._max_active_per_user = max_active_per_user
        self._retention_days = retention_days

    @staticmethod
    def _user_id(identity: ExportJobIdentity) -> UUID:
        try:
            return UUID(identity.id)
        except ValueError as error:
            raise UsageExportJobError(403, "Authenticated user is unavailable") from error

    @staticmethod
    def _as_response(row: dict[str, Any]) -> UsageExportJobResponse:
        job_id = UUID(str(row["id"]))
        expires_at = row["expires_at"]
        status = row["status"]
        if status != "expired" and expires_at <= datetime.now(UTC):
            status = "expired"
        return UsageExportJobResponse.model_validate(
            {
                "id": job_id,
                "status": status,
                "from": row["from_ts"],
                "to": row["to_ts"],
                "filters": row.get("filters") or {},
                "row_count": row.get("row_count"),
                "error_code": row.get("error_code"),
                "created_at": row["created_at"],
                "expires_at": expires_at,
                "status_url": f"/api/v1/observability/export-jobs/{job_id}",
            }
        )

    @staticmethod
    def _normalize_range(from_: datetime, to: datetime) -> tuple[datetime, datetime]:
        if from_.utcoffset() is None or to.utcoffset() is None:
            raise UsageExportJobError(422, "Export time range must include a timezone")
        normalized_from = from_.astimezone(UTC)
        normalized_to = to.astimezone(UTC)
        if normalized_to <= normalized_from:
            raise UsageExportJobError(422, "to must be after from")
        return normalized_from, normalized_to

    @staticmethod
    def _filter_snapshot(filters: UsageFilters) -> dict[str, Any]:
        return {
            "organization_id": filters.organization_id,
            "department_id": filters.department_id,
            "project_id": filters.project_id,
            "agent_id": filters.agent_id,
            "model_id": filters.model_id,
            "user_id": list(filters.user_id)
            if isinstance(filters.user_id, tuple)
            else filters.user_id,
            "runtime": list(filters.runtime) if filters.runtime else None,
            "status_code": filters.status_code,
        }

    def create_job(
        self,
        identity: ExportJobIdentity,
        *,
        from_: datetime,
        to: datetime,
        filters: UsageFilters,
        idempotency_key: str,
    ) -> UsageExportJobResponse:
        permissions = identity.permissions
        if not permissions.intersection({"reports.create.all", "reports.create.own"}):
            raise UsageExportJobError(403, "Permission denied")
        if not idempotency_key.strip() or len(idempotency_key) > 128:
            raise UsageExportJobError(422, "Invalid Idempotency-Key")
        normalized_from, normalized_to = self._normalize_range(from_, to)
        if normalized_to - normalized_from > timedelta(days=self._max_range_days):
            raise UsageExportJobError(422, "Export time range exceeds the configured maximum")
        try:
            scoped_filters = scope_usage_filters(identity, filters)
        except AccessControlError as error:
            raise UsageExportJobError(error.status_code, error.detail) from error
        try:
            row = self._store.create_job(
                creator_user_id=self._user_id(identity),
                from_=normalized_from,
                to=normalized_to,
                filters=self._filter_snapshot(scoped_filters),
                idempotency_key=idempotency_key.strip(),
                max_active_per_user=self._max_active_per_user,
                retention_days=self._retention_days,
            )
        except ExportJobLimitReached as error:
            raise UsageExportJobError(429, "Too many active export jobs") from error
        except ExportJobIdempotencyConflict as error:
            raise UsageExportJobError(
                409, "Idempotency-Key was already used for a different export request"
            ) from error
        except LookupError as error:
            raise UsageExportJobError(404, "Export job creator is unavailable") from error
        return self._as_response(row)

    def list_jobs(
        self, identity: ExportJobIdentity, *, limit: int = 50
    ) -> list[UsageExportJobResponse]:
        can_read_all = bool(
            identity.permissions.intersection({"platform.manage", "usage.read.all"})
        )
        if not can_read_all and "usage.read.own" not in identity.permissions:
            raise UsageExportJobError(403, "Permission denied")
        rows = self._store.list_jobs(
            viewer_user_id=self._user_id(identity), can_read_all=can_read_all, limit=limit
        )
        return [self._as_response(row) for row in rows]

    def get_job(
        self, identity: ExportJobIdentity, job_id: UUID
    ) -> UsageExportJobResponse:
        return self._as_response(self._visible_job(identity, job_id))

    def get_download_target(
        self, identity: ExportJobIdentity, job_id: UUID
    ) -> tuple[str, int | None]:
        row = self._visible_job(identity, job_id)
        expires_at = row.get("expires_at")
        if row.get("status") == "expired" or (
            isinstance(expires_at, datetime) and expires_at <= datetime.now(UTC)
        ):
            raise UsageExportJobError(410, "Export job has expired")
        if row.get("status") != "ready":
            raise UsageExportJobError(409, "Export job is not ready")
        blob_name = row.get("blob_name")
        if not isinstance(blob_name, str) or not blob_name:
            raise UsageExportJobError(503, "Usage export is temporarily unavailable")
        row_count = row.get("row_count")
        return blob_name, int(row_count) if row_count is not None else None

    def _visible_job(self, identity: ExportJobIdentity, job_id: UUID) -> dict[str, Any]:
        can_read_all = bool(
            identity.permissions.intersection({"platform.manage", "usage.read.all"})
        )
        if not can_read_all and "usage.read.own" not in identity.permissions:
            raise UsageExportJobError(403, "Permission denied")
        row = self._store.get_job(
            job_id=job_id,
            viewer_user_id=self._user_id(identity),
            can_read_all=can_read_all,
        )
        if row is None:
            raise UsageExportJobError(404, "Export job not found")
        return row
