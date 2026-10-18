"""A paused, legacy-revision installation handoff, not a normal publisher bypass.

The cloud adapter stages the returned bundle, projects existing model identities
with ``project_model_availability``, and supplies live ARM/Table/readiness reads.
Only a real current revision can be imported into the publication repository.
An imported APIM release remains fenced: this does not activate financial traffic.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, Field

from backend.services.client_user_policy import strip_verified_client_user_policy
from scripts.budget_execution_upgrade import plan_budget_parent_upgrade
from scripts.budget_policy_bindings import bind_policy_parameters
from scripts.install_budget_execution import (
    BudgetInstallationPlan,
    InstallationJournal,
    MaintenanceReadback,
)
from scripts.install_model_availability import InstallationChange, normalize_legacy_parent
from scripts.prepare_budget_execution import FenceConfiguration
from turnstile_core.domain.budget_counting import BudgetPublicationContracts
from turnstile_core.domain.budget_delegation import BudgetDelegationReadiness
from turnstile_core.domain.control_plane import (
    GatewayPublication,
    GatewayReleaseDependencies,
    PublicationKind,
    PublicationStatus,
)
from turnstile_core.domain.model_availability import (
    ModelAvailabilityCoverage,
    ModelAvailabilityPolicyAdoption,
    ModelAvailabilityPublication,
    ModelAvailabilityState,
)
from turnstile_core.domain.models import StrictModel
from turnstile_core.integrations.apim_budget_execution import compose_budget_execution_parent
from turnstile_core.integrations.apim_control_plane_contract import (
    ApimPublisherClient,
    CompiledGatewayRelease,
    OperationResource,
)
from turnstile_core.integrations.apim_directory_policy import (
    strip_validated_directory_identity_policy,
)
from turnstile_core.integrations.apim_image_policy import IMAGE_OPERATION_ID
from turnstile_core.integrations.apim_model_availability import (
    AVAILABILITY_PROBE_PREFIX,
    INHERITED_OPERATION_POLICY,
    compile_availability_adoption,
    compile_protocol_fenced_availability_operation,
    compile_protocol_fenced_probe,
    compose_availability_parent,
    is_protocol_fenced_availability_operation,
    validate_availability_parent,
    without_availability_parent,
)
from turnstile_core.integrations.apim_policy_compiler import ApimPolicyCompiler
from turnstile_core.integrations.apim_policy_components import (
    component_digest,
    parse_policy,
    policy_document_digest,
    serialize_policy,
    validate_parent_policy,
)
from turnstile_core.integrations.model_availability_verifier import inspect_availability_operation
from turnstile_core.persistence.repository_publications import PostgreSqlPublicationRepositoryMixin

_SLOTS = re.compile(r"__[A-Z][A-Z0-9_]*__")
_FIXED_MANIFESTS = {"fixed_route_program", "route_configuration", "fixed_route_migration"}


def _recorded_dependency_names(publication: GatewayPublication) -> set[str]:
    result: set[str] = set()
    for field, prefix in (("backends", "backends/"), ("named_values", "namedValues/"),
                          ("oauth_credentials", "authorizationProviders/")):
        values = publication.resource_manifest.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("Serving lineage requires the original dependency manifest")
        result.update(prefix + value for value in values)
    return result


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _bind_policy(source: str, bindings: Mapping[str, str]) -> str:
    return serialize_policy(bind_policy_parameters(parse_policy(source), bindings))


def capture_installation_contracts(
    publication: GatewayPublication,
    registry: Mapping[str, Sequence[dict[str, Any]]],
    fence: FenceConfiguration,
) -> BudgetPublicationContracts:
    """Capture only unchanged, existing routes; never pretend a paused F is active."""
    if fence.mode != "paused" or fence.gateway_profile_id != publication.gateway_profile_id:
        raise ValueError("Installation contracts require their exact paused Gateway")
    if publication.publication_kind != PublicationKind.ROUTE_RECONCILE:
        raise ValueError("Installation cannot add or remove a model")
    aliases = {binding.model.model_key for binding in publication.desired_spec.bindings}
    if aliases != set(fence.model_contracts):
        raise ValueError("Installation must explicitly review every existing route contract")
    contracts, digests = {}, {}
    for binding in publication.desired_spec.bindings:
        alias = binding.model.model_key
        contract = fence.model_contracts[alias]
        matches = [model for model in registry["models"]
                   if str(model["model_key"]).casefold() == alias.casefold()]
        if len(matches) != 1 or UUID(str(matches[0]["id"])) != contract.model_id:
            raise ValueError("Installation model identity is missing or ambiguous")
        if binding.logical_model_id not in {None, contract.model_id} or (
            binding.runtime_id is not None
            and str(matches[0].get("runtime_id")) != str(binding.runtime_id)
        ):
            raise ValueError("Installation cannot change an existing model route")
        required = BudgetPublicationContracts._estimate(
            publication, binding, contract.model_id, BudgetPublicationContracts.binding_digest(
                binding,
            ),
        )
        if not set(required.protocols) <= set(contract.operations) | contract.protocols.keys():
            raise ValueError("Installation must retain every existing protocol operation")
        for protocol in (contract, *contract.protocols.values()):
            if protocol.verified_at > publication.created_at or (
                protocol.request_policy is not None
                and protocol.request_policy.verified_at > publication.created_at
            ):
                raise ValueError("Counting and request evidence must precede installation")
        contracts[alias] = contract.model_copy(deep=True)
        digests[alias] = BudgetPublicationContracts.binding_digest(binding)
    result = BudgetPublicationContracts(
        publication_id=publication.id, gateway_profile_id=publication.gateway_profile_id,
        epoch=fence.epoch, release_spec_sha256=publication.desired_spec_sha256,
        binding_sha256=digests, model_contracts=contracts,
    )
    result.validate_publication(publication)
    return result


@dataclass(frozen=True)
class HistoricalAvailabilityBundle:
    """Offline exact changes; install only AFTER the shared F is observed paused."""

    adoption: ModelAvailabilityPolicyAdoption
    changes: tuple[InstallationChange, ...]
    normalized_legacy_choice: bool
    removed_client_overlay: bool
    removed_directory_overlay: bool

    @property
    def desired_policy_sha256(self) -> dict[str, str]:
        return {change.path: hashlib.sha256(change.body["properties"]["value"].encode()).hexdigest()
                for change in self.changes if change.path.endswith("/policies/policy")}

    @property
    def digest(self) -> str:
        return _digest({"adoption": self.adoption.model_dump(mode="json"),
                        "removed_client_overlay": self.removed_client_overlay,
                        "removed_directory_overlay": self.removed_directory_overlay,
                        "normalized_legacy_choice": self.normalized_legacy_choice,
                        "changes": [change.summary() for change in self.changes]})


def build_historical_availability(
    adoption: ModelAvailabilityPolicyAdoption,
    parent: tuple[str, str],
    operations: Mapping[str, tuple[OperationResource, str | None, str | None]],
    *, bindings: Mapping[str, str], allow_verified_client_overlay_removal: bool = False,
    allow_verified_directory_overlay_removal: bool = False,
) -> HistoricalAvailabilityBundle:
    """Retain old routing/TPM behind a permanent, verifiable parent denial.

    The inputs are the backed-up raw policy reads plus concrete ETags. Inherited
    operation policies must be explicitly listed in the adoption. Extra HEAD
    routes are create-only. The normal full-revision verifier must run afterward;
    this artifact never creates coverage or treats an old revision as active.
    Only explicitly approved, verified client/directory overlays can be removed, and only
    in the SAME final parent PUT that installs the first unconditional denial.
    Recovery restores the backed-up original bytes, including that overlay.
    """
    if set(adoption.original_policy_digests) != {"parent", *operations}:
        raise ValueError("Historical adoption requires the exact complete operation inventory")

    def etag(value: str | None) -> None:
        if not value or value == "*" or any(item in value for item in ("\r", "\n")):
            raise ValueError("Historical policy needs its exact observed ETag")

    source, parent_etag = parent
    etag(parent_etag)
    adoption.validate_source("parent", source)
    existing = parse_policy(source).find(".//set-variable[@name='availabilityProtocolVersion']")
    if existing is not None:
        raise ValueError("An already adopted revision must be recovered, never wrapped again")
    normalized_original = normalize_legacy_parent(source)
    client_overlay = parse_policy(normalized_original).find(
        ".//set-variable[@name='clientContextPolicyVersion']",
    ) is not None
    if client_overlay and not allow_verified_client_overlay_removal:
        raise ValueError("Historical client overlay removal needs explicit installation approval")
    normalized = (
        strip_verified_client_user_policy(normalized_original)
        if client_overlay else normalized_original
    )
    directory_overlay = parse_policy(normalized).find(
        ".//set-variable[@name='directoryIdentityPolicyVersion']",
    ) is not None
    if directory_overlay and not allow_verified_directory_overlay_removal:
        raise ValueError("Directory overlay removal needs explicit installation approval")
    if directory_overlay:
        normalized = strip_validated_directory_identity_policy(normalized)
    available = _bind_policy(compose_availability_parent(normalized), bindings)
    candidate = compile_protocol_fenced_availability_operation(available)
    candidate_root = parse_policy(candidate)
    validate_availability_parent(candidate_root)
    if not is_protocol_fenced_availability_operation(candidate_root):
        raise ValueError("Historical admission must remain permanently fenced")
    peeled = without_availability_parent(candidate_root)
    inbound = peeled.find("inbound")
    assert inbound is not None
    inbound.remove(next(child for child in inbound if isinstance(child.tag, str)))
    if policy_document_digest(serialize_policy(peeled)) != policy_document_digest(normalized):
        raise ValueError("Historical adoption changed the approved baseline beyond its overlay")
    prefix = f"/apis/{adoption.api_id};rev={adoption.revision}"

    def policy_body(document: str) -> dict[str, Any]:
        return {"properties": {"format": "xml", "value": document}}

    changes = [InstallationChange(
        prefix + "/policies/policy", parent_etag,
        hashlib.sha256(source.encode()).hexdigest(), policy_body(candidate),
    )]
    expected_paths = {
        "chat-completions": "/chat/completions", "responses": "/responses",
        "responses-compact": "/responses/compact", "anthropic-messages": "/v1/messages",
        "anthropic-count-tokens": "/v1/messages/count_tokens",
        IMAGE_OPERATION_ID: "/images/generations",
    }
    catalogs = 0
    for identity, (operation, document, observed_etag) in sorted(operations.items()):
        if (identity != operation.id
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identity)
            or identity.startswith(AVAILABILITY_PROBE_PREFIX)
            or any(char in operation.path for char in ("{", "}", "?", "#"))):
            raise ValueError("Historical operation identity is not an exact reviewed route")
        catalog = operation.method == "GET" and operation.path in {"/models", "/v1/models"}
        catalogs += int(catalog)
        if not catalog and (operation.method != "POST" or (
            expected_paths.get(identity) != operation.path
            and identity not in adoption.fixed_operations
        )):
            raise ValueError("Historical operation needs an explicit verified admission contract")
        inherited = identity in adoption.inherited_operations
        if (document is None) != inherited or (observed_etag is None) != inherited:
            raise ValueError("Historical operation policy presence changed")
        if not inherited:
            etag(observed_etag)
        original = INHERITED_OPERATION_POLICY if document is None else document
        compiled = compile_availability_adoption(original, adoption, identity, catalog=catalog)
        inspect_availability_operation(
            compiled, adoption.target_id, adoption.api_id, adoption.revision, catalog=catalog,
        )
        path = prefix + "/operations/" + identity
        changes.append(InstallationChange(
            path + "/policies/policy", observed_etag,
            None if inherited else hashlib.sha256(original.encode()).hexdigest(),
            policy_body(compiled),
        ))
        diagnostic = prefix + "/operations/" + AVAILABILITY_PROBE_PREFIX + identity
        changes.extend((
            InstallationChange(diagnostic, None, None, {"properties": {
                "displayName": "Availability " + identity, "method": "HEAD",
                "urlTemplate": operation.path, "templateParameters": [],
            }}),
            InstallationChange(diagnostic + "/policies/policy", None, None,
                               policy_body(compile_protocol_fenced_probe(compiled))),
        ))
    if not catalogs:
        raise ValueError("Historical availability requires its original model catalog")
    return HistoricalAvailabilityBundle(
        adoption, tuple(changes), normalized_original != source, client_overlay, directory_overlay,
    )


class ServingPolicyReadback(StrictModel):
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    etag: str = Field(min_length=1)

    @classmethod
    def capture(cls, source: str, etag: str) -> ServingPolicyReadback:
        if etag == "*" or any(character in etag for character in ("\r", "\n")):
            raise ValueError("Serving lineage requires concrete source policy ETags")
        return cls(raw_sha256=hashlib.sha256(source.encode()).hexdigest(),
                   document_sha256=policy_document_digest(source), etag=etag)


class ServingOperationReadback(StrictModel):
    method: Literal["GET", "POST", "HEAD"]
    path: str = Field(min_length=1)
    policy: ServingPolicyReadback


class ServingBaselineLineage(StrictModel):
    """Reviewed manual-parent lineage, NOT a claim of old compiler hash equality."""

    version: Literal[1] = 1
    publication_id: UUID
    gateway_profile_id: UUID
    sql_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    serving_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    desired_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sql_recorded_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sql_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sql_parent: ServingPolicyReadback
    serving_parent: ServingPolicyReadback
    sql_operations: dict[str, ServingOperationReadback] = Field(min_length=1)
    serving_operations: dict[str, ServingOperationReadback] = Field(min_length=1)
    shared_dependency_sha256: dict[str, str] = Field(min_length=1)
    source_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: AwareDatetime
    reviewed_at: AwareDatetime
    reviewed_by: str = Field(min_length=1, max_length=255)
    review_kind: Literal["manual_parent_only_upgrade"] = "manual_parent_only_upgrade"
    sql_policy_digest_comparison: Literal["not_asserted"] = "not_asserted"

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))

    def require_previous(self, previous: GatewayPublication, now: datetime) -> None:
        if (self.publication_id != previous.id
            or self.gateway_profile_id != previous.gateway_profile_id
            or self.sql_revision != previous.apim_revision
            or self.serving_revision == self.sql_revision
            or self.desired_spec_sha256 != previous.desired_spec_sha256
            or self.sql_recorded_policy_sha256 != previous.policy_sha256
            or self.sql_manifest_sha256 != _digest(previous.resource_manifest)):
            raise ValueError("Serving lineage does not bind the unchanged SQL publication")
        if not self.observed_at <= self.reviewed_at <= now or not (
            0 <= (now - self.observed_at).total_seconds() <= 86400
        ):
            raise ValueError("Serving lineage requires a current reviewed observation")
        if self.sql_operations.keys() != self.serving_operations.keys():
            raise ValueError("Serving lineage must include every operation on both revisions")
        expected_operations = {
            "chat-completions", "responses", "responses-compact", "anthropic-messages",
            "anthropic-count-tokens", "anthropic-models",
        }
        if previous.resource_manifest.get("images_generations_operation"):
            expected_operations.add(IMAGE_OPERATION_ID)
        if self.sql_operations.keys() != expected_operations:
            raise ValueError("Serving lineage is missing the legacy publication operation set")
        if not _recorded_dependency_names(previous) <= set(self.shared_dependency_sha256):
            raise ValueError("Serving lineage dependency scope changed")
        for parent in (self.sql_parent, self.serving_parent):
            if parent.etag == "*" or any(c in parent.etag for c in ("\r", "\n")):
                raise ValueError("Serving lineage parent ETag is not concrete")
        for identity, original in self.sql_operations.items():
            current = self.serving_operations[identity]
            if (current.method != original.method or current.path != original.path
                or current.policy.document_sha256 != original.policy.document_sha256):
                raise ValueError("Manual serving lineage changed an operation contract")
            for policy in (original.policy, current.policy):
                if policy.etag == "*" or any(c in policy.etag for c in ("\r", "\n")):
                    raise ValueError("Serving lineage operation ETag is not concrete")
        for key, digest in self.shared_dependency_sha256.items():
            if (not key.startswith(("backends/", "namedValues/", "authorizationProviders/"))
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError("Serving lineage needs exact shared dependency readback digests")


def capture_serving_baseline_lineage(
    previous: GatewayPublication, *, serving_revision: str,
    sql_parent: tuple[str, str], serving_parent: tuple[str, str],
    sql_operations: Mapping[str, tuple[OperationResource, str, str]],
    serving_operations: Mapping[str, tuple[OperationResource, str, str]],
    shared_dependencies: Mapping[str, Mapping[str, Any]], source_evidence_sha256: str,
    observed_at: datetime, reviewed_at: datetime, reviewed_by: str,
) -> ServingBaselineLineage:
    """Capture read-only baseline evidence before the old revisions are fenced.

    Both revisions use the same APIM-global dependency resources. Their actual
    resource documents are fingerprinted, not declared healthy. The new candidate
    must still pass the existing complete dependency inspector independently.
    """
    def operations(
        values: Mapping[str, tuple[OperationResource, str, str]],
    ) -> dict[str, ServingOperationReadback]:
        result = {}
        for identity, (resource, source, etag) in values.items():
            if identity != resource.id:
                raise ValueError("Serving lineage operation identity is inconsistent")
            result[identity] = ServingOperationReadback(
                method=resource.method, path=resource.path,
                policy=ServingPolicyReadback.capture(source, etag),
            )
        return result

    manifest = previous.resource_manifest
    dependencies = _recorded_dependency_names(previous)
    source_documents = [sql_parent[0], serving_parent[0], *(
        source for values in (sql_operations, serving_operations)
        for _, source, _ in values.values()
    ), json.dumps(shared_dependencies, default=str)]
    dependencies.update(
        "namedValues/" + name for document in source_documents
        for name in re.findall(r"\{\{([A-Za-z0-9][A-Za-z0-9._-]{0,126})\}\}", document)
    )
    if not dependencies or dependencies != set(shared_dependencies):
        raise ValueError("Serving lineage must bind every recorded shared dependency")
    lineage = ServingBaselineLineage(
        publication_id=previous.id, gateway_profile_id=previous.gateway_profile_id,
        sql_revision=str(previous.apim_revision), serving_revision=serving_revision,
        desired_spec_sha256=previous.desired_spec_sha256,
        sql_recorded_policy_sha256=str(previous.policy_sha256),
        sql_manifest_sha256=_digest(manifest),
        sql_parent=ServingPolicyReadback.capture(*sql_parent),
        serving_parent=ServingPolicyReadback.capture(*serving_parent),
        sql_operations=operations(sql_operations),
        serving_operations=operations(serving_operations),
        shared_dependency_sha256={key: _digest(value)
                                  for key, value in shared_dependencies.items()},
        source_evidence_sha256=source_evidence_sha256, observed_at=observed_at,
        reviewed_at=reviewed_at, reviewed_by=reviewed_by,
    )
    lineage.require_previous(previous, reviewed_at)
    return lineage


@dataclass(frozen=True)
class InstallationPublicationBundle:
    previous: GatewayPublication
    publication: GatewayPublication
    parent_policy: str
    compiled: CompiledGatewayRelease
    serving_lineage: ServingBaselineLineage | None = None

    @property
    def serving_revision(self) -> str | None:
        return self.serving_lineage.serving_revision if self.serving_lineage else (
            self.previous.apim_revision
        )

    @property
    def operations(self) -> dict[str, tuple[OperationResource, str]]:
        text: tuple[tuple[str, Literal["GET", "POST", "HEAD"], str, str], ...] = (
            ("chat-completions", "POST", "/chat/completions",
             self.compiled.chat_completions_policy),
            ("responses", "POST", "/responses", self.compiled.responses_policy),
            ("responses-compact", "POST", "/responses/compact",
             self.compiled.responses_compact_policy),
            ("anthropic-messages", "POST", "/v1/messages", self.compiled.messages_policy),
            ("anthropic-count-tokens", "POST", "/v1/messages/count_tokens",
             self.compiled.count_tokens_policy),
            ("anthropic-models", "GET", "/v1/models", self.compiled.models_policy),
        )
        result = {
            name: (OperationResource(name, name, method, path), source)
            for name, method, path, source in text
        }
        extras = dict(self.compiled.availability_probe_policies)
        if self.compiled.images_generations_policy is not None:
            extras[IMAGE_OPERATION_ID] = self.compiled.images_generations_policy
        for operation in self.compiled.operations:
            if operation.id in result or operation.id not in extras:
                raise ValueError("Installation has an ambiguous operation inventory")
            result[operation.id] = operation, extras.pop(operation.id)
        if extras:
            raise ValueError("Installation is missing an operation definition")
        return result

    @property
    def policy_digests(self) -> dict[str, str]:
        return {
            "parent": policy_document_digest(self.parent_policy),
            **{key: policy_document_digest(value[1]) for key, value in self.operations.items()},
        }

    @property
    def policy_contracts(self) -> dict[str, str]:
        # Keep source digests immutable; APIM readbacks use the native upload contract.
        return {
            "parent": component_digest(
                parse_policy(self.parent_policy), normalize_text_defaults=False,
            ),
            **{key: component_digest(parse_policy(value[1]), normalize_text_defaults=False)
               for key, value in self.operations.items()},
        }

    @property
    def dependency_digest(self) -> str:
        return _digest({
            "backends": [asdict(value) for value in self.compiled.backends],
            "named_values": [asdict(value) for value in self.compiled.named_values],
            "oauth_credentials": [asdict(value) for value in self.compiled.oauth_credentials],
        })

    @property
    def digest(self) -> str:
        return _digest({
            "publication": self.publication.model_dump(mode="json"),
            "policies": self.policy_digests, "dependencies": self.dependency_digest,
            "operations": {key: asdict(value[0]) for key, value in self.operations.items()},
        })


def build_installation_publication(
    previous: GatewayPublication, *,
    registry: Mapping[str, Sequence[dict[str, Any]]],
    fence: FenceConfiguration,
    parent_policy: str,
    bindings: Mapping[str, str],
    compiler: ApimPolicyCompiler,
    revision: str,
    generation: int,
    actor: str,
    created_at: datetime,
    serving_lineage: ServingBaselineLineage | None = None,
    source_parent_policy: str | None = None,
) -> InstallationPublicationBundle:
    if previous.status != PublicationStatus.ACTIVE or not previous.apim_revision:
        raise ValueError("Installation requires the actual effective legacy publication")
    if _FIXED_MANIFESTS & previous.resource_manifest.keys():
        raise ValueError("Fixed programs require their own reviewed migration path")
    if "budget_contracts" in previous.resource_manifest:
        raise ValueError("An installed budget publication must be recovered, not reinstalled")
    if generation <= previous.generation or previous.gateway_profile_id != fence.gateway_profile_id:
        raise ValueError("Installation generation or Gateway identity is invalid")
    if serving_lineage is not None:
        serving_lineage.require_previous(previous, created_at)
        if revision == serving_lineage.serving_revision:
            raise ValueError("Installation cannot overwrite the actual serving revision")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", revision) or (
        revision == previous.apim_revision or any(
            revision not in revisions for revisions in fence.gateways.values()
        )
    ):
        raise ValueError("The new revision must be explicitly covered by every serving Gateway")
    publication_id = uuid5(fence.epoch, f"legacy-budget-publication:{fence.gateway_profile_id}")
    publication = previous.model_copy(deep=True, update={
        "id": publication_id, "publication_kind": PublicationKind.ROUTE_RECONCILE,
        "generation": generation, "status": PublicationStatus.VERIFYING,
        "base_release_id": previous.id, "apim_revision": revision, "policy_sha256": None,
        "resource_manifest": {"native_stream_contract_version": 1},
        "created_by": actor, "created_at": created_at, "started_at": created_at,
        "updated_at": created_at, "completed_at": None, "attempt_count": 0,
        "error_code": None, "error_message": None,
    })
    contracts = capture_installation_contracts(publication, registry, fence)
    availability = ModelAvailabilityPublication.capture(publication, registry, fence.api_id)
    publication.resource_manifest.update({
        "budget_contracts": contracts.model_dump(mode="json"),
        "model_availability": availability.model_dump(mode="json"),
        **({"installation_serving_lineage": serving_lineage.model_dump(mode="json")}
           if serving_lineage is not None else {}),
    })
    if parse_policy(parent_policy).find(
        "inbound/set-variable[@name='budgetExecutionVersion'][@value='@(2)']",
    ) is None:
        raise ValueError("Installation requires the reviewed budget schema-two parent")
    parent = _bind_policy(compose_availability_parent(parent_policy), bindings)
    canonical = (
        Path(__file__).resolve().parents[1] / "infra/policies/foundry-finops-policy.xml"
    ).read_text()
    if serving_lineage is not None:
        if source_parent_policy is None or policy_document_digest(source_parent_policy) != (
            serving_lineage.serving_parent.document_sha256
        ) or fence.delegation is None:
            raise ValueError("Candidate adoption requires the actual reviewed serving parent")
        legacy = (
            Path(__file__).resolve().parents[1] / "infra/policies/foundry-finops-policy.schema1.xml"
        ).read_text()
        upgraded = plan_budget_parent_upgrade(
            source_parent_policy, legacy_template=legacy, canonical_parent=canonical,
            delegation=fence.delegation, gateway_profile_id=fence.gateway_profile_id,
        )
        expected_parent = _bind_policy(
            compose_availability_parent(upgraded.candidate_parent), upgraded.adopted_bindings,
        )
        if policy_document_digest(parent) != policy_document_digest(expected_parent):
            raise ValueError("Candidate parent discarded changes from the actual serving baseline")
    validate_parent_policy(parent, compose_availability_parent(compose_budget_execution_parent(
        canonical, canonical, enabled=True, delegation=fence.delegation,
    )))
    compiled = compiler.compile(publication)
    publication.policy_sha256 = compiled.policy_sha256
    publication.resource_manifest.update({
        "backends": [value.id for value in compiled.backends],
        "named_values": [value.id for value in compiled.named_values],
        "oauth_credentials": [value.config.provider_id for value in compiled.oauth_credentials],
        "parent_policy_sha256": hashlib.sha256(parent.encode()).hexdigest(),
        "parent_policy_contract_sha256": component_digest(
            parse_policy(parent), normalize_text_defaults=False,
        ),
        **({"images_generations_operation": IMAGE_OPERATION_ID}
           if compiled.images_generations_policy is not None else {}),
    })
    bundle = InstallationPublicationBundle(previous.model_copy(deep=True), publication,
                                           parent, compiled, serving_lineage)
    if any(_SLOTS.search(source) for _, source in bundle.operations.values()):
        raise ValueError("Compiled operations contain unresolved parameters")
    return bundle


class InstallationProtocolVerification(StrictModel):
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity: BudgetDelegationReadiness
    paused: BudgetDelegationReadiness

    def require_verified(
        self, bundle: InstallationPublicationBundle, fence: FenceConfiguration, now: datetime,
    ) -> None:
        if self.bundle_sha256 != bundle.digest:
            raise ValueError("Protocol diagnostics belong to another installation artifact")
        for result, mode in ((self.identity, "prepared"), (self.paused, "paused")):
            if (not result.ready or result.protocol_mode != mode
                or result.gateway_profile_id != bundle.publication.gateway_profile_id
                or result.revision != bundle.publication.apim_revision
                or not 0 <= (now - result.checked_at).total_seconds() <= 3600):
                raise ValueError("Installation needs real, current candidate protocol diagnostics")
        expected = {"valid_identity": 200, "wrong_audience": 401, "missing_person": 403}
        if len(self.identity.checks) != len(expected) or {
            item.case for item in self.identity.checks
        } != expected.keys():
            raise ValueError("Identity verification requires both negative cases")
        for item in self.identity.checks:
            if (item.expected_status != expected[item.case]
                or item.actual_status != expected[item.case]):
                raise ValueError("Delegated backend identity did not pass the actual HTTP checks")
        paused_cases = {"current_fenced", "candidate_fenced"}
        required = {(native, case) for native in fence.gateways for case in paused_cases}
        if len(self.paused.checks) != len(required) or {
            (item.native_gateway_id, item.case) for item in self.paused.checks
        } != required:
            raise ValueError("Both current and candidate admission must be observed paused")
        if any(item.expected_status != 503 or item.actual_status != 503 or (
            item.expected_error_code != "budget_protocol_fenced"
            or item.actual_error_code != "budget_protocol_fenced"
        ) for item in self.paused.checks):
            raise ValueError("A generic outage is not proof of a working protocol fence")


class InstallationPublicationReadback(StrictModel):
    observed_at: AwareDatetime
    maintenance: MaintenanceReadback
    publisher_disabled: bool = Field(strict=True)
    current_revision: str
    parent_policy: str
    operations: dict[str, tuple[str, str, str]]
    dependencies: GatewayReleaseDependencies
    projected_models: dict[UUID, ModelAvailabilityState]
    availability: ModelAvailabilityCoverage
    availability_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def require_candidate(
        self, plan: BudgetInstallationPlan, bundle: InstallationPublicationBundle,
        *, promoted: bool, now: datetime,
    ) -> None:
        self.maintenance.require_paused(plan, now)
        if not self.publisher_disabled or not 0 <= (now - self.observed_at).total_seconds() <= 30:
            raise ValueError("Installation needs a fresh readback with the ordinary publisher off")
        candidate, previous = bundle.publication, bundle.previous
        identity = uuid5(plan.epoch, f"legacy-budget-publication:{candidate.gateway_profile_id}")
        if (candidate.id != identity
            or candidate.base_release_id != previous.id
            or candidate.gateway_profile_id != previous.gateway_profile_id
            or candidate.desired_spec != previous.desired_spec
            or candidate.desired_spec_sha256 != previous.desired_spec_sha256
            or candidate.status != PublicationStatus.VERIFYING
            or candidate.policy_sha256 != bundle.compiled.policy_sha256):
            raise ValueError("The pinned installation must preserve its identity and legacy routes")
        contracts = BudgetPublicationContracts.model_validate(
            bundle.publication.resource_manifest["budget_contracts"],
        )
        contracts.validate_publication(bundle.publication)
        if (contracts.epoch != plan.epoch
            or contracts.model_contracts != plan.expected_fence.model_contracts):
            raise ValueError("Publication contracts differ from the reviewed paused fence")
        if bundle.serving_lineage is not None:
            bundle.serving_lineage.require_previous(previous, now)
            if candidate.resource_manifest.get("installation_serving_lineage") != (
                bundle.serving_lineage.model_dump(mode="json")
            ):
                raise ValueError("Installation serving lineage changed after compilation")
        expected_current = (
            bundle.publication.apim_revision if promoted else bundle.serving_revision
        )
        if self.current_revision != expected_current:
            raise ValueError("APIM current revision does not match the installation phase")
        if self.maintenance.revision_policy_sha256.get(str(bundle.publication.apim_revision)) != (
            hashlib.sha256(self.parent_policy.encode()).hexdigest()
        ):
            raise ValueError("Candidate parent is not the policy pinned in maintenance evidence")
        contracts_by_policy = bundle.policy_contracts
        if component_digest(parse_policy(self.parent_policy), normalize_text_defaults=False) != (
            contracts_by_policy["parent"]
        ):
            raise ValueError("Candidate parent differs from the reviewed installation")
        if self.operations.keys() != bundle.operations.keys():
            raise ValueError("Candidate must have the exact complete operation inventory")
        for key, (method, path, source) in self.operations.items():
            operation, _ = bundle.operations[key]
            if (method != operation.method or path != operation.path
                or component_digest(parse_policy(source), normalize_text_defaults=False)
                != contracts_by_policy[key]):
                raise ValueError("Candidate operation contract differs: " + key)
        dependencies = self.dependencies
        if (not dependencies.recorded_complete or dependencies.live_status != "healthy"
            or dependencies.issues or dependencies.apim_revision != bundle.publication.apim_revision
            or dependencies.compiled_policy_sha256 != bundle.compiled.policy_sha256
            or dependencies.parent_policy_sha256
            != bundle.publication.resource_manifest["parent_policy_sha256"]
            or set(dependencies.backends + dependencies.backend_pools)
            != {value.id for value in bundle.compiled.backends}
            or set(dependencies.named_values)
            != {value.id for value in bundle.compiled.named_values}
            or set(dependencies.oauth_credentials)
            != {value.config.provider_id for value in bundle.compiled.oauth_credentials}):
            raise ValueError("Actual candidate dependencies have not been completely verified")
        snapshot = ModelAvailabilityPublication.model_validate(
            bundle.publication.resource_manifest["model_availability"],
        )
        if set(self.projected_models) != set(snapshot.models.values()) or any(
            key != state.model_id or state.lifecycle != "active"
            for key, state in self.projected_models.items()
        ):
            raise ValueError("Every existing catalog model needs its real availability projection")
        coverage = self.availability
        if (coverage.target_id != bundle.publication.gateway_profile_id
            or not coverage.current(now, self.availability_inventory_sha256)):
            raise ValueError("Current model availability coverage is required")
        for native in plan.expected_fence.gateways:
            for resource, _ in bundle.operations.values():
                if resource.method == "HEAD":
                    continue
                if not any(
                    (parts := surface.split("/"))[0] == native
                    and parts[-2:] == [bundle.publication.apim_revision, resource.id]
                    for surface in coverage.verified_surfaces
                ):
                    raise ValueError("Availability proof must cover every candidate surface")


def promote_installation_publication(
    plan: BudgetInstallationPlan, bundle: InstallationPublicationBundle,
    verification: InstallationProtocolVerification, journal: InstallationJournal,
    client: ApimPublisherClient,
    observe: Callable[[], InstallationPublicationReadback],
    *, now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """No retries: a timed-out promotion is recovered by reading the same revision."""
    if journal.plan.digest != plan.digest:
        raise ValueError("Publication journal belongs to another installation")
    before = observe()
    before.require_candidate(plan, bundle, promoted=False, now=now())
    verification.require_verified(bundle, plan.expected_fence, now())
    intent = {"publication_id": str(bundle.publication.id), "bundle_sha256": bundle.digest,
              "current_revision": bundle.publication.apim_revision, "mode": "paused",
              "sql_source_revision": bundle.previous.apim_revision,
              "serving_source_revision": bundle.serving_revision}

    def readback() -> dict[str, Any]:
        actual = observe()
        actual.require_candidate(plan, bundle, promoted=True, now=now())
        return dict(intent)

    journal.perform(
        "promote-budget-installation", intent=intent,
        apply=lambda: client.promote_revision(
            str(bundle.publication.apim_revision), f"budget-install-{plan.epoch.hex[:12]}",
        ), readback=readback,
    )


def recover_installation_promotion(
    plan: BudgetInstallationPlan, bundle: InstallationPublicationBundle,
    journal: InstallationJournal, observe: Callable[[], InstallationPublicationReadback],
    *, now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Certify an unknown promotion by reads only; never promote a replacement ID."""
    if journal.plan.digest != plan.digest:
        raise ValueError("Publication journal belongs to another installation")

    def readback() -> dict[str, Any]:
        actual = observe()
        actual.require_candidate(plan, bundle, promoted=True, now=now())
        return {"publication_id": str(bundle.publication.id), "bundle_sha256": bundle.digest,
                "current_revision": bundle.publication.apim_revision, "mode": "paused",
                "sql_source_revision": bundle.previous.apim_revision,
                "serving_source_revision": bundle.serving_revision}

    journal.recover("promote-budget-installation", readback)


def record_promoted_installation(
    connection: Any, plan: BudgetInstallationPlan, bundle: InstallationPublicationBundle,
    verification: InstallationProtocolVerification,
    observe: Callable[[], InstallationPublicationReadback],
    *, now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> GatewayPublication:
    """Import observed promotion atomically using a dictionary-row SQL connection.

    The caller journals this SQL write before invoking it. Recovery reads the
    same publication ID; it does not issue another promotion or new publication.
    No reservations, balances, budget rows, or rollout epochs are changed here.
    """
    publication = bundle.publication
    expected_id = uuid5(plan.epoch, f"legacy-budget-publication:{publication.gateway_profile_id}")
    if publication.id != expected_id:
        raise ValueError("Installation publication must retain its epoch-bound identity")
    verification.require_verified(bundle, plan.expected_fence, now())
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout = '5s'")
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"gateway-publication:{publication.gateway_profile_id}",),
        )
        existing = connection.execute(
            "SELECT id FROM gateway_publication WHERE id = %s", (publication.id,),
        ).fetchone()
        if existing is not None:
            raise ValueError("A recorded installation must be read back, never replayed")
        effective = connection.execute(
            "SELECT publication_id FROM effective_gateway_release "
            "WHERE gateway_profile_id = %s FOR UPDATE", (publication.gateway_profile_id,),
        ).fetchone()
        if effective is None or effective["publication_id"] != bundle.previous.id:
            raise ValueError("The previous effective release changed before handoff")
        previous = connection.execute(
            "SELECT * FROM gateway_publication WHERE id = %s FOR UPDATE", (bundle.previous.id,),
        ).fetchone()
        if previous is None or GatewayPublication.model_validate(previous) != bundle.previous:
            raise ValueError("The reviewed legacy publication changed before handoff")
        pending = connection.execute(
            "SELECT count(*) AS total FROM gateway_publication "
            "WHERE gateway_profile_id = %s AND status NOT IN "
            "('active','failed','superseded','rolled_back')", (publication.gateway_profile_id,),
        ).fetchone()
        if pending is None or pending["total"]:
            raise ValueError("Installation cannot overtake another publication")
        generation = connection.execute(
            "SELECT COALESCE(MAX(generation),0)+1 AS next FROM gateway_publication "
            "WHERE gateway_profile_id = %s", (publication.gateway_profile_id,),
        ).fetchone()
        if generation is None or generation["next"] != publication.generation:
            raise ValueError("Installation generation changed")
        rollout = connection.execute(
            "SELECT epoch, gateway_profile_ids FROM budget_execution_rollout "
            "WHERE state = 'active' AND scope_sync_enabled FOR SHARE",
        ).fetchone()
        if rollout is None or rollout["epoch"] != plan.epoch or (
            publication.gateway_profile_id not in rollout["gateway_profile_ids"]
        ):
            raise ValueError("Installation coordination epoch is not initialized")
        PostgreSqlPublicationRepositoryMixin._validate_publication_deployments(
            connection, publication.gateway_profile_id,
            publication.desired_spec.model_dump(mode="json"),
        )
        actual = observe()
        actual.require_candidate(plan, bundle, promoted=True, now=now())
        snapshots = actual.projected_models
        for model_id, projected in snapshots.items():
            row = connection.execute(
                "SELECT identity.revision, identity.lifecycle, model.enabled "
                "FROM model_availability_identity identity JOIN managed_model model "
                "ON model.id = identity.model_id WHERE model.id = %s FOR SHARE",
                (model_id,),
            ).fetchone()
            if row is None or (row["revision"], row["lifecycle"], row["enabled"]) != (
                projected.revision, projected.lifecycle, projected.enabled,
            ):
                raise ValueError("Projected model availability changed before handoff")
        actual.require_candidate(plan, bundle, promoted=True, now=now())
        manifest = {**publication.resource_manifest, "budget_installation": {
            "version": 1, "phase": "protocol_installed_admission_paused",
            "epoch": str(plan.epoch), "plan_sha256": plan.digest,
            "bundle_sha256": bundle.digest, "dependency_sha256": bundle.dependency_digest,
            "policy_sha256": bundle.policy_digests,
            "policy_contract_sha256": bundle.policy_contracts,
            "observed_current_revision": actual.current_revision,
            "sql_source_revision": bundle.previous.apim_revision,
            "serving_source_revision": bundle.serving_revision,
            "observed_at": actual.observed_at.isoformat(),
            "readback_sha256": _digest(actual.model_dump(mode="json")),
            "verification": verification.model_dump(mode="json"),
            "financial_activation": False,
        }}
        # The unchanged spec has a unique reusable index. Supersession and the
        # effective pointer move stay in this one post-promotion transaction.
        connection.execute(
            "UPDATE gateway_publication SET status='superseded', updated_at=now() "
            "WHERE id=%s AND status='active'", (bundle.previous.id,),
        )
        row = connection.execute(
            """INSERT INTO gateway_publication (
                id,gateway_profile_id,generation,publication_kind,desired_spec,
                desired_spec_sha256,status,base_release_id,apim_revision,policy_sha256,
                resource_manifest,created_by,created_at,started_at,updated_at
            ) VALUES (%s,%s,%s,'route_reconcile',%s,%s,'promoting',%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *""",
            (publication.id, publication.gateway_profile_id, publication.generation,
             Jsonb(publication.desired_spec.model_dump(mode="json")),
             publication.desired_spec_sha256,
             bundle.previous.id, publication.apim_revision, publication.policy_sha256,
             Jsonb(manifest), publication.created_by, publication.created_at,
             publication.started_at, now()),
        ).fetchone()
        if row is None:
            raise ValueError("Installation publication was not inserted")
        connection.execute(
            "INSERT INTO gateway_publication_outbox (publication_id) VALUES (%s)",
            (publication.id,),
        )
        connection.execute(
            "INSERT INTO gateway_publication_audit "
            "(publication_id,from_status,to_status,actor,detail) "
            "VALUES (%s,NULL,'promoting',%s,%s)",
            (publication.id, publication.created_by, Jsonb(manifest["budget_installation"])),
        )
        PostgreSqlPublicationRepositoryMixin._sync_active_route_bindings(connection, row)
        activated = PostgreSqlPublicationRepositoryMixin._complete_gateway_activation(
            connection, row, publication.id, publication.created_by,
        )
        result = GatewayPublication.model_validate(activated)
        if result.status != PublicationStatus.ACTIVE or result.resource_manifest != manifest:
            raise ValueError("Installation publication activation readback differs")
        return result
