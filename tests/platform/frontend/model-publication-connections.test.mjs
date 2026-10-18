// Isolated component and selection tests only: no browser, API, authentication or database.
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire, stripTypeScriptTypes } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import { cachePolicies } from "../../../frontend/src/api/cache-policies.ts"
import { createModelPriceDraft, validateModelPricing } from "../../../frontend/src/components/model-management/model-edit-form.ts"
import * as gatewayForms from "../../../frontend/src/lib/gateway-forms.ts"
import { consumeGatewayBootstrap, downloadGatewayEnvironment } from "../../../frontend/src/lib/gateway-bootstrap.ts"
import * as gatewayTopology from "../../../frontend/src/lib/gateway-topology.ts"
import { availabilityPresentation, availabilityRefetchInterval } from "../../../frontend/src/lib/model-availability.ts"
import * as runtimeHealth from "../../../frontend/src/lib/runtime-health.ts"
import * as usageFilterScope from "../../../frontend/src/lib/usage-filter-scope.ts"
import { runtimeDiagnosticPrefix, runtimeHealthDescription, runtimeHealthState, runtimeWithTelemetryHealth } from "../../../frontend/src/lib/runtime-health.ts"

import {
  isDatabricksWorkspaceUrl,
  preferredPublicationConnection,
  publicationConnectionAuth,
  publicationConnectionEndpoint,
  publicationConnections,
} from "../../../frontend/src/components/model-management/model-publication-connections.ts"

const gateway = (id, overrides = {}) => ({ id, implementation: "apim", enabled: true, base_url: `https://${id}.azure-api.net/models`, ...overrides })
const provider = (id, overrides = {}) => ({ id, enabled: true, ...overrides })
const connection = (id, gatewayId, overrides = {}) => ({
  id,
  gateway_profile_id: gatewayId,
  provider_id: "provider-a",
  enabled: true,
  is_default: false,
  brand_key: "generic",
  config: { api_format: "openai_chat" },
  ...overrides,
})
const registry = (overrides = {}) => ({
  gateways: [gateway("gateway-a"), gateway("gateway-b")],
  providers: [provider("provider-a"), provider("provider-b")],
  runtimes: [
    connection("a-first", "gateway-a"),
    connection("a-default", "gateway-a", { is_default: true }),
    connection("a-other-provider", "gateway-a", { provider_id: "provider-b" }),
    connection("b-default", "gateway-b", { is_default: true }),
  ],
  models: [],
  ...overrides,
})

test("new Gateway form requires endpoint control and never sends a native ID", () => {
  const value = { display_name: " Edge ", endpoint_url: "https://edge.example/models",
    location: " ", hosting_mode: "docker", release_gate: "required", endpoint_control_confirmed: true }
  assert.equal(gatewayForms.gatewayCreateFormValid(value), true)
  assert.equal(gatewayForms.gatewayCreateFormValid({ ...value, endpoint_control_confirmed: false }), false)
  assert.equal(gatewayForms.gatewayCreateFormValid({ ...value, endpoint_url: "https://edge.example:8443/models" }), false)
  const payload = gatewayForms.gatewayCreateFormPayload({ ...value, native_gateway_id: "caller-injected" })
  assert.equal(payload.display_name, "Edge")
  assert.equal(payload.location, null)
  assert.equal(Object.hasOwn(payload, "native_gateway_id"), false)
})

test("Gateway creation rows own search and hand over without resurrecting deleted Gateways", () => {
  const target = { target: { gateway_profile_id: "target-a", service_name: "Service A", api_id: "api-a", resource_group: "group-a" }, deployments: [] }
  const operation = { id: "task-a", gateway_profile_id: "target-a", operation_kind: "self_hosted_gateway_provision", status: "queued", updated_at: "2026-09-26T10:00:00Z",
    semantic_preview: { deployment_id: "edge-a", native_gateway_id: "generated-native", configuration: { display_name: "Customer edge", endpoint_url: "https://edge.example/models", hosting_mode: "docker", location: "Office" } } }
  const rows = gatewayTopology.gatewayProvisionRows([operation, operation, { ...operation, id: "unrelated", operation_kind: "apim_onboarding" }], [target])
  assert.equal(rows.length, 1)
  for (const search of [" Customer ", "edge.example", "generated-native", "SERVICE A", "api-a", "office", "docker", "task-a"]) {
    assert.equal(gatewayTopology.gatewayProvisionMatches(rows[0], search), true)
  }
  assert.equal(gatewayTopology.gatewayProvisionMatches(rows[0], "unmatched"), false)
  assert.equal(gatewayTopology.gatewayProvisionRows([{ ...operation, status: "failed" }], [target]).length, 1)
  const succeeded = { ...operation, status: "succeeded", updated_at: "2026-09-26T10:01:00Z" }
  assert.equal(gatewayTopology.gatewayProvisionRows([succeeded, operation], [target]).length, 0)
  assert.equal(gatewayTopology.gatewayProvisionRows([succeeded], [{ ...target, deployments: [] }]).length, 0)
  assert.equal(gatewayTopology.gatewayProvisionRows([operation], []).length, 0)
  for (const deployment of [{ id: "edge-a", retired_at: "2026-09-26" }, { id: "edge-a", provisioning_operation_id: "task-a" }]) {
    assert.equal(gatewayTopology.gatewayProvisionRows([operation], [{ ...target, deployments: [{ deployment }] }]).length, 0)
  }
})

test("created Gateway stays waiting until a real saved connection probe exists", () => {
  const entry = { target: { target: { api_id: "api-a" } }, inventory: { associated_api_ids: ["api-a"] },
    deployment: { kind: "self_hosted", endpoint_url: "https://edge.example/models", declared_state: "present", reachability_state: "reachable",
      last_probe_at: null, provisioning_operation_id: "task-a" } }
  assert.equal(gatewayTopology.gatewayEvidence(entry).label, "待启动")
  entry.deployment.last_probe_at = "2026-09-26T10:00:00Z"
  assert.equal(gatewayTopology.gatewayEvidence(entry).state, "ready")
  entry.deployment.reachability_state = "unreachable"
  assert.equal(gatewayTopology.gatewayEvidence(entry).state, "unavailable")
  assert.equal(gatewayTopology.gatewayEvidence(entry).probedAt, entry.deployment.last_probe_at)
})

test("Gateway bootstrap only returns safe metadata and clears sensitive content on all outcomes", () => {
  const native = `turnstile-${"a".repeat(32)}`
  const bootstrap = () => ({ deployment_id: "deployment", native_gateway_id: native,
    configuration_endpoint: "https://unit.configuration.azure-api.net", expires_at: new Date(Date.now() + 3600000).toISOString(),
    image: "mcr.microsoft.com/azure-api-management/gateway:2.12.1", environment_file_name: `${native}.env`,
    environment_file: `config.service.auth=GatewayKey ${native}&unit-sensitive-token\n`,
    docker_command: `docker run --env-file ./${native}.env`, renewal_command: `docker stop ${native}` })
  const original = bootstrap(), downloads = []
  const metadata = consumeGatewayBootstrap(original, "deployment", (name, content) => downloads.push({ name, content }))
  assert.equal(downloads.length, 1)
  assert.match(downloads[0].content, /unit-sensitive-token/)
  assert.equal(original.environment_file, "")
  assert.equal(JSON.stringify(metadata).includes("unit-sensitive-token"), false)
  for (const mode of ["wrong-identity", "bad-image", "command-leak", "download-error"]) {
    const value = bootstrap()
    if (mode === "wrong-identity") value.deployment_id = "other"
    if (mode === "bad-image") value.image = "gateway:latest"
    if (mode === "command-leak") value.docker_command = value.environment_file
    assert.throws(() => consumeGatewayBootstrap(value, "deployment", () => { throw new Error("download failed") }))
    assert.equal(value.environment_file, "")
  }
})

test("Gateway environment download revokes its Blob URL even when click fails", () => {
  const previousDocument = globalThis.document, previousCreate = URL.createObjectURL, previousRevoke = URL.revokeObjectURL
  const revoked = [], removed = [], hrefs = []
  URL.createObjectURL = () => "blob:opaque-download"
  URL.revokeObjectURL = url => revoked.push(url)
  globalThis.document = { createElement: () => ({ set href(value) { hrefs.push(value) },
    click() { throw new Error("download failed") }, removeAttribute: name => removed.push(name), remove() {} }) }
  try {
    assert.throws(() => downloadGatewayEnvironment("unit.env", "unit-sensitive-token"))
    assert.deepEqual(revoked, ["blob:opaque-download"])
    assert.deepEqual(removed, ["href"])
    assert.deepEqual(hrefs, ["blob:opaque-download"])
  } finally {
    globalThis.document = previousDocument
    URL.createObjectURL = previousCreate
    URL.revokeObjectURL = previousRevoke
  }
})

const healthNow = Date.parse("2026-09-24T01:00:00Z")
const healthRuntime = overrides => ({ id: "connection-a", enabled: true, health_status: "unknown",
  health_message: null, last_checked_at: null, ...overrides })

test("Connection filter triggers follow selected content without fixed or stretched widths", () => {
  const styles = readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8")
  const sizing = styles.match(/\.connection-provider-filter,\s*\.connection-health-filter\s*\{([^}]+)\}/)?.[1] ?? ""
  assert.match(sizing, /width:\s*fit-content;/)
  assert.match(sizing, /max-width:\s*100%;/)
  assert.match(sizing, /min-width:\s*0;/)
  assert.match(sizing, /flex:\s*0 1 auto;/)
  const layout = styles.match(/\.model-toolbar-filters:has\(> \.connection-health-filter\)\s*\{([^}]+)\}/)?.[1] ?? ""
  assert.match(layout, /display:\s*flex;/)
  assert.match(layout, /flex-wrap:\s*wrap;/)
  assert.equal(/\.connection-(?:provider|health)-filter\s*\{[^}]*?(?<![-\w])width:\s*(?:\d+px|100%);/.test(styles), false)
})

for (const [status, error] of [[403, "model_not_assigned"], [503, '{"code":"budget_ledger_unavailable"}'], [429, "rate_limit_exceeded"]]) {
  test(`historical health HTTP ${status} remains blocked evidence, never offline`, () => {
    const record = { request_source: "runtime-health-check", timestamp: "2026-09-24T00:59:00Z",
      status_code: status, error_message: error, request_id: "request", correlation_id: "attempt", latency_ms: 12 }
    const actual = runtimeWithTelemetryHealth(healthRuntime(), [record])
    assert.equal(runtimeHealthState(actual, healthNow), "blocked")
    assert.match(runtimeHealthDescription(actual), /未确认连接状态/)
    assert.equal(runtimeHealthState(actual, healthNow + 86_400_000), "blocked")
    assert.equal(record.error_message, error)
  })
}
test("runtime health preserves the last scoped result without treating it as live health", () => {
  const diagnostic = { runtime_id: "connection-a", status: "available", outcome: "passed",
    check_kind: "model_inference", checked_at: "2026-09-24T00:59:00Z", valid_until: "2026-09-24T01:04:00Z",
    selection: { model_name: "One model", gateway_name: "Selected Gateway", native_gateway_id: "managed" } }
  const runtime = healthRuntime({ last_checked_at: diagnostic.checked_at,
    health_message: runtimeDiagnosticPrefix + JSON.stringify(diagnostic) })
  assert.equal(runtimeHealthState(runtime, healthNow), "online")
  assert.match(runtimeHealthDescription(runtime), /One model/)
  assert.match(runtimeHealthDescription(runtime), /Selected Gateway/)
  assert.equal(runtimeHealthState(runtime, healthNow + 86_400_000), "online")
  assert.equal(runtimeHealth.runtimeHealthLabels.online, "上次检查通过")
  assert.equal(runtimeHealthState({ ...runtime, enabled: false }, healthNow), "disabled")
  assert.equal(runtimeHealthState(healthRuntime({ health_status: "available", last_checked_at: diagnostic.checked_at }), healthNow), "inconclusive")
  assert.equal(runtimeHealthState(healthRuntime({ health_message: runtimeDiagnosticPrefix + "invalid" }), healthNow), "unchecked")
})

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const queryLibrary = frontendRequire("@tanstack/react-query")
const postcss = frontendRequire("postcss")

test("diagnostic states use consistent semantic colors across status surfaces", () => {
  const sheet = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const declaration = (selector, property) => {
    let value
    sheet.walkRules(rule => {
      if (!rule.selectors.includes(selector)) return
      rule.walkDecls(property, item => { value = item.value })
    })
    return value
  }
  for (const [state, token] of [
    ["checking", "--brand"], ["offline", "--destructive"], ["blocked", "--destructive"],
    ["configuration", "--destructive"], ["online", "--success"], ["changed", "--warning"],
  ]) {
    for (const selector of [`.machine-health.${state}`, `.runtime-health-badge.${state}`,
      `.runtime-diagnostic-result[data-state="${state}"]`]) {
      assert.equal(declaration(selector, "color"), `var(${token})`, selector)
    }
    assert.equal(declaration(`.connection-health-option i.${state}`, "background"), `var(${token})`)
    assert.equal(declaration(`.runtime-health-badge.${state} i`, "background"), state === "online" ? "var(--success)" : "currentColor")
  }
  for (const selector of [".machine-health", ".runtime-health-badge", ".machine-health.unchecked", ".machine-health.disabled"]) {
    assert.equal(declaration(selector, "color"), "var(--muted-foreground)")
  }
  for (const selector of [".connection-health-action .spin", ".diagnostic-action .spin"]) {
    assert.equal(declaration(selector, "color"), "var(--brand)")
  }
  const shared = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/finops.css", import.meta.url), "utf8"))
  const pending = shared.nodes.find(rule => rule.type === "rule" && rule.selector === ".connection-pending-status")
  assert.equal(pending.nodes.find(item => item.prop === "color").value, "var(--warning)")
})

test("selects and filter dropdowns share one compact theme contract", () => {
  const sheet = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/finops.css", import.meta.url), "utf8"))
  const rule = sheet.nodes.find(node => node.type === "rule" && node.selectors.includes('[data-slot="select-trigger"]'))
  assert.ok(rule)
  for (const selector of [".model-filter-trigger", ".finops-filter-menu-trigger", ".people-model-select-trigger"]) {
    assert.ok(rule.selectors.includes(selector))
  }
  const declarations = Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))
  const expected = {
    height: "28px", "font-size": "12px", "font-weight": "500", border: "1px solid var(--border)",
    "border-radius": "7px", padding: "0 10px", background: "var(--background)", color: "var(--muted-foreground)",
  }
  assert.deepEqual(Object.fromEntries(Object.keys(expected).map(name => [name, declarations[name]])), expected)
  const focus = sheet.nodes.find(node => node.type === "rule" && node.selector.startsWith(':is([data-slot="select-trigger"]') && node.selector.endsWith(":focus-visible"))
  assert.ok(focus.selector.includes(".model-filter-trigger"))
  const popup = sheet.nodes.find(node => node.type === "rule" && node.selector === '[data-slot="select-content"]')
  assert.equal(popup.nodes.find(node => node.prop === "border-radius").value, "var(--radius)")
  assert.ok(sheet.nodes.some(node => node.type === "rule" && node.selector === '[data-slot="select-trigger"][aria-invalid="true"]'))
})

test("infrastructure action menus inherit the filter surface without a second border", () => {
  const sheet = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const rule = sheet.nodes.find(node => node.type === "rule" && node.selector === ".infrastructure-row-menu")
  assert.ok(rule)
  assert.deepEqual(Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value])), {
    "min-width": "188px",
  })
  const shared = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/finops.css", import.meta.url), "utf8"))
    .nodes.find(node => node.type === "rule" && node.selector === '[data-slot="dropdown-menu-content"]')
  const reference = sheet.nodes.find(node => node.type === "rule" && node.selectors.includes(".model-filter-menu"))
  const declarations = rule => Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))
  for (const property of ["outline", "box-shadow", "border-radius", "background", "color", "padding"]) {
    assert.equal(declarations(shared)[property], declarations(reference)[property])
  }
})

test("select page layouts do not override shared filter colors or density", () => {
  const layoutSelectors = [".settings-section .settings-select-trigger", ".dimension-picker .dimension-select-trigger",
    '.router-field [data-slot="select-trigger"]', '.router-rule-row [data-slot="select-trigger"]']
  const forbidden = new Set(["height", "min-height", "font-size", "font-weight", "color", "background", "border-color", "border-radius"])
  for (const name of ["base.css", "model-platform.css"]) {
    const sheet = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/" + name, import.meta.url), "utf8"))
    sheet.walkRules(rule => {
      if (!rule.selectors.some(selector => layoutSelectors.includes(selector))) return
      assert.deepEqual(rule.nodes.filter(node => node.type === "decl" && forbidden.has(node.prop)).map(node => node.prop), [])
    })
  }
})

async function bundleModule(relative, define = {}) {
  const entry = fileURLToPath(new URL(relative, import.meta.url))
  const bundle = await rolldown({ input: entry, external: name => name !== entry, transform: { define, jsx: { runtime: "automatic" } }, treeshake: false })
  try { return (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
}

const apiModule = await bundleModule("../../../frontend/src/data-sources/apim/api.ts")
const apiClientModule = await bundleModule("../../../frontend/src/api/client.ts", { "import.meta.env": "{}" })
const queriesModule = await bundleModule("../../../frontend/src/data-sources/apim/queries.ts")
const workspaceModule = await bundleModule("../../../frontend/src/components/model-management/apim-gateway-workspace.tsx")
const gatewayDialogModule = await bundleModule("../../../frontend/src/components/model-management/self-hosted-gateway-dialog.tsx")
const diagnosticModule = await bundleModule("../../../frontend/src/components/model-management/runtime-diagnostic-dialog.tsx")
const availabilityModule = await bundleModule("../../../frontend/src/components/model-management/model-availability.tsx")
const releasesModule = await bundleModule("../../../frontend/src/pages/gateway-releases-page.tsx")
const modelPageModule = await bundleModule("../../../frontend/src/pages/model-management-page.tsx")
const modelDetailModule = await bundleModule("../../../frontend/src/components/model-management/model-usage-detail.tsx")
const modelDetailExports = {}
const modelDetailNavigation = { pushed: [], events: [] }
runInNewContext(modelDetailModule, {
  exports: modelDetailExports,
  URL,
  Event,
  window: {
    location: { href: "https://unit.example/?source=apim&page=models&tab=models" },
    history: { pushState: (_state, _title, href) => modelDetailNavigation.pushed.push(href) },
    dispatchEvent: event => modelDetailNavigation.events.push(event.type),
  },
  require: name => ["react", "react/jsx-runtime", "lucide-react"].includes(name) ? frontendRequire(name)
    : name === "../../lib/navigation" ? { FINOPS_NAVIGATE_EVENT: "finops:navigate" }
      : new Proxy({}, { get: (_target, key) => key }),
})
const modelPageComponents = {}
runInNewContext(modelPageModule + "\nexports.PublicationModelRow = PublicationModelRow; exports.ConnectionRow = ConnectionRow; exports.ModelDeleteDialog = ModelDeleteDialog; exports.PublicationArchiveDialog = PublicationArchiveDialog", {
  exports: modelPageComponents,
  URL,
  window: { location: { href: "https://unit.example/?source=apim&page=models&tab=connections" } },
  require: name => name === "react/jsx-runtime" || name === "lucide-react" ? frontendRequire(name)
    : name === "../lib/gateway-topology" ? gatewayTopology
      : name === "../lib/runtime-health" ? runtimeHealth
        : name.endsWith("model-publication-dialog") ? { publicationStatusLabel: status => status }
      : name === "../components/brand-logos" ? { providerBrandFromIdentity: () => "generic", gatewayBrandFromIdentity: () => "generic" }
      : new Proxy({}, { get: (_target, key) => key }),
})

for (const status of ["failed", "queued", "verifying", "promoting", "active"]) {
  test(`publication row offers removal only for failed state: ${status}`, () => {
    const publication = { id: "unit-publication", status, gateway_profile_id: "unit-target",
      display_name: "Unit model", model_key: "unit-model" }
    let selected = null
    const tree = modelPageComponents.PublicationModelRow({ publication,
      registry: { gateways: [] }, topology: null, onOpen() {}, onArchive: item => { selected = item } })
    const markup = frontendRequire("react-dom/server").renderToStaticMarkup(tree)
    assert.equal(markup.includes("移除失败记录"), status === "failed")
    if (status === "failed") {
      const actions = tree.props.children.at(-1).props.children
      const remove = actions.find(child => child?.props?.title === "移除失败记录")
      assert.ok(remove)
      remove.props.onClick()
      assert.equal(selected, publication)
    }
  })
}

for (const [dialog, item] of [
  ["ModelDeleteDialog", { model: { id: "unit-model", display_name: "Unit model" } }],
  ["PublicationArchiveDialog", { publication: { id: "unit-publication", display_name: "Unit model" } }],
]) {
  for (const busy of [false, true]) {
    test(`${dialog} shows a disabled spinning action only while submitting: ${busy}`, () => {
      const tree = modelPageComponents[dialog]({ ...item, busy, error: null, onClose() {}, onConfirm() {} })
      const footer = tree.props.children.props.children.at(-1)
      const action = footer.props.children.at(-1)
      const children = Array.isArray(action.props.children) ? action.props.children : [action.props.children]
      const spinner = children.find(child => child?.props?.className === "spin")
      assert.equal(action.props.disabled, busy)
      assert.equal(action.props["aria-busy"], busy)
      assert.equal(Boolean(spinner), busy)
      if (busy) {
        assert.equal(spinner.props.size, 14)
        assert.equal(spinner.props["aria-hidden"], "true")
        assert.ok(children.includes("正在提交"))
      }
    })
  }
}

test("archive success removes cached rows before closing and cancels stale list reads", async () => {
  const { parse } = await import(frontendRequire.resolve("rolldown/utils"))
  const source = readFileSync(
    new URL("../../../frontend/src/pages/model-management-page.tsx", import.meta.url), "utf8",
  )
  const { program } = await parse("model-management-page.tsx", source)
  let callback
  const visit = node => {
    if (!node || typeof node !== "object") return
    if (node.type === "VariableDeclarator" && node.id?.name === "archivePublication") {
      callback = node.init.arguments[0].properties.find(property => property.key?.name === "onSuccess").value
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(visit)
      else if (value?.type) visit(value)
    }
  }
  visit(program)
  assert.ok(callback)
  const { QueryClient } = frontendRequire("@tanstack/react-query")
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const keys = {
    gatewayPublications: ["control-plane", "gateway-publications"],
    gatewayPublication: id => ["control-plane", "gateway-publication", id],
  }
  const removed = { id: "failed-record", status: "failed" }
  const kept = { id: "unrelated-record", status: "active" }
  const globalKey = [...keys.gatewayPublications, null]
  const targetKey = [...keys.gatewayPublications, "unit-target"]
  const before = { items: [removed, kept], marker: "preserved" }
  client.setQueryData(globalKey, before)
  client.setQueryData(targetKey, before)
  client.setQueryData(keys.gatewayPublication(removed.id), removed)
  let finishRead
  const pendingRead = client.fetchQuery({ queryKey: targetKey, queryFn: () => new Promise(resolve => {
    finishRead = resolve
  }) }).catch(() => undefined)
  let closed = false
  let notice
  client.invalidateQueries = () => new Promise(() => {})
  const code = stripTypeScriptTypes(`exports.success = (${source.slice(callback.start, callback.end)})`)
  const exports = {}
  runInNewContext(code, {
    exports, client, finopsKeys: keys, publicationId: null,
    setArchivingPublication: value => {
      assert.equal(value, null)
      assert.deepEqual(client.getQueryData(globalKey).items.map(item => item.id), [kept.id])
      assert.deepEqual(client.getQueryData(targetKey).items.map(item => item.id), [kept.id])
      closed = true
    },
    setNotice: value => { notice = value },
  })
  try {
    await exports.success(undefined, removed)
    assert.equal(closed, true)
    assert.equal(notice.ok, true)
    assert.equal(client.getQueryData(globalKey).marker, "preserved")
    finishRead(before)
    await pendingRead
    assert.deepEqual(client.getQueryData(targetKey).items.map(item => item.id), [kept.id])
  } finally {
    client.clear()
  }
})

for (const status of ["promoting", "failed", "active"]) {
  test(`model removal updates cached rows only after confirmed completion: ${status}`, async () => {
    const { parse } = await import(frontendRequire.resolve("rolldown/utils"))
    const source = readFileSync(new URL("../../../frontend/src/pages/model-management-page.tsx", import.meta.url), "utf8")
    const { program } = await parse("model-management-page.tsx", source)
    let callback
    const visit = node => {
      if (!node || typeof node !== "object") return
      if (node.type === "CallExpression" && node.callee?.name === "useEffect") {
        const candidate = node.arguments[0]
        if (candidate?.body?.body?.some(statement => statement.declarations?.some(declaration =>
          declaration.id?.name === "publication" && declaration.init?.object?.name === "trackedPublication"))) callback = candidate
      }
      for (const value of Object.values(node)) {
        if (Array.isArray(value)) value.forEach(visit)
        else if (value?.type) visit(value)
      }
    }
    visit(program)
    assert.ok(callback)
    const { QueryClient } = frontendRequire("@tanstack/react-query")
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const keys = { registry: ["reference", "model-registry"], gatewayPublications: ["control-plane", "gateway-publications"] }
    const before = { models: [{ id: "removed-model", model_key: "removed-key" }, { id: "kept-model", model_key: "kept-key" }], marker: "preserved" }
    client.setQueryData(keys.registry, before)
    let finishRead
    const pendingRead = client.fetchQuery({ queryKey: keys.registry, queryFn: () => new Promise(resolve => { finishRead = resolve }) }).catch(() => undefined)
    client.invalidateQueries = () => Promise.resolve()
    const exports = {}
    runInNewContext(stripTypeScriptTypes(`exports.effect = (${source.slice(callback.start, callback.end)})`), {
      exports, client, finopsKeys: keys, assistantSettingsKey: ["assistant-settings"], setNotice() {},
      trackedPublication: { data: { id: "removal", status, publication_kind: "model_remove", model_key: "removed-key", display_name: "Unit model" } },
    })
    const expected = status === "active" ? ["kept-model"] : ["removed-model", "kept-model"]
    try {
      exports.effect()
      assert.deepEqual(client.getQueryData(keys.registry).models.map(model => model.id), expected)
      assert.equal(client.getQueryData(keys.registry).marker, "preserved")
      finishRead(before)
      await pendingRead
      assert.deepEqual(client.getQueryData(keys.registry).models.map(model => model.id), expected)
    } finally { client.clear() }
  })
}

test("failed model removal preserves verified row state without hiding the failed task", () => {
  const source = readFileSync(new URL("../../../frontend/src/pages/model-management-page.tsx", import.meta.url), "utf8")
  const selection = source.match(/  const latestPublicationByModel = ([\s\S]+?)\n  const rowStatuses/)
  assert.ok(selection)
  const tasks = [
    { id: "removal-failed", publication_kind: "model_remove", status: "failed", gateway_profile_id: "target-a", model_key: "model-a", updated_at: "2026-10-06T05:25:00Z" },
    { id: "addition-active", publication_kind: "model_add", status: "active", gateway_profile_id: "target-a", model_key: "model-a", updated_at: "2026-10-05T05:25:00Z" },
    { id: "removal-pending", publication_kind: "model_remove", status: "promoting", gateway_profile_id: "target-b", model_key: "model-b", updated_at: "2026-10-06T05:25:00Z" },
    { id: "addition-failed", publication_kind: "model_add", status: "failed", gateway_profile_id: "target-a", model_key: "model-c", updated_at: "2026-10-06T05:25:00Z" },
  ]
  const before = JSON.stringify(tasks)
  const exports = {}
  runInNewContext(stripTypeScriptTypes(`const latestPublicationByModel = ${selection[1]}\nexports.rows = latestPublicationByModel`), { exports, publications: tasks })
  assert.equal(exports.rows.get("target-a:model-a").id, "addition-active")
  assert.equal(exports.rows.get("target-b:model-b").id, "removal-pending")
  assert.equal(exports.rows.get("target-a:model-c").id, "addition-failed")
  assert.equal(JSON.stringify(tasks), before)
})

test("model deletion dialogs keep eight pixels between their title and description", () => {
  const sheet = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  for (const selector of ['.model-delete-body [data-slot="alert-dialog-description"]', '.publication-archive-dialog [data-slot="alert-dialog-description"]']) {
    const rule = sheet.nodes.find(node => node.type === "rule" && node.selector === selector)
    assert.equal(rule.nodes.find(node => node.prop === "margin-top").value, "8px")
  }
})

test("Foundry model-router and Decision use their existing official icons without changing model behavior", () => {
  const components = {}
  const foundryLogo = () => null
  const microsoftLogo = () => null
  const action = { submitting: false, inFlight: false, error: null, change() {} }
  runInNewContext(modelPageModule + "\nexports.ModelRow = ModelRow", {
    exports: components,
    require: name => name === "react/jsx-runtime" || name === "lucide-react" ? frontendRequire(name)
      : name === "../components/brand-logos" ? { FoundryLogo: foundryLogo, MicrosoftLogo: microsoftLogo,
        providerBrandFromIdentity: () => "generic", gatewayBrandFromIdentity: () => "generic" }
        : name === "../components/model-management/model-usage-detail" ? modelDetailExports
        : name === "../lib/model-inventory-filters" ? { modelInventoryFamily: model => model.family_key }
          : name === "../components/model-management/model-availability" ? {
            useModelAvailabilityCommand: () => action,
            ModelAvailabilitySwitch: "ModelAvailabilitySwitch",
            ModelAvailabilityStatus: "ModelAvailabilityStatus",
          } : new Proxy({}, { get: (_target, key) => key }),
  })
  for (const [runtimeKind, upstream, displayName, expectedFoundry, capabilities = ["chat"], expectedMicrosoft = false] of [
    ["foundry", "model-router", "model-router", true],
    ["foundry", "model-router", "Renamed deployment", true],
    ["foundry", "other-model", "model-router", false],
    ["foundry", null, "model-router", false],
    ["direct", "model-router", "model-router", false],
    [null, "model-router", "model-router", false],
    ["foundry", "Decision-1", "Customer triage", false, ["decision"], true],
    ["foundry", "other-model", "Decision-1", false],
  ]) {
    const model = { id: "unit-model", model_key: "custom-alias", display_name: displayName,
      upstream_model_id: upstream, family_key: "generic", runtime_name: "Unit runtime",
      provider_name: "Unit provider", capabilities, enabled: true, is_default: false }
    const original = JSON.stringify(model)
    const availability = { model_id: model.id, desired_enabled: true, command_ready: true }
    const runtime = runtimeKind ? { runtime_kind: runtimeKind } : undefined
    const row = components.ModelRow({ model, runtime,
      availability, busy: false, removing: false, publication: null, canRotateCredential: false,
      deleteDisabledReason: null })
    const icon = row.props.children[0].props.children[0]
    assert.equal(icon.props.children.type === foundryLogo, expectedFoundry)
    assert.equal(icon.props.children.type === microsoftLogo, expectedMicrosoft)
    assert.equal(icon.props.title, expectedMicrosoft ? "Microsoft" : expectedFoundry ? "Microsoft Foundry" : "Model")
    assert.equal(icon.props["data-model-family"], "generic")
    if (expectedFoundry || expectedMicrosoft) assert.equal(icon.props.children.props.size, 16)
    const runtimeCell = row.props.children[1]
    assert.equal(runtimeCell.props.className, "model-runtime-cell model-runtime-identity")
    const [runtimeIcon, copy] = runtimeCell.props.children
    assert.equal(runtimeIcon.props.className, "model-list-icon")
    assert.equal(runtimeIcon.props["aria-hidden"], "true")
    assert.equal(runtimeIcon.props.children.props.size, 16)
    if (runtime) {
      assert.equal(runtimeIcon.props.children.type.name, "RuntimeLogo")
      assert.equal(runtimeIcon.props.children.props.runtime, runtime)
    } else assert.equal(runtimeIcon.props.children.type, frontendRequire("lucide-react").Cpu)
    assert.equal(copy.props.className, "model-runtime-copy")
    assert.equal(copy.props.children[0].props.children, model.runtime_name)
    assert.equal(copy.props.children[0].props.title, model.runtime_name)
    assert.equal(copy.props.children[1].props.children, model.provider_name)
    const toggle = row.props.children[5].props.children.at(-1)
    assert.equal(toggle.props.readback, availability)
    assert.equal(toggle.props.onChange, action.change)
    assert.equal(toggle.props.disabled, false)
    assert.equal(JSON.stringify(model), original)
  }
})

test("model detail links clear unrelated routes and retain a real encoded model identity", () => {
  const href = modelDetailExports.modelDetailHref("model with / ?", "https://unit.example/?source=github-copilot&page=models&tab=connections&runtime=other&publication=other&release=other&consumer=applications&application=other&request=other")
  const address = new URL(href, "https://unit.example")
  assert.equal(address.origin, "https://unit.example")
  assert.deepEqual(Object.fromEntries(address.searchParams), {
    source: "apim", page: "models", tab: "models", model: "model with / ?",
  })
  const back = modelDetailExports.modelDetailHref(null, address.href)
  assert.equal(new URL(back, address).searchParams.has("model"), false)
})

test("model detail navigation preserves modified clicks and native browser history", () => {
  modelDetailNavigation.pushed.length = 0
  modelDetailNavigation.events.length = 0
  let prevented = 0
  const event = { button: 0, currentTarget: { href: "https://unit.example/?page=models&model=unit" },
    preventDefault() { prevented += 1 } }
  for (const override of [{ metaKey: true }, { ctrlKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }, { defaultPrevented: true }]) {
    modelDetailExports.openModelDetailLink({ ...event, ...override })
  }
  assert.equal(prevented, 0)
  assert.equal(modelDetailNavigation.pushed.length, 0)
  modelDetailExports.openModelDetailLink(event)
  assert.equal(prevented, 1)
  assert.deepEqual(modelDetailNavigation.pushed, [event.currentTarget.href])
  assert.deepEqual(modelDetailNavigation.events, ["finops:navigate"])
})

test("model usage ranks server totals without merging duplicate display names or mutating inputs", () => {
  const items = [{ id: "second", name: "Same name", total_tokens: 20 },
    { id: "first", name: "Same name", total_tokens: 80 },
    ...Array.from({ length: 12 }, (_, index) => ({ id: `other-${index}`, name: `Other ${index}`, total_tokens: index }))]
  const before = JSON.stringify(items)
  const rows = modelDetailExports.modelUsageRankingRows(items)
  assert.equal(rows.length, 10)
  assert.deepEqual(Array.from(rows.slice(0, 2), row => row.id), ["first", "second"])
  assert.equal(rows[0].name, rows[1].name)
  assert.deepEqual(Array.from(rows, row => row.total_tokens), [80, 20, 11, 10, 9, 8, 7, 6, 5, 4])
  assert.equal(JSON.stringify(items), before)
  assert.equal(modelDetailExports.modelUsageRankingRows([]).length, 0)
})

test("model dimension selector renders one reader and preserves selection across date changes", () => {
  let selected = "department"
  const components = {}
  runInNewContext(modelDetailModule + "\nexports.ModelDimensionRanking = ModelDimensionRanking", {
    exports: components,
    require: name => name === "react" ? { useState: () => [selected, value => { selected = value }] }
      : ["react/jsx-runtime", "lucide-react"].includes(name) ? frontendRequire(name)
        : new Proxy({}, { get: (_target, key) => key }),
  })
  const filters = { model_id: "unit-model", from: "2026-09-01T00:00:00Z", to: "2026-09-08T00:00:00Z" }
  const render = days => components.ModelDimensionRanking({ filters, days })
  let view = render(30)
  assert.equal(view.type.name, "ModelUsageRanking")
  for (const dimension of ["department", "project", "agent", "organization"]) {
    const group = view.props.controls.props.children
    assert.equal(group.props.className, "trend-segment model-dimension-switch")
    assert.equal(group.props.children.length, 4)
    group.props.children.find(button => button.props["data-dimension-option"] === dimension).props.onClick()
    view = render(30)
    assert.equal(view.props.dimension, dimension)
    assert.equal(view.props.filters, filters)
    assert.equal(view.props.chartHeight, 382)
    assert.equal(view.props.controls.props.children.props.children.filter(button => button.props["aria-pressed"]).length, 1)
  }
  view = render(7)
  assert.equal(view.props.dimension, "organization")
  assert.equal(view.props.days, 7)
})

test("unknown model detail cannot mount usage readers or fall back to all models", () => {
  const view = modelDetailExports.ModelUsageDetail({ modelId: "missing", registry: registry(), onToggleSidebar() {} })
  const state = view.props.children[1]
  assert.equal(state.type, "div")
  assert.equal(state.props.className, "smh-empty application-detail-error")
  assert.equal(state.props.children[1].props.children, "模型不存在")
})

for (const state of ["loading", "failed", "empty"]) test(`model usage ranking keeps ${state} separate from measured zero`, () => {
  const components = {}, scopes = []
  let retries = 0
  const query = { isLoading: state === "loading", error: state === "failed" ? new Error("unit read failed") : null,
    data: { items: state === "failed" ? [{ id: "stale", name: "Stale", total_tokens: 80 }] : [] }, refetch() { retries += 1 } }
  runInNewContext(modelDetailModule + "\nexports.ModelUsageRanking = ModelUsageRanking", {
    exports: components,
    require: name => ["react", "react/jsx-runtime", "lucide-react"].includes(name) ? frontendRequire(name)
      : name === "@tanstack/react-query" ? { useQuery: () => query }
        : name === "../../data-sources/apim/queries" ? { finopsQueries: { distribution: (...args) => { scopes.push(args); return {} } } }
          : name === "../finops/category-axis" ? { useChartWidthKey: () => [null, 320], categoryAxisWidth: () => 100, categoryTickGutter: 10 }
            : new Proxy({}, { get: (_target, key) => key }),
  })
  const filters = { from: "2026-09-01T00:00:00Z", to: "2026-09-08T00:00:00Z", model_id: "unit-model" }
  const view = components.ModelUsageRanking({ filters, days: 7, dimension: "user", title: "Usage", icon: "Icon", color: "var(--chart-1)" })
  assert.equal(scopes[0][0], filters)
  assert.equal(scopes[0][1], "user")
  assert.equal(scopes[0][3], "fixed")
  const content = view.props.children[1]
  if (state === "empty") assert.equal(content.props.children, "所选范围暂无调用")
  else {
    assert.equal(content.type.name, "ReadState")
    const rendered = content.type(content.props)
    assert.equal(rendered.props.role, state === "failed" ? "alert" : "status")
    if (state === "failed") { content.props.onRetry(); assert.equal(retries, 1) }
  }
})

test("runtime brand column keeps a fixed icon beside shrinkable two-line text", () => {
  const sheet = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const declarations = selector => {
    const rule = sheet.nodes.find(node => node.type === "rule" && node.selector === selector)
    assert.ok(rule, selector)
    return Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))
  }
  const identity = declarations(".model-runtime-cell.model-runtime-identity")
  assert.equal(identity["grid-template-columns"], "32px minmax(0, 1fr)")
  assert.equal(identity["align-items"], "center")
  assert.equal(identity.gap, "8px")
  const copy = declarations(".model-runtime-copy")
  assert.equal(copy["min-width"], "0")
  assert.equal(copy.display, "grid")
  assert.equal(copy.gap, "2px")
})

test("model Gateway column uses exact published model identities and stays compact", () => {
  const profiles = [{ id: "target-a", name: "Selected APIM" }, { id: "target-b", name: "Other APIM" }]
  const entry = (id, kind, models, parent = "target-a", retired = null) => ({ deployment: {
    id, apim_target_id: parent, native_gateway_id: kind === "managed" ? "managed" : id, kind,
    display_name: kind === "managed" ? "Azure-managed Gateway" : "Selected VM", endpoint_url: "https://example.test/models",
    release_gate: "required", retired_at: retired,
  }, published_models: models.map(modelId => ({ id: modelId })) })
  const topology = { items: [{ target: { gateway_profile_id: "target-a", service_name: "service-a" }, deployments: [
    entry("managed-a", "managed", ["managed-model"]), entry("edge-a", "self_hosted", ["edge-model"]),
    entry("retired-a", "self_hosted", ["edge-model"], "target-a", "2026-09-25"),
  ] }, { target: { gateway_profile_id: "target-b", service_name: "service-b" }, deployments: [entry("foreign", "self_hosted", ["edge-model"], "target-b")] }] }
  const runtime = { gateway_profile_id: "target-a" }
  const selected = gatewayTopology.modelTargetDetails({ id: "edge-model" }, runtime, profiles, topology)
  assert.deepEqual(Array.from(selected.gateways, item => item.id), ["edge-a"])
  const markup = frontendRequire("react-dom/server").renderToStaticMarkup(modelPageComponents.ModelTargetGatewayCell({ target: selected }))
  assert.match(markup, /model-target-cell/)
  assert.match(markup, /Selected VM/)
  assert.match(markup, /自托管/)
  assert.match(markup, /Selected APIM/)
  assert.equal(markup.includes("<dl"), false)
  assert.equal(markup.includes("Other APIM"), false)
  const managed = gatewayTopology.modelTargetDetails({ id: "managed-model" }, runtime, profiles, topology)
  assert.deepEqual(Array.from(managed.gateways, item => item.id), ["managed-a"])
  for (const value of [null, { items: [] }]) {
    const unknown = gatewayTopology.modelTargetDetails({ id: "edge-model" }, runtime, profiles, value)
    assert.equal(unknown.gateways.length, 0)
    assert.equal(unknown.apimName, "Selected APIM")
  }
  topology.items[0].deployments[1].published_models_available = false
  assert.equal(gatewayTopology.modelTargetDetails({ id: "edge-model" }, runtime, profiles, topology).gateways.length, 0)
})

test("Connection targets show only its models' published Gateways, not every Gateway in the APIM", () => {
  const profiles = [{ id: "target-a", name: "Selected APIM" }, { id: "target-b", name: "Other APIM" }]
  const runtime = { id: "connection-a", gateway_profile_id: "target-a" }
  const models = [{ id: "first", runtime_id: runtime.id }, { id: "second", runtime_id: runtime.id },
    { id: "foreign", runtime_id: "connection-b" }]
  const entry = (id, kind, published, targetId = "target-a") => ({ deployment: {
    id, apim_target_id: targetId, native_gateway_id: kind === "managed" ? "managed" : id,
    kind, display_name: id, retired_at: null,
  }, published_models: published.map(id => ({ id })) })
  const current = { target: { gateway_profile_id: "target-a", service_name: "service-a" }, deployments: [
    entry("managed-a", "managed", ["first"]), entry("edge-a", "self_hosted", ["first", "second"]),
    entry("unused", "self_hosted", []), entry("foreign-model", "self_hosted", ["foreign"]),
    { ...entry("retired", "self_hosted", ["first"]), deployment: {
      ...entry("retired", "self_hosted", []).deployment, retired_at: "2026-09-26",
    } },
  ] }
  const topology = { items: [current, { target: { gateway_profile_id: "target-b", service_name: "service-b" },
    deployments: [entry("foreign-target", "self_hosted", ["first"], "target-b")] }] }
  const before = JSON.stringify(topology)
  const actual = gatewayTopology.connectionTargetDetails(runtime, models, profiles, topology)
  assert.deepEqual(actual.gateways.map(item => item.id), ["managed-a", "edge-a"])
  assert.deepEqual(actual.gatewayModelCounts, { "managed-a": 1, "edge-a": 2 })
  assert.deepEqual(actual.gatewayModelIds, { "managed-a": ["first"], "edge-a": ["first", "second"] })
  assert.equal(actual.apimName, "Selected APIM")
  assert.equal(actual.modelCount, 2)
  assert.equal(actual.unrecordedModelCount, 0)
  assert.equal(JSON.stringify(topology), before)
  const render = (models, topology) => frontendRequire("react-dom/server").renderToStaticMarkup(
    modelPageComponents.ConnectionTargetGatewayCell({ runtime, models, profiles, topology }),
  )
  const markup = render(models, topology)
  assert.match(markup, /data-deployment-id="managed-a"/)
  assert.match(markup, /Selected APIM/)
  assert.doesNotMatch(markup, /data-deployment-id="edge-a"/)
  assert.doesNotMatch(markup, /经网关接入|foreign-target|foreign-model|retired/)
  current.deployments[1].published_models_available = false
  const partial = gatewayTopology.connectionTargetDetails(runtime, models, profiles, topology)
  assert.deepEqual(partial.gateways.map(item => item.id), ["managed-a"])
  assert.equal(partial.unrecordedModelCount, 1)
  assert.match(render(models, topology), /网关未记录/)
  const unknown = gatewayTopology.connectionTargetDetails(runtime, models, profiles, null)
  assert.equal(unknown.gateways.length, 0)
  assert.equal(unknown.unrecordedModelCount, 2)
  assert.equal(unknown.apimName, "Selected APIM")
  assert.match(render(models, null), /网关未记录/)
  const empty = gatewayTopology.connectionTargetDetails(runtime, [], profiles, topology)
  assert.equal(empty.gateways.length, 0)
  assert.equal(empty.modelCount, 0)
  assert.match(render([], topology), /尚未发布模型/)
})

const publicationDialogModule = await bundleModule("../../../frontend/src/components/model-management/model-publication-dialog.tsx")
test("Decision model purpose is selectable but cannot submit through the unsupported gateway", async () => {
  const source = readFileSync(new URL("../../../frontend/src/components/model-management/model-publication-dialog.tsx", import.meta.url), "utf8")
  assert.match(source, /<SelectItem value="decision">决策（Decision）<\/SelectItem>/)
  const validation = source.match(/  const validate = \(\) => \{([\s\S]+?)\n  \}\n\n  const submit/)
  const submission = source.match(/  const submit = async \(\) => \{([\s\S]+?)\n  \}\n  const status/)
  assert.ok(validation)
  assert.ok(submission)
  const errors = [], requests = []
  const script = stripTypeScriptTypes(`const validate = () => {${validation[1]}\n}; (async () => {${submission[1]}\n})()`)
  await runInNewContext(script, {
    decision: true,
    decisionSupported: false,
    topologyBlocked: false,
    selectedGateway: { id: "unit-gateway" },
    programPending: false,
    programReady: true,
    publishing: false,
    setFormError: value => errors.push(value),
    setPublishError() {},
    dataSource: { publishModel: async request => requests.push(request) },
  })
  assert.deepEqual(errors, ["所选网关尚未启用 Decision 接口。"])
  assert.deepEqual(requests, [])
})

test("Decision pricing uses the shared official catalog instead of hardcoded rates", () => {
  const source = readFileSync(new URL("../../../frontend/src/components/model-management/model-publication-dialog.tsx", import.meta.url), "utf8")
  assert.doesNotMatch(source, /applyDecisionReferencePrice|0\.042|Microsoft 2026-10-09/)
  assert.match(source, /priceValues = decision \? \[inputPrice, outputPrice\]/)
  assert.match(source, /<ModelPriceSourceFields/)
  assert.doesNotMatch(source, /!imageGeneration && <ModelPriceSourceFields/)
  assert.match(source, /<ModelPriceText>\{pricingStatus\}<\/ModelPriceText>/)
  assert.match(source, /!imageGeneration && \(!decision \|\| pricing\.priceSource !== "manual"\) && <div className="form-grid">/)
  assert.match(source, /\{!decision && <div className="form-grid">\s*<label[^>]*><span[^>]*>\{imageGeneration \? "缓存文字输入/)
})

test("Decision and image price sources omit Anthropic while chat retains it", () => {
  const dialog = readFileSync(new URL("../../../frontend/src/components/model-management/model-publication-dialog.tsx", import.meta.url), "utf8")
  const source = readFileSync(new URL("../../../frontend/src/components/model-management/model-price-source.tsx", import.meta.url), "utf8")
  assert.match(dialog, /showAnthropic=\{!decision && !imageGeneration\}/)
  assert.match(dialog, /imageGeneration=\{imageGeneration\}/)
  assert.match(source, /showAnthropic = true/)
  const choices = source.match(/\{(\(\["models_dev", "manual", "azure_retail", "anthropic"\] as const\)\.filter\(.*?\))\.map/)
  assert.ok(choices)
  for (const showAnthropic of [true, false]) {
    const sources = runInNewContext(stripTypeScriptTypes(choices[1]), { showAnthropic })
    assert.deepEqual([...sources], showAnthropic
      ? ["models_dev", "manual", "azure_retail", "anthropic"]
      : ["models_dev", "manual", "azure_retail"])
  }
})

test("Decision purpose changes reset prices for shared catalog selection", () => {
  const source = readFileSync(new URL("../../../frontend/src/components/model-management/model-publication-dialog.tsx", import.meta.url), "utf8")
  const choosePurpose = source.match(/onValueChange=\{operation => \{([\s\S]+?)\n              \}\}>/)
  assert.ok(choosePurpose)
  for (const operation of ["decision", "chat", "image_generation"]) {
    let selectedPurpose, contextWindow, draft
    runInNewContext(`(() => {${choosePurpose[1]}\n})()`, {
      operation,
      registry: { context_pricing_supported: true, context_price_rules_supported: true },
      createModelPriceDraft,
      setModelOperation: value => { selectedPurpose = value },
      setContextWindow: value => { contextWindow = value },
      setPricing: value => { draft = value },
    })
    assert.equal(selectedPurpose, operation)
    assert.equal(contextWindow, "")
    assert.equal(draft.inputPrice, "")
    assert.equal(draft.outputPrice, "")
    assert.equal(draft.cacheReadPrice, "")
    assert.equal(draft.cacheWritePrice, "")
    assert.equal(draft.priceSource, "models_dev")
    assert.equal(draft.priceReference, "")
    assert.equal(validateModelPricing(draft), "price_reference")
  }
  assert.doesNotMatch(source, />应用参考价<\/Button>/)
  assert.match(source, /className=\{decision \? "publication-decision-price-fields" : "form-grid"\}/)
})

for (const mode of ["remove", "publish", "foreign-target", "missing-model"]) {
  test(`publication retry uses the correct lifecycle endpoint: ${mode}`, async () => {
    const source = readFileSync(new URL("../../../frontend/src/components/model-management/model-publication-dialog.tsx", import.meta.url), "utf8")
    const retry = source.match(/  const retry = async \(\) => \{([\s\S]+?)\n  \}\n\n  const resumeAuthorization/)
    assert.ok(retry)
    const requests = [], errors = []
    const queued = { id: "next-task", publication_kind: mode === "publish" ? "model_add" : "model_remove" }
    const context = {
      publicationId: "failed-task", removing: mode !== "publish",
      publication: { data: { model_key: "unit-model", gateway_profile_id: "unit-target" } },
      registry: { models: mode === "missing-model" ? [] : [{ id: "unit-id", model_key: "unit-model", runtime_id: "unit-runtime" }],
        runtimes: [{ id: "unit-runtime", gateway_profile_id: mode === "foreign-target" ? "foreign" : "unit-target" }] },
      providerApiKey: "", oauthCredential: false,
      dataSource: {
        deleteModel: async id => { requests.push(["delete", id]); return { publication: queued } },
        retryGatewayPublication: async id => { requests.push(["retry", id]); return queued },
      },
      setPublishing() {}, setPublishError: value => { if (value) errors.push(value) },
      setProviderApiKey() {}, setKeyRevealed() {}, onPublicationQueued() {},
      queryClient: { setQueryData() {}, invalidateQueries() {} },
      finopsKeys: { gatewayPublication: id => [id], gatewayPublications: ["publications"] },
      errorMessage: error => String(error),
    }
    await runInNewContext(`(async () => {${retry[1]}\n})()`, context)
    assert.deepEqual(requests, mode === "publish" ? [["retry", "failed-task"]]
      : mode === "remove" ? [["delete", "unit-id"]] : [])
    assert.equal(errors.length, mode === "foreign-target" || mode === "missing-model" ? 1 : 0)
  })
}
const publicationComponents = {}
runInNewContext(publicationDialogModule + "\nexports.PublicationProgress = PublicationProgress", {
  exports: publicationComponents,
  require: name => name === "react/jsx-runtime" || name === "lucide-react" ? frontendRequire(name)
    : name === "../../lib/gateway-topology" ? gatewayTopology
      : new Proxy({}, { get: (_target, key) => key }),
})

for (const mode of ["selected", "multiple", "unknown-legacy", "old-backend", "removed", "reassociated"]) {
  test(`publication targets preserve selected Gateway and APIM identities for ${mode}`, () => {
    const edge = { deployment_id: "edge-a", native_gateway_id: "native-edge-a", kind: "self_hosted",
      endpoint_url: "https://edge.example/models", release_gate: "required" }
    const managed = { deployment_id: "managed-a", native_gateway_id: "managed", kind: "managed",
      endpoint_url: "https://customer.azure-api.net/models", release_gate: "required" }
    const publication = { gateway_profile_id: "target-a", target_deployments: mode === "multiple" ? [managed, edge]
      : mode === "unknown-legacy" ? null : mode === "old-backend" ? undefined : [edge] }
    const profiles = [{ id: "other", name: "Unrelated default APIM", is_default: true }, { id: "target-a", name: "Chosen APIM" }]
    const entry = snapshot => ({ deployment: { id: snapshot.deployment_id, native_gateway_id: snapshot.native_gateway_id,
      kind: snapshot.kind, display_name: snapshot.kind === "managed" ? "Azure-managed Gateway" : "Chosen VM Gateway",
      apim_target_id: mode === "reassociated" ? "other" : "target-a" } })
    const topology = { items: [{ target: { gateway_profile_id: "target-a", service_name: "customer-apim" },
      deployments: mode === "removed" ? [] : [entry(managed), entry(edge)] }] }
    const before = JSON.stringify(publication)
    const details = gatewayTopology.publicationTargetDetails(publication, profiles, topology)
    const markup = frontendRequire("react-dom/server").renderToStaticMarkup(publicationComponents.PublicationTargetDetails({
      publication, registry: { gateways: profiles }, topology,
    }))
    assert.match(markup, /目标 Gateway/)
    assert.match(markup, /所属 APIM/)
    assert.match(markup, /Chosen APIM/)
    assert.match(markup, /customer-apim/)
    assert.equal(markup.includes("Unrelated default APIM"), false)
    assert.equal(JSON.stringify(publication), before)
    if (["unknown-legacy", "old-backend"].includes(mode)) {
      assert.equal(details.gateways.length, 0)
      assert.equal(markup.includes("Chosen VM Gateway"), false)
      assert.match(markup, /本次任务未提供具体 Gateway 信息/)
    } else {
      assert.match(markup, /native-edge-a/)
      assert.match(markup, /Self-hosted/)
      assert.equal(markup.includes("Azure-managed Gateway"), mode === "multiple")
      assert.equal(markup.includes("Chosen VM Gateway"), !["removed", "reassociated"].includes(mode))
    }
  })
}

for (const [status, stage] of [["queued", 1], ["provisioning", 2], ["building_revision", 2],
  ["verifying", 3], ["promoting", 4], ["active", 5], ["superseded", 5]]) {
  test(`publication progress displays routing stages and elapsed time for ${status}`, () => {
    if (["provisioning", "building_revision"].includes(status)) {
      assert.equal(publicationComponents.publicationStatusLabel(status), "正在准备模型路由")
    }
    const markup = frontendRequire("react-dom/server").renderToStaticMarkup(publicationComponents.PublicationProgress({
      publication: { status, created_at: "2026-10-03T09:44:12Z", completed_at: "2026-10-03T09:53:42Z" },
    }))
    assert.match(markup, new RegExp(`${stage} / 5`))
    assert.match(markup, /准备模型路由/)
    assert.match(markup, /验证模型/)
    assert.match(markup, /9:30/)
    assert.doesNotMatch(markup, /APIM Revision|候选 Revision|6 \/ 6/)
  })
}

test("configuration publication progress does not claim model inference was tested", () => {
  const markup = frontendRequire("react-dom/server").renderToStaticMarkup(publicationComponents.PublicationProgress({
    publication: { status: "verifying", configuration_only: true },
  }))
  assert.match(markup, /验证路由配置/)
  assert.doesNotMatch(markup, /验证模型/)
})

test("failed publication progress does not claim a model is serving or that Azure was unaffected", () => {
  const markup = frontendRequire("react-dom/server").renderToStaticMarkup(publicationComponents.PublicationProgress({
    publication: { status: "failed" },
  }))
  assert.match(markup, /发布未完成，生效状态未确认/)
  assert.equal(markup.includes("当前 Revision 未受影响"), false)
  assert.equal(markup.includes("当前模型继续正常服务"), false)
})

const releaseComponents = {}
runInNewContext(releasesModule + "\nexports.PublicationFailureNotice = PublicationFailureNotice; exports.ReleaseDetail = ReleaseDetail", {
  exports: releaseComponents,
  require: name => name === "react/jsx-runtime" || name === "lucide-react"
    ? frontendRequire(name) : name === "../providers/timezone-provider"
      ? { useTimezone: () => ({ timezone: "UTC" }) } : name === "../locales"
        ? { getIntlLocale: () => "en-US" } : new Proxy({}, { get: (_target, key) => key }),
})

for (const status of ["queued", "validating", "provisioning", "building_revision", "verifying", "awaiting_authorization", "promoting", "failed", "rolling_back", "rolled_back", "active", "superseded"]) {
  test(`release changes distinguish ${status} intent from confirmed activation`, () => {
    const applied = ["active", "superseded"].includes(status)
    const release = { id: "publication-a", status, role: applied ? "current" : "failed", generation: 1,
      apim_revision: "candidate-already-created", publication_kind: "model_add", created_at: "2026-09-25T01:00:00Z",
      completed_at: "2026-09-25T01:05:00Z", created_by: "owner@example.com", audit: [],
      rollback_blockers: [],
      dependencies: { live_status: "not_checked", issues: [], backends: [], backend_pools: [], named_values: [] },
      diff_from_base: { changes: { added_models: ["proposed-model"], removed_models: ["old-model"], changed_models: ["edited-model"],
        added_backend_pools: ["proposed-pool"], removed_backend_pools: [], changed_backend_pools: [] } },
    }
    const render = () => frontendRequire("react-dom/server").renderToStaticMarkup(releaseComponents.ReleaseDetail({
      release, loading: false, canManage: false, operationsEnabled: false, busy: false,
    }))
    const markup = render()
    assert.match(markup, new RegExp(`data-state="${applied ? "applied" : "planned"}"`))
    for (const label of applied ? ["已新增", "已移除", "已变更"] : ["拟新增", "拟移除", "拟变更", "计划变更", "未确认生效"]) assert.ok(markup.includes(label))
    if (!applied) for (const label of ["已新增", "已移除", "已变更"]) assert.equal(markup.includes(label), false)
    assert.match(markup, /proposed-model/)
    assert.match(markup, /proposed-pool/)
    if (status === "rolled_back") {
      release.audit = [{ id: "audit-a", to_status: "active", created_at: "2026-09-25T01:04:00Z", actor: "worker" }]
      assert.match(render(), /data-state="applied"/)
      assert.match(render(), /已新增/)
    }
  })
}

for (const [code, expected] of [
  ["publisher_permissions_missing", "Publisher 缺少此 APIM 的模型发布权限"],
  ["publisher_permissions_unverified", "无法核验 Publisher 的发布权限"],
  ["publisher_authorization_failed", "Azure 拒绝了 Publisher 的 APIM 管理请求"],
  ["parent_policy_changed", "APIM 父策略与排队时的快照不一致"],
  ["credential_source_missing", "APIM 凭据资源尚未创建，请重新提供 API Key 后重试"],
  ["publication_failed", "发布未完成，请使用任务 ID 查询诊断记录"],
  ["future_unknown_code", "发布未完成，请使用任务 ID 查询诊断记录"],
]) {
  test(`publication failure ${code} renders controlled copy without raw messages or commands`, () => {
    const markup = frontendRequire("react-dom/server").renderToStaticMarkup(
      releaseComponents.PublicationFailureNotice({
        publicationId: "10000000-0000-4000-8000-000000000099",
        failure: { code, message: "private-error-must-not-render", stage: "validating",
          request_id: "10000000-0000-4000-8000-000000000098", http_status: 403,
          missing_actions: code === "publisher_permissions_missing"
            ? ["Microsoft.ApiManagement/service/namedValues/write"] : [] },
      }),
    )
    assert.ok(markup.includes(expected))
    assert.match(markup, /role="alert"/)
    assert.match(markup, /验证配置/)
    assert.match(markup, /10000000-0000-4000-8000-000000000099/)
    assert.match(markup, /Azure Request ID/)
    assert.match(markup, /10000000-0000-4000-8000-000000000098/)
    assert.match(markup, />403</)
    assert.equal(markup.includes("private-error-must-not-render"), false)
    assert.equal(markup.includes("<button"), false)
    assert.equal(markup.includes("<details"), code === "publisher_permissions_missing")
    if (code === "publisher_permissions_missing") {
      assert.match(markup, /Microsoft.ApiManagement\/service\/namedValues\/write/)
    }
  })
}

test("legacy publication failure does not invent an Azure request ID or HTTP status", () => {
  const markup = frontendRequire("react-dom/server").renderToStaticMarkup(
    releaseComponents.PublicationFailureNotice({
      publicationId: "10000000-0000-4000-8000-000000000099",
      failure: { code: "publication_failed", message: "", stage: null,
        request_id: null, http_status: null, missing_actions: [] },
    }),
  )
  assert.match(markup, /10000000-0000-4000-8000-000000000099/)
  assert.equal(markup.includes("Azure Request ID"), false)
  assert.equal(markup.includes("HTTP"), false)
  assert.equal(markup.includes("失败阶段"), false)
  assert.equal(markup.includes("<details"), false)
})

function diagnosticHarness({ role = "owner", supported = true } = {}) {
  const option = { model_id: "model-a", gateway_deployment_id: "deployment-a", model_name: "Chosen model",
    model_key: "chosen-model", gateway_name: "Chosen Gateway", native_gateway_id: "native-a" }
  const preferredOption = { model_id: "model-b", gateway_deployment_id: "deployment-b", model_name: "Preferred model",
    model_key: "preferred-model", gateway_name: "Preferred Gateway", native_gateway_id: "native-b" }
  const query = { data: { options: [option, preferredOption], access: {
    status: "ready", reason: null, scope: "/subscriptions/customer/resourceGroups/platform/providers/Microsoft.ApiManagement/service/customer-apim/subscriptions/customer-probe",
    api_principal_id: "customer-api-identity", tenant_id: "customer-tenant", subscription_id: "customer",
    resource_group: "platform", apim_name: "customer-apim", probe_subscription_id: "customer-probe",
    required_actions: ["Microsoft.ApiManagement/service/subscriptions/listSecrets/action"], missing_actions: [],
    checked_at: new Date().toISOString(), template_path: "infra/modules/runtime-diagnostic-rbac.bicep",
  } }, error: null, isLoading: false, isFetching: false, refreshes: 0, refetch() { this.refreshes += 1 } }
  const calls = [], closes = [], states = []
  let cursor = 0, queryOptions
  const effects = []
  const useState = initial => {
    const index = cursor++
    if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial
    return [states[index], value => { states[index] = value }]
  }
  const imports = {
    react: { useState, useEffect: effect => effects.push(effect), useRef: initial => useState({ current: initial })[0], useId: () => "diagnostic-target" },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@tanstack/react-query": { useQuery: options => { queryOptions = options; return query } },
    "../../providers/auth-provider": { useAuth: () => ({ user: { role } }) },
    "../../data-sources/apim/api": { dataSource: { runtimeCheckPlan: () => { throw new Error("Unit harness does not make API calls") } } },
    "../../lib/runtime-health": runtimeHealth,
  }
  const exports = {}
  runInNewContext(diagnosticModule, { exports, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  const props = { runtime: healthRuntime({ name: "Connection A" }), supported, busy: false, error: null, result: null,
    onCheck: value => calls.push(JSON.parse(JSON.stringify(value))), onClose: () => closes.push(true) }
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === "object"
    ? [value, ...nodes(value.props?.children)] : []
  const all = () => {
    cursor = 0
    const rendered = nodes(exports.RuntimeDiagnosticDialog(props))
    while (effects.length) effects.shift()()
    cursor = 0
    return effects.length ? nodes(exports.RuntimeDiagnosticDialog(props)) : nodes(exports.RuntimeDiagnosticDialog(props))
  }
  const find = predicate => all().find(predicate)
  const run = () => find(node => node.type === "Button" && Array.isArray(node.props.children)
    && node.props.children.some(child => child === "运行检查" || child === "正在检查"))
  const select = () => find(node => node.type === "Select").props.onValueChange("model-a:deployment-a")
  return { props, query, calls, closes, all, find, run, select, queryOptions: () => { all(); return queryOptions } }
}

test("diagnostic dialog opens as a read plan, never implicitly selecting or invoking a model", () => {
  const harness = diagnosticHarness()
  assert.match(harness.find(node => node.type === "DialogContent").props.className, /registry-editor-dialog registry-editor/)
  assert.equal(harness.find(node => node.type === "DialogHeader").props.className, "registry-editor-header")
  assert.equal(harness.find(node => node.type === "DialogFooter").props.className, "registry-editor-footer")
  assert.equal(harness.queryOptions().enabled, true)
  assert.equal(harness.queryOptions().retry, false)
  assert.equal(harness.find(node => node.type === "Select").props.value, "")
  assert.equal(harness.run().props.disabled, true)
  assert.equal(harness.calls.length, 0)
  harness.find(node => node.type === "Button" && node.props.children === "取消").props.onClick()
  assert.equal(harness.closes.length, 1)
  assert.equal(harness.calls.length, 0)
})

test("Gateway child diagnostic preselects its deployment without running the check", () => {
  const harness = diagnosticHarness()
  harness.props.preferredGatewayDeploymentId = "deployment-b"
  assert.equal(harness.find(node => node.type === "Select").props.value, "model-b:deployment-b")
  assert.equal(harness.run().props.disabled, false)
  assert.equal(harness.calls.length, 0)
})

test("diagnostic loading keeps compact body typography and separates its icon from the label", () => {
  const harness = diagnosticHarness()
  harness.query.data = undefined
  harness.query.isLoading = true
  harness.query.isFetching = true
  const loading = harness.find(node => node.props?.role === "status")
  assert.equal(loading.props.className, "runtime-diagnostic-loading")
  assert.ok(harness.find(node => node.type === "span" && node.props.children === "正在加载诊断计划"))
  const spinner = harness.find(node => node.type === "RefreshCw" && node.props["aria-hidden"] === "true")
  assert.equal(spinner.props.size, 14)
  assert.equal(spinner.props.className, "spin")
  const css = readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8")
  const bodyStyles = css.match(/\.runtime-diagnostic-body\s*\{([^}]+)\}/)?.[1] ?? ""
  assert.match(bodyStyles, /font-size:\s*12px/)
  assert.match(bodyStyles, /line-height:\s*18px/)
  const loadingStyles = css.match(/\.runtime-diagnostic-loading\s*\{([^}]+)\}/)?.[1] ?? ""
  assert.match(loadingStyles, /display:\s*flex/)
  assert.match(loadingStyles, /align-items:\s*center/)
  assert.match(loadingStyles, /gap:\s*6px/)
  assert.match(loadingStyles, /color:\s*var\(--muted-foreground\)/)
  assert.equal(harness.run().props.disabled, true)
  assert.equal(harness.calls.length, 0)
})

for (const options of [{ role: "member" }, { supported: false }]) test(`diagnostic dialog gates reads and execution ${JSON.stringify(options)}`, () => {
  const harness = diagnosticHarness(options)
  assert.equal(harness.queryOptions().enabled, false)
  harness.select()
  assert.equal(harness.run().props.disabled, true)
  harness.run().props.onClick()
  assert.equal(harness.calls.length, 0)
})

test("diagnostic dialog submits once, allows closing while pending, and never retries a failure", () => {
  const harness = diagnosticHarness()
  harness.select()
  const run = harness.run()
  assert.equal(run.props.disabled, false)
  run.props.onClick()
  run.props.onClick()
  assert.deepEqual(harness.calls, [{ model_id: "model-a", gateway_deployment_id: "deployment-a" }])
  harness.props.busy = true
  harness.find(node => node.type === "Dialog").props.onOpenChange(false)
  assert.equal(harness.closes.length, 1)
  const close = harness.find(node => node.type === "Button" && node.props.children === "关闭")
  assert.notEqual(close.props.disabled, true)
  close.props.onClick()
  assert.equal(harness.closes.length, 2)
  assert.equal(harness.run().props.disabled, true)
  assert.equal(harness.find(node => node.type === "Select").props.disabled, true)
  harness.props.busy = false
  harness.props.error = "Unknown execution outcome"
  assert.equal(harness.run().props.disabled, true)
  assert.equal(harness.calls.length, 1)
})

test("reopened diagnostic dialog keeps the in-flight target and cannot submit again", () => {
  const harness = diagnosticHarness()
  harness.props.busy = true
  harness.props.initialSelection = { model_id: "model-a", gateway_deployment_id: "deployment-a" }
  assert.equal(harness.find(node => node.type === "Select").props.value, "model-a:deployment-a")
  assert.equal(harness.run().props.disabled, true)
  harness.run().props.onClick()
  assert.equal(harness.calls.length, 0)
  harness.props.busy = false
  harness.props.error = "Unknown execution outcome"
  assert.equal(harness.run().props.disabled, true)
  harness.props.result = { runtime_id: "connection-a", status: "available", outcome: "passed",
    checked_at: new Date().toISOString() }
  assert.equal(harness.run(), undefined)
  assert.equal(harness.find(node => node.type === "strong").props.children, "上次检查通过")
})

function availabilityCommandHarness(enabled, role = "owner") {
  const client = new queryLibrary.QueryClient({ defaultOptions: {
    queries: { retry: false, gcTime: Infinity }, mutations: { gcTime: Infinity },
  } })
  const cache = inventoryHarness(async () => { throw new Error("Unexpected API read in unit test") })
  const calls = [], tasks = [], gates = new Map()
  const initial = { model_id: "model-a", desired_enabled: enabled, desired_revision: 7,
    effective_enabled: enabled, effective_revision: 7, verified_at: "2026-09-28T00:00:00Z",
    status: "succeeded", command_ready: true, reason: null, operation: null }
  client.setQueryData(cache.finopsKeys.modelAvailability, { items: [initial, { ...initial, model_id: "model-b" }] })
  const read = modelId => client.getQueryData(cache.finopsKeys.modelAvailability).items.find(item => item.model_id === modelId)
  const update = (modelId, change) => client.setQueryData(cache.finopsKeys.modelAvailability, current => ({
    items: current.items.map(item => item.model_id === modelId ? { ...item, ...change } : item),
  }))
  const send = (action, id, value) => {
    calls.push({ action, id, value: JSON.parse(JSON.stringify(value)) })
    const gate = gates.get(`${action}:${id}`)
    gate.started.resolve()
    return gate.response.promise
  }
  class ApiError extends Error {
    constructor(status) { super("Request not confirmed"); this.status = status }
  }
  const mount = (modelId = "model-a") => {
    let observer, unsubscribe
    const command = { current: null }
    const imports = {
      react: { useRef: () => command },
      "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
      "../../lib/model-availability": { availabilityPresentation },
      "../../providers/auth-provider": { useAuth: () => ({ user: { role } }) },
      "../../data-sources/apim/queries": cache,
      "../../data-sources/apim/api": { ApiError, dataSource: {
        changeModelAvailability: (id, value) => send("change", id, value),
        retryModelAvailability: (id, revision) => send("retry", id, revision),
      } },
      "@tanstack/react-query": {
        useQueryClient: () => client,
        useMutationState: options => client.getMutationCache().findAll(options.filters).map(options.select),
        useMutation: options => {
          if (!observer) {
            observer = new queryLibrary.MutationObserver(client, options)
            unsubscribe = observer.subscribe(() => {})
          } else observer.setOptions(options)
          return { ...observer.getCurrentResult(), mutate: request => { tasks.push(observer.mutate(request).catch(() => {})) } }
        },
      },
    }
    const exports = {}
    runInNewContext(availabilityModule, { exports, crypto: globalThis.crypto,
      require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
    return {
      render: (overrides = {}) => {
        const readback = read(modelId)
        const action = exports.useModelAvailabilityCommand(modelId, readback)
        const props = { readback, pending: action.submitting, requestedEnabled: action.requestedEnabled, ...overrides }
        return { action,
          status: exports.ModelAvailabilityStatus({ ...props, error: action.error, onOpen() {} }),
          toggle: exports.ModelAvailabilitySwitch({ ...props, registryEnabled: enabled, onChange: action.change }),
        }
      },
      unmount: () => { unsubscribe?.(); unsubscribe = undefined },
    }
  }
  const prepare = (action = "change", id = "model-a") => {
    const gate = { started: Promise.withResolvers(), response: Promise.withResolvers() }
    gates.set(`${action}:${id}`, gate)
    return gate
  }
  return { client, cache, calls, tasks, initial, read, update, mount, prepare, ApiError }
}

for (const role of ["owner", "member", null]) {
  for (const publicationId of [null, "publication-a"]) {
  test(`model queries respect ${role ?? "anonymous"} permissions with publication ${publicationId}`, () => {
    const components = {}, seen = [], stop = new Error("query inspected")
    runInNewContext(modelPageModule, {
      exports: components,
      URL,
      window: { location: { href: `https://unit.invalid/?publication=${publicationId ?? ""}` } },
      require: name => name === "@tanstack/react-query" ? {
        useQueryClient: () => ({}),
        useQuery: options => {
          seen.push(options)
          if (options.queryKey[0] === "publication") throw stop
          return {}
        },
      } : name === "react" ? {
        useState: value => [typeof value === "function" ? value() : value, () => {}],
        useEffect() {},
      } : name === "../providers/auth-provider" ? { useAuth: () => ({ user: role ? { role } : null }) }
        : name === "../data-sources/apim/queries" ? { finopsQueries: {
          registry: () => ({ queryKey: ["registry"] }),
          modelAvailability: () => ({ queryKey: ["availability"] }),
          apimTargetTopologies: () => ({ queryKey: ["topology"] }),
          gatewayPublications: () => ({ queryKey: ["publications"] }),
          gatewayPublication: id => ({ queryKey: ["publication", id], enabled: Boolean(id) }),
        } } : name === "../components/brand-logos" ? { providerBrandFromIdentity: () => "generic",
          gatewayBrandFromIdentity: () => "generic" }
          : name === "react/jsx-runtime" || name === "lucide-react" ? frontendRequire(name)
          : new Proxy({}, { get: (_target, key) => key }),
    })
    assert.throws(() => components.ModelManagementPage({ onToggleSidebar() {} }), error => error === stop)
    assert.equal(seen.find(options => options.queryKey[0] === "availability").enabled, role !== null)
    assert.equal(seen.find(options => options.queryKey[0] === "publications").enabled, role === "owner")
    assert.equal(seen.find(options => options.queryKey[0] === "publication").enabled,
      role === "owner" && Boolean(publicationId))
  })
  }
}

for (const enabled of [true, false]) {
  test(`initial availability read shows loading without replacing confirmed ${enabled ? "enabled" : "disabled"} evidence`, () => {
    const harness = availabilityCommandHarness(enabled)
    const view = harness.mount()
    try {
      const reading = view.render({ readback: undefined, loading: true })
      assert.equal(reading.status.props.title, "正在读取状态")
      assert.equal(reading.status.props.className, "model-availability-status pending")
      assert.equal(reading.toggle.props.title, "正在读取状态")
      assert.equal(reading.toggle.props["aria-busy"], true)
      assert.equal(reading.toggle.props.disabled, true)
      const refreshing = view.render({ loading: true })
      assert.equal(refreshing.status.props.title, enabled ? "已启用" : "已停用")
      assert.equal(refreshing.toggle.props.disabled, false)
      const missing = view.render({ readback: undefined, loading: false })
      assert.equal(missing.status.props.title, "未验证")
      assert.equal(missing.toggle.props.disabled, true)
      assert.equal(harness.calls.length, 0)
    } finally {
      view.unmount()
      harness.client.clear()
    }
  })

  test(`failed initial confirmation keeps ${enabled ? "enabled" : "disabled"} intent without claiming publication failure`, () => {
    const harness = availabilityCommandHarness(enabled)
    harness.update("model-a", { status: "failed", effective_enabled: null,
      effective_revision: null, verified_at: null, command_ready: true,
      reason: "model_availability_initialization_failed", operation: null })
    const view = harness.mount()
    try {
      const rendered = view.render()
      assert.equal(rendered.status.props.title, "同步失败")
      assert.equal(rendered.toggle.props.checked, enabled)
      assert.equal(rendered.toggle.props.disabled, false)
      assert.equal(rendered.toggle.props.title, enabled ? "停用此模型" : "启用此模型")
      assert.equal(rendered.action.verified, false)
      assert.equal(rendered.action.canRetry, false)
      assert.equal(harness.calls.length, 0)
    } finally {
      view.unmount()
      harness.client.clear()
    }
  })

  test(`confirmed ${enabled ? "enabled" : "disabled"} state remains visible while publication blocks commands`, () => {
    const harness = availabilityCommandHarness(enabled)
    harness.update("model-a", { command_ready: false, reason: "model_availability_capability_unverified" })
    const view = harness.mount()
    try {
      const rendered = view.render()
      assert.equal(rendered.status.props.title, enabled ? "已启用" : "已停用")
      assert.equal(rendered.action.verified, true)
      assert.equal(rendered.action.canChange, false)
      assert.equal(rendered.toggle.props.disabled, true)
      assert.equal(rendered.toggle.props.title, "网关覆盖尚未验证")
      rendered.toggle.props.onCheckedChange(!enabled)
      rendered.action.change()
      assert.equal(harness.calls.length, 0)
      assert.equal(harness.tasks.length, 0)
    } finally {
      view.unmount()
      harness.client.clear()
    }
  })

  for (const commandReady of [true, false]) {
    test(`member sees verified ${enabled ? "enabled" : "disabled"} status without commands (${commandReady})`, () => {
      const harness = availabilityCommandHarness(enabled, "member")
      harness.update("model-a", { command_ready: commandReady })
      const view = harness.mount()
      try {
        const rendered = view.render()
        assert.equal(rendered.status.props.title, enabled ? "已启用" : "已停用")
        assert.equal(rendered.action.verified, true)
        assert.equal(rendered.toggle.props.checked, enabled)
        assert.equal(rendered.toggle.props.disabled, true)
        assert.equal(rendered.toggle.props.title, "仅 Owner 可修改模型状态")
        assert.equal(rendered.action.canChange, false)
        rendered.action.change()
        rendered.toggle.props.onCheckedChange(!enabled)
        harness.update("model-a", { status: "failed", command_ready: true,
          operation: { id: "operation-a" } })
        const failed = view.render()
        assert.equal(failed.status.props.title, "同步失败")
        assert.equal(failed.action.canRetry, false)
        failed.action.retry()
        assert.equal(harness.calls.length, 0)
        assert.equal(harness.tasks.length, 0)
      } finally {
        view.unmount()
        harness.client.clear()
      }
    })
  }
}

for (const enabled of [true, false]) {
  test(`availability ${enabled ? "disable" : "enable"} displays the request direction before and after acknowledgement`, async () => {
    const harness = availabilityCommandHarness(enabled)
    const row = harness.mount(), dialog = harness.mount(), other = harness.mount("model-b")
    const gate = harness.prepare()
    const label = enabled ? "正在停用" : "正在启用"
    try {
      const original = row.render()
      original.action.change()
      original.action.change()
      for (const view of [row, dialog]) {
        const pending = view.render()
        assert.equal(pending.status.props.title, label)
        assert.equal(pending.action.requestedEnabled, !enabled)
        assert.equal(pending.action.verified, false)
        assert.equal(pending.status.props.className.includes("registry-health"), false)
        assert.equal(pending.toggle.props.checked, enabled)
        assert.equal(pending.toggle.props["aria-label"], label)
        assert.equal(pending.toggle.props.disabled, true)
        pending.action.change()
      }
      assert.equal(other.render().action.inFlight, false)
      assert.equal(other.render().action.canChange, true)
      await gate.started.promise
      assert.equal(harness.calls.length, 1)
      assert.equal(harness.calls[0].value.enabled, !enabled)
      assert.equal(harness.calls[0].value.expected_revision, 7)
      const stale = Promise.withResolvers()
      const oldReadback = harness.client.getQueryData(harness.cache.finopsKeys.modelAvailability)
      const staleRead = harness.client.fetchQuery({ queryKey: harness.cache.finopsKeys.modelAvailability,
        queryFn: () => stale.promise }).catch(() => {})
      row.unmount()
      assert.equal(dialog.render().status.props.title, label)
      gate.response.resolve({ operation: { id: "operation-a", model_id: "model-a", desired_enabled: !enabled,
        desired_revision: 8, status: "pending" } })
      await harness.tasks[0]
      stale.resolve(oldReadback)
      await staleRead
      assert.equal(dialog.render().action.submitting, false)
      assert.equal(dialog.render().status.props.title, label)
      assert.equal(harness.read("model-a").desired_enabled, !enabled)
      assert.equal(harness.read("model-a").desired_revision, 8)
      for (const stage of ["projected", "partial"]) {
        harness.update("model-a", { status: stage })
        assert.equal(dialog.render().action.direction, enabled ? "disabling" : "enabling")
        assert.equal(dialog.render().action.verified, false)
        assert.equal(dialog.render().status.props.title, stage === "partial" ? "部分生效" : label)
      }
      harness.update("model-a", { status: "succeeded", effective_enabled: !enabled, effective_revision: 8, command_ready: true })
      assert.equal(dialog.render().status.props.title, enabled ? "已停用" : "已启用")
      assert.equal(dialog.render().action.verified, true)
      assert.equal(dialog.render().action.canChange, true)
      assert.equal(harness.calls.length, 1)
    } finally { row.unmount(); dialog.unmount(); other.unmount(); harness.client.clear() }
  })

  test(`availability retry retains the original ${enabled ? "enable" : "disable"} direction`, async () => {
    const harness = availabilityCommandHarness(enabled)
    harness.update("model-a", { status: "failed", operation: { id: "operation-a" } })
    const view = harness.mount(), dialog = harness.mount()
    const gate = harness.prepare("retry", "operation-a")
    try {
      view.render().action.retry()
      const expected = enabled ? "正在启用" : "正在停用"
      assert.equal(dialog.render().status.props.title, expected)
      dialog.render().action.retry()
      await gate.started.promise
      assert.equal(harness.calls.length, 1)
      assert.equal(harness.calls[0].action, "retry")
      assert.equal(harness.calls[0].id, "operation-a")
      assert.equal(harness.calls[0].value, 7)
      gate.response.resolve({ operation: { id: "operation-a", model_id: "model-a", desired_enabled: enabled,
        desired_revision: 7, status: "pending" } })
      await harness.tasks[0]
      assert.equal(dialog.render().status.props.title, expected)
    } finally { view.unmount(); dialog.unmount(); harness.client.clear() }
  })
}

test("availability failed transport preserves the command identity without inventing a completed toggle", async () => {
  const harness = availabilityCommandHarness(true)
  const view = harness.mount()
  const first = harness.prepare()
  try {
    view.render().action.change()
    await first.started.promise
    first.response.reject(new Error("No response received"))
    await harness.tasks[0]
    assert.equal(view.render().status.props.children.props.children[1].props.children, "请求未确认")
    assert.equal(view.render().status.props.title, "请求结果未确认，请刷新状态。")
    assert.equal(harness.read("model-a").desired_enabled, true)
    assert.equal(harness.read("model-a").desired_revision, 7)
    const retry = harness.prepare()
    view.render().action.change()
    assert.equal(view.render().status.props.title, "正在停用")
    await retry.started.promise
    assert.deepEqual(harness.calls[1].value, harness.calls[0].value)
    retry.response.reject(new harness.ApiError(409))
    await harness.tasks[1]
    assert.equal(view.render().status.props.children.props.children[1].props.children, "请求未确认")
    assert.equal(view.render().status.props.title, "模型状态已变化，请刷新后重试。")
    assert.equal(harness.calls.length, 2)
  } finally { view.unmount(); harness.client.clear() }
})

test("availability late acknowledgement cannot overwrite a newer server revision", async () => {
  const harness = availabilityCommandHarness(true)
  const view = harness.mount()
  const gate = harness.prepare()
  try {
    view.render().action.change()
    await gate.started.promise
    harness.update("model-a", { desired_revision: 9, effective_revision: 9 })
    gate.response.resolve({ operation: { id: "old-operation", model_id: "model-a", desired_enabled: false,
      desired_revision: 8, status: "pending" } })
    await harness.tasks[0]
    assert.equal(harness.read("model-a").desired_revision, 9)
    assert.equal(harness.read("model-a").desired_enabled, true)
    assert.equal(view.render().status.props.title, "已启用")
  } finally { view.unmount(); harness.client.clear() }
})

function diagnosticRequestsHarness({ supported = true } = {}) {
  const client = new queryLibrary.QueryClient({ defaultOptions: {
    queries: { retry: false, gcTime: Infinity }, mutations: { gcTime: Infinity },
  } })
  const cache = inventoryHarness(async () => { throw new Error("Unexpected API read in unit test") })
  const calls = [], completions = [], errors = [], tasks = [], observers = []
  const gates = new Map()
  const runtimes = ["connection-a", "connection-b"].map(id => healthRuntime({ id, name: id }))
  client.setQueryData(cache.finopsKeys.registry, registry({ runtimes }))
  const mount = () => {
    let observer, unsubscribe
    const imports = {
      "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
      "../../lib/runtime-health": runtimeHealth,
      "../../data-sources/apim/queries": cache,
      "../../data-sources/apim/api": { dataSource: { checkRuntime(runtimeId, selection) {
        calls.push({ runtimeId, selection })
        const gate = gates.get(runtimeId)
        gate.started.resolve()
        return gate.response.promise
      } } },
      "@tanstack/react-query": {
        useQueryClient: () => client,
        useMutationState: options => client.getMutationCache().findAll(options.filters).map(options.select),
        useMutation: options => {
          if (!observer) {
            observer = new queryLibrary.MutationObserver(client, options)
            observers.push(observer)
            unsubscribe = observer.subscribe(() => {})
          } else observer.setOptions(options)
          return { mutate: request => { tasks.push(observer.mutate(request).catch(() => {})) } }
        },
      },
    }
    const exports = {}
    runInNewContext(diagnosticModule, { exports, crypto: globalThis.crypto,
      require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
    return {
      render: () => exports.useRuntimeDiagnostics({ supported,
        onSuccess: (result, request) => completions.push({ result, request }),
        onError: (error, request) => errors.push({ error, request }),
      }),
      unmount: () => { unsubscribe?.(); unsubscribe = undefined },
    }
  }
  const prepare = runtimeId => {
    const gate = { started: Promise.withResolvers(), response: Promise.withResolvers() }
    gates.set(runtimeId, gate)
    return gate
  }
  return { client, cache, calls, completions, errors, observers, runtimes, mount, prepare, tasks,
    flush: () => Promise.all(tasks),
    stored: () => client.getQueryData(cache.finopsKeys.registry),
  }
}

test("diagnostic mutation survives unmount and reopens without replaying the request", async () => {
  const harness = diagnosticRequestsHarness()
  const first = harness.mount()
  const second = harness.mount()
  const selection = { model_id: "model-a", gateway_deployment_id: "deployment-a" }
  const gate = harness.prepare("connection-a")
  try {
    const request = first.render().start(harness.runtimes[0], selection)
    await gate.started.promise
    assert.equal(first.render().attempts[0].status, "pending")
    first.unmount()
    const reopened = second.render()
    assert.equal(reopened.attempts[0].request.id, request.id)
    assert.equal(reopened.start(harness.runtimes[0], selection).id, request.id)
    assert.equal(harness.calls.length, 1)
    const stale = Promise.withResolvers()
    const oldRegistry = harness.stored()
    const staleRead = harness.client.fetchQuery({ queryKey: harness.cache.finopsKeys.registry,
      queryFn: () => stale.promise }).catch(() => {})
    const result = { runtime_id: "connection-a", status: "available", outcome: "passed",
      checked_at: new Date().toISOString(), request_id: "server-request-a" }
    gate.response.resolve(result)
    await harness.flush()
    stale.resolve(oldRegistry)
    await staleRead
    assert.equal(second.render().attempts[0].status, "success")
    assert.equal(second.render().attempts[0].result.request_id, "server-request-a")
    assert.equal(harness.stored().runtimes[0].diagnostic, result)
    assert.equal(harness.stored().runtimes[1].diagnostic, undefined)
    assert.equal(harness.completions.length, 1)
    assert.equal(harness.completions[0].request.runtimeId, "connection-a")
    assert.equal(harness.calls.length, 1)
    assert.equal(harness.observers[0].options.retry, false)
  } finally { first.unmount(); second.unmount(); harness.client.clear() }
})

test("concurrent diagnostic mutations retain per-connection progress and failures without retries", async () => {
  const harness = diagnosticRequestsHarness()
  const view = harness.mount()
  const firstGate = harness.prepare("connection-a"), secondGate = harness.prepare("connection-b")
  try {
    const first = view.render().start(harness.runtimes[0], { model_id: "first", gateway_deployment_id: "deployment-a" })
    const second = view.render().start(harness.runtimes[1], { model_id: "second", gateway_deployment_id: "deployment-b" })
    await Promise.all([firstGate.started.promise, secondGate.started.promise])
    assert.equal(view.render().attempts.filter(attempt => attempt.status === "pending").length, 2)
    secondGate.response.resolve({ runtime_id: "connection-b", status: "unknown", outcome: "blocked",
      checked_at: new Date().toISOString(), http_status: 503 })
    await harness.tasks[1]
    assert.equal(view.render().attempts.find(attempt => attempt.request.id === second.id).status, "success")
    assert.equal(view.render().attempts.find(attempt => attempt.request.id === first.id).status, "pending")
    assert.equal(harness.stored().runtimes[0].diagnostic, undefined)
    assert.equal(runtimeHealthState(harness.stored().runtimes[1]), "blocked")
    firstGate.response.reject(new Error("Connection interrupted"))
    await harness.flush()
    const failed = view.render().attempts.find(attempt => attempt.request.id === first.id)
    assert.equal(failed.status, "error")
    assert.equal(failed.result, undefined)
    assert.equal(harness.errors[0].request.runtimeId, "connection-a")
    assert.equal(harness.stored().runtimes[0].diagnostic, undefined)
    assert.equal(harness.client.getQueryState(harness.cache.finopsKeys.registry).isInvalidated, true)
    assert.equal(harness.calls.length, 2)
    assert.equal(harness.client.getMutationCache().getAll().length, 2)
  } finally { view.unmount(); harness.client.clear() }
})

test("diagnostic mutation refuses unsupported execution and a server-recorded pending check", () => {
  for (const supported of [false, true]) {
    const harness = diagnosticRequestsHarness({ supported })
    const view = harness.mount()
    try {
      const runtime = { ...harness.runtimes[0], last_checked_at: new Date().toISOString(),
        health_message: runtimeDiagnosticPrefix + "pending:existing-check" }
      assert.equal(view.render().start(runtime, { model_id: "first", gateway_deployment_id: "deployment-a" }), null)
      assert.equal(harness.calls.length, 0)
      assert.equal(view.render().attempts.length, 0)
    } finally { view.unmount(); harness.client.clear() }
  }
})

test("connection row shows checking without old timestamps and locks only its own metadata", () => {
  const render = checking => modelPageComponents.ConnectionRow({
    runtime: healthRuntime({ name: "Connection A", diagnostic: { runtime_id: "connection-a",
      status: "available", outcome: "passed", checked_at: "2026-09-24T00:59:00Z" }, config: {} }),
    models: [{ id: "model-a", enabled: true }], profiles: [], topology: null, busy: false, checking,
  })
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === "object"
    ? [value, ...nodes(value.props?.children)] : []
  for (const checking of [true, false]) {
    const row = nodes(render(checking))
    const cell = row.find(node => node.props?.className === "model-status-cell")
    assert.equal(cell.props["aria-busy"], checking || undefined)
    assert.equal(cell.props.children.props.state, checking ? "checking" : "online")
    assert.equal(cell.props.children.props.checkedAt, checking ? undefined : "2026-09-24T00:59:00Z")
    assert.equal(row.find(node => node.props?.className === "model-edit-action").props.disabled, checking)
    assert.equal(row.find(node => node.props?.className === "connection-health-action").props.disabled, false)
    assert.equal(row.find(node => node.type?.name === "Toggle").props.disabled, checking)
  }
})

test("diagnostic dialog cannot submit stale selection or a plan being revalidated", () => {
  const harness = diagnosticHarness()
  harness.select()
  harness.query.isFetching = true
  assert.equal(harness.run().props.disabled, true)
  harness.query.isFetching = false
  harness.query.data.options = []
  assert.equal(harness.run().props.disabled, true)
  harness.run().props.onClick()
  assert.equal(harness.calls.length, 0)
})

for (const status of ["missing_permissions", "unverified", "omitted"]) test(`diagnostic execution fails closed when deployment access is ${status}`, () => {
  const harness = diagnosticHarness()
  const original = harness.query.data.access
  harness.query.data.access = status === "omitted" ? undefined : { ...original, status,
    reason: status === "missing_permissions" ? "permission_missing" : "permission_unverified",
    missing_actions: status === "missing_permissions" ? original.required_actions : [] }
  harness.select()
  assert.equal(harness.run().props.disabled, true)
  harness.run().props.onClick()
  assert.equal(harness.calls.length, 0)
  harness.find(node => node.type === "Button" && node.props["aria-label"] === "重新核验权限").props.onClick()
  assert.equal(harness.query.refreshes, 1)
  assert.equal(harness.calls.length, 0)
  if (status !== "omitted") {
    const parameters = JSON.parse(harness.find(node => node.type === "pre").props.children)
    assert.deepEqual(parameters, { apimName: "customer-apim", probeSubscriptionId: "customer-probe", apiPrincipalIds: ["customer-api-identity"] })
    assert.ok(harness.find(node => node.props?.children === original.scope))
  }
})

for (const [outcome, status, age, expected] of [
  ["passed", "available", 0, "online"],
  ["upstream_error", "unavailable", 0, "offline"],
  ["blocked", "unknown", 0, "blocked"],
  ["passed", "available", 86_400_000, "online"],
]) test(`diagnostic result styling follows its recorded outcome (${expected}, age=${age})`, () => {
  const harness = diagnosticHarness()
  const checkedAt = Date.now() - age
  harness.props.result = { runtime_id: "connection-a", status, outcome,
    check_kind: "model_inference", checked_at: new Date(checkedAt).toISOString(),
    valid_until: new Date(checkedAt + 300_000).toISOString() }
  const result = harness.find(node => node.props?.className === "runtime-diagnostic-result")
  assert.equal(result.props["data-state"], expected)
  assert.equal(harness.find(node => node.type === "strong").props.children, runtimeHealth.runtimeHealthLabels[expected])
  const styles = readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8")
  const success = styles.match(/\.runtime-diagnostic-result\[data-state="online"\]\s*\{([^}]+)\}/)?.[1] ?? ""
  assert.match(success, /color:\s*var\(--success\)/)
  assert.match(success, /background:\s*color-mix\(in srgb, var\(--success\) 5%, var\(--background\)\)/)
  assert.match(success, /border:\s*1px solid color-mix\(in srgb, var\(--success\) 22%, var\(--border\)\)/)
  assert.equal(harness.calls.length, 0)
})

test("diagnostic result uses the same scoped failure label as the connection list", () => {
  const harness = diagnosticHarness()
  harness.props.result = { runtime_id: "connection-a", status: "unavailable", outcome: "upstream_error",
    check_kind: "model_inference", checked_at: new Date().toISOString(), http_status: 500 }
  assert.equal(harness.find(node => node.type === "strong").props.children, "上次调用失败")
  assert.equal(harness.run(), undefined)
})

test("diagnostic configuration changes retain the past outcome without claiming a current pass", () => {
  const diagnostic = { runtime_id: "connection-a", status: "available", outcome: "passed",
    checked_at: "2026-09-24T00:59:00Z", configuration_status: "changed",
    configuration_fingerprint: "v1:" + "a".repeat(64) }
  const runtime = healthRuntime({ diagnostic })
  assert.equal(runtimeHealthState(runtime, healthNow), "changed")
  assert.match(runtimeHealthDescription(runtime), /所选模型推理成功/)
  assert.match(runtimeHealthDescription(runtime), /调用配置已变更/)
  assert.equal(runtimeHealthState(runtime, healthNow + 86_400_000), "changed")
  const harness = diagnosticHarness()
  harness.props.result = { ...diagnostic, checked_at: new Date().toISOString() }
  assert.equal(harness.find(node => node.props?.className === "runtime-diagnostic-result").props["data-state"], "changed")
})

test("legacy or unreadable configuration stays historical and cannot invent a current version", () => {
  const diagnostic = { runtime_id: "connection-a", status: "available", outcome: "passed",
    checked_at: "2026-09-24T00:59:00Z", configuration_status: "current" }
  const legacy = healthRuntime({ health_message: runtimeDiagnosticPrefix + JSON.stringify(diagnostic) })
  assert.equal(runtimeHealthState(legacy, healthNow), "online")
  assert.match(runtimeHealthDescription(legacy), /历史结果未记录配置版本/)
  assert.equal(runtimeHealth.runtimeDiagnostic(legacy).configuration_status, "unverified")
  const unreadable = healthRuntime({ diagnostic: { ...diagnostic,
    configuration_fingerprint: "v1:" + "a".repeat(64), configuration_status: "unverified" } })
  assert.match(runtimeHealthDescription(unreadable), /当前配置暂时无法核对/)
  assert.equal(runtimeHealthState(unreadable, healthNow), "online")
})

test("runtime diagnostic rejects wrong identities and invalid timestamps without breaking inventory", () => {
  const diagnostic = { runtime_id: "other", status: "available", outcome: "passed", checked_at: new Date().toISOString() }
  assert.equal(runtimeHealth.runtimeDiagnostic(healthRuntime({ diagnostic })), null)
  assert.equal(runtimeHealth.runtimeDiagnostic(healthRuntime({ diagnostic: { ...diagnostic, runtime_id: "connection-a", status: "unknown" } })), null)
  const runtime = healthRuntime()
  assert.equal(runtimeWithTelemetryHealth(runtime, [{ request_source: "runtime-health-check", timestamp: "invalid" }]), runtime)
  const record = { request_source: "runtime-health-check", timestamp: "2026-09-24T00:59:00Z", status_code: 503,
    error_message: '{"code":"application_model_not_assigned"}' }
  assert.equal(runtimeWithTelemetryHealth(runtime, [record]).diagnostic.error_code, "application_model_not_assigned")
})

function gatewayAttachmentHarness({ role = "owner", save = async () => ({}), rename = async () => {}, remove = async () => ({}), create = async () => ({}), retryProvision = async () => ({}), bootstrap = async () => { throw new Error("Unexpected bootstrap") }, provisions, provisionError = null, onboardingError = null, operationError = null, locale = "en-US", translateForLocale = async value => value } = {}) {
  const target = id => ({
    target: { gateway_profile_id: id, service_name: id, resource_group: "unit", subscription_id: "unit",
      api_id: "models", api_path: "models", ownership_mode: "adopted", drift_state: "in_sync", capabilities: { self_hosted_gateway: "supported" }, last_inventory_sync_at: null },
    deployments: [], published_models: [], published_model_count: 0, connection_state: "unknown",
    management_endpoint: `https://management.azure.com/${id}`, gateway_endpoint: `https://${id}.azure-api.net/models`,
    configuration_endpoint: `https://${id}.configuration.azure-api.net`,
  })
  const props = { mode: "apim", loading: false, error: null, onTabChange(tab) { props.mode = tab }, onToggleSidebar() {},
    registry: registry({ self_hosted_gateway_enabled: true,
      gateways: [gateway("gateway-a", { name: "First APIM" }), gateway("gateway-b", { name: "Second APIM" })] }),
    topology: { items: [target("gateway-a"), target("gateway-b")], resource_lifecycle_available: true, inventory_sync_available: true },
  }
  const calls = [], creations = [], retries = [], bootstrapCalls = [], downloads = [], queries = [], refreshes = [], syncs = [], renames = [], removals = [], nameUpdates = [], tasks = [], states = new Map()
  let provisionData = provisions
  let effects = []
  let slots, cursor, visited
  const useState = initial => {
    const owner = slots, index = cursor++
    if (!(index in owner)) owner[index] = typeof initial === "function" ? initial() : initial
    return [owner[index], value => { owner[index] = typeof value === "function" ? value(owner[index]) : value }]
  }
  const queryClient = { getQueriesData: () => [], cancelQueries: async () => {},
    setQueryData(key, value) { if (key[1] === "self-hosted-gateway-provisions") provisionData = typeof value === "function" ? value(provisionData) : value },
    invalidateQueries: async value => { refreshes.push(value) } }
  const imports = {
    react: { useState, useEffect(callback, dependencies) {
      const effect = useState({ kind: "effect", dependencies: null, cleanup: null })[0]
      if (!effect.dependencies || dependencies.some((value, index) => !Object.is(value, effect.dependencies[index]))) {
        effect.dependencies = dependencies
        effects.push(() => { effect.cleanup?.(); effect.cleanup = callback() })
      }
    }, useRef: initial => useState({ current: initial })[0], useId: () => `field-${cursor++}` },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@tanstack/react-query": {
      useQueryClient: () => queryClient,
      useQuery: options => { queries.push(options); return { data: options.queryKey?.[1] === "self-hosted-gateway-provisions" ? provisionData
        : options.queryKey?.[1] === "resource-removal-preview" && options.enabled ? {removal_allowed:true,model_count:0,models:[],blockers:[]} : undefined,
        error: options.queryKey?.[1] === "self-hosted-gateway-provisions" ? provisionError
          : options.queryKey?.[1] === "apim-onboarding" ? onboardingError : options.queryKey ? null : operationError } },
      useMutation(options) {
        const mutation = useState({ isPending: false, error: null })[0]
        mutation.reset = () => { mutation.error = null }
        mutation.mutateAsync = variables => {
          mutation.isPending = true
          mutation.variables = variables
          mutation.error = null
          const task = (async () => {
            try { const result = await options.mutationFn(variables); await options.onSuccess?.(result, variables); return result }
            catch (error) { mutation.error = error; throw error }
            finally { mutation.isPending = false; await options.onSettled?.() }
          })()
          tasks.push(task.catch(() => {}))
          return task
        }
        mutation.mutate = variables => { void mutation.mutateAsync(variables).catch(() => {}) }
        return mutation
      },
    },
    "../../data-sources/apim/api": {
      requestGatewayBootstrap: async (id, value, signal) => { bootstrapCalls.push({ id, value, signal }); return bootstrap(id, value, signal) },
      dataSource: {
      addSelfHostedGateway: async (targetId, value) => { calls.push({ targetId, value: JSON.parse(JSON.stringify(value)) }); return save() },
      createSelfHostedGateway: async (targetId, value, requestId) => { creations.push({ targetId, value: JSON.parse(JSON.stringify(value)), requestId }); return create() },
      retrySelfHostedGatewayProvision: async id => { retries.push(id); return retryProvision(id) },
      renameSelfHostedGateway: async (id, name) => { renames.push({ kind: "gateway", id, name }); return rename() },
      renameGateway: async (id, name) => { renames.push({ kind: "apim", id, name }); return rename() },
      removeSelfHostedGateway: async id => { removals.push(id); return remove() },
      requestApimTargetInventorySync: async targetId => { syncs.push(targetId); return { operation: { id: "sync", gateway_profile_id: targetId } } },
    } },
    "../../data-sources/apim/queries": { finopsKeys: { registry: ["registry"], gatewayReleaseOperation: id => [id] }, finopsQueries: { gatewayReleaseOperation: () => ({}) },
      storeGatewayDeploymentName: async (_client, id, name) => { nameUpdates.push({ kind: "gateway", id, name }) },
      storeGatewayName: async (_client, id, name) => { nameUpdates.push({ kind: "apim", id, name }) },
    },
    "../../lib/gateway-forms": gatewayForms,
    "../../lib/gateway-bootstrap": { consumeGatewayBootstrap, downloadGatewayEnvironment: (name, content) => downloads.push({ name, content }) },
    "../../lib/gateway-topology": gatewayTopology,
    "../../providers/auth-provider": { useAuth: () => ({ user: { role } }) },
    "../../providers/timezone-provider": { useTimezone: () => ({ timezone: "UTC" }) },
    "../../locales": { useLocale: () => ({ locale }), translateForLocale },
  }
  const dialogs = {}
  runInNewContext(gatewayDialogModule, { exports: dialogs, URL, crypto: globalThis.crypto, AbortController, AbortSignal,
    require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  imports["./self-hosted-gateway-dialog"] = dialogs
  const exports = {}
  runInNewContext(workspaceModule, { exports, URL, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  const nodes = (value, path) => {
    if (Array.isArray(value)) return value.flatMap((child, index) => nodes(child, `${path}.${child?.key ?? index}`))
    if (!value || typeof value !== "object") return []
    if (typeof value.type !== "function") return [value, ...nodes(value.props?.children, path)]
    const key = `${path}:${value.type.name}`
    visited.add(key)
    if (!states.has(key)) states.set(key, [])
    slots = states.get(key)
    cursor = 0
    return [value, ...nodes(value.type(value.props), key)]
  }
  const all = () => {
    visited = new Set()
    effects = []
    const result = nodes({ type: exports.ApimGatewayWorkspace, props }, "root")
    for (const key of states.keys()) if (!visited.has(key)) {
      for (const value of states.get(key)) if (value?.kind === "effect") value.cleanup?.()
      states.delete(key)
    }
    for (const effect of effects) effect()
    return result
  }
  const find = predicate => all().find(predicate)
  const input = label => [].concat(find(node => node.type?.name === "Field" && node.props.label === label).props.children)
    .find(node => node?.type === "Input")
  const fill = (label, value) => input(label).props.onChange({ target: { value } })
  const submit = () => find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  const attach = (index = 1) => all().filter(node => node.type === "DropdownMenuItem" && node.props.children?.includes("添加自托管网关"))[index]
  const createDialog = () => find(node => node.type?.name === "SelfHostedGatewayCreateDialog")
  const adopt = (index = 1) => { attach(index).props.onClick(); const dialog = createDialog(); dialog.props.onAdopt(dialog.props.initialTargetId) }
  const form = () => find(node => node.type?.name === "GatewayEditor")
  const validDraft = () => {
    fill("网关名称", " Customer gateway ")
    fill("APIM 网关 ID", " native-node ")
    fill("网关端点", "https://gateway.example.test/models")
  }
  return { props, calls, creations, retries, bootstrapCalls, downloads, queries, refreshes, syncs, renames, removals, nameUpdates, all, find, input, fill, submit, attach, adopt, createDialog, form, validDraft,
    setLocale(value) { locale = value },
    setProvisions(value) { provisionData = value },
    async flush() { for (let index = 0; index < tasks.length; index++) await tasks[index] } }
}

test("APIM management authentication follows the selected display locale", async () => {
  const translations = []
  const harness = gatewayAttachmentHarness({ locale: "en", translateForLocale: async (source, locale) => {
    translations.push({ source, locale })
    return locale === "en" ? "System-managed identity" : source
  } })
  harness.find(node => node.type === "button" && Array.isArray(node.props.children)
    && node.props.children.some(child => child === "添加 APIM 实例" || child?.props?.children === "添加 APIM 实例")).props.onClick()
  harness.find(node => node.type === "ApimOnboardingDialog").props.onAdopt()
  harness.all()
  await Promise.resolve()
  assert.equal(harness.input("管理身份验证").props.value, "System-managed identity")
  assert.equal(harness.input("管理身份验证").props.disabled, true)
  harness.setLocale("zh-CN")
  harness.all()
  await Promise.resolve()
  assert.equal(harness.input("管理身份验证").props.value, "系统托管身份")
  assert.deepEqual(translations, [{ source: "系统托管身份", locale: "en" }, { source: "系统托管身份", locale: "zh-CN" }])
})

function gatewayNameHarness(options) {
  const harness = gatewayAttachmentHarness(options)
  harness.props.topology.self_hosted_gateway_name_edit_supported = true
  harness.props.registry.gateway_name_edit_supported = true
  for (const [index, target] of harness.props.topology.items.entries()) {
    target.deployments = [{ deployment: {
      id: `deployment-${index}`, apim_target_id: target.target.gateway_profile_id,
      native_gateway_id: `native-${index}`, display_name: `Gateway ${index}`, kind: "self_hosted",
      endpoint_url: `https://gateway-${index}.example/models`, hosting_mode: "docker", retired_at: null,
      declared_state: "present", reachability_state: "reachable", last_probe_at: "2026-09-22T07:11:27Z",
    }, configuration_endpoint: target.configuration_endpoint, inventory: { associated_api_ids: ["models"] },
    published_models: [{ id: "existing-model", display_name: "Existing model", model_key: "existing" }] }]
  }
  const edit = (index = 1) => harness.all().filter(node => node.type === "button" && node.props["aria-label"] === "编辑网关名称")[index]
  const editor = () => harness.find(node => node.type?.name === "InfrastructureNameEditor")
  const save = () => harness.find(node => node.type === "Button" && node.props.type === "submit")
  return { ...harness, edit, editor, save }
}

for (const mode of ["apim", "gateways"]) test(`Gateway name editor submits the exact deployment from ${mode} view`, async () => {
  const harness = gatewayNameHarness()
  harness.props.mode = mode
  harness.edit().props.onClick()
  assert.equal(harness.input("Turnstile 显示名称").props.value, "Gateway 1")
  assert.equal(harness.editor().props.identity, "native-1 · gateway-b")
  assert.equal(harness.all().filter(node => node.type === "Input").length, 1)
  assert.equal(harness.save().props.disabled, true)
  harness.fill("Turnstile 显示名称", "  Finance VM  ")
  assert.equal(harness.save().props.disabled, false)
  harness.submit()
  await harness.flush()
  assert.deepEqual(harness.renames, [{ kind: "gateway", id: "deployment-1", name: "Finance VM" }])
  assert.deepEqual(harness.nameUpdates, harness.renames)
  assert.equal(harness.editor(), undefined)
  assert.equal(harness.calls.length, 0)
  assert.equal(harness.syncs.length, 0)
  assert.ok(harness.find(node => node.props?.children === "网关显示名称已更新"))
})

test("Gateway name editor locks pending saves and preserves failed drafts for an explicit retry", async () => {
  let rejectSave
  let attempts = 0
  const harness = gatewayNameHarness({ rename: () => ++attempts === 1
    ? new Promise((_resolve, reject) => { rejectSave = reject }) : Promise.resolve() })
  harness.edit().props.onClick()
  harness.fill("Turnstile 显示名称", "Retained name")
  harness.submit()
  assert.equal(harness.input("Turnstile 显示名称").props.disabled, true)
  assert.equal(harness.save().props.disabled, true)
  harness.find(node => node.type === "Dialog").props.onOpenChange(false)
  assert.ok(harness.editor())
  harness.submit()
  assert.equal(harness.renames.length, 1)
  rejectSave(new Error("Name save failed"))
  await harness.flush()
  assert.equal(harness.input("Turnstile 显示名称").props.value, "Retained name")
  assert.equal(harness.save().props.disabled, false)
  assert.match(harness.find(node => node.props?.role === "alert").props.children, /Name save failed$/)
  assert.equal(harness.nameUpdates.length, 0)
  harness.submit()
  await harness.flush()
  assert.equal(harness.renames.length, 2)
  assert.equal(harness.nameUpdates.length, 1)
  assert.equal(harness.editor(), undefined)
})

test("Gateway name editor rejects blank unchanged and long names and discards cancelled drafts", () => {
  const harness = gatewayNameHarness()
  harness.edit().props.onClick()
  for (const name of ["", "   ", " Gateway 1 ", "n".repeat(121)]) {
    harness.fill("Turnstile 显示名称", name)
    assert.equal(harness.save().props.disabled, true)
    harness.submit()
  }
  assert.equal(harness.renames.length, 0)
  harness.fill("Turnstile 显示名称", "Cancelled name")
  harness.find(node => node.type === "Dialog").props.onOpenChange(false)
  assert.equal(harness.editor(), undefined)
  harness.edit().props.onClick()
  assert.equal(harness.input("Turnstile 显示名称").props.value, "Gateway 1")
})

test("Gateway rename guards old backends owner permissions and disappearing resources", () => {
  const member = gatewayNameHarness({ role: "member" })
  assert.equal(member.edit(), undefined)
  assert.equal(member.find(node => node.type === "DropdownMenuItem" && node.props.children?.includes("编辑网关名称")), undefined)
  for (const unavailable of ["legacy", "capability", "lifecycle", "missing", "retired"]) {
    const harness = gatewayNameHarness()
    harness.edit().props.onClick()
    harness.fill("Turnstile 显示名称", "Draft")
    if (unavailable === "legacy") delete harness.props.topology.self_hosted_gateway_name_edit_supported
    if (unavailable === "capability") harness.props.topology.self_hosted_gateway_name_edit_supported = false
    if (unavailable === "lifecycle") harness.props.topology.resource_lifecycle_available = false
    if (unavailable === "missing") harness.props.topology.items.pop()
    if (unavailable === "retired") harness.props.topology.items[1].deployments[0].deployment.retired_at = "2026-09-22T08:00:00Z"
    assert.equal(harness.save().props.disabled, true)
    harness.submit()
    assert.equal(harness.renames.length, 0)
  }
})

test("Gateway rename has mobile menu parity and leaves Managed children uneditable", () => {
  const harness = gatewayNameHarness()
  const target = harness.props.topology.items[0]
  const original = target.deployments[0]
  target.deployments.unshift({ ...original, deployment: { ...original.deployment, id: "managed", native_gateway_id: "managed", kind: "managed" } })
  assert.equal(harness.all().filter(node => node.type === "button" && node.props["aria-label"] === "编辑网关名称").length, 2)
  const menu = harness.all().filter(node => node.type === "DropdownMenuItem" && node.props.children?.includes("编辑网关名称"))
  assert.equal(menu.length, 2)
  menu[1].props.onClick()
  assert.equal(harness.editor().props.identity, "native-1 · gateway-b")
})

test("Gateway deletion queues only its owning APIM synchronization after success", async () => {
  const harness = gatewayNameHarness()
  harness.all().filter(node => node.type === "DropdownMenuItem" && node.props.children?.includes("从 Turnstile 移除网关"))[1].props.onClick()
  const confirmation = harness.find(node => node.type?.name === "RemovalDialog")
  assert.equal(confirmation.props.target.id, "deployment-1")
  confirmation.props.onConfirm()
  await harness.flush()
  assert.deepEqual(harness.removals, ["deployment-1"])
  assert.deepEqual(harness.syncs, ["gateway-b"])
  assert.equal(harness.find(node => node.type?.name === "RemovalDialog"), undefined)
})

test("failed Gateway deletion preserves its dialog and never queues protocol cleanup", async () => {
  const harness = gatewayNameHarness({ remove: async () => { throw new Error("Dependency appeared") } })
  harness.all().filter(node => node.type === "DropdownMenuItem" && node.props.children?.includes("从 Turnstile 移除网关"))[1].props.onClick()
  harness.find(node => node.type?.name === "RemovalDialog").props.onConfirm()
  await harness.flush()
  assert.deepEqual(harness.removals, ["deployment-1"])
  assert.deepEqual(harness.syncs, [])
  assert.ok(harness.find(node => node.type?.name === "RemovalDialog").props.error)
})

test("Gateway more action keeps a persistent rightmost slot without moving other actions", () => {
  const harness = gatewayNameHarness()
  const groups = harness.all().filter(node => node.props?.className === "model-row-actions infrastructure-row-actions gateway-name-actions")
  assert.equal(groups.length, 2)
  for (const group of groups) {
    const actions = group.props.children[0].props.children.filter(Boolean)
    assert.deepEqual(Array.from(actions.slice(0, -1), node => node.props["aria-label"]), ["验证连接", "编辑网关名称", "APIM 绑定"])
    assert.equal(actions.at(-1).type, "DropdownMenu")
    assert.equal(group.props.children[1], false)
  }
  const styles = readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8")
  assert.match(styles, /\.infrastructure-resource-table \.infrastructure-row-actions > \.infrastructure-menu-trigger:not\(:disabled\)\s*\{[^}]*opacity:\s*1;[^}]*pointer-events:\s*auto;/)
  assert.match(styles, /\.infrastructure-resource-table \.infrastructure-row-actions > \.infrastructure-menu-trigger:disabled\s*\{[^}]*opacity:\s*\.5;/)
})

test("shared name dialog keeps APIM name-only editing behavior", async () => {
  const harness = gatewayNameHarness()
  harness.all().find(node => node.type === "button" && node.props["aria-label"] === "编辑 APIM 名称").props.onClick()
  assert.equal(harness.input("Turnstile 显示名称").props.value, "First APIM")
  harness.fill("Turnstile 显示名称", " Updated APIM ")
  harness.submit()
  await harness.flush()
  assert.deepEqual(harness.renames, [{ kind: "apim", id: "gateway-a", name: "Updated APIM" }])
  assert.deepEqual(harness.nameUpdates, harness.renames)
})

for (const kind of ["onboarding", "operation", "topology", "mutation"]) test(`APIM ${kind} error text uses the notice body instead of the icon column`, async () => {
  const failure = new Error(`API 409: Self-hosted Gateway validation returned HTTP 503 ${"details ".repeat(30)}`)
  const harness = gatewayAttachmentHarness({
    onboardingError: kind === "onboarding" ? failure : null,
    operationError: kind === "operation" ? failure : null,
    save: async () => { throw failure },
  })
  if (kind === "topology") harness.props.error = failure
  if (kind === "mutation") {
    harness.adopt()
    harness.validDraft()
    harness.submit()
    await harness.flush()
  }
  const alert = harness.find(node => node.props?.className === "registry-error smh-notice infrastructure-error-notice")
  assert.ok(alert)
  assert.equal(alert.props.role, "alert")
  const children = Array.from(alert.props.children)
  assert.equal(children.length, 2)
  assert.equal(children[0].type, "CircleAlert")
  assert.equal(children[0].props["aria-hidden"], "true")
  assert.equal(children[1].props.className, "smh-notice-copy")
  const content = children[1].props.children.props.children
  assert.ok([].concat(content).join("").includes(failure.message))
  const styles = readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8")
  assert.match(styles, /\.smh-notice\.infrastructure-error-notice\s*\{[^}]*grid-template-columns:\s*16px minmax\(0, 1fr\)/)
  assert.match(styles, /\.smh-notice\.infrastructure-error-notice\s*\{[^}]*align-items:\s*center/)
  assert.match(styles, /\.infrastructure-error-notice \.smh-notice-copy span\s*\{[^}]*white-space:\s*normal;[^}]*overflow-wrap:\s*anywhere/)
})

test("APIM attachment menu locks the non-first target and submits its normalized association", async () => {
  const harness = gatewayAttachmentHarness()
  harness.adopt()
  assert.equal(harness.form().props.initialTargetId, "gateway-b")
  const target = harness.find(node => node.type?.name === "TargetSelect")
  assert.equal(target.props.value, "gateway-b")
  assert.equal(target.props.disabled, true)
  assert.equal(harness.input("配置端点").props.value, "https://gateway-b.configuration.azure-api.net")
  assert.equal(harness.input("配置端点").props.readOnly, true)
  harness.submit()
  assert.equal(harness.calls.length, 0)
  harness.validDraft()
  harness.submit()
  await harness.flush()
  assert.deepEqual(harness.calls, [{ targetId: "gateway-b", value: { native_gateway_id: "native-node", display_name: "Customer gateway",
    endpoint_url: "https://gateway.example.test/models", location: null, hosting_mode: "kubernetes", release_gate: "required" } }])
  assert.equal(harness.form(), undefined)
  assert.equal(harness.refreshes.length, 1)
  assert.deepEqual(harness.syncs, ["gateway-b"])
})

test("APIM attachment pending and failed saves keep the draft for one explicit retry", async () => {
  let rejectSave
  let attempts = 0
  const harness = gatewayAttachmentHarness({ save: () => ++attempts === 1
    ? new Promise((_resolve, reject) => { rejectSave = reject }) : Promise.resolve({}) })
  harness.adopt()
  harness.validDraft()
  harness.submit()
  assert.equal(harness.find(node => node.type === "Button" && node.props.type === "submit").props.disabled, true)
  assert.equal(harness.input("网关名称").props.disabled, true)
  harness.submit()
  assert.equal(harness.calls.length, 1)
  rejectSave(new Error("Association rejected"))
  await harness.flush()
  assert.equal(harness.form().props.initialTargetId, "gateway-b")
  assert.equal(harness.input("网关名称").props.value, " Customer gateway ")
  assert.ok(harness.find(node => node.props?.role === "alert" && String(node.props.children).includes("Association rejected")))
  assert.equal(harness.find(node => node.type === "Button" && node.props.type === "submit").props.disabled, false)
  assert.equal(harness.refreshes.length, 0)
  harness.submit()
  await harness.flush()
  assert.equal(harness.calls.length, 2)
  assert.deepEqual(harness.calls[0], harness.calls[1])
  assert.equal(harness.form(), undefined)
})

test("APIM attachment cancellation resets the draft and global add retains target selection", () => {
  const harness = gatewayAttachmentHarness()
  harness.adopt()
  harness.validDraft()
  harness.find(node => node.type === "Dialog").props.onOpenChange(false)
  assert.equal(harness.form(), undefined)
  harness.props.mode = "gateways"
  harness.find(node => node.type === "button" && node.props.children?.some?.(child => child?.props?.children === "添加网关")).props.onClick()
  assert.ok(harness.createDialog())
  harness.createDialog().props.onAdopt(null)
  assert.equal(harness.form().props.initialTargetId, null)
  assert.equal(harness.input("网关名称").props.value, "")
  const target = harness.find(node => node.type?.name === "TargetSelect")
  assert.equal(target.props.disabled, false)
  target.props.onChange("gateway-b")
  assert.equal(harness.input("配置端点").props.value, "https://gateway-b.configuration.azure-api.net")
  assert.equal(harness.calls.length, 0)
})

test("APIM attachment respects owner, capability and target disappearance guards", () => {
  const member = gatewayAttachmentHarness({ role: "member" })
  assert.equal(member.attach(), undefined)
  for (const unavailable of ["feature", "lifecycle", "tier", "missing"]) {
    const harness = gatewayAttachmentHarness()
    harness.adopt()
    harness.validDraft()
    if (unavailable === "feature") harness.props.registry.self_hosted_gateway_enabled = false
    if (unavailable === "lifecycle") harness.props.topology.resource_lifecycle_available = false
    if (unavailable === "tier") harness.props.topology.items[1].target.capabilities.self_hosted_gateway = "unsupported"
    if (unavailable === "missing") harness.props.topology.items.pop()
    assert.equal(harness.find(node => node.type === "Button" && node.props.type === "submit").props.disabled, true)
    harness.submit()
    assert.equal(harness.calls.length, 0)
    assert.equal(harness.find(node => node.type?.name === "TargetSelect").props.value, "gateway-b")
    if (unavailable !== "missing") assert.equal(harness.attach().props.disabled, true)
  }
})

test("APIM attachment validates endpoint and native identity before allowing a save", () => {
  const harness = gatewayAttachmentHarness()
  harness.adopt()
  harness.validDraft()
  for (const endpoint of ["http://gateway.example.test", "https://user:password@gateway.example.test", "https://gateway.example.test?token=value", "https://gateway.example.test#part"]) {
    harness.fill("网关端点", endpoint)
    assert.equal(harness.input("网关端点").props["aria-invalid"], true)
    assert.equal(harness.find(node => node.type?.name === "Field" && node.props.label === "网关端点").props.children.type, "Input")
    assert.ok(harness.find(node => node.props?.id === harness.input("网关端点").props["aria-describedby"]))
    harness.submit()
    assert.equal(harness.calls.length, 0)
  }
  harness.fill("网关端点", "https://gateway.example.test/models")
  harness.fill("APIM 网关 ID", "managed")
  harness.submit()
  assert.equal(harness.calls.length, 0)
})

const provisioningOperation = (id = "task-a", targetId = "gateway-a", status = "failed") => ({
  id, gateway_profile_id: targetId, operation_kind: "self_hosted_gateway_provision", status,
  semantic_preview: { native_gateway_id: `native-${id}`, deployment_id: `deployment-${id}`,
    configuration: { display_name: `Edge ${id}`, endpoint_url: `https://${id}.example/models`, hosting_mode: "docker" } },
  checkpoint: {}, created_at: "2026-09-26T10:00:00Z", updated_at: "2026-09-26T10:00:00Z",
  worker_available: true, error_message: status === "failed" ? `Failure ${id}` : null,
})

test("default Gateway creation keeps one request identity after a lost response and never calls association", async () => {
  let rejectCreate, attempts = 0
  const operation = provisioningOperation("task-b", "gateway-b", "queued")
  const harness = gatewayAttachmentHarness({ create: () => ++attempts === 1
    ? new Promise((_resolve, reject) => { rejectCreate = reject }) : Promise.resolve({ operation }) })
  harness.props.topology.self_hosted_gateway_provisioning_available = true
  harness.attach().props.onClick()
  assert.equal(harness.createDialog().props.initialTargetId, "gateway-b")
  assert.equal(harness.form(), undefined)
  assert.equal(harness.find(node => node.type === "Input" && node.props.name === "native_gateway_id"), undefined)
  assert.ok(harness.find(node => node.type === "Select" && node.props.value === "docker"))
  for (const [name, value] of [["display_name", " New edge "], ["endpoint_url", "https://edge.example/models"]]) {
    harness.find(node => node.type === "Input" && node.props.name === name).props.onChange({ target: { value } })
  }
  assert.equal(harness.find(node => node.type === "Button" && node.props.type === "submit").props.disabled, true)
  harness.find(node => node.type === "Checkbox").props.onCheckedChange(true)
  harness.submit(); harness.submit()
  assert.equal(harness.creations.length, 1)
  assert.equal(harness.find(node => node.type === "Input" && node.props.name === "display_name").props.disabled, true)
  rejectCreate(new Error("Response lost"))
  await harness.flush()
  assert.equal(harness.creations.length, 1)
  assert.ok(harness.createDialog())
  assert.equal(harness.find(node => node.type === "Input" && node.props.name === "display_name").props.disabled, true)
  await harness.submit(); await harness.flush()
  assert.equal(harness.creations.length, 2)
  assert.deepEqual(harness.creations[0], harness.creations[1])
  assert.equal(harness.creations[0].value.hosting_mode, "docker")
  assert.equal(harness.creations[0].value.display_name, "New edge")
  assert.match(harness.creations[0].requestId, /^[a-f\d-]{36}$/)
  assert.equal(harness.calls.length, 0)
  assert.equal(harness.createDialog(), undefined)
  assert.equal(harness.props.mode, "gateways")
  assert.ok(harness.find(node => node.props?.["data-operation-id"] === "task-b"))
})

test("Gateway tasks render only inside the Gateway table with matching counts and search", () => {
  const items = [provisioningOperation(), provisioningOperation("task-b", "gateway-b", "queued")]
  const harness = gatewayAttachmentHarness({ provisions: { available: true, items } })
  harness.props.topology.self_hosted_gateway_provisioning_available = true
  assert.equal(harness.all().filter(node => node.props?.["data-operation-id"]).length, 0)
  assert.equal(harness.queries.filter(query => query.queryKey?.[1] === "self-hosted-gateway-provisions").at(-1).enabled, false)
  harness.props.mode = "gateways"
  assert.equal(harness.all().filter(node => node.props?.["data-operation-id"]).length, 2)
  assert.equal(harness.queries.filter(query => query.queryKey?.[1] === "self-hosted-gateway-provisions").at(-1).enabled, true)
  const table = harness.find(node => node.type?.name === "GatewayTable")
  assert.equal(table.props.provisionItems.length, 2)
  assert.equal(harness.find(node => node.props?.className === "model-table-footer").props.children, "共 2 个网关")
  harness.find(node => node.type === "ExpandableSearch").props.onChange("task-b.example")
  assert.equal(harness.all().filter(node => node.props?.["data-operation-id"]).length, 1)
  assert.equal(harness.find(node => node.props?.className === "model-table-footer").props.children, "共 1 个网关")
  assert.equal(harness.find(node => node.type?.name === "InfrastructureEmpty"), undefined)
  harness.props.mode = "apim"
  harness.setProvisions({ available: true, items: items.map(item => ({ ...item, status: "succeeded" })) })
  assert.equal(harness.all().filter(node => node.props?.["data-operation-id"]).length, 0)
  assert.equal(harness.find(node => node.type?.name === "SelfHostedGatewayBootstrapDialog"), undefined)
  assert.equal(harness.find(node => node.props?.className === "registry-notice smh-notice"), undefined)
  harness.props.mode = "gateways"
  assert.equal(harness.all().filter(node => node.props?.["data-operation-id"]).length, 0)
})

test("Gateway task retries and failures are isolated to their owning rows", async () => {
  let rejectFirst
  const first = provisioningOperation(), second = provisioningOperation("task-b", "gateway-b")
  const harness = gatewayAttachmentHarness({ provisions: { available: true, items: [first, second] },
    retryProvision: id => id === first.id ? new Promise((_resolve, reject) => { rejectFirst = reject })
      : Promise.resolve({ operation: { ...second, status: "queued", error_message: null } }) })
  harness.props.topology.self_hosted_gateway_provisioning_available = true
  harness.props.mode = "gateways"
  const buttons = () => harness.all().filter(node => node.props?.["aria-label"] === "重试创建")
  void buttons()[0].props.onClick()
  assert.equal(buttons()[0].props.disabled, true)
  assert.equal(buttons()[1].props.disabled, false)
  await buttons()[1].props.onClick()
  rejectFirst(new Error("Only first retry failed"))
  await harness.flush()
  assert.deepEqual(harness.retries, ["task-a", "task-b"])
  const alerts = harness.all().filter(node => node.props?.className === "gateway-provision-error")
  assert.equal(alerts.length, 1)
  assert.equal(alerts[0].props.children, "Only first retry failed")
  assert.equal(harness.find(node => node.props?.className === "registry-error smh-notice infrastructure-error-notice"), undefined)
})

test("old backends and Members do not query Gateway creation endpoints", () => {
  for (const role of ["owner", "member"]) {
    const harness = gatewayAttachmentHarness({ role })
    harness.props.mode = "gateways"
    if (role === "member") harness.props.topology.self_hosted_gateway_provisioning_available = true
    harness.all()
    assert.equal(harness.queries.filter(query => query.queryKey?.[1] === "self-hosted-gateway-provisions").at(-1).enabled, false)
  }
})

for (const exit of ["close", "tab-change"]) test(`Gateway bootstrap discards late credentials on ${exit}`, async () => {
  let resolveBootstrap
  const harness = gatewayNameHarness({ bootstrap: () => new Promise(resolve => { resolveBootstrap = resolve }) })
  harness.props.mode = "gateways"
  harness.props.topology.self_hosted_gateway_provisioning_available = true
  harness.props.topology.items[0].deployments[0].deployment.provisioning_operation_id = "task-a"
  harness.all().find(node => node.type === "DropdownMenuItem" && node.props.children?.includes("启动配置与续期")).props.onClick()
  assert.equal(harness.bootstrapCalls.length, 0)
  const generate = () => harness.find(node => node.type === "Button" && node.props.children?.includes?.("签发并下载配置"))
  assert.equal(generate().props.disabled, true)
  harness.all().find(node => node.type === "Checkbox").props.onCheckedChange(true)
  generate().props.onClick()
  assert.equal(harness.bootstrapCalls.length, 1)
  if (exit === "close") harness.find(node => node.type === "Dialog").props.onOpenChange(false)
  else harness.props.mode = "apim"
  harness.all()
  assert.equal(harness.bootstrapCalls[0].signal.aborted, true)
  const late = { environment_file: "unit-sensitive-late-config" }
  resolveBootstrap(late)
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(late.environment_file, "")
  assert.equal(harness.downloads.length, 0)
  assert.equal(harness.find(node => node.type?.name === "SelfHostedGatewayBootstrapDialog"), undefined)
  assert.equal(JSON.stringify(harness.all()).includes("unit-sensitive-late-config"), false)
})

test("Gateway bootstrap downloads once and renders only safe metadata outside query caches", async () => {
  const native = `turnstile-${"a".repeat(32)}`
  const payload = { deployment_id: "deployment-0", native_gateway_id: native,
    configuration_endpoint: "https://unit.configuration.azure-api.net", expires_at: new Date(Date.now() + 3600000).toISOString(),
    image: "mcr.microsoft.com/azure-api-management/gateway:2.12.1", environment_file_name: `${native}.env`,
    environment_file: `config.service.auth=GatewayKey ${native}&unit-sensitive-token\n`,
    docker_command: `docker run --env-file ./${native}.env`, renewal_command: `docker stop ${native}` }
  const harness = gatewayNameHarness({ bootstrap: async () => payload })
  harness.props.mode = "gateways"
  harness.props.topology.self_hosted_gateway_provisioning_available = true
  harness.props.topology.items[0].deployments[0].deployment.provisioning_operation_id = "task-a"
  harness.all().find(node => node.type === "DropdownMenuItem" && node.props.children?.includes("启动配置与续期")).props.onClick()
  harness.all().find(node => node.type === "Checkbox").props.onCheckedChange(true)
  const generate = harness.find(node => node.type === "Button" && node.props.children?.includes?.("签发并下载配置"))
  generate.props.onClick(); generate.props.onClick()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(harness.bootstrapCalls.length, 1)
  assert.equal(harness.bootstrapCalls[0].value.expires_in_hours, 1)
  assert.equal(harness.bootstrapCalls[0].value.trusted_reverse_proxy, false)
  assert.equal(harness.downloads.length, 1)
  assert.match(harness.downloads[0].content, /unit-sensitive-token/)
  assert.equal(payload.environment_file, "")
  assert.equal(JSON.stringify(harness.all()).includes("unit-sensitive-token"), false)
  assert.ok(harness.find(node => node.type === "pre" && node.props.children.includes("--env-file")))
  assert.equal(harness.queries.some(query => JSON.stringify(query.queryKey ?? []).includes("bootstrap")), false)
})

for (const failure of ["http", "cache", "invalid-json"]) test(`Gateway bootstrap transport sanitizes ${failure} without reading error text`, async () => {
  const exports = {}, clientExports = {}, calls = []
  const response = { ok: failure !== "http", status: failure === "http" ? 502 : 200,
    headers: { get: name => name === "content-type" ? "application/json" : failure === "cache" ? "public" : "no-store, private" },
    body: { async cancel() {} }, async json() { throw new Error("unit-sensitive-response") },
    async text() { assert.fail("Bootstrap must not read raw error text") } }
  runInNewContext(apiClientModule, { exports: clientExports, fetch: async (path, init) => { calls.push({ path, init }); return response } })
  runInNewContext(apiModule, { exports, URLSearchParams,
    require: name => name === "../../api/client" ? clientExports
      : { getIntlLocale: () => "en-US" } })
  await assert.rejects(exports.requestGatewayBootstrap("deployment", { endpoint_control_confirmed: true }, new AbortController().signal),
    error => error.message === "Sensitive configuration unavailable" && !error.message.includes("unit-sensitive"))
  assert.equal(calls.length, 1)
  assert.equal(calls[0].init.cache, "no-store")
  assert.equal(calls[0].init.redirect, "error")
  assert.equal(calls[0].init.credentials, "include")
})

function inventoryHarness(request) {
  const api = {}
  const apiImports = {
    "../../api/client": { request, writeJson: () => { throw new Error("Unexpected write") }, ApiError: Error },
    "../../locales/index": { getIntlLocale: () => "en-US" },
  }
  runInNewContext(apiModule, { exports: api, URLSearchParams, require: name => {
    assert.ok(name in apiImports, name)
    return apiImports[name]
  } })
  const queries = {}
  const queryImports = {
    "@tanstack/react-query": queryLibrary,
    "../../api/cache-policies": { cachePolicies },
    "../../lib/model-availability": { availabilityRefetchInterval },
    "../../lib/timezone": { resolveTimezone: () => "UTC" },
    "../../lib/usage-filter-scope": usageFilterScope,
    "./api": api,
  }
  runInNewContext(queriesModule, { exports: queries, require: name => {
    assert.ok(name in queryImports, name)
    return queryImports[name]
  } })
  return { ...api, ...queries }
}

function storedTopology() {
  return { total: 1, resource_lifecycle_available: true, inventory_sync_available: true,
    items: [{ target: { gateway_profile_id: "gateway-a", service_name: "unit-apim",
      arm_resource_id: "/unit-apim", drift_state: "in_sync", last_inventory_sync_at: "2026-09-01T00:00:00Z" },
    published_models: [], deployments: [{ deployment: { id: "self-hosted", kind: "self_hosted",
      native_gateway_id: "unit-node", retired_at: null, endpoint_url: "https://unit.example/models" },
    inventory: { synced_at: "2026-09-01T00:00:00Z" }, published_models: [] }] }] }
}

test("fixed usage queries preserve one exact model window across all aggregate readers", async () => {
  const calls = []
  const { finopsQueries, withWindowDays } = inventoryHarness(async path => { calls.push(new URL(path, "https://unit.example")); return { items: [], points: [] } })
  const filters = { from: "2026-09-01T00:00:00.000Z", to: "2026-09-08T00:00:00.000Z", model_id: "unit-model" }
  const options = [finopsQueries.executiveOverview(filters, "fixed"),
    finopsQueries.distribution(filters, "user", undefined, "fixed"),
    finopsQueries.distribution(filters, "department", undefined, "fixed"),
    finopsQueries.usageActivity(filters, "hour", "Asia/Shanghai", "fixed"),
    finopsQueries.requests(filters, "fixed")]
  for (const option of options) await option.queryFn()
  assert.equal(calls.length, 5)
  for (const address of calls) {
    assert.equal(address.searchParams.get("model_id"), filters.model_id)
    assert.equal(address.searchParams.get("from"), filters.from)
    assert.equal(address.searchParams.get("to"), filters.to)
  }
  const heatmap = withWindowDays(filters, 182, "fixed")
  assert.equal(heatmap.model_id, filters.model_id)
  assert.equal(heatmap.to, filters.to)
  assert.equal(Date.parse(heatmap.to) - Date.parse(heatmap.from), 182 * 86400000)
  const shifted = { ...filters, from: "2026-09-02T00:00:00.000Z", to: "2026-09-09T00:00:00.000Z" }
  assert.notEqual(JSON.stringify(options[0].queryKey), JSON.stringify(finopsQueries.executiveOverview(shifted, "fixed").queryKey))
  assert.notEqual(JSON.stringify(options[0].queryKey), JSON.stringify(finopsQueries.executiveOverview({ ...filters, model_id: "other" }, "fixed").queryKey))
  const recent = finopsQueries.requests(filters, "fixed", 10)
  assert.notEqual(JSON.stringify(recent.queryKey), JSON.stringify(options[4].queryKey))
  assert.equal(calls[4].searchParams.get("limit"), "200")
  await recent.queryFn()
  assert.equal(calls[5].searchParams.get("limit"), "10")
  assert.equal(calls[5].searchParams.get("model_id"), filters.model_id)
})

test("rolling usage query defaults retain their existing day-based cache and fresh dates", async () => {
  const calls = []
  const { finopsQueries } = inventoryHarness(async path => { calls.push(new URL(path, "https://unit.example")); return {} })
  const filters = { from: "2026-01-01T00:00:00.000Z", to: "2026-01-08T00:00:00.000Z", model_id: "unit-model" }
  const shifted = { ...filters, from: "2026-02-01T00:00:00.000Z", to: "2026-02-08T00:00:00.000Z" }
  const original = finopsQueries.executiveOverview(filters)
  assert.equal(JSON.stringify(original.queryKey), JSON.stringify(finopsQueries.executiveOverview(shifted).queryKey))
  assert.notEqual(JSON.stringify(original.queryKey), JSON.stringify(finopsQueries.executiveOverview(filters, "fixed").queryKey))
  await original.queryFn()
  assert.notEqual(calls[0].searchParams.get("from"), filters.from)
  assert.equal(calls[0].searchParams.get("model_id"), filters.model_id)
  assert.equal(Date.parse(calls[0].searchParams.get("to")) - Date.parse(calls[0].searchParams.get("from")), 7 * 86400000)
})

test("unified registry consumes stored topology in one request", async () => {
  const calls = []
  const { dataSource } = inventoryHarness(async path => {
    calls.push(path)
    assert.equal(path, "/api/v1/model-management")
    return registry({ apim_target_topology_available: true, apim_target_topology: storedTopology() })
  })
  const result = await dataSource.registry()
  assert.equal(calls.length, 1)
  assert.equal(result.apim_target_topology.total, 1)
  assert.equal(result.apim_target_topology.items[0].self_hosted_gateway_count, 1)
  assert.equal(result.apim_target_topology.items[0].target.last_inventory_sync_at, "2026-09-01T00:00:00Z")
  assert.equal(result.apim_target_topology.items[0].deployments[0].inventory.synced_at, "2026-09-01T00:00:00Z")
})

test("unified registry waits for legacy persisted topology before publishing any directory", async () => {
  const calls = []
  let releaseTopology
  let topologyRequested
  const started = new Promise(resolve => { topologyRequested = resolve })
  const { dataSource } = inventoryHarness(async path => {
    calls.push(path)
    if (path === "/api/v1/model-management") return registry({ apim_target_topology_available: true })
    assert.equal(path, "/api/v1/model-management/apim-targets")
    topologyRequested()
    return new Promise(resolve => { releaseTopology = resolve })
  })
  let completed = false
  const pending = dataSource.registry().then(result => { completed = true; return result })
  await started
  assert.equal(completed, false)
  releaseTopology(storedTopology())
  const result = await pending
  assert.equal(result.runtimes.length, 4)
  assert.equal(result.apim_target_topology.total, 1)
  assert.deepEqual(calls, ["/api/v1/model-management", "/api/v1/model-management/apim-targets"])
})

test("unified registry never queries disabled topology or invents a successful empty inventory", async () => {
  for (const available of [false, undefined]) {
    const calls = []
    const { dataSource } = inventoryHarness(async path => {
      calls.push(path)
      assert.equal(path, "/api/v1/model-management")
      return registry({ apim_target_topology_available: available })
    })
    assert.equal((await dataSource.registry()).apim_target_topology, null)
    assert.equal(calls.length, 1)
  }
  const { dataSource } = inventoryHarness(async path => {
    if (path === "/api/v1/model-management") return registry({ apim_target_topology_available: true })
    throw new Error("Stored inventory read failed")
  })
  await assert.rejects(dataSource.registry(), /Stored inventory read failed/)
})

test("registry and topology observers share in-flight reads, cache and refresh", async () => {
  let reads = 0
  const harness = inventoryHarness(async path => {
    assert.equal(path, "/api/v1/model-management")
    reads += 1
    return registry({ apim_target_topology_available: true, apim_target_topology: storedTopology() })
  })
  const client = new queryLibrary.QueryClient({ defaultOptions: { queries: { retry: false } } })
  const registryOptions = harness.finopsQueries.registry()
  const topologyOptions = harness.finopsQueries.apimTargetTopologies()
  const observer = new queryLibrary.QueryObserver(client, topologyOptions)
  try {
    await Promise.all([client.fetchQuery(registryOptions), client.fetchQuery(topologyOptions)])
    assert.equal(reads, 1)
    assert.equal(client.getQueryCache().getAll().length, 1)
    const unsubscribe = observer.subscribe(() => {})
    assert.equal(observer.getCurrentResult().data.total, 1)
    await client.fetchQuery(topologyOptions)
    assert.equal(reads, 1)
    await client.invalidateQueries({ queryKey: harness.finopsKeys.registry })
    assert.equal(reads, 2)
    assert.equal(observer.getCurrentResult().data.total, 1)
    unsubscribe()
  } finally { observer.destroy(); client.clear() }
})

test("legacy save retains visible inventory until refresh and feature-off never inherits it", async () => {
  const harness = inventoryHarness(async () => { throw new Error("No reads expected") })
  const client = new queryLibrary.QueryClient()
  const original = registry({ apim_target_topology_available: true, apim_target_topology: storedTopology() })
  try {
    client.setQueryData(harness.finopsKeys.registry, original)
    await harness.storeRegistry(client, registry({ apim_target_topology_available: true, models: [{ id: "saved" }] }))
    const retained = client.getQueryData(harness.finopsKeys.registry)
    assert.equal(retained.models[0].id, "saved")
    assert.equal(retained.apim_target_topology.total, 1)
    assert.equal(client.getQueryState(harness.finopsKeys.registry).isInvalidated, true)
    await harness.storeRegistry(client, registry({ apim_target_topology_available: false }))
    assert.equal(client.getQueryData(harness.finopsKeys.registry).apim_target_topology, undefined)
    assert.equal(client.getQueryState(harness.finopsKeys.registry).isInvalidated, false)
  } finally { client.clear() }
})

test("confirmed diagnostic survives an older inventory response", async () => {
  let releaseRead, readStarted
  const started = new Promise(resolve => { readStarted = resolve })
  const original = registry()
  const harness = inventoryHarness(async () => { readStarted(); return new Promise(resolve => { releaseRead = resolve }) })
  const client = new queryLibrary.QueryClient({ defaultOptions: { queries: { retry: false } } })
  const diagnostic = { runtime_id: "a-first", status: "available", outcome: "passed", checked_at: "2026-09-24T01:00:00Z" }
  try {
    client.setQueryData(harness.finopsKeys.registry, original)
    const pending = client.fetchQuery({ ...harness.finopsQueries.registry(), staleTime: 0 }).catch(error => error)
    await started
    await harness.storeRegistry(client, current => ({ ...current, runtimes: current.runtimes.map(runtime =>
      runtime.id === diagnostic.runtime_id ? { ...runtime, diagnostic } : runtime) }))
    releaseRead(original)
    await pending
    assert.deepEqual(client.getQueryData(harness.finopsKeys.registry).runtimes[0].diagnostic, diagnostic)
  } finally { client.clear() }
})

for (const embedded of [false, true]) test(`saved registry cannot be overwritten by an older query (embedded=${embedded})`, async () => {
  let releaseRead
  let readStarted
  const started = new Promise(resolve => { readStarted = resolve })
  const original = registry({ apim_target_topology_available: true, apim_target_topology: storedTopology(),
    gateways: [gateway("gateway-a", { name: "Original name" })] })
  const harness = inventoryHarness(async path => {
    assert.equal(path, "/api/v1/model-management")
    readStarted()
    return new Promise(resolve => { releaseRead = resolve })
  })
  const client = new queryLibrary.QueryClient({ defaultOptions: { queries: { retry: false } } })
  try {
    client.setQueryData(harness.finopsKeys.registry, original)
    const pending = client.fetchQuery({ ...harness.finopsQueries.registry(), staleTime: 0 }).catch(error => error)
    await started
    const saved = registry({ apim_target_topology_available: true,
      ...(embedded ? { apim_target_topology: storedTopology() } : {}),
      gateways: [gateway("gateway-a", { name: "Saved name" })] })
    await harness.storeRegistry(client, saved)
    assert.equal(client.getQueryData(harness.finopsKeys.registry).gateways[0].name, "Saved name")
    releaseRead(original)
    await pending
    assert.equal(client.getQueryData(harness.finopsKeys.registry).gateways[0].name, "Saved name")
    assert.equal(client.getQueryData(harness.finopsKeys.registry).apim_target_topology.total, 1)
  } finally { client.clear() }
})

for (const minimal of [false, true]) test(`name client accepts minimal acknowledgement and older full responses (minimal=${minimal})`, async () => {
  const calls = []
  const harness = inventoryHarness(async (path, options) => {
    calls.push(path)
    assert.equal(path, "/api/v1/model-management/gateways/unit%2Fgateway/name")
    assert.equal(options.method, "PATCH")
    assert.equal(options.headers.Prefer, "return=minimal")
    assert.equal(options.headers["content-type"], "application/json")
    assert.deepEqual(JSON.parse(options.body), { name: "Saved name" })
    return minimal ? undefined : registry({ gateways: [gateway("gateway-a", { name: "Saved name" })] })
  })
  const result = await harness.dataSource.renameGateway("unit/gateway", "Saved name")
  assert.equal(calls.length, 1)
  if (minimal) assert.equal(result, undefined)
  else assert.equal(result.gateways[0].name, "Saved name")
})

test("self-hosted name client only patches the display name and accepts 204", async () => {
  const calls = []
  const harness = inventoryHarness(async (path, options) => {
    calls.push(path)
    assert.equal(path, "/api/v1/model-management/self-hosted-gateways/unit%2Fdeployment/name")
    assert.equal(options.method, "PATCH")
    assert.equal(options.headers["content-type"], "application/json")
    assert.deepEqual(JSON.parse(options.body), { display_name: "Finance VM" })
    return undefined
  })
  assert.equal(await harness.dataSource.renameSelfHostedGateway("unit/deployment", "Finance VM"), undefined)
  assert.equal(calls.length, 1)
})

test("self-hosted name acknowledgement preserves identity and survives failed background refresh", async () => {
  const harness = inventoryHarness(async () => { throw new Error("Inventory refresh unavailable") })
  const client = new queryLibrary.QueryClient({ defaultOptions: { queries: { retry: false } } })
  const topology = storedTopology()
  const entry = topology.items[0].deployments[0]
  Object.assign(entry.deployment, { display_name: "Original VM", reachability_state: "reachable", last_probe_at: "2026-09-22T07:11:27Z" })
  topology.items[0].deployments.push({ ...entry, deployment: { ...entry.deployment, id: "other-node", display_name: "Other VM" } })
  const original = registry({ apim_target_topology_available: true, apim_target_topology: topology })
  const before = structuredClone(original)
  const observer = new queryLibrary.QueryObserver(client, harness.finopsQueries.registry())
  let refreshFailed
  const failed = new Promise(resolve => { refreshFailed = resolve })
  try {
    client.setQueryData(harness.finopsKeys.registry, original)
    const unsubscribe = observer.subscribe(result => { if (result.isError) refreshFailed() })
    await harness.storeGatewayDeploymentName(client, "self-hosted", "  Finance VM  ")
    await failed
    const current = client.getQueryData(harness.finopsKeys.registry)
    const saved = current.apim_target_topology.items[0].deployments[0]
    assert.equal(saved.deployment.display_name, "Finance VM")
    assert.deepEqual({ ...saved.deployment, display_name: "Original VM" }, entry.deployment)
    assert.equal(saved.inventory, entry.inventory)
    assert.equal(saved.published_models, entry.published_models)
    assert.equal(current.apim_target_topology.items[0].deployments[1].deployment.display_name, "Other VM")
    assert.equal(current.gateways, original.gateways)
    assert.equal(current.runtimes, original.runtimes)
    assert.deepEqual(original, before)
    unsubscribe()
  } finally { observer.destroy(); client.clear() }
})

test("self-hosted name acknowledgement cancels older reads without inventing missing topology", async () => {
  let releaseRead
  let readStarted
  const started = new Promise(resolve => { readStarted = resolve })
  const harness = inventoryHarness(async () => {
    readStarted()
    return new Promise(resolve => { releaseRead = resolve })
  })
  const client = new queryLibrary.QueryClient({ defaultOptions: { queries: { retry: false } } })
  const original = registry({ apim_target_topology_available: true, apim_target_topology: storedTopology() })
  try {
    await harness.storeGatewayDeploymentName(client, "self-hosted", "Saved")
    assert.equal(client.getQueryData(harness.finopsKeys.registry), undefined)
    client.setQueryData(harness.finopsKeys.registry, original)
    const pending = client.fetchQuery({ ...harness.finopsQueries.registry(), staleTime: 0 }).catch(error => error)
    await started
    await harness.storeGatewayDeploymentName(client, "self-hosted", "Saved")
    releaseRead(original)
    await pending
    assert.equal(client.getQueryData(harness.finopsKeys.registry).apim_target_topology.items[0].deployments[0].deployment.display_name, "Saved")
    assert.equal(client.getQueryState(harness.finopsKeys.registry).isInvalidated, true)
  } finally { client.clear() }
})

test("name acknowledgement preserves inventory and survives a failed background refresh", async () => {
  const harness = inventoryHarness(async () => { throw new Error("Inventory refresh unavailable") })
  const client = new queryLibrary.QueryClient({ defaultOptions: { queries: { retry: false } } })
  const original = registry({ apim_target_topology_available: true, apim_target_topology: storedTopology(),
    gateways: [gateway("gateway-a", { name: "Original name", updated_at: "original-time" }), gateway("gateway-b", { name: "Other name" })],
    runtimes: [connection("first", "gateway-a", { gateway_name: "Original name" }), connection("other", "gateway-b", { gateway_name: "Other name" })] })
  const before = structuredClone(original)
  const observer = new queryLibrary.QueryObserver(client, harness.finopsQueries.registry())
  let refreshFailed
  const failed = new Promise(resolve => { refreshFailed = resolve })
  try {
    client.setQueryData(harness.finopsKeys.registry, original)
    const unsubscribe = observer.subscribe(result => { if (result.isError) refreshFailed() })
    await harness.storeGatewayName(client, "gateway-a", "Saved name")
    assert.equal(client.getQueryData(harness.finopsKeys.registry).gateways[0].name, "Saved name")
    await failed
    const current = client.getQueryData(harness.finopsKeys.registry)
    assert.equal(current.gateways[0].name, "Saved name")
    assert.equal(current.gateways[0].updated_at, "original-time")
    assert.equal(current.gateways[1].name, "Other name")
    assert.equal(current.runtimes[0].gateway_name, "Saved name")
    assert.equal(current.runtimes[1].gateway_name, "Other name")
    assert.equal(current.apim_target_topology, original.apim_target_topology)
    assert.deepEqual(original, before)
    unsubscribe()
  } finally { observer.destroy(); client.clear() }
})

test("name acknowledgement discards older reads without inventing missing inventory", async () => {
  let releaseRead
  let readStarted
  const started = new Promise(resolve => { readStarted = resolve })
  const original = registry({ gateways: [gateway("gateway-a", { name: "Original name" })] })
  const harness = inventoryHarness(async () => {
    readStarted()
    return new Promise(resolve => { releaseRead = resolve })
  })
  const client = new queryLibrary.QueryClient({ defaultOptions: { queries: { retry: false } } })
  try {
    await harness.storeGatewayName(client, "gateway-a", "Saved name")
    assert.equal(client.getQueryData(harness.finopsKeys.registry), undefined)
    client.setQueryData(harness.finopsKeys.registry, original)
    const pending = client.fetchQuery({ ...harness.finopsQueries.registry(), staleTime: 0 }).catch(error => error)
    await started
    await harness.storeGatewayName(client, "gateway-a", "  Saved name  ")
    releaseRead(original)
    await pending
    assert.equal(client.getQueryData(harness.finopsKeys.registry).gateways[0].name, "Saved name")
    assert.equal(client.getQueryState(harness.finopsKeys.registry).isInvalidated, true)
  } finally { client.clear() }
})

test("only the selected gateway's connections are offered, across providers", () => {
  const data = registry()
  assert.deepEqual(publicationConnections(data, "gateway-a").map((item) => item.id),
    ["a-first", "a-default", "a-other-provider"])
  assert.deepEqual(publicationConnections(data, "gateway-b").map((item) => item.id), ["b-default"])
})

test("existing default is preferred and an explicit valid choice is retained", () => {
  const connections = publicationConnections(registry(), "gateway-a")
  assert.equal(preferredPublicationConnection(connections)?.id, "a-default")
  assert.equal(preferredPublicationConnection(connections, "a-first")?.id, "a-first")
  assert.equal(preferredPublicationConnection(connections.filter((item) => !item.is_default))?.id,
    "a-first")
})

test("switching gateways cannot retain a connection owned by the old gateway", () => {
  const data = registry()
  const next = publicationConnections(data, "gateway-b")
  assert.equal(preferredPublicationConnection(next, "a-default")?.id, "b-default")
  assert.equal(preferredPublicationConnection(next, "a-other-provider")?.id, "b-default")
})

test("an empty gateway leaves selection empty without creating or borrowing a connection", () => {
  const data = registry({ gateways: [gateway("gateway-a"), gateway("empty")] })
  const connections = publicationConnections(data, "empty")
  assert.deepEqual(connections, [])
  assert.equal(preferredPublicationConnection(connections, "a-default"), undefined)
  assert.equal(preferredPublicationConnection(connections, "a-default")?.id ?? "", "")
})

test("unknown, disabled and non-APIM gateways have no publication connections", () => {
  for (const overrides of [{ enabled: false }, { implementation: "direct" }]) {
    const data = registry({ gateways: [gateway("gateway-a", overrides)] })
    assert.deepEqual(publicationConnections(data, "gateway-a"), [])
  }
  assert.deepEqual(publicationConnections(registry(), "missing"), [])
})

test("disabled providers, disabled connections and missing providers are excluded", () => {
  const data = registry({
    providers: [provider("provider-a"), provider("provider-b", { enabled: false })],
    runtimes: [
      connection("enabled", "gateway-a"),
      connection("disabled", "gateway-a", { enabled: false, is_default: true }),
      connection("disabled-provider", "gateway-a", { provider_id: "provider-b" }),
      connection("missing-provider", "gateway-a", { provider_id: "missing" }),
    ],
  })
  assert.deepEqual(publicationConnections(data, "gateway-a").map((item) => item.id), ["enabled"])
})

test("Foundry requires a managed Project, while registered zero-model connections remain reusable", () => {
  const foundryConfig = { control_plane_managed: true,
    project_endpoint: "https://account.services.ai.azure.com/api/projects/project",
    credential_provisioned: false, auth_strategy: "named_value_api_key" }
  const data = registry({ runtimes: [
    connection("registered", "gateway-a", { brand_key: "microsoft_foundry", config: foundryConfig }),
    connection("unmanaged", "gateway-a", { brand_key: "microsoft_foundry",
      config: { ...foundryConfig, control_plane_managed: false } }),
    connection("no-project", "gateway-a", { brand_key: "microsoft_foundry",
      config: { control_plane_managed: true, api_format: "openai_chat" } }),
    connection("ineligible", "gateway-a", { config: {} }),
  ] })
  assert.deepEqual(publicationConnections(data, "gateway-a").map((item) => item.id), ["registered"])
})

test("Databricks requires a managed Workspace and a compatible backend", () => {
  const data = registry({ databricks_connections_supported: true, runtimes: [
    connection("bedrock", "gateway-a", { brand_key: "amazon_bedrock", config: {} }),
    connection("databricks", "gateway-a", { brand_key: "azure_databricks", config: {
      control_plane_managed: true, api_format: "anthropic_messages",
      workspace_url: "https://adb-123.7.azuredatabricks.net",
    } }),
    connection("legacy-databricks", "gateway-a", { brand_key: "azure_databricks", config: {} }),
    connection("messages", "gateway-a", { config: { api_format: "anthropic_messages" } }),
  ] })
  assert.deepEqual(publicationConnections(data, "gateway-a").map((item) => item.id),
    ["bedrock", "databricks", "messages"])
  delete data.databricks_connections_supported
  assert.deepEqual(publicationConnections(data, "gateway-a").map((item) => item.id),
    ["bedrock", "messages"])
})

test("Databricks OAuth remains unavailable until the backend advertises support", () => {
  const runtime = connection("oauth", "gateway-a", { brand_key: "azure_databricks", config: {
    control_plane_managed: true, api_format: "anthropic_messages",
    workspace_url: "https://adb-123.7.azuredatabricks.net",
    auth_strategy: "oauth_client_credentials",
  } })
  const data = registry({ databricks_connections_supported: true, runtimes: [runtime] })
  assert.deepEqual(publicationConnections(data, "gateway-a"), [])
  data.databricks_oauth_supported = true
  assert.deepEqual(publicationConnections(data, "gateway-a"), [runtime])
  assert.equal(publicationConnectionAuth(runtime), "oauth_m2m")
  assert.equal(publicationConnectionEndpoint(runtime), "https://adb-123.7.azuredatabricks.net")
})

test("Databricks URL validation rejects paths, credentials and unrelated hosts", () => {
  assert.equal(isDatabricksWorkspaceUrl("https://adb-123.7.azuredatabricks.net/"), true)
  for (const url of [
    "http://adb-123.7.azuredatabricks.net", "https://adb-123.7.azuredatabricks.net.evil.example",
    "https://user:invalid@adb-123.7.azuredatabricks.net", "https://adb-123.7.azuredatabricks.net:8000",
    "https://adb-123.7.azuredatabricks.net/path", "https://adb-123.7.azuredatabricks.net?token=invalid",
  ]) assert.equal(isDatabricksWorkspaceUrl(url), false)
})

test("connection authentication takes precedence over a shared provider default", () => {
  const item = connection("key-connection", "gateway-a", {
    config: { auth_strategy: "named_value_api_key" },
  })
  assert.equal(publicationConnectionAuth(item, provider("provider-a", { auth_type: "azure_ad" })),
    "api_key")
  assert.equal(publicationConnectionAuth(connection("identity", "gateway-a", {
    config: { auth_strategy: "managed_identity" },
  }), provider("provider-a", { auth_type: "api_key" })), "managed_identity")
  assert.equal(publicationConnectionAuth(connection("legacy", "gateway-a")), "connection")
})

test("endpoint summary exposes only HTTPS origin and path", () => {
  const item = connection("sample", "gateway-a", { config: {
    base_url: "https://unit-user:unit-password@api.example.test/v1/?unit-key=not-a-secret#fragment",
  } })
  assert.equal(publicationConnectionEndpoint(item), "https://api.example.test/v1")
  assert.equal(publicationConnectionEndpoint(connection("sample", "gateway-a", { config: {
    project_endpoint: "not a URL", base_url: "http://invalid.test", backend_url: "https://valid.test/",
  } })), "https://valid.test")
  assert.equal(publicationConnectionEndpoint(connection("sample", "gateway-a")), null)
})

test("apimGatewayProfiles excludes unconfigured placeholder gateway profiles", () => {
  const placeholder = gateway("10000000-0000-4000-8000-000000000001", { name: "Azure API Management", base_url: null })
  const registered = gateway("gateway-a", { name: "Registered APIM" })
  const topology = { items: [{ target: { gateway_profile_id: "gateway-a" } }] }
  assert.deepEqual(
    gatewayTopology.apimGatewayProfiles([placeholder, registered], topology, true).map(item => item.id),
    ["gateway-a"],
  )
  assert.deepEqual(
    gatewayTopology.apimGatewayProfiles([placeholder, registered], null, false).map(item => item.id),
    ["gateway-a"],
  )
})
