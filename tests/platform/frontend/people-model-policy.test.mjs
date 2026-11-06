import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const queryLibrary = frontendRequire("@tanstack/react-query")
const jsxRuntime = frontendRequire("react/jsx-runtime")

async function compile(path, expose = "") {
  const entry = fileURLToPath(new URL(path, import.meta.url))
  const bundle = await rolldown({
    input: entry,
    external: identity => identity !== entry,
    transform: { jsx: { runtime: "automatic" } },
    treeshake: false,
    plugins: [{ name: "people-test-entry", transform: (source, identity) => identity === entry
      ? { code: `${source}\n${expose}`, map: null } : null }],
  })
  try { return (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
}

const queryCode = await compile("../../../frontend/src/data-sources/apim/queries.ts")
const pageCode = await compile("../../../frontend/src/data-sources/apim/pages/budget-page.tsx", "export { PeopleBudgetWorkspace, BudgetEditor }")
const plain = value => JSON.parse(JSON.stringify(value))
const tick = () => new Promise(resolve => setImmediate(resolve))

function deferred() {
  let resolve
  let reject
  const promise = new Promise((accept, deny) => { resolve = accept; reject = deny })
  return { promise, resolve, reject }
}

function person(identity, name = identity, extra = {}) {
  return {
    scope_type: "user", scope_id: identity, scope_name: name, parent_scope_id: "department-a",
    token_limit: null, used_tokens: 0, remaining_tokens: null, usage_percent: null,
    forecast_tokens: 0, forecast_percent: null, warning_threshold_percent: 80,
    status: "unallocated", updated_at: null, updated_by: null,
    model_policy_configured: false, allowed_model_ids: [], ...extra,
  }
}

function peopleResponse(extra = {}) {
  return {
    period: "2026-11", department_id: "department-a", department_name: "Department A",
    department_token_limit: 10000, department_allocated_tokens: 0, department_available_tokens: 10000,
    total: 2, offset: 0, limit: 50, assigned_count: 0, unallocated_count: 2,
    risk_count: 0, model_configured_count: 0,
    items: [person("alice", "Alice"), person("bob", "Bob")], ...extra,
  }
}

function nodes(value) {
  if (Array.isArray(value)) return value.flatMap(nodes)
  if (!value || typeof value !== "object") return []
  return [value, ...nodes(value.props?.children)]
}

function text(value) {
  if (Array.isArray(value)) return value.map(text).join("")
  if (value && typeof value === "object") return text(value.props?.children)
  return value == null || typeof value === "boolean" ? "" : String(value)
}

class HttpFailure extends Error {
  constructor(status, detail) { super(detail); this.status = status; this.detail = detail }
}

function savedResult(extra = {}) {
  return { period: "2026-11", department_id: "department-a", updated_count: 1,
    token_limit_per_user: null, total_allocated: null, model_policy_updated_count: 1, ...extra }
}

function harness(initial = peopleResponse(), { seed = true } = {}) {
  const client = new queryLibrary.QueryClient({ defaultOptions: {
    queries: { retry: false, gcTime: Infinity }, mutations: { retry: false, gcTime: Infinity },
  } })
  const writes = []
  const reads = []
  const mutations = []
  const subscriptions = []
  const write = deferred()
  const dataSource = {
    peopleBudgets: (...args) => {
      const request = deferred()
      reads.push({ args, ...request })
      return request.promise
    },
    bulkSavePeopleBudgets: (period, request) => {
      writes.push({ period, request: plain(request) })
      return write.promise
    },
  }
  const queryExports = {}
  const queryImports = {
    "@tanstack/react-query": queryLibrary,
    "./api": { dataSource },
    "../../api/cache-policies": frontendRequire("./src/api/cache-policies.ts"),
    "../../lib/model-availability": {}, "../../lib/timezone": {},
  }
  runInNewContext(queryCode, { exports: queryExports, require: name => {
    assert.ok(name in queryImports, `Unexpected query import ${name}`)
    return queryImports[name]
  } })
  const { finopsKeys } = queryExports
  const key = finopsKeys.peopleBudgets(initial.period, initial.department_id, "", "all", 0, 50)
  if (seed) client.setQueryData(key, initial)
  const props = {
    period: initial.period, departmentId: initial.department_id, canManage: true,
    departments: [{ scope_id: "department-a", scope_name: "Department A" }, { scope_id: "department-b", scope_name: "Department B" }],
    models: [{ id: "model-a", display_name: "Alpha", model_key: "alpha", provider_name: "Unit", enabled: true }],
    onDepartmentChange: departmentId => { props.departmentId = departmentId },
  }
  const fibers = new Map()
  const timers = new Map()
  let active
  let effects = []
  let dirty = false
  let seen
  const hooks = {
    useState(initialValue) {
      const fiber = active
      const index = fiber.cursor++
      if (!(index in fiber.slots)) fiber.slots[index] = typeof initialValue === "function" ? initialValue() : initialValue
      return [fiber.slots[index], next => {
        const value = typeof next === "function" ? next(fiber.slots[index]) : next
        if (!Object.is(value, fiber.slots[index])) { fiber.slots[index] = value; dirty = true }
      }]
    },
    useMemo: compute => compute(),
    useEffect(effect, dependencies) {
      const fiber = active
      const index = fiber.cursor++
      const previous = fiber.slots[index]
      if (!previous || dependencies.some((value, position) => !Object.is(value, previous.dependencies[position]))) {
        effects.push(() => {
          previous?.cleanup?.()
          fiber.slots[index] = { dependencies, cleanup: effect() }
        })
      }
    },
  }
  const observerHooks = {
    useQueryClient: () => client,
    useQuery(options) {
      const [observer] = hooks.useState(() => {
        const current = new queryLibrary.QueryObserver(client, options)
        subscriptions.push(current.subscribe(() => {}))
        return current
      })
      observer.setOptions(options)
      return observer.getCurrentResult()
    },
    useMutation(options) {
      const [observer] = hooks.useState(() => new queryLibrary.MutationObserver(client, options))
      observer.setOptions(options)
      return { ...observer.getCurrentResult(), reset: () => observer.reset(), mutate: variables => {
        const pending = observer.mutate(variables)
        mutations.push(pending)
        pending.catch(() => {})
      } }
    },
  }
  const pageExports = {}
  const primitives = new Proxy({}, { get: (_target, name) => name })
  const pageImports = {
    react: hooks, "react/jsx-runtime": jsxRuntime, "@tanstack/react-query": observerHooks,
    "../api": { dataSource }, "../queries": queryExports,
    "../../../api/client": { ApiError: HttpFailure },
    "../../../locales/index": { getIntlLocale: () => "en-US", useLocale: () => ({ locale: "zh-CN" }) },
  }
  runInNewContext(pageCode, {
    exports: pageExports, Error, window: {
      setTimeout: callback => { const identity = Symbol(); timers.set(identity, callback); return identity },
      clearTimeout: identity => timers.delete(identity),
    }, require: name => pageImports[name] ?? primitives,
  })
  const component = (type, properties) => {
    seen.add(type)
    if (!fibers.has(type)) fibers.set(type, { slots: [], cursor: 0 })
    const previous = active
    active = fibers.get(type)
    active.cursor = 0
    try { return type(properties) }
    finally { active = previous }
  }
  const expand = value => {
    if (Array.isArray(value)) return value.map(expand)
    if (!value || typeof value !== "object") return value
    return { ...value, props: { ...value.props, children: expand(typeof value.type === "function"
      ? component(value.type, value.props) : value.props?.children) } }
  }
  const render = () => {
    let tree
    do {
      dirty = false
      seen = new Set()
      tree = expand(component(pageExports.PeopleBudgetWorkspace, props))
      const pendingEffects = effects
      effects = []
      for (const effect of pendingEffects) effect()
      for (const type of fibers.keys()) if (!seen.has(type)) fibers.delete(type)
    } while (dirty)
    return tree
  }
  const find = predicate => nodes(render()).find(predicate)
  const toggle = (label, checked) => {
    const control = find(node => node.props?.className === "people-update-toggle" && text(node).includes(label))
    assert.ok(control, `Missing update toggle ${label}`)
    nodes(control).find(node => node.type === "Checkbox").props.onCheckedChange(checked)
  }
  return {
    client, props, key, finopsKeys, write, writes, reads, render, find, toggle,
    budgetEditor: properties => { render(); return expand(component(pageExports.BudgetEditor, properties)) },
    finopsQueries: queryExports.finopsQueries,
    data: () => client.getQueryData(key),
    open: identity => {
      const row = identity ? find(node => node.props?.className === "people-table-row" && node.key === identity) : render()
      const control = nodes(row).find(node => node.props?.className === "people-budget-edit-button")
      assert.ok(control, `Missing person editor ${identity ?? "first"}`)
      control.props.onClick()
    },
    search: async value => {
      find(node => node.type === "Input" && node.props["aria-label"] === "搜索人员").props.onChange({ target: { value } })
      render()
      const pending = [...timers.values()]
      timers.clear()
      for (const callback of pending) callback()
      render()
      await tick()
    },
    status: value => {
      find(node => node.type === "Select" && nodes(node).some(child => child.props?.["aria-label"] === "人员预算状态"))
        .props.onValueChange(value)
      render()
    },
    allMatching: () => {
      find(node => node.type === "Checkbox" && node.props["aria-label"] === "选择当前页").props.onCheckedChange(true)
      find(node => node.type === "button" && text(node).startsWith("选择全部 ")).props.onClick()
      find(node => node.type === "Button" && text(node).startsWith("批量分配")).props.onClick()
    },
    model: checked => find(node => node.type === "Checkbox" && node.props["aria-label"] === "选择模型：Alpha").props.onCheckedChange(checked),
    submit: async () => { find(node => node.type === "form").props.onSubmit({ preventDefault() {} }); await tick() },
    settle: async result => { write.resolve(result); await mutations.at(-1); await tick() },
    reject: async error => { write.reject(error); await assert.rejects(mutations.at(-1), failure => failure === error); await tick() },
    refresh: () => client.invalidateQueries({ queryKey: key, exact: true }),
    close: async () => {
      for (const unsubscribe of subscriptions) unsubscribe()
      await client.cancelQueries()
      if (mutations.length) write.reject(new Error("People policy test closed"))
      for (const read of reads) read.resolve(initial)
      await Promise.allSettled(mutations)
      client.clear()
    },
  }
}

test("budget amount inputs remove only redundant zeros without floating-point rounding", context => {
  for (const [amount, expected] of [
    ["2000.00000000000000", "2000.00"],
    ["1.23000000000000", "1.23"],
    ["0.00000000000000", "0.00"],
    ["0.10000000000000", "0.10"],
    ["42", "42.00"],
    ["9999999999999999.99000000000000", "9999999999999999.99"],
    ["0.00000000000001", "0.00000000000001"],
    [null, ""], [undefined, ""],
  ]) {
    const view = harness()
    context.after(view.close)
    const item = person("organization-unit", "Unit organization", {
      scope_type: "organization", parent_scope_id: null, budget_mode: "usd", usd_limit: amount,
    })
    const writes = []
    const tree = view.budgetEditor({ item, items: [item], period: "2026-11", busy: false,
      error: null, onClose() {}, onDelete() {}, onSave: value => writes.push(plain(value)) })
    const input = nodes(tree).find(node => node.type === "Input" && node.props.name === "usd_limit")
    assert.ok(input)
    assert.equal(input.props.value, expected)
    if (expected) {
      nodes(tree).find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
      const valid = !["0.00", "0.00000000000001"].includes(expected)
      assert.equal(writes.length, valid ? 1 : 0)
      if (valid) {
        assert.equal(writes[0].usd_limit, expected)
        assert.equal(writes[0].budget_mode, "usd")
      }
    }
  }
})

test("the person budget editor uses the same compact USD input", context => {
  const view = harness(peopleResponse({ department_usd_limit: "5000.00000000000000", items: [
    person("alice", "Alice", { budget_mode: "usd", usd_limit: "2000.00000000000000" }),
  ] }))
  context.after(view.close)
  view.open("alice")
  const input = view.find(node => node.type === "Input" && node.props.name === "usd_limit")
  assert.ok(input)
  assert.equal(input.props.value, "2000.00")
})

test("confirmed editor save updates the selected visible policy before authoritative refresh", async context => {
  const view = harness()
  context.after(view.close)
  view.open()
  view.toggle("更新模型访问", true)
  view.model(true)
  await view.submit()
  assert.deepEqual(view.writes, [{ period: "2026-11", request: {
    department_id: "department-a", selection: "ids", user_ids: ["alice"], status: "all",
    allocation_mode: "preserve", warning_threshold_percent: 80, model_ids: ["model-a"],
  } }])
  assert.equal(view.data().items[0].model_policy_configured, false, "pending writes must not grant model access")
  assert.equal(view.find(node => node.type === "Button" && node.props.type === "submit").props.disabled, true)
  assert.ok(!text(view.render()).includes("已为 1 人更新模型访问"))
  await view.settle(savedResult())
  assert.equal(view.data().items[0].model_policy_configured, true, "confirmed save must replace stale unconfigured policy")
  assert.deepEqual(Array.from(view.data().items[0].allowed_model_ids), ["model-a"])
  assert.equal(view.data().items[1].model_policy_configured, false)
  const row = view.find(node => node.props?.className === "people-table-row" && text(node).includes("Alice"))
  assert.ok(text(row).includes("Alpha"))
  assert.ok(!text(row).includes("未配置"))
  assert.ok(text(view.render()).includes("已为 1 人更新模型访问"))
  assert.equal(view.reads.length, 1, "authoritative People query must still be invalidated")
  assert.deepEqual(plain(view.reads[0].args), ["2026-11", "department-a", "", "all", 0, 50])
  assert.equal(view.client.getQueryState(view.key).fetchStatus, "fetching")
})

test("ID selection synchronizes known pages and budget trees without changing amounts or other scopes", async context => {
  const view = harness()
  context.after(view.close)
  const selected = person("alice", "Alice", { token_limit: 100, updated_by: "original-owner" })
  const unselected = person("charlie", "Charlie")
  const otherDepartment = person("other", "Other", { parent_scope_id: "department-b" })
  const filteredKey = view.finopsKeys.peopleBudgets("2026-11", "department-a", "Alice", "all", 0, 50)
  const nextPageKey = view.finopsKeys.peopleBudgets("2026-11", "department-a", "", "all", 50, 50)
  const otherDepartmentKey = view.finopsKeys.peopleBudgets("2026-11", "department-b", "", "all", 0, 50)
  const otherPeriodKey = view.finopsKeys.peopleBudgets("2026-12", "department-a", "", "all", 0, 50)
  const budgetKey = view.finopsKeys.budgets("2026-11")
  const previewKey = [...budgetKey, "allocation-preview"]
  const otherBudgetKey = view.finopsKeys.budgets("2026-12")
  view.client.setQueryData(filteredKey, peopleResponse({ items: [selected], total: 1 }))
  view.client.setQueryData(nextPageKey, peopleResponse({ items: [selected, unselected], offset: 50 }))
  view.client.setQueryData(otherDepartmentKey, peopleResponse({ department_id: "department-b", items: [otherDepartment] }))
  view.client.setQueryData(otherPeriodKey, peopleResponse({ period: "2026-12", items: [selected] }))
  const budget = { period: "2026-11", items: [selected, unselected, otherDepartment], history: [], risk_count: 7 }
  view.client.setQueryData(budgetKey, budget)
  view.client.setQueryData(previewKey, budget)
  view.client.setQueryData(otherBudgetKey, { ...budget, period: "2026-12" })
  view.open()
  view.toggle("更新模型访问", true)
  view.model(true)
  await view.submit()
  await view.settle(savedResult())
  for (const key of [filteredKey, nextPageKey, budgetKey, previewKey]) {
    const data = view.client.getQueryData(key)
    assert.deepEqual(plain(data.items[0]), { ...selected, model_policy_configured: true, allowed_model_ids: ["model-a"] })
  }
  assert.deepEqual(plain(view.client.getQueryData(nextPageKey).items[1]), unselected)
  assert.deepEqual(plain(view.client.getQueryData(budgetKey).items.slice(1)), [unselected, otherDepartment])
  assert.deepEqual(plain(view.client.getQueryData(otherDepartmentKey).items), [otherDepartment])
  assert.deepEqual(plain(view.client.getQueryData(otherPeriodKey).items), [selected])
  assert.deepEqual(plain(view.client.getQueryData(otherBudgetKey).items), budget.items)
  assert.equal(view.client.getQueryState(budgetKey).isInvalidated, true)
  assert.equal(view.client.getQueryState(previewKey).isInvalidated, true)
  assert.equal(view.client.getQueryState(otherBudgetKey).isInvalidated, false)
  assert.equal(view.client.getQueryState(otherDepartmentKey).isInvalidated, false)
  assert.equal(view.data().model_configured_count, 0, "do not invent department summary counts from a page")
})

test("filtered all_matching submits its search and status and updates only known matching identities", async context => {
  const alice = person("alice", "Team Alice")
  const bob = person("bob", "Team Bob")
  const charlie = person("charlie", "Charlie")
  const assigned = person("dora", "Team Dora", { token_limit: 100, status: "healthy" })
  const view = harness(peopleResponse({ total: 4, items: [alice, bob, charlie, assigned] }))
  context.after(view.close)
  await view.search(" Team ")
  view.reads.at(-1).resolve(peopleResponse({ total: 3, items: [alice] }))
  await tick()
  view.status("unallocated")
  view.reads.at(-1).resolve(peopleResponse({ total: 3, items: [alice] }))
  await tick()
  const matchingPage = view.finopsKeys.peopleBudgets("2026-11", "department-a", "Team", "unallocated", 50, 50)
  const wrongStatus = view.finopsKeys.peopleBudgets("2026-11", "department-a", "Team", "assigned", 0, 50)
  view.client.setQueryData(matchingPage, peopleResponse({ total: 3, offset: 50, items: [bob] }))
  view.client.setQueryData(wrongStatus, peopleResponse({ total: 1, items: [assigned] }))
  view.allMatching()
  view.toggle("更新月度预算", false)
  view.toggle("更新模型访问", true)
  view.model(true)
  await view.submit()
  assert.deepEqual(view.writes[0].request, { department_id: "department-a", selection: "all_matching",
    user_ids: [], query: "Team", status: "unallocated", allocation_mode: "preserve",
    warning_threshold_percent: 80, model_ids: ["model-a"] })
  await view.settle(savedResult({ updated_count: 3, model_policy_updated_count: 3 }))
  assert.deepEqual(view.data().items.map(item => item.model_policy_configured), [true, true, false, false])
  assert.equal(view.client.getQueryData(matchingPage).items[0].model_policy_configured, true)
  assert.equal(view.client.getQueryData(wrongStatus).items[0].model_policy_configured, false)
  assert.equal(view.data().items.length, 4, "aggregate acknowledgement must not fabricate uncached people")
  assert.ok(text(view.render()).includes("已为 3 人更新模型访问"))
  assert.deepEqual(plain(view.reads.at(-1).args), ["2026-11", "department-a", "Team", "unallocated", 0, 50])
})

test("filtered all_matching excludes invalidated pages after Bob leaves the 52-person unallocated selection", async context => {
  const matching = Array.from({ length: 51 }, (_value, index) => person(`matching-${index}`))
  const bob = person("bob", "Bob")
  const firstPage = matching.slice(0, 50)
  const view = harness(peopleResponse({ total: 52, unallocated_count: 52, items: firstPage }))
  context.after(view.close)
  view.status("unallocated")
  view.reads.at(-1).resolve(peopleResponse({ total: 52, unallocated_count: 52, items: firstPage }))
  await tick()
  const activeKey = view.finopsKeys.peopleBudgets("2026-11", "department-a", "", "unallocated", 0, 50)
  const oldPageKey = view.finopsKeys.peopleBudgets("2026-11", "department-a", "", "unallocated", 50, 50)
  const oldPage = peopleResponse({ total: 52, unallocated_count: 52, offset: 50, items: [matching[50], bob] })
  view.client.setQueryData(oldPageKey, oldPage)
  const assignedBob = { ...bob, token_limit: 100, status: "healthy" }
  const budgetKey = view.finopsKeys.budgets("2026-11")
  const budget = { period: "2026-11", items: [...matching, assignedBob] }
  view.client.setQueryData(budgetKey, budget)
  const refreshed = view.client.invalidateQueries({
    queryKey: ["finops", "budgets", "people", "2026-11", "department-a"],
  })
  view.reads.at(-1).resolve(peopleResponse({ total: 51, assigned_count: 1, unallocated_count: 51,
    department_allocated_tokens: 100, department_available_tokens: 9900, items: firstPage }))
  await refreshed
  await tick()
  assert.equal(view.client.getQueryState(activeKey).isInvalidated, false)
  assert.equal(view.client.getQueryState(oldPageKey).isInvalidated, true)
  view.allMatching()
  view.toggle("更新月度预算", false)
  view.toggle("更新模型访问", true)
  view.model(true)
  await view.submit()
  assert.deepEqual(view.writes[0].request, { department_id: "department-a", selection: "all_matching",
    user_ids: [], query: "", status: "unallocated", allocation_mode: "preserve",
    warning_threshold_percent: 80, model_ids: ["model-a"] })
  await view.settle(savedResult({ updated_count: 51, model_policy_updated_count: 51 }))
  const cachedOldPage = view.client.getQueryData(oldPageKey)
  assert.equal(cachedOldPage.items[1].model_policy_configured, false,
    "Bob is now assigned and must not receive the unallocated selection's model policy")
  assert.deepEqual(plain(cachedOldPage), oldPage,
    "invalidated off-page identities remain unknown until authoritative refresh")
  const cachedBudget = view.client.getQueryData(budgetKey)
  assert.deepEqual(plain(cachedBudget.items.slice(50)), [matching[50], assignedBob])
  for (const key of [activeKey, budgetKey]) {
    assert.ok(view.client.getQueryData(key).items.slice(0, 50).every(item => item.model_policy_configured))
    assert.ok(view.client.getQueryData(key).items.slice(0, 50)
      .every(item => item.allowed_model_ids.length === 1 && item.allowed_model_ids[0] === "model-a"))
    assert.equal(view.client.getQueryState(key).isInvalidated, true)
  }
  assert.equal(view.client.getQueryState(oldPageKey).isInvalidated, true)
  assert.equal(view.client.getQueryData(activeKey).total, 51)
  assert.equal(view.client.getQueryData(activeKey).model_configured_count, 0)
  assert.ok(text(view.render()).includes("已为 51 人更新模型访问"))
  assert.deepEqual(plain(view.reads.at(-1).args), ["2026-11", "department-a", "", "unallocated", 0, 50])
})

test("unfiltered all_matching also confirms department members cached only under other searches", async context => {
  const view = harness(peopleResponse({ total: 3, items: [person("alice", "Alice")] }))
  context.after(view.close)
  const searchKey = view.finopsKeys.peopleBudgets("2026-11", "department-a", "Bob", "assigned", 0, 50)
  view.client.setQueryData(searchKey, peopleResponse({ total: 1, items: [person("bob", "Bob", { token_limit: 100 })] }))
  const budgetKey = view.finopsKeys.budgets("2026-11")
  view.client.setQueryData(budgetKey, { period: "2026-11", items: [person("charlie", "Charlie"),
    person("other", "Other", { parent_scope_id: "department-b" })] })
  view.allMatching()
  view.toggle("更新月度预算", false)
  view.toggle("更新模型访问", true)
  view.model(true)
  await view.submit()
  await view.settle(savedResult({ updated_count: 3, model_policy_updated_count: 3 }))
  assert.equal(view.client.getQueryData(searchKey).items[0].model_policy_configured, true)
  assert.deepEqual(Array.from(view.client.getQueryData(searchKey).items[0].allowed_model_ids), ["model-a"])
  assert.equal(view.client.getQueryData(budgetKey).items[0].model_policy_configured, true)
  assert.deepEqual(Array.from(view.client.getQueryData(budgetKey).items[0].allowed_model_ids), ["model-a"])
  assert.equal(view.client.getQueryData(budgetKey).items[1].model_policy_configured, false)
})

test("late save confirmation stays bound to its submitted period and department", async context => {
  for (const change of [{ departmentId: "department-b" }, { period: "2026-12" }]) {
    const view = harness()
    context.after(view.close)
    view.open()
    view.toggle("更新模型访问", true)
    view.model(true)
    await view.submit()
    Object.assign(view.props, change)
    const activeKey = view.finopsKeys.peopleBudgets(view.props.period, view.props.departmentId, "", "all", 0, 50)
    const activeData = peopleResponse({ period: view.props.period, department_id: view.props.departmentId,
      items: [person("other", "Other", { parent_scope_id: view.props.departmentId })] })
    view.client.setQueryData(activeKey, activeData)
    view.render()
    await view.settle(savedResult())
    assert.equal(view.writes[0].period, "2026-11")
    assert.equal(view.writes[0].request.department_id, "department-a")
    assert.equal(view.data().items[0].model_policy_configured, true)
    assert.deepEqual(plain(view.client.getQueryData(activeKey)), activeData)
    assert.equal(view.client.getQueryState(view.key).isInvalidated, true)
    assert.equal(view.client.getQueryState(activeKey).isInvalidated, false)
    assert.ok(!text(view.render()).includes("已为 1 人更新模型访问"))
  }
})

test("an omitted model update remains distinct from explicit deny-all", async context => {
  const untouched = harness()
  context.after(untouched.close)
  untouched.open()
  untouched.toggle("更新月度预算", true)
  untouched.find(node => node.type === "Input" && node.props.name === "token_limit").props.onChange({ target: { value: "100" } })
  await untouched.submit()
  assert.equal(Object.hasOwn(untouched.writes[0].request, "model_ids"), false)
  await untouched.settle(savedResult({ token_limit_per_user: 100, total_allocated: 100, model_policy_updated_count: 0 }))
  assert.equal(untouched.data().items[0].model_policy_configured, false)
  assert.equal(untouched.data().items[0].token_limit, null, "budget calculations remain authoritative")
  assert.ok(text(untouched.find(node => node.props?.className === "people-model-cell")).includes("未配置"))
  const denied = harness(peopleResponse({ items: [person("alice", "Alice", {
    model_policy_configured: true, allowed_model_ids: ["model-a"],
  })] }))
  context.after(denied.close)
  denied.open()
  denied.model(false)
  await denied.submit()
  assert.deepEqual(denied.writes[0].request.model_ids, [])
  assert.deepEqual(Array.from(denied.data().items[0].allowed_model_ids), ["model-a"])
  await denied.settle(savedResult())
  assert.equal(denied.data().items[0].model_policy_configured, true)
  assert.deepEqual(Array.from(denied.data().items[0].allowed_model_ids), [])
  assert.ok(text(denied.find(node => node.props?.className === "people-model-cell")).includes("禁止全部"))
})

test("incomplete or differently bound acknowledgements never invent successful model permissions", async context => {
  for (const result of [
    savedResult({ model_policy_updated_count: 0 }), savedResult({ updated_count: 2 }),
    savedResult({ department_id: "department-b" }), savedResult({ period: "2026-12" }),
  ]) {
    const view = harness()
    context.after(view.close)
    view.open()
    view.toggle("更新模型访问", true)
    view.model(true)
    await view.submit()
    await view.settle(result)
    assert.equal(view.data().items[0].model_policy_configured, false)
    assert.deepEqual(Array.from(view.data().items[0].allowed_model_ids), [])
    assert.equal(view.reads.length, 1)
  }
})

test("query loading and empty results remain distinct without inventing unconfigured rows", async context => {
  const view = harness(peopleResponse(), { seed: false })
  context.after(view.close)
  const loading = text(view.render())
  assert.ok(loading.includes("正在加载人员"))
  assert.ok(!loading.includes("没有符合条件的人员"))
  assert.equal(view.find(node => node.props?.className === "people-table-row"), undefined)
  assert.deepEqual(plain(view.reads[0].args), ["2026-11", "department-a", "", "all", 0, 50])
  view.reads[0].resolve(peopleResponse({ total: 0, items: [] }))
  await tick()
  assert.ok(text(view.render()).includes("没有符合条件的人员"))
  assert.ok(text(view.render()).includes("共 0 人"))
})

test("O15 local search retains filtered totals and department summaries; stale-count observation is not reproduced", async context => {
  const summary = { assigned_count: 2, model_configured_count: 3, risk_count: 1 }
  const view = harness(peopleResponse({ total: 5, ...summary }))
  context.after(view.close)
  assert.ok(text(view.render()).includes("共 5 人"))
  await view.search(" Alice ")
  assert.ok(text(view.render()).includes("正在加载人员"))
  assert.ok(!text(view.render()).includes("共 5 人"))
  assert.deepEqual(plain(view.reads.at(-1).args), ["2026-11", "department-a", "Alice", "all", 0, 50])
  view.reads.at(-1).resolve(peopleResponse({ total: 1, items: [person("alice", "Alice")], ...summary }))
  await tick()
  const filtered = text(view.render())
  for (const expected of ["共 1 人", "2 预算已配置", "3 模型已配置", "1 风险"]) assert.ok(filtered.includes(expected), expected)
  await view.search("Nobody")
  view.reads.at(-1).resolve(peopleResponse({ total: 0, items: [], ...summary }))
  await tick()
  const empty = text(view.render())
  for (const expected of ["共 0 人", "没有符合条件的人员", "2 预算已配置", "3 模型已配置"]) assert.ok(empty.includes(expected), expected)
})

test("refresh failure preserves confirmed policy and remains an explicit query failure", async context => {
  const view = harness()
  context.after(view.close)
  view.open()
  view.toggle("更新模型访问", true)
  view.model(true)
  await view.submit()
  await view.settle(savedResult())
  view.reads[0].reject(new Error("authoritative refresh unavailable"))
  await tick()
  assert.equal(view.data().items[0].model_policy_configured, true)
  assert.deepEqual(Array.from(view.data().items[0].allowed_model_ids), ["model-a"])
  const failed = text(view.render())
  assert.ok(failed.includes("人员预算加载失败"))
  assert.ok(failed.includes("authoritative refresh unavailable"))
  assert.ok(failed.includes("已为 1 人更新模型访问"))
  assert.equal(view.find(node => node.props?.className === "people-table-row"), undefined)
  const refresh = view.refresh()
  const authoritative = peopleResponse({ model_configured_count: 1, items: [person("alice", "Alice", {
    model_policy_configured: true, allowed_model_ids: [], token_limit: 123,
  })] })
  view.reads.at(-1).resolve(authoritative)
  await refresh
  await tick()
  assert.deepEqual(plain(view.data()), authoritative)
  assert.ok(text(view.render()).includes("禁止全部"))
})

test("an older in-flight query cannot erase the confirmed policy while authoritative refresh is pending", async context => {
  const view = harness()
  context.after(view.close)
  view.render()
  const olderRefresh = view.refresh()
  const older = view.reads[0]
  try {
    view.open()
    view.toggle("更新模型访问", true)
    view.model(true)
    await view.submit()
    await view.settle(savedResult())
    assert.equal(view.reads.length, 2)
    older.resolve(peopleResponse())
    await tick()
    assert.equal(view.data().items[0].model_policy_configured, true)
    view.reads[1].resolve(peopleResponse({ model_configured_count: 1, items: [person("alice", "Alice", {
      model_policy_configured: true, allowed_model_ids: ["model-a"],
    })] }))
    await olderRefresh
    await tick()
    assert.equal(view.data().model_configured_count, 1)
  } finally {
    for (const read of view.reads) read.resolve(peopleResponse())
    await olderRefresh
  }
})

test("write refusal, allocation conflict and transport failure do not apply or announce successful permissions", async context => {
  for (const failure of [new HttpFailure(403, "Owner permission required"),
    new HttpFailure(409, "User allocations would exceed the department budget"), new Error("write transport failed")]) {
    const view = harness()
    context.after(view.close)
    view.open()
    view.toggle("更新模型访问", true)
    view.model(true)
    await view.submit()
    await view.reject(failure)
    assert.equal(view.data().items[0].model_policy_configured, false)
    assert.deepEqual(Array.from(view.data().items[0].allowed_model_ids), [])
    assert.equal(view.reads.length, 0)
    assert.equal(view.writes.length, 1)
    const rendered = text(view.render())
    assert.ok(rendered.includes(failure.status === 409 ? "人员额度合计超过部门可用预算" : failure.message))
    assert.ok(!rendered.includes("已为 1 人更新"))
    assert.ok(view.find(node => node.type === "form"))
    assert.equal(view.find(node => node.type === "Button" && node.props.type === "submit").props.disabled, false)
  }
})

test("read-only People views expose neither editor submission nor selection controls", context => {
  const view = harness()
  context.after(view.close)
  view.props.canManage = false
  assert.equal(view.find(node => node.props?.className === "people-budget-edit-button"), undefined)
  assert.equal(view.find(node => node.type === "Checkbox"), undefined)
  assert.equal(view.find(node => node.type === "Button" && text(node).startsWith("批量分配")), undefined)
  assert.equal(view.find(node => node.type === "form"), undefined)
  assert.equal(view.writes.length, 0)
})