from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header

from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.control_plane import GatewayReleaseRetentionPolicy
from turnstile_core.integrations.apim_subscription_key_client import (
    AzureApimSubscriptionKeyClient,
)
from turnstile_core.integrations.ledger import LedgerSyncService, TableStorageLedger
from turnstile_core.pricing.catalog import CompositeCatalog, build_default_catalog
from turnstile_core.security import CredentialCipher
from turnstile_core.services.application_access import ApplicationAccessService
from turnstile_core.services.control_plane import GatewayControlPlaneService

from ..services.anomaly_service import AnomalyRuleService
from ..services.assistant import AssistantService
from ..services.budget_service import TokenBudgetService
from ..services.runtime_service import ModelRuntimeService
from .dependencies import Repository


@lru_cache(maxsize=1)
def price_catalog() -> CompositeCatalog:
    return build_default_catalog()


def runtime_service(repository: Repository) -> ModelRuntimeService:
    return ModelRuntimeService(repository, get_settings(), price_catalog=price_catalog())


RuntimeService = Annotated[ModelRuntimeService, Depends(runtime_service)]
Authorization = Annotated[str | None, Header()]


def _application_provisioning_available(settings: Settings) -> bool:
    return all((
        settings.gateway_release_worker_enabled,
        settings.gateway_application_provisioning_enabled,
        settings.ledger_table_endpoint,
        settings.credential_encryption_key,
        settings.azure_subscription_id,
        settings.apim_resource_group,
        settings.apim_service_name,
    ))


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
        application_provisioning_enabled=_application_provisioning_available(settings),
        application_default_token_limit=settings.gateway_application_default_monthly_token_limit,
        application_default_tokens_per_minute=settings.gateway_application_default_tokens_per_minute,
        application_product_id=settings.apim_product_id,
        image_generation_enabled=settings.image_generation_enabled,
        image_generation_defaults=settings.image_generation_defaults,
        databricks_oauth_enabled=settings.databricks_oauth_enabled,
    )


ControlPlaneService = Annotated[GatewayControlPlaneService, Depends(control_plane_service)]


def application_access_service(repository: Repository) -> ApplicationAccessService:
    settings = get_settings()
    provisioning_available = _application_provisioning_available(settings)
    key_settings_available = all(
        (
            settings.azure_subscription_id,
            settings.apim_resource_group,
            settings.apim_service_name,
        )
    )
    key_management_available = (
        settings.gateway_application_key_management_enabled
        and key_settings_available
    )
    return ApplicationAccessService(
        repository,
        sync_available=settings.gateway_release_worker_enabled,
        sync_unavailable_reason=(
            None
            if settings.gateway_release_worker_enabled
            else "Gateway Release Worker is not enabled."
        ),
        provisioning_available=provisioning_available,
        provisioning_unavailable_reason=(
            None
            if provisioning_available
            else "Application provisioning requires its worker, ledger, encryption and APIM target."
        ),
        key_management_available=key_management_available,
        key_management_unavailable_reason=(
            None
            if key_management_available
            else "Application subscription key management is not deployed."
        ),
        key_client=(
            AzureApimSubscriptionKeyClient(settings)
            if key_management_available
            else None
        ),
        default_token_limit=settings.gateway_application_default_monthly_token_limit,
        default_tokens_per_minute=(
            settings.gateway_application_default_tokens_per_minute
        ),
    )


ApplicationAccessServiceDependency = Annotated[
    ApplicationAccessService, Depends(application_access_service)
]


def token_budget_service(repository: Repository) -> TokenBudgetService:
    settings = get_settings()
    if not settings.ledger_sync_enabled or not settings.ledger_table_endpoint:
        return TokenBudgetService(
            repository, seed_demo_directory=settings.seed_demo_directory
        )

    endpoint = settings.ledger_table_endpoint

    def project(user_ids: Sequence[str]) -> None:
        with TableStorageLedger(endpoint, settings.ledger_table_name) as store:
            service = LedgerSyncService(repository, store)
            service.project_model_access(service.model_access(user_ids))

    return TokenBudgetService(
        repository, project, seed_demo_directory=settings.seed_demo_directory
    )


TokenBudgetServiceDependency = Annotated[
    TokenBudgetService, Depends(token_budget_service)
]


def anomaly_rule_service(repository: Repository) -> AnomalyRuleService:
    return AnomalyRuleService(repository)


AnomalyRuleServiceDependency = Annotated[
    AnomalyRuleService, Depends(anomaly_rule_service)
]


def assistant_service(repository: Repository) -> AssistantService:
    settings = get_settings()
    return AssistantService(repository, ModelRuntimeService(repository, settings), settings)


AssistantServiceDependency = Annotated[AssistantService, Depends(assistant_service)]