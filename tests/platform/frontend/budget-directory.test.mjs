import assert from "node:assert/strict"
import { registerHooks } from "node:module"
import test from "node:test"

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
