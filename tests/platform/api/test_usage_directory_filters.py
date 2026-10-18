from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.api import app
from backend.http.dependencies import get_repository
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import (
    local_postgres as local_postgres,
)
from tests.platform.api.api_support import _usage_record
from tests.platform.api.test_directory_api import (
    directory_http as directory_http,
)
from turnstile_core.domain.directory import DirectoryPrincipal, UnitWrite
from turnstile_core.domain.enterprise import enterprise_catalog
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import UsageFilters
from turnstile_core.persistence.repository_support import cache_scope_for_filters
from turnstile_core.services.directory_catalog import directory_store
from turnstile_core.services.directory_service import DirectoryService
from turnstile_core.services.usage_directory import scoped_usage_catalog


def test_directory_facets_and_all_aggregates_share_multiselect_status_and_permissions(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    repository = app.dependency_overrides[get_repository]()
    settings = data["settings"]
    store = directory_store(settings.database_url)
    service = DirectoryService(store)
    with store.connection() as connection:
        owner = connection.execute("SELECT id,email FROM app_user WHERE role='owner'").fetchone()
    assert owner is not None
    principal = DirectoryPrincipal(account_id=owner["id"], email=owner["email"], role="owner")
    archived = service.create_unit(
        principal, data["organization"]["id"],
        UnitWrite(code="archive", name="Archived", kind="department"),
    )
    service.update_unit(
        principal, archived["id"], UnitWrite(
            expected_revision=archived["revision"], code="archive", name="Archived",
            kind="department", status="archived",
        ),
    )
    now = datetime.now(UTC)
    organization_id = data["organization"]["id"]
    records = []
    for label, org, department, user in (
        ("one", organization_id, data["first"]["id"], data["person"]["governance_user_id"]),
        ("two", organization_id, data["second"]["id"], data["outsider"]["governance_user_id"]),
        ("archive", data["organization"]["id"], archived["id"], "old@example.com"),
        ("probe", "system-gateway-publication", "system-gateway-publication", "publisher"),
        ("missing", data["organization"]["id"], "unattributed", "unattributed"),
    ):
        record = _usage_record(str(uuid4()), "Runtime", 10).model_copy(update={
            "ts": now, "request_id": label, "organization_id": org,
            "organization": "Old organization label", "department_id": department,
            "department": "Old department label", "user_id": user, "user": label,
        })
        repository.write_token_usage(record)
        records.append(record)
    window = {"from": (now - timedelta(hours=1)).isoformat(),
              "to": (now + timedelta(hours=1)).isoformat()}
    all_catalog = client.get("/api/v1/enterprise/query-entities", params=window).json()
    assert next(row for row in all_catalog["organizations"]
                if row["id"] == data["organization"]["id"])["name"] == "Example"
    probe = next(row for row in all_catalog["organizations"]
                 if row["id"] == "system-gateway-publication")
    assert probe["source"] == "historical" and probe["directory_status"] == "historical"
    assert all_catalog["invocation_testers"] == []
    for statuses, expected in (
        (["active"], {"one", "two"}),
        (["archived"], {"archive"}),
        (["historical"], {"probe"}),
        (["unattributed"], {"missing"}),
        (["active", "archived"], {"one", "two", "archive"}),
        (["active", "archived", "historical", "unattributed"],
         {"one", "two", "archive", "probe", "missing"}),
    ):
        params = [*window.items(), *(("directory_status", value) for value in statuses)]
        response = client.get("/api/v1/observability/requests", params=params)
        assert response.status_code == 200, response.text
        assert {row["request_id"] for row in response.json()["items"]} == expected
        totals = client.get(
            "/api/v1/observability/executive-overview", params=params,
        ).json()["totals"]
        assert totals["total_requests"] == len(expected)
        for interval in ("hour", "day", "week"):
            trend = client.get("/api/v1/observability/trends", params=[
                *params, ("group_by", "none"), ("interval", interval),
                ("timezone", "Asia/Shanghai"),
            ])
            assert trend.status_code == 200, trend.text
            assert sum(row["totals"]["calls"] for row in trend.json()["points"]) == len(expected)
        distribution = client.get("/api/v1/observability/distribution", params=[
            *params, ("dimension", "department"),
        ]).json()["items"]
        assert sum(row["total_requests"] for row in distribution) == len(expected)
        assert client.get("/api/v1/observability/anomalies", params=params).status_code == 200
    active_facets = client.get("/api/v1/enterprise/query-entities", params={
        **window, "directory_status": "active",
    }).json()
    assert {row["id"] for row in active_facets["organizations"]} == {data["organization"]["id"]}
    assert all(row["directory_status"] == "active" for row in active_facets["departments"])
    archived_facets = client.get("/api/v1/enterprise/query-entities", params={
        **window, "directory_status": "archived",
    }).json()
    assert any(row["id"] == archived["id"] for row in archived_facets["departments"])
    assert any(row["id"] == organization_id and row["directory_status"] == "active"
               for row in archived_facets["organizations"])
    selected = [
        *window.items(), ("directory_status", "active"),
        ("department_id", data["first"]["id"]), ("department_id", data["second"]["id"]),
    ]
    assert len(client.get("/api/v1/observability/requests", params=selected).json()["items"]) == 2
    selected.append(("user_id", data["person"]["governance_user_id"]))
    assert len(client.get("/api/v1/observability/requests", params=selected).json()["items"]) == 1
    client.cookies.set("turnstile_session", "directory-member")
    restricted = client.get("/api/v1/observability/requests", params=selected).json()["items"]
    assert {row["request_id"] for row in restricted} == {"one"}
    denied = client.get("/api/v1/observability/requests", params=[
        *window.items(), ("department_id", data["second"]["id"]),
        ("department_id", "system-gateway-publication"),
    ]).json()["items"]
    assert denied == []
    assert client.get("/api/v1/observability/requests", params={
        **window, "directory_status": "invalid",
    }).status_code == 422
    assert len(repository.list_usage_requests(
        now - timedelta(hours=1), now + timedelta(hours=1), UsageFilters(), 200,
    )) == len(records)


def test_historical_catalog_preserves_all_parent_edges_and_latest_name() -> None:
    repository = InMemoryRepository()
    for index, parent in enumerate(("department-old", "department-new")):
        repository.write_token_usage(_usage_record(str(index), "Runtime", 10).model_copy(update={
            "ts": datetime(2026, 7, 20, index, tzinfo=UTC), "department_id": parent,
            "user_id": "same@example.com", "user": f"Name {index}",
        }))
    catalog = repository.historical_entities(
        datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC), UsageFilters(),
    )
    person = catalog.users[0]
    assert person.name == "Name 1" and person.parent_id == "department-new"
    assert set(person.parent_ids) == {"department-old", "department-new"}
    assert cache_scope_for_filters(UsageFilters(department_id=("department-old",))) is None


def test_legacy_usage_candidates_are_scoped_even_without_persistent_directory() -> None:
    catalog = scoped_usage_catalog(enterprise_catalog(), ("department-platform",))
    assert {row.id for row in catalog.departments} == {"department-platform"}
    assert all(row.parent_id == "department-platform" for row in catalog.users)
    empty = scoped_usage_catalog(enterprise_catalog(), ())
    assert empty.organizations == empty.departments == empty.users == []
