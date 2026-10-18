from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date
from functools import lru_cache
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import Depends, Header, HTTPException

from turnstile_core.config import get_settings
from turnstile_core.domain.control_plane import GatewayReleaseRetentionPolicy
from turnstile_core.domain.ledger import BudgetPriceProjection, LedgerScope
from turnstile_core.integrations.apim_target_client_factory import (
    AzureApimGatewayManagementClientResolver,
    AzureApimSubscriptionKeyClientResolver,
    apim_target_client_config_resolver,
)
from turnstile_core.integrations.atomic_ledger import AtomicTableLedger
from turnstile_core.integrations.gateway_connection_probe import (
    GatewayConnectionProbe,
)
from turnstile_core.integrations.ledger import LedgerSyncService, TableStorageLedger
from turnstile_core.pricing.catalog import CompositeCatalog, build_default_catalog
from turnstile_core.security import CredentialCipher
from turnstile_core.services.apim_onboarding import (
    ApimOnboardingService,
    apim_onboarding_available,
    build_apim_onboarding_service,
)
from turnstile_core.services.application_access import ApplicationAccessService
from turnstile_core.services.application_provisioning_ledger import (
    project_application_rate_limit,
    read_application_rate_limit,
)
from turnstile_core.services.budget_settlement import (
    BudgetReservationService,
    BudgetResolutionRepository,
    BudgetScopeRepository,
    BudgetStatusRepository,
    execution_budget_view,
)
from turnstile_core.services.control_plane import GatewayControlPlaneService
from turnstile_core.services.model_availability import (
    ModelAvailabilityRepository,
    ModelAvailabilityService,
    build_controlled_gateway_enroller,
)

from ..services.anomaly_service import AnomalyRuleService
from ..services.assistant import AssistantService
from ..services.budget_service import TokenBudgetService
from ..services.runtime_service import ModelRuntimeService
from .dependencies import Repository
from .session import CurrentSession


@lru_cache(maxsize=1)
def price_catalog() -> CompositeCatalog:
    return build_default_catalog()


def runtime_service(repository: Repository) -> ModelRuntimeService:
    settings = get_settings()

    def project_budget_price(model_id: UUID, projection: BudgetPriceProjection) -> None:
        if projection.model_id != model_id or not settings.ledger_table_endpoint:
            raise ValueError("Model price projection identity or storage is unavailable")
        with AtomicTableLedger(
            settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
        ) as store:
            store.project_model_price(projection)

    return ModelRuntimeService(
        repository, settings, price_catalog=price_catalog(),
        project_budget_price=project_budget_price if settings.ledger_sync_enabled else None,
        topology_reader=(
            lambda rows: control_plane_service(repository).apim_target_topologies(rows)
        ) if settings.apim_target_registry_enabled else None,
    )


RuntimeService = Annotated[ModelRuntimeService, Depends(runtime_service)]
Authorization = Annotated[str | None, Header()]


def model_availability_service(repository: Repository) -> ModelAvailabilityService:
    settings = get_settings()
    return ModelAvailabilityService(
        cast(ModelAvailabilityRepository, repository),
        node_enroller=(build_controlled_gateway_enroller(repository, settings)
                       if settings.apim_target_registry_enabled else None),
    )


ModelAvailabilityServiceDependency = Annotated[
    ModelAvailabilityService, Depends(model_availability_service),
]


def control_plane_service(repository: Repository) -> GatewayControlPlaneService:
    settings = get_settings()
    return GatewayControlPlaneService(
        repository,
        CredentialCipher.from_settings(settings),
        apim_principal_id=settings.apim_principal_id,
        retention_policy=GatewayReleaseRetentionPolicy(
            retained_count=settings.gateway_release_retention_count,
            retained_days=settings.gateway_release_retention_days,
            failed_retained_days=settings.gateway_failed_release_retention_days,
            protected_labels=settings.gateway_release_protected_labels,
        ),
        release_worker_enabled=settings.gateway_release_worker_enabled,
        application_provisioning_enabled=(
            settings.gateway_application_provisioning_enabled
        ),
        application_default_token_limit=settings.gateway_application_default_monthly_token_limit,
        application_default_tokens_per_minute=settings.gateway_application_default_tokens_per_minute,
        image_generation_defaults=settings.image_generation_defaults,
        image_generation_enabled=settings.image_generation_enabled,
        databricks_oauth_enabled=settings.databricks_oauth_enabled,
        target_inventory_sync_enabled=(
            settings.apim_target_registry_enabled
            and settings.gateway_release_worker_enabled
        ),
        deployment_scoping_enabled=settings.self_hosted_gateway_enabled,
        target_registry_enabled=settings.apim_target_registry_enabled,
        multi_target_rollout_enabled=settings.multi_apim_target_rollout_enabled,
        gateway_connection_probe=GatewayConnectionProbe(),
        trusted_gateway_endpoints=settings.self_hosted_gateway_trusted_endpoints,
        onboarding_available=apim_onboarding_available(settings),
        self_hosted_gateway_provisioning_enabled=settings.self_hosted_gateway_provisioning_enabled,
        gateway_bootstrap_client_resolver=AzureApimGatewayManagementClientResolver(
            apim_target_client_config_resolver(repository, settings),
        ) if settings.self_hosted_gateway_provisioning_enabled else None,
        gateway_bootstrap_image=settings.self_hosted_gateway_image,
        price_catalog=price_catalog(),
    )


ControlPlaneService = Annotated[GatewayControlPlaneService, Depends(control_plane_service)]


def apim_onboarding_service(repository: Repository) -> ApimOnboardingService:
    return build_apim_onboarding_service(repository, get_settings())


ApimOnboardingServiceDependency = Annotated[
    ApimOnboardingService, Depends(apim_onboarding_service)
]


def application_access_service(repository: Repository) -> ApplicationAccessService:
    settings = get_settings()
    projection_available = settings.ledger_sync_enabled and bool(settings.ledger_table_endpoint)

    def project_rate_limit(application_id: UUID) -> int | None:
        assert settings.ledger_table_endpoint is not None
        with TableStorageLedger(
            settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
        ) as store:
            return project_application_rate_limit(repository, store, application_id)

    def read_rate_limit(application_id: UUID) -> int | None:
        assert settings.ledger_table_endpoint is not None
        with TableStorageLedger(
            settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
        ) as store:
            return read_application_rate_limit(store, application_id)

    def project_organization(application_id: UUID) -> int:
        assert settings.ledger_table_endpoint is not None
        with TableStorageLedger(
            settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
        ) as store:
            return LedgerSyncService(repository, store).project_application_mappings(application_id)

    def project_model_access(application_id: UUID) -> int:
        assert settings.ledger_table_endpoint is not None
        with TableStorageLedger(
            settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
        ) as store:
            service = LedgerSyncService(repository, store)
            policies = [
                policy for policy in service.application_model_access()
                if policy.application_id == str(application_id)
            ]
            return service.project_application_model_access(policies)

    def read_execution_budget(application_id: UUID, period_start: date) -> dict[str, Any] | None:
        if (not isinstance(repository, BudgetStatusRepository)
            or not isinstance(repository, BudgetScopeRepository)
            or not settings.ledger_table_endpoint):
            raise RuntimeError("Execution budget reads require PostgreSQL and Table")
        scope = LedgerScope(
            scope_type="application", scope_id=str(application_id), period_start=period_start,
        )
        rollout = repository.active_budget_execution_rollout()
        status = repository.budget_execution_status(scope)
        if (rollout is None or status is None or status["epoch"] != rollout["epoch"]
            or status["state"] != "active"):
            return None
        with AtomicTableLedger(
            settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
        ) as store:
            return execution_budget_view(repository, store, scope)
    key_settings_available = all(
        (
            settings.azure_subscription_id,
            settings.apim_resource_group,
            settings.apim_service_name,
        )
    ) or settings.apim_target_registry_enabled
    key_management_available = (
        settings.gateway_application_key_management_enabled
        and key_settings_available
    )
    provisioning_unavailable_reason = (
        "Gateway Release Worker is not enabled."
        if not settings.gateway_release_worker_enabled
        else "Application subscription provisioning is not enabled in this environment."
        if not settings.gateway_application_provisioning_enabled
        else "APIM management target is not configured."
        if not key_settings_available
        else "Credential encryption is unavailable."
        if not settings.credential_encryption_key
        else None
    )
    return ApplicationAccessService(
        repository,
        sync_available=settings.gateway_release_worker_enabled,
        sync_unavailable_reason=(
            None
            if settings.gateway_release_worker_enabled
            else "Gateway Release Worker is not enabled."
        ),
        provisioning_available=provisioning_unavailable_reason is None,
        provisioning_unavailable_reason=provisioning_unavailable_reason,
        key_management_available=key_management_available,
        key_management_unavailable_reason=(
            None
            if key_management_available
            else "Application subscription key management is not deployed."
        ),
        key_client=(
            AzureApimSubscriptionKeyClientResolver(
                apim_target_client_config_resolver(repository, settings)
            )
            if key_management_available
            else None
        ),
        default_token_limit=settings.gateway_application_default_monthly_token_limit,
        default_tokens_per_minute=(
            settings.gateway_application_default_tokens_per_minute
        ),
        read_execution_budget=read_execution_budget if settings.budget_execution_enabled else None,
        project_rate_limit=project_rate_limit if projection_available else None,
        read_rate_limit=read_rate_limit if projection_available else None,
        project_organization=project_organization if projection_available else None,
        project_model_access=project_model_access if projection_available else None,
    )


ApplicationAccessServiceDependency = Annotated[
    ApplicationAccessService, Depends(application_access_service)
]


def token_budget_service(repository: Repository) -> TokenBudgetService:
    settings = get_settings()

    def require_atomic_budget() -> None:
        if (not settings.budget_execution_enabled
            or not isinstance(repository, BudgetScopeRepository)
            or repository.active_budget_execution_rollout() is None):
            raise HTTPException(
                status_code=503, detail="USD switching requires verified atomic admission",
            )

    if not settings.ledger_sync_enabled or not settings.ledger_table_endpoint:
        return TokenBudgetService(repository, require_atomic_budget=require_atomic_budget)

    endpoint = settings.ledger_table_endpoint

    def project(user_ids: Sequence[str]) -> None:
        with TableStorageLedger(endpoint, settings.ledger_table_name) as store:
            service = LedgerSyncService(repository, store)
            service.project_model_access(service.model_access(user_ids))

    return TokenBudgetService(repository, project, require_atomic_budget=require_atomic_budget)


TokenBudgetServiceDependency = Annotated[
    TokenBudgetService, Depends(token_budget_service)
]


def budget_reservation_service(repository: Repository) -> Iterator[BudgetReservationService]:
    settings = get_settings()
    if (
        not settings.budget_reservation_resolution_enabled or not settings.ledger_table_endpoint
        or not isinstance(repository, BudgetResolutionRepository)
    ):
        raise HTTPException(status_code=503, detail="Budget reservation review is not enabled")
    with AtomicTableLedger(
        settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
    ) as store:
        yield BudgetReservationService(repository, store)


BudgetReservationServiceDependency = Annotated[
    BudgetReservationService, Depends(budget_reservation_service),
]


def anomaly_rule_service(repository: Repository) -> AnomalyRuleService:
    return AnomalyRuleService(repository)


AnomalyRuleServiceDependency = Annotated[
    AnomalyRuleService, Depends(anomaly_rule_service)
]


def assistant_service(repository: Repository, identity: CurrentSession) -> AssistantService:
    settings = get_settings()
    return AssistantService(
        repository, ModelRuntimeService(repository, settings), settings,
        department_ids=identity.directory_department_ids,
        data_scope=identity.data_scope,
    )


AssistantServiceDependency = Annotated[AssistantService, Depends(assistant_service)]
