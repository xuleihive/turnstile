from tests.support.paths import (
    FRONTEND_SOURCE,
    read_apim_dashboard_source,
    read_frontend_styles,
)


def test_centered_page_loading_states_share_one_visual_scale() -> None:
    css = read_frontend_styles()
    shared_rule = css.split(
        ".registry-loading, .finops-state:not(.empty):not(.error) {", 1
    )[1].split("}", 1)[0]
    icon_rule = css.split(
        ".registry-loading > svg, .finops-state:not(.empty):not(.error) > svg {", 1
    )[1].split("}", 1)[0]

    assert "gap: 8px" in shared_rule
    assert "font-size: 12px" in shared_rule
    assert "line-height: 16px" in shared_rule
    assert "width: 18px" in icon_rule
    assert "height: 18px" in icon_rule
    assert "flex: 0 0 18px" in icon_rule


def test_invocation_console_includes_and_defaults_to_the_signed_in_owner() -> None:
    source = read_apim_dashboard_source()

    assert "const { user: sessionUser } = useAuth();" in source
    assert "person.id === sessionUser.email" in source
    assert "useState(sessionUser?.email ?? users[0]?.id" in source
    assert "assignedModelRouters" not in source
    assert 'items={users}' in source


def test_invocation_and_request_trace_use_direct_models() -> None:
    source = read_apim_dashboard_source()

    assert "runtime_id: model.runtime_id" in source
    assert "model_id: model.id" in source
    assert "assignedModelRouters" not in source
    assert "router_id" not in source


def test_request_trace_kpis_are_diagnostic_not_executive_duplicates() -> None:
    source = read_apim_dashboard_source()
    css = read_frontend_styles()
    request_trace = source.split("function RequestTrace(", 1)[1].split(
        "function RequestTraceDetail", 1
    )[0]

    for label in ("P50 时延", "P95 时延", "失败请求", "≥5 秒请求"):
        assert f'label="{label}"' in request_trace
    for label in ("请求数", "成功率", "Token", "估算费用"):
        assert f'label="{label}"' not in request_trace
    assert "client_error_requests" in request_trace
    assert "server_error_requests" in request_trace
    assert "latency_over_5s" in request_trace
    assert ".trace-kpis { grid-template-columns: repeat(4, minmax(0, 1fr)); }" in css
    assert 'finopsQueries.trends(filters, "none", "day")' in request_trace
    assert '<PanelTitle title="调用趋势" meta="每日" />' in request_trace
    assert '<RequestVolumeTrendChart rows={volumeRows} />' in request_trace
    assert 'title="时延占比分布"' in request_trace
    assert '<RequestLatencyChart totals={totals} percentage />' in request_trace
    assert "<RequestHealthChart" not in request_trace
    assert "const firstRequestId = query.data?.[0]?.correlation_id ?? null" in request_trace
    assert "if (!selected && firstRequestId) setSelected(firstRequestId)" in request_trace


def test_request_trace_selects_attempts_without_losing_the_caller_id() -> None:
    source = read_apim_dashboard_source()
    request_trace = source.split("function RequestTrace(", 1)[1].split(
        "function RequestTraceDetail", 1
    )[0]
    trace_row = source.split("function TraceRow(", 1)[1]

    assert "selectedCorrelationId = detail.data?.correlation_id ?? selected" in request_trace
    assert "key={item.correlation_id}" in request_trace
    assert "active={selectedCorrelationId === item.correlation_id}" in request_trace
    assert "onClick={() => setSelected(item.correlation_id)}" in request_trace
    assert "key={item.request_id}" not in request_trace
    assert "attemptsByRequest.get(item.request_id)" in request_trace
    assert "attemptPositions.set(item.correlation_id" in request_trace
    assert "Date.parse(left.timestamp) - Date.parse(right.timestamp)" in request_trace
    assert "left.correlation_id.localeCompare(right.correlation_id)" in request_trace
    assert "attempt={attemptPositions.get(item.correlation_id)}" in request_trace
    assert "Correlation ID: ${item.correlation_id}" in trace_row
    assert "#${attempt.index}/${attempt.total}" in trace_row
    assert "item.request_id" in trace_row


def test_model_success_rate_rankings_sort_highest_first() -> None:
    source = read_apim_dashboard_source()
    panels = source.split('title="模型成功率"')[1:]

    assert len(panels) == 2
    assert all("sortAscending" not in panel.split("/>", 1)[0] for panel in panels)
    assert "right.value - left.value" in source


def test_usage_activity_chart_splits_cache_read_and_write_without_double_counting() -> None:
    source = (FRONTEND_SOURCE / "components/finops/usage-activity-card.tsx").read_text(
        encoding="utf-8"
    )

    assert 'label: "缓存读取"' in source
    assert 'label: "缓存写入"' in source
    assert "const explicitCacheRead = point.totals.cache_read_tokens" in source
    assert "point.totals.cached_tokens - explicitCacheRead" in source
    assert "row.cacheWrite += cacheWrite" in source
    assert "row.cached += point.totals.cached_tokens" not in source


def test_member_governance_views_hide_owner_only_controls() -> None:
    budget_source = (FRONTEND_SOURCE / "data-sources/apim/pages/budget-page.tsx").read_text(
        encoding="utf-8"
    )
    finops_styles = (FRONTEND_SOURCE / "styles/finops.css").read_text(encoding="utf-8")
    anomaly_source = (
        FRONTEND_SOURCE / "data-sources/apim/pages/anomaly-rule-management.tsx"
    ).read_text(encoding="utf-8")

    assert 'const canManage = user?.role === "owner"' in budget_source
    assert "canManage ? <Switch" in budget_source
    assert 'className="budget-enforcement-state"' in budget_source
    assert "data-mode={enforcement.mode}" in budget_source
    assert 'className="budget-progress-cell" data-label="已使用"' in budget_source
    assert "minWidths={[180, 100, 140, 100, 110, 94, 88, 52]}" in budget_source
    assert ".budget-table-row > .budget-status-cell { grid-column: 1 / 3;" in finops_styles
    assert "{canManage && <DropdownMenu>" in budget_source
    assert "{canManage && bulkOpen && people.data" in budget_source

    assert 'const canManage = user?.role === "owner"' in anomaly_source
    assert "disabled={busy || !canManage}" in anomaly_source
    assert "{canManage && editing !== undefined" in anomaly_source


def test_budget_block_and_people_status_headers_are_centered() -> None:
    styles = (FRONTEND_SOURCE / "styles/finops.css").read_text(encoding="utf-8")

    assert (
        ".budget-table-head > span:nth-child(6), "
        ".budget-table-head > span:nth-child(7) { text-align: center; }"
        in styles
    )
    assert (
        ".people-table-head .people-status-heading "
        "{ padding: 0 14px; text-align: center; }"
        in styles
    )


def test_data_source_switcher_replaces_the_legacy_access_channel_filter() -> None:
    app_source = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    apim_source = (FRONTEND_SOURCE / "data-sources/apim/source.tsx").read_text(
        encoding="utf-8"
    )
    dashboard_source = read_apim_dashboard_source()

    assert 'type DataSource = "apim" | "github-copilot"' in app_source
    assert 'url.searchParams.set("source", next)' in app_source
    assert '<ApimLogo size={17} />' in app_source
    assert '<CopilotLogo size={17} />' in app_source
    assert "function normalizePageForSource" in app_source
    assert "normalizePageForAccess(next, page, user)" in app_source
    assert "normalizePageForSource(source, page)" in app_source
    assert "export function normalizeApimPage" in apim_source
    assert 'return apimPageIds.has(page) ? page : "finops-overview"' in apim_source
    assert "function AccessPointFilter" not in dashboard_source
    assert "runtimeAccessPoints" not in dashboard_source
    assert '"接入来源"' not in dashboard_source


def test_reports_are_in_report_center_under_the_fixed_ai_finops_module() -> None:
    app_source = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    pin_dialog = (
        FRONTEND_SOURCE / "components/assistant/pin-dialog.tsx"
    ).read_text(encoding="utf-8")
    english = (FRONTEND_SOURCE / "locales/en/phrases-core.ts").read_text(
        encoding="utf-8"
    )

    assert 'label: "AI FinOps"' in app_source
    assert 'label: "报表中心"' in app_source
    assert "<span>固定</span>" not in app_source
    assert "保存到报表中心。" in pin_dialog
    assert "左侧导航" not in pin_dialog
    assert '"报表中心": "Report Center"' in english
    assert '"固定": "Pinned"' not in english


def test_copilot_pages_use_only_the_copilot_query_domain() -> None:
    dashboard_source = (
        FRONTEND_SOURCE / "data-sources/github-copilot/pages/dashboard-page.tsx"
    ).read_text(encoding="utf-8")
    budget_source = (
        FRONTEND_SOURCE / "data-sources/github-copilot/pages/budget-page.tsx"
    ).read_text(encoding="utf-8")
    queries_source = (
        FRONTEND_SOURCE / "data-sources/github-copilot/queries.ts"
    ).read_text(encoding="utf-8")

    for source in (dashboard_source, budget_source):
        assert "finopsQueries" not in source
        assert "/api/v1/observability" not in source
    assert 'all: ["copilot"] as const' in queries_source
    assert '["copilot", "dashboard", organization]' in queries_source


def test_copilot_mobile_grids_can_shrink_to_the_viewport() -> None:
    css = (
        FRONTEND_SOURCE / "data-sources/github-copilot/styles.css"
    ).read_text(encoding="utf-8")

    assert "grid-template-columns: minmax(0, 1fr)" in css
    assert ".copilot-combined-view > * { min-width: 0; grid-column: 1; }" in css
    assert ".copilot-combined-view > .finops-panel { width: 100%; grid-column: 1 / -1; }" in css
    assert ".copilot-grid > * { min-width: 0; }" in css
    assert ".copilot-dimension-toolbar { min-width: 0;" in css


def test_usage_report_trend_has_no_redundant_divider_or_wide_left_inset() -> None:
    source = (
        FRONTEND_SOURCE / "data-sources/github-copilot/pages/dashboard-page.tsx"
    ).read_text(encoding="utf-8")
    css = (
        FRONTEND_SOURCE / "data-sources/github-copilot/styles.css"
    ).read_text(encoding="utf-8")

    assert "tickMargin={6} width={44}" in source
    assert ".copilot-trend-panel > .finops-panel-title { border-bottom: 0; }" in css
    assert "padding: 8px 14px 14px 8px" in css


def test_copilot_ai_usage_keeps_its_owned_real_data_surface() -> None:
    source = (
        FRONTEND_SOURCE / "data-sources/github-copilot/pages/dashboard-page.tsx"
    ).read_text(encoding="utf-8")
    analytics = source.split("function Analytics", 1)[1].split(
        "function ImportedUsagePanel", 1
    )[0]

    assert '<ActivityPanel daily={daily} title="用量趋势" />' in analytics
    assert '<span className="finops-panel-title-meta">每日</span>' not in source
    for title in ("模型用量", "功能用量"):
        assert f'<BreakdownPanel title="{title}"' in analytics
    for title in ("语言用量", "IDE 用量"):
        assert f'<BreakdownPanel title="{title}"' not in analytics
    assert "function DimensionMatrix" not in source
    assert "function UsageMatrixMembers" in source
    assert 'aria-label="GitHub Copilot 成员用量矩阵"' in source


def test_copilot_usage_metrics_follows_a_decision_flow() -> None:
    source = (
        FRONTEND_SOURCE / "data-sources/github-copilot/pages/dashboard-page.tsx"
    ).read_text(encoding="utf-8")
    overview = source.split("function Overview", 1)[1].split("type BreakdownMetric", 1)[0]

    for series in ("activeUserSeries", "locSeries", "acceptanceRateSeries"):
        assert f"const {series}: DailySeries[]" in source
    for title in ("语言分布", "IDE 分布"):
        assert f'<BreakdownPanel title="{title}"' in overview
    assert '<BreakdownPanel title="模型用量"' not in overview
    assert "function BreakdownExplorer" in source
    assert "function AiCreditTable" in source
    assert "data.ai_credit_breakdown" in source
    assert 'aria-label="使用结构明细维度"' in source
    assert "function TopActiveUsersTable" in source
    assert 'aria-label="GitHub Copilot 活跃用户排行"' in source
    assert "categoryAxisWidth" in source
    assert "CategoryAxisTick" in source
    assert "useChartWidthKey" in source
    assert "width={axisWidth}" in source
    assert "width={108}" not in source
    assert 'from "../../../components/finops/chart-drag-zoom"' in source
    assert source.count("useChartDragZoom(") >= 2
    assert source.count("<ChartZoomReset") >= 2
    assert "活跃与规模" not in overview
    primary_chart = overview.split("<MetricsGroup columns={1}>", 1)[1].split(
        "</MetricsGroup>", 1
    )[0]
    assert 'defaultMode="bar"' in primary_chart

    markers = (
        '<section className="finops-kpis">',
        "<MetricsGroup columns={1}>",
        '<MetricsGroup title="采纳与生产力" columns={2}>',
        '<MetricsGroup title="使用结构" columns={2}>',
        "<BreakdownExplorer data={data} />",
        '<MetricsGroup title="人员与席位" columns={1}>',
        "<SubscriptionPanel data={data} />",
        "<TopActiveUsersTable",
    )
    positions = [overview.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_copilot_tooltips_do_not_draw_hover_backdrops() -> None:
    source = (
        FRONTEND_SOURCE / "data-sources/github-copilot/pages/dashboard-page.tsx"
    ).read_text(encoding="utf-8")
    tooltip_tags = source.split("<Tooltip")[1:]

    assert tooltip_tags
    assert all("cursor={false}" in tag.split("/>", 1)[0] for tag in tooltip_tags)


def test_chart_mode_buttons_use_the_compact_shared_size() -> None:
    css = (
        FRONTEND_SOURCE / "components/finops/chart-mode-toggle.css"
    ).read_text(encoding="utf-8")
    component = (
        FRONTEND_SOURCE / "components/finops/chart-mode-toggle.tsx"
    ).read_text(encoding="utf-8")

    assert "width: 26px; height: 26px" in css
    assert "<Icon size={14}" in component


def test_copilot_imported_pages_keep_reference_data_surfaces() -> None:
    source = (
        FRONTEND_SOURCE / "data-sources/github-copilot/pages/dashboard-page.tsx"
    ).read_text(encoding="utf-8")

    assert "function ImportedTrendPanel" in source
    assert 'title="CSV 产品分布"' in source
    assert 'title={data.source_kind === "ai_usage" ? "CSV 模型分布" : "CSV SKU 分布"}' in source
    assert 'title="CSV 组织分布"' in source
    assert 'title="CSV 成本中心分布"' in source
    assert "data.product_breakdown" in source
    assert "user.monthly_quota" in source
    assert "user.usage_percent" in source


def test_copilot_governance_pages_keep_operational_detail_surfaces() -> None:
    governance = (
        FRONTEND_SOURCE / "data-sources/github-copilot/pages/governance-page.tsx"
    ).read_text(encoding="utf-8")
    requests = (
        FRONTEND_SOURCE / "data-sources/github-copilot/pages/budget-page.tsx"
    ).read_text(encoding="utf-8")

    assert "function CostCenterUserMapping" in governance
    assert 'aria-label="GitHub 用户成本中心映射"' in governance
    assert 'label="团队外 Copilot 席位"' in governance
    assert "function BudgetScopeChart" in governance
    assert 'aria-label="请求状态"' in requests
    for label in ("请求总数", "待审核", "已批准", "批准金额", "待审核金额"):
        assert f'label="{label}"' in requests
