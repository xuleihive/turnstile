import { mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import { join } from "node:path";
import { costForHost, syncPricing, tokenCost } from "./pricing.mjs";
import { adaptModelResponse, privateJSON } from "./helpers.mjs";
import { subscriptionImageSync } from "./image-sync.mjs";

async function durableWrite(path, content) {
  const temporary = `${path}.aigateway-${process.pid}-${crypto.randomUUID()}`;
  const file = await open(temporary, "wx", 0o600);
  try {
    await file.writeFile(content);
    await file.sync();
    await file.close();
    await rename(temporary, path);
  } finally {
    await file.close().catch(() => {});
    await unlink(temporary).catch(() => {});
  }
}

async function importOnce(input, cfg) {
  // Only magpie's documented config directory is eligible; OpenCode stays manual.
  if (input?.project?.id !== "magpie" || !input.directory) return;
  const authPath = join(input.directory, "plugin-auth.json");
  const markerDirectory = join(input.directory, "aigateway-proxy-imports");
  const marker = join(markerDirectory, `${cfg.providerId}.json`);
  let lock;
  try {
    try { await readFile(marker); return; }
    catch (error) { if (error.code !== "ENOENT") return; }
    await mkdir(input.directory, { recursive: true, mode: 0o700 });
    // Share magpie's auth lock, but never break an existing lock or replace unreadable auth.
    const deadline = Date.now() + 2000;
    while (!lock) {
      try { lock = await open(authPath + ".lock", "wx", 0o600); }
      catch (error) {
        if (error.code !== "EEXIST" || Date.now() >= deadline) return;
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
    }
    let previous;
    try { previous = await readFile(authPath, "utf8"); }
    catch (error) { if (error.code !== "ENOENT") return; }
    const accounts = previous === undefined ? {} : JSON.parse(previous);
    if (!accounts || typeof accounts !== "object" || Array.isArray(accounts)) return;
    const exists = Object.keys(accounts).some((id) => id === cfg.providerId || id.startsWith(cfg.providerId + "#"));
    await mkdir(markerDirectory, { recursive: true, mode: 0o700 });
    // Mark before importing: logout, reload, and a concurrent host must not resurrect the key.
    const mark = await open(marker, "wx", 0o600).catch((error) => {
      if (error.code === "EEXIST") return null;
      throw error;
    });
    if (!mark) return;
    try { await mark.writeFile(JSON.stringify({ version: 1, providerId: cfg.providerId })); await mark.sync(); }
    finally { await mark.close(); }
    if (exists) return;
    accounts[cfg.providerId] = { type: "api", key: cfg.subscriptionKey, accountId: cfg.accountId };
    if (previous !== undefined) await durableWrite(authPath + ".bak", previous);
    await durableWrite(authPath, JSON.stringify(accounts, null, 2) + "\n");
  } catch {
    // Auto-import is best effort; the manual import remains available without logging secrets.
  } finally {
    if (lock) { await lock.close(); await unlink(authPath + ".lock").catch(() => {}); }
  }
}

export default async function AIGatewayProxyPlugin(input) {
  const cfg = JSON.parse(await readFile(new URL("./aigateway.config.json", import.meta.url), "utf8"));
  const base = new URL(cfg.baseURL);
  if (cfg.version !== 1 || base.protocol !== "https:" || base.username || base.password || base.search || base.hash)
    throw new Error("Invalid AIGateway configuration");
  if (!/^aigateway-[\p{L}\p{N}-]+$/u.test(cfg.providerId) || typeof cfg.subscriptionKey !== "string" || !cfg.subscriptionKey)
    throw new Error("Invalid AIGateway subscription configuration");
  const worker = process.env.AIGATEWAY_PRICING_WORKER === "1" || process.env.AIGATEWAY_NATIVE_IMAGE_WORKER === "1";
  if (!worker) await importOnce(input, cfg);
  const syncImages = subscriptionImageSync(input, cfg);
  let catalog = { models: cfg.models ?? {}, images: cfg.images ?? [] };
  const directory = input?.project?.id === "magpie" && input.directory
    ? join(input.directory, "aigateway-proxy-subscription", cfg.providerId) : null;
  if (directory && !worker) {
    try {
      const cached = JSON.parse(await readFile(join(directory, "catalog.json"), "utf8"));
      if (cached.models && Array.isArray(cached.images)
          && Object.keys(cached.models).every(id => Object.hasOwn(cfg.models ?? {}, id))
          && cached.images.every(m => cfg.images?.some(b => b.id === m.id))) catalog = cached;
      else catalog = { models: {}, images: [] };
    } catch (error) {
      if (error.code !== "ENOENT") catalog = { models: {}, images: [] };
    }
  }
  const root = cfg.baseURL.replace(/\/$/, "");
  const endpoints = new Set(["/chat/completions", "/responses", "/messages", "/v1/messages", cfg.modelsPath]);
  const expired = () => Object.assign(new Error("AIGateway subscription rejected; check access or rotate the key"), { signIn: "expired" });
  const dispatch = async (key, input, init = {}) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    // Repair only this provider's escaped plugin prefix, never another host or path.
    const escapedPrefix = root + "plugin://" + encodeURIComponent(cfg.providerId) + "/v1/";
    if (encodeURIComponent(cfg.providerId) !== cfg.providerId && url.href.startsWith(escapedPrefix))
      url.href = root + "/" + url.href.slice(escapedPrefix.length);
    if (url.origin !== base.origin || !endpoints.has(url.pathname.slice(base.pathname.replace(/\/$/, "").length))
        || !url.pathname.startsWith(base.pathname.replace(/\/$/, "") + "/") || url.username || url.password || url.search || url.hash)
      throw new Error("Refusing to send subscription credentials outside the configured gateway");
    const headers = new Headers(input instanceof Request ? input.headers : undefined);
    new Headers(init.headers).forEach((value, name) => headers.set(name, value));
    for (const name of ["authorization", "proxy-authorization", "cookie", "x-api-key", "x-goog-api-key",
      "x-user-id", "x-user-email", "x-user-name", "x-org-id", "x-org-name", "x-department-id", "x-department-name",
      "x-turnstile-client-context", "x-turnstile-delegation-token"])
      headers.delete(name);
    headers.set("Ocp-Apim-Subscription-Key", key);
    // The SDK appends /messages; Turnstile publishes Anthropic at /v1/messages.
    if (url.pathname === base.pathname.replace(/\/$/, "") + "/messages")
      url.pathname = base.pathname.replace(/\/$/, "") + "/v1/messages";
    const request = input instanceof Request ? new Request(url, input) : url;
    return fetch(request, { ...init, headers, redirect: "manual" });
  };
  const finish = async (providerModels, auth, fallback = false) => {
    const models = Object.fromEntries(Object.entries(catalog.models).filter(([id]) => Object.hasOwn(providerModels, id)));
    const pricing = worker ? { state: "external", resolved: [] } : await syncPricing(input, cfg, models);
    if (!worker) await syncImages(catalog.images, auth);
    const result = Object.fromEntries(Object.entries(models).map(([id, model]) => {
      const provided = providerModels[id];
      const info = provided.api && provided.capabilities ? provided : {
        id, providerID: cfg.providerId, name: model.name ?? id,
        api: { id, url: model.provider?.api ?? root, npm: model.provider?.npm ?? "@ai-sdk/openai-compatible" },
        limit: model.limit ?? { context: 32768, output: 8192 }, options: {}, headers: {}, variants: {},
        capabilities: { temperature: true, toolcall: model.tool_call === true, reasoning: model.reasoning === true,
          input: { text: true, image: model.modalities?.input?.includes("image") === true }, output: { text: true } },
      };
      return [id, { ...info, cost: costForHost(worker ? null : input, cfg.providerId, id, model.cost, pricing) }];
    }));
    if (fallback) result[Symbol.for("magpie.fellBack")] = true;
    return result;
  };
  const discover = async (auth, providerModels, strict = false) => {
    if (worker) return finish(providerModels, auth);
    let response;
    try { response = await dispatch(auth.key, root + cfg.modelsPath, { signal: AbortSignal.timeout(20_000) }); }
    catch { if (strict) throw new Error("Gateway discovery is unavailable; login was preserved"); return finish(providerModels, auth, true); }
    if (response.status === 401 || response.status === 403) throw expired();
    let body;
    try { body = response.ok ? await response.json() : null; } catch { /* An invalid catalog is not an empty grant. */ }
    if (!Array.isArray(body?.data)) {
      if (strict) throw new Error("Gateway discovery is unavailable; login was preserved");
      return finish(providerModels, auth, true);
    }
    const allowed = new Map(body.data.filter(m => m && typeof m.id === "string").map(m => [m.id, m]));
    const priced = model => ({ ...model, cost: Object.hasOwn(allowed.get(model.id), "cost")
      ? tokenCost(allowed.get(model.id).cost) : model.cost });
    const next = { models: Object.fromEntries(Object.entries(cfg.models ?? {}).filter(([id]) => allowed.has(id))
      .map(([id, model]) => [id, priced({ ...model, id })])),
    images: (cfg.images ?? []).filter(m => allowed.has(m.id)).map(priced) };
    if (directory) { await mkdir(directory, { recursive: true, mode: 0o700 }); await privateJSON(join(directory, "catalog.json"), next); }
    catalog = next;
    return finish(providerModels, auth);
  };
  const currentAuthorization = async () => {
    if (worker || !directory) throw expired();
    const accounts = JSON.parse(await readFile(join(input.directory, "plugin-auth.json"), "utf8"));
    const own = Object.entries(accounts).filter(([id, value]) => (id === cfg.providerId || id.startsWith(cfg.providerId + "#"))
      && value?.type === "api" && typeof value.key === "string" && value.key);
    if (!own.length) throw expired();
    if (new Set(own.map(([, auth]) => auth.key)).size > 1)
      throw new Error("Multiple saved subscription keys; refresh through the selected account's model list instead");
    return own[0][1];
  };
  const completeAuthorization = async auth => {
    if (worker) throw new Error("Sign-in is unavailable in a maintenance worker");
    if (input?.project?.id === "magpie" && typeof input.client?.auth?.set === "function") {
      // Magpie drops top-level API-account metadata from a key callback. Stage the
      // full account through its SDK so settlement reuses the existing named slot.
      await input.client.auth.set({ path: { id: cfg.providerId }, body: auth });
      return { type: "success" };
    }
    return { ...auth, type: "success", metadata: { ...auth.metadata,
      email: auth.accountId ?? auth.metadata?.email ?? cfg.accountId } };
  };
  return {
    config: async (config) => {
      config.provider ??= {};
      config.provider[cfg.providerId] ??= {
        name: cfg.name, npm: "@ai-sdk/openai-compatible", api: root, models: cfg.models,
      };
    },
    auth: {
      provider: cfg.providerId,
      icon: cfg.icon,
      methods: [
        {
          type: "oauth", label: "导入配置",
          async authorize() {
            return { url: "", method: "auto", instructions: "Import the subscription bundled with this private download.",
              async callback() {
                if (directory && !worker) {
                  try {
                    const accounts = JSON.parse(await readFile(join(input.directory, "plugin-auth.json"), "utf8"));
                    const existing = Object.entries(accounts).find(([id, auth]) => (id === cfg.providerId || id.startsWith(cfg.providerId + "#"))
                      && auth?.type === "api" && auth.key === cfg.subscriptionKey)?.[1];
                    if (existing) return completeAuthorization(existing);
                  } catch { /* The host's manual import remains available without logging private state. */ }
                }
                return completeAuthorization({ type: "api", key: cfg.subscriptionKey, accountId: cfg.accountId });
              } };
          },
        },
        { type: "api", label: "更新API Key" },
        { type: "oauth", label: "刷新模型", async authorize() {
          return { url: "", method: "auto", instructions: "刷新本下载快照与网关授权的交集，不导入、轮换密钥或切换订阅。",
            async callback() {
              const auth = await currentAuthorization();
              await discover(auth, cfg.models ?? {}, true);
              const current = await currentAuthorization();
              if (current.key !== auth.key) throw new Error("Subscription authorization changed during refresh; try again");
              return completeAuthorization(current);
            } };
        } },
      ],
      async loader(getAuth) {
        const auth = await getAuth();
        if (auth?.type !== "api" || !auth.key) throw expired();
        return { baseURL: root, async fetch(input, init = {}) {
          const current = await getAuth();
          if (current?.type !== "api" || !current.key) throw expired();
          let model;
          try { model = typeof init.body === "string" ? JSON.parse(init.body).model
            : input instanceof Request ? (await input.clone().json()).model : undefined; } catch { /* Discovery has no body. */ }
          return adaptModelResponse(await dispatch(current.key, input, init), model, cfg.aliases?.[model]);
        } };
      },
    },
    provider: {
      id: cfg.providerId,
      async models(provider, { auth } = {}) {
        if (auth?.type !== "api" || !auth.key) return {};
        return discover(auth, provider.models ?? cfg.models ?? {});
      },
    },
  };
}
