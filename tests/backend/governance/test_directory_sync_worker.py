from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet

from tests.backend.governance.test_directory_service import (
    directory as directory,
)
from tests.backend.governance.test_directory_service import hierarchy
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import (
    local_postgres as local_postgres,
)
from turnstile_core.domain.directory import (
    ConnectionWrite,
    DirectoryError,
    DirectoryPrincipal,
    GroupMapping,
    GroupMappingsWrite,
    PersonCreate,
    PersonStatusWrite,
    SyncApplyWrite,
    SyncJobWrite,
)
from turnstile_core.integrations.entra_directory import GraphDirectoryClient
from turnstile_core.security import CredentialCipher
from turnstile_core.services.directory_connections import DirectoryConnectionService
from turnstile_core.services.directory_service import DirectoryService
from turnstile_core.services.directory_sync_worker import DirectorySyncWorker

TENANT = UUID("10000000-0000-4000-8000-000000000001")
GROUP = UUID("20000000-0000-4000-8000-000000000001")
USER = UUID("30000000-0000-4000-8000-000000000001")


class Graph:
    def __init__(self) -> None:
        self.users = [
            {
                "id": str(USER),
                "displayName": "Example",
                "mail": "example@example.com",
                "userPrincipalName": "example@example.com",
                "jobTitle": "Engineer",
                "accountEnabled": True,
                "userType": "Member",
            }
        ]
        self.error: DirectoryError | None = None
        self.checkpoints: list[str | None] = []

    def collection(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        del path, params
        yield from self.users

    def group(self, group_id: UUID) -> dict[str, Any]:
        return {"id": str(group_id), "visibility": "Private"}

    def user(self, object_id: UUID) -> dict[str, Any]:
        return next(user for user in self.users if user["id"] == str(object_id))

    def collection_page(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        del params
        if self.error:
            raise self.error
        if "/delta" in path:
            self.checkpoints.append(path if "deltatoken" in path else None)
            return [], None, "https://graph.microsoft.com/v1.0/groups/delta?$deltatoken=private"
        if path == "users":
            return self.users, None, None
        return (
            [{"id": user["id"], "@odata.type": "#microsoft.graph.user"} for user in self.users],
            None,
            None,
        )

    def members(
        self,
        group_id: UUID,
        *,
        transitive: bool = False,
    ) -> Iterator[dict[str, Any]]:
        del group_id, transitive
        if self.error:
            raise self.error
        yield from self.users

    def delta(
        self,
        resource: str,
        *,
        checkpoint: str | None = None,
        params: Mapping[str, str] | None = None,
        heartbeat: Any = None,
    ) -> tuple[list[dict[str, Any]], str]:
        del params
        if heartbeat:
            heartbeat()
        self.checkpoints.append(checkpoint)
        return [], f"https://graph.microsoft.com/v1.0/{resource}/delta?$deltatoken=private"

    def close(self) -> None:
        pass


def setup_sync(
    service: DirectoryService,
    owner: DirectoryPrincipal,
) -> tuple[DirectoryConnectionService, DirectorySyncWorker, Graph, dict[str, Any], str]:
    organization, department, _ = hierarchy(service, owner)
    graph = Graph()
    factory = lambda _: cast(GraphDirectoryClient, graph)  # noqa: E731
    connections = DirectoryConnectionService(service, factory, sync_enabled=True)
    saved = connections.create(
        owner,
        ConnectionWrite(
            name="Example directory",
            organization_id=organization["id"],
            tenant_id=TENANT,
        ),
        None,
    )
    saved = connections.set_mappings(
        owner,
        UUID(saved["id"]),
        GroupMappingsWrite(
            expected_revision=saved["revision"],
            items=[GroupMapping(group_id=GROUP, unit_id=department["id"])],
        ),
    )
    connections.validate(owner, UUID(saved["id"]))
    saved = connections.update(
        owner,
        UUID(saved["id"]),
        ConnectionWrite(
            name=saved["name"],
            organization_id=organization["id"],
            tenant_id=TENANT,
            expected_revision=saved["revision"],
            enabled=True,
        ),
    )
    worker = DirectorySyncWorker(service.store, factory, CredentialCipher(Fernet.generate_key()))
    return connections, worker, graph, saved, department["id"]


def approve(
    connections: DirectoryConnectionService,
    owner: DirectoryPrincipal,
    service: DirectoryService,
    job: dict[str, Any],
    **approval: Any,
) -> None:
    connections.apply_job(
        owner,
        UUID(job["id"]),
        SyncApplyWrite(
            expected_directory_version=service.store.state()["active_version"],
            **approval,
        ),
    )


def test_sync_preview_apply_checkpoint_and_stable_identity(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    connections, worker, graph, saved, _ = setup_sync(service, owner)
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "first")
    preview = worker.run_once(worker_id="test")
    assert preview and preview["status"] == "awaiting_review"
    assert preview["summary"]["create"] == 1
    assert service.people(owner).total == 0
    with service.store.connection() as db:
        assert not db.execute("SELECT 1 FROM directory_sync_checkpoint").fetchone()
    approve(connections, owner, service, job)
    assert worker.run_once(worker_id="test")["status"] == "succeeded"  # type: ignore[index]
    person = service.people(owner).items[0]
    assert person["menu_permission_group"] == "user"
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM app_user").fetchone()["n"] == 1  # type: ignore[index]
        assert not db.execute("SELECT 1 FROM app_user_external_identity").fetchone()
        cipher = db.execute(
            "SELECT checkpoint_ciphertext FROM directory_sync_checkpoint"
        ).fetchone()
        assert cipher and b"private" not in cipher["checkpoint_ciphertext"]
        db.execute(
            """UPDATE directory_person SET menu_permission_group='team_admin',
                 menu_permission_groups=ARRAY['team_admin','department_admin'] WHERE id=%s""",
            (person["id"],),
        )
    graph.users[0]["mail"] = "renamed@example.com"
    graph.users[0]["displayName"] = "Renamed"
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(mode="delta"), "second")
    assert worker.run_once(worker_id="test")["summary"]["update"] == 1  # type: ignore[index]
    approve(connections, owner, service, job)
    worker.run_once(worker_id="test")
    edited = service.people(owner).items[0]
    assert edited["id"] == person["id"]
    assert edited["governance_user_id"] == "example@example.com"
    assert edited["contact_email"] == "renamed@example.com"
    assert edited["menu_permission_group"] == "department_admin"
    assert set(edited["menu_permission_groups"]) == {"team_admin", "department_admin"}
    assert graph.checkpoints[-1] and "private" in graph.checkpoints[-1]


def test_missing_requires_explicit_approval_and_does_not_delete_history(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    connections, worker, graph, saved, _ = setup_sync(service, owner)
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "first")
    worker.run_once(worker_id="test")
    approve(connections, owner, service, job)
    worker.run_once(worker_id="test")
    original = service.people(owner).items[0]
    graph.users = []
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(mode="delta"), "missing")
    preview = worker.run_once(worker_id="test")
    assert preview and preview["summary"]["missing"] == 1
    with pytest.raises(DirectoryError, match="missing-member"):
        approve(connections, owner, service, job)
    with pytest.raises(DirectoryError, match="large membership"):
        approve(connections, owner, service, job, approve_missing=True)
    approve(connections, owner, service, job, approve_missing=True, approve_mass_changes=True)
    worker.run_once(worker_id="test")
    assert service.people(owner).total == 1
    assert service.people(owner).items[0]["source_disabled"]
    assert service.store.catalog().users == []
    assert (
        service.store.catalog(include_inactive=True).users[0].id == original["governance_user_id"]
    )


def test_failure_stale_preview_and_manual_disabled_are_fail_closed(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    connections, worker, graph, saved, _ = setup_sync(service, owner)
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "first")
    graph.error = DirectoryError("graph_permission_denied", "Missing consent", 403)
    assert worker.run_once(worker_id="test")["status"] == "failed"  # type: ignore[index]
    assert service.people(owner).total == 0
    graph.error = None
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "second")
    worker.run_once(worker_id="test")
    approve(connections, owner, service, job)
    worker.run_once(worker_id="test")
    person = service.people(owner).items[0]
    service.set_status(
        owner,
        UUID(person["id"]),
        PersonStatusWrite(
            expected_revision=person["revision"],
            status="inactive",
        ),
    )
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "third")
    worker.run_once(worker_id="test")
    approve(connections, owner, service, job)
    worker.run_once(worker_id="test")
    assert service.people(owner).items[0]["manual_disabled"]
    assert service.store.catalog().users == []
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "stale")
    worker.run_once(worker_id="test")
    person = service.people(owner).items[0]
    service.set_status(
        owner,
        UUID(person["id"]),
        PersonStatusWrite(
            expected_revision=person["revision"],
            status="active",
        ),
    )
    with pytest.raises(DirectoryError, match="new preview"):
        approve(connections, owner, service, job)
    connections.cancel(owner, UUID(job["id"]))


def test_email_collision_never_automatically_links_existing_person(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    connections, worker, _, saved, department = setup_sync(service, owner)
    service.create_person(
        owner,
        PersonCreate(
            display_name="Manual person",
            governance_user_id="example@example.com",
            department_id=department,
        ),
    )
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "collision")
    preview = worker.run_once(worker_id="test")
    assert preview and preview["summary"]["conflicts"] == 1
    changes = connections.changes(owner, UUID(job["id"]), offset=0, limit=10)
    assert changes["items"][0]["conflict_code"] == "explicit_person_binding_required"
    with pytest.raises(DirectoryError, match="conflicts"):
        approve(connections, owner, service, job)
    with service.store.connection() as db:
        assert not db.execute("SELECT 1 FROM directory_external_binding").fetchone()


def test_unvalidated_connection_remains_unvalidated_after_label_edit(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    organization, _, _ = hierarchy(service, owner)
    connections = DirectoryConnectionService(
        service,
        lambda _: cast(GraphDirectoryClient, Graph()),
        sync_enabled=True,
    )
    saved = connections.create(
        owner,
        ConnectionWrite(
            name="Original",
            organization_id=organization["id"],
            tenant_id=TENANT,
        ),
        None,
    )
    edited = connections.update(
        owner,
        UUID(saved["id"]),
        ConnectionWrite(
            name="Renamed",
            organization_id=organization["id"],
            tenant_id=TENANT,
            expected_revision=saved["revision"],
        ),
    )
    assert edited["validated_revision"] is None
    with pytest.raises(DirectoryError, match="Validate"):
        connections.update(
            owner,
            UUID(saved["id"]),
            ConnectionWrite(
                name="Renamed",
                organization_id=organization["id"],
                tenant_id=TENANT,
                expected_revision=edited["revision"],
                enabled=True,
            ),
        )


def test_expired_lease_reclaims_job_and_scheduler_deduplicates(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    _, worker, _, _, _ = setup_sync(service, owner)
    assert worker.enqueue_due() == 1
    assert worker.enqueue_due() == 0
    claim = worker._claim("first")
    assert claim and worker._claim("second") is None
    with service.store.connection() as db:
        db.execute(
            "UPDATE directory_sync_job SET lease_expires_at=now()-interval '1 minute' WHERE id=%s",
            (claim["id"],),
        )
    reclaimed = worker._claim("second")
    assert reclaimed and reclaimed["id"] == claim["id"]
    with pytest.raises(DirectoryError, match="lease"):
        worker._renew(claim["id"], "first")
    assert worker._read_segment(reclaimed, "second")
    worker._analyze_segment(reclaimed, "second")
    assert worker.run_once(worker_id=str(uuid4())) is None


def test_unchanged_sync_keeps_person_revision(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    connections, worker, _, saved, _ = setup_sync(service, owner)
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "first")
    worker.run_once(worker_id="test")
    approve(connections, owner, service, job)
    worker.run_once(worker_id="test")
    original = service.people(owner).items[0]
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(mode="delta"), "noop")
    preview = worker.run_once(worker_id="test")
    assert preview and preview["summary"]["ignored"] == 1
    approve(connections, owner, service, job)
    worker.run_once(worker_id="test")
    assert service.people(owner).items[0]["revision"] == original["revision"]


def test_sync_approval_expiry_and_owner_revocation_do_not_apply_changes(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    connections, worker, _, saved, _ = setup_sync(service, owner)
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "expired")
    worker.run_once(worker_id="test")
    with service.store.connection() as connection:
        connection.execute(
            """UPDATE directory_sync_job SET preview_expires_at=now()-interval '1 minute'
               WHERE id=%s""",
            (UUID(job["id"]),),
        )
    with pytest.raises(DirectoryError, match="expired"):
        approve(connections, owner, service, job)
    connections.cancel(owner, UUID(job["id"]))
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "revoked")
    worker.run_once(worker_id="test")
    approve(connections, owner, service, job)
    with service.store.connection() as connection:
        connection.execute("UPDATE app_user SET enabled=FALSE WHERE id=%s", (owner.account_id,))
    result = worker.run_once(worker_id="test")
    assert (
        result and result["status"] == "failed" and result["error_code"] == "sync_approval_revoked"
    )
    assert service.people(owner).total == 0


def test_large_snapshot_read_and_analysis_resume_without_early_directory_mutation(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    connections, worker, graph, saved, _ = setup_sync(service, owner)
    graph.users = [
        {
            "id": str(UUID(int=1000 + index)),
            "displayName": f"Person {index}",
            "mail": f"person-{index}@example.com",
            "userPrincipalName": f"person-{index}@example.com",
            "accountEnabled": True,
            "userType": "Member",
            "jobTitle": "",
        }
        for index in range(240)
    ]
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "large")
    phases = []
    for index in range(10):
        result = worker.run_once(worker_id=f"worker-{index}")
        assert result is not None
        phases.append(result["status"])
        assert service.people(owner).total == 0
        public = connections.job(owner, UUID(job["id"]))
        assert "read_progress_ciphertext" not in public
        assert "checkpoint_ciphertext" not in public
        if result["status"] == "awaiting_review":
            break
    assert len(phases) > 2 and phases[-1] == "awaiting_review"
    assert result is not None
    assert result["summary"]["create"] == 240
    with service.store.connection() as connection:
        rows = connection.execute(
            "SELECT count(*) AS n FROM directory_sync_source WHERE job_id=%s",
            (UUID(job["id"]),),
        ).fetchone()
        assert rows and rows["n"] == 240
        assert not connection.execute("SELECT 1 FROM directory_sync_checkpoint").fetchone()
    approve(connections, owner, service, job)
    applied = worker.run_once(worker_id="applier")
    assert applied and applied["status"] == "succeeded", applied
    assert service.people(owner).total == 240


def test_apply_failure_rolls_back_people_audit_memberships_and_checkpoint(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    connections, worker, _, saved, _ = setup_sync(service, owner)
    job = connections.create_job(owner, UUID(saved["id"]), SyncJobWrite(), "apply-failure")
    preview = worker.run_once(worker_id="reader")
    assert preview and preview["status"] == "awaiting_review"
    approve(connections, owner, service, job)
    with service.store.connection() as db:
        before = db.execute("SELECT count(*) AS n FROM directory_change_audit").fetchone()
        version = db.execute("SELECT active_version FROM directory_control_state").fetchone()
        db.execute(
            """CREATE FUNCTION reject_sync_audit() RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN
                 IF NEW.action='sync_create' THEN RAISE EXCEPTION 'injected audit failure'; END IF;
                 RETURN NEW;
               END $$;
               CREATE TRIGGER reject_sync_audit BEFORE INSERT ON directory_change_audit
                 FOR EACH ROW EXECUTE FUNCTION reject_sync_audit()"""
        )
    with pytest.raises(psycopg.errors.RaiseException, match="injected audit failure"):
        worker.run_once(worker_id="applier")
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM directory_change_audit").fetchone() == before
        assert (
            db.execute("SELECT active_version FROM directory_control_state").fetchone() == version
        )
        for table in (
            "directory_person",
            "directory_membership",
            "directory_external_binding",
            "directory_sync_checkpoint",
        ):
            assert not db.execute(f"SELECT 1 FROM {table}").fetchone()
        failed = db.execute(
            "SELECT status,error_code FROM directory_sync_job WHERE id=%s",
            (UUID(job["id"]),),
        ).fetchone()
        assert failed and failed["status"] == "failed"
        assert failed["error_code"] == "sync_execution_failed"
