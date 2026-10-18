import assert from "node:assert/strict"
import test from "node:test"

import {
  directoryOrganizationSelection, directoryUnitSelection, managedDirectoryStatus,
  sortDirectoryNodes, visibleDirectoryOrganizations, visibleDirectoryUnits,
} from "../../../frontend/src/lib/directory-visibility.ts"

const organizations = [
  { id: "archived-org", status: "archived" },
  { id: "legacy-org", status: "inactive" },
  { id: "active-org", status: "active" },
]
const units = [
  { id: "department", kind: "department", status: "active", parent_unit_id: null },
  { id: "archived-department", kind: "department", status: "archived", parent_unit_id: null },
  { id: "legacy-department", kind: "department", status: "inactive", parent_unit_id: null },
  { id: "team", kind: "team", status: "active", parent_unit_id: "department" },
  { id: "archived-team", kind: "team", status: "archived", parent_unit_id: "department" },
  { id: "child-of-archived", kind: "team", status: "active", parent_unit_id: "archived-department" },
]

test("legacy inactive status belongs to the archived display group", () => {
  assert.equal(managedDirectoryStatus("active"), "active")
  assert.equal(managedDirectoryStatus("inactive"), "archived")
  assert.equal(managedDirectoryStatus("archived"), "archived")
})

test("default filtering hides archived organizations, units and their subtrees", () => {
  assert.deepEqual(visibleDirectoryOrganizations(organizations, true).map(row => row.id), ["active-org"])
  assert.deepEqual(visibleDirectoryUnits(units, true).map(row => row.id), ["department", "team"])
  assert.equal(visibleDirectoryOrganizations(organizations, false), organizations)
  assert.equal(visibleDirectoryUnits(units, false), units)
})

test("a hidden organization falls back to the first active organization without losing unknown scope errors", () => {
  assert.equal(directoryOrganizationSelection(organizations, "", true), "active-org")
  assert.equal(directoryOrganizationSelection(organizations, "archived-org", true), "active-org")
  assert.equal(directoryOrganizationSelection(organizations, "legacy-org", true), "active-org")
  assert.equal(directoryOrganizationSelection(organizations, "archived-org", false), "archived-org")
  assert.equal(directoryOrganizationSelection(organizations, "unknown", true), "unknown")
  assert.equal(directoryOrganizationSelection(organizations.slice(0, 2), "archived-org", true), "")
})

test("a hidden team falls back to its active department, other hidden units to the organization", () => {
  assert.equal(directoryUnitSelection(units, "archived-team", true), "department")
  assert.equal(directoryUnitSelection(units, "archived-department", true), "")
  assert.equal(directoryUnitSelection(units, "child-of-archived", true), "")
  assert.equal(directoryUnitSelection(units, "legacy-department", true), "")
  assert.equal(directoryUnitSelection(units, "team", true), "team")
  assert.equal(directoryUnitSelection(units, "unknown", true), "unknown")
  assert.equal(directoryUnitSelection(units, "archived-team", false), "archived-team")
})

test("each node kind sorts enabled before archived, then by locale-aware natural names", () => {
  for (const kind of ["organization", "department", "team"]) {
    const rows = Object.freeze([
      { id: "archived", name: "A", status: "archived", kind },
      { id: "ten", name: "Team 10", status: "active", kind },
      { id: "legacy", name: "B", status: "inactive", kind },
      { id: "two", name: "Team 2", status: "active", kind },
      { id: "one", name: "alpha", status: "active", kind },
    ].map(Object.freeze))
    const before = [...rows]
    assert.deepEqual(sortDirectoryNodes(rows, "en").map(row => row.id),
      ["one", "two", "ten", "archived", "legacy"])
    assert.deepEqual(rows, before)
  }
})

test("name ties use IDs, and Chinese names follow the selected locale", () => {
  const ties = [
    { id: "z", name: "TEAM", status: "active" },
    { id: "a", name: "team", status: "active" },
  ]
  assert.deepEqual(sortDirectoryNodes(ties, "en").map(row => row.id), ["a", "z"])
  const chinese = [
    { id: "zhang", name: "张", status: "active" },
    { id: "li", name: "李", status: "active" },
    { id: "wang", name: "王", status: "active" },
  ]
  for (const locale of ["zh-CN", "zh-TW", "en", "ja", "ko"]) {
    const collator = new Intl.Collator(locale, {
      numeric: true, sensitivity: "base",
    })
    const expected = [...chinese].sort((a, b) => collator.compare(a.name, b.name))
    assert.deepEqual(sortDirectoryNodes(chinese, locale), expected)
  }
})
