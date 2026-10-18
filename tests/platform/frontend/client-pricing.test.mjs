import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { costForHost, syncPricing, tokenCost } from "../../../backend/services/client_plugin/pricing.mjs";

test("prices preserve explicit zero and unknown values, including cache read and writes", () => {
  assert.deepEqual(tokenCost({ input: 2, output: 10, cache_read: .2, cache_write: 2.5 }),
    { input: 2, output: 10, cache: { read: .2, write: 2.5 } });
  assert.deepEqual(tokenCost({ input: 0, output: 0, cache: { read: 0, write: 0 } }),
    { input: 0, output: 0, cache: { read: 0, write: 0 } });
  for (const cost of [null, { input: 2, output: 10 }, { input: 2, output: NaN, cache_read: 0, cache_write: 0 },
    { input: 2, output: 10, cache_read: -1, cache_write: 0 }]) assert.equal(tokenCost(cost), null);
  const raw = { input: 2, output: 10, cache_read: .2, cache_write: 2.5 };
  assert.equal(costForHost({ project: { id: "magpie" } }, "aigateway-user-edwin", "m", raw, { resolved: [] }), null);
  assert.deepEqual(costForHost({ project: { id: "opencode" } }, "aigateway-user-edwin", "m", raw, { resolved: [] }), tokenCost(raw));
});

for (const providerId of ["aigateway-user-edwin-test", "aigateway-user-edwin-test-images",
  "aigateway-subscription-edwin-test", "aigateway-subscription-edwin-test-images"])
test(`${providerId}: CLI imports only owned prices, refreshes them and preserves manual overrides`, async () => {
  const root = await mkdtemp(join(tmpdir(), "ts-native-pricing-")), directory = join(root, "magpie");
  await mkdir(directory);
  const settings = join(directory, "settings.json"), cfg = { providerId, authorization: "DO_NOT_EXPORT" };
  const input = { directory, project: { id: "magpie" } };
  const unrelated = { input: 1, output: 3, cache_read: .1, cache_write: .2 };
  let calls = 0;
  const execute = async (cli, args, options) => {
    calls++;
    assert.equal(cli, "/fixture/magpie");
    assert.equal(options.env.AIGATEWAY_PRICING_WORKER, "1");
    assert.equal(options.env.XDG_CONFIG_HOME, root);
    assert.deepEqual(args.slice(0, 2), ["model", "price"]);
    assert.ok(args[2].startsWith(cfg.providerId + "/"));
    const data = JSON.parse(await readFile(settings, "utf8"));
    if (args[3] === "--reset") delete data.modelPrices[args[2]];
    else {
      const [input, output, cache_read, cache_write, oneHour] = args[3].split(",").map(Number);
      data.modelPrices[args[2]] = { input, output, cache_read, cache_write,
        ...(oneHour > 0 ? { cache_write_1h: oneHour } : {}) };
    }
    await writeFile(settings, JSON.stringify(data));
  };
  try {
    await writeFile(settings, JSON.stringify({ theme: "light", modelPrices: { "copilot/m": unrelated } }));
    const models = { m: { cost: { input: 2, output: 10, cache_read: .2, cache_write: 2.5 } } };
    let status = await syncPricing(input, cfg, models, { executable: "/fixture/magpie", execute });
    assert.equal(status.state, "applied");
    assert.equal(calls, 1);
    assert.deepEqual(status.resolved, [cfg.providerId + "/m"]);
    assert.deepEqual(costForHost(input, cfg.providerId, "m", models.m.cost, status), tokenCost(models.m.cost));
    await syncPricing(input, cfg, models, { executable: "/fixture/magpie", execute });
    assert.equal(calls, 1);
    models.m.cost.cache_read = .125;
    await syncPricing(input, cfg, models, { executable: "/fixture/magpie", execute });
    assert.equal(calls, 2);
    let data = JSON.parse(await readFile(settings, "utf8"));
    assert.equal(data.theme, "light");
    assert.deepEqual(data.modelPrices["copilot/m"], unrelated);
    data.modelPrices[cfg.providerId + "/m"].output = 99;
    await writeFile(settings, JSON.stringify(data));
    models.m.cost.output = 20;
    status = await syncPricing(input, cfg, models, { executable: "/fixture/magpie", execute });
    assert.equal(status.state, "preserved_manual");
    assert.equal(calls, 2);
    assert.equal(JSON.parse(await readFile(settings, "utf8")).modelPrices[cfg.providerId + "/m"].output, 99);
    const exported = await readFile(join(directory, "aigateway-pricing", cfg.providerId, "prices.json"), "utf8");
    assert.ok(!exported.includes("DO_NOT_EXPORT"));
    assert.equal(JSON.parse(exported).prices[cfg.providerId + "/m"].cache_write_1h, 2.5);
    data = JSON.parse(await readFile(settings, "utf8"));
    delete data.modelPrices[cfg.providerId + "/m"];
    data.modelPrices[cfg.providerId + "/*"] = unrelated;
    await writeFile(settings, JSON.stringify(data));
    status = await syncPricing(input, cfg, models, { executable: "/fixture/magpie", execute });
    assert.equal(status.state, "preserved_manual");
    assert.equal(calls, 2);
    assert.equal(JSON.parse(await readFile(settings, "utf8")).modelPrices[cfg.providerId + "/m"], undefined);
    data = JSON.parse(await readFile(settings, "utf8"));
    delete data.modelPrices[cfg.providerId + "/*"];
    data.modelPrices["*/m"] = unrelated;
    await writeFile(settings, JSON.stringify(data));
    status = await syncPricing(input, cfg, models, { executable: "/fixture/magpie", execute });
    assert.equal(status.state, "preserved_manual");
    assert.equal(calls, 2);
    assert.equal(JSON.parse(await readFile(settings, "utf8")).modelPrices[cfg.providerId + "/m"], undefined);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("failed or missing native CLI does not replace settings or claim caches are free", async () => {
  const root = await mkdtemp(join(tmpdir(), "ts-pricing-failure-")), directory = join(root, "magpie");
  await mkdir(directory);
  const cfg = { providerId: "aigateway-user-failure-test" }, input = { directory, project: { id: "magpie" } };
  try {
    const original = '{"theme":"light","modelPrices":{}}';
    await writeFile(join(directory, "settings.json"), original);
    const models = { m: { cost: { input: 2, output: 10, cache_read: 1, cache_write: 2 } } };
    const status = await syncPricing(input, cfg, models, { executable: "/missing/magpie", execute: async () => { throw new Error("unavailable"); } });
    assert.equal(status.state, "pending");
    assert.equal(await readFile(join(directory, "settings.json"), "utf8"), original);
    assert.equal(costForHost(input, cfg.providerId, "m", models.m.cost, status), null);
    const missing = await syncPricing(input, cfg, models, { resolveExecutable: async () => null });
    assert.equal(missing.state, "pending");
    assert.equal(await readFile(join(directory, "settings.json"), "utf8"), original);
  } finally { await rm(root, { recursive: true, force: true }); }
});
