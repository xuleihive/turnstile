# Security audit events

Turnstile stores security audit metadata in PostgreSQL table `security_audit_event`. The first implementation records password and Entra sign-in success/failure, logout, permission denials, and user/organization-unit management changes. User and organization changes are written in the same database transaction as the change; if the audit insert fails, the management write rolls back. Authentication and authorization audit writes are best effort so an audit-store outage does not turn into an authentication outage.

The `owner` role can query the audit page and download CSV. The API independently checks the `security_audit.read` permission, so hiding the navigation entry is only a usability measure, not the access boundary. The API supports UTC time bounds, actor UUID, action, result, cursor pagination, and bounded CSV export. CSV cells beginning with spreadsheet formula characters are escaped. An audit CSV export records the exporting actor and export size; usage-report exports record their time range and row count without recording report contents.

The event metadata allowlist is intentionally narrow. It may contain the actor identity and role snapshot, action/result, resource identifiers, request and correlation IDs, client IP, user-agent, and approved before/after fields such as role, enabled state, organization unit, or export range/count. It must not contain request or response bodies, prompts, model output, passwords, cookies, bearer/API keys, or provider credentials. New before/after fields must be added deliberately to `SAFE_AUDIT_METADATA_FIELDS` and covered by a test before use.

App Service deployments trust `X-ARR-ClientIP` only when `TRUST_AZURE_ARR_CLIENT_IP=true`; this value is set by the data-plane Bicep module. Other reverse proxies must be listed in `TRUSTED_PROXY_CIDRS`, and forwarded headers are considered only when the direct peer belongs to one of those networks. With neither trust path configured, the socket peer address is recorded. Do not expose this service through a proxy and blindly trust arbitrary `X-Forwarded-For` values.

The Telemetry Function runs cleanup daily at 01:00 UTC and deletes events strictly older than `now() - 365 days`. Events exactly at the cutoff remain until a later run. This is 365-day online retention only; there is no immutable archive, external SIEM delivery, cryptographic chain, or administrator-proof tamper protection in this implementation. It does not establish or certify compliance with MLPS/China Cybersecurity Classified Protection Level 3 (等保三级). Production compliance requires a scoped assessment against the applicable system boundary and controls, including protected long-term log storage, access review, time synchronization, alerting, backup/restore, and operational procedures.

## Current API

- `GET /api/v1/security/audit-events` returns a cursor-paginated page, up to 500 events per request.
- `GET /api/v1/security/audit-events/export` returns at most 10,000 filtered events as CSV and reports truncation in `X-Export-Truncated`.
- No audit mutation or deletion route is exposed. Retention cleanup is the only application deletion path.

APIM Key lifecycle events and asynchronous report-job lifecycle events use the same audit service. Export creation is audited by the API; completion and failure are recorded by the background worker. The legacy bounded synchronous report endpoint also records an export event while it remains available for scripts.
