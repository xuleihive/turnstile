"""Attribution arriving from the subscription instead of from a request header.

The install this is for sends no `x-user-id` and no `x-department-id` on any request, so before
this everything landed on `unattributed` and the budget rollup discarded it. These tests pin the
two halves of the bargain that makes filling it in automatically safe: a sync may fill a blank,
and a sync may never overwrite what a person decided.
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.api import app, application_access_service
from backend.http.dependencies import get_repository
from backend.http.session import require_authenticated_session
from tests.backend.model_platform.control_plane_support import APIM_ID
from tests.platform.api.api_support import client
from turnstile_core.domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.application_access import ApplicationAccessService

pytest_plugins = ("tests.platform.api.api_fixtures",)

APPLICATIONS = "/api/v1/application-access/applications"
OWNER = "/api/v1/application-access/applications/{0}/owner"
DEPARTMENT = "/api/v1/application-access/applications/{0}/department"


def _item(subscription_id: str, display_name: str, **overrides: object) -> (
    GatewayApplicationDiscoveryItem
):
    return GatewayApplicationDiscoveryItem(
        apim_subscription_id=subscription_id,
        display_name=display_name,
        state="active",
        scope_type="product",
        scope_id="finops-applications",
        scope_exists=True,
        application_type=overrides.get("application_type", "service"),  # type: ignore[arg-type]
        system_managed=bool(overrides.get("system_managed", False)),
        owner_email=overrides.get("owner_email"),  # type: ignore[arg-type]
    )


# Both halves of one person's estate, in the shape the customer's APIM actually holds them:
# a direct key and a Databricks key, neither carrying an email anywhere.
NAMELESS_DIRECT = _item("dongyuli-it", "dongyuli-IT")
NAMELESS_DATABRICKS = _item("dongyuli-it-databricks", "dongyuli-IT - Databricks")
# The minority that does carry an address.
EMAIL_NAMED = _item(
    "sub-a-aiginin", "a.aiginin@insilicomedicine.com - IT Databricks Claude API"
)
APIM_OWNED = _item(
    "sub-e-kirilin-foundry",
    "Subscription for e.kirilin - Foundry",
    owner_email="e.kirilin@insilicomedicine.com",
)
SYSTEM = _item(
    "turnstile-dashboard", "Turnstile Dashboard",
    application_type="system", system_managed=True,
)


def _sync(
    repository: InMemoryRepository, *items: GatewayApplicationDiscoveryItem
) -> ApplicationAccessService:
    service = ApplicationAccessService(repository, sync_available=True)
    service.sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=APIM_ID,
            discovered_at=datetime(2026, 9, 20, 2, tzinfo=UTC),
            items=list(items),
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


def _by_name(items: list[dict[str, object]], name: str) -> dict[str, object]:
    return next(item for item in items if item["display_name"] == name)


def test_an_address_in_the_name_becomes_the_owner() -> None:
    repository = InMemoryRepository()
    _sync(repository, EMAIL_NAMED)
    try:
        item = client.get(APPLICATIONS).json()["items"]
        found = _by_name(item, EMAIL_NAMED.display_name)
        assert found["owner_id"] == "a.aiginin@insilicomedicine.com"
        assert found["owner_source"] == "derived"
    finally:
        _uninstall()


def test_the_apim_owner_outranks_the_name() -> None:
    repository = InMemoryRepository()
    _sync(repository, APIM_OWNED)
    try:
        found = _by_name(client.get(APPLICATIONS).json()["items"], APIM_OWNED.display_name)
        assert found["owner_id"] == "e.kirilin@insilicomedicine.com"
        assert found["owner_source"] == "apim"
    finally:
        _uninstall()


def test_a_holder_with_no_address_gets_no_invented_one() -> None:
    repository = InMemoryRepository()
    _sync(repository, NAMELESS_DIRECT)
    try:
        found = _by_name(client.get(APPLICATIONS).json()["items"], "dongyuli-IT")
        assert found["owner_id"] is None
        assert found["owner_source"] is None
    finally:
        _uninstall()


def test_two_keys_of_one_holder_are_reported_as_one_person() -> None:
    repository = InMemoryRepository()
    _sync(repository, NAMELESS_DIRECT, NAMELESS_DATABRICKS)
    try:
        items = client.get(APPLICATIONS).json()["items"]
        direct = _by_name(items, "dongyuli-IT")
        databricks = _by_name(items, "dongyuli-IT - Databricks")
        assert direct["person_group"] == databricks["person_group"]
        # Both rows say it, because an administrator may be looking at either one.
        assert direct["person_group_size"] == 2
        assert databricks["person_group_size"] == 2
    finally:
        _uninstall()


def test_a_platform_subscription_is_not_given_a_holder() -> None:
    repository = InMemoryRepository()
    _sync(repository, SYSTEM)
    try:
        found = _by_name(client.get(APPLICATIONS).json()["items"], "Turnstile Dashboard")
        assert found["owner_id"] is None
        assert found["system_managed"] is True
    finally:
        _uninstall()


def test_a_second_key_inherits_the_department_of_the_first() -> None:
    repository = InMemoryRepository()
    service = _sync(repository, NAMELESS_DIRECT)
    try:
        first = repository.gateway_applications[0]["id"]
        client.put(DEPARTMENT.format(first), json={"department_id": "department-security"})

        # The second key shows up on a later sync, as it would when someone issues it.
        service.sync_discovery(
            GatewayApplicationDiscovery(
                gateway_profile_id=APIM_ID,
                discovered_at=datetime(2026, 9, 21, 2, tzinfo=UTC),
                items=[NAMELESS_DIRECT, NAMELESS_DATABRICKS],
            ),
            "application-sync-worker",
        )
        items = client.get(APPLICATIONS).json()["items"]
        second = _by_name(items, "dongyuli-IT - Databricks")
        assert second["department_id"] == "department-security"
        assert second["department_source"] == "derived"
    finally:
        _uninstall()


def test_a_sync_fills_a_blank_that_predates_attribution() -> None:
    repository = InMemoryRepository()
    service = _sync(repository, EMAIL_NAMED)
    try:
        # An estate adopted before attribution existed has both columns NULL and will never
        # pass through the adoption branch again.
        repository.gateway_applications[0]["owner_id"] = None
        repository.gateway_application_attribution.clear()

        service.sync_discovery(
            GatewayApplicationDiscovery(
                gateway_profile_id=APIM_ID,
                discovered_at=datetime(2026, 9, 21, 2, tzinfo=UTC),
                items=[EMAIL_NAMED],
            ),
            "application-sync-worker",
        )
        found = _by_name(client.get(APPLICATIONS).json()["items"], EMAIL_NAMED.display_name)
        assert found["owner_id"] == "a.aiginin@insilicomedicine.com"
    finally:
        _uninstall()


def test_a_sync_never_overwrites_what_a_person_decided() -> None:
    repository = InMemoryRepository()
    service = _sync(repository, EMAIL_NAMED)
    try:
        application_id = repository.gateway_applications[0]["id"]
        corrected = client.put(
            OWNER.format(application_id), json={"owner_id": "someone.else@insilicomedicine.com"}
        )
        assert corrected.status_code == 200
        assert corrected.json()["owner_source"] == "manual"

        # Derivation would say a.aiginin@ here; the correction has to survive it.
        service.sync_discovery(
            GatewayApplicationDiscovery(
                gateway_profile_id=APIM_ID,
                discovered_at=datetime(2026, 9, 21, 2, tzinfo=UTC),
                items=[EMAIL_NAMED],
            ),
            "application-sync-worker",
        )
        found = _by_name(client.get(APPLICATIONS).json()["items"], EMAIL_NAMED.display_name)
        assert found["owner_id"] == "someone.else@insilicomedicine.com"
        assert found["owner_source"] == "manual"
    finally:
        _uninstall()


def test_a_filed_department_is_marked_as_decided_by_a_person() -> None:
    repository = InMemoryRepository()
    service = _sync(repository, NAMELESS_DIRECT, NAMELESS_DATABRICKS)
    try:
        first = repository.gateway_applications[0]["id"]
        filed = client.put(
            DEPARTMENT.format(first), json={"department_id": "department-platform"}
        )
        assert filed.json()["department_source"] == "manual"

        service.sync_discovery(
            GatewayApplicationDiscovery(
                gateway_profile_id=APIM_ID,
                discovered_at=datetime(2026, 9, 21, 2, tzinfo=UTC),
                items=[NAMELESS_DIRECT, NAMELESS_DATABRICKS],
            ),
            "application-sync-worker",
        )
        items = client.get(APPLICATIONS).json()["items"]
        assert _by_name(items, "dongyuli-IT")["department_id"] == "department-platform"
    finally:
        _uninstall()


def test_an_owner_without_an_address_is_refused() -> None:
    repository = InMemoryRepository()
    _sync(repository, NAMELESS_DIRECT)
    try:
        application_id = repository.gateway_applications[0]["id"]
        response = client.put(OWNER.format(application_id), json={"owner_id": "dongyuli-IT"})
        # Accepting it would store an id the budget directory can never admit, so the
        # subscription would read as attributed and still be unbudgetable.
        assert response.status_code == 422
    finally:
        _uninstall()


def test_an_owner_can_be_cleared() -> None:
    repository = InMemoryRepository()
    _sync(repository, EMAIL_NAMED)
    try:
        application_id = repository.gateway_applications[0]["id"]
        response = client.put(OWNER.format(application_id), json={"owner_id": None})
        assert response.status_code == 200
        assert response.json()["owner_id"] is None
    finally:
        _uninstall()


def test_an_unknown_subscription_is_not_found() -> None:
    repository = InMemoryRepository()
    _sync(repository, EMAIL_NAMED)
    try:
        response = client.put(
            OWNER.format("11111111-1111-1111-1111-111111111111"),
            json={"owner_id": "someone@insilicomedicine.com"},
        )
        assert response.status_code == 404
    finally:
        _uninstall()
