from uuid import UUID

import pytest

from tests.backend.governance.test_directory_service import directory as directory
from tests.backend.governance.test_scoped_menu_administrators import scopes
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from turnstile_core.domain.directory import (
    AdministratorsWrite,
    DirectoryError,
    DirectoryPrincipal,
    OrganizationWrite,
    PersonWrite,
    TeamsWrite,
    UnitMembersWrite,
    UnitWrite,
)
from turnstile_core.domain.menu_permissions import MENU_GROUPS
from turnstile_core.services.directory_service import DirectoryService


def test_same_named_team_appointment_does_not_match_other_team_or_parent(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, department, team, account_id, person_id = scopes(service, owner)
    other = service.create_unit(
        owner, org["id"],
        UnitWrite(code="other", name=team["name"], kind="team", parent_unit_id=department["id"]),
    )
    person = service.people(owner).items[0]
    service.set_teams(
        owner, UUID(person_id), TeamsWrite(
            team_ids=[team["id"], other["id"]], expected_revision=person["revision"],
        ),
    )
    other = service.set_menu_administrators(
        owner, "team", other["id"],
        AdministratorsWrite(account_ids=[account_id], expected_revision=other["revision"]),
    )
    principal = service.store.principal(account_id, "member@example.com", "member")
    assert principal.menu_permission_groups == ("team_admin",)
    assert principal.is_menu_administrator("team", other["id"])
    assert not principal.is_menu_administrator("team", team["id"])
    assert not principal.is_menu_administrator("department", department["id"])
    assert principal.menu_permissions_for_scope("team", team["id"]) == MENU_GROUPS["user"]
    assert principal.menu_permissions_for_scope("team", other["id"]) == MENU_GROUPS["team_admin"]
    for params, expected in (
        ({"team_id": team["id"]}, ["user"]),
        ({"team_id": other["id"]}, ["team_admin"]),
        ({"department_id": department["id"]}, ["user"]),
        ({"organization_id": org["id"]}, ["user"]),
    ):
        row = service.people(owner, **params).items[0]
        assert row["menu_permission_groups"] == ["team_admin"]
        assert row["scope_menu_permission_groups"] == expected
    service.set_menu_administrators(
        owner, "team", other["id"],
        AdministratorsWrite(account_ids=[], expected_revision=other["revision"]),
    )
    assert service.store.principal(
        account_id, "member@example.com", "member"
    ).menu_administrator_scopes == ()


def test_organization_department_and_legacy_groups_do_not_create_other_scope_rights(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, department, _, account_id, person_id = scopes(service, owner)
    foreign = service.create_organization(
        owner, OrganizationWrite(code="foreign", name=org["name"]),
    )
    other = service.create_unit(
        owner, org["id"], UnitWrite(code="other", name=department["name"], kind="department"),
    )
    service.add_unit_members(
        owner, other["id"], UnitMembersWrite(
            person_ids=[UUID(person_id)], expected_revision=other["revision"],
        ),
    )
    service.set_menu_administrators(
        owner, "organization", org["id"],
        AdministratorsWrite(account_ids=[account_id], expected_revision=org["revision"]),
    )
    service.set_menu_administrators(
        owner, "department", department["id"],
        AdministratorsWrite(account_ids=[account_id], expected_revision=department["revision"]),
    )
    row = service.people(owner, department_id=other["id"]).items[0]
    assert row["scope_menu_permission_groups"] == ["user"]
    assert set(row["menu_permission_groups"]) == {"organization_admin", "department_admin"}
    principal = service.store.principal(account_id, "member@example.com", "member")
    assert principal.is_menu_administrator("organization", org["id"])
    assert not principal.is_menu_administrator("organization", foreign["id"])
    assert principal.menu_permissions_for_scope(
        "organization", foreign["id"]
    ) == MENU_GROUPS["user"]
    assert principal.department_ids == tuple(sorted(
        row["id"] for row in service.units(owner, org["id"]) if row["kind"] == "department"
    ))
    scoped = service.people(principal, department_id=department["id"]).items[0]
    assert scoped["scope_menu_permission_groups"] == ["department_admin"]
    assert all(
        item["scope_kind"] == "department" and item["scope_id"] == department["id"]
        for item in scoped["menu_administrator_scopes"]
    )
    with pytest.raises(DirectoryError):
        principal.require("directory.edit_people", other["id"])
    assert service.people(principal, department_id=other["id"]).total == 1
    person = service.people(owner).items[0]
    service.update_person(
        owner, UUID(person_id), PersonWrite(
            display_name=person["display_name"], expected_revision=person["revision"],
            menu_permission_groups=["team_admin"],
        ),
    )
    principal = service.store.principal(account_id, "member@example.com", "member")
    assert "team_admin" in principal.menu_permission_groups
    assert not principal.is_menu_administrator("team", "arbitrary-team")
    assert all(item.organization_id == org["id"] for item in principal.menu_administrator_scopes)


def test_department_capabilities_are_matched_to_the_granted_department() -> None:
    principal = DirectoryPrincipal(
        email="member@example.com", department_ids=("read-only", "editable"),
        capabilities=("directory.read", "directory.edit_people"),
        department_capabilities={
            "read-only": ("directory.read",),
            "editable": ("directory.read", "directory.edit_people"),
        },
    )
    principal.require("directory.read", "read-only")
    principal.require("directory.edit_people", "editable")
    with pytest.raises(DirectoryError):
        principal.require("directory.edit_people", "read-only")
    with pytest.raises(DirectoryError):
        principal.require("directory.read", "other")
