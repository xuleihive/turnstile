from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from tests.backend.governance.test_directory_service import directory as directory
from tests.backend.governance.test_menu_permission_groups import linked_member
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from turnstile_core.domain.directory import (
    AdministratorsWrite,
    DirectoryError,
    DirectoryPrincipal,
    PersonWrite,
    TeamsWrite,
    UnitWrite,
)
from turnstile_core.services.directory_service import DirectoryService


def scopes(
    service: DirectoryService, owner: DirectoryPrincipal
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], UUID, str]:
    person, department, account_id = linked_member(service, owner)
    team = service.create_unit(
        owner, department["organization_id"],
        UnitWrite(kind="team", name="Team", code="team", parent_unit_id=department["id"]),
    )
    service.set_teams(
        owner, UUID(person["id"]),
        TeamsWrite(team_ids=[team["id"]], expected_revision=person["revision"]),
    )
    organization = next(
        org for org in service.organizations(owner) if org["id"] == department["organization_id"]
    )
    return organization, department, team, account_id, person["id"]


def test_appointments_share_labels_profile_and_do_not_alias_department_and_team(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, dept, team, account_id, person_id = scopes(service, owner)
    state = service.store.state()
    dept = service.set_administrators(
        owner, dept["id"],
        AdministratorsWrite(account_ids=[account_id], expected_revision=dept["revision"]),
    )
    assert service.menu_administrators(owner, "department", dept["id"])[0]["effective"]
    assert service.menu_administrators(owner, "team", team["id"]) == []
    assert service.people(owner).items[0]["menu_permission_groups"] == ["department_admin"]
    team = service.set_menu_administrators(
        owner, "team", team["id"],
        AdministratorsWrite(account_ids=[account_id], expected_revision=team["revision"]),
    )
    assert service.people(owner).items[0]["menu_permission_groups"] == [
        "department_admin", "team_admin",
    ]
    principal = service.store.principal(account_id, "member@example.com", "member")
    assert principal.menu_permission_groups == ("department_admin", "team_admin")
    assert principal.department_ids == (dept["id"],)
    assert principal.role == "member" and not principal.owner
    team = service.set_menu_administrators(
        owner, "team", team["id"],
        AdministratorsWrite(account_ids=[], expected_revision=team["revision"]),
    )
    assert service.people(owner).items[0]["menu_permission_groups"] == ["department_admin"]
    assert service.administrators(owner, dept["id"])[0]["app_user_id"] == str(account_id)
    org = service.set_menu_administrators(
        owner, "organization", org["id"],
        AdministratorsWrite(account_ids=[account_id], expected_revision=org["revision"]),
    )
    principal = service.store.principal(account_id, "member@example.com", "member")
    assert set(principal.menu_permission_groups) == {"department_admin", "organization_admin"}
    assert "settings" not in principal.menu_permissions and not principal.owner
    assert dept["id"] in (principal.department_ids or ())
    assert service.store.state()["active_version"] == state["active_version"]
    with service.store.connection() as db:
        assert db.execute(
            "SELECT menu_permission_groups FROM directory_person WHERE id=%s", (person_id,)
        ).fetchone() == {"menu_permission_groups": ["user"]}
        assert not db.execute("SELECT 1 FROM user_model_access").fetchone()
        assert not db.execute("SELECT 1 FROM token_budget").fetchone()
    service.set_administrators(
        owner, dept["id"],
        AdministratorsWrite(account_ids=[], expected_revision=dept["revision"]),
    )
    assert service.menu_administrators(owner, "department", dept["id"]) == []
    assert service.store.principal(
        account_id, "member@example.com", "member"
    ).data_scope is not None


def test_team_requires_real_membership_and_owner_and_stale_saves_are_rejected(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, dept, team, account_id, person_id = scopes(service, owner)
    other_team = service.create_unit(
        owner, org["id"],
        UnitWrite(kind="team", name="Other", code="other", parent_unit_id=dept["id"]),
    )
    with pytest.raises(DirectoryError, match="active linked"):
        service.set_menu_administrators(
            owner, "team", other_team["id"],
            AdministratorsWrite(account_ids=[account_id], expected_revision=other_team["revision"]),
        )
    member = service.store.principal(account_id, "member@example.com", "member")
    with pytest.raises(DirectoryError, match="Owner"):
        service.set_menu_administrators(
            member, "organization", org["id"],
            AdministratorsWrite(account_ids=[account_id], expected_revision=org["revision"]),
        )
    with pytest.raises(DirectoryError, match="scope"):
        service.menu_administrators(owner, "department", team["id"])
    appointed = service.set_menu_administrators(
        owner, "team", team["id"],
        AdministratorsWrite(account_ids=[account_id], expected_revision=team["revision"]),
    )
    with pytest.raises(DirectoryError, match="changed"):
        service.set_menu_administrators(
            owner, "team", team["id"],
            AdministratorsWrite(account_ids=[], expected_revision=team["revision"]),
        )
    person = service.people(owner).items[0]
    service.set_teams(
        owner, UUID(person_id),
        TeamsWrite(team_ids=[], expected_revision=person["revision"]),
    )
    assert service.people(owner).items[0]["menu_permission_groups"] == ["user"]
    assert service.store.principal(
        account_id, "member@example.com", "member"
    ).menu_permission_groups == ("user",)
    assert not service.menu_administrators(owner, "team", appointed["id"])[0]["effective"]


def test_owner_profile_keeps_appointments_and_person_edit_cannot_remove_them(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, dept, _, account_id, person_id = scopes(service, owner)
    service.set_menu_administrators(
        owner, "organization", org["id"],
        AdministratorsWrite(account_ids=[account_id], expected_revision=org["revision"]),
    )
    principal = service.store.principal(account_id, "member@example.com", "owner")
    assert principal.owner and principal.menu_permission_groups == ("organization_admin",)
    person = service.people(owner).items[0]
    edited = service.update_person(
        owner, UUID(person_id),
        PersonWrite(display_name="Changed", menu_permission_groups=[],
                    expected_revision=person["revision"]),
    )
    assert edited["menu_permission_groups"] == ["organization_admin"]
    assert service.menu_administrators(owner, "organization", org["id"])
