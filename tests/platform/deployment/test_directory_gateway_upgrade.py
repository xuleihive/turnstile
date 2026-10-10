from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.apim_upgrade import (
    ApimUpgradeError,
    ImageUpgradePlan,
    execute_image_upgrade,
    verify_upgrade_snapshot,
)
from scripts.deploy import DeploymentError
from scripts.directory_gateway_upgrade import (
    VERSION,
    directory_plan_directory,
    directory_upgrade_parameters,
    plan_directory_upgrade,
    validate_candidate_evidence,
    validate_directory_what_if,
)
from tests.platform.deployment.test_apim_upgrade import (
    API_ID,
    CANONICAL_PARENT,
    IMAGE_DENIAL,
    UpgradeFake,
    snapshot,
)


def plan() -> ImageUpgradePlan:
    return plan_directory_upgrade(
        API_ID,
        snapshot(image=True),
        CANONICAL_PARENT,
        cloud="public",
        endpoint="__LEDGER_TABLE_ENDPOINT__",
        table="__LEDGER_TABLE_NAME__",
    )


class DirectoryBackend(UpgradeFake):
    def prepare(self, value: ImageUpgradePlan, *, create_revision: bool) -> None:
        self.calls.append(f"prepare:{create_revision}")
        self.snapshots[value.revision] = replace(
            value.source,
            revision=value.revision,
            parent_policy=value.parent_policy,
        )


def evidence(backend: DirectoryBackend, value: ImageUpgradePlan) -> dict[str, Any]:
    return {
        "version": VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "candidate_fingerprint": backend.snapshots[value.revision].fingerprint(),
        "real_signed_employee_token": True,
        "identity_projection_readback_verified": True,
        "cases": {
            "unmapped_identity": 403,
            "disabled_identity": 403,
            "expired_mapping": 503,
            "header_spoofing_rejected": True,
            "stable_governance_id": True,
            "budget_ledger_unchanged": True,
        },
    }


def test_identity_revision_is_independent_and_retains_every_existing_operation() -> None:
    value = plan()
    assert value.required and value.version == VERSION
    assert not value.create_operation and not value.initialize_image_policy
    assert "images-v2" not in value.revision
    candidate = replace(value.source, revision=value.revision, parent_policy=value.parent_policy)
    verify_upgrade_snapshot(value, candidate, IMAGE_DENIAL)
    again = plan_directory_upgrade(
        API_ID,
        candidate,
        CANONICAL_PARENT,
        cloud="public",
        endpoint="__LEDGER_TABLE_ENDPOINT__",
        table="__LEDGER_TABLE_NAME__",
    )
    assert not again.required
    parameters = directory_upgrade_parameters(
        value, "prepare", value.revision, create_revision=True
    )
    assert (
        "initializeImagePolicy" not in parameters and "imageOperationProperties" not in parameters
    )


def test_prepare_does_not_promote_and_promotion_requires_identity_probes() -> None:
    value = plan()
    backend = DirectoryBackend(value)
    records: list[dict[str, Any]] = []
    result = execute_image_upgrade(
        value,
        backend,
        IMAGE_DENIAL,
        None,
        records.append,
        prepare_only=True,
    )
    assert result["status"] == "prepared" and backend.current == value.source.revision
    with pytest.raises(ApimUpgradeError, match="probe evidence"):
        execute_image_upgrade(
            value,
            backend,
            IMAGE_DENIAL,
            result,
            records.append,
            candidate_validator=lambda candidate: validate_candidate_evidence(candidate, None),
        )
    assert backend.current == value.source.revision
    passed = execute_image_upgrade(
        value,
        backend,
        IMAGE_DENIAL,
        records[-1],
        records.append,
        candidate_validator=lambda candidate: validate_candidate_evidence(
            candidate, evidence(backend, value)
        ),
    )
    assert passed["status"] == "passed" and backend.current == value.revision
    restored = execute_image_upgrade(
        value, backend, IMAGE_DENIAL, passed, records.append, rollback=True
    )
    assert restored["status"] == "rolled_back" and backend.current == value.source.revision


def test_identity_upgrade_does_not_accept_image_resource_changes_or_delete() -> None:
    value = plan()
    candidate = value.api_resource_id + ";rev=" + value.revision
    validate_directory_what_if(
        {"changes": [{"resourceId": candidate + "/policies/policy", "changeType": "Modify"}]},
        value,
        "prepare",
        value.revision,
    )
    for resource, change in (
        (candidate + "/operations/images-generations", "Modify"),
        (candidate + "/policies/policy", "Delete"),
        (value.api_resource_id + "/policies/policy", "Modify"),
    ):
        with pytest.raises(ApimUpgradeError, match="unapproved"):
            validate_directory_what_if(
                {"changes": [{"resourceId": resource, "changeType": change}]},
                value,
                "prepare",
                value.revision,
            )


def test_candidate_probe_drift_cannot_be_declared_success() -> None:
    value = plan()
    backend = DirectoryBackend(value)
    backend.prepare(value, create_revision=True)
    proof = evidence(backend, value)
    proof["candidate_fingerprint"] = "not-the-reviewed-candidate"
    with pytest.raises(ApimUpgradeError, match="incomplete"):
        validate_candidate_evidence(backend.snapshots[value.revision], proof)


def test_independent_plan_run_keeps_default_evidence_directory() -> None:
    state = Path("/private/state/deployment.json")
    base = directory_plan_directory(state, None)
    assert base == state.with_suffix(".upgrades") / VERSION
    assert directory_plan_directory(state, "policy-compile-2") == base / "runs/policy-compile-2"
    first = plan_directory_upgrade(
        API_ID,
        snapshot(image=True),
        CANONICAL_PARENT,
        cloud="public",
        endpoint="__LEDGER_TABLE_ENDPOINT__",
        table="__LEDGER_TABLE_NAME__",
        plan_id="first-run",
    )
    second = plan_directory_upgrade(
        API_ID,
        snapshot(image=True),
        CANONICAL_PARENT,
        cloud="public",
        endpoint="__LEDGER_TABLE_ENDPOINT__",
        table="__LEDGER_TABLE_NAME__",
        plan_id="second-run",
    )
    assert first.parent_policy == second.parent_policy
    assert first.revision != second.revision


@pytest.mark.parametrize("plan_id", ["", "../old", "/tmp", "UPPER", "a" * 65, "a/b"])
def test_plan_run_cannot_escape_evidence_directory(plan_id: str) -> None:
    with pytest.raises(DeploymentError, match="plan ID"):
        directory_plan_directory(Path("/private/state/deployment.json"), plan_id)
