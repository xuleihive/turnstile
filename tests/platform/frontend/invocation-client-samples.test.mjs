import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import { clientSampleFor, gatewayApiOperations, operationSampleModels } from "../../../frontend/src/lib/application-client-samples.ts"

const endpoint = "https://gateway.example.test/llm"
const headers = { "x-user-id": "person@example.test", "x-project-id": "project-unit" }
const messages = [{ role: "user", content: "User's \"quoted\" prompt\nwith a second line" }]
const cases = [
  { operation: "chat_completions", payload: { model: "direct-model", messages, max_completion_tokens: 2048, stream: false } },
  { operation: "chat_completions", payload: { model: "Unit Router", messages, max_tokens: 512, stream: false } },
  { operation: "decision", payload: { model: "decision-model", state: { service: "checkout", errors: 5 }, questions: { urgent: { type: "noul", instructions: "Escalate?" } } } },
  { operation: "decision", payload: { model: "decision-model", state: "The checkout service is unavailable.", questions: { team: { type: "choice", instructions: "Which team?", criteria: { engineering: "Errors" } } } } },
  { operation: "image_generations", payload: { model: "image-model", prompt: messages[0].content, size: "1024x1024", quality: "low", n: 1, stream: false } },
]

for (const { operation, payload } of cases) {
  test(`JavaScript preserves the current ${payload.model} request without sending it`, async () => {
    const sample = clientSampleFor("javascript", endpoint + "/", payload.model, operation, { payload, headers })
    let captured
    await runInNewContext(`(async () => { ${sample} })()`, {
      process: { env: { TURNSTILE_APIM_KEY: "unit-test-placeholder" } },
      console: { log() {} },
      fetch: async (url, request) => {
        captured = { url, headers: request.headers, payload: JSON.parse(request.body) }
        return { ok: true, json: async () => ({}) }
      },
    })
    assert.equal(captured.url, endpoint + gatewayApiOperations().find(item => item.id === operation).path)
    assert.deepEqual(captured.payload, payload)
    assert.equal(captured.headers["x-user-id"], headers["x-user-id"])
    assert.equal(captured.headers["x-project-id"], headers["x-project-id"])
    assert.equal(captured.headers["Ocp-Apim-Subscription-Key"], "unit-test-placeholder")
    assert.ok(!sample.includes("unit-test-placeholder"))
  })
}

test("shell and Python samples preserve quotes, line breaks, and native payloads", () => {
  const samples = cases.flatMap(({ operation, payload }) => ["bash", "python"].map(language => ({
    language,
    sample: clientSampleFor(language, endpoint, payload.model, operation, { payload, headers }),
    payload,
    headers,
  })))
  const result = spawnSync("uv", ["run", "--frozen", "--offline", "python", "-c", `
import ast, json, shlex, sys
for item in json.load(sys.stdin):
    if item["language"] == "bash":
        arguments = shlex.split(item["sample"])
        actual = json.loads(arguments[arguments.index("--data-raw") + 1])
        for name, value in item["headers"].items():
            assert name + ": " + value in arguments
    else:
        tree = ast.parse(item["sample"])
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign) and node.targets[0].id == "payload")
        actual = json.loads(ast.literal_eval(assignment.value.args[0]))
    assert actual == item["payload"], item["language"]
`], { cwd: new URL("../../../", import.meta.url), input: JSON.stringify(samples), encoding: "utf8" })
  assert.equal(result.status, 0, result.stderr)
})

test("all existing subscription operations retain their endpoints and key variable", () => {
  for (const language of ["bash", "python", "javascript"]) {
    for (const operation of gatewayApiOperations()) {
      const sample = clientSampleFor(language, endpoint, "published-model", operation.id)
      assert.ok(sample.includes(endpoint + operation.path))
      assert.ok(sample.includes("TURNSTILE_APIM_KEY"))
      assert.ok(!sample.includes("x-user-id"))
      if (operation.id === "models") assert.ok(!sample.includes("--data-raw"))
    }
  }
})

test("each sample operation offers only the models whose capability can serve it", () => {
  const chat = { id: "chat", capabilities: ["chat", "streaming"] }
  const image = { id: "image", capabilities: ["image_generation"] }
  const decision = { id: "decision", capabilities: ["decision"] }
  const models = [chat, image, decision]

  assert.deepEqual(operationSampleModels(models, "image_generations"), [image])
  assert.deepEqual(operationSampleModels(models, "chat_completions"), [chat])
  assert.deepEqual(operationSampleModels(models, "decision"), [decision])
  assert.deepEqual(operationSampleModels(models, "models"), models)
  assert.deepEqual(operationSampleModels([chat], "image_generations"), [])
})

test("copy success expires after two seconds and repeated copies restart the feedback", async () => {
  const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
  const { rolldown } = await import(require.resolve("rolldown"))
  const entry = fileURLToPath(new URL("../../../frontend/src/components/finops/application-client-quickstart.tsx", import.meta.url))
  const bundle = await rolldown({ input: entry, external: name => name !== entry, transform: { jsx: { runtime: "automatic" } } })
  let code
  try { code = (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }

  const states = [], timers = new Map(), exports = {}
  let cursor = 0, now = 0, nextTimer = 0, dependencies, cleanup, rejectClipboard = false
  const hooks = {
    useState(initial) {
      const index = cursor++
      if (!(index in states)) states[index] = initial
      return [states[index], value => { states[index] = value }]
    },
    useEffect(effect, nextDependencies) {
      if (!dependencies || nextDependencies.some((value, index) => value !== dependencies[index])) {
        cleanup?.()
        dependencies = nextDependencies
        cleanup = effect()
      }
    },
  }
  runInNewContext(code, { exports,
    window: {
      setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, deadline: now + delay }); return id },
      clearTimeout(id) { timers.delete(id) },
    },
    navigator: { clipboard: { async writeText() { if (rejectClipboard) throw new Error("denied") } } },
    require(name) {
      if (name === "react") return hooks
      if (name === "react/jsx-runtime") return require(name)
      if (name === "lucide-react") return { Check: "Check", Copy: "Copy", Code2: "Code2", WrapText: "WrapText" }
      if (name.startsWith("highlight.js/")) return { registerLanguage() {}, highlight: value => ({ value }) }
      if (name === "../ui/button") return { Button: "button" }
      return {}
    },
  })
  const render = () => { cursor = 0; return exports.ClientSampleCode({ endpoint, sampleForLanguage: () => "unit sample" }) }
  const find = (node, predicate) => {
    if (!node || typeof node !== "object") return undefined
    if (predicate(node)) return node
    return [node.props?.children].flat(Infinity).map(child => find(child, predicate)).find(Boolean)
  }
  const copyButton = tree => find(tree, node => node.props?.className === "application-quickstart-copy")
  const isCopied = () => copyButton(render()).props.children.type === "Check"
  const advance = milliseconds => {
    now += milliseconds
    for (const [id, timer] of timers) if (timer.deadline <= now) { timers.delete(id); timer.callback() }
  }
  const copy = async () => {
    copyButton(render()).props.onClick()
    await new Promise(resolve => setImmediate(resolve))
    render()
  }

  assert.equal(isCopied(), false)
  await copy()
  assert.equal(isCopied(), true)
  advance(1999)
  assert.equal(isCopied(), true)
  advance(1)
  assert.equal(isCopied(), false)
  await copy()
  advance(1000)
  await copy()
  assert.equal(timers.size, 1)
  advance(1999)
  assert.equal(isCopied(), true)
  advance(1)
  assert.equal(isCopied(), false)
  await copy()
  find(render(), node => node.props?.children === "Python").props.onClick()
  assert.equal(isCopied(), false)
  assert.equal(timers.size, 0)
  rejectClipboard = true
  await copy()
  assert.ok(find(render(), node => node.props?.role === "alert"))
  assert.equal(timers.size, 0)
  rejectClipboard = false
  await copy()
  assert.equal(timers.size, 1)
  cleanup()
  assert.equal(timers.size, 0)
})