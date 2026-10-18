from tests.support.paths import FRONTEND_SOURCE, read_frontend_styles


def test_release_selection_preserves_matching_current_inventory_deep_link() -> None:
    page = (FRONTEND_SOURCE / "pages/gateway-releases-page.tsx").read_text(encoding="utf-8")
    assert "if (selectedId && items.some((release) => release.id === selectedId)) return" in page
    assert "[items, selectedId]" in page


def test_rollback_confirmation_uses_destructive_semantics_and_localized_count() -> None:
    page = (FRONTEND_SOURCE / "pages/gateway-releases-page.tsx").read_text(encoding="utf-8")
    styles = read_frontend_styles()
    assert (
        '<AlertDialogAction variant="destructive" '
        'className="gateway-release-rollback-confirm"' in page
    )
    assert (
        '<dd data-no-localize>{changeCount(rollbackPreview.data.changes)'
        '.toLocaleString(getIntlLocale())}</dd>' in page
    )
    assert (
        '.gateway-release-rollback-dialog [data-slot="alert-dialog-action"]:focus-visible' in styles
    )
    assert '.dark .gateway-release-rollback-dialog .gateway-release-rollback-confirm' in styles
    assert 'height: 32px; border: 1px solid transparent; border-radius: 7px;' in styles
    assert 'pointer-events: none; opacity: .5; cursor: not-allowed;' in styles


def test_gateway_releases_are_a_first_class_guarded_model_platform_page() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages/gateway-releases-page.tsx").read_text(
        encoding="utf-8"
    )
    source = (FRONTEND_SOURCE / "data-sources/apim/source.tsx").read_text(
        encoding="utf-8"
    )
    api = (FRONTEND_SOURCE / "data-sources/apim/api.ts").read_text(encoding="utf-8")
    queries = (FRONTEND_SOURCE / "data-sources/apim/queries.ts").read_text(
        encoding="utf-8"
    )
    styles = read_frontend_styles()

    assert '"gateway-releases"' in source
    assert '{ label: "网关发布", icon: History, page: "gateway-releases" }' in app
    assert 'const [gatewayReleaseDrawerOpen, setGatewayReleaseDrawerOpen]' in app
    assert 'aria-label="打开网关发布列表"' in app
    assert "releaseDrawerOpen={gatewayReleaseDrawerOpen}" in app
    assert '"/api/v1/model-management/releases?limit=100"' in api
    assert "/integrity`" in api
    assert "/diff${againstId" in api
    assert "/integrity-checks`" in api
    assert "/rollback-preview`" in api
    assert "/rollback`" in api
    assert "/gc-plans`" in api
    assert "gatewayReleases: () => queryOptions" in queries
    assert "gatewayRelease: (id: string | null) => queryOptions" in queries
    assert '(item) => item.role === "in_progress"' in queries
    assert 'query.state.data?.role === "in_progress" ? 2_000 : false' in queries
    assert "gatewayReleaseOperation: (id: string | null) => queryOptions" in queries
    assert "const roleLabels" in page
    assert 'current: "当前基线"' in page
    assert 'immediate_rollback: "立即回滚版本"' in page
    assert 'pinned: "已固定"' in page
    assert 'expired: "已过期"' in page
    assert "相对基础版本的变更" in page
    assert 'planned ? "拟新增" : "已新增"' in page
    assert 'planned ? "拟移除" : "已移除"' in page
    assert 'planned ? "拟变更" : "已变更"' in page
    assert 'plannedChanges ? "计划变更" : "相对基础版本的变更"' in page
    assert 'release.audit.some(event => event.to_status === "active")' in page
    assert '.gateway-release-changes-card[data-state="planned"]' in styles
    assert ">+ {value}</span>" not in page
    assert ">− {value}</span>" not in page
    assert ">~ {value}</span>" not in page
    assert "<Plus" not in page
    assert "<Minus" not in page
    assert "<Pencil" not in page
    change_code_styles = styles.split(
        ".gateway-release-change-list code {", 1
    )[1].split("}", 1)[0]
    assert "color: var(--foreground);" in change_code_styles
    assert "font-size: 11px;" in change_code_styles
    assert "line-height: 16px;" in change_code_styles
    assert "color: inherit;" not in change_code_styles
    assert ".gateway-release-change-list .removed b { background: color-mix(" in styles
    assert "线上配置状态" in page
    assert "回滚就绪状态" in page
    assert 'isCurrentRelease ? "配置差异" : "回滚阻塞项"' in page
    assert "当前 APIM 配置与此 Release 的记录不同" in page
    assert "使用此 Release 回滚前，先运行 Azure 实时检查" in page
    assert "发布审计" in page
    assert "formatAuditTimestamp(event.created_at, timezone)" in page
    assert 'className="gateway-release-card gateway-release-actions-card"' in page
    assert 'className="gateway-release-detail-grid"' in page
    assert 'className="gateway-release-detail-main"' in page
    assert 'className="gateway-release-summary-card"' in page
    assert (
        ".gateway-release-detail-head, .gateway-release-card-head { min-height: 42px;"
        in styles
    )
    assert (
        ".gateway-release-identity-line h2, .gateway-release-card h2 { "
        "margin: 0; font-size: 13px; line-height: 18px; font-weight: 600; }" in styles
    )
    assert "padding: 10px 16px; border-bottom: 1px solid var(--border);" in styles
    assert 'className="gateway-release-identity-line"' in page
    assert "<span><History size={18} /></span>" not in page
    assert (
        '<h2>{publicationKindLabels[release.publication_kind]} '
        '<span data-no-localize>#{release.generation}</span></h2><ReleaseRole '
        'role={release.role} status={release.status} hideSuperseded={isModelRelease} />' in page
    )
    assert '!isModelRelease && release.role !== "current"' in page
    assert 'rollback_blockers.includes("model_release_not_gateway_rollback_target")' in page
    assert '<RollbackConfirmationHeader preview={rollbackPreview.data} />' in page
    assert '不修改网关 Policy 或切换 Revision' in page
    assert "release.gateway_name" in page
    assert 'className="gateway-release-card gateway-release-changes-card"' in page
    assert 'className="gateway-release-card gateway-release-dependency-card"' in page
    assert 'className="gateway-release-detail-rail"' in page
    assert 'className="gateway-release-card gateway-release-audit-card"' in page
    assert "gateway-release-band-disclosure" not in page
    assert "confirmation_sha256" in api
    assert "rollback_eligible" in page
    assert 'user?.role === "owner"' in page
    assert "失败时自动恢复当前版本" in page
    assert "Release Worker 未部署" in page
    assert 'const gatewayOperation = operation.data?.operation_kind === "gc_plan"' in page
    assert 'operation.data?.target_release_id === selectedId' in page
    assert "<GatewayCleanupStatus operation={gatewayOperation} />" in page
    assert "operation={releaseOperation}" in page
    assert "operation={operation.data}" not in page
    assert "className={`gateway-release-gateway-operation" in page
    assert 'className="gateway-release-mobile-maintenance"' in page
    assert "扫描闲置资源" in page
    assert '<ShieldCheck size={13} /><span>检查依赖</span>' in page
    assert '<ScanSearch size={13} /><span>检查依赖</span>' not in page
    assert "onGcPlan" not in page
    assert "已排队，尚未开始" in page
    assert "正在等待 Publication Worker 处理。" in page
    assert "operation.data?.worker_available" in page
    assert "query.state.data?.worker_available === false" in queries
    assert "useResizablePane" in page
    assert "useIsMobile" in page
    assert 'if (hideSuperseded && role === "superseded") return null' in page
    assert "gateway-release-list-count" not in page
    assert "ChevronRight" in page
    assert "const releaseGroups = useMemo(() =>" in page
    assert "groups.get(release.gateway_profile_id)" in page
    assert 'className="gateway-release-group-trigger"' in page
    assert "aria-expanded={!collapsed}" in page
    assert '<i aria-hidden="true" />' in page
    assert "hidden={collapsed}" in page
    assert "Revision pending" not in page
    assert "releaseIndicatorRoles.has(role)" in page
    assert '"rolled_back"' in page.split("const releaseIndicatorRoles", 1)[1].split("]", 1)[0]
    assert '"expired"' in page.split("const releaseIndicatorRoles", 1)[1].split("]", 1)[0]
    assert '"superseded"' not in page.split("const releaseIndicatorRoles", 1)[1].split("]", 1)[0]
    assert (
        "const statusLabel = releaseIndicatorRoles.has(release.role) "
        "? roleLabels[release.role] : undefined" in page
    )
    assert (
        'className="gateway-release-list-leading" data-role={release.role} '
        'role={statusLabel ? "img" : undefined} aria-label={statusLabel} '
        'title={statusLabel}><History size={15} aria-hidden="true" />' in page
    )
    assert '<ReleaseRole role={release.role} status={release.status} hideSuperseded />' not in page
    assert 'value !== "all" && <ReleaseStatusDot role={value} />' in page
    assert '<ReleaseStatusDot role={value} reserve /><span>{roleLabels[value]}</span>' in page
    assert 'role === "in_progress" ? auditStatusLabels[status]' in page
    assert "release.completed_at ?? release.created_at" in page
    assert 'const primaryFilters: ReleaseFilter[] = ["all", "current"]' in page
    assert (
        'const secondaryFilters: GatewayReleaseRole[] = ["immediate_rollback", '
        '"in_progress", "pinned"' in page
    )
    secondary_filters = page.split(
        "const secondaryFilters: GatewayReleaseRole[] = [", 1
    )[1].split("]", 1)[0]
    assert secondary_filters.rstrip().endswith('"superseded"')
    assert '<Dialog open={releaseDrawerOpen}' in page
    assert "portalContainer={closeOnSelect ? releaseDrawerPortalRef : undefined}" in page
    assert 'className="gateway-release-drawer-portals"' in page
    assert "showMobileActions={isMobile}" in page
    assert '!isMobile && <aside className="gateway-release-list-pane">' in page
    assert 'className="runtime-pane-handle"' in page
    assert ".gateway-release-layout {" in styles
    assert "grid-template-columns: var(--gateway-release-pane-width, 336px)" in styles
    assert "@media (max-width: 767px)" in styles
    assert ".gateway-release-layout > .runtime-pane-handle { display: none; }" in styles
    assert ".gateway-release-gateway-operation { min-height: 54px;" in styles
    assert "padding: 10px 16px;" in styles
    assert (
        ".gateway-release-gateway-operation > div { min-width: 0; "
        "display: grid; gap: 2px; }" in styles
    )
    assert ".gateway-release-mobile-maintenance { display: none; }" in styles
    assert ".gateway-release-mobile-maintenance { min-height: 46px; display: flex;" in styles
    assert ".gateway-release-drawer-list.gateway-release-list-pane" in styles
    assert ".gateway-release-group + .gateway-release-group { margin-top: 8px; }" in styles
    assert (
        ".gateway-release-group-trigger > i { min-width: 12px; "
        "height: 1px; background: var(--border); }" in styles
    )
    assert (
        '.gateway-release-group-trigger[aria-expanded="true"] '
        ".gateway-release-group-caret" in styles
    )
    assert ".gateway-release-group-content[hidden] { display: none; }" in styles
    assert "grid-template-columns: 28px minmax(0, 1fr);" in styles
    assert ".gateway-release-status-dot.current { background: var(--success); }" in styles
    assert ".gateway-release-status-dot.immediate_rollback { background: var(--brand); }" in styles
    assert ".gateway-release-status-dot.rolled_back { background: var(--chart-2); }" in styles
    assert ".gateway-release-status-dot.empty { visibility: hidden; }" in styles
    assert (
        ".gateway-release-list-copy small { min-width: 0; display: flex; "
        "flex-wrap: nowrap;" in styles
    )
    assert ".gateway-release-list-copy small span:last-child" in styles
    assert (
        ".gateway-release-detail-grid { display: grid; grid-template-columns: "
        "minmax(0, 1fr) 320px; gap: 12px; padding: 12px; }" in styles
    )
    assert ".gateway-release-detail-grid { gap: 10px; padding: 10px; }" in styles
    assert ".gateway-release-facts { grid-template-columns: 1fr; }" not in styles
    assert (
        ".gateway-release-actions-mobile { width: 100%; display: grid; "
        "grid-template-columns: repeat(2, minmax(0, 1fr)); }" in styles
    )
    assert ".gateway-release-summary-card, .gateway-release-card" in styles
    assert "border-radius: var(--radius); background: var(--card);" in styles
    assert "grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 0;" in styles
    assert ".gateway-release-card-head" in styles
    assert (
        ".gateway-release-dependency-card .gateway-release-card-head > span { "
        "min-height: 22px; display: inline-flex;" in styles
    )
    assert (
        '.gateway-release-card-head[data-state="mismatched"] > span { '
        "background: color-mix(in oklch, var(--warning) 10%, transparent); "
        "color: var(--warning); }" in styles
    )
    assert (
        '.gateway-release-card-head[data-state="mismatched"] > span { '
        "color: var(--destructive); }" not in styles
    )
    assert ".gateway-release-card h2 { margin: 0; font-size: 13px; line-height: 18px;" in styles
    assert "font-size: 12px; line-height: 18px; font-weight: 600; list-style: none;" in styles
    assert ".gateway-release-timeline b { font-size: 12px; line-height: 18px; }" in styles
    assert "font-size: 11px; line-height: 16px; white-space: nowrap;" in styles
    assert (
        ".gateway-release-integrity { min-height: 52px; display: flex; "
        "align-items: center; padding: 10px 16px; }" in styles
    )
    assert "font-size: 12px; line-height: 18px;" in styles
    assert ".gateway-release-disclosure { margin: 0; overflow: hidden; border: 0;" in styles
    assert ".gateway-release-disclosure summary:not(:has(> small))::after" in styles
    assert (
        ".gateway-release-audit-card .gateway-release-timeline { padding: "
        "16px 16px 12px; }" in styles
    )
    assert (
        ".gateway-release-actions-rail .gateway-release-action > svg { "
        "flex: 0 0 auto; }" in styles
    )
    assert ".gateway-release-timeline > div:not(:last-child)::after" in styles
    assert "top: 14px; bottom: -9px; left: 4px; width: 1px;" in styles
    assert ".gateway-release-timeline > div > i" in styles
    assert ".gateway-release-timeline { position: relative; display: grid; gap: 4px; }" in styles
    assert (
        ".gateway-release-timeline > div { position: relative; min-width: 0; "
        "min-height: 44px; display: grid;" in styles
    )
    assert ".gateway-release-timeline::before" not in styles
    assert ".gateway-release-change-column { min-width: 0; padding: 14px 16px 16px; }" in styles
    assert ".gateway-release-change-column { min-width: 0; min-height: 96px;" not in styles
    assert ".gateway-release-list-copy > span { flex-wrap: wrap;" in styles


def test_gateway_release_locales_cover_the_visible_safety_language() -> None:
    labels = (
        "网关发布",
        "打开网关发布列表",
        "未完成",
        "创建时间",
        "已排队，尚未开始",
        "正在等待 Publication Worker 处理。",
        "只读",
        "当前版本",
        "立即回滚版本",
        "已固定",
        "已过期",
        "相对基础版本的变更",
        "已新增",
        "已移除",
        "已变更",
        "线上配置状态",
        "回滚就绪状态",
        "发现漂移",
        "需要检查",
        "配置差异",
        "回滚阻塞项",
        "当前 APIM 配置与此 Release 的记录不同。当前流量不一定受影响，但需要协调配置漂移。",
        "使用此 Release 回滚前，先运行 Azure 实时检查。",
        "发布审计",
        "搜索网关发布",
        "确认网关回滚",
        "确认回滚",
        "清理计划",
        "扫描闲置资源",
        "扫描未启动",
        "闲置资源扫描失败",
        "闲置资源扫描完成",
        "正在扫描闲置 APIM 资源",
        "等待 Release Worker 开始扫描。",
        "正在读取 APIM 配置并构建完整引用图。",
        "Release Worker 未部署，扫描未被处理。",
        "已完成整个 Gateway 的 APIM 引用检查。",
        "个清理候选；未删除资源",
        "Release Worker 未部署",
        "检查依赖、回滚和清理计划暂不可用。",
        "操作未启动",
        "Release Worker 未部署，操作未被处理。",
    )
    for locale in ("en", "ja", "ko"):
        catalog = (
            FRONTEND_SOURCE / "locales" / locale / "phrases-core.ts"
        ).read_text(encoding="utf-8")
        assert not [label for label in labels if f'"{label}":' not in catalog]
