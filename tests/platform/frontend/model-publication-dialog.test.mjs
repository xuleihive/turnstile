import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire, stripTypeScriptTypes } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import * as connections from "../../../frontend/src/components/model-management/model-publication-connections.ts"
import * as compatible from "../../../frontend/src/components/model-management/openai-compatible.ts"
import * as modelEditForm from "../../../frontend/src/components/model-management/model-edit-form.ts"
const pricing = modelEditForm
import * as gatewayTopology from "../../../frontend/src/lib/gateway-topology.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const vendorEntry = fileURLToPath(new URL("../../../frontend/src/components/model-management/model-vendor-select.tsx", import.meta.url))
const vendorBundle = await rolldown({ input: vendorEntry, external: id => id !== vendorEntry, transform: { jsx: { runtime: "automatic" } }, treeshake: false })
const vendorExports = {}
try {
  const code = (await vendorBundle.generate({ format: "cjs" })).output[0].code
  runInNewContext(code, { exports: vendorExports, require: name => name === "react/jsx-runtime" ? frontendRequire(name) : new Proxy({}, { get: (_target, key) => key }) })
} finally { await vendorBundle.close() }
const entry = fileURLToPath(new URL("../../../frontend/src/components/model-management/model-publication-dialog.tsx", import.meta.url))
const bundle = await rolldown({ input: entry, external: id => id !== entry, transform: { jsx: { runtime: "automatic" } }, treeshake: false })
let compiled
try { compiled = (await bundle.generate({ format: "cjs" })).output[0].code }
finally { await bundle.close() }
const apiSource = readFileSync(new URL("../../../frontend/src/data-sources/apim/api.ts", import.meta.url), "utf8")
const normalization = apiSource.slice(apiSource.indexOf("const LEGACY_CLOUD_CODE_PROVIDER_ID"), apiSource.indexOf("const normalizeGatewayPublication"))
const normalizeRegistry = runInNewContext(`${stripTypeScriptTypes(normalization)}\nnormalizeRegistry`)

function registry(overrides = {}) {
  return {
    image_generation_supported: true, image_configuration_schema_version: 4,
    self_hosted_gateway_enabled: true,
    gateways: [{ id: "gateway", name: "Gateway", implementation: "apim", enabled: true }],
    providers: [{ id: "provider", name: "Foundry", provider_kind: "microsoft_foundry", brand_key: "microsoft_foundry", enabled: true }],
    runtimes: [{ id: "connection", name: "Foundry connection", gateway_profile_id: "gateway", provider_id: "provider", provider_name: "Foundry", brand_key: "microsoft_foundry", enabled: true,
      config: { control_plane_managed: true, project_endpoint: "https://unit.services.ai.azure.com/api/projects/unit", auth_strategy: "managed_identity" } }],
    models: [], ...overrides,
  }
}

function nodes(value) {
  if (Array.isArray(value)) return value.flatMap(nodes)
  if (!value || typeof value !== "object") return []
  return [value, ...nodes(typeof value.type === "function" ? value.type(value.props) : value.props?.children)]
}

function text(value) {
  if (Array.isArray(value)) return value.map(text).join("")
  if (value && typeof value === "object") return text(typeof value.type === "function" ? value.type(value.props) : value.props?.children)
  return value == null || typeof value === "boolean" ? "" : String(value)
}

function harness(rawRegistry = registry(), publication, publishFailure, targets = null, executeEffects = false) {
  const slots = []
  const effects = []
  const pendingEffects = []
  let cursor = 0
  let effectCursor = 0
  const writes = []
  const queued = []
  const topology = { data: targets, isPending: false, error: null, refetch() {} }
  const props = { registry: normalizeRegistry(rawRegistry), publicationId: publication?.id ?? null,
    onPublicationQueued: id => queued.push(id), onManageConnections() {}, onClose() {} }
  const primitives = new Proxy({}, { get: (_target, name) => name })
  const hooks = {
    useState(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
      return [slots[index], next => { slots[index] = typeof next === "function" ? next(slots[index]) : next }]
    },
    useRef(initial) { return { current: initial } },
    useEffect(callback, dependencies) {
      if (!executeEffects) return
      const index = effectCursor++
      const previous = effects[index]
      if (previous && dependencies?.every((value, position) => Object.is(value, previous.dependencies[position]))) return
      effects[index] = { dependencies }
      pendingEffects.push(() => {
        previous?.cleanup?.()
        effects[index].cleanup = callback()
      })
    },
  }
  const queryClient = { setQueryData() {}, invalidateQueries() {} }
  const exports = {}
  const imports = {
    react: hooks, "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@tanstack/react-query": { useQuery: options => options.queryKey?.[0] === "targets" ? topology
      : options.queryKey?.[0] === "upgrade" ? { data: {
        previous_publication_id: "local-publication", previous_installation_sha256: "a".repeat(64),
        installation: { model_additions_enabled: true, decision_enabled: true,
          supported_profiles: ["microsoft-decision", "openai-chat", "openai-images"] },
      }, isPending: false, isFetching: false } : ({ data: publication }), useQueryClient: () => queryClient },
    "../../data-sources/apim/api": { dataSource: {
      publishModel: async request => { writes.push(request); if (publishFailure) throw publishFailure; return { publication: { id: "accepted" } } },
      retryGatewayPublication: async (...args) => { writes.push(args); return { id: publication.id } },
    } },
    "../../data-sources/apim/queries": { finopsKeys: { gatewayPublication: id => [id] }, finopsQueries: {
      gatewayPublication: () => ({}), apimTargets: () => ({ queryKey: ["targets"] }),
      gatewayProgramUpgradePreview: () => ({ queryKey: ["upgrade"] }),
    } },
    "../../lib/gateway-topology": gatewayTopology,
    "../../locales/index": { useLocale: () => ({ locale: "zh-CN" }), translateForLocale: async value => value },
    "./model-publication-connections": connections, "./openai-compatible": compatible,
    "./model-edit-form": modelEditForm,
    "./model-vendor-select": { ...vendorExports, ModelVendorLogo: "ModelVendorLogo" },
    "./public-model-pricing": {
      PublicModelPricing: "PublicModelPricing",
      emptyPricingDraft: () => ({
        priceSource: "models_dev", priceReference: "", priceEntryDigest: "",
        allowUnpriced: false, discountPercent: "", contextWindow: "",
        inputPrice: "", outputPrice: "", cacheReadPrice: "", cacheWritePrice: "",
      }),
    },
    "../brand-logos": { GatewayBrandLogo: "GatewayBrandLogo", ManagedGatewayLogo: "ManagedGatewayLogo", ProviderBrandLogo: "ProviderBrandLogo", gatewayBrandFromIdentity: () => "generic", providerBrandFromMetadata: () => "generic" },
  }
  runInNewContext(compiled, { exports, require: name => imports[name] ?? primitives })
  const render = () => {
    cursor = 0
    effectCursor = 0
    props.topology = topology.data
    props.topologyAvailable = topology.data?.deployment_scoping_available === true
    const result = exports.ModelPublicationDialog(props)
    for (const effect of pendingEffects.splice(0)) effect()
    return result
  }
  const find = predicate => nodes(render()).find(predicate)
  const input = (id, value) => {
    const control = find(node => node.type === "Input" && node.props.id === id)
    assert.ok(control, `Missing input ${id}`)
    control.props.onChange({ target: { value } })
  }
  const select = (value, next) => {
    const control = find(node => node.type === "Select" && node.props.value === value)
    assert.ok(control, `Missing selection ${value}`)
    control.props.onValueChange(next)
  }
  const submit = async () => {
    const form = find(node => node.type === "form")
    if (form) form.props.onSubmit({ preventDefault() {} })
    else find(node => node.type === "Button" && Array.isArray(node.props.children)
      && node.props.children.includes("发布模型")).props.onClick()
    await Promise.resolve()
  }
  const manual = () => find(node => node.type === "ModelPriceSourceFields").props.setDraft(current => ({ ...current, priceSource: "manual" }))
  const publishButton = () => find(node => node.type === "Button" && Array.isArray(node.props.children) && node.props.children.includes("发布模型"))
  return { props, topology, writes, queued, render, find, input, select, submit, manual, publishButton }
}

function readyTopology() {
  return { deployment_scoping_available: true, items: [{
    target: { gateway_profile_id: "gateway", service_name: "unit-apim", api_id: "unit-api",
      drift_state: "in_sync", capabilities: { self_hosted_gateway: "supported" } },
    deployments: ["managed", "edge"].map(identity => ({
      deployment: { id: identity, native_gateway_id: identity, display_name: identity,
        kind: identity === "managed" ? "managed" : "self_hosted", retired_at: null,
        declared_state: "present", reachability_state: "reachable", release_gate: "required",
        endpoint_url: `https://${identity}.example.test/unit/llm` },
      inventory: { associated_api_ids: ["unit-api"] },
    })),
  }] }
}

test("failed publication shows its recorded target and does not assert current serving health", () => {
  const publication = {
    id: "publication", gateway_profile_id: "gateway", status: "failed", publication_kind: "model_add",
    model_key: "unit-model", target_deployments: [{ deployment_id: "edge", native_gateway_id: "edge", kind: "self_hosted" }],
    failure: { code: "credential_source_missing" }, retry_requires_credential: true,
  }
  const view = harness(registry(), publication, undefined, readyTopology())
  const rendered = text(view.render())
  assert.ok(rendered.includes("目标 Gateway"))
  assert.ok(rendered.includes("Self-hosted"))
  assert.ok(rendered.includes("unit-apim"))
  assert.ok(rendered.includes("APIM 凭据资源尚未创建"))
  assert.ok(rendered.includes("生效状态未确认"))
  assert.ok(!rendered.includes("当前模型继续正常服务"))
  assert.equal(view.find(node => node.props?.["data-deployment-id"] === "managed"), undefined)
})

test("scoped publication selectors show APIM and Gateway identities with labels and type icons", () => {
  const view = harness(registry(), undefined, undefined, readyTopology())
  assert.ok(view.find(node => node.type === "SelectTrigger" && node.props["aria-label"] === "APIM 实例"))
  assert.ok(view.find(node => node.type === "SelectTrigger" && node.props["aria-label"] === "Gateway API"))
  const selected = () => view.find(node => node.props?.className === "gateway-api-select-value")
  assert.equal(text(selected()), "managed")
  assert.equal(selected().props.children[0].type, "ManagedGatewayLogo")
  view.select("gateway:managed", "gateway:edge")
  assert.equal(text(selected()), "edgeSelf-hosted Gateway")
  assert.equal(selected().props.children[0].type, "Server")
  const control = view.find(node => node.type === "Select" && node.props.value === "gateway:edge")
  assert.deepEqual(Array.from(control.props.items, item => [item.value, item.label]), [
    ["gateway:managed", "managed"], ["gateway:edge", "edge"],
  ])
  const option = view.find(node => node.type === "SelectItem" && node.props.value === "gateway:edge")
  assert.equal(option.props.children.props.className, "gateway-api-select-option")
  assert.equal(text(option), "edgeSelf-hosted Gateway")
})

test("self-hosted publication choices require an explicitly enabled capability", () => {
  for (const enabled of [true, false, undefined]) {
    const view = harness(registry({ self_hosted_gateway_enabled: enabled }), undefined, undefined, readyTopology())
    assert.ok(view.find(node => node.type === "SelectItem" && node.props.value === "gateway:managed"))
    assert.equal(Boolean(view.find(node => node.type === "SelectItem" && node.props.value === "gateway:edge")), enabled === true)
  }
})

test("explicit deployment choice submits only its target and selected scope", async () => {
  const view = harness(registry(), undefined, undefined, readyTopology())
  view.select("gateway:managed", "gateway:edge")
  view.manual()
  view.input("foundry-deployment", "unit-model")
  await view.submit()
  assert.equal(view.writes.length, 1)
  assert.equal(view.writes[0].gateway_profile_id, "gateway")
  assert.equal(view.writes[0].deployment_scope.mode, "selected")
  assert.deepEqual(Array.from(view.writes[0].deployment_scope.deployment_ids), ["edge"])
})

test("a removed explicit deployment blocks publication without choosing another", async () => {
  const view = harness(registry(), undefined, undefined, readyTopology())
  view.select("gateway:managed", "gateway:edge")
  view.input("foundry-deployment", "unit-model")
  view.topology.data.items[0].deployments.pop()
  await view.submit()
  assert.equal(view.writes.length, 0)
  assert.ok(text(view.render()).includes("请选择可用的 Gateway API。"))
})

test("disabled target scoping preserves the legacy publication payload", async () => {
  const view = harness(registry(), undefined, undefined, { deployment_scoping_available: false, items: [] })
  view.manual()
  view.input("foundry-deployment", "unit-model")
  await view.submit()
  assert.equal(view.writes.length, 1)
  assert.equal(Object.hasOwn(JSON.parse(JSON.stringify(view.writes[0])), "deployment_scope"), false)
})

test("publication errors use the existing notice layout without shrinking long messages", async () => {
  const failure = new Error(`API 422: ${"price_source is not supported. ".repeat(30)}`)
  const view = harness(registry(), undefined, failure)
  view.manual()
  view.input("foundry-deployment", "gpt-5.6-sol")
  await view.submit()
  await new Promise(resolve => setImmediate(resolve))
  const alert = view.find(node => node.props?.role === "alert")
  assert.equal(alert.props.className, "registry-error publication-form-error")
  assert.equal(alert.props.children[0].type, "TriangleAlert")
  assert.equal(alert.props.children[1].type, "div")
  assert.equal(alert.props.children[1].props["data-no-localize"], true)
  assert.equal(text(alert.props.children[1]), String(failure))
  assert.equal(view.queued.length, 0)
  const form = view.find(node => node.props?.className === "registry-editor simple-model-form")
  assert.equal(form.props.children.at(-2).type.name, "PublicationErrorNotice")
  assert.equal(form.props.children.at(-1).props.className, "registry-editor-footer")
  assert.equal(form.props.children.at(-3).props.className, "registry-editor-body simple-model-body")
  const stylesheet = frontendRequire("postcss").parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const styles = new Map()
  stylesheet.walkRules(rule => styles.set(rule.selector, Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))))
  const notice = styles.get(".registry-error.publication-form-error")
  assert.equal(notice["flex-shrink"], "0")
  assert.equal(notice.display, "grid")
  assert.equal(notice.margin, "12px 16px 16px")
  assert.equal(notice["max-height"], "min(180px, 25dvh)")
  assert.equal(notice["overflow-y"], "auto")
  for (const property of ["color", "background", "border-color"]) assert.equal(notice[property], undefined)
  assert.equal(styles.get(".publication-form-error > div")["overflow-wrap"], "anywhere")
  assert.equal(styles.get(".publication-form-error > div")["white-space"], "pre-wrap")
})

test("unsupported publication pricing fields explain the backend version mismatch without retrying", async () => {
  const failure = new Error(`API 422: ${JSON.stringify({ detail: ["price_source", "price_reference", "price_discount_percent"].map(field => ({
    type: "extra_forbidden", loc: ["body", "model", field], msg: "Extra inputs are not permitted", input: "do-not-display-input",
  })) })}`)
  const view = harness(registry(), undefined, failure)
  view.input("foundry-deployment", "gpt-5.6-sol")
  const picker = view.find(node => node.type === "ModelPriceSourceFields")
  picker.props.setDraft(current => ({ ...current, priceSource: "azure_retail", priceReference: "azure_retail:Azure OpenAI GPT5:5.6 sol ShortCo Std:Global:*" }))
  await view.submit()
  await new Promise(resolve => setImmediate(resolve))
  const alert = view.find(node => node.props?.role === "alert")
  assert.equal(text(alert), "前后端计价接口版本不一致，请更新后端后再发布。")
  assert.equal(view.writes.length, 1)
  assert.equal(view.writes[0].model.price_source, "azure_retail")
  assert.equal(view.queued.length, 0)
  assert.equal(text(view.render()).includes("do-not-display-input"), false)
})

test("publication validation errors show fields and messages instead of raw response input", async () => {
  const failure = new Error(`API 422: ${JSON.stringify({ detail: [{
    type: "greater_than_equal", loc: ["body", "model", "context_window"], msg: "Input should be greater than or equal to 1", input: "do-not-display-input",
  }] })}`)
  const view = harness(registry(), undefined, failure)
  view.manual()
  view.input("foundry-deployment", "gpt-5.6-sol")
  await view.submit()
  await new Promise(resolve => setImmediate(resolve))
  const alert = view.find(node => node.props?.role === "alert")
  assert.equal(text(alert), "model.context_window: Input should be greater than or equal to 1")
  assert.equal(text(view.render()).includes("do-not-display-input"), false)
})

test("manual chat prices remain optional and do not send unused catalog fields", async () => {
  const view = harness()
  view.manual()
  view.input("foundry-deployment", "gpt-5.6-sol")
  for (const id of ["publication-input-price", "publication-output-price", "publication-cache-read-price", "publication-cache-write-price"]) {
    assert.equal(view.find(node => node.props?.id === id).props.required === true, false)
  }
  await view.submit()
  assert.equal(view.writes.length, 1)
  const model = view.writes[0].model
  for (const field of ["price_source", "price_reference", "price_discount_percent"]) assert.equal(Object.hasOwn(model, field), false)
  for (const field of ["input_cost_per_million", "output_cost_per_million", "cached_cost_per_million", "cache_write_cost_per_million"]) assert.equal(model[field], null)
})

test("normalized backend image capability controls the real component option", () => {
  for (const [enabled, version, disabled] of [[true, 4, false], [false, 4, true], [undefined, 4, true], [true, 3, false]]) {
    const view = harness(registry({ image_generation_supported: enabled, image_configuration_schema_version: version }))
    assert.equal(view.find(node => node.type === "SelectItem" && node.props.value === "image_generation").props.disabled, disabled)
  }
})

test("public pricing is default and its confirmed mapping accompanies publication", async () => {
  const view = harness()
  view.input("foundry-deployment", "gpt-5")
  const pricing = view.find(node => node.type === "ModelPriceSourceFields")
  assert.equal(pricing.props.draft.priceSource, "models_dev")
  pricing.props.setDraft(current => ({
    ...current, priceReference: "models_dev:openai:gpt-5", priceEntryDigest: "a".repeat(64),
    contextWindow: "400000", inputPrice: "1.25", outputPrice: "10", cacheReadPrice: "0",
  }))
  await view.submit()
  assert.equal(view.writes.length, 1)
  assert.equal(view.writes[0].model.price_source, "models_dev")
  assert.equal(view.writes[0].model.price_reference, "models_dev:openai:gpt-5")
  assert.equal(view.writes[0].model.price_entry_digest, "a".repeat(64))
  assert.equal(view.writes[0].model.context_window, 400000)
  assert.equal(view.writes[0].model.cached_cost_per_million, 0)
})

test("switching to manual preserves prices and does not collapse pricing", () => {
  const view = harness()
  view.find(node => node.type === "ModelPriceSourceFields").props.setDraft(current => ({
    ...current, inputPrice: "1.25", outputPrice: "10",
  }))
  view.find(node => node.type === "ModelPriceSourceFields").props.setDraft(current => ({ ...current, priceSource: "manual" }))
  assert.ok(view.find(node => node.props?.className === "simple-model-section simple-pricing"))
  const price = view.find(node => node.type === "Input" && node.props.id === "publication-input-price")
  assert.equal(price.props.value, "1.25")
  assert.equal(price.props.disabled, false)
})

test("single gateway uses summary, multiple gateways use a selector and empty stays blocked", () => {
  const single = harness()
  assert.ok(single.find(node => node.props?.className === "publication-target-summary"))
  assert.equal(single.find(node => node.type === "SelectTrigger" && node.props["aria-label"] === "目标网关"), undefined)
  const data = registry()
  data.gateways.push({ ...data.gateways[0], id: "other", name: "Other gateway" })
  const multiple = harness(data)
  multiple.select("gateway", "other")
  assert.ok(text(multiple.render()).includes("此网关没有可用连接"))
  assert.equal(multiple.publishButton().props.disabled, true)
})

test("source dialog composition retains connection help, deployment help and credential section", () => {
  const data = registry()
  data.runtimes[0].config = { ...data.runtimes[0].config, auth_strategy: "named_value_api_key", credential_provisioned: false }
  const view = harness(data)
  const copy = text(view.render())
  for (const phrase of ["选择已有连接，将模型或部署发布到所选网关。", "连接包含服务端点和认证方式", "Turnstile 不会创建上游部署", "首次发布凭据", "价格未填写"]) assert.ok(copy.includes(phrase), phrase)
  const grid = view.find(node => node.props?.className?.includes("publication-model-identity-grid"))
  assert.ok(nodes(grid).some(node => node.props?.id === "foundry-deployment"))
  assert.ok(nodes(grid).some(node => node.props?.["aria-label"] === "模型用途"))
})

test("connection type uses a neutral OpenAI-compatible icon and retains provider branding", () => {
  for (const openaiCompatible of [false, true]) {
    const data = registry()
    if (openaiCompatible) {
      data.providers[0] = { ...data.providers[0], provider_kind: "openai_compatible", brand_key: "generic", name: "Unit provider" }
      data.runtimes[0] = { ...data.runtimes[0], brand_key: "generic", name: "Unit connection", provider_name: "Unit provider", config: { control_plane_managed: true, api_format: "openai_chat", base_url: "https://unit.example/v1", auth_strategy: "named_value_bearer", credential_provisioned: true, model_vendor: "deepseek" } }
    }
    const view = harness(data)
    const summary = view.find(node => node.props?.className === "publication-connection-summary")
    assert.ok(summary)
    const typeRow = nodes(summary).find(node => node.type === "div" && node.props.children?.[0]?.type === "dt" && text(node.props.children[0]) === "接入类型")
    assert.ok(typeRow)
    assert.equal(nodes(typeRow).some(node => node.type === "Plug"), openaiCompatible)
    assert.equal(nodes(typeRow).some(node => node.type === "ProviderBrandLogo"), !openaiCompatible)
    if (openaiCompatible) {
      assert.ok(text(typeRow).includes("OpenAI-compatible API"))
      assert.equal(nodes(typeRow).find(node => node.type === "Plug").props["aria-hidden"], "true")
      assert.ok(nodes(summary).some(node => node.type === "ModelVendorLogo" && node.props.value === "deepseek"))
    }
    assert.equal(view.writes.length, 0)
  }
})

test("image price states accept explicit zero and submit all three rates without chat fields", async () => {
  const view = harness()
  view.select("chat", "image_generation")
  view.manual()
  view.input("foundry-deployment", " image-deployment ")
  await view.submit()
  assert.equal(view.writes.length, 0)
  view.input("publication-input-price", "0")
  assert.ok(text(view.render()).includes("价格部分填写"))
  view.input("publication-output-price", "30")
  view.input("publication-cache-read-price", "1.25")
  assert.ok(text(view.render()).includes("价格已填写"))
  assert.equal(view.find(node => node.props?.id === "publication-context-window"), undefined)
  await view.submit()
  const sent = view.writes[0]
  assert.equal(sent.runtime.existing_id, "connection")
  assert.equal(sent.runtime.api_key, undefined)
  assert.equal(sent.model.operation, "image_generation")
  assert.equal(sent.model.deployment_name, "image-deployment")
  assert.equal(sent.model.input_cost_per_million, 0)
  assert.equal(sent.model.output_cost_per_million, 30)
  assert.equal(sent.model.cached_cost_per_million, 1.25)
  assert.equal(sent.model.context_window, null)
  assert.equal(sent.model.cache_write_cost_per_million, null)
})

test("image publication defaults to public synchronization and keeps the confirmed mapping", async () => {
  const view = harness()
  view.select("chat", "image_generation")
  view.input("foundry-deployment", "gpt-image-2.5-flare")
  const pricing = view.find(node => node.type === "ModelPriceSourceFields")
  assert.equal(pricing.props.publicRequest.operation, "image_generation")
  assert.equal(pricing.props.draft.priceSource, "models_dev")
  await view.submit()
  assert.equal(view.writes.length, 0)
  pricing.props.setDraft(current => ({
    ...current, priceReference: "models_dev:azure:gpt-image-2.5-flare",
    priceEntryDigest: "a".repeat(64), inputPrice: "5", outputPrice: "30", cacheReadPrice: "1.25",
  }))
  await view.submit()
  assert.equal(view.writes[0].model.price_source, "models_dev")
  assert.equal(view.writes[0].model.price_reference, "models_dev:azure:gpt-image-2.5-flare")
  assert.equal(view.writes[0].model.output_cost_per_million, 30)
  assert.equal(view.writes[0].model.context_window, null)
})

test("connection changes clear credentials, model operation, prices and deployment", () => {
  const data = registry()
  data.runtimes[0].config = { ...data.runtimes[0].config, auth_strategy: "named_value_api_key", credential_provisioned: false }
  data.runtimes.push({ ...data.runtimes[0], id: "second", name: "Second connection" })
  const view = harness(data)
  view.select("chat", "image_generation")
  view.manual()
  view.input("existing-connection-api-key", "unit-key")
  view.input("foundry-deployment", "unit-model")
  view.input("publication-input-price", "5")
  view.select("connection", "second")
  for (const id of ["existing-connection-api-key", "foundry-deployment"]) assert.equal(view.find(node => node.type === "Input" && node.props.id === id).props.value, "")
  assert.equal(view.find(node => node.type === "ModelPriceSourceFields").props.draft.inputPrice, "")
  assert.ok(view.find(node => node.type === "Select" && node.props.value === "chat"))
  assert.ok(text(view.render()).includes("价格未填写"))
})

test("new text models expose the shared pricing picker without a fabricated saved model", () => {
  const data = registry()
  data.runtimes[0].price_discount_percent = 90
  const view = harness(data)
  const section = view.find(node => node.props?.["aria-labelledby"] === "publication-pricing-heading")
  assert.equal(section.type, "section")
  const picker = view.find(node => node.type === "ModelPriceSourceFields")
  assert.equal(picker.props.model, undefined)
  assert.equal(picker.props.draft.priceSource, "models_dev")
  assert.equal(picker.props.connectionDiscount, 90)
})

test("publication submits the selected regional reference, effective rates and model discount", async () => {
  const data = registry()
  data.runtimes[0].price_discount_percent = 90
  for (const override of ["", "80"]) {
    const view = harness(data)
    view.input("foundry-deployment", "gpt-4.1-deployment")
    const picker = view.find(node => node.type === "ModelPriceSourceFields")
    assert.equal(picker.props.modelName, "gpt-4.1-deployment")
    picker.props.setDraft(current => {
      const draft = { ...current, discountPercent: override }
      return pricing.applyCatalogOption(draft, "azure_retail", {
        reference: "azure_retail:Azure OpenAI:gpt 4.1:Regional:westus",
        input_per_million: 2, output_per_million: 8,
        cached_per_million: 0.5, cache_write_per_million: null,
      }, pricing.resolveDiscount(draft, picker.props.connectionDiscount).percent)
    })
    assert.equal(view.find(node => node.props?.id === "publication-input-price"), undefined)
    await view.submit()
    assert.equal(view.writes.length, 1)
    const sent = view.writes[0].model
    assert.equal(sent.price_source, "azure_retail")
    assert.equal(sent.price_reference, "azure_retail:Azure OpenAI:gpt 4.1:Regional:westus")
    assert.equal(sent.price_discount_percent, override ? 80 : null)
    assert.equal(sent.input_cost_per_million, override ? 1.6 : 1.8)
    assert.equal(sent.cached_cost_per_million, override ? 0.4 : 0.45)
    assert.equal(sent.cache_write_cost_per_million, null)
  }
})

test("context pricing publishes complete references when the backend supports it", async () => {
  const view = harness(registry({ context_pricing_supported:true }))
  view.input("foundry-deployment", "unit-model")
  const references = ["azure_retail:Unit:short:Global:eastus", "azure_retail:Unit:long:Global:eastus"]
  view.find(node => node.type === "ModelPriceSourceFields").props.setDraft(current => ({
    ...current, priceSource:"azure_retail", contextPricingPreview:true, contextPriceReferences:references,
    contextPriceTiers:references.map((reference,index)=>({
      key:String(index),label:`Tier ${index}`,order:index,inputRange:{min_input_tokens:index?1001:0,max_input_tokens:index?null:1000},
      option:{reference,input_per_million:2,output_per_million:8,cached_per_million:0.5,cache_write_per_million:1},
    })),
  }))
  assert.equal(view.publishButton().props.disabled, false)
  await view.submit()
  assert.equal(view.writes.length,1)
  assert.deepEqual(view.writes[0].model.context_price_references,references)
  assert.equal(view.writes[0].model.price_reference,null)
  assert.equal(view.writes[0].model.input_cost_per_million,null)
  assert.equal(Object.hasOwn(view.writes[0].model,"context_price_tiers"),false)
})

test("context pricing preview cannot publish a single-price payload", async () => {
  const view = harness()
  view.input("foundry-deployment", "gpt-5.6-sol")
  view.find(node => node.type === "ModelPriceSourceFields").props.setDraft(current => ({
    ...current, priceSource: "azure_retail", contextPricingPreview: true, priceReference: "",
  }))
  assert.equal(view.publishButton().props.disabled, true)
  await view.submit()
  assert.equal(view.writes.length, 0)
  assert.ok(text(view.render()).includes("当前后端尚不支持分档计费，暂不可保存或发布。"))
})

test("new-model pricing rejects missing or mismatched references and invalid discounts", async () => {
  for (const changes of [
    { priceSource: "azure_retail" },
    { priceSource: "anthropic", priceReference: "azure_retail:Azure OpenAI:gpt 4.1:Global:*" },
    { discountPercent: "0" }, { discountPercent: "101" }, { discountPercent: "NaN" },
  ]) {
    const view = harness()
    view.input("foundry-deployment", "gpt-4.1-deployment")
    view.find(node => node.type === "ModelPriceSourceFields").props.setDraft(current => ({ ...current, ...changes }))
    await view.submit()
    assert.equal(view.writes.length, 0)
    assert.ok(view.find(node => node.props?.role === "alert"))
  }
})

test("switching connection or operation clears catalog references and discounts", () => {
  const data = registry()
  data.runtimes.push({ ...data.runtimes[0], id: "second", name: "Second connection" })
  for (const change of [view => view.select("connection", "second"), view => {
    view.select("chat", "image_generation")
    assert.equal(view.find(node => node.type === "ModelPriceSourceFields").props.imageGeneration, true)
    view.select("image_generation", "chat")
  }]) {
    const view = harness(data)
    view.find(node => node.type === "ModelPriceSourceFields").props.setDraft(current => ({
      ...current, priceSource: "azure_retail", priceReference: "azure_retail:Azure OpenAI:gpt 4.1:Global:*",
      discountPercent: "80", inputPrice: "1.6",
    }))
    change(view)
    assert.deepEqual(view.find(node => node.type === "ModelPriceSourceFields").props.draft, pricing.createModelPriceDraft())
  }
})

test("image retries keep the current publication and v1.2 credential kind", async () => {
  const publication = { id: "failed-image", status: "failed", publication_kind: "model_add",
    retry_can_authorize_image_probes: true, retry_requires_credential: false }
  const view = harness(registry(), publication)
  const retry = async () => {
    view.find(node => node.type === "Button" && text(node) === "重新发布").props.onClick()
    await Promise.resolve()
  }
  assert.equal(view.find(node => node.props?.id === "authorize-image-probes"), undefined)
  await retry()
  assert.deepEqual(Array.from(view.writes[0]), [publication.id, undefined, "api_key"])
  view.props.publicationId = "another-publication"
  await retry()
  assert.equal(view.writes[1][0], "another-publication")
  const oldBackend = harness(registry(), { ...publication, retry_can_authorize_image_probes: undefined })
  assert.equal(oldBackend.find(node => node.type === "Checkbox" && node.props.id === "authorize-image-probes"), undefined)
})

test("changing a new model identity discards the previous public quote and digest", () => {
  const view = harness(registry(), undefined, undefined, null, true)
  view.input("foundry-deployment", "gpt-5")
  view.render()
  view.find(node => node.type === "ModelPriceSourceFields").props.setDraft(current => ({
    ...current, priceReference: "models_dev:openai:gpt-5", priceEntryDigest: "a".repeat(64),
    inputPrice: "1.25", outputPrice: "10", cacheReadPrice: "0.125", cacheWritePrice: "1.5",
    contextWindow: "400000", contextOrigin: "catalog", allowUnpriced: true,
  }))
  view.input("foundry-deployment", "gpt-6-astra")
  view.render()
  const picker = view.find(node => node.type === "ModelPriceSourceFields")
  assert.equal(picker.props.publicRequest.deployment_name, "gpt-6-astra")
  for (const field of ["priceReference", "priceEntryDigest", "inputPrice", "outputPrice", "cacheReadPrice", "cacheWritePrice", "contextWindow"]) {
    assert.equal(picker.props.draft[field], "", field)
  }
  assert.equal(picker.props.draft.allowUnpriced, false)
})

test("an explicitly unpriced new model sends a null reference instead of an invalid empty string", async () => {
  const view = harness()
  view.input("foundry-deployment", "private-deployment")
  view.find(node => node.type === "ModelPriceSourceFields").props.setDraft(current => ({ ...current, allowUnpriced: true }))
  await view.submit()
  assert.equal(view.writes.length, 1)
  assert.equal(view.writes[0].model.price_reference, null)
  assert.equal(view.writes[0].model.input_cost_per_million, null)
  assert.equal(view.writes[0].model.allow_unpriced, true)
})
