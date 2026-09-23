from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import UUID

import azure.functions as func

from turnstile_core.config import get_settings
from turnstile_core.domain.control_plane import GatewayReleaseRetentionPolicy
from turnstile_core.integrations.apim_control_plane import (
    ApimPolicyCompiler,
    AzureApimPublisherClient,
)
from turnstile_core.integrations.ledger import TableStorageLedger
from turnstile_core.persistence.factory import create_repository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.application_provisioning_ledger import prepare_application_ledger
from turnstile_core.services.gateway_publication_worker import GatewayPublicationWorker
from turnstile_core.services.gateway_release_operation_worker import (
    GatewayReleaseOperationWorker,
)

app = func.FunctionApp()
logger = logging.getLogger(__name__)


@app.timer_trigger(
    arg_name="timer",
    schedule="*/30 * * * * *",
    run_on_startup=False,
    use_monitor=True,
)
def publish_gateway_changes(timer: func.TimerRequest) -> None:
    del timer
    settings = get_settings()
    if not (
        settings.control_plane_enabled
        and settings.gateway_publication_worker_enabled
    ):
        return
    repository = create_repository(settings)
    publisher = AzureApimPublisherClient(settings)
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
            ApimPolicyCompiler(
                settings.apim_probe_subscription_id,
                settings.apim_usage_observer_url,
                settings.apim_usage_observer_key_named_value,
            ),
            CredentialCipher.from_settings(settings),
            chat_completions_operation_id=settings.apim_chat_completions_operation_id,
            responses_operation_id=settings.apim_responses_operation_id,
            responses_compact_operation_id=settings.apim_responses_compact_operation_id,
            messages_operation_id=settings.apim_messages_operation_id,
            count_tokens_operation_id=settings.apim_count_tokens_operation_id,
            models_operation_id=settings.apim_models_operation_id,
        )
        result = worker.run_once(
            worker_id=worker_id,
            lease_seconds=settings.control_plane_lease_seconds,
            max_attempts=settings.control_plane_max_attempts,
        )
        if result is not None:
            logger.info(
                "Gateway publication %s advanced to %s",
                result.id,
                result.status.value,
            )


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
    publisher = AzureApimPublisherClient(settings)
    worker_id = os.environ.get("WEBSITE_INSTANCE_ID", "control-plane-local")[:128]

    def project_application(gateway_id: UUID, application_id: UUID) -> None:
        if not settings.ledger_table_endpoint:
            raise RuntimeError("Application admission ledger is not configured")
        with TableStorageLedger(
            settings.ledger_table_endpoint, settings.ledger_table_name
        ) as store:
            prepare_application_ledger(repository, store, gateway_id, application_id)

    policy_path = Path(
        os.environ.get("CONTROL_PLANE_PARENT_POLICY_PATH", "policies/foundry-finops-policy.xml")
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
        application_default_token_limit=(settings.gateway_application_default_monthly_token_limit),
        application_default_tokens_per_minute=(
            settings.gateway_application_default_tokens_per_minute
        ),
        cipher=CredentialCipher.from_settings(settings),
        parent_policy=policy_path.read_text(encoding="utf-8") if policy_path.is_file() else None,
        application_projector=(
            project_application
            if settings.gateway_application_provisioning_enabled and settings.ledger_table_endpoint
            else None
        ),
        dashboard_subscription_id=settings.apim_dashboard_subscription_id,
        probe_subscription_id=settings.apim_probe_subscription_id,
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