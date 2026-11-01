# Architecture

Turnstile separates the inference data plane from management and analytics.

## Components

| Component | Responsibility |
| --- | --- |
| Azure API Management | Authenticates callers, applies access and budget policy, selects an APIM backend pool member, and emits metadata-only usage events. |
| FastAPI Web App | Serves the API and frontend, manages configuration, exposes analytics, and coordinates operator actions. |
| Telemetry Function | Consumes Event Hub records, persists usage, reconciles provider measurements, and updates the budget ledger. |
| Control-plane Function | Applies queued APIM publications and release operations outside the request path. |
| PostgreSQL | Stores configuration, identity, governance, usage, audit, and release state. |
| Table Storage | Holds the low-latency budget projection consumed by APIM. |
| Event Hubs | Decouples inference traffic from telemetry persistence. |
| Key Vault | Stores deployment and provider credentials. |
| Application Insights and Log Analytics | Host operational telemetry and reconciliation inputs. |

## Python runtime boundary

`turnstile_core` owns runtime code shared by the FastAPI application and background Functions,
including domain models, persistence, provider integrations, ingestion, and worker services. It
does not import the FastAPI `backend` package. `backend` owns the web composition root, HTTP routes,
web-only services, and data-source adapters.

The API artifact contains both packages. Telemetry and Control-plane Function artifacts contain
`turnstile_core` and their own `function_app.py`, but exclude `backend`. Architecture and staging
tests enforce these dependency and packaging boundaries.

## Request flow

1. A client calls the Turnstile APIM endpoint.
2. APIM authenticates the request and evaluates model access and budget state.
3. APIM forwards to a customer-configured provider deployment or native backend pool.
4. APIM emits metadata and provider usage to Event Hubs.
5. The Telemetry Function stores the event in PostgreSQL and updates aggregates.
6. The web application reads PostgreSQL for dashboards and governance workflows.

## Control-plane flow

Connection and model changes are stored as immutable publication intent. The control-plane Function creates a candidate APIM revision, validates it, and promotes it only after probes pass. Gateway releases preserve integrity data and rollback metadata.

Each gateway runs a fixed program whose `supported_profiles` declare the protocols it can route: OpenAI, Anthropic Messages, image generation (`openai-images`) and Decision (`microsoft-decision`). New installations and onboarded APIM gateways enable all four. A publication for a profile the program lacks is rejected before any APIM write. An earlier program gains image generation and Decision only through a recorded program upgrade that appends those profiles, adds the Decision operation and recompiles the image operation; the Control-plane verifies the native API against the approved program before and after the cutover.

## Governed image requests

Image generation uses an explicitly published v4 profile on an existing Foundry Connection. The profile is content-addressed and release-owned. Text and image models can share the Connection credential while keeping distinct APIM operations and backend routes; image responses bypass the text usage observer. Connection credential rotation updates every owned binding and nested Pool reference in the candidate release before activation.

The internal image API uses the same authenticated identity, model assignment and budget checks as text invocation. Before dispatch it records an immutable billable intent, request identity, model identity, admission month and reservation. A valid measured result creates an exact acknowledgement; uncertain results remain reserved. Publication probes use the same durable journal with explicit authorization and bounded attempts, including independent rollback phases. Worker leases are renewed during long probes and checked again when committing state or activation.

The response carries one validated PNG or JPEG image; Microsoft Foundry rejects `output_format: webp` with HTTP 400. Preview URLs exist only in browser memory and are revoked on replacement or exit. Prompts and image bytes do not become usage records, logs or query-cache entries.

## Ledger finalization

The Telemetry timer keeps reservation accounting outside the inference path. PostgreSQL stores append-only recovery evidence keyed by scope and APIM correlation. Exact usage takes precedence over terminal-zero evidence and conservative timeout bounds; an unmeasured late event cannot erase recovered exact usage. Read-only views apply this ordering to both Person and Application budget totals without fabricating request traces or model prices.

The timer discovers reservations across older partitions, determines settlement before reading confirmed totals, projects `C`, then marks or deletes `R`. A timeout ends Pending status but retains the full reserved charge in the original Table row. Unknown or incomplete log results do not authorize finalization. Application snapshots record confirmed usage, pending reservations, finalized bounds and available Tokens together, rejecting older snapshots. Their timestamps describe observed ledger state, not a real-time balance guarantee.

The independently gated v2 policy selects complete formal evidence, application acknowledgement and complete diagnostic recovery in that order. Equal-rank conflicts preserve first received evidence; an estimated or status-only error cannot displace a measured result. Immutable admission fixes the UTC budget month. Pre-cutover requests keep their legacy rules, with no backfill or historical reclassification. Person and Application budgets use the selected evidence, while raw activity retains only actual measured events and their original timestamps and pricing.

## Scope boundary

The deployment creates Turnstile infrastructure. It does not create Azure AI Foundry projects or provider model deployments. Provider resources remain customer-owned and are connected after installation.

APIM native backend pools provide failover, balanced, and weighted distribution, and Model Intelligent Router provides rule-based, weighted, and model-based logical routing.
