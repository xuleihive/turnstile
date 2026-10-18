# Local Upstream Main Merge

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
