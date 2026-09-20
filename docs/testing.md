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

For a clean installation, run `uv run python -m backend.migrate` against an authorized new PostgreSQL 16+ database. Verify one `schema_migration` row per numbered migration (currently `001_initial_schema`, `002_apim_request_attempt_identity`, `003_budget_reservation_finalization`, `004_apim_usage_identity_guard`, `005_billable_request_lifecycle`, `006_versioned_budget_evidence`, `007_model_price_source`, `008_price_review_and_guard`, `009_org_units`, and `010_application_attribution`), then run the command again and verify that no migration is reapplied.

The initial schema contains no users, credentials, provider connections, runtimes, business models, usage events, or customer data.

For an existing installation, run the same migration command to apply only pending upgrades. Verify that the initial migration checksum and existing usage rows are unchanged. Migration `002` replaces the unique caller-request index with a non-unique `(request_id, ts DESC)` index; it does not rewrite historical usage. Apply it before running the updated telemetry consumer, and drain older telemetry consumers before enabling the new version. Do not overlap consumers that use the old and new identity rules. Index replacement takes a table lock, so schedule the upgrade for an appropriate maintenance window. Migration `007` only adds nullable columns and defaults every existing model to `price_source = 'manual'`, so rates already in the registry are left exactly as they were and the price sync skips those rows until someone opts a model in.

Request-attempt validation must prove that two APIM attempts with the same `request_id` and different `correlation_id` values both persist, while redelivery of one attempt is idempotent. Replay a pre-upgrade event whose stored primary key differs from its correlation ID: the existing primary key and Application attribution must remain unchanged, an estimated record may gain exact usage, and an already exact record must not be charged again. Detail lookup prefers an exact `correlation_id`; a legacy `request_id` selects the latest matching APIM attempt. Verify both lookups and confirm that Copilot records remain excluded. Isolated unit and migration-source tests do not substitute for this database validation.

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

## Browser validation

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

Image generation is off by default. The version-2 budget evidence cutoff is independently disabled (`budget_evidence_policy.effective_at IS NULL`). Applying migrations does not enable either feature or reassess historical requests. Enabling a future cutoff requires separate operational authorization; it cannot be cleared or moved after being set.

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
