from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID
from xml.etree import ElementTree

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import HttpUrl, SecretStr, ValidationError

from backend.http.publication_auth import require_publication_owner
from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.control_plane_support import (
    APIM_ID,
    FakeApimClient,
    GatewayControlPlaneService,
    GatewayPublicationWorker,
    PublicationAuthStore,
    activate_publication,
    bedrock_publication,
    external_tenant_foundry_publication,
    foundry_publication,
    publication_request,
    transition_publication,
)
from turnstile_core.config import Settings
from turnstile_core.domain.budget_counting import (
    BudgetPublicationContracts,
    ModelCountContract,
    NativeCountContract,
    ProtocolCountContract,
)
from turnstile_core.domain.control_plane import (
    COUNT_TOKENS_REJECTIONS,
    ApiFormat,
    AuthStrategy,
    GatewayBackendFailureStatusCodeRange,
    GatewayBackendPoolConfig,
    GatewayBackendPoolMember,
    GatewayBackendPoolMemberWrite,
    GatewayBackendPoolWrite,
    GatewayCredentialRotation,
    GatewayPublication,
    GatewayPublicationCreate,
    GatewayPublicationRetry,
    GatewayPublicationView,
    GatewayRateLimitCircuitBreaker,
    GatewayRateLimitResilience,
    ModelCreateTarget,
    ModelTarget,
    PublicationKind,
    PublicationStatus,
    RuntimeTarget,
    program_migration_spec_matches,
    publication_credential_evidence,
    publication_credential_states,
    publication_failure,
    publication_materialized_named_values,
    publication_model_id,
    publication_provisioning_completed,
    publication_retry_requires_credential,
)
from turnstile_core.domain.gateway_targets import GatewayServingDeployment
from turnstile_core.domain.model_availability import ModelAvailabilityNodeEnrollment
from turnstile_core.domain.runtime_models import (
    BrandKey,
    ModelConnectionCreate,
    ModelVendorKey,
    OAuthClientCredentialsConfig,
    PriceSource,
    ProviderTarget,
    model_vendor_label,
)
from turnstile_core.domain.storage_routes import (
    MAX_HEAD_BYTES,
    ActiveRouteHead,
    ConfigurationApproval,
    ConnectionApproval,
    ConnectionRevision,
    CountCredential,
    FixedProgramCutover,
    FixedProgramInstallation,
    FixedProgramUpgrade,
    ModelRouteAddition,
    ProgramCapabilityProof,
    ProgramIdentity,
    RouteApprovalRenewal,
    RouteCandidateProbe,
    RouteConfigurationPlan,
    RouteContent,
    RouteCredential,
    RoutePublicationTask,
    RouteScope,
    RouteSnapshot,
    RouteTransitionReceipt,
    RouteWriterReceipt,
    RouteWriterState,
    canonical_document,
    catalog_digest,
    changed_route_aliases,
    configuration_manifests,
    document_digest,
    parse_document,
    require_route_storage,
    text_digest,
    validate_origin,
    validate_path,
)
from turnstile_core.integrations.apim_control_plane import (
    ApimPolicyCompiler,
    AzureApimPublisherClient,
    BackendPoolResource,
)
from turnstile_core.integrations.apim_control_plane_contract import (
    CredentialSourceMissingError,
    PolicyCompilationError,
)
from turnstile_core.integrations.apim_policy_components import (
    component_digest,
    expression_token_spans,
    parse_policy,
    validate_executable_policy,
    validate_native_stream_policy,
)
from turnstile_core.integrations.apim_storage_routes import (
    FixedProgramVerifier,
    compact_fixed_policy,
    fixed_operation_contracts,
    fixed_policy_digest,
    fixed_program_digest,
)
from turnstile_core.integrations.apim_target_client_factory import ApimTargetClientConfig
from turnstile_core.integrations.route_credential_vault import VaultCheckpoint as _VaultCheckpoint
from turnstile_core.integrations.storage_route_projection import (
    ModelRouteActivationIntent,
    RouteActivationIntent,
    RouteProjectionConflict,
    RouteProjectionUnknown,
    RouteWriterIntent,
    TableRouteProjection,
    entity_document,
    require_route_batch,
    route_entity,
)
from turnstile_core.integrations.table_batch import conditional_batch
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository_publications import PostgreSqlPublicationRepositoryMixin
from turnstile_core.persistence.repository_support import (
    _model_addition_manifest,
    _model_removal_manifest,
)
from turnstile_core.pricing.catalog import CatalogEntry, CompositeCatalog
from turnstile_core.security import CredentialCipher
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    ControlPlaneUnavailableError,
)
from turnstile_core.services.probe_journal import PersistentProbeJournal
from turnstile_core.services.route_publication import (
    PersistentModelRouteActivationJournal,
    PersistentRouteActivationJournal,
    PersistentRouteWriterJournal,
    RouteConfigurationExecutor,
    RouteIntegrityCheckpoint,
    RouteIntegrityDeferred,
    _ActivationCheckpoint,
    _binding_route_contract,
    _CommitApprovalCheckpoint,
    _pool_matches_binding,
    _RouteProgress,
    build_route_publication_task,
    configuration_probe_steps,
    pool_header_name,
    pool_member_bindings,
    recorded_route_dependencies,
    recorded_route_scopes,
    recorded_route_snapshot,
    require_model_addition_activation,
    require_route_specification,
    route_credential_source,
)
from turnstile_core.services.storage_route_publisher import (
    FixedProgramMigration,
    FixedRoutePublisherFactory,
    FixedRoutePublisherPort,
    RouteCredentialVault,
    _CredentialBindings,
    _OAuthWriteIntent,
    _PoolWriteIntent,
    _PreparationCheckpoint,
    native_gateway_programs,
)


def test_publication_view_exposes_only_recorded_serving_targets() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "unit-owner",
    )
    assert GatewayPublicationView.from_publication(publication).target_deployments is None
    deployment = GatewayServingDeployment.model_validate({
        "deployment_id": str(UUID(int=71)), "native_gateway_id": "unit-edge",
        "kind": "self_hosted", "endpoint_url": "https://edge.example.test/unit/llm",
        "release_gate": "required",
    })
    binding = publication.desired_spec.bindings[-1].model_copy(update={
        "serving_deployments": (deployment,),
    })
    scoped = publication.model_copy(update={"desired_spec": publication.desired_spec.model_copy(
        update={"bindings": [*publication.desired_spec.bindings[:-1], binding],
                "gateway_deployments": (deployment,)},
    )})
    view = GatewayPublicationView.from_publication(scoped)
    assert view.target_deployments == (deployment,)
    assert view.model_dump(mode="json")["target_deployments"] == [
        deployment.model_dump(mode="json"),
    ]
    assert "unit-secret" not in view.model_dump_json()
    assert "desired_spec" not in view.model_dump()


@pytest.mark.parametrize(("message", "expected"), [
    ("opaque upstream secret", "publication_failed"),
    ("The current APIM policy changed after publication was queued", "parent_policy_changed"),
    ('APIM management failed: {"code":"AuthorizationFailed","status":403}',
     "publisher_authorization_failed"),
    ('Publisher permission preflight failed: {"code":"publisher_permissions_unverified"}',
     "publisher_permissions_unverified"),
    ('Publisher permission preflight failed: {"code":"publisher_permissions_missing",'
     '"missing_actions":["Microsoft.ApiManagement/service/apis/write"],'
     '"request_id":"00000000-0000-4000-8000-000000000123"}', "publisher_permissions_missing"),
    ('Publisher permission preflight failed: {"code":"publisher_permissions_missing",'
     '"missing_actions":["opaque upstream secret"]}', "publication_failed"),
])
def test_publication_failure_is_structured_and_never_echoes_raw_errors(
    message: str, expected: str,
) -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "unit-owner",
    )
    assert publication_failure(publication) is None
    failed = publication.model_copy(update={
        "status": PublicationStatus.FAILED, "error_message": message,
    })
    result = publication_failure(failed, stage=PublicationStatus.VALIDATING)
    assert result is not None and result.code == expected
    assert result.stage is PublicationStatus.VALIDATING
    assert "opaque upstream secret" not in result.model_dump_json()
    view = GatewayPublicationView.from_publication(failed)
    assert view.failure is not None and view.failure.code == expected
    if expected == "publisher_permissions_missing":
        assert result.request_id == UUID("00000000-0000-4000-8000-000000000123")
        assert result.missing_actions == ["Microsoft.ApiManagement/service/apis/write"]


@pytest.mark.parametrize("error_code", ["PolicyCompilationError", "opaque upstream secret"])
def test_generic_publication_failure_names_the_publication(
    error_code: str,
) -> None:
    # "Publication failed" alone left no reference to look up in the logs.
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "unit-owner",
    )
    failed = publication.model_copy(update={
        "status": PublicationStatus.FAILED, "error_code": error_code,
        "error_message": "Fixed route dependency preflight is unsupported",
    })

    result = publication_failure(failed)

    assert result is not None and result.code == "publication_failed"
    assert str(publication.id) in result.message
    assert error_code not in result.message
    assert "preflight" not in result.message


@pytest.mark.parametrize(("error_code", "message", "expected"), [
    ("PolicyCompilationError", "Route credential has no approved original source",
     "credential_source_missing"),
    ("PolicyCompilationError", "Deployment metadata credential is unavailable",
     "credential_unreadable"),
    ("PolicyCompilationError", "Route publication credential is unavailable",
     "credential_unreadable"),
    ("PolicyCompilationError", "Deployment credential vault is unavailable",
     "credential_vault_unavailable"),
    ("PolicyCompilationError", "Route credential materialization is unavailable",
     "credential_vault_unavailable"),
    ("PolicyCompilationError", "Route credential Vault is not approved for this program",
     "credential_vault_unavailable"),
    ("ValueError", "Route publication credential is unavailable", "publication_failed"),
    ("PolicyCompilationError", "opaque upstream secret", "publication_failed"),
])
def test_known_credential_failures_have_safe_reasons(
    error_code: str, message: str, expected: str,
) -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "unit-owner",
    )
    failed = publication.model_copy(update={
        "status": PublicationStatus.FAILED, "error_code": error_code, "error_message": message,
    })
    result = publication_failure(failed)
    assert result is not None and result.code == expected
    assert message not in result.model_dump_json()
    if expected == "publication_failed":
        assert str(publication.id) in result.message
    view = GatewayPublicationView.from_publication(failed)
    assert view.failure is not None and view.failure.code == expected


@pytest.mark.parametrize(("details", "expected"), [
    ({"http_status": 400, "upstream_code": "PermissionDenied", "auth": "managed_identity"},
     "returned HTTP 400 (PermissionDenied). "
     "Check that the Publisher managed identity can read this Foundry project."),
    ({"http_status": 401, "upstream_code": "PermissionDenied"},
     "returned HTTP 401 (PermissionDenied)."),
    ({"http_status": 500, "auth": "managed_identity"}, "returned HTTP 500."),
    ({"http_status": 403, "upstream_code": "private code with spaces"}, None),
    ({"http_status": 403, "auth": "private"}, None),
])
def test_deployment_directory_failure_keeps_the_upstream_code_and_identity_hint(
    details: dict[str, object], expected: str | None,
) -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "unit-owner",
    )
    failed = publication.model_copy(update={
        "status": PublicationStatus.FAILED, "error_code": "PolicyCompilationError",
        "error_message": "Deployment validation failed: " + json.dumps({
            "code": "deployment_metadata_unavailable", "deployment": "unit-deployment",
            **details,
        }),
    })
    result = publication_failure(failed)
    assert result is not None and "private" not in result.model_dump_json()
    if expected is None:
        assert result.code == "publication_failed"
        return
    assert result.code == "deployment_metadata_unavailable"
    assert result.http_status == details["http_status"]
    assert result.message == (
        "Deployment 'unit-deployment' could not be verified; the deployment directory "
        + expected
    )


def test_legacy_publication_hash_omits_only_empty_deployment_fields() -> None:
    repository = InMemoryRepository()
    repository.models = []
    service = GatewayControlPlaneService(repository)
    publication = service.publish(bedrock_publication(), "unit-owner")
    payload, digest = service._release_payload(publication.desired_spec)
    assert "gateway_deployments" not in payload
    assert all("deployment_scope" not in binding and "serving_deployments" not in binding
               and "logical_model_id" not in binding for binding in payload["bindings"])
    expected = hashlib.sha256(json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert digest == expected == publication.desired_spec_sha256


@pytest.mark.parametrize("change", [
    {"endpoint": "http://count.example.test/messages/count_tokens"},
    {"endpoint": "https://count.example.test/messages"},
    {"endpoint": "https://user:secret@count.example.test/messages/count_tokens"},
    {"endpoint": "https://count.example.test/messages/count_tokens?key=unit"},
    {"upper_bound_padding_tokens": 10},
    {"upper_bound_padding_tokens": True},
    {"supported_features": ["server_tools"]},
    {"supported_features": ["background"]},
])
def test_budget_native_count_contract_rejects_unsafe_guarantees(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        NativeCountContract.model_validate({
            "kind": "anthropic_messages",
            "endpoint": "https://count.example.test/messages/count_tokens",
            "evidence_sha256": "a" * 64,
            **change,
        })


@pytest.mark.parametrize("change", [
    {"input_ceiling": True},
    {"input_ceiling": 2**63 - 1},
    {"operations": ["images-generations"]},
    {"operations": ["responses-compact"]},
    {"output_field": "none"},
    {"output_estimate": 4096},
    {"algorithm": "legacy_request_estimate_v1", "output_estimate": 4096},
])
def test_budget_count_contract_does_not_conflate_estimates_and_ceilings(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ProtocolCountContract.model_validate({
            "input_ceiling": 1000, "output_ceiling": 100, "output_field": "max_tokens",
            "operations": ["anthropic-messages"], "evidence_sha256": "a" * 64,
            "verified_at": "2026-09-22T00:00:00Z", **change,
        })


def test_budget_count_contract_retains_protocol_specific_estimates() -> None:
    text = ProtocolCountContract.model_validate({
        "algorithm": "legacy_request_estimate_v1", "output_estimate": 4096,
        "output_field": "max_tokens", "operations": ["anthropic-messages"],
        "evidence_sha256": "a" * 64, "verified_at": "2026-09-22T00:00:00Z",
    })
    images = ProtocolCountContract.model_validate({
        **text.model_dump(), "output_field": "none", "operations": ["images-generations"],
    })
    assert text.output_field == "max_tokens" and images.output_field == "none"
    assert text.input_ceiling == images.input_ceiling == 0
    assert text.native_counter is images.native_counter is None


def budget_publication_contract_fixture() -> tuple[GatewayPublication, dict[str, Any]]:
    repository = InMemoryRepository()
    repository.models = []
    request = foundry_publication()
    request.provider = ProviderTarget(existing_id=next(
        item["id"] for item in repository.providers if item["brand_key"] == "microsoft_foundry"
    ))
    service = GatewayControlPlaneService(repository, apim_principal_id=str(UUID(int=99)))
    publication = service.publish(request, "owner@example.com")
    return publication, {
        "protocol_version": 2, "mode": "active", "epoch": str(UUID(int=2)),
        "gateway_profile_id": str(publication.gateway_profile_id), "model_contracts": {},
    }


def test_budget_publication_default_is_explicit_estimate_with_public_model_identity() -> None:
    publication, fence = budget_publication_contract_fixture()
    snapshot = BudgetPublicationContracts.capture(publication, {"models": []}, fence)
    snapshot.validate_publication(publication)
    binding = publication.desired_spec.bindings[-1]
    contract = snapshot.model_contracts[binding.model.model_key]
    assert contract.model_id == publication_model_id(publication.id, binding.model.model_key)
    assert contract.algorithm == "legacy_request_estimate_v1"
    assert contract.native_counter is None and contract.input_ceiling == 0
    assert contract.protocols["responses-compact"].output_field == "none"
    assert contract.protocols["responses"].output_field == "max_output_tokens"


@pytest.mark.parametrize("change", [
    {"mode": "prepared"}, {"protocol_version": 1},
    {"gateway_profile_id": str(UUID(int=9))}, {"model_contracts": None},
])
def test_budget_publication_requires_active_exact_fence(change: dict[str, Any]) -> None:
    publication, fence = budget_publication_contract_fixture()
    with pytest.raises(ValueError):
        BudgetPublicationContracts.capture(publication, {"models": []}, {**fence, **change})


def test_budget_publication_rejects_rebound_publication_and_modified_route() -> None:
    publication, fence = budget_publication_contract_fixture()
    snapshot = BudgetPublicationContracts.capture(publication, {"models": []}, fence)
    with pytest.raises(ValueError, match="another publication"):
        snapshot.validate_publication(publication.model_copy(update={"id": UUID(int=9)}))
    binding = publication.desired_spec.bindings[-1].model_copy(update={"backend_path": "/changed"})
    changed = publication.model_copy(update={
        "desired_spec": publication.desired_spec.model_copy(update={"bindings": [binding]}),
    })
    with pytest.raises(ValueError, match="binding changed"):
        snapshot.validate_publication(changed)


def test_budget_existing_model_cannot_fall_back_to_unapproved_counting() -> None:
    publication, fence = budget_publication_contract_fixture()
    binding = publication.desired_spec.bindings[-1]
    registry = {"models": [{"id": str(UUID(int=5)), "model_key": binding.model.model_key,
                            "runtime_id": binding.runtime_id}]}
    with pytest.raises(ValueError, match="missing its approved budget contract"):
        BudgetPublicationContracts.capture(publication, registry, fence)


@pytest.mark.parametrize("fault", [
    None, "epoch", "gateway", "inactive", "future", "model", "changed_route",
])
def test_budget_route_reconcile_inherits_independently_active_model_contract(
    fault: str | None,
) -> None:
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherFactory

    source, fence = budget_publication_contract_fixture()
    source_binding = source.desired_spec.bindings[0]
    alias = source_binding.model.model_key
    original = BudgetPublicationContracts.model_validate({
        "publication_id": source.id, "gateway_profile_id": source.gateway_profile_id,
        "epoch": fence["epoch"], "release_spec_sha256": source.desired_spec_sha256,
        "binding_sha256": {alias: hashlib.sha256(json.dumps(
            source_binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()},
        "model_contracts": {alias: {
            "model_id": UUID(int=6000), "algorithm": "legacy_request_estimate_v1",
            "output_estimate": 256, "output_field": "max_completion_tokens",
            "operations": ["chat-completions"], "evidence_sha256": "c" * 64,
            "verified_at": source.created_at,
        }},
    })
    source = source.model_copy(update={
        "status": PublicationStatus.ACTIVE,
        "resource_manifest": {"budget_contracts": original.model_dump(mode="json")},
    })
    previous = source.model_copy(update={
        "id": UUID(int=6001),
        "desired_spec": source.desired_spec.model_copy(update={"bindings": []}),
        "resource_manifest": {},
    })
    baseline = BudgetPublicationContracts.capture(previous, {"models": []}, fence)
    previous = previous.model_copy(update={
        "resource_manifest": {"budget_contracts": baseline.model_dump(mode="json")},
    })
    publication = source.model_copy(update={
        "id": UUID(int=6002), "publication_kind": PublicationKind.ROUTE_RECONCILE,
        "created_at": source.created_at + timedelta(seconds=1), "resource_manifest": {},
    })
    binding = publication.desired_spec.bindings[0]
    contract = original.model_contracts[binding.model.model_key]
    registry = {"models": [{
        "id": str(contract.model_id), "model_key": binding.model.model_key,
        "runtime_id": binding.runtime_id,
    }]}
    if fault == "epoch":
        original = original.model_copy(update={"epoch": UUID(int=6003)})
    elif fault == "gateway":
        source = source.model_copy(update={"gateway_profile_id": UUID(int=6003)})
        original = original.model_copy(update={"gateway_profile_id": UUID(int=6003)})
    elif fault == "inactive":
        source = source.model_copy(update={"status": PublicationStatus.FAILED})
    elif fault in {"future", "model"}:
        changed_contract = contract.model_copy(update={
            "verified_at": publication.created_at + timedelta(seconds=1),
        } if fault == "future" else {"model_id": UUID(int=6003)})
        original = original.model_copy(update={
            "model_contracts": {binding.model.model_key: changed_contract},
        })
    elif fault == "changed_route":
        verified = ModelCountContract.model_validate({
            "model_id": contract.model_id,
            "input_ceiling": 1000, "output_ceiling": 100,
            "output_field": "max_completion_tokens", "operations": ["chat-completions"],
            "evidence_sha256": "a" * 64, "verified_at": source.created_at,
        })
        original = original.model_copy(update={
            "model_contracts": {binding.model.model_key: verified},
        })
        publication = publication.model_copy(update={
            "desired_spec": publication.desired_spec.model_copy(update={
                "bindings": [binding.model_copy(update={"backend_path": "/changed"})],
            }),
        })
    source = source.model_copy(update={
        "resource_manifest": {"budget_contracts": original.model_dump(mode="json")},
    })
    repository = MagicMock(spec=InMemoryRepository)
    repository.registry.return_value = registry
    repository.active_model_route_publications.return_value = [source.model_dump(mode="json")]
    factory = FixedRoutePublisherFactory(
        repository, MagicMock(), active_fence=lambda _target: fence,
        availability_projector=MagicMock(),
    )
    if fault is not None:
        with pytest.raises(ValueError):
            factory._budget_contracts(publication, previous)
        return
    snapshot = factory._budget_contracts(publication, previous)
    snapshot.validate_publication(publication)
    assert snapshot.model_contracts[binding.model.model_key] == contract


@pytest.mark.parametrize("identity", [
    {"deployment_name": "gpt-4.1-845778"},
    {"model_key": "gpt-4.1", "display_name": "GPT-4.1", "upstream_model_id": "gpt-4.1"},
])
def test_new_model_contract_preserves_catalog_pricing(identity: dict[str, str]) -> None:
    model = ModelCreateTarget.model_validate({
        **identity,
        "price_source": "azure_retail",
        "price_reference": "azure_retail:Azure OpenAI:gpt 4.1:Global:*",
        "price_discount_percent": 80,
    })

    assert model.price_source is PriceSource.AZURE_RETAIL
    assert model.price_reference == "azure_retail:Azure OpenAI:gpt 4.1:Global:*"
    assert model.price_discount_percent == 80
    restored = ModelCreateTarget.model_validate(model.model_dump())
    assert restored.price_reference == model.price_reference


@pytest.mark.parametrize("changes", [
    {"price_source": "azure_retail"},
    {"price_source": "anthropic", "price_reference": "azure_retail:wrong"},
    {"price_discount_percent": 0}, {"price_discount_percent": 101},
    {"price_discount_percent": float("nan")}, {"price_discount_percent": float("inf")},
    {"operation": "image_generation", "price_source": "anthropic",
     "price_reference": "anthropic:unit-image"},
])
def test_new_model_contract_rejects_invalid_pricing(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelCreateTarget.model_validate({"deployment_name": "unit-deployment", **changes})


@pytest.mark.parametrize("method,field,phase", [
    ("checkpoint_gateway_publication_probes", "probe_verification", "verifying"),
    ("checkpoint_gateway_publication_activation", "route_activation", "promoting"),
    ("checkpoint_gateway_publication_writer", "route_writer", "promoting"),
    ("checkpoint_gateway_publication_dependencies", "route_dependencies", "queued"),
    ("checkpoint_gateway_publication_credentials", "route_credentials", "queued"),
    ("checkpoint_gateway_publication_preparation", "route_preparation", "queued"),
    ("checkpoint_gateway_program_promotion", "route_program_promotion", "promoting"),
    ("checkpoint_gateway_publication_approval", "route_commit_approval", "promoting"),
])
def test_publication_checkpoint_cas_requires_phase_lease_and_expected_old_value(
    method: str, field: str, phase: str,
) -> None:
    repository = InMemoryRepository()
    publication_id = UUID(int=911)
    row: dict[str, Any] = {
        "id": publication_id, "status": phase, "resource_manifest": {"unchanged": "unit"},
    }
    repository.gateway_publications.append(row)
    lease = {"publication_id": publication_id, "status": "leased", "lease_owner": "unit-worker",
             "lease_expires_at": datetime.now(UTC) + timedelta(minutes=1)}
    repository.gateway_publication_outbox.append(lease)
    save = getattr(repository, method)
    candidate: dict[str, Any] = {"version": 1, "steps": {"a" * 64: "started"}}
    assert not save(publication_id, "other-worker", None, candidate)
    assert save(publication_id, "unit-worker", None, candidate)
    assert row["resource_manifest"]["unchanged"] == "unit"
    candidate["steps"]["a" * 64] = "passed"
    assert row["resource_manifest"][field]["steps"]["a" * 64] == "started"
    assert not save(publication_id, "unit-worker", None, candidate)
    expected = row["resource_manifest"][field]
    assert save(publication_id, "unit-worker", expected, candidate)
    assert len(repository.gateway_publication_audit) == 2
    row["status"] = "failed"
    assert not save(publication_id, "unit-worker", candidate, {})
    row["status"] = phase
    lease["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    assert not save(publication_id, "unit-worker", candidate, {})

    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {"id": publication_id}
    sql = PostgreSqlPublicationRepositoryMixin()
    with patch.object(sql, "_connection", return_value=connection):
        assert getattr(sql, method)(publication_id, "unit-worker", expected, candidate)
    statement, parameters = connection.__enter__().execute.call_args.args
    assert "FOR UPDATE OF outbox" in statement and "lease_expires_at > now()" in statement
    assert "IS NOT DISTINCT FROM %(expected)s::jsonb" in statement
    assert parameters["phase"] == phase and parameters["field"] == field


def test_publication_probe_request_reservation_caps_dispatches_and_checks_lease() -> None:
    repository = InMemoryRepository()
    publication_id = UUID(int=912)
    row = {"id": publication_id, "status": "verifying", "resource_manifest": {}}
    repository.gateway_publications.append(row)
    repository.gateway_publication_outbox.append({
        "publication_id": publication_id, "status": "leased", "lease_owner": "unit-worker",
        "lease_expires_at": datetime.now(UTC) + timedelta(minutes=1),
    })
    assert not repository.reserve_gateway_publication_probe(
        publication_id, "other-worker", 1, "POST", "a" * 64,
    )
    assert repository.reserve_gateway_publication_probe(
        publication_id, "unit-worker", 1, "POST", "a" * 64,
    )
    assert not repository.reserve_gateway_publication_probe(
        publication_id, "unit-worker", 1, "POST", "a" * 64,
    )
    assert repository.gateway_publication_audit[-1]["detail"]["probe_request"]["request_index"] == 1
    with pytest.raises(ValueError, match="must be positive"):
        repository.reserve_gateway_publication_probe(publication_id, "unit-worker", 0, "POST", "a")


@pytest.mark.parametrize("marker", ["model_route_addition", "model_route_removal"])
def test_model_transaction_activation_requires_validator_before_mutation(marker: str) -> None:
    repository = InMemoryRepository()
    repository.providers, repository.runtimes, repository.models = [], [], []
    publication = GatewayControlPlaneService(
        repository, apim_principal_id=str(UUID(int=902)),
    ).publish(foundry_publication(), "unit-owner")
    row = repository.get_gateway_publication(publication.id)
    assert row is not None
    row.update(status="promoting", resource_manifest={marker: {"version": 1}})
    assert repository.claim_gateway_publication("unit-worker", 180) is not None
    models = [dict(model) for model in repository.models]
    with pytest.raises(ValueError, match="complete transaction validator"):
        repository.activate_gateway_publication(publication.id, "unit-worker")
    assert repository.models == models and row["status"] == "promoting"

    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.side_effect = [{"id": UUID(int=914)}, row]
    sql = PostgreSqlPublicationRepositoryMixin()
    with (
        patch.object(sql, "_connection", return_value=connection),
        pytest.raises(ValueError, match="complete transaction validator"),
    ):
        sql.activate_gateway_publication(publication.id, "unit-worker")
    assert all(call.args[0].strip().startswith("SELECT")
               for call in connection.execute.call_args_list)
    connection.reset_mock()
    connection.execute.return_value.fetchone.side_effect = None
    connection.execute.return_value.fetchone.return_value = {**row, "status": "active"}
    with patch.object(PostgreSqlPublicationRepositoryMixin, "_sync_active_route_bindings"):
        completed = sql._complete_gateway_activation(
            connection, row, publication.id, "unit-worker",
        )
    assert completed["status"] == "active"
    statements = [call.args[0] for call in connection.execute.call_args_list]
    assert all("effective_gateway_release" not in statement for statement in statements)
    assert all("superseded" not in statement for statement in statements)


def test_active_model_route_catalog_is_target_scoped_sorted_and_excludes_active_removals() -> None:
    repository = InMemoryRepository()
    manifest: dict[str, Any]
    for identity, target_id, kind, state, manifest in (
        (UUID(int=34), APIM_ID, "model_add", "active", {"model_route_addition": {}}),
        (UUID(int=33), APIM_ID, "model_add", "active", {"model_route_addition": {}}),
        (UUID(int=35), APIM_ID, "model_add", "failed", {"model_route_addition": {}}),
        (UUID(int=36), UUID(int=39), "model_add", "active", {"model_route_addition": {}}),
        (UUID(int=37), APIM_ID, "model_add", "active", {}),
    ):
        repository.gateway_publications.append({"id": identity, "gateway_profile_id": target_id,
            "publication_kind": kind, "status": state, "resource_manifest": manifest})
    assert [row["id"] for row in repository.active_model_route_publications(APIM_ID)] == [
        UUID(int=33), UUID(int=34),
    ]
    repository.gateway_publications.append({"id": UUID(int=38), "gateway_profile_id": APIM_ID,
        "publication_kind": "model_remove", "status": "active", "resource_manifest": {
            "model_route_removal": {"source_publication_id": str(UUID(int=33))},
        }})
    assert [row["id"] for row in repository.active_model_route_publications(APIM_ID)] == [
        UUID(int=34),
    ]
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = []
    sql = PostgreSqlPublicationRepositoryMixin()
    with patch.object(sql, "_connection", return_value=connection):
        assert sql.active_model_route_publications(APIM_ID) == []
    statement, parameters = connection.execute.call_args.args
    assert statement.strip().startswith("SELECT") and "LIMIT 10001" in statement
    assert "NOT EXISTS" in statement and "source_publication_id" in statement
    assert parameters == (APIM_ID,)


@pytest.mark.parametrize("kind,status,code", [
    ("unavailable", 404, "count_tokens_unavailable"),
    ("unpublished", 400, "model_not_published"),
])
def test_count_token_rejections_match_exact_source_protocol(
    kind: str, status: int, code: str,
) -> None:
    rejection = COUNT_TOKENS_REJECTIONS[kind]
    payload = rejection.payload()
    assert rejection.status_code == status and rejection.code == code
    assert rejection.matches(status, payload)
    assert not rejection.matches(503, payload)
    assert not rejection.matches(status, {"type": "error", "error": {
        "type": rejection.error_type, "code": "other", "message": rejection.message,
    }})
    assert not rejection.matches(status, {"type": "error", "error": {
        "type": rejection.error_type, "code": code, "message": "  ",
    }})


def test_concrete_route_probe_usage_is_exact_native_and_never_fabricates_missing_counters() -> None:
    usage = FixedRoutePublisherPort._usage({"usage": {
        "input_tokens": 10, "output_tokens": 3,
        "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 1},
    }}, "microsoft-decision")
    assert (usage.input_tokens, usage.cached_tokens, usage.cache_write_tokens,
            usage.output_tokens) == (7, 3, 1, 3)
    assert usage.estimated is False
    for counters in ({"input_tokens": True, "output_tokens": 3},
                     {"input_tokens": 10},
                     {"input_tokens": 10, "output_tokens": 3, "total_tokens": 12}):
        with pytest.raises(ValueError, match="exact measured usage"):
            FixedRoutePublisherPort._usage({"usage": counters}, "microsoft-decision")
    chat = {"choices": [{}], "usage": {
        "prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
    }}
    assert FixedRoutePublisherPort._usage(chat, "chat-completions").input_tokens == 10
    with pytest.raises(ValueError, match="incomplete"):
        FixedRoutePublisherPort._usage({**chat, "choices": []}, "chat-completions")


def test_route_credential_vault_persists_encrypted_intent_once_and_recovers_readonly() -> None:
    cipher = CredentialCipher(Fernet.generate_key())
    token = MagicMock()
    token.token.return_value = "unit-only-token"
    saved: list[dict[str, Any]] = []
    requests: list[str] = []
    document: dict[str, Any] = {}
    recovered = False

    def dispatch(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.method == "PUT":
            assert len(saved) == 1 and "unit-only-value" not in canonical_document(saved[0])
            document.update(json.loads(request.content))
            document["id"] = str(request.url).split("?")[0] + "/" + "a" * 32
            raise httpx.ReadTimeout("unit-only unknown response")
        if not document or not recovered:
            return httpx.Response(404)
        return httpx.Response(200, json=document)

    with httpx.Client(transport=httpx.MockTransport(dispatch)) as client:
        vault = RouteCredentialVault("https://unitvault.vault.azure.net", cipher, token, client,
                                      clock=lambda: 101)
        with pytest.raises(RuntimeError, match="read-only recovery"):
            vault.materialize(UUID(int=1), "b" * 64, SecretStr("unit-only-value"), checkpoint=None,
                              persist=lambda old, new: saved.append(new))
        assert requests == ["GET", "PUT", "GET"]
        recovered = True
        secret_id = vault.materialize(
            UUID(int=1), "b" * 64, SecretStr("unit-only-value"), checkpoint=saved[0],
            persist=lambda old, new: saved.append(new),
        )
        assert requests == ["GET", "PUT", "GET", "GET"]
        verified = vault.verify(secret_id, SecretStr("unit-only-value"))
        assert verified.get_secret_value() == "unit-only-value"
        with pytest.raises(ValueError, match="CHECKPOINT_CHANGED"):
            vault.materialize(UUID(int=1), "b" * 64, SecretStr("other-value"), checkpoint=saved[-1],
                              persist=lambda old, new: saved.append(new))
        with pytest.raises(ValueError, match="TARGET_INVALID"):
            vault._request("GET", "https://other.vault.azure.net/secrets/unit")
        assert requests.count("PUT") == 1


@pytest.mark.parametrize("failure", ["credential", "budget", "program"])
@pytest.mark.parametrize("fault", [
    None, "prepared", "probed", "paid", "leased", "revision", "policy", "base", "spec",
    "method", "path", "status", "code",
])
def test_pool_resubmission_preserves_zero_effect_credential_denial(
    fault: str | None, failure: str,
) -> None:
    from copy import deepcopy

    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository, apim_principal_id=str(UUID(int=705)))
    request = foundry_publication()
    request.provider = ProviderTarget(existing_id=next(
        item["id"] for item in repository.providers if item["brand_key"] == "microsoft_foundry"
    ))
    baseline = service.seed_legacy_publication(request, "unit-owner")
    baseline_row = repository.get_gateway_publication(baseline.id)
    assert baseline_row is not None
    baseline_row.update(status="active", resource_manifest={"fixed_route_program": {}})
    repository.effective_gateway_releases[APIM_ID] = baseline.id
    repository.gateway_publication_outbox[0].update(status="completed")
    spec = baseline.desired_spec.model_dump(mode="python")
    digest = "d" * 64
    failed = repository.create_gateway_publication(
        APIM_ID, spec, digest, "route_reconcile", "unit-owner",
        expected_base_release_id=baseline.id,
    )
    diagnostic = {"method": "POST", "path": "/namedValues/unit-observer-key/listValue",
                  "status": 403, "code": "AuthorizationFailed"}
    if fault in {"method", "path", "status", "code"}:
        diagnostic[fault] = {"method": "PUT", "path": "/backends/unit-backend",
                             "status": 400, "code": "BadRequest"}[fault]
    failed.update(status="failed", error_code="PolicyCompilationError",
                  error_message="APIM management failed: " + json.dumps(diagnostic))
    if failure == "budget":
        failed.update(
            error_code="ValueError",
            error_message="An existing model is missing its approved budget contract",
            resource_manifest={"route_credentials": {}, "route_preparation": {},
                               "route_dependencies": {}, "route_last_progress_attempt": 1},
        )
        if fault in {"method", "path", "status", "code"}:
            failed["error_message"] += " (different failure)"
    elif failure == "program":
        failed.update(
            error_code="ValidationError",
            error_message="1 validation error for RoutePublicationTask\n"
            "  Value error, ROUTE_PUBLICATION_TASK_INVALID [type=value_error, "
            "input_value={}, input_type=dict]",
            resource_manifest={"route_credentials": {}, "route_dependencies": {},
                               "route_last_progress_attempt": 1},
        )
        if fault in {"method", "path", "status", "code"}:
            failed["error_message"] = failed["error_message"].replace(
                "ROUTE_PUBLICATION_TASK_INVALID", "OTHER_TASK_FAILURE",
            )
    outbox = repository.gateway_publication_outbox[-1]
    outbox.update(status="completed", lease_owner=None, lease_expires_at=None)
    if fault == "prepared":
        failed["resource_manifest"][
            "route_preparation" if failure == "credential" else "route_cutover"
        ] = {}
    elif fault == "probed":
        repository.gateway_publication_audit.append({
            "publication_id": failed["id"], "detail": {"probe_request": {}},
        })
    elif fault == "paid":
        repository.billable_requests[UUID(int=707)] = MagicMock(
            scope_type="system", scope_id=f"publication:{failed['id']}",
        )
    elif fault == "leased":
        outbox["status"] = "leased"
    elif fault == "revision":
        failed["apim_revision"] = "candidate"
    elif fault == "policy":
        failed["policy_sha256"] = "e" * 64
    elif fault == "base":
        failed["base_release_id"] = UUID(int=706)
    elif fault == "spec":
        failed["desired_spec"] = {**spec, "discovery_models": []}
    preserved = deepcopy(failed)

    def submit() -> dict[str, Any]:
        return repository.create_gateway_publication(
            APIM_ID, spec, digest, "route_reconcile", "unit-owner",
            expected_base_release_id=baseline.id,
        )

    if fault is None:
        created = submit()
        assert created["id"] != failed["id"]
        assert created["status"] == "queued"
        assert created["base_release_id"] == baseline.id
        assert created["desired_spec"] == spec
    else:
        with pytest.raises(ValueError):
            submit()
    assert failed == preserved
    assert outbox["status"] == ("leased" if fault == "leased" else "completed")


def test_fixed_policy_compaction_preserves_xml_strings_and_executable_tokens() -> None:
    expression = ('@{ var url = "https://unit.example.test/a b";\n'
                  ' // unit comment\n return url + " x"; }')
    spans = expression_token_spans(expression)
    assert all(expression[start:end] == token for token, start, end in spans)
    assert not any(token.startswith("//") for token, _, _ in spans)
    root = ElementTree.Element("policies")
    inbound = ElementTree.SubElement(root, "inbound")
    ElementTree.SubElement(inbound, "set-variable", {"name": "unit", "value": expression})
    source = ElementTree.tostring(root, encoding="unicode")
    compacted = compact_fixed_policy(source)
    assert len(compacted) < len(source)
    original_digest = component_digest(parse_policy(source), normalize_text_defaults=False)
    compacted_digest = component_digest(parse_policy(compacted), normalize_text_defaults=False)
    assert compacted_digest == original_digest
    assert validate_executable_policy(compacted) == compacted
    with pytest.raises(ValueError, match="unbound parameters"):
        validate_executable_policy(
            '<policies><inbound><set-header name="__UNIT__"/></inbound></policies>',
        )
    with pytest.raises(ValueError, match="unsupported XML namespaces"):
        compact_fixed_policy('<policies xmlns="urn:unit"><inbound/></policies>')


def test_fixed_program_verifier_requires_current_management_and_exact_authenticated_head() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    contracts = fixed_operation_contracts()
    operation = ("<policies><inbound><base/></inbound><backend><base/></backend>"
                 "<outbound><base/></outbound><on-error><base/></on-error></policies>")
    parent = operation.replace("<inbound><base/>", '<inbound><base/><set-variable '
                                'name="routeProgram" value="' + "0" * 64 + '"/>'
                                '<set-variable name="routeVaultOrigins" value="[]"/>')
    policies = {name: operation for name in contracts}
    digest = fixed_program_digest(parent, policies, tuple((name, *route)
                                                          for name, route in contracts.items()))
    parent = parent.replace("0" * 64, digest)
    identity = ProgramIdentity(scope=scope, api_revision="1", instance_id="managed",
                                program_sha256=digest, trust_binding_sha256="a" * 64)
    installed = FixedProgramInstallation(
        program=identity, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net",
        storage_table="TurnstileRoutes", gateway_endpoint="https://unit-apim.example.test/llm",
        publisher_subscription_id="unit-only",
        parent_policy_sha256=fixed_policy_digest(parent), supported_profiles=("openai-v1",),
        operation_contracts=contracts,
        operation_policies_sha256={
            name: fixed_policy_digest(policy) for name, policy in policies.items()
        },
    )
    management = MagicMock()
    management.current_revision.return_value = "1"
    management.revision_api_policy.return_value = parent
    management.route_program_operations.return_value = {
        name: (*contract, policies[name]) for name, contract in contracts.items()
    }
    wrong_head = False
    requests: list[str] = []

    def probe(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        fields = {"abi": "2", "nonce": request.headers["x-turnstile-route-probe"],
                  "target": scope.target_id, "api": scope.api_id, "revision": "1",
                  "gateway": "managed", "instance": "other" if wrong_head else "managed",
                  "program": digest, "trust": identity.trust_binding_sha256}
        return httpx.Response(204, headers={"x-turnstile-route-" + name: value
                                            for name, value in fields.items()})

    with httpx.Client(transport=httpx.MockTransport(probe)) as client:
        verifier = FixedProgramVerifier(installed, management, SecretStr("unit-only"), client,
                                         clock=lambda: 101)
        proof = verifier.verify()
        assert proof.identity == identity
        assert proof.issued_epoch == 101 and proof.expires_epoch == 221
        management.current_revision.return_value = "2"
        with pytest.raises(ValueError, match="revision changed"):
            verifier.verify()
        assert requests == ["HEAD"]
        management.current_revision.return_value = "1"
        wrong_head = True
        with pytest.raises(ValueError, match="authenticated probe identity changed"):
            verifier.verify()
        assert requests == ["HEAD", "HEAD"]


def _timed_program_verifier(
    parent_seconds: float, operation_seconds: float, fault: str | None = None,
) -> FixedProgramVerifier:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    contracts = fixed_operation_contracts()
    operation = ("<policies><inbound><base/></inbound><backend><base/></backend>"
                 "<outbound><base/></outbound><on-error><base/></on-error></policies>")
    parent = operation.replace("<inbound><base/>", '<inbound><base/><set-variable '
                                'name="routeProgram" value="' + "0" * 64 + '"/>'
                                '<set-variable name="routeVaultOrigins" value="[]"/>')
    policies = {name: operation for name in contracts}
    digest = fixed_program_digest(parent, policies, tuple((name, *route)
                                                          for name, route in contracts.items()))
    parent = parent.replace("0" * 64, digest)
    identity = ProgramIdentity(scope=scope, api_revision="1", instance_id="managed",
                                program_sha256=digest, trust_binding_sha256="a" * 64)
    installed = FixedProgramInstallation(
        program=identity, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net",
        storage_table="TurnstileRoutes", gateway_endpoint="https://unit-apim.example.test/llm",
        publisher_subscription_id="unit-only",
        parent_policy_sha256=fixed_policy_digest(parent), supported_profiles=("openai-v1",),
        operation_contracts=contracts,
        operation_policies_sha256={
            name: fixed_policy_digest(policy) for name, policy in policies.items()
        },
    )
    now = [1000.0]
    changed = operation.replace("<base/></inbound>", '<base/><set-variable name="unitChanged" '
                                'value="1"/></inbound>')

    def read_parent(_revision: str) -> str:
        now[0] += parent_seconds
        return changed if fault == "parent" else parent

    def read_operations(_revision: str) -> dict[str, tuple[str, str, str]]:
        now[0] += operation_seconds * len(contracts)
        return {name: (*contract, changed if fault == "operation" and name == "models"
                       else policies[name]) for name, contract in contracts.items()}

    def probe(request: httpx.Request) -> httpx.Response:
        fields = {"abi": "2", "target": scope.target_id, "api": scope.api_id, "revision": "1",
                  "nonce": "0" * 32 if fault == "nonce"
                  else request.headers["x-turnstile-route-probe"],
                  "gateway": "managed", "instance": "managed",
                  "program": "f" * 64 if fault == "program" else digest,
                  "trust": identity.trust_binding_sha256}
        return httpx.Response(204, headers={"x-turnstile-route-" + name: value
                                            for name, value in fields.items()})

    management = MagicMock()
    management.current_revision.return_value = "2" if fault == "revision" else "1"
    management.revision_api_policy.side_effect = read_parent
    management.route_program_operations.side_effect = read_operations
    return FixedProgramVerifier(
        installed, management, SecretStr("unit-only"),
        httpx.Client(transport=httpx.MockTransport(probe)), clock=lambda: int(now[0]),
    )


def test_fixed_program_verifier_bounds_evidence_age_on_slow_management_reads() -> None:
    fast = _timed_program_verifier(5, 0.5).verify()
    assert (fast.issued_epoch, fast.expires_epoch) == (1009, 1129)
    slow = _timed_program_verifier(25, 1.5).verify()
    assert (slow.issued_epoch, slow.expires_epoch) == (1038, 1150)
    last = _timed_program_verifier(149, 0).verify()
    assert (last.issued_epoch, last.expires_epoch) == (1149, 1150)
    with pytest.raises(ValueError, match="time window after 150 of 150 seconds"):
        _timed_program_verifier(150, 0).verify()
    with pytest.raises(ValueError, match="exceeded its time window"):
        _timed_program_verifier(-1, 0).verify()
    for fault, message in {
        "nonce": "authenticated probe identity changed",
        "program": "authenticated probe identity changed",
        "parent": "parent changed",
        "operation": "operation changed",
        "revision": "revision changed",
    }.items():
        with pytest.raises(ValueError, match=message):
            _timed_program_verifier(25, 1.5, fault).verify()


@pytest.mark.parametrize("parent_seconds", [25, 150])
def test_program_proof_renewal_completes_or_reports_slow_management_reads(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, parent_seconds: int,
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    import turnstile_core.services.storage_route_publisher as publisher_module

    verifier = _timed_program_verifier(parent_seconds, 1.5)
    program = verifier.installation.program
    target_id = UUID(program.scope.target_id)
    repository, port = Mock(), Mock()
    repository.list_apim_targets.return_value = [{"gateway_profile_id": target_id}]
    repository.get_gateway_profiles.return_value = []
    repository.effective_gateway_publication.return_value = {
        "id": UUID(int=512), "gateway_profile_id": target_id, "status": "active",
        "resource_manifest": {"fixed_route_program": {}},
    }
    repository.model_availability_proof_guard.return_value = nullcontext()
    port._verifier = verifier
    port.projection.read_program_proof.return_value = None
    factory = publisher_module.FixedRoutePublisherFactory(
        repository, Mock(), active_fence=Mock(), availability_projector=Mock(),
    )
    monkeypatch.setattr(publisher_module, "_PROGRAM_RENEW_BACKOFF_UNTIL", {})
    monkeypatch.setattr(factory, "installed", Mock(return_value=(
        SimpleNamespace(program=program, native_gateway_scope=False), Mock(),
    )))
    monkeypatch.setattr(factory, "_open", Mock(return_value=nullcontext((port, Mock()))))
    if parent_seconds == 25:
        factory.renew_program_proofs()
        published = port.projection.publish_program_proof.call_args.args[0]
        assert (published.issued_epoch, published.expires_epoch) == (1038, 1150)
    else:
        with pytest.raises(ValueError, match="after 163 of 150 seconds"):
            factory.renew_program_proofs()
        port.projection.publish_program_proof.assert_not_called()
        assert ("reason=Fixed route program verification exceeded its time window after "
                "163 of 150 seconds") in caplog.text
        assert "elapsed_seconds=" in caplog.text and "unit-only" not in caplog.text


def test_fixed_program_operations_and_digest_preserve_exact_source_native_contract() -> None:
    contracts = fixed_operation_contracts("/v1/models", "/v1/messages", decision_enabled=True)
    assert contracts["microsoft-decision"] == ("POST", "/providers/microsoft/v1/systemone")
    assert contracts["anthropic-count-tokens"] == ("POST", "/v1/messages/count_tokens")
    assert "microsoft-decision" not in fixed_operation_contracts()
    with pytest.raises(ValueError, match="retired operation"):
        fixed_operation_contracts(retired_operations={"unit-old": ("POST", "/responses")})
    with pytest.raises(ValueError, match="catalog path"):
        fixed_operation_contracts("/other-models")
    parent = ('<policies><inbound><base/><set-variable name="routeProgram" value="'
              + "a" * 64 + '"/></inbound><backend><base/></backend>'
              '<outbound><base/></outbound><on-error><base/></on-error></policies>')
    operation = ("<policies><inbound><base/></inbound><backend><base/></backend>"
                 "<outbound><base/></outbound><on-error><base/></on-error></policies>")
    assert fixed_policy_digest(operation) == fixed_policy_digest(operation.replace(
        "<inbound>", "\n<inbound>\n",
    ))
    identity = fixed_program_digest(parent, {"models": operation}, (("models", "GET", "/models"),))
    assert identity == fixed_program_digest(parent.replace("a" * 64, "b" * 64),
                                            {"models": operation}, (("models", "GET", "/models"),))
    assert identity != fixed_program_digest(parent, {"models": operation},
                                            (("models", "GET", "/v1/models"),))


def test_route_preparation_checkpoints_keep_encrypted_intents_strict_and_bounded() -> None:
    checkpoint = _PreparationCheckpoint(context_sha256="a" * 64)
    assert "oauth" not in checkpoint.model_dump(mode="json")
    with pytest.raises(ValueError):
        _PreparationCheckpoint.model_validate({
            **checkpoint.model_dump(), "plaintext_secret": "unit",
        })
    with pytest.raises(ValueError):
        _CredentialBindings(bindings={"invalid-hash": "unit-version"})
    intent = _OAuthWriteIntent(resource_id="unit-resource", configuration_sha256="b" * 64,
                                body_sha256="c" * 64, body_ciphertext="unit-encrypted-only")
    assert "unit-encrypted-only" not in repr(intent)
    with pytest.raises(ValueError):
        _OAuthWriteIntent.model_validate_json(canonical_document({
            **intent.model_dump(mode="json"), "if_none_match": "unconditional",
        }))
    pool = _PoolWriteIntent(version=1, method="PUT", resource_id="unit-pool",
                            api_version="2024-05-01", if_none_match="*",
                            connection_sha256="a" * 64, body="{}", body_sha256="b" * 64)
    with pytest.raises(ValueError):
        _PoolWriteIntent.model_validate_json(canonical_document({
            **pool.model_dump(mode="json"), "version": True,
        }))
    vault = _VaultCheckpoint(publication_id=str(UUID(int=1)), source_sha256="a" * 64,
                              url="https://unitvault.vault.azure.net/secrets/unit",
                              issued_epoch=101, body_ciphertext="unit-encrypted-only",
                              body_sha256="b" * 64)
    assert "unit-encrypted-only" not in repr(vault)


def test_route_publication_checkpoints_are_strict_context_bound_and_bounded() -> None:
    progress = _RouteProgress(context_sha256="a" * 64, staged=3, verified=2)
    assert _RouteProgress.model_validate(progress.model_dump()) == progress
    with pytest.raises(ValueError):
        _RouteProgress(context_sha256="a" * 64, staged=True)
    with pytest.raises(ValueError):
        _RouteProgress(context_sha256="a" * 64, staged=30001)
    integrity = RouteIntegrityCheckpoint(context_sha256="a" * 64, entities=3)
    assert RouteIntegrityDeferred(integrity).checkpoint == integrity
    assert "connection_resources" not in integrity.model_dump(mode="json")
    with pytest.raises(ValueError):
        RouteIntegrityCheckpoint(context_sha256="a" * 64, connection_resources=121)
    activation = _ActivationCheckpoint(
        context_sha256="a" * 64, intent={"unit": "persisted"}, outcome="unknown_no_replay",
    )
    assert "scopes" not in activation.model_dump(mode="json")
    with pytest.raises(ValueError):
        _ActivationCheckpoint.model_validate({**activation.model_dump(), "invented_retry": True})
    with pytest.raises(ValueError, match="activation evidence is incomplete"):
        require_model_addition_activation({}, None, None)


def test_native_decision_budget_contract_never_injects_chat_output_parameters() -> None:
    repository = InMemoryRepository()
    repository.providers, repository.runtimes, repository.models = [], [], []
    service = GatewayControlPlaneService(repository, apim_principal_id=str(UUID(int=902)))
    publication = service.publish(foundry_publication(), "unit-owner")
    binding = publication.desired_spec.bindings[0].model_copy(update={
        "api_format": ApiFormat.MICROSOFT_DECISION,
    })
    contract = BudgetPublicationContracts._estimate(publication, binding, UUID(int=901), "a" * 64)
    assert contract.operations == ("microsoft-decision",)
    assert set(contract.protocols) == {"microsoft-decision"}
    assert contract.output_field == "none" and contract.native_counter is None
    assert contract.algorithm == "legacy_request_estimate_v1"
    assert contract.output_estimate == 4096 and contract.request_token_ceiling is None
    native_binding = binding.model_copy(update={
        "backend_url": HttpUrl("https://unit.services.ai.azure.com"),
        "backend_path": "/providers/microsoft/v1/systemone",
        "auth_strategy": AuthStrategy.MANAGED_IDENTITY,
        "managed_identity_resource": "https://cognitiveservices.azure.com",
    })
    profile, paths, credential = _binding_route_contract(native_binding)
    assert program_migration_spec_matches(publication.desired_spec, publication.desired_spec)
    resolved = publication.desired_spec.model_copy(update={"bindings": [binding.model_copy(update={
        "runtime_id": UUID(int=920), "api_format": publication.desired_spec.bindings[0].api_format,
    })]})
    assert program_migration_spec_matches(publication.desired_spec, resolved)
    changed_binding = resolved.bindings[0].model_copy(update={
        "model": resolved.bindings[0].model.model_copy(update={"model_key": "other-model"}),
    })
    changed_alias = resolved.model_copy(update={"bindings": [changed_binding]})
    assert not program_migration_spec_matches(publication.desired_spec, changed_alias)
    assert profile == "microsoft-decision"
    assert paths == {"microsoft-decision": "/providers/microsoft/v1/systemone"}
    assert credential.mode == "managed_identity"
    assert credential.audience == "https://cognitiveservices.azure.com"
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    connection = ConnectionRevision(
        scope=scope, revision_id=str(UUID(int=2)), backend_id="unit-backend",
        origin="https://unit.services.ai.azure.com", path=paths["microsoft-decision"],
        dependency_sha256="a" * 64, credential=credential,
    )
    assert pool_member_bindings(native_binding) == ()
    assert _pool_matches_binding(native_binding, connection, {})
    assert pool_header_name(scope, "unit-header") == pool_header_name(scope, "unit-header")
    assert pool_header_name(scope, "unit-header") != pool_header_name(
        scope.model_copy(update={"gateway_id": "unit-edge"}), "unit-header",
    )
    assert native_binding.count_tokens_operation.support == "unsupported"
    supported_count = native_binding.model_copy(update={
        "api_format": ApiFormat.ANTHROPIC_MESSAGES, "backend_path": "/anthropic/v1/messages",
    }).count_tokens_operation
    assert supported_count.support == "supported"
    assert supported_count.backend_path == "/anthropic/v1/messages/count_tokens"
    assert native_binding.model_copy(update={"api_format": ApiFormat.ANTHROPIC_MESSAGES,
                                              "backend_path": "/v1/messages"}) \
        .count_tokens_operation.support == "unknown"
    publication = publication.model_copy(update={
        "desired_spec": publication.desired_spec.model_copy(update={"bindings": [native_binding]}),
    })
    alias = native_binding.model.model_key
    scope = RouteScope(
        target_id=str(publication.gateway_profile_id), api_id="unit-api", gateway_id="managed",
    )
    approved_connection = ConnectionApproval(
        approval_id=str(UUID(int=903)), connection=connection.model_copy(update={"scope": scope}),
        state="approved", evidence_sha256="b" * 64, issued_epoch=100, expires_epoch=220,
    )
    budgets = BudgetPublicationContracts(
        publication_id=publication.id, gateway_profile_id=publication.gateway_profile_id,
        epoch=UUID(int=904), release_spec_sha256=publication.desired_spec_sha256,
        binding_sha256={alias: BudgetPublicationContracts.binding_digest(native_binding)},
        model_contracts={alias: contract},
    )
    program = ProgramIdentity(scope=scope, api_revision="1", instance_id="managed",
                               program_sha256="c" * 64, trust_binding_sha256="d" * 64)
    proof = ProgramCapabilityProof(proof_id=str(UUID(int=905)), identity=program,
                                   supported_profiles=("microsoft-decision",),
                                   issued_epoch=100, expires_epoch=220)
    task = build_route_publication_task(
        publication, program, proof, None, [], budgets, {alias: approved_connection},
        "https://unitstore.table.core.windows.net", "TurnstileRoutes", "e" * 64, 101,
    )
    assert task.plan.policy_write_count == 0
    assert task.plan.snapshot.routes[0].profile == "microsoft-decision"
    steps = configuration_probe_steps(task)
    assert len(steps) == 1 and steps[0].alias == alias and steps[0].kind == "inference"
    assert steps[0].operation is None and steps[0].member_backend_id is None
    assert steps[0].key == document_digest({"context": task.context_sha256, "alias": alias})
    assert configuration_probe_steps(task.model_copy(update={"configuration_only": True})) == ()
    unavailable = configuration_probe_steps(task.model_copy(update={
        "known_unavailable_bindings": {alias: "unit-known-unavailable"},
    }))
    assert len(unavailable) == 1 and unavailable[0].kind == "non-dispatch"
    assert unavailable[0].operation == "microsoft-decision" and unavailable[0].key != steps[0].key
    requests: list[str] = []
    recorded_requests: list[tuple[str, str]] = []
    journal = PersistentProbeJournal(
        repository, "unit-native-route", UUID(int=906),
        request_recorder=lambda method, endpoint: recorded_requests.append((method, endpoint)),
    )
    route = task.plan.snapshot.routes[0]
    native_probe_body = FixedRoutePublisherPort._probe_body(route, "microsoft-decision")
    assert native_probe_body == {
        "model": alias, "state": "OK",
        "questions": {"ok": {"type": "noul", "instructions": "Is the state OK?"}},
    }
    assert FixedRoutePublisherPort._selected_probe_operations(route, None, None) == (
        "microsoft-decision",
    )
    with pytest.raises(ValueError, match="not approved"):
        FixedRoutePublisherPort._selected_probe_operations(route, None, "chat-completions")
    endpoint = "https://unit-apim.example.test/llm/providers/microsoft/v1/systemone"
    probe_body = {"model": alias, "state": {"text": "unit-only"}, "questions": []}

    def send(request_id: str) -> dict[str, Any]:
        requests.append(request_id)
        journal.record_request("POST", endpoint)
        return {"total_tokens": 10, "correlation_id": "unit-native", "route_validated": True}

    evidence = journal.run_route(route, "microsoft-decision", endpoint, probe_body, send)
    assert journal.run_route(route, "microsoft-decision", endpoint, probe_body, send) == evidence
    assert journal.run_route(route, "microsoft-decision", endpoint, probe_body, send,
                             recovery_only=True) == evidence
    assert len(requests) == 1 and recorded_requests == [("POST", endpoint)]
    with pytest.raises(ValueError, match="approved operation"):
        journal.run_route(route, "microsoft-decision", endpoint, {"model": "other"}, send)
    unknown = PersistentProbeJournal(repository, "unit-native-unknown", UUID(int=907))

    def unconfirmed(request_id: str) -> dict[str, Any]:
        requests.append(request_id)
        raise TimeoutError("unit-only unknown response")

    with pytest.raises(TimeoutError):
        unknown.run_route(route, "microsoft-decision", endpoint, probe_body, unconfirmed)
    with pytest.raises(ValueError, match="no request replayed"):
        unknown.run_route(route, "microsoft-decision", endpoint, probe_body, unconfirmed,
                          recovery_only=True)
    assert len(requests) == 2
    checkpoint = _CommitApprovalCheckpoint(
        context_sha256=task.context_sha256, approval=task.plan.snapshot.approval,
    )
    assert "scopes" not in checkpoint.model_dump(mode="json")
    assert checkpoint.approval == task.plan.snapshot.approval
    require_route_specification(publication, task)
    head = ActiveRouteHead(approval=task.plan.snapshot.approval, activated_epoch=101)
    recorded = publication.model_copy(update={"resource_manifest": {
        "fixed_route_snapshot": task.plan.snapshot.model_dump(mode="json"),
        "fixed_route_head": head.model_dump(mode="json"),
    }})
    assert recorded_route_snapshot(recorded) == (task.plan.snapshot, head)
    assert recorded_route_scopes(recorded) == (scope,)
    with pytest.raises(ValueError, match="program installation is unavailable"):
        recorded_route_dependencies(recorded)
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256=task.target_identity_sha256,
        storage_endpoint=task.storage_endpoint, storage_table=task.storage_table,
        gateway_endpoint="https://unit-apim.example.test/llm",
        publisher_subscription_id="unit-publisher", parent_policy_sha256="f" * 64,
        operation_policies_sha256={"microsoft-decision": "1" * 64},
        operation_contracts={"microsoft-decision": ("POST", paths["microsoft-decision"])},
        supported_profiles=("microsoft-decision",), decision_enabled=True,
    )
    migration = FixedProgramMigration(
        request_id=UUID(int=921), previous_publication_id=publication.id, previous_revision="old",
        previous_spec_sha256=publication.desired_spec_sha256, previous_policy_sha256="a" * 64,
        installation=installation,
    )
    assert "known_unavailable_bindings" not in migration.model_dump(mode="json")
    with pytest.raises(ValueError, match="previous publication changed"):
        migration.require_previous(publication)
    recorded = recorded.model_copy(update={
        "apim_revision": program.api_revision, "policy_sha256": program.program_sha256,
        "resource_manifest": {**recorded.resource_manifest,
                              "fixed_route_program": installation.model_dump(mode="json")},
    })
    dependencies = recorded_route_dependencies(recorded)
    projection = MagicMock(spec=TableRouteProjection)
    verifier = MagicMock(spec=FixedProgramVerifier)
    verifier.installation = installation
    projection.read_program_proof.return_value = (proof, 'W/"unit"')
    fence = {"protocol_version": 2, "mode": "active", "gateway_profile_id": scope.target_id,
             "epoch": task.plan.snapshot.approval.budget_epoch}
    verified_connections = MagicMock()
    with httpx.Client() as client:
        concrete = FixedRoutePublisherPort(
            projection, verifier, SecretStr("unit-only"), client, active_fence=lambda: fence,
            verify_connections=verified_connections, prepare_availability=lambda *_: True,
            clock=lambda: 101,
        )
        assert concrete.check(task) == proof
        verified_connections.assert_called_once_with(task, fence)
        verifier.verify.assert_not_called()
        projection.publish_program_proof.assert_not_called()
        projection.read_model_active.return_value = ("unit-active-head", 'W/"unit"')
        with pytest.raises(RuntimeError, match="still active"):
            concrete.require_model_absent(alias)
        verifier.verify.assert_not_called()
        projection.read_model_active.return_value = None
        absence_requests: list[dict[str, Any]] = []

        def absence_dispatch(request: httpx.Request) -> httpx.Response:
            absence_requests.append(json.loads(request.content))
            return httpx.Response(503, json={"error": {"code": "route_configuration_unavailable"}})

        with httpx.Client(transport=httpx.MockTransport(absence_dispatch)) as absence_client:
            concrete._client = absence_client
            concrete.require_model_absent(alias)
            assert absence_requests == [{"model": alias, "input": False}]
        verifier.verify.reset_mock()
        native_dispatches: list[dict[str, Any]] = []
        wrong_response = False

        def candidate_dispatch(request: httpx.Request) -> httpx.Response:
            candidate = projection.stage_candidate.call_args.args[0]
            native_dispatches.append(json.loads(request.content))
            assert native_dispatches[-1] == native_probe_body
            assert request.headers["x-turnstile-route-candidate"] == candidate.probe_id
            return httpx.Response(200, json={
                "answers": {"ok": True}, "usage": {"input_tokens": 10, "output_tokens": 3},
            }, headers={
                "x-correlation-id": str(UUID(int=917)),
                "x-turnstile-route-version": candidate.head.approval.route_version,
                "x-turnstile-route-candidate": candidate.probe_id,
                "x-turnstile-route-request": "wrong" if wrong_response else candidate.request_id,
            })

        with httpx.Client(transport=httpx.MockTransport(candidate_dispatch)) as dispatch_client:
            concrete._client = dispatch_client
            evidence = concrete._send_probe(task, route, "microsoft-decision", proof, endpoint,
                                            native_probe_body, journal, str(UUID(int=916)))
            assert evidence["total_tokens"] == 13 and evidence["decision_validated"] is True
            assert evidence["route_validated"] is True
            wrong_response = True
            with pytest.raises(PolicyCompilationError, match="candidate request") as failed:
                concrete._send_probe(task, route, "microsoft-decision", proof, endpoint,
                                      native_probe_body, journal, str(UUID(int=918)))
            outcome = failed.value.billable_outcome
            assert outcome is not None
            assert outcome.actual_tokens == 13 and outcome.evidence["route_validated"] is False
            assert len(native_dispatches) == 2
            wrong_response = False
            controller_journal = PersistentProbeJournal(
                repository, "unit-native-controller", UUID(int=919),
            )
            concrete.probe_changed(task, alias, controller_journal)
            concrete.recover_changed(task, alias, controller_journal)
            concrete.probe_changed(task, alias, controller_journal)
            assert len(native_dispatches) == 3
            with pytest.raises(ValueError, match="cannot be dispatched"):
                concrete.probe_changed(task.model_copy(update={
                    "known_unavailable_bindings": {alias: "unit-known-unavailable"},
                }), alias, controller_journal)
            with pytest.raises(ValueError, match="approved changed route"):
                concrete.probe_count(task, alias, controller_journal)
            assert len(native_dispatches) == 3
        fence["epoch"] = str(UUID(int=909))
        with pytest.raises(ValueError, match="budget fence changed"):
            concrete.check(task)
        fence["epoch"] = task.plan.snapshot.approval.budget_epoch
        projection.read_program_proof.return_value = None
        with pytest.raises(RuntimeError, match="independent renewal"):
            concrete.check(task.model_copy(update={"model_addition": True}))
        verifier.verify.assert_not_called()
    assert dependencies.route_configuration_sha256 == task.plan.snapshot.configuration_sha256
    assert dependencies.route_head_sha256 == document_digest(head)
    assert dependencies.backends == [approved_connection.connection.backend_id]
    assert dependencies.recorded_complete and dependencies.live_status == "not_checked"
    legacy = dependencies.model_copy(update={
        "route_configuration_sha256": None, "route_head_sha256": None,
    }).model_dump(mode="json")
    assert "route_configuration_sha256" not in legacy and "route_head_sha256" not in legacy
    with pytest.raises(ValueError, match="identity changed"):
        recorded_route_dependencies(recorded.model_copy(update={"policy_sha256": "2" * 64}))
    with pytest.raises(ValueError, match="not confirmed"):
        recorded_route_snapshot(publication.model_copy(update={"resource_manifest": {
            "route_configuration": task.model_dump(mode="json"),
            "route_activation": {"context_sha256": task.context_sha256,
                                 "intent": {}, "outcome": "unknown_no_replay"},
        }}))
    with pytest.raises(ValueError, match="incomplete approved dependencies"):
        build_route_publication_task(publication, program, proof, None, [], budgets, {},
                                      task.storage_endpoint, task.storage_table, "e" * 64, 101)
    port = MagicMock()
    port.check.return_value = proof
    executor = RouteConfigurationExecutor(repository, lambda current: nullcontext(port),
                                           clock=lambda: 101)
    publication_row = repository.get_gateway_publication(publication.id)
    assert publication_row is not None
    publication_row.update(status="promoting", resource_manifest={})
    assert repository.claim_gateway_publication("unit-worker", 180) is not None
    promoting = publication.model_copy(update={"status": PublicationStatus.PROMOTING,
                                               "resource_manifest": {}})
    approved_snapshot = executor._commit_snapshot(
        promoting, task, port, "unit-worker", lambda: None,
    )
    approved_snapshot.approval.require_same_configuration(task.plan.snapshot.approval)
    assert approved_snapshot.approval.issued_epoch == 101
    assert approved_snapshot.approval.activate_before_epoch == 701
    resumed = promoting.model_copy(update={
        "resource_manifest": publication_row["resource_manifest"],
    })
    assert executor._commit_snapshot(
        resumed, task, port, "unit-worker", lambda: None,
    ) == approved_snapshot
    port.check.assert_called_once_with(task)

    def activate_model(
        addition: ModelRouteAddition, current_proof: ProgramCapabilityProof, epoch: str,
        operation_id: str, clock: Any, activation_journal: PersistentModelRouteActivationJournal,
    ) -> None:
        model_head = addition.active_head(current_proof, epoch, clock())
        receipt = RouteTransitionReceipt(
            operation_id=operation_id, scope=scope, before_etag=None, before_generation=0,
            after_sha256=document_digest(model_head),
            route_version=model_head.head.approval.route_version,
            approval_id=model_head.head.approval.approval_id,
        )
        content_type, body = conditional_batch(task.storage_endpoint, task.storage_table,
                                               scope.partition, [
            (model_head.row_key, route_entity(scope, scope.partition, model_head.row_key,
                                              canonical_document(model_head)), None),
            ("model-transition:" + operation_id,
             route_entity(scope, scope.partition, "model-transition:" + operation_id,
                          canonical_document(receipt)), None),
        ])
        intent = ModelRouteActivationIntent(receipt, model_head, content_type, body,
                                            task.storage_endpoint, task.storage_table)
        activation_journal.begin_once(intent)
        assert publication_row["resource_manifest"]["route_activation"]["outcome"] == "started"
        activation_journal.record_outcome(operation_id, "confirmed")

    port.projection.activate_model_addition.side_effect = activate_model
    model_head = executor._activate_model_scope(resumed, task, port, "unit-worker", lambda: None)
    assert model_head.alias == alias and model_head.publication_id == str(publication.id)
    latest = promoting.model_copy(update={
        "resource_manifest": publication_row["resource_manifest"],
    })
    port.projection.recover_model_addition.return_value = "confirmed"
    assert executor._activate_model_scope(
        latest, task, port, "unit-worker", lambda: None,
    ) == model_head
    assert port.projection.activate_model_addition.call_count == 1
    with (
        patch.object(executor, "_activate_model_scope", return_value=model_head) as activate_scope,
        patch.object(repository, "activate_gateway_publication", return_value=promoting.model_copy(
            update={"status": PublicationStatus.ACTIVE},
        ).model_dump(mode="python")) as activate_database,
    ):
        assert executor._activate_model(
            promoting, task, port, "unit-worker", lambda: None,
        ).status is PublicationStatus.ACTIVE
    assert activate_scope.call_count == 1
    activate_database.assert_called_once_with(
        publication.id, "unit-worker", validate_model_activation=require_model_addition_activation,
    )
    route_head = ActiveRouteHead(approval=approved_snapshot.approval, activated_epoch=101)
    transition = RouteTransitionReceipt(
        operation_id=str(UUID(int=915)), scope=scope, before_etag=None, before_generation=0,
        after_sha256=document_digest(route_head), route_version=route_head.approval.route_version,
        approval_id=route_head.approval.approval_id,
    )
    content_type, body = conditional_batch(
        task.storage_endpoint, task.storage_table, scope.partition, [
        ("active", route_entity(scope, scope.partition, "active",
                                 canonical_document(route_head)), None),
        ("transition:" + transition.operation_id,
         route_entity(scope, scope.partition, "transition:" + transition.operation_id,
                      canonical_document(transition)), None),
    ])
    intent = RouteActivationIntent(transition, route_head, content_type, body,
                                   text_digest(body.decode()), task.storage_endpoint,
                                   task.storage_table)
    publication_row["resource_manifest"] = {
        "route_commit_approval": _CommitApprovalCheckpoint(
            context_sha256=task.context_sha256, approval=approved_snapshot.approval,
        ).model_dump(mode="json"),
        "route_activation": {"version": 1, "context_sha256": task.context_sha256,
                             "intent": intent.to_record(), "outcome": "unknown_no_replay"},
    }
    ordinary = promoting.model_copy(update={
        "resource_manifest": publication_row["resource_manifest"],
    })
    port.projection.recover_activation.return_value = "unknown"
    with pytest.raises(RuntimeError, match="writer requires receipt recovery"):
        executor._activate(ordinary, task, port, "unit-worker", lambda: None)
    port.projection.activate.assert_not_called()
    port.projection.takeover_writer.assert_not_called()
    publication_row.update({
        **publication.model_dump(mode="python"), "status": "queued",
        "resource_manifest": {"route_configuration": task.model_dump(mode="json")},
    })
    lease = next(item for item in repository.gateway_publication_outbox
                 if item["publication_id"] == publication.id)
    lease.update(status="queued", lease_owner=None, lease_expires_at=None)
    port.program_record = installation.model_dump(mode="json")
    port.projection.stage_slice.return_value = 3
    port.projection.verify_slice_recorded.return_value = 3
    for expected_phase in (
        "validating", "provisioning", "building_revision", "verifying", "promoting",
    ):
        leased = repository.claim_gateway_publication("unit-worker", 180)
        assert leased is not None
        current = executor(GatewayPublication.model_validate(leased), "unit-worker", lambda: None)
        assert current.status.value == expected_phase
    assert _RouteProgress.model_validate(current.resource_manifest["route_progress"]).verified == 3
    verified_probes = current.resource_manifest["probe_verification"]
    assert isinstance(verified_probes, dict)
    assert verified_probes["steps"] == {steps[0].key: "passed"}
    assert port.projection.stage_slice.call_count == 1
    assert port.projection.verify_slice_recorded.call_count == 1
    assert port.probe_changed.call_count == 1
    previous = publication.model_copy(update={
        "id": UUID(int=908), "status": PublicationStatus.ACTIVE,
        "desired_spec": publication.desired_spec.model_copy(update={
            "bindings": [native_binding.model_copy(update={
                "model": native_binding.model.model_copy(update={"model_key": "unit-existing"}),
            })],
            "discovery_models": [publication.desired_spec.discovery_models[0].model_copy(
                update={"id": "unit-existing"},
            )],
        }),
        "resource_manifest": {"fixed_route_program": installation.model_dump(mode="json")},
    })
    configuration = ApimTargetClientConfig(
        publication.gateway_profile_id, "f" * 64, True,
        Settings.model_construct(apim_api_id=scope.api_id,
                                 apim_gateway_url=installation.gateway_endpoint,
                                 apim_probe_subscription_id=installation.publisher_subscription_id,
                                 apim_probe_subscription_key=SecretStr("unit-only")),
        route_target_identity_sha256=installation.target_identity_sha256,
    )
    configurations = MagicMock()
    configurations.resolve.return_value = configuration
    factory = FixedRoutePublisherFactory(repository, configurations, active_fence=MagicMock(),
                                         availability_projector=MagicMock(), clock=lambda: 101)
    from datetime import UTC, datetime

    from turnstile_core.domain.model_availability import (
        ModelAvailabilityCatalogCheck,
        ModelAvailabilityConfirmationPlan,
        ModelAvailabilityCoverage,
        ModelAvailabilityStagedProgram,
    )

    staged_program = ModelAvailabilityStagedProgram(expected_current_revision="1",
                                                    installation=installation)
    assert staged_program.installation == installation
    with pytest.raises(ValueError):
        ModelAvailabilityStagedProgram(expected_current_revision="*", installation=installation)
    staged_surfaces = tuple(sorted([
        "managed/0/models",
        *("managed/" + installation.program.api_revision + "/" + name
          for name in installation.operation_contracts),
    ]))
    staged_coverage = ModelAvailabilityCoverage(
        target_id=UUID(installation.program.scope.target_id), inventory_digest="a" * 64,
        policy_digest="b" * 64, serving_surfaces=staged_surfaces,
        verified_surfaces=staged_surfaces, verified_at=datetime.fromtimestamp(100, UTC),
        expires_at=datetime.fromtimestamp(160, UTC),
    )
    staged_plan = ModelAvailabilityConfirmationPlan(
        resource_id="/unit/resource", api_id=installation.program.scope.api_id,
        gateway_url=HttpUrl(installation.gateway_endpoint),
        table_endpoint=HttpUrl(installation.storage_endpoint),
        table_name=installation.storage_table,
        coverage=staged_coverage, enrollments=(), catalogs=(ModelAvailabilityCatalogCheck(
            endpoint_url=HttpUrl(installation.gateway_endpoint), revision="0", route="/models",
            gateway_id="managed", instance_id="managed", models={}, capability_digest="c" * 64,
            admission_surfaces={"/v1/chat/completions": "d" * 64},
        ),),
        staged_programs=(staged_program.model_copy(update={"expected_current_revision": "0"}),),
    )
    assert ModelAvailabilityConfirmationPlan.from_entity(staged_plan.entity()) == staged_plan
    with pytest.raises(ValueError, match="complete distinct approved coverage"):
        ModelAvailabilityConfirmationPlan.model_validate({
            **staged_plan.model_dump(), "staged_programs": (staged_program,),
        })
    legacy_coverage = staged_coverage.model_copy(update={
        "serving_surfaces": ("managed/0/models",), "verified_surfaces": ("managed/0/models",),
    })
    legacy_plan = ModelAvailabilityConfirmationPlan.model_validate({
        **staged_plan.model_dump(), "staged_programs": (), "coverage": legacy_coverage,
    })
    assert "staged_programs" not in legacy_plan.model_dump(mode="json")
    with pytest.raises(ValueError, match="installed base release"), factory.resolve(task):
        pytest.fail("a task without its base cannot open a route port")
    with pytest.raises(ValueError, match="migration publication is unavailable"):
        factory.promote_program(publication, task, "unit-worker", lambda: None)
    with pytest.raises(ValueError, match="installed base release"):
        factory.prepare(publication, "unit-worker", lambda: None)
    with pytest.raises(ValueError, match="installed program"):
        factory._prepare_model_addition(publication, "unit-worker", lambda: None)
    with pytest.raises(ValueError, match="removal intent is invalid"):
        factory.remove_model(publication, "unit-worker", lambda: None)
    with pytest.raises(ValueError, match="scope removal intent is unavailable"):
        factory._remove_model_scopes(publication, previous, previous.model_dump(mode="python"),
            configuration, (installation,), "unit-worker", lambda: None)
    managed_binding = native_binding.model_copy(update={"runtime_id": UUID(int=923)})
    metadata_tokens = MagicMock()
    metadata_tokens.token.return_value = "unit-only"
    metadata_factory = FixedRoutePublisherFactory(repository, configurations,
        active_fence=MagicMock(), availability_projector=MagicMock(), clock=lambda: 101)
    metadata_factory._tokens = metadata_tokens
    assert managed_binding.backend_url is not None
    host = managed_binding.backend_url.host
    assert host is not None
    resource = host.removesuffix(".openai.azure.com").removesuffix(
        ".services.ai.azure.com",
    )
    metadata_binding = managed_binding.model_copy(update={"runtime_config": {
        **managed_binding.runtime_config,
        "project_endpoint": "https://" + resource + ".services.ai.azure.com/api/projects/unit",
    }})
    metadata_publication = publication.model_copy(update={"desired_spec":
        publication.desired_spec.model_copy(update={"bindings": [metadata_binding]}),
    })
    metadata_requests: list[httpx.Request] = []

    def deployment_metadata(request: httpx.Request) -> httpx.Response:
        metadata_requests.append(request)
        assert request.method == "GET" and request.url.path == "/api/projects/unit/deployments"
        assert request.url.params.get("api-version") == "v1"
        return httpx.Response(200, json={"value": [
            {"name": metadata_binding.model.upstream_model_id},
        ]})

    with httpx.Client(transport=httpx.MockTransport(deployment_metadata)) as metadata_client:
        without_project = metadata_publication.model_copy(update={"desired_spec":
            metadata_publication.desired_spec.model_copy(update={"bindings": [
                metadata_binding.model_copy(update={"runtime_config": {
                    key: value for key, value in metadata_binding.runtime_config.items()
                    if key != "project_endpoint"
                }}),
            ]}),
        })
        metadata_factory._validate_deployment(
            without_project, MagicMock(), metadata_client, installation,
        )
        assert metadata_requests == []
        metadata_factory._validate_deployment(
            metadata_publication, MagicMock(), metadata_client, installation,
        )
    assert len(metadata_requests) == 1
    metadata_tokens.token.assert_called_once_with("https://ai.azure.com")
    new_connection = factory._new_connection(managed_binding, installation, configuration)
    assert new_connection == factory._new_connection(managed_binding, installation, configuration)
    assert new_connection.path == "/providers/microsoft/v1/systemone"
    assert not new_connection.observer
    assert new_connection.credential == credential and new_connection.public_count_path is None
    connection_management = MagicMock()

    def prepare_connection(connection: ConnectionRevision, persist: Any,
                           *, recovery_only: bool = False) -> str:
        assert recovery_only is False
        persist({"unit": "immutable-intent"})
        return connection.dependency_sha256

    connection_management.prepare_route_connection.side_effect = prepare_connection
    with patch.object(repository, "checkpoint_gateway_publication_preparation",
                      return_value=True) as save_connection:
        prepare = factory._connection_preparer(publication, installation, configuration,
                                               connection_management, "unit-worker", lambda: None)
        assert prepare(managed_binding) == (new_connection, 101)
    assert save_connection.call_count == 1
    connection_management.prepare_route_connection.side_effect = None
    connection_management.prepare_route_connection.return_value = new_connection.dependency_sha256
    connection_management.prepare_route_connection.reset_mock()
    restored_preparation = publication.model_copy(update={"resource_manifest": {
        "route_preparation": save_connection.call_args.args[-1],
    }})
    recovered_prepare = factory._connection_preparer(
        restored_preparation, installation, configuration,
        connection_management, "unit-worker", lambda: None,
    )
    assert recovered_prepare(managed_binding) == (new_connection, 101)
    assert connection_management.prepare_route_connection.call_args.kwargs["recovery_only"] is True
    approved_runtime: dict[str, Any] = {
        "id": native_binding.runtime_id, "enabled": True,
        "gateway_profile_id": publication.gateway_profile_id,
        "config": {"storage_route_approvals": [approved_connection.model_dump(mode="json")]},
    }
    connection_management.verify_route_connection.return_value = (
        approved_connection.connection.dependency_sha256
    )
    assert factory.approved_connections(
        publication, installation, connection_management, registry={"runtimes": [approved_runtime]},
    ) == {alias: approved_connection}
    connection_management.verify_route_connection.assert_called_once_with(
        approved_connection.connection,
    )
    approved_runtime["config"]["storage_route_approvals"].append(
        approved_connection.model_dump(mode="json"),
    )
    with pytest.raises(ValueError, match="one current independent"):
        factory.approved_connections(publication, installation, connection_management,
                                     registry={"runtimes": [approved_runtime]})
    with pytest.raises(ValueError, match="preparation is unsupported"):
        factory._new_connection(native_binding, installation, configuration)
    with pytest.raises(ValueError, match="Pool capability is unavailable"):
        factory._new_pool_connection(managed_binding, installation, configuration, {})
    assert managed_binding.backend_url is not None
    pool = GatewayBackendPoolConfig(members=[
        GatewayBackendPoolMember(runtime_id=UUID(int=923), runtime_name="unit-primary",
                                 backend_url=managed_binding.backend_url,
                                 auth_strategy=AuthStrategy.MANAGED_IDENTITY, priority=0, weight=3),
        GatewayBackendPoolMember(runtime_id=UUID(int=924), runtime_name="unit-secondary",
                                 backend_url=HttpUrl("https://unit-secondary.services.ai.azure.com"),
                                 auth_strategy=AuthStrategy.MANAGED_IDENTITY, priority=1, weight=1),
    ], rate_limit=GatewayRateLimitResilience(
        max_attempts_per_request=2, retry_interval_seconds=1, first_fast_retry=True,
        circuit_breaker=GatewayRateLimitCircuitBreaker(
            failure_count=1, interval_seconds=60, trip_duration_seconds=60, accept_retry_after=True,
        ),
    ),
        session_affinity=True)
    pool_binding = managed_binding.model_copy(update={"runtime_config": {
        **managed_binding.runtime_config, "apim_backend_pool": pool.model_dump(mode="json"),
    }})
    pool_installation = installation.model_copy(update={"pool_enabled": True})
    pool_config = ApimTargetClientConfig(configuration.gateway_profile_id, "f" * 64, True,
        configuration.settings.model_copy(update={"azure_subscription_id": str(UUID(int=1)),
            "apim_resource_group": "unit-group", "apim_service_name": "unit-apim"}),
        route_target_identity_sha256=installation.target_identity_sha256)
    pooled_connection, members = factory._new_pool_connection(
        pool_binding, pool_installation, pool_config, {},
    )
    assert (pooled_connection, members) == factory._new_pool_connection(
        pool_binding, pool_installation, pool_config, {},
    )
    assert pooled_connection.pool is not None
    assert pooled_connection.pool.session_cookie_name is not None
    assert [(member.priority, member.weight) for member in pooled_connection.pool.members] == [
        (0, 3), (1, 1),
    ]
    assert len(members) == 2 and all(member.circuit_breaker is not None for member in members)
    with factory._open(installation, pool_config) as (opened_port, opened_management):
        assert opened_port.program_record == installation.model_dump(mode="json")
        assert opened_port.projection._table == installation.storage_table
        assert isinstance(opened_management, AzureApimPublisherClient)
        staged_store = MagicMock()
        staged_store.read_versioned_entity.return_value = None
        assert opened_management._staged_availability_programs(staged_store,
            staged_coverage.target_id, installation.storage_endpoint,
            installation.storage_table) == ()
        reader_plan = staged_plan.model_copy(update={
            "resource_id": opened_management._resource_id_base,
        })
        staged_store.read_versioned_entity.return_value = (reader_plan.entity(), '"unit"')
        assert opened_management._staged_availability_programs(staged_store,
            staged_coverage.target_id, installation.storage_endpoint,
            installation.storage_table) == reader_plan.staged_programs
        staged_store.replace_if_match.assert_not_called()
        staged_store.read_versioned_entity.return_value = (staged_plan.entity(), '"unit"')
        with pytest.raises(RuntimeError, match="another target"):
            opened_management._staged_availability_programs(staged_store,
                staged_coverage.target_id, installation.storage_endpoint,
                installation.storage_table)
        wrong_probe = staged_program.model_copy(update={"installation": installation.model_copy(
            update={"publisher_subscription_id": "unit-other"},
        )})
        with pytest.raises(RuntimeError, match="probe identity changed"):
            opened_management._verify_staged_availability_program(wrong_probe)
    availability_repository = MagicMock()
    availability_repository.get_model_availability_state.return_value = None
    availability_factory = FixedRoutePublisherFactory(availability_repository, configurations,
        active_fence=MagicMock(), availability_projector=MagicMock(), clock=lambda: 101)
    with pytest.raises(ValueError, match="availability changed"):
        availability_factory._prepare_availability(task, route)
    availability_repository.stage_model_availability.assert_called_once_with(
        UUID(route.model_id), publication.id,
    )
    assert factory._dependency_context(publication, installation) == document_digest({
        "publication_id": str(publication.id),
        "desired_spec_sha256": publication.desired_spec_sha256,
        "specification": publication.desired_spec.model_dump(mode="json"),
        "installation": installation.model_dump(mode="json"),
    })
    assert factory._credential_bindings(publication) == {}
    assert factory._rollback_source(publication, previous, installation, task.plan.snapshot) == (
        previous, task.plan.snapshot,
    )
    assert factory._connection_source(publication, installation, previous) == (previous, None)
    scope_context = MagicMock()
    scope_context.__enter__.return_value = (port, MagicMock())
    port.projection.read_model_active.return_value = None
    port.projection.read_program_proof.return_value = None
    with (
        patch.object(factory, "_preflight_dependencies"),
        patch.object(factory, "_open", return_value=scope_context),
        patch.object(factory, "_prepare_credentials") as prepare_scope_credentials,
        pytest.raises(RuntimeError, match="proof is not current"),
    ):
        factory._prepare_model_scope(publication, previous, (installation,), configuration,
            {}, "unit-worker", lambda: None, credentials=None, scope_only=False)
    prepare_scope_credentials.assert_not_called()
    with patch.object(repository, "get_gateway_publication", return_value=None), \
            pytest.raises(ValueError, match="publication is unavailable"):
        factory._verify_connections(installation, MagicMock(), task)
    with pytest.raises(ValueError, match="rollback source binding is invalid"):
        factory._rollback_source(publication.model_copy(update={"resource_manifest": {
            "route_rollback": {"operation_id": "unit-invalid"},
        }}), previous, installation, task.plan.snapshot)
    prepared_without_keys, key_bindings = factory._prepare_credentials(
        publication, previous, installation, MagicMock(), "unit-worker", lambda: None,
    )
    assert prepared_without_keys == publication and key_bindings == {}
    assert factory._prepare_oauth(publication, previous, installation, MagicMock(),
                                  "unit-worker", lambda: None) == publication
    oauth = OAuthClientCredentialsConfig(provider_id="turnstile-oauth-" + UUID(int=926).hex,
                                         client_id=UUID(int=927),
                                         token_url=HttpUrl("https://adb-unit.1.azuredatabricks.net/oidc/v1/token"))
    oauth_binding = native_binding.model_copy(update={
        "auth_strategy": AuthStrategy.OAUTH_CLIENT_CREDENTIALS,
        "runtime_config": {**native_binding.runtime_config, "oauth": oauth.model_dump(mode="json")},
        "managed_identity_resource": None,
    })
    oauth_publication = publication.model_copy(update={
        "desired_spec": publication.desired_spec.model_copy(update={"bindings": [oauth_binding]}),
    })
    oauth_cipher = CredentialCipher(Fernet.generate_key())
    oauth_factory = FixedRoutePublisherFactory(repository, configurations, active_fence=MagicMock(),
        availability_projector=MagicMock(), clock=lambda: 101, cipher=oauth_cipher)
    oauth_management = MagicMock()

    def prepare_oauth(resource: Any, persist: Any) -> None:
        assert resource.config == oauth and resource.client_secret is None
        body = canonical_document({"unit": "opaque-client-secret"})
        persist({"version": 1, "method": "PUT", "api_version": "2024-05-01",
                 "resource_id": "/unit/authorizationProviders/" + oauth.provider_id,
                 "if_none_match": "*", "configuration_sha256": text_digest(oauth.model_dump_json()),
                 "body": body, "body_sha256": text_digest(body)})

    oauth_management.prepare_route_oauth.side_effect = prepare_oauth
    with (
        patch.object(oauth_factory, "_preflight_dependencies"),
        patch.object(repository, "gateway_publication_credential", return_value=None),
        patch.object(repository, "checkpoint_gateway_publication_preparation",
                 return_value=True) as save_oauth,
    ):
        prepared_oauth = oauth_factory._prepare_oauth(
            oauth_publication, previous, installation, oauth_management,
            "unit-worker", lambda: None,
        )
    assert save_oauth.call_count == 2
    assert "opaque-client-secret" not in canonical_document(prepared_oauth.resource_manifest)
    with (
        patch.object(oauth_factory, "_preflight_dependencies"),
        patch.object(repository, "gateway_publication_credential", return_value=None),
        patch.object(repository, "checkpoint_gateway_publication_preparation", return_value=False),
        pytest.raises(RuntimeError, match="preparation lease changed"),
    ):
        oauth_factory._prepare_oauth(oauth_publication, previous, installation,
                                     oauth_management, "unit-worker", lambda: None)
    assert factory._credential_references(publication, previous, installation) == {}
    secret_version = "https://unitvault.vault.azure.net/secrets/unit/" + "b" * 32
    with pytest.raises(ValueError, match="Vault is not approved"):
        factory._require_credential_vault(secret_version, installation)
    approved_vault = installation.model_copy(update={
        "trusted_vault_origins": ("https://unitvault.vault.azure.net",),
    })
    factory._require_credential_vault(secret_version, approved_vault)
    keyed_binding = native_binding.model_copy(update={
        "runtime_id": UUID(int=925), "auth_strategy": AuthStrategy.NAMED_VALUE_API_KEY,
        "named_value_name": "unit-key", "managed_identity_resource": None,
    })
    keyed = publication.model_copy(update={"desired_spec": publication.desired_spec.model_copy(
        update={"bindings": [keyed_binding]},
    )})
    source_hash = route_credential_source(keyed_binding)
    keyed_previous = previous.model_copy(update={
        "desired_spec": keyed.desired_spec, "resource_manifest": {"route_credentials": {
            "version": 1, "bindings": {source_hash: secret_version},
        }},
    })
    key_runtime = {"id": keyed_binding.runtime_id, "enabled": True,
                   "gateway_profile_id": publication.gateway_profile_id, "config": {
                       "control_plane_managed": True, "auth_strategy": "named_value_api_key",
                       "named_value_name": "unit-key",
                   }}
    management = MagicMock()
    with patch.object(repository, "checkpoint_gateway_publication_credentials",
                      return_value=True) as save:
        prepared_keys, inherited_keys = factory._prepare_credentials(
            keyed, keyed_previous, approved_vault, management, "unit-worker", lambda: None,
            registry={"runtimes": [key_runtime]},
        )
    assert inherited_keys == {source_hash: secret_version}
    assert factory._credential_bindings(prepared_keys) == inherited_keys
    save.assert_called_once_with(publication.id, "unit-worker", None, {
        "version": 1, "bindings": inherited_keys,
    })
    management.route_credential_value.assert_not_called()
    with (
        patch.object(repository, "checkpoint_gateway_publication_credentials", return_value=False),
        pytest.raises(RuntimeError, match="bindings lease changed"),
    ):
        factory._prepare_credentials(
            keyed, keyed_previous, approved_vault, management, "unit-worker", lambda: None,
            registry={"runtimes": [key_runtime]},
        )
    with pytest.raises(ValueError, match="preflight is unsupported"):
        factory._preflight_dependencies(publication, installation, {"runtimes": []})
    assert not factory._runtime_destination_matches(publication, native_binding, {})
    bound_credentials = publication.model_copy(update={"resource_manifest": {"route_credentials": {
        "version": 1, "bindings": {
            "a" * 64: "https://unitvault.vault.azure.net/secrets/unit/" + "b" * 32,
        },
    }}})
    assert factory._credential_bindings(bound_credentials) == {
        "a" * 64: "https://unitvault.vault.azure.net/secrets/unit/" + "b" * 32,
    }
    with pytest.raises(ValueError, match="bindings are invalid"):
        factory._credential_bindings(publication.model_copy(update={
            "resource_manifest": {"route_credentials": "unit-invalid"},
        }))
    with patch.object(repository, "effective_gateway_publication",
                      return_value=previous.model_dump(mode="python")):
        assert factory.installed(publication.gateway_profile_id, previous.id) == (
            installation, configuration,
        )
        configurations.resolve.assert_called_once_with(publication.gateway_profile_id,
                                                        include_probe_credentials=True)
        with pytest.raises(ValueError, match="effective publication changed"):
            factory.installed(publication.gateway_profile_id, UUID(int=922))
    marker = {"model_route_addition": {
        "version": 1, "program_release_id": str(previous.id), "alias": alias,
    }}
    desired_spec = publication.desired_spec.model_dump(mode="json")
    digest = document_digest({**marker, "desired_spec": desired_spec})
    assert _model_addition_manifest(
        previous.model_dump(mode="python"), desired_spec, digest, marker,
    ) == marker
    with pytest.raises(ValueError, match="cannot contain or replace"):
        _model_addition_manifest(
            previous.model_dump(mode="python"), desired_spec, "a" * 64, marker,
        )
    with pytest.raises(ValueError, match="cannot contain or replace"):
        _model_addition_manifest(publication.model_copy(update={
            "id": previous.id, "status": PublicationStatus.ACTIVE,
            "resource_manifest": previous.resource_manifest,
        }).model_dump(mode="python"), desired_spec, digest, marker)
    removal_source = publication.model_copy(update={
        "status": PublicationStatus.ACTIVE, "publication_kind": PublicationKind.MODEL_ADD,
        "resource_manifest": marker,
    })
    removal_spec = {**desired_spec, "bindings": [], "discovery_models": [], "removed_models": [{
        "model_id": route.model_id, "model_key": alias,
        "display_name": route.display_name, "api_format": native_binding.api_format.value,
    }]}
    removal_marker = {"model_route_removal": {
        "version": 1, "program_release_id": str(previous.id),
        "source_publication_id": str(removal_source.id),
        "source_spec_sha256": removal_source.desired_spec_sha256,
        "model_id": route.model_id, "alias": alias,
    }}
    removal_digest = document_digest({**removal_marker, "desired_spec": removal_spec})
    assert _model_removal_manifest(previous.model_dump(mode="python"),
        removal_source.model_dump(mode="python"), removal_spec,
        removal_digest, removal_marker) == removal_marker
    with pytest.raises(ValueError, match="source or target binding changed"):
        _model_removal_manifest(previous.model_dump(mode="python"),
            removal_source.model_dump(mode="python"), removal_spec, "a" * 64, removal_marker)


def test_fixed_route_program_promotion_once_never_replays_started_intent() -> None:
    from unittest.mock import call

    from turnstile_core.services.storage_route_publisher import promote_program_once

    management, persist, verify = MagicMock(), MagicMock(), MagicMock()
    expected = {"version": 1, "previous_revision": "1", "revision": "2",
                "release_name": "unit-release", "context_sha256": "a" * 64}
    started, confirmed = {**expected, "outcome": "started"}, {**expected, "outcome": "confirmed"}

    def promote(revision: str, release: str, previous: str) -> None:
        assert (revision, release, previous) == ("2", "unit-release", "1")
        persist.assert_called_once_with(None, started)

    management.promote_fixed_revision.side_effect = promote
    management.current_revision.side_effect = ["1", "2"]
    promote_program_once(management, "1", "2", "unit-release", "a" * 64, None, persist, verify)
    management.promote_fixed_revision.assert_called_once_with("2", "unit-release", "1")
    assert persist.call_args_list == [call(None, started), call(started, confirmed)]
    assert verify.call_args_list == [call("1"), call("2")]
    management.reset_mock()
    management.current_revision.side_effect = None
    management.current_revision.return_value = "1"
    persist.reset_mock()
    verify.reset_mock()
    with pytest.raises(RuntimeError, match="read back only"):
        promote_program_once(management, "1", "2", "unit-release", "a" * 64,
                             started, persist, verify)
    management.promote_fixed_revision.assert_not_called()
    persist.assert_not_called()
    verify.assert_not_called()
    management.current_revision.return_value = "2"
    promote_program_once(management, "1", "2", "unit-release", "a" * 64,
                         started, persist, verify)
    management.promote_fixed_revision.assert_not_called()
    persist.assert_called_once_with(started, confirmed)
    verify.assert_called_once_with("2")


@pytest.mark.parametrize("pool_enabled", [False, True])
@pytest.mark.parametrize("auth_strategy", [
    AuthStrategy.NAMED_VALUE_API_KEY, AuthStrategy.NAMED_VALUE_BEARER,
])
@pytest.mark.parametrize("published,fault", [
    *((True, fault) for fault in (
        None, "runtime-version", "missing-mapping", "mapped-version", "declared-conflict",
    )),
    (False, None), (False, "missing-mapping"), (False, "declared-conflict"),
])
def test_connection_renewal_uses_activated_materialized_credential(
    pool_enabled: bool, auth_strategy: AuthStrategy, published: bool, fault: str | None,
) -> None:
    from tests.backend.model_platform.test_storage_route_projection import snapshot_fixture
    from turnstile_core.persistence.repository_support import _activated_runtime_credentials
    from turnstile_core.services.route_publication import (
        pool_member_bindings,
        route_credential_source,
    )

    publication, _ = budget_publication_contract_fixture()
    snapshot, program, _ = snapshot_fixture()
    target = UUID(program.scope.target_id)
    binding = publication.desired_spec.bindings[0].model_copy(update={
        "runtime_id": UUID(int=923), "api_format": ApiFormat.OPENAI_CHAT,
        "auth_strategy": auth_strategy, "named_value_name": "unit-runtime-key",
        "key_vault_secret_id": None,
    })
    assert binding.backend_url is not None
    runtime_config = {
        **binding.runtime_config, "control_plane_managed": True,
        "backend_url": str(binding.backend_url), "backend_path": binding.backend_path,
        "api_format": binding.api_format.value, "auth_strategy": binding.auth_strategy.value,
        "managed_identity_resource": binding.managed_identity_resource,
    }
    if pool_enabled:
        pool = GatewayBackendPoolConfig(members=[GatewayBackendPoolMember(
            runtime_id=UUID(int=923 + index), runtime_name=f"unit-runtime-{index}",
            backend_url=binding.backend_url, auth_strategy=binding.auth_strategy,
            named_value_name=binding.named_value_name, priority=0, weight=1,
        ) for index in range(2)], rate_limit=GatewayRateLimitResilience(
            max_attempts_per_request=2, retry_interval_seconds=1, first_fast_retry=True,
            circuit_breaker=GatewayRateLimitCircuitBreaker(
                failure_count=1, interval_seconds=60, trip_duration_seconds=60,
                accept_retry_after=True,
            ),
        ))
        runtime_config["apim_backend_pool"] = pool.model_dump(mode="json")
    binding = binding.model_copy(update={"runtime_config": runtime_config})
    secret_id = "https://unit-vault.vault.azure.net/secrets/runtime-key/" + "a" * 32
    credentials = {
        route_credential_source(source): secret_id
        for source in pool_member_bindings(binding) or (binding,)
    }
    manifest = {"route_credentials": {"version": 1, "bindings": credentials}}
    activated = _activated_runtime_credentials(
        runtime_config, binding.model_dump(mode="json"), manifest,
    )
    assert activated["key_vault_secret_id"] == secret_id
    assert binding.key_vault_secret_id is None
    current = activated if published else dict(runtime_config)
    assert (current.get("key_vault_secret_id") is not None) is published
    changed_secret = secret_id[:-32] + "b" * 32
    if fault == "runtime-version":
        current["key_vault_secret_id"] = changed_secret
    elif fault == "missing-mapping":
        manifest = {"route_credentials": {"version": 1, "bindings": {}}}
    elif fault == "mapped-version":
        manifest = {"route_credentials": {"version": 1, "bindings": {
            key: changed_secret for key in credentials
        }}}
    elif fault == "declared-conflict":
        binding = binding.model_copy(update={"key_vault_secret_id": HttpUrl(secret_id)})
        manifest = {"route_credentials": {"version": 1, "bindings": {
            **credentials, route_credential_source(binding): changed_secret,
        }}}
    publication = publication.model_copy(update={
        "gateway_profile_id": target, "resource_manifest": manifest,
        "desired_spec": publication.desired_spec.model_copy(update={"bindings": [binding]}),
    })
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256="f" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/turnstile", publisher_subscription_id="unit",
        parent_policy_sha256="a" * 64, operation_policies_sha256={"models": "b" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
        pool_enabled=pool_enabled,
    )
    configuration = ApimTargetClientConfig(target, "f" * 64, True, Settings.model_construct(
        azure_subscription_id=str(UUID(int=1)), apim_resource_group="unit-group",
        apim_service_name="unit-apim", apim_api_id=program.scope.api_id,
    ), route_target_identity_sha256=installation.target_identity_sha256)
    repository, configurations, management = MagicMock(), MagicMock(), MagicMock()
    configurations.resolve.return_value = configuration
    repository.checkpoint_gateway_publication_preparation.return_value = True

    def prepare(connection: ConnectionRevision, persist: Any,
                *, recovery_only: bool = False) -> str:
        assert recovery_only is False
        persist({"unit": "connection-intent"})
        return connection.dependency_sha256

    management.prepare_route_connection.side_effect = prepare
    management.verify_route_connection.side_effect = lambda value: value.dependency_sha256
    factory = FixedRoutePublisherFactory(
        repository, configurations, active_fence=MagicMock(),
        availability_projector=MagicMock(), clock=lambda: 1100,
    )
    registry = {"runtimes": [{
        "id": binding.runtime_id, "enabled": True,
        "gateway_profile_id": target, "config": current,
    }]}
    prepare_connection = factory._connection_preparer(
        publication, installation, configuration, management, "unit-worker", lambda: None,
    )
    if fault is not None:
        with pytest.raises(PolicyCompilationError):
            factory.approved_connections(publication, installation, management, baseline=snapshot,
                registry=registry, prepare_connection=prepare_connection)
        management.prepare_route_connection.assert_not_called()
        management.prepare_route_pool.assert_not_called()
        management.verify_route_connection.assert_not_called()
    else:
        approved = factory.approved_connections(publication, installation, management,
            baseline=snapshot, registry=registry, prepare_connection=prepare_connection)
        assert approved[binding.model.model_key].state == "approved"
        if pool_enabled:
            management.prepare_route_pool.assert_called_once()
            assert management.prepare_route_pool.call_args.args[0] == (
                approved[binding.model.model_key].connection
            )
            assert len(management.prepare_route_pool.call_args.args[1]) == 2
        else:
            management.prepare_route_connection.assert_called_once()
            repository.checkpoint_gateway_publication_preparation.assert_called_once()
        management.verify_route_connection.assert_called_once_with(
            approved[binding.model.model_key].connection,
        )


def test_aliases_of_one_existing_connection_share_one_route_approval() -> None:
    from itertools import count

    from tests.backend.model_platform.test_storage_route_projection import snapshot_fixture
    from turnstile_core.persistence.repository_support import _activated_runtime_credentials
    from turnstile_core.services.route_publication import route_credential_source

    publication, _ = budget_publication_contract_fixture()
    snapshot, program, _ = snapshot_fixture()
    target = UUID(program.scope.target_id)
    binding = publication.desired_spec.bindings[0].model_copy(update={
        "runtime_id": UUID(int=923), "api_format": ApiFormat.OPENAI_CHAT,
        "auth_strategy": AuthStrategy.NAMED_VALUE_API_KEY,
        "named_value_name": "unit-runtime-key", "key_vault_secret_id": None,
    })
    assert binding.backend_url is not None
    runtime_config = {
        **binding.runtime_config, "control_plane_managed": True,
        "backend_url": str(binding.backend_url), "backend_path": binding.backend_path,
        "api_format": binding.api_format.value, "auth_strategy": binding.auth_strategy.value,
        "managed_identity_resource": binding.managed_identity_resource,
    }
    binding = binding.model_copy(update={"runtime_config": runtime_config})
    sibling = binding.model_copy(update={
        "model": binding.model.model_copy(update={"model_key": "unit-sibling-alias"}),
    })
    secret_id = "https://unit-vault.vault.azure.net/secrets/runtime-key/" + "a" * 32
    manifest = {"route_credentials": {"version": 1, "bindings": {
        route_credential_source(binding): secret_id,
    }}}
    activated = _activated_runtime_credentials(
        runtime_config, binding.model_dump(mode="json"), manifest,
    )
    publication = publication.model_copy(update={
        "gateway_profile_id": target, "resource_manifest": manifest,
        "desired_spec": publication.desired_spec.model_copy(update={
            "bindings": [binding, sibling],
        }),
    })
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256="f" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/turnstile", publisher_subscription_id="unit",
        parent_policy_sha256="a" * 64, operation_policies_sha256={"models": "b" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
    )
    configuration = ApimTargetClientConfig(target, "f" * 64, True, Settings.model_construct(
        azure_subscription_id=str(UUID(int=1)), apim_resource_group="unit-group",
        apim_service_name="unit-apim", apim_api_id=program.scope.api_id,
    ), route_target_identity_sha256=installation.target_identity_sha256)
    repository, configurations, management = MagicMock(), MagicMock(), MagicMock()
    configurations.resolve.return_value = configuration
    # The Backend already exists from earlier model additions, so preparation writes nothing.
    management.prepare_route_connection.side_effect = (
        lambda connection, persist, *, recovery_only=False: connection.dependency_sha256
    )
    management.verify_route_connection.side_effect = lambda value: value.dependency_sha256
    ticks = count(1100)
    factory = FixedRoutePublisherFactory(
        repository, configurations, active_fence=MagicMock(),
        availability_projector=MagicMock(), clock=lambda: next(ticks),
    )
    registry = {"runtimes": [{
        "id": binding.runtime_id, "enabled": True,
        "gateway_profile_id": target, "config": activated,
    }]}
    approved = factory.approved_connections(
        publication, installation, management, baseline=snapshot, registry=registry,
        prepare_connection=factory._connection_preparer(
            publication, installation, configuration, management, "unit-worker", lambda: None,
        ),
    )
    first, second = approved[binding.model.model_key], approved[sibling.model.model_key]
    assert management.prepare_route_connection.call_count == 2
    assert first.connection.revision_id == second.connection.revision_id
    assert first == second
    repository.checkpoint_gateway_publication_preparation.assert_not_called()


@pytest.mark.parametrize("variant", ["base", "addition", "forged", "reconcile", "unrelated"])
def test_fixed_route_rotation_renews_only_from_the_connection_base_credential(
    variant: str,
) -> None:
    from tests.backend.model_platform.test_storage_route_projection import snapshot_fixture
    from turnstile_core.services.route_publication import route_credential_source

    publication, _ = budget_publication_contract_fixture()
    snapshot, program, _ = snapshot_fixture()
    target = UUID(program.scope.target_id)
    prior = publication.desired_spec.bindings[0].model_copy(update={
        "runtime_id": UUID(int=923), "api_format": ApiFormat.OPENAI_CHAT,
        "auth_strategy": AuthStrategy.NAMED_VALUE_API_KEY, "named_value_name": "unit-runtime-key",
        "key_vault_secret_id": None,
    })
    assert prior.backend_url is not None
    config = {
        **prior.runtime_config, "control_plane_managed": True,
        "backend_url": str(prior.backend_url), "backend_path": prior.backend_path,
        "api_format": prior.api_format.value, "auth_strategy": prior.auth_strategy.value,
        "managed_identity_resource": prior.managed_identity_resource,
        "named_value_name": prior.named_value_name,
    }
    prior = prior.model_copy(update={"runtime_config": config})
    rotated_name = "finops-credential-" + "c" * 20
    rotated = prior.model_copy(update={
        "named_value_name": rotated_name,
        "runtime_config": {**config, "named_value_name": rotated_name},
    })
    vault = "https://unit-vault.vault.azure.net/secrets/"
    recorded, materialized = vault + "runtime-key/" + "a" * 32, vault + "rotated-key/" + "b" * 32
    # Only the base release, or a model addition pinned to the record, may prove the record.
    routed = variant not in {"addition", "unrelated"}
    other = prior.model_copy(update={
        "runtime_id": UUID(int=924),
        "model": prior.model.model_copy(update={"model_key": "unit-other-model"}),
    })
    base = publication.model_copy(update={
        "id": UUID(int=931), "gateway_profile_id": target,
        "publication_kind": PublicationKind.ROUTE_RECONCILE,
        "resource_manifest": {"route_credentials": {"version": 1, "bindings": {
            route_credential_source(prior): recorded,
        } if routed else {}}},
        "desired_spec": publication.desired_spec.model_copy(update={
            "gateway_profile_id": target, "bindings": [prior if routed else other],
            "discovery_models": [item.model_copy(update={"id": "unit-other-model"})
                                 if not routed else item
                                 for item in publication.desired_spec.discovery_models],
        }),
    })
    addition = publication.model_copy(update={
        "id": UUID(int=932), "gateway_profile_id": target, "base_release_id": base.id,
        "publication_kind": PublicationKind.MODEL_ADD, "resource_manifest": {},
        "desired_spec": publication.desired_spec.model_copy(update={
            "gateway_profile_id": target,
            "bindings": [prior.model_copy(update={"key_vault_secret_id": HttpUrl(recorded)})],
        }),
    })
    rotation = publication.model_copy(update={
        "id": UUID(int=933), "gateway_profile_id": target, "base_release_id": base.id,
        "publication_kind": PublicationKind.ROUTE_RECONCILE if variant == "reconcile"
        else PublicationKind.CREDENTIAL_ROTATION,
        "resource_manifest": {},
        "desired_spec": publication.desired_spec.model_copy(update={
            "gateway_profile_id": target, "bindings": [rotated],
        }),
    })
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256="f" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/turnstile", publisher_subscription_id="unit",
        parent_policy_sha256="a" * 64, operation_policies_sha256={"models": "b" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
        credential_vault_origin="https://unit-vault.vault.azure.net",
        trusted_vault_origins=("https://unit-vault.vault.azure.net",),
    )
    configuration = ApimTargetClientConfig(target, "f" * 64, True, Settings.model_construct(
        azure_subscription_id=str(UUID(int=1)), apim_resource_group="unit-group",
        apim_service_name="unit-apim", apim_api_id=program.scope.api_id,
    ), route_target_identity_sha256=installation.target_identity_sha256)
    cipher = CredentialCipher(Fernet.generate_key())
    repository, configurations, management = MagicMock(), MagicMock(), MagicMock()
    configurations.resolve.return_value = configuration
    repository.get_gateway_publication.return_value = base.model_dump(mode="python")
    repository.active_model_route_publications.return_value = (
        [addition.model_dump(mode="python")] if variant == "addition" else []
    )
    repository.gateway_publication_credential.return_value = cipher.encrypt("unit-rotated-key")
    repository.checkpoint_gateway_publication_credentials.return_value = True
    repository.checkpoint_gateway_publication_preparation.return_value = True

    def prepare(connection: ConnectionRevision, persist: Any,
                *, recovery_only: bool = False) -> str:
        persist({"unit": "connection-intent"})
        return connection.dependency_sha256

    management.prepare_route_connection.side_effect = prepare
    management.verify_route_connection.side_effect = lambda value: value.dependency_sha256
    factory = FixedRoutePublisherFactory(
        repository, configurations, active_fence=MagicMock(),
        availability_projector=MagicMock(), clock=lambda: 1100, cipher=cipher,
    )
    registry = {"runtimes": [{
        "id": prior.runtime_id, "enabled": True, "gateway_profile_id": target,
        "config": {**config, "key_vault_secret_id": (
            recorded[:-32] + "f" * 32 if variant == "forged" else recorded
        )},
    }]}
    accepted = variant in {"base", "addition"}
    if accepted:
        with (
            patch("turnstile_core.services.storage_route_publisher.RouteCredentialVault") as vaults,
            patch("turnstile_core.services.storage_route_publisher.ManagedIdentityTokenProvider"),
        ):
            vaults.return_value.materialize.return_value = materialized
            rotation, keys = factory._prepare_credentials(
                rotation, base, installation, management, "unit-worker", lambda: None,
                registry=registry,
            )
        assert keys == {route_credential_source(rotated): materialized}
    else:
        rotation = rotation.model_copy(update={"resource_manifest": {"route_credentials": {
            "version": 1, "bindings": {route_credential_source(rotated): materialized},
        }}})
    prepare_connection = factory._connection_preparer(
        rotation, installation, configuration, management, "unit-worker", lambda: None,
    )
    if accepted:
        approved = factory.approved_connections(rotation, installation, management,
            baseline=snapshot, registry=registry, prepare_connection=prepare_connection)
        assert approved[rotated.model.model_key].state == "approved"
    else:
        with pytest.raises(PolicyCompilationError, match="independent connection approval"):
            factory.approved_connections(rotation, installation, management, baseline=snapshot,
                registry=registry, prepare_connection=prepare_connection)
        management.prepare_route_connection.assert_not_called()


def test_storage_route_document_is_strict_and_canonical_with_no_duplicate_properties() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    document = canonical_document(scope)
    assert parse_document(RouteScope, document, MAX_HEAD_BYTES) == scope
    assert scope.partition.startswith("route-v2:") and len(scope.partition) == 73
    with pytest.raises(ValueError, match="DUPLICATE_JSON_PROPERTY"):
        parse_document(RouteScope, document.replace('"api_id":', '"api_id":"other","api_id":'),
                       MAX_HEAD_BYTES)
    with pytest.raises(ValueError, match="TOO_LARGE"):
        parse_document(RouteScope, document, 1)
    with pytest.raises(ValidationError):
        RouteScope.model_validate({**scope.model_dump(), "policy_abi": True})
    validate_origin("https://unit.example.test")
    validate_path("/providers/microsoft/v1/systemone")
    for origin in ("http://unit.example.test", "https://user:secret@unit.example.test",
                   "https://unit.example.test/path", "https://unit.example.test?token=unit"):
        with pytest.raises(ValueError, match="ORIGIN"):
            validate_origin(origin)
    for path in ("//unit.example.test", "/a/../b", "/a%2fb", "/a?token=unit", "/a b"):
        with pytest.raises(ValueError, match="PATH"):
            validate_path(path)


def test_storage_route_credentials_keep_versioned_secret_and_counter_origin_contract() -> None:
    secret_id = "https://unit-vault.vault.azure.net/secrets/unit-key/" + "a" * 32
    credential = RouteCredential(mode="key_vault_api_key", header="api-key", secret_id=secret_id)
    assert credential.secret_id == secret_id
    for invalid in (secret_id.rsplit("/", 1)[0], secret_id + "?token=unit",
                    secret_id.replace("https://", "http://")):
        with pytest.raises(ValidationError, match="VERSIONED_SECRET"):
            RouteCredential(mode="key_vault_api_key", header="api-key", secret_id=invalid)
    with pytest.raises(ValidationError, match="UNAUTHENTICATED"):
        RouteCredential(mode="none", header="Authorization")
    with pytest.raises(ValidationError, match="MANAGED_IDENTITY"):
        RouteCredential(mode="managed_identity", header="Authorization")
    counter = CountCredential(mode="managed_identity", header="Authorization",
                              audience="https://cognitiveservices.azure.com",
                              origin="https://unit.services.ai.azure.com",
                              endpoints=("/responses/input_tokens",))
    assert counter.origin == "https://unit.services.ai.azure.com"
    with pytest.raises(ValidationError, match="COUNT_ENDPOINT"):
        CountCredential.model_validate({**counter.model_dump(),
                                        "endpoints": ("/providers/microsoft/v1/systemone",)})


def test_storage_route_connection_binds_count_origin_and_exact_approval_document() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    credential = RouteCredential(mode="managed_identity", header="Authorization",
                                 audience="https://cognitiveservices.azure.com")
    counter = CountCredential(**credential.model_dump(), origin="https://unit.services.ai.azure.com",
                              endpoints=("/responses/input_tokens",))
    connection = ConnectionRevision(
        scope=scope, revision_id=str(UUID(int=2)), backend_id="unit-backend",
        origin=counter.origin, path="/providers/microsoft/v1/systemone",
        dependency_sha256="a" * 64, credential=credential, count=counter,
    )
    with pytest.raises(ValidationError, match="COUNTER_ORIGIN"):
        ConnectionRevision.model_validate({**connection.model_dump(), "count": {
            **counter.model_dump(), "origin": "https://other.services.ai.azure.com",
        }})
    approved = ConnectionApproval(approval_id=str(UUID(int=3)), connection=connection,
                                  state="approved", evidence_sha256="b" * 64,
                                  issued_epoch=100, expires_epoch=200)
    approved.require_current(connection, 101)
    with pytest.raises(ValueError, match="APPROVAL_INVALID"):
        approved.require_current(connection.model_copy(update={"dependency_sha256": "c" * 64}), 101)
    with pytest.raises(ValueError, match="POOL_UNEXPECTED"):
        connection.require_probe_member("unrelated")


def test_native_decision_route_content_binds_exact_model_budget_and_profile() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    model_id = UUID(int=901)
    connection = ConnectionRevision(
        scope=scope, revision_id=str(UUID(int=2)), backend_id="unit-backend",
        origin="https://unit.services.ai.azure.com", path="/providers/microsoft/v1/systemone",
        dependency_sha256="a" * 64,
        credential=RouteCredential(mode="managed_identity", header="Authorization",
                                   audience="https://cognitiveservices.azure.com"),
    )
    budget = ModelCountContract(model_id=model_id, algorithm="legacy_request_estimate_v1",
                                output_field="none", output_estimate=4096,
                                operations=("microsoft-decision",), evidence_sha256="b" * 64,
                                verified_at=datetime(2026, 10, 12, tzinfo=UTC))
    content = RouteContent(scope=scope, model_id=str(model_id), alias="unit-decision",
                           display_name="Unit Decision",
                           created_at=datetime(2026, 10, 12, tzinfo=UTC),
                           upstream_model="unit-native-deployment", profile="microsoft-decision",
                           streaming_mode="buffered", operations=("microsoft-decision",),
                           connection=connection, budget_epoch=str(UUID(int=3)), budget=budget)
    assert len(content.sha256) == 64
    assert RouteContent.model_validate_json(canonical_document(content)) == content
    connection_approval = ConnectionApproval(approval_id=str(UUID(int=8)), connection=connection,
                                             state="approved", evidence_sha256="c" * 64,
                                             issued_epoch=100, expires_epoch=200)
    version = str(UUID(int=9))
    approved_connections = {connection.revision_id: connection_approval}
    members, manifest, dependencies = configuration_manifests(
        scope, version, [content], approved_connections, 101,
    )
    configuration = ConfigurationApproval(
        approval_id=str(UUID(int=10)), scope=scope, route_version=version, generation=1,
        manifest_sha256=manifest, dependency_manifest_sha256=dependencies,
        catalog_sha256=catalog_digest([content]), entry_count=1,
        required_program_sha256="d" * 64, required_profiles=("microsoft-decision",),
        budget_epoch=content.budget_epoch, issued_epoch=100, activate_before_epoch=200,
    )
    snapshot = RouteSnapshot.prepare(configuration, [content], approved_connections, 101)
    assert snapshot.members == members and len(snapshot.configuration_sha256) == 64
    stored: dict[tuple[str, str], str] = {}
    with httpx.Client() as client:
        projection = TableRouteProjection("https://unitstore.table.core.windows.net",
                                          "TurnstileRoutes", scope, MagicMock(), client)

        def store_document(partition: str, row: str, document: str, maximum: int) -> None:
            stored[(partition, row)] = document

        def read_document(partition: str, row: str, maximum: int) -> tuple[str, str] | None:
            document = stored.get((partition, row))
            return (document, 'W/"unit"') if document is not None else None

        with patch.object(projection, "_stage_entity", side_effect=store_document), \
                patch.object(projection, "_read_document", side_effect=read_document):
            assert projection.stage_slice(snapshot, 101, 0, maximum=3) == 3
            with pytest.raises(RouteProjectionConflict, match="FINAL_READBACK"):
                projection.require_verified_snapshot(snapshot, 101)
            assert projection.verify_slice_recorded(snapshot, 101, 0, maximum=3) == 3
            projection.require_verified_snapshot(snapshot, 101)
            assert len(stored) == 4
            stored.pop(next(key for key in stored if key[0].endswith(":content")))
            with pytest.raises(RouteProjectionConflict, match="INCOMPLETE"):
                projection.verify_snapshot(snapshot, 101)
    assert changed_route_aliases(
        [content], [content.model_copy(update={"display_name": "Renamed"})],
    ) == ()
    with pytest.raises(ValueError, match="CONNECTION_APPROVAL_REQUIRED"):
        RouteSnapshot.prepare(configuration, [content], {}, 101)
    with pytest.raises(ValueError, match="DUPLICATE_ALIAS"):
        configuration_manifests(scope, version, [content, content], approved_connections, 101)
    program = ProgramIdentity(scope=scope, api_revision="1", instance_id="managed",
                               program_sha256="d" * 64, trust_binding_sha256="e" * 64)
    proof = ProgramCapabilityProof(proof_id=str(UUID(int=11)), identity=program,
                                   supported_profiles=("microsoft-decision",),
                                   issued_epoch=100, expires_epoch=220)
    addition = ModelRouteAddition.prepare(str(UUID(int=12)), program, proof, content,
                                          connection_approval, content.budget_epoch, 101)
    model_head = addition.active_head(proof, content.budget_epoch, 101)
    assert model_head.alias == content.alias and model_head.content_sha256 == content.sha256
    assert model_head.row_key.startswith("model:") and len(model_head.row_key) == 70
    transition = RouteTransitionReceipt(operation_id=str(UUID(int=15)), scope=scope,
                                         before_etag=None, before_generation=0,
                                         after_sha256=document_digest(model_head),
                                         route_version=model_head.head.approval.route_version,
                                         approval_id=model_head.head.approval.approval_id)
    endpoint, table = "https://unitstore.table.core.windows.net", "TurnstileRoutes"
    content_type, body = conditional_batch(endpoint, table, scope.partition, [
        (model_head.row_key, route_entity(scope, scope.partition, model_head.row_key,
                                          canonical_document(model_head)), None),
        ("model-transition:" + transition.operation_id,
         route_entity(scope, scope.partition, "model-transition:" + transition.operation_id,
                      canonical_document(transition)), None),
    ])
    intent = ModelRouteActivationIntent(transition, model_head, content_type, body, endpoint, table)
    record = intent.to_record()
    assert ModelRouteActivationIntent.from_record(record) == intent
    repository = InMemoryRepository()
    publication_id = UUID(model_head.publication_id)
    row = {"id": publication_id, "status": "promoting", "resource_manifest": {}}
    repository.gateway_publications.append(row)
    repository.gateway_publication_outbox.append({
        "publication_id": publication_id, "status": "leased", "lease_owner": "unit-worker",
        "lease_expires_at": datetime.now(UTC) + timedelta(minutes=1),
    })
    persistent = PersistentModelRouteActivationJournal(
        repository, publication_id, "unit-worker", "a" * 64, None, lambda: None,
    )
    persistent.begin_once(intent)
    assert persistent.intent == intent and persistent.outcome == "started"
    persistent.record_outcome(transition.operation_id, "unknown_no_replay")
    with pytest.raises(ValueError, match="RECOVER_ONLY"):
        persistent.begin_once(intent)
    persistent.record_outcome(transition.operation_id, "confirmed")
    with pytest.raises(ValueError, match="REGRESSION"):
        persistent.record_outcome(transition.operation_id, "unknown_no_replay")
    with pytest.raises(ValueError, match="BODY_CHANGED"):
        ModelRouteActivationIntent.from_record({**record, "body_sha256": "a" * 64})
    journal, submissions = MagicMock(), []
    with httpx.Client() as client:
        projection = TableRouteProjection(endpoint, table, scope, MagicMock(), client)

        def lost_activation(method: str, url: str, **kwargs: Any) -> httpx.Response:
            journal.begin_once.assert_called_once()
            submissions.append(url)
            raise httpx.ReadTimeout("unit unknown model activation")

        with patch.object(projection, "require_verified_snapshot"), \
                patch.object(projection, "read_model_active", return_value=None), \
                patch.object(projection, "_read_document", return_value=None), \
                patch.object(projection, "_request", side_effect=lost_activation):
            with pytest.raises(RouteProjectionUnknown, match="UNKNOWN_RECOVER_ONLY"):
                projection.activate_model_addition(addition, proof, content.budget_epoch,
                                                    str(UUID(int=17)), lambda: 101, journal)
            assert projection.recover_model_addition(
                journal.begin_once.call_args.args[0],
            ) == "unknown_no_replay"
        assert len(submissions) == 1
        journal.record_outcome.assert_called_once_with(str(UUID(int=17)), "unknown_no_replay")
    plan = RouteConfigurationPlan.prepare([], addition.snapshot, program, proof,
                                           content.budget_epoch, 101)
    task = RoutePublicationTask(
        model_addition=True, configuration_only=True, publication_id=addition.publication_id,
        base_publication_id=str(UUID(int=14)), desired_spec_sha256="f" * 64,
        target_identity_sha256="1" * 64, storage_endpoint="https://unitstore.table.core.windows.net",
        storage_table="TurnstileRoutes", plan=plan, previous_routes=(), previous_head=None,
    )
    assert task.plan.policy_write_count == 0 and len(task.context_sha256) == 64
    candidate = RouteCandidateProbe(probe_id=str(UUID(int=19)), publication_id=task.publication_id,
                                     request_id=str(UUID(int=20)),
                                     subscription_id="unit-subscription",
                                     alias=content.alias, operation="microsoft-decision",
                                     body_sha256="a" * 64, head=model_head.head,
                                     issued_epoch=101, expires_epoch=200)
    with httpx.Client() as client:
        projection = TableRouteProjection(endpoint, table, scope, MagicMock(), client)
        with patch.object(projection, "_stage_entity") as stage:
            projection.stage_candidate(candidate, addition.snapshot, program, proof, 101)
            stage.assert_called_once()
            with pytest.raises(RouteProjectionConflict, match="SNAPSHOT_CHANGED"):
                projection.stage_candidate(
                    candidate.model_copy(update={"operation": "chat-completions"}),
                    addition.snapshot, program, proof, 101,
                )
    journal, dispatched = MagicMock(), []
    with httpx.Client() as client:
        projection = TableRouteProjection(endpoint, table, scope, MagicMock(), client)

        def unknown_configuration(method: str, url: str, **kwargs: Any) -> httpx.Response:
            journal.begin_once.assert_called_once()
            dispatched.append(url)
            raise httpx.ReadTimeout("unit unknown configuration activation")

        with patch.object(projection, "verify_snapshot"), \
                patch.object(projection, "read_writer", return_value=None), \
                patch.object(projection, "read_active", return_value=None), \
                patch.object(projection, "_read_document", return_value=None), \
                patch.object(projection, "_request", side_effect=unknown_configuration):
            with pytest.raises(RouteProjectionUnknown, match="UNKNOWN_RECOVER_ONLY"):
                projection.activate(addition.snapshot, program, proof, content.budget_epoch,
                                      str(UUID(int=18)), None, lambda: 101, journal)
            assert projection.recover_activation(journal.begin_once.call_args.args[0]) == "unknown"
        assert len(dispatched) == 1
        journal.record_outcome.assert_called_once_with(str(UUID(int=18)), "unknown_no_replay")
    renewed_approval = connection_approval.model_copy(update={
        "approval_id": str(UUID(int=16)), "issued_epoch": 101, "expires_epoch": 210,
    })
    renewal = RouteApprovalRenewal(scope=scope, content_sha256=content.sha256,
                                   original_approval_sha256=members[0].approval_sha256,
                                   required_program_sha256=program.program_sha256,
                                   budget_epoch=content.budget_epoch, approval=renewed_approval)
    stored_renewals: dict[str, str] = {}
    batches = 0
    with httpx.Client() as client:
        projection = TableRouteProjection(endpoint, table, scope, MagicMock(), client)

        def renewal_read(partition: str, row: str, maximum: int) -> tuple[str, str] | None:
            document = stored_renewals.get(row)
            return (document, 'W/"unit"') if document is not None else None

        def renewal_dispatch(method: str, url: str, **kwargs: Any) -> httpx.Response:
            nonlocal batches
            if url.endswith("/$batch"):
                batches += 1
                raise httpx.ReadTimeout("unit unknown approval write")
            entity = kwargs["json"]
            stored_renewals[entity["RowKey"]] = entity["Document"]
            return httpx.Response(201)

        with patch.object(projection, "_read_document", side_effect=renewal_read), \
                patch.object(projection, "_request", side_effect=renewal_dispatch):
            for _attempt in range(2):
                with pytest.raises(RouteProjectionUnknown, match="UNKNOWN_RECOVER_ONLY"):
                    projection.publish_approval_renewal(
                        renewal, content, members[0], proof, None, 101,
                    )
    assert batches == 1
    task.require_publication(task.publication_id, task.base_publication_id,
                             task.desired_spec_sha256, scope.target_id)
    with pytest.raises(ValueError, match="PUBLICATION_CHANGED"):
        task.require_publication(task.publication_id, task.base_publication_id,
                                 "2" * 64, scope.target_id)
    with pytest.raises(ValueError, match="PROGRAM_OR_BUDGET"):
        ModelRouteAddition.prepare(str(UUID(int=12)), program, proof, content,
                                    connection_approval, str(UUID(int=13)), 101)
    changes: list[tuple[dict[str, Any], str]] = [
        ({"model_id": str(UUID(int=999))}, "IDENTITY_MISMATCH"),
        ({"profile": "openai-v1"}, "PROFILE_OPERATION_MISMATCH"),
        ({"operations": ["chat-completions"]}, "PROFILE_OPERATION_MISMATCH"),
    ]
    for changed, message in changes:
        with pytest.raises(ValidationError, match=message):
            RouteContent.model_validate_json(json.dumps({
                **content.model_dump(mode="json"), **changed,
            }))


def test_installed_program_requires_native_decision_operation_and_dedicated_public_table() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    identity = ProgramIdentity(scope=scope, api_revision="1", instance_id="managed",
                                program_sha256="a" * 64, trust_binding_sha256="b" * 64)
    installed = FixedProgramInstallation(
        program=identity, target_identity_sha256="c" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net",
        storage_table="TurnstileRoutes",
        gateway_endpoint="https://unit-apim.example.test/llm",
        publisher_subscription_id="unit-publisher",
        parent_policy_sha256="d" * 64, operation_policies_sha256={"microsoft-decision": "e" * 64},
        operation_contracts={"microsoft-decision": ("POST", "/providers/microsoft/v1/systemone")},
        supported_profiles=("microsoft-decision",), decision_enabled=True,
    )
    assert installed.for_native_gateway(
        "managed", "managed", installed.gateway_endpoint,
    ) == installed
    requests: list[str] = []

    def program_probe(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        headers = {"abi": "2", "nonce": request.headers["x-turnstile-route-probe"],
                   "target": scope.target_id, "api": scope.api_id, "revision": "1",
                   "gateway": "managed", "instance": "managed",
                   "program": identity.program_sha256, "trust": identity.trust_binding_sha256}
        return httpx.Response(204, headers={"x-turnstile-route-" + key: value
                                            for key, value in headers.items()})

    with httpx.Client(transport=httpx.MockTransport(program_probe)) as client:
        verifier = FixedProgramVerifier(installed, MagicMock(), SecretStr("unit-only"), client)
        verifier._probe(configuration=None)
        assert requests == ["HEAD"]
        with pytest.raises(ValueError, match="public protocol contract is missing"):
            verifier.verify()
        assert requests == ["HEAD"]
    enrollment = ModelAvailabilityNodeEnrollment.model_validate({
        "enrollment_id": UUID(int=21), "target_id": UUID(scope.target_id),
        "deployment_id": UUID(int=22), "api_id": scope.api_id, "native_gateway_id": "unit-edge",
        "revision": 1, "controller_evidence_sha256": "1" * 64,
        "fenced_installation_sha256": "2" * 64,
        "rejoin_contract": "enrolled_nodes_only_guarded_bootstrap",
        "enrolled_at": datetime.now(UTC),
        "nodes": [{"instance_id": "node-b", "endpoint_url": "https://node-b.example.test/llm"},
                  {"instance_id": "node-a", "endpoint_url": "https://node-a.example.test/llm"}],
    })
    deployment = {"id": enrollment.deployment_id, "apim_target_id": enrollment.target_id,
                  "native_gateway_id": enrollment.native_gateway_id, "declared_state": "present",
                  "retired_at": None, "endpoint_url": "https://unit-edge.example.test/llm"}
    native = installed.model_copy(update={"native_gateway_scope": True})
    programs = native_gateway_programs(native, deployment, enrollment)
    factory = FixedRoutePublisherFactory(
        InMemoryRepository(), MagicMock(), active_fence=MagicMock(),
        availability_projector=MagicMock(), clock=lambda: 101,
    )
    assert factory.bound_gateway_programs(
        native, {**deployment, "kind": "self_hosted"}, (enrollment,),
    ) == programs
    with pytest.raises(ValueError, match="ambiguous"):
        factory.bound_gateway_programs(native, {**deployment, "kind": "self_hosted"},
                                       (enrollment, enrollment))
    assert [entry.program.instance_id for entry in programs] == ["node-a", "node-b"]
    assert all(entry.program.scope.gateway_id == "unit-edge" for entry in programs)
    assert programs[0].gateway_endpoint == "https://node-a.example.test/llm"
    for changed in ({"retired_at": datetime.now(UTC)}, {"apim_target_id": UUID(int=23)},
                    {"native_gateway_id": "other"}, {"declared_state": "missing"}):
        with pytest.raises(ValueError, match="current node enrollment"):
            native_gateway_programs(native, {**deployment, **changed}, enrollment)
    with pytest.raises(ValueError, match="NATIVE_GATEWAY_SCOPE"):
        installed.for_native_gateway("unit-edge", "instance", "https://unit-edge.example.test/llm")
    with pytest.raises(ValidationError, match="DECISION_CAPABILITY"):
        FixedProgramInstallation.model_validate_json(canonical_document({
            **installed.model_dump(mode="json"), "decision_enabled": False,
        }))
    with pytest.raises(ValueError, match="DEDICATED_TABLE"):
        require_route_storage(installed.storage_endpoint, "TurnstileLedger")
    upgraded = FixedProgramInstallation.model_validate_json(canonical_document({
        **installed.model_dump(mode="json"),
        "program": {**installed.program.model_dump(mode="json"), "program_sha256": "f" * 64},
        "parent_policy_sha256": "1" * 64, "model_additions_enabled": True,
        "legacy_program_sha256": installed.program.program_sha256,
    }))
    upgrade = FixedProgramUpgrade(previous_publication_id=str(UUID(int=8)),
                                  previous_installation_sha256=document_digest(installed),
                                  installation=upgraded)
    upgrade.require_previous(str(UUID(int=8)), installed)
    with pytest.raises(ValueError, match="BINDING_CHANGED"):
        upgrade.require_previous(str(UUID(int=9)), installed)
    with pytest.raises(ValueError, match="BINDING_CHANGED"):
        upgrade.model_copy(update={"previous_installation_sha256": "2" * 64}).require_previous(
            str(UUID(int=8)), installed,
        )
    with pytest.raises(ValidationError):
        FixedProgramCutover(upgrade=upgrade, parent_policy="<policies />", expected_parent_etag="*")


def test_program_capability_proof_is_short_and_exact_not_a_program_upgrade() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    identity = ProgramIdentity(scope=scope, api_revision="1", instance_id="managed",
                                program_sha256="a" * 64, trust_binding_sha256="b" * 64)
    proof = ProgramCapabilityProof(proof_id=str(UUID(int=2)), identity=identity,
                                   supported_profiles=("microsoft-decision",),
                                   issued_epoch=100, expires_epoch=220)
    assert proof.identity == identity
    for changed in ({"expires_epoch": 221}, {"expires_epoch": 100},
                    {"supported_profiles": ["microsoft-decision", "microsoft-decision"]}):
        with pytest.raises(ValidationError, match="PROOF_INVALID"):
            ProgramCapabilityProof.model_validate_json(json.dumps({
                **proof.model_dump(mode="json"), **changed,
            }))


def test_active_route_head_requires_exact_program_profile_budget_and_current_proof() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    identity = ProgramIdentity(scope=scope, api_revision="1", instance_id="managed",
                                program_sha256="a" * 64, trust_binding_sha256="b" * 64)
    proof = ProgramCapabilityProof(proof_id=str(UUID(int=2)), identity=identity,
                                   supported_profiles=("microsoft-decision",),
                                   issued_epoch=100, expires_epoch=220)
    approval = ConfigurationApproval(
        approval_id=str(UUID(int=3)), scope=scope, route_version=str(UUID(int=4)), generation=1,
        manifest_sha256="c" * 64, dependency_manifest_sha256="d" * 64, catalog_sha256="e" * 64,
        entry_count=1, required_program_sha256=identity.program_sha256,
        required_profiles=("microsoft-decision",), budget_epoch=str(UUID(int=5)),
        issued_epoch=100, activate_before_epoch=200,
    )
    head = ActiveRouteHead(approval=approval, activated_epoch=101)
    head.require_ready(identity, proof, approval.budget_epoch, 101, activating=True)
    transition = RouteTransitionReceipt(operation_id=str(UUID(int=7)), scope=scope,
                                         before_etag=None, before_generation=0,
                                         after_sha256=document_digest(head),
                                         route_version=approval.route_version,
                                         approval_id=approval.approval_id)
    endpoint, table = "https://unitstore.table.core.windows.net", "TurnstileRoutes"
    content_type, body = conditional_batch(endpoint, table, scope.partition, [
        ("active", route_entity(scope, scope.partition, "active", canonical_document(head)), None),
        ("transition:" + transition.operation_id,
         route_entity(scope, scope.partition, "transition:" + transition.operation_id,
                      canonical_document(transition)), None),
    ])
    intent = RouteActivationIntent(transition, head, content_type, body,
                                   text_digest(body.decode()), endpoint, table)
    assert RouteActivationIntent.from_record(intent.to_record()) == intent
    repository = InMemoryRepository()
    publication_id = UUID(int=8)
    repository.gateway_publications.append({
        "id": publication_id, "status": "promoting", "resource_manifest": {},
    })
    repository.gateway_publication_outbox.append({
        "publication_id": publication_id, "status": "leased", "lease_owner": "unit-worker",
        "lease_expires_at": datetime.now(UTC) + timedelta(minutes=1),
    })
    persistent = PersistentRouteActivationJournal(
        repository, publication_id, "unit-worker", "a" * 64, None,
    )
    persistent.begin_once(intent)
    assert persistent.intent == intent
    persistent.record_outcome(transition.operation_id, "unknown_no_replay")
    resumed = PersistentRouteActivationJournal(
        repository, publication_id, "unit-worker", "a" * 64, persistent.snapshot(),
    )
    with pytest.raises(ValueError, match="RECOVER_ONLY"):
        resumed.begin_once(intent)
    recovery = MagicMock(spec=TableRouteProjection)
    recovery.recover_activation.return_value = "confirmed"
    assert resumed.recover(recovery) == "confirmed"
    recovery.recover_activation.assert_called_once_with(intent)
    with pytest.raises(ValueError, match="REGRESSION"):
        resumed.record_outcome(transition.operation_id, "unknown")
    with pytest.raises(ValueError, match="INTENT_INVALID"):
        RouteActivationIntent.from_record({**intent.to_record(), "body_sha256": "a" * 64})
    with pytest.raises(ValueError, match="BINDING_INVALID"):
        head.require_ready(identity, proof, str(UUID(int=6)), 101)
    with pytest.raises(ValueError, match="BINDING_INVALID"):
        head.require_ready(identity, proof, approval.budget_epoch, 220)
    with pytest.raises(ValueError, match="BINDING_INVALID"):
        head.require_ready(identity, proof, approval.budget_epoch, 102, activating=True)
    with pytest.raises(ValidationError, match="ACTIVATION_TIME"):
        ActiveRouteHead(approval=approval, activated_epoch=200)


def test_route_writer_state_requires_exact_owner_expiry_and_new_takeover_token() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    owner, publication = str(UUID(int=2)), str(UUID(int=3))
    writer = RouteWriterState(scope=scope, owner_token=owner, publication_id=publication,
                              writer_epoch=1, leased_epoch=100, lease_until_epoch=130,
                              active_generation=0, active_version=None, active_sha256=None)
    writer.require_owner(owner, publication, 129)
    receipt = RouteWriterReceipt(operation_id=str(UUID(int=6)), scope=scope, kind="claim",
                                  before_etag=None, before_sha256=None,
                                  after_sha256=document_digest(writer),
                                  owner_token=owner, writer_epoch=1)
    endpoint, table = "https://unitstore.table.core.windows.net", "TurnstileRoutes"
    content_type, body = conditional_batch(endpoint, table, scope.partition, [
        ("writer", route_entity(scope, scope.partition, "writer",
                     canonical_document(writer)), None),
        ("writer-transition:" + receipt.operation_id,
         route_entity(scope, scope.partition, "writer-transition:" + receipt.operation_id,
                      canonical_document(receipt)), None),
    ])
    intent = RouteWriterIntent(receipt, None, writer, content_type, body,
                               text_digest(body.decode()), endpoint, table)
    assert RouteWriterIntent.from_record(intent.to_record()) == intent
    repository = InMemoryRepository()
    publication_id = UUID(publication)
    repository.gateway_publications.append({
        "id": publication_id, "status": "promoting", "resource_manifest": {},
    })
    repository.gateway_publication_outbox.append({
        "publication_id": publication_id, "status": "leased", "lease_owner": "unit-worker",
        "lease_expires_at": datetime.now(UTC) + timedelta(minutes=1),
    })
    persistent = PersistentRouteWriterJournal(
        repository, publication_id, "unit-worker", "a" * 64, None,
    )
    persistent.begin_once(intent)
    persistent.record_outcome(receipt.operation_id, "unknown_no_replay")
    resumed = PersistentRouteWriterJournal(
        repository, publication_id, "unit-worker", "a" * 64, persistent.snapshot(),
    )
    assert resumed.intent == intent and resumed.outcome == "unknown_no_replay"
    with pytest.raises(ValueError, match="OPERATION_REUSED"):
        resumed.begin_once(intent)
    recovery = MagicMock(spec=TableRouteProjection)
    recovery.recover_writer.return_value = "confirmed"
    assert resumed.recover(recovery) == "confirmed"
    recovery.recover_writer.assert_called_once_with(intent)
    with pytest.raises(ValueError, match="REGRESSION"):
        resumed.record_outcome(receipt.operation_id, "unknown")
    with pytest.raises(ValueError, match="INTENT_INVALID"):
        RouteWriterIntent.from_record({**intent.to_record(), "body_sha256": "a" * 64})
    with pytest.raises(ValueError, match="OWNER_OR_LEASE"):
        writer.require_owner(owner, publication, 130)
    with pytest.raises(ValueError, match="NOT_EXPIRED"):
        writer.takeover(str(UUID(int=4)), str(UUID(int=5)), 129)
    with pytest.raises(ValueError, match="TOKEN_REUSED"):
        writer.takeover(owner, publication, 130)
    changed = writer.takeover(str(UUID(int=4)), str(UUID(int=5)), 130)
    assert changed.writer_epoch == 2 and changed.lease_until_epoch == 160
    with pytest.raises(ValidationError, match="STATE_INVALID"):
        RouteWriterState.model_validate_json(canonical_document({
            **writer.model_dump(mode="json"), "lease_until_epoch": 131,
        }))


def test_route_projection_read_binds_entity_scope_hash_and_strict_etag() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    document = canonical_document(scope)
    entity = route_entity(scope, scope.partition, "active", document)
    assert entity_document(entity, scope, scope.partition, "active", MAX_HEAD_BYTES) == document
    for field, value in (("Sha256", "a" * 64), ("SchemaVersion", True), ("RowKey", "other")):
        with pytest.raises(RouteProjectionConflict):
            entity_document(
                {**entity, field: value}, scope, scope.partition, "active", MAX_HEAD_BYTES,
            )
    token = MagicMock()
    token.token.return_value = "unit-only"
    with httpx.Client(transport=httpx.MockTransport(lambda incoming: httpx.Response(
        200, json=entity, headers={"ETag": 'W/"unit"'},
    ))) as client:
        projection = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "TurnstileRoutes", scope, token, client,
        )
        assert projection._read_document(scope.partition, "active", MAX_HEAD_BYTES) == (
            document, 'W/"unit"',
        )
        with pytest.raises(ValueError, match="SCOPE_MISMATCH"):
            projection._entity_url("unrelated-scope", "active")
        with pytest.raises(ValueError, match="ORIGIN_CHANGED"):
            projection._request("GET", "https://other.table.core.windows.net/TurnstileRoutes")


@pytest.mark.parametrize("confirmed", [False, True])
def test_route_projection_stage_requires_exact_immutable_readback(confirmed: bool) -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    document = canonical_document(scope)
    observed: dict[str, Any] | None = None
    writes = 0

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal observed, writes
        if incoming.method == "GET":
            return httpx.Response(404) if observed is None else httpx.Response(
                200, json=observed, headers={"ETag": 'W/"unit"'},
            )
        assert incoming.method == "POST"
        writes += 1
        if confirmed:
            observed = json.loads(incoming.content)
        return httpx.Response(201)

    token = MagicMock()
    token.token.return_value = "unit-only"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        projection = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "TurnstileRoutes", scope, token, client,
        )
        if confirmed:
            projection._stage_entity(scope.partition, "unit", document, MAX_HEAD_BYTES)
            projection._stage_entity(scope.partition, "unit", document, MAX_HEAD_BYTES)
            with pytest.raises(RouteProjectionConflict, match="IMMUTABLE_ENTITY_CONFLICT"):
                projection._stage_entity(scope.partition, "unit", "{}", MAX_HEAD_BYTES)
        else:
            with pytest.raises(RouteProjectionConflict, match="READBACK_DIFFERS"):
                projection._stage_entity(scope.partition, "unit", document, MAX_HEAD_BYTES)
    assert writes == 1


def test_route_conditional_batch_binds_actual_target_condition_and_entity() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    endpoint, table = "https://unitstore.table.core.windows.net", "TurnstileRoutes"
    document = canonical_document(scope)
    content_type, body = conditional_batch(endpoint, table, scope.partition, [
        ("writer", route_entity(scope, scope.partition, "writer", document), 'W/"unit"'),
    ])
    require_route_batch(endpoint, table, scope, content_type, body, [("writer", scope, 'W/"unit"')])
    for changed in (body.replace(b'W/"unit"', b"*"),
                    body.replace(b"unitstore.table.core.windows.net",
                                 b"other.table.core.windows.net"),
                    body.replace(b'"SchemaVersion":2', b'"SchemaVersion":1')):
        with pytest.raises(ValueError):
            require_route_batch(endpoint, table, scope, content_type, changed,
                                [("writer", scope, 'W/"unit"')])


def test_route_writer_cas_journals_before_dispatch_and_never_replays_unknown_http_outcome() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    journal = MagicMock()
    writes = 0

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal writes
        if incoming.method == "GET":
            return httpx.Response(404)
        assert incoming.method == "POST" and incoming.url.path == "/$batch"
        journal.begin_once.assert_called_once()
        writes += 1
        raise httpx.ReadTimeout("unit lost response", request=incoming)

    token = MagicMock()
    token.token.return_value = "unit-only"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        projection = TableRouteProjection(
            "https://unitstore.table.core.windows.net", "TurnstileRoutes", scope, token, client,
        )
        with pytest.raises(RouteProjectionUnknown, match="UNKNOWN_RECOVER_ONLY"):
            projection.claim_writer(str(UUID(int=2)), str(UUID(int=3)), str(UUID(int=4)),
                                     lambda: 100, journal)
        journal.record_outcome.assert_called_once_with(str(UUID(int=4)), "unknown_no_replay")
        intent = journal.begin_once.call_args.args[0]
        assert projection.recover_writer(intent) == "unknown"
    assert writes == 1


def test_table_program_proof_cannot_be_borrowed_from_another_instance_or_scope() -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    identity = ProgramIdentity(scope=scope, api_revision="1", instance_id="managed",
                                program_sha256="a" * 64, trust_binding_sha256="b" * 64)
    proof = ProgramCapabilityProof(proof_id=str(UUID(int=2)), identity=identity,
                                   supported_profiles=("microsoft-decision",),
                                   issued_epoch=100, expires_epoch=220)
    with httpx.Client() as client:
        projection = TableRouteProjection("https://unitstore.table.core.windows.net",
                                          "TurnstileRoutes", scope, MagicMock(), client)
        with patch.object(projection, "_read_document", return_value=(
            canonical_document(proof), 'W/"unit"',
        )):
            assert projection.read_program_proof(identity) == (proof, 'W/"unit"')
            with pytest.raises(RouteProjectionConflict, match="IDENTITY_CHANGED"):
                projection.read_program_proof(identity.model_copy(update={"instance_id": "other"}))
            with pytest.raises(RouteProjectionConflict, match="SCOPE_CHANGED"):
                projection.read_program_proof(identity.model_copy(update={"scope": scope.model_copy(
                    update={"gateway_id": "unit-edge"},
                )}))
        stored: dict[str, str] = {}
        batches = 0

        def read_document(partition: str, row: str, maximum: int) -> tuple[str, str] | None:
            document = stored.get(row)
            return (document, 'W/"unit"') if document is not None else None

        def dispatch(method: str, url: str, **kwargs: Any) -> httpx.Response:
            nonlocal batches
            if url.endswith("/$batch"):
                batches += 1
                raise httpx.ReadTimeout("unit unknown proof write")
            entity = kwargs["json"]
            stored[entity["RowKey"]] = entity["Document"]
            return httpx.Response(201)

        with patch.object(projection, "_read_document", side_effect=read_document), \
                patch.object(projection, "_request", side_effect=dispatch):
            with pytest.raises(RouteProjectionUnknown, match="UNKNOWN_RECOVER_ONLY"):
                projection.publish_program_proof(proof, identity, None, 101)
            with pytest.raises(RouteProjectionUnknown, match="UNKNOWN_RECOVER_ONLY"):
                projection.publish_program_proof(proof, identity, None, 101)
        assert batches == 1
        with patch.object(projection, "_read_document", return_value=None), \
                patch.object(projection, "read_active", return_value=(MagicMock(), 'W/"unit"')), \
                patch.object(projection, "_request") as request:
            with pytest.raises(RouteProjectionConflict, match="CANNOT_REPLACE_HEAD"):
                projection.initialize_empty_scope(identity, proof, str(UUID(int=3)),
                                                    str(UUID(int=4)), lambda: 101)
            request.assert_not_called()


def test_native_decision_publication_target_retains_resource_protocol_and_mi_audience() -> None:
    provider = {"brand_key": BrandKey.MICROSOFT_FOUNDRY}
    runtime = {
        "config": {"project_endpoint": "https://unit-decision.services.ai.azure.com/api/projects/unit"},
        "auth_strategy": AuthStrategy.MANAGED_IDENTITY,
        "managed_identity_resource": "https://ai.azure.com",
    }
    model = GatewayControlPlaneService._release_model(
        ModelCreateTarget(operation="decision", deployment_name="unit-decision"), provider, runtime,
    )
    assert model.capabilities == ["decision"] and model.assignment_required
    bound = GatewayControlPlaneService._model_binding_runtime(provider, runtime, model)
    assert bound["api_format"] == ApiFormat.MICROSOFT_DECISION
    assert bound["backend_url"] == "https://unit-decision.services.ai.azure.com"
    assert bound["backend_path"] == "/providers/microsoft/v1/systemone"
    assert bound["streaming_mode"] == "buffered"
    assert bound["managed_identity_resource"] == "https://cognitiveservices.azure.com"


@pytest.mark.parametrize("endpoint", [
    "http://unit.services.ai.azure.com", "https://unit.example.test",
    "https://user:secret@unit.services.ai.azure.com",
    "https://unit.services.ai.azure.com:443", "https://unit.services.ai.azure.com?token=unit",
    "https://unit.services.ai.azure.com#fragment",
])
def test_native_decision_publication_rejects_nonresource_endpoint(endpoint: str) -> None:
    with pytest.raises(ControlPlaneConflictError, match="resource endpoint"):
        GatewayControlPlaneService._decision_binding_values({"backend_url": endpoint})


@pytest.mark.parametrize("custom_rule", [False, True])
def test_context_pricing_publication_and_activation_keep_the_complete_schedule(
    custom_rule: bool,
) -> None:
    from turnstile_core.domain.runtime_models import PriceCatalogContextTier, PriceCatalogInputRange
    from turnstile_core.pricing.catalog import CatalogModel, CatalogOption, CatalogOptions

    repository = InMemoryRepository()
    catalog = MagicMock(spec=CompositeCatalog)
    entries = {}
    references = []
    for index in range(2):
        label = f"Opaque tier {index}"
        candidate = CatalogModel(
            key=f"azure_retail:Unit:{label}", label=label, product="Unit",
            source=PriceSource.AZURE_RETAIL,
            context_tier=PriceCatalogContextTier(
                group_key="arbitrary-model", model_label="Arbitrary model",
                key=("short" if index == 0 else "long") if custom_rule else str(index),
                label=label, order=index,
                input_range=None if custom_rule else PriceCatalogInputRange(
                    min_input_tokens=0 if index == 0 else 1201,
                    max_input_tokens=1200 if index == 0 else None,
                ),
            ),
        )
        reference = f"{candidate.key}:Global:*"
        references.append(reference)
        rate = CatalogEntry(
            reference=reference, label=label, source=candidate.source,
            input_per_million=2 + index, output_per_million=8 + index,
        )
        entries[candidate.key] = CatalogOptions(model=candidate, options=(CatalogOption(
            reference=reference, deployment="Global", entry=rate, regions=("eastus",),
            region_required=False,
        ),))
    catalog.options.side_effect = entries.get
    write = foundry_publication("unit-context-model")
    write.provider = ProviderTarget(existing_id=next(
        item["id"] for item in repository.providers if item["brand_key"] == "microsoft_foundry"
    ))
    write.model = ModelCreateTarget.model_validate(write.model.model_dump() | {
        "price_source": "azure_retail", "context_price_references": references,
        "price_discount_percent": 80, "input_cost_per_million": 999,
        **({"context_price_rule": {"short_context_max_tokens": 1200}} if custom_rule else {}),
    })
    service = GatewayControlPlaneService(
        repository, apim_principal_id=str(UUID(int=99)), price_catalog=catalog,
    )
    publication = service.publish(write, "unit-owner")
    model = publication.desired_spec.bindings[-1].model
    assert [tier.input_price_per_million for tier in model.context_price_tiers] == [1.6, 2.4]
    assert model.input_cost_per_million is None
    if custom_rule:
        assert model.context_price_rule is not None
        assert model.context_price_rule.short_context_max_tokens == 1200
        source = model.context_price_tiers[0].input_range_source
        assert source is not None and source.origin == "custom"
    assert service.publish(write, "unit-owner").id == publication.id
    for before, after in zip(
        ("queued", "validating", "provisioning", "building_revision", "verifying"),
        ("validating", "provisioning", "building_revision", "verifying", "promoting"), strict=True,
    ):
        transition_publication(repository, publication.id, before, after, {}, "context-worker")
    activate_publication(repository, publication.id, "context-worker")
    stored = next(
        row for row in repository.registry()["models"] if row["model_key"] == model.model_key
    )
    assert stored["context_price_tiers"] == [
        tier.model_dump(mode="json") for tier in model.context_price_tiers
    ]
    price = repository.model_prices()[str(stored["id"])].for_input_tokens(1201)
    assert price.input_price_per_million == 2.4


def test_manual_publication_model_preserves_legacy_serialization() -> None:
    model = ModelTarget(
        model_key="unit", display_name="Unit", upstream_model_id="unit", capabilities=["chat"],
    )
    payload = model.model_dump(mode="json")
    assert not any(key.startswith(("price_", "list_")) for key in payload)
    assert ModelTarget.model_validate(payload).model_dump(mode="json") == payload


@pytest.mark.parametrize("connection_discount,discount,expected", [
    (None, None, 2), (90, None, 1.8), (90, 80, 1.6),
])
def test_publication_uses_catalog_prices_not_submitted_amounts(
    connection_discount: float | None, discount: float | None, expected: float,
) -> None:
    repository = InMemoryRepository()
    catalog = MagicMock(spec=CompositeCatalog)
    reference = "azure_retail:Azure OpenAI:gpt 4.1:Global:*"
    catalog.lookup.return_value = CatalogEntry(
        reference=reference, label="gpt 4.1", source=PriceSource.AZURE_RETAIL,
        input_per_million=2, output_per_million=8, cached_per_million=0.5,
    )
    write = foundry_publication("gpt-4.1-845778")
    write.provider = ProviderTarget(existing_id=next(
        item["id"] for item in repository.providers if item["brand_key"] == "microsoft_foundry"
    ))
    if connection_discount is not None:
        registry = ModelRuntimeService(
            repository, Settings(apim_principal_id=str(UUID(int=99))),
        ).save_connection(
            ModelConnectionCreate.model_validate({
                "gateway_profile_id": APIM_ID,
                "provider": write.provider.model_dump(),
                "auth_mode": "managed_identity",
                "foundry_project_endpoint": str(write.runtime.foundry_project_endpoint),
            })
        )
        runtime = next(item for item in registry.runtimes if item.config.get("project_endpoint"))
        repository.update_registry_item("runtime", runtime.id, {
            "price_discount_percent": connection_discount,
        })
        write.runtime = RuntimeTarget(existing_id=runtime.id)
    write.model = ModelCreateTarget.model_validate({
        **write.model.model_dump(), "price_source": "azure_retail",
        "price_reference": reference, "price_discount_percent": discount,
        "input_cost_per_million": 999, "output_cost_per_million": 999,
    })
    service = GatewayControlPlaneService(
        repository, apim_principal_id=str(UUID(int=99)), price_catalog=catalog,
    )
    publication = service.publish(write, "owner@example.com")
    model = publication.desired_spec.bindings[-1].model
    assert model.input_cost_per_million == expected
    assert model.output_cost_per_million == expected * 4
    assert model.list_input_cost_per_million == 2
    assert model.price_reference == reference
    assert model.price_discount_percent == discount
    catalog.lookup.assert_called_once_with(reference)
    repeated = service.publish(write, "owner@example.com")
    assert repeated.id == publication.id
    assert repeated.desired_spec_sha256 == publication.desired_spec_sha256
    for before, after in zip(
        ("queued", "validating", "provisioning", "building_revision", "verifying"),
        ("validating", "provisioning", "building_revision", "verifying", "promoting"),
        strict=True,
    ):
        transition_publication(repository, publication.id, before, after, {}, "price-worker")
    activate_publication(repository, publication.id, "price-worker")
    saved = next(
        item for item in repository.registry()["models"] if item["model_key"] == model.model_key
    )
    assert saved["price_source"] == "azure_retail"
    assert saved["price_reference"] == reference
    assert saved["price_discount_percent"] == discount
    assert saved["effective_discount_percent"] == (discount or connection_discount)
    assert saved["input_cost_per_million"] == expected
    assert saved["list_input_cost_per_million"] == 2
    assert saved["price_sync_status"] == "ok"
    assert saved["price_synced_at"] == publication.created_at


def test_manually_priced_models_report_no_effective_discount() -> None:
    # A connection discount is never applied to a hand-entered price, so reporting one
    # for that model contradicts what is actually charged.
    repository = InMemoryRepository()
    manual, retail = repository.models[:2]
    runtime = next(row for row in repository.runtimes if row["id"] == manual["runtime_id"])
    runtime["price_discount_percent"] = 30
    retail["runtime_id"] = manual["runtime_id"]
    manual.update(price_source="manual", price_discount_percent=None)
    retail.update(price_source="azure_retail", price_discount_percent=None)

    saved = {row["id"]: row for row in repository.registry()["models"]}

    assert saved[manual["id"]]["effective_discount_percent"] is None
    assert saved[retail["id"]]["effective_discount_percent"] == 30


@pytest.mark.parametrize("failure", ["missing", "partial", "no-output", "unavailable"])
def test_publication_rejects_unusable_catalog_without_queueing(failure: str) -> None:
    repository = InMemoryRepository()
    catalog = MagicMock(spec=CompositeCatalog)
    reference = "azure_retail:Azure OpenAI:gpt 4.1:Global:*"
    catalog.lookup.return_value = None if failure == "missing" else CatalogEntry(
        reference=reference, label="gpt 4.1", source=PriceSource.AZURE_RETAIL,
        input_per_million=2, output_per_million=None if failure == "no-output" else 8,
        complete=failure != "partial",
    )
    if failure == "unavailable":
        catalog.lookup.side_effect = TimeoutError("catalog read timed out")
    write = foundry_publication()
    write.provider = ProviderTarget(existing_id=next(
        item["id"] for item in repository.providers if item["brand_key"] == "microsoft_foundry"
    ))
    write.model = ModelCreateTarget.model_validate({
        **write.model.model_dump(), "price_source": "azure_retail", "price_reference": reference,
    })
    expected = (
        ControlPlaneUnavailableError if failure == "unavailable" else ControlPlaneConflictError
    )
    with pytest.raises(expected, match="catalog price|price source"):
        GatewayControlPlaneService(
            repository, apim_principal_id=str(UUID(int=99)), price_catalog=catalog,
        ).publish(write, "owner@example.com")
    catalog.lookup.assert_called_once_with(reference)
    assert repository.gateway_publications == []


def test_openai_existing_connection_publishes_with_a_one_time_encrypted_key(tmp_path: Path) -> None:
    repository = InMemoryRepository()
    registry = ModelRuntimeService(
        repository, Settings(credential_key_file=tmp_path / "key")
    ).save_connection(ModelConnectionCreate(
        gateway_profile_id=APIM_ID,
        provider=ProviderTarget(template="openai_compatible"),
        openai_base_url=HttpUrl("https://api.example.test/v1"),
    ))
    runtime = next(
        item for item in registry.runtimes if item.config.get("base_url") == "https://api.example.test/v1"
    )
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository, cipher)
    request = GatewayPublicationCreate(
        gateway_profile_id=APIM_ID,
        provider=ProviderTarget(existing_id=runtime.provider_id),
        runtime=RuntimeTarget(existing_id=runtime.id),
        model=ModelCreateTarget(
            model_key="compatible-chat", display_name="Compatible chat", upstream_model_id="chat-v1"
        ),
    )
    with pytest.raises(ControlPlaneConflictError, match="one-time API key"):
        service.publish(request, "owner@example.com")
    request.runtime.api_key = SecretStr("compatible-test-key")
    publication = service.publish(request, "owner@example.com")
    binding = publication.desired_spec.bindings[-1]
    encrypted = repository.gateway_publication_credential(publication.id)

    assert binding.runtime_id == runtime.id
    assert binding.api_format is ApiFormat.OPENAI_CHAT
    assert binding.backend_path == "/v1/chat/completions"
    assert binding.auth_strategy is AuthStrategy.NAMED_VALUE_BEARER
    assert binding.runtime_config["max_tokens_field"] == "max_tokens"
    assert encrypted is not None and cipher.decrypt(encrypted) == "compatible-test-key"
    assert "compatible-test-key" not in publication.model_dump_json()
    policy = ApimPolicyCompiler().compile(publication).chat_completions_policy
    assert "compatible-test-key" not in policy


def test_openai_direct_publication_uses_the_same_https_connection_rules() -> None:
    repository = InMemoryRepository()
    request = GatewayPublicationCreate(
        gateway_profile_id=APIM_ID,
        provider=ProviderTarget(template="openai_compatible"),
        runtime=RuntimeTarget(
            openai_base_url=HttpUrl("https://api.example.test/v1/chat/completions"),
            api_key=SecretStr("compatible-test-key"),
        ),
        model=ModelCreateTarget(
            model_key="compatible-chat", display_name="Compatible chat", upstream_model_id="chat-v1"
        ),
    )
    binding = GatewayControlPlaneService(repository).publish(
        request, "owner@example.com"
    ).desired_spec.bindings[-1]
    assert str(binding.backend_url).rstrip("/") == "https://api.example.test"
    assert binding.backend_path == "/v1/chat/completions"
    assert binding.named_value_name is not None
    assert binding.named_value_name.startswith("turnstile-openai-")


@pytest.mark.parametrize("direct_publication", [False, True])
def test_openai_credential_names_are_isolated_and_persisted(direct_publication: bool) -> None:
    names = []
    for index in range(2):
        repository = InMemoryRepository()
        gateway_id = UUID(int=900 + index)
        repository.gateways.append({
            **next(item for item in repository.gateways if item["id"] == APIM_ID),
            "id": gateway_id, "name": f"Credential scope {index}", "is_default": False,
        })
        service = GatewayControlPlaneService(repository)
        if direct_publication:
            request = GatewayPublicationCreate(
                gateway_profile_id=gateway_id,
                provider=ProviderTarget(template="openai_compatible"),
                runtime=RuntimeTarget(
                    openai_base_url=HttpUrl("https://shared.vendor.test/v1"),
                    api_key=SecretStr("unit-key"),
                ),
                model=ModelCreateTarget(
                    model_key="shared-model", display_name="Shared model",
                    upstream_model_id="chat-v1",
                ),
            )
            publication = service.publish(request, "owner@example.com")
            name = publication.desired_spec.bindings[-1].named_value_name
            transition_publication(
                repository, publication.id, "queued", "failed", {}, "unit-worker"
            )
            retried = service.retry(
                publication.id,
                GatewayPublicationRetry(api_key=SecretStr("unit-replacement-key")),
                "owner@example.com",
            )
            assert retried.desired_spec.bindings[-1].named_value_name == name
        else:
            registry = ModelRuntimeService(
                repository, Settings(apim_api_id=f"release-{index}-llm")
            ).save_connection(ModelConnectionCreate(
                gateway_profile_id=gateway_id,
                provider=ProviderTarget(template="openai_compatible"),
                openai_base_url=HttpUrl("https://shared.vendor.test/v1"),
            ))
            runtime = next(
                item for item in registry.runtimes
                if item.config.get("base_url") == "https://shared.vendor.test/v1"
            )
            name = runtime.config["named_value_name"]
            request = GatewayPublicationCreate(
                gateway_profile_id=gateway_id,
                provider=ProviderTarget(existing_id=runtime.provider_id),
                runtime=RuntimeTarget(existing_id=runtime.id, api_key=SecretStr("unit-key")),
                model=ModelCreateTarget(
                    model_key="shared-model", display_name="Shared model",
                    upstream_model_id="chat-v1",
                ),
            )
            publication = service.publish(request, "owner@example.com")
            assert publication.desired_spec.bindings[-1].named_value_name == name
        assert isinstance(name, str) and name.startswith("turnstile-openai-")
        names.append(name)
    assert names[0] != names[1]


@pytest.mark.parametrize("vendor", list(ModelVendorKey))
def test_openai_direct_publication_preserves_vendor(vendor: ModelVendorKey) -> None:
    repository = InMemoryRepository()
    request = GatewayPublicationCreate(
        gateway_profile_id=APIM_ID,
        provider=ProviderTarget(template="openai_compatible"),
        runtime=RuntimeTarget(
            openai_base_url=HttpUrl("https://vendor.example.test/v1"),
            api_key=SecretStr("unit-vendor-key"),
            model_vendor=vendor,
        ),
        model=ModelCreateTarget(
            model_key="vendor-chat", display_name="Vendor chat", upstream_model_id="chat-v1"
        ),
    )
    service = GatewayControlPlaneService(repository)
    binding = service.publish(request, "owner@example.com").desired_spec.bindings[-1]
    assert binding.provider_name == model_vendor_label(vendor)
    assert binding.provider_config["model_vendor"] == vendor.value
    assert binding.runtime_config["model_vendor"] == vendor.value
    assert model_vendor_label(vendor) in binding.runtime_name


def test_openai_direct_publication_reuses_vendor_provider_and_rejects_mismatch() -> None:
    repository = InMemoryRepository()
    connection = ModelRuntimeService(repository, Settings()).save_connection(
        ModelConnectionCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(template="openai_compatible"),
            openai_base_url=HttpUrl("https://existing.vendor.test/v1"),
            model_vendor=ModelVendorKey.DEEPSEEK,
        )
    )
    provider = next(item for item in connection.providers if item.name == "DeepSeek")
    request = GatewayPublicationCreate(
        gateway_profile_id=APIM_ID,
        provider=ProviderTarget(template="openai_compatible"),
        runtime=RuntimeTarget(
            openai_base_url=HttpUrl("https://new.vendor.test/v1"),
            api_key=SecretStr("unit-vendor-key"), model_vendor=ModelVendorKey.DEEPSEEK,
        ),
        model=ModelCreateTarget(
            model_key="vendor-chat", display_name="Vendor chat", upstream_model_id="chat-v1"
        ),
    )
    service = GatewayControlPlaneService(repository)
    assert service._resolve_provider(request, repository.providers)["id"] == provider.id
    request.provider = ProviderTarget(existing_id=provider.id)
    request.runtime.model_vendor = ModelVendorKey.OPENAI
    with pytest.raises(ControlPlaneConflictError, match="vendor does not match"):
        service.publish(request, "owner@example.com")
    assert repository.gateway_publications == []


@pytest.mark.parametrize("runtime", [
    {"existing_id": str(APIM_ID)},
    {"foundry_project_endpoint": "https://unit.services.ai.azure.com/api/projects/unit"},
    {
        "bedrock_runtime_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "api_key": "unit-key",
    },
])
def test_runtime_vendor_cannot_redefine_existing_or_other_provider(runtime: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="redefine|only to a new OpenAI"):
        RuntimeTarget.model_validate({**runtime, "model_vendor": "deepseek"})


@pytest.mark.parametrize("field,value", (
    ("backend_url", "http://api.example.test"),
    ("api_format", "anthropic_messages"),
    ("auth_strategy", "none"),
    ("routing_managed", False),
    ("backend_path", ""),
    ("streaming_mode", "buffered"),
))
def test_openai_new_runtime_validation_remains_strict(field: str, value: object) -> None:
    runtime: dict[str, object] = {
        "id": None,
        "backend_url": "https://api.example.test",
        "api_format": "openai_chat",
        "auth_strategy": "named_value_bearer",
        "routing_managed": True,
        "backend_path": "/v1/chat/completions",
        "streaming_mode": "native",
    }
    runtime[field] = value
    with pytest.raises(ControlPlaneConflictError):
        GatewayControlPlaneService._validate_connection(
            {"provider_kind": "openai_compatible", "brand_key": "generic"}, runtime
        )


def test_enabled_control_plane_requires_usage_observer() -> None:
    with pytest.raises(ValueError, match="usage observer"):
        Settings(control_plane_enabled=True)

    settings = Settings(
        control_plane_enabled=True,
        apim_usage_observer_url="https://observer.example.com",
        apim_usage_observer_key_named_value="turnstile-envoy-adapter-key",
    )

    assert settings.apim_usage_observer_url == "https://observer.example.com"

def test_publication_authorization_requires_owner_session_and_allowed_origin() -> None:
    settings = Settings(database_url="postgresql://unused")

    assert require_publication_owner(
        publication_request(), PublicationAuthStore("owner"), settings  # type: ignore[arg-type]
    ) == "owner@example.com"
    with pytest.raises(HTTPException) as member_error:
        require_publication_owner(
            publication_request(),
            PublicationAuthStore("member"),  # type: ignore[arg-type]
            settings,
        )
    assert member_error.value.status_code == 403
    with pytest.raises(HTTPException) as origin_error:
        require_publication_owner(
            publication_request("https://attacker.example"),
            PublicationAuthStore("owner"),  # type: ignore[arg-type]
            settings,
        )
    assert origin_error.value.status_code == 403

def test_foundry_api_key_contract_rejects_incomplete_credentials() -> None:
    with pytest.raises(ValueError, match="inference endpoint and API key"):
        RuntimeTarget(
            foundry_project_endpoint=HttpUrl(
                "https://unit-foundry-account.services.ai.azure.com/api/projects/unit-foundry-project"
            ),
            foundry_inference_endpoint=HttpUrl(
                "https://unit-foundry-account.openai.azure.com/openai/v1"
            ),
        )

def test_foundry_managed_identity_contract_needs_only_project_endpoint() -> None:
    target = RuntimeTarget(
        foundry_project_endpoint=HttpUrl(
            "https://contoso-ai.services.ai.azure.com/api/projects/finops"
        ),
    )

    assert target.foundry_inference_endpoint is None
    assert target.api_key is None

def test_foundry_api_key_contract_rejects_mismatched_resource_endpoint() -> None:
    repository = InMemoryRepository()
    request = external_tenant_foundry_publication(repository)
    request.runtime.foundry_inference_endpoint = HttpUrl(
        "https://attacker-resource.openai.azure.com/openai/v1"
    )

    with pytest.raises(ControlPlaneConflictError, match="same account"):
        GatewayControlPlaneService(repository).publish(request, "owner@example.com")

def test_foundry_project_connection_derives_a_managed_identity_binding() -> None:
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
    request = foundry_publication()
    assert not hasattr(request.model, "capabilities")
    publication = GatewayControlPlaneService(
        repository,
        apim_principal_id="00000000-0000-0000-0000-000000000705",
    ).publish(request, "owner@example.com")
    binding = publication.desired_spec.bindings[-1]

    assert str(binding.backend_url).rstrip("/") == (
        "https://contoso-ai.services.ai.azure.com/api/projects/finops"
    )
    assert binding.backend_path == "/openai/v1/chat/completions"
    assert binding.auth_strategy.value == "managed_identity"
    assert binding.managed_identity_resource == "https://ai.azure.com"
    assert binding.model.upstream_model_id == "gpt-5-mini-deployment"
    assert binding.model.display_name == "gpt-5-mini-deployment · Microsoft Foundry"
    assert binding.model.model_key.startswith("gpt-5-mini-deployment-foundry-")
    assert binding.model.capabilities == ["chat", "streaming"]
    authorization = binding.runtime_config["authorization"]
    assert isinstance(authorization, dict)
    assert authorization["principal_id"] == (
        "00000000-0000-0000-0000-000000000705"
    )
    assert authorization["role_id"] == (
        "a97b65f3-24c7-4388-baec-2e87135dc908"
    )
    assert authorization["resource_endpoint"] == (
        "https://contoso-ai.services.ai.azure.com"
    )

def test_existing_foundry_claude_uses_anthropic_messages_binding() -> None:
    repository = InMemoryRepository()
    provider = next(
        item
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    runtime = next(
        item for item in repository.runtimes if item["provider_id"] == provider["id"]
    )
    runtime["name"] = "Microsoft Foundry example-project via APIM"
    runtime["config"].update(
        control_plane_managed=True,
        project_endpoint=(
            "https://example-foundry-resource.services.ai.azure.com/"
            "api/projects/example-project"
        ),
        backend_url=(
            "https://example-foundry-resource.services.ai.azure.com/"
            "api/projects/example-project"
        ),
        auth_strategy="managed_identity",
        managed_identity_resource="https://ai.azure.com",
        streaming_mode="native",
    )
    request = GatewayPublicationCreate(
        gateway_profile_id=APIM_ID,
        provider=ProviderTarget(existing_id=provider["id"]),
        runtime=RuntimeTarget(existing_id=runtime["id"]),
                model=ModelCreateTarget(deployment_name="claude-opus-5"),
    )

    publication = GatewayControlPlaneService(repository).publish(
        request, "owner@example.com"
    )
    binding = publication.desired_spec.bindings[-1]
    compiled = ApimPolicyCompiler().compile(publication)

    assert binding.api_format is ApiFormat.ANTHROPIC_MESSAGES
    assert binding.model.capabilities == [
        "chat",
        "streaming",
    ]
    assert str(binding.backend_url).rstrip("/") == (
        "https://example-foundry-resource.services.ai.azure.com"
    )
    assert binding.backend_path == "/anthropic/v1/messages"
    assert binding.runtime_config["api_format"] == "openai_chat"
    assert binding.model.model_key not in compiled.chat_completions_policy
    assert binding.model.model_key in compiled.messages_policy
    assert 'template="/anthropic/v1/messages"' in compiled.messages_policy
    assert 'name="anthropic-version" exists-action="override"' in compiled.messages_policy
    assert (
        'body[&quot;model&quot;] = &quot;claude-opus-5&quot;'
        in compiled.messages_policy
    )

def test_compiler_emits_native_apim_pool_breakers_retry_and_probe_routes() -> None:
    repository = InMemoryRepository()
    provider = next(
        item
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    runtime = next(
        item for item in repository.runtimes if item["provider_id"] == provider["id"]
    )
    primary_url = "https://primary.services.ai.azure.com/openai/v1"
    secondary_id = UUID("30000000-0000-4000-8000-000000000099")
    runtime["config"].update(
        control_plane_managed=True,
        project_endpoint="https://primary.services.ai.azure.com/api/projects/main",
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
            model=ModelCreateTarget(deployment_name="gpt-5.6-pool"),
        ),
        "owner@example.com",
    )
    binding = publication.desired_spec.bindings[-1]
    rate_limit = GatewayRateLimitResilience(
        max_attempts_per_request=2,
        retry_interval_seconds=1,
        first_fast_retry=True,
        circuit_breaker=GatewayRateLimitCircuitBreaker(
            failure_count=1,
            interval_seconds=60,
            trip_duration_seconds=60,
            accept_retry_after=True,
        ),
    )
    pool = GatewayBackendPoolConfig(
        members=[
            GatewayBackendPoolMember(
                runtime_id=runtime["id"],
                runtime_name=runtime["name"],
                backend_url=HttpUrl(primary_url),
                auth_strategy=AuthStrategy.MANAGED_IDENTITY,
                priority=0,
                weight=3,
            ),
            GatewayBackendPoolMember(
                runtime_id=secondary_id,
                runtime_name="Secondary Foundry runtime",
                backend_url=HttpUrl(
                    "https://secondary.services.ai.azure.com/openai/v1"
                ),
                auth_strategy=AuthStrategy.NAMED_VALUE_API_KEY,
                named_value_name="secondary-foundry-key",
                priority=0,
                weight=1,
            ),
        ],
        rate_limit=rate_limit,
    )
    pooled_binding = binding.model_copy(
        update={
            "runtime_config": {
                **binding.runtime_config,
                "apim_backend_pool": pool.model_dump(mode="json"),
            }
        }
    )
    pooled_publication = publication.model_copy(
        update={
            "desired_spec": publication.desired_spec.model_copy(
                update={"bindings": [pooled_binding]}
            )
        }
    )

    compiled = ApimPolicyCompiler(
        usage_observer_url="https://observer.example.com",
        usage_observer_key_named_value="turnstile-envoy-adapter-key",
    ).compile(pooled_publication)

    assert len(compiled.backends) == 3
    members = [
        item
        for item in compiled.backends
        if not isinstance(item, BackendPoolResource)
    ]
    pool_resource = next(
        item for item in compiled.backends if isinstance(item, BackendPoolResource)
    )
    assert len(members) == 2
    assert [member.priority for member in pool_resource.members] == [0, 0]
    assert [member.weight for member in pool_resource.members] == [3, 1]
    assert {member.backend_id for member in pool_resource.members} == {
        member.id for member in members
    }
    assert all(member.circuit_breaker is not None for member in members)
    assert {
        dict(member.headers)["x-turnstile-pool-runtime"] for member in members
    } == {runtime["name"], "Secondary Foundry runtime"}
    assert {
        dict(member.headers)["x-hive-runtime"] for member in members
    } == {runtime["name"], "Secondary Foundry runtime"}
    secondary = next(
        member for member in members if member.title.endswith("Secondary Foundry runtime")
    )
    assert dict(secondary.headers)["api-key"] == "{{secondary-foundry-key}}"
    primary_member = next(member for member in members if member is not secondary)
    assert "api-key" not in dict(primary_member.headers)
    assert {item.id for item in compiled.named_values} == {"secondary-foundry-key"}
    breaker = members[0].circuit_breaker
    assert breaker is not None
    assert breaker.failure_count == 1
    assert breaker.accept_retry_after is True
    assert breaker.status_code_ranges == ((429, 429), (408, 408), (500, 599))
    assert breaker.error_reasons == ("BackendConnectionFailure", "Timeout")
    policy = compiled.chat_completions_policy
    assert f'backend-id="{pool_resource.id}"' in policy
    assert all(f'backend-id="{member.id}"' in policy for member in members)
    assert 'name="selectedPoolProbeBackend"' in policy
    assert '&quot;x-turnstile-pool-member&quot;' in policy
    assert (
        '<retry condition="@(context.Response != null &amp;&amp; '
        'context.Response.StatusCode == 429)"' in policy
    )
    assert 'count="1"' in policy
    assert 'first-fast-retry="true"' in policy
    assert (
        '<forward-request timeout="120" buffer-request-body="true" '
        'buffer-response="false" />'
        in policy
    )
    retry_block = policy[policy.index("<retry condition=") : policy.index("</retry>")]
    assert "StatusCode == 429" in retry_block
    assert "StatusCode &gt;= 500" not in retry_block
    assert "BackendConnectionFailure" not in retry_block
    assert "Timeout" not in retry_block
    ElementTree.fromstring(policy)
    _assert_pool_retries_every_request_and_hides_rate_limit_details(
        ElementTree.fromstring(policy), "./backend/choose/when",
    )


def _assert_pool_retries_every_request_and_hides_rate_limit_details(
    root: ElementTree.Element, pooled_branch: str,
) -> None:
    pooled = root.find(pooled_branch)
    backend = root.find("./backend")
    assert pooled is not None and backend is not None
    # Requests with and without an execution receipt take the same retrying forward.
    assert "executionReceiptHeader" not in ElementTree.tostring(backend, encoding="unicode")
    forwards = pooled.findall(".//forward-request")
    assert len(forwards) == 1
    assert pooled.find("./retry/forward-request") is forwards[0]
    retry = pooled.find("./retry")
    assert retry is not None
    # Headers that only some members set must not reach the member chosen for the retry.
    assert [(child.tag, child.get("name"), child.get("exists-action")) for child in retry] == [
        ("set-header", "x-turnstile-pool-deployment", "delete"),
        ("set-header", "api-key", "delete"),
        ("set-header", "x-api-key", "delete"),
        ("forward-request", None, None),
    ]
    assert retry.get("condition") == (
        "@(context.Response != null && context.Response.StatusCode == 429)"
    )
    assert (retry.get("count"), retry.get("first-fast-retry")) == ("1", "true")
    limited = [
        when for when in root.findall("./outbound/choose/when")
        if "context.Response.StatusCode == 429" in when.get("condition", "")
    ]
    assert len(limited) == 1
    assert [header.get("name") for header in limited[0].findall("set-header")] == [
        "Content-Type",
    ]
    body = json.loads(limited[0].findtext("set-body") or "")
    assert body["error"]["type"] == "rate_limit_error"
    assert body["error"]["code"] == "rate_limit_exceeded"


def test_fixed_pool_retries_budgeted_requests_and_hides_member_rate_limits() -> None:
    from turnstile_core.integrations.apim_storage_routes import (
        FIXED_OPERATIONS,
        FixedRouteBinding,
        fixed_operation,
    )

    binding = FixedRouteBinding(
        scope=RouteScope(
            target_id=str(APIM_ID), api_id="finops-foundry-llm", gateway_id="managed",
        ),
        api_revision="fixed-abi2-60e4f760",
        table_endpoint="https://unitstore.table.core.windows.net",
        table_name="Routes",
        ledger_table_endpoint="https://unitstore.table.core.windows.net",
        ledger_table_name="TurnstileLedger",
        trust_binding_sha256="a" * 64,
        supported_profiles=("openai-v1", "anthropic-messages", "microsoft-decision"),
        pool_enabled=True,
        decision_enabled=True,
    )
    for operation in (name for name in FIXED_OPERATIONS if name != "images-generations"):
        _assert_pool_retries_every_request_and_hides_rate_limit_details(
            ElementTree.fromstring(fixed_operation(operation, binding)),
            "./backend/choose/when",
        )
    unpooled = fixed_operation(
        "chat-completions", binding.model_copy(update={"pool_enabled": False}),
    )
    assert "<retry" not in unpooled
    assert "StatusCode == 429" not in unpooled


def test_native_stream_pool_retry_only_deletes_headers_before_forwarding() -> None:
    def policy(step: str) -> str:
        return (
            '<policies><inbound><base /></inbound><backend><retry condition="@(true)" count="1"'
            f' interval="1">{step}<forward-request buffer-response="false" /></retry></backend>'
            "<outbound><base /></outbound><on-error><base /></on-error></policies>"
        )

    deleted = policy('<set-header name="api-key" exists-action="delete" />')
    assert validate_native_stream_policy(deleted, "chat-completions") == deleted
    for step in (
        '<set-header name="api-key" exists-action="override"><value>k</value></set-header>',
        '<set-variable name="unit" value="1" />',
    ):
        with pytest.raises(PolicyCompilationError, match="explicit and unbuffered"):
            validate_native_stream_policy(policy(step), "chat-completions")

def test_backend_pool_accepts_distinct_deployments_in_one_runtime() -> None:
    runtime_id = UUID("30000000-0000-4000-8000-000000000001")
    model_ids = [
        UUID("40000000-0000-4000-8000-000000000091"),
        UUID("40000000-0000-4000-8000-000000000092"),
    ]
    pool = GatewayBackendPoolWrite.model_validate({
        "members": [
            {"runtime_id": runtime_id, "model_id": model_id, "priority": 0, "weight": 1}
            for model_id in model_ids
        ],
        "rate_limit": {
            "max_attempts_per_request": 2,
            "retry_interval_seconds": 1,
            "first_fast_retry": True,
            "circuit_breaker": {
                "failure_count": 1,
                "interval_seconds": 60,
                "trip_duration_seconds": 60,
                "accept_retry_after": True,
            },
        },
    })

    assert [member.model_id for member in pool.members] == model_ids
    assert {member.runtime_id for member in pool.members} == {runtime_id}


@pytest.mark.parametrize("invalid", [
    None, "disabled", "runtime", "provider", "image", "decision", "embeddings",
    "family", "protocol", "unrelated-model", "version-lookalike", "pool-capability",
])
@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("deployment_name", ["gpt-5-unit-chat", "unit-future-chat"])
def test_backend_pool_resolves_registered_deployments_in_one_runtime(
    invalid: str | None, independent: bool, deployment_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryRepository()
    provider = next(
        item for item in repository.providers if item["brand_key"] == "microsoft_foundry"
    )
    runtime = next(item for item in repository.runtimes if item["provider_id"] == provider["id"])
    runtime["config"].update(
        control_plane_managed=True,
        project_endpoint="https://pool-deployments.services.ai.azure.com/api/projects/main",
        backend_url="https://pool-deployments.services.ai.azure.com/api/projects/main",
        backend_path="/openai/v1/chat/completions",
        auth_strategy="managed_identity",
        managed_identity_resource="https://ai.azure.com",
        streaming_mode="native",
    )
    service = GatewayControlPlaneService(repository)
    publication = service.publish(GatewayPublicationCreate(
        gateway_profile_id=APIM_ID,
        provider=ProviderTarget(existing_id=provider["id"]),
        runtime=RuntimeTarget(existing_id=runtime["id"]),
        model=ModelCreateTarget(deployment_name=deployment_name),
    ), "owner@example.com")
    binding = publication.desired_spec.bindings[-1]
    model = {**binding.model.model_dump(mode="python"), "id": UUID(int=901),
             "runtime_id": runtime["id"], "provider_id": provider["id"], "enabled": True}
    replica_name = f"{deployment_name}-2"
    replica = {**model, "id": UUID(int=902), "model_key": "secondary-public",
               "upstream_model_id": replica_name}
    if invalid == "disabled":
        replica["enabled"] = False
    elif invalid == "runtime":
        replica["runtime_id"] = UUID(int=903)
    elif invalid == "provider":
        replica["provider_id"] = UUID(int=904)
    elif invalid == "image":
        replica["capabilities"] = ["image_generation"]
    elif invalid == "decision":
        replica["capabilities"] = ["decision"]
    elif invalid == "embeddings":
        replica["capabilities"] = ["embeddings"]
    elif invalid == "family":
        replica["family_key"] = "claude"
    elif invalid == "protocol":
        runtime["config"]["api_format"] = "anthropic_messages"
    elif invalid == "unrelated-model":
        replica["upstream_model_id"] = "unrelated-unit-deployment"
    elif invalid == "version-lookalike":
        replica["upstream_model_id"] = f"{deployment_name}-2026-10-01"
    write = GatewayBackendPoolMemberWrite(
        runtime_id=runtime["id"], model_id=replica["id"], priority=0, weight=1,
    )
    if invalid == "pool-capability":
        from turnstile_core.domain.storage_routes import (
            FixedProgramInstallation,
            ProgramIdentity,
            RouteScope,
        )

        installed = FixedProgramInstallation(
            program=ProgramIdentity(
                scope=RouteScope(target_id=str(APIM_ID), api_id="unit-api", gateway_id="managed"),
                api_revision="1", instance_id="managed", program_sha256="a" * 64,
                trust_binding_sha256="b" * 64,
            ),
            target_identity_sha256="c" * 64,
            storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
            gateway_endpoint="https://unit.azure-api.net/turnstile",
            publisher_subscription_id="unit", parent_policy_sha256="d" * 64,
            operation_policies_sha256={"models": "e" * 64},
            operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
        )
        original_model = repository.models[0]
        repository.models = [{**original_model, **model}, {**original_model, **replica}]
        fixed_effective = publication.model_copy(update={
            "resource_manifest": {"fixed_route_program": installed.model_dump(mode="json")},
        })
        monkeypatch.setattr(repository, "effective_gateway_publication", lambda _: (
            fixed_effective.model_dump(mode="python")
        ))
        request = GatewayBackendPoolWrite(members=[
            GatewayBackendPoolMemberWrite(runtime_id=runtime["id"], model_id=model["id"],
                                         priority=0, weight=1), write,
        ], rate_limit=GatewayRateLimitResilience(
            max_attempts_per_request=2, retry_interval_seconds=1, first_fast_retry=True,
            circuit_breaker=GatewayRateLimitCircuitBreaker(
                failure_count=1, interval_seconds=60, trip_duration_seconds=60,
                accept_retry_after=True,
            ),
        ))
        with patch.object(service, "_queue_route_reconcile") as queue:
            with pytest.raises(ControlPlaneConflictError, match="Pool capability is not installed"):
                service.model_backend_pool_candidates(model["id"])
            with pytest.raises(ControlPlaneConflictError, match="Pool capability is not installed"):
                service.configure_model_backend_pool(model["id"], request, "owner@example.com")
            queue.assert_not_called()
        return
    if invalid is not None:
        with pytest.raises(ControlPlaneConflictError) as rejected:
            service._backend_pool_member(
                binding, write, model, [runtime], [model, replica], APIM_ID,
            )
        if invalid in {"image", "decision", "embeddings", "family"}:
            # The 409 must name the member that differs, not just the rule.
            assert f"{replica['model_key']} on {runtime['name']}" in str(rejected.value)
        source_row = repository.models[0]
        repository.models = [{**source_row, **model}, {**source_row, **replica}]
        monkeypatch.setattr(repository, "effective_gateway_publication", lambda _gateway:
                            publication.model_dump(mode="python"))
        with patch.object(httpx.Client, "request", side_effect=AssertionError("No cloud reads")):
            assert replica["id"] not in {
                item.model_id for item in service.model_backend_pool_candidates(model["id"])
            }
        with patch.object(service, "_queue_route_reconcile") as queue:
            with pytest.raises(ControlPlaneConflictError):
                service.configure_model_backend_pool(model["id"], GatewayBackendPoolWrite(
                    members=[GatewayBackendPoolMemberWrite(
                        runtime_id=runtime["id"], model_id=model["id"], priority=0, weight=1,
                    ), write],
                    rate_limit=GatewayRateLimitResilience(
                        max_attempts_per_request=2, retry_interval_seconds=1,
                        first_fast_retry=True, circuit_breaker=GatewayRateLimitCircuitBreaker(
                            failure_count=1, interval_seconds=60, trip_duration_seconds=60,
                            accept_retry_after=True,
                        ),
                    ),
                ), "owner@example.com")
            queue.assert_not_called()
        return
    member = service._backend_pool_member(
        binding, write, model, [runtime], [model, replica], APIM_ID,
    )
    assert member.model_id == replica["id"]
    assert member.runtime_id == runtime["id"]
    assert member.upstream_model_id == replica_name
    assert member.backend_url == binding.backend_url
    from turnstile_core.services.route_publication import pool_member_bindings

    primary_member = service._backend_pool_member(binding, GatewayBackendPoolMemberWrite(
        runtime_id=runtime["id"], model_id=model["id"], priority=0, weight=1,
    ), model, [runtime], [model, replica], APIM_ID)
    pooled = binding.model_copy(update={"runtime_config": {
        **binding.runtime_config,
        "apim_backend_pool": GatewayBackendPoolConfig(
            members=[primary_member, member],
            rate_limit=GatewayRateLimitResilience(
                max_attempts_per_request=2, retry_interval_seconds=1, first_fast_retry=True,
                circuit_breaker=GatewayRateLimitCircuitBreaker(
                    failure_count=1, interval_seconds=60, trip_duration_seconds=60,
                    accept_retry_after=True,
                ),
            ),
        ).model_dump(mode="json"),
    }})
    sources = pool_member_bindings(pooled)
    assert [source.model.upstream_model_id for source in sources] == [
        deployment_name, replica_name,
    ]
    assert sources[0].runtime_id == sources[1].runtime_id == runtime["id"]
    assert (
        sources[0].runtime_config["project_endpoint"]
        == sources[1].runtime_config["project_endpoint"]
    )
    from turnstile_core.integrations.apim_control_plane_contract import BackendResource

    compiled = ApimPolicyCompiler(
        usage_observer_url="https://observer.example.com",
        usage_observer_key_named_value="unit-observer-key",
    ).compile(publication.model_copy(update={"desired_spec":
        publication.desired_spec.model_copy(update={"bindings": [pooled]}),
    }))
    member_backends = [backend for backend in compiled.backends
                       if isinstance(backend, BackendResource)
                       and backend.id.startswith("turnstile-pool-member-")]
    assert len(member_backends) == 2
    assert len({backend.id for backend in member_backends}) == 2
    assert [dict(backend.headers).get("x-turnstile-pool-deployment")
            for backend in member_backends] == [None, replica_name]
    assert member_backends[0].url == member_backends[1].url
    from turnstile_core.services.control_plane import GatewayControlPlaneService as PoolService

    source_row = repository.models[0]
    repository.models = [{**source_row, **model}, {**source_row, **replica}]
    effective = publication.model_copy(update={"desired_spec":
        publication.desired_spec.model_copy(update={"bindings": [pooled]}),
    }).model_dump(mode="python")
    if independent:
        source = publication.model_copy(update={
            "id": UUID(int=906), "status": PublicationStatus.ACTIVE,
            "desired_spec": publication.desired_spec.model_copy(update={"bindings": [pooled]}),
            "resource_manifest": {"model_route_addition": {
                "version": 1, "program_release_id": str(publication.id),
                "alias": model["model_key"],
            }},
        })
        repository.gateway_publications.append(source.model_dump(mode="python"))
        baseline = binding.model_copy(update={"model": binding.model.model_copy(update={
            "model_key": "baseline-public", "upstream_model_id": "baseline-deployment",
        })})
        effective["desired_spec"].update({
            "bindings": [baseline.model_dump(mode="python")],
            "discovery_models": [{"id": "baseline-public", "display_name": "Baseline model"}],
        })
    monkeypatch.setattr(repository, "effective_gateway_publication", lambda _gateway: effective)
    monkeypatch.setattr(repository, "effective_gateway_publications", lambda _gateways: [effective])
    candidate_service = PoolService(repository)
    with patch.object(httpx.Client, "request", side_effect=AssertionError("No cloud reads in GET")):
        candidates = candidate_service.model_backend_pool_candidates(model["id"])
    assert [item.model_id for item in candidates] == [model["id"], replica["id"]]
    assert [item.deployment_name for item in candidates] == [
        deployment_name, replica_name,
    ]
    assert {item.runtime_id for item in candidates} == {runtime["id"]}
    assert all(candidate.model_version is None for candidate in candidates)
    summaries = candidate_service.model_backend_pools()
    assert len(summaries) == 1
    assert summaries[0].model_id == model["id"]
    assert [item.model_id for item in summaries[0].pool.members] == [model["id"], replica["id"]]
    assert candidate_service.model_backend_pool(model["id"]) == pooled.backend_pool
    with patch.object(
        candidate_service, "_queue_route_reconcile", return_value=publication,
    ) as queue:
        candidate_service.configure_model_backend_pool(model["id"], GatewayBackendPoolWrite(
            members=[GatewayBackendPoolMemberWrite(
                runtime_id=item.runtime_id, model_id=item.model_id,
                priority=item.priority, weight=item.weight,
            ) for item in pooled.backend_pool.members],
            rate_limit=pooled.backend_pool.rate_limit,
        ), "owner@example.com")
        configured = next(item for item in queue.call_args.args[1].bindings
                          if item.model.model_key == model["model_key"])
        assert configured.backend_pool is not None
        assert len(configured.backend_pool.members) == 2
        candidate_service.remove_model_backend_pool(model["id"], "owner@example.com")
        removed = next(item for item in queue.call_args.args[1].bindings
                       if item.model.model_key == model["model_key"])
        assert removed.backend_pool is None
    from turnstile_core.domain.storage_routes import (
        ConnectionRevision,
        RouteBackendPool,
        RouteCredential,
        RoutePoolMember,
        RouteScope,
        document_digest,
    )
    from turnstile_core.services.route_publication import _pool_matches_binding, pool_header_name

    third_member = member.model_copy(update={
        "model_id": UUID(int=905), "upstream_model_id": "third-deployment", "weight": 2,
    })
    triple_pool = pooled.backend_pool.model_copy(update={
        "members": [primary_member, member, third_member],
    })
    scope = RouteScope(target_id=str(APIM_ID), api_id="unit-api", gateway_id="managed")
    snapshot_pool = RouteBackendPool(
        members=tuple(RoutePoolMember(
            runtime_id=str(runtime["id"]), backend_id="unit-member-" + str(index),
            dependency_sha256="a" * 64, priority=0, weight=weight,
        ) for index, weight in enumerate((1, 1, 2))),
        rate_limit=triple_pool.rate_limit,
        configuration_sha256=document_digest(triple_pool),
        header_values={pool_header_name(scope, value): value for value in (
            "pool-deployments.services.ai.azure.com", runtime["name"],
            replica_name, "third-deployment",
        )},
    )
    connection = ConnectionRevision(
        scope=scope, revision_id=str(UUID(int=906)), backend_id="unit-pool",
        origin="https://pool-deployments.services.ai.azure.com",
        path="/openai/v1/chat/completions", dependency_sha256="a" * 64,
        credential=RouteCredential(mode="managed_identity", header="Authorization",
                                   audience="https://ai.azure.com"),
        observer=True, pool=snapshot_pool,
    )
    unchanged = binding.model_copy(update={"runtime_config": {
        **binding.runtime_config, "apim_backend_pool": triple_pool.model_dump(mode="json"),
    }})
    assert _pool_matches_binding(unchanged, connection, {})
    swapped_pool = triple_pool.model_copy(update={"members": [
        primary_member, third_member.model_copy(update={"weight": 1}),
        member.model_copy(update={"weight": 2}),
    ]})
    swapped = binding.model_copy(update={"runtime_config": {
        **binding.runtime_config, "apim_backend_pool": swapped_pool.model_dump(mode="json"),
    }})
    assert not _pool_matches_binding(swapped, connection, {})


def test_backend_pool_contract_rejects_duplicate_and_zero_weight_groups() -> None:
    runtime_id = UUID("30000000-0000-4000-8000-000000000001")
    rate_limit = GatewayRateLimitResilience(
        max_attempts_per_request=2,
        retry_interval_seconds=1,
        first_fast_retry=True,
        circuit_breaker=GatewayRateLimitCircuitBreaker(
            failure_count=1,
            interval_seconds=60,
            trip_duration_seconds=60,
            accept_retry_after=True,
        ),
    )
    with pytest.raises(ValueError, match="Input should be 1"):
        GatewayRateLimitCircuitBreaker.model_validate(
            {
                "failure_count": 2,
                "interval_seconds": 60,
                "trip_duration_seconds": 60,
                "accept_retry_after": True,
            }
        )
    with pytest.raises(ValueError, match="cannot repeat"):
        GatewayBackendPoolWrite(
            members=[
                GatewayBackendPoolMemberWrite(
                    runtime_id=runtime_id,
                    priority=0,
                    weight=1,
                ),
                GatewayBackendPoolMemberWrite(
                    runtime_id=runtime_id,
                    priority=0,
                    weight=1,
                ),
            ],
            rate_limit=rate_limit,
        )
    with pytest.raises(ValueError, match="positive weight"):
        GatewayBackendPoolWrite(
            members=[
                GatewayBackendPoolMemberWrite(
                    runtime_id=runtime_id,
                    priority=0,
                    weight=1,
                ),
                GatewayBackendPoolMemberWrite(
                    runtime_id=UUID("30000000-0000-4000-8000-000000000002"),
                    priority=1,
                    weight=0,
                ),
            ],
            rate_limit=rate_limit,
        )
    with pytest.raises(ValueError, match="requires 429, 408, and 500-599"):
        GatewayRateLimitCircuitBreaker(
            failure_count=1,
            interval_seconds=60,
            trip_duration_seconds=60,
            accept_retry_after=True,
            status_code_ranges=(
                GatewayBackendFailureStatusCodeRange(minimum=200, maximum=299),
            ),
        )
    with pytest.raises(ValueError, match="requires connection failure and timeout"):
        GatewayRateLimitCircuitBreaker(
            failure_count=1,
            interval_seconds=60,
            trip_duration_seconds=60,
            accept_retry_after=True,
            error_reasons=("Timeout",),
        )

def test_route_reconcile_does_not_copy_one_models_pool_to_sibling_models() -> None:
    repository = InMemoryRepository()
    provider = next(
        item
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    runtime = next(
        item for item in repository.runtimes if item["provider_id"] == provider["id"]
    )
    runtime["config"].update(
        control_plane_managed=True,
        project_endpoint="https://pool-template.services.ai.azure.com/api/projects/main",
        backend_url="https://pool-template.services.ai.azure.com/api/projects/main",
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
            model=ModelCreateTarget(deployment_name="gpt-pool-template"),
        ),
        "owner@example.com",
    )
    binding = publication.desired_spec.bindings[-1]
    assert binding.backend_url is not None
    pool = GatewayBackendPoolConfig(
        members=[
            GatewayBackendPoolMember(
                runtime_id=runtime["id"],
                runtime_name=runtime["name"],
                backend_url=binding.backend_url,
                priority=0,
                weight=1,
            ),
            GatewayBackendPoolMember(
                runtime_id=UUID("30000000-0000-4000-8000-000000000096"),
                runtime_name="Sibling-safe secondary",
                backend_url=HttpUrl(
                    "https://pool-template-secondary.services.ai.azure.com/"
                    "api/projects/main"
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
    pooled = binding.model_copy(
        update={
            "runtime_config": {
                **binding.runtime_config,
                "apim_backend_pool": pool.model_dump(mode="json"),
            }
        }
    )
    effective = publication.desired_spec.model_copy(update={"bindings": [pooled]})

    reconciled = GatewayControlPlaneService._reconciled_release_spec(
        effective,
        repository.registry(),
    )

    target = next(
        item
        for item in reconciled.bindings
        if item.model.model_key == binding.model.model_key
    )
    siblings = [
        item
        for item in reconciled.bindings
        if item.model.model_key != binding.model.model_key
    ]
    assert "apim_backend_pool" in target.runtime_config
    assert siblings
    assert all("apim_backend_pool" not in item.runtime_config for item in siblings)

def test_retry_backend_identity_changes_with_normalized_upstream() -> None:
    repository = InMemoryRepository()
    provider = next(
        item
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    runtime = next(
        item for item in repository.runtimes if item["provider_id"] == provider["id"]
    )
    project_endpoint = (
        "https://example-foundry-resource.services.ai.azure.com/"
        "api/projects/example-project"
    )
    runtime["config"].update(
        control_plane_managed=True,
        project_endpoint=project_endpoint,
        backend_url=project_endpoint,
        auth_strategy="managed_identity",
        managed_identity_resource="https://ai.azure.com",
        streaming_mode="native",
    )
    publication = GatewayControlPlaneService(repository).publish(
        GatewayPublicationCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(existing_id=provider["id"]),
            runtime=RuntimeTarget(existing_id=runtime["id"]),
            model=ModelCreateTarget(deployment_name="claude-opus-5"),
        ),
        "owner@example.com",
    )
    binding = publication.desired_spec.bindings[-1]
    legacy_binding = binding.model_copy(
        update={
            "api_format": ApiFormat.OPENAI_CHAT,
            "backend_url": project_endpoint,
            "backend_path": "/openai/v1/chat/completions",
        }
    )
    legacy_publication = publication.model_copy(
        update={
            "desired_spec": publication.desired_spec.model_copy(
                update={"bindings": [legacy_binding]}
            )
        }
    )
    compiler = ApimPolicyCompiler(
        usage_observer_url="https://observer.example.com",
        usage_observer_key_named_value="turnstile-envoy-adapter-key",
    )

    legacy_backend = compiler.compile(legacy_publication).backends[0]
    messages_backend = compiler.compile(publication).backends[0]

    assert legacy_backend.url == "https://observer.example.com/api/projects/example-project"
    assert messages_backend.url == "https://observer.example.com"
    assert legacy_backend.id != messages_backend.id
    assert compiler.compile(publication).backends[0].id == messages_backend.id

def test_retry_upgrades_legacy_failed_foundry_claude_binding() -> None:
    repository = InMemoryRepository()
    provider = next(
        item
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    runtime = next(
        item for item in repository.runtimes if item["provider_id"] == provider["id"]
    )
    project_endpoint = (
        "https://example-foundry-resource.services.ai.azure.com/"
        "api/projects/example-project"
    )
    runtime["config"].update(
        control_plane_managed=True,
        project_endpoint=project_endpoint,
        backend_url=project_endpoint,
        auth_strategy="managed_identity",
        managed_identity_resource="https://ai.azure.com",
        streaming_mode="native",
    )
    service = GatewayControlPlaneService(repository)
    publication = service.publish(
        GatewayPublicationCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(existing_id=provider["id"]),
            runtime=RuntimeTarget(existing_id=runtime["id"]),
            model=ModelCreateTarget(deployment_name="claude-opus-5"),
        ),
        "owner@example.com",
    )
    stored = next(
        item for item in repository.gateway_publications if item["id"] == publication.id
    )
    legacy_binding = stored["desired_spec"]["bindings"][-1]
    legacy_binding.update(
        api_format="openai_chat",
        backend_url=project_endpoint,
        backend_path="/openai/v1/chat/completions",
    )
    next(
        item
        for item in stored["desired_spec"]["discovery_models"]
        if item["id"] == legacy_binding["model"]["model_key"]
    )["api_format"] = "openai_chat"
    stored["desired_spec_sha256"] = "legacy-openai-spec"
    transition_publication(
        repository,
        publication.id,
        "queued",
        "failed",
        {"error_code": "max_attempts_exceeded"},
        "worker",
    )

    retried = service.retry(
        publication.id,
        GatewayPublicationRetry(),
        "owner@example.com",
    )
    binding = retried.desired_spec.bindings[-1]

    assert retried.id == publication.id
    assert retried.status == "queued"
    assert retried.desired_spec_sha256 != "legacy-openai-spec"
    assert binding.api_format is ApiFormat.ANTHROPIC_MESSAGES
    assert str(binding.backend_url).rstrip("/") == (
        "https://example-foundry-resource.services.ai.azure.com"
    )
    assert binding.backend_path == "/anthropic/v1/messages"

def test_external_foundry_claude_uses_anthropic_api_key_header() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(
            repository,
            deployment="claude-opus-5",
        ),
        "owner@example.com",
    )

    compiled = ApimPolicyCompiler().compile(publication)
    binding = publication.desired_spec.bindings[-1]

    assert binding.api_format is ApiFormat.ANTHROPIC_MESSAGES
    assert '<set-header name="x-api-key" exists-action="override">' in (
        compiled.messages_policy
    )
    assert '<set-header name="api-key" exists-action="override">' not in (
        compiled.messages_policy
    )
    assert 'name="anthropic-version" exists-action="override"' in (
        compiled.messages_policy
    )

def test_claude_first_foundry_activation_preserves_shared_connection_protocol() -> None:
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
    service = GatewayControlPlaneService(
        repository,
        apim_principal_id="00000000-0000-0000-0000-000000000705",
    )
    publication = service.publish(
        foundry_publication("claude-opus-5"), "owner@example.com"
    )
    current = "queued"
    for status in (
        "validating",
        "provisioning",
        "building_revision",
        "verifying",
        "promoting",
    ):
        transition_publication(repository, publication.id, current, status, {}, "worker")
        current = status

    activate_publication(repository, publication.id, "worker")

    runtime = next(
        item
        for item in repository.runtimes
        if item["name"] == "Microsoft Foundry contoso-ai/finops via APIM"
    )
    assert runtime["config"]["path"] == "/chat/completions"
    assert runtime["config"]["api_format"] == "openai_chat"
    assert runtime["config"]["backend_url"] == (
        "https://contoso-ai.services.ai.azure.com/api/projects/finops"
    )
    assert runtime["config"]["backend_path"] == "/openai/v1/chat/completions"
    model = next(
        model for model in repository.models if model["runtime_id"] == runtime["id"]
    )
    assert model["family_key"] == "claude"
    discovery = service._existing_discovery(APIM_ID, repository.registry())
    assert next(item for item in discovery if item.id == model["model_key"]).api_format is (
        ApiFormat.ANTHROPIC_MESSAGES
    )
    removal = service.remove_model(model["id"], "owner@example.com")
    assert removal.desired_spec.removed_models[0].api_format is (
        ApiFormat.ANTHROPIC_MESSAGES
    )

def test_external_tenant_foundry_key_is_encrypted_outside_the_release() -> None:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())

    publication = GatewayControlPlaneService(repository, cipher).publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )
    binding = publication.desired_spec.bindings[-1]
    encrypted = repository.gateway_publication_credential(publication.id)

    assert str(binding.backend_url).rstrip("/") == (
        "https://unit-foundry-account.openai.azure.com"
    )
    assert binding.backend_path == "/openai/v1/chat/completions"
    assert binding.auth_strategy.value == "named_value_api_key"
    assert binding.named_value_name is not None
    assert binding.named_value_name.startswith("turnstile-foundry-key-")
    assert binding.managed_identity_resource is None
    assert binding.runtime_config["max_tokens_field"] == "max_completion_tokens"
    assert binding.runtime_config["supports_temperature"] is False
    assert binding.runtime_config["inference_endpoint"] == (
        "https://unit-foundry-account.openai.azure.com/openai/v1"
    )
    assert "authorization" not in binding.runtime_config
    assert encrypted is not None
    assert cipher.decrypt(encrypted) == "external-foundry-key"
    snapshot = publication.model_dump_json()
    assert "external-foundry-key" not in snapshot

def test_external_tenant_foundry_policy_uses_secret_api_key_header() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )

    compiled = ApimPolicyCompiler().compile(publication)
    policy = compiled.chat_completions_policy
    responses_policy = compiled.responses_policy
    binding = publication.desired_spec.bindings[-1]

    assert len(compiled.named_values) == 1
    assert compiled.named_values[0].id == binding.named_value_name
    assert '<set-header name="api-key" exists-action="override">' in policy
    assert f"{{{{{binding.named_value_name}}}}}" in policy
    assert "external-foundry-key" not in policy
    assert 'template="/openai/v1/chat/completions"' in policy
    assert f"{{{{{binding.named_value_name}}}}}" in responses_policy
    assert 'template="/openai/v1/responses"' in responses_policy
    assert 'body[&quot;model&quot;] = &quot;gpt-5-unit-deployment&quot;' in responses_policy
    assert 'template="/openai/v1/responses/compact"' in compiled.responses_compact_policy
    assert 'name="selectedContextWindow"' in compiled.responses_compact_policy
    assert '<set-header name="Accept-Encoding" exists-action="override">' in responses_policy
    assert "<value>identity</value>" in responses_policy
    assert "streamOptions" not in responses_policy

def test_usage_observer_preserves_foundry_route_auth_and_model_transform() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )

    compiled = ApimPolicyCompiler(
        usage_observer_url="https://observer.example.com",
        usage_observer_key_named_value="turnstile-envoy-adapter-key",
    ).compile(publication)
    policy = compiled.chat_completions_policy
    binding = publication.desired_spec.bindings[-1]
    backend = compiled.backends[-1]

    assert backend.id.startswith("turnstile-obs-")
    assert backend.url == "https://observer.example.com"
    assert backend.headers == (
        ("x-adapter-key", "{{turnstile-envoy-adapter-key}}"),
        ("x-turnstile-upstream-host", "unit-foundry-account.openai.azure.com"),
    )
    assert f"{{{{{binding.named_value_name}}}}}" in policy
    assert 'template="/openai/v1/chat/completions"' in policy
    assert 'body[&quot;model&quot;] = &quot;gpt-5-unit-deployment&quot;' in policy
    assert 'streamOptions[&quot;include_usage&quot;] = true' in policy
    assert 'name="selectedRoutingManaged" value="@(true)"' in policy
    assert "x-turnstile-api-format" in policy
    assert "openai_chat" in policy

def test_usage_observer_preserves_foundry_managed_identity_and_project_path() -> None:
    repository = InMemoryRepository()
    request = foundry_publication()
    provider = next(
        item
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    request.provider = ProviderTarget(existing_id=provider["id"])
    publication = GatewayControlPlaneService(
        repository,
        apim_principal_id="00000000-0000-0000-0000-000000000705",
    ).publish(request, "owner@example.com")

    compiled = ApimPolicyCompiler(
        usage_observer_url="https://observer.example.com",
        usage_observer_key_named_value="turnstile-envoy-adapter-key",
    ).compile(publication)
    backend = compiled.backends[-1]

    assert backend.url == "https://observer.example.com/api/projects/finops"
    assert backend.headers == (
        ("x-adapter-key", "{{turnstile-envoy-adapter-key}}"),
        ("x-turnstile-upstream-host", "contoso-ai.services.ai.azure.com"),
    )
    assert (
        '<authentication-managed-identity resource="https://ai.azure.com" '
        'output-token-variable-name="turnstileProviderAccessToken" '
        'ignore-error="false" />'
        in compiled.chat_completions_policy
    )
    assert 'context.Variables["turnstileProviderAccessToken"]' in (
        compiled.chat_completions_policy
    )
    assert 'template="/openai/v1/chat/completions"' in compiled.chat_completions_policy
    assert (
        'body[&quot;model&quot;] = &quot;gpt-5-mini-deployment&quot;'
        in compiled.chat_completions_policy
    )

def test_usage_observer_keeps_same_name_cross_account_projects_separate() -> None:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository)
    first = service.publish(
        external_tenant_foundry_publication(
            repository,
            account="tenant-a-resource",
            project="shared",
            deployment="gpt-tenant-a",
        ),
        "owner@example.com",
    )
    current = "queued"
    for status in (
        "validating",
        "provisioning",
        "building_revision",
        "verifying",
        "promoting",
    ):
        assert transition_publication(repository, first.id, current, status, {}, "worker")
        current = status
    activate_publication(repository, first.id, "worker")

    second = service.seed_legacy_publication(
        external_tenant_foundry_publication(
            repository,
            account="tenant-b-resource",
            project="shared",
            deployment="gpt-tenant-b",
        ),
        "owner@example.com",
    )
    assert {binding.runtime_name for binding in second.desired_spec.bindings} == {
        "Microsoft Foundry tenant-a-resource/shared via APIM",
        "Microsoft Foundry tenant-b-resource/shared via APIM",
    }
    colliding_spec = second.desired_spec.model_copy(
        update={
            "bindings": [
                binding.model_copy(update={"runtime_name": "Renamed Foundry connection"})
                for binding in second.desired_spec.bindings
            ]
        }
    )
    colliding_publication = second.model_copy(update={"desired_spec": colliding_spec})
    compiled = ApimPolicyCompiler(
        usage_observer_url="https://observer.example.com",
        usage_observer_key_named_value="turnstile-envoy-adapter-key",
    ).compile(colliding_publication)

    upstream_hosts = {
        dict(backend.headers)["x-turnstile-upstream-host"]
        for backend in compiled.backends
    }
    assert upstream_hosts == {
        "tenant-a-resource.openai.azure.com",
        "tenant-b-resource.openai.azure.com",
    }

def test_usage_observer_preserves_bedrock_route_auth_and_body_transform() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        bedrock_publication(), "owner@example.com"
    )

    compiled = ApimPolicyCompiler(
        usage_observer_url="https://observer.example.com",
        usage_observer_key_named_value="turnstile-envoy-adapter-key",
    ).compile(publication)
    policy = compiled.messages_policy
    binding = publication.desired_spec.bindings[-1]
    backend = compiled.backends[-1]

    assert backend.id.startswith("turnstile-obs-")
    assert backend.url == "https://observer.example.com"
    assert backend.headers == (
        ("x-adapter-key", "{{turnstile-envoy-adapter-key}}"),
        (
            "x-turnstile-upstream-host",
            "bedrock-runtime.ap-southeast-2.amazonaws.com",
        ),
    )
    assert f"Bearer {{{{{binding.named_value_name}}}}}" in policy
    assert 'template="/model/au.anthropic.claude-sonnet-4-6/invoke"' in policy
    assert 'body[&quot;anthropic_version&quot;] = &quot;bedrock-2023-05-31&quot;' in policy
    assert "x-turnstile-api-format" in policy
    assert "anthropic_messages" in policy
    assert 'name="selectedRoutingManaged" value="@(true)"' in policy

def test_unmanaged_known_model_is_marked_for_legacy_fallback() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        bedrock_publication(), "owner@example.com"
    )
    unmanaged = publication.desired_spec.bindings[-1].model_copy(
        update={"routing_managed": False}
    )
    desired = publication.desired_spec.model_copy(update={"bindings": [unmanaged]})
    publication = publication.model_copy(update={"desired_spec": desired})

    compiled = ApimPolicyCompiler().compile(publication)

    assert 'name="selectedRoutingManaged" value="@(false)"' in compiled.messages_policy
    assert "<set-backend-service" not in compiled.messages_policy

def test_usage_observer_requires_a_complete_https_origin_configuration() -> None:
    with pytest.raises(ValueError, match="configured together"):
        ApimPolicyCompiler(usage_observer_url="https://observer.example.com")
    with pytest.raises(ValueError, match="HTTPS origin"):
        ApimPolicyCompiler(
            usage_observer_url="https://observer.example.com/path",
            usage_observer_key_named_value="turnstile-envoy-adapter-key",
        )

def test_external_tenant_key_is_materialized_once_then_erased() -> None:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    publication = GatewayControlPlaneService(repository, cipher).publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )
    client = FakeApimClient()
    worker = GatewayPublicationWorker(repository, client, client.policy, cipher=cipher)

    worker.run_once("worker")
    provisioning = worker.run_once("worker")

    assert provisioning is not None
    assert provisioning.status == "provisioning"
    assert client.named_values[-1].value == "external-foundry-key"
    assert repository.gateway_publication_credential(publication.id) is None

@pytest.mark.parametrize("evidence", ("none", "manifest", "revision", "runtime"))
def test_retry_credential_evidence_controls_the_public_view(evidence: str) -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )
    binding = publication.desired_spec.bindings[-1]
    if evidence == "manifest":
        publication = publication.model_copy(
            update={"resource_manifest": {"named_values": [binding.named_value_name]}}
        )
    elif evidence == "revision":
        publication = publication.model_copy(update={"apim_revision": "candidate-revision"})
    elif evidence == "runtime":
        updated_binding = binding.model_copy(
            update={"runtime_config": {**binding.runtime_config, "credential_provisioned": True}}
        )
        publication = publication.model_copy(
            update={
                "desired_spec": publication.desired_spec.model_copy(
                    update={"bindings": [*publication.desired_spec.bindings[:-1], updated_binding]}
                )
            }
        )

    required = evidence != "revision"
    assert publication_retry_requires_credential(publication) is required
    view = GatewayPublicationView.from_publication(publication)
    assert view.retry_requires_credential is required
    assert publication_materialized_named_values(publication) == (
        [] if required else [binding.named_value_name]
    )


def test_retry_credential_evidence_does_not_invent_a_key_for_an_unrelated_resource() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )
    publication = publication.model_copy(
        update={"resource_manifest": {"named_values": [None, False, "unrelated-key"]}}
    )
    assert publication_retry_requires_credential(publication) is True
    assert publication_materialized_named_values(publication) == []
    materialized = publication_materialized_named_values(publication, provisioning_completed=True)
    assert publication.desired_spec.bindings[-1].named_value_name in materialized
    assert publication.resource_manifest["named_values"] == [None, False, "unrelated-key"]


@pytest.mark.parametrize("replacement", [None, "api_key", "oauth_client_secret", "probes"])
def test_model_addition_recovery_capability_preserves_original_intent(
    replacement: str | None,
) -> None:
    publication = GatewayControlPlaneService(InMemoryRepository()).publish(
        external_tenant_foundry_publication(InMemoryRepository()), "unit-owner",
    ).model_copy(update={
        "status": PublicationStatus.FAILED, "attempt_count": 1,
        "resource_manifest": {"model_route_addition": {"version": 1}},
    })
    repository = MagicMock()
    repository.get_gateway_publication.return_value = publication.model_dump(mode="python")
    repository.requeue_gateway_publication.return_value = publication.model_copy(
        update={"status": PublicationStatus.QUEUED},
    ).model_dump(mode="python")
    service = GatewayControlPlaneService(repository)
    view = GatewayPublicationView.from_publication(service.publication(publication.id))
    assert view.retry_requires_credential is False
    assert view.credential_kind is None
    write = GatewayPublicationRetry(
        api_key=SecretStr("unit-replacement") if replacement == "api_key" else None,
        oauth_client_secret=SecretStr("unit-replacement")
        if replacement == "oauth_client_secret" else None,
        authorize_image_probes=replacement == "probes",
    )
    if replacement is not None:
        with pytest.raises(ControlPlaneConflictError, match="cannot replace its intent"):
            service.retry(publication.id, write, "unit-owner")
        repository.requeue_gateway_publication.assert_not_called()
    else:
        queued = service.retry(publication.id, write, "unit-owner")
        assert queued.id == publication.id
        assert queued.desired_spec_sha256 == publication.desired_spec_sha256
        assert queued.resource_manifest == publication.resource_manifest
        assert queued.attempt_count == publication.attempt_count
        assert queued.status is PublicationStatus.QUEUED
        repository.requeue_gateway_publication.assert_called_once_with(
            publication.id, "unit-owner", None,
        )


@pytest.mark.parametrize("fault", [
    None, "probed", "prepared", "leased", "base", "intent", "other_error", "context", "progress",
])
@pytest.mark.parametrize("checkpoint", [False, True, "upgraded", "twice_upgraded"])
def test_budget_denied_model_addition_recovers_through_real_repository(
    fault: str | None, checkpoint: bool | str,
) -> None:
    from copy import deepcopy

    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository, apim_principal_id=str(UUID(int=705)))
    request = foundry_publication()
    request.provider = ProviderTarget(existing_id=next(
        item["id"] for item in repository.providers if item["brand_key"] == "microsoft_foundry"
    ))
    previous = service.seed_legacy_publication(request, "unit-owner")
    baseline = repository.get_gateway_publication(previous.id)
    assert baseline is not None
    baseline.update(status="active", resource_manifest={"fixed_route_program": {}})
    repository.effective_gateway_releases[APIM_ID] = previous.id
    repository.gateway_publication_outbox[0].update(status="completed")
    binding = previous.desired_spec.bindings[-1]
    spec = previous.desired_spec.model_copy(update={
        "bindings": [binding.model_copy(update={
            "runtime_id": binding.runtime_id or UUID(int=997), "routing_managed": True,
            "model": binding.model.model_copy(update={"model_key": "unit-recovery-model"}),
        })],
        "discovery_models": [previous.desired_spec.discovery_models[-1].model_copy(update={
            "id": "unit-recovery-model",
        })],
    })
    payload, _ = service._release_payload(spec)
    manifest = {"model_route_addition": {
        "version": 1, "program_release_id": str(previous.id), "alias": "unit-recovery-model",
    }}
    failed = repository.create_gateway_publication(
        APIM_ID, payload, document_digest({**manifest, "desired_spec": payload}),
        "model_add", "unit-owner", expected_base_release_id=previous.id,
        initial_manifest=manifest,
    )
    failed.update(status="failed", attempt_count=1, error_code="ValueError",
                  error_message="Budget publication target is not enabled")
    if checkpoint:
        from turnstile_core.domain.storage_routes import FixedProgramUpgrade
        from turnstile_core.integrations.apim_storage_routes import fixed_operation_contracts

        contracts = fixed_operation_contracts("/v1/models", "/v1/messages", {})
        installation = FixedProgramInstallation(
            program=ProgramIdentity(scope=RouteScope(
                target_id=str(APIM_ID), api_id="unit-api", gateway_id="managed",
            ), api_revision="1", instance_id="managed", program_sha256="a" * 64,
                trust_binding_sha256="b" * 64),
            target_identity_sha256="c" * 64,
            storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
            gateway_endpoint="https://unit.azure-api.net/llm",
            publisher_subscription_id="unit-publisher",
            parent_policy_sha256="d" * 64, operation_contracts=contracts,
            operation_policies_sha256={name: "e" * 64 for name in contracts},
            supported_profiles=("openai-v1", "anthropic-messages"), model_additions_enabled=True,
        )
        baseline["resource_manifest"]["fixed_route_program"] = installation.model_dump(mode="json")
        failed["resource_manifest"].update({
            "route_preparation": {
                "version": 1,
                "context_sha256": FixedRoutePublisherFactory._dependency_context(
                    GatewayPublication.model_validate(failed), installation,
                ), "connections": {}, "credentials": {},
            },
            "route_credentials": {"version": 1, "bindings": {
                "f" * 64: "https://unitvault.vault.azure.net/secrets/original/" + "1" * 32,
            }}, "route_last_progress_attempt": 1,
        })
        current_installation = installation
        if checkpoint in {"upgraded", "twice_upgraded"}:
            current_installation = installation.model_copy(update={
                "program": installation.program.model_copy(update={"program_sha256": "9" * 64}),
                "parent_policy_sha256": "8" * 64,
                "legacy_program_sha256": installation.program.program_sha256,
            })
            upgrade = FixedProgramUpgrade(
                previous_publication_id=str(previous.id),
                previous_installation_sha256=document_digest(installation),
                installation=current_installation,
            )
            upgrade.require_previous(str(previous.id), installation)
            gateway = next(row for row in repository.gateways if row["id"] == APIM_ID)
            gateway.setdefault("config", {})["fixed_route_program_upgrade"] = (
                upgrade.model_dump(mode="json")
            )
            if checkpoint == "twice_upgraded":
                gateway["config"]["fixed_route_program_upgrade_history"] = [
                    upgrade.model_dump(mode="json"),
                ]
                previous_installation = current_installation
                current_installation = current_installation.model_copy(update={
                    "program": current_installation.program.model_copy(update={
                        "program_sha256": "7" * 64,
                    }), "parent_policy_sha256": "6" * 64,
                    "predecessor_program_sha256": (previous_installation.program.program_sha256,),
                })
                gateway["config"]["fixed_route_program_upgrade"] = FixedProgramUpgrade(
                    previous_publication_id=str(previous.id),
                    previous_installation_sha256=document_digest(previous_installation),
                    installation=current_installation,
                ).model_dump(mode="json")
    outbox = repository.gateway_publication_outbox[-1]
    outbox.update(status="completed", attempts=1, lease_owner=None, lease_expires_at=None)
    if fault == "probed":
        repository.gateway_publication_audit.append({
            "publication_id": failed["id"], "detail": {"probe_request": {}},
        })
    elif fault == "prepared":
        failed["resource_manifest"]["route_preparation"] = {}
    elif fault == "leased":
        outbox["status"] = "leased"
    elif fault == "base":
        failed["base_release_id"] = UUID(int=998)
    elif fault == "intent":
        failed["desired_spec_sha256"] = "f" * 64
    elif fault == "other_error":
        failed["error_message"] = "Unknown outcome"
    elif fault == "context":
        failed["resource_manifest"].setdefault("route_preparation", {})["context_sha256"] = "f" * 64
    elif fault == "progress":
        failed["resource_manifest"]["route_last_progress_attempt"] = 2
    preserved, old_outbox = deepcopy(failed), deepcopy(outbox)
    audit = deepcopy(repository.gateway_publication_audit)
    if fault is not None:
        with pytest.raises(ControlPlaneConflictError):
            service.retry(failed["id"], GatewayPublicationRetry(), "unit-recovery")
        assert failed == preserved and outbox == old_outbox
        assert repository.gateway_publication_audit == audit
    else:
        recovered = service.retry(failed["id"], GatewayPublicationRetry(), "unit-recovery")
        assert recovered.id == failed["id"] and recovered.status is PublicationStatus.QUEUED
        for field in ("desired_spec", "desired_spec_sha256", "resource_manifest", "base_release_id",
                      "attempt_count", "created_by"):
            assert failed[field] == preserved[field]
        assert outbox["status"] == "queued" and outbox["attempts"] == old_outbox["attempts"]
        assert repository.gateway_publication_audit[:-1] == audit
        assert repository.gateway_publication_audit[-1]["actor"] == "unit-recovery"
        if checkpoint:
            factory = FixedRoutePublisherFactory(
                repository, MagicMock(), active_fence=MagicMock(),
                availability_projector=MagicMock(), clock=lambda: 101,
            )
            configuration, management = MagicMock(), MagicMock()
            configuration.settings.apim_usage_observer_url = None
            management.prepare_route_connection.side_effect = (
                lambda connection, persist, **kwargs: connection.dependency_sha256
            )
            preparer = factory._connection_preparer(
                recovered, current_installation, configuration, management,
                "unit-worker", lambda: None,
            )
            connection, issued = preparer(recovered.desired_spec.bindings[0])
            assert connection.scope == current_installation.program.scope and issued == 101
            management.prepare_route_connection.assert_called_once()
            assert recovered.resource_manifest == preserved["resource_manifest"]
            unregistered = current_installation.model_copy(update={
                "program": current_installation.program.model_copy(update={
                    "program_sha256": "5" * 64,
                }),
            })
            with pytest.raises(PolicyCompilationError, match="not approved"):
                factory._connection_preparer(
                    recovered, unregistered, configuration, management,
                    "unit-worker", lambda: None,
                )
        from turnstile_core.persistence.repository_publications import (
            PostgreSqlPublicationRepositoryMixin,
        )

        statements: list[tuple[str, Any]] = []

        def execute_sql(statement: str, parameters: Any) -> Mock:
            sql = " ".join(statement.split())
            statements.append((sql, parameters))
            if sql.startswith("SELECT publication.*"):
                return Mock(fetchone=lambda: baseline)
            if sql.startswith("SELECT count(*)"):
                return Mock(fetchone=lambda: {"total": 0})
            if sql.startswith("SELECT config"):
                return Mock(fetchone=lambda: {"config": (
                    repository.get_gateway_profiles([APIM_ID])[0].get("config") or {}
                )})
            if sql.startswith("SELECT id, apim_target_id"):
                return Mock(fetchall=lambda: list(repository.gateway_deployments.values()))
            if sql.startswith("SELECT id FROM gateway_publication"):
                return Mock(fetchone=lambda: None)
            if sql.startswith("SELECT * FROM gateway_publication_outbox"):
                return Mock(fetchone=lambda: old_outbox)
            if sql.startswith("UPDATE gateway_publication SET"):
                return Mock(fetchone=lambda: failed)
            return Mock()

        connection = Mock(execute=execute_sql)
        sql_recovered = PostgreSqlPublicationRepositoryMixin._requeue_gateway_publication(
            connection, preserved, "unit-recovery", None,
        )
        assert sql_recovered["id"] == preserved["id"]
        updates = [sql for sql, _ in statements if sql.startswith("UPDATE")]
        assert len(updates) == 2
        assert all(not any(field in sql for field in (
            "attempt_count", "attempts=", "resource_manifest", "desired_spec", "created_by",
        )) for sql in updates)
        assert not any(sql.startswith("DELETE") for sql, _ in statements)
        assert statements[-1][1][2].obj["manifest_sha256"] == document_digest(
            preserved["resource_manifest"],
        )


def test_retry_credential_evidence_is_bound_and_missing_overrides_old_success() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "unit-owner",
    )
    reference = publication.desired_spec.bindings[-1].named_value_name
    assert reference is not None
    observed = publication_credential_evidence(publication, {reference: "present"})
    present = publication.model_copy(update={
        "resource_manifest": {"credential_evidence": observed},
    })
    assert publication_credential_states(present) == {reference: "present"}
    assert publication_retry_requires_credential(present) is False
    for state in ("missing", "unverified"):
        evidence = publication_credential_evidence(publication, {reference: state})
        missing = publication.model_copy(update={
            "apim_revision": "old-candidate",
            "resource_manifest": {"credential_evidence": evidence, "named_values": [reference]},
        })
        assert publication_retry_requires_credential(missing) is True
    stale = present.model_copy(update={"resource_manifest": {
        **present.resource_manifest, "credential_generation": "replacement",
    }})
    assert publication_credential_states(stale) == {}
    assert publication_retry_requires_credential(stale) is True
    assert publication_provisioning_completed([
        {"from_status": "validating", "to_status": "provisioning"},
        {"from_status": "failed", "to_status": "queued"},
    ]) is False
    with pytest.raises(ValueError, match="unrelated binding"):
        publication_credential_evidence(publication, {"foreign": "present"})


def test_retry_credential_evidence_never_requires_a_key_for_managed_identity() -> None:
    repository = InMemoryRepository()
    request = external_tenant_foundry_publication(repository)
    request.runtime = RuntimeTarget(
        foundry_project_endpoint=request.runtime.foundry_project_endpoint
    )
    publication = GatewayControlPlaneService(
        repository, apim_principal_id="00000000-0000-4000-8000-000000000001"
    ).publish(request, "owner@example.com")
    assert publication_retry_requires_credential(publication) is False
    assert publication_materialized_named_values(publication, provisioning_completed=True) == []


def test_foundry_activation_normalizes_older_release_token_field() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )
    stored = next(
        item for item in repository.gateway_publications if item["id"] == publication.id
    )
    stored["desired_spec"]["bindings"][-1]["runtime_config"].pop(
        "max_tokens_field", None
    )
    current = "queued"
    for next_status in (
        "validating",
        "provisioning",
        "building_revision",
        "verifying",
        "promoting",
    ):
        transition_publication(repository, publication.id, current, next_status, {}, "worker")
        current = next_status

    activate_publication(repository, publication.id, "worker")

    runtime = next(
        item
        for item in repository.runtimes
        if item["name"] == "Microsoft Foundry unit-foundry-account/unit-foundry-project via APIM"
    )
    assert runtime["config"]["max_tokens_field"] == "max_completion_tokens"
    assert runtime["config"]["supports_temperature"] is False

def test_external_tenant_foundry_failed_release_requires_replacement_key() -> None:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository, cipher)
    publication = service.publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )
    transition_publication(
        repository,
        publication.id,
        "queued",
        "failed",
        {"error_code": "probe_failed", "error_message": "probe failed"},
        "worker",
    )

    with pytest.raises(ControlPlaneConflictError, match="replacement API key"):
        service.retry(publication.id, GatewayPublicationRetry(), "owner@example.com")

    retried = service.retry(
        publication.id,
        GatewayPublicationRetry(api_key=SecretStr("replacement-foundry-key")),
        "owner@example.com",
    )

    encrypted = repository.gateway_publication_credential(publication.id)
    assert retried.status == "queued"
    assert encrypted is not None
    assert cipher.decrypt(encrypted) == "replacement-foundry-key"

@pytest.mark.parametrize("legacy_manifest", (False, True))
def test_retry_reuses_materialized_credentials_and_the_same_publication(
    legacy_manifest: bool,
) -> None:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository, cipher)
    publication = service.publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )
    client = FakeApimClient()
    worker = GatewayPublicationWorker(repository, client, client.policy, cipher=cipher)
    worker.run_once("worker")
    provisioned = worker.run_once("worker")
    assert provisioned is not None and provisioned.status.value == "provisioning"
    updates: dict[str, object] = {"resource_manifest": {}} if legacy_manifest else {}
    transition_publication(repository, publication.id, "provisioning", "failed", updates, "worker")

    observed = service.publication(publication.id)
    assert GatewayPublicationView.from_publication(observed).retry_requires_credential is False
    retried = service.retry(publication.id, GatewayPublicationRetry(), "owner@example.com")

    assert retried.id == publication.id
    assert retried.generation == publication.generation
    assert retried.status.value == "queued"
    assert retried.apim_revision is None
    reference = publication.desired_spec.bindings[-1].named_value_name
    assert retried.resource_manifest["named_values"] == [reference]
    assert publication_credential_states(retried) == {reference: "present"}
    assert repository.gateway_publication_credential(publication.id) is None
    assert len(repository.gateway_publications) == 1
    assert len(repository.gateway_publication_outbox) == 1
    worker.run_once("retry-worker")
    repeated = worker.run_once("retry-worker")
    assert repeated is not None and repeated.status.value == "provisioning"
    assert repository.gateway_publication_credential(publication.id) is None


def test_credential_preparation_does_not_clear_secret_after_lease_loss() -> None:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository, cipher)
    publication = service.publish(external_tenant_foundry_publication(repository), "unit-owner")
    client = FakeApimClient()
    worker = GatewayPublicationWorker(repository, client, client.policy, cipher=cipher)
    worker.run_once("worker")
    original_ensure = client.ensure_backend

    def lose_lease(backend: Any) -> None:
        original_ensure(backend)
        repository.gateway_publication_outbox[0]["lease_expires_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        )

    client.ensure_backend = lose_lease  # type: ignore[method-assign]
    worker.run_once("worker")
    current = repository.get_gateway_publication(publication.id)
    assert current is not None and current["status"] == "validating"
    assert repository.gateway_publication_credential(publication.id) is not None
    assert "credential_evidence" not in current["resource_manifest"]


def test_missing_credential_observation_is_preserved_on_worker_failure() -> None:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository, cipher)
    publication = service.publish(external_tenant_foundry_publication(repository), "unit-owner")
    client = FakeApimClient()
    worker = GatewayPublicationWorker(repository, client, client.policy, cipher=cipher)
    worker.run_once("worker")

    def missing(named_value: Any) -> None:
        raise CredentialSourceMissingError(named_value.id)

    client.ensure_named_value = missing  # type: ignore[method-assign]
    failed = worker.run_once("worker")
    assert failed is not None and failed.status == "failed"
    reference = publication.desired_spec.bindings[-1].named_value_name
    assert publication_credential_states(failed) == {reference: "missing"}
    assert publication_retry_requires_credential(service.publication(publication.id))


def test_retry_replacement_key_clears_old_materialization_for_reprovisioning() -> None:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository, cipher)
    publication = service.publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )
    transition_publication(
        repository,
        publication.id,
        "queued",
        "failed",
        {
            "resource_manifest": {
                "named_values": [publication.desired_spec.bindings[-1].named_value_name]
            }
        },
        "worker",
    )

    retried = service.retry(
        publication.id,
        GatewayPublicationRetry(api_key=SecretStr("replacement-test-key")),
        "owner@example.com",
    )

    assert set(retried.resource_manifest) == {"credential_generation"}
    assert UUID(str(retried.resource_manifest["credential_generation"]))
    encrypted = repository.gateway_publication_credential(publication.id)
    assert encrypted is not None and cipher.decrypt(encrypted) == "replacement-test-key"


def test_retry_postgresql_preserves_audit_proven_credentials_in_one_requeue() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )
    row = publication.model_dump(mode="python")
    row["status"] = "failed"
    row["resource_manifest"] = {}
    connection = MagicMock()
    connection.execute.return_value.fetchone.side_effect = [None, None, row]
    connection.execute.return_value.fetchall.return_value = [
        {"from_status": "queued", "to_status": "validating"},
        {"from_status": "validating", "to_status": "provisioning"},
    ]

    PostgreSqlPublicationRepositoryMixin._requeue_gateway_publication(
        connection, row, "owner@example.com", None
    )

    calls = connection.execute.call_args_list
    audit = next(
        call for call in calls if "SELECT from_status, to_status" in call.args[0]
    )
    assert audit.args[1] == (publication.id,)
    assert "ORDER BY created_at, id" in audit.args[0]
    update = next(call for call in calls if "UPDATE gateway_publication SET" in call.args[0])
    manifest = update.args[1][0].obj
    reference = publication.desired_spec.bindings[-1].named_value_name
    assert manifest["named_values"] == [reference]
    assert publication_credential_states(publication.model_copy(update={
        "resource_manifest": manifest,
    })) == {reference: "present"}
    assert "apim_revision = NULL" in update.args[0]
    assert any("DELETE FROM gateway_publication_secret" in call.args[0] for call in calls)
    assert not any("INSERT INTO gateway_publication (" in call.args[0] for call in calls)


def test_external_tenant_foundry_key_can_rotate_through_a_candidate_release() -> None:
    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository, cipher)
    publication = service.publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )
    current = "queued"
    for status in (
        "validating",
        "provisioning",
        "building_revision",
        "verifying",
        "promoting",
    ):
        assert transition_publication(repository, publication.id, current, status, {}, "worker")
        current = status
    activate_publication(repository, publication.id, "worker")
    original = publication.desired_spec.bindings[-1]

    rotation = service.rotate_credential(
        APIM_ID,
        GatewayCredentialRotation(
            model_key=original.model.model_key,
            api_key=SecretStr("rotated-foundry-key"),
        ),
        "owner@example.com",
    )
    rotated = rotation.desired_spec.bindings[-1]
    encrypted = repository.gateway_publication_credential(rotation.id)
    policy = ApimPolicyCompiler().compile(rotation).chat_completions_policy

    assert rotation.publication_kind.value == "credential_rotation"
    assert rotated.auth_strategy.value == "named_value_api_key"
    assert rotated.named_value_name != original.named_value_name
    assert encrypted is not None
    assert cipher.decrypt(encrypted) == "rotated-foundry-key"
    assert f"{{{{{rotated.named_value_name}}}}}" in policy
    assert "rotated-foundry-key" not in policy


@pytest.mark.parametrize("pinned", [None, "target", "peer"])
def test_fixed_route_key_rotation_replaces_every_route_of_its_connection(
    pinned: str | None,
) -> None:
    from turnstile_core.services.route_publication import route_credential_source

    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository, cipher)

    def activate(publication_id: UUID) -> None:
        current = "queued"
        for status in ("validating", "provisioning", "building_revision", "verifying",
                       "promoting"):
            assert transition_publication(
                repository, publication_id, current, status, {}, "worker",
            )
            current = status
        activate_publication(repository, publication_id, "worker")

    effective = service.publish(
        external_tenant_foundry_publication(repository), "owner@example.com"
    )
    activate(effective.id)
    original = effective.desired_spec.bindings[-1]
    connection = service._binding_connection(original, APIM_ID, repository.registry())
    runtime = next(item for item in repository.runtimes if item["id"] == connection["id"])
    recorded = "https://unit-vault.vault.azure.net/secrets/runtime-key/" + "a" * 32
    runtime["config"]["key_vault_secret_id"] = recorded
    base = repository.get_gateway_publication(effective.id)
    assert base is not None
    base["resource_manifest"] = {"fixed_route_program": {}}
    alias = "unit-rotation-addition"
    spec = effective.desired_spec.model_copy(update={
        "bindings": [original.model_copy(update={
            "model": original.model.model_copy(update={"model_key": alias}),
            "key_vault_secret_id": HttpUrl(
                recorded if pinned is None else recorded[:-32] + "b" * 32,
            ),
        })],
        "discovery_models": [effective.desired_spec.discovery_models[-1].model_copy(
            update={"id": alias},
        )],
    })
    payload, _ = service._release_payload(spec)
    manifest = {"model_route_addition": {
        "version": 1, "program_release_id": str(effective.id), "alias": alias,
    }}
    added = repository.create_gateway_publication(
        APIM_ID, payload, document_digest({**manifest, "desired_spec": payload}),
        "model_add", "unit-owner", expected_base_release_id=effective.id,
        initial_manifest=manifest,
    )
    added["status"] = "active"
    repository.gateway_publication_outbox[-1].update(status="completed")
    write = GatewayCredentialRotation(
        model_key=original.model.model_key if pinned == "peer" else alias,
        api_key=SecretStr("rotated-route-key"),
    )
    if pinned is not None:
        with pytest.raises(ControlPlaneConflictError, match=(
            "credential ownership" if pinned == "peer" else "directly managed API key"
        )):
            service.rotate_credential(APIM_ID, write, "owner@example.com")
        return

    rotation = service.rotate_credential(APIM_ID, write, "owner@example.com")
    rotated = rotation.desired_spec.bindings[-1]
    routes = {item.model.model_key: item for item in rotation.desired_spec.bindings}
    encrypted = repository.gateway_publication_credential(rotation.id)

    assert rotation.publication_kind is PublicationKind.CREDENTIAL_ROTATION
    assert rotation.base_release_id == effective.id
    assert rotated.model.model_key == alias
    assert rotated.named_value_name not in {None, original.named_value_name}
    for key in (original.model.model_key, alias):
        assert routes[key].named_value_name == rotated.named_value_name
        assert routes[key].key_vault_secret_id is None
    assert encrypted is not None and cipher.decrypt(encrypted) == "rotated-route-key"

    materialized = recorded[:-32] + "c" * 32
    row = repository.get_gateway_publication(rotation.id)
    assert row is not None
    row["resource_manifest"]["route_credentials"] = {"version": 1, "bindings": {
        route_credential_source(rotated): materialized,
    }}
    activate(rotation.id)

    assert runtime["config"]["key_vault_secret_id"] == materialized
    assert runtime["config"]["named_value_name"] == rotated.named_value_name
    assert added["status"] == "superseded"
    assert repository.effective_gateway_releases[APIM_ID] == rotation.id


def test_foundry_chat_policy_owns_backend_auth_path_and_deployment_mapping() -> None:
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
    ).publish(foundry_publication(), "owner@example.com")

    compiled = ApimPolicyCompiler().compile(publication)
    policy = compiled.chat_completions_policy
    binding = publication.desired_spec.bindings[-1]

    assert len(compiled.backends) == 1
    assert compiled.backends[0].url == (
        "https://contoso-ai.services.ai.azure.com/api/projects/finops"
    )
    assert 'backend-id="turnstile-dyn-' in policy
    assert 'template="/openai/v1/chat/completions"' in policy
    assert 'authentication-managed-identity resource="https://ai.azure.com"' in policy
    assert 'output-token-variable-name="turnstileProviderAccessToken"' in policy
    assert 'context.Variables["turnstileProviderAccessToken"]' in policy
    assert 'body[&quot;model&quot;] = &quot;gpt-5-mini-deployment&quot;' in policy
    assert 'name="selectedProviderName" value="microsoft_foundry"' in policy
    assert binding.model.model_key not in compiled.messages_policy

def test_reusing_a_foundry_project_needs_only_a_deployment_name() -> None:
    repository = InMemoryRepository()
    foundry_provider = next(
        item for item in repository.providers if item["brand_key"] == "microsoft_foundry"
    )
    runtime = next(
        item for item in repository.runtimes if item["provider_id"] == foundry_provider["id"]
    )
    runtime["enabled"] = True
    runtime["config"] = {
        "control_plane_managed": True,
        "project_endpoint": (
            "https://contoso-ai.services.ai.azure.com/api/projects/finops"
        ),
        "backend_url": (
            "https://contoso-ai.services.ai.azure.com/api/projects/finops"
        ),
        "path": "/chat/completions",
        "api_format": "openai_chat",
        "auth_strategy": "managed_identity",
        "managed_identity_resource": "https://ai.azure.com",
        "streaming_mode": "native",
    }
    request = GatewayPublicationCreate(
        gateway_profile_id=APIM_ID,
        provider=ProviderTarget(existing_id=foundry_provider["id"]),
        runtime=RuntimeTarget(existing_id=runtime["id"]),
        model=ModelCreateTarget(deployment_name="gpt-5.4"),
    )

    publication = GatewayControlPlaneService(repository).publish(
        request, "owner@example.com"
    )
    binding = publication.desired_spec.bindings[-1]

    assert binding.runtime_id == runtime["id"]
    assert binding.model.upstream_model_id == "gpt-5.4"
    assert binding.model.display_name == "gpt-5.4 · Microsoft Foundry"
    assert binding.model.model_key.startswith("gpt-5.4-foundry-")
    assert binding.model.capabilities == ["chat", "streaming"]
    assert binding.backend_path == "/openai/v1/chat/completions"
