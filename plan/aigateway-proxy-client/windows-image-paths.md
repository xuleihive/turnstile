# Cross-platform image bridge lifecycle

Historical implementation scope: 2026-10-14. The path and ownership contract
remains relevant; the version, local-host availability and no-deploy restriction
below apply to that original task only. Subsequent 0.4.6 delivery and native macOS
verification are recorded in the [release index](release-history.md) and
[image-registration design](native-image-registration-design.md).

## Approved scope

Keep one user plugin for macOS and Windows. Replace the POSIX-only leading-slash
check with the host's `node:path.isAbsolute()`, retaining canonical `realpath()`
comparison and ownership checks. Relative, malformed, missing, unrelated and
disabled registrations must not enable the bridge. Signed-out or foreign-user
accounts must remain rejected. Requests and the existing shutdown timer must use
the same check.

Update the download generator and locally installed plugin to 0.4.5. Do not change
Magpie source, OAuth bootstrap identity, subscriptions, budgets, APIM, or Agent
configuration. Do not deploy, push changes, or perform paid generation requests.

## Alternatives

- Native absolute-path recognition (selected): one package, minimal change,
  preserves filesystem-based verification on each supported host.
- Separate OS packages: duplicates lifecycle/security code without fixing an
  OS-specific dependency; unnecessary for this path handling issue.
- Remove the registration check: unsafe because disabled/removed plugins could
  retain an image capability; rejected.

## Verification

- Node regression tests for registration, account ownership, malformed paths,
  spaces/non-ASCII directories and directory links.
- Isolated native Bun bridge tests with fake credentials and a loopback platform:
  authenticated discovery/MCP, stable lifetime, and disable/remove/logout cleanup.
- Existing plugin, pricing, quota, authorization and download tests.
- Local Magpie restart; read-only text model, bridge health, image model and MCP
  discovery checks. Never copy private configuration into the repository.

Actual macOS execution is not available on this Windows host. POSIX path contracts
and platform-neutral code/tests are not a substitute for a macOS runtime test.

## Implementation sequence

1. Add a failing native-path regression test.
2. Extract the shared enabled check and replace its path predicate.
3. Update version/package regression assertions and integration coverage.
4. Run isolated tests before patching the installed plugin and restarting Magpie.
5. Record verified results and remaining limitations here.

## Local verification (2026-10-14)

- The original bridge returned 403 for authenticated model discovery from a
  Windows absolute-path fixture. The same native Bun regression passes after
  replacing the path predicate.
- Related JS suites: 68 passed, zero failed/skipped, including 11 new path and
  lifecycle cases. Five of those exercise a real native ARM64 Bun child process
  against an isolated loopback platform with fake credentials.
- Existing test harnesses now use `pathToFileURL()` for dynamic imports. POSIX
  permission assertions remain enabled on POSIX hosts, but are not treated as
  Windows ACL assertions. Windows ACL security is not newly certified by this fix.
- Targeted Python package/API/frontend harness tests: 29 passed, eight skipped
  because local PostgreSQL is not configured, one Starlette deprecation warning.
  Ruff passes for the changed Python service/tests.
- Windows checkout initially converted prepared SVGs to CRLF, breaking their
  stored hashes. The committed Git blobs match the original checksums; those
  exact bytes were restored, without regenerating hashes or weakening validation.
  `.gitattributes` prevents future checkout conversion for prepared icon assets.
  The frontend Python harness now explicitly reads source files as UTF-8.
- TypeScript/Vite production build passes; the existing large-chunk warning
  remains. Dependency installation reports two existing high npm audit findings;
  this fix does not upgrade dependencies or claim those findings resolved.
- The installed 0.4.5 image source matches the repository source byte-for-byte.
  After restarting desktop Magpie, read-only discovery returns five text models,
  three image models, and the MCP `generate_image` tool with three allowed models.
  The same image process survives multiple two-second lifecycle checks.
- Installed private bootstrap, host providers, plugin registrations and settings
  are unchanged. Old code/README/package metadata were backed up separately;
  private authorization and runtime descriptors were never copied into Git.
- No paid image/text generation, production deployment, Agent configuration
  change, live logout, permission change or business-data write was performed.

macOS native execution still requires a macOS host. The fix keeps native POSIX
recognition and canonical-directory verification, but Windows-only validation
must not be described as a completed macOS acceptance test.
