# Testing

## Clean installation

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

The assistant upgrade retains the immutable `009_assistant_api_format` migration alongside
upstream's independent `009_context_tier_pricing`. Migration identity is the complete filename
stem, not its numeric prefix; both must be recorded with their original checksums. Run the
complete chain against an authorized empty database and again to verify no migration is reapplied.

New forward-only product upgrades are `017_budget_terminal_observation`,
`018_budget_reservation_resolution`, `019_runtime_display_name`,
`020_failed_publication_spec`, `021_usd_budget_reservations`, `022_subscription_tags`,
`023_usd_reservation_resolution`, `024_gateway_usage_attribution`,
`025_usage_cost_component_prices`, `026_invocation_trace_service_identity`,
`027_product_schema_alignment` and `028_product_index_alignment`. They preserve applied migration
bytes and existing usage, financial/audit history and routing identities. The first two add
immutable admission-bound observations and explicit Owner approval records; elapsed time or
an HTTP failure alone is not permission to refund unknown usage. Display names no longer
define Connection identity, while endpoint/target uniqueness remains. Failed publication specs
may be submitted as a new task without erasing the failed record. Tags are separate metadata.

Run the migration-source and schema-documentation tests locally. On an accurately authorized
PostgreSQL target, additionally verify parsing, unchanged old checksums/rows, immutable/FK
constraints, same-approval idempotency, concurrent claims and the ordinary API outcomes.
SQL source checks do not prove applied migrations, isolation, request attribution or refunds.
Do not initialize a shared database or import model intelligent-routing schema.

USD upgrades add exact `NUMERIC(30,14)` reservations, immutable admission prices and formal
price-bound charges. Missing/estimated prices remain unknown, not zero. Owner-approved releases
retain unknown actual Tokens/USD and their original receipt; later formal usage still settles
once. Public legacy Token snapshots keep their separate upper-bound deduction. On the approved
database, verify all seven new upgrades together, unchanged 001-016 checksums/history, exact
cache-read/write buckets, same-approval idempotency, lost responses, late formal evidence and
both Person/Application scopes before claiming financial acceptance. The later upgrades add
Gateway-bound usage metadata, immutable component-price calculation, actual correlation-based
Trace matching and the current product's required side tables and indexes. Static schema or
migration tests do not prove that these upgrades have been applied or that existing history
has been preserved in a real database.

Migration `016_model_availability` adds retained model identities, immutable commands/audit,
capability and node-enrollment evidence, and a leased projection outbox. It preserves existing
model states, prices, defaults and financial history. Local checks are:

```bash
uv run --frozen --no-sync pytest -q tests/backend/model_platform/test_model_availability.py \
	tests/platform/contracts/test_migrations.py tests/platform/api/test_model_management_api.py \
	tests/platform/deployment/test_control_plane_function.py
node --experimental-strip-types --test tests/platform/frontend/model-edit-form.test.mjs \
	tests/platform/frontend/model-publication-connections.test.mjs
```

The existing C# policy suite also compiles and runs native availability expressions. On an
explicitly authorized PostgreSQL database, separately verify migration parsing, same-transaction
command enforcement, stale/duplicate commands, concurrent removal, immutable intent/audit,
retained tombstones, lost/expired worker leases, and bounded same-operation retry. Test exact
Table ETag conflicts, projection readback and complete Gateway evidence independently. Do not
run an initializing local database fixture against a shared database. Static SQL and recording
adapters do not prove PostgreSQL transaction isolation.

For UI acceptance, compare actual source and public components with the same data at desktop and
390 px. Cover enabled, disabled, pending, projected, partial, failed and unknown; open details,
long target names, keyboard activation, disabled controls, duplicate clicks and an uncertain
response that reuses its idempotency key. Editing metadata must never submit `enabled`.
Verify all locales and light/dark themes. Local component transport adapters are not authenticated
Gateway E2E; that requires the separate installation and live validation workflow.

Migration `015_platform_invocation_trace` adds nonbilling platform call snapshots and a
Request Trace view. Apply it before deploying the updated API. On an authorized PostgreSQL
database, verify duplicate-start rejection, conditional immutable completion, visibility before
telemetry, and a unique formal match on the actual Gateway correlation ID, caller request ID,
user, model, Runtime and optional Application identity. Missing or ambiguous correlation evidence
must remain unmatched. Confirm that platform timeouts cannot overwrite formal usage, prices or
Gateway status, and that attempts never enter budget totals. Static SQL and in-memory tests do
not prove migration parsing, PostgreSQL concurrency or real Gateway attribution.

Migration `014_self_hosted_gateway_provisioning` adds a durable creation operation, target-scoped
idempotency index and nullable deployment-to-operation ownership link. It does not create Azure
resources, enable the feature, store Gateway tokens or modify usage history. Validate concurrent
duplicate submissions, cross-target foreign-key rejection, lease loss and same-ID failed-task
retry against an explicitly authorized PostgreSQL database. Do not run an initializing local
database fixture against an existing shared environment.

Migration `013_multi_target_rollout` adds parent intent and target-bound child publication tables
only. It leaves models, existing releases and financial history unchanged. Validate all-or-none
queue creation, stable lock order, cross-target child rejection and derived partial status in an
authorized PostgreSQL database; local repository doubles do not prove transaction isolation.

Migration `012_apim_onboarding` extends the durable release-operation kind constraint only.
It creates no target or credentials and does not activate a feature. Real initialization acceptance
must verify same-plan idempotency, failed-task history, linked retry and lease-bound activation.

Migration `011_gateway_targets` adds registered APIM identities, deployment inventory and
logical-model route bindings. It does not register live targets or rewrite legacy models or usage.
Validate cross-target foreign keys, the unique enabled default route, preserved retired history,
and transactional inventory replacement against a separately authorized PostgreSQL environment.
Local domain and SQL-source tests are not evidence that a migration has been applied.

For a clean installation, run `uv run python -m backend.migrate` against an authorized new PostgreSQL 16+ database. Verify one `schema_migration` row per numbered migration (currently `001_initial_schema`, `002_apim_request_attempt_identity`, `003_budget_reservation_finalization`, `004_apim_usage_identity_guard`, `005_billable_request_lifecycle`, `006_versioned_budget_evidence`, `007_model_price_source`, `008_price_review_and_guard`, `009_context_tier_pricing`, `010_budget_execution_protocol`, `011_gateway_targets`, `012_apim_onboarding`, `013_multi_target_rollout`, `014_self_hosted_gateway_provisioning`, `015_platform_invocation_trace`, and `016_model_availability`), then run the command again and verify that no migration is reapplied.

The initial schema contains no users, credentials, provider connections, runtimes, business models, usage events, or customer data.

For an existing installation, run the same migration command to apply only pending upgrades. Verify that the initial migration checksum and existing usage rows are unchanged. Migration `002` replaces the unique caller-request index with a non-unique `(request_id, ts DESC)` index; it does not rewrite historical usage. Apply it before running the updated telemetry consumer, and drain older telemetry consumers before enabling the new version. Do not overlap consumers that use the old and new identity rules. Index replacement takes a table lock, so schedule the upgrade for an appropriate maintenance window. Migration `007` only adds nullable columns and defaults every existing model to `price_source = 'manual'`, so rates already in the registry are left exactly as they were and the price sync skips those rows until someone opts a model in.

Request-attempt validation must prove that two APIM attempts with the same `request_id` and different `correlation_id` values both persist, while redelivery of one attempt is idempotent. Replay a pre-upgrade event whose stored primary key differs from its correlation ID: the existing primary key and Application attribution must remain unchanged, an estimated record may gain exact usage, and an already exact record must not be charged again. Detail lookup prefers an exact `correlation_id`; a legacy `request_id` selects the latest matching APIM attempt. Verify both lookups and confirm that Copilot records remain excluded. Isolated unit and migration-source tests do not substitute for this database validation.

### Context-tier pricing validation

Apply `009_context_tier_pricing` only to the separately approved database before updating API, Control Plane and Telemetry packages together. The migration adds accepted/pending schedules to the price side table and request-level tier/schedule snapshots; it does not rewrite historical requests. Registry responses advertise `context_pricing_supported` only in the compatible application version. Do not enable tier writes while an old consumer still reads scalar rates.

Run `uv run --frozen --no-sync pytest -q tests/backend/persistence/test_price_sync_concurrency.py tests/backend/telemetry/test_processor.py tests/platform/pricing` and the two frontend model form Node suites. The PostgreSQL fixture starts an isolated session-owned local cluster; verify it runs rather than skips. Cover save/reopen, source/region/range mismatch, inherited and model discounts, sync review and concurrent edits, publication activation, exact threshold boundaries including cached input, missing usage, and reconciliation using the original schedule after registry prices change. Local source fixtures and local SQL results are not cloud deployment evidence.

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

### Application and budget workflows

Local Application checks must cover exact APIM selection for discovery and key operations,
creation progress recovery without duplicate submission, and successful navigation to the
created Application. Initial keys must remain outside the DOM, URL and query cache.

Budget projection tests must cover ETag conflict retries, unchanged-value idempotence, invalid
TPM, missing admission rows, readback mismatch and preservation of all unrelated quota fields.
Database-save success with failed Table projection must remain distinguishable from a verified
projection. The detail UI must retain unknown execution balances rather than falling back to
historical usage or rendering a measured-zero progress indicator.

Run `tests/backend/model_platform/test_budget_diagnostics.py` for Owner and origin protection,
strict request inputs, valid and wrong-audience delegation, exact fence error codes, isolated
candidate requirements, both scope epochs and identities, a fence change before dispatch,
one-request dispatch, APIM-only secret headers and immutable platform Trace. Settlement cases
must include both exact receipts, one missing or mismatched receipt, unmeasured usage and timeout
without replay. These tests use isolated HTTP and ledger adapters, not live budget validation.

Compare the actual source and public components with identical synthetic data for release
grouping, full release details, the two-step budget dialog, Application selection and details,
budget editing, connection creation/editing and publication controls. Cover desktop and 390 px,
Chinese and English, light and dark themes, expanded menus and invalid/disabled states. Verify
fonts and images actually load and that mobile dialogs remain within the viewport. Save source
and candidate fingerprints with results; a successful build or screenshot count alone does not
establish feature equivalence.

### Connection diagnostics and invocation Trace

Local diagnostics tests must cover Owner-only plan and execution, explicit model/Gateway
selection, exact system-probe subscription permissions, configuration changes between plan and
dispatch, cooldown ownership, deadlines and safe historical outcomes. Invocation tests must cover
exact target selection, unavailable selections without fallback, text output limits, image routing
metadata exclusion and recording failure before dispatch. Test unknown usage separately from
measured zero, and platform failures separately from formal Gateway status.

Use the actual component handlers for selection and failure-state tests. Isolated component
screenshots at desktop and 390 px verify layout only; they do not establish authenticated or
deployed acceptance. A later authorized run must use real permissions, Gateway responses,
persisted Trace identities and formal telemetry. Never replace that evidence with mock success
or reuse a previous candidate's screenshots. Unknown outcomes must not be automatically replayed.

### New Self-hosted Gateway creation

Local checks exercise the actual API/client/worker handlers with isolated repository and HTTP
adapters. Cover an initially absent native Gateway, stable generated ID, `HEAD` API membership
readback, same-ID recovery after both possible lost PUT responses, foreign ownership rejection,
missing permissions, disabled capabilities, Owner and origin guards, endpoint consent, no-store
startup responses and secret-free task history. Frontend checks execute real component handlers
for default creation versus legacy association, uncertainty-safe resubmission, pending status,
renewal selection and memory/Blob cleanup. Compile the optional access template independently.

For live acceptance, first authorize the exact APIM child resource, migration, identity permissions
and application packages. Prove the generated native ID is absent before submitting the normal
Turnstile form. Verify creation and API association through ARM and persisted task readback; then
download the startup file without recording its token, run the official image on the separately
authorized server, and validate connectivity from Turnstile. Complete model publication, normal
page invocation, exact request/correlation Trace and accounting checks. Repeat the UI inspection
at desktop and 390 px, retaining copyable non-secret input values above screenshots. Keep all
existing gateways, containers and history; cleanup needs an exact session-owned allowlist.

Do not count an already configured Gateway attachment, offline component screenshots, a generated
Docker command, or task status alone as successful new-Gateway cloud acceptance.

### APIM initialization

Self-hosted lifecycle tests must cover strict endpoint/hosting requests, disabled capability,
name-only acknowledgement, trusted-host checks before credential use, failed probe evidence,
retirement blockers, re-addition identity and failed reassociation without an orphan destination.
Validate publication/retirement locking against a separately authorized PostgreSQL database;
in-memory tests prove state rules, not database concurrency. Actual Gateway container movement
or deletion is outside the metadata lifecycle and is not implied by these tests.

Protocol membership tests additionally cover exact response ETags, conditional conflict limits,
preserved concurrent additions, retired-only revocation, current revision/epoch and canonical
policy checks, lease loss immediately before a write, and lost responses both before and after
server commit. Unknown outcomes must not cause an unconditional replay or overwrite unrelated
F fields. The Table transport and repository doubles do not establish real Azure CAS or lease
timing; verify those on separately authorized targets.

Run the onboarding model-platform tests, target-inventory API tests, Control-plane Function tests
and OpenAPI checks. Cover exact preview binding, ownership/readback drift, permissions, suspended
subscriptions, concrete ETag activation, authentication denial, linked retries, lease expiry and
failure without target activation. Verify repeated Ledger projection preserves confirmed usage and
outstanding reservations and refuses a different F document. HTTP/SQL doubles are local evidence,
not real PostgreSQL locking, role propagation, policy compilation or Gateway acceptance.

Before deployment acceptance, authorize the exact existing APIM, Event Hub and Table resources
and necessary role assignments. Verify the shared governance Named Value, target identity and
current budget period; retain before/after resource and policy evidence. Confirm the seven owned
protocol routes, two system-key catalog reads and anonymous denial, then exercise a normal model
publication on that initialized target. Check the Owner dialog, preview blockers, durable progress,
failure/retry and desktop/390 px layout against the real authenticated backend. Do not substitute a
component fixture or create a new environment under a generic testing request.

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

Image generation is available without an enablement setting. Verify that a default installation advertises image support, allows selecting the image operation and accepts a valid publication without extra feature configuration. The version-2 budget evidence cutoff remains independently disabled (`budget_evidence_policy.effective_at IS NULL`). Applying migrations does not enable that cutoff or reassess historical requests. Enabling a future cutoff requires separate operational authorization; it cannot be cleared or moved after being set.

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
