from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock
from typing import Any
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from backend.services.runtime_service import ModelRuntimeService
from scripts.install_model_availability import (
    AvailabilityInstallation,
    AvailabilityInstaller,
    normalize_legacy_parent,
)
from tests.backend.model_platform.control_plane_support import (
    FakeApimClient,
    GatewayControlPlaneService,
    GatewayPublicationWorker,
    bedrock_publication,
    foundry_publication,
    publisher_settings,
)
from tests.backend.model_platform.test_image_publication import image_publication
from tests.backend.model_platform.test_model_runtime import request as invocation_request
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.domain.model_availability import (
    MAX_AVAILABILITY_REVISION,
    ModelAvailabilityCommand,
    ModelAvailabilityConfirmationPlan,
    ModelAvailabilityConflict,
    ModelAvailabilityCoverage,
    ModelAvailabilityNodeLease,
    ModelAvailabilityOperation,
    ModelAvailabilityPolicyAdoption,
    ModelAvailabilityPublication,
    ModelAvailabilityState,
    ModelAvailabilityTargetResult,
    ModelAvailabilityUnavailable,
)
from turnstile_core.domain.runtime_models import ManagedModelWrite, ProviderTarget
from turnstile_core.integrations.apim_budget_execution import (
    _deny,
    budget_execution_fence,
    compose_budget_execution_parent,
)
from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
from turnstile_core.integrations.apim_model_availability import (
    INHERITED_OPERATION_POLICY,
    RESOLVE_FIXED_MODEL,
    _admission_locations,
    availability_denial,
    availability_kernel_digest,
    availability_operation_digest,
    compile_availability_adoption,
    compile_protocol_fenced_availability_operation,
    compile_protocol_fenced_probe,
    compose_availability_parent,
    is_pre_admission_denial,
    is_protocol_fenced_availability_operation,
    is_read_only_budget_fence,
    is_retired_availability_operation,
    validate_availability_parent,
    without_availability_parent,
)
from turnstile_core.integrations.apim_policy_compiler import ApimPolicyCompiler
from turnstile_core.integrations.apim_policy_components import (
    component_digest,
    parse_policy,
    serialize_policy,
    validate_parent_policy,
)
from turnstile_core.integrations.apim_publisher_client import (
    AzureApimPublisherClient,
    _AvailabilityManagementReadPacer,
)
from turnstile_core.integrations.ledger import TableStorageLedger
from turnstile_core.integrations.model_availability_verifier import (
    ModelAvailabilityGatewayVerifier,
    ModelAvailabilityNodeVerifier,
    _no_bypass,
    _revision_name,
    _verify_diagnostic_pairs,
    inspect_availability_operation,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository_model_availability import (
    PostgreSqlModelAvailabilityRepositoryMixin,
    _model_inventory,
)
from turnstile_core.persistence.repository_registry import PostgreSqlRegistryRepositoryMixin
from turnstile_core.services.gateway_release_operation_worker import GatewayReleaseOperationWorker
from turnstile_core.services.model_availability import (
    ModelAvailabilityCoordinator,
    ModelAvailabilityWorker,
    project_model_availability,
)


@pytest.mark.parametrize("task_status", ["pending", "leased", "failed"])
def test_model_readback_isolates_gateway_failure_and_exposes_initial_confirmation(
    task_status: str,
) -> None:
    model_id, target, other_model, unrelated = [UUID(int=value) for value in (801, 802, 803, 804)]
    own = {"model_id": model_id, "revision": 2, "enabled": True, "lifecycle": "active",
           "effective_revision": None, "effective_enabled": None, "verified_at": None}
    now = datetime.now(UTC)
    proof = ModelAvailabilityCoverage(
        target_id=target, inventory_digest="a" * 64, policy_digest="b" * 64,
        serving_surfaces=("managed/current/responses",),
        verified_surfaces=("managed/current/responses",),
        verified_at=now - timedelta(seconds=1), expires_at=now + timedelta(seconds=119),
    )
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchall.side_effect = [
        [own, {**own, "model_id": other_model}], [],
        [{"model_id": model_id, "revision": 2, "status": task_status}],
        [{"model_id": model_id, "target_id": target},
         {"model_id": other_model, "target_id": unrelated}],
        [{"target_id": target, "inventory_digest": "a" * 64},
         {"target_id": unrelated, "inventory_digest": "c" * 64}],
        [{"target_id": target, "coverage": proof.model_dump()}],
    ]
    repository = PostgreSqlModelAvailabilityRepositoryMixin()
    with patch.object(repository, "_connection", return_value=connection):
        result, blocked = repository.list_model_availability()
    assert result.command_ready
    assert result.status == ("failed" if task_status == "failed" else "pending")
    assert result.reason == ("model_availability_initialization_failed"
                             if task_status == "failed" else None)
    assert result.effective_revision is None and result.verified_at is None
    assert not blocked.command_ready
    assert blocked.reason == "model_availability_capability_unverified"
    assert all(call.args[0].lstrip().startswith(("SELECT", "SET"))
               for call in connection.execute.call_args_list)


def test_model_inventory_requires_every_own_target_without_unrelated_targets() -> None:
    model_id, own, other = UUID(int=801), UUID(int=802), UUID(int=803)
    inventory = {own: "a" * 64, other: "b" * 64}
    assert _model_inventory(model_id, inventory, {model_id: {own}}) == {own: "a" * 64}
    assert _model_inventory(model_id, inventory, {model_id: {own, UUID(int=804)}}) == {}
    assert _model_inventory(model_id, inventory, {}) == {}


@pytest.mark.parametrize("related", [False, True])
def test_model_command_does_not_depend_on_unrelated_gateway_coverage(related: bool) -> None:
    model_id, own, other = UUID(int=801), UUID(int=802), UUID(int=803)
    repository = PostgreSqlModelAvailabilityRepositoryMixin()
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.side_effect = [
        {"id": model_id, "enabled": True}, None,
        {"revision": 7, "lifecycle": "active"}, None, None,
    ]
    connection.execute.return_value.fetchall.return_value = [
        {"model_id": model_id, "target_id": own},
        {"model_id": model_id if related else UUID(int=804), "target_id": other},
    ]
    now = datetime.now(UTC)
    proof = ModelAvailabilityCoverage(
        target_id=own, inventory_digest="a" * 64, policy_digest="b" * 64,
        serving_surfaces=("managed/current/responses",),
        verified_surfaces=("managed/current/responses",),
        verified_at=now - timedelta(seconds=1), expires_at=now + timedelta(seconds=119),
    )
    command = ModelAvailabilityCommand(enabled=False, expected_revision=7, idempotency_key=uuid4())
    with patch.object(repository, "_connection", return_value=connection), \
            patch("turnstile_core.persistence.repository_model_availability._coverage",
                  return_value=({own: "a" * 64, other: "c" * 64}, {own: proof})):
        if related:
            with pytest.raises(ModelAvailabilityConflict, match="capability is not verified"):
                repository.change_model_availability(model_id, command, "unit-owner")
            assert all(call.args[0].lstrip().startswith("SELECT")
                       for call in connection.execute.call_args_list)
        else:
            operation = repository.change_model_availability(model_id, command, "unit-owner")
            assert [target.target_id for target in operation.targets] == [own]
            assert operation.desired_revision == 8 and operation.desired_enabled is False


def test_projection_retries_conflicts_and_reads_back_the_exact_revision() -> None:
    desired = ModelAvailabilityState(model_id=UUID(int=2), revision=2, enabled=False)
    prior = desired.model_copy(update={"revision": 1, "enabled": True})
    repository, store = Mock(), Mock()
    repository.get_model_availability_state.return_value = desired
    store.read_versioned_entity.side_effect = [
        (prior.entity(), '"first"'),
        (prior.entity(), '"second"'),
        (desired.entity(), '"third"'),
    ]
    store.replace_if_match.side_effect = [False, True]
    assert project_model_availability(repository, store, desired.model_id) == desired
    assert store.replace_if_match.call_args.args[-1] == '"second"'
    assert store.replace_if_match.call_count == 2


@pytest.mark.parametrize("change", [{"revision": 3}, {"enabled": True}])
def test_projection_never_replaces_newer_or_conflicting_state(change: dict[str, Any]) -> None:
    desired = ModelAvailabilityState(model_id=UUID(int=2), revision=2, enabled=False)
    repository, store = Mock(), Mock()
    repository.get_model_availability_state.return_value = desired
    store.read_versioned_entity.return_value = (
        desired.model_copy(update=change).entity(),
        '"etag"',
    )
    with pytest.raises(ModelAvailabilityUnavailable, match="roll back"):
        project_model_availability(repository, store, desired.model_id)
    store.replace_if_match.assert_not_called()


def test_worker_lost_projection_lease_never_calls_gateway_confirmation() -> None:
    desired = ModelAvailabilityState(model_id=UUID(int=2), revision=2, enabled=False)
    repository, store, verifier = MagicMock(), MagicMock(), Mock()
    repository.claim_model_availability_task.return_value = {
        "model_id": desired.model_id,
        "revision": 2,
    }
    repository.get_model_availability_state.return_value = desired
    repository.renew_model_availability_lease.return_value = True
    repository.mark_model_availability_projected.return_value = False
    store.__enter__.return_value = store
    store.read_versioned_entity.side_effect = [None, (desired.entity(), '"etag"')]
    store.replace_if_match.return_value = True
    assert ModelAvailabilityWorker(repository, lambda: store, verifier).run_once("unit-worker")
    verifier.assert_not_called()
    assert (
        repository.finish_model_availability_task.call_args.args[-1]
        == "model_availability_unavailable"
    )


@pytest.mark.parametrize("bad_page", ["duplicate", "foreign", "continuation", "limit"])
def test_partition_inventory_rejects_incomplete_or_ambiguous_pages(bad_page: str) -> None:
    partition = "model-availability|v1"
    client = Mock()
    first = httpx.Response(
        200,
        json={"value": [{"PartitionKey": partition, "RowKey": "one"}]},
        headers={"x-ms-continuation-NextPartitionKey": "next"},
        request=httpx.Request("GET", "https://unit.table.core.windows.net/Unit"),
    )
    row = {
        "PartitionKey": "other" if bad_page == "foreign" else partition,
        "RowKey": "one" if bad_page == "duplicate" else "two",
    }
    second = httpx.Response(
        200,
        json={"value": [row]},
        headers={"x-ms-continuation-NextPartitionKey": "next"}
        if bad_page == "continuation"
        else {},
        request=first.request,
    )
    store = TableStorageLedger.__new__(TableStorageLedger)
    store._endpoint, store._table = "https://unit.table.core.windows.net", "Unit"
    store._request = client  # type: ignore[method-assign]
    client.side_effect = [first, second]
    with pytest.raises(ValueError):
        store.list_partition_entities(partition, max_entities=1 if bad_page == "limit" else 10)
    assert client.call_args.args[2] == {"Cache-Control": "no-cache"}


def test_native_admission_composes_without_rewriting_the_existing_parent() -> None:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    compiled = compose_availability_parent(source)
    root = parse_policy(compiled)
    validate_availability_parent(root)
    assert component_digest(without_availability_parent(root)) == component_digest(
        parse_policy(source)
    )
    assert compose_availability_parent(compiled) == compiled
    assert len(root.findall(".//set-variable[@name='availabilityBindingValid']")) == 2
    status = availability_denial(disabled=True).find("set-status")
    assert status is not None and status.get("code") == "403"
    reads = root.findall(".//send-request[@response-variable-name='availabilityRead']")
    assert len(reads) == 2 and all(node.get("timeout") == "2" for node in reads)
    for read in reads:
        assert read.findtext("set-method") == "GET"
        assert read.findtext("set-header[@name='Cache-Control']/value") == "no-cache"


@pytest.mark.parametrize("change", ["cache", "shadow", "guard"])
def test_native_admission_rejects_policy_tampering(change: str) -> None:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    root = parse_policy(compose_availability_parent(source))
    if change == "guard":
        node = root.find(".//set-variable[@name='availabilityBindingValid']")
        assert node is not None
        node.set("value", "@(true)")
    elif change == "shadow":
        node = root.find("inbound/set-variable[@name='availabilityKernelDigest']")
        assert node is not None
        node.set("value", "bad")
    else:
        node = root.find("backend")
        assert node is not None
        node.append(parse_policy("<cache-lookup />"))
    with pytest.raises(PolicyCompilationError):
        if change == "cache":
            compose_availability_parent(serialize_policy(root))
        else:
            validate_availability_parent(root)


@pytest.mark.parametrize("retired", [False, True])
def test_fixed_availability_route_preserves_binding_or_rejects_before_inheritance(
    retired: bool,
) -> None:
    source = INHERITED_OPERATION_POLICY.replace(
        "<inbound><base />", '<inbound><base /><set-backend-service backend-id="unit-backend" />',
    )
    adoption = ModelAvailabilityPolicyAdoption(
        adoption_id=UUID(int=11), target_id=UUID(int=12), api_id="unit-api", revision="1",
        models={"unit-model": UUID(int=2), "other-model": UUID(int=3)},
        serving_gateways={"unit-model": ("edge",), "other-model": None},
        original_policy_digests={"fixed": hashlib.sha256(source.encode()).hexdigest()},
        fixed_operations={"fixed": "unit-model"}, retired_operations=("fixed",) if retired else (),
    )
    compiled = compile_availability_adoption(source, adoption, "fixed")
    root = parse_policy(compiled)
    assert root.find("inbound/set-backend-service[@backend-id='unit-backend']") is not None
    assert is_retired_availability_operation(root) is retired
    inspected = inspect_availability_operation(
        compiled, UUID(int=12), "unit-api", "1", catalog=False,
    )
    assert inspected == ([] if retired else [{
        "alias": "unit-model", "model_id": str(UUID(int=2)), "gateways": ["edge"],
    }])
    if retired:
        first = root.find("inbound")
        assert first is not None
        assert first[0].tag == "return-response"
        assert first[0].findtext("set-header[@name='x-turnstile-model-retired']/value") == "1"
        status = first[0].find("set-status")
        assert status is not None and status.get("code") == "403"
    else:
        resolver = root.find("inbound/set-variable[@name='availabilityModelId']")
        routes = root.find("inbound/set-variable[@name='availabilityRoutes']")
        assert resolver is not None and resolver.get("value") == RESOLVE_FIXED_MODEL
        assert routes is not None
        assert json.loads(routes.attrib["value"]) == [{
            "alias": "unit-model", "model_id": str(UUID(int=2)), "gateways": ["edge"],
        }]
    with pytest.raises(PolicyCompilationError):
        compile_availability_adoption(source, adoption, "fixed", catalog=True)


def test_historical_protocol_fence_is_idempotent_and_probe_cannot_process_an_image_body() -> None:
    source = INHERITED_OPERATION_POLICY
    compiled = compile_protocol_fenced_availability_operation(source)
    assert compile_protocol_fenced_availability_operation(compiled) == compiled
    root = parse_policy(compiled)
    assert is_protocol_fenced_availability_operation(root)
    inbound = root.find("inbound")
    assert inbound is not None
    assert inbound[0].findtext("set-body") is not None
    assert "budget_protocol_fenced" in inbound[0].findtext("set-body", "")
    inbound.remove(inbound[0])
    assert component_digest(root) == component_digest(parse_policy(source))
    image_source = source.replace(
        "<inbound><base />",
        '<inbound><set-variable name="imageRequestValid" value="@(false)" /><base />',
    )
    probe = parse_policy(compile_protocol_fenced_probe(image_source))
    probe_inbound = probe.find("inbound")
    assert probe_inbound is not None and probe_inbound[0].tag == "base"
    assert compile_protocol_fenced_probe(source) == source
    tampered = parse_policy(compiled)
    tampered_status = tampered.find("inbound/return-response/set-status")
    assert tampered_status is not None
    tampered_status.set("code", "200")
    assert not is_protocol_fenced_availability_operation(tampered)


@pytest.mark.parametrize("defect", [None, "post", "success", "scope", "write"])
def test_parent_only_accepts_the_exact_read_only_budget_fence(defect: str | None) -> None:
    fence = budget_execution_fence(["unit-api"])
    if defect == "post":
        method = fence.find("when/send-request/set-method")
        assert method is not None
        method.text = "POST"
    elif defect == "success":
        response = fence.find(".//return-response/set-status")
        assert response is not None
        response.set("code", "200")
    elif defect == "scope":
        branch = fence.find("when")
        assert branch is not None
        branch.set("condition", "@(true)")
    elif defect == "write":
        branch = fence.find("when")
        assert branch is not None
        ET.SubElement(branch, "send-request")
    assert is_read_only_budget_fence(fence) is (defect is None)


@pytest.mark.parametrize("defect", [None, "status", "reassign", "late"])
def test_legacy_budget_denial_uses_bounded_status_without_log_text(defect: str | None) -> None:
    root = ET.Element("inbound")
    _deny(root, "@(true)", "budget_protocol_fenced")
    branch = root.find("choose/when")
    assert branch is not None
    event = branch.find("log-to-eventhub")
    assert event is not None
    branch.remove(event)
    status = branch.find("set-variable[@name='executionStatus']")
    response = branch.find("return-response")
    assert status is not None and response is not None
    if defect == "status":
        status.set("value", "@(200)")
    elif defect == "reassign":
        branch.insert(list(branch).index(response), ET.Element("set-variable", {
            "name": "executionStatus", "value": "@(200)",
        }))
    elif defect == "late":
        branch.remove(status)
        branch.append(status)
    assert is_pre_admission_denial(root, response) is (defect is None)


@pytest.mark.parametrize("defect", [None, "employee_auth", "subscription_auth", "early_work"])
def test_legacy_parent_requires_authentication_before_availability(defect: str | None) -> None:
    employee = '<validate-azure-ad-token tenant-id="unit-tenant" />'
    subscription = (
        '<choose><when condition="@(context.Subscription == null)">'
        '<return-response><set-status code="401" reason="Unauthorized" />'
        '</return-response></when></choose>'
    )
    if defect == "employee_auth":
        employee = '<set-variable name="unchecked" value="true" />'
    elif defect == "subscription_auth":
        subscription = subscription.replace('code="401"', 'code="200"')
    elif defect == "early_work":
        employee = '<rate-limit calls="1" renewal-period="60" />' + employee
    source = (
        '<policies><inbound><base /><choose>'
        '<when condition="@((bool)context.Variables[&quot;hasEmployeeToken&quot;])">'
        + employee + '<llm-token-limit counter-key="unit" tokens-per-minute="100" />'
        '</when><otherwise>' + subscription
        + '<llm-token-limit counter-key="unit" tokens-per-minute="100" />'
        '</otherwise></choose></inbound><backend><base /></backend>'
        '<outbound><base /></outbound><on-error><base /></on-error></policies>'
    )
    if defect:
        with pytest.raises(PolicyCompilationError):
            compose_availability_parent(source)
    else:
        upgraded = parse_policy(compose_availability_parent(source))
        assert len(_admission_locations(upgraded)) == 2
        assert component_digest(without_availability_parent(upgraded)) == component_digest(
            parse_policy(source)
        )


def test_native_catalog_adoption_binds_model_and_preserves_the_upstream_route() -> None:
    source = (
        '<policies><inbound><base /><return-response><set-status code="200" reason="OK" />'
        '<set-body>{"data":[{"id":"unit-model","owned_by":"unit"}]}</set-body>'
        "</return-response></inbound><backend><base /></backend>"
        "<outbound><base /></outbound><on-error><base /></on-error></policies>"
    )
    adoption = ModelAvailabilityPolicyAdoption(
        adoption_id=UUID(int=11),
        target_id=UUID(int=12),
        api_id="unit-api",
        revision="unit-rev",
        models={"unit-model": UUID(int=2)},
        serving_gateways={"unit-model": None},
        original_policy_digests={"models": hashlib.sha256(source.encode()).hexdigest()},
    )
    root = parse_policy(compile_availability_adoption(source, adoption, "models", catalog=True))
    scope = root.find(".//set-variable[@name='availabilityCatalogScope']")
    assert scope is not None
    assert json.loads(scope.attrib["value"])[0]["logical_model_id"] == str(UUID(int=2))
    marker = root.find("inbound/set-variable[@name='availabilitySurfaceDigest']")
    assert marker is not None
    assert marker.get("value") == availability_operation_digest(root)
    assert "availabilityCatalogResult" in root.findtext(
        ".//return-response/set-body", ""
    ) or root.findall(".//set-variable[@name='availabilityCatalogResult']")
    with pytest.raises(ValueError):
        compile_availability_adoption(source + " ", adoption, "models", catalog=True)


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"apiRevision": True},
        {"apiRevision": "../other"},
        {"apiRevision": "1", "properties": {"apiRevision": "2"}},
    ],
)
def test_availability_revision_inventory_rejects_ambiguous_identity(row: dict[str, Any]) -> None:
    with pytest.raises(ModelAvailabilityUnavailable):
        _revision_name(row)


@pytest.mark.parametrize(
    "policy",
    [
        '<return-response><set-status code="200" /></return-response>',
        "<send-request><set-method>POST</set-method></send-request>",
        '<set-variable name="availabilityModelId" value="other" />',
        '<include-fragment fragment-id="unknown" />',
        "<cache-lookup />",
        '<llm-token-limit tokens-per-minute="1" />',
    ],
)
def test_availability_inherited_policy_rejects_work_before_admission(policy: str) -> None:
    with pytest.raises(ModelAvailabilityUnavailable):
        _no_bypass(parse_policy(f"<policies><inbound>{policy}</inbound></policies>"))


def test_diagnostics_require_exact_inference_policy_and_path_pairs() -> None:
    inference = {"responses": ("/responses", "a" * 64)}
    _verify_diagnostic_pairs(inference, {"availability-probe-responses": inference["responses"]})
    for diagnostics in ({}, {"availability-probe-responses": ("/responses", "b" * 64)}):
        with pytest.raises(ModelAvailabilityUnavailable):
            _verify_diagnostic_pairs(inference, diagnostics)


def gateway_availability_fixture(
    corruption: str | None = None, *, revision_count: int = 2,
) -> tuple[ModelAvailabilityGatewayVerifier, ModelAvailabilityState, list[httpx.Request]]:
    target_id, model_id = UUID(int=12), UUID(int=2)
    state = ModelAvailabilityState(model_id=model_id, revision=2, enabled=False)
    raw_inference = (
        '<policies><inbound><base /><set-backend-service backend-id="unit" />'
        "</inbound><backend><base /></backend><outbound><base /></outbound>"
        "<on-error><base /></on-error></policies>"
    )
    raw_catalog = (
        '<policies><inbound><base /><return-response><set-status code="200" reason="OK" />'
        '<set-body>{"data":[{"id":"unit-model"}]}</set-body></return-response>'
        "</inbound><backend><base /></backend><outbound><base /></outbound>"
        "<on-error><base /></on-error></policies>"
    )
    policies: dict[str, str] = {}
    revisions = [str(index) for index in range(1, revision_count + 1)]
    resources: dict[str, list[dict[str, Any]]] = {
        "/apis/unit-api/products": [],
        "/apis/unit-api/revisions": [
            {"apiRevision": revision} if int(revision) % 2
            else {"properties": {"apiRevision": revision}}
            for revision in revisions
        ],
    }
    capabilities: dict[str, str] = {}
    surface_digests: dict[tuple[str, str], str] = {}
    for revision in revisions:
        adoption = ModelAvailabilityPolicyAdoption(
            adoption_id=UUID(int=11),
            target_id=target_id,
            api_id="unit-api",
            revision=revision,
            models={"unit-model": model_id},
            serving_gateways={"unit-model": None},
            original_policy_digests={
                "responses": hashlib.sha256(raw_inference.encode()).hexdigest(),
                "models": hashlib.sha256(raw_catalog.encode()).hexdigest(),
            },
        )
        inference = compile_availability_adoption(raw_inference, adoption, "responses")
        catalog = compile_availability_adoption(raw_catalog, adoption, "models", catalog=True)
        assert inspect_availability_operation(
            inference,
            target_id,
            "unit-api",
            revision,
            catalog=False,
        )[0]["model_id"] == str(model_id)
        assert inspect_availability_operation(
            catalog,
            target_id,
            "unit-api",
            revision,
            catalog=True,
        )[0]["logical_model_id"] == str(model_id)
        root = parse_policy(inference)
        capability = root.find("inbound/set-variable[@name='availabilityCapabilityDigest']")
        assert capability is not None
        capabilities[revision] = capability.attrib["value"]
        surface_digests[revision, "/responses"] = availability_operation_digest(root)
        surface_digests[revision, "/models"] = availability_operation_digest(parse_policy(catalog))
        prefix = f"/apis/unit-api;rev={revision}"
        policies[prefix + "/policies/policy"] = (
            compose_availability_parent(
                (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
            )
            .replace("__GATEWAY_PROFILE_ID__", str(target_id))
            .replace(
                "__LEDGER_TABLE_ENDPOINT__",
                "https://unit.table.core.windows.net",
            )
            .replace("__LEDGER_TABLE_NAME__", "Unit")
        )
        resources[prefix + "/operations"] = [
            {"name": "responses", "properties": {"method": "POST", "urlTemplate": "/responses"}},
            {"name": "models", "properties": {"method": "GET", "urlTemplate": "/models"}},
            {
                "name": "availability-probe-responses",
                "properties": {
                    "method": "HEAD",
                    "urlTemplate": "/responses",
                },
            },
            {"name": "availability-probe-models", "properties": {
                "method": "HEAD", "urlTemplate": "/models",
            }},
        ]
        for name, source in (
            ("responses", inference),
            ("models", catalog),
            ("availability-probe-responses", inference),
            ("availability-probe-models", catalog),
        ):
            policies[prefix + f"/operations/{name}/policies/policy"] = source
    if corruption == "missing_revision_policy":
        policies.pop("/apis/unit-api;rev=1/policies/policy")
    if corruption == "missing_diagnostic":
        resources["/apis/unit-api;rev=1/operations"].pop()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method in {"GET", "HEAD"}
        assert request.headers["Cache-Control"] == "no-cache"
        revision = request.url.path.split(";rev=", 1)[1].split("/", 1)[0]
        fields = {
            "target": str(target_id),
            "revision": revision,
            "gateway": "managed",
            "instance": "managed",
            "version": "1",
            "kernel": availability_kernel_digest(),
            "store": hashlib.sha256(b"https://unit.table.core.windows.net|Unit").hexdigest(),
            "capability": capabilities[revision],
            "surface": surface_digests[revision, "/" + request.url.path.rsplit("/", 1)[-1]],
        }
        if request.method == "HEAD":
            fields["probe"] = request.headers["x-turnstile-availability-probe"]
        else:
            fields["digest"] = hashlib.sha256(f"{model_id}:2:0".encode()).hexdigest()
        if corruption in {"target", "revision", "digest", "kernel"}:
            fields[corruption] = "wrong"
        if corruption == "missing_probe" and request.method == "HEAD":
            fields.pop("probe")
        return httpx.Response(
            503 if request.method == "HEAD" else 200,
            headers={
                "Cache-Control": "public" if corruption == "cached" else "no-store",
                **{"x-turnstile-availability-" + name: value for name, value in fields.items()},
            },
            json={"data": [{"id": "unit-model"}] if corruption == "stale_catalog" else []},
        )

    verifier = ModelAvailabilityGatewayVerifier(
        target_id=target_id,
        api_id="unit-api",
        gateway_url="https://unit.azure-api.net/llm",
        table_endpoint="https://unit.table.core.windows.net",
        table_name="Unit",
        key=SecretStr("unit"),
        list_resources=lambda resource: resources[resource],
        read_policy=policies.get,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        state_reader=lambda _model: state,
        node_store=Mock(spec=TableStorageLedger),
        resource_id=f"/subscriptions/{UUID(int=20)}/resourceGroups/unit/providers/Microsoft.ApiManagement/service/unit",
    )
    return verifier, state, requests


@pytest.mark.parametrize(
    "age,valid", [(-1, False), (0, True), (239, True), (240, True), (241, False), (300, False)],
)
def test_confirmation_management_evidence_has_an_absolute_five_minute_bound(
    age: int, valid: bool,
) -> None:
    verifier, _state, _requests = gateway_availability_fixture()
    with verifier.client:
        verifier.verify("a" * 64, [])
    assert verifier.confirmation_plan is not None
    payload = verifier.confirmation_plan.model_dump(mode="json")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    payload["coverage"]["verified_at"] = now.isoformat()
    payload["coverage"]["expires_at"] = (now + timedelta(seconds=60)).isoformat()
    payload["management_verified_at"] = (now - timedelta(seconds=age)).isoformat()
    if valid:
        plan = ModelAvailabilityConfirmationPlan.model_validate(payload)
        assert ModelAvailabilityConfirmationPlan.from_entity(plan.entity()) == plan
    else:
        with pytest.raises(ValidationError, match="validity window"):
            ModelAvailabilityConfirmationPlan.model_validate(payload)


@pytest.mark.parametrize(
    "damage", ["duplicate", "overlap", "traversal", "fenced_models", "retired_deployments"],
)
def test_confirmation_rejects_ambiguous_retired_surface_evidence(damage: str) -> None:
    verifier, _state, _requests = gateway_availability_fixture()
    with verifier.client:
        verifier.verify("a" * 64, [])
    assert verifier.confirmation_plan is not None
    payload = verifier.confirmation_plan.model_dump(mode="json")
    check = payload["catalogs"][0]
    if damage == "duplicate":
        check["retired_surfaces"] = ["/old-model", "/old-model"]
    elif damage == "overlap":
        check["retired_surfaces"] = list(check["admission_surfaces"])
    elif damage == "traversal":
        check["retired_surfaces"] = ["/../other"]
    elif damage == "fenced_models":
        check["protocol_fenced"] = True
    else:
        payload["retired_deployments"] = [str(UUID(int=50)), str(UUID(int=50))]
    with pytest.raises(ValidationError):
        ModelAvailabilityConfirmationPlan.model_validate(payload)


@pytest.mark.parametrize("damage", [
    "missing_operation", "unknown_model", "parent", "unbound_retirement",
    "duplicate_retirement", "duplicate_inheritance",
])
def test_policy_adoption_requires_explicit_fixed_and_retired_operation_identities(
    damage: str,
) -> None:
    payload: dict[str, Any] = {
        "adoption_id": str(UUID(int=11)), "target_id": str(UUID(int=12)),
        "api_id": "unit-api", "revision": "1", "models": {"unit-model": str(UUID(int=2))},
        "serving_gateways": {"unit-model": None}, "original_policy_digests": {"fixed": "a" * 64},
        "fixed_operations": {"fixed": "unit-model"}, "retired_operations": ["fixed"],
    }
    assert ModelAvailabilityPolicyAdoption.model_validate(payload).retired_operations == ("fixed",)
    if damage == "missing_operation":
        payload["original_policy_digests"] = {"other": "b" * 64}
    elif damage == "unknown_model":
        payload["fixed_operations"] = {"fixed": "unbound"}
    elif damage == "parent":
        payload["inherited_operations"] = ["parent"]
    elif damage == "unbound_retirement":
        payload["retired_operations"] = ["other"]
    elif damage == "duplicate_retirement":
        payload["retired_operations"] = ["fixed", "fixed"]
    else:
        payload["inherited_operations"] = ["fixed", "fixed"]
    with pytest.raises(ValidationError):
        ModelAvailabilityPolicyAdoption.model_validate(payload)


def test_gateway_verification_covers_inventory_exceeding_the_old_300_read_limit() -> None:
    verifier, _state, requests = gateway_availability_fixture(revision_count=50)
    original_policy, original_inventory = verifier.read_policy, verifier.list_resources
    management_reads: list[str] = []
    lock = Lock()

    def policy(path: str) -> str | None:
        with lock:
            management_reads.append(path)
        return original_policy(path)

    def inventory(path: str) -> list[dict[str, Any]]:
        with lock:
            management_reads.append(path)
        return original_inventory(path)

    verifier.read_policy, verifier.list_resources = policy, inventory
    with verifier.client:
        coverage = verifier.verify("a" * 64, [])
    assert len(coverage.serving_surfaces) == 200
    assert len(management_reads) == 303
    assert len(requests) == 150
    assert len(management_reads) + len(requests) == 453
    assert {request.method for request in requests} == {"GET", "HEAD"}
    assert verifier.confirmation_plan is not None
    assert verifier.confirmation_plan.management_verified_at is not None


def test_availability_management_reads_are_parallel_bounded_and_precede_native_proof() -> None:
    verifier, _state, requests = gateway_availability_fixture(revision_count=16)
    original_policy = verifier.read_policy
    barrier = Barrier(8)
    lock = Lock()
    active = 0
    maximum = 0
    calls: list[str] = []
    proof_started = False

    def policy(path: str) -> str | None:
        nonlocal active, maximum
        if ";rev=" not in path:
            return original_policy(path)
        with lock:
            active += 1
            maximum = max(maximum, active)
            calls.append(path)
        try:
            assert not proof_started
            barrier.wait(timeout=5)
            return original_policy(path)
        finally:
            with lock:
                active -= 1

    def before_proof() -> None:
        nonlocal proof_started
        assert len(calls) == 80 and active == 0 and not requests
        proof_started = True

    verifier.read_policy, verifier.before_proof = policy, before_proof
    with verifier.client:
        verifier.verify("a" * 64, [])
    assert maximum == 8 and proof_started


def test_fast_confirmation_uses_live_proofs_without_management_reads_or_lease_writes() -> None:
    verifier, state, requests = gateway_availability_fixture()
    with verifier.client:
        verifier.verify("a" * 64, [])
        assert verifier.confirmation_plan is not None
        requests.clear()
        verifier.read_policy = Mock(side_effect=AssertionError("Unexpected policy read"))
        verifier.list_resources = Mock(side_effect=AssertionError("Unexpected inventory read"))
        store = Mock(spec=TableStorageLedger)
        verifier.node_store = store
        result = verifier.confirm(
            verifier.confirmation_plan, state, "a" * 64, [], lambda _ids: {state.model_id: state},
        )
    assert result.status == "verified"
    assert [request.method for request in requests].count("GET") == 2
    assert [request.method for request in requests].count("HEAD") == 4
    store.replace_if_match.assert_not_called()
    store.read_versioned_entity.assert_not_called()


@pytest.mark.parametrize("defect", ["missing", "expired", "future", "inventory", "state"])
def test_coverage_renewal_refuses_invalid_management_evidence(defect: str) -> None:
    verifier, state, requests = gateway_availability_fixture()
    with verifier.client:
        verifier.verify("a" * 64, [])
        assert verifier.confirmation_plan is not None
        plan = verifier.confirmation_plan
        now = datetime.now(UTC)
        if defect == "missing":
            plan = plan.model_copy(update={"management_verified_at": None})
        elif defect == "expired":
            plan = plan.model_copy(update={"management_verified_at": now - timedelta(seconds=301)})
        elif defect == "future":
            plan = plan.model_copy(update={"management_verified_at": now + timedelta(seconds=30)})
        requests.clear()
        with pytest.raises(ModelAvailabilityUnavailable):
            verifier.renew(
                plan, "b" * 64 if defect == "inventory" else "a" * 64, [],
                lambda _ids: {} if defect == "state" else {state.model_id: state},
            )
    assert not requests


def test_coverage_renewal_cannot_extend_the_original_management_validity() -> None:
    verifier, state, requests = gateway_availability_fixture()
    with verifier.client:
        verifier.verify("a" * 64, [])
        assert verifier.confirmation_plan is not None
        payload = verifier.confirmation_plan.model_dump()
        now = datetime.now(UTC)
        payload["management_verified_at"] = now - timedelta(seconds=220)
        payload["coverage"]["verified_at"] = now - timedelta(seconds=2)
        payload["coverage"]["expires_at"] = now + timedelta(seconds=60)
        plan = ModelAvailabilityConfirmationPlan.model_validate(payload)
        verifier.read_policy = Mock(side_effect=AssertionError("Unexpected policy read"))
        verifier.list_resources = Mock(side_effect=AssertionError("Unexpected inventory read"))
        coverage = verifier.renew(plan, "a" * 64, [], lambda _ids: {state.model_id: state})
        assert coverage.expires_at <= now + timedelta(seconds=80)
        assert verifier.confirmation_plan is not None
        assert verifier.confirmation_plan.management_verified_at == plan.management_verified_at
        again = verifier.renew(
            verifier.confirmation_plan, "a" * 64, [], lambda _ids: {state.model_id: state},
        )
        assert again.expires_at == coverage.expires_at
    assert {request.method for request in requests} == {"GET", "HEAD"}


def test_verification_rejects_state_drift_after_the_batch_snapshot() -> None:
    verifier, state, _requests = gateway_availability_fixture()
    verifier.state_batch_reader = Mock(side_effect=[
        {state.model_id: state},
        {state.model_id: state.model_copy(update={"revision": state.revision + 1})},
    ])
    with verifier.client, pytest.raises(ModelAvailabilityUnavailable, match="source changed"):
        verifier.verify("a" * 64, [])
    assert verifier.confirmation_plan is None


def test_gateway_confirmation_covers_retained_revisions_without_dispatching_inference() -> None:
    verifier, state, requests = gateway_availability_fixture()
    with verifier.client:
        coverage = verifier.verify("a" * 64, [])
        assert len(coverage.serving_surfaces) == 8
        assert verifier.confirmation_plan is not None
        result = verifier.confirm(
            verifier.confirmation_plan, state, "a" * 64, [], lambda _ids: {state.model_id: state}
        )
    assert result.status == "verified" and result.observed_enabled is False
    assert {request.method for request in requests} == {"GET", "HEAD"}
    assert {request.url.path for request in requests if request.method == "HEAD"} == {
        "/llm;rev=1/responses",
        "/llm;rev=2/responses",
        "/llm;rev=1/models",
        "/llm;rev=2/models",
    }


@pytest.mark.parametrize(
    "corruption",
    [
        "target",
        "revision",
        "digest",
        "kernel",
        "cached",
        "stale_catalog",
        "missing_revision_policy",
        "missing_diagnostic",
        "missing_probe",
    ],
)
def test_gateway_confirmation_refuses_incomplete_or_wrong_evidence(corruption: str) -> None:
    verifier, state, _requests = gateway_availability_fixture(corruption)
    with verifier.client, pytest.raises(ModelAvailabilityUnavailable):
        verifier.verify("a" * 64, [])
        assert verifier.confirmation_plan is not None
        verifier.confirm(
            verifier.confirmation_plan, state, "a" * 64, [], lambda _ids: {state.model_id: state}
        )


@pytest.mark.parametrize("native_state", ["removed", "detached", "attached", "ambiguous"])
def test_retired_gateway_requires_fresh_native_association_revocation(native_state: str) -> None:
    verifier, _state, requests = gateway_availability_fixture()
    original = verifier.list_resources
    native_reads = []

    def inventory(path: str) -> list[dict[str, Any]]:
        if path == "/gateways":
            native_reads.append(path)
            return [] if native_state == "removed" else [{"name": "unit-node"}]
        if path == "/gateways/unit-node/apis":
            native_reads.append(path)
            return [] if native_state == "detached" else [
                {"name": "unit-api;rev=1"} if native_state == "attached" else {},
            ]
        return original(path)

    verifier.list_resources = inventory
    deployments = [{
        "id": str(UUID(int=5)), "kind": "self_hosted",
        "native_gateway_id": "unit-node", "retired_at": "2026-01-01T00:00:00Z",
    }]
    with verifier.client:
        if native_state in {"removed", "detached"}:
            verifier.verify("a" * 64, deployments)
            assert verifier.confirmation_plan is not None
            assert verifier.confirmation_plan.retired_deployments == (UUID(int=5),)
            assert native_reads.count("/gateways") == 2
        else:
            with pytest.raises(ModelAvailabilityUnavailable):
                verifier.verify("a" * 64, deployments)
            assert not requests


def test_retired_but_associated_gateway_remains_subject_to_complete_node_enrollment() -> None:
    verifier, _state, _requests = gateway_availability_fixture()
    verifier.list_resources = lambda path: (
        [{"name": "unit-node"}] if path == "/gateways" else [{"name": "unit-api;rev=1"}]
    )
    deployment = {
        "id": str(UUID(int=5)), "kind": "self_hosted", "native_gateway_id": "unit-node",
        "retired_at": "2026-01-01T00:00:00Z",
    }
    with verifier.client:
        with pytest.raises(ModelAvailabilityUnavailable, match="Retired Gateway still serves"):
            verifier._retired_deployments([deployment])
        with pytest.raises(ModelAvailabilityUnavailable):
            verifier._enrollments([deployment])
    assert _requests == []


@pytest.mark.parametrize("expired", [False, True])
def test_node_lease_never_outlives_its_management_evidence(
    expired: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Mock(spec=TableStorageLedger)
    saved: dict[str, Any] = {}

    def read(_partition: str, _row: str) -> tuple[dict[str, Any], str] | None:
        return (saved, '"unit"') if saved else None

    def replace(_partition: str, _row: str, entity: dict[str, Any], _etag: str | None) -> bool:
        saved.update(entity)
        return True

    store.read_versioned_entity.side_effect = read
    store.replace_if_match.side_effect = replace
    with httpx.Client() as client:
        verifier = ModelAvailabilityNodeVerifier(
            client, SecretStr("unit"), store, "https://unit.table.core.windows.net", "Unit",
        )
        monkeypatch.setattr(verifier, "attest", Mock())
        now = datetime.now(UTC).replace(microsecond=0)
        lease = ModelAvailabilityNodeLease(
            target_id=UUID(int=12), api_id="unit-api", revision="1",
            native_gateway_id="edge", instance_id="unit-node", capability_digest="a" * 64,
            issued_at=now, expires_at=now + timedelta(seconds=120),
        )
        valid_until = now + timedelta(seconds=-1 if expired else 30)
        if expired:
            with pytest.raises(ModelAvailabilityUnavailable, match="expired"):
                verifier.verify("https://unit.example/llm", lease, {"/responses": "b" * 64},
                                valid_until=valid_until)
            store.replace_if_match.assert_not_called()
        else:
            result = verifier.verify(
                "https://unit.example/llm", lease, {"/responses": "b" * 64},
                valid_until=valid_until,
            )
            assert result.expires_at == valid_until
            assert ModelAvailabilityNodeLease.from_entity(saved) == result


@pytest.mark.parametrize("protocol_fenced", [False, True])
@pytest.mark.parametrize("defect", [None, "status", "marker", "probe", "api", "revision",
                                  "gateway", "instance", "cache"])
def test_retired_and_fenced_native_proofs_require_exact_denial_identity(
    protocol_fenced: bool, defect: str | None,
) -> None:
    verifier, _state, _requests = gateway_availability_fixture()
    with verifier.client:
        verifier.verify("a" * 64, [])
    assert verifier.confirmation_plan is not None
    check = verifier.confirmation_plan.catalogs[0]
    prefix = "x-turnstile-protocol-fence-" if protocol_fenced else "x-turnstile-retired-"
    marker = "x-turnstile-protocol-fenced" if protocol_fenced else "x-turnstile-model-retired"
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        headers = {
            "Cache-Control": "no-store", marker: "1",
            prefix + "probe": request.headers["x-turnstile-availability-probe"],
            prefix + "api": "unit-api", prefix + "revision": check.revision,
            prefix + "gateway": "managed", prefix + "instance": "managed",
        }
        if defect == "marker":
            headers[marker] = "wrong"
        elif defect == "cache":
            headers["Cache-Control"] = "public"
        elif defect and defect != "status":
            headers[prefix + defect] = "wrong"
        return httpx.Response(
            200 if defect == "status" else 503 if protocol_fenced else 403, headers=headers,
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        verifier.client = client
        verify = (verifier._check_protocol_fenced_surface if protocol_fenced
                  else verifier._check_retired_surface)
        if defect:
            with pytest.raises(ModelAvailabilityUnavailable):
                verify(check, "/responses", lambda: None)
        else:
            verify(check, "/responses", lambda: None)
    assert len(sent) == 1 and sent[0].method == "HEAD"


@pytest.mark.parametrize("defect", [None, "scope", "revision", "current", "duplicate", "empty"])
def test_empty_revision_endpoint_uses_complete_identity_checked_api_inventory(
    defect: str | None,
) -> None:
    verifier, _state, _requests = gateway_availability_fixture()
    original = verifier.list_resources
    verifier.resource_id = (
        "/subscriptions/unit/resourceGroups/unit/providers/Microsoft.ApiManagement/service/unit"
    )
    revisions = original("/apis/unit-api/revisions")
    revision = revisions[0].get(
        "apiRevision", revisions[0].get("properties", {}).get("apiRevision"),
    )
    assert revision
    entry: dict[str, Any] = {
        "name": "unit-api",
        "id": verifier.resource_id + "/apis/unit-api",
        "properties": {"apiRevision": revision, "isCurrent": True},
    }
    if defect == "scope":
        entry["id"] = "/foreign/apis/unit-api"
    elif defect == "revision":
        entry["name"] = "unit-api;rev=other"
        entry["id"] = verifier.resource_id + "/apis/unit-api;rev=other"
    elif defect == "current":
        entry["properties"]["isCurrent"] = False
    entries = [] if defect == "empty" else [entry, entry] if defect == "duplicate" else [entry]

    def inventory(path: str) -> list[dict[str, Any]]:
        if path == "/apis/unit-api/revisions":
            return []
        if path == "/apis?includeRevisions=true":
            return [{"name": "other-api", "properties": {}}, *entries]
        return original(path)

    verifier.list_resources = inventory
    with verifier.client:
        with pytest.raises(ModelAvailabilityUnavailable, match="revision inventory is empty"):
            verifier.verify("a" * 64, [])
        assert verifier.confirmation_plan is None
        assert _requests == []


def test_node_attestation_does_not_follow_redirects_or_publish_a_lease() -> None:
    store = Mock(spec=TableStorageLedger)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(302, headers={"Location": "https://other.example"}),
        )
    ) as client:
        verifier = ModelAvailabilityNodeVerifier(
            client, SecretStr("unit"), store, "https://unit.table.core.windows.net", "Unit"
        )
        now = datetime.now(UTC)
        lease = ModelAvailabilityNodeLease(
            target_id=UUID(int=12),
            api_id="unit-api",
            revision="1",
            native_gateway_id="unit",
            instance_id="node-one",
            capability_digest="a" * 64,
            issued_at=now,
            expires_at=now + timedelta(seconds=120),
        )
        with pytest.raises(ModelAvailabilityUnavailable, match="attestation"):
            verifier.verify("https://node.example/llm", lease, {"/responses": "b" * 64})
    store.replace_if_match.assert_not_called()


@pytest.mark.parametrize("conflict", [False, True])
def test_publisher_persists_exact_confirmation_plan_with_conditional_readback(
    monkeypatch: pytest.MonkeyPatch,
    conflict: bool,
) -> None:
    verifier, state, _requests = gateway_availability_fixture()
    store = MagicMock(spec=TableStorageLedger)
    store.__enter__.return_value = store
    entities: dict[tuple[str, str], dict[str, Any]] = {}

    def read(partition: str, key: str) -> tuple[dict[str, Any], str] | None:
        value = entities.get((partition, key))
        return (value, '"unit"') if value else None

    def replace(partition: str, key: str, value: dict[str, Any], etag: str | None) -> bool:
        assert etag is None
        if conflict:
            return False
        entities[partition, key] = value
        return True

    store.read_versioned_entity.side_effect = read
    store.replace_if_match.side_effect = replace
    monkeypatch.setattr(
        "turnstile_core.integrations.apim_publisher_client.TableStorageLedger",
        Mock(return_value=store),
    )
    monkeypatch.setattr(
        "turnstile_core.integrations.apim_publisher_client.ModelAvailabilityGatewayVerifier",
        Mock(return_value=verifier),
    )
    settings = Settings(
        azure_subscription_id=str(UUID(int=20)),
        apim_resource_group="unit",
        apim_service_name="unit",
        apim_api_id="unit-api",
        apim_gateway_url="https://unit.azure-api.net/llm",
        apim_probe_subscription_key=SecretStr("unit"),
    )
    publisher = AzureApimPublisherClient(settings, token_provider=Mock(), client=verifier.client)
    with verifier.client:
        if conflict:
            with pytest.raises(ModelAvailabilityUnavailable, match="lost its version"):
                publisher.verify_model_availability(
                    UUID(int=12),
                    "a" * 64,
                    [],
                    "https://unit.table.core.windows.net",
                    "Unit",
                    lambda _model: state,
                    before_proof=lambda: None,
                )
            assert not entities
        else:
            coverage = publisher.verify_model_availability(
                UUID(int=12),
                "a" * 64,
                [],
                "https://unit.table.core.windows.net",
                "Unit",
                lambda _model: state,
                before_proof=lambda: None,
            )
            result = publisher.confirm_model_availability(
                UUID(int=12),
                state,
                coverage,
                [],
                "https://unit.table.core.windows.net",
                "Unit",
                lambda _ids: {state.model_id: state},
                node_enrollment_reader=lambda: [],
            )
            assert result.status == "verified"
            with pytest.raises(ModelAvailabilityUnavailable, match="does not match"):
                publisher.confirm_model_availability(
                    UUID(int=12),
                    state,
                    coverage.model_copy(update={"policy_digest": "e" * 64}),
                    [],
                    "https://unit.table.core.windows.net",
                    "Unit",
                    lambda _ids: {state.model_id: state},
                    node_enrollment_reader=lambda: [],
                )
    store.__exit__.assert_called()


@pytest.mark.parametrize("renew", [False, True])
def test_coordinator_refresh_bounds_parallel_targets_and_holds_exact_proof_guards(
    renew: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    verifier, _state, _requests = gateway_availability_fixture()
    with verifier.client:
        baseline = verifier.verify("a" * 64, [])
    targets = {UUID(int=index): "a" * 64 for index in range(12, 18)}
    repository, clients = MagicMock(), Mock()
    repository.get_model_availability_inventory.return_value = targets
    repository.list_gateway_deployments.return_value = []
    client = Mock(spec=AzureApimPublisherClient)
    clients.resolve.return_value = Mock(client=client)
    coordinator = ModelAvailabilityCoordinator(
        repository, clients, "https://unit.table.core.windows.net", "Unit",
    )
    barrier, lock = Barrier(3), Lock()
    held: set[UUID] = set()
    maximum = 0
    guard_modes = []

    @contextmanager
    def guard(target: UUID, *, wait: bool) -> Iterator[None]:
        nonlocal maximum
        with lock:
            assert target not in held
            held.add(target)
            guard_modes.append(wait)
            maximum = max(maximum, len(held))
        try:
            yield
        finally:
            with lock:
                held.remove(target)

    def check(target: UUID, *_args: Any, **kwargs: Any) -> ModelAvailabilityCoverage:
        if not renew:
            assert target not in held
            kwargs["before_proof"]()
            assert kwargs["state_batch_reader"] == repository.get_model_availability_states
        assert target in held
        barrier.wait(timeout=5)
        if target == UUID(int=13):
            raise ModelAvailabilityUnavailable("Test target unavailable")
        return baseline.model_copy(update={"target_id": target})

    def save(value: ModelAvailabilityCoverage) -> bool:
        assert value.target_id in held
        return True

    repository.model_availability_proof_guard.side_effect = guard
    repository.save_model_availability_coverage.side_effect = save
    client.verify_model_availability.side_effect = check
    client.renew_model_availability.side_effect = check
    assert coordinator.refresh(renew=renew) == 5
    assert maximum == 3 and not held
    assert guard_modes == [not renew] * 6
    assert repository.save_model_availability_coverage.call_count == 5
    assert "error=ModelAvailabilityUnavailable" in caplog.text
    assert "locations=" in caplog.text and "test_model_availability.py:" in caplog.text
    assert ":check" in caplog.text
    assert "reason=Test target unavailable" in caplog.text
    if renew:
        client.verify_model_availability.assert_not_called()
    else:
        client.renew_model_availability.assert_not_called()


def test_coordinator_confirm_is_parallel_and_preserves_target_order() -> None:
    verifier, state, _requests = gateway_availability_fixture()
    with verifier.client:
        baseline = verifier.verify("a" * 64, [])
    targets = {UUID(int=index): "a" * 64 for index in range(12, 18)}
    repository, clients = MagicMock(), Mock()
    repository.list_gateway_deployments.return_value = []
    repository.get_model_availability_coverage.side_effect = (
        lambda target: baseline.model_copy(update={"target_id": target})
    )
    client = Mock(spec=AzureApimPublisherClient)
    clients.resolve.return_value = Mock(client=client)
    barrier = Barrier(3)

    def confirm(target: UUID, *_args: Any, **_kwargs: Any) -> ModelAvailabilityTargetResult:
        barrier.wait(timeout=5)
        return ModelAvailabilityTargetResult(
            target_id=target, inventory_digest="a" * 64, policy_digest=baseline.policy_digest,
            status="verified", observed_revision=state.revision, observed_enabled=state.enabled,
            verified_at=datetime.now(UTC),
        )

    client.confirm_model_availability.side_effect = confirm
    coordinator = ModelAvailabilityCoordinator(
        repository, clients, "https://unit.table.core.windows.net", "Unit",
    )
    results = coordinator.verify(state, targets)
    assert [result.target_id for result in results] == list(targets)
    assert all(result.status == "verified" for result in results)
    repository.model_availability_proof_guard.assert_not_called()


def test_availability_management_pacer_reserves_bounded_shared_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(
        "turnstile_core.integrations.apim_publisher_client.monotonic", lambda: 1000.0,
    )
    monkeypatch.setattr(
        "turnstile_core.integrations.apim_publisher_client.Event",
        lambda: Mock(wait=lambda delay: waits.append(delay)),
    )
    pacer = _AvailabilityManagementReadPacer()
    for _index in range(10):
        pacer.wait()
    assert waits == [index / 16 for index in range(1, 10)]


@pytest.mark.parametrize("full_audit", [False, True])
def test_invalid_confirmation_plan_can_only_be_replaced_after_a_full_audit(
    full_audit: bool,
) -> None:
    verifier, _state, _requests = gateway_availability_fixture()
    with verifier.client:
        verifier.verify("a" * 64, [])
    assert verifier.confirmation_plan is not None
    plan = verifier.confirmation_plan
    store = Mock(spec=TableStorageLedger)
    store.read_versioned_entity.side_effect = [
        ({"SchemaVersion": 0}, '"prior"'), (plan.entity(), '"next"'),
    ]
    store.replace_if_match.return_value = True
    if full_audit:
        AzureApimPublisherClient._save_availability_plan(store, plan, full_audit=True)
        assert store.replace_if_match.call_args.args[-1] == '"prior"'
    else:
        with pytest.raises(ModelAvailabilityUnavailable, match="full audit"):
            AzureApimPublisherClient._save_availability_plan(store, plan)
        store.replace_if_match.assert_not_called()


@pytest.mark.parametrize("newer_management", [False, True])
def test_confirmation_write_cannot_replace_more_recent_management_or_native_proof(
    newer_management: bool,
) -> None:
    verifier, _state, _requests = gateway_availability_fixture()
    with verifier.client:
        verifier.verify("a" * 64, [])
    assert verifier.confirmation_plan is not None
    plan = verifier.confirmation_plan
    payload = plan.model_dump()
    payload["coverage"]["verified_at"] += timedelta(seconds=2)
    payload["coverage"]["expires_at"] += timedelta(seconds=2)
    if newer_management:
        payload["management_verified_at"] += timedelta(seconds=1)
    previous = ModelAvailabilityConfirmationPlan.model_validate(payload)
    store = Mock(spec=TableStorageLedger)
    store.read_versioned_entity.return_value = (previous.entity(), '"prior"')
    with pytest.raises(ModelAvailabilityUnavailable, match="advanced"):
        AzureApimPublisherClient._save_availability_plan(store, plan, full_audit=True)
    store.replace_if_match.assert_not_called()


def test_coordinator_preserves_unknown_target_and_includes_retired_deployments() -> None:
    verifier, state, _requests = gateway_availability_fixture()
    with verifier.client:
        coverage = verifier.verify("a" * 64, [])
    repository, clients = Mock(), Mock()
    client = Mock(spec=AzureApimPublisherClient)
    clients.resolve.return_value = Mock(client=client)
    repository.get_model_availability_coverage.side_effect = (
        lambda target: coverage if target == UUID(int=12) else None
    )
    repository.list_gateway_deployments.return_value = [{"retired_at": "earlier"}]
    client.confirm_model_availability.return_value = Mock(target_id=UUID(int=12), status="verified")
    coordinator = ModelAvailabilityCoordinator(
        repository, clients, "https://unit.table.core.windows.net", "Unit"
    )
    results = coordinator.verify(state, {UUID(int=12): "a" * 64, UUID(int=13): "b" * 64})
    assert [result.status for result in results] == ["verified", "unknown"]
    assert results[1].target_id == UUID(int=13)
    assert results[1].reason == "model_availability_capability_unverified"
    clients.resolve.assert_called_once_with(UUID(int=12))
    repository.list_gateway_deployments.assert_called_once_with(UUID(int=12), include_retired=True)


@pytest.mark.parametrize(
    "values,next_link",
    [
        ([{}, {}], None),
        ([{}, None], None),
        ([{}], "https://management.azure.com/subscriptions/unit/next"),
    ],
)
def test_availability_arm_inventory_never_returns_partial_bounded_results(
    values: list[Any],
    next_link: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = AzureApimPublisherClient.__new__(AzureApimPublisherClient)
    monkeypatch.setattr(
        publisher,
        "_request",
        Mock(return_value=httpx.Response(200, json={"value": values, "nextLink": next_link})),
    )
    with pytest.raises(PolicyCompilationError, match="bounded"):
        publisher._list_all("/apis/unit-api/revisions", max_items=1, max_pages=1)


def test_publication_compiler_binds_all_diagnostics_to_the_same_immutable_snapshot() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository, image_generation_enabled=True).publish(
        bedrock_publication(), "unit-owner"
    )
    original = ApimPolicyCompiler().compile(publication)
    snapshot = ModelAvailabilityPublication.capture(publication, repository.registry(), "unit-api")
    publication = publication.model_copy(
        update={
            "resource_manifest": {
                **publication.resource_manifest,
                "model_availability": snapshot.model_dump(mode="json"),
            }
        }
    )
    compiled = ApimPolicyCompiler().compile(publication)
    assert compiled.backends == original.backends
    assert compiled.named_values == original.named_values
    assert compiled.policy_sha256 != original.policy_sha256
    assert compiled == ApimPolicyCompiler().compile(publication)
    policies = {
        "chat-completions": compiled.chat_completions_policy,
        "responses": compiled.responses_policy,
        "responses-compact": compiled.responses_compact_policy,
        "anthropic-messages": compiled.messages_policy,
        "anthropic-count-tokens": compiled.count_tokens_policy,
        "anthropic-models": compiled.models_policy,
    }
    assert len(compiled.availability_probe_policies) == 6
    assert all(operation.method == "HEAD" for operation in compiled.operations)
    for name, source in compiled.availability_probe_policies:
        assert source == policies[name.removeprefix("availability-probe-")]
        assert inspect_availability_operation(
            source, publication.gateway_profile_id, "unit-api", snapshot.revision,
            catalog=name == "availability-probe-anthropic-models",
        )
    assert inspect_availability_operation(
        compiled.models_policy,
        publication.gateway_profile_id,
        "unit-api",
        snapshot.revision,
        catalog=True,
    )
    corrupted = snapshot.model_copy(update={"spec_digest": "0" * 64})
    with pytest.raises(ModelAvailabilityConflict):
        ApimPolicyCompiler().compile(
            publication.model_copy(
                update={
                    "resource_manifest": {
                        "model_availability": corrupted.model_dump(mode="json"),
                    }
                }
            )
        )


def test_public_parent_validation_requires_matching_availability_installation() -> None:
    original = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    enabled = compose_availability_parent(original)
    assert validate_parent_policy(enabled, enabled)
    for installed, canonical in ((original, enabled), (enabled, original)):
        with pytest.raises(PolicyCompilationError, match="compatible canonical"):
            validate_parent_policy(installed, canonical)
    corrupted = parse_policy(enabled)
    limit = next(corrupted.iter("llm-token-limit"))
    parent = next(node for node in corrupted.iter() if limit in list(node))
    parent.remove(limit)
    with pytest.raises(PolicyCompilationError):
        validate_parent_policy(serialize_policy(corrupted), enabled)


@pytest.mark.parametrize("budget_enabled", [False, True])
def test_publication_worker_projects_before_probes_and_recovers_promotion_read_only(
    monkeypatch: pytest.MonkeyPatch,
    budget_enabled: bool,
) -> None:
    repository, client = InMemoryRepository(), FakeApimClient()
    repository.models = []
    publication = GatewayControlPlaneService(repository, image_generation_enabled=True).publish(
        bedrock_publication(), "unit-owner"
    )
    canonical = client.policy
    if budget_enabled:
        canonical = compose_budget_execution_parent(canonical, canonical, enabled=True)
    client.policy = compose_availability_parent(canonical)
    monkeypatch.setattr(repository, "model_availability_supported", lambda: True)
    monkeypatch.setattr(repository, "get_apim_target", lambda _target: {"api_id": "unit-api"})
    candidate_revision = f"turnstile-{publication.generation}-{publication.id.hex[:8]}"
    monkeypatch.setattr(repository, "get_model_availability_inventory", lambda: {
        publication.gateway_profile_id: "a" * 64,
    }, raising=False)
    monkeypatch.setattr(repository, "get_model_availability_coverage", lambda _target: coverage(
        target_id=publication.gateway_profile_id,
        serving_surfaces=(f"managed/{candidate_revision}/responses",),
        verified_surfaces=(f"managed/{candidate_revision}/responses",),
        verified_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=120),
    ), raising=False)
    staging = Mock()
    monkeypatch.setattr(repository, "stage_model_availability", staging, raising=False)
    projector = Mock(
        side_effect=lambda model_id: ModelAvailabilityState(
            model_id=model_id,
            revision=2,
            enabled=False,
        )
    )
    probe = Mock(wraps=client.probe_revision)
    monkeypatch.setattr(client, "probe_revision", probe)
    fence = {
        "protocol_version": 2,
        "mode": "active",
        "epoch": str(UUID(int=2)),
        "gateway_profile_id": str(publication.gateway_profile_id),
        "model_contracts": {},
    }
    worker = GatewayPublicationWorker(
        repository,
        client,
        canonical,
        availability_projector=projector,
        budget_configuration=lambda _target: fence,
    )
    for expected in ("validating", "provisioning", "building_revision", "verifying", "promoting"):
        result = worker.run_once("unit-worker")
        assert result is not None and result.status.value == expected, result
    assert result is not None and result.apim_revision is not None
    snapshot = ModelAvailabilityPublication.model_validate(
        result.resource_manifest["model_availability"]
    )
    assert probe.call_args.args[1].resource_manifest["availability_probe_states"] == {
        alias: False for alias in snapshot.models
    }
    assert ("budget_contracts" in probe.call_args.args[1].resource_manifest) is budget_enabled
    assert (
        len([name for name in client.operation_policies if name.startswith("availability-probe-")])
        == 6
    )
    assert projector.call_count == len(snapshot.models)
    staging.assert_called()
    client.current = result.apim_revision
    client.calls.clear()
    projector.reset_mock()
    staging.reset_mock()
    probe.reset_mock()
    result = worker.run_once("unit-worker")
    assert result is not None and result.status.value == "active"
    assert client.permission_checks[-1] is True
    assert client.calls == [("parent-readback", result.apim_revision)]
    assert [item.args for item in projector.call_args_list] == [
        (model_id,) for model_id in snapshot.models.values()
    ]
    assert [item.args for item in staging.call_args_list] == [
        (model_id, result.id) for model_id in snapshot.models.values()
    ]
    probe.assert_not_called()


def test_publication_worker_rejects_uninstalled_availability_before_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, client = InMemoryRepository(), FakeApimClient()
    GatewayControlPlaneService(repository, image_generation_enabled=True).publish(
        bedrock_publication(), "unit-owner",
    )
    monkeypatch.setattr(repository, "model_availability_supported", lambda: True)
    worker = GatewayPublicationWorker(
        repository, client, client.policy, availability_projector=Mock()
    )
    result = worker.run_once("unit-worker")
    assert result is not None and result.status.value == "failed"
    assert "installed model availability" in str(result.error_message)
    assert all(name == "current-policy" for name, _value in client.calls)
    assert not client.backends and not client.named_values


@pytest.mark.parametrize("kind", ["chat", "messages", "images"])
@pytest.mark.parametrize(
    "status,code", [(403, "model_disabled"), (403, "model_not_assigned"), (200, "model_disabled")]
)
def test_publication_probe_requires_exact_disabled_contract_without_normal_inference(
    kind: str,
    status: int,
    code: str,
) -> None:
    repository = InMemoryRepository()
    repository.models = []
    request = (
        image_publication(repository)
        if kind == "images"
        else foundry_publication()
        if kind == "chat"
        else bedrock_publication()
    )
    if kind == "chat":
        request.provider = ProviderTarget(
            existing_id=next(
                row["id"] for row in repository.providers if row["brand_key"] == "microsoft_foundry"
            )
        )
    publication = GatewayControlPlaneService(
        repository, apim_principal_id="unit", image_generation_enabled=True,
    ).publish(
        request, "unit-owner"
    )
    snapshot = ModelAvailabilityPublication.capture(publication, repository.registry(), "unit-api")
    publication = publication.model_copy(
        update={
            "resource_manifest": {
                **publication.resource_manifest,
                "model_availability": snapshot.model_dump(mode="json"),
                "availability_probe_states": {alias: False for alias in snapshot.models},
            }
        }
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "model": publication.desired_spec.bindings[-1].model.model_key
        }
        return httpx.Response(status, json={"error": {"code": code}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        settings = publisher_settings().model_copy(
            update={"apim_probe_subscription_key": SecretStr("unit")}
        )
        client = AzureApimPublisherClient(settings, token_provider=Mock(), client=transport)
        def check_disabled() -> None:
            if kind == "images":
                client._probe_disabled_model(
                    str(settings.apim_gateway_url).rstrip("/"), {},
                    publication.desired_spec.bindings[-1],
                )
            else:
                client.probe_revision(snapshot.revision, publication)
        if status == 403 and code == "model_disabled":
            check_disabled()
        else:
            with pytest.raises(PolicyCompilationError, match="Disabled model"):
                check_disabled()
    assert [request.method for request in requests] == (
        ["POST"] if kind == "images" else ["GET", "POST"]
    )


def test_rollback_keeps_current_disabled_state_and_rejects_unenforced_old_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, client = InMemoryRepository(), FakeApimClient()
    publication = GatewayControlPlaneService(repository, image_generation_enabled=True).publish(
        bedrock_publication(), "unit-owner"
    )
    snapshot = ModelAvailabilityPublication.capture(publication, repository.registry(), "unit-api")
    canonical = client.policy
    client.policy = compose_availability_parent(canonical)
    client.api_policies[snapshot.revision] = client.policy
    publication = publication.model_copy(
        update={
            "apim_revision": snapshot.revision,
            "resource_manifest": {
                "model_availability": snapshot.model_dump(mode="json"),
                "parent_policy_contract_sha256": component_digest(
                    parse_policy(client.policy), normalize_text_defaults=False
                ),
            },
        }
    )
    monkeypatch.setattr(repository, "model_availability_supported", lambda: True)
    worker = GatewayReleaseOperationWorker(
        repository,
        client,
        parent_policy=canonical,
        availability_projector=lambda model_id: ModelAvailabilityState(
            model_id=model_id, revision=3, enabled=False
        ),
    )
    prepared = worker._validate_rollback_parent(publication, client)
    assert prepared.resource_manifest["availability_probe_states"] == {
        alias: False for alias in snapshot.models
    }
    client.policy = canonical
    client.api_policies[snapshot.revision] = canonical
    with pytest.raises(PolicyCompilationError, match="cannot remove"):
        worker._validate_rollback_parent(publication, client)
    assert client.calls == [("parent-readback", snapshot.revision)] * 2


def test_general_model_save_preserves_omitted_enabled_and_rejects_explicit_toggle(
    tmp_path: Path,
) -> None:
    repository = InMemoryRepository()
    model = next(row for row in repository.models if str(row["id"]).endswith("000000000003"))
    model["enabled"] = False
    service = ModelRuntimeService(repository, Settings(credential_key_file=tmp_path / "unit.key"))
    values = {
        key: value
        for key, value in model.items()
        if key in ManagedModelWrite.model_fields and key != "enabled"
    }
    values["display_name"] = "Updated model name"
    saved = service.save_model(ManagedModelWrite.model_validate(values), model["id"])
    actual = next(row for row in saved.models if row.id == model["id"])
    assert actual.enabled is False and actual.display_name == values["display_name"]
    with pytest.raises(HTTPException) as rejected:
        service.save_model(
            ManagedModelWrite.model_validate(values | {"enabled": True}), model["id"]
        )
    assert rejected.value.status_code == 409
    assert model["enabled"] is False
    with pytest.raises(ModelAvailabilityConflict):
        repository.update_registry_item("model", model["id"], {"enabled": True})


def test_disabled_explicit_invocation_never_resolves_fallback_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryRepository()
    model = next(row for row in repository.models if str(row["id"]).endswith("000000000003"))
    request = invocation_request().model_copy(
        update={
            "model_id": model["id"],
            "runtime_id": model["runtime_id"],
        }
    )
    model["enabled"] = False
    route = Mock(side_effect=AssertionError("No fallback route may be selected"))
    monkeypatch.setattr(repository, "invocation_route", route)
    service = ModelRuntimeService(repository, Settings(credential_key_file=tmp_path / "unit.key"))
    with pytest.raises(HTTPException) as rejected:
        service.invoke(request)
    assert rejected.value.status_code == 403
    detail = rejected.value.detail
    assert isinstance(detail, dict) and detail["code"] == "model_disabled"
    route.assert_not_called()


def test_postgres_general_update_locks_model_and_never_assigns_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = UUID(int=2)
    conflicting = RecordedAvailabilityRepository([None, {"enabled": False}])
    with pytest.raises(ModelAvailabilityConflict):
        conflicting.update_registry_item(
            "model", model_id, {"display_name": "unit", "enabled": True}
        )
    assert "FOR UPDATE" in conflicting.calls[1][0]
    assert not any(query.startswith("UPDATE") for query, _parameters in conflicting.calls)
    repository = RecordedAvailabilityRepository(
        [
            None,
            {"enabled": False},
            None,
            {"id": model_id, "enabled": False, "display_name": "unit"},
        ]
    )
    invalidation = Mock()
    monkeypatch.setattr(repository, "_invalidate_identities", invalidation)
    result = repository.update_registry_item(
        "model", model_id, {"display_name": "unit", "enabled": False}
    )
    assert result is not None and result["enabled"] is False
    update = next(query for query, _parameters in repository.calls if query.startswith("UPDATE"))
    assert "display_name =" in update and "enabled =" not in update
    invalidation.assert_called_once()


def availability_installer_fixture(
    failure: str | None = None, *, route_mode: str = "ordinary",
) -> tuple[AvailabilityInstaller, list[httpx.Request], dict[str, Any]]:
    parent = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    inference = (
        '<policies><inbound><base /><set-backend-service backend-id="unit" />'
        "</inbound><backend><base /></backend><outbound><base /></outbound>"
        "<on-error><base /></on-error></policies>"
    )
    catalog = (
        '<policies><inbound><base /><return-response><set-status code="200" reason="OK" />'
        '<set-body>{"data":[{"id":"unit-model"}]}</set-body></return-response>'
        "</inbound><backend><base /></backend><outbound><base /></outbound>"
        "<on-error><base /></on-error></policies>"
    )
    resources: dict[str, Any] = {
        "/apis/unit-api/revisions": {"value": [{"apiRevision": "1"}, {"apiRevision": "2"}]},
        "/apis/unit-api/products": {"value": []},
    }
    adoptions = []
    for revision in ("1", "2"):
        original = {
            "parent": parent,
            "responses": INHERITED_OPERATION_POLICY if route_mode == "inherited" else inference,
            "models": catalog,
        }
        adoptions.append(
            ModelAvailabilityPolicyAdoption(
                adoption_id=UUID(int=int(revision)),
                target_id=UUID(int=12),
                api_id="unit-api",
                revision=revision,
                models={"unit-model": UUID(int=2)},
                serving_gateways={"unit-model": None},
                original_policy_digests={
                    name: hashlib.sha256(source.encode()).hexdigest()
                    for name, source in original.items()
                },
                fixed_operations={"responses": "unit-model"}
                if route_mode in {"fixed", "retired"} else {},
                inherited_operations=("responses",) if route_mode == "inherited" else (),
                retired_operations=("responses",) if route_mode == "retired" else (),
            )
        )
        prefix = f"/apis/unit-api;rev={revision}"
        resources[prefix + "/policies/policy"] = {"properties": {"value": parent}}
        resources[prefix + "/operations"] = {
            "value": [
                {
                    "name": "responses",
                    "properties": {
                        "method": "POST",
                        "urlTemplate": "/unit-model/invoke"
                        if route_mode in {"fixed", "retired"} else "/responses",
                    },
                },
                {"name": "models", "properties": {"method": "GET", "urlTemplate": "/models"}},
            ]
        }
        for name, source in (("responses", inference), ("models", catalog)):
            if name == "responses" and route_mode == "inherited":
                continue
            resources[prefix + f"/operations/{name}/policies/policy"] = {
                "properties": {"value": source}
            }
    if failure == "revision":
        resources["/apis/unit-api/revisions"]["value"].pop()
    elif failure == "pagination":
        resources["/apis/unit-api/revisions"]["nextLink"] = "https://management.azure.com/next"
    elif failure == "source":
        resources["/apis/unit-api;rev=1/policies/policy"]["properties"]["value"] += " "
    elif failure == "operation":
        resources["/apis/unit-api;rev=1/operations"]["value"][0]["properties"]["method"] = "GET"
    scope = AvailabilityInstallation.model_validate(
        {
            "subscription_id": str(UUID(int=20)),
            "resource_group": "unit",
            "service_name": "unit",
            "target_id": str(UUID(int=12)),
            "api_id": "unit-api",
            "table_endpoint": "https://unit.table.core.windows.net",
            "table_name": "Unit",
            "revisions": adoptions,
        }
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "management.azure.com"
        assert request.url.path.startswith(scope.service_path + "/")
        path = request.url.path.removeprefix(scope.service_path)
        if request.method == "PUT":
            if failure == "lost_before_commit":
                raise httpx.ReadTimeout("Unit request was not applied", request=request)
            if path in resources:
                assert request.headers["If-Match"] == '"base"'
            else:
                assert request.headers["If-None-Match"] == "*"
            resources[path] = json.loads(request.content)
            if failure == "readback":
                resources[path]["properties"]["value"] = "<policies />"
            if failure == "lost_response":
                raise httpx.ReadTimeout("Unit accepted write lost its response", request=request)
            return httpx.Response(200, json=resources[path])
        assert request.method == "GET"
        if path not in resources:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json=resources[path],
            headers={
                "ETag": "*" if failure == "etag" else '"base"',
            },
        )

    return (
        AvailabilityInstaller(scope, httpx.Client(transport=httpx.MockTransport(handler))),
        requests,
        resources,
    )


@pytest.mark.parametrize("route_mode", ["fixed", "inherited", "retired"])
def test_installer_binds_explicit_fixed_inherited_and_retired_routes(
    route_mode: str, tmp_path: Path,
) -> None:
    installer, requests, _resources = availability_installer_fixture(route_mode=route_mode)
    with installer.client:
        changes = installer.plan()
        assert len(changes) == 14
        policies = [change for change in changes
                    if change.path.endswith("/operations/responses/policies/policy")]
        assert len(policies) == 2
        for change in policies:
            assert (change.etag is None) is (route_mode == "inherited")
            assert (change.original_sha256 is None) is (route_mode == "inherited")
            root = parse_policy(change.body["properties"]["value"])
            assert is_retired_availability_operation(root) is (route_mode == "retired")
            if route_mode == "fixed":
                fixed = root.find("inbound/set-variable[@name='availabilityFixedModelAlias']")
                assert fixed is not None and fixed.get("value") == "unit-model"
        installer.apply(changes, installer.plan_digest(changes), tmp_path / "installation.jsonl")
    assert len([request for request in requests if request.method == "PUT"]) == 14


def test_installer_refuses_inherited_policy_presence_drift() -> None:
    installer, requests, resources = availability_installer_fixture(route_mode="inherited")
    resources["/apis/unit-api;rev=1/operations/responses/policies/policy"] = {
        "properties": {"value": INHERITED_OPERATION_POLICY},
    }
    with installer.client, pytest.raises(ValueError, match="presence changed"):
        installer.plan()
    assert all(request.method == "GET" for request in requests)


def test_installer_plans_all_revisions_and_conditionally_verifies_every_change(
    tmp_path: Path,
) -> None:
    installer, requests, _resources = availability_installer_fixture()
    journal = tmp_path / "installation.jsonl"
    with installer.client:
        changes = installer.plan()
        assert len(changes) == 14
        assert {request.method for request in requests} == {"GET"}
        installer.apply(changes, installer.plan_digest(changes), journal)
        writes = [request for request in requests if request.method == "PUT"]
        assert len(writes) == len(changes)
        assert {request.url.path.split(";rev=", 1)[1].split("/", 1)[0] for request in writes} == {
            "1",
            "2",
        }
        with pytest.raises(FileExistsError):
            installer.apply(changes, installer.plan_digest(changes), journal)
        assert len([request for request in requests if request.method == "PUT"]) == 14
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [event["event"] for event in events] == ["attempt", "verified"] * 14
    assert journal.stat().st_mode & 0o777 == 0o600
    assert not any("body" in event for event in events)


@pytest.mark.parametrize("failure", ["revision", "pagination", "source", "operation", "etag"])
def test_installer_rejects_incomplete_or_changed_baseline_before_any_write(failure: str) -> None:
    installer, requests, _resources = availability_installer_fixture(failure)
    with installer.client, pytest.raises(ValueError):
        installer.plan()
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize("failure", ["readback", "lost_before_commit"])
def test_installer_records_unknown_write_and_never_replays_it(tmp_path: Path, failure: str) -> None:
    installer, requests, _resources = availability_installer_fixture(failure)
    journal = tmp_path / "installation.jsonl"
    with installer.client:
        changes = installer.plan()
        with pytest.raises((ValueError, httpx.ReadTimeout)):
            installer.apply(changes, installer.plan_digest(changes), journal)
        with pytest.raises(FileExistsError):
            installer.apply(changes, installer.plan_digest(changes), journal)
    assert len([request for request in requests if request.method == "PUT"]) == 1
    assert [json.loads(line)["event"] for line in journal.read_text().splitlines()] == (
        ["attempt", "response_unavailable"] if failure == "lost_before_commit" else ["attempt"]
    )


def test_installer_reads_back_lost_put_response_without_replaying_the_write(tmp_path: Path) -> None:
    installer, requests, _resources = availability_installer_fixture("lost_response")
    journal = tmp_path / "installation.jsonl"
    with installer.client:
        changes = installer.plan()
        installer.apply(changes, installer.plan_digest(changes), journal)
        with pytest.raises(FileExistsError):
            installer.apply(changes, installer.plan_digest(changes), journal)
    writes = [request for request in requests if request.method == "PUT"]
    assert len(writes) == len(changes) == len({request.url.path for request in writes})
    assert [json.loads(line)["event"] for line in journal.read_text().splitlines()] == (
        ["attempt", "response_unavailable", "verified"] * len(changes)
    )


@pytest.mark.parametrize("defect", [None, "unknown_variable", "early_request", "wrong_parent"])
def test_legacy_parent_normalization_moves_only_known_ordered_variables(
    defect: str | None,
) -> None:
    source = (
        '<policies><inbound><choose><when condition="@(true)"><choose>'
        '<set-variable name="budgetRequestCannotFit" value="@(false)" />'
        '<set-variable name="budgetDenialReason" value="unit" />'
        '<when condition="@(false)"><return-response><set-status code="403" />'
        '</return-response></when></choose></when></choose></inbound></policies>'
    )
    if defect == "unknown_variable":
        source = source.replace("budgetDenialReason", "otherVariable")
    elif defect == "early_request":
        source = source.replace('<set-variable name="budgetRequestCannotFit" value="@(false)" />',
                                '<send-request><set-method>GET</set-method></send-request>')
    elif defect == "wrong_parent":
        source = source.replace('<when condition="@(true)">', '<otherwise>', 1)
        source = source.replace('</choose></when></choose>', '</choose></otherwise></choose>')
    if defect:
        with pytest.raises(ValueError, match="unknown legacy"):
            normalize_legacy_parent(source)
    else:
        normalized = normalize_legacy_parent(source)
        assert normalize_legacy_parent(normalized) == normalized
        branch = parse_policy(normalized).find("inbound/choose/when")
        assert branch is not None
        assert [node.tag for node in branch] == ["set-variable", "set-variable", "choose"]
        assert [node.get("name") for node in list(branch)[:2]] == [
            "budgetRequestCannotFit", "budgetDenialReason",
        ]


def test_installer_rejects_unapproved_digest_and_late_source_drift(tmp_path: Path) -> None:
    installer, requests, resources = availability_installer_fixture()
    journal = tmp_path / "installation.jsonl"
    with installer.client:
        changes = installer.plan()
        requests.clear()
        with pytest.raises(ValueError, match="digest changed"):
            installer.apply(changes, "0" * 64, journal)
        assert not journal.exists() and not requests
        resources[changes[0].path]["properties"]["value"] += " "
        with pytest.raises(ValueError, match="baseline changed"):
            installer.apply(changes, installer.plan_digest(changes), journal)
    assert not any(request.method == "PUT" for request in requests)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://unit.table.core.windows.net",
        "https://unit-secondary.table.core.windows.net",
        "https://unit.table.core.windows.net/Unit",
        "https://other.example.test",
    ],
)
def test_installer_requires_primary_table_origin(endpoint: str) -> None:
    installer, _requests, _resources = availability_installer_fixture()
    with installer.client, pytest.raises(ValidationError):
        AvailabilityInstallation.model_validate(
            installer.scope.model_dump() | {"table_endpoint": endpoint}
        )


class RecordedAvailabilityRepository(
    PostgreSqlModelAvailabilityRepositoryMixin,
    PostgreSqlRegistryRepositoryMixin,
):
    def __init__(self, rows: list[Any]) -> None:
        self.rows = iter(rows)
        self.calls: list[tuple[str, Any]] = []
        self.current: Any = None

    @contextmanager
    def _connection(self) -> Iterator[RecordedAvailabilityRepository]:
        yield self

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, sql: str, parameters: Any = ()) -> RecordedAvailabilityRepository:
        self.calls.append((sql, parameters))
        self.current = next(self.rows, None)
        return self

    def fetchone(self) -> Any:
        return self.current

    def fetchall(self) -> Any:
        return self.current or []


@pytest.mark.parametrize("wait", [False, True])
@pytest.mark.parametrize("acquired", [True, False, None])
def test_postgres_proof_guard_serializes_only_the_exact_target(
    wait: bool, acquired: bool | None,
) -> None:
    repository = RecordedAvailabilityRepository([
        None, None, {"acquired": acquired} if acquired is not None else None, None,
    ])
    target = UUID(int=12)
    reached = False
    if not wait and acquired is not True:
        with (
            pytest.raises(ModelAvailabilityUnavailable, match="writer is busy"),
            repository.model_availability_proof_guard(target, wait=wait),
        ):
            reached = True
        assert not reached and len(repository.calls) == 3
    else:
        with repository.model_availability_proof_guard(target, wait=wait):
            reached = True
        assert reached and repository.calls[-1] == ("SELECT 1", ())
    assert repository.calls[:2] == [
        ("SET LOCAL statement_timeout = '95s'", ()),
        ("SET LOCAL lock_timeout = '90s'", ()),
    ]
    statement, parameters = repository.calls[2]
    assert ("pg_advisory_xact_lock" if wait else "pg_try_advisory_xact_lock") in statement
    assert parameters == ("model-availability-proof-writer:" + str(target),)


def test_postgres_command_rejects_stale_revision_before_any_update() -> None:
    repository = RecordedAvailabilityRepository(
        [
            {"id": UUID(int=2), "enabled": True},
            None,
            {"revision": 3, "lifecycle": "active"},
        ]
    )
    with pytest.raises(ModelAvailabilityConflict, match="revision changed"):
        repository.change_model_availability(
            UUID(int=2),
            ModelAvailabilityCommand(
                enabled=False,
                expected_revision=2,
                idempotency_key=UUID(int=7),
            ),
            "owner@example.test",
        )
    assert len(repository.calls) == 3
    assert all(sql.startswith("SELECT") for sql, _ in repository.calls)
    assert "FOR UPDATE" in repository.calls[0][0]


def test_postgres_command_enqueues_exact_intent_before_mutating_enabled() -> None:
    moment = datetime.now(UTC)
    capability = coverage(verified_at=moment, expires_at=moment + timedelta(minutes=5))
    repository = RecordedAvailabilityRepository(
        [
            {"id": UUID(int=2), "enabled": True},
            None,
            {"revision": 1, "lifecycle": "active"},
            None,
            None,
            [{"target_id": capability.target_id, "inventory_digest": capability.inventory_digest}],
            [{"target_id": capability.target_id, "coverage": capability.model_dump()}],
            [{"model_id": UUID(int=2), "target_id": capability.target_id}],
        ]
    )
    result = repository.change_model_availability(
        UUID(int=2),
        ModelAvailabilityCommand(
            enabled=False,
            expected_revision=1,
            idempotency_key=UUID(int=7),
        ),
        "owner@example.test",
    )
    writes = [(sql, values) for sql, values in repository.calls if not sql.startswith("SELECT")]
    assert result.status == "pending" and result.desired_revision == 2
    assert writes[0][0].startswith("UPDATE model_availability_identity")
    assert writes[1][0].startswith("INSERT INTO model_availability_operation")
    assert writes[2][0].startswith("UPDATE managed_model SET enabled")
    assert writes[2][1] == (False, UUID(int=2))
    assert writes[3][0].startswith("INSERT INTO model_availability_task")
    assert writes[4][0].startswith("INSERT INTO model_availability_audit")
    assert not any("is_default" in sql or "price" in sql for sql, _ in writes)


def test_postgres_expired_lease_cannot_complete_a_transition() -> None:
    repository = RecordedAvailabilityRepository(
        [
            {"model_id": UUID(int=2), "revision": 2, "lifecycle": "active", "enabled": False},
            None,
        ]
    )
    assert not repository.finish_model_availability_task(
        UUID(int=2), 2, "old-owner", None, [], None
    )
    assert len(repository.calls) == 2
    assert "lease_expires_at > now()" in repository.calls[1][0]


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": "false"},
        {"enabled": 0},
        {"expected_revision": True},
        {"expected_revision": "1"},
        {"expected_revision": 0},
        {"expected_revision": MAX_AVAILABILITY_REVISION + 1},
        {"force": True},
    ],
)
def test_availability_commands_reject_coercion_and_unversioned_writes(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ModelAvailabilityCommand.model_validate(
            {
                "enabled": False,
                "expected_revision": 1,
                "idempotency_key": UUID(int=1),
                **change,
            }
        )


def test_availability_state_is_immutable_and_round_trips_exact_identity() -> None:
    model_id = UUID(int=2)
    state = ModelAvailabilityState(model_id=model_id, revision=2, enabled=False)
    assert ModelAvailabilityState.from_entity(state.entity(), model_id) == state
    with pytest.raises(ValidationError):
        state.enabled = True
    with pytest.raises(ValidationError):
        ModelAvailabilityState(model_id=model_id, revision=3, enabled=True, lifecycle="deleted")


@pytest.mark.parametrize(
    "change",
    [
        {"ModelId": str(UUID(int=3))},
        {"RowKey": str(UUID(int=3))},
        {"PartitionKey": "other"},
        {"SchemaVersion": True},
        {"SchemaVersion": 2},
    ],
)
def test_availability_projection_rejects_foreign_identity_and_schema(
    change: dict[str, Any],
) -> None:
    state = ModelAvailabilityState(model_id=UUID(int=2), revision=1, enabled=True)
    with pytest.raises(ModelAvailabilityUnavailable):
        ModelAvailabilityState.from_entity({**state.entity(), **change}, state.model_id)


def coverage(**changes: Any) -> ModelAvailabilityCoverage:
    moment = datetime(2026, 9, 27, tzinfo=UTC)
    return ModelAvailabilityCoverage.model_validate(
        {
            "target_id": UUID(int=4),
            "inventory_digest": "a" * 64,
            "policy_digest": "b" * 64,
            "serving_surfaces": ["managed:revision-1", "edge:revision-1"],
            "verified_surfaces": ["managed:revision-1", "edge:revision-1"],
            "verified_at": moment,
            "expires_at": moment + timedelta(seconds=300),
            **changes,
        }
    )


def test_availability_coverage_expires_and_is_bound_to_inventory() -> None:
    evidence = coverage()
    assert evidence.current(evidence.verified_at, "a" * 64)
    assert not evidence.current(evidence.verified_at - timedelta(seconds=1), "a" * 64)
    assert not evidence.current(evidence.expires_at, "a" * 64)
    assert not evidence.current(evidence.verified_at, "c" * 64)


@pytest.mark.parametrize(
    "change",
    [
        {"verified_surfaces": ["managed:revision-1"]},
        {"verified_surfaces": ["managed:revision-1", "managed:revision-1"]},
        {"serving_surfaces": ["*"], "verified_surfaces": ["*"]},
        {"expires_at": datetime(2026, 9, 27, 0, 5, 1, tzinfo=UTC)},
        {"expires_at": datetime(2026, 9, 27, tzinfo=UTC)},
        {"verified_at": datetime(2026, 9, 27)},
        {"read_contract": "cached"},
    ],
)
def test_availability_coverage_requires_complete_fresh_uncached_evidence(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        coverage(**change)


def operation(**changes: Any) -> ModelAvailabilityOperation:
    moment = datetime(2026, 9, 27, tzinfo=UTC)
    return ModelAvailabilityOperation.model_validate(
        {
            "id": UUID(int=6),
            "model_id": UUID(int=2),
            "idempotency_key": UUID(int=7),
            "expected_revision": 1,
            "desired_revision": 2,
            "desired_enabled": False,
            "created_by": "owner@example.test",
            "created_at": moment,
            "updated_at": moment,
            "status": "succeeded",
            "projected_revision": 2,
            "projected_at": moment,
            "completed_at": moment,
            "targets": [
                {
                    "target_id": UUID(int=4),
                    "inventory_digest": "a" * 64,
                    "policy_digest": "b" * 64,
                    "status": "verified",
                    "observed_revision": 2,
                    "observed_enabled": False,
                    "verified_at": moment,
                }
            ],
            **changes,
        }
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "pending"),
        ("observed_revision", 1),
        ("observed_enabled", True),
        ("policy_digest", None),
        ("verified_at", None),
        ("verified_at", datetime(2026, 9, 26, tzinfo=UTC)),
    ],
)
def test_success_requires_fresh_exact_target_readback(field: str, value: object) -> None:
    original = operation()
    result = original.targets[0].model_dump()
    result[field] = value
    with pytest.raises(ValidationError):
        operation(targets=[result])


@pytest.mark.parametrize(
    "change",
    [
        {"targets": []},
        {"projected_revision": 1},
        {"projected_at": None},
        {"completed_at": None},
        {"desired_revision": 3},
    ],
)
def test_success_requires_projection_and_one_step_revision(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        operation(**change)


def test_node_lease_is_identity_bound_and_short_lived() -> None:
    moment = datetime(2026, 9, 27, tzinfo=UTC)
    lease = ModelAvailabilityNodeLease(
        target_id=UUID(int=4),
        api_id="unit-api",
        revision="revision-1",
        native_gateway_id="edge",
        instance_id="node-1",
        capability_digest="a" * 64,
        issued_at=moment,
        expires_at=moment + timedelta(seconds=120),
    )
    assert ModelAvailabilityNodeLease.from_entity(lease.entity()) == lease
    assert lease.current(moment, "a" * 64)
    assert not lease.current(moment, "b" * 64)
    assert not lease.current(lease.expires_at, "a" * 64)
    for change in ({"RowKey": "wrong"}, {"SchemaVersion": True}, {"IssuedEpoch": "01"}):
        with pytest.raises(ModelAvailabilityUnavailable):
            ModelAvailabilityNodeLease.from_entity({**lease.entity(), **change})
    with pytest.raises(ValidationError):
        ModelAvailabilityNodeLease.model_validate(
            {
                **lease.model_dump(),
                "expires_at": moment + timedelta(seconds=121),
            }
        )
