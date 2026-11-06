from tests.support.paths import (
    FRONTEND_SOURCE,
    read_apim_dashboard_source,
    read_frontend_styles,
)


def test_shared_tool_buttons_keep_styling_when_composed_as_triggers() -> None:
    button = (FRONTEND_SOURCE / "components/ui/button.tsx").read_text(encoding="utf-8")
    css = read_frontend_styles()
    shared_rule = css.split('[data-slot="button"] {', 1)[1].split("}", 1)[0]

    assert button.index("{...props}") < button.index("data-slot={slot}")
    assert 'requestedSlot === "alert-dialog-action"' in button
    assert 'requestedSlot === "alert-dialog-cancel"' in button
    assert ': "button"' in button
    assert "appearance: none;" in shared_rule
    assert (
        '[data-slot="button"][data-variant="ghost"] { '
        "background: transparent; color: inherit; }" in css
    )
    assert '[data-slot="button"][data-variant="ghost"]:hover:not(:disabled)' in css
    assert '[data-slot="button"][data-variant="ghost"][aria-expanded="true"]' in css
    assert '[data-slot="button"]:focus-visible' in css


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


def test_invocation_console_preserves_owner_directory_and_current_selection() -> None:
    source = read_apim_dashboard_source()

    assert "const { user: sessionUser } = useAuth();" in source
    assert "person.id === sessionUser.email" in source
    assert 'sessionUser.role === "owner"' in source
    assert 'useState(sessionUser?.email ?? users[0]?.id ?? "")' in source
    assert "if (users.some((person) => person.id === userId)) return" in source
    assert "finopsQueries.assignedModelRouters(userId)" in source
    assert 'items={users}' in source


def test_invocation_and_request_trace_use_direct_models() -> None:
    source = read_apim_dashboard_source()

    assert "runtime_id: explicitGateway ? undefined : model!.runtime_id" in source
    assert "model_id: model!.id" in source
    assert "gateway_profile_id: explicitGateway ? gatewaySelection.targetId! : undefined" in source
    assert (
        "gateway_deployment_id: explicitGateway ? gatewaySelection.deploymentId! : undefined"
        in source
    )
    assert "finopsQueries.assignedModelRouters(userId)" in source
    assert "router_id: routerId" in source


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
    filtered = (
        "requestRows.filter((item) => requestTraceMatches(item, normalizedSearch, statusFilter))",
        "pricedRows.filter((item) => requestTraceMatches(item, normalizedSearch, statusFilter))",
    )
    assert all(predicate in request_trace for predicate in filtered)
    assert (
        "const firstMatchingId = matchingRows[0] ? requestTraceId(matchingRows[0]) : null"
        in request_trace
    )
    assert (
        "const selectedMatches = matchingRows.some((item) => requestTraceId(item) === selected)"
        in request_trace
    )
    assert (
        "const nextSelection = keepDeepLink ? initialRequestId : firstMatchingId" in request_trace
    )
    assert "if (!selected && firstMatchingId)" not in request_trace
    assert "if (!selected && firstRequestId)" not in request_trace


def test_request_trace_selects_attempts_without_losing_the_caller_id() -> None:
    source = read_apim_dashboard_source()
    request_trace = source.split("function RequestTrace(", 1)[1].split(
        "function RequestTraceDetail", 1
    )[0]
    trace_row = source.split("function TraceRow(", 1)[1]

    assert (
        "const requestRows = query.data?.pages.flatMap((page) => page.items) ?? []"
        in request_trace
    )
    assert "key={requestTraceId(item)}" in request_trace
    assert "active={selected === requestTraceId(item)}" in request_trace
    assert "onClick={() => setSelected(requestTraceId(item))}" in request_trace
    assert "key={item.request_id}" not in request_trace
    assert "attemptsByRequest.get(item.request_id)" in request_trace
    assert "attemptPositions.set(requestTraceId(item)" in request_trace
    assert "left.timestamp.localeCompare(right.timestamp)" in request_trace
    assert "attempt={attemptPositions.get(requestTraceId(item))}" in request_trace
    assert 'Correlation ID: ${item.correlation_id ?? "未知"}' in trace_row
    assert "#${attempt.index}/${attempt.total}" in trace_row
    assert "item.request_id" in trace_row


def test_request_trace_cost_availability_is_per_request() -> None:
    source = read_apim_dashboard_source()
    request_trace = source.split("function RequestTrace(", 1)[1].split(
        "function RequestTraceDetail", 1
    )[0]

    assert "costAvailable: boolean" not in request_trace
    assert "costAvailable={estimateRequestCost(item, models) !== null}" in request_trace
    assert "costAvailable={estimateRequestCost(pricedDetail, models) !== null}" in request_trace
    assert "costAvailable={costAvailable}" not in request_trace


def test_request_trace_blocked_before_the_model_shows_zero_usage_and_cost() -> None:
    detail = read_apim_dashboard_source().split("function RequestTraceDetail(", 1)[1]

    # The blocked explanation says no tokens and no cost; the figures must agree with it.
    assert (
        'const blockedBeforeModel = admissionWarning?.tone === "blocked" '
        '|| modelWarning?.tone === "blocked";'
    ) in detail
    assert "const inputOutputMeasured = blockedBeforeModel || (request.usage_available" in detail
    assert ": costAvailable || blockedBeforeModel ? `" in detail


def test_pages_nest_no_main_landmark_and_name_model_options() -> None:
    nested = [
        str(path.relative_to(FRONTEND_SOURCE)) for path in FRONTEND_SOURCE.rglob("*.tsx")
        if path.name != "app.tsx" and "<main" in path.read_text(encoding="utf-8")
    ]
    budget = (FRONTEND_SOURCE / "data-sources/apim/pages/budget-page.tsx").read_text(
        encoding="utf-8"
    )

    assert nested == []
    assert (
        "toggleModel(model.id, checked === true)} aria-label={`选择模型：${model.display_name}`} />"
        in budget
    )


def test_request_trace_cost_formula_uses_the_recorded_request() -> None:
    source = read_apim_dashboard_source()
    calculation = source.split("export function RequestCostCalculation(", 1)[1].split(
        "function TraceFacts", 1
    )[0]
    assert "requestCostCalculation(request)" in calculation
    assert "models" not in calculation
    assert 'role="math"' in calculation
    assert "calculation.matchesRecordedCost" in calculation
    assert (
        'if (!calculation) return <span className="trace-cost-unavailable">'
        '{request.router_attempts.length ? "多次调用费用见路由明细" : '
        'request.input_price_per_million === undefined ? "当前后端未提供历史单价" : '
        '"历史单价不可用"}</span>' in calculation
    )
    css = read_frontend_styles()
    assert ".trace-cost-expression { display: flex; flex-wrap: wrap;" in css
    assert ".trace-cost-numerator { display: flex; flex-wrap: wrap;" in css


def test_recorded_cost_is_not_hidden_by_incomplete_registry_pricing() -> None:
    source = read_apim_dashboard_source()
    overview = source.split("function ExecutiveOverview(", 1)[1].split("function Kpi(", 1)[0]

    assert "const estimatedCost = totals.estimated_cost" in overview
    assert "const costAvailable = configuredCostAvailable || estimatedCost > 0" in overview
    assert "currency.format(estimatedCost)" in overview
    assert "unpricedModels.length > 0" in source


def test_model_success_rate_rankings_sort_highest_first() -> None:
    source = read_apim_dashboard_source()
    panels = source.split('title="模型成功率"')[1:]

    assert len(panels) == 2
    assert all("sortAscending" not in panel.split("/>", 1)[0] for panel in panels)
    assert "right.value - left.value" in source


def test_distribution_rankings_keep_same_named_models_apart() -> None:
    source = read_apim_dashboard_source()
    bars = source[source.index("function DistributionBars") :]
    bars = bars[: bars.index("\nfunction ")]

    assert 'return item.id && item.id !== "unattributed" ? item.id : item.name' in source
    assert "const key = distributionKey(item)" in bars
    assert "grouped.get(item.name)" not in bars
    for title in ("模型用量结构", "模型 Token 排行"):
        panel = source.split(f'title="{title}"', 1)[1].split("/>", 1)[0]
        assert "models.data?.items.map(distributionKey)" in panel
        # Same-named rows carry their connection name so they can be told apart.
        assert (
            "items={withConnectionNames(models.data?.items ?? [], registry.data?.models)}"
            in panel
        )
    labels = source[source.index("function withConnectionNames") :]
    labels = labels[: labels.index("\nfunction ")]
    assert "(counts.get(item.name) ?? 0) > 1 ? connections.get(item.id)" in labels
    assert "name: `${item.name} · ${connection}`" in labels


def test_mobile_trace_header_keeps_every_text_block_in_the_content_column() -> None:
    styles = read_frontend_styles()
    mobile = styles.split(
        ".trace-detail-hero, .trace-detail-hero.has-router { grid-template-columns: 38px", 1,
    )[1]
    # The model/time block has no class; it must not fall into the 38px status column.
    assert ".trace-detail-hero > div, .trace-detail-hero > strong { grid-column: 2;" in mobile


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
    assert "minWidths={[180, 100, 140, 160, 160, 94, 88, 52]}" in budget_source
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
    assert "normalizePageForSource(next, page)" in app_source
    assert "export function normalizeApimPage" in apim_source
    assert 'return apimPageIds.has(page) ? page : "finops-overview"' in apim_source
    assert "function AccessPointFilter" not in dashboard_source
    assert "runtimeAccessPoints" not in dashboard_source
    assert '"接入来源"' not in dashboard_source


def test_report_sidebar_group_uses_the_resource_name_not_the_pin_state() -> None:
    app_source = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    pin_dialog = (
        FRONTEND_SOURCE / "components/assistant/pin-dialog.tsx"
    ).read_text(encoding="utf-8")
    english = (FRONTEND_SOURCE / "locales/en/phrases-core.ts").read_text(
        encoding="utf-8"
    )

    assert "<span>报表</span>" in app_source
    assert "<span>固定</span>" not in app_source
    assert "左侧导航的「报表」分组" in pin_dialog
    assert '"报表": "Reports"' in english
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


def test_request_trace_shows_small_costs_with_invocation_precision() -> None:
    shared = (FRONTEND_SOURCE / "data-sources/apim/pages/dashboard-shared.tsx").read_text(
        encoding="utf-8"
    )
    source = read_apim_dashboard_source()
    trace = source.split("function RequestTrace(", 1)[1]

    # $0.0000118 must read $0.000012 like the invocation page, not $0.00.
    request_cost = shared.split("export const requestCost = ", 1)[1].split("})", 1)[0]
    assert 'currency: "USD"' in request_cost
    assert "maximumFractionDigits: 6" in request_cost
    assert "currency.format(" not in trace
    assert trace.count("requestCost.format(") >= 5


def _budget_source() -> str:
    return (FRONTEND_SOURCE / "data-sources/apim/pages/budget-page.tsx").read_text(
        encoding="utf-8"
    )


def test_budget_remaining_without_an_organization_budget_is_not_an_overrun() -> None:
    # With no organization budget, "remaining" is unknown, not exceeded.
    assert (
        '!allocatedBudget && !usdOrganizations.length ? "等待组织预算" : '
        'remainingTokens < 0 ? "已超过组织预算"' in _budget_source()
    )


def test_department_budget_dialog_counts_person_allocations() -> None:
    # The page list omits people, so "allocated to children" read 0 for departments.
    budget = _budget_source()

    assert "enabled: previewRequired || departmentScope," in budget
    assert "departmentScope ? allocationTree.data?.items ?? items : items" in budget
    assert "disabled={busy || childrenLoading || constraint.childAllocated > 0}" in budget


def test_budget_removal_and_block_changes_ask_for_confirmation() -> None:
    budget = _budget_source()

    assert "onClick={onDelete}" not in budget
    assert "onClick={() => setConfirmRemove(true)}" in budget
    assert "onConfirm={() => { setConfirmRemove(false); onDelete() }}" in budget
    assert (
        "onToggleEnforcement={(departmentId, mode) => setEnforcementTarget({ departmentId, mode })}"
        in budget
    )
    assert "enforcement.mutate(enforcementTarget); setEnforcementTarget(null)" in budget


def test_person_budgets_can_be_removed_individually() -> None:
    budget = _budget_source()

    assert 'dataSource.deleteBudget(period, "user", personId)' in budget
    assert "onRemove={(personId) => removePerson.mutate(personId)}" in budget
    assert "onRemove(singlePerson.scope_id)" in budget


def test_people_allocation_checks_the_balance_and_explains_conflicts() -> None:
    # The server's 409 used to surface as raw "API 409: ...".
    budget = _budget_source()

    assert "selectedItems.reduce((total, item) => total + (item.token_limit ?? 0), 0)" in budget
    assert "selectedItems.reduce((total, item) => total + cents(item.usd_limit), 0)" in budget
    assert "error={bulk.error ? peopleAllocationError(bulk.error)" in budget
    assert "error instanceof ApiError && error.status === 409 && error.detail" in budget
    assert (
        '"User allocations would exceed the department budget": "人员额度合计超过部门可用预算"'
    ) in budget


def test_deleting_an_assistant_conversation_asks_for_confirmation() -> None:
    conversation = (FRONTEND_SOURCE / "components/assistant/assistant-conversation.tsx").read_text(
        encoding="utf-8"
    )
    for path in ("components/assistant/assistant-panel.tsx", "pages/assistant-page.tsx"):
        surface = (FRONTEND_SOURCE / path).read_text(encoding="utf-8")
        assert "c.removeConversation.mutate(item.id)" not in surface
        assert "<ConversationDeleteDialog controller={c} />" in surface
    assert "c.removeConversation.mutate(c.deleteTarget)" in conversation


def test_invocation_targets_are_limited_to_models_the_person_may_call() -> None:
    source = read_apim_dashboard_source()

    assert "personMayInvoke(model, assignedModelIds)" in source
    assert "const invocationModel = resolveInvocationModel(\n    invocableModels," in source
    assert "const models = invocableModels.filter(" in source
    assert "registry.data?.models.filter((item) => item.enabled" not in source


def test_same_named_invocation_targets_show_their_model_key() -> None:
    # A Pool primary and its replica both read "gpt-5.4-mini · Microsoft Foundry".
    source = read_apim_dashboard_source()

    assert (
        "{models.some((other) => other.id !== model.id && other.display_name === "
        "model.display_name) && <small data-no-localize>{model.model_key}</small>}" in source
    )
