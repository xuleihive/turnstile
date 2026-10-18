from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest

from scripts.budget_installation_activation import (
    ACTIVATION_OPERATION,
    BudgetActivationReadback,
    FenceReadback,
    activate_budget_installation,
    recover_budget_activation,
)
from scripts.install_budget_execution import BudgetInstallationPlan, InstallationJournal
from tests.platform.infrastructure.test_budget_installation import GATEWAY, NOW, plan
from turnstile_core.domain.budget_completion import usd_completion_charge
from turnstile_core.domain.gateway_targets import canonical_usage_id_v2
from turnstile_core.domain.ledger import (
    BudgetPriceProjection,
    LedgerAttemptIdentity,
    LedgerScope,
)
from turnstile_core.domain.models import ModelPrice

SHARED_POLICY = "8" * 64
PERSON = "unit-person"
APPLICATION = UUID(int=10)
MODEL = UUID(int=3)
PERSON_SCOPE = LedgerScope(
    scope_type="person", scope_id=PERSON, period_start=NOW.date().replace(day=1),
)
APPLICATION_SCOPE = LedgerScope(
    scope_type="application", scope_id=str(APPLICATION), period_start=NOW.date().replace(day=1),
)
SCOPES = [PERSON_SCOPE, APPLICATION_SCOPE]


def activation_plan() -> BudgetInstallationPlan:
    result = plan()
    fence = result.expected_fence.model_dump()
    fence.update(
        validation_revision="candidate", validation_person_id=PERSON,
        validation_application_id=APPLICATION, validation_subscription_id="qa-usd-standard",
    )
    fence["model_contracts"]["unit"]["verified_at"] = NOW - timedelta(minutes=10)
    return BudgetInstallationPlan.model_validate({**result.model_dump(), "expected_fence": fence})


def readback_document() -> dict[str, Any]:
    configuration = activation_plan()
    admitted = NOW - timedelta(seconds=5)
    completed = NOW - timedelta(seconds=3)
    price = BudgetPriceProjection.create(MODEL, ModelPrice(
        input_price_per_million=10, cached_price_per_million=1,
        cache_write_price_per_million=20, output_price_per_million=30,
    ), NOW - timedelta(minutes=20))
    evidence = {
        "source": "observer", "observed_at": completed.isoformat(),
        "input_tokens": 10, "cached_tokens": 20, "cache_write_tokens": 5,
        "output_tokens": 30, "total_tokens": 60,
    }
    amount = usd_completion_charge(price.model_dump(mode="json"), evidence)
    usage_id = canonical_usage_id_v2(GATEWAY, "unit-apim", "managed", "actual-correlation")
    receipts, attempts, scopes = [], [], []
    for index, scope in enumerate(SCOPES):
        mode = "tokens" if index == 0 else "usd"
        identity = LedgerAttemptIdentity(
            scope=scope, gateway_profile_id=GATEWAY, native_gateway_id="managed",
            correlation_id="actual-correlation", model_id=str(MODEL), admitted_at=admitted,
        )
        receipt = {
            "identity": identity.model_dump(), "epoch": configuration.epoch,
            "reserved_tokens": 1000, "budget_mode": mode,
            **({"reserved_usd": "0.1", "price_snapshot": price.model_dump()} if index else {}),
        }
        receipts.append(receipt)
        attempt = {
            **receipt, "state": "settled", "actual_tokens": 60,
            "evidence_revision": 1, "evidence_key": "completion:actual",
            "evidence_rank": 3, "completion_evidence": evidence,
            "updated_at": completed,
            **({"actual_usd": amount, "price_etag": "price-etag"} if index else {}),
        }
        charge = {
            "identity": identity.model_dump(), "epoch": configuration.epoch,
            "revision": 1, "evidence_key": "observer:" + usage_id,
            "total_tokens": 60, "budget_mode": mode, "evidence_rank": 3,
            **({"actual_usd": amount, "price_snapshot": price.model_dump()} if index else {}),
        }
        attempts.append({
            "attempt": attempt, "attempt_etag": f"attempt-{index}",
            "sql_receipt": receipt, "sql_dispatch_id": UUID(int=12),
            "sql_caller_request_id": "operator-request-id", "sql_usage_id": usage_id,
            "sql_usage_complete": True,
            "settlement": {"id": index + 1, "charge": charge, "applied_revision": 1,
                           "applied_at": completed, "lease_until": None, "last_error": None},
        })
        summary = {
            "scope": scope.model_dump(), "epoch": configuration.epoch, "version": 3,
            "policy_revision": 2, "budget_mode": mode, "token_limit": 0 if index else 5000,
            "usd_limit": "10" if index else None, "enabled": True, "enforce": True,
            "confirmed_tokens": 123 if index == 0 else 0,
            "confirmed_usd": amount if index else 0, "updated_at": completed,
        }
        scopes.append({
            "observed_at": NOW, "scope": scope.model_dump(), "epoch": configuration.epoch,
            "state": "active", "budget_mode": mode, "policy_revision": 2,
            "policy_applied_revision": 2, "desired_limit": summary["token_limit"],
            "desired_usd_limit": summary["usd_limit"], "desired_enforce": True,
            "desired_enabled": True, "sql_confirmed_tokens": 123 if index == 0 else 60,
            "sql_confirmed_usd": amount if index else None,
            "sql_pending_requests": 0, "sql_upper_bound_rows": 0, "sql_pending_settlements": 0,
            "summary": summary, "summary_etag": f"summary-{index}",
        })
    return {
        "observed_at": NOW,
        "fence": {"observed_at": NOW, "etag": "paused-etag",
                  "document": configuration.expected_fence.model_dump(mode="json")},
        "current_revision": "candidate", "shared_policy_sha256": SHARED_POLICY,
        "revision_policy_sha256": configuration.policy_sha256,
        "reachable_revisions": ["old", "candidate"], "fenced_revisions": ["old", "candidate"],
        "package_sha256": configuration.package_sha256,
        "rollout_epoch": configuration.epoch, "rollout_state": "active",
        "scope_sync_enabled": True, "prepared_period": PERSON_SCOPE.period_start,
        "gateway_profile_ids": [GATEWAY], "pending_publications": 0,
        "active_dispatches": 0, "pending_telemetry": 0, "pending_settlements": 0,
        "unresolved_dispatched_attempts": 0, "scopes": scopes,
        "mixed_attempts": [{
            "observed_at": NOW, "operation": "responses", "dispatch_etag": "dispatch-etag",
            "dispatch": {"owner": UUID(int=12), "caller_request_id": "operator-request-id",
                         "receipts": receipts, "state": "dispatching", "recovery_complete": True,
                         "expires_at": admitted + timedelta(seconds=45), "updated_at": completed},
            "person": attempts[0], "application": attempts[1],
            "usage": {
                "id": usage_id, "correlation_id": "actual-correlation", "model_id": MODEL,
                "user_id": PERSON, "ts": completed, "status_code": 200, "usage_domain": "apim",
                "ingest_source": "eventhub", "estimated": False, "ingest_error": None,
                "input_tokens": 10, "cached_tokens": 20, "cache_write_tokens": 5,
                "output_tokens": 30, "usd_cost": amount, "usd_price_sha256": price.price_sha256,
                "gateway": {
                    "usage_id": usage_id, "apim_target_id": GATEWAY, "native_gateway_id": "managed",
                    "is_managed": True, "apim_service_id": "unit-apim", "apim_region": "test",
                    "api_revision": "candidate", "event_schema_version": 2,
                    "attribution_source": "observer", "observed_at": completed,
                },
                "application": {
                    "application_id": APPLICATION, "application_subscription_id": UUID(int=11),
                    "application_name_snapshot": "QA USD",
                    "apim_subscription_id": "qa-usd-standard",
                    "actor_type": "person", "actor_id": PERSON, "person_id": PERSON,
                },
            },
        }],
    }


def assert_ready(document: dict[str, Any]) -> None:
    BudgetActivationReadback.model_validate(document).require_ready(
        activation_plan(), SCOPES, "candidate", SHARED_POLICY, NOW,
    )


def test_real_mixed_units_use_current_sql_not_stale_legacy_c() -> None:
    document = readback_document()
    scope = document["scopes"][1]
    assert scope["sql_confirmed_tokens"] != scope["summary"]["confirmed_tokens"]
    assert_ready(document)


@pytest.mark.parametrize(("field", "value"), [
    ("observed_at", NOW - timedelta(seconds=31)), ("observed_at", NOW + timedelta(seconds=1)),
    ("current_revision", "old"), ("shared_policy_sha256", "9" * 64),
    ("reachable_revisions", ["candidate"]), ("fenced_revisions", ["candidate"]),
    ("reachable_revisions", ["candidate", "old", "old"]),
    ("rollout_epoch", UUID(int=99)), ("rollout_state", "paused"), ("scope_sync_enabled", False),
    ("prepared_period", NOW.date()), ("gateway_profile_ids", [GATEWAY, GATEWAY]),
    ("pending_publications", 1), ("active_dispatches", 1), ("pending_telemetry", 1),
    ("pending_settlements", 1), ("unresolved_dispatched_attempts", 1), ("mixed_attempts", []),
])
def test_activation_rejects_missing_or_stale_global_evidence(field: str, value: Any) -> None:
    document = readback_document()
    document[field] = value
    with pytest.raises(ValueError):
        assert_ready(document)


@pytest.mark.parametrize(
    "kind", ["package", "policy", "fence", "etag", "scope-missing", "scope-repeat"],
)
def test_activation_rejects_topology_and_scope_drift(kind: str) -> None:
    document = readback_document()
    if kind == "package":
        document["package_sha256"]["observer"] = "9" * 64
    elif kind == "policy":
        document["revision_policy_sha256"]["old"] = "9" * 64
    elif kind == "fence":
        document["fence"]["document"]["delegation"]["audience"] = "api://other"
    elif kind == "etag":
        document["fence"]["etag"] = "*"
    elif kind == "scope-missing":
        document["scopes"].pop()
    else:
        document["scopes"].append(document["scopes"][0])
    with pytest.raises(ValueError):
        assert_ready(document)


@pytest.mark.parametrize(("field", "value"), [
    ("state", "prepared"), ("policy_applied_revision", 1), ("desired_enabled", False),
    ("desired_limit", 4000), ("sql_confirmed_tokens", 122), ("sql_pending_requests", 1),
    ("sql_upper_bound_rows", 1), ("sql_pending_settlements", 1), ("summary_etag", "*"),
    ("observed_at", NOW - timedelta(seconds=31)),
])
def test_every_scope_requires_live_applied_policy_and_exact_balance(field: str, value: Any) -> None:
    document = readback_document()
    document["scopes"][0][field] = value
    with pytest.raises(ValueError):
        assert_ready(document)


@pytest.mark.parametrize("field", ["active_reserved_tokens", "upper_bound_tokens",
                                  "active_reserved_usd", "upper_bound_usd"])
def test_any_table_charge_prevents_activation(field: str) -> None:
    document = readback_document()
    scope = document["scopes"][1 if field.endswith("usd") else 0]
    scope["summary"][field] = 1
    with pytest.raises(ValueError):
        assert_ready(document)


@pytest.mark.parametrize("value", [None, Decimal("0.5")])
def test_usd_sql_must_be_priced_and_exactly_agree(value: Decimal | None) -> None:
    document = readback_document()
    document["scopes"][1]["sql_confirmed_usd"] = value
    with pytest.raises(ValueError):
        assert_ready(document)


def test_sql_settlement_fallback_does_not_require_direct_observer_completion() -> None:
    document = readback_document()
    for name in ("person", "application"):
        document["mixed_attempts"][0][name]["attempt"].pop("completion_evidence")
    assert_ready(document)


def test_matching_fabricated_usd_totals_still_fail_the_original_price_calculation() -> None:
    document = readback_document()
    mixed = document["mixed_attempts"][0]
    mixed["usage"]["usd_cost"] = "0.5"
    mixed["application"]["attempt"]["actual_usd"] = "0.5"
    mixed["application"]["settlement"]["charge"]["actual_usd"] = "0.5"
    document["scopes"][1]["summary"]["confirmed_usd"] = "0.5"
    document["scopes"][1]["sql_confirmed_usd"] = "0.5"
    with pytest.raises(ValueError, match="admitted price"):
        assert_ready(document)


@pytest.mark.parametrize("kind", ["estimated", "price", "cost", "usage-id", "revision", "person",
                                  "model", "subscription", "backfill", "empty", "only-token"])
def test_mixed_validation_requires_formal_same_attempt_monetary_evidence(kind: str) -> None:
    document = readback_document()
    mixed = document["mixed_attempts"][0]
    usage = mixed["usage"]
    if kind == "estimated":
        usage["estimated"] = True
    elif kind == "price":
        usage["usd_price_sha256"] = "9" * 64
    elif kind == "cost":
        usage["usd_cost"] = "0.5"
    elif kind == "usage-id":
        usage["id"] = "not-formal-usage"
    elif kind == "revision":
        usage["gateway"]["api_revision"] = "old"
    elif kind == "person":
        usage["user_id"] = "old-email@example.test"
    elif kind == "model":
        usage["model_id"] = UUID(int=99)
    elif kind == "subscription":
        usage["application"]["apim_subscription_id"] = "real-dashboard"
    elif kind == "backfill":
        usage["gateway"]["attribution_source"] = "backfill"
    elif kind == "empty":
        usage["usd_cost"] = "0"
    else:
        mixed["application"] = mixed["person"]
    with pytest.raises(ValueError):
        assert_ready(document)


@pytest.mark.parametrize("kind", ["sql-receipt", "sql-dispatch", "sql-complete", "sql-usage",
                                  "task-pending", "task-lease", "task-error", "task-usd",
                                  "task-tokens", "dispatch-pending", "unverified", "fake-settled"])
def test_sql_receipts_and_applied_tasks_must_match_table(kind: str) -> None:
    document = readback_document()
    mixed = document["mixed_attempts"][0]
    observed = mixed["application"]
    if kind == "sql-receipt":
        observed["sql_receipt"] = deepcopy(observed["sql_receipt"])
        observed["sql_receipt"]["reserved_usd"] = "0.2"
    elif kind == "sql-dispatch":
        observed["sql_dispatch_id"] = UUID(int=99)
    elif kind == "sql-complete":
        observed["sql_usage_complete"] = False
    elif kind == "sql-usage":
        observed["sql_usage_id"] = "unrelated"
    elif kind == "task-pending":
        observed["settlement"]["applied_revision"] = 0
    elif kind == "task-lease":
        observed["settlement"]["lease_until"] = NOW + timedelta(seconds=60)
    elif kind == "task-error":
        observed["settlement"]["last_error"] = "failed"
    elif kind == "task-usd":
        observed["settlement"]["charge"]["actual_usd"] = "0.5"
    elif kind == "task-tokens":
        observed["settlement"]["charge"]["total_tokens"] = 61
    elif kind == "dispatch-pending":
        mixed["dispatch"]["recovery_complete"] = False
    elif kind == "unverified":
        observed["settlement"]["charge"]["evidence_rank"] = 2
    else:
        document["settled"] = True
    with pytest.raises(ValueError):
        assert_ready(document)


def run_activation(
    journal: InstallationJournal, document: dict[str, Any], state: dict[str, Any],
    write: Mock | None = None,
) -> dict[str, Any]:
    def read_fence() -> FenceReadback:
        return FenceReadback.model_validate(state)

    def replace(partition: str, etag: str, active: dict[str, Any]) -> bool:
        assert partition == activation_plan().expected_fence.partition and etag == "paused-etag"
        if write is not None:
            write(partition, etag, active)
        state.update(etag="active-etag", document=active)
        return True

    return activate_budget_installation(
        activation_plan(), journal, expected_scopes=SCOPES,
        promoted_revision="candidate", shared_policy_sha256=SHARED_POLICY,
        observe=lambda: BudgetActivationReadback.model_validate(document),
        replace_fence=replace, read_fence=read_fence, clock=lambda: NOW,
    )


def test_activation_writes_exact_etag_and_preserves_nonvalidation_fields(tmp_path: Path) -> None:
    document = readback_document()
    original = deepcopy(document["fence"]["document"])
    state, write = deepcopy(document["fence"]), Mock()
    with InstallationJournal(tmp_path / "journal.json", activation_plan()) as journal:
        intent = run_activation(journal, document, state, write)
        assert write.call_count == 1
        expected = {key: value for key, value in original.items()
                    if not key.startswith("validation_")}
        expected["mode"] = "active"
        assert state["document"] == expected == intent["active_document"]
        events = json.loads(journal.path.read_text())["events"]
        assert [event["phase"] for event in events] == ["intent", "verified"]
        with pytest.raises(ValueError):
            run_activation(journal, document, state, write)
        assert write.call_count == 1


def test_failed_financial_proof_never_calls_write_or_records_intent(tmp_path: Path) -> None:
    document = readback_document()
    document["scopes"][1]["sql_confirmed_usd"] = "9"
    write = Mock()
    with InstallationJournal(tmp_path / "journal.json", activation_plan()) as journal:
        with pytest.raises(ValueError):
            run_activation(journal, document, deepcopy(document["fence"]), write)
        assert json.loads(journal.path.read_text())["events"] == []
    write.assert_not_called()


def test_changed_fence_before_put_does_not_write(tmp_path: Path) -> None:
    document = readback_document()
    changed = deepcopy(document["fence"])
    changed["etag"] = "another-writer"
    write = Mock()
    with (
        InstallationJournal(tmp_path / "journal.json", activation_plan()) as journal,
        pytest.raises(ValueError, match="changed before activation"),
    ):
        run_activation(journal, document, changed, write)
    write.assert_not_called()


def test_slow_last_fence_read_cannot_make_expired_financial_proof_usable(tmp_path: Path) -> None:
    document = readback_document()
    moment = [NOW]
    write = Mock(return_value=True)

    def read_fence() -> FenceReadback:
        moment[0] = NOW + timedelta(seconds=31)
        return FenceReadback.model_validate({**document["fence"], "observed_at": moment[0]})

    with (
        InstallationJournal(tmp_path / "journal.json", activation_plan()) as journal,
        pytest.raises(ValueError, match="30 seconds"),
    ):
        activate_budget_installation(
            activation_plan(), journal, expected_scopes=SCOPES,
            promoted_revision="candidate", shared_policy_sha256=SHARED_POLICY,
            observe=lambda: BudgetActivationReadback.model_validate(document),
            replace_fence=write, read_fence=read_fence, clock=lambda: moment[0],
        )
    write.assert_not_called()


def test_unknown_outcome_is_recovered_by_readback_without_replaying_write(tmp_path: Path) -> None:
    document = readback_document()
    state = deepcopy(document["fence"])
    calls = 0

    def lost_reply(partition: str, etag: str, active: dict[str, Any]) -> bool:
        nonlocal calls
        calls += 1
        assert etag == "paused-etag"
        state.update(etag="active-etag", document=active)
        raise TimeoutError("Reply lost after the server committed F")

    path = tmp_path / "journal.json"
    with InstallationJournal(path, activation_plan()) as journal:
        with pytest.raises(TimeoutError):
            activate_budget_installation(
                activation_plan(), journal, expected_scopes=SCOPES,
                promoted_revision="candidate", shared_policy_sha256=SHARED_POLICY,
                observe=lambda: BudgetActivationReadback.model_validate(document),
                replace_fence=lost_reply, read_fence=lambda: FenceReadback.model_validate(state),
                clock=lambda: NOW,
            )
        assert len(json.loads(path.read_text())["events"]) == 1
    with InstallationJournal.resume(path, activation_plan()) as resumed:
        recovered = recover_budget_activation(
            activation_plan(), resumed, read_fence=lambda: FenceReadback.model_validate(state),
            clock=lambda: NOW,
        )
        assert recovered["active_document"] == state["document"] and calls == 1
        event = json.loads(path.read_text())["events"][-1]
        assert event["operation"] == ACTIVATION_OPERATION and event["recovered_without_replay"]


def test_unresolved_prior_write_blocks_activation(tmp_path: Path) -> None:
    document = readback_document()
    write = Mock()
    with InstallationJournal(tmp_path / "journal.json", activation_plan()) as journal:
        with pytest.raises(TimeoutError):
            journal.perform(
                "prior", intent={}, apply=Mock(side_effect=TimeoutError), readback=lambda: {},
            )
        with pytest.raises(ValueError, match="unresolved external write"):
            run_activation(journal, document, deepcopy(document["fence"]), write)
    write.assert_not_called()


@pytest.mark.parametrize("failure", ["etag-conflict", "lost-readback", "expired-proof"])
def test_activation_failure_keeps_an_unresolved_intent_and_never_retries(
    tmp_path: Path, failure: str,
) -> None:
    document = readback_document()
    state = deepcopy(document["fence"])
    calls = 0

    def write(partition: str, etag: str, active: dict[str, Any]) -> bool:
        nonlocal calls
        calls += 1
        if failure == "etag-conflict":
            return False
        state.update(etag="active-etag", document=active)
        return True

    def read_fence() -> FenceReadback:
        if failure == "lost-readback" and calls:
            raise TimeoutError("Readback interrupted")
        return FenceReadback.model_validate(state)

    clock = Mock(side_effect=[NOW, NOW + timedelta(seconds=31)]) if failure == "expired-proof" \
        else lambda: NOW
    with InstallationJournal(tmp_path / "journal.json", activation_plan()) as journal:
        with pytest.raises((ValueError, TimeoutError)):
            activate_budget_installation(
                activation_plan(), journal, expected_scopes=SCOPES,
                promoted_revision="candidate", shared_policy_sha256=SHARED_POLICY,
                observe=lambda: BudgetActivationReadback.model_validate(document),
                replace_fence=write, read_fence=read_fence, clock=clock,
            )
        assert calls == (0 if failure == "expired-proof" else 1)
        assert len(json.loads(journal.path.read_text())["events"]) == 1
        if failure != "expired-proof":
            with pytest.raises(ValueError):
                recover_budget_activation(
                    activation_plan(), journal,
                    read_fence=lambda: FenceReadback.model_validate(document["fence"]),
                    clock=lambda: NOW,
                )


def test_recovery_can_confirm_a_committed_write_after_plan_expiry(tmp_path: Path) -> None:
    document = readback_document()
    state = deepcopy(document["fence"])

    def write(partition: str, etag: str, active: dict[str, Any]) -> bool:
        state.update(etag="active-etag", document=active)
        raise TimeoutError("Unknown reply")

    with InstallationJournal(tmp_path / "journal.json", activation_plan()) as journal:
        with pytest.raises(TimeoutError):
            activate_budget_installation(
                activation_plan(), journal, expected_scopes=SCOPES,
                promoted_revision="candidate", shared_policy_sha256=SHARED_POLICY,
                observe=lambda: BudgetActivationReadback.model_validate(document),
                replace_fence=write, read_fence=lambda: FenceReadback.model_validate(state),
                clock=lambda: NOW,
            )
        later = NOW + timedelta(hours=2)
        state["observed_at"] = later
        result = recover_budget_activation(
            activation_plan(), journal, read_fence=lambda: FenceReadback.model_validate(state),
            clock=lambda: later,
        )
        assert result["active_document"]["mode"] == "active"
