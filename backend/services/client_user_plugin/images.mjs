import { readFile, realpath, unlink, writeFile } from "node:fs/promises";
import { timingSafeEqual } from "node:crypto";
import { isAbsolute, join } from "node:path";
import { localQuotaControl, quotaReader, quotaRefusal } from "./quota.mjs";
import { removeNativeImages, resolveNativeMagpie } from "./native-images.mjs";

export function localMagpieURL(address = "127.0.0.1:3425") {
  const url = new URL(address.includes("://") ? address : "http://" + address);
  if (url.protocol !== "http:" || !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
      || url.username || url.password || url.search || url.hash || url.pathname !== "/")
    throw new Error("Picture registration requires a loopback magpie gateway");
  return url.origin;
}

export async function ownLogin(settingsDirectory, providerId, userId) {
  const auth = JSON.parse(await readFile(join(settingsDirectory, "plugin-auth.json"), "utf8"));
  return Object.entries(auth).some(([id, value]) => (id === providerId || id.startsWith(providerId + "#"))
    && value?.type === "oauth" && value.uid === userId && value.access);
}

async function subscriptionAccount(state) {
  if (typeof state.accountSlot !== "string" || !(state.accountSlot === state.providerId
      || state.accountSlot.startsWith(state.providerId + "#"))) return null;
  const accounts = JSON.parse(await readFile(join(state.settingsDirectory, "plugin-auth.json"), "utf8"));
  const auth = accounts[state.accountSlot];
  return auth?.type === "api" && typeof auth.key === "string" && auth.key ? auth : null;
}

export async function imagePluginEnabled(state) {
  if (state.kind === "subscription" ? !await subscriptionAccount(state)
    : !await ownLogin(state.settingsDirectory, state.providerId, state.userId)) return false;
  const plugins = JSON.parse(await readFile(join(state.settingsDirectory, "plugins.json"), "utf8"));
  const canonical = await realpath(state.spec);
  for (const entry of plugins.plugins ?? []) {
    if (typeof entry?.spec !== "string" || !isAbsolute(entry.spec)) continue;
    if (await realpath(entry.spec).catch(() => "") === canonical) return !entry.off;
  }
  return false;
}

export function imageQuotaGuard() {
  // Cache is per bridge instance and origin; model calls still use the image-only capability.
  let origin, cached;
  return async current => {
    const directory = join(current.settingsDirectory, "aigateway-proxy-user", current.providerId);
    if (!await localQuotaControl(directory, current)) return null;
    const accounts = JSON.parse(await readFile(join(current.settingsDirectory, "plugin-auth.json"), "utf8"));
    const own = Object.entries(accounts).find(([id, auth]) => (id === current.providerId || id.startsWith(current.providerId + "#"))
      && auth?.type === "oauth" && auth.uid === current.userId)?.[1];
    if (!own?.access) throw new Error("请重新登录 AIGateway 平台");
    if (origin !== current.platformOrigin) {
      origin = current.platformOrigin;
      const target = origin + "/api/v1/client-runtime/usage";
      cached = quotaReader(async access => {
        const response = await fetch(target, {
          method: "GET", headers: { Authorization: "Bearer " + access }, redirect: "manual", signal: AbortSignal.timeout(20000),
        });
        if (!response.ok) throw new Error("额度读取失败，请刷新模型或重新登录");
        return response.json();
      });
    }
    return quotaRefusal(await cached(own.access), true);
  };
}

async function descriptor(path) {
  const data = JSON.parse(await readFile(path, "utf8"));
  if (data.kind === "subscription") {
    if (!/^aigateway-subscription-[a-z0-9-]+$/.test(data.providerId) || !data.subscriptionId
        || !Array.isArray(data.images) || !data.nonce || !data.settingsDirectory)
      throw new Error("Invalid subscription picture bridge state");
    const bootstrap = JSON.parse(await readFile(join(data.spec, "aigateway.config.json"), "utf8"));
    const origin = new URL(data.baseURL);
    if (origin.protocol !== "https:" || origin.username || origin.password || origin.search || origin.hash
        || bootstrap.providerId !== data.providerId || bootstrap.subscriptionId !== data.subscriptionId
        || bootstrap.baseURL !== data.baseURL || !Array.isArray(bootstrap.images)
        || data.images.some(m => !bootstrap.images.some(b => b.id === m.id)))
      throw new Error("Picture gateway and models must match the installed subscription plugin");
    return data;
  }
  if (data.kind !== undefined) throw new Error("Invalid picture authorization mode");
  if (!/^aigateway-user-[\p{L}\p{N}-]+$/u.test(data.providerId) || !Array.isArray(data.images)
      || !data.capability?.startsWith("tsci_") || !data.nonce || !data.settingsDirectory)
    throw new Error("Invalid private picture bridge state");
  const origin = new URL(data.platformOrigin);
  if (origin.protocol !== "https:" && !(origin.protocol === "http:" && ["localhost", "127.0.0.1"].includes(origin.hostname)))
    throw new Error("Invalid picture platform origin");
  const bootstrap = JSON.parse(await readFile(join(data.spec, "aigateway-user.config.json"), "utf8"));
  if (bootstrap.platformOrigin !== data.platformOrigin || bootstrap.providerId !== data.providerId || bootstrap.userId !== data.userId)
    throw new Error("Picture quota origin must match the installed user plugin");
  return data;
}

export async function pictureBridge(statePath, readyPath) {
  const quotaGuard = imageQuotaGuard();
  const initial = await descriptor(statePath);
  const subscription = initial.kind === "subscription";
  const generate = async (state, body, signal) => {
    if ((state.kind === "subscription") !== subscription) throw new Error("Picture authorization mode changed");
    const auth = subscription ? await subscriptionAccount(state) : null;
    if (subscription && !auth) throw new Error("Subscription signed out");
    return fetch(subscription ? state.baseURL.replace(/\/$/, "") + "/images/generations"
      : state.platformOrigin + "/api/v1/client-runtime/images/generations", {
      method: "POST", redirect: "manual", signal,
      headers: { "Content-Type": "application/json", ...(subscription
        ? { "Ocp-Apim-Subscription-Key": auth.key } : { Authorization: "Bearer " + state.capability }) },
      body: JSON.stringify(body),
    });
  };
  const checkQuota = state => subscription ? null : quotaGuard(state);
  localMagpieURL(initial.magpieAddress);
  const nonce = initial.nonce;
  const matchNonce = (value) => {
    const a = Buffer.from(value ?? ""); const b = Buffer.from("Bearer " + nonce);
    return a.length === b.length && timingSafeEqual(a, b);
  };
  let stopping = false;
  const server = Bun.serve({ hostname: "127.0.0.1", port: 0, maxRequestBodySize: 4 * 1024 * 1024,
    async fetch(request) {
      if (!matchNonce(request.headers.get("authorization")) || request.headers.get("origin"))
        return Response.json({ error: "Unauthorized" }, { status: 401 });
      const path = new URL(request.url).pathname;
      if (path === "/health") return Response.json({ providerId: initial.providerId, bridgeVersion: 4, pid: process.pid });
      let state;
      try { state = await descriptor(statePath); if (!await imagePluginEnabled(state)) throw new Error(); }
      catch { return Response.json({ error: "The user plugin is disabled or signed out" }, { status: 403 }); }
      if (request.method === "POST" && path === "/mcp") {
        const rpc = await request.json().catch(() => null);
        if (!rpc || rpc.jsonrpc !== "2.0" || typeof rpc.method !== "string") return Response.json({ error: "Invalid JSON-RPC" }, { status: 400 });
        if (rpc.method.startsWith("notifications/")) return new Response(null, { status: 202 });
        const reply = (result) => Response.json({ jsonrpc: "2.0", id: rpc.id, result });
        if (rpc.method === "initialize") return reply({ protocolVersion: "2025-03-26", capabilities: { tools: { listChanged: false } }, serverInfo: { name: subscription ? "aigateway-subscription-images" : "aigateway-user-images", version: "0.2.0" } });
        if (rpc.method === "tools/list") return reply({ tools: [{ name: "generate_image", description: subscription ? "Generate an image through the configured AIGateway subscription." : "Generate an image through the current AIGateway user subscription.", inputSchema: { type: "object", properties: { model: { type: "string", enum: state.images.map((m) => m.id) }, prompt: { type: "string" }, size: { type: "string" }, quality: { type: "string" } }, required: ["model", "prompt"], additionalProperties: false } }] });
        if (rpc.method !== "tools/call" || rpc.params?.name !== "generate_image") return Response.json({ jsonrpc: "2.0", id: rpc.id, error: { code: -32601, message: "Method not found" } });
        const args = rpc.params.arguments;
        if (!state.images.some((m) => m.id === args?.model) || typeof args?.prompt !== "string") return reply({ isError: true, content: [{ type: "text", text: "Image model and prompt are required" }] });
        try {
          if (await checkQuota(state)) return reply({ isError: true, content: [{ type: "text", text: "个人月度额度已用尽，插件已在本地阻断图片调用。" }] });
        } catch { return reply({ isError: true, content: [{ type: "text", text: "额度读取失败，请刷新模型或重新登录。" }] }); }
        let response;
        try { response = await generate(state, args, request.signal); }
        catch { return reply({ isError: true, content: [{ type: "text", text: "The image gateway is unavailable" }] }); }
        const data = await response.json().catch(() => null);
        if (!response.ok || !data?.data?.length) return reply({ isError: true, content: [{ type: "text", text: "Image generation failed; check platform access and quota" }] });
        return reply({ content: data.data.map((image) => image.b64_json ? { type: "image", data: image.b64_json, mimeType: "image/png" } : { type: "text", text: image.url ?? "Image data was unavailable" }), isError: false });
      }
      if (request.method === "GET" && path === "/v1/models") return Response.json({ object: "list", data: state.images.map((m) => ({ id: m.id, object: "model", owned_by: "aigateway" })) });
      if (request.method !== "POST" || path !== "/v1/images/generations") return Response.json({ error: "Images only" }, { status: 404 });
      let body;
      try { body = await request.json(); } catch { return Response.json({ error: "JSON required" }, { status: 400 }); }
      if (!state.images.some((m) => m.id === body?.model)) return Response.json({ error: "Image model not authorized" }, { status: 403 });
      try {
        const refusal = await checkQuota(state);
        if (refusal) return refusal;
        const response = await generate(state, body, request.signal);
        return new Response(response.body, { status: response.status, headers: { "content-type": response.headers.get("content-type") ?? "application/json", "cache-control": "no-store" } });
      } catch { return Response.json({ error: "The image gateway is unavailable" }, { status: 502 }); }
    },
  });
  await writeFile(readyPath, JSON.stringify({ port: server.port, pid: process.pid, nonce }), { mode: 0o600 });
  // The helper outlives an idle plugin host, but never an enabled, signed-in installation.
  const stop = async () => {
    if (stopping) return; stopping = true;
    let state;
    try { state = await descriptor(statePath); } catch { state = initial; }
    try { if (!subscription) {
      await fetch(state.platformOrigin + "/api/v1/client-auth/revoke", { method: "POST", headers: { Authorization: "Bearer " + state.capability }, signal: AbortSignal.timeout(2000) });
    } } catch { /* Never log private bridge state or credentials. */ }
    await removeNativeImages({ directory: state.settingsDirectory, project: { id: "magpie" } }, state,
      { resolveExecutable: () => resolveNativeMagpie(state.nativeCli) }).catch(() => {});
    await unlink(readyPath).catch(() => {});
    server.stop(true); process.exit(0);
  };
  const timer = setInterval(async () => {
    try { const state = await descriptor(statePath); if (!state.images.length || !await imagePluginEnabled(state)) await stop(); }
    catch { await stop(); }
  }, 2000);
  timer.unref?.();
  process.on("SIGTERM", () => void stop());
  return server;
}

if (import.meta.main) await pictureBridge(process.argv[2], process.argv[3]);
