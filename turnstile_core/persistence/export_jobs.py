from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


class ExportJobLimitReached(Exception):
    pass


class ExportJobIdempotencyConflict(Exception):
    pass


class ExportJobStore:
    def __init__(self, database_url: str, pool_min_size: int = 1, pool_max_size: int = 4) -> None:
        self._pool = ConnectionPool(
            database_url,
            min_size=pool_min_size,
            max_size=pool_max_size,
            kwargs={"row_factory": dict_row},
        )

    @staticmethod
    def _tenant_id(connection: Any, user_id: UUID) -> UUID | None:
        row = connection.execute(
            "SELECT tenant_id FROM public.app_user WHERE id = %s AND enabled",
            (user_id,),
        ).fetchone()
        return UUID(str(row["tenant_id"])) if row else None

    @staticmethod
    def _request_hash(from_: datetime, to: datetime, filters: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"from": from_.isoformat(), "to": to.isoformat(), "filters": filters},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def create_job(
        self,
        *,
        creator_user_id: UUID,
        from_: datetime,
        to: datetime,
        filters: dict[str, Any],
        idempotency_key: str,
        max_active_per_user: int,
        retention_days: int,
    ) -> dict[str, Any]:
        request_hash = self._request_hash(from_, to, filters)
        with self._pool.connection() as connection, connection.transaction():
            tenant_id = self._tenant_id(connection, creator_user_id)
            if tenant_id is None:
                raise LookupError("Export job creator is unavailable")
            lock_key = f"usage-export:{tenant_id}:{creator_user_id}"
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
            )
            existing = cast(dict[str, Any] | None, connection.execute(
                """SELECT * FROM public.usage_export_job
                   WHERE tenant_id = %s AND creator_user_id = %s AND idempotency_key = %s
                   FOR UPDATE""",
                (tenant_id, creator_user_id, idempotency_key),
            ).fetchone())
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ExportJobIdempotencyConflict
                return dict(existing)
            active = cast(dict[str, Any] | None, connection.execute(
                """SELECT count(*) AS count FROM public.usage_export_job
                   WHERE tenant_id = %s AND creator_user_id = %s
                     AND status IN ('queued', 'running')""",
                (tenant_id, creator_user_id),
            ).fetchone())
            if active is None:
                raise RuntimeError("Could not count active usage export jobs")
            if int(active["count"]) >= max_active_per_user:
                raise ExportJobLimitReached
            created = cast(dict[str, Any] | None, connection.execute(
                    """INSERT INTO public.usage_export_job (
                           tenant_id, creator_user_id, from_ts, to_ts, filters,
                           expires_at, idempotency_key, request_hash
                       ) VALUES (%s, %s, %s, %s, %s, now() + (%s * interval '1 day'), %s, %s)
                       RETURNING *""",
                    (
                        tenant_id,
                        creator_user_id,
                        from_,
                        to,
                        Jsonb(filters),
                        retention_days,
                        idempotency_key,
                        request_hash,
                    ),
                ).fetchone())
            if created is None:
                raise RuntimeError("Could not create usage export job")
            return created

    def list_jobs(
        self, *, viewer_user_id: UUID, can_read_all: bool, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            tenant_id = self._tenant_id(connection, viewer_user_id)
            if tenant_id is None:
                return []
            rows = connection.execute(
                """SELECT * FROM public.usage_export_job
                   WHERE tenant_id = %s AND (%s OR creator_user_id = %s)
                   ORDER BY created_at DESC, id DESC LIMIT %s""",
                (tenant_id, can_read_all, viewer_user_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_job(
        self, *, job_id: UUID, viewer_user_id: UUID, can_read_all: bool
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            tenant_id = self._tenant_id(connection, viewer_user_id)
            if tenant_id is None:
                return None
            row = connection.execute(
                """SELECT * FROM public.usage_export_job
                   WHERE id = %s AND tenant_id = %s
                     AND (%s OR creator_user_id = %s)""",
                (job_id, tenant_id, can_read_all, viewer_user_id),
            ).fetchone()
            return dict(row) if row else None

    def claim_next_job(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        max_attempts: int,
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """WITH candidate AS (
                       SELECT id FROM public.usage_export_job
                       WHERE expires_at > %s AND attempt_count < %s
                         AND (status = 'queued'
                           OR (status = 'running' AND lease_until <= %s))
                       ORDER BY created_at, id
                       LIMIT 1
                       FOR UPDATE SKIP LOCKED
                   )
                   UPDATE public.usage_export_job AS job
                   SET status = 'running', attempt_count = job.attempt_count + 1,
                       lease_owner = %s,
                       lease_until = %s + (%s * interval '1 second'),
                       updated_at = %s, error_code = NULL
                   FROM candidate
                   WHERE job.id = candidate.id
                   RETURNING job.*,
                       (SELECT email FROM public.app_user WHERE id = job.creator_user_id)
                           AS creator_email,
                       (SELECT role FROM public.app_user WHERE id = job.creator_user_id)
                           AS creator_role""",
                (now, max_attempts, now, worker_id, now, lease_seconds, now),
            ).fetchone()
            return dict(row) if row else None

    def fail_exhausted_jobs(
        self, *, now: datetime, max_attempts: int
    ) -> list[dict[str, Any]]:
        with self._pool.connection() as connection, connection.transaction():
            rows = connection.execute(
                """WITH exhausted AS (
                       SELECT id FROM public.usage_export_job
                       WHERE status = 'running' AND lease_until <= %s
                         AND attempt_count >= %s
                       ORDER BY lease_until, id
                       LIMIT 100
                       FOR UPDATE SKIP LOCKED
                   )
                   UPDATE public.usage_export_job AS job
                   SET status = 'failed', lease_owner = NULL, lease_until = NULL,
                       error_code = 'attempts_exhausted', updated_at = %s
                   FROM exhausted
                   WHERE job.id = exhausted.id
                   RETURNING job.*,
                       (SELECT email FROM public.app_user WHERE id = job.creator_user_id)
                           AS creator_email,
                       (SELECT role FROM public.app_user WHERE id = job.creator_user_id)
                           AS creator_role""",
                (now, max_attempts, now),
            ).fetchall()
            return [dict(row) for row in rows]

    def expire_due_jobs(self, *, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
        with self._pool.connection() as connection, connection.transaction():
            rows = connection.execute(
                """WITH due AS (
                       SELECT id FROM public.usage_export_job
                       WHERE status <> 'expired' AND expires_at <= %s
                       ORDER BY expires_at, id
                       LIMIT %s
                       FOR UPDATE SKIP LOCKED
                   )
                   UPDATE public.usage_export_job AS job
                   SET status = 'expired', lease_owner = NULL, lease_until = NULL,
                       updated_at = %s
                   FROM due
                   WHERE job.id = due.id
                   RETURNING job.*,
                       (SELECT email FROM public.app_user WHERE id = job.creator_user_id)
                           AS creator_email,
                       (SELECT role FROM public.app_user WHERE id = job.creator_user_id)
                           AS creator_role""",
                (now, limit, now),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_nonready_blob_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """SELECT id, tenant_id, blob_name
                   FROM public.usage_export_job
                   WHERE status IN ('failed', 'expired') AND blob_name IS NOT NULL
                   ORDER BY updated_at, id LIMIT %s""",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def clear_nonready_blob(self, *, job_id: UUID, blob_name: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """UPDATE public.usage_export_job
                   SET blob_name = NULL, updated_at = now()
                   WHERE id = %s AND blob_name = %s AND status IN ('failed', 'expired')""",
                (job_id, blob_name),
            )

    def set_blob_name(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        blob_name: str,
    ) -> bool:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """UPDATE public.usage_export_job
                   SET blob_name = %s, updated_at = %s
                   WHERE id = %s AND status = 'running' AND lease_owner = %s
                     AND lease_until > %s AND expires_at > %s""",
                (blob_name, now, job_id, worker_id, now, now),
            )
            return cursor.rowcount == 1

    def renew_lease(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """UPDATE public.usage_export_job
                   SET lease_until = %s + (%s * interval '1 second'), updated_at = %s
                   WHERE id = %s AND status = 'running' AND lease_owner = %s
                     AND lease_until > %s AND expires_at > %s""",
                (now, lease_seconds, now, job_id, worker_id, now, now),
            )
            return cursor.rowcount == 1

    def mark_ready(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        row_count: int,
        blob_name: str,
    ) -> bool:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """UPDATE public.usage_export_job
                   SET status = 'ready', row_count = %s, blob_name = %s,
                       lease_owner = NULL, lease_until = NULL, error_code = NULL,
                       updated_at = %s
                   WHERE id = %s AND status = 'running' AND lease_owner = %s
                     AND lease_until > %s AND expires_at > %s""",
                (row_count, blob_name, now, job_id, worker_id, now, now),
            )
            return cursor.rowcount == 1

    def record_failure(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        error_code: str,
        max_attempts: int,
        terminal: bool = False,
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE public.usage_export_job AS job
                   SET status = CASE WHEN %s OR attempt_count >= %s THEN 'failed'
                                     ELSE 'queued' END,
                       lease_owner = NULL, lease_until = NULL, error_code = %s,
                       updated_at = %s
                   WHERE id = %s AND status = 'running' AND lease_owner = %s
                     AND lease_until > %s AND expires_at > %s
                   RETURNING job.*,
                       (SELECT email FROM public.app_user WHERE id = job.creator_user_id)
                           AS creator_email,
                       (SELECT role FROM public.app_user WHERE id = job.creator_user_id)
                           AS creator_role""",
                (
                    terminal,
                    max_attempts,
                    error_code,
                    now,
                    job_id,
                    worker_id,
                    now,
                    now,
                ),
            ).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        self._pool.close()
