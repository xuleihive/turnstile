from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import httpx
from cryptography.fernet import Fernet

from backend.api import (
    app,
    application_access_service,
    control_plane_service,
)
from backend.http.dependencies import get_repository
from backend.http.security_audit_support import get_optional_security_audit_service
from backend.http.session import SessionIdentity, require_authenticated_session
from backend.http.user_management import get_access_control_service
from tests.backend.model_platform.control_plane_support import APIM_ID
from tests.platform.api.api_support import MEMBER_SESSION, _usage_record, client
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
from turnstile_core.services.control_plane import GatewayControlPlaneService

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
            finalized_upper_bound_tokens=300,
            finalized_upper_bound_count=1,
            oldest_reservation_at=moment - timedelta(hours=2),
            available_tokens=99_400,
            snapshot_at=moment,
        )
        repository.save_gateway_application_ledger_states([state])
        observed = client.get(url).json()["budget"]
        assert observed["pending_reserved_tokens"] == 200
        assert observed["finalized_upper_bound_tokens"] == 300
        assert observed["available_tokens"] == 99_400
        repository.save_gateway_application_ledger_states(
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
            ]
        )
        repository.save_gateway_application_ledger_states([state])
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

    def cancel_application_subscription(self, apim_subscription_id: str) -> None:
        self.calls.append(("cancel", apim_subscription_id, None))


class ConcurrentSubscriptionKeyClient(StubSubscriptionKeyClient):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def application_subscription_keys(self, apim_subscription_id: str) -> tuple[str, str]:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        time.sleep(0.01)
        with self._lock:
            self._active -= 1
            self.calls.append(("reveal", apim_subscription_id, None))
        return f"primary-{apim_subscription_id}", f"secondary-{apim_subscription_id}"


class StubAccessControlService:
    def get_user(self, user_id: UUID) -> dict[str, object]:
        user_id_text = str(user_id)
        if user_id_text == "00000000-0000-4000-8000-000000000001":
            return {
                "id": user_id,
                "email": "owner@contoso.com",
                "display_name": "Owner",
                "role": "owner",
                "enabled": True,
                "tenant_id": UUID("10000000-0000-4000-8000-000000000001"),
                "organization_unit_id": None,
            }
        if user_id_text == "00000000-0000-4000-8000-000000000002":
            return {
                "id": user_id,
                "email": "member@contoso.com",
                "display_name": "Member",
                "role": "member",
                "enabled": True,
                "tenant_id": UUID("10000000-0000-4000-8000-000000000001"),
                "organization_unit_id": None,
            }
        raise RuntimeError("User not found")


class StubSecurityAuditService:
    def __init__(self) -> None:
        self.events: list[object] = []

    def record(self, event: object, connection: object | None = None) -> None:
        del connection
        self.events.append(event)


class FailingSecurityAuditService(StubSecurityAuditService):
    def __init__(self, failing_action: str) -> None:
        super().__init__()
        self.failing_action = failing_action

    def record(self, event: object, connection: object | None = None) -> None:
        if getattr(event, "action", None) == self.failing_action:
            raise RuntimeError("audit storage unavailable")
        super().record(event, connection)


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
        permissions=frozenset(),
        method="entra",
        session_expires_at=datetime(2026, 8, 27, tzinfo=UTC),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[require_authenticated_session] = lambda: member
    try:
        response = client.post(f"/api/v1/application-access/gateways/{APIM_ID}/sync")
        assert response.status_code == 403
        assert response.json()["detail"] == "Permission denied"
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


def test_owner_can_reveal_and_rotate_application_subscription_keys() -> None:
    repository = InMemoryRepository()
    key_client = StubSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    audit_service = StubSecurityAuditService()
    application_id = repository.gateway_applications[0]["id"]
    subscription = repository.gateway_application_subscriptions[0]
    subscription_id = subscription["id"]
    base = (
        f"/api/v1/application-access/applications/{application_id}"
        f"/subscriptions/{subscription_id}/keys"
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
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
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_reveal_does_not_deliver_key_when_success_audit_cannot_be_persisted() -> None:
    repository = InMemoryRepository()
    key_client = StubSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    audit_service = FailingSecurityAuditService("key.reveal")
    application_id = repository.gateway_applications[0]["id"]
    subscription_id = repository.gateway_application_subscriptions[0]["id"]
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    try:
        response = client.post(
            f"/api/v1/application-access/applications/{application_id}"
            f"/subscriptions/{subscription_id}/keys/primary/reveal"
        )

        assert response.status_code == 503
        assert "primary-secret" not in response.text
        assert key_client.calls == [("reveal", "outline-assistant", None)]
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(application_access_service, None)


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
    audit_service = StubSecurityAuditService()
    application_id = repository.gateway_applications[0]["id"]
    subscription_id = repository.gateway_application_subscriptions[0]["id"]
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    try:
        response = client.post(
            f"/api/v1/application-access/applications/{application_id}"
            f"/subscriptions/{subscription_id}/keys/primary/reveal"
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "APIM key management is not authorized"
        assert "primary-secret-must-not-leak" not in response.text
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_member_lists_and_reveals_only_owned_apim_subscriptions() -> None:
    repository = InMemoryRepository()
    key_client = StubSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    member_id = UUID("00000000-0000-4000-8000-000000000002")
    repository.gateway_applications[0].update(
        owner_user_id=member_id,
        owner_id="member@contoso.com",
    )
    audit_service = StubSecurityAuditService()
    application_id = repository.gateway_applications[0]["id"]
    subscription_id = repository.gateway_application_subscriptions[0]["id"]
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_access_control_service] = StubAccessControlService
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        listed = client.get("/api/v1/application-access/me/subscriptions")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [
            str(subscription_id)
        ]
        applications = client.get("/api/v1/application-access/applications")
        assert applications.status_code == 200
        assert [item["id"] for item in applications.json()["items"]] == [
            str(application_id)
        ]
        detail = client.get(
            f"/api/v1/application-access/applications/{application_id}"
        )
        assert detail.status_code == 200
        revealed = client.post(
            f"/api/v1/application-access/applications/{application_id}"
            f"/subscriptions/{subscription_id}/keys/primary/reveal"
        )
        assert revealed.status_code == 200
        assert revealed.json()["value"] == "primary-secret"
        assert revealed.headers["cache-control"] == "no-store"
        assert key_client.calls == [("reveal", "outline-assistant", None)]
    finally:
        client.cookies.set("turnstile_session", "test-owner-session")
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(get_access_control_service, None)
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_member_cannot_read_another_users_subscription_directory_or_key() -> None:
    repository = InMemoryRepository()
    key_client = StubSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    owner_id = UUID("00000000-0000-4000-8000-000000000001")
    repository.gateway_applications[0].update(
        owner_user_id=owner_id,
        owner_id="owner@contoso.com",
    )
    application_id = repository.gateway_applications[0]["id"]
    subscription_id = repository.gateway_application_subscriptions[0]["id"]
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_access_control_service] = StubAccessControlService
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        listed = client.get(
            f"/api/v1/admin/users/{owner_id}/subscriptions"
        )
        assert listed.status_code == 403
        revealed = client.post(
            f"/api/v1/application-access/applications/{application_id}"
            f"/subscriptions/{subscription_id}/keys/primary/reveal"
        )
        assert revealed.status_code == 404
        cancelled = client.post(
            f"/api/v1/application-access/applications/{application_id}"
            f"/subscriptions/{subscription_id}/cancel",
            json={"confirmation": "outline-assistant"},
        )
        assert cancelled.status_code == 404
        assert key_client.calls == []
    finally:
        client.cookies.set("turnstile_session", "test-owner-session")
        app.dependency_overrides.pop(get_access_control_service, None)
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_member_cannot_list_or_read_another_users_application_data() -> None:
    repository = InMemoryRepository()
    read_service = _seed(repository)
    audit_service = StubSecurityAuditService()
    repository.gateway_applications[0].update(
        owner_user_id=UUID("00000000-0000-4000-8000-000000000001"),
        owner_id="owner@contoso.com",
    )
    application_id = repository.gateway_applications[0]["id"]
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        listed = client.get("/api/v1/application-access/applications")
        detail = client.get(
            f"/api/v1/application-access/applications/{application_id}"
        )
        activity = client.get(
            f"/api/v1/application-access/applications/{application_id}/usage-activity",
            params={
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
                "interval": "day",
            },
        )
        avatar = client.get(
            f"/api/v1/application-access/applications/{application_id}/avatar"
        )

        assert listed.status_code == 200
        assert listed.json()["items"] == []
        assert detail.status_code == 404
        assert activity.status_code == 404
        assert avatar.status_code == 404
        denied_reads = [
            event for event in audit_service.events
            if getattr(event, "action", None) == "authorization.denied"
        ]
        assert len(denied_reads) == 3
        assert all(event.resource_id == str(application_id) for event in denied_reads)
    finally:
        client.cookies.set("turnstile_session", "test-owner-session")
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(application_access_service, None)


def test_denied_key_management_permission_is_recorded_in_security_audit() -> None:
    repository = InMemoryRepository()
    audit_service = StubSecurityAuditService()
    identity = SessionIdentity(
        id="00000000-0000-4000-8000-000000000003",
        email="unprivileged@contoso.com",
        name="Unprivileged",
        role="member",
        permissions=frozenset(),
        method="entra",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    app.dependency_overrides[require_authenticated_session] = lambda: identity
    app.dependency_overrides[application_access_service] = lambda: _seed(repository)
    app.dependency_overrides[get_access_control_service] = StubAccessControlService
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    try:
        response = client.get("/api/v1/application-access/me/subscriptions")
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(get_access_control_service, None)
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(require_authenticated_session, None)

    assert response.status_code == 403
    assert len(audit_service.events) == 1
    event = audit_service.events[0]
    assert event.action == "authorization.denied"
    assert event.result == "denied"
    assert event.resource_type == "permission"
    assert event.after == {"permission": "keys.manage.own|keys.manage.all"}


def test_member_key_creation_uses_server_owned_identity_and_apim_operation() -> None:
    repository = InMemoryRepository()
    operation_service = GatewayControlPlaneService(
        repository,
        CredentialCipher(Fernet.generate_key()),
        release_worker_enabled=True,
        application_provisioning_enabled=True,
        application_product_id="finops-applications",
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[control_plane_service] = lambda: operation_service
    app.dependency_overrides[get_access_control_service] = StubAccessControlService
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        response = client.post(
            "/api/v1/application-access/me/subscriptions",
            json={"display_name": "Build Agent Key", "application_type": "agent"},
        )
        assert response.status_code == 202
        payload = response.json()
        assert payload["operation"]["operation_kind"] == "application_provision"
        assert "primary_key" not in response.text
        assert "secondary_key" not in response.text
        spec = payload["operation"]["semantic_preview"]
        assert spec["owner_user_id"] == "00000000-0000-4000-8000-000000000002"
        assert spec["apim_subscription_id"].startswith("ts-key-")
        assert spec["slug"] == "user-00000000000040008000000000000002"
        assert spec["application_type"] == "agent"
        assert repository.gateway_release_operation_secret(
            UUID(payload["operation"]["id"])
        ) is None
    finally:
        client.cookies.set("turnstile_session", "test-owner-session")
        app.dependency_overrides.pop(get_access_control_service, None)
        app.dependency_overrides.pop(control_plane_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_owner_can_create_apim_key_for_selected_user_without_client_identity_fields() -> None:
    repository = InMemoryRepository()
    operation_service = GatewayControlPlaneService(
        repository,
        CredentialCipher(Fernet.generate_key()),
        release_worker_enabled=True,
        application_provisioning_enabled=True,
        application_product_id="finops-applications",
    )
    audit_service = StubSecurityAuditService()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[control_plane_service] = lambda: operation_service
    app.dependency_overrides[get_access_control_service] = StubAccessControlService
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    try:
        response = client.post(
            "/api/v1/admin/users/00000000-0000-4000-8000-000000000002/subscriptions",
            json={"display_name": "Member service key", "application_type": "service"},
        )
        assert response.status_code == 202
        spec = response.json()["operation"]["semantic_preview"]
        assert spec["owner_user_id"] == "00000000-0000-4000-8000-000000000002"
        assert spec["apim_subscription_id"].startswith("ts-key-")
        assert "primary_key" not in response.text
        assert repository.gateway_release_operation_secret(
            UUID(response.json()["operation"]["id"])
        ) is None
        assert [event.action for event in audit_service.events] == [
            "key.create.requested",
            "subscription.create",
        ]
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(get_access_control_service, None)
        app.dependency_overrides.pop(control_plane_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_owner_claims_unassigned_application_with_audit_and_future_owner_projection() -> None:
    repository = InMemoryRepository()
    read_service = _seed(repository)
    application_id = repository.gateway_applications[0]["id"]
    audit_service = StubSecurityAuditService()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_access_control_service] = StubAccessControlService
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    try:
        response = client.put(
            f"/api/v1/application-access/applications/{application_id}/owner",
            json={"user_id": "00000000-0000-4000-8000-000000000002"},
        )
        assert response.status_code == 200
        assert str(repository.gateway_applications[0]["owner_user_id"]) == (
            "00000000-0000-4000-8000-000000000002"
        )
        assert [event.action for event in audit_service.events] == [
            "application.owner.change"
        ]
        assert "primary-secret" not in repr(audit_service.events)
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(get_access_control_service, None)
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_owner_can_cancel_subscription_and_records_secret_free_audit() -> None:
    repository = InMemoryRepository()
    key_client = StubSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    application_id = repository.gateway_applications[0]["id"]
    subscription = repository.gateway_application_subscriptions[0]
    subscription_id = subscription["id"]
    audit_service = StubSecurityAuditService()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    try:
        cancelled = client.post(
            f"/api/v1/application-access/applications/{application_id}"
            f"/subscriptions/{subscription_id}/cancel",
            json={"confirmation": subscription["apim_subscription_id"]},
        )
        assert cancelled.status_code == 204
        assert cancelled.headers["cache-control"] == "no-store"
        assert subscription["state"] == "cancelled"
        assert key_client.calls == [("cancel", "outline-assistant", None)]
        assert [event.action for event in audit_service.events] == [
            "key.cancel.requested",
            "key.cancel",
        ]
        assert all(
            "primary-secret" not in repr(event)
            and "secondary-secret" not in repr(event)
            for event in audit_service.events
        )
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(application_access_service, None)
        app.dependency_overrides.pop(get_repository, None)


def test_member_can_cancel_own_subscription() -> None:
    repository = InMemoryRepository()
    key_client = StubSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    application = repository.gateway_applications[0]
    application.update(
        owner_user_id=UUID("00000000-0000-4000-8000-000000000002"),
        owner_email="member@contoso.com",
        owner_display_name="Member",
        owner_enabled=True,
        tenant_id=UUID("10000000-0000-4000-8000-000000000001"),
        tenant_enabled=True,
    )
    subscription = repository.gateway_application_subscriptions[0]
    audit_service = StubSecurityAuditService()
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        response = client.post(
            f"/api/v1/application-access/applications/{application['id']}"
            f"/subscriptions/{subscription['id']}/cancel",
            json={"confirmation": subscription["apim_subscription_id"]},
        )
    finally:
        client.cookies.clear()
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(application_access_service, None)

    assert response.status_code == 204
    assert subscription["state"] == "cancelled"
    assert key_client.calls == [("cancel", "outline-assistant", None)]
    assert [event.action for event in audit_service.events] == [
        "key.cancel.requested",
        "key.cancel",
    ]
    assert all("primary-secret" not in repr(event) for event in audit_service.events)


def _assign_seeded_subscription_to_owner(repository: InMemoryRepository) -> UUID:
    owner_id = UUID("00000000-0000-4000-8000-000000000001")
    repository.gateway_applications[0].update(
        owner_user_id=owner_id,
        owner_email="owner@contoso.com",
        owner_display_name="Owner",
        owner_enabled=True,
        tenant_id=UUID("10000000-0000-4000-8000-000000000001"),
        tenant_enabled=True,
        organization_unit_id=None,
    )
    return owner_id


def test_owner_can_export_filtered_apim_keys_as_no_store_csv_with_audit() -> None:
    repository = InMemoryRepository()
    key_client = StubSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    owner_id = _assign_seeded_subscription_to_owner(repository)
    audit_service = StubSecurityAuditService()
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_access_control_service] = StubAccessControlService
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    try:
        response = client.post(
            "/api/v1/admin/subscriptions/keys/export",
            json={"user_ids": [str(owner_id)], "subscription_ids": ["outline-assistant"]},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"].startswith("text/csv")
        assert "owner@contoso.com" in response.text
        assert "outline-assistant" in response.text
        assert "primary-secret" in response.text
        assert "secondary-secret" in response.text
        assert [event.action for event in audit_service.events] == [
            "key.export.requested",
            "key.export",
        ]
        assert all("primary-secret" not in repr(event) for event in audit_service.events)
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(get_access_control_service, None)
        app.dependency_overrides.pop(application_access_service, None)


def test_key_export_does_not_deliver_csv_when_success_audit_cannot_be_persisted() -> None:
    repository = InMemoryRepository()
    key_client = StubSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    owner_id = _assign_seeded_subscription_to_owner(repository)
    audit_service = FailingSecurityAuditService("key.export")
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_access_control_service] = StubAccessControlService
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    try:
        response = client.post(
            "/api/v1/admin/subscriptions/keys/export",
            json={"user_ids": [str(owner_id)]},
        )

        assert response.status_code == 503
        assert "primary-secret" not in response.text
        assert "secondary-secret" not in response.text
        assert key_client.calls == [("reveal", "outline-assistant", None)]
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(get_access_control_service, None)
        app.dependency_overrides.pop(application_access_service, None)


def test_apim_key_export_caps_list_secrets_concurrency_at_eight() -> None:
    repository = InMemoryRepository()
    key_client = ConcurrentSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    _assign_seeded_subscription_to_owner(repository)
    audit_service = StubSecurityAuditService()
    template = dict(repository.gateway_application_subscriptions[0])
    for index in range(16):
        repository.gateway_application_subscriptions.append(
            {
                **template,
                "id": UUID(int=index + 1000),
                "apim_subscription_id": f"extra-{index}",
                "display_name": f"Extra {index}",
            }
        )
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_access_control_service] = StubAccessControlService
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    try:
        response = client.post(
            "/api/v1/admin/subscriptions/keys/export", json={}
        )
        assert response.status_code == 200
        assert key_client.max_active <= 8
        assert key_client.max_active > 1
        assert len(key_client.calls) == 17
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(get_access_control_service, None)
        app.dependency_overrides.pop(application_access_service, None)


def test_apim_key_export_rejects_over_one_thousand_before_reading_secrets() -> None:
    repository = InMemoryRepository()
    key_client = StubSubscriptionKeyClient()
    read_service = _seed(repository, key_client)
    _assign_seeded_subscription_to_owner(repository)
    audit_service = StubSecurityAuditService()
    template = dict(repository.gateway_application_subscriptions[0])
    for index in range(1000):
        repository.gateway_application_subscriptions.append(
            {
                **template,
                "id": UUID(int=index + 2000),
                "apim_subscription_id": f"large-export-{index}",
            }
        )
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_access_control_service] = StubAccessControlService
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    try:
        response = client.post("/api/v1/admin/subscriptions/keys/export", json={})
        assert response.status_code == 422
        assert "1000" in response.text
        assert key_client.calls == []
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(get_access_control_service, None)
        app.dependency_overrides.pop(application_access_service, None)


def test_member_cannot_export_enterprise_apim_keys() -> None:
    repository = InMemoryRepository()
    read_service = _seed(repository, StubSubscriptionKeyClient())
    app.dependency_overrides[application_access_service] = lambda: read_service
    client.cookies.set("turnstile_session", MEMBER_SESSION)
    try:
        response = client.post("/api/v1/admin/subscriptions/keys/export", json={})
        assert response.status_code == 403
        assert response.headers.get("content-type", "").startswith("application/json")
    finally:
        client.cookies.set("turnstile_session", "test-owner-session")
        app.dependency_overrides.pop(application_access_service, None)


def test_key_export_sanitizes_apim_forbidden_response() -> None:
    repository = InMemoryRepository()
    read_service = _seed(repository, ForbiddenSubscriptionKeyClient())
    _assign_seeded_subscription_to_owner(repository)
    audit_service = StubSecurityAuditService()
    app.dependency_overrides[application_access_service] = lambda: read_service
    app.dependency_overrides[get_access_control_service] = StubAccessControlService
    app.dependency_overrides[get_optional_security_audit_service] = lambda: audit_service
    try:
        response = client.post(
            "/api/v1/admin/subscriptions/keys/export",
            json={"subscription_ids": ["outline-assistant"]},
        )
        assert response.status_code == 503
        assert response.json() == {"detail": "APIM key management is not authorized"}
        assert "primary-secret-must-not-leak" not in response.text
    finally:
        app.dependency_overrides.pop(get_optional_security_audit_service, None)
        app.dependency_overrides.pop(get_access_control_service, None)
        app.dependency_overrides.pop(application_access_service, None)


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


def test_owner_subscription_provision_returns_only_operation_status() -> None:
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
        assert "primary_key" not in payload
        assert "secondary_key" not in payload
        assert payload["operation"]["operation_kind"] == "application_provision"

        operation = client.get(payload["status_url"])
        assert operation.status_code == 200
        assert "primary_key" not in operation.text
        assert "secondary_key" not in operation.text
        assert repository.gateway_release_operation_secret(
            UUID(payload["operation"]["id"])
        ) is None
    finally:
        app.dependency_overrides.pop(control_plane_service, None)
        app.dependency_overrides.pop(get_repository, None)
