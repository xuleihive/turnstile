import { test } from "node:test";
import assert from "node:assert/strict";
import { copyFile, mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { localQuotaControl, quotaForMagpie, quotaReader, quotaRefusal, saveQuotaControl } from "../../../backend/services/client_user_plugin/quota.mjs";
import { imageQuotaGuard } from "../../../backend/services/client_user_plugin/images.mjs";

const providerId = "aigateway-user-control-fixture", cfg = { userId: "stable-user" };
const reading = aside => ({ user: "Edwin", as_of: "2026-10-13T00:00:00Z", windows: [
  { name: "个人月度 Token", amount: 110, limit: 100, used: 100, unit: "tokens", aside,
    span: 31 * 86400, resetsAt: "2026-11-01T00:00:00Z" },
] });

async function fixture(run) {
  const root = await mkdtemp(join(tmpdir(), "ts-quota-control-")), folder = join(root, "magpie");
  const previous = globalThis.fetch;
  try {
    await mkdir(folder);
    for (const file of ["index.mjs", "images.mjs", "native-images.mjs", "helpers.mjs", "quota.mjs"])
      await copyFile(new URL("../../../backend/services/client_user_plugin/" + file, import.meta.url), join(folder, file));
    await copyFile(new URL("../../../backend/services/client_plugin/pricing.mjs", import.meta.url), join(folder, "pricing.mjs"));
    await writeFile(join(folder, "aigateway-user.config.json"), JSON.stringify({ version: 4, platformOrigin: "https://platform.example",
      ...cfg, providerId, userName: "Edwin", name: "User" }));
    const auth = { type: "oauth", uid: cfg.userId, accountId: "Edwin", access: "fixture-access", refresh: "fixture-refresh", expires: Date.now() + 900000 };
    await writeFile(join(folder, "plugin-auth.json"), JSON.stringify({ [providerId]: auth, other: { type: "api", key: "keep" } }));
    const current = { usage: reading(true), requests: [] };
    globalThis.fetch = async (url, init) => {
      current.requests.push(String(url));
      assert.equal(init.headers.Authorization, "Bearer fixture-access");
      assert.equal(init.method, "GET");
      assert.ok(String(url).endsWith("/usage") || String(url).endsWith("/usage?fresh=true"));
      return Response.json(current.usage);
    };
    const plugin = await (await import(pathToFileURL(join(folder, "index.mjs")))).default({ directory: folder, project: { id: "magpie" } });
    const directory = join(folder, "aigateway-proxy-user", providerId);
    await run({ plugin, action: plugin.auth.methods[3], auth, directory, folder, current });
  } finally { globalThis.fetch = previous; await rm(root, { recursive: true, force: true }); }
}

test("one click toggles audit-mode blocking without prompts, policy changes or credential changes", async () => {
  await fixture(async ({ plugin, action, auth, directory, folder, current }) => {
    const before = await readFile(join(folder, "plugin-auth.json"), "utf8");
    assert.equal(action.label, "额度控制");
    assert.equal(action.prompts, undefined);
    assert.equal(action.ask, undefined);
    assert.equal((await plugin.auth.usage(async () => auth)).windows[0].aside, true);
    const choice = await action.authorize();
    assert.match(choice.instructions, /阻断开启/);
    assert.equal(await localQuotaControl(directory, cfg), false);
    assert.equal(choice.url, "");
    const saved = await choice.callback();
    assert.equal(saved.access, auth.access);
    assert.equal(saved.refresh, auth.refresh);
    assert.equal(saved.uid, auth.uid);
    assert.equal(await localQuotaControl(directory, cfg), true);
    await choice.callback();
    assert.equal(await localQuotaControl(directory, cfg), true);
    // POSIX modes are not an ACL assertion on Windows.
    if (process.platform !== "win32")
      assert.equal((await stat(join(directory, "quota-control.json"))).mode & 0o777, 0o600);
    assert.equal((await plugin.auth.usage(async () => auth)).windows[0].aside, false);
    assert.equal(current.usage.windows[0].aside, true);
    const history = JSON.parse(await readFile(join(directory, "quota-history.json"), "utf8"));
    assert.equal(history.windows[0].points[0].aside, true);
    assert.equal(await readFile(join(folder, "plugin-auth.json"), "utf8"), before);
    const off = await action.authorize();
    assert.match(off.instructions, /阻断关闭/);
    await off.callback();
    assert.equal((await plugin.auth.usage(async () => auth)).windows[0].aside, true);
    assert.ok(current.requests.filter(url => url.endsWith("fresh=true")).length >= 4);
  });
});

test("platform blocking hides choices, cannot be disabled and overrides a tampered local preference", async () => {
  await fixture(async ({ plugin, action, auth, directory, current }) => {
    await saveQuotaControl(directory, cfg, false);
    const before = await readFile(join(directory, "quota-control.json"), "utf8");
    current.usage = reading(false);
    assert.equal(action.ask, undefined);
    const locked = await action.authorize();
    assert.match(locked.instructions, /不可关闭/);
    assert.equal((await locked.callback()).access, auth.access);
    assert.equal(await readFile(join(directory, "quota-control.json"), "utf8"), before);
    assert.equal((await plugin.auth.usage(async () => auth)).windows[0].aside, false);
    await writeFile(join(directory, "quota-control.json"), "corrupt");
    assert.equal((await plugin.auth.usage(async () => auth)).windows[0].aside, false);
    assert.equal(await readFile(join(directory, "quota-control.json"), "utf8"), "corrupt");
  });
});

test("saving rechecks policy changes and cancelled clicks never write preferences", async () => {
  await fixture(async ({ action, directory, current }) => {
    await action.authorize();
    await assert.rejects(readFile(join(directory, "quota-control.json")), { code: "ENOENT" });
    await saveQuotaControl(directory, cfg, true);
    const off = await action.authorize();
    current.usage = reading(false);
    await assert.rejects(off.callback(), /不可关闭/);
    assert.equal(await localQuotaControl(directory, cfg), true);
    current.usage = { ...reading(true), windows: [] };
    await assert.rejects(action.authorize(), /未配置/);
    current.usage = reading(undefined);
    await assert.rejects(action.authorize(), /无效/);
  });
});

test("preferences reject foreign or corrupt state; missing budgets never invent a blocking window", async () => {
  const directory = await mkdtemp(join(tmpdir(), "ts-quota-settings-"));
  try {
    assert.equal(await localQuotaControl(directory, cfg), false);
    await saveQuotaControl(directory, cfg, true);
    const original = reading(true), before = JSON.stringify(original);
    const modified = await quotaForMagpie(directory, cfg, original);
    assert.equal(modified.windows[0].aside, false);
    assert.equal(JSON.stringify(original), before);
    assert.equal(modified.windows[0].amount, original.windows[0].amount);
    const none = { windows: [] };
    assert.equal(await quotaForMagpie(directory, cfg, none), none);
    await assert.rejects(localQuotaControl(directory, { userId: "foreign" }), /无效/);
    await writeFile(join(directory, "quota-control.json"), "corrupt");
    await assert.rejects(saveQuotaControl(directory, cfg, false));
    assert.equal(await readFile(join(directory, "quota-control.json"), "utf8"), "corrupt");
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test("local dispatch rejects exhausted quotas, but audit off and unconfigured budgets remain usable", async () => {
  assert.equal(quotaRefusal(reading(true), false), null);
  assert.equal(quotaRefusal({ windows: [] }, true), null);
  assert.equal(quotaRefusal(reading(true), true).status, 429);
  assert.equal(quotaRefusal(reading(false), false).status, 429);
  assert.equal(quotaRefusal({ ...reading(true), windows: [{ ...reading(true).windows[0], amount: 99, used: 99 }] }, true), null);
  await fixture(async ({ plugin, action, auth, current }) => {
    await (await action.authorize()).callback();
    const loader = await plugin.auth.loader(async () => auth);
    const refusal = await loader.fetch(loader.baseURL + "/responses", { method: "POST", body: '{"model":"fixture"}' });
    assert.equal(refusal.status, 429);
    assert.equal((await refusal.json()).error.code, "aigateway_local_quota_exceeded");
    assert.ok(current.requests.every(url => url.includes("/usage")));
  });
});

test("quota reads coalesce without polling and an older response cannot replace a fresh policy", async () => {
  let release, reads = 0;
  const now = Date.now(), old = { ...reading(true), as_of: new Date(now - 1000).toISOString() };
  const newer = { ...reading(false), as_of: new Date(now).toISOString() };
  const read = quotaReader(async (access, fresh) => {
    reads++;
    return fresh || access === "new-token" ? newer : new Promise(resolve => { release = resolve; });
  });
  const first = read("token"), second = read("token");
  assert.equal(reads, 1);
  assert.equal((await read("token", true)).windows[0].aside, false);
  release(old);
  assert.equal((await first).windows[0].aside, false);
  assert.equal((await second).windows[0].aside, false);
  assert.equal((await read("token")).windows[0].aside, false);
  assert.equal(reads, 2);
  await read("new-token");
  assert.equal(reads, 3);
});

test("image local blocking reads quota only, never sends refresh tokens or invokes an image model", async () => {
  await fixture(async ({ folder, directory, auth, current }) => {
    const guard = imageQuotaGuard(), state = { settingsDirectory: folder, providerId, userId: cfg.userId, platformOrigin: "https://platform.example" };
    assert.equal(await guard(state), null);
    assert.equal(current.requests.length, 0);
    await saveQuotaControl(directory, cfg, true);
    assert.equal((await guard(state)).status, 429);
    assert.ok(current.requests.every(url => url.endsWith("/usage")));
    const saved = await readFile(join(folder, "plugin-auth.json"), "utf8");
    assert.ok(saved.includes(auth.refresh));
    current.usage = { ...reading(true), windows: [{ ...reading(true).windows[0], amount: 99, used: 99 }] };
    assert.equal(await guard(state), null);
  });
});
