from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from typing import Any, Literal
from unittest.mock import Mock
from urllib.parse import unquote
from uuid import UUID

import httpx
import pytest

from tests.backend.model_platform.test_storage_route_contracts import route_fixture
from turnstile_core.domain.storage_routes import (
    ActiveRouteHead,
    ConfigurationApproval,
    ProgramCapabilityProof,
    ProgramIdentity,
    RoutePendingActivation,
    RouteSnapshot,
    RouteTransitionReceipt,
    RouteWriterState,
    canonical_document,
    catalog_digest,
    catalog_entry,
    catalog_partition,
    configuration_manifests,
    content_partition,
    document_digest,
    member_partition,
    member_row,
)
from turnstile_core.integrations.storage_route_projection import (
    RouteActivationIntent,
    RouteProjectionConflict,
    RouteProjectionUnknown,
    TableRouteProjection,
    route_entity,
)
from turnstile_core.integrations.table_batch import conditional_batch


@pytest.mark.parametrize("fault", [None, "target", "api", "publisher", "endpoint", "operations"])
@pytest.mark.parametrize("current_capabilities", [False, True])
def test_public_fixed_installation_descriptor_keeps_target_binding(
    fault: str | None, current_capabilities: bool,
) -> None:
    from pydantic import ValidationError

    from backend.bootstrap_storage_route import FixedInstallationDescriptor, installation_record
    from turnstile_core.integrations.apim_storage_routes import fixed_operation_contracts

    target_id = str(UUID(int=970))
    descriptor: dict[str, Any] = {
        "database_name": "turnstile",
        "expected_server": "public-test.postgres.database.azure.com",
        "api_name": "public-test-api",
        "epoch": str(UUID(int=971)),
        "registration": {
            "gateway_profile_id": target_id,
            "name": "Public test gateway",
            "tenant_id": str(UUID(int=972)),
            "subscription_id": str(UUID(int=973)),
            "resource_group": "public-test-rg",
            "service_name": "public-test-apim",
            "service_principal_id": str(UUID(int=974)),
            "api_id": "turnstile-llm",
            "api_path": "turnstile/llm",
            "managed_gateway_url": "https://public-test-apim.azure-api.net/turnstile/llm",
            "dashboard_subscription_id": "turnstile-dashboard",
            "probe_subscription_id": "turnstile-publisher-probe",
        },
        "binding": {
            "scope": {"target_id": target_id, "api_id": "turnstile-llm", "gateway_id": "managed"},
            "api_revision": "initial-fixed",
            "table_endpoint": "https://publictest.table.core.windows.net",
            "table_name": "ModelRoutes",
            "ledger_table_endpoint": "https://publictest.table.core.windows.net",
            "ledger_table_name": "TurnstileLedger",
            "trust_binding_sha256": "a" * 64,
            "program_sha256": "b" * 64,
            "trusted_vault_origins": ["https://public-test-kv.vault.azure.net"],
            "observer_key_named_value": "turnstile-observer-cache-key",
            "publisher_subscription_id": "turnstile-publisher-probe",
        },
        "parent_policy_sha256": "c" * 64,
        "operation_policies_sha256": {name: "d" * 64 for name in fixed_operation_contracts()},
    }
    if current_capabilities:
        descriptor["binding"].update({
            "supported_profiles": ["openai-v1", "anthropic-messages", "microsoft-decision"],
            "decision_enabled": True,
            "model_additions_enabled": True,
            "native_gateway_scope": True,
            "legacy_program_sha256": "e" * 64,
            "predecessor_program_sha256": ["f" * 64],
            "retired_operations": {"old-messages": ["POST", "/_turnstile/retired/old-messages"]},
        })
        descriptor["operation_policies_sha256"] = {
            name: "d" * 64 for name in fixed_operation_contracts(
                retired_operations={"old-messages": ("POST", "/_turnstile/retired/old-messages")},
                decision_enabled=True,
            )
        }
    if fault == "target":
        descriptor["binding"]["scope"]["target_id"] = str(UUID(int=975))
    elif fault == "api":
        descriptor["binding"]["scope"]["api_id"] = "other-api"
    elif fault == "publisher":
        descriptor["binding"]["publisher_subscription_id"] = "other-publisher"
    elif fault == "endpoint":
        descriptor["registration"]["managed_gateway_url"] = "https://other.example.test/api"
    elif fault == "operations":
        policies = descriptor["operation_policies_sha256"]
        policies.pop(next(iter(policies)))
    if fault:
        with pytest.raises(ValidationError):
            FixedInstallationDescriptor.model_validate_json(json.dumps(descriptor))
    else:
        parsed = FixedInstallationDescriptor.model_validate_json(json.dumps(descriptor))
        assert parsed.database_name == "turnstile"
        assert parsed.program.api_revision == "initial-fixed"
        assert parsed.binding.scope.target_id == target_id
        installed = installation_record(parsed, "9" * 64)
        assert installed.decision_enabled is current_capabilities
        assert installed.model_additions_enabled is current_capabilities
        assert installed.native_gateway_scope is current_capabilities
        assert installed.legacy_program_sha256 == parsed.binding.legacy_program_sha256
        assert installed.predecessor_program_sha256 == parsed.binding.predecessor_program_sha256
        assert installed.retired_operations == parsed.binding.retired_operations


@pytest.mark.parametrize("fault", [None, "target", "api", "gateway", "nonce", "instance"])
def test_native_binding_discovery_rejects_wrong_authenticated_identity(
    monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    from pathlib import Path

    from pydantic import SecretStr

    from tests.backend.model_platform.test_apim_onboarding import (
        onboarding_configuration,
        onboarding_plan,
    )
    from turnstile_core.domain.gateway_targets import CapabilitySupport
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.integrations.apim_onboarding_policy import render_onboarding_fixed_program
    from turnstile_core.integrations.apim_storage_routes import FixedProgramVerifier

    plan = onboarding_plan(service_tier="Developer")
    source = Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml"
    artifact = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(), source.read_text(),
        self_hosted_gateway=CapabilitySupport.SUPPORTED,
    )
    installed = artifact.installation("b" * 64, "https://managed.example.test/api")
    verified: list[str] = []

    def verify(verifier: FixedProgramVerifier) -> Any:
        verified.append(verifier.installation.program.instance_id)
        assert verifier.installation.program.scope.gateway_id == "customer-native"
        return Mock()

    monkeypatch.setattr(FixedProgramVerifier, "verify", verify)

    def response(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        assert request.headers["Ocp-Apim-Subscription-Key"] == "isolated-unit-key"
        assert request.url.path.endswith("/_turnstile/program")
        headers = {"abi": "2", "nonce": request.headers["x-turnstile-route-probe"],
                   "target": installed.program.scope.target_id,
                   "api": installed.program.scope.api_id,
                   "revision": installed.program.api_revision, "gateway": "customer-native",
                   "instance": "customer-instance", "program": installed.program.program_sha256,
                   "trust": installed.program.trust_binding_sha256}
        if fault:
            headers[fault] = "managed" if fault == "instance" else "wrong"
        return httpx.Response(204, headers={"x-turnstile-route-" + key: value
                                            for key, value in headers.items()})

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        if fault:
            with pytest.raises(PolicyCompilationError):
                FixedProgramVerifier.native_installation(
                    installed, "customer-native", "https://customer.example.test/api",
                    Mock(), SecretStr("isolated-unit-key"), client,
                )
            assert verified == []
        else:
            native = FixedProgramVerifier.native_installation(
                installed, "customer-native", "https://customer.example.test/api",
                Mock(), SecretStr("isolated-unit-key"), client,
            )
            assert native.program.instance_id == "customer-instance"
            assert verified == ["customer-instance"]


@pytest.mark.parametrize("fault", [None, "retired", "target", "undeclared"])
def test_self_hosted_program_uses_registered_binding_without_node_enrollment(
    monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    from pathlib import Path

    from pydantic import SecretStr

    from tests.backend.model_platform.test_apim_onboarding import (
        onboarding_configuration,
        onboarding_plan,
    )
    from turnstile_core.domain.gateway_targets import CapabilitySupport
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.integrations.apim_onboarding_policy import render_onboarding_fixed_program
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherFactory

    plan = onboarding_plan(service_tier="Developer")
    source = Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml"
    artifact = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(), source.read_text(),
        self_hosted_gateway=CapabilitySupport.SUPPORTED,
    )
    installed = artifact.installation("b" * 64, "https://managed.example.test/api")
    deployment = {"id": str(UUID(int=996)), "apim_target_id": installed.program.scope.target_id,
                  "native_gateway_id": "customer-native", "kind": "self_hosted",
                  "declared_state": "present", "retired_at": None,
                  "endpoint_url": "https://customer.example.test/api"}
    if fault == "retired":
        deployment["retired_at"] = "2026-10-08T00:00:00Z"
    elif fault == "target":
        deployment["apim_target_id"] = str(UUID(int=997))
    elif fault == "undeclared":
        deployment["declared_state"] = "absent"
    factory = FixedRoutePublisherFactory.__new__(FixedRoutePublisherFactory)
    factory._client, factory._tokens = Mock(), Mock()
    factory._repository, factory._configurations = Mock(), Mock()
    factory._configurations.resolve.return_value = Mock(
        settings=Mock(apim_probe_subscription_key=SecretStr("isolated-unit-key")),
    )
    native = installed.for_native_gateway("customer-native", "customer-instance",
                                          "https://customer.example.test/api")
    discover = Mock(return_value=native)
    monkeypatch.setattr("turnstile_core.services.storage_route_publisher."
                        "FixedProgramVerifier.native_installation", discover)
    if fault:
        with pytest.raises(PolicyCompilationError):
            factory.bound_gateway_programs(installed, deployment)
        discover.assert_not_called()
    else:
        assert factory.bound_gateway_programs(installed, deployment) == (native,)
        assert discover.call_args.args[:3] == (
            installed, "customer-native", "https://customer.example.test/api",
        )
    factory._repository.save_model_availability_node_enrollment.assert_not_called()
    factory._repository.model_availability_proof_guard.assert_not_called()


@pytest.mark.parametrize("fault", [None, "expired", "connection", "future"])
def test_current_connection_checks_match_members_by_alias_not_route_order(
    fault: str | None,
) -> None:
    snapshot, program, _ = snapshot_fixture()
    original_route = snapshot.routes[0]
    original_approval = snapshot.members[0].connection_approval
    first_route = original_route.model_copy(update={"alias": "z-unit"})
    second_route = original_route.model_copy(update={
        "alias": "a-unit", "model_id": str(UUID(int=981)),
        "budget": original_route.budget.model_copy(update={"model_id": UUID(int=981)}),
        "connection": original_route.connection.model_copy(update={
            "revision_id": str(UUID(int=982)),
        }),
    })
    second_approval = original_approval.model_copy(update={
        "approval_id": str(UUID(int=983)), "connection": second_route.connection,
    })
    routes = (first_route, second_route)
    members, manifest, dependencies = configuration_manifests(
        program.scope, snapshot.approval.route_version, routes,
        {first_route.connection.revision_id: original_approval,
         second_route.connection.revision_id: second_approval}, snapshot.approval.issued_epoch,
    )
    snapshot = RouteSnapshot(
        approval=snapshot.approval.model_copy(update={
            "entry_count": 2, "manifest_sha256": manifest,
            "dependency_manifest_sha256": dependencies, "catalog_sha256": catalog_digest(routes),
        }), routes=routes, members=members,
    )
    assert [route.alias for route in snapshot.routes] == ["z-unit", "a-unit"]
    assert [member.alias for member in snapshot.members] == ["a-unit", "z-unit"]
    snapshot.require_valid(snapshot.approval.issued_epoch)
    now = original_approval.expires_epoch if fault == "expired" else snapshot.approval.issued_epoch
    if fault == "future":
        now = original_approval.issued_epoch - 1
    if fault == "connection":
        snapshot = snapshot.model_copy(update={"routes": (
            first_route.model_copy(update={"connection": second_route.connection}), second_route,
        )})
    original = canonical_document(snapshot)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as client:
        projection = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "Routes", program.scope,
            UnitToken(), client,
        )
        if fault in {"connection", "future"}:
            with pytest.raises((ValueError, RouteProjectionConflict)):
                projection.require_current_connections(snapshot, program, now)
        else:
            projection.require_current_connections(snapshot, program, now)
    assert canonical_document(snapshot) == original


@pytest.mark.parametrize(
    "fault", [None, "etag", "conflict", "lost-ack", "redirect", "readback", "scope"],
)
def test_model_head_removal_is_conditional_and_never_deletes_route_history(
    monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    from turnstile_core.domain.storage_routes import ModelRouteHead

    snapshot, program, _ = snapshot_fixture()
    route = snapshot.routes[0]
    expected = ModelRouteHead(
        publication_id=str(UUID(int=997)), alias=route.alias, model_id=route.model_id,
        display_name=route.display_name, created_at=route.created_at,
        content_sha256=route.sha256,
        head=ActiveRouteHead(approval=snapshot.approval,
                             activated_epoch=snapshot.approval.issued_epoch),
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "DELETE"
        assert unquote(request.url.path) == (
            f"/Routes(PartitionKey='{program.scope.partition}',RowKey='{expected.row_key}')"
        )
        assert request.headers["If-Match"] == '"unit-etag"'
        if fault == "lost-ack":
            raise httpx.ReadTimeout("unit removal acknowledgement unavailable")
        return httpx.Response(412 if fault == "conflict" else 307 if fault == "redirect" else 204)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        projection = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "Routes", program.scope,
            UnitToken(), client,
        )
        reader = Mock(side_effect=[
            (expected, '"changed"' if fault == "etag" else '"unit-etag"'),
            (expected, '"unit-etag"') if fault == "readback" else None,
        ])
        monkeypatch.setattr(projection, "read_model_active", reader)
        candidate = expected
        if fault == "scope":
            candidate = expected.model_copy(update={"head": expected.head.model_copy(update={
                "approval": expected.head.approval.model_copy(update={
                    "scope": program.scope.model_copy(update={"target_id": str(UUID(int=998))}),
                }),
            })})
        if fault:
            with pytest.raises((RouteProjectionConflict, RouteProjectionUnknown)):
                projection.remove_model_head(candidate, '"unit-etag"')
        else:
            projection.remove_model_head(candidate, '"unit-etag"')
        assert len(requests) == (0 if fault in {"etag", "scope"} else 1)


@pytest.mark.parametrize("scenario", [
    "create", "lost-ack", "unknown", "foreign", "redirect", "disabled", "mismatch",
    "checkpoint-failure", "tampered-intent", "tampered-url", "verify-changed",
])
def test_route_vault_materialization_keeps_secrets_encrypted_and_never_replays(
    scenario: str,
) -> None:
    from cryptography.fernet import Fernet
    from pydantic import SecretStr

    from turnstile_core.security import CredentialCipher
    from turnstile_core.services.storage_route_publisher import RouteCredentialVault

    ciphertext = CredentialCipher(Fernet.generate_key())
    token = Mock()
    token.token.return_value = "unit-identity"
    publication_id, source = UUID(int=990), "a" * 64
    stored: dict[str, Any] | None = None
    saved: dict[str, Any] | None = None
    requests: list[httpx.Request] = []
    marker = "unit-private-value-never-log"

    def persist(expected: object, candidate: dict[str, Any]) -> None:
        nonlocal stored
        assert expected == stored
        assert marker not in json.dumps(candidate)
        if scenario == "checkpoint-failure":
            raise RuntimeError("UNIT_VAULT_CHECKPOINT_LOST")
        stored = deepcopy(candidate)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal saved
        requests.append(request)
        assert request.url.host == "unitvault.vault.azure.net"
        assert request.headers["Authorization"] == "Bearer unit-identity"
        assert request.url.params["api-version"] == "7.4"
        if request.method == "GET":
            if scenario == "foreign" and stored is None:
                return httpx.Response(200, json={"value": "foreign"})
            return httpx.Response(200, json=saved) if saved else httpx.Response(404)
        assert request.method == "PUT" and stored is not None
        body = json.loads(request.content)
        assert body["value"] == marker
        assert body["tags"] == {
            "turnstile-publication": str(publication_id), "turnstile-source": source,
        }
        if scenario == "redirect":
            return httpx.Response(307, headers={"Location": "https://foreign.example"})
        if scenario != "unknown":
            saved = {
                **body, "id": str(request.url).split("?")[0] + "/" + "b" * 32,
                "attributes": {"enabled": scenario != "disabled"},
                "value": "different" if scenario == "mismatch" else marker,
            }
        if scenario in {"unknown", "lost-ack"}:
            raise httpx.ReadTimeout("UNIT_SECRET_ACK_UNKNOWN")
        return httpx.Response(200, json=saved)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        vault = RouteCredentialVault(
            "https://unitvault.vault.azure.net", ciphertext, token, client, clock=lambda: 1100,
        )
        options = (publication_id, source, SecretStr(marker))
        if scenario in {
            "create", "lost-ack", "tampered-intent", "tampered-url", "verify-changed",
        }:
            secret_id = vault.materialize(*options, checkpoint=stored, persist=persist)
            assert secret_id.endswith("/" + "b" * 32)
            assert stored is not None and stored["secret_id"] == secret_id
            if scenario in {"tampered-intent", "tampered-url"}:
                if scenario == "tampered-intent":
                    stored["body_ciphertext"] = stored["body_ciphertext"][:-8] + "invalid!"
                else:
                    stored["secret_id"] = secret_id.replace(
                        "unitvault.vault.azure.net", "foreign.example",
                    )
                before = len(requests)
                with pytest.raises(ValueError):
                    vault.materialize(*options, checkpoint=stored, persist=persist)
                assert len(requests) == before
            else:
                assert vault.materialize(*options, checkpoint=stored, persist=persist) == secret_id
                vault.verify(secret_id, SecretStr(marker))
                if scenario == "verify-changed":
                    assert saved is not None
                    saved["value"] = "different-source-value"
                    with pytest.raises(ValueError):
                        vault.verify(secret_id, SecretStr(marker))
        else:
            with pytest.raises((ValueError, RuntimeError)):
                vault.materialize(*options, checkpoint=stored, persist=persist)
        if scenario == "unknown":
            assert stored is not None and stored["secret_id"] is None
            with pytest.raises(RuntimeError):
                vault.materialize(*options, checkpoint=stored, persist=persist)
    assert sum(request.method == "PUT" for request in requests) == (
        0 if scenario in {"foreign", "checkpoint-failure"} else 1
    )
    assert all(call.args == ("https://vault.azure.net",) for call in token.token.call_args_list)


@pytest.mark.parametrize("fault", [None, "missing", "expired", "identity", "profiles", "endpoint"])
def test_publication_checks_every_physical_serving_node(fault: str | None) -> None:
    from unittest.mock import Mock

    from pydantic import SecretStr

    from turnstile_core.domain.storage_routes import (
        FixedProgramInstallation,
        RouteConfigurationPlan,
        RoutePublicationTask,
        document_digest,
    )
    from turnstile_core.integrations.apim_control_plane_contract import (
        PolicyCompilationError,
        RetryablePublicationError,
    )
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherPort

    snapshot, program, proof = snapshot_fixture()
    other = program.model_copy(update={"instance_id": "unit-node-two"})
    other_proof = proof.model_copy(update={"identity": other})
    installed = FixedProgramInstallation(
        program=program, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm", publisher_subscription_id="unit",
        parent_policy_sha256="c" * 64, operation_policies_sha256={"models": "d" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
        native_gateway_scope=True,
    )
    peer_installation = installed.for_native_gateway(
        program.scope.gateway_id, other.instance_id, "https://unit-node-two.example/llm",
    )
    task = RoutePublicationTask(
        publication_id=str(UUID(int=640)), base_publication_id=None,
        desired_spec_sha256="a" * 64, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        previous_routes=(), previous_head=None, serving_programs=(program, other),
        serving_installation_sha256={
            program.instance_id: document_digest(installed),
            other.instance_id: document_digest(peer_installation),
        },
        plan=RouteConfigurationPlan.prepare(
            [], snapshot, program, proof, snapshot.approval.budget_epoch, 1100,
        ),
    )
    if fault == "expired":
        other_proof = other_proof.model_copy(update={"issued_epoch": 900, "expires_epoch": 1020})
    elif fault == "identity":
        other_proof = other_proof.model_copy(update={"identity": program})
    elif fault == "profiles":
        other_proof = other_proof.model_copy(update={"supported_profiles": ("anthropic-messages",)})
    projection, verifier, peer_verifier = Mock(), Mock(), Mock()
    projection.read_program_proof.side_effect = lambda identity: (
        (proof, '"unit-primary"') if identity == program else
        None if fault == "missing" else (other_proof, '"unit-peer"')
    )
    verifier.installation = installed
    peer_verifier.installation = peer_installation
    if fault == "endpoint":
        peer_verifier.installation = peer_installation.model_copy(update={
            "gateway_endpoint": "https://foreign-node.example/llm",
        })
    fence = {"protocol_version": 2, "mode": "active",
             "gateway_profile_id": program.scope.target_id,
             "epoch": snapshot.approval.budget_epoch}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        port = FixedRoutePublisherPort(
            projection, verifier, SecretStr("unit-probe"), client,
            active_fence=lambda: fence, verify_connections=Mock(),
            prepare_availability=Mock(return_value=True), clock=lambda: 1100,
            serving_verifiers=(verifier, peer_verifier),
        )
        if fault == "endpoint":
            with pytest.raises(PolicyCompilationError, match="serving installations changed"):
                port.check(task)
        elif fault:
            with pytest.raises(RetryablePublicationError, match="Every serving node"):
                port.check(task)
        else:
            assert port.check(task) == proof
    if fault != "endpoint":
        assert other in [call.args[0] for call in projection.read_program_proof.call_args_list]
    projection.publish_program_proof.assert_not_called()
    verifier.verify.assert_not_called()


@pytest.mark.parametrize("fault", [None, "scope", "trust", "storage", "model", "nested"])
def test_model_addition_scope_tasks_preserve_platform_bindings(fault: str | None) -> None:
    from turnstile_core.domain.storage_routes import RouteConfigurationPlan, RoutePublicationTask

    tasks = []
    for gateway_id in ("managed", "customer-node-one"):
        snapshot, program, proof = snapshot_fixture(gateway_id=gateway_id)
        tasks.append(RoutePublicationTask(
            model_addition=True, configuration_only=True,
            publication_id=str(UUID(int=645)), base_publication_id=str(UUID(int=646)),
            desired_spec_sha256="a" * 64, target_identity_sha256="b" * 64,
            storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
            previous_routes=(), previous_head=None,
            plan=RouteConfigurationPlan.prepare(
                (), snapshot, program, proof, snapshot.approval.budget_epoch, 1100,
            ),
        ))
    primary, peer = tasks
    candidate = primary.model_dump(mode="json")
    child = peer.model_dump(mode="json")
    if fault == "scope":
        child = primary.model_dump(mode="json")
    elif fault == "trust":
        child["plan"]["program"]["trust_binding_sha256"] = "f" * 64
    elif fault == "storage":
        child["storage_table"] = "ForeignRoutes"
    elif fault == "model":
        child["publication_id"] = str(UUID(int=647))
    elif fault == "nested":
        child["additional_scope_tasks"] = [primary.model_dump(mode="json")]
    candidate["additional_scope_tasks"] = [child]
    if fault:
        with pytest.raises(ValueError, match="ROUTE_SCOPE_TASK"):
            RoutePublicationTask.model_validate_json(json.dumps(candidate))
    else:
        batch = RoutePublicationTask.model_validate_json(json.dumps(candidate))
        assert len(batch.scope_tasks) == 2
        assert {task.plan.program.scope.gateway_id for task in batch.scope_tasks} == {
            "managed", "customer-node-one",
        }
        assert len({task.plan.program.scope.partition for task in batch.scope_tasks}) == 2
        assert "additional_scope_tasks" not in primary.model_dump(mode="json")


def snapshot_fixture(
    *, pool: bool = False, gateway_id: str = "managed",
) -> tuple[RouteSnapshot, ProgramIdentity, ProgramCapabilityProof]:
    route, connection = route_fixture()
    if gateway_id != route.scope.gateway_id:
        scope = route.scope.model_copy(update={"gateway_id": gateway_id})
        revision = route.connection.model_copy(update={"scope": scope})
        route = route.model_copy(update={"scope": scope, "connection": revision})
        connection = connection.model_copy(update={"connection": revision})
    if pool:
        from turnstile_core.domain.storage_routes import RouteBackendPool

        backend_pool = RouteBackendPool.model_validate({
            "members": tuple(
                {"runtime_id": str(UUID(int=980 + index)), "backend_id": f"unit-member-{index}",
                 "dependency_sha256": str(index + 1) * 64, "priority": 0, "weight": index + 1}
                for index in range(2)
            ),
            "rate_limit": {
                "max_attempts_per_request": 2, "retry_interval_seconds": 1,
                "first_fast_retry": True, "circuit_breaker": {
                    "failure_count": 1, "interval_seconds": 60,
                    "trip_duration_seconds": 60, "accept_retry_after": True,
                },
            },
            "credential_versions": {
                "unit-pool-key": "https://unitvault.vault.azure.net/secrets/key/" + "a" * 32,
            },
        })
        revision = route.connection.model_copy(update={"pool": backend_pool})
        route = route.model_copy(update={"connection": revision})
        connection = connection.model_copy(update={"connection": revision})
    connections = {connection.connection.revision_id: connection}
    version = str(UUID(int=20))
    _, manifest, dependencies = configuration_manifests(
        route.scope,
        version,
        [route],
        connections,
        1100,
    )
    identity = ProgramIdentity(
        scope=route.scope,
        api_revision="1",
        instance_id="unit-node",
        program_sha256="d" * 64,
        trust_binding_sha256="e" * 64,
    )
    proof = ProgramCapabilityProof(
        proof_id=str(UUID(int=21)),
        identity=identity,
        supported_profiles=("openai-v1",),
        issued_epoch=1050,
        expires_epoch=1170,
    )
    approval = ConfigurationApproval(
        approval_id=str(UUID(int=22)),
        scope=route.scope,
        route_version=version,
        generation=1,
        manifest_sha256=manifest,
        dependency_manifest_sha256=dependencies,
        catalog_sha256=catalog_digest([route]),
        entry_count=1,
        required_program_sha256=identity.program_sha256,
        required_profiles=("openai-v1",),
        budget_epoch=route.budget_epoch,
        issued_epoch=1000,
        activate_before_epoch=1600,
    )
    return RouteSnapshot.prepare(approval, [route], connections, 1100), identity, proof


class UnitToken:
    def token(self, resource: str) -> str:
        assert resource == "https://storage.azure.com/"
        return "unit-token"


def test_model_addition_stages_only_its_own_entities_and_no_global_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstile_core.domain.storage_routes import ModelRouteAddition

    snapshot, identity, _ = snapshot_fixture()
    addition = ModelRouteAddition(
        publication_id=str(UUID(int=930)), program=identity, snapshot=snapshot,
    )
    projection = TableRouteProjection(
        "https://unit.table.core.windows.net", "UnitRoutes", identity.scope,
        token_provider=UnitToken(),
    )
    documents: dict[tuple[str, str], str] = {}
    reads: list[tuple[str, str]] = []

    def read_document(partition: str, row: str, maximum: int) -> tuple[str, str] | None:
        del maximum
        reads.append((partition, row))
        value = documents.get((partition, row))
        return (value, 'W/"unit"') if value is not None else None

    def stage_entity(partition: str, row: str, document: str, maximum: int) -> None:
        assert len(document.encode()) <= maximum
        assert row not in {"active", "writer"}
        documents[(partition, row)] = document

    monkeypatch.setattr(projection, "_read_document", Mock(side_effect=read_document))
    monkeypatch.setattr(projection, "_stage_entity", Mock(side_effect=stage_entity))
    try:
        projection.stage_model_addition(addition, 1100)
    finally:
        projection.close()
    route = snapshot.routes[0]
    assert len(documents) == 4
    assert {partition for partition, _ in documents} == {
        content_partition(identity.scope),
        member_partition(identity.scope, snapshot.approval.route_version),
        catalog_partition(identity.scope, snapshot.approval.route_version),
        identity.scope.partition,
    }
    assert reads[0] == (identity.scope.partition, "model:" + member_row(route.alias))
    assert all(row not in {"active", "writer"} for _, row in reads)
    assert all("unrelated-model" not in document for document in documents.values())


def test_model_addition_rejects_a_full_gateway_snapshot() -> None:
    from turnstile_core.domain.storage_routes import ModelRouteAddition

    snapshot, identity, _ = snapshot_fixture()
    invalid = snapshot.model_copy(update={"routes": (*snapshot.routes, *snapshot.routes)})
    with pytest.raises(ValueError, match="MODEL_ADDITION_REQUIRES_SINGLE_MODEL"):
        ModelRouteAddition(publication_id=str(UUID(int=930)), program=identity, snapshot=invalid)


@pytest.mark.parametrize("fault", [None, "target", "budget", "connection", "proof", "expired"])
def test_model_addition_plan_accepts_one_route_and_its_exact_current_dependencies(
    fault: str | None,
) -> None:
    from turnstile_core.domain.storage_routes import ModelRouteAddition

    snapshot, program, proof = snapshot_fixture()
    route = snapshot.routes[0]
    approval = snapshot.members[0].connection_approval
    epoch = snapshot.approval.budget_epoch
    if fault == "target":
        program = program.model_copy(update={"scope": program.scope.model_copy(update={
            "target_id": str(UUID(int=932)),
        })})
    elif fault == "budget":
        epoch = str(UUID(int=933))
    elif fault == "connection":
        approval = approval.model_copy(update={"connection": approval.connection.model_copy(
            update={"backend_id": "another-backend"},
        )})
    elif fault == "proof":
        proof = proof.model_copy(update={"identity": program.model_copy(update={
            "program_sha256": "f" * 64,
        })})
    arguments = (str(UUID(int=930)), program, proof, route, approval, epoch,
                 1180 if fault == "expired" else 1100)
    if fault:
        with pytest.raises(ValueError):
            ModelRouteAddition.prepare(*arguments)
    else:
        addition = ModelRouteAddition.prepare(*arguments)
        assert addition.snapshot.routes == (route,)
        assert len(addition.snapshot.members) == 1
        assert addition.snapshot.approval.entry_count == 1
        assert addition.snapshot.members[0].connection_approval == approval
        assert ModelRouteAddition.prepare(*arguments) == addition
        assert addition.snapshot.approval.route_version != snapshot.approval.route_version


@pytest.mark.parametrize("previous_models", [0, 29, 100])
@pytest.mark.parametrize("fault", [None, "absent", "member", "content", "digest", "expired"])
def test_model_addition_reader_work_does_not_grow_with_old_models(
    previous_models: int, fault: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstile_core.domain.storage_routes import ModelRouteAddition, text_digest

    snapshot, program, proof = snapshot_fixture()
    route = snapshot.routes[0]
    addition = ModelRouteAddition.prepare(
        str(UUID(int=930)), program, proof, route, snapshot.members[0].connection_approval,
        snapshot.approval.budget_epoch, 1100,
    )
    head = addition.active_head(proof, route.budget_epoch, 1100)
    version = addition.snapshot.approval.route_version
    head_key = (program.scope.partition, head.row_key)
    member_key = (member_partition(program.scope, version), member_row(route.alias))
    content_key = (content_partition(program.scope), route.sha256)
    approval_key = (
        program.scope.partition,
        "approval:" + text_digest(route.sha256 + addition.snapshot.members[0].approval_sha256),
    )
    own = {
        head_key: canonical_document(head),
        member_key: canonical_document(addition.snapshot.members[0]),
        content_key: canonical_document(route),
    }
    old = {(program.scope.partition, "model:" + member_row(f"old-{index}")): "untouched"
           for index in range(previous_models)}
    if fault == "absent":
        own.pop(head_key)
    elif fault == "member":
        own.pop(member_key)
    elif fault == "content":
        own.pop(content_key)
    elif fault == "digest":
        own[content_key] = canonical_document(route.model_copy(update={"display_name": "Changed"}))
    reads: list[tuple[str, str]] = []

    def read_document(partition: str, row: str, maximum: int) -> tuple[str, str] | None:
        del maximum
        key = (partition, row)
        assert key not in old
        assert key in {head_key, member_key, content_key, approval_key}
        reads.append(key)
        document = own.get(key)
        return (document, 'W/"own-record"') if document is not None else None

    projection = TableRouteProjection(
        "https://unit.table.core.windows.net", "UnitRoutes", program.scope, UnitToken(),
    )
    with projection:
        monkeypatch.setattr(projection, "_read_document", read_document)
        writes = Mock(side_effect=AssertionError("MODEL_READER_ATTEMPTED_WRITE"))
        monkeypatch.setattr(projection, "_stage_entity", writes)
        arguments = (route.alias, program, proof, route.budget_epoch,
                     1180 if fault == "expired" else 1100)
        if fault in {"member", "content", "digest", "expired"}:
            with pytest.raises(ValueError):
                projection.read_model_route(*arguments)
        else:
            assert projection.read_model_route(*arguments) == (None if fault == "absent" else route)
        writes.assert_not_called()
    assert len(reads) == (1 if fault in {"absent", "expired"} else 4 if fault is None else 3)
    assert all(value == "untouched" for value in old.values())


@pytest.mark.parametrize("fault", [None, "routes", "members", "head", "migration", "base"])
def test_model_addition_task_never_carries_previous_gateway_models(fault: str | None) -> None:
    from turnstile_core.domain.storage_routes import RouteConfigurationPlan, RoutePublicationTask

    snapshot, program, proof = snapshot_fixture()
    plan = RouteConfigurationPlan.prepare(
        (), snapshot, program, proof, snapshot.approval.budget_epoch, 1100,
    )
    values: dict[str, Any] = {
        "model_addition": True, "publication_id": str(UUID(int=930)),
        "base_publication_id": str(UUID(int=929)), "desired_spec_sha256": "a" * 64,
        "target_identity_sha256": "b" * 64,
        "storage_endpoint": "https://unit.table.core.windows.net", "storage_table": "UnitRoutes",
        "plan": plan, "previous_routes": (), "previous_members": (), "previous_head": None,
    }
    if fault == "routes":
        values["previous_routes"] = snapshot.routes
    elif fault == "members":
        values["previous_members"] = snapshot.members
    elif fault == "head":
        values["previous_head"] = ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100)
    elif fault == "migration":
        values["program_migration_sha256"] = "c" * 64
    elif fault == "base":
        values["base_publication_id"] = None
    if fault:
        with pytest.raises(ValueError):
            RoutePublicationTask.model_validate(values)
    else:
        task = RoutePublicationTask.model_validate(values)
        assert task.addition().snapshot.routes == snapshot.routes
        assert task.plan.changed_aliases == (snapshot.routes[0].alias,)
        assert task.plan.removed_aliases == ()
        assert task.previous_routes == ()
        assert task.previous_members == ()
        assert RoutePublicationTask.model_validate_json(canonical_document(task)) == task


@pytest.mark.parametrize(
    "fault", [None, "source", "head", "native-denial", "intent", "unknown", "lost-ack"],
)
def test_independent_removal_executor_keeps_program_and_never_replays_unknown(
    monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from tests.backend.model_platform.control_plane_support import (
        GatewayControlPlaneService,
        bedrock_publication,
    )
    from turnstile_core.domain.control_plane import (
        ApiFormat,
        GatewayReleaseSpec,
        ModelRemovalTarget,
        PublicationKind,
        PublicationStatus,
    )
    from turnstile_core.domain.storage_routes import ModelRouteHead
    from turnstile_core.integrations.apim_control_plane_contract import (
        PolicyCompilationError,
        RetryablePublicationError,
    )
    from turnstile_core.persistence.in_memory import InMemoryRepository
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherFactory

    snapshot, program, proof = snapshot_fixture()
    route = snapshot.routes[0]
    target_id = UUID(program.scope.target_id)
    draft = GatewayControlPlaneService(InMemoryRepository()).publish(
        bedrock_publication(), "unit-owner",
    )
    spec = draft.desired_spec.model_copy(update={"gateway_profile_id": target_id,
        "discovery_models": [
        item.model_copy(update={"id": route.alias})
        if item.id == draft.desired_spec.bindings[0].model.model_key else item
        for item in draft.desired_spec.discovery_models
    ], "bindings": [
        draft.desired_spec.bindings[0].model_copy(update={"model":
            draft.desired_spec.bindings[0].model.model_copy(update={"model_key": route.alias})}),
    ]})
    source = draft.model_copy(update={
        "gateway_profile_id": target_id, "status": PublicationStatus.ACTIVE,
        "desired_spec": spec, "resource_manifest": {
            "model_route_addition": {}, "fixed_route_snapshot": snapshot.model_dump(mode="json"),
            "fixed_route_head": ActiveRouteHead(approval=snapshot.approval,
                                                 activated_epoch=1100).model_dump(mode="json"),
        },
    })
    base = source.model_copy(update={"id": UUID(int=980)})
    removed = ModelRemovalTarget(
        model_id=UUID(route.model_id), model_key=route.alias,
        display_name=route.display_name, api_format=ApiFormat("openai_chat"),
    )
    deletion_spec = GatewayReleaseSpec(gateway_profile_id=target_id, discovery_models=[],
                                       bindings=[], removed_models=[removed])
    payload, _ = GatewayControlPlaneService._release_payload(deletion_spec)
    marker = {"version": 1, "program_release_id": str(base.id),
              "source_publication_id": str(source.id),
              "source_spec_sha256": source.desired_spec_sha256,
              "model_id": route.model_id, "alias": route.alias}
    removal = draft.model_copy(update={
        "id": UUID(int=981), "base_release_id": base.id, "gateway_profile_id": target_id,
        "publication_kind": PublicationKind.MODEL_REMOVE,
        "desired_spec": deletion_spec,
        "desired_spec_sha256": document_digest({"model_route_removal": marker,
                                                 "desired_spec": payload}),
        "resource_manifest": {"model_route_removal": marker},
    })
    repository = Mock()
    repository.get_gateway_publication.return_value = source.model_dump(mode="python")
    repository.effective_gateway_publication.return_value = base.model_dump(mode="python")
    if fault == "source":
        repository.get_gateway_publication.return_value = source.model_copy(update={
            "desired_spec_sha256": "f" * 64,
        }).model_dump(mode="python")
    factory = FixedRoutePublisherFactory(repository, Mock(), active_fence=lambda target: {
        "protocol_version": 2, "mode": "active", "gateway_profile_id": str(target),
        "epoch": snapshot.approval.budget_epoch,
    }, availability_projector=Mock(), clock=lambda: 1100)
    installation = SimpleNamespace(
        model_additions_enabled=True, native_gateway_scope=False, program=program,
    )
    monkeypatch.setattr(factory, "installed", lambda *args: (installation, None))
    expected = ModelRouteHead(publication_id=str(source.id), model_id=route.model_id,
                             alias=route.alias, display_name=route.display_name,
                             created_at=route.created_at, content_sha256=route.sha256,
                             head=ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100))
    live: tuple[ModelRouteHead, str] | None = (expected, '"unit-etag"')
    port = Mock()
    port._verifier.verify.return_value = proof
    port.projection.read_model_active.side_effect = lambda alias: live
    port.read_catalog.side_effect = AssertionError("REMOVAL_READ_UNRELATED_MODEL_CATALOG")

    def require_absent(alias: str) -> None:
        assert alias == route.alias and live is None
        if fault == "native-denial":
            raise PolicyCompilationError("Model removal native rejection is unconfirmed")

    port.require_model_absent.side_effect = require_absent

    def remove(*args: Any) -> None:
        nonlocal live
        if fault != "unknown":
            live = None
        if fault in {"unknown", "lost-ack"}:
            raise RouteProjectionUnknown("unit acknowledgement unavailable")

    port.projection.remove_model_head.side_effect = remove
    if fault == "head":
        live = None
    monkeypatch.setattr(factory, "_open", lambda *args: nullcontext((port, Mock())))

    def transition(
        identity: UUID, before: str, after: str, updates: dict[str, Any], actor: str,
    ) -> Any:
        nonlocal removal
        assert identity == removal.id and before == removal.status.value
        removal = removal.model_copy(update={"status": PublicationStatus(after), **updates})
        return removal.model_dump(mode="python")

    def checkpoint(identity: UUID, actor: str, old: Any, updated: Any) -> bool:
        nonlocal removal
        if fault == "intent":
            return False
        assert removal.resource_manifest["route_activation"] == old
        removal = removal.model_copy(update={"resource_manifest": {
            **removal.resource_manifest, "route_activation": updated,
        }})
        return True

    def activate(identity: UUID, actor: str, **kwargs: Any) -> Any:
        assert port.projection.read_model_active(route.alias) is None
        kwargs["validate_model_activation"](
            removal.model_dump(mode="python"), base.model_dump(mode="python"), None,
        )
        return removal.model_copy(update={
            "status": PublicationStatus.ACTIVE,
        }).model_dump(mode="python")

    repository.transition_gateway_publication.side_effect = transition
    repository.checkpoint_gateway_publication_activation.side_effect = checkpoint
    repository.activate_gateway_publication.side_effect = activate
    if fault in {"source", "head"}:
        with pytest.raises((ValueError, PolicyCompilationError)):
            factory.remove_model(removal, "unit-worker", Mock())
        port.projection.remove_model_head.assert_not_called()
        repository.activate_gateway_publication.assert_not_called()
        return
    for _ in range(5):
        factory.remove_model(removal, "unit-worker", Mock())
    assert removal.status.value == "promoting"
    if fault:
        with pytest.raises((RetryablePublicationError, PolicyCompilationError)):
            factory.remove_model(removal, "unit-worker", Mock())
        if fault == "lost-ack":
            assert factory.remove_model(removal, "unit-worker", Mock()).status.value == "active"
        else:
            with pytest.raises((RetryablePublicationError, PolicyCompilationError)):
                factory.remove_model(removal, "unit-worker", Mock())
            repository.activate_gateway_publication.assert_not_called()
    else:
        assert factory.remove_model(removal, "unit-worker", Mock()).status.value == "active"
    assert port.projection.remove_model_head.call_count == (0 if fault == "intent" else 1)
    port.read_catalog.assert_not_called()
    assert repository.effective_gateway_publication.return_value == base.model_dump(mode="python")


@pytest.mark.parametrize("fault", [None, "head", "success", "wrong-code", "wrong-status", "shape"])
def test_model_absence_requires_current_program_and_exact_native_rejection(
    fault: str | None,
) -> None:
    from types import SimpleNamespace

    from pydantic import SecretStr

    from turnstile_core.integrations.apim_control_plane_contract import (
        PolicyCompilationError,
        RetryablePublicationError,
    )
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherPort

    projection = Mock()
    projection.read_model_active.return_value = object() if fault == "head" else None
    verifier = Mock(installation=SimpleNamespace(
        gateway_endpoint="https://native.example.test/models",
        program=SimpleNamespace(api_revision="1"),
    ))

    def respond(request: httpx.Request) -> httpx.Response:
        verifier.verify.assert_called_once()
        assert request.url.path == "/models;rev=1/responses"
        assert request.headers["Ocp-Apim-Subscription-Key"] == "unit-probe-key"
        assert json.loads(request.content) == {"model": "removed-alias", "input": False}
        code = "different_failure" if fault == "wrong-code" else "route_configuration_unavailable"
        body = [] if fault == "shape" else {"error": {"code": code}}
        response_status = 200 if fault == "success" else 500 if fault == "wrong-status" else 503
        return httpx.Response(response_status, json=body)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        port = FixedRoutePublisherPort.__new__(FixedRoutePublisherPort)
        port.projection = projection
        port._verifier = verifier
        port._client = client
        port._subscription_key = SecretStr("unit-probe-key")
        if fault:
            with pytest.raises((RetryablePublicationError, PolicyCompilationError)):
                port.require_model_absent("removed-alias")
        else:
            port.require_model_absent("removed-alias")
        if fault == "head":
            verifier.verify.assert_not_called()
        projection.read_model_active.assert_called_once_with("removed-alias")


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("native_scope,scope_count", [(False, 1), (True, 1), (True, 2)])
def test_managed_approval_renewal_compares_raw_publication_not_decorated_view(
    monkeypatch: pytest.MonkeyPatch, changed: bool, native_scope: bool, scope_count: int,
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    import turnstile_core.services.route_publication as route_module
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherFactory

    snapshot, program, proof = snapshot_fixture()
    managed_program = program
    if native_scope:
        scope = program.scope.model_copy(update={"gateway_id": "customer-native-gateway"})
        program = program.model_copy(update={"scope": scope})
        native_route = snapshot.routes[0].model_copy(update={
            "scope": scope, "connection": snapshot.routes[0].connection.model_copy(update={
                "scope": scope,
            }),
        })
        approval = snapshot.members[0].connection_approval.model_copy(update={
            "connection": native_route.connection,
        })
        connections = {native_route.connection.revision_id: approval}
        _, manifest, dependencies = configuration_manifests(
            scope, snapshot.approval.route_version, (native_route,), connections, 1100,
        )
        snapshot = RouteSnapshot.prepare(snapshot.approval.model_copy(update={
            "scope": scope, "manifest_sha256": manifest,
            "dependency_manifest_sha256": dependencies,
        }), (native_route,), connections, 1100)
        proof = proof.model_copy(update={"identity": program})
    original_snapshot = canonical_document(snapshot)
    scope_snapshots = {program.scope: snapshot}
    scope_programs = [program]
    if scope_count > 1:
        secondary, secondary_program, _ = snapshot_fixture(gateway_id="customer-native-secondary")
        scope_snapshots[secondary_program.scope] = secondary
        scope_programs.append(secondary_program)
    route = snapshot.routes[0]
    target = UUID(program.scope.target_id)
    identity, runtime_id = UUID(int=979), UUID(int=978)
    raw = {"id": identity, "gateway_profile_id": target, "status": "active",
           "resource_manifest": {"legacy": "unchanged"}}
    current: Any = SimpleNamespace(
        id=identity, gateway_profile_id=target, resource_manifest={},
        desired_spec=SimpleNamespace(bindings=[SimpleNamespace(
            model=SimpleNamespace(model_key=route.alias), runtime_id=runtime_id,
        )]), model_dump=lambda **kwargs: {**raw, "resource_manifest": {"managed_program": True}},
    )
    repository = Mock()
    repository.effective_gateway_publication.side_effect = [
        raw, *[{**raw, "status": "superseded"} if changed else deepcopy(raw)
               for _ in range(scope_count)],
    ]
    repository.active_model_route_publications.return_value = []
    repository.registry.return_value = {"runtimes": [{
        "id": runtime_id, "gateway_profile_id": target, "enabled": True, "config": {},
    }]}
    repository.model_availability_proof_guard.return_value = nullcontext()
    now = max(1100, snapshot.members[0].connection_approval.expires_epoch - 3600)
    proof = proof.model_copy(update={"issued_epoch": now, "expires_epoch": now + 120})
    factory = FixedRoutePublisherFactory(repository, Mock(), active_fence=lambda _: {
        "protocol_version": 2, "mode": "active", "gateway_profile_id": str(target),
        "epoch": snapshot.approval.budget_epoch,
    }, availability_projector=Mock(), clock=lambda: now)
    head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100)
    monkeypatch.setattr(route_module, "recorded_route_snapshot", lambda _: (snapshot, head))
    if scope_count > 1:
        monkeypatch.setattr(route_module, "recorded_route_scopes", lambda _: tuple(scope_snapshots))
        monkeypatch.setattr(route_module, "recorded_route_snapshot", lambda _, *, scope: (
            scope_snapshots[scope], ActiveRouteHead(
                approval=scope_snapshots[scope].approval, activated_epoch=1100,
            ),
        ))
    monkeypatch.setattr(factory, "_runtime_destination_matches", Mock(return_value=True))
    monkeypatch.setattr(factory, "_credential_references", Mock(return_value={}))
    monkeypatch.setattr(factory, "_verify_credential_references", Mock())
    port, management = Mock(), Mock()
    port._verifier.verify.return_value = proof
    port.projection.read_active.side_effect = [(ActiveRouteHead(
        approval=candidate.approval, activated_epoch=1100,
    ), '"original"') for candidate in scope_snapshots.values()]
    port.projection.read_program_proof.side_effect = lambda identity: (
        proof.model_copy(update={"identity": identity}), '"native-proof"',
    )
    port.projection.read_approval_renewal_history.return_value = None
    management.verify_route_connection.side_effect = lambda revision: revision.dependency_sha256
    monkeypatch.setattr(factory, "_open", lambda *args: nullcontext((port, management)))
    installation: Any = SimpleNamespace(program=managed_program, native_gateway_scope=native_scope)
    monkeypatch.setattr(factory, "serving_programs", Mock(return_value=tuple(
        SimpleNamespace(program=identity) for identity in scope_programs
    )))
    if changed:
        with pytest.raises(ValueError, match="program base changed"):
            factory._renew_independent_approvals(current, installation, Mock())
        port.projection.publish_approval_renewal.assert_not_called()
    else:
        factory._renew_independent_approvals(current, installation, Mock())
        assert port.projection.publish_approval_renewal.call_count == scope_count
        for call in port.projection.publish_approval_renewal.call_args_list:
            renewal = call.args[0]
            actual = scope_snapshots[renewal.scope].routes[0]
            assert renewal.content_sha256 == actual.sha256
            assert renewal.approval.connection == actual.connection
        assert canonical_document(snapshot) == original_snapshot
        renewals = port.projection.publish_approval_renewal.call_args_list
        assert {call.args[0].scope for call in renewals} == set(scope_snapshots)
        assert len(renewals) == len(scope_snapshots)


class Journal:
    def __init__(self) -> None:
        self.intent: RouteActivationIntent | None = None
        self.outcomes: list[str] = []

    def begin_once(self, intent: RouteActivationIntent) -> None:
        if self.intent is not None:
            raise ValueError("UNIT_DURABLE_INTENT_ALREADY_EXISTS")
        self.intent = intent

    def record_outcome(self, operation_id: str, outcome: str) -> None:
        assert self.intent is not None and self.intent.receipt.operation_id == operation_id
        self.outcomes.append(outcome)


@pytest.mark.parametrize("failure", [None, "lost-ack", "unknown", "epoch", "existing"])
def test_empty_gateway_scope_initializes_once_with_durable_recovery(failure: str | None) -> None:
    _, identity, proof = snapshot_fixture()
    operation_id = str(UUID(int=650))
    epoch = str(UUID(int=9))
    entities: dict[tuple[str, str], dict[str, Any]] = {}
    batches: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            address = unquote(str(request.url))
            partition = address.split("PartitionKey='")[1].split("'")[0]
            row = address.split("RowKey='")[1].split("'")[0]
            value = entities.get((partition, row))
            return httpx.Response(404) if value is None else httpx.Response(
                200, json=value, headers={"ETag": '"unit-version"'},
            )
        assert request.method == "POST"
        if request.url.path != "/$batch":
            value = json.loads(request.content)
            entities[(value["PartitionKey"], value["RowKey"])] = value
            return httpx.Response(204)
        batches.append(request.content)
        recorded = entities[(identity.scope.partition, "initialization:" + operation_id)]
        intent = RouteActivationIntent.from_record(json.loads(recorded["Document"]))
        assert request.content == intent.body
        assert intent.head.approval.entry_count == 0
        if failure != "unknown":
            entities[(identity.scope.partition, "active")] = route_entity(
                identity.scope, identity.scope.partition, "active", canonical_document(intent.head),
            )
            receipt_row = "transition:" + operation_id
            entities[(identity.scope.partition, receipt_row)] = route_entity(
                identity.scope, identity.scope.partition,
                receipt_row, canonical_document(intent.receipt),
            )
        if failure in {"lost-ack", "unknown"}:
            raise httpx.ReadTimeout("isolated unknown initialization result", request=request)
        return batch_result()

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        projection = TableRouteProjection(
            "https://unit.table.core.windows.net", "UnitRoutes",
            identity.scope, UnitToken(), client,
        )
        if failure == "existing":
            snapshot, _, _ = snapshot_fixture()
            old = ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100)
            entities[(identity.scope.partition, "active")] = route_entity(
                identity.scope, identity.scope.partition, "active", canonical_document(old),
            )
            with pytest.raises(RouteProjectionConflict, match="CANNOT_REPLACE"):
                projection.initialize_empty_scope(
                    identity, proof, epoch, operation_id, lambda: 1100,
                )
            assert batches == []
            return
        if failure in {"lost-ack", "unknown"}:
            with pytest.raises(RouteProjectionUnknown):
                projection.initialize_empty_scope(
                    identity, proof, epoch, operation_id, lambda: 1100,
                )
        else:
            head = projection.initialize_empty_scope(
                identity, proof, epoch, operation_id, lambda: 1100,
            )
            assert head.approval.entry_count == 0
        previous = len(batches)
        if failure in {"unknown", "epoch"}:
            with pytest.raises(RouteProjectionUnknown, match="RECOVERY_ONLY"):
                projection.initialize_empty_scope(
                    identity, proof, str(UUID(int=651)) if failure == "epoch" else epoch,
                    operation_id, lambda: 1100,
                )
        else:
            recovered = projection.initialize_empty_scope(
                identity, proof, epoch, operation_id, lambda: 1100,
            )
            assert recovered.approval.entry_count == 0
        assert len(batches) == previous == 1


def batch_result(code: int = 204, operations: int = 2) -> httpx.Response:
    body = "--batch\r\nContent-Type: multipart/mixed; boundary=change\r\n\r\n"
    for index in range(operations if code == 204 else 1):
        body += (
            f"--change\r\nContent-Type: application/http\r\n\r\nHTTP/1.1 {code} Result\r\n"
            f"Content-ID: {index + 1}\r\n\r\n"
        )
    body += "--change--\r\n--batch--\r\n"
    return httpx.Response(
        202,
        text=body,
        headers={"Content-Type": "multipart/mixed; boundary=batch"},
    )


@pytest.mark.parametrize("failure", [
    None, "unknown", "lost-ack", "conflict", "partial", "auth", "receipt", "head",
])
@pytest.mark.parametrize("cross_second", [False, True])
def test_model_addition_commits_only_new_model_and_recovers_without_replay(
    failure: str | None, cross_second: bool,
) -> None:
    from turnstile_core.domain.storage_routes import ModelRouteAddition
    from turnstile_core.integrations.storage_route_projection import ModelRouteActivationIntent

    snapshot, identity, proof = snapshot_fixture()
    addition = ModelRouteAddition(
        publication_id=str(UUID(int=930)), program=identity, snapshot=snapshot,
    )
    scope = identity.scope
    intents: list[ModelRouteActivationIntent] = []
    writes: list[httpx.Request] = []
    reads: list[str] = []
    committed = False
    instant = 1100

    def begin_once(intent: ModelRouteActivationIntent) -> None:
        nonlocal instant
        if intents:
            raise ValueError("UNIT_MODEL_INTENT_ALREADY_EXISTS")
        intent.require_valid()
        assert ModelRouteActivationIntent.from_record(intent.to_record()) == intent
        for defect in ("body", "target", "extra", "version", "oversize"):
            changed = deepcopy(intent.to_record())
            if defect == "body":
                changed["body_base64"] = "!"
            elif defect == "target":
                changed["endpoint"] = "https://different.table.core.windows.net"
            elif defect == "extra":
                changed["unexpected"] = True
            elif defect == "version":
                changed["version"] = True
            else:
                changed["body_base64"] = "a" * 87385
            with pytest.raises(ValueError):
                ModelRouteActivationIntent.from_record(changed)
        intents.append(intent)
        if cross_second:
            instant += 1

    journal = Mock(begin_once=Mock(side_effect=begin_once))

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal committed
        if request.method == "POST":
            writes.append(request)
            assert len(intents) == 1 and request.content == intents[0].body
            assert request.url.path == "/$batch"
            assert request.content.count(b"POST ") == 2
            assert b"PUT " not in request.content and b"DELETE " not in request.content
            assert b'"RowKey":"active"' not in request.content
            assert b'"RowKey":"writer"' not in request.content
            committed = failure not in {"unknown", "conflict", "partial", "auth"}
            if failure in {"unknown", "lost-ack"}:
                raise httpx.ReadTimeout("UNIT_MODEL_ACK_UNKNOWN")
            if failure == "conflict":
                return batch_result(412)
            if failure == "partial":
                return batch_result(500)
            if failure == "auth":
                return httpx.Response(401)
            return batch_result()
        assert request.method == "GET"
        address = unquote(str(request.url))
        reads.append(address)
        assert "RowKey='active'" not in address and "RowKey='writer'" not in address
        if "RowKey='readback:" in address:
            row, document = TableRouteProjection._readback_receipt(snapshot, 3)
        elif "RowKey='model-transition:" in address:
            if not committed:
                return httpx.Response(404)
            receipt = intents[0].receipt
            row = "model-transition:" + receipt.operation_id
            if failure == "receipt":
                receipt = receipt.model_copy(update={"after_sha256": "f" * 64})
            document = canonical_document(receipt)
        elif "RowKey='model:" in address:
            if not committed or failure == "head":
                return httpx.Response(404)
            row, document = intents[0].head.row_key, canonical_document(intents[0].head)
        else:
            pytest.fail("Model addition accessed an unrelated entity")
        return httpx.Response(200, json=route_entity(scope, scope.partition, row, document),
                              headers={"ETag": 'W/"model-owned"'})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        projection = TableRouteProjection(
            "https://unit.table.core.windows.net", "UnitRoutes", scope, UnitToken(), client,
        )
        arguments = (addition, proof, snapshot.approval.budget_epoch, str(UUID(int=931)),
                     lambda: instant, journal)
        if failure is None:
            assert projection.activate_model_addition(*arguments) == intents[0].head
            assert projection.recover_model_addition(intents[0]) == "confirmed"
        else:
            with pytest.raises((RouteProjectionUnknown, RouteProjectionConflict)):
                projection.activate_model_addition(*arguments)
            if failure == "lost-ack":
                assert projection.recover_model_addition(intents[0]) == "confirmed"
            elif failure in {"unknown", "conflict", "partial", "auth"}:
                assert projection.recover_model_addition(intents[0]) == "unknown_no_replay"
        with pytest.raises((ValueError, RouteProjectionConflict)):
            projection.activate_model_addition(*arguments)
    assert len(writes) == 1
    assert reads
    assert journal.record_outcome.call_args.args[1] == (
        "confirmed" if failure is None else "conditional_conflict" if failure == "conflict"
        else "unknown_no_replay"
    )


@pytest.mark.parametrize("failure", [None, "unknown", "conflict", "partial", "auth", "readback"])
@pytest.mark.parametrize("owned", [False, True])
def test_activation_uses_one_durable_batch_and_never_replays(
    failure: str | None, owned: bool,
) -> None:
    snapshot, identity, proof = snapshot_fixture()
    scope = snapshot.approval.scope
    journal = Journal()
    writes: list[httpx.Request] = []
    writer = RouteWriterState(
        scope=scope, owner_token=str(UUID(int=26)), publication_id=str(UUID(int=24)),
        writer_epoch=1, leased_epoch=1090, lease_until_epoch=1120,
        active_generation=0, active_version=None, active_sha256=None,
        pending=RoutePendingActivation(
            operation_id=str(UUID(int=23)), base_generation=0, approval=snapshot.approval,
            snapshot_sha256=document_digest(snapshot),
        ),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            writes.append(request)
            assert journal.intent is not None and request.content == journal.intent.body
            assert request.content.count(b"POST ") == 2
            assert request.content.count(b"PUT ") == (1 if owned else 0)
            if owned:
                assert b'If-Match: W/"writer-original"' in request.content
                assert journal.intent.writer_before == writer
                assert journal.intent.writer_after is not None
                assert journal.intent.writer_after.pending is None
                assert journal.intent.writer_after.active_generation == 1
            if failure == "unknown":
                raise httpx.ReadTimeout("unit-only")
            if failure == "conflict":
                return batch_result(412)
            if failure == "partial":
                return batch_result(500)
            if failure == "auth":
                return httpx.Response(401)
            return batch_result(operations=3 if owned else 2)
        address = str(request.url)
        if "readback" in address:
            row, document = TableRouteProjection._readback_receipt(snapshot, 3)
            return httpx.Response(200, json=route_entity(
                scope, scope.partition, row, document,
            ), headers={"ETag": 'W/"readback"'})
        if "RowKey='writer'" in address:
            if not owned:
                return httpx.Response(404)
            return httpx.Response(200, json=route_entity(
                scope, scope.partition, "writer", canonical_document(writer),
            ), headers={"ETag": 'W/"writer-original"'})
        if "transition" in address:
            if not writes:
                return httpx.Response(404)
            if failure == "readback":
                raise httpx.ReadTimeout("unit receipt readback timeout")
            assert journal.intent is not None
            receipt = journal.intent.receipt
            values = route_entity(
                scope,
                scope.partition,
                "transition:" + receipt.operation_id,
                canonical_document(receipt),
            )
        elif "RowKey='active'" in address:
            return httpx.Response(404)
        elif ":catalog" in unquote(address):
            values = route_entity(
                scope,
                catalog_partition(scope, snapshot.approval.route_version),
                member_row(snapshot.routes[0].alias),
                canonical_document(catalog_entry(snapshot.routes[0])),
            )
        elif snapshot.routes[0].sha256 in address:
            values = route_entity(
                scope,
                content_partition(scope),
                snapshot.routes[0].sha256,
                canonical_document(snapshot.routes[0]),
            )
        else:
            values = route_entity(
                scope,
                member_partition(scope, snapshot.approval.route_version),
                member_row(snapshot.routes[0].alias),
                canonical_document(snapshot.members[0]),
            )
        return httpx.Response(200, json=values, headers={"ETag": 'W/"unit"'})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        store = TableRouteProjection(
            "https://unitstore.table.core.windows.net",
            "Routes",
            scope,
            UnitToken(),
            client,
        )
        options: dict[str, Any] = dict(
            snapshot=snapshot,
            identity=identity,
            proof=proof,
            active_budget_epoch=snapshot.approval.budget_epoch,
            operation_id=str(UUID(int=23)),
            expected_etag=None,
            now=lambda: 1100,
            journal=journal,
        )
        if owned:
            options["writer"] = (writer, 'W/"writer-original"')
        if failure:
            with pytest.raises(
                RouteProjectionConflict if failure == "conflict" else RouteProjectionUnknown,
            ):
                store.activate(**options)
            assert len(writes) == 1
            if failure == "readback":
                assert journal.outcomes == ["unknown_no_replay"]
            with pytest.raises(RouteProjectionUnknown if failure == "readback" else ValueError):
                store.activate(**options)
            assert len(writes) == 1
        else:
            assert store.activate(**options) == "confirmed"
            assert journal.outcomes == ["confirmed"]

        assert journal.intent is not None
        recorded = journal.intent.to_record()
        restored = RouteActivationIntent.from_record(json.loads(json.dumps(recorded)))
        assert restored == journal.intent
        corrupted = {**recorded, "body_sha256": "0" * 64}
        with pytest.raises(ValueError):
            RouteActivationIntent.from_record(corrupted)
        for payload in (
            journal.intent.body.replace(identity.program_sha256.encode(), b"f" * 64),
            journal.intent.body.replace(b"https://unitstore.", b"https://foreignstore."),
        ):
            assert payload != journal.intent.body
            corrupted = {
                **recorded, "body_base64": base64.b64encode(payload).decode(),
                "body_sha256": hashlib.sha256(payload).hexdigest(),
            }
            with pytest.raises(ValueError):
                RouteActivationIntent.from_record(corrupted)


@pytest.mark.parametrize("recovery_case", ["sent", "lost-outcome", "expired"])
def test_persistent_activation_journal_recovers_without_dispatch(recovery_case: str) -> None:
    from turnstile_core.services.route_publication import PersistentRouteActivationJournal

    snapshot, identity, _ = snapshot_fixture()
    head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100)
    receipt = RouteTransitionReceipt(
        operation_id=str(UUID(int=23)), scope=identity.scope, before_etag=None,
        before_generation=0, after_sha256=document_digest(head),
        route_version=head.approval.route_version, approval_id=head.approval.approval_id,
    )
    content_type, body = conditional_batch(
        "https://unitstore.table.core.windows.net", "Routes", identity.scope.partition,
        [("active", route_entity(identity.scope, identity.scope.partition, "active",
                                 canonical_document(head)), None),
         ("transition:" + receipt.operation_id,
          route_entity(
              identity.scope, identity.scope.partition, "transition:" + receipt.operation_id,
              canonical_document(receipt),
          ), None)],
    )
    intent = RouteActivationIntent(
        receipt, head, content_type, body, hashlib.sha256(body).hexdigest(),
        "https://unitstore.table.core.windows.net", "Routes",
    )
    repository = Mock()
    stored: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    lost_outcome = recovery_case == "lost-outcome"

    def checkpoint(
        publication_id: UUID, worker_id: str, expected: dict[str, Any] | None,
        candidate: dict[str, Any],
    ) -> bool:
        nonlocal stored
        assert publication_id == UUID(int=24) and worker_id in {"first", "second"}
        assert expected == stored
        if lost_outcome and records:
            raise RuntimeError("UNIT_OUTCOME_COMMIT_UNKNOWN")
        stored = json.loads(json.dumps(candidate))
        records.append(deepcopy(stored))
        return True

    repository.checkpoint_gateway_publication_activation.side_effect = checkpoint
    journal = PersistentRouteActivationJournal(repository, UUID(int=24), "first", "c" * 64, None)
    journal.begin_once(intent)
    if lost_outcome:
        with pytest.raises(RuntimeError, match="UNIT_OUTCOME_COMMIT_UNKNOWN"):
            journal.record_outcome(receipt.operation_id, "unknown_no_replay")
    else:
        journal.record_outcome(
            receipt.operation_id,
            "expired_before_dispatch" if recovery_case == "expired" else "unknown_no_replay",
        )
    assert stored is not None
    restarted = PersistentRouteActivationJournal(
        repository, UUID(int=24), "second", "c" * 64, deepcopy(stored),
    )
    assert restarted.intent == intent
    with pytest.raises(ValueError, match="RECOVER_ONLY"):
        restarted.begin_once(intent)
    with pytest.raises(ValueError, match="CONTEXT_CHANGED"):
        PersistentRouteActivationJournal(repository, UUID(int=24), "second", "f" * 64, stored)
    requests: list[str] = []
    fenced = False

    def read_receipt(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        assert request.method == "GET"
        if recovery_case == "expired":
            if fenced and "RowKey='active'" in unquote(str(request.url)):
                return httpx.Response(200, json=route_entity(
                    identity.scope, identity.scope.partition, "active", canonical_document(head),
                ), headers={"ETag": 'W/"other-writer"'})
            return httpx.Response(404)
        return httpx.Response(200, json=route_entity(
            identity.scope, identity.scope.partition, "transition:" + receipt.operation_id,
            canonical_document(receipt),
        ), headers={"ETag": 'W/"receipt"'})

    lost_outcome = False
    with httpx.Client(transport=httpx.MockTransport(read_receipt)) as client:
        projection = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "Routes", identity.scope,
            UnitToken(), client,
        )
        if recovery_case == "expired":
            assert restarted.recover(projection) == "unknown"
            assert records[-1]["outcome"] == "expired_before_dispatch"
            fenced = True
            assert restarted.recover(projection) == "not_applied_fenced"
        else:
            assert restarted.recover(projection) == "confirmed"
    assert requests == ["GET"] * (5 if recovery_case == "expired" else 1)
    assert records[-1]["outcome"] == (
        "not_applied_fenced" if recovery_case == "expired" else "confirmed"
    )
    assert records[0]["intent"] == records[-1]["intent"]
    with pytest.raises(ValueError, match="OUTCOME_REGRESSION"):
        restarted.record_outcome(receipt.operation_id, "unknown_no_replay")


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-epoch",
        "wrong-manifest",
        "missing-member",
        "wrong-program",
        "expired-proof",
    ],
)
def test_activation_rejects_invalid_contract_before_network_write(fault: str) -> None:
    snapshot, identity, proof = snapshot_fixture()
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        scope = snapshot.approval.scope
        address = unquote(str(request.url))
        if "active" in address or "transition" in address:
            return httpx.Response(404)
        if ":catalog" in address:
            values = route_entity(
                scope,
                catalog_partition(scope, snapshot.approval.route_version),
                member_row(snapshot.routes[0].alias),
                canonical_document(catalog_entry(snapshot.routes[0])),
            )
        elif snapshot.routes[0].sha256 in address:
            values = route_entity(
                scope,
                content_partition(scope),
                snapshot.routes[0].sha256,
                canonical_document(snapshot.routes[0]),
            )
        else:
            values = route_entity(
                scope,
                member_partition(scope, snapshot.approval.route_version),
                member_row(snapshot.routes[0].alias),
                canonical_document(snapshot.members[0]),
            )
        return httpx.Response(200, json=values, headers={"ETag": 'W/"unit"'})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        store = TableRouteProjection(
            "https://unitstore.table.core.windows.net",
            "Routes",
            snapshot.approval.scope,
            UnitToken(),
            client,
        )
        if fault == "wrong-epoch":
            snapshot = snapshot.model_copy(
                update={
                    "approval": snapshot.approval.model_copy(
                        update={"budget_epoch": str(UUID(int=99))},
                    )
                }
            )
        elif fault == "wrong-manifest":
            snapshot = snapshot.model_copy(
                update={
                    "approval": snapshot.approval.model_copy(
                        update={"manifest_sha256": "f" * 64},
                    )
                }
            )
        elif fault == "missing-member":
            snapshot = snapshot.model_copy(update={"members": ()})
        elif fault == "wrong-program":
            identity = identity.model_copy(update={"program_sha256": "f" * 64})
        else:
            proof = proof.model_copy(update={"expires_epoch": 1099})
        with pytest.raises(ValueError):
            store.activate(
                snapshot,
                identity,
                proof,
                snapshot.approval.budget_epoch,
                str(UUID(int=23)),
                None,
                lambda: 1100,
                Journal(),
            )
        assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize("fault", [None, "stale-etag", "wrong-generation"])
def test_noninitial_activation_uses_exact_head_etag(fault: str | None) -> None:
    snapshot, identity, proof = snapshot_fixture()
    scope = snapshot.approval.scope
    old_head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100)
    snapshot = snapshot.model_copy(
        update={
            "approval": snapshot.approval.model_copy(
                update={
                    "generation": 1 if fault == "wrong-generation" else 2,
                }
            )
        }
    )
    journal = Journal()
    writes: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        address = unquote(str(request.url))
        if request.method == "POST":
            writes.append(request)
            assert journal.intent is not None and journal.intent.receipt.before_generation == 1
            assert request.content.count(b'If-Match: W/"current"') == 1
            assert request.content.count(b"PUT ") == 1 and request.content.count(b"POST ") == 1
            return batch_result()
        if "RowKey='writer'" in address:
            return httpx.Response(404)
        if "transition" in address:
            if not writes:
                return httpx.Response(404)
            assert journal.intent is not None
            receipt = journal.intent.receipt
            payload = route_entity(
                scope,
                scope.partition,
                "transition:" + receipt.operation_id,
                canonical_document(receipt),
            )
        elif "RowKey='active'" in address:
            payload = route_entity(scope, scope.partition, "active", canonical_document(old_head))
        elif ":catalog" in address:
            payload = route_entity(
                scope,
                catalog_partition(scope, snapshot.approval.route_version),
                member_row(snapshot.routes[0].alias),
                canonical_document(catalog_entry(snapshot.routes[0])),
            )
        elif snapshot.routes[0].sha256 in address:
            payload = route_entity(
                scope,
                content_partition(scope),
                snapshot.routes[0].sha256,
                canonical_document(snapshot.routes[0]),
            )
        else:
            payload = route_entity(
                scope,
                member_partition(scope, snapshot.approval.route_version),
                member_row(snapshot.routes[0].alias),
                canonical_document(snapshot.members[0]),
            )
        return httpx.Response(200, json=payload, headers={"ETag": 'W/"current"'})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        store = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "Routes", scope, UnitToken(), client
        )
        expected_etag = 'W/"old"' if fault == "stale-etag" else 'W/"current"'
        if fault:
            with pytest.raises(RouteProjectionConflict):
                store.activate(
                    snapshot,
                    identity,
                    proof,
                    snapshot.approval.budget_epoch,
                    str(UUID(int=23)),
                    expected_etag,
                    lambda: 1100,
                    journal,
                )
            assert not writes and journal.intent is None
        else:
            assert (
                store.activate(
                    snapshot,
                    identity,
                    proof,
                    snapshot.approval.budget_epoch,
                    str(UUID(int=23)),
                    expected_etag,
                    lambda: 1100,
                    journal,
                )
                == "confirmed"
            )
            assert len(writes) == 1


@pytest.mark.parametrize("expired", [False, True])
def test_storage_writer_takeover_requires_expiry_and_retains_pending(expired: bool) -> None:
    from turnstile_core.domain.storage_routes import RoutePendingActivation, RouteWriterState

    snapshot, identity, _ = snapshot_fixture()
    pending = RoutePendingActivation(
        operation_id=str(UUID(int=25)), base_generation=0, approval=snapshot.approval,
        snapshot_sha256=document_digest(snapshot),
    )
    previous = RouteWriterState(
        scope=identity.scope, owner_token=str(UUID(int=26)), publication_id=str(UUID(int=24)),
        writer_epoch=1, leased_epoch=1070, lease_until_epoch=1100,
        active_generation=0, active_version=None, active_sha256=None, pending=pending,
    )
    if not expired:
        with pytest.raises(ValueError, match="LEASE_NOT_EXPIRED"):
            previous.takeover(str(UUID(int=27)), str(UUID(int=28)), 1099)
        return
    acquired = previous.takeover(str(UUID(int=27)), str(UUID(int=28)), 1100)
    assert acquired.owner_token == str(UUID(int=27))
    assert acquired.publication_id == str(UUID(int=28))
    assert acquired.writer_epoch == 2
    assert acquired.pending == previous.pending
    assert acquired.active_generation == 0 and acquired.active_sha256 is None
    with pytest.raises(ValueError, match="OWNER_OR_LEASE_CHANGED"):
        previous.require_owner(str(UUID(int=26)), str(UUID(int=24)), 1100)
    with pytest.raises(ValueError, match="OWNER_OR_LEASE_CHANGED"):
        acquired.require_owner(str(UUID(int=26)), str(UUID(int=24)), 1101)


@pytest.mark.parametrize("winner", ["activation", "takeover"])
def test_activation_recovery_checks_original_receipt_after_writer_fence(winner: str) -> None:
    snapshot, identity, _ = snapshot_fixture()
    head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100)
    pending = RoutePendingActivation(
        operation_id=str(UUID(int=23)), base_generation=0,
        approval=snapshot.approval, snapshot_sha256=document_digest(snapshot),
    )
    original = RouteWriterState(
        scope=identity.scope, owner_token=str(UUID(int=26)), publication_id=str(UUID(int=24)),
        writer_epoch=1, leased_epoch=1090, lease_until_epoch=1120,
        active_generation=0, active_version=None, active_sha256=None, pending=pending,
    )
    committed = RouteWriterState.model_validate({
        **original.model_dump(mode="python"), "pending": None, "active_generation": 1,
        "active_version": snapshot.approval.route_version, "active_sha256": document_digest(head),
    })
    receipt = RouteTransitionReceipt(
        operation_id=pending.operation_id, scope=identity.scope, before_etag=None,
        before_generation=0, after_sha256=document_digest(head),
        route_version=head.approval.route_version, approval_id=head.approval.approval_id,
    )
    content_type, body = conditional_batch(
        "https://unitstore.table.core.windows.net", "Routes", identity.scope.partition,
        [(row, route_entity(identity.scope, identity.scope.partition, row,
                            canonical_document(value)), etag)
         for row, value, etag in [
             ("writer", committed, 'W/"original"'), ("active", head, None),
             ("transition:" + receipt.operation_id, receipt, None),
         ]],
    )
    intent = RouteActivationIntent(
        receipt, head, content_type, body, hashlib.sha256(body).hexdigest(),
        "https://unitstore.table.core.windows.net", "Routes",
        original, committed, 'W/"original"',
    )
    intent = RouteActivationIntent.from_record(intent.to_record())
    takeover = original.takeover(str(UUID(int=27)), str(UUID(int=28)), 1120)
    reads: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        address = unquote(str(request.url))
        reads.append(address)
        if "transition:" in address and len(reads) == 4 and winner == "activation":
            row, value = "transition:" + receipt.operation_id, canonical_document(receipt)
        elif "RowKey='writer'" in address:
            row, value = "writer", canonical_document(takeover)
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=route_entity(
            identity.scope, identity.scope.partition, row, value,
        ), headers={"ETag": 'W/"taken-over"'})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        projection = TableRouteProjection(
            intent.endpoint, intent.table, identity.scope, UnitToken(), client,
        )
        assert projection.recover_activation(intent) == (
            "confirmed" if winner == "activation" else "not_applied_fenced"
        )
    assert len(reads) == 4
    assert "transition:" in reads[0] and "transition:" in reads[-1]


@pytest.mark.parametrize("tamper", [None, "changed", "publication", "base"])
def test_configuration_task_binds_publication_baseline_and_changed_routes(
    tamper: str | None,
) -> None:
    from turnstile_core.domain.storage_routes import RouteConfigurationPlan, RoutePublicationTask

    snapshot, program, proof = snapshot_fixture()
    plan = RouteConfigurationPlan.prepare(
        [], snapshot, program, proof, snapshot.approval.budget_epoch, 1100,
    )
    values: dict[str, Any] = {
        "publication_id": str(UUID(int=24)), "base_publication_id": None,
        "desired_spec_sha256": "a" * 64, "target_identity_sha256": "b" * 64,
        "plan": plan.model_dump(mode="json"), "previous_routes": [], "previous_head": None,
        "storage_endpoint": "https://unitstore.table.core.windows.net", "storage_table": "Routes",
    }
    if tamper == "changed":
        values["plan"]["changed_aliases"] = []
    elif tamper == "base":
        values["previous_head"] = ActiveRouteHead(
            approval=snapshot.approval, activated_epoch=1100,
        ).model_dump(mode="json")
    if tamper in {"changed", "base"}:
        with pytest.raises(ValueError):
            RoutePublicationTask.model_validate_json(json.dumps(values))
        return
    task = RoutePublicationTask.model_validate_json(json.dumps(values))
    if tamper == "publication":
        with pytest.raises(ValueError, match="PUBLICATION_CHANGED"):
            task.require_publication(str(UUID(int=25)), None, "a" * 64, program.scope.target_id)
        return
    task.require_publication(str(UUID(int=24)), None, "a" * 64, program.scope.target_id)
    assert task.plan.changed_aliases == (snapshot.routes[0].alias,)
    assert task.plan.policy_write_count == 0
    assert task.context_sha256 == document_digest(task)


@pytest.mark.parametrize("registered", ["legacy", "predecessor", "unapproved"])
@pytest.mark.parametrize("fault", [None, "missing", "wrong"])
def test_configuration_task_binds_previous_program_after_approved_upgrade(
    fault: str | None, registered: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    from turnstile_core.domain.storage_routes import RouteConfigurationPlan, RoutePublicationTask
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherFactory

    previous, original, proof = snapshot_fixture()
    program = original.model_copy(update={"program_sha256": "f" * 64})
    proof = proof.model_copy(update={"identity": program})
    connections = {
        member.connection_approval.connection.revision_id: member.connection_approval
        for member in previous.members
    }
    version = str(UUID(int=30))
    _, manifest, dependencies = configuration_manifests(
        program.scope, version, previous.routes, connections, 1100,
    )
    approval = previous.approval.model_copy(update={
        "approval_id": str(UUID(int=31)), "route_version": version, "generation": 2,
        "manifest_sha256": manifest, "dependency_manifest_sha256": dependencies,
        "required_program_sha256": program.program_sha256,
    })
    snapshot = RouteSnapshot.prepare(approval, previous.routes, connections, 1100)
    plan = RouteConfigurationPlan.prepare(
        previous.routes, snapshot, program, proof, approval.budget_epoch, 1100,
    )
    values: dict[str, Any] = {
        "publication_id": str(UUID(int=32)), "base_publication_id": str(UUID(int=33)),
        "desired_spec_sha256": "a" * 64, "target_identity_sha256": "b" * 64,
        "storage_endpoint": "https://unitstore.table.core.windows.net", "storage_table": "Routes",
        "plan": plan.model_dump(mode="json"),
        "previous_head": ActiveRouteHead(
            approval=previous.approval, activated_epoch=1100,
        ).model_dump(mode="json"),
        "previous_routes": [route.model_dump(mode="json") for route in previous.routes],
        "previous_members": [member.model_dump(mode="json") for member in previous.members],
    }
    if fault != "missing":
        values["previous_program_sha256"] = (
            "e" * 64 if fault == "wrong" else original.program_sha256
        )
    if fault is not None:
        with pytest.raises(ValueError):
            RoutePublicationTask.model_validate_json(json.dumps(values))
        return
    task = RoutePublicationTask.model_validate_json(json.dumps(values))
    assert task.previous_program_sha256 == original.program_sha256
    assert task.previous_head is not None
    assert task.previous_head.approval == previous.approval
    assert task.plan.program == program
    assert task.plan.policy_write_count == 0
    repository = Mock()
    repository.get_gateway_publication.return_value = {
        "resource_manifest": {"route_configuration": task.model_dump(mode="json")},
    }
    factory = FixedRoutePublisherFactory(
        repository, Mock(), active_fence=Mock(), availability_projector=Mock(),
    )
    installation = Mock(
        program=program,
        legacy_program_sha256=original.program_sha256 if registered == "legacy" else "e" * 64,
        predecessor_program_sha256=(original.program_sha256,)
        if registered == "predecessor" else (),
    )
    monkeypatch.setattr(factory, "installed", Mock(return_value=(installation, Mock())))
    port = Mock()
    opened = Mock(return_value=nullcontext((port, Mock())))
    monkeypatch.setattr(factory, "_open", opened)
    if registered == "unapproved":
        with (
            pytest.raises(PolicyCompilationError, match="approval changed"),
            factory.resolve(task),
        ):
            pytest.fail("Unapproved previous program opened the publication port")
        opened.assert_not_called()
    else:
        with factory.resolve(task) as resolved:
            assert resolved is port
        opened.assert_called_once()


def test_activation_cannot_bypass_an_existing_storage_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, identity, proof = snapshot_fixture()
    writer = RouteWriterState(
        scope=identity.scope, owner_token=str(UUID(int=26)), publication_id=str(UUID(int=24)),
        writer_epoch=1, leased_epoch=1090, lease_until_epoch=1120,
        active_generation=0, active_version=None, active_sha256=None,
    )
    projection = TableRouteProjection(
        "https://unitstore.table.core.windows.net", "Routes", identity.scope, UnitToken(),
    )
    monkeypatch.setattr(projection, "verify_snapshot", Mock())
    monkeypatch.setattr(projection, "read_writer", Mock(return_value=(writer, 'W/"owned"')))
    monkeypatch.setattr(projection, "read_active", Mock(return_value=None))
    request = Mock(side_effect=AssertionError("UNFENCED_REQUEST"))
    monkeypatch.setattr(projection, "_request", request)
    with pytest.raises(RouteProjectionConflict, match="WRITER_REQUIRED"):
        projection.activate(
            snapshot, identity, proof, snapshot.approval.budget_epoch,
            str(UUID(int=23)), None, lambda: 1100, Journal(),
        )
    request.assert_not_called()


@pytest.mark.parametrize("failure", [None, "lost-ack", "conflict", "journal"])
@pytest.mark.parametrize("durable", [False, True])
def test_storage_writer_claim_is_durable_conditional_and_never_replayed(
    failure: str | None, durable: bool,
) -> None:
    from turnstile_core.integrations.storage_route_projection import RouteWriterIntent
    from turnstile_core.services.route_publication import PersistentRouteWriterJournal

    snapshot, identity, _ = snapshot_fixture()
    captured: list[RouteWriterIntent] = []
    writes: list[httpx.Request] = []
    outcomes: list[str] = []

    class WriterJournal:
        def begin_once(self, intent: RouteWriterIntent) -> None:
            if failure == "journal":
                raise RuntimeError("UNIT_JOURNAL_UNAVAILABLE")
            if captured:
                raise ValueError("UNIT_WRITER_INTENT_EXISTS")
            captured.append(RouteWriterIntent.from_record(json.loads(json.dumps(intent.to_record()))))

        def record_outcome(self, operation_id: str, outcome: str) -> None:
            assert captured[0].receipt.operation_id == operation_id
            outcomes.append(outcome)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            writes.append(request)
            assert request.content == captured[0].body
            assert request.content.count(b"POST ") == 2
            assert b"RowKey='active'" not in request.content
            if failure == "lost-ack":
                raise httpx.ReadTimeout("UNIT_LOST_ACK")
            return batch_result(412 if failure == "conflict" else 204)
        if not writes or failure == "conflict" or "active" in str(request.url):
            return httpx.Response(404)
        intent = captured[0]
        row, document = (
            ("writer-transition:" + intent.receipt.operation_id, canonical_document(intent.receipt))
            if "writer-transition" in str(request.url)
            else ("writer", canonical_document(intent.after))
        )
        return httpx.Response(200, json=route_entity(
            identity.scope, identity.scope.partition, row, document,
        ), headers={"ETag": 'W/"writer-one"'})

    stored: dict[str, Any] | None = None
    repository = Mock()

    def persist(
        publication_id: UUID, worker_id: str, expected: dict[str, Any] | None,
        checkpoint: dict[str, Any],
    ) -> bool:
        nonlocal stored
        assert publication_id == UUID(int=24) and worker_id in {"first", "second"}
        assert expected == stored
        if failure == "journal":
            raise RuntimeError("UNIT_JOURNAL_UNAVAILABLE")
        stored = json.loads(json.dumps(checkpoint))
        if not captured:
            captured.append(RouteWriterIntent.from_record(stored["operations"][-1]["intent"]))
        return True

    repository.checkpoint_gateway_publication_writer.side_effect = persist
    journal = (
        PersistentRouteWriterJournal(repository, UUID(int=24), "first", "c" * 64, None)
        if durable else WriterJournal()
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        projection = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "Routes", identity.scope,
            UnitToken(), client,
        )
        options: dict[str, Any] = dict(
            owner_token=str(UUID(int=26)), publication_id=str(UUID(int=24)),
            operation_id=str(UUID(int=29)), now=lambda: 1100, journal=journal,
        )
        if failure:
            error = (RuntimeError if failure == "journal" else RouteProjectionConflict
                     if failure == "conflict" else RouteProjectionUnknown)
            with pytest.raises(error):
                projection.claim_writer(**options)
        else:
            state, etag = projection.claim_writer(**options)
            assert state.owner_token == str(UUID(int=26)) and etag == 'W/"writer-one"'
            assert state.active_generation == 0 and state.pending is None
        if failure == "journal":
            assert not writes
        else:
            assert len(writes) == 1
            assert projection.recover_writer(captured[0]) == (
                "unknown" if failure == "conflict" else "confirmed"
            )
            if durable:
                assert stored is not None
                restarted = PersistentRouteWriterJournal(
                    repository, UUID(int=24), "second", "c" * 64, stored,
                )
                assert restarted.intent == captured[0]
                with pytest.raises(ValueError, match="REUSED"):
                    restarted.begin_once(captured[0])
                assert restarted.recover(projection) == (
                    "unknown" if failure == "conflict" else "confirmed"
                )
                assert stored["operations"][0]["intent"] == captured[0].to_record()
            with pytest.raises(ValueError):
                projection.claim_writer(**options)
            assert len(writes) == 1


@pytest.mark.parametrize(
    "after_etag,receipt_present,expected",
    [
        ('W/"same"', False, "unknown"),
        ('W/"changed"', False, "not_applied_fenced"),
        ('W/"changed"', True, "confirmed"),
    ],
)
def test_unknown_recovery_requires_receipt_or_storage_fence(
    after_etag: str,
    receipt_present: bool,
    expected: str,
) -> None:
    snapshot, _, _ = snapshot_fixture()
    scope = snapshot.approval.scope
    head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100)
    receipt = RouteTransitionReceipt(
        operation_id=str(UUID(int=23)),
        scope=scope,
        before_etag='W/"same"',
        before_generation=0,
        after_sha256=document_digest(head),
        route_version=head.approval.route_version,
        approval_id=head.approval.approval_id,
    )
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        if "transition" in str(request.url):
            if not receipt_present or len(calls) == 1:
                return httpx.Response(404)
            value = canonical_document(receipt)
            row = "transition:" + receipt.operation_id
        else:
            value = canonical_document(head)
            row = "active"
        return httpx.Response(
            200,
            json=route_entity(scope, scope.partition, row, value),
            headers={"ETag": after_etag},
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        store = TableRouteProjection(
            "https://unitstore.table.core.windows.net",
            "Routes",
            scope,
            UnitToken(),
            client,
        )
        assert store.recover(receipt) == expected
        assert len(calls) == (2 if after_etag == 'W/"same"' else 3)


@pytest.mark.parametrize(
    "table,endpoint",
    [
        ("TurnstileLedger", "https://unitstore.table.core.windows.net"),
        ("Routes", "https://unitstore-secondary.table.core.windows.net"),
        ("Routes", "http://localhost:10002"),
    ],
)
def test_projection_refuses_financial_table_replica_or_emulator(table: str, endpoint: str) -> None:
    snapshot, _, _ = snapshot_fixture()
    with pytest.raises(ValueError, match="DEDICATED_TABLE"):
        TableRouteProjection(endpoint, table, snapshot.approval.scope, UnitToken())


@pytest.mark.parametrize("existing,conflict", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("sliced", [False, True])
def test_stage_is_create_only_and_requires_exact_full_readback(
    existing: bool, conflict: bool, sliced: bool,
) -> None:
    snapshot, _, _ = snapshot_fixture()
    scope = snapshot.approval.scope
    expected = [
        route_entity(
            scope,
            content_partition(scope),
            snapshot.routes[0].sha256,
            canonical_document(snapshot.routes[0]),
        ),
        route_entity(
            scope,
            member_partition(scope, snapshot.approval.route_version),
            member_row(snapshot.routes[0].alias),
            canonical_document(snapshot.members[0]),
        ),
        route_entity(
            scope,
            catalog_partition(scope, snapshot.approval.route_version),
            member_row(snapshot.routes[0].alias),
            canonical_document(catalog_entry(snapshot.routes[0])),
        ),
    ]
    writes: list[httpx.Request] = []
    reads: dict[str, int] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            writes.append(request)
            assert request.url.path == "/Routes" and "If-Match" not in request.headers
            assert json.loads(request.content) == expected[len(writes) - 1]
            return httpx.Response(204)
        row = (
            expected[2]
            if ":catalog" in unquote(str(request.url))
            else expected[0]
            if snapshot.routes[0].sha256 in str(request.url)
            else expected[1]
        )
        row_key = str(row["PartitionKey"]) + str(row["RowKey"])
        reads[row_key] = reads.get(row_key, 0) + 1
        if not existing and reads[row_key] == 1:
            return httpx.Response(404)
        payload = {**row, "Timestamp": "2026-09-30T00:00:00Z", "odata.etag": 'W/"one"'}
        if conflict:
            payload["Document"] = "{}"
        return httpx.Response(200, json=payload, headers={"ETag": 'W/"one"'})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        store = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "Routes", scope, UnitToken(), client
        )

        def stage() -> None:
            if not sliced:
                store.stage(snapshot, 1100)
                return
            for cursor in range(3):
                before = len(writes)
                assert store.stage_slice(snapshot, 1100, cursor, maximum=1) == cursor + 1
                assert len(writes) - before <= 1
            for cursor in range(3):
                before_reads = sum(reads.values())
                assert store.verify_slice(snapshot, 1100, cursor, maximum=1) == cursor + 1
                assert sum(reads.values()) - before_reads == 1
            assert store.stage_slice(snapshot, 1100, 3, maximum=1) == 3
            with pytest.raises(ValueError):
                store.stage_slice(snapshot, 1100, -1)

        if conflict:
            with pytest.raises(RouteProjectionConflict):
                stage()
        else:
            stage()
        assert len(writes) == (0 if existing else 3)


def test_snapshot_readback_receipt_cannot_skip_slices_or_change_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, program, _ = snapshot_fixture()
    projection = TableRouteProjection(
        "https://unitstore.table.core.windows.net", "Routes", program.scope, UnitToken(),
    )
    documents = {
        (partition, row): document
        for partition, row, document, _ in projection._snapshot_entities(snapshot, 1100)
    }
    recorded: dict[tuple[str, str], str] = {}
    reads: list[tuple[str, str]] = []

    def read(partition: str, row: str, maximum: int) -> tuple[str, str] | None:
        reads.append((partition, row))
        document = recorded.get((partition, row), documents.get((partition, row)))
        return (document, 'W/"readback"') if document is not None else None

    def save(partition: str, row: str, document: str, maximum: int) -> None:
        assert (partition, row) not in recorded or recorded[(partition, row)] == document
        recorded[(partition, row)] = document

    monkeypatch.setattr(projection, "_read_document", read)
    monkeypatch.setattr(projection, "_stage_entity", save)
    with pytest.raises(ValueError, match="READBACK"):
        projection.verify_slice_recorded(snapshot, 1100, 1, maximum=1)
    assert not recorded
    for cursor in range(3):
        assert projection.verify_slice_recorded(snapshot, 1100, cursor, maximum=1) == cursor + 1
    reads.clear()
    projection.require_verified_snapshot(snapshot, 1100)
    assert len(reads) == 1
    renewed = snapshot.model_copy(update={"approval": snapshot.approval.model_copy(update={
        "approval_id": str(UUID(int=621)), "issued_epoch": 1100, "activate_before_epoch": 1700,
    })})
    projection.require_verified_snapshot(renewed, 1100)
    assert len(reads) == 2
    changed = snapshot.model_copy(update={"routes": (
        snapshot.routes[0].model_copy(update={"upstream_model": "another-model"}),
    )})
    with pytest.raises(ValueError):
        projection.require_verified_snapshot(changed, 1100)


@pytest.mark.parametrize("pool", [False, True])
@pytest.mark.parametrize("fault", [None, "foreign", "expired", "changed", "member"])
def test_candidate_projection_is_create_only_and_bound_to_exact_snapshot(
    fault: str | None, pool: bool,
) -> None:
    from turnstile_core.domain.storage_routes import RouteCandidateProbe

    snapshot, program, proof = snapshot_fixture(pool=pool)
    head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100)
    candidate = RouteCandidateProbe(
        probe_id=str(UUID(int=451)), publication_id=str(UUID(int=452)),
        request_id=str(UUID(int=453)), subscription_id="unit-publisher",
        alias=snapshot.routes[0].alias, operation="chat-completions", head=head,
        issued_epoch=1100, expires_epoch=1160, body_sha256="a" * 64,
        member_backend_id="foreign-backend" if fault == "member" else
        "unit-member-1" if pool else None,
    )
    if fault == "foreign":
        candidate = candidate.model_copy(update={"head": head.model_copy(update={
            "approval": snapshot.approval.model_copy(update={"scope": program.scope.model_copy(
                update={"api_id": "other-api"},
            )}),
        })})
    elif fault == "changed":
        candidate = candidate.model_copy(update={"alias": "unapproved-model"})
    writes: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            writes.append(request)
            assert request.url.path == "/Routes" and "If-Match" not in request.headers
            return httpx.Response(204)
        if not writes:
            return httpx.Response(404)
        return httpx.Response(200, json=route_entity(
            program.scope, program.scope.partition, "candidate:" + candidate.probe_id,
            canonical_document(candidate),
        ), headers={"ETag": 'W/"candidate"'})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        projection = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "Routes", program.scope,
            UnitToken(), client,
        )
        if fault:
            with pytest.raises(ValueError):
                projection.stage_candidate(
                    candidate, snapshot, program, proof, 1160 if fault == "expired" else 1100,
                )
            assert not writes
        else:
            projection.stage_candidate(candidate, snapshot, program, proof, 1100)
            projection.stage_candidate(candidate, snapshot, program, proof, 1100)
            assert len(writes) == 1


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("fault", [None, "lost-ack", "not-applied", "conflict", "binding", "proof",
                                    "upgraded-program"])
def test_independent_approval_renewal_is_conditional_and_preserves_old_records(
    existing: bool, fault: str | None,
) -> None:
    from turnstile_core.domain.storage_routes import RouteApprovalRenewal

    snapshot, program, old_proof = snapshot_fixture()
    route, member = snapshot.routes[0], snapshot.members[0]
    now = member.connection_approval.expires_epoch - 1
    proof = old_proof.model_copy(update={"issued_epoch": now, "expires_epoch": now + 120})
    renewal = RouteApprovalRenewal(
        scope=program.scope, content_sha256=route.sha256,
        original_approval_sha256=member.approval_sha256,
        required_program_sha256=program.program_sha256, budget_epoch=route.budget_epoch,
        approval=member.connection_approval.model_copy(update={
            "approval_id": str(UUID(int=970)), "issued_epoch": now, "expires_epoch": now + 86400,
        }),
    )
    old = renewal.model_copy(update={"approval": renewal.approval.model_copy(update={
        "approval_id": str(UUID(int=971)), "issued_epoch": now - 30,
        "expires_epoch": now + 60,
    })})
    if fault == "binding":
        renewal = renewal.model_copy(update={"budget_epoch": str(UUID(int=972))})
    elif fault == "proof":
        proof = proof.model_copy(update={"issued_epoch": now - 120, "expires_epoch": now})
    documents: dict[str, dict[str, Any]] = {}
    if existing:
        documents[old.row_key] = route_entity(
            program.scope, program.scope.partition, old.row_key, canonical_document(old),
        )
    original = canonical_document(snapshot)
    writes: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/Routes":
            entity = json.loads(request.content)
            assert entity["RowKey"] == "approval-intent:" + renewal.approval.approval_id
            assert entity["RowKey"] not in documents
            documents[entity["RowKey"]] = entity
            return httpx.Response(204)
        if request.method == "POST":
            assert request.url.path == "/$batch"
            writes.append(request)
            assert request.content.count(b"PUT ") == int(existing)
            assert request.content.count(b"POST ") == 2 - int(existing)
            assert b"RowKey='active'" not in request.content
            assert b"RowKey='writer'" not in request.content
            if fault == "conflict":
                return batch_result(412)
            if fault != "not-applied":
                for row in (renewal.row_key, "approval-receipt:" + renewal.approval.approval_id):
                    documents[row] = route_entity(
                        program.scope, program.scope.partition, row, canonical_document(renewal),
                    )
            if fault in {"lost-ack", "not-applied"}:
                raise httpx.ReadTimeout("UNIT_RENEWAL_ACK_UNKNOWN")
            return batch_result()
        address = unquote(str(request.url))
        key = address.split("RowKey='")[1].split("'")[0]
        assert key.startswith(("approval:", "approval-intent:", "approval-receipt:"))
        return httpx.Response(200, json=documents[key], headers={"ETag": 'W/"renewal"'}) \
            if key in documents else httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        projection = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "Routes", program.scope,
            UnitToken(), client,
        )
        arguments = (renewal, route, member, proof, 'W/"renewal"' if existing else None, now)
        if fault in {"binding", "proof", "conflict", "not-applied"}:
            with pytest.raises((ValueError, RouteProjectionUnknown)):
                projection.publish_approval_renewal(*arguments)
            if fault == "not-applied":
                with pytest.raises(RouteProjectionUnknown):
                    projection.publish_approval_renewal(*arguments)
        else:
            projection.publish_approval_renewal(*arguments)
            projection.publish_approval_renewal(*arguments)
            if fault == "upgraded-program":
                program = program.model_copy(update={"program_sha256": "f" * 64})
            projection.require_current_connections(snapshot, program, now + 20)
            observed_renewal = projection.read_approval_renewal(route, member, program, now + 20)
            assert observed_renewal is not None and observed_renewal[0] == renewal
        assert len(writes) == (0 if fault in {"binding", "proof"} else 1)
    assert canonical_document(snapshot) == original


@pytest.mark.parametrize("existing,fault", [
    (False, None), (True, None), (True, "conflict"), (True, "unknown"), (True, "identity"),
    (True, "not-applied"), (True, "journal"),
])
@pytest.mark.parametrize("program_upgrade", [False, True])
def test_program_proof_renewal_is_conditional_with_immutable_receipt(
    existing: bool, fault: str | None, program_upgrade: bool,
) -> None:
    from turnstile_core.domain.storage_routes import (
        FixedProgramInstallation,
        FixedProgramUpgrade,
        document_digest,
    )

    snapshot, identity, proof = snapshot_fixture()
    previous = proof.model_copy(update={"issued_epoch": 1000, "expires_epoch": 1120})
    renewed = proof.model_copy(update={
        "proof_id": str(UUID(int=461)), "issued_epoch": 1100, "expires_epoch": 1220,
    })
    installed = FixedProgramInstallation(
        program=identity, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm",
        publisher_subscription_id="unit-publisher",
        parent_policy_sha256="a" * 64, operation_policies_sha256={"models": "c" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
    )
    upgraded_identity = identity.model_copy(update={"program_sha256": "e" * 64})
    upgrade = FixedProgramUpgrade(
        previous_publication_id=str(UUID(int=460)),
        previous_installation_sha256=document_digest(installed),
        installation=installed.model_copy(update={
            "program": upgraded_identity, "parent_policy_sha256": "e" * 64,
            "model_additions_enabled": True, "legacy_program_sha256": identity.program_sha256,
        }),
    )
    if program_upgrade:
        renewed = renewed.model_copy(update={"identity": upgraded_identity})
    row = "proof:" + hashlib.sha256(json.dumps(
        [identity.api_revision, identity.scope.gateway_id, identity.instance_id],
        separators=(",", ":"),
    ).encode()).hexdigest()
    calls: list[httpx.Request] = []
    writes: list[httpx.Request] = []
    intent_record: dict[str, Any] | None = None
    if fault == "identity":
        renewed = renewed.model_copy(update={"identity": identity.model_copy(update={
            "program_sha256": "f" * 64,
        })})

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal intent_record
        calls.append(request)
        if request.method == "POST":
            if request.url.path == "/Routes":
                assert intent_record is None
                if fault == "journal":
                    raise httpx.ReadTimeout("UNIT_PROOF_INTENT_COMMIT_UNKNOWN")
                intent_record = json.loads(request.content)
                assert intent_record is not None
                assert intent_record["RowKey"] == "proof-intent:" + renewed.proof_id
                recorded = json.loads(intent_record["Document"])
                assert recorded["proof"] == renewed.model_dump(mode="json")
                assert recorded["expected_etag"] == ('W/"previous"' if existing else None)
                return httpx.Response(204)
            writes.append(request)
            assert intent_record is not None
            recorded = json.loads(intent_record["Document"])
            assert base64.b64decode(recorded["body_base64"]) == request.content
            assert request.url.path == "/$batch"
            assert request.content.count(b"PUT ") == (1 if existing else 0)
            assert request.content.count(b"POST ") == (1 if existing else 2)
            if existing:
                assert b'If-Match: W/"previous"' in request.content
            if fault in {"unknown", "not-applied"}:
                raise httpx.ReadTimeout("UNIT_PROOF_ACK_LOST")
            return batch_result(412 if fault == "conflict" else 204)
        address = unquote(str(request.url))
        if "proof-intent:" in address:
            if intent_record is None:
                return httpx.Response(404)
            return httpx.Response(200, json=intent_record, headers={"ETag": 'W/"intent"'})
        if "proof-receipt:" in address:
            if not writes or fault in {"conflict", "not-applied"}:
                return httpx.Response(404)
            key, document = "proof-receipt:" + renewed.proof_id, renewed
        else:
            if not existing and not writes:
                return httpx.Response(404)
            key, document = row, renewed if writes and fault != "not-applied" else previous
        return httpx.Response(200, json=route_entity(
            identity.scope, identity.scope.partition, key, canonical_document(document),
        ), headers={"ETag": 'W/"previous"' if not writes else 'W/"renewed"'})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        projection = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "Routes", snapshot.approval.scope,
            UnitToken(), client,
        )
        options = (renewed, identity, 'W/"previous"' if existing else None, 1100)
        def publish(selected: TableRouteProjection) -> None:
            if program_upgrade:
                selected.publish_upgraded_program_proof(
                    upgrade, installed, renewed, 'W/"previous"' if existing else None, 1100,
                )
            else:
                selected.publish_program_proof(*options)
        if fault in {"conflict", "identity"}:
            with pytest.raises(ValueError):
                publish(projection)
        elif fault in {"not-applied", "journal"}:
            with pytest.raises((httpx.ReadTimeout, RouteProjectionUnknown)):
                publish(projection)
            if fault == "not-applied":
                restarted = TableRouteProjection(
                    "https://unitstore.table.core.windows.net", "Routes", identity.scope,
                    UnitToken(), client,
                )
                with pytest.raises(RouteProjectionUnknown):
                    publish(restarted)
        else:
            publish(projection)
            observed = projection.read_program_proof(
                upgraded_identity if program_upgrade else identity,
            )
            assert observed is not None and observed[0] == renewed
            publish(projection)
        assert len(writes) == (0 if fault in {"identity", "journal"} else 1)
        if fault == "identity":
            assert not calls


@pytest.mark.parametrize("fault", [
    None, "policy", "nonce", "target", "redirect", "missing-operation", "program-digest",
    "oauth-missing-marker", "oauth-false-marker", "oauth-enabled",
    "adapter-missing-marker", "adapter-enabled",
    "pool-missing-marker", "pool-false-marker", "pool-enabled",
    "pool-unexpected-marker", "pool-duplicate-marker", "arm-indentation",
    "staged-revision", "staged-current-changed", "count-path", "retired-denied", "retired-forward",
    "native-enabled", "native-missing-marker", "native-false-marker", "native-duplicate-marker",
    "vault-enabled", "vault-empty-marker", "vault-foreign-marker", "vault-missing-marker",
    "vault-duplicate-marker",
])
@pytest.mark.parametrize("models_path", ["/models", "/v1/models"])
@pytest.mark.parametrize("messages_path", ["/messages", "/v1/messages"])
def test_live_program_verifier_requires_exact_policies_and_authenticated_identity(
    fault: str | None, models_path: str, messages_path: str,
) -> None:
    from xml.etree import ElementTree as ET

    from pydantic import SecretStr

    from turnstile_core.integrations.apim_policy_components import (
        parse_policy,
        policy_document_digest,
        serialize_policy,
    )
    from turnstile_core.services.storage_route_publisher import (
        FixedProgramInstallation,
        FixedProgramVerifier,
        FixedRoutePublisherPort,
    )

    _, identity, _ = snapshot_fixture()
    parent = '<policies><inbound><set-variable name="routeProgram" value="' + "d" * 64 + (
        '" /></inbound><backend /><outbound /><on-error /></policies>'
    )
    vault_origins = ("https://unit-key-vault.vault.azure.net",) if (
        fault is not None and fault.startswith("vault-")
    ) else ()
    policy = parse_policy(parent)
    inbound = policy.find("inbound")
    assert inbound is not None
    if fault != "vault-missing-marker":
        actual_origins = (
            () if fault == "vault-empty-marker"
            else ("https://foreign-key-vault.vault.azure.net",)
            if fault == "vault-foreign-marker" else vault_origins
        )
        for _ in range(2 if fault == "vault-duplicate-marker" else 1):
            ET.SubElement(inbound, "set-variable", {
                "name": "routeVaultOrigins",
                "value": json.dumps(actual_origins, separators=(",", ":")),
            })
    parent = serialize_policy(policy)
    if fault in {"native-enabled", "native-false-marker", "native-duplicate-marker"}:
        enabled = "false" if fault == "native-false-marker" else "true"
        marker = f'<set-variable name="routeNativeGatewayScopeEnabled" value="@({enabled})" />'
        parent = parent.replace(
            "<inbound>", "<inbound>" + marker * (2 if fault == "native-duplicate-marker" else 1),
        )
    if fault in {"oauth-false-marker", "oauth-enabled"}:
        enabled = "true" if fault == "oauth-enabled" else "false"
        parent = parent.replace(
            "<inbound>", f'<inbound><set-variable name="routeOAuthEnabled" value="@({enabled})" />',
        )
    if fault in {
        "pool-false-marker", "pool-enabled", "pool-unexpected-marker", "pool-duplicate-marker",
    }:
        enabled = "false" if fault == "pool-false-marker" else "true"
        marker = f'<set-variable name="routePoolEnabled" value="@({enabled})" />'
        parent = parent.replace(
            "<inbound>", "<inbound>" + marker * (2 if fault == "pool-duplicate-marker" else 1),
        )
    if fault == "adapter-enabled":
        parent = parent.replace(
            "<inbound>", '<inbound><set-variable name="routeRequestAdapters" '
            'value="[&quot;bedrock-anthropic-v1&quot;]" />',
        )
    operation = (
        "<policies><inbound><base /></inbound><backend /><outbound /><on-error /></policies>"
    )
    from turnstile_core.integrations.apim_storage_routes import (
        AUXILIARY_OPERATIONS,
        FIXED_OPERATIONS,
        compact_fixed_policy,
        fixed_program_digest,
        retired_operation,
    )

    contracts = {**FIXED_OPERATIONS, **AUXILIARY_OPERATIONS}
    contracts.pop("microsoft-decision", None)
    contracts["models"] = ("GET", models_path)
    contracts["anthropic-messages"] = ("POST", messages_path)
    contracts["anthropic-count-tokens"] = (
        "POST", "/wrong/count_tokens" if fault == "count-path" else messages_path + "/count_tokens",
    )
    retired = fault in {"retired-denied", "retired-forward"}
    if retired:
        contracts["retained-operation"] = ("POST", "/retired/converse")
    operations = dict.fromkeys(contracts, operation)
    if fault == "retired-denied":
        operations["retained-operation"] = compact_fixed_policy(retired_operation())
    digest = fixed_program_digest(
        parent, operations, tuple((name, *contract) for name, contract in contracts.items()),
    )
    identity = identity.model_copy(update={"program_sha256": digest})
    parent = parent.replace("d" * 64, digest)
    if fault == "missing-operation":
        contracts.pop("responses")
        operations.pop("responses")
    elif fault == "program-digest":
        identity = identity.model_copy(update={"program_sha256": "f" * 64})

    installation = FixedProgramInstallation(
        program=identity, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/models",
        publisher_subscription_id="unit-publisher",
        parent_policy_sha256=policy_document_digest(parent),
        operation_policies_sha256={
            name: policy_document_digest(policy) for name, policy in operations.items()
        },
        operation_contracts=contracts,
        retired_operations={"retained-operation": ("POST", "/retired/converse")} if retired else {},
        supported_profiles=("openai-v1",),
        trusted_vault_origins=vault_origins,
        oauth_enabled=fault in {"oauth-missing-marker", "oauth-false-marker", "oauth-enabled"},
        pool_enabled=fault in {
            "pool-missing-marker", "pool-false-marker", "pool-enabled", "pool-duplicate-marker",
        },
        native_gateway_scope=fault in {
            "native-enabled", "native-missing-marker",
            "native-false-marker", "native-duplicate-marker",
        },
        supported_adapters=("bedrock-anthropic-v1",)
        if fault in {"adapter-missing-marker", "adapter-enabled"} else (),
    )
    management = Mock()
    management.current_revision.return_value = (
        "previous" if fault == "staged-revision"
        else "unexpected" if fault == "staged-current-changed" else "1"
    )
    management.revision_api_policy.return_value = parent if fault != "policy" else operation
    management.route_program_operations.return_value = {
        name: (*contract, operations[name]) for name, contract in contracts.items()
    }
    if fault == "arm-indentation":
        management.revision_api_policy.return_value = "\ufeff" + parent.replace(
            "><", ">\r\n\t<",
        )
        management.route_program_operations.return_value = {
            name: (*contract, "\ufeff" + operation.replace("><", ">\r\n\t<"))
            for name, contract in contracts.items()
        }
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            assert request.url.path == "/models;rev=1" + models_path
            assert request.headers["Ocp-Apim-Subscription-Key"] == "unit-only"
            return httpx.Response(200, json={"data": []})
        assert request.method == "HEAD"
        assert request.headers["Ocp-Apim-Subscription-Key"] == "unit-only"
        if fault == "redirect":
            return httpx.Response(307, headers={"Location": "https://foreign.example"})
        headers = {
            "x-turnstile-route-abi": "2", "x-turnstile-route-program": identity.program_sha256,
            "x-turnstile-route-trust": identity.trust_binding_sha256,
            "x-turnstile-route-target": identity.scope.target_id,
            "x-turnstile-route-api": identity.scope.api_id,
            "x-turnstile-route-revision": identity.api_revision,
            "x-turnstile-route-gateway": identity.scope.gateway_id,
            "x-turnstile-route-instance": identity.instance_id,
            "x-turnstile-route-nonce": request.headers["x-turnstile-route-probe"],
        }
        if fault == "nonce":
            headers["x-turnstile-route-nonce"] = "0" * 32
        elif fault == "target":
            headers["x-turnstile-route-target"] = str(UUID(int=462))
        return httpx.Response(204, headers=headers)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        verifier = FixedProgramVerifier(
            installation, management, SecretStr("unit-only"), client, clock=lambda: 1100,
            expected_current_revision="previous" if fault in {
                "staged-revision", "staged-current-changed",
            } else None,
        )
        accepted = {None, "oauth-enabled", "adapter-enabled", "pool-enabled", "arm-indentation",
            "staged-revision", "retired-denied", "native-enabled", "vault-enabled"}
        if fault not in accepted:
            with pytest.raises(ValueError, match=(
                "trusted Vault origins binding changed"
                if fault is not None and fault.startswith("vault-") else None
            )):
                verifier.verify()
        else:
            verified = verifier.verify()
            assert verified.identity == identity
            assert verified.issued_epoch == 1100 and verified.expires_epoch == 1220
        assert len(requests) == (0 if fault in {
            "policy", "missing-operation", "program-digest",
            "oauth-missing-marker", "oauth-false-marker",
            "adapter-missing-marker",
            "pool-missing-marker", "pool-false-marker", "pool-unexpected-marker",
            "pool-duplicate-marker",
            "staged-current-changed", "retired-forward", "count-path",
            "native-missing-marker", "native-false-marker", "native-duplicate-marker",
            "vault-empty-marker", "vault-foreign-marker", "vault-missing-marker",
            "vault-duplicate-marker",
        } else 1)
        allowed = {"current_revision", "revision_api_policy", "route_program_operations"}
        assert all(call[0] in allowed for call in management.method_calls)
        if fault in accepted:
            port = FixedRoutePublisherPort.__new__(FixedRoutePublisherPort)
            port._verifier, port._client = verifier, client
            port._subscription_key = SecretStr("unit-only")
            assert port.read_catalog() == []


@pytest.mark.parametrize("recoverable", [False, True])
def test_route_probe_progress_recovers_completed_evidence_without_replaying(
    recoverable: bool,
) -> None:
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.services.probe_journal import PublicationProbeProgress

    stored: dict[str, Any] = {
        "version": 1, "context_sha256": "a" * 64, "steps": {"b" * 64: "started"},
    }
    before = deepcopy(stored)
    records: list[dict[str, Any]] = []

    def persist(expected: dict[str, Any] | None, checkpoint: dict[str, Any]) -> None:
        assert expected == before
        records.append(deepcopy(checkpoint))

    verify = Mock(side_effect=AssertionError("original step must not replay"))
    recover = Mock()
    if not recoverable:
        recover.side_effect = PolicyCompilationError("UNIT_ORIGINAL_EVIDENCE_UNCONFIRMED")
    progress = PublicationProbeProgress("a" * 64, stored, persist)
    if recoverable:
        progress.run_step("b" * 64, verify, recover=recover)
        assert progress.snapshot()["steps"] == {"b" * 64: "passed"}
        assert len(records) == 1
    else:
        with pytest.raises(PolicyCompilationError, match="EVIDENCE_UNCONFIRMED"):
            progress.run_step("b" * 64, verify, recover=recover)
        assert progress.snapshot() == before and not records
    verify.assert_not_called()
    recover.assert_called_once_with()


@pytest.mark.parametrize("outcome", [
    "exact", "timeout", "invalid", "already-exact", "recover-exact", "recover-missing",
    "wrong-member",
])
@pytest.mark.parametrize("pool", [False, True])
def test_route_probe_journal_persists_before_sending_and_never_replays(
    outcome: str, pool: bool,
) -> None:
    from datetime import UTC, datetime

    from turnstile_core.domain.billable_requests import (
        BillableOutcomeUncertain,
        BillableRequestAttempt,
        BillableRequestPlan,
    )
    from turnstile_core.services.probe_journal import PersistentProbeJournal

    snapshot, _, _ = snapshot_fixture(pool=pool)
    route = snapshot.routes[0]
    member_backend_id = "unit-member-1" if pool else None
    repository = Mock()
    recorded: list[BillableRequestPlan] = []
    instant = datetime.now(UTC)
    evidence = {
        "route_validated": True, "total_tokens": 12,
        "correlation_id": str(UUID(int=481)),
        **({"member_backend_id": member_backend_id} if pool else {}),
    }

    def begin(plan: Any) -> BillableRequestAttempt:
        if recorded:
            raise BillableOutcomeUncertain("UNIT_ATTEMPT_EXISTS")
        recorded.append(plan)
        assert plan.model_id == route.model_id
        assert plan.scope_type == "system" and plan.attempt_limit == 1
        assert plan.operation_key == "publication:unit-route:route:" + document_digest({
            "model": route.model_id, "alias": route.alias, "operation": "chat-completions",
            "endpoint": "https://unit.azure-api.net/chat/completions",
            **({"member_backend_id": member_backend_id} if pool else {}),
        })
        return BillableRequestAttempt(
            id=UUID(int=480), **plan.model_dump(exclude={"reuse_requires", "admission_authority",
                                                       "attempt_limit"}),
            state="exact" if outcome in {"already-exact", "recover-exact"} else "started",
            actual_tokens=12 if outcome in {"already-exact", "recover-exact"} else None,
            attempt_index=1, created_at=instant, updated_at=instant,
            evidence=evidence if outcome in {"already-exact", "recover-exact"} else {},
        )

    repository.begin_billable_request.side_effect = begin
    repository.recover_billable_request.side_effect = (
        BillableOutcomeUncertain("UNIT_EVIDENCE_MISSING") if outcome == "recover-missing" else begin
    )
    sent = []

    def send(request_id: str) -> dict[str, Any]:
        assert len(recorded) == 1 and request_id == str(UUID(int=480))
        sent.append(request_id)
        if outcome == "timeout":
            raise httpx.ReadTimeout("UNIT_ROUTE_PROBE_UNKNOWN")
        if outcome == "wrong-member":
            return {**evidence, "member_backend_id": "foreign-member"}
        return evidence if outcome == "exact" else {**evidence, "route_validated": False}

    journal = PersistentProbeJournal(
        repository, "publication:unit-route", UUID(int=482),
        plan_context={"route_task": "c" * 64},
    )
    payload = {"model": route.alias, "max_completion_tokens": 32, "stream": False,
               "messages": [{"role": "user", "content": "Say OK."}]}
    operation: Literal["chat-completions"] = "chat-completions"
    arguments = (route, operation, "https://unit.azure-api.net/chat/completions",
                 payload, send)
    if outcome in {"recover-exact", "recover-missing"}:
        if outcome == "recover-missing":
            with pytest.raises(ValueError, match="unconfirmed"):
                journal.run_route(
                    *arguments, recovery_only=True, member_backend_id=member_backend_id,
                )
        else:
            assert journal.run_route(
                *arguments, recovery_only=True, member_backend_id=member_backend_id,
            ) == evidence
        assert not sent
        repository.begin_billable_request.assert_not_called()
        repository.finish_billable_request.assert_not_called()
        return
    if outcome in {"timeout", "invalid", "wrong-member"}:
        with pytest.raises((httpx.ReadTimeout, ValueError)):
            journal.run_route(*arguments, member_backend_id=member_backend_id)
    else:
        assert journal.run_route(*arguments, member_backend_id=member_backend_id) == evidence
    assert len(sent) == (0 if outcome == "already-exact" else 1)
    assert repository.finish_billable_request.call_count == len(sent)
    with pytest.raises(ValueError):
        journal.run_route(*arguments, member_backend_id=member_backend_id)
    assert len(sent) == (0 if outcome == "already-exact" else 1)


@pytest.mark.parametrize("supported", [False, True])
@pytest.mark.parametrize("fault", [None, "counter", "identity", "response"])
@pytest.mark.parametrize("pool", [False, True])
def test_fixed_route_public_count_probe_validates_only_changed_route(
    supported: bool, fault: str | None, pool: bool,
) -> None:
    from pydantic import SecretStr

    from turnstile_core.domain.control_plane import COUNT_TOKENS_REJECTIONS
    from turnstile_core.domain.storage_routes import RouteConfigurationPlan, RoutePublicationTask
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.services.route_publication import configuration_probe_steps
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherPort

    snapshot, program, proof = snapshot_fixture(pool=pool)
    route = snapshot.routes[0].model_copy(update={
        "profile": "anthropic-messages", "operations": ("anthropic-messages",),
        "connection": snapshot.routes[0].connection.model_copy(update={
            "public_count_path": "/anthropic/v1/messages/count_tokens" if supported else None,
        }),
        "budget": snapshot.routes[0].budget.model_copy(update={
            "operations": ("anthropic-messages",), "output_field": "max_tokens",
        }),
    })
    approval = snapshot.members[0].connection_approval.model_copy(update={
        "connection": route.connection,
    })
    approvals = {route.connection.revision_id: approval}
    _, manifest, dependencies = configuration_manifests(
        program.scope, snapshot.approval.route_version, [route], approvals, 1100,
    )
    snapshot = RouteSnapshot.prepare(snapshot.approval.model_copy(update={
        "required_profiles": ("anthropic-messages",), "manifest_sha256": manifest,
        "dependency_manifest_sha256": dependencies, "catalog_sha256": catalog_digest([route]),
    }), [route], approvals, 1100)
    proof = proof.model_copy(update={"supported_profiles": ("anthropic-messages",)})
    task = RoutePublicationTask(
        publication_id=str(UUID(int=500)), base_publication_id=None,
        desired_spec_sha256="a" * 64, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        previous_routes=(), previous_head=None,
        plan=RouteConfigurationPlan.prepare(
            [], snapshot, program, proof, snapshot.approval.budget_epoch, 1100,
        ),
    )
    projection, verifier, journal = Mock(), Mock(), Mock()
    steps = configuration_probe_steps(task)
    expected = [
        (route.alias, "inference"), (route.alias, "count"),
    ]
    if pool:
        expected.append((route.alias, "inference"))
        if supported:
            expected.append((route.alias, "count"))
    assert [(step.alias, step.kind) for step in steps] == expected
    assert len({step.key for step in steps}) == len(expected)
    if pool:
        assert {step.member_backend_id for step in steps} == {"unit-member-0", "unit-member-1"}
        assert all(step.operation == "anthropic-messages" for step in steps
                   if step.kind == "inference")
    unchanged = task.model_copy(update={"plan": task.plan.model_copy(update={
        "changed_aliases": (),
    })})
    assert configuration_probe_steps(unchanged) == ()
    verifier.installation.program = program
    verifier.installation.publisher_subscription_id = "unit-publisher"
    verifier.installation.gateway_endpoint = "https://unit.azure-api.net/models"
    verifier.installation.operation_contracts = {
        "anthropic-count-tokens": ("POST", "/messages/count_tokens"),
    }
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST" and request.url.path.endswith("/messages/count_tokens")
        candidate = projection.stage_candidate.call_args.args[0]
        assert candidate.operation == "anthropic-count-tokens"
        assert json.loads(request.content) == {"model": route.alias, "messages": [{
            "role": "user", "content": "Say OK.",
        }]}
        headers = {
            "x-turnstile-route-version": candidate.head.approval.route_version,
            "x-turnstile-route-candidate": candidate.probe_id,
            "x-turnstile-route-request": candidate.request_id,
            **({"x-turnstile-route-member": "unit-member-0"} if pool else {}),
        }
        if fault == "identity":
            headers["x-turnstile-route-version"] = str(UUID(int=599))
        payload: dict[str, Any] = (
            {"input_tokens": True if fault == "counter" else 12}
            if supported else COUNT_TOKENS_REJECTIONS["unavailable"].payload()
        )
        if fault == "response":
            payload = {"error": "upstream unavailable"}
        return httpx.Response(200 if supported else 404, json=payload, headers=headers)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        port = FixedRoutePublisherPort(
            projection, verifier, SecretStr("unit-probe"), client, active_fence=Mock(),
            verify_connections=Mock(), prepare_availability=Mock(return_value=True),
            clock=lambda: 1100,
        )
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(port, "check", Mock(return_value=proof))
            if fault in {"identity", "response"} or fault == "counter" and supported:
                with pytest.raises(PolicyCompilationError):
                    port.probe_count(task, route.alias, journal,
                                     member_backend_id="unit-member-0" if pool else None)
            else:
                port.probe_count(task, route.alias, journal,
                                 member_backend_id="unit-member-0" if pool else None)
    assert len(calls) == 1
    journal.run_route.assert_not_called()
    journal.record_request.assert_called_once()


@pytest.mark.parametrize("fault", [None, "image", "usage", "oversize"])
def test_fixed_image_probe_requires_decoded_image_and_exact_usage(fault: str | None) -> None:
    from pydantic import SecretStr

    from tests.backend.model_platform.test_image_gateway import unit_image_bytes, usage_payload
    from turnstile_core.domain.image_profiles import ImageGenerationLimits, create_image_profile
    from turnstile_core.domain.storage_routes import RouteConfigurationPlan, RoutePublicationTask
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherPort

    snapshot, program, proof = snapshot_fixture()
    original = snapshot.routes[0]
    image = create_image_profile(ImageGenerationLimits(
        max_request_bytes=24576, max_response_bytes=100 if fault == "oversize" else 16777216,
        output_reservation_tokens=8192, timeout_seconds=180,
    ), original.upstream_model)
    route = original.model_copy(update={
        "profile": "openai-images", "operations": ("images-generations",),
        "streaming_mode": "buffered", "image_profile": image,
        "budget": original.budget.model_copy(update={
            "operations": ("images-generations",), "output_field": "none", "output_estimate": 8192,
        }),
    })
    approvals = {original.connection.revision_id: snapshot.members[0].connection_approval}
    _, manifest, dependencies = configuration_manifests(
        program.scope, snapshot.approval.route_version, [route], approvals, 1100,
    )
    snapshot = RouteSnapshot.prepare(snapshot.approval.model_copy(update={
        "required_profiles": ("openai-images",), "manifest_sha256": manifest,
        "dependency_manifest_sha256": dependencies, "catalog_sha256": catalog_digest([route]),
    }), [route], approvals, 1100)
    proof = proof.model_copy(update={"supported_profiles": ("openai-images",)})
    task = RoutePublicationTask(
        publication_id=str(UUID(int=690)), base_publication_id=None,
        desired_spec_sha256="a" * 64, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        previous_routes=(), previous_head=None,
        plan=RouteConfigurationPlan.prepare(
            [], snapshot, program, proof, snapshot.approval.budget_epoch, 1100,
        ),
    )
    projection, verifier, journal = Mock(), Mock(), Mock()
    verifier.installation.publisher_subscription_id = "unit-publisher"
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        assert set(body) == {"model", "prompt", "n", "stream"}
        assert body["n"] == 1 and body["stream"] is False
        candidate = projection.stage_candidate.call_args.args[0]
        payload = {} if fault == "usage" else usage_payload()
        payload["data"] = [{"b64_json": "invalid" if fault == "image" else (
            base64.b64encode(unit_image_bytes()).decode()
        )}]
        return httpx.Response(200, json=payload, headers={
            "x-correlation-id": str(UUID(int=691)),
            "x-turnstile-route-version": candidate.head.approval.route_version,
            "x-turnstile-route-candidate": candidate.probe_id,
            "x-turnstile-route-request": candidate.request_id,
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        port = FixedRoutePublisherPort(
            projection, verifier, SecretStr("unit-probe"), client, active_fence=Mock(),
            verify_connections=Mock(), prepare_availability=Mock(), clock=lambda: 1100,
        )
        operation: Literal["images-generations"] = "images-generations"
        body = port._probe_body(route, operation)
        arguments = (task, route, operation, proof,
                     "https://unit.azure-api.net/images/generations", body, journal,
                     str(UUID(int=692)))
        if fault:
            with pytest.raises(PolicyCompilationError) as caught:
                port._send_probe(*arguments)
            if fault == "image":
                outcome = caught.value.billable_outcome
                assert outcome is not None and outcome.actual_tokens == 222
                assert outcome.evidence["route_validated"] is False
        else:
            evidence = port._send_probe(*arguments)
            assert evidence["route_validated"] is True and evidence["total_tokens"] == 222
            assert evidence["image_validated"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("fault", [None, "absent", "total", "boolean"])
@pytest.mark.parametrize("prompt_fields", [False, True])
def test_fixed_decision_probe_uses_native_body_and_exact_usage(
    fault: str | None, prompt_fields: bool,
) -> None:
    from turnstile_core.domain.runtime_models import DecisionRequest
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherPort

    snapshot, _, _ = snapshot_fixture()
    route = snapshot.routes[0]
    route = route.model_copy(update={"budget": route.budget.model_copy(update={
        "output_field": "none", "protocols": {},
    })})
    body = FixedRoutePublisherPort._probe_body(route, "microsoft-decision")
    parsed = DecisionRequest.model_validate(body)
    assert parsed.model == route.alias and parsed.state == "OK"
    assert parsed.questions["ok"].type == "noul"
    assert set(body) == {"model", "state", "questions"}
    raw: dict[str, Any] = {
        "prompt_tokens" if prompt_fields else "input_tokens": 12,
        "completion_tokens" if prompt_fields else "output_tokens": 0,
        "total_tokens": 12,
    }
    if fault == "total":
        raw["total_tokens"] = 13
    elif fault == "boolean":
        raw["total_tokens"] = True
    payload = {"answers": {"ok": 0.9}, "usage": None if fault == "absent" else raw}
    if fault:
        with pytest.raises(PolicyCompilationError):
            FixedRoutePublisherPort._usage(payload, "microsoft-decision")
    else:
        usage = FixedRoutePublisherPort._usage(payload, "microsoft-decision")
        assert usage.input_tokens == 12 and usage.output_tokens == 0
        assert usage.cached_tokens == usage.cache_write_tokens == 0
        assert usage.estimated is False


@pytest.mark.parametrize("validated", [False, True])
def test_fixed_image_probe_recovery_requires_image_validation(validated: bool) -> None:
    from datetime import UTC, datetime

    from turnstile_core.domain.billable_requests import BillableRequestAttempt, BillableRequestPlan
    from turnstile_core.domain.image_profiles import ImageGenerationLimits, create_image_profile
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.persistence.in_memory import InMemoryRepository
    from turnstile_core.services.probe_journal import PersistentProbeJournal

    snapshot, _, _ = snapshot_fixture()
    original = snapshot.routes[0]
    image = create_image_profile(ImageGenerationLimits(
        max_request_bytes=24576, max_response_bytes=16777216,
        output_reservation_tokens=8192, timeout_seconds=180,
    ), original.upstream_model)
    route = original.model_copy(update={
        "profile": "openai-images", "operations": ("images-generations",), "image_profile": image,
        "budget": original.budget.model_copy(update={
            "operations": ("images-generations",), "output_field": "none", "output_estimate": 8192,
        }),
    })
    repository = InMemoryRepository()
    evidence = {"route_validated": True, "total_tokens": 222, "image_validated": validated}
    moment = datetime.now(UTC)

    def recover(plan: BillableRequestPlan) -> BillableRequestAttempt:
        assert plan.reuse_requires == {"route_validated": True, "image_validated": True}
        return BillableRequestAttempt(
            **plan.model_dump(exclude={"attempt_limit", "reuse_requires", "admission_authority"}),
            id=UUID(int=696), attempt_index=1, state="exact", actual_tokens=222,
            created_at=moment, updated_at=moment, evidence=evidence,
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(repository, "recover_billable_request", recover)
        journal = PersistentProbeJournal(repository, "publication:image", UUID(int=695))
        send = Mock(side_effect=AssertionError("image recovery must not dispatch"))
        if validated:
            result = journal.run_route(
                route, "images-generations", "https://unit.example/images/generations",
                {"model": route.alias, "prompt": "unit"}, send, recovery_only=True,
            )
            assert result == evidence
        else:
            with pytest.raises(PolicyCompilationError):
                journal.run_route(
                    route, "images-generations", "https://unit.example/images/generations",
                    {"model": route.alias, "prompt": "unit"}, send, recovery_only=True,
                )
        send.assert_not_called()


@pytest.mark.parametrize(
    "fault", [None, "missing", "auth", "auth401", "paging", "invalid", "target"],
)
@pytest.mark.parametrize("service_origin", [False, True])
@pytest.mark.parametrize("managed_identity", [False, True])
@pytest.mark.parametrize("version_bound", [False, True])
def test_foundry_deployment_validation_reads_metadata_without_inference(
    fault: str | None, service_origin: bool, managed_identity: bool, version_bound: bool,
) -> None:
    from turnstile_core.domain.control_plane import AuthStrategy
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherFactory

    repository = Mock()
    repository.gateway_publication_credential.return_value = None
    factory = FixedRoutePublisherFactory(
        repository, Mock(), active_fence=Mock(), availability_projector=Mock(),
    )
    tokens = Mock()
    tokens.token.return_value = "unit-only-managed-identity-token"
    factory._tokens = tokens
    factory._cipher = Mock()
    secret_id = "https://unit-vault.vault.azure.net/secrets/unit-key/" + "a" * 32
    installation = Mock(
        credential_vault_origin="https://unit-vault.vault.azure.net",
        trusted_vault_origins=("https://unit-vault.vault.azure.net",),
    )
    binding = Mock(
        runtime_config={"project_endpoint": "https://unit.services.ai.azure.com/api/projects/unit"},
        backend_url="https://foreign.openai.azure.com" if fault == "target"
        else "https://unit.services.ai.azure.com" if service_origin
        else "https://unit.openai.azure.com",
        model=Mock(upstream_model_id="unit-deployment"),
        auth_strategy=AuthStrategy.MANAGED_IDENTITY if managed_identity
        else AuthStrategy.NAMED_VALUE_API_KEY,
        named_value_name="unit-key",
        key_vault_secret_id=secret_id if version_bound and not managed_identity else None,
    )
    publication = Mock(desired_spec=Mock(bindings=[binding]))
    publication.resource_manifest = {
        "route_credentials": {"version": 1, "bindings": {"b" * 64: secret_id}},
    }
    management = Mock()
    management.route_credential_value.side_effect = AssertionError("Business Named Value read")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        if request.url.host == "unit-vault.vault.azure.net":
            assert not managed_identity
            assert request.url.path == "/secrets/unit-key/" + "a" * 32
            return httpx.Response(200, json={
                "id": secret_id, "value": "unit-only-key", "attributes": {"enabled": True},
            })
        assert request.url.path == "/api/projects/unit/deployments"
        if managed_identity:
            assert request.headers["Authorization"] == "Bearer unit-only-managed-identity-token"
            assert "api-key" not in request.headers
        if not managed_identity:
            assert request.headers["api-key"] == "unit-only-key"
            assert "Authorization" not in request.headers
        if fault in {"auth", "auth401"}:
            return httpx.Response(
                401 if fault == "auth401" else 403, json={"error": "private-upstream-error"},
            )
        payload: dict[str, Any] = {
            "value": [{"name": "different" if fault == "missing" else "unit-deployment"}],
        }
        if fault == "paging":
            payload["nextLink"] = "https://foreign.example/private"
        if fault == "invalid":
            payload = {"value": "private-invalid-data"}
        return httpx.Response(200, json=payload)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "turnstile_core.services.route_publication.route_credential_source",
            lambda _binding: "b" * 64,
        )
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            if fault is None:
                factory._validate_deployment(publication, management, client, installation)
            if fault is not None:
                with pytest.raises(PolicyCompilationError) as failed:
                    factory._validate_deployment(publication, management, client, installation)
                assert "private" not in str(failed.value)
                if fault == "missing":
                    assert "deployment_not_found" in str(failed.value)
                    assert "unit-deployment" in str(failed.value)
                if fault in {"auth", "auth401"}:
                    assert "deployment_metadata_unavailable" in str(failed.value)

    expected_requests = 0 if fault == "target" else 1 if managed_identity else 2
    assert len(requests) == expected_requests
    management.route_credential_value.assert_not_called()
    if managed_identity and fault != "target":
        tokens.token.assert_called_once_with("https://ai.azure.com")
        repository.gateway_publication_credential.assert_not_called()
    if not managed_identity and fault != "target":
        tokens.token.assert_called_once_with("https://vault.azure.net")
    if fault == "target":
        tokens.token.assert_not_called()


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("pool", [False, True])
def test_fixed_route_probe_recovery_only_reads_original_protocol_evidence(
    complete: bool, pool: bool,
) -> None:
    from pydantic import SecretStr

    from turnstile_core.domain.storage_routes import RouteConfigurationPlan, RoutePublicationTask
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherPort

    snapshot, program, proof = snapshot_fixture(pool=pool)
    task = RoutePublicationTask(
        publication_id=str(UUID(int=500)), base_publication_id=None,
        desired_spec_sha256="a" * 64, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        previous_routes=(), previous_head=None,
        plan=RouteConfigurationPlan.prepare(
            [], snapshot, program, proof, snapshot.approval.budget_epoch, 1100,
        ),
    )
    projection, verifier, client, availability, journal = Mock(), Mock(), Mock(), Mock(), Mock()
    verifier.installation.gateway_endpoint = "https://unit.azure-api.net/models"
    verifier.installation.program = program
    verifier.installation.operation_contracts = {"chat-completions": ("POST", "/chat/completions")}
    if not complete:
        journal.run_route.side_effect = PolicyCompilationError("UNIT_ORIGINAL_PROTOCOL_UNKNOWN")
    port = FixedRoutePublisherPort(
        projection, verifier, SecretStr("unit-probe"), client, active_fence=Mock(),
        verify_connections=Mock(), prepare_availability=availability,
    )
    options: dict[str, Any] = {
        "member_backend_id": "unit-member-1", "probe_operation": "chat-completions",
    } if pool else {}
    if complete:
        port.recover_changed(task, snapshot.routes[0].alias, journal, **options)
    else:
        with pytest.raises(PolicyCompilationError, match="PROTOCOL_UNKNOWN"):
            port.recover_changed(task, snapshot.routes[0].alias, journal, **options)
    assert journal.run_route.call_count == 1
    assert journal.run_route.call_args.kwargs["recovery_only"] is True
    assert journal.run_route.call_args.kwargs.get("member_backend_id") == options.get(
        "member_backend_id",
    )
    assert not projection.method_calls and not client.method_calls
    availability.assert_not_called()


@pytest.mark.parametrize("fault", [
    None, "alias", "version", "usage", "redirect", "timeout", "member", "selection", "operation",
])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("pool", [False, True])
def test_candidate_http_probe_requires_exact_route_and_usage(
    fault: str | None, enabled: bool, pool: bool,
) -> None:
    from pydantic import SecretStr

    from turnstile_core.domain.storage_routes import RouteConfigurationPlan, RoutePublicationTask
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherPort

    snapshot, program, proof = snapshot_fixture(pool=pool)
    task = RoutePublicationTask(
        publication_id=str(UUID(int=500)), base_publication_id=None,
        desired_spec_sha256="a" * 64, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        previous_routes=(), previous_head=None,
        plan=RouteConfigurationPlan.prepare(
            [], snapshot, program, proof, snapshot.approval.budget_epoch, 1100,
        ),
    )
    projection = Mock()
    projection.read_program_proof.return_value = (proof, 'W/"proof"')
    verifier = Mock()
    verifier.installation.program = program
    verifier.installation.target_identity_sha256 = task.target_identity_sha256
    verifier.installation.storage_endpoint = task.storage_endpoint
    verifier.installation.storage_table = task.storage_table
    verifier.installation.gateway_endpoint = "https://unit.azure-api.net/llm"
    verifier.installation.publisher_subscription_id = "unit-publisher"
    verifier.installation.pool_enabled = pool
    verifier.installation.operation_contracts = {"chat-completions": ("POST", "/chat/completions")}
    verifier.verify.return_value = proof
    calls: list[httpx.Request] = []
    journal = Mock()
    availability = Mock(return_value=enabled)
    request_id = str(UUID(int=501))
    journal.run_route.side_effect = (
        lambda route, operation, endpoint, body, send, **options: send(request_id)
    )
    options: dict[str, Any] = {
        "member_backend_id": "unit-member-1", "probe_operation": "chat-completions",
    } if pool else {}
    if fault == "selection":
        options["member_backend_id"] = "foreign-member"
    elif fault == "operation":
        options["probe_operation"] = "images-generations"

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        if enabled:
            assert request.headers["x-request-id"] == request_id
        assert request.headers["Ocp-Apim-Subscription-Key"] == "unit-only"
        projection.stage_candidate.assert_called_once()
        candidate = projection.stage_candidate.call_args.args[0]
        assert request.headers["x-turnstile-route-candidate"] == candidate.probe_id
        assert candidate.request_id == request.headers["x-request-id"]
        assert candidate.allow_dispatch is enabled
        assert candidate.publication_id == task.publication_id
        assert candidate.member_backend_id == options.get("member_backend_id")
        assert "x-turnstile-pool-member" not in request.headers
        if fault == "redirect":
            return httpx.Response(307, headers={"Location": "https://foreign.example"})
        if fault == "timeout":
            raise httpx.ReadTimeout("UNIT_CANDIDATE_TIMEOUT")
        if not enabled:
            return httpx.Response(403, json={"error": {"code": "model_disabled"}})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": True if fault == "usage" else 5,
                      "total_tokens": 12},
        }, headers={
            "Content-Type": "application/json", "x-correlation-id": str(UUID(int=502)),
            "x-turnstile-route-version": "foreign" if fault == "version"
            else snapshot.approval.route_version,
            "x-turnstile-route-candidate": candidate.probe_id,
            "x-turnstile-route-request": request_id,
            "x-turnstile-route-member": "foreign-member" if fault == "member" else
            options.get("member_backend_id", ""),
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        port = FixedRoutePublisherPort(
            projection, verifier, SecretStr("unit-only"), client,
            active_fence=lambda: {"protocol_version": 2, "mode": "active",
                                  "gateway_profile_id": program.scope.target_id,
                                  "epoch": snapshot.approval.budget_epoch},
            verify_connections=Mock(), clock=lambda: 1100,
            prepare_availability=availability,
        )
        invalid = fault in {"alias", "selection", "operation"}
        rejected = fault and (enabled or invalid or fault in {"redirect", "timeout"})
        if rejected and (fault != "member" or pool):
            with pytest.raises((ValueError, httpx.ReadTimeout)):
                port.probe_changed(task, "unapproved" if fault == "alias"
                                   else snapshot.routes[0].alias, journal, **options)
        else:
            port.probe_changed(task, snapshot.routes[0].alias, journal, **options)
        assert len(calls) == (0 if invalid else 1)
        assert journal.record_request.call_count == len(calls)
        assert journal.run_route.call_count == (len(calls) if enabled else 0)
        assert availability.call_count == (0 if invalid else 1)


@pytest.mark.parametrize("ceiling", [32, 512, 4096])
def test_route_probe_output_bound_allows_reasoning_within_approved_ceiling(ceiling: int) -> None:
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherPort

    snapshot, _, _ = snapshot_fixture()
    route = snapshot.routes[0]
    route = route.model_copy(update={"budget": route.budget.model_copy(update={
        "output_ceiling": ceiling, "protocols": {}, "output_field": "max_output_tokens",
    })})
    body = FixedRoutePublisherPort._probe_body(route, "responses")
    assert body["max_output_tokens"] == min(1024, ceiling)
    assert body["input"] == "Say OK." and body["stream"] is False


@pytest.mark.parametrize("status", ["completed", "incomplete", "failed"])
def test_uncompleted_responses_probe_retains_exact_usage_without_route_acceptance(
    status: str,
) -> None:
    from pydantic import SecretStr

    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherPort

    snapshot, _, proof = snapshot_fixture()
    route = snapshot.routes[0]
    request_id, correlation = str(UUID(int=594)), str(UUID(int=595))
    task, projection, journal = Mock(), Mock(), Mock()
    task.publication_id = str(UUID(int=596))
    task.context_sha256 = "a" * 64
    task.plan.snapshot = snapshot
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        candidate = projection.stage_candidate.call_args.args[0]
        return httpx.Response(200, json={
            "status": status, "output": [],
            "usage": {"input_tokens": 9, "output_tokens": 0, "total_tokens": 9},
        }, headers={
            "x-correlation-id": correlation,
            "x-turnstile-route-version": candidate.head.approval.route_version,
            "x-turnstile-route-candidate": candidate.probe_id,
            "x-turnstile-route-request": request_id,
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        port = FixedRoutePublisherPort.__new__(FixedRoutePublisherPort)
        port._client, port.projection = client, projection
        port._clock = lambda: 1100
        port._subscription_key = SecretStr("unit-only")
        port._verifier = Mock()
        port._verifier.installation.publisher_subscription_id = "unit-publisher"
        def send() -> dict[str, Any]:
            return port._send_probe(
                task, route, "responses", proof, "https://unit.invalid/responses",
                {"model": route.alias, "input": "Say OK."}, journal, request_id,
            )

        if status == "completed":
            assert send()["route_validated"] is True
        else:
            with pytest.raises(PolicyCompilationError) as failure:
                send()
            outcome = failure.value.billable_outcome
            assert outcome is not None and outcome.actual_tokens == 9
            assert outcome.correlation_id == correlation
            assert outcome.evidence["route_validated"] is False
        assert len(calls) == journal.record_request.call_count == 1


@pytest.mark.parametrize("fault", [None, "owner", "extra", "version", "uuid"])
def test_program_migration_real_http_boundary_uses_strict_json_contract(
    monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    from typing import get_args

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    import backend.http.model_platform as routes
    from backend.http.dependencies import Repository
    from backend.http.publication_auth import PublicationOwner
    from backend.http.service_dependencies import ControlPlaneService
    from tests.backend.model_platform.control_plane_support import (
        GatewayControlPlaneService,
        bedrock_publication,
    )
    from turnstile_core.domain.control_plane import PublicationStatus
    from turnstile_core.persistence.in_memory import InMemoryRepository
    from turnstile_core.services.storage_route_publisher import (
        FixedProgramInstallation,
        FixedProgramMigration,
    )

    _, identity, _ = snapshot_fixture()
    target = UUID(identity.scope.target_id)
    publication = GatewayControlPlaneService(InMemoryRepository()).publish(
        bedrock_publication(), "unit-owner",
    ).model_copy(update={"gateway_profile_id": target, "status": PublicationStatus.QUEUED})
    migration = FixedProgramMigration(
        request_id=publication.id, previous_publication_id=UUID(int=880),
        previous_revision="previous", previous_spec_sha256="b" * 64,
        previous_policy_sha256="c" * 64,
        installation=FixedProgramInstallation(
            program=identity, target_identity_sha256="d" * 64,
            storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
            gateway_endpoint="https://unit.azure-api.net/llm", publisher_subscription_id="unit",
            parent_policy_sha256="a" * 64,
            operation_policies_sha256={"chat-completions": "b" * 64},
            operation_contracts={"chat-completions": ("POST", "/chat/completions")},
            supported_profiles=("openai-v1",),
        ),
    )
    service, repository = Mock(), Mock()
    service.queue_fixed_program_migration.return_value = publication
    app = FastAPI()
    app.include_router(routes.publication_router)
    app.dependency_overrides[get_args(Repository)[-1].dependency] = lambda: repository
    app.dependency_overrides[get_args(ControlPlaneService)[-1].dependency] = lambda: service
    def owner() -> str:
        if fault == "owner":
            raise HTTPException(status_code=403)
        return "unit-owner"
    app.dependency_overrides[get_args(PublicationOwner)[-1].dependency] = owner
    configuration = Mock()
    configuration.resolve.return_value.route_target_identity_sha256 = "d" * 64
    monkeypatch.setattr(routes, "apim_target_client_config_resolver", lambda *args: configuration)
    body = migration.model_dump(mode="json")
    if fault == "extra":
        body["foreign"] = True
    elif fault == "version":
        body["installation"]["version"] = "1"
    elif fault == "uuid":
        body["request_id"] = "not-a-uuid"
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/model-management/gateways/" + str(target) + "/program-migrations", json=body,
        )
    assert response.status_code == (403 if fault == "owner" else 422 if fault else 202)
    if fault:
        service.queue_fixed_program_migration.assert_not_called()
    else:
        assert service.queue_fixed_program_migration.call_args.args == (
            target, migration.model_dump(mode="json"), "unit-owner",
        )


@pytest.mark.parametrize("coordinated", [False, True])
@pytest.mark.parametrize("fault", [
    None, "owner", "extra", "version", "target", "missing-etag", "invalid-etag", "native",
])
def test_program_upgrade_http_is_strict_owner_only_and_conditional(
    monkeypatch: pytest.MonkeyPatch, fault: str | None, coordinated: bool,
) -> None:
    from typing import get_args

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    import backend.http.model_platform as routes
    from backend.http.dependencies import Repository
    from backend.http.publication_auth import PublicationOwner
    from turnstile_core.domain.storage_routes import FixedProgramInstallation, FixedProgramUpgrade

    _, program, _ = snapshot_fixture()
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256="a" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm", publisher_subscription_id="unit",
        parent_policy_sha256="b" * 64, operation_policies_sha256={"models": "c" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
        model_additions_enabled=True, legacy_program_sha256="e" * 64,
    )
    upgrade = FixedProgramUpgrade(
        previous_publication_id=str(UUID(int=991)),
        previous_installation_sha256="f" * 64, installation=installation,
    )
    repository, configuration, register = Mock(), Mock(), Mock()
    if fault == "native":
        register.side_effect = ValueError("UNIT_NATIVE_VERIFICATION_FAILED")
    app = FastAPI()
    app.include_router(routes.publication_router)
    app.dependency_overrides[get_args(Repository)[-1].dependency] = lambda: repository
    def owner() -> str:
        if fault == "owner":
            raise HTTPException(status_code=403)
        return "unit-owner"
    app.dependency_overrides[get_args(PublicationOwner)[-1].dependency] = owner
    monkeypatch.setattr(routes, "apim_target_client_config_resolver", lambda *args: configuration)
    monkeypatch.setattr(routes, "register_verified_program_upgrade", register)
    body = upgrade.model_dump(mode="json")
    if fault == "extra":
        body["unapproved"] = True
    elif fault == "version":
        body["version"] = "1"
    elif fault == "target":
        body["installation"]["program"]["scope"]["target_id"] = str(UUID(int=992))
    if coordinated:
        body = {"upgrade": body, "parent_policy": "<policies />",
                "expected_parent_etag": '"original"'}
    headers = {"If-Match": "a" * 64}
    if fault == "missing-etag":
        headers = {}
    elif fault == "invalid-etag":
        headers = {"If-Match": "*"}
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/model-management/gateways/" + program.scope.target_id + "/program-upgrades"
            + ("/cutover" if coordinated else ""),
            json=body, headers=headers,
        )
    expected = 403 if fault == "owner" else 409 if fault in {"target", "native"} else (
        422 if fault else 200
    )
    assert response.status_code == expected
    if fault not in {None, "native"}:
        register.assert_not_called()
    elif fault is None:
        if coordinated:
            register.assert_called_once_with(
                repository, configuration, upgrade, "a" * 64,
                parent_policy="<policies />", expected_parent_etag='"original"',
            )
        else:
            register.assert_called_once_with(repository, configuration, upgrade, "a" * 64)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == upgrade.model_dump(mode="json")
    repository.create_gateway_publication.assert_not_called()


@pytest.mark.parametrize("fault", [None, "ordinary-error", "unexpected-success", "no-ack"])
@pytest.mark.parametrize("enabled", [False, True])
def test_known_unavailable_migration_proves_no_dispatch_without_billable_probe(
    fault: str | None, enabled: bool,
) -> None:
    from pydantic import SecretStr

    from turnstile_core.domain.storage_routes import RouteConfigurationPlan, RoutePublicationTask
    from turnstile_core.services.route_publication import configuration_probe_steps
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherPort

    snapshot, program, proof = snapshot_fixture()
    alias = snapshot.routes[0].alias
    task = RoutePublicationTask(
        publication_id=str(UUID(int=590)), base_publication_id=str(UUID(int=589)),
        desired_spec_sha256="a" * 64, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        previous_routes=(), previous_head=None, program_migration_sha256="c" * 64,
        known_unavailable_bindings={alias: "d" * 64},
        plan=RouteConfigurationPlan.prepare(
            [], snapshot, program, proof, snapshot.approval.budget_epoch, 1100,
        ),
    )
    assert all(step.kind == "non-dispatch" for step in configuration_probe_steps(task))
    if fault == "no-ack":
        task = task.model_copy(update={"known_unavailable_bindings": {}})
    projection, verifier, journal = Mock(), Mock(), Mock()
    projection.read_program_proof.return_value = (proof, 'W/"proof"')
    verifier.installation.program = program
    verifier.installation.target_identity_sha256 = task.target_identity_sha256
    verifier.installation.storage_endpoint = task.storage_endpoint
    verifier.installation.storage_table = task.storage_table
    verifier.installation.gateway_endpoint = "https://unit.azure-api.net/llm"
    verifier.installation.publisher_subscription_id = "unit-publisher"
    verifier.installation.operation_contracts = {"chat-completions": ("POST", "/chat/completions")}
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        candidate = projection.stage_candidate.call_args.args[0]
        assert candidate.allow_dispatch is False and candidate.alias == alias
        assert request.headers["x-turnstile-route-candidate"] == candidate.probe_id
        assert request.headers["x-request-id"] == candidate.request_id
        assert request.headers["Ocp-Apim-Subscription-Key"] == "unit-only"
        if fault == "unexpected-success":
            return httpx.Response(200, json={"usage": {"total_tokens": 1}})
        code = "route_disabled_probe_changed" if enabled else "model_disabled"
        return httpx.Response(503 if enabled else 403, json={"error": {
            "code": "upstream_failure" if fault == "ordinary-error" else code,
        }})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        port = FixedRoutePublisherPort(
            projection, verifier, SecretStr("unit-only"), client,
            active_fence=lambda: {"protocol_version": 2, "mode": "active",
                                  "gateway_profile_id": program.scope.target_id,
                                  "epoch": snapshot.approval.budget_epoch},
            verify_connections=Mock(), clock=lambda: 1100,
            prepare_availability=Mock(return_value=enabled),
        )
        if fault:
            with pytest.raises(ValueError):
                port.probe_unavailable(task, alias, journal)
        else:
            port.probe_unavailable(task, alias, journal)
            count_before = len(calls)
            with pytest.raises(ValueError, match="cannot be dispatched"):
                port.probe_changed(task, alias, journal)
            with pytest.raises(ValueError, match="cannot be counted"):
                port.probe_count(task, alias, journal)
            assert len(calls) == count_before
        assert len(calls) == (0 if fault == "no-ack" else 1)
        journal.run_route.assert_not_called()
        assert journal.record_request.call_count == len(calls)


@pytest.mark.parametrize(("program_upgraded", "fault"), [
    (program_upgraded, fault)
    for program_upgraded in (False, True)
    for fault in [None, "identity", "endpoint", "subscription", "base", "missing-gateway",
                  "foreign-gateway", "duplicate-gateway"] + ([
        "upgrade-origin", "upgrade-operation", "upgrade-trust", "upgrade-revision",
        "upgrade-legacy", "upgrade-base", "upgrade-before", "upgrade-capability",
        "upgrade-extra", "upgrade-parent",
    ] if program_upgraded else [])
])
def test_publisher_factory_binds_installed_program_to_current_target(
    fault: str | None, program_upgraded: bool,
) -> None:
    from copy import deepcopy
    from types import SimpleNamespace

    from pydantic import SecretStr

    from turnstile_core.domain.storage_routes import FixedProgramUpgrade, document_digest
    from turnstile_core.services.storage_route_publisher import (
        FixedProgramInstallation,
        FixedRoutePublisherFactory,
    )

    snapshot, program, _ = snapshot_fixture()
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm",
        publisher_subscription_id="unit-publisher",
        parent_policy_sha256="a" * 64, operation_policies_sha256={"chat-completions": "c" * 64},
        operation_contracts={"chat-completions": ("POST", "/chat/completions")},
        supported_profiles=("openai-v1",),
    )
    config = SimpleNamespace(
        gateway_profile_id=UUID(program.scope.target_id),
        target_identity_sha256="b" * 64,
        route_target_identity_sha256="b" * 64,
        settings=SimpleNamespace(
            apim_api_id=program.scope.api_id,
            apim_gateway_url=installation.gateway_endpoint,
            apim_probe_subscription_id="unit-publisher",
            apim_probe_subscription_key=SecretStr("unit"),
        ),
    )
    base = {"id": UUID(int=510), "gateway_profile_id": UUID(program.scope.target_id),
            "status": "active", "resource_manifest": {
                "fixed_route_program": installation.model_dump(mode="json"),
            }}
    repository, configurations = Mock(), Mock()
    repository.effective_gateway_publication.return_value = base
    gateway_config: dict[str, Any] = {}
    expected = installation
    if program_upgraded:
        expected = installation.model_copy(update={
            "model_additions_enabled": True,
            "legacy_program_sha256": program.program_sha256,
            "parent_policy_sha256": "d" * 64,
            "program": program.model_copy(update={"program_sha256": "e" * 64}),
        })
        upgrade = FixedProgramUpgrade(
            previous_publication_id=str(base["id"]),
            previous_installation_sha256=document_digest(installation), installation=expected,
        ).model_dump(mode="json")
        if fault == "upgrade-origin":
            upgrade["installation"]["gateway_endpoint"] = "https://foreign.example/llm"
        elif fault == "upgrade-operation":
            upgrade["installation"]["operation_contracts"]["chat-completions"] = ["GET", "/chat"]
        elif fault == "upgrade-trust":
            upgrade["installation"]["program"]["trust_binding_sha256"] = "f" * 64
        elif fault == "upgrade-revision":
            upgrade["installation"]["program"]["api_revision"] = "foreign"
        elif fault == "upgrade-legacy":
            upgrade["installation"]["legacy_program_sha256"] = "f" * 64
        elif fault == "upgrade-base":
            upgrade["previous_publication_id"] = str(UUID(int=511))
        elif fault == "upgrade-before":
            upgrade["previous_installation_sha256"] = "f" * 64
        elif fault == "upgrade-capability":
            upgrade["installation"]["model_additions_enabled"] = False
            upgrade["installation"]["legacy_program_sha256"] = None
        elif fault == "upgrade-extra":
            upgrade["unapproved"] = True
        elif fault == "upgrade-parent":
            upgrade["installation"]["parent_policy_sha256"] = installation.parent_policy_sha256
        gateway_config["fixed_route_program_upgrade"] = upgrade
    gateway = {"id": UUID(program.scope.target_id), "config": gateway_config}
    repository.get_gateway_profiles.return_value = [gateway]
    configurations.resolve.return_value = config
    if fault == "identity":
        config.route_target_identity_sha256 = "f" * 64
    elif fault == "endpoint":
        config.settings.apim_gateway_url = "https://foreign.example/llm"
    elif fault == "subscription":
        config.settings.apim_probe_subscription_id = "ordinary-application"
    elif fault == "base":
        base["id"] = UUID(int=511)
    elif fault == "missing-gateway":
        repository.get_gateway_profiles.return_value = []
    elif fault == "foreign-gateway":
        gateway["id"] = UUID(int=511)
    elif fault == "duplicate-gateway":
        repository.get_gateway_profiles.return_value = [gateway, gateway]
    original = deepcopy((base, gateway))
    factory = FixedRoutePublisherFactory(
        repository, configurations, active_fence=lambda _: {}, availability_projector=Mock(),
    )
    if fault:
        with pytest.raises(ValueError):
            factory.installed(UUID(program.scope.target_id), UUID(int=510))
    else:
        result, resolved = factory.installed(UUID(program.scope.target_id), UUID(int=510))
        assert result == expected
        assert resolved.gateway_profile_id == config.gateway_profile_id
        assert resolved.target_identity_sha256 == config.target_identity_sha256
    assert repository.method_calls == [
        ("effective_gateway_publication", (UUID(program.scope.target_id),), {}),
    ] + ([] if fault == "base" else [
        ("get_gateway_profiles", ([UUID(program.scope.target_id)],), {}),
    ])
    assert (base, gateway) == original
    assert snapshot.approval.scope == program.scope


@pytest.mark.parametrize("fault", [
    None, "native", "proof", "expired-proof", "head", "late-head", "target",
    "pending-pg", "config-pg",
])
@pytest.mark.parametrize("previous_model_additions", [False, True])
def test_program_upgrade_registration_verifies_native_state_before_conditional_write(
    fault: str | None, monkeypatch: pytest.MonkeyPatch, previous_model_additions: bool,
) -> None:
    from contextlib import nullcontext
    from copy import deepcopy

    from pydantic import SecretStr

    from tests.backend.model_platform.control_plane_support import (
        GatewayControlPlaneService,
        bedrock_publication,
    )
    from turnstile_core.domain.control_plane import PublicationStatus
    from turnstile_core.domain.storage_routes import (
        FixedProgramInstallation,
        FixedProgramUpgrade,
        document_digest,
    )
    from turnstile_core.persistence.in_memory import InMemoryRepository
    from turnstile_core.persistence.repository_publications import (
        PostgreSqlPublicationRepositoryMixin,
    )
    from turnstile_core.services import storage_route_publisher as module

    snapshot, program, proof = snapshot_fixture()
    target = UUID(program.scope.target_id)
    head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100)
    source = GatewayControlPlaneService(InMemoryRepository()).publish(
        bedrock_publication(), "unit-owner",
    )
    previous = FixedProgramInstallation(
        program=program, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm",
        publisher_subscription_id="unit-publisher", parent_policy_sha256="a" * 64,
        operation_policies_sha256={"models": "c" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
        model_additions_enabled=previous_model_additions,
    )
    upgrade = FixedProgramUpgrade(
        previous_publication_id=str(source.id),
        previous_installation_sha256=document_digest(previous),
        installation=previous.model_copy(update={
            "program": program.model_copy(update={"program_sha256": "e" * 64}),
            "parent_policy_sha256": "f" * 64, "model_additions_enabled": True,
            "legacy_program_sha256": program.program_sha256,
        }),
    )
    source = source.model_copy(update={
        "gateway_profile_id": target, "status": PublicationStatus.ACTIVE,
        "desired_spec": source.desired_spec.model_copy(update={"gateway_profile_id": target}),
        "resource_manifest": {
            "fixed_route_program": previous.model_dump(mode="json"),
            "fixed_route_snapshot": snapshot.model_dump(mode="json"),
            "fixed_route_head": head.model_dump(mode="json"),
        },
    })
    original = source.model_dump(mode="json")
    config_state = {"config": {"preserved": "original"}}
    original_config = deepcopy(config_state["config"])
    repository, configuration, verifier, projection = Mock(), Mock(), Mock(), Mock()
    repository.effective_gateway_publication.return_value = source.model_dump(mode="python")
    repository.get_gateway_profiles.side_effect = lambda identifiers: [
        {"id": target, "config": config_state["config"]},
    ]
    repository.model_availability_proof_guard.side_effect = lambda *args, **kwargs: nullcontext()
    configuration.resolve.return_value = Mock(
        gateway_profile_id=target,
        route_target_identity_sha256="f" * 64 if fault == "target" else "b" * 64,
        settings=Mock(
            apim_api_id=program.scope.api_id, apim_gateway_url=previous.gateway_endpoint,
            apim_probe_subscription_id=previous.publisher_subscription_id,
            apim_probe_subscription_key=SecretStr("unit-publisher"),
        ),
    )
    if fault == "native":
        verifier.verify.side_effect = ValueError("UNIT_NATIVE_PROGRAM_MISMATCH")
    current_proof = proof.model_copy(update={
        "identity": upgrade.installation.program,
        **({"issued_epoch": 900, "expires_epoch": 1020} if fault == "expired-proof" else {}),
    })
    projection.read_program_proof.return_value = (
        None if fault == "proof" else (current_proof, 'W/"new-proof"')
    )
    projection.read_active.return_value = None if fault == "head" else (head, 'W/"old-head"')
    if fault == "late-head":
        projection.read_active.side_effect = [(head, 'W/"old-head"'), None]
    monkeypatch.setattr(module, "AzureApimPublisherClient", Mock(return_value=Mock()))
    monkeypatch.setattr(module, "FixedProgramVerifier", Mock(return_value=verifier))
    monkeypatch.setattr(module, "TableRouteProjection", Mock(return_value=projection))
    writes: list[str] = []

    def execute(statement: str, parameters: tuple[Any, ...] = ()) -> Mock:
        normalized = " ".join(statement.split())
        result: Any = None
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            assert parameters == (f"gateway-publication:{target}",)
        elif normalized.startswith("SELECT id FROM gateway_publication"):
            result = {"id": source.id} if fault == "pending-pg" else None
        elif normalized.startswith("SELECT publication.*"):
            result = source.model_dump(mode="python")
        elif normalized.startswith("SELECT config FROM gateway_profile"):
            result = {"config": {"changed": True}} if fault == "config-pg" else config_state
        else:
            assert normalized == (
                "UPDATE gateway_profile SET config = %s, updated_at = now() WHERE id = %s"
            )
            assert parameters[1] == target
            config_state["config"] = parameters[0].obj
            writes.append(normalized)
        return Mock(fetchone=Mock(return_value=result))

    connection = Mock(execute=Mock(side_effect=execute))
    connection.transaction.side_effect = lambda: nullcontext()
    sql_repository = Mock()
    sql_repository._connection.side_effect = lambda: nullcontext(connection)
    repository.record_fixed_program_upgrade.side_effect = lambda descriptor, digest: (
        PostgreSqlPublicationRepositoryMixin.record_fixed_program_upgrade(
            sql_repository, descriptor, digest,
        )
    )
    def register() -> None:
        module.register_verified_program_upgrade(
            repository, configuration, upgrade, document_digest(original_config),
            clock=lambda: 1100,
        )

    if fault:
        with pytest.raises(ValueError):
            register()
        assert not writes
    else:
        register()
        register()
        assert len(writes) == 1
        assert config_state["config"] == {
            **original_config, "fixed_route_program_upgrade": upgrade.model_dump(mode="json"),
        }
    assert source.model_dump(mode="json") == original
    assert all(
        arguments.args == (target,) and arguments.kwargs == {"wait": True}
        for arguments in repository.model_availability_proof_guard.call_args_list
    )
    assert all(call[0] in {"read_active", "read_program_proof"} for call in projection.method_calls)


@pytest.mark.parametrize("complete_operations,decision,fault", [
    (complete, decision, fault)
    for complete, decision in ((False, False), (True, False), (True, True))
    for fault in (None, "etag", "bytes", "old-proof", "native", "pending-pg", "config-pg",
                  "operation-etag")
] + [(True, True, "parent-readback")] + [
    (False, False, state) for state in (
        "written", "xml-normalized", "foreign-parent", "post-put-verifier", "written-expired",
    )
])
def test_program_upgrade_cutover_holds_proof_guard_until_registration(
    monkeypatch: pytest.MonkeyPatch, fault: str | None, complete_operations: bool, decision: bool,
) -> None:
    from contextlib import contextmanager, nullcontext
    from xml.etree import ElementTree as ET

    from pydantic import SecretStr

    from tests.backend.model_platform.control_plane_support import (
        GatewayControlPlaneService,
        bedrock_publication,
    )
    from turnstile_core.domain.control_plane import PublicationStatus
    from turnstile_core.domain.storage_routes import (
        FixedProgramInstallation,
        FixedProgramUpgrade,
        document_digest,
    )
    from turnstile_core.integrations.apim_storage_routes import fixed_policy_digest
    from turnstile_core.persistence.in_memory import InMemoryRepository
    from turnstile_core.persistence.repository_publications import (
        PostgreSqlPublicationRepositoryMixin,
    )
    from turnstile_core.services import storage_route_publisher as module

    snapshot, program, proof = snapshot_fixture()
    target = UUID(program.scope.target_id)
    head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100)
    old_parent = "<policies><inbound><base /></inbound></policies>"
    previous = FixedProgramInstallation(
        program=program, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm",
        publisher_subscription_id="unit-publisher",
        parent_policy_sha256=fixed_policy_digest(old_parent),
        operation_policies_sha256={"models": "c" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
    )
    parent = (
        '<policies><inbound><set-variable name="routeProgram" '
        'value="@{\n return &quot;new&quot;;\n}" /></inbound></policies>'
    )
    upgrade = FixedProgramUpgrade(
        previous_publication_id=str(UUID(int=75)),
        previous_installation_sha256=document_digest(previous),
        installation=previous.model_copy(update={
            "program": program.model_copy(update={"program_sha256": "e" * 64}),
            "parent_policy_sha256": "f" * 64 if fault == "bytes" else fixed_policy_digest(parent),
            "model_additions_enabled": True, "legacy_program_sha256": program.program_sha256,
            **({"decision_enabled": True,
                "supported_profiles": (*previous.supported_profiles, "microsoft-decision"),
                "operation_contracts": {**previous.operation_contracts,
                    "microsoft-decision": ("POST", "/providers/microsoft/v1/systemone")},
                "operation_policies_sha256": {**previous.operation_policies_sha256,
                                              "microsoft-decision": "d" * 64}}
               if decision else {}),
        }),
    )
    source = GatewayControlPlaneService(InMemoryRepository()).publish(bedrock_publication(), "unit")
    source = source.model_copy(update={
        "id": UUID(int=75), "gateway_profile_id": target, "status": PublicationStatus.ACTIVE,
        "desired_spec": source.desired_spec.model_copy(update={"gateway_profile_id": target}),
        "resource_manifest": {"fixed_route_program": previous.model_dump(mode="json"),
                              "fixed_route_snapshot": snapshot.model_dump(mode="json"),
                              "fixed_route_head": head.model_dump(mode="json")},
    })
    repository, configurations, management, projection = Mock(), Mock(), Mock(), Mock()
    repository.effective_gateway_publication.return_value = source.model_dump(mode="python")
    repository.get_gateway_profiles.return_value = [{"id": target, "config": {}}]
    configurations.resolve.return_value = Mock(
        gateway_profile_id=target, route_target_identity_sha256="b" * 64,
        settings=Mock(
            apim_api_id=program.scope.api_id, apim_gateway_url=previous.gateway_endpoint,
            apim_probe_subscription_id=previous.publisher_subscription_id,
            apim_probe_subscription_key=SecretStr("unit-key"),
            ledger_table_endpoint="https://unitstore.table.core.windows.net",
            ledger_table_name="Ledger",
        ),
    )
    events: list[str] = []

    @contextmanager
    def guarded(*args: Any, **kwargs: Any) -> Any:
        events.append("guard-enter")
        try:
            yield
        finally:
            events.append("guard-exit")

    repository.model_availability_proof_guard.side_effect = guarded
    management._request.return_value = Mock(status_code=200, headers={
        "ETag": '"wrong"' if fault == "etag" else '"original"',
    })
    def request(method: str, *args: Any, **kwargs: Any) -> Mock:
        if method == "PUT":
            events.append("operation-put" if "/operations/" in args[0] else "parent-put")
            assert kwargs["extra_headers"] == {"If-Match": '"original"'}
            if "/operations/" not in args[0]:
                uploaded = ET.fromstring(kwargs["json_body"]["properties"]["value"])
                assert fixed_policy_digest(ET.tostring(uploaded, encoding="unicode")) == (
                    fixed_policy_digest(parent)
                )
                assert "&quot;" not in kwargs["json_body"]["properties"]["value"]
            return Mock(status_code=200)
        if method == "GET":
            assert decision and "/operations/" not in args[0]
            events.append("parent-read")
            return Mock(status_code=500 if fault == "parent-readback" else 200)
        if "/operations/microsoft-decision" in args[0]:
            return Mock(status_code=200 if fault == "operation-etag" else 404)
        return Mock(status_code=200, headers={
            "ETag": '"wrong"' if fault == "etag" or (
                fault == "operation-etag" and "/operations/" in args[0]
            ) else '"original"',
        })

    management._request.side_effect = request
    management._policy_response_value.return_value = parent
    native_parent = parent if fault in {"written", "written-expired"} else (
        ET.tostring(ET.fromstring(parent), encoding="unicode") if fault == "xml-normalized"
        else "<policies><inbound><base /><return-response /></inbound></policies>"
        if fault == "foreign-parent" else old_parent
    )
    management._read_policy.return_value = (
        Mock(status_code=200, headers={"ETag": '"original"'}), native_parent,
    )
    management.ensure_operation.side_effect = lambda *args, **options: (
        events.append("operation-create") if options == {"create_only": True} else pytest.fail()
    )
    management.put_operation_policy.side_effect = lambda *args, **options: (
        events.append("new-policy-put") if options == {"create_only": True} else pytest.fail()
    )
    fresh = proof.model_copy(update={"identity": upgrade.installation.program})
    projection.read_program_proof.side_effect = [
        None if fault == "old-proof" else (
            proof.model_copy(update={"expires_epoch": 1100}) if fault == "written-expired"
            else proof, 'W/"old"'
        ), (fresh, 'W/"new"'),
    ]
    projection.read_active.return_value = (head, 'W/"head"')
    projection.publish_upgraded_program_proof.side_effect = lambda *args: events.append("proof-put")

    @contextmanager
    def transaction() -> Any:
        events.append("database-enter")
        try:
            yield
        finally:
            events.append("database-exit")

    def execute(statement: str, parameters: tuple[Any, ...] = ()) -> Mock:
        normalized = " ".join(statement.split())
        result: Any = None
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            assert parameters == (f"gateway-publication:{target}",)
        elif normalized.startswith("SELECT id FROM gateway_publication"):
            result = {"id": source.id} if fault == "pending-pg" else None
        elif normalized.startswith("SELECT publication.*"):
            result = source.model_dump(mode="python")
        elif normalized.startswith("SELECT config FROM gateway_profile"):
            result = {"config": {"changed": True} if fault == "config-pg" else {}}
        else:
            assert normalized == (
                "UPDATE gateway_profile SET config = %s, updated_at = now() WHERE id = %s"
            )
            assert parameters[1] == target
            assert parameters[0].obj == {"fixed_route_program_upgrade": upgrade.model_dump(
                mode="json",
            )}
            events.append("registration")
        return Mock(fetchone=Mock(return_value=result))

    connection = Mock(execute=Mock(side_effect=execute))
    connection.transaction.side_effect = transaction
    repository._connection.side_effect = lambda: nullcontext(connection)
    repository.record_fixed_program_upgrade.side_effect = lambda descriptor, digest, **options: (
        PostgreSqlPublicationRepositoryMixin.record_fixed_program_upgrade(
            repository, descriptor, digest, **options,
        )
    )
    prior_verifier, next_verifier = Mock(), Mock()
    next_verifier.verify.return_value = fresh
    if fault == "post-put-verifier":
        next_verifier.verify.side_effect = ValueError("Injected post-write validation failure")
    if fault == "native":
        prior_verifier.verify.side_effect = ValueError("Original native program changed")
    monkeypatch.setattr(module, "AzureApimPublisherClient", Mock(return_value=management))
    monkeypatch.setattr(module, "FixedProgramVerifier", Mock(
        side_effect=[next_verifier, prior_verifier],
    ))
    monkeypatch.setattr(module, "TableRouteProjection", Mock(return_value=projection))
    monkeypatch.setattr(
        module, "_validate_program_cutover", lambda *args, **options: None, raising=False,
    )

    def cutover() -> None:
        module.register_verified_program_upgrade(
            repository, configurations, upgrade, document_digest({}), clock=lambda: 1100,
            parent_policy=parent, expected_parent_etag='"original"',
            operation_policies={"models": "<policies />",
                **({"microsoft-decision": "<policies />"} if decision else {})}
            if complete_operations else None,
            expected_operation_etags={"models": '"original"',
                **({"microsoft-decision": None} if decision else {})}
            if complete_operations else None,
        )

    recovering = fault in {"written", "xml-normalized", "written-expired"}
    if fault and not recovering and (fault != "operation-etag" or complete_operations):
        with pytest.raises(ValueError):
            cutover()
        assert not {"operation-put", "operation-create", "new-policy-put", "registration"} & set(
            events,
        )
        assert ("parent-put" in events) is (fault in {"parent-readback", "post-put-verifier"})
    else:
        cutover()
        assert events == [
            "guard-enter", "database-enter", *(["parent-put", "parent-read"] if decision else []),
            *(["operation-put"] if complete_operations else []),
            *(["operation-create", "new-policy-put"] if decision else (
                [] if fault in {"written", "written-expired"} else ["parent-put"]
            )),
            "proof-put", "registration",
            "database-exit", "guard-exit",
        ]
        if recovering:
            prior_verifier.verify.assert_not_called()
    assert events[0] == "guard-enter" and events[-1] == "guard-exit"


@pytest.mark.parametrize("fault", [None, "hash", "target", "history"])
def test_program_upgrade_preview_exposes_verified_current_decision_installation(
    fault: str | None,
) -> None:
    from fastapi import Response

    from backend.http.model_platform import preview_gateway_program_upgrade
    from turnstile_core.domain.storage_routes import (
        FixedProgramInstallation,
        FixedProgramUpgrade,
        document_digest,
    )

    _, program, _ = snapshot_fixture()
    target = UUID(program.scope.target_id)
    previous = FixedProgramInstallation(
        program=program, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm",
        publisher_subscription_id="unit-publisher",
        parent_policy_sha256="a" * 64, operation_policies_sha256={"models": "c" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
    )
    publication_id = str(UUID(int=75))
    history = []
    expected_previous = previous
    if fault == "history":
        expected_previous = previous.model_copy(update={
            "program": program.model_copy(update={"program_sha256": "7" * 64}),
            "parent_policy_sha256": "8" * 64, "model_additions_enabled": True,
            "legacy_program_sha256": program.program_sha256,
        })
        history = [FixedProgramUpgrade(
            previous_publication_id=publication_id,
            previous_installation_sha256=document_digest(previous),
            installation=expected_previous,
        ).model_dump(mode="json")]
        intermediate = expected_previous
        expected_previous = intermediate.model_copy(update={
            "program": program.model_copy(update={"program_sha256": "9" * 64}),
            "parent_policy_sha256": "a" * 64,
            "predecessor_program_sha256": (intermediate.program.program_sha256,),
        })
        history.append(FixedProgramUpgrade(
            previous_publication_id=publication_id,
            previous_installation_sha256=document_digest(intermediate),
            installation=expected_previous,
        ).model_dump(mode="json"))
    current = previous.model_copy(update={
        "program": program.model_copy(update={"program_sha256": "6" * 64,
            **({"scope": program.scope.model_copy(update={"target_id": str(UUID(int=999))})}
               if fault == "target" else {})}),
        "parent_policy_sha256": "e" * 64, "legacy_program_sha256": program.program_sha256,
        "model_additions_enabled": True, "decision_enabled": True,
        "supported_profiles": ("openai-v1", "microsoft-decision"),
        "operation_contracts": {**previous.operation_contracts,
            "microsoft-decision": ("POST", "/providers/microsoft/v1/systemone")},
        "operation_policies_sha256": {**previous.operation_policies_sha256,
                                     "microsoft-decision": "f" * 64},
          **({"predecessor_program_sha256": ("7" * 64, "9" * 64)}
              if fault == "history" else {}),
    })
    registration = FixedProgramUpgrade(
        previous_publication_id=publication_id,
        previous_installation_sha256=("0" * 64 if fault == "hash"
                                     else document_digest(expected_previous)),
        installation=current,
    ).model_dump(mode="json")
    repository = Mock()
    repository.effective_gateway_publication.return_value = {
        "id": publication_id, "resource_manifest": {
            "fixed_route_program": previous.model_dump(mode="json"),
        },
    }
    repository.get_gateway_profiles.return_value = [{"id": target, "config": {
        "fixed_route_program_upgrade": registration,
        "fixed_route_program_upgrade_history": history,
    }}]
    response = Response()
    if fault in {"hash", "target"}:
        with pytest.raises(ValueError):
            preview_gateway_program_upgrade(target, repository, "unit-owner", response)
    else:
        result = preview_gateway_program_upgrade(target, repository, "unit-owner", response)
        assert result["installation"] == expected_previous.model_dump(mode="json")
        assert result["current_installation"] == current.model_dump(mode="json")
        assert result["current_installation_sha256"] == document_digest(current)
        assert result["registration"] == registration
        assert response.headers["Cache-Control"] == "no-store"
    assert [call[0] for call in repository.method_calls] == [
        "effective_gateway_publication", "get_gateway_profiles",
    ]


@pytest.mark.parametrize("fault", [None, "config", "base", "history", "predecessor"])
@pytest.mark.parametrize("include_predecessors", [False, True])
def test_subsequent_program_upgrade_preserves_registration_history_and_original_routes(
    fault: str | None,
    include_predecessors: bool,
) -> None:
    from tests.backend.model_platform.control_plane_support import (
        GatewayControlPlaneService,
        bedrock_publication,
    )
    from turnstile_core.domain.storage_routes import (
        FixedProgramInstallation,
        FixedProgramUpgrade,
        document_digest,
    )
    from turnstile_core.persistence.in_memory import InMemoryRepository
    from turnstile_core.persistence.repository_support import (
        _fixed_program_upgrade_config,
        _managed_program_publication,
        _registered_program_installation,
    )

    _, program, _ = snapshot_fixture()
    base = FixedProgramInstallation(
        program=program, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm", publisher_subscription_id="unit",
        parent_policy_sha256="a" * 64, operation_policies_sha256={"models": "c" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
    )
    source = GatewayControlPlaneService(InMemoryRepository()).publish(bedrock_publication(), "unit")
    source = source.model_copy(update={
        "status": "active", "gateway_profile_id": UUID(program.scope.target_id),
        "desired_spec": source.desired_spec.model_copy(update={
            "gateway_profile_id": UUID(program.scope.target_id),
        }), "resource_manifest": {"fixed_route_program": base.model_dump(mode="json")},
    })
    first = FixedProgramUpgrade(
        previous_publication_id=str(source.id), previous_installation_sha256=document_digest(base),
        installation=base.model_copy(update={
            "program": program.model_copy(update={"program_sha256": "1" * 64}),
            "parent_policy_sha256": "e" * 64, "model_additions_enabled": True,
            "legacy_program_sha256": program.program_sha256,
        }),
    )
    config = {"fixed_route_program_upgrade": first.model_dump(mode="json"), "untouched": True}
    next_upgrade = FixedProgramUpgrade(
        previous_publication_id=str(source.id),
        previous_installation_sha256=document_digest(
            base if fault == "base" else first.installation,
        ),
        installation=first.installation.model_copy(update={
            "program": program.model_copy(update={"program_sha256": "2" * 64}),
            "parent_policy_sha256": "b" * 64, "operation_policies_sha256": {"models": "d" * 64},
            "predecessor_program_sha256": ("f" * 64,) if fault == "predecessor"
            else (first.installation.program.program_sha256,) if include_predecessors else (),
        }),
    )
    if fault == "history":
        config["fixed_route_program_upgrade_history"] = [first.model_dump(mode="json")]
    original = canonical_document(config)
    if fault:
        with pytest.raises(ValueError):
            _fixed_program_upgrade_config(
                source.model_dump(mode="python"), config, next_upgrade.model_dump(mode="json"),
                "a" * 64 if fault == "config" else document_digest(config),
            )
    else:
        updated = _fixed_program_upgrade_config(
            source.model_dump(mode="python"), config, next_upgrade.model_dump(mode="json"),
            document_digest(config),
        )
        assert updated is not None and updated["untouched"] is True
        assert updated["fixed_route_program_upgrade_history"] == [first.model_dump(mode="json")]
        assert _registered_program_installation(str(source.id), base, updated) == (
            next_upgrade.installation
        )
        derived = _managed_program_publication(source.model_dump(mode="python"), updated)
        assert derived is not None
        assert derived["resource_manifest"]["fixed_route_program"] == first.installation.model_dump(
            mode="json",
        )
        next_upgrade.require_previous(str(source.id), first.installation)
        assert _fixed_program_upgrade_config(
            source.model_dump(mode="python"), updated, next_upgrade.model_dump(mode="json"),
            document_digest(updated),
        ) is None
    assert canonical_document(config) == original


@pytest.mark.parametrize("initial_legacy", [None, "9" * 64])
@pytest.mark.parametrize("upgrades", [0, 1, 2])
def test_empty_installation_accepts_model_additions_after_every_registered_upgrade(
    upgrades: int, initial_legacy: str | None,
) -> None:
    from tests.backend.model_platform.control_plane_support import (
        APIM_ID,
        GatewayControlPlaneService,
        bedrock_publication,
        foundry_publication,
    )
    from turnstile_core.domain.control_plane import (
        GatewayPublication,
        GatewayReleaseSpec,
        PublicationKind,
        PublicationStatus,
    )
    from turnstile_core.domain.runtime_models import ProviderTarget
    from turnstile_core.domain.storage_routes import FixedProgramInstallation, FixedProgramUpgrade
    from turnstile_core.persistence.in_memory import InMemoryRepository
    from turnstile_core.persistence.repository_support import (
        _fixed_program_upgrade_config,
        _managed_program_publication,
    )

    _, program, _ = snapshot_fixture()
    program = program.model_copy(update={
        "scope": program.scope.model_copy(update={"target_id": str(APIM_ID)}),
    })
    legacy_fields: dict[str, Any] = (
        {"model_additions_enabled": True, "legacy_program_sha256": initial_legacy}
        if initial_legacy else {}
    )
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm", publisher_subscription_id="unit",
        parent_policy_sha256="a" * 64, operation_policies_sha256={"models": "c" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
        **legacy_fields,
    )
    version = str(UUID(int=76))
    _, manifest, dependencies = configuration_manifests(program.scope, version, (), {}, 1100)
    approval = ConfigurationApproval(
        approval_id=str(UUID(int=77)), scope=program.scope, route_version=version,
        generation=1, manifest_sha256=manifest, dependency_manifest_sha256=dependencies,
        catalog_sha256=catalog_digest(()), entry_count=0,
        required_program_sha256=program.program_sha256, required_profiles=(),
        budget_epoch=str(UUID(int=2)), issued_epoch=1100, activate_before_epoch=1700,
    )
    template = GatewayControlPlaneService(InMemoryRepository()).publish(
        bedrock_publication(), "unit",
    )
    empty = template.model_copy(update={
        "id": UUID(int=75), "gateway_profile_id": APIM_ID, "status": PublicationStatus.ACTIVE,
        "publication_kind": PublicationKind.ROUTE_RECONCILE, "generation": 1,
        "base_release_id": None, "apim_revision": program.api_revision,
        "policy_sha256": program.program_sha256,
        "desired_spec": GatewayReleaseSpec(gateway_profile_id=APIM_ID, bindings=[],
                                           discovery_models=[]),
        "resource_manifest": {
            "fixed_route_program": installation.model_dump(mode="json"),
            "fixed_route_snapshot": RouteSnapshot.prepare(approval, (), {}, 1100).model_dump(
                mode="json",
            ),
            "fixed_route_head": ActiveRouteHead(
                approval=approval, activated_epoch=1100,
            ).model_dump(mode="json"),
        },
    }).model_dump(mode="python")
    config: dict[str, Any] = {}
    previous = installation
    for index in range(upgrades):
        # The production upgrade chain: legacy is carried, earlier programs become predecessors.
        legacy = previous.legacy_program_sha256 or previous.program.program_sha256
        upgraded = previous.model_copy(update={
            "program": program.model_copy(update={"program_sha256": str(index + 1) * 64}),
            "parent_policy_sha256": str(index + 4) * 64, "model_additions_enabled": True,
            "legacy_program_sha256": legacy,
            "predecessor_program_sha256": tuple(value for value in (
                *previous.predecessor_program_sha256, previous.program.program_sha256,
            ) if value != legacy),
        })
        updated = _fixed_program_upgrade_config(empty, config, FixedProgramUpgrade(
            previous_publication_id=str(UUID(int=75)),
            previous_installation_sha256=document_digest(previous), installation=upgraded,
        ).model_dump(mode="json"), document_digest(config))
        assert updated is not None
        config, previous = updated, upgraded
    derived = _managed_program_publication(empty, config)
    assert derived is not None
    assert GatewayPublication.model_validate(derived).desired_spec.bindings == []
    overlaid = derived["resource_manifest"]["fixed_route_program"]
    assert (overlaid != empty["resource_manifest"]["fixed_route_program"]) is (upgrades == 2)
    if upgrades == 2:
        forged = deepcopy(derived)
        assert program.program_sha256 not in {"e" * 64, "8" * 64}
        forged["resource_manifest"]["fixed_route_program"].update(
            legacy_program_sha256="e" * 64, predecessor_program_sha256=["8" * 64],
        )
        with pytest.raises(ValueError, match="configuration binding changed"):
            GatewayPublication.model_validate(forged)
    if not upgrades:
        return
    repository = InMemoryRepository()
    repository.gateway_publications.append(empty)
    repository.effective_gateway_releases[APIM_ID] = empty["id"]
    next(row for row in repository.gateways if row["id"] == APIM_ID)["config"] = config
    request = foundry_publication("unit-upgraded-empty-installation")
    request.provider = ProviderTarget(existing_id=next(
        row["id"] for row in repository.providers if row["brand_key"] == "microsoft_foundry"
    ))

    addition = GatewayControlPlaneService(
        repository, apim_principal_id=str(UUID(int=705)),
    ).publish(request, "unit-owner")

    assert addition.publication_kind is PublicationKind.MODEL_ADD
    assert addition.base_release_id == empty["id"]
    assert set(addition.resource_manifest) == {"model_route_addition"}
    assert repository.effective_gateway_releases[APIM_ID] == empty["id"]


@pytest.mark.parametrize("fault", [
    None, "previous-id", "previous-revision", "previous-spec", "previous-policy",
    "target", "already-installed", "routes", "missing-marker",
    "runtime-provider", "model-provider", "model-upstream", "ambiguous-model", "unmanaged-runtime",
    "retry", "retry-probed", "retry-prepared", "retry-credential", "retry-base", "retry-active",
    "retry-pending", "retry-spec", "retry-checkpoint", "retry-context", "retry-progress",
    "retry-configuration",
])
def test_program_migration_binds_unmodified_legacy_release_and_new_candidate(
    fault: str | None,
) -> None:
    from pydantic import SecretStr

    from tests.backend.model_platform.control_plane_support import (
        GatewayControlPlaneService,
        bedrock_publication,
    )
    from turnstile_core.domain.control_plane import (
        GatewayPublicationRetry,
        PublicationKind,
        PublicationStatus,
    )
    from turnstile_core.domain.gateway_targets import GatewayServingDeployment
    from turnstile_core.persistence.in_memory import InMemoryRepository
    from turnstile_core.services.storage_route_publisher import (
        FixedProgramInstallation,
        FixedProgramMigration,
    )

    _, program, _ = snapshot_fixture()
    target = UUID(program.scope.target_id)
    previous = GatewayControlPlaneService(InMemoryRepository()).publish(
        bedrock_publication(), "unit-owner",
    )
    serving = GatewayServingDeployment.model_validate({
        "deployment_id": UUID(int=512), "native_gateway_id": "managed", "kind": "managed",
        "endpoint_url": "https://unit.azure-api.net/llm", "release_gate": "required",
    })
    previous = previous.model_copy(update={
        "gateway_profile_id": target, "status": PublicationStatus.ACTIVE,
        "apim_revision": "previous", "policy_sha256": "b" * 64,
        "desired_spec": previous.desired_spec.model_copy(update={
            "gateway_profile_id": target, "gateway_deployments": (serving,),
        }),
    })
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm",
        publisher_subscription_id="unit-publisher", parent_policy_sha256="a" * 64,
        operation_policies_sha256={"chat-completions": "c" * 64},
        operation_contracts={"chat-completions": ("POST", "/chat/completions")},
        supported_profiles=("openai-v1",),
    )
    assert previous.policy_sha256 is not None
    migration = FixedProgramMigration(
        request_id=UUID(int=514), previous_publication_id=previous.id,
        previous_revision="previous", previous_spec_sha256=previous.desired_spec_sha256,
        previous_policy_sha256=previous.policy_sha256, installation=installation,
    )
    candidate = previous.model_copy(update={
        "id": migration.request_id, "base_release_id": previous.id,
        "status": PublicationStatus.QUEUED,
        "publication_kind": PublicationKind.ROUTE_RECONCILE,
        "resource_manifest": {"fixed_route_migration": migration.model_dump(mode="json")},
    })
    if fault and fault.startswith("previous-"):
        name = {"previous-id": "id", "previous-revision": "apim_revision",
                "previous-spec": "desired_spec_sha256", "previous-policy": "policy_sha256"}[fault]
        previous = previous.model_copy(update={name: UUID(int=516) if name == "id" else "f" * 64})
    elif fault == "target":
        previous = previous.model_copy(update={"gateway_profile_id": UUID(int=516)})
    elif fault == "already-installed":
        previous = previous.model_copy(update={"resource_manifest": {
            "fixed_route_program": installation.model_dump(mode="json"),
        }})
    elif fault == "routes":
        candidate = candidate.model_copy(update={"desired_spec": candidate.desired_spec.model_copy(
            update={"bindings": []},
        )})
    elif fault == "missing-marker":
        candidate = candidate.model_copy(update={"resource_manifest": {}})
    identity_faults = {"runtime-provider", "model-provider", "model-upstream", "ambiguous-model",
                       "unmanaged-runtime"}
    retry_case = fault is not None and fault.startswith("retry")
    if fault and fault not in identity_faults and not retry_case:
        with pytest.raises(ValueError):
            migration.require_candidate(candidate, previous)
    else:
        migration.require_candidate(candidate, previous)
        assert "fixed_route_program" not in previous.resource_manifest
        repository = InMemoryRepository()
        repository.gateways.append({**repository.gateways[0], "id": target})
        for index, legacy_binding in enumerate(previous.desired_spec.bindings):
            if legacy_binding.runtime_id is not None:
                continue
            runtime_id = UUID(int=520 + index)
            provider_id = legacy_binding.provider_id or UUID(int=560 + index)
            repository.runtimes.append({
                "id": runtime_id, "gateway_profile_id": target, "enabled": True,
                "provider_id": provider_id,
                "name": legacy_binding.runtime_name,
                "config": {**legacy_binding.runtime_config, "control_plane_managed": True,
                           "backend_url": str(legacy_binding.backend_url),
                           "backend_path": legacy_binding.backend_path,
                           "auth_strategy": legacy_binding.auth_strategy.value,
                           "named_value_name": legacy_binding.named_value_name},
            })
            repository.models.append({
                "id": legacy_binding.logical_model_id or UUID(int=540 + index),
                "model_key": legacy_binding.model.model_key,
                "runtime_id": runtime_id, "provider_id": provider_id,
                "upstream_model_id": legacy_binding.model.upstream_model_id,
            })
        original_row = previous.model_dump(mode="python")
        before = deepcopy(original_row)
        repository.gateway_publications = [original_row]
        repository.effective_gateway_releases[target] = previous.id
        repository.gateway_deployments[serving.deployment_id] = {
            "id": serving.deployment_id, "apim_target_id": target,
            "native_gateway_id": "managed", "endpoint_url": str(serving.endpoint_url),
            "retired_at": None,
        }
        service = GatewayControlPlaneService(repository)
        if fault in identity_faults:
            if fault == "runtime-provider":
                repository.runtimes[-1]["provider_id"] = UUID(int=599)
            elif fault == "model-provider":
                repository.models[-1]["provider_id"] = UUID(int=599)
            elif fault == "model-upstream":
                repository.models[-1]["upstream_model_id"] = "different-model"
            elif fault == "ambiguous-model":
                repository.models.append(dict(repository.models[-1]))
            else:
                repository.runtimes[-1]["config"]["control_plane_managed"] = False
            with pytest.raises(ValueError, match="uniquely materialized"):
                service.queue_fixed_program_migration(
                    target, migration.model_dump(mode="json"), "unit-owner",
                )
            assert not repository.gateway_publication_outbox
            assert not repository.gateway_publication_audit
            assert original_row == before
            return
        queued = service.queue_fixed_program_migration(
            target, migration.model_dump(mode="json"), "unit-owner",
        )
        repeated = service.queue_fixed_program_migration(
            target, migration.model_dump(mode="json"), "unit-owner",
        )
        assert queued.id == repeated.id and queued.base_release_id == previous.id
        assert queued.id == migration.request_id
        migration.require_candidate(queued, previous)
        assert queued.resource_manifest == {
            "fixed_route_migration": migration.model_dump(mode="json"),
        }
        assert len(repository.gateway_publication_outbox) == 1
        assert len(repository.gateway_publication_audit) == 1
        assert repository.gateway_publication_audit[0]["detail"] == queued.resource_manifest
        assert original_row == before
        from datetime import UTC, datetime, timedelta

        queued_row = repository.get_gateway_publication(queued.id)
        assert queued_row is not None
        if retry_case:
            queued_row.update(status="failed", attempt_count=3,
                              error_code="PolicyCompilationError", error_message="unit denied",
                              started_at=datetime.now(UTC), completed_at=datetime.now(UTC))
            outbox = repository.gateway_publication_outbox[0]
            outbox.update(status="completed", attempts=3, lease_owner=None, lease_expires_at=None)
            if fault == "retry-probed":
                repository.gateway_publication_audit.append({
                    "publication_id": queued.id, "detail": {"probe_request": {"method": "POST"}},
                    "id": UUID(int=697), "created_at": datetime.now(UTC),
                })
            elif fault == "retry-prepared":
                queued_row["resource_manifest"]["route_preparation"] = {"unit": "recorded"}
            elif fault in {"retry-checkpoint", "retry-context", "retry-progress",
                           "retry-configuration"}:
                from turnstile_core.services.storage_route_publisher import (
                    FixedRoutePublisherFactory,
                )

                queued_row["resource_manifest"].update({
                    "route_preparation": {
                        "version": 1,
                        "context_sha256": FixedRoutePublisherFactory._dependency_context(
                            queued, installation,
                        ) if fault != "retry-context" else "f" * 64,
                        "credentials": {}, "connections": {},
                    },
                    "route_credentials": {"version": 1, "bindings": {}},
                    "route_last_progress_attempt": 4 if fault == "retry-progress" else 3,
                })
                if fault == "retry-configuration":
                    queued_row["resource_manifest"]["route_configuration"] = {}
            elif fault == "retry-base":
                repository.effective_gateway_releases[target] = UUID(int=699)
            elif fault == "retry-active":
                queued_row["status"] = "active"
            elif fault == "retry-pending":
                repository.gateway_publications.append({**queued_row, "id": UUID(int=698),
                                                        "status": "queued"})
            preserved_row = deepcopy(queued_row)
            preserved_outbox = deepcopy(outbox)
            prior_audit = deepcopy(repository.gateway_publication_audit)
            if fault in {"retry", "retry-checkpoint"}:
                restored = service.retry(queued.id, GatewayPublicationRetry(), "unit-recovery")
                assert restored.id == queued.id and restored.status.value == "queued"
                for name in ("resource_manifest", "desired_spec", "desired_spec_sha256",
                             "attempt_count", "started_at", "base_release_id", "created_by"):
                    assert queued_row[name] == preserved_row[name]
                assert outbox["attempts"] == preserved_outbox["attempts"] == 3
                assert outbox["status"] == "queued"
                assert repository.gateway_publication_audit[:-1] == prior_audit
                assert repository.gateway_publication_audit[-1]["detail"]["recovery"] == (
                    "program_migration_from_preparation" if fault == "retry-checkpoint"
                    else "program_migration_before_preparation"
                )
                from turnstile_core.persistence.repository_publications import (
                    PostgreSqlPublicationRepositoryMixin,
                )

                statements: list[tuple[str, Any]] = []

                def execute_sql(statement: str, parameters: Any) -> Mock:
                    sql = " ".join(statement.split())
                    statements.append((sql, parameters))
                    if sql.startswith("SELECT publication.*"):
                        return Mock(fetchone=lambda: before)
                    if sql.startswith("SELECT count(*)"):
                        return Mock(fetchone=lambda: {"total": 0})
                    if sql.startswith("SELECT id, apim_target_id"):
                        return Mock(fetchall=lambda: [repository.gateway_deployments[
                            serving.deployment_id
                        ]])
                    if sql.startswith("SELECT * FROM gateway_publication_outbox"):
                        return Mock(fetchone=lambda: preserved_outbox)
                    if sql.startswith("SELECT id FROM gateway_publication"):
                        return Mock(fetchone=lambda: None)
                    if sql.startswith("UPDATE gateway_publication SET"):
                        return Mock(fetchone=lambda: queued_row)
                    return Mock()

                connection = Mock(execute=execute_sql)
                sql_result = PostgreSqlPublicationRepositoryMixin._resume_program_migration(
                    connection, preserved_row, "unit-recovery",
                )
                assert sql_result["id"] == queued.id
                updates = [sql for sql, _ in statements if sql.startswith("UPDATE")]
                assert len(updates) == 2
                assert all(not any(field in sql for field in (
                    "attempt_count", "attempts =", "attempts=", "resource_manifest", "desired_spec",
                    "started_at", "created_by",
                )) for sql in updates)
                assert any("FOR UPDATE OF effective, publication" in sql for sql, _ in statements)
                assert any("detail ? 'probe_request'" in sql for sql, _ in statements)
                assert statements[-1][1][2].obj["outbox_attempts"] == 3
            else:
                with pytest.raises(ValueError):
                    if fault == "retry-spec":
                        repository.requeue_gateway_publication(
                            queued.id, "unit-recovery", None, queued.desired_spec.model_dump(),
                            queued.desired_spec_sha256,
                        )
                    else:
                        service.retry(queued.id, GatewayPublicationRetry(
                            api_key=SecretStr("unit-new-secret") if fault == "retry-credential"
                            else None,
                        ), "unit-recovery")
                assert queued_row == preserved_row and outbox == preserved_outbox
                assert repository.gateway_publication_audit == prior_audit
            assert original_row == before
            return
        queued_row["status"] = "promoting"
        repository.gateway_publication_outbox[0].update({
            "status": "leased", "lease_owner": "unit-worker",
            "lease_expires_at": datetime.now(UTC) + timedelta(seconds=60),
        })
        with pytest.raises(ValueError, match="migration.*confirmation"):
            repository.activate_gateway_publication(queued.id, "unit-worker")
        assert repository.effective_gateway_releases[target] == previous.id
        assert original_row == before


@pytest.mark.parametrize("fault", [
    None, "before", "persist", "unknown", "unknown-applied", "unconfirmed", "foreign-intent",
])
def test_program_promotion_records_before_send_and_never_replays(fault: str | None) -> None:
    from turnstile_core.services.storage_route_publisher import promote_program_once

    management = Mock()
    current = "foreign" if fault == "before" else "previous"
    recorded: dict[str, Any] | None = None
    writes = 0

    def persist(expected: Any, candidate: Any) -> None:
        nonlocal recorded
        assert expected == recorded
        if fault == "persist":
            raise ValueError("UNIT_LEASE_CHANGED")
        recorded = dict(candidate)

    def promote(revision: str, release: str, previous: str) -> None:
        nonlocal current, writes
        assert recorded is not None and recorded["outcome"] == "started"
        assert (revision, release, previous) == ("candidate", "unit-release", "previous")
        writes += 1
        if fault not in {"unknown", "unconfirmed"}:
            current = revision
        if fault in {"unknown", "unknown-applied"}:
            raise httpx.ReadTimeout("UNIT_UNKNOWN")

    management.current_revision.side_effect = lambda: current
    management.promote_fixed_revision.side_effect = promote
    verify = Mock()
    if fault == "foreign-intent":
        recorded = {"outcome": "started", "context_sha256": "f" * 64}
    options = (management, "previous", "candidate", "unit-release", "a" * 64)
    if fault:
        with pytest.raises((ValueError, httpx.ReadTimeout, RuntimeError)):
            promote_program_once(*options, recorded, persist, verify)
    else:
        promote_program_once(*options, recorded, persist, verify)
    if fault in {None, "unknown-applied"}:
        promote_program_once(*options, recorded, persist, verify)
        assert recorded is not None and recorded["outcome"] == "confirmed"
    elif fault in {"unknown", "unconfirmed"}:
        with pytest.raises(RuntimeError):
            promote_program_once(*options, recorded, persist, verify)
    assert writes == (0 if fault in {"before", "persist", "foreign-intent"} else 1)


@pytest.mark.parametrize("fault", [None, "fresh", "verification", "target-drift", "disabled-first"])
def test_program_renewal_factory_checks_installed_targets_without_model_publication(
    fault: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    from turnstile_core.services.storage_route_publisher import (
        FixedProgramInstallation,
        FixedRoutePublisherFactory,
    )

    _, program, proof = snapshot_fixture()
    target_id, publication_id = UUID(program.scope.target_id), UUID(int=510)
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm",
        publisher_subscription_id="unit-publisher", parent_policy_sha256="a" * 64,
        operation_policies_sha256={"chat-completions": "c" * 64},
        operation_contracts={"chat-completions": ("POST", "/chat/completions")},
        supported_profiles=("openai-v1",),
    )
    repository, port = Mock(), Mock()
    repository.list_apim_targets.return_value = [
        {"gateway_profile_id": target_id}, {"gateway_profile_id": UUID(int=511)},
    ]
    if fault == "disabled-first":
        repository.list_apim_targets.return_value.insert(0, {"gateway_profile_id": UUID(int=513)})
    active = {"id": publication_id, "gateway_profile_id": target_id, "status": "active",
              "resource_manifest": {"fixed_route_program": installation.model_dump(mode="json")}}
    repository.effective_gateway_publication.side_effect = lambda target: (
        active if target == target_id else {"resource_manifest": {}}
    )
    repository.get_gateway_profiles.return_value = []
    if fault == "disabled-first":
        repository.get_gateway_profiles.side_effect = lambda identities: (
            [{"id": UUID(int=513), "enabled": False}] if identities == [UUID(int=513)] else []
        )
    repository.model_availability_proof_guard.return_value = nullcontext()
    current = proof.model_copy(update={"issued_epoch": 1000, "expires_epoch": 1120})
    if fault == "fresh":
        current = proof
    fresh = proof.model_copy(update={"issued_epoch": 1100, "expires_epoch": 1220})
    port.projection.read_program_proof.return_value = (current, 'W/"proof"')
    port._verifier.verify.return_value = fresh
    if fault == "verification":
        port._verifier.verify.side_effect = ValueError("UNIT_PROGRAM_DRIFT")
    factory = FixedRoutePublisherFactory(
        repository, Mock(), active_fence=lambda _: {}, availability_projector=Mock(),
        clock=lambda: 1100,
    )
    resolved = Mock(return_value=(installation, Mock()))
    if fault == "target-drift":
        resolved.side_effect = ValueError("UNIT_TARGET_DRIFT")
    monkeypatch.setattr(factory, "installed", resolved)
    monkeypatch.setattr(factory, "_open", Mock(return_value=nullcontext((port, Mock()))))
    if fault in {"verification", "target-drift"}:
        with pytest.raises(ValueError):
            factory.renew_program_proofs()
        port.projection.publish_program_proof.assert_not_called()
    else:
        factory.renew_program_proofs()
        assert port.projection.publish_program_proof.call_count == (0 if fault == "fresh" else 1)
        if fault != "fresh":
            port.projection.publish_program_proof.assert_called_once_with(
                fresh, program, 'W/"proof"', 1100,
            )
    resolved.assert_called_once_with(target_id, publication_id)
    if fault == "disabled-first":
        assert all(call.args != (UUID(int=513),)
                   for call in repository.effective_gateway_publication.call_args_list)
    repository.activate_gateway_publication.assert_not_called()
    port.probe_changed.assert_not_called()
    port.projection.activate.assert_not_called()


@pytest.mark.parametrize("fault", ["busy", "unavailable", "network"])
def test_busy_program_target_does_not_starve_the_next_target(
    monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    from contextlib import contextmanager, nullcontext
    from types import SimpleNamespace

    from turnstile_core.domain.model_availability import ModelAvailabilityUnavailable
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherFactory

    _, program, proof = snapshot_fixture()
    first, second = UUID(int=511), UUID(program.scope.target_id)
    repository, port = Mock(), Mock()
    repository.list_apim_targets.return_value = [{"gateway_profile_id": identity}
                                                for identity in (first, second)]
    repository.get_gateway_profiles.side_effect = lambda identities: [
        {"id": identity, "enabled": True} for identity in identities
    ]
    repository.effective_gateway_publication.side_effect = lambda identity: {
        "id": UUID(int=512), "gateway_profile_id": identity, "status": "active",
        "resource_manifest": {"fixed_route_program": {}},
    }

    @contextmanager
    def guard(identity: UUID, *, wait: bool) -> Any:
        assert wait is False
        if identity == first:
            if fault == "network":
                raise httpx.ConnectTimeout("Isolated target network failure")
            raise ModelAvailabilityUnavailable("Availability proof writer is busy"
                                              if fault == "busy" else "Unavailable identity")
        yield

    repository.model_availability_proof_guard.side_effect = guard
    port.projection.read_program_proof.return_value = None
    port._verifier.verify.return_value = proof
    factory = FixedRoutePublisherFactory(repository, Mock(), active_fence=Mock(),
                                        availability_projector=Mock(), clock=lambda: 1100)
    resolver = Mock(return_value=(
        SimpleNamespace(program=program, native_gateway_scope=False), Mock(),
    ))
    monkeypatch.setattr(factory, "installed", resolver)
    monkeypatch.setattr(factory, "_open", Mock(return_value=nullcontext((port, Mock()))))
    if fault == "unavailable":
        with pytest.raises(ModelAvailabilityUnavailable, match="Unavailable identity"):
            factory.renew_program_proofs()
        resolver.assert_not_called()
        port.projection.publish_program_proof.assert_not_called()
    elif fault == "network":
        with pytest.raises(httpx.ConnectTimeout, match="Isolated target network failure"):
            factory.renew_program_proofs()
        resolver.assert_called_once_with(second, UUID(int=512))
        port.projection.publish_program_proof.assert_called_once_with(proof, program, None, 1100)
    else:
        factory.renew_program_proofs()
        resolver.assert_called_once_with(second, UUID(int=512))
        port.projection.publish_program_proof.assert_called_once_with(proof, program, None, 1100)


@pytest.mark.parametrize("fault", [None, "foreign", "verification", "changed-history"])
def test_native_program_renewal_handoffs_only_registered_predecessors(
    fault: str | None,
) -> None:
    from turnstile_core.domain.storage_routes import FixedProgramInstallation, FixedProgramUpgrade
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherFactory

    _, identity, proof = snapshot_fixture()
    previous = FixedProgramInstallation(
        program=identity, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm", publisher_subscription_id="unit",
        parent_policy_sha256="a" * 64, operation_policies_sha256={"models": "c" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
        native_gateway_scope=True,
    )
    installed = previous.model_copy(update={
        "program": identity.model_copy(update={"program_sha256": "e" * 64}),
        "parent_policy_sha256": "f" * 64, "model_additions_enabled": True,
        "legacy_program_sha256": identity.program_sha256,
    })
    publication_id = str(UUID(int=900))
    upgrade = FixedProgramUpgrade(
        previous_publication_id=publication_id,
        previous_installation_sha256=document_digest(previous), installation=installed,
    )
    config = {"fixed_route_program_upgrade": upgrade.model_dump(mode="json")}
    if fault == "changed-history":
        config["fixed_route_program_upgrade"]["previous_installation_sha256"] = "0" * 64
    repository, projection, verifier = Mock(), Mock(), Mock()
    repository.effective_gateway_publication.return_value = {
        "id": publication_id,
        "resource_manifest": {"fixed_route_program": previous.model_dump(mode="json")},
    }
    repository.get_gateway_profiles.return_value = [
        {"id": UUID(identity.scope.target_id), "config": config},
    ]
    node = installed.for_native_gateway("unit-native", "unit-instance", "https://native.example.test/llm")
    old_node = previous.for_native_gateway("unit-native", "unit-instance", "https://native.example.test/llm")
    old_proof = proof.model_copy(update={"identity": old_node.program})
    fresh = proof.model_copy(update={"identity": node.program})

    def read(program: Any) -> Any:
        if program == old_node.program and fault != "foreign":
            return old_proof, 'W/"old-native"'
        raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_IDENTITY_CHANGED")

    projection.read_program_proof.side_effect = read
    verifier.verify.return_value = fresh
    if fault == "verification":
        verifier.verify.side_effect = ValueError("UNIT_NATIVE_PROGRAM_DRIFT")
    factory = FixedRoutePublisherFactory(repository, Mock(), active_fence=Mock(),
                                        availability_projector=Mock(), clock=lambda: 1100)
    port = Mock(projection=projection, _verifier=verifier)
    if fault:
        with pytest.raises(ValueError):
            factory._renew_native_program_proof(installed, node, port)
        projection.publish_upgraded_program_proof.assert_not_called()
    else:
        factory._renew_native_program_proof(installed, node, port)
        call = projection.publish_upgraded_program_proof.call_args
        assert call.args[1:] == (old_node, fresh, 'W/"old-native"', 1100)
        call.args[0].require_previous(publication_id, old_node)
    repository.record_fixed_program_upgrade.assert_not_called()
    projection.activate.assert_not_called()


def test_proof_expiring_during_journal_commit_prevents_dispatch() -> None:
    snapshot, identity, proof = snapshot_fixture()
    instant = 1100
    writes = []

    class SlowJournal(Journal):
        def begin_once(self, intent: RouteActivationIntent) -> None:
            nonlocal instant
            super().begin_once(intent)
            instant = 1170

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            writes.append(request)
            pytest.fail("expired program proof dispatched")
        if any(value in str(request.url) for value in ("active", "transition", "writer")):
            return httpx.Response(404)
        scope = snapshot.approval.scope
        if ":catalog" in unquote(str(request.url)):
            payload = route_entity(
                scope,
                catalog_partition(scope, snapshot.approval.route_version),
                member_row(snapshot.routes[0].alias),
                canonical_document(catalog_entry(snapshot.routes[0])),
            )
        elif snapshot.routes[0].sha256 in str(request.url):
            payload = route_entity(
                scope,
                content_partition(scope),
                snapshot.routes[0].sha256,
                canonical_document(snapshot.routes[0]),
            )
        else:
            payload = route_entity(
                scope,
                member_partition(scope, snapshot.approval.route_version),
                member_row(snapshot.routes[0].alias),
                canonical_document(snapshot.members[0]),
            )
        return httpx.Response(200, json=payload, headers={"ETag": 'W/"one"'})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        store = TableRouteProjection(
            "https://unitstore.table.core.windows.net",
            "Routes",
            snapshot.approval.scope,
            UnitToken(),
            client,
        )
        journal = SlowJournal()
        with pytest.raises(ValueError, match="BINDING_INVALID"):
            store.activate(
                snapshot,
                identity,
                proof,
                snapshot.approval.budget_epoch,
                str(UUID(int=23)),
                None,
                lambda: instant,
                journal,
            )
        assert journal.intent is not None and not writes
        assert journal.outcomes == ["expired_before_dispatch"]


@pytest.mark.parametrize("fault", [None, "program", "expired", "scope"])
def test_configuration_plan_is_explicitly_policy_free(fault: str | None) -> None:
    from turnstile_core.services.control_plane import prepare_route_configuration

    snapshot, program, proof = snapshot_fixture()
    previous = list(snapshot.routes)
    if fault == "program":
        program = program.model_copy(update={"program_sha256": "f" * 64})
    elif fault == "expired":
        proof = proof.model_copy(update={"expires_epoch": 1099})
    elif fault == "scope":
        foreign_scope = snapshot.approval.scope.model_copy(update={"api_id": "foreign-api"})
        previous = [
            snapshot.routes[0].model_copy(
                update={
                    "scope": foreign_scope,
                    "connection": snapshot.routes[0].connection.model_copy(
                        update={"scope": foreign_scope}
                    ),
                }
            )
        ]
    if fault:
        with pytest.raises(ValueError):
            prepare_route_configuration(
                previous, snapshot, program, proof, snapshot.approval.budget_epoch, 1100
            )
    else:
        plan = prepare_route_configuration(
            previous, snapshot, program, proof, snapshot.approval.budget_epoch, 1100
        )
        assert plan.kind == "configuration" and plan.policy_write_count == 0
        assert plan.changed_aliases == () and plan.removed_aliases == ()
