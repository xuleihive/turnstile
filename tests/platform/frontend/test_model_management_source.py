import shutil
import subprocess
from pathlib import Path

import pytest

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT, read_frontend_styles


@pytest.mark.parametrize(
    "suite_name",
    (
        "model-edit-form.test.mjs",
        "model-publication-connections.test.mjs",
        "openai-compatible.test.mjs",
    ),
)
def test_model_form_unit_cases(suite_name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend unit tests"
    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--test",
            str(Path(__file__).with_name(suite_name)),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_model_editing_uses_the_dedicated_dialog_without_generic_identity_inputs() -> None:
    page = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(encoding="utf-8")
    dialog = (
        FRONTEND_SOURCE / "components/model-management/model-edit-dialog.tsx"
    ).read_text(encoding="utf-8")
    generic = page.split("function RegistryEditor(", 1)[1].split("function AuthSelect(", 1)[0]

    assert "<ModelEditDialog" in page
    assert "model={editingModel}" in page
    assert 'kind: "model", id: editingModel.id, value' in page
    assert "onEditModel={openModelEditor}" in page
    assert "openModelEditor(item as ManagedModel)" in page
    assert 'kind === "model"' not in generic
    assert "model_key" not in generic
    assert "save.reset()" in page
    assert "setEditingModel(null)" in page
    assert "<code>{model.model_key}</code>" in dialog
    assert "<code>{model.upstream_model_id" in dialog
    assert "<Select" not in dialog
    assert "draft.capabilities" not in dialog
    assert "roleOptions.map" in dialog
    assert "modelEditPayload(model, draft)" in dialog
    assert "if (busy || !dirty || validation) return" in dialog
    assert "if (!busy) onClose()" in dialog
    assert "disabled={busy || !dirty || Boolean(validation) || imagePriceMissing}" in dialog
    assert 'role="alert">{message}' in dialog


def test_model_editor_layout_contains_long_identity_and_role_values() -> None:
    styles = read_frontend_styles()

    assert ".registry-editor-dialog.model-edit-dialog" in styles
    assert "calc(100dvh - 24px)" in styles
    assert ".model-editor-identity dd > span { min-width: 0; white-space: normal;" in styles
    assert ".model-editor-checkbox > span { min-width: 0; overflow-wrap: anywhere; }" in styles
    assert (
        ".model-edit-dialog .form-grid, .model-edit-dialog .form-grid.three { "
        "grid-template-columns: minmax(0, 1fr); }"
    ) in styles
    assert ".model-editor-identity { grid-template-columns: 1fr; }" in styles
    assert (
        ".model-editor-checkbox-grid { "
        "grid-template-columns: repeat(2, minmax(0, 1fr)); }"
    ) in styles


def test_connection_onboarding_is_distinct_from_model_publication() -> None:
    app_source = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    navigation_source = (FRONTEND_SOURCE / "lib/navigation.ts").read_text(
        encoding="utf-8"
    )
    model_source = (
        FRONTEND_SOURCE / "components/model-management/model-publication-dialog.tsx"
    ).read_text(encoding="utf-8")
    connection_selection_source = (
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

    assert 'runtime.config.api_format === "openai_chat"' in connection_selection_source
    assert 'runtime.brand_key === "microsoft_foundry"' in connection_selection_source
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
    assert "publicationConnectionTarget(registry," in model_source
    assert "needsCredential ? { api_key: apiKey.trim() } : {}" in connection_selection_source
    assert "一次性 API Key" in model_source
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
    assert "当前所选 APIM 的 Managed Identity" in connection_source
    assert "Cognitive Services User" in connection_source
    assert "仅位于同一租户不会自动获得访问权限" in connection_source
    assert ".field-help-popup { max-width: min(360px" in styles
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


def test_model_publication_selects_existing_connections_and_clears_connection_drafts() -> None:
    source = (
        FRONTEND_SOURCE / "components/model-management/model-publication-dialog.tsx"
    ).read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(encoding="utf-8")

    assert "publicationConnections(registry, gatewayId)" in source
    assert "preferredPublicationConnection(apimRuntimes)?.id" in source
    assert "apimRuntimes.find((runtime) => runtime.id === runtimeId)" in source
    assert "preferredPublicationConnection(nextConnections, runtimeId)?.id" in source
    assert 'aria-label="连接"' in source
    assert "selectedRuntimeNeedsCredential && !providerApiKey.trim()" in source
    assert "!selectedRuntime || !selectedProvider || requiredCredentialMissing" in source
    for removed in (
        "NEW_RUNTIME", "providerChoices", "foundry_project_endpoint:",
        "foundry_inference_endpoint:", "bedrock_runtime_url:", "creatingRuntime",
    ):
        assert removed not in source

    clear_draft = source.split("const clearConnectionInputs = () => {", 1)[1].split(
        "const chooseGateway", 1
    )[0]
    for setter in (
        "setProviderApiKey", "setFoundryDeployment", "setModelKey", "setDisplayName",
        "setUpstreamModelId", "setContextWindow", "setInputPrice", "setOutputPrice",
        "setCacheReadPrice", "setCacheWritePrice",
    ):
        assert f'{setter}("")' in clear_draft
    assert "setKeyRevealed(false)" in clear_draft
    assert source.count("clearConnectionInputs()") == 2
    assert "if (publishing) return" in source
    assert "if (!publishing) onClose()" in source
    assert 'type="submit" disabled={publishing' in source
    assert "publicationConnectionEndpoint(selectedRuntime)" in source
    assert "publicationConnectionAuth(selectedRuntime, selectedProvider)" in source
    assert "dataSource.retryGatewayPublication(" in source
    assert "dataSource.resumeGatewayPublicationAuthorization(publicationId)" in source

    navigation = page.split("onManageConnections={() => {", 1)[1].split(
        "}} onClose=", 1
    )[0]
    assert "closePublication()" in navigation
    assert "setRuntimeDetailId(null)" in navigation
    assert 'changeTab("connections")' in navigation


def test_publication_connection_summary_wraps_long_endpoints_on_narrow_screens() -> None:
    styles = read_frontend_styles()
    summary_rule = styles.split(".publication-connection-summary dd {", 1)[1].split("}", 1)[0]

    assert "min-width: 0" in summary_rule
    assert "overflow-wrap: anywhere" in summary_rule
    assert ".publication-connection-endpoint dd { white-space: normal;" in styles
    assert ".publication-connection-dialog .registry-editor-footer { flex-wrap: wrap; }" in styles
    assert ".publication-connection-summary { grid-template-columns: minmax(0, 1fr); }" in styles


def test_openai_onboarding_keeps_vendor_identity_and_key_entry_separate() -> None:
    connection = (
        FRONTEND_SOURCE / "components/model-management/connection-dialog.tsx"
    ).read_text(encoding="utf-8")
    publication = (
        FRONTEND_SOURCE / "components/model-management/model-publication-dialog.tsx"
    ).read_text(encoding="utf-8")
    editor = (
        FRONTEND_SOURCE / "components/model-management/model-edit-dialog.tsx"
    ).read_text(encoding="utf-8")
    api = (FRONTEND_SOURCE / "data-sources/apim/api.ts").read_text(encoding="utf-8")

    assert 'provider.provider_kind === "openai_compatible"' in connection
    assert "<ModelVendorSelect" in connection
    assert "isOpenAICompatibleBaseUrl(openaiBaseUrl)" in connection
    assert "openai_base_url: openaiCompatible" in connection
    assert "model_vendor: openaiCompatible" in connection
    assert 'type="password"' not in connection
    assert "api_key:" not in connection
    assert "publicModelKeyFromProviderId(upstreamModelId)" in publication
    assert "displayNameFromProviderId(upstreamModelId)" in publication
    assert "upstream_model_id: foundry ? undefined : upstreamModelId.trim()" in publication
    assert "<ModelVendorLogo" in editor
    assert "<ModelVendorSelect" not in editor
    assert "requireOpenAICompatibleApi(" in api
    assert "isUnsupportedOpenAICompatibleDetails(" in api


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
    assert 'aria-label="按健康状态筛选"' in page
    assert 'className="connection-health-action"' in page
    assert 'className="connection-default-action"' in page
    assert "onCheckRuntime" in page
    assert "onDefaultRuntime" in page
    assert "useQueries" in page
    assert "function runtimeWithTelemetryHealth" in page
    assert "runtimeWithTelemetryHealth(runtime, runtimeRequests)" in page
    assert "runtimeHealthQueries[index]?.data ?? []" in page
    assert "loadingRuntimeHealthIds.has(runtime.id)" in page
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
    health_counts = page.split("const connectionHealthCounts = {", 1)[1].split(
        "}", 1
    )[0]
    assert "inventoryConnections.filter" in health_counts
    assert 'value === "active" || connectionHealthCounts[value] > 0' in page
    assert ">连接</a><ChevronRight" in page


def test_gateway_creation_action_stays_hidden() -> None:
    page = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(
        encoding="utf-8"
    )

    assert 'activeTab !== "gateways" && <div className="smh-header-actions"' in page


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
    assert 'className="gateway-split apim-native-routes-layout"' in native_page
    assert 'className="gateway-list-pane apim-native-route-list"' in native_page
    assert '!isMobile && <aside className="gateway-list-pane apim-native-route-list"' in native_page
    assert '<Dialog open={routeDrawerOpen} onOpenChange={onRouteDrawerOpenChange}>' in native_page
    assert 'className="apim-native-route-drawer"' in native_page
    assert (
        'className="gateway-list-pane apim-native-route-list '
        'apim-native-route-drawer-list"' in native_page
    )
    assert 'if (closeOnSelect) onRouteDrawerOpenChange(false)' in native_page
    assert 'className="gateway-list-controls apim-native-route-list-controls"' in native_page
    assert 'className="gateway-editor-pane apim-native-route-detail"' in native_page
    assert 'className="smh-search"' in native_page
    assert (
        ".apim-native-route-list.gateway-list-pane .apim-native-route-list-controls { "
        "min-height: 54px; flex-basis: 54px; padding: 12px 16px 6px; border-bottom: 0; }"
    ) in styles
    assert ".apim-native-route-list .gateway-list-row::before { inset-inline: 16px; }" in styles
    assert ".apim-native-route-list .gateway-list-scroll { padding-top: 6px; }" in styles
    assert '.apim-native-routes-workspace > .smh-page-header { display: none; }' in styles
    assert (
        "body:has(.apim-native-route-drawer) .search-dialog-overlay { "
        "inset: 48px 0 0; }"
    ) in styles
    assert '.apim-native-route-drawer-list.gateway-list-pane' in styles
    assert '.apim-native-route-drawer-list .gateway-list-row { padding-inline: 28px; }' in styles
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
    assert "gateway-list-section-label" not in native_page
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
