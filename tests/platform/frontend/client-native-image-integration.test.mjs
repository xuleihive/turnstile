import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import { createServer } from "node:http";
import { createServer as createSecureServer } from "node:https";
import { copyFile, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { isAbsolute, join, relative } from "node:path";
import { tmpdir } from "node:os";

const bun = process.env.AIGATEWAY_TEST_BUN, magpie = process.env.AIGATEWAY_TEST_MAGPIE;
const source = new URL("../../../backend/services/client_user_plugin/", import.meta.url);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const imageIds = ["gpt-image-2-foundry-d8c6686a", "gpt-image-2.5-flare-foundry-d8c6686a", "gpt-image-2.5-sunburst-foundry-d8c6686a"];
const png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

async function eventually(run, message, attempts = 150) {
  let error;
  for (let i = 0; i < attempts; i++) {
    try { return await run(); } catch (next) { error = next; await pause(200); }
  }
  throw new Error(message + ": " + error?.message);
}

function childProcess(executable, args, options) {
  const child = spawn(executable, args, { ...options, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
  let stderr = "", stdout = "";
  child.stderr.on("data", data => { stderr = (stderr + data.toString()).slice(-16000); });
  child.stdout.on("data", data => { stdout = (stdout + data.toString()).slice(-16000); });
  const exited = once(child, "exit");
  exited.catch(() => {});
  return { child, exited, output: () => stdout, errors: () => stderr, stop: async () => {
    child.stdin.destroy(); child.stdout.destroy(); child.stderr.destroy();
    if (child.exitCode === null && child.signalCode === null) child.kill();
    await exited;
  } };
}

for (const { kind, stop } of [{ kind: "user", stop: "logout" }, ...["logout", "disable", "remove", "foreign-slot", "gateway-tamper", "model-tamper"].map(stop => ({ kind: "subscription", stop }))])
test(`${kind}: native images, prices, historical repricing and ${stop}`, { skip: !bun || !magpie, timeout: 90000 }, async t => {
  try {
  const root = await mkdtemp(join(tmpdir(), "ts-native-image-integration-"));
  const settings = join(root, "config", "magpie"), spec = join(root, "plugin space"), project = join(root, "project");
  await mkdir(settings, { recursive: true });
  await mkdir(spec);
  await mkdir(project);
  const cleanup = [];
  t.after(async () => {
    const errors = [];
    for (const close of cleanup.reverse()) { try { await close(); } catch (error) { errors.push(error); } }
    const target = await realpath(root), parent = await realpath(tmpdir()), inside = relative(parent, target);
    assert.ok(inside && !inside.startsWith("..") && !isAbsolute(inside));
    await rm(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    assert.equal(errors.length, 0, errors.map(error => error.message).join("\n"));
  });
  const providerId = `aigateway-${kind}-native-integration`, installationId = "fixture-installation", userId = "fixture-user";
  const catalog = { user: { id: userId, name: "Fixture" }, models: {
    "gpt-6.1-sol-fixture": { name: "Fixture text", tool_call: true, limit: { context: 32768, output: 8192 } },
  }, images: imageIds.map(id => ({ id, name: id, protocol: "openai_images",
    cost: { input: 2, output: 10, cache_read: 0, cache_write: 0 } })), aliases: {} };
  if (kind === "subscription" && stop === "remove") catalog.models = {};
  const generations = [], platformCalls = [], discoveryKeys = [];
  const serve = async (req, res) => {
    const chunks = [];
    for await (const data of req) chunks.push(data);
    const body = Buffer.concat(chunks).toString();
    platformCalls.push(req.url);
    if (req.url === "/llm/v1/models") discoveryKeys.push(req.headers["ocp-apim-subscription-key"]);
    let result;
    if (kind === "subscription" && req.url === "/llm/v1/models") result = { data: [...Object.keys(catalog.models), ...imageIds].map(id => ({ id })) };
    else if (req.url === "/api/v1/client-runtime/image-capabilities") result = { key: "tsci_fixture" };
    else if (req.url === "/api/v1/client-runtime/catalog") result = catalog;
    else if (req.url.startsWith("/api/v1/client-runtime/usage")) result = { as_of: "2026-10-14T00:00:00Z", windows: [] };
    else if (req.url === "/api/v1/client-runtime/images/generations" || req.url === "/llm/images/generations") {
      generations.push({ body: JSON.parse(body), authorization: req.headers.authorization, subscriptionKey: req.headers["ocp-apim-subscription-key"] });
      result = { created: 1, data: [{ b64_json: png }], usage: { input_tokens: 7, output_tokens: 23, total_tokens: 30 } };
    } else if (req.url === "/api/v1/client-auth/revoke") result = {};
    else { res.statusCode = 404; result = { error: "Unexpected fixture route" }; }
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify(result));
  };
  let certificate;
  if (kind === "subscription") {
    certificate = join(root, "fixture-cert.pem");
    execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", join(root, "fixture-key.pem"),
      "-out", certificate, "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"], { stdio: "ignore" });
  }
  const platform = kind === "subscription" ? createSecureServer({ key: await readFile(join(root, "fixture-key.pem")),
    cert: await readFile(certificate) }, serve) : createServer(serve);
  platform.listen(0, "127.0.0.1");
  await once(platform, "listening");
  cleanup.push(async () => { platform.closeAllConnections(); await new Promise(resolve => platform.close(resolve)); });
  const origin = `${kind === "subscription" ? "https" : "http"}://127.0.0.1:${platform.address().port}`;
  for (const file of ["index.mjs", "images.mjs", "native-images.mjs", "helpers.mjs", "quota.mjs"])
    await copyFile(new URL(file, source), join(spec, file));
  await copyFile(new URL("../../../backend/services/client_plugin/pricing.mjs", import.meta.url), join(spec, "pricing.mjs"));
  await writeFile(join(spec, "package.json"), JSON.stringify({ name: "fixture-user-plugin", type: "module", main: "./index.mjs" }));
  if (kind === "user") await writeFile(join(spec, "aigateway-user.config.json"), JSON.stringify({ version: 4, platformOrigin: origin,
    providerId, userId, userName: "Fixture", name: "AIGateway User Fixture", installationId, catalog,
    authorization: { type: "oauth", uid: userId, accountId: "Fixture", access: "tsca_fixture", refresh: "tscr_fixture",
      installationId, expires: Date.now() + 900000 } }));
  else {
    for (const file of ["index.mjs", "image-sync.mjs"])
      await copyFile(new URL("../../../backend/services/client_plugin/" + file, import.meta.url), join(spec, file));
    await writeFile(join(spec, "aigateway.config.json"), JSON.stringify({ version: 1, providerId, subscriptionId: "fixture-subscription",
      name: "Fixture subscription", accountId: "Fixture", subscriptionKey: "fixture-apim-key", baseURL: origin + "/llm",
      modelsPath: "/v1/models", models: catalog.models, images: catalog.images, aliases: {} }));
  }
  await writeFile(join(settings, "plugins.json"), JSON.stringify({ plugins: [{ spec }] }));
  const preferences = '{"imageGen":"","theme":"light"}';
  await writeFile(join(settings, "settings.json"), preferences);
  await writeFile(join(settings, "providers.json"), JSON.stringify({ providers: [{ id: "keep", name: "Keep", off: true, key: "keep-private" }] }));

  const reservation = createServer();
  reservation.listen(0, "127.0.0.1");
  await once(reservation, "listening");
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const env = { ...process.env, HOME: root, USERPROFILE: root, XDG_CONFIG_HOME: join(root, "config"),
    APPDATA: join(root, "appdata"), LOCALAPPDATA: join(root, "localappdata"),
    XDG_CACHE_HOME: join(root, "cache"), MAGPIE_ADDR: `127.0.0.1:${port}`, MAGPIE_BUN: bun,
    MAGPIE_NO_STATS: "1", MAGPIE_NO_ACCOUNTS: "1", DO_NOT_TRACK: "1" };
  if (certificate) env.NODE_EXTRA_CA_CERTS = certificate;
  delete env.AIGATEWAY_PRICING_WORKER;
  delete env.AIGATEWAY_NATIVE_IMAGE_WORKER;
  delete env.AIGATEWAY_MAGPIE_EXECUTABLE;
  const server = childProcess(magpie, ["serve"], { env, cwd: root });
  cleanup.push(server.stop);
  const gateway = `http://127.0.0.1:${port}`;
  await eventually(async () => {
    const response = await fetch(gateway + "/v1/models", { signal: AbortSignal.timeout(1000) });
    assert.equal(response.status, 200);
  }, "isolated gateway failed to start");
  const loader = childProcess(magpie, ["plugin", "list", "--json"], { env, cwd: root });
  cleanup.push(loader.stop);
  let deadline;
  try {
    await Promise.race([loader.exited, new Promise((_, reject) => {
      deadline = setTimeout(() => reject(new Error("plugin initialization timed out: " + loader.errors())), 25000);
    })]);
  } finally { clearTimeout(deadline); }
  assert.equal(loader.child.exitCode, 0, loader.errors());
  const directory = join(settings, kind === "user" ? "aigateway-proxy-user" : "aigateway-proxy-subscription", providerId), readyPath = join(directory, "images-ready.json");
  cleanup.push(async () => {
    await writeFile(join(settings, "plugins.json"), '{"plugins":[]}');
    await writeFile(join(settings, "plugin-auth.json"), "{}");
    await eventually(async () => {
      try { await readFile(readyPath); } catch (error) { if (error.code === "ENOENT") return; throw error; }
      throw new Error("owned bridge has not stopped");
    }, "owned bridge cleanup", 70);
    const remaining = JSON.parse(await readFile(join(settings, "providers.json"), "utf8"));
    assert.ok(!remaining.providers.some(p => p.id === providerId + "-images"), "cleanup removes the owned native provider");
  });
  await eventually(async () => {
    const providers = JSON.parse(await readFile(join(settings, "providers.json"), "utf8")).providers;
    const images = providers.find(p => p.id === providerId + "-images");
    assert.deepEqual(images?.models, imageIds);
  }, "native image provider was not registered: " + loader.errors());
  const registered = JSON.parse(await readFile(join(settings, "providers.json"), "utf8"));
  assert.deepEqual(registered.providers.find(p => p.id === "keep"), { id: "keep", name: "Keep", off: true, key: "keep-private" });
  const assertPrices = async () => {
    const actual = JSON.parse(await readFile(join(settings, "settings.json"), "utf8"));
    assert.equal(actual.imageGen ?? "", "");
    assert.equal(actual.theme, "light");
    assert.deepEqual(actual.modelPrices, Object.fromEntries(imageIds.map(id => [providerId + "-images/" + id,
      { input: 2, output: 10, cache_read: 0, cache_write: 0 }])));
  };
  await eventually(assertPrices, "image prices were not imported into the native image provider");
  await eventually(async () => {
    const nativeStatus = JSON.parse(await readFile(join(directory, "native-images-status.json"), "utf8"));
    assert.equal(nativeStatus.state, "applied");
    assert.equal(nativeStatus.modelCount, 3);
  }, "native registration status did not settle");
  const ready = JSON.parse(await readFile(readyPath, "utf8"));
  assert.ok(ready.pid > 0);

  const mcp = childProcess(magpie, ["mcp", "image"], { env, cwd: project });
  cleanup.push(mcp.stop);
  let buffer = "", next = 0;
  const waiting = new Map();
  mcp.child.stdout.on("data", bytes => {
    buffer += bytes.toString();
    let at;
    while ((at = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, at); buffer = buffer.slice(at + 1);
      let message;
      try { message = JSON.parse(line); } catch { continue; }
      const complete = waiting.get(message.id);
      if (complete) { waiting.delete(message.id); complete(message); }
    }
  });
  const rpc = (method, params) => new Promise((resolve, reject) => {
    const id = ++next, timeout = setTimeout(() => { waiting.delete(id); reject(new Error("MCP timeout: " + method)); }, 25000);
    waiting.set(id, message => { clearTimeout(timeout); resolve(message); });
    mcp.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
  });
  assert.ok((await rpc("initialize", { protocolVersion: "2025-06-18", capabilities: {},
    clientInfo: { name: "native-image-fixture", version: "1" } })).result);
  mcp.child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');
  const response = await rpc("tools/call", { name: "generate_image", arguments: {
    prompt: "Fixture circle", n: 1,
    size: "1024x1024", quality: "low", path: "native-fixture.png",
  } });
  assert.ok(!response.error && !response.result?.isError, JSON.stringify(response));
  assert.deepEqual(await readFile(join(project, "native-fixture.png")), Buffer.from(png, "base64"));
  assert.equal(generations.length, 1);
  assert.ok(imageIds.includes(generations[0].body.model), "automatic selection preserves an original gateway ID");
  assert.equal(generations[0].body.n, 1);
  if (kind === "user") assert.equal(generations[0].authorization, "Bearer tsci_fixture");
  else {
    assert.equal(generations[0].subscriptionKey, "fixture-apim-key");
    assert.equal(generations[0].authorization, undefined);
    for (const name of ["images-runtime.json", "images-ready.json", "images-mcp.json", "images-provider.json", "native-images-managed.json"])
      assert.ok(!(await readFile(join(directory, name), "utf8")).includes("fixture-apim-key"), name + " must not export a subscription key");
  }
  assert.ok(!platformCalls.some(path => path.includes("/token") || path.includes("/api/provider")));
  await assertPrices();
  const usage = await fetch(gateway + "/v1/magpie/usage?period=today").then(r => r.json());
  const imageUsage = usage.models.find(m => m.provider === providerId + "-images");
  assert.equal(imageUsage?.input, 7);
  assert.equal(imageUsage?.output, 23);
  assert.equal(imageUsage?.unpriced, 0);
  assert.ok(Math.abs(imageUsage.cost - 0.000244) < 1e-12);
  const repricer = childProcess(magpie, ["model", "price", providerId + "-images/" + generations[0].body.model,
    "2,20,0,0"], { env, cwd: root });
  cleanup.push(repricer.stop);
  await repricer.exited;
  assert.equal(repricer.child.exitCode, 0, repricer.errors());
  await eventually(async () => {
    const updated = await fetch(gateway + "/v1/magpie/usage?period=today").then(r => r.json());
    const row = updated.models.find(m => m.provider === providerId + "-images");
    assert.equal(row.calls, 1);
    assert.equal(row.unpriced, 0);
    assert.ok(Math.abs(row.cost - 0.000474) < 1e-12);
  }, "recorded image tokens were not repriced");
  assert.equal(generations.length, 1, "historical repricing must not generate another image");
  if (kind === "subscription") {
    const local = `http://127.0.0.1:${ready.port}`, headers = { Authorization: "Bearer " + ready.nonce, "content-type": "application/json" };
    const statePath = join(directory, "images-runtime.json"), state = JSON.parse(await readFile(statePath, "utf8"));
    const authPath = join(settings, "plugin-auth.json"), accounts = JSON.parse(await readFile(authPath, "utf8"));
    accounts[state.accountSlot].key = "fixture-rotated-key";
    await writeFile(authPath, JSON.stringify(accounts));
    const generated = await fetch(local + "/v1/images/generations", { method: "POST", headers,
      body: JSON.stringify({ model: imageIds[0], prompt: "Fixture rotated key", n: 1 }) });
    assert.equal(generated.status, 200);
    assert.equal(generations.at(-1).subscriptionKey, "fixture-rotated-key");
    if (stop === "logout") {
      const beforeRefresh = await readFile(authPath, "utf8");
      const refresh = childProcess(magpie, ["plugin", "login", providerId, "3"], { env, cwd: root });
      cleanup.push(refresh.stop);
      await refresh.exited;
      assert.equal(refresh.child.exitCode, 0, refresh.errors());
      assert.deepEqual(JSON.parse(await readFile(authPath, "utf8")), JSON.parse(beforeRefresh));
      assert.equal(discoveryKeys.at(-1), "fixture-rotated-key");
      const prefs = JSON.parse(await readFile(join(settings, "settings.json"), "utf8"));
      assert.equal(prefs.modelPrices[providerId + "-images/" + generations[0].body.model].output, 20,
        "refresh preserves the manual image price override");
    }
    const denied = await fetch(local + "/v1/images/generations", { method: "POST", headers,
      body: JSON.stringify({ model: "not-in-snapshot", prompt: "must not dispatch" }) });
    assert.equal(denied.status, 403);
    const tools = await fetch(local + "/mcp", { method: "POST", headers,
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }) }).then(r => r.json());
    assert.deepEqual(tools.result.tools[0].inputSchema.properties.model.enum, imageIds);
    const malformed = await fetch(local + "/mcp", { method: "POST", headers,
      body: JSON.stringify({ jsonrpc: "2.0", method: 7 }) });
    assert.equal(malformed.status, 400);
    assert.equal(generations.length, 2, "rejected calls never reach the simulated APIM");
    if (stop === "logout") await writeFile(authPath, "{}");
    if (stop === "disable") await writeFile(join(settings, "plugins.json"), JSON.stringify({ plugins: [{ spec, off: true }] }));
    if (stop === "remove") await writeFile(join(settings, "plugins.json"), '{"plugins":[]}');
    if (stop === "foreign-slot") {
      const own = accounts[state.accountSlot]; delete accounts[state.accountSlot]; accounts.unrelated = own;
      await writeFile(authPath, JSON.stringify(accounts));
    }
    if (stop === "gateway-tamper") await writeFile(statePath, JSON.stringify({ ...state, baseURL: "https://attacker.example/llm" }));
    if (stop === "model-tamper") await writeFile(statePath, JSON.stringify({ ...state, images: [{ id: "not-in-snapshot" }] }));
    try {
      const stopped = await fetch(local + "/v1/images/generations", { method: "POST", headers,
        body: JSON.stringify({ model: imageIds[0], prompt: "must not dispatch after stop" }) });
      assert.equal(stopped.status, 403);
    } catch (error) { if (!(error instanceof TypeError)) throw error; }
    await eventually(async () => {
      try { await readFile(readyPath); } catch (error) { if (error.code === "ENOENT") return; throw error; }
      throw new Error("companion has not stopped");
    }, "subscription companion did not stop after " + stop, 70);
    assert.equal(generations.length, 2);
    assert.ok(!platformCalls.some(path => path.includes("revoke") || path.includes("client-runtime")),
      "subscription mode must not query personal OAuth/quota APIs or revoke an APIM key");
  }
  } catch (error) { console.error("Native fixture failure: " + error.stack); throw error; }
});
