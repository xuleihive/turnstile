# Local Upstream Main Merge

The Scope through Boundaries sections record the 2026-10-16 merge snapshot, not current live state.
Current branch installation and regression guidance follows under the 2026-10-18 supplement.

## Scope

- Checkout: `/Users/edwin/Development/turnstile`, branch `main`.
- Fork parent: `2a58e152197fc377c48f5328df4c7169264607ea`.
- Upstream parent: `e59177d` (`xuleihive/turnstile`, `main`).
- Integrated 50 upstream-only commits while retaining all 48 fork-only commits.
- Backup: `codex/backup-main-before-upstream-20261016`.
- Local merge only. No push, deployment, production migration, paid inference, or Azure write.

## Compatibility Decisions

- Retained database-backed organization/person directory, scoped menu permissions, account
  profiles, and the fork's navigation labels and multi-value usage filters.
- Retained `models_dev` default pricing, explicit manual pricing, bulk synchronization,
  accepted-price guards, and Claude/Responses tool and streaming compatibility.
- Integrated context-tier pricing, USD budget support, APIM targets, diagnostics, availability
  commands, and invocation traces. Provider request IDs remain separate from APIM financial
  correlation IDs, including Claude stream message IDs when no provider header is available.
- Kept image generation opt-in. App defaults remain disabled; image fixtures opt in explicitly.
- Preserved both migration histories. All 18 fork files and 28 upstream files match their
  respective parents byte-for-byte; shared files yield 38 distinct full filenames. The migration
  runner identifies migrations by full filename, not numeric prefix alone.
- Repaired automatic-merge damage to DTOs, repository mixins, policy composition, query cache
  identity, fixed time windows, translations, responsive classes, and component test harnesses.

## Local Verification

- Final unit regression: 3892 passed, 7 skipped. A temporary external pytest selector separated
  109 local-PostgreSQL cases and 137 .NET execution cases from this run; it is not shipped.
- Isolated PostgreSQL upgrade/concurrency suite: 8 passed, including fresh install, upgrade from
  the fork's 18 applied migrations, historical row/checksum preservation, and no-op reruns.
- Frontend/deployment/directory-policy regression: 298 passed.
- Model editor and availability component regression: 53 passed.
- Publication/connection component regression: 189 passed.
- Public-price publication integration cases: 3 passed.
- Python Ruff and Mypy passed; frontend TypeScript/Vite build passed.
- OpenAPI validated with 38 warnings; Bicep compiled with 3 warnings; APIM XML parsed.
- Mocked browser smoke passed at 1440x1000 and 390x844: directory loaded, model editor opened,
  manual pricing selected, display name edited, no uncaught errors, no save request sent.
- `git diff --check` passed. No existing migration file was rewritten.

## Boundaries

- The complete suite was exercised during integration, but a fully green unfiltered run is not
  claimed: the local .NET SDK is absent, so the 137 executable policy gates were not validated.
- Additional standalone Node suites contain pre-existing outdated upstream harness/source
  expectations. An unchanged `upstream/main` archive reproduced 91 failures under the same
  runtime/dependencies. The merged checkout's final standalone run has 406 passing and 80 failing
  cases; every remaining failure also fails in the unchanged upstream archive. Passing focused
  cases above are reported independently of that suite.
- Mocked browser checks are local UI evidence only, not authentication, Azure, gateway,
  provider, production-database, or deployment acceptance evidence.
- Deployment and infrastructure rollout require a separate task and environment validation.

## 当前分支配置与验证补充（2026-10-18）

`docs/` 原有上游文件保持 `main` 的 `e59177d61c27d3641fff86caed8fe476bacf858e`；
另按用户要求增加[企业插件使用专区](../docs/enterprise-plugin/README.md)，不改变原有文件字节。
以下是本分支必须另外遵循的配置与升级要求。
文档迁移没有修改任何已应用 SQL，也没有重新安装、部署或变更线上预算。

以下内容从本分支修改的 `docs/` 文档迁移，描述分支实现和操作要求，不代表本次重新部署或完成所有验证。

<!-- Migrated from docs/testing.md; section: Clean installation -->

## Clean installation

Pricing upgrade `010_models_dev_pricing` adds the public catalog source and matching metadata
without rewriting accepted prices. Verify explicit manual configurations remain manual, absent
configurations follow the public default, and the numbered migration is applied once.

```bash
uv sync --frozen
npm --prefix frontend ci
```

<!-- Migrated from docs/testing.md; section: Required checks -->

## Required checks

```bash
uv run ruff check backend turnstile_core scripts tests functions/telemetry/function_app.py functions/control_plane/function_app.py
uv run mypy backend turnstile_core scripts tests
uv run pytest -q
npm --prefix frontend run build
node --import ./tests/support/register_typescript.mjs --test tests/platform/frontend/*.test.mjs
az bicep build --file infra/main.bicep
xmllint --noout infra/policies/foundry-finops-policy.xml
npx --yes @redocly/cli@2.38.0 lint contracts/openapi.yaml
git diff --check
```

The standalone frontend suites require Node with native TypeScript stripping and
`registerHooks` support. The resolver keeps the application's extensionless TypeScript
imports usable under Node without changing the browser bundler's module contract. These
isolated component checks do not replace authenticated browser or production acceptance.

<!-- Migrated from docs/testing.md; section: Database validation -->

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

<!-- Migrated from docs/testing.md; section: Migration inventory -->

### Migration inventory

The current chain contains **40 migrations**, listed in the runner's lexicographic filename
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
| `019_client_user_authorization.up.sql` | Fork | Independent desktop device authorizations, user grants and installation generations. |
| `019_runtime_display_name.up.sql` | Upstream | Drops the global `model_runtime_name_key` uniqueness constraint; adds no metadata columns. |
| `020_failed_publication_spec.up.sql` | Upstream | Rebuilds the reusable-spec unique index, excluding failed, superseded and rolled-back publications. |
| `020_plugin_download_permissions.up.sql` | Fork | Independent user/subscription plugin download permissions with legacy-compatible role defaults. |
| `021_usd_budget_reservations.up.sql` | Upstream | USD budget limits, reservations, immutable admission prices and cost-aware settlement. |
| `022_subscription_tags.up.sql` | Upstream | Application subscription-tag storage. |
| `023_usd_reservation_resolution.up.sql` | Upstream | USD settlement/release evidence validation against immutable admission and approval. |
| `024_gateway_usage_attribution.up.sql` | Upstream | Per-request APIM target, Gateway deployment/instance and revision attribution. |
| `025_usage_cost_component_prices.up.sql` | Upstream | Replaces `budget_usage_usd_cost` to prefer stored exact USD cost or require prices only for nonzero usage components; estimated/unpriced usage stays unknown. Adds no price columns. |
| `026_invocation_trace_service_identity.up.sql` | Upstream | Matches platform invocation telemetry using service identity. |
| `027_product_schema_alignment.up.sql` | Upstream | Application recovery/finalization and Gateway provisioning tables; aligned finalization and invocation views. |
| `028_product_index_alignment.up.sql` | Upstream | Scope/period finalization index and unique self-hosted provisioning request index. |

<!-- Migrated from docs/testing.md; section: Upgrade validation -->

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

<!-- Migrated from docs/testing.md; section: Governed image generation and evidence v2 -->

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

<!-- Migrated from docs/e2e-validation.md; section: Platform bootstrap -->

## Platform bootstrap

- [ ] Apply the complete candidate chain with `uv run python -m backend.migrate`
- [ ] Verify one ledger row and matching checksum per full version in the [migration inventory](#migration-inventory)
- [ ] Re-run the migration command and confirm zero pending migrations, unchanged checksums and applied timestamps
- [ ] Start the API and frontend
- [ ] Create the first password account with `python -m backend.accounts <email> --role owner`
- [ ] Sign in through the real web page with that email and password
- [ ] Confirm `/api/v1/auth/me` reports `role=owner` and `method=password`
- [ ] Confirm health, authentication, registry, and empty-state pages

<!-- Migrated from docs/deployment.md; section: Prepare for v1.2 -->

## Prepare for v1.2

Turnstile v1.2 is the current public release. Upgrading an existing v1.1 installation requires
reviewing the migrations and protocol steps below in addition to uploading application packages.

For this merged fork, review and apply all pending migrations shipped with the compatible candidate
through `uv run python -m backend.migrate`, before starting packages that require the new schema.
Preserve every applied version and checksum. The [complete migration inventory](#migration-inventory)
lists both histories in runner order: the full filename minus `.up.sql` identifies a version,
not its numeric prefix. Feature introduction migrations below are not a selectable subset of
the candidate chain. Feature flags and external protocol installation remain separate steps.

| Change | Required preparation | Verify before use |
| --- | --- | --- |
| Catalog pricing, discounts and context tiers | Compatible API, Control Plane and Telemetry readers; `007_model_price_source`, `008_price_review_and_guard`, `009_context_tier_pricing` and `010_models_dev_pricing` | Regional references, all price buckets, explicit tier boundaries and preserved historical snapshots. |
| Registered targets, initialization and multi-target API publication | `011_gateway_targets`, `012_apim_onboarding`, `013_multi_target_rollout` and target-specific worker configuration | Exact APIM identity, endpoint trust, declared capabilities and per-target results; multi-target publication is an API workflow, not a multi-select Add Model form. |
| Create a Self-hosted Gateway | `014_self_hosted_gateway_provisioning` and the separately reviewed Gateway-management permissions | API association, a running operator-managed container, HTTPS, restart-persistent ingress and configuration-token renewal. |
| Platform invocation attempts and explicit Gateway selection | `015_platform_invocation_trace`, `026_invocation_trace_service_identity`, `027_product_schema_alignment` and compatible API readers | Failures remain visible; selected Gateway scope is respected; incomplete usage is not billed as zero. |
| Versioned model availability | `016_model_availability`, compatible workers, Table access, all retained Revision policies and self-hosted enrollment | Full fresh coverage, disabled rejection and enabled recovery across all serving Gateways. |
| Atomic budget execution and USD reservations | Complete candidate chain, including `010_budget_execution_protocol` and `021_usd_budget_reservations`; separate opt-in flags, identities and reviewed cutover | Existing balances, admission, completion and recovery; an application update does not activate the protocol. |

1. Back up the existing private deployment state in an approved secret store. Keep the database,
  APIM/API identities, model references, budgets and historical releases; do not start with a new
  deployment state to upgrade an existing installation.
2. Select and validate one compatible application candidate. Inspect which full migration versions are
  missing and retain the checksums of all applied migrations. Never edit an applied migration.
3. Review separate infrastructure and policy changes, permissions, network reachability and the
  expected maintenance impact. Do not grant APIM operation-creation rights to the runtime Publisher.
4. Apply the approved pending chain before starting matching application packages; verify the mounted
  packages, served frontend assets, Function host/index and API contracts.
5. Complete each enabled feature's own installation and real acceptance checks. Record missing
  prerequisites as unavailable, and preserve prior failures and in-flight request evidence.

Follow the [availability installation](../docs/deployment.md#model-availability-installation),
[Self-hosted Gateway management](#self-hosted-gateway-management) and
[budget cutover](aigateway-proxy-client/operations.md#budget-execution-cutover) sections only for the capabilities being installed.
There is no single package-only command that proves all of these protocols ready.

<!-- Migrated from docs/deployment.md; section: Self-hosted Gateway management -->

## Self-hosted Gateway management

The optional [access template](../infra/self-hosted-gateway-access.bicep) enables Turnstile to
manage new Self-hosted Gateway configurations on an **existing** APIM service. It never creates
an APIM instance, server, network or container and is not invoked by the default installation.
Use explicit subscription, APIM resource group/name, API managed-identity principal ID and
Control-plane principal ID. Review a subscription-level what-if before applying it; only the two
new role definitions and two assignments scoped to that exact APIM service are expected.

The Worker role allows Gateway creation, API association and necessary inventory/permission reads.
The API role allows Gateway and association reads plus `gateways/generateToken/action` only.
Neither role grants Gateway deletion, model subscription key reads, shared-key rotation, policy
updates or role assignment. The existing Publisher role is unchanged. These installation-time
permissions require operator authorization; the application never grants itself additional roles.

Before rollout, approve and apply the complete pending candidate chain through `backend.migrate`,
including `014_self_hosted_gateway_provisioning`, `027_product_schema_alignment` and
`028_product_index_alignment`, preserving existing migration checksums and history.
Deploy matching API, Control-plane and shared-model readers with creation disabled, then
enable the settings described in
[configuration](../docs/configuration.md#create-a-self-hosted-gateway-from-turnstile). Confirm the managed
identities' effective permissions and real worker execution, not merely application availability.
The endpoint must be reachable through HTTPS from the API and Control-plane applications.

Feature acceptance must start with the generated Gateway absent from APIM and create it through
Turnstile. Precreating a Gateway outside the application and attaching it proves only the legacy
association workflow. A generic package-only functional validation does not authorize these RBAC
or APIM resource changes; use a separately reviewed, exact-target infrastructure/feature plan.
Retain existing APIM resources, containers, application state and financial history.

<!-- Migrated from docs/deployment.md; section: Deploy v1.1 from zero -->

### Deploy v1.1 from zero

Follow [Prerequisites](../docs/deployment.md#prerequisites), [Prepare public parameters](../docs/deployment.md#prepare-public-parameters),
[Preview](../docs/deployment.md#preview) and [Deploy](../docs/deployment.md#deploy) below with new state and a unique resource prefix.
Do not reuse the v1.0 secret state or database for a clean installation. Verify regional
availability and capacity for PostgreSQL 16, separate Flex Consumption Functions and the
default StandardV2 APIM before provisioning. APIM creation can take substantially longer than
deploying the application packages.

The main template creates the ledger account/table and supplies `LEDGER_SYNC_ENABLED=true`,
`LEDGER_TABLE_ENDPOINT` and `LEDGER_TABLE_NAME` to the API. It grants that API's managed
identity Storage Table Data Contributor on the **new Table**, with dependencies on the new
identity and table. No separate model-access upgrade template is required for this path.

The initial database has no customer models, connections or usage. Sign in as the initial
Owner, create a provider Connection, publish an existing upstream deployment, assign model
access and then test invocation. An unconfigured policy and an explicit empty policy are
different; empty means deny all. Configure real production budgets before admitting client
traffic. A healthy homepage is not proof that publication, identity, permissions or billing work.

For Databricks, use the HTTPS Workspace root, an existing supported serving endpoint and
the managed identity of the **selected APIM**, not the Turnstile API identity. A Workspace
administrator must register/authorize that identity and grant the required serving-endpoint
access. Managed identity is the default; OAuth M2M requires its explicit deployment capability
and scoped APIM Credential Manager permissions. Neither method creates upstream endpoints.
The supported Databricks publication path is native Anthropic chat/streaming, not image
generation or an assumption of every OpenAI-compatible operation.

Foundry text, governed images and an OpenAI-compatible provider have separate model and
credential prerequisites. Images require the explicit `IMAGE_GENERATION_ENABLED` opt-in, an
existing Foundry image deployment, image-ready APIM infrastructure and consent for paid probes.
Publication probes also consume upstream
requests; include them when choosing a validation budget. Never enable an upstream provider's
local-key authentication or weaken networking merely to complete installation.

<!-- Migrated from docs/deployment.md; section: Image and evidence upgrade boundary -->

### Image and evidence upgrade boundary

Apply pending migrations `004_apim_usage_identity_guard`, `005_billable_request_lifecycle` and `006_versioned_budget_evidence` through the same authorized migration entry point before running the new packages. Do not edit or replay the earlier migrations. The upgrade adds guarded APIM attempt identity, a durable billable-request journal and versioned budget evidence without rewriting historical usage. Drain old consumers before introducing the new identity guard. PostgreSQL concurrency and upgrade safety require the database checks in [Testing](#database-validation).

The API and Control-plane packages both require the pinned Pillow dependency for full image validation. The Control-plane artifact includes the public canonical parent policy at `policies/foundry-finops-policy.xml`; publication and image rollback read it through `CONTROL_PLANE_PARENT_POLICY_PATH`. The publisher verifies the live policy and its readback, and refuses incompatible image policies instead of transforming an unknown live template. Use the APIM infrastructure upgrade below before publishing image models on an older installation; do not rerun the bootstrap template against customer routing.

`IMAGE_GENERATION_ENABLED` defaults to false in this fork; the Bicep parameter
`imageGenerationEnabled` also defaults to false. Keep `IMAGE_GENERATION_ENABLED=false` during
the code and migration rollout. Enable it only after verifying the image infrastructure,
compatible packages and provider prerequisites described in [configuration](../docs/configuration.md).
Keep the evidence-v2 cutoff unset during rollout, or preserve an existing cutoff unchanged.
Neither migration nor the image flag authorizes paid publication probes or scheduling the future
evidence cutoff; these remain separate operational decisions. This upgrade does not provision a
Foundry account or model, modify an upstream deployment, or grant new provider roles.

<!-- Migrated from docs/configuration.md; section: OpenAI-compatible connections -->

### OpenAI-compatible connections

Register an OpenAI-compatible Connection with an HTTPS Base URL and optional API-provider display metadata. URLs must not contain credentials, query parameters, or fragments. A trailing `/chat/completions` is normalized into the provider path; the public gateway path remains `/chat/completions`.

Connection registration does not accept an API key or publish a model. Add the first model on that existing Connection and provide its one-time bearer key. The publisher encrypts the temporary key, materializes an APIM Secret Named Value, and erases the temporary ciphertext. Subsequent models reuse the Connection credential. The API and Control-plane packages must both support this schema before onboarding.

Compatible Chat Completions connections use `max_tokens`; Foundry keeps its configured `max_completion_tokens` behavior. Measured cache usage prefers `prompt_tokens_details.cached_tokens`, including explicit zero, and falls back to top-level `cached_tokens` only when the nested measurement is absent. Provider display metadata does not change routing, credentials, or pricing. Configure actual model rates explicitly.

<!-- Migrated from docs/configuration.md; section: Governed image generation -->

### Governed image generation

This fork preserves the existing image opt-in: `IMAGE_GENERATION_ENABLED` defaults to false.
Enable it explicitly only in an authorized environment. Deploy and verify the API, Control-plane
package, required migrations and image-ready APIM parent policy together. The Registry advertises
`image_generation_supported` from that setting and `image_configuration_schema_version: 4`;
an older backend must not be treated as image-capable. Publication still rejects missing or
incompatible APIM image infrastructure, and invocation still requires a published model,
authorization and available budget.

The Bicep `imageGenerationEnabled` parameter defaults to `false` and supplies the same flag to the API and Control-plane. After verifying the prerequisites, preserve an explicit opt-in in the public deployment parameters. Enabling the flag does not publish models or authorize paid probes; keep it false during package and migration upgrades.

`IMAGE_GENERATION_DEFAULTS` is a JSON object containing `max_request_bytes` (24576), `max_response_bytes` (16777216), `output_reservation_tokens` (8192), and `timeout_seconds` (180). Request/response limits cannot exceed 16 MiB, timeout cannot exceed 240 seconds, and request bytes plus output reservation must fit a signed 32-bit counter. The parent policy burst cap must cover that sum. These are transport and accounting limits, not provider size, quality or format defaults.

Publish a model on an existing Foundry Connection with `model.operation: image_generation`, an existing deployment name, and explicit nonnegative text-input, cached-text and image-output prices. Zero is an explicit free rate; an absent rate is not zero. Image models cannot be chat defaults or backend Pool members. Parameter names and values are provider-owned JSON; only one image and non-streaming responses are supported. Registry metadata edits cannot change the immutable profile. New limits use the normal route-reconciliation publication with an `image_configurations` model-key map.

Image previews accept validated PNG, JPEG and WebP only. The backend does not store prompts or image bytes. Unknown post-dispatch usage remains reserved, and the caller and publication worker do not automatically repeat a paid image request. An Owner may explicitly authorize a bounded replacement publication probe with `authorize_image_probes: true`; inspect the previous billable attempt first.
