"""The organization catalog.

Until one is written the seeded demonstration catalog is used, unchanged. Writing one makes
its organizations and departments the budget scopes, the people-discovery targets and the
place Owners are listed, and returns to the seeded catalog when it is deleted.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.api import app
from backend.http.dependencies import get_repository
from backend.http.session import SessionIdentity, require_authenticated_session
from turnstile_core.domain.enterprise import (
    DEFAULT_APPLICATION_USER_DEPARTMENT_ID,
    catalog_response,
    catalog_rows,
    catalog_write_problems,
    configured_catalog,
    merge_application_owners,
    merge_observed_users,
    resolve_enterprise_catalog,
)
from turnstile_core.domain.models import EnterpriseCatalogWrite
from turnstile_core.persistence.in_memory import InMemoryRepository

client = TestClient(app)
ORIGIN = {"Origin": "http://localhost:5173"}
PERIOD = datetime.now(UTC).strftime("%Y-%m")

CATALOG = {
    "organizations": [
        {
            "id": "platform",
            "name": "Platform",
            "external_ref": "group:1111",
            "attributes": {"tokens": 20000000},
        },
        {"id": "finance", "name": "Finance", "external_ref": "group:2222"},
    ],
    "departments": [
        {"id": "platform-web", "name": "Platform Web", "parent_id": "platform"},
        {"id": "platform", "name": "Platform (direct)", "parent_id": "platform"},
        {"id": "finance", "name": "Finance (direct)", "parent_id": "finance"},
    ],
    "default_department_id": "platform",
}


def _write(document: dict) -> EnterpriseCatalogWrite:
    return EnterpriseCatalogWrite.model_validate(document)


# --- the domain ------------------------------------------------------------------------


def test_no_rows_keeps_the_seeded_catalog() -> None:
    assert configured_catalog([]) is None
    seeded = resolve_enterprise_catalog([])
    assert DEFAULT_APPLICATION_USER_DEPARTMENT_ID in {item.id for item in seeded.departments}
    assert catalog_response([]).source == "seeded"


def test_a_configured_catalog_replaces_the_seeded_one_in_document_order() -> None:
    catalog = resolve_enterprise_catalog(catalog_rows(_write(CATALOG)))
    assert [item.id for item in catalog.organizations] == ["platform", "finance"]
    assert [item.id for item in catalog.departments] == ["platform-web", "platform", "finance"]
    assert catalog.projects == catalog.agents == catalog.users == []
    assert catalog.default_department_id == "platform"


def test_without_a_stated_default_the_first_department_is_the_default() -> None:
    document = {**CATALOG, "default_department_id": None}
    assert (
        resolve_enterprise_catalog(catalog_rows(_write(document))).default_department_id
        == "platform-web"
    )


def test_structural_errors_are_named() -> None:
    bad = {
        "organizations": [{"id": "a", "name": "A"}, {"id": "a", "name": "A again"}],
        "departments": [{"id": "x", "name": "X", "parent_id": "nowhere"}],
        "default_department_id": "missing",
    }
    problems = catalog_write_problems(_write(bad))
    assert any("Duplicate organization id: a" in p for p in problems)
    assert any("names parent nowhere" in p for p in problems)
    assert any("Default department missing" in p for p in problems)


def test_ids_must_be_url_and_scope_safe() -> None:
    with pytest.raises(ValueError):
        _write({"organizations": [{"id": "has space", "name": "Bad"}]})
    with pytest.raises(ValueError):
        _write({"organizations": []})


def test_owners_are_listed_under_the_configured_default_department() -> None:
    owners = [{"email": "Admin@Contoso.com", "display_name": "Admin", "role": "owner"}]
    configured = resolve_enterprise_catalog(catalog_rows(_write(CATALOG)))
    assert [(u.id, u.parent_id) for u in merge_application_owners(configured, owners).users] == [
        ("admin@contoso.com", "platform")
    ]
    seeded = resolve_enterprise_catalog([])
    listed = merge_application_owners(seeded, owners).users
    assert [u.parent_id for u in listed if u.id == "admin@contoso.com"] == [
        DEFAULT_APPLICATION_USER_DEPARTMENT_ID
    ]


def test_people_are_discovered_into_configured_departments_only() -> None:
    configured = resolve_enterprise_catalog(catalog_rows(_write(CATALOG)))
    observed = [
        {
            "user_id": "dev@contoso.com",
            "user_ref": "dev@contoso.com",
            "department_id": "platform-web",
        },
        {
            "user_id": "old@contoso.com",
            "user_ref": "old@contoso.com",
            "department_id": "department-finance",
        },
    ]
    merged = merge_observed_users(configured, observed)
    assert [(u.id, u.parent_id) for u in merged.users] == [("dev@contoso.com", "platform-web")]


# --- the API ---------------------------------------------------------------------------


def _identity(role: str) -> SessionIdentity:
    return SessionIdentity(
        id="00000000-0000-4000-8000-000000000009",
        email=f"{role}@contoso.com",
        name=role,
        role=role,  # type: ignore[arg-type]
        method="entra",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def repository() -> Iterator[InMemoryRepository]:
    repo = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repo
    yield repo
    app.dependency_overrides.pop(get_repository, None)
    app.dependency_overrides.pop(require_authenticated_session, None)


def _as(role: str) -> None:
    app.dependency_overrides[require_authenticated_session] = lambda: _identity(role)


def test_anyone_signed_in_can_read_the_catalog(repository: InMemoryRepository) -> None:
    _as("member")
    response = client.get("/api/v1/enterprise-catalog")
    assert response.status_code == 200
    assert response.json()["source"] == "seeded"


def test_only_an_owner_can_write_it(repository: InMemoryRepository) -> None:
    _as("member")
    assert client.put("/api/v1/enterprise-catalog", headers=ORIGIN, json=CATALOG).status_code == 403
    assert repository.enterprise_entities() == []


def test_an_owner_writes_it_and_it_reads_back_as_written(repository: InMemoryRepository) -> None:
    _as("owner")
    response = client.put("/api/v1/enterprise-catalog", headers=ORIGIN, json=CATALOG)
    assert response.status_code == 200
    body = client.get("/api/v1/enterprise-catalog").json()
    assert body["source"] == "configured"
    assert body["updated_by"] == "owner@contoso.com"
    assert body["organizations"][0] == {
        "id": "platform",
        "name": "Platform",
        "parent_id": None,
        "external_ref": "group:1111",
        "attributes": {"tokens": 20000000},
    }
    assert body["default_department_id"] == "platform"


def test_a_structurally_wrong_catalog_is_refused_and_changes_nothing(
    repository: InMemoryRepository,
) -> None:
    _as("owner")
    bad = {**CATALOG, "departments": [{"id": "x", "name": "X", "parent_id": "nowhere"}]}
    response = client.put("/api/v1/enterprise-catalog", headers=ORIGIN, json=bad)
    assert response.status_code == 422
    assert repository.enterprise_entities() == []


def test_deleting_it_returns_to_the_seeded_catalog(repository: InMemoryRepository) -> None:
    _as("owner")
    client.put("/api/v1/enterprise-catalog", headers=ORIGIN, json=CATALOG)
    assert client.delete("/api/v1/enterprise-catalog", headers=ORIGIN).json()["source"] == "seeded"


def test_a_configured_department_can_be_given_a_budget(repository: InMemoryRepository) -> None:
    _as("owner")
    budget = {"token_limit": 5_000_000, "warning_threshold_percent": 80}
    url = f"/api/v1/budgets/department/platform-web?period={PERIOD}"
    assert client.put(url, headers=ORIGIN, json=budget).status_code == 404

    client.put("/api/v1/enterprise-catalog", headers=ORIGIN, json=CATALOG)
    organization = client.put(
        f"/api/v1/budgets/organization/platform?period={PERIOD}",
        headers=ORIGIN,
        json={"token_limit": 20_000_000, "warning_threshold_percent": 80},
    )
    assert organization.status_code == 200
    department = client.put(url, headers=ORIGIN, json=budget)
    assert department.status_code == 200
    scopes = {(item["scope_type"], item["scope_id"]) for item in department.json()["items"]}
    assert ("department", "platform-web") in scopes
    assert ("organization", "platform") in scopes
