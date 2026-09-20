from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.api import (
    app,
)
from backend.http.dependencies import get_repository
from backend.services import budget_service
from tests.platform.api.api_support import (
    MEMBER_SESSION,
    OWNER_SESSION,
    client,
)
from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.enterprise import enterprise_catalog, merge_observed_users
from turnstile_core.domain.models import (
    EnterpriseEntity,
    EnterpriseEntityCatalog,
    TokenUsageRecord,
)
from turnstile_core.persistence.in_memory import InMemoryRepository

pytest_plugins = ("tests.platform.api.api_fixtures",)


def test_demo_runs_include_normal_and_abnormal_waterfalls() -> None:
    response = client.get(
        "/api/v1/observability/runs",
        params={"from": "2026-07-01T00:00:00Z", "to": "2026-07-18T00:00:00Z"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert [item["turn_count"] for item in payload["items"]] == [4, 18]

def test_overview_matches_contract() -> None:
    response = client.get("/api/v1/observability/overview", params={"timezone": "Asia/Shanghai"})
    assert response.status_code == 200
    assert response.json()["timezone"] == "Asia/Shanghai"

def test_enterprise_entities_meet_acceptance_scale() -> None:
    payload = client.get("/api/v1/enterprise/entities").json()
    assert len(payload["departments"]) == 5
    assert len(payload["projects"]) == 10
    assert len(payload["agents"]) == 11
    assert len(payload["users"]) == 20
    assert {
        "id": "agent-chatgpt-desktop-codex",
        "name": "ChatGPT Desktop Codex",
        "parent_id": "project-finops",
    } in payload["agents"]
    assert all(user["id"] == user["name"] and "@" in user["id"] for user in payload["users"])


def test_enterprise_entities_expose_configured_directory_testers_only() -> None:
    previous_settings = app.dependency_overrides.get(get_settings)
    app.dependency_overrides[get_settings] = lambda: Settings(
        delegated_invocation_tester_ids=[
            "TEST.USER01@CONTOSO.COM",
            "missing.user@contoso.com",
        ]
    )
    try:
        response = client.get("/api/v1/enterprise/entities")
    finally:
        if previous_settings is None:
            app.dependency_overrides.pop(get_settings, None)
        else:
            app.dependency_overrides[get_settings] = previous_settings

    assert response.status_code == 200
    assert response.json()["invocation_testers"] == [
        {
            "id": "test.user01@contoso.com",
            "name": "test.user01@contoso.com",
            "parent_id": "department-platform",
        }
    ]


def test_delegated_invocation_tester_config_is_normalized_and_unique() -> None:
    settings = Settings(
        delegated_invocation_tester_ids=[" Test.User01@Contoso.com "]
    )

    assert settings.delegated_invocation_tester_ids == ["test.user01@contoso.com"]
    with pytest.raises(ValueError, match="must be unique"):
        Settings(
            delegated_invocation_tester_ids=[
                "test.user01@contoso.com",
                "TEST.USER01@CONTOSO.COM",
            ]
        )
    with pytest.raises(ValueError, match="must be email addresses"):
        Settings(delegated_invocation_tester_ids=["not-an-email"])

def test_machine_identities_never_become_people() -> None:
    # The runtime health probe used to call as an employee, which tied a runtime's verdict
    # to that employee's budget and model policy. It now uses a machine id, and the whole
    # reason that is safe is that a machine id can never be listed, budgeted or restricted.
    catalog = merge_observed_users(
        enterprise_catalog(),
        [
            {"user_id": "system-runtime-health-check", "department_id": "department-platform"},
            {"user_id": "real.person@contoso.com", "department_id": "department-platform"},
        ],
    )
    ids = {user.id for user in catalog.users}
    assert "system-runtime-health-check" not in ids
    assert "real.person@contoso.com" in ids

def test_application_owner_is_budgetable_before_gateway_traffic() -> None:
    repository = InMemoryRepository()
    repository.application_owner_rows.extend(
        [
            {
                "email": "owner@contoso.com",
                "display_name": "Initial Owner",
                "role": "owner",
            },
            {
                "email": "member@contoso.com",
                "display_name": "Unattributed Member",
                "role": "member",
            },
        ]
    )
    app.dependency_overrides[get_repository] = lambda: repository
    period = datetime.now(UTC).strftime("%Y-%m")
    headers = {"X-Hive-Role": "owner"}
    try:
        entities = client.get("/api/v1/enterprise/entities", headers=headers).json()
        owner = next(
            user for user in entities["users"] if user["id"] == "owner@contoso.com"
        )
        assert owner == {
            "id": "owner@contoso.com",
            "name": "Initial Owner",
            "parent_id": "department-platform",
        }
        assert all(user["id"] != "member@contoso.com" for user in entities["users"])

        model_id = str(
            next(model["id"] for model in repository.models if model["enabled"])
        )
        assigned = client.post(
            "/api/v1/budgets/users/bulk",
            params={"period": period},
            headers=headers,
            json={
                "department_id": "department-platform",
                "selection": "ids",
                "user_ids": ["owner@contoso.com"],
                "status": "all",
                "allocation_mode": "preserve",
                "model_ids": [model_id],
            },
        )
        assert assigned.status_code == 200
        assert [
            str(value)
            for value in repository.user_model_policies["owner@contoso.com"]["model_ids"]
        ] == [model_id]
    finally:
        app.dependency_overrides.clear()

def test_people_who_used_the_gateway_become_budgetable() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    headers = {"X-Hive-Role": "owner"}
    # The period has to come off the same clock as the usage this test writes. `date.today()`
    # is local, so between local midnight and UTC midnight it names next month while the
    # records land in this one, and the assertions below read an empty period.
    period = datetime.now(UTC).strftime("%Y-%m")
    employee = "admin@contoso.onmicrosoft.com"
    try:
        # Before any traffic the person does not exist, so a budget cannot be assigned.
        rejected = client.put(
            f"/api/v1/budgets/user/{employee}",
            params={"period": period},
            headers=headers,
            json={"token_limit": 1_000, "warning_threshold_percent": 80},
        )
        assert rejected.status_code == 404

        for user_id, department_id in (
            (employee, "department-platform"),
            # An unattributed department has nowhere to hang in organization -> department
            # -> user, so this person must not appear as an allocatable scope.
            ("stranger@contoso.onmicrosoft.com", "unattributed"),
            # A pre-email historical id. The same person is already in the seeded list, so
            # admitting this would show them twice under two different identifiers.
            ("user-01", "department-platform"),
        ):
            repository.write_token_usage(
                TokenUsageRecord(
                    id=f"seed-{user_id}",
                    request_id=f"seed-{user_id}",
                    correlation_id=f"seed-{user_id}",
                    ts=datetime.now(UTC),
                    team="AI Platform",
                    organization="Contoso Global",
                    organization_id="org-contoso-global",
                    department="AI Platform",
                    department_id=department_id,
                    project="unattributed",
                    project_id="unattributed",
                    user=user_id,
                    user_id=user_id,
                    agent="Claude Desktop",
                    agent_id="agent-claude-desktop",
                    workflow="unattributed",
                    run_id=f"seed-{user_id}",
                    turn_index=1,
                    provider="anthropic",
                    model="databricks-claude-sonnet-5",
                    model_id="databricks-claude-sonnet-5",
                    runtime="Azure Databricks Claude via APIM",
                    request_source="employee-desktop",
                    input_tokens=100,
                    cached_tokens=0,
                    output_tokens=50,
                    et=150,
                    et_coeff_m=1,
                    latency_ms=900,
                    status="success",
                    status_code=200,
                    estimated_cost=0.001,
                    estimated=False,
                    ingest_source="eventhub",
                )
            )

        catalog = client.get("/api/v1/enterprise/entities").json()
        users = {user["id"]: user for user in catalog["users"]}
        assert len(catalog["users"]) == 21
        assert users[employee]["parent_id"] == "department-platform"
        assert "stranger@contoso.onmicrosoft.com" not in users
        assert "user-01" not in users

        # A person still hangs under the normal hierarchy, so the parents must be funded
        # first. Being discovered makes someone allocatable, not exempt.
        for scope_type, scope_id, limit in (
            ("organization", "org-contoso-global", 100_000),
            ("department", "department-platform", 50_000),
        ):
            parent = client.put(
                f"/api/v1/budgets/{scope_type}/{scope_id}",
                params={"period": period},
                headers=headers,
                json={"token_limit": limit, "warning_threshold_percent": 80},
            )
            assert parent.status_code == 200

        accepted = client.put(
            f"/api/v1/budgets/user/{employee}",
            params={"period": period},
            headers=headers,
            json={"token_limit": 1_000, "warning_threshold_percent": 80},
        )
        assert accepted.status_code == 200
        # Assert through the people workspace, because that is the table an administrator
        # actually looks at when asking "why is this person not listed".
        people = client.get(
            "/api/v1/budgets/users",
            params={
                "period": period,
                "department_id": "department-platform",
                "query": "admin",
                "status": "all",
                "offset": 0,
                "limit": 50,
            },
            headers=headers,
        ).json()
        listed = {item["scope_id"]: item for item in people["items"]}
        assert employee in listed
        assert listed[employee]["token_limit"] == 1_000
        # Real usage is attributed immediately; the budget is not a fresh counter.
        assert listed[employee]["used_tokens"] == 150
        assert listed[employee]["parent_scope_id"] == "department-platform"
    finally:
        app.dependency_overrides.clear()

def test_anomaly_rules_support_owner_crud_and_validation() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    headers = {"X-Hive-Role": "owner"}
    try:
        listed = client.get("/api/v1/anomaly-rules", headers=headers)
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 4

        created = client.post(
            "/api/v1/anomaly-rules",
            headers=headers,
            json={
                "name": "Finance latency",
                "description": "Finance requests above 1.5 seconds",
                "metric": "request_latency_ms",
                "threshold_mode": "absolute",
                "threshold_value": 1500,
                "minimum_sample_size": 1,
                "severity": "critical",
                "scope_type": "department",
                "scope_id": "department-finance",
                "enabled": True,
            },
        )
        assert created.status_code == 201
        rule = created.json()
        assert rule["name"] == "Finance latency"
        assert rule["scope_id"] == "department-finance"

        updated = client.put(
            f"/api/v1/anomaly-rules/{rule['id']}",
            headers=headers,
            json={**{key: rule[key] for key in (
                "name",
                "description",
                "metric",
                "threshold_mode",
                "threshold_value",
                "minimum_sample_size",
                "severity",
                "scope_type",
                "scope_id",
            )}, "enabled": False},
        )
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False

        invalid = client.post(
            "/api/v1/anomaly-rules",
            headers=headers,
            json={
                "name": "Invalid percentile latency",
                "metric": "request_latency_ms",
                "threshold_mode": "percentile",
                "threshold_value": 99,
                "minimum_sample_size": 1,
                "severity": "warning",
                "scope_type": "global",
                "enabled": True,
            },
        )
        assert invalid.status_code == 409
        assert "absolute threshold" in invalid.json()["detail"]

        client.cookies.set("turnstile_session", MEMBER_SESSION)
        member_listed = client.get("/api/v1/anomaly-rules")
        assert member_listed.status_code == 200
        assert len(member_listed.json()["items"]) == 5
        client.cookies.set("turnstile_session", OWNER_SESSION)

        removed = client.delete(
            f"/api/v1/anomaly-rules/{rule['id']}", headers=headers
        )
        assert removed.status_code == 204
        assert all(
            item["id"] != rule["id"]
            for item in client.get("/api/v1/anomaly-rules", headers=headers).json()["items"]
        )
    finally:
        app.dependency_overrides.clear()

def test_anomaly_rule_configuration_controls_detected_signals() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    headers = {"X-Hive-Role": "owner"}
    try:
        repository.write_token_usage(
            TokenUsageRecord(
                id="usage-rule-1",
                request_id="request-rule-1",
                correlation_id="correlation-rule-1",
                ts=datetime(2026, 7, 20, tzinfo=UTC),
                team="Finance",
                organization="Contoso Global",
                organization_id="org-contoso-global",
                department="Finance",
                department_id="department-finance",
                project="Finance Copilot",
                project_id="project-finance-copilot",
                user="test.user03@contoso.com",
                user_id="test.user03@contoso.com",
                agent="Finance Analyst",
                agent_id="agent-finance",
                workflow="rule-validation",
                run_id="run-rule-1",
                turn_index=1,
                provider="microsoft_foundry",
                model="gpt-5.6-luna",
                model_id="gpt-5.6-luna",
                runtime="Microsoft Foundry via APIM",
                request_source="rule-test",
                input_tokens=20,
                cached_tokens=0,
                output_tokens=10,
                et=30,
                et_coeff_m=1,
                latency_ms=750,
                status="success",
                status_code=200,
                estimated_cost=0,
                estimated=False,
                ingest_source="gateway",
            )
        )
        write = {
            "name": "Finance latency over 500ms",
            "description": "",
            "metric": "request_latency_ms",
            "threshold_mode": "absolute",
            "threshold_value": 500,
            "minimum_sample_size": 1,
            "severity": "critical",
            "scope_type": "department",
            "scope_id": "department-finance",
            "enabled": True,
        }
        created = client.post(
            "/api/v1/anomaly-rules", headers=headers, json=write
        ).json()
        anomaly_params = {
            "from": "2026-07-01T00:00:00Z",
            "to": "2026-08-01T00:00:00Z",
            "department_id": "department-finance",
        }
        active = client.get(
            "/api/v1/observability/anomalies", params=anomaly_params
        ).json()["items"]
        assert any(item["rule_id"] == created["id"] for item in active)

        updated = client.put(
            f"/api/v1/anomaly-rules/{created['id']}",
            headers=headers,
            json={**write, "enabled": False},
        )
        assert updated.status_code == 200
        inactive = client.get(
            "/api/v1/observability/anomalies", params=anomaly_params
        ).json()["items"]
        assert all(item["rule_id"] != created["id"] for item in inactive)
    finally:
        app.dependency_overrides.clear()

def test_department_enforcement_defaults_to_audit_and_records_every_change() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    headers = {"X-Hive-Role": "owner"}
    # Audit rows are stamped in UTC by the service, so the period has to be UTC too.
    period = datetime.now(UTC).strftime("%Y-%m")
    try:
        initial = client.get(
            "/api/v1/budgets", params={"period": period}, headers=headers
        ).json()
        assert len(initial["enforcement"]) == 5
        # Nothing may enforce until an administrator turns it on deliberately.
        assert {row["mode"] for row in initial["enforcement"]} == {"audit"}
        assert [event for event in initial["history"] if event["event_type"] == "enforcement"] == []

        enabled = client.put(
            "/api/v1/budgets/enforcement/department-platform",
            params={"period": period},
            headers=headers,
            json={"mode": "block"},
        )
        assert enabled.status_code == 200
        modes = {row["department_id"]: row["mode"] for row in enabled.json()["enforcement"]}
        assert modes["department-platform"] == "block"
        assert modes["department-commerce"] == "audit"

        history = [
            event for event in enabled.json()["history"] if event["event_type"] == "enforcement"
        ]
        assert len(history) == 1
        assert history[0]["previous_mode"] == "audit"
        assert history[0]["new_mode"] == "block"
        assert history[0]["department_name"] == "AI Platform"
        assert history[0]["changed_by"] == "owner@contoso.com"

        # Re-saving the same mode must not manufacture an audit entry.
        repeated = client.put(
            "/api/v1/budgets/enforcement/department-platform",
            params={"period": period},
            headers=headers,
            json={"mode": "block"},
        )
        assert repeated.status_code == 200
        assert len(
            [event for event in repeated.json()["history"] if event["event_type"] == "enforcement"]
        ) == 1

        unknown = client.put(
            "/api/v1/budgets/enforcement/department-nope",
            params={"period": period},
            headers=headers,
            json={"mode": "block"},
        )
        assert unknown.status_code == 404

        client.cookies.set("turnstile_session", MEMBER_SESSION)
        member = client.put(
            "/api/v1/budgets/enforcement/department-platform",
            params={"period": period},
            json={"mode": "audit"},
        )
        assert member.status_code == 403
    finally:
        app.dependency_overrides.clear()

def test_monthly_token_budgets_track_usage_and_enforce_parent_limit() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    headers = {"X-Hive-Role": "owner"}
    try:
        repository.write_token_usage(
            TokenUsageRecord(
                id="usage-budget-1",
                request_id="request-budget-1",
                correlation_id="correlation-budget-1",
                ts=datetime(2026, 7, 20, tzinfo=UTC),
                team="AI Platform",
                organization="Contoso Global",
                organization_id="org-contoso-global",
                department="AI Platform",
                department_id="department-platform",
                project="Model FinOps",
                project_id="project-finops",
                user="test.user01@contoso.com",
                user_id="test.user01@contoso.com",
                agent="Delivery Engineer",
                agent_id="agent-delivery",
                workflow="budget-validation",
                run_id="run-budget-1",
                turn_index=1,
                provider="microsoft_foundry",
                model="gpt-5.6-terra",
                model_id="gpt-5.6-terra",
                runtime="Microsoft Foundry via APIM",
                request_source="budget-test",
                input_tokens=300,
                cached_tokens=50,
                output_tokens=150,
                et=500,
                et_coeff_m=1,
                latency_ms=700,
                status="success",
                status_code=200,
                estimated_cost=0.01,
                estimated=False,
                ingest_source="gateway",
            )
        )

        for scope_type, scope_id, token_limit, warning_threshold in (
            ("organization", "org-contoso-global", 10_000, 80),
            ("department", "department-platform", 6_000, 80),
            ("user", "test.user01@contoso.com", 2_000, 20),
        ):
            response = client.put(
                f"/api/v1/budgets/{scope_type}/{scope_id}",
                params={"period": "2026-07"},
                headers=headers,
                json={
                    "token_limit": token_limit,
                    "warning_threshold_percent": warning_threshold,
                },
            )
            assert response.status_code == 200

        compact_payload = client.get(
            "/api/v1/budgets",
            params={"period": "2026-07"},
            headers=headers,
        ).json()
        assert all(item["scope_type"] != "user" for item in compact_payload["items"])
        assert compact_payload["risk_count"] == 1
        assert len(compact_payload["risk_items"]) == 1
        compact_risk = compact_payload["risk_items"][0]
        assert compact_risk["scope_type"] == "user"
        assert compact_risk["scope_id"] == "test.user01@contoso.com"
        assert compact_risk["status"] == "warning"

        payload = client.get(
            "/api/v1/budgets",
            params={"period": "2026-07", "include_users": True},
            headers=headers,
        ).json()
        by_key = {(item["scope_type"], item["scope_id"]): item for item in payload["items"]}
        assert by_key[("organization", "org-contoso-global")]["used_tokens"] == 500
        assert by_key[("department", "department-platform")]["used_tokens"] == 500
        user_budget = by_key[("user", "test.user01@contoso.com")]
        assert user_budget["used_tokens"] == 500
        assert user_budget["remaining_tokens"] == 1_500
        assert user_budget["usage_percent"] == 25.0
        assert user_budget["status"] == "warning"
        assert [event["action"] for event in payload["history"]] == [
            "assigned",
            "assigned",
            "assigned",
        ]

        overallocated = client.put(
            "/api/v1/budgets/department/department-commerce",
            params={"period": "2026-07"},
            headers=headers,
            json={"token_limit": 5_000, "warning_threshold_percent": 80},
        )
        assert overallocated.status_code == 409
        assert "organization budget" in overallocated.json()["detail"]

        lowered = client.put(
            "/api/v1/budgets/organization/org-contoso-global",
            params={"period": "2026-07"},
            headers=headers,
            json={"token_limit": 5_000, "warning_threshold_percent": 80},
        )
        assert lowered.status_code == 409
        assert "allocated child tokens" in lowered.json()["detail"]

        client.cookies.set("turnstile_session", MEMBER_SESSION)
        member_view = client.get(
            "/api/v1/budgets",
            params={"period": "2026-07"},
        )
        assert member_view.status_code == 200
        member_payload = member_view.json()
        assert {
            key: value for key, value in member_payload.items() if key != "generated_at"
        } == {
            key: value for key, value in compact_payload.items() if key != "generated_at"
        }
        client.cookies.set("turnstile_session", OWNER_SESSION)

        protected_parent = client.delete(
            "/api/v1/budgets/organization/org-contoso-global",
            params={"period": "2026-07"},
            headers=headers,
        )
        assert protected_parent.status_code == 409
        assert "department budgets" in protected_parent.json()["detail"]

        removed = client.delete(
            "/api/v1/budgets/user/test.user01@contoso.com",
            params={"period": "2026-07"},
            headers=headers,
        )
        assert removed.status_code == 200
        removed_history = removed.json()["history"]
        removed_payload = client.get(
            "/api/v1/budgets/users",
            params={
                "period": "2026-07",
                "department_id": "department-platform",
                "query": "test.user01@contoso.com",
            },
            headers=headers,
        ).json()
        removed_user = next(
            item
            for item in removed_payload["items"]
            if item["scope_type"] == "user"
            and item["scope_id"] == "test.user01@contoso.com"
        )
        assert removed_user["token_limit"] is None
        assert removed_history[0]["action"] == "removed"
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_people_budgets_support_server_paging_and_bulk_allocation() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    headers = {"X-Hive-Role": "owner"}
    # This test reads back the model-access audit rows it just wrote, and the service stamps
    # those with the current UTC time. A hardcoded month therefore stops matching the moment
    # the calendar rolls past it -- which it did, silently, on the 1st.
    period = datetime.now(UTC).strftime("%Y-%m")
    try:
        for scope_type, scope_id, token_limit in (
            ("organization", "org-contoso-global", 20_000),
            ("department", "department-platform", 8_000),
        ):
            response = client.put(
                f"/api/v1/budgets/{scope_type}/{scope_id}",
                params={"period": period},
                headers=headers,
                json={"token_limit": token_limit, "warning_threshold_percent": 80},
            )
            assert response.status_code == 200

        first_page = client.get(
            "/api/v1/budgets/users",
            params={
                "period": period,
                "department_id": "department-platform",
                "offset": 0,
                "limit": 2,
            },
            headers=headers,
        )
        assert first_page.status_code == 200
        first_payload = first_page.json()
        assert first_payload["total"] == 4
        assert len(first_payload["items"]) == 2
        assert first_payload["assigned_count"] == 0
        assert first_payload["unallocated_count"] == 4

        bulk = client.post(
            "/api/v1/budgets/users/bulk",
            params={"period": period},
            headers=headers,
            json={
                "department_id": "department-platform",
                "selection": "all_matching",
                "query": "test.user",
                "status": "unallocated",
                "allocation_mode": "fixed",
                "token_limit": 1_500,
                "warning_threshold_percent": 75,
            },
        )
        assert bulk.status_code == 200
        assert bulk.json() == {
            "period": period,
            "department_id": "department-platform",
            "updated_count": 4,
            "token_limit_per_user": 1_500,
            "total_allocated": 6_000,
            "model_policy_updated_count": 0,
        }

        assigned = client.get(
            "/api/v1/budgets/users",
            params={
                "period": period,
                "department_id": "department-platform",
                "status": "assigned",
                "offset": 0,
                "limit": 50,
            },
            headers=headers,
        ).json()
        assert assigned["total"] == 4
        assert assigned["assigned_count"] == 4
        assert all(item["token_limit"] == 1_500 for item in assigned["items"])

        equalized = client.post(
            "/api/v1/budgets/users/bulk",
            params={"period": period},
            headers=headers,
            json={
                "department_id": "department-platform",
                "selection": "all_matching",
                "status": "assigned",
                "allocation_mode": "equal_remaining",
                "warning_threshold_percent": 80,
            },
        )
        assert equalized.status_code == 200
        assert equalized.json()["token_limit_per_user"] == 2_000
        assert equalized.json()["total_allocated"] == 8_000

        allowed_model_id = str(
            next(model["id"] for model in repository.models if model["enabled"])
        )
        model_only = client.post(
            "/api/v1/budgets/users/bulk",
            params={"period": period},
            headers=headers,
            json={
                "department_id": "department-platform",
                "selection": "ids",
                "user_ids": [
                    "test.user01@contoso.com",
                    "test.user06@contoso.com",
                ],
                "status": "all",
                "allocation_mode": "preserve",
                "model_ids": [allowed_model_id],
            },
        )
        assert model_only.status_code == 200
        assert model_only.json() == {
            "period": period,
            "department_id": "department-platform",
            "updated_count": 2,
            "token_limit_per_user": None,
            "total_allocated": None,
            "model_policy_updated_count": 2,
        }
        configured = client.get(
            "/api/v1/budgets/users",
            params={
                "period": period,
                "department_id": "department-platform",
                "query": "test.user01",
            },
            headers=headers,
        ).json()
        assert configured["model_configured_count"] == 2
        assert configured["items"][0]["model_policy_configured"] is True
        assert configured["items"][0]["allowed_model_ids"] == [allowed_model_id]
        activity = client.get(
            "/api/v1/budgets",
            params={"period": period},
            headers=headers,
        ).json()["history"]
        model_events = [
            event for event in activity if event["event_type"] == "model_access"
        ]
        assert len(model_events) == 2
        assert {
            event["user_id"] for event in model_events
        } == {
            "test.user01@contoso.com",
            "test.user06@contoso.com",
        }
        assert all(event["previous_model_ids"] == [] for event in model_events)
        assert all(
            event["new_model_ids"] == [allowed_model_id] for event in model_events
        )
        model_audit_count = len(repository.user_model_access_audit)
        repeated_model_update = client.post(
            "/api/v1/budgets/users/bulk",
            params={"period": period},
            headers=headers,
            json={
                "department_id": "department-platform",
                "selection": "ids",
                "user_ids": [
                    "test.user01@contoso.com",
                    "test.user06@contoso.com",
                ],
                "status": "all",
                "allocation_mode": "preserve",
                "model_ids": [allowed_model_id],
            },
        )
        assert repeated_model_update.status_code == 200
        assert len(repository.user_model_access_audit) == model_audit_count

        budget_audit_count = len(repository.token_budget_audit)
        deny_all = client.post(
            "/api/v1/budgets/users/bulk",
            params={"period": period},
            headers=headers,
            json={
                "department_id": "department-platform",
                "selection": "ids",
                "user_ids": ["test.user01@contoso.com"],
                "status": "all",
                "allocation_mode": "fixed",
                "token_limit": 2_000,
                "warning_threshold_percent": 80,
                "model_ids": [],
            },
        )
        assert deny_all.status_code == 200
        assert len(repository.token_budget_audit) == budget_audit_count
        denied = client.get(
            "/api/v1/budgets/users",
            params={
                "period": period,
                "department_id": "department-platform",
                "query": "test.user01",
            },
            headers=headers,
        ).json()["items"][0]
        assert denied["model_policy_configured"] is True
        assert denied["allowed_model_ids"] == []
        latest_activity = client.get(
            "/api/v1/budgets",
            params={"period": period},
            headers=headers,
        ).json()["history"][0]
        assert latest_activity["event_type"] == "model_access"
        assert latest_activity["user_id"] == "test.user01@contoso.com"
        assert latest_activity["previous_model_ids"] == [allowed_model_id]
        assert latest_activity["new_model_ids"] == []

        disabled_model_id = str(
            next(model["id"] for model in repository.models if not model["enabled"])
        )
        unavailable_model = client.post(
            "/api/v1/budgets/users/bulk",
            params={"period": period},
            headers=headers,
            json={
                "department_id": "department-platform",
                "selection": "ids",
                "user_ids": ["test.user01@contoso.com"],
                "status": "all",
                "allocation_mode": "preserve",
                "model_ids": [disabled_model_id],
            },
        )
        assert unavailable_model.status_code == 409
        assert "unavailable" in unavailable_model.json()["detail"]

        overallocated = client.post(
            "/api/v1/budgets/users/bulk",
            params={"period": period},
            headers=headers,
            json={
                "department_id": "department-platform",
                "selection": "ids",
                "user_ids": ["test.user01@contoso.com", "test.user06@contoso.com"],
                "status": "all",
                "allocation_mode": "fixed",
                "token_limit": 5_000,
                "warning_threshold_percent": 80,
            },
        )
        assert overallocated.status_code == 409
        assert "department budget" in overallocated.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_people_budget_directory_is_bounded_at_ten_thousand_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = EnterpriseEntityCatalog(
        organizations=[EnterpriseEntity(id="org-scale", name="Scale Corp")],
        departments=[
            EnterpriseEntity(
                id="department-scale", name="Engineering", parent_id="org-scale"
            )
        ],
        projects=[],
        agents=[],
        users=[
            EnterpriseEntity(
                id=f"person.{index:05d}@scale.example",
                name=f"person.{index:05d}@scale.example",
                parent_id="department-scale",
            )
            for index in range(10_000)
        ],
    )
    monkeypatch.setattr(
        budget_service, "governance_directory", lambda *_args, **_kwargs: catalog
    )
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    headers = {"X-Hive-Role": "owner"}
    try:
        for scope_type, scope_id, token_limit in (
            ("organization", "org-scale", 100_000_000),
            ("department", "department-scale", 100_000_000),
        ):
            response = client.put(
                f"/api/v1/budgets/{scope_type}/{scope_id}",
                params={"period": "2026-07"},
                headers=headers,
                json={"token_limit": token_limit, "warning_threshold_percent": 80},
            )
            assert response.status_code == 200

        main_response = client.get(
            "/api/v1/budgets", params={"period": "2026-07"}, headers=headers
        )
        assert len(main_response.json()["items"]) == 2
        assert len(main_response.content) < 10_000

        page = client.get(
            "/api/v1/budgets/users",
            params={
                "period": "2026-07",
                "department_id": "department-scale",
                "offset": 5_000,
                "limit": 50,
            },
            headers=headers,
        )
        payload = page.json()
        assert payload["total"] == 10_000
        assert len(payload["items"]) == 50
        assert payload["items"][0]["scope_id"] == "person.05000@scale.example"
        assert len(page.content) < 100_000

        searched = client.get(
            "/api/v1/budgets/users",
            params={
                "period": "2026-07",
                "department_id": "department-scale",
                "query": "person.09876",
                "limit": 50,
            },
            headers=headers,
        ).json()
        assert searched["total"] == 1
        assert searched["items"][0]["scope_id"] == "person.09876@scale.example"
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_traffic_plan_is_dry_run_and_budget_bounded() -> None:
    response = client.post(
        "/api/v1/traffic/plan",
        headers={"X-Hive-Role": "owner"},
        json={"requests_per_model": 2, "budget_usd": 1, "dry_run": False},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["request_count"] >= 2
    assert payload["conservative_cost_ceiling"] <= 1
    assert len({item["project_id"] for item in payload["items"]}) >= 2
    assert all("@" in item["user_id"] for item in payload["items"])

def test_traffic_execute_defaults_to_no_paid_calls() -> None:
    response = client.post(
        "/api/v1/traffic/execute",
        headers={"X-Hive-Role": "owner"},
        json={"requests_per_model": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["completed_requests"] == 0
    assert payload["total_estimated_cost"] == 0
    assert payload["stopped_reason"] == "dry_run"
