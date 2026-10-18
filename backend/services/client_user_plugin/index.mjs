import { mkdir, open, readFile, unlink } from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { imagePluginEnabled, localMagpieURL } from "./images.mjs";
import { imageFileLock, removeNativeImages, syncNativeImages } from "./native-images.mjs";
import { adaptModelResponse, importBootstrap, privateJSON, syncAccountProfile } from "./helpers.mjs";
import { costForHost, syncPricing } from "./pricing.mjs";
import { localQuotaControl, personalQuota, quotaForMagpie, quotaReader, quotaRefusal, recordQuota, saveQuotaControl } from "./quota.mjs";

export default async function AIGatewayUserPlugin(input) {
  const nativeMagpie = input.project?.id === "magpie";
  const pricingWorker = process.env.AIGATEWAY_PRICING_WORKER === "1" || process.env.AIGATEWAY_NATIVE_IMAGE_WORKER === "1";
  const cfg = JSON.parse(await readFile(new URL("./aigateway-user.config.json", import.meta.url), "utf8"));
  const origin = new URL(cfg.platformOrigin);
  const local = origin.protocol === "http:" && ["localhost", "127.0.0.1"].includes(origin.hostname);
  if (![2, 3, 4].includes(cfg.version) || (origin.protocol !== "https:" && !local) || origin.username || origin.password
      || origin.search || origin.hash || origin.pathname !== "/" || !/^aigateway-user-[\p{L}\p{N}-]+$/u.test(cfg.providerId))
    throw new Error("Invalid user plugin bootstrap");
  const base = origin.origin;
  const directory = join(input.directory ?? fileURLToPath(new URL(".", import.meta.url)), "aigateway-proxy-user", cfg.providerId);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const bootstrapped = !pricingWorker && await importBootstrap(input, cfg);
  const installPath = join(directory, "installation.json");
  if (bootstrapped) await privateJSON(installPath, { id: cfg.installationId });
  let installation;
  try { installation = JSON.parse(await readFile(installPath, "utf8")).id; }
  catch (error) {
    if (error.code !== "ENOENT") throw new Error("User plugin installation state cannot be read");
    installation = bootstrapped ? cfg.installationId : randomUUID();
    try {
      const f = await open(installPath, "wx", 0o600);
      try { await f.writeFile(JSON.stringify({ id: installation })); } finally { await f.close(); }
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      installation = JSON.parse(await readFile(installPath, "utf8")).id;
    }
  }
  const runtime = base + "/api/v1/client-runtime";
  let cached = (bootstrapped ? cfg.catalog : null) ?? { models: {}, aliases: {}, images: [] };
  let hasCatalog = bootstrapped && Boolean(cfg.catalog);
  const cachePath = join(directory, "catalog.json");
  if (!bootstrapped) {
    try { cached = JSON.parse(await readFile(cachePath, "utf8")); hasCatalog = true; } catch { /* Display cache is optional. */ }
  }
  if (bootstrapped) await privateJSON(cachePath, cached);
  let userName = cached.user?.name ?? cfg.userName;
  if (cached.user && cached.user.id !== cfg.userId) throw new Error("Cached profile belongs to another user");
  if (!pricingWorker && cached.user) await syncAccountProfile(input, cfg, cached.user);
  else if (!pricingWorker && cfg.userName) await syncAccountProfile(input, cfg, { id: cfg.userId, name: cfg.userName });
  const expired = () => Object.assign(new Error("请重新登录 AIGateway 平台"), { signIn: "expired" });
  const call = async (path, body, access) => {
    const response = await fetch(base + path, {
      method: body === undefined ? "GET" : "POST", redirect: "manual",
      headers: { "Content-Type": "application/json", ...(access ? { Authorization: "Bearer " + access } : {}) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(20_000),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 || (path === "/api/v1/client-auth/token" && data.error === "invalid_grant")) throw expired();
      throw Object.assign(new Error(typeof data.error === "string" ? data.error : typeof data.detail === "string" ? data.detail : "平台请求未能完成"), { status: response.status });
    }
    return data;
  };
  const readQuota = quotaReader((access, fresh) =>
    call("/api/v1/client-runtime/usage" + (fresh ? "?fresh=true" : ""), undefined, access));
  const method = (label, purpose) => ({
    type: "oauth", label,
    async authorize() {
      const device = await call("/api/v1/client-auth/device-authorizations", {
        user_id: cfg.userId, binding: cfg.binding, installation_id: installation, purpose,
      });
      const verify = new URL(device.verification_uri);
      if (verify.origin !== base) throw new Error("Refusing an external platform sign-in URL");
      return { url: verify.href, method: "auto", instructions: `在平台确认当前账号和设备。Code: ${device.user_code}`,
        async callback() {
          const deadline = Date.now() + Math.min(device.expires_in, 300) * 1000;
          let wait = Math.max(5, device.interval);
          while (Date.now() < deadline) {
            await new Promise((resolve) => setTimeout(resolve, wait * 1000));
            try {
              const data = await call("/api/v1/client-auth/token", { grant_type: "device_code", device_code: device.device_code });
              const latest = await updateCatalog(data.access_token, true);
              await privateJSON(join(directory, "profile.json"), { userId: cfg.userId, selection: latest.selection });
              return { type: "success", access: data.access_token, refresh: data.refresh_token,
                expires: Date.now() + data.expires_in * 1000, accountId: userName ?? cfg.userId, uid: cfg.userId,
                installationId: installation };
            } catch (error) {
              if (error.message === "authorization_pending") continue;
              if (error.message === "slow_down") { wait = Math.min(wait + 5, 30); continue; }
              throw error;
            }
          }
          return { type: "failed", error: "登录验证已超时，请重新开始" };
        },
      };
    },
  });
  let imageSync;
  const syncImages = async (images, access) => {
    if (pricingWorker || !nativeMagpie || typeof Bun === "undefined") return;
    if (imageSync) { await imageSync.catch(() => {}); return; }
    imageSync = (async () => {
      const release = await imageFileLock(join(directory, "images-sync.lock"));
      try {
        localMagpieURL(process.env.MAGPIE_ADDR);
        const owner = { providerId: cfg.providerId, userId: cfg.userId, installationId: installation };
        const scope = { ...owner, settingsDirectory: input.directory, spec: fileURLToPath(new URL(".", import.meta.url)) };
        const imageId = cfg.providerId + "-images", statePath = join(directory, "images-runtime.json");
        const readyPath = join(directory, "images-ready.json");
        let previous;
        try { previous = JSON.parse(await readFile(statePath, "utf8")); } catch { /* Optional previous runtime. */ }
        let ready;
        try {
          ready = JSON.parse(await readFile(readyPath, "utf8"));
          if (!Number.isInteger(ready.port) || ready.port < 1 || ready.port > 65535
              || !Number.isInteger(ready.pid) || ready.pid < 1) throw new Error();
          const live = await fetch(`http://127.0.0.1:${ready.port}/health`, {
            headers: { Authorization: "Bearer " + ready.nonce }, signal: AbortSignal.timeout(2000),
          }).then(r => r.json());
          if (live.providerId !== cfg.providerId || live.pid !== ready.pid) throw new Error();
          if (live.bridgeVersion !== 4 || !images.length) {
            if (previous?.providerId === cfg.providerId && previous.userId === cfg.userId
                && previous.platformOrigin === base && previous.capability?.startsWith("tsci_"))
              await call("/api/v1/client-auth/revoke", {}, previous.capability).catch(() => {});
            process.kill(ready.pid, "SIGTERM");
            let stopped = false;
            for (let i = 0; i < 100; i++) {
              try { process.kill(ready.pid, 0); } catch { stopped = true; break; }
              await new Promise(r => setTimeout(r, 100));
            }
            if (!stopped) return;
            ready = undefined;
          }
        } catch { ready = undefined; }
        if (!images.length) {
          if (previous?.providerId === cfg.providerId && previous.userId === cfg.userId
              && previous.platformOrigin === base && previous.capability?.startsWith("tsci_"))
            await call("/api/v1/client-auth/revoke", {}, previous.capability).catch(() => {});
          await removeNativeImages(input, owner);
          await unlink(statePath).catch(() => {});
          return;
        }
        if (!await imagePluginEnabled(scope)) { await removeNativeImages(input, owner); return; }
        const capability = await call("/api/v1/client-runtime/image-capabilities", {}, access);
        const nonce = ready?.nonce ?? randomUUID() + randomUUID();
        const state = { ...owner, images, capability: capability.key, nonce, platformOrigin: base,
          settingsDirectory: input.directory, magpieAddress: process.env.MAGPIE_ADDR ?? "127.0.0.1:3425",
          spec: scope.spec,
          nativeCli: previous?.userId === cfg.userId ? previous.nativeCli : undefined };
        await privateJSON(statePath, state);
        if (!ready) {
          await unlink(readyPath).catch(() => {});
          const helper = spawn(process.execPath, ["run", fileURLToPath(new URL("./images.mjs", import.meta.url)), statePath, readyPath],
            { stdio: "ignore", detached: true, windowsHide: true });
          helper.unref();
          for (let i = 0; i < 50; i++) {
            try { ready = JSON.parse(await readFile(readyPath, "utf8")); break; } catch { await new Promise(r => setTimeout(r, 100)); }
          }
          if (!ready) throw new Error("The private image helper could not start");
        }
        const picture = { id: imageId, name: (cfg.name ?? "AIGateway User").replace(/^AIGateway User/, "AIGateway User Images"),
          key: nonce, chat: `http://127.0.0.1:${ready.port}/v1`, models: images.map(m => m.id), new: false };
        await privateJSON(join(directory, "images-provider.json"), picture);
        await privateJSON(join(directory, "images-mcp.json"), { mcpServers: { [imageId]: {
          url: `http://127.0.0.1:${ready.port}/mcp`, headers: { Authorization: "Bearer " + nonce },
        } } });
        if (!await imagePluginEnabled(scope)) { await removeNativeImages(input, owner); return; }
        const registered = await syncNativeImages(input, owner, picture);
        if (!await imagePluginEnabled(scope)) { await removeNativeImages(input, owner); return; }
        if (registered.state === "applied" && !registered.disabled)
          await syncPricing(input, { providerId: imageId }, Object.fromEntries(images.map(m => [m.id, m])),
            registered.cli ? { executable: registered.cli } : {});
        if (registered.cli && registered.cli !== state.nativeCli)
          await privateJSON(statePath, { ...state, nativeCli: registered.cli });
        await privateJSON(join(directory, "image-sync-status.json"), { state: "applied", modelCount: images.length });
      } finally { await release(); }
    })();
    try { return await imageSync; }
    catch (error) {
      if (error.code !== "EEXIST") {
        const status = { state: "pending", reason: "image-companion-unavailable" };
        let old;
        try { old = JSON.parse(await readFile(join(directory, "image-sync-status.json"), "utf8")); } catch { /* First failure. */ }
        if (old?.state !== status.state) console.warn("AIGateway image companion synchronization pending; text models remain available.");
        await privateJSON(join(directory, "image-sync-status.json"), status).catch(() => {});
      }
    } finally { imageSync = undefined; }
  };
  const updateCatalog = async (access, fresh = false) => {
    const data = await call(fresh ? "/api/v1/client-runtime/refresh-models" : "/api/v1/client-runtime/catalog", fresh ? {} : undefined, access);
    if (data.user) {
      if (data.user.id !== cfg.userId) throw new Error("Platform profile belongs to another user");
      if (!pricingWorker) await syncAccountProfile(input, cfg, data.user);
      userName = data.user.name;
    }
    const next = { user: data.user, models: data.models ?? {}, aliases: data.aliases ?? {}, images: data.images ?? [], selection: data.selection ?? null };
    // Keep a usable previous catalog if fetching or persistence fails.
    await privateJSON(cachePath, next);
    cached = next;
    hasCatalog = true;
    await syncImages(cached.images, access).catch(() => {});
    return data;
  };
  const refreshAuthorization = async (auth) => {
    const hex = createHash("sha256").update(auth.refresh).digest("hex").slice(0, 32);
    const requestId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    const data = await call("/api/v1/client-auth/token", { grant_type: "refresh_token", refresh_token: auth.refresh, request_id: requestId });
    return { ...auth, access: data.access_token, refresh: data.refresh_token, expires: Date.now() + data.expires_in * 1000 };
  };
  const currentAuthorization = async () => {
    if (input.project?.id !== "magpie" || !input.directory) throw expired();
    const accounts = JSON.parse(await readFile(join(input.directory, "plugin-auth.json"), "utf8"));
    let auth = Object.entries(accounts).find(([id, value]) =>
      (id === cfg.providerId || id.startsWith(cfg.providerId + "#")) && value.type === "oauth"
      && (value.uid === cfg.userId || value.accountId === cfg.userId))?.[1];
    if (!auth?.access || !auth.refresh) throw expired();
    if (auth.expires <= Date.now() + 60_000) {
      auth = await refreshAuthorization(auth);
      // Persist rotation before any later catalog or quota failure.
      await input.client.auth.set({ path: { id: cfg.providerId }, body: auth });
    }
    return auth;
  };
  const refreshModels = {
    type: "oauth", label: "刷新模型",
    async authorize() {
      return { url: "", method: "auto", instructions: "从平台读取当前绑定订阅的最新模型，不重新登录或切换订阅。",
        async callback() {
          const auth = await currentAuthorization();
          await updateCatalog(auth.access, true);
          return { ...auth, type: "success", accountId: userName ?? cfg.userId, uid: cfg.userId };
        },
      };
    },
  };
  const quotaTrend = {
    type: "oauth", label: "查看额度趋势",
    async authorize() {
      const auth = await currentAuthorization();
      const usage = await readQuota(auth.access);
      const file = await recordQuota(directory, cfg, usage);
      if (!file) throw new Error("平台未返回有效的额度快照时间");
      const view = pathToFileURL(file);
      view.searchParams.set("snapshot", usage.as_of);
      return { url: view.href, method: "auto",
        instructions: "打开本机个人额度快照曲线，不改变预算、不重新登录。再次点击可更新快照。",
        async callback() { return { ...auth, type: "success", accountId: userName ?? cfg.userId, uid: cfg.userId }; } };
    },
  };
  const controlReading = async () => {
    const auth = await currentAuthorization();
    const usage = await readQuota(auth.access, true);
    const window = personalQuota(usage);
    if (!window) throw new Error("未配置个人额度，不能启用 magpie 额度阻断");
    return { auth, forced: !window.aside };
  };
  const quotaControl = {
    type: "oauth", label: "额度控制",
    async authorize() {
      const { forced } = await controlReading();
      const target = forced ? null : !await localQuotaControl(directory, cfg);
      return { url: "", method: "auto",
        instructions: forced ? "平台已强制开启预算阻断，不可关闭。magpie 同步执行本地阻断。"
          : `本次切换目标：magpie 本地阻断${target ? "开启" : "关闭"}，保存完成后生效；平台预算和历史数据不变。`,
        async callback() {
          // The target is captured once so a repeated callback cannot toggle it back.
          const current = await controlReading();
          if (current.forced && target === false)
            throw new Error("平台已强制开启预算阻断，不可关闭");
          if (!current.forced && target !== null)
            await saveQuotaControl(directory, cfg, target);
          return { ...current.auth, type: "success", accountId: userName ?? cfg.userId, uid: cfg.userId };
        } };
    },
  };
  return {
    config: async (config) => {
      config.provider ??= {};
      config.provider[cfg.providerId] ??= { name: cfg.name, npm: "@ai-sdk/openai-compatible", api: runtime, models: cached.models };
    },
    auth: {
      provider: cfg.providerId,
      icon: cfg.icon,
      methods: [refreshModels, method("重新登录", "reauth"), quotaTrend, quotaControl],
      refreshLead: 60_000,
      async refresh(auth) {
        if (pricingWorker) return { access: auth.access, refresh: auth.refresh, expires: auth.expires };
        const next = await refreshAuthorization(auth);
        // Renew the image-only capability without ever sharing the main refresh token.
        // A local companion failure must not discard a successfully rotated OAuth token.
        if (cached.images.length) await syncImages(cached.images, next.access).catch(() => {});
        return { access: next.access, refresh: next.refresh, expires: next.expires };
      },
      async usage(getAuth) {
        if (pricingWorker) throw new Error("Usage is unavailable in a maintenance worker");
        const auth = await getAuth(); if (auth?.type !== "oauth" || !auth.access) throw expired();
        const usage = await readQuota(auth.access);
        await recordQuota(directory, cfg, usage).catch(() => {});
        return input.project?.id === "magpie" ? quotaForMagpie(directory, cfg, usage) : usage;
      },
      async loader(getAuth) {
        const auth = await getAuth(); if (auth?.type !== "oauth" || !auth.access) throw expired();
        return { baseURL: runtime, async fetch(input, init = {}) {
          const url = new URL(input instanceof Request ? input.url : String(input));
          // magpie URL-escapes Unicode plugin hosts before stripping the provider prefix.
          const escapedPrefix = runtime + "plugin://" + encodeURIComponent(cfg.providerId) + "/v1/";
          if (encodeURIComponent(cfg.providerId) !== cfg.providerId && url.href.startsWith(escapedPrefix))
            url.href = runtime + "/" + url.href.slice(escapedPrefix.length);
          const allowed = ["/responses", "/chat/completions", "/messages", "/v1/messages", "/models", "/v1/models"];
          const path = url.pathname.slice(new URL(runtime).pathname.length);
          if (url.origin !== base || !url.pathname.startsWith(new URL(runtime).pathname + "/")
              || !allowed.includes(path) || url.username || url.password || url.search || url.hash)
            throw new Error("Refusing credentials outside the user runtime");
          const current = await getAuth(); if (current?.type !== "oauth" || !current.access) throw expired();
          if (nativeMagpie && !(path === "/models" || path === "/v1/models")) {
            const usage = await readQuota(current.access);
            const window = personalQuota(usage);
            const refusal = quotaRefusal(usage, window && !window.aside ? true : await localQuotaControl(directory, cfg));
            if (refusal) return refusal;
          }
          const headers = new Headers(input instanceof Request ? input.headers : undefined);
          new Headers(init.headers).forEach((v, k) => headers.set(k, v));
          for (const k of ["authorization", "ocp-apim-subscription-key", "x-api-key", "x-user-id", "x-user-email", "x-turnstile-client-context"])
            headers.delete(k);
          headers.set("Authorization", "Bearer " + current.access);
          let model;
          try { model = input instanceof Request ? (await input.clone().json()).model : JSON.parse(init.body).model; } catch { /* Discovery has no body. */ }
          const request = input instanceof Request ? new Request(url, input) : url;
          const response = await fetch(request, { ...init, headers, redirect: "manual" });
          return adaptModelResponse(response, model, cached.aliases?.[model]);
        } };
      },
    },
    provider: { id: cfg.providerId, async models(provider, { auth } = {}) {
      if (auth?.type !== "oauth" || !auth.access) return {};
      if (!hasCatalog) await updateCatalog(auth.access);
      await syncImages(cached.images, auth.access);
      const pricing = pricingWorker ? { state: "external", resolved: [] } : await syncPricing(input, cfg, cached.models);
      // config uses Config.Model, but provider.models returns normalized Model.Info.
      return Object.fromEntries(Object.entries(cached.models).map(([id, model]) => [id, {
        id, providerID: cfg.providerId, name: model.name ?? id,
        cost: costForHost(pricingWorker ? null : input, cfg.providerId, id, model.cost, pricing),
        api: { id, url: model.provider?.api ?? runtime, npm: model.provider?.npm ?? "@ai-sdk/openai-compatible" },
        limit: model.limit ?? { context: 32768, output: 8192 },
        options: {}, headers: {}, variants: {},
        capabilities: {
          temperature: true, toolcall: model.tool_call === true, reasoning: model.reasoning === true,
          input: { text: true, image: model.modalities?.input?.includes("image") === true },
          output: { text: true },
        },
      }]));
    } },
  };
}
