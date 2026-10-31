from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet

from backend.api import (
    app,
    application_access_service,
    control_plane_service,
)
from backend.http.dependencies import get_repository
from backend.http.session import SessionIdentity, require_authenticated_session
from tests.backend.model_platform.control_plane_support import APIM_ID
from tests.platform.api.api_support import _usage_record, client
from turnstile_core.domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
    GatewayApplicationLedgerState,
    GatewayApplicationSubscriptionCreate,
    UsageApplicationAttribution,
)
from turnstile_core.integrations.apim_control_plane_contract import ApimSubscriptionKeyClient
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.application_access import ApplicationAccessService
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    ControlPlaneNotFoundError,
    ControlPlaneUnavailableError,
    GatewayControlPlaneService,
)

pytest_plugins = ("tests.platform.api.api_fixtures",)


def _seed(
    repository: InMemoryRepository,
    key_client: ApimSubscriptionKeyClient | None = None,
    *,
    discovered_at: datetime | None = None,
) -> ApplicationAccessService:
    service = ApplicationAccessService(
        repository,
        sync_available=True,
        key_management_available=key_client is not None,
        key_client=key_client,
    )
    service.sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=APIM_ID,
            discovered_at=discovered_at or datetime(2026, 8, 26, 2, tzinfo=UTC),
            items=[
                GatewayApplicationDiscoveryItem(
                    apim_subscription_id="outline-assistant",
                    display_name="Outline Assistant",
                    state="active",
                    scope_type="product",
                    scope_id="finops-applications",
                    scope_exists=True,
                    application_type="service",
                    system_managed=False,
                )
            ],
        ),
        "application-sync-worker",
    )
    return service


def test_application_budget_api_distinguishes_missing_zero_and_upper_bound() -> None:
    repository = InMemoryRepository()
    moment = datetime.now(UTC)
    service = _seed(repository, discovered_at=moment)
    application_id = repository.gateway_applications[0]["id"]
    app.dependency_overrides[application_access_service] = lambda: service
    try:
        url = f"/api/v1/application-access/applications/{application_id}"
        response = client.get(url)
        assert response.status_code == 200
        unknown = response.json()["budget"]
        assert unknown["pending_reserved_tokens"] is None
        assert unknown["available_tokens"] is None
        assert unknown["ledger_snapshot_at"] is None
        state = GatewayApplicationLedgerState(
            period_start=moment.date().replace(day=1),
            application_id=application_id,
            token_limit=100_000,
            confirmed_tokens=100,
            pending_reserved_tokens=200,
            pending_reservation_count=1,
            stale_reservation_count=1,
            oldest_reservation_at=moment - timedelta(hours=2),
            available_tokens=99_700,
            snapshot_at=moment,
        )
        sidecar = {
            "period_start": state.period_start, "application_id": application_id,
            "finalized_upper_bound_tokens": 300, "finalized_upper_bound_count": 1,
            "snapshot_at": moment,
        }
        repository.save_gateway_application_ledger_snapshot([state], [sidecar])
        observed = client.get(url).json()["budget"]
        assert observed["pending_reserved_tokens"] == 200
        assert observed["finalized_upper_bound_tokens"] == 300
        assert observed["available_tokens"] == 99_400
        repository.save_gateway_application_ledger_snapshot(
            [
                state.model_copy(
                    update={
                        "pending_reserved_tokens": 0,
                        "pending_reservation_count": 0,
                        "stale_reservation_count": 0,
                        "oldest_reservation_at": None,
                        "finalized_upper_bound_tokens": 0,
                        "finalized_upper_bound_count": 0,
                        "available_tokens": 99_900,
                        "snapshot_at": moment + timedelta(minutes=5),
                    }
                )
            ],
            [{
                **sidecar, "snapshot_at": moment + timedelta(minutes=5),
                "finalized_upper_bound_tokens": 0, "finalized_upper_bound_count": 0,
            }],
        )
        repository.save_gateway_application_ledger_snapshot([state], [sidecar])
        zero = client.get(url).json()["budget"]
        assert zero["pending_reserved_tokens"] == 0
        assert zero["finalized_upper_bound_tokens"] == 0
        assert zero["available_tokens"] == 99_900
    finally:
        app.dependency_overrides.pop(application_access_service, None)


class StubSubscriptionKeyClient(ApimSubscriptionKeyClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def application_subscription_keys(self, apim_subscription_id: str) -> tuple[str, str]:
        self.calls.append(("reveal", apim_subscription_id, None))
        return "primary-secret", "secondary-secret"

    def regenerate_application_subscription_key(
        self,
        apim_subscription_id: str,
        key_kind: Literal["primary", "secondary"],
    ) -> None:
        self.calls.append(("rotate", apim_subscription_id, key_kind))


class ForbiddenSubscriptionKeyClient(StubSubscriptionKeyClient):
    def application_subscription_keys(self, apim_subscription_id: str) -> tuple[str, str]:
        request = httpx.Request("POST", "https://management.azure.com/listSecrets")
        response = httpx.Response(
            403,
            request=request,
            json={"error": {"message": "primary-secret-must-not-leak"}},
        )
        raise httpx.HTTPStatusError(
            "forbidden",
            request=request,
            response=response,
        )


def test_application_access_list_detail_and_owner_sync() -> None:
    repository = InMemoryRepository()
    moment = datetime.now(UTC)
    read_service = _seed(repository, discovered_at=moment)
    application = repository.gateway_applications[0]
    subscription = repository.gateway_application_subscriptions[0]
    repository.write_token_usage(
        _usage_record("outline-user", "Microsoft Foundry via APIM", 42).model_copy(
            update={
                "ts": moment,
                "user": "Alice",
                "user_id": "person-alice",
            }
        ),
        UsageApplicationAttribution(
            application_id=application["id"],
            application_subscription_id=subscription["id"],
            application_name_snapshot=str(application["display_name"]),
            apim_subscription_id="outline-assistant",
            actor_type="person",
            actor_id="alice@contoso.com",
            person_id="person-alice",
        ),
    )
    operation_service = GatewayControlPlaneService(
        repository,
        CredentialCipher(Fernet.generate_key()),
        release_worker_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[control_plane_service] = lambda: operation_service
    try:
        response = client.get("/api/v1/application-access/applications")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["active"] == 1
        assert payload["sync_available"] is True
        assert payload["provisioning_available"] is False
        assert payload["provisioning_unavailable_reason"] is None
        assert payload["key_management_available"] is False
        assert payload["items"][0]["display_name"] == "Outline Assistant"
        assert payload["items"][0]["governance_write_available"] is True
        assert payload["items"][0]["budget"]["token_limit"] == 100_000
        assert "primary_key" not in response.text
        assert "secondary_key" not in response.text

        application_id = payload["items"][0]["id"]
        detail = client.get(f"/api/v1/application-access/applications/{application_id}")
        assert detail.status_code == 200
        assert detail.json()["subscriptions"][0]["apim_subscription_id"] == ("outline-assistant")
        assert detail.json()["user_count"] == 1
        assert detail.json()["users"][0]["display_name"] == "Alice"
        assert detail.json()["users"][0]["request_count"] == 1

        accepted = client.post(f"/api/v1/application-access/gateways/{APIM_ID}/sync")
        assert accepted.status_code == 202
        operation = accepted.json()["operation"]
        assert operation["operation_kind"] == "application_sync"
        assert operation["status"] == "queued"
        assert accepted.json()["status_url"].endswith(operation["id"])
    finally:
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(control_plane_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_application_usage_activity_is_server_scoped_and_bucketed() -> None:
    repository = InMemoryRepository()
    read_service = _seed(repository)
    application = repository.gateway_applications[0]
    subscription = repository.gateway_application_subscriptions[0]
    attribution = UsageApplicationAttribution(
        application_id=application["id"],
        application_subscription_id=subscription["id"],
        application_name_snapshot=str(application["display_name"]),
        apim_subscription_id="outline-assistant",
        actor_type="service",
        actor_id="service:outline-assistant",
    )
    for usage_id, timestamp, tokens in (
        ("outline-activity-a", datetime(2026, 8, 20, 8, tzinfo=UTC), 40),
        ("outline-activity-b", datetime(2026, 8, 21, 8, tzinfo=UTC), 60),
    ):
        repository.write_token_usage(
            _usage_record(usage_id, "Microsoft Foundry via APIM", tokens).model_copy(
                update={"ts": timestamp}
            ),
            attribution,
        )
    repository.write_token_usage(
        _usage_record("other-activity", "Microsoft Foundry via APIM", 500).model_copy(
            update={"ts": datetime(2026, 8, 20, 9, tzinfo=UTC)}
        )
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    try:
        response = client.get(
            f"/api/v1/application-access/applications/{application['id']}/usage-activity",
            params={
                "from": "2026-08-20T00:00:00Z",
                "to": "2026-08-22T00:00:00Z",
                "interval": "day",
                "timezone": "UTC",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["interval"] == "day"
        assert payload["group_by"] == "none"
        assert len(payload["points"]) == 2
        assert [point["totals"]["total_tokens"] for point in payload["points"]] == [
            40,
            60,
        ]
        assert [point["totals"]["calls"] for point in payload["points"]] == [1, 1]
    finally:
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_application_sync_requires_owner() -> None:
    repository = InMemoryRepository()
    member = SessionIdentity(
        id="member-id",
        email="member@contoso.com",
        name="Member",
        role="member",
        method="entra",
        session_expires_at=datetime(2026, 8, 27, tzinfo=UTC),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[require_authenticated_session] = lambda: member
    try:
        response = client.post(f"/api/v1/application-access/gateways/{APIM_ID}/sync")
        assert response.status_code == 403
        assert response.json()["detail"] == "Owner role is required"
        provision = client.post(
            f"/api/v1/application-access/gateways/{APIM_ID}/subscriptions",
            json={
                "subscription_id": "member-app",
                "display_name": "Member App",
            },
        )
        assert provision.status_code == 403
        avatar = client.put(
            "/api/v1/application-access/applications/00000000-0000-0000-0000-000000000001/avatar",
            json={"avatar_data_url": None},
        )
        assert avatar.status_code == 403
        budget = client.put(
            "/api/v1/application-access/applications/00000000-0000-0000-0000-000000000001/budget",
            json={
                "token_limit": 200_000,
                "tokens_per_minute": 10_000,
                "enforce": True,
                "warning_threshold_percent": 75,
            },
        )
        assert budget.status_code == 403
        reveal = client.post(
            "/api/v1/application-access/applications/"
            "00000000-0000-0000-0000-000000000001/subscriptions/"
            "00000000-0000-0000-0000-000000000002/keys/primary/reveal"
        )
        assert reveal.status_code == 403
        rotate = client.post(
            "/api/v1/application-access/applications/"
            "00000000-0000-0000-0000-000000000001/subscriptions/"
            "00000000-0000-0000-0000-000000000002/keys/primary/rotate",
            json={"confirmation": "member-app"},
        )
        assert rotate.status_code == 403
    finally:
        app.dependency_overrides.pop(require_authenticated_session, None)
        app.dependency_overrides.pop(get_repository, None)


def test_owner_can_update_application_budget_and_model_access() -> None:
    repository = InMemoryRepository()
    read_service = _seed(repository)
    application_id = repository.gateway_applications[0]["id"]
    model_id = UUID("10000000-0000-4000-8000-000000000001")
    repository.models.append({"id": model_id, "enabled": True})
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    try:
        budget = client.put(
            f"/api/v1/application-access/applications/{application_id}/budget",
            json={
                "token_limit": 250_000,
                "tokens_per_minute": 12_000,
                "enforce": False,
                "warning_threshold_percent": 70,
            },
        )
        assert budget.status_code == 200
        assert budget.json()["budget"]["token_limit"] == 250_000
        assert budget.json()["budget"]["enforce"] is False

        access = client.put(
            f"/api/v1/application-access/applications/{application_id}/model-access",
            json={"mode": "restricted", "model_ids": [str(model_id)]},
        )
        assert access.status_code == 200
        assert access.json()["model_policy_configured"] is True
        assert access.json()["allowed_model_ids"] == [str(model_id)]
        operations = [item["operation"] for item in repository.gateway_application_audit]
        assert "budget_updated" in operations
        assert "models_updated" in operations
    finally:
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (ControlPlaneConflictError("Cannot switch to USD budget"), 409),
        (ControlPlaneUnavailableError("USD budgets require a current scope"), 503),
        (ControlPlaneNotFoundError("Gateway application was not found"), 404),
    ],
)
def test_application_budget_update_maps_service_refusals(
    error: Exception, status_code: int,
) -> None:
    repository = InMemoryRepository()
    read_service = _seed(repository)
    application_id = repository.gateway_applications[0]["id"]
    app.dependency_overrides[application_access_service] = lambda: read_service
    try:
        with patch.object(read_service, "update_application_budget", side_effect=error):
            response = client.put(
                f"/api/v1/application-access/applications/{application_id}/budget",
                json={
                    "budget_mode": "usd",
                    "usd_limit": "10.00",
                    "tokens_per_minute": 12_000,
                    "enforce": True,
                    "warning_threshold_percent": 70,
                },
            )
        assert response.status_code == status_code
        assert response.json()["detail"] == str(error)
    finally:
        app.dependency_overrides.pop(application_access_service, None)


def test_owner_can_reveal_and_rotate_application_subscription_keys() -> None:
    repository = InMemoryRepository()
    key_client = StubSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    application_id = repository.gateway_applications[0]["id"]
    subscription = repository.gateway_application_subscriptions[0]
    subscription_id = subscription["id"]
    base = (
        f"/api/v1/application-access/applications/{application_id}"
        f"/subscriptions/{subscription_id}/keys"
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    try:
        revealed = client.post(f"{base}/primary/reveal")
        assert revealed.status_code == 200
        assert revealed.json() == {
            "key_kind": "primary",
            "value": "primary-secret",
        }
        assert revealed.headers["cache-control"] == "no-store"
        assert revealed.headers["pragma"] == "no-cache"

        rejected = client.post(
            f"{base}/secondary/rotate",
            json={"confirmation": "wrong-subscription"},
        )
        assert rejected.status_code == 422
        rotated = client.post(
            f"{base}/secondary/rotate",
            json={"confirmation": subscription["apim_subscription_id"]},
        )
        assert rotated.status_code == 204
        assert rotated.content == b""
        assert rotated.headers["cache-control"] == "no-store"
        assert key_client.calls == [
            ("reveal", "outline-assistant", None),
            ("rotate", "outline-assistant", "secondary"),
        ]
        repository_state = repr(repository.__dict__)
        assert "primary-secret" not in repository_state
        assert "secondary-secret" not in repository_state
    finally:
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_system_managed_application_keys_are_not_exposed() -> None:
    repository = InMemoryRepository()
    key_client = StubSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    repository.gateway_applications[0]["system_managed"] = True
    application_id = repository.gateway_applications[0]["id"]
    subscription_id = repository.gateway_application_subscriptions[0]["id"]
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    try:
        response = client.post(
            f"/api/v1/application-access/applications/{application_id}"
            f"/subscriptions/{subscription_id}/keys/primary/reveal"
        )
        assert response.status_code == 409
        assert response.json()["detail"] == (
            "System-managed subscription keys cannot be managed here"
        )
        assert key_client.calls == []
    finally:
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_apim_key_error_body_is_sanitized() -> None:
    repository = InMemoryRepository()
    read_service = _seed(repository, ForbiddenSubscriptionKeyClient())
    application_id = repository.gateway_applications[0]["id"]
    subscription_id = repository.gateway_application_subscriptions[0]["id"]
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    try:
        response = client.post(
            f"/api/v1/application-access/applications/{application_id}"
            f"/subscriptions/{subscription_id}/keys/primary/reveal"
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "APIM key management is not authorized"
        assert "primary-secret-must-not-leak" not in response.text
    finally:
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_owner_can_update_read_and_remove_application_avatar() -> None:
    repository = InMemoryRepository()
    read_service = _seed(repository)
    application_id = repository.gateway_applications[0]["id"]
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    try:
        updated = client.put(
            f"/api/v1/application-access/applications/{application_id}/avatar",
            json={"avatar_data_url": "data:image/png;base64,iVBORw0KGgo="},
        )
        assert updated.status_code == 200
        avatar_url = updated.json()["avatar_url"]
        assert avatar_url.startswith(
            f"/api/v1/application-access/applications/{application_id}/avatar?v="
        )

        inventory = client.get("/api/v1/application-access/applications")
        assert inventory.json()["items"][0]["avatar_url"] == avatar_url
        image = client.get(avatar_url)
        assert image.status_code == 200
        assert image.content == b"\x89PNG\r\n\x1a\n"
        assert image.headers["content-type"] == "image/png"
        assert image.headers["x-content-type-options"] == "nosniff"
        assert "immutable" in image.headers["cache-control"]
        assert "image_bytes" not in json.dumps(repository.gateway_application_audit, default=str)

        removed = client.put(
            f"/api/v1/application-access/applications/{application_id}/avatar",
            json={"avatar_data_url": None},
        )
        assert removed.status_code == 200
        assert removed.json() == {"avatar_url": None, "updated_at": None}
        assert client.get(avatar_url).status_code == 404
    finally:
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_owner_subscription_provision_returns_key_once() -> None:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    operation_service = GatewayControlPlaneService(
        repository,
        cipher,
        release_worker_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[control_plane_service] = lambda: operation_service
    try:
        response = client.post(
            f"/api/v1/application-access/gateways/{APIM_ID}/subscriptions",
            json=GatewayApplicationSubscriptionCreate(
                subscription_id="owner-api-app",
                display_name="Owner API App",
            ).model_dump(),
        )
        assert response.status_code == 202
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        payload = response.json()
        primary_key = payload["primary_key"]
        assert primary_key
        assert payload["operation"]["operation_kind"] == "application_provision"
        assert primary_key not in json.dumps(payload["operation"])

        operation = client.get(payload["status_url"])
        assert operation.status_code == 200
        assert "primary_key" not in operation.text
        assert primary_key not in operation.text
        ciphertext = repository.gateway_release_operation_secret(UUID(payload["operation"]["id"]))
        assert ciphertext is not None
        assert primary_key.encode() not in ciphertext
    finally:
        app.dependency_overrides.pop(control_plane_service, None)
        app.dependency_overrides.pop(get_repository, None)
