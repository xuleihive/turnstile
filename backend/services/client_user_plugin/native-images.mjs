import { execFile } from "node:child_process";
import { access, mkdir, open, readFile, realpath, stat, unlink } from "node:fs/promises";
import { basename, dirname, isAbsolute, join } from "node:path";
import { promisify } from "node:util";
import { privateJSON } from "./helpers.mjs";

const execute = promisify(execFile);
const namespace = /^aigateway-user-[a-z0-9-]+$/;
const cliName = /^magpie(?:-(?:windows|darwin|linux)-(?:arm64|aarch64|amd64|x64))?(?:\.exe)?$/i;
const stable = value => Array.isArray(value) ? value.map(stable) : value && typeof value === "object"
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])])) : value;
const equal = (a, b) => JSON.stringify(stable(a)) === JSON.stringify(stable(b));
const withoutOff = value => Object.fromEntries(Object.entries(value ?? {}).filter(([key]) => key !== "off"));

async function json(path, fallback) {
  try { return JSON.parse(await readFile(path, "utf8")); }
  catch (error) { if (error.code === "ENOENT") return fallback; throw error; }
}

export async function imageFileLock(path) {
  let file;
  for (let attempt = 0; attempt < 2; attempt++) {
    try { file = await open(path, "wx", 0o600); break; }
    catch (error) {
      if (error.code !== "EEXIST" || attempt) throw error;
      const existing = await json(path, null).catch(() => null);
      let stale = false;
      if (existing?.type === "aigateway-image-lock" && Number.isInteger(existing.pid) && existing.pid > 0) {
        try { process.kill(existing.pid, 0); }
        catch (error) { stale = error.code === "ESRCH"; }
      } else if ((await readFile(path, "utf8")) === "") {
        stale = Date.now() - (await stat(path)).mtimeMs > 60_000;
      }
      if (!stale) throw error;
      await unlink(path);
    }
  }
  try { await file.writeFile(JSON.stringify({ type: "aigateway-image-lock", pid: process.pid })); }
  catch (error) { await file.close(); await unlink(path).catch(() => {}); throw error; }
  return async () => { await file.close(); await unlink(path).catch(() => {}); };
}

async function checkedExecutable(candidate) {
  if (typeof candidate !== "string" || !isAbsolute(candidate) || !cliName.test(basename(candidate))) return null;
  try {
    const resolved = await realpath(candidate);
    const app = resolved.indexOf(".app/");
    const folder = app >= 0 ? dirname(resolved.slice(0, app + 4)) : dirname(resolved);
    for (const marker of [".portable", "data"]) {
      try { await access(join(folder, marker)); return null; }
      catch (error) { if (error.code !== "ENOENT") return null; }
    }
    return resolved;
  } catch { return null; }
}

export async function resolveNativeMagpie(explicit = process.env.AIGATEWAY_MAGPIE_EXECUTABLE) {
  if (explicit) return checkedExecutable(explicit);
  let parent;
  try {
    if (process.platform === "win32") {
      const powershell = join(process.env.SystemRoot ?? "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
      parent = (await execute(powershell, ["-NoProfile", "-NonInteractive", "-Command",
        `(Get-CimInstance Win32_Process -Filter 'ProcessId = ${process.ppid}').ExecutablePath`],
      { timeout: 3000, maxBuffer: 4096, windowsHide: true })).stdout.trim();
    } else if (process.platform === "linux") parent = await realpath(`/proc/${process.ppid}/exe`);
    else if (process.platform === "darwin") parent = (await execute("/bin/ps", ["-p", String(process.ppid), "-o", "comm="],
      { timeout: 3000, maxBuffer: 4096 })).stdout.trim();
  } catch { /* PATH and the standard application remain available. */ }
  const candidates = [parent, globalThis.Bun?.which?.("magpie"), globalThis.Bun?.which?.("magpie.exe"),
    process.platform === "darwin" ? "/Applications/magpie.app/Contents/MacOS/magpie" : null];
  for (const candidate of candidates) {
    const cli = await checkedExecutable(candidate);
    if (cli) return cli;
  }
  return null;
}

function context(input, cfg) {
  if (input?.project?.id !== "magpie" || !isAbsolute(input.directory ?? "") || basename(input.directory) !== "magpie") return null;
  const subscription = cfg.kind === "subscription";
  const subject = subscription ? cfg.subscriptionId : cfg.userId;
  if (!(subscription ? /^aigateway-subscription-[a-z0-9-]+$/ : namespace).test(cfg.providerId)
      || typeof subject !== "string" || !subject
      || typeof cfg.installationId !== "string" || !cfg.installationId) throw new Error("Invalid image registration owner");
  const directory = join(input.directory, subscription ? "aigateway-proxy-subscription" : "aigateway-proxy-user", cfg.providerId);
  return { directory, providers: join(input.directory, "providers.json"),
    manifest: join(directory, "native-images-managed.json"), status: join(directory, "native-images-status.json"),
    owner: { schema: 1, providerId: cfg.providerId + "-images", installationId: cfg.installationId,
      ...(subscription ? { kind: "subscription", subscriptionId: subject } : { userId: subject }) } };
}

async function snapshot(path, id) {
  const document = await json(path, { providers: [] });
  if (!document || typeof document !== "object" || Array.isArray(document) || !Array.isArray(document.providers))
    throw new Error("Invalid native provider document");
  const found = document.providers.filter(p => p?.id === id);
  if (found.length > 1) throw new Error("Duplicate native image provider ID");
  return found[0] ?? null;
}

function belongs(record, owner) {
  return record && Object.entries(owner).every(([key, value]) => record[key] === value) && record.last?.id === owner.providerId;
}

function desiredPicture(picture, id) {
  if (!picture || picture.id !== id || typeof picture.name !== "string" || !picture.name
      || typeof picture.key !== "string" || !picture.key || /[\r\n]/.test(picture.key)
      || !Array.isArray(picture.models) || !picture.models.length
      || picture.models.some(model => typeof model !== "string" || !model || /[,\s*]/.test(model))
      || new Set(picture.models).size !== picture.models.length) throw new Error("Invalid native image descriptor");
  const url = new URL(picture.chat);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || url.username || url.password || url.search || url.hash || url.pathname !== "/v1")
    throw new Error("Native images require a loopback companion");
  return { id, name: picture.name, key: picture.key, chat: picture.chat, models: [...picture.models] };
}

function matches(p, desired) {
  return p && Object.entries(desired).every(([key, value]) => equal(p[key], value));
}

async function report(ctx, status, options) {
  const previous = await json(ctx.status, null).catch(() => null);
  await privateJSON(ctx.status, status);
  if (!equal(previous, status) && ["pending", "conflict", "preserved_manual"].includes(status.state))
    (options.warn ?? console.warn)(`AIGateway native images: ${status.state} (${status.reason}); HTTP MCP remains available.`);
  return status;
}

async function command(cli, args, input, options) {
  // Only the local nonce goes to the CLI. Never surface raw errors containing its arguments.
  await (options.execute ?? execute)(cli, args, { env: { ...process.env, ...options.env,
    XDG_CONFIG_HOME: dirname(input.directory), AIGATEWAY_NATIVE_IMAGE_WORKER: "1", AIGATEWAY_PRICING_WORKER: "1",
    MAGPIE_NO_ACCOUNTS: "1", MAGPIE_NO_STATS: "1", DO_NOT_TRACK: "1" },
  timeout: options.timeoutMs ?? 8000, maxBuffer: 128 * 1024, windowsHide: true });
}

async function operate(input, cfg, picture, remove, options) {
  let ctx, release, cli;
  const result = { state: "pending", reason: "registration-unavailable", providerId: cfg.providerId + "-images" };
  try {
    ctx = context(input, cfg);
    if (!ctx) return { ...result, state: "unsupported", reason: "non-native-installation" };
    await mkdir(ctx.directory, { recursive: true, mode: 0o700 });
    try { release = await imageFileLock(join(ctx.directory, "native-images.lock")); }
    catch (error) { if (error.code === "EEXIST") return { ...result, state: "busy", reason: "registration-in-progress" }; throw error; }
    const record = await json(ctx.manifest, null);
    const current = await snapshot(ctx.providers, ctx.owner.providerId);
    if (current && !belongs(record, ctx.owner))
      return await report(ctx, { ...result, state: "conflict", reason: "unowned-provider-id" }, options);
    if (record && !belongs(record, ctx.owner))
      return await report(ctx, { ...result, state: "conflict", reason: "foreign-registration-owner" }, options);
    if (current && (!equal(withoutOff(current), withoutOff(record.last)) || (remove && current.off)))
      return await report(ctx, { ...result, state: "preserved_manual", reason: "provider-manually-modified" }, options);
    if (remove && !current) {
      if (record) await unlink(ctx.manifest);
      return await report(ctx, { ...result, state: "absent", reason: "not-registered" }, options);
    }
    const desired = remove ? null : desiredPicture(picture, ctx.owner.providerId);
    if (!remove && matches(current, desired))
      return { ...await report(ctx, { ...result, state: "applied", reason: "unchanged", modelCount: desired.models.length,
        disabled: Boolean(current.off) }, options), cli: record.cli };
    cli = await (options.resolveExecutable ?? resolveNativeMagpie)();
    if (!cli) return await report(ctx, { ...result, reason: "cli-unavailable" }, options);
    if (remove) {
      await command(cli, ["provider", "rm", ctx.owner.providerId], input, options).catch(() => {});
      if (await snapshot(ctx.providers, ctx.owner.providerId)) throw new Error("Native removal readback differs");
      await unlink(ctx.manifest);
      return await report(ctx, { ...result, state: "removed", reason: "owned-provider-removed" }, options);
    }
    const fields = [`name=${desired.name}`, `chat=${desired.chat}`, `key=${desired.key}`, `models=${desired.models.join(",")}`];
    const args = current ? ["provider", "set", desired.id, ...fields]
      : ["provider", "add", desired.name, `id=${desired.id}`, ...fields.slice(1)];
    // A save can succeed before subsequent model discovery times out. Readback is authoritative.
    await command(cli, args, input, options).catch(() => {});
    const actual = await snapshot(ctx.providers, desired.id);
    if (!matches(actual, desired) || (current && Boolean(current.off) !== Boolean(actual.off)))
      throw new Error("Native registration readback differs");
    await privateJSON(ctx.manifest, { ...ctx.owner, last: actual, cli });
    return { ...await report(ctx, { ...result, state: "applied", reason: current ? "updated" : "created",
      modelCount: desired.models.length, disabled: Boolean(actual.off) }, options), cli };
  } catch {
    const status = { ...result, reason: cli ? "cli-or-readback-failed" : "registration-unavailable" };
    if (ctx && release) await report(ctx, status, options).catch(() => {});
    return status;
  } finally { if (release) await release(); }
}

export async function syncNativeImages(input, cfg, picture, options = {}) {
  if (process.env.AIGATEWAY_NATIVE_IMAGE_WORKER === "1" || process.env.AIGATEWAY_PRICING_WORKER === "1")
    return { state: "worker", reason: "maintenance-worker" };
  return operate(input, cfg, picture, false, options);
}

export async function removeNativeImages(input, cfg, options = {}) {
  return operate(input, cfg, null, true, options);
}
