from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID
from xml.etree import ElementTree

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

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
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
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
    )
    ApplicationAccessService(repository, sync_available=True).provision(
        gateway_id, spec, "owner@example.com"
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
    assert len(store.rows) == 6


@pytest.mark.parametrize("missing", ("Q", "C", "M", "unit-application"))
def test_application_ledger_prepare_requires_complete_readback(missing: str) -> None:
    repository, gateway_id, spec = _materialized_application()
    store = AdmissionRows(missing)
    with pytest.raises(RetryablePublicationError, match="readback is incomplete"):
        prepare_application_ledger(
            repository, cast(TableStorageLedger, store), gateway_id, spec.application_id
        )


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
    authentication: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "management.azure.com":
            key = request.headers.get("Ocp-Apim-Subscription-Key")
            authentication.append(key)
            assert request.method == "GET" and request.url.path.endswith("/v1/models")
            return httpx.Response(200 if key == "unit-primary" else 401, json={"data": []})
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
            stored.update(json.loads(request.content)["properties"])
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
    client.ensure_application_subscription(spec, "unit-primary", "unit-secondary")
    assert stored["state"] == "suspended"
    assert stored["displayName"].startswith("Turnstile pending ")
    assert "unit-primary" not in stored["displayName"]
    client.ensure_application_subscription(spec, "unit-primary", "unit-secondary")
    assert len(writes) == 1
    client.activate_application_subscription(spec, "unit-primary", "unit-secondary")
    assert stored["state"] == "active"
    assert stored["displayName"] == spec.display_name
    assert stored["allowTracing"] is False
    client.activate_application_subscription(spec, "unit-primary", "unit-secondary")
    assert len(writes) == 2
    assert membership == ["HEAD", "HEAD"]
    assert authentication == ["unit-primary", None, "unit-primary", None]


class ApplicationSubscriptionTransport(httpx.MockTransport):
    def __init__(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str,
        secondary_key: str,
        initial_state: str = "active",
    ) -> None:
        self.spec = spec
        self.primary_key = primary_key
        self.secondary_key = secondary_key
        self.settings = publisher_settings()
        self.requests: list[httpx.Request] = []
        self.data_plane_result: int | type[httpx.TransportError] = 200
        self.anonymous_status = 401
        self.response_headers: dict[str, str] = {}
        self.api_path = httpx.URL(str(self.settings.apim_gateway_url)).path.strip("/")
        self.gateway_host = httpx.URL(str(self.settings.apim_gateway_url)).host
        self.catalog_operation = {"method": "GET", "urlTemplate": "/v1/models"}
        self.catalog_policy = (
            "<policies><inbound><base /><return-response /></inbound></policies>"
        )
        self.root = (
            f"/subscriptions/{self.settings.azure_subscription_id}"
            f"/resourceGroups/{self.settings.apim_resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{self.settings.apim_service_name}"
        )
        self.current: dict[str, Any] | None = None if initial_state == "missing" else {
            "scope": f"{self.root}/products/{spec.scope_id}",
            "displayName": (
                spec.display_name if initial_state == "active"
                else AzureApimPublisherClient._application_pending_name(primary_key, secondary_key)
            ),
            "state": initial_state,
        }
        super().__init__(self._respond)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == self.gateway_host:
            assert request.method == "GET" and request.url.path == f"/{self.api_path}/v1/models"
            assert not request.url.query and not request.content
            assert "Authorization" not in request.headers and "Cookie" not in request.headers
            key = request.headers.get("Ocp-Apim-Subscription-Key")
            assert key in {None, self.primary_key}
            assert request.extensions["timeout"] == dict.fromkeys(
                ("connect", "read", "write", "pool"), 10.0,
            )
            if key and not isinstance(self.data_plane_result, int):
                raise self.data_plane_result(
                    f"unsafe transport detail {self.primary_key}", request=request,
                )
            response_status = self.data_plane_result if key else self.anonymous_status
            assert isinstance(response_status, int)
            return httpx.Response(
                response_status, json={"data": [], "unsafe_body": self.primary_key},
                headers=self.response_headers,
            )
        assert request.url.host == "management.azure.com"
        operation_path = f"/operations/{self.settings.apim_models_operation_id}"
        if request.url.path.endswith(f"{operation_path}/policies/policy"):
            return httpx.Response(200, json={"properties": {"value": self.catalog_policy}})
        if request.url.path.endswith(operation_path):
            return httpx.Response(200, json={"properties": self.catalog_operation})
        if request.url.path.endswith("/policies/policy"):
            assert self.spec.gateway_profile_id is not None
            return httpx.Response(200, json={"properties": {
                "value": _admission_policy(self.spec.gateway_profile_id),
            }})
        if request.url.path == f"{self.root}/apis/{self.settings.apim_api_id}":
            return httpx.Response(200, json={"properties": {"path": self.api_path}})
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
            assert self.current["primaryKey"] == self.primary_key
            assert self.current["secondaryKey"] == self.secondary_key
        else:
            assert request.method == "GET"
        return httpx.Response(404) if self.current is None else httpx.Response(
            200, headers={"ETag": '"unit-etag"'}, json={"properties": self.current},
        )


@pytest.mark.parametrize("initial_state", ("suspended", "active"))
def test_application_activation_waits_for_data_plane_authentication(initial_state: str) -> None:
    spec = _spec(
        provisioning_version=2,
        gateway_profile_id=UUID(int=1),
        initial_monthly_token_limit=1000,
        initial_tokens_per_minute=100,
    )
    transport = ApplicationSubscriptionTransport(
        spec, "unit-primary", "unit-secondary", initial_state,
    )
    transport.data_plane_result = 401
    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),  # type: ignore[arg-type]
        httpx.Client(transport=transport),
    )
    with pytest.raises(RetryablePublicationError, match="data-plane authentication"):
        client.activate_application_subscription(spec, "unit-primary", "unit-secondary")
    assert transport.current is not None and transport.current["state"] == "active"
    expected_writes = int(initial_state == "suspended")
    assert sum(request.method == "PUT" for request in transport.requests) == expected_writes
    transport.data_plane_result = 200
    client.activate_application_subscription(spec, "unit-primary", "unit-secondary")
    assert sum(request.method == "PUT" for request in transport.requests) == expected_writes
    probes = [
        request for request in transport.requests if request.url.host == transport.gateway_host
    ]
    assert [request.headers.get("Ocp-Apim-Subscription-Key") for request in probes] == [
        "unit-primary", "unit-primary", None,
    ]


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
        client.ensure_application_subscription(spec, "unit-primary", "unit-secondary")
    assert writes == []


@pytest.mark.parametrize(
    "missing",
    (
        "gateway_release_worker_enabled",
        "gateway_application_provisioning_enabled",
        "ledger_table_endpoint",
        "credential_encryption_key",
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
        credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
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


def test_api_services_use_configured_system_subscription_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        gateway_release_worker_enabled=True,
        gateway_application_provisioning_enabled=True,
        ledger_table_endpoint="https://ledger.example.test",
        credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
        azure_subscription_id=str(UUID(int=5)),
        apim_resource_group="unit-rg",
        apim_service_name="unit-apim",
        apim_dashboard_subscription_id="unit-dashboard",
        apim_probe_subscription_id="unit-publisher-probe",
    )
    monkeypatch.setattr(service_dependencies, "get_settings", lambda: settings)
    repository = InMemoryRepository()

    access = service_dependencies.application_access_service(repository)
    access.sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=APIM_ID,
            discovered_at=datetime(2026, 9, 23, tzinfo=UTC),
            items=[
                GatewayApplicationDiscoveryItem(
                    apim_subscription_id="unit-dashboard",
                    display_name="Unit dashboard",
                    state="active",
                    scope_type="product",
                    scope_id="unit-product",
                    scope_exists=True,
                    application_type="system",
                    system_managed=True,
                )
            ],
        ),
        "worker",
    )
    assert repository.gateway_application_subscriptions[0]["source"] == "bicep"

    control_plane = service_dependencies.control_plane_service(repository)
    with pytest.raises(ControlPlaneConflictError, match="reserved"):
        control_plane.request_application_subscription_provision(
            APIM_ID,
            GatewayApplicationSubscriptionCreate(
                subscription_id="unit-publisher-probe",
                display_name="Reserved",
            ),
            "owner@example.com",
        )


class StagedProvisioner(FakeApimClient):
    def __init__(self) -> None:
        super().__init__()
        self.subscription: tuple[GatewayApplicationSubscriptionProvisionSpec, str, str] | None = (
            None
        )
        self.suspend_count = 0
        self.active = False

    def ensure_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str,
        secondary_key: str,
    ) -> None:
        self.subscription = (spec, primary_key, secondary_key)
        self.suspend_count += 1

    def activate_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str,
        secondary_key: str,
    ) -> None:
        assert self.subscription == (spec, primary_key, secondary_key)
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


def test_ledger_retry_cannot_activate_or_change_frozen_budgets_and_keys() -> None:
    repository, cipher, operation_id = _queued_creation()
    client = StagedProvisioner()
    ciphertext = repository.gateway_release_operation_secret(operation_id)
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
        assert repository.gateway_release_operation_secret(operation_id) == ciphertext
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


@pytest.mark.parametrize("recover", (False, True))
@pytest.mark.parametrize(
    "outcome", (401, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError),
)
def test_application_worker_waits_for_authentication_or_reports_retry_limit(
    outcome: int | type[httpx.TransportError], recover: bool,
) -> None:
    repository, cipher, operation_id = _queued_creation()
    operation = repository.get_gateway_release_operation(operation_id)
    assert operation is not None
    spec = GatewayApplicationSubscriptionProvisionSpec.model_validate(operation["semantic_preview"])
    ciphertext = repository.gateway_release_operation_secret(operation_id)
    plaintext = cipher.decrypt(ciphertext)
    assert plaintext is not None
    keys = json.loads(plaintext)
    transport = ApplicationSubscriptionTransport(
        spec, keys["primary_key"], keys["secondary_key"], "missing",
    )
    transport.data_plane_result = outcome
    correlation_id = str(UUID(int=42))
    transport.response_headers = {"apim-request-id": correlation_id}
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
    max_attempts = 6 if recover else 5
    for expected in ("validating_dependencies", "promoting", "verifying_readback"):
        pending = worker.run_once("unit-worker", max_attempts=max_attempts)
        assert pending is not None and pending.status == expected
    assert pending is not None
    checkpoint = dict(pending.checkpoint)
    budgets = deepcopy(repository.gateway_application_budgets)
    for _ in range(2):
        pending = worker.run_once("unit-worker", max_attempts=max_attempts)
        assert pending is not None and pending.status == "verifying_readback"
        assert not pending.checkpoint.get("subscription_active")
        assert not pending.checkpoint.get("data_plane_authentication_ready")
        assert all(pending.checkpoint[name] == value for name, value in checkpoint.items())
        assert repository.gateway_release_operation_secret(operation_id) == ciphertext
        assert pending.error_message is not None
        diagnostic = json.loads(pending.error_message.partition(": ")[2])
        assert diagnostic == {
            "category": (
                "subscription_not_ready" if isinstance(outcome, int) else
                "timeout" if issubclass(outcome, httpx.TimeoutException) else "transport_error"
            ),
            "status_code": outcome if isinstance(outcome, int) else None,
            "correlation_id": correlation_id if isinstance(outcome, int) else None,
        }
        serialized = pending.model_dump_json()
        assert keys["primary_key"] not in serialized and keys["secondary_key"] not in serialized
        assert "unsafe_body" not in serialized and "unsafe transport detail" not in serialized
    if recover:
        transport.data_plane_result = 200
    completed = worker.run_once("replacement-worker", max_attempts=max_attempts)
    assert completed is not None and completed.status == ("succeeded" if recover else "failed")
    if recover:
        assert completed.error_message is None
        assert completed.checkpoint["data_plane_authentication_ready"] is True
        assert completed.checkpoint["subscription_active"] is True
    else:
        assert completed.error_code == "max_attempts_exceeded"
        assert completed.checkpoint["max_attempts_exceeded"] is True
        assert not completed.checkpoint.get("subscription_active")
    assert repository.gateway_release_operation_secret(operation_id) is None
    assert repository.gateway_application_budgets == budgets
    assert sum(request.method == "PUT" for request in transport.requests) == 2
    probes = [
        request for request in transport.requests if request.url.host == transport.gateway_host
    ]
    assert len(probes) == (4 if recover else 2)
    assert all(request.method == "GET" for request in probes)
    request_count = len(transport.requests)
    assert worker.run_once("unit-worker", max_attempts=6) is None
    assert len(transport.requests) == request_count


def test_application_authentication_lost_lease_retains_recovery_keys() -> None:
    repository, cipher, operation_id = _queued_creation()
    operation = repository.get_gateway_release_operation(operation_id)
    assert operation is not None
    spec = GatewayApplicationSubscriptionProvisionSpec.model_validate(operation["semantic_preview"])
    ciphertext = repository.gateway_release_operation_secret(operation_id)
    plaintext = cipher.decrypt(ciphertext)
    assert plaintext is not None
    keys = json.loads(plaintext)
    transport = ApplicationSubscriptionTransport(
        spec, keys["primary_key"], keys["secondary_key"], "missing",
    )
    lose_lease = True

    def handler(request: httpx.Request) -> httpx.Response:
        response = transport.handle_request(request)
        if (
            lose_lease and request.url.host == transport.gateway_host
            and request.headers.get("Ocp-Apim-Subscription-Key")
        ):
            operation["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
        return response

    ledger = AdmissionRows()
    worker = GatewayReleaseOperationWorker(
        repository,
        AzureApimPublisherClient(
            publisher_settings(), cast(Any, StubTokenProvider()),
            httpx.Client(transport=httpx.MockTransport(handler)),
        ),
        cipher=cipher,
        application_projector=lambda gateway, application: prepare_application_ledger(
            repository, cast(TableStorageLedger, ledger), gateway, application,
        ),
    )
    for _ in range(3):
        assert worker.run_once("unit-worker") is not None
    with pytest.raises(RuntimeError, match="changed while leased"):
        worker.run_once("unit-worker")
    assert operation["status"] == "verifying_readback"
    assert not operation["checkpoint"].get("data_plane_authentication_ready")
    assert repository.gateway_release_operation_secret(operation_id) == ciphertext
    lose_lease = False
    completed = worker.run_once("replacement-worker")
    assert completed is not None and completed.status == "succeeded"
    assert completed.checkpoint["data_plane_authentication_ready"] is True
    assert repository.gateway_release_operation_secret(operation_id) is None
    assert sum(request.method == "PUT" for request in transport.requests) == 2


@pytest.mark.parametrize("problem", (
    "api_path", "http", "query", "fragment", "userinfo", "post_operation",
    "inference_path", "missing_base", "late_base", "invalid_xml",
))
def test_application_authentication_rejects_unsafe_catalog_target(problem: str) -> None:
    spec = _spec(
        provisioning_version=2, gateway_profile_id=APIM_ID,
        initial_monthly_token_limit=1000, initial_tokens_per_minute=100,
    )
    transport = ApplicationSubscriptionTransport(spec, "unit-primary", "unit-secondary")
    settings = publisher_settings()
    base = str(settings.apim_gateway_url)
    addresses = {
        "api_path": f"{base}/wrong-api",
        "http": base.replace("https://", "http://"),
        "query": f"{base}?subscription-key=unit-wrong-key",
        "fragment": f"{base}#fragment",
        "userinfo": base.replace("https://", "https://unit-user:unit-password@"),
    }
    if problem in addresses:
        settings = settings.model_copy(update={"apim_gateway_url": addresses[problem]})
    elif problem == "post_operation":
        transport.catalog_operation["method"] = "POST"
    elif problem == "inference_path":
        transport.catalog_operation["urlTemplate"] = "/v1/messages"
    elif problem == "missing_base":
        transport.catalog_policy = "<policies><inbound><return-response /></inbound></policies>"
    elif problem == "late_base":
        transport.catalog_policy = (
            "<policies><inbound><return-response /><base /></inbound></policies>"
        )
    else:
        transport.catalog_policy = "not xml"
    client = AzureApimPublisherClient(
        settings, cast(Any, StubTokenProvider()), httpx.Client(transport=transport),
    )
    with pytest.raises(PolicyCompilationError):
        client.activate_application_subscription(spec, "unit-primary", "unit-secondary")
    assert all(
        request.method == "GET" and request.url.host == "management.azure.com"
        for request in transport.requests
    )


@pytest.mark.parametrize("anonymous_status", (200, 302, 401, 403, 429, 503))
def test_application_authentication_isolates_credentials_and_checks_anonymous_control(
    anonymous_status: int,
) -> None:
    spec = _spec(
        provisioning_version=2, gateway_profile_id=APIM_ID,
        initial_monthly_token_limit=1000, initial_tokens_per_minute=100,
    )
    transport = ApplicationSubscriptionTransport(spec, "unit-primary", "unit-secondary")
    transport.anonymous_status = anonymous_status
    transport.response_headers = {
        "Set-Cookie": "session=untrusted", "Location": "https://untrusted.example/",
    }
    token_provider = cast(Any, StubTokenProvider())
    client = AzureApimPublisherClient(publisher_settings(), token_provider, httpx.Client(
        transport=transport, timeout=None, follow_redirects=True,
        auth=("unit-user", "unit-password"),
        headers={
            "Authorization": "Bearer unit-token", "Ocp-Apim-Subscription-Key": "other-unit-key",
        },
        cookies={"session": "unit-cookie"}, params={"subscription-key": "other-unit-key"},
    ))
    if anonymous_status in {401, 403}:
        client.activate_application_subscription(spec, "unit-primary", "unit-secondary")
    else:
        expected = PolicyCompilationError if anonymous_status == 200 else RetryablePublicationError
        with pytest.raises(expected) as caught:
            client.activate_application_subscription(spec, "unit-primary", "unit-secondary")
        assert "unit-primary" not in str(caught.value) and "unsafe_body" not in str(caught.value)
    probes = [
        request for request in transport.requests if request.url.host == transport.gateway_host
    ]
    assert [request.headers.get("Ocp-Apim-Subscription-Key") for request in probes] == [
        "unit-primary", None,
    ]
    assert all(request.method == "GET" for request in transport.requests)


@pytest.mark.parametrize(("response_status", "header", "value", "correlation"), (
    (401, "apim-request-id", str(UUID(int=41)), str(UUID(int=41))),
    (403, "x-request-id", str(UUID(int=42)), str(UUID(int=42))),
    (404, "x-correlation-id", str(UUID(int=43)), str(UUID(int=43))),
    (408, "x-request-id", "", None),
    (429, "x-request-id", "unit-primary", None),
    (503, "apim-request-id", "untrusted raw diagnostic", None),
    (302, "Location", "https://untrusted.example/", None),
))
def test_application_authentication_preserves_only_safe_diagnostics(
    response_status: int, header: str, value: str, correlation: str | None,
) -> None:
    spec = _spec(
        provisioning_version=2, gateway_profile_id=APIM_ID,
        initial_monthly_token_limit=1000, initial_tokens_per_minute=100,
    )
    transport = ApplicationSubscriptionTransport(spec, "unit-primary", "unit-secondary")
    transport.data_plane_result = response_status
    transport.response_headers = {header: value}
    client = AzureApimPublisherClient(
        publisher_settings(), cast(Any, StubTokenProvider()), httpx.Client(transport=transport),
    )
    with pytest.raises(RetryablePublicationError) as caught:
        client.activate_application_subscription(spec, "unit-primary", "unit-secondary")
    assert json.loads(str(caught.value).partition(": ")[2]) == {
        "category": "subscription_not_ready", "status_code": response_status,
        "correlation_id": correlation,
    }
    assert "unit-primary" not in str(caught.value) and "unsafe_body" not in str(caught.value)


@pytest.mark.parametrize("response_status", (200, 401))
def test_application_authentication_closes_response_without_reading_body(
    response_status: int,
) -> None:
    spec = _spec(
        provisioning_version=2, gateway_profile_id=APIM_ID,
        initial_monthly_token_limit=1000, initial_tokens_per_minute=100,
    )
    transport = ApplicationSubscriptionTransport(spec, "unit-primary", "unit-secondary")
    transport.data_plane_result = response_status
    responses: list[httpx.Response] = []

    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise AssertionError("Authentication probes must not read arbitrary response bodies")

    def handler(request: httpx.Request) -> httpx.Response:
        response = transport.handle_request(request)
        if request.url.host == transport.gateway_host:
            response = httpx.Response(response.status_code, stream=UnreadBody())
            responses.append(response)
        return response

    client = AzureApimPublisherClient(
        publisher_settings(), cast(Any, StubTokenProvider()),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    if response_status == 200:
        client.activate_application_subscription(spec, "unit-primary", "unit-secondary")
    else:
        with pytest.raises(RetryablePublicationError):
            client.activate_application_subscription(spec, "unit-primary", "unit-secondary")
    assert len(responses) == (2 if response_status == 200 else 1)
    assert all(response.is_closed and not response.is_stream_consumed for response in responses)


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


@pytest.mark.parametrize("subscription_id", ("unit-dashboard", "unit-publisher-probe"))
def test_custom_system_subscription_ids_cannot_be_queued(subscription_id: str) -> None:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(
        repository,
        CredentialCipher(Fernet.generate_key()),
        dashboard_subscription_id="unit-dashboard",
        probe_subscription_id="unit-publisher-probe",
    )
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
