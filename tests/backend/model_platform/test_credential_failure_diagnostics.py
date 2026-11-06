from __future__ import annotations

import json
import re
from contextlib import nullcontext
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import HttpUrl, SecretStr

from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.control_plane_support import (
    APIM_ID,
    GatewayControlPlaneService,
    activate_publication,
    external_tenant_foundry_publication,
    transition_publication,
)
from tests.platform.contracts.test_openapi import _yaml_document
from turnstile_core.config import Settings
from turnstile_core.domain.budget_counting import BudgetPublicationContracts
from turnstile_core.domain.control_plane import (
    GatewayCredentialRotation,
    GatewayPublication,
    GatewayPublicationCreate,
    GatewayPublicationFailure,
    GatewayPublicationRetry,
    GatewayPublicationView,
    ModelCreateTarget,
    PublicationKind,
    PublicationStatus,
    RuntimeTarget,
    publication_failure,
)
from turnstile_core.domain.runtime_models import (
    ConnectionAuthMode,
    ModelConnectionCreate,
    ProviderTarget,
)
from turnstile_core.domain.storage_routes import (
    ActiveRouteHead,
    ConnectionApproval,
    FixedProgramInstallation,
    ProgramCapabilityProof,
    ProgramIdentity,
    RouteScope,
    document_digest,
)
from turnstile_core.integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    RetryablePublicationError,
)
from turnstile_core.integrations.apim_storage_routes import fixed_operation_contracts
from turnstile_core.integrations.apim_target_client_factory import ApimTargetClientConfig
from turnstile_core.integrations.route_credential_vault import RouteCredentialVault
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.control_plane import ControlPlaneConflictError
from turnstile_core.services.route_publication import (
    build_route_publication_task,
)
from turnstile_core.services.storage_route_publisher import FixedRoutePublisherFactory


@pytest.mark.parametrize("status_code", [401, 403])
@pytest.mark.parametrize("operation", ["materialize", "verify"])
def test_vault_access_denial_is_not_an_occupied_or_missing_credential(
    status_code: int, operation: str,
) -> None:
    requests: list[httpx.Request] = []
    checkpoints: list[dict[str, object]] = []
    tokens = Mock()
    tokens.token.return_value = "diagnostic-test-token"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json={"error": "private-upstream-body"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        vault = RouteCredentialVault(
            "https://unit-vault.vault.azure.net", CredentialCipher(Fernet.generate_key()),
            tokens, client,
        )
        with pytest.raises(PolicyCompilationError) as failure:
            if operation == "materialize":
                vault.materialize(
                    UUID(int=7), "a" * 64, SecretStr("diagnostic-test-secret"),
                    checkpoint=None,
                    persist=lambda previous, candidate: checkpoints.append(candidate),
                )
            else:
                vault.verify("https://unit-vault.vault.azure.net/secrets/unit/" + "b" * 32)

    assert str(failure.value) == "Route credential Vault access was denied"
    assert "private-upstream-body" not in str(failure.value)
    assert "diagnostic-test-secret" not in str(failure.value)
    assert "diagnostic-test-token" not in str(failure.value)
    assert [request.method for request in requests] == ["GET"]
    assert checkpoints == []


@pytest.mark.parametrize(("status_code", "message"), [
    (200, "Route credential name is not an unclaimed resource"),
    (302, "Route credential availability could not be verified"),
    (400, "Route credential availability could not be verified"),
    (429, "Route credential availability could not be verified"),
    (500, "Route credential availability could not be verified"),
    (503, "Route credential availability could not be verified"),
])
def test_only_a_successful_lookup_proves_a_credential_name_conflict(
    status_code: int, message: str,
) -> None:
    requests: list[str] = []
    persist = Mock()
    tokens = Mock()
    tokens.token.return_value = "diagnostic-test-token"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(status_code, text="private-upstream-body")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        vault = RouteCredentialVault(
            "https://unit-vault.vault.azure.net", CredentialCipher(Fernet.generate_key()),
            tokens, client,
        )
        with pytest.raises(PolicyCompilationError, match=message):
            vault.materialize(
                UUID(int=7), "a" * 64, SecretStr("diagnostic-test-secret"),
                checkpoint=None, persist=persist,
            )
    assert requests == ["GET"]
    persist.assert_not_called()


@pytest.mark.parametrize("status_code", [401, 403])
@pytest.mark.parametrize("denied_phase", ["write", "readback", "recovery"])
def test_vault_denial_preserves_the_intent_and_never_replays_a_put(
    status_code: int, denied_phase: str,
) -> None:
    requests: list[str] = []
    saved: list[dict[str, Any]] = []
    document: dict[str, Any] = {}
    recovered = False
    tokens = Mock()
    tokens.token.return_value = "diagnostic-test-token"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.method == "PUT":
            assert len(saved) == 1
            assert "diagnostic-test-secret" not in json.dumps(saved)
            document.update(json.loads(request.content))
            document["id"] = str(request.url).split("?")[0] + "/" + "b" * 32
            if denied_phase == "write":
                return httpx.Response(status_code, text="private-upstream-body")
            raise httpx.ReadTimeout("uncertain-test-write")
        if not saved:
            return httpx.Response(404)
        if recovered:
            return httpx.Response(200, json=document)
        if denied_phase == "recovery" and requests.count("GET") == 2:
            return httpx.Response(404)
        return httpx.Response(status_code, text="private-upstream-body")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        vault = RouteCredentialVault(
            "https://unit-vault.vault.azure.net", CredentialCipher(Fernet.generate_key()),
            tokens, client,
        )
        expected = "read-only recovery" if denied_phase == "recovery" else "Vault access was denied"
        expected_error = (
            PolicyCompilationError if denied_phase == "write" else RetryablePublicationError
        )
        with pytest.raises(expected_error, match=expected):
            vault.materialize(
                UUID(int=7), "a" * 64, SecretStr("diagnostic-test-secret"), checkpoint=None,
                persist=lambda previous, candidate: saved.append(candidate),
            )
        assert len(saved) == 1
        checkpoint = saved[0].copy()
        with pytest.raises(RetryablePublicationError, match="Vault access was denied"):
            vault.materialize(
                UUID(int=7), "a" * 64, SecretStr("diagnostic-test-secret"), checkpoint=checkpoint,
                persist=lambda previous, candidate: saved.append(candidate),
            )
        assert saved == [checkpoint]
        recovered = True
        secret_id = vault.materialize(
            UUID(int=7), "a" * 64, SecretStr("diagnostic-test-secret"), checkpoint=checkpoint,
            persist=lambda previous, candidate: saved.append(candidate),
        )
        assert vault.verify(secret_id).get_secret_value() == "diagnostic-test-secret"
    assert requests.count("PUT") == 1
    assert len(saved) == 2 and saved[-1]["secret_id"] == secret_id
    assert "diagnostic-test-secret" not in json.dumps(saved)


@pytest.mark.parametrize("error_code", ["PolicyCompilationError", "RetryablePublicationError"])
@pytest.mark.parametrize(("message", "expected"), [
    ("Route credential Vault access was denied", "credential_vault_access_denied"),
    ("Route credential name is not an unclaimed resource", "credential_name_conflict"),
    ("Route credential version is unavailable", "credential_source_missing"),
    ("Route credential materialization is unavailable", "credential_vault_unavailable"),
    ("Route credential availability could not be verified", "publication_failed"),
    ("private-upstream-body diagnostic-test-secret", "publication_failed"),
])
def test_credential_failures_expose_only_safe_distinct_codes(
    message: str, expected: str, error_code: str,
) -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "unit-owner",
    ).model_copy(update={
        "status": PublicationStatus.FAILED, "error_code": error_code,
        "error_message": message,
    })
    failure = publication_failure(publication)
    expected_code = (
        "publication_failed" if error_code == "RetryablePublicationError"
        and message == "Route credential version is unavailable" else expected
    )
    assert failure is not None and failure.code == expected_code
    assert "private-upstream-body" not in failure.model_dump_json()
    assert "diagnostic-test-secret" not in failure.model_dump_json()
    assert message not in failure.message


@pytest.mark.parametrize("message", [{"error": "private-detail"}, ["private-detail"], 42])
def test_non_string_publication_errors_remain_safe(message: object) -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "unit-owner",
    ).model_copy(update={
        "status": PublicationStatus.FAILED, "error_code": "PolicyCompilationError",
        "error_message": message,
    })
    failure = publication_failure(publication)
    assert failure is not None and failure.code == "publication_failed"
    assert "private-detail" not in failure.model_dump_json()


@pytest.mark.parametrize("kind", [PublicationKind.MODEL_REMOVE, PublicationKind.ROUTE_RECONCILE])
@pytest.mark.parametrize("error_code", ["PolicyCompilationError", "CredentialSourceMissingError"])
def test_missing_credential_without_bindings_has_no_invalid_recovery(
    kind: PublicationKind, error_code: str,
) -> None:
    repository = InMemoryRepository()
    original = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "unit-owner",
    )
    publication = original.model_copy(update={
        "status": PublicationStatus.FAILED, "publication_kind": kind,
        "error_code": error_code, "error_message": "Route credential version is unavailable",
        "desired_spec": original.desired_spec.model_copy(update={"bindings": []}),
    })
    failure = publication_failure(publication)
    assert failure is not None and failure.code == "credential_source_missing"
    assert failure.credential_recovery is None
    assert failure.message == "The credential resource is missing."


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("message", [
    "Route credential has no approved original source",
    "Route publication credential is unavailable",
    "Route credential version is unavailable",
])
def test_model_addition_recovery_matches_the_real_retry_credential_contract(
    managed: bool, message: str,
) -> None:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository)
    publication = service.publish(external_tenant_foundry_publication(repository), "unit-owner")
    binding = publication.desired_spec.bindings[-1]
    if managed:
        binding = binding.model_copy(update={
            "key_vault_secret_id": HttpUrl(
                "https://unit-vault.vault.azure.net/secrets/existing/" + "a" * 32,
            ),
        })
    failed = publication.model_copy(update={
        "status": PublicationStatus.FAILED, "error_code": "PolicyCompilationError",
        "error_message": message,
        "desired_spec": publication.desired_spec.model_copy(update={"bindings": [binding]}),
        "resource_manifest": {"model_route_addition": {"version": 1}},
    })
    row = repository.get_gateway_publication(publication.id)
    assert row is not None
    row.update(failed.model_dump(mode="python"))
    view = GatewayPublicationView.from_publication(failed)
    assert view.retry_requires_credential is False and view.credential_kind is None
    assert view.failure is not None
    assert view.failure.credential_recovery == (
        "administrator_republish" if managed else "republish_with_key"
    )
    assert "rotation" not in view.failure.message
    assert "provide the API key to retry" not in view.failure.message.lower()
    with pytest.raises(ControlPlaneConflictError, match="cannot replace its intent"):
        service.retry(publication.id, GatewayPublicationRetry(api_key=SecretStr("replacement")),
                      "unit-owner")
    assert row["status"] == "failed"


def test_credential_failure_schema_and_types_match_the_safe_domain_contract() -> None:
    root = Path(__file__).resolve().parents[3]
    schema = cast(dict[str, Any], _yaml_document(
        root / "contracts/openapi/schemas/model-platform.yaml",
    ))
    properties = schema["schemas"]["GatewayPublicationFailure"]["properties"]
    domain = GatewayPublicationFailure.model_json_schema()["properties"]
    assert set(properties["code"]["enum"]) == set(domain["code"]["enum"])
    source = (root / "frontend/src/data-sources/apim/types.ts").read_text()
    failure_type = source.split("export type GatewayPublicationFailure = {", 1)[1].split(
        "\n}", 1,
    )[0]
    code = next(line for line in failure_type.splitlines() if line.strip().startswith("code:"))
    assert set(re.findall(r'"([a-z_]+)"', code)) == set(properties["code"]["enum"])
    recovery = next(line for line in failure_type.splitlines()
                    if line.strip().startswith("credential_recovery?:"))
    expected = {"retry_with_key", "republish_with_key", "administrator", "administrator_republish"}
    assert set(re.findall(r'"([a-z_]+)"', recovery)) == expected
    assert set(properties["credential_recovery"]["enum"]) == expected | {None}
    assert set(domain["credential_recovery"]["anyOf"][0]["enum"]) == expected


def _managed_route_fixture() -> tuple[Any, ...]:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository, cipher)
    publication = service.publish(external_tenant_foundry_publication(repository), "unit-owner")
    current = "queued"
    for status in ("validating", "provisioning", "building_revision", "verifying", "promoting"):
        assert transition_publication(
            repository, publication.id, current, status, {}, "unit-worker",
        )
        current = status
    activate_publication(repository, publication.id, "unit-worker")
    original = publication.desired_spec.bindings[-1]
    connection = service._binding_connection(original, APIM_ID, repository.registry())
    runtime = next(row for row in repository.runtimes if row["id"] == connection["id"])
    recorded = "https://unit-vault.vault.azure.net/secrets/original/" + "a" * 32
    runtime["config"].update({
        "key_vault_secret_id": recorded, "named_value_name": original.named_value_name,
        "backend_url": str(original.backend_url), "backend_path": original.backend_path,
        "auth_strategy": original.auth_strategy.value, "api_format": original.api_format.value,
    })
    binding = original.model_copy(update={
        "runtime_id": runtime["id"], "key_vault_secret_id": HttpUrl(recorded),
        "runtime_config": dict(runtime["config"]),
    })
    spec = publication.desired_spec.model_copy(update={
        "bindings": [binding], "discovery_models": [next(
            item for item in publication.desired_spec.discovery_models
            if item.id == binding.model.model_key
        )],
    })
    _, digest = service._release_payload(spec)
    base = publication.model_copy(update={
        "status": PublicationStatus.ACTIVE, "desired_spec": spec, "desired_spec_sha256": digest,
    })
    program = ProgramIdentity(
        scope=RouteScope(target_id=str(APIM_ID), api_id="unit-api", gateway_id="managed"),
        api_revision="1", instance_id="managed", program_sha256="d" * 64,
        trust_binding_sha256="e" * 64,
    )
    proof = ProgramCapabilityProof(
        proof_id=str(UUID(int=21)), identity=program, supported_profiles=("openai-v1",),
        issued_epoch=1050, expires_epoch=1170,
    )
    contracts = fixed_operation_contracts()
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256="f" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/turnstile", publisher_subscription_id="unit",
        parent_policy_sha256="a" * 64, operation_contracts=contracts,
        operation_policies_sha256={name: "b" * 64 for name in contracts},
        supported_profiles=("openai-v1",), model_additions_enabled=True,
        credential_vault_origin="https://unit-vault.vault.azure.net",
        trusted_vault_origins=("https://unit-vault.vault.azure.net",),
    )
    configuration = ApimTargetClientConfig(APIM_ID, "f" * 64, True, Settings.model_construct(
        azure_subscription_id=str(UUID(int=1)), apim_resource_group="unit-group",
        apim_service_name="unit-apim", apim_api_id=program.scope.api_id,
        apim_gateway_url=installation.gateway_endpoint, apim_probe_subscription_id="unit",
        apim_probe_subscription_key=SecretStr("diagnostic-test-probe-key"),
    ), route_target_identity_sha256=installation.target_identity_sha256)
    fence = {"protocol_version": 2, "mode": "active", "epoch": str(UUID(int=2)),
             "gateway_profile_id": str(APIM_ID), "model_contracts": {}}
    budgets = BudgetPublicationContracts.capture(base, {"models": []}, fence)
    revision = FixedRoutePublisherFactory._new_connection(binding, installation, configuration)
    approval = ConnectionApproval(
        approval_id=str(UUID(int=22)), connection=revision, state="approved",
        evidence_sha256=document_digest(revision), issued_epoch=1000, expires_epoch=10000,
    )
    task = build_route_publication_task(
        base, program, proof, None, (), budgets, {binding.model.model_key: approval},
        installation.storage_endpoint, installation.storage_table,
        installation.target_identity_sha256, 1100,
    )
    head = ActiveRouteHead(approval=task.plan.snapshot.approval, activated_epoch=1100)
    row = repository.get_gateway_publication(base.id)
    assert row is not None
    row.update({**base.model_dump(mode="python"), "resource_manifest": {
        "fixed_route_program": installation.model_dump(mode="json"),
        "fixed_route_snapshot": task.plan.snapshot.model_dump(mode="json"),
        "fixed_route_head": head.model_dump(mode="json"),
        "budget_contracts": budgets.model_dump(mode="json"),
    }})
    return (
        repository, service, cipher, GatewayPublication.model_validate(row), installation,
        configuration, fence, proof, head,
    )


def test_route_reconcile_credential_recovery_does_not_promise_original_task_retry() -> None:
    repository, service, cipher, base, installation, *_ = _managed_route_fixture()
    candidate = service.reconcile_gateway(APIM_ID, "unit-owner")
    assert candidate.publication_kind is PublicationKind.ROUTE_RECONCILE
    assert "model_route_addition" not in candidate.resource_manifest
    assert "fixed_route_migration" not in candidate.resource_manifest
    original = str(base.desired_spec.bindings[-1].key_vault_secret_id)
    restored = False
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        assert request.method == "GET" and str(request.url).split("?")[0] == original
        return httpx.Response(200, json={
            "id": original, "value": "diagnostic-original-key", "attributes": {"enabled": True},
        }) if restored else httpx.Response(404)

    tokens = Mock()
    tokens.token.return_value = "diagnostic-test-token"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        vault = RouteCredentialVault(installation.credential_vault_origin, cipher, tokens, client)
        with pytest.raises(PolicyCompilationError, match="version is unavailable") as missing:
            vault.verify(original)
        assert transition_publication(repository, candidate.id, "queued", "failed", {
            "error_code": "PolicyCompilationError", "error_message": str(missing.value),
        }, "unit-worker")
        view = GatewayPublicationView.from_publication(service.publication(candidate.id))
        assert view.retry_requires_credential is False and view.credential_kind is None
        assert view.failure is not None
        assert view.failure.code == "credential_source_missing"
        assert view.failure.credential_recovery == "administrator"
        with pytest.raises(ControlPlaneConflictError, match="Submit a fresh route reconciliation"):
            service.retry(candidate.id, GatewayPublicationRetry(), "unit-owner")
        restored = True
        assert vault.verify(original).get_secret_value() == "diagnostic-original-key"
        with pytest.raises(ControlPlaneConflictError, match="Submit a fresh route reconciliation"):
            service.retry(candidate.id, GatewayPublicationRetry(), "unit-owner")

    assert requests == ["GET", "GET"]
    assert service.publication(candidate.id).status is PublicationStatus.FAILED
    assert service.publication(base.id).status is PublicationStatus.ACTIVE
    assert view.failure.message == (
        "The credential resource is missing. Ask the deployment administrator to restore the "
        "original credential and its read access."
    )
    assert "diagnostic-original-key" not in view.model_dump_json()
    assert "diagnostic-test-token" not in view.model_dump_json()


@pytest.mark.parametrize("operation", ["rotation", "republish", "administrator"])
def test_credential_recovery_runs_real_vault_and_publication_preparation(
    monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    import turnstile_core.services.storage_route_publisher as publisher_module

    repository, service, cipher, base, installation, configuration, fence, proof, head = (
        _managed_route_fixture()
    )
    original = str(base.desired_spec.bindings[-1].key_vault_secret_id)
    if operation == "rotation":
        candidate = service.rotate_credential(APIM_ID, GatewayCredentialRotation(
            model_key=base.desired_spec.bindings[-1].model.model_key,
            api_key=SecretStr("diagnostic-replacement-key"),
        ), "unit-owner")
        assert candidate.publication_kind is PublicationKind.CREDENTIAL_ROTATION
    elif operation == "republish":
        write = external_tenant_foundry_publication(
            repository, account="unit-initial-foundry", deployment="unit-new-model",
        )
        existing_runtime_ids = {row["id"] for row in repository.runtimes}
        connection = ModelConnectionCreate(
            gateway_profile_id=APIM_ID, provider=write.provider,
            auth_mode=ConnectionAuthMode.API_KEY,
            foundry_project_endpoint=write.runtime.foundry_project_endpoint,
            foundry_inference_endpoint=write.runtime.foundry_inference_endpoint,
        )
        registry = ModelRuntimeService(repository, Settings()).save_connection(connection)
        runtime = next(item for item in registry.runtimes if item.id not in existing_runtime_ids)
        write.runtime = RuntimeTarget(existing_id=runtime.id, api_key=write.runtime.api_key)
        first = service.publish(write, "unit-owner")
        assert transition_publication(repository, first.id, "queued", "failed", {
            "error_code": "PolicyCompilationError",
            "error_message": "Route publication credential is unavailable",
        }, "unit-worker")
        failed = service.publication(first.id)
        failure = publication_failure(failed)
        assert failure is not None and failure.credential_recovery == "republish_with_key"
        candidate = service.publish(write, "unit-owner")
        assert candidate.id != first.id
        assert (
            candidate.desired_spec.bindings[-1].model
            == first.desired_spec.bindings[-1].model
        )
    else:
        binding = base.desired_spec.bindings[-1]
        write = GatewayPublicationCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(existing_id=binding.provider_id),
            runtime=RuntimeTarget(existing_id=binding.runtime_id),
            model=ModelCreateTarget(deployment_name="unit-existing-connection-addition"),
        )
        candidate = service.publish(write, "unit-owner")
    requests: list[tuple[str, str]] = []
    documents: dict[str, dict[str, Any]] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        requests.append((request.method, url))
        if url == original and url not in documents:
            return httpx.Response(404)
        if request.url.host != "unit-vault.vault.azure.net":
            assert request.method == "GET" and "api-key" in request.headers
            return httpx.Response(200, json={"value": [{
                "name": candidate.desired_spec.bindings[-1].model.upstream_model_id,
            }]})
        assert request.url.host == "unit-vault.vault.azure.net"
        if request.method == "PUT":
            document = json.loads(request.content)
            document["id"] = url + "/" + "b" * 32
            documents[url] = documents[document["id"]] = document
            return httpx.Response(201, json={"id": document["id"]})
        return (
            httpx.Response(200, json=documents[url]) if url in documents else httpx.Response(404)
        )

    tokens = Mock()
    tokens.token.return_value = "diagnostic-test-token"
    configurations = Mock()
    configurations.resolve.return_value = configuration
    management, port = Mock(), Mock()
    management.prepare_route_connection.side_effect = (
        lambda connection, persist, *, recovery_only=False: connection.dependency_sha256
    )
    management.verify_route_connection.side_effect = lambda connection: connection.dependency_sha256
    port.projection.read_active.return_value = (head, 'W/"base"')
    port.projection.read_program_proof.return_value = (proof, 'W/"proof"')
    port.projection.read_model_active.return_value = None
    factory = FixedRoutePublisherFactory(
        repository, configurations, active_fence=lambda target: fence,
        availability_projector=Mock(), clock=lambda: 1100, cipher=cipher,
    )
    client_type = httpx.Client
    transport = httpx.MockTransport(respond)
    with client_type(transport=transport) as client:
        factory._client, factory._tokens = client, tokens
        port._client = client
        monkeypatch.setattr(publisher_module, "ManagedIdentityTokenProvider", lambda: tokens)
        monkeypatch.setattr(httpx, "Client",
                            lambda **options: client_type(transport=transport))
        monkeypatch.setattr(
            factory, "_open", lambda *args, **kwargs: nullcontext((port, management)),
        )
        if operation == "rotation":
            with pytest.raises(PolicyCompilationError, match="version is unavailable"):
                vault = RouteCredentialVault(
                    installation.credential_vault_origin, cipher, tokens, client,
                )
                vault.verify(original)
        claimed = repository.claim_gateway_publication("unit-worker", 180)
        assert claimed is not None and claimed["id"] == candidate.id
        if operation == "administrator":
            with pytest.raises(PolicyCompilationError, match="version is unavailable"):
                factory.prepare(
                    GatewayPublication.model_validate(claimed), "unit-worker", lambda: None,
                )
            assert transition_publication(repository, candidate.id, "queued", "failed", {
                "error_code": "PolicyCompilationError",
                "error_message": "Route credential version is unavailable",
            }, "unit-worker")
            failure = publication_failure(service.publication(candidate.id))
            assert failure is not None
            assert failure.credential_recovery == "administrator_republish"
            documents[original] = {"id": original, "value": "diagnostic-original-key",
                                   "attributes": {"enabled": True}}
            with pytest.raises(ControlPlaneConflictError, match="zero-probe preparation denial"):
                service.retry(candidate.id, GatewayPublicationRetry(), "unit-owner")
            failed_id = candidate.id
            candidate = service.publish(write, "unit-owner")
            assert candidate.id != failed_id
            assert service.publication(failed_id).status is PublicationStatus.FAILED
            claimed = repository.claim_gateway_publication("unit-worker", 180)
            assert claimed is not None and claimed["id"] == candidate.id
        task = factory.prepare(
            GatewayPublication.model_validate(claimed), "unit-worker", lambda: None,
        )
        row = repository.get_gateway_publication(candidate.id)
        assert row is not None
        prepared = GatewayPublication.model_validate(row)
        factory._verify_credentials(prepared, base, installation)
    assert task.publication_id == str(candidate.id)
    assert task.plan.changed_aliases == (
        candidate.desired_spec.bindings[-1].model.model_key,
    )
    assert len(task.plan.snapshot.routes) == 1
    assert management.prepare_route_connection.call_count == 1
    assert sum(method == "PUT" for method, url in requests) == (
        0 if operation == "administrator" else 1
    )
    if operation == "rotation":
        assert requests.count(("GET", original)) == 1
    elif operation == "administrator":
        assert requests.count(("GET", original)) >= 3
        assert (
            management.prepare_route_connection.call_args.args[0].credential.secret_id
            == original
        )
    assert "diagnostic-replacement-key" not in task.model_dump_json()