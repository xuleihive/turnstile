# Native image registration for the AIGateway user plugin

This specification retains the image-registration contract and dated validation
boundaries. Version numbers, host availability and work checklists below describe
the original implementation, not the current installation. See the
[release index](release-history.md) for subsequent delivery and the
[plugin design](design.md) for the consolidated current contract.

## Objective

Make the user's authorized image models appear automatically in Magpie's native
image-generation selector. Keep a single macOS/Windows plugin and retain the
gateway's original model IDs, including the Foundry suffix. Native registration
must use supported Magpie CLI commands, not writes to Magpie's configuration or
the unavailable desktop `/api/provider/save` endpoint.

The previous 0.4.5 fix keeps the image companion alive on Windows. It does not
register that companion as a native image provider. The companion currently
exports three models, while the OAuth plugin registers five text models.

## Selected approach and alternatives

1. Selected: register a separate, plugin-owned native image provider through the
   Magpie CLI. Its URL and key point to the existing loopback-only companion.
   This populates the native selector and supports the existing `magpie-image`
   MCP without changing Magpie application source.
2. HTTP MCP only: retain this fallback, but it cannot populate the native image
   selector by itself.
3. Add image models to the OAuth text provider: not selected. Current Magpie
   drawing and plugin loader behavior do not provide that native image path.

## Registration contract

- Provider ID is exactly `<user-provider-id>-images`, distinct from the existing
  text provider and subscription plugin.
- Image IDs are copied verbatim from the authorized catalog. Do not strip
  suffixes, invent aliases, derive IDs from display names, or infer authorization
  from an image-like name.
- Native URL is the companion's current loopback `/v1` endpoint. Native key is
  its local nonce; no APIM key, OAuth access token or refresh token is handed to
  the native provider or a CLI command.
- Only supported CLI syntax is used, with argument arrays, no shell expansion,
  hidden Windows subprocesses, bounded timeouts and suppressed sensitive output.
- Locate the native CLI from an explicit override, the running host, PATH or the
  standard macOS installation. Confirm that its configuration directory matches
  the plugin's directory. Unsupported/ambiguous installations remain MCP-only
  with a sanitized pending status rather than writing to another installation.
- Keep the user's existing default image selection, provider switches, prices,
  routing groups and Agent configurations unchanged. In particular, do not pick
  one of the three image models on the user's behalf.

## Ownership and lifecycle

- Maintain a private, versioned record of successful native registration and its
  last managed endpoint, key and model list. The legacy ID-only ownership file
  is not sufficient evidence to overwrite an existing provider.
- A preexisting, unowned provider with the same ID is a conflict. Preserve it and
  report the conflict; do not rename another provider or create numbered copies.
- Preserve manual changes and a manually disabled native provider. Update only
  fields still matching this installation's last managed values.
- Serialize this plugin's registration attempts. Skip no-op updates and verify
  the resulting provider and model list after every successful command.
- Refresh the owned model list after catalog changes, including removals. Update
  the endpoint/local key when the companion changes. Do not restart or rename
  the text provider or restore a signed-out account.
- CLI maintenance workers must not bootstrap authorization, rotate unrelated
  accounts, recurse into registration/pricing, or start extra image companions.
- On logout, disable, removal or loss of image authorization, revoke/stop the
  companion as before. Remove a native provider only when current registration
  still matches this installation's ownership record. Never remove a modified
  or unowned provider. A retained stale card must not keep calling the platform.
- A CLI failure must not discard a valid OAuth refresh or the working HTTP MCP.
  Record a sanitized failure status; do not silently claim registration worked.

The native CLI's `key=` interface accepts the local nonce as an argument. Do not
log command arguments or raw subprocess errors. The nonce is a local credential,
not the platform capability or OAuth credential. A future stdin-based native
interface can improve this without weakening ownership or login checks.

## Source and local rollout

- Add an isolated native-registration helper, reused by the plugin and companion
  cleanup without introducing a circular import.
- Update the user package file list, regression fixtures, README and version to
  0.4.6. Keep the subscription plugin unchanged.
- Patch the locally installed plugin only after isolated tests pass. Back up code
  and package metadata without copying private bootstrap/runtime files into Git.
- The user authorizes committing and pushing this feature branch after local
  tests pass. Do not merge main, deploy the server, alter APIM, change user
  permissions/budgets, or edit WorkBuddy's trust/approval records automatically.

## Test plan

- Unit tests: exact native IDs; fresh registration; no-op refresh; changed model
  list and endpoint; conflicts; manual edits/disable; unavailable CLI; timeout;
  sanitized errors; worker recursion guards; owned-only cleanup.
- Isolated native CLI/Bun tests with temporary configuration and a loopback fake
  platform: provider readback, model discovery, native Magpie image routing, and
  simulated image generation without a paid upstream request.
- Run existing user/subscription plugin, pricing, quota, lifecycle, package and
  frontend tests. Preserve the existing Windows portability fixes.
- Local desktop verification: the three image models appear as a native image
  provider and in the selector, using the unchanged original IDs; five text
  models and existing providers/settings remain intact.
- macOS code paths receive unit coverage, but native macOS acceptance requires a
  macOS host and must not be claimed from Windows-only testing.

## Controlled real-image acceptance

The user permits a low-cost real test, preferably initiated from WorkBuddy.

- Preflight the MCP trust/connection, selected provider, model mapping and
  available quota before sending any generation request.
- At most one real generation request, `n=1`, with a short simple prompt, low
  quality and the smallest supported size. If low quality/size is rejected, stop;
  do not automatically retry with higher quality or another model.
- WorkBuddy may use only the approved `magpie-image` MCP, not its built-in
  `ImageGen` or another provider as a fallback. Never bypass its trust prompt.
- If the tool returns an in-progress job, poll `generation_result` for that same
  job instead of starting another generation.
- Record request count, result and any usage/cost actually returned by the
  platform. Do not promise a fixed dollar cost without a verified tariff.
- If WorkBuddy cannot be driven or has not been trusted, complete isolated and
  read-only local verification, then request the required human action. Do not
  claim WorkBuddy acceptance from a standalone MCP probe.

## Work checklist

- [x] Inspect current plugin, Magpie CLI and registration limitations.
- [x] Select the independent native-provider approach and preserve model IDs.
- [x] Record real-test request/cost boundaries and self-review this specification.
- [x] Obtain review of this written specification before implementation.
- [x] Implement registration, ownership, worker guards and lifecycle integration.
- [x] Run isolated tests, patch the local plugin and verify native discovery.
- [ ] Perform at most one authorized real-image test if WorkBuddy is ready.
- [x] Record results, skipped cases and remaining limitations.

## Implementation and verification (2026-10-14)

- User package version is 0.4.6. The download includes `native-images.mjs` and the
  installed local copy uses the same source. No server deployment was performed.
- Native registration uses add/set/rm with explicit ownership, readback and
  sanitized status. The legacy ID-only marker cannot adopt an existing provider.
  Original model IDs, manual edits, manually disabled cards and defaults remain
  protected. CLI save success can be recovered if later discovery times out.
- Windows native testing revealed that a companion started by a short-lived CLI
  host could die with that host. The companion now uses a detached, unreferenced
  child with hidden Windows startup; its own login/registration checks still stop
  it and clean up the owned native provider.
- JS regression: 80 passed, zero failed/skipped. This includes native CLI/Bun
  registration, automatic image-model selection, one simulated generation and
  cleanup against an isolated loopback platform. The original gateway model ID
  reaches the platform fixture without renaming.
- Targeted Python package/API/frontend harness: 29 passed, eight skipped because
  local PostgreSQL is unavailable. Ruff and the TypeScript/Vite build pass. The
  existing Starlette deprecation and large-chunk warnings remain.
- Local desktop readback: exactly one owned native image provider, three image
  IDs visible on the gateway, five text models retained, native status `applied`,
  image companion version 4. Existing provider records, settings, plugin
  registrations, private bootstrap and WorkBuddy MCP/trust files are unchanged.
- WorkBuddy's approval file remains empty and its tool cache does not contain
  `magpie-image`. Real WorkBuddy acceptance is deferred until the human trusts
  the MCP inside WorkBuddy. No trust bypass, built-in ImageGen fallback, actual
  upstream generation or real generation fee was incurred (zero paid requests).
- Native macOS execution was not available; platform-neutral contracts are tested
  but do not constitute a macOS acceptance result. Private runtime proof stays in
  ignored local storage and no credentials are included in this repository.

## Subsequent merge and production release (2026-10-16)

The user subsequently authorized merging `codex/plugin-windows-paths` into
`codex/aigateway-proxy-client` and publishing the plugin. User version 0.4.6 is now
deployed; subscription version 0.1.4 is unchanged. The production ZIP passed
isolated native macOS CLI/Bun registration and one simulated generation. No new
Windows-device or paid WorkBuddy generation acceptance is claimed. See
[the release index](release-history.md) for the source and historical record
retrieval; the original record contains package checksums, business-health checks,
activation caveats and remaining limits.
