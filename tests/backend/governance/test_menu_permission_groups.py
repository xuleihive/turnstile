from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from tests.backend.governance.test_directory_service import directory as directory
from tests.backend.governance.test_directory_service import hierarchy
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from turnstile_core.domain.directory import (
    AccountLinkWrite,
    AdministratorsWrite,
    DirectoryError,
    DirectoryPrincipal,
    PersonCreate,
    PersonWrite,
)
from turnstile_core.domain.menu_permissions import MENU_GROUPS, MenuPermissionGroup
from turnstile_core.services.directory_service import DirectoryService


def linked_member(
    service: DirectoryService, owner: DirectoryPrincipal
) -> tuple[dict[str, Any], dict[str, Any], UUID]:
    _, department, _ = hierarchy(service, owner)
    account_id = uuid4()
    with service.store.connection() as connection:
        connection.execute(
            "INSERT INTO app_user(id,email) VALUES (%s,'member@example.com')",
            (account_id,),
        )
    person = service.create_person(
        owner,
        PersonCreate(
            governance_user_id="member@example.com",
            display_name="Member",
            job_title="organization_admin",
            department_id=department["id"],
        ),
    )
    person = service.link_account(
        owner,
        UUID(person["id"]),
        AccountLinkWrite(app_user_id=account_id, expected_revision=person["revision"]),
    )
    return person, department, account_id


@pytest.mark.parametrize("group", list(MENU_GROUPS))
def test_menu_assignment_does_not_grant_data_scope_owner_or_gateway_policy(
    directory: tuple[DirectoryService, DirectoryPrincipal], group: MenuPermissionGroup
) -> None:
    service, owner = directory
    person, _, account_id = linked_member(service, owner)
    before = service.store.principal(account_id, "member@example.com", "member")
    assert before.menu_permission_group == "user"
    assert before.data_scope is not None
    assert before.data_scope.department_ids == ()
    assert before.data_scope.user_ids == ("member@example.com",)
    assert before.capabilities == ("directory.read",)
    state = service.store.state()
    with service.store.connection() as connection:
        outbox_count = connection.execute(
            "SELECT count(*) AS total FROM directory_outbox"
        ).fetchone()
    saved = service.update_person(
        owner,
        UUID(person["id"]),
        PersonWrite(
            display_name=person["display_name"],
            expected_revision=person["revision"],
            menu_permission_group=group,
        ),
    )
    after = service.store.principal(account_id, "member@example.com", "member")
    assert saved["menu_permission_group"] == after.menu_permission_group == group
    assert saved["job_title"] == "organization_admin"
    assert "_navigation_only" not in saved
    assert after.menu_permissions == MENU_GROUPS[group]
    assert after.role == "member" and not after.owner
    assert after.department_ids == before.department_ids
    assert after.capabilities == before.capabilities
    assert service.store.state()["active_version"] == state["active_version"]
    assert after.permission_revision > before.permission_revision
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT count(*) AS total FROM directory_outbox"
        ).fetchone() == outbox_count
        assert connection.execute(
            "SELECT role FROM app_user WHERE id=%s", (account_id,)
        ).fetchone() == {"role": "member"}
        for table in ("user_model_access", "user_model_policy", "token_budget"):
            assert connection.execute(
                f"SELECT count(*) AS total FROM {table}"
            ).fetchone() == {"total": 0}


def test_department_appointment_sets_menu_identity_and_cannot_assign_other_groups(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    person, department, account_id = linked_member(service, owner)
    service.set_administrators(
        owner,
        department["id"],
        AdministratorsWrite(expected_revision=department["revision"], account_ids=[account_id]),
    )
    delegated = service.store.principal(account_id, "member@example.com", "member")
    assert delegated.department_ids == (department["id"],)
    assert delegated.menu_permission_group == "department_admin"
    with pytest.raises(DirectoryError, match="Owner"):
        service.update_person(
            delegated,
            UUID(person["id"]),
            PersonWrite(
                display_name="Member",
                expected_revision=person["revision"],
                menu_permission_group="organization_admin",
            ),
        )
    with pytest.raises(DirectoryError, match="Owner"):
        service.create_person(
            delegated,
            PersonCreate(
                display_name="Elevated",
                governance_user_id="elevated@example.com",
                department_id=department["id"],
                menu_permission_group="team_admin",
            ),
        )
    created = service.create_person(
        delegated,
        PersonCreate(
            display_name="Ordinary",
            governance_user_id="ordinary@example.com",
            department_id=department["id"],
        ),
    )
    assert created["menu_permission_group"] == "user"


def test_disabled_or_unlinked_person_loses_menu_group_without_changing_data_policy(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    person, _, account_id = linked_member(service, owner)
    service.update_person(
        owner,
        UUID(person["id"]),
        PersonWrite(
            display_name="Member",
            expected_revision=person["revision"],
            menu_permission_group="team_admin",
        ),
    )
    assert "organization-management" in service.store.principal(
        account_id, "member@example.com", "member"
    ).menu_permissions
    for field, inactive, active in (
        ("manual_disabled", True, False),
        ("status", "inactive", "active"),
    ):
        with service.store.connection() as connection:
            connection.execute(
                f"UPDATE directory_person SET {field}=%s WHERE id=%s", (inactive, person["id"])
            )
        assert service.store.principal(
            account_id, "member@example.com", "member"
        ).menu_permission_group == "user"
        with service.store.connection() as connection:
            connection.execute(
                f"UPDATE directory_person SET {field}=%s WHERE id=%s", (active, person["id"])
            )
    with service.store.connection() as connection:
        connection.execute("DELETE FROM directory_account_link WHERE app_user_id=%s", (account_id,))
    assert service.store.principal(
        account_id, "member@example.com", "member"
    ).menu_permission_group == "user"
    assert "settings" in owner.menu_permissions


def test_menu_group_validation_rejects_owner_and_unknown_values() -> None:
    from pydantic import ValidationError

    for value in ("owner", "system", "Entra Group", ""):
        with pytest.raises(ValidationError):
            PersonWrite.model_validate({"display_name": "Member", "menu_permission_group": value})


@pytest.mark.parametrize(
    "groups",
    [
        ["team_admin", "department_admin"],
        ["team_admin", "organization_admin"],
        ["organization_admin", "department_admin"],
        ["team_admin", "organization_admin", "department_admin"],
    ],
)
def test_multiple_groups_union_menus_and_removing_one_preserves_other_assignments(
    directory: tuple[DirectoryService, DirectoryPrincipal], groups: list[MenuPermissionGroup]
) -> None:
    service, owner = directory
    person, _, account_id = linked_member(service, owner)
    saved = service.update_person(
        owner,
        UUID(person["id"]),
        PersonWrite(
            display_name=person["display_name"],
            expected_revision=person["revision"],
            menu_permission_groups=groups,
        ),
    )
    principal = service.store.principal(account_id, "member@example.com", "member")
    assert set(principal.menu_permission_groups) == set(groups)
    assert set(principal.menu_permissions) == {
        menu for group in groups for menu in MENU_GROUPS[group]
    }
    assert not principal.owner and principal.capabilities == ("directory.read",)
    with pytest.raises(DirectoryError):
        service.update_person(
            principal,
            UUID(person["id"]),
            PersonWrite(
                display_name=person["display_name"],
                expected_revision=saved["revision"],
                menu_permission_groups=[groups[0]],
            ),
        )
    single = service.update_person(
        owner,
        UUID(person["id"]),
        PersonWrite(
            display_name=person["display_name"],
            expected_revision=saved["revision"],
            menu_permission_groups=[groups[0]],
        ),
    )
    assert single["menu_permission_groups"] == [groups[0]]
    default = service.update_person(
        owner,
        UUID(person["id"]),
        PersonWrite(
            display_name=person["display_name"],
            expected_revision=single["revision"],
            menu_permission_groups=[],
        ),
    )
    assert default["menu_permission_groups"] == ["user"]
    assert service.store.principal(
        account_id, "member@example.com", "member"
    ).menu_permissions == MENU_GROUPS["user"]


def test_multiple_group_inputs_deduplicate_and_reject_conflicting_representations() -> None:
    from pydantic import ValidationError

    assert PersonWrite(
        display_name="Member", menu_permission_groups=["team_admin", "team_admin"]
    ).requested_menu_groups == ["team_admin"]
    with pytest.raises(ValidationError):
        PersonWrite(
            display_name="Member", menu_permission_groups=["user", "team_admin"]
        )
    with pytest.raises(ValidationError):
        PersonWrite(
            display_name="Member",
            menu_permission_group="team_admin",
            menu_permission_groups=["department_admin"],
        )


def test_person_profile_edit_without_menu_fields_preserves_all_assignments(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    person, _, account_id = linked_member(service, owner)
    assigned = service.update_person(
        owner,
        UUID(person["id"]),
        PersonWrite(
            display_name=person["display_name"],
            expected_revision=person["revision"],
            menu_permission_groups=["organization_admin", "department_admin", "team_admin"],
        ),
    )
    edited = service.update_person(
        owner,
        UUID(person["id"]),
        PersonWrite(
            display_name="Updated Profile",
            contact_email="contact@example.com",
            employee_number="profile-01",
            expected_revision=assigned["revision"],
        ),
    )
    assert edited["display_name"] == "Updated Profile"
    assert edited["menu_permission_groups"] == assigned["menu_permission_groups"]
    assert edited["menu_permission_group"] == assigned["menu_permission_group"]
    principal = service.store.principal(account_id, "member@example.com", "member")
    assert set(principal.menu_permission_groups) == {
        "organization_admin", "department_admin", "team_admin"
    }
