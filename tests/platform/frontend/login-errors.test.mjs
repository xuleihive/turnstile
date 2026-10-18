import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))

async function compile(path) {
  const entry = fileURLToPath(new URL(path, import.meta.url))
  const bundle = await rolldown({
    input: entry,
    external: name => name !== entry,
    transform: {
      jsx: { runtime: "automatic" },
      define: { "import.meta.env": "{}", "import.meta.hot": "__testHot" },
    },
    treeshake: false,
  })
  try { return (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
}

const clientCode = await compile("../../../frontend/src/api/client.ts")
const authCode = await compile("../../../frontend/src/api/auth.ts")
const pageCode = await compile("../../../frontend/src/pages/login-page.tsx")

function harness(fetch) {
  const client = {}
  runInNewContext(clientCode, { exports: client, fetch, Error, TypeError })
  const auth = {}
  runInNewContext(authCode, {
    exports: auth, fetch, Error, TypeError,
    require: name => { assert.equal(name, "./client"); return client },
  })
  const slots = []
  let cursor = 0
  const initial = ["owner@example.test", "unit-test-password", false, null, ""]
  const imports = {
    react: { useState() {
      const index = cursor++
      if (!(index in slots)) slots[index] = initial[index]
      return [slots[index], value => { slots[index] = value }]
    } },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "../api/client": client,
    "../providers/auth-provider": { useAuth: () => ({
      signInWithPassword: auth.authApi.signInWithPassword,
      entraError: null,
    }) },
  }
  const page = {}
  runInNewContext(pageCode, {
    exports: page, Error, TypeError,
    require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }),
  })
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
    : value && typeof value === "object" ? [value, ...nodes(value.props?.children)] : []
  const render = () => { cursor = 0; return nodes(page.LoginPage()) }
  return { auth: auth.authApi, client, render, slots }
}

const cases = [
  [401, "邮箱或密码不正确，请重新输入。"],
  [403, "Origin is not allowed"],
  [422, "登录失败，请重试。"],
  [500, "登录服务暂时不可用，请稍后重试。"],
  [502, "登录服务暂时不可用，请稍后重试。"],
  [504, "登录服务暂时不可用，请稍后重试。"],
]

for (const [status, expected] of cases) {
  test(`password login reports HTTP ${status} without misclassifying the failure`, async () => {
    const view = harness(async () => status >= 500
      ? new Response("Upstream unavailable", { status })
      : Response.json({ detail: status === 422 ? [] : "Origin is not allowed" }, { status }))
    await assert.rejects(view.auth.signInWithPassword("unit", "unit"), error =>
      error instanceof view.client.ApiError && error.status === status)
    const form = view.render().find(node => node.type === "form")
    await form.props.onSubmit({ preventDefault() {} })
    assert.equal(view.render().find(node => node.props?.role === "alert").props.children, expected)
    assert.equal(view.slots[1], "unit-test-password")
    assert.equal(view.slots[3], null)
  })
}

test("a network failure is not reported as an invalid password", async () => {
  const view = harness(async () => { throw new TypeError("Failed to fetch") })
  await view.render().find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(view.render().find(node => node.props?.role === "alert").props.children,
    "无法连接登录服务，请检查网络后重试。")
  assert.equal(view.slots[3], null)
})

test("successful password authentication returns the real response profile", async () => {
  const profile = { id: "unit-user", email: "owner@example.test", role: "owner", method: "password" }
  const view = harness(async (path, init) => {
    assert.equal(path, "/api/v1/auth/login")
    assert.equal(init.credentials, "include")
    return Response.json(profile)
  })
  assert.deepEqual(await view.auth.signInWithPassword("owner@example.test", "unit"), profile)
  await view.render().find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(view.render().some(node => node.props?.role === "alert"), false)
})

test("authentication context remains shared across hot module replacements", async () => {
  const code = await compile("../../../frontend/src/providers/auth-provider.tsx")
  const React = frontendRequire("react")
  const value = { status: "authenticated", user: { id: "unit-owner" } }
  let providerContext
  const imports = {
    react: { ...React, useContext: context => context === providerContext ? value : null },
    "@azure/msal-browser": { PublicClientApplication: class {} },
    "../api/auth": { authApi: {} },
    "../api/client": { SESSION_EXPIRED_EVENT: "turnstile:session-expired" },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
  }
  const load = hot => {
    const exports = {}
    runInNewContext(code, {
      exports, require: name => imports[name], __testHot: hot,
      window: { location: { origin: "http://localhost:5174" } },
    })
    return exports
  }
  const hot = { data: {} }
  const before = load(hot)
  providerContext = hot.data.authContext
  assert.ok(providerContext)
  assert.equal(before.useAuth(), value)
  const after = load(hot)
  assert.equal(hot.data.authContext, providerContext)
  assert.equal(after.useAuth(), value)
  assert.equal(before.useAuth(), value)
  const separate = { data: {} }
  load(separate)
  assert.notEqual(separate.data.authContext, providerContext)
  assert.throws(() => load(undefined).useAuth(), /AuthProvider/)
})

test("entry hot updates reuse one React root and query client", async () => {
  const code = await compile("../../../frontend/src/main.tsx")
  const container = {}
  let roots = 0
  let clients = 0
  let renders = 0
  const imports = {
    react: frontendRequire("react"),
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "react-dom/client": { createRoot(target) {
      assert.equal(target, container)
      roots++
      return { render() { renders++ } }
    } },
    "@tanstack/react-query": {
      QueryClient: class { constructor() { clients++ } },
      QueryClientProvider: "QueryClientProvider",
    },
    "./styles.css": {},
  }
  const load = hot => runInNewContext(code, {
    require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }),
    __testHot: hot,
    URLSearchParams,
    sessionStorage: { getItem: () => null },
    window: { location: { hostname: "localhost", search: "" } },
    document: { getElementById: id => { assert.equal(id, "root"); return container } },
  })
  const hot = { data: {} }
  load(hot)
  const originalRoot = hot.data.root
  const originalClient = hot.data.queryClient
  load(hot)
  assert.ok(originalRoot)
  assert.ok(originalClient)
  assert.equal(hot.data.root, originalRoot)
  assert.equal(hot.data.queryClient, originalClient)
  assert.deepEqual({ roots, clients, renders }, { roots: 1, clients: 1, renders: 2 })
  load(undefined)
  assert.deepEqual({ roots, clients, renders }, { roots: 2, clients: 2, renders: 3 })
})
