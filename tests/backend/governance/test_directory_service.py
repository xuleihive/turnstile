from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import (
    local_postgres as local_postgres,
)
from turnstile_core.domain.directory import (
    AccountLinkWrite,
    AdministratorsWrite,
    DirectoryError,
    DirectoryPrincipal,
    OrganizationWrite,
    PersonCreate,
    PersonStatusWrite,
    PersonWrite,
    TeamsWrite,
    UnitWrite,
)
from turnstile_core.persistence.directory_store import DirectoryStore
from turnstile_core.services.directory_service import DirectoryService


@pytest.fixture
def directory(
    directory_database: str,
) -> Iterator[tuple[DirectoryService, DirectoryPrincipal]]:
    store = DirectoryStore(directory_database)
    store.initialize_empty(person_admission_mode="bff_only")
    owner_id = uuid4()
    with psycopg.connect(directory_database) as connection:
        connection.execute(
            "INSERT INTO app_user(id,email,role) VALUES (%s,'owner@example.com','owner')",
            (owner_id,),
        )
    principal = DirectoryPrincipal(
        account_id=owner_id,
        email="owner@example.com",
        role="owner",
    )
    service = DirectoryService(store)
    yield service, principal
    store.close()


def hierarchy(
    service: DirectoryService,
    owner: DirectoryPrincipal,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    organization = service.create_organization(
        owner, OrganizationWrite(code="example", name="Example")
    )
    first = service.create_unit(
        owner,
        organization["id"],
        UnitWrite(code="one", name="One", kind="department"),
    )
    second = service.create_unit(
        owner,
        organization["id"],
        UnitWrite(code="two", name="Two", kind="department"),
    )
    return organization, first, second


def test_directory_creation_is_idempotent_and_identity_is_immutable(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    organization, department, _ = hierarchy(service, owner)
    write = PersonCreate(
        governance_user_id="first@example.com",
        display_name="First",
        department_id=str(department["id"]),
    )
    first = service.create_person(owner, write, "person-key")
    second = service.create_person(owner, write, "person-key")
    assert first == second
    with pytest.raises(DirectoryError, match="another request"):
        service.create_person(
            owner,
            write.model_copy(update={"display_name": "Different"}),
            "person-key",
        )
    edited = service.update_person(
        owner,
        UUID(first["id"]),
        PersonWrite(
            expected_revision=first["revision"],
            display_name="Renamed",
            contact_email="new@example.com",
        ),
    )
    assert edited["governance_user_id"] == "first@example.com"
    assert service.store.catalog().users[0].name == "Renamed"
    assert service.people(owner).total == 1
    assert service.organizations(owner)[0]["id"] == organization["id"]
    with pytest.raises(DirectoryError, match="changed"):
        service.update_person(
            owner,
            UUID(first["id"]),
            PersonWrite(
                expected_revision=first["revision"],
                display_name="Stale",
            ),
        )


def test_department_admin_cannot_read_or_write_another_department(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    organization, first, second = hierarchy(service, owner)
    member_id = uuid4()
    with service.store.connection() as connection:
        connection.execute(
            "INSERT INTO app_user(id,email) VALUES (%s,'admin@example.com')",
            (member_id,),
        )
    administrator = service.create_person(
        owner,
        PersonCreate(
            governance_user_id="admin@example.com",
            display_name="Administrator",
            department_id=str(first["id"]),
        ),
    )
    service.link_account(
        owner,
        UUID(administrator["id"]),
        AccountLinkWrite(
            expected_revision=administrator["revision"],
            app_user_id=member_id,
        ),
    )
    service.set_administrators(
        owner,
        str(first["id"]),
        AdministratorsWrite(
            expected_revision=int(first["revision"]),
            account_ids=[member_id],
        ),
    )
    outsider = service.create_person(
        owner,
        PersonCreate(
            governance_user_id="outside@example.com",
            display_name="Outside",
            department_id=str(second["id"]),
        ),
    )
    principal = service.store.principal(member_id, "admin@example.com", "member")
    assert principal.department_ids == (first["id"],)
    assert service.people(principal).total == 1
    assert all(
        row["id"] != second["id"] for row in service.units(principal, str(organization["id"]))
    )
    with pytest.raises(DirectoryError):
        service.update_person(
            principal,
            UUID(outsider["id"]),
            PersonWrite(
                expected_revision=outsider["revision"],
                display_name="Forbidden",
            ),
        )
    with pytest.raises(DirectoryError):
        service.create_organization(
            principal, OrganizationWrite(code="forbidden", name="Forbidden")
        )
    with pytest.raises(DirectoryError):
        service.set_administrators(
            principal,
            str(first["id"]),
            AdministratorsWrite(
                expected_revision=2,
                account_ids=[member_id],
            ),
        )


def test_team_memberships_do_not_duplicate_people_or_move_budgets(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    organization, department, other = hierarchy(service, owner)
    team = service.create_unit(
        owner,
        str(organization["id"]),
        UnitWrite(
            code="team",
            name="Team",
            kind="team",
            parent_unit_id=str(department["id"]),
        ),
    )
    other_team = service.create_unit(
        owner,
        str(organization["id"]),
        UnitWrite(
            code="other-team",
            name="Other",
            kind="team",
            parent_unit_id=str(other["id"]),
        ),
    )
    person = service.create_person(
        owner,
        PersonCreate(
            governance_user_id="member@example.com",
            display_name="Member",
            department_id=str(department["id"]),
            team_ids=[team["id"], team["id"]],
        ),
    )
    assert person["team_ids"] == [team["id"]]
    assert service.people(owner).total == 1
    assert service.store.catalog().users[0].parent_id == department["id"]
    service.set_teams(
        owner,
        UUID(person["id"]),
        TeamsWrite(expected_revision=person["revision"], team_ids=[other_team["id"]]),
    )
    assert service.people(owner).items[0]["team_ids"] == [other_team["id"]]
    assert service.store.catalog().users[0].parent_id == department["id"]


def test_person_disable_preserves_model_policy_and_last_owner(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    _, department, _ = hierarchy(service, owner)
    assert owner.account_id is not None
    person = service.create_person(
        owner,
        PersonCreate(
            governance_user_id=owner.email,
            display_name="Owner",
            department_id=str(department["id"]),
        ),
    )
    linked = service.link_account(
        owner,
        UUID(person["id"]),
        AccountLinkWrite(
            expected_revision=person["revision"],
            app_user_id=owner.account_id,
        ),
    )
    with service.store.connection() as connection:
        connection.execute(
            "INSERT INTO user_model_policy(user_id,updated_by) VALUES (%s,'test')",
            (owner.email,),
        )
    with pytest.raises(DirectoryError, match="last"):
        service.set_status(
            owner,
            UUID(person["id"]),
            PersonStatusWrite(
                expected_revision=linked["revision"],
                status="inactive",
                disable_account=True,
            ),
        )
    service.set_status(
        owner,
        UUID(person["id"]),
        PersonStatusWrite(
            expected_revision=linked["revision"],
            status="inactive",
        ),
    )
    assert service.store.catalog().users == []
    with service.store.connection() as connection:
        policy = connection.execute(
            "SELECT user_id FROM user_model_policy WHERE user_id=%s", (owner.email,)
        ).fetchone()
        assert policy is not None and policy["user_id"] == owner.email
