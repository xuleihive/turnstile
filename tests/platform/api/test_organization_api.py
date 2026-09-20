from __future__ import annotations

from datetime import UTC, date, datetime

from backend.api import app
from backend.http.dependencies import get_repository
from backend.http.session import SessionIdentity, require_authenticated_session
from tests.platform.api.api_support import client
from turnstile_core.domain.organization import suggested_unit_id
from turnstile_core.persistence.in_memory import InMemoryRepository

pytest_plugins = ("tests.platform.api.api_fixtures",)

DIRECTORY = "/api/v1/organization/directory"
DEPARTMENTS = "/api/v1/organization/departments"
NAME = "/api/v1/organization/units/{0}/name"
STATUS = "/api/v1/organization/units/{0}/status"


def _install(repository: InMemoryRepository) -> None:
    app.dependency_overrides[get_repository] = lambda: repository


def _uninstall() -> None:
    app.dependency_overrides.pop(get_repository, None)
    app.dependency_overrides.pop(require_authenticated_session, None)


def test_a_fresh_install_reads_the_structure_the_catalog_used_to_hardcode() -> None:
    repository = InMemoryRepository()
    _install(repository)
    try:
        body = client.get(DIRECTORY).json()
        assert body["organization"]["id"] == "org-contoso-global"
        assert body["organization"]["display_name"] == "Contoso Global"
        assert {item["id"] for item in body["departments"]} == {
            "department-platform",
            "department-commerce",
            "department-finance",
            "department-support",
            "department-security",
        }
        # Ordered for a picker rather than by the order they were declared in.
        assert [item["display_name"] for item in body["departments"]] == [
            "AI Platform", "Commerce", "Customer Support", "Finance", "Security",
        ]
    finally:
        _uninstall()


def test_the_organization_can_be_renamed_to_this_installs_own_name() -> None:
    """The first thing any real deployment needs and the thing it could not do.

    Nothing joins on the display name, so this is a one-field write with no downstream
    consequence -- which is exactly why it is separated from the id, where the opposite
    is true.
    """
    repository = InMemoryRepository()
    _install(repository)
    try:
        response = client.put(
            NAME.format("org-contoso-global"), json={"display_name": "InSilico Medicine"}
        )
        assert response.status_code == 200
        assert response.json()["organization"]["display_name"] == "InSilico Medicine"
        assert response.json()["organization"]["id"] == "org-contoso-global"
        trail = repository.org_unit_audit
        assert [item["operation"] for item in trail] == ["renamed"]
        assert trail[0]["actor"] == "owner@contoso.com"
        assert trail[0]["before_state"] == {"display_name": "Contoso Global"}
    finally:
        _uninstall()


def test_a_department_can_be_created_and_is_immediately_attributable() -> None:
    repository = InMemoryRepository()
    _install(repository)
    try:
        response = client.post(
            DEPARTMENTS,
            json={"id": "department-drug-discovery", "display_name": "Drug Discovery"},
        )
        assert response.status_code == 201
        created = next(
            item for item in response.json()["departments"]
            if item["id"] == "department-drug-discovery"
        )
        assert created["display_name"] == "Drug Discovery"
        assert created["parent_id"] == "org-contoso-global"
        assert created["status"] == "active"

        # The point of creating it: something can now be filed under it. This is the same
        # check the channel-ownership route makes, so a department that exists here is a
        # department that can receive attribution without any further step.
        entities = client.get("/api/v1/enterprise/entities").json()
        assert "department-drug-discovery" in {
            item["id"] for item in entities["departments"]
        }
    finally:
        _uninstall()


def test_retiring_a_department_reports_what_still_names_it() -> None:
    repository = InMemoryRepository()
    repository.token_budgets[(date(2026, 9, 1), "department", "department-support")] = {
        "period_start": date(2026, 9, 1),
        "scope_type": "department",
        "scope_id": "department-support",
        "parent_scope_id": "org-contoso-global",
        "token_limit": 500_000,
        "warning_threshold_percent": 80,
        "updated_by": "owner@contoso.com",
        "updated_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
    _install(repository)
    try:
        before = client.get(DIRECTORY).json()
        support = next(
            item for item in before["departments"] if item["id"] == "department-support"
        )
        assert support["references"]["budgets"] == 1

        response = client.put(
            STATUS.format("department-support"), json={"status": "retired"}
        )
        assert response.status_code == 200
        departments = {item["id"]: item for item in response.json()["departments"]}
        assert departments["department-support"]["status"] == "retired"
        # Still stored, so the budget keeps naming something real; no longer offered.
        entities = client.get("/api/v1/enterprise/entities").json()
        assert "department-support" not in {item["id"] for item in entities["departments"]}
    finally:
        _uninstall()


def test_a_retired_department_can_be_restored() -> None:
    repository = InMemoryRepository()
    _install(repository)
    try:
        client.put(STATUS.format("department-support"), json={"status": "retired"})
        response = client.put(
            STATUS.format("department-support"), json={"status": "active"}
        )
        assert response.status_code == 200
        departments = {item["id"]: item for item in response.json()["departments"]}
        assert departments["department-support"]["status"] == "active"
        assert [item["operation"] for item in repository.org_unit_audit] == [
            "retired", "restored"
        ]
    finally:
        _uninstall()


def test_the_organization_cannot_be_retired() -> None:
    """There is nowhere for a department to hang without one, and no way back.

    Renaming is the operation an install actually wants here; retiring the organization
    would empty the top of the budget tree while every row under it still points at it.
    """
    repository = InMemoryRepository()
    _install(repository)
    try:
        response = client.put(
            STATUS.format("org-contoso-global"), json={"status": "retired"}
        )
        assert response.status_code == 422
        assert "rename" in response.json()["detail"]
    finally:
        _uninstall()


def test_a_duplicate_id_is_refused() -> None:
    repository = InMemoryRepository()
    _install(repository)
    try:
        response = client.post(
            DEPARTMENTS, json={"id": "department-platform", "display_name": "Another"}
        )
        assert response.status_code == 422
        assert "department-platform" in response.json()["detail"]
    finally:
        _uninstall()


def test_editing_the_organization_requires_owner_role() -> None:
    repository = InMemoryRepository()
    _install(repository)
    app.dependency_overrides[require_authenticated_session] = lambda: SessionIdentity(
        id="member-id",
        email="member@contoso.com",
        name="Member",
        role="member",
        method="entra",
        session_expires_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    try:
        assert client.get(DIRECTORY).status_code == 200
        assert client.post(
            DEPARTMENTS, json={"id": "department-x", "display_name": "X"}
        ).status_code == 403
        assert client.put(
            NAME.format("org-contoso-global"), json={"display_name": "X"}
        ).status_code == 403
        assert client.put(
            STATUS.format("department-support"), json={"status": "retired"}
        ).status_code == 403
    finally:
        _uninstall()


def test_an_id_is_suggested_where_the_name_allows_one_and_withheld_where_it_does_not() -> None:
    assert suggested_unit_id("Drug Discovery") == "department-drug-discovery"
    assert suggested_unit_id("R&D  Platform") == "department-r-d-platform"
    # The normal case at an install whose departments are named in Chinese: there is no
    # slug to derive, so the screen asks for the id rather than inventing a permanent one.
    assert suggested_unit_id("药物发现") is None
    assert suggested_unit_id("   ") is None
