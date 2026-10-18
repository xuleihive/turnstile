"""Independent directory identity APIM plan, candidate preparation and reviewed promotion."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from turnstile_core.integrations.apim_directory_policy import (
    compose_directory_identity_policy,
    strip_validated_directory_identity_policy,
)
from turnstile_core.integrations.apim_policy_components import validate_parent_policy

from .apim_upgrade import (
    ApimUpgradeError,
    AzureUpgradeBackend,
    GatewaySnapshot,
    ImageUpgradePlan,
    document_digest,
    execute_image_upgrade,
    policy_digest,
)
from .deploy import (
    CommandRunner,
    DeploymentError,
    DeploymentInputs,
    ExistingCore,
    _arm_parameter_document,
    _confirm_deployment,
    _deployment_command,
    _output_string,
    _upgrade_lock,
    _what_if_counts,
    _write_private_json,
    current_app_settings,
    load_saved_outputs,
    require_prerequisites,
    temporary_parameter_file,
)
from .stage_deployment import REPOSITORY_ROOT

VERSION = "directory-identity-v1"


def directory_plan_directory(state_path: Path, plan_id: str | None) -> Path:
    directory = state_path.with_suffix(".upgrades") / VERSION
    if plan_id is None:
        return directory
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", plan_id) is None:
        raise DeploymentError("Directory plan ID must be 1-64 lowercase letters, digits or hyphens")
    return directory / "runs" / plan_id


def plan_directory_upgrade(
    api_resource_id: str,
    snapshot: GatewaySnapshot,
    canonical: str,
    *,
    cloud: str,
    endpoint: str,
    table: str,
    plan_id: str | None = None,
) -> ImageUpgradePlan:
    validate_parent_policy(snapshot.parent_policy, canonical)
    if "directoryIdentityPolicyVersion" in snapshot.parent_policy:
        base = strip_validated_directory_identity_policy(snapshot.parent_policy)
        expected = compose_directory_identity_policy(
            base, cloud=cloud, endpoint=endpoint, table=table
        )
        if policy_digest(expected) != policy_digest(snapshot.parent_policy):
            raise ApimUpgradeError("The current directory policy has different identity targets")
        parent = snapshot.parent_policy
    else:
        parent = compose_directory_identity_policy(
            snapshot.parent_policy,
            cloud=cloud,
            endpoint=endpoint,
            table=table,
        )
    identity = document_digest(
        {
            "target": api_resource_id.casefold(),
            "source": snapshot.fingerprint(),
            "parent": policy_digest(parent),
            "version": VERSION,
            **({"run": plan_id} if plan_id is not None else {}),
        }
    )
    # The existing revision engine is shared; directory plans never initialize image operations.
    return ImageUpgradePlan(
        api_resource_id=api_resource_id,
        source=snapshot,
        parent_policy=parent,
        create_operation=False,
        initialize_image_policy=False,
        revision=f"turnstile-{VERSION}-{identity[:16]}",
        version=VERSION,
    )


def directory_upgrade_parameters(
    plan: ImageUpgradePlan,
    stage: str,
    revision: str,
    *,
    create_revision: bool,
) -> dict[str, Any]:
    if (
        plan.version != VERSION
        or plan.create_operation
        or plan.initialize_image_policy
        or stage not in {"prepare", "promote"}
        or revision not in {plan.revision, plan.source.revision}
    ):
        raise ApimUpgradeError("Directory upgrade stage is outside the reviewed plan")
    parts = plan.api_resource_id.split("/")
    return {
        "apimResourceGroupName": parts[4],
        "apimName": parts[8],
        "apiId": parts[10],
        "sourceRevision": plan.source.revision,
        "revision": revision,
        "stage": stage,
        "createRevision": create_revision,
        "apiProperties": plan.source.api_properties if stage == "prepare" else {},
        "parentPolicy": plan.parent_policy if stage == "prepare" else "",
    }


def validate_directory_what_if(
    result: Mapping[str, Any],
    plan: ImageUpgradePlan,
    stage: str,
    revision: str,
) -> None:
    directory_upgrade_parameters(plan, stage, revision, create_revision=False)
    api = plan.api_resource_id.casefold()
    candidate = api + ";rev=" + revision.casefold()
    allowed = (
        {
            candidate,
            candidate + "/policies/policy",
        }
        if stage == "prepare"
        else {api + "/releases/infrastructure-" + revision.casefold()}
    )
    changes = result.get("changes", result.get("properties", {}).get("changes"))
    if not isinstance(changes, list):
        raise ApimUpgradeError("Directory what-if did not enumerate resource changes")
    for change in changes:
        if change.get("changeType") in {"NoChange", "Ignore"}:
            continue
        if (
            change.get("changeType") not in {"Create", "Modify"}
            or str(change.get("resourceId", "")).casefold() not in allowed
        ):
            raise ApimUpgradeError("Directory what-if contains an unapproved resource change")


def validate_candidate_evidence(
    observed: GatewaySnapshot,
    evidence: dict[str, Any] | None,
) -> None:
    if evidence is None:
        raise ApimUpgradeError(
            "Real identity candidate probe evidence is required before promotion"
        )
    try:
        age = datetime.now(UTC) - datetime.fromisoformat(evidence["captured_at"])
        cases = evidence["cases"]
        if (
            not timedelta(0) <= age <= timedelta(minutes=15)
            or evidence["candidate_fingerprint"] != observed.fingerprint()
            or evidence["version"] != VERSION
            or evidence["real_signed_employee_token"] is not True
            or evidence["identity_projection_readback_verified"] is not True
            or cases
            != {
                "unmapped_identity": 403,
                "disabled_identity": 403,
                "expired_mapping": 503,
                "header_spoofing_rejected": True,
                "stable_governance_id": True,
                "budget_ledger_unchanged": True,
            }
        ):
            raise ValueError("Incomplete identity probe evidence")
    except (KeyError, TypeError, ValueError) as error:
        raise ApimUpgradeError(
            "Candidate identity probes, ledger integrity and signed-token evidence are incomplete",
        ) from error


def execute(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    outputs: Mapping[str, Any],
    *,
    action: str,
    cloud: str,
    assume_yes: bool = False,
    candidate_evidence: dict[str, Any] | None = None,
    plan_id: str | None = None,
) -> None:
    if action not in {"plan", "prepare", "promote", "rollback"}:
        raise DeploymentError("Unknown directory identity upgrade action")
    if (
        _output_string(outputs, "resourceGroupName").casefold()
        != inputs.resource_group_name.casefold()
    ):
        raise DeploymentError("Directory upgrade outputs belong to another resource group")
    core = ExistingCore.from_outputs(outputs)
    api_id = _output_string(outputs, "apimApiId")
    resource_id = (
        f"/subscriptions/{inputs.subscription}/resourceGroups/{core.apim_resource_group_name}"
        f"/providers/Microsoft.ApiManagement/service/{core.apim_name}/apis/{api_id}"
    )
    application_ids = tuple(
        f"/subscriptions/{inputs.subscription}/resourceGroups/{inputs.resource_group_name}"
        f"/providers/Microsoft.Web/sites/{_output_string(outputs, name)}"
        for name in ("apiName", "controlPlaneFunctionName")
    )
    settings = current_app_settings(
        runner,
        inputs,
        _output_string(outputs, "apiName"),
    )
    endpoint = str(settings.get("LEDGER_TABLE_ENDPOINT", "")).rstrip("/")
    table = str(settings.get("LEDGER_TABLE_NAME", ""))
    if not endpoint or not table:
        raise DeploymentError(
            "Read the existing configured ledger before planning identity upgrade"
        )
    template = REPOSITORY_ROOT / "infra/directory-identity-upgrade.bicep"
    baseline = REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml"
    canonical = baseline.read_text(encoding="utf-8")
    denial = (REPOSITORY_ROOT / "infra/policies/provider-neutral-images-policy.xml").read_text()
    bindings = {
        "target": resource_id,
        "applications": list(application_ids),
        "cloud": cloud,
        "endpoint": endpoint,
        "table": table,
        "templates": {
            str(path.relative_to(REPOSITORY_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                template,
                REPOSITORY_ROOT / "infra/modules/apim-upgrade.bicep",
                baseline,
                REPOSITORY_ROOT / "scripts/apim_upgrade.py",
                Path(__file__).resolve(),
                REPOSITORY_ROOT / "turnstile_core/integrations/apim_directory_policy.py",
            )
        },
    }
    directory = directory_plan_directory(inputs.state_path, plan_id)
    credentials = runner.run_json(
        [
            "az",
            "account",
            "get-access-token",
            "--subscription",
            inputs.subscription,
            "--resource",
            "https://management.azure.com/",
            "--output",
            "json",
        ]
    )
    token = credentials.pop("accessToken")
    with (
        _upgrade_lock(directory_plan_directory(inputs.state_path, None)),
        httpx.Client(
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=60,
            trust_env=False,
        ) as client,
    ):

        def preview(
            plan: ImageUpgradePlan,
            stage: str,
            revision: str,
            create: bool,
        ) -> tuple[dict[str, Any], str]:
            parameters = _arm_parameter_document(
                directory_upgrade_parameters(
                    plan,
                    stage,
                    revision,
                    create_revision=create,
                )
            )
            name = f"directory-{document_digest(plan.document())[:24]}-{stage}"
            with temporary_parameter_file(parameters, directory) as file:
                result = runner.run_json(
                    _deployment_command("what-if", inputs, template, file, name)
                )
            validate_directory_what_if(result, plan, stage, revision)
            _what_if_counts(result)
            _write_private_json(directory / f"what-if-{stage}.json", result)
            return parameters, name

        def deploy(
            plan: ImageUpgradePlan,
            stage: str,
            revision: str,
            create: bool,
        ) -> None:
            name = f"directory-{document_digest(plan.document())[:24]}-{stage}"
            prior = client.get(
                f"https://management.azure.com/subscriptions/{inputs.subscription}"
                f"/providers/Microsoft.Resources/deployments/{name}",
                params={"api-version": "2025-04-01"},
            )
            if prior.status_code == 200:
                if prior.json().get("properties", {}).get("provisioningState") not in {
                    "Succeeded",
                    "Failed",
                    "Canceled",
                }:
                    raise ApimUpgradeError("A previous directory ARM operation is still running")
            elif prior.status_code != 404:
                raise ApimUpgradeError("Previous directory ARM operation state is unavailable")
            parameters, name = preview(plan, stage, revision, create)
            with temporary_parameter_file(parameters, directory) as file:
                result = runner.run_json(
                    _deployment_command("create", inputs, template, file, name)
                )
            if result.get("properties", {}).get("provisioningState") != "Succeeded":
                raise ApimUpgradeError("Directory ARM operation is not confirmed successful")

        backend = AzureUpgradeBackend(
            client, resource_id, (application_ids[0], application_ids[1]), deploy
        )
        current = backend.read()
        if current is None:
            raise DeploymentError("An identity upgrade must not bootstrap an absent API")
        plan_path, journal_path = directory / "plan.json", directory / "journal.json"
        if plan_path.exists():
            recorded = json.loads(plan_path.read_text())
            if recorded["binding"] != bindings:
                raise DeploymentError("Directory upgrade binding or source code changed")
            raw = recorded["plan"]
            plan = ImageUpgradePlan(**{**raw, "source": GatewaySnapshot(**raw["source"])})
            if (
                plan.document()
                != plan_directory_upgrade(
                    resource_id,
                    plan.source,
                    canonical,
                    cloud=cloud,
                    endpoint=endpoint,
                    table=table,
                    plan_id=plan_id,
                ).document()
            ):
                raise DeploymentError("Directory plan does not match the reviewed transformation")
        else:
            if action != "plan":
                raise DeploymentError(
                    "Create the independent directory plan before resource writes"
                )
            plan = plan_directory_upgrade(
                resource_id,
                current,
                canonical,
                cloud=cloud,
                endpoint=endpoint,
                table=table,
                plan_id=plan_id,
            )
            _write_private_json(plan_path, {"binding": bindings, "plan": plan.document()})
        if not plan.required:
            if plan_directory_upgrade(
                resource_id,
                current,
                canonical,
                cloud=cloud,
                endpoint=endpoint,
                table=table,
                plan_id=plan_id,
            ).required:
                raise DeploymentError(
                    "The current API no longer satisfies the no-change directory plan"
                )
            print("Directory identity policy already matches; no resource writes")
            return
        if action == "plan":
            preview(plan, "prepare", plan.revision, backend.read(plan.revision) is None)
            print(f"Private directory identity plan saved: {plan_path}")
            return
        _confirm_deployment(assume_yes)
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
        if (
            action != "rollback"
            and journal is not None
            and journal.get("status") == "passed"
            and current.revision != plan.revision
            and not plan_directory_upgrade(
                resource_id,
                current,
                canonical,
                cloud=cloud,
                endpoint=endpoint,
                table=table,
                plan_id=plan_id,
            ).required
        ):
            print("The current revision retains the completed directory identity upgrade")
            return
        result = execute_image_upgrade(
            plan,
            backend,
            denial,
            journal,
            lambda value: _write_private_json(journal_path, value),
            rollback=action == "rollback",
            prepare_only=action == "prepare",
            candidate_validator=lambda candidate: validate_candidate_evidence(
                candidate, candidate_evidence
            ),
        )
        print(
            f"Directory identity revision: {result['status']}; application states are not changed"
        )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Reviewed, independent directory identity APIM upgrade"
    )
    parser.add_argument("action", choices=("plan", "prepare", "promote", "rollback"))
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--parameters", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--cloud", choices=("public", "usgov", "china"), default="public")
    parser.add_argument("--candidate-evidence", type=Path)
    parser.add_argument(
        "--plan-id",
        help="Independent reviewed plan run; retains previous plans and failed candidate evidence",
    )
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    evidence = None
    if args.candidate_evidence is not None:
        if args.candidate_evidence.stat().st_mode & 0o077:
            parser.error("Private candidate evidence must have mode 0600")
        evidence = json.loads(args.candidate_evidence.read_text())
    runner = CommandRunner()
    inputs = DeploymentInputs.load(args.subscription, args.parameters, args.state)
    require_prerequisites(runner, inputs.subscription)
    outputs = load_saved_outputs(inputs)
    if outputs is None:
        parser.error("Existing saved deployment outputs are required")
    execute(
        runner,
        inputs,
        outputs,
        action=args.action,
        cloud=args.cloud,
        assume_yes=args.yes,
        candidate_evidence=evidence,
        plan_id=args.plan_id,
    )


if __name__ == "__main__":
    main()
