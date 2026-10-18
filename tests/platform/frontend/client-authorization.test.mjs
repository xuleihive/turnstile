import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath, pathToFileURL } from "node:url"
import vm from "node:vm"

const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(pathToFileURL(require.resolve("rolldown")).href)

async function compile(path) {
  const entry = fileURLToPath(new URL(`../../../frontend/src/${path}`, import.meta.url))
  const bundle = await rolldown({ input: entry, external: id => id !== entry, treeshake: false,
    transform: { jsx: { runtime: "automatic" }, define: { "import.meta.env.VITE_API_BASE_URL": '""' } },
  })
  try { return (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
}
const clientCode = await compile("api/client.ts")
const pageCode = await compile("pages/client-authorization-page.tsx")

function load(code, require, globals = {}) {
  const exports = {}
  const module = { exports }
  vm.runInNewContext(code, { exports, module, require, AbortController, Error, ...globals })
  return module.exports
}

function render(purpose, { fail = false, state = "pending" } = {}) {
  const calls = [], states = [], removed = []
  const client = load(clientCode, null, {
    fetch: async (url, init) => {
      calls.push({ url, init })
      // Match the browser's string-body default, which FastAPI does not parse as JSON.
      const contentType = new Headers(init.headers).get("content-type") ?? "text/plain;charset=UTF-8"
      return contentType === "application/json" && !fail
        ? Response.json({ status: "approved" })
        : Response.json({ detail: "Invalid authorization request body" }, { status: 422 })
    },
  })
  const selected = { application_id: "app-1", subscription_id: "sub-1", gateway_id: "gateway-1", gateway_name: "APIM", models: [{ id: "model-1" }] }
  const jsx = (type, props) => ({ type, props })
  const page = load(pageCode, name => {
    if (name === "react") return {
      useEffect: () => {}, useRef: value => ({ current: value }),
      useState: value => {
        const index = states.length; states.push(value)
        return [value, next => { states[index] = next }]
      },
    }
    if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "fragment" }
    if (name === "@tanstack/react-query") return { useQuery: () => ({
      data: { purpose, state, subscription: selected },
    }) }
    if (name === "../api/client") return client
    if (name === "../providers/auth-provider") return { useAuth: () => ({ user: { id: "user-1" } }) }
    if (name === "../components/ui/button") return { Button: "button" }
    if (name === "lucide-react") return { ShieldCheck: "icon" }
    throw new Error(`Unexpected import ${name}`)
  }, { sessionStorage: { removeItem: key => removed.push(key) } })
  const nodes = []
  function visit(node) {
    if (Array.isArray(node)) { node.forEach(visit); return }
    if (!node || typeof node !== "object") return
    nodes.push(node); visit(node.props?.children)
  }
  visit(page.ClientAuthorizationPage({ code: "ABCD-EFGH" }))
  return { calls, states, removed, selected, nodes }
}

for (const purpose of ["login", "import", "reauth"]) {
  for (const approve of [true, false]) {
    test(`${purpose} ${approve ? "approval" : "denial"} sends JSON through the actual request client`, async () => {
      const view = render(purpose)
      if (approve) {
        let prevented = false
        view.nodes.find(node => node.type === "form").props.onSubmit({ preventDefault: () => { prevented = true } })
        assert.equal(prevented, true)
      } else {
        view.nodes.find(node => node.type === "button" && node.props.type === "button").props.onClick()
      }
      await new Promise(resolve => setImmediate(resolve))
      assert.equal(view.calls.length, 1)
      const { url, init } = view.calls[0]
      assert.equal(url, "/api/v1/client-auth/decision")
      assert.equal(init.method, "POST")
      assert.equal(new Headers(init.headers).get("content-type"), "application/json")
      assert.equal(init.credentials, "include")
      assert.equal(init.cache, "no-store")
      assert.equal(init.signal.aborted, false)
      assert.deepEqual(JSON.parse(init.body), {
        user_code: "ABCD-EFGH", approve,
      })
      assert.equal(view.states[0], false)
      assert.equal(view.states[1], approve
        ? "验证已完成，请回到客户端。插件将自动接收结果。"
        : "已拒绝授权，原有有效配置不变。")
      assert.equal(view.states[2], "")
      assert.equal(view.nodes.filter(node => node.type === "select").length, 0)
      assert.deepEqual(view.removed, ["turnstile-client-authorization"])
    })
  }
}

test("pending and processed authorization instructions use generic client wording", () => {
  const pending = render("login")
  assert.ok(pending.nodes.some(node => node.type === "p"
    && node.props.children === "只确认你本人刚从客户端发起的请求。"))
  const processed = render("login", { state: "approved" })
  assert.ok(processed.nodes.some(node => node.props.role === "status"
    && node.props.children === "这次申请已处理，请回到客户端或重新发起验证。"))
  assert.equal(processed.nodes.some(node => node.type === "form"), false)
})

test("a failed decision retains the pending authorization and reports an error", async () => {
  const view = render("login", { fail: true })
  view.nodes.find(node => node.type === "form").props.onSubmit({ preventDefault: () => {} })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(view.states[0], false)
  assert.equal(view.states[1], "")
  assert.match(view.states[2], /API 422/)
  assert.deepEqual(view.removed, [])
})
