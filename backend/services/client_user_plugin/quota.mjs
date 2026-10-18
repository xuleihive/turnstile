import { open, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { privateJSON } from "./helpers.mjs";
import { randomUUID } from "node:crypto";

const escape = value => String(value).replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const number = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
const personalUnits = new Map([["个人月度 Token", "tokens"], ["个人月度 USD", "USD"]]);
const windowUnit = window => window.unit ?? (window.name === "个人月度 Token" ? "tokens" : null);
const unitLabel = window => windowUnit(window) === "USD" ? "USD" : "Token";
const formatAmount = (value, window) => value.toLocaleString("en-US", {
  maximumFractionDigits: windowUnit(window) === "USD" ? 8 : 0,
});

export function personalQuota(usage) {
  const windows = (usage.windows ?? []).filter(window => personalUnits.has(window.name));
  if (!windows.length) return null;
  const window = windows[0];
  if (windows.length !== 1 || windowUnit(window) !== personalUnits.get(window.name)
      || typeof window.aside !== "boolean"
      || !number(window.amount) || !number(window.limit) || !window.limit
      || !number(window.used) || window.used > 100
      || !number(window.span) || !window.span || !Number.isFinite(Date.parse(window.resetsAt)))
    throw new Error("平台个人额度信息无效，不能修改额度控制");
  return window;
}

export async function localQuotaControl(directory, cfg) {
  let saved;
  try { saved = JSON.parse(await readFile(join(directory, "quota-control.json"), "utf8")); }
  catch (error) { if (error.code === "ENOENT") return false; throw error; }
  if (saved.version !== 1 || saved.userId !== cfg.userId || typeof saved.enabled !== "boolean")
    throw new Error("本地额度控制配置无效，保留原文件");
  return saved.enabled;
}

export async function saveQuotaControl(directory, cfg, enabled) {
  if (typeof enabled !== "boolean") throw new Error("Invalid local quota control");
  await localQuotaControl(directory, cfg);
  await privateJSON(join(directory, "quota-control.json"), { version: 1, userId: cfg.userId, enabled });
}

export async function quotaForMagpie(directory, cfg, usage) {
  const window = personalQuota(usage);
  // Platform blocking always wins, including over a disabled or corrupt local preference.
  if (!window || !window.aside || !await localQuotaControl(directory, cfg)) return usage;
  return { ...usage, windows: usage.windows.map(item => item === window ? { ...item, aside: false } : item) };
}

export function quotaRefusal(usage, enabled) {
  const window = personalQuota(usage);
  if (!window || window.aside && !enabled || window.amount < window.limit) return null;
  return Response.json({ error: { type: "insufficient_quota", code: "aigateway_local_quota_exceeded",
    message: "个人月度额度已用尽，magpie 插件已在本地阻断；请等待额度重置或联系平台管理员。" } },
  { status: 429, headers: { "cache-control": "no-store" } });
}

export function quotaReader(read) {
  let snapshot, pending;
  return async (access, fresh = false) => {
    if (!fresh && snapshot?.access === access && snapshot.until > Date.now()) return snapshot.usage;
    if (!fresh && pending?.access === access) return pending.promise;
    const promise = (async () => {
      const usage = await read(access, fresh), observed = Date.parse(usage.as_of);
      if (!Number.isFinite(observed)) throw new Error("平台未返回有效的额度快照时间");
      if (!snapshot || snapshot.access !== access || !Number.isFinite(Date.parse(snapshot.usage.as_of))
          || observed >= Date.parse(snapshot.usage.as_of))
        snapshot = { access, usage, until: Math.min(Date.now() + 60000, Number.isFinite(observed) ? observed + 60000 : 0) };
      return snapshot.access === access ? snapshot.usage : usage;
    })();
    const current = { access, promise }; pending = current;
    try { return await promise; }
    finally { if (pending === current) pending = undefined; }
  };
}

export function quotaHTML(history, usage) {
  let observed = Date.parse(usage.as_of);
  const charts = (usage.windows ?? []).map(window => {
    const points = history.windows.find(item => item.name === window.name)?.points ?? [];
    const current = points.at(-1);
    const windowReset = Date.parse(window.resetsAt);
    if (!current || !number(window.amount) || !number(window.limit) || !window.limit
        || !Number.isFinite(windowReset) || current.reset !== new Date(windowReset).toISOString()) return "";
    observed = Math.max(observed, Date.parse(current.at));
    const reset = Date.parse(current.reset), start = current.start;
    const x = point => 32 + 736 * Math.min(1, Math.max(0, (Date.parse(point.at) - start) / (reset - start)));
    const left = point => Math.max(0, 100 * (1 - point.amount / point.limit));
    const y = point => 24 + 192 * (1 - left(point) / 100);
    const coords = points.filter(point => point.reset === current.reset)
      .map(point => `${x(point).toFixed(2)},${y(point).toFixed(2)}`).join(" ");
    return `<section><h2>${escape(window.name)}</h2><p class="metric">${formatAmount(current.amount, window)} / ${formatAmount(current.limit, window)} ${unitLabel(window)}</p>
<p>剩余 ${formatAmount(Math.max(0, current.limit - current.amount), window)} ${unitLabel(window)} · ${current.aside ? "平台仅记录，不阻断" : "平台预算阻断已启用"}</p>
<div class="plot"><div class="axis" aria-hidden="true"><span>100%</span><span>50%</span><span>0%</span></div><svg viewBox="0 0 800 248" role="img" aria-label="个人剩余额度百分比的真实采样趋势"><path class="grid" d="M32 24H768 M32 120H768 M32 216H768"/>
<polyline class="line" points="${coords}"/><circle cx="${x(current).toFixed(2)}" cy="${y(current).toFixed(2)}" r="4"/></svg></div>
<div class="dates"><time datetime="${escape(new Date(start).toISOString())}"></time><time datetime="${escape(current.reset)}"></time></div>
<p class="note">${points.filter(point => point.reset === current.reset).length} 个真实快照。首次采样之前的数据未知，不补零、不借用应用共享额度。</p></section>`;
  }).join("");
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>AIGateway · 个人额度趋势</title>
<style>:root{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#19191c;color:#e8e8ec}body{max-width:960px;margin:0 auto;padding:36px 24px;overflow-wrap:anywhere}h1{font-size:25px;margin-bottom:8px}h2{font-size:17px}.meta,.note{color:#b0b0ba;font-size:13px;line-height:1.7}section{margin-top:24px;padding:24px;border:1px solid #39393f;border-radius:12px;background:#232328}.metric{font-size:22px;font-weight:600}svg{display:block;width:100%;height:auto;overflow:visible}.plot{position:relative;padding-left:34px}.axis span{position:absolute;left:0;font-size:11px;color:#b0b0ba;transform:translateY(-50%)}.axis span:nth-child(1){top:9.677%}.axis span:nth-child(2){top:48.387%}.axis span:nth-child(3){top:87.097%}.grid{stroke:#3b3b42;fill:none}.line{fill:none;stroke:#aaa7f5;stroke-width:2}circle{fill:#aaa7f5}.dates{display:flex;justify-content:space-between;gap:12px;font-size:12px;color:#b0b0ba}@media(max-width:480px){body{padding:20px 12px}section{padding:16px}.metric{font-size:18px}.dates{font-size:10px}h1{font-size:20px}}</style></head><body>
<h1>${escape(usage.user || "用户")} · 个人额度趋势</h1><p class="meta">插件独立视图，不是 magpie 原生卡片。只读取平台个人额度，查看此页不改变预算控制。</p>
<p class="meta">数据时间：<time datetime="${escape(Number.isFinite(observed) ? new Date(observed).toISOString() : "")}"></time> · 浏览器时区：<span id="timezone"></span></p>
${charts || "<section><h2>未配置个人额度或暂无有效快照</h2></section>"}
<p class="note">这是本地快照，不实时推送。请在插件中再次点击“查看额度趋势”获取最新读数；平台读数可能有 60 秒缓存。</p>
<script>document.getElementById('timezone').textContent=Intl.DateTimeFormat().resolvedOptions().timeZone;for(const element of document.querySelectorAll('time')){const date=new Date(element.dateTime);element.textContent=Number.isNaN(date.valueOf())?'未知':date.toLocaleString(undefined,{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})}</script></body></html>`;
}

async function record(directory, cfg, usage) {
  const at = Date.parse(usage.as_of);
  if (!Number.isFinite(at)) return null;
  const path = join(directory, "quota-history.json");
  let history;
  try { history = JSON.parse(await readFile(path, "utf8")); }
  catch (error) { if (error.code !== "ENOENT") throw error; history = { userId: cfg.userId, windows: [] }; }
  if (history.userId !== cfg.userId || !Array.isArray(history.windows))
    throw new Error("Quota history does not belong to this user");
  if (history.windows.some(line => typeof line.name !== "string" || !Array.isArray(line.points)
      || line.points.some(point => !Number.isFinite(Date.parse(point.at))
        || !Number.isFinite(Date.parse(point.reset)) || !number(point.start)
        || !number(point.amount) || !number(point.limit) || !point.limit)))
    throw new Error("Quota history is invalid; preserve it for recovery");
  for (const window of usage.windows ?? []) {
    const reset = Date.parse(window.resetsAt);
    if (!personalUnits.has(window.name) || windowUnit(window) !== personalUnits.get(window.name)
        || !number(window.amount) || !number(window.limit) || !window.limit
        || !Number.isFinite(reset) || !number(window.span) || !window.span
        || at >= reset || at < reset - window.span * 1000) continue;
    let line = history.windows.find(item => item.name === window.name);
    if (!line) { line = { name: window.name, unit: windowUnit(window), points: [] }; history.windows.push(line); }
    if (line.unit != null && line.unit !== windowUnit(window))
      throw new Error("Quota history unit changed; preserve it for recovery");
    const point = { at: new Date(at).toISOString(), start: reset - window.span * 1000,
      reset: new Date(reset).toISOString(), amount: window.amount, limit: window.limit, aside: window.aside === true };
    const last = line.points.at(-1);
    if (last && Date.parse(last.at) > at) continue;
    if (last?.at === point.at) line.points[line.points.length - 1] = point;
    else if (line.points.length > 1 && last.reset === point.reset && Date.parse(last.at) > at - 300_000)
      line.points[line.points.length - 1] = point;
    else line.points.push(point);
    line.points = line.points.filter(item => Date.parse(item.at) >= at - 90 * 86400_000).slice(-10000);
  }
  await privateJSON(path, history);
  const target = join(directory, "quota-trend.html"), temporary = target + "." + randomUUID();
  try {
    await writeFile(temporary, quotaHTML(history, usage), { mode: 0o600, flag: "wx" });
    await rename(temporary, target);
  } finally { await unlink(temporary).catch(() => {}); }
  return target;
}

export async function recordQuota(directory, cfg, usage) {
  const path = join(directory, "quota-history.json.lock");
  let lock;
  try {
    const deadline = Date.now() + 2000;
    while (!lock) {
      try { lock = await open(path, "wx", 0o600); }
      catch (error) {
        if (error.code !== "EEXIST" || Date.now() >= deadline) throw error;
        await new Promise(resolve => setTimeout(resolve, 20));
      }
    }
    return await record(directory, cfg, usage);
  } finally { if (lock) { await lock.close(); await unlink(path).catch(() => {}); } }
}
