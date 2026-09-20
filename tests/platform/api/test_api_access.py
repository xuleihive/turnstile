from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi.routing import APIRoute

from backend.api import (
    app,
    assistant_service,
    protected,
    runtime_service,
)
from backend.data_sources.github_copilot.router import (
    copilot_service,
)
from backend.data_sources.github_copilot.router import (
    protected as copilot_protected,
)
from backend.data_sources.github_copilot.router import (
    router as copilot_router,
)
from backend.http.assistant import title_router as assistant_title_router
from backend.http.dependencies import get_repository
from backend.http.model_platform import publication_router
from backend.http.publication_auth import require_publication_owner
from backend.http.session import (
    require_allowed_write_origin,
    require_authenticated_session,
    require_owner_session,
)
from tests.platform.api.api_support import (
    MEMBER_SESSION,
    OWNER_SESSION,
    client,
)
from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.assistant_models import PinnedReport, PinnedReportLayout
from turnstile_core.domain.runtime_models import ModelInvocationRequest
from turnstile_core.persistence.in_memory import InMemoryRepository

pytest_plugins = ("tests.platform.api.api_fixtures",)


def test_budget_history_omits_legacy_noop_audit_events() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    headers = {"X-Hive-Role": "owner"}
    period = datetime.now(UTC).strftime("%Y-%m")
    legacy_event_id = UUID("00000000-0000-0000-0000-000000000001")
    repository.token_budget_audit.append(
        {
            "id": legacy_event_id,
            "period_start": datetime.strptime(period, "%Y-%m").date(),
            "scope_type": "user",
            "scope_id": "test.user01@contoso.com",
            "action": "updated",
            "previous_token_limit": 60_000_000,
            "new_token_limit": 60_000_000,
            "previous_warning_threshold_percent": 80,
            "new_warning_threshold_percent": 80,
            "changed_at": datetime.now(UTC),
            "changed_by": "owner@contoso.com",
        }
    )
    try:
        response = client.get(
            "/api/v1/budgets", params={"period": period}, headers=headers
        )
        assert response.status_code == 200
        assert all(
            event["id"] != str(legacy_event_id)
            for event in response.json()["history"]
        )
    finally:
        app.dependency_overrides.pop(get_repository, None)

def test_health() -> None:
    client.cookies.clear()
    assert client.get("/health").json() == {"status": "ok"}

def test_every_business_api_route_requires_the_shared_session_dependency() -> None:
    # APIM and GitHub Copilot are physically separate routers. Both depend on the same
    # session/CSRF boundary, while only the OAuth callback remains public.
    assert any(
        getattr(route, "original_router", None) is protected for route in app.routes
    )
    assert any(
        getattr(route, "original_router", None) is copilot_router for route in app.routes
    )
    assert any(
        getattr(route, "original_router", None) is publication_router
        for route in app.routes
    )
    assert any(
        getattr(route, "original_router", None) is assistant_title_router
        for route in app.routes
    )
    assert any(
        getattr(route, "original_router", None) is copilot_protected
        for route in copilot_router.routes
    )
    apim_routes = [route for route in protected.routes if isinstance(route, APIRoute)]
    copilot_routes = [
        route for route in copilot_protected.routes if isinstance(route, APIRoute)
    ]

    assert len(apim_routes) == 96
    adoption = next(
        route for route in apim_routes
        if route.path == "/api/v1/model-management/connections/{runtime_id}/adopt"
    )
    assert adoption.methods == {"POST"}
    assert any(
        dependency.call is require_owner_session for dependency in adoption.dependant.dependencies
    )
    assert any(route.path == "/api/v1/model-gateway/images/generations" for route in apim_routes)
    release_methods = {
        (route.path, method)
        for route in apim_routes
        if route.path.startswith("/api/v1/model-management/releases")
        for method in (route.methods or set())
    }
    assert release_methods == {
        ("/api/v1/model-management/releases", "GET"),
        ("/api/v1/model-management/releases/{release_id}", "GET"),
        ("/api/v1/model-management/releases/{release_id}/diff", "GET"),
        ("/api/v1/model-management/releases/{release_id}/integrity", "GET"),
        ("/api/v1/model-management/releases/{release_id}/protection", "PUT"),
        ("/api/v1/model-management/releases/{release_id}/protection", "DELETE"),
        (
            "/api/v1/model-management/releases/{release_id}/integrity-checks",
            "POST",
        ),
        (
            "/api/v1/model-management/releases/{release_id}/rollback-preview",
            "GET",
        ),
        ("/api/v1/model-management/releases/{release_id}/rollback", "POST"),
    }
    owner_release_routes = {
        ("/api/v1/model-management/releases/{release_id}/protection", "PUT"),
        ("/api/v1/model-management/releases/{release_id}/protection", "DELETE"),
        (
            "/api/v1/model-management/releases/{release_id}/integrity-checks",
            "POST",
        ),
        ("/api/v1/model-management/releases/{release_id}/rollback", "POST"),
        (
            "/api/v1/model-management/gateways/{gateway_profile_id}/gc-plans",
            "POST",
        ),
    }
    owner_application_key_routes = {
        (
            "/api/v1/application-access/applications/{application_id}/subscriptions/"
            "{application_subscription_id}/keys/{key_kind}/reveal",
            "POST",
        ),
        (
            "/api/v1/application-access/applications/{application_id}/subscriptions/"
            "{application_subscription_id}/keys/{key_kind}/rotate",
            "POST",
        ),
    }
    for route in apim_routes:
        for method in route.methods or set():
            if (route.path, method) in owner_release_routes:
                assert any(
                    dependency.call is require_owner_session
                    for dependency in route.dependant.dependencies
                ), route.path
            if (route.path, method) in owner_application_key_routes:
                assert any(
                    dependency.call is require_owner_session
                    for dependency in route.dependant.dependencies
                ), route.path
    assert len(copilot_routes) == 25
    for route in [*apim_routes, *copilot_routes]:
        assert any(
            dependency.call is require_authenticated_session
            for dependency in route.dependant.dependencies
        ), route.path
    callback_route = next(
        route
        for route in copilot_router.routes
        if isinstance(route, APIRoute)
        and route.path == "/api/v1/copilot/oauth/callback"
    )
    assert not any(
        dependency.call is require_authenticated_session
        for dependency in callback_route.dependant.dependencies
    )
    publication_routes = [
        route
        for route in publication_router.routes
        if isinstance(route, APIRoute)
        and (
            route.path.startswith("/api/v1/model-management/publications")
            or route.path.endswith("/credentials/rotate")
            or (
                route.path.endswith("/backend-pool")
                and "GET" not in (route.methods or set())
            )
        )
    ]
    assert len(publication_routes) == 9
    for route in publication_routes:
        assert any(
            dependency.call is require_publication_owner
            for dependency in route.dependant.dependencies
        ), route.path

    title_route = next(
        route
        for route in assistant_title_router.routes
        if isinstance(route, APIRoute)
        and route.path == "/api/v1/assistant/conversations/{conversation_id}/title"
    )
    assert any(
        dependency.call is require_authenticated_session
        for dependency in title_route.dependant.dependencies
    )
    assert any(
        dependency.call is require_allowed_write_origin
        for dependency in title_route.dependant.dependencies
    )

def test_public_routes_stay_public_and_me_still_requires_a_session() -> None:
    client.cookies.clear()

    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/not-a-real-route").status_code == 404
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.post("/api/v1/auth/login", json={}).status_code == 422
    assert client.post("/api/v1/auth/entra", json={}).status_code == 422

def test_browser_write_requires_an_allowed_origin() -> None:
    blocked = client.put(
        "/api/v1/budgets/organization/org-contoso-global",
        params={"period": "2026-07"},
        headers={"Origin": "https://attacker.example"},
        json={"token_limit": 10_000, "warning_threshold_percent": 80},
    )
    allowed = client.put(
        "/api/v1/budgets/organization/org-contoso-global",
        params={"period": "2026-07"},
        headers={"Origin": "http://localhost:5173"},
        json={"token_limit": 10_000, "warning_threshold_percent": 80},
    )
    login = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "https://attacker.example"},
        json={},
    )
    entra = client.post(
        "/api/v1/auth/entra",
        headers={"Origin": "https://attacker.example"},
        json={},
    )
    logout = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "https://attacker.example"},
    )

    assert blocked.status_code == 403
    assert blocked.json() == {"detail": "不允许从该来源执行写操作。"}
    assert allowed.status_code == 200
    assert login.status_code == 403
    assert entra.status_code == 403
    assert logout.status_code == 403

def test_session_lookup_uses_the_configured_cookie_name() -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        session_cookie_name="custom_session"
    )
    client.cookies.clear()
    client.cookies.set("custom_session", OWNER_SESSION)
    try:
        response = client.get(
            "/api/v1/budgets",
            params={"period": "2026-07"},
        )
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 200

def test_business_api_requires_a_session_even_with_a_spoofed_owner_header() -> None:
    client.cookies.clear()

    response = client.get(
        "/api/v1/budgets",
        params={"period": "2026-07"},
        headers={"X-Hive-Role": "owner"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "未登录。"}

def test_member_can_read_budget_and_anomaly_views_but_cannot_manage_them() -> None:
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    period = "2026-07"

    budget_overview = client.get("/api/v1/budgets", params={"period": period})
    people = client.get(
        "/api/v1/budgets/users",
        params={
            "period": period,
            "department_id": "department-platform",
            "offset": 0,
            "limit": 50,
        },
    )
    rules = client.get("/api/v1/anomaly-rules")

    assert budget_overview.status_code == 200
    assert people.status_code == 200
    assert rules.status_code == 200

    budget_write = client.put(
        "/api/v1/budgets/organization/org-contoso-global",
        params={"period": period},
        headers={"X-Hive-Role": "owner"},
        json={"token_limit": 10_000, "warning_threshold_percent": 80},
    )
    bulk_write = client.post(
        "/api/v1/budgets/users/bulk",
        params={"period": period},
        json={
            "department_id": "department-platform",
            "selection": "ids",
            "user_ids": ["test.user01@contoso.com"],
            "status": "all",
            "allocation_mode": "fixed",
            "token_limit": 1_000,
            "warning_threshold_percent": 80,
        },
    )
    enforcement_write = client.put(
        "/api/v1/budgets/enforcement/department-platform",
        params={"period": period},
        json={"mode": "block"},
    )
    rule_write = client.post(
        "/api/v1/anomaly-rules",
        json={
            "name": "Member must not create",
            "metric": "request_latency_ms",
            "threshold_mode": "absolute",
            "threshold_value": 1_500,
            "minimum_sample_size": 1,
            "severity": "warning",
            "scope_type": "global",
            "enabled": True,
        },
    )

    assert budget_write.status_code == 403
    assert bulk_write.status_code == 403
    assert enforcement_write.status_code == 403
    assert rule_write.status_code == 403

def test_copilot_administration_is_owner_only_at_the_api_boundary() -> None:
    calls: list[str] = []

    class StubCopilotService:
        def status(
            self, user_id: UUID, role: str, origin: str = ""
        ) -> dict[str, object]:
            calls.append(f"status:{user_id}:{role}")
            return {
                "configured": False,
                "oauth_configured": bool(origin),
                "viewer_role": role,
                "viewer_github_login": None,
                "connections": [],
            }

        def budget_requests(self, user_id: UUID, role: str) -> dict[str, object]:
            calls.append(f"requests:{user_id}:{role}")
            return {
                "items": [],
                "can_review": False,
                "pending_count": 0,
                "approved_count": 0,
                "rejected_count": 0,
            }

        async def governance(
            self, *, role: str, organization: str | None = None
        ) -> dict[str, object]:
            calls.append(f"governance:{role}:{organization}")
            return {}

        async def cost_center_options(
            self, *, role: str, organization: str | None = None
        ) -> list[object]:
            calls.append(f"cost-center-options:{role}:{organization}")
            return []

        def cost_center_requests(
            self, user_id: UUID, role: str
        ) -> dict[str, object]:
            calls.append(f"cost-center-requests:{user_id}:{role}")
            return {
                "items": [],
                "can_review": False,
                "pending_count": 0,
                "approved_count": 0,
                "rejected_count": 0,
            }

    app.dependency_overrides[copilot_service] = StubCopilotService
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        status = client.get("/api/v1/copilot/status")
        requests = client.get("/api/v1/copilot/budget-requests")
        governance = client.get("/api/v1/copilot/governance")
        cost_center_options = client.get("/api/v1/copilot/cost-centers/options")
        cost_center_requests = client.get("/api/v1/copilot/cost-center-requests")
        imported_usage = client.get(
            "/api/v1/copilot/imported-usage", params={"source_kind": "ai_usage"}
        )
        usage_imports = client.get(
            "/api/v1/copilot/usage-imports", params={"source_kind": "ai_usage"}
        )
        usage_import = client.post(
            "/api/v1/copilot/usage-imports",
            params={"filename": "usage.csv"},
            headers={"Content-Type": "text/csv"},
            content=b"date,organization,username,model,quantity,gross_amount\n",
        )
        connection = client.put(
            "/api/v1/copilot/connections",
            headers={"X-Hive-Role": "owner"},
            json={
                "organization": "example-enterprise",
                "token": "not-a-real-token",
                "write_enabled": False,
                "set_default": True,
            },
        )
        identities = client.get(
            "/api/v1/copilot/identities", headers={"X-Hive-Role": "owner"}
        )
        identity = client.put(
            "/api/v1/copilot/identities/00000000-0000-4000-8000-000000000001",
            headers={"X-Hive-Role": "owner"},
            json={"github_login": "member"},
        )
        review = client.post(
            "/api/v1/copilot/budget-requests/00000000-0000-4000-8000-000000000001/review",
            headers={"X-Hive-Role": "owner"},
            json={"decision": "approve", "apply_to_github": False},
        )
        cost_center_review = client.post(
            "/api/v1/copilot/cost-center-requests/00000000-0000-4000-8000-000000000001/review",
            json={"decision": "approve", "apply_to_github": False},
        )
    finally:
        app.dependency_overrides.pop(copilot_service, None)

    assert status.status_code == 200
    assert requests.status_code == 200
    assert governance.status_code == 403
    assert cost_center_options.status_code == 200
    assert cost_center_requests.status_code == 200
    assert imported_usage.status_code == 403
    assert usage_imports.status_code == 403
    assert usage_import.status_code == 403
    assert connection.status_code == 403
    assert identities.status_code == 403
    assert identity.status_code == 403
    assert review.status_code == 403
    assert cost_center_review.status_code == 403
    assert calls == [
        "status:00000000-0000-4000-8000-000000000002:member",
        "requests:00000000-0000-4000-8000-000000000002:member",
        "cost-center-options:member:None",
        "cost-center-requests:00000000-0000-4000-8000-000000000002:member",
    ]

def test_owner_session_cannot_be_downgraded_by_a_spoofed_role_header() -> None:
    response = client.get(
        "/api/v1/budgets",
        params={"period": "2026-07"},
        headers={"X-Hive-Role": "member"},
    )

    assert response.status_code == 200

def test_assistant_owner_comes_from_session_not_spoofed_user_header() -> None:
    owners: list[str] = []

    class CapturingAssistantService:
        def list_pinned(self, owner: str) -> list[object]:
            owners.append(owner)
            return []

    app.dependency_overrides[assistant_service] = CapturingAssistantService
    try:
        response = client.get(
            "/api/v1/assistant/pinned-charts",
            headers={"X-Hive-User": "victim@contoso.com"},
        )
    finally:
        app.dependency_overrides.pop(assistant_service, None)

    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert owners == ["owner@contoso.com"]

def test_report_layout_write_is_session_bound_and_strictly_bounded() -> None:
    writes: list[tuple[UUID, str, PinnedReportLayout]] = []

    class CapturingAssistantService:
        def set_pinned_layout(
            self,
            report_id: UUID,
            owner: str,
            layout: PinnedReportLayout,
        ) -> PinnedReport:
            writes.append((report_id, owner, layout))
            now = datetime(2026, 8, 10, tzinfo=UTC)
            return PinnedReport(
                id=report_id,
                title="Layout report",
                description="",
                owner_id=owner,
                can_manage=True,
                position=0,
                layout=layout,
                created_at=now,
                updated_at=now,
            )

    report_id = UUID("0e38b479-4d4d-4b98-ada3-ee785afeb903")
    chart_id = "a0d2fd54-d725-4add-a899-0f429abe3931"
    app.dependency_overrides[assistant_service] = CapturingAssistantService
    try:
        response = client.put(
            f"/api/v1/assistant/pinned-charts/{report_id}/layout",
            headers={"X-Hive-User": "victim@contoso.com"},
            json={"version": 1, "spans": {chart_id: 6}, "row_heights": {"0": 420}},
        )
        invalid = client.put(
            f"/api/v1/assistant/pinned-charts/{report_id}/layout",
            json={"version": 1, "spans": {chart_id: 13}, "row_heights": {"0": 199}},
        )
    finally:
        app.dependency_overrides.pop(assistant_service, None)

    assert response.status_code == 200
    assert response.json()["layout"] == {
        "version": 1,
        "spans": {chart_id: 6},
        "row_heights": {"0": 420},
    }
    assert writes[0][1] == "owner@contoso.com"
    assert invalid.status_code == 422
    assert len(writes) == 1

def test_member_invocation_accepts_self_or_configured_tester_only() -> None:
    captured: list[ModelInvocationRequest] = []

    class CapturingRuntimeService:
        def invoke(self, request: ModelInvocationRequest) -> dict[str, object]:
            captured.append(request)
            return {
                "request_id": "request-id",
                "correlation_id": "correlation-id",
                "content": "ok",
                "provider": "test",
                "runtime": "test",
                "model": "test",
                "gateway": "test",
                "latency_ms": 1,
                "usage": None,
                "estimated_cost": None,
            }

    payload: dict[str, Any] = {
        "metadata": {
            "organization_id": "org-contoso-global",
            "organization": "Contoso Global",
            "department_id": "department-platform",
            "department": "AI Platform",
            "project_id": "project-finops",
            "project": "Model FinOps",
            "agent_id": "agent-console",
            "agent": "Invocation Console",
            "user_id": "victim@contoso.com",
            "user": "Victim",
            "workflow": "security-test",
            "model_id": "test-model",
            "model": "test-model",
            "runtime": "test-runtime",
            "request_source": "security-test",
            "run_id": "security-test",
            "turn_index": 1,
        },
        "messages": [{"role": "user", "content": "hello"}],
    }
    app.dependency_overrides[runtime_service] = CapturingRuntimeService
    app.dependency_overrides[get_settings] = lambda: Settings(
        delegated_invocation_tester_ids=[
            "test.user01@contoso.com",
            "test.user02@contoso.com",
        ]
    )
    try:
        client.cookies.set("turnstile_session", MEMBER_SESSION)
        arbitrary_member = client.post("/api/v1/model-gateway/invoke", json=payload)
        self_payload = {
            **payload,
            "metadata": {
                **payload["metadata"],
                "user_id": "member@contoso.com",
                "user": "Forged member name",
            },
        }
        self_member = client.post("/api/v1/model-gateway/invoke", json=self_payload)
        tester_payload = {
            **payload,
            "metadata": {
                **payload["metadata"],
                "user_id": "test.user01@contoso.com",
                "user": "Forged tester name",
            },
        }
        tester_member = client.post("/api/v1/model-gateway/invoke", json=tester_payload)
        wrong_department_payload = {
            **tester_payload,
            "metadata": {
                **tester_payload["metadata"],
                "user_id": "test.user02@contoso.com",
            },
        }
        wrong_department = client.post(
            "/api/v1/model-gateway/invoke", json=wrong_department_payload
        )
        client.cookies.set("turnstile_session", OWNER_SESSION)
        owner = client.post("/api/v1/model-gateway/invoke", json=payload)
    finally:
        app.dependency_overrides.pop(runtime_service, None)
        app.dependency_overrides.pop(get_settings, None)

    assert arbitrary_member.status_code == 403
    assert self_member.status_code == 200
    assert tester_member.status_code == 200
    assert wrong_department.status_code == 422
    assert owner.status_code == 200
    assert captured[0].metadata.user_id == "member@contoso.com"
    assert captured[0].metadata.user == "Member"
    assert captured[1].metadata.user_id == "test.user01@contoso.com"
    assert captured[1].metadata.user == "test.user01@contoso.com"
    assert captured[2].metadata.user_id == "victim@contoso.com"
    assert captured[2].metadata.user == "Victim"

def test_unknown_api_route_never_falls_back_to_spa_html() -> None:
    response = client.get("/api/v1/not-a-real-route")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": "API route not found"}
