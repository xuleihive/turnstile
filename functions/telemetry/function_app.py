import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, List  # noqa: UP035 - required by the Azure Functions worker
from uuid import UUID

import azure.functions as func

from turnstile_core.config import Settings, get_settings
from turnstile_core.ingestion.processor import CoefficientResolver, UsageProcessor
from turnstile_core.integrations.atomic_ledger import AtomicTableLedger
from turnstile_core.integrations.ledger import (
    ROLL_FORWARD_ACTOR,
    LedgerSyncService,
    TableStorageLedger,
    period_start_for,
)
from turnstile_core.integrations.reconciliation import (
    CacheReadSyncService,
    LogAnalyticsCacheReadLog,
    LogAnalyticsGatewayUsageLog,
    LogAnalyticsReservationTerminalLog,
    ReconciliationService,
)
from turnstile_core.persistence.factory import create_repository
from turnstile_core.persistence.repository import QueryRepository
from turnstile_core.services.application_provisioning_ledger import project_application_rate_limit
from turnstile_core.services.budget_settlement import (
    BudgetRecoveryRepository,
    BudgetRecoveryService,
    BudgetScopeRepository,
    BudgetScopeService,
    BudgetSettlementRepository,
    BudgetSettlementService,
)

app = func.FunctionApp()
logger = logging.getLogger(__name__)


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def telemetry_health(req: func.HttpRequest) -> func.HttpResponse:
    del req
    return func.HttpResponse(
        json.dumps({"status": "ok"}), status_code=200, mimetype="application/json",
    )


def _close_repository(repository: object) -> None:
    close = getattr(repository, "close", None)
    if close is not None:
        close()


def _processor() -> UsageProcessor:
    settings = get_settings()
    repository = create_repository(settings)
    def settle(usage_id: str) -> None:
        if not settings.budget_settlement_enabled:
            return
        if not isinstance(repository, BudgetSettlementRepository):
            raise RuntimeError("Budget settlement requires a durable repository")
        if not isinstance(repository, BudgetRecoveryRepository):
            raise RuntimeError("Budget recovery requires a durable repository")
        assert settings.ledger_table_endpoint is not None
        with AtomicTableLedger(
            settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
        ) as store:
            BudgetRecoveryService(repository, store).completions(usage_id=usage_id, limit=2)
            BudgetSettlementService(repository, store).drain(usage_id, limit=2, seconds=10)
    return UsageProcessor(
        repository, CoefficientResolver(settings.model_coefficients),
        execution_enabled=settings.budget_execution_enabled,
        settle_committed_usage=settle if settings.budget_settlement_enabled else None,
    )


@app.timer_trigger(
    arg_name="timer", schedule="*/15 * * * * *", run_on_startup=False, use_monitor=True,
)
def settle_budget_requests(timer: func.TimerRequest) -> None:
    del timer
    settings = get_settings()
    if not settings.budget_settlement_enabled:
        return
    repository = create_repository(settings)
    try:
        if not isinstance(repository, BudgetSettlementRepository):
            raise RuntimeError("Budget settlement requires a durable repository")
        assert settings.ledger_table_endpoint is not None
        with AtomicTableLedger(
            settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
        ) as store:
            if not isinstance(repository, BudgetScopeRepository):
                raise RuntimeError("Budget scope preparation requires a durable repository")
            BudgetScopeService(repository, store).prepare()
            if not isinstance(repository, BudgetRecoveryRepository):
                raise RuntimeError("Budget recovery requires a durable repository")
            recovery = BudgetRecoveryService(repository, store)
            recovery.policies()
            recovery.recover()
            recovery.completions()
            recovery.finalize_uncertain()
            applied = BudgetSettlementService(repository, store).drain()
        health = repository.budget_settlement_health()
        logger.info("Budget settlement drain applied=%d pending=%d oldest_seconds=%.3f",
                    applied, health["pending"], health["oldest_seconds"])
        if health["oldest_seconds"] > 30 or health["repeatedly_failed"]:
            logger.error("Budget settlement backlog exceeds recovery target")
        if health["unresolved"]:
            logger.warning(
                "Budget attempts lack settlement evidence unresolved=%d terminal_unresolved=%d "
                "oldest_seconds=%.3f",
                health["unresolved"], health["terminal_unresolved"],
                health["oldest_unresolved_seconds"],
            )
    finally:
        _close_repository(repository)


@app.event_hub_message_trigger(
    arg_name="events",
    event_hub_name="%EVENT_HUB_NAME%",
    connection="EVENT_HUB_CONNECTION",
    cardinality="many",
    consumer_group="$Default",
)
def process_usage_events(events: List[func.EventHubEvent]) -> None:  # noqa: UP006
    processor = _processor()
    try:
        for event in events:
            try:
                payload: Any = json.loads(event.get_body().decode("utf-8"))
                if not isinstance(payload, Mapping):
                    logger.warning("Non-object Event Hub payload skipped")
                    continue
                processor.process(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("Malformed Event Hub message skipped")
    finally:
        processor.close()


@app.timer_trigger(
    arg_name="timer",
    schedule="0 */10 * * * *",
    run_on_startup=False,
    use_monitor=True,
)
def reconcile_stream_usage(timer: func.TimerRequest) -> None:
    """Fills in token counts that streamed responses could not report inside the APIM policy."""
    settings = get_settings()
    if not settings.reconciliation_enabled:
        return
    if not settings.log_analytics_workspace_id:
        logger.warning("Reconciliation skipped: LOG_ANALYTICS_WORKSPACE_ID is not configured")
        return

    repository = create_repository(settings)
    try:
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
    finally:
        _close_repository(repository)


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
    settings = get_settings()
    repository = create_repository(settings)
    failures: list[Exception] = []
    try:
        # Deliberately ahead of the ledger guards. Budgets are period-scoped while
        # `department_enforcement` is not, so a new month starts with `block` still set and no
        # allowance to enforce — and both admission paths read a missing budget row as "not
        # blocked". The console path checks PostgreSQL directly and never touches the ledger,
        # so gating this behind a ledger feature flag would let that path lapse every month
        # whenever the flag was off. The marker inside makes it exactly-once per period.
        try:
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
        except Exception as error:
            logger.exception("Budget roll-forward failed; the ledger projection continues")
            failures.append(error)
        try:
            application_rolled = repository.roll_forward_gateway_application_budgets(
                period_start_for(datetime.now(UTC)), ROLL_FORWARD_ACTOR
            )
            if application_rolled:
                logger.info(
                    "Application budget roll-forward carried %s scopes into the current period",
                    application_rolled,
                )
        except Exception as error:
            logger.exception(
                "Application budget roll-forward failed; the ledger projection continues"
            )
            failures.append(error)
        _project_ledger(settings, repository, failures)
    finally:
        _close_repository(repository)
    if failures:
        raise ExceptionGroup("Budget ledger sync failed", failures)


def _project_ledger(
    settings: Settings, repository: QueryRepository, failures: list[Exception],
) -> None:
    if not settings.ledger_sync_enabled:
        return
    if not settings.ledger_table_endpoint:
        logger.warning("Ledger sync skipped: LEDGER_TABLE_ENDPOINT is not configured")
        return

    terminal_log = (
        LogAnalyticsReservationTerminalLog(
            settings.log_analytics_workspace_id, settings.apim_api_id
        )
        if settings.log_analytics_workspace_id
        else None
    )
    if terminal_log is None:
        logger.warning(
            "Application reservation recovery skipped: "
            "LOG_ANALYTICS_WORKSPACE_ID is not configured"
        )
    with TableStorageLedger(
        settings.ledger_table_endpoint, settings.ledger_table_name
    ) as store:
        try:
            applications = repository.list_gateway_applications()
        except Exception as error:
            logger.exception("Application TPM projection failed; the ledger sync continues")
            failures.append(error)
            applications = []
        for application in applications:
            application_id = UUID(str(application["id"]))
            try:
                project_application_rate_limit(repository, store, application_id)
            except Exception:
                logger.warning("Application TPM projection pending for %s", application_id)
        outcome = LedgerSyncService(
            repository,
            store,
            terminal_log=terminal_log,
            recovery_lag=timedelta(
                minutes=settings.ledger_reservation_recovery_lag_minutes
            ),
            finalization_lag=timedelta(
                hours=settings.ledger_reservation_finalization_lag_hours
            ),
        ).run()
    logger.info(
        "Budget ledger sync projected %s people, %s person model policies, %s applications "
        "and %s application model policies with %s mappings; settled %s person and %s "
        "application reservations; persisted %s Person and %s Application finalizations "
        "(%s conservative upper bounds), settled %s upgraded upper bounds, with %s stale "
        "Application reservations unresolved",
        outcome.people,
        outcome.model_policies,
        outcome.applications,
        outcome.application_model_policies,
        outcome.application_mappings,
        outcome.reservations_settled,
        outcome.application_reservations_settled,
        outcome.person_finalizations_written,
        outcome.application_finalizations_written,
        outcome.reservations_finalized_upper_bound,
        outcome.upper_bounds_settled,
        outcome.stale_application_reservations,
    )
