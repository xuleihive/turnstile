# Turnstile documentation

## Version scope

**Turnstile v1.3 is the current public release.** The documentation below
describes the release workflows and operational boundaries.

| Task | Start here | Important boundary |
| --- | --- | --- |
| Configure list prices, discounts and context tiers | [Pricing](configuration.md#context-tier-pricing) | Unknown thresholds and missing prices are not zero; historical requests retain their price snapshots. |
| Register and inspect an APIM target | [Registered targets](configuration.md#registered-apim-targets) | Registration references an existing Azure service; it does not create one. |
| Create or associate a Self-hosted Gateway | [Create a Gateway](configuration.md#create-a-self-hosted-gateway-from-turnstile) | An APIM Gateway configuration is not a running container; HTTPS, persistent proxy configuration and token renewal remain operator responsibilities. |
| Diagnose a Connection | [Connection diagnostics](configuration.md#connection-diagnostics) | Planning is non-inference; a confirmed check may incur provider charges. |
| Select a Gateway and inspect a failed invocation | [Platform invocation trace](configuration.md#platform-invocation-trace) | A request-attempt record is not billing evidence; unknown usage must remain unknown. |
| Understand a disabled model switch | [Availability](configuration.md#versioned-model-availability) and [troubleshooting](troubleshooting.md#model-switch-is-disabled-or-unverified) | Accepted intent is not verified Gateway enforcement; availability requires verified Gateway coverage. |
| Prepare an existing installation | [Upgrade preparation](deployment.md#prepare-for-v13) | Review application, migration and Gateway protocol changes separately; retain existing state and history. |

## Operator references

- [Architecture](architecture.md)
- [Configuration](configuration.md)
- [Deployment](deployment.md)
- [Testing](testing.md)
- [End-to-End Validation](e2e-validation.md)
- [Security](security.md)
- [Troubleshooting](troubleshooting.md)

The root [README](../README.md) is the primary onboarding document. The [website user manual](https://turn-stile.com/docs)
provides task-oriented UI guidance. These repository pages contain maintainer and operator details;
environment-specific deployment history does not belong in either public documentation surface.
