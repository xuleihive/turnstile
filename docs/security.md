# Security

## Secrets

Never commit `.env`, Azure parameter files with values, publish profiles, certificates, keys, tokens, connection strings, database dumps, or deployment packages. Use Key Vault and managed identity for Azure-hosted workloads.

Provider credentials are write-only through the API, encrypted before persistence, redacted from reads, and excluded from logs. APIM subscription keys can be revealed by the owning Member or an Owner through an audited on-demand APIM request. Reveal and key-export delivery fail closed if the success audit cannot be stored; responses use `Cache-Control: no-store`, and Turnstile does not persist key values.

## Identity and authorization

- Microsoft Entra tokens are signature-, audience-, issuer-, and domain-validated.
- Application sessions are HTTP-only and secure in production.
- Governance mutations and tenant-wide administration are Owner-only. Members can read and manage their own applications, subscriptions, and keys; API handlers enforce this scope and hide another user's application resources as not found.
- Member usage queries match both the stable Turnstile user UUID written by the APIM subscription attribution map and the email identifier used by older usage records. Request-detail authorization uses the same aliases; member-facing entity results include only the signed-in user, not the enterprise user directory.
- User enablement, display-name, and department changes immediately revoke sessions where applicable and refresh that user's APIM admission mapping. Department name/enablement changes refresh all APIM application mappings because multiple users and subscriptions can belong to a department. A user department change also updates the current department attribution of that user's owned applications, while historical usage snapshots stay unchanged. If a refresh fails, the saved change remains in place and the API returns `503`; the five-minute scheduled projection retries as a recovery path. Until a successful projection, APIM may still enforce the previous mapping snapshot.
- Security audit metadata excludes prompts, model outputs, passwords, cookies, bearer/API keys, and provider credentials. Key reveal/export requests are blocked when the corresponding success audit cannot be persisted.
- Managed identities receive narrowly scoped data-plane and management-plane roles.
- APIM removes Turnstile-internal headers before forwarding requests to providers.

## Telemetry privacy

Usage events contain identifiers, status, latency, token counts, and pricing metadata. Prompt and completion content must not be emitted to Event Hubs, logs, metrics, or PostgreSQL.

## Pre-push review

1. Run a secret scanner over tracked files.
2. Review `git diff --cached`.
3. Confirm ignored local configuration is not staged.
4. Confirm generated packages and logs are absent.
5. Rotate any value that may have been exposed before relying on history cleanup.
