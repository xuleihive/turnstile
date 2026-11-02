from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from functions.control_plane import function_app
from tests.backend.model_platform.test_apim_onboarding import (
    governance_configuration,
    onboarding_request,
)
from tests.backend.model_platform.test_apim_target_inventory import arm_inventory
from tests.backend.model_platform.test_gateway_targets import TARGET
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.domain.model_availability import ModelAvailabilityState
from turnstile_core.integrations.apim_onboarding_policy import render_onboarding_parent
from turnstile_core.integrations.apim_policy_components import validate_parent_policy
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.budget_settlement import BudgetScopeRepository


@pytest.mark.parametrize("change", [
    {}, {"mode": "paused"}, {"protocol_version": 1}, {"api_id": "another-api"},
    {"gateway_profile_id": str(UUID(int=2))}, {"apim_service_id": "another-service"},
])
def test_budget_fence_is_read_only_from_exact_configured_target(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any],
) -> None:
    gateway_id = UUID(int=1)
    settings = Settings(
        budget_execution_enabled=True, budget_execution_target_ids=[gateway_id],
        ledger_table_endpoint="https://ledger.example.test", apim_service_name="Unit-Service",
        apim_api_id="unit-api",
    )
    fence = {"protocol_version": 2, "mode": "active", "gateway_profile_id": str(gateway_id),
             "apim_service_id": "unit-service", "api_id": "unit-api", **change}
    repository = MagicMock()
    repository.get_apim_target.return_value = {
        "service_name": "Unit-Service", "api_id": "unit-api",
    }
    ledger = MagicMock()
    ledger.__enter__.return_value = ledger
    ledger.read_entity.return_value = {"Document": json.dumps(fence)}
    factory_mock = MagicMock(return_value=ledger)
    monkeypatch.setattr(function_app, "AtomicTableLedger", factory_mock)
    if change:
        with pytest.raises(ValueError, match="registered target"):
            function_app._budget_configuration(gateway_id, repository, settings)
    else:
        assert function_app._budget_configuration(gateway_id, repository, settings) == fence
    partition = "budget-protocol|" + hashlib.sha256(b"unit-service").hexdigest() + "|unit-api"
    repository.get_apim_target.assert_called_once_with(gateway_id)
    ledger.read_entity.assert_called_once_with(partition, "F")
    ledger.__exit__.assert_called_once()
    ledger.upsert.assert_not_called()


@pytest.mark.parametrize("mode", ["off", "unselected", "selected", "initialized", "missing_policy"])
def test_protocol_enrollment_only_uses_enabled_target_configuration(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    settings = Settings.model_construct(
        budget_execution_enabled=mode != "off", apim_target_registry_enabled=True,
        budget_execution_target_ids=[TARGET] if mode in {"selected", "missing_policy"} else [],
        ledger_table_endpoint="https://unitledger.table.core.windows.net",
    )
    repository, client = MagicMock(), MagicMock()
    before_write, ledger = MagicMock(), MagicMock()
    ledger.__enter__.return_value = ledger
    fence = {"epoch": "unit-epoch"}
    reader = MagicMock(return_value=fence)
    enroll = MagicMock(return_value=("edge",))
    ledger_factory = MagicMock(return_value=ledger)
    monkeypatch.setattr(
        function_app, "initialized_apim_targets",
        lambda _: {TARGET: onboarding_request()} if mode == "initialized" else {},
    )
    monkeypatch.setattr(function_app, "_budget_configuration", reader)
    monkeypatch.setattr(function_app, "AtomicTableLedger", ledger_factory)
    monkeypatch.setattr(function_app, "enroll_self_hosted_gateway_protocol", enroll)
    inventory = arm_inventory()
    if mode == "missing_policy":
        with pytest.raises(ValueError, match="canonical policy"):
            function_app._enroll_gateway_protocol(
                inventory, client, repository, settings, None, before_write,
            )
        enroll.assert_not_called()
        return
    result = function_app._enroll_gateway_protocol(
        inventory, client, repository, settings, "canonical-policy", before_write,
    )
    if mode in {"off", "unselected"}:
        assert result == ()
        reader.assert_not_called()
        ledger_factory.assert_not_called()
    else:
        assert result == ("edge",)
        reader.assert_called_once_with(TARGET, repository, settings)
        assert enroll.call_args.args == (
            repository, ledger, inventory, fence, client, "canonical-policy",
        )
        assert enroll.call_args.kwargs["before_write"] is before_write
        assert callable(enroll.call_args.kwargs["verify_fixed_program"])
        ledger.__exit__.assert_called_once()


@pytest.mark.parametrize("mode", ["off", "no_store", "no_migration"])
def test_availability_timer_skips_unsupported_configuration(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    settings = Settings.model_construct(
        control_plane_enabled=mode != "off",
        ledger_table_endpoint=None if mode == "no_store" else "https://unit.table.core.windows.net",
    )
    repository = MagicMock()
    repository.model_availability_supported.return_value = False
    repository_factory, coordinator = MagicMock(return_value=repository), MagicMock()
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(function_app, "create_repository", repository_factory)
    monkeypatch.setattr(function_app, "ModelAvailabilityCoordinator", coordinator)
    function_app.synchronize_model_availability(None)
    function_app.refresh_model_availability_coverage(None)
    function_app.renew_model_availability_proofs(None)
    coordinator.assert_not_called()
    if mode == "no_migration":
        assert repository.close.call_count == 3
    else:
        repository_factory.assert_not_called()


@pytest.mark.parametrize("mode", ["synchronize", "audit", "renew"])
@pytest.mark.parametrize("fails", [False, True])
def test_availability_timers_bind_scoped_clients_and_always_close_repository(
    monkeypatch: pytest.MonkeyPatch, mode: str, fails: bool,
) -> None:
    settings = Settings.model_construct(
        control_plane_enabled=True, ledger_table_endpoint="https://unit.table.core.windows.net",
        ledger_table_name="Unit",
    )
    repository, coordinator, worker, clients, configs, store = (MagicMock() for _index in range(6))
    repository.model_availability_supported.return_value = True
    coordinator_factory = MagicMock(return_value=coordinator)
    worker_factory = MagicMock(return_value=worker)
    config_factory = MagicMock(return_value=configs)
    fixed_routes = MagicMock()
    fixed_factory = MagicMock(return_value=fixed_routes)
    store_factory = MagicMock(return_value=store)
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(function_app, "create_repository", lambda _settings: repository)
    monkeypatch.setattr(function_app, "apim_target_client_config_resolver", config_factory)
    monkeypatch.setattr(function_app, "AzureApimPublisherClientResolver", lambda _configs: clients)
    monkeypatch.setattr(function_app, "FixedRoutePublisherFactory", fixed_factory)
    monkeypatch.setattr(function_app, "ModelAvailabilityCoordinator", coordinator_factory)
    monkeypatch.setattr(function_app, "ModelAvailabilityWorker", worker_factory)
    monkeypatch.setattr(function_app, "TableStorageLedger", store_factory)
    action = coordinator.refresh if mode != "synchronize" else worker.run_once
    if fails:
        action.side_effect = RuntimeError("unit failure")
        with pytest.raises(RuntimeError, match="unit failure"):
            function_app._run_model_availability(audit=mode == "audit", renew=mode == "renew")
    else:
        function_app._run_model_availability(audit=mode == "audit", renew=mode == "renew")
    repository.close.assert_called_once()
    clients.close.assert_called_once()
    assert config_factory.call_count == 2
    assert all(call.args == (repository, settings) for call in config_factory.call_args_list)
    assert coordinator_factory.call_args.args == (
        repository, clients, settings.ledger_table_endpoint, "Unit",
    )
    callbacks = coordinator_factory.call_args.kwargs
    assert callbacks["fixed_program_verifier"] == fixed_routes.verify_availability
    renewer = callbacks["fixed_program_renewer"]
    assert renewer.func == fixed_routes.verify_availability
    assert renewer.keywords == {"renew": True}
    assert fixed_factory.call_args.args == (repository, configs)
    action.assert_called_once()
    if mode != "synchronize":
        worker_factory.assert_not_called()
        if mode == "renew":
            coordinator.refresh.assert_called_once_with(renew=True)
        else:
            coordinator.refresh.assert_called_once_with()
    else:
        assert worker_factory.call_args.args[0] is repository
        assert worker_factory.call_args.args[2] == coordinator.verify
        assert worker_factory.call_args.args[1]() is store
        store_factory.assert_called_once_with(settings.ledger_table_endpoint, "Unit", timeout=2)


def test_availability_projection_callback_uses_configured_store_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_construct(
        ledger_table_endpoint="https://unit.table.core.windows.net", ledger_table_name="Unit",
    )
    repository, store = MagicMock(), MagicMock()
    store.__enter__.return_value = store
    state = ModelAvailabilityState(model_id=UUID(int=2), revision=1, enabled=False)
    projection = MagicMock(return_value=state)
    store_factory = MagicMock(return_value=store)
    monkeypatch.setattr(function_app, "TableStorageLedger", store_factory)
    monkeypatch.setattr(function_app, "project_model_availability", projection)
    assert function_app._project_model_availability(repository, settings, state.model_id) == state
    projection.assert_called_once_with(repository, store, state.model_id)
    store_factory.assert_called_once_with(settings.ledger_table_endpoint, "Unit", timeout=2)
    store.__exit__.assert_called_once()
    with pytest.raises(ValueError, match="configured store"):
        function_app._project_model_availability(
            repository, settings.model_copy(update={"ledger_table_endpoint": None}), state.model_id,
        )
    assert store_factory.call_count == 1


def test_disabled_publication_timer_does_not_build_dependencies(monkeypatch: Any) -> None:
    settings = Settings.model_construct(
        control_plane_enabled=True,
        gateway_publication_worker_enabled=False,
    )
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)

    def reject_repository(_: object) -> None:
        raise AssertionError("disabled publication timer created a repository")

    monkeypatch.setattr(function_app, "create_repository", reject_repository)

    function_app.publish_gateway_changes(None)


@pytest.mark.parametrize("bootstrap_fails", [False, True])
@pytest.mark.parametrize("already_installed", [False, True])
def test_initial_fixed_program_uses_resolved_target_before_publication(
    monkeypatch: pytest.MonkeyPatch, bootstrap_fails: bool, already_installed: bool,
) -> None:
    import hashlib
    import json
    import sys
    from types import SimpleNamespace

    from pydantic import SecretStr

    from backend.bootstrap_storage_route import require_runtime

    target_id = UUID(int=971)
    settings = Settings.model_construct(
        control_plane_enabled=True, gateway_publication_worker_enabled=True,
        database_url="postgresql://unit@unit-server.postgres.database.azure.com/turnstile",
        azure_subscription_id=str(UUID(int=972)),
        apim_resource_group="unit-rg", apim_service_name="unit-apim", apim_api_id="unit-api",
        apim_gateway_profile_id=None, apim_probe_subscription_key=SecretStr("offline-key"),
        ledger_table_endpoint="https://unitstore.table.core.windows.net",
        ledger_table_name="TurnstileLedger",
    )
    scoped_settings = settings.model_copy(update={"apim_gateway_profile_id": target_id})
    descriptor_type, descriptor = MagicMock(), MagicMock()
    descriptor_type.model_validate_json.return_value = descriptor
    descriptor.api_name = "control-test"
    descriptor.expected_server = "unit-server.postgres.database.azure.com"
    descriptor.database_name = "turnstile"
    descriptor.registration.gateway_profile_id = target_id
    descriptor.registration.subscription_id = UUID(int=972)
    descriptor.registration.resource_group = "unit-rg"
    descriptor.registration.service_name = "unit-apim"
    descriptor.registration.api_id = "unit-api"
    descriptor.binding.ledger_table_endpoint = settings.ledger_table_endpoint
    descriptor.binding.ledger_table_name = settings.ledger_table_name
    descriptor.model_dump.return_value = {"target": str(target_id)}

    def initialize(value: Any, resolved: Settings) -> dict[str, str]:
        require_runtime(value, resolved)
        if bootstrap_fails:
            raise RuntimeError("initial program unverified")
        return {"status": "FIXED_PROGRAM_INSTALLED"}

    bootstrap = MagicMock(side_effect=initialize)
    monkeypatch.setitem(sys.modules, "bootstrap_storage_route", SimpleNamespace(
        FixedInstallationDescriptor=descriptor_type, bootstrap=bootstrap,
    ))
    monkeypatch.setenv("TURNSTILE_FIXED_BOOTSTRAP_DESCRIPTOR", "offline-descriptor")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "control-test")
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    repository, configurations = MagicMock(), MagicMock()
    repository.get_gateway_publication.return_value = {
        "resource_manifest": {"installation_request_sha256": hashlib.sha256(
            json.dumps({"target": str(target_id)}, separators=(",", ":")).encode(),
        ).hexdigest()},
    } if already_installed else None
    configurations.resolve.return_value = SimpleNamespace(settings=scoped_settings)
    if already_installed:
        configurations.resolve.side_effect = AssertionError("Installed target may be retired")
    publisher = MagicMock(side_effect=RuntimeError("publisher reached"))
    monkeypatch.setattr(function_app, "create_repository", MagicMock(return_value=repository))
    monkeypatch.setattr(function_app, "apim_target_client_config_resolver", MagicMock(
        return_value=configurations,
    ))
    monkeypatch.setattr(function_app, "AzureApimPublisherClientResolver", publisher)

    expected = (
        "initial program unverified"
        if bootstrap_fails and not already_installed else "publisher reached"
    )
    with pytest.raises(RuntimeError, match=expected):
        function_app.publish_gateway_changes(None)

    descriptor_type.model_validate_json.assert_called_once_with("offline-descriptor")
    if already_installed:
        configurations.resolve.assert_not_called()
        bootstrap.assert_not_called()
    else:
        configurations.resolve.assert_called_once_with(target_id)
        bootstrap.assert_called_once_with(descriptor, scoped_settings)
    repository.close.assert_called_once()
    if bootstrap_fails and not already_installed:
        publisher.assert_not_called()
    else:
        publisher.assert_called_once_with(configurations)


@pytest.mark.parametrize("retired", [False, True])
def test_retired_initial_target_with_delegation_does_not_block_publisher(
    monkeypatch: pytest.MonkeyPatch, retired: bool,
) -> None:
    import sys
    from types import SimpleNamespace
    from uuid import uuid5

    from tests.backend.model_platform.test_apim_target_inventory import lifecycle_service
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError

    repository, service = lifecycle_service()
    repository.runtimes = []
    if retired:
        service.remove_apim_target(TARGET)
    profile = repository.get_gateway_profiles([TARGET])[0]
    assert profile["enabled"] is not retired
    document = {"target": str(TARGET)}
    repository.gateway_publications.append({
        "id": uuid5(TARGET, "fixed-program-installation"),
        "resource_manifest": {"installation_request_sha256": hashlib.sha256(
            json.dumps(document, separators=(",", ":")).encode(),
        ).hexdigest()},
    })
    descriptor = SimpleNamespace(
        registration=SimpleNamespace(gateway_profile_id=TARGET),
        model_dump=lambda **_: document,
    )
    initialization = SimpleNamespace(
        FixedInstallationDescriptor=SimpleNamespace(model_validate_json=lambda _: descriptor),
        configure_budget_delegation=MagicMock(),
    )
    monkeypatch.setitem(sys.modules, "bootstrap_storage_route", initialization)
    monkeypatch.setenv("TURNSTILE_FIXED_BOOTSTRAP_DESCRIPTOR", "offline-descriptor")
    settings = Settings.model_construct(
        control_plane_enabled=True, gateway_publication_worker_enabled=True,
        apim_target_registry_enabled=True, budget_delegation_trust={TARGET: object()},
    )
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(function_app, "create_repository", lambda *_, **__: repository)
    publisher = MagicMock(side_effect=RuntimeError("publisher reached"))
    monkeypatch.setattr(function_app, "AzureApimPublisherClientResolver", publisher)
    if retired:
        with pytest.raises(RuntimeError, match="publisher reached"):
            function_app.publish_gateway_changes(None)
        publisher.assert_called_once()
    else:
        with pytest.raises(PolicyCompilationError, match="management authentication"):
            function_app.publish_gateway_changes(None)
        publisher.assert_not_called()
    initialization.configure_budget_delegation.assert_not_called()


def test_target_mode_does_not_construct_a_global_publisher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    settings = Settings.model_construct(
        apim_target_registry_enabled=True, control_plane_enabled=True,
        gateway_publication_worker_enabled=True,
    )
    repository, configs, resolver = MagicMock(), MagicMock(), MagicMock()
    config_factory = MagicMock(return_value=configs)
    resolver_factory = MagicMock(return_value=resolver)
    worker = MagicMock()
    worker.run_once.return_value = None
    worker_factory = MagicMock(return_value=worker)
    policy = tmp_path / "policy.xml"
    policy.write_text("<policies />")
    monkeypatch.setenv("CONTROL_PLANE_PARENT_POLICY_PATH", str(policy))
    global_factory = MagicMock(side_effect=AssertionError("global client is forbidden"))
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(function_app, "create_repository", lambda *_, **__: repository)
    monkeypatch.setattr(CredentialCipher, "from_settings", lambda _: None)
    monkeypatch.setattr(function_app, "initialized_apim_targets", lambda _: {})
    monkeypatch.setattr(function_app, "apim_target_client_config_resolver", config_factory)
    monkeypatch.setattr(function_app, "AzureApimPublisherClientResolver", resolver_factory)
    monkeypatch.setattr(function_app, "FixedRoutePublisherFactory", MagicMock())
    monkeypatch.setattr(function_app, "GatewayPublicationWorker", worker_factory)
    monkeypatch.setattr(function_app, "AzureApimPublisherClient", global_factory)
    function_app.publish_gateway_changes(None)
    config_factory.assert_called_once_with(repository, settings)
    resolver_factory.assert_called_once_with(configs)
    assert worker_factory.call_args.args[:2] == (repository, resolver)
    repository.close.assert_called_once()
    global_factory.assert_not_called()


def test_budget_fence_uses_resolved_target_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_id = UUID(int=17)
    settings = Settings.model_construct(
        apim_service_name="default-service", apim_api_id="default-api",
        budget_execution_enabled=True, budget_execution_target_ids=[gateway_id],
        ledger_table_endpoint="https://ledger.example.test",
    )
    repository = MagicMock()
    repository.get_apim_target.return_value = {
        "service_name": "target-service", "api_id": "target-api",
    }
    fence = {
        "protocol_version": 2, "mode": "active", "gateway_profile_id": str(gateway_id),
        "apim_service_id": "target-service", "api_id": "target-api",
    }
    ledger = MagicMock()
    ledger.__enter__.return_value = ledger
    ledger.read_entity.return_value = {"Document": json.dumps(fence)}
    monkeypatch.setattr(function_app, "AtomicTableLedger", lambda *_, **__: ledger)
    assert function_app._budget_configuration(gateway_id, repository, settings) == fence
    partition = "budget-protocol|" + hashlib.sha256(b"target-service").hexdigest() + "|target-api"
    ledger.read_entity.assert_called_once_with(partition, "F")
    ledger.upsert.assert_not_called()


def test_initialized_parent_matches_the_public_rendered_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, config = onboarding_request(), governance_configuration()
    parts = config.ledger_table_resource_id.split("/")
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    settings = Settings.model_construct(
        apim_target_registry_enabled=True, budget_execution_enabled=True,
        ledger_table_endpoint=f"https://{parts[8]}.table.core.windows.net/",
        ledger_table_name=parts[-1],
    )
    repository = InMemoryRepository()
    monkeypatch.setattr(
        function_app, "initialized_apim_targets", lambda _: {request.gateway_profile_id: request},
    )
    policy_path = REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml"
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    parents = function_app._parent_policy_overrides(policy_path, repository)
    validate_parent_policy(
        render_onboarding_parent(request, config, source), parents[request.gateway_profile_id],
    )
    settings = settings.model_copy(update={
        "budget_execution_target_ids": [request.gateway_profile_id],
    })
    with pytest.raises(ValueError, match="manual parent override"):
        function_app._parent_policy_overrides(policy_path, repository)


@pytest.mark.parametrize("enrollment", ["valid", "missing", "other_epoch", "other_target"])
@pytest.mark.parametrize("initialization", ["onboarding", "installation"])
def test_initialized_budget_reader_requires_exact_active_epoch(
    monkeypatch: pytest.MonkeyPatch, enrollment: str, initialization: str,
) -> None:
    request, epoch = onboarding_request(), UUID(int=24)
    settings = Settings.model_construct(
        apim_target_registry_enabled=True, budget_execution_enabled=True,
        ledger_table_endpoint="https://unitledger.table.core.windows.net",
        apim_service_name=request.service_name, apim_api_id=request.api_id,
    )
    repository = MagicMock(spec=BudgetScopeRepository)
    repository.get_apim_target = MagicMock(return_value={
        "service_name": request.service_name, "api_id": request.api_id,
    })
    repository.get_gateway_profiles = MagicMock(return_value=[{
        "id": request.gateway_profile_id, "implementation": "apim", "enabled": True,
        "config": {"onboarding_ready": True,
                   "onboarding_request": request.model_dump(mode="json")}
        if initialization == "onboarding" else {"fixed_installation": "a" * 64},
    }])
    repository.list_apim_targets = MagicMock(return_value=[{
        "gateway_profile_id": request.gateway_profile_id, "ownership_mode": "turnstile_managed",
        "arm_resource_id": request.resource_id, "tenant_id": request.tenant_id,
        "api_id": request.api_id, "api_path": request.api_path,
        "probe_subscription_id": request.probe_subscription_id,
    }])
    repository.effective_gateway_publication = MagicMock(return_value={
        "gateway_profile_id": request.gateway_profile_id, "status": "active",
        "resource_manifest": {"fixed_route_program": {}},
    })
    rollout = None if enrollment == "missing" else {
        "epoch": epoch if enrollment != "other_epoch" else UUID(int=25),
        "gateway_profile_ids": [request.gateway_profile_id] if enrollment != "other_target" else [],
    }
    repository.active_budget_execution_rollout.return_value = rollout
    fence = {
        "protocol_version": 2, "mode": "active", "epoch": str(epoch),
        "gateway_profile_id": str(request.gateway_profile_id), "api_id": request.api_id,
        "apim_service_id": request.service_name,
    }
    ledger = MagicMock()
    ledger.__enter__.return_value = ledger
    ledger.read_entity.return_value = {"Document": json.dumps(fence)}
    monkeypatch.setattr(function_app, "AtomicTableLedger", lambda *_, **__: ledger)
    if enrollment == "valid":
        actual = function_app._budget_configuration(
            request.gateway_profile_id, repository, settings,
        )
        assert actual == fence
    else:
        with pytest.raises(ValueError, match="not enrolled"):
            function_app._budget_configuration(request.gateway_profile_id, repository, settings)
    ledger.upsert.assert_not_called()


@pytest.mark.parametrize("condition", [
    "disabled", "not_apim", "missing_gateway", "wrong_gateway", "missing_target",
    "missing_installation", "pending_installation", "wrong_installation", "missing_program",
    "budget_off", "registry_off", "missing_ledger", "paused_fence", "wrong_fence_target",
    "wrong_fence_api", "wrong_fence_service", "wrong_fence_protocol",
])
def test_installed_budget_target_keeps_admission_guards(
    monkeypatch: pytest.MonkeyPatch, condition: str,
) -> None:
    request, epoch = onboarding_request(), UUID(int=24)
    profile = {"id": request.gateway_profile_id, "implementation": "apim", "enabled": True,
               "config": {"fixed_installation": "a" * 64}}
    installation = {
        "gateway_profile_id": request.gateway_profile_id, "status": "active",
        "resource_manifest": {"fixed_route_program": {}},
    }
    settings = Settings.model_construct(
        apim_target_registry_enabled=condition != "registry_off",
        budget_execution_enabled=condition != "budget_off",
        ledger_table_endpoint=None if condition == "missing_ledger" else "https://offline.invalid",
    )
    repository = MagicMock(spec=BudgetScopeRepository)
    repository.get_gateway_profiles = MagicMock(return_value=[profile])
    repository.get_apim_target = MagicMock(return_value={
        "service_name": request.service_name, "api_id": request.api_id,
    })
    repository.effective_gateway_publication = MagicMock(return_value=installation)
    repository.active_budget_execution_rollout.return_value = {
        "epoch": epoch, "gateway_profile_ids": [request.gateway_profile_id],
    }
    if condition == "disabled":
        profile["enabled"] = False
    elif condition == "not_apim":
        profile["implementation"] = "other"
    elif condition == "missing_gateway":
        repository.get_gateway_profiles.return_value = []
    elif condition == "wrong_gateway":
        profile["id"] = UUID(int=25)
    elif condition == "missing_target":
        repository.get_apim_target.return_value = None
    elif condition == "missing_installation":
        repository.effective_gateway_publication.return_value = None
    elif condition == "pending_installation":
        installation["status"] = "pending"
    elif condition == "wrong_installation":
        installation["gateway_profile_id"] = UUID(int=25)
    elif condition == "missing_program":
        installation["resource_manifest"] = {}
    fence = {
        "protocol_version": 1 if condition == "wrong_fence_protocol" else 2,
        "mode": "paused" if condition == "paused_fence" else "active", "epoch": str(epoch),
        "gateway_profile_id": str(UUID(int=25) if condition == "wrong_fence_target"
                                  else request.gateway_profile_id),
        "api_id": "other" if condition == "wrong_fence_api" else request.api_id,
        "apim_service_id": "other" if condition == "wrong_fence_service" else request.service_name,
    }
    ledger = MagicMock()
    ledger.read_entity.return_value = {"Document": json.dumps(fence)}
    monkeypatch.setattr(function_app, "initialized_apim_targets", lambda _: {})
    with pytest.raises(ValueError):
        function_app._budget_configuration(
            request.gateway_profile_id, repository, settings, ledger=ledger,
        )
    ledger.upsert.assert_not_called()


def test_release_timer_wires_lazy_initialization_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_construct(
        control_plane_enabled=True, gateway_release_worker_enabled=True,
        apim_target_registry_enabled=True,
    )
    repository, service, worker = MagicMock(), MagicMock(), MagicMock()
    worker.run_once.return_value = None
    worker_factory = MagicMock(return_value=worker)
    builder = MagicMock(return_value=service)
    monkeypatch.setenv(
        "CONTROL_PLANE_PARENT_POLICY_PATH",
        str(REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml"),
    )
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(function_app, "create_repository", lambda _: repository)
    monkeypatch.setattr(function_app, "apim_target_client_config_resolver", MagicMock())
    monkeypatch.setattr(function_app, "AzureApimPublisherClientResolver", MagicMock())
    monkeypatch.setattr(function_app, "FixedRoutePublisherFactory", MagicMock())
    monkeypatch.setattr(function_app, "initialized_apim_targets", lambda _: {})
    monkeypatch.setattr(CredentialCipher, "from_settings", lambda _: None)
    monkeypatch.setattr(function_app, "build_apim_onboarding_service", builder)
    monkeypatch.setattr(function_app, "GatewayReleaseOperationWorker", worker_factory)
    function_app.process_gateway_release_operations(None)
    builder.assert_called_once_with(repository, settings)
    assert worker_factory.call_args.kwargs["onboarding_handler"] == service.advance
    repository.close.assert_called_once()


@pytest.mark.parametrize("provisioning_enabled", (False, True))
@pytest.mark.parametrize("fails", (False, True))
@pytest.mark.parametrize("gateway_creation_enabled", (False, True))
def test_release_timer_never_constructs_publication_worker(
    monkeypatch: Any, provisioning_enabled: bool, fails: bool, gateway_creation_enabled: bool,
) -> None:
    settings = Settings.model_construct(
        control_plane_enabled=True,
        gateway_release_worker_enabled=True,
        gateway_application_provisioning_enabled=provisioning_enabled,
        self_hosted_gateway_provisioning_enabled=gateway_creation_enabled,
        ledger_table_endpoint="https://ledger.example.test",
        ledger_table_name="TurnstileLedger",
        gateway_release_retention_count=20,
        gateway_release_retention_days=180,
        gateway_failed_release_retention_days=30,
        gateway_release_protected_labels=["milestone", "rollback"],
        gateway_application_default_monthly_token_limit=100_000,
        gateway_application_default_tokens_per_minute=100_000,
        control_plane_lease_seconds=180,
        control_plane_max_attempts=30,
    )
    repository = MagicMock()
    repository.get_effective_gateway_application_budget.return_value = None
    publisher = object()
    cipher = object()
    calls: list[tuple[str, int, int]] = []
    ledger_calls: list[tuple[object, ...]] = []

    class Ledger:
        def __init__(self, endpoint: str, table: str) -> None:
            ledger_calls.append((endpoint, table))

        def __enter__(self) -> Ledger:
            return self

        def __exit__(self, *_: object) -> None:
            ledger_calls.append(("closed",))

    def prepare(*args: object) -> None:
        assert args[0] is repository
        assert isinstance(args[1], Ledger)
        ledger_calls.append(args[2:])

    class ReleaseWorker:
        def __init__(
            self,
            actual_repository: object,
            actual_publisher: object,
            _: object,
            **kwargs: object,
        ):
            assert actual_repository is repository
            assert actual_publisher is publisher
            assert callable(kwargs.pop("availability_projector"))
            projector = kwargs.pop("application_projector")
            if provisioning_enabled:
                assert callable(projector)
                projector(UUID(int=1), UUID(int=2))
            else:
                assert projector is None
            assert callable(kwargs.pop("application_rate_limit_projector"))
            assert callable(kwargs.pop("route_integrity_verifier"))
            assert kwargs.pop("parent_policy_overrides") == {}
            assert kwargs.pop("probe_request_limits") == {}
            assert kwargs.pop("onboarding_handler") is None
            assert kwargs.pop("gateway_protocol_enrollment") is None
            management = kwargs.pop("gateway_management_client_resolver")
            assert (management is not None) is gateway_creation_enabled
            assert kwargs == {
                "application_default_token_limit": 100_000,
                "application_default_tokens_per_minute": 100_000,
                "cipher": cipher,
                "parent_policy": None,
                "self_hosted_gateway_provisioning_enabled": gateway_creation_enabled,
            }

        def run_once(
            self, worker_id: str, lease_seconds: int, max_attempts: int
        ) -> None:
            calls.append((worker_id, lease_seconds, max_attempts))
            if fails:
                raise RuntimeError("worker failure")

    class PublicationWorker:
        def __init__(self, *_: object, **__: object) -> None:
            raise AssertionError("release timer constructed publication worker")

    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(function_app, "create_repository", lambda _: repository)
    monkeypatch.setattr(function_app, "apim_target_client_config_resolver", MagicMock())
    monkeypatch.setattr(function_app, "AzureApimPublisherClientResolver", lambda _: publisher)
    monkeypatch.setattr(function_app, "AzureApimGatewayManagementClientResolver", MagicMock())
    monkeypatch.setattr(function_app, "FixedRoutePublisherFactory", MagicMock())
    monkeypatch.setattr(
        CredentialCipher, "from_settings", lambda _: cipher
    )
    monkeypatch.setattr(function_app, "GatewayReleaseOperationWorker", ReleaseWorker)
    monkeypatch.setattr(function_app, "GatewayPublicationWorker", PublicationWorker)
    monkeypatch.setattr(function_app, "AtomicTableLedger", Ledger)
    monkeypatch.setattr(function_app, "prepare_application_ledger", prepare)
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "release-worker-instance")

    if fails:
        with pytest.raises(RuntimeError, match="worker failure"):
            function_app.process_gateway_release_operations(None)
    else:
        function_app.process_gateway_release_operations(None)

    repository.close.assert_called_once_with()
    assert calls == [("release-worker-instance", 180, 30)]
    assert ledger_calls == ([
        ("https://ledger.example.test", "TurnstileLedger"),
        (UUID(int=1), UUID(int=2)),
        ("closed",),
    ] if provisioning_enabled else [])


@pytest.mark.parametrize("fails", (False, True))
def test_publication_timer_closes_its_repository(
    monkeypatch: Any, tmp_path: Path, fails: bool,
) -> None:
    settings = Settings.model_construct(
        control_plane_enabled=True, gateway_publication_worker_enabled=True,
    )
    repository = MagicMock()
    worker = MagicMock()
    worker.run_once.return_value = None
    if fails:
        worker.run_once.side_effect = RuntimeError("worker failure")
    policy = tmp_path / "policy.xml"
    policy.write_text("<policies />")
    monkeypatch.setenv("CONTROL_PLANE_PARENT_POLICY_PATH", str(policy))
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    repository_factory = MagicMock(return_value=repository)
    monkeypatch.setattr(function_app, "create_repository", repository_factory)
    monkeypatch.setattr(function_app, "AzureApimPublisherClient", lambda _: object())
    monkeypatch.setattr(CredentialCipher, "from_settings", lambda _: object())
    monkeypatch.setattr(function_app, "GatewayPublicationWorker", lambda *_, **__: worker)

    if fails:
        with pytest.raises(RuntimeError, match="worker failure"):
            function_app.publish_gateway_changes(None)
    else:
        function_app.publish_gateway_changes(None)

    repository.close.assert_called_once_with()
    repository_factory.assert_called_once_with(settings, autocommit=True)


def test_publication_timer_logs_the_failure_reason(
    monkeypatch: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings.model_construct(
        control_plane_enabled=True, gateway_publication_worker_enabled=True,
    )
    failed = MagicMock(
        id=UUID(int=7), error_code="PolicyCompilationError",
        error_message="Fixed route connection approvals conflict", resource_manifest={},
    )
    failed.status.value = "failed"
    worker = MagicMock()
    worker.run_once.return_value = failed
    policy = tmp_path / "policy.xml"
    policy.write_text("<policies />")
    monkeypatch.setenv("CONTROL_PLANE_PARENT_POLICY_PATH", str(policy))
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(function_app, "create_repository", MagicMock())
    monkeypatch.setattr(function_app, "AzureApimPublisherClient", lambda _: object())
    monkeypatch.setattr(CredentialCipher, "from_settings", lambda _: object())
    monkeypatch.setattr(function_app, "GatewayPublicationWorker", lambda *_, **__: worker)

    with caplog.at_level("WARNING", logger=function_app.logger.name):
        function_app.publish_gateway_changes(None)

    worker.run_once.assert_called_once()
    assert caplog.messages == [
        f"Gateway publication {UUID(int=7)} failed: error_code=PolicyCompilationError "
        "error_message='Fixed route connection approvals conflict'"
    ]


@pytest.mark.parametrize("kind,fails", [
    ("model_add", False), ("model_add", True), ("route_reconcile", False),
])
def test_publication_timer_projects_prices_once_a_model_is_active(
    monkeypatch: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str, fails: bool,
) -> None:
    settings = Settings.model_construct(
        control_plane_enabled=True, gateway_publication_worker_enabled=True,
        ledger_table_endpoint="https://ledger.example.test", ledger_table_name="Ledger",
    )
    active = MagicMock(id=UUID(int=8), resource_manifest={})
    active.status.value = "active"
    active.publication_kind.value = kind
    worker = MagicMock()
    worker.run_once.return_value = active
    repository = MagicMock()
    ledger = MagicMock()
    ledger.__enter__.return_value = ledger
    projections: list[tuple[object, object]] = []

    def project(target: object, store: object, now: object) -> None:
        projections.append((target, store))
        if fails:
            raise RuntimeError("ledger unavailable")

    policy = tmp_path / "policy.xml"
    policy.write_text("<policies />")
    monkeypatch.setenv("CONTROL_PLANE_PARENT_POLICY_PATH", str(policy))
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(function_app, "create_repository", lambda *_, **__: repository)
    monkeypatch.setattr(function_app, "AtomicTableLedger", lambda *_, **__: ledger)
    monkeypatch.setattr(function_app, "initialize_model_prices", project)
    monkeypatch.setattr(CredentialCipher, "from_settings", lambda _: object())
    monkeypatch.setattr(function_app, "GatewayPublicationWorker", lambda *_, **__: worker)

    with caplog.at_level("WARNING", logger=function_app.logger.name):
        function_app.publish_gateway_changes(None)

    assert projections == ([(repository, ledger)] if kind == "model_add" else [])
    assert caplog.messages == ([
        f"Model price projection pending after publication {UUID(int=8)} "
        "error_type=RuntimeError"
    ] if fails else [])


def test_worker_id_does_not_claim_a_local_process_on_flex_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Flex Consumption has no WEBSITE_INSTANCE_ID; the fallback lands in publication audit,
    # where "control-plane-local" read as a developer machine.
    monkeypatch.delenv("WEBSITE_INSTANCE_ID", raising=False)
    assert function_app._worker_id() == "control-plane"
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "a" * 200)
    assert function_app._worker_id() == "a" * 128
