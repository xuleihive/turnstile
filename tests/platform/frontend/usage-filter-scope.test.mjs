import assert from "node:assert/strict"
import test from "node:test"
import {
  usageArchiveSelection, usageArchiveFilter,
  usageSelection, usageFacetWindow, usageFacetOptions, reconcileUsageScope,
} from "../../../frontend/src/lib/usage-filter-scope.ts"

const entity = (id, parents = []) => ({ id, name: id, parent_id: parents[0] ?? null, parent_ids: parents })
const catalog = {
  organizations: [entity("o1"), entity("o2")],
  departments: [entity("d1", ["o1"]), entity("d2", ["o2"]), entity("d3", ["o2"])],
  projects: [entity("p1", ["d1"]), entity("p2", ["d2"])],
  agents: [entity("a1", ["p1"]), entity("a2", ["p2"])],
  users: [entity("u1", ["d1", "d2"]), entity("u2", ["d3"])],
}
const filters = scope => ({ from: "a", to: "b", ...scope })

test("archive UI exposes only managed states and all means active plus archived", () => {
  assert.deepEqual(usageArchiveSelection(undefined), ["active"])
  assert.deepEqual(usageArchiveFilter(undefined), ["active"])
  assert.deepEqual(usageArchiveSelection([]), [])
  assert.deepEqual(usageArchiveFilter([]), ["active", "archived"])
  assert.deepEqual(usageArchiveFilter(["archived"]), ["archived"])
  assert.deepEqual(usageArchiveFilter(["active", "historical", "unattributed"]), ["active"])
  assert.deepEqual(usageArchiveFilter(["historical", "unattributed"]), ["active"])
  const previous = Object.freeze(["archived", "active", "archived"])
  assert.deepEqual(usageArchiveFilter(previous), ["active", "archived"])
  assert.deepEqual(previous, ["archived", "active", "archived"])
})

test("selection sets are canonical and immutable", () => {
  const value = Object.freeze(["b", "a", "b"])
  assert.deepEqual(usageSelection(value), ["a", "b"])
  assert.deepEqual(usageSelection("a"), ["a"])
  assert.deepEqual(usageSelection(undefined), [])
  assert.deepEqual(value, ["b", "a", "b"])
})
test("facet queries ignore their own selections and retain status/window", () => {
  assert.deepEqual(usageFacetWindow(filters({
    directory_status: ["active"], organization_id: ["o1"], user_id: ["u1"],
  })), { from: "a", to: "b", directory_status: ["active"] })
})
test("multi-parent cascade retains union and does not invent children for empty parents", () => {
  assert.equal(usageFacetOptions(catalog, filters({ organization_id: ["o1", "o2"] })).departments.length, 3)
  assert.deepEqual(usageFacetOptions(catalog, filters({ department_id: ["d2"] })).users.map(row => row.id), ["u1"])
  assert.deepEqual(usageFacetOptions(catalog, filters({ department_id: ["d3"] })).agents, [])
})
test("scope reconciliation removes stale descendants without changing input", () => {
  const scope = Object.freeze({ organization_id: ["o1"], department_id: ["d2"], agent_id: ["a2"], user_id: ["u2"] })
  const next = reconcileUsageScope(scope, catalog)
  assert.deepEqual(next, { organization_id: ["o1"], department_id: undefined, agent_id: undefined, user_id: undefined })
  assert.deepEqual(scope.department_id, ["d2"])
})
