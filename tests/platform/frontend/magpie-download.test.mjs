import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import vm from "node:vm"

const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(require.resolve("rolldown"))
const entry = fileURLToPath(new URL("../../../frontend/src/lib/magpie-download.ts", import.meta.url))
const bundle = await rolldown({ input: entry })
let code
try { code = (await bundle.generate({ format: "cjs" })).output[0].code }
finally { await bundle.close() }
const module = { exports: {} }
vm.runInNewContext(code, { module, exports: module.exports })
const { magpieDownload, detectMagpieDownload, MAGPIE_DOWNLOAD_PAGE } = module.exports

for (const [name, browser, architecture, path, label] of [
  ["Mac Apple Silicon hints", { userAgent: "Macintosh; Intel Mac OS X" }, "arm", "download/mac-arm64", "macOS"],
  ["Mac Intel hints", { userAgent: "Macintosh; Intel Mac OS X" }, "x86", "download/mac-intel", "macOS"],
  ["Windows default x64", { userAgent: "Windows NT 10.0; Win64; x64" }, undefined, "download/windows-amd64", "Windows"],
  ["Windows ARM UA", { userAgent: "Windows NT 10.0; ARM64" }, undefined, "download/windows-arm64", "Windows"],
  ["Windows ARM hints", { userAgent: "Windows NT 10.0; Win64; x64" }, "arm", "download/windows-arm64", "Windows"],
  ["hints override ARM UA", { userAgent: "Windows NT 10.0; ARM64" }, "x86", "download/windows-amd64", "Windows"],
  ["Linux x64", { userAgent: "X11; Linux x86_64" }, undefined, "download/linux-amd64", "Linux"],
  ["Linux ARM UA", { userAgent: "X11; Linux aarch64" }, undefined, "download/linux-arm64", "Linux"],
  ["Linux ARM hints", { userAgent: "X11; Linux x86_64" }, "arm", "download/linux-arm64", "Linux"],
  ["platform hints", { userAgent: "Reduced UA", userAgentData: { platform: "Windows" } }, "arm", "download/windows-arm64", "Windows"],
]) {
  test(`official current-release link: ${name}`, () => {
    const result = magpieDownload(browser, architecture)
    assert.equal(result.href, `https://usemagpie.ai/${path}`)
    assert.equal(result.label, `下载 ${label} 客户端`)
    assert.equal(/releases\/download\/v/.test(result.href), false)
  })
}

for (const [name, browser] of [
  ["server", undefined], ["unknown", { userAgent: "Unknown OS" }],
  ["Android", { userAgent: "Linux; Android 15; arm64" }],
  ["iPhone", { userAgent: "iPhone; CPU iPhone OS 18 like Mac OS X" }],
  ["desktop-mode iPad", { userAgent: "Macintosh; Intel Mac OS X", maxTouchPoints: 5 }],
  ["mobile hints", { userAgent: "Windows", userAgentData: { mobile: true } }],
]) {
  test(`unsupported systems open the official selector: ${name}`, () => {
    assert.equal(magpieDownload(browser).href, MAGPIE_DOWNLOAD_PAGE)
    assert.equal(magpieDownload(browser).label, "下载客户端")
  })
}

test("architecture refinement uses optional client hints without a server request", async () => {
  const requested = []
  const result = await detectMagpieDownload({ userAgent: "Windows NT 10.0", userAgentData: {
    getHighEntropyValues: async hints => { requested.push(...hints); return { architecture: "arm" } },
  } })
  assert.deepEqual(requested, ["architecture"])
  assert.equal(result.href, "https://usemagpie.ai/download/windows-arm64")
})

test("missing or rejected hints retain the official OS fallback", async () => {
  assert.equal((await detectMagpieDownload({ userAgent: "Macintosh" })).href, MAGPIE_DOWNLOAD_PAGE)
  const result = await detectMagpieDownload({ userAgent: "Windows NT 10.0", userAgentData: {
    getHighEntropyValues: async () => { throw new Error("blocked by browser policy") },
  } })
  assert.equal(result.href, "https://usemagpie.ai/download/windows-amd64")
})

test("a Mac UA cannot distinguish Intel from Apple Silicon without architecture hints", () => {
  const result = magpieDownload({ userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" })
  assert.equal(result.href, MAGPIE_DOWNLOAD_PAGE)
  assert.equal(result.label, "下载 macOS 客户端")
})
