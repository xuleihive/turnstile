from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.api import (
    app,
)
from backend.http.dependencies import get_repository
from backend.http.security_audit_support import get_optional_security_audit_service
from backend.http.session import get_auth_store
from tests.platform.api.api_support import (
    MEMBER_SESSION,
    OWNER_SESSION,
    StubAuthStore,
    _usage_record,
    client,
)
from turnstile_core.domain.models import (
    TokenUsageRecord,
)
from turnstile_core.domain.security_audit import AuditEventWrite
from turnstile_core.persistence.in_memory import InMemoryRepository

pytest_plugins = ("tests.platform.api.api_fixtures",)


def test_real_usage_record_drives_enterprise_analytics() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        params = {
            "from": "2026-07-01T00:00:00Z",
            "to": "2026-08-01T00:00:00Z",
        }
        empty = client.get("/api/v1/observability/executive-overview", params=params)
        assert empty.status_code == 200
        assert empty.json()["totals"]["total_requests"] == 0

        repository.write_token_usage(
            TokenUsageRecord(
                id="usage-real-1",
                request_id="request-real-1",
                correlation_id="apim-correlation-1",
                ts=datetime(2026, 7, 20, tzinfo=UTC),
                team="AI Platform",
                organization="Contoso Global",
                organization_id="org-contoso-global",
                department="AI Platform",
                department_id="department-platform",
                project="Model FinOps",
                project_id="project-finops",
                user="Test User 01",
                user_id="user-01",
                agent="Delivery Engineer",
                agent_id="agent-delivery",
                workflow="usage-validation",
                run_id="run-real-1",
                turn_index=1,
                provider="microsoft_foundry",
                model="gpt-5.6-terra",
                model_id="gpt-5.6-terra",
                runtime="Microsoft Foundry via APIM",
                request_source="agent-console",
                input_tokens=100,
                cached_tokens=0,
                output_tokens=25,
                et=200,
                et_coeff_m=1,
                latency_ms=750,
                status="success",
                status_code=200,
                estimated_cost=0.0025,
                estimated=False,
                ingest_source="gateway",
            )
        )

        overview = client.get("/api/v1/observability/executive-overview", params=params)
        assert overview.json()["totals"] == {
            "total_tokens": 125,
            "cache_read_tokens": 0,
            "total_requests": 1,
            "estimated_cost": 0.0025,
            "average_latency_ms": 750.0,
            "error_rate": 0.0,
            # One successful 750 ms request: every percentile is that request, it lands in
            # the success bucket, and 750 ms puts it in the sub-second band.
            "p50_latency_ms": 750.0,
            "p95_latency_ms": 750.0,
            "p99_latency_ms": 750.0,
            "success_requests": 1,
            "client_error_requests": 0,
            "server_error_requests": 0,
            "latency_under_1s": 1,
            "latency_1_to_2s": 0,
            "latency_2_to_5s": 0,
            "latency_over_5s": 0,
        }
        distribution = client.get(
            "/api/v1/observability/distribution",
            params={**params, "dimension": "department"},
        ).json()
        assert distribution["items"][0]["id"] == "department-platform"
        assert distribution["items"][0]["total_tokens"] == 125
        requests = client.get("/api/v1/observability/requests", params=params).json()
        assert requests["items"][0]["request_id"] == "request-real-1"
        detail = client.get("/api/v1/observability/requests/request-real-1").json()
        assert detail["correlation_id"] == "apim-correlation-1"
        assert detail["organization_id"] == "org-contoso-global"
    finally:
        app.dependency_overrides.pop(get_repository, None)


@pytest.mark.parametrize("reverse_insertion", [False, True])
def test_request_trace_preserves_attempts_and_legacy_lookup(reverse_insertion: bool) -> None:
    repository = InMemoryRepository()
    records = [
        _usage_record("attempt-first", "Model Runtime", 100).model_copy(
            update={"request_id": "caller-request", "correlation_id": "attempt-first"}
        ),
        _usage_record("attempt-second", "Model Runtime", 0, status_code=429).model_copy(
            update={
                "request_id": "caller-request",
                "correlation_id": "attempt-second",
                "ts": datetime(2026, 7, 20, 0, 0, 1, tzinfo=UTC),
                "error_message": "rate limited",
            }
        ),
    ]
    for record in reversed(records) if reverse_insertion else records:
        repository.write_token_usage(record)
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        params = {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"}
        response = client.get("/api/v1/observability/requests", params=params)
        assert response.status_code == 200
        listed = response.json()["items"]
        assert [item["correlation_id"] for item in listed] == ["attempt-second", "attempt-first"]
        assert {item["request_id"] for item in listed} == {"caller-request"}
        assert [item["status_code"] for item in listed] == [429, 200]

        for selector, expected_correlation, expected_status in (
            ("attempt-first", "attempt-first", 200),
            ("attempt-second", "attempt-second", 429),
            ("caller-request", "attempt-second", 429),
        ):
            detail = client.get(f"/api/v1/observability/requests/{selector}")
            assert detail.status_code == 200
            assert detail.json()["correlation_id"] == expected_correlation
            assert detail.json()["status_code"] == expected_status
        assert client.get("/api/v1/observability/requests/unknown-attempt").status_code == 404
    finally:
        app.dependency_overrides.pop(get_repository, None)


def test_request_trace_prioritizes_correlation_and_keeps_copilot_private() -> None:
    repository = InMemoryRepository()
    exact = _usage_record("exact-attempt", "Model Runtime", 100).model_copy(
        update={"request_id": "caller-request", "correlation_id": "shared-selector"}
    )
    newer = _usage_record("newer-attempt", "Model Runtime", 200).model_copy(
        update={
            "request_id": "shared-selector",
            "correlation_id": "newer-correlation",
            "ts": datetime(2026, 7, 20, 0, 0, 1, tzinfo=UTC),
        }
    )
    copilot = _usage_record("copilot-event", "GitHub Copilot CLI", 50).model_copy(
        update={
            "request_id": "shared-selector",
            "correlation_id": "copilot-correlation",
            "ts": datetime(2026, 7, 20, 0, 0, 2, tzinfo=UTC),
            "usage_domain": "github_copilot",
            "ingest_source": "copilot_cli",
        }
    )
    for record in (copilot, newer, exact):
        repository.write_token_usage(record)
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        detail = client.get("/api/v1/observability/requests/shared-selector")
        assert detail.status_code == 200
        assert detail.json()["correlation_id"] == "shared-selector"
        assert detail.json()["request_id"] == "caller-request"
        assert client.get("/api/v1/observability/requests/copilot-correlation").status_code == 404
    finally:
        app.dependency_overrides.pop(get_repository, None)


def test_usage_report_export_contains_summary_and_all_filtered_request_rows() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        for index in range(205):
            record = _usage_record(f"export-{index:03d}", "Foundry via APIM", 1).model_copy(
                update={
                    "estimated_cost": 0.001,
                    "user": " \t=HYPERLINK(\"https://example.invalid\")"
                    if index == 0
                    else "Analyst",
                }
            )
            repository.write_token_usage(record)

        outside_scope = _usage_record("export-outside", "Foundry via APIM", 50).model_copy(
            update={"organization_id": "org-other", "organization": "Other"}
        )
        repository.write_token_usage(outside_scope)
        repository.write_token_usage(
            _usage_record("export-copilot", "GitHub Copilot CLI", 100).model_copy(
                update={"usage_domain": "github_copilot", "ingest_source": "copilot_cli"}
            )
        )

        response = client.get(
            "/api/v1/observability/requests/export",
            params={
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
                "organization_id": "org-contoso-global",
            },
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/zip")
        assert response.headers["content-disposition"].endswith(
            'filename="turnstile_usage_report_20260701_20260801.zip"'
        )
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert set(archive.namelist()) == {"analysis.md", "requests.csv"}
            report = archive.read("analysis.md").decode("utf-8")
            assert "205" in report
            assert "0.205" in report

            csv_bytes = archive.read("requests.csv")
            assert csv_bytes.startswith(b"\xef\xbb\xbf")
            rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
            assert len(rows) == 205
            assert rows[0]["request_id"] == "request-export-204"
            assert rows[0]["usage_id"] == "export-204"
            formula_row = next(row for row in rows if row["request_id"] == "request-export-000")
            assert formula_row["user_name"] == "' \t=HYPERLINK(\"https://example.invalid\")"
            assert "request-export-outside" not in {row["request_id"] for row in rows}
            assert "request-export-copilot" not in {row["request_id"] for row in rows}
            assert "cached_tokens" in rows[0]
            assert "cache_write_tokens" in rows[0]
            assert "input_price_per_million_usd" in rows[0]
            assert "estimated_cost_usd" in rows[0]
            assert not {"prompt", "completion", "content", "messages"}.intersection(rows[0])
    finally:
        app.dependency_overrides.pop(get_repository, None)


def test_legacy_usage_report_export_records_audit_metadata() -> None:
    class RecordingAuditService:
        def __init__(self) -> None:
            self.events: list[AuditEventWrite] = []

        def record(
            self, event: AuditEventWrite, connection: object | None = None
        ) -> None:
            self.events.append(event)

    repository = InMemoryRepository()
    repository.write_token_usage(_usage_record("audited-export", "Foundry via APIM", 1))
    audit = RecordingAuditService()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit
    try:
        response = client.get(
            "/api/v1/observability/requests/export",
            params={
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
            },
        )
    finally:
        app.dependency_overrides.pop(get_repository, None)
        app.dependency_overrides.pop(get_optional_security_audit_service, None)

    assert response.status_code == 200
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == "usage_export.legacy_exported"
    assert event.result == "success"
    assert event.after["row_count"] == 1


def test_usage_report_export_requires_an_authenticated_session() -> None:
    client.cookies.clear()
    try:
        response = client.get(
            "/api/v1/observability/requests/export",
            params={
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
            },
        )
        assert response.status_code == 401
    finally:
        client.cookies.set("turnstile_session", OWNER_SESSION)


def test_legacy_usage_report_export_rejects_ranges_over_31_days() -> None:
    response = client.get(
        "/api/v1/observability/requests/export",
        params={"from": "2026-06-30T00:00:00Z", "to": "2026-08-01T00:00:00Z"},
    )

    assert response.status_code == 422


def test_legacy_usage_report_export_rejects_more_than_10000_rows() -> None:
    class OverLimitRepository:
        @staticmethod
        def executive_overview(*_args: object) -> dict[str, object]:
            return {"totals": {"total_requests": 10_001}}

    app.dependency_overrides[get_repository] = OverLimitRepository
    try:
        response = client.get(
            "/api/v1/observability/requests/export",
            params={
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
            },
        )
    finally:
        app.dependency_overrides.pop(get_repository, None)

    assert response.status_code == 413


def test_member_usage_queries_cannot_select_another_user() -> None:
    repository = InMemoryRepository()
    repository.write_token_usage(
        _usage_record("member-own", "Foundry via APIM", 10).model_copy(
            update={"user_id": "member@contoso.com"}
        )
    )
    repository.write_token_usage(
        _usage_record("member-own-uuid", "Foundry via APIM", 20).model_copy(
            update={"user_id": "00000000-0000-4000-8000-000000000002"}
        )
    )
    repository.write_token_usage(
        _usage_record("member-other", "Foundry via APIM", 20).model_copy(
            update={"user_id": "other@contoso.com"}
        )
    )
    repository.write_token_usage(
        _usage_record("member-other-uuid", "Foundry via APIM", 30).model_copy(
            update={"user_id": "00000000-0000-4000-8000-000000000001"}
        )
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_auth_store] = lambda: StubAuthStore(role="member")
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        params = {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"}
        own = client.get("/api/v1/observability/requests", params=params)
        own_detail = client.get(
            "/api/v1/observability/requests/request-member-own-uuid"
        )
        other_detail = client.get(
            "/api/v1/observability/requests/request-member-other-uuid"
        )
        other = client.get(
            "/api/v1/observability/requests",
            params={**params, "user_id": "other@contoso.com"},
        )
    finally:
        client.cookies.clear()
        app.dependency_overrides.pop(get_repository, None)
        app.dependency_overrides.pop(get_auth_store, None)

    assert own.status_code == 200
    assert {
        item["user_id"] for item in own.json()["items"]
    } == {"member@contoso.com", "00000000-0000-4000-8000-000000000002"}
    assert own_detail.status_code == 200
    assert own_detail.json()["user_id"] == "00000000-0000-4000-8000-000000000002"
    assert other_detail.status_code == 404
    assert other.status_code == 403


def test_postgres_request_detail_prefers_attempt_correlation() -> None:
    source = (
        Path(__file__).resolve().parents[3] / "turnstile_core/persistence/repository.py"
    ).read_text(encoding="utf-8")

    assert "WHERE (usage.correlation_id = %s OR usage.request_id = %s)" in source
    assert "AND usage.usage_domain = 'apim'" in source
    assert "ORDER BY CASE WHEN usage.correlation_id = %s THEN 0 ELSE 1 END" in source
    assert "usage.ts DESC, usage.id DESC" in source
    assert "(request_id, request_id, request_id)" in source


def test_apim_observability_excludes_copilot_cli_usage() -> None:
    """The APIM data domain cannot expose Copilot CLI records, even through a runtime filter."""
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        params = {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"}
        repository.write_token_usage(_usage_record("apim-1", "Microsoft Foundry via APIM", 100))
        repository.write_token_usage(
            _usage_record("cli-1", "GitHub Copilot CLI", 40).model_copy(
                update={
                    "ingest_source": "copilot_cli",
                    "usage_domain": "github_copilot",
                }
            )
        )

        distribution = client.get(
            "/api/v1/observability/distribution",
            params={**params, "dimension": "runtime"},
        )
        assert distribution.status_code == 200
        items = {item["name"]: item["total_tokens"] for item in distribution.json()["items"]}
        assert items == {"Microsoft Foundry via APIM": 100}

        # A caller cannot cross the boundary by naming the hidden runtime explicitly.
        scoped = client.get(
            "/api/v1/observability/executive-overview",
            params={**params, "runtime": "GitHub Copilot CLI"},
        ).json()
        assert scoped["totals"]["total_tokens"] == 0
        assert scoped["totals"]["total_requests"] == 0

        # Lists and detail lookups enforce the same boundary as aggregates.
        listed = client.get("/api/v1/observability/requests", params=params).json()["items"]
        assert [item["runtime"] for item in listed] == ["Microsoft Foundry via APIM"]
        scoped_list = client.get(
            "/api/v1/observability/requests",
            params={**params, "runtime": "GitHub Copilot CLI"},
        ).json()["items"]
        assert scoped_list == []
        assert client.get("/api/v1/observability/requests/request-cli-1").status_code == 404
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_legacy_user_cache_metric_is_ignored_by_totals_and_ranking() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        first = _usage_record("person-a", "Microsoft Foundry via APIM", 100).model_copy(
            update={"user_id": "a@example.com", "user": "Person A", "ingest_source": "eventhub"}
        )
        second = _usage_record("person-b", "Microsoft Foundry via APIM", 200).model_copy(
            update={"user_id": "b@example.com", "user": "Person B", "ingest_source": "eventhub"}
        )
        repository.write_token_usage(first)
        repository.write_token_usage(second)
        bucket = first.ts.replace(minute=0, second=0, microsecond=0)
        repository.apim_cache_read_buckets.update(
            {
                ("turnstile-llm", "user", "a@example.com", bucket): 1000,
                ("turnstile-llm", "user", "b@example.com", bucket): 10,
            }
        )
        params = {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"}

        items = client.get(
            "/api/v1/observability/distribution",
            params={**params, "dimension": "user"},
        ).json()["items"]
        scoped = client.get(
            "/api/v1/observability/executive-overview",
            params={**params, "user_id": "a@example.com"},
        ).json()["totals"]

        assert [item["id"] for item in items[:2]] == ["b@example.com", "a@example.com"]
        assert items[0]["cache_read_tokens"] == 0
        assert items[0]["total_tokens"] == 200
        assert items[1]["cache_read_tokens"] == 0
        assert items[1]["total_tokens"] == 100
        assert scoped["cache_read_tokens"] == 0
        assert scoped["total_tokens"] == 100
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_legacy_user_cache_metric_is_ignored_by_grouped_and_scoped_trends() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        first = _usage_record("trend-a", "Microsoft Foundry via APIM", 100).model_copy(
            update={"user_id": "a@example.com", "user": "Person A", "ingest_source": "eventhub"}
        )
        second = _usage_record("trend-b", "Microsoft Foundry via APIM", 200).model_copy(
            update={"user_id": "b@example.com", "user": "Person B", "ingest_source": "eventhub"}
        )
        repository.write_token_usage(first)
        repository.write_token_usage(second)
        bucket = first.ts.replace(minute=0, second=0, microsecond=0)
        repository.apim_cache_read_buckets.update(
            {
                ("turnstile-llm", "user", "a@example.com", bucket): 1000,
                ("turnstile-llm", "user", "b@example.com", bucket): 10,
            }
        )
        params = {
            "from": "2026-07-01T00:00:00Z",
            "to": "2026-08-01T00:00:00Z",
            "interval": "hour",
        }

        grouped = client.get(
            "/api/v1/observability/trends",
            params={**params, "group_by": "user"},
        ).json()["points"]
        scoped = client.get(
            "/api/v1/observability/trends",
            params={**params, "group_by": "none", "user_id": "a@example.com"},
        ).json()["points"]

        by_user = {point["key"]: point["totals"] for point in grouped}
        assert by_user["a@example.com"]["cache_read_tokens"] == 0
        assert by_user["a@example.com"]["total_tokens"] == 100
        assert by_user["b@example.com"]["cache_read_tokens"] == 0
        assert scoped[0]["totals"]["cache_read_tokens"] == 0
        assert scoped[0]["totals"]["total_tokens"] == 100
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_cross_dimension_filter_does_not_invent_cache_read_intersection() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        record = _usage_record("cross-scope", "Microsoft Foundry via APIM", 100).model_copy(
            update={
                "user_id": "a@example.com",
                "user": "Person A",
                "model_id": "model-a",
                "ingest_source": "eventhub",
            }
        )
        repository.write_token_usage(record)
        bucket = record.ts.replace(minute=0, second=0, microsecond=0)
        repository.apim_cache_read_buckets[
            ("turnstile-llm", "user", "a@example.com", bucket)
        ] = 1000

        totals = client.get(
            "/api/v1/observability/executive-overview",
            params={
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
                "user_id": "a@example.com",
                "model_id": "model-a",
            },
        ).json()["totals"]

        assert totals["cache_read_tokens"] == 0
        assert totals["total_tokens"] == 100
    finally:
        app.dependency_overrides.pop(get_repository, None)

@pytest.mark.parametrize(
    ("dimension", "dimension_value", "cache_read_tokens"),
    (
        ("organization", "org-contoso-global", 101),
        ("department", "department-platform", 102),
        ("project", "project-finops", 103),
        ("agent", "agent-delivery", 104),
        ("user", "test.user01@contoso.com", 105),
        ("model", "gpt-5.6-luna", 106),
        ("runtime", "Microsoft Foundry via APIM", 107),
    ),
)
def test_legacy_business_dimension_cache_metric_is_ignored(
    dimension: str, dimension_value: str, cache_read_tokens: int
) -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        record = _usage_record("dimension-cache", "Microsoft Foundry via APIM", 100)
        repository.write_token_usage(record)
        bucket = record.ts.replace(minute=0, second=0, microsecond=0)
        repository.apim_cache_read_buckets[
            ("turnstile-llm", dimension, dimension_value, bucket)
        ] = cache_read_tokens

        item = client.get(
            "/api/v1/observability/distribution",
            params={
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
                "dimension": dimension,
            },
        ).json()["items"][0]

        assert item["id"] == dimension_value
        assert item["cache_read_tokens"] == 0
        assert item["total_tokens"] == 100
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_department_user_trend_ignores_legacy_dynamic_cache_metrics() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        inside = _usage_record("inside", "Microsoft Foundry via APIM", 100).model_copy(
            update={"user_id": "inside@example.com", "user": "Inside", "ingest_source": "eventhub"}
        )
        outside = _usage_record("outside", "Microsoft Foundry via APIM", 100).model_copy(
            update={
                "department_id": "department-other",
                "department": "Other",
                "user_id": "outside@example.com",
                "user": "Outside",
                "ingest_source": "eventhub",
            }
        )
        repository.write_token_usage(inside)
        repository.write_token_usage(outside)
        bucket = inside.ts.replace(minute=0, second=0, microsecond=0)
        repository.apim_cache_read_buckets.update(
            {
                ("turnstile-llm", "user", "inside@example.com", bucket): 50,
                ("turnstile-llm", "user", "outside@example.com", bucket): 900,
            }
        )

        points = client.get(
            "/api/v1/observability/trends",
            params={
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
                "interval": "hour",
                "group_by": "user",
                "department_id": "department-platform",
            },
        ).json()["points"]

        assert [point["key"] for point in points] == ["inside@example.com"]
        assert points[0]["totals"]["cache_read_tokens"] == 0
        assert points[0]["totals"]["total_tokens"] == 100
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_trend_buckets_carry_the_settled_cost() -> None:
    """A per-period chart has to be able to plot cost from the same source as the KPI.

    Without this the caller can only re-derive cost from token counts at today's registry
    rates, which disagrees with the executive total whenever a price has changed since the
    request landed. `MetricTotals.estimated_cost` defaults to 0 because run and optimization
    aggregates reuse the shape and have no cost dimension, so only a test keeps the trend
    path from silently regressing to that default.
    """
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        params = {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"}
        repository.write_token_usage(_usage_record("apim-1", "Microsoft Foundry via APIM", 100))
        repository.write_token_usage(_usage_record("cli-1", "GitHub Copilot CLI", 40))

        points = client.get(
            "/api/v1/observability/trends",
            params={**params, "group_by": "runtime", "interval": "day"},
        ).json()["points"]
        costs = {point["label"]: point["totals"]["estimated_cost"] for point in points}
        assert costs["GitHub Copilot CLI"] > 0

        # The bucket total must equal what the executive KPI reports for the same scope, so a
        # chart and a headline figure can never disagree.
        scoped = client.get(
            "/api/v1/observability/executive-overview",
            params={**params, "runtime": "GitHub Copilot CLI"},
        ).json()
        assert costs["GitHub Copilot CLI"] == scoped["totals"]["estimated_cost"]
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_trend_buckets_expose_exclusive_cache_read_and_write() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        record = _usage_record("cache-split", "GitHub Copilot CLI", 16).model_copy(
            update={"cached_tokens": 384, "cache_write_tokens": 128}
        )
        repository.write_token_usage(record)

        totals = client.get(
            "/api/v1/observability/trends",
            params={
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
                "group_by": "runtime",
                "interval": "day",
            },
        ).json()["points"][0]["totals"]

        assert totals["cache_read_tokens"] == 256
        assert totals["cache_write_tokens"] == 128
        assert totals["total_tokens"] == 400
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_global_trend_uses_apim_cache_read_without_attributing_it_to_filters() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        record = _usage_record("stream-1", "Microsoft Foundry via APIM", 100)
        repository.write_token_usage(record)
        bucket = record.ts.replace(minute=0, second=0, microsecond=0)
        repository.apim_cache_read_buckets[
            ("turnstile-llm", "global", "all", bucket)
        ] = 420000
        params = {
            "from": "2026-07-01T00:00:00Z",
            "to": "2026-08-01T00:00:00Z",
            "group_by": "none",
            "interval": "hour",
        }

        global_totals = client.get("/api/v1/observability/trends", params=params).json()[
            "points"
        ][0]["totals"]
        scoped_totals = client.get(
            "/api/v1/observability/trends",
            params={**params, "runtime": "Microsoft Foundry via APIM"},
        ).json()["points"][0]["totals"]

        assert global_totals["cache_read_tokens"] == 420000
        assert scoped_totals["cache_read_tokens"] == 0
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_global_cache_read_accepts_zero_metric_and_excludes_non_apim_usage() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        apim = _usage_record("stream-1", "Microsoft Foundry via APIM", 100).model_copy(
            update={"cached_tokens": 999, "ingest_source": "eventhub"}
        )
        cli = _usage_record("cli-1", "GitHub Copilot CLI", 40).model_copy(
            update={
                "cached_tokens": 25,
                "ingest_source": "copilot_cli",
                "usage_domain": "github_copilot",
            }
        )
        repository.write_token_usage(apim)
        repository.write_token_usage(cli)
        bucket = apim.ts.replace(minute=0, second=0, microsecond=0)
        repository.apim_cache_read_buckets[
            ("turnstile-llm", "global", "all", bucket)
        ] = 0

        totals = client.get(
            "/api/v1/observability/trends",
            params={
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
                "group_by": "none",
                "interval": "hour",
            },
        ).json()["points"][0]["totals"]

        assert totals["cache_read_tokens"] == 999
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_aggregates_answer_tail_latency_and_the_failure_split() -> None:
    """The dashboard must not have to read raw rows to draw a P95 or a status chart.

    Every figure of this shape used to be computed in the browser from a page capped at 200
    rows and then labelled with the full window, so it under-reported silently as soon as the
    window held more requests than the cap (AGENTS.md section 16 item 23). A mean latency and
    an error rate cannot replace them: the mean hides the tail, and one error rate cannot say
    whether the failures were the caller's fault or the provider's.

    `DistributionItem` extends `ExecutiveTotals`, so the same assertions are made against a
    ranking row -- a per-model P95 has to be a real per-model percentile, not the window's.
    """
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        params = {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"}
        # Latencies chosen so every band gets one request and the percentiles are checkable
        # by hand: ordered 100, 1500, 3000, 9000 over four requests.
        for index, (latency, status) in enumerate(
            ((100, 200), (1500, 200), (3000, 404), (9000, 500))
        ):
            repository.write_token_usage(
                _usage_record(
                    f"q{index}",
                    "Microsoft Foundry via APIM",
                    10,
                    latency_ms=latency,
                    status_code=status,
                )
            )

        totals = client.get(
            "/api/v1/observability/executive-overview", params=params
        ).json()["totals"]

        # The failure split is three numbers, not one rate: a 404 is the caller's problem and
        # a 500 is the provider's, and a chart that merges them cannot say which got worse.
        assert totals["success_requests"] == 2
        assert totals["client_error_requests"] == 1
        assert totals["server_error_requests"] == 1
        assert (
            totals["success_requests"]
            + totals["client_error_requests"]
            + totals["server_error_requests"]
            == totals["total_requests"]
        )

        # Latency bands must also partition the window exactly, or the histogram silently
        # drops requests that fall on a boundary.
        assert totals["latency_under_1s"] == 1
        assert totals["latency_1_to_2s"] == 1
        assert totals["latency_2_to_5s"] == 1
        assert totals["latency_over_5s"] == 1
        assert (
            totals["latency_under_1s"]
            + totals["latency_1_to_2s"]
            + totals["latency_2_to_5s"]
            + totals["latency_over_5s"]
            == totals["total_requests"]
        )

        # `percentile_cont` interpolates: with four ordered values the median sits between
        # the middle two. Pinning the interpolated value is what keeps InMemoryRepository and
        # PostgreSQL from disagreeing on small samples.
        assert totals["p50_latency_ms"] == 2250.0
        assert totals["average_latency_ms"] == 3400.0
        # The whole point of a percentile: the tail is far from the mean, in both directions.
        assert totals["p95_latency_ms"] > totals["average_latency_ms"]
        assert totals["p50_latency_ms"] < totals["average_latency_ms"]
        assert totals["p99_latency_ms"] <= 9000.0

        # A ranking row carries the same metrics, scoped to itself rather than to the window.
        item = client.get(
            "/api/v1/observability/distribution",
            params={**params, "dimension": "runtime"},
        ).json()["items"][0]
        for field in ("p50_latency_ms", "p95_latency_ms", "success_requests"):
            assert item[field] == totals[field], field

        # Per-bucket tail latency and failures, so a quality-over-time chart is a server
        # aggregate too. All four records share a day, so the bucket equals the window.
        bucket = client.get(
            "/api/v1/observability/trends",
            params={**params, "group_by": "runtime", "interval": "day"},
        ).json()["points"][0]["totals"]
        assert bucket["failed_calls"] == 2
        assert bucket["p95_latency_ms"] == totals["p95_latency_ms"]
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_one_channel_selects_every_runtime_behind_it() -> None:
    """A gateway fronts several runtimes, so 'spend through APIM' is a multi-runtime question.

    Telemetry stores a runtime name and never a channel, so the filter has to accept the whole
    set. A single-valued filter would force the UI to either drop the channel view or invent a
    channel column the ingestion path does not produce.
    """
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        params = {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"}
        repository.write_token_usage(_usage_record("apim-a", "Microsoft Foundry via APIM", 100))
        repository.write_token_usage(
            _usage_record("apim-b", "Azure Databricks Claude via APIM", 60)
        )
        repository.write_token_usage(_usage_record("cli-a", "GitHub Copilot CLI", 40))

        both_apim = client.get(
            "/api/v1/observability/executive-overview",
            params=[
                *params.items(),
                ("runtime", "Microsoft Foundry via APIM"),
                ("runtime", "Azure Databricks Claude via APIM"),
            ],
        ).json()
        assert both_apim["totals"]["total_tokens"] == 160
        assert both_apim["totals"]["total_requests"] == 2

        # The runtime breakdown stays per runtime inside the selected channel, because the
        # point of drilling in is to see which backend inside APIM is spending.
        inside = client.get(
            "/api/v1/observability/distribution",
            params=[
                *params.items(),
                ("dimension", "runtime"),
                ("runtime", "Microsoft Foundry via APIM"),
                ("runtime", "Azure Databricks Claude via APIM"),
            ],
        ).json()["items"]
        assert {item["name"] for item in inside} == {
            "Microsoft Foundry via APIM",
            "Azure Databricks Claude via APIM",
        }

        # No selection means no constraint, not "match nothing".
        unscoped = client.get(
            "/api/v1/observability/executive-overview", params=params
        ).json()
        assert unscoped["totals"]["total_requests"] == 3
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_a_filtered_channel_can_never_exceed_the_unfiltered_total() -> None:
    """The whole must be at least as large as any of its parts.

    This broke visibly once the channel switcher existed: the overview cost KPI summed the
    200-row request page instead of the server total, so selecting one channel pulled the list
    under the cap and counted every row, while 'all' counted 200 of 442 and came out smaller.
    A subset reading larger than the whole destroys trust in every other number on the page.
    """
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        params = {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"}
        runtimes = [
            "Microsoft Foundry via APIM",
            "GitHub Copilot CLI",
            "Azure Databricks Claude via APIM",
        ]
        for index in range(30):
            repository.write_token_usage(
                _usage_record(f"mixed-{index}", runtimes[index % 3], 100 + index)
            )

        total = client.get(
            "/api/v1/observability/executive-overview", params=params
        ).json()["totals"]
        summed = {"total_tokens": 0, "total_requests": 0, "estimated_cost": 0.0}
        for runtime in runtimes:
            part = client.get(
                "/api/v1/observability/executive-overview",
                params={**params, "runtime": runtime},
            ).json()["totals"]
            for key in summed:
                assert part[key] <= total[key] + 1e-9, f"{runtime} {key} exceeds the total"
                summed[key] += part[key]
        # Disjoint channels, so the parts must also add back up to the whole.
        assert summed["total_tokens"] == total["total_tokens"]
        assert summed["total_requests"] == total["total_requests"]
        assert abs(summed["estimated_cost"] - total["estimated_cost"]) < 1e-9
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_model_registry_is_available_and_redacted() -> None:
    response = client.get("/api/v1/model-management", headers={"X-Hive-Role": "owner"})
    assert response.status_code == 200
    payload = response.json()
    assert [item["name"] for item in payload["runtimes"]] == [
        "Microsoft Foundry via APIM"
    ]
    assert all(item["runtime_kind"] != "copilot_cli" for item in payload["runtimes"])
    assert all(item["provider_kind"] != "github" for item in payload["providers"])
    assert all("credential_ciphertext" not in item for item in payload["providers"])

def test_provider_credential_is_redacted_and_runtime_can_be_updated() -> None:
    headers = {"X-Hive-Role": "owner"}
    created = client.post(
        "/api/v1/model-management/providers",
        headers=headers,
        json={
            "name": "API Test Provider",
            "provider_kind": "openai_compatible",
            "endpoint_url": "https://models.test",
            "auth_type": "bearer",
            "credential": "api-test-secret-2468",
            "enabled": True,
            "config": {},
        },
    )
    assert created.status_code == 200
    provider = next(
        item for item in created.json()["providers"] if item["name"] == "API Test Provider"
    )
    assert provider["credential_configured"] is True
    assert provider["credential_hint"] == "...2468"
    assert "api-test-secret" not in created.text

    registry = created.json()
    runtime = next(
        item for item in registry["runtimes"] if item["name"] == "Microsoft Foundry via APIM"
    )
    updated = client.put(
        f"/api/v1/model-management/runtimes/{runtime['id']}",
        headers=headers,
        json={
            "provider_id": runtime["provider_id"],
            "gateway_profile_id": runtime["gateway_profile_id"],
            "name": runtime["name"],
            "runtime_kind": runtime["runtime_kind"],
            "enabled": runtime["enabled"],
            "is_default": True,
            "config": runtime["config"],
            "allowed_roles": runtime["allowed_roles"],
        },
    )
    assert updated.status_code == 200
    defaults = [item["name"] for item in updated.json()["runtimes"] if item["is_default"]]
    assert defaults == ["Microsoft Foundry via APIM"]
    default_models = [
        item["display_name"] for item in updated.json()["models"] if item["is_default"]
    ]
    assert default_models == ["gpt-5.6-luna"]
