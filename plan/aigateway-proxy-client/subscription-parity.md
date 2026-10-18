# Subscription plugin parity

Status: implemented, deployed to the existing online test environment and validated
on 2026-10-18. Existing installed plugins were not upgraded.

## Scope

Bring portable user-plugin fixes into the subscription plugin without changing its
APIM-key authorization, exported application/subscription/gateway binding, model
snapshot boundary, or another provider's state. Previous user-image pricing changes
in this worktree are retained. The follow-up request authorizes publishing to the
existing online test environment and committing/pushing all reviewed changes to
the user-owned repository. Installed-plugin updates, paid inference, budget edits,
permission changes and migrations remain outside scope. Online acceptance is
recorded separately from implementation and local checks.

## Audit

Compared the current user implementation and the history at `67b530e` (namespace,
prices and curves), `010d0f8`/`1c2f822` (personal control and icons), `e63465b`
(archive root), `b97ca72`/`10d3efd` (paths, native images and lifecycle),
`10fb56f`/`b10115a` (response aliases and helper isolation), `dd68660` (personal
USD/delegation) and the user 0.5.1 image-pricing patch. These locate source changes;
they are not evidence that any current installed or deployed package contains them.

| Capability | Subscription baseline | Action |
| --- | --- | --- |
| Readable namespace, ZIP root, offline icons, safe one-time import | Present | Preserve and regress |
| Four platform token prices and manual-price precedence | Present | Reuse for text and independent image provider |
| Published and authorized image catalog | Missing | Export image entries separately, including prices and aliases; support image-only subscriptions |
| Native images/MCP, Windows paths, ownership and lifecycle | Missing | Reuse user bridge/native-registration engine with explicit subscription mode |
| Response model aliases, split UTF-8 SSE, Messages | Missing alias adapter | Reuse exact-match streaming adapter; preserve tools, IDs, usage and cancellation |
| Explicit refresh and current-key dispatch | Missing refresh; loader captures old key | Refresh snapshot intersection without importing/rotating keys; re-read current authorization per request |
| Existing API account identity on explicit actions | Key callbacks lose top-level account names in Magpie | Stage full API auth via the host SDK and let its normal settlement reuse the account; real CLI verifies unchanged slots, key, name and manual image price |
| URL/credential boundaries and maintenance workers | Partial | Reject query/fragment and forged delegation headers; both worker flags prevent import, companions and discovery |
| Personal OAuth/profile/default subscription resolution | Different authority | Do not copy; keep existing subscription identity and key selection |
| Personal quota curves, USD local switch and budget download gate | No authenticated subscription allowance endpoint | Do not manufacture a personal/shared allowance or weaken APIM enforcement |

## Contracts

- A new subscription download remains version-1 config-compatible and retains the
  provider namespace. Version 0.2.0 includes separate `models`, `images` and exact
  `aliases`; discovery can remove or update bundled entries but never add an ID.
- A successful empty discovery is authoritative. Network failures use only the
  last accepted intersection, not a wider stale provider list. Failed/denied
  explicit refresh preserves stored login and never silently reimports a key.
  A corrupt saved intersection fails closed; several different saved keys make
  the explicit action refuse to guess (normal selected-account model refresh
  remains available). Logout or a changed key during discovery aborts auth completion.
- Images use a loopback companion and random local nonce. APIM keys are read from
  the current, exact saved account slot at invocation, not passed as CLI arguments or written to
  MCP descriptors, ready/runtime files or URLs. The descriptor binds the canonical
  installed plugin, configured HTTPS gateway, subscription and snapshot IDs.
- No image capability or personal OAuth is created for subscription mode. Request
  forwarding uses only the configured APIM endpoint and subscription header;
  revocation/quotas/model authorization remain authoritative at APIM.
- Image registration, price import, conflicts/manual-disable preservation, stale
  lock recovery and cleanup reuse the user engine. User-mode behavior and existing
  private paths/manifests remain backward compatible. Subscriber state is separate.
- Personal allowance features remain explicitly unsupported without a trustworthy
  subscription-scoped API. No zero/fake counters, platform user impersonation or
  invented allowance is exposed.

## Verification

Backend: authorized text/image exports, prices/aliases, image-only download,
publication bindings, empty/foreign/revoked scopes, version and complete ZIP.
Node: both existing plugin suites plus key changes/logout, bounded discovery and
refresh, model aliases and hostile URLs/headers, both maintenance workers, shared
native ownership and subscription lifecycle. Real Magpie/Bun uses isolated config,
a temporary trusted loopback HTTPS upstream for subscriptions and no paid inference.
Its actual third CLI action verifies refresh without adding an unnamed account,
and the image-only subscription case has no conversation model as a hidden prerequisite.
Source lint/types and package
checks are distinct from online/download/client acceptance, recorded below for the
separately authorized release.

Fresh release preflight: 158 related Python tests and 107 Node tests pass; the Node
run has no skips and includes the installed Magpie/Bun binaries against isolated
simulated upstreams. Full Ruff and strict Mypy (376 source files), frontend build,
OpenAPI lint and `git diff --check` pass. OpenAPI lint reports 49 warnings; the
frontend build retains its large-chunk warning. These are not paid-inference,
native Windows-device, online browser or financial-settlement acceptance results.

The earlier `/tmp` evidence was removed by the user. This run recreates checks in
the private persistent directory
`/Users/edwin/Development/turnstile/.turnstile/deployments/subscription-parity-20261018/`.
No secret, runtime credential, private download or complete settings file belongs
in the public commit. Submitting an upstream PR or merging main is not implied by a push
to the existing `origin/codex/aigateway-proxy-client` branch.

## Online release acceptance, 2026-10-18

- Completed at 23:52:39 Asia/Shanghai on <https://aigateway.edwinai.in>.
  Release source: `2bf8da81dd226b33f33c97ea1543568d817fe7f3`, exported from the clean
  committed branch. API package SHA-256:
  `0e086ec93aaec617671368858d0951442a54a630db7040942fdb56d79219d282`.
- One API-only ZIP publication. The mounted ZIP matches the candidate; after
  waiting for Azure warmup, authenticated API reads return subscription 0.2.0 and
  user 0.5.1. No blind reupload or extra API restart was used.
- The candidate changes exactly nine backend/plugin files plus frontend assets
  compared with the previous actual API package. Shared core, Linux dependency
  bytes, requirements, migrations, policies and the runtime manifest are unchanged;
  Telemetry, Control Plane and Observer were not republished.
- Six authenticated business reads pass. The actual subscription download contains
  five conversation models and three authorized image models with registry-matched
  prices. Both actual ZIPs' templates match committed source. The personal catalog
  and explicit refresh retain correct image prices; its temporary download grant
  is revoked and subsequent access returns 401. APIM keys are never revoked here.
- The actual online subscription ZIP runs through the installed Magpie/Bun in
  isolated HOME/XDG directories: native image registration, exact original IDs,
  image-price imports, MCP tool discovery, real third-method CLI refresh and
  logout cleanup pass. Refresh preserves the full account, including its slot,
  current key and display metadata. Other providers and image preferences are
  preserved; image descriptors contain no APIM key. No model generation is invoked.
- Public HTML and its two directly referenced assets match the candidate bytes.
  This is not a new live browser/mobile or native Windows-device acceptance.
  The model registry, all three app-setting dictionaries and API-level APIM policy
  match the baseline. Budgets, schema and historical consumption were not edited.
- Fresh encrypted previous package/settings/APIM/business backups and final proof
  are kept in `.turnstile/backups/subscription-parity-20261018/` in the primary
  project checkout; recovery keys are separate. Temporary plaintext downloaded-key
  copies and isolated native settings are removed only after their processes stop.

Installed users must download/update their existing plugin files, reload and then
refresh models. Publishing changes the server download, not an existing desktop
installation. Native local repricing is not a rewrite of platform historical bills.
