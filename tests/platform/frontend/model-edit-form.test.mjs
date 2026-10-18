// Isolated draft/payload unit tests. No browser, API, registry writes or database.
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import {
  applyCatalogOption,
  createModelEditDraft,
  discountedRate,
  modelKeyFromReference,
  modelEditHasChanges,
  modelEditPayload,
  modelEditRoleOptions,
  resolveDiscount,
  toggleModelSelection,
  validateModelEdit,
} from "../../../frontend/src/components/model-management/model-edit-form.ts"
import * as modelEditForm from "../../../frontend/src/components/model-management/model-edit-form.ts"
import * as modelVendor from "../../../frontend/src/components/model-management/openai-compatible.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
async function compile(relative) {
  const entry = fileURLToPath(new URL(relative, import.meta.url))
  const bundle = await rolldown({ input: entry, external: path => path !== entry,
    transform: { jsx: { runtime: "automatic" } }, treeshake: false })
  try { return (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
}
const component = await compile("../../../frontend/src/components/model-management/model-edit-dialog.tsx")
const priceComponent = await compile("../../../frontend/src/components/model-management/model-price-source.tsx")

function editorHarness(model, providerKind = "microsoft_foundry") {
  const slots = []
  let cursor = 0
  const imports = {
    react: { useId: () => "unit-editor", useState(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
      return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value }]
    } },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "./model-edit-form": modelEditForm,
    "./model-vendor-select": { ...modelVendor, ModelVendorLogo: "ModelVendorLogo" },
    "./model-availability": { useModelAvailabilityCommand: () => ({ submitting: false }),
      ModelAvailabilityStatus: "ModelAvailabilityStatus", ModelAvailabilitySwitch: "ModelAvailabilitySwitch" },
    "../brand-logos": { GatewayBrandLogo: "GatewayBrandLogo", ProviderBrandLogo: "ProviderBrandLogo", gatewayBrandFromIdentity: value => value, providerBrandFromMetadata: value => value },
  }
  const exports = {}
  runInNewContext(component, { exports, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  const saved = []
  const props = {
    model, busy: false, error: null, onClose() {}, onSave: value => saved.push(value),
    registry: {
      gateways: [{ id: "gateway", name: "Unit gateway", implementation: "apim" }],
      providers: [{ id: model.provider_id, provider_kind: providerKind }],
      runtimes: [{ id: model.runtime_id, gateway_profile_id: "gateway", brand_key: providerKind, name: "Unit connection", config: { model_vendor: "deepseek" } }],
    },
  }
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
    : value && typeof value === "object" ? [value, ...nodes(typeof value.type === "function" ? value.type(value.props) : value.props?.children)] : []
  const render = () => { cursor = 0; return exports.ModelEditDialog(props) }
  return { props, saved, nodes, find: predicate => nodes(render()).find(predicate), all: () => nodes(render()) }
}

const savedModel = overrides => model(overrides)

const model = (overrides = {}) => ({
  id: "model-a",
  provider_id: "provider-a",
  runtime_id: "connection-a",
  model_key: "published-alias",
  upstream_model_id: "existing-deployment",
  family_key: "generic",
  assignment_required: true,
  display_name: "Existing model",
  capabilities: ["chat", "streaming"],
  context_window: 1048576,
  input_cost_per_million: 3,
  output_cost_per_million: 15,
  cached_cost_per_million: 0.3,
  cache_write_cost_per_million: 3,
  price_source: "manual",
  price_reference: null,
  price_discount_percent: null,
  allowed_roles: ["owner", "admin", "member"],
  enabled: true,
  is_default: false,
  ...overrides,
})

test("unchanged metadata round-trips while routing identity comes from the saved model", () => {
  const source = model()
  const result = modelEditPayload(source, createModelEditDraft(source))
  for (const key of Object.keys(result)) assert.deepEqual(result[key], source[key], key)
  assert.equal("id" in result, false)
  assert.equal("gateway_profile_id" in result, false)
})

test("editing display and price fields cannot override routing identity or capabilities", () => {
  const source = model()
  const draft = { ...createModelEditDraft(source), displayName: " Renamed ",
    capabilities: ["chat", "tools"], inputPrice: "4.123456789",
    provider_id: "different", runtime_id: "different", model_key: "different",
    upstream_model_id: "different", assignment_required: false, family_key: "openai" }
  const result = modelEditPayload(source, draft)
  for (const key of ["provider_id", "runtime_id", "model_key", "upstream_model_id",
    "family_key", "assignment_required"]) assert.equal(result[key], source[key])
  assert.equal(result.display_name, "Renamed")
  assert.equal(result.input_cost_per_million, 4.123456789)
  assert.deepEqual(result.capabilities, source.capabilities)
})

test("null upstream identity is preserved without substituting the public alias", () => {
  const source = model({ upstream_model_id: null })
  assert.equal(modelEditPayload(source, createModelEditDraft(source)).upstream_model_id, null)
})

test("blank nullable fields remain null and zero prices remain explicitly free", () => {
  const source = model({ context_window: null, input_cost_per_million: null,
    output_cost_per_million: 0, cached_cost_per_million: null, cache_write_cost_per_million: 0 })
  const draft = createModelEditDraft(source)
  assert.equal(draft.contextWindow, "")
  assert.equal(draft.inputPrice, "")
  assert.equal(draft.outputPrice, "0")
  const result = modelEditPayload(source, draft)
  assert.equal(result.context_window, null)
  assert.equal(result.input_cost_per_million, null)
  assert.equal(result.output_cost_per_million, 0)
  assert.equal(result.cached_cost_per_million, null)
  assert.equal(result.cache_write_cost_per_million, 0)
})

test("clearing cache rates requests backend fallback, not a client-side fabricated rate", () => {
  const source = model()
  const result = modelEditPayload(source, { ...createModelEditDraft(source),
    cacheReadPrice: "", cacheWritePrice: " " })
  assert.equal(result.cached_cost_per_million, null)
  assert.equal(result.cache_write_cost_per_million, null)
  assert.equal(result.input_cost_per_million, 3)
})

test("capabilities are excluded from the draft and preserved from the registered model", () => {
  for (const capabilities of [["chat", "streaming"], ["tools", "vision", "reasoning"], []]) {
    const source = model({ capabilities })
    const draft = createModelEditDraft(source)
    assert.equal("capabilities" in draft, false)
    const payload = modelEditPayload(source, { ...draft, capabilities: ["invented-capability"] })
    assert.deepEqual(payload.capabilities, capabilities)
    assert.notEqual(payload.capabilities, source.capabilities)
  }
})

test("legacy and custom roles round-trip without silently adding or dropping permissions", () => {
  const source = model({ allowed_roles: ["owner", "admin", "service", "custom-role"] })
  const draft = createModelEditDraft(source)
  assert.deepEqual(modelEditRoleOptions(source), ["owner", "member", "admin", "service", "custom-role"])
  assert.deepEqual(modelEditPayload(source, draft).allowed_roles, source.allowed_roles)
  assert.deepEqual(modelEditRoleOptions(model({ allowed_roles: ["member"] })), ["owner", "member"])
  assert.deepEqual(modelEditPayload(source, { ...draft, allowedRoles: [] }).allowed_roles, [])
  const selected = toggleModelSelection(draft.allowedRoles, "member", true)
  assert.deepEqual(toggleModelSelection(selected, "member", true), selected)
  assert.deepEqual(toggleModelSelection(selected, "member", false), source.allowed_roles)
})

test("restoring the same checkbox choices does not leave a spurious pending change", () => {
  const initial = createModelEditDraft(model())
  assert.equal(modelEditHasChanges(initial, { ...initial,
    allowedRoles: [...initial.allowedRoles].reverse(),
  }), false)
  assert.equal(modelEditHasChanges(initial, { ...initial, allowedRoles: ["owner"] }), true)
})

test("invalid display names are rejected before a save payload can be constructed", () => {
  const source = model()
  for (const displayName of ["", "   ", "x".repeat(256)]) {
    const draft = { ...createModelEditDraft(source), displayName }
    assert.equal(validateModelEdit(draft), "display_name")
    assert.throws(() => modelEditPayload(source, draft), /display_name/)
  }
})

test("context limits require safe positive integers and rates finite non-negative values", () => {
  const draft = createModelEditDraft(model())
  for (const contextWindow of ["0", "-1", "1.5", "Infinity", "not-a-number", "9007199254740992"]) {
    assert.equal(validateModelEdit({ ...draft, contextWindow }), "context_window")
  }
  for (const value of ["-1", "Infinity", "NaN", "1e999"]) {
    for (const key of ["inputPrice", "outputPrice", "cacheReadPrice", "cacheWritePrice"]) {
      assert.equal(validateModelEdit({ ...draft, [key]: value }), "prices")
    }
  }
  assert.equal(validateModelEdit({ ...draft, contextWindow: "1", inputPrice: "0" }), null)
})

test("availability never enters a metadata draft or payload and defaults survive disablement", () => {
  const source = model({ enabled: false, is_default: true })
  const draft = createModelEditDraft(source)
  assert.equal("enabled" in draft, false)
  assert.equal(validateModelEdit(draft), null)
  const payload = modelEditPayload(source, { ...draft, enabled: true })
  assert.equal("enabled" in payload, false)
  assert.equal(payload.is_default, true)
  assert.equal(modelEditHasChanges(draft, createModelEditDraft({ ...source, enabled: true })), false)
})

test("model editor restores identity, help and units without changing saved routing", () => {
  const model = savedModel({ provider_name: "Unit provider" })
  const view = editorHarness(model)
  assert.equal(view.find(node => node.type === "DialogContent").props.finalFocus, false)
  assert.equal(view.find(node => node.type === "DialogDescription").props.children, "更新模型名称、价格和访问配置。连接与模型标识保持不变。")
  const identity = view.find(node => node.props?.className === "model-editor-identity")
  assert.deepEqual(view.nodes(identity).filter(node => node.type === "dt").map(node => node.props.children), ["目标网关", "接入类型", "连接", "模型别名", "Deployment Name"])
  assert.ok(view.find(node => node.type === "FieldHelp" && node.props.children.startsWith("连接和模型标识决定")))
  assert.ok(view.find(node => node.type === "FieldHelp" && node.props.children.startsWith("设为整个模型目录")))
  assert.ok(view.find(node => node.props?.className === "model-editor-unit" && node.props.children === "Tokens"))
  const advanced = view.find(node => node.props?.className === "model-editor-advanced-body")
  assert.ok(view.nodes(advanced).some(node => node.type === "p" && node.props.children.startsWith("角色限制不替代")))
  assert.ok(view.nodes(advanced).some(node => node.type === "p" && node.props.children.startsWith("当前应用登录角色")))
  view.find(node => node.type === "Input" && node.props.maxLength === 255).props.onChange({ target: { value: "Renamed" } })
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(view.saved.length, 1)
  assert.equal(view.saved[0].display_name, "Renamed")
  for (const key of ["provider_id", "runtime_id", "model_key", "upstream_model_id", "capabilities", "allowed_roles"]) assert.deepEqual(view.saved[0][key], model[key])
})

test("model editor explains image default restrictions and preserves validation", () => {
  const view = editorHarness(savedModel({ capabilities: ["image_generation"], context_window: null, cached_cost_per_million: null }))
  assert.ok(view.find(node => node.type === "FieldHelp" && node.props.children === "图片模型不能设为聊天默认"))
  const defaultLabel = view.find(node => node.type === "label" && view.nodes(node).some(child => child.type === "span" && child.props.children === "设为默认"))
  assert.equal(view.nodes(defaultLabel).find(node => node.type === "Checkbox").props.disabled, true)
  assert.equal(view.find(node => node.props?.id === "unit-editor-context"), undefined)
  view.find(node => node.type === "Input" && node.props.maxLength === 255).props.onChange({ target: { value: "Image renamed" } })
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(view.saved.length, 0)
  assert.equal(view.find(node => node.props?.role === "alert").props.children, "请填写文字输入、缓存文字和图像输出单价。")
  view.props.busy = true
  assert.ok(view.all().filter(node => node.type === "Input" || node.type === "Checkbox").every(node => node.props.disabled))
})

test("model editor retains source validation wording and one vendor identity row", () => {
  const view = editorHarness(savedModel(), "openai_compatible")
  const identity = view.find(node => node.props?.className === "model-editor-identity")
  const labels = view.nodes(identity).filter(node => node.type === "dt").map(node => node.props.children)
  assert.deepEqual(labels, ["目标网关", "API 服务商", "连接", "模型别名", "上游模型 ID"])
  view.find(node => node.type === "Input" && node.props.id === "unit-editor-context").props.onChange({ target: { value: "-1" } })
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(view.saved.length, 0)
  assert.equal(view.find(node => node.props?.role === "alert").props.children, "上下文窗口须为正整数，或留空。")
})

test("model editor density and responsive rules retain the source composition", () => {
  const stylesheet = frontendRequire("postcss").parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const declarations = (selector, media = null) => {
    const matches = []
    stylesheet.walkRules(rule => {
      if (rule.selector === selector && (rule.parent.type === "atrule" ? rule.parent.params : null) === media) matches.push(rule)
    })
    assert.equal(matches.length, 1, selector)
    return Object.fromEntries(matches[0].nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))
  }
  const identity = declarations(".model-editor-identity")
  assert.equal(identity.background, "var(--muted)")
  assert.equal(identity.padding, "10px 12px")
  assert.equal(identity.gap, "8px 14px")
  assert.equal(identity["border-radius"], "8px")
  assert.equal(declarations(".model-editor-advanced-body").padding, "6px 0 2px")
  assert.equal(declarations(".model-editor-checkbox")["min-height"], "26px")
  assert.equal(declarations(".model-editor-checkbox-grid")["grid-template-columns"], "repeat(3, minmax(0, 1fr))")
  assert.equal(declarations(".model-editor-identity", "(max-width: 640px)")["grid-template-columns"], "1fr")
  assert.equal(declarations(".model-editor-checkbox-grid", "(max-width: 640px)")["grid-template-columns"], "repeat(2, minmax(0, 1fr))")
})

for (const [locale, exportName] of [["en", "ENGLISH_CORE_PHRASES"], ["ja", "JAPANESE_CORE_PHRASES"], ["ko", "KOREAN_CORE_PHRASES"]]) {
  test(`model editor source help and validation are localized in ${locale}`, async () => {
    const core = (await import(new URL(`../../../frontend/src/locales/${locale}/phrases-core.ts`, import.meta.url)))[exportName]
    const image = await import(new URL(`../../../frontend/src/locales/${locale}/phrases-image-generation.ts`, import.meta.url))
    const phrases = { ...core, ...Object.values(image)[0] }
    for (const phrase of ["更新模型名称、价格和访问配置。连接与模型标识保持不变。", "显示名称须为 1–255 个字符。", "上下文窗口须为正整数，或留空。", "单价须为非负有限数字，或留空。", "连接和模型标识决定已发布路由，仅供查看；更换连接请走模型接入与发布流程。", "设为整个模型目录的默认模型，并将其连接设为默认；不改变网关路由优先级。", "图片模型不能设为聊天默认", "角色限制不替代人员模型分配，仍需满足连接和人员的访问策略。", "当前应用登录角色为 Owner 和 Member；其他已有角色值按兼容配置保留。"]) {
      assert.ok(phrases[phrase], phrase)
      assert.notEqual(phrases[phrase], phrase)
    }
  })
}

const catalogOption = (overrides = {}) => ({
  reference: "azure_retail:Azure OpenAI:gpt 4.1:Global:*",
  deployment: "Global",
  regions: ["brazilsouth", "canadaeast", "eastus"],
  region_required: false,
  input_per_million: 2,
  output_per_million: 8,
  cached_per_million: 0.5,
  cache_write_per_million: null,
  ...overrides,
})

test("a model keeps its typed rates until someone opts it into a list price", () => {
  const model = savedModel()
  const draft = createModelEditDraft(model)
  assert.equal(draft.priceSource, "manual")
  const payload = modelEditPayload(model, draft)
  assert.equal(payload.price_source, "manual")
  assert.equal(payload.price_reference, null)
  assert.equal(payload.input_cost_per_million, model.input_cost_per_million)
})

test("choosing a deployment prices every bucket the source publishes", () => {
  const draft = applyCatalogOption(
    createModelEditDraft(savedModel()), "azure_retail", catalogOption(), 90)
  assert.equal(draft.priceSource, "azure_retail")
  assert.equal(draft.priceReference, "azure_retail:Azure OpenAI:gpt 4.1:Global:*")
  assert.equal(draft.inputPrice, "1.8")
  assert.equal(draft.outputPrice, "7.2")
  assert.equal(draft.cacheReadPrice, "0.45")
  assert.equal(draft.cacheWritePrice, "")
})

test("a discount of 100 percent and no discount price identically", () => {
  assert.equal(discountedRate(2, 100), 2)
  assert.equal(discountedRate(2, null), 2)
  assert.equal(discountedRate(15, 66), 9.9)
})

test("a model discount overrides the connection, and blank inherits it", () => {
  const base = createModelEditDraft(savedModel())
  assert.deepEqual(resolveDiscount(base, 90), { percent: 90, inherited: true })
  assert.deepEqual(resolveDiscount({ ...base, discountPercent: "66" }, 90),
    { percent: 66, inherited: false })
  assert.deepEqual(resolveDiscount(base, null), { percent: null, inherited: true })
})

test("following a list price requires a chosen reference, and a discount stays in range", () => {
  const base = createModelEditDraft(savedModel())
  assert.equal(validateModelEdit({ ...base, priceSource: "anthropic" }), "price_reference")
  assert.equal(validateModelEdit({ ...base, discountPercent: "0" }), "discount")
  assert.equal(validateModelEdit({ ...base, discountPercent: "101" }), "discount")
  assert.equal(validateModelEdit({ ...base, discountPercent: "90" }), null)
})


test("a stored reference points back at the model it was chosen from", () => {
  assert.equal(
    modelKeyFromReference("azure_retail:Azure OpenAI:gpt 4.1:Global:*"),
    "azure_retail:Azure OpenAI:gpt 4.1")
  assert.equal(
    modelKeyFromReference("anthropic:Anthropic:Claude Opus 5:List price:*"),
    "anthropic:Anthropic:Claude Opus 5")
  // Anything that is not a reference has no model to point at, and saying so beats guessing.
  assert.equal(modelKeyFromReference("nonsense"), null)
})

function catalogResponse(overrides = {}) {
  return {
    model_entry: { key: "azure_retail:Azure OpenAI:gpt 4.1", label: "gpt 4.1", product: "Azure OpenAI", source: "azure_retail" },
    options: [catalogOption()], complete: true, unreadable: [], other_meters: [], note: null,
    ...overrides,
  }
}

function priceSourceHarness(response, model = savedModel(), api = {}) {
  const slots = []
  const effects = []
  const pending = []
  let cursor = 0
  let effectCursor = 0
  let draft = { ...createModelEditDraft(model), priceSource: "azure_retail" }
  const imports = {
    react: {
      useState(initial) {
        const index = cursor++
        if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
        return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value }]
      },
      useMemo: callback => callback(),
      useRef(initial) {
        const index = cursor++
        if (!(index in slots)) slots[index] = { current: initial }
        return slots[index]
      },
      useEffect(callback, dependencies) {
        const index = effectCursor++
        const previous = effects[index]
        if (previous && dependencies.every((value, position) => Object.is(value, previous.dependencies[position]))) return
        effects[index] = { dependencies }
        pending.push(() => {
          previous?.cleanup?.()
          effects[index].cleanup = callback()
        })
      },
    },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "./model-edit-form": modelEditForm,
    "../../data-sources/apim/api": { dataSource: { priceCatalogOptions: async () => response, ...api } },
  }
  const exports = {}
  runInNewContext(priceComponent, { exports, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
    : value && typeof value === "object" ? [value, ...nodes(typeof value.type === "function" ? value.type(value.props) : value.props?.children)] : []
  const render = () => {
    cursor = 0
    effectCursor = 0
    const tree = exports.ModelPriceSourceFields({ model, draft, setDraft: updater => { draft = updater(draft) }, busy: false, connectionDiscount: 90 })
    for (const effect of pending.splice(0)) effect()
    return nodes(tree)
  }
  return {
    get draft() { return draft },
    payload: () => modelEditPayload(model, draft),
    all: render,
    find: predicate => render().find(predicate),
    async flush() { await Promise.resolve(); await Promise.resolve(); return render() },
  }
}

for (const complete of [false, undefined]) {
  test(`an incomplete catalog (${complete}) cannot replace the model's typed prices`, async () => {
    const response = catalogResponse({ complete, options: [catalogOption({ cached_per_million: null })] })
    const harness = priceSourceHarness(response)
    const before = { ...harness.draft }

    await harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)

    assert.deepEqual(JSON.parse(JSON.stringify(harness.draft)), JSON.parse(JSON.stringify(before)))
    assert.equal(validateModelEdit(harness.draft), "price_reference")
    assert.throws(() => harness.payload(), /price_reference/)
    assert.equal(harness.find(node => node.props?.name === "price-deployment").props.disabled, true)
    harness.find(node => node.type?.name === "DeploymentStep").props.onChoose(response.options[0])
    assert.deepEqual(JSON.parse(JSON.stringify(harness.draft)), JSON.parse(JSON.stringify(before)))
  })
}

test("partial prices do not overwrite saved buckets when a discount changes", async () => {
  const response = catalogResponse({ complete: false, options: [catalogOption({ cached_per_million: null })] })
  const model = savedModel({ price_source: "azure_retail", price_reference: response.options[0].reference })
  const harness = priceSourceHarness(response, model)
  harness.all()
  await harness.flush()

  const discount = harness.find(node => node.props?.id === "model-discount")
  assert.equal(discount.props.disabled, true)
  discount.props.onChange({ target: { value: "80" } })
  harness.all()

  for (const field of ["input_cost_per_million", "output_cost_per_million", "cached_cost_per_million", "cache_write_cost_per_million", "price_reference"]) {
    assert.equal(harness.payload()[field], model[field], field)
  }
})

test("complete catalog selection still applies the connection discount", async () => {
  const response = catalogResponse()
  const harness = priceSourceHarness(response)

  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)

  assert.equal(harness.payload().input_cost_per_million, 1.8)
  assert.equal(harness.payload().cached_cost_per_million, 0.45)
  assert.equal(validateModelEdit(harness.draft), null)
  assert.equal(harness.find(node => node.props?.name === "price-deployment").props.disabled, false)
})

test("the selected region survives saving, discount changes, and reopening", async () => {
  const references = Object.fromEntries(["eastus", "westus", "northeurope"].map(region => [region, `azure_retail:Azure OpenAI:gpt 4.1:Regional:${region}`]))
  const response = catalogResponse({ options: [
    catalogOption({ reference: references.eastus, deployment: "Regional", regions: ["eastus", "westus"], region_required: true, references_by_region: { eastus: references.eastus, westus: references.westus } }),
    catalogOption({ reference: references.northeurope, deployment: "Regional", regions: ["northeurope"], region_required: true, references_by_region: { northeurope: references.northeurope }, input_per_million: 3 }),
  ] })
  const harness = priceSourceHarness(response)
  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)
  const westus = harness.find(node => node.type === "SelectItem" && node.props.value === references.westus)
  assert.equal(westus.props.value, references.westus)
  harness.find(node => node.type === "Select" && node.props.name === "price-region").props.onValueChange(westus.props.value)
  assert.equal(harness.payload().price_reference, references.westus)

  harness.find(node => node.props?.id === "model-discount").props.onChange({ target: { value: "80" } })
  harness.all()
  assert.equal(harness.payload().input_cost_per_million, 1.6)
  assert.equal(harness.payload().price_reference, references.westus)

  const reopened = priceSourceHarness(response, savedModel(harness.payload()))
  reopened.all()
  await reopened.flush()
  assert.equal(reopened.find(node => node.type === "Select" && node.props.name === "price-region").props.value, references.westus)
})

test("image pricing preserves its configured source and exposes the public controls", () => {
  const model = savedModel({
    capabilities: ["image_generation"], price_source: "models_dev",
    price_reference: "models_dev:azure:gpt-image-2.5-flare",
  })
  assert.equal(createModelEditDraft(model).priceSource, "models_dev")
  assert.ok(editorHarness(model).find(node => node.type === "ModelPriceSourceFields"))
  const unconfigured = createModelEditDraft({ ...model, price_source_configured: false })
  assert.equal(unconfigured.priceSource, "models_dev")
  assert.equal(createModelEditDraft({ ...model, price_source: "manual" }).priceSource, "manual")
})

test("Azure search passes its source and rejects cross-source results and selections", async () => {
  const publicModel = { key: "models_dev:nano-gpt:grok-4.7", label: "Grok 4.7", product: "NanoGPT", source: "models_dev" }
  const calls = []
  const response = catalogResponse()
  const harness = priceSourceHarness(response, savedModel(), {
    priceCatalogModels: async (query, source) => {
      calls.push({ query, source })
      return { models: [publicModel, response.model_entry], unavailable: [] }
    },
  })
  await harness.find(node => node.type?.name === "ModelStep").props.onSearch()
  assert.equal(calls[0].source, "azure_retail")
  assert.deepEqual(harness.find(node => node.type?.name === "ModelStep").props.matches.map(item => item.source), ["azure_retail"])
  const before = { ...harness.draft }
  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(publicModel)
  assert.deepEqual(harness.draft, before)
})

test("switching official sources clears loaded details and late searches", async () => {
  let finish
  const harness = priceSourceHarness(catalogResponse(), savedModel(), {
    priceCatalogModels: (_query, source) => source === "azure_retail"
      ? new Promise(resolve => { finish = resolve })
      : Promise.resolve({ models: [], unavailable: [] }),
  })
  const pending = harness.find(node => node.type?.name === "ModelStep").props.onSearch()
  harness.find(node => node.type === "button" && node.props["aria-label"] === "Anthropic 列表价").props.onClick()
  harness.all()
  finish({ models: [catalogResponse().model_entry], unavailable: [] })
  await pending
  await harness.flush()
  assert.equal(harness.draft.priceSource, "anthropic")
  const step = harness.find(node => node.type?.name === "ModelStep")
  assert.deepEqual(JSON.parse(JSON.stringify(step.props.matches ?? [])), [])
  assert.equal(step.props.chosen, null)
  assert.equal(step.props.searching, false)
})

test("late options cannot switch a manual draft back to Azure or overwrite prices", async () => {
  let finish
  const response = catalogResponse()
  const harness = priceSourceHarness(response, savedModel(), {
    priceCatalogOptions: () => new Promise(resolve => { finish = resolve }),
  })
  const pending = harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)
  harness.find(node => node.type === "button" && node.props["aria-label"] === "手工填写").props.onClick()
  harness.all()
  const before = { ...harness.draft }
  finish(response)
  await pending
  assert.equal(JSON.stringify(harness.draft), JSON.stringify(before))
})

test("selected source rejects options carrying a different source", async () => {
  const response = catalogResponse()
  const harness = priceSourceHarness({
    ...response, model_entry: { ...response.model_entry, source: "models_dev" },
  })
  const before = { ...harness.draft }
  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)
  assert.deepEqual(JSON.parse(JSON.stringify(harness.draft)), JSON.parse(JSON.stringify(before)))
})
