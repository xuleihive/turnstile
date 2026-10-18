# Testing

`018_permission_management` adds the persisted four-role menu matrix and audit.
Permission regression tests use real PostgreSQL and authenticated sessions: Owner-only policy
configuration, CAS save conflicts, existing-session revocation, ordinary-user self scope,
department membership unions, exact team membership unions, and denial of global snapshots.
Menu configuration does not alter budget storage, gateway admission, or Owner-only writes.

Usage directory filter changes require real PostgreSQL and API coverage for repeated IDs,
same-field union/cross-field intersection, authenticated scope isolation, active/archived/
historical/unattributed classification, current-name precedence and all historical parent edges.
Check totals agree across overview, distribution and hour/day/week trends for the same filters.
Omitted status preserves the older all-status query, whereas UI reset returns to active.
UI archive options are only All, Enabled and Archived. All must send active plus archived,
not omit the status constraint; stale historical/unattributed UI selections return to active.
Archived child scopes retain active ancestors in the facet list. Never prove these read changes
by deleting usage rows or saving budgets. Browser checks cover five pages, theme/mobile layouts,
multi-select persistence and clearing, cascading options, no console/API errors and zero writes.

## Clean installation

Pricing upgrade `010_models_dev_pricing` adds the public catalog source and matching metadata
without rewriting accepted prices. Verify explicit manual configurations remain manual, absent
configurations follow the public default, and the numbered migration is applied once.

```bash
uv sync --frozen
npm --prefix frontend ci
```

## Required checks

```bash
uv run ruff check backend turnstile_core scripts tests functions/telemetry/function_app.py functions/control_plane/function_app.py
uv run mypy backend turnstile_core scripts tests
uv run pytest -q
npm --prefix frontend run build
az bicep build --file infra/main.bicep
xmllint --noout infra/policies/foundry-finops-policy.xml
npx --yes @redocly/cli@2.38.0 lint contracts/openapi.yaml
git diff --check
```

## Database validation

The merged fork preserves migration identities from both histories. The runner uses the full
filename minus `.up.sql`, not just the numeric prefix: `009_assistant_api_format` and
`009_context_tier_pricing` are separate migrations. Never rename or edit either applied file.
Validate both a fresh database and an upgrade from the fork's existing 18-file ledger.
Keep existing usage, financial/audit history, directory memberships and menu assignments.
SQL source checks do not prove applied migrations. Do not initialize a shared database.

Upstream additions retained in this fork are `009_context_tier_pricing`,
`010_budget_execution_protocol`, `011_gateway_targets`, `012_apim_onboarding`,
`013_multi_target_rollout`, `014_self_hosted_gateway_provisioning`,
`015_platform_invocation_trace`, `016_model_availability`,
`017_budget_terminal_observation`, `018_budget_reservation_resolution`,
`019_runtime_display_name`, `020_failed_publication_spec`, `021_usd_budget_reservations`,
`022_subscription_tags`, `023_usd_reservation_resolution`, `024_gateway_usage_attribution`,
`025_usage_cost_component_prices`, `026_invocation_trace_service_identity`,
`027_product_schema_alignment` and `028_product_index_alignment`.
Budget execution and settlement remain independently gated. Unknown usage is not refundable
merely because time has elapsed or a request failed. Verify atomic reservations, immutable
prices, settlement idempotency and late formal evidence on an isolated PostgreSQL target.
Model availability tests and recording adapters do not prove PostgreSQL transaction isolation
or real APIM propagation. These require separately authorized validation, not a Git merge.

For a clean installation, run `uv run python -m backend.migrate` against an authorized new
PostgreSQL 16+ database. Verify exactly one `schema_migration` row for every full version in
the inventory below, with its matching source checksum. Run the command again and verify that
no migration is reapplied and all checksums and applied timestamps remain unchanged.

### Migration inventory

The current chain contains **38 migrations**, listed in the runner's lexicographic filename
order. Shared files belong to both histories; Fork and Upstream identify additions on each
side of the merge. These are source identities, not a choice of deployment subsets. Existing
installations apply all pending files from the compatible candidate through `backend.migrate`
while preserving every already-applied version and checksum.

| Migration file | Origin | Effect |
| --- | --- | --- |
| `001_initial_schema.up.sql` | Shared | Empty platform schema, including consolidated historical schema definitions; no customer data. |
| `002_apim_request_attempt_identity.up.sql` | Shared | Non-unique caller request index; distinct APIM attempt identities. |
| `003_budget_reservation_finalization.up.sql` | Shared | Immutable reservation finalization/recovery and Application ledger snapshots. |
| `004_apim_usage_identity_guard.up.sql` | Shared | Guarded APIM usage identity and immutable discrepancy evidence. |
| `005_billable_request_lifecycle.up.sql` | Shared | Durable billable-request journal and effective budget-usage views. |
| `006_versioned_budget_evidence.up.sql` | Shared | Versioned evidence selection and separately scheduled, initially unset cutoff. |
| `007_model_price_source.up.sql` | Shared | Pricing side tables, source metadata and connection discounts; no existing-model backfill. |
| `008_price_review_and_guard.up.sql` | Shared | Separate pending-price review baseline and superseded sync status. |
| `009_assistant_api_format.up.sql` | Fork | Assistant API format selection, defaulting to automatic. |
| `009_context_tier_pricing.up.sql` | Upstream | Context-tier catalog prices and per-request tier snapshots. |
| `010_budget_execution_protocol.up.sql` | Upstream | Default-off budget rollout/scopes, immutable execution evidence and settlement outbox. |
| `010_models_dev_pricing.up.sql` | Fork | Public catalog source, matching/sync metadata and absent-row reader semantics; explicit choices preserved. |
| `011_gateway_targets.up.sql` | Upstream | Registered APIM targets, Gateway deployments and model target bindings. |
| `011_user_settings_profile.up.sql` | Fork | Private normalized avatars and account-security audit/throttling records. |
| `012_apim_onboarding.up.sql` | Upstream | APIM onboarding release-operation kind. |
| `012_organization_directory.up.sql` | Fork | Empty directory/synchronization tables, versioned authority state and stored query scopes. |
| `013_menu_permission_groups.up.sql` | Fork | Local navigation group with Ordinary User as the default. |
| `013_multi_target_rollout.up.sql` | Upstream | Multi-target publication parent/child tasks and target validation. |
| `014_multiple_menu_permission_groups.up.sql` | Fork | Authoritative menu-group array preserving earlier single-group assignments. |
| `014_self_hosted_gateway_provisioning.up.sql` | Upstream | Self-hosted Gateway provisioning operation and deployment metadata. |
| `015_platform_invocation_trace.up.sql` | Upstream | Persisted platform attempts, telemetry matching and unified trace view. |
| `015_scoped_menu_administrators.up.sql` | Fork | Scoped administrator appointments, valid legacy department imports and effective menu view. |
| `016_model_availability.up.sql` | Upstream | Versioned model availability, tasks, coverage/enrollment evidence and immutable audit. |
| `016_organization_members.up.sql` | Fork | Explicit organization ownership backfill and retirement of pending transfers. |
| `017_budget_terminal_observation.up.sql` | Upstream | Immutable budget execution terminal observations. |
| `017_department_memberships.up.sql` | Fork | Additional non-billing department memberships and hierarchy validation. |
| `018_budget_reservation_resolution.up.sql` | Upstream | Immutable execution-reservation resolution documents. |
| `018_permission_management.up.sql` | Fork | Persisted menu permission matrix/audit and effective appointment projection. |
| `019_runtime_display_name.up.sql` | Upstream | Drops the global `model_runtime_name_key` uniqueness constraint; adds no metadata columns. |
| `020_failed_publication_spec.up.sql` | Upstream | Rebuilds the reusable-spec unique index, excluding failed, superseded and rolled-back publications. |
| `021_usd_budget_reservations.up.sql` | Upstream | USD budget limits, reservations, immutable admission prices and cost-aware settlement. |
| `022_subscription_tags.up.sql` | Upstream | Application subscription-tag storage. |
| `023_usd_reservation_resolution.up.sql` | Upstream | USD settlement/release evidence validation against immutable admission and approval. |
| `024_gateway_usage_attribution.up.sql` | Upstream | Per-request APIM target, Gateway deployment/instance and revision attribution. |
| `025_usage_cost_component_prices.up.sql` | Upstream | Replaces `budget_usage_usd_cost` to prefer stored exact USD cost or require prices only for nonzero usage components; estimated/unpriced usage stays unknown. Adds no price columns. |
| `026_invocation_trace_service_identity.up.sql` | Upstream | Matches platform invocation telemetry using service identity. |
| `027_product_schema_alignment.up.sql` | Upstream | Application recovery/finalization and Gateway provisioning tables; aligned finalization and invocation views. |
| `028_product_index_alignment.up.sql` | Upstream | Scope/period finalization index and unique self-hosted provisioning request index. |

### Upgrade validation

Migration `012_organization_directory` adds empty organizational-directory and synchronization
tables, versioned control state, audit guards and stored assistant/report scope metadata.
It does not import customers, change login roles, rewrite budgets or activate directory mode.
Run the isolated directory migration, service, synchronization and transfer suites before
approving the separate business backfill and authority switch.

Migration `013_menu_permission_groups` adds a local navigation group with Ordinary User
as the default, without translating job titles or existing department grants.
Apply it before the new authentication runtime. Verify fresh-install/rerun behavior,
historical account/session/budget/usage/title/grant preservation, Owner-only assignment,
source-import preservation, and unchanged API data scopes/model authorization.
Menu-only saves must not add APIM projection work. Exercise all four groups in a real
browser, including direct URL, search, source switch, existing-session downgrade and
desktop/mobile rendering. See the menu matrix in the organization-management plan.

Migration `014_multiple_menu_permission_groups` adds the authoritative group array,
preserving any single-group assignments from 013. Check combinations of administrator
groups, menu union, single-label removal and fallback to Ordinary User after the last
administrator assignment is removed. Personnel forms display read-only tags, with no group
checkboxes or dropdown. Verify profile saves omit both menu-group fields and preserve existing
assignments, and new-person forms show Ordinary User without granting administrator groups.

Migration `015_scoped_menu_administrators` unifies administrator appointments and menu identity.
It imports valid legacy department administrators only, without inventing team/organization
appointments or altering APIM, model access or historical billing. Verify old migration checksums,
deduplication, rerun safety, real team-member validation, organization configuration, revocation,
Owner/Member protection and consistent personnel/profile labels. Department appointments keep the
existing department-scope contract through the legacy grant projection; there is no second UI.
Scope matching regression must cover one person in two teams (including identical names),
administrator in only one, unrelated departments/organizations, exact kind/ID matching,
revocation and disabled memberships. Profile menu unions remain compatible but must not imply
resource authority. Check selected-node personnel/editor tags, exact profile scope fields,
per-department capability isolation and unchanged Owner/model/budget controls.

Migration `016_organization_members` backfills explicit organization ownership from current
primary departments, retains all historical memberships and retires pending transfer plans.
Migration `017_department_memberships` adds non-billing department membership. Verify the
unchanged 001-016 ledger, rerun safety, same-organization constraints, one primary department
and preservation of budget/usage/model facts. New personnel creation is Owner-only and requires
name, email, login password and a primary department in the selected organization. Personnel,
Member login account, account link and primary membership must commit together; conflicts must
not overwrite an existing password or leave an orphan account. Passwords use the shared scrypt
implementation and must never appear in responses, audit or idempotency evidence. Check required
department validation, duplicate/cross-organization member selection, atomic batch rejection,
linked password login, retired transfer/identity endpoints and desktop/mobile workflows.
Department/team Add Members selects existing organization people only, without changing their
primary budget department. Transfer scheduling and the historical-identity editor are retired;
historical query catalogs, billing records and immutable audit data remain available.

The initial schema contains no users, credentials, provider connections, runtimes, business models, usage events, or customer data.

For an existing installation, run the same migration command to apply all pending upgrades.
Verify that every previously applied checksum and existing usage row is unchanged. Migration
`002_apim_request_attempt_identity` replaces the unique caller-request index with a non-unique
`(request_id, ts DESC)` index; it does not rewrite historical usage. Apply it before running
the updated telemetry consumer, and drain older telemetry consumers before enabling the new
version. Do not overlap consumers that use the old and new identity rules. Index replacement
takes a table lock, so schedule the upgrade for an appropriate maintenance window.

Migration `007_model_price_source` creates the side tables `model_runtime_price` and
`managed_model_price`, not nullable columns on the registry tables. The `price_source` SQL
column default remains `manual`. Explicit manual rows remain manual and are skipped by sync.
With `010_models_dev_pricing` and compatible readers, an absent row follows the default public
catalog; neither migration backfills existing models or rewrites charged rates. A later accepted
price synchronization is a separate write. Do not infer an explicit manual choice from the
absence of a price-source row or from the SQL default.

Request-attempt validation must prove that two APIM attempts with the same `request_id` and different `correlation_id` values both persist, while redelivery of one attempt is idempotent. Replay a pre-upgrade event whose stored primary key differs from its correlation ID: the existing primary key and Application attribution must remain unchanged, an estimated record may gain exact usage, and an already exact record must not be charged again. Detail lookup prefers an exact `correlation_id`; a legacy `request_id` selects the latest matching APIM attempt. Verify both lookups and confirm that Copilot records remain excluded. Isolated unit and migration-source tests do not substitute for this database validation.

### Personal user settings

Apply `011_user_settings_profile` before deploying the updated authentication API, following
the [upgrade procedure](deployment.md#user-settings-database-upgrade). Personal avatars are
private PostgreSQL records; Microsoft sessions continue to use Graph photos.
Only password sessions can update the current account's name, avatar, or password. Successful
password changes revoke all Turnstile sessions without changing Microsoft credentials.

The reproducible migration suite starts and stops only its own local PostgreSQL 16+
cluster, using a private Unix socket and rejecting TCP connections. It does not read
`DATABASE_URL` or `TEST_USER_SETTINGS_DATABASE_URL` as a target. With `initdb` and
`postgres` available on `PATH`, run:

```bash
uv run pytest -q tests/backend/persistence/test_user_settings_migration.py
```

It covers fresh installation through `011`, upgrade with historical account/session/budget/
usage preservation, unchanged migration checksums and timestamps on rerun, rejection of
applied-file changes, and schema/ledger rollback on failure. Missing or older PostgreSQL
tools explicitly skip these tests; a skipped run is not migration evidence.
The fresh-Owner case exercises migration followed by real bootstrap, session issuance
with the new avatar schema, display-name/password updates and a restart-style rerun
that must preserve the new name/password and the existing migration ledger.

Run the self-service API and UI source checks with the ordinary suite. To exercise real
transaction locking, rollback, avatar persistence, audit and password attempt limiting,
explicitly set `TEST_USER_SETTINGS_DATABASE_URL` to an isolated, migrated PostgreSQL database
and run `tests/backend/persistence/test_user_settings_store.py`. Never point this test
variable at a shared or deployed database. Tests create temporary accounts and delete only
those accounts during teardown.

### Ledger upgrade validation

For v1.1 model-access projection, compile both `infra/main.bicep` (new ledger) and
`infra/model-access-upgrade.bicep` (existing ledger). Run the focused local checks:

```bash
uv run --frozen pytest -q tests/backend/governance/test_budget_service.py \
	tests/backend/telemetry/test_ledger.py tests/platform/infrastructure/test_infrastructure.py
```

These tests cover post-save full UUID/alias projection, replacement, explicit deny-all,
budget/other-user preservation, failed transactions and timer repair. The infrastructure
checks bind all three API settings and the API identity's exact Table role. They use unit
fixtures; do not count them as deployed evidence. Validate an upgraded API/ledger and a newly
created API/ledger independently, including settings readback, Table-scoped RBAC, immediate
grant/replacement/revocation and a direct APIM client. Preserve both runs' pre-change snapshots
and evidence; a platform Invocation Test alone reads PostgreSQL and cannot prove Desktop
permissions have propagated. See the two [v1.1 deployment paths](deployment.md#choose-the-v11-deployment-path).

Apply `003_budget_reservation_finalization` before deploying the updated API and Telemetry packages. Verify that the initial two migration checksums and historical request rows are unchanged. The new evidence is append-only and the Application ledger snapshot is updated atomically; older timer snapshots must not replace newer ones.

In a separately authorized PostgreSQL/Table/Log Analytics environment, verify exact recovery, terminal-zero failures, silent reservations past the grace period, unavailable and partial log results, both Person and Application scopes, retired applications, and old-month partitions. Upper-bound finalization must leave the original `R.Reserved` amount unchanged. Project confirmed usage before deleting settled reservations, including usage that crossed a month boundary. A late unmeasured event must not erase recovered exact usage; later final usage must replace it without double counting. Re-run after interrupted Table marking and deletion to prove convergence.

For API and browser checks, distinguish a missing snapshot from zero pending usage. Verify that available Tokens subtract both pending reservations and finalized upper bounds. Recovery records have no model pricing or cache classification; they must not invent prices, latency or per-user cache measurements. Unit tests and SQL source checks do not establish live data-plane or migration correctness.

### Budget execution validation

Migration `010_budget_execution_protocol` adds default-off execution scopes, immutable admission
and evidence records, and a durable settlement outbox. It does not activate a rollout or rewrite
historical usage. Apply it only to the separately authorized database after preserving the existing
migration checksums and data readback. Runtime activation requires a verified legacy-admission fence,
drained in-flight work, equal opening balances, compatible readers, and an explicit epoch.

```bash
uv run --frozen --no-sync pytest -q tests/backend/telemetry/test_atomic_ledger.py \
	tests/platform/infrastructure/test_budget_execution_policy.py
```

The policy suite requires an installed
.NET 8 SDK and executes the generated expressions as C# 7.3 with the SDK's Newtonsoft.Json assembly;
it does not substitute a Python parser or mock results for C# execution. Cover tool schemas and
arguments, text containing JSON, protocol-specific mixed/remote content, incomplete structures,
strict versus estimate contracts, native-count guarantees, error ownership, CAS conflicts and
completion idempotence. Local HTTP doubles and SQL structure checks are not real database acceptance.

For a separately approved deployed run, preserve parent-policy ETags and component hashes, prove
all affected parents match the candidate, and verify exact request-to-receipt-to-settlement identity.
Use the actual subscription header and model setting in a supported normal Claude Code session;
do not treat bare-mode diagnostics or private-environment results as public acceptance. Unknown
request outcomes cannot authorize a replay. Record unresolved transport errors without attributing
their cause to the platform without evidence.

## Browser validation

### Assistant API protocols

Apply `009_assistant_api_format` before deploying the updated API. Existing model and title
settings remain unchanged and the new nullable protocol defaults to automatic selection.
The initial migration and historical conversations must not be rewritten.

Local gateway and assistant tests use mocked provider responses. They cover model-specific
protocol selection, Claude tool definitions and parallel tool results, private reasoning
continuity, optional protocol persistence, incompatible overrides, fallback visibility,
usage, correlation IDs and API error propagation. Frontend tests cover protocol resets on
model changes and preservation on title-toggle changes. None establishes live APIM routing.

In an authorized deployed environment, verify the active APIM revision includes the selected
model alias on its Messages or Responses operation, its backend path and provider credential.
Test a real two-round analysis question, attribution and measured usage, then reopen its
history. Ordinary text publication probes are not evidence of successful tool calling.

### Governed image generation and evidence v2

The fixed APIM image operation has two installation paths. Fresh infrastructure must create
`POST /images/generations` with a default deny policy. An older installation must use the
versioned APIM upgrade without replaying bootstrap or granting operation-write permission
to the Control-plane publisher. Missing infrastructure produces a safe, actionable publication
error rather than a runtime operation PUT.

Run the focused tests, then compile both `infra/main.bicep` and `infra/apim-upgrade.bicep`:

```bash
uv run pytest -q tests/platform/deployment/test_apim_upgrade.py \
	tests/platform/deployment/test_deploy_script.py \
	tests/platform/infrastructure/test_infrastructure.py
```

The upgrade tests cover
pre-image parent conversion, already upgraded no-op behavior, customer text-policy retention,
missing/partial image infrastructure, conflicting routes, exact what-if scope, maintenance
requirements, private plans, concurrent local execution, interruption recovery, lost promotion
responses, and rollback. The command integration test uses mocked ARM responses and does not
establish Azure provisioning or deployed data-plane behavior.

Before releasing the upgrade, use separately authorized real targets for both paths. In the
upgrade target, retain pre-upgrade snapshots and test existing text traffic, operation/policy
identity, subscriptions and historical usage before and after upgrading. Interrupt preparation
and promotion, resume without duplication, rerun after success, and verify explicit rollback
before introducing later model publications. Never make the current revision writable through
a fabricated success response or relax the shared Publisher role to pass these checks.

This fork preserves `IMAGE_GENERATION_ENABLED=false` by default. Test the disabled capability
and rejection paths separately, then use an explicitly enabled local fixture for image protocols.
The version-2 budget evidence cutoff remains independently disabled
(`budget_evidence_policy.effective_at IS NULL`). Applying migrations does not enable that cutoff
or reassess historical requests. Enabling a future cutoff requires separate operational
authorization; it cannot be cleared or moved after being set.

Focused local checks include `tests/backend/model_platform/test_image_*.py`, `tests/backend/telemetry/test_billable_requests.py`, `tests/backend/telemetry/test_ledger.py`, and `tests/platform/frontend/test_image_generation.py`. These use unit fixtures and mocked transports, not live images or database evidence.

In a separately authorized environment, test one PNG, JPEG and WebP image through the authenticated backend; assignment and both budget scopes; missing usage, provider failures and timeouts; exact measured zero; and invalid or oversized image responses. Verify pre-dispatch attempt creation, immutable acknowledgement, and no automatic retry after an uncertain outcome. A replacement paid probe needs explicit `authorize_image_probes` authorization. Check release lease expiry, parent-policy tampering and APIM readback before activation and rollback.

For evidence v2, verify formal usage outranks an application acknowledgement, which outranks complete diagnostic recovery. Weak error or estimated rows cannot erase exact usage; equal-rank conflicts retain first received evidence and remain inspectable. Check immutable UTC admission month, platform-request and APIM-correlation aliases, Person and Application independence, retired applications, repeated timers and old-month reservations. Missing after-dispatch usage must remain reserved; an HTTP error alone is not measured zero. Recovery and acknowledgements must not invent raw request counts, prices, cache buckets or latency. Pre-cutover requests must retain their old accounting rules.

SQL source checks cannot prove PostgreSQL parsing, trigger behavior, locking, concurrent ingestion or migration safety. A frontend build cannot prove authenticated browser behavior. Record those checks as unverified until they run against separately authorized real targets.

### Authenticated UI

Use an authenticated real backend. Check desktop and 390 px mobile viewports, loading and empty states, keyboard focus, hover stability, modal layout, and horizontal overflow. Browser interception or fabricated responses do not count as end-to-end evidence.

For images, verify model-operation selection, required text-input/cached-text/image-output prices, preview dimensions, format-aware download and failed-image actions. Changing model or mode must clear the result; image data and prompt must not enter browser storage, query caches or logs. Leaving the result must revoke its Blob URL.

## Test boundaries

Unit tests may use in-memory repositories. Integration and E2E results must identify the real deployed environment and resource boundary used for validation.

## Feature acceptance matrix

Multi-target local coverage must include bounded distinct targets, stable parent identity,
all-or-none child queueing, truthful partial status, canonical manual and tiered catalog prices,
all price-identity drift fields, and route removal preserving shared access until the final target.
Validate the same logic with concurrent PostgreSQL transactions and independent real APIM
publications before operational acceptance. No local double proves distributed promotion,
pricing under production load or per-target upstream invoice equivalence.

Passing a helper test or finding a source string does not establish complete feature delivery.
The image Node suites execute the production registry normalization, publication component and
invocation component with isolated hook/transport adapters. They verify actual handlers and request
construction, but do not validate browser layout, authentication, Azure or persisted billing.

| Feature | Required local coverage | Required deployed evidence |
| --- | --- | --- |
| Registry image capability | API response through client normalization into both Add model and Invoke, missing/false/unsupported versions | Enabled controls and successful authenticated registry load |
| Model publication dialog | Zero/single/multiple gateways, unavailable connections, switch resets, pricing completeness including zero, one-time credentials | Same-data desktop/390px layout, keyboard controls, accepted UI publication and Active readback |
| OpenAI-compatible onboarding | Direct and existing-connection vendor contracts, mismatch rejection, provider reuse, unique persisted credential references | Authorized provider publication and isolated APIM resource references |
| Image retry | Default no consent, publication-bound explicit consent, cleared consent, unchanged authorization on ordinary retry, 32-attempt cap | Authorized recovery without replaying uncertain paid requests; persisted audit |
| Text and image invocation | Identity/assignment/budget denials, one image/nonstreaming, typed result and memory clearing | Before-assignment denial, preserve-budget assignment, real response, request/correlation IDs, preview and download |
| Ledger/evidence v2 | Same caller/distinct attempts, first receipt, immutable admission month, ranked evidence, unknown outcomes, scope separation | Migration checksums, exact API/telemetry/DB accounting, pending reservations and repeated convergence |
| Model editor, Pool, Application, columns | Identity preservation, cancellation, affinity payloads, staged creation safety, hidden width preservation | Actual supported workflows, failure states, responsive layout and retained history |

Maintain a per-candidate result for every row: passed, failed, blocked or not exercised, with exact
evidence references. API mutation acceptance is not a completed UI check. Store completion only after
readback and visible assertions. A failed mandatory image journey must keep the overall gate failed,
even when independent text checks continue. Never reuse a different candidate's screenshots or results.
