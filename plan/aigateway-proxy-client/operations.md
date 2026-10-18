# Budget Operations

This is the maintained operations contract for this branch. Original upstream files in `docs/`
stay byte-for-byte aligned with the reviewed baseline; branch operations remain under `plan/`.
The separately requested [enterprise plugin guides](../../docs/enterprise-plugin/README.md)
are end-user/admin documentation, not installation journals or live environment settings.
For the completed installation and ordinary-user USD test, see the dated
[USD acceptance record](usd-installation.md), not a live health guarantee.
Do not repeat initialization because an old plan or status file says it is incomplete.

## Scope and invariants

- Token and USD organizations can share an APIM. Organization/department allocation constraints
  remain separate from request-time Person and applicable Application execution scopes.
- Local password users remain supported. Trusted backend delegation uses a service identity;
  it does not require converting users or organizations to Entra management.
- An application deployment or enabled flag alone does not install or activate financial admission.
- Apply the complete migration chain using `backend.migrate`. Never edit applied migration files,
  reset historical consumption, replace an existing epoch or seed a balance twice.
- Preserve subscription authentication, model access, TPM, route dependencies and existing budgets.
  Unpriced usage is unknown, not zero; a timed-out request must not be replayed automatically.

See [upstream configuration](../../docs/configuration.md#atomic-budget-execution) for baseline settings and
[Deployment](#budget-execution-cutover) for the general cutover contract.

## Supported tools

These are maintained operator tools, not disposable scripts from one environment. Their tests live
in `tests/platform/infrastructure/`. None bundles production credentials or a ready-made cloud adapter.

| Module | Supported entry point | Boundary |
| --- | --- | --- |
| `scripts/prepare_budget_execution.py` | Offline CLI: `--configuration`, `--output` | Renders prepared artifacts only; no cloud access or activation. |
| `scripts/budget_execution_upgrade.py` | Offline CLI: `--current-parent`, `--gateway-profile-id`, `--delegation`, `--output` | Verifies known schema-one parents; refuses an existing execution epoch or unknown custom policy. |
| `scripts/install_budget_execution.py` | CLI: `--plan`; library: `initialize_rollout`, `InstallationJournal` | CLI only validates a current saved plan. Initialization requires separate fresh maintenance readbacks and a database connection. |
| `scripts/budget_installation_publication.py` | Library: build, promote, read-only recovery and SQL publication adoption | Requires reviewed cloud adapters, exact ETags and preserved serving lineage; does not activate admission. |
| `scripts/budget_installation_activation.py` | Library: `activate_budget_installation`, `recover_budget_activation` | Validates settlement and exact scope evidence before changing the existing fence; recovery reads the original intent only. |
| `scripts/budget_policy_bindings.py` | Shared XML binding helper | Copies templates; rejects missing or recursively introduced parameters. No cloud I/O. |

Use `uv run python -m scripts.prepare_budget_execution --help`,
`uv run python -m scripts.budget_execution_upgrade --help` and
`uv run python -m scripts.install_budget_execution --help` to inspect the actual CLI arguments.
There is no universal `install --activate` command. A saved plan is not proof of live cloud state.
Environment-specific cloud adapters, acceptance collectors, credentials and raw evidence stay in a
private recovery workspace; do not move them into public source or delete unresolved journals.

## Budget execution cutover

The atomic budget protocol is not activated by an application update. Keep all new execution flags
off until the complete pending candidate chain has been applied through `backend.migrate` and
verified. Its budget work includes `010_budget_execution_protocol`,
`017_budget_terminal_observation`, `018_budget_reservation_resolution`,
`021_usd_budget_reservations`, `023_usd_reservation_resolution` and
`025_usage_cost_component_prices`; applying only the original protocol migration is insufficient
for the current readers and USD settlement. Verify compatible API/Telemetry/Control Plane packages
and the Observer image before activation. The Observer build uses an isolated allowlist of its four
runtime files and three shared standard-library completion modules; its version digest includes
those modules. Do not build from the adapter directory alone or upload a whole working tree.

For an existing installation, use an explicitly authorized maintenance plan bound to exact resource
IDs, source/package hashes, old policy revisions and ETags. Preserve authentication, model access,
TPM, historical usage, applied migration checksums and current budgets. Missing managed-identity
roles or private connectivity require a separate infrastructure review, not a feature-flag workaround.

1. Record the current parent and operation policies, all retained revisions, reader versions, active
  work and per-scope financial counters. Verify that each application can reach the existing Table
  with its own managed identity and only its required access. Before any mutation, capture encrypted
  database/ledger, runtime/settings and APIM recovery material, validate hashes and an isolated
  database restore, and store recovery keys separately.
2. Apply all approved pending candidate migrations and deploy compatible readers/writers with
  flags disabled. Verify the candidate's actual Telemetry function index, including
  `process_usage_events` and `settle_budget_requests`, and that the real worker can run, not just
  that its site reports Running. Preserve the existing always-on policy.
3. Prepare an explicit epoch, model counting contracts and disabled scope seeds. Use
  `uv run python -m scripts.prepare_budget_execution --help` for offline candidate rendering; this
  tool writes local artifacts only and refuses to generate an active cutover. Bind the reviewed
  candidate to the existing serving lineage, model price snapshots and counting contracts.
4. Fence old admissions across every reachable revision, drain in-flight requests and telemetry,
  and compare exact old confirmed balances. Outstanding or conservatively charged legacy
  reservations block scope initialization. Seed each opening balance once, never by resetting
  history. A stale readback cannot authorize the cutover. Freeze ordinary publication during this
  window. SQL coordination may be active while the shared gateway fence still remains paused.
5. Validate the exact canonical execution parent and shared fence with its prepared validation
  scope. Update only the reviewed policy resources using their exact ETags and read all content
  back. Never replace every `executionPlan` variable: the native-count apply branch is distinct
  from the initial plan. Retain all previous revisions without admitting unfenced old traffic.
  Promote only the verified candidate, then record the actual serving revision and preserved
  dependencies in SQL. Configure requested budgets through normal APIs and await policy sync;
  do not silently allocate personal budgets or change unrelated organizations.
6. Activate only after scope balances, compatible readers and all policy readbacks agree. Verify
  request, APIM correlation, receipt, formal event, database settlement task and Table S/A counters
  for the same attempt. Errors and interrupted streams must preserve outstanding charges.
  After guarded activation, restore ordinary publication, verify an explicitly authorized ordinary
  request without subscription/revision overrides, and revoke temporary roles and sessions.

Publication snapshots bind epoch, model identity, route digest and counting contracts. A normal
publication cannot upgrade an incompatible parent, and rollback cannot reinstate a legacy parent
under an execution fence. Returning to old admission requires its own drained, financially verified
transition; switching a flag off is not a rollback procedure.

Claude Code validation requires an already registered native Messages model and the actual gateway
subscription header. Set the published alias through `ANTHROPIC_MODEL`, preserve normal client
behavior, disable retries and never replay an unknown outcome. Bare mode, model-name substitution,
thinking rewrites and successful checks against another environment are not acceptance evidence.

The installation plan expires within 24 hours and cannot cross a UTC budget month. Maintenance and
activation require observations no older than 30 seconds. An expired plan is not repaired by changing
its timestamps: preserve its journal, read actual state and obtain a new review for the remaining work.
Already initialized or promoted installations must resume from their actual state, not repeat earlier steps.
The current initialization library requires one Gateway target; it explicitly rejects multi-target
initialization without independent fence proofs.

## Settings and worker checks

Set only the responsibilities required by each runtime; never turn every setting on everywhere.

| Runtime | Responsibility |
| --- | --- |
| API | Execution reads and dispatch with explicit target/delegation settings; not the Telemetry settlement worker. |
| Telemetry | Compatible execution reader plus `BUDGET_SETTLEMENT_ENABLED` and the settlement/recovery timer. |
| Control Plane | Compatible execution-aware publication; `CONTROL_PLANE_ENABLED` controls ordinary publication separately. |
| Observer | `BUDGET_COMPLETION_ENABLED`, exact existing Table endpoint/name and its own managed identity/network. |

Check `AzureWebJobs.settle_budget_requests.Disabled` as well as the runtime flags. A disabled timer
prevents scheduled settlement even when the financial settings are correct. Preserve unrelated
per-function disable settings and verify actual worker executions after any approved change.
`BUDGET_RESERVATION_RESOLUTION_ENABLED` is a separately authorized Owner recovery capability,
not a prerequisite for enabling the USD selector; restore its prior state after temporary use.

`GET /api/v1/budgets/capabilities` is an authenticated, no-store check of installed SQL/configuration
and publication evidence. It deliberately does not scan ARM on every form open. It does not replace
live ledger, worker, fence or real settlement acceptance. See
[Budget verification](#budget-verification) for the Owner verification APIs.

## Budget verification

The Gateway Releases page offers two Owner-only, no-store operations under
`/api/v1/model-management/gateways/{gateway_profile_id}`:

- `POST /budget-readiness` checks delegation or fencing through catalog reads, without inference.
- `POST /budget-execution-verification` sends one fixed, nonstreaming verification request with
	a 32-token output limit and the normal configured model runtime timeout. Its body accepts only
	`application_id` and `model_id`; the signed-in Owner determines the person identity.

These operations require the selected registered target to be configured in both the execution
target allowlist and delegation targets, with a trusted HTTPS origin and the existing Table
ledger. They do not enable execution, grant permissions, change budgets or prepare candidates.
The readiness result reports expected and actual status/error evidence, never a fabricated pass.

Joint verification requires an exact paused protocol fence naming the validation person,
Application, subscription and isolated Revision. All configured execution targets must be paused
in the same epoch, and both Person and Application scopes must already be active and match that
epoch. The Application must be active, non-system and bound to the selected APIM. Model access is
checked for both scopes. The target fence is read again immediately before dispatch.

The request uses the normal platform dispatch and nonbilling Trace recording. Delegation tokens
are sent only to APIM and never returned in the response. Only matching settled receipts for both
scopes, with exact measured usage, establish settlement. A timeout, missing receipt, mismatched
identity or delayed settlement remains unconfirmed and never authorizes automatic replay.
Each explicit verification can incur normal model charges; operational isolation and real
accounting acceptance require separate authorization.

The directory-readiness HTTP timeout is separate from the inference timeout; an unknown
inference result must not be retried automatically.

Before reservation, request analysis walks only protocol-defined message, content and tool-result
positions. Tool schemas, arguments, metadata and text containing JSON are business data, not new
envelopes to scan recursively. The entire original body still contributes to a legacy byte estimate.
Chat Completions, Responses, Messages, compact Responses and Images have distinct rules. Unknown
structures produce incomplete analysis: verified-bound contracts reject them, while an explicitly
labelled legacy estimate remains an estimate. Analysis never logs user values in error paths.

Native counting must belong to the selected route's HTTPS origin, operation and existing auth.
Only a verified upper bound can reduce a reservation; an estimate or failed count cannot do so.
Native-count payload projection does not mutate the provider request. Images and compact Responses
retain their operation-specific counting contracts. No model name or client version bypass exists.

Invalid requests return `budget_request_invalid` (400); analysis and plan faults return
`budget_request_analysis_failed` or `budget_plan_failed` (500); invalid configuration or unavailable
identity returns `budget_configuration_invalid` or `budget_identity_unavailable` (503). A real ledger
failure retains `budget_ledger_unavailable`; it is not the fallback label for parser errors.

Complete APIM/Observer evidence requires exact, consistent counters and immutable receipt identity.
Interrupted or incomplete streams keep their reservation. Observer completion uses a bounded
conditional Table transaction; a failed direct write still emits the event for durable recovery.
Equal-rank conflicts remain recorded; weaker evidence cannot overwrite completion or release an
unknown attempt. Retries of a receipted Pool request are disabled to preserve one attempt per charge.

## Troubleshooting

| Symptom | Inspect | Do not do |
| --- | --- | --- |
| USD option unavailable | Capability reason, execution target/delegation, current rollout period and publication/counting contracts. | Force the UI enabled or bypass the backend save guard. |
| Department USD edit disabled | Parent allocation must use `budgetLimit(organization) != null`, not only `token_limit`. | Recreate budgets or assume two organizations conflict. |
| Saved policy stays pending | Telemetry settlement flag, actual timer enable/index state, task errors and ledger access. | Treat a SQL row update as an applied gateway policy. |
| Inference times out | Verification inference uses the normal model runtime timeout; directory readiness has its own HTTP timeout. | Restore the former extra eight-second inference timeout or replay unknown results. |
| Observer completion missing | Shipped stdlib-only modules, MIME type/parameters, exact counters, identity and Table delivery. | Import unshipped API/Pydantic modules or fill missing counters with zero. |
| Chat reasoning differs | Only add explicit reasoning if advertised total equals prompt + completion + reasoning and differs from prompt + completion. | Double count already included reasoning or accept unexplained differences. |
| Publisher enabled but inactive | Actual loaded artifact, function index, FDM deployment substeps and subsequent executions. | Assume site Running or a setting readback proves worker health. |
| Database unavailable | Database state plus authenticated budget/business reads. | Accept `/health` liveness alone or redeploy the platform to fix a stopped database. |

## Recovery and evidence retention

Keep original plans, seeds, operation intents/results, usage, price snapshots and Owner approvals
immutable. Recovery of an unknown cloud write first reads the exact original resource; it does not
create a second journal to hide an unresolved intent. Failed inference is not automatically free.

An authorized Owner reservation release does not set unknown formal cost to zero. Preserve original
usage and allow later exact evidence to settle it. A test risk ceiling is not an actual invoice.

Before financial activation, restore only reviewed compatible artifacts while retaining additive
migrations and audit state. After consumption, pause and drain, reconcile current SQL/Table balances
and prefer forward repair. Restoring old balances, independently rewinding checkpoints, turning off
flags or switching to an unfenced legacy revision is not a safe rollback.

Keep encrypted backups, raw journals and secrets outside Git in durable restricted storage. `/tmp`
may hold a working copy but is not a durable recovery index. The dated acceptance record links to
the private backup inventory without publishing keys, cookies or full runtime settings.

## Plugin and permission regression requirements

`020_plugin_download_permissions` adds the four-role download matrix to the existing
permission policy. Existing menu choices, revisions, audit history, browser sessions,
budgets and usage are preserved; user-plugin downloads remain enabled for all groups,
while subscription-plugin downloads remain Owner-only until explicitly delegated.
Validate atomic menu/download saves, CAS conflicts, menu-only client compatibility,
existing-session revocation, and rejection before key reads or grant creation.
Delegation permits only applications owned by that account; it does not grant access
to another application's subscription key or change model/gateway authorization.
The PostgreSQL upgrade test checks 001-019 checksums and business history, then reruns
the standard migration entry point as a no-op. Browser QA checks collapsible sections,
desktop two-column/mobile single-column downloads, independent card visibility and
the absence of requests for hidden editions.

`019_client_user_authorization` adds independent desktop device authorizations,
hashed user token families, and generation-checked imports. It does not change existing
browser sessions, subscription keys, historical usage, or budget evidence. User mode remains
disabled until `CLIENT_USER_ENABLED=true` and an exact `CLIENT_PLATFORM_ORIGIN` are configured.
Run the real PostgreSQL client-grant tests, user API tests, plugin stream tests, and APIM
client-policy tests before enabling it. Negative signatures and model/subscription mismatches
must fail on an isolated APIM revision before current traffic is changed.

The image companion uses loopback-only authentication and an image-only platform capability;
it never puts an APIM key or the main OAuth refresh token into a normal image provider.
Desktop magpie's inference port does not expose its GUI provider mutation APIs. Register the
generated private MCP configuration in the intended Agent, or import the owned picture
provider through magpie's supported UI; do not pretend automatic desktop registration passed.
Test sign-out/disable shutdown and preservation of other providers separately from image calls.

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

## Local regression

```sh
uv run --frozen --no-sync pytest -q \
  tests/platform/infrastructure/test_budget_policy_bindings.py \
  tests/platform/infrastructure/test_budget_parent_upgrade.py \
  tests/platform/infrastructure/test_budget_installation.py \
  tests/platform/infrastructure/test_budget_installation_publication.py \
  tests/platform/infrastructure/test_budget_installation_activation.py \
  tests/platform/infrastructure/test_budget_operations_docs.py \
  tests/backend/model_platform/test_budget_readiness.py \
  tests/backend/model_platform/test_budget_readiness_credentials.py \
  tests/backend/model_platform/test_budget_diagnostics.py \
  tests/platform/frontend/test_budget_directory.py \
  tests/platform/deployment/test_deploy_script.py \
  tests/platform/infrastructure/test_envoy_cache_adapter.py
```

These are local regression tests, not live financial acceptance, load testing or a disaster-recovery
certification. Any paid test requires a separate explicit scope and spending authorization.
