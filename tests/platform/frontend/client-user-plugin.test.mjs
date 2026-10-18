import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile, copyFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { adaptModelResponse, importBootstrap, syncAccountProfile } from "../../../backend/services/client_user_plugin/helpers.mjs";
import * as entry from "../../../backend/services/client_user_plugin/index.mjs";

test("user namespaces accept readable names and preserve stable authorization identity", async () => {
  const folder = await mkdtemp(join(tmpdir(), "ts-user-namespace-"));
  const nativeFetch = globalThis.fetch;
  try {
    for (const file of ["index.mjs", "images.mjs", "native-images.mjs", "helpers.mjs", "quota.mjs"])
      await copyFile(new URL("../../../backend/services/client_user_plugin/" + file, import.meta.url), join(folder, file));
    await copyFile(new URL("../../../backend/services/client_plugin/pricing.mjs", import.meta.url), join(folder, "pricing.mjs"));
    const factory = (await import(pathToFileURL(join(folder, "index.mjs")))).default;
    for (const providerId of ["aigateway-user-edwin-012345abcdef", "aigateway-user-研发-012345abcdef", "aigateway-user-platform-user"]) {
      await writeFile(join(folder, "aigateway-user.config.json"), JSON.stringify({ version: 4,
        platformOrigin: "https://platform.example", providerId, userId: "stable-user", userName: "Edwin", name: "User", icon: "data:image/svg+xml;base64,PHN2Zy8+" }));
      const hooks = await factory({ directory: folder, project: { id: "opencode" } });
      assert.equal(hooks.auth.provider, providerId);
      assert.equal(hooks.auth.icon, "data:image/svg+xml;base64,PHN2Zy8+");
      assert.equal(hooks.provider.id, providerId);
      const cfg = {};
      await hooks.config(cfg);
      assert.deepEqual(Object.keys(cfg.provider), [providerId]);
      if (providerId.includes("研发")) {
        let target, sent, sentBody;
        globalThis.fetch = async (input, init) => { target = input instanceof Request ? input.url : String(input); sent = init;
          sentBody = input instanceof Request ? await input.text() : undefined;
          return Response.json({ model: "fixture" }); };
        const loader = await hooks.auth.loader(async () => ({ type: "oauth", access: "fixture-token" }));
        const broken = loader.baseURL + "plugin://" + encodeURIComponent(providerId) + "/v1/chat/completions";
        const body = '{"model":"fixture"}';
        await loader.fetch(new Request(broken, { method: "POST", body }));
        assert.equal(target, loader.baseURL + "/chat/completions");
        assert.equal(sentBody, body);
        assert.equal(sent.headers.get("authorization"), "Bearer fixture-token");
        await assert.rejects(loader.fetch(broken.replace("/v1/chat/completions", "/v1/admin")), /outside/);
        await assert.rejects(loader.fetch(broken.replace(encodeURIComponent(providerId), "another-provider")), /outside/);
      }
    }
    await writeFile(join(folder, "aigateway-user.config.json"), JSON.stringify({ version: 4,
      platformOrigin: "https://platform.example", providerId: "aigateway-user-../../other" }));
    await assert.rejects(factory({ directory: folder }), /Invalid user plugin bootstrap/);
  } finally { globalThis.fetch = nativeFetch; await rm(folder, { recursive: true, force: true }); }
});

test("entry exports only the plugin factory, not helpers the host would execute as plugins", () => {
  assert.deepEqual(Object.keys(entry), ["default"]);
});

for (const workerFlag of ["AIGATEWAY_PRICING_WORKER", "AIGATEWAY_NATIVE_IMAGE_WORKER"])
test(`${workerFlag} workers do not bootstrap, rename accounts or start image companions`, async () => {
  const root = await mkdtemp(join(tmpdir(), "ts-pricing-worker-")), folder = join(root, "magpie");
  const previousFlag = process.env[workerFlag], previousFetch = globalThis.fetch;
  try {
    await mkdir(folder);
    for (const file of ["index.mjs", "images.mjs", "native-images.mjs", "helpers.mjs", "quota.mjs"])
      await copyFile(new URL("../../../backend/services/client_user_plugin/" + file, import.meta.url), join(folder, file));
    await copyFile(new URL("../../../backend/services/client_plugin/pricing.mjs", import.meta.url), join(folder, "pricing.mjs"));
    const providerId = "aigateway-user-worker-fixture", state = join(folder, "aigateway-proxy-user", providerId);
    await mkdir(state, { recursive: true });
    await writeFile(join(state, "installation.json"), '{"id":"keep-installation"}');
    await writeFile(join(state, "catalog.json"), JSON.stringify({ user: { id: "uid", name: "Edwin" },
      models: { m: { cost: { input: 2, output: 10, cache_read: .2, cache_write: 2.5 } } },
      images: [{ id: "image-fixture", protocol: "openai_images" }] }));
    await writeFile(join(folder, "aigateway-user.config.json"), JSON.stringify({ version: 4,
      platformOrigin: "https://platform.example", providerId, userId: "uid", userName: "Edwin", name: "User" }));
    const auth = '{"aigateway-user-worker-fixture":{"type":"oauth","uid":"uid","accountId":"Keep manual label","access":"fixture-access"}}';
    await writeFile(join(folder, "plugin-auth.json"), auth);
    process.env[workerFlag] = "1";
    globalThis.fetch = async () => { throw new Error("Pricing worker must not call the platform here"); };
    const factory = (await import(pathToFileURL(join(folder, "index.mjs")))).default;
    const plugin = await factory({ project: { id: "magpie" }, directory: folder });
    const models = await plugin.provider.models({}, { auth: { type: "oauth", access: "fixture-access" } });
    assert.equal(models.m.cost.cache.read, .2);
    const tokens = { access: "unchanged-access", refresh: "unchanged-refresh", expires: 1 };
    assert.deepEqual(await plugin.auth.refresh(tokens), tokens);
    await assert.rejects(plugin.auth.usage(async () => tokens), /maintenance worker/);
    assert.equal(await readFile(join(folder, "plugin-auth.json"), "utf8"), auth);
    assert.equal(await readFile(join(state, "installation.json"), "utf8"), '{"id":"keep-installation"}');
    await assert.rejects(readFile(join(state, "bootstrap-imported.json")), { code: "ENOENT" });
    await assert.rejects(readFile(join(state, "images-ready.json")), { code: "ENOENT" });
  } finally {
    if (previousFlag === undefined) delete process.env[workerFlag];
    else process.env[workerFlag] = previousFlag;
    globalThis.fetch = previousFetch;
    await rm(root, { recursive: true, force: true });
  }
});

test("SSE aliases preserve split UTF-8, tools, usage and real model differences", async () => {
  const original = 'event: response.created\ndata: {"response":{"model":"gpt-6-astra","id":"r","text":"中文"}}\n\nevent: message_start\ndata: {"message":{"model":"gpt-6-astra","id":"msg"}}\n\ndata: {"model":"other","call_id":"c","usage":{"output_tokens":2}}\n\ndata: [DONE]\n\n';
  const bytes = new TextEncoder().encode(original);
  const body = new ReadableStream({ start(controller) { for (const b of bytes) controller.enqueue(new Uint8Array([b])); controller.close(); } });
  const response = adaptModelResponse(new Response(body, { headers: { "content-type": "text/event-stream" } }), "astra-platform", "gpt-6-astra");
  const text = await response.text();
  assert.ok(text.includes('"model":"astra-platform"'));
  assert.ok(text.includes('"message":{"model":"astra-platform","id":"msg"}'));
  assert.ok(text.includes('"text":"中文"'));
  assert.ok(text.includes('"model":"other"'));
  assert.ok(text.includes('"call_id":"c"'));
  assert.ok(text.includes('"output_tokens":2'));
  assert.ok(text.includes("data: [DONE]"));
});

test("user plugin has OAuth only, dynamically replaces models and isolates credentials", async () => {
  const folder = await mkdtemp(join(tmpdir(), "ts-user-plugin-"));
  const previous = globalThis.fetch;
  try {
    await copyFile(new URL("../../../backend/services/client_user_plugin/index.mjs", import.meta.url), join(folder, "index.mjs"));
    await copyFile(new URL("../../../backend/services/client_user_plugin/images.mjs", import.meta.url), join(folder, "images.mjs"));
    await copyFile(new URL("../../../backend/services/client_user_plugin/native-images.mjs", import.meta.url), join(folder, "native-images.mjs"));
    await copyFile(new URL("../../../backend/services/client_user_plugin/helpers.mjs", import.meta.url), join(folder, "helpers.mjs"));
    await copyFile(new URL("../../../backend/services/client_user_plugin/quota.mjs", import.meta.url), join(folder, "quota.mjs"));
    await copyFile(new URL("../../../backend/services/client_plugin/pricing.mjs", import.meta.url), join(folder, "pricing.mjs"));
    await writeFile(join(folder, "aigateway-user.config.json"), JSON.stringify({ version: 2, platformOrigin: "https://platform.example", providerId: "aigateway-user-platform-user", userId: "uid", binding: "b", name: "User" }));
    const factory = (await import(pathToFileURL(join(folder, "index.mjs")).href)).default;
    const plugin = await factory({ directory: folder, project: { id: "magpie" } });
    assert.deepEqual(plugin.auth.methods.map((x) => x.label), ["刷新模型", "重新登录", "查看额度趋势", "额度控制"]);
    assert.ok(plugin.auth.methods.every((x) => x.type === "oauth"));
    let current = { new: { id: "new", provider: { npm: "@ai-sdk/openai", api: "https://platform.example/api/v1/client-runtime" }, tool_call: true, reasoning: true, modalities: { input: ["text", "image"] } } };
    let last;
    globalThis.fetch = async (url, init) => {
      last = { url: String(url), init };
      if (String(url).endsWith("/catalog") || String(url).endsWith("/refresh-models")) return Response.json({ user: { id: "uid", name: "Edwin" }, models: current, aliases: { new: "upstream" }, images: [] });
      if (String(url).endsWith("/usage")) return Response.json({ user: "Edwin", as_of: "2026-10-13T00:00:00Z",
        windows: [{ name: "个人月度 Token", amount: 100, limit: 100, used: 100, aside: true,
          resetsAt: "2026-11-01T00:00:00Z", span: 31 * 86400 }] });
      return Response.json({ model: "upstream", id: "response" });
    };
    const auth = { type: "oauth", access: "private-user-token", refresh: "refresh", expires: Date.now() + 900_000, uid: "uid" };
    await writeFile(join(folder, "plugin-auth.json"), JSON.stringify({ "aigateway-user-platform-user": auth }));
    const discovered = await plugin.provider.models({ models: { old: {} } }, { auth });
    assert.deepEqual(Object.keys(discovered), ["new"]);
    assert.equal(discovered.new.api.npm, "@ai-sdk/openai");
    assert.equal(discovered.new.api.url, "https://platform.example/api/v1/client-runtime");
    assert.equal(discovered.new.api.id, "new");
    assert.equal(discovered.new.capabilities.toolcall, true);
    assert.equal(discovered.new.capabilities.reasoning, true);
    assert.equal(discovered.new.capabilities.input.image, true);
    current = {};
    assert.deepEqual(Object.keys(await plugin.provider.models({}, { auth })), ["new"]);
    const refresh = await plugin.auth.methods[0].authorize();
    assert.equal(refresh.url, "");
    const refreshed = await refresh.callback();
    assert.equal(refreshed.access, auth.access);
    assert.equal(refreshed.accountId, "Edwin");
    assert.equal(refreshed.uid, "uid");
    assert.ok(last.url.endsWith("/refresh-models"));
    assert.equal(last.init.headers.Authorization, "Bearer private-user-token");
    await writeFile(join(folder, "aigateway-proxy-user/aigateway-user-platform-user/images-owned.json"), JSON.stringify({ providerId: "aigateway-user-platform-user-images" }));
    const catalogFetch = globalThis.fetch;
    globalThis.fetch = async (url, init) => {
      if (String(url).endsWith("/api/provider/remove")) throw new Error("Desktop mutation API unavailable");
      return catalogFetch(url, init);
    };
    assert.deepEqual(await plugin.provider.models({ models: { old: {} } }, { auth }), {});
    const data = await plugin.auth.usage(async () => auth);
    assert.equal(data.windows[0].aside, true);
    const savedBeforeView = await readFile(join(folder, "plugin-auth.json"), "utf8");
    const view = await plugin.auth.methods[2].authorize();
    assert.ok(view.url.startsWith("file:"));
    assert.ok((await readFile(new URL(view.url), "utf8")).includes("仅记录，不阻断"));
    assert.equal((await view.callback()).access, auth.access);
    assert.equal(await readFile(join(folder, "plugin-auth.json"), "utf8"), savedBeforeView);
    const workingFetch = globalThis.fetch;
    globalThis.fetch = async () => Response.json({ error: "invalid_grant" }, { status: 400 });
    await assert.rejects(plugin.auth.refresh({ refresh: "expired-refresh" }), (error) => error.signIn === "expired");
    globalThis.fetch = workingFetch;
    const loader = await plugin.auth.loader(async () => auth);
    await assert.rejects(loader.fetch("https://attacker.example/responses"), /outside/);
    await loader.fetch(loader.baseURL + "/responses", { body: '{"model":"new"}', headers: { "x-user-id": "forged", "Ocp-Apim-Subscription-Key": "wrong", Authorization: "agent-key" } });
    assert.equal(last.init.headers.get("authorization"), "Bearer private-user-token");
    assert.equal(last.init.headers.get("x-user-id"), null);
    assert.equal(last.init.headers.get("ocp-apim-subscription-key"), null);
    assert.equal(last.init.redirect, "manual");
    const cached = await readFile(join(folder, "aigateway-proxy-user/aigateway-user-platform-user/catalog.json"), "utf8");
    assert.ok(!cached.includes("private-user-token"));
    assert.deepEqual(await plugin.provider.models({}, {}), {});
  } finally { globalThis.fetch = previous; await rm(folder, { recursive: true, force: true }); }
});

test("profile metadata migrates existing slots by stable uid without touching other users or providers", async () => {
  const directory = await mkdtemp(join(tmpdir(), "ts-user-profile-"));
  try {
    const cfg = { providerId: "aigateway-user-platform-user", userId: "uuid-user" };
    const input = { project: { id: "magpie" }, directory };
    const path = join(directory, "plugin-auth.json");
    const legacy = { type: "oauth", access: "private-access", refresh: "private-refresh", uid: cfg.userId, accountId: cfg.userId };
    const other = { type: "oauth", access: "other-access", uid: "another-user", accountId: cfg.userId };
    await writeFile(path, JSON.stringify({ [cfg.providerId + "#old"]: legacy,
      [cfg.providerId + "#other"]: other, "aigateway-subscription": { type: "api", key: "legacy-key" } }));
    await syncAccountProfile(input, cfg, { id: cfg.userId, name: "Edwin" });
    let saved = JSON.parse(await readFile(path, "utf8"));
    assert.deepEqual(saved[cfg.providerId + "#old"], { ...legacy, accountId: "Edwin" });
    assert.deepEqual(saved[cfg.providerId + "#other"], other);
    assert.deepEqual(saved["aigateway-subscription"], { type: "api", key: "legacy-key" });
    await syncAccountProfile(input, cfg, { id: cfg.userId, name: "Renamed Edwin" });
    saved = JSON.parse(await readFile(path, "utf8"));
    assert.equal(saved[cfg.providerId + "#old"].accountId, "Renamed Edwin");
    assert.equal(Object.keys(saved).filter(id => id.startsWith(cfg.providerId)).length, 2);
    await assert.rejects(syncAccountProfile(input, cfg, { id: "attacker", name: "Edwin" }), /does not match/);
    assert.deepEqual(JSON.parse(await readFile(path, "utf8")), saved);
    await writeFile(path, JSON.stringify({ unrelated: { type: "api", key: "keep" } }));
    await syncAccountProfile(input, cfg, { id: cfg.userId, name: "Edwin" });
    assert.deepEqual(JSON.parse(await readFile(path, "utf8")), { unrelated: { type: "api", key: "keep" } });
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test("downloaded OAuth imports once, preserves other accounts and cannot resurrect logout", async () => {
  const directory = await mkdtemp(join(tmpdir(), "ts-user-bootstrap-"));
  try {
    const cfg = { version: 4, providerId: "aigateway-user-platform-user", userId: "user", userName: "Edwin", installationId: "install",
      authorization: { type: "oauth", access: "tsca_test", refresh: "tscr_test", expires: Date.now() + 900_000,
        uid: "user", accountId: "Edwin", installationId: "install" } };
    const input = { project: { id: "magpie" }, directory };
    const other = { unrelated: { type: "api", key: "other" }, "aigateway-old": { type: "api", key: "legacy" } };
    const path = join(directory, "plugin-auth.json");
    await writeFile(path, JSON.stringify(other));
    assert.equal(await importBootstrap({ directory }, cfg), false);
    assert.equal(await importBootstrap(input, cfg), true);
    assert.deepEqual(JSON.parse(await readFile(path, "utf8")), { ...other, [cfg.providerId]: cfg.authorization });
    await writeFile(path, JSON.stringify(other));
    assert.equal(await importBootstrap(input, cfg), false);
    assert.deepEqual(JSON.parse(await readFile(path, "utf8")), other);
    assert.deepEqual(JSON.parse(await readFile(path + ".bak", "utf8")), other);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test("first authorized import replaces stale unauthenticated installation and catalog state", async () => {
  const folder = await mkdtemp(join(tmpdir(), "ts-user-stale-bootstrap-"));
  try {
    const providerId = "aigateway-user-platform-user";
    for (const name of ["index.mjs", "helpers.mjs", "images.mjs", "native-images.mjs", "quota.mjs"]) await copyFile(new URL(`../../../backend/services/client_user_plugin/${name}`, import.meta.url), join(folder, name));
    await copyFile(new URL("../../../backend/services/client_plugin/pricing.mjs", import.meta.url), join(folder, "pricing.mjs"));
    const state = join(folder, "aigateway-proxy-user", providerId);
    await mkdir(state, { recursive: true });
    await writeFile(join(state, "installation.json"), JSON.stringify({ id: "old-install" }));
    await writeFile(join(state, "catalog.json"), JSON.stringify({ models: { stale: {} }, images: [] }));
    const authorization = { type: "oauth", access: "tsca_new", refresh: "tscr_new", expires: Date.now() + 900_000, uid: "user", accountId: "user", installationId: "new-install" };
    await writeFile(join(folder, "aigateway-user.config.json"), JSON.stringify({ version: 3, platformOrigin: "https://platform.example", providerId, userId: "user", name: "User", installationId: "new-install", authorization, catalog: { models: { bundled: { id: "bundled" } }, images: [], aliases: {} } }));
    const factory = (await import(pathToFileURL(join(folder, "index.mjs")).href)).default;
    const plugin = await factory({ project: { id: "magpie" }, directory: folder });
    assert.equal(JSON.parse(await readFile(join(state, "installation.json"), "utf8")).id, "new-install");
    assert.deepEqual(Object.keys(await plugin.provider.models({}, { auth: authorization })), ["bundled"]);
  } finally { await rm(folder, { recursive: true, force: true }); }
});
