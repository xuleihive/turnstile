import subprocess
import sys
import typing
from datetime import timedelta
from inspect import signature
from pathlib import Path
from typing import Any, get_args, get_origin
from unittest.mock import MagicMock, patch

import azure.functions as func
import pytest

from functions.telemetry.function_app import (
    app,
    process_usage_events,
    reconcile_stream_usage,
    settle_budget_requests,
    sync_budget_ledger,
    telemetry_health,
)
from turnstile_core.config import Settings
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy

ROOT = Path(__file__).resolve().parents[3]


def test_metadata_indexing_does_not_import_backend_modules() -> None:
    script = """
import builtins

original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "backend" or name.startswith("backend."):
        raise AssertionError(f"backend import during metadata indexing: {name}")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
from functions.telemetry import function_app

assert len(function_app.app.get_functions()) == 5
"""

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_trigger_binding_names_match_function_parameters() -> None:
    for function in app.get_functions():
        trigger = function.get_trigger()
        parameters = signature(function.get_user_function()).parameters

        assert trigger is not None
        assert trigger.name in parameters


def test_budget_execution_is_default_off_and_settlement_timer_has_no_side_effects() -> None:
    settings = Settings()
    assert not settings.budget_execution_enabled and not settings.budget_settlement_enabled
    with (
        patch("functions.telemetry.function_app.get_settings", return_value=settings),
        patch("functions.telemetry.function_app.create_repository") as create,
        patch("functions.telemetry.function_app.AtomicTableLedger") as store,
    ):
        settle_budget_requests.build().get_user_function()(None)
    create.assert_not_called()
    store.assert_not_called()


@pytest.mark.parametrize("options", [
    {"budget_execution_target_ids": ["10000000-0000-4000-8000-000000000001"]},
    {"budget_settlement_enabled": True, "ledger_table_endpoint": "https://ledger.example.com"},
    {"budget_settlement_enabled": True, "budget_execution_enabled": True},
    {"budget_settlement_enabled": True, "budget_execution_enabled": True,
     "ledger_table_endpoint": "https://ledger.example.com", "data_backend": "demo"},
])
def test_budget_execution_settings_reject_incomplete_dependencies(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Budget (execution|settlement)"):
        Settings(**options)


@pytest.mark.parametrize("fails", [False, True])
def test_budget_settlement_timer_uses_durable_services_and_closes_resources(fails: bool) -> None:
    settings = Settings(budget_execution_enabled=True,
                        budget_settlement_enabled=True,
                        ledger_table_endpoint="https://ledger.example.com")
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    with (
        patch("functions.telemetry.function_app.get_settings", return_value=settings),
        patch("functions.telemetry.function_app.create_repository", return_value=repository),
        patch.object(repository, "close") as close,
        patch.object(repository, "budget_settlement_health", return_value={
            "pending": 0, "oldest_seconds": 0, "repeatedly_failed": 0,
            "unresolved": 0, "terminal_unresolved": 0, "oldest_unresolved_seconds": 0,
        }),
        patch("functions.telemetry.function_app.AtomicTableLedger") as store,
        patch("functions.telemetry.function_app.BudgetScopeService") as scopes,
        patch("functions.telemetry.function_app.BudgetRecoveryService") as recovery,
        patch("functions.telemetry.function_app.BudgetSettlementService") as settlement,
    ):
        settlement.return_value.drain.return_value = 1
        if fails:
            settlement.return_value.drain.side_effect = RuntimeError("drain failed")
            with pytest.raises(RuntimeError, match="drain failed"):
                settle_budget_requests.build().get_user_function()(None)
        else:
            settle_budget_requests.build().get_user_function()(None)
        scopes.assert_called_once_with(repository, store.return_value.__enter__.return_value)
        scopes.return_value.prepare.assert_called_once_with()
        assert [call[0] for call in recovery.return_value.method_calls] == [
            "policies", "recover", "completions", "finalize_uncertain",
        ]
        store.return_value.__exit__.assert_called_once()
        close.assert_called_once_with()


def test_event_hub_batch_annotation_is_available_at_runtime() -> None:
    user_function = process_usage_events.build().get_user_function()
    annotation = signature(user_function).parameters["events"].annotation

    assert annotation == typing.List[func.EventHubEvent]  # noqa: UP006
    assert get_origin(annotation) is list
    assert get_args(annotation) == (func.EventHubEvent,)


def test_telemetry_health_proves_the_python_worker_is_running() -> None:
    response = telemetry_health.build().get_user_function()(None)

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.get_body() == b'{"status": "ok"}'


@pytest.mark.parametrize(
    "workspace,enabled", [(None, True), ("workspace", False), ("workspace", True)]
)
@pytest.mark.parametrize("fails", (False, True))
def test_ledger_timer_only_enables_evidence_when_configured(
    workspace: str | None, enabled: bool, fails: bool,
) -> None:
    settings = Settings.model_construct(
        ledger_sync_enabled=True,
        ledger_table_endpoint="https://ledger.example.com",
        log_analytics_workspace_id=workspace,
        reconciliation_enabled=enabled,
    )
    repository = MagicMock()
    repository.roll_forward_budgets.return_value = None
    repository.roll_forward_gateway_application_budgets.return_value = 0
    with (
        patch("functions.telemetry.function_app.get_settings", return_value=settings),
        patch("functions.telemetry.function_app.create_repository", return_value=repository),
        patch("functions.telemetry.function_app.TableStorageLedger") as store,
        patch("functions.telemetry.function_app.LedgerSyncService") as service,
        patch(
            "functions.telemetry.function_app.LogAnalyticsReservationTerminalLog"
        ) as log,
    ):
        if fails:
            service.return_value.run.side_effect = RuntimeError("ledger failure")
            with pytest.raises(RuntimeError, match="ledger failure"):
                sync_budget_ledger.build().get_user_function()(None)
        else:
            sync_budget_ledger.build().get_user_function()(None)
        repository.close.assert_called_once_with()
        assert log.call_count == (1 if workspace else 0)
        assert service.call_args.args == (repository, store.return_value.__enter__.return_value)
        assert service.call_args.kwargs["terminal_log"] == (
            log.return_value if workspace else None
        )
        assert service.call_args.kwargs["recovery_lag"] == timedelta(minutes=10)
        assert service.call_args.kwargs["finalization_lag"] == timedelta(hours=24)


@pytest.mark.parametrize(
    "failing",
    (
        "roll_forward_budgets",
        "roll_forward_gateway_application_budgets",
        "list_gateway_applications",
    ),
)
def test_ledger_timer_projects_even_when_an_earlier_step_fails(failing: str) -> None:
    settings = Settings.model_construct(
        ledger_sync_enabled=True,
        ledger_table_endpoint="https://ledger.example.com",
        log_analytics_workspace_id=None,
    )
    repository = MagicMock()
    repository.roll_forward_budgets.return_value = None
    repository.roll_forward_gateway_application_budgets.return_value = 0
    getattr(repository, failing).side_effect = RuntimeError("earlier step failure")
    with (
        patch("functions.telemetry.function_app.get_settings", return_value=settings),
        patch("functions.telemetry.function_app.create_repository", return_value=repository),
        patch("functions.telemetry.function_app.TableStorageLedger"),
        patch("functions.telemetry.function_app.LedgerSyncService") as service,
        pytest.raises(ExceptionGroup) as failed,
    ):
        sync_budget_ledger.build().get_user_function()(None)
    assert failed.group_contains(RuntimeError, match="earlier step failure")
    assert len(failed.value.exceptions) == 1
    repository.roll_forward_budgets.assert_called_once()
    repository.roll_forward_gateway_application_budgets.assert_called_once()
    repository.list_gateway_applications.assert_called_once_with()
    service.return_value.run.assert_called_once_with()
    repository.close.assert_called_once_with()


@pytest.mark.parametrize("enabled,endpoint", [(False, "https://ledger.example.com"), (True, None)])
def test_ledger_timer_closes_repository_on_early_return(
    enabled: bool, endpoint: str | None,
) -> None:
    repository = MagicMock()
    repository.roll_forward_budgets.return_value = None
    repository.roll_forward_gateway_application_budgets.return_value = 0
    settings = Settings.model_construct(ledger_sync_enabled=enabled, ledger_table_endpoint=endpoint)
    with (
        patch("functions.telemetry.function_app.get_settings", return_value=settings),
        patch("functions.telemetry.function_app.create_repository", return_value=repository),
    ):
        sync_budget_ledger.build().get_user_function()(None)
    repository.close.assert_called_once_with()


@pytest.mark.parametrize("fails", (False, True))
def test_event_batch_closes_repository_without_swallowing_processing_failure(fails: bool) -> None:
    repository = MagicMock()
    event = MagicMock()
    event.get_body.return_value = b'{"model":"test"}'
    with (
        patch(
            "functions.telemetry.function_app.get_settings",
            return_value=Settings.model_construct(),
        ),
        patch("functions.telemetry.function_app.create_repository", return_value=repository),
        patch("functions.telemetry.function_app.UsageProcessor") as processor,
    ):
        processor.return_value.close.side_effect = repository.close
        if fails:
            processor.return_value.process.side_effect = RuntimeError("batch failure")
            with pytest.raises(RuntimeError, match="batch failure"):
                process_usage_events.build().get_user_function()([event])
        else:
            process_usage_events.build().get_user_function()([event])
        processor.return_value.process.assert_called_once_with({"model":"test"})
    repository.close.assert_called_once_with()


@pytest.mark.parametrize("fails", (False, True))
def test_reconciliation_closes_repository(fails: bool) -> None:
    repository = MagicMock()
    settings = Settings.model_construct(
        reconciliation_enabled=True, log_analytics_workspace_id="workspace",
    )
    with (
        patch("functions.telemetry.function_app.get_settings", return_value=settings),
        patch("functions.telemetry.function_app.create_repository", return_value=repository),
        patch("functions.telemetry.function_app.ReconciliationService") as service,
        patch("functions.telemetry.function_app.CacheReadSyncService"),
        patch("functions.telemetry.function_app.LogAnalyticsGatewayUsageLog"),
        patch("functions.telemetry.function_app.LogAnalyticsCacheReadLog"),
    ):
        if fails:
            service.return_value.run.side_effect = RuntimeError("reconciliation failure")
            with pytest.raises(RuntimeError, match="reconciliation failure"):
                reconcile_stream_usage.build().get_user_function()(None)
        else:
            reconcile_stream_usage.build().get_user_function()(None)
    repository.close.assert_called_once_with()
