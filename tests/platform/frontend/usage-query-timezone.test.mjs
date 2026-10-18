import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { stripTypeScriptTypes } from "node:module"
import test from "node:test"
import { cachePolicies } from "../../../frontend/src/api/cache-policies.ts"
import * as filters from "../../../frontend/src/lib/usage-filter-scope.ts"

const source = readFileSync(new URL("../../../frontend/src/data-sources/apim/queries.ts", import.meta.url), "utf8")
const compiled = stripTypeScriptTypes(source).replace(/^import .*\n/gm, "").replace(/^export /gm, "")
assert.doesNotMatch(compiled, /^\s*import\b/m)

function loadQueries() {
  let now = Date.parse("2026-10-11T17:45:00Z")
  let timezone = "Asia/Shanghai"
  const calls = []
  const usageWindow = days => ({
    from: new Date(now - days * 86400_000).toISOString(),
    to: new Date(now).toISOString(),
  })
  const dataSource = {
    trendPoints: async (...args) => { calls.push(args); return { points: [] } },
  }
  // Execute the actual builder with a read-only API spy, without React or network access.
  const exported = new Function(
    "queryOptions", "cachePolicies", "resolveTimezone", "usageFacetWindow", "usageSelection",
    "dataSource", "usageWindow", `${compiled}\nreturn { finopsQueries, prefetchPage };`,
  )(
    options => options, cachePolicies, () => timezone, filters.usageFacetWindow,
    filters.usageSelection, dataSource, usageWindow,
  )
  return {
    ...exported,
    calls,
    setNow: value => { now = Date.parse(value) },
    setTimezone: value => { timezone = value },
  }
}

const scope = {
  from: "2026-09-11T17:45:00Z", to: "2026-10-11T17:45:00Z",
  directory_status: ["active", "unattributed"], model_id: ["model-a"],
}

test("trend cache identity includes timezone for hour, day and week", async () => {
  const api = loadQueries()
  for (const interval of ["hour", "day", "week"]) {
    const local = api.finopsQueries.trends(scope, "department", interval, "Asia/Shanghai")
    const utc = api.finopsQueries.trends(scope, "department", interval, "UTC")
    assert.notDeepEqual(local.queryKey, utc.queryKey)
    assert.ok(local.queryKey.includes("Asia/Shanghai"))
    assert.ok(utc.queryKey.includes("UTC"))
    await local.queryFn()
    assert.deepEqual(api.calls.at(-1).slice(1), ["department", interval, "Asia/Shanghai"])
  }
})

test("polling recalculates the window without changing filters or cached identity", async () => {
  const api = loadQueries()
  const before = JSON.stringify(scope)
  const query = api.finopsQueries.trends(scope, "none", "day", "Asia/Shanghai")
  await query.queryFn()
  const first = api.calls.at(-1)[0]
  api.setNow("2026-10-11T18:00:00Z")
  await query.queryFn()
  const next = api.calls.at(-1)[0]
  assert.notEqual(first.to, next.to)
  assert.equal(Date.parse(next.to) - Date.parse(next.from), 30 * 86400_000)
  assert.deepEqual(next.directory_status, ["active", "unattributed"])
  assert.deepEqual(next.model_id, ["model-a"])
  assert.deepEqual(query.queryKey, api.finopsQueries.trends(scope, "none", "day", "Asia/Shanghai").queryKey)
  assert.equal(JSON.stringify(scope), before)
})

test("trend prefetch uses the same selected timezone as the rendered page", () => {
  const api = loadQueries()
  const prefetched = []
  const client = { prefetchQuery: query => { prefetched.push(query) } }
  for (const timezone of ["UTC", "Asia/Shanghai"]) {
    api.setTimezone(timezone)
    api.prefetchPage(client, "finops-trends", scope)
    const trend = prefetched.findLast(query => query.queryKey[1] === "trends")
    assert.deepEqual(trend.queryKey, api.finopsQueries.trends(scope, "department", "hour", timezone).queryKey)
  }
})
