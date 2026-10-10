import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire, stripTypeScriptTypes } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import * as connections from "../../../frontend/src/components/model-management/model-publication-connections.ts"
import * as compatible from "../../../frontend/src/components/model-management/openai-compatible.ts"
import * as modelEditForm from "../../../frontend/src/components/model-management/model-edit-form.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
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
  return [value, ...nodes(value.props?.children)]
}

function text(value) {
  if (Array.isArray(value)) return value.map(text).join("")
  if (value && typeof value === "object") return text(value.props?.children)
  return value == null || typeof value === "boolean" ? "" : String(value)
}

function harness(rawRegistry = registry(), publication) {
  const slots = []
  let cursor = 0
  const writes = []
  const queued = []
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
    useEffect() {},
  }
  const queryClient = { setQueryData() {}, invalidateQueries() {} }
  const exports = {}
  const imports = {
    react: hooks, "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@tanstack/react-query": { useQuery: () => ({ data: publication }), useQueryClient: () => queryClient },
    "../../data-sources/apim/api": { dataSource: {
      publishModel: async request => { writes.push(request); return { publication: { id: "accepted" } } },
      retryGatewayPublication: async (...args) => { writes.push(args); return { id: publication.id } },
    } },
    "../../data-sources/apim/queries": { finopsKeys: { gatewayPublication: id => [id] }, finopsQueries: { gatewayPublication: () => ({}) } },
    "./model-publication-connections": connections, "./openai-compatible": compatible,
    "./model-edit-form": modelEditForm,
    "./public-model-pricing": {
      PublicModelPricing: "PublicModelPricing",
      emptyPricingDraft: () => ({
        priceSource: "models_dev", priceReference: "", priceEntryDigest: "",
        allowUnpriced: false, discountPercent: "", contextWindow: "",
        inputPrice: "", outputPrice: "", cacheReadPrice: "", cacheWritePrice: "",
      }),
    },
    "../brand-logos": { GatewayBrandLogo: "GatewayBrandLogo", ProviderBrandLogo: "ProviderBrandLogo", gatewayBrandFromIdentity: () => "generic", providerBrandFromMetadata: () => "generic" },
  }
  runInNewContext(compiled, { exports, require: name => imports[name] ?? primitives })
  const render = () => { cursor = 0; return exports.ModelPublicationDialog(props) }
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
    find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
    await Promise.resolve()
  }
  return { props, writes, queued, render, find, input, select, submit }
}

test("normalized backend image capability controls the real component option", () => {
  for (const [enabled, version, disabled] of [[true, 4, false], [false, 4, true], [undefined, 4, true], [true, 3, true]]) {
    const view = harness(registry({ image_generation_supported: enabled, image_configuration_schema_version: version }))
    assert.equal(view.find(node => node.type === "SelectItem" && node.props.value === "image_generation").props.disabled, disabled)
  }
})

test("public pricing is default and its confirmed mapping accompanies publication", async () => {
  const view = harness()
  view.input("foundry-deployment", "gpt-5")
  const pricing = view.find(node => node.type === "PublicModelPricing")
  assert.equal(pricing.props.draft.priceSource, "models_dev")
  pricing.props.onChange(current => ({
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
  view.find(node => node.type === "PublicModelPricing").props.onChange(current => ({
    ...current, inputPrice: "1.25", outputPrice: "10",
  }))
  view.find(node => node.type === "input" && node.props.value === "manual").props.onChange()
  assert.equal(view.find(node => node.type === "details" && node.props.className === "simple-pricing").props.open, true)
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
  assert.equal(multiple.find(node => node.type === "Button" && node.props.type === "submit").props.disabled, true)
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
    const summary = view.find(node => node.props?.["aria-label"] === "连接信息")
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
  view.find(node => node.type === "input" && node.props.value === "manual").props.onChange()
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
  const pricing = view.find(node => node.type === "PublicModelPricing")
  assert.equal(pricing.props.request.operation, "image_generation")
  assert.equal(pricing.props.draft.priceSource, "models_dev")
  await view.submit()
  assert.equal(view.writes.length, 0)
  pricing.props.onChange(current => ({
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
  view.input("existing-provider-api-key", "unit-key")
  view.input("foundry-deployment", "unit-model")
  view.input("publication-input-price", "5")
  view.select("connection", "second")
  for (const id of ["existing-provider-api-key", "foundry-deployment", "publication-input-price"]) assert.equal(view.find(node => node.type === "Input" && node.props.id === id).props.value, "")
  assert.ok(view.find(node => node.type === "Select" && node.props.value === "chat"))
  assert.ok(text(view.render()).includes("价格未填写"))
})

test("paid image retry consent is opt-in, publication-bound and consumed by each retry", async () => {
  const publication = { id: "failed-image", status: "failed", publication_kind: "model_add",
    retry_can_authorize_image_probes: true, retry_requires_credential: false }
  const view = harness(registry(), publication)
  const consent = () => view.find(node => node.type === "Checkbox" && node.props.id === "authorize-image-probes")
  const retry = async () => {
    view.find(node => node.type === "Button" && text(node) === "重新发布").props.onClick()
    await Promise.resolve()
  }
  assert.equal(consent().props.checked, false)
  await retry()
  assert.equal(view.writes[0][2], false)
  consent().props.onCheckedChange(true)
  await retry()
  assert.equal(view.writes[1][2], true)
  assert.equal(consent().props.checked, false)
  consent().props.onCheckedChange(true)
  view.props.publicationId = "another-publication"
  await retry()
  assert.equal(view.writes[2][2], false)
  const oldBackend = harness(registry(), { ...publication, retry_can_authorize_image_probes: undefined })
  assert.equal(oldBackend.find(node => node.type === "Checkbox" && node.props.id === "authorize-image-probes"), undefined)
})
