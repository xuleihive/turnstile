from __future__ import annotations

import csv
import io
import threading
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zipfile import ZipFile

import pytest

from turnstile_core.integrations.usage_export_blob import PrivateUsageExportBlob
from turnstile_core.services.usage_export_worker import UsageExportWorker

NOW = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
CREATOR_ID = UUID("00000000-0000-4000-8000-000000000001")


def _job(**overrides: object) -> dict[str, object]:
    return {
        "id": UUID("20000000-0000-4000-8000-000000000001"),
        "tenant_id": TENANT_ID,
        "creator_user_id": CREATOR_ID,
        "creator_email": "analyst@example.com",
        "creator_role": "member",
        "from_ts": datetime(2026, 7, 1, tzinfo=UTC),
        "to_ts": datetime(2026, 8, 1, tzinfo=UTC),
        "filters": {"organization_id": "org-contoso-global"},
        "status": "queued",
        "attempt_count": 0,
        "lease_owner": None,
        "lease_until": None,
        "row_count": None,
        "blob_name": None,
        "expires_at": NOW + timedelta(days=7),
        "error_code": None,
        "created_at": NOW,
        "updated_at": NOW,
        **overrides,
    }


class MemoryExportJobStore:
    """Atomic in-memory lease store for testing worker behavior without PostgreSQL."""

    def __init__(self, *jobs: dict[str, object]) -> None:
        self.jobs = {job["id"]: dict(job) for job in jobs}
        self.lock = threading.Lock()
        self.lose_renewal = False
        self.crash_before_ready = False

    def fail_exhausted_jobs(self, *, now: datetime, max_attempts: int) -> list[dict[str, object]]:
        failed: list[dict[str, object]] = []
        with self.lock:
            for job in self.jobs.values():
                if (
                    job["status"] == "running"
                    and job["lease_until"] is not None
                    and job["lease_until"] <= now
                    and job["attempt_count"] >= max_attempts
                ):
                    job.update(status="failed", lease_owner=None, lease_until=None)
                    job["error_code"] = "attempts_exhausted"
                    failed.append(dict(job))
        return failed

    def expire_due_jobs(self, *, now: datetime, limit: int = 100) -> list[dict[str, object]]:
        expired: list[dict[str, object]] = []
        with self.lock:
            for job in self.jobs.values():
                if job["status"] != "expired" and job["expires_at"] <= now:
                    job.update(status="expired", lease_owner=None, lease_until=None)
                    expired.append(dict(job))
                    if len(expired) >= limit:
                        break
        return expired

    def list_nonready_blob_jobs(self, *, limit: int = 100) -> list[dict[str, object]]:
        with self.lock:
            return [
                dict(job)
                for job in self.jobs.values()
                if job["status"] in {"failed", "expired"} and job["blob_name"]
            ][:limit]

    def clear_nonready_blob(self, *, job_id: UUID, blob_name: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            if job["status"] in {"failed", "expired"} and job["blob_name"] == blob_name:
                job["blob_name"] = None

    def set_blob_name(
        self, *, job_id: UUID, worker_id: str, now: datetime, blob_name: str
    ) -> bool:
        with self.lock:
            job = self.jobs[job_id]
            if not self._owns_live_lease(job, worker_id, now):
                return False
            job["blob_name"] = blob_name
            return True

    def claim_next_job(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        max_attempts: int,
    ) -> dict[str, object] | None:
        with self.lock:
            candidates = sorted(self.jobs.values(), key=lambda row: row["created_at"])
            for job in candidates:
                is_queued = job["status"] == "queued"
                is_reclaimable = (
                    job["status"] == "running"
                    and job["lease_until"] is not None
                    and job["lease_until"] <= now
                )
                if (
                    (is_queued or is_reclaimable)
                    and job["expires_at"] > now
                    and job["attempt_count"] < max_attempts
                ):
                    job.update(
                        status="running",
                        attempt_count=job["attempt_count"] + 1,
                        lease_owner=worker_id,
                        lease_until=now + timedelta(seconds=lease_seconds),
                    )
                    return dict(job)
        return None

    def renew_lease(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        with self.lock:
            job = self.jobs[job_id]
            if self.lose_renewal or not self._owns_live_lease(job, worker_id, now):
                return False
            job["lease_until"] = now + timedelta(seconds=lease_seconds)
            return True

    def mark_ready(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        row_count: int,
        blob_name: str,
    ) -> bool:
        with self.lock:
            if self.crash_before_ready:
                self.crash_before_ready = False
                raise SimulatedWorkerCrash
            job = self.jobs[job_id]
            if not self._owns_live_lease(job, worker_id, now):
                return False
            job.update(
                status="ready",
                row_count=row_count,
                blob_name=blob_name,
                lease_owner=None,
                lease_until=None,
            )
            return True

    def record_failure(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        error_code: str,
        max_attempts: int,
        terminal: bool = False,
    ) -> dict[str, object] | None:
        with self.lock:
            job = self.jobs[job_id]
            if not self._owns_live_lease(job, worker_id, now):
                return None
            job.update(
                status=("failed" if terminal or job["attempt_count"] >= max_attempts else "queued"),
                lease_owner=None,
                lease_until=None,
                error_code=error_code,
            )
            return dict(job)

    @staticmethod
    def _owns_live_lease(job: dict[str, object], worker_id: str, now: datetime) -> bool:
        return bool(
            job["status"] == "running"
            and job["lease_owner"] == worker_id
            and job["lease_until"] is not None
            and job["lease_until"] > now
        )


class MemoryBlobStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.upload_calls: list[str] = []
        self.delete_calls: list[str] = []

    def upload(self, name: str, body: io.BufferedIOBase) -> None:
        self.objects[name] = body.read()
        self.upload_calls.append(name)

    def delete(self, name: str) -> None:
        self.delete_calls.append(name)
        self.objects.pop(name, None)


class MemoryAudit:
    def __init__(self) -> None:
        self.events: list[object] = []

    def record(self, event: object) -> None:
        self.events.append(event)


class RowsRepository:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []
        self.fail_report = False
        self.entered_report: threading.Event | None = None
        self.release_report: threading.Event | None = None

    def executive_overview(
        self, from_: datetime, to: datetime, filters: object
    ) -> dict[str, object]:
        del from_, to, filters
        if self.entered_report:
            self.entered_report.set()
            assert self.release_report is not None
            self.release_report.wait(timeout=5)
        if self.fail_report:
            raise RuntimeError("private database detail must not escape")
        return {
            "generated_at": NOW,
            "totals": {
                "total_requests": len(self.rows),
                "total_tokens": 22,
                "estimated_cost": 0.002,
                "average_latency_ms": 10,
                "p50_latency_ms": 10,
                "p95_latency_ms": 10,
                "p99_latency_ms": 10,
                "error_rate": 0,
                "success_requests": len(self.rows),
                "client_error_requests": 0,
                "server_error_requests": 0,
            },
            "changes_percent": {},
        }

    def distribution(
        self, from_: datetime, to: datetime, dimension: str, filters: object, limit: int
    ) -> list[dict[str, object]]:
        del from_, to, dimension, filters, limit
        return []

    def iter_usage_requests(
        self, from_: datetime, to: datetime, filters: object
    ):
        del from_, to, filters
        yield from self.rows


class SimulatedWorkerCrash(BaseException):
    pass


def _worker(
    store: MemoryExportJobStore,
    repository: RowsRepository,
    blob: MemoryBlobStore,
    audit: MemoryAudit,
    *,
    max_rows: int = 1_000_000,
    now: datetime = NOW,
) -> UsageExportWorker:
    return UsageExportWorker(
        store,
        repository,
        blob,
        audit,
        max_rows=max_rows,
        clock=lambda: now,
    )


def _row(request_id: str = "request-1", user_name: str = "Analyst") -> dict[str, object]:
    return {
        "usage_id": f"usage-{request_id}",
        "request_id": request_id,
        "correlation_id": f"correlation-{request_id}",
        "timestamp": datetime(2026, 7, 20, tzinfo=UTC),
        "user_name": user_name,
        "input_tokens": 12,
        "output_tokens": 10,
        "total_tokens": 22,
        "status_code": 200,
    }


def test_success_uploads_analysis_and_csv_archive_and_marks_job_ready() -> None:
    store = MemoryExportJobStore(_job())
    repository = RowsRepository([_row("request-1"), _row("request-2")])
    blob = MemoryBlobStore()
    audit = MemoryAudit()

    processed = _worker(store, repository, blob, audit).run_once("worker-a", NOW)

    job = store.jobs[UUID("20000000-0000-4000-8000-000000000001")]
    assert processed == 1
    assert job["status"] == "ready"
    assert job["row_count"] == 2
    assert job["blob_name"] == f"usage-exports/{TENANT_ID}/{job['id']}.zip"
    assert list(blob.objects) == [job["blob_name"]]
    with ZipFile(io.BytesIO(blob.objects[str(job["blob_name"])])) as archive:
        assert set(archive.namelist()) == {"analysis.md", "requests.csv"}
        assert "Turnstile 用量与费用分析" in archive.read("analysis.md").decode("utf-8")
        csv_text = archive.read("requests.csv").decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert [row["request_id"] for row in rows] == ["request-1", "request-2"]
    assert len(audit.events) == 1
    assert audit.events[0].actor_role == "worker"
    assert audit.events[0].actor_id is None


def test_csv_formula_values_with_leading_whitespace_are_written_as_text() -> None:
    payload = " \t=HYPERLINK(\"https://example.invalid\")"
    store = MemoryExportJobStore(_job())
    blob = MemoryBlobStore()
    _worker(store, RowsRepository([_row(user_name=payload)]), blob, MemoryAudit()).run_once(
        "worker-a", NOW
    )
    name = next(iter(blob.objects))
    with ZipFile(io.BytesIO(blob.objects[name])) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read("requests.csv").decode("utf-8-sig"))))

    assert rows[0]["user_name"] == "'" + payload


def test_two_workers_cannot_process_the_same_live_job() -> None:
    job = _job()
    store = MemoryExportJobStore(job)
    repository = RowsRepository([_row()])
    repository.entered_report = threading.Event()
    repository.release_report = threading.Event()
    blob = MemoryBlobStore()
    audit = MemoryAudit()
    first_worker = _worker(store, repository, blob, audit)
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(first_worker.run_once("worker-a", NOW)))
    thread.start()
    assert repository.entered_report is not None
    assert repository.entered_report.wait(timeout=5)

    second_result = _worker(store, RowsRepository([_row()]), blob, audit).run_once(
        "worker-b", NOW
    )
    assert second_result == 0
    assert repository.release_report is not None
    repository.release_report.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result == [1]
    assert store.jobs[job["id"]]["attempt_count"] == 1
    assert len(blob.upload_calls) == 1


def test_lost_lease_after_a_thousand_rows_stops_before_blob_upload() -> None:
    store = MemoryExportJobStore(_job())
    store.lose_renewal = True
    repository = RowsRepository([_row(f"request-{index}") for index in range(1001)])
    blob = MemoryBlobStore()

    _worker(store, repository, blob, MemoryAudit()).run_once("worker-a", NOW)

    assert blob.upload_calls == []
    assert blob.objects == {}
    assert store.jobs[UUID("20000000-0000-4000-8000-000000000001")]["status"] == "running"


def test_expired_lease_can_be_reclaimed_and_retried() -> None:
    job = _job(
        status="running",
        attempt_count=1,
        lease_owner="worker-that-crashed",
        lease_until=NOW - timedelta(seconds=1),
    )
    store = MemoryExportJobStore(job)
    blob = MemoryBlobStore()

    _worker(store, RowsRepository([_row()]), blob, MemoryAudit()).run_once("worker-b", NOW)

    assert store.jobs[job["id"]]["status"] == "ready"
    assert store.jobs[job["id"]]["attempt_count"] == 2


def test_uploaded_archive_before_crash_is_safely_overwritten_after_lease_expiry() -> None:
    job = _job()
    store = MemoryExportJobStore(job)
    store.crash_before_ready = True
    blob = MemoryBlobStore()
    first_worker = _worker(store, RowsRepository([_row()]), blob, MemoryAudit())

    with pytest.raises(SimulatedWorkerCrash):
        first_worker.run_once("worker-a", NOW)
    previous_bytes = next(iter(blob.objects.values()))
    assert store.jobs[job["id"]]["status"] == "running"

    retry_time = NOW + timedelta(minutes=11)
    retry_worker = _worker(
        store, RowsRepository([_row()]), blob, MemoryAudit(), now=retry_time
    )
    retry_worker.run_once("worker-b", retry_time)

    assert store.jobs[job["id"]]["status"] == "ready"
    assert len(blob.objects) == 1
    assert blob.upload_calls == [next(iter(blob.objects)), next(iter(blob.objects))]
    assert next(iter(blob.objects.values())) == previous_bytes


def test_three_failed_attempts_end_in_sanitized_terminal_failure() -> None:
    job = _job()
    store = MemoryExportJobStore(job)
    repository = RowsRepository()
    repository.fail_report = True
    audit = MemoryAudit()
    worker = _worker(store, repository, MemoryBlobStore(), audit)

    for attempt in range(3):
        worker.run_once("worker-a", NOW + timedelta(minutes=attempt))

    saved = store.jobs[job["id"]]
    assert saved["status"] == "failed"
    assert saved["attempt_count"] == 3
    assert saved["error_code"] == "export_failed"
    assert "private database detail" not in str(saved)
    assert len(audit.events) == 3


def test_expired_ready_job_deletes_blob_and_remains_queryable_as_expired() -> None:
    name = f"usage-exports/{TENANT_ID}/20000000-0000-4000-8000-000000000001.zip"
    job = _job(
        status="ready",
        row_count=3,
        blob_name=name,
        expires_at=NOW - timedelta(seconds=1),
    )
    store = MemoryExportJobStore(job)
    blob = MemoryBlobStore()
    blob.objects[name] = b"private report"
    audit = MemoryAudit()

    _worker(store, RowsRepository(), blob, audit).run_once("worker-a", NOW)

    assert store.jobs[job["id"]]["status"] == "expired"
    assert store.jobs[job["id"]]["blob_name"] is None
    assert name not in blob.objects
    assert len(audit.events) == 1


def test_row_limit_failure_does_not_upload_a_partial_archive() -> None:
    job = _job()
    store = MemoryExportJobStore(job)
    blob = MemoryBlobStore()

    _worker(
        store,
        RowsRepository([_row(f"request-{index}") for index in range(3)]),
        blob,
        MemoryAudit(),
        max_rows=2,
    ).run_once("worker-a", NOW)

    assert store.jobs[job["id"]]["status"] == "failed"
    assert store.jobs[job["id"]]["error_code"] == "row_limit_exceeded"
    assert blob.objects == {}
    assert blob.upload_calls == []


def test_blob_adapter_uses_the_private_container_and_overwrites_job_blob() -> None:
    class Container:
        def __init__(self) -> None:
            self.uploaded: list[tuple[str, bytes, bool]] = []
            self.deleted: list[str] = []

        def upload_blob(self, name: str, body: io.BufferedIOBase, *, overwrite: bool) -> None:
            self.uploaded.append((name, body.read(), overwrite))

        def delete_blob(self, name: str) -> None:
            self.deleted.append(name)

    class Service:
        def __init__(self) -> None:
            self.container_name: str | None = None
            self.container = Container()

        def get_container_client(self, container_name: str) -> Container:
            self.container_name = container_name
            return self.container

    service = Service()
    blob = PrivateUsageExportBlob(
        "https://storage.example.invalid", blob_service_client=service
    )
    body = io.BytesIO(b"private")

    blob.upload("usage-exports/job.zip", body)
    blob.delete("usage-exports/job.zip")

    assert service.container_name == "turnstile-usage-exports"
    assert service.container.uploaded == [("usage-exports/job.zip", b"private", True)]
    assert service.container.deleted == ["usage-exports/job.zip"]


def test_blob_adapter_downloads_chunks_with_sdk_supported_arguments() -> None:
    class Downloader:
        def chunks(self):
            yield b"part-1"
            yield b"part-2"

    class Container:
        def __init__(self) -> None:
            self.download_args: tuple[str, dict[str, object]] | None = None

        def download_blob(self, name: str, **kwargs: object) -> Downloader:
            self.download_args = (name, kwargs)
            return Downloader()

    class Service:
        def __init__(self) -> None:
            self.container = Container()

        def get_container_client(self, _container_name: str) -> Container:
            return self.container

    service = Service()
    blob = PrivateUsageExportBlob(
        "https://storage.example.invalid", blob_service_client=service
    )

    chunks = list(blob.download_chunks("usage-exports/job.zip", chunk_size=1024))

    assert chunks == [b"part-1", b"part-2"]
    assert service.container.download_args == (
        "usage-exports/job.zip",
        {"max_concurrency": 1, "max_chunk_get_size": 1024},
    )
