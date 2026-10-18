import { execFile } from "node:child_process";
import { access, mkdir, open, readFile, realpath, rename, unlink, writeFile } from "node:fs/promises";
import { basename, dirname, join } from "node:path";
import { randomUUID } from "node:crypto";
import { promisify } from "node:util";

const execute = promisify(execFile);
const canonical = value => JSON.stringify(value, Object.keys(value ?? {}).sort());
const validNumber = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
const preference = value => value && [value.input, value.output, value.cache_read, value.cache_write].every(validNumber)
  && (value.cache_write_1h == null || validNumber(value.cache_write_1h))
  && (value.tiers === undefined || Array.isArray(value.tiers) && value.tiers.every((tier, index) =>
    tier && validNumber(tier.above) && tier.above > 0 && (!index || tier.above > value.tiers[index - 1].above)
    && [tier.input ?? 0, tier.output ?? 0, tier.cache_read ?? 0, tier.cache_write ?? 0,
      tier.cache_write_1h ?? 0].every(validNumber)));

export function tokenCost(value) {
  if (!value || !validNumber(value.input) || !validNumber(value.output)) return null;
  const read = value.cache_read ?? value.cache?.read;
  const write = value.cache_write ?? value.cache?.write;
  if (!validNumber(read) || !validNumber(write)) return null;
  return { input: value.input, output: value.output, cache: { read, write } };
}

async function json(path, fallback) {
  try { return JSON.parse(await readFile(path, "utf8")); }
  catch (error) { if (error.code === "ENOENT") return fallback; throw error; }
}

async function save(path, data) {
  const temporary = path + "." + randomUUID();
  try {
    await writeFile(temporary, JSON.stringify(data), { mode: 0o600, flag: "wx" });
    await rename(temporary, path);
  } finally { await unlink(temporary).catch(() => {}); }
}

async function executable() {
  const explicit = process.env.AIGATEWAY_MAGPIE_EXECUTABLE;
  const candidates = [explicit, globalThis.Bun?.which?.("magpie"),
    process.platform === "darwin" ? "/Applications/magpie.app/Contents/MacOS/magpie" : undefined];
  for (const candidate of candidates.filter(Boolean)) {
    if (!["magpie", "magpie.exe"].includes(basename(candidate).toLowerCase())) continue;
    try {
      const resolved = await realpath(candidate), bundle = resolved.indexOf(".app/");
      const directory = bundle >= 0 ? dirname(resolved.slice(0, bundle + 4)) : dirname(resolved);
      let portable = false;
      for (const name of [".portable", "data"]) {
        try { await access(join(directory, name)); portable = true; }
        catch (error) { if (error.code !== "ENOENT") portable = true; }
      }
      if (!portable) return resolved;
    } catch { /* Try the next native installation. */ }
  }
  return null;
}

// Use magpie's supported price command, not direct writes to its settings file.
// The worker flag prevents recursive price imports when that command loads this plugin.
export async function syncPricing(input, cfg, models, options = {}) {
  if (process.env.AIGATEWAY_PRICING_WORKER === "1" || input?.project?.id !== "magpie")
    return { state: "external", resolved: [] };
  if (!input.directory || basename(input.directory) !== "magpie")
    return { state: "unsupported", resolved: [] };
  if (!/^aigateway-[\p{L}\p{N}-]+$/u.test(cfg.providerId))
    throw new Error("Invalid pricing namespace");
  const directory = join(input.directory, "aigateway-pricing", cfg.providerId);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const manifest = join(directory, "managed.json"), settings = join(input.directory, "settings.json");
  const desired = Object.fromEntries(Object.entries(models).filter(([id]) => id && id !== "*").map(([id, model]) => {
    const cost = tokenCost(model.cost);
    return [cfg.providerId + "/" + id, cost ? { input: cost.input, output: cost.output,
      cache_read: cost.cache.read, cache_write: cost.cache.write, cache_write_1h: cost.cache.write } : null];
  }));
  const status = { state: "applied", resolved: [], preserved: [], pending: [] };
  let lock;
  try {
    lock = await open(manifest + ".lock", "wx", 0o600);
    const managed = await json(manifest, { prices: {} });
    const cli = options.executable ?? await (options.resolveExecutable ?? executable)();
    const deadline = Date.now() + (options.budgetMs ?? 8000);
    // Always export a private, complete snapshot for hosts without the native CLI.
    await save(join(directory, "prices.json"), { unit: "USD per million tokens", prices: desired });
    for (const [ref, price] of Object.entries(desired)) {
      const snapshot = await json(settings, {});
      if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot))
        throw new Error("Invalid native settings; do not overwrite them");
      const configured = snapshot.modelPrices ?? {};
      if (typeof configured !== "object" || Array.isArray(configured))
        throw new Error("Invalid native prices; do not overwrite them");
      const current = configured[ref];
      const previous = managed.prices?.[ref];
      if (current && (!previous || canonical(current) !== canonical(previous))) {
        status.preserved.push(ref);
        if (preference(current))
          status.resolved.push(ref);
        continue;
      }
      const model = ref.slice(cfg.providerId.length + 1);
      const manualWide = [configured[cfg.providerId + "/*"], configured["*/" + model.toLowerCase()]].find(preference);
      if (manualWide && !current) {
        status.preserved.push(ref);
        status.resolved.push(ref);
        continue;
      }
      // An explicit wide preference must not be hidden by a plugin-owned override.
      const effective = manualWide ? null : price;
      const target = effective && Object.fromEntries(Object.entries(effective).filter(([key, value]) => key !== "cache_write_1h" || value > 0));
      if (canonical(current) === canonical(target)) {
        if (current) status.resolved.push(ref);
        continue;
      }
      if (!effective && !current) continue;
      if (!cli || Date.now() >= deadline) { status.pending.push(ref); continue; }
      const values = effective ? [effective.input, effective.output, effective.cache_read, effective.cache_write, effective.cache_write_1h].join(",") : "--reset";
      try {
        await (options.execute ?? execute)(cli, ["model", "price", ref, values], {
          env: { ...process.env, XDG_CONFIG_HOME: dirname(input.directory),
            AIGATEWAY_PRICING_WORKER: "1", MAGPIE_NO_STATS: "1", DO_NOT_TRACK: "1" },
          timeout: Math.max(500, deadline - Date.now()), maxBuffer: 256 * 1024, windowsHide: true,
        });
        const actual = (await json(settings, {})).modelPrices?.[ref];
        if (target ? canonical(actual) !== canonical(target) : actual !== undefined)
          throw new Error("Price readback differs");
        managed.prices ??= {};
        if (actual) { managed.prices[ref] = actual; status.resolved.push(ref); }
        else delete managed.prices[ref];
        if (manualWide) { status.preserved.push(ref); status.resolved.push(ref); }
        // Commit each successful import so a later failure cannot claim a manual price.
        await save(manifest, managed);
      } catch { status.pending.push(ref); }
    }
    status.state = status.pending.length ? "pending" : status.preserved.length ? "preserved_manual" : "applied";
    await save(join(directory, "status.json"), status);
  } catch {
    status.state = "pending";
  } finally {
    if (lock) { await lock.close(); await unlink(manifest + ".lock").catch(() => {}); }
  }
  return status;
}

export function costForHost(input, providerId, id, raw, status) {
  const cost = tokenCost(raw);
  if (!cost) return null;
  // The native plugin adapter drops cache prices. Do not silently bill caches as free
  // when its complete per-model price has not been imported successfully.
  if (input?.project?.id === "magpie" && process.env.AIGATEWAY_PRICING_WORKER !== "1"
      && !status.resolved.includes(providerId + "/" + id) && (cost.cache.read > 0 || cost.cache.write > 0))
    return null;
  return cost;
}
