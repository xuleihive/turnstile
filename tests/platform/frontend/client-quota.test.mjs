import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { personalQuota, quotaHTML, quotaRefusal, recordQuota } from "../../../backend/services/client_user_plugin/quota.mjs";

const cfg = { userId: "stable-user" };
const usage = (at, amount, aside = true) => ({ user: 'Edwin<script>alert(1)</script>', as_of: at,
  windows: [{ name: "个人月度 Token", amount, limit: 1000, unit: "tokens", aside,
    resetsAt: "2026-11-01T00:00:00Z", span: 31 * 86400 }] });

test("USD allowances retain their unit, local blocking and a separate history from Tokens", async () => {
  const directory = await mkdtemp(join(tmpdir(), "ts-usd-quota-"));
  try {
    await recordQuota(directory, cfg, usage("2026-10-13T00:00:00Z", 100));
    const money = usage("2026-10-13T00:01:00Z", 0.01234567, false);
    money.windows[0] = { ...money.windows[0], name: "个人月度 USD", unit: "USD", limit: 10, used: 0.1234567 };
    assert.equal(personalQuota(money), money.windows[0]);
    assert.equal(quotaRefusal(money, true), null);
    const file = await recordQuota(directory, cfg, money);
    const html = await readFile(file, "utf8");
    assert.ok(html.includes("0.01234567 / 10 USD"));
    assert.ok(!html.includes("0.01234567 / 10 Token"));
    const history = JSON.parse(await readFile(join(directory, "quota-history.json"), "utf8"));
    assert.equal(history.windows.length, 2);
    assert.equal(history.windows[0].points[0].amount, 100);
    assert.equal(history.windows[1].unit, "USD");
    money.windows[0].amount = 10;
    assert.equal(quotaRefusal(money, true).status, 429);
    money.windows[0].unit = "tokens";
    assert.throws(() => personalQuota(money), /无效/);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test("private trends capture only real personal snapshots, keep audit mode and never change host history", async () => {
  const directory = await mkdtemp(join(tmpdir(), "ts-quota-trend-"));
  try {
    const first = usage("2026-10-13T00:00:00Z", 100);
    const before = JSON.stringify(first);
    const file = await recordQuota(directory, cfg, first);
    await recordQuota(directory, cfg, first);
    await recordQuota(directory, cfg, usage("2026-10-13T00:01:00Z", 200));
    await recordQuota(directory, cfg, usage("2026-10-13T00:02:00Z", 250));
    const history = JSON.parse(await readFile(join(directory, "quota-history.json"), "utf8"));
    assert.deepEqual(history.windows[0].points.map(point => point.amount), [100, 250]);
    assert.ok(history.windows[0].points.every(point => point.aside));
    assert.equal(JSON.stringify(first), before);
    const html = await readFile(file, "utf8");
    assert.ok(html.includes("<polyline"));
    assert.ok(html.includes("仅记录，不阻断"));
    assert.ok(html.includes("不是 magpie 原生卡片"));
    assert.ok(html.includes("Edwin&lt;script&gt;"));
    assert.ok(!html.includes("<script>alert(1)</script>"));
    assert.ok(!html.includes("stable-user"));
    // POSIX modes are not an ACL assertion on Windows.
    if (process.platform !== "win32") {
      assert.equal((await stat(file)).mode & 0o777, 0o600);
      assert.equal((await stat(join(directory, "quota-history.json"))).mode & 0o777, 0o600);
    }
    assert.ok(!quotaHTML(history, { ...first, windows: [] }).includes("<polyline"));
    await assert.rejects(recordQuota(directory, { userId: "other-user" }, first), /does not belong/);
    assert.equal(JSON.parse(await readFile(join(directory, "quota-history.json"), "utf8")).userId, cfg.userId);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test("simultaneous samples serialize, and missing budget never invents zero consumption", async () => {
  const directory = await mkdtemp(join(tmpdir(), "ts-quota-concurrent-"));
  try {
    await Promise.all([recordQuota(directory, cfg, usage("2026-10-13T00:00:00Z", 100)),
      recordQuota(directory, cfg, usage("2026-10-13T00:10:00Z", 200))]);
    const history = JSON.parse(await readFile(join(directory, "quota-history.json"), "utf8"));
    assert.equal(history.windows[0].points.at(-1).amount, 200);
    assert.ok(history.windows[0].points.every(point => point.amount > 0));
    await recordQuota(directory, cfg, { user: "Edwin", as_of: "2026-10-13T00:15:00Z", windows: [] });
    assert.ok((await readFile(join(directory, "quota-trend.html"), "utf8")).includes("未配置个人额度"));
    await writeFile(join(directory, "quota-history.json"), "corrupt");
    await assert.rejects(recordQuota(directory, cfg, usage("2026-10-13T00:20:00Z", 300)));
    assert.equal(await readFile(join(directory, "quota-history.json"), "utf8"), "corrupt");
  } finally { await rm(directory, { recursive: true, force: true }); }
});
