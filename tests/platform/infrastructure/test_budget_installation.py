from __future__ import annotations

import json
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from scripts.install_budget_execution import (
    BudgetInstallationPlan,
    InstallationJournal,
    MaintenanceReadback,
    initialize_rollout,
    materialize_shared_fence,
)
from scripts.prepare_budget_execution import FenceConfiguration
from turnstile_core.integrations.apim_policy_components import serialize_policy

NOW = datetime(2026, 10, 17, tzinfo=UTC)
EPOCH = UUID(int=1)
GATEWAY = UUID(int=2)


def test_shared_fence_materializes_the_deny_logger_and_gateway_identity() -> None:
    node = materialize_shared_fence(["unit-api"], {
        "__LEDGER_TABLE_ENDPOINT__": "https://unit.table.core.windows.net",
        "__LEDGER_TABLE_NAME__": "UnitLedger", "__LOGGER_ID__": "existing-logger",
        "__GATEWAY_PROFILE_ID__": str(GATEWAY),
    })
    assert not re.search(r"__[A-Z][A-Z0-9_]*__", serialize_policy(node))
    events = list(node.iter("log-to-eventhub"))
    assert events and all(event.get("logger-id") == "existing-logger" for event in events)
    assert str(GATEWAY) in serialize_policy(node)


def test_shared_fence_refuses_url_only_binding_and_nested_placeholders() -> None:
    with pytest.raises(ValueError, match="missing bindings"):
        materialize_shared_fence(["unit-api"], {
            "__LEDGER_TABLE_ENDPOINT__": "https://unit.table.core.windows.net",
            "__LEDGER_TABLE_NAME__": "UnitLedger",
        })
    with pytest.raises(ValueError, match="unresolved parameters"):
        materialize_shared_fence(["unit-api"], {
            "__LEDGER_TABLE_ENDPOINT__": "https://unit.table.core.windows.net",
            "__LEDGER_TABLE_NAME__": "UnitLedger", "__LOGGER_ID__": "__OTHER_LOGGER__",
            "__GATEWAY_PROFILE_ID__": str(GATEWAY),
        })


def fence() -> FenceConfiguration:
    return FenceConfiguration.model_validate({
        "gateway_profile_id": GATEWAY, "apim_service_id": "unit-apim", "api_id": "unit-api",
        "epoch": EPOCH, "mode": "paused", "gateways": {"managed": ["candidate"]},
        "delegation": {
            "tenant_id": UUID(int=4), "audience": "api://unit",
            "client_ids": [UUID(int=5)], "object_ids": [UUID(int=6)],
        },
        "model_contracts": {"unit": {
            "model_id": UUID(int=3), "algorithm": "request_total_ceiling_v1",
            "output_field": "none", "request_token_ceiling": 1000,
            "evidence_sha256": "6" * 64, "verified_at": NOW,
        }},
    })


def plan() -> BudgetInstallationPlan:
    return BudgetInstallationPlan(
        epoch=EPOCH, gateway_profile_ids=[GATEWAY], period_start=NOW.replace(day=1),
        source_commit="1" * 40,
        expected_fence=fence(),
        package_sha256={key: "2" * 64 for key in ("api", "telemetry", "control-plane", "observer")},
        policy_sha256={"old": "3" * 64, "candidate": "4" * 64},
        backup_manifest_sha256="5" * 64,
        created_at=NOW - timedelta(hours=1), expires_at=NOW + timedelta(hours=1),
    )


def observation() -> MaintenanceReadback:
    return MaintenanceReadback.model_validate({
        "observed_at": NOW, "fence_etag": "unit-etag",
        "fence": fence(),
        "revision_policy_sha256": plan().policy_sha256,
        "reachable_revisions": ["old", "candidate"], "fenced_revisions": ["old", "candidate"],
        "package_sha256": plan().package_sha256, "pending_publications": 0,
        "active_dispatches": 0, "pending_telemetry": 0, "pending_settlements": 0,
        "admission_fenced_at": NOW - timedelta(minutes=10), "drain_completed_at": NOW,
        "balances": {
            "observed_at": NOW, "old_admission_fenced": True,
            "all_gateway_revisions_verified": True, "old_inflight_drained": True,
            "compatible_readers_verified": True, "scopes": [{
                "summary": {
                    "epoch": EPOCH, "scope": {
                        "scope_type": "person", "scope_id": "unit-person",
                        "period_start": "2026-10-01",
                    },
                    "token_limit": 0, "enforce": True, "enabled": False,
                    "confirmed_tokens": 123, "updated_at": NOW,
                },
                "legacy_active_reservations": 0, "legacy_upper_bound_rows": 0,
                "legacy_confirmed_tokens": 123, "legacy_snapshot_sha256": "7" * 64,
                "observed_at": NOW,
            }],
        },
    })


def test_valid_observation_preserves_disabled_nonzero_opening_balance() -> None:
    observation().require_paused(plan(), NOW)


@pytest.mark.parametrize("changes", [
    {"fence_etag": "*"}, {"observed_at": NOW - timedelta(seconds=31)},
    {"observed_at": NOW + timedelta(seconds=1)}, {"pending_publications": 1},
    {"pending_telemetry": 1}, {"active_dispatches": 1}, {"pending_settlements": 1},
    {"reachable_revisions": ["old", "candidate", "forgotten"]},
    {"fenced_revisions": ["candidate"]},
    {"package_sha256": {**plan().package_sha256, "observer": "9" * 64}},
    {"revision_policy_sha256": {"old": "9" * 64, "candidate": "4" * 64}},
    {"drain_completed_at": NOW + timedelta(seconds=1)},
    {"admission_fenced_at": NOW - timedelta(minutes=5)},
])
def test_incomplete_or_drifted_readback_never_authorizes_initialization(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        observation().model_copy(update=changes).require_paused(plan(), NOW)


@pytest.mark.parametrize("mode", ["prepared", "active"])
def test_only_paused_fence_can_authorize_initialization(mode: str) -> None:
    current = observation()
    current.fence = current.fence.model_copy(update={"mode": mode})
    with pytest.raises(ValueError, match="paused"):
        current.require_paused(plan(), NOW)


@pytest.mark.parametrize("field,value", [
    ("legacy_active_reservations", 1), ("legacy_upper_bound_rows", 1),
    ("legacy_confirmed_tokens", 124),
])
def test_zero_reservation_rows_alone_is_not_sufficient(field: str, value: int) -> None:
    current = observation()
    current.balances.scopes[0] = current.balances.scopes[0].model_copy(update={field: value})
    with pytest.raises(ValueError):
        current.require_paused(plan(), NOW)


def test_seed_must_not_enable_admission() -> None:
    current = observation()
    current.balances.scopes[0].summary.enabled = True
    with pytest.raises(ValueError, match="disabled"):
        current.require_paused(plan(), NOW)


def test_plan_expires_and_cannot_cross_month() -> None:
    with pytest.raises(ValueError, match="current"):
        plan().require_current(NOW + timedelta(hours=1))
    future = plan().model_copy(update={
        "created_at": datetime(2026, 10, 31, 23, tzinfo=UTC),
        "expires_at": datetime(2026, 11, 1, 1, tzinfo=UTC),
    })
    with pytest.raises(ValueError, match="period boundary"):
        future.require_current(datetime(2026, 11, 1, tzinfo=UTC))


def test_personal_usd_plan_requires_the_trusted_backend_identity() -> None:
    raw = plan().model_dump(mode="json")
    raw["expected_fence"]["delegation"] = None
    with pytest.raises(ValueError, match="reviewed backend identity"):
        BudgetInstallationPlan.model_validate(raw)


def test_journal_is_private_and_unknown_write_is_never_replayed(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    journal = InstallationJournal(path, plan())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        InstallationJournal(path, plan())

    def unknown() -> None:
        raise TimeoutError("outcome unknown")

    with pytest.raises(TimeoutError):
        journal.perform("write-fence", intent={"mode": "paused"}, apply=unknown,
                        readback=lambda: {"mode": "paused"})
    assert json.loads(path.read_text())["events"][-1]["phase"] == "intent"
    with pytest.raises(ValueError, match="never replayed"):
        journal.perform("write-fence", intent={"mode": "paused"}, apply=lambda: None,
                        readback=lambda: {"mode": "paused"})


def test_journal_requires_exact_external_readback(tmp_path: Path) -> None:
    journal = InstallationJournal(tmp_path / "journal.json", plan())
    with pytest.raises(ValueError, match="differs"):
        journal.perform("pause", intent={"mode": "paused"}, apply=lambda: None,
                        readback=lambda: {"mode": "active"})
    with pytest.raises(ValueError, match="unresolved"):
        journal.perform("inspect", intent={"observed": True}, apply=lambda: None,
                        readback=lambda: {"observed": True})
    with pytest.raises(ValueError, match="different external"):
        journal.recover("pause", lambda: {"mode": "active"})
    journal.recover("pause", lambda: {"mode": "paused"})
    result = journal.perform("inspect", intent={"observed": True}, apply=lambda: None,
                             readback=lambda: {"observed": True})
    assert result == {"observed": True}


def test_fence_must_match_the_exact_target_and_model_contracts() -> None:
    current = observation()
    current.fence.api_id = "another-api"
    with pytest.raises(ValueError, match="identity or counting"):
        current.require_paused(plan(), NOW)


def test_fenced_but_unreviewed_revision_is_not_adopted() -> None:
    current = observation()
    current.reachable_revisions.append("unreviewed")
    current.fenced_revisions.append("unreviewed")
    current.revision_policy_sha256["unreviewed"] = "8" * 64
    with pytest.raises(ValueError, match="review every reachable"):
        current.require_paused(plan(), NOW)


def test_interrupted_journal_can_be_resumed_without_replaying_the_write(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    journal = InstallationJournal(path, plan())

    def interrupted() -> None:
        raise TimeoutError("lost response")

    with pytest.raises(TimeoutError):
        journal.perform("pause", intent={"mode": "paused"}, apply=interrupted,
                        readback=lambda: {"mode": "paused"})
    with pytest.raises(BlockingIOError):
        InstallationJournal.resume(path, plan())
    journal.close()
    resumed = InstallationJournal.resume(path, plan())
    try:
        assert resumed.recover("pause", lambda: {"mode": "paused"}) == {"mode": "paused"}
    finally:
        resumed.close()
    assert json.loads(path.read_text())["events"][-1]["recovered_without_replay"] is True


def test_recovery_refuses_insecure_files_and_other_plans(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    journal = InstallationJournal(path, plan())
    journal.close()
    other = plan().model_copy(update={"source_commit": "9" * 40})
    with pytest.raises(ValueError, match="another plan"):
        InstallationJournal.resume(path, other)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular"):
        InstallationJournal.resume(path, plan())


def connection(
    *, existing: bool = False, balance: int = 123,
    scopes: list[tuple[str, str]] | None = None,
    pending: int = 0, upper_bound: int = 0, target_registered: bool = True,
) -> MagicMock:
    db = MagicMock()

    def execute(statement: str, parameters: object = None) -> MagicMock:
        cursor = MagicMock()
        if "SELECT 1 FROM budget_execution_rollout" in statement:
            cursor.fetchone.return_value = (1,) if existing else None
        elif "SELECT count(*) FROM gateway_publication" in statement:
            cursor.fetchone.return_value = (0,)
        elif "SELECT source FROM directory_control_state" in statement:
            cursor.fetchone.return_value = ("database",)
        elif "SELECT target.gateway_profile_id" in statement:
            cursor.fetchone.return_value = (GATEWAY,) if target_registered else None
        elif "SELECT 'person', governance_user_id" in statement:
            cursor.fetchall.return_value = (
                scopes if scopes is not None else [("person", "unit-person")]
            )
        elif "SELECT budget_scope_confirmed_tokens" in statement:
            cursor.fetchone.return_value = (balance,)
        elif "SELECT budget_scope_pending_request_tokens" in statement:
            cursor.fetchone.return_value = (pending, upper_bound)
        return cursor

    db.execute.side_effect = execute
    return db


@pytest.mark.parametrize("options,match", [
    ({"existing": True}, "existing epoch"), ({"balance": 124}, "do not agree"),
    ({"scopes": [("person", "unit-person"), ("application", "forgotten")]}, "every authoritative"),
    ({"pending": 1}, "pending or conservatively"),
    ({"upper_bound": 1}, "pending or conservatively"),
    ({"target_registered": False}, "registered APIM identity"),
])
def test_rollout_refuses_existing_epoch_balance_drift_and_missing_scope(
    options: dict[str, Any], match: str,
) -> None:
    db = connection(**options)
    with pytest.raises(ValueError, match=match):
        initialize_rollout(db, plan(), observation(), now=NOW)
    assert not any("INSERT INTO budget_execution_rollout" in call.args[0]
                   for call in db.execute.call_args_list)


def test_rollout_creates_only_coordination_state_not_financial_balances() -> None:
    db = connection()
    initialize_rollout(db, plan(), observation(), now=NOW)
    writes = [call for call in db.execute.call_args_list if "INSERT" in call.args[0]]
    assert len(writes) == 1
    assert "INSERT INTO budget_execution_rollout" in writes[0].args[0]
    assert writes[0].args[1][0] == EPOCH
    assert not any("DELETE" in call.args[0] for call in db.execute.call_args_list)
