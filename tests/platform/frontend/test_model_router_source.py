import json

from tests.support.paths import (
    FRONTEND_SOURCE,
    REPOSITORY_ROOT,
    read_apim_dashboard_source,
    read_frontend_styles,
)


def test_model_router_is_an_apim_model_platform_workspace() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    source = (
        FRONTEND_SOURCE / "data-sources/apim/source.tsx"
    ).read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages/model-router-page.tsx").read_text(
        encoding="utf-8"
    )
    resizable_pane = (
        FRONTEND_SOURCE / "components/ui/use-resizable-pane.ts"
    ).read_text(encoding="utf-8")

    assert '| "model-router"' in app
    assert '{ label: "模型智能路由", icon: Workflow, page: "model-router" }' in app
    assert 'page === "model-router" && <ModelRouterPage routerDrawerOpen=' in app
    assert '"model-router",' in source
    assert "finopsQueries.modelRouters()" in page
    assert "finopsQueries.modelRouterCatalog()" in page
    assert "allowedIds.has(model.id)" in page
    assert "useResizablePane" in page
    assert "localStorage.getItem(storageKey)" in resizable_pane
    assert "localStorage.setItem(storageKey" in resizable_pane
    assert "resizeWithKeyboard" in resizable_pane
    assert "demo" not in page.lower()
    assert "mock" not in page.lower()


def test_model_router_lifecycle_uses_the_real_typed_api() -> None:
    api = (FRONTEND_SOURCE / "data-sources/apim/api.ts").read_text(encoding="utf-8")
    queries = (
        FRONTEND_SOURCE / "data-sources/apim/queries.ts"
    ).read_text(encoding="utf-8")
    types = (
        FRONTEND_SOURCE / "data-sources/apim/types.ts"
    ).read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages/model-router-page.tsx").read_text(
        encoding="utf-8"
    )

    for operation in (
        "modelRouters",
        "modelRouterCatalog",
        "saveModelRouter",
        "duplicateModelRouter",
        "validateModelRouter",
        "publishModelRouter",
        "disableModelRouter",
        "deleteModelRouter",
    ):
        assert f"{operation}:" in api
    assert 'modelRouters: ["model-platform", "model-routers"]' in queries
    assert "export type ModelRouterDefinition" in types
    assert "export type ModelRouterValidation" in types
    assert "persistDraft" in page
    assert "runValidation" in page
    assert "runPublish" in page
    assert (
        "const [savedDraftSignature, setSavedDraftSignature] = "
        "useState<string | null>(null)" in page
    )
    assert "serializedWrite(draft) !== savedDraftSignature" in page
    assert "setSavedDraftSignature(serializedWrite(nextDraft))" in page
    assert "storedWriteFromRouter" not in page
    assert "放弃未保存的修改" in page
    assert "已发布版本记录会保留用于审计" in page


def test_model_router_menu_has_consistent_icons_and_destructive_styling() -> None:
    page = (FRONTEND_SOURCE / "pages/model-router-page.tsx").read_text(encoding="utf-8")
    styles = read_frontend_styles()
    menu = page.split(
        '<DropdownMenuContent className="router-list-menu-content"', 1
    )[1].split("</DropdownMenuContent>", 1)[0]

    for icon in ("Pencil", "Copy", "Users", "PowerOff", "Trash2"):
        assert f"<{icon} size={{14}} />" in menu
    assert '<DropdownMenuItem variant="destructive"' in menu
    selector = (
        '.router-list-menu-content [data-slot="dropdown-menu-item"]'
        '[data-variant="destructive"]'
    )
    assert f"{selector} {{ color: var(--destructive); }}" in styles
    assert f"{selector}:focus," in styles
    assert f'{selector}[data-highlighted]' in styles


def test_model_router_mobile_list_reuses_the_existing_drawer() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages/model-router-page.tsx").read_text(encoding="utf-8")
    styles = read_frontend_styles()

    assert "setModelRouterDrawerOpen(true)" in app
    assert "onRouterDrawerOpenChange={setModelRouterDrawerOpen}" in app
    assert "const isMobile = useIsMobile()" in page
    assert "{!isMobile && routerList}" in page
    assert '{!isMobile && <button' in page
    assert '<Dialog open={routerDrawerOpen} onOpenChange={onRouterDrawerOpenChange}>' in page
    assert 'className="apim-native-route-drawer model-router-drawer"' in page
    assert 'className={isMobile ? "apim-native-route-drawer-list" : undefined}' in page
    assert "onRouterDrawerOpenChange(false)\n    if (routerId === selectedId) return" in page
    assert "setPendingSelection(routerId)" in page
    assert 'setMobileActionsTarget(document.getElementById("mobile-topbar-end-actions"))' in page
    assert 'isMobile && mobileActionsTarget && createPortal(<Button' in page
    assert 'variant="ghost" size="icon-sm" className="settings-mobile-topbar-end-action"' in page
    assert '<Plus size={16} /></Button>, mobileActionsTarget)' in page
    assert '.model-router-workspace > .smh-page-header { display: none; }' in styles


def test_model_router_mobile_settings_use_a_drawer() -> None:
    canvas = (
        FRONTEND_SOURCE / "components/model-router/router-canvas.tsx"
    ).read_text(encoding="utf-8")
    styles = read_frontend_styles()

    assert "const isMobile = useIsMobile()" in canvas
    assert "const [propertiesOpen, setPropertiesOpen] = useState(false)" in canvas
    assert 'className="router-settings-trigger"' in canvas
    assert "onSelectedNodeChange={selectNode}" in canvas
    assert "if (isMobile && nodeId) setPropertiesOpen(true)" in canvas
    assert "{!isMobile && properties}" in canvas
    assert '<Dialog open={propertiesOpen} onOpenChange={setPropertiesOpen}>' in canvas
    assert 'className="apim-native-route-drawer router-settings-drawer"' in canvas
    assert ".router-settings-drawer { left: auto; right: 0;" in styles
    assert ".router-settings-drawer .router-properties > header { display: none; }" in styles
    assert ".router-settings-drawer .router-properties { min-height: 0; max-height: none;" in styles


def test_model_router_embeds_real_apim_deployment_resilience() -> None:
    api = (FRONTEND_SOURCE / "data-sources/apim/api.ts").read_text(encoding="utf-8")
    assert (
        "backend_pool_session_affinity_supported: "
        "registry.backend_pool_session_affinity_supported === true"
    ) in api
    queries = (
        FRONTEND_SOURCE / "data-sources/apim/queries.ts"
    ).read_text(encoding="utf-8")
    types = (
        FRONTEND_SOURCE / "data-sources/apim/types.ts"
    ).read_text(encoding="utf-8")
    strategy = (
        FRONTEND_SOURCE / "components/model-router/strategy-model-editor.tsx"
    ).read_text(encoding="utf-8")
    canvas = (
        FRONTEND_SOURCE / "components/model-router/router-canvas.tsx"
    ).read_text(encoding="utf-8")
    resilience = (
        FRONTEND_SOURCE
        / "components/model-management/deployment-resilience-editor.tsx"
    ).read_text(encoding="utf-8")
    eligibility = (
        FRONTEND_SOURCE
        / "components/model-management/apim-native-route-eligibility.ts"
    ).read_text(encoding="utf-8")
    native_routes = (
        FRONTEND_SOURCE / "pages/apim-native-routes-page.tsx"
    ).read_text(encoding="utf-8")
    styles = read_frontend_styles()
    button = (FRONTEND_SOURCE / "components/ui/button.tsx").read_text(encoding="utf-8")

    assert "export type GatewayBackendPoolConfig" in types
    assert "failure_count: 1" in types
    assert "modelBackendPool:" in api
    assert "saveModelBackendPool:" in api
    assert "deleteModelBackendPool:" in api
    assert "/backend-pool`" in api
    assert "error instanceof ApiError && error.status === 404" in api
    assert "modelBackendPool: (id: string)" in queries
    assert "enabled: Boolean(id)" in queries
    assert "DeploymentResilienceEditor" not in strategy
    assert "配置运行约束与部署韧性" not in strategy
    assert "收起运行约束与部署韧性" not in strategy
    assert "DeploymentResilienceEditor" not in canvas
    assert "useQueries" not in canvas
    assert "finopsQueries.modelBackendPool" not in canvas
    assert "ApimStrategyPanel" not in canvas
    assert "APIM Pool</button>" not in canvas
    assert "strategyPanelTab" not in canvas
    assert "apimPanelMounted" not in canvas
    assert ".router-node-strategy-badges" not in styles
    assert "DeploymentResilienceEditor" in native_routes
    assert "finopsQueries.modelBackendPools()" in native_routes
    assert "hasNativeRouteCounterpart" not in native_routes
    assert "equivalentNativeRouteRuntimes" not in native_routes
    assert "APIM_DEPLOYMENT_STRATEGY_NODE_ID" not in canvas
    assert 'type: "deploymentStrategy"' not in canvas
    assert "deploymentStrategyProjection" not in canvas
    assert "Math.max(propertiesWidth, 400)" not in canvas
    assert '"--router-properties-width": `${propertiesWidth}px`' in canvas
    assert "aria-valuenow={propertiesWidth}" in canvas
    assert "const ROUTER_CANVAS_MIN_ZOOM = 0.1" in canvas
    assert "minZoom={ROUTER_CANVAS_MIN_ZOOM}" in canvas
    assert "0.75" not in canvas
    assert "...finopsQueries.modelBackendPool(model.id)" in resilience
    assert "getQueryData<GatewayBackendPoolSummary[]>(finopsKeys.modelBackendPools)" in resilience
    assert "enabled: canManage && (!pool.data || membersExpanded)" in resilience
    assert "const canConfigure = canManage && pool.isSuccess" in resilience
    assert "useQuery(finopsQueries.gatewayPublication(publicationId))" in resilience
    assert 'const canManage = user?.role === "owner"' in resilience
    assert "dataSource.saveModelBackendPool(" in resilience
    assert "poolPublicationPayload(draft, affinitySupported)" in resilience
    assert "registry.data?.backend_pool_session_affinity_supported === true" in resilience
    assert "session_affinity: pool.session_affinity === true" in resilience
    assert "dataSource.deleteModelBackendPool(model.id)" in resilience
    assert "createPortal" not in resilience
    assert "headerControlsTarget" not in resilience
    assert "renderFrame?: (editor: ReactNode, controls: ReactNode) => ReactNode" in resilience
    assert "renderFrame ? renderFrame(editor, externalControls) : editor" in resilience
    assert "!renderFrame && (disableAction || changeActions)" in resilience
    assert "<footer>{disableAction}{changeActions}</footer>" in resilience
    assert "renderFrame={(editor, controls)" in native_routes
    assert 'className="apim-native-route-toolbar-actions">{controls}' in native_routes
    assert 'className="apim-native-route-toolbar"' in native_routes
    assert "apim-native-route-breadcrumb" not in native_routes
    assert 'className="runtime-detail-hero apim-native-route-hero"' in native_routes
    assert 'className="runtime-detail-sidecard apim-native-route-config-card"' in native_routes
    assert "apim-native-route-rail" not in native_routes
    assert "TERMINAL_PUBLICATION_STATUSES" in resilience
    assert 'type PublicationAction = "configure" | "remove"' in resilience
    assert 'publicationAction === "remove"' in resilience
    assert "APIM 后端池已移除，直接后端已恢复" in resilience
    assert "finopsQueries.modelBackendPoolCandidates(model.id)" in resilience
    assert "poolDeploymentModelId" in resilience
    assert "runtime.provider_id !== primary.provider_id" in eligibility
    assert "runtime.gateway_profile_id !== primary.gateway_profile_id" in eligibility
    assert "authCompatible(runtime, primary)" in eligibility
    assert "nativeRouteRuntimeIssue" in eligibility
    assert "runtimeIdsWithDeployment.has(runtime.id)" in eligibility
    assert "candidate.enabled" in eligibility
    assert "nativeRouteUpstreamIdentity(candidate) === upstream" in eligibility
    assert 'primaryAuth === "managed_identity"' in eligibility
    assert 'runtimeAuth === "named_value_api_key"' in eligibility
    assert 'configValue(runtime, "credential_provisioned", false) !== true' in eligibility
    assert "effectiveMemberIds.has(deployment.model_id)" in resilience
    assert "verifiedIds.has(deployment.model_id)" in resilience
    assert "toggleDeployment" in resilience
    assert "管理成员" in resilience
    assert "主部署（必选）" in resilience
    assert 'const showCandidates = !hasEffectivePool || membersExpanded' in resilience
    assert resilience.index('"池成员" : "选择部署"') < resilience.index("<b>部署模式</b>")
    assert 'runtime.health_status === "available"' not in resilience
    assert 'runtime.health_status === "unavailable"' not in resilience
    assert '"未检查"' not in resilience
    for invariant in (
        "api_format",
        "backend_path",
        "auth_strategy",
        "named_value_name",
        "managed_identity_resource",
        "streaming_mode",
    ):
        assert invariant in eligibility
    assert "draft.members.length >= 2 && draft.members.length <= 30" in resilience
    assert "Trash2" in resilience
    assert "PowerOff" not in resilience
    assert "focus-visible:border-transparent" in button
    for size, pixels in ((6, 24), (7, 28), (8, 32), (9, 36)):
        assert (
            f'[data-slot="button"].size-{size} {{ width: {pixels}px; '
            f"min-width: {pixels}px; height: {pixels}px; padding: 0; }}"
        ) in styles
    assert (
        ".apim-native-route-toolbar-actions .apim-native-route-disable { "
        "height: 28px; gap: 6px"
    ) in styles
    assert (
        "background: color-mix(in oklch, var(--destructive) 10%, transparent) "
        "!important"
    ) in styles
    assert (
        ".apim-native-route-toolbar-actions "
        ".apim-native-route-disable:hover:not(:disabled) { background: "
        "color-mix(in oklch, var(--destructive) 16%, transparent) !important; }"
    ) in styles
    assert "button.router-node-action-destructive { border: 1px solid transparent" in styles
    assert ".anomaly-rule-delete { border-color: transparent" in styles
    assert (
        'status[data-active="true"] { border-color: var(--success); '
        "background: var(--background)"
    ) in styles
    assert 'className="router-action-primary"' in resilience
    assert "<Send size={13} />" in resilience
    assert ".apim-native-route-change-actions > button.router-action-primary" in styles
    assert "dirty || configuring" in resilience
    assert "dirty && !configuring" in resilience
    assert "disabled={busy || !canConfigure || !validDraft}" in resilience
    assert "setRemoveOpen(true)" in resilience
    assert "主备切换" in resilience
    assert "均衡分流" in resilience
    assert "加权分流" in resilience
    assert 'useState<DeploymentMode | null>(null)' in resilience
    assert 'validMemberCount && mode === "failover"' in resilience
    assert 'mode === "balanced"' in resilience
    assert 'mode === "weighted"' in resilience
    pool_helpers = (
        FRONTEND_SOURCE / "components/model-management/pool-session-affinity.ts"
    ).read_text()
    assert 'return equalWeights ? "balanced" : "weighted"' in pool_helpers
    assert "poolMemberIdentity(member) !== primaryMemberId" in pool_helpers
    assert "poolMemberIdentity(member) === primaryMemberId ? 2 : 1" in pool_helpers
    assert "membersForMode" in resilience
    assert "changeWeight" in resilience
    assert (
        'aria-label={`${deployment.runtime_name} ${deployment.deployment_name} 权重`}'
    ) in resilience
    assert 'type="number" min="1" max="100"' in resilience
    assert "weightedIsDistinct" in resilience
    assert "<details" in resilience and "故障处理策略</summary>" in resilience
    assert "(hasEffectivePool || hasPendingPool) && <>" in resilience
    assert "(hasEffectivePool || hasPendingPool && member) && <div" in resilience
    assert "429 · 408 · 5xx · Connection · Timeout" in resilience
    assert "至少选择两个部署" in resilience
    assert '"创建"' in resilience
    assert '"发布到 APIM"' in resilience
    assert 'className="apim-native-route-dirty"' in resilience
    assert "无更改" not in resilience
    assert ".apim-native-route-settled" not in styles
    assert 'const shownSelected = selected' in resilience
    assert 'className="router-resilience-runtime-list" role="list"' in resilience
    assert 'className="router-resilience-runtime" role="listitem"' in resilience
    assert 'className="router-resilience-member-meta"' in resilience
    assert (
        ".router-resilience-toolbar b { color: var(--foreground); "
        "font-size: 13px; line-height: 18px;"
    ) in styles
    assert (
        ".router-resilience-toolbar small { color: var(--muted-foreground); "
        "font-size: 12px; line-height: 18px; }"
    ) in styles
    assert (
        ".router-resilience-runtime-list { display: grid; "
        "border-top: 1px solid var(--border); }"
    ) in styles
    assert ".router-resilience-runtime { min-width: 0; min-height: 60px;" in styles
    assert (
        ".router-resilience-runtime > label b { color: var(--foreground); "
        "font-size: 13px; line-height: 18px; font-weight: 600; overflow-wrap: anywhere; }"
    ) in styles
    assert (
        ".router-resilience-runtime > label small { overflow: hidden; "
        "color: var(--muted-foreground); font-size: 11px; line-height: 16px;"
    ) in styles
    assert (
        ".router-resilience-policy { min-width: 0; display: grid; "
        "grid-template-columns: minmax(0, 1fr);"
    ) in styles
    assert (
        ".router-resilience-runtime-list + .router-resilience-policy { "
        "margin-top: -12px; }"
    ) in styles
    assert (
        ".router-resilience-policy dl { min-width: 0; display: grid; "
        "grid-template-columns: repeat(4, minmax(0, 1fr));"
    ) in styles
    assert (
        ".router-resilience-policy dl > div:nth-child(-n+4) { "
        "border-bottom: 1px solid var(--border); }"
    ) in styles
    assert (
        ".router-resilience-policy dl > div:not(:last-child) { "
        "border-bottom: 1px solid var(--border); }"
    ) in styles
    assert (
        ".router-resilience-policy b { color: var(--foreground); "
        "font-size: 13px; line-height: 18px;"
    ) in styles
    assert (
        ".router-resilience-policy small { color: var(--muted-foreground); "
        "font-size: 12px; line-height: 18px; }"
    ) in styles
    assert (
        ".router-resilience-policy dt { color: var(--muted-foreground); "
        "font-size: 11px; line-height: 16px;"
    ) in styles
    assert (
        ".router-resilience-policy dd { margin: 3px 0 0; overflow: hidden; "
        "font-size: 13px; line-height: 18px;"
    ) in styles
    assert "router-resilience-runtime-head" not in resilience
    assert "router-resilience-mode-help" not in resilience
    assert ".router-resilience-runtime-head" not in styles
    assert ".router-resilience-mode-help" not in styles
    assert ".router-resilience-member-role { color:" in styles
    assert (
        ".router-resilience-member-role { color: var(--muted-foreground); "
        "font-size: 13px; line-height: 18px; font-weight: 500; }"
    ) in styles
    assert (
        ".router-resilience-member-priority small, "
        ".router-resilience-member-weight small, "
        ".router-resilience-member-weight-readonly small { "
        "color: var(--muted-foreground); font-size: 12px; line-height: 18px; }"
    ) in styles
    assert (
        ".router-resilience-member-priority b { color: var(--foreground); "
        "font: 600 13px/18px"
    ) in styles
    assert (
        ".router-resilience-member-weight-readonly b { color: var(--foreground); "
        "font: 600 13px/18px"
    ) in styles
    assert ".router-resilience-member-role { width:" not in styles
    assert ".router-resilience-runtime > i" not in styles
    assert ".router-resilience-runtime > label small i" in styles
    for removed_copy in (
        "当前模型的部署策略",
        "主部署优先，429 时切换备用",
        "在所有健康部署间平均分配",
        "至少需要两个兼容的已管理 Runtime",
    ):
        assert removed_copy not in resilience
    assert 'from "../ui/input"' in resilience
    assert 'from "../ui/switch"' not in resilience
    assert 'from "../ui/button-group"' in resilience
    assert 'className="usage-metric-segment router-deployment-mode"' in resilience
    assert 'className={validMemberCount && mode === "balanced" ? "active" : ""}' in resilience
    assert 'className={validMemberCount && mode === "weighted" ? "active" : ""}' in resilience
    assert "<small>APIM</small>" in resilience
    assert "P${member.priority + 1}" in resilience
    assert "故障信号" in resilience
    assert "熔断时间" in resilience
    assert "最大尝试" in resilience
    assert "重试间隔" in resilience
    assert "首次快速重试" in resilience
    assert "后端超时" in resilience
    assert "后端 Retry-After" in resilience
    assert 'accept_retry_after ? "采用" : "忽略"' in resilience
    assert '"遵循"' not in resilience
    assert "恢复单一后端，并影响该模型的所有调用。" in resilience
    assert "localStorage" not in resilience
    assert "mock" not in resilience.lower()
    assert ".router-deployment-resilience" in styles
    assert '[data-node-type="deployment_strategy"]' not in styles
    assert ".router-apim-explanation" not in styles
    assert ".router-apim-scope-warning" not in styles


def test_model_router_access_includes_the_signed_in_owner() -> None:
    page = (FRONTEND_SOURCE / "pages/model-router-page.tsx").read_text(encoding="utf-8")
    api = (FRONTEND_SOURCE / "data-sources/apim/api.ts").read_text(encoding="utf-8")
    queries = (
        FRONTEND_SOURCE / "data-sources/apim/queries.ts"
    ).read_text(encoding="utf-8")

    assert "finopsQueries.entities()" not in page
    assert "finopsQueries.modelRouterPeople(" in page
    assert "dataSource.saveModelRouterAssignments(" in page
    assert "modelRouterPeople:" in api
    assert "saveModelRouterAssignments:" in api
    assert "modelRouterPeople:" in queries
    assert "<RouterAccessDialog router={accessTarget}" in page


def test_model_router_save_retries_only_for_the_legacy_schema_contract() -> None:
    api = (FRONTEND_SOURCE / "data-sources/apim/api.ts").read_text(encoding="utf-8")

    assert "function legacyModelRouterWrite" in api
    assert 'schema_version: 1' in api
    assert 'node.type === "strategy" ? "priority" : node.type' in api
    assert 'node.strategy_type !== "priority"' in api
    assert "function isLegacyModelRouterSchemaError" in api
    assert 'issue.type === "literal_error"' in api
    assert 'issue.input === 2' in api
    assert 'issue.ctx?.expected === "1"' in api
    assert 'issue.loc?.at(-1) === "schema_version"' in api
    assert "if (!isLegacyModelRouterSchemaError(error)) throw error" in api


def test_assistant_router_confirmation_uses_only_the_persisted_proposal() -> None:
    api = (
        FRONTEND_SOURCE / "components/assistant/api.ts"
    ).read_text(encoding="utf-8")
    controller = (
        FRONTEND_SOURCE / "components/assistant/use-assistant-controller.ts"
    ).read_text(encoding="utf-8")
    conversation = (
        FRONTEND_SOURCE / "components/assistant/assistant-conversation.tsx"
    ).read_text(encoding="utf-8")
    types = (
        FRONTEND_SOURCE / "components/assistant/types.ts"
    ).read_text(encoding="utf-8")
    styles = read_frontend_styles()

    confirmation = api[
        api.index("confirmRouterProposal:") : api.index("conversations:")
    ]
    assert "/router-proposals/${encodeURIComponent(proposalId)}/confirm" in confirmation
    assert '{ method: "POST" }' in confirmation
    assert "body:" not in confirmation
    assert "JSON.stringify" not in confirmation
    assert "router_proposals?: AssistantRouterProposal[]" in types
    assert "assistantApi.confirmRouterProposal(proposalConversationId, proposalId)" in controller
    assert "invalidateQueries({ queryKey: finopsKeys.modelRouters })" in controller
    assert 'canManageRouters: source === "apim" && user?.role === "owner"' in controller
    assert "exchange.reply.router_proposals?.map" in conversation
    assert "conversationId," in conversation
    assert "proposalId: proposal.id" in conversation
    assert 'url.searchParams.set("router", routerId)' in conversation
    assert "openModelRouters(result.router.id)" in conversation
    confirmation_start = conversation.index(
        "onClick={() => controller.confirmRouterProposal.mutate"
    )
    confirmation_end = conversation.index("})}", confirmation_start)
    assert "proposal.definition" not in conversation[
        confirmation_start:confirmation_end
    ]
    for selector in (
        ".assistant-router-proposal",
        ".assistant-router-branches",
        ".assistant-router-confirm",
        ".assistant-router-created",
    ):
        assert selector in styles


def test_request_trace_rows_show_router_identity_before_selection() -> None:
    dashboard = read_apim_dashboard_source()
    styles = read_frontend_styles()

    assert (
        'className={`trace-row-status ${item.status_code == null ? "" : '
        'requestTraceFailed(item) ? "failed" : "success"}`}'
        in dashboard
    )
    assert (
        '`${item.router_name} · v${item.router_version} · '
        '${item.selected_model_name ?? item.model_name}`' in dashboard
    )
    assert "item.selected_model_name ?? item.model_name" in dashboard
    assert 'className={`trace-detail-hero ${request.router_name ? "has-router" : ""}`}' in dashboard
    assert 'className="trace-detail-router"' in dashboard
    assert "routerTotalLatency" in dashboard
    assert "routerTotalTokens" in dashboard
    assert "routerTotalCost" in dashboard
    assert 'className="trace-router-attempt-metrics"' in dashboard
    assert "routerDecisionLabels" in dashboard
    assert "routerStrategyLabels" in dashboard
    assert "request.router_attempts[0]?.strategy_type" in dashboard
    assert ".trace-row-status" in styles
    assert ".trace-row > span {" not in styles
    assert ".trace-detail-hero.has-router" in styles


def test_model_based_router_preview_and_trace_evidence_are_privacy_safe() -> None:
    api = (FRONTEND_SOURCE / "data-sources/apim/api.ts").read_text(encoding="utf-8")
    types = (
        FRONTEND_SOURCE / "data-sources/apim/types.ts"
    ).read_text(encoding="utf-8")
    canvas = (
        FRONTEND_SOURCE / "components/model-router/router-canvas.tsx"
    ).read_text(encoding="utf-8")
    form = (
        FRONTEND_SOURCE / "components/model-router/router-config-form.tsx"
    ).read_text(encoding="utf-8")
    editor = (
        FRONTEND_SOURCE / "components/model-router/decision-prompt-editor.tsx"
    ).read_text(encoding="utf-8")
    strategy_editor = (
        FRONTEND_SOURCE / "components/model-router/strategy-model-editor.tsx"
    ).read_text(encoding="utf-8")
    dashboard = read_apim_dashboard_source()

    assert 'selected.strategy_type === "model_based" && <DecisionPromptEditor' in canvas
    assert 'node.strategy_type === "model_based" && <DecisionPromptEditor' in form
    assert (
        'placeholder={isSystemOne\n          ? "留空使用内置判路由指令；'
        '候选模型的名称、单价和路由用途说明会自动作为 Choice 判据"'
    ) in editor
    assert '<SelectTrigger aria-label="选择判路由模型"' in canvas
    assert 'aria-label="选择 Routing Model"' not in canvas
    assert "SAMPLE_PROMPT_PRESETS" not in editor
    assert "previewModelRouterDecision" not in editor
    assert "trialModelRouterDecision" not in editor
    assert '"/api/v1/model-routers/decision-preview"' in api
    assert "strategy_node_id: strategyNodeId" in api
    assert "sample_prompt: samplePrompt" in api
    assert 'maxLength={20_000}' in editor
    assert 'max={50_000}' in editor
    assert '<span>路由用途说明</span>' in strategy_editor
    assert 'maxLength={1000}' in strategy_editor
    assert "description: string" in types
    assert "decision_prompt: string | null" in types
    assert "max_prompt_chars: number" in types
    for field in (
        "decision_system",
        "decision_system_sha256",
        "decision_input_chars",
        "decision_input_sha256",
        "decision_response_chars",
        "decision_response_sha256",
    ):
        assert field in types
        assert f"attempt.{field}" in dashboard
    assert "decision_input?:" not in types
    assert "decision_response?:" not in types
    assert "decision_rationale" not in types


def test_model_router_canvas_and_regular_mode_share_one_definition() -> None:
    canvas = (
        FRONTEND_SOURCE / "components/model-router/router-canvas.tsx"
    ).read_text(encoding="utf-8")
    form = (
        FRONTEND_SOURCE / "components/model-router/router-config-form.tsx"
    ).read_text(encoding="utf-8")
    strategy = (
        FRONTEND_SOURCE / "components/model-router/strategy-model-editor.tsx"
    ).read_text(encoding="utf-8")
    definition = (
        FRONTEND_SOURCE / "components/model-router/router-definition.ts"
    ).read_text(encoding="utf-8")
    edge = (
        FRONTEND_SOURCE / "components/model-router/router-edge.tsx"
    ).read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages/model-router-page.tsx").read_text(
        encoding="utf-8"
    )
    styles = read_frontend_styles()
    package = json.loads(
        (REPOSITORY_ROOT / "frontend/package.json").read_text(encoding="utf-8")
    )

    assert "ReactFlowProvider" in canvas
    assert '<RouterCanvas key={router.id}' in page
    assert "const ROUTER_CANVAS_MIN_ZOOM = 0.1" in canvas
    assert "useUpdateNodeInternals" in canvas
    assert "updateNodeInternals(canvasNodesRef.current.map((node) => node.id))" in canvas
    assert "requestAnimationFrame" in canvas
    assert "pendingDragChangesRef" in canvas
    assert "commitCanvasNodes(nextNodes)" in canvas
    assert "onlyRenderVisibleElements" in canvas
    assert "nodesDraggable={!isLocked}" in canvas
    assert "elementsSelectable={!isLocked && !isDragging}" in canvas
    assert "edgesFocusable={!isLocked && !isDragging}" in canvas
    assert "deleteKeyCode" in canvas
    assert "onConnect={handleConnect}" in canvas
    # A Strategy newly fed by a candidate pool must store the candidate rows it displays.
    assert (
        'const feedsStrategy = sourceNode?.type === "candidate_models"\n'
        '      && definition.nodes.find((node) => node.id === connection.target)?.type'
        ' === "strategy"' in canvas
    )
    assert "onChange(feedsStrategy ? syncStrategyModelConfigs(next) : next)" in canvas
    # A clicked edge becomes the only selection, so it can be inspected or deleted.
    assert (
        "onEdgeClick={(_event, edge) => { if (isLocked) return; setSelectedEdgeId(edge.id); "
        "onSelectedNodeChange(null) }}" in canvas
    )
    assert "selected: edge.id === selectedEdgeId," in canvas
    # A locked canvas presents no selected edge, so Delete cannot remove one.
    assert "flowEdges(definition, isLocked ? null : selectedEdgeId)" in canvas
    # Canvas branch labels and draft normalization share one Match label list.
    assert "MATCH_BRANCH_LABELS.includes(edge.label" in canvas
    assert '"match", "matched"' not in canvas
    assert "onDrop=" in canvas
    assert "<MiniMap" not in canvas
    assert "showMinimap" not in canvas
    assert canvas.count("fitViewOptions={ROUTER_CANVAS_FIT_OPTIONS}") == 2
    assert "autoLayoutDefinition" in canvas
    assert "inferLayoutDirection" in canvas
    assert '<Panel className="router-canvas-toolbar" position="top-right">' in canvas
    assert "canUndo" in canvas
    assert "canRedo" in canvas
    assert "historyRef" in canvas
    assert "futureRef" in canvas
    assert 'aria-label="从左到右排列"' in canvas
    assert 'aria-label="从上到下排列"' in canvas
    assert 'aria-label="按当前方向自动布局"' in canvas
    assert "const [showGrid, setShowGrid] = useState(true)" in canvas
    assert "{showGrid && <Background gap={20} size={1} />}" in canvas
    assert 'aria-label="显示画布网格"' in canvas
    assert "aria-pressed={showGrid}" in canvas
    assert "<Grid3X3 size={14} />" in canvas
    assert (
        ".router-canvas-stage .react-flow__background { "
        "--xy-background-pattern-color: color-mix(in oklch, "
        "var(--muted-foreground) 22%, transparent); }" in styles
    )
    assert (
        ".dark .router-canvas-stage .react-flow__background { "
        "--xy-background-pattern-color: color-mix(in oklch, "
        "var(--muted-foreground) 30%, transparent); }" in styles
    )
    assert ".router-canvas-toolbar { display: flex; align-items: center; gap: 4px;" in styles
    assert ".router-toolbar-group { display: flex; align-items: center; gap: 2px; }" in styles
    assert ".router-canvas-toolbar button.active { border-color: transparent;" in styles
    assert (
        ".router-canvas-stage .react-flow__minimap { width: 113.333333px; "
        "height: 80px;" in styles
    )
    assert 'className="router-node-candidate-list"' in canvas
    assert 'className="router-node-candidate-item"' in canvas
    assert "routerNodeDimensions" in canvas
    assert "const showTypeBadge" in canvas
    assert "ROUTER_CANDIDATE_NODE_MIN_HEIGHT = 134" in definition
    assert "ROUTER_NODE_HEIGHT = 88" in definition
    assert "ROUTER_CONDITION_NODE_HEIGHT = 96" in definition
    assert "ROUTER_DETAIL_NODE_HEIGHT = 88" in definition
    assert "90 + node.candidates.length * ROUTER_CANDIDATE_ROW_HEIGHT" in definition
    assert "background: color-mix(in srgb, var(--node-accent) 12%, transparent)" in styles
    assert "candidate-edge:" not in canvas
    assert 'const edgeTypes = { routerEdge: RouterEdge }' in canvas
    assert 'type: "routerEdge"' in canvas
    assert "getBezierPath" in edge
    assert "getStraightPath" not in edge
    assert "const horizontallyAligned" not in edge
    assert "const arcHeight" not in edge
    assert "interactionWidth={20}" in edge
    assert "EdgeLabelRenderer" in edge
    assert "outgoingHandleId" in canvas
    assert "outgoingEdges" in canvas
    assert 'className={`router-branch-handle router-branch-${handle.tone}`}' in canvas
    assert '`router-branch-edge router-branch-${tone}`' in canvas
    assert 'position={vertical ? Position.Bottom : Position.Right}' in canvas
    assert 'position={vertical ? Position.Top : Position.Left}' in canvas
    assert 'ariaLabel="路由图概览"' not in canvas
    assert "RouterProperties" in canvas
    assert "routerName={routerName}" in canvas
    assert "routerDescription={routerDescription}" in canvas
    assert "onMetadataChange={onMetadataChange}" in canvas
    assert '<span>标题</span><Input value={routerName}' in canvas
    assert '<span>描述</span><Textarea value={routerDescription}' in canvas
    assert "未命中兜底模型" not in canvas
    assert "未命中兜底模型" not in form
    assert "default_model_missing" not in form
    assert "default_model_not_candidate" not in form
    assert "CandidateEditor" in canvas
    assert "ModelRouterDefinition" in form
    assert "CandidateConfiguration" in form
    assert "StrategyModelEditor" in form
    assert "StrategyModelEditor" in canvas
    assert '<SelectItem value="model_based">模型判路由</SelectItem>' in canvas
    assert '<SelectItem value="model_based">模型判路由</SelectItem>' in form
    assert 'value="model_based" disabled' not in canvas
    assert 'value="model_based" disabled' not in form
    assert '<span>Routing Model</span>' in canvas
    assert '<span>Routing Model</span>' in form
    assert (
        "displayModels.find((model) => "
        "model.id === selected.routing_model_id)" in canvas
    )
    assert 'displayModels.find((model) => model.id === node.routing_model_id)' in form
    for source in (canvas, form):
        assert 'routingModel?.display_name ?? "已不可用的模型"' in source
        assert 'routerModelStatusLabel(routingModel.status)' in source
        assert 'routingModel.status !== "available"' in source
    assert "resolveRouterDisplayModels" in page
    assert "routers.data?.model_references" in page
    assert "ModelBrandLogo" in canvas
    assert "ModelBrandLogo" in strategy
    assert "displayModels={displayModels}" in page
    assert "models={availableModels} displayModels={displayModels}" in page
    assert "models={displayModels}" in canvas
    assert 'model_based: "模型判路由"' in canvas
    assert 'model_based: "模型判路由"' in form
    assert (
        '<SelectTrigger aria-label="选择 Routing Model"><SelectValue placeholder='
        not in canvas
    )
    assert (
        '<SelectTrigger aria-label="常规配置选择 Routing Model"><SelectValue placeholder='
        not in form
    )
    assert '<SelectItem value="prompt_length">问题长度</SelectItem>' in canvas
    assert '<SelectItem value="prompt_length">问题长度</SelectItem>' in form
    assert "ConditionConfiguration" in form
    assert "function routerBranches" in form
    assert 'node.type === "candidate_models"' in form
    assert 'nodesById.get(edge.source)?.type === "condition"' in form
    assert '["strategy", "priority"].includes' in form
    assert 'nodesById.get(edge.target)?.type === "fallback"' in form
    assert "ensureCandidateNode" not in form
    assert "ensureStrategyNode" not in form
    assert "ensureFallbackNode" not in form
    assert "node={activeBranch.candidate}" in form
    assert "node={activeBranch.strategy}" in form
    assert "node={activeBranch.fallback}" in form
    assert 'role="tablist" aria-label="路由分支"' in form
    assert ".router-branch-editor-grid { display: grid;" in styles
    assert ".router-list-controls {" in styles
    assert "min-height: 100px;\n\tflex: 0 0 100px;" in styles
    palette = canvas[
        canvas.index("const PALETTE_TYPES") : canvas.index("const NODE_ICONS")
    ]
    assert '"condition"' in palette
    assert "FallbackConfiguration" in form
    assert "RouterConfigForm" in page
    assert "RouterCanvas" in page
    assert "const ROUTER_CONFIG_MODE_ENABLED = false" in page
    assert 'const effectiveMode: RouterMode = ROUTER_CONFIG_MODE_ENABLED ? mode : "canvas"' in page
    assert 'ROUTER_CONFIG_MODE_ENABLED && <div className="router-editor-view-row">' in page
    assert 'effectiveMode === "canvas" ? <RouterCanvas' in page
    assert "routerName={value.name}" in page
    assert "routerDescription={value.description}" in page
    assert (
        "onMetadataChange={(name, description) => "
        "onChange({ ...value, name, description })}" in page
    )
    assert 'className="router-editor-view-row"' in page
    assert "onChange({ ...value, definition })" in page
    assert "ROUTER_PANE_WIDTH_MIN = 240" in page
    assert "ROUTER_PANE_WIDTH_MAX = 420" in page
    assert "@container (max-width: 720px)" in styles
    assert ".router-editor-identity { min-width: 0; flex: 1 1 420px;" in styles
    assert "startPaneResize" in page
    assert "onPointerDown={startPaneResize}" in page
    assert 'role="separator"' in page
    assert "aria-valuenow={paneWidth}" in page
    assert "turnstile_model_router_pane_width" in page
    assert page.count('className="router-new-button"') == 1
    assert "createStarterDefinition" in definition
    assert "normalizeRouterDefinition" in definition
    assert 'const hasElse' in definition
    assert 'createRouterEdge(start.id, candidates.id)' in definition
    assert 'createRouterNode("condition"' not in definition[
        definition.index("export function createStarterDefinition") :
        definition.index("export function inferLayoutDirection")
    ]
    assert "ROUTER_GRAPH_PRESENTATION_VERSION = 6" in definition
    write_from_router = page[
        page.index("function writeFromRouter") : page.index(
            "function serializedWrite"
        )
    ]
    assert "normalizeRouterDefinition(router.definition)" in write_from_router
    assert "autoLayoutDefinition" not in write_from_router
    assert "presentationVersion" not in write_from_router
    assert 'createRouterNode("strategy"' in definition
    assert 'createRouterEdge(candidates.id, strategy.id)' in definition
    assert 'createRouterEdge(strategy.id, output.id, "正常输出")' in definition
    assert 'createRouterNode("fallback"' not in definition[
        definition.index("export function createStarterDefinition") :
        definition.index("export function inferLayoutDirection")
    ]
    notice = (
        "经 APIM 网关调用的 Router 不支持 Fallback，包含 Fallback 时会返回 503；"
        "Fallback 只在平台内调用时生效。"
    )
    assert f'export const ROUTER_NATIVE_FALLBACK_NOTICE = "{notice}"' in definition
    assert (
        '<div className="router-properties-empty">{ROUTER_NATIVE_FALLBACK_NOTICE}</div>'
    ) in canvas
    assert "value.definition.nodes.some((node) => node.fallback_on.length > 0)" in page
    assert (
        '{nativeFallback && <div className="router-validation-banner-warning">'
        "{ROUTER_NATIVE_FALLBACK_NOTICE}</div>}"
    ) in page
    assert ".router-validation-banner-warning {" in styles
    for locale in ("en", "ja", "ko"):
        assert f'"{notice}":' in (
            FRONTEND_SOURCE / f"locales/{locale}/phrases-core.ts"
        ).read_text(encoding="utf-8")
    assert "const policyEdges = next.edges.filter(" in definition
    assert "for (const policyEdge of policyEdges)" in definition
    assert "strategy.timeout_ms = fallback.timeout_ms" in definition
    assert 'outputEdge.source_handle = "branch-normal"' in definition
    assert 'uniqueId(`${candidatePool.id}-strategy`, usedNodeIds)' in definition
    assert 'edge.source = strategy.id' in definition
    assert "const synthesizedStrategyIds = new Set<string>()" in definition
    assert "synthesizedStrategyIds.add(strategy.id)" in definition
    assert "legacyOutputs.length || synthesizedStrategyIds.has(policyEdge.source)" in definition
    assert "migratedStrategyIds.has(policyEdge.source)" not in definition
    assert "layout(graph)" in definition
    assert "for (const node of normalized.nodes)" in definition
    assert "const branchRank" in definition
    assert "if (targets.length < 2) continue" in definition
    assert 'role="toolbar"' in canvas
    assert "ModelBrandLogo" in canvas
    assert "router-candidate-search" in canvas
    assert "ariaLabel:" in canvas
    assert "turnstile_model_router_properties_width" in canvas
    assert "router-properties-handle" in canvas
    assert 'aria-label="候选模型池"' in form
    candidate_editor = canvas[
        canvas.index("function CandidateEditor") : canvas.index(
            "function RouterProperties"
        )
    ]
    strategy_editor = (
        FRONTEND_SOURCE / "components/model-router/strategy-model-editor.tsx"
    ).read_text(encoding="utf-8")
    assert "priority:" not in candidate_editor
    assert "weight:" not in candidate_editor
    assert 'className="router-strategy-model-list"' in strategy_editor
    assert 'className="router-strategy-row-actions"' in strategy_editor
    assert 'className="router-field"' in strategy_editor
    assert 'data-mode={orderedMode ? "priority" : "weighted"}' in strategy_editor
    assert 'const weightedMode = node.strategy_type === "weighted"' in strategy_editor
    assert 'const modelBasedMode = node.strategy_type === "model_based"' in strategy_editor
    assert 'modelBasedMode ? "安全回退顺序"' in strategy_editor
    assert (
        'modelBasedMode && <label className="router-field router-strategy-description"'
        in strategy_editor
    )
    assert "{weightedMode && <label" in strategy_editor
    assert '<span>候选调用超时 ms</span>' in canvas
    assert '<span>候选调用超时 ms</span>' in form
    assert "质量不足（未支持，请移除）" in canvas
    assert "质量不足（未支持，请移除）" in form
    assert '<SelectItem value="max_cost">' not in canvas
    assert '<SelectItem value="max_latency">' not in canvas
    assert '<SelectItem value="matches_any">包含任一关键词</SelectItem>' in canvas
    assert '<SelectItem value="matches_any">包含任一关键词</SelectItem>' in form
    assert '<SelectItem value="region">' not in canvas
    assert (
        'weightedMode && <label className="router-field router-strategy-weight"'
        in strategy_editor
    )
    assert 'aria-pressed={constraintsExpanded}' in strategy_editor
    assert 'data-state={constraintsExpanded ? "open" : "closed"}' in strategy_editor
    assert '.router-strategy-row-actions { display: flex; align-items: center;' in styles
    assert '.router-strategy-model-constraints [data-slot="input"],' in styles
    assert page.count('className="model-delete-dialog router-confirm-dialog"') == 3
    assert (
        '.model-delete-body [data-slot="alert-dialog-header"] { '
        "grid-template-columns: minmax(0, 1fr); gap: 4px; }" in styles
    )
    assert '.model-delete-body [data-slot="alert-dialog-title"] { grid-column: 1;' in styles
    assert '.model-delete-body [data-slot="alert-dialog-description"] { grid-column: 1;' in styles
    assert package["dependencies"]["@xyflow/react"] == "12.11.2"
    assert package["dependencies"]["@dagrejs/dagre"] == "3.1.1"
    for selector in (
        ".router-split",
        ".router-list-pane",
        "var(--router-pane-width, 304px) 1px minmax(0, 1fr)",
        ".router-canvas-layout",
        ".router-flow-node",
        ".router-canvas-toolbar",
        ".router-edge-label",
        ".router-node-candidate-list",
        ".router-editor-view-row",
        ".router-properties",
        ".router-config-section",
        ".router-candidate-search",
        ".router-node-action-secondary",
        ".router-action-primary",
        ".router-confirm-dialog",
        "button.router-new-button",
    ):
        assert selector in styles


def test_publish_stays_disabled_while_the_current_draft_fails_validation() -> None:
    page = (FRONTEND_SOURCE / "pages/model-router-page.tsx").read_text(encoding="utf-8")
    actions = page.split('<div className="router-editor-actions">', 1)[1].split("</div>", 1)[0]
    publish = next(line for line in actions.splitlines() if "onClick={onPublish}" in line)

    assert "disabled={busy || validation?.valid === false}" in publish
    # Any edit clears the result, so Publish re-enables once the draft changes.
    assert "onChange={(value) => { setDraft(value); setValidation(null) }}" in page


def test_published_router_with_saved_changes_reads_unpublished() -> None:
    page = (FRONTEND_SOURCE / "pages/model-router-page.tsx").read_text(encoding="utf-8")
    unpublished = 'router.status === "published" && router.has_unpublished_changes'

    assert f"{{{unpublished} && <> · <span>未发布</span></>}}" in page
    assert f"{{!dirty && {unpublished} && <strong>未发布</strong>}}" in page
