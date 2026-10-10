# Organization Management

## Runtime Boundaries

The APIM workspace maintains organizations, departments, teams and personnel in PostgreSQL.
Contact email and display-name edits do not change governance IDs, prior-period budget parents,
historical usage or model policies. Account binding is explicit and one-to-one. Department
administrators are scoped Members, not a third global account role.

The GitHub Copilot workspace hides this menu and search entry. Its enterprise team and cost-center
features retain their own data source. Switching workspaces does not delete directory data or
stop background jobs.

Fixed sidebar destinations are derived from the selected workspace and the authenticated
menu profile, not asynchronously inserted after a directory-capabilities request. Capability
reads still govern organization-page data and actions; backend scope checks remain authoritative.
The dynamic report group loads after the fixed menu groups so new reports do not shift their
positions. No persisted menu or cached permission snapshot is used to bypass session checking.

Readiness requires schemas 012-017, the configured directory authority and compatible packages.
An HTTP health response alone does not prove directory, Graph or gateway readiness.

`/api/v1/enterprise/entities` remains the active assignment/invocation projection.
`/api/v1/enterprise/query-entities` is a separate, scoped, query-only projection for an aware
window up to 366 days. It retains original unknown/archived IDs and latest in-window
attribution without editing historical requests. The query client does not rewrite legacy
user IDs into today's governance identities.

## Usage Query Filters

The five APIM usage pages share multi-select organization, department, agent and personnel
dropdowns. A repeated query key is a union within that dimension; different dimensions and
the authenticated department scope intersect. Do not comma-join IDs or aggregate multiple
requests in the browser. Time range remains a single choice.

The UI exposes only Enabled and Archived, defaults to `directory_status=active`, and maps
All to the union of active and archived. Historical attribution and unattributed are not
management states and have no UI options; stale selections normalize to active.
The API keeps its existing all-status
behavior when that parameter is omitted. Status filters apply to both facets and aggregate,
trend, anomaly and request queries. Active means an active organization/department pair with
no archived managed person; archived matches retained managed scopes/personnel. Unknown old
scope pairs are historical attribution, and `unattributed` organization/department IDs are
a separate unattributed category, not fabricated archived entities. Current ancestors remain
available when selecting their archived descendants.

Current directory names win over historical labels. `parent_ids` retains every in-window
parent edge and the current parent; the original request identities, names, amounts and
primary billing department are not rewritten. Query-only historical people remain queryable
under valid current organization/department attribution, but never become login/invocation
identities. The selected status does not establish a menu, APIM or model permission.

The 2026-10-10 read-only diagnosis found 81 publication-probe requests attributed to
`system-gateway-publication`, two protocol-validation requests attributed to
`turnstile-internal`, and one request with unattributed organization in the 90-day/124-request
sample. The publisher intentionally supplies those probe identities. The old query catalog
merged them into normal-looking organization options without status/source metadata and
allowed historical names to overwrite current labels. Do not delete these actual invocation
and billing facts. The active default hides their non-directory scopes in both options and
results; direct API queries retain historical/unattributed diagnostic access. UI All does
not restore those non-directory categories.
Active custom departments, including test-named departments, remain real configured records;
names alone must never trigger automatic archive or deletion.

No budget storage/enforcement, migration, ingestion domain or APIM policy changes are required.
Global cache-read metrics cannot be assigned to a directory status intersection; only the
unfiltered all-status query may merge the existing global correction. Facet queries exclude
their own dimension selections to keep sibling options available, and query caches normalize
selection ordering and include status. Failed reads do not reset selections to an unfiltered view.

## Archive Visibility

Organizations, departments, teams and personnel use Enabled and Archived in management.
Existing `inactive` rows remain readable and display as Archived; the archived personnel
filter includes both stored values. Legacy clients may still send `inactive`, but write DTOs
normalize it to `archived`. No historical data or applied migration is rewritten.
Login-account disabling and external-source disabling remain separate controls.

The directory's Hide archived switch is enabled on each page entry. It hides archived
organizations and their subtrees, archived departments and their subtrees, and archived teams.
At each sibling level, enabled nodes precede archived nodes; names use the current locale's
natural ascending order, with stable IDs breaking equal-name ties. Sorting does not mutate
query caches, memberships or the selected node.
Turn it off to inspect or restore records. A hidden selected team falls back to an active
parent department, a hidden department to its organization, and a hidden organization to the
first active organization or an empty state. Unknown/inaccessible IDs still report an error.
The URL is reconciled after hiding or archiving, and unsaved changes require confirmation.
This is navigation filtering, not an authorization boundary or an APIM revocation guarantee.

Archiving an organization, department or team does not require emptying its members,
children, projects, applications or budgets. It changes only the selected node and its
normal revision/audit metadata; personnel status, login accounts, memberships, child status,
billing facts and model configuration are retained. Restoring a node also does not change
those records. Active assignment/admission projections and scoped administrator effectiveness
still respect archived scopes; personnel status and a scope's effective access are distinct.
Pending directory synchronization jobs targeting the organization, unit, its descendant
team mappings or an all-users default department must finish or be cancelled before archiving.
Permissions, optimistic revisions and immutable hierarchy checks remain enforced.

## Budget Directory Navigation

The APIM budget hierarchy defaults to collapsed organizations and Hide archived enabled.
Organization/department rows sort enabled first, then by locale-aware natural names; directory
archive state is separate from the existing budget risk status. Hiding rows does not remove
stored budgets or alter overview, risk, history, quota constraints or enforcement totals.

Click an organization or department name to select the personnel scope. Organization selection
shows all people in its monthly primary-budget departments, including retained archived department
records; department selection shows its monthly primary-budget people. The team selector contains
only teams under that selected organization/department. All teams includes people without a team.
Changing the hierarchy scope resets the team and personnel selection. Selecting a team intersects
current directory memberships with the original monthly budget personnel; it does not move anyone's
primary department, create team budgets or reinterpret historical accounting.

The UI reads every required page from the existing department budget and directory membership
endpoints, with bounded concurrent department requests, before filtering and paginating locally.
Only already-authorized departments are read. Installations/users without directory-management
read access retain the scoped current enterprise catalog fallback and do not call protected directory
lists. Query errors are not shown as fabricated zero totals.

All budget/model write endpoints, payload definitions, allocation validation and database storage
remain unchanged. Organization views support individual edits and same-primary-department batches;
mixed-department selections cannot submit a batch. Team selections use explicit governance IDs,
so the existing all-matching operation cannot accidentally include people outside the team.
The existing 500-ID limit applies; full unfiltered single-department result selections can still
use the existing server all-matching mode. No automatic cross-department partial writes are introduced.
Any future change to budget controls, write contracts or storage requires explicit approval.

## Menu Groups, Not Job Titles

Personnel forms/lists display a local menu permission group instead of Job Title.
Unassigned people default to Ordinary User. Organization Administrator, Department Administrator
and Team Administrator are navigation presets, not Owner, APIM roles or department data grants.
Only Owner may assign or change them; an explicit enabled account/person link makes them effective.
Multiple administrator groups can coexist and are displayed as read-only tags in personnel
forms/lists. The personnel form has no group assignment controls and never submits menu groups
when creating or editing a person. Administrator maintenance remains in its separate workflow;
menus are the union of assigned groups. No administrator assignment means Ordinary User.
Existing job-title data is retained for old clients/source imports but never authorizes access.
Administrator configuration is a single workflow at organization, department and team nodes.
The matching appointment produces menu-group labels and menu entitlements; department appointments
also maintain the existing department-capability projection. Team appointments require actual team
membership and never reuse their parent department's list. Personnel forms remain read-only.

Ordinary User has personal settings and APIM invocation. Team Administrator adds analysis,
assistant/report, model and organization menus; Department Administrator adds governance/budget
and Copilot cost-center/unassigned menus; Organization Administrator adds system/platform
and Copilot enterprise-team menus. Source applicability and existing API permissions still apply.
A visible page may therefore show no data permission until separately authorized.
Owner retains all applicable menus. Groups cannot grant model invocation, budget allocation,
gateway publication or broader data scope.

Login and `/auth/me` return the local group and allowed page IDs. Source changes, direct routes,
browser history, search, prefetching and floating assistant use the same whitelist.
Profile refresh updates an existing session without requiring sign-out.
Administrator-only changes do not create directory gateway-projection work.

### Scoped Administrator Compatibility

Personnel tables and their read-only editor display `scope_menu_permission_groups` for the
selected node, not the global group union. Matching requires both `scope_kind` and stable
`scope_id`: a Test Team administrator is an ordinary member in Team Example unless separately
appointed there. An organization/department appointment is not silently inherited as a team
appointment. `scope_menu_administrator_scopes` identifies the exact matching assignments;
scope names are display/tooltip data, never authorization keys.

Login/current-profile responses retain `menu_permission_groups`/`menu_permissions` as global
navigation availability and expose effective `menu_administrator_scopes` with kind, ID,
organization ID and display name. Use exact scope matching for resource-context menus;
never authorize a business operation by `team_admin`/`department_admin` text or menu visibility.
Legacy unscoped person group fields remain navigation compatibility only and do not invent
appointments. Backend `menu_permissions_for_scope` and frontend `isMenuAdministrator` use
exact appointments; Owner bypass is explicit and independent.

Department capabilities are checked from a per-department map, not the union across grants.
Current-node edit controls use that same map. Appointment-only organization/team roles do not
grant Owner operations, department data capabilities, budgets or model/APIM admission.
This fix requires no new schema or appointment data rewrite.

`015_scoped_menu_administrators` adds scoped appointments and a validated effective view.
Personnel and current-profile group arrays are server-derived from those appointments, with
pre-existing explicit legacy menu groups retained for compatibility. Owner retains all menus
but its profile now also reports actual appointments. The legacy department write endpoint
delegates to the same appointment service, synchronizing existing data-scope grants atomically.
No separate Data Authorization editor is required.

Import only current, enabled, linked legacy department administrators whose department membership
is valid. Do not derive organization administrators from Owner or team administrators from team
membership. The pre-015 team panel accidentally displayed its parent department's assignments;
that selection is not proof of a team appointment. Use the new team Administrator tab explicitly.
Apply 015 after backups and isolated verification; preserve 012-014 checksums and all historical
business records. This compatibility import changes application navigation, not APIM admission.
No live Entra consent or model permissions are granted.

### Create People And Add Members

Only the organization node creates people. Name, email, login password (12-256 characters)
and primary department are required. The department selector contains enabled departments in
that organization and has no automatic default. Owner creation atomically stores the person,
Member password account, explicit account link and primary membership. An existing email is a
conflict, never a password reset or an implicit account merge. Password hashing uses the same
scrypt format as login; raw passwords never enter audit, responses or idempotency records.

Department and team nodes use **Add Members**, selecting enabled existing people in their
organization who are not already in that node. Search and pagination stay server-scoped.
Additional department/team membership never changes the unique primary department used by
budgets, model invocation and gateway identity. Non-Owner candidates require permission to
maintain the target unit; this bounded selector does not grant organization-wide personnel
editing or cross-department budget access. Teams have a department parent and cannot contain
nested teams; only a department node exposes Create Team.

`016_organization_members` preserves current organization ownership and cancels pending legacy
transfer plans. `017_department_memberships` stores additional non-billing department edges.
Transfer plans, historical-identity editing, their HTTP routes and monthly transfer execution
are retired. Historical query entities and billing attribution remain intact. Existing records
are not deleted. Apply additive migrations and compatible runtimes before using this workflow;
do not alter already applied migrations, reactivate the legacy directory or rerun backfill.

### Upgrade From The Active Directory

Back up the current database, packages and full settings before a maintenance update.
Apply additive `013_menu_permission_groups`, `014_multiple_menu_permission_groups`
`015_scoped_menu_administrators`, `016_organization_members` and `017_department_memberships`
plus `018_permission_management` through `backend.migrate` before starting the new
runtime trio. Fresh installations use the same migration chain. Unassigned existing people
default to Ordinary User; Owner keeps its independent menus.
014 preserves single assignments from 013 as arrays; the singular API field remains only for
legacy-client compatibility. Do not translate job titles into groups. 015 imports valid existing
department administrator appointments as described above, not arbitrary data grants or team membership.
Do not rerun legacy backfill
or directory activation. Repeat migration to verify checksum/idempotency and check real
Owner/Member profiles, assigned/unassigned navigation, data scopes and existing model policies.
Retain the column and migration ledger on compatible-package rollback; do not restore an older
database and lose new teams/personnel or menu assignments. This update does not deploy APIM policy,
enable Graph sync or complete Microsoft login configuration.

### Configurable Menu And Automatic Data Permissions

Permission Management follows Organization Management under Platform Management. Only Owner
can read or save the four-group menu matrix; existing administrator appointments stay in the
organization directory. Complete saves use compare-and-swap revisions and immutable audit.
Menu access does not delegate budget/model writes, Owner privileges, Graph consent or APIM
invocation authorization.

In database-directory mode ordinary Members now see only themselves. Organization
administrators see their organization, department administrators their effective departments,
and team administrators their effective teams' people. Same-level cross-memberships are
combined; team parent departments are navigation context, not whole-department grants.
Lists, facets, totals, details, budget read projections and assistant tools enforce the same
automatic scope. Saved reports/conversations record versioned department/user scopes in the
existing `directory_scope` column; restricted viewers cannot read old global snapshots.

Shared usage APIs are authorized by their actual consuming pages. Granting Usage Breakdown
alone must authorize its summary and distribution reads without also granting Overview.
Scope enforcement still applies to every query, including malicious or multi-value filters.
See [requirements and review](../plan/permission-management/review.md).

Migration 018 adds the default role policy/audit and updates effective appointments without
rewriting budgets, telemetry or primary billing membership. Previously global Member reads
are intentionally tightened. Do not roll back to pre-permission packages with Member access
enabled: retain additive schema and audit, use a secured compatible runtime or maintenance
mode. Database restoration is separate and is not a routine package rollback.

## Empty Installation

1. Use a dedicated new environment and private deployment state. Review the normal what-if.
2. Explicitly opt in with `directoryEmptyInitializationAllowed: true`. The deployment runner
   supplies `directoryExpectedCoreDigest`; sync and projection remain disabled.
3. Keep the standard `backend.migrate`, `backend.bootstrap`, `backend.api` startup sequence.
   Bootstrap creates only the first Owner; initialization creates no customer organizations,
   departments, users, budgets, projects or model grants.
4. Sign in as the Owner, create the real hierarchy and explicitly link existing accounts.
   Configure budgets and models through their existing Owner workflows.
5. Restart and repeat initialization only to validate idempotency, not to reset business data.

## Existing Installation

Keep the source `legacy` and new write/worker flags disabled until the approved cutover.
Do not rerun APIM bootstrap or change the evidence cutoff, encryption key, subscription keys,
ledger partitions or Event Hub checkpoint.

1. Record application states, exact mounted packages and SHA-256, assets, app settings,
   migration checksums, APIM revision and billing/model-policy baselines. Back up PostgreSQL
   and private deployment state, and verify the backup can be read/restored in an approved
   isolated environment. Burstable PostgreSQL does not imply an on-demand backup is available.
2. Stop directory/governance writers for a maintenance window. Apply pending additive migrations
   using `python -m backend.migrate`; never edit 001-011 or apply schema through a second runner.
3. Deploy the same compatible API, Telemetry and Control-plane candidate in legacy mode.
   Each package contains `directory-runtime-manifest.json`; verify target, protocol and
   matching core digest, and confirm all expected Functions index.
4. Plan and approve the real legacy directory plus actual governance references. The plan
   contains personal information and must remain in a 0700 directory as a 0600 file.
5. Apply and verify the saved plan. Durable staging precedes atomic materialization.
   Interrupted materialization rolls back business rows; retrying the same plan resumes
   its staging checkpoint rather than cloning identities or repeating audit.
6. With all three writers stopped, merge precise current settings using
   `infra/directory-settings-upgrade.bicep`. Review a no-Delete what-if limited to the three
   exact appsetting resources. Set the same database source and core digest; keep Graph and
   projection disabled. Read back the complete setting maps and preserve unrelated values.
7. Capture private three-package maintenance evidence, verify and activate by database CAS.
   Restore each application's recorded running/stopped state. Verify real login, source
   normalization, scopes, histories, budgets, models and directory CRUD.

Use the authorized environment's `DATABASE_URL`, never a connection string in command arguments:

```bash
python -m scripts.directory_upgrade plan --output <private-directory>/plan.json
python -m scripts.directory_upgrade apply --plan <private-directory>/plan.json --approve
python -m scripts.directory_upgrade verify --run-id <run-id>
python -m scripts.directory_upgrade activate --run-id <run-id> --approve \
  --runtime-manifest <private-directory>/runtime-evidence.json
```

The runtime evidence is an operator-verified readback, not an application-generated assertion.
It contains `captured_at` (UTC, within 15 minutes), the plan's `instance_digest`,
`protocol_version: 1`, `backup_verified: true`, `maintenance_confirmed: true`, and exactly
three `applications` entries named `api`, `telemetry`, `control-plane`. Each entry records
`target`, `state: Stopped`, the mounted `core_digest`, `configured_source: database`,
the matching `configured_core_digest`, and the exact `package_sha256`. Do not manufacture
evidence from intended settings without verifying the deployed packages and Azure state.

When `uv` wheel downloads fail, the packager's pip fallback requires an existing Python
3.11+ interpreter with pip. Set `TURNSTILE_BUILD_PYTHON` when the default interpreter has
no pip; it installs only into the private staged target and still checks every package
against `uv.lock`. An older host interpreter may incorrectly evaluate dependency markers
for the Linux/Python 3.11 target and is not accepted.

Plans bind the database instance, migration checksums, code digest, legacy source digest and
billing/account baseline. Drift requires new investigation; never edit the stored baseline to
make verification pass. Unknown historical identities remain candidates, not guessed employees.

### Compatible Runtime Updates

After database activation, routine updates must not rerun backfill or activation against a
different code digest. Back up the current compatible package trio, settings and database,
then build all three new packages with an identical core digest. Verify all applied migration
checksums and the isolated backup restore before publication.

For compatible updates after 018 with no new migrations, require all 18 applied checksums
to match the candidate and require both migration runs to return an empty result.
Compare every restored table before and after the isolated no-op migration. Do not replay
the 015-017 backfill assertions or accept an older migration baseline. Production verification
must preserve directory rows, historical billing and the current APIM revision. Read-only
release smoke tests must not save budgets, archive customer nodes or enable Graph workers.

For the 015-017 upgrade, compare all existing personnel fields except the added
`organization_id`, and verify that its backfill matches current primary memberships.
Only legal legacy department appointments may be imported by 015. The control state's
permission revision advances twice; other control fields and gateway-projection versions
remain unchanged. Verify that only scheduled/conflicted transfer plans are cancelled by
016, while all other historical rows, original migration checksums/timestamps and billing
tables remain unchanged. Repeat migration and require an empty result.

Stop the three writers, temporarily disable write triggers (including telemetry Event Hub
consumption) and merge the new
`DIRECTORY_EXPECTED_CORE_DIGEST` into every current setting map. Review a what-if limited to
those appsettings. Publish all three compatible packages, then start the apps with write
triggers still disabled. A stopped Flex app can upload its package but fail trigger sync with
`403 Site Disabled`; successful upload is not Function readiness. Explicitly synchronize
Function triggers, verify indexing and read back actual deployed packages before restoring the
recorded trigger settings and application states. Never leave an old package running with a
new expected digest. A package/configuration failure must restore the matching previous digest,
compatible trio and complete settings; retain the active database directory and additive schema.
Only use a previous runtime with compatible menu/row protection; pre-018 behavior must not
restore global Member reads. If core content is unchanged, API-only updates may retain the
two identical Function packages after verifying their actual file contents and full settings.
Verify real login, directory data, disabled identities, billing baselines and Function indexing
after both rollback and republish. Restoring a database backup is not part of this drill.
Refresh Azure management credentials before long-running deployment readbacks and recovery
steps; a token captured at the start of a maintenance window may expire before rollback.
The ordinary `scripts.deploy` path rejects mixed directory authority or an expected core digest
that differs from the source being published before uploading runtime packages. This guard
does not automate or replace the maintenance procedure above.

Use an isolated, clearly identified organization for deployed write validation. Exercise
person/password creation, required primary department, additional department/team membership,
exact organization/department/team appointments and revocation. Compare all pre-existing
customer rows and billing facts after validation. Revoke test appointments, archive only
the owned test people/units/organization and disable their login accounts; preserve immutable
audit records. A failed test script must still perform cleanup with freshly read revisions.
Loading-state screenshots are not final layout evidence; wait for real candidate and
administrator queries to complete before capturing desktop/mobile and Light/Dark results.

Azure Functions may repack a ZIP before storing the active `released-package.zip`.
Record both uploaded and actual-package SHA-256, then verify archive integrity, the runtime
manifest and every file's SHA-256. An outer ZIP hash difference alone is not a code mismatch;
never waive a changed/missing file or mismatched core digest. Preserve the readback evidence.
Use bounded retries for transient management-network failures and refresh tokens during
long windows. A failed settings what-if/create/readback is not successful publication:
restore matching packages/configuration or retry the same reviewed change, then verify the
complete final settings and function indexing before claiming success.

An SCM command response timeout is not proof that the remote backup/readback failed.
Before replaying the command, read its completed immutable result from the private remote
directory with bounded polling. Require all expected packages and the validated database dump,
then independently download and compare their hashes and package contents. If the completed
result is missing or any content check fails, retain the recovery path; never turn a timeout
into a successful release merely because the apps are running. Record failed attempts and
protective recovery separately from the final verified publication.

## Synchronization And Login

The Control-plane `synchronize_directory` timer is independently disabled by default.
Connections select a tenant/cloud and read-only credential, then map Group object IDs to existing
departments/teams. Direct and transitive memberships are explicit; hidden-membership Groups
require a separately approved permission policy and are currently rejected.

Full and delta jobs produce durable staged previews. Selected Groups use complete member
snapshots even in delta mode because Group deltas alone cannot prove nested membership or
user-profile completeness. Cursors advance only after reviewed application; failed paging,
missing permissions and expired cursors do not remove people. A full preview can recover an
expired delta cursor. Same-email collisions and multi-department membership require resolution.
Missing source members require approval, with an additional confirmation above 20% reduction.
Source-disabled and manually disabled people remain in history. Manual disable overrides
are never cleared by synchronization; source status remains authoritative for source disables.
Previews expire after 30 minutes and must be regenerated before application. The applying worker
rechecks the approving account's enabled Owner role. Source `last_seen_at` records the read time,
not a delayed approval time; directory review cannot make an old Graph snapshot look fresh.
Reads process bounded segments and persist encrypted resource/page progress, source rows and
analysis cursors. A recovered lease resumes that job. Missing-source members are staged only
after every source resource is complete. Application uses one set-based transaction, preserving
membership, profile and source-status audit snapshots; a failed application advances no cursor.
The set-based transaction indexes and analyzes its temporary staging joins. A real local
PostgreSQL test applies 100,000 reviewed people with complete per-person membership audit;
this is application-scale evidence, not live Graph throughput or a production latency SLA.

Application read permissions and tenant administrator consent are external prerequisites,
not Bicep side effects. Import does not automatically create login accounts or grant Owner,
department administration, budgets or model access. An explicit source-person association and
optional login-identity authorization are separate audited choices.

Existing email-only Microsoft accounts require explicit `(cloud, tid, oid)` association in
database-directory mode. Configure client ID, redirect URI, domains and optional login tenant
allow-list separately. Directory synchronization is not a completed SSO deployment.

## Transfers And Rollback

### Directory Identity APIM Upgrade

Directory policy upgrades have an independent `directory-identity-v1` plan/journal directory
beside the private deployment state. They reuse the existing ledger endpoint and table.
The upgrade may only clone the selected API revision, replace its parent policy and create
the corresponding infrastructure release. It cannot modify existing operations or initialize
image policies, and rejects Delete or writes to any other resource in what-if.

```bash
python -m scripts.directory_gateway_upgrade plan --subscription <subscription> \
  --parameters <private-parameters> --state <private-state>
python -m scripts.directory_gateway_upgrade prepare --subscription <subscription> \
  --parameters <private-parameters> --state <private-state> --yes
python -m scripts.directory_gateway_upgrade promote --subscription <subscription> \
  --parameters <private-parameters> --state <private-state> --yes \
  --candidate-evidence <private-identity-probes>
python -m scripts.directory_gateway_upgrade rollback --subscription <subscription> \
  --parameters <private-parameters> --state <private-state> --yes
```

Drain publication/rollback work and stop API/Control-plane for the maintenance window.
If source code changes after a failed prepare, do not delete or overwrite the bound plan,
journal or failed candidate. Use a new `--plan-id <reviewed-run-name>` on `plan`, then use
that same ID on `prepare`, `promote` and `rollback`. Each run has a separate private evidence
directory; all runs share the environment lock and still verify the current source revision.
Review the new what-if before any writes. The previous candidate remains noncurrent.
`prepare` verifies the complete candidate but does not promote it. Run real signed employee-token
probes against the candidate with the configured publisher/employee client and scope. Read back
the identity projection and all existing budget partitions. Do not use a CLI user's Graph token
or mocked HTTP responses as evidence of employee gateway admission.

The private 0600 candidate evidence records UTC `captured_at` (within 15 minutes),
`version: directory-identity-v1`, the exact `candidate_fingerprint`,
`real_signed_employee_token: true`, `identity_projection_readback_verified: true`, and
these `cases`: `unmapped_identity: 403`, `disabled_identity: 403`, `expired_mapping: 503`,
`header_spoofing_rejected: true`, `stable_governance_id: true`, `budget_ledger_unchanged: true`.
These values must describe actual probe results. Preparation, ARM policy validation or unit
tests alone cannot manufacture them.

The upgrade leaves application states unchanged. Rollback re-reads the original and current
revisions and refuses a changed source or later operation/policy drift. Preserve the independent
journal; an image upgrade's successful journal is not directory identity evidence.

### Month Boundary

Directory-mode text and image BFF requests seal a separate immutable attribution admission
before provider dispatch. The snapshot shares the directory control/person locking order with
transfer activation. It is not a token measurement and creates no `Q/C/M/R` ledger write.
A due scheduled transfer blocks new BFF admission until activation or conflict resolution;
old metadata cannot pass after a successful move.
Incomplete or cross-boundary prior calls block activation rather than allowing old attribution
to be resealed in the new month. Terminal markers are separate immutable records.

Transfers remain unavailable while `person_admission_mode` is `unverified`. After independently
reading back the deployed API package and complete APIM parent policy, record private 0600 evidence:

```bash
python -m scripts.directory_upgrade configure-admission --approve \
  --admission-evidence <private-directory>/admission-evidence.json
```

Evidence contains UTC `captured_at` (within 15 minutes), current `instance_digest`,
`expected_directory_version`, matching `core_digest`, `api_directory_admission_protocol: 1`,
the exact 64-character `apim_policy_fingerprint`, and `person_admission_mode`.
`bff_only` requires verified `employee_token_enabled: false`. `identity_v1` requires
`directory_identity_policy_protocol: 1`, `identity_gateway_probes_verified: true`, and current
Table projection readback. These are actual readback/probe assertions, not intended configuration.
The gateway identity snapshot's validity is capped by each scheduled transfer's UTC boundary,
so old employee mappings expire before the new assignment can be admitted.
Identity Table snapshots expire within 5 minutes and are refreshed by the independent projection
timer. Failed refresh cannot preserve an enabled mapping indefinitely. The directory policy seals
the employee budget partition and reservation timestamp to `requestStarted`, including calls
whose metadata lookup crosses a UTC month boundary.

The former transfer and historical-identity editing workflows are retired. Migration 016
cancels pending plans; old tables and admission guards are retained for compatibility and
historical evidence, but no UI, HTTP scheduling route or monthly executor remains.
Previous months retain original budget parents and requests retain admission-time attribution.
Merging different historical IDs remains a separately reviewed identity migration.

Stop new writers and independent sync/projection workers before rollback. Keep additive tables,
staging, audit and migration ledger. After new IDs are used, do not switch back to a seed-only
package or legacy authority. Use a verified compatible package trio and preserve recorded
application states. A database restore is a separate approved recovery operation; it is not a
normal package rollback.

Static employee APIM attribution does not automatically follow directory edits or disables.
Treat it as pending alignment until the separately gated dynamic identity policy and projection
have been deployed and verified. Never infer gateway revocation from a successful directory save.
