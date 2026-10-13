import subprocess
import sys
import typing
from datetime import UTC, timedelta
from inspect import signature
from pathlib import Path
from typing import get_args, get_origin
from unittest.mock import MagicMock, patch

import azure.functions as func
import pytest

from functions.telemetry.function_app import (
    app,
    process_usage_events,
    process_usage_export_jobs,
    sync_budget_ledger,
    telemetry_health,
)
from turnstile_core.config import Settings

ROOT = Path(__file__).resolve().parents[3]
FUNCTIONS = app.get_functions()


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

assert len(function_app.app.get_functions()) == 6
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
    for function in FUNCTIONS:
        trigger = function.get_trigger()
        parameters = signature(function.get_user_function()).parameters

        assert trigger is not None
        assert trigger.name in parameters


def test_security_audit_retention_is_a_daily_0100_utc_timer() -> None:
    function = next(
        item for item in FUNCTIONS if item.get_function_name() == "cleanup_security_audit_events"
    )
    trigger = function.get_trigger()

    assert type(trigger).__name__ == "TimerTrigger"
    assert trigger.schedule == "0 0 1 * * *"


def test_usage_export_worker_runs_on_a_one_minute_utc_timer() -> None:
    function = next(
        item for item in FUNCTIONS if item.get_function_name() == "process_usage_export_jobs"
    )
    trigger = function.get_trigger()

    assert type(trigger).__name__ == "TimerTrigger"
    assert trigger.schedule == "0 * * * * *"


def test_usage_export_timer_runs_a_single_job_with_a_utc_timestamp() -> None:
    class Timer:
        past_due = False

    worker = MagicMock()
    with patch("turnstile_core.services.usage_export_worker.create_usage_export_worker") as factory:
        factory.return_value = worker
        process_usage_export_jobs.build().get_user_function()(Timer())

    factory.assert_called_once()
    worker.run_once.assert_called_once()
    assert worker.run_once.call_args.args[0]
    assert worker.run_once.call_args.args[1].tzinfo is UTC


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
def test_ledger_timer_only_enables_evidence_when_configured(
    workspace: str | None, enabled: bool
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
        patch("turnstile_core.config.get_settings", return_value=settings),
        patch("turnstile_core.persistence.factory.create_repository", return_value=repository),
        patch("turnstile_core.integrations.ledger.TableStorageLedger") as store,
        patch("turnstile_core.integrations.ledger.LedgerSyncService") as service,
        patch(
            "turnstile_core.integrations.reconciliation.LogAnalyticsReservationTerminalLog"
        ) as log,
    ):
        sync_budget_ledger.build().get_user_function()(None)
        assert log.call_count == (1 if workspace and enabled else 0)
        assert service.call_args.args == (repository, store.return_value.__enter__.return_value)
        assert service.call_args.kwargs["terminal_log"] == (
            log.return_value if workspace and enabled else None
        )
        assert service.call_args.kwargs["recovery_lag"] == timedelta(minutes=10)
        assert service.call_args.kwargs["finalization_lag"] == timedelta(hours=24)
