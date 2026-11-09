import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import * as pricing from "../../../frontend/src/lib/pricing.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const paths = {
  api: "data-sources/apim/api.ts",
  queries: "data-sources/apim/queries.ts",
  source: "data-sources/apim/source.tsx",
  shared: "data-sources/apim/pages/dashboard-shared.tsx",
  dashboard: "data-sources/apim/pages/dashboard-page.tsx",
  app: "app.tsx",
}
const compiled = {}
for (const [name, path] of Object.entries(paths)) {
  const entry = fileURLToPath(new URL(`../../../frontend/src/${path}`, import.meta.url))
  const bundle = await rolldown({ input: entry, external: identity => identity !== entry,
    transform: { jsx: { runtime: "automatic" } }, treeshake: false })
  try { compiled[name] = (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
}

const now = Date.parse("2026-11-05T12:00:00Z")
const dayMs = 86_400_000
class FixedDate extends Date {
  constructor(...values) { super(...(values.length ? values : [now])) }
  static now() { return now }
}
const labels = {
  period: "\u65f6\u95f4\u8303\u56f4",
  organization: "\u7ec4\u7ec7",
  department: "\u90e8\u95e8",
  agent: "\u667a\u80fd\u4f53",
  user: "\u4eba\u5458",
  search: "\u641c\u7d22\u8bf7\u6c42",
  empty: "\u6682\u65e0\u8bf7\u6c42\u8bb0\u5f55",
}

function request(identity, overrides = {}) {
  return {
    request_id: `caller-${identity}`, correlation_id: `correlation-${identity}`, trace_id: identity,
    upstream_request_id: `upstream-${identity}`, model_id: "model", model_name: identity,
    timestamp: new Date(now - 60_000).toISOString(), status_code: 200, latency_ms: 120,
    prompt_tokens: 10, cached_tokens: 0, completion_tokens: 5, total_tokens: 15,
    estimated_cost: 0, cost_available: false, usage_available: true, estimated: false,
    router_attempts: [], request_source: "unit-client", ingest_source: "gateway",
    provider: "unit-provider", runtime: "unit-runtime", user_name: "unit-person", turn_index: 1,
    ...overrides,
  }
}

function nodes(value) {
  if (Array.isArray(value)) return value.flatMap(nodes)
  if (!value || typeof value !== "object") return []
  return [value, ...nodes(Object.hasOwn(value, "rendered") ? value.rendered : value.props?.children)]
}

function text(value) {
  if (Array.isArray(value)) return value.map(text).join("")
  if (value && typeof value === "object") return text(Object.hasOwn(value, "rendered") ? value.rendered : value.props?.children)
  return value == null || typeof value === "boolean" ? "" : String(value)
}

function harness(initial = {}) {
  const state = {
    rows: [], details: new Map(), calls: [], fetches: [], refetches: [], nextCursor: null,
    listError: null, overviewError: null, trendError: null, detailError: null,
    detailLoading: false, listLoading: false, fetchingNextPage: false,
    ...initial,
  }
  let location = new URL(`https://unit.test/?source=apim&page=finops-requests${initial.requestId ? `&request=${encodeURIComponent(initial.requestId)}` : ""}`)
  const listeners = new Map()
  const storage = new Map()
  const window = {
    get location() { return location },
    history: { replaceState(_value, _title, url) { location = new URL(url, location) } },
    addEventListener(name, callback) { listeners.set(name, callback) },
    removeEventListener(name) { listeners.delete(name) },
    dispatchEvent(event) { listeners.get(event.type)?.(event) },
  }
  const document = { cookie: "", addEventListener() {}, removeEventListener() {} }
  const localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) }
  const primitives = new Proxy({}, { get: (_target, name) => name })
  const locale = { getIntlLocale: () => "en-US", useLocale: () => ({ locale: "en" }) }
  const load = (name, imports = {}) => {
    const exports = {}
    runInNewContext(compiled[name], { exports, Error, Date: FixedDate, URL, URLSearchParams, Event,
      window, document, localStorage, require: identity => identity === "react/jsx-runtime"
        ? frontendRequire(identity) : imports[identity] ?? primitives })
    return exports
  }
  const api = load("api", { "../../locales/index": locale })
  const call = (name, args, value) => { state.calls.push({ name, args }); return value }
  const entities = {
    organizations: [{ id: "org", name: "Organization" }],
    departments: [{ id: "dept", name: "Department" }],
    agents: [{ id: "agent", name: "Agent" }],
    users: [{ id: "person", name: "Person", parent_id: "dept" }],
  }
  const overview = { totals: {
    total_requests: 20, success_requests: 17, client_error_requests: 2, server_error_requests: 1,
    p50_latency_ms: 120, p95_latency_ms: 7200, p99_latency_ms: 9200,
    latency_under_1s: 10, latency_1_to_2s: 4, latency_2_to_5s: 2, latency_over_5s: 4,
    ...state.overviewTotals,
  } }
  const page = () => ({ items: state.rows, page: { next_cursor: state.nextCursor },
    counts: state.requestCounts ?? overview.totals })
  const transport = {
    entities: () => entities,
    registry: () => ({ models: [] }),
    anomalies: (filters, limit) => {
      const days = filters.range ?? Math.round((Date.parse(filters.to) - Date.parse(filters.from)) / dayMs)
      return call("anomalies", [filters, limit], Array.from({ length: ({ month: 1, 7: 2, 30: 3, 90: 4 })[days] + (filters.organization_id ? 1 : 0) }, (_, index) => ({ id: String(index) })))
    },
    requestPage: (filters, cursor) => {
      call("requestPage", [filters, cursor])
      if (state.listError) throw state.listError
      return page()
    },
    executiveOverview: filters => {
      call("executiveOverview", [filters])
      if (state.overviewError) throw state.overviewError
      return overview
    },
    trendPoints: (filters, group, interval) => {
      call("trendPoints", [filters, group, interval])
      if (state.trendError) throw state.trendError
      return { points: [{ bucket_start: "2026-11-05T00:00:00Z", totals: { calls: 20 } }] }
    },
    requestDetail: identity => {
      call("requestDetail", [identity])
      if (state.detailError) throw state.detailError
      return state.details.get(identity)
        ?? state.rows.find(item => [item.trace_id, item.correlation_id, item.request_id].includes(identity))
        ?? request(identity)
    },
  }
  const apiImports = { ...api, dataSource: transport }
  const frames = new Map()
  let frame
  let effects = []
  let dirty = false
  let observed = []
  const sameDependencies = (left, right) => left && right && left.length === right.length && left.every((value, index) => Object.is(value, right[index]))
  const hooks = {
    useState(initialValue) {
      const owner = frame
      const index = owner.cursor++
      if (!(index in owner.slots)) owner.slots[index] = typeof initialValue === "function" ? initialValue() : initialValue
      return [owner.slots[index], next => {
        const value = typeof next === "function" ? next(owner.slots[index]) : next
        if (!Object.is(value, owner.slots[index])) { owner.slots[index] = value; dirty = true }
      }]
    },
    useRef(initialValue) {
      const index = frame.cursor++
      return frame.slots[index] ??= { current: initialValue }
    },
    useMemo(compute, dependencies) {
      const index = frame.cursor++
      if (!sameDependencies(frame.slots[index]?.dependencies, dependencies)) frame.slots[index] = { dependencies, value: compute() }
      return frame.slots[index].value
    },
    useEffect(effect, dependencies) {
      const owner = frame
      const index = owner.cursor++
      const previous = owner.slots[index]
      if (!sameDependencies(previous?.dependencies, dependencies)) {
        owner.slots[index] = { dependencies }
        effects.push(() => { previous?.cleanup?.(); owner.slots[index].cleanup = effect() })
      }
    },
  }
  const queryClient = { refetchQueries: options => { state.refetches.push(options); return Promise.resolve() } }
  const queryHooks = {
    queryOptions: options => options, infiniteQueryOptions: options => options,
    useQueryClient: () => queryClient, useMutation: () => ({ isPending: false }),
    useQuery(options) {
      observed.push(options)
      if (options.enabled === false) return { isLoading: false }
      if (options.queryKey[1] === "request-detail" && state.detailLoading) return { isLoading: true }
      try { return { data: options.queryFn(), isLoading: false } }
      catch (error) { return { error, isLoading: false } }
    },
    useInfiniteQuery(options) {
      observed.push(options)
      try {
        const currentPage = options.queryFn({ pageParam: null })
        const cursor = options.getNextPageParam(currentPage)
        return { data: { pages: [currentPage] }, isLoading: state.listLoading,
          hasNextPage: Boolean(cursor), isFetchingNextPage: state.fetchingNextPage,
          fetchNextPage: () => {
            state.fetches.push(cursor)
            options.queryFn({ pageParam: cursor })
            return Promise.resolve()
          } }
      } catch (error) { return { error, isLoading: false } }
    },
  }
  const queries = load("queries", {
    "@tanstack/react-query": queryHooks, "./api": apiImports,
    "../../api/cache-policies": { cachePolicies: { reference: {}, aggregate: {}, requestList: {}, requestDetail: {} } },
  })
  const auth = { useAuth: () => ({ user: { email: "member@unit.test", role: "member" } }) }
  const shared = load("shared", { "../../../locales/index": locale })
  const dashboard = load("dashboard", {
    react: hooks, "@tanstack/react-query": queryHooks, "../api": apiImports, "../queries": queries,
    "../../../locales/index": locale, "../../../lib/pricing": pricing,
    "../../../providers/auth-provider": auth, "./dashboard-shared": shared,
  })
  const source = load("source", { "./api": apiImports, "./queries": queries, "./pages/dashboard-page": dashboard })
  const app = load("app", {
    react: hooks, "@tanstack/react-query": queryHooks, "./providers/auth-provider": auth,
    "./data-sources/apim/api": apiImports, "./data-sources/apim/queries": queries,
    "./data-sources/apim/source": source, "./hooks/use-mobile": { useIsMobile: () => false },
    "./locales/index": locale, "./lib/navigation": { FINOPS_NAVIGATE_EVENT: "finops-navigate" },
    "./components/assistant/queries": { pinnedChartsQuery: () => ({ queryKey: ["pinned"], queryFn: () => [] }) },
    "./data-sources/github-copilot/source": { githubCopilotPages: [], normalizeGithubCopilotPage: () => "finops-overview", githubCopilotOwnsPage: () => false },
  })
  const expand = value => {
    if (Array.isArray(value)) return value.map(expand)
    if (!value || typeof value !== "object") return value
    if (typeof value.type === "function") {
      const owner = frames.get(value.type) ?? { slots: [], cursor: 0 }
      frames.set(value.type, owner)
      frame = owner
      owner.cursor = 0
      const rendered = value.type(value.props)
      return { ...value, rendered: expand(rendered) }
    }
    return { ...value, props: { ...value.props, children: expand(value.props?.children) } }
  }
  let tree
  const render = () => {
    let passes = 0
    do {
      dirty = false
      effects = []
      observed = []
      tree = expand(frontendRequire("react/jsx-runtime").jsx(app.App, {}))
      for (const effect of effects) effect()
      assert.ok(++passes < 10, "Component effects must settle")
    } while (dirty)
    return tree
  }
  const find = predicate => nodes(tree).find(predicate)
  const control = predicate => { const result = find(predicate); assert.ok(result, "Expected rendered control"); return result }
  const filter = (label, value) => { control(node => node.type === "FilterMenuField" && node.props.label === label).props.onChange(value); render() }
  const search = value => { control(node => node.type === "input" && node.props["aria-label"] === labels.search).props.onChange({ target: { value } }); render() }
  render()
  return {
    state, render, find, control, filter, search,
    period: value => filter(labels.period, String(value)),
    selected: () => observed.find(options => options.queryKey[1] === "request-detail")?.queryKey[2],
    badgeQuery: () => observed.find(options => options.queryKey[1] === "anomalies"),
    lastCall: name => state.calls.findLast(item => item.name === name),
    rows: () => nodes(tree).filter(node => node.type === dashboard.TraceRow),
    list: () => find(node => node.props?.className === "finops-panel trace-list"),
    detail: () => find(node => node.props?.className === "finops-panel trace-detail"),
    status(value) { control(node => node.type === "button" && node.key === value && typeof node.props.onClick === "function").props.onClick(); render() },
    clickRow(identity) { control(node => node.type === dashboard.TraceRow && node.key === identity).props.onClick(); render() },
  }
}

test("AUD-01: real period and scope controls bind the badge to the dashboard queries", async () => {
  const view = harness()
  for (const [days, count] of [["month", 1], [7, 2], [30, 3], [90, 4], ["month", 1]]) {
    view.period(days)
    const badge = view.badgeQuery()
    assert.equal(badge.queryKey.at(-1).days, days, "Badge query must use the selected period, not fixed 30 days")
    assert.equal(badge.enabled, true)
    assert.equal(badge.refetchInterval, 300_000)
    assert.equal(text(view.control(node => node.props?.["data-page"] === "finops-governance").props.children.at(-1)), String(count))
    const filters = view.lastCall("anomalies").args[0]
    assert.equal(view.lastCall("anomalies").args[1], 100)
    assert.equal(filters.from, view.lastCall("requestPage").args[0].from)
    assert.equal(filters.to, view.lastCall("executiveOverview").args[0].to)
    if (days !== "month") assert.equal(Date.parse(filters.to) - Date.parse(filters.from), days * dayMs)
  }
  for (const [label, value] of [[labels.organization, "org"], [labels.department, "dept"], [labels.agent, "agent"], [labels.user, "person"]]) view.filter(label, value)
  for (const name of ["anomalies", "requestPage", "executiveOverview", "trendPoints"]) {
    const filters = view.lastCall(name).args[0]
    assert.deepEqual([filters.organization_id, filters.department_id, filters.agent_id, filters.user_id], ["org", "dept", "agent", "person"])
  }
  assert.equal(text(view.control(node => node.props?.["data-page"] === "finops-governance").props.children.at(-1)), "2")
  view.control(node => node.props?.className === "finops-header-refresh").props.onClick()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(view.state.refetches.map(options => Array.from(options.queryKey)), [["finops"], ["reference", "enterprise-entities"], ["reference", "model-registry"]])
  const calls = view.state.calls.filter(item => item.name === "anomalies").length
  view.control(node => node.type === "DropdownMenuItem" && text(node) === "GitHub Copilot").props.onClick()
  view.render()
  assert.equal(view.badgeQuery().enabled, false)
  assert.equal(view.state.calls.filter(item => item.name === "anomalies").length, calls)
})

test("AUD-02: an empty window still renders the independently loaded deep-linked detail", () => {
  const detail = request("outside", { timestamp: "2026-01-01T00:00:00Z", request_id: "historical-caller", correlation_id: "historical-attempt" })
  const view = harness({ requestId: "outside", details: new Map([["outside", detail]]) })
  assert.equal(view.selected(), "outside")
  assert.ok(view.detail(), "An empty list must not hide an explicitly selected detail")
  assert.ok(text(view.detail()).includes("historical-caller"))
  assert.ok(text(view.detail()).includes("historical-attempt"))
  assert.equal(view.rows().length, 0, "Detail must not be injected into the request list")
  assert.equal(view.lastCall("requestPage").args[0].range, "month")
  assert.ok(view.find(node => node.props?.["aria-label"] === "\u8bf7\u6c42\u6458\u8981"))
})

test("Clearing a deep-link search restores the filtered-list selection", () => {
  const view = harness({ requestId: "outside", rows: [request("inside")] })
  view.search("")
  assert.equal(view.selected(), "inside")
  assert.deepEqual(view.rows().map(node => node.props.item.trace_id), ["inside"])
  assert.ok(text(view.detail()).includes("caller-inside"))
  assert.equal(view.lastCall("requestPage").args[0].range, "month")
})

test("An empty deep-link window retains controls and restores the ID after clearing status", () => {
  const view = harness({ requestId: "outside" })
  view.status("error")
  assert.equal(view.selected(), "")
  assert.ok(view.list())
  assert.equal(text(view.detail()).includes("caller-outside"), false)
  view.status("all")
  assert.equal(view.selected(), "outside")
  assert.ok(text(view.detail()).includes("caller-outside"))
  view.search("not-the-selected-request")
  assert.equal(view.selected(), "")
  view.search("")
  assert.equal(view.selected(), "outside")
})

test("TRC-02: status KPIs use full-window Trace counts, not billing status or loaded rows", () => {
  const view = harness({
    rows: [request("denied", { status_code: 403 }),
      request("platform-failed", { status_code: 200, platform_outcome: "failed" })],
    overviewTotals: { total_requests: 5, success_requests: 1,
      client_error_requests: 4, server_error_requests: 0 },
    requestCounts: { total_requests: 5, success_requests: 0,
      client_error_requests: 4, server_error_requests: 1 },
  })
  view.status("error")
  const failure = view.control(node => node.props?.label === "失败请求")
  assert.equal(failure.props.value, "5")
  assert.equal(failure.props.note, "4xx 4 · 5xx 1")
  assert.equal(view.control(node => node.props?.title === "请求记录").props.meta, "2 / 5 条")
})

test("User selection follows search, status and empty-list changes", () => {
  const view = harness({ rows: [request("first"), request("chosen"), request("failed", { status_code: 500 }),
    request("platform-failed", { status_code: 200, platform_outcome: "failed" }), request("unknown", { status_code: null })] })
  assert.equal(view.selected(), "first", "No selection should still select the initial first row")
  view.clickRow("chosen")
  view.search("  FAILED  ")
  assert.equal(view.selected(), "failed")
  assert.deepEqual(view.rows().map(node => node.key), ["failed", "platform-failed"])
  view.status("success")
  assert.equal(view.rows().length, 0)
  assert.equal(view.selected(), "")
  assert.equal(text(view.detail()).includes("caller-failed"), false)
  view.search("")
  assert.deepEqual(view.rows().map(node => node.key), ["first", "chosen"])
  assert.equal(view.selected(), "first")
  view.status("error")
  assert.deepEqual(view.rows().map(node => node.key), ["failed", "platform-failed"])
  assert.equal(view.selected(), "failed")
  view.period(7)
  assert.equal(Date.parse(view.lastCall("requestPage").args[0].to) - Date.parse(view.lastCall("requestPage").args[0].from), 7 * dayMs)
  view.state.rows = []
  view.render()
  assert.equal(view.selected(), "")
  assert.equal(view.detail(), undefined)
  assert.equal(view.rows().length, 0)
})

for (const populated of [false, true]) {
  test(`AUD-02: selected detail loading is visible with ${populated ? "unrelated" : "no"} list rows`, () => {
    const view = harness({ requestId: "pending", rows: populated ? [request("other")] : [], detailLoading: true })
    assert.ok(view.detail(), "Selection loading must remain reachable")
    assert.ok(nodes(view.detail()).some(node => node.props?.className === "finops-state"))
    view.search("")
    assert.equal(view.selected(), populated ? "other" : "pending")
    assert.equal(nodes(view.detail()).some(node => node.props?.className === "trace-detail-content"), false)
  })
  for (const status of [403, 404]) {
    test(`AUD-02: detail HTTP ${status} is retained with ${populated ? "unrelated" : "no"} list rows`, () => {
      const view = harness({ requestId: "denied", rows: populated ? [request("other")] : [], detailError: new Error(`API ${status}: original detail failure`) })
      assert.ok(view.detail(), "Access and not-found failures must not become an empty-list state")
      assert.ok(text(view.detail()).includes(`API ${status}: original detail failure`))
      view.search("")
      assert.equal(view.selected(), populated ? "other" : "denied")
      assert.ok(text(view.detail()).includes(`API ${status}: original detail failure`))
      assert.equal(nodes(view.detail()).some(node => node.props?.className === "trace-detail-content"), false)
    })
  }
}

test("No selection and no rows keeps the original empty state and does not request detail", () => {
  const view = harness()
  assert.equal(view.selected(), "")
  assert.ok(text(view.find(node => node.props?.className === "finops-state empty")).includes(labels.empty))
  assert.equal(view.detail(), undefined)
  assert.equal(view.state.calls.some(item => item.name === "requestDetail"), false)
  view.state.rows = [request("arrived")]
  view.render()
  assert.equal(view.selected(), "arrived")
  assert.ok(text(view.detail()).includes("caller-arrived"))
})

for (const source of ["listError", "overviewError", "trendError"]) {
  test(`A ${source} remains an error rather than selected success or an empty state`, () => {
    const view = harness({ requestId: "outside", [source]: new Error(`API 403: ${source}`) })
    assert.ok(text(view.find(node => node.props?.className === "finops-state error")).includes(`API 403: ${source}`))
    assert.equal(view.detail(), undefined)
    assert.equal(view.find(node => node.props?.className === "finops-state empty"), undefined)
    assert.equal(view.selected(), "outside")
  })
}

test("Attempt selection keeps trace/correlation/caller identity, ordering and searchable fields", () => {
  const view = harness({ rows: [
    request("second", { trace_id: null, correlation_id: "attempt-two", request_id: "same-caller", timestamp: "2026-11-05T11:59:00Z" }),
    request("first", { trace_id: null, correlation_id: "attempt-one", request_id: "same-caller", timestamp: "2026-11-05T11:58:00Z" }),
    request("platform-trace", { correlation_id: "gateway-correlation" }),
    request("legacy", { trace_id: null, correlation_id: null, request_id: "legacy-caller" }),
  ] })
  assert.equal(view.selected(), "attempt-two")
  assert.deepEqual(view.rows().slice(0, 2).map(node => [node.key, node.props.attempt.index, node.props.attempt.total]), [["attempt-two", 2, 2], ["attempt-one", 1, 2]])
  for (const identity of ["attempt-one", "attempt-two", "platform-trace", "legacy-caller"]) {
    view.clickRow(identity)
    assert.equal(view.selected(), identity)
    assert.equal(view.lastCall("requestDetail").args[0], identity)
  }
  view.search("same-caller")
  assert.deepEqual(view.rows().map(node => node.key), ["attempt-two", "attempt-one"])
  view.search("ATTEMPT-ONE")
  assert.deepEqual(view.rows().map(node => node.key), ["attempt-one"])
  view.search("platform-trace")
  assert.deepEqual(view.rows().map(node => node.key), ["platform-trace"])
  view.search("gateway-correlation")
  assert.deepEqual(view.rows().map(node => node.key), ["platform-trace"])
  view.search("upstream-platform")
  assert.deepEqual(view.rows().map(node => node.key), ["platform-trace"])
})

test("Paging retains the real cursor, unfiltered scrolling and the in-flight dispatch guard", async () => {
  const view = harness({ rows: [request("inside")], nextCursor: "next-page" })
  const scroll = () => view.list().props.onScroll({ currentTarget: { scrollHeight: 500, scrollTop: 320, clientHeight: 100 } })
  scroll()
  scroll()
  assert.deepEqual(view.state.fetches, ["next-page"])
  assert.equal(view.lastCall("requestPage").args[1], "next-page")
  assert.equal(view.lastCall("requestPage").args[0].range, "month")
  await new Promise(resolve => setImmediate(resolve))
  view.search("inside")
  scroll()
  assert.equal(view.state.fetches.length, 1)
  view.control(node => node.props?.className === "trace-load-more").props.onClick()
  assert.deepEqual(view.state.fetches, ["next-page", "next-page"])
  await new Promise(resolve => setImmediate(resolve))
  view.state.fetchingNextPage = true
  view.render()
  assert.equal(view.control(node => node.props?.className === "trace-load-more").props.disabled, true)
  view.control(node => node.props?.className === "trace-load-more").props.onClick()
  assert.equal(view.state.fetches.length, 2)
})