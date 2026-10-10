from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from tests.backend.governance.test_directory_service import directory as directory
from tests.backend.governance.test_directory_service import hierarchy
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from turnstile_core.domain.directory import (
    AccountLinkWrite,
    DirectoryPrincipal,
    PersonCreate,
    PersonStatusWrite,
)
from turnstile_core.domain.directory_identity import DirectoryIdentity, ProjectionOutcome
from turnstile_core.integrations.directory_identity_table import TableDirectoryIdentityStore
from turnstile_core.services.directory_projection import DirectoryProjectionWorker
from turnstile_core.services.directory_service import DirectoryService

TENANT = UUID("10000000-0000-4000-8000-000000000001")
OBJECT = UUID("20000000-0000-4000-8000-000000000001")


class MemoryIdentityWriter:
    def __init__(self) -> None:
        self.items: list[DirectoryIdentity] = []
        self.fail = False

    def project(self, identity: DirectoryIdentity, *, now: int) -> ProjectionOutcome:
        del now
        if self.fail:
            raise RuntimeError("Unavailable storage")
        self.items.append(identity)
        return "confirmed"


def mapped_person(service: DirectoryService, owner: DirectoryPrincipal) -> dict[str, Any]:
    _, department, _ = hierarchy(service, owner)
    person = service.create_person(
        owner,
        PersonCreate(
            governance_user_id=owner.email,
            display_name="Owner Person",
            department_id=department["id"],
        ),
    )
    person = service.link_account(
        owner,
        UUID(person["id"]),
        AccountLinkWrite(
            expected_revision=person["revision"],
            app_user_id=owner.account_id,  # type: ignore[arg-type]
        ),
    )
    with service.store.connection() as connection:
        connection.execute(
            """INSERT INTO app_user_external_identity(cloud,tenant_id,object_id,app_user_id)
               VALUES ('public',%s,%s,%s)""",
            (TENANT, OBJECT, owner.account_id),
        )
    return person


def test_projection_confirms_all_identities_without_creating_or_changing_budgets(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    person = mapped_person(service, owner)
    writer = MemoryIdentityWriter()
    worker = DirectoryProjectionWorker(service.store, writer)
    first = worker.run_once(worker_id="test", batch_size=1)
    assert first and first["status"] == "completed" and first["projected"] == 1
    assert writer.items[0].governance_user_id == owner.email and not writer.items[0].disabled
    assert writer.items[0].partition == f"I|1|public|{TENANT}"
    assert worker.run_once(worker_id="test") is None
    with service.store.connection() as connection:
        assert not connection.execute("SELECT 1 FROM token_budget").fetchone()
        assert not connection.execute("SELECT 1 FROM user_model_policy").fetchone()
        assert not connection.execute(
            "SELECT 1 FROM directory_outbox WHERE status<>'completed'"
        ).fetchone()
    service.set_status(
        owner,
        UUID(person["id"]),
        PersonStatusWrite(
            expected_revision=person["revision"],
            status="inactive",
        ),
    )
    second = worker.run_once(worker_id="test")
    assert second and second["status"] == "completed"
    assert writer.items[-1].disabled
    assert writer.items[-1].projection_sequence > writer.items[0].projection_sequence
    assert service.capabilities(owner)["gateway_projection_state"] == "confirmed"


def test_projection_failure_never_acknowledges_outbox_and_retries_same_snapshot(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    mapped_person(service, owner)
    writer = MemoryIdentityWriter()
    worker = DirectoryProjectionWorker(service.store, writer)
    writer.fail = True
    with pytest.raises(RuntimeError, match="Unavailable"):
        worker.run_once(worker_id="test")
    with service.store.connection() as connection:
        run = connection.execute("SELECT * FROM directory_projection_run").fetchone()
        assert run and run["status"] == "failed"
        assert connection.execute("SELECT 1 FROM directory_outbox WHERE status='queued'").fetchone()
        connection.execute("UPDATE directory_projection_run SET available_at=now()")
    writer.fail = False
    retried = worker.run_once(worker_id="test")
    assert retried and retried["id"] == str(run["id"]) and retried["status"] == "completed"


def test_projection_discards_stale_snapshot_before_writing_new_version(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    person = mapped_person(service, owner)
    writer = MemoryIdentityWriter()
    worker = DirectoryProjectionWorker(service.store, writer)
    claimed = worker._claim("abandoned")
    assert claimed
    service.set_status(
        owner,
        UUID(person["id"]),
        PersonStatusWrite(
            expected_revision=person["revision"],
            status="inactive",
        ),
    )
    result = worker.run_once(worker_id="new-worker")
    assert result and result["status"] == "completed" and result["id"] != str(claimed["id"])
    assert len(writer.items) == 1 and writer.items[0].disabled


def test_projection_checkpoints_multiple_batches(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    person = mapped_person(service, owner)
    with service.store.connection() as connection:
        connection.execute(
            """INSERT INTO app_user_external_identity(cloud,tenant_id,object_id,app_user_id)
               VALUES ('public',%s,%s,%s)""",
            (TENANT, uuid4(), owner.account_id),
        )
    writer = MemoryIdentityWriter()
    worker = DirectoryProjectionWorker(service.store, writer)
    first = worker.run_once(worker_id="test", batch_size=1)
    assert first and first["status"] == "queued"
    assert service.capabilities(owner)["gateway_projection_state"] == "pending"
    second = worker.run_once(worker_id="test", batch_size=1)
    assert second and second["id"] == first["id"] and second["status"] == "completed"
    assert len(writer.items) == 2
    assert {item.governance_user_id for item in writer.items} == {person["governance_user_id"]}


class Tokens:
    def token(self, resource: str) -> str:
        del resource
        return "test-storage-token"


def identity(version: int, sequence: int, disabled: bool = False) -> DirectoryIdentity:
    return DirectoryIdentity(
        cloud="public",
        tenant_id=TENANT,
        object_id=OBJECT,
        governance_user_id="person@example.com",
        display_name="Person",
        organization_id="org-example",
        organization_name="Example",
        department_id="department-example",
        department_name="Example Department",
        disabled=disabled,
        directory_version=version,
        projection_sequence=sequence,
    )


def test_table_cas_and_readback_do_not_replay_old_enabled_identity_over_disable() -> None:
    stored: dict[str, Any] | None = None
    revision = 0
    writes: list[str] = []
    raced = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stored, revision, raced
        import json

        assert "Authorization" in request.headers
        if request.method == "GET":
            return httpx.Response(
                404 if stored is None else 200,
                json=stored,
                headers={"ETag": f'"{revision}"'},
            )
        writes.append(request.method)
        if request.method == "POST":
            stored = json.loads(request.content)
            revision += 1
            return httpx.Response(204)
        assert request.method == "PUT" and request.headers["If-Match"] == f'"{revision}"'
        if not raced:
            raced = True
            revision += 1
            return httpx.Response(412)
        stored = json.loads(request.content)
        revision += 1
        return httpx.Response(204)

    writer = TableDirectoryIdentityStore(
        "https://example.table.core.windows.net",
        "TurnstileLedger",
        token_provider=Tokens(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert writer.project(identity(1, 1), now=100) == "confirmed"
    assert writer.project(identity(2, 2, True), now=101) == "confirmed"
    count = len(writes)
    assert writer.project(identity(1, 1), now=102) == "superseded"
    assert writer.project(identity(2, 1), now=102) == "superseded"
    assert len(writes) == count
    assert stored and stored["Disabled"] is True
    assert stored["DirectoryVersion"] == "2"


def test_failed_table_readback_is_not_projection_success() -> None:
    reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.method == "POST":
            return httpx.Response(204)
        reads += 1
        return httpx.Response(404 if reads == 1 else 503)

    writer = TableDirectoryIdentityStore(
        "https://example.table.core.windows.net",
        "TurnstileLedger",
        token_provider=Tokens(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        writer.project(identity(1, 1), now=100)


def test_identity_mapping_expiry_is_bounded_by_revocation_and_transfer_boundary() -> None:
    mapping = identity(1, 1)
    assert int(mapping.entity(now=100)["ExpiresAt"]) == 400
    mapping = mapping.model_copy(update={"valid_until": 180})
    assert int(mapping.entity(now=100)["ExpiresAt"]) == 180
    assert int(mapping.entity(now=200)["ExpiresAt"]) == 180
