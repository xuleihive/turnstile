from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from tests.backend.governance.test_directory_service import directory as directory
from tests.backend.governance.test_directory_service import hierarchy
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from turnstile_core.domain.directory import (
    DirectoryError,
    DirectoryPrincipal,
    OrganizationPersonCreate,
    OrganizationWrite,
    UnitMembersWrite,
    UnitWrite,
)
from turnstile_core.services.directory_service import DirectoryService
from turnstile_core.services.passwords import verify_password


def create_member(
    service: DirectoryService, owner: DirectoryPrincipal, organization_id: str,
    department_id: str,
    email: str = "created@example.com",
) -> dict[str, Any]:
    return service.create_organization_person(
        owner, OrganizationPersonCreate(
            organization_id=organization_id, department_id=department_id,
            display_name="Created", email=email,
            password=SecretStr("test-new-member-password"),
        ),
    )


def test_organization_creation_is_atomic_private_and_idempotent(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, department, _ = hierarchy(service, owner)
    write = OrganizationPersonCreate(
        organization_id=org["id"], department_id=department["id"],
        display_name="Created", email="created@example.com",
        password=SecretStr("  test-new-member-password  "),
    )
    person = service.create_organization_person(owner, write, "create-key")
    assert person == service.create_organization_person(owner, write, "create-key")
    assert person["department_id"] == department["id"]
    assert person["organization_id"] == org["id"]
    assert person["team_ids"] == [] and person["menu_permission_groups"] == ["user"]
    assert service.people(owner, organization_id=org["id"]).total == 1
    assert service.store.catalog().users[0].parent_id == department["id"]
    with service.store.connection() as db:
        account = db.execute(
            "SELECT * FROM app_user WHERE id=%s", (UUID(person["app_user_id"]),),
        ).fetchone()
        assert account and account["role"] == "member"
        assert verify_password("  test-new-member-password  ", account["password_hash"])
        assert not db.execute("SELECT 1 FROM token_budget").fetchone()
        assert not db.execute("SELECT 1 FROM user_model_access").fetchone()
        evidence = db.execute(
            "SELECT result,request_digest FROM directory_idempotency"
        ).fetchall()
        assert "test-new-member-password" not in str(evidence)
        assert "password_hash" not in str(person)
    with pytest.raises(DirectoryError, match="another request"):
        service.create_organization_person(
            owner, write.model_copy(
                update={"password": write.password.__class__("other-password")}
            ),
            "create-key",
        )
    with pytest.raises(DirectoryError, match="already exists"):
        service.create_organization_person(owner, write)


def test_add_existing_members_keeps_primary_attribution_and_rejects_cross_org(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, first, second = hierarchy(service, owner)
    team = service.create_unit(
        owner, org["id"],
        UnitWrite(code="team", name="Team", kind="team", parent_unit_id=second["id"]),
    )
    person = create_member(service, owner, org["id"], second["id"])
    assert service.people(owner, available_unit_id=first["id"]).total == 1
    first = service.add_unit_members(
        owner, first["id"],
        UnitMembersWrite(person_ids=[UUID(person["id"])], expected_revision=first["revision"]),
    )
    assert service.people(owner, available_unit_id=first["id"]).total == 0
    assert service.people(owner, department_id=first["id"]).total == 1
    service.add_unit_members(
        owner, team["id"],
        UnitMembersWrite(person_ids=[UUID(person["id"])], expected_revision=team["revision"]),
    )
    current = service.people(owner).items[0]
    assert current["department_id"] == second["id"] and current["team_ids"] == [team["id"]]
    assert service.store.catalog().users[0].parent_id == second["id"]
    assert service.people(owner, available_unit_id=team["id"]).total == 0
    with pytest.raises(DirectoryError, match="already"):
        service.add_unit_members(
            owner, second["id"],
            UnitMembersWrite(
                person_ids=[UUID(person["id"])], expected_revision=second["revision"],
            ),
        )
    foreign = service.create_organization(owner, OrganizationWrite(code="foreign", name="Foreign"))
    foreign_department = service.create_unit(
        owner, foreign["id"], UnitWrite(code="foreign", name="Foreign", kind="department"),
    )
    outsider = create_member(
        service, owner, foreign["id"], foreign_department["id"], "foreign@example.com"
    )
    with pytest.raises(DirectoryError, match="outside"):
        service.add_unit_members(
            owner, first["id"],
            UnitMembersWrite(
                person_ids=[UUID(outsider["id"])], expected_revision=first["revision"],
            ),
        )
    with pytest.raises(DirectoryError, match="parent"):
        service.create_unit(
            owner, org["id"],
            UnitWrite(code="nested", name="Nested", kind="team", parent_unit_id=team["id"]),
        )


def test_invalid_department_and_failed_person_insert_never_create_an_account(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, department, _ = hierarchy(service, owner)
    foreign = service.create_organization(owner, OrganizationWrite(code="foreign", name="Foreign"))
    foreign_department = service.create_unit(
        owner, foreign["id"], UnitWrite(code="foreign", name="Foreign", kind="department"),
    )
    with pytest.raises(DirectoryError, match="outside"):
        create_member(service, owner, org["id"], foreign_department["id"])
    with service.store.connection() as db:
        db.execute(
            """INSERT INTO directory_person(governance_user_id,display_name,updated_by)
               VALUES ('created@example.com','Legacy','test')"""
        )
    with pytest.raises(DirectoryError):
        create_member(service, owner, org["id"], department["id"])
    with service.store.connection() as db:
        assert not db.execute(
            "SELECT 1 FROM app_user WHERE email='created@example.com'"
        ).fetchone()


def test_member_batch_rolls_back_and_membership_never_grants_department_authority(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, first, second = hierarchy(service, owner)
    person = create_member(service, owner, org["id"], first["id"])
    foreign = service.create_organization(owner, OrganizationWrite(code="foreign", name="Foreign"))
    foreign_department = service.create_unit(
        owner, foreign["id"], UnitWrite(code="foreign", name="Foreign", kind="department"),
    )
    outsider = create_member(
        service, owner, foreign["id"], foreign_department["id"], "foreign@example.com"
    )
    with pytest.raises(DirectoryError):
        service.add_unit_members(
            owner, second["id"], UnitMembersWrite(
                person_ids=[UUID(person["id"]), UUID(outsider["id"])],
                expected_revision=second["revision"],
            ),
        )
    assert service.people(owner, department_id=second["id"]).total == 0
    service.add_unit_members(
        owner, second["id"], UnitMembersWrite(
            person_ids=[UUID(person["id"])], expected_revision=second["revision"],
        ),
    )
    principal = service.store.principal(
        UUID(person["app_user_id"]), person["account_email"], "member"
    )
    assert principal.capabilities == ("directory.read",)
    assert principal.data_scope is not None and principal.data_scope.department_ids == ()
    assert principal.data_scope.user_ids == (person["governance_user_id"],)
    assert principal.menu_permission_groups == ("user",)
    with service.store.connection() as db:
        assert not db.execute(
            "SELECT 1 FROM directory_department_grant WHERE app_user_id=%s",
            (UUID(person["app_user_id"]),),
        ).fetchone()
