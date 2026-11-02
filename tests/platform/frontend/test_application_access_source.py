import re

from tests.support.paths import FRONTEND_SOURCE


def test_application_creation_commands_use_complete_localization_phrases() -> None:
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")
    creation = (
        FRONTEND_SOURCE / "components/applications/application-create-dialog.tsx"
    ).read_text(encoding="utf-8")
    assert 'aria-label="添加订阅" title="添加订阅"' in page
    assert '<Plus size={14} />添加订阅</Button>' in page
    assert '<DialogTitle>{accepted ? "订阅创建进度" : "添加订阅"}</DialogTitle>' in creation
    assert "添加${consumer}" not in page
    assert "添加{consumer}" not in page


def test_ledger_display_uses_api_balances_and_preserves_unknown_values() -> None:
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")
    types = (FRONTEND_SOURCE / "data-sources/apim/types.ts").read_text(encoding="utf-8")
    styles = (FRONTEND_SOURCE / "styles/applications.css").read_text(encoding="utf-8")
    for field in (
        "pending_reserved_tokens",
        "finalized_upper_bound_tokens",
        "available_tokens",
        "pending_reservation_count",
    ):
        assert f"{field}: number | null" in types
    for field in (
        "finalized_upper_bound_tokens",
        "available_tokens",
        "pending_reservation_count",
    ):
        assert f'budget.{field} == null ? "—" : formatFullCount(budget.{field})' in page
    assert 'pendingTokens == null ? "—" : formatFullCount(pendingTokens)' in page
    assert "formatTimestamp(budget.ledger_snapshot_at, timezone)" in page
    assert '"等待账本快照"' in page
    assert 'budget.ledger_status === "unavailable"' in page
    assert 'budget.ledger_status === "prepared"' in page
    assert ".application-budget-stats > div:nth-child(2n)" in styles
    for language in ("en", "ja", "ko"):
        phrases = (FRONTEND_SOURCE / f"locales/{language}/phrases-core.ts").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "已确认用量",
            "待结算预留",
            "保守终态上界",
            "APIM 可用余额",
            "预留数量",
            "账本快照",
            "执行账本暂不可用，余额未知",
            "执行账本尚未启用",
            "等待账本快照",
        ):
            assert f'"{phrase}":' in phrases


def test_usd_limit_editors_follow_the_api_decimal_contract() -> None:
    from pydantic import ValidationError

    from turnstile_core.domain.application_access import GatewayApplicationBudgetUpdate

    validators = [
        re.compile(pattern)
        for path in ("pages/applications-page.tsx", "data-sources/apim/pages/budget-page.tsx")
        for pattern in re.findall(
            r"/(\^\\d\{1,16\}.*?\$)/\.test\(usdLimit\)",
            (FRONTEND_SOURCE / path).read_text(encoding="utf-8"),
        )
    ]
    assert len(validators) == 3
    # Editors prefill stored limits, which the API returns at NUMERIC(30,14) scale.
    for value in ("10", "0.01", "10.5", "10.00000000000000", "0.001", "1.005", "10.123"):
        try:
            GatewayApplicationBudgetUpdate.model_validate({
                "budget_mode": "usd", "usd_limit": value, "tokens_per_minute": 1,
                "enforce": True, "warning_threshold_percent": 80,
            })
            accepted = True
        except ValidationError:
            accepted = False
        assert [bool(item.fullmatch(value)) for item in validators] == [accepted] * 3, value


def test_subscription_dialogs_explain_usd_limits_retries_and_key_rotation() -> None:
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")
    creation = (
        FRONTEND_SOURCE / "components/applications/application-create-dialog.tsx"
    ).read_text(encoding="utf-8")
    card = (
        FRONTEND_SOURCE / "components/finops/application-subscription-card.tsx"
    ).read_text(encoding="utf-8")

    assert (
        '!usdValid && <div className="registry-error" role="alert">'
        "请输入有效的 USD 金额</div>"
    ) in page
    # A retryable readiness miss is progress; only a terminal failure is an error.
    assert (
        '{failed && currentOperation?.error_message && '
        '<div className="registry-error" role="alert">'
    ) in creation
    assert (
        '{!terminal && currentOperation?.error_message && '
        '<p className="application-create-note" role="status">'
    ) in creation
    # Only the expected key-propagation wait hides the latest retry reason.
    assert "const retryDetail = applicationRetryDetail(currentOperation)" in creation
    assert (
        '{retryDetail && <p className="application-create-note">{retryDetail}</p>}'
    ) in creation
    # APIM propagates a regenerated key before the old one stops authenticating.
    assert "立即失效" not in card
    assert "将在网关同步后失效" in card


def test_subscriptions_are_a_model_platform_domain() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    source = (FRONTEND_SOURCE / "data-sources/apim/source.tsx").read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")
    assert '| "applications"' in app
    assert 'label: "模型平台"' in app
    assert '{ label: "订阅管理", icon: KeyRound, page: "applications" }' in app
    assert app.index('label: "模型平台"') < app.index(
        '{ label: "订阅管理", icon: KeyRound, page: "applications" }'
    )
    assert 'page === "applications" && <ApplicationsPage' in app
    assert 'page === "applications" && <ApplicationsPage />' in app
    assert 'url.searchParams.delete("application")' in app
    assert '"applications",' in source
    assert "GatewayReleasesPage" not in page
    assert "BudgetManagementPage" not in page


def test_applications_use_only_typed_read_inventory_and_async_sync() -> None:
    api = (FRONTEND_SOURCE / "data-sources/apim/api.ts").read_text(encoding="utf-8")
    queries = (FRONTEND_SOURCE / "data-sources/apim/queries.ts").read_text(encoding="utf-8")
    types = (FRONTEND_SOURCE / "data-sources/apim/types.ts").read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")
    activity_card = (
        FRONTEND_SOURCE / "components/finops/application-usage-activity-card.tsx"
    ).read_text(encoding="utf-8")
    subscription_card = (
        FRONTEND_SOURCE / "components/finops/application-subscription-card.tsx"
    ).read_text(encoding="utf-8")

    assert '"/api/v1/application-access/applications"' in api
    assert "/api/v1/application-access/applications/${encodeURIComponent(id)}" in api
    assert "/api/v1/application-access/gateways/${encodeURIComponent(gatewayId)}/sync" in api
    assert 'gatewayApplications: ["application-access", "applications"]' in queries
    assert "finopsQueries.gatewayReleaseOperation(activeOperationId)" in page
    assert 'operation.data?.operation_kind === "application_sync"' in page
    assert "export type GatewayApplicationDetail" in types
    assert "export type GatewayApplicationUserUsage" in types
    assert "users: GatewayApplicationUserUsage[]" in types
    assert (
        'operation_kind: "rollback" | "integrity_check" | "gc_plan" '
        '| "application_sync" | "application_provision"' in types
    )
    assert "provisionGatewayApplicationSubscription" in api
    assert "updateGatewayApplicationAvatar" in api
    assert "GatewayApplicationAvatarUpdate" in types
    assert "avatar_url?: string | null" in types
    assert "ApplicationCreateDialog" in page
    assert "prepareApplicationAvatar" in page
    assert "ApplicationAvatarEditor" in page
    assert "governance_write_available?: boolean" in types
    assert "updateGatewayApplicationBudget" in api
    assert "updateGatewayApplicationModelAccess" in api
    assert "ApplicationBudgetDialog" in page
    assert "ApplicationModelAccessDialog" in page
    assert "gatewayApplicationUsageActivity" in api
    assert "finopsQueries.gatewayApplicationUsageActivity" in activity_card
    assert "ApplicationUsageActivityCard" in page
    assert "aggregateActivity" in activity_card
    assert "ActivityHeatmap" in activity_card
    assert 'className="application-usage-controls"' in activity_card
    assert 'className="application-card application-usage-activity-card"' in activity_card
    assert 'title="后端尚未发布编辑能力"' in page
    assert "primary_key" in types
    creation = (
        FRONTEND_SOURCE / "components/applications/application-create-dialog.tsx"
    ).read_text(encoding="utf-8")
    assert "navigator.clipboard.writeText(primaryKey.current)" in creation
    assert "useMutation" not in creation
    assert "primaryKey = useRef<string | null>(null)" in creation
    assert "onAccepted(safeAccepted)" in creation
    assert "{accepted.primary_key}" not in creation
    assert "{response.primary_key}" not in creation
    assert "localStorage" not in creation
    assert "submittingRef.current || accepted" in creation
    assert "applicationProvisionBlocker(inventory)" in creation
    assert "applicationOperationIdFromUrl(window.location.href)" in page
    assert "setQueryData(finopsKeys.gatewayReleaseOperation" in page
    assert "secondary_key" not in page.lower()
    assert "listsecrets" not in page.lower()
    assert "ApplicationSubscriptionCard" in page
    assert "revealGatewayApplicationSubscriptionKey" in api
    assert "rotateGatewayApplicationSubscriptionKey" in api
    assert "navigator.clipboard.writeText" in subscription_card
    assert "useMutation" in subscription_card
    assert "useQuery" not in subscription_card
    assert "secret.value" not in subscription_card.replace("copySecret(secret.value)", "")
    assert "application.subscriptions.length > 1" in subscription_card
    assert 'subscription.state !== "active"' in subscription_card
    assert "mock" not in page.lower()
    assert "demo" not in page.lower()


def test_applications_use_two_level_inventory_and_detail_routes() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")
    styles = (FRONTEND_SOURCE / "styles/applications.css").read_text(encoding="utf-8")
    subscription_card = (
        FRONTEND_SOURCE / "components/finops/application-subscription-card.tsx"
    ).read_text(encoding="utf-8")

    assert "applicationRouteHref" in page
    assert "openApplicationRoute" in page
    assert "FINOPS_NAVIGATE_EVENT" in page
    assert 'className="model-list-row application-inventory-row"' in page
    assert 'className="model-table application-model-table"' in page
    assert "APPLICATION_TABLE_COLUMN_MIN_WIDTHS = [180, 130, 180, 100]" in page
    assert "application-row-action" not in page
    assert "subscriptions-category-chevron" not in page
    assert 'className="application-detail-workspace"' in page
    assert 'className="smh-page-header application-detail-breadcrumb"' in page
    assert 'className="application-breadcrumb-trail"' in page
    assert 'aria-current="page"' in page
    assert page.count("onClick={openApplicationRoute(null)}") >= 2
    assert "detailCategory" not in page
    assert "if (applicationId) {" in page
    assert "useResizablePane" in page
    assert 'storageKey: "turnstile_subscriptions_navigation_width"' in page
    assert 'className="runtime-pane-handle subscriptions-pane-handle"' in page
    assert 'className="subscriptions-category-list"' in page
    assert "subscriptions-category-card" in page
    assert (
        ".subscriptions-category-card.active { border-color: transparent; "
        "background: var(--muted); color: var(--foreground); box-shadow: none; }" in styles
    )
    assert "createPortal" in page
    assert 'id="mobile-topbar-end-actions"' in app
    assert 'className="subscriptions-mobile-topbar-actions"' in page
    assert "application-mobile-actions" not in page
    assert "applicationDrawerOpen" not in page
    assert "application-budget-progress" in page
    assert "application-model-list" in page
    assert "application-subscription-list" in subscription_card
    assert "application-user-list" in page
    assert "application.user_count" in page
    assert "application-timeline" in page
    assert ".application-inventory-row" in styles
    assert "--model-table-min-width: 658px" in styles
    assert ".application-avatar-editor" in styles
    assert "container: subscription-content / inline-size" in styles
    assert "grid-template-columns: var(--subscriptions-nav-width, 276px) minmax(0, 1fr)" in styles
    assert ".subscriptions-pane-handle { z-index: 3; grid-column: 2;" in styles
    assert ".application-model-table .model-list-row" in styles
    assert ".application-detail-workspace" in styles
    assert (
        ".application-detail-scroll { min-width: 0; min-height: 0; flex: 1; "
        "overflow: auto; background: var(--background); }" in styles
    )
    assert "background: color-mix(in oklch, var(--muted) 12%, var(--background))" not in styles
    assert ".application-detail-grid" in styles
    assert "grid-template-columns: minmax(0, 1fr) 320px" in styles
    assert ".application-user-list" in styles
    assert "@media (max-width: 767px)" in styles
    assert ":focus-visible" in styles
    assert "var(--border)" in styles
    assert "var(--muted)" in styles


def test_application_summary_does_not_double_its_bottom_border() -> None:
    styles = (FRONTEND_SOURCE / "styles/applications.css").read_text(encoding="utf-8")
    facts_rule = re.search(r"\.application-facts \{([^}]+)\}", styles)
    assert facts_rule is not None
    assert "border-bottom:" not in facts_rule.group(1)
    assert (
        ".application-facts:not(:last-child) { border-bottom: 1px solid var(--border); }"
        in styles
    )


def test_application_sync_exposes_every_enabled_apim_target() -> None:
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")
    assert 'gateway.enabled && gateway.implementation === "apim"' in page
    assert '<DropdownMenuTrigger render={button}' in page
    assert 'targets.map(target =>' in page
    assert 'onClick={() => onSync(target.id)}' in page
    assert 'targets.length < 2' in page
    assert page.count('<ApplicationSyncButton targets={syncTargets}') == 2
    assert 'onSync={targetId => syncMutation.mutate(targetId)}' in page


def test_subscription_navigation_filters_by_apim_without_merging_detail_pages() -> None:
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")
    assert "SubscriptionCategory" not in page
    assert "subscriptionCategoryFromUrl" not in page
    assert 'aria-label="类型"' not in page
    assert 'url.searchParams.set("consumer", "all")' in page
    assert 'searchParams.get("apim")' in page
    assert 'url.searchParams.set("apim", apimId)' in page
    assert 'url.searchParams.delete("apim")' in page
    assert 'setSelectedApimId(subscriptionApimFromUrl())' in page
    assert '!selectedApimId || item.gateway_profile_id === selectedApimId' in page
    assert 'gateway.implementation === "apim"' in page
    assert 'aria-label="APIM 实例"' in page
    assert 'href={applicationRouteHref(null, card.id)}' in page
    assert 'onClick={openApplicationRoute(null, card.id)}' in page
    assert 'if (applicationId) {' in page
    assert "categoryItems" not in page
    assert 'applicationType="service"' in page
    assert 'application.system_managed && <em>系统</em>' in page
    assert 'aria-label="订阅状态筛选"' in page
    assert 'data-application-id={application.id}' in page


def test_application_detail_breadcrumb_reuses_inventory_header_geometry() -> None:
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")
    styles = (FRONTEND_SOURCE / "styles/applications.css").read_text(encoding="utf-8")
    header = page.split(
        '<header className="smh-page-header application-detail-breadcrumb">', 1
    )[1].split("</header>", 1)[0]
    assert 'className="smh-header-icon"' in header
    assert '<KeyRound size={17}' in header
    assert '<nav className="application-breadcrumb-trail"' in header
    assert 'aria-current="page"' in header
    assert '>订阅</a>' in header
    assert header.count("<ChevronRight") == 1
    assert 'className="smh-page-header applications-header"' in page
    header_styles = styles.split(".application-detail-breadcrumb {", 1)[1].split("}", 1)[0]
    assert "font-size: 14px;" in header_styles
    assert "line-height: 20px;" in header_styles
    assert re.search(
        r"(?:^|;)\s*(?:height|min-height|max-height|flex|padding(?:-inline)?|border-bottom)\s*:",
        header_styles,
    ) is None
    assert styles.count(".application-detail-breadcrumb {") == 1
    assert (
        ".application-breadcrumb-trail { min-width: 0; display: flex; "
        "align-items: center; gap: 6px; padding: 0; }" in styles
    )
    assert ".application-breadcrumb-trail > svg { flex-shrink: 0; }" in styles
    assert ".application-breadcrumb-trail > a:first-child { min-width: 0; flex-shrink: 1;" in styles


def test_subscription_creation_states_the_expected_wait() -> None:
    dialog = (
        FRONTEND_SOURCE / "components/applications/application-create-dialog.tsx"
    ).read_text(encoding="utf-8")
    note = "新订阅通常需要 2–3 分钟生效，期间会自动重试，无需重复提交。"

    assert (
        '{!terminal && !failed && <p className="application-create-note" role="status">'
        + note + "</p>}"
    ) in dialog
    for locale in ("en", "ja", "ko"):
        phrases = (FRONTEND_SOURCE / f"locales/{locale}/phrases-application-create.ts").read_text(
            encoding="utf-8"
        )
        assert f'"{note}":' in phrases


def test_ownership_dialog_warns_before_the_assigned_person_is_removed() -> None:
    # Changing department clears the person, because a person must belong to it.
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")

    assert (
        "{personSupported && application.person_id && !personId && "
        '<div className="application-governance-unavailable" role="status">' in page
    )
    assert "<span>保存后将移除归属人员</span>" in page


def test_application_usage_does_not_count_cache_writes_as_reads() -> None:
    # cached_tokens includes cache writes; showing it as "cache read" doubled the reads.
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")

    old_metric = '<Metric label="缓存读取" value={formatCount(application.usage.cached_tokens)} />'
    assert old_metric not in page
    assert (
        "formatCount(Math.max(application.usage.cached_tokens - "
        "application.usage.cache_write_tokens, 0))" in page
    )


def test_apim_group_label_counts_the_same_subscriptions_as_its_badge() -> None:
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")

    assert '`${card.items.length} 个订阅`' in page
    assert "total + item.subscription_count, 0)} 个订阅" not in page
