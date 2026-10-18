from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from tests.backend.governance.test_directory_service import directory as directory
from tests.backend.governance.test_directory_service import hierarchy
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from turnstile_core.domain.directory import (
    DirectoryError,
    DirectoryPrincipal,
    PersonCreate,
    TransferCancelWrite,
    TransferWrite,
)
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.services.directory_service import DirectoryService
from turnstile_core.services.directory_transfers import DirectoryTransferWorker


def next_month(current: date) -> date:
    return date(current.year + (current.month == 12), current.month % 12 + 1, 1)


@pytest.mark.parametrize("target_limit,expected", [(100, "completed"), (10, "conflicted")])
def test_transfer_rollforward_keeps_prior_period_and_is_idempotent(
    directory: tuple[DirectoryService, DirectoryPrincipal],
    target_limit: int,
    expected: str,
) -> None:
    service, owner = directory
    org, source, target = hierarchy(service, owner)
    current = datetime.now(UTC).date().replace(day=1)
    future = next_month(current)
    person = service.create_person(
        owner,
        PersonCreate(
            display_name="Transfer",
            governance_user_id="transfer@example.com",
            department_id=source["id"],
        ),
    )
    service.schedule_transfer(
        owner,
        UUID(person["id"]),
        TransferWrite(
            expected_revision=person["revision"],
            target_department_id=target["id"],
            effective_month=future,
        ),
        "move-next-month",
    )
    assert isinstance(service.store.pool.conninfo, str)
    repository = PostgreSqlOpsDbProxy(service.store.pool.conninfo)
    try:
        for scope_type, scope_id, parent_id, amount in [
            ("organization", org["id"], None, 200),
            ("department", source["id"], org["id"], 100),
            ("department", target["id"], org["id"], target_limit),
            ("user", person["governance_user_id"], source["id"], 40),
        ]:
            repository.upsert_token_budget(
                current, scope_type, scope_id, parent_id, amount, 80, "test"
            )
        repository.roll_forward_budgets(future, "test")
        worker = DirectoryTransferWorker(service.store)
        now = datetime.combine(future, datetime.min.time(), tzinfo=UTC)
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: worker.run(future, now=now), range(2)))
        assert sum(outcome[expected] for outcome in outcomes) == 1
        assert worker.run(future, now=now) == {"completed": 0, "conflicted": 0}
        old_budget = repository.list_token_budgets(current, person["governance_user_id"])[0]
        new_budget = repository.list_token_budgets(future, person["governance_user_id"])[0]
        assert old_budget["parent_scope_id"] == source["id"]
        assert new_budget["parent_scope_id"] == (
            target["id"] if expected == "completed" else source["id"]
        )
        assert old_budget["token_limit"] == new_budget["token_limit"] == 40
        assert service.store.catalog(at_time=now).users[0].parent_id == (
            target["id"] if expected == "completed" else source["id"]
        )
        with service.store.connection() as connection:
            transfer = connection.execute("SELECT * FROM directory_transfer").fetchone()
            assert transfer and transfer["status"] == expected
            if expected == "conflicted":
                assert transfer["conflict_code"] == "transfer_budget_capacity"
    finally:
        repository.close()


def test_transfer_does_not_change_attribution_after_admission(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    _, source, target = hierarchy(service, owner)
    future = next_month(datetime.now(UTC).date())
    person = service.create_person(
        owner,
        PersonCreate(
            display_name="Transfer",
            governance_user_id="transfer@example.com",
            department_id=source["id"],
        ),
    )
    service.schedule_transfer(
        owner,
        UUID(person["id"]),
        TransferWrite(
            expected_revision=person["revision"],
            target_department_id=target["id"],
            effective_month=future,
        ),
        "move",
    )
    now = datetime.combine(future, datetime.min.time(), tzinfo=UTC)
    with service.store.connection() as connection:
        connection.execute(
            """INSERT INTO budget_reservation_admission(
                 scope_type,scope_id,correlation_id,created_at,reserved_tokens)
               VALUES ('person',%s,'existing-reservation',%s,100)""",
            (person["governance_user_id"], now),
        )
    result = DirectoryTransferWorker(service.store).run(future, now=now)
    assert result["conflicted"] == 1
    assert service.store.catalog(at_time=now).users[0].parent_id == source["id"]
    with service.store.connection() as connection:
        conflict = connection.execute("SELECT conflict_code FROM directory_transfer").fetchone()
        assert conflict and conflict["conflict_code"] == "transfer_usage_started"


def test_transfer_preview_detects_future_budget_drift_and_cancel_preserves_assignment(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, source, target = hierarchy(service, owner)
    future = next_month(datetime.now(UTC).date())
    person = service.create_person(
        owner,
        PersonCreate(
            display_name="Transfer",
            governance_user_id="transfer@example.com",
            department_id=source["id"],
        ),
    )
    write = TransferWrite(
        expected_revision=person["revision"],
        target_department_id=target["id"],
        effective_month=future,
    )
    preview = service.transfer_preview(owner, UUID(person["id"]), write)
    with service.store.connection() as connection:
        connection.execute(
            """INSERT INTO token_budget(period_start,scope_type,scope_id,parent_scope_id,
                 token_limit,warning_threshold_percent,updated_by)
               VALUES (%s,'user',%s,%s,40,80,'test')""",
            (future, person["governance_user_id"], source["id"]),
        )
    with pytest.raises(DirectoryError, match="changed"):
        service.schedule_transfer(
            owner,
            UUID(person["id"]),
            write.model_copy(
                update={
                    "preview_digest": preview["preview_digest"],
                }
            ),
            "stale",
        )
    preview = service.transfer_preview(owner, UUID(person["id"]), write)
    scheduled = service.schedule_transfer(
        owner,
        UUID(person["id"]),
        write.model_copy(
            update={
                "preview_digest": preview["preview_digest"],
            }
        ),
        "reviewed",
    )
    rows = service.transfers(owner, person_id=UUID(person["id"]))
    assert rows[0]["source_organization_id"] == org["id"]
    assert rows[0]["id"] == scheduled["id"]
    service.cancel_transfer(
        owner, UUID(scheduled["id"]), TransferCancelWrite(reason="Changed plan")
    )
    assert service.transfers(owner)[0]["status"] == "cancelled"
    assert service.store.catalog().users[0].parent_id == source["id"]
