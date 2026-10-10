from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.apim_upgrade import (
    IMAGE_OPERATION_PROPERTIES,
    ApimUpgradeError,
    AzureUpgradeBackend,
    GatewaySnapshot,
    ImageUpgradePlan,
    document_digest,
    execute_image_upgrade,
    plan_image_upgrade,
    policy_digest,
    upgrade_parameters,
    validate_upgrade_what_if,
    verify_upgrade_snapshot,
)
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.integrations.apim_policy_components import (
    CACHE_READ_WITH_FALLBACK,
    CACHE_READ_WITHOUT_FALLBACK,
    IMAGE_CONDITION,
    POOL_RUNTIME_HEADER,
    TEXT_CONDITION,
    parse_policy,
    serialize_policy,
)

CANONICAL_PARENT = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
IMAGE_DENIAL = (REPOSITORY_ROOT / "infra/policies/provider-neutral-images-policy.xml").read_text()
API_ID = (
    "/subscriptions/unit/resourceGroups/customer/providers/"
    "Microsoft.ApiManagement/service/unit/apis/customer-llm"
)


def legacy_parent() -> str:
    root = parse_policy(CANONICAL_PARENT)
    inbound = root.find("inbound")
    assert inbound is not None
    for name in ("isImagesOperation", "imageGenerationPolicyVersion"):
        variable = inbound.find(f"set-variable[@name='{name}']")
        assert variable is not None
        inbound.remove(variable)
    inference = inbound.find("set-variable[@name='isInferenceOperation']")
    assert inference is not None
    inference.set("value", "@(" + inference.get("value", "").split(" || ", 1)[1])
    # v1.1 added two parent-policy elements outside the image feature. A real v1.0
    # gateway carries neither, so a fixture that keeps them is v1.1 wearing a v1.0
    # label and the upgrade regression it guards never runs against the real shape.
    pool_runtime = inbound.find(f"set-header[@name='{POOL_RUNTIME_HEADER}']")
    assert pool_runtime is not None
    inbound.remove(pool_runtime)
    for node in root.iter("set-variable"):
        if node.get("name") != "usagePayload":
            continue
        node.set(
            "value",
            node.get("value", "").replace(CACHE_READ_WITH_FALLBACK, CACHE_READ_WITHOUT_FALLBACK),
        )
    for parent in list(root.iter()):
        for index, child in reversed(list(enumerate(list(parent)))):
            branch = child.find("when") if child.tag == "choose" else None
            if branch is None or branch.get("condition") not in {IMAGE_CONDITION, TEXT_CONDITION}:
                continue
            text = child.find("otherwise") if branch.get("condition") == IMAGE_CONDITION else branch
            assert text is not None
            parent.remove(child)
            for offset, element in enumerate(list(text)):
                parent.insert(index + offset, deepcopy(element))
    return serialize_policy(root)


def snapshot(*, legacy: bool = False, image: bool = False) -> GatewaySnapshot:
    operations = {
        "chat-completions": {
            "displayName": "Customer text",
            "method": "POST",
            "urlTemplate": "/chat/completions",
            "templateParameters": [],
            "request": {},
            "responses": [],
        }
    }
    policies: dict[str, str | None] = {
        "chat-completions": "<policies><inbound><base /></inbound></policies>",
    }
    if image:
        operations["images-generations"] = deepcopy(IMAGE_OPERATION_PROPERTIES)
        policies["images-generations"] = IMAGE_DENIAL
    return GatewaySnapshot(
        revision="turnstile-customer-current",
        api_properties={"path": "customer/gateway", "subscriptionRequired": False},
        parent_policy=legacy_parent() if legacy else CANONICAL_PARENT,
        operations=operations,
        operation_policies=policies,
    )


def test_legacy_upgrade_preserves_text_and_has_deterministic_identity() -> None:
    source = snapshot(legacy=True)
    plan = plan_image_upgrade(API_ID, source, CANONICAL_PARENT)
    assert plan.required and plan.create_operation and plan.initialize_image_policy
    assert plan.source is source
    assert policy_digest(plan.parent_policy) == policy_digest(CANONICAL_PARENT)
    assert plan.revision == plan_image_upgrade(API_ID, source, CANONICAL_PARENT).revision
    assert plan.revision != plan_image_upgrade(API_ID + "-other", source, CANONICAL_PARENT).revision


@pytest.mark.parametrize("prefix", ["", "\ufeff"])
def test_arm_rawxml_policy_response_is_parsed_without_json_assumption(prefix: str) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, text=prefix + CANONICAL_PARENT, headers={"Content-Type": "application/xml"}
            ),
        )
    ) as client:
        backend = AzureUpgradeBackend(
            client,
            API_ID,
            (
                "/subscriptions/unit/resourceGroups/customer/providers/Microsoft.Web/sites/api",
                "/subscriptions/unit/resourceGroups/customer/providers/Microsoft.Web/sites/control",
            ),
            lambda *_: None,
        )
        assert backend._policy(API_ID) == CANONICAL_PARENT


def test_upgraded_api_is_a_noop_and_existing_image_policy_is_not_overwritten() -> None:
    source = snapshot(image=True)
    source.operation_policies["images-generations"] = (
        "<policies><inbound><set-variable name='customer' value='preserved' /></inbound></policies>"
    )
    plan = plan_image_upgrade(API_ID, source, CANONICAL_PARENT)
    assert not plan.required
    assert not plan.create_operation and not plan.initialize_image_policy


def test_image_ready_parent_without_operation_still_requires_upgrade() -> None:
    plan = plan_image_upgrade(API_ID, snapshot(), CANONICAL_PARENT)
    assert plan.required and plan.create_operation
    assert plan.parent_policy == CANONICAL_PARENT


def test_operation_without_policy_is_initialized_fail_closed() -> None:
    source = snapshot(image=True)
    source.operation_policies["images-generations"] = None
    plan = plan_image_upgrade(API_ID, source, CANONICAL_PARENT)
    assert plan.required and not plan.create_operation and plan.initialize_image_policy


@pytest.mark.parametrize("legacy", [False, True])
def test_custom_parent_is_rejected_without_replacing_it(legacy: bool) -> None:
    source = snapshot(legacy=legacy)
    changed = replace(
        source,
        parent_policy=source.parent_policy.replace(
            'name="ledgerState"', 'name="customerLedgerState"', 1
        ),
    )
    with pytest.raises(ApimUpgradeError, match="supported public version"):
        plan_image_upgrade(API_ID, changed, CANONICAL_PARENT)


def test_conflicting_image_route_is_rejected() -> None:
    source = snapshot(image=True)
    source.operations["images-generations"]["method"] = "GET"
    with pytest.raises(ApimUpgradeError, match="conflicts"):
        plan_image_upgrade(API_ID, source, CANONICAL_PARENT)


def test_readback_checks_all_existing_operations_and_policies() -> None:
    plan = plan_image_upgrade(API_ID, snapshot(legacy=True), CANONICAL_PARENT)
    upgraded = replace(snapshot(image=True), revision=plan.revision)
    verify_upgrade_snapshot(plan, upgraded, IMAGE_DENIAL)
    upgraded.operation_policies["chat-completions"] = IMAGE_DENIAL
    with pytest.raises(ApimUpgradeError, match="existing operation policy"):
        verify_upgrade_snapshot(plan, upgraded, IMAGE_DENIAL)


def test_azure_null_effective_path_does_not_change_the_operation_contract() -> None:
    plan = plan_image_upgrade(API_ID, snapshot(), CANONICAL_PARENT)
    upgraded = replace(snapshot(image=True), revision=plan.revision)
    upgraded.operations["images-generations"]["effectivePath"] = None
    verify_upgrade_snapshot(plan, upgraded, IMAGE_DENIAL)
    upgraded.operations["images-generations"]["effectivePath"] = "/unexpected"
    with pytest.raises(ApimUpgradeError, match="operation definitions"):
        verify_upgrade_snapshot(plan, upgraded, IMAGE_DENIAL)


class UpgradeFake:
    def __init__(self, plan: ImageUpgradePlan) -> None:
        self.plan = plan
        self.current = plan.source.revision
        self.snapshots = {plan.source.revision: plan.source}
        self.calls: list[str] = []
        self.maintenance = True
        self.fail_after_promotion = False

    def require_maintenance(self) -> None:
        if not self.maintenance:
            raise ApimUpgradeError("Maintenance is required")

    def read(self, revision: str | None = None) -> GatewaySnapshot | None:
        return self.snapshots.get(revision or self.current)

    def prepare(self, plan: ImageUpgradePlan, *, create_revision: bool) -> None:
        self.calls.append(f"prepare:{create_revision}")
        self.snapshots[plan.revision] = replace(snapshot(image=True), revision=plan.revision)

    def promote(self, plan: ImageUpgradePlan, revision: str) -> None:
        self.calls.append(f"promote:{revision}")
        self.current = revision
        if self.fail_after_promotion:
            raise ApimUpgradeError("Client lost the promotion response")


def test_upgrade_verifies_candidate_before_promotion_and_is_reentrant() -> None:
    plan = plan_image_upgrade(API_ID, snapshot(legacy=True), CANONICAL_PARENT)
    backend = UpgradeFake(plan)
    records: list[dict[str, Any]] = []
    result = execute_image_upgrade(plan, backend, IMAGE_DENIAL, None, records.append)
    assert result["status"] == "passed"
    assert [record["status"] for record in records] == ["preparing", "promoting", "passed"]
    assert backend.calls == ["prepare:True", f"promote:{plan.revision}"]
    execute_image_upgrade(plan, backend, IMAGE_DENIAL, result, records.append)
    assert len(backend.calls) == 2
    assert backend.snapshots[plan.source.revision] is plan.source


def test_lost_promotion_response_is_read_back_without_repromotion() -> None:
    plan = plan_image_upgrade(API_ID, snapshot(legacy=True), CANONICAL_PARENT)
    backend = UpgradeFake(plan)
    backend.fail_after_promotion = True
    records: list[dict[str, Any]] = []
    with pytest.raises(ApimUpgradeError, match="lost"):
        execute_image_upgrade(plan, backend, IMAGE_DENIAL, None, records.append)
    assert records[-1]["status"] == "promoting"
    result = execute_image_upgrade(plan, backend, IMAGE_DENIAL, records[-1], records.append)
    assert result["status"] == "passed"
    assert len(backend.calls) == 2


def test_resume_uses_owned_partial_revision_without_recloning() -> None:
    plan = plan_image_upgrade(API_ID, snapshot(legacy=True), CANONICAL_PARENT)
    backend = UpgradeFake(plan)
    backend.snapshots[plan.revision] = replace(plan.source, revision=plan.revision)
    record = {"planSha256": document_digest(plan.document()), "status": "preparing"}
    execute_image_upgrade(plan, backend, IMAGE_DENIAL, record, lambda _: None)
    assert backend.calls == ["prepare:False", f"promote:{plan.revision}"]


def test_resume_accepts_a_verified_azure_candidate_without_recreating_it() -> None:
    plan = plan_image_upgrade(API_ID, snapshot(), CANONICAL_PARENT)
    backend = UpgradeFake(plan)
    candidate = replace(snapshot(image=True), revision=plan.revision)
    candidate.operations["images-generations"]["effectivePath"] = None
    backend.snapshots[plan.revision] = candidate
    record = {"planSha256": document_digest(plan.document()), "status": "preparing"}
    result = execute_image_upgrade(plan, backend, IMAGE_DENIAL, record, lambda _: None)
    assert result["status"] == "passed"
    assert backend.calls == [f"promote:{plan.revision}"]


def test_resume_refuses_to_overwrite_a_policy_added_to_the_interrupted_candidate() -> None:
    plan = plan_image_upgrade(API_ID, snapshot(legacy=True), CANONICAL_PARENT)
    backend = UpgradeFake(plan)
    candidate = replace(snapshot(image=True), revision=plan.revision)
    candidate.operation_policies["images-generations"] = (
        "<policies><inbound><set-variable name='customer' value='keep' /></inbound></policies>"
    )
    backend.snapshots[plan.revision] = candidate
    record = {"planSha256": document_digest(plan.document()), "status": "preparing"}
    with pytest.raises(ApimUpgradeError, match="unrecognized image policy"):
        execute_image_upgrade(plan, backend, IMAGE_DENIAL, record, lambda _: None)
    assert backend.calls == []


def test_current_candidate_requires_a_persisted_promotion_checkpoint() -> None:
    plan = plan_image_upgrade(API_ID, snapshot(legacy=True), CANONICAL_PARENT)
    backend = UpgradeFake(plan)
    backend.current = plan.revision
    backend.snapshots[plan.revision] = replace(snapshot(image=True), revision=plan.revision)
    record = {"planSha256": document_digest(plan.document()), "status": "preparing"}
    with pytest.raises(ApimUpgradeError, match="promotion checkpoint"):
        execute_image_upgrade(plan, backend, IMAGE_DENIAL, record, lambda _: None)
    assert backend.calls == []


@pytest.mark.parametrize("failure", ["running", "changed", "unowned", "journal"])
def test_upgrade_rejects_uncoordinated_or_changed_state_before_writing(failure: str) -> None:
    plan = plan_image_upgrade(API_ID, snapshot(), CANONICAL_PARENT)
    backend = UpgradeFake(plan)
    journal = None
    if failure == "running":
        backend.maintenance = False
    elif failure == "changed":
        backend.snapshots[backend.current] = replace(
            plan.source, api_properties={"path": "changed"}
        )
    elif failure == "unowned":
        backend.snapshots[plan.revision] = replace(plan.source, revision=plan.revision)
    else:
        journal = {"planSha256": "another-plan"}
    with pytest.raises(ApimUpgradeError):
        execute_image_upgrade(plan, backend, IMAGE_DENIAL, journal, lambda _: None)
    assert backend.calls == []


def test_explicit_rollback_restores_only_unchanged_original_revision() -> None:
    plan = plan_image_upgrade(API_ID, snapshot(legacy=True), CANONICAL_PARENT)
    backend = UpgradeFake(plan)
    result = execute_image_upgrade(plan, backend, IMAGE_DENIAL, None, lambda _: None)
    restored = execute_image_upgrade(
        plan, backend, IMAGE_DENIAL, result, lambda _: None, rollback=True
    )
    assert restored["status"] == "rolled_back"
    assert backend.current == plan.source.revision
    assert plan.revision in backend.snapshots
    calls = len(backend.calls)
    execute_image_upgrade(plan, backend, IMAGE_DENIAL, restored, lambda _: None, rollback=True)
    assert len(backend.calls) == calls


def test_upgrade_parameters_do_not_bootstrap_or_change_roles() -> None:
    plan = plan_image_upgrade(API_ID, snapshot(), CANONICAL_PARENT)
    parameters = upgrade_parameters(plan, "prepare", plan.revision, create_revision=True)
    assert parameters["apimName"] == "unit"
    assert parameters["apiId"] == "customer-llm"
    assert parameters["apimResourceGroupName"] == "customer"
    assert parameters["apiProperties"] == plan.source.api_properties
    assert not any("secret" in key.lower() or "role" in key.lower() for key in parameters)
    with pytest.raises(ApimUpgradeError):
        upgrade_parameters(plan, "promote", "unplanned", create_revision=False)


@pytest.mark.parametrize("kind", ["Delete", "Unsupported", "Create", "Modify"])
def test_what_if_rejects_other_resources_and_unresolved_writes(kind: str) -> None:
    plan = plan_image_upgrade(API_ID, snapshot(), CANONICAL_PARENT)
    with pytest.raises(ApimUpgradeError, match="unapproved"):
        validate_upgrade_what_if(
            {
                "changes": [
                    {
                        "changeType": kind,
                        "resourceId": API_ID + "-another",
                    }
                ]
            },
            plan,
            "prepare",
            plan.revision,
        )
    validate_upgrade_what_if(
        {
            "changes": [
                {
                    "changeType": "Create",
                    "resourceId": API_ID + ";rev=" + plan.revision,
                }
            ]
        },
        plan,
        "prepare",
        plan.revision,
    )


def test_arm_reader_pins_revision_checks_maintenance_and_refuses_external_pagination() -> None:
    source = snapshot()
    application_ids = (
        "/subscriptions/unit/resourceGroups/customer/providers/Microsoft.Web/sites/api",
        "/subscriptions/unit/resourceGroups/customer/providers/Microsoft.Web/sites/control",
    )
    current_path = API_ID + ";rev=" + source.revision
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path in application_ids:
            return httpx.Response(200, json={"properties": {"state": "Stopped"}})
        if path == API_ID:
            return httpx.Response(
                200,
                json={
                    "properties": {
                        **source.api_properties,
                        "apiRevision": source.revision,
                        "isCurrent": True,
                        "isOnline": True,
                        "sourceApiId": API_ID + ";rev=old",
                    }
                },
            )
        if path == current_path + "/policies/policy":
            assert request.url.params["format"] == "rawxml"
            return httpx.Response(200, json={"properties": {"value": source.parent_policy}})
        if path == current_path + "/operations":
            return httpx.Response(200, json={"value": [{"name": "chat-completions"}]})
        if path == current_path + "/operations/chat-completions":
            return httpx.Response(
                200,
                json={
                    "properties": {
                        **source.operations["chat-completions"],
                        "description": "",
                        "policies": source.operation_policies["chat-completions"],
                    }
                },
            )
        if path == current_path + "/operations/chat-completions/policies/policy":
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "value": source.operation_policies["chat-completions"],
                    }
                },
            )
        raise AssertionError(path)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        backend = AzureUpgradeBackend(client, API_ID, application_ids, lambda *_: None)
        assert backend.read() == source
        backend.require_maintenance()
        with pytest.raises(ApimUpgradeError, match="authorized management endpoint"):
            backend._get("https://other.example/operations")
        with pytest.raises(ApimUpgradeError, match="exact upgrade target"):
            backend._get(API_ID + "-another")
        for suffix in ("/../another", "/%2e%2e/another"):
            with pytest.raises(ApimUpgradeError, match="exact upgrade target"):
                backend._get(API_ID + suffix)
    assert all(request.method == "GET" for request in requests)


def test_full_upgrade_commands_plan_apply_repeat_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import deploy
    from tests.platform.deployment.test_deploy_script import _parameters

    inputs = deploy.DeploymentInputs.load(
        "unit", _parameters(tmp_path / "parameters.json"), tmp_path / "state.json"
    )
    outputs = {
        "resourceGroupName": inputs.resource_group_name,
        "apimResourceGroupName": "customer",
        "apimName": "unit",
        "apimApiId": "customer-llm",
        "apimPrincipalId": "unit-principal",
        "apimGatewayUrl": "https://unit.azure-api.net",
        "apiName": "api-unit",
        "controlPlaneFunctionName": "control-unit",
    }
    original = snapshot(legacy=True)
    snapshots = {original.revision: original}
    current = original.revision
    commands: list[list[str]] = []

    def arm(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        path = request.url.path
        if "/providers/Microsoft.Web/sites/" in path:
            return httpx.Response(200, json={"properties": {"state": "Stopped"}})
        if "/providers/Microsoft.Resources/deployments/" in path:
            return httpx.Response(404)
        if path == API_ID:
            selected = snapshots[current]
            return httpx.Response(
                200,
                json={
                    "properties": {
                        **selected.api_properties,
                        "apiRevision": selected.revision,
                        "isCurrent": True,
                    }
                },
            )
        assert path.startswith(API_ID + ";rev=")
        revision, _, suffix = path[len(API_ID + ";rev=") :].partition("/")
        if revision not in snapshots:
            return httpx.Response(404)
        selected = snapshots[revision]
        if not suffix:
            value: dict[str, Any] = {
                **selected.api_properties,
                "apiRevision": revision,
                "isCurrent": revision == current,
            }
        elif suffix == "policies/policy":
            value = {"value": selected.parent_policy}
        elif suffix == "operations":
            return httpx.Response(
                200,
                json={
                    "value": [{"name": name} for name in selected.operations],
                },
            )
        else:
            parts = suffix.split("/")
            assert parts[0] == "operations"
            if len(parts) == 2:
                value = selected.operations[parts[1]]
            else:
                value = {"value": selected.operation_policies[parts[1]]}
        return httpx.Response(200, json={"properties": value})

    class Runner(deploy.CommandRunner):
        def run_json(
            self,
            command: Sequence[str],
            *,
            cwd: Path | None = None,
            env: Mapping[str, str] | None = None,
        ) -> dict[str, Any]:
            nonlocal current
            commands.append(list(command))
            assert command[command.index("--subscription") + 1] == "unit"
            if command[:3] == ["az", "account", "get-access-token"]:
                return {"accessToken": "unit-token-not-persisted"}
            assert command[:3] == ["az", "deployment", "sub"]
            parameter_file = Path(command[command.index("--parameters") + 1][1:])
            values = {
                name: value["value"]
                for name, value in json.loads(parameter_file.read_text())["parameters"].items()
            }
            revision = values["revision"]
            if command[3] == "what-if":
                suffix = (
                    ";rev=" + revision
                    if values["stage"] == "prepare"
                    else "/releases/infrastructure-" + revision
                )
                return {"changes": [{"changeType": "Create", "resourceId": API_ID + suffix}]}
            assert command[3] == "create"
            if values["stage"] == "prepare":
                assert values["createRevision"] is True
                upgraded = deepcopy(original)
                upgraded.operations["images-generations"] = values["imageOperationProperties"]
                upgraded.operation_policies["images-generations"] = IMAGE_DENIAL
                snapshots[revision] = replace(
                    upgraded, revision=revision, parent_policy=values["parentPolicy"]
                )
            else:
                assert revision in snapshots
                current = revision
            return {"properties": {"provisioningState": "Succeeded"}}

    client_type = httpx.Client

    def client(**options: Any) -> httpx.Client:
        return client_type(transport=httpx.MockTransport(arm), **options)

    monkeypatch.setattr(httpx, "Client", client)
    runner = Runner()
    deploy.gateway_upgrade(runner, inputs, outputs, "plan-upgrade")
    assert not any(command[:4] == ["az", "deployment", "sub", "create"] for command in commands)
    deploy.gateway_upgrade(runner, inputs, outputs, "upgrade", assume_yes=True)
    assert current != original.revision
    written = sum(command[:4] == ["az", "deployment", "sub", "create"] for command in commands)
    assert written == 2
    deploy.gateway_upgrade(runner, inputs, outputs, "upgrade", assume_yes=True)
    deploy.gateway_upgrade(runner, inputs, outputs, "check")
    assert written == sum(
        command[:4] == ["az", "deployment", "sub", "create"] for command in commands
    )
    upgrade_revision = current
    current = "later-model-publication"
    snapshots[current] = replace(snapshots[upgrade_revision], revision=current)
    deploy.gateway_upgrade(runner, inputs, outputs, "plan-upgrade")
    deploy.gateway_upgrade(runner, inputs, outputs, "upgrade", assume_yes=True)
    assert written == sum(
        command[:4] == ["az", "deployment", "sub", "create"] for command in commands
    )
    with pytest.raises(ApimUpgradeError, match="changed API identity"):
        deploy.gateway_upgrade(runner, inputs, outputs, "rollback-upgrade", assume_yes=True)
    current = upgrade_revision
    deploy.gateway_upgrade(runner, inputs, outputs, "rollback-upgrade", assume_yes=True)
    assert current == original.revision and len(snapshots) == 3
    assert snapshots[original.revision] is original
    with pytest.raises(deploy.DeploymentError, match="upgrade required"):
        deploy.gateway_upgrade(runner, inputs, outputs, "check")
    assert not inputs.state_path.exists()
    for document in inputs.state_path.with_suffix(".upgrades").rglob("*.json"):
        assert "unit-token-not-persisted" not in document.read_text()
