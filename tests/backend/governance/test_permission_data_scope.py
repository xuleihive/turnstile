from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import HTTPException

from backend.services.assistant import AssistantService
from backend.services.assistant_tools import AssistantTools, TimeScopedArguments
from backend.services.runtime_service import ModelRuntimeService
from tests.backend.assistant.test_assistant import _any_chart
from tests.backend.governance.test_directory_service import directory as directory
from tests.backend.governance.test_scoped_menu_administrators import scopes
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import (
    local_postgres as local_postgres,
)
from turnstile_core.config import Settings
from turnstile_core.domain.assistant_models import PinnedChartWrite
from turnstile_core.domain.data_access import DataAccessScope
from turnstile_core.domain.directory import (
    AdministratorsWrite,
    DirectoryPrincipal,
    UnitMembersWrite,
    UnitWrite,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.directory_service import DirectoryService


def test_cross_department_and_team_scope_uses_members_without_multiplying_role_scopes(
    directory: tuple[DirectoryService, DirectoryPrincipal],
) -> None:
    service, owner = directory
    org, department, team, account, person = scopes(service, owner)
    second = next(
        row
        for row in service.units(owner, org["id"])
        if row["kind"] == "department" and row["id"] != department["id"]
    )
    another = service.create_unit(
        owner,
        org["id"],
        UnitWrite(
            name="Cross team",
            code="cross",
            kind="team",
            parent_unit_id=second["id"],
        ),
    )
    service.add_unit_members(
        owner,
        second["id"],
        UnitMembersWrite(
            person_ids=[UUID(person)],
            expected_revision=second["revision"],
        ),
    )
    service.add_unit_members(
        owner,
        another["id"],
        UnitMembersWrite(
            person_ids=[UUID(person)],
            expected_revision=another["revision"],
        ),
    )
    team = next(row for row in service.units(owner, org["id"]) if row["id"] == team["id"])
    service.set_menu_administrators(
        owner,
        "team",
        team["id"],
        AdministratorsWrite(
            account_ids=[account],
            expected_revision=team["revision"],
        ),
    )
    principal = service.store.principal(account, "member@example.com", "member")
    assert principal.data_scope is not None
    assert set(principal.data_scope.team_ids) == {team["id"], another["id"]}
    assert principal.data_scope.department_ids == ()
    assert not principal.data_scope.allows(second["id"], "unrelated@example.com")
    second = next(row for row in service.units(owner, org["id"]) if row["id"] == second["id"])
    service.set_menu_administrators(
        owner,
        "department",
        second["id"],
        AdministratorsWrite(
            account_ids=[account],
            expected_revision=second["revision"],
        ),
    )
    principal = service.store.principal(account, "member@example.com", "member")
    assert principal.is_menu_administrator("department", second["id"])
    assert not principal.is_menu_administrator("department", department["id"])
    assert principal.data_scope is not None
    assert set(principal.data_scope.department_ids) == {department["id"], second["id"]}


def test_snapshot_coverage_does_not_accept_global_or_department_snapshot_for_team() -> None:
    team = DataAccessScope(user_ids=("one", "two"))
    assert not team.covers(None)
    assert not team.covers([])
    assert not team.covers(["department-a"])
    assert team.covers(["scope:v2", "user:one"])
    assert not team.covers(["scope:v2", "user:other"])
    assert not team.covers(["scope:v2", "department:department-a"])


def test_assistant_persisted_user_scope_and_old_global_snapshot_denial() -> None:
    repository = InMemoryRepository()
    settings = Settings()
    runtime = ModelRuntimeService(repository, settings)
    scope = DataAccessScope(user_ids=("one",))
    scoped = AssistantService(repository, runtime, settings, data_scope=scope)
    write = PinnedChartWrite(title="Scoped", original_question="Test", chart=_any_chart())
    saved = scoped.pin(write, "one")
    stored = repository.get_pinned_report(saved.id, "one")
    assert stored is not None and stored["directory_scope"] == scope.snapshot()
    narrowed = AssistantService(repository, runtime, settings,
                                data_scope=DataAccessScope(user_ids=("other",)))
    with pytest.raises(HTTPException):
        narrowed.get_pinned(saved.id, "one")
    global_service = AssistantService(repository, runtime, settings)
    global_report = global_service.pin(write, "one")
    with pytest.raises(HTTPException):
        scoped.get_pinned(global_report.id, "one")
    tools = AssistantTools(repository, lambda: {}, ("department-a",), scope)
    filters = tools._filters(TimeScopedArguments())
    assert filters.data_scope == scope and filters.allowed_department_ids is None
