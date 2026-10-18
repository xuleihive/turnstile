"""Offline schema-one adoption; never publishes policies or activates a budget fence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree as ET

from backend.services.client_user_policy import strip_verified_client_user_policy
from scripts.budget_policy_bindings import bind_policy_parameters
from turnstile_core.domain.budget_delegation import BudgetDelegationTrust
from turnstile_core.integrations.apim_budget_execution import compose_budget_execution_parent
from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
from turnstile_core.integrations.apim_directory_policy import (
    strip_validated_directory_identity_policy,
)
from turnstile_core.integrations.apim_policy_components import (
    _matches_policy_component,
    component_digest,
    parse_policy,
    serialize_policy,
    validate_parent_policy,
)

_SLOT = re.compile(r"__[A-Z][A-Z0-9_]*__")


@dataclass(frozen=True)
class BudgetParentUpgrade:
    source_sha256: str
    candidate_sha256: str
    candidate_parent: str = field(repr=False)
    adopted_bindings: dict[str, str] = field(repr=False)


def plan_budget_parent_upgrade(
    source: str,
    *,
    legacy_template: str,
    canonical_parent: str,
    delegation: BudgetDelegationTrust,
    gateway_profile_id: UUID,
) -> BudgetParentUpgrade:
    source_root = parse_policy(source)
    if source_root.find(".//set-variable[@name='budgetExecutionVersion']") is not None:
        raise PolicyCompilationError("Budget adoption cannot replace an existing execution epoch")
    if source_root.find(".//set-variable[@name='directoryIdentityPolicyVersion']") is not None:
        raise PolicyCompilationError(
            "Directory identity adoption needs a separately verified binding",
        )
    legacy = strip_verified_client_user_policy(source)
    legacy = strip_validated_directory_identity_policy(legacy)
    validate_parent_policy(legacy, legacy_template)
    actual, reference = parse_policy(legacy), parse_policy(legacy_template)
    bindings: dict[str, str] = {
        "__LEGACY_PROVIDER_ROUTING__": "", "__USAGE_OBSERVER_HEADERS__": "",
    }
    for node in reference.iter():
        if not _SLOT.search(ET.tostring(node, encoding="unicode")):
            continue
        if node.tag == "set-variable" and node.get("name"):
            candidates = actual.findall(f".//set-variable[@name='{node.get('name')}']")
        elif node.tag == "set-header" and node.get("name"):
            candidates = actual.findall(f".//set-header[@name='{node.get('name')}']")
        elif node.tag in {"validate-azure-ad-token", "log-to-eventhub", "llm-token-limit"}:
            candidates = list(actual.iter(node.tag))
        else:
            continue
        matches: list[dict[str, str]] = []
        for candidate in candidates:
            trial = dict(bindings)
            if _matches_policy_component(candidate, node, trial):
                matches.append(trial)
        if matches:
            if any(value != matches[0] for value in matches):
                raise PolicyCompilationError(
                    "Legacy adoption parameters are ambiguous at " + node.tag
                    + ":" + str(node.get("name", "")),
                )
            bindings.update(matches[0])
    if bindings.get("__GATEWAY_PROFILE_ID__") != str(gateway_profile_id):
        raise PolicyCompilationError("Legacy parent belongs to a different Gateway")
    def materialize(template: ET.Element) -> ET.Element:
        try:
            return bind_policy_parameters(template, bindings)
        except ValueError as error:
            raise PolicyCompilationError(str(error)) from error

    if component_digest(actual, normalize_text_defaults=False) != component_digest(
        materialize(reference), normalize_text_defaults=False,
    ):
        raise PolicyCompilationError("Legacy adoption cannot discard unrecognized policy content")
    root = materialize(parse_policy(canonical_parent))
    materialized = serialize_policy(root)
    candidate_parent = compose_budget_execution_parent(
        materialized, canonical_parent, enabled=True, delegation=delegation,
    )
    # The execution component adds its own slots after the legacy parent is bound.
    candidate_parent = serialize_policy(materialize(parse_policy(candidate_parent)))
    validate_parent_policy(candidate_parent, compose_budget_execution_parent(
        canonical_parent, canonical_parent, enabled=True, delegation=delegation,
    ))
    return BudgetParentUpgrade(
        source_sha256=component_digest(source_root, normalize_text_defaults=False),
        candidate_sha256=component_digest(
            parse_policy(candidate_parent), normalize_text_defaults=False,
        ),
        candidate_parent=candidate_parent,
        adopted_bindings=bindings,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan a verified schema-one budget adoption; no cloud writes or activation",
    )
    parser.add_argument("--current-parent", type=Path, required=True)
    parser.add_argument("--gateway-profile-id", type=UUID, required=True)
    parser.add_argument("--delegation", type=Path, required=True)
    parser.add_argument("--legacy-template", type=Path, default=Path(
        "infra/policies/foundry-finops-policy.schema1.xml",
    ))
    parser.add_argument("--canonical-parent", type=Path, default=Path(
        "infra/policies/foundry-finops-policy.xml",
    ))
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    original = arguments.current_parent.read_bytes()
    plan = plan_budget_parent_upgrade(
        original.decode("utf-8-sig"), legacy_template=arguments.legacy_template.read_text(),
        canonical_parent=arguments.canonical_parent.read_text(),
        delegation=BudgetDelegationTrust.model_validate_json(arguments.delegation.read_text()),
        gateway_profile_id=arguments.gateway_profile_id,
    )
    output: Path = arguments.output
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    candidate = output / "candidate-parent.xml"
    with candidate.open("x", encoding="utf-8") as stream:
        candidate.chmod(0o600)
        stream.write(plan.candidate_parent)
    proof = {
        "version": 1, "cloud_writes": 0, "activation": False,
        "gateway_profile_id": str(arguments.gateway_profile_id),
        "source_raw_sha256": hashlib.sha256(original).hexdigest(),
        "source_policy_digest": plan.source_sha256,
        "candidate_policy_digest": plan.candidate_sha256,
        "candidate_raw_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "parameter_names": sorted(plan.adopted_bindings),
    }
    target = output / "plan.json"
    with target.open("x", encoding="utf-8") as stream:
        target.chmod(0o600)
        json.dump(proof, stream, indent=2)
        stream.write("\n")
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
