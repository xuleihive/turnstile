from tests.support.paths import FRONTEND_SOURCE, read_frontend_styles


def test_connection_metadata_actions_use_the_owner_connection_endpoint() -> None:
    source = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(encoding="utf-8")
    update = source.split("const updateRuntime =", 1)[1].split("const updateGateway =", 1)[0]
    assert "Partial<ModelConnectionUpdate>" in update
    assert "updateConnection.mutate({ runtime, value:" in update
    assert "patch.is_default ?? runtime.is_default" in update
    assert "patch.enabled ?? runtime.enabled" in update
    assert "patch.price_discount_percent === undefined" in update
    assert "runtime.price_discount_percent ?? null" in update
    assert "brand_key" not in update
    assert "save.mutate" not in update
    assert "runtimePayload" not in update


def test_connection_default_actions_are_removed_without_removing_compatibility_fields() -> None:
    source = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(encoding="utf-8")
    row = source.split("function ConnectionRow(", 1)[1].split(
        "function GatewayCompatibilityRow(", 1,
    )[0]
    assert "connection-default-action" not in row
    assert "onDefaultRuntime" not in source
    assert "runtime.is_default" in row
    dialog = (FRONTEND_SOURCE / "components/model-management/connection-dialog.tsx").read_text(
        encoding="utf-8",
    )
    assert 'id="connection-default"' not in dialog
    assert "setIsDefault" not in dialog
    assert "is_default: enabled && runtime.is_default" in dialog


def test_connection_diagnostic_renders_localized_observation_time_not_live_health() -> None:
    source = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(encoding="utf-8")
    assert 'activeTab === "connections" ? "最近诊断" : "状态"' in source
    assert "formatTimeAgo(checkedAt, locale)" in source
    assert 'dateTime={checkedAt} title={checkedAt} data-no-localize' in source
    assert (
        "checkedAt={primaryChecking || !diagnosticMatches ? undefined : diagnostic?.checked_at}"
        in source
    )


def test_connection_save_error_uses_the_wrapping_notice_body() -> None:
    source = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(encoding="utf-8")
    error = source.split(
        '{error && <div className="registry-error smh-notice', 1,
    )[1].split("<ResizableGridTable", 1)[0]
    assert 'infrastructure-error-notice" role="alert"' in error
    assert '<TriangleAlert size={14} aria-hidden="true" />' in error
    assert '<div className="smh-notice-copy">保存失败：{String(error)}</div>' in error
    styles = read_frontend_styles()
    notice = styles.split(".smh-notice.infrastructure-error-notice {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: 16px minmax(0, 1fr)" in notice
    assert "align-items: center" in notice


def test_runtime_diagnostic_waits_for_safe_registry_cache_update() -> None:
    source = (
        FRONTEND_SOURCE / "components/model-management/runtime-diagnostic-dialog.tsx"
    ).read_text(encoding="utf-8")
    mutation = source.split("const mutation = useMutation({", 1)[1].split(
        "export function RuntimeDiagnosticDialog", 1,
    )[0]
    assert "onSuccess: async (result, request) =>" in mutation
    assert "await storeRegistry(client," in mutation
    assert "client.setQueryData" not in mutation
    assert "retry: false" in mutation


def test_runtime_diagnostic_progress_is_not_owned_by_the_modal() -> None:
    page = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(encoding="utf-8")
    diagnostic = (
        FRONTEND_SOURCE / "components/model-management/runtime-diagnostic-dialog.tsx"
    ).read_text(encoding="utf-8")
    assert "useMutationState" in diagnostic
    assert "client.getMutationCache().find({" in diagnostic
    assert 'status: "pending"' in diagnostic
    assert "health.isPending" not in page
    assert "health.reset" not in page
    assert "setDiagnosticRuntime(null); setDiagnosticRequestId(null)" in page
    assert "attempt.request.id === diagnosticRequestId" in page
    assert 'attempt.status === "pending"' in page
    assert "checkingRuntimeIds.has(runtime.id)" in page
    assert 'state={checking ? "checking" : healthState}' in page
    assert "<RuntimeHealth state={primaryState}" in page
    assert "runtimeHealthQueries[index]?.isLoading" not in page
    assert 'runtimeHealthState(runtime) === "checking") ? 2_000 : false' in page


def test_topology_and_models_share_one_inventory_query() -> None:
    source = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(encoding="utf-8")
    assert "const targetTopologies = useQuery(finopsQueries.apimTargetTopologies())" in source
    queries = (FRONTEND_SOURCE / "data-sources/apim/queries.ts").read_text(encoding="utf-8")
    assert "registry: registryQuery," in queries
    topology = queries.split("apimTargetTopologies: () => queryOptions({", 1)[1].split("}),", 1)[0]
    assert "...registryQuery()," in topology
    assert "registry.apim_target_topology ?? null" in topology
    assert 'apimTargetTopologies: ["control-plane"' not in queries
    prefetch = queries.split("export function prefetchPage(", 1)[1]
    assert "queryClient.prefetchQuery(finopsQueries.registry())" in prefetch
    assert "queryClient.prefetchQuery(finopsQueries.apimTargetTopologies())" not in prefetch


def test_apim_onboarding_limits_belong_to_dashboard_application() -> None:
    source = (
        FRONTEND_SOURCE / "components/model-management/apim-onboarding-dialog.tsx"
    ).read_text(encoding="utf-8")
    dashboard = source.split('aria-label="Dashboard 调用应用"', 1)[1].split("</section>", 1)[0]
    probe = source.split('aria-label="Probe 系统验证"', 1)[1].split("</section>", 1)[0]
    assert "<h3>Dashboard 调用应用</h3>" in dashboard
    assert "plan.dashboard_subscription_id" in dashboard
    assert "plan.initial_monthly_token_limit" in dashboard
    assert "plan.initial_tokens_per_minute" in dashboard
    assert "共享月度 Token 上限" in dashboard
    assert "共享每分钟 Token 上限 (TPM)" in dashboard
    assert "plan.probe_subscription_id" not in dashboard
    assert "plan.probe_subscription_id" in probe
    assert "plan.initial_monthly_token_limit" not in probe
    assert "plan.initial_tokens_per_minute" not in probe
    assert "<dt>初始月额度</dt>" not in source
    assert "<dt>TPM</dt>" not in source
    for locale in ("en", "ja", "ko"):
        phrases = (FRONTEND_SOURCE / f"locales/{locale}/phrases-core.ts").read_text(
            encoding="utf-8"
        )
        for label in (
            "Dashboard 调用应用", "Probe 系统验证",
            "共享月度 Token 上限", "共享每分钟 Token 上限 (TPM)",
        ):
            assert f'"{label}":' in phrases


def test_apim_onboarding_shows_display_name_and_azure_resource_name() -> None:
    source = (
        FRONTEND_SOURCE / "components/model-management/apim-onboarding-dialog.tsx"
    ).read_text(encoding="utf-8")
    assert "Turnstile 显示名称（可选）" in source
    assert "{plan.request.name ?? plan.service_name}" in source
    assert "<dt>Azure 资源名称</dt>" in source
    assert "<code data-no-localize>{plan.service_name}</code>" in source
    assert "apimOnboardingOperationName(operation)" in source
    workspace = (
        FRONTEND_SOURCE / "components/model-management/apim-gateway-workspace.tsx"
    ).read_text(encoding="utf-8")
    assert "profiles={registry.gateways}" in workspace
    assert "apimTargetDisplayName(item, registry.gateways)" in workspace
    assert "apimTargetDisplayName(item, profiles)" in workspace
    assert "<b data-no-localize title={displayName}>{displayName}</b>" in workspace
    for locale in ("en", "ja", "ko"):
        phrases = (FRONTEND_SOURCE / f"locales/{locale}/phrases-core.ts").read_text(
            encoding="utf-8"
        )
        assert '"Turnstile 显示名称（可选）":' in phrases
        assert '"Azure 资源名称":' in phrases


def test_apim_initialization_progress_is_part_of_its_inventory_row() -> None:
    workspace = (
        FRONTEND_SOURCE / "components/model-management/apim-gateway-workspace.tsx"
    ).read_text(encoding="utf-8")
    onboarding = (
        FRONTEND_SOURCE / "components/model-management/apim-onboarding-dialog.tsx"
    ).read_text(encoding="utf-8")
    table = workspace.split("function ApimTable(", 1)[1].split("function GatewayChildRow(", 1)[0]
    assert "ApimOnboardingProgress" not in workspace
    assert "<ApimOnboardingRow" in table
    assert "apimOnboardingRows(" in workspace
    assert "onboardingItems.length" in table
    assert (
        'className="connection-provider-group apim-target-group apim-onboarding-row"'
        in onboarding
    )
    assert 'className="connection-provider-heading apim-target-heading"' in onboarding
    assert 'className="apim-onboarding-details"' in onboarding
    assert 'aria-colspan={5}' in onboarding
    assert 'aria-expanded={expanded}' in onboarding
    assert "operation.error_message" in onboarding
    styles = read_frontend_styles()
    assert (
        '.apim-onboarding-details > [role="cell"], '
        '.gateway-provision-details > [role="cell"] '
        '{ min-width: 0; padding: 0 24px 12px; }' in styles
    )
    assert (
        '.apim-onboarding-details > [role="cell"], '
        '.gateway-provision-details > [role="cell"] { padding-inline: 20px; }' in styles
    )
    assert ".apim-onboarding-details .apim-onboarding-steps { padding-inline: 2px; }" in styles
    assert ".apim-onboarding-details .registry-error { margin: 10px 2px 0;" in styles


def test_apim_retry_is_a_visible_command_outside_collapsible_steps() -> None:
    source = (
        FRONTEND_SOURCE / "components/model-management/apim-onboarding-dialog.tsx"
    ).read_text(encoding="utf-8")
    actions = source.split('className="model-row-actions infrastructure-row-actions"', 1)[1]
    assert "apim-onboarding-retry" not in actions.split("</div>", 1)[0]
    assert 'className="apim-onboarding-progress" id={detailsId} hidden={!expanded}' in source
    assert 'hidden={!expanded && !failed && !operation.error_message}' in source
    error = source.split('className="registry-error apim-onboarding-error"', 1)[1]
    assert '<p role="alert">{operation.error_message || "初始化失败"}</p>' in error
    assert '<Button type="button" size="sm"' in error
    assert '<span>重试初始化</span>' in error
    assert 'size="icon-sm"' not in error
    assert 'disabled={!available || retryingId !== null}' in error
    assert 'onClick={() => onRetry(operation.id)}' in error
    styles = read_frontend_styles()
    assert (
        '.apim-onboarding-details .apim-onboarding-error > button.apim-onboarding-retry'
        in styles
    )
    assert 'width: auto; height: 28px; padding: 0 10px;' in styles
    assert 'max-width: 100%; font-size: 12px; line-height: 17px;' in styles


def test_infrastructure_status_uses_compact_row_typography() -> None:
    styles = read_frontend_styles()
    assert (
        ".infrastructure-resource-table .gateway-readiness > span "
        "{ font-size: 12px; line-height: 17px; }" in styles
    )


def test_infrastructure_actions_reveal_on_hover_without_hiding_disclosure() -> None:
    styles = read_frontend_styles()
    actions = (
        ".infrastructure-resource-table .infrastructure-row-actions > "
        ":is(.connection-health-action, .model-edit-action, .model-delete-action)"
    )
    hidden = actions + " { opacity: 0; pointer-events: none; }"
    assert hidden in styles
    reveal = styles.split(hidden, 1)[1].split("\n}", 1)[0]
    assert "@media (min-width: 761px) {" in reveal
    assert "opacity: 1; pointer-events: auto;" in reveal
    assert (
        ":is(.connection-provider-heading, .model-list-row):is(:hover, :has(:focus-visible))"
        in reveal
    )
    assert "(hover: hover)" not in reveal and "(pointer: fine)" not in reveal
    assert ":focus-within" not in reveal
    assert "apim-row-chevron" not in hidden + reveal
    assert "infrastructure-menu-trigger" not in hidden + reveal
    assert "display: none" not in hidden + reveal


def test_apim_name_editor_uses_narrow_write_and_backend_capability() -> None:
    workspace = (
        FRONTEND_SOURCE / "components/model-management/apim-gateway-workspace.tsx"
    ).read_text(encoding="utf-8")
    api = (FRONTEND_SOURCE / "data-sources/apim/api.ts").read_text(encoding="utf-8")
    editor = workspace.split("function InfrastructureNameEditor(", 1)[1].split(
        "function ApimEditor(", 1
    )[0]
    rename = api.split("renameGateway:", 1)[1].split("checkBudgetDelegation:", 1)[0]
    assert 'editLabel = "编辑 APIM 名称"' in workspace
    assert 'aria-label={editLabel}' in workspace
    assert '<Pencil size={14} />{editLabel}' in workspace
    assert 'supported={canManage && registry.gateway_name_edit_supported === true}' in workspace
    assert "await storeRegistry(queryClient, updatedRegistry)" in workspace
    assert "await storeGatewayName(queryClient, id, name)" in workspace
    assert 'gateway_name_edit_supported: registry.gateway_name_edit_supported === true' in api
    assert "{ name }" in rename and '"PATCH"' in rename
    assert 'Prefer: "return=minimal"' in rename
    assert "/gateways/${encodeURIComponent(id)}/name" in rename
    assert "saveGateway" not in editor
    assert 'maxLength={120}' in editor
    assert "onSubmit(value.trim())" in editor
    assert "disabled={!supported || busy || !valid}" in editor
    assert "当前后端版本不支持修改 APIM 显示名称" in editor


def test_apim_gateway_child_matches_parent_font_sizes() -> None:
    styles = read_frontend_styles()
    assert (
        ".apim-gateway-child-row .model-primary-cell b,\n"
        ".apim-gateway-child-row .model-runtime-cell b,\n"
        ".apim-gateway-child-row .model-pricing-cell b "
        "{ font-size: 12px; line-height: 17px; }" in styles
    )
    assert (
        ".apim-gateway-child-row .model-primary-cell code,\n"
        ".apim-gateway-child-row .model-runtime-cell span,\n"
        ".apim-gateway-child-row .model-pricing-cell span "
        "{ font-size: 10px; line-height: 14px; }" in styles
    )


def test_apim_inventory_includes_managed_gateway_without_self_hosted_actions() -> None:
    workspace = (
        FRONTEND_SOURCE / "components/model-management/apim-gateway-workspace.tsx"
    ).read_text(encoding="utf-8")
    table = workspace.split("function ApimTable(", 1)[1].split("function GatewayChildRow(", 1)[0]
    child = workspace.split("function GatewayChildRow(", 1)[1].split("function GatewayTable(", 1)[0]
    assert "const entries = apimGatewayEntries(item)" in table
    assert "`${entries.length} 个网关`" in table
    assert "entries.map((entry) => <GatewayChildRow" in table
    assert "managed ? <ManagedGatewayLogo size={16} />" in child
    assert "<ApimLogo" not in child
    assert 'const managed = entry.deployment.kind === "managed"' in child
    assert 'managed ? <b>托管网关</b>' in child
    assert 'data-gateway-kind={entry.deployment.kind}' in child
    assert (
        '{managed ? <div className="model-row-actions infrastructure-row-actions" '
        'role="cell" /> : <InfrastructureActions' in child
    )


def test_connection_onboarding_is_distinct_from_model_publication() -> None:
    app_source = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    navigation_source = (FRONTEND_SOURCE / "lib/navigation.ts").read_text(
        encoding="utf-8"
    )
    model_source = (
        FRONTEND_SOURCE / "components/model-management/model-publication-dialog.tsx"
    ).read_text(encoding="utf-8")
    connection_selection = (
        FRONTEND_SOURCE / "components/model-management/model-publication-connections.ts"
    ).read_text(encoding="utf-8")
    connection_source = (
        FRONTEND_SOURCE / "components/model-management/connection-dialog.tsx"
    ).read_text(encoding="utf-8")
    auth_switch_source = (
        FRONTEND_SOURCE
        / "components/model-management/foundry-auth-mode-switch.tsx"
    ).read_text(encoding="utf-8")
    field_help_source = (
        FRONTEND_SOURCE / "components/model-management/field-help.tsx"
    ).read_text(encoding="utf-8")
    page_source = (
        FRONTEND_SOURCE / "pages/model-management-page.tsx"
    ).read_text(encoding="utf-8")
    styles = read_frontend_styles()

    assert 'runtime.config.api_format === "openai_chat"' in connection_selection
    assert 'runtime.brand_key === "microsoft_foundry"' in connection_selection
    assert 'intent: "model" | "connection"' not in model_source
    assert "connectionIntent" not in model_source
    assert "Deployment Name" not in connection_source
    assert "价格与上下文" not in connection_source
    assert "useState<FoundryAuthMode>" in connection_source
    assert '"managed_identity"' in connection_source
    assert "foundry_inference_endpoint:" in connection_source
    assert 'foundryAuthMode === "api_key"' in connection_source
    assert "foundryApiKey" not in connection_source
    assert "Foundry API Key" not in connection_source
    assert 'template: "openai_compatible"' in connection_source
    assert "API Base URL" in connection_source
    assert "openai_base_url:" in connection_source
    assert "isOpenAICompatibleBaseUrl" in connection_source
    assert "OpenAI-compatible API" in model_source
    assert "<dt>接入类型</dt>" in model_source
    for connection_creation in (
        "NEW_RUNTIME", "creatingRuntime", "ProviderChoice", "providerChoices",
        "foundry_project_endpoint:", "foundry_inference_endpoint:",
        "bedrock_runtime_url:", "openai_base_url:", "model_vendor:",
        "template:", "ModelVendorSelect", "FoundryAuthModeSwitch", 'type="url"',
    ):
        assert connection_creation not in model_source
    assert "provider: { existing_id: selectedRuntime.provider_id }" in model_source
    assert "existing_id: selectedRuntime.id" in model_source
    assert 'id="existing-connection-api-key"' in model_source
    assert "publicModelKeyFromProviderId" in model_source
    assert "displayNameFromProviderId" in model_source
    assert "effectiveModelKey" in model_source
    assert "effectiveDisplayName" in model_source
    assert "上游模型 ID" in model_source
    assert "公开身份（可选）" not in model_source
    assert "公开模型别名" not in model_source
    assert 'id="provider-model-id"' in model_source
    assert 'htmlFor="provider-model-id"' in model_source
    assert "selectedRuntimeVendor" in model_source
    assert 'className="publication-connection-summary"' in model_source
    assert "<dt>API 服务商</dt>" in model_source
    assert "<ModelVendorLogo value={selectedRuntimeVendor}" in model_source
    assert 'aria-label="接入类型"' not in model_source
    assert 'selectedProvider?.provider_kind === "openai_compatible"' in model_source
    assert 'model_vendor: openAICompatible ? modelVendor' in connection_source
    assert 'editing ? "API 服务商" : "接入类型"' in connection_source
    assert '<ModelVendorSelect value={modelVendor}' in connection_source
    assert 'className="openai-compatible-identity-grid"' in connection_source
    vendor_source = (
        FRONTEND_SOURCE / "components/model-management/model-vendor-select.tsx"
    ).read_text(encoding="utf-8")
    assert '{ value: "kimi", label: "Kimi" }' in vendor_source
    assert '{ value: "deepseek", label: "DeepSeek" }' in vendor_source
    assert '{ value: "generic", label: "其他服务商" }' in vendor_source
    assert 'aria-label="API 服务商"' in vendor_source
    assert 'return <Database aria-hidden="true"' in vendor_source
    assert 'configured === "generic"' in vendor_source
    assert '.replaceAll("openai-compatible", "")' in vendor_source
    assert (
        ".openai-compatible-identity-grid { min-width: 0; display: grid; "
        "grid-template-columns: minmax(150px, 180px) minmax(0, 1fr); gap: 10px; }"
    ) in styles
    assert "<FoundryAuthModeSwitch" in connection_source
    assert "<FoundryAuthModeSwitch" not in model_source
    assert 'aria-label="Foundry 认证方式"' in auth_switch_source
    assert "同租户 · 仅 Project Endpoint" in auth_switch_source
    assert "跨租户 · Project + Inference Endpoint" in auth_switch_source
    assert 'dataSource.saveConnection' in page_source
    assert 'dataSource.updateConnection' in page_source
    assert '<ConnectionDialog' in page_source
    assert 'runtime={editingConnection ?? undefined}' in page_source
    assert 'setEditingConnection(item as ModelRuntime)' in page_source
    assert 'onUpdate' in connection_source
    assert 'editing ? "编辑连接" : "添加连接"' in connection_source
    assert "Configuration JSON" not in connection_source
    assert "参数 JSON" not in connection_source
    assert "ProviderBrandLogo" in connection_source
    assert "GatewayBrandLogo" in connection_source
    assert '{ value: "litellm", label: "LiteLLM", disabled: true }' in page_source
    assert '{ value: "direct", label: "Direct / Custom", disabled: true }' in page_source
    assert "disabled={option.disabled}" in page_source
    assert 'ProviderBrandLogo brand={providerBrand(provider)}' in page_source
    assert '<SelectItem value="all"><span className="registry-option"><Database' in page_source
    assert 'FINOPS_NAVIGATE_EVENT = "finops:navigate"' in navigation_source
    assert "window.addEventListener(FINOPS_NAVIGATE_EVENT, syncPageFromUrl)" in app_source
    assert 'window.addEventListener("popstate", syncPageFromUrl)' in app_source
    assert 'openNewPublication("connection")' not in page_source
    assert 'intent={publicationIntent}' not in page_source
    assert "selectedRuntimeNeedsCredential" in model_source
    assert "api_key: selectedRuntimeNeedsCredential" in model_source
    assert "一次性 API Key" in model_source
    assert "首次发布凭据" in model_source
    assert "后续模型无需重复提供" in model_source
    assert "等待首次模型验证" in page_source
    assert 'className="connection-pending-primary"' in page_source
    assert "<CircleDashed size={12} /><b>待验证</b>" in page_source
    assert "</span><small>添加首个模型</small>" in page_source
    assert (
        ".connection-pending-status { max-width: 100%; display: inline-grid; "
        "align-items: start; gap: 3px;"
    ) in styles
    assert (
        ".connection-pending-primary { min-width: 0; display: inline-flex; "
        "align-items: center; gap: 5px;"
    ) in styles
    assert "此处不收 API Key" in connection_source
    assert "<FieldHelp>" in connection_source
    assert "<FieldHelp>" in model_source
    assert '<p className="registry-field-help">' not in connection_source
    assert '<p className="registry-field-help">' not in model_source
    assert "connection-state-note" not in connection_source
    assert 'import { Tooltip } from "@base-ui/react/tooltip"' in field_help_source
    assert "CircleHelp" in field_help_source
    assert 'aria-label="查看说明"' in field_help_source
    assert "dataSource.deleteConnection" in page_source
    assert "dataSource.deleteGateway" in page_source
    assert "删除网关记录" in page_source
    assert "ConnectionDeleteDialog" in page_source
    assert "请先删除此连接中的全部模型" in page_source
    assert "历史用量、publication 和旧 APIM 资源会保留" in page_source
    recovery_source = page_source.split("const recoverable =", 1)[1].split(
        "if (recoverable)", 1
    )[0]
    assert '"failed"' in recovery_source
    assert '"awaiting_authorization"' not in recovery_source
    nonterminal_notice = page_source.split("} else if (publication) {", 1)[1].split(
        "}", 1
    )[0]
    assert "setNotice(null)" in nonterminal_notice
    assert 'runtime?.config.auth_strategy === "named_value_api_key"' in (
        FRONTEND_SOURCE / "pages/model-management-page.tsx"
    ).read_text(encoding="utf-8")
    assert '.foundry-auth-mode { width: 100%;' in styles
    assert "border-radius: 8px" in styles.split(".foundry-auth-mode {", 1)[1].split("}", 1)[0]
    assert '.foundry-auth-mode > button { min-width: 0; min-height: 50px;' in styles
    button_rule = styles.split(".foundry-auth-mode > button {", 1)[1].split("}", 1)[0]
    assert "border-radius: 6px" in button_rule
    assert '.foundry-auth-mode > button[aria-pressed="true"]' in styles
    active_rule = styles.split(
        '.foundry-auth-mode > button[aria-pressed="true"]', 1
    )[1].split("}", 1)[0]
    assert "background: var(--background)" in active_rule
    assert "box-shadow: 0 1px 2px" in active_rule
    assert "border" not in active_rule
    select_rule = styles.split(
        ".registry-field .registry-select-trigger {", 1
    )[1].split("}", 1)[0]
    assert "width: 100%" in select_rule
    assert "min-width: 0" in select_rule
    assert "max-width: 100%" in select_rule
    safety_note_rule = styles.split(
        ".publication-safety-note {", 1
    )[1].split("}", 1)[0]
    assert "align-items: center" in safety_note_rule
    assert "padding: 8px 10px" in safety_note_rule
    safety_icon_rule = styles.split(
        ".publication-safety-note > svg {", 1
    )[1].split("}", 1)[0]
    assert "margin-top" not in safety_icon_rule


def test_model_publication_is_gateway_first_and_connection_scoped() -> None:
    source = (
        FRONTEND_SOURCE / "components/model-management/model-publication-dialog.tsx"
    ).read_text(encoding="utf-8")

    assert source.index('className="simple-model-section publication-target"') < source.index(
        'aria-labelledby="publication-connection-heading"'
    ) < source.index('aria-labelledby="publication-model-heading"') < source.index(
        'aria-labelledby="publication-pricing-heading"'
    )
    assert "apimGateways.length > 1" in source
    assert 'className="publication-target-summary"' in source
    assert "publicationConnections(registry, gatewayId)" in source
    assert "const selectedRuntime = runtimeId === null" in source
    assert "? preferredPublicationConnection(apimRuntimes)" in source
    assert ": apimRuntimes.find((runtime) => runtime.id === runtimeId)" in source
    assert "preferredPublicationConnection(nextConnections, runtimeId ?? undefined)" in source
    assert 'aria-label="连接"' in source
    assert 'aria-label="运行时"' not in source
    assert 'aria-label="接入方式"' not in source
    assert "新建连接" not in source
    assert "newConnectionChoices" not in source
    assert 'onClick={onManageConnections}>管理连接' in source
    assert "此网关没有可用连接。请先到连接管理添加并启用连接。" in source
    assert "apimRuntimes.length > 0" in source
    assert 'type="button"' in source
    assert '<Plug size={15} aria-hidden="true" />' in source
    assert "!selectedGateway || !selectedRuntime || !selectedProvider" in source
    assert "价格与限制" in source
    assert "价格未填写" in source
    assert "价格部分填写" in source
    assert "价格已填写" in source
    assert "context_window: imageGeneration ? null : numberOrNull(contextWindow)" in source
    assert (
        'operation: decision ? "decision" : imageGeneration ? "image_generation" : undefined'
        in source
    )
    assert 'registry.image_generation_supported !== true' in source
    assert (
        'cache_write_cost_per_million: decision || imageGeneration || '
        'pricing.contextPricingPreview ? null'
        in source
    )
    assert "const numericError = optionalNumberError(" in source

    reset = source.split("const clearConnectionInputs = () => {", 1)[1].split("\n  }", 1)[0]
    for setter in (
        "setProviderApiKey", "setUpstreamModelId", "setFoundryDeployment",
        "setModelKey", "setDisplayName", "setContextWindow",
    ):
        assert f'{setter}("")' in reset
    assert "setKeyRevealed(false)" in reset
    assert (
        "setPricing(createModelPriceDraft(registry.context_pricing_supported === true, "
        "registry.context_price_rules_supported === true))" in reset
    )
    for handler in ("chooseGateway", "chooseRuntime"):
        body = source.split(f"const {handler} = ", 1)[1].split("\n  }", 1)[0]
        assert "clearConnectionInputs()" in body

    page = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(encoding="utf-8")
    navigation = page.split("onManageConnections={() => {", 1)[1].split("}}", 1)[0]
    assert "closePublication()" in navigation
    assert 'changeTab("connections")' in navigation
    assert "setConnectionOpen(true)" not in navigation


def test_foundry_model_fields_share_a_localized_responsive_row() -> None:
    source = (
        FRONTEND_SOURCE / "components/model-management/model-publication-dialog.tsx"
    ).read_text(encoding="utf-8")
    fields = source.split(
        'className="simple-model-grid publication-model-identity-grid"', 1,
    )[1].split("openAICompatible ?", 1)[0]
    assert 'className="registry-select-trigger" aria-label="模型用途"' in fields
    assert 'htmlFor="foundry-deployment">部署名称</label>' in fields
    assert "Deployment Name" not in fields
    styles = read_frontend_styles()
    layout = styles.split(".publication-model-identity-grid {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: minmax(0, 180px) minmax(0, 1fr)" in layout
    assert "align-items: end" in layout
    mobile = styles.split("@media (max-width: 640px)", 1)[1]
    assert ".simple-model-grid { grid-template-columns: 1fr; }" in mobile
    for locale in ("en", "ja", "ko"):
        phrases = (
            FRONTEND_SOURCE / f"locales/{locale}/phrases-image-generation.ts"
        ).read_text(encoding="utf-8")
        assert '"部署名称":' in phrases


def test_manage_connections_is_a_transparent_secondary_action() -> None:
    styles = read_frontend_styles()
    rule = styles.split(".publication-connection-action {", 1)[1].split("}", 1)[0]
    assert "appearance: none" in rule
    assert "background: transparent" in rule
    assert "color: var(--muted-foreground)" in rule
    assert "height: 24px" in rule
    hover = styles.split(
        ".publication-connection-action:hover:not(:disabled) {", 1
    )[1].split("}", 1)[0]
    assert "background: var(--muted)" in hover
    assert "color: var(--foreground)" in hover
    assert ".publication-connection-action:focus-visible" in styles


def test_connections_own_the_runtime_inventory_and_detail_route() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(
        encoding="utf-8"
    )
    apim_types = (
        FRONTEND_SOURCE / "data-sources/apim/types.ts"
    ).read_text(encoding="utf-8")
    apim_api = (
        FRONTEND_SOURCE / "data-sources/apim/api.ts"
    ).read_text(encoding="utf-8")
    apim_source = (
        FRONTEND_SOURCE / "data-sources/apim/source.tsx"
    ).read_text(encoding="utf-8")

    assert '"runtime-config"' not in app
    assert '"runtime-config"' not in apim_source
    assert 'url.searchParams.set("page", "models")' in page
    assert 'url.searchParams.set("tab", "connections")' in page
    assert "function RuntimeWorkspace" not in page
    assert "function ConnectionRow" in page
    assert 'aria-label="按诊断结果筛选"' in page
    assert 'className="connection-health-action"' in page
    assert 'className="connection-default-action"' in page
    assert "onCheckRuntime" in page
    assert "onDefaultRuntime" not in page
    assert "apimGatewayProfiles(registry.gateways, topology" in (
        FRONTEND_SOURCE
        / "components/model-management/connection-dialog.tsx"
    ).read_text(encoding="utf-8")
    assert "useQueries" in page
    assert 'from "../lib/runtime-health"' in page
    assert "function runtimeWithTelemetryHealth" in (
        FRONTEND_SOURCE / "lib/runtime-health.ts"
    ).read_text(encoding="utf-8")
    assert "runtimeWithTelemetryHealth(runtime, runtimeRequests)" in page
    assert "runtimeHealthQueries[index]?.data ?? []" in page
    assert "checkingRuntimeIds.has(runtime.id)" in page
    assert "const inventoryConnections = registry.runtimes" in page
    assert "copilot_cli" not in page
    assert "CopilotLogo" not in page
    assert 'RuntimeKind = "foundry" | "openai_compatible"' in apim_types
    assert 'provider_kind: "anthropic" | "microsoft_foundry" | "openai_compatible"' in apim_types
    assert 'String(runtime.runtime_kind) !== "copilot_cli"' in apim_api
    assert 'String(runtime.brand_key) !== "github"' in apim_api
    assert 'String(provider.provider_kind) !== "github"' in apim_api
    assert 'String(provider.brand_key) !== "github"' in apim_api
    assert 'String(model.family_key) !== "copilot"' in apim_api
    assert "RETIRED_LITELLM_GATEWAY_ID" in apim_api
    assert "gateway.id !== RETIRED_LITELLM_GATEWAY_ID" in apim_api
    assert "const inventoryProviderIds = new Set(" in page
    assert ".filter((provider) => inventoryProviderIds.has(provider.id))" in page
    assert "const matchingProviderIds = new Set(providers" in page
    assert "matchingProviderIds.has(runtime.provider_id)" in page
    assert "gatewayEntries(topology).length" in page
    assert "GatewayApiRow" not in page
    health_counts = page.split("const connectionHealthCounts = {", 1)[1].split(
        "}", 1
    )[0]
    assert "inventoryConnections.filter" in health_counts
    assert 'value === "active" || connectionHealthCounts[value] > 0' in page
    assert ">连接</a><ChevronRight" in page


def test_apim_and_gateway_creation_use_dedicated_external_resource_flows() -> None:
    page = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(
        encoding="utf-8"
    )
    workspace = (
        FRONTEND_SOURCE
        / "components/model-management/apim-gateway-workspace.tsx"
    ).read_text(encoding="utf-8")
    styles = read_frontend_styles()

    assert "ApimGatewayWorkspace" in page
    assert 'tab === "apim" || tab === "gateways"' in page
    assert 'import { ApimLogo, ManagedGatewayLogo } from "../brand-logos"' in workspace
    assert "<ApimLogo size={16} />" in workspace
    assert "CloudCog" not in workspace
    assert "添加 APIM 实例" in workspace
    assert "添加自托管网关" in workspace
    assert "addApimTarget" in workspace
    assert "addSelfHostedGateway" in workspace
    assert "validateSelfHostedGateway" in workspace
    assert "changeSelfHostedGatewayAssociation" in workspace
    assert "const canChangeAssociation = (topology?.items.length ?? 0) > 1" in workspace
    assert "onBinding={onChange}" in workspace
    assert 'aria-label="APIM 绑定"' in workspace
    assert "canChangeAssociation={canChangeAssociation}" in workspace
    assert "...item.deployments.flatMap" in workspace
    assert "deployment.native_gateway_id" in workspace
    assert "...publishedModels.map" in workspace
    assert "apim-hierarchy-toggle" in workspace
    assert "aria-expanded={expanded}" in workspace
    assert 'className="apim-gateway-children"' in workspace
    assert "hidden={!expanded}" in workspace
    assert 'className="connection-provider-group apim-target-group"' in workspace
    assert 'className="connection-provider-heading apim-target-heading"' in workspace
    assert 'className="model-list-row connection-list-row' in workspace
    assert 'className="connection-branch"' in workspace
    assert 'className="model-list-icon gateway-child-icon"' in workspace
    assert "<GatewayChildRow" in workspace
    assert "gatewayEvidence(entry)" in workspace
    assert "timestamp={evidence.probedAt}" in workspace
    assert 'user?.role === "owner"' in workspace
    assert "onSettled: refresh" in workspace
    assert "pendingInventorySyncId" in workspace
    assert "removal-preview" in workspace
    assert "不会创建或部署网关" in workspace
    assert "不会远程修改或重启网关" in workspace
    assert "ResizableGridTable" in workspace
    assert 'className="model-list-row infrastructure-resource-row' in workspace
    assert "model-table infrastructure-resource-table ${kind}" in workspace
    assert "<InfrastructureTableLoading mode={mode} />" in workspace
    assert (
        'className="connection-provider-group apim-target-group '
        'infrastructure-loading-group"' in workspace
    )
    assert (
        'className="connection-provider-heading apim-target-heading '
        'infrastructure-loading-row"' in workspace
    )
    assert (
        'className="model-list-icon gateway-child-icon '
        'infrastructure-skeleton-icon"' in workspace
    )
    assert "<b>加载资源拓扑</b>" not in workspace
    assert "<InfrastructureTableError mode={mode} error={error} />" in workspace
    assert 'className="registry-loading"' not in workspace
    assert (
        '{!loading && !topologyUnavailable && <footer className="model-table-footer">'
        in workspace
    )
    assert (
        'apim: topology ? new Set([...topology.items.map(item => '
        'item.target.gateway_profile_id), ...initializingIds]).size : "—"'
        in workspace
    )
    assert "targets.length + onboardingItems.length" in workspace
    assert (
        'gateways: topology ? registeredGateways.length + gatewayProvisions.length : "—"'
        in workspace
    )
    gateway_table = workspace.split("function GatewayTable(", 1)[1].split(
        "function InfrastructureActions(", 1,
    )[0]
    assert "<SelfHostedGatewayProvisionProgress" in gateway_table
    assert "!entries.length && !provisionItems.length && !provisionError" in gateway_table
    assert 'mode === "gateways" && <GatewayTable' in workspace
    assert 'mode === "gateways" && <SelfHostedGatewayBootstrapDialog' in workspace
    assert ".gateway-provision-item::before" in styles
    assert "inset: 0 16px;" in styles
    assert ".gateway-provision-item::before { inset-inline: 12px; }" in styles
    assert 'className="infrastructure-table"' not in workspace
    assert ".infrastructure-table" not in styles
    assert ".apim-hierarchy-toggle:hover .apim-hierarchy-logo" in styles
    assert '.apim-hierarchy-toggle[aria-expanded="true"]' in styles
    assert ".connection-provider-heading::before" in styles
    assert ".connection-list-row::before" in styles
    assert ".infrastructure-resource-table .connection-provider-heading" in styles
    assert (
        ".infrastructure-resource-table "
        ".model-status-cell:not(.infrastructure-skeleton-cell)" in styles
    )
    assert "align-self: stretch; display: flex; align-items: center;" in styles
    assert (
        ".connection-list-row.apim-gateway-child-row "
        ".model-row-actions { margin-right: 0; }" in styles
    )
    assert 'className="connection-provider-chevron apim-row-chevron"' in workspace
    assert 'className="apim-row-chevron-placeholder"' in workspace
    assert "resourceTableColumnMinWidths = [180, 120, 160, 80, 136]" in workspace
    assert ".infrastructure-resource-table { --model-table-actions-width: 136px; }" in styles
    assert ".infrastructure-row-actions { width: 117px; display: flex; gap: 3px;" in styles
    assert (
        '.apim-row-chevron-placeholder { flex: 0 0 16px; '
        'width: 16px; height: 28px; margin: 0; }' in styles
    )
    assert ".connection-provider-chevron.apim-row-chevron" in styles
    assert '.apim-row-chevron[aria-expanded="true"]' in styles
    assert ".infrastructure-skeleton-cell { min-width: 0; display: grid;" in styles
    assert "display: grid !important; align-content: center" not in styles
    assert "width: min(148px, 100%)" in styles


def test_publication_selects_instance_then_gateway_with_target_scoped_connection() -> None:
    page = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(
        encoding="utf-8"
    )
    publication = (
        FRONTEND_SOURCE / "components/model-management/model-publication-dialog.tsx"
    ).read_text(encoding="utf-8")
    apim_api = (
        FRONTEND_SOURCE / "data-sources/apim/api.ts"
    ).read_text(encoding="utf-8")

    assert 'type Tab = ModelManagementTab' in page
    assert 'tab === "apim"' in page
    assert "目标网关" in publication
    assert 'aria-label="Gateway API"' in publication
    assert "choice.kind === \"self_hosted\"" in publication
    assert "topologyAvailable" in publication
    assert "topologyLoading" in publication
    assert 'className="simple-target-state"' in publication
    assert (
        "topologyAvailable && (topologyLoading || Boolean(topologyError) || !topology"
        in publication
    )
    assert "apimGatewayProfiles" in publication
    assert "resolveGatewaySelection" in publication
    assert "title={choice.reason ?? choice.endpoint ?? undefined}" in publication
    assert "gatewayPublicationChoices" in publication
    assert "selectedGatewayApi?.selectable" in publication
    assert "targetChoices.map" in publication
    assert 'aria-label="APIM 实例"' in publication
    assert "choice.targetId !== gatewayId" in publication
    assert 'mode: "selected", deployment_ids: [selectedGatewayApi.deploymentId]' in publication
    assert "publicationConnections(registry, gatewayId)" in publication
    assert "Managed APIM 与 Self-hosted Gateway 是并列的调用入口" not in publication
    assert "deployment-scope-mode" not in publication
    assert "Serving Gateway scope" not in publication
    assert "scopeMode" not in publication
    assert "const targetTopologies = useQuery(finopsQueries.apimTargetTopologies())" in page
    assert "topologyAvailable={data.apim_target_topology_available === true}" in page
    assert "const tabs = topologyAvailable" in page
    assert 'topology?.total ?? "—"' in page
    assert ".then(normalizeApimTargetTopologies)" in apim_api
    assert "resource_lifecycle_available" in apim_api
    assert "当前独立 API 版本仅支持查看 APIM 与网关" in (
        FRONTEND_SOURCE
        / "components/model-management/apim-gateway-workspace.tsx"
    ).read_text(encoding="utf-8")
    assert "previewApimTargetRemoval" in apim_api
    assert "removeSelfHostedGateway" in apim_api


def test_terminal_rolled_back_publication_is_not_rendered_as_a_model() -> None:
    page = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(
        encoding="utf-8"
    )
    row_statuses = page.split(
        'const rowStatuses = new Set<GatewayPublication["status"]>([', 1
    )[1].split("])", 1)[0]

    assert '"failed"' in row_statuses
    assert '"rolled_back"' not in row_statuses
    assert "PublicationModelRow" in page


def test_publication_targets_remain_visible_across_mobile_columns() -> None:
    page = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(
        encoding="utf-8"
    )
    dialog = (
        FRONTEND_SOURCE / "components/model-management/model-publication-dialog.tsx"
    ).read_text(encoding="utf-8")
    styles = read_frontend_styles()
    row = page.split("function PublicationModelRow(", 1)[1].split("\nfunction ", 1)[0]

    assert row.count("<ModelTargetGatewayCell ") == 1
    assert "<PublicationTargetDetails " not in row
    assert '...(activeTab === "models" ? ["目标网关"] : [])' in page
    assert "等待 Registry 激活" not in row
    assert ".model-table.models .model-list-row > :nth-child(3) { display: grid; }" in styles
    assert "models: [160, 120, 120, 160, 80, 125]" in page
    assert "const MODEL_COMPACT_COLUMN_MIN_WIDTHS = [160, 120, 120, 160, 80, 63]" in page
    assert "<ModelTargetGatewayCell target={target} />" in page
    assert "当前模型继续正常服务" not in dialog
    target_layout = styles.split(".publication-target-details {", 1)[1].split("}", 1)[0]
    target_label = styles.split(".publication-target-details dt {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: max-content minmax(0, 1fr)" in target_layout
    assert "white-space: nowrap" in target_label


def test_apim_native_routes_have_one_dedicated_qualified_surface() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    model_page = (
        FRONTEND_SOURCE / "pages/model-management-page.tsx"
    ).read_text(encoding="utf-8")
    native_page = (
        FRONTEND_SOURCE / "pages/apim-native-routes-page.tsx"
    ).read_text(encoding="utf-8")
    create_dialog = (
        FRONTEND_SOURCE
        / "components/model-management/apim-native-route-create-dialog.tsx"
    ).read_text(encoding="utf-8")
    resizable_pane = (
        FRONTEND_SOURCE / "components/ui/use-resizable-pane.ts"
    ).read_text(encoding="utf-8")
    eligibility = (
        FRONTEND_SOURCE
        / "components/model-management/apim-native-route-eligibility.ts"
    ).read_text(encoding="utf-8")
    styles = read_frontend_styles()

    assert '{ label: "负载均衡", icon: Network, page: "apim-native-routes" }' in app
    assert 'page === "apim-native-routes" && <ApimNativeRoutesPage routeDrawerOpen=' in app
    assert 'aria-label="打开负载均衡列表"' in app
    assert 'className="settings-mobile-topbar-end-action"' in app
    assert "ApimNativeRouteDialog" not in model_page
    assert "model-route-action" not in model_page
    assert "配置 APIM 后端池" not in model_page
    assert "<DeploymentResilienceEditor" in native_page
    assert "renderFrame={(editor, controls)" in native_page
    assert (
        'className="runtime-detail-sidecard apim-native-route-config-card">{editor}'
        in native_page
    )
    assert 'className="router-split apim-native-routes-layout"' in native_page
    assert 'className="router-list-pane apim-native-route-list"' in native_page
    assert '!isMobile && <aside className="router-list-pane apim-native-route-list"' in native_page
    assert '<Dialog open={routeDrawerOpen} onOpenChange={onRouteDrawerOpenChange}>' in native_page
    assert 'className="apim-native-route-drawer"' in native_page
    assert (
        'className="router-list-pane apim-native-route-list '
        'apim-native-route-drawer-list"' in native_page
    )
    assert 'if (closeOnSelect) onRouteDrawerOpenChange(false)' in native_page
    assert 'className="router-list-controls apim-native-route-list-controls"' in native_page
    assert 'className="router-editor-pane apim-native-route-detail"' in native_page
    assert 'className="smh-search"' in native_page
    assert (
        ".apim-native-route-list.router-list-pane .apim-native-route-list-controls { "
        "min-height: 54px; flex-basis: 54px; padding: 12px 16px 6px; border-bottom: 0; }"
    ) in styles
    assert ".apim-native-route-list .router-list-row::before { inset-inline: 16px; }" in styles
    assert ".apim-native-route-list .router-list-scroll { padding-top: 6px; }" in styles
    assert '.apim-native-routes-workspace > .smh-page-header { display: none; }' in styles
    assert (
        "body:has(.apim-native-route-drawer) .search-dialog-overlay { "
        "inset: 48px 0 0; }"
    ) in styles
    assert '.apim-native-route-drawer-list.router-list-pane' in styles
    assert '.apim-native-route-drawer-list .router-list-row { padding-inline: 28px; }' in styles
    assert 'className="apim-native-route-drawer-close"' in native_page
    assert '.apim-native-route-drawer-close { width: 28px; min-width: 28px; height: 28px;' in styles
    assert (
        ".apim-native-route-drawer-close:hover, "
        ".apim-native-route-drawer-close:focus-visible"
    ) in styles
    assert '.apim-native-route-toolbar { align-items: center; flex-direction: row;' in styles
    assert (
        ".apim-native-route-toolbar-actions { width: auto; flex: 1 1 auto; "
        "justify-content: flex-end; }"
    ) in styles
    assert 'className="runtime-pane-handle"' in native_page
    assert "router-list-section-label" not in native_page
    assert "<footer>" not in native_page
    assert "useResizablePane" in native_page
    assert "turnstile_apim_native_route_pane_width" in native_page
    assert "onPointerDown={startPaneResize}" in native_page
    assert "onKeyDown={resizePaneWithKeyboard}" in native_page
    assert "onDoubleClick={resetPaneWidth}" in native_page
    handle_rule = styles.split(".runtime-pane-handle {", 1)[1].split("}", 1)[0]
    assert "z-index: 2" in handle_rule
    assert "width: 1px" in handle_rule
    assert "background: var(--border)" in handle_rule
    assert "cursor: col-resize" in handle_rule
    assert (
        '.runtime-pane-handle::after { content: ""; position: absolute; '
        "inset: 0 -3px; }"
    ) in styles
    assert ".runtime-pane-handle:focus-visible { background: var(--ring);" in styles
    assert "localStorage.getItem(storageKey)" in resizable_pane
    assert "routeStateLoading ?" in native_page
    assert "routeStateError || !registry.data" in native_page
    assert "没有匹配的后端池" in native_page
    assert "model-search-control" not in native_page
    assert "ModelPublicationDialog" not in native_page
    assert "ApimNativeRouteCreateDialog" in native_page
    assert "useQueries" in native_page
    assert "poolQuery?.data" in native_page
    assert 'replicaOwner\n        ? "replica"' in native_page
    assert 'poolQuery?.data\n              ? "active"\n              : "available"' in native_page
    assert 'user?.role === "owner"' in native_page
    assert "添加后端池" in native_page
    assert "DeploymentResilienceEditor" in create_dialog
    assert 'option.state === "available"' in create_dialog
    assert 'option.state === "ineligible"' in create_dialog
    assert 'option.state === "replica"' in create_dialog
    assert "onActivated={finishCreation}" in create_dialog
    assert "missingDeploymentRuntimeNames" in create_dialog
    assert "尚不能创建后端池" in create_dialog
    assert "前往模型管理" in create_dialog
    assert "前往连接管理" in create_dialog
    assert "onManageModels" in create_dialog
    assert "onManageRuntimes" in create_dialog
    assert "ModelPublicationDialog" not in create_dialog
    assert "Project Endpoint" not in create_dialog
    assert "提供方" not in create_dialog
    assert "hasNativeRouteCounterpart(model, registry.data!)" in native_page
    assert "compatibleNativeRouteRuntimes(model, registry.data)" in native_page
    assert "nativeRouteUpstreamIdentity(model)" in native_page
    assert "equivalentNativeRouteRuntimes(selectedModel, registry.data)" in native_page
    assert "<dt>数据面</dt><dd>Azure API Management</dd>" in native_page
    assert "<dt>上游 Deployment</dt>" in native_page
    assert "<dt>作用范围</dt>" in native_page
    assert "<dt>FastAPI Router</dt>" not in native_page
    assert 'className="runtime-detail-identity"' not in native_page
    assert "apim-native-route-logo" not in native_page
    assert (
        ".runtime-detail-grid { display: grid; grid-template-columns: "
        "minmax(0, 1fr) 320px; gap: 16px; padding: 14px; }"
    ) in styles
    assert ".runtime-detail-grid { grid-template-columns: 1fr; padding: 10px; }" in styles
    assert (
        ".runtime-detail-grid.apim-native-route-detail-grid { "
        "grid-template-columns: minmax(0, 1fr); padding-inline: 16px; }"
    ) in styles
    assert ".apim-native-route-toolbar { min-height: 48px" in styles
    assert ".apim-native-route-hero .runtime-detail-facts" in styles
    assert "runtimeIdsWithDeployment.has(runtime.id)" in eligibility
    assert "runtime.id !== model.runtime_id" in eligibility
    assert ".apim-native-routes-layout" in styles
    assert ".apim-native-route-config-card { padding: 16px" in styles
    assert ".apim-native-route-content" not in styles
    assert ".apim-native-route-readiness" in styles
    assert ".apim-native-route-dialog" not in styles
