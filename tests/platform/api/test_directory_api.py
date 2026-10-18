from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api import app
from backend.http.dependencies import get_repository
from backend.http.model_platform import _bind_invocation_identity
from backend.http.session import get_auth_store
from backend.services.auth_service import SessionIdentity, hash_password, hash_session_token
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import (
    local_postgres as local_postgres,
)
from tests.platform.api.api_support import _usage_record
from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.directory import (
    AccountLinkWrite,
    AdministratorsWrite,
    DirectoryPrincipal,
    OrganizationWrite,
    PersonCreate,
    UnitWrite,
)
from turnstile_core.domain.runtime_models import (
    ChatMessage,
    InvocationMetadata,
    ModelInvocationRequest,
)
from turnstile_core.persistence.auth_store import AuthStore
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy, UsageFilters
from turnstile_core.services import directory_catalog
from turnstile_core.services.directory_catalog import directory_store
from turnstile_core.services.directory_service import DirectoryService


@pytest.fixture
def directory_http(
    directory_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, dict[str, Any]]]:
    settings = Settings(
        database_url=directory_database,
        directory_source="database",
        organization_management_enabled=True,
    )
    monkeypatch.setattr(directory_catalog, "get_settings", lambda: settings)
    store = directory_store(directory_database)
    store.initialize_empty()
    accounts = AuthStore(directory_database)
    repository = PostgreSqlOpsDbProxy(directory_database)
    password = hash_password("long-test-directory-password")
    owner = accounts.create_password_user("owner@example.com", "Owner", password, "owner")
    member = accounts.create_password_user("member@example.com", "Member", password)
    now = datetime.now(UTC)
    for user, token in ((owner, "directory-owner"), (member, "directory-member")):
        assert accounts.create_session(
            user["id"],
            hash_session_token(token),
            "password",
            now,
            now + timedelta(hours=1),
            expected_password_hash=password,
        )
    service = DirectoryService(store)
    principal = DirectoryPrincipal(account_id=owner["id"], email=owner["email"], role="owner")
    organization = service.create_organization(
        principal,
        OrganizationWrite(code="example", name="Example"),
    )
    first = service.create_unit(
        principal,
        organization["id"],
        UnitWrite(code="first", name="First", kind="department"),
    )
    second = service.create_unit(
        principal,
        organization["id"],
        UnitWrite(code="second", name="Second", kind="department"),
    )
    person = service.create_person(
        principal,
        PersonCreate(
            governance_user_id=member["email"],
            display_name="Member",
            department_id=first["id"],
        ),
    )
    service.link_account(
        principal,
        UUID(person["id"]),
        AccountLinkWrite(
            expected_revision=person["revision"],
            app_user_id=member["id"],
        ),
    )
    service.set_administrators(
        principal,
        first["id"],
        AdministratorsWrite(
            expected_revision=first["revision"],
            account_ids=[member["id"]],
        ),
    )
    outsider = service.create_person(
        principal,
        PersonCreate(
            governance_user_id="other@example.com",
            display_name="Other",
            department_id=second["id"],
        ),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_auth_store] = lambda: accounts
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, "directory-owner")
    try:
        yield (
            client,
            {
                "organization": organization,
                "first": first,
                "second": second,
                "person": person,
                "outsider": outsider,
                "settings": settings,
            },
        )
    finally:
        client.close()
        app.dependency_overrides.clear()
        accounts.close()
        repository.close()
        store.close()
        directory_store.cache_clear()


def test_directory_api_authentication_and_department_scope(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    assert client.get("/api/v1/organization-management/capabilities").json()["can_read"]
    client.cookies.set("turnstile_session", "directory-member")
    page = client.get("/api/v1/organization-management/people").json()
    assert page["total"] == 1 and len(page["items"]) == 1
    outside = client.get(f"/api/v1/organization-management/people/{data['outsider']['id']}")
    assert outside.status_code == 404
    denied = client.patch(
        f"/api/v1/organization-management/people/{data['outsider']['id']}",
        json={"display_name": "Forbidden", "expected_revision": data["outsider"]["revision"]},
    )
    assert denied.status_code == 404
    assert (
        client.post(
            "/api/v1/organization-management/organizations",
            json={
                "code": "forbidden",
                "name": "Forbidden",
            },
        ).status_code
        == 403
    )
    catalog = client.get("/api/v1/enterprise/entities").json()
    assert {row["id"] for row in catalog["departments"]} == {data["first"]["id"]}
    assert client.get("/api/v1/organization-management/accounts").status_code == 403
    assert client.get("/api/v1/application-access/applications").status_code == 403
    assert client.get("/api/v1/copilot/status").status_code == 403
    assert (
        client.get("/api/v1/organization-management/accounts").headers["cache-control"]
        == "no-store"
    )
    client.cookies.clear()
    assert client.get("/api/v1/organization-management/people").status_code == 401


def test_menu_group_profile_and_password_login_do_not_change_data_authorization(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    person_url = f"/api/v1/organization-management/people/{data['person']['id']}"
    client.cookies.set("turnstile_session", "directory-member")
    before = client.get("/api/v1/auth/me").json()
    assert before["menu_permission_group"] == "department_admin"
    assert "organization-management" in before["menu_permissions"]
    client.cookies.set("turnstile_session", "directory-owner")
    person = client.get(person_url).json()
    response = client.patch(
        person_url,
        json={
            "display_name": person["display_name"],
            "expected_revision": person["revision"],
            "menu_permission_groups": ["organization_admin", "team_admin"],
        },
    )
    assert response.status_code == 200, response.text
    client.cookies.set("turnstile_session", "directory-member")
    after = client.get("/api/v1/auth/me").json()
    assert after["menu_permission_group"] == "department_admin"
    assert set(after["menu_permission_groups"]) == {
        "organization_admin", "team_admin", "department_admin"
    }
    assert "settings" not in after["menu_permissions"]
    assert after["role"] == before["role"] == "member"
    assert after["directory_scope_key"] == before["directory_scope_key"]
    assert after["directory_permission_revision"] > before["directory_permission_revision"]
    assert client.get("/api/v1/application-access/applications").status_code == 403
    assert client.get("/api/v1/copilot/status").status_code == 403
    assert client.get("/api/v1/organization-management/accounts").status_code == 403
    assert client.get("/api/v1/organization-management/people").json()["total"] == 1
    denied = client.patch(
        person_url,
        json={
            "display_name": person["display_name"],
            "expected_revision": response.json()["revision"],
            "menu_permission_group": "department_admin",
        },
    )
    assert denied.status_code == 403
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "member@example.com", "password": "long-test-directory-password"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["menu_permissions"] == after["menu_permissions"]
    assert login.json()["menu_permission_groups"] == after["menu_permission_groups"]


def test_revoked_department_admin_does_not_recover_legacy_global_read(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    client.cookies.set("turnstile_session", "directory-member")
    prior = client.get("/api/v1/auth/me").json()
    client.cookies.set("turnstile_session", "directory-owner")
    units = client.get(
        f"/api/v1/organization-management/organizations/{data['organization']['id']}/units",
    ).json()
    department = next(unit for unit in units if unit["id"] == data["first"]["id"])
    response = client.put(
        f"/api/v1/organization-management/departments/{department['id']}/administrators",
        json={"expected_revision": department["revision"], "account_ids": []},
    )
    assert response.status_code == 200, response.text
    client.cookies.set("turnstile_session", "directory-member")
    profile = client.get("/api/v1/auth/me").json()
    assert profile["directory_permission_revision"] > prior["directory_permission_revision"]
    assert profile["directory_scope_key"] != prior["directory_scope_key"]
    assert client.get("/api/v1/organization-management/capabilities").status_code == 403
    assert client.get("/api/v1/organization-management/people").status_code == 403
    own_catalog = client.get("/api/v1/enterprise/entities").json()
    assert [row["id"] for row in own_catalog["users"]] == [data["person"]["governance_user_id"]]
    assert client.get("/api/v1/application-access/applications").status_code == 403


def test_scoped_admin_endpoints_update_labels_and_do_not_alias_team_or_create_owner(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    root = "/api/v1/organization-management"
    person = client.get(f"{root}/people/{data['person']['id']}").json()
    account_id = person["app_user_id"]
    team = client.post(
        f"{root}/organizations/{data['organization']['id']}/units",
        json={
            "name": "Team", "code": "team", "kind": "team",
            "parent_unit_id": data["first"]["id"],
        },
    ).json()
    assert client.get(f"{root}/menu-administrators/team/{team['id']}").json() == []
    result = client.put(
        f"{root}/people/{person['id']}/teams",
        json={"team_ids": [team["id"]], "expected_revision": person["revision"]},
    )
    assert result.status_code == 200
    for kind, scope in (("organization", data["organization"]), ("team", team)):
        response = client.put(
            f"{root}/menu-administrators/{kind}/{scope['id']}",
            json={"account_ids": [account_id], "expected_revision": scope["revision"]},
        )
        assert response.status_code == 200, response.text
    person = client.get(f"{root}/people/{person['id']}").json()
    assert set(person["menu_permission_groups"]) == {
        "organization_admin", "department_admin", "team_admin",
    }
    scoped = client.get(f"{root}/people", params={"team_id": team["id"]}).json()["items"][0]
    assert scoped["scope_menu_permission_groups"] == ["team_admin"]
    assert scoped["scope_menu_administrator_scopes"][0]["scope_id"] == team["id"]
    client.cookies.set("turnstile_session", "directory-member")
    profile = client.get("/api/v1/auth/me").json()
    assert set(profile["menu_permission_groups"]) == set(person["menu_permission_groups"])
    assert profile["role"] == "member"
    assert {
        (item["scope_kind"], item["scope_id"]) for item in profile["menu_administrator_scopes"]
    } == {
        ("organization", data["organization"]["id"]),
        ("department", data["first"]["id"]), ("team", team["id"]),
    }
    assert client.get(f"{root}/accounts").status_code == 403
    assert client.get(
        f"{root}/menu-administrators/organization/{data['organization']['id']}"
    ).status_code == 403


def test_invocation_requires_account_binding_and_explicit_delegation(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    from fastapi import HTTPException

    client, data = directory_http
    profile = client.get("/api/v1/auth/me").json()
    service = DirectoryService(directory_store(data["settings"].database_url))
    principal = DirectoryPrincipal(
        account_id=UUID(profile["id"]),
        email=profile["email"],
        role="owner",
    )
    service.create_person(
        principal,
        PersonCreate(
            display_name="Same email, not bound",
            governance_user_id=profile["email"],
            department_id=data["first"]["id"],
        ),
    )
    identity = SessionIdentity(
        id=profile["id"],
        email=profile["email"],
        name="Owner",
        role="owner",
        method="password",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    metadata = {
        field: "unattributed"
        for field in (
            "organization_id",
            "organization",
            "department_id",
            "department",
            "project_id",
            "project",
            "agent_id",
            "agent",
            "user_id",
            "user",
            "workflow",
            "model_id",
            "model",
            "runtime",
            "request_source",
            "run_id",
        )
    }
    metadata["user_id"] = profile["email"]
    request = ModelInvocationRequest(
        runtime_id=uuid4(),
        model_id=uuid4(),
        messages=[ChatMessage(role="user", content="test")],
        metadata=InvocationMetadata.model_validate(metadata),
    )
    repository = app.dependency_overrides[get_repository]()
    with pytest.raises(HTTPException) as missing:
        _bind_invocation_identity(request, identity, repository, data["settings"])
    assert missing.value.status_code == 409
    delegated = request.model_copy(
        update={
            "metadata": request.metadata.model_copy(
                update={
                    "user_id": data["outsider"]["governance_user_id"],
                }
            )
        }
    )
    with pytest.raises(HTTPException) as denied:
        _bind_invocation_identity(delegated, identity, repository, data["settings"])
    assert denied.value.status_code == 403


def test_historical_query_catalog_retains_unknown_ids_and_original_scope(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, _ = directory_http
    repository = app.dependency_overrides[get_repository]()
    now = datetime.now(UTC)
    record = _usage_record(str(uuid4()), "test-runtime", 10).model_copy(
        update={
            "ts": now,
            "organization_id": "retired-organization",
            "organization": "Retired Organization",
            "department_id": "unknown-department",
            "department": "Original Department",
            "user_id": "user-legacy-opaque",
            "user": "Historical Person",
        }
    )
    repository.write_token_usage(record)
    window = {
        "from": (now - timedelta(hours=1)).isoformat(),
        "to": (now + timedelta(hours=1)).isoformat(),
    }
    response = client.get("/api/v1/enterprise/query-entities", params=window)
    assert response.status_code == 200, response.text
    people = response.json()["users"]
    historical = next(row for row in people if row["id"] == "user-legacy-opaque")
    assert historical["parent_id"] == "unknown-department"
    assert historical["name"] == "Historical Person"
    assert not client.get("/api/v1/enterprise/entities").json()["invocation_testers"]
    client.cookies.set("turnstile_session", "directory-member")
    restricted = client.get("/api/v1/enterprise/query-entities", params=window)
    assert restricted.status_code == 200
    assert all(row["id"] != record.user_id for row in restricted.json()["users"])
    assert all(row["id"] != record.department_id for row in restricted.json()["departments"])


def test_http_create_retries_and_validation_do_not_echo_private_input(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    body = {
        "display_name": "Created",
        "email": "created@example.com",
        "password": "new-directory-password",
        "organization_id": data["organization"]["id"],
        "department_id": data["first"]["id"],
    }
    first = client.post(
        "/api/v1/organization-management/people",
        json=body,
        headers={"Idempotency-Key": "create-person"},
    )
    retry = client.post(
        "/api/v1/organization-management/people",
        json=body,
        headers={"Idempotency-Key": "create-person"},
    )
    assert first.status_code == retry.status_code == 201
    assert first.json() == retry.json()
    marker = "private-input-must-not-appear"
    bad = client.post(
        "/api/v1/organization-management/people",
        json={
            **body,
            "email": marker,
        },
    )
    assert bad.status_code == 422 and marker not in bad.text
    assert bad.headers["cache-control"] == "no-store"
    oversized = client.post(
        "/api/v1/organization-management/organizations",
        content=(chunk for chunk in (b'{"name":"', b"x" * (256 * 1024), b'"}')),
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    assert oversized.headers["cache-control"] == "no-store"


def test_new_member_requires_primary_department_and_owner_and_retired_routes_are_absent(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    root = "/api/v1/organization-management"
    body = {
        "display_name": "New member", "email": "new-login@example.com",
        "password": "new-member-test-password", "organization_id": data["organization"]["id"],
    }
    assert client.post(f"{root}/people", json=body).status_code == 422
    body["department_id"] = data["first"]["id"]
    client.cookies.set("turnstile_session", "directory-member")
    assert client.post(f"{root}/people", json=body).status_code == 403
    client.cookies.set("turnstile_session", "directory-owner")
    response = client.post(f"{root}/people", json=body)
    assert response.status_code == 201, response.text
    assert "password" not in response.text
    assert response.json()["department_id"] == data["first"]["id"]
    for path in ("transfers", "identity-conflicts"):
        assert client.get(f"{root}/{path}").status_code == 404
    assert client.post(
        f"{root}/people/{data['person']['id']}/transfer-preview", json={}
    ).status_code in (404, 405)
    login = client.post(
        "/api/v1/auth/login",
        json={"email": body["email"], "password": body["password"]},
    )
    assert login.status_code == 200
    assert login.json()["role"] == "member"
    assert login.json()["menu_permission_groups"] == ["user"]


def test_budget_scope_does_not_disclose_other_department_allocations(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    period = datetime.now(UTC).strftime("%Y-%m")
    for kind, scope_id, limit in (
        ("organization", data["organization"]["id"], 100_000),
        ("department", data["first"]["id"], 10_000),
        ("department", data["second"]["id"], 90_000),
    ):
        response = client.put(
            f"/api/v1/budgets/{kind}/{scope_id}",
            params={"period": period},
            json={"token_limit": limit},
        )
        assert response.status_code == 200, response.text
    client.cookies.set("turnstile_session", "directory-member")
    response = client.get("/api/v1/budgets", params={"period": period})
    assert response.status_code == 200
    payload = response.json()
    assert payload["coverage"] == "department"
    organizations = [row for row in payload["items"] if row["scope_type"] == "organization"]
    assert organizations[0]["token_limit"] == 10_000
    assert all(row["scope_id"] != data["second"]["id"] for row in payload["items"])
    assert (
        client.get(
            "/api/v1/budgets/users",
            params={
                "period": period,
                "department_id": data["second"]["id"],
            },
        ).status_code
        == 404
    )


def test_renamed_department_has_one_ranking_and_trend_identity(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    repository = app.dependency_overrides[get_repository]()
    now = datetime.now(UTC).replace(hour=12, minute=30, second=0, microsecond=0)
    for index, name in enumerate(("Before Rename", "After Rename")):
        record = _usage_record(str(uuid4()), "test-runtime", 10).model_copy(
            update={
                "ts": now + timedelta(minutes=index),
                "department_id": data["first"]["id"],
                "department": name,
                "organization_id": data["organization"]["id"],
                "organization": "Example",
                "user_id": "member@example.com",
                "user": name,
            }
        )
        repository.write_token_usage(record)
    window = {
        "from": (now - timedelta(hours=1)).isoformat(),
        "to": (now + timedelta(hours=1)).isoformat(),
    }
    response = client.get(
        "/api/v1/observability/distribution",
        params={
            **window,
            "dimension": "department",
            "split_by": "user",
        },
    )
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert len(rows) == 1 and rows[0]["total_tokens"] == 20
    assert rows[0]["name"] == "After Rename"
    assert len(rows[0]["breakdown"]) == 1
    trend = repository.trends(
        now - timedelta(hours=1),
        now + timedelta(hours=1),
        "hour",
        "department",
        "UTC",
        UsageFilters(),
    )
    assert len(trend["points"]) == 1
    assert trend["points"][0]["label"] == "After Rename"
    assert trend["points"][0]["totals"]["total_tokens"] == 20


@pytest.mark.parametrize("interval", ["hour", "day", "week"])
@pytest.mark.parametrize("timezone", ["UTC", "Asia/Shanghai"])
def test_ungrouped_trends_support_overview_governance_and_scoped_queries(
    directory_http: tuple[TestClient, dict[str, Any]],
    interval: str,
    timezone: str,
) -> None:
    client, data = directory_http
    repository = app.dependency_overrides[get_repository]()
    now = datetime(2026, 10, 10, 12, 30, tzinfo=UTC)
    for index, (department, tokens, domain) in enumerate(
        (
            (data["first"], 10, "apim"),
            (data["second"], 20, "apim"),
            (data["first"], 1000, "github_copilot"),
        )
    ):
        repository.write_token_usage(
            _usage_record(str(uuid4()), "test-runtime", tokens).model_copy(
                update={
                    "ts": now + timedelta(minutes=index),
                    "organization_id": data["organization"]["id"],
                    "organization": "Example",
                    "department_id": department["id"],
                    "department": department["name"],
                    "usage_domain": domain,
                }
            )
        )
    params = {
        "from": (now - timedelta(hours=1)).isoformat(),
        "to": (now + timedelta(hours=1)).isoformat(),
        "group_by": "none",
        "interval": interval,
        "timezone": timezone,
    }
    response = client.get("/api/v1/observability/trends", params=params)
    assert response.status_code == 200, response.text
    points = response.json()["points"]
    assert len(points) == 1
    assert points[0]["key"] == points[0]["label"] == "all"
    assert points[0]["totals"]["total_tokens"] == 30
    assert points[0]["totals"]["calls"] == 2
    client.cookies.set("turnstile_session", "directory-member")
    scoped = client.get("/api/v1/observability/trends", params=params)
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["points"][0]["totals"]["total_tokens"] == 10
    outside = client.get(
        "/api/v1/observability/trends",
        params={**params, "department_id": data["second"]["id"]},
    )
    assert outside.status_code == 200, outside.text
    assert outside.json()["points"] == []
