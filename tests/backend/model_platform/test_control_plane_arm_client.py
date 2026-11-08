from __future__ import annotations

import json
from datetime import UTC, datetime
from html import escape, unescape
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest
from pydantic import HttpUrl, SecretStr

from tests.backend.model_platform.control_plane_support import (
    APIM_ID,
    ROOT,
    FakeApimClient,
    GatewayControlPlaneService,
    GatewayPublicationWorker,
    StubTokenProvider,
    bedrock_publication,
    foundry_publication,
    publisher_settings,
)
from tests.backend.model_platform.test_gateway_targets import TARGET, target
from turnstile_core.domain.control_plane import (
    PUBLICATION_REQUIRED_ACTIONS,
    ApiFormat,
    GatewayBackendPoolConfig,
    GatewayBackendPoolMember,
    GatewayPublication,
    GatewayPublicationCreate,
    GatewayRateLimitCircuitBreaker,
    GatewayRateLimitResilience,
    ModelCreateTarget,
    RuntimeTarget,
    publication_model_id,
)
from turnstile_core.domain.gateway_targets import (
    GatewayDeploymentScope,
    GatewayServingDeployment,
    SelfHostedGatewayBootstrapRequest,
    SelfHostedGatewayCreate,
    SelfHostedGatewayProvisionSpec,
    gateway_deployment_id_for,
    self_hosted_gateway_native_id_for,
)
from turnstile_core.domain.runtime_models import OAuthClientCredentialsConfig, ProviderTarget
from turnstile_core.domain.storage_routes import (
    ConnectionRevision,
    RouteBackendPool,
    RouteCredential,
    RoutePoolMember,
    RouteScope,
    document_digest,
)
from turnstile_core.integrations.apim_control_plane import (
    ApimPolicyCompiler,
    AuthorizationRequiredError,
    AzureApimPublisherClient,
    BackendCircuitBreakerResource,
    BackendPoolMemberResource,
    BackendPoolResource,
    BackendResource,
    NamedValueResource,
    PolicyCompilationError,
    RetryablePublicationError,
)
from turnstile_core.integrations.apim_control_plane_contract import (
    CredentialSourceMissingError,
    OAuthCredentialResource,
    OperationResource,
    PublisherPermissionError,
    missing_management_actions,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository_support import ModelActivationValidator


@pytest.mark.parametrize("defect", [
    None, "wrong-resource", "method", "duplicate", "missing-policy",
])
def test_fixed_route_operation_reader_is_bounded_readonly_and_identity_bound(
    defect: str | None,
) -> None:
    settings = publisher_settings()
    policy = "<policies><inbound><base/></inbound><backend><base/></backend></policies>"
    requests: list[httpx.Request] = []
    resource = (f"/subscriptions/{settings.azure_subscription_id}/resourceGroups/"
                f"{settings.apim_resource_group}/providers/Microsoft.ApiManagement/service/"
                f"{settings.apim_service_name}/apis/{settings.apim_api_id};rev=1/operations/")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/operations"):
            row: dict[str, Any] = {
                "id": resource + "microsoft-decision", "name": "microsoft-decision",
                "properties": {
                    "method": "POST", "urlTemplate": "/providers/microsoft/v1/systemone",
                },
            }
            if defect == "wrong-resource":
                row["id"] = resource.replace(";rev=1", ";rev=2") + "microsoft-decision"
            if defect == "method":
                row["properties"]["method"] = "DELETE"
            return httpx.Response(200, json={
                "value": [row, row] if defect == "duplicate" else [row],
            })
        if defect == "missing-policy":
            return httpx.Response(404)
        assert request.url.params["format"] == "rawxml"
        return httpx.Response(200, json={"properties": {"value": policy}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        tokens = Mock()
        tokens.token.return_value = "unit-only-token"
        client = AzureApimPublisherClient(
            settings, token_provider=tokens, client=transport,
        )
        if defect:
            with pytest.raises(ValueError, match="Fixed program operation"):
                client.route_program_operations("1")
        else:
            assert client.route_program_operations("1") == {
                "microsoft-decision": ("POST", "/providers/microsoft/v1/systemone", policy),
            }
    assert len(requests) <= 2


@pytest.mark.parametrize("defect", [None, "revision", "etag", "release"])
def test_fixed_program_promotion_creates_only_new_release_with_unchanged_base(
    defect: str | None,
) -> None:
    settings = publisher_settings()
    requests: list[httpx.Request] = []
    reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/apis/" + settings.apim_api_id):
            reads += 1
            return httpx.Response(200, json={"properties": {
                "apiRevision": "changed" if defect == "revision" else "1",
            }}, headers={"ETag": '"changed"' if defect == "etag" and reads == 2 else '"unit"'})
        if request.method == "GET" and request.url.path.endswith("/releases/unit-release"):
            return httpx.Response(200 if defect == "release" else 404,
                                  json={"error": {"code": "ResourceNotFound"}})
        assert request.method == "PUT" and request.url.path.endswith("/releases/unit-release")
        assert request.headers["If-None-Match"] == "*"
        assert "if-match" not in request.headers
        assert json.loads(request.content)["properties"]["apiId"].endswith(
            "/apis/" + settings.apim_api_id + ";rev=2",
        )
        return httpx.Response(201, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        tokens = Mock()
        tokens.token.return_value = "unit-only-token"
        management = AzureApimPublisherClient(settings, token_provider=tokens, client=transport)
        if defect:
            with pytest.raises(ValueError, match="Fixed program promotion"):
                management.promote_fixed_revision("2", "unit-release", "1")
        else:
            management.promote_fixed_revision("2", "unit-release", "1")
    assert sum(request.method == "PUT" for request in requests) == (0 if defect else 1)


def test_fixed_route_backend_properties_preserve_tls_and_reject_unsupported_readback() -> None:
    transport_adapter = httpx.MockTransport(lambda request: httpx.Response(500))
    with httpx.Client(transport=transport_adapter) as transport:
        tokens = Mock()
        client = AzureApimPublisherClient(
            publisher_settings(), token_provider=tokens, client=transport,
        )
        backend = BackendResource("unit-backend", "unit", "https://unit.example.test", (), None)
        properties = client.route_backend_properties(backend)
        assert properties["tls"] == {
            "validateCertificateChain": True, "validateCertificateName": True,
        }
        assert client._pool_resource_values(properties, False) == properties
        assert client._normalized_duration("P1DT1H1M1S") == 90061
        assert client._normalized_duration("PT60S") == 60
        assert client._normalized_duration("PT") == "PT"
        with pytest.raises(ValueError, match="unsupported properties"):
            client._pool_resource_values({**properties, "authentication": "invented"}, False)
        with pytest.raises(ValueError, match="credential header"):
            client.route_backend_properties(BackendResource("unit", "unit", backend.url,
                (("X-Key", "unit"), ("x-key", "unit")), None))
        tokens.token.assert_not_called()


def test_route_backend_creation_unknown_is_recoveronly_and_keeps_exact_tls_contract() -> None:
    settings = publisher_settings().model_copy(update={"apim_gateway_profile_id": APIM_ID})
    scope = RouteScope(target_id=str(settings.apim_gateway_profile_id), api_id=settings.apim_api_id,
                        gateway_id="managed")
    properties: dict[str, Any] = {"type": "Single", "protocol": "http", "url": "https://unit.example.test",
                  "tls": {"validateCertificateChain": True, "validateCertificateName": True},
                  "credentials": {}, "circuitBreaker": None}
    connection = ConnectionRevision(
        scope=scope, revision_id=str(UUID(int=31)), backend_id="unit-backend",
        origin=properties["url"], path="/chat/completions",
        dependency_sha256=document_digest(properties), credential=RouteCredential(mode="none"),
    )
    requests: list[str] = []
    saved: list[dict[str, Any]] = []
    observed = False

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.method == "PUT":
            assert len(saved) == 1 and request.headers["If-None-Match"] == "*"
            assert json.loads(request.content) == {"properties": properties}
            raise httpx.ReadTimeout("unit-only unknown result")
        return (httpx.Response(200, json={"properties": properties}) if observed
            else httpx.Response(404))

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        tokens = Mock()
        tokens.token.return_value = "unit-only-token"
        client = AzureApimPublisherClient(settings, token_provider=tokens, client=transport)
        with pytest.raises(RuntimeError, match="read-only recovery"):
            client.prepare_route_connection(connection, lambda intent: saved.append(intent))
        assert requests == ["GET", "PUT", "GET"]
        observed = True
        assert client.prepare_route_connection(connection, lambda *_: None,
                                                recovery_only=True) == connection.dependency_sha256
        assert requests.count("PUT") == 1
        properties["tls"]["validateCertificateName"] = False
        with pytest.raises(ValueError, match="approved dependency"):
            client.verify_route_connection(connection)


@pytest.mark.parametrize("maximum_pool", [False, True])
def test_route_pool_preparation_caps_four_creates_and_resumes_from_exact_readback(
    maximum_pool: bool,
) -> None:
    settings = publisher_settings().model_copy(update={"apim_gateway_profile_id": APIM_ID})
    scope = RouteScope(target_id=str(APIM_ID), api_id=settings.apim_api_id, gateway_id="managed")
    rows: dict[str, dict[str, Any]] = {}
    writes: list[str] = []
    reads: list[str] = []
    intents: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            assert len(intents) == len(writes) + 1 and request.headers["If-None-Match"] == "*"
            writes.append(request.url.path)
            rows[request.url.path] = json.loads(request.content)["properties"]
            return httpx.Response(201, json={"properties": rows[request.url.path]})
        reads.append(request.url.path)
        return httpx.Response(200, json={"properties": rows[request.url.path]}) \
            if request.url.path in rows else httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        tokens = Mock()
        tokens.token.return_value = "unit-only-token"
        client = AzureApimPublisherClient(settings, token_provider=tokens, client=transport)
        backends = tuple(BackendResource(
            f"unit-member-{index}", "unit", "https://unit.example.test",
            (("api-key", "{{unit-key-" + str(index) + "}}"),) if maximum_pool else (), None,
        ) for index in range(30 if maximum_pool else 5))
        pool = RouteBackendPool(members=tuple(RoutePoolMember(
            runtime_id=str(UUID(int=index + 1)), backend_id=backend.id,
            dependency_sha256=document_digest(client.route_backend_properties(backend)),
            priority=0, weight=1,
        ) for index, backend in enumerate(backends)), rate_limit=GatewayRateLimitResilience(
            max_attempts_per_request=2, retry_interval_seconds=1, first_fast_retry=True,
            circuit_breaker=GatewayRateLimitCircuitBreaker(failure_count=1, interval_seconds=60,
                trip_duration_seconds=60, accept_retry_after=True),
        ), credential_versions={
            "unit-key-" + str(index): "https://unitvault.vault.azure.net/secrets/key/"
            + format(index + 1, "032x") for index in range(30 if maximum_pool else 0)
        }, header_values={
            "unit-data-" + str(index): "deployment-" + str(index)
            for index in range(90 if maximum_pool else 0)
        })
        native_pool = BackendPoolResource(id="unit-pool", title="unit", url="", members=tuple(
            BackendPoolMemberResource(member.backend_id, member.priority, member.weight)
            for member in pool.members
        ))
        connection = ConnectionRevision(
            scope=scope, revision_id=str(UUID(int=32)), backend_id="unit-pool",
            origin="https://unit.example.test", path="/chat/completions", pool=pool,
            credential=RouteCredential(mode="none"),
            dependency_sha256=document_digest(client.route_backend_properties(native_pool)))
        resource_count = (
            1 + len(pool.members) + len(pool.credential_versions) + len(pool.header_values)
        )
        while len(writes) < resource_count:
            expected = min(len(writes) + 4, resource_count)
            if expected < resource_count:
                with pytest.raises(RuntimeError, match="slice completed"):
                    client.prepare_route_pool(connection, backends, intents.append)
            else:
                client.prepare_route_pool(connection, backends, intents.append)
            assert len(writes) == expected
        client.prepare_route_pool(connection, backends, lambda intent: intents.append(intent))
        assert len(writes) == resource_count
        reads.clear()
        assert client.verify_route_pool(connection) == connection.dependency_sha256
        assert len(reads) == resource_count
        assert set(reads) == set(rows)
        if maximum_pool:
            assert resource_count == 151
            assert client.verify_route_pool_slice(connection, 0, maximum=151) == 151
            with pytest.raises(ValueError, match="cursor is invalid"):
                client.verify_route_pool_slice(connection, 0, maximum=152)


def test_route_pool_waits_for_apim_to_mark_new_key_vault_references_secret() -> None:
    settings = publisher_settings().model_copy(update={"apim_gateway_profile_id": APIM_ID})
    scope = RouteScope(target_id=str(APIM_ID), api_id=settings.apim_api_id, gateway_id="managed")
    rows: dict[str, dict[str, Any]] = {}
    provisioning: dict[str, int] = {}
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PUT":
            writes.append(path)
            rows[path] = json.loads(request.content)["properties"]
            if "keyVault" in rows[path]:
                provisioning[path] = 2
            return httpx.Response(202)
        if path not in rows:
            return httpx.Response(404)
        if provisioning.get(path):
            provisioning[path] -= 1
            return httpx.Response(200, json={"properties": {**rows[path], "secret": False}})
        return httpx.Response(200, json={"properties": rows[path]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        tokens = Mock()
        tokens.token.return_value = "unit-only-token"
        client = AzureApimPublisherClient(settings, token_provider=tokens, client=transport)
        backends = tuple(BackendResource(
            f"unit-member-{index}", "unit", "https://unit.example.test",
            (("api-key", "{{unit-key-" + str(index) + "}}"),), None,
        ) for index in range(2))
        pool = RouteBackendPool(members=tuple(RoutePoolMember(
            runtime_id=str(UUID(int=index + 1)), backend_id=backend.id,
            dependency_sha256=document_digest(client.route_backend_properties(backend)),
            priority=index, weight=1,
        ) for index, backend in enumerate(backends)), rate_limit=GatewayRateLimitResilience(
            max_attempts_per_request=2, retry_interval_seconds=1, first_fast_retry=True,
            circuit_breaker=GatewayRateLimitCircuitBreaker(failure_count=1, interval_seconds=60,
                trip_duration_seconds=60, accept_retry_after=True),
        ), credential_versions={
            "unit-key-" + str(index): "https://unitvault.vault.azure.net/secrets/key/"
            + format(index + 1, "032x") for index in range(2)
        })
        native_pool = BackendPoolResource(id="unit-pool", title="unit", url="", members=tuple(
            BackendPoolMemberResource(member.backend_id, member.priority, member.weight)
            for member in pool.members
        ))
        connection = ConnectionRevision(
            scope=scope, revision_id=str(UUID(int=33)), backend_id="unit-pool",
            origin="https://unit.example.test", path="/chat/completions", pool=pool,
            credential=RouteCredential(mode="none"),
            dependency_sha256=document_digest(client.route_backend_properties(native_pool)))
        not_ready = 0
        while True:
            try:
                client.prepare_route_pool(connection, backends, lambda intent: None)
                break
            except RetryablePublicationError as error:
                assert str(error) == "APIM credential resource is not yet ready"
                not_ready += 1
                assert not_ready <= 2 * len(pool.credential_versions)
        assert not_ready == 2 * len(pool.credential_versions)
        assert len(writes) == len(set(writes)) == 1 + len(pool.members) + len(
            pool.credential_versions
        )
        assert client.verify_route_pool(connection) == connection.dependency_sha256
        key = rows[next(path for path in rows if path.endswith("/namedValues/unit-key-0"))]
        key["secret"] = False
        with pytest.raises(RetryablePublicationError, match="not yet ready"):
            client.prepare_route_pool(connection, backends, lambda intent: None)
        del key["keyVault"]
        with pytest.raises(ValueError, match="not marked secret"):
            client.prepare_route_pool(connection, backends, lambda intent: None)
        assert len(writes) == 1 + len(pool.members) + len(pool.credential_versions)


@pytest.mark.parametrize("state", ["Creating", "Updating", "InProgress", "UnknownState"])
def test_route_pool_public_metadata_waits_for_apim_provisioning(state: str) -> None:
    properties = {
        "displayName": "unit-route-data",
        "secret": False,
        "value": "unit-deployment",
        "provisioningState": state,
    }
    with pytest.raises(
        RetryablePublicationError, match="public metadata.*not yet ready",
    ) as pending:
        AzureApimPublisherClient._verify_route_header_value(
            "unit-route-data", "unit-deployment", properties,
        )
    assert "provisioningState" in str(pending.value)
    assert "unit-deployment" not in str(pending.value)

    properties["provisioningState"] = "Succeeded"
    AzureApimPublisherClient._verify_route_header_value(
        "unit-route-data", "unit-deployment", properties,
    )
    properties["value"] = "unit-unapproved-deployment"
    with pytest.raises(PolicyCompilationError, match="public metadata readback changed"):
        AzureApimPublisherClient._verify_route_header_value(
            "unit-route-data", "unit-deployment", properties,
        )


def test_route_pool_public_metadata_failed_is_terminal() -> None:
    value = "unit-private-metadata-value"
    properties = {
        "displayName": "unit-route-data",
        "secret": False,
        "value": value,
        "provisioningState": "Failed",
    }
    with pytest.raises(
        PolicyCompilationError, match="public metadata readback changed",
    ) as failed:
        AzureApimPublisherClient._verify_route_header_value(
            "unit-route-data", value, properties,
        )
    assert "provisioningState" in str(failed.value)
    assert value not in str(failed.value)


@pytest.mark.parametrize("state", ["Succeeded", "InProgress"])
@pytest.mark.parametrize(("changes", "field"), [
    ({"displayName": "unit-unapproved-name"}, "displayName"),
    ({"secret": True}, "secret"),
    ({"secret": 0}, "secret"),
    ({"value": "unit-unapproved-value"}, "value"),
    ({"keyVault": {"secretIdentifier": "unit-private-reference"}}, "keyVault"),
    ({"unexpected": "unit-private-extra-value"}, "unexpected_fields"),
])
def test_route_pool_public_metadata_mismatch_diagnostics_are_safe(
    state: str, changes: dict[str, Any], field: str,
) -> None:
    properties = {
        "displayName": "unit-route-data",
        "secret": False,
        "value": "unit-private-metadata-value",
        "provisioningState": state,
        **changes,
    }
    with pytest.raises(
        PolicyCompilationError, match="public metadata readback changed",
    ) as failed:
        AzureApimPublisherClient._verify_route_header_value(
            "unit-route-data", "unit-private-metadata-value", properties,
        )
    assert field in str(failed.value)
    assert "unit-" not in str(failed.value)


@pytest.mark.parametrize("include_state", [False, True])
def test_route_pool_public_metadata_succeeded_readback_is_unchanged(
    include_state: bool,
) -> None:
    properties: dict[str, Any] = {
        "displayName": "unit-route-data", "secret": False, "value": "unit-deployment",
    }
    if include_state:
        properties["provisioningState"] = "Succeeded"
    AzureApimPublisherClient._verify_route_header_value(
        "unit-route-data", "unit-deployment", properties,
    )


def test_route_pool_public_metadata_invalid_properties_are_safe() -> None:
    with pytest.raises(
        PolicyCompilationError, match="public metadata readback changed",
    ) as failed:
        AzureApimPublisherClient._verify_route_header_value(
            "unit-route-data", "unit-private-metadata-value", None,
        )
    assert "properties" in str(failed.value)
    assert "unit-" not in str(failed.value)


def test_route_pool_public_metadata_recovers_without_repeating_creation() -> None:
    settings = publisher_settings().model_copy(update={"apim_gateway_profile_id": APIM_ID})
    scope = RouteScope(target_id=str(APIM_ID), api_id=settings.apim_api_id, gateway_id="managed")
    rows: dict[str, dict[str, Any]] = {}
    writes: list[str] = []
    public_ready = False

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PUT":
            writes.append(path)
            rows[path] = json.loads(request.content)["properties"]
            return httpx.Response(202)
        if path not in rows:
            return httpx.Response(404)
        properties = rows[path]
        if path.endswith("/namedValues/unit-route-data"):
            properties = {**properties, "provisioningState":
                          "Succeeded" if public_ready else "Creating"}
        return httpx.Response(200, json={"properties": properties})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        tokens = Mock()
        tokens.token.return_value = "unit-only-token"
        client = AzureApimPublisherClient(settings, token_provider=tokens, client=transport)
        backends = tuple(BackendResource(
            f"unit-member-{index}", "unit", "https://unit.example.test", (), None,
        ) for index in range(2))
        members = tuple(RoutePoolMember(
            runtime_id=str(UUID(int=index + 1)), backend_id=backend.id,
            dependency_sha256=document_digest(client.route_backend_properties(backend)),
            priority=index, weight=1,
        ) for index, backend in enumerate(backends))
        pool = RouteBackendPool(members=members, rate_limit=GatewayRateLimitResilience(
            max_attempts_per_request=2, retry_interval_seconds=1, first_fast_retry=True,
            circuit_breaker=GatewayRateLimitCircuitBreaker(failure_count=1, interval_seconds=60,
                trip_duration_seconds=60, accept_retry_after=True),
        ), header_values={"unit-route-data": "unit-deployment"})
        native_pool = BackendPoolResource(id="unit-pool", title="unit", url="", members=tuple(
            BackendPoolMemberResource(member.backend_id, member.priority, member.weight)
            for member in members
        ))
        connection = ConnectionRevision(
            scope=scope, revision_id=str(UUID(int=34)), backend_id="unit-pool",
            origin="https://unit.example.test", path="/chat/completions", pool=pool,
            credential=RouteCredential(mode="none"),
            dependency_sha256=document_digest(client.route_backend_properties(native_pool)),
        )
        intents: list[dict[str, Any]] = []
        with pytest.raises(RetryablePublicationError, match="public metadata.*not yet ready"):
            client.prepare_route_pool(connection, backends, intents.append)
        assert len(writes) == len(intents) == 1
        public_ready = True
        client.prepare_route_pool(connection, backends, intents.append)
        assert len(writes) == len(set(writes)) == len(intents) == 4
        assert client.verify_route_pool(connection) == connection.dependency_sha256


@pytest.mark.parametrize("payload", [
    {}, {"value": [], "nextLink": "https://example.test/next"},
    {"value": [{"actions": ["*"], "condition": "unverified"}]},
    {"value": [{"actions": [None]}]},
])
def test_permission_inventory_rejects_incomplete_evidence(payload: object) -> None:
    with pytest.raises(ValueError):
        missing_management_actions(payload, PUBLICATION_REQUIRED_ACTIONS)


@pytest.mark.parametrize("granted", [False, True])
def test_observer_permission_preflight_is_named_value_scoped(granted: bool) -> None:
    action = "Microsoft.ApiManagement/service/namedValues/listValue/action"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path.endswith(
            "/namedValues/unit-observer-key/providers/Microsoft.Authorization/permissions"
        )
        return httpx.Response(200, json={"value": [{"actions": [action] if granted else []}]})

    client = AzureApimPublisherClient(
        publisher_settings(), cast(Any, StubTokenProvider()),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    if granted:
        client.verify_observer_permissions("unit-observer-key")
    else:
        with pytest.raises(PublisherPermissionError) as failed:
            client.verify_observer_permissions("unit-observer-key")
        assert failed.value.missing_actions == (action,)
    assert len(calls) == 1


@pytest.mark.parametrize("materialized", [False, True])
def test_pool_catalog_key_auth_never_reads_business_named_values(materialized: bool) -> None:
    from cryptography.fernet import Fernet

    from tests.backend.model_platform.control_plane_support import (
        external_tenant_foundry_publication,
    )

    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "unit-owner",
    )
    binding = publication.desired_spec.bindings[-1]
    secret_id = "https://unit-vault.vault.azure.net/secrets/unit-key/" + "a" * 32
    if materialized:
        binding = binding.model_copy(update={"key_vault_secret_id": secret_id})
    settings = publisher_settings().model_copy(update={
        "credential_encryption_key": SecretStr(Fernet.generate_key().decode()),
    })
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert "/namedValues/" not in request.url.path
        if request.url.host == "unit-vault.vault.azure.net":
            assert request.url.path == "/secrets/unit-key/" + "a" * 32
            return httpx.Response(200, json={
                "id": secret_id, "value": "unit-only-key", "attributes": {"enabled": True},
            })
        assert request.url.path == "/api/projects/unit-foundry-project/deployments"
        assert request.headers["api-key"] == "unit-only-key"
        return httpx.Response(200, json={"value": [{"name": binding.model.upstream_model_id}]})

    token = Mock()
    token.token.return_value = "unit-token"
    client = AzureApimPublisherClient(
        settings, token, httpx.Client(transport=httpx.MockTransport(handler)),
    )
    if materialized:
        assert binding.model.upstream_model_id in client.pool_deployment_catalog(binding)
        token.token.assert_called_once_with("https://vault.azure.net")
        assert len(requests) == 2
    else:
        with pytest.raises(ValueError, match="credential is unavailable"):
            client.pool_deployment_catalog(binding)
        assert not requests


def test_publication_permission_preflight_uses_current_source_and_readonly_recovery() -> None:
    publication = GatewayControlPlaneService(InMemoryRepository()).publish(
        bedrock_publication(), "unit-owner",
    )
    compiled = ApimPolicyCompiler().compile(publication)
    required = list(PUBLICATION_REQUIRED_ACTIONS)
    assert "Microsoft.ApiManagement/service/apis/operations/write" in required
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path.endswith("/providers/Microsoft.Authorization/permissions")
        return httpx.Response(200, json={"value": [{"actions": required}]})

    client = AzureApimPublisherClient(
        publisher_settings(), cast(Any, StubTokenProvider()),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.verify_publication_permissions(compiled)
    required.remove("Microsoft.ApiManagement/service/backends/write")
    with pytest.raises(PublisherPermissionError) as error:
        client.verify_publication_permissions(compiled)
    assert error.value.missing_actions == ("Microsoft.ApiManagement/service/backends/write",)
    client.verify_publication_permissions(compiled, recovery_only=True)
    assert len(requests) == 3
    assert missing_management_actions({"value": [
        {"actions": ["MICROSOFT.APIMANAGEMENT/*"], "notActions": ["*/write"]},
        {"actions": ["Microsoft.ApiManagement/service/backends/write"]},
    ]}, ["Microsoft.ApiManagement/service/apis/read",
         "Microsoft.ApiManagement/service/backends/write"]) == ()


@pytest.mark.parametrize("interrupted", [None, "gateway", "association"])
@pytest.mark.parametrize("association_status", [200, 204])
def test_new_self_hosted_gateway_creates_and_recovers_without_duplicate_resources(
    interrupted: str | None, association_status: int,
) -> None:
    settings = publisher_settings().model_copy(update={
        "azure_subscription_id": str(UUID(int=2)), "apim_resource_group": "unit-group",
        "apim_service_name": "unit-service", "apim_api_id": "unit-api",
    })
    native_id = self_hosted_gateway_native_id_for(TARGET, UUID(int=91))
    spec = SelfHostedGatewayProvisionSpec(
        request_id=UUID(int=91), target=target(), native_gateway_id=native_id,
        deployment_id=gateway_deployment_id_for(TARGET, native_id),
        configuration=SelfHostedGatewayCreate(
            display_name="New edge", endpoint_url=HttpUrl("https://edge.example.test/unit/llm"),
            endpoint_control_confirmed=True,
        ),
    )
    resources: dict[str, Any] = {}
    writes: list[str] = []
    token_requests: list[dict[str, Any]] = []
    interrupted_once = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal interrupted_once
        assert request.headers["Authorization"]
        assert request.url.host == "management.azure.com"
        path = request.url.path.removeprefix(spec.target.arm_resource_id)
        if path in {
            "/providers/Microsoft.Authorization/permissions",
            f"/gateways/{native_id}/providers/Microsoft.Authorization/permissions",
        }:
            return httpx.Response(200, json={"value": [{"actions": ["*"], "notActions": []}]})
        if path == "":
            return httpx.Response(200, json={"id": spec.target.arm_resource_id,
                                           "sku": {"name": "Developer"}})
        if path == "/apis/unit-api":
            return httpx.Response(200, json={
                "id": spec.target.arm_resource_id + path,
                "properties": {"path": spec.target.api_path},
            })
        if path.endswith("/generateToken"):
            token_requests.append(json.loads(request.content))
            return httpx.Response(200, json={"value": native_id + "&202601010000&unit-secret"})
        if request.method == "HEAD":
            assert path == f"/gateways/{native_id}/apis/unit-api"
            return httpx.Response(association_status if path in resources else 404)
        if request.method == "GET":
            assert "/apis/" not in path
            if path in resources:
                return httpx.Response(200, json=resources[path])
            return httpx.Response(404)
        assert request.method == "PUT"
        if path == "/gateways/" + native_id:
            assert request.headers["if-none-match"] == "*"
        writes.append(path)
        resources[path] = {"id": spec.target.arm_resource_id + path, **json.loads(request.content)}
        step = "association" if "/apis/" in path else "gateway"
        if interrupted == step and not interrupted_once:
            interrupted_once = True
            raise httpx.ReadTimeout("Response lost after server commit", request=request)
        return httpx.Response(201, json=resources[path])

    client = AzureApimPublisherClient(
        settings, cast(Any, StubTokenProvider()),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    lease = Mock()
    if interrupted:
        with pytest.raises(httpx.ReadTimeout):
            client.ensure_self_hosted_gateway(spec, lease)
    client.ensure_self_hosted_gateway(spec, lease)
    client.ensure_self_hosted_gateway(spec, lease)
    assert writes == [f"/gateways/{native_id}", f"/gateways/{native_id}/apis/unit-api"]
    assert lease.call_count == 2
    bootstrap = client.self_hosted_gateway_bootstrap(
        spec, SelfHostedGatewayBootstrapRequest(
            expires_in_hours=24, endpoint_control_confirmed=True,
        ), "mcr.microsoft.com/azure-api-management/gateway:2.12.1",
    )
    assert bootstrap.native_gateway_id == native_id
    environment_file = bootstrap.environment_file.get_secret_value()
    assert "GatewayKey " + native_id in environment_file
    assert "unit-secret" not in repr(bootstrap)
    assert "unit-secret" not in bootstrap.docker_command
    assert "chmod 600" in bootstrap.docker_command
    assert "--env-file" in bootstrap.docker_command
    assert "config.service.endpoint=https://unit-service.configuration.azure-api.net" in (
        environment_file
    )
    assert token_requests[0]["keyType"] == "primary"
    assert 23 * 3600 < (bootstrap.expires_at - datetime.now(UTC)).total_seconds() < 24 * 3600
    assert len(writes) == 2


@pytest.mark.parametrize("blocked", [
    "permission", "foreign", "lease", "permission_shape", "permission_page", "sku", "api",
])
def test_gateway_creation_never_overwrites_foreign_resources_or_bypasses_permissions(
    blocked: str,
) -> None:
    settings = publisher_settings().model_copy(update={
        "azure_subscription_id": str(UUID(int=2)), "apim_resource_group": "unit-group",
        "apim_service_name": "unit-service", "apim_api_id": "unit-api",
    })
    native_id = self_hosted_gateway_native_id_for(TARGET, UUID(int=92))
    spec = SelfHostedGatewayProvisionSpec(
        request_id=UUID(int=92), target=target(), native_gateway_id=native_id,
        deployment_id=gateway_deployment_id_for(TARGET, native_id),
        configuration=SelfHostedGatewayCreate(
            display_name="New edge", endpoint_url=HttpUrl("https://edge.example.test/unit/llm"),
            endpoint_control_confirmed=True,
        ),
    )
    writes = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(spec.target.arm_resource_id)
        if request.method != "GET":
            writes.append(path)
            raise AssertionError("No ARM write may be issued")
        if path.endswith("/permissions"):
            if blocked == "permission_shape":
                return httpx.Response(200, json={"value": [{"actions": "*"}]})
            if blocked == "permission_page":
                return httpx.Response(200, json={"value": [{"actions": ["*"]}],
                                                "nextLink": "https://foreign.example.test"})
            return httpx.Response(200, json={"value": [{
                "actions": ["*"],
                "notActions": ["*/gateways/write"] if blocked == "permission" else [],
            }]})
        if path == "":
            return httpx.Response(200, json={
                "id": spec.target.arm_resource_id,
                "sku": {"name": "StandardV2" if blocked == "sku" else "Developer"},
            })
        if path == "/apis/unit-api":
            resource_id = "wrong" if blocked == "api" else spec.target.arm_resource_id + path
            return httpx.Response(200, json={
                "id": resource_id, "properties": {"path": spec.target.api_path},
            })
        return httpx.Response(200, json={"id": spec.target.arm_resource_id + path,
                                        "properties": {"description": "customer-owned"}}) \
            if blocked == "foreign" else httpx.Response(404)

    client = AzureApimPublisherClient(
        settings, cast(Any, StubTokenProvider()),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    def lease() -> None:
        if blocked == "lease":
            raise RetryablePublicationError("Lease lost")

    with pytest.raises((PolicyCompilationError, RetryablePublicationError)):
        client.ensure_self_hosted_gateway(spec, lease)
    assert writes == []


@pytest.mark.parametrize("defect", [
    "foreign", "association", "association_pending", "token_target", "token_newline",
])
def test_gateway_bootstrap_rejects_changed_ownership_or_invalid_token(defect: str) -> None:
    settings = publisher_settings().model_copy(update={
        "azure_subscription_id": str(UUID(int=2)), "apim_resource_group": "unit-group",
        "apim_service_name": "unit-service", "apim_api_id": "unit-api",
    })
    native_id = self_hosted_gateway_native_id_for(TARGET, UUID(int=93))
    spec = SelfHostedGatewayProvisionSpec(
        request_id=UUID(int=93), target=target(), native_gateway_id=native_id,
        deployment_id=gateway_deployment_id_for(TARGET, native_id),
        configuration=SelfHostedGatewayCreate(
            display_name="New edge", endpoint_url=HttpUrl("https://edge.example.test/unit/llm"),
            endpoint_control_confirmed=True,
        ),
    )
    issued = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(spec.target.arm_resource_id)
        if path == f"/gateways/{native_id}/providers/Microsoft.Authorization/permissions":
            assert request.method == "GET"
            return httpx.Response(200, json={"value": [{"actions": ["*"]}]})
        if request.method == "GET":
            assert path == f"/gateways/{native_id}"
            marker = f"Turnstile gateway {TARGET}/{spec.request_id}"
            return httpx.Response(200, json={
                "id": spec.target.arm_resource_id + path,
                "properties": {"description": "foreign" if defect == "foreign" else marker},
            })
        if request.method == "HEAD":
            assert path == f"/gateways/{native_id}/apis/unit-api"
            return httpx.Response({"association": 404, "association_pending": 202}.get(defect, 204))
        assert request.method == "POST" and path == f"/gateways/{native_id}/generateToken"
        issued.append(path)
        value = "another&unit-secret" if defect == "token_target" else native_id + "&unit-secret\n"
        return httpx.Response(200, json={"value": value})

    client = AzureApimPublisherClient(
        settings, cast(Any, StubTokenProvider()),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    error_type = (
        RetryablePublicationError
        if defect in {"association", "association_pending"}
        else PolicyCompilationError
    )
    with pytest.raises(error_type) as error:
        client.self_hosted_gateway_bootstrap(
            spec, SelfHostedGatewayBootstrapRequest(
                expires_in_hours=24, endpoint_control_confirmed=True,
            ), "mcr.microsoft.com/azure-api-management/gateway:2.12.1",
        )
    assert "unit-secret" not in str(error.value)
    assert len(issued) == (1 if defect.startswith("token_") else 0)


@pytest.mark.parametrize("out_of_scope", [False, True])
def test_candidate_probe_uses_each_gateway_scope_and_records_optional_failure(
    out_of_scope: bool,
) -> None:
    repository = InMemoryRepository()
    repository.models = []
    publication = GatewayControlPlaneService(repository).publish(
        bedrock_publication(), "unit-owner",
    )
    endpoints = tuple(GatewayServingDeployment.model_validate({
        "deployment_id": str(UUID(int=index + 1)), "native_gateway_id": name,
        "kind": "managed" if name == "managed" else "self_hosted",
        "endpoint_url": f"https://{name}.example.test/unit/llm", "release_gate": gate,
    }) for index, (name, gate) in enumerate((("managed", "required"), ("edge", "required"),
                                           ("optional", "optional"))))
    binding = publication.desired_spec.bindings[-1].model_copy(update={
        "logical_model_id": UUID(int=80),
        "deployment_scope": GatewayDeploymentScope.model_validate({
            "mode": "selected", "deployment_ids": [endpoints[1].deployment_id],
        }), "serving_deployments": (endpoints[1],),
    })
    spec = publication.desired_spec.model_copy(update={
        "bindings": [binding], "gateway_deployments": endpoints,
    })
    publication = publication.model_copy(update={"desired_spec": spec})
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "optional.example.test":
            return httpx.Response(503)
        if request.method == "GET":
            serves_model = request.url.host == "edge.example.test" or out_of_scope
            ids = [binding.model.model_key] if serves_model else []
            return httpx.Response(200, json={"data": [{"id": identity} for identity in ids]})
        if request.url.host == "managed.example.test":
            body = json.loads(request.content)
            assert body["model"] == binding.model.model_key
            if request.url.path.endswith("/count_tokens"):
                from turnstile_core.domain.control_plane import COUNT_TOKENS_REJECTIONS

                denial = COUNT_TOKENS_REJECTIONS["unpublished"]
                return httpx.Response(denial.status_code, json=denial.payload())
            assert body["stream"] is False and body["max_tokens"] == 8
            return httpx.Response(400, json={"error": "The model is not published"})
        assert request.url.host == "edge.example.test"
        assert request.headers["x-model-id"] == str(binding.logical_model_id)
        if request.url.path.endswith("/count_tokens"):
            from turnstile_core.domain.control_plane import COUNT_TOKENS_REJECTIONS

            denial = COUNT_TOKENS_REJECTIONS["unavailable"]
            return httpx.Response(denial.status_code, json=denial.payload())
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}],
                                        "usage": {"input_tokens": 1, "output_tokens": 1}})

    settings = publisher_settings().model_copy(update={
        "apim_probe_subscription_key": SecretStr("unit-probe-key"),
    })
    client = AzureApimPublisherClient(settings, cast(Any, StubTokenProvider()),
                                     httpx.Client(transport=httpx.MockTransport(handler)))
    if out_of_scope:
        with pytest.raises(RetryablePublicationError, match="outside this Gateway scope"):
            client.probe_revision("unit-revision", publication)
        assert len(requests) == 1
    else:
        results = client.probe_revision("unit-revision", publication)
        assert [item.status for item in results] == ["succeeded", "succeeded", "optional_pending"]
        assert results[-1].error_code == "RetryablePublicationError"
        assert {request.url.host for request in requests} == {
            "managed.example.test", "edge.example.test", "optional.example.test",
        }


@pytest.mark.parametrize("tier", ["Developer", "StandardV2"])
def test_target_inventory_is_read_only_and_whitelists_gateway_fields(tier: str) -> None:
    settings = publisher_settings().model_copy(update={"azure_subscription_id": str(UUID(int=2))})
    base = (
        f"/subscriptions/{settings.azure_subscription_id}"
        f"/resourceGroups/{settings.apim_resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{settings.apim_service_name}"
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        path = request.url.path.removeprefix(base)
        requests.append(path)
        if not path:
            return httpx.Response(200, json={
                "id": base, "sku": {"name": tier}, "location": "eastus",
            })
        if path == f"/apis/{settings.apim_api_id}":
            return httpx.Response(200, json={"id": base + path,
                                           "properties": {"path": "unit/llm", "apiRevision": "1"}})
        if path == "/gateways":
            return httpx.Response(200, json={"value": [{"id": base + "/gateways/edge",
                "properties": {"locationData": {"name": "Unit edge"}, "key": "not-in-inventory"}}]})
        return httpx.Response(200, json={"value": []})

    client = AzureApimPublisherClient(settings, cast(Any, StubTokenProvider()),
                                     httpx.Client(transport=httpx.MockTransport(handler)))
    inventory = client.discover_apim_target_inventory(APIM_ID)
    assert inventory.api_path == "unit/llm"
    assert "not-in-inventory" not in inventory.model_dump_json()
    assert inventory.capabilities.table_ledger == "unknown"
    assert ("/gateways" in requests) == (tier == "Developer")
    assert len(inventory.gateways) == int(tier == "Developer")


def test_target_inventory_rejects_foreign_service_before_any_child_requests() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "/subscriptions/foreign/service/other"})

    client = AzureApimPublisherClient(publisher_settings(), cast(Any, StubTokenProvider()),
                                     httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(PolicyCompilationError, match="another resource identity"):
        client.discover_apim_target_inventory(APIM_ID)
    assert len(requests) == 1


def test_databricks_oauth_provisions_owned_resources_without_secret_readback() -> None:
    resources: dict[str, dict[str, Any]] = {}
    writes: list[str] = []
    settings = publisher_settings().model_copy(update={"databricks_oauth_enabled": True})
    identity = {"principalId": str(UUID(int=99)), "tenantId": str(UUID(int=97))}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/service/{settings.apim_service_name}"):
            return httpx.Response(200, json={"identity": identity})
        if path.endswith("/accessPolicies") and request.method == "GET":
            return httpx.Response(200, json={"value": [
                {"id": resource_path, **resource}
                for resource_path, resource in resources.items()
                if resource_path.startswith(path + "/")
            ]})
        if request.method == "PUT":
            body = json.loads(request.content)
            parameters = body["properties"].get("parameters")
            if parameters:
                assert parameters.pop("clientSecret") == "unit-oauth-secret"
                body["properties"]["status"] = "Connected"
            resources[path] = body
            writes.append(path)
            return httpx.Response(201, json=body)
        assert request.method == "GET"
        return httpx.Response(200, json=resources[path]) if path in resources else httpx.Response(
            404, json={"error": {"code": "NotFound"}},
        )

    config = OAuthClientCredentialsConfig(
        provider_id=f"turnstile-oauth-{UUID(int=8).hex}", client_id=UUID(int=98),
        token_url=HttpUrl("https://adb-unit.1.azuredatabricks.net/oidc/v1/token"),
    )
    client = AzureApimPublisherClient(
        settings, cast(Any, StubTokenProvider()),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    resource = OAuthCredentialResource(config, "unit-oauth-secret")
    assert "unit-oauth-secret" not in repr(resource)
    client.ensure_oauth_credential(resource)
    assert len(writes) == 3
    client.ensure_oauth_credential(OAuthCredentialResource(config))
    assert len(writes) == 3 and client.oauth_credential_issues(resource) == []
    intents: list[dict[str, Any]] = []
    client.prepare_route_oauth(resource, lambda intent: intents.append(intent))
    assert len(writes) == 3 and intents == []
    assert all("unit-oauth-secret" not in json.dumps(value) for value in resources.values())
    extra_policy = writes[2].rsplit("/", 1)[0] + "/other-identity"
    resources[extra_policy] = {"properties": {
        "objectId": str(UUID(int=96)), "tenantId": identity["tenantId"],
    }}
    assert f"mismatched_oauth_access_policy_set:{config.provider_id}" in (
        client.oauth_credential_issues(resource)
    )
    resources.pop(extra_policy)
    provider = resources[writes[0]]["properties"]
    provider["oauth2"]["grantTypes"]["clientCredentials"]["tokenUrl"] = "https://other.example/token"
    with pytest.raises(PolicyCompilationError, match="different connection"):
        client.ensure_oauth_credential(resource)
    assert len(writes) == 3


@pytest.mark.parametrize("state", ["present", "missing", "conflicting"])
def test_fixed_image_operation_is_read_only_and_requires_infrastructure_upgrade(state: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path.endswith(";rev=candidate/operations/images-generations")
        return httpx.Response(
            404 if state == "missing" else 200,
            json={"properties": {
                "method": "GET" if state == "conflicting" else "POST",
                "urlTemplate": "/images/generations", "templateParameters": [],
            }},
        )

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    operation = OperationResource(
        "images-generations", "Image generations", "POST", "/images/generations"
    )
    if state == "present":
        client.ensure_operation("candidate", operation)
    else:
        message = (
            "infrastructure upgrade required" if state == "missing" else "route does not match"
        )
        with pytest.raises(PolicyCompilationError, match=message):
            client.ensure_operation("candidate", operation)
    assert len(requests) == 1


def test_arm_client_accepts_redacted_existing_backend_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "properties": {
                    "url": "https://observer.example.com",
                    "tls": {
                        "validateCertificateChain": True,
                        "validateCertificateName": True,
                    },
                    "credentials": {
                        "header": {
                            "x-adapter-key": None,
                            "x-turnstile-upstream-host": None,
                        }
                    },
                }
            },
        )

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.ensure_backend(
        BackendResource(
            id="turnstile-obs-existing",
            title="Existing observer",
            url="https://observer.example.com",
            headers=(
                ("x-adapter-key", "{{observer-key}}"),
                ("x-turnstile-upstream-host", "provider.example.com"),
            ),
        )
    )

@pytest.mark.parametrize(
    "method_name",
    ("_probe_model", "_probe_responses_model", "_probe_responses_compact_model"),
)
@pytest.mark.parametrize("baseline_status", (403, 200, 401, 429, 503))
def test_surviving_candidate_failure_requires_a_matching_nontransient_baseline(
    method_name: str, baseline_status: int
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        code = baseline_status if ";rev=current" in request.url.path else 403
        return httpx.Response(code, json={"error": "provider_error"})

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    probe = getattr(client, method_name)
    candidate = "https://gateway.example.com/turnstile/llm;rev=candidate"
    baseline = "https://gateway.example.com/turnstile/llm;rev=current"
    if baseline_status == 403:
        probe(candidate, {"x-test-scope": "survivor"}, "existing-model", baseline_base=baseline)
    else:
        with pytest.raises(RetryablePublicationError):
            probe(candidate, {"x-test-scope": "survivor"}, "existing-model", baseline_base=baseline)
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert requests[0].headers == requests[1].headers
    assert json.loads(requests[1].content)["model"] == "existing-model"


@pytest.mark.parametrize(
    "method_name",
    ("_probe_model", "_probe_responses_model", "_probe_responses_compact_model"),
)
def test_new_candidate_failure_has_no_baseline_exemption(method_name: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(403, json={"error": "forbidden"})

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AuthorizationRequiredError):
        getattr(client, method_name)(
            "https://gateway.example.com/turnstile/llm;rev=candidate",
            {},
            "new-model",
            authorization_required=True,
        )
    assert len(requests) == 1


@pytest.mark.parametrize(
    "method_name",
    ("_probe_model", "_probe_responses_model", "_probe_responses_compact_model"),
)
@pytest.mark.parametrize("status_code", (429, 503))
def test_transient_candidate_failure_never_uses_a_baseline_exemption(
    method_name: str, status_code: int
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code)

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RetryablePublicationError):
        getattr(client, method_name)(
            "https://gateway.example.com/turnstile/llm;rev=candidate",
            {},
            "existing-model",
            baseline_base="https://gateway.example.com/turnstile/llm;rev=current",
        )
    assert len(requests) == (3 if status_code == 503 else 1)
    assert all(";rev=candidate" in request.url.path for request in requests)
    assert all(request.content == requests[0].content for request in requests)


@pytest.mark.parametrize("field", ("max_tokens", "max_completion_tokens"))
def test_openai_candidate_probe_uses_the_connections_token_field(field: str) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client._probe_model(
        "https://gateway.example.com/turnstile/llm;rev=candidate", {}, "compatible-chat",
        api_format=ApiFormat.OPENAI_CHAT, max_tokens_field=field,
    )
    assert bodies[0][field] == 8
    assert len({"max_tokens", "max_completion_tokens"} & bodies[0].keys()) == 1
    with pytest.raises(RuntimeError, match="unsupported token field"):
        client._probe_model(
            "https://gateway.example.com/turnstile/llm;rev=candidate", {}, "compatible-chat",
            api_format=ApiFormat.OPENAI_CHAT, max_tokens_field="unknown",
        )
    assert len(bodies) == 1


def test_arm_client_creates_native_pool_and_rate_limit_breakers() -> None:
    writes: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)
        writes[request.url.path] = json.loads(request.content)
        return httpx.Response(200, json={})

    settings = publisher_settings()
    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    breaker = BackendCircuitBreakerResource(
        failure_count=1,
        interval_seconds=60,
        trip_duration_seconds=120,
        accept_retry_after=True,
    )
    members = (
        BackendResource(
            id="member-a",
            title="Member A",
            url="https://a.example.com",
            circuit_breaker=breaker,
        ),
        BackendResource(
            id="member-b",
            title="Member B",
            url="https://b.example.com",
            headers=(("api-key", "{{member-b-key}}"),),
            circuit_breaker=breaker,
        ),
    )
    for member in members:
        client.ensure_backend(member)
    client.ensure_backend(
        BackendPoolResource(
            id="pool-a",
            title="Pool A",
            url="",
            members=(
                BackendPoolMemberResource(
                    backend_id="member-a",
                    priority=0,
                    weight=3,
                ),
                BackendPoolMemberResource(
                    backend_id="member-b",
                    priority=1,
                    weight=1,
                ),
            ),
        )
    )

    member_properties = writes[
        f"/subscriptions/{settings.azure_subscription_id}/resourceGroups/"
        f"{settings.apim_resource_group}/providers/Microsoft.ApiManagement/service/"
        f"{settings.apim_service_name}/backends/member-a"
    ]["properties"]
    assert member_properties["type"] == "Single"
    assert member_properties["circuitBreaker"] == {
        "rules": [
            {
                "name": "deployment-fault-isolation",
                "failureCondition": {
                    "count": 1,
                    "interval": "PT60S",
                    "statusCodeRanges": [
                        {"min": 429, "max": 429},
                        {"min": 408, "max": 408},
                        {"min": 500, "max": 599},
                    ],
                    "errorReasons": ["BackendConnectionFailure", "Timeout"],
                },
                "tripDuration": "PT120S",
                "acceptRetryAfter": True,
            }
        ]
    }
    member_b_properties = writes[
        f"/subscriptions/{settings.azure_subscription_id}/resourceGroups/"
        f"{settings.apim_resource_group}/providers/Microsoft.ApiManagement/service/"
        f"{settings.apim_service_name}/backends/member-b"
    ]["properties"]
    assert member_b_properties["credentials"]["header"] == {
        "api-key": ["{{member-b-key}}"]
    }
    pool_properties = writes[
        f"/subscriptions/{settings.azure_subscription_id}/resourceGroups/"
        f"{settings.apim_resource_group}/providers/Microsoft.ApiManagement/service/"
        f"{settings.apim_service_name}/backends/pool-a"
    ]["properties"]
    assert pool_properties["type"] == "Pool"
    assert pool_properties["pool"]["services"] == [
        {
            "id": (
                f"/subscriptions/{settings.azure_subscription_id}/resourceGroups/"
                f"{settings.apim_resource_group}/providers/Microsoft.ApiManagement/"
                f"service/{settings.apim_service_name}/backends/member-a"
            ),
            "priority": 1,
            "weight": 3,
        },
        {
            "id": (
                f"/subscriptions/{settings.azure_subscription_id}/resourceGroups/"
                f"{settings.apim_resource_group}/providers/Microsoft.ApiManagement/"
                f"service/{settings.apim_service_name}/backends/member-b"
            ),
            "priority": 2,
            "weight": 1,
        },
    ]

def test_arm_client_pool_readback_is_immutable_and_idempotent() -> None:
    settings = publisher_settings()
    base = (
        f"/subscriptions/{settings.azure_subscription_id}/resourceGroups/"
        f"{settings.apim_resource_group}/providers/Microsoft.ApiManagement/service/"
        f"{settings.apim_service_name}"
    )
    writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        if request.method == "PUT":
            writes += 1
        return httpx.Response(
            200,
            json={
                "properties": {
                    "type": "Pool",
                    "pool": {
                        "services": [
                            {
                                "id": f"{base}/backends/member-a",
                                "priority": 1,
                                "weight": 1,
                            },
                            {
                                "id": f"{base}/backends/member-b",
                                "priority": 1,
                                "weight": 1,
                            },
                        ]
                    },
                }
            },
        )

    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    pool = BackendPoolResource(
        id="pool-a",
        title="Pool A",
        url="",
        members=(
            BackendPoolMemberResource("member-a", 0, 1),
            BackendPoolMemberResource("member-b", 0, 1),
        ),
    )

    client.ensure_backend(pool)
    assert writes == 0

    changed = pool.__class__(
        id=pool.id,
        title=pool.title,
        url="",
        members=(
            BackendPoolMemberResource("member-a", 0, 2),
            BackendPoolMemberResource("member-b", 0, 1),
        ),
    )
    with pytest.raises(PolicyCompilationError, match="different settings"):
        client.ensure_backend(changed)

def test_foundry_candidate_probes_chat_and_responses_before_promotion() -> None:
    repository = InMemoryRepository()
    foundry_provider_ids = {
        item["id"]
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    }
    repository.models = [
        item for item in repository.models if item["provider_id"] not in foundry_provider_ids
    ]
    repository.runtimes = [
        item for item in repository.runtimes if item["provider_id"] not in foundry_provider_ids
    ]
    repository.providers = [
        item for item in repository.providers if item["id"] not in foundry_provider_ids
    ]
    publication = GatewayControlPlaneService(
        repository,
        apim_principal_id="00000000-0000-0000-0000-000000000705",
    ).publish(foundry_publication("gpt-5.6-luna"), "owner@example.com")
    alias = publication.desired_spec.bindings[-1].model.model_key
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": alias}]})
        payload = json.loads(request.content)
        assert payload["model"] == alias
        if request.url.path.endswith("/responses/compact"):
            assert payload == {"model": alias, "input": "Reply only with OK."}
            return httpx.Response(
                200,
                json={
                    "object": "response.compaction",
                    "output": [{"type": "compaction", "encrypted_content": "opaque"}],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "total_tokens": 3,
                    },
                },
            )
        if request.url.path.endswith("/responses"):
            assert payload["input"] == "Reply only with OK."
            assert payload["max_output_tokens"] == 16
            assert payload["store"] is False
            assert "messages" not in payload
            return httpx.Response(
                200,
                json={"output": [{"type": "message", "content": []}]},
            )
        assert request.url.path.endswith("/chat/completions")
        assert payload["max_completion_tokens"] == 8
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    settings = publisher_settings().model_copy(
        update={
            "apim_probe_subscription_key": SecretStr("probe-key"),
            "apim_regression_model_key": alias,
        }
    )
    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.probe_revision("turnstile-test", publication)

    assert [
        request.url.path for request in requests if request.method == "POST"
    ] == [
        "/turnstile/llm;rev=turnstile-test/chat/completions",
        "/turnstile/llm;rev=turnstile-test/responses",
        "/turnstile/llm;rev=turnstile-test/responses/compact",
    ]
    for request in requests:
        assert request.headers["x-hive-run-id"] == str(publication.id)
        if request.method == "POST":
            assert request.headers["x-model-id"] == str(publication_model_id(publication.id, alias))
            assert request.headers["x-hive-model"] == alias
            assert "x-hive-runtime" not in request.headers

def test_pool_candidate_probe_covers_every_member_and_operation() -> None:
    repository = InMemoryRepository()
    provider = next(
        item
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    runtime = next(
        item for item in repository.runtimes if item["provider_id"] == provider["id"]
    )
    primary_url = "https://probe-primary.services.ai.azure.com/openai/v1"
    runtime["config"].update(
        control_plane_managed=True,
        project_endpoint="https://probe-primary.services.ai.azure.com/api/projects/main",
        backend_url=primary_url,
        backend_path="/openai/v1/chat/completions",
        auth_strategy="managed_identity",
        managed_identity_resource="https://ai.azure.com",
        streaming_mode="native",
    )
    publication = GatewayControlPlaneService(repository).publish(
        GatewayPublicationCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(existing_id=provider["id"]),
            runtime=RuntimeTarget(existing_id=runtime["id"]),
            model=ModelCreateTarget(deployment_name="gpt-5.6-probe-pool"),
        ),
        "owner@example.com",
    )
    binding = publication.desired_spec.bindings[-1]
    pool = GatewayBackendPoolConfig(
        members=[
            GatewayBackendPoolMember(
                runtime_id=runtime["id"],
                runtime_name=runtime["name"],
                backend_url=HttpUrl(primary_url),
                priority=0,
                weight=1,
            ),
            GatewayBackendPoolMember(
                runtime_id=UUID("30000000-0000-4000-8000-000000000097"),
                runtime_name="Probe secondary runtime",
                backend_url=HttpUrl(
                    "https://probe-secondary.services.ai.azure.com/openai/v1"
                ),
                priority=0,
                weight=1,
            ),
        ],
        rate_limit=GatewayRateLimitResilience(
            max_attempts_per_request=2,
            retry_interval_seconds=1,
            first_fast_retry=True,
            circuit_breaker=GatewayRateLimitCircuitBreaker(
                failure_count=1,
                interval_seconds=60,
                trip_duration_seconds=60,
                accept_retry_after=True,
            ),
        ),
    )
    pooled_binding = binding.model_copy(
        update={
            "runtime_config": {
                **binding.runtime_config,
                "apim_backend_pool": pool.model_dump(mode="json"),
            }
        }
    )
    publication = publication.model_copy(
        update={
            "desired_spec": publication.desired_spec.model_copy(
                update={"bindings": [pooled_binding]}
            )
        }
    )
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": item.id}
                        for item in publication.desired_spec.discovery_models
                    ]
                },
            )
        member_id = request.headers["x-turnstile-pool-member"]
        assert request.headers["x-hive-run-id"] == str(publication.id)
        assert request.headers["x-model-id"] == str(
            publication_model_id(publication.id, binding.model.model_key)
        )
        assert request.headers["x-hive-model"] == binding.model.model_key
        assert request.headers["x-runtime-id"] == str(binding.runtime_id)
        assert "x-hive-runtime" not in request.headers
        calls.append((member_id, request.url.path.rsplit("/", 1)[-1]))
        if request.url.path.endswith("/responses/compact"):
            return httpx.Response(
                200,
                json={
                    "object": "response.compaction",
                    "output": [{"type": "compaction", "encrypted_content": "opaque"}],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "total_tokens": 3,
                    },
                },
            )
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json={"output": []})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    client = AzureApimPublisherClient(
        publisher_settings().model_copy(
            update={
                "apim_probe_subscription_key": SecretStr("probe-key"),
                "apim_regression_model_key": binding.model.model_key,
            }
        ),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.probe_revision("turnstile-pool", publication)

    member_ids = {
        ApimPolicyCompiler._pool_member_backend_id(publication, binding, member)
        for member in pool.members
    }
    assert {member_id for member_id, _ in calls} == member_ids
    assert len(calls) == 6
    assert all(
        sum(1 for observed, _ in calls if observed == member_id) == 3
        for member_id in member_ids
    )

def test_foundry_removal_probe_requires_survivor_and_removed_alias_rejection() -> None:
    repository = InMemoryRepository()
    model = next(
        item for item in repository.models if item["model_key"] == "gpt-5.6-luna"
    )
    publication = GatewayControlPlaneService(repository).remove_model(
        model["id"], "owner@example.com"
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": item.id}
                        for item in publication.desired_spec.discovery_models
                    ]
                },
            )
        payload = json.loads(request.content)
        if request.url.path.endswith("/responses/compact"):
            assert payload == {
                "model": model["model_key"],
                "input": "Reply only with OK.",
            }
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "The requested model is not published by this gateway revision"
                    }
                },
            )
        if request.url.path.endswith("/responses"):
            assert payload["max_output_tokens"] == 16
            assert payload["store"] is False
            assert "messages" not in payload
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "The requested model is not published by this gateway revision"
                    }
                },
            )
        assert payload["max_completion_tokens"] == 8
        assert "max_tokens" not in payload
        if payload["model"] == model["model_key"]:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "The requested model is not published by this gateway revision"
                    }
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    settings = publisher_settings().model_copy(
        update={"apim_probe_subscription_key": SecretStr("probe-key")}
    )
    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.probe_revision("turnstile-test", publication)

    post_paths = [request.url.path for request in requests if request.method == "POST"]
    assert post_paths == [
        "/turnstile/llm;rev=turnstile-test/chat/completions",
        "/turnstile/llm;rev=turnstile-test/chat/completions",
        "/turnstile/llm;rev=turnstile-test/responses",
        "/turnstile/llm;rev=turnstile-test/responses/compact",
    ]
    probe_headers = requests[-1].headers
    assert probe_headers["x-user-id"] == "system-gateway-publication"
    assert probe_headers["x-hive-user"] == "Turnstile Publisher"
    assert probe_headers["x-hive-workflow"] == "gateway-publication-probe"
    for request in requests:
        assert request.headers["x-hive-run-id"] == str(publication.id)
        if request.method == "POST":
            alias = json.loads(request.content)["model"]
            assert request.headers["x-hive-model"] == alias
            assert request.headers["x-model-id"] == (
                str(model["id"]) if alias == model["model_key"] else alias
            )

@pytest.mark.parametrize("api_format", [ApiFormat.OPENAI_CHAT, ApiFormat.ANTHROPIC_MESSAGES])
@pytest.mark.parametrize("json_body", [False, True], ids=["plain-text", "json"])
@pytest.mark.parametrize("response_marker", ["NOT PUBLISHED", "DOES NOT SUPPORT"])
def test_text_removal_probe_preserves_legacy_response_contract(
    api_format: ApiFormat, json_body: bool, response_marker: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert json.loads(request.content)["model"] == "removed-text"
        message = (
            f"The gateway {response_marker} the requested Responses model"
            if "/responses" in request.url.path
            else "The requested model is NOT PUBLISHED by this gateway revision"
        )
        if json_body:
            return httpx.Response(400, json={"error": {"message": message}})
        return httpx.Response(400, text=message)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport_client:
        client = AzureApimPublisherClient(publisher_settings(), client=transport_client)
        client._probe_removed_model(
            "https://gateway.example/llm;rev=candidate", {}, "removed-text", api_format,
        )
    expected_paths = (
        ["/chat/completions", "/responses", "/responses/compact"]
        if api_format is ApiFormat.OPENAI_CHAT else ["/v1/messages"]
    )
    assert [request.url.path for request in requests] == [
        f"/llm;rev=candidate{path}" for path in expected_paths
    ]


@pytest.mark.parametrize("api_format", [ApiFormat.OPENAI_CHAT, ApiFormat.ANTHROPIC_MESSAGES])
@pytest.mark.parametrize(("status_code", "error_type"), [
    (200, RuntimeError), (400, RuntimeError), (401, RuntimeError), (403, RuntimeError),
    (429, RetryablePublicationError), (500, RetryablePublicationError),
    (503, RetryablePublicationError),
])
def test_text_removal_probe_does_not_accept_image_error_code(
    api_format: ApiFormat, status_code: int, error_type: type[Exception],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json={"error": "image_model_not_published"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport_client:
        client = AzureApimPublisherClient(publisher_settings(), client=transport_client)
        with pytest.raises(error_type, match=f"HTTP {status_code}") as error:
            client._probe_removed_model(
                "https://gateway.example/llm", {}, "removed-text", api_format,
            )
    assert type(error.value) is error_type
    assert len(requests) == 1


@pytest.mark.parametrize("failed_path", ["/responses", "/responses/compact"])
@pytest.mark.parametrize(("status_code", "error_type"), [
    (200, RuntimeError), (400, RuntimeError), (401, RuntimeError), (403, RuntimeError),
    (429, RetryablePublicationError), (500, RetryablePublicationError),
])
def test_text_removal_probe_requires_all_responses_rejections(
    failed_path: str, status_code: int, error_type: type[Exception],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == f"/llm{failed_path}":
            return httpx.Response(status_code, json={"error": "image_model_not_published"})
        return httpx.Response(400, text="The requested model is not published")

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport_client:
        client = AzureApimPublisherClient(publisher_settings(), client=transport_client)
        with pytest.raises(error_type, match=f"HTTP {status_code}") as error:
            client._probe_removed_model(
                "https://gateway.example/llm", {}, "removed-text", ApiFormat.OPENAI_CHAT,
            )
    assert type(error.value) is error_type
    assert len(requests) == (2 if failed_path == "/responses" else 3)
    assert requests[-1].url.path == f"/llm{failed_path}"


@pytest.mark.parametrize(
    "denial",
    [
        {"error": "model_not_assigned"},
        {
            "type": "error",
            "error": {
                "type": "permission_error",
                "code": "model_not_assigned",
                "param": "model",
                "message": "The requested model is not assigned to you",
            },
        },
    ],
)
def test_only_new_model_assignment_denial_is_retryable(denial: dict[str, Any]) -> None:
    responses = iter(
        [
            httpx.Response(403, json=denial),
            httpx.Response(403, json=denial),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(httpx.HTTPStatusError):
        client._probe_model("https://candidate", {}, "regression")
    with pytest.raises(RetryablePublicationError):
        client._probe_model(
            "https://candidate",
            {},
            "new-model",
            retry_assignment_denial=True,
        )

def test_live_parent_policy_drift_fails_before_candidate_policy_write() -> None:
    repository = InMemoryRepository()
    GatewayControlPlaneService(repository).publish(bedrock_publication(), "owner@example.com")
    client = FakeApimClient()
    worker = GatewayPublicationWorker(
        repository,
        client,
        (ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
    )
    for _ in range(3):
        assert worker.run_once("worker-a") is not None
    client.policy += "\n<!-- manual hotfix -->"

    failed = worker.run_once("worker-a")

    assert failed is not None and failed.status == "failed"
    assert "changed after publication was queued" in (failed.error_message or "")
    assert not any(name == "api-policy" for name, _ in client.calls)

def test_activation_failure_after_promotion_converges_without_failed_state() -> None:
    repository = InMemoryRepository()
    GatewayControlPlaneService(repository).publish(bedrock_publication(), "owner@example.com")
    client = FakeApimClient()
    worker = GatewayPublicationWorker(
        repository,
        client,
        (ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
    )
    for _ in range(5):
        assert worker.run_once("worker-a") is not None

    original_activate = repository.activate_gateway_publication
    failed_once = False

    def flaky_activate(
        publication_id: UUID, actor: str,
        *, validate_model_activation: ModelActivationValidator | None = None,
    ) -> dict[str, Any]:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("database temporarily unavailable")
        return original_activate(publication_id, actor,
                     validate_model_activation=validate_model_activation)

    repository.activate_gateway_publication = flaky_activate  # type: ignore[method-assign]
    converging = worker.run_once("worker-a")
    active = worker.run_once("worker-a")

    assert converging is not None and converging.status == "promoting"
    assert active is not None and active.status == "active"

def test_arm_client_clones_revision_from_current_api() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and ";rev=turnstile-1-test" in request.url.path:
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        if request.method == "GET" and request.url.path.endswith("/apis/turnstile-llm"):
            return httpx.Response(
                200,
                json={
                    "id": "/subscriptions/s/resourceGroups/r/providers/"
                    "Microsoft.ApiManagement/service/a/apis/turnstile-llm",
                    "properties": {
                        "path": "finops/llm",
                        "serviceUrl": "https://legacy.example",
                    },
                },
            )
        return httpx.Response(201, json={"properties": {"provisioningState": "InProgress"}})

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.ensure_revision("turnstile-1-test", "Candidate")

    create = requests[-1]
    body = json.loads(create.content)
    assert create.method == "PUT"
    assert create.url.path.endswith("/apis/turnstile-llm;rev=turnstile-1-test")
    assert body["properties"] == {
        "path": "finops/llm",
        "sourceApiId": "/subscriptions/s/resourceGroups/r/providers/"
        "Microsoft.ApiManagement/service/a/apis/turnstile-llm",
        "apiRevisionDescription": "Candidate",
        "serviceUrl": "https://legacy.example",
    }

@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        ("application/vnd.ms-azure-apim.policy+xml", "<policies />", "<policies />"),
        (
            "application/vnd.ms-azure-apim.policy+xml",
            '<policies value="&amp;quot;model&amp;quot;" />',
            '<policies value="&amp;quot;model&amp;quot;" />',
        ),
        (
            "application/json",
            json.dumps({"properties": {"value": "<policies />"}}),
            "<policies />",
        ),
    ],
)
def test_arm_client_reads_current_policy_wire_formats(
    content_type: str, body: str, expected: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/apis/turnstile-llm"):
            return httpx.Response(200, json={"properties": {"apiRevision": "1"}})
        if request.method == "HEAD":
            assert request.url.path.endswith("/apis/turnstile-llm;rev=1/policies/policy")
            return httpx.Response(200, headers={"ETag": '"original"'})
        if request.url.path.endswith("/apis/turnstile-llm;rev=1/policies"):
            assert request.method == "GET" and request.url.params["format"] == "xml"
            return httpx.Response(200, json={"value": [{
                "name": "policy", "id": request.url.path + "/policy",
                "properties": {"format": "xml", "value": expected},
            }]})
        assert request.url.path.endswith(
            "/apis/turnstile-llm;rev=1/policies/policy"
        )
        assert request.url.params["format"] == "rawxml"
        return httpx.Response(
            200,
            text=body,
            headers={"Content-Type": content_type},
        )

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client._policy_response_value(httpx.Response(
        200, text=body, headers={"Content-Type": content_type},
    )) == expected
    assert client.current_api_policy() == ("1", expected)
    assert [request.url.params.get("format") for request in requests] == [
        None, None, "xml", "rawxml", None,
    ]


@pytest.mark.parametrize("content_type", [
    "application/json", "application/vnd.ms-azure-apim.policy.raw+xml; charset=utf-8",
])
@pytest.mark.parametrize("defect", [None, "parent", "operation", "etag", "inventory"])
@pytest.mark.parametrize("raw_expression_characters", [False, True])
def test_runtime_fixed_program_verification_reads_raw_policy_with_unchanged_guards(
    monkeypatch: pytest.MonkeyPatch, content_type: str, defect: str | None,
    raw_expression_characters: bool,
) -> None:
    from tests.backend.model_platform.test_apim_onboarding import (
        onboarding_configuration,
        onboarding_plan,
    )
    from tests.support.paths import REPOSITORY_ROOT
    from turnstile_core.domain.gateway_targets import CapabilitySupport
    from turnstile_core.integrations.apim_onboarding_policy import render_onboarding_fixed_program
    from turnstile_core.integrations.apim_policy_components import parse_policy, serialize_policy
    from turnstile_core.integrations.apim_storage_routes import FixedProgramVerifier

    settings = publisher_settings()
    request = onboarding_plan().request.model_copy(update={
        "api_id": settings.apim_api_id, "gateway_profile_id": settings.apim_gateway_profile_id,
    })
    configuration = onboarding_configuration()
    program = render_onboarding_fixed_program(
        request, configuration,
        (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
        self_hosted_gateway=CapabilitySupport.SUPPORTED,
    )
    installation = program.installation(
        document_digest(configuration), "https://gateway.example.test/turnstile/llm",
    )
    policies = dict(program.operation_policies)
    requests: list[httpx.Request] = []
    head_reads = 0

    def escaped_conditions(value: str) -> str:
        root = parse_policy(value)
        for node in root.iter():
            if "condition" in node.attrib:
                node.set("condition", escape(node.attrib["condition"], quote=True))
        return serialize_policy(root)

    def policy_response(value: str) -> httpx.Response:
        if content_type == "application/json":
            return httpx.Response(200, json={"properties": {"format": "rawxml", "value": value}})
        return httpx.Response(200, text=value, headers={"Content-Type": content_type})

    def handler(arm_request: httpx.Request) -> httpx.Response:
        nonlocal head_reads
        requests.append(arm_request)
        assert arm_request.url.host == "management.azure.com"
        assert arm_request.method in {"GET", "HEAD"}
        assert arm_request.headers["Authorization"].startswith("Bearer ")
        path = arm_request.url.path
        if arm_request.method == "HEAD":
            head_reads += 1
            etag = '"changed"' if defect == "etag" and head_reads == 2 else '"original"'
            return httpx.Response(200, headers={"ETag": etag})
        if path.endswith("/apis/" + settings.apim_api_id):
            return httpx.Response(200, json={"properties": {"apiRevision": "1"}})
        assert ";rev=1/" in path
        if path.endswith("/policies"):
            assert arm_request.url.params["format"] == "xml"
            return httpx.Response(200, json={"value": [{
                "id": path + ("/other" if defect == "inventory" else "/policy"),
                "name": "policy", "properties": {
                    "format": "xml", "value": escaped_conditions(program.parent_policy),
                },
            }]})
        if path.endswith("/operations"):
            return httpx.Response(200, json={"value": [{
                "id": path + "/" + name, "name": name,
                "properties": {"method": method, "urlTemplate": route},
            } for name, method, route in program.operation_contracts]})
        if path.endswith("/policies/policy"):
            representation = arm_request.url.params["format"]
            assert representation in {"rawxml", "xml"}
            if "/operations/" in path:
                name = path.split("/operations/", 1)[1].split("/", 1)[0]
                policy = policies[name]
                if defect == "operation" and name == "chat-completions":
                    policy = escaped_conditions(policy)
            else:
                policy = program.parent_policy
                if defect == "parent":
                    policy = escaped_conditions(policy)
            if representation == "rawxml" and raw_expression_characters:
                policy = unescape(policy)
            return policy_response(policy)
        raise AssertionError(path)

    probe = Mock()
    monkeypatch.setattr(FixedProgramVerifier, "_probe", probe)
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        management = AzureApimPublisherClient(settings, StubTokenProvider(), transport)
        verifier = FixedProgramVerifier(
            installation, management, SecretStr("offline-probe-key"), transport, clock=lambda: 100,
        )
        if defect is not None:
            messages = {
                "parent": "Fixed route program parent changed",
                "operation": "Fixed route program operation changed",
                "etag": "APIM parent audit version changed",
                "inventory": "APIM parent audit identity is invalid",
            }
            with pytest.raises(PolicyCompilationError, match=messages[defect]):
                verifier.verify()
            probe.assert_not_called()
        else:
            assert verifier.verify().identity == installation.program
            probe.assert_called_once()
            before_request = Mock()
            policy_path = f"/apis/{settings.apim_api_id};rev=1/policies/policy"
            assert management._policy_value(
                policy_path, before_request=before_request,
            ) == program.parent_policy
            assert before_request.call_count == 4 + int(raw_expression_characters)
            with monkeypatch.context() as interrupted:
                interrupted.setattr(management, "_request", Mock(side_effect=httpx.ReadTimeout(
                    "Offline policy read timeout",
                )))
                with pytest.raises(httpx.ReadTimeout):
                    management._policy_value(
                        f"/apis/{settings.apim_api_id};rev=1/operations/chat-completions/policies/policy",
                        before_request=before_request,
                    )


@pytest.mark.parametrize("failure", ["invalid-xml", "missing-value", "http", "timeout"])
def test_policy_representation_fallback_only_handles_xml_syntax(failure: str) -> None:
    formats: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        formats.append(request.url.params["format"])
        if failure == "timeout":
            raise httpx.ReadTimeout("Offline policy timeout", request=request)
        if failure == "http":
            return httpx.Response(403, json={"error": {"code": "AuthorizationFailed"}})
        return httpx.Response(200, json={"properties": {
            "value": "<policies>&&</policies>" if failure == "invalid-xml" else None,
        }})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = AzureApimPublisherClient(publisher_settings(), StubTokenProvider(), transport)
        if failure == "timeout":
            with pytest.raises(httpx.ReadTimeout):
                client._policy_value("/apis/turnstile-llm;rev=1/policies/policy")
        else:
            with pytest.raises((PolicyCompilationError, httpx.HTTPStatusError)):
                client._policy_value("/apis/turnstile-llm;rev=1/policies/policy")
    assert formats == (["rawxml", "xml"] if failure == "invalid-xml" else ["rawxml"])


@pytest.mark.parametrize("observed", [
    None, {"secret": False}, {"secret": True, "provisioningState": "Creating"},
    {"secret": True, "keyVault": {"secretIdentifier": "https://foreign.test/secret"}},
])
def test_named_value_readback_rejects_unready_or_wrong_credential_sources(
    observed: dict[str, Any] | None,
) -> None:
    reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.method == "GET":
            reads += 1
            return httpx.Response(404) if reads == 1 or observed is None else httpx.Response(
                200, json={"properties": observed},
            )
        assert request.method == "PUT"
        return httpx.Response(201)

    client = AzureApimPublisherClient(
        publisher_settings(), cast(Any, StubTokenProvider()),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises((PolicyCompilationError, RetryablePublicationError)):
        client.ensure_named_value(NamedValueResource(
            id="unit-value", key_vault_secret_id=None, value="unit-test-credential",
        ))
    assert reads == 2
    with pytest.raises(CredentialSourceMissingError) as missing:
        AzureApimPublisherClient(
            publisher_settings(), cast(Any, StubTokenProvider()),
            httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
        ).ensure_named_value(NamedValueResource(id="unit-missing", key_vault_secret_id=None))
    assert missing.value.named_value_id == "unit-missing"


def test_arm_client_creates_tls_backend_and_secret_named_values() -> None:
    requests: list[httpx.Request] = []
    resources: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            if request.url.path in resources:
                return httpx.Response(200, json=resources[request.url.path])
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        if request.method == "PUT":
            resources[request.url.path] = json.loads(request.content)
        return httpx.Response(201, json={})

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.ensure_backend(
        BackendResource(
            id="turnstile-dyn-test-1",
            title="FinOps Bedrock",
            url="https://bedrock-runtime.ap-southeast-2.amazonaws.com",
            headers=(
                ("x-adapter-key", "{{turnstile-envoy-adapter-key}}"),
                (
                    "x-turnstile-upstream-host",
                    "bedrock-runtime.ap-southeast-2.amazonaws.com",
                ),
            ),
        )
    )
    client.ensure_named_value(
        NamedValueResource(
            id="bedrock-bearer-key",
            key_vault_secret_id="https://vault.vault.azure.net/secrets/bedrock-key",
        )
    )
    client.ensure_named_value(
        NamedValueResource(
            id="bedrock-direct-key",
            key_vault_secret_id=None,
            owner_publication_id="publication-one",
            value="one-time-direct-key",
        )
    )
    client.put_operation_policy(
        "turnstile-1-test", "anthropic-messages",
        '<policies><inbound><base/></inbound><backend><forward-request buffer-response="false"/>'
        '</backend><outbound><base/></outbound><on-error><base/></on-error></policies>',
    )

    backend = next(
        request
        for request in requests
        if "/backends/" in request.url.path and request.method == "PUT"
    )
    backend_body = json.loads(backend.content)["properties"]
    assert backend_body["tls"] == {
        "validateCertificateChain": True,
        "validateCertificateName": True,
    }
    assert backend_body["credentials"]["header"] == {
        "x-adapter-key": ["{{turnstile-envoy-adapter-key}}"],
        "x-turnstile-upstream-host": [
            "bedrock-runtime.ap-southeast-2.amazonaws.com"
        ],
    }
    key_vault_value = next(
        request
        for request in requests
        if request.url.path.endswith("/namedValues/bedrock-bearer-key")
        and request.method == "PUT"
    )
    assert json.loads(key_vault_value.content)["properties"]["keyVault"][
        "secretIdentifier"
    ] == "https://vault.vault.azure.net/secrets/bedrock-key"
    direct_value = next(
        request
        for request in requests
        if request.url.path.endswith("/namedValues/bedrock-direct-key")
        and request.method == "PUT"
    )
    direct_properties = json.loads(direct_value.content)["properties"]
    assert direct_properties["secret"] is True
    assert direct_properties["value"] == "one-time-direct-key"
    assert direct_properties["tags"] == ["turnstile_publication_publication_one"]
    assert all(
        character.isalnum() or character in "._"
        for character in direct_properties["tags"][0]
    )
    assert "one-time-direct-key" not in repr(
        NamedValueResource(
            id="bedrock-direct-key",
            key_vault_secret_id=None,
            owner_publication_id="publication-one",
            value="one-time-direct-key",
        )
    )
    policy = next(request for request in requests
                  if request.method == "PUT" and request.url.path.endswith("/policies/policy"))
    assert policy.headers["If-None-Match"] == "*"
    assert "If-Match" not in policy.headers
    assert "bedrock-key" not in policy.content.decode("utf-8")

def test_arm_client_refreshes_an_existing_direct_secret_named_value() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "displayName": "managed-key",
                        "secret": True,
                        "tags": ["turnstile_publication_publication_one"],
                    }
                },
            )
        return httpx.Response(200, json={})

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.ensure_named_value(
        NamedValueResource(
            id="managed-key",
            key_vault_secret_id=None,
            owner_publication_id="publication-one",
            value="replacement-key",
        )
    )

    update = next(request for request in requests if request.method == "PUT")
    properties = json.loads(update.content)["properties"]
    assert properties == {
        "displayName": "managed-key",
        "secret": True,
        "value": "replacement-key",
        "tags": ["turnstile_publication_publication_one"],
    }

def test_arm_client_never_refreshes_another_publications_direct_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "displayName": "managed-key",
                        "secret": True,
                        "tags": ["turnstile_publication_publication_one"],
                    }
                },
            )
        raise AssertionError("A foreign publication must not overwrite this secret")

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(PolicyCompilationError, match="another publication"):
        client.ensure_named_value(
            NamedValueResource(
                id="managed-key",
                key_vault_secret_id=None,
                owner_publication_id="publication-two",
                value="replacement-key",
            )
        )


def _active_bedrock_release() -> tuple[GatewayPublication, FakeApimClient]:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository)
    fake = FakeApimClient()
    worker = GatewayPublicationWorker(repository, fake, fake.policy)
    service.publish(bedrock_publication("dependency-check"), "owner@example.com")
    release = None
    for _ in range(8):
        release = worker.run_once("worker")
        assert release is not None
        if release.status == "active":
            break
    assert release is not None and release.status == "active"
    return release, fake


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        (None, "healthy"),
        ("missing_backend", "missing"),
        ("mismatched_named_value", "mismatched"),
    ],
)
def test_arm_client_inspects_recorded_release_dependencies(
    failure: str | None,
    expected_status: str,
) -> None:
    release, fake = _active_bedrock_release()
    assert release.apim_revision is not None
    revision = release.apim_revision
    raw_backends = release.resource_manifest.get("backends")
    assert isinstance(raw_backends, list)
    backend_ids = [str(value) for value in raw_backends]

    def backend_properties(backend_id: str) -> dict[str, Any]:
        backend = fake.backends[backend_id]
        if isinstance(backend, BackendPoolResource):
            return {
                "type": "Pool",
                "pool": {
                    "services": [
                        {
                            "id": (
                                f"/subscriptions/00000000-0000-0000-0000-000000000001"
                                f"/resourceGroups/rg-finops/providers/Microsoft.ApiManagement"
                                f"/service/apim-finops/backends/{member.backend_id}"
                            ),
                            "priority": member.priority + 1,
                            "weight": member.weight,
                        }
                        for member in backend.members
                    ]
                },
            }
        properties: dict[str, Any] = {
            "type": "Single",
            "url": backend.url,
            "tls": {
                "validateCertificateChain": True,
                "validateCertificateName": True,
            },
        }
        if backend.headers:
            properties["credentials"] = {
                "header": {name: [value] for name, value in backend.headers}
            }
        return properties

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/backends/" in path:
            backend_id = path.rsplit("/", 1)[-1]
            if failure == "missing_backend" and backend_id == backend_ids[0]:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"properties": backend_properties(backend_id)}
            )
        if "/namedValues/" in path:
            named_value_id = path.rsplit("/", 1)[-1]
            expected = next(
                value
                for value in reversed(fake.named_values)
                if value.id == named_value_id
            )
            properties: dict[str, Any] = {
                "secret": failure != "mismatched_named_value"
            }
            if expected.key_vault_secret_id is not None:
                properties["keyVault"] = {
                    "secretIdentifier": expected.key_vault_secret_id
                }
            return httpx.Response(200, json={"properties": properties})
        if path.endswith("/policies/policy"):
            operation = next(
                (
                    key
                    for key in fake.operation_policies
                    if f"/operations/{key}/" in path
                ),
                None,
            )
            value = (
                fake.operation_policies[operation]
                if operation
                else fake.api_policies[revision]
            )
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"properties": {"value": value}},
            )
        return httpx.Response(200, json={"properties": {"provisioningState": "Succeeded"}})

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.inspect_revision_dependencies(release)

    assert result.live_status == expected_status
    assert (f"missing_backend:{backend_ids[0]}" in result.issues) is (
        failure == "missing_backend"
    )
    assert any(
        issue.startswith("mismatched_named_value_identity:")
        for issue in result.issues
    ) is (failure == "mismatched_named_value")


def test_gc_plan_protects_policy_and_pool_references() -> None:
    release, _ = _active_bedrock_release()
    assert release.apim_revision is not None
    current = release.model_copy(
        update={
            "id": UUID("f0000000-0000-4000-8000-000000000002"),
            "generation": release.generation + 1,
            "apim_revision": "current-revision",
            "resource_manifest": {
                **release.resource_manifest,
                "backends": [],
                "named_values": [],
            },
        }
    )
    expired = release.model_copy(
        update={
            "apim_revision": "expired-revision",
            "resource_manifest": {
                **release.resource_manifest,
                "backends": [
                    "pool-protected",
                    "member-protected",
                    "fragment-protected",
                    "backend-protected",
                    "backend-orphan",
                ],
                "named_values": ["named-value-protected", "backend-named-value"],
            },
        }
    )
    product_linked = release.model_copy(
        update={
            "id": UUID("f0000000-0000-4000-8000-000000000003"),
            "apim_revision": "product-protected-revision",
            "resource_manifest": {
                **release.resource_manifest,
                "backends": [],
                "named_values": [],
            },
        }
    )
    settings = publisher_settings()
    resource_base = (
        f"/subscriptions/{settings.azure_subscription_id}"
        f"/resourceGroups/{settings.apim_resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{settings.apim_service_name}"
    )

    def resource(resource_type: str, name: str, properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": f"{resource_base}/{resource_type}/{name}",
            "properties": properties,
        }

    def listed(values: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(200, json={"value": values})

    def policy(value: str) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"properties": {"value": value}},
        )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        path = request.url.path
        query = request.url.query.decode()
        if (
            path.endswith("/apis")
            and "expandApiVersionSet=true" in query
            and "includeRevisions=true" in query
        ):
            return listed(
                [
                    resource(
                        "apis",
                        "turnstile-llm;rev=expired-revision",
                        {"apiRevision": "expired-revision"},
                    ),
                    resource(
                        "apis",
                        "turnstile-llm;rev=current-revision",
                        {"apiRevision": "current-revision"},
                    ),
                    resource(
                        "apis",
                        "turnstile-llm;rev=product-protected-revision",
                        {"apiRevision": "product-protected-revision"},
                    ),
                ]
            )
        if path.endswith("/apis/turnstile-llm"):
            return httpx.Response(200, json={"properties": {"apiRevision": "current-revision"}})
        if path.endswith("/apis/turnstile-llm/releases"):
            return listed(
                [
                    resource(
                        "apis/turnstile-llm/releases",
                        "product-rollback-asset",
                        {},
                    ),
                    resource(
                        "apis/turnstile-llm/releases",
                        "current-release-asset",
                        {},
                    ),
                ]
            )
        if path.endswith("/releases/product-rollback-asset"):
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "apiId": (
                            f"{resource_base}/apis/turnstile-llm;rev="
                            "product-protected-revision"
                        )
                    }
                },
            )
        if path.endswith("/releases/current-release-asset"):
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "apiId": f"{resource_base}/apis/turnstile-llm"
                    }
                },
            )
        if path.endswith(";rev=current-revision/policies/policy"):
            return policy(
                '<policies><inbound>'
                '<set-backend-service backend-id="pool-protected" />'
                '<set-backend-service backend-id="backend-protected" />'
                '</inbound></policies>'
            )
        if path.endswith(";rev=product-protected-revision/policies/policy"):
            return policy("<policies />")
        if path.endswith(";rev=current-revision/operations") or path.endswith(
            ";rev=product-protected-revision/operations"
        ):
            return listed([])
        if path.endswith("/policies/policy") and "/products/" not in path:
            return httpx.Response(404)
        if path.endswith("/products"):
            return listed([resource("products", "product-one", {})])
        if path.endswith("/products/product-one/policies/policy"):
            return policy(
                '<policies><inbound><set-header name="x-unit" exists-action="override">'
                '<value>{{named-value-protected}}</value>'
                '</set-header></inbound></policies>'
            )
        if path.endswith("/products/product-one/apis"):
            return listed(
                [
                    resource(
                        "products/product-one/apis",
                        "turnstile-llm",
                        {"apiRevision": "product-protected-revision"},
                    )
                ]
            )
        if path.endswith("/policyFragments"):
            return listed([resource("policyFragments", "fragment-one", {})])
        if path.endswith("/policyFragments/fragment-one"):
            return httpx.Response(
                200,
                json={"properties": {"value": "fragment-protected"}},
            )
        if path.endswith("/backends"):
            return listed(
                [
                    resource(
                        "backends",
                        "pool-protected",
                        {
                            "type": "Pool",
                            "pool": {
                                "services": [
                                    {"id": f"{resource_base}/backends/member-protected"}
                                ]
                            },
                        },
                    ),
                    resource("backends", "member-protected", {"type": "Single"}),
                    resource("backends", "fragment-protected", {"type": "Single"}),
                    resource(
                        "backends",
                        "backend-protected",
                        {
                            "type": "Single",
                            "credentials": {
                                "header": {"api-key": ["{{backend-named-value}}"]}
                            },
                        },
                    ),
                    resource("backends", "backend-orphan", {"type": "Single"}),
                ]
            )
        if path.endswith("/namedValues"):
            return listed(
                [
                    resource("namedValues", "named-value-protected", {}),
                    resource("namedValues", "backend-named-value", {}),
                ]
            )
        raise AssertionError(f"Unexpected APIM graph request: {request.url}")

    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    evidence = client.plan_release_garbage_collection(
        [expired, current, product_linked], {str(current.id)}
    )
    repeated = client.plan_release_garbage_collection(
        [expired, current, product_linked], {str(current.id)}
    )
    candidates = {
        (value["resource_type"], value["resource_id"])
        for value in evidence.candidates
    }

    assert repeated.candidates == evidence.candidates
    assert repeated.reference_graph_sha256 == evidence.reference_graph_sha256
    assert ("api_revision", "expired-revision") in candidates
    assert ("backend", "backend-orphan") in candidates
    assert ("backend_pool", "pool-protected") not in candidates
    assert ("backend", "member-protected") not in candidates
    assert ("backend", "fragment-protected") not in candidates
    assert ("backend", "backend-protected") not in candidates
    assert ("named_value", "named-value-protected") not in candidates
    assert ("named_value", "backend-named-value") not in candidates
    assert ("api_revision", "product-protected-revision") not in candidates


def test_gc_plan_rejects_pagination_outside_the_configured_apim_service() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/apis")
        return httpx.Response(
            200,
            json={
                "value": [],
                "nextLink": (
                    "https://management.azure.com/subscriptions/other/resourceGroups/other/"
                    "providers/Microsoft.ApiManagement/service/other/apis"
                    "?api-version=2024-05-01"
                ),
            },
        )

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(PolicyCompilationError, match="untrusted pagination URL"):
        client.plan_release_garbage_collection([], set())


def test_apim_list_accepts_same_service_pagination_on_explicit_https_port() -> None:
    settings = publisher_settings()
    resource_base = (
        f"/subscriptions/{settings.azure_subscription_id}"
        f"/resourceGroups/{settings.apim_resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{settings.apim_service_name}"
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.method == "GET"
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "value": [{"id": f"{resource_base}/backends/first"}],
                    "nextLink": (
                        f"https://management.azure.com:443{resource_base}/backends"
                        "?api-version=2024-05-01&$skip=next"
                    ),
                },
            )
        return httpx.Response(
            200,
            json={"value": [{"id": f"{resource_base}/backends/second"}]},
        )

    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert [value["id"].rsplit("/", 1)[-1] for value in client._list_all("/backends")] == [
        "first",
        "second",
    ]
    assert calls == 2
