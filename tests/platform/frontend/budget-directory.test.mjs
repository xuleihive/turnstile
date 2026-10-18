import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire, registerHooks, stripTypeScriptTypes } from "node:module"
import test from "node:test"
import { fileURLToPath, pathToFileURL } from "node:url"
import vm from "node:vm"

// Node's type-stripping runner does not resolve Vite's extensionless TS imports.
registerHooks({
  resolve(specifier, context, nextResolve) {
    return nextResolve(specifier === "./directory-visibility" ? `${specifier}.ts` : specifier, context)
  },
})
const {
  budgetDirectoryItems, budgetScopeDepartments, budgetScopeTeams, filterBudgetPeople,
  readDepartmentBudgetPeople, readTeamBudgetMembers, selectedBudgetDepartment,
} = await import("../../../frontend/src/lib/budget-directory.ts")

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(pathToFileURL(frontendRequire.resolve("rolldown")).href)
const pageEntry = fileURLToPath(new URL("../../../frontend/src/data-sources/apim/pages/budget-page.tsx", import.meta.url))
const pageBundle = await rolldown({ input: pageEntry, external: id => id !== pageEntry,
  treeshake: false, transform: { jsx: { runtime: "automatic" } } })
let pageCode
try { pageCode = (await pageBundle.generate({ format: "cjs" })).output[0].code }
finally { await pageBundle.close() }

function budgetComponents(people = []) {
  const module = { exports: {} }
  const components = new Proxy({}, { get: (_target, name) => `unit-${String(name).toLowerCase()}` })
  vm.runInNewContext(`${pageCode}\nmodule.exports.BudgetKpis = BudgetKpis; module.exports.PeopleBudgetWorkspace = PeopleBudgetWorkspace`, {
    module, exports: module.exports, Intl, Date, Set, Map,
    require: name => {
      if (["react", "react/jsx-runtime"].includes(name)) return frontendRequire(name)
      if (name === "@tanstack/react-query") return {
        useQueryClient: () => ({}),
        useMutation: () => ({ reset() {}, isPending: false }),
        useQuery: options => ({ data: options.queryKey[0] === "finops"
          ? { items: people, departments: [] } : undefined }),
      }
      if (name === "../../../locales/index") return {
        getIntlLocale: () => "en-US", useLocale: () => ({ locale: "en" }),
      }
      if (name === "../../../lib/budget-directory") return {
        filterBudgetPeople, selectedBudgetDepartment,
      }
      if (name === "lucide-react") return new Proxy({}, { get: () => () => null })
      return components
    },
  })
  return module.exports
}

function renderBudget(component, props) {
  return frontendRequire("react-dom/server").renderToStaticMarkup(
    frontendRequire("react").createElement(component, props),
  )
}

const directory = {
  organizations: [
    { id: "org", name: "Z enabled", status: "active" },
    { id: "archived", name: "A archived", status: "archived" },
  ],
  units: [
    { id: "dept-b", name: "B department", kind: "department", organization_id: "org", status: "active" },
    { id: "dept-a", name: "A archived dept", kind: "department", organization_id: "org", status: "inactive" },
    { id: "foreign", name: "Foreign", kind: "department", organization_id: "archived", status: "active" },
    { id: "team-b", name: "B team", kind: "team", organization_id: "org", parent_unit_id: "dept-b", status: "active" },
    { id: "team-a", name: "A archived team", kind: "team", organization_id: "org", parent_unit_id: "dept-b", status: "archived" },
    { id: "team-other", name: "Other team", kind: "team", organization_id: "org", parent_unit_id: "dept-a", status: "active" },
    { id: "foreign-team", name: "Foreign team", kind: "team", organization_id: "archived", parent_unit_id: "foreign", status: "active" },
  ],
}
const scope = (type, id, name, parent = null) => ({
  scope_type: type, scope_id: id, scope_name: name, parent_scope_id: parent,
  status: "exceeded", token_limit: 100, used_tokens: 123, remaining_tokens: -23,
  forecast_tokens: 123, unpriced_usage_count: 0,
})
const items = [
  scope("organization", "archived", "A archived"),
  scope("department", "foreign", "Foreign", "archived"),
  scope("organization", "org", "Z enabled"),
  scope("department", "dept-a", "A archived dept", "org"),
  scope("department", "dept-b", "B department", "org"),
]
const people = [
  { ...scope("user", "a@example.com", "Alice", "dept-b"), status: "healthy", model_policy_configured: true },
  { ...scope("user", "b@example.com", "Bob", "dept-a"), token_limit: null, status: "unallocated" },
  { ...scope("user", "c@example.com", "Carol", "dept-b"), status: "exceeded" },
]

test("department editing recognizes Token and USD parent budgets, including zero limits", () => {
  const source = readFileSync(new URL("../../../frontend/src/data-sources/apim/pages/budget-page.tsx", import.meta.url), "utf8")
  const helper = source.slice(source.indexOf("function budgetLimit("), source.indexOf("function budgetUsdMetric("))
  const budgetLimit = new Function(`${stripTypeScriptTypes(helper)}; return budgetLimit`)()
  const parentCheck = source.match(/parentAllocated: ([^}\n]+) \}\)/g)
    .find(row => row.includes("organization"))
    .replace(/^parentAllocated: /, "").replace(/ \}\)$/, "")
  const parentAllocated = new Function("budgetLimit", "organization", `return ${parentCheck}`)
  for (const [parent, expected] of [
    [{ budget_mode: "tokens", token_limit: 100, usd_limit: null }, true],
    [{ budget_mode: "usd", token_limit: null, usd_limit: "100.00" }, true],
    [{ budget_mode: "tokens", token_limit: 0, usd_limit: null }, true],
    [{ budget_mode: "usd", token_limit: null, usd_limit: "0.00" }, true],
    [{ budget_mode: "tokens", token_limit: null, usd_limit: null }, false],
    [{ budget_mode: "usd", token_limit: null, usd_limit: null }, false],
    [{ token_limit: 100 }, true],
  ]) assert.equal(parentAllocated(budgetLimit, parent), expected, JSON.stringify(parent))
})

for (const [name, organizations, amount, assigned] of [
  ["USD only", [{ budget_mode: "usd", token_limit: null, usd_limit: "100.00" }], "$100.00", 1],
  ["USD zero", [{ budget_mode: "usd", token_limit: null, usd_limit: "0.00" }], "$0.00", 1],
  ["mixed units", [{ token_limit: 1000 }, { budget_mode: "usd", token_limit: null, usd_limit: "100.00" }], "1,000 Token / $100.00", 2],
  ["legacy Token", [{ token_limit: 1000 }], "1,000", 1],
  ["unallocated", [{ token_limit: null }], "未分配", 0],
]) {
  test(`organization budget KPI preserves upstream ${name} semantics`, () => {
    const { BudgetKpis } = budgetComponents()
    const markup = renderBudget(BudgetKpis, { data: {
      coverage: "department", risk_count: 0,
      items: organizations.map((row, i) => ({ ...scope("organization", `org-${i}`, "Org"), ...row })),
    } })
    assert.ok(markup.includes("可见部门预算"))
    assert.ok(markup.includes(amount), markup)
    assert.ok(markup.includes(`${assigned} 个组织已配置`), markup)
  })
}

test("directory people preserve USD allocation, spent and remaining display", () => {
  const person = { ...people[0], budget_mode: "usd", token_limit: null, usd_limit: "20.00",
    used_tokens: 0, used_usd: "3.00", remaining_tokens: null, remaining_usd: "17.00",
    usage_percent: 15, warning_threshold_percent: 80, allowed_model_ids: [] }
  const { PeopleBudgetWorkspace } = budgetComponents([person])
  const markup = renderBudget(PeopleBudgetWorkspace, { period: "2026-10", canManage: false,
    departments: [], models: [], scope: { type: "department", id: "dept-b" },
    scopeName: "Department", teams: [], teamId: "", onTeamChange() {} })
  for (const text of ["1 预算已配置", "$20.00", "$3.000", "$17.00", "USD", "预警 80%"])
    assert.ok(markup.includes(text), `${text}: ${markup}`)
  assert.ok(!markup.includes("月度 Token 额度"))
})

test("directory people retain legacy Token allocation display", () => {
  const person = { ...people[0], token_limit: 1000, used_tokens: 100, remaining_tokens: 900,
    usage_percent: 10, warning_threshold_percent: 80, allowed_model_ids: [] }
  const { PeopleBudgetWorkspace } = budgetComponents([person])
  const markup = renderBudget(PeopleBudgetWorkspace, { period: "2026-10", canManage: false,
    departments: [], models: [], scope: { type: "department", id: "dept-b" },
    scopeName: "Department", teams: [], teamId: "", onTeamChange() {} })
  for (const text of ["1 预算已配置", "1,000", "900", "Token", "预警 80%"])
    assert.ok(markup.includes(text), `${text}: ${markup}`)
})

test("directory people do not render unknown USD usage as zero", () => {
  const person = { ...people[0], budget_mode: "usd", token_limit: null, usd_limit: "20.00",
    used_tokens: 0, used_usd: null, remaining_tokens: null, remaining_usd: null,
    usage_percent: null, status: "unverified", warning_threshold_percent: 80, allowed_model_ids: [] }
  const { PeopleBudgetWorkspace } = budgetComponents([person])
  const markup = renderBudget(PeopleBudgetWorkspace, { period: "2026-10", canManage: false,
    departments: [], models: [], scope: { type: "department", id: "dept-b" },
    scopeName: "Department", teams: [], teamId: "", onTeamChange() {} })
  assert.ok(markup.includes("1 预算已配置"))
  assert.ok(markup.includes("等待金额数据"))
  assert.ok(markup.includes("用量未验证"))
  assert.ok(!markup.includes("$0.000"))
})

test("archive display and sorting do not change any budget status, limit or attribution", () => {
  const frozen = Object.freeze(items.map(row => Object.freeze(row)))
  const before = structuredClone(frozen)
  const visible = budgetDirectoryItems(frozen, directory, true, "en")
  assert.deepEqual(visible.map(row => row.scope_id), ["dept-b", "org"])
  const all = budgetDirectoryItems(frozen, directory, false, "en")
  assert(all.findIndex(row => row.scope_id === "org") < all.findIndex(row => row.scope_id === "archived"))
  assert(all.findIndex(row => row.scope_id === "dept-b") < all.findIndex(row => row.scope_id === "dept-a"))
  for (const row of all) {
    const original = items.find(item => item.scope_id === row.scope_id)
    for (const key of Object.keys(original)) assert.deepEqual(row[key], original[key])
  }
  assert.deepEqual(frozen, before)
})

test("organization and department scope lists keep the original primary budget hierarchy", () => {
  assert.deepEqual(budgetScopeDepartments(items, { type: "organization", id: "org" }).map(row => row.scope_id),
    ["dept-a", "dept-b"])
  assert.deepEqual(budgetScopeDepartments(items, { type: "department", id: "dept-b" }).map(row => row.scope_id),
    ["dept-b"])
  assert.deepEqual(budgetScopeTeams(directory, { type: "department", id: "dept-b" }, true, "en").map(row => row.id),
    ["team-b"])
  assert.deepEqual(budgetScopeTeams(directory, { type: "department", id: "dept-b" }, false, "en").map(row => row.id),
    ["team-b", "team-a"])
  assert.deepEqual(budgetScopeTeams(directory, { type: "organization", id: "org" }, true, "en").map(row => row.id),
    ["team-b", "team-other"])
})

test("team intersection, name/email search and budget filters do not change user records", () => {
  assert.deepEqual(filterBudgetPeople(people, "", "all", null, "en"), people)
  assert.deepEqual(filterBudgetPeople(people, "", "all", new Set(["b@example.com"]), "en"), [people[1]])
  assert.deepEqual(filterBudgetPeople(people, "c@example", "all", null, "en"), [people[2]])
  assert.deepEqual(filterBudgetPeople(people, "ALICE", "healthy", null, "en"), [people[0]])
  assert.deepEqual(filterBudgetPeople(people, "", "assigned", null, "en"), [people[0], people[2]])
  assert.deepEqual(filterBudgetPeople(people, "", "all", new Set(), "en"), [])
})

test("assigned people filtering recognizes USD budgets without changing directory scope", () => {
  const usd = { ...people[0], scope_id: "usd", budget_mode: "usd", token_limit: null, usd_limit: "20.00" }
  const zero = { ...usd, scope_id: "zero", usd_limit: "0.00" }
  const absent = { ...usd, scope_id: "absent", usd_limit: null, status: "unallocated" }
  const all = [...people, usd, zero, absent]
  assert.deepEqual(new Set(filterBudgetPeople(all, "", "assigned", null, "en").map(row => row.scope_id)),
    new Set([people[0].scope_id, people[2].scope_id, "usd", "zero"]))
  assert.deepEqual(filterBudgetPeople(all, "", "assigned", new Set(["usd"]), "en"), [usd])
  assert.deepEqual(new Set(filterBudgetPeople(all, "", "unallocated", null, "en").map(row => row.scope_id)),
    new Set([people[1].scope_id, "absent"]))
})

test("bulk editing resolves only one unchanged primary department", () => {
  assert.equal(selectedBudgetDepartment(people, new Set(["a@example.com", "c@example.com"])), "dept-b")
  assert.equal(selectedBudgetDepartment(people, new Set(["a@example.com", "b@example.com"])), null)
  assert.equal(selectedBudgetDepartment(people, new Set(["missing"])), null)
  assert.equal(selectedBudgetDepartment(people, new Set()), null)
})

test("organization read aggregation loads all pages without inventing write requests", async () => {
  const calls = []
  const result = await readDepartmentBudgetPeople(["dept-b", "dept-a"], async (id, offset, limit) => {
    calls.push([id, offset, limit])
    return {
      department_id: id, department_token_limit: 1000, department_available_tokens: 500,
      total: id === "dept-b" ? 201 : 1,
      items: id === "dept-b" ? Array.from({ length: offset === 0 ? 200 : 1 }, (_, i) =>
        scope("user", `person-${offset + i}`, `Person ${offset + i}`, id)) : [people[1]],
    }
  })
  assert.equal(result.items.length, 202)
  assert.deepEqual(calls, [["dept-b", 0, 200], ["dept-a", 0, 200], ["dept-b", 200, 200]])
  assert.equal(result.departments[0].department_token_limit, 1000)
  await assert.rejects(readDepartmentBudgetPeople(["dept"], async () => ({ total: 1, items: [] })), /变更/)
})

test("team membership pagination collects every governance ID and rejects repeated cursors", async () => {
  const ids = await readTeamBudgetMembers(async cursor => ({
    items: [{ governance_user_id: cursor ? "b@example.com" : "a@example.com" }],
    next_cursor: cursor ? null : "50",
  }))
  assert.deepEqual([...ids], ["a@example.com", "b@example.com"])
  await assert.rejects(readTeamBudgetMembers(async () => ({
    items: [], next_cursor: "50",
  })), /变更/)
})
