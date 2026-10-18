"""Isolated creation tests: no live APIM, Table Storage, or database connections."""

from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from tests.backend.model_platform.control_plane_support import (
    APIM_ID,
    FakeApimClient,
    StubTokenProvider,
    publisher_settings,
)
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
    application_rate_limit_partition_key,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository_applications import PostgreSqlApplicationRepositoryMixin
from turnstile_core.security import CredentialCipher
from turnstile_core.services.application_access import ApplicationAccessService
from turnstile_core.services.application_provisioning_ledger import (
    prepare_application_ledger,
    read_application_rate_limit,
)
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    GatewayControlPlaneService,
)
from turnstile_core.services.gateway_release_operation_worker import GatewayReleaseOperationWorker


class StagedClient(FakeApimClient):
    def __init__(self) -> None:
        super().__init__()
        self.prepared = False
        self.activated = False

    def ensure_application_subscription(
        self, spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str, secondary_key: str,
    ) -> None:
        assert spec.provisioning_version == 2
        assert primary_key != secondary_key
        self.prepared = True

    def activate_application_subscription(
        self, spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str, secondary_key: str,
    ) -> None:
        assert self.prepared and primary_key != secondary_key
        self.activated = True


def provision_fixture() -> tuple[
    InMemoryRepository, CredentialCipher, StagedClient, UUID,
    GatewayApplicationSubscriptionProvisionSpec,
]:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(
        repository, cipher,
        application_default_token_limit=420_000,
        application_default_tokens_per_minute=24_000,
    )
    accepted = service.request_application_subscription_provision(
        APIM_ID, GatewayApplicationSubscriptionCreate(
            subscription_id="staged-application", display_name="Staged Application",
        ), "owner@example.com",
    )
    spec = GatewayApplicationSubscriptionProvisionSpec.model_validate(
        accepted.operation.semantic_preview
    )
    return repository, cipher, StagedClient(), accepted.operation.id, spec


def test_ledger_failure_keeps_subscription_inactive_and_credentials_retryable() -> None:
    repository, cipher, client, operation_id, spec = provision_fixture()
    available = False

    def project(_gateway: UUID, application_id: UUID) -> None:
        assert application_id == spec.application_id
        if not available:
            raise RetryablePublicationError("Table is unavailable")

    worker = GatewayReleaseOperationWorker(
        repository, client, cipher=cipher, application_projector=project,
        application_default_token_limit=1,
    )
    for _ in range(3):
        assert worker.run_once("worker") is not None
    retrying = worker.run_once("worker")
    assert retrying is not None and retrying.status == "verifying_readback"
    assert client.prepared and not client.activated
    assert repository.gateway_release_operation_secret(operation_id) is not None
    budget = next(iter(repository.gateway_application_budgets.values()))
    assert budget["token_limit"] == 420_000
    assert budget["tokens_per_minute"] == 24_000
    available = True
    completed = worker.run_once("worker")
    assert completed is not None and completed.status == "succeeded"
    assert completed.error_message is None
    assert client.activated
    assert repository.gateway_release_operation_secret(operation_id) is None


@pytest.mark.parametrize("budget", [
    {"budget_mode": "tokens", "token_limit": 250_000},
    {"budget_mode": "usd", "usd_limit": "12.50"},
])
def test_creation_snapshots_admin_budget_and_tags(budget: dict[str, Any]) -> None:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository, CredentialCipher(Fernet.generate_key()))
    request = GatewayApplicationSubscriptionCreate(
        subscription_id="admin-budget", display_name="Admin budget",
        tokens_per_minute=24_000, tags={"team": "finance", "environment": "test"},
        **budget,
    )
    accepted = service.request_application_subscription_provision(
        APIM_ID, request, "owner@example.com",
    )
    spec = GatewayApplicationSubscriptionProvisionSpec.model_validate(
        accepted.operation.semantic_preview,
    )
    assert spec.tags == request.tags
    assert spec.budget_mode == budget["budget_mode"]
    assert spec.initial_tokens_per_minute == 24_000
    assert spec.initial_monthly_token_limit == budget.get("token_limit")
    assert spec.initial_usd_limit == (Decimal("12.50") if budget["budget_mode"] == "usd" else None)
    client = StagedClient()

    def project(_gateway_id: UUID, application_id: UUID) -> None:
        stored = next(iter(repository.gateway_application_budgets.values()))
        assert stored["budget_mode"] == budget["budget_mode"]
        assert stored["token_limit"] == budget.get("token_limit")
        assert stored["usd_limit"] == spec.initial_usd_limit
        saved_application = repository.get_gateway_application(application_id)
        assert saved_application is not None and saved_application["tags"] == request.tags

    worker = GatewayReleaseOperationWorker(
        repository, client, cipher=service._cipher, application_projector=project,
    )
    for _ in range(4):
        completed = worker.run_once("worker")
    assert completed is not None and completed.status == "succeeded"
    assert client.activated
    stored_before = deepcopy(repository.gateway_application_budgets)
    service_view = ApplicationAccessService(repository, sync_available=True)
    assert service_view.application(spec.application_id).tags == request.tags
    service_view.provision(APIM_ID, spec, "owner@example.com")
    assert repository.gateway_application_budgets == stored_before


@pytest.mark.parametrize("budget_mode", ["tokens", "usd"])
def test_postgres_creation_persists_budget_tags_and_audit_atomically(budget_mode: str) -> None:
    _, _, _, _, original = provision_fixture()
    spec = original.model_copy(update={
        "budget_mode": budget_mode,
        "initial_monthly_token_limit": 250_000 if budget_mode == "tokens" else None,
        "initial_usd_limit": Decimal("12.50") if budget_mode == "usd" else None,
        "tags": {"team": "finance"},
    })
    connection = MagicMock()

    def execute(sql: str, parameters: tuple[object, ...] = ()) -> MagicMock:
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            {"id": spec.application_id} if "INSERT INTO gateway_application (" in sql
            else {"application_id": spec.application_id}
            if "INSERT INTO gateway_application_subscription" in sql else None
        )
        return cursor

    connection.execute.side_effect = execute
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = PostgreSqlApplicationRepositoryMixin()
    period = datetime.now(UTC).date().replace(day=1)
    with patch.object(repository, "_connection", return_value=context):
        result = repository.provision_gateway_application(
            APIM_ID, spec.model_dump(), "owner", period, 250_000, 24_000,
        )
    assert result["tags"] == {"team": "finance"}
    connection.transaction.assert_called_once()
    calls = connection.execute.call_args_list
    budget_call = next(
        call for call in calls if "INSERT INTO gateway_application_budget" in call.args[0]
    )
    assert budget_call.args[1][2:6] == (
        budget_mode, 250_000 if budget_mode == "tokens" else None,
        Decimal("12.50") if budget_mode == "usd" else None, 24_000,
    )
    tag_call = next(
        call for call in calls if "INSERT INTO gateway_application_tags" in call.args[0]
    )
    assert tag_call.args[1][1].obj == {"team": "finance"}
    audit_call = next(
        call for call in calls if "INSERT INTO gateway_application_audit" in call.args[0]
    )
    assert audit_call.args[1][1].obj["application"]["tags"] == {"team": "finance"}
    assert audit_call.args[1][1].obj["budget"]["budget_mode"] == budget_mode


@pytest.mark.parametrize("value", [
    {"budget_mode": "usd"},
    {"budget_mode": "usd", "usd_limit": "12.50", "token_limit": 100_000},
    {"budget_mode": "tokens", "usd_limit": "12.50"},
    {"budget_mode": "usd", "usd_limit": "0"},
    {"budget_mode": "usd", "usd_limit": "1.001"},
    {"token_limit": True}, {"token_limit": 1.5},
    {"tags": {"": "finance"}}, {"tags": {"team": "x" * 257}},
])
def test_creation_rejects_invalid_budget_units_and_tags(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GatewayApplicationSubscriptionCreate(
            subscription_id="invalid-budget", display_name="Invalid", **value,
        )


def test_no_ledger_configuration_fails_before_any_apim_creation() -> None:
    repository, cipher, client, operation_id, _ = provision_fixture()
    worker = GatewayReleaseOperationWorker(repository, client, cipher=cipher)
    assert worker.run_once("worker") is not None
    failed = worker.run_once("worker")
    assert failed is not None and failed.status == "failed"
    assert not client.prepared
    assert repository.gateway_release_operation_secret(operation_id) is None


@pytest.mark.parametrize(
    "subscription_id", ["master", "finops-dashboard", "finops-publisher-probe"]
)
def test_system_subscription_ids_cannot_be_provisioned(subscription_id: str) -> None:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository, CredentialCipher(Fernet.generate_key()))
    with pytest.raises(ControlPlaneConflictError, match="reserved"):
        service.request_application_subscription_provision(
            APIM_ID, GatewayApplicationSubscriptionCreate(
                subscription_id=subscription_id, display_name="Do not overwrite",
            ), "owner@example.com",
        )
    assert repository.gateway_release_operations == []


def test_disabled_gateway_cannot_accept_creation() -> None:
    repository = InMemoryRepository()
    gateway = next(item for item in repository.gateways if item["id"] == APIM_ID)
    gateway["enabled"] = False
    service = GatewayControlPlaneService(repository, CredentialCipher(Fernet.generate_key()))
    with pytest.raises(ControlPlaneConflictError, match="disabled"):
        service.request_application_subscription_provision(
            APIM_ID, GatewayApplicationSubscriptionCreate(
                subscription_id="disabled-target", display_name="Disabled",
            ), "owner@example.com",
        )


@pytest.mark.parametrize("catalog_operation_id", ["models", "anthropic-models"])
@pytest.mark.parametrize("identity_prefix", [False, True])
def test_staged_apim_creation_is_inactive_until_etag_guarded_activation_readback(
    catalog_operation_id: str,
    identity_prefix: bool,
) -> None:
    _, _, _, _, spec = provision_fixture()
    requests: list[httpx.Request] = []
    current: dict[str, Any] | None = None
    root = ("/subscriptions/00000000-0000-0000-0000-000000000001/"
            "resourceGroups/rg-finops/providers/Microsoft.ApiManagement/service/apim-finops")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current
        requests.append(request)
        if request.url.host == "apim-finops.azure-api.net":
            assert request.method == "GET" and request.url.path == "/turnstile/llm/v1/models"
            return httpx.Response(200 if request.headers.get("Ocp-Apim-Subscription-Key") else 401,
                                  json={"data": []})
        if request.url.path == root + "/apis/turnstile-llm/operations":
            return httpx.Response(200, json={"value": [{
                "name": catalog_operation_id,
                "properties": {"method": "GET", "urlTemplate": "/v1/models"},
            }]})
        if request.url.path.endswith("/operations/" + catalog_operation_id + "/policies/policy"):
            prefix = ''.join(
                f'<set-variable name="{name}" value="unit" />' for name in (
                    "availabilitySurfaceDigest", "availabilityTargetId", "availabilityApiId",
                    "availabilityApiRevision", "availabilitySurfaceKind",
                    "availabilityCapabilityDigest",
                )
            ) if identity_prefix else ''
            return httpx.Response(200, json={"properties": {"value": (
                '<policies><inbound>' + prefix + '<base /><return-response /></inbound></policies>'
            )}})
        if request.url.path.endswith("/operations/" + catalog_operation_id):
            return httpx.Response(200, json={"properties": {
                "method": "GET", "urlTemplate": "/v1/models",
            }})
        if request.url.path == root + "/apis/turnstile-llm":
            return httpx.Response(200, json={"properties": {"path": "turnstile/llm"}})
        if request.url.path.endswith("/policies/policy"):
            return httpx.Response(200, json={"properties": {"value": admission_policy(APIM_ID)}})
        if request.url.path.endswith("/products/turnstile-ai-consumers/apis/turnstile-llm"):
            assert request.method == "HEAD"
            return httpx.Response(204)
        if "/products/" in request.url.path:
            return httpx.Response(200, json={
                "id": root + "/products/turnstile-ai-consumers",
                "properties": {"state": "published", "subscriptionRequired": True},
            })
        if request.method == "GET":
            return httpx.Response(404) if current is None else httpx.Response(
                200, json={"properties": current}, headers={"ETag": '"unit-etag"'}
            )
        properties = json.loads(request.content)["properties"]
        if current is None:
            assert properties["state"] == "suspended"
            assert properties["displayName"].startswith("Turnstile pending ")
            assert "If-Match" not in request.headers
        else:
            assert request.headers["If-Match"] == '"unit-etag"'
        current = properties
        return httpx.Response(201, json={"properties": current})

    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.ensure_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
    client.ensure_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
    assert current is not None and current["state"] == "suspended"
    assert len([request for request in requests if request.method == "PUT"]) == 1
    client.activate_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
    assert current["state"] == "active"
    assert current["displayName"] == spec.display_name
    client.activate_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
    assert len([request for request in requests if request.method == "PUT"]) == 2
    assert requests[-1].method == "GET"
    assert all("listSecrets" not in str(request.url) for request in requests)


class ApplicationSubscriptionTransport(httpx.MockTransport):
    def __init__(
        self, spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str, secondary_key: str, initial_state: str = "active",
    ) -> None:
        self.spec = spec
        self.primary_key = primary_key
        self.secondary_key = secondary_key
        self.requests: list[httpx.Request] = []
        self.data_plane_result: int | type[httpx.TransportError] = 200
        self.anonymous_status = 401
        self.response_headers: dict[str, str] = {}
        self.api_path = "turnstile/llm"
        self.catalog_operation = {"method": "GET", "urlTemplate": "/v1/models"}
        self.catalog_policy = '<policies><inbound><base /><return-response /></inbound></policies>'
        self.root = ("/subscriptions/00000000-0000-0000-0000-000000000001/"
                     "resourceGroups/rg-finops/providers/Microsoft.ApiManagement/service/apim-finops")
        self.current: dict[str, Any] | None = None if initial_state == "missing" else {
            "displayName": spec.display_name if initial_state == "active" else
                AzureApimPublisherClient._application_pending_name(primary_key, secondary_key),
            "state": initial_state,
            "scope": self.root + "/products/turnstile-ai-consumers",
        }
        super().__init__(self._respond)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "apim-finops.azure-api.net":
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
            status = self.data_plane_result if key else self.anonymous_status
            assert isinstance(status, int)
            return httpx.Response(status, json={"data": [], "unsafe_body": self.primary_key},
                                  headers=self.response_headers)
        if request.url.path == self.root + "/apis/turnstile-llm/operations":
            return httpx.Response(200, json={"value": [{
                "name": "anthropic-models", "properties": self.catalog_operation,
            }]})
        if request.url.path.endswith("/operations/anthropic-models/policies/policy"):
            return httpx.Response(200, json={"properties": {"value": self.catalog_policy}})
        if request.url.path.endswith("/operations/anthropic-models"):
            return httpx.Response(200, json={"properties": self.catalog_operation})
        if request.url.path.endswith("/policies/policy"):
            return httpx.Response(200, json={"properties": {"value": admission_policy(APIM_ID)}})
        if request.url.path == self.root + "/apis/turnstile-llm":
            return httpx.Response(200, json={"properties": {"path": self.api_path}})
        if request.method == "HEAD" and "/products/" in request.url.path:
            return httpx.Response(204)
        if request.url.path == self.root + "/products/turnstile-ai-consumers":
            return httpx.Response(200, json={"properties": {
                "state": "published", "subscriptionRequired": True,
            }})
        assert request.url.path == self.root + "/subscriptions/" + self.spec.apim_subscription_id
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
            200, json={"properties": self.current}, headers={"ETag": '"unit-etag"'},
        )


@pytest.mark.parametrize("already_active", [False, True])
def test_application_activation_waits_for_data_plane_authentication(
    already_active: bool,
) -> None:
    _, _, _, _, spec = provision_fixture()
    transport = ApplicationSubscriptionTransport(
        spec, "primary-unit-key", "secondary-unit-key", "active" if already_active else "suspended",
    )
    transport.data_plane_result = 401

    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(),
        httpx.Client(transport=transport),
    )
    with pytest.raises(RetryablePublicationError):
        client.activate_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
    assert transport.current is not None and transport.current["state"] == "active"
    expected_writes = 0 if already_active else 1
    assert sum(request.method == "PUT" for request in transport.requests) == expected_writes
    transport.data_plane_result = 200
    client.activate_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
    assert sum(request.method == "PUT" for request in transport.requests) == expected_writes
    probes = [
        request for request in transport.requests
        if request.url.host == "apim-finops.azure-api.net"
        and request.headers.get("Ocp-Apim-Subscription-Key")
    ]
    assert len(probes) == 2
    assert all(request.method in {"GET", "PUT"} for request in transport.requests)


@pytest.mark.parametrize("recover_after_rejection", [False, True])
@pytest.mark.parametrize("outcome", [
    401, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError,
])
def test_application_worker_retains_readiness_checkpoint_until_success_or_retry_limit(
    outcome: int | type[httpx.TransportError], recover_after_rejection: bool,
) -> None:
    repository, cipher, _, operation_id, spec = provision_fixture()
    ciphertext = repository.gateway_release_operation_secret(operation_id)
    plaintext = cipher.decrypt(ciphertext)
    assert plaintext is not None
    keys = json.loads(plaintext)
    transport = ApplicationSubscriptionTransport(
        spec, keys["primary_key"], keys["secondary_key"], "missing",
    )
    transport.data_plane_result = outcome
    transport.response_headers = {"apim-request-id": "b99f1744-21f9-428d-b37a-a99070363706"}
    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(), httpx.Client(transport=transport),
    )
    ledger = UnitLedger()
    worker = GatewayReleaseOperationWorker(
        repository, client, cipher=cipher,
        application_projector=lambda gateway, application: prepare_application_ledger(
            repository, ledger, gateway, application,
        ),
    )
    max_attempts = 6 if recover_after_rejection else 5
    for _ in range(3):
        pending = worker.run_once("worker", max_attempts=max_attempts)
    assert pending is not None and pending.status == "verifying_readback"
    checkpoint = dict(pending.checkpoint)
    assert checkpoint["application_materialized"] and checkpoint["apim_subscription_created"]
    budgets = deepcopy(repository.gateway_application_budgets)
    for _ in range(2):
        pending = worker.run_once("worker", max_attempts=max_attempts)
        assert pending is not None and pending.status == "verifying_readback"
        assert not pending.checkpoint.get("subscription_active")
        assert not pending.checkpoint.get("data_plane_authentication_ready")
        assert all(pending.checkpoint[name] == value for name, value in checkpoint.items())
        assert repository.gateway_release_operation_secret(operation_id) == ciphertext
        assert pending.error_message is not None
        diagnostic = json.loads(pending.error_message.partition(": ")[2])
        assert set(diagnostic) == {"category", "status_code", "correlation_id"}
        assert diagnostic["status_code"] == (outcome if isinstance(outcome, int) else None)
        assert diagnostic["category"] == (
            "subscription_not_ready" if isinstance(outcome, int) else
            "timeout" if issubclass(outcome, httpx.TimeoutException) else "transport_error"
        )
        assert diagnostic["correlation_id"] == (
            transport.response_headers["apim-request-id"] if isinstance(outcome, int) else None
        )
        serialized = pending.model_dump_json()
        assert keys["primary_key"] not in serialized and keys["secondary_key"] not in serialized
        assert "unsafe_body" not in serialized and "unsafe transport detail" not in serialized
    if recover_after_rejection:
        transport.data_plane_result = 200
    completed = worker.run_once("recovered-worker", max_attempts=max_attempts)
    assert completed is not None
    assert completed.status == ("succeeded" if recover_after_rejection else "failed")
    assert all(completed.checkpoint[name] == value for name, value in checkpoint.items())
    if recover_after_rejection:
        assert completed.error_message is None
        assert completed.checkpoint["admission_ready"]
        assert completed.checkpoint["subscription_active"]
        assert completed.checkpoint["data_plane_authentication_ready"]
        assert not completed.checkpoint["key_stored"]
    else:
        assert completed.error_code == "max_attempts_exceeded"
        assert completed.checkpoint["max_attempts_exceeded"]
        assert not completed.checkpoint.get("subscription_active")
    assert repository.gateway_release_operation_secret(operation_id) is None
    assert repository.gateway_application_budgets == budgets
    assert sum(request.method == "PUT" for request in transport.requests) == 2
    probes = [
        request for request in transport.requests
        if request.url.host == "apim-finops.azure-api.net"
    ]
    assert len(probes) == (4 if recover_after_rejection else 2)
    assert all(request.method == "GET" for request in probes)
    assert all("listSecrets" not in str(request.url) and "regenerate" not in str(request.url)
               for request in transport.requests)
    request_count = len(transport.requests)
    assert worker.run_once("worker", max_attempts=6) is None
    assert len(transport.requests) == request_count


def test_application_auth_readiness_lost_lease_preserves_recovery_keys() -> None:
    repository, cipher, _, operation_id, spec = provision_fixture()
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
        if (lose_lease and request.url.host == "apim-finops.azure-api.net"
                and request.headers.get("Ocp-Apim-Subscription-Key")):
            operation = repository.get_gateway_release_operation(operation_id)
            assert operation is not None
            operation["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
        return response

    ledger = UnitLedger()
    worker = GatewayReleaseOperationWorker(
        repository,
        AzureApimPublisherClient(publisher_settings(), StubTokenProvider(),
                                 httpx.Client(transport=httpx.MockTransport(handler))),
        cipher=cipher,
        application_projector=lambda gateway, application: prepare_application_ledger(
            repository, ledger, gateway, application,
        ),
    )
    for _ in range(3):
        assert worker.run_once("worker") is not None
    with pytest.raises(RuntimeError, match="changed while leased"):
        worker.run_once("worker")
    operation = repository.get_gateway_release_operation(operation_id)
    assert operation is not None and operation["status"] == "verifying_readback"
    assert operation["checkpoint"]["application_materialized"]
    assert not operation["checkpoint"].get("subscription_active")
    assert repository.gateway_release_operation_secret(operation_id) == ciphertext
    lose_lease = False
    completed = worker.run_once("replacement-worker")
    assert completed is not None and completed.status == "succeeded"
    assert completed.checkpoint["data_plane_authentication_ready"]
    assert repository.gateway_release_operation_secret(operation_id) is None
    assert sum(request.method == "PUT" for request in transport.requests) == 2
    keyed_probes = [request for request in transport.requests
                    if request.url.host == "apim-finops.azure-api.net"
                    and request.headers.get("Ocp-Apim-Subscription-Key")]
    assert len(keyed_probes) == 2


@pytest.mark.parametrize("problem", [
    "api_path", "http", "query", "fragment", "userinfo", "post_operation", "inference_path",
    "missing_base", "late_base", "authentication_variable", "invalid_xml",
])
def test_application_auth_probe_rejects_untrusted_catalog_target(problem: str) -> None:
    _, _, _, _, spec = provision_fixture()
    transport = ApplicationSubscriptionTransport(spec, "primary-unit-key", "secondary-unit-key")
    settings = publisher_settings()
    gateway_urls = {
        "api_path": "https://apim-finops.azure-api.net/another-api",
        "http": "http://apim-finops.azure-api.net/turnstile/llm",
        "query": "https://apim-finops.azure-api.net/turnstile/llm?subscription-key=wrong-unit-key",
        "fragment": "https://apim-finops.azure-api.net/turnstile/llm#fragment",
        "userinfo": "https://username:password@apim-finops.azure-api.net/turnstile/llm",
    }
    if problem in gateway_urls:
        settings = settings.model_copy(update={"apim_gateway_url": gateway_urls[problem]})
    elif problem == "post_operation":
        transport.catalog_operation["method"] = "POST"
    elif problem == "inference_path":
        transport.catalog_operation["urlTemplate"] = "/v1/messages"
    elif problem == "missing_base":
        transport.catalog_policy = '<policies><inbound><return-response /></inbound></policies>'
    elif problem == "late_base":
        transport.catalog_policy = (
            '<policies><inbound><return-response /><base /></inbound></policies>'
        )
    elif problem == "authentication_variable":
        transport.catalog_policy = (
            '<policies><inbound><set-variable name="hasEmployeeToken" value="@(true)" />'
            '<base /><return-response /></inbound></policies>'
        )
    else:
        transport.catalog_policy = "not xml"
    client = AzureApimPublisherClient(
        settings, StubTokenProvider(), httpx.Client(transport=transport),
    )
    with pytest.raises(PolicyCompilationError):
        client.activate_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
    assert all(request.method == "GET" and request.url.host == "management.azure.com"
               for request in transport.requests)


@pytest.mark.parametrize("anonymous_status", [200, 302, 401, 403, 429, 503])
def test_application_catalog_authentication_cannot_be_anonymous_or_inherited(
    anonymous_status: int,
) -> None:
    _, _, _, _, spec = provision_fixture()
    transport = ApplicationSubscriptionTransport(spec, "primary-unit-key", "secondary-unit-key")
    transport.api_path = "private-prefix/gateway"
    transport.anonymous_status = anonymous_status
    transport.response_headers = {"Set-Cookie": "session=untrusted", "Location": "https://untrusted.example/"}
    settings = publisher_settings().model_copy(update={
        "apim_gateway_url": "https://apim-finops.azure-api.net/private-prefix/gateway/",
    })
    client = AzureApimPublisherClient(settings, StubTokenProvider(), httpx.Client(
        transport=transport, timeout=None, follow_redirects=True,
        auth=("shared-user", "shared-password"),
        headers={
            "Authorization": "Bearer shared-token", "Ocp-Apim-Subscription-Key": "shared-unit-key",
        },
        cookies={"session": "shared-cookie"}, params={"subscription-key": "shared-unit-key"},
    ))
    if anonymous_status in {401, 403}:
        client.activate_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
    else:
        expected = PolicyCompilationError if anonymous_status == 200 else RetryablePublicationError
        with pytest.raises(expected) as caught:
            client.activate_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
        assert "primary-unit-key" not in str(caught.value)
        assert "unsafe_body" not in str(caught.value)
    probes = [
        request for request in transport.requests if request.url.host != "management.azure.com"
    ]
    assert len(probes) == 2
    assert [request.headers.get("Ocp-Apim-Subscription-Key") for request in probes] == [
        "primary-unit-key", None,
    ]
    assert all(
        request.url.host == "apim-finops.azure-api.net" and request.method == "GET"
        for request in probes
    )
    assert all(request.method == "GET" for request in transport.requests)


@pytest.mark.parametrize(("status", "headers", "correlation"), [
    (401, {"apim-request-id": "b99f1744-21f9-428d-b37a-a99070363706"},
     "b99f1744-21f9-428d-b37a-a99070363706"),
    (403, {"x-request-id": "ba4b0825-374e-4b60-bf0f-1bdce47f090a"},
     "ba4b0825-374e-4b60-bf0f-1bdce47f090a"),
    (404, {"x-correlation-id": "338bdb45-c47a-47d1-b7b2-6f526d7eb934"},
     "338bdb45-c47a-47d1-b7b2-6f526d7eb934"),
    (408, {}, None),
    (429, {"x-request-id": "primary-unit-key"}, None),
    (503, {"apim-request-id": "untrusted raw diagnostic"}, None),
    (302, {"Location": "https://untrusted.example/"}, None),
])
def test_application_auth_probe_preserves_only_safe_failure_evidence(
    status: int, headers: dict[str, str], correlation: str | None,
) -> None:
    _, _, _, _, spec = provision_fixture()
    transport = ApplicationSubscriptionTransport(spec, "primary-unit-key", "secondary-unit-key")
    transport.data_plane_result = status
    transport.response_headers = headers
    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(),
        httpx.Client(transport=transport, follow_redirects=True),
    )
    with pytest.raises(RetryablePublicationError) as caught:
        client.activate_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
    diagnostic = json.loads(str(caught.value).partition(": ")[2])
    assert diagnostic == {
        "category": "subscription_not_ready", "status_code": status, "correlation_id": correlation,
    }
    assert "primary-unit-key" not in str(caught.value) and "unsafe_body" not in str(caught.value)
    probes = [
        request for request in transport.requests if request.url.host != "management.azure.com"
    ]
    assert len(probes) == 1 and probes[0].method == "GET"


@pytest.mark.parametrize("status", [200, 401])
def test_application_auth_probe_closes_responses_without_reading_bodies(status: int) -> None:
    _, _, _, _, spec = provision_fixture()
    transport = ApplicationSubscriptionTransport(spec, "primary-unit-key", "secondary-unit-key")
    transport.data_plane_result = status
    streams: list[httpx.Response] = []

    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise AssertionError("Authentication probes must not consume arbitrary response bodies")

    def handler(request: httpx.Request) -> httpx.Response:
        response = transport.handle_request(request)
        if request.url.host == "apim-finops.azure-api.net":
            response = httpx.Response(response.status_code, stream=UnreadBody())
            streams.append(response)
        return response

    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    if status == 200:
        client.activate_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
    else:
        with pytest.raises(RetryablePublicationError):
            client.activate_application_subscription(spec, "primary-unit-key", "secondary-unit-key")
    assert len(streams) == (2 if status == 200 else 1)
    assert all(response.is_closed and not response.is_stream_consumed for response in streams)


def test_same_name_existing_subscription_is_not_mistaken_for_our_generated_keys() -> None:
    _, _, _, _, spec = provision_fixture()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/policies/policy"):
            return httpx.Response(200, json={"properties": {"value": admission_policy(APIM_ID)}})
        if request.url.path.endswith("/products/turnstile-ai-consumers/apis/turnstile-llm"):
            assert request.method == "HEAD"
            return httpx.Response(204)
        if "/products/" in request.url.path:
            return httpx.Response(200, json={"properties": {
                "state": "published", "subscriptionRequired": True,
            }})
        root = str(request.url).split("/subscriptions/staged-application")[0]
        return httpx.Response(200, json={"properties": {
            "displayName": spec.display_name, "state": "active",
            "scope": root.replace("https://management.azure.com", "")
                     + "/products/turnstile-ai-consumers",
        }})

    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PolicyCompilationError, match="already exists"):
        client.ensure_application_subscription(spec, "new-primary", "new-secondary")
    assert all(request.method in {"GET", "HEAD"} for request in requests)


class UnitLedger(TableStorageLedger):
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.versions: dict[tuple[str, str], int] = {}

    def upsert(self, partition: str, row_key: str, entity: dict[str, Any]) -> None:
        self.rows[(partition, row_key)] = dict(entity)
        self.versions[(partition, row_key)] = self.versions.get((partition, row_key), 0) + 1

    def insert_if_missing(self, partition: str, row_key: str, entity: dict[str, Any]) -> None:
        if (partition, row_key) not in self.rows:
            self.upsert(partition, row_key, entity)

    def read_entity(self, partition: str, row_key: str) -> dict[str, Any] | None:
        return self.rows.get((partition, row_key))

    def read_versioned_entity(
        self, partition: str, row_key: str,
    ) -> tuple[dict[str, Any], str] | None:
        row = self.read_entity(partition, row_key)
        return (dict(row), str(self.versions[(partition, row_key)])) if row is not None else None

    def replace_if_match(
        self, partition: str, row_key: str, entity: dict[str, Any], etag: str | None,
    ) -> bool:
        current = self.read_versioned_entity(partition, row_key)
        if (current[1] if current is not None else None) != etag:
            return False
        self.upsert(partition, row_key, entity)
        return True


def test_initial_admission_projection_does_not_reset_consumption_or_other_consumers() -> None:
    repository, cipher, client, _, spec = provision_fixture()
    ledger = UnitLedger()
    worker = GatewayReleaseOperationWorker(
        repository, client, cipher=cipher,
        application_projector=lambda gateway, application: prepare_application_ledger(
            repository, ledger, gateway, application
        ),
    )
    for _ in range(3):
        assert worker.run_once("worker") is not None
    partition = application_partition_key(spec.application_id, datetime.now(UTC).strftime("%Y-%m"))
    prepare_application_ledger(repository, ledger, APIM_ID, spec.application_id)
    assert ledger.rows[(application_rate_limit_partition_key(spec.application_id), "T")] == {
        "TokensPerMinute": 24_000,
    }
    quota_revision = ledger.rows[(partition, "Q")]["ConfigurationUpdatedAt"]
    assert datetime.fromisoformat(quota_revision).tzinfo is not None
    ledger.rows[(partition, "C")] = {"ConfirmedUsed": 123}
    ledger.rows[(partition, "R|unit")] = {"Reserved": 456}
    ledger.rows[("other-consumer", "Q")] = {"Limit": 999}
    completed = worker.run_once("worker")
    assert completed is not None and completed.status == "succeeded"
    assert ledger.rows[(partition, "Q")] == {
        "Limit": 420_000, "TokensPerMinute": 24_000, "Enforce": True,
        "ConfigurationUpdatedAt": quota_revision,
    }
    assert ledger.rows[(partition, "C")] == {"ConfirmedUsed": 123}
    assert ledger.rows[(partition, "R|unit")] == {"Reserved": 456}
    assert ledger.rows[("other-consumer", "Q")] == {"Limit": 999}
    assert ledger.rows[(partition, "M")] == {"Configured": True, "Models": "||"}


def test_usd_creation_projects_identity_and_tpm_without_a_competing_token_limit() -> None:
    repository, cipher, client, _, spec = provision_fixture()
    worker = GatewayReleaseOperationWorker(
        repository, client, cipher=cipher, application_projector=lambda *_: None,
    )
    for _ in range(3):
        assert worker.run_once("worker") is not None
    budget = next(iter(repository.gateway_application_budgets.values()))
    budget.update(budget_mode="usd", token_limit=None, usd_limit=Decimal("12.50"))
    ledger = UnitLedger()
    ledger.rows[("other-consumer", "Q")] = {"Limit": 999}
    prepare_application_ledger(repository, ledger, APIM_ID, spec.application_id)
    partition = application_partition_key(spec.application_id, datetime.now(UTC).strftime("%Y-%m"))
    assert (partition, "Q") not in ledger.rows
    assert (partition, "C") not in ledger.rows
    assert ledger.rows[(partition, "M")] == {"Configured": True, "Models": "||"}
    assert ledger.rows[(application_rate_limit_partition_key(spec.application_id), "T")] == {
        "TokensPerMinute": 24_000,
    }
    assert ledger.rows[("other-consumer", "Q")] == {"Limit": 999}


def test_insert_missing_ledger_entity_does_not_replace_existing_confirmed_row() -> None:
    calls: list[str] = []

    class StorageTokenProvider:
        def token(self, resource: str) -> str:
            assert resource == "https://storage.azure.com/"
            return "unit-storage-token"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(409)

    store = TableStorageLedger(
        "https://unit.table.core.windows.net", "TurnstileLedger",
        StorageTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    store.insert_if_missing("app|unit|2026-09", "C", {"ConfirmedUsed": 0})
    assert calls == ["POST"]


def admission_policy(gateway_id: UUID) -> str:
    return (
        '<policies><inbound><set-variable name="applicationMapPartition" '
        f'value="@(&quot;app-map|{gateway_id}&quot;)" />'
        f'<set-variable name="telemetryGatewayProfileId" value="{gateway_id}" />'
        '</inbound></policies>'
    )


@pytest.mark.parametrize("content_type", [
    "application/vnd.ms-azure-apim.policy.raw+xml; charset=utf-8", "application/xml",
])
def test_policy_read_keeps_xml_entities_until_the_xml_parser(content_type: str) -> None:
    _, _, _, _, spec = provision_fixture()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.params["format"] == "rawxml"
        return httpx.Response(
            200, text=admission_policy(APIM_ID), headers={"Content-Type": content_type}
        )

    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client._validate_application_admission_target(spec)


def test_mismatched_gateway_policy_fails_before_any_subscription_write() -> None:
    _, _, _, _, spec = provision_fixture()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"properties": {"value": admission_policy(
            UUID("10000000-0000-4000-8000-000000000002")
        )}})

    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PolicyCompilationError, match="selected gateway"):
        client.ensure_application_subscription(spec, "primary-unit", "secondary-unit")
    assert calls == ["GET"]


@pytest.mark.parametrize("problem", ["unpublished", "no_subscription", "missing_api"])
def test_unready_product_never_creates_a_subscription(problem: str) -> None:
    _, _, _, _, spec = provision_fixture()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.url.path.endswith("/policies/policy"):
            return httpx.Response(200, json={"properties": {"value": admission_policy(APIM_ID)}})
        if request.url.path.endswith("/apis/turnstile-llm"):
            assert request.method == "HEAD"
            return httpx.Response(404)
        return httpx.Response(200, json={"properties": {
            "state": "notPublished" if problem == "unpublished" else "published",
            "subscriptionRequired": problem != "no_subscription",
        }})

    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PolicyCompilationError, match="Product"):
        client.ensure_application_subscription(spec, "primary-unit", "secondary-unit")
    assert set(calls) <= {"GET", "HEAD"}
    assert calls.count("HEAD") == (1 if problem == "missing_api" else 0)


def test_legacy_queued_operations_still_complete_without_a_ledger_projector() -> None:
    repository, cipher, _, operation_id, _ = provision_fixture()
    operation = repository.get_gateway_release_operation(operation_id)
    assert operation is not None
    # Construct a historical version-one queue payload in this isolated repository double.
    for name in (
        "provisioning_version", "gateway_profile_id", "initial_monthly_token_limit",
        "initial_tokens_per_minute",
    ):
        operation["semantic_preview"].pop(name)

    class LegacyClient(FakeApimClient):
        def ensure_application_subscription(
            self, spec: GatewayApplicationSubscriptionProvisionSpec,
            primary_key: str, secondary_key: str,
        ) -> None:
            assert spec.provisioning_version == 1
            assert primary_key != secondary_key

    worker = GatewayReleaseOperationWorker(
        repository, LegacyClient(), cipher=cipher,
        application_default_token_limit=7_000,
        application_default_tokens_per_minute=900,
    )
    for _ in range(3):
        completed = worker.run_once("worker")
    assert completed is not None and completed.status == "succeeded"
    assert repository.gateway_release_operation_secret(operation_id) is None
    budget = next(iter(repository.gateway_application_budgets.values()))
    assert budget["token_limit"] == 7_000
    assert budget["tokens_per_minute"] == 900


def test_lost_lease_cannot_delete_keys_before_a_terminal_transition() -> None:
    repository, _, _, operation_id, _ = provision_fixture()
    assert repository.claim_gateway_release_operation("worker", 180) is not None
    changed = repository.transition_gateway_release_operation(
        operation_id, "queued", "failed", {}, "another-worker"
    )
    assert changed is None
    assert repository.gateway_release_operation_secret(operation_id) is not None
    changed = repository.transition_gateway_release_operation(
        operation_id, "queued", "failed", {}, "worker"
    )
    assert changed is not None
    assert repository.gateway_release_operation_secret(operation_id) is None

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


class AdmissionRows(UnitLedger):
    def __init__(self, missing: str | None = None) -> None:
        super().__init__()
        self.missing = missing

    def upsert(self, partition: str, row_key: str, entity: dict[str, Any]) -> None:
        super().upsert(partition, row_key, entity)

    def insert_if_missing(self, partition: str, row_key: str, entity: dict[str, Any]) -> None:
        super().insert_if_missing(partition, row_key, entity)

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


@pytest.mark.parametrize("value", [0, -1, True, "100", 2147483648])
def test_rate_readback_rejects_invalid_values(value: object) -> None:
    store = Mock(spec=TableStorageLedger)
    store.read_versioned_entity.return_value = ({"TokensPerMinute": value}, '"unit"')
    with pytest.raises(ValueError, match="invalid"):
        read_application_rate_limit(store, UUID(int=1))


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
    assert store.rows[(application_rate_limit_partition_key(spec.application_id), "T")] == {
        "TokensPerMinute": 100,
    }
    assert len(store.rows) == 7


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
