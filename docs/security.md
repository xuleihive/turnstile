# Security

## Secrets

Never commit `.env`, Azure parameter files with values, publish profiles, certificates, keys, tokens, connection strings, database dumps, or deployment packages. Use Key Vault and managed identity for Azure-hosted workloads.

Provider credentials are write-only through the API, encrypted before persistence, redacted from reads, and excluded from logs. APIM subscription-key reveal responses are Owner-only and use `Cache-Control: no-store`.

## Identity and authorization

- Microsoft Entra tokens are signature-, audience-, issuer-, and domain-validated.
- Application sessions are HTTP-only and secure in production.
- Budget, model, gateway, account-link and organization-level mutations are Owner-only.
- Explicit department grants allow a Member to maintain only that department's personnel
  business profiles and teams. They do not grant global governance, login-account management,
  budget allocation, model assignment, connection management or gateway publication rights.
- A current or previously delegated Member does not inherit legacy global reads when grants
  are revoked. Scoped SQL, detail lookups, totals, histories and stored assistant/report scopes
  enforce the same department boundary; private client caches are invalidated on permission changes.
  The authenticated profile refreshes every 30 seconds in either workspace and on window focus.
  Global Application and GitHub Copilot views reject scoped Members until their own verified
  department-query boundaries exist.
- In database-directory mode all Members have automatic row scopes. Ordinary users see
  themselves; organization administrators see their organization, department administrators
  their effective departments, and team administrators their effective teams' users.
  Same-level cross-memberships are combined; a team's parent department is not a whole-
  department grant. This does not change the `owner/member` login role model.
- Managed identities receive narrowly scoped data-plane and management-plane roles.
- APIM removes Turnstile-internal headers before forwarding requests to providers.

In database-directory mode, Microsoft sign-in binds the signed `(cloud, tid, oid)` to an
account. An email collision with an existing password account or Owner requires explicit
administrator binding and cannot silently adopt its role. `ENTRA_ALLOWED_TENANT_IDS` can
add an exact tenant allow-list without widening the existing email-domain gate.
Directory sync and Microsoft login configuration are independent.

## Menu Permission Groups

Local `user`, `organization_admin`, `department_admin` and `team_admin` groups control
application feature access independently of APIM admission. Administrator groups can coexist
and menus are their union. Migration `018_permission_management` stores an Owner-configured
four-group matrix, compare-and-swap revision and immutable audit. Permission Management,
global settings and global gateway/subscription operations remain Owner-only.
API `menu_permission_groups` is the effective appointment projection; singular/explicit old
personnel fields are retained for compatibility, not a second administrator configuration UI.
It is a navigation union, not a current-resource administrator check. Resource-context role
matching must compare effective `menu_administrator_scopes.scope_kind` and `.scope_id`, never
names, parent membership or a bare global role. Personnel labels use the selected-node
`scope_menu_permission_groups`; organization/department/team appointments are not aliases.
Login/profile expose exact appointments to support future scoped menu decisions; all business
APIs enforce menu entitlements in addition to independent row and mutation authorization.
Department capabilities are matched per
department, so an edit grant in one scope cannot authorize edits in a read-only scope.
Unassigned and unlinked accounts default to `user`; active
linked people supply the local group. Disabled/source-disabled people lose its menus.
Owner independently retains all applicable menus and is the only role allowed to change groups.
Appointments produce their matching menu groups. Department appointments also maintain the
pre-existing department capabilities in the same transaction; they do not grant Owner, Graph
consent, model access, budget writes or APIM admission.

The server publishes the menu whitelist in login/current-profile responses. The client applies
it to navigation, search, direct routes, prefetching and assistant entry points, and refreshes
it on focus/every 30 seconds. Existing sessions resolve the current policy and row scope on
every request, so revoked API access does not wait for the client refresh. Ordinary users
no longer inherit global directory-mode reads. Existing Owner mutations and APIM admission
remain unchanged. Saved assistant/report scopes use versioned department/user tokens in
the existing `directory_scope` column; scoped viewers cannot read old global snapshots.
Entra profile imports, job titles and team membership never create administrator appointments.
Migration 015 explicitly imports genuine active legacy department appointments. New department
writes (including the legacy API) share one appointment service and its data-scope projection.
Organizations and teams have distinct scope identities; a department appointment is not a team
appointment. Only Owner may appoint or revoke administrators, and team candidates must be actual
active linked team members. An administrator-only edit is audited and invalidates caches without
publishing a gateway identity update.

Organization personnel creation is Owner-only and atomically creates a Member password account
with a linked person and same-organization primary department. It cannot promote an account,
overwrite a pre-existing email/password or allocate model access/budget. Creation secrets are
write-only and excluded from audit/idempotency evidence. Department/team member selection is
bounded to the target unit's organization; additional membership is not billing delegation.
Retired transfer/identity-editing endpoints are absent; historical facts remain protected.

Directory connections store credential references, not secrets. Graph access is read-only,
uses fixed cloud endpoints, follows only same-cloud HTTPS paging links and encrypts delta
checkpoints with the existing credential key. Missing permissions, incomplete data and expired
cursors are not successful empty snapshots. Import and identity association do not create
Owners, model access or department grants. Large source-membership reductions require
separate review. Directory mutations are audited and preserve governance IDs and billing evidence.

## Telemetry privacy

Usage events contain identifiers, status, latency, token counts, and pricing metadata. Prompt and completion content must not be emitted to Event Hubs, logs, metrics, or PostgreSQL.

## Pre-push review

1. Run a secret scanner over tracked files.
2. Review `git diff --cached`.
3. Confirm ignored local configuration is not staged.
4. Confirm generated packages and logs are absent.
5. Rotate any value that may have been exposed before relying on history cleanup.
