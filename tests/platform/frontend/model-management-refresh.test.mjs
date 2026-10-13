import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { QueryClient, QueryObserver } = frontendRequire("@tanstack/react-query")
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const entry = fileURLToPath(new URL("../../../frontend/src/pages/model-management-page.tsx", import.meta.url))
const bundle = await rolldown({ input: entry, external: id => id !== entry,
  transform: { jsx: { runtime: "automatic" } }, treeshake: false })
let compiled
try { compiled = (await bundle.generate({ format: "cjs" })).output[0].code }
finally { await bundle.close() }

const registryKey = ["reference", "model-registry"]
const publicationsKey = ["control-plane", "gateway-publications"]
const publicationKey = ["control-plane", "gateway-publication", "publication"]
const healthKey = ["finops", "requests", { runtime: ["Foundry connection"] }]

function nodes(value) {
  if (Array.isArray(value)) return value.flatMap(nodes)
  if (!value || typeof value !== "object") return []
  return [value, ...nodes(value.props?.children)]
}

function harness(tab = "connections", invokeModel = async () => ({})) {
  const runtime = { id: "runtime", name: "Foundry connection", enabled: true,
    provider_id: "provider", gateway_profile_id: "gateway", brand_key: "microsoft_foundry",
    config: { max_tokens_field: "max_completion_tokens" }, health_status: "unknown" }
  const registry = {
    models: [{ id: "model", runtime_id: "runtime", provider_id: "provider", model_key: "gpt-5-mini", enabled: true,
      capabilities: ["chat"], display_name: "GPT-5 mini", allowed_roles: ["owner"] }],
    runtimes: [runtime],
    providers: [{ id: "provider", name: "Foundry", provider_kind: "microsoft_foundry", enabled: true }],
    gateways: [{ id: "gateway", name: "APIM", implementation: "apim", enabled: true }],
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const cleanup = []
  function observe(key, initial, queryFn) {
    client.setQueryData(key, initial)
    const observer = new QueryObserver(client, { queryKey: key, queryFn, staleTime: Infinity, retry: false })
    cleanup.push(observer.subscribe(() => {}))
  }
  const stores = { parent: [], workspace: [] }
  let current = "parent", cursor = 0
  const hooks = {
    useState(initial) {
      const index = cursor++, slots = stores[current]
      if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
      return [slots[index], next => { slots[index] = typeof next === "function" ? next(slots[index]) : next }]
    },
    useMemo(fn) { return fn() }, useEffect() {},
  }
  // A stable ref slot is needed even when the React import calls useRef without a receiver.
  hooks.useRef = initial => hooks.useState(() => ({ current: initial }))[0]
  const queries = {
    registry: () => ({ queryKey: registryKey }),
    gatewayPublications: () => ({ queryKey: publicationsKey }),
    gatewayPublication: id => ({ queryKey: ["control-plane", "gateway-publication", id] }),
    requests: () => ({ queryKey: healthKey }),
  }
  class ApiError extends Error {}
  const primitives = new Proxy({}, { get: (_target, name) => name })
  const imports = {
    react: hooks, "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@tanstack/react-query": {
      useQueryClient: () => client,
      useQuery: options => ({ data: client.getQueryData(options.queryKey),
        error: client.getQueryState(options.queryKey)?.error, isLoading: false }),
      useQueries: ({ queries: items }) => items.map(options => ({ data: client.getQueryData(options.queryKey), isLoading: false })),
      useMutation: options => ({ isPending: false, reset() {}, mutate: async value => {
        const result = await options.mutationFn(value)
        options.onSuccess?.(result)
      } }),
    },
    "../data-sources/apim/queries": { finopsKeys: {
      registry: registryKey, gatewayPublications: publicationsKey,
      gatewayPublication: id => ["control-plane", "gateway-publication", id],
    }, finopsQueries: queries },
    "../data-sources/apim/api": { ApiError, dataSource: { invokeModel }, usageWindow: () => ({}) },
    "../components/brand-logos": { ...primitives,
      providerBrandFromIdentity: () => "generic", providerBrandFromMetadata: () => "generic",
      gatewayBrandFromIdentity: () => "apim" },
    "../locales/index": { getIntlLocale: () => "en-US" },
  }
  const exports = {}
  const window = { location: { href: `https://unit.test/?page=models&tab=${tab}` } }
  runInNewContext(compiled, { exports, require: name => imports[name] ?? primitives,
    URL, window, crypto: { randomUUID: () => "test-probe-request" } })
  observe(registryKey, registry, async () => registry)
  observe(publicationsKey, { items: [] }, async () => ({ items: [] }))
  observe(healthKey, [], async () => [])
  function workspace() {
    current = "parent"; cursor = 0
    return nodes(exports.ModelManagementPage({ onToggleSidebar() {} }))
      .find(node => node.props?.registry && node.props?.onTabChange)
  }
  function render() {
    const element = workspace()
    current = "workspace"; cursor = 0
    return element.type(element.props)
  }
  return { client, observe, registry, runtime, workspace, render,
    find: predicate => nodes(render()).find(predicate),
    close() { cleanup.forEach(unsubscribe => unsubscribe()); client.clear() } }
}

for (const tab of ["models", "connections", "gateways"]) {
  test(`${tab} refresh reads updated registry, publication and health caches without inference`, async () => {
    const view = harness(tab, async () => { throw new Error("Refresh must not invoke a model") })
    try {
      view.observe(registryKey, view.registry, async () => ({ ...view.registry, revision: 2 }))
      view.observe(publicationsKey, { items: [] }, async () => ({ items: [{ id: "new-publication" }] }))
      view.observe(publicationKey, { status: "verifying" }, async () => ({ status: "active" }))
      view.observe(healthKey, [], async () => [{ request_source: "runtime-health-check",
        timestamp: "2026-10-10T00:00:00Z", status_code: 200, latency_ms: 2500 }])
      view.client.setQueryData(["unrelated"], "unchanged")
      const button = view.find(node => node.type === "button" && node.props["aria-label"] === "刷新模型管理")
      assert.ok(button, "Each tab needs an in-page refresh button")
      const actions = view.find(node => node.props?.className === "smh-header-actions")
      assert.equal(nodes(actions).filter(node => node.type === "button").length,
        tab === "gateways" ? 1 : 2, "Gateway creation stays hidden while refresh is available")
      await button.props.onClick()
      assert.equal(view.client.getQueryData(registryKey).revision, 2)
      assert.equal(view.client.getQueryData(publicationsKey).items[0].id, "new-publication")
      assert.equal(view.client.getQueryData(publicationKey).status, "active")
      assert.equal(view.client.getQueryData(healthKey)[0].status_code, 200)
      assert.equal(view.workspace().props.tab, tab)
      assert.equal(view.client.getQueryData(["unrelated"]), "unchanged")
    } finally { view.close() }
  })
}

test("refresh stays pending, deduplicates clicks and retains search and provider filter", async () => {
  const view = harness()
  let release
  const pending = new Promise(resolve => { release = resolve })
  try {
    view.observe(registryKey, view.registry, () => pending)
    view.find(node => node.type === "ExpandableSearch").props.onChange("Foundry")
    view.find(node => node.type === "Select" && node.props.value === "all").props.onValueChange("provider")
    const button = view.find(node => node.props?.["aria-label"] === "刷新模型管理")
    assert.ok(button)
    const first = button.props.onClick()
    await Promise.resolve()
    assert.equal(view.find(node => node.props?.["aria-label"] === "刷新模型管理").props.disabled, true)
    await button.props.onClick()
    release({ ...view.registry, revision: 2 })
    await first
    assert.equal(view.find(node => node.type === "ExpandableSearch").props.value, "Foundry")
    assert.ok(view.find(node => node.type === "Select" && node.props.value === "provider"))
    assert.equal(view.find(node => node.props?.["aria-label"] === "刷新模型管理").props.disabled, false)
  } finally { release?.(view.registry); view.close() }
})

test("a refresh failure preserves cached data and reports failure instead of success", async () => {
  const view = harness()
  try {
    view.observe(registryKey, view.registry, async () => { throw new Error("API offline") })
    const button = view.find(node => node.props?.["aria-label"] === "刷新模型管理")
    assert.ok(button)
    await button.props.onClick()
    const notice = view.workspace().props.notice
    assert.equal(notice.ok, false)
    assert.match(notice.text, /刷新失败/)
    assert.match(notice.detail, /API offline/)
    assert.equal(view.client.getQueryData(registryKey), view.registry)
    assert.equal(view.find(node => node.props?.["aria-label"] === "刷新模型管理").props.disabled, false)
  } finally { view.close() }
})

test("reasoning-model health check leaves enough token budget to return visible content", async () => {
  const view = harness("connections", async payload => {
    assert.equal(payload.max_output_tokens, 4096)
    assert.equal(payload.stream, false)
    assert.equal(payload.metadata.user_id, "system-runtime-health-check")
    return { gateway: "apim", model: "gpt-5-mini", latency_ms: 2500, content: "OK" }
  })
  try {
    await view.workspace().props.onCheckRuntime(view.runtime)
    await Promise.resolve()
    assert.equal(view.workspace().props.notice.ok, true)
  } finally { view.close() }
})

test("ordinary token-field health checks retain the small probe budget", async () => {
  const view = harness("connections", async payload => {
    assert.equal(payload.max_output_tokens, 16)
    return { gateway: "apim", model: "ordinary-model", latency_ms: 10, content: "OK" }
  })
  try {
    view.runtime.config = { max_tokens_field: "max_tokens" }
    await view.workspace().props.onCheckRuntime(view.runtime)
    await Promise.resolve()
    assert.equal(view.workspace().props.notice.ok, true)
  } finally { view.close() }
})

test("refresh waits for remaining reads after a failure and leaves inactive data untouched", async () => {
  const view = harness()
  let release
  const pending = new Promise(resolve => { release = resolve })
  try {
    view.client.setQueryDefaults(["finops", "requests", "inactive"], {
      queryFn: () => { throw new Error("Inactive query must not be fetched") },
    })
    view.client.setQueryData(["finops", "requests", "inactive"], "cached")
    view.observe(registryKey, view.registry, async () => { throw new Error("API offline") })
    view.observe(healthKey, [], () => pending)
    const button = view.find(node => node.props?.["aria-label"] === "刷新模型管理")
    const refresh = button.props.onClick()
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(view.find(node => node.props?.["aria-label"] === "刷新模型管理").props.disabled, true)
    release([])
    await refresh
    assert.equal(view.workspace().props.notice.ok, false)
    assert.equal(view.client.getQueryData(["finops", "requests", "inactive"]), "cached")
  } finally { release?.([]); view.close() }
})
