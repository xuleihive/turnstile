from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID
from xml.etree import ElementTree

import httpx
import pytest
from cryptography.fernet import Fernet

from backend.http import service_dependencies
from tests.backend.model_platform.control_plane_support import (
    APIM_ID,
    FakeApimClient,
    GatewayControlPlaneService,
    StubTokenProvider,
    publisher_settings,
)
from turnstile_core.config import Settings
from turnstile_core.domain.application_access import (
    GatewayApplicationSubscriptionCreate,
    GatewayApplicationSubscriptionProvisionSpec,
)
from turnstile_core.integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    RetryablePublicationError,
)
from turnstile_core.integrations.apim_publisher_client import AzureApimPublisherClient
from turnstile_core.integrations.ledger import (
    TableStorageLedger,
    application_map_partition_key,
    application_partition_key,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.application_access import ApplicationAccessService
from turnstile_core.services.application_provisioning_ledger import prepare_application_ledger
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    ControlPlaneUnavailableError,
)
from turnstile_core.services.gateway_release_operation_worker import GatewayReleaseOperationWorker


def _spec(**overrides: object) -> GatewayApplicationSubscriptionProvisionSpec:
    return GatewayApplicationSubscriptionProvisionSpec.model_validate(
        {
            "application_id": UUID(int=10),
            "application_subscription_id": UUID(int=11),
            "apim_subscription_id": "unit-application",
            "slug": "unit-application",
            "display_name": "Unit application",
            "application_type": "service",
            "scope_id": "unit-product",
            **overrides,
        }
    )


def test_historical_application_spec_remains_readable() -> None:
    spec = _spec()
    assert spec.provisioning_version == 1
    assert spec.gateway_profile_id is None
    assert spec.initial_monthly_token_limit is None


@pytest.mark.parametrize(
    "missing",
    (
        "gateway_profile_id",
        "initial_monthly_token_limit",
        "initial_tokens_per_minute",
    ),
)
def test_staged_application_spec_requires_bound_gateway_and_limits(missing: str) -> None:
    values: dict[str, object] = {
        "provisioning_version": 2,
        "gateway_profile_id": UUID(int=1),
        "initial_monthly_token_limit": 1000,
        "initial_tokens_per_minute": 100,
    }
    assert _spec(**values).provisioning_version == 2
    values.pop(missing)
    with pytest.raises(ValueError, match="snapshotted budget limits"):
        _spec(**values)


@pytest.mark.parametrize("value", (0, -1))
def test_staged_application_spec_rejects_nonpositive_limits(value: int) -> None:
    with pytest.raises(ValueError):
        _spec(
            provisioning_version=2,
            gateway_profile_id=UUID(int=1),
            initial_monthly_token_limit=value,
            initial_tokens_per_minute=100,
        )


class AdmissionRows:
    def __init__(self, missing: str | None = None) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.missing = missing

    def upsert(self, partition: str, row_key: str, entity: dict[str, Any]) -> None:
        self.rows[partition, row_key] = dict(entity)

    def insert_if_missing(self, partition: str, row_key: str, entity: dict[str, Any]) -> None:
        self.rows.setdefault((partition, row_key), dict(entity))

    def read_entity(self, partition: str, row_key: str) -> dict[str, Any] | None:
        return None if row_key == self.missing else self.rows.get((partition, row_key))


def _materialized_application() -> tuple[
    InMemoryRepository, UUID, GatewayApplicationSubscriptionProvisionSpec
]:
    repository = InMemoryRepository()
    gateway_id = next(
        item["id"] for item in repository.gateways if item["implementation"] == "apim"
    )
    spec = _spec(
        provisioning_version=2,
        gateway_profile_id=gateway_id,
        initial_monthly_token_limit=1000,
        initial_tokens_per_minute=100,
        owner_user_id=UUID("20000000-0000-4000-8000-000000000001"),
        tenant_id=UUID("10000000-0000-4000-8000-000000000001"),
    )
    ApplicationAccessService(repository, sync_available=True).provision(
        gateway_id, spec, "owner@example.com"
    )
    repository.gateway_applications[0].update(
        owner_email="owner@example.com",
        owner_display_name="Owner",
        owner_enabled=True,
        owner_tenant_id=UUID("10000000-0000-4000-8000-000000000001"),
        tenant_name="Test tenant",
        tenant_enabled=True,
        organization_unit_name="Platform",
        organization_unit_enabled=True,
    )
    return repository, gateway_id, spec


def test_application_ledger_prepare_preserves_confirmed_usage_and_reservations() -> None:
    repository, gateway_id, spec = _materialized_application()
    store = AdmissionRows()
    partition = application_partition_key(spec.application_id, datetime.now(UTC).strftime("%Y-%m"))
    store.rows[partition, "C"] = {"ConfirmedUsed": 55}
    store.rows[partition, "R|pending|request"] = {"Reserved": 40}
    store.rows["unrelated", "C"] = {"ConfirmedUsed": 200}
    for _ in range(2):
        prepare_application_ledger(
            repository, cast(TableStorageLedger, store), gateway_id, spec.application_id
        )
    assert store.rows[partition, "C"] == {"ConfirmedUsed": 55}
    assert store.rows[partition, "R|pending|request"] == {"Reserved": 40}
    assert store.rows["unrelated", "C"] == {"ConfirmedUsed": 200}
    assert store.rows[partition, "Q"]["Limit"] == 1000
    assert store.rows[partition, "Q"]["TokensPerMinute"] == 100
    mapping = store.rows[application_map_partition_key(gateway_id), spec.apim_subscription_id]
    assert mapping["ApplicationId"] == str(spec.application_id)
    assert mapping["ScopeExists"] is True
    assert mapping["OwnerUserId"] == "20000000-0000-4000-8000-000000000001"
    assert mapping["OwnerEnabled"] is True
    assert mapping["TenantEnabled"] is True
    assert len(store.rows) == 6


@pytest.mark.parametrize("missing", ("Q", "C", "M", "unit-application"))
def test_application_ledger_prepare_requires_complete_readback(missing: str) -> None:
    repository, gateway_id, spec = _materialized_application()
    store = AdmissionRows(missing)
    with pytest.raises(RetryablePublicationError, match="readback is incomplete"):
        prepare_application_ledger(
            repository, cast(TableStorageLedger, store), gateway_id, spec.application_id
        )


def test_application_ledger_prepare_projects_each_subscription_for_shared_user_app() -> None:
    repository, gateway_id, spec = _materialized_application()
    second = spec.model_copy(
        update={
            "application_subscription_id": UUID(int=12),
            "apim_subscription_id": "second-user-key",
            "display_name": "Second user key",
        }
    )
    ApplicationAccessService(repository, sync_available=True).provision(
        gateway_id, second, "owner@example.com"
    )
    store = AdmissionRows()

    prepare_application_ledger(
        repository, cast(TableStorageLedger, store), gateway_id, spec.application_id
    )

    map_partition = application_map_partition_key(gateway_id)
    primary_mapping = store.rows[(map_partition, spec.apim_subscription_id)]
    second_mapping = store.rows[(map_partition, second.apim_subscription_id)]
    assert primary_mapping["ApplicationId"] == second_mapping["ApplicationId"]
    assert primary_mapping["OwnerUserId"] == "20000000-0000-4000-8000-000000000001"
    assert second_mapping["OwnerUserId"] == primary_mapping["OwnerUserId"]
    assert second_mapping["TenantEnabled"] is True
    assert second_mapping["OwnerEnabled"] is True


def test_application_ledger_prepare_never_crosses_gateway_identity() -> None:
    repository, _gateway_id, spec = _materialized_application()
    store = AdmissionRows()
    with pytest.raises(RetryablePublicationError, match="not ready"):
        prepare_application_ledger(
            repository, cast(TableStorageLedger, store), UUID(int=99), spec.application_id
        )
    assert store.rows == {}


def _admission_policy(gateway_id: UUID) -> str:
    root = ElementTree.Element("policies")
    inbound = ElementTree.SubElement(root, "inbound")
    ElementTree.SubElement(
        inbound,
        "set-variable",
        {
            "name": "applicationMapPartition",
            "value": f'@("app-map|{gateway_id}")',
        },
    )
    ElementTree.SubElement(
        inbound,
        "set-variable",
        {
            "name": "telemetryGatewayProfileId",
            "value": str(gateway_id),
        },
    )
    return ElementTree.tostring(root, encoding="unicode")


def test_application_subscription_is_suspended_until_owned_activation_with_etag() -> None:
    spec = _spec(
        provisioning_version=2,
        gateway_profile_id=UUID(int=1),
        initial_monthly_token_limit=1000,
        initial_tokens_per_minute=100,
    )
    settings = publisher_settings()
    stored: dict[str, Any] = {}
    writes: list[httpx.Request] = []
    membership: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/policies/policy"):
            return httpx.Response(
                200,
                headers={"content-type": "application/xml"},
                text=(
                    "<policies><inbound><base /><return-response /></inbound></policies>"
                    if "/operations/" in request.url.path else _admission_policy(UUID(int=1))
                ),
            )
        if request.method == "GET" and request.url.path.endswith(f"/apis/{settings.apim_api_id}"):
            return httpx.Response(200, json={"properties": {
                "path": httpx.URL(str(settings.apim_gateway_url)).path.strip("/"),
            }})
        if request.url.path.endswith(f"/operations/{settings.apim_models_operation_id}"):
            return httpx.Response(200, json={"properties": {
                "method": "GET", "urlTemplate": "/v1/models",
            }})
        if "/products/" in request.url.path:
            if "/apis/" in request.url.path:
                membership.append(request.method)
                return httpx.Response(204)
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "state": "published",
                        "subscriptionRequired": True,
                    }
                },
            )
        if request.method == "PUT":
            writes.append(request)
            if stored:
                assert request.headers["If-Match"] == '"unit-etag"'
            body = json.loads(request.content)["properties"]
            assert "primaryKey" not in body
            assert "secondaryKey" not in body
            stored.update(body)
            return httpx.Response(200, json={"properties": stored})
        return (
            httpx.Response(200, headers={"ETag": '"unit-etag"'}, json={"properties": stored})
            if stored
            else httpx.Response(404)
        )

    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),  # type: ignore[arg-type]
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.ensure_application_subscription(spec)
    assert stored["state"] == "suspended"
    assert stored["displayName"] == spec.display_name
    client.ensure_application_subscription(spec)
    assert len(writes) == 1
    client.activate_application_subscription(spec)
    assert stored["state"] == "active"
    assert stored["displayName"] == spec.display_name
    assert stored["allowTracing"] is False
    client.activate_application_subscription(spec)
    assert len(writes) == 2
    assert membership == ["HEAD", "HEAD"]
    assert all(request.url.host == "management.azure.com" for request in writes)


class ApplicationSubscriptionTransport(httpx.MockTransport):
    def __init__(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        initial_state: str = "active",
    ) -> None:
        self.spec = spec
        self.settings = publisher_settings()
        self.requests: list[httpx.Request] = []
        self.root = (
            f"/subscriptions/{self.settings.azure_subscription_id}"
            f"/resourceGroups/{self.settings.apim_resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{self.settings.apim_service_name}"
        )
        self.current: dict[str, Any] | None = None if initial_state == "missing" else {
            "scope": f"{self.root}/products/{spec.scope_id}",
            "displayName": spec.display_name,
            "state": initial_state,
        }
        super().__init__(self._respond)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "management.azure.com"
        if request.url.path.endswith("/policies/policy"):
            assert self.spec.gateway_profile_id is not None
            return httpx.Response(200, json={"properties": {
                "value": _admission_policy(self.spec.gateway_profile_id),
            }})
        if request.method == "HEAD" and "/products/" in request.url.path:
            return httpx.Response(204)
        if request.url.path == f"{self.root}/products/{self.spec.scope_id}":
            return httpx.Response(200, json={"properties": {
                "state": "published", "subscriptionRequired": True,
            }})
        assert request.url.path == f"{self.root}/subscriptions/{self.spec.apim_subscription_id}"
        if request.method == "PUT":
            if self.current is None:
                assert "If-Match" not in request.headers
            else:
                assert self.current["state"] == "suspended"
                assert request.headers["If-Match"] == '"unit-etag"'
            self.current = json.loads(request.content)["properties"]
            assert self.current is not None
            assert "primaryKey" not in self.current
            assert "secondaryKey" not in self.current
        else:
            assert request.method == "GET"
        return httpx.Response(404) if self.current is None else httpx.Response(
            200, headers={"ETag": '"unit-etag"'}, json={"properties": self.current},
        )


def test_application_activation_uses_arm_state_readback_without_data_plane_probe() -> None:
    spec = _spec(
        provisioning_version=2,
        gateway_profile_id=UUID(int=1),
        initial_monthly_token_limit=1000,
        initial_tokens_per_minute=100,
    )
    transport = ApplicationSubscriptionTransport(spec, "missing")
    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),  # type: ignore[arg-type]
        httpx.Client(transport=transport),
    )
    client.ensure_application_subscription(spec)
    client.activate_application_subscription(spec)
    assert transport.current is not None and transport.current["state"] == "active"
    writes = [request for request in transport.requests if request.method == "PUT"]
    assert len(writes) == 2
    assert all("primaryKey" not in json.loads(request.content)["properties"] for request in writes)
    assert all(request.url.host == "management.azure.com" for request in transport.requests)


@pytest.mark.parametrize("mismatch", ("scope", "display_name", "state"))
def test_existing_subscription_with_same_id_but_different_identity_conflicts(
    mismatch: str,
) -> None:
    spec = _spec(
        provisioning_version=2, gateway_profile_id=UUID(int=1),
        initial_monthly_token_limit=1000, initial_tokens_per_minute=100,
    )
    transport = ApplicationSubscriptionTransport(spec, "suspended")
    assert transport.current is not None
    if mismatch == "scope":
        transport.current["scope"] += "-other"
    elif mismatch == "display_name":
        transport.current["displayName"] = "Some other application"
    else:
        transport.current["state"] = "active"
    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(), httpx.Client(transport=transport),
    )
    with pytest.raises(PolicyCompilationError, match="already exists"):
        client.ensure_application_subscription(spec)
    assert not any(request.method == "PUT" for request in transport.requests)


def test_legacy_suspended_subscription_can_be_resumed_with_encrypted_operation_marker() -> None:
    spec = _spec(
        provisioning_version=2, gateway_profile_id=UUID(int=1),
        initial_monthly_token_limit=1000, initial_tokens_per_minute=100,
    )
    primary_key, secondary_key = "legacy-primary", "legacy-secondary"
    legacy_name = AzureApimPublisherClient._application_pending_name(primary_key, secondary_key)
    transport = ApplicationSubscriptionTransport(spec, "suspended")
    assert transport.current is not None
    transport.current["displayName"] = legacy_name
    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(), httpx.Client(transport=transport),
    )
    client.ensure_application_subscription(spec, legacy_pending_name=legacy_name)
    client.activate_application_subscription(spec, legacy_pending_name=legacy_name)
    assert transport.current["displayName"] == spec.display_name
    assert transport.current["state"] == "active"
    assert all(
        "primaryKey" not in json.loads(request.content).get("properties", {})
        and "secondaryKey" not in json.loads(request.content).get("properties", {})
        for request in transport.requests if request.method == "PUT"
    )


@pytest.mark.parametrize("rejection", ("gateway", "product", "membership"))
def test_application_preflight_rejection_never_writes_a_subscription(rejection: str) -> None:
    spec = _spec(
        provisioning_version=2,
        gateway_profile_id=UUID(int=1),
        initial_monthly_token_limit=1000,
        initial_tokens_per_minute=100,
    )
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            writes.append(request.url.path)
        if request.url.path.endswith("/policies/policy"):
            gateway = UUID(int=2) if rejection == "gateway" else UUID(int=1)
            return httpx.Response(
                200, headers={"content-type": "application/xml"}, text=_admission_policy(gateway)
            )
        if request.method == "HEAD":
            return httpx.Response(404 if rejection == "membership" else 204)
        return httpx.Response(
            200,
            json={
                "properties": {
                    "state": "published",
                    "subscriptionRequired": rejection != "product",
                }
            },
        )

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),  # type: ignore[arg-type]
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PolicyCompilationError):
        client.ensure_application_subscription(spec)
    assert writes == []


@pytest.mark.parametrize(
    "missing",
    (
        "gateway_release_worker_enabled",
        "gateway_application_provisioning_enabled",
        "ledger_table_endpoint",
        "azure_subscription_id",
        "apim_resource_group",
        "apim_service_name",
    ),
)
def test_creation_capability_requires_all_deployed_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    settings = Settings(
        control_plane_enabled=True,
        gateway_release_worker_enabled=True,
        gateway_application_provisioning_enabled=True,
        ledger_table_endpoint="https://ledger.example.test",
        azure_subscription_id=str(UUID(int=5)),
        apim_resource_group="unit-rg",
        apim_service_name="unit-apim",
        apim_usage_observer_url="https://observer.example.test",
        apim_usage_observer_key_named_value="unit-observer-key",
    )
    assert service_dependencies._application_provisioning_available(settings)
    settings = settings.model_copy(
        update={
            missing: False if missing.endswith("enabled") else None,
        }
    )
    monkeypatch.setattr(service_dependencies, "get_settings", lambda: settings)
    repository = InMemoryRepository()
    inventory = service_dependencies.application_access_service(repository).applications()
    assert inventory.provisioning_available is False
    assert inventory.provisioning_unavailable_reason
    assert inventory.provisioning_defaults is not None
    service = service_dependencies.control_plane_service(repository)
    with pytest.raises(ControlPlaneUnavailableError):
        service.request_application_subscription_provision(
            UUID(int=1),
            GatewayApplicationSubscriptionCreate(
                subscription_id="unavailable",
                display_name="Unavailable",
            ),
            "owner@example.com",
        )
    assert repository.gateway_release_operations == []


class StagedProvisioner(FakeApimClient):
    def __init__(self) -> None:
        super().__init__()
        self.subscription: tuple[GatewayApplicationSubscriptionProvisionSpec] | None = None
        self.suspend_count = 0
        self.active = False

    def ensure_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        *,
        legacy_pending_name: str | None = None,
    ) -> None:
        assert legacy_pending_name is None
        self.subscription = (spec,)
        self.suspend_count += 1

    def activate_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        *,
        legacy_pending_name: str | None = None,
    ) -> None:
        assert legacy_pending_name is None
        assert self.subscription == (spec,)
        self.active = True


def _queued_creation() -> tuple[InMemoryRepository, CredentialCipher, UUID]:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(
        repository,
        cipher,
        application_default_token_limit=24_000,
        application_default_tokens_per_minute=1500,
        application_product_id="unit-product",
    )
    accepted = service.request_application_subscription_provision(
        APIM_ID,
        GatewayApplicationSubscriptionCreate(
            subscription_id="staged-unit-app",
            display_name="Staged unit app",
        ),
        "owner@example.com",
    )
    return repository, cipher, accepted.operation.id


def test_ledger_retry_keeps_frozen_budgets_without_local_keys() -> None:
    repository, cipher, operation_id = _queued_creation()
    client = StagedProvisioner()
    assert repository.gateway_release_operation_secret(operation_id) is None
    attempts: list[UUID] = []
    store = AdmissionRows()

    def project(gateway_id: UUID, application_id: UUID) -> None:
        assert gateway_id == APIM_ID
        attempts.append(application_id)
        if len(attempts) == 1:
            raise RuntimeError("unit ledger propagation delay")
        prepare_application_ledger(
            repository, cast(TableStorageLedger, store), gateway_id, application_id
        )

    worker = GatewayReleaseOperationWorker(
        repository,
        client,
        cipher=cipher,
        application_projector=project,
        application_default_token_limit=1,
        application_default_tokens_per_minute=1,
    )
    for expected in (
        "validating_dependencies",
        "promoting",
        "verifying_readback",
        "verifying_readback",
    ):
        result = worker.run_once("unit-worker")
        assert result is not None and result.status == expected
        assert result.id == operation_id
        assert not client.active
        assert repository.gateway_release_operation_secret(operation_id) is None
    assert client.subscription is not None
    assert client.subscription[0].scope_id == "unit-product"
    assert client.subscription[0].initial_monthly_token_limit == 24_000
    completed = worker.run_once("unit-worker")
    assert completed is not None and completed.status == "succeeded"
    assert completed.error_message is None
    assert completed.checkpoint["admission_ready"] is True
    assert client.active and client.suspend_count == 1
    assert repository.gateway_release_operation_secret(operation_id) is None
    assert len(repository.gateway_applications) == len(repository.gateway_release_operations) == 1
    assert (
        next(value for (partition, key), value in store.rows.items() if key == "Q")["Limit"]
        == 24_000
    )


def test_application_worker_completes_from_apim_state_without_local_secret() -> None:
    repository, _cipher, operation_id = _queued_creation()
    assert repository.gateway_release_operation_secret(operation_id) is None
    operation = repository.get_gateway_release_operation(operation_id)
    assert operation is not None
    spec = GatewayApplicationSubscriptionProvisionSpec.model_validate(operation["semantic_preview"])
    transport = ApplicationSubscriptionTransport(spec, "missing")
    ledger = AdmissionRows()
    worker = GatewayReleaseOperationWorker(
        repository,
        AzureApimPublisherClient(
            publisher_settings(), cast(Any, StubTokenProvider()), httpx.Client(transport=transport),
        ),
        application_projector=lambda gateway, application: prepare_application_ledger(
            repository, cast(TableStorageLedger, ledger), gateway, application,
        ),
    )
    for expected in (
        "validating_dependencies", "promoting", "verifying_readback", "succeeded",
    ):
        result = worker.run_once("unit-worker")
        assert result is not None and result.status == expected
    assert result is not None
    assert result.checkpoint["subscription_active"] is True
    assert result.checkpoint["key_stored"] is False
    assert "data_plane_authentication_ready" not in result.checkpoint
    assert repository.gateway_release_operation_secret(operation_id) is None
    assert transport.current is not None and transport.current["state"] == "active"
    assert all(
        request.url.host == "management.azure.com" for request in transport.requests
    )
    assert all(
        "primaryKey" not in json.loads(request.content).get("properties", {})
        and "secondaryKey" not in json.loads(request.content).get("properties", {})
        for request in transport.requests if request.method == "PUT"
    )


def test_legacy_application_secret_recovers_old_pending_resource_and_is_cleared() -> None:
    repository, cipher, operation_id = _queued_creation()
    primary_key, secondary_key = "legacy-primary", "legacy-secondary"
    repository.gateway_release_operation_secrets[operation_id] = cipher.encrypt(
        json.dumps({"primary_key": primary_key, "secondary_key": secondary_key})
    )
    operation = repository.get_gateway_release_operation(operation_id)
    assert operation is not None
    operation["checkpoint"] = {"data_plane_authentication_ready": False}
    spec = GatewayApplicationSubscriptionProvisionSpec.model_validate(operation["semantic_preview"])
    legacy_name = AzureApimPublisherClient._application_pending_name(primary_key, secondary_key)
    transport = ApplicationSubscriptionTransport(spec, "suspended")
    assert transport.current is not None
    transport.current["displayName"] = legacy_name
    ledger = AdmissionRows()
    worker = GatewayReleaseOperationWorker(
        repository,
        AzureApimPublisherClient(
            publisher_settings(), cast(Any, StubTokenProvider()), httpx.Client(transport=transport),
        ),
        cipher=cipher,
        application_projector=lambda gateway, application: prepare_application_ledger(
            repository, cast(TableStorageLedger, ledger), gateway, application,
        ),
    )
    for expected in (
        "validating_dependencies", "promoting", "verifying_readback",
    ):
        result = worker.run_once("unit-worker")
        assert result is not None and result.status == expected
        assert repository.gateway_release_operation_secret(operation_id) is not None
        assert primary_key not in result.model_dump_json()
        assert secondary_key not in result.model_dump_json()
    completed = worker.run_once("replacement-worker")
    assert completed is not None and completed.status == "succeeded"
    assert repository.gateway_release_operation_secret(operation_id) is None
    assert "data_plane_authentication_ready" not in completed.checkpoint
    assert transport.current is not None
    assert transport.current["displayName"] == spec.display_name
    assert transport.current["state"] == "active"
    assert all(
        "primaryKey" not in json.loads(request.content).get("properties", {})
        and "secondaryKey" not in json.loads(request.content).get("properties", {})
        for request in transport.requests if request.method == "PUT"
    )


@pytest.mark.parametrize("historical", (False, True))
def test_missing_projector_fails_new_creation_before_write_but_preserves_historical_path(
    historical: bool,
) -> None:
    repository, cipher, operation_id = _queued_creation()
    if historical:
        preview = repository.gateway_release_operations[0]["semantic_preview"]
        for field in (
            "provisioning_version",
            "gateway_profile_id",
            "initial_monthly_token_limit",
            "initial_tokens_per_minute",
        ):
            preview.pop(field)
    client = StagedProvisioner()
    worker = GatewayReleaseOperationWorker(repository, client, cipher=cipher)
    worker.run_once("unit-worker")
    result = worker.run_once("unit-worker")
    if historical:
        assert result is not None and result.status == "promoting"
        result = worker.run_once("unit-worker")
        assert result is not None and result.status == "succeeded"
        assert client.suspend_count == 1
    else:
        assert result is not None and result.status == "failed"
        assert client.suspend_count == 0
        assert repository.gateway_applications == []
    assert repository.gateway_release_operation_secret(operation_id) is None


def test_terminal_ledger_failure_keeps_subscription_suspended_and_clears_secret() -> None:
    repository, cipher, operation_id = _queued_creation()
    client = StagedProvisioner()

    def reject(_gateway: UUID, _application: UUID) -> None:
        raise PolicyCompilationError("unit scope no longer exists")

    worker = GatewayReleaseOperationWorker(
        repository, client, cipher=cipher, application_projector=reject
    )
    for _ in range(3):
        worker.run_once("unit-worker")
    failed = worker.run_once("unit-worker")
    assert failed is not None and failed.status == "failed"
    assert not client.active
    assert client.suspend_count == 1
    assert failed.checkpoint["apim_subscription_created"] is True
    assert repository.gateway_release_operation_secret(operation_id) is None


@pytest.mark.parametrize(
    "subscription_id", ("master", "turnstile-dashboard", "turnstile-publisher-probe")
)
def test_system_subscription_ids_cannot_be_queued(subscription_id: str) -> None:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository, CredentialCipher(Fernet.generate_key()))
    with pytest.raises(ControlPlaneConflictError, match="reserved"):
        service.request_application_subscription_provision(
            APIM_ID,
            GatewayApplicationSubscriptionCreate(
                subscription_id=subscription_id,
                display_name="Reserved",
            ),
            "owner@example.com",
        )
    assert repository.gateway_release_operations == []


def test_foreign_gateway_in_staged_operation_is_rejected_before_any_side_effect() -> None:
    repository, cipher, operation_id = _queued_creation()
    repository.gateway_release_operations[0]["semantic_preview"]["gateway_profile_id"] = str(
        UUID(int=99)
    )
    client = StagedProvisioner()
    worker = GatewayReleaseOperationWorker(repository, client, cipher=cipher)
    failed = worker.run_once("unit-worker")
    assert failed is not None and failed.status == "failed"
    assert client.subscription is None
    assert repository.gateway_release_operation_secret(operation_id) is None
