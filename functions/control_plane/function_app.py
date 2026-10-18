from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from time import monotonic
from typing import cast
from uuid import UUID

import azure.functions as func
import httpx

from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.control_plane import GatewayReleaseRetentionPolicy
from turnstile_core.domain.gateway_targets import ApimTargetArmInventory
from turnstile_core.domain.model_availability import ModelAvailabilityState
from turnstile_core.domain.storage_routes import (
    FixedProgramCutover,
    FixedProgramInstallation,
    canonical_document,
)
from turnstile_core.integrations.apim_control_plane import (
    ApimPolicyCompiler,
    ApimPublisherClient,
    PolicyCompilationError,
)
from turnstile_core.integrations.apim_onboarding_policy import onboarding_canonical_parent
from turnstile_core.integrations.apim_publisher_client import AzureApimPublisherClient
from turnstile_core.integrations.apim_storage_routes import FixedProgramVerifier
from turnstile_core.integrations.apim_target_client_factory import (
    AzureApimGatewayManagementClientResolver,
    AzureApimPublisherClientResolver,
    apim_target_client_config_resolver,
    resolve_apim_target_settings,
)
from turnstile_core.integrations.atomic_ledger import AtomicTableLedger
from turnstile_core.integrations.ledger import TableStorageLedger
from turnstile_core.persistence.factory import create_repository
from turnstile_core.persistence.repository import QueryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.apim_onboarding import (
    build_apim_onboarding_service,
    initialized_apim_targets,
)
from turnstile_core.services.apim_onboarding_ledger import enroll_self_hosted_gateway_protocol
from turnstile_core.services.application_provisioning_ledger import (
    prepare_application_ledger,
    project_application_rate_limit,
)
from turnstile_core.services.budget_settlement import BudgetScopeRepository, BudgetScopeService
from turnstile_core.services.gateway_publication_worker import GatewayPublicationWorker
from turnstile_core.services.gateway_release_operation_worker import (
    GatewayReleaseOperationWorker,
)
from turnstile_core.services.model_availability import (
    ModelAvailabilityCoordinator,
    ModelAvailabilityRepository,
    ModelAvailabilityWorker,
    project_model_availability,
)
from turnstile_core.services.route_publication import RouteConfigurationExecutor
from turnstile_core.services.storage_route_publisher import (
    FixedRoutePublisherFactory,
    register_verified_program_upgrade,
)

app = func.FunctionApp()
logger = logging.getLogger(__name__)


def _close_repository(repository: object) -> None:
    close = getattr(repository, "close", None)
    if close is not None:
        close()


@app.route(
    route="model-programs/{target_id}/cutover", methods=["POST"],
    trigger_arg_name="request",
    auth_level=func.AuthLevel.FUNCTION,
)
def cutover_gateway_program(request: func.HttpRequest) -> func.HttpResponse:
    settings = get_settings()
    if not settings.control_plane_enabled:
        return func.HttpResponse(status_code=503)
    expected = request.headers.get("If-Match", "")
    try:
        target = UUID(request.route_params.get("target_id", ""))
        write = FixedProgramCutover.model_validate_json(request.get_body())
        if len(expected) != 64 or any(
            character not in "0123456789abcdef" for character in expected
        ):
            return func.HttpResponse(status_code=400)
    except ValueError:
        return func.HttpResponse(status_code=400)
    if str(target) != write.upgrade.installation.program.scope.target_id:
        return func.HttpResponse(status_code=409)
    repository = create_repository(settings)
    try:
        register_verified_program_upgrade(
            repository, apim_target_client_config_resolver(repository, settings),
            write.upgrade, expected, parent_policy=write.parent_policy,
            expected_parent_etag=write.expected_parent_etag,
            operation_policies=write.operation_policies or None,
            expected_operation_etags=write.expected_operation_etags or None,
        )
        return func.HttpResponse(
            write.upgrade.model_dump_json(), mimetype="application/json",
            headers={"Cache-Control": "no-store"}, status_code=200,
        )
    except ValueError:
        return func.HttpResponse(
            '{"code":"program_cutover_unconfirmed","readback_required":true}',
            mimetype="application/json", status_code=409,
        )
    finally:
        _close_repository(repository)
def _project_model_availability(
    repository: QueryRepository, settings: Settings, model_id: UUID,
) -> ModelAvailabilityState:
    if not settings.ledger_table_endpoint:
        raise ValueError("Model availability projection requires the configured store")
    with TableStorageLedger(
        settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
    ) as store:
        return project_model_availability(
            cast(ModelAvailabilityRepository, repository), store, model_id,
        )


@app.timer_trigger(
    arg_name="timer", schedule="*/2 * * * * *", run_on_startup=False, use_monitor=True,
)
def synchronize_model_availability(timer: func.TimerRequest) -> None:
    del timer
    _run_model_availability(audit=False)


@app.timer_trigger(
    arg_name="timer", schedule="10 */2 * * * *", run_on_startup=False, use_monitor=True,
)
def refresh_model_availability_coverage(timer: func.TimerRequest) -> None:
    del timer
    _run_model_availability(audit=True)


@app.timer_trigger(
    arg_name="timer", schedule="5,20,35,50 * * * * *", run_on_startup=False, use_monitor=True,
)
def renew_model_availability_proofs(timer: func.TimerRequest) -> None:
    del timer
    _run_model_availability(audit=False, renew=True)


@app.timer_trigger(
    arg_name="timer", schedule="7,22,37,52 * * * * *", run_on_startup=False, use_monitor=True,
)
def renew_storage_route_program_proofs(timer: func.TimerRequest) -> None:
    del timer
    settings = get_settings()
    if not settings.control_plane_enabled:
        return
    repository = create_repository(settings)
    try:
        factory = FixedRoutePublisherFactory(
            repository, apim_target_client_config_resolver(repository, settings),
            active_fence=lambda target: _publication_budget_configuration(
                target, repository, settings,
            ),
            availability_projector=lambda model_id: _project_model_availability(
                repository, settings, model_id,
            ),
            cipher=CredentialCipher.from_settings(settings),
        )
        factory.renew_program_proofs()
    finally:
        _close_repository(repository)


def _run_model_availability(*, audit: bool, renew: bool = False) -> None:
    settings = get_settings()
    if not settings.control_plane_enabled or not settings.ledger_table_endpoint:
        return
    repository = create_repository(settings)
    clients: AzureApimPublisherClientResolver | None = None
    try:
        if not repository.model_availability_supported():
            return
        clients = AzureApimPublisherClientResolver(
            apim_target_client_config_resolver(repository, settings),
        )
        fixed_routes = FixedRoutePublisherFactory(
            repository, apim_target_client_config_resolver(repository, settings),
            active_fence=lambda target: _publication_budget_configuration(
                target, repository, settings,
            ),
            availability_projector=lambda model_id: _project_model_availability(
                repository, settings, model_id,
            ),
        )
        coordinator = ModelAvailabilityCoordinator(
            repository, clients, settings.ledger_table_endpoint, settings.ledger_table_name,
            fixed_program_verifier=fixed_routes.verify_availability,
            fixed_program_renewer=partial(fixed_routes.verify_availability, renew=True),
        )
        if renew:
            coordinator.refresh(renew=True)
            return
        if audit:
            coordinator.refresh()
            return
        endpoint = settings.ledger_table_endpoint
        worker = ModelAvailabilityWorker(
            cast(ModelAvailabilityRepository, repository),
            lambda: TableStorageLedger(endpoint, settings.ledger_table_name, timeout=2),
            coordinator.verify,
        )
        worker.run_once(os.environ.get("WEBSITE_INSTANCE_ID", "control-plane-local")[:128])
    finally:
        try:
            if clients is not None:
                clients.close()
        finally:
            _close_repository(repository)


def _parent_policy_overrides(
    policy_path: Path, repository: QueryRepository | None = None,
) -> dict[UUID, str]:
    settings = get_settings()
    overrides = {}
    if getattr(settings, "apim_legacy_target_compatibility_enabled", False):
        if repository is not None:
            settings = resolve_apim_target_settings(repository, settings)
        if settings.apim_gateway_profile_id is None:
            raise ValueError("Legacy APIM configuration requires APIM_GATEWAY_PROFILE_ID")
        overrides[settings.apim_gateway_profile_id] = policy_path.with_name(
            "foundry-finops-policy.schema1.xml"
        ).read_text(encoding="utf-8")
    targets = getattr(settings, "budget_execution_target_ids", [])
    if targets:
        from turnstile_core.integrations.apim_budget_execution import (
            compose_budget_execution_parent,
        )
        source = policy_path.read_text(encoding="utf-8")
        for target in targets:
            if target in overrides:
                raise RuntimeError("Execution cannot silently replace a schema-one target")
            overrides[target] = compose_budget_execution_parent(
                source, source, enabled=True,
                delegation=settings.budget_delegation_trust.get(target),
            )
    if repository is not None and getattr(settings, "apim_target_registry_enabled", False):
        source = policy_path.read_text(encoding="utf-8")
        for target, request in initialized_apim_targets(repository).items():
            if target in overrides:
                raise ValueError("Initialized APIM cannot use a manual parent override")
            overrides[target] = onboarding_canonical_parent(source, request.api_id)
    return overrides


def _budget_configuration(
    gateway_id: UUID, repository: QueryRepository, settings: Settings,
    *, ledger: AtomicTableLedger | None = None,
) -> dict[str, object]:
    initialized = (
        initialized_apim_targets(repository)
        if getattr(settings, "apim_target_registry_enabled", False) else {}
    )
    if (
        not settings.budget_execution_enabled
        or (
            gateway_id not in settings.budget_execution_target_ids
            and gateway_id not in initialized
        )
        or not settings.ledger_table_endpoint
    ):
        raise ValueError("Budget publication target is not enabled")
    target = repository.get_apim_target(gateway_id)
    if target is None:
        raise ValueError("Budget publication target is unavailable")
    service = str(target["service_name"]).lower()
    partition = f"budget-protocol|{hashlib.sha256(service.encode()).hexdigest()}|{target['api_id']}"
    ledger_context = (nullcontext(ledger) if ledger is not None else AtomicTableLedger(
        settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
    ))
    with ledger_context as store:
        entity = store.read_entity(partition, "F")
    fence = json.loads(entity["Document"]) if entity is not None else None
    if (
        not isinstance(fence, dict) or fence.get("protocol_version") != 2
        or fence.get("mode") != "active" or fence.get("gateway_profile_id") != str(gateway_id)
        or fence.get("api_id") != target["api_id"]
        or str(fence.get("apim_service_id", "")).lower() != service
    ):
        raise ValueError("Budget publication fence does not match its registered target")
    if gateway_id in initialized:
        if not isinstance(repository, BudgetScopeRepository):
            raise ValueError("Initialized APIM requires durable budget execution enrollment")
        rollout = repository.active_budget_execution_rollout()
        if (
            rollout is None or gateway_id not in rollout["gateway_profile_ids"]
            or str(rollout["epoch"]) != fence.get("epoch")
        ):
            raise ValueError("Initialized APIM is not enrolled in the current budget execution")
    return fence


def _publication_budget_configuration(
    gateway_id: UUID, repository: QueryRepository, settings: Settings,
    *, ledger: AtomicTableLedger | None = None,
) -> dict[str, object]:
    fence = (_budget_configuration(gateway_id, repository, settings) if ledger is None
             else _budget_configuration(gateway_id, repository, settings, ledger=ledger))
    policies = settings.budget_request_policies.get(gateway_id)
    model_policies = settings.budget_model_request_policies.get(gateway_id)
    approved = settings.budget_approved_model_contracts.get(gateway_id)
    if not policies and not approved and not model_policies:
        return fence
    return {
        **fence,
        **({"request_policies": {
            operation: policy.model_dump(mode="json") for operation, policy in policies.items()
        }} if policies else {}),
        **({"model_request_policies": {
            str(model_id): policy.model_dump(mode="json")
            for model_id, policy in model_policies.items()
        }} if model_policies else {}),
        **({"approved_model_contracts": {
            str(model_id): contract.model_dump(mode="json")
            for model_id, contract in approved.items()
        }} if approved else {}),
    }


def _enroll_gateway_protocol(
    inventory: ApimTargetArmInventory,
    client: ApimPublisherClient,
    repository: QueryRepository,
    settings: Settings,
    canonical_parent: str | None,
    before_write: Callable[[], None] | None = None,
) -> tuple[str, ...]:
    if not settings.budget_execution_enabled:
        return ()
    initialized = (
        initialized_apim_targets(repository) if settings.apim_target_registry_enabled else {}
    )
    if (
        inventory.gateway_profile_id not in settings.budget_execution_target_ids
        and inventory.gateway_profile_id not in initialized
    ):
        return ()
    if not settings.ledger_table_endpoint or canonical_parent is None:
        raise PolicyCompilationError(
            "Gateway registration requires its ledger and canonical policy"
        )
    fence = _budget_configuration(inventory.gateway_profile_id, repository, settings)

    def verify_fixed() -> None:
        current = repository.effective_gateway_publication(inventory.gateway_profile_id)
        if current is None:
            raise PolicyCompilationError("Gateway registration fixed baseline is unavailable")
        gateways = repository.get_gateway_profiles([inventory.gateway_profile_id])
        if len(gateways) != 1 or gateways[0]["id"] != inventory.gateway_profile_id:
            raise PolicyCompilationError("Gateway registration target is unavailable")
        raw = current.get("resource_manifest", {}).get("fixed_route_program")
        if not isinstance(raw, dict):
            raise PolicyCompilationError("Gateway registration fixed program is unavailable")
        installed = FixedProgramInstallation.model_validate_json(canonical_document(raw))
        from turnstile_core.persistence.repository_support import (
            _registered_program_installation,
        )

        installed = _registered_program_installation(
            str(current["id"]), installed, gateways[0].get("config") or {},
        )
        configurations = apim_target_client_config_resolver(repository, settings)
        config = configurations.resolve(
            inventory.gateway_profile_id, include_probe_credentials=True,
        )
        if (
            installed.native_gateway_scope is not True
            or installed.program.scope.target_id != str(inventory.gateway_profile_id)
            or installed.program.scope.api_id != inventory.api_id
            or installed.program.api_revision != inventory.api_revision
            or installed.target_identity_sha256 != config.route_target_identity_sha256
            or installed.gateway_endpoint != str(config.settings.apim_gateway_url).rstrip("/")
            or config.settings.apim_probe_subscription_key is None
        ):
            raise PolicyCompilationError("Gateway registration fixed program binding changed")
        with httpx.Client(timeout=30.0, follow_redirects=False) as http:
            management = AzureApimPublisherClient(config.settings, client=http)
            verifier = FixedProgramVerifier(
                installed, management, config.settings.apim_probe_subscription_key, http,
            )
            verifier.verify()

    with AtomicTableLedger(
        settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
    ) as ledger:
        return enroll_self_hosted_gateway_protocol(
            repository, ledger, inventory, fence, client, canonical_parent,
            before_write=before_write,
            verify_fixed_program=verify_fixed,
        )


@app.timer_trigger(
    arg_name="timer",
    schedule="25 * * * * *",
    run_on_startup=False,
    use_monitor=True,
)
def project_directory_identities(timer: func.TimerRequest) -> None:
    del timer
    settings = get_settings()
    if not settings.directory_identity_projection_enabled:
        return
    from turnstile_core.integrations.directory_identity_table import TableDirectoryIdentityStore
    from turnstile_core.persistence.directory_store import directory_store
    from turnstile_core.services.directory_projection import DirectoryProjectionWorker

    if not settings.database_url or not settings.ledger_table_endpoint:
        logger.error("Directory identity projection requires PostgreSQL and the existing ledger")
        return
    with TableDirectoryIdentityStore(
        settings.ledger_table_endpoint, settings.ledger_table_name
    ) as writer:
        result = DirectoryProjectionWorker(directory_store(settings.database_url), writer).run_once(
            worker_id=os.environ.get("WEBSITE_INSTANCE_ID", "directory-projector-local")[:128],
        )
    if result:
        logger.info("Directory projection %s advanced to %s", result["id"], result["status"])


@app.timer_trigger(
    arg_name="timer",
    schedule="5 * * * * *",
    run_on_startup=False,
    use_monitor=True,
)
def synchronize_directory(timer: func.TimerRequest) -> None:
    del timer
    settings = get_settings()
    if not settings.directory_sync_enabled:
        return
    from turnstile_core.integrations.entra_directory import GraphDirectoryClient
    from turnstile_core.persistence.directory_store import directory_store
    from turnstile_core.services.directory_sync_worker import DirectorySyncWorker

    if not settings.database_url:
        logger.error("Directory synchronization requires PostgreSQL")
        return
    worker = DirectorySyncWorker(
        directory_store(settings.database_url),
        lambda connection: GraphDirectoryClient(
            connection,
            managed_identity_tenant_id=settings.directory_managed_identity_tenant_id,
            deployment_cloud=settings.directory_deployment_cloud,
        ),
        CredentialCipher.from_settings(settings),
    )
    worker.enqueue_due()
    result = worker.run_once(
        worker_id=os.environ.get("WEBSITE_INSTANCE_ID", "directory-local")[:128],
    )
    if result:
        logger.info("Directory sync %s advanced to %s", result["id"], result["status"])


@app.timer_trigger(
    arg_name="timer",
    schedule="*/30 * * * * *",
    run_on_startup=False,
    use_monitor=True,
)
def publish_gateway_changes(timer: func.TimerRequest) -> None:
    del timer
    settings = get_settings()
    if not (settings.control_plane_enabled and settings.gateway_publication_worker_enabled):
        return
    repository = create_repository(settings, autocommit=True)
    resources = ExitStack()
    try:
        configurations = apim_target_client_config_resolver(repository, settings)
        publisher = AzureApimPublisherClientResolver(configurations)
        cipher = CredentialCipher.from_settings(settings)
        client = resources.enter_context(httpx.Client(timeout=10.0, follow_redirects=False))
        ledger: AtomicTableLedger | None = None
        routes = FixedRoutePublisherFactory(
            repository, configurations,
            active_fence=lambda target: _publication_budget_configuration(
            target, repository, settings, ledger=ledger,
            ),
            availability_projector=lambda model_id: _project_model_availability(
                repository, settings, model_id,
            ),
            cipher=cipher,
            client=client,
        )
        if settings.ledger_table_endpoint:
            ledger = resources.enter_context(AtomicTableLedger(
                settings.ledger_table_endpoint, settings.ledger_table_name,
                client=client, token_provider=routes._tokens, timeout=2,
            ))
        worker_id = os.environ.get("WEBSITE_INSTANCE_ID", "control-plane-local")[:128]
        policy_path = Path(
            os.environ.get(
                "CONTROL_PLANE_PARENT_POLICY_PATH",
                "policies/foundry-finops-policy.xml",
            )
        )
        if not policy_path.is_file():
            logger.error("Gateway publication skipped: parent policy baseline is unavailable")
        else:
            worker = GatewayPublicationWorker(
                repository,
                publisher,
                policy_path.read_text(encoding="utf-8"),
                ApimPolicyCompiler(settings.apim_probe_subscription_id),
                cipher,
                chat_completions_operation_id=settings.apim_chat_completions_operation_id,
                responses_operation_id=settings.apim_responses_operation_id,
                responses_compact_operation_id=settings.apim_responses_compact_operation_id,
                messages_operation_id=settings.apim_messages_operation_id,
                count_tokens_operation_id=settings.apim_count_tokens_operation_id,
                models_operation_id=settings.apim_models_operation_id,
                parent_policy_overrides=_parent_policy_overrides(policy_path, repository),
                budget_configuration=lambda target: _publication_budget_configuration(
                    target, repository, settings, ledger=ledger,
                ),
                probe_request_limits=settings.control_plane_probe_request_limits,
                availability_projector=lambda model_id: _project_model_availability(
                    repository, settings, model_id,
                ),
                route_configuration_executor=RouteConfigurationExecutor(
                    repository, routes.resolve, prepare=routes.prepare,
                    promote_program=routes.promote_program, remove_model=routes.remove_model,
                ),
            )
            preparation_deadline = monotonic() + 60
            for _ in range(32):
                result = worker.run_once(
                    worker_id=worker_id,
                    lease_seconds=settings.control_plane_lease_seconds,
                    max_attempts=settings.control_plane_max_attempts,
                )
                if result is None:
                    break
                logger.info(
                    "Gateway publication %s advanced to %s",
                    result.id,
                    result.status.value,
                )
                route_configuration = result.resource_manifest.get("route_configuration")
                model_removal = "model_route_removal" in result.resource_manifest
                advancing = {"queued", "validating", "provisioning", "building_revision"}
                if (isinstance(route_configuration, dict)
                        and route_configuration.get("configuration_only") is True) or model_removal:
                    advancing |= {"verifying", "promoting"}
                if (result.status.value not in advancing
                    or (route_configuration is None and not model_removal)
                    or result.resource_manifest.get("route_last_progress_attempt")
                    != result.attempt_count or monotonic() >= preparation_deadline):
                    break
    finally:
        resources.close()
        _close_repository(repository)


@app.timer_trigger(
    arg_name="timer",
    schedule="15,45 * * * * *",
    run_on_startup=False,
    use_monitor=True,
)
def process_gateway_release_operations(timer: func.TimerRequest) -> None:
    del timer
    settings = get_settings()
    if not (
        settings.control_plane_enabled
        and settings.gateway_release_worker_enabled
    ):
        return
    repository = create_repository(settings)
    try:
        client_config_resolver = apim_target_client_config_resolver(repository, settings)
        publisher = AzureApimPublisherClientResolver(client_config_resolver)
        cipher = CredentialCipher.from_settings(settings)
        routes = FixedRoutePublisherFactory(
            repository, client_config_resolver,
            active_fence=lambda target: _publication_budget_configuration(
                target, repository, settings,
            ),
            availability_projector=lambda model_id: _project_model_availability(
                repository, settings, model_id,
            ),
            cipher=cipher,
        )
        worker_id = os.environ.get("WEBSITE_INSTANCE_ID", "control-plane-local")[:128]

        def project_application(gateway_id: UUID, application_id: UUID) -> None:
            assert settings.ledger_table_endpoint is not None
            with AtomicTableLedger(
                settings.ledger_table_endpoint, settings.ledger_table_name
            ) as store:
                prepare_application_ledger(repository, store, gateway_id, application_id)
                budget = repository.get_effective_gateway_application_budget(
                    application_id, datetime.now(UTC).date().replace(day=1),
                )
                usd_budget = budget is not None and budget.get("budget_mode") == "usd"
                if usd_budget and not settings.budget_execution_enabled:
                    raise RuntimeError("USD subscription creation requires budget execution")
                if settings.budget_execution_enabled:
                    if not isinstance(repository, BudgetScopeRepository):
                        raise RuntimeError("Application execution needs a durable repository")
                    prepared = BudgetScopeService(repository, store).prepare(
                        application_id=application_id, gateway_profile_id=gateway_id,
                    )
                    if usd_budget and prepared == 0:
                        raise RuntimeError("USD subscription execution scope is not ready")

        def project_rate_limit(application_id: UUID) -> int | None:
            assert settings.ledger_table_endpoint is not None
            with AtomicTableLedger(
                settings.ledger_table_endpoint, settings.ledger_table_name,
            ) as store:
                return project_application_rate_limit(repository, store, application_id)

        policy_path = Path(os.environ.get(
            "CONTROL_PLANE_PARENT_POLICY_PATH", "policies/foundry-finops-policy.xml",
        ))
        parent_policy = policy_path.read_text(encoding="utf-8") if policy_path.is_file() else None
        parent_overrides = _parent_policy_overrides(policy_path, repository)

        def enroll_gateway(
            inventory: ApimTargetArmInventory, client: ApimPublisherClient,
            before_write: Callable[[], None],
        ) -> tuple[str, ...]:
            return _enroll_gateway_protocol(
                inventory, client, repository, settings,
                parent_overrides.get(inventory.gateway_profile_id, parent_policy),
                before_write,
            )

        release_worker = GatewayReleaseOperationWorker(
            repository,
            publisher,
            GatewayReleaseRetentionPolicy(
                retained_count=settings.gateway_release_retention_count,
                retained_days=settings.gateway_release_retention_days,
                failed_retained_days=settings.gateway_failed_release_retention_days,
                protected_labels=settings.gateway_release_protected_labels,
            ),
            application_default_token_limit=(
                settings.gateway_application_default_monthly_token_limit
            ),
            application_default_tokens_per_minute=(
                settings.gateway_application_default_tokens_per_minute
            ),
            cipher=cipher,
            parent_policy=parent_policy,
            parent_policy_overrides=parent_overrides,
            availability_projector=lambda model_id: _project_model_availability(
                repository, settings, model_id,
            ),
            application_projector=(
                project_application
                if settings.gateway_application_provisioning_enabled
                and settings.ledger_table_endpoint
                else None
            ),
            application_rate_limit_projector=(
                project_rate_limit if settings.ledger_table_endpoint else None
            ),
            onboarding_handler=(
                build_apim_onboarding_service(repository, settings).advance
                if getattr(settings, "apim_target_registry_enabled", False) else None
            ),
            gateway_protocol_enrollment=(
                enroll_gateway if getattr(settings, "budget_execution_enabled", False) else None
            ),
            self_hosted_gateway_provisioning_enabled=settings.self_hosted_gateway_provisioning_enabled,
            gateway_management_client_resolver=AzureApimGatewayManagementClientResolver(
                client_config_resolver,
            ) if settings.self_hosted_gateway_provisioning_enabled else None,
            probe_request_limits=settings.control_plane_probe_request_limits,
            route_integrity_verifier=routes.inspect_integrity,
        )
        operation = release_worker.run_once(
            worker_id=worker_id,
            lease_seconds=settings.control_plane_lease_seconds,
            max_attempts=settings.control_plane_max_attempts,
        )
        if operation is not None:
            logger.info(
                "Gateway release operation %s advanced to %s",
                operation.id,
                operation.status.value,
            )
    finally:
        _close_repository(repository)
