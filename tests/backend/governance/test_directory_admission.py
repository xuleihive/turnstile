from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from tests.backend.governance.test_directory_service import directory as directory
from tests.backend.governance.test_directory_service import hierarchy
from tests.backend.governance.test_directory_transfers import next_month
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from turnstile_core.config import core_runtime_digest
from turnstile_core.domain.directory import (
    DirectoryError,
    DirectoryPrincipal,
    PersonCreate,
    TransferWrite,
)
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.services.directory_service import DirectoryService
from turnstile_core.services.directory_transfers import DirectoryTransferWorker
from turnstile_core.services.directory_upgrade import DirectoryUpgrade, digest


def person(
    service: DirectoryService, owner: DirectoryPrincipal
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    org, source, target = hierarchy(service, owner)
    employee = service.create_person(
        owner,
        PersonCreate(
            governance_user_id="person@example.com",
            display_name="Person",
            department_id=source["id"],
        ),
    )
    return org, employee, target


def test_admission_is_idempotent_immutable_and_not_usage(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, employee, _ = person(service, owner)
    args = {
        "request_id": "sealed-request",
        "governance_user_id": employee["governance_user_id"],
        "organization_id": org["id"],
        "department_id": employee["department_id"],
    }
    service.store.admit_invocation(**args)
    service.store.admit_invocation(**args)
    with pytest.raises(DirectoryError, match="already sealed"):
        service.store.admit_invocation(**{**args, "department_id": "other"})
    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT count(*) AS n FROM directory_invocation_admission"
        ).fetchone()
        assert row and row["n"] == 1
        assert not connection.execute("SELECT 1 FROM token_usage").fetchone()
        assert not connection.execute("SELECT 1 FROM budget_reservation_admission").fetchone()


def test_admission_and_month_boundary_transfer_cannot_accept_old_department(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, employee, target = person(service, owner)
    future = next_month(datetime.now(UTC).date())
    moment = datetime.combine(future, datetime.min.time(), tzinfo=UTC)
    service.schedule_transfer(
        owner,
        UUID(employee["id"]),
        TransferWrite(
            expected_revision=employee["revision"],
            target_department_id=target["id"],
            effective_month=future,
        ),
        "scheduled",
    )
    with pytest.raises(DirectoryError, match="not activated"):
        service.store.admit_invocation(
            request_id="pending",
            governance_user_id=employee["governance_user_id"],
            organization_id=org["id"],
            department_id=employee["department_id"],
            admitted_at=moment,
        )

    def old_admission() -> str:
        try:
            service.store.admit_invocation(
                request_id="concurrent",
                governance_user_id=employee["governance_user_id"],
                organization_id=org["id"],
                department_id=employee["department_id"],
                admitted_at=moment,
            )
        except DirectoryError as error:
            return error.code
        return "incorrectly-admitted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        admission = executor.submit(old_admission)
        transfer = executor.submit(DirectoryTransferWorker(service.store).run, future, now=moment)
        assert transfer.result()["completed"] == 1
        assert admission.result() in {"transfer_activation_pending", "invocation_identity_changed"}
    service.store.admit_invocation(
        request_id="new-department",
        governance_user_id=employee["governance_user_id"],
        organization_id=org["id"],
        department_id=target["id"],
        admitted_at=moment,
    )


def test_prior_period_admission_remains_frozen_after_transfer(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, employee, target = person(service, owner)
    admitted = datetime.now(UTC)
    service.store.admit_invocation(
        request_id="old-period",
        governance_user_id=employee["governance_user_id"],
        organization_id=org["id"],
        department_id=employee["department_id"],
        admitted_at=admitted,
    )
    service.store.finish_invocation("old-period")
    future = next_month(admitted.date())
    moment = datetime.combine(future, datetime.min.time(), tzinfo=UTC)
    service.schedule_transfer(
        owner,
        UUID(employee["id"]),
        TransferWrite(
            expected_revision=employee["revision"],
            target_department_id=target["id"],
            effective_month=future,
        ),
        "scheduled",
    )
    assert DirectoryTransferWorker(service.store).run(future, now=moment)["completed"] == 1
    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM directory_invocation_admission WHERE request_id='old-period'",
        ).fetchone()
        assert row and row["department_id"] == employee["department_id"]


def test_unfinished_prior_month_invocation_blocks_transfer(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, employee, target = person(service, owner)
    admitted = datetime.now(UTC)
    service.store.admit_invocation(
        request_id="unfinished",
        governance_user_id=employee["governance_user_id"],
        organization_id=org["id"],
        department_id=employee["department_id"],
        admitted_at=admitted,
    )
    future = next_month(admitted.date())
    service.schedule_transfer(
        owner,
        UUID(employee["id"]),
        TransferWrite(
            expected_revision=employee["revision"],
            target_department_id=target["id"],
            effective_month=future,
        ),
        "scheduled",
    )
    result = DirectoryTransferWorker(service.store).run(
        future,
        now=datetime.combine(future, datetime.min.time(), tzinfo=UTC),
    )
    assert result["conflicted"] == 1
    with service.store.connection() as connection:
        row = connection.execute("SELECT conflict_code FROM directory_transfer").fetchone()
        assert row and row["conflict_code"] == "transfer_invocation_pending"


def test_transfer_admission_configuration_requires_current_evidence(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, employee, target = person(service, owner)
    with service.store.connection() as connection:
        connection.execute("UPDATE directory_control_state SET person_admission_mode='unverified'")
    future = next_month(datetime.now(UTC).date())
    with pytest.raises(DirectoryError, match="every employee admission path"):
        service.transfer_preview(
            owner,
            UUID(employee["id"]),
            TransferWrite(
                expected_revision=employee["revision"],
                target_department_id=target["id"],
                effective_month=future,
            ),
        )
    assert isinstance(service.store.pool.conninfo, str)
    repository = PostgreSqlOpsDbProxy(service.store.pool.conninfo)
    try:
        upgrade = DirectoryUpgrade(service.store, repository)
        state = service.store.state()
        proof = {
            "person_admission_mode": "bff_only",
            "captured_at": datetime.now(UTC).isoformat(),
            "instance_digest": digest(str(state["instance_id"])),
            "expected_directory_version": state["active_version"],
            "core_digest": core_runtime_digest(),
            "api_directory_admission_protocol": 1,
            "apim_policy_fingerprint": "a" * 64,
            "employee_token_enabled": True,
        }
        with pytest.raises(DirectoryError, match="evidence is required"):
            upgrade.configure_admission(proof, approved=True, actor=owner.email)
        proof["employee_token_enabled"] = False
        upgrade.configure_admission(proof, approved=True, actor=owner.email)
        assert not service.capabilities(owner)["can_schedule_transfers"]
        assert service.store.state()["person_admission_mode"] == "bff_only"
    finally:
        repository.close()
