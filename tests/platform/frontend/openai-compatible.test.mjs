import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import { isOpenAICompatibleBaseUrl } from "../../../frontend/src/components/model-management/openai-compatible.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
async function helpers(relativePath, expose = []) {
  const entry = fileURLToPath(new URL(relativePath, import.meta.url))
  const bundle = await rolldown({ input: entry, external: identity => identity !== entry,
    transform: { jsx: { runtime: "automatic" } }, treeshake: false,
    plugins: expose.length ? [{ name: "test-current-helper-exports", load(identity) {
      if (identity === entry) return `${readFileSync(entry, "utf8")}\nexport { ${expose.join(", ")} }`
    } }] : [],
  })
  try {
    const exports = {}
    const code = (await bundle.generate({ format: "cjs" })).output[0].code
    runInNewContext(code, { exports, require: () => new Proxy({}, { get: (_target, key) => key }) })
    return exports
  } finally { await bundle.close() }
}
const { displayNameFromProviderId, publicModelKeyFromProviderId, publicationErrorMessage } = await helpers(
  "../../../frontend/src/components/model-management/model-publication-dialog.tsx",
  ["displayNameFromProviderId", "publicModelKeyFromProviderId", "publicationErrorMessage"],
)
const { modelVendorFromMetadata, modelVendorLabel } = await helpers(
  "../../../frontend/src/components/model-management/model-vendor-select.tsx",
)

test("compatible endpoints accept HTTPS bases and full chat paths only without URL credentials", () => {
  for (const value of ["https://api.example.test/v1", " https://api.example.test/v1/chat/completions "]) {
    assert.equal(isOpenAICompatibleBaseUrl(value), true)
  }
  for (const value of ["", "/v1", "http://api.example.test", "https://user:value@api.example.test",
    "https://api.example.test/v1?key=sample", "https://api.example.test/#fragment"]) {
    assert.equal(isOpenAICompatibleBaseUrl(value), false)
  }
})

test("public aliases are normalized without changing the provider model ID", () => {
  const providerId = " Team/Model_V2 "
  assert.equal(publicModelKeyFromProviderId(providerId), "team-model_v2")
  assert.equal(providerId, " Team/Model_V2 ")
  assert.equal(publicModelKeyFromProviderId("///"), "")
  assert.equal(publicModelKeyFromProviderId("gpt-4.1:mini"), "gpt-4.1:mini")
})

test("display names use the model leaf while preserving recognizable vendor names", () => {
  assert.equal(displayNameFromProviderId("team/deepseek-v3.2"), "DeepSeek V3.2")
  assert.equal(displayNameFromProviderId("gpt-4.1"), "GPT 4.1")
  assert.equal(displayNameFromProviderId("team/kimi-k2"), "Kimi K2")
  assert.equal(displayNameFromProviderId(""), "")
})

test("explicit vendor metadata wins over a display-name guess", () => {
  assert.equal(modelVendorFromMetadata({ model_vendor: "generic" }, "OpenAI compatible GPT"), "generic")
  assert.equal(modelVendorFromMetadata({ model_vendor: "kimi" }, "DeepSeek"), "kimi")
  assert.equal(modelVendorFromMetadata(undefined, "OpenAI-compatible"), "generic")
  assert.equal(modelVendorFromMetadata(undefined, "moonshot.example.test"), "kimi")
  assert.equal(modelVendorFromMetadata(undefined, "DeepSeek connection"), "deepseek")
  assert.equal(modelVendorLabel("anthropic"), "Anthropic")
})

test("current publication validation reports provider fields without retaining raw input", () => {
  const detail = [
    { type: "literal_error", loc: ["body", "provider", "template"], msg: "Unsupported provider", input: "private-input" },
    { type: "extra_forbidden", loc: ["body", "openai_base_url"], msg: "Extra field", input: "private-input" },
  ]
  const message = publicationErrorMessage(`API 422: ${JSON.stringify({ detail })}`)
  assert.equal(message, "provider.template: Unsupported provider\nopenai_base_url: Extra field")
  assert.equal(message.includes("private-input"), false)
  assert.equal(publicationErrorMessage('API 409: {"detail":"Connection already exists"}'), "Connection already exists")
  assert.equal(publicationErrorMessage("Network unavailable"), "Network unavailable")
})

test("a publication conflict shows only the server's reason", () => {
  // The API client puts a string detail after the status, not the JSON body.
  const reason = "The model is already published on gateway Unit. Gateways share one model record, so it can have only one price; different: context_price_rule"
  assert.equal(publicationErrorMessage(`API 409: ${reason}`), reason)
  assert.equal(publicationErrorMessage("API 503: Gateway unavailable"), "API 503: Gateway unavailable")
})