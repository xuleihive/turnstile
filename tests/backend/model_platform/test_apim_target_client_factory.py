from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from tests.backend.model_platform.control_plane_support import (
    ROOT,
    FakeApimClient,
    GatewayControlPlaneService,
    bedrock_publication,
)
from tests.backend.model_platform.test_gateway_targets import (
    NOW,
    SUBSCRIPTION,
    TARGET,
    deployment,
    target,
)
from turnstile_core.config import Settings
from turnstile_core.integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    ResolvedApimPublisherClient,
)
from turnstile_core.integrations.apim_policy_compiler import ApimPolicyCompiler
from turnstile_core.integrations.apim_target_client_factory import (
    AzureApimPublisherClientResolver,
    AzureApimTargetProbeCredentialResolver,
    CompatibilityTargetProbeCredentialResolver,
    LegacySettingsApimTargetClientConfigResolver,
    _identity_sha256,
    apim_target_client_config_resolver,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.gateway_publication_worker import GatewayPublicationWorker


def test_publication_captures_target_and_rejects_drift_before_resource_writes() -> None:
    repository = InMemoryRepository()
    repository.models = []
    publication = GatewayControlPlaneService(repository).publish(
        bedrock_publication(), "unit-owner",
    )
    client, resolver = FakeApimClient(), Mock()
    resolver.resolve.return_value = ResolvedApimPublisherClient(TARGET, "first", True, client)
    worker = GatewayPublicationWorker(
        repository, resolver, (ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
    )
    first = worker.run_once("target-worker")
    assert first is not None and first.id == publication.id and first.status == "validating"
    assert first.resource_manifest["apim_target_identity_sha256"] == "first"
    resolver.resolve.return_value = ResolvedApimPublisherClient(TARGET, "changed", True, client)
    with patch.object(client, "ensure_named_value") as write:
        result = worker.run_once("target-worker")
    assert result is not None and result.status == "failed"
    assert "identity changed" in (result.error_message or "")
    write.assert_not_called()


def test_compiler_target_configuration_is_copied_without_mutating_default() -> None:
    original = ApimPolicyCompiler("default-probe", "https://default.example.test", "default-key")
    scoped = original.with_probe_subscription("unit-probe").with_usage_observer(None, None)
    assert original._probe_subscription_id == "default-probe"
    assert original._usage_observer_url == "https://default.example.test"
    assert scoped._probe_subscription_id == "unit-probe" and scoped._usage_observer_url is None


def configured(**changes: Any) -> Settings:
    return Settings.model_validate(
        {
            "apim_target_registry_enabled": True,
            "apim_gateway_profile_id": UUID(int=99),
            "azure_subscription_id": str(SUBSCRIPTION),
            "apim_resource_group": "unit-group",
            "apim_service_name": "unit-service",
            "apim_api_id": "unit-api",
            "apim_usage_observer_url": "https://default-observer.example.test",
            "apim_usage_observer_key_named_value": "default-observer-key",
            "apim_probe_subscription_key": "default-test-key",
            "apim_dashboard_subscription_key": "default-dashboard-test-key",
            **changes,
        }
    )


def registered() -> InMemoryRepository:
    repository = InMemoryRepository()
    repository.upsert_apim_target(
        target(
            probe_subscription_id="unit-probe",
            management_auth_profile_ref="system_managed_identity",
        )
    )
    repository.upsert_gateway_deployment(
        deployment(
            endpoint_url="https://unit.azure-api.net",
            declared_state="present",
            last_arm_sync_at=NOW,
            reachability_state="reachable",
            last_probe_at=NOW,
        )
    )
    return repository


def test_route_target_identity_preserves_public_settings_wrapper_and_binds_ledger_only() -> None:
    repository = registered()
    settings = configured(ledger_table_endpoint="https://unitstore.table.core.windows.net",
                          ledger_table_name="TurnstileLedger")
    original = apim_target_client_config_resolver(repository, settings, None).resolve(
        TARGET, include_probe_credentials=False,
    )
    assert original.route_target_identity_sha256 == _identity_sha256({
        "target_identity_sha256": original.target_identity_sha256,
        "ledger_table_endpoint": settings.ledger_table_endpoint,
        "ledger_table_name": settings.ledger_table_name,
    })
    changed = apim_target_client_config_resolver(repository, settings.model_copy(update={
        "ledger_table_name": "UnitOtherLedger",
    }), None).resolve(TARGET, include_probe_credentials=False)
    assert original.target_identity_sha256 == changed.target_identity_sha256
    assert original.route_target_identity_sha256 != changed.route_target_identity_sha256
    assert original.settings.ledger_table_name == "TurnstileLedger"
    legacy_settings = settings.model_copy(update={"apim_gateway_profile_id": TARGET})
    legacy = LegacySettingsApimTargetClientConfigResolver(legacy_settings).resolve(TARGET)
    assert legacy.route_target_identity_sha256 == _identity_sha256({
        "target_identity_sha256": legacy.target_identity_sha256,
        "ledger_table_endpoint": settings.ledger_table_endpoint,
        "ledger_table_name": settings.ledger_table_name,
    })
    hidden = LegacySettingsApimTargetClientConfigResolver(legacy_settings).resolve(
        TARGET, include_probe_credentials=False,
    )
    assert hidden.route_target_identity_sha256 == legacy.route_target_identity_sha256
    assert hidden.settings.apim_probe_subscription_key is None


def test_bootstrap_management_resolver_never_reads_or_copies_model_subscription_keys() -> None:
    repository, credentials = registered(), Mock()
    settings = configured()
    config = apim_target_client_config_resolver(
        repository, settings, credentials, include_probe_credentials=False,
    ).resolve(TARGET)
    credentials.resolve.assert_not_called()
    assert config.gateway_profile_id == TARGET
    assert config.settings.apim_probe_subscription_key is None
    assert config.settings.apim_dashboard_subscription_key is None
    assert settings.apim_probe_subscription_key is not None


def test_resolver_does_not_reuse_default_secrets_or_observer_for_other_targets() -> None:
    repository, credentials = registered(), Mock()
    credentials.resolve.return_value = SecretStr("target-test-key")
    settings = configured()
    config = apim_target_client_config_resolver(repository, settings, credentials).resolve(TARGET)
    assert config.gateway_profile_id == TARGET and config.enforce_identity_snapshot
    assert config.settings.apim_dashboard_subscription_key is None
    assert config.settings.apim_probe_subscription_key == SecretStr("target-test-key")
    assert config.settings.apim_probe_subscription_id == "unit-probe"
    assert config.settings.apim_usage_observer_url is None
    assert settings.apim_probe_subscription_key == SecretStr("default-test-key")
    credentials.resolve.assert_called_once()


@pytest.mark.parametrize(
    "defect,error",
    [
        ("missing", "metadata is unavailable"),
        ("drift", "identity is drifted"),
        ("undeclared", "not declared and reachable"),
        ("unreachable", "not declared and reachable"),
        ("auth", "authentication is not supported"),
    ],
)
def test_resolver_rejects_unproved_targets_before_reading_credentials(
    defect: str,
    error: str,
) -> None:
    repository, credentials = registered(), Mock()
    if defect == "missing":
        repository.apim_targets.clear()
    elif defect == "drift":
        repository.apim_targets[TARGET]["drift_state"] = "drifted"
    elif defect == "auth":
        repository.apim_targets[TARGET]["management_auth_profile_ref"] = "other"
    else:
        repository.gateway_deployments[deployment().id][
            "declared_state" if defect == "undeclared" else "reachability_state"
        ] = "unknown"
    with pytest.raises(PolicyCompilationError, match=error):
        apim_target_client_config_resolver(repository, configured(), credentials).resolve(TARGET)
    credentials.resolve.assert_not_called()


def test_client_cache_is_bound_to_target_fingerprint() -> None:
    repository, credentials, builder = registered(), Mock(), Mock()
    credentials.resolve.return_value = None
    config_resolver = apim_target_client_config_resolver(repository, configured(), credentials)
    resolver = AzureApimPublisherClientResolver(config_resolver, builder)
    first = resolver.resolve(TARGET)
    config = config_resolver.resolve(TARGET)
    assert config.legacy_route_target_identity_sha256 is not None
    assert (first.route_target_identity_sha256, first.legacy_route_target_identity_sha256) == (
        config.route_target_identity_sha256, config.legacy_route_target_identity_sha256,
    )
    assert resolver.resolve(TARGET).client is first.client
    repository.gateway_deployments[deployment().id]["endpoint_url"] = "https://changed.example.test"
    second = resolver.resolve(TARGET)
    assert first.target_identity_sha256 != second.target_identity_sha256
    assert builder.call_count == 2


def test_recovery_client_does_not_read_or_reuse_probe_credentials() -> None:
    repository, credentials, builder = registered(), Mock(), Mock(side_effect=lambda config: Mock())
    credentials.resolve.return_value = SecretStr("unit-probe-key")
    resolver = AzureApimPublisherClientResolver(
        apim_target_client_config_resolver(repository, configured(), credentials), builder,
    )
    regular = resolver.resolve(TARGET)
    credentials.reset_mock()
    recovery = resolver.resolve(TARGET, include_probe_credentials=False)
    credentials.resolve.assert_not_called()
    assert recovery.target_identity_sha256 == regular.target_identity_sha256
    assert recovery.client is not regular.client
    assert builder.call_args.args[0].apim_probe_subscription_key is None
    assert resolver.resolve(TARGET, include_probe_credentials=False).client is recovery.client
    assert builder.call_count == 2


def test_probe_credential_client_ignores_ambient_target_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROL_PLANE_ENABLED", "true")
    monkeypatch.setenv("APIM_SERVICE_NAME", "unrelated-default")
    with patch(
        "turnstile_core.integrations.apim_target_client_factory.AzureApimSubscriptionKeyClient"
    ) as client, patch(
        "turnstile_core.integrations.apim_target_client_factory.AzureApimProbeAccessChecker"
    ) as access:
        access.return_value.check.return_value.status = "ready"
        client.return_value.application_subscription_keys.return_value = ("unit-key", "unit-other")
        assert AzureApimTargetProbeCredentialResolver().resolve(
            target(probe_subscription_id="unit-probe")
        ) == SecretStr("unit-key")
    scoped = client.call_args.args[0]
    assert scoped.apim_service_name == "unit-service"
    assert scoped.azure_subscription_id == str(SUBSCRIPTION)
    assert not scoped.control_plane_enabled


def test_compatibility_credentials_refuse_another_target() -> None:
    with pytest.raises(PolicyCompilationError):
        CompatibilityTargetProbeCredentialResolver(configured()).resolve(target())


@pytest.mark.parametrize(
    "url",
    [
        "http://observer.test",
        "https://observer.test/path",
        "https://observer.test?key=unit",
        "https://user@observer.test",
    ],
)
def test_observer_target_configuration_is_an_explicit_secure_origin(url: str) -> None:
    with pytest.raises(ValidationError):
        configured(apim_usage_observers={TARGET: {"url": url, "key_named_value": "observer-key"}})
