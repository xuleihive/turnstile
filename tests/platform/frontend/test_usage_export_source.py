from tests.support.paths import FRONTEND_SOURCE


def test_usage_export_source_uses_job_api_and_idempotent_filtered_requests() -> None:
    api = (FRONTEND_SOURCE / "data-sources" / "apim" / "api.ts").read_text(
        encoding="utf-8"
    )
    types = (FRONTEND_SOURCE / "data-sources" / "apim" / "types.ts").read_text(
        encoding="utf-8"
    )

    assert '"queued" | "running" | "ready" | "failed" | "expired"' in types
    assert '"/api/v1/observability/export-jobs"' in api
    assert '"Idempotency-Key"' in api
    assert "crypto.randomUUID()" in api
    assert '"/api/v1/observability/requests/export?' not in api
    for key in (
        "organization_id",
        "department_id",
        "project_id",
        "agent_id",
        "model_id",
        "user_id",
        "runtime",
        "status_code",
    ):
        assert key in api


def test_usage_export_queries_poll_until_all_jobs_are_terminal() -> None:
    queries = (FRONTEND_SOURCE / "data-sources" / "apim" / "queries.ts").read_text(
        encoding="utf-8"
    )

    assert "exportJobs" in queries
    assert "dataSource.usageExportJobs" in queries
    assert '"queued"' in queries
    assert '"running"' in queries
    assert "refetchInterval" in queries


def test_finops_request_page_queues_tracks_retries_and_downloads_exports() -> None:
    api = (FRONTEND_SOURCE / "data-sources" / "apim" / "api.ts").read_text(
        encoding="utf-8"
    )
    page = (FRONTEND_SOURCE / "data-sources" / "apim" / "pages" / "dashboard-page.tsx").read_text(
        encoding="utf-8"
    )

    assert "useMutation" in page
    assert "finopsQueries.usageExportJobs" in page
    assert "createUsageExportJob" in api
    assert "dataSource.createUsageExportJob(selectedFilters)" in page
    assert "createExportJob.mutate(filters)" in page
    assert "downloadUsageExportJob" in page
    assert 'queued: "等待处理"' in page
    assert 'running: "正在生成"' in page
    assert 'job.status === "ready"' in page
    assert 'job.status === "failed"' in page
    assert 'job.status === "expired"' in page
    assert "job.row_count" in page
    assert "job.expires_at" in page
    assert "job.filters" in page
    assert "usageFiltersFromExportJob(job)" in page
    assert "重试" in page
    assert "exportUsageReport" not in page
