# AIGateway-Proxy for magpie

This is a **private** plugin download. `aigateway.config.json` contains an APIM
subscription key. Do not upload this folder to npm, GitHub, chat, or a shared drive.

From 0.1.3, the prepared platform icon is bundled under icons/ and declared in
both auth.icon and package.json.magpie.icon. Downloads read the versioned files;
they do not render or fetch icons. The platform badge is legible in light/dark
themes. An installed older package must be replaced to receive a new brand icon.

## 安装 / Install

1. 解压 ZIP，把与 ZIP 同名（不含 `.zip`）的文件夹移动到自己保管的长期目录。
   例如 `aigateway-proxy-Edwin-Agent.zip` 解压为 `aigateway-proxy-Edwin-Agent/`。
2. 在 magpie 的「插件」页面，选择本地文件夹，添加该文件夹（不是 ZIP）。
3. 首次加载时自动导入配置中的密钥到 magpie 的 `plugin-auth.json`（权限 0600）；
   已有供应商登录信息不会被覆盖。自动导入失败时，点击供应商选择「导入配置」。
   不会修改已有 Agent 配置。
4. 供应商和模型出现后，在 Agent 页面选择该供应商的模型。

CLI equivalent (uses your normal magpie configuration):

```sh
magpie plugin add /absolute/path/aigateway-proxy-Edwin-Agent
# Usually not needed after installation; this explicitly re-imports the bundled key.
magpie plugin login <providerId-from-aigateway.config.json> 1
magpie provider test <providerId-from-aigateway.config.json>
```

From 0.1.2, model namespaces use the subscription name plus a short hashed
disambiguator, not the subscription UUID. Existing 0.1.1 packages remain supported
and are not silently renamed. Replace an old installation explicitly and reselect
its models in the Agent; installing both generations creates separate providers.

The exact provider ID is `providerId` in the configuration. Each subscription has
a distinct ID. Keep the folder in place: magpie loads local plugins in place.
For development tests, isolate HOME, XDG_CONFIG_HOME, XDG_CACHE_HOME and MAGPIE_ADDR.

From 0.1.2, the private snapshot includes the platform's complete token prices
(USD per million tokens). Gateway discovery can update a bundled model's prices
when it returns a complete `cost` object; discovery never grants a new model.
The shared pricing adapter imports only this provider's prices through the
existing `magpie model price` CLI, preserving manual overrides and other providers.
Complete cache prices are never replaced by zero to work around a missing CLI.
The normal magpie data directory and native CLI are needed for automatic import;
`AIGATEWAY_MAGPIE_EXECUTABLE` can name the installed executable explicitly.
Status is kept under `aigateway-pricing/<providerId>/status.json`.

The plugin sends requests only to the selected HTTPS gateway, with the selected
APIM subscription key; agent credentials are not forwarded. Redirects are not
followed. Responses and cancellation remain streaming. Models are a download-time
snapshot intersected with gateway discovery, never a grant of access: live APIM
admission, quotas and subscription revocation always apply.

Automatic import runs once per provider in magpie. A non-secret marker in
`aigateway-proxy-imports/<providerId>.json` prevents restart/reload or a deliberate
sign-out from signing you back in. Existing accounts, including additional accounts,
are preserved. Concurrent imports use magpie's auth-file lock and preserve other
providers and the prior auth-file backup. Unreadable auth files or lock/write failures
are left alone: use the manual method. OpenCode does not auto-import.

「导入配置」 explicitly imports the key in this download. 「更新API Key」 asks for
a key you enter; it does not rotate a key in APIM. After key rotation, use 更新API Key
or download a fresh package and manually import it. A fresh package does not
automatically overwrite a previous login. After model grants change, download again to add new models.
If magpie still shows no provider after installation, switch this plugin off and on
to reload its host; this does not restart other agents.
Requests, prompts and subscription credentials are not logged by this plugin.
No runtime dependencies, install scripts or automatic updater are included.

## Portable user-plugin fixes (0.2.0)

New downloads include authorized image-only models separately from conversation
models. They keep the original gateway IDs and complete platform token prices.
The companion and native-registration engine are shared with the user plugin:
native absolute paths, ownership/readback, manual edits and disabled providers,
loopback MCP fallback, private files, worker guards and bounded CLI imports follow
the same rules. Image prices use the independent `<providerId>-images/modelId`
namespace. Existing manual prices always win; unknown prices are not zero.

The image companion reads the current APIM key from its exact saved account slot
on each invocation. Runtime/ready files and native-provider/MCP descriptors contain
only the local nonce, never an APIM key or personal OAuth token. Turning the plugin
off, removing it or signing out stops the companion and cleans only its unmodified
owned native provider. It does not revoke or rotate a key in APIM. The gateway and
image IDs are checked against this installed subscription's private download.
Local state is under `aigateway-proxy-subscription/<providerId>/`; pending native
registration is in `native-images-status.json`, companion setup in
`image-sync-status.json`, and price imports in `aigateway-pricing/<providerId>-images/`.
Automatic native registration needs the normal Magpie config directory and CLI;
`images-provider.json` and `images-mcp.json` remain private manual fallbacks.

The new third action, `刷新模型`, uses the current key without importing the bundled
key, rotating it or switching subscriptions. Import and Update API Key retain their
original method positions. Refresh remains the download snapshot intersected with
live discovery; adding models outside that snapshot requires a fresh download.
Empty discovery is authoritative; an offline fallback cannot resurrect an earlier
removed model from a wider host list. Discovery can update complete prices but
cannot expand authorization. Failed explicit refresh preserves the saved login.
With several different saved keys, the action refuses to guess an account; use
the selected account's normal model refresh instead.
Native API-account actions preserve full account metadata through the host SDK,
so refresh does not create an extra unnamed account with the same key. Logout or
a changed key during discovery aborts refresh completion rather than restoring it.

Text dispatch reads the current authorization each time, preserves streaming and
normalizes only exact upstream model aliases in JSON/SSE, including Anthropic
message-start events. Real model differences, tools, IDs and usage remain unchanged.
Host metadata includes the declared SDK, context and vision/tools/reasoning
capabilities. Foreign URLs, queries/fragments and forged user/delegation headers
cannot carry this plugin's credentials elsewhere.

Personal OAuth sign-in, personal profile renaming, personal Token/USD curves and
the user's local personal-budget switch are not subscription-key features. This
plugin has no trustworthy subscription allowance API and does not invent zero or
shared/personal counters. Existing APIM authorization, limits and budget enforcement
remain authoritative. Both plugins remain independent; this package never adopts a
user plugin's account, image provider or personal allowance.

Replace the installed plugin files and reload to upgrade code; refreshing models
alone does not upgrade an old package. Host repricing of recorded image tokens uses
current imported rates and does not rewrite the platform's historical bills.
