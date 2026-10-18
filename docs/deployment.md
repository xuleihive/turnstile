# Deployment

## Scope

Turnstile infrastructure includes PostgreSQL, Event Hubs, Storage, Key Vault, Application Insights, the Web App, telemetry and control-plane Functions, and APIM configuration. FastAPI and the observer each have a dedicated App Service Plan. The telemetry and control-plane Functions each have a separate Flex Consumption plan, deployment container, and VNet subnet. The templates do not create Azure AI Foundry projects or provider model deployments.

The API, telemetry and control-plane Functions, observer, and budget ledger require public service endpoints for package deployment or runtime traffic. They explicitly enable public network access and carry the resource-level `SecurityControl=Ignore` tag so an organization-level network Modify policy does not silently disable them. The exemption is not applied at resource-group scope; platform Storage and Key Vault resources designed for private-link access remain private.

## Prepare for v1.2

Turnstile v1.2 is the current public release. Upgrading an existing v1.1 installation requires
reviewing the migrations and protocol steps below in addition to uploading application packages.

For this merged fork, review and apply all pending migrations shipped with the compatible candidate
through `uv run python -m backend.migrate`, before starting packages that require the new schema.
Preserve every applied version and checksum. The [complete migration inventory](testing.md#migration-inventory)
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

Follow the [availability installation](#model-availability-installation),
[Self-hosted Gateway management](#self-hosted-gateway-management) and
[budget cutover](#budget-execution-cutover) sections only for the capabilities being installed.
There is no single package-only command that proves all of these protocols ready.

## Model availability installation

Availability is an application and Gateway protocol upgrade. Deploying the UI alone does not
install admission protection. The public base infrastructure does not automatically reconcile
existing Revision policies or self-hosted rejoin controls. The runtime Publisher remains unable
to create APIM operations; its existing `ensure_operation` path only verifies installed operations.
The independent [installation tool](../scripts/install_model_availability.py) is an operator tool,
not a background Publisher permission expansion.

Before installation, authorize the exact database, APIM service/API, every retained Revision and
the shared primary Table. Preserve migration checksums, model/price/default/access/budget data,
policy ETags and hashes. Quiesce publication and administrative writers and arrange a reviewed
maintenance window: the installer updates policies in place and stops at the first uncertain or
mismatched result; it is not a zero-downtime or automatic rollback workflow. Ensure compatible
API and Control Plane packages, migration `016_model_availability`, exact scoped identities,
network reachability and Table access are available before admitting normal traffic.

An installation manifest validates against `AvailabilityInstallation`. It contains explicit
`subscription_id`, `resource_group`, `service_name`, `target_id`, `api_id`, `table_endpoint`,
`table_name` and `revisions`. Each revision entry is a `ModelAvailabilityPolicyAdoption` with a
unique adoption ID, target/API/Revision, exact alias-to-model-UUID mapping, alias-to-native-Gateway
scope, and SHA-256 values for `parent` and every original operation policy. Do not infer model
identity from a name alone or omit historical revisions. Keep manifests and journals outside Git;
they contain environment identity but must contain no credentials.

```bash
uv run python -m scripts.install_model_availability <installation-manifest.json>
uv run python -m scripts.install_model_availability <installation-manifest.json> \
  --expected-plan-sha256 <reviewed-plan-sha256> --journal <new-installation-journal.jsonl>
```

The first command is read-only and reports exact paths and baseline/candidate digests. Review
that plan before the second command. Every existing policy write uses its concrete `If-Match`
ETag; new diagnostic operations use `If-None-Match`. The journal is created exclusively with
mode `0600` and records intent before each write. A lost response or bad readback leaves the
attempt unresolved and prevents replay using that journal. Do not delete the journal or rerun
under a new name to bypass that stop. Inspect exact ARM state and derive a separately reviewed
recovery plan. The operator needs installation rights for the selected API operations; do not
grant those rights to the ordinary Publisher.

For self-hosted Gateways, separately verify the controller's complete instance inventory and
fenced installation/rejoin behavior. Record a `ModelAvailabilityNodeEnrollment` for every
deployment, including retained retired deployments: exact direct HTTPS node endpoints, instance
IDs, revisioned controller evidence, fenced-installation evidence and the
`enrolled_nodes_only_guarded_bootstrap` contract. `save_model_availability_node_enrollment`
requires the matching target inventory digest, next enrollment revision and actor. This port does
not provide a generic controller, VM reconciler or an enrollment HTTP endpoint. Do not invent
evidence hashes or mark enrollment complete from a load-balanced endpoint probe. Old unguarded
nodes must not be able to rejoin and receive consumer traffic.

After policy installation, the normal Control Plane projects retained model identities and
verifies full coverage. Keep traffic gated until the database and Table revisions, all catalogs,
all managed/self-hosted HEAD surfaces and node leases agree. The UI must remain unverified when
any requirement is absent. Validate disabled admission, enabled recovery, stale/conflicting
commands, worker lease loss and unchanged financial history on the explicitly approved targets.
Local tests do not establish Azure policy acceptance, PostgreSQL concurrency or actual node fencing.

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
[configuration](configuration.md#create-a-self-hosted-gateway-from-turnstile). Confirm the managed
identities' effective permissions and real worker execution, not merely application availability.
The endpoint must be reachable through HTTPS from the API and Control-plane applications.

Feature acceptance must start with the generated Gateway absent from APIM and create it through
Turnstile. Precreating a Gateway outside the application and attaching it proves only the legacy
association workflow. A generic package-only functional validation does not authorize these RBAC
or APIM resource changes; use a separately reviewed, exact-target infrastructure/feature plan.
Retain existing APIM resources, containers, application state and financial history.

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
  with its own managed identity and only its required access.
2. Apply all approved pending candidate migrations and deploy compatible readers/writers with
  flags disabled.
  Confirm five Telemetry functions are indexed and the real worker can run, not just that its site
  reports Running. Preserve the existing always-on policy.
3. Prepare an explicit epoch, model counting contracts and disabled scope seeds. Use
  `uv run python scripts/prepare_budget_execution.py --help` for offline candidate rendering; this
  tool writes local artifacts only and refuses to generate an active cutover.
4. Fence old admissions across every reachable revision, drain in-flight requests and telemetry,
  and compare exact old confirmed balances. Outstanding or conservatively charged legacy
  reservations block scope initialization. Seed each opening balance once, never by resetting
  history. A stale readback cannot authorize the cutover.
5. Validate the exact canonical execution parent and shared fence with its prepared validation
  scope. Update only the reviewed policy resources using their exact ETags and read all content
  back. Never replace every `executionPlan` variable: the native-count apply branch is distinct
  from the initial plan. Retain all previous revisions without admitting unfenced old traffic.
6. Activate only after scope balances, compatible readers and all policy readbacks agree. Verify
  request, APIM correlation, receipt, formal event, database settlement task and Table S/A counters
  for the same attempt. Errors and interrupted streams must preserve outstanding charges.

Publication snapshots bind epoch, model identity, route digest and counting contracts. A normal
publication cannot upgrade an incompatible parent, and rollback cannot reinstate a legacy parent
under an execution fence. Returning to old admission requires its own drained, financially verified
transition; switching a flag off is not a rollback procedure.

Claude Code validation requires an already registered native Messages model and the actual gateway
subscription header. Set the published alias through `ANTHROPIC_MODEL`, preserve normal client
behavior, disable retries and never replay an unknown outcome. Bare mode, model-name substitution,
thinking rewrites and successful checks against another environment are not acceptance evidence.

## Choose the v1.1 deployment path

| Your installation | Use | Keep or create |
| --- | --- | --- |
| Turnstile v1.0 is already installed | [Upgrade an existing installation](#upgrade-v10-to-v11) | Keep the original parameter file, secret state, outputs, database, ledger, APIM, identities and history. |
| No Turnstile installation exists | [Deploy from zero](#deploy-v11-from-zero) | Create a new prefix, resource group, state and platform. APIM reuse is an explicit alternative, not the default. |

An application ZIP update alone does not install missing APIM operations or grant the API
permission to update the ledger. Conversely, applying infrastructure alone does not update
the API or Control-plane code. Complete both parts of the selected path.

### Upgrade v1.0 to v1.1

1. Record the deployed version, package hashes, running/stopped application states, migration
  checksums, current APIM revision, model assignments and budget/usage totals. Retain the
  original public parameters, private deployment state and matching outputs. Back up the
  database through the installation's normal backup procedure. Do not regenerate encryption
  keys, bootstrap another Owner, or point at another installation's outputs.
2. Check out the reviewed v1.1 candidate and install its locked dependencies. Schedule a
  maintenance window, drain publications, release operations and old telemetry consumers,
  and exclude concurrent configuration changes. Follow the [APIM upgrade](#incremental-apim-infrastructure-upgrade)
  plan/apply sequence if the fixed image operation or parent contract is missing. That
  versioned upgrade preserves existing text routes and subscriptions; it is not APIM bootstrap.
3. Apply [immediate model-access configuration](#immediate-model-access-upgrade) to every API
  that saves People permissions. Reuse the exact ledger already read by APIM and written by
  Telemetry. Do not create an empty replacement table: it would lose the current enforcement
  state. The template changes only three API settings and Table-scoped identity access.
4. Apply only pending numbered migrations and deploy the v1.1 API, Telemetry and Control-plane
  packages using the existing deployment mechanism. The public `scripts.deploy plan` and
  `deploy` commands accept the **original** `--parameters`, `--state`, and Owner input; their
  saved outputs suppress APIM bootstrap. Review their complete what-if before using them:
  they reconcile the platform, not just application code. For a package-only rollout, retain
  the installation's existing package deployment process and apply the two explicit
  infrastructure upgrades separately. Never replay the full platform solely to fix permissions.
5. Restore the recorded application states and complete the [verification checklist](#verify).
  Confirm prior migration checksums, subscriptions, model routes, budgets and historical usage
  remain intact. Test an existing client as well as the newly installed features.

Keep images disabled and the evidence-v2 cutoff unchanged during the ordinary upgrade.
Enabling images or setting the one-time future cutoff requires the separate
[image/evidence procedure](#image-and-evidence-upgrade-boundary). Do not move an existing cutoff.
Databricks OAuth also remains disabled by default. Installing v1.1 does not grant the APIM
identity access to a Databricks Workspace or any provider account.

Application readiness is an API/Control-plane package change. A new Application must not be
reported ready until its APIM subscription is usable on the data plane; an ARM `active` state
alone is insufficient. Do not rotate existing Application keys as an upgrade workaround.

### Deploy v1.1 from zero

Follow [Prerequisites](#prerequisites), [Prepare public parameters](#prepare-public-parameters),
[Preview](#preview) and [Deploy](#deploy) below with new state and a unique resource prefix.
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

### User settings database upgrade

For the permission-management upgrade, apply pending `018_permission_management` using
the same `backend.migrate` entry point before the candidate API starts. Preserve 001-017
checksums and business tables; 018 adds only policy/audit and the effective-appointment
projection. Stage matching API/Telemetry/Control-plane core manifests. Record complete
settings, package hashes and a restorable database backup; pause workers during deployment
and restore their original states/settings after readback. Entra sync and gateway identity
projection switches must retain their current values.

Menu/row authorization is a security behavior change, including ordinary-user self scope.
Do not expose Members through an older package that lacks this authorization after rollback.
Retain additive 018 schema and audit, restore only a compatible secured runtime or hold
the API in maintenance. Database restore is a separate recovery operation, not a package
rollback. See [permission design](../plan/permission-management/design.md).

The user-settings API requires `migrations/011_user_settings_profile.up.sql`. It adds
`app_user_avatar` for private normalized avatars and `account_security_event` for account
change auditing and password-verification throttling. It does not rewrite existing accounts,
sessions, budgets or usage, and needs no infrastructure changes or new Graph permissions.
This migration's table definitions do not depend on `009_assistant_api_format` or
`010_models_dev_pricing`, but deploying the current candidate still requires its complete
pending chain. `011_gateway_targets` is a separate full version, not a substitute.

For a fresh deployment, the existing API staging step includes all numbered migrations,
including `011_user_settings_profile`. App Service runs migration, initial-Owner bootstrap and
API startup in that order with `&&`; migration failure prevents bootstrap and API startup. No new Bicep
parameter, Owner field, Graph scope, Blob Storage resource or deployment mode is required.
The explicit pre-deployment migration procedure below is for an existing installation.

No second SQL upgrade script is required: clean installs and existing installations use
the same numbered migration through `backend.migrate`. If `011_user_settings_profile` is already
recorded with the matching checksum, leave it unchanged and let the runner skip it. A later schema change
needs a new numbered migration; do not add a second script that recreates these tables.

Use the installation's actual backup capabilities. Azure PostgreSQL Burstable does not
support customer on-demand backups; do not change its SKU or network rules just to run an
upgrade. Verify existing automatic backups and recovery retention, and use a compatible
`pg_dump --format=custom` in the authorized database environment when a fresh logical
backup is needed. Encrypt the backup, restrict its permissions, and retain its recovery key
outside Git. `pg_restore --list` validates the archive catalog, not a successful restore;
test restoration separately against an explicitly authorized isolated database.
Deployment-state secrets and installation-specific SCM/backup helpers remain private.

1. Back up the existing database and record its `schema_migration` versions and checksums.
   Use the installation's existing secret configuration and authorized migration environment;
   do not put `DATABASE_URL` or credentials into Git or the command line.
2. Before deploying the new API, apply pending migrations through the existing runner:

   ```bash
   uv run python -m backend.migrate
   ```

   The runner applies each pending file transactionally and verifies existing checksums.
   Rerunning it skips applied files; do not run the SQL directly or edit an applied file.
   The API startup migration check does not replace this pre-deployment step. Run it in
   the existing authorized database environment without exposing credentials or temporarily
   opening the database firewall.
3. Confirm `011_user_settings_profile` appears once in `schema_migration`, both new tables
   exist, and earlier checksums and account/budget/usage data are unchanged. Deploy the
   matching API and frontend packages through the existing deployment process.
4. Verify password-account display-name and avatar updates, wrong-password rejection,
   password replacement followed by reauthentication, and read-only Microsoft profiles.
   A successful password change revokes all existing Turnstile sessions for that account.

For an application rollback, restore the previous API/frontend packages and retain the
additive tables and migration ledger entry. Do not drop audit or avatar data or rewrite
migration history. Rolling back packages does not restore changed passwords or revoked
sessions; database restoration requires a separately reviewed recovery procedure.

### Immediate model-access upgrade

The API also needs network reachability to the ledger. With the default private ledger,
use [model-access-network-upgrade.bicep](../infra/model-access-network-upgrade.bicep) to add
an unused `snet-api` subnet delegated to `Microsoft.Web/serverFarms` and integrate the API
with the platform VNet. Review address-space overlap, private DNS links and the ledger private
endpoint first. The default subnet is `10.42.3.64/27`. This does not open the Storage firewall.

Regional VNet integration requires B1 or higher; F1 is not sufficient. The optional
`restoreFreePlanName` parameter restores one explicitly selected existing Free plan to B1
and incurs Basic-plan charges. Leave it empty to preserve a suitable existing plan. Verify
all apps hosted by that plan and review its what-if before opting in. Apply the network
template with the same explicit subscription and API resource group, before the settings
upgrade below. New installations already include this API subnet and VNet integration.

Use [model-access-upgrade.bicep](../infra/model-access-upgrade.bicep) only for existing API
Web Apps with system-assigned managed identities and an existing ledger. It supports multiple
API names in one resource group and a ledger in another resource group in the same subscription.
Repeat for any other API resource group. Verify the endpoint/table against **both** APIM's
current policy and Telemetry's settings before applying; a successful grant on the wrong table
does not synchronize permissions.

Create a private parameter file at `.turnstile/model-access-upgrade.parameters.json`:

```json
{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
   "apiNames": { "value": ["<existing-api-name>"] },
   "ledgerResourceGroupName": { "value": "<existing-ledger-resource-group>" },
   "ledgerStorageAccountName": { "value": "<existing-ledger-account>" },
   "ledgerTableName": { "value": "<existing-ledger-table>" }
  }
}
```

Use an identity allowed to update the selected API settings and create role assignments on
the exact Table. If the groups differ, it also needs deployment permission in the ledger group.
Review the resource IDs and reject Delete, storage creation or any unexpected write:

```bash
export SUBSCRIPTION_ID='<subscription-id>'
export API_RESOURCE_GROUP='<existing-api-resource-group>'
uv run python - <<'PY'
import json
import os
import subprocess
import tempfile
from pathlib import Path

subscription = os.environ["SUBSCRIPTION_ID"]
group = os.environ["API_RESOURCE_GROUP"]
document = json.loads(Path(".turnstile/model-access-upgrade.parameters.json").read_text())

def read_settings(api_name):
  resource = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.Web/sites/{api_name}"
  result = subprocess.run([
    "az", "rest", "--subscription", subscription, "--method", "post",
    "--url", f"https://management.azure.com{resource}/config/appsettings/list?api-version=2024-11-01",
    "--headers", "Accept=application/json", "--output", "json",
  ], check=True, capture_output=True)
  return json.loads(result.stdout)["properties"]

snapshots = {name: read_settings(name) for name in document["parameters"]["apiNames"]["value"]}
document["parameters"]["currentApiSettings"] = {"value": snapshots}
with tempfile.TemporaryDirectory(prefix="model-access-", dir=".turnstile") as directory:
  parameter_file = Path(directory) / "parameters.json"
  with os.fdopen(os.open(parameter_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
    json.dump(document, stream)
  arguments = [
    "--subscription", subscription, "--resource-group", group,
    "--name", "model-access-v1-1", "--template-file", "infra/model-access-upgrade.bicep",
    "--parameters", "@" + str(parameter_file), "--mode", "Incremental",
  ]
  subprocess.run(["az", "deployment", "group", "what-if", *arguments,
          "--result-format", "ResourceIdOnly"], check=True)
  with open("/dev/tty", encoding="utf-8") as terminal:
    print("After reviewing exact API/Table changes, type upgrade: ", end="", flush=True)
    confirmation = terminal.readline().strip()
  if confirmation != "upgrade":
    raise SystemExit("Cancelled without deployment")
  if any(read_settings(name) != settings for name, settings in snapshots.items()):
    raise SystemExit("App Settings changed after preview; repeat from a fresh snapshot")
  subprocess.run(["az", "deployment", "group", "create", *arguments,
          "--output", "none"], check=True)
PY
```

The caller reads current App Settings immediately before preview and supplies them through
the `currentApiSettings` secure object, keyed by API name. The temporary parameter file has
mode `0600`, is removed on exit, and must not be copied into Git or validation evidence.
Run this example in an interactive macOS/Linux terminal; confirmation is read from `/dev/tty`
because the here-document already supplies Python's standard input. Without a terminal, it
stops before deployment.
Do not pass settings as command-line values or handcraft an empty snapshot. Reading the same
`appsettings` resource inside its own ARM write creates a circular dependency.
The template merges only the three ledger values. It does not export secrets, change other
settings, create storage, or modify networks.
Keep configuration writers excluded until readback completes. The Table role uses the same
deterministic name as a new deployment, avoiding duplicate grants on rerun. Its scope is
`.../storageAccounts/<account>/tableServices/default/tables/<table>`, never the storage
account, resource group or subscription. Configuration waits for the role deployment, but
Azure RBAC propagation must still be checked with the API identity before admitting traffic.

After restoring service, save a model grant, replace the allowed set, then revoke all models
for an authorized test person. Verify the affected ledger `M` row contains the latest full UUID
and alias set immediately after each save; deny-all must retain `Configured=true` and
`Models="||"`. Other users and budgets must remain unchanged. Check a direct APIM client:
Invocation Test also reads PostgreSQL, so its success alone does not prove Desktop synchronization.

PostgreSQL commits before immediate projection. A ledger write failure preserves the saved
policy and emits `Model access saved but not projected`; the Telemetry timer retries from
canonical state. Do not report that warning as synchronization success, disable the timer,
or hide it with a frontend delay. A concurrent stale writer or an inaccessible Table still
requires diagnosis; local tests are not proof of Azure propagation or live concurrency.

## Prerequisites

- Azure CLI with an authenticated subscription context
- `uv` and Python 3.11 or newer
- Node.js and npm versions accepted by `frontend/package.json`
- Permission to create the resource group and included resources
- Permission to create role assignments; use Owner, or Contributor plus User Access Administrator, at the target scope

Docker is not required. The observer image is built remotely by Azure Container Registry.

## Prepare public parameters

Copy the example into the ignored `.turnstile` directory:

```bash
mkdir -p .turnstile
cp infra/main.parameters.example.json .turnstile/main.parameters.json
```

Set the resource prefix, resource group, Azure and PostgreSQL regions, APIM publisher email, and `bootstrapOwnerEmail`. New deployments provision a capacity-one `StandardV2` APIM by default. `entraClientId` and `entraAllowedEmailDomains` are optional; password Owner login works without Entra.

To run a clean end-to-end deployment without provisioning another APIM service, set all four `existingApimName`, `existingApimResourceGroupName`, `existingApimPrincipalId`, and `existingApimGatewayUrl` values. PostgreSQL and every other platform resource are still created from scratch. The deployment derives environment-unique API, path, product, subscription, logger, diagnostic-setting, and observer Named Value identifiers from `resourcePrefix`, so the shared APIM configuration does not overwrite another Turnstile environment.

Do not add passwords, keys, hashes, or connection strings to this file. The deployment command rejects secure parameter names in the public file.

Interactive deployment prompts for the initial Owner's chosen password. For automation, copy `infra/owner.credentials.example.json` to `.turnstile/owner.credentials.json`, use the same email as `bootstrapOwnerEmail`, choose the password, run `chmod 600`, and pass `--owner-credentials .turnstile/owner.credentials.json`. The plaintext is read only in process memory; state and ARM receive only its scrypt hash.

## Preview

```bash
az login
uv sync --frozen
uv run python -m scripts.deploy plan \
  --subscription <subscription-id> \
  --parameters .turnstile/main.parameters.json
```

The first preview asks for the initial Owner password twice and stores only its scrypt hash. It also creates independent random platform secrets in `.turnstile/deployments/<resource-group>.json` with mode `0600`. Repeated previews and deployments reuse that state.

Before creating deployment state, the command verifies that the selected region supports Flex Consumption, that `Microsoft.App` is registered, and that the selected PostgreSQL region supports PostgreSQL 16 with the configured SKU and availability zone. If either service is unavailable, choose another location and run the preview again.

Review all creates, updates, unsupported previews, role assignments, and network settings. The script refuses every what-if containing a Delete change.

## Deploy

```bash
uv run python -m scripts.deploy deploy \
  --subscription <subscription-id> \
  --parameters .turnstile/main.parameters.json
```

The command runs these phases in order:

1. Re-run the platform what-if and require the exact confirmation `deploy`.
2. Provision the platform with publication and release workers disabled.
3. Build the frontend with the configured public Entra client ID.
4. Install exact Linux x86-64/Python 3.11 dependencies and create deterministic API, telemetry Function, and control-plane Function ZIPs.
5. Deploy the API and Function packages with short-lived Microsoft Entra authentication. Flex Functions use One Deploy, and SCM basic publishing credentials remain disabled.
6. Provision a Basic ACR and observer App Service on its own Premium v3 plan, build the image remotely, and restart the observer. The default is one `P0v3` worker; set `observerPlanSkuName` and `observerPlanWorkerCount` in the deployment parameters when additional compute or horizontal capacity is required.
7. Run another no-delete what-if and enable the workers with the observer URL and APIM Named Value.
8. Verify API health, Function indexing, and real password Owner login.

Generated packages and deployment state remain under `.turnstile/deployments`. The script prints and stores only non-secret Azure outputs.

Fresh installations include the fixed `POST /images/generations` operation with a default
deny policy. Creating the operation does not publish an image model, enable image generation
or authorize a provider call. Model publications inherit the operation into their candidate
revisions and replace only its model-specific policy. The Control-plane publisher does not
need `Microsoft.ApiManagement/service/apis/operations/write`.

## Initial Owner

The deployment command reads the password interactively and never sends plaintext to ARM. It deploys only the scrypt hash. API startup applies the schema and atomically creates the Owner only when `app_user` is empty; restarts and reruns do not reset the account. Enabled Owner accounts are immediately listed in People under the default AI Platform department so the bootstrap Owner can assign model access before generating gateway traffic. Member department placement still comes from attributed gateway usage.

Password users can change their own password in User settings. Operators can also use
the existing account command from an authorized application execution context:

```bash
python -m backend.accounts <owner-email> --role owner
```

After changing the Owner password, use the current password for deployment verification.
Update any private `--owner-credentials` file used by automation, retaining mode `0600`.
Do not replace the deployment state, platform keys or stored bootstrap hash: that hash
creates an Owner only while the account table is empty. Existing accounts and their
display names/passwords remain unchanged by restarts and repeated bootstrap attempts.

## Customer Foundry onboarding

The deployment output includes the APIM managed-identity principal ID. Turnstile does not grant it access to customer resources and does not create a Foundry project.

Use the authenticated web console to add the existing Foundry Project Endpoint and model deployment. The user does not enter model capabilities: Turnstile derives them from the provider protocol, including `chat`, `tools`, and `streaming` for OpenAI-family Foundry deployments. If the candidate probe lacks access, the publication pauses and displays the exact principal, resource endpoint, and `Cognitive Services User` role. An Azure user authorized on that Foundry account grants the role, then resumes the publication in the same dialog. Model access remains explicit; assign the new model to the intended people before using Invocation Test.

Creating connections or models through direct API calls does not count as frontend E2E evidence.

### Managed identity authorization

Enabling a managed identity and granting it Foundry access are separate operations. The API,
publisher Function and APIM have different identities. APIM authenticates the upstream model
request; use the **APIM principal ID returned by the pending publication**, not the API or
Function principal. The default project-endpoint profile uses the `https://ai.azure.com` audience
and declares `Cognitive Services User` (`a97b65f3-24c7-4388-baec-2e87135dc908`) on the existing
Foundry account. `Cognitive Services OpenAI User`, individual Responses actions and
`Foundry Agent Consumer` are not interchangeable authorization profiles for that route.

After account-owner approval, [foundry-managed-identity-access.bicep](../infra/foundry-managed-identity-access.bicep)
can apply that declared role. Supply the existing account's `resourceGroupName`, `accountName`
and the publication's `apimPrincipalId`. Run subscription-scope what-if with an explicit
subscription and deployment location, review that it creates only the intended account-scoped
role assignment, then apply the same template and parameters. This is an explicit customer
authorization step, never part of the default Turnstile deployment. The built-in role includes
account key access and broad Cognitive Services data access; review those permissions before
granting it. The template does not read or output keys, create a Foundry account, or change
networking or local authentication.

Read back the assignment and allow RBAC propagation before resuming the **same** publication.
Require its real Chat Completions, Responses and compact probes to pass and the publication to
reach `active`. A Running Function, successful role deployment, or a single Chat response is
not proof of completed model publication. Do not infer a particular missing permission from
a generic 403 alone.

### API-key alternative

For an explicitly authorized API-key connection, supply the Project Endpoint and a matching
same-account Inference Endpoint ending in `/openai/v1`; provide the key at the first model
publication. Turnstile transfers the encrypted one-time credential to a secret APIM Named Value
and does not return it in the registry. Never put it in a deployment parameter file or log.
The Foundry account must already permit local-key authentication. Do not enable it automatically
as a fallback. Connection endpoint and authentication identity are immutable: use normal
connection/publication operations to replace a failed MI configuration, retaining the original
publication history rather than rewriting its snapshot or bypassing its probes.

### Image and evidence upgrade boundary

Apply pending migrations `004_apim_usage_identity_guard`, `005_billable_request_lifecycle` and `006_versioned_budget_evidence` through the same authorized migration entry point before running the new packages. Do not edit or replay the earlier migrations. The upgrade adds guarded APIM attempt identity, a durable billable-request journal and versioned budget evidence without rewriting historical usage. Drain old consumers before introducing the new identity guard. PostgreSQL concurrency and upgrade safety require the database checks in [Testing](testing.md).

The API and Control-plane packages both require the pinned Pillow dependency for full image validation. The Control-plane artifact includes the public canonical parent policy at `policies/foundry-finops-policy.xml`; publication and image rollback read it through `CONTROL_PLANE_PARENT_POLICY_PATH`. The publisher verifies the live policy and its readback, and refuses incompatible image policies instead of transforming an unknown live template. Use the APIM infrastructure upgrade below before publishing image models on an older installation; do not rerun the bootstrap template against customer routing.

`IMAGE_GENERATION_ENABLED` defaults to false in this fork; the Bicep parameter
`imageGenerationEnabled` also defaults to false. Keep `IMAGE_GENERATION_ENABLED=false` during
the code and migration rollout. Enable it only after verifying the image infrastructure,
compatible packages and provider prerequisites described in [configuration](configuration.md).
Keep the evidence-v2 cutoff unset during rollout, or preserve an existing cutoff unchanged.
Neither migration nor the image flag authorizes paid publication probes or scheduling the future
evidence cutoff; these remain separate operational decisions. This upgrade does not provision a
Foundry account or model, modify an upstream deployment, or grant new provider roles.

### Incremental APIM infrastructure upgrade

Existing installations skip APIM bootstrap when saved deployment outputs exist. Adding a
new operation to the fresh-install template therefore does not upgrade those installations.
The versioned `images-v2` infrastructure upgrade handles the fixed image route and the
image-aware parent contract independently of package deployment and database migrations.

Use the original public parameter file, secret state and matching `.outputs.json` on a
POSIX deployment host (Linux or macOS). Do not generate replacement state or copy another
installation's outputs. The exact APIM, API and maintenance applications are read from
those outputs; a missing API is an error, not permission to initialize it.

```bash
uv run python -m scripts.deploy plan-upgrade \
  --subscription <subscription-id> \
  --parameters .turnstile/main.parameters.json \
  --state .turnstile/deployments/<resource-group>.json
```

The preview snapshots the current revision, API configuration, every operation and every
operation policy. It creates a private plan under `<state-name>.upgrades/images-v2/`, beside
the original state. Plans can contain customer policy values; keep that directory private
and outside Git. The plan binds the exact targets and upgrade template/code hashes.
Preview performs ARM reads and what-if only, without changing platform secrets or resources.

Supported pre-image public parent policies are transformed structurally, preserving their
configured authentication, ledger, telemetry and text values. The transformed policy must
match the current reviewed public contract. An already compatible parent is unchanged.
An existing compatible image operation and its policy are preserved; a missing operation
or missing policy is initialized fail-closed. Unknown/custom parent policies, ambiguous
components and conflicting operation definitions stop automatic migration for explicit
review. No wholesale replacement of a customer's live parent is attempted.

Before applying, schedule a maintenance window and drain pending publications, rollback,
retention and Application provisioning work. Stop the API and Control-plane applications
so no new control-plane work can be submitted or processed. The upgrade verifies both
applications are `Stopped`; draining queued work and excluding other administrators or
deployment systems from this API remain operational prerequisites. Existing direct APIM
text traffic can continue, but the Turnstile web/API application is unavailable while stopped.

```bash
uv run python -m scripts.deploy upgrade \
  --subscription <subscription-id> \
  --parameters .turnstile/main.parameters.json \
  --state .turnstile/deployments/<resource-group>.json
```

The command confirms the saved plan, repeats a no-delete, exact-resource what-if, and uses
the deployment identity, not the Control-plane identity:

1. Clone the original API revision into a deterministic non-current upgrade revision.
2. Add only missing image infrastructure and update the reviewed parent policy in that candidate.
3. Read back the complete candidate; all original operation definitions and policies must remain unchanged.
4. Recheck the current revision and maintenance state, preview the exact release resource,
   and activate the verified candidate.
5. Read back the current revision before recording completion. Keep the original revision
   and all database publication/release history, models, credentials and subscriptions.

The upgrade does not edit shared roles, create resource groups or APIM services, restart
applications, enable images, run inference, set the evidence cutoff, or apply database
migrations. Continue the separately reviewed package/migration rollout and restore service
after the infrastructure upgrade. Ordinary `plan` and `deploy` reruns now reject a pending
APIM infrastructure upgrade before changing secrets or packages.

Retry the same `upgrade` command after a resolved interruption. The private journal and
deterministic revision identify owned work. A complete candidate is not rebuilt; a lost
promotion response is resolved by readback without another promotion. Nonterminal ARM
operations, unrelated revisions, changed live configuration or an unexpected candidate
policy stop recovery. Do not clear the journal or edit a plan to force acceptance.

During the same maintenance window, explicit infrastructure rollback is available:

```bash
uv run python -m scripts.deploy rollback-upgrade \
  --subscription <subscription-id> \
  --parameters .turnstile/main.parameters.json \
  --state .turnstile/deployments/<resource-group>.json
```

Rollback requires the recorded promotion and unchanged original/candidate snapshots. It
selects the retained original revision without deleting either revision or changing data.
After subsequent model publications or customer configuration changes, automatic rollback
is refused; review that later state instead of discarding it. Restoring old infrastructure
does not roll back application packages or migrations and is incompatible with enabling
new image functionality. Never clear or reschedule an already effective evidence-v2 cutoff.

## Verify

1. Confirm the infrastructure deployment succeeded.
2. Confirm `/health` returns HTTP 200.
3. Confirm the served frontend asset belongs to the deployed package.
4. Confirm the migration ledger contains each migration listed in [Testing](testing.md) once, with unchanged checksums for previously applied files.
5. Confirm the bootstrapped Owner can sign in with a password.
6. Verify managed-identity role assignments at their intended resource scopes.
7. Complete the real workflow in [E2E Validation](e2e-validation.md).

## Reruns and recovery

The command is idempotent for the same parameter and state files. Keep the state file available: generating replacement platform keys during a rerun can invalidate database credentials, encrypted provider credentials, APIM subscriptions, and observer authentication.

If a phase fails, correct the reported cause and run the same command again. Every infrastructure phase repeats what-if and still refuses Delete changes. Use `--yes` only in controlled automation after separately reviewing `scripts.deploy plan`; it never bypasses the no-delete gate.

## Rollback

Retain the exact previously mounted package and its SHA-256 before each application deployment. A deployment record alone is insufficient; verify the mounted asset and health after rollback.
