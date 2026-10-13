from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api import app
from backend.http import observability
from backend.http.dependencies import get_repository
from backend.http.observability import get_usage_export_job_store
from backend.http.security_audit_support import get_optional_security_audit_service
from tests.platform.api.api_support import MEMBER_SESSION, OWNER_SESSION, client

pytest_plugins = ("tests.platform.api.api_fixtures",)


TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
OWNER_ID = UUID("00000000-0000-4000-8000-000000000001")
MEMBER_ID = UUID("00000000-0000-4000-8000-000000000002")


class InMemoryExportJobStore:
    def __init__(self) -> None:
        self.jobs: dict[UUID, dict[str, object]] = {}
        self.keys: dict[tuple[UUID, str], tuple[str, UUID]] = {}

    def create_job(
        self,
        *,
        creator_user_id: UUID,
        from_: datetime,
        to: datetime,
        filters: dict[str, object],
        idempotency_key: str,
        max_active_per_user: int,
        retention_days: int,
    ) -> dict[str, object]:
        serialized = json.dumps(
            {"from": from_.isoformat(), "to": to.isoformat(), "filters": filters},
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(serialized.encode()).hexdigest()
        key = (creator_user_id, idempotency_key)
        if key in self.keys:
            existing_hash, job_id = self.keys[key]
            if existing_hash != request_hash:
                from turnstile_core.persistence.export_jobs import ExportJobIdempotencyConflict

                raise ExportJobIdempotencyConflict
            return self.jobs[job_id]
        active = sum(
            1
            for job in self.jobs.values()
            if job["creator_user_id"] == creator_user_id
            and job["status"] in {"queued", "running"}
        )
        if active >= max_active_per_user:
            from turnstile_core.persistence.export_jobs import ExportJobLimitReached

            raise ExportJobLimitReached
        now = datetime.now(UTC)
        job_id = uuid4()
        row: dict[str, object] = {
            "id": job_id,
            "tenant_id": TENANT_ID,
            "creator_user_id": creator_user_id,
            "from_ts": from_,
            "to_ts": to,
            "filters": filters,
            "status": "queued",
            "attempt_count": 0,
            "lease_owner": None,
            "lease_until": None,
            "row_count": None,
            "blob_name": None,
            "expires_at": now + timedelta(days=retention_days),
            "error_code": None,
            "created_at": now,
            "updated_at": now,
        }
        self.jobs[job_id] = row
        self.keys[key] = (request_hash, job_id)
        return row

    def list_jobs(
        self, *, viewer_user_id: UUID, can_read_all: bool, limit: int = 50
    ) -> list[dict[str, object]]:
        rows = [
            row for row in self.jobs.values()
            if can_read_all or row["creator_user_id"] == viewer_user_id
        ]
        return list(reversed(rows))[:limit]

    def get_job(
        self, *, job_id: UUID, viewer_user_id: UUID, can_read_all: bool
    ) -> dict[str, object] | None:
        row = self.jobs.get(job_id)
        if row is None or (not can_read_all and row["creator_user_id"] != viewer_user_id):
            return None
        return row


class CapturingAuditService:
    def __init__(self) -> None:
        self.events: list[object] = []

    def record(self, event: object, *, connection: object | None = None) -> None:
        del connection
        self.events.append(event)


@pytest.fixture
def export_store() -> InMemoryExportJobStore:
    store = InMemoryExportJobStore()
    app.dependency_overrides[get_usage_export_job_store] = lambda: store
    yield store
    app.dependency_overrides.pop(get_usage_export_job_store, None)


def _create_job(
    idempotency_key: str,
    *,
    from_: str = "2026-07-01T00:00:00Z",
    to: str = "2026-08-01T00:00:00Z",
    filters: dict[str, object] | None = None,
    client_: TestClient = client,
) -> object:
    return client_.post(
        "/api/v1/observability/export-jobs",
        headers={"Idempotency-Key": idempotency_key},
        json={"from": from_, "to": to, "filters": filters or {}},
    )


def test_create_usage_export_job_returns_accepted_status_resource(
    export_store: InMemoryExportJobStore,
) -> None:
    response = _create_job(
        "browser-retry-001", filters={"organization_id": "org-contoso-global"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["id"]
    assert body["status_url"].endswith(body["id"])
    assert response.headers["location"] == body["status_url"]
    assert response.headers["cache-control"] == "private, no-store"
    row = next(iter(export_store.jobs.values()))
    assert row["filters"]["organization_id"] == "org-contoso-global"


def test_export_job_creation_audits_only_job_metadata(
    export_store: InMemoryExportJobStore,
) -> None:
    del export_store
    audit = CapturingAuditService()
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit
    try:
        response = _create_job("audit-create")
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)

    assert response.status_code == 202
    event = audit.events[0]
    assert event.action == "usage_export.created"
    assert event.resource_id == response.json()["id"]
    assert event.after == {
        "job_id": response.json()["id"],
        "status": "queued",
        "row_count": None,
    }


def test_export_job_range_is_aware_ordered_and_limited_to_366_days(
    export_store: InMemoryExportJobStore,
) -> None:
    reversed_range = _create_job(
        "reversed", from_="2026-08-01T00:00:00Z", to="2026-07-01T00:00:00Z"
    )
    naive_range = _create_job("naive", from_="2026-07-01T00:00:00", to="2026-08-01T00:00:00")
    too_long = _create_job(
        "too-long", from_="2025-06-29T00:00:00Z", to="2026-07-01T00:00:00Z"
    )

    assert reversed_range.status_code == 422
    assert naive_range.status_code == 422
    assert too_long.status_code == 422
    assert export_store.jobs == {}

    exactly_max = _create_job(
        "max-range", from_="2025-07-01T00:00:00Z", to="2026-07-02T00:00:00Z"
    )
    assert exactly_max.status_code == 202


def test_export_job_is_queued_without_a_synchronous_row_count_query(
    export_store: InMemoryExportJobStore,
) -> None:
    class OverLimitRepository:
        def __init__(self) -> None:
            self.overview_calls = 0

        def executive_overview(self, *_args: object) -> dict[str, object]:
            self.overview_calls += 1
            return {"totals": {"total_requests": 1_000_001}}

    repository = OverLimitRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        response = _create_job("too-many-rows")
    finally:
        app.dependency_overrides.pop(get_repository, None)

    assert response.status_code == 202
    assert repository.overview_calls == 0
    assert len(export_store.jobs) == 1
    assert next(iter(export_store.jobs.values()))["status"] == "queued"


def test_export_job_requires_an_idempotency_key(
    export_store: InMemoryExportJobStore,
) -> None:
    response = client.post(
        "/api/v1/observability/export-jobs",
        json={"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"},
    )

    assert response.status_code == 422
    assert export_store.jobs == {}


def test_member_export_scope_is_forced_to_the_signed_in_user(
    export_store: InMemoryExportJobStore,
) -> None:
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        response = _create_job("member-own")
    finally:
        client.cookies.set("turnstile_session", OWNER_SESSION)

    assert response.status_code == 202
    row = next(iter(export_store.jobs.values()))
    assert row["creator_user_id"] == MEMBER_ID
    assert row["filters"]["user_id"] == [
        str(MEMBER_ID),
        "member@contoso.com",
    ]


def test_export_job_response_includes_the_authorized_filter_snapshot(
    export_store: InMemoryExportJobStore,
) -> None:
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        response = _create_job(
            "member-snapshot",
            filters={
                "organization_id": "org-contoso-global",
                "department_id": "department-platform",
                "runtime": ["foundry", "apim"],
                "status_code": 429,
            },
        )
    finally:
        client.cookies.set("turnstile_session", OWNER_SESSION)

    assert response.status_code == 202
    assert response.json()["filters"] == {
        "organization_id": "org-contoso-global",
        "department_id": "department-platform",
        "project_id": None,
        "agent_id": None,
        "model_id": None,
        "user_id": [str(MEMBER_ID), "member@contoso.com"],
        "runtime": ["foundry", "apim"],
        "status_code": 429,
    }


def test_member_cannot_choose_another_user_or_read_another_users_job(
    export_store: InMemoryExportJobStore,
) -> None:
    owner_job = _create_job("owner-job")
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        cross_user_filter = _create_job(
            "member-cross-filter", filters={"user_id": "owner@contoso.com"}
        )
        read_other_job = client.get(
            f"/api/v1/observability/export-jobs/{owner_job.json()['id']}"
        )
    finally:
        client.cookies.set("turnstile_session", OWNER_SESSION)

    assert cross_user_filter.status_code == 403
    assert read_other_job.status_code == 404
    assert len(export_store.jobs) == 1


def test_owner_lists_enterprise_jobs_while_member_lists_only_their_own(
    export_store: InMemoryExportJobStore,
) -> None:
    _create_job("owner-visible")
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        member_job = _create_job("member-visible")
        member_list = client.get("/api/v1/observability/export-jobs")
    finally:
        client.cookies.set("turnstile_session", OWNER_SESSION)
    owner_list = client.get("/api/v1/observability/export-jobs")

    assert member_job.status_code == 202
    assert [item["id"] for item in member_list.json()["items"]] == [
        member_job.json()["id"]
    ]
    assert len(owner_list.json()["items"]) == 2


def test_idempotency_key_reuses_identical_job_and_rejects_changed_request(
    export_store: InMemoryExportJobStore,
) -> None:
    first = _create_job("retry-key")
    retry = _create_job("retry-key")
    changed = _create_job("retry-key", filters={"project_id": "project-other"})

    assert first.status_code == retry.status_code == 202
    assert first.json()["id"] == retry.json()["id"]
    assert changed.status_code == 409
    assert len(export_store.jobs) == 1


def test_per_user_active_job_limit_is_enforced_before_a_third_job(
    export_store: InMemoryExportJobStore,
) -> None:
    first = _create_job("active-1")
    second = _create_job("active-2")
    third = _create_job("active-3")

    assert first.status_code == second.status_code == 202
    assert third.status_code == 429
    assert len(export_store.jobs) == 2


def _set_ready_job(
    store: InMemoryExportJobStore,
    job_id: UUID,
    *,
    creator_user_id: UUID = OWNER_ID,
    expires_at: datetime | None = None,
) -> None:
    store.jobs[job_id].update(
        {
            "creator_user_id": creator_user_id,
            "status": "ready",
            "row_count": 2,
            "blob_name": f"usage-exports/{TENANT_ID}/{job_id}.zip",
            "expires_at": expires_at or datetime.now(UTC) + timedelta(days=7),
        }
    )


def _override_usage_export_blob(blob: object) -> object | None:
    dependency = getattr(observability, "get_usage_export_blob", None)
    if dependency is not None:
        app.dependency_overrides[dependency] = lambda: blob
    return dependency


class MemoryUsageExportBlob:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.downloaded: list[str] = []

    def download_chunks(self, name: str):
        self.downloaded.append(name)
        if self.failure is not None:
            raise self.failure
        yield b"PK\x03\x04private-report"


def test_owner_downloads_ready_job_as_private_no_store_zip_and_audits_metadata(
    export_store: InMemoryExportJobStore,
) -> None:
    created = _create_job("download-owner")
    job_id = UUID(created.json()["id"])
    _set_ready_job(export_store, job_id)
    blob = MemoryUsageExportBlob()
    dependency = _override_usage_export_blob(blob)
    audit = CapturingAuditService()
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit
    try:
        response = client.get(f"/api/v1/observability/export-jobs/{job_id}/download")
    finally:
        if dependency is not None:
            app.dependency_overrides.pop(dependency, None)
        app.dependency_overrides.pop(get_optional_security_audit_service, None)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert "attachment;" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content == b"PK\x03\x04private-report"
    assert "blob.core.windows.net" not in response.text
    assert blob.downloaded == [f"usage-exports/{TENANT_ID}/{job_id}.zip"]
    assert audit.events[0].action == "usage_export.downloaded"
    assert audit.events[0].resource_id == str(job_id)
    assert audit.events[0].after == {"export_job_id": str(job_id), "row_count": 2}


def test_export_download_requires_a_fresh_authenticated_session(
    export_store: InMemoryExportJobStore,
) -> None:
    created = _create_job("download-requires-session")
    job_id = UUID(created.json()["id"])
    _set_ready_job(export_store, job_id)
    blob = MemoryUsageExportBlob()
    dependency = _override_usage_export_blob(blob)
    client.cookies.clear()
    try:
        response = client.get(f"/api/v1/observability/export-jobs/{job_id}/download")
    finally:
        client.cookies.set("turnstile_session", OWNER_SESSION)
        if dependency is not None:
            app.dependency_overrides.pop(dependency, None)

    assert response.status_code == 401
    assert blob.downloaded == []


def test_owner_can_download_another_users_job_while_member_is_limited_to_their_own(
    export_store: InMemoryExportJobStore,
) -> None:
    owner_created = _create_job("download-other-owner")
    owner_job_id = UUID(owner_created.json()["id"])
    _set_ready_job(export_store, owner_job_id)

    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        member_created = _create_job("download-member-own")
        member_job_id = UUID(member_created.json()["id"])
        _set_ready_job(export_store, member_job_id, creator_user_id=MEMBER_ID)
        blob = MemoryUsageExportBlob()
        dependency = _override_usage_export_blob(blob)
        denied = client.get(
            f"/api/v1/observability/export-jobs/{owner_job_id}/download"
        )
        own = client.get(
            f"/api/v1/observability/export-jobs/{member_job_id}/download"
        )
    finally:
        client.cookies.set("turnstile_session", OWNER_SESSION)

    try:
        owner_download = client.get(
            f"/api/v1/observability/export-jobs/{member_job_id}/download"
        )
    finally:
        if dependency is not None:
            app.dependency_overrides.pop(dependency, None)

    assert denied.status_code == 404
    assert own.status_code == 200
    assert owner_download.status_code == 200
    assert blob.downloaded == [
        f"usage-exports/{TENANT_ID}/{member_job_id}.zip",
        f"usage-exports/{TENANT_ID}/{member_job_id}.zip",
    ]


def test_download_hides_unknown_nonready_and_expired_jobs(
    export_store: InMemoryExportJobStore,
) -> None:
    queued = _create_job("download-queued")
    expired = _create_job("download-expired")
    expired_id = UUID(expired.json()["id"])
    _set_ready_job(
        export_store,
        expired_id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    blob = MemoryUsageExportBlob()
    dependency = _override_usage_export_blob(blob)
    try:
        missing = client.get(
            "/api/v1/observability/export-jobs/30000000-0000-4000-8000-000000000099/download"
        )
        not_ready = client.get(
            f"/api/v1/observability/export-jobs/{queued.json()['id']}/download"
        )
        expired_response = client.get(
            f"/api/v1/observability/export-jobs/{expired_id}/download"
        )
    finally:
        if dependency is not None:
            app.dependency_overrides.pop(dependency, None)

    assert missing.status_code == 404
    assert not_ready.status_code == 409
    assert expired_response.status_code == 410
    assert blob.downloaded == []


def test_job_status_reports_expired_after_retention_even_before_worker_cleanup(
    export_store: InMemoryExportJobStore,
) -> None:
    created = _create_job("status-expired-before-cleanup")
    job_id = UUID(created.json()["id"])
    _set_ready_job(
        export_store,
        job_id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    response = client.get(f"/api/v1/observability/export-jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "expired"


def test_missing_blob_returns_sanitized_service_error_and_audits_failure(
    export_store: InMemoryExportJobStore,
) -> None:
    created = _create_job("download-missing-blob")
    job_id = UUID(created.json()["id"])
    _set_ready_job(export_store, job_id)
    blob = MemoryUsageExportBlob(
        failure=RuntimeError(
            "https://private.blob.core.windows.net/container?sig=secret-token"
        )
    )
    dependency = _override_usage_export_blob(blob)
    audit = CapturingAuditService()
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit
    try:
        response = client.get(f"/api/v1/observability/export-jobs/{job_id}/download")
    finally:
        if dependency is not None:
            app.dependency_overrides.pop(dependency, None)
        app.dependency_overrides.pop(get_optional_security_audit_service, None)

    assert response.status_code == 503
    assert "secret-token" not in response.text
    assert "private.blob.core.windows.net" not in response.text
    assert audit.events[0].action == "usage_export.downloaded"
    assert audit.events[0].result == "failure"
    assert audit.events[0].after["reason_code"] == "blob_unavailable"
