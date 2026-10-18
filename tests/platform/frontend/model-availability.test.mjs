import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import { availabilityPresentation, availabilityRefetchInterval } from "../../../frontend/src/lib/model-availability.ts"
import { resolveInvocationModel } from "../../../frontend/src/lib/gateway-topology.ts"
import * as runtimeHealth from "../../../frontend/src/lib/runtime-health.ts"
import * as assistantSettings from "../../../frontend/src/components/assistant/settings-update.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const entry = fileURLToPath(new URL("../../../frontend/src/components/model-management/model-availability.tsx", import.meta.url))
const bundle = await rolldown({ input: entry, external: path => path !== entry, transform: { jsx: { runtime: "automatic" } }, treeshake: false })
let component
try { component = (await bundle.generate({ format: "cjs" })).output[0].code }
finally { await bundle.close() }

const settingsEntry = fileURLToPath(new URL("../../../frontend/src/pages/settings-page.tsx", import.meta.url))
const settingsBundle = await rolldown({ input: settingsEntry, external: path => path !== settingsEntry,
  transform: { jsx: { runtime: "automatic" } }, treeshake: false,
  plugins: [{ name: "settings-test-export", transform(source, id) { return id === settingsEntry ? `${source}\nexport { AssistantSection, useAssistantEditor }` : null } }],
})
let settingsComponent
try { settingsComponent = (await settingsBundle.generate({ format: "cjs" })).output[0].code }
finally { await settingsBundle.close() }

class ApiError extends Error {
  constructor(status) { super(`HTTP ${status}`); this.status = status }
}

function componentHarness(initial = readback(), send = async (_modelId, value) => ({
  operation: { id: "unit-operation", model_id: "unit-model", desired_enabled: value.enabled,
    desired_revision: value.expected_revision + 1, status: "pending", targets: [] },
})) {
  const keys = { modelAvailability: ["model-platform", "model-availability"], registry: ["reference", "model-registry"] }
  const refs = []
  const calls = []
  const invalidated = []
  let cursor = 0
  let pending = 0
  let error = null
  let completion = Promise.resolve()
  let sequence = 0
  let activeAction
  let cached = { items: initial ? [initial] : [] }
  let retrySend = async operationId => ({ operation: {
    id: operationId, desired_enabled: initial.desired_enabled, desired_revision: initial.desired_revision,
    status: "pending", targets: [],
  } })
  const client = {
    isMutating: () => pending,
    cancelQueries: async () => {},
    setQueryData(_key, update) { cached = update(cached) },
    invalidateQueries({ queryKey }) { invalidated.push(queryKey); return Promise.resolve() },
  }
  const imports = {
    react: { useRef(value) { const index = cursor++; return refs[index] ??= { current: value } } },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@tanstack/react-query": {
      useQueryClient: () => client,
      useIsMutating: () => pending,
      useMutationState: () => pending ? [activeAction] : [],
      useMutation(options) {
        assert.equal(options.retry, false)
        return { error, mutate(action) {
          pending += 1
          activeAction = action
          completion = Promise.resolve().then(() => options.mutationFn(action)).then(result => {
            error = null
            return options.onSuccess?.(result)
          }, failure => {
            error = failure
            options.onError?.(failure)
          }).finally(() => { pending -= 1; options.onSettled?.() })
        } }
      },
    },
    "../../data-sources/apim/api": { ApiError, dataSource: {
      changeModelAvailability(modelId, value) { calls.push(["change", modelId, { ...value }]); return send(modelId, value) },
      retryModelAvailability(operationId, revision) { calls.push(["retry", operationId, revision]); return retrySend(operationId, revision) },
    } },
    "../../data-sources/apim/queries": { finopsKeys: keys },
    "../../lib/model-availability": { availabilityPresentation },
    "../../lib/runtime-health": runtimeHealth,
    "../../locales": { getIntlLocale: () => "en-US" },
    "../../providers/timezone-provider": { useTimezone: () => ({ timezone: "Asia/Shanghai" }) },
    "../../providers/auth-provider": { useAuth: () => ({ user: { role: "owner" } }) },
    "../assistant/queries": { assistantSettingsKey: ["assistant", "settings"] },
  }
  const exports = {}
  runInNewContext(component, { exports, crypto: { randomUUID: () => `00000000-0000-4000-8000-${String(++sequence).padStart(12, "0")}` },
    require: name => imports[name] ?? new Proxy({}, { get: (_target, property) => property }),
  })
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
    : value && typeof value === "object" ? [value, ...nodes(typeof value.type === "function" ? value.type(value.props) : value.props?.children)] : []
  return {
    exports, calls, invalidated, nodes,
    state: () => cached.items[0],
    sequence: () => sequence,
    settle: () => completion,
    action: () => { cursor = 0; return exports.useModelAvailabilityCommand("unit-model", cached.items[0]) },
    render: (name, props) => { cursor = 0; return exports[name](props) },
    setReadback(value) { cached = { items: value ? [value] : [] } },
    setRetry(value) { retrySend = value },
  }
}

function ready(enabled = true) {
  return readback({ status: "succeeded", desired_enabled: enabled, effective_enabled: enabled,
    effective_revision: 2, verified_at: "2026-01-01T00:00:00Z", command_ready: true })
}

function readback(patch = {}) {
  return {
    model_id: "unit-model", desired_enabled: false, desired_revision: 2,
    effective_enabled: true, effective_revision: 1, verified_at: null,
    status: "pending", command_ready: false, reason: null, operation: null,
    ...patch,
  }
}

for (const status of ["pending", "projected", "partial"]) {
  test(`${status} keeps desired state separate from verified state and prevents another command`, () => {
    const state = availabilityPresentation(readback({ status }))
    assert.equal(state.checked, false)
    assert.equal(state.verified, false)
    assert.equal(state.inFlight, true)
    assert.equal(state.canChange, false)
    assert.equal(availabilityRefetchInterval([readback({ status })]), 2000)
  })
}

for (const patch of [{ effective_revision: 1 }, { effective_enabled: true }, { verified_at: null }]) {
  test(`a success label without exact proof stays unknown: ${Object.keys(patch)[0]}`, () => {
    const state = availabilityPresentation(readback({
      status: "succeeded", effective_revision: 2, effective_enabled: false,
      verified_at: "2026-01-01T00:00:00Z", ...patch,
    }))
    assert.equal(state.state, "unknown")
    assert.equal(state.verified, false)
  })
}

test("only a current verified revision can present effective success", () => {
  const state = availabilityPresentation(readback({
    status: "succeeded", effective_revision: 2, effective_enabled: false,
    verified_at: "2026-01-01T00:00:00Z", command_ready: true,
  }))
  assert.equal(state.verified, true)
  assert.equal(state.canChange, true)
  assert.equal(state.checked, false)
})

test("missing backend and polling errors retain unknown state without hot retries", () => {
  const state = availabilityPresentation(undefined, false, true)
  assert.equal(state.checked, true)
  assert.equal(state.state, "unknown")
  assert.equal(state.canChange, false)
  assert.equal(availabilityRefetchInterval(undefined), 15000)
  assert.equal(availabilityRefetchInterval([readback()], new Error("offline")), 15000)
})

test("retry requires the existing failed operation and fresh command readiness", () => {
  assert.equal(availabilityPresentation(readback({ status: "failed" })).canRetry, false)
  const failed = readback({ status: "failed", command_ready: true, operation: { id: "unit-operation" } })
  assert.equal(availabilityPresentation(failed).canRetry, true)
  assert.equal(availabilityPresentation(failed, true).canRetry, false)
  assert.equal(availabilityPresentation({ ...failed, command_ready: false }).canRetry, false)
})

test("the actual command hook deduplicates clicks and never promotes accepted state to effective success", async () => {
  const harness = componentHarness(ready())
  const action = harness.action()
  action.change()
  action.change()
  await harness.settle()
  assert.equal(harness.calls.length, 1)
  const [kind, model, command] = harness.calls[0]
  assert.equal(kind, "change")
  assert.equal(model, "unit-model")
  assert.equal(command.enabled, false)
  assert.equal(command.expected_revision, 2)
  assert.match(command.idempotency_key, /^[a-f0-9-]{36}$/)
  assert.equal(harness.state().status, "pending")
  assert.equal(harness.state().desired_enabled, false)
  assert.equal(harness.state().effective_enabled, true)
  assert.equal(harness.state().effective_revision, 2)
  assert.equal(harness.action().canChange, false)
  assert.deepEqual(harness.invalidated, [["reference", "model-registry"], ["model-platform", "model-availability"]])
})

test("an uncertain command reuses its idempotency key while a version conflict clears it", async () => {
  let failure = new Error("offline")
  const harness = componentHarness(ready(), async () => { throw failure })
  harness.action().change()
  await harness.settle()
  assert.equal(harness.action().error, "请求结果未确认，请刷新状态。")
  harness.action().change()
  await harness.settle()
  assert.equal(harness.calls[0][2].idempotency_key, harness.calls[1][2].idempotency_key)
  failure = new ApiError(409)
  harness.action().change()
  await harness.settle()
  assert.equal(harness.action().error, "模型状态已变化，请刷新后重试。")
  harness.setReadback({ ...ready(false), desired_revision: 3, effective_revision: 3 })
  harness.action().change()
  await harness.settle()
  assert.notEqual(harness.calls[3][2].idempotency_key, harness.calls[0][2].idempotency_key)
  assert.equal(harness.calls[3][2].expected_revision, 3)
  assert.equal(harness.calls[3][2].enabled, true)
})

test("retry addresses the existing failed operation instead of creating another change", async () => {
  const harness = componentHarness(readback({ status: "failed", command_ready: true,
    operation: { id: "existing-operation", targets: [] } }))
  harness.action().retry()
  await harness.settle()
  assert.deepEqual(harness.calls, [["retry", "existing-operation", 2]])
  assert.equal(harness.sequence(), 0)
  assert.equal(harness.state().operation.id, "existing-operation")
})

test("missing, in-flight and mismatched readbacks cannot issue a command", async () => {
  for (const state of [undefined, readback(), readback({ status: "partial" }), { ...ready(), model_id: "other-model" }]) {
    const harness = componentHarness(state)
    harness.action().change()
    await harness.settle()
    assert.equal(harness.calls.length, 0)
  }
})

test("the actual Switch exposes desired direction, busy state and unavailable guard", () => {
  const harness = componentHarness()
  const pending = harness.render("ModelAvailabilitySwitch", { readback: readback(), registryEnabled: true, onChange() {} })
  assert.equal(pending.props.checked, false)
  assert.equal(pending.props.disabled, true)
  assert.equal(pending.props["aria-busy"], true)
  const missing = harness.render("ModelAvailabilitySwitch", { registryEnabled: true, onChange() {} })
  assert.equal(missing.props.checked, true)
  assert.equal(missing.props.disabled, true)
  const available = harness.render("ModelAvailabilitySwitch", { readback: ready(), registryEnabled: true, onChange() {} })
  assert.equal(available.props.disabled, false)
  assert.equal(available.props["aria-label"], "停用此模型")
})

test("the detail keeps target evidence and old verified state separate from the requested state", () => {
  const current = readback({ status: "partial", operation: { id: "existing-operation", targets: [
    { target_id: "managed", status: "verified", verified_at: "2026-01-01T00:00:00Z", reason: null },
    { target_id: "edge", status: "unknown", verified_at: null, reason: "self_hosted_node_coverage_unverified" },
  ] } })
  const harness = componentHarness(current)
  const content = harness.nodes(harness.render("ModelAvailabilityDialog", {
    model: { id: "unit-model", display_name: "Unit model", enabled: true, runtime_id: "connection" },
    registry: { gateways: [{ id: "managed", name: "Managed" }, { id: "edge", name: "Edge" }],
      runtimes: [{ id: "connection", health_status: "available" }] },
    readback: current, readError: false, onRefresh() {}, onClose() {},
  }))
  assert.deepEqual(content.filter(node => node.type === "dd").slice(0, 4).map(node => node.props.children), ["已停用", 2, "已启用", 1])
  assert.equal(content.filter(node => node.type === "li").length, 2)
  assert.ok(content.some(node => node.type === "span" && node.props.children === "部分生效"))
  assert.ok(!content.some(node => node.type === "span" && node.props.children === "正在停用"))
  assert.ok(content.some(node => node.type === "p" && node.props.children === "自托管节点覆盖尚未验证"))
  assert.equal(content.find(node => node.type === "Switch").props.disabled, true)
})

test("verified status uses the existing health indicator without promoting unknown state", () => {
  const harness = componentHarness()
  for (const enabled of [true, false]) {
    const result = harness.render("ModelAvailabilityStatus", { readback: ready(enabled) })
    assert.equal(result.props.className, `model-availability-status succeeded registry-health ${enabled ? "available" : "unavailable"}`)
    assert.ok(harness.nodes(result).some(node => node.type === "i" && node.props["aria-hidden"] === "true"))
  }
  for (const status of ["pending", "partial", "failed", "unknown"]) {
    const result = harness.render("ModelAvailabilityStatus", { readback: readback({ status }) })
    assert.equal(result.props.className.includes("registry-health"), false)
  }
})

test("availability details use the selected timezone and historical diagnostic health", () => {
  const current = ready()
  const harness = componentHarness(current)
  const runtime = { id: "connection", enabled: true, health_status: "available", diagnostic: {
    runtime_id: "connection", status: "available", outcome: "passed", checked_at: "2026-01-01T00:00:00Z",
    configuration_status: "changed", configuration_fingerprint: "unit", selection: null,
  } }
  const content = harness.nodes(harness.render("ModelAvailabilityDialog", {
    model: { id: "unit-model", display_name: "Unit model", enabled: true, runtime_id: "connection" },
    registry: { gateways: [], runtimes: [runtime] }, readback: current,
    readError: false, onRefresh() {}, onClose() {},
  }))
  const facts = content.filter(node => node.type === "dd")
  assert.equal(facts[4].props.children, new Date(current.verified_at).toLocaleString("en-US", { timeZone: "Asia/Shanghai" }))
  assert.equal(facts[5].props.children, "当前配置待验证")
  assert.ok(facts[5].props.title.includes("历史结果不代表当前配置"))
})

test("a disabled default blocks automatic invocation selection but not an explicit enabled model", () => {
  const models = [
    { id: "default", enabled: false, is_default: true, capabilities: ["chat"] },
    { id: "other", enabled: true, is_default: false, capabilities: ["chat"] },
  ]
  assert.equal(resolveInvocationModel(models, null, false), undefined)
  assert.equal(resolveInvocationModel(models, "default", false), undefined)
  assert.equal(resolveInvocationModel(models, "other", false), models[1])
})

test("assistant settings do not resave an unavailable pin when changing auto-title", async () => {
  const writes = []
  const value = { model_id: "disabled-model", model_available: false, effective_model_id: null,
    effective_model_name: null, auto_title: true, available_models: [{ id: "other", display_name: "Other", runtime_name: "Connection" }] }
  const imports = {
    react: { useState: () => [draft, value => { draft = value }], useEffect() {} },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@tanstack/react-query": {
      useQueryClient: () => ({ setQueryData() {} }),
      useQuery: () => ({ data: value, isPending: false }),
      useMutation: options => ({ isPending: false, reset() {}, mutate: payload => options.mutationFn(payload) }),
    },
    "../components/assistant/api": { assistantApi: { saveSettings: async payload => { writes.push(payload); return value } } },
    "../components/assistant/queries": { assistantSettingsKey: ["assistant", "settings"], assistantSettingsQuery: () => ({}) },
    "../components/assistant/settings-update": assistantSettings,
  }
  let draft = null
  const exports = {}
  runInNewContext(settingsComponent, { exports, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  const render = () => exports.AssistantSection({ editor: exports.useAssistantEditor(true), canEdit: true })
  const nodes = componentHarness().nodes(render())
  assert.equal(nodes.find(node => node.type === "Select").props.value, "automatic")
  const option = nodes.find(node => node.type === "SelectItem" && node.props.value === "disabled-model")
  assert.equal(option.props.disabled, true)
  assert.equal(option.props.children, "指定模型不可用")
  await nodes.find(node => node.type === "Switch").props.onCheckedChange(false)
  assert.equal(writes.length, 0)
  componentHarness().nodes(render()).find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.deepEqual(JSON.parse(JSON.stringify(writes)), [{ model_id: null, auto_title: false }])
})
