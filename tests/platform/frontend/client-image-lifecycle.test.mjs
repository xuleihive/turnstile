import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createServer } from "node:http";
import { mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join, posix, relative, win32 } from "node:path";
import { fileURLToPath } from "node:url";
import * as images from "../../../backend/services/client_user_plugin/images.mjs";

const bridgeFile = fileURLToPath(new URL("../../../backend/services/client_user_plugin/images.mjs", import.meta.url));
const bun = process.env.AIGATEWAY_TEST_BUN;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), "ts-image-paths-"));
  const cleanup = [];
  const spec = join(root, "plugin space \u7814\u53d1"), settings = join(root, "settings");
  await mkdir(spec);
  await mkdir(settings);
  const state = { providerId: "aigateway-user-lifecycle-fixture", userId: "fixture-user", spec,
    settingsDirectory: settings, platformOrigin: "https://platform.example", magpieAddress: "127.0.0.1:3425",
    images: [{ id: "fixture-image" }], capability: "tsci_fixture", nonce: "fixture-local-nonce" };
  const auth = { type: "oauth", uid: state.userId, access: "tsca_fixture" };
  const savePlugins = entries => writeFile(join(settings, "plugins.json"), JSON.stringify({ plugins: entries }));
  const saveAuth = accounts => writeFile(join(settings, "plugin-auth.json"), JSON.stringify(accounts));
  await savePlugins([{ spec }]);
  await saveAuth({ [state.providerId]: auth });
  t.after(async () => {
    for (const close of cleanup.reverse()) await close();
    const target = await realpath(root), parent = await realpath(tmpdir()), inside = relative(parent, target);
    assert.ok(inside && !inside.startsWith("..") && !isAbsolute(inside), "cleanup stays in the temp directory");
    await rm(target, { recursive: true, force: true });
  });
  return { root, spec, settings, state, auth, savePlugins, saveAuth, cleanup };
}

test("native absolute-path contracts cover POSIX, Windows drives and UNC", () => {
  assert.equal(posix.isAbsolute("/Users/person/plugin space"), true);
  assert.equal(posix.isAbsolute("./plugin"), false);
  assert.equal(win32.isAbsolute("C:\\Development\\plugin space"), true);
  assert.equal(win32.isAbsolute("C:/Development/plugin"), true);
  assert.equal(win32.isAbsolute("\\\\server\\share\\plugin"), true);
  assert.equal(win32.isAbsolute("C:plugin"), false);
  assert.equal(win32.isAbsolute(".\\plugin"), false);
});

test("image ownership accepts native absolute directories with spaces and Unicode", async t => {
  const f = await fixture(t);
  assert.equal(isAbsolute(f.spec), true);
  assert.equal(await images.imagePluginEnabled(f.state), true);
  if (process.platform === "win32") {
    await f.savePlugins([{ spec: f.spec.replaceAll("\\", "/") }]);
    assert.equal(await images.imagePluginEnabled(f.state), true);
  }
});

test("canonical directory links retain ownership without accepting unrelated directories", async t => {
  const f = await fixture(t), link = join(f.root, "plugin-link");
  await symlink(f.spec, link, process.platform === "win32" ? "junction" : "dir");
  await f.savePlugins([{ spec: link }]);
  assert.equal(await images.imagePluginEnabled(f.state), true);
  await f.savePlugins([{ spec: f.settings }]);
  assert.equal(await images.imagePluginEnabled(f.state), false);
});

test("malformed, missing and relative registrations cannot enable the image bridge", async t => {
  const f = await fixture(t);
  const invalid = [null, "not-registration", {}, { spec: null }, { spec: 42 }, { spec: {} }, { spec: "" },
    { spec: "./plugin" }, { spec: "C:plugin" }, { spec: join(f.root, "missing") }];
  await f.savePlugins(invalid);
  assert.equal(await images.imagePluginEnabled(f.state), false);
  await f.savePlugins([...invalid, { spec: f.spec }]);
  assert.equal(await images.imagePluginEnabled(f.state), true);
});

test("disabled and removed registrations remain rejected", async t => {
  const f = await fixture(t);
  await f.savePlugins([{ spec: f.spec, off: true }]);
  assert.equal(await images.imagePluginEnabled(f.state), false);
  await f.savePlugins([]);
  assert.equal(await images.imagePluginEnabled(f.state), false);
});

test("login checks reject logout, foreign identity and unrelated providers", async t => {
  const f = await fixture(t);
  for (const accounts of [{}, { [f.state.providerId]: { ...f.auth, uid: "foreign-user" } },
    { unrelated: f.auth }, { [f.state.providerId]: { ...f.auth, access: "" } },
    { [f.state.providerId]: { ...f.auth, type: "api" } }]) {
    await f.saveAuth(accounts);
    assert.equal(await images.imagePluginEnabled(f.state), false);
  }
  await f.saveAuth({ [f.state.providerId + "#account"]: f.auth });
  assert.equal(await images.imagePluginEnabled(f.state), true);
});

async function runningBridge(t) {
  const f = await fixture(t), requests = [];
  const platform = createServer((req, res) => {
    requests.push({ path: req.url, method: req.method, authorization: req.headers.authorization });
    res.setHeader("content-type", "application/json");
    res.end("{}");
  });
  f.cleanup.push(async () => {
    platform.closeAllConnections();
    await new Promise(resolve => platform.close(resolve));
  });
  platform.listen(0, "127.0.0.1");
  await once(platform, "listening");
  const origin = `http://127.0.0.1:${platform.address().port}`;
  f.state.platformOrigin = origin;
  f.state.magpieAddress = origin;
  await writeFile(join(f.spec, "aigateway-user.config.json"), JSON.stringify({
    platformOrigin: origin, providerId: f.state.providerId, userId: f.state.userId,
  }));
  const statePath = join(f.root, "state.json"), readyPath = join(f.root, "ready.json");
  await writeFile(statePath, JSON.stringify(f.state));
  const child = spawn(bun, ["run", bridgeFile, statePath, readyPath], { windowsHide: true,
    stdio: ["ignore", "ignore", "pipe"] });
  let stderr = "";
  child.stderr.on("data", bytes => { stderr += bytes.toString(); });
  const exited = once(child, "exit");
  exited.catch(() => {});
  f.cleanup.push(async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill();
    await exited;
  });
  let ready;
  for (let i = 0; i < 100; i++) {
    try { ready = JSON.parse(await readFile(readyPath, "utf8")); break; } catch { /* Wait for startup. */ }
    assert.equal(child.exitCode, null, stderr);
    await pause(50);
  }
  assert.ok(ready, "Bun bridge starts before the deadline: " + stderr);
  const url = `http://127.0.0.1:${ready.port}`;
  const headers = { Authorization: "Bearer " + f.state.nonce, "content-type": "application/json" };
  const request = (path, init = {}) => fetch(url + path, { ...init, headers: { ...headers, ...init.headers },
    signal: AbortSignal.timeout(5000) });
  return { ...f, requests, child, exited, readyPath, request };
}

test("native Bun bridge stays alive and serves authenticated image/MCP discovery", { skip: !bun }, async t => {
  const f = await runningBridge(t);
  assert.equal((await f.request("/health")).status, 200);
  assert.equal((await f.request("/v1/models", { headers: { Authorization: "wrong" } })).status, 401);
  assert.equal((await f.request("/v1/models", { headers: { Origin: "https://foreign.example" } })).status, 401);
  const models = await f.request("/v1/models");
  assert.equal(models.status, 200);
  assert.deepEqual((await models.json()).data.map(m => m.id), ["fixture-image"]);
  const rpc = method => f.request("/mcp", { method: "POST", body: JSON.stringify({ jsonrpc: "2.0", id: 1, method }) });
  const init = await rpc("initialize");
  assert.equal(init.status, 200);
  assert.equal((await init.json()).result.serverInfo.name, "aigateway-user-images");
  const tools = await rpc("tools/list");
  assert.equal(tools.status, 200);
  assert.deepEqual((await tools.json()).result.tools[0].inputSchema.properties.model.enum, ["fixture-image"]);
  await pause(4500);
  assert.equal((await f.request("/v1/models")).status, 200);
  assert.equal(f.child.exitCode, null);
  assert.deepEqual(f.requests, [], "discovery does not call the platform or generate images");
});

for (const change of ["disable", "remove", "logout", "foreign-user"]) {
  test(`native Bun bridge revokes its capability and stops after ${change}`, { skip: !bun }, async t => {
    const f = await runningBridge(t);
    if (change === "disable") await f.savePlugins([{ spec: f.spec, off: true }]);
    if (change === "remove") await f.savePlugins([]);
    if (change === "logout") await f.saveAuth({});
    if (change === "foreign-user") await f.saveAuth({ [f.state.providerId]: { ...f.auth, uid: "foreign" } });
    assert.equal((await f.request("/v1/models")).status, 403);
    let timeout;
    try {
      const result = await Promise.race([f.exited, new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error("bridge did not stop")), 7000);
      })]);
      assert.equal(result[0], 0);
    } finally { clearTimeout(timeout); }
    await assert.rejects(readFile(f.readyPath), { code: "ENOENT" });
    assert.deepEqual(f.requests, [
      { path: "/api/v1/client-auth/revoke", method: "POST", authorization: "Bearer tsci_fixture" },
    ]);
  });
}
