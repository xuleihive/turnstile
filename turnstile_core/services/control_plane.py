from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4, uuid5

from pydantic import HttpUrl

from ..domain.application_access import (
    GatewayApplicationSubscriptionCreate,
    GatewayApplicationSubscriptionProvisionSpec,
    application_id_for,
    application_organization,
    application_subscription_id_for,
)
from ..domain.control_plane import (
    ApiFormat,
    AuthStrategy,
    DiscoveryModel,
    GatewayApplicationSubscriptionProvisionAccepted,
    GatewayBackendPoolConfig,
    GatewayBackendPoolMember,
    GatewayBackendPoolMemberWrite,
    GatewayBackendPoolWrite,
    GatewayCredentialRotation,
    GatewayModelBinding,
    GatewayPublication,
    GatewayPublicationCreate,
    GatewayPublicationRetry,
    GatewayPublicationView,
    GatewayReleaseAuditEvent,
    GatewayReleaseChangeSet,
    GatewayReleaseDependencies,
    GatewayReleaseDetail,
    GatewayReleaseDiff,
    GatewayReleaseIntegrity,
    GatewayReleaseList,
    GatewayReleaseOperation,
    GatewayReleaseOperationAccepted,
    GatewayReleaseOperationAuditEvent,
    GatewayReleaseOperationKind,
    GatewayReleaseProtection,
    GatewayReleaseProtectionAuditEvent,
    GatewayReleaseProtectionWrite,
    GatewayReleaseRetentionPolicy,
    GatewayReleaseRole,
    GatewayReleaseRollbackPreview,
    GatewayReleaseRollbackRequest,
    GatewayReleaseSpec,
    GatewayReleaseSummary,
    ModelCreateTarget,
    ModelRemovalTarget,
    ModelTarget,
    MultiTargetPublicationCreate,
    PublicationKind,
    PublicationStatus,
    SelfHostedGatewayProvisionOperations,
    StreamingMode,
    publication_credential_evidence,
    publication_credential_states,
    publication_failure,
    publication_materialized_named_values,
    publication_missing_credential_source,
    publication_provisioning_completed,
    publication_retry_requires_credential,
)
from ..domain.gateway_targets import (
    SELF_HOSTED_GATEWAY_IMAGE,
    ApimTarget,
    ApimTargetArmInventory,
    ApimTargetConnectionState,
    ApimTargetDriftState,
    ApimTargetRemovalPreview,
    ApimTargetRouteBinding,
    ApimTargetTopology,
    ApimTargetTopologyList,
    CapabilitySupport,
    GatewayDeclaredState,
    GatewayDeployment,
    GatewayDeploymentInventory,
    GatewayDeploymentKind,
    GatewayDeploymentScope,
    GatewayDeploymentScopeMode,
    GatewayDeploymentTopology,
    GatewayHostingMode,
    GatewayLifecycleOwnership,
    GatewayPublishedModelSummary,
    GatewayReachabilityState,
    GatewayReleaseGate,
    GatewayServingDeployment,
    MultiTargetRollout,
    SelfHostedGatewayAssociationChange,
    SelfHostedGatewayBootstrap,
    SelfHostedGatewayBootstrapRequest,
    SelfHostedGatewayCreate,
    SelfHostedGatewayNameWrite,
    SelfHostedGatewayProvisionSpec,
    SelfHostedGatewayRemovalPreview,
    SelfHostedGatewaySummary,
    SelfHostedGatewayWrite,
    TargetRolloutChild,
    TargetRolloutChildStatus,
    TargetRolloutKind,
    derive_target_rollout_status,
    gateway_deployment_id_for,
    gateway_provision_digest,
    self_hosted_gateway_native_id_for,
    validate_gateway_provision_target,
)
from ..domain.image_profiles import (
    ImageGenerationLimits,
    create_image_profile,
    validate_image_profile,
)
from ..domain.runtime_models import (
    DATABRICKS_ANTHROPIC_PATH,
    DATABRICKS_INFERENCE_RESOURCE,
    FOUNDRY_INFERENCE_RESOURCE,
    FOUNDRY_INFERENCE_ROLE_ID,
    BrandKey,
    ModelCapability,
    ModelFamilyKey,
    ModelVendorKey,
    OAuthClientCredentialsConfig,
    PriceSource,
    databricks_connection_config,
    databricks_workspace_url,
    foundry_runtime_name,
    model_vendor_label,
    openai_compatible_endpoint_values,
    openai_compatible_runtime_name,
)
from ..domain.storage_routes import (
    ProgramCapabilityProof,
    ProgramIdentity,
    RouteConfigurationPlan,
    RouteContent,
    RouteSnapshot,
)
from ..integrations.apim_control_plane_contract import (
    ApimPublisherClientResolver,
    SelfHostedGatewayManagementClient,
)
from ..integrations.gateway_connection_probe import (
    GatewayConnectionProbe,
    GatewayConnectionProbeError,
)
from ..persistence.repository import QueryRepository
from ..pricing.catalog import CompositeCatalog, build_default_catalog, parse_context_tier
from ..pricing.context import context_price_reference, resolve_context_prices
from ..security import CredentialCipher
from .directory_catalog import catalog_for


class ControlPlaneConflictError(ValueError):
    pass


class ControlPlaneNotFoundError(ValueError):
    pass


class ControlPlaneUnavailableError(RuntimeError):
    pass


_RELEASE_WORKER_UNAVAILABLE = (
    "Gateway Release operation worker is not enabled in this environment"
)
_APPLICATION_PROVISIONING_UNAVAILABLE = (
    "Application subscription provisioning is not enabled in this environment"
)
_APPLICATION_PRODUCT_ID = "turnstile-ai-consumers"
_MULTI_TARGET_ROLLOUT_NAMESPACE = UUID("29d93b9a-c811-4de7-9aad-123e67e8549e")
_MULTI_TARGET_MODEL_NAMESPACE = UUID("f29bf413-870d-4c8b-9892-3d4bb39b6f70")


@dataclass(frozen=True)
class _PreparedModelPublication:
    spec: GatewayReleaseSpec
    credential_ciphertext: bytes | None
    expected_base_release_id: UUID | None = None


def prepare_route_configuration(
    previous: Sequence[RouteContent],
    snapshot: RouteSnapshot,
    program: ProgramIdentity,
    proof: ProgramCapabilityProof,
    active_budget_epoch: str,
    now: int,
) -> RouteConfigurationPlan:
    return RouteConfigurationPlan.prepare(
        previous, snapshot, program, proof, active_budget_epoch, now,
    )


class GatewayControlPlaneService:
    def __init__(
        self,
        repository: QueryRepository,
        cipher: CredentialCipher | None = None,
        apim_principal_id: str | None = None,
        retention_policy: GatewayReleaseRetentionPolicy | None = None,
        release_worker_enabled: bool = True,
        application_provisioning_enabled: bool = True,
        application_default_token_limit: int = 100_000,
        application_default_tokens_per_minute: int = 100_000,
        application_product_id: str = _APPLICATION_PRODUCT_ID,
        image_generation_enabled: bool = False,
        image_generation_defaults: ImageGenerationLimits | None = None,
        databricks_oauth_enabled: bool = False,
        target_inventory_sync_enabled: bool = False,
        deployment_scoping_enabled: bool = False,
        target_registry_enabled: bool = False,
        multi_target_rollout_enabled: bool = False,
        gateway_connection_probe: GatewayConnectionProbe | None = None,
        trusted_gateway_endpoints: Mapping[UUID, HttpUrl] | None = None,
        onboarding_available: bool = False,
        price_catalog: CompositeCatalog | None = None,
        self_hosted_gateway_provisioning_enabled: bool = False,
        gateway_bootstrap_client_resolver: ApimPublisherClientResolver | None = None,
        gateway_bootstrap_image: str = SELF_HOSTED_GATEWAY_IMAGE,
    ) -> None:
        self._repository = repository
        self._price_catalog = price_catalog or build_default_catalog()
        self._cipher = cipher
        self._apim_principal_id = apim_principal_id
        self._databricks_oauth_enabled = databricks_oauth_enabled
        self._release_worker_enabled = release_worker_enabled
        self._application_provisioning_enabled = application_provisioning_enabled
        self._application_default_token_limit = application_default_token_limit
        self._application_default_tokens_per_minute = application_default_tokens_per_minute
        self._application_product_id = application_product_id
        self._image_generation_defaults = image_generation_defaults
        self._image_generation_enabled = image_generation_enabled
        self._target_inventory_sync_enabled = target_inventory_sync_enabled
        self._deployment_scoping_enabled = deployment_scoping_enabled
        self._target_registry_enabled = target_registry_enabled
        self._onboarding_available = onboarding_available
        self._self_hosted_gateway_provisioning_enabled = self_hosted_gateway_provisioning_enabled
        self._gateway_bootstrap_client_resolver = gateway_bootstrap_client_resolver
        self._gateway_bootstrap_image = gateway_bootstrap_image
        self._multi_target_rollout_enabled = multi_target_rollout_enabled
        self._gateway_connection_probe = gateway_connection_probe
        self._trusted_gateway_endpoints = {
            deployment_id: str(endpoint).rstrip("/")
            for deployment_id, endpoint in (trusted_gateway_endpoints or {}).items()
        }
        self._retention_policy = retention_policy or GatewayReleaseRetentionPolicy(
            retained_count=20,
            retained_days=180,
            failed_retained_days=30,
            protected_labels=["milestone", "rollback"],
        )

    def publications(
        self, gateway_profile_id: UUID | None = None, limit: int = 50
    ) -> list[GatewayPublication]:
        return [
            GatewayPublication.model_validate(row)
            for row in self._repository.list_gateway_publications(gateway_profile_id, limit)
        ]

    def archive_publication(self, publication_id: UUID, actor: str) -> None:
        row = self._repository.get_gateway_publication(publication_id)
        if row is None:
            raise ControlPlaneNotFoundError("Gateway publication not found")
        try:
            self._repository.archive_gateway_publication(publication_id, actor)
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error

    def publication(self, publication_id: UUID) -> GatewayPublication:
        row = self._repository.get_gateway_publication(publication_id)
        if row is None:
            raise ControlPlaneNotFoundError("Gateway publication not found")
        publication = GatewayPublication.model_validate(row)
        if "model_route_addition" in publication.resource_manifest:
            return publication
        audit = self._repository.list_gateway_publication_audit(publication_id)
        return self._materialize_publication(publication, audit)

    def _materialize_publication(
        self, publication: GatewayPublication, audit: Sequence[Mapping[str, Any]],
    ) -> GatewayPublication:
        if "model_route_addition" in publication.resource_manifest:
            return publication
        materialized = publication_materialized_named_values(
            publication,
            provisioning_completed=publication_provisioning_completed(audit),
        )
        if not materialized:
            inherited = self._inherited_named_value(publication)
            if inherited is not None:
                materialized = [inherited]
        if not materialized:
            return publication
        return publication.model_copy(
            update={
                "resource_manifest": {
                    **publication.resource_manifest,
                    "named_values": materialized,
                    "credential_evidence": publication.resource_manifest.get(
                        "credential_evidence",
                        publication_credential_evidence(
                            publication, {name: "present" for name in materialized},
                        ),
                    ),
                }
            }
        )

    def _inherited_named_value(self, publication: GatewayPublication) -> str | None:
        if (
            publication.publication_kind is not PublicationKind.MODEL_ADD
            or publication_missing_credential_source(publication)
        ):
            return None
        binding = publication.desired_spec.bindings[-1]
        states = publication_credential_states(publication)
        if states is not None and states.get(binding.named_value_name or "") != "present":
            return None
        if (
            publication.base_release_id is None
            or binding.runtime_id is None
            or binding.runtime_config.get("credential_provisioned") is False
            or binding.auth_strategy not in {
                AuthStrategy.NAMED_VALUE_BEARER, AuthStrategy.NAMED_VALUE_API_KEY,
            }
            or binding.key_vault_secret_id is not None
            or binding.named_value_name is None
            or binding.backend_url is None
        ):
            return None
        row = self._repository.get_gateway_publication(publication.base_release_id)
        if row is None:
            return None
        try:
            base = GatewayPublication.model_validate(row)
        except ValueError:
            return None
        recorded = base.resource_manifest.get("named_values")
        if (
            base.gateway_profile_id != publication.gateway_profile_id
            or base.status not in {PublicationStatus.ACTIVE, PublicationStatus.SUPERSEDED}
            or base.apim_revision is None
            or not isinstance(recorded, list)
            or binding.named_value_name not in recorded
        ):
            return None
        for previous in base.desired_spec.bindings:
            if (
                previous.named_value_name == binding.named_value_name
                and previous.auth_strategy == binding.auth_strategy
                and previous.key_vault_secret_id is None
                and previous.provider_id in {None, binding.provider_id}
                and previous.runtime_id in {None, binding.runtime_id}
                and previous.provider_kind == binding.provider_kind
                and previous.provider_brand_key == binding.provider_brand_key
                and previous.runtime_name == binding.runtime_name
                and previous.api_format == binding.api_format
                and previous.backend_path == binding.backend_path
                and str(previous.backend_url).rstrip("/") == str(binding.backend_url).rstrip("/")
            ):
                return binding.named_value_name
        return None

    def releases(
        self, gateway_profile_id: UUID | None = None, limit: int = 100
    ) -> GatewayReleaseList:
        publications = self.publications(gateway_profile_id, limit)
        base_ids = {
            publication.base_release_id
            for publication in publications
            if publication.base_release_id is not None
        }
        related = {
            publication.id: publication
            for publication in publications
        }
        related.update(
            {
                publication.id: publication
                for row in self._repository.get_gateway_publications(list(base_ids))
                if (publication := GatewayPublication.model_validate(row))
            }
        )
        integrity = {
            UUID(str(row["publication_id"])): row
            for row in self._repository.latest_gateway_release_integrity_snapshots(
                [publication.id for publication in publications]
            )
        }
        contexts = self._release_contexts(publications, related)
        retention = self._release_retention(publications, contexts)
        return GatewayReleaseList(
            items=[
                self._release_summary(
                    publication,
                    contexts[publication.gateway_profile_id],
                    retention[publication.id],
                    (
                        related.get(publication.base_release_id)
                        if publication.base_release_id is not None
                        else None
                    ),
                    self._release_dependencies_from_snapshot(
                        publication, integrity.get(publication.id)
                    ),
                )
                for publication in publications
            ],
            retention_policy=self._retention_policy,
            operations_enabled=self._release_worker_enabled,
            operations_disabled_reason=(
                None if self._release_worker_enabled else _RELEASE_WORKER_UNAVAILABLE
            ),
        )

    def release(self, release_id: UUID) -> GatewayReleaseDetail:
        row = self._repository.get_gateway_publication(release_id)
        if row is None:
            raise ControlPlaneNotFoundError("Gateway publication not found")
        audit_rows = self._repository.list_gateway_publication_audit(release_id)
        publication = self._materialize_publication(
            GatewayPublication.model_validate(row), audit_rows,
        )
        related = self.publications(publication.gateway_profile_id, 1000)
        context = self._release_contexts(related)[publication.gateway_profile_id]
        retention = self._release_retention(
            related, {publication.gateway_profile_id: context}
        )
        against = next((candidate for candidate in related
                        if candidate.id == publication.base_release_id), None)
        if against is None:
            against = self._optional_publication(publication.base_release_id)
        dependencies = self._release_dependencies(publication)
        summary = self._release_summary(
            publication,
            context,
            retention[publication.id],
            against,
            dependencies,
        )
        audit = []
        for row in audit_rows:
            event = GatewayReleaseAuditEvent.model_validate(row)
            if "error_message" in event.detail or "error_code" in event.detail:
                event_failure = publication_failure(publication.model_copy(update={
                    "status": PublicationStatus.FAILED,
                    "error_code": event.detail.get("error_code"),
                    "error_message": event.detail.get("error_message"),
                }), stage=event.from_status)
                event = event.model_copy(update={
                    "detail": {"failure": event_failure.model_dump(mode="json")}
                    if event_failure else {},
                })
            audit.append(event)
        failure_stage = next(
            (event.from_status for event in reversed(audit)
             if event.to_status is PublicationStatus.FAILED), None,
        )
        summary = summary.model_copy(update={
            "failure": publication_failure(publication, stage=failure_stage),
        })
        protection_audit = [
            GatewayReleaseProtectionAuditEvent.model_validate(row)
            for row in self._repository.list_gateway_release_protection_audit(
                publication.id
            )
        ]
        return GatewayReleaseDetail(
            **summary.model_dump(),
            base_release_id=publication.base_release_id,
            diff_from_base=GatewayReleaseDiff(
                release_id=publication.id,
                against_release_id=against.id if against else None,
                changes=self._release_changes(publication, against),
            ),
            dependencies=dependencies,
            audit=audit,
            protection_audit=protection_audit,
        )

    def release_diff(
        self, release_id: UUID, against_release_id: UUID | None = None
    ) -> GatewayReleaseDiff:
        release = self.publication(release_id)
        against = self._optional_publication(
            against_release_id if against_release_id is not None else release.base_release_id
        )
        if against is not None and against.gateway_profile_id != release.gateway_profile_id:
            raise ControlPlaneConflictError("Gateway releases belong to different gateways")
        return GatewayReleaseDiff(
            release_id=release.id,
            against_release_id=against.id if against else None,
            changes=self._release_changes(release, against),
        )

    def release_integrity(self, release_id: UUID) -> GatewayReleaseIntegrity:
        publication = self.publication(release_id)
        related = self.publications(publication.gateway_profile_id, 1000)
        context = self._release_contexts(related)[publication.gateway_profile_id]
        current = context.get("current_publication")
        dependencies = self._release_dependencies(publication)
        blockers = self._rollback_blockers(
            publication,
            dependencies,
            is_current=publication.id == context["current_id"],
            current=current if isinstance(current, GatewayPublication) else None,
        )
        return GatewayReleaseIntegrity(
            release_id=publication.id,
            dependencies=dependencies,
            rollback_eligible=not blockers,
            rollback_blockers=blockers,
        )

    def protect_release(
        self,
        release_id: UUID,
        write: GatewayReleaseProtectionWrite,
        actor: str,
    ) -> GatewayReleaseProtection | None:
        self.publication(release_id)
        if (
            write.protected_label is not None
            and write.protected_label not in self._retention_policy.protected_labels
        ):
            raise ControlPlaneConflictError("Unsupported release protection label")
        row = self._repository.set_gateway_release_protection(
            release_id,
            pinned=write.pinned,
            protected_label=write.protected_label,
            retain_until=write.retain_until,
            updated_by=actor,
        )
        return GatewayReleaseProtection.model_validate(row) if row is not None else None

    def rollback_preview(self, release_id: UUID) -> GatewayReleaseRollbackPreview:
        target = self.publication(release_id)
        current_row = self._repository.effective_gateway_publication(
            target.gateway_profile_id
        )
        if current_row is None:
            raise ControlPlaneConflictError("The gateway has no effective release")
        current = GatewayPublication.model_validate(current_row)
        dependencies = self._release_dependencies(target)
        blockers = self._rollback_blockers(
            target, dependencies, is_current=target.id == current.id, current=current,
        )
        changes = self._release_changes(target, current)
        payload = {
            "target_release_id": str(target.id),
            "target_desired_spec_sha256": target.desired_spec_sha256,
            "target_policy_sha256": target.policy_sha256,
            "current_release_id": str(current.id),
            "current_desired_spec_sha256": current.desired_spec_sha256,
            "current_policy_sha256": current.policy_sha256,
            "changes": changes.model_dump(mode="json"),
            "dependencies": dependencies.model_dump(
                mode="json", exclude={"live_checked_at"}
            ),
        }
        confirmation_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return GatewayReleaseRollbackPreview(
            target_release_id=target.id,
            current_release_id=current.id,
            changes=changes,
            dependencies=dependencies,
            rollback_eligible=not blockers,
            rollback_blockers=blockers,
            confirmation_sha256=confirmation_sha256,
        )

    def request_rollback(
        self,
        release_id: UUID,
        request: GatewayReleaseRollbackRequest,
        actor: str,
    ) -> GatewayReleaseOperationAccepted:
        self._require_release_worker()
        preview = self.rollback_preview(release_id)
        if not preview.rollback_eligible:
            raise ControlPlaneConflictError(
                f"Release is not rollback eligible: {', '.join(preview.rollback_blockers)}"
            )
        if not hmac.compare_digest(
            preview.confirmation_sha256, request.confirmation_sha256
        ):
            raise ControlPlaneConflictError("Rollback preview changed; confirm it again")
        target = self.publication(release_id)
        try:
            row = self._repository.create_gateway_release_operation(
                target.gateway_profile_id,
                GatewayReleaseOperationKind.ROLLBACK.value,
                actor,
                target_release_id=target.id,
                prior_release_id=preview.current_release_id,
                confirmation_sha256=preview.confirmation_sha256,
                semantic_preview=preview.model_dump(mode="json"),
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        operation = self._operation_view(row)
        return GatewayReleaseOperationAccepted(
            operation=operation,
            status_url=f"/api/v1/model-management/release-operations/{operation.id}",
        )

    def request_integrity_check(
        self, release_id: UUID, actor: str
    ) -> GatewayReleaseOperationAccepted:
        self._require_release_worker()
        release = self.publication(release_id)
        try:
            row = self._repository.create_gateway_release_operation(
                release.gateway_profile_id,
                GatewayReleaseOperationKind.INTEGRITY_CHECK.value,
                actor,
                target_release_id=release.id,
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        operation = self._operation_view(row)
        return GatewayReleaseOperationAccepted(
            operation=operation,
            status_url=f"/api/v1/model-management/release-operations/{operation.id}",
        )

    def request_gc_plan(
        self, gateway_profile_id: UUID, actor: str
    ) -> GatewayReleaseOperationAccepted:
        self._require_release_worker()
        current = self._repository.effective_gateway_publication(gateway_profile_id)
        if current is None:
            raise ControlPlaneConflictError("The gateway has no effective release")
        try:
            row = self._repository.create_gateway_release_operation(
                gateway_profile_id,
                GatewayReleaseOperationKind.GC_PLAN.value,
                actor,
                prior_release_id=UUID(str(current["id"])),
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        operation = self._operation_view(row)
        return GatewayReleaseOperationAccepted(
            operation=operation,
            status_url=f"/api/v1/model-management/release-operations/{operation.id}",
        )

    def apim_target_topologies(
        self, registry: Mapping[str, Sequence[dict[str, Any]]] | None = None,
    ) -> ApimTargetTopologyList:
        if registry is None:
            registry = self._repository.registry()
        active_gateway_ids = {
            UUID(str(row["id"]))
            for row in registry["gateways"]
            if row["implementation"] == "apim" and row["enabled"]
        }
        targets = [
            row
            for row in self._repository.list_apim_targets()
            if UUID(str(row["gateway_profile_id"])) in active_gateway_ids
        ]
        target_ids = [UUID(str(row["gateway_profile_id"])) for row in targets]
        bindings = self._repository.list_apim_target_route_bindings(apim_target_ids=target_ids)
        deployments = self._repository.list_gateway_deployments_for_targets(target_ids)
        inventory_rows = self._repository.list_gateway_deployment_inventories(
            [UUID(str(row["id"])) for row in deployments]
        )
        bindings_by_target: dict[UUID, list[dict[str, Any]]] = {}
        deployments_by_target: dict[UUID, list[dict[str, Any]]] = {}
        for row in bindings:
            bindings_by_target.setdefault(UUID(str(row["apim_target_id"])), []).append(row)
        for row in deployments:
            deployments_by_target.setdefault(UUID(str(row["apim_target_id"])), []).append(row)
        items = [
            self._apim_target_topology(
                row,
                registry=registry,
                route_bindings=bindings_by_target.get(target_id, []),
                deployment_rows=deployments_by_target.get(target_id, []),
                inventory_rows=inventory_rows,
            )
            for row, target_id in zip(targets, target_ids, strict=True)
        ]
        return ApimTargetTopologyList(
            items=items,
            total=len(items),
            inventory_sync_available=self._target_inventory_sync_enabled,
            inventory_sync_unavailable_reason=(
                None
                if self._target_inventory_sync_enabled
                else "APIM target inventory synchronization is not enabled"
            ),
            resource_lifecycle_available=True,
            onboarding_available=self._onboarding_available,
            self_hosted_gateway_name_edit_supported=True,
            self_hosted_gateway_provisioning_available=(
                self._self_hosted_gateway_provisioning_enabled and self._release_worker_enabled
            ),
        )

    def apim_target_topology(self, gateway_profile_id: UUID) -> ApimTargetTopology:
        row = self._active_apim_target_row(gateway_profile_id)
        return self._apim_target_topology(row)

    def request_self_hosted_gateway_provision(
        self,
        apim_target_id: UUID,
        write: SelfHostedGatewayCreate,
        request_id: UUID,
        actor: str,
    ) -> GatewayReleaseOperationAccepted:
        self._require_gateway_provisioning()
        target = self._apim_target(self._active_apim_target_row(apim_target_id)).model_copy(
            update={"probe_secret_ref": None},
        )
        native_id = self_hosted_gateway_native_id_for(apim_target_id, request_id)
        spec = SelfHostedGatewayProvisionSpec(
            request_id=request_id,
            target=target,
            configuration=write,
            native_gateway_id=native_id,
            deployment_id=gateway_deployment_id_for(apim_target_id, native_id),
        )
        try:
            validate_gateway_provision_target(spec, target.model_dump())
            if target.drift_state != "in_sync":
                raise ValueError("Synchronize the APIM target before creating a Gateway")
            row = self._repository.create_gateway_release_operation(
                apim_target_id,
                GatewayReleaseOperationKind.SELF_HOSTED_GATEWAY_PROVISION,
                actor,
                confirmation_sha256=gateway_provision_digest(spec),
                semantic_preview=spec.model_dump(mode="json"),
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        return GatewayReleaseOperationAccepted(
            operation=self._operation_view(row),
            status_url=f"/api/v1/model-management/release-operations/{row['id']}",
        )

    def _require_gateway_provisioning(self) -> None:
        self._require_release_worker()
        if not self._self_hosted_gateway_provisioning_enabled:
            raise ControlPlaneUnavailableError("Self-hosted Gateway creation is not enabled")

    def self_hosted_gateway_provisions(self) -> SelfHostedGatewayProvisionOperations:
        available = self._self_hosted_gateway_provisioning_enabled and self._release_worker_enabled
        return SelfHostedGatewayProvisionOperations(
            available=available,
            unavailable_reason=None if available else "Self-hosted Gateway creation is not enabled",
            items=[
                self._operation_view(row)
                for row in self._repository.list_self_hosted_gateway_provisions()
            ],
        )

    def retry_self_hosted_gateway_provision(
        self,
        operation_id: UUID,
        actor: str,
    ) -> GatewayReleaseOperationAccepted:
        self._require_gateway_provisioning()
        row = self._repository.get_gateway_release_operation(operation_id)
        if row is None or row["operation_kind"] != "self_hosted_gateway_provision":
            raise ControlPlaneNotFoundError("Gateway creation operation was not found")
        try:
            spec = SelfHostedGatewayProvisionSpec.model_validate(row["semantic_preview"])
            validate_gateway_provision_target(
                spec, self._active_apim_target_row(spec.target.gateway_profile_id)
            )
            existing = self._repository.get_gateway_deployment(spec.deployment_id)
            if existing and existing.get("retired_at") is not None:
                raise ValueError("The Gateway has been removed; create a new request")
            retried = self._repository.retry_self_hosted_gateway_provision(operation_id, actor)
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        return GatewayReleaseOperationAccepted(
            operation=self._operation_view(retried),
            status_url=f"/api/v1/model-management/release-operations/{operation_id}",
        )

    def _gateway_provision_spec(
        self, deployment: GatewayDeployment
    ) -> SelfHostedGatewayProvisionSpec:
        row = (
            self._repository.get_gateway_release_operation(deployment.provisioning_operation_id)
            if deployment.provisioning_operation_id
            else None
        )
        if (
            row is None
            or row["status"] != "succeeded"
            or row["operation_kind"] != "self_hosted_gateway_provision"
        ):
            raise ControlPlaneConflictError("Gateway creation has not completed")
        try:
            spec = SelfHostedGatewayProvisionSpec.model_validate(row["semantic_preview"])
            if (
                spec.deployment_id != deployment.id
                or spec.target.gateway_profile_id != deployment.apim_target_id
                or spec.native_gateway_id != deployment.native_gateway_id
                or spec.configuration.endpoint_url != deployment.endpoint_url
                or row["confirmation_sha256"] != gateway_provision_digest(spec)
            ):
                raise ValueError("Gateway configuration changed after creation")
            validate_gateway_provision_target(
                spec, self._active_apim_target_row(deployment.apim_target_id)
            )
        except ValueError as error:
            raise ControlPlaneConflictError(
                "Gateway creation identity is no longer verified"
            ) from error
        return spec

    def self_hosted_gateway_bootstrap(
        self,
        deployment_id: UUID,
        request: SelfHostedGatewayBootstrapRequest,
    ) -> SelfHostedGatewayBootstrap:
        self._require_gateway_provisioning()
        if self._gateway_bootstrap_client_resolver is None:
            raise ControlPlaneUnavailableError("Gateway startup configuration is unavailable")
        deployment = self._active_self_hosted_gateway(deployment_id)
        spec = self._gateway_provision_spec(deployment)
        resolved = self._gateway_bootstrap_client_resolver.resolve(deployment.apim_target_id)
        if resolved.gateway_profile_id != deployment.apim_target_id:
            raise ControlPlaneConflictError("Gateway bootstrap resolved another APIM target")
        if not isinstance(resolved.client, SelfHostedGatewayManagementClient):
            raise ControlPlaneUnavailableError("Gateway startup configuration is unavailable")
        return resolved.client.self_hosted_gateway_bootstrap(
            spec, request, self._gateway_bootstrap_image
        )

    def add_self_hosted_gateway(
        self,
        apim_target_id: UUID,
        write: SelfHostedGatewayWrite,
    ) -> GatewayDeploymentTopology:
        target = self._apim_target(
            self._active_apim_target_row(apim_target_id)
        )
        if (
            target.capabilities.self_hosted_gateway
            is CapabilitySupport.UNSUPPORTED
        ):
            raise ControlPlaneConflictError(
                "The APIM target does not support Self-hosted Gateway"
            )
        return self._save_self_hosted_gateway(apim_target_id, write)

    def rename_self_hosted_gateway(
        self, deployment_id: UUID, write: SelfHostedGatewayNameWrite,
    ) -> None:
        deployment = self._active_self_hosted_gateway(deployment_id)
        if deployment.display_name == write.display_name:
            return
        if not self._repository.rename_gateway_deployment(deployment_id, write.display_name):
            raise ControlPlaneNotFoundError("Self-hosted Gateway not found")

    def validate_self_hosted_gateway(
        self, deployment_id: UUID
    ) -> GatewayDeploymentTopology:
        deployment = self._active_self_hosted_gateway(deployment_id)
        if self._gateway_connection_probe is None or self._cipher is None:
            raise ControlPlaneUnavailableError(
                "Self-hosted Gateway connection validation is not configured"
            )
        if deployment.endpoint_url is None:
            raise ControlPlaneConflictError(
                "The Self-hosted Gateway has no endpoint to validate"
            )
        inventory_row = self._repository.get_gateway_deployment_inventory(
            deployment.id
        )
        if inventory_row is None:
            raise ControlPlaneConflictError(
                "Synchronize the APIM inventory before validating this Gateway"
            )
        inventory = self._gateway_deployment_inventory(inventory_row)
        target = self._apim_target(
            self._active_apim_target_row(deployment.apim_target_id)
        )
        if (
            deployment.declared_state is not GatewayDeclaredState.PRESENT
            or not any(
                api_id.casefold() == target.api_id.casefold()
                for api_id in inventory.associated_api_ids
            )
        ):
            raise ControlPlaneConflictError(
                "The Self-hosted Gateway is not associated with the target APIM API"
            )
        endpoint_host = (deployment.endpoint_url.host or "").casefold()
        declared_hosts = {
            item.hostname.casefold()
            for item in inventory.hostname_configurations
        }
        provisioned_trust = bool(
            deployment.provisioning_operation_id and self._gateway_provision_spec(deployment)
        )
        if (
            not provisioned_trust and endpoint_host not in declared_hosts
            and self._trusted_gateway_endpoints.get(deployment.id)
            != str(deployment.endpoint_url).rstrip("/")
        ):
            raise ControlPlaneConflictError(
                "The Gateway endpoint host is not declared in APIM hostname inventory"
            )
        profile = next(
            (
                row
                for row in self._repository.registry()["gateways"]
                if UUID(str(row["id"])) == deployment.apim_target_id
            ),
            None,
        )
        if profile is None:
            raise ControlPlaneNotFoundError("APIM target not found")
        credential = self._cipher.decrypt(profile.get("credential_ciphertext"))
        if credential is None:
            raise ControlPlaneUnavailableError(
                "The APIM target has no data-plane validation credential"
            )
        config = profile.get("config")
        header_name = (
            str(config.get("header_name") or "")
            if isinstance(config, Mapping)
            else ""
        )
        if not header_name:
            raise ControlPlaneUnavailableError(
                "The APIM target has no authentication header configuration"
            )
        validated_at = datetime.now(UTC)
        try:
            self._gateway_connection_probe.validate(
                deployment.endpoint_url,
                header_name=header_name,
                credential=credential,
            )
        except GatewayConnectionProbeError as error:
            self._repository.upsert_gateway_deployment(
                deployment.model_copy(
                    update={
                        "reachability_state": GatewayReachabilityState.UNREACHABLE,
                        "last_probe_at": validated_at,
                    }
                ),
                preserve_display_name=True,
            )
            raise ControlPlaneConflictError(str(error)) from error
        updated = self._gateway_deployment(
            self._repository.upsert_gateway_deployment(
                deployment.model_copy(
                    update={
                        "reachability_state": GatewayReachabilityState.REACHABLE,
                        "last_probe_at": validated_at,
                    }
                ),
                preserve_display_name=True,
            )
        )
        return GatewayDeploymentTopology(
            deployment=updated,
            inventory=inventory,
            configuration_endpoint=HttpUrl(
                f"https://{target.service_name}.configuration.azure-api.net"
            ),
            published_models=self._published_models_for_deployment(updated),
        )

    def self_hosted_gateway_removal_preview(
        self, deployment_id: UUID
    ) -> SelfHostedGatewayRemovalPreview:
        deployment = self._active_self_hosted_gateway(deployment_id)
        models = self._published_models_for_deployment(deployment)
        blockers: tuple[str, ...] = (
            ("Remove or move the Gateway's published models first",)
            if models
            else ()
        )
        if any(
            row["status"] not in {"active", "failed", "superseded", "rolled_back"}
            for row in self._repository.all_gateway_publications(deployment.apim_target_id)
        ):
            blockers += ("Wait for the APIM publication to finish before changing this Gateway",)
        return SelfHostedGatewayRemovalPreview(
            deployment_id=deployment.id,
            display_name=deployment.display_name,
            model_count=len(models),
            models=models,
            removal_allowed=not blockers,
            blockers=blockers,
        )

    def remove_self_hosted_gateway(
        self, deployment_id: UUID
    ) -> SelfHostedGatewayRemovalPreview:
        preview = self.self_hosted_gateway_removal_preview(deployment_id)
        if not preview.removal_allowed:
            raise ControlPlaneConflictError("; ".join(preview.blockers))
        deployment = self._active_self_hosted_gateway(deployment_id)
        try:
            self._repository.upsert_gateway_deployment(
                deployment.model_copy(update={"retired_at": datetime.now(UTC)}),
                preserve_display_name=True,
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        return preview

    def change_self_hosted_gateway_association(
        self,
        deployment_id: UUID,
        write: SelfHostedGatewayAssociationChange,
    ) -> GatewayDeploymentTopology:
        current = self._active_self_hosted_gateway(deployment_id)
        if current.apim_target_id == write.apim_target_id:
            raise ControlPlaneConflictError(
                "The Self-hosted Gateway is already associated with this APIM target"
            )
        preview = self.self_hosted_gateway_removal_preview(deployment_id)
        if not preview.removal_allowed:
            raise ControlPlaneConflictError("; ".join(preview.blockers))
        target = self._apim_target(
            self._active_apim_target_row(write.apim_target_id)
        )
        if (
            target.capabilities.self_hosted_gateway
            is CapabilitySupport.UNSUPPORTED
        ):
            raise ControlPlaneConflictError(
                "The destination APIM target does not support Self-hosted Gateway"
            )
        replacement = self._save_self_hosted_gateway(
            write.apim_target_id,
            SelfHostedGatewayWrite.model_validate(
                {
                    name: getattr(write, name)
                    for name in SelfHostedGatewayWrite.model_fields
                }
            ),
            retiring_deployment=current,
        )
        return replacement

    def apim_target_removal_preview(
        self, gateway_profile_id: UUID
    ) -> ApimTargetRemovalPreview:
        target = self._apim_target(
            self._active_apim_target_row(gateway_profile_id)
        )
        deployments = [
            self._gateway_deployment(row)
            for row in self._repository.list_gateway_deployments(gateway_profile_id)
        ]
        self_hosted = tuple(
            SelfHostedGatewaySummary(
                id=deployment.id,
                native_gateway_id=deployment.native_gateway_id,
                display_name=deployment.display_name,
            )
            for deployment in deployments
            if deployment.kind is GatewayDeploymentKind.SELF_HOSTED
            and deployment.endpoint_url is not None
        )
        registry = self._repository.registry()
        runtimes = [
            row
            for row in registry["runtimes"]
            if row.get("gateway_profile_id") == gateway_profile_id
        ]
        runtime_ids = {UUID(str(row["id"])) for row in runtimes}
        models = tuple(
            sorted(
                (
                    GatewayPublishedModelSummary(
                        id=UUID(str(row["id"])),
                        model_key=str(row["model_key"]),
                        display_name=str(row["display_name"]),
                    )
                    for row in registry["models"]
                    if UUID(str(row["runtime_id"])) in runtime_ids
                ),
                key=lambda item: (item.display_name.casefold(), str(item.id)),
            )
        )
        blockers: list[str] = []
        if self_hosted:
            blockers.append("Remove associated Self-hosted Gateways first")
        if models:
            blockers.append("Remove or move published models first")
        if runtimes:
            blockers.append("Remove model connections first")
        return ApimTargetRemovalPreview(
            gateway_profile_id=gateway_profile_id,
            service_name=target.service_name,
            self_hosted_gateway_count=len(self_hosted),
            runtime_count=len(runtimes),
            model_count=len(models),
            self_hosted_gateways=self_hosted,
            models=models,
            removal_allowed=not blockers,
            blockers=tuple(blockers),
        )

    def remove_apim_target(
        self, gateway_profile_id: UUID
    ) -> ApimTargetRemovalPreview:
        preview = self.apim_target_removal_preview(gateway_profile_id)
        if not preview.removal_allowed:
            raise ControlPlaneConflictError("; ".join(preview.blockers))
        for row in self._repository.list_gateway_deployments(gateway_profile_id):
            deployment = self._gateway_deployment(row)
            self._repository.upsert_gateway_deployment(
                deployment.model_copy(update={"retired_at": datetime.now(UTC)}),
                preserve_display_name=True,
            )
        updated = self._repository.update_registry_item(
            "gateway",
            gateway_profile_id,
            {"enabled": False, "is_default": False},
        )
        if updated is None:
            raise ControlPlaneNotFoundError("APIM target not found")
        return preview

    def request_target_inventory_sync(
        self, gateway_profile_id: UUID, actor: str
    ) -> GatewayReleaseOperationAccepted:
        self._require_release_worker()
        if not self._target_inventory_sync_enabled:
            raise ControlPlaneUnavailableError(
                "APIM target inventory synchronization is not enabled"
            )
        if self._repository.get_apim_target(gateway_profile_id) is None:
            raise ControlPlaneNotFoundError("APIM target not found")
        try:
            row = self._repository.create_gateway_release_operation(
                gateway_profile_id,
                GatewayReleaseOperationKind.TARGET_INVENTORY_SYNC.value,
                actor,
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        operation = self._operation_view(row)
        return GatewayReleaseOperationAccepted(
            operation=operation,
            status_url=f"/api/v1/model-management/release-operations/{operation.id}",
        )

    def apply_target_inventory(
        self, inventory: ApimTargetArmInventory
    ) -> ApimTargetTopology:
        row = self._repository.get_apim_target(inventory.gateway_profile_id)
        if row is None:
            raise ControlPlaneNotFoundError("APIM target not found")
        current = self._apim_target(row)
        if (
            current.last_inventory_sync_at is not None
            and inventory.discovered_at < current.last_inventory_sync_at
        ):
            return self.apim_target_topology(inventory.gateway_profile_id)
        identity_matches = (
            current.arm_resource_id.casefold() == inventory.arm_resource_id.casefold()
            and current.subscription_id == inventory.subscription_id
            and current.resource_group.casefold() == inventory.resource_group.casefold()
            and current.service_name.casefold() == inventory.service_name.casefold()
            and current.api_id.casefold() == inventory.api_id.casefold()
            and current.api_path == inventory.api_path
            and current.principal_id == inventory.principal_id
        )
        if not identity_matches:
            self._repository.upsert_apim_target(
                current.model_copy(
                    update={
                        "drift_state": ApimTargetDriftState.DRIFTED,
                        "last_inventory_sync_at": inventory.discovered_at,
                    }
                )
            )
            raise ControlPlaneConflictError(
                "APIM inventory does not match the immutable target identity"
            )
        updated_target = current.model_copy(
            update={
                "service_tier": inventory.service_tier,
                "tier_family": inventory.tier_family,
                "service_location": inventory.service_location,
                "principal_id": inventory.principal_id,
                "capabilities": inventory.capabilities,
                "last_inventory_sync_at": inventory.discovered_at,
                "last_validated_at": inventory.discovered_at,
                "drift_state": ApimTargetDriftState.IN_SYNC,
            }
        )
        existing = {
            str(item["native_gateway_id"]).casefold(): self._gateway_deployment(item)
            for item in self._repository.list_gateway_deployments(
                inventory.gateway_profile_id, include_retired=True
            )
        }
        managed = existing.get("managed")
        if managed is None:
            managed = GatewayDeployment(
                id=gateway_deployment_id_for(
                    inventory.gateway_profile_id, "managed"
                ),
                apim_target_id=inventory.gateway_profile_id,
                native_gateway_id="managed",
                kind=GatewayDeploymentKind.MANAGED,
                display_name="Azure-managed Gateway",
                location=inventory.service_location,
                endpoint_url=inventory.managed_gateway_url,
                hosting_mode=GatewayHostingMode.AZURE_MANAGED,
                lifecycle_ownership=GatewayLifecycleOwnership.EXTERNAL,
                release_gate=GatewayReleaseGate.REQUIRED,
            )
        managed = managed.model_copy(
            update={
                "display_name": "Azure-managed Gateway",
                "location": inventory.service_location,
                "endpoint_url": inventory.managed_gateway_url,
                "declared_state": GatewayDeclaredState.PRESENT,
                "last_arm_sync_at": inventory.discovered_at,
                "retired_at": None,
            }
        )
        self._repository.upsert_gateway_deployment(managed)
        self._repository.upsert_gateway_deployment_inventory(
            GatewayDeploymentInventory(
                deployment_id=managed.id,
                associated_api_ids=(inventory.api_id,),
                source_api_version=inventory.source_api_version,
                synced_at=inventory.discovered_at,
            )
        )

        observed_native_ids = {"managed"}
        for gateway in inventory.gateways:
            native_id = gateway.native_gateway_id.casefold()
            observed_native_ids.add(native_id)
            deployment = existing.get(native_id)
            if deployment is None:
                deployment = GatewayDeployment(
                    id=gateway_deployment_id_for(
                        inventory.gateway_profile_id,
                        gateway.native_gateway_id,
                    ),
                    apim_target_id=inventory.gateway_profile_id,
                    native_gateway_id=gateway.native_gateway_id,
                    kind=GatewayDeploymentKind.SELF_HOSTED,
                    display_name=gateway.location_name or gateway.native_gateway_id,
                    location=gateway.location_name,
                    hosting_mode=GatewayHostingMode.OTHER,
                    lifecycle_ownership=GatewayLifecycleOwnership.CUSTOMER,
                    release_gate=GatewayReleaseGate.OPTIONAL,
                )
            deployment = deployment.model_copy(
                update={
                    "location": gateway.location_name or deployment.location,
                    "declared_state": GatewayDeclaredState.PRESENT,
                    "last_arm_sync_at": inventory.discovered_at,
                }
            )
            self._repository.upsert_gateway_deployment(deployment, preserve_display_name=True)
            self._repository.upsert_gateway_deployment_inventory(
                GatewayDeploymentInventory(
                    deployment_id=deployment.id,
                    associated_api_ids=gateway.associated_api_ids,
                    hostname_configurations=gateway.hostname_configurations,
                    certificate_authorities=gateway.certificate_authorities,
                    source_api_version=inventory.source_api_version,
                    synced_at=inventory.discovered_at,
                )
            )
        for native_id, deployment in existing.items():
            if (
                deployment.kind is GatewayDeploymentKind.SELF_HOSTED
                and native_id not in observed_native_ids
            ):
                self._repository.upsert_gateway_deployment(
                    deployment.model_copy(
                        update={
                            "declared_state": GatewayDeclaredState.ABSENT,
                            "last_arm_sync_at": inventory.discovered_at,
                        }
                    ),
                    preserve_display_name=True,
                )
        self._repository.upsert_apim_target(updated_target)
        return self.apim_target_topology(inventory.gateway_profile_id)

    def request_application_sync(
        self, gateway_profile_id: UUID, actor: str
    ) -> GatewayReleaseOperationAccepted:
        self._require_release_worker()
        gateway = next(
            (
                item
                for item in self._repository.registry()["gateways"]
                if UUID(str(item["id"])) == gateway_profile_id
            ),
            None,
        )
        if gateway is None or gateway.get("implementation") != "apim":
            raise ControlPlaneNotFoundError("APIM gateway not found")
        try:
            row = self._repository.create_gateway_release_operation(
                gateway_profile_id,
                GatewayReleaseOperationKind.APPLICATION_SYNC.value,
                actor,
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        operation = self._operation_view(row)
        return GatewayReleaseOperationAccepted(
            operation=operation,
            status_url=f"/api/v1/model-management/release-operations/{operation.id}",
        )

    def request_application_subscription_provision(
        self,
        gateway_profile_id: UUID,
        request: GatewayApplicationSubscriptionCreate,
        actor: str,
    ) -> GatewayApplicationSubscriptionProvisionAccepted:
        self._require_release_worker()
        if not self._application_provisioning_enabled:
            raise ControlPlaneUnavailableError(_APPLICATION_PROVISIONING_UNAVAILABLE)
        if self._cipher is None:
            raise ControlPlaneUnavailableError("Credential encryption is unavailable")
        if request.subscription_id in {"master", "finops-dashboard", "finops-publisher-probe"}:
            raise ControlPlaneConflictError("This APIM subscription ID is reserved for the system")
        gateway = next(
            (
                item
                for item in self._repository.registry()["gateways"]
                if UUID(str(item["id"])) == gateway_profile_id
            ),
            None,
        )
        if gateway is None or gateway.get("implementation") != "apim":
            raise ControlPlaneNotFoundError("APIM gateway not found")
        if not gateway.get("enabled"):
            raise ControlPlaneConflictError("APIM gateway is disabled")
        if any(
            str(item["apim_subscription_id"]).casefold()
            == request.subscription_id.casefold()
            for item in self._repository.gateway_application_attribution_map(
                gateway_profile_id
            )
        ):
            raise ControlPlaneConflictError("APIM subscription ID already exists")
        try:
            application_organization(request.tags, catalog=catalog_for(self._repository))
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        spec = GatewayApplicationSubscriptionProvisionSpec(
            provisioning_version=2,
            gateway_profile_id=gateway_profile_id,
            initial_monthly_token_limit=(
                request.token_limit or self._application_default_token_limit
            ) if request.budget_mode == "tokens" else None,
            initial_tokens_per_minute=(
                request.tokens_per_minute or self._application_default_tokens_per_minute
            ),
            budget_mode=request.budget_mode,
            initial_usd_limit=request.usd_limit,
            tags=request.tags,
            application_id=application_id_for(
                gateway_profile_id, request.subscription_id
            ),
            application_subscription_id=application_subscription_id_for(
                gateway_profile_id, request.subscription_id
            ),
            apim_subscription_id=request.subscription_id,
            slug=request.subscription_id,
            display_name=request.display_name,
            description=request.description,
            application_type=request.application_type,
            scope_id=self._application_product_id,
        )
        primary_key = secrets.token_urlsafe(32)
        ciphertext = self._cipher.encrypt(
            json.dumps(
                {
                    "primary_key": primary_key,
                    "secondary_key": secrets.token_urlsafe(32),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        try:
            row = self._repository.create_gateway_release_operation(
                gateway_profile_id,
                GatewayReleaseOperationKind.APPLICATION_PROVISION.value,
                actor,
                semantic_preview=spec.model_dump(mode="json"),
                credential_ciphertext=ciphertext,
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        operation = self._operation_view(row)
        return GatewayApplicationSubscriptionProvisionAccepted(
            operation=operation,
            status_url=(
                f"/api/v1/model-management/release-operations/{operation.id}"
            ),
            primary_key=primary_key,
        )

    def release_operation(self, operation_id: UUID) -> GatewayReleaseOperation:
        row = self._repository.get_gateway_release_operation(operation_id)
        if row is None:
            raise ControlPlaneNotFoundError("Gateway release operation not found")
        return self._operation_view(row)

    def retained_release_ids(self, gateway_profile_id: UUID) -> set[UUID]:
        publications = [
            GatewayPublication.model_validate(row)
            for row in self._repository.all_gateway_publications(gateway_profile_id)
        ]
        if not publications:
            return set()
        contexts = self._release_contexts(publications)
        retention = self._release_retention(publications, contexts)
        return {
            publication.id
            for publication in publications
            if bool(retention[publication.id]["retention_eligible"])
        }

    @staticmethod
    def _apim_target(row: Mapping[str, Any]) -> ApimTarget:
        return ApimTarget.model_validate(
            {name: row[name] for name in ApimTarget.model_fields}
        )

    @staticmethod
    def _gateway_deployment(row: Mapping[str, Any]) -> GatewayDeployment:
        return GatewayDeployment.model_validate(
            {name: row[name] for name in GatewayDeployment.model_fields if name in row}
        )

    @staticmethod
    def _gateway_deployment_inventory(
        row: Mapping[str, Any],
    ) -> GatewayDeploymentInventory:
        return GatewayDeploymentInventory.model_validate(
            {name: row[name] for name in GatewayDeploymentInventory.model_fields}
        )

    def _active_apim_target_row(
        self, gateway_profile_id: UUID
    ) -> Mapping[str, Any]:
        row = self._repository.get_apim_target(gateway_profile_id)
        gateway = next(
            (
                item
                for item in self._repository.registry()["gateways"]
                if UUID(str(item["id"])) == gateway_profile_id
                and item["implementation"] == "apim"
                and item["enabled"]
            ),
            None,
        )
        if row is None or gateway is None:
            raise ControlPlaneNotFoundError("APIM target not found")
        return row

    def _active_self_hosted_gateway(
        self, deployment_id: UUID
    ) -> GatewayDeployment:
        row = self._repository.get_gateway_deployment(deployment_id)
        if row is None:
            raise ControlPlaneNotFoundError("Self-hosted Gateway not found")
        deployment = self._gateway_deployment(row)
        if (
            deployment.kind is not GatewayDeploymentKind.SELF_HOSTED
            or deployment.retired_at is not None
        ):
            raise ControlPlaneNotFoundError("Self-hosted Gateway not found")
        self._active_apim_target_row(deployment.apim_target_id)
        return deployment

    def _save_self_hosted_gateway(
        self,
        apim_target_id: UUID,
        write: SelfHostedGatewayWrite,
        *, retiring_deployment: GatewayDeployment | None = None,
    ) -> GatewayDeploymentTopology:
        deployment_id = gateway_deployment_id_for(
            apim_target_id, write.native_gateway_id
        )
        row = self._repository.get_gateway_deployment(deployment_id)
        existing = self._gateway_deployment(row) if row is not None else None
        if (
            existing is not None
            and existing.retired_at is None
            and existing.endpoint_url is not None
        ):
            raise ControlPlaneConflictError(
                "The Self-hosted Gateway is already associated with this APIM target"
            )
        deployment = GatewayDeployment(
            id=deployment_id,
            apim_target_id=apim_target_id,
            native_gateway_id=write.native_gateway_id,
            kind=GatewayDeploymentKind.SELF_HOSTED,
            display_name=write.display_name,
            location=write.location,
            endpoint_url=write.endpoint_url,
            hosting_mode=write.hosting_mode,
            lifecycle_ownership=GatewayLifecycleOwnership.CUSTOMER,
            release_gate=write.release_gate,
            declared_state=(
                existing.declared_state
                if existing is not None
                else GatewayDeclaredState.UNKNOWN
            ),
            last_arm_sync_at=(
                existing.last_arm_sync_at if existing is not None else None
            ),
        )
        try:
            if retiring_deployment is None:
                row = self._repository.upsert_gateway_deployment(deployment)
            else:
                row = self._repository.upsert_gateway_deployment(
                    deployment, retiring_deployment=retiring_deployment,
                )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        persisted = self._gateway_deployment(row)
        inventory_row = self._repository.get_gateway_deployment_inventory(
            persisted.id
        )
        target = self._apim_target(
            self._active_apim_target_row(apim_target_id)
        )
        return GatewayDeploymentTopology(
            deployment=persisted,
            inventory=(
                self._gateway_deployment_inventory(inventory_row)
                if inventory_row is not None
                else None
            ),
            configuration_endpoint=HttpUrl(
                f"https://{target.service_name}.configuration.azure-api.net"
            ),
            published_models=self._published_models_for_deployment(persisted),
        )

    def _published_models_for_deployment(
        self, deployment: GatewayDeployment, *,
        registry: Mapping[str, Sequence[dict[str, Any]]] | None = None,
        route_bindings: Sequence[dict[str, Any]] | None = None,
    ) -> tuple[GatewayPublishedModelSummary, ...]:
        if registry is None:
            registry = self._repository.registry()
        if route_bindings is None:
            route_bindings = self._repository.list_apim_target_route_bindings(
                apim_target_id=deployment.apim_target_id
            )
        runtime_ids = {
            UUID(str(row["id"]))
            for row in registry["runtimes"]
            if row.get("gateway_profile_id") == deployment.apim_target_id
        }
        bindings = {
            UUID(str(row["logical_model_id"])): ApimTargetRouteBinding.model_validate(
                {
                    name: row[name]
                    for name in ApimTargetRouteBinding.model_fields
                }
            )
            for row in route_bindings
        }
        models: list[GatewayPublishedModelSummary] = []
        for row in registry["models"]:
            model_id = UUID(str(row["id"]))
            binding = bindings.get(model_id)
            if binding is None and UUID(str(row["runtime_id"])) not in runtime_ids:
                continue
            if binding is not None and (
                not binding.enabled
                or (
                    binding.deployment_scope.mode
                    is GatewayDeploymentScopeMode.SELECTED
                    and deployment.id
                    not in binding.deployment_scope.deployment_ids
                )
            ):
                continue
            models.append(
                GatewayPublishedModelSummary(
                    id=model_id,
                    model_key=str(row["model_key"]),
                    display_name=str(row["display_name"]),
                )
            )
        return tuple(
            sorted(
                models,
                key=lambda item: (item.display_name.casefold(), str(item.id)),
            )
        )

    def _published_models_for_target(
        self, apim_target_id: UUID, *,
        registry: Mapping[str, Sequence[dict[str, Any]]] | None = None,
    ) -> tuple[GatewayPublishedModelSummary, ...]:
        if registry is None:
            registry = self._repository.registry()
        runtime_ids = {
            UUID(str(row["id"]))
            for row in registry["runtimes"]
            if row.get("gateway_profile_id") == apim_target_id
        }
        return tuple(
            sorted(
                (
                    GatewayPublishedModelSummary(
                        id=UUID(str(row["id"])),
                        model_key=str(row["model_key"]),
                        display_name=str(row["display_name"]),
                    )
                    for row in registry["models"]
                    if UUID(str(row["runtime_id"])) in runtime_ids
                ),
                key=lambda item: (item.display_name.casefold(), str(item.id)),
            )
        )

    def _apim_target_topology(
        self, target_row: Mapping[str, Any], *,
        registry: Mapping[str, Sequence[dict[str, Any]]] | None = None,
        route_bindings: Sequence[dict[str, Any]] | None = None,
        deployment_rows: Sequence[dict[str, Any]] | None = None,
        inventory_rows: Sequence[dict[str, Any]] | None = None,
    ) -> ApimTargetTopology:
        target = self._apim_target(target_row)
        if registry is None:
            registry = self._repository.registry()
        if route_bindings is None:
            route_bindings = self._repository.list_apim_target_route_bindings(
                apim_target_id=target.gateway_profile_id
            )
        if deployment_rows is None:
            deployment_rows = self._repository.list_gateway_deployments(
                target.gateway_profile_id
            )
        deployments = [self._gateway_deployment(row) for row in deployment_rows]
        if inventory_rows is None:
            inventory_rows = self._repository.list_gateway_deployment_inventories(
                [deployment.id for deployment in deployments]
            )
        inventories = {
            UUID(str(row["deployment_id"])): self._gateway_deployment_inventory(row)
            for row in inventory_rows
        }
        configuration_endpoint = HttpUrl(
            f"https://{target.service_name}.configuration.azure-api.net"
        )
        managed = next(
            (
                deployment
                for deployment in deployments
                if deployment.kind is GatewayDeploymentKind.MANAGED
            ),
            None,
        )
        published_models = self._published_models_for_target(
            target.gateway_profile_id, registry=registry
        )
        return ApimTargetTopology(
            target=target,
            deployments=[
                GatewayDeploymentTopology(
                    deployment=deployment,
                    inventory=inventories.get(deployment.id),
                    configuration_endpoint=configuration_endpoint,
                    published_models=self._published_models_for_deployment(
                        deployment, registry=registry, route_bindings=route_bindings
                    ),
                )
                for deployment in deployments
            ],
            management_endpoint=HttpUrl(
                f"https://management.azure.com{target.arm_resource_id}"
            ),
            gateway_endpoint=(
                managed.endpoint_url if managed is not None else None
            ),
            configuration_endpoint=configuration_endpoint,
            connection_state=(
                ApimTargetConnectionState.CONNECTED
                if target.drift_state is ApimTargetDriftState.IN_SYNC
                else ApimTargetConnectionState.UNAVAILABLE
                if target.drift_state is ApimTargetDriftState.DRIFTED
                else ApimTargetConnectionState.UNKNOWN
            ),
            self_hosted_gateway_count=sum(
                deployment.kind is GatewayDeploymentKind.SELF_HOSTED
                and deployment.retired_at is None
                for deployment in deployments
            ),
            published_model_count=len(published_models),
            published_models=published_models,
        )

    def _operation_view(self, row: Mapping[str, Any]) -> GatewayReleaseOperation:
        operation_id = UUID(str(row["id"]))
        audit = [
            GatewayReleaseOperationAuditEvent.model_validate(value)
            for value in self._repository.list_gateway_release_operation_audit(
                operation_id
            )
        ]
        gc_row = self._repository.get_gateway_release_gc_plan(operation_id)
        fields = GatewayReleaseOperation.model_fields
        values = {key: row[key] for key in fields if key in row}
        values["worker_available"] = self._release_worker_enabled
        values["worker_unavailable_reason"] = (
            None if self._release_worker_enabled else _RELEASE_WORKER_UNAVAILABLE
        )
        values["audit"] = audit
        if gc_row is not None:
            values["gc_plan"] = gc_row
        return GatewayReleaseOperation.model_validate(values)

    def _require_release_worker(self) -> None:
        if not self._release_worker_enabled:
            raise ControlPlaneUnavailableError(_RELEASE_WORKER_UNAVAILABLE)

    def _optional_publication(self, publication_id: UUID | None) -> GatewayPublication | None:
        if publication_id is None:
            return None
        row = self._repository.get_gateway_publication(publication_id)
        return GatewayPublication.model_validate(row) if row is not None else None

    def _release_contexts(
        self, publications: Sequence[GatewayPublication],
        related: Mapping[UUID, GatewayPublication] | None = None,
    ) -> dict[UUID, dict[str, object]]:
        grouped: dict[UUID, list[GatewayPublication]] = {}
        for publication in publications:
            grouped.setdefault(publication.gateway_profile_id, []).append(publication)
        gateway_names = {
            UUID(str(row["id"])): str(row["name"])
            for row in self._repository.get_gateway_profiles(list(grouped))
        }
        current_publications = {
            publication.gateway_profile_id: publication
            for row in self._repository.effective_gateway_publications(list(grouped))
            if (publication := GatewayPublication.model_validate(row))
        }
        candidates = {publication.id: publication for publication in publications}
        if related is not None:
            candidates.update(related)
        candidates.update({publication.id: publication
                           for publication in current_publications.values()})
        rollback_ids = {
            publication.base_release_id for publication in current_publications.values()
            if publication.base_release_id is not None
            and publication.base_release_id not in candidates
        }
        candidates.update({
            publication.id: publication
            for row in self._repository.get_gateway_publications(list(rollback_ids))
            if (publication := GatewayPublication.model_validate(row))
        })
        contexts: dict[UUID, dict[str, object]] = {}
        for gateway_id, rows in grouped.items():
            current = current_publications.get(gateway_id)
            rollback = (
                candidates.get(current.base_release_id)
                if current is not None and current.base_release_id is not None
                else None
            )
            if (
                rollback is None or not self._has_recorded_release(rollback)
                or (current is not None and not self._same_gateway_program(rollback, current))
            ):
                rollback = next(
                    (
                        item
                        for item in sorted(rows, key=lambda value: value.generation, reverse=True)
                        if current is not None
                        and item.id != current.id
                        and item.generation < current.generation
                        and self._has_recorded_release(item)
                        and self._same_gateway_program(item, current)
                    ),
                    None,
                )
            contexts[gateway_id] = {
                "gateway_name": gateway_names.get(gateway_id, "Unknown gateway"),
                "current_id": current.id if current else None,
                "rollback_id": rollback.id if rollback else None,
                "current_publication": current,
            }
        return contexts

    def _release_retention(
        self,
        publications: Sequence[GatewayPublication],
        contexts: Mapping[UUID, Mapping[str, object]],
    ) -> dict[UUID, dict[str, object]]:
        protections = {
            UUID(str(row["publication_id"])): row
            for row in self._repository.list_gateway_release_protections(
                [publication.id for publication in publications]
            )
        }
        grouped: dict[UUID, list[GatewayPublication]] = {}
        for publication in publications:
            grouped.setdefault(publication.gateway_profile_id, []).append(publication)
        operation_ids_by_gateway: dict[UUID, set[UUID]] = {
            gateway_id: set() for gateway_id in grouped
        }
        for operation in self._repository.list_gateway_release_operations_for_gateways(
            list(grouped), 1000
        ):
            if str(operation["status"]) in {"succeeded", "failed", "restored"}:
                continue
            operation_ids = operation_ids_by_gateway[UUID(str(operation["gateway_profile_id"]))]
            for key in ("target_release_id", "prior_release_id"):
                if operation.get(key) is not None:
                    operation_ids.add(UUID(str(operation[key])))
        now = datetime.now(UTC)
        retained: dict[UUID, dict[str, object]] = {}
        for gateway_id, rows in grouped.items():
            context = contexts[gateway_id]
            promoted = sorted(
                (
                    row
                    for row in rows
                    if row.status
                    in {
                        PublicationStatus.ACTIVE,
                        PublicationStatus.SUPERSEDED,
                        PublicationStatus.ROLLED_BACK,
                    }
                ),
                key=lambda row: row.generation,
                reverse=True,
            )
            count_ids = {
                row.id for row in promoted[: self._retention_policy.retained_count]
            }
            operation_ids = operation_ids_by_gateway[gateway_id]
            for publication in rows:
                protection = protections.get(publication.id)
                reasons: list[str] = []
                if publication.id == context["current_id"]:
                    reasons.append("current")
                if publication.id == context["rollback_id"]:
                    reasons.append("immediate_rollback")
                if protection is not None and bool(protection["pinned"]):
                    reasons.append("pinned")
                label = (
                    str(protection["protected_label"])
                    if protection is not None
                    and protection.get("protected_label") is not None
                    else None
                )
                if label is not None and label in self._retention_policy.protected_labels:
                    reasons.append(f"protected_label:{label}")
                retain_until = protection.get("retain_until") if protection else None
                if isinstance(retain_until, datetime) and retain_until > now:
                    reasons.append("retain_until")
                if publication.id in count_ids:
                    reasons.append("retained_count")
                age_limit = (
                    self._retention_policy.failed_retained_days
                    if publication.status is PublicationStatus.FAILED
                    else self._retention_policy.retained_days
                )
                if publication.created_at >= now - timedelta(days=age_limit):
                    reasons.append("retained_age")
                if publication.id in operation_ids:
                    reasons.append("active_operation")
                retained[publication.id] = {
                    "pinned": bool(protection and protection["pinned"]),
                    "protected_label": label,
                    "retain_until": retain_until,
                    "retention_eligible": bool(reasons),
                    "retention_reasons": reasons,
                }
        return retained

    @staticmethod
    def _has_recorded_release(publication: GatewayPublication) -> bool:
        return bool(publication.apim_revision and publication.policy_sha256) and not any(
            marker in publication.resource_manifest
            for marker in ("model_route_addition", "model_route_removal")
        )

    @staticmethod
    def _same_gateway_program(target: GatewayPublication, current: GatewayPublication) -> bool:
        return target.resource_manifest.get("fixed_route_program") == current.resource_manifest.get(
            "fixed_route_program",
        )

    def _release_summary(
        self,
        publication: GatewayPublication,
        context: Mapping[str, object],
        retention: Mapping[str, object],
        against: GatewayPublication | None,
        dependencies: GatewayReleaseDependencies,
    ) -> GatewayReleaseSummary:
        current_id = context["current_id"]
        rollback_id = context["rollback_id"]
        raw_retention_reasons = retention["retention_reasons"]
        retention_reasons = (
            [str(value) for value in raw_retention_reasons]
            if isinstance(raw_retention_reasons, Sequence)
            and not isinstance(raw_retention_reasons, str)
            else []
        )
        role = (
            GatewayReleaseRole.CURRENT
            if publication.id == current_id
            else GatewayReleaseRole.IMMEDIATE_ROLLBACK
            if publication.id == rollback_id
            else GatewayReleaseRole.PINNED
            if retention["pinned"]
            else GatewayReleaseRole.EXPIRED
            if not retention["retention_eligible"]
            and publication.status
            in {
                PublicationStatus.ACTIVE,
                PublicationStatus.SUPERSEDED,
                PublicationStatus.FAILED,
                PublicationStatus.ROLLED_BACK,
            }
            else GatewayReleaseRole.FAILED
            if publication.status is PublicationStatus.FAILED
            else GatewayReleaseRole.ROLLED_BACK
            if publication.status is PublicationStatus.ROLLED_BACK
            else GatewayReleaseRole.IN_PROGRESS
            if publication.status not in {PublicationStatus.ACTIVE, PublicationStatus.SUPERSEDED}
            else GatewayReleaseRole.SUPERSEDED
        )
        view = GatewayPublicationView.from_publication(publication)
        current = context.get("current_publication")
        blockers = self._rollback_blockers(
            publication,
            dependencies,
            is_current=role is GatewayReleaseRole.CURRENT,
            current=current if isinstance(current, GatewayPublication) else None,
        )
        return GatewayReleaseSummary(
            id=publication.id,
            gateway_profile_id=publication.gateway_profile_id,
            gateway_name=str(context["gateway_name"]),
            generation=publication.generation,
            role=role,
            publication_kind=publication.publication_kind,
            model_key=view.model_key,
            display_name=view.display_name,
            status=publication.status,
            failure=view.failure,
            apim_revision=publication.apim_revision,
            policy_sha256=publication.policy_sha256,
            desired_spec_sha256=publication.desired_spec_sha256,
            change_set=self._release_changes(publication, against),
            dependency_count=(
                len(dependencies.backends)
                + len(dependencies.backend_pools)
                + len(dependencies.named_values)
            ),
            recorded_dependencies_complete=dependencies.recorded_complete,
            live_integrity_status=dependencies.live_status,
            rollback_eligible=not blockers,
            rollback_blockers=blockers,
            pinned=bool(retention["pinned"]),
            protected_label=(
                str(retention["protected_label"])
                if retention["protected_label"] is not None
                else None
            ),
            retain_until=(
                retention["retain_until"]
                if isinstance(retention["retain_until"], datetime)
                else None
            ),
            retention_eligible=bool(retention["retention_eligible"]),
            retention_reasons=retention_reasons,
            created_by=publication.created_by,
            created_at=publication.created_at,
            completed_at=publication.completed_at,
        )

    @staticmethod
    def _release_changes(
        release: GatewayPublication, against: GatewayPublication | None
    ) -> GatewayReleaseChangeSet:
        before = against.desired_spec.bindings if against else []
        after = release.desired_spec.bindings
        if "model_route_removal" in release.resource_manifest:
            removed = {item.model_key.casefold(): item.model_key
                       for item in release.desired_spec.removed_models}
            return GatewayReleaseChangeSet(
                removed_models=sorted(removed.values()),
                removed_backend_pools=sorted(
                    item.model.model_key for item in before
                    if item.model.model_key.casefold() in removed
                    and item.runtime_config.get("apim_backend_pool")
                ),
            )
        if "model_route_addition" in release.resource_manifest:
            selected = {item.model.model_key.casefold() for item in after}
            before = [item for item in before if item.model.model_key.casefold() in selected]
        before_by_model = {item.model.model_key.casefold(): item for item in before}
        after_by_model = {item.model.model_key.casefold(): item for item in after}
        before_keys = set(before_by_model)
        after_keys = set(after_by_model)
        shared = before_keys & after_keys

        def pool(binding: GatewayModelBinding) -> object | None:
            return binding.runtime_config.get("apim_backend_pool")

        before_pools = {key: pool(value) for key, value in before_by_model.items() if pool(value)}
        after_pools = {key: pool(value) for key, value in after_by_model.items() if pool(value)}
        before_pool_keys = set(before_pools)
        after_pool_keys = set(after_pools)
        return GatewayReleaseChangeSet(
            added_models=sorted(
                after_by_model[key].model.model_key for key in after_keys - before_keys
            ),
            removed_models=sorted(
                before_by_model[key].model.model_key for key in before_keys - after_keys
            ),
            changed_models=sorted(
                after_by_model[key].model.model_key
                for key in shared
                if before_by_model[key].model_dump(mode="json")
                != after_by_model[key].model_dump(mode="json")
            ),
            added_backend_pools=sorted(
                after_by_model[key].model.model_key for key in after_pool_keys - before_pool_keys
            ),
            removed_backend_pools=sorted(
                before_by_model[key].model.model_key for key in before_pool_keys - after_pool_keys
            ),
            changed_backend_pools=sorted(
                after_by_model[key].model.model_key
                for key in before_pool_keys & after_pool_keys
                if before_pools[key] != after_pools[key]
            ),
        )

    def _release_dependencies(
        self, publication: GatewayPublication
    ) -> GatewayReleaseDependencies:
        snapshot = self._repository.latest_gateway_release_integrity_snapshot(
            publication.id
        )
        return self._release_dependencies_from_snapshot(publication, snapshot)

    @staticmethod
    def _recorded_release_dependencies(
        publication: GatewayPublication,
    ) -> GatewayReleaseDependencies:
        manifest = publication.resource_manifest
        if any(field in manifest for field in (
            "fixed_route_program", "route_configuration", "fixed_route_snapshot",
        )):
            from .route_publication import recorded_route_dependencies

            try:
                return recorded_route_dependencies(publication)
            except (KeyError, TypeError, ValueError):
                return GatewayReleaseDependencies(
                    apim_revision=publication.apim_revision, parent_policy_sha256=None,
                    compiled_policy_sha256=publication.policy_sha256, recorded_complete=False,
                    live_status="not_checked", issues=["invalid_recorded_route_configuration"],
                )
        raw_backends = manifest.get("backends")
        raw_named_values = manifest.get("named_values")
        raw_oauth = manifest.get("oauth_credentials")
        oauth_credentials = (
            sorted(str(value) for value in raw_oauth) if isinstance(raw_oauth, list) else []
        )
        expected_oauth = sorted({
            oauth.provider_id for binding in publication.desired_spec.bindings
            if (oauth := binding.oauth) is not None
        })
        backend_ids = (
            [str(value) for value in raw_backends]
            if isinstance(raw_backends, list)
            else []
        )
        named_values = (
            [str(value) for value in raw_named_values]
            if isinstance(raw_named_values, list)
            else []
        )
        pools = sorted(value for value in backend_ids if value.startswith("finops-pool-"))
        backends = sorted(value for value in backend_ids if value not in pools)
        required = {
            "apim_revision": publication.apim_revision,
            "compiled_policy_sha256": publication.policy_sha256,
            "base_apim_revision": manifest.get("base_apim_revision"),
            "parent_policy_sha256": (
                manifest.get("parent_policy_sha256")
                or manifest.get("base_policy_sha256")
            ),
            "backends": raw_backends if isinstance(raw_backends, list) else None,
            "named_values": raw_named_values if isinstance(raw_named_values, list) else None,
        }
        issues = [f"missing_recorded_{key}" for key, value in required.items() if value is None]
        if oauth_credentials != expected_oauth:
            issues.append("mismatched_recorded_oauth_credentials")
        return GatewayReleaseDependencies(
            apim_revision=publication.apim_revision,
            parent_policy_sha256=(
                str(
                    manifest.get("parent_policy_sha256")
                    or manifest["base_policy_sha256"]
                )
                if manifest.get("parent_policy_sha256") is not None
                or manifest.get("base_policy_sha256") is not None
                else None
            ),
            compiled_policy_sha256=publication.policy_sha256,
            backends=backends,
            backend_pools=pools,
            named_values=sorted(named_values),
            oauth_credentials=oauth_credentials,
            recorded_complete=not issues,
            live_status="not_checked",
            issues=issues,
        )

    @staticmethod
    def _release_dependencies_from_snapshot(
        publication: GatewayPublication, snapshot: Mapping[str, Any] | None,
    ) -> GatewayReleaseDependencies:
        recorded = GatewayControlPlaneService._recorded_release_dependencies(publication)
        issues = recorded.issues
        if snapshot is None:
            return recorded
        try:
            checked = GatewayReleaseDependencies.model_validate(
                snapshot["dependencies"]
            )
        except (TypeError, ValueError):
            return recorded.model_copy(
                update={
                    "live_status": "mismatched",
                    "live_checked_at": snapshot["checked_at"],
                    "issues": [*issues, "invalid_integrity_snapshot"],
                }
            )
        identity_fields = (
            "apim_revision",
            "parent_policy_sha256",
            "compiled_policy_sha256",
            "route_configuration_sha256",
            "route_head_sha256",
            "backends",
            "backend_pools",
            "named_values",
            "oauth_credentials",
        )
        if any(
            getattr(recorded, key) != getattr(checked, key) for key in identity_fields
        ):
            return recorded.model_copy(
                update={
                    "live_status": "mismatched",
                    "live_checked_at": snapshot["checked_at"],
                    "issues": [*issues, "integrity_snapshot_identity_mismatch"],
                }
            )
        return checked.model_copy(
            update={
                "live_status": snapshot["status"],
                "live_checked_at": snapshot["checked_at"],
                "issues": [str(value) for value in snapshot["issues"]],
            }
        )

    @staticmethod
    def _rollback_blockers(
        publication: GatewayPublication,
        dependencies: GatewayReleaseDependencies,
        *,
        is_current: bool,
        current: GatewayPublication | None = None,
    ) -> list[str]:
        blockers: list[str] = []
        if any(marker in publication.resource_manifest for marker in (
            "model_route_addition", "model_route_removal",
        )):
            blockers.append("model_release_not_gateway_rollback_target")
        if current is not None and not GatewayControlPlaneService._same_gateway_program(
            publication, current,
        ):
            blockers.append("gateway_program_mismatch")
        if is_current:
            blockers.append("release_is_current")
        if publication.status not in {PublicationStatus.ACTIVE, PublicationStatus.SUPERSEDED}:
            blockers.append("release_was_not_promoted")
        if not dependencies.recorded_complete:
            blockers.append("recorded_dependencies_incomplete")
        if dependencies.live_status != "healthy":
            blockers.append("live_dependency_check_required")
        if publication.resource_manifest.get("rollback_invalid") is True:
            blockers.append("release_marked_invalid_for_rollback")
        return blockers

    def queue_fixed_program_migration(
        self, gateway_profile_id: UUID, descriptor: Mapping[str, Any], created_by: str,
    ) -> GatewayPublication:
        from ..domain.storage_routes import canonical_document, document_digest
        from .storage_route_publisher import FixedProgramMigration, FixedRoutePublisherFactory

        migration = FixedProgramMigration.model_validate_json(canonical_document(descriptor))
        if migration.installation.program.scope.target_id != str(gateway_profile_id):
            raise ControlPlaneConflictError("Program migration belongs to another APIM target")
        previous_row = self._repository.effective_gateway_publication(gateway_profile_id)
        if previous_row is None:
            raise ControlPlaneConflictError("Program migration requires an effective legacy base")
        previous = GatewayPublication.model_validate(previous_row)
        try:
            migration.require_previous(previous)
            registry = self._repository.registry()
            bindings = []
            for binding in previous.desired_spec.bindings:
                if binding.runtime_id is None:
                    matches = [model for model in registry["models"]
                               if model["model_key"] == binding.model.model_key]
                    model = matches[0] if len(matches) == 1 else None
                    runtime_id = model.get("runtime_id") if model is not None else None
                    runtime = self._find(registry["runtimes"], runtime_id) \
                        if isinstance(runtime_id, UUID) else None
                    config = (runtime or {}).get("config") or {}
                    if (
                        model is None or runtime is None or not runtime["enabled"]
                        or model.get("provider_id") is None
                        or model.get("provider_id") != runtime.get("provider_id")
                        or binding.provider_id is not None
                        and runtime.get("provider_id") != binding.provider_id
                        or binding.logical_model_id is not None
                        and model.get("id") != binding.logical_model_id
                        or (model.get("upstream_model_id") or model["model_key"])
                        != binding.model.upstream_model_id
                        or runtime.get("gateway_profile_id") != gateway_profile_id
                        or config.get("control_plane_managed") is not True
                        or config.get("auth_strategy") != binding.auth_strategy.value
                        or config.get("named_value_name") != binding.named_value_name
                        or not FixedRoutePublisherFactory._runtime_destination_matches(
                            previous, binding, config,
                        )
                    ):
                        raise ValueError("Legacy runtime identity cannot be uniquely materialized")
                    binding = binding.model_copy(update={"runtime_id": runtime["id"]})
                bindings.append(binding)
            spec = previous.desired_spec.model_copy(update={
                "removed_models": [], "bindings": bindings,
            }).model_dump(mode="json")
            manifest = {"fixed_route_migration": migration.model_dump(mode="json")}
            digest = document_digest({**manifest, "desired_spec": spec})
            row = self._repository.create_gateway_publication(
                gateway_profile_id, spec, digest, PublicationKind.ROUTE_RECONCILE.value, created_by,
                expected_base_release_id=previous.id, initial_manifest=manifest,
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        return GatewayPublication.model_validate(row)

    def reconcile_gateway(
        self, gateway_profile_id: UUID, created_by: str,
        image_configurations: Mapping[UUID, ImageGenerationLimits] | None = None,
    ) -> GatewayPublication:
        registry = self._repository.registry()
        gateway = self._find(registry["gateways"], gateway_profile_id)
        if gateway is None or not gateway["enabled"] or gateway["implementation"] != "apim":
            raise ControlPlaneConflictError("The selected APIM gateway is unavailable")
        effective_row = self._repository.effective_gateway_publication(gateway_profile_id)
        if effective_row is None:
            raise ControlPlaneConflictError("The gateway has no effective release to reconcile")
        effective = GatewayPublication.model_validate(effective_row)
        declared_profiles = {}
        for model_id, configuration in (image_configurations or {}).items():
            if not self._image_generation_enabled:
                raise ControlPlaneUnavailableError("Image generation is not enabled")
            model = self._find(registry["models"], model_id)
            if model is None or model.get("capabilities") != ["image_generation"]:
                raise ControlPlaneConflictError("An image profile requires an existing image model")
            if not any(
                binding.model.model_key == model["model_key"]
                for binding in effective.desired_spec.bindings
            ):
                raise ControlPlaneConflictError("The image model is not in this gateway release")
            binding = next(
                item for item in effective.desired_spec.bindings
                if item.model.model_key == model["model_key"]
            )
            declared_profiles[model["model_key"]] = create_image_profile(
                configuration, binding.model.upstream_model_id
            )
        updated_spec = effective.desired_spec.model_copy(update={"bindings": [
            binding.model_copy(update={"model": binding.model.model_copy(update={
                "image_profile": declared_profiles[binding.model.model_key],
            })}) if binding.model.model_key in declared_profiles else binding
            for binding in effective.desired_spec.bindings
        ]})
        reconciled = self._reconciled_release_spec(updated_spec, registry)
        return self._queue_route_reconcile(effective, reconciled, created_by)

    def adopt_databricks_connection(
        self, runtime_id: UUID, workspace_url: str, created_by: str,
    ) -> GatewayPublication:
        registry = self._repository.registry()
        runtime = self._find(registry["runtimes"], runtime_id)
        if runtime is None or runtime.get("brand_key") != BrandKey.AZURE_DATABRICKS:
            raise ControlPlaneNotFoundError("Databricks connection not found")
        if not runtime["enabled"] or (runtime.get("config") or {}).get("control_plane_managed"):
            raise ControlPlaneConflictError(
                "Only an enabled legacy Databricks connection can be adopted"
            )
        if not self._apim_principal_id:
            raise ControlPlaneConflictError("APIM managed identity is not configured")
        gateway_id = runtime.get("gateway_profile_id")
        gateway = self._find(registry["gateways"], gateway_id) if gateway_id else None
        if gateway is None or not gateway["enabled"] or gateway["implementation"] != "apim":
            raise ControlPlaneConflictError("The selected APIM gateway is unavailable")
        gateway_id = UUID(str(gateway["id"]))
        provider = self._find(registry["providers"], runtime["provider_id"])
        if provider is None or not provider["enabled"]:
            raise ControlPlaneConflictError("The selected provider is unavailable")
        previous = self._repository.effective_gateway_publication(gateway_id)
        if previous is None:
            raise ControlPlaneConflictError("The gateway has no effective release to reconcile")
        try:
            config = databricks_connection_config(workspace_url, self._apim_principal_id)
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        config["databricks_connection_adoption"] = True
        bindings = []
        for model in registry["models"]:
            if model["runtime_id"] != runtime_id or not model["enabled"]:
                continue
            if self._model_api_format(model, runtime) is not ApiFormat.ANTHROPIC_MESSAGES:
                raise ControlPlaneConflictError("Only native Claude models can use this connection")
            target = ModelTarget.model_validate({
                key: model[key] for key in ModelTarget.model_fields if key in model
            })
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", target.upstream_model_id) is None:
                raise ControlPlaneConflictError("The Databricks serving endpoint name is invalid")
            binding = GatewayModelBinding(
                provider_id=provider["id"], provider_name=provider["name"],
                provider_kind=provider["provider_kind"], provider_brand_key=provider["brand_key"],
                provider_config=dict(provider.get("config") or {}),
                runtime_id=runtime_id, runtime_name=runtime["name"],
                runtime_kind=runtime["runtime_kind"], runtime_brand_key=runtime["brand_key"],
                routing_managed=True, api_format=ApiFormat.ANTHROPIC_MESSAGES,
                backend_url=config["backend_url"], backend_path=config["backend_path"],
                auth_strategy=AuthStrategy.MANAGED_IDENTITY,
                managed_identity_resource=DATABRICKS_INFERENCE_RESOURCE,
                streaming_mode=StreamingMode.NATIVE, runtime_config=config, model=target,
            )
            bindings.append(binding)
        if not bindings:
            raise ControlPlaneConflictError("The legacy connection has no enabled models to adopt")
        effective = GatewayPublication.model_validate(previous)
        discovery = {item.id.casefold(): item for item in effective.desired_spec.discovery_models}
        discovery.update({
            item.id.casefold(): item for item in self._existing_discovery(gateway_id, registry)
        })
        updated = effective.desired_spec.model_copy(update={
            "discovery_models": sorted(discovery.values(), key=lambda item: item.id),
            "bindings": [
                binding for binding in effective.desired_spec.bindings
                if binding.runtime_id != runtime_id
            ] + bindings,
        })
        reconciled = self._reconciled_release_spec(updated, registry)
        return self._queue_route_reconcile(effective, reconciled, created_by)

    def configure_model_backend_pool(
        self,
        model_id: UUID,
        write: GatewayBackendPoolWrite,
        created_by: str,
    ) -> GatewayPublication:
        registry = self._repository.registry()
        model = self._find(registry["models"], model_id)
        if model is None or not model["enabled"]:
            raise ControlPlaneNotFoundError("Model not found")
        if "image_generation" in (model.get("capabilities") or []):
            raise ControlPlaneConflictError("Image generation pools are not supported")
        primary_runtime = self._find(registry["runtimes"], model["runtime_id"])
        if primary_runtime is None or primary_runtime.get("gateway_profile_id") is None:
            raise ControlPlaneConflictError(
                "The model is not routed through a managed APIM gateway"
            )
        gateway_id = UUID(str(primary_runtime["gateway_profile_id"]))
        effective_row = self._repository.effective_gateway_publication(gateway_id)
        if effective_row is None:
            raise ControlPlaneConflictError("The gateway has no effective release")
        effective = GatewayPublication.model_validate(effective_row)
        reconciled = self._reconciled_release_spec(effective.desired_spec, registry)
        alias = str(model["model_key"]).casefold()
        target = next(
            (
                binding
                for binding in reconciled.bindings
                if binding.model.model_key.casefold() == alias
            ),
            None,
        )
        if target is None or not target.routing_managed:
            raise ControlPlaneConflictError(
                "The model has no managed APIM binding in the effective release"
            )
        target = target.model_copy(
            update={
                "provider_id": model["provider_id"],
                "runtime_id": primary_runtime["id"],
            }
        )
        members = [
            self._backend_pool_member(
                target,
                item,
                model,
                registry["runtimes"],
                registry["models"],
                gateway_id,
            )
            for item in write.members
        ]
        pool = GatewayBackendPoolConfig(
            members=members,
            rate_limit=write.rate_limit,
            session_affinity=write.session_affinity,
        )
        runtime_config = {
            **target.runtime_config,
            "apim_backend_pool": pool.model_dump(
                mode="json", exclude={"session_affinity"}
            ),
        }
        runtime_config.pop("apim_backend_pool_session_affinity", None)
        if pool.session_affinity:
            runtime_config["apim_backend_pool_session_affinity"] = True
        updated_target = target.model_copy(
            update={"runtime_config": runtime_config}
        )
        updated_bindings = [
            updated_target
            if binding.model.model_key.casefold() == alias
            else binding
            for binding in reconciled.bindings
        ]
        updated = reconciled.model_copy(update={"bindings": updated_bindings})
        return self._queue_route_reconcile(effective, updated, created_by)

    def model_backend_pool(self, model_id: UUID) -> GatewayBackendPoolConfig:
        registry = self._repository.registry()
        model = self._find(registry["models"], model_id)
        if model is None or not model["enabled"]:
            raise ControlPlaneNotFoundError("Model not found")
        runtime = self._find(registry["runtimes"], model["runtime_id"])
        if runtime is None or runtime.get("gateway_profile_id") is None:
            raise ControlPlaneNotFoundError("Model backend pool not found")
        effective_row = self._repository.effective_gateway_publication(
            UUID(str(runtime["gateway_profile_id"]))
        )
        if effective_row is None:
            raise ControlPlaneNotFoundError("Model backend pool not found")
        effective = GatewayPublication.model_validate(effective_row)
        alias = str(model["model_key"]).casefold()
        binding = next(
            (
                item
                for item in effective.desired_spec.bindings
                if item.model.model_key.casefold() == alias
            ),
            None,
        )
        pool = binding.backend_pool if binding is not None else None
        if pool is None:
            raise ControlPlaneNotFoundError("Model backend pool not found")
        return pool

    def remove_model_backend_pool(
        self,
        model_id: UUID,
        created_by: str,
    ) -> GatewayPublication:
        registry = self._repository.registry()
        model = self._find(registry["models"], model_id)
        if model is None or not model["enabled"]:
            raise ControlPlaneNotFoundError("Model not found")
        runtime = self._find(registry["runtimes"], model["runtime_id"])
        if runtime is None or runtime.get("gateway_profile_id") is None:
            raise ControlPlaneConflictError(
                "The model is not routed through a managed APIM gateway"
            )
        gateway_id = UUID(str(runtime["gateway_profile_id"]))
        effective_row = self._repository.effective_gateway_publication(gateway_id)
        if effective_row is None:
            raise ControlPlaneConflictError("The gateway has no effective release")
        effective = GatewayPublication.model_validate(effective_row)
        reconciled = self._reconciled_release_spec(effective.desired_spec, registry)
        alias = str(model["model_key"]).casefold()
        target = next(
            (
                binding
                for binding in reconciled.bindings
                if binding.model.model_key.casefold() == alias
            ),
            None,
        )
        if target is None:
            raise ControlPlaneConflictError(
                "The model has no APIM binding in the effective release"
            )
        if "apim_backend_pool" not in target.runtime_config:
            raise ControlPlaneConflictError("The model has no APIM backend pool")
        runtime_config = dict(target.runtime_config)
        runtime_config.pop("apim_backend_pool")
        runtime_config.pop("apim_backend_pool_session_affinity", None)
        updated_target = target.model_copy(
            update={"runtime_config": runtime_config}
        )
        updated = reconciled.model_copy(
            update={
                "bindings": [
                    updated_target if binding is target else binding
                    for binding in reconciled.bindings
                ]
            }
        )
        return self._queue_route_reconcile(effective, updated, created_by)

    def _queue_route_reconcile(
        self,
        effective: GatewayPublication,
        spec: GatewayReleaseSpec,
        created_by: str,
    ) -> GatewayPublication:
        normalized = self._normalize_release_spec(
            self._refresh_release_deployments(spec.model_copy(update={"removed_models": []}))
        )
        desired_spec, reconciled_sha256 = self._release_payload(normalized)
        desired_hash = hashlib.sha256(
            f"route_reconcile:{effective.id}:{reconciled_sha256}".encode()
        ).hexdigest()
        try:
            row = self._repository.create_gateway_publication(
                spec.gateway_profile_id,
                desired_spec,
                desired_hash,
                PublicationKind.ROUTE_RECONCILE.value,
                created_by,
                expected_base_release_id=effective.id,
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        return GatewayPublication.model_validate(row)

    def _backend_pool_member(
        self,
        target: GatewayModelBinding,
        member: GatewayBackendPoolMemberWrite,
        model: Mapping[str, Any],
        runtimes: Sequence[dict[str, Any]],
        models: Sequence[dict[str, Any]],
        gateway_id: UUID,
    ) -> GatewayBackendPoolMember:
        runtime = self._find(runtimes, member.runtime_id)
        if runtime is None or not runtime["enabled"]:
            raise ControlPlaneConflictError(
                f"Backend pool runtime {member.runtime_id} is unavailable"
            )
        if (
            runtime.get("provider_id") != model.get("provider_id")
            or runtime.get("gateway_profile_id") != gateway_id
        ):
            raise ControlPlaneConflictError(
                "Every backend pool runtime must use the model provider and APIM gateway"
            )
        upstream_model_id = str(
            model.get("upstream_model_id") or model["model_key"]
        ).strip().casefold()
        has_equivalent_deployment = any(
            candidate.get("enabled") is True
            and candidate.get("provider_id") == model.get("provider_id")
            and candidate.get("runtime_id") == runtime["id"]
            and str(
                candidate.get("upstream_model_id") or candidate["model_key"]
            ).strip().casefold()
            == upstream_model_id
            for candidate in models
        )
        if not has_equivalent_deployment:
            raise ControlPlaneConflictError(
                "Every backend pool runtime must have an enabled equivalent model deployment"
            )
        config = dict(runtime.get("config") or {})
        if not config.get("control_plane_managed"):
            raise ControlPlaneConflictError(
                "Every backend pool runtime must be managed by the control plane"
            )
        runtime_brand = BrandKey(
            runtime.get("brand_key", target.runtime_brand_key)
        )
        api_format = self._runtime_api_format({**runtime, "brand_key": runtime_brand})
        backend_url = config.get("backend_url")
        backend_path = config.get("backend_path")
        if not backend_url or not backend_path:
            raise ControlPlaneConflictError(
                "Every backend pool runtime needs a managed backend URL and path"
            )
        candidate = GatewayModelBinding.model_validate(
            {
                **target.model_dump(mode="python"),
                "runtime_id": runtime["id"],
                "runtime_name": runtime["name"],
                "runtime_kind": runtime["runtime_kind"],
                "runtime_brand_key": runtime_brand,
                "api_format": api_format,
                "backend_url": backend_url,
                "backend_path": backend_path,
                "auth_strategy": config.get("auth_strategy", AuthStrategy.NONE),
                "named_value_name": config.get("named_value_name"),
                "key_vault_secret_id": config.get("key_vault_secret_id"),
                "managed_identity_resource": config.get(
                    "managed_identity_resource"
                ),
                "streaming_mode": config.get(
                    "streaming_mode",
                    StreamingMode.NATIVE,
                ),
                "runtime_config": config,
            }
        )
        candidate_spec = GatewayReleaseSpec(
            gateway_profile_id=gateway_id,
            discovery_models=[
                DiscoveryModel(
                    id=target.model.model_key,
                    display_name=target.model.display_name,
                    api_format=api_format,
                )
            ],
            bindings=[candidate],
            gateway_deployments=target.serving_deployments,
        )
        normalized = self._normalize_release_spec(candidate_spec).bindings[0]
        invariant_fields = ("api_format", "backend_path", "streaming_mode")
        if any(
            getattr(normalized, field) != getattr(target, field)
            for field in invariant_fields
        ):
            raise ControlPlaneConflictError(
                "Backend pool runtimes must share protocol, path, and streaming"
            )
        named_auth = {
            AuthStrategy.NAMED_VALUE_BEARER,
            AuthStrategy.NAMED_VALUE_API_KEY,
        }
        if normalized.auth_strategy in named_auth and (
            not normalized.named_value_name
            or config.get("credential_provisioned") is not True
        ):
            raise ControlPlaneConflictError(
                "A named-value pool member requires a provisioned credential"
            )
        if target.auth_strategy is AuthStrategy.MANAGED_IDENTITY:
            auth_compatible = (
                normalized.auth_strategy is AuthStrategy.NAMED_VALUE_API_KEY
                or (
                    normalized.auth_strategy is AuthStrategy.MANAGED_IDENTITY
                    and normalized.managed_identity_resource
                    == target.managed_identity_resource
                )
            )
        else:
            auth_compatible = normalized.auth_strategy is target.auth_strategy
        if not auth_compatible:
            raise ControlPlaneConflictError(
                "Backend pool runtime authentication cannot be applied per member"
            )
        if normalized.backend_url is None:
            raise ControlPlaneConflictError(
                "The normalized backend pool runtime has no backend URL"
            )
        return GatewayBackendPoolMember(
            runtime_id=runtime["id"],
            runtime_name=str(runtime["name"]),
            backend_url=normalized.backend_url,
            auth_strategy=normalized.auth_strategy,
            named_value_name=normalized.named_value_name,
            priority=member.priority,
            weight=member.weight,
        )

    @classmethod
    def _reconciled_release_spec(
        cls,
        effective: GatewayReleaseSpec,
        registry: Mapping[str, Sequence[dict[str, Any]]],
    ) -> GatewayReleaseSpec:
        for binding in effective.bindings:
            if binding.runtime_config.get("databricks_connection_adoption") is not True:
                continue
            runtime = (
                cls._find(registry["runtimes"], binding.runtime_id) if binding.runtime_id else None
            )
            if (
                runtime is None or not runtime["enabled"]
                or runtime["provider_id"] != binding.provider_id
                or runtime.get("gateway_profile_id") != effective.gateway_profile_id
            ):
                raise ControlPlaneConflictError("The adopted Databricks connection has changed")
            workspace = binding.runtime_config.get("workspace_url")
            if any(
                row["id"] != binding.runtime_id
                and row.get("gateway_profile_id") == effective.gateway_profile_id
                and (row.get("config") or {}).get("workspace_url") == workspace
                for row in registry["runtimes"]
            ):
                raise ControlPlaneConflictError(
                    "This Databricks Workspace connection already exists"
                )
            cls._validate_connection(
                {"brand_key": binding.provider_brand_key, "provider_kind": binding.provider_kind},
                {**binding.model_dump(), "config": binding.runtime_config},
            )
        bindings = list(effective.bindings)
        bound_aliases = {binding.model.model_key.casefold() for binding in bindings}
        discovery_aliases = {item.id.casefold() for item in effective.discovery_models}
        templates_by_runtime: dict[UUID, list[GatewayModelBinding]] = {}
        for binding in bindings:
            if binding.routing_managed and binding.runtime_id is not None:
                templates_by_runtime.setdefault(binding.runtime_id, []).append(binding)

        additions: list[GatewayModelBinding] = []
        for model in registry["models"]:
            alias = str(model["model_key"]).casefold()
            runtime_id = model.get("runtime_id")
            if (
                alias in bound_aliases
                or alias not in discovery_aliases
                or runtime_id not in templates_by_runtime
            ):
                continue
            family = ModelFamilyKey(model.get("family_key", ModelFamilyKey.GENERIC))
            templates = templates_by_runtime[runtime_id]
            template = next(
                (
                    item
                    for item in templates
                    if family is ModelFamilyKey.CLAUDE
                    or item.api_format is ApiFormat.OPENAI_CHAT
                                        or "image_generation" in (model.get("capabilities") or [])
                ),
                                next((item for item in templates
                                            if item.provider_brand_key is BrandKey.MICROSOFT_FOUNDRY
                                            and item.api_format is ApiFormat.OPENAI_IMAGES), None),
            )
            if template is None:
                continue
            target = ModelTarget(
                model_key=model["model_key"],
                display_name=model["display_name"],
                upstream_model_id=model.get("upstream_model_id") or model["model_key"],
                family_key=family,
                capabilities=list(model.get("capabilities") or ["chat"]),
                context_window=model.get("context_window"),
                input_cost_per_million=model.get("input_cost_per_million"),
                output_cost_per_million=model.get("output_cost_per_million"),
                cached_cost_per_million=model.get("cached_cost_per_million"),
                cache_write_cost_per_million=model.get("cache_write_cost_per_million"),
                price_source=model.get("price_source", "manual"),
                price_reference=model.get("price_reference"),
                price_discount_percent=model.get("price_discount_percent"),
                context_price_references=[
                    tier["reference"] for tier in model.get("context_price_tiers") or []
                ],
                context_price_tiers=model.get("context_price_tiers") or [],
                allowed_roles=list(model.get("allowed_roles") or ["owner", "admin", "member"]),
                assignment_required=bool(model.get("assignment_required", False)),
                image_profile=model.get("image_profile"),
            )
            runtime_config = dict(template.runtime_config)
            runtime_config.pop("apim_backend_pool", None)
            additions.append(
                template.model_copy(
                    update={
                        "model": target,
                        "logical_model_id": UUID(str(model["id"])),
                        "runtime_config": runtime_config,
                        "deployment_scope": GatewayDeploymentScope(),
                        "serving_deployments": (),
                    }
                )
            )
            bound_aliases.add(alias)

        reconciled = effective.model_copy(
            update={
                "bindings": [
                    *sorted(additions, key=lambda item: item.model.model_key.casefold()),
                    *bindings,
                ]
            }
        )
        return cls._normalize_release_spec(reconciled)

    def retry(
        self,
        publication_id: UUID,
        write: GatewayPublicationRetry,
        created_by: str,
    ) -> GatewayPublication:
        publication = self.publication(publication_id)
        if publication.status.value != "failed":
            raise ControlPlaneConflictError("Only a failed publication can be retried")
        if publication.publication_kind is PublicationKind.MODEL_REMOVE:
            raise ControlPlaneConflictError(
                "Retry model deletion by submitting the delete request again"
            )
        if "model_route_addition" in publication.resource_manifest:
            if (write.api_key is not None or write.oauth_client_secret is not None
                    or write.authorize_image_probes):
                raise ControlPlaneConflictError("Model addition recovery cannot replace its intent")
            try:
                row = self._repository.requeue_gateway_publication(
                    publication_id, created_by, None,
                )
            except ValueError as error:
                raise ControlPlaneConflictError(str(error)) from error
            return GatewayPublication.model_validate(row)
        if publication.publication_kind is PublicationKind.ROUTE_RECONCILE:
            if "fixed_route_migration" in publication.resource_manifest:
                if (write.api_key is not None or write.oauth_client_secret is not None
                        or write.authorize_image_probes):
                    raise ControlPlaneConflictError(
                        "Program migration retry cannot replace credentials or probe authorization",
                    )
                try:
                    row = self._repository.requeue_gateway_publication(
                        publication_id, created_by, None,
                    )
                except ValueError as error:
                    raise ControlPlaneConflictError(str(error)) from error
                return GatewayPublication.model_validate(row)
            raise ControlPlaneConflictError(
                "Submit a fresh route reconciliation against the effective release"
            )
        binding = publication.desired_spec.bindings[-1]
        uses_oauth = binding.oauth is not None
        if uses_oauth and not self._databricks_oauth_enabled:
            raise ControlPlaneConflictError("Databricks OAuth M2M is not enabled")
        if (
            uses_oauth and write.api_key is not None
            or not uses_oauth and write.oauth_client_secret is not None
        ):
            raise ControlPlaneConflictError("The credential type does not match the publication")
        convert_foundry_project_to_key = (
            binding.provider_brand_key is BrandKey.MICROSOFT_FOUNDRY
            and binding.auth_strategy is AuthStrategy.MANAGED_IDENTITY
            and write.api_key is not None
        )
        if convert_foundry_project_to_key:
            if self._cipher is None:
                raise ControlPlaneConflictError("Credential encryption is unavailable")
            assert write.api_key is not None
            backend_url = str(binding.backend_url or "").rstrip("/")
            if not backend_url:
                raise ControlPlaneConflictError(
                    "The Foundry Project binding has no backend endpoint"
                )
            credential_scope = (
                f"{publication.gateway_profile_id}:{backend_url}:{backend_url}"
            )
            named_value_name = (
                "turnstile-foundry-key-"
                + hashlib.sha256(credential_scope.encode("utf-8")).hexdigest()[:16]
            )
            runtime_config = dict(binding.runtime_config)
            runtime_config.update(
                inference_endpoint=backend_url,
                credential_kind="api_key",
                credential_provisioned=True,
            )
            binding = binding.model_copy(
                update={
                    "auth_strategy": AuthStrategy.NAMED_VALUE_API_KEY,
                    "named_value_name": named_value_name,
                    "managed_identity_resource": None,
                    "runtime_config": runtime_config,
                }
            )
            desired_spec_model = publication.desired_spec.model_copy(
                update={
                    "bindings": [
                        *publication.desired_spec.bindings[:-1],
                        binding,
                    ]
                }
            )
        else:
            desired_spec_model = publication.desired_spec
        accepts_credential = (
            binding.auth_strategy
            in {
                AuthStrategy.NAMED_VALUE_BEARER,
                AuthStrategy.NAMED_VALUE_API_KEY,
            }
            and binding.key_vault_secret_id is None
        )
        if accepts_credential and not binding.named_value_name:
            raise ControlPlaneConflictError(
                "This publication does not use a directly managed API key"
            )
        requires_credential = publication_retry_requires_credential(publication)
        submitted_credential = write.oauth_client_secret if uses_oauth else write.api_key
        if submitted_credential is not None:
            if not accepts_credential and not uses_oauth:
                raise ControlPlaneConflictError(
                    "This publication does not accept an API key"
                )
            if self._cipher is None:
                raise ControlPlaneConflictError("Credential encryption is unavailable")
            if uses_oauth and not requires_credential:
                raise ControlPlaneConflictError(
                    "Use credential rotation for a provisioned OAuth connection"
                )
            if accepts_credential and self._inherited_named_value(publication) is not None:
                raise ControlPlaneConflictError(
                    "Use credential rotation for a provisioned connection"
                )
            encrypted = self._cipher.encrypt(submitted_credential.get_secret_value())
        else:
            if requires_credential:
                raise ControlPlaneConflictError(
                    "A replacement OAuth client secret is required" if uses_oauth
                    else "A replacement API key is required"
                )
            encrypted = None
        normalized_spec = self._normalize_release_spec(desired_spec_model)
        desired_spec, desired_hash = self._release_payload(normalized_spec)
        image_authorization = None
        if write.authorize_image_probes:
            from ..domain.control_plane import ImageProbeAuthorization

            if not any(
                binding.api_format is ApiFormat.OPENAI_IMAGES
                for binding in normalized_spec.bindings
            ):
                raise ControlPlaneConflictError("This publication has no image probes to authorize")
            prior = ImageProbeAuthorization.model_validate(
                publication.resource_manifest.get(
                    "image_probe_authorization", {"id": str(publication.id)}
                )
            )
            if prior.attempt_limit >= 32:
                raise ControlPlaneConflictError("The image probe attempt limit has been reached")
            image_authorization = ImageProbeAuthorization(
                id=uuid4(), attempt_limit=prior.attempt_limit + 1
            ).model_dump(mode="json")
        effective = self._repository.effective_gateway_publication(publication.gateway_profile_id)
        if effective is not None and any(
            field in (effective.get("resource_manifest") or {})
            for field in ("fixed_route_program", "route_configuration")
        ) and not any(
            field.startswith("route_") or field.startswith("fixed_route_")
            for field in publication.resource_manifest
        ):
            if publication.publication_kind is not PublicationKind.MODEL_ADD:
                raise ControlPlaneConflictError("publication_retry_requires_new_release")
            current = GatewayPublication.model_validate(effective)
            if (current.gateway_profile_id != publication.gateway_profile_id
                    or current.status is not PublicationStatus.ACTIVE
                    or image_authorization is not None
                    or binding.api_format is ApiFormat.OPENAI_IMAGES
                    or any(item.model.model_key.casefold() == binding.model.model_key.casefold()
                           for item in current.desired_spec.bindings)):
                raise ControlPlaneConflictError(
                    "The current gateway configuration requires a new explicit publication",
                )
            discovery = DiscoveryModel(
                id=binding.model.model_key, display_name=binding.model.display_name,
                api_format=binding.api_format,
            )
            successor = current.desired_spec.model_copy(update={
                "bindings": [binding], "discovery_models": [discovery], "removed_models": [],
            })
            successor = self._refresh_release_deployments(self._normalize_release_spec(successor))
            return self._queue_release(
                successor, PublicationKind.MODEL_ADD, created_by, encrypted,
                expected_base_release_id=current.id,
            )
        try:
            row = self._repository.requeue_gateway_publication(
                publication_id,
                created_by,
                encrypted,
                desired_spec,
                desired_hash,
                image_probe_authorization=image_authorization,
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        return GatewayPublication.model_validate(row)

    def resume_authorization(
        self,
        publication_id: UUID,
        created_by: str,
    ) -> GatewayPublication:
        publication = self.publication(publication_id)
        if publication.status.value != "awaiting_authorization":
            raise ControlPlaneConflictError(
                "The publication is not waiting for provider authorization"
            )
        try:
            row = self._repository.resume_gateway_publication_authorization(
                publication_id, created_by
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        return GatewayPublication.model_validate(row)

    def cancel_authorization(
        self,
        publication_id: UUID,
        created_by: str,
    ) -> GatewayPublication:
        publication = self.publication(publication_id)
        if publication.status.value == "queued":
            if publication.attempt_count != 0 or publication.started_at is not None:
                raise ControlPlaneConflictError(
                    "Only an unstarted queued publication can be cancelled"
                )
            row = self._repository.cancel_unstarted_gateway_publication(
                publication_id, created_by
            )
            if row is None:
                raise ControlPlaneConflictError(
                    "The publication changed before it could be cancelled"
                )
            return GatewayPublication.model_validate(row)
        if publication.status.value != "awaiting_authorization":
            raise ControlPlaneConflictError(
                "Only an unstarted queued publication or a publication waiting "
                "for provider authorization can be cancelled"
            )
        row = self._repository.transition_gateway_publication(
            publication_id,
            "awaiting_authorization",
            "rolled_back",
            {
                "error_code": "cancelled_by_owner",
                "error_message": "Publication cancelled before promotion",
            },
            created_by,
        )
        if row is None:
            raise ControlPlaneConflictError(
                "The publication changed before it could be cancelled"
            )
        return GatewayPublication.model_validate(row)

    def _resolve_publication_deployments(
        self,
        gateway_profile_id: UUID,
        scope: GatewayDeploymentScope,
    ) -> tuple[
        GatewayDeploymentScope,
        tuple[GatewayServingDeployment, ...],
        tuple[GatewayServingDeployment, ...],
    ]:
        if not self._deployment_scoping_enabled:
            if scope.mode is not GatewayDeploymentScopeMode.ALL:
                raise ControlPlaneUnavailableError(
                    "Gateway deployment scoping is not enabled"
                )
            return scope, (), ()
        target_row = self._repository.get_apim_target(gateway_profile_id)
        if target_row is None:
            raise ControlPlaneConflictError("APIM target metadata is unavailable")
        target = self._apim_target(target_row)
        if target.drift_state is not ApimTargetDriftState.IN_SYNC:
            raise ControlPlaneConflictError(
                "APIM target inventory is not validated and in sync"
            )
        deployments = [
            self._gateway_deployment(row)
            for row in self._repository.list_gateway_deployments(gateway_profile_id)
        ]
        inventories = {
            UUID(str(row["deployment_id"])): self._gateway_deployment_inventory(row)
            for row in self._repository.list_gateway_deployment_inventories(
                [deployment.id for deployment in deployments]
            )
        }
        ready: dict[UUID, GatewayServingDeployment] = {}
        selected_ids = set(scope.deployment_ids)
        for deployment in deployments:
            if deployment.declared_state is not GatewayDeclaredState.PRESENT:
                if deployment.release_gate is GatewayReleaseGate.REQUIRED:
                    raise ControlPlaneConflictError(
                        f"Required Gateway deployment {deployment.display_name} "
                        "is not declared present"
                    )
                continue
            if deployment.endpoint_url is None:
                if deployment.release_gate is GatewayReleaseGate.REQUIRED:
                    raise ControlPlaneConflictError(
                        f"Required Gateway deployment {deployment.display_name} has no endpoint"
                    )
                continue
            if (
                deployment.release_gate is GatewayReleaseGate.REQUIRED
                and deployment.reachability_state
                is not GatewayReachabilityState.REACHABLE
            ):
                raise ControlPlaneConflictError(
                    f"Required Gateway deployment {deployment.display_name} is not reachable"
                )
            inventory = inventories.get(deployment.id)
            if deployment.kind is GatewayDeploymentKind.SELF_HOSTED:
                associated = inventory is not None and any(
                    api_id.casefold() == target.api_id.casefold()
                    for api_id in inventory.associated_api_ids
                )
                if not associated:
                    if (
                        deployment.id in selected_ids
                        or deployment.release_gate is GatewayReleaseGate.REQUIRED
                    ):
                        raise ControlPlaneConflictError(
                            "Turnstile API is not associated with "
                            f"{deployment.display_name}"
                        )
                    continue
                if (
                    target.capabilities.self_hosted_gateway
                    is not CapabilitySupport.SUPPORTED
                ):
                    raise ControlPlaneConflictError(
                        "The APIM target does not support Self-hosted Gateway"
                    )
            ready[deployment.id] = GatewayServingDeployment(
                deployment_id=deployment.id,
                native_gateway_id=deployment.native_gateway_id,
                kind=deployment.kind,
                endpoint_url=deployment.endpoint_url,
                release_gate=deployment.release_gate,
            )
        snapshots = tuple(
            sorted(ready.values(), key=lambda item: item.native_gateway_id.casefold())
        )
        if not snapshots:
            raise ControlPlaneConflictError(
                "The APIM target has no declared Gateway deployment with an endpoint"
            )
        if scope.mode is GatewayDeploymentScopeMode.ALL:
            return scope, (), snapshots
        if selected_ids - set(ready):
            raise ControlPlaneConflictError(
                "Selected Gateway deployments are not ready for publication"
            )
        selected = tuple(
            item for item in snapshots if item.deployment_id in selected_ids
        )
        return scope, selected, snapshots

    def _refresh_release_deployments(
        self,
        spec: GatewayReleaseSpec,
    ) -> GatewayReleaseSpec:
        if not self._deployment_scoping_enabled:
            return spec
        _, _, snapshots = self._resolve_publication_deployments(
            spec.gateway_profile_id,
            GatewayDeploymentScope(),
        )
        by_id = {item.deployment_id: item for item in snapshots}
        bindings: list[GatewayModelBinding] = []
        for binding in spec.bindings:
            if binding.deployment_scope.mode is GatewayDeploymentScopeMode.ALL:
                bindings.append(
                    binding.model_copy(update={"serving_deployments": ()})
                )
                continue
            missing = set(binding.deployment_scope.deployment_ids) - set(by_id)
            if missing:
                raise ControlPlaneConflictError(
                    "A scoped model references a Gateway deployment that is no "
                    "longer ready"
                )
            bindings.append(
                binding.model_copy(
                    update={
                        "serving_deployments": tuple(
                            item
                            for item in snapshots
                            if item.deployment_id
                            in binding.deployment_scope.deployment_ids
                        )
                    }
                )
            )
        payload = spec.model_dump(mode="python")
        payload.update(bindings=bindings, gateway_deployments=snapshots)
        return GatewayReleaseSpec.model_validate(payload)

    def publish(
        self, write: GatewayPublicationCreate, created_by: str,
        *, pricing_values: dict[str, Any] | None = None,
    ) -> GatewayPublication:
        if write.model.operation == "image_generation" and not self._image_generation_enabled:
            raise ControlPlaneUnavailableError("Image generation is not enabled")
        logical_model_id = uuid4() if self._target_registry_enabled else None
        prepared = self._prepare_model_publication(
            write, logical_model_id, reuse_existing_model=self._target_registry_enabled,
            pricing_values=pricing_values,
        )
        return self._queue_release(
            prepared.spec,
            PublicationKind.MODEL_ADD,
            created_by,
            prepared.credential_ciphertext,
            expected_base_release_id=prepared.expected_base_release_id,
        )

    def _prepare_model_publication(
        self,
        write: GatewayPublicationCreate,
        logical_model_id: UUID | None,
        *, reuse_existing_model: bool = False,
        pricing_values: dict[str, Any] | None = None,
    ) -> _PreparedModelPublication:
        from ..persistence.repository_support import _managed_program_publication

        registry = self._repository.registry()
        gateway = self._find(registry["gateways"], write.gateway_profile_id)
        if gateway is None or not gateway["enabled"] or gateway["implementation"] != "apim":
            raise ControlPlaneConflictError("The selected APIM gateway is unavailable")
        deployment_scope, serving_deployments, release_deployments = (
            self._resolve_publication_deployments(
                write.gateway_profile_id, write.deployment_scope
            )
        )

        provider = self._resolve_provider(write, registry["providers"])
        runtime = self._resolve_runtime(write, provider, registry["runtimes"])
        self._validate_connection(provider, runtime)
        model_input = write.model
        if model_input.operation == "image_generation" and model_input.image_configuration is None:
            if self._image_generation_defaults is None:
                raise ControlPlaneUnavailableError("Image generation defaults are unavailable")
            model_input = model_input.model_copy(
                update={"image_configuration": self._image_generation_defaults}
            )
        model = self._release_model(model_input, provider, runtime)
        existing = next((row for row in registry["models"]
                         if str(row["model_key"]).casefold() == model.model_key.casefold()), None)
        reused = False
        if reuse_existing_model and existing is not None:
            primary = self._find(registry["runtimes"], existing["runtime_id"])
            routes = self._repository.list_apim_target_route_bindings(
                logical_model_id=existing["id"],
            )
            if (primary is None or primary.get("gateway_profile_id") == write.gateway_profile_id
                    or any(row["apim_target_id"] == write.gateway_profile_id for row in routes)):
                raise ControlPlaneConflictError("The model alias is already active on this APIM")
            canonical = ModelTarget.model_validate({
                key: existing[key] for key in ModelTarget.model_fields if key in existing
            })
            if (existing["provider_id"] != provider.get("id")
                    or canonical.upstream_model_id != model.upstream_model_id
                    or canonical.family_key is not model.family_key
                    or canonical.capabilities != model.capabilities):
                raise ControlPlaneConflictError("Existing model identity does not match this route")
            if provider["brand_key"] == BrandKey.MICROSOFT_FOUNDRY and (
                str((primary.get("config") or {}).get("project_endpoint", "")).rstrip("/")
                .casefold() != str((runtime.get("config") or {}).get("project_endpoint", ""))
                .rstrip("/").casefold()
            ):
                raise ControlPlaneConflictError("Existing model belongs to another Foundry project")
            logical_model_id, model, reused = existing["id"], canonical, True
        else:
            self._validate_model(model, registry["models"])
        if model.context_price_rule is not None:
            for row in self._repository.list_gateway_publications(write.gateway_profile_id, 1):
                pending = GatewayPublication.model_validate(row)
                if (pending.publication_kind is PublicationKind.MODEL_ADD
                    and pending.status not in {
                        PublicationStatus.SUPERSEDED, PublicationStatus.ROLLED_BACK,
                    } and pending.desired_spec.bindings):
                    previous_binding = pending.desired_spec.bindings[-1]
                    previous_model = previous_binding.model
                    if (previous_model.model_key == model.model_key
                        and previous_binding.runtime_id == runtime.get("id")
                        and previous_model.context_price_rule == model.context_price_rule):
                        model = model.model_copy(update={
                            "context_price_tiers": previous_model.context_price_tiers,
                        })
        if pricing_values is not None and not reused:
            # A retry of the same quote must retain the same publication digest.
            configuration = {
                key: value for key, value in pricing_values.items() if key != "price_synced_at"
            }
            model = ModelTarget.model_validate({
                **model.model_dump(mode="python"),
                **{key: value for key, value in pricing_values.items()
                   if key in ModelTarget.model_fields},
                "price_configuration": configuration,
            })
        elif not reused:
            if model.price_source is PriceSource.MODELS_DEV and not model.price_reference:
                if not write.model.allow_unpriced:
                    raise ControlPlaneConflictError(
                        "Synchronize pricing or explicitly confirm an unpriced model"
                    )
            else:
                model = self._price_model(
                    model, runtime, expected_digest=write.model.price_entry_digest,
                )
        binding_runtime = self._model_binding_runtime(provider, runtime, model)

        previous_row = self._repository.effective_gateway_publication(
            write.gateway_profile_id
        )
        previous_row = _managed_program_publication(previous_row, gateway.get("config") or {})
        previous = (
            GatewayPublication.model_validate(previous_row)
            if previous_row is not None
            else None
        )
        incremental = previous is not None and any(
            name in previous.resource_manifest
            for name in ("fixed_route_program", "route_configuration")
        )
        if (self._target_registry_enabled or previous is not None) and not incremental:
            raise ControlPlaneConflictError("Single-model publication program is not installed")
        previous_spec = (
            self._reconciled_release_spec(previous.desired_spec, registry)
            if previous and not incremental
            else None
        )
        bindings = list(previous_spec.bindings) if previous_spec else []
        binding = GatewayModelBinding(
            provider_id=provider.get("id"),
            provider_name=str(provider["name"]),
            provider_kind=provider["provider_kind"],
            provider_brand_key=provider["brand_key"],
            provider_config=dict(provider.get("config") or {}),
            runtime_id=binding_runtime.get("id"),
            routing_managed=bool(binding_runtime["routing_managed"]),
            runtime_name=str(binding_runtime["name"]),
            runtime_kind=binding_runtime["runtime_kind"],
            runtime_brand_key=binding_runtime["brand_key"],
            api_format=binding_runtime["api_format"],
            backend_url=binding_runtime.get("backend_url"),
            backend_path=str(binding_runtime["backend_path"]),
            auth_strategy=binding_runtime["auth_strategy"],
            named_value_name=binding_runtime.get("named_value_name"),
            key_vault_secret_id=binding_runtime.get("key_vault_secret_id"),
            managed_identity_resource=binding_runtime.get("managed_identity_resource"),
            streaming_mode=binding_runtime["streaming_mode"],
            runtime_config=dict(binding_runtime.get("config") or {}),
            deployment_scope=deployment_scope,
            serving_deployments=serving_deployments,
            logical_model_id=logical_model_id,
            model=model,
        )
        bindings.append(binding)

        discovery = list(previous_spec.discovery_models) if previous_spec else []
        if not incremental:
            discovery.extend(self._existing_discovery(write.gateway_profile_id, registry))
        discovery.append(
            DiscoveryModel(
                id=model.model_key,
                display_name=model.display_name,
                api_format=binding_runtime["api_format"],
            )
        )
        unique_discovery = {
            item.id.casefold(): item for item in discovery
        }
        spec = GatewayReleaseSpec(
            gateway_profile_id=write.gateway_profile_id,
            discovery_models=sorted(unique_discovery.values(), key=lambda item: item.id),
            bindings=bindings,
            gateway_deployments=(
                release_deployments
                if self._deployment_scoping_enabled
                else previous_spec.gateway_deployments
                if previous_spec is not None
                else ()
            ),
        )
        credential_ciphertext = None
        submitted_credential = write.runtime.api_key or write.runtime.oauth_client_secret
        if submitted_credential is not None:
            if self._cipher is None:
                raise ControlPlaneConflictError("Credential encryption is unavailable")
            credential_ciphertext = self._cipher.encrypt(
                submitted_credential.get_secret_value()
            )
        return _PreparedModelPublication(
            spec=spec,
            credential_ciphertext=credential_ciphertext,
            expected_base_release_id=previous.id if previous is not None and incremental else None,
        )

    def publish_multi_target(
        self,
        write: MultiTargetPublicationCreate,
        created_by: str,
    ) -> tuple[MultiTargetRollout, list[GatewayPublication]]:
        if not self._multi_target_rollout_enabled:
            raise ControlPlaneUnavailableError(
                "Multi-target APIM rollout is not enabled"
            )
        request_items: list[dict[str, Any]] = []
        ordered_children = sorted(
            write.children,
            key=lambda child: str(child.publication.gateway_profile_id),
        )
        for child in ordered_children:
            item = child.model_dump(mode="json")
            publication = item["publication"]
            runtime = publication["runtime"]
            runtime.pop("api_key", None)
            runtime.pop("oauth_client_secret", None)
            request_items.append(item)
        request_sha256 = hashlib.sha256(
            json.dumps(
                request_items,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        rollout_id = uuid5(_MULTI_TARGET_ROLLOUT_NAMESPACE, request_sha256)
        existing = self._repository.get_multi_target_rollout(rollout_id)
        if existing is not None:
            return self._multi_target_rollout_view(existing)
        logical_model_id = uuid5(_MULTI_TARGET_MODEL_NAMESPACE, request_sha256)

        explicit_models = [
            child.publication.model
            for child in ordered_children
            if child.publication.model.model_key is not None
        ]
        explicit_identities = {
            (str(model.model_key).casefold(), str(model.display_name))
            for model in explicit_models
        }
        if len(explicit_identities) > 1:
            raise ControlPlaneConflictError(
                "Explicit multi-target children must use one logical model identity"
            )

        raw_prepared: list[tuple[_PreparedModelPublication, bool]] = []
        for child in ordered_children:
            prepared = self._prepare_model_publication(
                child.publication,
                logical_model_id,
            )
            prepared = self._qualify_multi_target_runtime(prepared)
            raw_prepared.append((prepared, child.required))
        canonical_index = next(
            (
                index
                for index, child in enumerate(ordered_children)
                if child.publication.model.model_key is not None
            ),
            0,
        )
        canonical_model = raw_prepared[canonical_index][0].spec.bindings[-1].model
        prepared_children = [
            (
                prepared
                if index == canonical_index
                else self._canonicalize_multi_target_model(
                    prepared,
                    canonical_model,
                ),
                required,
            )
            for index, (prepared, required) in enumerate(raw_prepared)
        ]

        child_rows: list[dict[str, Any]] = []
        for prepared, required in prepared_children:
            payload, digest = self._release_payload(prepared.spec)
            manifest = None
            if prepared.expected_base_release_id is not None:
                from ..domain.storage_routes import document_digest

                manifest = {"model_route_addition": {
                    "version": 1,
                    "program_release_id": str(prepared.expected_base_release_id),
                    "alias": prepared.spec.bindings[0].model.model_key,
                }}
                digest = document_digest({**manifest, "desired_spec": payload})
            child_rows.append(
                {
                    "gateway_profile_id": prepared.spec.gateway_profile_id,
                    "desired_spec": payload,
                    "desired_spec_sha256": digest,
                    "credential_ciphertext": prepared.credential_ciphertext,
                    "expected_base_release_id": prepared.expected_base_release_id,
                    "initial_manifest": manifest,
                    "required": required,
                }
            )
        try:
            row = self._repository.create_multi_target_rollout(
                rollout_id,
                TargetRolloutKind.MODEL_ADD.value,
                logical_model_id,
                request_sha256,
                created_by,
                child_rows,
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        return self._multi_target_rollout_view(row)

    @staticmethod
    def _qualify_multi_target_runtime(
        prepared: _PreparedModelPublication,
    ) -> _PreparedModelPublication:
        latest = prepared.spec.bindings[-1]
        if latest.runtime_id is not None:
            return prepared
        target_key = hashlib.sha256(
            str(prepared.spec.gateway_profile_id).encode("ascii")
        ).hexdigest()[:8]
        suffix = f" · target-{target_key}"
        runtime_name = latest.runtime_name[: 160 - len(suffix)] + suffix
        bindings = [
            *prepared.spec.bindings[:-1],
            latest.model_copy(update={"runtime_name": runtime_name}),
        ]
        return _PreparedModelPublication(
            spec=prepared.spec.model_copy(update={"bindings": bindings}),
            credential_ciphertext=prepared.credential_ciphertext,
            expected_base_release_id=prepared.expected_base_release_id,
        )

    def _canonicalize_multi_target_model(
        self,
        prepared: _PreparedModelPublication,
        canonical: ModelTarget,
    ) -> _PreparedModelPublication:
        latest = prepared.spec.bindings[-1]
        candidate = latest.model
        if (
            candidate.family_key is not canonical.family_key
            or latest.api_format
            is not self._model_api_format_from_family(canonical.family_key)
        ):
            raise ControlPlaneConflictError(
                "Every target in a model rollout must use the same model family and protocol"
            )
        model = candidate.model_copy(
            update={
                "model_key": canonical.model_key,
                "display_name": canonical.display_name,
                "family_key": canonical.family_key,
                "capabilities": canonical.capabilities,
                "context_window": canonical.context_window,
                "input_cost_per_million": canonical.input_cost_per_million,
                "output_cost_per_million": canonical.output_cost_per_million,
                "cached_cost_per_million": canonical.cached_cost_per_million,
                "cache_write_cost_per_million": canonical.cache_write_cost_per_million,
                "price_source": canonical.price_source,
                "price_reference": canonical.price_reference,
                "price_discount_percent": canonical.price_discount_percent,
                "context_price_references": list(canonical.context_price_references),
                "context_price_rule": canonical.context_price_rule,
                "context_price_tiers": [
                    tier.model_copy(deep=True) for tier in canonical.context_price_tiers
                ],
                "list_input_cost_per_million": canonical.list_input_cost_per_million,
                "list_output_cost_per_million": canonical.list_output_cost_per_million,
                "list_cached_cost_per_million": canonical.list_cached_cost_per_million,
                "list_cache_write_cost_per_million": canonical.list_cache_write_cost_per_million,
                "allowed_roles": canonical.allowed_roles,
                "assignment_required": canonical.assignment_required,
            }
        )
        bindings = [
            *prepared.spec.bindings[:-1],
            latest.model_copy(update={"model": model}),
        ]
        discovery = [
            item
            for item in prepared.spec.discovery_models
            if item.id.casefold() != candidate.model_key.casefold()
        ]
        discovery.append(
            DiscoveryModel(
                id=canonical.model_key,
                display_name=canonical.display_name,
                api_format=latest.api_format,
            )
        )
        unique_discovery = {item.id.casefold(): item for item in discovery}
        spec = GatewayReleaseSpec(
            gateway_profile_id=prepared.spec.gateway_profile_id,
            discovery_models=sorted(
                unique_discovery.values(),
                key=lambda item: item.id.casefold(),
            ),
            bindings=bindings,
            gateway_deployments=prepared.spec.gateway_deployments,
        )
        return _PreparedModelPublication(
            spec=self._refresh_release_deployments(spec),
            credential_ciphertext=prepared.credential_ciphertext,
            expected_base_release_id=prepared.expected_base_release_id,
        )

    @staticmethod
    def _model_api_format_from_family(family: ModelFamilyKey) -> ApiFormat:
        return (
            ApiFormat.ANTHROPIC_MESSAGES
            if family is ModelFamilyKey.CLAUDE
            else ApiFormat.OPENAI_CHAT
        )

    def multi_target_rollout(
        self,
        rollout_id: UUID,
    ) -> tuple[MultiTargetRollout, list[GatewayPublication]]:
        row = self._repository.get_multi_target_rollout(rollout_id)
        if row is None:
            raise ControlPlaneNotFoundError("Multi-target rollout not found")
        return self._multi_target_rollout_view(row)

    def multi_target_rollouts(
        self,
        limit: int = 50,
    ) -> list[MultiTargetRollout]:
        return [
            self._multi_target_rollout_view(row)[0]
            for row in self._repository.list_multi_target_rollouts(limit)
        ]

    @staticmethod
    def _multi_target_rollout_view(
        row: Mapping[str, Any],
    ) -> tuple[MultiTargetRollout, list[GatewayPublication]]:
        publications: list[GatewayPublication] = []
        children: list[TargetRolloutChild] = []
        for child in row["children"]:
            publication = GatewayPublication.model_validate(child["publication"])
            publications.append(publication)
            status = publication.status
            child_status = (
                TargetRolloutChildStatus.QUEUED
                if status is PublicationStatus.QUEUED
                else TargetRolloutChildStatus.SUCCEEDED
                if status in {PublicationStatus.ACTIVE, PublicationStatus.SUPERSEDED}
                else TargetRolloutChildStatus.FAILED
                if status is PublicationStatus.FAILED
                else TargetRolloutChildStatus.ROLLING_BACK
                if status is PublicationStatus.ROLLING_BACK
                else TargetRolloutChildStatus.ROLLED_BACK
                if status is PublicationStatus.ROLLED_BACK
                else TargetRolloutChildStatus.IN_PROGRESS
            )
            children.append(
                TargetRolloutChild(
                    apim_target_id=UUID(str(child["apim_target_id"])),
                    publication_id=publication.id,
                    required=bool(child["required"]),
                    status=child_status,
                )
            )
        child_tuple = tuple(children)
        rollout = MultiTargetRollout(
            id=UUID(str(row["id"])),
            kind=TargetRolloutKind(str(row["rollout_kind"])),
            logical_model_id=(
                UUID(str(row["logical_model_id"]))
                if row.get("logical_model_id") is not None
                else None
            ),
            status=derive_target_rollout_status(child_tuple),
            children=child_tuple,
            created_by=str(row["created_by"]),
            created_at=row["created_at"],
        )
        return rollout, publications

    def remove_model(
        self, model_id: UUID, created_by: str, *, gateway_profile_id: UUID | None = None,
    ) -> GatewayPublication:
        registry = self._repository.registry()
        model = self._find(registry["models"], model_id)
        if model is None:
            raise ControlPlaneNotFoundError("Model not found")
        if model["is_default"]:
            raise ControlPlaneConflictError(
                "Select another default model before deleting this model"
            )
        runtime = self._find(registry["runtimes"], model["runtime_id"])
        if gateway_profile_id is not None:
            routes = [row for row in self._repository.list_apim_target_route_bindings(
                logical_model_id=model_id,
            ) if row["apim_target_id"] == gateway_profile_id]
            if len(routes) != 1:
                raise ControlPlaneConflictError("Model removal target route is unavailable")
            runtime = self._find(registry["runtimes"], routes[0]["runtime_id"])
        if runtime is None or runtime.get("gateway_profile_id") is None:
            raise ControlPlaneConflictError(
                "Only models routed through an APIM gateway can be deleted"
            )
        gateway_id = runtime["gateway_profile_id"]
        gateway = self._find(registry["gateways"], gateway_id)
        if gateway is None or gateway["implementation"] != "apim":
            raise ControlPlaneConflictError(
                "The model is not routed through a managed APIM gateway"
            )

        previous_row = self._repository.effective_gateway_publication(gateway_id)
        previous = (
            GatewayPublication.model_validate(previous_row)
            if previous_row is not None
            else None
        )
        alias = str(model["model_key"]).casefold()
        target_routes = [row for row in self._repository.list_apim_target_route_bindings(
            logical_model_id=model_id,
        ) if row["apim_target_id"] == gateway_id]
        if len(target_routes) > 1:
            raise ControlPlaneConflictError("Model removal target route is ambiguous")
        source_id = target_routes[0].get("current_publication_id") if target_routes else (
            model.get("publication_id")
        )
        source_row = self._repository.get_gateway_publication(source_id) \
            if source_id is not None else None
        if source_row is not None and "model_route_addition" in source_row.get(
            "resource_manifest", {},
        ):
            source = GatewayPublication.model_validate(source_row)
            if (previous is None or source.gateway_profile_id != gateway_id
                    or source.status is not PublicationStatus.ACTIVE
                    or source.publication_kind is not PublicationKind.MODEL_ADD
                    or len(source.desired_spec.bindings) != 1
                    or source.desired_spec.bindings[0].model.model_key.casefold() != alias):
                raise ControlPlaneConflictError("Model removal source is not active")
            removal = ModelRemovalTarget(
                model_id=model["id"], model_key=model["model_key"],
                display_name=model["display_name"],
                api_format=self._model_api_format(model, runtime),
            )
            spec = GatewayReleaseSpec(
                gateway_profile_id=gateway_id, discovery_models=[], bindings=[],
                removed_models=[removal],
                gateway_deployments=previous.desired_spec.gateway_deployments,
            )
            return self._queue_release(
                spec, PublicationKind.MODEL_REMOVE, created_by, None,
                expected_base_release_id=previous.id,
                initial_manifest={"model_route_removal": {
                    "version": 1, "program_release_id": str(previous.id),
                    "source_publication_id": str(source.id),
                    "source_spec_sha256": source.desired_spec_sha256,
                    "model_id": str(model["id"]), "alias": str(model["model_key"]),
                }},
            )
        bindings = [
            binding
            for binding in (previous.desired_spec.bindings if previous else [])
            if binding.model.model_key.casefold() != alias
        ]
        discovery = list(previous.desired_spec.discovery_models) if previous else []
        discovery.extend(self._existing_discovery(gateway_id, registry))
        remaining = {
            item.id.casefold(): item
            for item in discovery
            if item.id.casefold() != alias
        }
        spec = GatewayReleaseSpec(
            gateway_profile_id=gateway_id,
            discovery_models=sorted(remaining.values(), key=lambda item: item.id),
            bindings=bindings,
            gateway_deployments=(
                previous.desired_spec.gateway_deployments if previous else ()
            ),
            removed_models=[
                ModelRemovalTarget(
                    model_id=model["id"],
                    model_key=model["model_key"],
                    display_name=model["display_name"],
                    api_format=self._model_api_format(model, runtime),
                )
            ],
        )
        spec = self._refresh_release_deployments(spec)
        return self._queue_release(
            spec,
            PublicationKind.MODEL_REMOVE,
            created_by,
            None,
        )

    def rotate_credential(
        self,
        gateway_profile_id: UUID,
        write: GatewayCredentialRotation,
        created_by: str,
    ) -> GatewayPublication:
        previous_row = self._repository.effective_gateway_publication(
            gateway_profile_id
        )
        if previous_row is None:
            raise ControlPlaneConflictError("The gateway has no active publication")
        previous = GatewayPublication.model_validate(previous_row)
        target = next(
            (
                binding
                for binding in previous.desired_spec.bindings
                if binding.model.model_key.casefold() == write.model_key.casefold()
            ),
            None,
        )
        if target is None:
            raise ControlPlaneConflictError("The model is not managed by this gateway release")
        if target.oauth is not None:
            if not self._databricks_oauth_enabled or write.oauth_client_secret is None:
                raise ControlPlaneConflictError(
                    "OAuth rotation requires enablement and a client secret"
                )
            if self._cipher is None:
                raise ControlPlaneConflictError("Credential encryption is unavailable")
            registry = self._repository.registry()
            connection = self._binding_connection(target, gateway_profile_id, registry)
            oauth = target.oauth.model_copy(update={
                "provider_id": f"turnstile-oauth-{uuid4().hex}",
            })
            updated_bindings = []
            updated_target = target
            for binding in previous.desired_spec.bindings:
                if binding.runtime_id == connection["id"]:
                    if binding.oauth != target.oauth:
                        raise ControlPlaneConflictError(
                            "OAuth connection bindings have inconsistent ownership"
                        )
                    binding = binding.model_copy(update={"runtime_config": {
                        **binding.runtime_config, "oauth": oauth.model_dump(mode="json"),
                    }})
                if binding.model.model_key == target.model.model_key:
                    updated_target = binding
                else:
                    updated_bindings.append(binding)
            spec = previous.desired_spec.model_copy(update={
                "bindings": [*updated_bindings, updated_target],
            })
            return self._queue_release(
                spec, PublicationKind.CREDENTIAL_ROTATION, created_by,
                self._cipher.encrypt(write.oauth_client_secret.get_secret_value()),
            )
        if write.api_key is None:
            raise ControlPlaneConflictError("This connection requires an API key")
        if (
            not target.routing_managed
            or target.auth_strategy
            not in {
                AuthStrategy.NAMED_VALUE_BEARER,
                AuthStrategy.NAMED_VALUE_API_KEY,
            }
            or target.key_vault_secret_id is not None
        ):
            raise ControlPlaneConflictError(
                "The model does not use a directly managed API key"
            )
        if self._cipher is None:
            raise ControlPlaneConflictError("Credential encryption is unavailable")
        registry = self._repository.registry()
        connection = self._binding_connection(target, gateway_profile_id, registry)
        named_value_name = f"finops-credential-{uuid4().hex[:20]}"
        updated = []
        for binding in previous.desired_spec.bindings:
            if (
                binding.routing_managed
                and self._binding_connection(binding, gateway_profile_id, registry)["id"]
                == connection["id"]
            ):
                if (
                    binding.auth_strategy is not target.auth_strategy
                    or binding.key_vault_secret_id is not None
                ):
                    raise ControlPlaneConflictError(
                        "Connection bindings have incompatible credential ownership"
                    )
                binding = binding.model_copy(update={
                    "runtime_id": connection["id"],
                    "provider_id": connection["provider_id"],
                    "named_value_name": named_value_name,
                    "runtime_config": {
                        **binding.runtime_config,
                        "named_value_name": named_value_name,
                    },
                })
            pool = binding.backend_pool
            if pool is not None and any(
                member.runtime_id == connection["id"] for member in pool.members
            ):
                members = []
                for member in pool.members:
                    if member.runtime_id == connection["id"]:
                        if member.auth_strategy is not target.auth_strategy:
                            raise ControlPlaneConflictError(
                                "Pool member has incompatible credential ownership"
                            )
                        member = member.model_copy(update={"named_value_name": named_value_name})
                    members.append(member)
                binding = binding.model_copy(update={"runtime_config": {
                    **binding.runtime_config,
                    "apim_backend_pool": pool.model_copy(update={"members": members}).model_dump(
                        mode="json", exclude={"session_affinity"},
                    ),
                }})
            updated.append(binding)
        target_binding = next(
            binding
            for binding in updated
            if binding.model.model_key.casefold() == write.model_key.casefold()
        )
        bindings = [binding for binding in updated if binding is not target_binding]
        bindings.append(target_binding)
        discovery = list(previous.desired_spec.discovery_models)
        discovery.extend(self._existing_discovery(gateway_profile_id, registry))
        unique_discovery = {item.id.casefold(): item for item in discovery}
        spec = GatewayReleaseSpec(
            gateway_profile_id=gateway_profile_id,
            discovery_models=sorted(unique_discovery.values(), key=lambda item: item.id),
            bindings=bindings,
            gateway_deployments=previous.desired_spec.gateway_deployments,
        )
        spec = self._refresh_release_deployments(spec)
        encrypted = self._cipher.encrypt(write.api_key.get_secret_value())
        return self._queue_release(
            spec,
            PublicationKind.CREDENTIAL_ROTATION,
            created_by,
            encrypted,
        )

    @staticmethod
    def _binding_connection(
        binding: GatewayModelBinding,
        gateway_profile_id: UUID,
        registry: Mapping[str, Sequence[dict[str, Any]]],
    ) -> dict[str, Any]:
        runtime_id = binding.runtime_id
        if runtime_id is None:
            models = [
                model for model in registry["models"]
                if str(model["model_key"]).casefold() == binding.model.model_key.casefold()
            ]
            if len(models) == 1:
                runtime_id = models[0]["runtime_id"]
        matches = [
            runtime for runtime in registry["runtimes"]
            if runtime.get("gateway_profile_id") == gateway_profile_id
            and (
                runtime["id"] == runtime_id if runtime_id is not None
                else str(runtime["name"]).casefold() == binding.runtime_name.casefold()
            )
            and (binding.provider_id is None or runtime["provider_id"] == binding.provider_id)
        ]
        if len(matches) != 1 or not (matches[0].get("config") or {}).get("control_plane_managed"):
            raise ControlPlaneConflictError("The release Connection identity cannot be resolved")
        return matches[0]

    def _queue_release(
        self,
        spec: GatewayReleaseSpec,
        publication_kind: PublicationKind,
        created_by: str,
        credential_ciphertext: bytes | None,
        *, expected_base_release_id: UUID | None = None,
        initial_manifest: dict[str, Any] | None = None,
    ) -> GatewayPublication:
        payload, digest = self._release_payload(spec)
        manifest = initial_manifest
        if initial_manifest is not None:
            from ..domain.storage_routes import document_digest

            digest = document_digest({**initial_manifest, "desired_spec": payload})
        if publication_kind is PublicationKind.MODEL_ADD and expected_base_release_id is not None:
            from ..domain.storage_routes import (
                FixedProgramInstallation,
                FixedProgramUpgrade,
                canonical_document,
                document_digest,
            )
            from ..persistence.repository_support import _managed_program_publication

            if len(spec.bindings) != 1 or len(spec.discovery_models) != 1 or spec.removed_models:
                raise ControlPlaneConflictError("Model addition requires exactly one new model")
            previous = self._repository.effective_gateway_publication(spec.gateway_profile_id)
            gateways = self._repository.get_gateway_profiles([spec.gateway_profile_id])
            if len(gateways) != 1 or gateways[0]["id"] != spec.gateway_profile_id:
                raise ControlPlaneConflictError("Single-model publication Gateway is unavailable")
            previous = _managed_program_publication(previous, gateways[0].get("config") or {})
            installation = (previous or {}).get("resource_manifest", {}).get("fixed_route_program")
            if (previous is None or previous["id"] != expected_base_release_id
                    or not isinstance(installation, dict)):
                raise ControlPlaneConflictError("Single-model publication program is not installed")
            upgrade_record = (gateways[0].get("config") or {}).get(
                "fixed_route_program_upgrade",
            )
            if (installation.get("model_additions_enabled") is not True
                    or upgrade_record is not None):
                if not isinstance(upgrade_record, dict):
                    raise ControlPlaneConflictError(
                        "Single-model publication program is not installed",
                    )
                try:
                    upgrade = FixedProgramUpgrade.model_validate_json(
                        canonical_document(upgrade_record),
                    )
                    upgrade.require_previous(
                        str(expected_base_release_id), FixedProgramInstallation.model_validate_json(
                            canonical_document(installation),
                        ),
                    )
                except ValueError as error:
                    raise ControlPlaneConflictError(
                        "Single-model program registration changed",
                    ) from error
            manifest = {"model_route_addition": {
                "version": 1, "program_release_id": str(expected_base_release_id),
                "alias": spec.bindings[0].model.model_key,
            }}
            digest = document_digest({**manifest, "desired_spec": payload})
        try:
            row = self._repository.create_gateway_publication(
                spec.gateway_profile_id,
                payload,
                digest,
                publication_kind.value,
                created_by,
                credential_ciphertext,
                expected_base_release_id=expected_base_release_id,
                initial_manifest=manifest,
            )
        except ValueError as error:
            raise ControlPlaneConflictError(str(error)) from error
        return GatewayPublication.model_validate(row)

    @staticmethod
    def _release_payload(spec: GatewayReleaseSpec) -> tuple[dict[str, Any], str]:
        payload = spec.model_dump(mode="json")
        if not payload["gateway_deployments"]:
            payload.pop("gateway_deployments")
        for binding in payload["bindings"]:
            if binding["deployment_scope"] == {
                "mode": "all",
                "deployment_ids": [],
            }:
                binding.pop("deployment_scope")
            if not binding["serving_deployments"]:
                binding.pop("serving_deployments")
            if binding["logical_model_id"] is None:
                binding.pop("logical_model_id")
        canonical = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        return payload, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_release_spec(spec: GatewayReleaseSpec) -> GatewayReleaseSpec:
        bindings: list[GatewayModelBinding] = []
        for binding in spec.bindings:
            if "decision" in binding.model.capabilities:
                if binding.provider_brand_key is not BrandKey.MICROSOFT_FOUNDRY:
                    raise ControlPlaneConflictError("Decision requires Foundry")
                binding = GatewayModelBinding.model_validate({
                    **binding.model_dump(mode="python"),
                    **GatewayControlPlaneService._decision_binding_values({
                        "config": binding.runtime_config, "backend_url": binding.backend_url,
                        "auth_strategy": binding.auth_strategy,
                        "managed_identity_resource": binding.managed_identity_resource,
                    }),
                })
            elif "image_generation" in binding.model.capabilities:
                try:
                    profile = validate_image_profile(binding.model.image_profile)
                except ValueError as error:
                    raise ControlPlaneConflictError(str(error)) from error
                if binding.provider_brand_key is not BrandKey.MICROSOFT_FOUNDRY:
                    raise ControlPlaneConflictError("Image generation requires Foundry")
                if binding.backend_pool is not None:
                    raise ControlPlaneConflictError("Image generation pools are not supported")
                binding = binding.model_copy(update={
                    "api_format": ApiFormat.OPENAI_IMAGES,
                    "backend_url": GatewayControlPlaneService._image_backend_url(
                        {"backend_url": binding.backend_url, "config": binding.runtime_config}
                    ),
                    "backend_path": profile.backend_path,
                    "streaming_mode": StreamingMode.BUFFERED,
                })
            elif (
                binding.provider_brand_key is BrandKey.MICROSOFT_FOUNDRY
                and binding.model.family_key is ModelFamilyKey.CLAUDE
            ):
                config = dict(binding.runtime_config)
                endpoint = urlsplit(
                    str(config.get("project_endpoint") or binding.backend_url or "")
                )
                if endpoint.scheme != "https" or not (
                    endpoint.hostname or ""
                ).casefold().endswith(".services.ai.azure.com"):
                    raise ControlPlaneConflictError(
                        "Foundry Claude requires a services.ai.azure.com Project connection"
                    )
                binding = GatewayModelBinding.model_validate(
                    {
                        **binding.model_dump(mode="python"),
                        "api_format": ApiFormat.ANTHROPIC_MESSAGES,
                        "backend_url": f"{endpoint.scheme}://{endpoint.netloc}",
                        "backend_path": "/anthropic/v1/messages",
                        "runtime_config": {
                            **config,
                            "anthropic_version": "2023-06-01",
                        },
                    }
                )
            elif binding.api_format is ApiFormat.OPENAI_IMAGES:
                config = binding.runtime_config
                binding = binding.model_copy(update={
                    "api_format": ApiFormat.OPENAI_CHAT,
                    "backend_url": config.get("backend_url") or binding.backend_url,
                    "backend_path": "/openai/v1/chat/completions",
                    "streaming_mode": StreamingMode.NATIVE,
                })
            bindings.append(binding)
        formats = {
            binding.model.model_key.casefold(): binding.api_format
            for binding in bindings
        }
        discovery = [
            item.model_copy(update={"api_format": formats.get(item.id.casefold(), item.api_format)})
            for item in spec.discovery_models
        ]
        return spec.model_copy(
            update={"bindings": bindings, "discovery_models": discovery}
        )

    @staticmethod
    def _find(rows: Sequence[dict[str, Any]], item_id: UUID) -> dict[str, Any] | None:
        return next((row for row in rows if row["id"] == item_id), None)

    def _resolve_provider(
        self,
        write: GatewayPublicationCreate,
        providers: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        target = write.provider
        if target.existing_id is not None:
            provider = self._find(providers, target.existing_id)
            if provider is None or not provider["enabled"]:
                raise ControlPlaneConflictError("The selected provider is unavailable")
            return provider
        assert target.template is not None
        template = target.template
        model_vendor = write.runtime.model_vendor or ModelVendorKey.GENERIC
        provider_name = {
            "amazon_bedrock": "Amazon Bedrock",
            "microsoft_foundry": "Microsoft Foundry",
            "openai_compatible": model_vendor_label(model_vendor),
            "azure_databricks": "Azure Databricks",
        }[template]
        if template == "azure_databricks":
            raise ControlPlaneConflictError(
                "Select an existing Databricks Workspace connection and its provider"
            )
        if template == "openai_compatible":
            existing_provider = next(
                (
                    row
                    for row in providers
                    if row["enabled"]
                    and row["provider_kind"] == "openai_compatible"
                    and str((row.get("config") or {}).get("model_vendor", "generic"))
                    == model_vendor.value
                ),
                None,
            )
            if existing_provider is not None:
                return existing_provider
        if any(
            str(row["name"]).casefold() == provider_name.casefold()
            for row in providers
        ):
            raise ControlPlaneConflictError(
                f"A {provider_name} provider already exists; select it instead"
            )
        if template == "microsoft_foundry":
            return {
                "id": None,
                "name": provider_name,
                "provider_kind": "microsoft_foundry",
                "brand_key": BrandKey.MICROSOFT_FOUNDRY,
                "config": {"hosting_platform": "microsoft_foundry"},
            }
        if template == "openai_compatible":
            provider_brand = {
                ModelVendorKey.OPENAI: BrandKey.OPENAI,
                ModelVendorKey.ANTHROPIC: BrandKey.ANTHROPIC,
            }.get(model_vendor, BrandKey.GENERIC)
            return {
                "id": None,
                "name": provider_name,
                "provider_kind": "openai_compatible",
                "brand_key": provider_brand,
                "config": {
                    "hosting_platform": "openai_compatible",
                    "model_vendor": model_vendor.value,
                },
            }
        return {
            "id": None,
            "name": provider_name,
            "provider_kind": "anthropic",
            "brand_key": BrandKey.AMAZON_BEDROCK,
            "config": {"hosting_platform": "amazon_bedrock"},
        }

    def _resolve_runtime(
        self,
        write: GatewayPublicationCreate,
        provider: Mapping[str, Any],
        runtimes: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        target = write.runtime
        if target.existing_id is not None:
            runtime = self._find(runtimes, target.existing_id)
            if runtime is None or not runtime["enabled"]:
                raise ControlPlaneConflictError("The selected runtime is unavailable")
            if runtime["provider_id"] != provider["id"]:
                raise ControlPlaneConflictError(
                    "The selected runtime does not belong to the provider"
                )
            if runtime.get("gateway_profile_id") != write.gateway_profile_id:
                raise ControlPlaneConflictError(
                    "The selected runtime does not use the selected gateway"
                )
            config = dict(runtime.get("config") or {})
            auth_strategy = AuthStrategy(
                config.get("auth_strategy", AuthStrategy.NONE)
            )
            uses_oauth = auth_strategy is AuthStrategy.OAUTH_CLIENT_CREDENTIALS
            if uses_oauth and not self._databricks_oauth_enabled:
                raise ControlPlaneConflictError("Databricks OAuth M2M is not enabled")
            if (
                uses_oauth and target.api_key is not None
                or not uses_oauth and target.oauth_client_secret is not None
            ):
                raise ControlPlaneConflictError("The credential type does not match the connection")
            submitted_credential = target.oauth_client_secret if uses_oauth else target.api_key
            credential_pending = (
                config.get("credential_provisioned") is False
                and auth_strategy
                in {
                    AuthStrategy.NAMED_VALUE_BEARER,
                    AuthStrategy.NAMED_VALUE_API_KEY,
                    AuthStrategy.OAUTH_CLIENT_CREDENTIALS,
                }
            )
            if credential_pending and submitted_credential is None:
                raise ControlPlaneConflictError(
                    "The selected connection requires a one-time OAuth client secret "
                    "for its first model"
                    if uses_oauth else
                    "The selected connection requires a one-time API key for its first model"
                )
            if not credential_pending and submitted_credential is not None:
                raise ControlPlaneConflictError(
                    "The selected connection already has a provisioned credential"
                )
            if credential_pending or (
                config.get("credential_provisioned") is None
                and auth_strategy in {
                    AuthStrategy.NAMED_VALUE_BEARER, AuthStrategy.NAMED_VALUE_API_KEY,
                }
                and config.get("named_value_name")
            ):
                config["credential_provisioned"] = True
            runtime_brand = BrandKey(
                runtime.get("brand_key", provider["brand_key"])
            )
            api_format = self._runtime_api_format({**runtime, "brand_key": runtime_brand})
            routing_managed = bool(config.get("control_plane_managed", False))
            backend_path = config.get("backend_path")
            if not backend_path and routing_managed:
                if runtime_brand is BrandKey.MICROSOFT_FOUNDRY:
                    backend_path = "/openai/v1/chat/completions"
                elif runtime_brand is BrandKey.AMAZON_BEDROCK:
                    backend_path = "/model/{upstream_model_id}/invoke"
                else:
                    raise ControlPlaneConflictError(
                        "The selected managed runtime is missing its provider backend path"
                    )
            if not backend_path:
                backend_path = config.get("path", "/v1/messages")
            return {
                "id": runtime["id"],
                "routing_managed": routing_managed,
                "name": runtime["name"],
                "runtime_kind": runtime["runtime_kind"],
                "brand_key": runtime_brand,
                "api_format": api_format,
                "backend_url": config.get("backend_url"),
                "backend_path": backend_path,
                "auth_strategy": auth_strategy,
                "named_value_name": config.get("named_value_name"),
                "key_vault_secret_id": config.get("key_vault_secret_id"),
                "managed_identity_resource": config.get("managed_identity_resource"),
                "streaming_mode": config.get("streaming_mode", StreamingMode.NATIVE),
                "price_discount_percent": runtime.get("price_discount_percent"),
                "config": config,
            }
        brand = BrandKey(provider["brand_key"])
        if brand is BrandKey.AZURE_DATABRICKS:
            raise ControlPlaneConflictError(
                "Select an existing Databricks Workspace connection"
            )
        if provider["provider_kind"] == "openai_compatible":
            if target.openai_base_url is None or target.api_key is None:
                raise ControlPlaneConflictError(
                    "OpenAI-compatible publication requires a Base URL and API key"
                )
            try:
                base_url, backend_url, backend_path = openai_compatible_endpoint_values(
                    str(target.openai_base_url)
                )
            except ValueError as error:
                raise ControlPlaneConflictError(str(error)) from error
            duplicate_runtime = next(
                (
                    row
                    for row in runtimes
                    if row.get("gateway_profile_id") == write.gateway_profile_id
                    and str((row.get("config") or {}).get("base_url", ""))
                    .rstrip("/")
                    .casefold()
                    == base_url.casefold()
                ),
                None,
            )
            if duplicate_runtime is not None:
                raise ControlPlaneConflictError(
                    "An OpenAI-compatible runtime already exists for this gateway and Base URL"
                )
            credential_scope = f"{write.gateway_profile_id}:{base_url.casefold()}"
            generated_named_value = (
                "turnstile-openai-"
                + hashlib.sha256(credential_scope.encode("utf-8")).hexdigest()[:16]
            )
            configured_vendor = str(
                (provider.get("config") or {}).get("model_vendor", "generic")
            )
            model_vendor = target.model_vendor or ModelVendorKey(configured_vendor)
            if target.model_vendor is not None and configured_vendor not in {
                "generic",
                target.model_vendor.value,
            }:
                raise ControlPlaneConflictError(
                    "The selected runtime vendor does not match its provider"
                )
            runtime_brand = {
                ModelVendorKey.OPENAI: BrandKey.OPENAI,
                ModelVendorKey.ANTHROPIC: BrandKey.ANTHROPIC,
            }.get(model_vendor, BrandKey.GENERIC)
            return {
                "id": None,
                "routing_managed": True,
                "name": openai_compatible_runtime_name(base_url, model_vendor),
                "runtime_kind": "openai_compatible",
                "brand_key": runtime_brand,
                "api_format": ApiFormat.OPENAI_CHAT,
                "backend_url": backend_url,
                "backend_path": backend_path,
                "auth_strategy": AuthStrategy.NAMED_VALUE_BEARER,
                "named_value_name": generated_named_value,
                "key_vault_secret_id": None,
                "managed_identity_resource": None,
                "streaming_mode": StreamingMode.NATIVE,
                "config": {
                    "base_url": base_url,
                    "model_vendor": model_vendor.value,
                    "credential_kind": "api_key",
                    "max_tokens_field": "max_tokens",
                    "supports_temperature": True,
                },
            }
        if brand is BrandKey.AMAZON_BEDROCK:
            assert target.bedrock_runtime_url is not None
            assert target.api_key is not None
            assert write.model.model_key is not None
            endpoint = urlsplit(str(target.bedrock_runtime_url))
            host = (endpoint.hostname or "").casefold()
            suffix = ".amazonaws.com"
            prefix = "bedrock-runtime."
            region = (
                host.removeprefix(prefix).removesuffix(suffix)
                if host.startswith(prefix) and host.endswith(suffix)
                else ""
            )
            if endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment:
                raise ControlPlaneConflictError(
                    "The Bedrock Runtime URL must contain only the regional origin"
                )
            backend_url = f"{endpoint.scheme}://{endpoint.netloc}"
            duplicate_runtime = next(
                (
                    row
                    for row in runtimes
                    if row.get("provider_id") == provider.get("id")
                    and row.get("gateway_profile_id") == write.gateway_profile_id
                    and str((row.get("config") or {}).get("backend_url", "")).rstrip("/").casefold()
                    == backend_url.rstrip("/").casefold()
                ),
                None,
            )
            if duplicate_runtime is not None:
                raise ControlPlaneConflictError(
                    "A Bedrock runtime already exists for this gateway and Region; "
                    "select it or rotate its credential"
                )
            runtime_name = f"Amazon Bedrock Claude ({region}) via APIM"
            credential_scope = (
                f"{write.gateway_profile_id}:{backend_url}:"
                f"{write.model.model_key.casefold()}"
            )
            generated_named_value = (
                "turnstile-bedrock-"
                + hashlib.sha256(credential_scope.encode("utf-8")).hexdigest()[:16]
            )
            return {
                "id": None,
                "routing_managed": True,
                "name": runtime_name,
                "runtime_kind": "openai_compatible",
                "brand_key": BrandKey.AMAZON_BEDROCK,
                "api_format": ApiFormat.ANTHROPIC_MESSAGES,
                "backend_url": backend_url,
                "backend_path": "/model/{upstream_model_id}/invoke",
                "auth_strategy": AuthStrategy.NAMED_VALUE_BEARER,
                "named_value_name": generated_named_value,
                "key_vault_secret_id": None,
                "managed_identity_resource": None,
                "streaming_mode": StreamingMode.BUFFERED,
                "config": {"region": region},
            }
        if brand is BrandKey.MICROSOFT_FOUNDRY:
            assert target.foundry_project_endpoint is not None
            uses_api_key = target.foundry_inference_endpoint is not None
            principal_id = self._apim_principal_id
            if not uses_api_key and self._target_registry_enabled:
                registered = self._repository.get_apim_target(write.gateway_profile_id)
                if registered is None:
                    raise ControlPlaneConflictError("Selected APIM target identity is unavailable")
                selected = ApimTarget.model_validate({
                    name: registered[name] for name in ApimTarget.model_fields if name in registered
                })
                if (
                    selected.gateway_profile_id != write.gateway_profile_id
                    or selected.drift_state is ApimTargetDriftState.DRIFTED
                ):
                    raise ControlPlaneConflictError("Selected APIM target identity has changed")
                principal_id = str(selected.principal_id) if (
                    selected.principal_id is not None
                ) else None
            if not uses_api_key and not principal_id:
                raise ControlPlaneConflictError(
                    "APIM managed identity is not configured for Foundry onboarding"
                )
            endpoint = urlsplit(str(target.foundry_project_endpoint))
            host = (endpoint.hostname or "").casefold()
            suffix = ".services.ai.azure.com"
            account = host.removesuffix(suffix) if host.endswith(suffix) else ""
            path_match = re.fullmatch(
                r"/api/projects/(?P<project>[A-Za-z0-9._-]+)/?",
                endpoint.path,
            )
            if (
                endpoint.scheme != "https"
                or not account
                or path_match is None
                or endpoint.query
                or endpoint.fragment
                or endpoint.username is not None
            ):
                raise ControlPlaneConflictError(
                    "The Foundry project endpoint must use "
                    "https://<account>.services.ai.azure.com/api/projects/<project>"
                )
            project = unquote(path_match.group("project"))
            project_endpoint = (
                f"{endpoint.scheme}://{endpoint.netloc}/api/projects/{project}"
            )
            backend_url = project_endpoint
            auth_strategy = AuthStrategy.MANAGED_IDENTITY
            named_value_name = None
            managed_identity_resource: str | None = FOUNDRY_INFERENCE_RESOURCE
            runtime_config: dict[str, object] = {
                "project_endpoint": project_endpoint,
                "max_tokens_field": "max_completion_tokens",
                "supports_temperature": False,
            }
            if uses_api_key:
                assert target.foundry_inference_endpoint is not None
                assert target.api_key is not None
                inference = urlsplit(str(target.foundry_inference_endpoint))
                inference_host = (inference.hostname or "").casefold()
                valid_inference_host = inference_host in {
                    f"{account}.openai.azure.com",
                    f"{account}.services.ai.azure.com",
                }
                if (
                    inference.scheme != "https"
                    or not valid_inference_host
                    or inference.path.rstrip("/") != "/openai/v1"
                    or inference.query
                    or inference.fragment
                    or inference.username is not None
                ):
                    raise ControlPlaneConflictError(
                        "The Foundry inference endpoint must use the same account at "
                        "https://<account>.openai.azure.com/openai/v1 or "
                        "https://<account>.services.ai.azure.com/openai/v1"
                    )
                backend_url = f"{inference.scheme}://{inference.netloc}"
                credential_scope = (
                    f"{write.gateway_profile_id}:{project_endpoint}:{backend_url}"
                )
                named_value_name = (
                    "turnstile-foundry-key-"
                    + hashlib.sha256(credential_scope.encode("utf-8")).hexdigest()[:16]
                )
                auth_strategy = AuthStrategy.NAMED_VALUE_API_KEY
                managed_identity_resource = None
                runtime_config.update(
                    inference_endpoint=str(target.foundry_inference_endpoint).rstrip("/"),
                    credential_kind="api_key",
                )
            else:
                runtime_config["authorization"] = {
                    "kind": "azure_rbac",
                    "principal_id": principal_id,
                    "resource_endpoint": f"{endpoint.scheme}://{endpoint.netloc}",
                        "role_id": FOUNDRY_INFERENCE_ROLE_ID,
                    "role_name": "Cognitive Services User",
                }
            duplicate_runtime = next(
                (
                    row
                    for row in runtimes
                    if row.get("gateway_profile_id") == write.gateway_profile_id
                    and str((row.get("config") or {}).get("project_endpoint", ""))
                    .rstrip("/")
                    .casefold()
                    == project_endpoint.casefold()
                ),
                None,
            )
            if duplicate_runtime is not None:
                raise ControlPlaneConflictError(
                    "A Foundry runtime already exists for this gateway and project"
                )
            return {
                "id": None,
                "routing_managed": True,
                "name": foundry_runtime_name(account, project),
                "runtime_kind": "foundry",
                "brand_key": BrandKey.MICROSOFT_FOUNDRY,
                "api_format": ApiFormat.OPENAI_CHAT,
                "backend_url": backend_url,
                "backend_path": "/openai/v1/chat/completions",
                "auth_strategy": auth_strategy,
                "named_value_name": named_value_name,
                "key_vault_secret_id": None,
                "managed_identity_resource": managed_identity_resource,
                "streaming_mode": StreamingMode.NATIVE,
                "config": runtime_config,
            }
        raise ControlPlaneConflictError(
            "New runtime publication requires a supported provider template"
        )

    @staticmethod
    def _validate_model(
        model: ModelTarget, models: Sequence[dict[str, Any]]
    ) -> None:
        alias = model.model_key.casefold()
        if any(str(row["model_key"]).casefold() == alias for row in models):
            raise ControlPlaneConflictError("The model alias is already active")

    @staticmethod
    def _release_model(
        model: ModelCreateTarget,
        provider: Mapping[str, Any],
        runtime: Mapping[str, Any],
    ) -> ModelTarget:
        brand = BrandKey(provider["brand_key"])
        profile = None
        if model.operation == "decision" and brand is not BrandKey.MICROSOFT_FOUNDRY:
            raise ControlPlaneConflictError("Decision requires a Microsoft Foundry connection")
        if model.operation == "image_generation":
            try:
                if model.image_configuration is None:
                    raise ValueError("Image generation requires explicit configuration")
                profile = create_image_profile(
                    model.image_configuration, (model.deployment_name or "").strip()
                )
            except ValueError as error:
                raise ControlPlaneConflictError(str(error)) from error
        if model.operation == "image_generation" and brand is not BrandKey.MICROSOFT_FOUNDRY:
            raise ControlPlaneConflictError(
                "Image generation currently requires a Microsoft Foundry connection"
            )
        if model.operation == "image_generation" and any(rate is None for rate in (
            model.input_cost_per_million, model.cached_cost_per_million,
            model.output_cost_per_million,
        )):
            raise ControlPlaneConflictError(
                "Image generation requires explicit text, cached-text and image-output prices"
            )
        if brand is BrandKey.MICROSOFT_FOUNDRY:
            if model.deployment_name is None:
                raise ControlPlaneConflictError(
                    "Microsoft Foundry requires an existing deployment name"
                )
            deployment = model.deployment_name.strip()
            config = dict(runtime.get("config") or {})
            connection = str(
                config.get("project_endpoint") or runtime.get("backend_url") or ""
            ).rstrip("/").casefold()
            if not connection:
                raise ControlPlaneConflictError(
                    "The selected runtime is not a managed Foundry Project connection"
                )
            slug = re.sub(r"[^a-z0-9._:-]+", "-", deployment.casefold()).strip("-")
            if not slug:
                raise ControlPlaneConflictError("The Foundry deployment name is invalid")
            suffix = hashlib.sha256(connection.encode("utf-8")).hexdigest()[:8]
            model_key = f"{slug[:220]}-foundry-{suffix}"
            identity = f"{model_key} {deployment}".casefold()
            family = (
                ModelFamilyKey.CLAUDE
                if "claude" in identity
                else ModelFamilyKey.OPENAI
                if "gpt" in identity or "openai" in identity
                else ModelFamilyKey.GENERIC
            )
            return ModelTarget(
                model_key=model_key,
                display_name=f"{deployment} · Microsoft Foundry",
                upstream_model_id=deployment,
                family_key=family,
                capabilities=(
                    ["image_generation"]
                    if model.operation == "image_generation"
                    else ["decision"]
                    if model.operation == "decision"
                    else ["chat", "streaming"]
                ),
                context_window=model.context_window,
                input_cost_per_million=model.input_cost_per_million,
                output_cost_per_million=model.output_cost_per_million,
                cached_cost_per_million=model.cached_cost_per_million,
                cache_write_cost_per_million=model.cache_write_cost_per_million,
                price_source=model.price_source or PriceSource.MANUAL,
                price_reference=model.price_reference,
                price_discount_percent=model.price_discount_percent,
                context_price_references=model.context_price_references,
                context_price_rule=model.context_price_rule,
                allowed_roles=["owner", "admin", "member"],
                assignment_required=True,
                image_profile=profile,
            )
        assert model.model_key is not None
        assert model.display_name is not None
        assert model.upstream_model_id is not None
        if brand is BrandKey.AZURE_DATABRICKS and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", model.upstream_model_id
        ) is None:
            raise ControlPlaneConflictError("The Databricks serving endpoint name is invalid")
        identity = f"{model.model_key} {model.display_name} {model.upstream_model_id}".casefold()
        if brand is BrandKey.AMAZON_BEDROCK and "claude" not in identity:
            raise ControlPlaneConflictError(
                "Automated Anthropic publication requires a Claude model"
            )
        family = (
            ModelFamilyKey.CLAUDE
            if "claude" in identity or brand is BrandKey.AZURE_DATABRICKS
            else ModelFamilyKey.OPENAI
            if "gpt" in identity or "openai" in identity
            else ModelFamilyKey.GENERIC
        )
        capabilities: list[ModelCapability] = (
            ["chat", "tools", "vision", "reasoning", "streaming"]
            if runtime["api_format"] == ApiFormat.ANTHROPIC_MESSAGES
            and brand is not BrandKey.AZURE_DATABRICKS
            else ["chat", "streaming"]
        )
        return ModelTarget(
            **model.model_dump(exclude={
                "deployment_name", "operation", "image_configuration",
                "price_source", "price_reference", "price_discount_percent",
                "price_entry_digest", "allow_unpriced",
            }),
            price_source=model.price_source or PriceSource.MANUAL,
            price_reference=model.price_reference,
            price_discount_percent=model.price_discount_percent,
            family_key=family,
            capabilities=capabilities,
            allowed_roles=["owner", "admin", "member"],
            assignment_required=True,
        )

    @staticmethod
    def _image_backend_url(runtime: Mapping[str, Any]) -> HttpUrl:
        config = dict(runtime.get("config") or {})
        endpoint = urlsplit(str(
            config.get("inference_endpoint") or config.get("project_endpoint")
            or runtime.get("backend_url") or ""
        ))
        host = (endpoint.hostname or "").casefold()
        if endpoint.scheme != "https" or endpoint.username or endpoint.port:
            raise ControlPlaneConflictError("Image generation requires a Foundry HTTPS endpoint")
        if host.endswith(".services.ai.azure.com"):
            host = host.removesuffix(".services.ai.azure.com") + ".openai.azure.com"
        if not host.endswith(".openai.azure.com"):
            raise ControlPlaneConflictError("Image generation requires an Azure OpenAI endpoint")
        return HttpUrl("https://" + host)

    @staticmethod
    def _decision_binding_values(runtime: Mapping[str, Any]) -> dict[str, Any]:
        config = dict(runtime.get("config") or {})
        endpoint = urlsplit(str(
            config.get("inference_endpoint") or config.get("project_endpoint")
            or runtime.get("backend_url") or ""
        ))
        host = (endpoint.hostname or "").casefold()
        if host.endswith(".openai.azure.com"):
            host = host.removesuffix(".openai.azure.com") + ".services.ai.azure.com"
        if (endpoint.scheme != "https" or endpoint.username or endpoint.password
                or endpoint.port or endpoint.query or endpoint.fragment
                or not host.endswith(".services.ai.azure.com")):
            raise ControlPlaneConflictError("Decision requires a Foundry HTTPS resource endpoint")
        return {
            "api_format": ApiFormat.MICROSOFT_DECISION,
            "backend_url": str(HttpUrl("https://" + host)).rstrip("/"),
            "backend_path": "/providers/microsoft/v1/systemone",
            "streaming_mode": StreamingMode.BUFFERED,
            "managed_identity_resource": (
                "https://cognitiveservices.azure.com"
                if runtime.get("auth_strategy") in {
                    AuthStrategy.MANAGED_IDENTITY, "managed_identity",
                }
                else runtime.get("managed_identity_resource")
            ),
        }

    @staticmethod
    def _model_binding_runtime(
        provider: Mapping[str, Any],
        runtime: Mapping[str, Any],
        model: ModelTarget,
    ) -> dict[str, Any]:
        result = dict(runtime)
        if "decision" in model.capabilities:
            if BrandKey(provider["brand_key"]) is not BrandKey.MICROSOFT_FOUNDRY:
                raise ControlPlaneConflictError("Decision requires a Microsoft Foundry connection")
            result.update(GatewayControlPlaneService._decision_binding_values(runtime))
            return result
        if "image_generation" in model.capabilities:
            profile = validate_image_profile(model.image_profile)
            result.update(
                api_format=ApiFormat.OPENAI_IMAGES,
                backend_url=str(GatewayControlPlaneService._image_backend_url(runtime)).rstrip("/"),
                backend_path=profile.backend_path,
                streaming_mode=StreamingMode.BUFFERED,
            )
            return result
        if (
            BrandKey(provider["brand_key"]) is not BrandKey.MICROSOFT_FOUNDRY
            or model.family_key is not ModelFamilyKey.CLAUDE
        ):
            return result
        config = dict(runtime.get("config") or {})
        endpoint = urlsplit(
            str(config.get("project_endpoint") or runtime.get("backend_url") or "")
        )
        if endpoint.scheme != "https" or not (
            endpoint.hostname or ""
        ).casefold().endswith(".services.ai.azure.com"):
            raise ControlPlaneConflictError(
                "Foundry Claude requires a services.ai.azure.com Project connection"
            )
        result.update(
            api_format=ApiFormat.ANTHROPIC_MESSAGES,
            backend_url=f"{endpoint.scheme}://{endpoint.netloc}",
            backend_path="/anthropic/v1/messages",
            config={**config, "anthropic_version": "2023-06-01"},
        )
        return result

    @classmethod
    def _validate_connection(
        cls, provider: Mapping[str, Any], runtime: Mapping[str, Any]
    ) -> None:
        brand = BrandKey(provider["brand_key"])
        if brand is BrandKey.AZURE_DATABRICKS:
            config = dict(runtime.get("config") or {})
            if not runtime.get("routing_managed") or not config.get("workspace_url"):
                raise ControlPlaneConflictError(
                    "Adopt the legacy Databricks connection before publishing new models"
                )
            try:
                workspace = databricks_workspace_url(str(config["workspace_url"]))
                backend = databricks_workspace_url(str(runtime.get("backend_url") or ""))
            except ValueError as error:
                raise ControlPlaneConflictError(str(error)) from error
            if workspace != backend or runtime.get("backend_path") != DATABRICKS_ANTHROPIC_PATH:
                raise ControlPlaneConflictError("The Databricks backend must match its Workspace")
            if (
                provider["provider_kind"] != "anthropic"
                or runtime["api_format"] != ApiFormat.ANTHROPIC_MESSAGES
                or runtime["streaming_mode"] != StreamingMode.NATIVE
            ):
                raise ControlPlaneConflictError(
                    "Databricks Claude requires native Anthropic Messages"
                )
            if runtime["auth_strategy"] == AuthStrategy.OAUTH_CLIENT_CREDENTIALS:
                oauth = OAuthClientCredentialsConfig.model_validate(config.get("oauth"))
                if (
                    str(oauth.token_url) != workspace + "/oidc/v1/token"
                    or runtime.get("managed_identity_resource") is not None
                    or runtime.get("named_value_name") is not None
                ):
                    raise ControlPlaneConflictError("Databricks OAuth must match its Workspace")
                return
            if (
                runtime["auth_strategy"] != AuthStrategy.MANAGED_IDENTITY
                or runtime.get("managed_identity_resource") != DATABRICKS_INFERENCE_RESOURCE
            ):
                raise ControlPlaneConflictError(
                    "Databricks requires its configured managed-identity token audience"
                )
            return
        if runtime.get("id") is not None and brand is not BrandKey.MICROSOFT_FOUNDRY:
            return
        url = str(runtime["backend_url"])
        host = (urlsplit(url).hostname or "").casefold()
        if provider["provider_kind"] == "openai_compatible":
            if urlsplit(url).scheme != "https" or not host:
                raise ControlPlaneConflictError("OpenAI-compatible backends require HTTPS")
            if runtime["api_format"] != ApiFormat.OPENAI_CHAT:
                raise ControlPlaneConflictError(
                    "OpenAI-compatible runtimes require the Chat Completions format"
                )
            if runtime["auth_strategy"] != AuthStrategy.NAMED_VALUE_BEARER:
                raise ControlPlaneConflictError(
                    "OpenAI-compatible runtimes require bearer API-key authentication"
                )
            if not runtime["routing_managed"] or not runtime.get("backend_path"):
                raise ControlPlaneConflictError(
                    "The selected runtime is not a managed OpenAI-compatible connection"
                )
            if runtime["streaming_mode"] != StreamingMode.NATIVE:
                raise ControlPlaneConflictError(
                    "OpenAI-compatible runtimes require native streaming"
                )
            return
        if brand is BrandKey.AMAZON_BEDROCK:
            if runtime["api_format"] != ApiFormat.ANTHROPIC_MESSAGES:
                raise ControlPlaneConflictError(
                    "Amazon Bedrock Claude requires the Anthropic Messages format"
                )
            if provider["provider_kind"] != "anthropic":
                raise ControlPlaneConflictError(
                    "Amazon Bedrock Claude must use the Anthropic Messages protocol"
                )
            if not host.startswith("bedrock-runtime.") or not host.endswith(".amazonaws.com"):
                raise ControlPlaneConflictError(
                    "Amazon Bedrock backends must use a regional "
                    "bedrock-runtime.amazonaws.com endpoint"
                )
            if runtime["auth_strategy"] != AuthStrategy.NAMED_VALUE_BEARER:
                raise ControlPlaneConflictError(
                    "Amazon Bedrock requires a release-scoped bearer named value"
                )
            # This is an AWS Bedrock short/long-term API key, whose verified wire shape is
            # `Authorization: Bearer`. It is not an IAM access-key pair and must not be
            # silently converted to SigV4 without a separate credential model.
            if runtime["streaming_mode"] != StreamingMode.BUFFERED:
                raise ControlPlaneConflictError(
                    "Amazon Bedrock requires buffered Anthropic streaming in APIM"
                )
            return
        if brand is BrandKey.MICROSOFT_FOUNDRY:
            if provider["provider_kind"] != "microsoft_foundry":
                raise ControlPlaneConflictError(
                    "Microsoft Foundry must use the Foundry provider kind"
                )
            config = dict(runtime.get("config") or {})
            auth_strategy = AuthStrategy(runtime["auth_strategy"])
            if (
                auth_strategy is AuthStrategy.MANAGED_IDENTITY
                and not host.endswith(".services.ai.azure.com")
            ):
                raise ControlPlaneConflictError(
                    "Foundry project endpoints must use services.ai.azure.com"
                )
            if auth_strategy not in {
                AuthStrategy.MANAGED_IDENTITY,
                AuthStrategy.NAMED_VALUE_API_KEY,
            }:
                raise ControlPlaneConflictError(
                    "Microsoft Foundry requires a supported authentication strategy"
                )
            if (
                auth_strategy is AuthStrategy.MANAGED_IDENTITY
                and runtime["managed_identity_resource"] != FOUNDRY_INFERENCE_RESOURCE
            ):
                raise ControlPlaneConflictError(
                    "Microsoft Foundry requires the ai.azure.com token audience"
                )
            if auth_strategy is AuthStrategy.NAMED_VALUE_API_KEY and (
                not runtime.get("named_value_name")
                or not config.get("inference_endpoint")
            ):
                raise ControlPlaneConflictError(
                    "Foundry API-key authentication requires a secret named value"
                )
            if runtime["api_format"] != ApiFormat.OPENAI_CHAT:
                raise ControlPlaneConflictError(
                    "Microsoft Foundry onboarding requires the OpenAI chat format"
                )
            if not runtime["routing_managed"] or not config.get("project_endpoint"):
                raise ControlPlaneConflictError(
                    "The selected runtime is not a managed Foundry Project connection"
                )
            return
        raise ControlPlaneConflictError(
            "New runtime publication requires a supported provider template"
        )

    @staticmethod
    def _existing_discovery(
        gateway_profile_id: UUID, registry: Mapping[str, Sequence[dict[str, Any]]]
    ) -> list[DiscoveryModel]:
        runtimes = {
            row["id"]: row
            for row in registry["runtimes"]
            if row.get("gateway_profile_id") == gateway_profile_id and row["enabled"]
        }
        result: list[DiscoveryModel] = []
        for model in registry["models"]:
            runtime = runtimes.get(model["runtime_id"])
            if runtime is None or not model["enabled"]:
                continue
            result.append(
                DiscoveryModel(
                    id=model["model_key"],
                    display_name=model["display_name"],
                    api_format=GatewayControlPlaneService._model_api_format(
                        model, runtime
                    ),
                )
            )
        return result

    @staticmethod
    def _model_api_format(
        model: Mapping[str, Any], runtime: Mapping[str, Any]
    ) -> ApiFormat:
        if "decision" in (model.get("capabilities") or []):
            return ApiFormat.MICROSOFT_DECISION
        if "image_generation" in (model.get("capabilities") or []):
            return ApiFormat.OPENAI_IMAGES
        if (
            runtime.get("brand_key") in {
                BrandKey.MICROSOFT_FOUNDRY,
                BrandKey.MICROSOFT_FOUNDRY.value,
            }
            and model.get("family_key") in {
                ModelFamilyKey.CLAUDE,
                ModelFamilyKey.CLAUDE.value,
            }
        ):
            return ApiFormat.ANTHROPIC_MESSAGES
        return GatewayControlPlaneService._runtime_api_format(runtime)

    @staticmethod
    def _runtime_api_format(runtime: Mapping[str, Any]) -> ApiFormat:
        config = dict(runtime.get("config") or {})
        if config.get("api_format"):
            return ApiFormat(config["api_format"])
        brand = runtime.get("brand_key")
        if config.get("api_format") == ApiFormat.ANTHROPIC_MESSAGES or brand in {
            BrandKey.AMAZON_BEDROCK,
            BrandKey.ANTHROPIC,
            BrandKey.AZURE_DATABRICKS,
            "amazon_bedrock",
            "anthropic",
            "azure_databricks",
        }:
            return ApiFormat.ANTHROPIC_MESSAGES
        return ApiFormat.OPENAI_CHAT


    def _price_model(
        self, model: ModelTarget, runtime: Mapping[str, Any],
        *, expected_digest: str | None = None,
    ) -> ModelTarget:
        if model.price_source is PriceSource.MANUAL:
            return model
        percent = model.price_discount_percent
        if percent is None and runtime.get("price_discount_percent") is not None:
            percent = float(runtime["price_discount_percent"])
        if model.context_price_references:
            try:
                tiers = resolve_context_prices(
                    self._price_catalog, model.price_source,
                    model.context_price_references, percent,
                    rule=model.context_price_rule, previous_tiers=model.context_price_tiers,
                )
            except ValueError as error:
                raise ControlPlaneConflictError(str(error)) from error
            except Exception as error:
                raise ControlPlaneUnavailableError(
                    "The selected price source is unavailable"
                ) from error
            return model.model_copy(update={
                "context_price_tiers": tiers,
                "context_price_references": [tier.reference for tier in tiers],
                "price_reference": context_price_reference(
                    model.price_source, [tier.reference for tier in tiers],
                ),
                "input_cost_per_million": None, "output_cost_per_million": None,
                "cached_cost_per_million": None, "cache_write_cost_per_million": None,
            })
        try:
            entry = self._price_catalog.lookup(model.price_reference or "")
        except Exception as error:  # noqa: BLE001
            raise ControlPlaneUnavailableError(
                "The selected price source is unavailable"
            ) from error
        if (
            entry is None or not entry.complete or not entry.priced or entry.unsupported
            or entry.source != model.price_source
            or entry.operation != (
                "image_generation" if "image_generation" in model.capabilities else "chat"
            )
            or model.price_source is PriceSource.MODELS_DEV and "decision" in model.capabilities
            or model.image_profile is not None
            and (not entry.text_to_image or entry.cached_per_million is None)
        ):
            raise ControlPlaneConflictError(
                "The selected catalog price is incomplete or unavailable"
            )
        if entry.context_tier is not None or parse_context_tier(entry.label) is not None:
            raise ControlPlaneConflictError(
                "Select the complete context price schedule, not one tier"
            )
        if expected_digest is not None and expected_digest != entry.digest:
            raise ControlPlaneConflictError("Catalog prices changed; synchronize and confirm again")
        charged = entry.discounted(percent)
        values: dict[str, Any] = {
            "input_cost_per_million": charged.input_per_million,
            "output_cost_per_million": charged.output_per_million,
            "cached_cost_per_million": charged.cached_per_million,
            "cache_write_cost_per_million": charged.cache_write_per_million,
            "list_input_cost_per_million": entry.input_per_million,
            "list_output_cost_per_million": entry.output_per_million,
            "list_cached_cost_per_million": entry.cached_per_million,
            "list_cache_write_cost_per_million": entry.cache_write_per_million,
        }
        if model.price_source is PriceSource.MODELS_DEV:
            values["price_configuration"] = {
                **values,
                "price_source": model.price_source.value,
                "price_reference": entry.reference,
                "match_metadata": {
                    "reference": entry.reference, "provider_id": entry.provider_id,
                    "model_id": entry.model_id, "name": entry.label,
                    "canonical_model_id": entry.canonical_model_id,
                    "method": "manual",
                },
                "source_snapshot": {**entry.metadata, "version": 1, "entry_digest": entry.digest},
            }
        return model.model_copy(update=values)
