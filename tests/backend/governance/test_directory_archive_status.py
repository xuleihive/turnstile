from typing import Any, Literal
from uuid import UUID, uuid4

import pytest

from tests.backend.governance.test_directory_service import directory as directory
from tests.backend.governance.test_directory_service import hierarchy
from tests.backend.governance.test_organization_members import create_member
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from tests.backend.persistence.test_user_settings_migration import seed_history
from turnstile_core.domain.directory import (
    DirectoryError,
    DirectoryPrincipal,
    OrganizationWrite,
    PersonStatusWrite,
    TeamsWrite,
    UnitWrite,
)
from turnstile_core.services.directory_service import DirectoryService
from turnstile_core.services.directory_upgrade import billing_baseline


def test_legacy_write_status_is_normalized_without_removing_compatibility() -> None:
    assert OrganizationWrite(code="org", name="Org", status="inactive").status == "archived"
    unit = UnitWrite(code="dept", name="Dept", kind="department", status="inactive")
    assert unit.status == "archived"
    assert PersonStatusWrite(status="inactive").status == "archived"
    assert PersonStatusWrite(status="active").status == "active"


def test_archive_filter_includes_legacy_people_without_rewriting_records(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, department, _ = hierarchy(service, owner)
    legacy = create_member(service, owner, org["id"], department["id"], "legacy@example.com")
    archived = create_member(service, owner, org["id"], department["id"], "archived@example.com")
    active = create_member(service, owner, org["id"], department["id"], "active@example.com")
    with service.store.connection() as db:
        db.execute(
            "UPDATE directory_person SET status='inactive' WHERE id=%s", (UUID(legacy["id"]),),
        )
    saved = service.set_status(
        owner, UUID(archived["id"]),
        PersonStatusWrite(status="inactive", expected_revision=archived["revision"]),
    )
    assert saved["status"] == "archived"
    rows = service.people(owner, organization_id=org["id"], status="archived")
    assert {row["id"] for row in rows.items} == {legacy["id"], archived["id"]}
    current = service.people(owner, organization_id=org["id"], status="active")
    assert current.items[0]["id"] == active["id"]
    with service.store.connection() as db:
        stored = db.execute(
            "SELECT status FROM directory_person WHERE id=%s", (UUID(legacy["id"]),),
        ).fetchone()
        account = db.execute(
            "SELECT enabled FROM app_user WHERE id=%s", (UUID(archived["app_user_id"]),),
        ).fetchone()
        assert stored and stored["status"] == "inactive"
        assert account and account["enabled"]


@pytest.mark.parametrize("kind", ["organization", "department", "team"])
def test_archive_with_active_members_retains_people_children_and_business_records(
    directory: tuple[DirectoryService, DirectoryPrincipal],
    directory_database: str,
    kind: Literal["organization", "department", "team"],
) -> None:
    service, owner = directory
    org, first, second = hierarchy(service, owner)
    team = service.create_unit(
        owner, org["id"], UnitWrite(
            code="team", name="Team", kind="team", parent_unit_id=first["id"],
        ),
    )
    person = create_member(service, owner, org["id"], first["id"])
    service.set_teams(
        owner, UUID(person["id"]), TeamsWrite(
            team_ids=[team["id"]], expected_revision=person["revision"],
        ),
    )
    seed_history(directory_database)
    with service.store.connection() as db:
        db.execute(
            "INSERT INTO directory_project_reference(id,name,department_id) VALUES "
            "('preserved-project','Preserved project',%s)", (first["id"],),
        )
        db.execute(
            "INSERT INTO user_model_policy(user_id,updated_by) VALUES (%s,'test')",
            (person["governance_user_id"],),
        )
        preserved = billing_baseline(db, (
            "directory_person", "directory_membership", "directory_account_link",
            "directory_project_reference", "directory_department_grant",
            "directory_menu_administrator",
        ))
        billing = billing_baseline(db)
        children = db.execute("SELECT * FROM directory_unit ORDER BY id").fetchall()
    target = {"organization": org, "department": first, "team": team}[kind]

    def save(status: str, revision: int) -> dict[str, Any]:
        value = {
            "code": target["code"], "name": target["name"],
            "status": status, "expected_revision": revision,
        }
        if kind == "organization":
            return service.update_organization(owner, target["id"], OrganizationWrite(**value))
        return service.update_unit(
            owner, target["id"], UnitWrite(
                **value, kind=kind, parent_unit_id=target["parent_unit_id"],
            ),
        )

    saved = save("archived", target["revision"])
    assert saved["status"] == "archived"
    assert service.organizations(owner)[0]["id"] == org["id"]
    assert len(service.units(owner, org["id"])) == 3
    assert service.people(owner, organization_id=org["id"], status="active").total == 1
    assert service.people(owner, team_id=team["id"]).items[0]["team_ids"] == [team["id"]]
    with service.store.connection() as db:
        assert billing_baseline(db, tuple(preserved)) == preserved
        assert billing_baseline(db) == billing
        current_children = db.execute("SELECT * FROM directory_unit ORDER BY id").fetchall()
        assert [row for row in current_children if row["id"] != target["id"]] == [
            row for row in children if row["id"] != target["id"]
        ]
    restored = save("active", saved["revision"])
    assert restored["status"] == "active"
    assert {row.id for row in service.store.catalog().departments} == {first["id"], second["id"]}
    with pytest.raises(DirectoryError, match="changed"):
        save("archived", target["revision"])
    with service.store.connection() as db:
        assert billing_baseline(db, tuple(preserved)) == preserved
        assert billing_baseline(db) == billing


@pytest.mark.parametrize("kind", ["organization", "department", "team", "default_department"])
def test_archive_still_rejects_pending_sync_writes(
    directory: tuple[DirectoryService, DirectoryPrincipal],
    kind: str,
) -> None:
    service, owner = directory
    org, department, _ = hierarchy(service, owner)
    team = service.create_unit(
        owner, org["id"], UnitWrite(
            code="team", name="Team", kind="team", parent_unit_id=department["id"],
        ),
    )
    connection_id = uuid4()
    with service.store.connection() as db:
        db.execute(
            """INSERT INTO directory_connection(id,organization_id,name,tenant_id,
                 default_department_id,updated_by) VALUES (%s,%s,'Local test',%s,%s,'test')""",
            (connection_id, org["id"], uuid4(),
             department["id"] if kind == "default_department" else None),
        )
        if kind != "default_department":
            db.execute(
                "INSERT INTO directory_group_mapping(connection_id,group_id,unit_id) "
                "VALUES (%s,%s,%s)", (connection_id, uuid4(), team["id"]),
            )
        db.execute(
            """INSERT INTO directory_sync_job(connection_id,connection_revision,directory_version,
                 mode,requested_by,idempotency_key) VALUES (%s,1,1,'full','test',%s)""",
            (connection_id, str(uuid4())),
        )
    with pytest.raises(DirectoryError, match="pending directory jobs"):
        if kind == "organization":
            service.update_organization(
                owner, org["id"], OrganizationWrite(
                    code=org["code"], name=org["name"], status="archived",
                    expected_revision=org["revision"],
                ),
            )
        else:
            unit = team if kind == "team" else department
            service.update_unit(
                owner, unit["id"], UnitWrite(
                    code=unit["code"], name=unit["name"], kind=unit["kind"],
                    parent_unit_id=unit["parent_unit_id"], status="archived",
                    expected_revision=unit["revision"],
                ),
            )
    assert service.organizations(owner)[0]["status"] == "active"
    assert all(row["status"] == "active" for row in service.units(owner, org["id"]))
