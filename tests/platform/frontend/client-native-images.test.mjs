import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { isAbsolute, join, relative } from "node:path";
import { tmpdir } from "node:os";
import { imageFileLock, removeNativeImages, syncNativeImages } from "../../../backend/services/client_user_plugin/native-images.mjs";

const model = "gpt-image-2-foundry-d8c6686a";

async function fixture(t, kind) {
  const root = await mkdtemp(join(tmpdir(), "ts-native-images-")), directory = join(root, "magpie");
  await mkdir(directory);
  const cfg = { providerId: `aigateway-${kind}-native-fixture`, installationId: "fixture-install",
    ...(kind === "subscription" ? { kind, subscriptionId: "fixture-subscription" } : { userId: "fixture-user" }) };
  const picture = { id: cfg.providerId + "-images", name: "AIGateway User Images", chat: "http://127.0.0.1:12345/v1",
    key: "fixture-local-nonce", models: [model, "gpt-image-2.5-flare-foundry-d8c6686a"] };
  const input = { directory, project: { id: "magpie" } }, path = join(directory, "providers.json");
  const managed = join(directory, kind === "subscription" ? "aigateway-proxy-subscription" : "aigateway-proxy-user", cfg.providerId);
  const unrelated = { id: "untouched", name: "Keep provider", key: "other-provider-secret", off: true, models: ["keep"] };
  const original = { providers: [unrelated], groups: [{ id: "keep-group", name: "Keep group", models: ["untouched/keep"] }] };
  await writeFile(path, JSON.stringify(original));
  const settings = '{"imageGen":"auto","theme":"light","modelPrices":{"other/m":{"input":1}}}';
  await writeFile(join(directory, "settings.json"), settings);
  const read = async () => JSON.parse(await readFile(path, "utf8"));
  const save = document => writeFile(path, JSON.stringify(document));
  const calls = [], warnings = [];
  const options = { resolveExecutable: async () => "/fixture/magpie", warn: message => warnings.push(message),
    execute: async (cli, args, options) => {
      calls.push(args);
      assert.equal(cli, "/fixture/magpie");
      assert.equal(options.windowsHide, true);
      assert.equal(options.env.XDG_CONFIG_HOME, root);
      assert.equal(options.env.AIGATEWAY_NATIVE_IMAGE_WORKER, "1");
      assert.equal(options.env.AIGATEWAY_PRICING_WORKER, "1");
      assert.ok(!JSON.stringify(args).includes("other-provider-secret"));
      const document = await read(), action = args[1];
      if (action === "rm") document.providers = document.providers.filter(p => p.id !== args[2]);
      else {
        const values = Object.fromEntries(args.slice(3).map(pair => [pair.slice(0, pair.indexOf("=")), pair.slice(pair.indexOf("=") + 1)]));
        if (values.models) values.models = values.models.split(",");
        if (action === "add") document.providers.push({ name: args[2], ...values });
        else Object.assign(document.providers.find(p => p.id === args[2]), values);
      }
      await save(document);
    } };
  t.after(async () => {
    assert.deepEqual((await read()).providers.find(p => p.id === unrelated.id), unrelated);
    assert.deepEqual((await read()).groups, original.groups);
    assert.equal(await readFile(join(directory, "settings.json"), "utf8"), settings);
    const target = await realpath(root), parent = await realpath(tmpdir()), inside = relative(parent, target);
    assert.ok(inside && !inside.startsWith("..") && !isAbsolute(inside));
    await rm(target, { recursive: true, force: true });
  });
  return { root, directory, cfg, picture, input, path, managed, original, read, save, calls, warnings, options };
}

for (const kind of ["user", "subscription"]) {

test(`${kind}: native registration preserves original gateway image IDs and skips unchanged updates`, async t => {
  const f = await fixture(t, kind);
  let status = await syncNativeImages(f.input, f.cfg, f.picture, f.options);
  assert.equal(status.state, "applied");
  assert.equal(status.reason, "created");
  assert.deepEqual((await f.read()).providers.at(-1).models, f.picture.models);
  assert.equal(f.calls.length, 1);
  const record = JSON.parse(await readFile(join(f.managed, "native-images-managed.json"), "utf8"));
  assert.equal(record[kind === "subscription" ? "subscriptionId" : "userId"], f.cfg[kind === "subscription" ? "subscriptionId" : "userId"]);
  assert.equal(record.installationId, f.cfg.installationId);
  status = await syncNativeImages(f.input, f.cfg, f.picture, f.options);
  assert.equal(status.state, "applied");
  assert.equal(status.reason, "unchanged");
  assert.equal(status.cli, "/fixture/magpie");
  assert.equal(f.calls.length, 1);
});

test(`${kind}: owned registration updates endpoint, nonce and catalog without re-enabling a disabled card`, async t => {
  const f = await fixture(t, kind);
  await syncNativeImages(f.input, f.cfg, f.picture, f.options);
  const document = await f.read();
  document.providers.at(-1).off = true;
  await f.save(document);
  const changed = { ...f.picture, chat: "http://127.0.0.1:12346/v1", key: "new-local-nonce", models: [model] };
  const status = await syncNativeImages(f.input, f.cfg, changed, f.options);
  assert.equal(status.state, "applied");
  assert.equal(status.disabled, true);
  assert.equal(f.calls[1][1], "set");
  assert.deepEqual((await f.read()).providers.at(-1), { ...changed, off: true });
  assert.equal((await removeNativeImages(f.input, f.cfg, f.options)).state, "preserved_manual");
});

test(`${kind}: existing IDs and legacy ID-only markers cannot grant ownership`, async t => {
  const f = await fixture(t, kind);
  const document = await f.read();
  document.providers.push({ ...f.picture });
  await f.save(document);
  await mkdir(f.managed, { recursive: true });
  await writeFile(join(f.managed, "images-owned.json"), JSON.stringify({ providerId: f.picture.id }));
  const before = await readFile(f.path, "utf8");
  assert.equal((await syncNativeImages(f.input, f.cfg, f.picture, f.options)).state, "conflict");
  assert.equal((await removeNativeImages(f.input, f.cfg, f.options)).state, "conflict");
  assert.equal(await readFile(f.path, "utf8"), before);
  assert.equal(f.calls.length, 0);
});

test(`${kind}: manual native settings and foreign installation manifests are preserved`, async t => {
  const f = await fixture(t, kind);
  await syncNativeImages(f.input, f.cfg, f.picture, f.options);
  const document = await f.read();
  document.providers.at(-1).headers = { "X-Manual": "keep" };
  await f.save(document);
  assert.equal((await syncNativeImages(f.input, f.cfg, f.picture, f.options)).state, "preserved_manual");
  assert.equal((await removeNativeImages(f.input, f.cfg, f.options)).state, "preserved_manual");
  assert.equal((await syncNativeImages(f.input, { ...f.cfg, installationId: "another-install" }, f.picture, f.options)).state, "conflict");
  assert.equal(f.calls.length, 1);
});

test(`${kind}: only a matching, unmodified owned provider is removed`, async t => {
  const f = await fixture(t, kind);
  await syncNativeImages(f.input, f.cfg, f.picture, f.options);
  const status = await removeNativeImages(f.input, f.cfg, f.options);
  assert.equal(status.state, "removed");
  assert.deepEqual(await f.read(), f.original);
  await assert.rejects(readFile(join(f.managed, "native-images-managed.json")), { code: "ENOENT" });
  assert.equal((await removeNativeImages(f.input, f.cfg, f.options)).state, "absent");
  assert.equal(f.calls.length, 2);
});

test(`${kind}: unavailable CLI, failures and mismatched readback are reported without credentials`, async t => {
  const f = await fixture(t, kind);
  const missing = await syncNativeImages(f.input, f.cfg, f.picture, { ...f.options, resolveExecutable: async () => null });
  assert.equal(missing.reason, "cli-unavailable");
  const broken = await syncNativeImages(f.input, f.cfg, f.picture, { ...f.options,
    execute: async () => { throw new Error("private " + f.picture.key + " tsca_secret tscr_secret tsci_secret"); } });
  assert.equal(broken.state, "pending");
  const mismatch = await syncNativeImages(f.input, f.cfg, f.picture, { ...f.options, execute: async () => {} });
  assert.equal(mismatch.state, "pending");
  const status = await readFile(join(f.managed, "native-images-status.json"), "utf8");
  for (const secret of [f.picture.key, "tsca_secret", "tscr_secret", "tsci_secret", "other-provider-secret"])
    assert.ok(!(status + f.warnings.join("\n")).includes(secret));
  assert.deepEqual(await f.read(), f.original);
});

test(`${kind}: image descriptors reject external URLs, renamed IDs and ambiguous model lists`, async t => {
  const f = await fixture(t, kind);
  for (const change of [{ chat: "https://external.example/v1" }, { id: "other-provider" },
    { models: ["gpt-image-2,other"] }, { models: [model, model] }]) {
    assert.equal((await syncNativeImages(f.input, f.cfg, { ...f.picture, ...change }, f.options)).state, "pending");
  }
  assert.equal(f.calls.length, 0);
});

test(`${kind}: a CLI timeout after saving is recovered through verified readback`, async t => {
  const f = await fixture(t, kind);
  const options = { ...f.options, execute: async (...args) => {
    await f.options.execute(...args);
    throw new Error("discovery timed out after save");
  } };
  assert.equal((await syncNativeImages(f.input, f.cfg, f.picture, options)).state, "applied");
  assert.equal((await removeNativeImages(f.input, f.cfg, options)).state, "removed");
  assert.deepEqual(await f.read(), f.original);
});

test(`${kind}: registration locks coalesce work, reject active owners and recover a dead owner`, async t => {
  const f = await fixture(t, kind);
  await mkdir(f.managed, { recursive: true });
  const path = join(f.managed, "native-images.lock"), release = await imageFileLock(path);
  try { assert.equal((await syncNativeImages(f.input, f.cfg, f.picture, f.options)).state, "busy"); }
  finally { await release(); }
  await writeFile(path, JSON.stringify({ type: "aigateway-image-lock", pid: 2147483647 }));
  assert.equal((await syncNativeImages(f.input, f.cfg, f.picture, f.options)).state, "applied");
});

test(`${kind}: maintenance workers do not register providers or write registration state`, async t => {
  const f = await fixture(t, kind), previous = process.env.AIGATEWAY_NATIVE_IMAGE_WORKER;
  process.env.AIGATEWAY_NATIVE_IMAGE_WORKER = "1";
  try { assert.equal((await syncNativeImages(f.input, f.cfg, f.picture, f.options)).state, "worker"); }
  finally {
    if (previous === undefined) delete process.env.AIGATEWAY_NATIVE_IMAGE_WORKER;
    else process.env.AIGATEWAY_NATIVE_IMAGE_WORKER = previous;
  }
  assert.equal(f.calls.length, 0);
  await assert.rejects(readFile(join(f.managed, "native-images-status.json")), { code: "ENOENT" });
});

}
