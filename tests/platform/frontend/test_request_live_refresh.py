from tests.support.paths import REPOSITORY_ROOT


def test_request_monitor_polls_list_totals_trend_and_reconciled_detail_when_visible() -> None:
    source = (
        REPOSITORY_ROOT / "frontend/src/data-sources/apim/pages/dashboard-page.tsx"
    ).read_text()
    trace = source.split("function RequestTrace({", 1)[1].split("function RequestTraceDetail(", 1)[
        0
    ]
    assert "cachePolicies.liveUsage" not in source
    policies = (REPOSITORY_ROOT / "frontend/src/api/cache-policies.ts").read_text()
    assert "refetchInterval" not in policies
    scheduler = (REPOSITORY_ROOT / "frontend/src/lib/data-refresh.ts").read_text()
    provider = (REPOSITORY_ROOT / "frontend/src/providers/data-refresh-provider.tsx").read_text()
    assert '"finops"' in scheduler and 'type: "active"' in scheduler
    assert 'document.visibilityState !== "hidden"' in provider
    assert "const refresh = useDataRefresh()" in trace
    assert "${refresh.label}" in trace
    assert 'finopsQueries.usageActivity(filters, "day", timezone)' in trace
    assert "timeZone: timezone" in trace


def test_enabled_directory_filter_keeps_unattributed_subscription_calls() -> None:
    source = (REPOSITORY_ROOT / "frontend/src/lib/usage-filter-scope.ts").read_text()
    assert 'managed.includes("active") ? [...managed, "unattributed"]' in source


def test_all_four_analytics_pages_poll_all_their_visible_data() -> None:
    source = (
        REPOSITORY_ROOT / "frontend/src/data-sources/apim/pages/dashboard-page.tsx"
    ).read_text()
    sections = {
        "ModelUsage": ("function ModelUsage(", "function HorizontalMetricChart(", 3),
        "UsageTrends": ("function UsageTrends(", "function summarizeTrendContributions(", 3),
        "Governance": ("function Governance(", "const SIGNALS_PER_PAGE", 6),
    }
    for name, (start, end, query_count) in sections.items():
        section = source.split(start, 1)[1].split(end, 1)[0]
        # Every observer is covered by the shared scheduler's finops query root.
        queries = section.split("if (", 1)[0]
        assert queries.count("useQuery(") == query_count, name
        assert "refetchInterval" not in queries


def test_trends_and_governance_use_the_selected_timezone_everywhere() -> None:
    source = (
        REPOSITORY_ROOT / "frontend/src/data-sources/apim/pages/dashboard-page.tsx"
    ).read_text()
    trends = source.split("function UsageTrends(", 1)[1].split(
        "function summarizeTrendContributions(", 1
    )[0]
    assert "const { timezone } = useTimezone()" in trends
    assert "finopsQueries.trends(filters, dimension, interval, timezone)" in trends
    assert 'finopsQueries.trends(filters, "none", interval, timezone)' in trends
    assert "formatTrendBucket(peak.bucket, interval, timezone)" in trends
    assert "pivotPoints(points, metric, interval, new Set(leadingSeries), timezone)" in trends
    assert "formatTrendBucket(point.bucket_start, interval, timezone)" in trends
    governance = source.split("function Governance(", 1)[1].split(
        "const SIGNALS_PER_PAGE", 1
    )[0]
    assert "const { timezone } = useTimezone()" in governance
    assert 'finopsQueries.trends(filters, "none", "day", timezone)' in governance
    assert 'formatTrendBucket(point.bucket_start, "day", timezone)' in governance
    assert "formatUsageTimestamp(item.detected_at, timezone, getIntlLocale())" in source
    assert "formatUsageTimestamp(signal.detected_at, timezone, getIntlLocale())" in source
    assert 'timeZone: "UTC"' not in source
