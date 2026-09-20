from __future__ import annotations

from datetime import UTC, datetime

from backend.api import app, application_access_service
from backend.http.dependencies import get_repository
from backend.http.session import SessionIdentity, require_authenticated_session
from tests.backend.model_platform.control_plane_support import APIM_ID
from tests.platform.api.api_support import client
from turnstile_core.domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.application_access import ApplicationAccessService

pytest_plugins = ("tests.platform.api.api_fixtures",)

DEPARTMENT = "/api/v1/application-access/applications/{0}/department"

# The shape an install that names subscriptions after people produces.
PERSON_NAMED = GatewayApplicationDiscoveryItem(
    apim_subscription_id="sub-a-aiginin-insilicomedicine-com",
    display_name="a.aiginin@insilicomedicine.com - IT Databricks Claude API",
    state="active",
    scope_type="product",
    scope_id="finops-applications",
    scope_exists=True,
    application_type="service",
    system_managed=False,
)
UNNAMED = GatewayApplicationDiscoveryItem(
    apim_subscription_id="aiclaw",
    display_name="AIClaw",
    state="active",
    scope_type="product",
    scope_id="finops-applications",
    scope_exists=True,
    application_type="service",
    system_managed=False,
)


def _seed(
    repository: InMemoryRepository, *items: GatewayApplicationDiscoveryItem
) -> ApplicationAccessService:
    service = ApplicationAccessService(repository, sync_available=True)
    service.sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=APIM_ID,
            discovered_at=datetime(2026, 9, 20, 2, tzinfo=UTC),
            items=list(items) or [PERSON_NAMED],
        ),
        "application-sync-worker",
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: service
    return service


def _uninstall() -> None:
    app.dependency_overrides.pop(application_access_service, None)
    app.dependency_overrides.pop(get_repository, None)
    app.dependency_overrides.pop(require_authenticated_session, None)


def test_adopted_subscriptions_arrive_unfiled() -> None:
    repository = InMemoryRepository()
    _seed(repository, PERSON_NAMED, UNNAMED)
    try:
        items = client.get("/api/v1/application-access/applications").json()["items"]
        for item in items:
            if not item["system_managed"]:
                assert item["department_id"] is None
                assert item["department_name"] is None
    finally:
        _uninstall()


def test_filing_a_subscription_resolves_the_department_name() -> None:
    repository = InMemoryRepository()
    _seed(repository)
    application_id = repository.gateway_applications[0]["id"]
    try:
        response = client.put(
            DEPARTMENT.format(application_id), json={"department_id": "department-platform"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["department_id"] == "department-platform"
        # Resolved from the catalog, so a renamed department does not leave its old name
        # on every subscription filed under it.
        assert body["department_name"] == "AI Platform"

        trail = [
            item for item in repository.gateway_application_audit
            if item["operation"] == "updated"
        ]
        assert len(trail) == 1
        assert trail[0]["actor"] == "owner@contoso.com"
        assert trail[0]["before_state"] == {"department_id": None}
    finally:
        _uninstall()


def test_a_department_that_does_not_exist_is_refused_and_nothing_is_written() -> None:
    """The check that keeps the filing from pointing at nothing.

    A subscription filed under a department that is not in the catalog is filed nowhere,
    while still reading on screen as though someone had filed it -- worse than the blank
    it replaced.
    """
    repository = InMemoryRepository()
    _seed(repository)
    application_id = repository.gateway_applications[0]["id"]
    try:
        response = client.put(
            DEPARTMENT.format(application_id),
            json={"department_id": "department-does-not-exist"},
        )
        assert response.status_code == 422
        assert "department-does-not-exist" in response.json()["detail"]
        assert repository.gateway_applications[0]["department_id"] is None
        assert not [
            item for item in repository.gateway_application_audit
            if item["operation"] == "updated"
        ]
    finally:
        _uninstall()


def test_re_syncing_the_gateway_does_not_unfile_a_subscription() -> None:
    """The regression this file exists for.

    Filing is manual and sync runs on a button anyone can press. If a later discovery reset
    this column the work would disappear without an error, and the only evidence would be a
    screen that used to name a department and now names nothing.
    """
    repository = InMemoryRepository()
    service = _seed(repository)
    application_id = repository.gateway_applications[0]["id"]
    try:
        client.put(
            DEPARTMENT.format(application_id), json={"department_id": "department-platform"}
        )
        service.sync_discovery(
            GatewayApplicationDiscovery(
                gateway_profile_id=APIM_ID,
                discovered_at=datetime(2026, 9, 21, 2, tzinfo=UTC),
                items=[
                    PERSON_NAMED.model_copy(
                        update={"display_name": "a.aiginin@insilicomedicine.com - renamed"}
                    )
                ],
            ),
            "application-sync-worker",
        )
        stored = repository.gateway_applications[0]
        assert stored["display_name"].endswith("renamed")
        assert stored["department_id"] == "department-platform"
    finally:
        _uninstall()


def test_a_subscription_can_be_unfiled_again() -> None:
    repository = InMemoryRepository()
    _seed(repository)
    application_id = repository.gateway_applications[0]["id"]
    try:
        client.put(
            DEPARTMENT.format(application_id), json={"department_id": "department-platform"}
        )
        response = client.put(DEPARTMENT.format(application_id), json={"department_id": None})
        assert response.status_code == 200
        assert response.json()["department_id"] is None
    finally:
        _uninstall()


def test_filing_it_where_it_already_is_files_no_audit_row() -> None:
    repository = InMemoryRepository()
    _seed(repository)
    application_id = repository.gateway_applications[0]["id"]
    try:
        body = {"department_id": "department-platform"}
        assert client.put(DEPARTMENT.format(application_id), json=body).status_code == 200
        assert client.put(DEPARTMENT.format(application_id), json=body).status_code == 200
        assert len([
            item for item in repository.gateway_application_audit
            if item["operation"] == "updated"
        ]) == 1
    finally:
        _uninstall()


def test_filing_a_subscription_requires_owner_role() -> None:
    repository = InMemoryRepository()
    _seed(repository)
    application_id = repository.gateway_applications[0]["id"]
    app.dependency_overrides[require_authenticated_session] = lambda: SessionIdentity(
        id="member-id",
        email="member@contoso.com",
        name="Member",
        role="member",
        method="entra",
        session_expires_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    try:
        response = client.put(
            DEPARTMENT.format(application_id), json={"department_id": "department-platform"}
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Owner role is required"
        assert repository.gateway_applications[0]["department_id"] is None
    finally:
        _uninstall()
