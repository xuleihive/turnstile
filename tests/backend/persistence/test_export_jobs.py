from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.persistence import export_jobs as export_jobs_module
from turnstile_core.persistence.export_jobs import (
    ExportJobIdempotencyConflict,
    ExportJobLimitReached,
    ExportJobStore,
)

TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
USER_ID = UUID("00000000-0000-4000-8000-000000000001")
FROM = datetime(2026, 7, 1, tzinfo=UTC)
TO = datetime(2026, 8, 1, tzinfo=UTC)
FILTERS = {"organization_id": "org-contoso"}


class FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self.row


class FakeConnection:
    def __init__(self, *, active_count: int = 0, existing: dict[str, Any] | None = None) -> None:
        self.active_count = active_count
        self.existing = existing
        self.claim_row: dict[str, Any] | None = None
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        self.statements.append((query, params))
        if "SKIP LOCKED" in query:
            return FakeCursor(self.claim_row)
        if "SELECT tenant_id FROM public.app_user" in query:
            return FakeCursor({"tenant_id": TENANT_ID})
        if "idempotency_key = %s" in query:
            return FakeCursor(self.existing)
        if "SELECT count(*) AS count" in query:
            return FakeCursor({"count": self.active_count})
        if "INSERT INTO public.usage_export_job" in query:
            now = datetime.now(UTC)
            return FakeCursor(
                {
                    "id": UUID("20000000-0000-4000-8000-000000000001"),
                    "tenant_id": TENANT_ID,
                    "creator_user_id": USER_ID,
                    "from_ts": FROM,
                    "to_ts": TO,
                    "filters": FILTERS,
                    "status": "queued",
                    "attempt_count": 0,
                    "lease_owner": None,
                    "lease_until": None,
                    "row_count": None,
                    "blob_name": None,
                    "expires_at": now + timedelta(days=7),
                    "error_code": None,
                    "idempotency_key": "retry-001",
                    "request_hash": ExportJobStore._request_hash(FROM, TO, FILTERS),
                    "created_at": now,
                    "updated_at": now,
                }
            )
        return FakeCursor(None)


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection_value = connection

    @contextmanager
    def connection(self) -> Iterator[FakeConnection]:
        yield self.connection_value

    def close(self) -> None:
        pass


def _store(monkeypatch: pytest.MonkeyPatch, connection: FakeConnection) -> ExportJobStore:
    pool = FakePool(connection)
    monkeypatch.setattr(
        export_jobs_module,
        "ConnectionPool",
        lambda *_args, **_kwargs: pool,
    )
    return ExportJobStore("postgresql://unused")


def test_usage_export_job_migration_keeps_idempotency_with_creator_scope() -> None:
    path = REPOSITORY_ROOT / "migrations/011_usage_export_jobs.up.sql"
    assert path.is_file(), "the export job schema must be versioned after migration 010"
    sql = path.read_text(encoding="utf-8").lower()

    assert "usage_export_job_idempotency_idx" in sql
    assert "on public.usage_export_job (tenant_id, creator_user_id, idempotency_key)" in sql
    assert "status in ('queued', 'running', 'ready', 'failed', 'expired')" in sql


def test_usage_export_limits_have_bounded_defaults() -> None:
    settings = Settings()

    assert settings.usage_export_max_range_days == 366
    assert settings.usage_export_max_rows == 1_000_000
    assert settings.usage_export_max_active_per_user == 2
    assert settings.usage_export_retention_days == 7


def test_create_job_serializes_user_limit_check_and_persists_filter_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = _store(monkeypatch, connection)

    job = store.create_job(
        creator_user_id=USER_ID,
        from_=FROM,
        to=TO,
        filters=FILTERS,
        idempotency_key="retry-001",
        max_active_per_user=2,
        retention_days=7,
    )

    queries = [query for query, _ in connection.statements]
    assert "pg_advisory_xact_lock" in queries[1]
    assert "FOR UPDATE" in queries[2]
    assert "status IN ('queued', 'running')" in queries[3]
    assert "INSERT INTO public.usage_export_job" in queries[4]
    inserted = connection.statements[4][1]
    assert inserted[0:2] == (TENANT_ID, USER_ID)
    assert inserted[4].obj == FILTERS
    assert job["status"] == "queued"


def test_create_job_enforces_active_limit_while_holding_user_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(active_count=2)
    store = _store(monkeypatch, connection)

    with pytest.raises(ExportJobLimitReached):
        store.create_job(
            creator_user_id=USER_ID,
            from_=FROM,
            to=TO,
            filters=FILTERS,
            idempotency_key="retry-001",
            max_active_per_user=2,
            retention_days=7,
        )

    queries = [query for query, _ in connection.statements]
    advisory_index = next(
        index for index, query in enumerate(queries) if "pg_advisory" in query
    )
    count_index = next(
        index for index, query in enumerate(queries) if "SELECT count(*)" in query
    )
    assert advisory_index < count_index
    assert not any("INSERT INTO" in query for query in queries)


def test_create_job_returns_matching_idempotent_row_without_counting_active_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_hash = ExportJobStore._request_hash(FROM, TO, FILTERS)
    existing = {"id": uuid4(), "request_hash": request_hash, "status": "queued"}
    connection = FakeConnection(existing=existing)
    store = _store(monkeypatch, connection)

    result = store.create_job(
        creator_user_id=USER_ID,
        from_=FROM,
        to=TO,
        filters=FILTERS,
        idempotency_key="retry-001",
        max_active_per_user=2,
        retention_days=7,
    )

    assert result == existing
    assert not any("SELECT count(*)" in query for query, _ in connection.statements)
    assert not any("INSERT INTO" in query for query, _ in connection.statements)


def test_create_job_rejects_reusing_key_for_a_different_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(existing={"request_hash": "different"})
    store = _store(monkeypatch, connection)

    with pytest.raises(ExportJobIdempotencyConflict):
        store.create_job(
            creator_user_id=USER_ID,
            from_=FROM,
            to=TO,
            filters=FILTERS,
            idempotency_key="retry-001",
            max_active_per_user=2,
            retention_days=7,
        )


def test_worker_claim_uses_postgres_skip_locked_and_live_job_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    connection.claim_row = {
        "id": UUID("20000000-0000-4000-8000-000000000001"),
        "status": "running",
        "attempt_count": 1,
        "lease_owner": "worker-01",
    }
    store = _store(monkeypatch, connection)

    claimed = store.claim_next_job(
        worker_id="worker-01",
        now=datetime(2026, 10, 12, tzinfo=UTC),
        lease_seconds=600,
        max_attempts=3,
    )

    claim_query = next(query for query, _ in connection.statements if "SKIP LOCKED" in query)
    claim_parameters = next(
        params for query, params in connection.statements if "SKIP LOCKED" in query
    )
    assert claimed is not None
    assert claimed["lease_owner"] == "worker-01"
    assert "FOR UPDATE" in claim_query
    assert "SKIP LOCKED" in claim_query
    assert "attempt_count" in claim_query
    assert "expires_at" in claim_query
    assert "lease_until" in claim_query
    assert "worker-01" in claim_parameters
    assert 600 in claim_parameters
