from tests.support.paths import FRONTEND_SOURCE


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
        assert f"{field}?: number | null" in types
        assert f'budget.{field} == null ? "—" : formatFullCount(budget.{field})' in page
    assert 'budget?.ledger_snapshot_at ? "已投影"' in page
    assert "dateTime={budget.ledger_snapshot_at ?? undefined}" in page
    assert ".application-budget-stats > div:nth-child(2n)" in styles
    for language in ("en", "ja", "ko"):
        phrases = (FRONTEND_SOURCE / f"locales/{language}/phrases-core.ts").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "预留占用",
            "上限占用",
            "可用额度",
            "待处理请求",
            "账本快照",
            "未同步",
            "延迟结算",
        ):
            assert f'"{phrase}":' in phrases


def test_subscriptions_are_a_model_platform_domain() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    source = (FRONTEND_SOURCE / "data-sources/apim/source.tsx").read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")
    assert '| "applications"' in app
    assert 'label: "模型平台"' in app
    assert '{ label: "订阅", icon: KeyRound, page: "applications" }' in app
    assert app.index('label: "模型平台"') < app.index(
        '{ label: "订阅", icon: KeyRound, page: "applications" }'
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
    # Six columns: the department a subscription is filed under, and the person who holds it.
    # Both are now what the gateway attributes a request to when the caller declares nothing,
    # so an inventory that does not show them hides the inputs to every budget on the site.
    assert "APPLICATION_TABLE_COLUMN_MIN_WIDTHS = [180, 130, 120, 160, 170, 80]" in page
    assert 'className="model-runtime-cell application-department-cell"' in page
    assert 'className="model-runtime-cell application-owner-cell"' in page
    assert '"部门"' in page
    assert '"归属人"' in page
    # A holder with two keys draws two allowances from a per-key budget, so the pairing has to
    # be visible even when no address could be derived for them.
    assert "person_group_size" in page
    # Filtering to one department lives in the URL so the view can be sent to someone else.
    assert "function departmentFromUrl()" in page
    assert 'searchParams.get("department")' in page
    assert "application-row-action" not in page
    assert "subscriptions-category-chevron" not in page
    assert 'className="application-detail-workspace"' in page
    assert 'className="application-detail-breadcrumb"' in page
    assert page.count("onClick={openApplicationRoute(null, detailCategory)}") >= 3
    assert '<span>{detailCategory === "agents" ? "Agents" : "Applications"}</span>' not in page
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
    # Widened again for the owner column; the point of the assertion is that the inventory
    # still declares a minimum, so a six-column grid cannot silently collapse.
    assert "--model-table-min-width: 950px" in styles
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
