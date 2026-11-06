from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from typing import Any, Literal, cast
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import azure.functions as func
import psycopg
import pytest
from psycopg_pool import PoolTimeout

from functions.telemetry.function_app import process_usage_events
from tests.backend.persistence.test_price_sync_concurrency import (
    pricing_postgres as pricing_postgres,
)
from tests.backend.telemetry.test_atomic_ledger import identity, summary
from tests.backend.telemetry.test_processor import GATEWAY_ID, RecordingRepository, execution_event
from tests.platform.infrastructure.test_envoy_cache_adapter import execution_row, load_shipper
from turnstile_core.domain.gateway_targets import canonical_usage_id_v2, gateway_deployment_id_for
from turnstile_core.domain.ledger import (
    BudgetPriceProjection,
    LedgerAdmissionReceipt,
    LedgerScope,
    reserve_ledger,
    settle_ledger,
    transition_ledger_attempt,
)
from turnstile_core.domain.models import (
    ModelPrice,
    ReconciledUsage,
    UsageEvent,
    UsageRequestDetail,
    UsageRequestSummary,
)
from turnstile_core.ingestion.processor import CoefficientResolver, UsageProcessor
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy, UsageFilters


def encoded_receipts(receipts: list[LedgerAdmissionReceipt]) -> str:
    return base64.b64encode(json.dumps([
        receipt.model_dump(mode="json") for receipt in receipts
    ]).encode()).decode()


@pytest.mark.parametrize("body", [
    b'{"model":"unit\\u0000model"}',
    b'{"model":"unit\\ud800model"}',
    b'{"model":"unit","extra":NaN}',
])
def test_failed_event_retains_exact_bytes_once_with_safe_error_text(
    pricing_postgres: tuple[str, Event], body: bytes,
) -> None:
    info, _waiting = pricing_postgres
    repository = PostgreSqlOpsDbProxy(info)
    processor = UsageProcessor(repository, CoefficientResolver({}))
    error = ValueError("unit\x00\ud800failure")
    try:
        assert processor.retain_failed_event(body, error)
        assert processor.retain_failed_event(body, error)
        with psycopg.connect(info) as connection:
            retained = connection.execute(
                "SELECT body, error_type, reason, body_sha256 "
                "FROM usage_ingestion_failure WHERE body = %s",
                (body,),
            ).fetchall()
        assert retained == [
            (body, "ValueError", str(error).encode("unicode_escape").decode("ascii"),
             hashlib.sha256(body).digest()),
        ]
        with (psycopg.connect(info) as connection,
              pytest.raises(psycopg.errors.RaiseException, match="ingestion failure evidence")):
            connection.execute("DELETE FROM usage_ingestion_failure WHERE body = %s", (body,))
    finally:
        processor.close()


@pytest.fixture(params=["tokens", "usd"])
def ingestion_case(
    pricing_postgres: tuple[str, Event], request: pytest.FixtureRequest,
) -> Iterator[tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]]]:
    info, _waiting = pricing_postgres
    repository = PostgreSqlOpsDbProxy(info)
    mode = cast(Literal["tokens", "usd"], request.param)
    admitted_at = datetime.now(UTC) - timedelta(hours=25)
    period = admitted_at.date().replace(day=1)
    epoch, application_id, subscription_id, model_id = uuid4(), uuid4(), uuid4(), uuid4()
    correlation, caller, person = uuid4().hex, uuid4().hex, f"{uuid4().hex}@example.test"
    subscription = f"ingestion-{subscription_id}"
    service = (
        "/subscriptions/unit/resourceGroups/unit/providers/Microsoft.ApiManagement/service/unit"
    )
    price = ModelPrice(input_price_per_million=10, cached_price_per_million=1,
                       cache_write_price_per_million=20, output_price_per_million=30)
    projection = BudgetPriceProjection.create(model_id, price, admitted_at)
    try:
        with psycopg.connect(info, autocommit=True) as connection:
            connection.execute(
                """INSERT INTO apim_target_metadata (
                   gateway_profile_id, arm_resource_id, tenant_id, subscription_id,
                   resource_group, service_name, api_id, api_path, service_tier,
                   tier_family, service_location, ownership_mode
                   ) VALUES (%s,%s,%s,%s,'unit','unit','unit','unit','Developer',
                             'classic','unit','adopted') ON CONFLICT DO NOTHING""",
                (GATEWAY_ID, service, uuid4(), uuid4()),
            )
            connection.execute(
                """INSERT INTO gateway_deployment (
                   id, apim_target_id, native_gateway_id, kind, display_name,
                   hosting_mode, lifecycle_ownership
                   ) VALUES (%s,%s,'managed','managed','Unit','azure_managed','customer')
                   ON CONFLICT DO NOTHING""",
                (gateway_deployment_id_for(GATEWAY_ID, "managed"), GATEWAY_ID),
            )
        repository.sync_gateway_applications(GATEWAY_ID, [{
            "application_id": application_id, "subscription_id": subscription_id,
            "apim_subscription_id": subscription, "slug": subscription,
            "display_name": subscription, "application_type": "service", "system_managed": False,
            "state": "active", "scope_type": "api", "scope_id": "unit", "scope_exists": True,
        }], "unit", period, 10000, 10000)
        receipts = []
        for scope in (
            LedgerScope(scope_type="person", scope_id=person, period_start=period),
            LedgerScope(
                scope_type="application", scope_id=str(application_id), period_start=period,
            ),
        ):
            kind, scope_id = scope.scope_type, scope.scope_id
            receipt = LedgerAdmissionReceipt(
                identity=identity(correlation, scope=scope, gateway_profile_id=GATEWAY_ID,
                                  admitted_at=admitted_at, model_id=str(model_id)),
                epoch=epoch, reserved_tokens=2000, budget_mode=mode,
                reserved_usd=Decimal("0.5") if mode == "usd" else None,
                price_snapshot=projection if mode == "usd" else None,
            )
            receipts.append(receipt)
            with psycopg.connect(info, autocommit=True) as connection:
                connection.execute(
                    """INSERT INTO budget_execution_scope (
                       scope_type, scope_id, period_start, epoch, state,
                       admitted_after, legacy_fence_verified_at
                       ) VALUES (%s,%s,%s,%s,'active',%s,%s)""",
                    (kind, scope_id, period, epoch, admitted_at, admitted_at),
                )
                if mode == "usd":
                    connection.execute(
                        """UPDATE budget_execution_scope SET budget_mode = 'usd',
                           desired_usd_limit = 1 WHERE scope_type = %s AND scope_id = %s
                           AND period_start = %s""", (kind, scope_id, period),
                    )
        row = execution_row(
            timestamp=admitted_at.isoformat(), request_id=caller, correlation_id=correlation,
            user_id=person, model_id=str(model_id), model=str(model_id),
            gateway_profile_id=str(GATEWAY_ID), apim_service_id=service,
            native_gateway_id="managed", native_gateway_instance_id="managed",
            gateway_is_managed="true", apim_subscription_id=subscription,
            application_actor_type="person", application_actor_id=person,
            usage_id=canonical_usage_id_v2(GATEWAY_ID, service, "managed", correlation),
            budget_execution=encoded_receipts(receipts),
        )
        yield repository, row, receipts
    finally:
        repository.close()


def observer_event(row: dict[str, Any], **changes: Any) -> dict[str, Any]:
    event = load_shipper().normalize_access_row({**row, **changes})
    assert event is not None
    UsageEvent.model_validate(event)
    return cast(dict[str, Any], event)


def test_observer_transport_failure_preserves_raw_status_and_disconnect_reason() -> None:
    raw = load_shipper().normalize_access_row(execution_row(
        response_code=0, response_flags="DC",
        response_code_details="downstream_remote_disconnect",
    ))
    assert raw is not None
    parsed = UsageEvent.model_validate(raw)
    assert parsed.status == "0" and parsed.status_code == 0
    assert parsed.estimated and not parsed.usage_complete
    assert parsed.error_message is not None
    assert "DC" in parsed.error_message
    assert "downstream_remote_disconnect" in parsed.error_message


def test_transport_status_cannot_turn_attached_counters_into_complete_usage() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True)
    assert processor.process(execution_event(status="0", status_code=0))
    assert repository.records[0].estimated
    assert repository.records[0].usd_cost is None
    assert repository.execution_writes[0]["complete"] is False


def test_batch_continues_after_a_reliably_retained_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    processor = MagicMock()
    processor.process.side_effect = [ValueError("single event failed"), True]
    processor.retain_failed_event.return_value = True
    bad, good = MagicMock(), MagicMock()
    bad.get_body.return_value = b'{"id":"bad"}'
    good.get_body.return_value = b'{"id":"good"}'
    processing_error = None
    with patch("functions.telemetry.function_app._processor", return_value=processor):
        try:
            process_usage_events.build().get_user_function()([bad, good])
        except ValueError as error:
            processing_error = error
    assert processor.process.call_count == 2
    assert processing_error is None
    processor.close.assert_called_once_with()
    assert hashlib.sha256(b'{"id":"bad"}').hexdigest() in caplog.text
    assert '"id":"bad"' not in caplog.text


@pytest.mark.parametrize("error", [
    psycopg.OperationalError("Unit connection lost"),
    psycopg.errors.ConnectionFailure("Unit connection failed"),
    psycopg.errors.DeadlockDetected("Unit deadlock"),
    psycopg.errors.SerializationFailure("Unit serialization failure"),
    psycopg.errors.DiskFull("Unit temporary capacity"),
    psycopg.errors.AdminShutdown("Unit restart"),
    psycopg.errors.LockNotAvailable("Unit lock unavailable"),
    PoolTimeout("Unit pool timeout"),
])
def test_transient_database_errors_retry_without_parking(error: psycopg.OperationalError) -> None:
    processor = MagicMock()
    processor.process.side_effect = error
    processor.retain_failed_event.return_value = True
    event = func.EventHubEvent(body=b'{"id":"unit-transient"}')
    with (patch("functions.telemetry.function_app._processor", return_value=processor),
          pytest.raises(type(error))):
        process_usage_events.build().get_user_function()([event])
    processor.retain_failed_event.assert_not_called()
    processor.close.assert_called_once_with()


@pytest.mark.parametrize("model", ["unit\x00model", "unit\ud800model"])
def test_poison_body_is_retained_and_same_batch_good_event_is_written(
    ingestion_case: tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]],
    model: str,
) -> None:
    repository, row, _receipts = ingestion_case
    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True)
    original_process = processor.process
    poison = {"id": "unit-poison", "model": model}
    bodies = [json.dumps(value).encode() for value in (
        {"id": "unit-first", "model": "unit"}, poison, observer_event(row),
    )]

    def process(payload: dict[str, Any]) -> bool:
        if payload["id"] in {"unit-first", "unit-poison"}:
            raise ValueError("unit\x00\ud800failure")
        return original_process(payload)

    with (patch("functions.telemetry.function_app._processor", return_value=processor),
            patch.object(processor, "process", side_effect=process),
            patch.object(processor, "close")):
        for _attempt in range(2):
            process_usage_events.build().get_user_function()([
                func.EventHubEvent(body=body) for body in bodies
            ])
    with repository._connection() as connection:
        retained = connection.execute(
            "SELECT body, reason FROM usage_ingestion_failure WHERE body = %s", (bodies[1],),
        ).fetchall()
        assert len(retained) == 1 and retained[0]["body"] == bodies[1]
        assert "\x00" not in retained[0]["reason"] and "\ud800" not in retained[0]["reason"]
        assert connection.execute(
            "SELECT count(*) AS total FROM token_usage WHERE id = %s", (row["usage_id"],),
        ).fetchone()["total"] == 1


def test_index_size_failure_is_parked_once_and_good_usage_continues(
    ingestion_case: tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]],
) -> None:
    repository, row, _receipts = ingestion_case
    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True)
    bad = observer_event(row, workflow="".join(uuid4().hex for _part in range(188)))
    good = observer_event(row)
    bad_body = json.dumps(bad).encode()
    with (patch("functions.telemetry.function_app._processor", return_value=processor),
          patch.object(processor, "close")):
        for _attempt in range(2):
            process_usage_events.build().get_user_function()([
                func.EventHubEvent(body=bad_body),
                func.EventHubEvent(body=json.dumps(good).encode()),
            ])
    with repository._connection() as connection:
        retained = connection.execute(
            "SELECT error_type FROM usage_ingestion_failure WHERE body = %s", (bad_body,),
        ).fetchall()
        assert len(retained) == 1 and retained[0]["error_type"] == "ProgramLimitExceeded"
        assert connection.execute(
            "SELECT id FROM token_usage WHERE id = %s", (good["id"],),
        ).fetchone() is not None


@pytest.mark.parametrize("placeholder", [False, True])
def test_postgres_transport_usage_retains_holds_and_late_exact_settles_both_scopes_once(
    ingestion_case: tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]],
    placeholder: bool,
) -> None:
    repository, row, receipts = ingestion_case
    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True)
    if placeholder:
        assert processor.process(observer_event(row, output_tokens=None))
    failed = observer_event(row, response_code=0, response_flags="DC",
                            response_code_details="downstream_remote_disconnect")
    assert processor.process(failed)
    assert processor.process(failed)
    observed_at = UsageEvent.model_validate(failed).ts
    assert repository.oldest_unreconciled_usage(observed_at) is None
    assert repository.apply_reconciled_usage([ReconciledUsage(
        correlation_id=row["correlation_id"], input_tokens=72, cached_tokens=1128, output_tokens=30,
    )]) == 0
    detail = repository.get_usage_request(row["correlation_id"])
    assert detail is not None
    parsed_detail = UsageRequestDetail.model_validate(detail)
    assert parsed_detail.status_code is None and parsed_detail.gateway_status_code is None
    assert parsed_detail.error_message and "DC" in parsed_detail.error_message
    listed = repository.list_usage_requests(
        observed_at, observed_at + timedelta(seconds=1), UsageFilters(user_id=row["user_id"]), 10,
    )
    assert len(listed) == 1
    parsed_summary = UsageRequestSummary.model_validate(listed[0])
    assert parsed_summary.status_code is None and parsed_summary.gateway_status_code is None
    with repository._connection() as connection:
        stored = connection.execute("SELECT * FROM token_usage WHERE id = %s",
                                    (row["usage_id"],)).fetchone()
        assert stored["status"] == "0" and stored["status_code"] == 0
        assert stored["estimated"] and stored["usd_cost"] is None
        assert "DC" in stored["error_message"]
        assert "downstream_remote_disconnect" in stored["error_message"]
        assert stored["ingest_error"] and stored["input_tokens"] == stored["output_tokens"] == 0
        assert connection.execute(
            "SELECT usage_complete, identity, price_snapshot FROM budget_execution_attempt "
            "WHERE usage_id = %s ORDER BY scope_type", (row["usage_id"],),
        ).fetchall() == [{
            "usage_complete": False, "identity": receipt.identity.model_dump(mode="json"),
            "price_snapshot": receipt.price_snapshot.model_dump(mode="json")
            if receipt.price_snapshot else None,
        } for receipt in reversed(receipts)]
        assert connection.execute(
            "SELECT count(*) AS total FROM budget_settlement_task task "
            "JOIN budget_execution_attempt attempt "
            "USING(scope_type,scope_id,period_start,attempt_key) "
            "WHERE attempt.usage_id = %s", (row["usage_id"],),
        ).fetchone()["total"] == 0
    for assignment in (
        "estimated = false", "status = '200'", "usd_cost = 0", "ingest_error = NULL",
    ):
        with pytest.raises(psycopg.errors.CheckViolation), repository._connection() as connection:
            connection.execute(
                f"UPDATE token_usage SET {assignment} WHERE id = %s", (row["usage_id"],),
            )
    eligible = repository.unverified_execution_attempts(limit=32)
    states = {}
    for receipt in receipts:
        assert receipt.identity in eligible
        initial = summary(limit=10000, scope=receipt.identity.scope, epoch=receipt.epoch,
                          budget_mode=receipt.budget_mode,
                          usd_limit=Decimal("1") if receipt.budget_mode == "usd" else None)
        reserved, attempt = reserve_ledger(
            initial, receipt.identity, receipt.reserved_tokens, receipt.identity.admitted_at,
            reserved_usd=receipt.reserved_usd, price_snapshot=receipt.price_snapshot,
        )
        bounded, attempt = transition_ledger_attempt(
            reserved, attempt, "upper_bound", datetime.now(UTC),
        )
        assert bounded.available_usd == reserved.available_usd
        assert bounded.available_tokens == reserved.available_tokens
        assert attempt.actual_tokens is None and attempt.actual_usd is None
        assert (bounded.upper_bound_usd == Decimal("0.5") if receipt.budget_mode == "usd"
                else bounded.upper_bound_tokens == 2000)
        repository.complete_execution_finalization(receipt.identity)
        states[receipt.identity.scope.scope_type] = (bounded, attempt)
    complete = observer_event(row)
    if receipts[0].budget_mode == "usd":
        processor._prices = {row["model_id"]: ModelPrice(
            input_price_per_million=999, cached_price_per_million=999,
            cache_write_price_per_million=999, output_price_per_million=999,
        )}
    assert processor.process(complete)
    assert processor.process(complete)
    for _scope in receipts:
        task = repository.claim_budget_settlement(usage_id=row["usage_id"])
        assert task is not None and task.charge.evidence_rank == 3
        kind = task.charge.identity.scope.scope_type
        bounded, attempt = states[kind]
        settled, retained = settle_ledger(bounded, attempt, task.charge, datetime.now(UTC))
        duplicate, duplicate_attempt = settle_ledger(
            settled, retained, task.charge, datetime.now(UTC),
        )
        assert duplicate is settled and duplicate_attempt is retained
        assert task.charge.total_tokens == 1230
        if receipts[0].budget_mode == "usd":
            assert task.charge.actual_usd == settled.confirmed_usd == Decimal("0.00518")
            assert settled.available_usd is not None and bounded.available_usd is not None
            assert settled.available_usd - bounded.available_usd == Decimal("0.49482")
            assert settled.upper_bound_usd == settled.active_reserved_usd == 0
        else:
            assert settled.confirmed_tokens == 1230
            assert settled.available_tokens - bounded.available_tokens == 770
            assert settled.upper_bound_tokens == settled.active_reserved_tokens == 0
        assert repository.complete_budget_settlement(task)
        assert not repository.complete_budget_settlement(task)
    assert processor.process(failed)
    assert processor.process(complete)
    assert repository.claim_budget_settlement(usage_id=row["usage_id"]) is None
    with repository._connection() as connection:
        exact = connection.execute("SELECT * FROM token_usage WHERE id = %s",
                                   (row["usage_id"],)).fetchone()
        assert exact["status_code"] == 200 and not exact["estimated"]
        assert exact["error_message"] is None and exact["ingest_error"] is None
        if receipts[0].price_snapshot is not None:
            assert exact["usd_cost"] == Decimal("0.00518")
            assert exact["usd_price_sha256"] == receipts[0].price_snapshot.price_sha256
            changed = [receipt.model_copy(update={"reserved_usd": Decimal("0.6")})
                       for receipt in receipts]
            with pytest.raises(ValueError, match="immutable"):
                processor.process(observer_event(row, budget_execution=encoded_receipts(changed)))
            assert connection.execute(
                "SELECT DISTINCT reserved_usd FROM budget_execution_attempt WHERE usage_id = %s",
                (row["usage_id"],),
            ).fetchall() == [{"reserved_usd": Decimal("0.5")}]
            changed_price = BudgetPriceProjection.create(
                receipts[0].price_snapshot.model_id,
                ModelPrice(input_price_per_million=999, cached_price_per_million=999,
                           cache_write_price_per_million=999, output_price_per_million=999),
                receipts[0].price_snapshot.updated_at,
            )
            drifted = [receipt.model_copy(update={"price_snapshot": changed_price})
                       for receipt in receipts]
            with pytest.raises(ValueError, match="immutable"):
                processor.process(observer_event(row, budget_execution=encoded_receipts(drifted)))
            assert connection.execute(
                "SELECT DISTINCT price_snapshot FROM budget_execution_attempt WHERE usage_id = %s",
                (row["usage_id"],),
            ).fetchall() == [{
                "price_snapshot": receipts[0].price_snapshot.model_dump(mode="json"),
            }]


@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("late_receipt", [False, True])
def test_postgres_late_transport_failure_preserves_recovered_usage_and_admission_prices(
    ingestion_case: tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]],
    recovered: bool, late_receipt: bool,
) -> None:
    repository, row, receipts = ingestion_case
    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True)
    assert processor.process(observer_event(row, output_tokens=None))
    if recovered:
        assert repository.apply_reconciled_usage([ReconciledUsage(
            correlation_id=row["correlation_id"], input_tokens=72, output_tokens=30,
            cached_tokens=None,
        )]) == 1
    with repository._connection() as connection:
        before = connection.execute(
            "SELECT * FROM token_usage WHERE id = %s", (row["usage_id"],),
        ).fetchone()
    assert before["estimated"] and before["usd_cost"] is None
    assert before["input_tokens"] == (72 if recovered else 0)
    assert before["output_tokens"] == (30 if recovered else 0)
    if receipts[0].price_snapshot is not None:
        assert before["usd_price_sha256"] == receipts[0].price_snapshot.price_sha256
        assert before["input_price_per_million"] == 10
    processor._prices = {row["model_id"]: ModelPrice(
        input_price_per_million=999, cached_price_per_million=999,
        cache_write_price_per_million=999, output_price_per_million=999,
    )}
    failed = observer_event(
        row, response_code=0, response_flags="DC",
        response_code_details="downstream_remote_disconnect",
        budget_execution=row["budget_execution"] if late_receipt else "",
    )
    assert processor.process(failed)
    assert processor.process(failed)
    with repository._connection() as connection:
        after = connection.execute(
            "SELECT * FROM token_usage WHERE id = %s", (row["usage_id"],),
        ).fetchone()
        expected = before if recovered else {
            **before, "status": "0", "status_code": 0,
            "error_message": failed["error_message"],
            "ingest_error": failed["ingest_error"] or "transport_failure_usage_unknown",
        }
        assert after == expected
        assert connection.execute(
            "SELECT count(*) AS total FROM budget_settlement_task task "
            "JOIN budget_execution_attempt attempt "
            "USING(scope_type,scope_id,period_start,attempt_key) "
            "WHERE attempt.usage_id = %s", (row["usage_id"],),
        ).fetchone()["total"] == 0


@pytest.mark.parametrize(("status_code", "reconciled"), [
    (0, False), (200, False), (200, True), (400, False), (504, False),
])
def test_late_transport_and_unreconciled_repository_parity(
    ingestion_case: tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]],
    status_code: int, reconciled: bool,
) -> None:
    repository, row, _receipts = ingestion_case
    memory = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({}))
    record = processor.normalize(observer_event(row, response_code=status_code, output_tokens=None))
    assert record is not None and record.estimated
    repository.write_token_usage(record)
    memory.write_token_usage(record)
    correction = ReconciledUsage(
        correlation_id=record.correlation_id, input_tokens=72, output_tokens=30,
        cached_tokens=None,
    )
    if reconciled:
        assert repository.apply_reconciled_usage([correction]) == 1
        assert memory.apply_reconciled_usage([correction]) == 1
    failed = record.model_copy(update={
        "status": "0", "status_code": 0, "estimated": True,
        "error_message": "Unit transport failed", "ingest_error": "transport_failure_usage_unknown",
        "usd_cost": None,
    })
    repository.write_token_usage(failed)
    memory.write_token_usage(failed)
    fields = {"status", "status_code", "estimated", "input_tokens", "output_tokens", "ingest_error"}
    with repository._connection() as connection:
        stored = connection.execute(
            "SELECT * FROM token_usage WHERE id = %s", (record.id,),
        ).fetchone()
    assert {field: stored[field] for field in fields} == memory.usage_records[0].model_dump(
        include=fields,
    )
    expected_status = (
        0 if status_code == 0 or (status_code == 200 and not reconciled) else status_code
    )
    assert stored["status_code"] == expected_status
    if expected_status == 0:
        since = record.ts - timedelta(seconds=1)
        assert repository.oldest_unreconciled_usage(since) is None
        assert memory.oldest_unreconciled_usage(since) is None
        assert repository.apply_reconciled_usage([correction]) == 0
        assert memory.apply_reconciled_usage([correction]) == 0


@pytest.mark.parametrize("status_code", [0, 200, 400, 429, 500, 504])
def test_postgres_transport_and_http_outcomes_share_query_failure_contract(
    ingestion_case: tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]],
    status_code: int,
) -> None:
    repository, row, receipts = ingestion_case
    raw = observer_event(row, response_code=status_code,
                         response_flags="DC" if status_code == 0 else "-")
    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True)
    assert processor.process(raw)
    memory = InMemoryRepository()
    record = processor.normalize(raw)
    assert record is not None
    memory.write_token_usage(
        record, processor._application_attribution(UsageEvent.model_validate(raw)),
    )
    observed_at = UsageEvent.model_validate(raw).ts
    until = observed_at + timedelta(seconds=1)
    filters = UsageFilters(user_id=row["user_id"])
    success = 100 <= status_code < 400
    overview = repository.executive_overview(observed_at, until, filters)["totals"]
    distribution = repository.distribution(observed_at, until, "user", filters, 10)
    assert len(distribution) == 1
    for totals in (overview, distribution[0],
                   memory.executive_overview(observed_at, until, filters)["totals"],
                   memory.distribution(observed_at, until, "user", filters, 10)[0]):
        assert totals["total_requests"] == 1
        assert totals["success_requests"] == int(success)
        assert totals["client_error_requests"] == int(400 <= status_code < 500)
        assert totals["server_error_requests"] == int(status_code == 0 or status_code >= 500)
        assert (
            totals["success_requests"] + totals["client_error_requests"]
            + totals["server_error_requests"]
        ) == totals["total_requests"]
        assert totals["error_rate"] == (0 if success else 100)
    for storage in (repository, memory):
        trends = storage.trends(observed_at, until, "hour", "none", "UTC", filters)
        assert sum(
            point["totals"]["failed_calls"] for point in trends["points"]
        ) == int(not success)
    activity = repository.gateway_application_usage_activity(
        UUID(receipts[1].identity.scope.scope_id), observed_at, until, "hour", "UTC",
    )
    assert len(activity) == 1
    assert activity[0]["totals"]["calls"] == 1
    assert activity[0]["totals"]["failed_calls"] == int(not success)
    assert memory.gateway_application_usage_activity(
        UUID(receipts[1].identity.scope.scope_id), observed_at, until, "hour", "UTC",
    )[0]["totals"]["failed_calls"] == int(not success)
    detail = memory.get_usage_request(row["correlation_id"])
    assert detail is not None
    parsed = UsageRequestDetail.model_validate(detail)
    assert parsed.status_code == parsed.gateway_status_code == (status_code or None)
    rule = repository.create_anomaly_rule({
        "name": "Ingestion failure", "description": "", "metric": "error_rate_percent",
        "threshold_mode": "absolute", "threshold_value": 100, "minimum_sample_size": 1,
        "severity": "warning", "scope_type": "user", "scope_id": row["user_id"],
        "enabled": True, "updated_by": "unit",
    })
    anomalies = repository.list_usage_anomalies(observed_at, until, filters, 100)
    failures = [item for item in anomalies if item["rule_id"] == str(rule["id"])]
    assert len(failures) == int(not success)
    if failures:
        assert failures[0]["actual_value"] == 100
    with repository._connection() as connection:
        stored = connection.execute(
            "SELECT status, status_code FROM token_usage WHERE id = %s", (row["usage_id"],),
        ).fetchone()
        assert stored == {"status": str(status_code), "status_code": status_code}


@pytest.mark.parametrize("changes", [
    {"response_code": 400}, {"response_code": 429}, {"response_code": 500},
    {"response_code": 504, "response_flags": "UT"},
    {"output_tokens": None}, {"response_flags": "UC"},
    {"api_format": "openai_responses", "response_status": "incomplete"},
    {"api_format": "anthropic_messages", "last_event_type": "message_stop",
     "cache_write_tokens": None},
])
def test_postgres_unknown_http_stream_and_cache_usage_never_queues_a_refund(
    ingestion_case: tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]],
    changes: dict[str, Any],
) -> None:
    repository, row, _receipts = ingestion_case
    raw = observer_event(row, **changes)
    assert not raw["usage_complete"]
    assert UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True).process(raw)
    with repository._connection() as connection:
        stored = connection.execute("SELECT * FROM token_usage WHERE id = %s",
                                    (row["usage_id"],)).fetchone()
        assert stored["estimated"] and stored["usd_cost"] is None
        assert stored["status_code"] == changes.get("response_code", 200)
        assert connection.execute(
            "SELECT count(*) AS total FROM budget_settlement_task task "
            "JOIN budget_execution_attempt attempt "
            "USING(scope_type,scope_id,period_start,attempt_key) "
            "WHERE attempt.usage_id = %s", (row["usage_id"],),
        ).fetchone()["total"] == 0


@pytest.mark.parametrize("bad_first", [False, True])
def test_postgres_batch_rolls_back_retains_then_continues_and_retries(
    ingestion_case: tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]],
    bad_first: bool,
) -> None:
    repository, row, receipts = ingestion_case
    bad = observer_event(row)
    bad_body = json.dumps(bad).encode()
    next_correlation = uuid4().hex
    next_receipts = [receipt.model_copy(update={"identity": receipt.identity.model_copy(update={
        "correlation_id": next_correlation,
    })}) for receipt in receipts]
    good = observer_event(row, correlation_id=next_correlation, request_id=uuid4().hex,
                          usage_id=canonical_usage_id_v2(
                              GATEWAY_ID, row["apim_service_id"], "managed", next_correlation,
                          ), budget_execution=encoded_receipts(next_receipts))
    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True)
    original_write = repository._write_execution_receipts
    original_process = processor.process
    failed_writes: list[str] = []

    def fail_after_writes(*args: Any, **kwargs: Any) -> None:
        original_write(*args, **kwargs)
        if args[1].id == bad["id"]:
            failed_writes.append(bad["id"])
            args[0].execute("SELECT 1 / 0")

    def assert_failed_event_rolled_back() -> None:
        with repository._connection() as connection:
            for table, field in (("token_usage", "id"), ("apim_usage_identity", "usage_id"),
                                 ("token_usage_gateway_attribution", "usage_id"),
                                 ("token_usage_application_attribution", "usage_id"),
                                 ("budget_execution_attempt", "usage_id")):
                assert connection.execute(
                    f"SELECT count(*) AS total FROM {table} WHERE {field} = %s", (bad["id"],),
                ).fetchone()["total"] == 0
            assert connection.execute(
                "SELECT count(*) AS total FROM budget_settlement_task WHERE attempt_key = ANY(%s)",
                ([receipt.identity.attempt_key for receipt in receipts],),
            ).fetchone()["total"] == 0

    def process_after_prior_failure(payload: dict[str, Any]) -> bool:
        if failed_writes:
            assert_failed_event_rolled_back()
            with repository._connection() as connection:
                retained = connection.execute(
                    "SELECT body, error_type, reason FROM usage_ingestion_failure "
                    "WHERE body = %s", (bad_body,),
                ).fetchall()
                assert len(retained) == 1 and retained[0]["body"] == bad_body
                assert retained[0]["error_type"] == "DivisionByZero"
                assert "division by zero" in retained[0]["reason"]
        return original_process(payload)

    batch = [bad, good] if bad_first else [good, bad]
    processing_error: psycopg.Error | None = None
    with (patch("functions.telemetry.function_app._processor", return_value=processor),
          patch.object(processor, "close"),
          patch.object(processor, "process", side_effect=process_after_prior_failure) as process,
          patch.object(repository, "_write_execution_receipts", side_effect=fail_after_writes),
          ):
        try:
            process_usage_events.build().get_user_function()([
                func.EventHubEvent(body=json.dumps(event).encode()) for event in batch
            ])
        except psycopg.Error as error:
            processing_error = error
    assert failed_writes == [bad["id"]]
    assert_failed_event_rolled_back()
    assert processing_error is None, "A retained single-event failure must not abort the batch"
    assert process.call_count == 2
    with repository._connection() as connection:
        retained = connection.execute(
            "SELECT * FROM usage_ingestion_failure WHERE body = %s", (bad_body,),
        ).fetchall()
        assert len(retained) == 1 and retained[0]["body"] == bad_body
        assert retained[0]["error_type"] == "DivisionByZero"
        assert "division by zero" in retained[0]["reason"]
        assert connection.execute("SELECT id FROM token_usage WHERE id = %s",
                                  (good["id"],)).fetchone()["id"] == good["id"]
    assert processor.process(json.loads(retained[0]["body"]))
    assert processor.process(json.loads(retained[0]["body"]))
    with repository._connection() as connection:
        assert connection.execute(
            "SELECT count(*) AS total FROM budget_execution_attempt WHERE usage_id = %s",
            (bad["id"],),
        ).fetchone()["total"] == 2
        assert connection.execute(
            "SELECT count(*) AS total FROM budget_settlement_task WHERE attempt_key = ANY(%s)",
            ([receipt.identity.attempt_key for receipt in receipts],),
        ).fetchone()["total"] == 2


@pytest.mark.parametrize("bad_first", [False, True])
def test_checkpoint_cannot_skip_a_failure_when_durable_retention_fails(
    ingestion_case: tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]],
    bad_first: bool,
) -> None:
    repository, row, receipts = ingestion_case
    bad = observer_event(row)
    bad_body = json.dumps(bad).encode()
    correlation = uuid4().hex
    later_receipts = [receipt.model_copy(update={"identity": receipt.identity.model_copy(update={
        "correlation_id": correlation,
    })}) for receipt in receipts]
    good = observer_event(row, correlation_id=correlation, request_id=uuid4().hex,
                          usage_id=canonical_usage_id_v2(
                              GATEWAY_ID, row["apim_service_id"], "managed", correlation,
                          ), budget_execution=encoded_receipts(later_receipts))
    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True)
    original_write = repository._write_execution_receipts
    original_execute = psycopg.Connection.execute
    failed_writes: list[str] = []
    retention_attempts: list[str] = []

    def fail_after_writes(*args: Any, **kwargs: Any) -> None:
        original_write(*args, **kwargs)
        if args[1].id == bad["id"]:
            failed_writes.append(bad["id"])
            args[0].execute("SELECT 1 / 0")

    def fail_durable_retention(
        connection: Any, query: Any, *args: Any, **kwargs: Any,
    ) -> Any:
        if isinstance(query, str) and "INSERT INTO usage_ingestion_failure" in query:
            retention_attempts.append(bad["id"])
            return original_execute(connection, "SELECT 'durable retention unavailable'::BIGINT")
        return original_execute(connection, query, *args, **kwargs)

    batch = [bad, good] if bad_first else [good, bad]
    processing_error: psycopg.Error | None = None
    with (patch("functions.telemetry.function_app._processor", return_value=processor),
          patch.object(processor, "close"),
          patch.object(processor, "process", wraps=processor.process) as process,
          patch.object(repository, "_write_execution_receipts", side_effect=fail_after_writes),
          patch.object(psycopg.Connection, "execute", new=fail_durable_retention)):
        try:
            process_usage_events.build().get_user_function()([
                func.EventHubEvent(body=json.dumps(event).encode()) for event in batch
            ])
        except psycopg.Error as error:
            processing_error = error
    assert failed_writes == [bad["id"]]
    assert processing_error is not None
    assert process.call_count == (1 if bad_first else 2)
    with repository._connection() as connection:
        for table, field in (("token_usage", "id"), ("apim_usage_identity", "usage_id"),
                             ("token_usage_gateway_attribution", "usage_id"),
                             ("token_usage_application_attribution", "usage_id"),
                             ("budget_execution_attempt", "usage_id")):
            assert connection.execute(
                f"SELECT count(*) AS total FROM {table} WHERE {field} = %s", (bad["id"],),
            ).fetchone()["total"] == 0
        assert connection.execute(
            "SELECT count(*) AS total FROM budget_settlement_task WHERE attempt_key = ANY(%s)",
            ([receipt.identity.attempt_key for receipt in receipts],),
        ).fetchone()["total"] == 0
        assert bool(connection.execute("SELECT id FROM token_usage WHERE id = %s",
                                       (good["id"],)).fetchone()) is not bad_first
    retry = process_usage_events.build().get_settings_dict("retry_policy")
    assert retry is not None
    assert retry["strategy"] == "fixed_delay"
    assert int(retry["max_retry_count"]) == -1
    assert retry["delay_interval"] == "00:00:05"
    assert isinstance(processing_error, psycopg.errors.InvalidTextRepresentation)
    assert "durable retention unavailable" in str(processing_error)
    assert retention_attempts == [bad["id"]]
    with repository._connection() as connection:
        assert connection.execute(
            "SELECT id FROM usage_ingestion_failure WHERE body = %s", (bad_body,),
        ).fetchone() is None
    with (patch("functions.telemetry.function_app._processor", return_value=processor),
          patch.object(processor, "close")):
        process_usage_events.build().get_user_function()([
            func.EventHubEvent(body=json.dumps(event).encode()) for event in batch
        ])
    with repository._connection() as connection:
        assert connection.execute(
            "SELECT count(*) AS total FROM token_usage WHERE id = ANY(%s)",
            ([bad["id"], good["id"]],),
        ).fetchone()["total"] == 2


@pytest.mark.parametrize("changes", [
    {"usage_event_source": "backfill"}, {"ingest_source": "copilot_cli"},
    {"user_id": "untrusted@example.test"},
])
def test_postgres_rejects_untrusted_receipt_attribution_without_partial_writes(
    ingestion_case: tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]],
    changes: dict[str, Any],
) -> None:
    repository, row, _receipts = ingestion_case
    raw = {**observer_event(row), **changes}
    with pytest.raises(ValueError, match="trusted"):
        UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True).process(raw)
    with repository._connection() as connection:
        assert connection.execute("SELECT id FROM token_usage WHERE id = %s",
                                  (row["usage_id"],)).fetchone() is None


def test_missing_receipt_transport_usage_does_not_invent_an_admission_or_actual_cost(
    ingestion_case: tuple[PostgreSqlOpsDbProxy, dict[str, Any], list[LedgerAdmissionReceipt]],
) -> None:
    repository, row, _receipts = ingestion_case
    raw = observer_event(row, budget_execution="", response_code=0, response_flags="DC")
    assert UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True).process(raw)
    with repository._connection() as connection:
        stored = connection.execute(
            "SELECT estimated, status_code, usd_cost FROM token_usage WHERE correlation_id = %s",
            (row["correlation_id"],),
        ).fetchone()
        assert stored == {"estimated": True, "status_code": 0, "usd_cost": None}
        assert connection.execute(
            "SELECT count(*) AS total FROM budget_execution_attempt WHERE correlation_id = %s",
            (row["correlation_id"],),
        ).fetchone()["total"] == 0