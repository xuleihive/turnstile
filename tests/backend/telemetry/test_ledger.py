from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from turnstile_core.domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
    GatewayApplicationLedgerState,
    UsageApplicationAttribution,
)
from turnstile_core.domain.ledger import BudgetReservationFinalization
from turnstile_core.domain.models import ReservationTerminalEvidence, TokenUsageRecord
from turnstile_core.integrations.ledger import (
    RESERVATION_PREFIX,
    ROLL_FORWARD_ACTOR,
    LedgerReservation,
    LedgerSyncService,
    TableStorageLedger,
    application_map_partition_key,
    application_partition_key,
    model_access_value,
    partition_key,
    period_start_for,
    reservation_row_key,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.application_access import ApplicationAccessService

PERIOD_START = datetime(2026, 7, 1, tzinfo=UTC)
USER = "test.user01@contoso.com"


def _finalization(**overrides: Any) -> BudgetReservationFinalization:
    return BudgetReservationFinalization.model_validate(
        {
            "scope_type": "person",
            "scope_id": USER,
            "period_start": PERIOD_START.date(),
            "correlation_id": "reservation-attempt",
            "reservation_created_at": PERIOD_START,
            "reservation_tokens": 900,
            "evidence_at": datetime(2026, 7, 2, tzinfo=UTC),
            "total_tokens": 900,
            "finalization_kind": "unverified_upper_bound",
            "source": "reservation_timeout",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_tokens": 0},
        {"input_tokens": 0},
        {"status_code": 200},
        {"source": "apim_gateway_log"},
        {"scope_id": " "},
        {"period_start": date(2026, 7, 2)},
        {"evidence_at": datetime(2026, 6, 30, tzinfo=UTC)},
        {"evidence_at": datetime(2026, 7, 2)},
    ],
)
def test_finalization_contract_rejects_unsafe_upper_bounds(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _finalization(**overrides)


def test_finalization_contract_distinguishes_evidence_strengths() -> None:
    assert _finalization().total_tokens == 900
    assert (
        _finalization(
            finalization_kind="exact_usage",
            source="apim_gateway_llm_log",
            input_tokens=70,
            output_tokens=30,
            total_tokens=100,
        ).total_tokens
        == 100
    )
    assert (
        _finalization(
            finalization_kind="terminal_zero",
            source="apim_gateway_log",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            status_code=429,
        ).total_tokens
        == 0
    )
    with pytest.raises(ValueError):
        _finalization(
            finalization_kind="terminal_zero",
            source="apim_gateway_log",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            status_code=200,
        )
    with pytest.raises(ValueError):
        _finalization(
            finalization_kind="exact_usage",
            source="apim_gateway_llm_log",
            input_tokens=70,
            output_tokens=30,
            total_tokens=0,
        )


@pytest.mark.parametrize("status_code", [400, 429, 500])
def test_v2_terminal_failure_needs_complete_usage(status_code: int) -> None:
    row = LedgerReservation(reservation_row_key(PERIOD_START, "attempt"), "attempt", 500)
    missing = ReservationTerminalEvidence(
        correlation_id="attempt",
        observed_at=PERIOD_START,
        status_code=status_code,
    )
    assert (
        LedgerSyncService._finalization_from_evidence(
            "person", USER, PERIOD_START.date(), row, missing, strict=True
        )
        is None
    )
    complete = missing.model_copy(
        update={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
    )
    result = LedgerSyncService._finalization_from_evidence(
        "person", USER, PERIOD_START.date(), row, complete, strict=True
    )
    assert result is not None and result.total_tokens == 20
    assert result.finalization_kind == "exact_usage"


def test_v2_platform_reservation_uses_bound_gateway_correlation() -> None:
    row = LedgerReservation(reservation_row_key(PERIOD_START, "platform"), "platform", 500)
    evidence = ReservationTerminalEvidence(
        correlation_id="gateway",
        observed_at=PERIOD_START,
        status_code=200,
        prompt_tokens=12,
        completion_tokens=8,
        total_tokens=20,
    )
    assert (
        LedgerSyncService._finalization_from_evidence(
            "person", USER, PERIOD_START.date(), row, evidence, strict=True
        )
        is None
    )
    result = LedgerSyncService._finalization_from_evidence(
        "person",
        USER,
        PERIOD_START.date(),
        row,
        evidence,
        strict=True,
        evidence_correlation_id="gateway",
    )
    assert result is not None and result.correlation_id == "platform"


class FakeLedgerStore:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], dict[str, Any]] = {}
        self.operations: list[tuple[str, str, str]] = []

    def upsert(self, partition: str, row_key: str, entity: dict[str, Any]) -> None:
        self.entities[(partition, row_key)] = entity
        self.operations.append(("upsert", partition, row_key))

    def list_reservations(self, partition: str) -> list[LedgerReservation]:
        return [
            LedgerReservation(
                row_key=row_key,
                correlation_id=row_key.split("|", 2)[2],
                reserved_tokens=int(entity["Reserved"]),
                finalization_kind=entity.get("FinalizationKind"),
            )
            for (row_partition, row_key), entity in sorted(self.entities.items())
            if row_partition == partition and row_key.startswith(RESERVATION_PREFIX)
        ]

    def delete_reservations(self, partition: str, row_keys: Sequence[str]) -> int:
        deleted = 0
        for row_key in row_keys:
            self.operations.append(("delete", partition, row_key))
            if self.entities.pop((partition, row_key), None) is not None:
                deleted += 1
        return deleted

    def list_pending_partitions(self) -> list[str]:
        return sorted(
            {
                partition
                for partition, row_key in self.entities
                if row_key.startswith(RESERVATION_PREFIX)
            }
        )

    def mark_upper_bounds(
        self,
        partition: str,
        reservations: Sequence[LedgerReservation],
    ) -> int:
        for reservation in reservations:
            self.entities[(partition, reservation.row_key)]["FinalizationKind"] = (
                "unverified_upper_bound"
            )
            self.operations.append(("mark", partition, reservation.row_key))
        return len(reservations)


class FakeTokenProvider:
    def __init__(self) -> None:
        self.calls = 0

    def token(self, resource: str) -> str:
        assert resource == "https://storage.azure.com/"
        self.calls += 1
        return f"token-{self.calls}"


class FakeTerminalLog:
    def __init__(
        self, items: Sequence[ReservationTerminalEvidence] = (), fail: bool = False
    ) -> None:
        self.items = items
        self.fail = fail

    def fetch_correlations(
        self,
        correlation_ids: Sequence[str],
        window_start: datetime,
        window_end: datetime,
    ) -> Sequence[ReservationTerminalEvidence]:
        if self.fail:
            raise RuntimeError("unavailable evidence")
        return [item for item in self.items if item.correlation_id in correlation_ids]


def test_orphan_upper_bound_retains_charge_and_late_usage_replaces_it() -> None:
    repository = _repository_with_budget()
    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    row_key = reservation_row_key(PERIOD_START, "correlation-orphan")
    store.entities[(partition, row_key)] = {"Reserved": 900}
    service = LedgerSyncService(repository, store, FakeTerminalLog())
    now = datetime(2026, 7, 3, tzinfo=UTC)
    first = service.run(now)
    assert first.person_finalizations_written == 1
    assert first.reservations_finalized_upper_bound == 1
    assert store.entities[(partition, row_key)] == {
        "Reserved": 900,
        "FinalizationKind": "unverified_upper_bound",
    }
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 0}
    assert service.run(now).person_finalizations_written == 0
    repository.write_token_usage(_usage("orphan", PERIOD_START, 120, estimated=False))
    outcome = service.run(now)
    assert outcome.upper_bounds_settled == 1
    assert (partition, row_key) not in store.entities
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 120}
    assert store.operations.index(("upsert", partition, "C")) < store.operations.index(
        ("mark", partition, row_key)
    )


@pytest.mark.parametrize("terminal_log", [None, FakeTerminalLog(fail=True)])
def test_unavailable_recovery_never_finalizes_unknown_usage(
    terminal_log: FakeTerminalLog | None,
) -> None:
    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    row_key = reservation_row_key(PERIOD_START, "unknown")
    store.entities[(partition, row_key)] = {"Reserved": 900}
    repository = _repository_with_budget()
    outcome = LedgerSyncService(repository, store, terminal_log).run(
        datetime(2026, 7, 3, tzinfo=UTC)
    )
    assert outcome.person_finalizations_written == 0
    assert store.entities[(partition, row_key)] == {"Reserved": 900}


def test_exact_log_recovery_settles_an_orphan_without_fabricating_a_usage_event() -> None:
    repository = _repository_with_budget()
    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    row_key = reservation_row_key(PERIOD_START, "orphan")
    store.entities[(partition, row_key)] = {"Reserved": 900}
    log = FakeTerminalLog(
        [
            ReservationTerminalEvidence(
                correlation_id="orphan",
                observed_at=PERIOD_START,
                status_code=200,
                prompt_tokens=70,
                completion_tokens=30,
                total_tokens=100,
            )
        ]
    )
    outcome = LedgerSyncService(repository, store, log).run(datetime(2026, 7, 3, tzinfo=UTC))
    assert outcome.reservations_settled == 1
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 100}
    assert repository.usage_records == []


def _usage(
    suffix: str,
    ts: datetime,
    tokens: int,
    *,
    estimated: bool,
    status_code: int = 200,
    ingest_error: str | None = None,
) -> TokenUsageRecord:
    return TokenUsageRecord(
        id=f"usage-{suffix}",
        request_id=f"request-{suffix}",
        correlation_id=f"correlation-{suffix}",
        ts=ts,
        team="AI Platform",
        organization="Contoso Global",
        organization_id="org-contoso-global",
        department="AI Platform",
        department_id="department-platform",
        project="Model FinOps",
        project_id="project-finops",
        user=USER,
        user_id=USER,
        agent="Delivery Engineer",
        agent_id="agent-delivery",
        workflow="ledger-test",
        run_id=f"run-{suffix}",
        turn_index=1,
        provider="anthropic",
        model="databricks-claude-sonnet-5",
        model_id="databricks-claude-sonnet-5",
        runtime="Azure Databricks Claude via APIM",
        request_source="employee-desktop",
        input_tokens=tokens,
        cached_tokens=0,
        output_tokens=0,
        et=1,
        et_coeff_m=1,
        latency_ms=100,
        status="success" if status_code < 400 else "error",
        status_code=status_code,
        estimated_cost=0.0,
        estimated=estimated,
        ingest_source="eventhub",
        ingest_error=ingest_error,
    )


def _repository_with_budget() -> InMemoryRepository:
    repository = InMemoryRepository()
    for scope_type, scope_id, parent, limit in (
        ("organization", "org-contoso-global", None, 100_000),
        ("department", "department-platform", "org-contoso-global", 50_000),
        ("user", USER, "department-platform", 10_000),
    ):
        repository.upsert_token_budget(
            PERIOD_START.date(), scope_type, scope_id, parent, limit, 80, "test"
        )
    return repository


def _application_fixture() -> tuple[InMemoryRepository, UUID, UsageApplicationAttribution]:
    repository = _repository_with_budget()
    application = ApplicationAccessService(repository, sync_available=True).sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=UUID("10000000-0000-4000-8000-000000000001"),
            discovered_at=PERIOD_START,
            items=[
                GatewayApplicationDiscoveryItem(
                    apim_subscription_id="ledger-client",
                    display_name="Ledger Client",
                    state="active",
                    scope_type="product",
                    scope_id="applications",
                    scope_exists=True,
                    application_type="service",
                    system_managed=False,
                )
            ],
        ),
        "test-worker",
    )[0]
    application_id = UUID(str(application["id"]))
    attribution = UsageApplicationAttribution(
        application_id=application_id,
        application_subscription_id=repository.gateway_application_subscriptions[0]["id"],
        application_name_snapshot="Ledger Client",
        apim_subscription_id="ledger-client",
        actor_type="person",
        actor_id=USER,
        person_id=USER,
    )
    return repository, application_id, attribution


def test_application_upper_bound_leaves_pending_but_remains_charged() -> None:
    repository, application_id, attribution = _application_fixture()
    store = FakeLedgerStore()
    partition = application_partition_key(application_id, "2026-07")
    row_key = reservation_row_key(PERIOD_START, "correlation-app")
    store.entities[(partition, row_key)] = {"Reserved": 900}
    now = PERIOD_START + timedelta(days=2)
    service = LedgerSyncService(repository, store, FakeTerminalLog())
    service.run(now)
    state = repository.list_gateway_application_ledger_states(
        PERIOD_START.date(), [application_id]
    )[0]
    assert state["pending_reserved_tokens"] == 0
    assert state["pending_reservation_count"] == 0
    assert state["finalized_upper_bound_tokens"] == 900
    assert state["available_tokens"] == 99_100
    repository.write_token_usage(_usage("app", PERIOD_START, 120, estimated=False), attribution)
    service.run(now + timedelta(minutes=5))
    state = repository.list_gateway_application_ledger_states(
        PERIOD_START.date(), [application_id]
    )[0]
    assert state["finalized_upper_bound_tokens"] == 0
    assert state["confirmed_tokens"] == 120
    assert state["available_tokens"] == 99_880
    assert (partition, row_key) not in store.entities


def test_person_and_application_recovery_share_evidence_not_balances() -> None:
    repository, application_id, _ = _application_fixture()
    store = FakeLedgerStore()
    person = partition_key(USER, "2026-07")
    application = application_partition_key(application_id, "2026-07")
    row_key = reservation_row_key(PERIOD_START, "delegated-attempt")
    for partition in (person, application):
        store.entities[(partition, row_key)] = {"Reserved": 900}
    log = FakeTerminalLog(
        [
            ReservationTerminalEvidence(
                correlation_id="delegated-attempt",
                observed_at=PERIOD_START,
                prompt_tokens=70,
                completion_tokens=30,
                total_tokens=100,
                status_code=200,
            )
        ]
    )
    outcome = LedgerSyncService(repository, store, log).run(PERIOD_START + timedelta(days=2))
    assert outcome.reservations_settled == outcome.application_reservations_settled == 1
    assert outcome.person_finalizations_written == outcome.application_finalizations_written == 1
    assert (
        store.entities[(person, "C")]
        == store.entities[(application, "C")]
        == {"ConfirmedUsed": 100}
    )
    assert (
        repository.budget_scope_confirmed_tokens(
            "application", str(application_id), PERIOD_START.date(), date(2026, 8, 1)
        )
        == 100
    )
    assert (
        repository.gateway_application_usage(
            PERIOD_START.date(), date(2026, 8, 1), [application_id]
        )
        == []
    )
    assert (
        repository.gateway_application_usage_activity(
            application_id, PERIOD_START, PERIOD_START + timedelta(days=2), "day", "UTC"
        )
        == []
    )
    assert repository.usage_records == []


def test_retired_application_old_partition_is_recovered_without_reactivation() -> None:
    repository, application_id, _ = _application_fixture()
    repository.gateway_applications[0]["status"] = "retired"
    store = FakeLedgerStore()
    partition = application_partition_key(application_id, "2026-07")
    row_key = reservation_row_key(PERIOD_START, "retired-attempt")
    store.entities[(partition, row_key)] = {"Reserved": 900}
    log = FakeTerminalLog(
        [
            ReservationTerminalEvidence(
                correlation_id="retired-attempt",
                observed_at=PERIOD_START,
                status_code=429,
            )
        ]
    )
    outcome = LedgerSyncService(repository, store, log).run(datetime(2026, 11, 1, tzinfo=UTC))
    assert outcome.application_reservations_settled == 1
    assert (partition, row_key) not in store.entities
    assert (partition, "Q") not in store.entities
    assert repository.gateway_applications[0]["status"] == "retired"


def test_upper_bound_mark_failure_retries_after_database_evidence_is_saved() -> None:
    class FailOnceStore(FakeLedgerStore):
        failed = False

        def mark_upper_bounds(
            self, partition: str, reservations: Sequence[LedgerReservation]
        ) -> int:
            if reservations and not self.failed:
                self.failed = True
                raise RuntimeError("interrupted Table mark")
            return super().mark_upper_bounds(partition, reservations)

    repository = _repository_with_budget()
    store = FailOnceStore()
    partition = partition_key(USER, "2026-07")
    row_key = reservation_row_key(PERIOD_START, "mark-attempt")
    store.entities[(partition, row_key)] = {"Reserved": 900}
    service = LedgerSyncService(repository, store, FakeTerminalLog())
    with pytest.raises(RuntimeError, match="interrupted Table mark"):
        service.run(PERIOD_START + timedelta(days=2))
    assert len(repository.budget_reservation_finalizations) == 1
    outcome = service.run(PERIOD_START + timedelta(days=2, minutes=5))
    assert outcome.person_finalizations_written == 0
    assert outcome.reservations_finalized_upper_bound == 1
    assert store.entities[(partition, row_key)]["Reserved"] == 900


def test_usage_arriving_during_projection_keeps_its_reservation_until_next_run() -> None:
    from unittest.mock import patch

    repository = _repository_with_budget()
    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    row_key = reservation_row_key(PERIOD_START, "correlation-race")
    store.entities[(partition, row_key)] = {"Reserved": 900}
    original = repository.budget_scope_confirmed_tokens

    def arrive(scope_type: Any, scope_id: str, start: date, end: date) -> int:
        repository.write_token_usage(_usage("race", PERIOD_START, 120, estimated=False))
        return original(scope_type, scope_id, start, end)

    service = LedgerSyncService(repository, store)
    with patch.object(repository, "budget_scope_confirmed_tokens", side_effect=arrive):
        assert service.run(PERIOD_START + timedelta(days=2)).reservations_settled == 0
    assert (partition, row_key) in store.entities
    assert service.run(PERIOD_START + timedelta(days=2, minutes=5)).reservations_settled == 1
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 120}


def test_nonzero_partial_failure_does_not_release_reservation() -> None:
    repository = _repository_with_budget()
    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    row_key = reservation_row_key(PERIOD_START, "partial-failure")
    store.entities[(partition, row_key)] = {"Reserved": 900}
    log = FakeTerminalLog(
        [
            ReservationTerminalEvidence(
                correlation_id="partial-failure",
                observed_at=PERIOD_START,
                status_code=500,
                prompt_tokens=70,
            )
        ]
    )
    service = LedgerSyncService(repository, store, log)
    assert service.run(PERIOD_START + timedelta(hours=1)).reservations_settled == 0
    outcome = service.run(PERIOD_START + timedelta(days=2))
    assert outcome.reservations_finalized_upper_bound == 1
    assert store.entities[(partition, row_key)]["Reserved"] == 900


def test_postgres_finalization_batch_serializes_all_evidence_without_per_row_writes() -> None:
    from unittest.mock import MagicMock, patch

    from psycopg.types.json import Jsonb

    from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy

    repository = object.__new__(PostgreSqlOpsDbProxy)
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = [{"id": 1}, {"id": 2}]
    items = [_finalization(), _finalization(correlation_id="second-attempt")]
    with patch.object(repository, "_connection") as connect:
        connect.return_value.__enter__.return_value = connection
        assert repository.save_budget_reservation_finalizations(items) == 2
        assert repository.save_budget_reservation_finalizations([]) == 0
    connection.execute.assert_called_once()
    query, parameters = connection.execute.call_args.args
    assert "jsonb_to_recordset" in query
    assert "DO NOTHING RETURNING id" in query
    assert isinstance(parameters[0], Jsonb)
    assert parameters[0].obj == [item.model_dump(mode="json") for item in items]
    json.dumps(parameters[0].obj)


def test_postgres_ledger_reads_bind_all_scope_and_period_parameters() -> None:
    from unittest.mock import MagicMock, patch

    from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy

    repository = object.__new__(PostgreSqlOpsDbProxy)
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = []
    connection.execute.return_value.fetchone.return_value = None
    start, end = date(2026, 7, 1), date(2026, 8, 1)
    with patch.object(repository, "_connection") as connect:
        connect.return_value.__enter__.return_value = connection
        repository.budget_ledger_snapshot(start, end)
        repository.person_budget_state(USER, start, end)
        repository.gateway_application_ledger_snapshot(start, end)
        repository.gateway_application_usage(start, end, [UUID(int=1)])
        repository.gateway_application_usage_activity(
            UUID(int=1), PERIOD_START, PERIOD_START + timedelta(days=1), "day", "UTC"
        )
        repository.token_usage_by_budget_scope(PERIOD_START, PERIOD_START + timedelta(days=1))
    assert connection.execute.call_count == 6
    for call in connection.execute.call_args_list:
        query, parameters = call.args
        assert query % parameters


def test_postgres_snapshot_writes_one_complete_version_guarded_balance() -> None:
    from unittest.mock import MagicMock, patch

    from psycopg.types.json import Jsonb

    from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy

    state = GatewayApplicationLedgerState(
        period_start=PERIOD_START.date(), application_id=UUID(int=1), token_limit=1000,
        confirmed_tokens=100, pending_reserved_tokens=200, pending_reservation_count=1,
        finalized_upper_bound_tokens=300, finalized_upper_bound_count=1,
        stale_reservation_count=1, oldest_reservation_at=PERIOD_START,
        available_tokens=400, snapshot_at=PERIOD_START + timedelta(days=2),
    )
    repository = object.__new__(PostgreSqlOpsDbProxy)
    connection = MagicMock()
    with patch.object(repository, "_connection") as connect:
        connect.return_value.__enter__.return_value = connection
        repository.save_gateway_application_ledger_states([state])
        repository.save_gateway_application_ledger_states([])
    connection.execute.assert_called_once()
    query, parameters = connection.execute.call_args.args
    assert "WHERE existing.snapshot_at < EXCLUDED.snapshot_at" in query
    assert "ON CONFLICT (period_start, application_id)" in query
    assert isinstance(parameters[0], Jsonb)
    assert parameters[0].obj == [state.model_dump(mode="json")]
    for field in ("pending_reserved_tokens", "finalized_upper_bound_tokens", "available_tokens"):
        assert f"{field} = EXCLUDED.{field}" in query


def test_recovery_clamps_evidence_clock_skew_and_preserves_reservation_identity() -> None:
    reservation = LedgerReservation(reservation_row_key(PERIOD_START, "attempt"), "attempt", 900)
    evidence = ReservationTerminalEvidence(
        correlation_id="attempt", observed_at=PERIOD_START - timedelta(milliseconds=5),
        status_code=200, prompt_tokens=70, completion_tokens=30, total_tokens=100,
    )
    recovered = LedgerSyncService._finalization_from_evidence(
        "person", USER, PERIOD_START.date(), reservation, evidence
    )
    assert recovered is not None
    assert recovered.evidence_at == reservation.created_at
    assert recovered.correlation_id == "attempt"


def test_snapshot_counts_full_stored_usage_without_a_global_watermark() -> None:
    repository = _repository_with_budget()
    repository.write_token_usage(
        _usage("final", datetime(2026, 7, 10, tzinfo=UTC), 400, estimated=False)
    )
    # Streamed and not yet reconciled: its real size is unknown, so it must not be
    # folded into the confirmed total.
    repository.write_token_usage(
        _usage("pending", datetime(2026, 7, 11, tzinfo=UTC), 0, estimated=True)
    )
    repository.write_token_usage(
        _usage("later", datetime(2026, 7, 12, tzinfo=UTC), 700, estimated=False)
    )

    service = LedgerSyncService(repository, FakeLedgerStore())
    person = service.snapshot(datetime(2026, 7, 27, 12, 0, tzinfo=UTC))[0]

    # The pending stream contributes its currently stored zero; its own R row covers the
    # unknown usage. Unrelated later requests are settled into C immediately.
    assert person.confirmed_tokens == 1_100
    assert person.token_limit == 10_000
    assert person.enforce is False


def test_finalization_is_idempotent_scoped_and_superseded_only_by_final_usage() -> None:
    repository = _repository_with_budget()
    upper = _finalization()
    exact = _finalization(
        finalization_kind="exact_usage",
        source="apim_gateway_llm_log",
        input_tokens=70,
        output_tokens=30,
        total_tokens=100,
    )
    assert repository.save_budget_reservation_finalizations([upper, exact]) == 2
    assert repository.save_budget_reservation_finalizations([upper, exact]) == 0
    assert (
        repository.budget_scope_confirmed_tokens("person", USER, date(2026, 7, 1), date(2026, 8, 1))
        == 100
    )
    assert (
        repository.budget_scope_confirmed_tokens(
            "person", "another-user", date(2026, 7, 1), date(2026, 8, 1)
        )
        == 0
    )
    assert (
        repository.settled_reservation_correlations(
            [exact.correlation_id], scope_type="application", scope_id="other-application"
        )
        == set()
    )
    pending = _usage("late", PERIOD_START, 0, estimated=True).model_copy(
        update={"correlation_id": exact.correlation_id}
    )
    repository.write_token_usage(pending)
    assert (
        repository.budget_scope_confirmed_tokens("person", USER, date(2026, 7, 1), date(2026, 8, 1))
        == 100
    )
    repository.write_token_usage(
        pending.model_copy(update={"input_tokens": 40, "estimated": False})
    )
    assert (
        repository.budget_scope_confirmed_tokens("person", USER, date(2026, 7, 1), date(2026, 8, 1))
        == 40
    )
    assert len(repository.budget_reservation_finalizations) == 2


def test_recovered_person_usage_reaches_budget_hierarchy_without_double_counting() -> None:
    repository = _repository_with_budget()
    exact = _finalization(
        finalization_kind="exact_usage",
        source="apim_gateway_llm_log",
        input_tokens=70,
        output_tokens=30,
        total_tokens=100,
    )
    repository.save_budget_reservation_finalizations([exact])
    totals = repository.token_usage_by_budget_scope(PERIOD_START, datetime(2026, 8, 1, tzinfo=UTC))
    assert {(row["scope_type"], row["scope_id"]): row["used_tokens"] for row in totals} == {
        ("organization", "org-contoso-global"): 100,
        ("department", "department-platform"): 100,
        ("user", USER): 100,
    }
    state = repository.person_budget_state(USER, date(2026, 7, 1), date(2026, 8, 1))
    assert state is not None
    assert state["used_tokens"] == 100


def test_snapshot_counts_final_usage_when_nothing_is_pending() -> None:
    repository = _repository_with_budget()
    repository.write_token_usage(
        _usage("a", datetime(2026, 7, 10, tzinfo=UTC), 250, estimated=False)
    )
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

    service = LedgerSyncService(repository, FakeLedgerStore())
    person = service.snapshot(now)[0]

    assert person.confirmed_tokens == 250


def test_table_upper_bound_mark_preserves_the_original_reservation() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    reservation = LedgerReservation(reservation_row_key(PERIOD_START, "attempt"), "attempt", 900)
    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        TableStorageLedger(
            "https://ledger.example.com", "Ledger", FakeTokenProvider(), client
        ) as store,
    ):
        assert store.mark_upper_bounds(partition_key(USER, "2026-07"), [reservation]) == 1
    assert requests[0].method == "MERGE"
    assert requests[0].headers["If-Match"] == "*"
    assert json.loads(requests[0].content) == {"FinalizationKind": "unverified_upper_bound"}


def test_table_partition_discovery_follows_continuation_and_deduplicates() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={"value": [{"PartitionKey": "person|2026-06"}]},
                headers={
                    "x-ms-continuation-nextpartitionkey": "next",
                    "x-ms-continuation-nextrowkey": "row",
                },
            )
        assert request.url.params["NextPartitionKey"] == "next"
        assert request.url.params["NextRowKey"] == "row"
        return httpx.Response(
            200,
            json={
                "value": [
                    {"PartitionKey": "person|2026-06"},
                    {"PartitionKey": "person|2026-07"},
                ]
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        TableStorageLedger(
            "https://ledger.example.com", "Ledger", FakeTokenProvider(), client
        ) as store,
    ):
        assert store.list_pending_partitions() == ["person|2026-06", "person|2026-07"]
    assert len(requests) == 2


def test_ledger_snapshot_keeps_upper_bound_balance_and_rejects_stale_updates() -> None:
    repository = InMemoryRepository()
    state = GatewayApplicationLedgerState(
        period_start=PERIOD_START.date(),
        application_id=UUID(int=1),
        token_limit=1000,
        confirmed_tokens=100,
        pending_reserved_tokens=200,
        pending_reservation_count=1,
        finalized_upper_bound_tokens=300,
        finalized_upper_bound_count=1,
        stale_reservation_count=1,
        oldest_reservation_at=PERIOD_START,
        available_tokens=400,
        snapshot_at=datetime(2026, 7, 3, tzinfo=UTC),
    )
    repository.save_gateway_application_ledger_states([state])
    stale = state.model_copy(update={"snapshot_at": PERIOD_START, "confirmed_tokens": 0})
    repository.save_gateway_application_ledger_states([stale])
    assert repository.list_gateway_application_ledger_states(
        PERIOD_START.date(), [UUID(int=1)]
    ) == [state.model_dump()]
    with pytest.raises(ValueError):
        GatewayApplicationLedgerState.model_validate(
            {**state.model_dump(), "available_tokens": 700}
        )


def test_sync_settles_by_correlation_without_head_of_line_blocking() -> None:
    repository = _repository_with_budget()
    repository.set_department_enforcement("department-platform", "block", "owner")
    repository.write_token_usage(
        _usage("final", datetime(2026, 7, 10, tzinfo=UTC), 400, estimated=False)
    )
    repository.write_token_usage(
        _usage("pending", datetime(2026, 7, 11, tzinfo=UTC), 0, estimated=True)
    )
    repository.write_token_usage(
        _usage("later", datetime(2026, 7, 12, tzinfo=UTC), 700, estimated=False)
    )

    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    # Production ordering: R is written inbound, before the corresponding outbound usage ts.
    final = reservation_row_key(datetime(2026, 7, 10, 8, 59, tzinfo=UTC), "correlation-final")
    pending = reservation_row_key(datetime(2026, 7, 11, 8, 59, tzinfo=UTC), "correlation-pending")
    later = reservation_row_key(datetime(2026, 7, 12, 8, 59, tzinfo=UTC), "correlation-later")
    store.entities[(partition, final)] = {"Reserved": 500}
    store.entities[(partition, pending)] = {"Reserved": 900}
    store.entities[(partition, later)] = {"Reserved": 800}

    service = LedgerSyncService(repository, store)
    outcome = service.run(datetime(2026, 7, 27, 12, 0, tzinfo=UTC))

    assert outcome.people == 1
    assert outcome.reservations_settled == 2
    assert store.entities[(partition, "Q")] == {"Limit": 10_000, "Enforce": True}
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 1_100}
    assert (partition, final) not in store.entities
    assert (partition, later) not in store.entities
    assert (partition, pending) in store.entities

    c_write = store.operations.index(("upsert", partition, "C"))
    first_delete = min(
        index for index, operation in enumerate(store.operations) if operation[0] == "delete"
    )
    assert c_write < first_delete


def test_application_ledger_projects_q_c_m_and_settles_exact_reservation() -> None:
    repository = InMemoryRepository()
    gateway_id = UUID("10000000-0000-4000-8000-000000000001")
    application = ApplicationAccessService(repository, sync_available=True).sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=gateway_id,
            discovered_at=datetime(2026, 7, 1, tzinfo=UTC),
            items=[
                GatewayApplicationDiscoveryItem(
                    apim_subscription_id="outline-assistant",
                    display_name="Outline Assistant",
                    state="active",
                    scope_type="product",
                    scope_id="finops-applications",
                    scope_exists=True,
                    application_type="service",
                    system_managed=False,
                )
            ],
        ),
        "application-sync-worker",
    )[0]
    application_id = UUID(str(application["id"]))
    repository.gateway_applications[0].update(
        {
            "owner_user_id": UUID("20000000-0000-4000-8000-000000000001"),
            "owner_email": "owner@example.com",
            "owner_display_name": "Application Owner",
            "owner_enabled": True,
            "owner_tenant_id": UUID("10000000-0000-4000-8000-000000000001"),
            "tenant_id": UUID("10000000-0000-4000-8000-000000000001"),
            "tenant_name": "Contoso",
            "tenant_enabled": True,
            "organization_unit_id": UUID("10000000-0000-4000-8000-000000000002"),
            "organization_unit_name": "Platform",
            "organization_unit_enabled": True,
        }
    )
    subscription = repository.gateway_application_subscriptions[0]
    usage = _usage(
        "application-final",
        datetime(2026, 7, 10, tzinfo=UTC),
        400,
        estimated=False,
    )
    repository.write_token_usage(
        usage,
        UsageApplicationAttribution(
            application_id=application_id,
            application_subscription_id=subscription["id"],
            application_name_snapshot="Outline Assistant",
            apim_subscription_id="outline-assistant",
            actor_type="service",
            actor_id="service:outline-assistant",
        ),
    )
    model = repository.models[0]
    model_id = UUID(str(model["id"]))
    repository.gateway_application_model_policies[application_id] = {
        "application_id": application_id,
        "updated_by": "owner",
        "updated_at": datetime(2026, 7, 1, tzinfo=UTC),
    }
    repository.gateway_application_model_access[application_id] = {model_id}
    store = FakeLedgerStore()
    partition = application_partition_key(application_id, "2026-07")
    reservation = reservation_row_key(
        datetime(2026, 7, 10, 8, 59, tzinfo=UTC), usage.correlation_id
    )
    store.entities[(partition, reservation)] = {"Reserved": 500}

    outcome = LedgerSyncService(repository, store).run(datetime(2026, 7, 27, 12, tzinfo=UTC))

    assert outcome.applications == 1
    assert outcome.application_reservations_settled == 1
    assert outcome.application_model_policies == 1
    assert outcome.application_mappings == 1
    assert store.entities[(partition, "Q")] == {
        "Limit": 100_000,
        "TokensPerMinute": 100_000,
        "Enforce": True,
    }
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 400}
    assert (partition, reservation) not in store.entities
    assert store.entities[(partition, "M")] == {
        "Configured": True,
        "Models": model_access_value([str(model_id), str(model["model_key"])]),
    }
    assert store.entities[(application_map_partition_key(gateway_id), "outline-assistant")] == {
        "ApplicationId": str(application_id),
        "ApplicationSlug": "outline-assistant",
        "ApplicationName": "Outline Assistant",
        "ApplicationType": "service",
        "ApplicationStatus": "active",
        "SubscriptionState": "active",
        "ScopeExists": True,
        "OwnerUserId": "20000000-0000-4000-8000-000000000001",
        "OwnerEmail": "owner@example.com",
        "OwnerDisplayName": "Application Owner",
        "OwnerEnabled": True,
        "OwnerTenantId": "10000000-0000-4000-8000-000000000001",
        "TenantId": "10000000-0000-4000-8000-000000000001",
        "TenantName": "Contoso",
        "TenantEnabled": True,
        "OrganizationUnitId": "10000000-0000-4000-8000-000000000002",
        "OrganizationUnitName": "Platform",
        "OrganizationUnitEnabled": True,
    }
    c_write = store.operations.index(("upsert", partition, "C"))
    reservation_delete = store.operations.index(("delete", partition, reservation))
    assert c_write < reservation_delete


def test_application_budget_roll_forward_is_idempotent() -> None:
    repository = InMemoryRepository()
    gateway_id = UUID("10000000-0000-4000-8000-000000000001")
    application = ApplicationAccessService(repository, sync_available=True).sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=gateway_id,
            discovered_at=datetime(2026, 7, 1, tzinfo=UTC),
            items=[
                GatewayApplicationDiscoveryItem(
                    apim_subscription_id="outline-assistant",
                    display_name="Outline Assistant",
                    state="active",
                    scope_type="product",
                    scope_id="finops-applications",
                    scope_exists=True,
                    application_type="service",
                    system_managed=False,
                )
            ],
        ),
        "application-sync-worker",
    )[0]
    application_id = UUID(str(application["id"]))
    repository.gateway_application_budgets[(date(2026, 7, 1), application_id)]["token_limit"] = (
        750_000
    )

    first = repository.roll_forward_gateway_application_budgets(
        date(2026, 8, 1), ROLL_FORWARD_ACTOR
    )
    second = repository.roll_forward_gateway_application_budgets(
        date(2026, 8, 1), ROLL_FORWARD_ACTOR
    )

    assert first == 1
    assert second == 0
    assert (
        repository.gateway_application_budgets[(date(2026, 8, 1), application_id)]["token_limit"]
        == 750_000
    )


def test_missing_telemetry_keeps_only_its_own_reservation() -> None:
    repository = _repository_with_budget()
    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    orphan = reservation_row_key(datetime(2026, 7, 10, tzinfo=UTC), "correlation-not-ingested")
    store.entities[(partition, orphan)] = {"Reserved": 9_000}

    outcome = LedgerSyncService(repository, store).run(datetime(2026, 7, 27, 12, 0, tzinfo=UTC))

    assert outcome.reservations_settled == 0
    assert (partition, orphan) in store.entities
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 0}


def test_failed_request_settles_its_zero_usage_reservation() -> None:
    repository = _repository_with_budget()
    repository.write_token_usage(
        _usage(
            "failed",
            datetime(2026, 7, 10, tzinfo=UTC),
            0,
            estimated=True,
            status_code=500,
        )
    )
    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    failed = reservation_row_key(datetime(2026, 7, 10, 8, 59, tzinfo=UTC), "correlation-failed")
    store.entities[(partition, failed)] = {"Reserved": 900}

    outcome = LedgerSyncService(repository, store).run(datetime(2026, 7, 27, 12, 0, tzinfo=UTC))

    assert outcome.reservations_settled == 1
    assert (partition, failed) not in store.entities


def test_reconciled_stream_settles_without_inventing_cache_classification() -> None:
    repository = _repository_with_budget()
    repository.write_token_usage(
        _usage(
            "partial-stream",
            datetime(2026, 7, 10, tzinfo=UTC),
            400,
            estimated=True,
            ingest_error="stream_cache_usage_unavailable",
        )
    )
    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    reservation = reservation_row_key(
        datetime(2026, 7, 10, 8, 59, tzinfo=UTC), "correlation-partial-stream"
    )
    store.entities[(partition, reservation)] = {"Reserved": 900}

    outcome = LedgerSyncService(repository, store).run(datetime(2026, 7, 27, 12, 0, tzinfo=UTC))

    assert outcome.reservations_settled == 1
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 400}
    assert (partition, reservation) not in store.entities
    assert repository.usage_records[0].ingest_error == "stream_cache_usage_unavailable"


def test_legacy_user_cache_metric_does_not_change_exact_total_settlement() -> None:
    repository = _repository_with_budget()
    usage = _usage(
        "partial-stream",
        datetime(2026, 7, 10, tzinfo=UTC),
        400,
        estimated=True,
        ingest_error="stream_cache_usage_unavailable",
    )
    repository.write_token_usage(usage)
    repository.apim_cache_read_buckets[
        (
            "turnstile-llm",
            "user",
            USER,
            usage.ts.replace(minute=0, second=0, microsecond=0),
        )
    ] = 600
    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    reservation = reservation_row_key(
        datetime(2026, 7, 10, 8, 59, tzinfo=UTC), "correlation-partial-stream"
    )
    store.entities[(partition, reservation)] = {"Reserved": 900}

    outcome = LedgerSyncService(repository, store).run(datetime(2026, 7, 27, 12, 0, tzinfo=UTC))

    assert outcome.reservations_settled == 1
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 400}
    assert (partition, reservation) not in store.entities


def test_removed_budget_is_disabled_and_its_reservations_still_settle() -> None:
    repository = _repository_with_budget()
    repository.write_token_usage(
        _usage("final", datetime(2026, 7, 10, tzinfo=UTC), 400, estimated=False)
    )
    repository.delete_token_budget(PERIOD_START.date(), "user", USER, "owner")
    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    final = reservation_row_key(datetime(2026, 7, 10, 8, 59, tzinfo=UTC), "correlation-final")
    store.entities[(partition, "Q")] = {"Limit": 10_000, "Enforce": True}
    store.entities[(partition, final)] = {"Reserved": 500}

    outcome = LedgerSyncService(repository, store).run(datetime(2026, 7, 27, 12, 0, tzinfo=UTC))

    assert outcome.reservations_settled == 1
    assert store.entities[(partition, "Q")] == {"Limit": 0, "Enforce": False}
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 400}
    assert (partition, final) not in store.entities


def test_current_usage_is_written_before_previous_month_reservation_is_deleted() -> None:
    repository = _repository_with_budget()
    repository.roll_forward_budgets(date(2026, 8, 1), ROLL_FORWARD_ACTOR)
    repository.write_token_usage(
        _usage("cross-month", datetime(2026, 8, 1, 0, 0, 2, tzinfo=UTC), 600, estimated=False)
    )
    store = FakeLedgerStore()
    july_partition = partition_key(USER, "2026-07")
    august_partition = partition_key(USER, "2026-08")
    july_reservation = reservation_row_key(
        datetime(2026, 7, 31, 23, 59, 58, tzinfo=UTC), "correlation-cross-month"
    )
    store.entities[(july_partition, july_reservation)] = {"Reserved": 900}

    outcome = LedgerSyncService(repository, store).run(datetime(2026, 8, 1, 0, 5, tzinfo=UTC))

    assert outcome.reservations_settled == 1
    assert store.entities[(august_partition, "C")] == {"ConfirmedUsed": 600}
    current_write = store.operations.index(("upsert", august_partition, "C"))
    previous_delete = store.operations.index(("delete", july_partition, july_reservation))
    assert current_write < previous_delete


def test_reservation_row_keys_sort_chronologically() -> None:
    earlier = reservation_row_key(datetime(2026, 7, 9, 23, 59, 59, tzinfo=UTC), "z")
    later = reservation_row_key(datetime(2026, 7, 10, 0, 0, 0, tzinfo=UTC), "a")
    assert earlier < later


def test_table_reservation_listing_follows_continuation_and_reuses_auth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "RowKey": "R|2026-07-10T09:00:00.000Z|correlation-a",
                            "Reserved": 500,
                        }
                    ]
                },
                headers={
                    "x-ms-continuation-NextPartitionKey": "next-partition",
                    "x-ms-continuation-NextRowKey": "next-row",
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "RowKey": "R|2026-07-10T09:01:00.000Z|correlation-b",
                        "Reserved": 700,
                    }
                ]
            },
        )

    token_provider = FakeTokenProvider()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = TableStorageLedger(
        "https://ledger.table.core.windows.net",
        "TurnstileLedger",
        token_provider=token_provider,
        client=client,
    )

    rows = store.list_reservations("person@example.com|2026-07")

    assert [(row.correlation_id, row.reserved_tokens) for row in rows] == [
        ("correlation-a", 500),
        ("correlation-b", 700),
    ]
    assert len(requests) == 2
    assert requests[1].url.params["NextPartitionKey"] == "next-partition"
    assert requests[1].url.params["NextRowKey"] == "next-row"
    assert token_provider.calls == 1
    client.close()


def test_table_request_refreshes_managed_identity_token_once_after_unauthorized() -> None:
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers["Authorization"])
        if len(authorizations) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"value": []})

    token_provider = FakeTokenProvider()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = TableStorageLedger(
        "https://ledger.table.core.windows.net",
        "TurnstileLedger",
        token_provider=token_provider,
        client=client,
    )

    assert store.list_reservations("person@example.com|2026-07") == []
    assert authorizations == ["Bearer token-1", "Bearer token-2"]
    assert token_provider.calls == 2
    client.close()


def test_table_reservation_deletes_are_partition_batched_at_one_hundred() -> None:
    batch_sizes: list[int] = []
    request_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/$batch"
        count = request.content.count(b"DELETE ")
        batch_sizes.append(count)
        request_bodies.append(request.content)
        return httpx.Response(202, text="HTTP/1.1 204 No Content\r\n" * count)

    token_provider = FakeTokenProvider()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = TableStorageLedger(
        "https://ledger.table.core.windows.net",
        "TurnstileLedger",
        token_provider=token_provider,
        client=client,
    )
    row_keys = [
        reservation_row_key(
            datetime(2026, 7, 10, 9, 0, microsecond=index * 1000, tzinfo=UTC),
            f"correlation-{index}",
        )
        for index in range(205)
    ]

    deleted = store.delete_reservations("person@example.com|2026-07", row_keys)

    assert deleted == 205
    assert batch_sizes == [100, 100, 5]
    first = request_bodies[0].decode()
    outer_boundary = first.split("\r\n", 1)[0][2:]
    assert first.startswith(f"--{outer_boundary}\r\nContent-Type: multipart/mixed;")
    assert "\r\nContent-Type: application/http\r\n" in first
    assert "\r\nContent-Transfer-Encoding: binary\r\n" in first
    assert "\r\nIf-Match: *\r\n" in first
    assert "\r\nDataServiceVersion: 3.0;\r\n" in first
    assert "PartitionKey='person%40example.com%7C2026-07'" in first
    assert first.endswith(f"--{outer_boundary}--\r\n")
    assert token_provider.calls == 1
    client.close()


def test_table_batch_inner_failure_is_not_mistaken_for_outer_accepted() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(202, text="HTTP/1.1 400 Bad Request\r\n")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = TableStorageLedger(
        "https://ledger.table.core.windows.net",
        "TurnstileLedger",
        token_provider=FakeTokenProvider(),
        client=client,
    )

    with pytest.raises(RuntimeError, match=r"statuses=\[400\]"):
        store.delete_reservations(
            "person@example.com|2026-07",
            [reservation_row_key(datetime(2026, 7, 10, 9, 0, tzinfo=UTC), "correlation-failed")],
        )
    client.close()


def test_model_access_projects_both_identifier_spaces() -> None:
    # The gateway sees a registry UUID from the dashboard BFF and a model key from a
    # desktop client for the same model, and cannot resolve one to the other. Projecting
    # only one space would deny the caller that happens to use the other.
    repository = InMemoryRepository()
    model = next(item for item in repository.models if item["model_key"] == "gpt-5.6-luna")
    repository.bulk_upsert_user_budgets(
        PERIOD_START.date(),
        "department-platform",
        [],
        80,
        "owner",
        selected_user_ids=[USER],
        model_ids=[model["id"]],
    )

    store = FakeLedgerStore()
    service = LedgerSyncService(repository, store)
    service.project_model_access(service.model_access(), datetime(2026, 7, 27, 12, 0, tzinfo=UTC))

    row = store.entities[(partition_key(USER, "2026-07"), "M")]
    assert row["Configured"] is True
    assert f"|{str(model['id']).lower()}|" in row["Models"]
    assert f"|{model['model_key'].lower()}|" in row["Models"]


def test_deny_all_projects_an_empty_membership_set_not_a_missing_row() -> None:
    # Deny-all and "never configured" must not look the same in the ledger: the first has
    # to block everything, the second has to allow everything. An absent row means the
    # latter, so an explicit policy with no models still needs a row.
    repository = InMemoryRepository()
    repository.bulk_upsert_user_budgets(
        PERIOD_START.date(),
        "department-platform",
        [],
        80,
        "owner",
        selected_user_ids=[USER],
        model_ids=[],
    )

    store = FakeLedgerStore()
    service = LedgerSyncService(repository, store)
    service.project_model_access(service.model_access(), datetime(2026, 7, 27, 12, 0, tzinfo=UTC))

    row = store.entities[(partition_key(USER, "2026-07"), "M")]
    assert row["Configured"] is True
    # Nothing can be a member of this, which is what deny-all means.
    assert row["Models"] == "||"


def test_timer_replaces_model_access_and_repairs_an_unprojected_revocation() -> None:
    repository = InMemoryRepository()
    store = FakeLedgerStore()
    service = LedgerSyncService(repository, store)
    moment = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    models = [model for model in repository.models if model["enabled"]][:2]
    assert len(models) == 2
    other_partition = partition_key("unselected@example.com", "2026-07")
    store.upsert(other_partition, "M", {"Configured": True, "Models": "|unchanged|"})
    for selection in (models, [models[1]], []):
        repository.bulk_upsert_user_budgets(
            PERIOD_START.date(), "department-platform", [], 80, "owner",
            selected_user_ids=[USER], model_ids=[model["id"] for model in selection],
        )
        service.run(moment)
        row = store.entities[(partition_key(USER, "2026-07"), "M")]
        identifiers = [
            identifier for model in selection
            for identifier in (str(model["id"]), model["model_key"])
        ]
        assert row == {"Configured": True, "Models": model_access_value(identifiers)}
        assert store.entities[(other_partition, "M")]["Models"] == "|unchanged|"
    assert row["Models"] == "||"


def test_model_access_value_cannot_match_a_prefix_of_a_longer_identifier() -> None:
    value = model_access_value(["gpt-5.6-luna"])
    assert "|gpt-5.6-luna|" in value
    assert "|gpt-5.6|" not in value


AUGUST = datetime(2026, 8, 1, tzinfo=UTC)


def test_a_new_period_inherits_the_previous_allowances_instead_of_enforcing_nothing() -> None:
    # The defect this exists for: budgets are period-scoped, enforcement mode is not, and
    # both admission paths read a missing budget row as "not blocked". Without the
    # carry-forward a department configured to block stops blocking every 1st of the month.
    repository = _repository_with_budget()
    store = FakeLedgerStore()
    service = LedgerSyncService(repository, store)

    assert service.snapshot(AUGUST) == []

    rolled = repository.roll_forward_budgets(period_start_for(AUGUST), ROLL_FORWARD_ACTOR)

    assert rolled is not None and rolled["scope_count"] == 3
    service.run(AUGUST)
    quota = store.entities[(partition_key(USER, "2026-08"), "Q")]
    assert quota["Limit"] == 10_000


def test_the_period_inherits_only_once_so_a_removal_is_not_undone() -> None:
    # The timer runs every five minutes. Re-copying would resurrect an allowance the
    # administrator had just deliberately removed, which is why the marker is written
    # even when nothing was copied.
    repository = _repository_with_budget()
    period = period_start_for(AUGUST)
    repository.roll_forward_budgets(period, ROLL_FORWARD_ACTOR)

    repository.delete_token_budget(period, "user", USER, "owner")
    second = repository.roll_forward_budgets(period, ROLL_FORWARD_ACTOR)

    assert second is None
    service = LedgerSyncService(repository, FakeLedgerStore())
    retired = service.snapshot(AUGUST)
    assert len(retired) == 1
    assert retired[0].user_id == USER
    assert retired[0].token_limit == 0
    assert retired[0].enforce is False


def test_a_period_already_being_configured_is_left_alone() -> None:
    # Copying into a half-configured month could push inherited children past a parent the
    # administrator had just lowered, and that invariant is only checked on write.
    repository = _repository_with_budget()
    period = period_start_for(AUGUST)
    repository.upsert_token_budget(
        period, "organization", "org-contoso-global", None, 20_000, 80, "owner"
    )

    rolled = repository.roll_forward_budgets(period, ROLL_FORWARD_ACTOR)

    assert rolled is not None and rolled["scope_count"] == 0
    assert rolled["source_period_start"] is None
    assert (
        repository.token_budgets[(period, "organization", "org-contoso-global")]["token_limit"]
        == 20_000
    )
    assert (period, "user", USER) not in repository.token_budgets


def test_the_inherited_allowance_is_audited_as_its_own_act() -> None:
    # An allowance that appears with no explanation reads as a bug. The timeline has to be
    # able to say the month inherited it, and from whom.
    repository = _repository_with_budget()
    period = period_start_for(AUGUST)

    repository.roll_forward_budgets(period, ROLL_FORWARD_ACTOR)

    events = repository.list_token_budget_audit(period, 20)
    assert len(events) == 3
    assert {event["action"] for event in events} == {"assigned"}
    actors = {event["changed_by"] for event in events}
    assert actors == {"system-budget-roll-forward"}
    # A machine identity with no `@` can never be listed as a person who owns a budget.
    assert not any("@" in actor for actor in actors)
