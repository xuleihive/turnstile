import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import { loadTypeScriptModule } from "./load-typescript-module.mjs"
const filterLogic = await loadTypeScriptModule(new URL("../../../frontend/src/lib/model-inventory-filters.ts", import.meta.url))
const {
  MODEL_FILTER_DIMENSIONS, emptyModelFilters, matchesModelFilters, modelFilterRecord,
  modelInventoryFamily, modelInventoryPlatform, publicationFilterRecord, toggleModelFilter,
} = filterLogic

const model = (changes = {}) => ({
  id: "model-one", model_key: "chat-public", display_name: "Chat model", family_key: "generic",
  upstream_model_id: "gpt-example", runtime_id: "connection-one", runtime_name: "Primary connection",
  provider_name: "Microsoft Foundry", capabilities: ["chat"], enabled: true, ...changes,
})
const runtime = { id: "connection-one", brand_key: "microsoft_foundry", runtime_kind: "foundry" }

test("the six filter dimensions match the model inventory menu", () => {
  assert.deepEqual(MODEL_FILTER_DIMENSIONS, ["family", "platform", "capability", "connection", "apim", "status"])
  assert.deepEqual(emptyModelFilters(), { family: [], platform: [], capability: [], connection: [], apim: [], status: [] })
  const first = emptyModelFilters()
  first.family.push("openai")
  assert.deepEqual(emptyModelFilters().family, [])
})

for (const [changes, family] of [
  [{ upstream_model_id: "DeepSeek-R1", family_key: "openai" }, "deepseek"],
  [{ upstream_model_id: "moonshot-chat", family_key: "openai" }, "kimi"],
  [{ upstream_model_id: "claude-example" }, "claude"],
  [{ upstream_model_id: "gpt-example" }, "openai"],
  [{ upstream_model_id: "custom" }, "generic"],
]) test(`model family classification resolves ${family}`, () => {
  assert.equal(modelInventoryFamily(model(changes)), family)
})

test("platform classification prefers connection identity then provider and runtime kind", () => {
  assert.equal(modelInventoryPlatform(runtime, { brand_key: "openai" }), "microsoft_foundry")
  assert.equal(modelInventoryPlatform({ brand_key: "generic" }, { brand_key: "azure_databricks" }), "azure_databricks")
  assert.equal(modelInventoryPlatform({ brand_key: "generic", runtime_kind: "foundry" }), "microsoft_foundry")
  assert.equal(modelInventoryPlatform(undefined, { brand_key: "generic", provider_kind: "openai_compatible" }), "openai_compatible")
  assert.equal(modelInventoryPlatform(), "unknown")
})

test("records preserve connection identity, enabled state and image classification", () => {
  const record = modelFilterRecord(model(), runtime)
  assert.deepEqual(record.family, ["openai"])
  assert.deepEqual(record.platform, ["microsoft_foundry"])
  assert.deepEqual(record.connection, ["connection-one"])
  assert.deepEqual(record.capability, ["chat"])
  assert.deepEqual(record.status, ["enabled"])
  const image = modelFilterRecord(model({ capabilities: ["chat", "image_generation"], enabled: false }), runtime)
  assert.deepEqual(image.capability, ["image_generation"])
  assert.deepEqual(image.status, ["disabled"])
  assert.deepEqual(modelFilterRecord(model({ capabilities: [] })).capability, [])
})

test("decision is an independent model type and never matches the chat filter", () => {
  const record = modelFilterRecord(model({ capabilities: ["decision"] }), runtime)
  assert.deepEqual(record.capability, ["decision"])
  assert.equal(matchesModelFilters(record, { ...emptyModelFilters(), capability: ["decision"] }), true)
  assert.equal(matchesModelFilters(record, { ...emptyModelFilters(), capability: ["chat"] }), false)
  assert.equal(matchesModelFilters(record, { ...emptyModelFilters(), capability: ["image_generation"] }), false)
})

test("multiple choices are OR within a dimension and AND across dimensions", () => {
  const record = modelFilterRecord(model(), runtime)
  const filters = { ...emptyModelFilters(), family: ["claude", "openai"], platform: ["microsoft_foundry"], connection: ["connection-one"], capability: ["chat"], status: ["enabled"] }
  assert.equal(matchesModelFilters(record, filters), true)
  for (const dimension of MODEL_FILTER_DIMENSIONS) {
    assert.equal(matchesModelFilters(record, { ...filters, [dimension]: ["missing"] }), false, dimension)
  }
})

test("search trims whitespace, ignores case and includes upstream and connection names", () => {
  const record = modelFilterRecord(model(), runtime)
  for (const search of ["  GPT-EXAMPLE  ", " PRIMARY CONNECTION ", "foundry", "CHAT-PUBLIC"]) {
    assert.equal(matchesModelFilters(record, emptyModelFilters(), search), true)
  }
  assert.equal(matchesModelFilters(record, { ...emptyModelFilters(), status: ["disabled"] }, "gpt"), false)
  assert.equal(matchesModelFilters(record, emptyModelFilters(), "not present"), false)
})

test("toggling never mutates prior state and clearing a category preserves others", () => {
  const original = { ...emptyModelFilters(), status: ["enabled"] }
  const selected = toggleModelFilter(original, "family", "openai")
  const combined = toggleModelFilter(selected, "family", "claude")
  assert.deepEqual(original.family, [])
  assert.deepEqual(selected.family, ["openai"])
  assert.deepEqual(combined.family, ["openai", "claude"])
  assert.deepEqual(toggleModelFilter(combined, "family", "openai").family, ["claude"])
  assert.deepEqual({ ...combined, family: [] }.status, ["enabled"])
})

test("pending and failed publications participate without inventing unavailable metadata", () => {
  for (const [status, expected] of [["queued", "pending"], ["verifying", "pending"], ["awaiting_authorization", "pending"], ["failed", "failed"]]) {
    const record = publicationFilterRecord({ status, display_name: "GPT pending", model_key: "gpt-pending" })
    assert.deepEqual(record.status, [expected])
    assert.equal(matchesModelFilters(record, { ...emptyModelFilters(), family: ["openai"], status: [expected] }), true)
    assert.equal(matchesModelFilters(record, { ...emptyModelFilters(), connection: ["connection-one"] }), false)
    assert.deepEqual(record.platform, [])
    assert.deepEqual(record.capability, [])
  }
})

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const entry = fileURLToPath(new URL("../../../frontend/src/components/model-management/model-inventory-filter-menu.tsx", import.meta.url))
const bundle = await rolldown({ input: entry, external: identity => identity !== entry, transform: { jsx: { runtime: "automatic" } } })
let componentCode
try { componentCode = (await bundle.generate({ format: "cjs" })).output[0].code }
finally { await bundle.close() }
const primitives = new Proxy({}, { get: (_target, key) => key })
const exports = {}
const imports = {
  "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
  "lucide-react": primitives,
  "../../lib/model-inventory-filters": filterLogic,
  "../brand-logos": primitives,
  "../ui/button": primitives,
  "../ui/dropdown-menu": primitives,
}
runInNewContext(componentCode, { exports, require: name => {
  assert.ok(name in imports, `Unexpected filter component import: ${name}`)
  return imports[name]
} })
const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
  : value && typeof value === "object" ? [value, ...nodes(value.props?.children)] : []
const plain = value => JSON.parse(JSON.stringify(value))
const records = [
  modelFilterRecord(model(), runtime),
  modelFilterRecord(model({ model_key: "claude-unit", display_name: "Claude unit", upstream_model_id: "claude-unit", runtime_id: "connection-two", enabled: false }), { brand_key: "azure_databricks" }),
  modelFilterRecord(model({ model_key: "gpt-image", capabilities: ["image_generation"] }), runtime),
]
function menu(filters = emptyModelFilters(), overrides = {}) {
  const changes = []
  const tree = exports.ModelInventoryFilterMenu({ filters, connections: [{ ...runtime, name: "Primary connection" }], gateways: [], apimInstances: [], records, search: "", compact: false, onChange: next => changes.push(next), ...overrides })
  const categories = nodes(tree).filter(node => node.type === "DropdownMenuSub")
  return { tree, changes, categories,
    options: dimension => nodes(categories.find(node => node.key === dimension)).filter(node => node.type === "DropdownMenuCheckboxItem"),
  }
}

test("menu exposes all six categories in the current inventory order", () => {
  const current = menu()
  assert.deepEqual(current.categories.map(node => node.key), MODEL_FILTER_DIMENSIONS)
  const labels = nodes(current.tree).filter(node => node.props?.className === "model-filter-label").map(node => node.props.children)
  assert.deepEqual(labels, ["模型家族", "接入平台", "模型类型", "连接", "APIM 实例", "状态"])
  assert.equal(nodes(current.tree).find(node => node.type === "DropdownMenuTrigger").props.render.props["aria-label"], "筛选模型")
})

test("decision menu selection retains source label, counts and zero-result selection", () => {
  const decision = modelFilterRecord(model({ model_key: "decision-example", capabilities: ["decision"] }), runtime)
  const current = menu(emptyModelFilters(), { records: [...records, decision] })
  const option = current.options("capability").find(node => node.props.title === "Decision")
  assert.ok(option)
  assert.equal(nodes(option).find(item => item.props?.className === "model-filter-option-count").props.children, 1)
  option.props.onCheckedChange(true)
  assert.deepEqual(plain(current.changes[0].capability), ["decision"])
  const selected = menu({ ...emptyModelFilters(), capability: ["decision"] }, { records: [] })
  assert.equal(selected.options("capability").find(node => node.props.title === "Decision").props.checked, true)
})

test("facet counts ignore their own selection but honor other dimensions and search", () => {
  const filters = { ...emptyModelFilters(), family: ["openai"], status: ["enabled"] }
  const current = menu(filters)
  const counts = Object.fromEntries(current.options("family").map(node => [node.props.title,
    nodes(node).find(item => item.props?.className === "model-filter-option-count").props.children]))
  assert.deepEqual(counts, { Claude: 0, "GPT / OpenAI": 2 })
  const imageOnly = menu(filters, { search: "gpt-image" })
  const openai = imageOnly.options("family").find(node => node.props.title === "GPT / OpenAI")
  assert.equal(nodes(openai).find(item => item.props?.className === "model-filter-option-count").props.children, 1)
})

test("checkboxes stay open, preserve other selections and retain zero-result selected options", () => {
  const filters = { ...emptyModelFilters(), family: ["openai"], status: ["enabled"] }
  const current = menu(filters)
  const claude = current.options("family").find(node => node.props.title === "Claude")
  assert.equal(claude.props.closeOnClick, false)
  claude.props.onCheckedChange(true)
  assert.deepEqual(plain(current.changes[0].family), ["openai", "claude"])
  assert.deepEqual(current.changes[0].status, ["enabled"])
  const empty = menu({ ...emptyModelFilters(), connection: ["removed-connection"] }, { records: [] })
  assert.equal(empty.options("connection")[0].props.checked, true)
  assert.equal(empty.options("capability").length, 3)
})

test("mobile submenus open below and active chips remove one category or clear all", () => {
  const filters = { ...emptyModelFilters(), family: ["openai", "claude"], status: ["enabled"] }
  const current = menu(filters, { compact: true })
  assert.ok(nodes(current.tree).filter(node => node.type === "DropdownMenuSubContent").every(node => node.props.side === "bottom" && node.props.align === "end"))
  const changes = []
  const chips = exports.ModelInventoryFilterChips({ filters, connections: [], gateways: [], apimInstances: [], onChange: value => changes.push(value) })
  const chipNodes = nodes(chips)
  assert.equal(chipNodes.filter(node => node.props?.className === "model-filter-chip").length, 2)
  chipNodes.find(node => node.props?.["aria-label"] === "移除筛选：模型家族").props.onClick()
  assert.deepEqual(plain(changes[0]), { ...emptyModelFilters(), status: ["enabled"] })
  chipNodes.find(node => node.props?.className === "model-filter-reset").props.onClick()
  assert.deepEqual(plain(changes[1]), emptyModelFilters())
  assert.equal(exports.ModelInventoryFilterChips({ filters: emptyModelFilters(), connections: [], gateways: [], apimInstances: [], onChange() {} }), null)
})

for (const [locale, expected, count, selected] of [
  ["en", "Filter models", "2 filters", "2 selected"],
  ["ja", "モデルを絞り込む", "2 件の絞り込み", "2 件"],
  ["ko", "모델 필터", "2개 필터", "2개"],
]) test(`${locale} filter labels and counts use the source translations`, async () => {
  const phrases = Object.values(await import(`../../../frontend/src/locales/${locale}/phrases-core.ts`)).find(value => typeof value === "object")
  const { DYNAMIC_RULES } = await import(`../../../frontend/src/locales/${locale}/dynamic-rules.ts`)
  assert.equal(phrases["筛选模型"], expected)
  for (const key of ["模型家族", "接入平台", "模型类型", "已选筛选条件", "清除筛选", "移除筛选", "未分类", "未识别"]) assert.ok(phrases[key], key)
  const translate = value => DYNAMIC_RULES.reduce((text, [pattern, replace]) => text.replace(pattern, replace), value)
  assert.equal(translate("2 个筛选"), count)
  assert.equal(translate("2 项"), selected)
})
