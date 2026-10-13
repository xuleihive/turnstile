import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, List  # noqa: UP035 - required by the Azure Functions worker
from uuid import uuid4

import azure.functions as func

if TYPE_CHECKING:
    from turnstile_core.ingestion.processor import UsageProcessor

app = func.FunctionApp()
logger = logging.getLogger(__name__)


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def telemetry_health(req: func.HttpRequest) -> func.HttpResponse:
    del req
    return func.HttpResponse(
        json.dumps({"status": "ok"}),
        status_code=200,
        mimetype="application/json",
    )


def _processor() -> "UsageProcessor":
    from turnstile_core.config import get_settings
    from turnstile_core.ingestion.processor import CoefficientResolver, UsageProcessor
    from turnstile_core.persistence.factory import create_repository

    settings = get_settings()
    repository = create_repository(settings)
    return UsageProcessor(repository, CoefficientResolver(settings.model_coefficients))


@app.event_hub_message_trigger(
    arg_name="events",
    event_hub_name="%EVENT_HUB_NAME%",
    connection="EVENT_HUB_CONNECTION",
    cardinality="many",
    consumer_group="$Default",
)
def process_usage_events(events: List[func.EventHubEvent]) -> None:  # noqa: UP006
    processor = _processor()
    for event in events:
        try:
            payload: Any = json.loads(event.get_body().decode("utf-8"))
            if not isinstance(payload, Mapping):
                logger.warning("Non-object Event Hub payload skipped")
                continue
            processor.process(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Malformed Event Hub message skipped")


@app.timer_trigger(
    arg_name="timer",
    schedule="0 */10 * * * *",
    run_on_startup=False,
    use_monitor=True,
)
def reconcile_stream_usage(timer: func.TimerRequest) -> None:
    """Fills in token counts that streamed responses could not report inside the APIM policy."""
    from turnstile_core.config import get_settings
    from turnstile_core.integrations.reconciliation import (
        CacheReadSyncService,
        LogAnalyticsCacheReadLog,
        LogAnalyticsGatewayUsageLog,
        ReconciliationService,
    )
    from turnstile_core.persistence.factory import create_repository

    settings = get_settings()
    if not settings.reconciliation_enabled:
        return
    if not settings.log_analytics_workspace_id:
        logger.warning("Reconciliation skipped: LOG_ANALYTICS_WORKSPACE_ID is not configured")
        return

    repository = create_repository(settings)
    service = ReconciliationService(
        repository,
        LogAnalyticsGatewayUsageLog(settings.log_analytics_workspace_id),
        lookback_hours=settings.reconciliation_lookback_hours,
        safety_lag_minutes=settings.reconciliation_safety_lag_minutes,
    )
    outcome = service.run()
    logger.info(
        "Stream usage reconciliation matched %s of %s rows up to %s",
        outcome.matched,
        outcome.scanned,
        outcome.watermark.isoformat(),
    )
    cache_buckets = CacheReadSyncService(
        repository,
        LogAnalyticsCacheReadLog(settings.log_analytics_workspace_id),
        settings.apim_api_id,
        backfill_hours=settings.cache_read_backfill_hours,
        overlap_hours=settings.cache_read_overlap_hours,
        safety_lag_minutes=settings.reconciliation_safety_lag_minutes,
    ).run()
    logger.info("Cache-read metric sync refreshed %s hourly buckets", cache_buckets)


@app.timer_trigger(
    arg_name="timer",
    schedule="0 */5 * * * *",
    run_on_startup=False,
    use_monitor=True,
)
def sync_budget_ledger(timer: func.TimerRequest) -> None:
    """Rolls budgets into the current period, then pushes them into the ledger APIM reads.

    Only the confirmed half is pushed here. Reservations are written by APIM itself, so
    a late or failed run costs precision, never correctness: an unsynced person is
    simply covered by their still-present reservation rows.
    """
    from turnstile_core.config import get_settings
    from turnstile_core.integrations.ledger import (
        ROLL_FORWARD_ACTOR,
        LedgerSyncService,
        TableStorageLedger,
        period_start_for,
    )
    from turnstile_core.integrations.reconciliation import LogAnalyticsReservationTerminalLog
    from turnstile_core.persistence.factory import create_repository

    settings = get_settings()
    repository = create_repository(settings)

    # Deliberately ahead of the ledger guards. Budgets are period-scoped while
    # `department_enforcement` is not, so a new month starts with `block` still set and no
    # allowance to enforce — and both admission paths read a missing budget row as "not
    # blocked". The console path checks PostgreSQL directly and never touches the ledger,
    # so gating this behind a ledger feature flag would let that path lapse every month
    # whenever the flag was off. The marker inside makes it exactly-once per period.
    rolled = repository.roll_forward_budgets(
        period_start_for(datetime.now(UTC)), ROLL_FORWARD_ACTOR
    )
    if rolled is not None:
        logger.info(
            "Budget roll-forward carried %s scopes into %s from %s",
            rolled["scope_count"],
            rolled["period_start"],
            rolled["source_period_start"],
        )
    application_rolled = repository.roll_forward_gateway_application_budgets(
        period_start_for(datetime.now(UTC)), ROLL_FORWARD_ACTOR
    )
    if application_rolled:
        logger.info(
            "Application budget roll-forward carried %s scopes into the current period",
            application_rolled,
        )

    if not settings.ledger_sync_enabled:
        return
    if not settings.ledger_table_endpoint:
        logger.warning("Ledger sync skipped: LEDGER_TABLE_ENDPOINT is not configured")
        return

    terminal_log = (
        LogAnalyticsReservationTerminalLog(
            settings.log_analytics_workspace_id, settings.apim_api_id
        )
        if settings.reconciliation_enabled and settings.log_analytics_workspace_id
        else None
    )
    if terminal_log is None:
        logger.warning(
            "Reservation recovery disabled or unconfigured; unknown usage remains reserved"
        )
    with TableStorageLedger(settings.ledger_table_endpoint, settings.ledger_table_name) as store:
        outcome = LedgerSyncService(
            repository,
            store,
            terminal_log=terminal_log,
            recovery_lag=timedelta(minutes=settings.ledger_reservation_recovery_lag_minutes),
            finalization_lag=timedelta(hours=settings.ledger_reservation_finalization_lag_hours),
        ).run()
    logger.info(
        "Budget ledger sync projected %s people, %s person model policies, %s applications "
        "and %s application model policies with %s mappings; settled %s person and %s application "
        "reservations",
        outcome.people,
        outcome.model_policies,
        outcome.applications,
        outcome.application_model_policies,
        outcome.application_mappings,
        outcome.reservations_settled,
        outcome.application_reservations_settled,
    )
    logger.info(
        "Ledger finalizations: people=%s applications=%s upper_bounds=%s "
        "upper_bounds_settled=%s stale_application_reservations=%s",
        outcome.person_finalizations_written,
        outcome.application_finalizations_written,
        outcome.reservations_finalized_upper_bound,
        outcome.upper_bounds_settled,
        outcome.stale_application_reservations,
    )


@app.timer_trigger(
    arg_name="timer",
    schedule="0 0 1 * * *",
    run_on_startup=False,
    use_monitor=True,
)
def cleanup_security_audit_events(timer: func.TimerRequest) -> None:
    """Keep online security audit events for 365 days, using a strict UTC cutoff."""
    del timer
    from turnstile_core.config import get_settings
    from turnstile_core.persistence.audit_store import AuditStore
    from turnstile_core.services.security_audit import SecurityAuditService

    settings = get_settings()
    if not settings.database_url:
        logger.warning("Security audit retention skipped: DATABASE_URL is not configured")
        return
    store = AuditStore(settings.database_url)
    try:
        deleted = SecurityAuditService(store).delete_expired_before(
            datetime.now(UTC) - timedelta(days=365)
        )
    finally:
        store.close()
    logger.info("Security audit retention removed %s events older than 365 days", deleted)


@app.timer_trigger(
    arg_name="timer",
    schedule="0 * * * * *",
    run_on_startup=False,
    use_monitor=True,
)
def process_usage_export_jobs(timer: func.TimerRequest) -> None:
    """Claim and process at most one leased private report export per invocation."""
    del timer
    from turnstile_core.services.usage_export_worker import create_usage_export_worker

    worker = create_usage_export_worker()
    try:
        processed = worker.run_once(f"telemetry-{uuid4().hex}", datetime.now(UTC))
    except Exception as error:
        logger.error(
            "Usage export worker invocation failed; error_type=%s",
            type(error).__name__,
        )
        raise RuntimeError("Usage export worker invocation failed") from None
    else:
        logger.info("Usage export worker processed %s job(s)", processed)
    finally:
        worker.close()
