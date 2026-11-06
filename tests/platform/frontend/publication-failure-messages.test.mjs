import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import * as connections from "../../../frontend/src/components/model-management/model-publication-connections.ts"
import * as pricing from "../../../frontend/src/components/model-management/model-edit-form.ts"
import * as gatewayTopology from "../../../frontend/src/lib/gateway-topology.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const primitives = new Proxy({}, { get: (_target, name) => name })

async function compile(path, additions = "") {
  const entry = fileURLToPath(new URL(path, import.meta.url))
  const bundle = await rolldown({ input: entry, external: identity => identity !== entry,
    transform: { jsx: { runtime: "automatic" } }, treeshake: false })
  try { return (await bundle.generate({ format: "cjs" })).output[0].code + additions }
  finally { await bundle.close() }
}

const dialogCode = await compile("../../../frontend/src/components/model-management/model-publication-dialog.tsx")
const releaseCode = await compile("../../../frontend/src/pages/gateway-releases-page.tsx",
  "\nexports.failureNotice = PublicationFailureNotice;\n")
const localeEntry = fileURLToPath(new URL("../../../frontend/src/locales/provider.tsx", import.meta.url))
const localeBundle = await rolldown({ input: localeEntry,
  external: ["react", "react/jsx-runtime"], transform: { jsx: { runtime: "automatic" } } })
const locales = {}
try {
  const compiled = (await localeBundle.generate({ format: "cjs", inlineDynamicImports: true })).output[0].code
  runInNewContext(compiled, { exports: locales, require: frontendRequire })
} finally { await localeBundle.close() }

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

function messages(failure, retryRequiresCredential = false, publicationKind = "model_add") {
  const publication = {
    id: "unit-publication", gateway_profile_id: "unit-gateway", status: "failed",
    publication_kind: publicationKind, model_key: "unit-model", display_name: "Unit model",
    retry_requires_credential: retryRequiresCredential, failure,
    error_message: "private-upstream-body diagnostic-test-secret ciphertext",
  }
  const dialog = {}
  const hooks = {
    useState: initial => [typeof initial === "function" ? initial() : initial, () => {}],
    useRef: initial => ({ current: initial }), useEffect() {},
  }
  const imports = {
    react: hooks, "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@tanstack/react-query": { useQuery: () => ({ data: publication }), useQueryClient: () => ({}) },
    "../../data-sources/apim/queries": { finopsQueries: {
      gatewayPublication: () => ({}), gatewayProgramUpgradePreview: () => ({}),
    } },
    "../../lib/gateway-topology": gatewayTopology,
    "../../locales/index": { useLocale: () => ({ locale: "zh-CN" }) },
    "./model-publication-connections": connections, "./model-edit-form": pricing,
  }
  runInNewContext(dialogCode, { exports: dialog, require: name => imports[name] ?? primitives })
  const tree = dialog.ModelPublicationDialog({
    registry: { gateways: [], runtimes: [], providers: [], models: [] },
    publicationId: publication.id, onClose() {}, onManageConnections() {}, onPublicationQueued() {},
  })
  const dialogMessage = text(nodes(tree).find(node => node.props?.className === "publication-safety-error"))
  const release = {}
  runInNewContext(releaseCode, { exports: release, require: name => name === "react/jsx-runtime"
    ? frontendRequire(name) : name === "../components/model-management/model-publication-dialog" ? dialog : primitives })
  const notice = release.failureNotice({ failure: failure ?? {
    code: "publication_failed", missing_actions: [],
  }, publicationId: publication.id })
  const releaseMessage = text(nodes(notice).find(node => node.type === "p"))
  return { dialogMessage, releaseMessage, tree, owner: dialog.publicationFailureMessage }
}

function failure(code, overrides = {}) {
  return { code, message: "private-upstream-body diagnostic-test-secret ciphertext",
    stage: "provisioning", http_status: null, request_id: null, missing_actions: [], ...overrides }
}

const cases = [
  ...["publisher_permissions_missing", "publisher_permissions_unverified", "publisher_authorization_failed",
    "parent_policy_changed", "credential_source_missing", "credential_unreadable", "credential_vault_unavailable",
    "credential_vault_access_denied", "credential_name_conflict", "deployment_not_found",
    "deployment_metadata_unavailable", "upstream_request_failed", "apim_infrastructure_upgrade_required",
    "publication_failed", "unknown-private-code", "__proto__", "toString"].map(code => failure(code)),
  ...["credential_source_missing", "credential_unreadable"].flatMap(code =>
    ["retry_with_key", "republish_with_key", "administrator", "administrator_republish"].map(credential_recovery => failure(code, { credential_recovery }))),
  ...["openai-images", "microsoft-decision", "openai-v1"].map(profile => failure("gateway_capability_unavailable", { profile })),
  undefined,
]

test("generic removal failure uses a task message rather than a publication message", () => {
  const result = messages(failure("publication_failed"), false, "model_remove")
  assert.equal(result.dialogMessage, "任务未完成，请使用任务 ID 查询诊断记录。")
  assert.equal(result.releaseMessage, result.dialogMessage)
  assert.equal(text(result.tree).includes("发布未完成"), false)
})

test("dialog and release details use the same safe owner for every code and recovery state", () => {
  for (const candidate of cases) {
    const result = messages(candidate)
    assert.ok(result.dialogMessage)
    assert.equal(result.dialogMessage, result.releaseMessage, candidate?.code)
    assert.equal(result.dialogMessage, result.owner(candidate))
    assert.doesNotMatch(result.dialogMessage, /private-upstream-body|diagnostic-test-secret|ciphertext/)
  }
})

test("credential recovery never offers replacement-key retry for immutable model additions", () => {
  for (const code of ["credential_source_missing", "credential_unreadable"]) {
    for (const recovery of ["republish_with_key", "administrator", "administrator_republish"]) {
      const result = messages(failure(code, { credential_recovery: recovery }))
      assert.equal(nodes(result.tree).some(node => node.props?.id === "retry-api-key"), false)
      assert.doesNotMatch(result.dialogMessage, /轮换|轮转|rotation|重新提供 API Key 后重试/)
      assert.match(result.dialogMessage, recovery === "republish_with_key" ? /新的发布任务.*原任务重试不能/ : /恢复原凭据及其读取权限/)
      if (recovery === "administrator_republish") assert.match(result.dialogMessage, /原连接提交新发布任务/)
    }
    const result = messages(failure(code, { credential_recovery: "retry_with_key" }), true)
    assert.equal(nodes(result.tree).some(node => node.props?.id === "retry-api-key"), true)
  }
})

test("administrator recovery does not promise original route-reconciliation retry in any locale", async () => {
  for (const code of ["credential_source_missing", "credential_unreadable"]) {
    const result = messages(failure(code, { credential_recovery: "administrator" }), false, "route_reconcile")
    assert.equal(result.dialogMessage, "连接的受管凭据缺失或无法读取，请联系部署管理员恢复原凭据及其读取权限。")
    assert.equal(result.dialogMessage, result.releaseMessage)
    assert.equal(nodes(result.tree).some(node => node.props?.id === "retry-api-key"), false)
    for (const locale of ["zh-CN", "en", "zh-TW", "ja", "ko"]) {
      const translated = await locales.translateForLocale(result.dialogMessage, locale)
      assert.equal(translated, await locales.translateForLocale(result.releaseMessage, locale))
      assert.doesNotMatch(translated, /重试|重試|retry|再試行|다시 시도/i, locale)
      assert.doesNotMatch(translated, /private-upstream-body|diagnostic-test-secret|ciphertext/)
    }
  }
})

test("the actual locale converters preserve matching safe messages in Chinese and all four conversions", async () => {
  for (const candidate of cases) {
    const { dialogMessage, releaseMessage } = messages(candidate)
    for (const locale of ["zh-CN", "en", "zh-TW", "ja", "ko"]) {
      const translated = await locales.translateForLocale(dialogMessage, locale)
      assert.equal(translated, await locales.translateForLocale(releaseMessage, locale))
      assert.doesNotMatch(translated, /private-upstream-body|diagnostic-test-secret|ciphertext/)
      if (locale === "zh-CN") assert.equal(translated, dialogMessage)
      else assert.notEqual(translated, dialogMessage, `${locale}: ${candidate?.code}`)
    }
  }
})