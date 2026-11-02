# Troubleshooting

## API fails at startup

- Confirm `DATABASE_URL` is set and reachable over TLS.
- Run `uv run python -m backend.migrate` manually and inspect the first error.
- Confirm `frontend/dist/index.html` exists when `PRODUCTION=true`.

## Microsoft sign-in is unavailable

- Set both `ENTRA_CLIENT_ID` and `VITE_ENTRA_CLIENT_ID` to the same SPA application ID.
- Add every browser origin as an Entra redirect URI.
- Configure `ENTRA_ALLOWED_EMAIL_DOMAINS` as a JSON array.
- Rebuild the frontend after changing `VITE_` variables.

## APIM publication does not advance

- Keep the worker disabled until its managed identity and APIM role are deployed.
- Confirm the probe subscription and `APIM_REGRESSION_MODEL_KEY` refer to an onboarded model.
- Inspect release state before retrying; do not create parallel publications for one gateway.

## A publication failed and shows only its ID

- For failures without a specific explanation the console shows the publication ID. The error
  type and text are not shown because the text can contain upstream responses.
- In the Control-plane Function's Application Insights, run
  `traces | where message has "<publication-id>" | order by timestamp desc`. The worker logs the
  failing stage and error type; retry lines also include the error text.
- The full text is stored in PostgreSQL:
  `SELECT status, error_code, error_message FROM gateway_publication WHERE id = '<publication-id>';`.

## Model switch is disabled or unverified

The v1.2 switch controls a versioned Gateway operation. A model can remain enabled in the registry
while its switch is unavailable: `desired_enabled` describes intent, `effective_enabled` describes
the last verified result, and `command_ready` controls whether a new command can be accepted.
Color alone is not an authoritative status indicator.

1. Confirm the signed-in user is an Owner and inspect
	 `GET /api/v1/model-management/model-availability` using the normal authenticated session.
2. A `404` means the backend does not expose the new route. Verify the actual API package and URL;
	 changing frontend CSS or refreshing indefinitely does not install the backend.
3. A `200` with `command_ready=false` means the route is available but prerequisites or fresh
	 coverage are missing. Inspect the operation/target reason, migration 016, Control Plane timers,
	 primary Table access, every retained API Revision and every required self-hosted node enrollment.
4. `202` means an operation was accepted, not that the Gateway has applied it. A `409` requires a
	 fresh state read; an unknown response must not create a duplicate intent. Do not replace this
	 workflow with a general model edit or direct database update.

The v1.2 availability protocol requires verified Gateway coverage. Do not remove historical
Revisions, skip nodes, fabricate proof or force `command_ready` to make the control clickable.
See [availability installation](deployment.md#model-availability-installation).

## Self-hosted Gateway catalog returns 404

Compare the selected endpoint's current catalog and candidate `;rev=<revision>` path with the
matching container route. A successful Managed Gateway check does not prove the self-hosted route.

- Verify the exact native Gateway ID, target API association, container and configured HTTPS endpoint.
- A proxy path prefix must be stripped consistently for both current and candidate routes. Persist
	the proxy configuration used at startup; a runtime-only change can disappear after a reload.
- When TLS terminates at a trusted reverse proxy, preserve the intended HTTPS scheme at the isolated
	container. Do not trust forwarded headers from arbitrary public clients or disable certificate checks.
- Check the configuration-token expiry and renewal process. A running container or an old connection
	timestamp does not prove it is receiving current API configuration.
- Inspect the original publication audit before retrying. A repeated catalog failure is not proof of
	an upstream model or provider-authorization failure.

## Price selection cannot be saved or synchronization needs review

Select the actual catalog model, deployment type and region; verify every required price bucket.
Missing data or an unavailable catalog is not a free rate. A discount is the percentage charged:
`80` charges 80% of list price, and a blank model discount inherits the Connection discount.

Short/long-context labels do not establish a billing threshold. Use supplied metadata or an explicit
administrator-defined rule; an estimated rule must not be described as a verified provider threshold.
If synchronization reports review required, inspect all changed rates, buckets and boundaries before
explicitly accepting them. Preserve manual pricing and previously accepted schedules during failures.
Some Azure retail entries have short- and long-context prices but no threshold, for example
`gpt-5.6-luna` ("5.6 luna Std"). Publishing stays disabled until you switch the threshold rule to
the administrator-defined option and enter the threshold.
Historical request costs use saved price evidence, not today's model price.
See [context-tier pricing](configuration.md#context-tier-pricing).

## Connection check is blocked or no longer applicable

Open the diagnostic plan first and inspect the selected text model, Gateway and missing permissions.
The plan does not invoke a model; confirming the check sends at most one bounded request and may
incur provider charges. Do not grant broad permissions or substitute a user's key to make it pass.

A historical successful check is not live health. After a configuration change, run a new check only
when the old attempt's result is known and the cooldown allows it. A timeout with unknown outcome is
not permission to repeat a paid request automatically. See [diagnostics](configuration.md#connection-diagnostics).

## Invocation failed but usage is unavailable

With migration 015 and compatible readers, Request Trace can display a platform attempt before formal
Gateway telemetry arrives. Preserve the caller request ID, actual Gateway correlation ID and upstream
request ID separately. `usage_available=false` is missing measurement, not zero consumption.
An attempt joins formal telemetry only on an unambiguous identity match; a platform timeout and a
later Gateway result can both be valid evidence. Do not retry an unknown paid outcome to create a trace.

## A response request ID is not found in Request Trace

Look up a Gateway response by its `x-correlation-id`. Responses from model calls carry it, including
chat, image generation and Decision responses. Some Gateway rejections do not, such as an invalid
subscription key (401) or `route_configuration_unavailable` (503). A request ID the caller sent as
`x-request-id` also finds the request. The response's own `x-request-id` is not always a Turnstile
identifier: image generation and Decision responses carry the provider's request ID.

## Telemetry is missing

- Verify APIM can send to Event Hubs and the Telemetry Function can receive from it.
- Confirm the Function uses managed-identity storage settings.
- Check correlation IDs across APIM, Event Hubs, Function logs, and PostgreSQL.

## App Service deployment succeeds but old assets remain

A successful OneDeploy record does not prove the Run-From-Package mount changed. Read the served `index.html`, compare its asset name, and inspect container startup logs. Avoid repeated restarts without mount or startup evidence.

## Deployment fails behind an HTTP proxy

The Bicep CLI that `az deployment` runs can crash behind a system HTTP proxy with
`System.AccessViolationException` in `HttpConnectionPool.EstablishProxyTunnelAsync`; the deployment
command then reports the failed `az` command and its error. It can happen before any Azure
deployment is created, and a rerun may succeed. Run the deployment from a host with direct access to
Azure, or bypass the proxy by domain suffix. A setting verified on an affected host was
`NO_PROXY=.com,.net,.ms,.io,.org`; it bypasses the proxy for all of those domains, so use it only
where they are reachable directly. `NO_PROXY='*'` is not honored by the .NET HTTP client.

## Frontend cannot call the API locally

Set `API_PROXY_TARGET` in `frontend/.env.local`, restart Vite, and confirm `/health` through `http://localhost:5173/health`.
