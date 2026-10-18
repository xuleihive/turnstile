import assert from "node:assert/strict"
import test from "node:test"
import { formatUsageBucket, formatUsageTimestamp, timezoneBucketKey } from "../../../frontend/src/lib/timezone.ts"
import { cachePolicies } from "../../../frontend/src/api/cache-policies.ts"

test("hour labels follow the selected zone across midnight", () => {
  const timestamp = "2026-10-11T17:45:39.401Z"
  assert.equal(formatUsageBucket(timestamp, "hour", "Asia/Shanghai", "zh-CN"), "10/12 01时")
  assert.equal(formatUsageBucket(timestamp, "hour", "UTC", "zh-CN"), "10/11 17时")
})

test("daily and weekly labels preserve the server's local-midnight buckets", () => {
  const localMonday = "2026-10-11T16:00:00Z"
  for (const interval of ["day", "week"]) {
    assert.equal(formatUsageBucket(localMonday, interval, "Asia/Shanghai", "zh-CN"), "10/12")
  }
  assert.equal(formatUsageBucket("2026-10-05T00:00:00Z", "week", "UTC", "zh-CN"), "10/05")
})

test("English hourly midnight is 00:00, not 24:00", () => {
  const label = formatUsageBucket("2026-10-11T16:00:00Z", "hour", "Asia/Shanghai", "en-US")
  assert.match(label, /10\/12.*00:00/)
})

test("signal table and dialog timestamps obey UTC even on a Shanghai browser", () => {
  const timestamp = "2026-10-11T17:45:39Z"
  assert.match(formatUsageTimestamp(timestamp, "Asia/Shanghai", "zh-CN"), /2026\/10\/12.*01:45:39/)
  assert.match(formatUsageTimestamp(timestamp, "UTC", "zh-CN"), /2026\/10\/11.*17:45:39/)
})

test("IANA zones handle daylight saving offsets without altering raw timestamps", () => {
  const winter = "2026-01-12T04:30:00Z"
  const summer = "2026-07-12T04:30:00Z"
  assert.equal(timezoneBucketKey(winter, "hour", "America/New_York"), "2026-01-11T23")
  assert.equal(timezoneBucketKey(summer, "hour", "America/New_York"), "2026-07-12T00")
  assert.match(formatUsageBucket(winter, "hour", "America/New_York", "en-US"), /01\/11.*23:30/)
  assert.match(formatUsageBucket(summer, "hour", "America/New_York", "en-US"), /07\/12.*00:30/)
  assert.equal(winter, "2026-01-12T04:30:00Z")
  assert.equal(summer, "2026-07-12T04:30:00Z")
})

test("query cache freshness does not define an independent polling cadence", () => {
  assert.equal(cachePolicies.liveUsage, undefined)
  assert.equal(cachePolicies.aggregate.staleTime, 5 * 60_000)
})
