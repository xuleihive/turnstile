import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import {
  DATA_REFRESH_OPTIONS, DATA_REFRESH_STORAGE_KEY, dataRefreshLabel,
  normalizeDataRefreshMinutes, persistDataRefreshMinutes, readDataRefreshMinutes,
  refreshActiveDataQueries, startDataRefresh,
} from "../../../frontend/src/lib/data-refresh.ts"

function clock() {
  let now = 0, visible = true, id = 0
  const timers = new Map(), listeners = new Set()
  return {
    environment: {
      now: () => now, visible: () => visible,
      schedule: (callback, delay) => {
        const key = ++id; timers.set(key, { at: now + delay, callback })
        return () => timers.delete(key)
      },
      subscribe: callback => { listeners.add(callback); return () => listeners.delete(callback) },
    },
    advance(ms) {
      const end = now + ms
      while (true) {
        const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0]
        if (!next || next[1].at > end) break
        now = next[1].at; timers.delete(next[0]); next[1].callback()
      }
      now = end
    },
    visibility(next) { visible = next; for (const listener of listeners) listener() },
    focus() { for (const listener of listeners) listener() },
    get timers() { return timers.size }, get listeners() { return listeners.size },
  }
}

for (const { minutes } of DATA_REFRESH_OPTIONS) {
  if (minutes === 0) continue
  test(`${minutes}-minute selection uses that exact cadence, not the cache staleTime`, () => {
    const c = clock(); let calls = 0
    const stop = startDataRefresh(minutes * 60_000, () => calls++, c.environment)
    c.advance(minutes * 60_000 - 1); assert.equal(calls, 0)
    c.advance(1); assert.equal(calls, 1)
    c.advance(minutes * 60_000); assert.equal(calls, 2)
    stop(); assert.equal(c.timers, 0); assert.equal(c.listeners, 0)
  })
}

test("disabled periodic refresh creates no timer or focus listener, including after a day hidden", () => {
  const c = clock(); let calls = 0
  const stop = startDataRefresh(0, () => calls++, c.environment)
  assert.equal(c.timers, 0); assert.equal(c.listeners, 0)
  c.advance(3600_000); c.visibility(false); c.advance(86400_000)
  c.visibility(true); c.focus(); c.advance(0)
  assert.equal(calls, 0)
  stop()
})

test("hidden tabs pause and overdue return refreshes once, not once per missed interval", () => {
  const c = clock(); let calls = 0
  const stop = startDataRefresh(60_000, () => calls++, c.environment)
  c.advance(30_000); c.visibility(false); c.advance(24 * 3600_000)
  assert.equal(calls, 0); assert.equal(c.timers, 0)
  c.visibility(true); c.focus(); c.advance(0); assert.equal(calls, 1)
  c.advance(59_999); assert.equal(calls, 1)
  c.advance(1); assert.equal(calls, 2)
  stop()
})

test("early visibility return does not bypass a long user-selected interval", () => {
  const c = clock(); let calls = 0
  const stop = startDataRefresh(3600_000, () => calls++, c.environment)
  c.advance(60_000); c.visibility(false); c.advance(60_000)
  c.visibility(true); c.focus(); c.advance(0); assert.equal(calls, 0)
  c.advance(3480_000); assert.equal(calls, 1)
  stop()
})

test("changing frequency cleans up the old timer and logout stops all future refreshes", () => {
  const c = clock(); let calls = 0
  const first = startDataRefresh(60_000, () => calls++, c.environment)
  c.advance(30_000); first()
  const stop = startDataRefresh(1800_000, () => calls++, c.environment)
  assert.equal(c.timers, 1); assert.equal(c.listeners, 1)
  c.advance(60_000); assert.equal(calls, 0)
  c.advance(1740_000); assert.equal(calls, 1)
  stop(); c.advance(86400_000); c.focus(); assert.equal(calls, 1)
})

test("persisted preferences whitelist valid options and survive unavailable browser storage", t => {
  const original = Object.getOwnPropertyDescriptor(global, "localStorage")
  t.after(() => original ? Object.defineProperty(global, "localStorage", original) : delete global.localStorage)
  const values = new Map()
  Object.defineProperty(global, "localStorage", { configurable: true, value: {
    getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
  } })
  assert.equal(readDataRefreshMinutes(), 5)
  for (const { minutes, label } of DATA_REFRESH_OPTIONS) {
    assert.equal(persistDataRefreshMinutes(minutes), true)
    assert.equal(readDataRefreshMinutes(), minutes)
    assert.equal(dataRefreshLabel(minutes), label)
  }
  assert.equal(normalizeDataRefreshMinutes("0"), 0)
  for (const value of ["NaN", "Infinity", "10", "-1", "", " ", "720", "1440", null, {}, true]) {
    assert.equal(normalizeDataRefreshMinutes(value), 5)
  }
  values.set(DATA_REFRESH_STORAGE_KEY, "broken"); assert.equal(readDataRefreshMinutes(), 5)
  Object.defineProperty(global, "localStorage", { configurable: true, get() { throw new Error("blocked") } })
  assert.equal(readDataRefreshMinutes(), 5); assert.equal(persistDataRefreshMinutes(30), false)
})

test("real query observers refresh budgets, people and other pages together without touching disabled, inactive or operational queries", async t => {
  const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
  const { QueryClient, QueryObserver } = require("@tanstack/react-query")
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const subscriptions = []; t.after(() => { subscriptions.forEach(stop => stop()); client.clear() })
  const counts = new Map()
  function observe(key, { enabled = true, data = {}, meta } = {}) {
    const observer = new QueryObserver(client, {
      queryKey: key, queryFn: async () => { counts.set(key.join("/"), (counts.get(key.join("/")) ?? 0) + 1); return data },
      enabled, meta,
    })
    subscriptions.push(observer.subscribe(() => {}))
  }
  const dataKeys = [
    ["finops", "budgets", "2026-10"], ["finops", "budgets", "people", "2026-10"],
    ["finops", "request-detail", "pending-usage"], ["finops", "executive-overview"],
    ["finops", "distribution"], ["finops", "trends"], ["finops", "anomalies"],
    ["user-settings", "me", "usage"], ["copilot", "dashboard"],
    ["application-access", "application", "app"],
    ["directory", "people"], ["assistant", "pinned-charts", "me"],
    ["control-plane", "gateway-releases"],
  ]
  for (const key of dataKeys) observe(key)
  const excluded = [
    ["directory", "capabilities"], ["directory", "sync-jobs"],
    ["control-plane", "gateway-release-operation"], ["assistant", "settings"],
    ["control-plane", "gateway-release-diff"], ["control-plane", "gateway-release-integrity"],
    ["control-plane", "gateway-release-rollback-preview"],
    ["assistant", "apim", "conversation"], ["auth", "me"],
    ["reference", "model-registry"], ["reference", "enterprise-query-entities"],
    ["directory", "budget-navigation"], ["directory", "budget-team-members"],
    ["directory", "status-preview"], ["model-platform", "model-backend-pool"],
  ]
  for (const key of excluded) observe(key)
  observe(["finops", "disabled"], { enabled: false })
  observe(["finops", "opt-out"], { meta: { automaticRefresh: false } })
  observe(["control-plane", "gateway-release", "running"], { data: { role: "in_progress" } })
  observe(["control-plane", "gateway-publications"], { data: { items: [{ status: "validating" }] } })
  client.setQueryData(["finops", "inactive"], {})
  await new Promise(resolve => setImmediate(resolve))
  await refreshActiveDataQueries(client)
  for (const key of dataKeys) assert.equal(counts.get(key.join("/")), 2, key.join("/"))
  for (const key of excluded) assert.equal(counts.get(key.join("/")), 1, key.join("/"))
  assert.equal(counts.get("finops/disabled"), undefined)
  assert.equal(counts.get("finops/inactive"), undefined)
  assert.equal(counts.get("finops/opt-out"), 1)
  assert.equal(counts.get("control-plane/gateway-release/running"), 1)
  assert.equal(counts.get("control-plane/gateway-publications"), 1)
})

test("primary inventories can opt in without polling metadata on other pages", async t => {
  const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
  const { QueryClient, QueryObserver } = require("@tanstack/react-query")
  const client = new QueryClient(); let calls = 0
  const observer = new QueryObserver(client, {
    queryKey: ["reference", "model-registry"], queryFn: async () => ++calls,
    meta: { automaticRefresh: true },
  })
  const stop = observer.subscribe(() => {})
  t.after(() => { stop(); client.clear() })
  await new Promise(resolve => setImmediate(resolve)); await refreshActiveDataQueries(client)
  assert.equal(calls, 2)
})

test("mounting a cached page reads once even with periodic refresh disabled, and manual refresh still works", async t => {
  const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
  const { QueryClient, QueryObserver } = require("@tanstack/react-query")
  const client = new QueryClient({ defaultOptions: { queries: {
    staleTime: Infinity, refetchOnMount: "always", refetchOnWindowFocus: false,
  } } })
  const key = ["finops", "budgets"]; let calls = 0
  client.setQueryData(key, "old")
  const observer = new QueryObserver(client, { queryKey: key, queryFn: async () => ++calls })
  let stop = observer.subscribe(() => {})
  t.after(() => { stop(); client.clear() })
  await new Promise(resolve => setImmediate(resolve)); assert.equal(calls, 1)
  stop()
  const returned = new QueryObserver(client, { queryKey: key, queryFn: async () => ++calls })
  stop = returned.subscribe(() => {})
  await new Promise(resolve => setImmediate(resolve)); assert.equal(calls, 2)
  await returned.refetch(); assert.equal(calls, 3)
})

test("automatic refresh does not cancel, restart or overlap an in-flight request", async t => {
  const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
  const { QueryClient, QueryObserver } = require("@tanstack/react-query")
  const client = new QueryClient(); let calls = 0, complete
  const observer = new QueryObserver(client, {
    queryKey: ["finops", "budgets"], queryFn: () => { calls++; return new Promise(resolve => { complete = resolve }) },
  })
  const stop = observer.subscribe(() => {})
  t.after(() => { stop(); client.clear() })
  await refreshActiveDataQueries(client); await refreshActiveDataQueries(client)
  assert.equal(calls, 1)
  complete({ used_tokens: 42 }); await new Promise(resolve => setImmediate(resolve))
  assert.equal(observer.getCurrentResult().data.used_tokens, 42)
})

test("pending writes do not start additional automatic reads", async t => {
  const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
  const { QueryClient, QueryObserver } = require("@tanstack/react-query")
  const client = new QueryClient(); let calls = 0, finishWrite
  const observer = new QueryObserver(client, { queryKey: ["finops", "budgets"], queryFn: async () => ++calls })
  const stop = observer.subscribe(() => {})
  t.after(() => { stop(); client.clear() })
  await new Promise(resolve => setImmediate(resolve))
  const mutation = client.getMutationCache().build(client, { mutationFn: () => new Promise(resolve => { finishWrite = resolve }) })
  const write = mutation.execute({})
  await new Promise(resolve => setImmediate(resolve))
  await refreshActiveDataQueries(client); assert.equal(calls, 1)
  finishWrite({}); await write
  await refreshActiveDataQueries(client); assert.equal(calls, 2)
})
