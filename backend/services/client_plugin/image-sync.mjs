import { mkdir, open, readFile, unlink } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { privateJSON } from "./helpers.mjs";
import { imagePluginEnabled, localMagpieURL } from "./images.mjs";
import { imageFileLock, removeNativeImages, syncNativeImages } from "./native-images.mjs";
import { syncPricing } from "./pricing.mjs";

export function subscriptionImageSync(input, cfg) {
  let pending;
  return async function syncImages(images, auth) {
    if (input?.project?.id !== "magpie" || !input.directory || typeof Bun === "undefined"
        || process.env.AIGATEWAY_PRICING_WORKER === "1" || process.env.AIGATEWAY_NATIVE_IMAGE_WORKER === "1") return;
    if (pending) { await pending.catch(() => {}); return syncImages(images, auth); }
    pending = (async () => {
      const directory = join(input.directory, "aigateway-proxy-subscription", cfg.providerId);
      await mkdir(directory, { recursive: true, mode: 0o700 });
      const release = await imageFileLock(join(directory, "images-sync.lock"));
      try {
        localMagpieURL(process.env.MAGPIE_ADDR);
        const installPath = join(directory, "installation.json");
        try {
          const file = await open(installPath, "wx", 0o600);
          try { await file.writeFile(JSON.stringify({ id: randomUUID() })); } finally { await file.close(); }
        } catch (error) { if (error.code !== "EEXIST") throw error; }
        const installationId = JSON.parse(await readFile(installPath, "utf8")).id;
        const owner = { kind: "subscription", providerId: cfg.providerId, subscriptionId: cfg.subscriptionId, installationId };
        const scope = { ...owner, settingsDirectory: input.directory, spec: fileURLToPath(new URL(".", import.meta.url)) };
        const statePath = join(directory, "images-runtime.json"), readyPath = join(directory, "images-ready.json");
        if (!images.length) {
          await removeNativeImages(input, owner);
          // An existing companion sees the empty list and cleans itself up.
          try { await privateJSON(statePath, { ...JSON.parse(await readFile(statePath, "utf8")), images: [] }); }
          catch (error) { if (error.code !== "ENOENT") throw error; }
          await privateJSON(join(directory, "image-sync-status.json"), { state: "absent", modelCount: 0 });
          return;
        }
        const accounts = JSON.parse(await readFile(join(input.directory, "plugin-auth.json"), "utf8"));
        const accountSlot = Object.keys(accounts).find(id => (id === cfg.providerId || id.startsWith(cfg.providerId + "#"))
          && accounts[id]?.type === "api" && accounts[id].key === auth?.key);
        if (!accountSlot || !await imagePluginEnabled({ ...scope, accountSlot })) return;
        let ready;
        try {
          ready = JSON.parse(await readFile(readyPath, "utf8"));
          if (!Number.isInteger(ready.port) || ready.port < 1 || ready.port > 65535
              || !Number.isInteger(ready.pid) || ready.pid < 1) throw new Error();
          const live = await fetch(`http://127.0.0.1:${ready.port}/health`, {
            headers: { Authorization: "Bearer " + ready.nonce }, signal: AbortSignal.timeout(2000),
          }).then(r => r.json());
          if (live.providerId !== cfg.providerId || live.pid !== ready.pid || live.bridgeVersion !== 4) throw new Error();
        } catch { ready = undefined; }
        let previous;
        try { previous = JSON.parse(await readFile(statePath, "utf8")); } catch { /* Optional previous descriptor. */ }
        const nonce = ready?.nonce ?? randomUUID() + randomUUID();
        const state = { ...scope, accountSlot, images, nonce, baseURL: cfg.baseURL,
          magpieAddress: process.env.MAGPIE_ADDR ?? "127.0.0.1:3425", nativeCli: previous?.nativeCli };
        await privateJSON(statePath, state);
        if (!ready) {
          await unlink(readyPath).catch(() => {});
          const child = spawn(process.execPath, ["run", fileURLToPath(new URL("./images.mjs", import.meta.url)), statePath, readyPath],
            { stdio: "ignore", detached: true, windowsHide: true });
          child.unref();
          for (let i = 0; i < 50; i++) {
            try { ready = JSON.parse(await readFile(readyPath, "utf8")); break; }
            catch { await new Promise(resolve => setTimeout(resolve, 100)); }
          }
          if (!ready) throw new Error("Image companion unavailable");
        }
        const picture = { id: cfg.providerId + "-images", name: cfg.name + " Images", key: nonce,
          chat: `http://127.0.0.1:${ready.port}/v1`, models: images.map(m => m.id), new: false };
        await privateJSON(join(directory, "images-provider.json"), picture);
        await privateJSON(join(directory, "images-mcp.json"), { mcpServers: { [picture.id]: {
          url: `http://127.0.0.1:${ready.port}/mcp`, headers: { Authorization: "Bearer " + nonce },
        } } });
        if (!await imagePluginEnabled(state)) { await removeNativeImages(input, owner); return; }
        const registered = await syncNativeImages(input, owner, picture);
        if (!await imagePluginEnabled(state)) { await removeNativeImages(input, owner); return; }
        if (registered.state === "applied" && !registered.disabled)
          await syncPricing(input, { providerId: picture.id }, Object.fromEntries(images.map(m => [m.id, m])),
            registered.cli ? { executable: registered.cli } : {});
        if (registered.cli && registered.cli !== state.nativeCli)
          await privateJSON(statePath, { ...state, nativeCli: registered.cli });
        await privateJSON(join(directory, "image-sync-status.json"), { state: "applied", modelCount: images.length });
      } finally { await release(); }
    })();
    try { await pending; }
    catch (error) {
      if (error.code !== "EEXIST") await privateJSON(join(input.directory, "aigateway-proxy-subscription", cfg.providerId,
        "image-sync-status.json"), { state: "pending", reason: "image-companion-unavailable" }).catch(() => {});
    }
    finally { pending = undefined; }
  };
}
