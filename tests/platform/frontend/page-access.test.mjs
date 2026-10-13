import assert from "node:assert/strict"
import test from "node:test"

const pageAccess = await import(
  "../../../frontend/src/data-sources/apim/page-access.js"
).catch(() => ({}))

test("members do not see enterprise budgets and direct routes return to overview", () => {
  const pages = [
    { id: "finops-overview" },
    { id: "budgets" },
    { id: "applications" },
  ]
  const memberPermissions = ["applications.read.own", "usage.read.own"]

  assert.equal(typeof pageAccess.visibleApimPages, "function")
  assert.equal(typeof pageAccess.resolveApimPage, "function")
  assert.deepEqual(
    pageAccess.visibleApimPages(pages, memberPermissions).map(({ id }) => id),
    ["finops-overview", "applications"],
  )
  assert.equal(
    pageAccess.resolveApimPage("budgets", memberPermissions),
    "finops-overview",
  )
})

test("owners retain the enterprise budget page", () => {
  const pages = [{ id: "finops-overview" }, { id: "budgets" }]
  const ownerPermissions = ["usage.read.all"]

  assert.deepEqual(
    pageAccess.visibleApimPages(pages, ownerPermissions).map(({ id }) => id),
    ["finops-overview", "budgets"],
  )
  assert.equal(pageAccess.resolveApimPage("budgets", ownerPermissions), "budgets")
})
