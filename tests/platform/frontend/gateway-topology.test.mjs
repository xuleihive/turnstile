import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import {
  apimGatewayEntries, gatewayEvidence, gatewayPublicationChoices, resolveGatewaySelection,
} from "../../../frontend/src/lib/gateway-topology.ts"
import * as gatewayTopology from "../../../frontend/src/lib/gateway-topology.ts"
import * as gatewayForms from "../../../frontend/src/lib/gateway-forms.ts"
import { loadTypeScriptModule } from "./load-typescript-module.mjs"
const modelFilters = await loadTypeScriptModule(new URL("../../../frontend/src/lib/model-inventory-filters.ts", import.meta.url))
import * as runtimeHealth from "../../../frontend/src/lib/runtime-health.ts"
import { formatTimeAgo } from "../../../frontend/src/lib/time.ts"
import * as publicationConnections from "../../../frontend/src/components/model-management/model-publication-connections.ts"
import * as compatible from "../../../frontend/src/components/model-management/openai-compatible.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const { QueryClient } = frontendRequire("@tanstack/react-query")
async function compile(path, expose = []) {
  const entry = fileURLToPath(new URL(path, import.meta.url))
  const bundle = await rolldown({ input: entry, external: path => path !== entry, transform: { jsx: { runtime: "automatic" }, define: { "import.meta.env.VITE_API_BASE_URL": '""' } }, treeshake: false,
    plugins: expose.length ? [{ name: "test-component-exports", load(path) { if (path === entry) return `${readFileSync(entry, "utf8")}\nexport { ${expose.join(", ")} }` } }] : [],
  })
  try { return (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
}
const apiCode = await compile("../../../frontend/src/data-sources/apim/api.ts")
const requestCode = await compile("../../../frontend/src/api/client.ts")
const queryCode = await compile("../../../frontend/src/data-sources/apim/queries.ts")
const onboardingCode = await compile("../../../frontend/src/components/model-management/apim-onboarding-dialog.tsx")
const creationCode = await compile("../../../frontend/src/components/model-management/self-hosted-gateway-dialog.tsx")
const workspaceCode = await compile("../../../frontend/src/components/model-management/apim-gateway-workspace.tsx", ["ApimTable", "GatewayTable", "GatewayChildRow", "InfrastructureActions", "ApimEditor", "GatewayEditor", "GatewayAssociationEditor", "InfrastructureNameEditor", "RemovalDialog", "InfrastructureTableLoading"])
const pageCode = await compile("../../../frontend/src/pages/model-management-page.tsx", ["ModelWorkspace", "modelManagementTabFromUrl", "ConnectionGatewayCell", "ModelTargetGatewayCell", "ConnectionRow", "RuntimeHealth"])
const releaseCode = await compile("../../../frontend/src/pages/gateway-releases-page.tsx", ["PublicationFailureNotice", "ChangeColumn", "GatewayReleaseGroups", "BudgetVerificationDialog"])
const diagnosticCode = await compile("../../../frontend/src/components/model-management/runtime-diagnostic-dialog.tsx")
const connectionCode = await compile("../../../frontend/src/components/model-management/connection-dialog.tsx")
const traceCode = await compile("../../../frontend/src/data-sources/apim/pages/dashboard-page.tsx")
const mobileCode = await compile("../../../frontend/src/hooks/use-mobile.ts")

test("responsive controls use the model-table breakpoint and preserve the default breakpoint", () => {
  for (const breakpoint of [undefined, 761]) {
    const limit = breakpoint ?? 768
    let state, effect, query
    const listeners = new Set()
    const viewport = { innerWidth: limit - 1, matchMedia(value) {
      query = value
      return { addEventListener(_event, listener) { listeners.add(listener) }, removeEventListener(_event, listener) { listeners.delete(listener) } }
    } }
    const exports = {}
    const hooks = { useState: () => [state, value => { state = value }], useEffect(callback) { effect = callback } }
    runInNewContext(mobileCode, { exports, window: viewport, require: () => hooks })
    exports.useIsMobile(breakpoint)
    const cleanup = effect()
    assert.equal(query, `(max-width: ${limit - 1}px)`)
    assert.equal(exports.useIsMobile(breakpoint), true)
    viewport.innerWidth = limit
    for (const listener of listeners) listener()
    assert.equal(exports.useIsMobile(breakpoint), false)
    viewport.innerWidth = limit - 1
    for (const listener of listeners) listener()
    assert.equal(exports.useIsMobile(breakpoint), true)
    cleanup()
    assert.equal(listeners.size, 0)
  }
  const page = readFileSync(new URL("../../../frontend/src/pages/model-management-page.tsx", import.meta.url), "utf8")
  assert.ok(page.includes("const compactLayout = useIsMobile(761)"))
})
const buttonCode = await compile("../../../frontend/src/components/ui/button.tsx")

test("shared button exposes its variant while preserving control props", () => {
  const exports = {}
  const imports = {
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@base-ui/react/button": { Button: "ButtonPrimitive" },
    "class-variance-authority": frontendRequire("class-variance-authority"),
    "../../lib/utils": { cn: (...values) => values.filter(Boolean).join(" ") },
  }
  runInNewContext(buttonCode, { exports, require: name => imports[name] })
  for (const variant of [undefined, "default", "outline", "secondary", "ghost", "destructive", "link"]) {
    const button = exports.Button({ variant, disabled: true, title: "Unit action", type: "button" })
    assert.equal(button.type, "ButtonPrimitive")
    assert.equal(button.props["data-variant"], variant ?? "default")
    assert.equal(button.props.disabled, true)
    assert.equal(button.props.title, "Unit action")
    assert.equal(button.props.type, "button")
  }
})

test("request errors unwrap string details but retain structured validation evidence", async () => {
  for (const [body, expected] of [
    ['{"detail":"Identity is not ready"}', "Identity is not ready"],
    ['{"detail":[{"loc":["body","price_source"],"msg":"Invalid"}]}', '{"detail":[{"loc":["body","price_source"],"msg":"Invalid"}]}'],
    ["Upstream unavailable", "Upstream unavailable"], ["", "/unit"],
  ]) {
    const exports = {}
    runInNewContext(requestCode, { exports, fetch: async () => new Response(body, { status: 409 }) })
    await assert.rejects(exports.request("/unit"), error => error.status === 409 && error.message === `API 409: ${expected}`)
  }
})

test("shared form styles cover standalone inputs and variant-aware dialog buttons", () => {
  const stylesheet = frontendRequire("postcss").parse(readFileSync(new URL("../../../frontend/src/styles/finops.css", import.meta.url), "utf8"))
  const rules = new Map()
  stylesheet.walkRules(rule => { for (const selector of rule.selectors) rules.set(selector, Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))) })
  assert.equal(rules.get('[data-slot="input"]').height, "32px")
  assert.equal(rules.get('[data-slot="input"][aria-invalid="true"]')["border-color"], "var(--destructive)")
  assert.equal(rules.get('.registry-editor-footer > [data-slot="button"][data-variant="outline"]').background, "var(--background)")
  assert.equal(rules.get('.registry-editor-footer > [data-slot="button"][data-variant="default"]').background, "var(--primary)")
  assert.equal(rules.get(".registry-editor-footer > button:disabled").cursor, "not-allowed")
})

test("publication summaries and budget history retain bounded source layouts", () => {
  const rules = new Map()
  for (const name of ["finops", "model-platform"]) {
    const stylesheet = frontendRequire("postcss").parse(readFileSync(new URL(`../../../frontend/src/styles/${name}.css`, import.meta.url), "utf8"))
    stylesheet.walkRules(rule => {
      const media = rule.parent.type === "atrule" ? `${rule.parent.params}|` : ""
      for (const selector of rule.selectors) rules.set(media + selector, Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value])))
    })
  }
  assert.equal(rules.get(".budget-history-list")["grid-template-columns"], "auto minmax(0, 1fr) minmax(100px, 1.5fr) auto")
  assert.equal(rules.get(".budget-history-list p")["min-width"], "0")
  assert.equal(rules.get(".budget-history-list p")["text-overflow"], "ellipsis")
  assert.equal(rules.get(".publication-connection-summary > div")["align-content"], "start")
  assert.equal(rules.get(".publication-connection-summary dd").color, "var(--foreground)")
  assert.equal(rules.get(".publication-connection-action:focus-visible").color, "var(--foreground)")
  assert.equal(rules.get("(max-width: 640px)|.publication-connection-summary")["grid-template-columns"], "1fr")
})

test("publication status labels wrap without shrinking their icons", () => {
  const stylesheet = frontendRequire("postcss").parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const rules = new Map()
  stylesheet.walkRules(rule => rules.set(rule.selector, Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))))
  assert.equal(rules.get(".model-publication-status")["align-items"], "flex-start")
  assert.equal(rules.get(".model-publication-status span")["white-space"], "normal")
  assert.equal(rules.get(".model-publication-status span")["overflow-wrap"], "anywhere")
  assert.equal(rules.get(".model-publication-status > svg")["flex-shrink"], "0")
  assert.equal(rules.get(".publication-model-row")["padding-block"], "8px")
})

test("budget diagnostic clients send only explicit selections with encoded target identity", async () => {
  const calls = []
  const api = apiHarness(async (...args) => { calls.push(args); return {} })
  await api.checkBudgetDelegation("unit/target")
  await api.verifyBudgetExecution("unit/target", "application", "model")
  assert.equal(calls[0][0], "/api/v1/model-management/gateways/unit%2Ftarget/budget-readiness")
  assert.equal(Object.keys(calls[0][1]).length, 0)
  assert.equal(calls[1][0], "/api/v1/model-management/gateways/unit%2Ftarget/budget-execution-verification")
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1][1])), { application_id: "application", model_id: "model" })
})

test("availability clients use no-store reads and exact versioned model and operation commands", async () => {
  const reads = []
  const writes = []
  const api = apiHarness(async (...args) => { reads.push(args); return { items: [] } },
    async (path, payload, method = "POST") => { writes.push({ path, payload, method }); return {} })
  await api.modelAvailability()
  const intent = { enabled: false, expected_revision: 3, idempotency_key: "unit-intent" }
  await api.changeModelAvailability("unit/model", intent)
  await api.retryModelAvailability("unit/operation", 4)
  assert.equal(reads[0][0], "/api/v1/model-management/model-availability")
  assert.equal(reads[0][1].cache, "no-store")
  assert.deepEqual(JSON.parse(JSON.stringify(writes)), [
    { path: "/api/v1/model-management/models/unit%2Fmodel/availability", payload: intent, method: "PUT" },
    { path: "/api/v1/model-management/model-availability/operations/unit%2Foperation/retry", payload: { expected_revision: 4 }, method: "POST" },
  ])
})

test("Trace errors wrap long identities within their grid column", () => {
  const stylesheet = frontendRequire("postcss").parse(readFileSync(new URL("../../../frontend/src/styles/finops.css", import.meta.url), "utf8"))
  const rules = new Map()
  stylesheet.walkRules(rule => rules.set(rule.selector, Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))))
  assert.equal(rules.get(".trace-error > div")["min-width"], "0")
  assert.equal(rules.get(".trace-error span")["min-width"], "0")
  assert.equal(rules.get(".trace-error span")["overflow-wrap"], "anywhere")
  assert.equal(rules.get(".trace-error span")["white-space"], "normal")
})

test("Trace status colors require explicit success or failure evidence", () => {
  const css = readFileSync(new URL("../../../frontend/src/styles/finops.css", import.meta.url), "utf8")
  for (const selector of [".trace-row-status", ".trace-detail-hero > span"]) {
    const neutral = css.split(`${selector} {`, 2)[1].split("}", 1)[0]
    assert.ok(neutral.includes("background: var(--muted)"))
    assert.ok(neutral.includes("color: var(--muted-foreground)"))
    const success = css.split(`${selector}.success {`, 2)[1].split("}", 1)[0]
    assert.ok(success.includes("color: var(--success)"))
    const failure = css.split(`${selector}.failed {`, 2)[1].split("}", 1)[0]
    assert.ok(failure.includes("color: var(--destructive)"))
  }
})

test("Trace rows distinguish unknown usage from measured zero and use stable platform identity", () => {
  for (const [status, available, label] of [[504, false, "不可用"], [null, false, "不可用"], [200, true, "0 tokens"]]) {
    const item = { request_id: "request", correlation_id: null, trace_id: "platform:request", timestamp: "2020-01-01T00:00:00Z",
      model_name: "Unit model", status_code: status, platform_outcome: status === 504 ? "failed" : status === 200 ? "succeeded" : "dispatched",
      usage_available: available, total_tokens: 0, estimated_cost: 0 }
    const row = componentHarness("TraceRow", { item, active: true, onClick() {}, costAvailable: available }, { code: traceCode })
    assert.ok(row.find(node => node.type === "small" && node.props.children === label))
    assert.ok(row.find(node => node.type === "span" && node.props.children === (status ?? "未知")))
    if (status == null) assert.ok(!row.find(node => node.props?.className === "trace-row-status success"))
  }
})

test("diagnostic dialog requires explicit eligible selection and allows only one dispatch", () => {
  const submissions = []
  const plan = { options: [{ model_id: "model", gateway_deployment_id: "edge", model_name: "Model", model_key: "alias", gateway_name: "Edge", native_gateway_id: "native" }], access: { status: "ready", missing_actions: [], required_actions: [] } }
  const props = { runtime: { id: "runtime", name: "Connection", enabled: true }, supported: true, busy: false, error: null, result: null, onCheck: value => submissions.push(value), onClose() {} }
  const view = componentHarness("RuntimeDiagnosticDialog", props, { code: diagnosticCode, queryData: { "control-plane": plan } })
  const runButton = () => view.find(node => node.type === "Button" && Array.isArray(node.props.children) && node.props.children[1] === "运行检查")
  assert.equal(runButton().props.disabled, true)
  view.find(node => node.type === "Select").props.onValueChange("model:edge")
  assert.equal(runButton().props.disabled, false)
  const click = runButton().props.onClick
  click()
  click()
  assert.equal(submissions.length, 1)
  assert.equal(submissions[0].model_id, "model")
  assert.equal(submissions[0].gateway_deployment_id, "edge")
  assert.equal(runButton().props.disabled, true)
  for (const options of [{ role: "member" }, { queryData: { "control-plane": { ...plan, access: { ...plan.access, status: "unverified" } } } }]) {
    const blocked = componentHarness("RuntimeDiagnosticDialog", props, { code: diagnosticCode, queryData: { "control-plane": plan }, ...options })
    blocked.find(node => node.type === "Select").props.onValueChange("model:edge")
    assert.equal(blocked.find(node => node.type === "Button" && Array.isArray(node.props.children) && node.props.children[1] === "运行检查").props.disabled, true)
  }
})

test("connection diagnosis distinguishes historical success, policy blocks and changed configuration", () => {
  const checkedAt = new Date(Date.now() - 30 * 86400000).toISOString()
  const runtime = { id: "unit-runtime", enabled: true, health_status: "available", last_checked_at: checkedAt }
  assert.equal(runtimeHealth.runtimeHealthState(runtime), "inconclusive")
  const diagnostic = { runtime_id: runtime.id, status: "available", outcome: "passed", checked_at: checkedAt,
    valid_until: checkedAt, configuration_status: "current" }
  assert.equal(runtimeHealth.runtimeHealthState({ ...runtime, diagnostic }), "online")
  assert.equal(runtimeHealth.runtimeHealthState({ ...runtime, diagnostic: { ...diagnostic, configuration_status: "changed" } }), "changed")
  assert.equal(runtimeHealth.runtimeDiagnostic({ ...runtime, health_message: runtimeHealth.runtimeDiagnosticPrefix + JSON.stringify(diagnostic) }).configuration_status, "unverified")
  const blocked = runtimeHealth.runtimeWithTelemetryHealth({ ...runtime, last_checked_at: null }, [{
    request_source: "runtime-health-check", timestamp: checkedAt, status_code: 403,
    error_message: "model_not_assigned", request_id: "unit-request", correlation_id: "unit-correlation", latency_ms: 1,
  }])
  assert.equal(runtimeHealth.runtimeHealthState(blocked), "blocked")
  assert.equal(runtimeHealth.runtimeHealthState({ ...runtime, enabled: false, diagnostic }), "disabled")
})

test("connection creation waits for registered APIM inventory without replacing explicit choices", () => {
  const writes = []
  const props = { registry: { apim_target_topology_available: true, providers: [], gateways: [{ id: "first", name: "Unit APIM", enabled: true, implementation: "apim" }] }, topology: null, topologyLoading: true, busy: false, error: null, onClose() {}, onCreate: value => writes.push(value), onUpdate() {} }
  const view = componentHarness("ConnectionDialog", props, { code: connectionCode })
  assert.ok(view.find(node => node.props?.role === "status"))
  assert.equal(view.find(node => node.type === "Select" && Array.isArray(node.props.items)).props.disabled, true)
  props.topology = { items: [{ target: { gateway_profile_id: "first" } }] }
  props.topologyLoading = false
  const selector = view.find(node => node.type === "Select" && Array.isArray(node.props.items))
  assert.equal(selector.props.value, "first")
  assert.equal(selector.props.disabled, false)
  selector.props.onValueChange("removed")
  assert.equal(view.find(node => node.type === "Select" && Array.isArray(node.props.items)).props.value, "removed")
  const submit = view.find(node => node.type === "Button" && node.props.children?.at?.(-1) === "添加连接")
  assert.equal(submit.props.disabled, true)
  submit.props.onClick()
  assert.equal(writes.length, 0)
})

test("compatible connection groups provider identity with its HTTPS endpoint", () => {
  const props = { registry: { providers: [], gateways: [{ id: "target", name: "Unit APIM", enabled: true, implementation: "apim" }] }, busy: false, error: null, onClose() {}, onCreate() {}, onUpdate() {} }
  const view = componentHarness("ConnectionDialog", props, { code: connectionCode })
  view.find(node => node.type === "Select" && !Array.isArray(node.props.items)).props.onValueChange("template:openai_compatible")
  assert.ok(view.find(node => node.props?.className === "openai-compatible-identity-grid"))
  assert.equal(view.find(node => node.props?.htmlFor === "openai-base-url").props.children, "API Base URL")
  assert.ok(view.find(node => node.type === "FieldHelp" && node.props.children.startsWith("选择提供此 API 的服务商")))
  assert.equal(view.find(node => node.type === "Input" && node.props.id === "openai-base-url").props.type, "url")
})

test("connection health renders valid historical timestamps without invalid dates", () => {
  for (const checkedAt of ["2026-09-26T00:00:00Z", "invalid", undefined]) {
    const view = componentHarness("RuntimeHealth", { state: "blocked", checkedAt }, { code: pageCode })
    const timestamp = view.find(node => node.type === "time")
    assert.equal(Boolean(timestamp), checkedAt === "2026-09-26T00:00:00Z")
    if (timestamp) assert.equal(timestamp.props.dateTime, checkedAt)
    assert.ok(view.find(node => node.type === "TriangleAlert"))
  }
})

test("connection row opens diagnostics without an independent default-connection action", () => {
  let checks = 0
  const runtime = { id: "runtime", name: "Connection", runtime_kind: "foundry", enabled: true, is_default: false, config: {}, health_message: null }
  const row = componentHarness("ConnectionRow", { runtime, models: [{ enabled: true }], topology: null,
    busy: false, checking: false, onEdit() {}, onToggle() {}, onDelete() {}, onCheck() { checks++ } }, { code: pageCode })
  assert.equal(row.find(node => node.props?.className === "connection-default-action"), undefined)
  row.find(node => node.props?.title === "连接诊断").props.onClick()
  assert.equal(checks, 1)
})

test("budget verification dialog binds validated candidates and prevents duplicate dispatch", async () => {
  const requests = []
  const registry = { gateways: [{ id: "target", name: "Unit APIM", enabled: true, implementation: "apim" }], runtimes: [{ id: "runtime", enabled: true, gateway_profile_id: "target" }], models: [{ id: "model", display_name: "Unit model", enabled: true, capabilities: ["chat"], runtime_id: "runtime" }] }
  const applications = { items: [{ id: "application", display_name: "Unit app", gateway_profile_id: "target", status: "active", system_managed: false, model_policy_configured: true, allowed_model_ids: ["model"] }] }
  const api = {
    checkBudgetDelegation: async id => ({ gateway_profile_id: id, ready: true, protocol_mode: "paused", checks: [], verification: { application_id: "application", model_ids: ["model"] } }),
    verifyBudgetExecution: async (...selection) => { requests.push(selection); return { actual_status: 200, settled: false, request_id: "unit-request", scopes: [] } },
  }
  const props = { gatewayId: "target", onClose() {} }
  const view = componentHarness("BudgetVerificationDialog", props, { code: releaseCode, api, queryData: { registry, applications } })
  const button = label => view.find(node => node.type === "Button" && Array.isArray(node.props.children) && node.props.children.at(-1) === label)
  assert.equal(button("联合账本验证").props.disabled, true)
  button("验证预算委托").props.onClick()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(button("联合账本验证").props.disabled, false)
  button("联合账本验证").props.onClick()
  assert.equal(button("发送验证请求").props.disabled, true)
  view.find(node => node.type === "Select" && node.props.items?.[0]?.value === "application").props.onValueChange("application")
  view.find(node => node.type === "Select" && node.props.items?.[0]?.value === "model").props.onValueChange("model")
  assert.equal(button("发送验证请求").props.disabled, false)
  const send = button("发送验证请求").props.onClick
  send(); send()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(requests, [["target", "application", "model"]])
  assert.equal(button("发送验证请求").props.disabled, true)
  assert.ok(view.find(node => node.props?.role === "status" && node.props.children?.includes("尚未确认结算")))
  const member = componentHarness("BudgetVerificationDialog", props, { code: releaseCode, api, role: "member", queryData: { registry, applications } })
  assert.equal(member.find(node => node.type === "Button" && node.props.children?.at?.(-1) === "验证预算委托").props.disabled, true)
})

test("release groups separate same-name APIM targets and retain selection when collapsed", () => {
  const items = [
    { id: "release-a1", gateway_profile_id: "target-a", gateway_name: "Unit APIM" },
    { id: "release-b1", gateway_profile_id: "target-b", gateway_name: "Unit APIM" },
    { id: "release-a2", gateway_profile_id: "target-a", gateway_name: "Unit APIM" },
  ]
  const toggled = [], selected = []
  const view = componentHarness("GatewayReleaseGroups", { items, selectedId: "release-b1", collapsedGatewayIds: new Set(["target-a"]), location: "rail", onToggle: id => toggled.push(id), onSelect: id => selected.push(id) }, { code: releaseCode })
  assert.equal(view.find(node => node.props?.id === "gateway-release-group-rail-target-a").props.hidden, true)
  assert.equal(view.find(node => node.props?.id === "gateway-release-group-rail-target-b").props.hidden, false)
  const target = view.find(node => node.props?.["aria-controls"] === "gateway-release-group-rail-target-a")
  target.props.onClick()
  assert.deepEqual(toggled, ["target-a"])
  const row = view.find(node => node.props?.release?.id === "release-b1")
  assert.equal(row.props.selected, true)
  row.props.onSelect()
  assert.deepEqual(selected, ["release-b1"])
})

test("release failure notice uses safe categories and marks unapplied changes as planned", () => {
  const notice = componentHarness("PublicationFailureNotice", {
    publicationId: "unit-publication", failure: {
      code: "publisher_permissions_missing", message: "untrusted raw error",
      stage: "validating", http_status: 403, request_id: "azure-request-id",
      missing_actions: ["Microsoft.ApiManagement/service/backends/write"],
    },
  }, { code: releaseCode })
  assert.ok(notice.find(node => node.type === "p" && node.props.children.includes("Publisher 缺少")))
  assert.ok(notice.find(node => node.type === "code" && node.props.children === "azure-request-id"))
  assert.ok(notice.find(node => node.type === "details"))
  assert.ok(!notice.find(node => node.props?.children === "untrusted raw error"))
  for (const planned of [true, false]) {
    const changes = componentHarness("ChangeColumn", { title: "Models", added: ["model"], removed: [], changed: [], planned }, { code: releaseCode })
    assert.ok(changes.find(node => node.type === "b" && node.props.children === (planned ? "拟新增" : "已新增")))
  }
})

test("model targets use recorded memberships and publication snapshots without default guesses", () => {
  const target = topology({ apim_target_id: "first" })
  const edge = topology({ id: "edge", apim_target_id: "first", native_gateway_id: "edge-native", kind: "self_hosted", display_name: "Docker edge" }).deployments[0]
  target.deployments[0].published_models = [{ id: "another-model" }]
  edge.published_models = [{ id: "model-one" }]
  target.deployments.push(edge)
  const inventory = { items: [target] }
  const model = { id: "model-one" }
  const runtime = { gateway_profile_id: "first" }
  assert.deepEqual(gatewayTopology.modelTargetDetails(model, runtime, profiles, inventory).gateways, [
    { id: "edge", name: "Docker edge", nativeId: "edge-native", kind: "self_hosted" },
  ])
  assert.deepEqual(gatewayTopology.modelTargetDetails(model, runtime, profiles, null).gateways, [])
  edge.published_models_available = false
  assert.deepEqual(gatewayTopology.modelTargetDetails(model, runtime, profiles, inventory).gateways, [])
  edge.published_models_available = true
  edge.deployment.retired_at = "2026-09-26T00:00:00Z"
  assert.deepEqual(gatewayTopology.modelTargetDetails(model, runtime, profiles, inventory).gateways, [])
  const publication = { gateway_profile_id: "first", target_deployments: [{ deployment_id: "edge", native_gateway_id: "edge-native", kind: "self_hosted" }] }
  assert.equal(gatewayTopology.publicationTargetDetails(publication, profiles, inventory).gateways[0].name, "Docker edge")
  edge.deployment.native_gateway_id = "foreign-native"
  assert.equal(gatewayTopology.publicationTargetDetails(publication, profiles, inventory).gateways[0].name, "edge-native")
  assert.deepEqual(gatewayTopology.publicationTargetDetails({ gateway_profile_id: "first" }, profiles, inventory).gateways, [])
})

test("invocation options require model membership and never replace a missing explicit model", () => {
  const model = { id: "model-one", enabled: true, is_default: true, capabilities: ["chat"] }
  assert.equal(gatewayTopology.resolveInvocationModel([model], null, false), model)
  assert.equal(gatewayTopology.resolveInvocationModel([model], "missing", false), undefined)
  assert.equal(gatewayTopology.resolveInvocationModel([model], null, true), undefined)
  const target = topology({ apim_target_id: "first" })
  const entry = target.deployments[0]
  entry.published_models = [{ id: model.id }]
  assert.equal(gatewayTopology.gatewayInvocationChoices(profiles, { items: [target] }, model.id).length, 1)
  entry.published_models_available = false
  assert.equal(gatewayTopology.gatewayInvocationChoices(profiles, { items: [target] }, model.id).length, 0)
  entry.published_models_available = true
  entry.deployment.reachability_state = "unreachable"
  assert.equal(gatewayTopology.gatewayInvocationChoices(profiles, { items: [target] }, model.id)[0].selectable, false)
  entry.deployment.retired_at = "2026-01-01T00:00:00Z"
  assert.equal(gatewayTopology.gatewayInvocationChoices(profiles, { items: [target] }, model.id).length, 0)
})

test("Gateway inventory uses Chinese UI labels while preserving resource identities", () => {
  const target = topology({ kind: "self_hosted", native_gateway_id: "unit-edge", display_name: "Customer Gateway", hosting_mode: "docker" })
  const entry = { ...target.deployments[0], target, configuration_endpoint: "https://unit-service.configuration.azure-api.net" }
  const workspace = componentHarness("ApimGatewayWorkspace", {
    mode: "gateways", registry: { models: [], runtimes: [], gateways: profiles },
    topology: { items: [target] }, loading: false, error: null,
    onTabChange() {}, onToggleSidebar() {},
  })
  assert.equal(workspace.find(node => node.type === "h1").props.children, "网关")
  assert.equal(workspace.find(node => node.props?.["data-tab"] === "gateways").props.children[0], "网关")
  const table = componentHarness("GatewayTable", {
    entries: [entry], filtered: false, busy: false, lifecycleAvailable: true, validatingId: null,
    onEdit() {}, onValidate() {}, onChange() {}, onRemove() {},
  })
  const resourceTable = table.find(node => node.type?.name === "ResourceTable")
  assert.deepEqual(Array.from(resourceTable.props.headers), ["网关", "所属 APIM / 模型", "端点", "状态", ""])
  assert.equal(resourceTable.props.label, "自托管网关列表")
  assert.ok(table.find(node => node.type === "b" && node.props.children === "Customer Gateway" && node.props["data-no-localize"] === true))
  assert.ok(table.find(node => node.type === "span" && node.props.children === "配置地址"))
  assert.ok(table.find(node => node.type === "span" && node.props.children === entry.configuration_endpoint && node.props["data-no-localize"] === true))
})

for (const tab of ["models", "connections", "gateways", "apim"]) test(`Management tab labels stay fully localized in ${tab} view`, () => {
  const infrastructure = tab === "gateways" || tab === "apim"
  const props = {
    tab, mode: tab, registry: { models: [], runtimes: [], providers: [], gateways: profiles },
    topology: { items: [topology()], total: 1 }, topologyAvailable: true,
    loading: false, busy: false, notice: null, error: null, publications: [],
    loadingRuntimeHealthIds: new Set(), onTabChange() {}, onToggleSidebar() {},
  }
  const harness = componentHarness(infrastructure ? "ApimGatewayWorkspace" : "ModelWorkspace", props, { code: infrastructure ? workspaceCode : pageCode })
  const labels = harness.all().filter(node => node.props?.["data-tab"]).map(node => node.props.children[0])
  assert.deepEqual(labels, ["模型", "连接", "网关", "APIM 实例"])
})

for (const locale of ["zh-CN", "zh-TW", "en", "ja", "ko"]) test(`Gateway startup duration follows ${locale} without translating secrets`, () => {
  const harness = componentHarness("SelfHostedGatewayBootstrapDialog", {
    deploymentId: "unit-edge", name: "Customer Gateway", onClose() {}, onValidate() {},
  }, { code: creationCode, locale })
  const value = harness.find(node => node.type === "SelectValue").props.children
  assert.equal(value.props["data-no-localize"], true)
  assert.equal(value.props.children, new Intl.NumberFormat(locale, { style: "unit", unit: "hour", unitDisplay: "long" }).format(24))
  assert.ok(harness.find(node => node.props?.["aria-label"] === "令牌有效期"))
  assert.ok(harness.find(node => node.type === "DialogDescription" && node.props["data-no-localize"] && node.props.children === "Customer Gateway"))
})

test("new Gateway form generates no native identity and requires endpoint control confirmation", () => {
  const form = { display_name: "  New Docker  ", endpoint_url: "https://edge.test/unit/llm", location: " ", endpoint_control_confirmed: false }
  assert.equal(gatewayForms.gatewayCreateFormValid(form), false)
  form.endpoint_control_confirmed = true
  assert.deepEqual(gatewayForms.gatewayCreateFormPayload(form), {
    display_name: "New Docker", endpoint_url: form.endpoint_url, location: null,
    hosting_mode: "docker", release_gate: "required", endpoint_control_confirmed: true,
  })
  assert.equal(gatewayForms.gatewayCreateFormValid({ ...form, endpoint_url: "http://edge.test" }), false)
  assert.equal(gatewayForms.gatewayCreateFormValid({ ...form, display_name: " " }), false)
  assert.equal("native_gateway_id" in gatewayForms.gatewayCreateFormPayload({ ...form, native_gateway_id: "injected" }), false)
})

test("Gateway creation uses an idempotency header and bootstrap is never cacheable", async () => {
  const requests = []
  const api = apiHarness(async (...args) => { requests.push(args); return {} })
  const payload = { display_name: "New Docker", endpoint_url: "https://edge.test", endpoint_control_confirmed: true }
  await api.createSelfHostedGateway("target", payload, "request-uuid")
  assert.equal(requests[0][0], "/api/v1/model-management/apim-targets/target/self-hosted-gateway-provisions")
  assert.equal(requests[0][1].headers["Idempotency-Key"], "request-uuid")
  assert.deepEqual(JSON.parse(requests[0][1].body), payload)
  await api.selfHostedGatewayBootstrap("deployment", 24)
  assert.equal(requests[1][1].cache, "no-store")
  assert.deepEqual(JSON.parse(requests[1][1].body), { expires_in_hours: 24 })
})

function componentHarness(exportName, props, { operation = null, queryData = {}, mutationPending = false, code = workspaceCode, api = {}, role = "owner", locale = "en-US", globals = {} } = {}) {
  const slots = []
  let cursor = 0
  const effects = []
  const syncCalls = []
  const pendingMutations = []
  const mutationVariables = []
  const queries = []
  const invalidations = []
  const queryClient = { setQueryData() {}, getQueriesData() { return [] }, invalidateQueries(options) { invalidations.push(options.queryKey); return Promise.resolve() } }
  const imports = {
    react: {
      useId: () => "unit-gateway",
      useEffect: callback => effects.push(callback),
      useState(initial) {
        const index = cursor++
        if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
        return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value }]
      },
      useRef(initial) {
        const index = cursor++
        if (!(index in slots)) slots[index] = { current: initial }
        return slots[index]
      },
    },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@tanstack/react-query": {
      useQueryClient: () => queryClient,
      useQuery: options => { queries.push(options); return { data: Object.hasOwn(queryData, options.queryKey[0]) ? queryData[options.queryKey[0]] : operation, error: null, refetch() {} } },
      useMutation: options => {
        const index = cursor++
        if (!(index in slots)) slots[index] = { pending: false, error: null }
        const state = slots[index]
        const mutateAsync = async value => {
            state.pending = true
            state.variables = value
            mutationVariables.push(value)
            try {
              const result = await options.mutationFn(value, { client: queryClient })
              state.data = result
              await options.onSuccess?.(result, value)
              return result
            } catch (error) { state.error = error; throw error }
            finally { state.pending = false; await options.onSettled?.() }
        }
        return { isPending: mutationPending || state.pending, error: state.error, data: state.data, variables: state.variables,
          reset() { state.error = null; state.data = undefined }, mutateAsync,
          mutate: value => {
            syncCalls.push(options)
            const pending = mutateAsync(value)
            pendingMutations.push(pending)
            void pending.catch(() => {})
          },
        }
      },
    },
    "../../data-sources/apim/api": { dataSource: api },
    "../../data-sources/apim/queries": {
      finopsKeys: { registry: ["registry"], apimTargets: ["targets"], apimOnboarding: ["onboarding"], selfHostedGatewayProvisions: ["provisions"], gatewayReleaseOperation: id => ["operation", id] },
      finopsQueries: {
        gatewayReleaseOperation: id => ({ queryKey: ["operation", id] }),
        apimOnboarding: () => ({ queryKey: ["onboarding"] }),
        selfHostedGatewayProvisions: () => ({ queryKey: ["provisions"] }),
        selfHostedGatewayRemoval: id => ({ queryKey: ["removal", id] }),
      },
      storeGatewayDeployment: async () => {},
      storeGatewayDeploymentName: async () => {},
      storeApimTarget: async () => {},
    },
    "../../lib/gateway-topology": gatewayTopology,
    "./model-publication-connections": publicationConnections,
    "./openai-compatible": compatible,
    "../brand-logos": new Proxy({ providerBrandFromMetadata: () => "generic", gatewayBrandFromIdentity: () => "apim" }, { get: (target, name) => target[name] ?? name }),
    "../../lib/gateway-forms": gatewayForms,
    "../../lib/runtime-health": runtimeHealth,
    "../lib/runtime-health": runtimeHealth,
    "../lib/time": { formatTimeAgo },
    "../locales/index": { useLocale: () => ({ locale }) },
    "./dashboard-shared": { compact: new Intl.NumberFormat("en-US"), currency: new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }) },
    "../lib/model-inventory-filters": modelFilters,
    "../hooks/use-mobile": { useIsMobile: () => false },
    "../../providers/auth-provider": { useAuth: () => ({ user: { role } }) },
    "../providers/auth-provider": { useAuth: () => ({ user: { role } }) },
    "../data-sources/apim/api": { dataSource: api, ApiError },
    "../data-sources/apim/queries": { finopsQueries: {
      registry: () => ({ queryKey: ["registry"] }),
      gatewayApplications: () => ({ queryKey: ["applications"] }),
    } },
    "../../locales": { useLocale: () => ({ locale }) },
    "../../providers/timezone-provider": { useTimezone: () => ({ timezone: "UTC" }) },
    "../components/brand-logos": new Proxy({ providerBrandFromIdentity: () => "generic", gatewayBrandFromIdentity: () => "generic" }, { get: (target, name) => target[name] ?? name }),
  }
  const onboardingExports = {}
  runInNewContext(onboardingCode, { exports: onboardingExports,
    require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }),
  })
  imports["./apim-onboarding-dialog"] = onboardingExports
  const creationExports = {}
  runInNewContext(creationCode, { exports: creationExports, crypto: { randomUUID: () => "unit-request-id" }, AbortController, URL, Blob,
    require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }),
  })
  imports["./self-hosted-gateway-dialog"] = creationExports
  const exports = {}
  runInNewContext(code, { exports, Set, URL, Blob, AbortController, Error, crypto: { randomUUID: () => "unit-request-id" }, window: { location: { href: "http://localhost/?tab=apim" } }, ...globals,
    require: name => imports[name] ?? (name.endsWith("/gateway-topology") ? gatewayTopology : new Proxy({}, { get: (_target, key) => key })),
  })
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
    : value && typeof value === "object" ? [value, ...nodes(value.props?.children)] : []
  const render = () => { cursor = 0; queries.length = 0; return exports[exportName](props) }
  const settle = async () => {
    let settled = 0
    while (settled < pendingMutations.length) {
      const current = pendingMutations.slice(settled)
      settled += current.length
      await Promise.allSettled(current)
    }
  }
  return { props, syncCalls, effects, mutationVariables, queries, invalidations, nodes, render, settle, all: () => nodes(render()), find: predicate => nodes(render()).find(predicate) }
}

class ApiError extends Error {
  constructor(status) { super(`HTTP ${status}`); this.status = status }
}
function apiHarness(request, writeJson = request) {
  const exports = {}
  runInNewContext(apiCode, { exports, require: name => name.includes("api/client")
    ? { request, writeJson, ApiError } : {} })
  return exports.dataSource
}
function queryHarness() {
  const exports = {}
  const imports = {
    "@tanstack/react-query": { queryOptions: options => options },
    "../../api/cache-policies": { cachePolicies: {} },
    "./api": { dataSource: {} },
  }
  runInNewContext(queryCode, { exports, require: name => imports[name] ?? {} })
  return exports
}

const profiles = [{ id: "first", name: "First", implementation: "apim", enabled: true, is_default: true, base_url: "https://first.example.test" }]
function topology(changes = {}) {
  return {
    target: { gateway_profile_id: "first", api_id: "unit-api", service_name: "unit-service", drift_state: "in_sync", capabilities: { self_hosted_gateway: "supported" } },
    deployments: [{ deployment: {
      id: "managed", native_gateway_id: "managed", kind: "managed", display_name: "Managed",
      retired_at: null, declared_state: "present", reachability_state: "reachable",
      endpoint_url: "https://first.example.test", last_probe_at: "2026-09-23T00:00:00Z",
      release_gate: "required", ...changes,
    }, inventory: { associated_api_ids: ["UNIT-API"] }, published_models: [] }],
  }
}

test("private design has four ordered tabs and a separate self-hosted inventory", () => {
  assert.deepEqual(gatewayTopology.MODEL_MANAGEMENT_TABS, ["models", "connections", "gateways", "apim"])
  const target = topology()
  target.deployments.push(topology({ id: "edge", native_gateway_id: "edge", kind: "self_hosted", endpoint_url: null, declared_state: "unknown" }).deployments[0])
  target.deployments.push(topology({ id: "retired", kind: "self_hosted", retired_at: "2026-09-23T00:00:00Z" }).deployments[0])
  assert.deepEqual(gatewayTopology.gatewayEntries({ items: [target] }).map(entry => entry.deployment.id), ["edge"])
  assert.deepEqual(gatewayTopology.gatewayEntries(null), [])
})

test("registry preserves strict backend topology and self-hosted capability flags", async () => {
  for (const enabled of [true, false, undefined, "true"]) {
    const registry = await apiHarness(async () => ({ providers: [], gateways: [], runtimes: [], models: [],
      apim_target_topology_available: enabled, self_hosted_gateway_enabled: enabled,
    })).registry()
    assert.equal(registry.apim_target_topology_available, enabled === true)
    assert.equal(registry.self_hosted_gateway_enabled, enabled === true)
  }
})

for (const mode of ["gateways", "apim"]) test(`source workspace renders four real tabs in ${mode} view`, () => {
  const changes = []
  const target = topology()
  target.deployments.push(topology({ id: "edge", kind: "self_hosted" }).deployments[0])
  const harness = componentHarness("ApimGatewayWorkspace", {
    mode, registry: { models: [], runtimes: [], gateways: profiles, self_hosted_gateway_enabled: true },
    topology: { items: [target], total: 1, resource_lifecycle_available: true },
    loading: false, error: null, onTabChange: value => changes.push(value), onToggleSidebar() {},
  }, { code: workspaceCode })
  const tabs = harness.all().filter(node => node.props?.["data-tab"])
  assert.deepEqual(tabs.map(node => node.props["data-tab"]), ["models", "connections", "gateways", "apim"])
  assert.equal(tabs.at(-1).props.children[0], "APIM 实例")
  tabs.at(-1).props.onClick()
  assert.deepEqual(changes, ["apim"])
  const table = harness.find(node => node.type?.name === (mode === "apim" ? "ApimTable" : "GatewayTable"))
  assert.ok(table)
  if (mode === "gateways") assert.deepEqual(table.props.entries.map(entry => entry.deployment.id), ["edge"])
  assert.equal(harness.find(node => node.type === "ManagementCredentialDialog"), undefined)
})

for (const mode of ["gateways", "apim"]) test(`Gateway creation stays inside its own list item in ${mode} view`, () => {
  const creation = {
    id: "creation", gateway_profile_id: "first", operation_kind: "self_hosted_gateway_provision",
    status: "queued", worker_available: true, checkpoint: {},
    semantic_preview: { native_gateway_id: "turnstile-unit", configuration: {
      display_name: "New Docker Gateway", endpoint_url: "https://edge.example.test/unit-api",
      hosting_mode: "docker",
    } },
  }
  const harness = componentHarness("ApimGatewayWorkspace", {
    mode, registry: { models: [], runtimes: [], gateways: profiles, self_hosted_gateway_enabled: true },
    topology: { items: [topology()], total: 1, resource_lifecycle_available: true, self_hosted_gateway_provisioning_available: true },
    loading: false, error: null, onTabChange() {}, onToggleSidebar() {},
  }, { queryData: { provisions: { items: [creation] } } })
  assert.equal(harness.find(node => node.type?.name === "SelfHostedGatewayProvisionProgress"), undefined)
  assert.equal(harness.queries.find(query => query.queryKey[0] === "provisions").enabled, mode === "gateways")
  if (mode === "gateways") {
    const table = harness.find(node => node.type?.name === "GatewayTable")
    assert.deepEqual(table.props.provisionItems.map(item => item.operation.id), ["creation"])
    const content = componentHarness("GatewayTable", table.props)
    assert.equal(content.all().filter(node => node.type?.name === "SelfHostedGatewayProvisionProgress").length, 1)
    assert.equal(content.find(node => node.type?.name === "InfrastructureEmpty"), undefined)
    assert.equal(harness.find(node => node.props?.["data-tab"] === "gateways").props.children[1].props.children, 1)
    assert.equal(harness.find(node => node.type === "footer").props.children, "共 1 个网关")
  } else {
    assert.ok(harness.find(node => node.type?.name === "ApimTable"))
    assert.equal(harness.find(node => node.type?.name === "GatewayTable"), undefined)
  }
})

test("Gateway creation shares inventory search and never duplicates or resurrects completed rows", () => {
  const target = topology()
  target.deployments.push(topology({ id: "edge", native_gateway_id: "turnstile-unit", kind: "self_hosted", provisioning_operation_id: "creation", display_name: "Docker edge" }).deployments[0])
  const current = {
    id: "creation", gateway_profile_id: "first", status: "queued", worker_available: true,
    checkpoint: {}, semantic_preview: { native_gateway_id: "turnstile-unit", configuration: {
      display_name: "New Docker", endpoint_url: "https://new.example.test", hosting_mode: "docker",
    } },
  }
  const queryData = { provisions: { items: [current] } }
  const props = {
    mode: "gateways", registry: { models: [], runtimes: [], gateways: profiles, self_hosted_gateway_enabled: true },
    topology: { items: [target], resource_lifecycle_available: true, self_hosted_gateway_provisioning_available: true },
    loading: false, error: null, onTabChange() {}, onToggleSidebar() {},
  }
  const harness = componentHarness("ApimGatewayWorkspace", props, { queryData })
  const table = () => harness.find(node => node.type?.name === "GatewayTable")
  const count = () => harness.find(node => node.props?.["data-tab"] === "gateways").props.children[1].props.children
  assert.equal(table().props.provisionItems.length, 1)
  assert.equal(table().props.entries.length, 0)
  assert.equal(count(), 1)
  harness.find(node => node.type === "ExpandableSearch").props.onChange("no-match")
  assert.equal(table().props.provisionItems.length, 0)
  assert.equal(harness.find(node => node.type === "footer").props.children, "共 0 个网关")
  assert.equal(count(), 1)
  harness.find(node => node.type === "ExpandableSearch").props.onChange("unit-service")
  assert.equal(table().props.provisionItems.length, 1)
  current.status = "succeeded"
  current.checkpoint = { deployment_id: "edge" }
  assert.equal(table().props.provisionItems.length, 0)
  assert.equal(table().props.entries.length, 1)
  assert.equal(count(), 1)
  target.deployments = target.deployments.filter(item => item.deployment.id !== "edge")
  assert.equal(table().props.provisionItems.length, 0)
  assert.equal(table().props.entries.length, 0)
  assert.equal(count(), 0)
  current.status = "failed"
  const member = componentHarness("ApimGatewayWorkspace", props, { queryData, role: "member" })
  assert.equal(member.find(node => node.type?.name === "GatewayTable").props.provisionItems.length, 0)
})

test("Gateway creation status, steps and retry errors belong to its own table item", () => {
  const retries = []
  const operation = {
    id: "creation", gateway_profile_id: "first", status: "failed", worker_available: true,
    checkpoint: { apim_gateway_created: true }, error_message: "API association failed",
    semantic_preview: { native_gateway_id: "turnstile-unit", configuration: {
      display_name: "Docker edge", endpoint_url: "https://edge.example.test", hosting_mode: "docker",
    } },
  }
  const props = { operation, target: topology(), available: true, retrying: false, error: new Error("Retry rejected"), onRetry: id => retries.push(id) }
  const harness = componentHarness("SelfHostedGatewayProvisionProgress", props, { code: creationCode })
  const item = harness.render()
  assert.equal(item.props.role, "rowgroup")
  assert.ok(item.props.className.split(" ").includes("connection-provider-group"))
  assert.equal(item.props["data-operation-id"], "creation")
  const rows = harness.all().filter(node => node.props?.role === "row")
  assert.equal(rows.length, 2)
  assert.equal(harness.nodes(rows[0]).filter(node => node.props?.role === "cell").length, 5)
  assert.equal(harness.nodes(rows[0]).find(node => node.props?.role === "status").props.children.props.children[1], "创建失败")
  assert.equal(harness.nodes(rows[1]).find(node => node.props?.role === "alert").props.children, "Retry rejected")
  assert.deepEqual(harness.nodes(rows[1]).filter(node => node.type === "li").map(node => node.props["data-complete"]), [true, false, false])
  const retry = () => harness.find(node => node.props?.["aria-label"] === "重试创建")
  retry().props.onClick()
  assert.deepEqual(retries, ["creation"])
  props.retrying = true
  assert.equal(retry().props.disabled, true)
  props.retrying = false
  props.available = false
  assert.equal(retry().props.disabled, true)
  operation.status = "queued"
  operation.worker_available = false
  operation.error_message = null
  props.error = null
  assert.equal(retry(), undefined)
  assert.equal(harness.find(node => node.props?.role === "status").props.children.props.children[1], "创建已排队，后台处理服务当前不可用")
})

test("Gateway creation completion cannot open a dialog or notice across tabs", () => {
  const current = { id: "creation", gateway_profile_id: "first", status: "queued", checkpoint: {}, semantic_preview: { native_gateway_id: "turnstile-unit", configuration: { display_name: "New Docker" } } }
  const props = { mode: "gateways", registry: { models: [], runtimes: [], gateways: profiles, self_hosted_gateway_enabled: true }, topology: { items: [topology()], self_hosted_gateway_provisioning_available: true, resource_lifecycle_available: true }, loading: false, error: null, onTabChange() {}, onToggleSidebar() {} }
  const harness = componentHarness("ApimGatewayWorkspace", props, { queryData: { provisions: { items: [current] } } })
  harness.render()
  harness.effects.splice(0).forEach(effect => effect())
  props.mode = "apim"
  current.status = "succeeded"
  current.checkpoint = { deployment_id: "edge" }
  harness.render()
  harness.effects.splice(0).forEach(effect => effect())
  assert.equal(harness.find(node => node.type?.name === "SelfHostedGatewayBootstrapDialog"), undefined)
  assert.equal(harness.find(node => node.props?.className === "registry-notice smh-notice"), undefined)
  assert.deepEqual(harness.invalidations, [])
  props.mode = "gateways"
  harness.render()
  harness.effects.splice(0).forEach(effect => effect())
  assert.equal(harness.find(node => node.type?.name === "SelfHostedGatewayBootstrapDialog"), undefined)
  assert.equal(harness.find(node => node.props?.className === "registry-notice smh-notice"), undefined)
  assert.deepEqual(harness.invalidations, [["registry"], ["targets"]])
})

test("Connection access names the exact APIM and only its models' published gateways", () => {
  const target = topology()
  target.deployments[0].published_models = [{ id: "model-one" }]
  const edge = topology({ id: "edge", native_gateway_id: "edge-native", kind: "self_hosted", display_name: "Docker edge" }).deployments[0]
  edge.published_models = [{ id: "another-connection-model" }]
  target.deployments.push(edge)
  const foreign = topology({ id: "foreign", kind: "self_hosted", display_name: "Other APIM edge" })
  foreign.target.gateway_profile_id = "another-target"
  foreign.deployments[0].published_models = [{ id: "model-one" }]
  const props = { runtime: { gateway_profile_id: "first", gateway_name: "Azure API Management" }, gateway: profiles[0], models: [{ id: "model-one" }], topology: { items: [target, foreign] } }
  const harness = componentHarness("ConnectionGatewayCell", props, { code: pageCode })
  assert.equal(harness.find(node => node.type === "b").props.children, "unit-service")
  assert.equal(harness.find(node => node.type === "b").props.title, "unit-service · unit-api")
  const summary = () => harness.find(node => node.type === "span" && typeof node.props.title === "string")
  assert.equal(summary().props.title, "托管网关: Managed")
  assert.equal(summary().props.children[0], "模型发布至 ")
  edge.published_models.push({ id: "model-one" })
  assert.equal(summary().props.title, "托管网关: Managed\n自托管网关: Docker edge")
  edge.deployment.retired_at = "2026-09-26T00:00:00Z"
  assert.equal(summary().props.title, "托管网关: Managed")
  target.deployments[0].published_models = []
  assert.equal(summary(), undefined)
  assert.ok(harness.find(node => node.props?.children === "尚无已发布网关"))
})

test("Connection access keeps unavailable gateway evidence distinct from direct access", () => {
  const props = { runtime: { gateway_profile_id: "first", gateway_name: null }, gateway: profiles[0], models: [], topology: null }
  const harness = componentHarness("ConnectionGatewayCell", props, { code: pageCode })
  assert.equal(harness.find(node => node.type === "b").props.children, profiles[0].base_url)
  assert.ok(harness.find(node => node.props?.children === "网关部署信息不可用"))
  props.gateway = undefined
  assert.equal(harness.find(node => node.type === "b").props.children, "first")
  props.topology = { items: [] }
  assert.ok(harness.find(node => node.props?.children === "网关部署信息不可用"))
  props.runtime.gateway_profile_id = null
  assert.equal(harness.find(node => node.type === "b").props.children, "直连")
  assert.ok(harness.find(node => node.props?.children === "提供方直连"))
})

test("the page routes apim URLs and preserves source three-tab compatibility for old backends", () => {
  assert.equal(componentHarness("modelManagementTabFromUrl", {}, { code: pageCode }).render(), "apim")
  const harness = componentHarness("ModelWorkspace", {
    registry: { models: [], providers: [], runtimes: [], gateways: [] }, topology: { items: [], total: 0 },
    topologyAvailable: true, tab: "models", publications: [], loadingRuntimeHealthIds: new Set(),
  }, { code: pageCode })
  assert.deepEqual(harness.all().filter(node => node.props?.["data-tab"]).map(node => node.props["data-tab"]), ["models", "connections", "gateways", "apim"])
  harness.props.topologyAvailable = false
  assert.deepEqual(harness.all().filter(node => node.props?.["data-tab"]).map(node => node.props["data-tab"]), ["models", "connections", "gateways"])
})

test("model workspace applies filters and search to models, pending rows, empty states and totals", () => {
  const registry = {
    gateways: profiles,
    providers: [{ id: "provider", name: "Foundry", brand_key: "microsoft_foundry", enabled: true }],
    runtimes: [{ id: "connection", name: "Connection", provider_id: "provider", gateway_profile_id: "first", brand_key: "microsoft_foundry", runtime_kind: "foundry", enabled: true, health_status: "unknown", config: {} }],
    models: [
      { id: "alpha", model_key: "gpt-alpha", display_name: "Alpha", runtime_id: "connection", provider_id: "provider", family_key: "openai", capabilities: ["chat"], enabled: true },
      { id: "beta", model_key: "claude-beta", display_name: "Beta", runtime_id: "connection", provider_id: "provider", family_key: "claude", capabilities: ["chat"], enabled: false },
    ],
  }
  const harness = componentHarness("ModelWorkspace", {
    registry, topology: { items: [], total: 0 }, topologyAvailable: true, tab: "models",
    availabilityByModel: new Map(), onOpenAvailability() {},
    publications: [{ id: "pending", model_key: "deepseek-pending", display_name: "DeepSeek pending", publication_kind: "model_add", status: "queued", updated_at: "2026-01-01T00:00:00Z" }],
    loadingRuntimeHealthIds: new Set(),
  }, { code: pageCode })
  const visible = () => harness.all().filter(node => ["ModelRow", "PublicationModelRow"].includes(node.type?.name))
    .map(node => node.props.model?.model_key ?? node.props.publication.model_key)
  const apply = value => harness.find(node => node.type === "ModelInventoryFilterMenu").props.onChange(value)
  const search = value => harness.find(node => node.type === "ExpandableSearch").props.onChange(value)
  assert.deepEqual(visible(), ["gpt-alpha", "claude-beta", "deepseek-pending"])
  apply({ ...modelFilters.emptyModelFilters(), family: ["deepseek"] })
  assert.deepEqual(visible(), ["deepseek-pending"])
  assert.equal(harness.find(node => node.props?.className === "smh-empty"), undefined)
  assert.equal(harness.find(node => node.type === "footer").props.children[1], 1)
  apply({ ...modelFilters.emptyModelFilters(), status: ["enabled"], capability: ["chat"] })
  assert.deepEqual(visible(), ["gpt-alpha"])
  search(" beta ")
  assert.deepEqual(visible(), [])
  assert.ok(harness.find(node => node.props?.className === "smh-empty"))
  assert.equal(harness.find(node => node.type === "footer").props.children[1], 0)
  apply(modelFilters.emptyModelFilters())
  assert.deepEqual(visible(), ["claude-beta"])
  search("")
  assert.deepEqual(visible(), ["gpt-alpha", "claude-beta", "deepseek-pending"])
})

test("model rows separate same-alias publications by APIM and render a dedicated target column", () => {
  const registry = {
    gateways: profiles,
    providers: [{ id: "provider", name: "Foundry", brand_key: "microsoft_foundry", enabled: true }],
    runtimes: [{ id: "connection", name: "Connection", provider_id: "provider", gateway_profile_id: "first", brand_key: "microsoft_foundry", runtime_kind: "foundry", enabled: true, config: {} }],
    models: [{ id: "alpha", model_key: "same-alias", display_name: "Alpha", runtime_id: "connection", provider_id: "provider", capabilities: ["chat"], enabled: true }],
  }
  const publications = [
    { id: "own", gateway_profile_id: "first", model_key: "same-alias", publication_kind: "model_add", status: "active", updated_at: "2026-01-01T00:00:00Z" },
    { id: "other", gateway_profile_id: "second", model_key: "same-alias", display_name: "Another target", publication_kind: "model_add", status: "failed", updated_at: "2026-01-02T00:00:00Z" },
  ]
  const target = topology({ apim_target_id: "first" })
  target.deployments[0].published_models = [{ id: "alpha" }]
  const availability = { model_id: "alpha", status: "pending", desired_enabled: false }
  const opened = []
  const harness = componentHarness("ModelWorkspace", { registry, publications, topology: { items: [target] }, topologyAvailable: true, tab: "models", loadingRuntimeHealthIds: new Set(), availabilityByModel: new Map([["alpha", availability]]), onOpenAvailability: id => opened.push(id) }, { code: pageCode })
  const row = harness.find(node => node.type?.name === "ModelRow")
  assert.equal(row.props.availability, availability)
  assert.equal("onToggle" in row.props, false)
  row.props.onOpenAvailability()
  assert.deepEqual(opened, ["alpha"])
  assert.equal(row.props.publication, null)
  assert.equal(row.props.target.gateways[0].id, "managed")
  assert.equal(harness.find(node => node.type?.name === "PublicationModelRow").props.publication.id, "other")
  const headings = harness.all().filter(node => node.props?.role === "columnheader")
  assert.deepEqual(headings.map(node => node.props["aria-label"]), ["模型", "运行时", "目标网关", "上下文 / 价格", "状态", "操作"])
  const cell = componentHarness("ModelTargetGatewayCell", { target: row.props.target }, { code: pageCode })
  assert.equal(cell.render().props.role, "cell")
  assert.ok(cell.find(node => node.props?.["data-deployment-id"] === "managed"))
  assert.ok(cell.find(node => node.props?.children === "托管网关"))
  const missing = componentHarness("ModelTargetGatewayCell", { target: { gateways: [], apimName: "First", serviceName: null } }, { code: pageCode })
  assert.ok(missing.find(node => node.props?.children === "目标未记录"))
})

test("source initialization queries honor capability presence and active-only 1.5-second polling", () => {
  const props = {
    mode: "apim", registry: { models: [], runtimes: [], gateways: [] },
    topology: { items: [], total: 0, resource_lifecycle_available: true },
    loading: false, error: null, onTabChange() {}, onToggleSidebar() {},
  }
  const harness = componentHarness("ApimGatewayWorkspace", props)
  const query = () => { harness.render(); return harness.queries.find(options => options.queryKey[0] === "onboarding") }
  assert.equal(query().enabled, false)
  props.topology.onboarding_available = false
  assert.equal(query().enabled, true)
  assert.equal(query().refetchInterval({ state: { data: { items: [{ status: "queued" }] } } }), 1500)
  assert.equal(query().refetchInterval({ state: { data: { items: [{ status: "failed" }, { status: "succeeded" }] } } }), false)
  props.mode = "gateways"
  assert.equal(query().enabled, false)
  props.mode = "apim"
  const member = componentHarness("ApimGatewayWorkspace", props, { role: "member" })
  member.render()
  assert.equal(member.queries.find(options => options.queryKey[0] === "onboarding").enabled, false)
})

test("source APIM collapse keeps its children and search preserves the total tab count", () => {
  const target = topology()
  const harness = componentHarness("ApimGatewayWorkspace", {
    mode: "apim", registry: { models: [], runtimes: [], gateways: profiles },
    topology: { items: [target], total: 1, resource_lifecycle_available: true },
    loading: false, error: null, onTabChange() {}, onToggleSidebar() {},
  })
  const table = () => harness.find(node => node.type?.name === "ApimTable")
  table().props.onToggleTarget("first")
  const collapsed = componentHarness("ApimTable", table().props)
  assert.equal(collapsed.find(node => node.props?.id === "apim-gateways-first").props.hidden, true)
  assert.equal(collapsed.all().filter(node => node.type?.name === "GatewayChildRow").length, 1)
  table().props.onToggleTarget("first")
  assert.equal(componentHarness("ApimTable", table().props).find(node => node.props?.id === "apim-gateways-first").props.hidden, false)
  harness.find(node => node.type === "ExpandableSearch").props.onChange("does-not-exist")
  assert.equal(table().props.targets.length, 0)
  assert.equal(table().props.filtered, true)
  assert.equal(harness.find(node => node.props?.["data-tab"] === "apim").props.children[1].props.children, 1)
})

for (const syncAvailable of [false, true]) test(`source attachment refreshes topology and respects inventory sync availability ${syncAvailable}`, async () => {
  const calls = []
  const harness = componentHarness("ApimGatewayWorkspace", {
    mode: "apim", registry: { models: [], runtimes: [], gateways: profiles, self_hosted_gateway_enabled: true },
    topology: { items: [topology()], total: 1, resource_lifecycle_available: true, inventory_sync_available: syncAvailable },
    loading: false, error: null, onTabChange() {}, onToggleSidebar() {},
  }, { api: {
    addSelfHostedGateway: async (targetId, value) => calls.push(["attach", targetId, value]),
    syncApimTarget: async targetId => { calls.push(["sync", targetId]); return { operation: { id: "sync-id", status: "queued" } } },
  } })
  const table = () => harness.find(node => node.type?.name === "ApimTable")
  table().props.onToggleTarget("first")
  table().props.onAttach("first")
  const create = harness.find(node => node.type?.name === "SelfHostedGatewayCreateDialog")
  assert.ok(create)
  assert.equal(harness.find(node => node.type?.name === "GatewayEditor"), undefined)
  create.props.onAdopt("first")
  const editor = harness.find(node => node.type?.name === "GatewayEditor")
  editor.props.onSubmit("first", { native_gateway_id: "edge" })
  await harness.settle()
  assert.deepEqual(calls.map(call => call[0]), syncAvailable ? ["attach", "sync"] : ["attach"])
  assert.ok(calls.every(call => call[1] === "first"))
  assert.equal(table().props.collapsedTargetIds.has("first"), false)
  assert.equal(harness.find(node => node.type?.name === "GatewayEditor"), undefined)
  assert.deepEqual(harness.invalidations, [["registry"], ["targets"]])
})

test("source failed connection validation refreshes persisted evidence", async () => {
  const harness = componentHarness("ApimGatewayWorkspace", {
    mode: "gateways", registry: { models: [], runtimes: [], gateways: profiles },
    topology: { items: [topology({ id: "edge", kind: "self_hosted" })], total: 1, resource_lifecycle_available: true },
    loading: false, error: null, onTabChange() {}, onToggleSidebar() {},
  }, { api: { validateSelfHostedGateway: async () => { throw new Error("Probe failed") } } })
  harness.find(node => node.type?.name === "GatewayTable").props.onValidate("edge")
  await harness.settle()
  assert.deepEqual(harness.invalidations, [["registry"], ["targets"]])
  assert.ok(harness.find(node => node.type?.name === "InfrastructureErrorNotice"))
})

test("source association preserves Gateway identity and blocks published or unknown model dependencies", async () => {
  const target = topology({ id: "edge", kind: "self_hosted", hosting_mode: "kubernetes" })
  const destination = topology()
  destination.target.gateway_profile_id = "second"
  destination.configuration_endpoint = "https://second.configuration.example.test"
  const saved = []
  const props = {
    entry: { ...target.deployments[0], target }, targets: [target, destination],
    canChangeAssociation: true, lifecycleAvailable: true, busy: false, error: null,
    onClose() {}, onSubmit: value => saved.push(value),
  }
  const harness = componentHarness("GatewayAssociationEditor", props)
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  harness.find(node => node.type?.name === "TargetSelect").props.onChange("second")
  harness.find(node => node.props?.type === "url").props.onChange({ target: { value: " https://second.example.test/api " } })
  await submitForm(harness)
  assert.equal(saved[0].apim_target_id, "second")
  assert.equal(saved[0].native_gateway_id, target.deployments[0].deployment.native_gateway_id)
  assert.equal(saved[0].display_name, "Managed")
  assert.equal(saved[0].endpoint_url, "https://second.example.test/api")
  assert.equal(saved[0].hosting_mode, "kubernetes")
  for (const entry of [
    { ...props.entry, published_models: [{ id: "model", display_name: "Model", model_key: "model-key" }] },
    { ...props.entry, published_models_available: false },
  ]) {
    const blocked = componentHarness("GatewayAssociationEditor", { ...props, entry })
    assert.equal(blocked.find(node => node.props?.type === "submit"), undefined)
    assert.equal(blocked.find(node => node.type?.name === "TargetSelect").props.disabled, true)
    await submitForm(blocked)
  }
  assert.equal(saved.length, 1)
})

test("source loading and errors cannot appear as an empty successful inventory", () => {
  const props = {
    mode: "apim", registry: { models: [], runtimes: [], gateways: [] }, topology: null,
    loading: true, error: null, onTabChange() {}, onToggleSidebar() {},
  }
  const harness = componentHarness("ApimGatewayWorkspace", props)
  assert.ok(harness.find(node => node.type?.name === "InfrastructureTableLoading"))
  assert.equal(harness.find(node => node.type === "footer"), undefined)
  props.loading = false
  props.error = new Error("Inventory unavailable")
  assert.ok(harness.find(node => node.props?.role === "alert"))
  assert.equal(harness.find(node => node.type?.name === "ApimTable"), undefined)
  assert.equal(harness.find(node => node.type === "footer"), undefined)
  props.error = null
  props.topology = { items: [], total: 0 }
  assert.ok(harness.find(node => node.type?.name === "ApimTable"))
  assert.ok(harness.find(node => node.type === "footer"))
})

test("source Gateway attachment validates endpoints and keeps the selected APIM without a release-gate field", async () => {
  const saved = []
  const harness = componentHarness("GatewayEditor", {
    targets: [topology()], initialTargetId: "first", available: true, busy: false, error: null,
    onClose() {}, onSubmit: (...values) => saved.push(values),
  }, { code: workspaceCode })
  for (const [name, value] of Object.entries({ display_name: " Edge ", native_gateway_id: " edge ", endpoint_url: "https://edge.example.test/api?" })) {
    harness.find(node => node.props?.name === name).props.onChange({ target: { value } })
  }
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  harness.find(node => node.props?.name === "endpoint_url").props.onChange({ target: { value: "https://edge.example.test/api" } })
  assert.equal(harness.find(node => node.type?.name === "TargetSelect").props.disabled, true)
  assert.equal(harness.find(node => node.props?.name === "release_gate"), undefined)
  await submitForm(harness)
  assert.equal(saved[0][0], "first")
  assert.equal(saved[0][1].display_name, "Edge")
  assert.equal(saved[0][1].hosting_mode, "kubernetes")
  assert.equal(saved[0][1].release_gate, "required")
  harness.props.available = false
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
})

test("source APIM adoption form validates identity without requesting a management credential", async () => {
  const saved = []
  const harness = componentHarness("ApimEditor", { busy: false, error: null, onClose() {}, onSubmit: value => saved.push(value) }, { code: workspaceCode })
  const subscription = "00000000-0000-0000-0000-000000000002"
  const values = { name: " Unit ", tenant_id: "00000000-0000-0000-0000-000000000003", subscription_id: subscription,
    resource_group: "unit", service_name: "unit-service", api_id: "unit-api", gateway_endpoint: "https://unit.example.test/api",
    management_endpoint: `https://management.azure.com/subscriptions/${subscription}/resourceGroups/unit/providers/Microsoft.ApiManagement/service/unit-service`,
  }
  for (const [name, value] of Object.entries(values)) harness.find(node => node.props?.name === name).props.onChange({ target: { value } })
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, false)
  assert.equal(harness.find(node => node.props?.type === "password"), undefined)
  await submitForm(harness)
  assert.equal(saved[0].name, "Unit")
  assert.equal(saved[0].management_auth_profile_ref, "system_managed_identity")
  harness.find(node => node.props?.name === "service_name").props.onChange({ target: { value: "other" } })
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
})

test("source name editor submits only a trimmed name and honors backend support", async () => {
  const saved = []
  const harness = componentHarness("InfrastructureNameEditor", {
    kind: "gateway", identity: "edge", name: "Edge", supported: true, busy: false, error: null, onClose() {}, onSubmit: value => saved.push(value),
  }, { code: workspaceCode })
  assert.equal(harness.all().filter(node => node.type === "Input").length, 1)
  harness.find(node => node.type === "Input").props.onChange({ target: { value: " Renamed " } })
  await submitForm(harness)
  assert.deepEqual(saved, ["Renamed"])
  harness.props.supported = false
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
})

test("source removal confirmation blocks loading or dependencies and retains the shared footer", () => {
  const harness = componentHarness("RemovalDialog", { target: { kind: "apim", id: "first", name: "First" }, preview: null,
    loading: true, busy: false, error: null, onClose() {}, onConfirm() {},
  }, { code: workspaceCode })
  const action = () => harness.find(node => node.type === "AlertDialogAction")
  assert.equal(action().props.disabled, true)
  assert.equal(harness.find(node => node.type === "AlertDialogFooter").props.className, "model-delete-footer")
  harness.props.loading = false
  harness.props.preview = { removal_allowed: false, blockers: ["Remove model connections first"], models: [] }
  assert.equal(action().props.disabled, true)
  harness.props.preview.removal_allowed = true
  assert.equal(action().props.disabled, false)
})

test("source Gateway actions are Owner-only and Managed children have no lifecycle controls", () => {
  const props = { name: "Edge", checkLabel: "验证连接", removeLabel: "移除", busy: false, checking: false, checkDisabled: false, removeDisabled: false,
    onCheck() {}, onEdit() {}, onBinding() {}, onRemove() {},
  }
  const member = componentHarness("InfrastructureActions", props, { code: workspaceCode, role: "member" })
  assert.equal(member.all().filter(node => node.type === "DropdownMenuItem").length, 0)
  const owner = componentHarness("InfrastructureActions", props, { code: workspaceCode })
  assert.equal(owner.render().props.className, "model-row-actions infrastructure-row-actions gateway-name-actions")
  assert.equal(owner.all().filter(node => node.type === "DropdownMenuItem").length, 4)
  const target = topology()
  const child = componentHarness("GatewayChildRow", { entry: { ...target.deployments[0], target }, busy: false, lifecycleAvailable: true }, { code: workspaceCode })
  assert.equal(child.find(node => node.type?.name === "InfrastructureActions"), undefined)
  assert.ok(child.find(node => node.type === "ManagedGatewayLogo"))
})

test("source Gateway validation dispatches directly from the existing Owner action", async () => {
  const calls = []
  const target = topology()
  const harness = componentHarness("ApimGatewayWorkspace", { mode: "gateways", registry: { gateways: profiles, runtimes: [], models: [] },
    topology: { items: [target], total: 1, resource_lifecycle_available: true }, loading: false, error: null,
    onTabChange() {}, onToggleSidebar() {},
  }, { code: workspaceCode, api: { async validateSelfHostedGateway(...values) { calls.push(values) } } })
  harness.find(node => node.type?.name === "GatewayTable").props.onValidate("edge")
  await harness.settle()
  assert.deepEqual(calls, [["edge"]])
  assert.equal(harness.find(node => node.type === "ManagementCredentialDialog"), undefined)
})

test("source loading state preserves the APIM parent and child hierarchy", () => {
  const apim = componentHarness("InfrastructureTableLoading", { mode: "apim" }, { code: workspaceCode })
  assert.ok(apim.find(node => node.props?.role === "rowgroup" && node.props?.["aria-busy"] === "true"))
  assert.equal(apim.all().filter(node => node.props?.role === "row").length, 2)
  const gateways = componentHarness("InfrastructureTableLoading", { mode: "gateways" }, { code: workspaceCode })
  assert.equal(gateways.all().filter(node => node.props?.role === "row").length, 1)
})

test("source mobile actions remain visible and initialization retry retains its dimensions", () => {
  const { parse } = frontendRequire("postcss")
  const css = parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const mobileRules = []
  const retry = new Map()
  css.walkRules(rule => {
    if (rule.parent.type === "atrule" && rule.parent.params === "(max-width: 760px)") mobileRules.push(rule)
    if (rule.selector === ".apim-onboarding-details .apim-onboarding-error > button.apim-onboarding-retry") {
      rule.walkDecls(declaration => retry.set(declaration.prop, declaration.value))
    }
  })
  assert.ok(mobileRules.some(rule => rule.selector === ".model-row-actions:not(.infrastructure-row-actions) > button:not(.toggle)"))
  assert.ok(!mobileRules.some(rule => rule.selector === ".model-row-actions > button:not(.toggle)"))
  const tabs = mobileRules.find(rule => rule.selector === ".model-workspace .model-tabs")
  assert.equal(tabs.nodes.find(declaration => declaration.prop === "width").value, "100%")
  assert.equal(tabs.nodes.find(declaration => declaration.prop === "overflow-x").value, "auto")
  assert.equal(retry.get("width"), "auto")
  assert.equal(retry.get("height"), "28px")
  assert.equal(retry.get("max-width"), "100%")
})

test("Gateway creation rows share existing item margins without shifting columns", () => {
  const { parse } = frontendRequire("postcss")
  const css = parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const declarationsFor = (selector, mobile = false) => {
    const declarations = new Map()
    css.walkRules(rule => {
      const isMobile = rule.parent.type === "atrule" && rule.parent.params === "(max-width: 760px)"
      if (isMobile === mobile && rule.selectors.includes(selector)) rule.walkDecls(declaration => declarations.set(declaration.prop, declaration.value))
    })
    return declarations
  }
  for (const mobile of [false, true]) {
    assert.deepEqual(declarationsFor(".gateway-provision-item", mobile), declarationsFor(".apim-onboarding-row", mobile))
    assert.deepEqual(declarationsFor(".gateway-provision-item::before", mobile), declarationsFor(".apim-onboarding-row::before", mobile))
    assert.deepEqual(declarationsFor('.gateway-provision-details > [role="cell"]', mobile), declarationsFor('.apim-onboarding-details > [role="cell"]', mobile))
    const row = declarationsFor(".gateway-provision-item .gateway-provision-row", mobile)
    for (const property of ["padding", "padding-inline", "padding-left", "padding-right", "margin", "margin-inline", "grid-template-columns", "width"]) assert.equal(row.has(property), false, property)
  }
  assert.equal(declarationsFor(".gateway-provision-item::before").get("position"), "absolute")
  assert.equal(declarationsFor(".gateway-provision-item::before").get("inset"), "0 16px")
  assert.equal(declarationsFor(".gateway-provision-item::before", true).get("inset-inline"), "12px")
})

for (const [changes, label] of [
  [{ retired_at: "2026-09-23T00:00:00Z" }, "已退役"],
  [{ declared_state: "absent" }, "未发现"],
  [{ endpoint_url: null }, "未配置"],
  [{ declared_state: "unknown" }, "待同步"],
  [{ last_probe_at: null }, "待验证"],
  [{ reachability_state: "unreachable" }, "不可达"],
  [{}, "已验证"],
]) test(`Gateway evidence: ${label}`, () => {
  const target = topology(changes)
  assert.equal(gatewayEvidence({ ...target.deployments[0], target }).label, label)
})

test("declared self-hosted Gateway without API membership is not verified", () => {
  const target = topology({ kind: "self_hosted" })
  const entry = { ...target.deployments[0], inventory: { associated_api_ids: ["other-api"] }, target }
  assert.equal(gatewayEvidence(entry).label, "未关联")
  assert.equal(apimGatewayEntries(topology({ retired_at: "2026-09-23T00:00:00Z" })).length, 0)
})

test("legacy profiles remain selectable only in compatibility mode", () => {
  assert.equal(gatewayPublicationChoices(profiles, null, false)[0].targetId, "first")
  assert.deepEqual(gatewayPublicationChoices(profiles, null, true), [])
})

test("required sibling readiness gates selected optional deployments", () => {
  const target = topology({ reachability_state: "unknown" })
  target.deployments.push(topology({ id: "optional", kind: "self_hosted", release_gate: "optional" }).deployments[0])
  const choices = gatewayPublicationChoices(profiles, { items: [target] }, true)
  assert.equal(choices.length, 2)
  assert.ok(choices.every(choice => !choice.selectable && choice.reason === "必需网关未就绪"))
})

test("an explicit invalid selection never jumps to another APIM or deployment", () => {
  const choices = gatewayPublicationChoices(profiles, { items: [topology()] }, true)
  assert.equal(resolveGatewaySelection(profiles, choices, { targetId: "missing", deploymentId: null }).choice, undefined)
  assert.equal(resolveGatewaySelection(profiles, choices, { targetId: "first", deploymentId: "removed" }).choice, undefined)
  assert.equal(resolveGatewaySelection(profiles, choices, { targetId: null, deploymentId: null }).choice.deploymentId, "managed")
})

test("only a missing topology API enables legacy fallback", async () => {
  assert.equal(await apiHarness(async () => { throw new ApiError(404) }).apimTargets(), null)
  for (const status of [401, 403, 500, 503]) {
    await assert.rejects(apiHarness(async () => { throw new ApiError(status) }).apimTargets(), error => error.status === status)
  }
})

test("initialization clients use exact URLs and preserve authorization errors", async () => {
  assert.equal(await apiHarness(async () => { throw new ApiError(404) }).apimOnboardingOperations(), null)
  for (const status of [401, 403, 500, 503]) {
    await assert.rejects(apiHarness(async () => { throw new ApiError(status) }).apimOnboardingOperations(), error => error.status === status)
  }
  const calls = []
  const api = apiHarness(async (...args) => { calls.push(args); return {} })
  const request = { tenant_id: "unit-tenant", resource_id: "unit-resource" }
  const start = { request, confirmation_sha256: "a".repeat(64) }
  await api.previewApimOnboarding(request)
  await api.startApimOnboarding(start)
  await api.retryApimOnboarding("first/second")
  assert.equal(calls[0][0], "/api/v1/model-management/apim-onboarding/preview")
  assert.equal(calls[0][1], request)
  assert.equal(calls[1][0], "/api/v1/model-management/apim-onboarding")
  assert.equal(calls[1][1], start)
  assert.equal(calls[2][0], "/api/v1/model-management/apim-onboarding/first%2Fsecond/retry")
  assert.equal(calls[2][1].method, "POST")
})

test("source management clients use Owner session without extra credential parameters", async () => {
  const calls = []
  const api = apiHarness(async (...args) => { calls.push(args); return {} })
  const target = { name: "Unit APIM" }
  const preview = { tenant_id: "unit-tenant", resource_id: "unit-resource" }
  const start = { request: preview, confirmation_sha256: "a".repeat(64) }
  await api.registerApimTarget(target)
  await api.previewApimOnboarding(preview)
  await api.startApimOnboarding(start)
  await api.retryApimOnboarding("first/second")
  await api.validateSelfHostedGateway("first/second")
  assert.equal(calls.length, 5)
  for (const [path, init] of calls) {
    assert.ok(path.startsWith("/api/v1/model-management/"))
    assert.equal(init.headers?.Authorization, undefined)
  }
  assert.equal(calls[0][1], target)
  assert.equal(calls[1][1], preview)
  assert.equal(calls[2][1], start)
  assert.equal(calls[3][1].method, "POST")
  assert.equal(calls[4][1].method, "POST")
  assert.equal(calls[3][1].body, undefined)
  assert.equal(calls[4][1].body, undefined)
  assert.ok(calls[3][0].includes("first%2Fsecond"))
  assert.ok(calls[4][0].includes("first%2Fsecond"))
})

test("initialization polls only active tasks and replaces stale attempts after accepted retry", async () => {
  const { finopsKeys, finopsQueries, storeApimOnboarding } = queryHarness()
  const options = finopsQueries.apimOnboarding()
  for (const status of ["succeeded", "failed", "restored"]) {
    assert.equal(options.refetchInterval({ state: { data: { items: [{ status }] } } }), false)
  }
  assert.equal(options.refetchInterval({ state: { data: { items: [{ status: "queued" }] } } }), 2000)
  assert.equal(options.retry, false)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const other = { id: "other", gateway_profile_id: "other-target", status: "failed" }
  const previous = { available: true, items: [{ id: "old", gateway_profile_id: "first", status: "failed" }, other] }
  client.setQueryData(finopsKeys.apimOnboarding, previous)
  let release
  const pending = client.fetchQuery({ queryKey: finopsKeys.apimOnboarding, queryFn: () => new Promise(resolve => { release = resolve }) }).catch(() => {})
  const accepted = { id: "new", gateway_profile_id: "first", status: "queued" }
  await storeApimOnboarding(client, accepted)
  release(previous)
  await pending
  const current = client.getQueryData(finopsKeys.apimOnboarding)
  assert.equal(current.available, true)
  assert.deepEqual(Array.from(current.items, item => item.id), ["new", "other"])
  assert.equal(client.getQueryData(finopsKeys.gatewayReleaseOperation("new")), accepted)
  client.clear()
})

function initializationPlan(ready = true) {
  return {
    request: { tenant_id: "00000000-0000-0000-0000-000000000012", resource_id: "/subscriptions/00000000-0000-0000-0000-000000000034/resourceGroups/unit/providers/Microsoft.ApiManagement/service/unit-service", name: "Unit APIM" },
    service_name: "unit-service", service_tier: "Developer", location: "unit-region",
    managed_gateway_origin: "https://unit-service.azure-api.net/", api_path: "turnstile/unit", api_id: "unit-api",
    dashboard_subscription_id: "unit-dashboard", probe_subscription_id: "unit-probe",
    initial_monthly_token_limit: 100000, initial_tokens_per_minute: 100000,
    steps: ["validate_target", "create_api"], blockers: ready ? [] : ["budget_execution_not_ready"],
    ready, confirmation_sha256: "a".repeat(64),
  }
}
function onboardingHarness(api, extra = {}) {
  const harness = componentHarness("ApimOnboardingDialog", {
    available: true, onClose() {}, onAccepted() {}, onSettled() {}, ...extra,
  }, { code: onboardingCode, api })
  for (const input of harness.all().filter(node => node.type === "Input")) {
    input.props.onChange({ target: { value: ` ${initializationPlan().request[input.props.name]} ` } })
  }
  return harness
}
const submitForm = harness => harness.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })

test("new Gateway dialog requires no native ID and retries an uncertain submission with identical identity", async () => {
  const calls = []
  const accepted = []
  const harness = componentHarness("SelfHostedGatewayCreateDialog", {
    targets: [topology()], initialTargetId: null, available: true,
    onClose() {}, onAdopt() {}, onAccepted: value => accepted.push(value), onSettled() {},
  }, { code: creationCode, api: { createSelfHostedGateway: async (...args) => {
    calls.push(args)
    if (calls.length === 1) throw new Error("Response lost")
    return { operation: { id: "created" } }
  } } })
  assert.equal(harness.find(node => node.props?.name === "native_gateway_id"), undefined)
  assert.equal(harness.find(node => node.type === "form").props["data-request-id"], "unit-request-id")
  for (const [name, value] of Object.entries({ display_name: "  Docker edge  ", endpoint_url: "https://edge.test/unit/llm" })) {
    harness.find(node => node.props?.name === name).props.onChange({ target: { value } })
  }
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  harness.find(node => node.type === "Checkbox").props.onCheckedChange(true)
  await submitForm(harness)
  assert.equal(harness.find(node => node.props?.role === "alert").props.children, "Response lost")
  assert.ok(harness.all().filter(node => node.type === "Input").every(node => node.props.disabled))
  await submitForm(harness)
  assert.equal(calls.length, 2)
  assert.deepEqual(calls[0], calls[1])
  assert.equal(calls[0][2], "unit-request-id")
  assert.equal(calls[0][1].display_name, "Docker edge")
  assert.equal(calls[0][1].hosting_mode, "docker")
  assert.equal("native_gateway_id" in calls[0][1], false)
  assert.equal(accepted[0].id, "created")
})

test("new Gateway creation capability fails closed and old association stays a separate action", async () => {
  let adopted = false
  const harness = componentHarness("SelfHostedGatewayCreateDialog", {
    targets: [topology()], initialTargetId: "first", available: false,
    onClose() {}, onAdopt: () => { adopted = true }, onAccepted() {}, onSettled() {},
  }, { code: creationCode })
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  assert.ok(harness.find(node => node.props?.role === "status"))
  harness.find(node => node.type === "Button" && node.props.children?.includes?.("关联已有网关")).props.onClick()
  assert.equal(adopted, true)
})

test("startup configuration stays outside query state and DOM, downloads then clears on close", async () => {
  const blobs = []
  const links = []
  const revoked = []
  let requestedSignal
  const harness = componentHarness("SelfHostedGatewayBootstrapDialog", {
    deploymentId: "edge", name: "Docker edge", onClose() {}, onValidate() {},
  }, { code: creationCode, api: { selfHostedGatewayBootstrap: async (id, hours, signal) => {
    assert.equal(id, "edge"); assert.equal(hours, 24); requestedSignal = signal
    return { native_gateway_id: "turnstile-generated", deployment_id: id,
      configuration_endpoint: "https://unit.configuration.azure-api.net", expires_at: "2026-10-01T00:00:00Z",
      image: "mcr.microsoft.com/azure-api-management/gateway:2.12.1", environment_file_name: "turnstile-generated.env",
      environment_file: "config.service.auth=GatewayKey confidential-bootstrap-token", docker_command: "docker run --env-file turnstile-generated.env official-image", renewal_command: "docker stop turnstile-generated && docker rm turnstile-generated && docker run --env-file turnstile-generated.env official-image" }
  } }, globals: {
    URL: { createObjectURL: blob => { blobs.push(blob); return "blob:unit" }, revokeObjectURL: url => revoked.push(url) },
    document: { createElement: () => { const link = { click() { links.push(this) } }; return link } },
  } })
  harness.render()
  const cleanup = harness.effects[0]()
  assert.equal(harness.mutationVariables.length, 0)
  assert.equal(harness.queries.length, 0)
  await harness.find(node => node.type === "Button" && node.props.children?.includes?.("获取启动配置")).props.onClick()
  assert.equal(requestedSignal.aborted, false)
  assert.equal(JSON.stringify(harness.render()).includes("confidential-bootstrap-token"), false)
  const selects = harness.all().filter(node => node.type === "Select")
  selects[1].props.onValueChange("renew")
  assert.ok(harness.find(node => node.type === "textarea").props.value.startsWith("docker stop turnstile-generated"))
  const download = harness.find(node => node.type === "Button" && node.props.children?.includes?.("下载环境文件"))
  download.props.onClick()
  assert.equal(await blobs[0].text(), "config.service.auth=GatewayKey confidential-bootstrap-token")
  assert.equal(links[0].download, "turnstile-generated.env")
  assert.deepEqual(revoked, ["blob:unit"])
  cleanup()
  download.props.onClick()
  assert.equal(blobs.length, 1)
})

test("newly created configuration is waiting for its container, never reported verified", () => {
  const target = topology({ id: "edge", native_gateway_id: "edge", kind: "self_hosted", provisioning_operation_id: "creation", last_probe_at: null, reachability_state: "unknown" })
  const evidence = gatewayEvidence(apimGatewayEntries(target)[0])
  assert.equal(evidence.state, "warning")
  assert.equal(evidence.label, "待启动")
})

test("initialization uses the confirmed server plan and invalidates it on Back", async () => {
  const plan = initializationPlan()
  const calls = []
  const accepted = []
  const harness = onboardingHarness({
    previewApimOnboarding: async request => { calls.push(["preview", request]); return plan },
    startApimOnboarding: async request => { calls.push(["start", request]); return { operation: { id: "unit-operation" } } },
  }, { onAccepted: operation => accepted.push(operation) })
  await submitForm(harness)
  assert.equal(calls[0][1].name, "Unit APIM")
  assert.equal(calls[0][1].tenant_id, plan.request.tenant_id)
  assert.equal(harness.all().filter(node => node.type === "Input").length, 0)
  await submitForm(harness)
  assert.equal(calls[1][1].request, plan.request)
  assert.equal(calls[1][1].confirmation_sha256, plan.confirmation_sha256)
  assert.equal(accepted[0].id, "unit-operation")
  harness.find(node => node.type === "Button" && node.props.children?.includes?.("返回修改")).props.onClick()
  assert.equal(harness.find(node => node.props?.name === "name").props.value.trim(), "Unit APIM")
  await submitForm(harness)
  assert.equal(calls[2][0], "preview")
})

test("blocked initialization plans can recheck but never start", async () => {
  let checks = 0
  const harness = onboardingHarness({
    previewApimOnboarding: async () => { checks++; return initializationPlan(false) },
    startApimOnboarding: async () => { throw new Error("blocked plan must not start") },
  })
  await submitForm(harness)
  assert.ok(harness.find(node => node.props?.role === "alert"))
  await submitForm(harness)
  assert.equal(checks, 2)
})

test("initialization follows the source Owner flow without a management credential field", async () => {
  const calls = []
  const harness = onboardingHarness({
    previewApimOnboarding: async (value, credential) => { calls.push({ value, credential }); return initializationPlan() },
    startApimOnboarding: async (value, credential) => { calls.push({ value, credential }); return { operation: { id: "unit-operation" } } },
  })
  assert.equal(harness.find(node => node.type === "ManagementCredentialField"), undefined)
  await submitForm(harness)
  await submitForm(harness)
  assert.deepEqual(calls.map(call => call.credential), [undefined, undefined])
})

test("initialization prevents duplicate submission and dismissal until preview settles", async () => {
  let finish
  let calls = 0
  let closes = 0
  const harness = onboardingHarness({ previewApimOnboarding: () => { calls++; return new Promise(resolve => { finish = resolve }) } }, { onClose() { closes++ } })
  const pending = submitForm(harness)
  await submitForm(harness)
  harness.render().props.onOpenChange(false)
  assert.equal(calls, 1)
  assert.equal(closes, 0)
  assert.ok(harness.all().filter(node => node.type === "Input").every(node => node.props.disabled))
  finish(initializationPlan())
  await pending
  harness.render().props.onOpenChange(false)
  assert.equal(closes, 1)
})

test("initialization error preserves the confirmed plan and disables unavailable execution", async () => {
  const harness = onboardingHarness({
    previewApimOnboarding: async () => initializationPlan(),
    startApimOnboarding: async () => { throw new Error("Preflight unavailable") },
  })
  await submitForm(harness)
  await submitForm(harness)
  assert.equal(harness.find(node => node.props?.role === "alert").props.children, "Preflight unavailable")
  assert.ok(harness.find(node => node.props?.["aria-label"] === "APIM 初始化计划"))
  harness.props.available = false
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
})

test("initialization progress retains failure, exact retry identity and completed steps", async () => {
  const retries = []
  const operation = { id: "failed-task", semantic_preview: initializationPlan(), status: "failed", worker_available: true,
    checkpoint: { completed_steps: ["validate_target", "create_api"], retry_of: "previous-task" }, error_message: "Permission preflight failed" }
  const harness = componentHarness("ApimOnboardingRow", { operation, available: true, retryingId: null, onRetry: id => { retries.push(id) } }, { code: onboardingCode })
  assert.equal(harness.all().filter(node => node.props?.["data-complete"] === true).length, 2)
  harness.find(node => node.props?.className === "apim-onboarding-retry").props.onClick()
  assert.deepEqual(retries, ["failed-task"])
  harness.find(node => node.props?.["aria-label"] === "收起初始化进度").props.onClick()
  assert.equal(harness.find(node => node.props?.role === "alert").props.children, "Permission preflight failed")
  harness.props.retryingId = "another-task"
  assert.equal(harness.find(node => node.props?.className === "apim-onboarding-retry").props.disabled, true)
})

test("Self-hosted clients keep exact identity, minimal name payload and mutation methods", async () => {
  const calls = []
  const api = apiHarness(async (...args) => { calls.push(args); return {} })
  const payload = { native_gateway_id: "edge", display_name: "Edge", endpoint_url: "https://edge.example.test" }
  await api.addSelfHostedGateway("target/path", payload)
  await api.renameSelfHostedGateway("edge/path", "Renamed")
  await api.validateSelfHostedGateway("edge/path")
  await api.selfHostedGatewayRemovalPreview("edge/path")
  await api.removeSelfHostedGateway("edge/path")
  const change = { ...payload, apim_target_id: "destination" }
  await api.changeSelfHostedGatewayAssociation("edge/path", change)
  const root = "/api/v1/model-management/self-hosted-gateways/edge%2Fpath"
  assert.equal(calls[0][0], "/api/v1/model-management/apim-targets/target%2Fpath/self-hosted-gateways")
  assert.equal(calls[0][1], payload)
  assert.deepEqual(Object.keys(calls[1][1]), ["display_name"])
  assert.equal(calls[1][1].display_name, "Renamed")
  assert.equal(calls[1][2], "PATCH")
  assert.deepEqual(calls.slice(1).map(call => call[0]), [root + "/name", root + "/validations", root + "/removal-preview", root, root + "/apim-association"])
  assert.equal(calls[2][1].method, "POST")
  assert.equal(calls[4][1].method, "DELETE")
  assert.equal(calls[5][1], change)
  assert.equal(calls[5][2], "PUT")
})

test("confirmed Self-hosted names and retirements survive stale topology responses", async () => {
  const { finopsKeys, storeGatewayDeployment, storeGatewayDeploymentName } = queryHarness()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const previous = { items: [topology({ id: "edge", apim_target_id: "first", kind: "self_hosted", display_name: "Old" })] }
  client.setQueryData(finopsKeys.apimTargets, previous)
  let release
  const pending = client.fetchQuery({ queryKey: finopsKeys.apimTargets, queryFn: () => new Promise(resolve => { release = resolve }) }).catch(() => {})
  await storeGatewayDeploymentName(client, "edge", " Confirmed ")
  release(previous)
  await pending
  assert.equal(client.getQueryData(finopsKeys.apimTargets).items[0].deployments[0].deployment.display_name, "Confirmed")
  await storeGatewayDeployment(client, null, "edge")
  assert.equal(client.getQueryData(finopsKeys.apimTargets).items[0].deployments.length, 0)
  assert.equal(client.getQueryData(finopsKeys.apimTargets).items[0].self_hosted_gateway_count, 0)
  client.clear()
})

test("confirmed reassociation removes the old entry and adds only its exact destination", async () => {
  const { finopsKeys, storeGatewayDeployment, finopsQueries } = queryHarness()
  const client = new QueryClient()
  const first = topology({ id: "old-edge", apim_target_id: "first", kind: "self_hosted" })
  const second = { ...topology(), target: { ...topology().target, gateway_profile_id: "second" }, deployments: [] }
  client.setQueryData(finopsKeys.apimTargets, { items: [first, second] })
  const saved = { ...first.deployments[0], deployment: { ...first.deployments[0].deployment, id: "new-edge", apim_target_id: "second" } }
  await storeGatewayDeployment(client, saved, "old-edge")
  const items = client.getQueryData(finopsKeys.apimTargets).items
  assert.equal(items[0].deployments.length, 0)
  assert.equal(items[1].deployments[0].deployment.id, "new-edge")
  assert.equal(items[1].self_hosted_gateway_count, 1)
  assert.equal(finopsQueries.selfHostedGatewayRemoval(null).enabled, false)
  assert.equal(finopsQueries.selfHostedGatewayRemoval("edge").staleTime, 0)
  client.clear()
})

function selfHostedEntry() {
  const target = topology({ id: "edge", apim_target_id: "first", kind: "self_hosted", native_gateway_id: "edge", hosting_mode: "docker", display_name: "Edge", location: "Unit" })
  return { ...target.deployments[0], target }
}

test("multi-target clients preserve parent and child identity with exact public URLs", async () => {
  const calls = []
  const rollout = { id: "parent", status: "partial", logical_model_id: "logical-model", children: [
    { apim_target_id: "first", publication_id: "child-one", required: true, status: "succeeded" },
    { apim_target_id: "second", publication_id: "child-two", required: false, status: "failed" },
  ] }
  const response = { rollout, status_url: "/api/v1/model-management/rollouts/parent", publications: [
    { id: "child-one", gateway_profile_id: "first", status: "active" },
    { id: "child-two", gateway_profile_id: "second", status: "failed", retry_requires_credential: true },
  ] }
  const api = apiHarness(async (...args) => { calls.push(args); return args[0].endsWith("?limit=50") ? { items: [rollout] } : rollout },
    async (...args) => { calls.push(args); return response })
  const payload = { children: [{ publication: { gateway_profile_id: "first" } }, { publication: { gateway_profile_id: "second" }, required: false }] }
  const accepted = await api.publishMultiTargetModel(payload)
  assert.equal(calls[0][0], "/api/v1/model-management/rollouts")
  assert.equal(calls[0][1], payload)
  assert.equal(accepted.rollout, rollout)
  assert.equal(accepted.status_url, response.status_url)
  assert.deepEqual(Array.from(accepted.publications, item => item.id), ["child-one", "child-two"])
  assert.equal(accepted.publications[1].retry_requires_credential, true)
  assert.equal((await api.multiTargetRollouts()).items[0], rollout)
  assert.equal(await api.multiTargetRollout("parent/path"), rollout)
  assert.equal(calls[1][0], "/api/v1/model-management/rollouts?limit=50")
  assert.equal(calls[2][0], "/api/v1/model-management/rollouts/parent%2Fpath")
})

test("multi-target clients never turn authorization or availability errors into success", async () => {
  for (const status of [401, 403, 404, 409, 503]) {
    const api = apiHarness(async () => { throw new ApiError(status) })
    await assert.rejects(api.publishMultiTargetModel({ children: [] }), error => error.status === status)
    await assert.rejects(api.multiTargetRollout("parent"), error => error.status === status)
    await assert.rejects(api.multiTargetRollouts(), error => error.status === status)
  }
})

test("registration and sync clients use the exact public routes", async () => {
  const calls = []
  const api = apiHarness(async (...args) => { calls.push(args); return { operation: { id: "queued" } } })
  const payload = { name: "Unit", tenant_id: "unit-tenant", api_id: "unit-api" }
  await api.registerApimTarget(payload)
  await api.syncApimTarget("first/second")
  assert.equal(calls[0][0], "/api/v1/model-management/apim-targets")
  assert.equal(calls[0][1], payload)
  assert.equal(calls[1][0], "/api/v1/model-management/apim-targets/first%2Fsecond/inventory-sync")
  assert.equal(calls[1][1].method, "POST")
})

test("a late topology read cannot replace a confirmed target write", async () => {
  const { finopsKeys, storeApimTarget } = queryHarness()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const previous = { items: [topology()], total: 1, inventory_sync_available: true }
  client.setQueryData(finopsKeys.apimTargets, previous)
  let release
  const pending = client.fetchQuery({ queryKey: finopsKeys.apimTargets, queryFn: () => new Promise(resolve => { release = resolve }) }).catch(() => {})
  const saved = topology()
  saved.target.service_name = "confirmed-service"
  await storeApimTarget(client, saved)
  release(previous)
  await pending
  assert.equal(client.getQueryData(finopsKeys.apimTargets).items[0].target.service_name, "confirmed-service")
  assert.equal(client.getQueryData(finopsKeys.apimTargets).total, 1)
  client.clear()
})
