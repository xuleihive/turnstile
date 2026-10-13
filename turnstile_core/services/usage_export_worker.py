from __future__ import annotations

import csv
import io
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from tempfile import SpooledTemporaryFile
from typing import IO, Any, Protocol
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from turnstile_core.domain.security_audit import AuditEventWrite, AuditResult
from turnstile_core.integrations.usage_export_blob import PrivateUsageExportBlob
from turnstile_core.persistence.audit_store import AuditStore
from turnstile_core.persistence.export_jobs import ExportJobStore
from turnstile_core.persistence.repository_contract import QueryRepository
from turnstile_core.persistence.repository_support import UsageFilters
from turnstile_core.services.security_audit import SecurityAuditService
from turnstile_core.services.usage_export_format import (
    CSV_COLUMNS,
    _report_markdown,
    _safe_csv_value,
)

logger = logging.getLogger(__name__)

LEASE_SECONDS = 10 * 60
MAX_ATTEMPTS = 3
ROWS_PER_LEASE_RENEWAL = 1000
SPOOL_MEMORY_LIMIT_BYTES = 8 * 1024 * 1024
NONREADY_BLOB_CLEANUP_BATCH = 100


class ExportJobStoreProtocol(Protocol):
    def claim_next_job(
        self, *, worker_id: str, now: datetime, lease_seconds: int, max_attempts: int
    ) -> dict[str, Any] | None: ...

    def fail_exhausted_jobs(
        self, *, now: datetime, max_attempts: int
    ) -> list[dict[str, Any]]: ...

    def expire_due_jobs(self, *, now: datetime, limit: int) -> list[dict[str, Any]]: ...

    def list_nonready_blob_jobs(self, *, limit: int) -> list[dict[str, Any]]: ...

    def clear_nonready_blob(self, *, job_id: UUID, blob_name: str) -> None: ...

    def set_blob_name(
        self, *, job_id: UUID, worker_id: str, now: datetime, blob_name: str
    ) -> bool: ...

    def renew_lease(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> bool: ...

    def mark_ready(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        row_count: int,
        blob_name: str,
    ) -> bool: ...

    def record_failure(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        error_code: str,
        max_attempts: int,
        terminal: bool = False,
    ) -> dict[str, Any] | None: ...


class UsageExportBlob(Protocol):
    def upload(self, name: str, body: IO[bytes]) -> None: ...

    def delete(self, name: str) -> None: ...


class AuditRecorder(Protocol):
    def record(self, event: AuditEventWrite) -> None: ...


class LeaseLost(Exception):
    """Signals that this worker no longer owns a live job lease."""


class ExportRowLimitExceeded(Exception):
    pass


class UsageExportWorker:
    def __init__(
        self,
        job_store: ExportJobStoreProtocol,
        repository: QueryRepository,
        blob: UsageExportBlob,
        audit: AuditRecorder,
        *,
        max_rows: int = 1_000_000,
        lease_seconds: int = LEASE_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        clock: Callable[[], datetime] | None = None,
        owned_resources: tuple[Any, ...] = (),
    ) -> None:
        self._jobs = job_store
        self._repository = repository
        self._blob = blob
        self._audit = audit
        self._max_rows = max_rows
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owned_resources = owned_resources

    def run_once(self, worker_id: str, now: datetime) -> int:
        if now.utcoffset() is None:
            raise ValueError("Worker time must include a timezone")
        self._recover_exhausted(now)
        self._expire_due(now)
        job = self._jobs.claim_next_job(
            worker_id=worker_id,
            now=now,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        if job is None:
            return 0
        self._process(job, worker_id)
        return 1

    def close(self) -> None:
        for resource in self._owned_resources:
            close = getattr(resource, "close", None)
            if callable(close):
                close()

    def _process(self, job: dict[str, Any], worker_id: str) -> None:
        job_id = UUID(str(job["id"]))
        blob_name = self._blob_name(job)
        now = self._clock()
        if not self._jobs.set_blob_name(
            job_id=job_id,
            worker_id=worker_id,
            now=now,
            blob_name=blob_name,
        ):
            return

        archive: SpooledTemporaryFile[bytes] | None = None
        try:
            from_ = job["from_ts"]
            to = job["to_ts"]
            filters = self._usage_filters(job.get("filters") or {})
            analysis = _report_markdown(self._repository, from_, to, filters)
            self._renew(job_id, worker_id)
            archive, row_count = self._build_archive(
                analysis, from_, to, filters, job_id, worker_id
            )
            self._renew(job_id, worker_id)
            self._blob.upload(blob_name, archive)
            ready = self._jobs.mark_ready(
                job_id=job_id,
                worker_id=worker_id,
                now=self._clock(),
                row_count=row_count,
                blob_name=blob_name,
            )
            if not ready:
                raise LeaseLost
        except LeaseLost:
            return
        except ExportRowLimitExceeded:
            self._fail(job, worker_id, "row_limit_exceeded", terminal=True)
            self._cleanup_nonready_blobs()
            return
        except Exception as error:
            logger.warning(
                "Usage export failed; job=%s error_type=%s",
                job_id,
                type(error).__name__,
            )
            failed = self._fail(job, worker_id, "export_failed")
            if failed is not None and failed.get("status") == "failed":
                self._cleanup_nonready_blobs()
            return
        finally:
            if archive is not None:
                archive.close()

        self._record_event(
            job,
            action="usage_export.completed",
            result="success",
            after={"export_job_id": str(job_id), "row_count": row_count},
        )

    def _build_archive(
        self,
        analysis: str,
        from_: datetime,
        to: datetime,
        filters: UsageFilters,
        job_id: UUID,
        worker_id: str,
    ) -> tuple[SpooledTemporaryFile[bytes], int]:
        archive_buffer = SpooledTemporaryFile(  # noqa: SIM115 - returned to the worker for upload
            max_size=SPOOL_MEMORY_LIMIT_BYTES, mode="w+b"
        )
        row_count = 0
        try:
            with ZipFile(archive_buffer, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("analysis.md", analysis)
                with archive.open("requests.csv", "w") as compressed_csv:
                    text_csv = io.TextIOWrapper(
                        compressed_csv, encoding="utf-8-sig", newline="", write_through=True
                    )
                    try:
                        writer = csv.DictWriter(
                            text_csv, fieldnames=CSV_COLUMNS, extrasaction="ignore"
                        )
                        writer.writeheader()
                        for row in self._repository.iter_usage_requests(from_, to, filters):
                            if row_count >= self._max_rows:
                                raise ExportRowLimitExceeded
                            writer.writerow(
                                {
                                    column: _safe_csv_value(row.get(column))
                                    for column in CSV_COLUMNS
                                }
                            )
                            row_count += 1
                            if row_count % ROWS_PER_LEASE_RENEWAL == 0:
                                self._renew(job_id, worker_id)
                        text_csv.flush()
                    finally:
                        text_csv.detach()
            archive_buffer.seek(0)
            return archive_buffer, row_count
        except Exception:
            archive_buffer.close()
            raise

    def _renew(self, job_id: UUID, worker_id: str) -> None:
        now = self._clock()
        if not self._jobs.renew_lease(
            job_id=job_id,
            worker_id=worker_id,
            now=now,
            lease_seconds=self._lease_seconds,
        ):
            raise LeaseLost

    def _fail(
        self,
        job: dict[str, Any],
        worker_id: str,
        error_code: str,
        *,
        terminal: bool = False,
    ) -> dict[str, Any] | None:
        job_id = UUID(str(job["id"]))
        failed = self._jobs.record_failure(
            job_id=job_id,
            worker_id=worker_id,
            now=self._clock(),
            error_code=error_code,
            max_attempts=self._max_attempts,
            terminal=terminal,
        )
        if failed is not None:
            self._record_event(
                failed,
                action="usage_export.failed",
                result="failure",
                after={"export_job_id": str(job_id), "reason_code": error_code},
            )
        return failed

    def _recover_exhausted(self, now: datetime) -> None:
        for job in self._jobs.fail_exhausted_jobs(now=now, max_attempts=self._max_attempts):
            self._record_event(
                job,
                action="usage_export.failed",
                result="failure",
                after={
                    "export_job_id": str(job["id"]),
                    "reason_code": "attempts_exhausted",
                },
            )
        self._cleanup_nonready_blobs()

    def _expire_due(self, now: datetime) -> None:
        for job in self._jobs.expire_due_jobs(now=now, limit=NONREADY_BLOB_CLEANUP_BATCH):
            self._record_event(
                job,
                action="usage_export.expired",
                result="success",
                after={"export_job_id": str(job["id"])},
            )
        self._cleanup_nonready_blobs()

    def _cleanup_nonready_blobs(self) -> None:
        for job in self._jobs.list_nonready_blob_jobs(limit=NONREADY_BLOB_CLEANUP_BATCH):
            blob_name = job.get("blob_name")
            if not blob_name:
                continue
            try:
                self._blob.delete(str(blob_name))
            except Exception as error:
                logger.warning(
                    "Could not delete nonready usage export; job=%s error_type=%s",
                    job.get("id"),
                    type(error).__name__,
                )
                continue
            self._jobs.clear_nonready_blob(job_id=UUID(str(job["id"])), blob_name=str(blob_name))

    def _record_event(
        self,
        job: Mapping[str, Any],
        *,
        action: str,
        result: AuditResult,
        after: Mapping[str, Any],
        system_actor: bool = True,
    ) -> None:
        self._audit.record(
            AuditEventWrite(
                action=action,
                result=result,
                resource_type="usage_export_job",
                resource_id=str(job["id"]),
                actor_id=None if system_actor else str(job.get("creator_user_id") or "") or None,
                actor_email="system" if system_actor else job.get("creator_email"),
                actor_role="worker" if system_actor else job.get("creator_role"),
                after=after,
            )
        )

    @staticmethod
    def _usage_filters(snapshot: Mapping[str, Any]) -> UsageFilters:
        values = dict(snapshot)
        if isinstance(values.get("user_id"), list):
            values["user_id"] = tuple(values["user_id"])
        if values.get("runtime"):
            values["runtime"] = tuple(values["runtime"])
        return UsageFilters(**values)

    @staticmethod
    def _blob_name(job: Mapping[str, Any]) -> str:
        return f"usage-exports/{job['tenant_id']}/{job['id']}.zip"


def create_usage_export_worker() -> UsageExportWorker:
    from turnstile_core.config import get_settings
    from turnstile_core.persistence.factory import create_repository

    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    if not settings.usage_export_blob_service_url:
        raise RuntimeError("USAGE_EXPORT_BLOB_SERVICE_URL is not configured")

    repository = create_repository(settings)
    job_store = ExportJobStore(settings.database_url)
    blob = PrivateUsageExportBlob(settings.usage_export_blob_service_url)
    audit_store = AuditStore(settings.database_url)
    return UsageExportWorker(
        job_store,
        repository,
        blob,
        SecurityAuditService(audit_store),
        max_rows=settings.usage_export_max_rows,
        owned_resources=(repository, job_store, blob, audit_store),
    )
