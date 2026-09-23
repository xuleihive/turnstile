import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import test from "node:test"
import { setImmediate as nextTurn } from "node:timers/promises"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import {
  applicationCreateError,
  applicationOperationIdFromUrl,
  applicationOperationTerminal,
  applicationProvisionBlocker,
  applicationProvisionStage,
  applicationSubscriptionIdFromName,
  provisionedApplicationId,
} from "../../../frontend/src/components/applications/application-create-form.ts"
import * as applicationCreateForm from "../../../frontend/src/components/applications/application-create-form.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const entry = fileURLToPath(new URL("../../../frontend/src/components/applications/application-create-dialog.tsx", import.meta.url))
const bundle = await rolldown({ input: entry, external: path => path !== entry, transform: { jsx: { runtime: "automatic" } }, treeshake: false })
let component
try { component = (await bundle.generate({ format: "cjs" })).output[0].code }
finally { await bundle.close() }

function creationHarness() {
  const slots = []
  let cursor = 0
  const requestPending = Promise.withResolvers()
  const requests = []
  const accepted = []
  const clipboard = []
  const closed = []
  const opened = []
  const useSlot = initial => {
    const index = cursor++
    if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
    return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value }]
  }
  const imports = {
    react: { useState: useSlot, useRef: initial => useSlot({ current: initial })[0], useMemo: callback => callback(), useEffect() {} },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "./application-create-form": applicationCreateForm,
    "../../locales": { getIntlLocale: () => "en-US" },
    "../brand-logos": { GatewayBrandLogo: "GatewayBrandLogo", gatewayBrandFromIdentity: value => value },
    "../../data-sources/apim/api": { ApiError: class extends Error {}, dataSource: { provisionGatewayApplicationSubscription: (gatewayId, payload) => { requests.push({ gatewayId, payload }); return requestPending.promise } } },
  }
  const exports = {}
  runInNewContext(component, { exports, navigator: { clipboard: { writeText: async value => { clipboard.push(value) } } }, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  const props = {
    gateways: [{ id: "gateway", name: "Unit gateway", implementation: "apim", enabled: true }],
    inventory: { items: [], provisioning_available: true, provisioning_defaults: { monthly_token_limit: 100000, tokens_per_minute: 100000 } },
    inventoryError: false, applicationType: "service", operation: undefined, operationError: null, refreshing: false,
    onRefresh() {}, onClose: () => closed.push(true), onAccepted: value => accepted.push(value), onOpenApplication: value => opened.push(value),
  }
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === "object" ? [value, ...nodes(value.props?.children)] : []
  const render = () => { cursor = 0; return exports.ApplicationCreateDialog(props) }
  return { props, requests, requestPending, accepted, clipboard, closed, opened, nodes, render, find: predicate => nodes(render()).find(predicate) }
}

const gateway = { id: "gateway", implementation: "apim", enabled: true }
const request = { display_name: "Unit application", subscription_id: "unit-app" }
const applicationId = "00000000-0000-4000-8000-000000000001"

test("subscription ID normalization is locale independent and bounded", () => {
  assert.equal(applicationSubscriptionIdFromName(" Café / INVOICE "), "cafe-invoice")
  assert.equal(applicationSubscriptionIdFromName("中文"), "")
  assert.equal(applicationSubscriptionIdFromName("x".repeat(126) + "--test"), "x".repeat(126))
  assert.equal(applicationSubscriptionIdFromName("x".repeat(150)).length, 127)
})

test("creation requires an enabled APIM gateway and leaves configurable reservations to the API", () => {
  assert.equal(applicationCreateError(gateway, request, []), null)
  for (const candidate of [undefined, { ...gateway, enabled: false }, { ...gateway, implementation: "direct" }]) {
    assert.ok(applicationCreateError(candidate, request, []))
  }
  for (const subscription_id of ["UPPER", "bad_id", "x".repeat(128), "master"]) {
    assert.ok(applicationCreateError(gateway, { ...request, subscription_id }, []))
  }
  for (const subscription_id of ["turnstile-dashboard", "turnstile-publisher-probe"]) {
    assert.equal(applicationCreateError(gateway, { ...request, subscription_id }, []), null)
  }
  assert.ok(applicationCreateError(gateway, { ...request, display_name: " " }, []))
  assert.ok(applicationCreateError(gateway, { ...request, description: "x".repeat(1001) }, []))
})

test("duplicate inventory identity is scoped to the selected gateway", () => {
  const duplicate = { gateway_profile_id: "gateway", slug: "UNIT-APP" }
  assert.ok(applicationCreateError(gateway, request, [duplicate]))
  assert.equal(applicationCreateError({ ...gateway, id: "other" }, request, [duplicate]), null)
})

test("capability remains unavailable until the server explicitly enables it", () => {
  assert.ok(applicationProvisionBlocker(undefined))
  assert.ok(applicationProvisionBlocker({ provisioning_available: false }))
  assert.equal(applicationProvisionBlocker({ provisioning_available: true }), null)
})

test("only successful creation yields an application detail destination", () => {
  const operation = { operation_kind: "application_provision", status: "succeeded", checkpoint: { application_id: applicationId } }
  assert.equal(provisionedApplicationId(operation), applicationId)
  for (const status of ["queued", "promoting", "verifying_readback", "failed"]) {
    assert.equal(provisionedApplicationId({ ...operation, status }), null)
  }
  assert.equal(provisionedApplicationId({ ...operation, operation_kind: "application_sync" }), null)
  assert.equal(provisionedApplicationId({ ...operation, checkpoint: { application_id: "bad" } }), null)
})

test("operation URLs never carry keys and accept UUIDs only", () => {
  assert.equal(applicationOperationIdFromUrl(`https://unit.test/?applicationOperation=${applicationId}`), applicationId)
  assert.equal(applicationOperationIdFromUrl("https://unit.test/?applicationOperation=invalid"), null)
  assert.equal(applicationOperationIdFromUrl("https://unit.test/"), null)
})

test("staged progress distinguishes disabled, terminal and ledger verification", () => {
  assert.equal(applicationOperationTerminal({ status: "verifying_readback" }), false)
  assert.equal(applicationOperationTerminal({ status: "failed" }), true)
  assert.notEqual(applicationProvisionStage({ status: "queued", worker_available: false }), applicationProvisionStage({ status: "queued" }))
  assert.notEqual(applicationProvisionStage({ status: "promoting" }), applicationProvisionStage({ status: "verifying_readback" }))
  assert.notEqual(applicationProvisionStage({ status: "succeeded" }), applicationProvisionStage({ status: "failed" }))
})

test("application create component preserves source help, defaults and dismiss composition", () => {
  const view = creationHarness()
  assert.equal(view.find(node => node.type === "DialogDescription").props.children, "创建独立的 APIM 订阅、调用密钥和应用预算，不是新增一个空白记录。")
  assert.ok(view.find(node => node.type === "FieldHelp" && node.props.children.startsWith("网关内唯一的调用标识")))
  assert.equal(view.find(node => node.type === "label" && node.props.htmlFor === "application-subscription-id").props.children, "订阅 ID")
  assert.equal(view.find(node => node.type === "Input" && node.props.id === "application-subscription-id").props["data-no-localize"], true)
  const defaults = view.find(node => node.props?.className === "application-create-defaults")
  assert.deepEqual(view.nodes(defaults).filter(node => node.type === "dt").map(node => node.props.children), ["创建内容", "初始模型访问", "初始月额度", "初始速率"])
  assert.deepEqual(view.nodes(defaults).filter(node => node.type === "dd").slice(-2).map(node => node.props.children[0]), ["100,000", "100,000"])
  assert.equal(view.find(node => node.props?.className === "application-create-dismiss").props.variant, "outline")
  assert.ok(view.find(node => node.props?.className === "application-create-note"))
  view.props.applicationType = "agent"
  assert.equal(view.find(node => node.type === "DialogTitle").props.children, "添加智能体")
  assert.ok(view.find(node => node.type === "span" && node.props.children === "智能体名称"))
})

test("application blocked state keeps configuration visible without dispatching", async () => {
  const view = creationHarness()
  view.props.inventory = { items: [], provisioning_available: false }
  assert.ok(view.find(node => node.type === "p" && node.props.children === "可以填写配置，但后台就绪前不会提交创建请求。"))
  const defaults = view.find(node => node.props?.className === "application-create-defaults")
  assert.deepEqual(view.nodes(defaults).filter(node => node.type === "dd").slice(-2).map(node => node.props.children[0]), ["使用平台默认值", "使用平台默认值"])
  assert.equal(view.find(node => node.type === "Button" && node.props.type === "submit").props.disabled, true)
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  await nextTurn()
  assert.equal(view.requests.length, 0)
})

test("application component protects the key and suppresses duplicate creation", async () => {
  const view = creationHarness()
  const operation = { id: applicationId, operation_kind: "application_provision", status: "queued", checkpoint: {}, semantic_preview: { apim_subscription_id: "unit-app" } }
  view.find(node => node.type === "Input" && node.props.maxLength === 100).props.onChange({ target: { value: "Unit App" } })
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(view.requests.length, 1)
  assert.equal(view.requests[0].payload.subscription_id, "unit-app")
  assert.equal(view.find(node => node.props?.className === "application-create-dismiss").props.disabled, true)
  view.requestPending.resolve({ primary_key: "unit-key-not-a-real-secret", operation, status_url: `/api/v1/model-management/release-operations/${applicationId}` })
  await nextTurn()
  assert.equal(view.accepted.length, 1)
  assert.equal("primary_key" in view.accepted[0], false)
  assert.equal(JSON.stringify(view.render()).includes("unit-key-not-a-real-secret"), false)
  assert.equal(view.find(node => node.props?.["aria-label"] === "订阅密钥已隐藏").props.children, "\u2022".repeat(32))
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(view.requests.length, 1)
  view.find(node => node.props?.className === "application-create-dismiss").props.onClick()
  assert.equal(view.closed.length, 0)
  assert.ok(view.find(node => node.props?.role === "alert" && node.props.children.startsWith("尚未复制密钥")))
  view.find(node => node.props?.["aria-label"] === "复制订阅密钥").props.onClick()
  await nextTurn()
  assert.deepEqual(view.clipboard, ["unit-key-not-a-real-secret"])
  assert.ok(view.find(node => node.props?.role === "status" && node.props.children === "已复制"))
  view.props.operation = { ...operation, status: "succeeded", checkpoint: { application_id: applicationId } }
  view.find(node => node.type === "Button" && node.props.children === "打开应用详情").props.onClick()
  assert.deepEqual(view.opened, [applicationId])
  assert.equal(view.find(node => node.props?.["aria-label"] === "复制订阅密钥"), undefined)
})

test("failed application creation never displays a usable key control", async () => {
  const view = creationHarness()
  view.find(node => node.type === "Input" && node.props.maxLength === 100).props.onChange({ target: { value: "Unit App" } })
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  view.requestPending.resolve({ primary_key: "unit-key-not-a-real-secret", operation: { id: applicationId, operation_kind: "application_provision", status: "failed", checkpoint: { apim_subscription_created: true }, semantic_preview: { apim_subscription_id: "unit-app" } }, status_url: `/api/v1/model-management/release-operations/${applicationId}` })
  await nextTurn()
  assert.equal(view.find(node => node.props?.["aria-label"] === "复制订阅密钥"), undefined)
  assert.ok(view.find(node => node.type === "p" && node.props.children.startsWith("部分资源已创建")))
  assert.equal(view.requests.length, 1)
})

test("application create defaults and dismiss retain source CSS tokens", () => {
  const stylesheet = frontendRequire("postcss").parse(readFileSync(new URL("../../../frontend/src/styles/applications.css", import.meta.url), "utf8"))
  const declarations = selector => {
    const rules = stylesheet.nodes.filter(node => node.type === "rule" && node.selector === selector)
    assert.equal(rules.length, 1, selector)
    return Object.fromEntries(rules[0].nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))
  }
  const defaults = declarations(".application-create-defaults")
  assert.equal(defaults.background, "var(--muted)")
  assert.equal(defaults.padding, "10px 12px")
  assert.equal(defaults.gap, "10px")
  assert.equal(defaults["border-radius"], "8px")
  assert.equal(defaults["grid-template-columns"], "repeat(2, minmax(0, 1fr))")
  assert.deepEqual(declarations(".application-create-form .registry-editor-footer > .application-create-dismiss"), { "border-color": "var(--border)", background: "var(--background)", color: "var(--foreground)" })
})

for (const [locale, exportName] of [["en", "ENGLISH_CORE_PHRASES"], ["ja", "JAPANESE_CORE_PHRASES"], ["ko", "KOREAN_CORE_PHRASES"]]) {
  test(`application creation source help and defaults are localized in ${locale}`, async () => {
    const phrases = (await import(new URL(`../../../frontend/src/locales/${locale}/phrases-core.ts`, import.meta.url)))[exportName]
    for (const phrase of ["创建独立的 APIM 订阅、调用密钥和应用预算，不是新增一个空白记录。", "可以填写配置，但后台就绪前不会提交创建请求。", "网关内唯一的调用标识；只能使用小写字母、数字和连字符。名称为中文时请单独填写。", "创建内容", "独立订阅与密钥", "使用平台默认值", "创建后可在应用详情调整预算和模型访问。提交将写入共享 APIM 和应用数据库。"]) {
      assert.ok(phrases[phrase], phrase)
      assert.notEqual(phrases[phrase], phrase)
    }
  })
}