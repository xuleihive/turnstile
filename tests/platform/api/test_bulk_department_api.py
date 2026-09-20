"""Filing several hundred subscriptions without opening several hundred dialogs.

One dialog at a time is not a slow version of this; it is the reason an inventory stays
unfiled, which is the state the whole area exists to get out of.
"""

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

BULK = "/api/v1/application-access/applications/bulk-department"
PEOPLE = ["a.aiginin", "shalin", "y.zhang"]


def _item(
    subscription_id: str, display_name: str, system: bool = False
) -> GatewayApplicationDiscoveryItem:
    return GatewayApplicationDiscoveryItem(
        apim_subscription_id=subscription_id,
        display_name=display_name,
        state="active",
        scope_type="product",
        scope_id="finops-applications",
        scope_exists=True,
        application_type="system" if system else "service",
        system_managed=system,
    )


def _seed(repository: InMemoryRepository) -> ApplicationAccessService:
    service = ApplicationAccessService(repository, sync_available=True)
    service.sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=APIM_ID,
            discovered_at=datetime(2026, 9, 20, 2, tzinfo=UTC),
            items=[
                *[
                    _item(f"sub-{name.replace('.', '-')}-insilicomedicine-com",
                          f"{name}@insilicomedicine.com - IT Databricks Claude API")
                    for name in PEOPLE
                ],
                _item("chem42-pipeline", "Chem42 Pipeline"),
                _item("turnstile-dashboard", "Turnstile Dashboard", system=True),
            ],
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


def _ids(repository: InMemoryRepository, *, include_system: bool = False) -> list[str]:
    return [
        str(row["id"]) for row in repository.gateway_applications
        if include_system or not row.get("system_managed")
    ]


def test_one_action_files_a_batch() -> None:
    repository = InMemoryRepository()
    _seed(repository)
    try:
        response = client.put(BULK, json={
            "application_ids": _ids(repository),
            "department_id": "department-platform",
        })
        assert response.status_code == 200
        assert response.json()["updated"] == 4
        assert all(
            row["department_id"] == "department-platform"
            for row in repository.gateway_applications if not row["system_managed"]
        )
    finally:
        _uninstall()


def test_filing_a_batch_where_it_already_is_reports_no_change() -> None:
    repository = InMemoryRepository()
    _seed(repository)
    try:
        body = {"application_ids": _ids(repository), "department_id": "department-finance"}
        assert client.put(BULK, json=body).json()["updated"] == 4
        second = client.put(BULK, json=body).json()
        assert second["updated"] == 0
        assert second["unchanged"] == 4
    finally:
        _uninstall()


def test_a_system_subscription_in_the_batch_is_skipped_rather_than_failing_it() -> None:
    """Four hundred rows should not be refused because one belongs to the platform."""
    repository = InMemoryRepository()
    _seed(repository)
    try:
        response = client.put(BULK, json={
            "application_ids": _ids(repository, include_system=True),
            "department_id": "department-finance",
        })
        assert response.status_code == 200
        assert response.json()["unchanged"] >= 1
        system = next(
            row for row in repository.gateway_applications if row["system_managed"]
        )
        assert system["department_id"] is None
    finally:
        _uninstall()


def test_the_department_is_still_checked_for_a_batch() -> None:
    repository = InMemoryRepository()
    _seed(repository)
    try:
        response = client.put(BULK, json={
            "application_ids": _ids(repository),
            "department_id": "department-does-not-exist",
        })
        assert response.status_code == 422
        assert "department-does-not-exist" in response.json()["detail"]
        assert all(
            row["department_id"] is None for row in repository.gateway_applications
        )
    finally:
        _uninstall()


def test_a_batch_can_unfile() -> None:
    repository = InMemoryRepository()
    _seed(repository)
    try:
        client.put(BULK, json={
            "application_ids": _ids(repository), "department_id": "department-platform",
        })
        client.put(BULK, json={
            "application_ids": _ids(repository), "department_id": None,
        })
        assert all(
            row["department_id"] is None for row in repository.gateway_applications
        )
    finally:
        _uninstall()


def test_the_batch_route_requires_owner_role() -> None:
    repository = InMemoryRepository()
    _seed(repository)
    app.dependency_overrides[require_authenticated_session] = lambda: SessionIdentity(
        id="member-id",
        email="member@contoso.com",
        name="Member",
        role="member",
        method="entra",
        session_expires_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    try:
        response = client.put(BULK, json={
            "application_ids": _ids(repository),
            "department_id": "department-platform",
        })
        assert response.status_code == 403
        assert all(
            row["department_id"] is None for row in repository.gateway_applications
        )
    finally:
        _uninstall()


def test_an_empty_or_oversized_batch_is_refused() -> None:
    repository = InMemoryRepository()
    _seed(repository)
    try:
        assert client.put(BULK, json={
            "application_ids": [], "department_id": None,
        }).status_code == 422
        assert client.put(BULK, json={
            "application_ids": [f"00000000-0000-4000-8000-{index:012d}" for index in range(501)],
            "department_id": None,
        }).status_code == 422
    finally:
        _uninstall()
