from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from tests.backend.model_platform.control_plane_support import (
    APIM_ID,
    StubTokenProvider,
    publisher_settings,
)
from tests.platform.api.api_support import _usage_record
from turnstile_core.domain.application_access import (
    GatewayApplicationAvatarUpdate,
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
    GatewayApplicationSubscriptionCreate,
    GatewayApplicationSubscriptionProvisionSpec,
    UsageApplicationAttribution,
    application_id_for,
    application_subscription_id_for,
    decode_application_avatar_data_url,
)
from turnstile_core.integrations.apim_control_plane import (
    AzureApimPublisherClient,
    AzureApimSubscriptionKeyClient,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository_applications import _json_value
from turnstile_core.services.application_access import ApplicationAccessService


def test_application_avatar_accepts_bounded_supported_image() -> None:
    value = GatewayApplicationAvatarUpdate(
        avatar_data_url="data:image/png;base64,iVBORw0KGgo="
    )

    assert decode_application_avatar_data_url(value.avatar_data_url or "") == (
        "image/png",
        b"\x89PNG\r\n\x1a\n",
    )


def test_application_avatar_rejects_mismatched_image_type() -> None:
    with pytest.raises(ValueError, match="declared media type"):
        GatewayApplicationAvatarUpdate(
            avatar_data_url="data:image/jpeg;base64,iVBORw0KGgo="
        )


def _discovery(
    *subscription_ids: str,
    discovered_at: datetime | None = None,
    system_ids: set[str] | None = None,
) -> GatewayApplicationDiscovery:
    system_ids = system_ids or {
        "master",
        "turnstile-dashboard",
        "turnstile-publisher-probe",
    }
    return GatewayApplicationDiscovery(
        gateway_profile_id=APIM_ID,
        discovered_at=discovered_at or datetime(2026, 8, 26, 2, tzinfo=UTC),
        items=[
            GatewayApplicationDiscoveryItem(
                apim_subscription_id=subscription_id,
                display_name=subscription_id.replace("-", " ").title(),
                state="active",
                scope_type="product",
                scope_id="finops-applications",
                scope_exists=True,
                application_type="system" if subscription_id in system_ids else "service",
                system_managed=subscription_id in system_ids,
            )
            for subscription_id in subscription_ids
        ],
    )


def test_owner_subscription_create_normalizes_human_fields() -> None:
    request = GatewayApplicationSubscriptionCreate(
        subscription_id="invoice-assistant",
        display_name="  Invoice Assistant  ",
        description="   ",
        application_type="agent",
    )

    assert request.display_name == "Invoice Assistant"
    assert request.description is None
    assert request.application_type == "agent"


def test_agent_provision_persists_specialized_application_type() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    spec = GatewayApplicationSubscriptionProvisionSpec(
        application_id=application_id_for(APIM_ID, "invoice-assistant"),
        application_subscription_id=application_subscription_id_for(
            APIM_ID, "invoice-assistant"
        ),
        apim_subscription_id="invoice-assistant",
        slug="invoice-assistant",
        display_name="Invoice Assistant",
        application_type="agent",
        scope_id="finops-applications",
    )

    application = service.provision(APIM_ID, spec, "owner@example.com")

    assert application["application_type"] == "agent"
    assert repository.gateway_application_ledger_mappings()[0][
        "application_slug"
    ] == "invoice-assistant"

    service.sync_discovery(_discovery("invoice-assistant"), "worker")

    assert service.applications().items[0].application_type == "agent"


def test_application_discovery_is_idempotent_and_preserves_budget() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)

    first = service.sync_discovery(
        _discovery("turnstile-dashboard", "master"), "worker-a"
    )
    dashboard_id = UUID(str(first[0]["id"]))
    first_updated_at = first[0]["updated_at"]
    repository.gateway_application_budgets[
        (datetime(2026, 8, 1).date(), dashboard_id)
    ]["token_limit"] = 750_000

    second = service.sync_discovery(
        _discovery("turnstile-dashboard", "master"), "worker-b"
    )

    assert second[0]["updated_at"] == first_updated_at
    assert len(repository.gateway_application_audit) == 2
    assert repository.gateway_application_budgets[
        (datetime(2026, 8, 1).date(), dashboard_id)
    ]["token_limit"] == 750_000
    master = next(item for item in second if item["slug"] == "master")
    master_id = UUID(str(master["id"]))
    assert master["system_managed"] is True
    assert repository.gateway_application_budgets[
        (datetime(2026, 8, 1).date(), master_id)
    ]["enforce"] is False


def test_custom_deployed_subscription_ids_are_classified_as_bicep() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(
        repository,
        sync_available=True,
        dashboard_subscription_id="unit-dashboard",
        probe_subscription_id="unit-publisher-probe",
    )

    service.sync_discovery(
        _discovery(
            "unit-dashboard",
            "unit-publisher-probe",
            "unit-application",
            system_ids={"unit-dashboard", "unit-publisher-probe"},
        ),
        "worker",
    )

    sources = {
        str(item["apim_subscription_id"]): item["source"]
        for item in repository.gateway_application_subscriptions
    }
    assert sources == {
        "unit-dashboard": "bicep",
        "unit-publisher-probe": "bicep",
        "unit-application": "discovered",
    }


def test_missing_subscription_is_marked_cancelled_and_stale() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    service.sync_discovery(_discovery("outline-assistant"), "worker-a")

    service.sync_discovery(_discovery(), "worker-b")

    subscription = repository.gateway_application_subscriptions[0]
    assert subscription["state"] == "cancelled"
    assert subscription["scope_exists"] is False
    application = service.applications().items[0]
    assert application.stale_subscription_count == 1
    assert application.active_subscription_count == 0


def test_usage_application_attribution_is_immutable_and_drives_ledger() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    moment = datetime.now(UTC)
    application = service.sync_discovery(
        _discovery("outline-assistant", discovered_at=moment), "worker-a"
    )[0]
    application_id = UUID(str(application["id"]))
    application_name = str(application["display_name"])
    subscription = repository.gateway_application_subscriptions[0]
    attribution = UsageApplicationAttribution(
        application_id=application_id,
        application_subscription_id=subscription["id"],
        application_name_snapshot=application_name,
        apim_subscription_id="outline-assistant",
        actor_type="service",
        actor_id="service:outline-assistant",
    )
    record = _usage_record(
        "application-usage", "Microsoft Foundry via APIM", 125
    ).model_copy(update={"ts": moment})

    repository.write_token_usage(record, attribution)
    repository.write_token_usage(
        record,
        attribution.model_copy(update={"application_admission": "confirmed"}),
    )

    stored = repository.usage_application_attributions[record.id]
    assert stored.application_admission == "confirmed"
    view = service.applications().items[0]
    assert view.usage.total_tokens == 125
    assert view.budget is not None
    assert view.budget.remaining_tokens == 99_875
    period_start = moment.date().replace(day=1)
    period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    assert repository.gateway_application_ledger_snapshot(
        period_start, period_end
    )[0]["confirmed_tokens"] == 125

    with pytest.raises(ValueError, match="immutable"):
        repository.write_token_usage(
            record,
            attribution.model_copy(update={"actor_id": "service:other"}),
        )


def test_application_detail_aggregates_people_and_service_users() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    moment = datetime.now(UTC)
    application = service.sync_discovery(
        _discovery("outline-assistant", discovered_at=moment), "worker-a"
    )[0]
    application_id = UUID(str(application["id"]))
    subscription = repository.gateway_application_subscriptions[0]
    person = UsageApplicationAttribution(
        application_id=application_id,
        application_subscription_id=subscription["id"],
        application_name_snapshot=str(application["display_name"]),
        apim_subscription_id="outline-assistant",
        actor_type="person",
        actor_id="alice@contoso.com",
        person_id="person-alice",
    )
    service_actor = person.model_copy(
        update={
            "actor_type": "service",
            "actor_id": "service:outline-assistant",
            "person_id": None,
        }
    )
    for usage_id, tokens, status_code, attribution in (
        ("alice-1", 80, 200, person),
        ("alice-2", 20, 403, person),
        ("service-1", 30, 200, service_actor),
    ):
        record = _usage_record(
            usage_id, "Microsoft Foundry via APIM", tokens, status_code=status_code
        ).model_copy(
            update={
                "ts": moment,
                "user": "Alice" if attribution.actor_type == "person" else "Service",
                "user_id": attribution.person_id or "unattributed",
            }
        )
        repository.write_token_usage(record, attribution)

    detail = service.application(application_id)

    assert detail.user_count == 2
    assert [item.user_id for item in detail.users] == [
        "person-alice",
        "service:outline-assistant",
    ]
    alice = detail.users[0]
    assert alice.display_name == "Alice"
    assert alice.actor_type == "person"
    assert alice.request_count == 2
    assert alice.denied_request_count == 1
    assert alice.total_tokens == 100
    assert detail.users[1].actor_type == "service"


def test_application_audit_state_is_json_serializable() -> None:
    value = {
        "id": uuid4(),
        "at": datetime(2026, 8, 26, 2, tzinfo=UTC),
        "nested": {"ids": [uuid4()]},
    }

    encoded = _json_value(value)

    assert json.loads(json.dumps(encoded))["at"] == "2026-08-26T02:00:00Z"


def test_apim_subscription_discovery_uses_list_without_secrets() -> None:
    requests: list[httpx.Request] = []
    root = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-finops/providers/Microsoft.ApiManagement/"
        "service/apim-finops"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/apis"):
            return httpx.Response(
                200,
                json={"value": [{"id": f"{root}/apis/turnstile-llm"}]},
            )
        if request.url.path.endswith("/products"):
            return httpx.Response(
                200,
                json={"value": [{"id": f"{root}/products/finops-applications"}]},
            )
        if request.url.path.endswith("/subscriptions"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": f"{root}/subscriptions/outline-assistant",
                            "properties": {
                                "displayName": "Outline Assistant",
                                "scope": f"{root}/products/finops-applications",
                                "state": "active",
                                "primaryKey": "must-be-ignored",
                                "secondaryKey": "must-be-ignored",
                            },
                        },
                        {
                            "id": f"{root}/subscriptions/master",
                            "properties": {
                                "displayName": "Built-in all-access",
                                "scope": f"{root}/apis",
                                "state": "active",
                            },
                        },
                        {
                            "id": f"{root}/subscriptions/unit-dashboard",
                            "properties": {
                                "displayName": "Unit dashboard",
                                "scope": f"{root}/products/finops-applications",
                                "state": "active",
                            },
                        },
                        {
                            "id": f"{root}/subscriptions/unit-publisher-probe",
                            "properties": {
                                "displayName": "Unit publisher probe",
                                "scope": f"{root}/products/finops-applications",
                                "state": "active",
                            },
                        },
                    ]
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    settings = publisher_settings().model_copy(
        update={
            "apim_dashboard_subscription_id": "unit-dashboard",
            "apim_probe_subscription_id": "unit-publisher-probe",
        }
    )
    settings.apim_subscription_agent_map = {
        "outline-assistant": {
            "id": "agent-outline-assistant",
            "name": "Outline Assistant",
        }
    }
    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),  # type: ignore[arg-type]
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    discovered = client.discover_gateway_applications(APIM_ID)

    assert {item.apim_subscription_id for item in discovered.items} == {
        "master",
        "outline-assistant",
        "unit-dashboard",
        "unit-publisher-probe",
    }
    master = next(
        item for item in discovered.items if item.apim_subscription_id == "master"
    )
    assistant = next(
        item
        for item in discovered.items
        if item.apim_subscription_id == "outline-assistant"
    )
    assert master.scope_type == "service"
    assert assistant.application_type == "agent"
    assert {
        item.apim_subscription_id
        for item in discovered.items
        if item.system_managed and item.application_type == "system"
    } == {"master", "unit-dashboard", "unit-publisher-probe"}
    assert all(request.method == "GET" for request in requests)
    assert all("listSecrets" not in str(request.url) for request in requests)
    assert "must-be-ignored" not in discovered.model_dump_json()


def test_apim_subscription_provision_uses_put_without_list_secrets() -> None:
    requests: list[httpx.Request] = []
    root = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-finops/providers/Microsoft.ApiManagement/"
        "service/apim-finops"
    )
    spec = GatewayApplicationSubscriptionProvisionSpec(
        application_id=uuid4(),
        application_subscription_id=uuid4(),
        apim_subscription_id="owner-created-app",
        slug="owner-created-app",
        display_name="Owner Created App",
        scope_id="finops-ai-consumers",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/products/finops-ai-consumers"):
            return httpx.Response(200, json={"id": f"{root}/products/finops-ai-consumers"})
        if request.url.path.endswith("/subscriptions/owner-created-app"):
            if request.method == "GET":
                return httpx.Response(404)
            body = json.loads(request.content)
            assert body["properties"]["primaryKey"] == "primary-test-key"
            assert body["properties"]["secondaryKey"] == "secondary-test-key"
            return httpx.Response(
                201,
                json={
                    "id": f"{root}/subscriptions/owner-created-app",
                    "properties": {
                        "displayName": "Owner Created App",
                        "scope": f"{root}/products/finops-ai-consumers",
                        "state": "active",
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),  # type: ignore[arg-type]
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.ensure_application_subscription(
        spec, "primary-test-key", "secondary-test-key"
    )

    assert [request.method for request in requests] == ["GET", "GET", "PUT"]
    assert all("listSecrets" not in str(request.url) for request in requests)


def test_apim_subscription_keys_can_be_revealed_and_rotated() -> None:
    requests: list[httpx.Request] = []
    primary_key = "primary-before-rotation"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_key
        requests.append(request)
        assert request.method == "POST"
        assert request.url.params["api-version"] == "2024-05-01"
        if request.url.path.endswith("/regeneratePrimaryKey"):
            primary_key = "primary-after-rotation"
            return httpx.Response(204)
        if request.url.path.endswith("/listSecrets"):
            return httpx.Response(
                200,
                json={
                    "primaryKey": primary_key,
                    "secondaryKey": "secondary-key",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = AzureApimSubscriptionKeyClient(
        publisher_settings(),
        StubTokenProvider(),  # type: ignore[arg-type]
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.application_subscription_keys("owner app") == (
        "primary-before-rotation",
        "secondary-key",
    )
    client.regenerate_application_subscription_key("owner app", "primary")
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "listSecrets",
        "regeneratePrimaryKey",
    ]
    assert all("owner%20app" in str(request.url) for request in requests)
