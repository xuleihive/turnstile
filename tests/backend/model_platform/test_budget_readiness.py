from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from backend.services.budget_readiness import budget_capabilities
from tests.backend.model_platform.control_plane_support import (
    GatewayControlPlaneService,
    bedrock_publication,
)
from turnstile_core.config import Settings
from turnstile_core.domain.budget_counting import BudgetPublicationContracts, ModelCountContract
from turnstile_core.domain.budget_delegation import BudgetDelegationTarget, BudgetDelegationTrust
from turnstile_core.domain.control_plane import PublicationStatus
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.budget_settlement import BudgetScopeRepository


def ready_installation() -> tuple[Mock, Settings]:
    publication = GatewayControlPlaneService(InMemoryRepository()).seed_legacy_publication(
        bedrock_publication(), "unit",
    )
    target, epoch = publication.gateway_profile_id, uuid4()
    bindings = publication.desired_spec.bindings
    contract = ModelCountContract(
        model_id=uuid4(), input_ceiling=32768, output_ceiling=8192, output_field="max_tokens",
        operations=("anthropic-messages",), evidence_sha256="0" * 64,
        verified_at=datetime.now(UTC),
    )
    snapshot = BudgetPublicationContracts(
        publication_id=publication.id, gateway_profile_id=target, epoch=epoch,
        release_spec_sha256=publication.desired_spec_sha256,
        binding_sha256={b.model.model_key: BudgetPublicationContracts.binding_digest(b)
                        for b in bindings},
        model_contracts={b.model.model_key: contract for b in bindings},
    )
    publication = publication.model_copy(update={
        "status": PublicationStatus.ACTIVE, "apim_revision": "unit", "policy_sha256": "1" * 64,
        "resource_manifest": {"budget_contracts": snapshot.model_dump(mode="json")},
    })
    repository = Mock(spec=BudgetScopeRepository)
    repository.active_budget_execution_rollout.return_value = {
        "epoch": epoch, "gateway_profile_ids": [target],
        "prepared_period": datetime.now(UTC).date().replace(day=1),
    }
    repository.get_apim_target = Mock(return_value={"gateway_profile_id": target})
    repository.effective_gateway_publication = Mock(return_value=publication.model_dump())
    settings = Settings(
        budget_execution_enabled=True, budget_execution_target_ids=[target],
        budget_delegation_targets={target: BudgetDelegationTarget.model_validate({
            "audience": "api://unit-audience", "allowed_origins": ["https://gateway.example.com"],
        })},
        budget_delegation_trust={target: BudgetDelegationTrust(
            tenant_id=uuid4(), audience="unit-audience", client_ids=(uuid4(),),
            object_ids=(uuid4(),),
        )},
    )
    return repository, settings


def test_usd_capability_requires_bound_active_publication_and_current_period() -> None:
    repository, settings = ready_installation()
    result = budget_capabilities(repository, settings)
    assert result.usd_available and result.token_available
    assert result.reason_code == "ready"


@pytest.mark.parametrize("defect", [
    "disabled", "no_rollout", "period", "target", "delegation", "trust", "publication",
    "epoch", "tamper", "estimate",
])
def test_usd_capability_is_unavailable_without_complete_installation_evidence(defect: str) -> None:
    repository, settings = ready_installation()
    rollout = repository.active_budget_execution_rollout.return_value
    if defect == "disabled":
        settings = settings.model_copy(update={"budget_execution_enabled": False})
    elif defect == "no_rollout":
        repository.active_budget_execution_rollout.return_value = None
    elif defect == "period":
        rollout["prepared_period"] = None
    elif defect == "target":
        repository.get_apim_target.return_value = None
    elif defect == "delegation":
        settings = settings.model_copy(update={"budget_delegation_targets": {}})
    elif defect == "trust":
        settings = settings.model_copy(update={"budget_delegation_trust": {}})
    elif defect == "publication":
        repository.effective_gateway_publication.return_value = None
    elif defect == "epoch":
        rollout["epoch"] = uuid4()
    elif defect == "estimate":
        raw = repository.effective_gateway_publication.return_value
        for contract in raw["resource_manifest"]["budget_contracts"]["model_contracts"].values():
            contract.update(algorithm="legacy_request_estimate_v1", input_ceiling=0,
                            output_ceiling=0, output_estimate=4096)
    else:
        raw = repository.effective_gateway_publication.return_value
        raw["resource_manifest"]["budget_contracts"]["release_spec_sha256"] = "2" * 64
    result = budget_capabilities(repository, settings)
    assert not result.usd_available
    assert result.token_available
    assert result.message


def test_readiness_errors_do_not_disclose_database_or_credential_details() -> None:
    repository, settings = ready_installation()
    repository.active_budget_execution_rollout.side_effect = RuntimeError("private material")
    result = budget_capabilities(repository, settings)
    assert result.reason_code == "evidence_unavailable"
    assert "private" not in result.message
