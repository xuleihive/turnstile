import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import { isOpenAICompatibleBaseUrl } from "../../../frontend/src/components/model-management/openai-compatible.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
async function componentExports(path, expose = []) {
  const entry = fileURLToPath(new URL(path, import.meta.url))
  const bundle = await rolldown({ input: entry, external: id => id !== entry,
    transform: { jsx: { runtime: "automatic" } }, treeshake: false,
    plugins: expose.length ? [{ name: "test-component-exports", load(id) {
      return id === entry ? `${readFileSync(entry, "utf8")}\nexport { ${expose.join(", ")} }` : null
    } }] : [],
  })
  const exports = {}
  try {
    runInNewContext((await bundle.generate({ format: "cjs" })).output[0].code, {
      exports, require: name => name === "react/jsx-runtime" ? frontendRequire(name) : new Proxy({}, { get: (_target, key) => key }),
    })
    return exports
  } finally { await bundle.close() }
}
const { modelVendorFromMetadata, modelVendorLabel } = await componentExports("../../../frontend/src/components/model-management/model-vendor-select.tsx")
const { displayNameFromProviderId, publicModelKeyFromProviderId, publicationErrorMessage } = await componentExports(
  "../../../frontend/src/components/model-management/model-publication-dialog.tsx",
  ["displayNameFromProviderId", "publicModelKeyFromProviderId", "publicationErrorMessage"],
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

test("publication compatibility errors use the current pricing schema without exposing inputs", () => {
  const detail = [{ type: "extra_forbidden", loc: ["body", "model", "price_source"], input: "secret", msg: "Extra inputs are not permitted" }]
  assert.equal(publicationErrorMessage(`API 422: ${JSON.stringify({ detail })}`), "\u524d\u540e\u7aef\u8ba1\u4ef7\u63a5\u53e3\u7248\u672c\u4e0d\u4e00\u81f4\uff0c\u8bf7\u66f4\u65b0\u540e\u7aef\u540e\u518d\u53d1\u5e03\u3002")
  assert.equal(publicationErrorMessage("API 403: Access denied"), "API 403: Access denied")
})
