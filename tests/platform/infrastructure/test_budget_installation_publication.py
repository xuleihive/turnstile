from __future__ import annotations

import copy
import hashlib
import re
from datetime import timedelta
from typing import Any, cast
from unittest.mock import MagicMock, Mock
from uuid import UUID, uuid5
from xml.etree import ElementTree

import httpx
import pytest
from pydantic import SecretStr

from backend.services.client_user_policy import strip_verified_client_user_policy
from scripts.budget_execution_upgrade import plan_budget_parent_upgrade
from scripts.budget_installation_publication import (
    InstallationProtocolVerification,
    InstallationPublicationBundle,
    InstallationPublicationReadback,
    build_historical_availability,
    build_installation_publication,
    capture_installation_contracts,
    capture_serving_baseline_lineage,
    promote_installation_publication,
    record_promoted_installation,
    recover_installation_promotion,
)
from scripts.install_budget_execution import BudgetInstallationPlan, InstallationJournal
from scripts.prepare_budget_execution import FenceConfiguration
from tests.backend.model_platform.control_plane_support import (
    GatewayControlPlaneService,
    StubTokenProvider,
    bedrock_publication,
    publisher_settings,
)
from tests.backend.model_platform.test_image_publication import image_publication
from tests.platform.infrastructure.test_budget_installation import EPOCH, NOW, observation, plan
from tests.platform.infrastructure.test_budget_parent_upgrade import ROOT, TARGET, legacy_parent
from turnstile_core.domain.budget_counting import BudgetPublicationContracts
from turnstile_core.domain.control_plane import PublicationStatus
from turnstile_core.domain.model_availability import (
    ModelAvailabilityPolicyAdoption,
    ModelAvailabilityPublication,
    ModelAvailabilityUnavailable,
)
from turnstile_core.integrations.apim_control_plane_contract import (
    OperationResource,
    ResolvedApimPublisherClient,
)
from turnstile_core.integrations.apim_directory_policy import compose_directory_identity_policy
from turnstile_core.integrations.apim_policy_compiler import ApimPolicyCompiler
from turnstile_core.integrations.apim_policy_components import (
    component_digest,
    parse_policy,
    policy_document_digest,
)
from turnstile_core.integrations.apim_publisher_client import AzureApimPublisherClient
from turnstile_core.integrations.model_availability_verifier import ModelAvailabilityGatewayVerifier
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository_publications import PostgreSqlPublicationRepositoryMixin
from turnstile_core.services.gateway_publication_worker import GatewayPublicationWorker

MODEL = UUID(int=22)
RUNTIME = UUID(int=23)


@pytest.fixture
def prepared() -> tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]]:
    previous = GatewayControlPlaneService(InMemoryRepository()).seed_legacy_publication(
        bedrock_publication(), "unit",
    )
    spec = previous.desired_spec.model_copy(deep=True)
    alias = spec.bindings[0].model.model_key
    spec.discovery_models = [item for item in spec.discovery_models if item.id == alias]
    spec.bindings[0].logical_model_id = MODEL
    spec.bindings[0].runtime_id = RUNTIME
    previous = previous.model_copy(update={
        "id": UUID(int=24), "status": PublicationStatus.ACTIVE, "apim_revision": "old",
        "desired_spec": spec, "desired_spec_sha256": "8" * 64, "generation": 9,
        "policy_sha256": "9" * 64, "created_at": NOW - timedelta(days=1),
        "updated_at": NOW - timedelta(days=1), "completed_at": NOW - timedelta(days=1),
    })
    configuration = plan().expected_fence.model_dump()
    configuration.update({
        "gateway_profile_id": TARGET,
        "model_contracts": {alias: {
            "model_id": MODEL, "input_ceiling": 10000, "output_ceiling": 1000,
            "output_field": "max_tokens", "operations": ["anthropic-messages"],
            "evidence_sha256": "7" * 64, "verified_at": NOW - timedelta(hours=1),
        }},
    })
    fence = FenceConfiguration.model_validate(configuration)
    template, source = legacy_parent()
    assert fence.delegation is not None
    upgraded = plan_budget_parent_upgrade(
        source, legacy_template=template,
        canonical_parent=(ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
        delegation=fence.delegation, gateway_profile_id=TARGET,
    )
    registry: dict[str, Any] = {"models": [{
        "id": MODEL, "model_key": alias, "runtime_id": RUNTIME,
    }]}
    bundle = build_installation_publication(
        previous, registry=registry, fence=fence, parent_policy=upgraded.candidate_parent,
        bindings=upgraded.adopted_bindings, compiler=ApimPolicyCompiler(), revision="candidate",
        generation=10, actor="unit", created_at=NOW - timedelta(minutes=15),
    )
    installation_plan = plan().model_copy(update={
        "gateway_profile_ids": [TARGET], "expected_fence": fence,
        "policy_sha256": {"old": "3" * 64, "candidate": hashlib.sha256(
            bundle.parent_policy.encode(),
        ).hexdigest()},
    })
    return bundle, installation_plan, registry


def verification(bundle: InstallationPublicationBundle) -> InstallationProtocolVerification:
    base = {"gateway_profile_id": TARGET, "native_gateway_id": "managed",
            "revision": "candidate", "checked_at": NOW, "ready": True}
    return InstallationProtocolVerification.model_validate({
        "bundle_sha256": bundle.digest,
        "identity": {**base, "protocol_mode": "prepared", "checks": [
            {"case": case, "expected_status": status, "actual_status": status}
            for case, status in (("valid_identity", 200), ("wrong_audience", 401),
                                 ("missing_person", 403))
        ]},
        "paused": {**base, "protocol_mode": "paused", "checks": [
            {"case": case, "expected_status": 503, "actual_status": 503,
             "native_gateway_id": "managed", "expected_error_code": "budget_protocol_fenced",
             "actual_error_code": "budget_protocol_fenced"}
            for case in ("current_fenced", "candidate_fenced")
        ]},
    })


def observed(
    bundle: InstallationPublicationBundle, installation_plan: BudgetInstallationPlan,
    *, promoted: bool = False,
) -> InstallationPublicationReadback:
    maintenance = observation().model_copy(update={
        "fence": installation_plan.expected_fence,
        "revision_policy_sha256": installation_plan.policy_sha256,
    })
    surfaces = tuple(f"managed/candidate/{key}" for key, (resource, _) in bundle.operations.items()
                     if resource.method != "HEAD")
    return InstallationPublicationReadback.model_validate({
        "observed_at": NOW, "maintenance": maintenance, "publisher_disabled": True,
        "current_revision": "candidate" if promoted else "old",
        "parent_policy": bundle.parent_policy,
        "operations": {key: (resource.method, resource.path, source)
                       for key, (resource, source) in bundle.operations.items()},
        "dependencies": {
            "apim_revision": "candidate", "compiled_policy_sha256": bundle.compiled.policy_sha256,
            "parent_policy_sha256": bundle.publication.resource_manifest["parent_policy_sha256"],
            "backends": [value.id for value in bundle.compiled.backends],
            "named_values": [value.id for value in bundle.compiled.named_values],
            "recorded_complete": True, "live_status": "healthy",
        },
        "projected_models": {MODEL: {"model_id": MODEL, "revision": 1, "enabled": True}},
        "availability_inventory_sha256": "5" * 64,
        "availability": {"target_id": TARGET, "inventory_digest": "5" * 64,
                         "policy_digest": "6" * 64, "serving_surfaces": surfaces,
                         "verified_surfaces": surfaces, "verified_at": NOW,
                         "expires_at": NOW + timedelta(seconds=300)},
    })


def test_installation_compiles_complete_budget_and_availability_protocol(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
) -> None:
    bundle, installation_plan, _ = prepared
    assert bundle.publication.id == uuid5(EPOCH, f"legacy-budget-publication:{TARGET}")
    assert bundle.publication.status == PublicationStatus.VERIFYING
    assert bundle.publication.desired_spec == bundle.previous.desired_spec
    assert installation_plan.expected_fence.mode == "paused"
    root = parse_policy(bundle.parent_policy)
    assert root.find(".//set-variable[@name='budgetExecutionVersion']") is not None
    assert root.find(".//set-variable[@name='availabilityProtocolVersion']") is not None
    assert len(bundle.operations) == 12
    assert bundle.operations["responses"][0].path == "/responses"
    assert bundle.operations["availability-probe-responses"][0].method == "HEAD"
    observed(bundle, installation_plan).require_candidate(
        installation_plan, bundle, promoted=False, now=NOW,
    )
    verification(bundle).require_verified(bundle, installation_plan.expected_fence, NOW)


def test_installed_snapshots_are_reusable_by_the_normal_publisher(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
) -> None:
    bundle, installation_plan, registry = prepared
    installed = bundle.publication.model_copy(update={"status": PublicationStatus.ACTIVE})
    next_publication = installed.model_copy(deep=True, update={
        "id": UUID(int=99), "base_release_id": installed.id,
        "generation": 11, "apim_revision": "next", "status": PublicationStatus.QUEUED,
    })
    actual_active = installation_plan.expected_fence.model_dump(mode="json")
    actual_active["mode"] = "active"
    snapshot = BudgetPublicationContracts.capture(
        next_publication, registry, actual_active, installed,
    )
    snapshot.validate_publication(next_publication)
    assert snapshot.model_contracts == installation_plan.expected_fence.model_contracts
    available = ModelAvailabilityPublication.capture(next_publication, registry, "unit-api")
    available.validate_publication(next_publication)
    with pytest.raises(ValueError, match="active Gateway fence"):
        BudgetPublicationContracts.capture(
            next_publication, registry, installation_plan.expected_fence.model_dump(mode="json"),
            installed,
        )


def test_normal_publication_worker_can_queue_after_installation(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
) -> None:
    bundle, installation_plan, registry = prepared
    installed = bundle.publication.model_copy(update={"status": PublicationStatus.ACTIVE})
    next_publication = installed.model_copy(deep=True, update={
        "id": UUID(int=99), "base_release_id": installed.id,
        "generation": 11, "apim_revision": None, "status": PublicationStatus.QUEUED,
        "policy_sha256": None, "resource_manifest": {},
    })
    repository, client = Mock(), Mock()
    repository.registry.return_value = registry
    repository.get_apim_target.return_value = {"api_id": "unit-api"}
    repository.get_gateway_publication.return_value = installed.model_dump()
    repository.effective_gateway_publication.return_value = installed.model_dump()
    repository.model_availability_supported.return_value = True
    repository.transition_gateway_publication.side_effect = (
        lambda identity, source, target, updates, actor: next_publication.model_copy(
            update={**updates, "status": PublicationStatus(target)},
        ).model_dump()
    )
    client.current_api_policy.return_value = ("candidate", bundle.parent_policy)
    live_fence = installation_plan.expected_fence.model_dump(mode="json")
    live_fence["mode"] = "active"
    worker = GatewayPublicationWorker(
        repository, client, bundle.parent_policy, budget_configuration=lambda target: live_fence,
        availability_projector=Mock(),
    )
    result = worker._advance(next_publication, "unit-worker", ResolvedApimPublisherClient(
        TARGET, "7" * 64, True, client,
    ))
    assert result.status == PublicationStatus.VALIDATING
    assert "budget_contracts" in result.resource_manifest
    assert "model_availability" in result.resource_manifest
    client.verify_publication_permissions.assert_called_once()


def with_serving_lineage(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
) -> tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]]:
    bundle, installation_plan, registry = copy.deepcopy(prepared)
    previous = bundle.previous
    compiled = ApimPolicyCompiler().compile(previous)
    previous.resource_manifest = {
        "backends": [item.id for item in compiled.backends],
        "named_values": [item.id for item in compiled.named_values],
    }
    raw = {
        "chat-completions": compiled.chat_completions_policy,
        "responses": compiled.responses_policy,
        "responses-compact": compiled.responses_compact_policy,
        "anthropic-messages": compiled.messages_policy,
        "anthropic-count-tokens": compiled.count_tokens_policy,
        "anthropic-models": compiled.models_policy,
    }
    operations = {key: (bundle.operations[key][0], source, '"original-policy"')
                  for key, source in raw.items()}
    _, original_parent = legacy_parent()
    actual_parent = original_parent.replace("unit-logger", "manual-parent-logger")
    dependencies: dict[str, dict[str, Any]] = {
        "backends/" + item.id: {"url": item.url} for item in compiled.backends
    }
    dependencies.update({"namedValues/" + item.id: {"secret": True}
                         for item in compiled.named_values})
    dependencies.update({"namedValues/" + name: {"secret": True}
                         for document in (original_parent, actual_parent, *raw.values())
                         for name in re.findall(
                             r"\{\{([A-Za-z0-9][A-Za-z0-9._-]{0,126})\}\}", document,
                         )})
    lineage = capture_serving_baseline_lineage(
        previous, serving_revision="manual-parent-revision",
        sql_parent=(original_parent, '"sql-revision-parent"'),
        serving_parent=(actual_parent, '"actual-serving-parent"'),
        sql_operations=operations, serving_operations=operations,
        shared_dependencies=dependencies, source_evidence_sha256="1" * 64,
        observed_at=NOW - timedelta(minutes=2), reviewed_at=NOW - timedelta(minutes=1),
        reviewed_by="unit-operator",
    )
    assert installation_plan.expected_fence.delegation is not None
    upgrade = plan_budget_parent_upgrade(
        actual_parent,
        legacy_template=(ROOT / "infra/policies/foundry-finops-policy.schema1.xml").read_text(),
        canonical_parent=(ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
        delegation=installation_plan.expected_fence.delegation, gateway_profile_id=TARGET,
    )
    rebuilt = build_installation_publication(
        previous, registry=registry, fence=installation_plan.expected_fence,
        parent_policy=upgrade.candidate_parent, bindings=upgrade.adopted_bindings,
        compiler=ApimPolicyCompiler(),
        revision="candidate", generation=10, actor="unit", created_at=NOW,
        serving_lineage=lineage, source_parent_policy=actual_parent,
    )
    installation_plan.policy_sha256["candidate"] = hashlib.sha256(
        rebuilt.parent_policy.encode(),
    ).hexdigest()
    return rebuilt, installation_plan, registry


def test_manual_parent_lineage_preserves_the_original_sql_record(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
) -> None:
    bundle, installation_plan, _ = with_serving_lineage(prepared)
    assert bundle.previous.apim_revision == "old"
    assert bundle.serving_revision == "manual-parent-revision"
    assert bundle.serving_lineage is not None
    assert bundle.serving_lineage.sql_policy_digest_comparison == "not_asserted"
    assert bundle.serving_lineage.sql_recorded_policy_sha256 == bundle.previous.policy_sha256
    actual = observed(bundle, installation_plan)
    actual.current_revision = "manual-parent-revision"
    actual.require_candidate(installation_plan, bundle, promoted=False, now=NOW)
    actual.current_revision = "old"
    with pytest.raises(ValueError, match="current revision"):
        actual.require_candidate(installation_plan, bundle, promoted=False, now=NOW)


def test_unproven_manual_revision_is_still_rejected(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
) -> None:
    bundle, installation_plan, _ = prepared
    actual = observed(bundle, installation_plan)
    actual.current_revision = "manual-parent-revision"
    with pytest.raises(ValueError, match="current revision"):
        actual.require_candidate(installation_plan, bundle, promoted=False, now=NOW)


@pytest.mark.parametrize("defect", [
    "sql_revision", "sql_spec", "sql_digest", "manifest", "missing_operation", "operation",
    "missing_dependency", "wildcard_parent", "wildcard_operation", "stale",
])
def test_lineage_cannot_bless_operation_changes_or_replace_sql_history(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
    defect: str,
) -> None:
    bundle, _, _ = with_serving_lineage(prepared)
    proof = bundle.serving_lineage
    assert proof is not None
    if defect == "sql_revision":
        proof.sql_revision = "not-sql-record"
    elif defect == "sql_spec":
        proof.desired_spec_sha256 = "f" * 64
    elif defect == "sql_digest":
        proof.sql_recorded_policy_sha256 = "f" * 64
    elif defect == "manifest":
        proof.sql_manifest_sha256 = "f" * 64
    elif defect == "missing_operation":
        del proof.sql_operations["responses"]
    elif defect == "operation":
        proof.serving_operations["responses"].path = "/changed-route"
    elif defect == "missing_dependency":
        proof.shared_dependency_sha256 = {}
    elif defect == "wildcard_parent":
        proof.serving_parent.etag = "*"
    elif defect == "wildcard_operation":
        proof.sql_operations["responses"].policy.etag = "*"
    else:
        proof.observed_at = NOW - timedelta(days=2)
    with pytest.raises(ValueError):
        proof.require_previous(bundle.previous, NOW)


def test_image_bundle_has_a_complete_manifest_for_the_real_dependency_inspector(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
) -> None:
    text_bundle, installation_plan, registry = copy.deepcopy(prepared)
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(
        repository, image_generation_enabled=True, apim_principal_id="unit-principal",
    )
    image = service.seed_legacy_publication(
        image_publication(repository), "unit",
    )
    binding = image.desired_spec.bindings[-1].model_copy(update={
        "logical_model_id": MODEL, "runtime_id": RUNTIME,
    })
    alias = binding.model.model_key
    previous = text_bundle.previous.model_copy(deep=True)
    previous.desired_spec = previous.desired_spec.model_copy(update={
        "bindings": [binding], "discovery_models": [
            item for item in image.desired_spec.discovery_models if item.id == alias
        ],
    })
    fence = installation_plan.expected_fence.model_copy(deep=True)
    fence.model_contracts = {alias: BudgetPublicationContracts._estimate(
        previous, binding, MODEL, BudgetPublicationContracts.binding_digest(binding),
    )}
    registry["models"][0]["model_key"] = alias
    bundle = build_installation_publication(
        previous, registry=registry, fence=fence, parent_policy=text_bundle.parent_policy,
        bindings={}, compiler=ApimPolicyCompiler(), revision="candidate", generation=10,
        actor="unit", created_at=NOW,
    )
    assert len(bundle.operations) == 14
    assert (
        bundle.publication.resource_manifest["images_generations_operation"] == "images-generations"
    )
    backends = {backend.id: backend for backend in bundle.compiled.backends}
    values = {value.id: value for value in bundle.compiled.named_values}
    visited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        visited.append(path)
        if "/backends/" in path:
            backend = backends[path.rsplit("/", 1)[-1]]
            return httpx.Response(200, json={"properties": {
                "type": "Single", "url": backend.url,
                "tls": {"validateCertificateChain": True, "validateCertificateName": True},
                **({"credentials": {"header": {
                    name: [value] for name, value in backend.headers
                }}} if backend.headers else {}),
            }})
        if "/namedValues/" in path:
            named = values[path.rsplit("/", 1)[-1]]
            return httpx.Response(200, json={"properties": {
                "secret": True,
                **({"keyVault": {"secretIdentifier": named.key_vault_secret_id}}
                   if named.key_vault_secret_id else {}),
            }})
        if path.endswith("/policies/policy"):
            key = next((key for key in bundle.operations if f"/operations/{key}/" in path), None)
            source = bundle.operations[key][1] if key else bundle.parent_policy
            return httpx.Response(200, json={"properties": {"value": source}})
        return httpx.Response(200, json={"properties": {"provisioningState": "Succeeded"}})

    client = AzureApimPublisherClient(
        publisher_settings(), StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.inspect_revision_dependencies(bundle.publication)
    assert result.live_status == "healthy", result.issues
    assert any("/operations/images-generations/" in path for path in visited)
    missing = bundle.publication.model_copy(deep=True)
    del missing.resource_manifest["images_generations_operation"]
    rejected = client.inspect_revision_dependencies(missing)
    assert "mismatched_compiled_policy_sha256" in rejected.issues


@pytest.mark.parametrize("defect", [None, "old_parent", "old_head"])
@pytest.mark.parametrize("overlay", ["client", "directory"])
def test_historical_artifacts_pass_the_full_revision_verifier_only_with_complete_coverage(
    defect: str | None, overlay: str,
) -> None:
    _, raw_parent = legacy_parent()
    if overlay == "directory":
        raw_parent = compose_directory_identity_policy(
            strip_verified_client_user_policy(raw_parent), cloud="public",
            endpoint="https://unit.table.core.windows.net", table="UnitLedger",
        )
    raw_inference = (
        '<policies><inbound><base/><set-backend-service backend-id="unit"/></inbound>'
        '<backend><base/></backend><outbound><base/></outbound><on-error><base/></on-error>'
        '</policies>'
    )
    raw_catalog = (
        '<policies><inbound><base/><return-response><set-status code="200" reason="OK"/>'
        '<set-body>{"data":[{"id":"unit-model"}]}</set-body></return-response></inbound>'
        '<backend><base/></backend><outbound><base/></outbound><on-error><base/></on-error>'
        '</policies>'
    )
    policies: dict[str, str] = {}
    resources: dict[str, list[dict[str, Any]]] = {
        "/apis/unit-api/products": [],
        "/apis/unit-api/revisions": [{"apiRevision": revision} for revision in ("old1", "old2")],
    }
    for revision in ("old1", "old2"):
        adoption = ModelAvailabilityPolicyAdoption(
            adoption_id=UUID(int=31), target_id=TARGET, api_id="unit-api", revision=revision,
            models={"unit-model": MODEL}, serving_gateways={"unit-model": None},
            original_policy_digests={key: hashlib.sha256(value.encode()).hexdigest()
                                     for key, value in {"parent": raw_parent,
                                                        "responses": raw_inference,
                                                        "models": raw_catalog}.items()},
        )
        operations = {
            "responses": (OperationResource("responses", "Responses", "POST", "/responses"),
                          raw_inference, '"r1"'),
            "models": (OperationResource("models", "Models", "GET", "/models"),
                       raw_catalog, '"m1"'),
        }
        with pytest.raises(ValueError, match="explicit installation approval"):
            build_historical_availability(adoption, (raw_parent, '"p1"'), operations, bindings={})
        historical = build_historical_availability(
            adoption, (raw_parent, '"p1"'), operations, bindings={
                "__GATEWAY_PROFILE_ID__": str(TARGET),
                "__LEDGER_TABLE_ENDPOINT__": "https://unit.table.core.windows.net",
                "__LEDGER_TABLE_NAME__": "UnitLedger",
            }, allow_verified_client_overlay_removal=True,
            allow_verified_directory_overlay_removal=True,
        )
        prefix = f"/apis/unit-api;rev={revision}"
        resources[prefix + "/operations"] = [
            {"name": key, "properties": {"method": value[0].method,
                                         "urlTemplate": value[0].path}}
            for key, value in operations.items()
        ]
        for change in historical.changes:
            if change.path.endswith("/policies/policy"):
                policies[change.path] = change.body["properties"]["value"]
            else:
                resources[prefix + "/operations"].append({
                    "name": change.path.rsplit("/", 1)[-1], "properties": change.body["properties"],
                })
        assert len(historical.desired_policy_sha256) == 5
        assert len(historical.changes) == 7
        assert historical.removed_client_overlay == (overlay == "client")
        assert historical.removed_directory_overlay == (overlay == "directory")
        assert "budgetExecutionVersion" not in policies[prefix + "/policies/policy"]
    if defect == "old_parent":
        policies["/apis/unit-api;rev=old2/policies/policy"] = raw_parent
    elif defect == "old_head":
        resources["/apis/unit-api;rev=old2/operations"].pop()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "HEAD"
        revision = request.url.path.split(";rev=", 1)[1].split("/", 1)[0]
        return httpx.Response(503, headers={
            "Cache-Control": "no-store", "x-turnstile-protocol-fenced": "1",
            "x-turnstile-protocol-fence-probe": request.headers["x-turnstile-availability-probe"],
            "x-turnstile-protocol-fence-api": "unit-api",
            "x-turnstile-protocol-fence-revision": revision,
            "x-turnstile-protocol-fence-gateway": "managed",
            "x-turnstile-protocol-fence-instance": "managed",
        })

    verifier = ModelAvailabilityGatewayVerifier(
        target_id=TARGET, api_id="unit-api", gateway_url="https://unit.azure-api.net/llm",
        table_endpoint="https://unit.table.core.windows.net", table_name="UnitLedger",
        key=SecretStr("unit"), list_resources=lambda path: resources[path],
        read_policy=policies.get,
        client=httpx.Client(transport=httpx.MockTransport(respond)), state_reader=Mock(),
        node_store=Mock(), resource_id=(
            f"/subscriptions/{UUID(int=20)}/resourceGroups/unit/"
            "providers/Microsoft.ApiManagement/service/unit"
        ),
    )
    with verifier.client:
        if defect:
            with pytest.raises((ModelAvailabilityUnavailable, ValueError)):
                verifier.verify("4" * 64, [])
        else:
            coverage = verifier.verify("4" * 64, [])
            assert len(coverage.verified_surfaces) == 8
            assert {request.url.path.split(";rev=", 1)[1].split("/", 1)[0]
                    for request in requests} == {"old1", "old2"}


@pytest.mark.parametrize("defect", ["model", "runtime", "operation", "future", "missing", "active"])
def test_contract_adoption_cannot_hide_a_route_identity_or_proof_change(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
    defect: str,
) -> None:
    bundle, installation_plan, registry = copy.deepcopy(prepared)
    fence = installation_plan.expected_fence
    alias, contract = next(iter(fence.model_contracts.items()))
    if defect == "model":
        contract.model_id = UUID(int=98)
    elif defect == "runtime":
        registry["models"][0]["runtime_id"] = UUID(int=98)
    elif defect == "operation":
        contract.operations = ("responses",)
    elif defect == "future":
        contract.verified_at = NOW
    elif defect == "missing":
        del fence.model_contracts[alias]
    else:
        fence.mode = "active"
    with pytest.raises(ValueError):
        capture_installation_contracts(bundle.publication, registry, fence)


@pytest.mark.parametrize("defect", [
    "current", "parent", "extra_operation", "missing_operation", "method", "path", "policy",
    "dependency", "backend", "named_value", "projection", "coverage", "publisher", "stale",
    "fence", "forgotten_revision", "planned_hash",
])
def test_unverified_or_drifted_cloud_state_never_authorizes_promotion(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
    defect: str,
) -> None:
    bundle, installation_plan, _ = prepared
    actual = observed(bundle, installation_plan)
    if defect == "current":
        actual.current_revision = "candidate"
    elif defect == "parent":
        actual.parent_policy = actual.parent_policy.replace("unit-logger", "other-logger")
    elif defect == "extra_operation":
        actual.operations["extra"] = actual.operations["responses"]
    elif defect == "missing_operation":
        del actual.operations["responses"]
    elif defect in {"method", "path", "policy"}:
        values = list(actual.operations["responses"])
        values[{"method": 0, "path": 1, "policy": 2}[defect]] = (
            "GET" if defect == "method" else "/elsewhere" if defect == "path"
            else "<policies><inbound><base/></inbound></policies>"
        )
        actual.operations["responses"] = tuple(values)  # type: ignore[assignment]
    elif defect == "dependency":
        actual.dependencies.live_status = "not_checked"
    elif defect == "backend":
        actual.dependencies.backends = []
    elif defect == "named_value":
        actual.dependencies.named_values = []
    elif defect == "projection":
        actual.projected_models = {}
    elif defect == "coverage":
        actual.availability = actual.availability.model_copy(update={
            "serving_surfaces": ("managed/candidate/responses",),
            "verified_surfaces": ("managed/candidate/responses",),
        })
    elif defect == "publisher":
        actual.publisher_disabled = False
    elif defect == "stale":
        actual.observed_at = NOW - timedelta(seconds=31)
    elif defect == "fence":
        actual.maintenance.fence = actual.maintenance.fence.model_copy(update={"mode": "active"})
    elif defect == "forgotten_revision":
        actual.maintenance.reachable_revisions.append("forgotten")
    else:
        actual.maintenance.revision_policy_sha256["candidate"] = "f" * 64
    with pytest.raises(ValueError):
        actual.require_candidate(installation_plan, bundle, promoted=False, now=NOW)


@pytest.mark.parametrize("policy", ["parent", "responses", "anthropic-models"])
def test_native_xml_reindent_preserves_contract_without_replacing_source_or_raw_plan(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
    policy: str,
) -> None:
    bundle, installation_plan, _ = prepared
    actual = observed(bundle, installation_plan)
    source = bundle.parent_policy if policy == "parent" else bundle.operations[policy][1]
    root = parse_policy(source)
    ElementTree.indent(root, space="    ")
    reserialized = ElementTree.tostring(root, encoding="unicode")
    assert policy_document_digest(reserialized) != bundle.policy_digests[policy]
    assert component_digest(parse_policy(reserialized), normalize_text_defaults=False) == (
        bundle.policy_contracts[policy]
    )
    original_digest, original_sources = bundle.digest, bundle.policy_digests
    if policy == "parent":
        actual.parent_policy = reserialized
        with pytest.raises(ValueError, match="pinned in maintenance"):
            actual.require_candidate(installation_plan, bundle, promoted=False, now=NOW)
        actual_hash = hashlib.sha256(reserialized.encode()).hexdigest()
        installation_plan.policy_sha256["candidate"] = actual_hash
        actual.maintenance.revision_policy_sha256["candidate"] = actual_hash
    else:
        method, path, _ = actual.operations[policy]
        actual.operations[policy] = method, path, reserialized
    actual.require_candidate(installation_plan, bundle, promoted=False, now=NOW)
    assert bundle.digest == original_digest
    assert bundle.policy_digests == original_sources


@pytest.mark.parametrize("defect", ["parent_logger", "parent_default", "operation_identity"])
def test_native_readback_contract_rejects_behavior_changes_even_with_reapproved_raw_hash(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
    defect: str,
) -> None:
    bundle, installation_plan, _ = prepared
    actual = observed(bundle, installation_plan)
    if defect.startswith("parent"):
        source = actual.parent_policy
        if defect == "parent_logger":
            changed = source.replace("unit-logger", "other-logger")
        else:
            changed = re.sub(
                r"long fallback = ([0-9]+);",
                lambda match: f"long fallback = {int(match[1]) + 1};",
                source, count=1,
            )
        assert changed != source
        actual.parent_policy = changed
        raw_hash = hashlib.sha256(changed.encode()).hexdigest()
        installation_plan.policy_sha256["candidate"] = raw_hash
        actual.maintenance.revision_policy_sha256["candidate"] = raw_hash
    else:
        method, path, source = actual.operations["responses"]
        root = parse_policy(source)
        marker = root.find("inbound/set-variable[@name='availabilityTargetId']")
        assert marker is not None
        marker.set("value", str(UUID(int=99)))
        actual.operations["responses"] = (
            method, path, ElementTree.tostring(root, encoding="unicode"),
        )
    with pytest.raises(ValueError, match="differs"):
        actual.require_candidate(installation_plan, bundle, promoted=False, now=NOW)


@pytest.mark.parametrize("defect", ["bundle", "failed", "identity", "outage", "native", "stale"])
def test_real_protocol_diagnostics_are_not_replaceable_by_success_flags(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
    defect: str,
) -> None:
    bundle, installation_plan, _ = prepared
    proof = verification(bundle)
    if defect == "bundle":
        proof.bundle_sha256 = "0" * 64
    elif defect == "failed":
        proof.identity.ready = False
    elif defect == "identity":
        proof.identity.checks[1].actual_status = 200
    elif defect == "outage":
        proof.paused.checks[0].actual_error_code = None
    elif defect == "native":
        proof.paused.checks[0].native_gateway_id = "elsewhere"
    else:
        proof.identity.checked_at = NOW - timedelta(hours=2)
    with pytest.raises(ValueError):
        proof.require_verified(bundle, installation_plan.expected_fence, NOW)


def test_unknown_promotion_is_not_replayed_and_does_not_record_a_publication(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
    tmp_path: Any,
) -> None:
    bundle, installation_plan, _ = prepared
    client = Mock()
    client.promote_revision.side_effect = TimeoutError("unknown ARM outcome")
    with InstallationJournal(tmp_path / "publication.json", installation_plan) as journal:
        for expected in (TimeoutError, ValueError):
            with pytest.raises(expected):
                promote_installation_publication(
                    installation_plan, bundle, verification(bundle), journal, client,
                    lambda: observed(bundle, installation_plan), now=lambda: NOW,
                )
    client.promote_revision.assert_called_once()


def test_interrupted_promotion_can_only_recover_the_same_observed_candidate(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
    tmp_path: Any,
) -> None:
    bundle, installation_plan, _ = prepared
    client = Mock()
    client.promote_revision.side_effect = TimeoutError("response lost after promotion")
    with InstallationJournal(tmp_path / "recover.json", installation_plan) as journal:
        with pytest.raises(TimeoutError):
            promote_installation_publication(
                installation_plan, bundle, verification(bundle), journal, client,
                lambda: observed(bundle, installation_plan), now=lambda: NOW,
            )
        with pytest.raises(ValueError, match="current revision"):
            recover_installation_promotion(
                installation_plan, bundle, journal,
                lambda: observed(bundle, installation_plan), now=lambda: NOW,
            )
        recover_installation_promotion(
            installation_plan, bundle, journal,
            lambda: observed(bundle, installation_plan, promoted=True), now=lambda: NOW,
        )
    client.promote_revision.assert_called_once()


def sql_connection(bundle: InstallationPublicationBundle) -> MagicMock:
    connection = MagicMock()

    def execute(statement: str, parameters: Any = ()) -> Mock:
        row: Any = None
        if statement.startswith("SELECT publication_id FROM effective"):
            row = {"publication_id": bundle.previous.id}
        elif statement.startswith("SELECT * FROM gateway_publication"):
            row = bundle.previous.model_dump()
        elif statement.startswith("SELECT count(*)"):
            row = {"total": 0}
        elif statement.startswith("SELECT COALESCE"):
            row = {"next": bundle.publication.generation}
        elif statement.startswith("SELECT epoch"):
            row = {"epoch": EPOCH, "gateway_profile_ids": [TARGET]}
        elif statement.startswith("SELECT identity.revision"):
            row = {"revision": 1, "enabled": True, "lifecycle": "active"}
        elif statement.startswith("INSERT INTO gateway_publication ("):
            row = bundle.publication.model_dump()
            row.update(status="promoting", resource_manifest=parameters[8].obj)
        cursor = Mock()
        cursor.fetchone.return_value = row
        return cursor

    connection.execute.side_effect = execute
    return connection


def test_sql_handoff_records_only_real_promotion_and_keeps_financial_admission_paused(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, installation_plan, _ = prepared
    connection = sql_connection(bundle)
    sync = Mock()
    activate = Mock(side_effect=lambda connection, row, identity, actor: {
        **row, "status": "active", "completed_at": NOW,
    })
    monkeypatch.setattr(PostgreSqlPublicationRepositoryMixin, "_sync_active_route_bindings", sync)
    monkeypatch.setattr(
        PostgreSqlPublicationRepositoryMixin, "_complete_gateway_activation", activate,
    )
    result = record_promoted_installation(
        connection, installation_plan, bundle, verification(bundle),
        lambda: observed(bundle, installation_plan, promoted=True), now=lambda: NOW,
    )
    assert result.status == PublicationStatus.ACTIVE
    detail = result.resource_manifest["budget_installation"]
    assert isinstance(detail, dict)
    assert detail["financial_activation"] is False
    assert detail["observed_current_revision"] == "candidate"
    assert detail["bundle_sha256"] == bundle.digest
    assert detail["policy_contract_sha256"] == bundle.policy_contracts
    sync.assert_called_once()
    activate.assert_called_once()
    queries = [call.args[0] for call in connection.execute.call_args_list]
    assert not any("UPDATE token_budget" in sql or "UPDATE budget_scope" in sql for sql in queries)
    supersede = next(i for i, sql in enumerate(queries)
                     if sql.startswith("UPDATE gateway_publication"))
    insert = next(i for i, sql in enumerate(queries)
                  if sql.startswith("INSERT INTO gateway_publication ("))
    assert supersede < insert


def test_offline_candidate_json_cannot_be_imported_as_an_effective_release(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
) -> None:
    bundle, installation_plan, _ = prepared
    connection = sql_connection(bundle)
    with pytest.raises(ValueError, match="APIM current revision"):
        record_promoted_installation(
            connection, installation_plan, bundle, verification(bundle),
            lambda: observed(bundle, installation_plan), now=lambda: NOW,
        )
    assert not any(call.args[0].startswith(("INSERT", "UPDATE", "DELETE"))
                   for call in connection.execute.call_args_list)


@pytest.mark.parametrize("defect", ["existing", "effective", "pending", "generation", "rollout"])
def test_database_drift_aborts_before_import(
    prepared: tuple[InstallationPublicationBundle, BudgetInstallationPlan, dict[str, Any]],
    defect: str,
) -> None:
    bundle, installation_plan, _ = prepared
    connection = sql_connection(bundle)
    original = connection.execute.side_effect
    overrides = {
        "existing": ("SELECT id FROM gateway_publication", {"id": bundle.publication.id}),
        "effective": ("SELECT publication_id FROM effective", {"publication_id": UUID(int=99)}),
        "pending": ("SELECT count(*)", {"total": 1}),
        "generation": ("SELECT COALESCE", {"next": 11}),
        "rollout": ("SELECT epoch", None),
    }

    def execute(statement: str, parameters: Any = ()) -> Mock:
        if statement.startswith(overrides[defect][0]):
            result = Mock()
            result.fetchone.return_value = overrides[defect][1]
            return result
        assert callable(original)
        return cast(Mock, original(statement, parameters))

    connection.execute.side_effect = execute
    with pytest.raises(ValueError):
        record_promoted_installation(
            connection, installation_plan, bundle, verification(bundle),
            lambda: observed(bundle, installation_plan, promoted=True), now=lambda: NOW,
        )
    assert not any(call.args[0].startswith(("INSERT", "UPDATE", "DELETE"))
                   for call in connection.execute.call_args_list)
