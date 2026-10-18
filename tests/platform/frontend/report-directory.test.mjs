import assert from "node:assert/strict"
import { test } from "node:test"
import { reportCenterUrl, reportDirectory, selectedReport } from "../../../frontend/src/lib/report-directory.ts"

const reports = [
  { id: "b", title: "成本报表", description: "monthly", position: 2 },
  { id: "c", title: "Token 趋势", description: "", position: 1 },
  { id: "a", title: "Team Ranking", description: "usage", position: 1 },
]

test("directory retains stored order with stable ties and never mutates the source", () => {
  assert.deepEqual(reportDirectory(reports).map(report => report.id), ["a", "c", "b"])
  assert.deepEqual(reports.map(report => report.id), ["b", "c", "a"])
})
test("search matches user titles and descriptions, case-insensitively", () => {
  assert.deepEqual(reportDirectory(reports, " TOKEN ").map(report => report.id), ["c"])
  assert.deepEqual(reportDirectory(reports, "MONTHLY").map(report => report.id), ["b"])
  assert.deepEqual(reportDirectory(reports, "missing"), [])
})
test("selection defaults to first report but never substitutes an inaccessible old link", () => {
  const directory = reportDirectory(reports)
  assert.equal(selectedReport(directory, null)?.id, "a")
  assert.equal(selectedReport(directory, "c")?.id, "c")
  assert.equal(selectedReport(directory, "missing"), undefined)
  assert.equal(selectedReport([], null), undefined)
  assert.equal(selectedReport(directory.filter(report => report.id !== "a"), null)?.id, "c")
})
test("report links preserve source and unrelated scopes and empty selection clears chart", () => {
  const href = "http://localhost/?source=apim&page=pinned-report&chart=old&directory_org=org"
  const next = reportCenterUrl(href, "new/id")
  assert.equal(next.searchParams.get("chart"), "new/id")
  assert.equal(next.searchParams.get("directory_org"), "org")
  assert.equal(next.searchParams.get("source"), "apim")
  assert.equal(next.searchParams.get("page"), "pinned-report")
  assert.equal(reportCenterUrl(next.href, null).searchParams.has("chart"), false)
})
