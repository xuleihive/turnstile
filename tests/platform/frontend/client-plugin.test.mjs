import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, copyFile, readFile, stat, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

async function copyPlugin(dir) {
  for (const name of ["index.mjs", "pricing.mjs", "image-sync.mjs"])
    await copyFile(new URL("../../../backend/services/client_plugin/" + name, import.meta.url), join(dir, name));
  for (const name of ["helpers.mjs", "images.mjs", "native-images.mjs", "quota.mjs"])
    await copyFile(new URL("../../../backend/services/client_user_plugin/" + name, import.meta.url), join(dir, name));
}

test("subscription namespaces accept readable names but reject path injection", async () => {
  const dir = await mkdtemp(join(tmpdir(), "aigateway-subscription-name-"));
  const nativeFetch = globalThis.fetch;
  try {
    await copyPlugin(dir);
    const factory = (await import(pathToFileURL(join(dir, "index.mjs")))).default;
    for (const id of ["aigateway-subscription-edwin-agent-012345abcdef", "aigateway-subscription-研发-012345abcdef", "aigateway-test"]) {
      await writeFile(join(dir, "aigateway.config.json"), JSON.stringify({ version: 1, providerId: id,
        name: "Subscription", accountId: "Edwin-Agent", subscriptionKey: "fixture-key", baseURL: "https://gateway.example/llm", models: {}, modelsPath: "/v1/models", icon: "data:image/svg+xml;base64,PHN2Zy8+" }));
      const hooks = await factory();
      assert.equal(hooks.auth.provider, id);
      assert.equal(hooks.auth.icon, "data:image/svg+xml;base64,PHN2Zy8+");
      assert.equal(hooks.provider.id, id);
      const cfg = {};
      await hooks.config(cfg);
      assert.deepEqual(Object.keys(cfg.provider), [id]);
      if (id.includes("研发")) {
        let target, body;
        globalThis.fetch = async input => { target = input instanceof Request ? input.url : String(input);
          body = input instanceof Request ? await input.text() : undefined;
          return Response.json({ model: "fixture" }); };
        const loader = await hooks.auth.loader(async () => ({ type: "api", key: "fixture-key" }));
        const broken = "https://gateway.example/llmplugin://" + encodeURIComponent(id) + "/v1/responses";
        await loader.fetch(new Request(broken, { method: "POST", body: '{"model":"fixture"}' }));
        assert.equal(target, "https://gateway.example/llm/responses");
        assert.equal(body, '{"model":"fixture"}');
        await assert.rejects(loader.fetch(broken.replace("/v1/responses", "/v1/admin")), /outside/);
        await assert.rejects(loader.fetch(broken.replace(encodeURIComponent(id), "another-provider")), /outside/);
      }
    }
    await writeFile(join(dir, "aigateway.config.json"), JSON.stringify({ version: 1,
      providerId: "aigateway-../../other", subscriptionKey: "fixture-key", baseURL: "https://gateway.example/llm" }));
    await assert.rejects(factory(), /Invalid AIGateway subscription configuration/);
  } finally { globalThis.fetch = nativeFetch; await rm(dir, { recursive: true, force: true }); }
});

test("private provider import, bounded dispatch and live model intersection", async () => {
  const dir = await mkdtemp(join(tmpdir(), "aigateway-plugin-test-"));
  const nativeFetch = globalThis.fetch;
  try {
    await copyPlugin(dir);
    await writeFile(join(dir, "aigateway.config.json"), JSON.stringify({
      version: 1, providerId: "aigateway-test", name: "Test", baseURL: "https://gateway.example.com/llm",
      subscriptionKey: "private-key", accountId: "Test subscription", modelsPath: "/v1/models",
      models: { a: { name: "A" }, b: { name: "B" } },
    }));
    const { default: plugin } = await import(pathToFileURL(join(dir, "index.mjs")));
    const hooks = await plugin();
    assert.deepEqual(hooks.auth.methods.map((method) => method.label), ["导入配置", "更新API Key", "刷新模型"]);
    const config = {};
    await hooks.config(config);
    assert.deepEqual(Object.keys(config.provider["aigateway-test"].models), ["a", "b"]);
    const custom = { provider: { "aigateway-test": { name: "Keep" } } };
    await hooks.config(custom);
    assert.equal(custom.provider["aigateway-test"].name, "Keep");
    const authorization = await hooks.auth.methods[0].authorize();
    assert.equal(authorization.url, "");
    assert.equal((await authorization.callback()).key, "private-key");
    assert.equal((await authorization.callback()).accountId, "Test subscription");
    await assert.rejects(hooks.auth.loader(async () => null), /subscription rejected/);
    const loader = await hooks.auth.loader(async () => ({ type: "api", key: "rotated-key" }));
    const calls = [];
    const reply = new Response("data: first\n\ndata: [DONE]\n", { headers: { "content-type": "text/event-stream" } });
    globalThis.fetch = async (input, init) => { calls.push({ input, init }); return reply; };
    const signal = new AbortController().signal;
    assert.equal(await loader.fetch("https://gateway.example.com/llm/responses", {
      signal, body: "payload", headers: { authorization: "agent-key", "x-api-key": "agent-key", "x-user-id": "forged" },
    }), reply);
    assert.equal(calls[0].init.signal, signal);
    assert.equal(calls[0].init.body, "payload");
    assert.equal(calls[0].init.redirect, "manual");
    assert.equal(calls[0].init.headers.get("ocp-apim-subscription-key"), "rotated-key");
    assert.equal(calls[0].init.headers.get("authorization"), null);
    assert.equal(calls[0].init.headers.get("x-api-key"), null);
    assert.equal(calls[0].init.headers.get("x-user-id"), null);
    await loader.fetch("https://gateway.example.com/llm/messages", {});
    assert.equal(String(calls[1].input), "https://gateway.example.com/llm/v1/messages");
    for (const url of ["https://attacker.example.com/llm/responses", "https://gateway.example.com/llm-other/responses",
      "https://gateway.example.com/llm/admin", "http://gateway.example.com/llm/responses"]) {
      await assert.rejects(loader.fetch(url), /outside the configured gateway/);
    }
    const provider = { models: config.provider["aigateway-test"].models };
    const auth = { type: "api", key: "private-key" };
    globalThis.fetch = async () => Response.json({ data: [{ id: "a" }, { id: "new-not-in-snapshot" }] });
    assert.deepEqual(Object.keys(await hooks.provider.models(provider, { auth })), ["a"]);
    globalThis.fetch = async () => Response.json({ data: [] });
    assert.deepEqual(await hooks.provider.models(provider, { auth }), {});
    globalThis.fetch = async () => { throw new Error("offline"); };
    const offline = await hooks.provider.models(provider, { auth });
    assert.deepEqual(Object.keys(offline), [], "an authoritative empty grant is not restored on network failure");
    assert.equal(offline[Symbol.for("magpie.fellBack")], true);
    globalThis.fetch = async () => new Response("denied", { status: 403 });
    await assert.rejects(hooks.provider.models(provider, { auth }), (error) => error.signIn === "expired");
  } finally {
    globalThis.fetch = nativeFetch;
    await rm(dir, { recursive: true, force: true });
  }
});

test("magpie automatically imports once, preserves accounts and respects logout", async () => {
  const dir = await mkdtemp(join(tmpdir(), "aigateway-auto-import-"));
  try {
    await copyPlugin(dir);
    const config = {
      version: 1, providerId: "aigateway-test", name: "Test", baseURL: "https://gateway.example.com/llm",
      subscriptionKey: "bundled-key", accountId: "Subscription", modelsPath: "/v1/models", models: {},
    };
    await writeFile(join(dir, "aigateway.config.json"), JSON.stringify(config));
    const { default: plugin } = await import(pathToFileURL(join(dir, "index.mjs")));
    const home = await mkdtemp(join(dir, "home-"));
    const input = { directory: home, project: { id: "magpie" } };
    const authPath = join(home, "plugin-auth.json");
    const original = JSON.stringify({ copilot: { type: "api", key: "existing-other-provider" } });
    await writeFile(authPath, original);
    await Promise.all([plugin(input), plugin(input)]);
    let auth = JSON.parse(await readFile(authPath, "utf8"));
    assert.equal(auth.copilot.key, "existing-other-provider");
    assert.equal(auth[config.providerId].key, "bundled-key");
    assert.equal(auth[config.providerId].accountId, "Subscription");
    // Windows stat modes do not represent its ACL-based file permissions.
    if (process.platform !== "win32") assert.equal((await stat(authPath)).mode & 0o777, 0o600);
    assert.equal(await readFile(authPath + ".bak", "utf8"), original);
    const marker = join(home, "aigateway-proxy-imports", config.providerId + ".json");
    if (process.platform !== "win32") assert.equal((await stat(marker)).mode & 0o777, 0o600);
    assert.equal((await readFile(marker, "utf8")).includes("bundled-key"), false);
    auth[config.providerId].key = "rotated-key";
    await writeFile(authPath, JSON.stringify(auth));
    await plugin(input);
    assert.equal(JSON.parse(await readFile(authPath, "utf8"))[config.providerId].key, "rotated-key");
    delete auth[config.providerId];
    await writeFile(authPath, JSON.stringify(auth));
    await plugin(input);
    assert.equal(JSON.parse(await readFile(authPath, "utf8"))[config.providerId], undefined);
    for (const suffix of ["", "#other"]) {
      const existing = await mkdtemp(join(dir, "existing-"));
      const data = JSON.stringify({ [config.providerId + suffix]: { type: "api", key: "keep-key" } });
      await writeFile(join(existing, "plugin-auth.json"), data);
      await plugin({ directory: existing, project: { id: "magpie" } });
      assert.equal(await readFile(join(existing, "plugin-auth.json"), "utf8"), data);
    }
    const corrupt = await mkdtemp(join(dir, "corrupt-"));
    await writeFile(join(corrupt, "plugin-auth.json"), "invalid-auth");
    const hooks = await plugin({ directory: corrupt, project: { id: "magpie" } });
    assert.equal(await readFile(join(corrupt, "plugin-auth.json"), "utf8"), "invalid-auth");
    assert.equal((await (await hooks.auth.methods[0].authorize()).callback()).key, "bundled-key");
    const otherHost = await mkdtemp(join(dir, "opencode-"));
    await plugin({ directory: otherHost, project: { id: "opencode" } });
    await assert.rejects(readFile(join(otherHost, "plugin-auth.json")), { code: "ENOENT" });
    const empty = await mkdtemp(join(dir, "new-"));
    await plugin({ directory: empty, project: { id: "magpie" } });
    assert.equal(JSON.parse(await readFile(join(empty, "plugin-auth.json"), "utf8"))[config.providerId].key, "bundled-key");
    const locked = await mkdtemp(join(dir, "locked-"));
    await writeFile(join(locked, "plugin-auth.json.lock"), "keep-lock");
    await plugin({ directory: locked, project: { id: "magpie" } });
    assert.equal(await readFile(join(locked, "plugin-auth.json.lock"), "utf8"), "keep-lock");
    await assert.rejects(readFile(join(locked, "plugin-auth.json")), { code: "ENOENT" });
    const failedWrite = await mkdtemp(join(dir, "write-failure-"));
    await writeFile(join(failedWrite, "plugin-auth.json"), original);
    await writeFile(join(failedWrite, "aigateway-proxy-imports"), "not-a-directory");
    await plugin({ directory: failedWrite, project: { id: "magpie" } });
    assert.equal(await readFile(join(failedWrite, "plugin-auth.json"), "utf8"), original);
    await assert.rejects(readFile(join(failedWrite, "plugin-auth.json.lock")), { code: "ENOENT" });
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("subscription response aliases stream intact and dispatch uses the current key and rejects hostile URLs", async () => {
  const dir = await mkdtemp(join(tmpdir(), "ts-subscription-dispatch-")), previousFetch = globalThis.fetch;
  try {
    await copyPlugin(dir);
    const cfg = { version: 1, providerId: "aigateway-subscription-dispatch", baseURL: "https://gateway.example/llm",
      subscriptionKey: "bundled", modelsPath: "/v1/models", models: {}, aliases: { alias: "upstream" } };
    await writeFile(join(dir, "aigateway.config.json"), JSON.stringify(cfg));
    const hooks = await (await import(pathToFileURL(join(dir, "index.mjs")))).default();
    let auth = { type: "api", key: "first" }, calls = [];
    const loader = await hooks.auth.loader(async () => auth);
    globalThis.fetch = async (url, init) => {
      calls.push(init.headers);
      const bytes = new TextEncoder().encode('event: message_start\ndata: {"message":{"model":"upstream","id":"msg"},"text":"中文"}\n\ndata: {"model":"different","call_id":"c","usage":{"output_tokens":2}}\n\ndata: [DONE]\n\n');
      return new Response(new ReadableStream({ start(c) { for (const b of bytes) c.enqueue(new Uint8Array([b])); c.close(); } }),
        { headers: { "content-type": "text/event-stream" } });
    };
    auth = { type: "api", key: "rotated" };
    const response = await loader.fetch(cfg.baseURL + "/v1/messages", { method: "POST", body: '{"model":"alias"}',
      headers: { "x-turnstile-client-context": "forged", "x-turnstile-delegation-token": "forged", cookie: "private=agent",
        Authorization: "agent", "x-user-email": "fake", "x-org-id": "forged", "x-agent-name": "Trace only" } });
    const text = await response.text();
    assert.ok(text.includes('"model":"alias"'));
    assert.ok(text.includes('"model":"different","call_id":"c","usage":{"output_tokens":2}'));
    assert.ok(text.includes("中文") && text.includes("[DONE]"));
    assert.equal(calls[0].get("ocp-apim-subscription-key"), "rotated");
    for (const h of ["authorization", "x-turnstile-client-context", "x-turnstile-delegation-token", "cookie", "x-user-email", "x-org-id"])
      assert.equal(calls[0].get(h), null);
    assert.equal(calls[0].get("x-agent-name"), "Trace only");
    for (const suffix of ["/responses?credential=bad", "/responses#fragment", "/admin"])
      await assert.rejects(loader.fetch(cfg.baseURL + suffix), /outside/);
    auth = null;
    await assert.rejects(loader.fetch(cfg.baseURL + "/responses"), e => e.signIn === "expired");
    assert.equal(calls.length, 1);
    assert.deepEqual(await hooks.provider.models({ models: { stale: {} } }), {});
  } finally { globalThis.fetch = previousFetch; await rm(dir, { recursive: true, force: true }); }
});

test("explicit subscription refresh preserves the current key and persists empty/restricted intersections", async () => {
  const dir = await mkdtemp(join(tmpdir(), "ts-subscription-refresh-")), previousFetch = globalThis.fetch;
  try {
    await copyPlugin(dir);
    const cfg = { version: 1, providerId: "aigateway-subscription-refresh", baseURL: "https://gateway.example/llm",
      subscriptionKey: "bundled-old", modelsPath: "/v1/models", models: { a: { id: "a" }, b: { id: "b" } }, images: [] };
    await writeFile(join(dir, "aigateway.config.json"), JSON.stringify(cfg));
    const authPath = join(dir, "plugin-auth.json"), own = { type: "api", key: "current", accountId: "Keep label" };
    const original = JSON.stringify({ [cfg.providerId]: own, copilot: { type: "api", key: "unrelated" } });
    await writeFile(authPath, original);
    const factory = (await import(pathToFileURL(join(dir, "index.mjs")))).default;
    const input = { directory: dir, project: { id: "magpie" } }, hooks = await factory(input);
    let data = [{ id: "a" }, { id: "not-in-snapshot" }];
    globalThis.fetch = async (url, init) => {
      assert.equal(init.headers.get("ocp-apim-subscription-key"), "current");
      return Response.json({ data });
    };
    const action = await hooks.auth.methods.find(m => m.label === "刷新模型").authorize();
    const result = await action.callback();
    assert.equal(result.key, "current");
    assert.equal(result.accountId, "Keep label");
    assert.deepEqual(Object.keys(await hooks.provider.models({ models: cfg.models }, { auth: own })), ["a"]);
    data = [];
    await action.callback();
    globalThis.fetch = async () => { throw new Error("offline"); };
    const reloaded = await factory(input);
    assert.deepEqual(Object.keys(await reloaded.provider.models({ models: cfg.models }, { auth: own })), []);
    await assert.rejects((await reloaded.auth.methods[2].authorize()).callback(), /unavailable/);
    assert.equal(await readFile(authPath, "utf8"), original);
    await writeFile(join(dir, "aigateway-proxy-subscription", cfg.providerId, "catalog.json"), "corrupt");
    const damaged = await factory(input);
    assert.deepEqual(Object.keys(await damaged.provider.models({ models: cfg.models }, { auth: own })), []);
    globalThis.fetch = async () => { await writeFile(authPath, "{}"); return Response.json({ data: [] }); };
    await assert.rejects(action.callback(), e => e.signIn === "expired");
    assert.equal(await readFile(authPath, "utf8"), "{}", "logout during refresh cannot restore a saved key");
    await writeFile(authPath, original);
    await writeFile(authPath, JSON.stringify({ [cfg.providerId]: own, [cfg.providerId + "#other"]: { type: "api", key: "another-key" } }));
    await assert.rejects(action.callback(), /Multiple saved subscription keys/);
    await writeFile(authPath, "{}");
    await assert.rejects(action.callback(), e => e.signIn === "expired");
    assert.equal(await readFile(authPath, "utf8"), "{}");
    assert.ok(!(await readFile(join(dir, "aigateway-proxy-subscription", cfg.providerId, "catalog.json"), "utf8")).includes("current"));
  } finally { globalThis.fetch = previousFetch; await rm(dir, { recursive: true, force: true }); }
});

for (const flag of ["AIGATEWAY_PRICING_WORKER", "AIGATEWAY_NATIVE_IMAGE_WORKER"])
test(`subscription ${flag} cannot bootstrap, query the gateway or start companions`, async () => {
  const dir = await mkdtemp(join(tmpdir(), "ts-subscription-worker-"));
  const previousFetch = globalThis.fetch, previousFlag = process.env[flag];
  try {
    await copyPlugin(dir);
    const cfg = { version: 1, providerId: "aigateway-subscription-worker", baseURL: "https://gateway.example/llm",
      subscriptionKey: "bundled", modelsPath: "/v1/models", models: { m: { id: "m" } }, images: [{ id: "image" }] };
    await writeFile(join(dir, "aigateway.config.json"), JSON.stringify(cfg));
    process.env[flag] = "1";
    globalThis.fetch = async () => { throw new Error("Maintenance worker cannot call the gateway"); };
    const hooks = await (await import(pathToFileURL(join(dir, "index.mjs")))).default({ directory: dir, project: { id: "magpie" } });
    assert.deepEqual(Object.keys(await hooks.provider.models({ models: cfg.models }, { auth: { type: "api", key: "current" } })), ["m"]);
    await assert.rejects(readFile(join(dir, "plugin-auth.json")), { code: "ENOENT" });
    await assert.rejects(readFile(join(dir, "aigateway-proxy-subscription", cfg.providerId, "images-ready.json")), { code: "ENOENT" });
    await assert.rejects((await hooks.auth.methods[0].authorize()).callback(), /maintenance worker/);
  } finally {
    if (previousFlag === undefined) delete process.env[flag]; else process.env[flag] = previousFlag;
    globalThis.fetch = previousFetch; await rm(dir, { recursive: true, force: true });
  }
});
