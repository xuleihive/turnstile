from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID, uuid4

import httpx

from ..domain.application_access import GatewayApplicationSubscriptionProvisionSpec
from ..domain.control_plane import (
    GatewayPublication,
    GatewayReleaseDependencies,
    GatewayReleaseOperation,
    GatewayReleaseOperationKind,
    GatewayReleaseRetentionPolicy,
)
from ..domain.gateway_targets import (
    ApimTargetArmInventory,
    GatewayDeclaredState,
    GatewayDeployment,
    GatewayDeploymentInventory,
    GatewayDeploymentKind,
    GatewayLifecycleOwnership,
    SelfHostedGatewayProvisionSpec,
    gateway_provision_digest,
    validate_gateway_provision_target,
)
from ..domain.model_availability import ModelAvailabilityPublication, ModelAvailabilityState
from ..integrations.apim_control_plane import (
    ApimPublisherClient,
    ApimPublisherClientResolver,
    PolicyCompilationError,
    ResolvedApimPublisherClient,
    RetryablePublicationError,
    coerce_apim_publisher_client_resolver,
)
from ..integrations.apim_control_plane_contract import (
    ProbeRequestLimitExceeded,
    SelfHostedGatewayManagementClient,
)
from ..integrations.apim_model_availability import compose_availability_parent
from ..integrations.apim_policy_components import parse_policy, validate_parent_readback
from ..integrations.apim_target_client_factory import route_target_identity_matches
from ..persistence.repository import QueryRepository
from ..security import CredentialCipher
from .application_access import ApplicationAccessService
from .control_plane import ControlPlaneConflictError, GatewayControlPlaneService
from .probe_journal import PersistentProbeJournal
from .route_publication import RouteIntegrityCheckpoint, RouteIntegrityDeferred
from .worker_lease import WorkerLease


class GatewayReleaseOperationWorker:
    _TERMINAL = {"succeeded", "failed", "restored"}

    def __init__(
        self,
        repository: QueryRepository,
        client: ApimPublisherClient | ApimPublisherClientResolver,
        retention_policy: GatewayReleaseRetentionPolicy | None = None,
        application_default_token_limit: int = 100_000,
        application_default_tokens_per_minute: int = 100_000,
        cipher: CredentialCipher | None = None,
        application_projector: Callable[[UUID, UUID], None] | None = None,
        parent_policy: str | None = None,
        parent_policy_overrides: Mapping[UUID, str] | None = None,
        application_rate_limit_projector: Callable[[UUID], int | None] | None = None,
        onboarding_handler: Callable[[Mapping[str, Any], str], None] | None = None,
        gateway_protocol_enrollment: (
            Callable[
                [ApimTargetArmInventory, ApimPublisherClient, Callable[[], None]], tuple[str, ...]
            ] | None
        ) = None,
        self_hosted_gateway_provisioning_enabled: bool = False,
        gateway_management_client_resolver: ApimPublisherClientResolver | None = None,
        availability_projector: Callable[[UUID], ModelAvailabilityState] | None = None,
        probe_request_limits: Mapping[UUID, int] | None = None,
        route_integrity_verifier: (
            Callable[[GatewayPublication, object], GatewayReleaseDependencies] | None
        ) = None,
    ) -> None:
        self._repository = repository
        self._parent_policy = parent_policy
        self._parent_policy_overrides = dict(parent_policy_overrides or {})
        self._client_resolver = coerce_apim_publisher_client_resolver(client)
        self._cipher = cipher
        self._application_projector = application_projector
        self._application_rate_limit_projector = application_rate_limit_projector
        self._onboarding_handler = onboarding_handler
        self._gateway_protocol_enrollment = gateway_protocol_enrollment
        self._self_hosted_gateway_provisioning_enabled = self_hosted_gateway_provisioning_enabled
        self._gateway_management_client_resolver = gateway_management_client_resolver
        self._availability_projector = availability_projector
        self._probe_request_limits = dict(probe_request_limits or {})
        self._route_integrity_verifier = route_integrity_verifier
        self._service = GatewayControlPlaneService(
            repository,
            retention_policy=retention_policy,
        )
        self._application_service = ApplicationAccessService(
            repository,
            sync_available=True,
            default_token_limit=application_default_token_limit,
            default_tokens_per_minute=application_default_tokens_per_minute,
        )

    def run_once(
        self, worker_id: str, lease_seconds: int = 180, max_attempts: int = 30
    ) -> GatewayReleaseOperation | None:
        worker_id = f"{worker_id}:{uuid4().hex}"
        row = self._repository.claim_gateway_release_operation(
            worker_id, lease_seconds
        )
        if row is None:
            return None
        operation_id = UUID(str(row["id"]))
        fixed_rollback = self._is_fixed_rollback(row)
        retry_base = int((row.get("checkpoint") or {}).get("retry_attempt_base", 0)) \
            if row["operation_kind"] == "self_hosted_gateway_provision" else 0
        if row["operation_kind"] == "integrity_check" or fixed_rollback:
            markers = ["route_integrity_attempt"]
            if fixed_rollback:
                markers.append("route_rollback_wait_attempt")
            for marker in markers:
                progress_attempt = (row.get("checkpoint") or {}).get(marker, 0)
                if (type(progress_attempt) is not int
                        or not 0 <= progress_attempt <= row["attempt_count"]):
                    self._fail_or_restore(
                        row, PolicyCompilationError("Fixed route operation attempt marker changed"),
                        worker_id,
                    )
                    return self._service.release_operation(operation_id)
                retry_base = max(retry_base, progress_attempt)
        if (
            int(row["attempt_count"]) - retry_base > max_attempts
            and row["status"] != "restoring"
        ):
            self._handle_attempt_exhaustion(row, worker_id)
            return self._service.release_operation(operation_id)
        try:
            with WorkerLease(lambda: self._repository.renew_gateway_release_operation_lease(
                operation_id, worker_id, lease_seconds
            ), lease_seconds):
                if fixed_rollback:
                    self._advance_configuration_rollback(row, worker_id)
                    return self._service.release_operation(operation_id)
                if row["operation_kind"] == "integrity_check" and any(
                    field in self._target(row).resource_manifest for field in (
                        "fixed_route_program", "route_configuration", "fixed_route_snapshot",
                    )
                ):
                    self._advance_integrity(row, worker_id, None)
                    return self._service.release_operation(operation_id)
                if row["operation_kind"] == GatewayReleaseOperationKind.APIM_ONBOARDING:
                    if self._onboarding_handler is None:
                        raise RuntimeError("APIM initialization is not enabled on this Worker")
                    self._onboarding_handler(row, worker_id)
                    return self._service.release_operation(operation_id)
                if (
                    row["operation_kind"] == "self_hosted_gateway_provision"
                    and not self._self_hosted_gateway_provisioning_enabled
                ):
                    raise PolicyCompilationError(
                        "Self-hosted Gateway creation is not enabled on this Worker"
                    )
                resolver = self._gateway_management_client_resolver \
                    if row["operation_kind"] == "self_hosted_gateway_provision" else None
                resolved = (resolver or self._client_resolver).resolve(
                    UUID(str(row["gateway_profile_id"])),
                    **({"include_probe_credentials": False}
                       if row["operation_kind"] == "self_hosted_gateway_provision" else {}),
                )
                self._validate_operation_target_identity(row, resolved)
                self._advance(row, worker_id, resolved.client, lease_seconds=lease_seconds)
        except (RetryablePublicationError, httpx.TransportError) as error:
            self._retry(row, error, worker_id)
        except Exception as error:
            self._fail_or_restore(row, error, worker_id)
        return self._service.release_operation(operation_id)

    def _validate_operation_target_identity(
        self,
        row: Mapping[str, Any],
        resolved: ResolvedApimPublisherClient,
    ) -> None:
        gateway_profile_id = UUID(str(row["gateway_profile_id"]))
        if resolved.gateway_profile_id != gateway_profile_id:
            raise ControlPlaneConflictError(
                "APIM client resolved another Release operation target"
            )
        for field in ("target_release_id", "prior_release_id"):
            value = row.get(field)
            if value is None:
                continue
            publication = self._publication(UUID(str(value)))
            self._require_operation_gateway(row, publication)
            snapshot = publication.resource_manifest.get(
                "apim_target_identity_sha256"
            )
            if snapshot is not None and snapshot != resolved.target_identity_sha256 and not (
                isinstance(snapshot, str) and route_target_identity_matches(resolved, snapshot)
            ):
                raise ControlPlaneConflictError(
                    "APIM target identity changed after the Release was created"
                )
            if snapshot is None and resolved.enforce_identity_snapshot:
                raise ControlPlaneConflictError(
                    "APIM target identity snapshot is missing from the Release"
                )

    def _advance(
        self,
        row: Mapping[str, Any],
        worker_id: str,
        client: ApimPublisherClient,
        *, lease_seconds: int = 180,
    ) -> None:
        kind = GatewayReleaseOperationKind(str(row["operation_kind"]))
        if kind is GatewayReleaseOperationKind.INTEGRITY_CHECK:
            self._advance_integrity(row, worker_id, client)
        elif kind is GatewayReleaseOperationKind.ROLLBACK:
            self._advance_rollback(row, worker_id, client)
        elif kind is GatewayReleaseOperationKind.GC_PLAN:
            self._advance_gc_plan(row, worker_id, client)
        elif kind is GatewayReleaseOperationKind.APPLICATION_SYNC:
            self._advance_application_sync(row, worker_id, client)
        elif kind is GatewayReleaseOperationKind.APPLICATION_PROVISION:
            self._advance_application_provision(row, worker_id, client)
        elif kind is GatewayReleaseOperationKind.TARGET_INVENTORY_SYNC:
            self._advance_target_inventory_sync(row, worker_id, client)
        elif kind is GatewayReleaseOperationKind.SELF_HOSTED_GATEWAY_PROVISION:
            self._advance_self_hosted_gateway_provision(row, worker_id, client, lease_seconds)

    def _advance_self_hosted_gateway_provision(
        self,
        row: Mapping[str, Any],
        worker_id: str,
        client: ApimPublisherClient,
        lease_seconds: int,
    ) -> None:
        if not self._self_hosted_gateway_provisioning_enabled:
            raise PolicyCompilationError(
                "Self-hosted Gateway creation is not enabled on this Worker"
            )
        spec = SelfHostedGatewayProvisionSpec.model_validate(row["semantic_preview"])
        if spec.target.gateway_profile_id != UUID(str(row["gateway_profile_id"])) or row[
            "confirmation_sha256"
        ] != gateway_provision_digest(spec):
            raise ControlPlaneConflictError("Gateway creation intent changed")
        target = self._service._active_apim_target_row(spec.target.gateway_profile_id)
        validate_gateway_provision_target(spec, target)
        if row["status"] == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return

        def before_write() -> None:
            if not self._repository.renew_gateway_release_operation_lease(
                UUID(str(row["id"])),
                worker_id,
                lease_seconds,
            ):
                raise RetryablePublicationError("Worker lease is no longer owned")
            validate_gateway_provision_target(
                spec,
                self._service._active_apim_target_row(spec.target.gateway_profile_id),
            )
            existing = self._repository.get_gateway_deployment(spec.deployment_id)
            if existing and existing.get("retired_at") is not None:
                raise ControlPlaneConflictError("Removed Gateway creation cannot be replayed")

        if not isinstance(client, SelfHostedGatewayManagementClient):
            raise PolicyCompilationError("APIM client does not support Gateway creation")
        if row["status"] == "validating_dependencies":
            client.ensure_self_hosted_gateway(spec, before_write)
            self._transition(
                row,
                "verifying_readback",
                {
                    "checkpoint": self._checkpoint(
                        row,
                        native_gateway_id=spec.native_gateway_id,
                        deployment_id=str(spec.deployment_id),
                        apim_gateway_created=True,
                        api_associated=True,
                    )
                },
                worker_id,
            )
            return
        if row["status"] != "verifying_readback":
            raise PolicyCompilationError("Gateway creation state is invalid")
        client.ensure_self_hosted_gateway(spec, before_write)
        inventory = client.discover_apim_target_inventory(spec.target.gateway_profile_id)
        if (
            inventory.gateway_profile_id != spec.target.gateway_profile_id
            or inventory.arm_resource_id.casefold() != spec.target.arm_resource_id.casefold()
            or inventory.api_id != spec.target.api_id
            or inventory.api_path != spec.target.api_path
        ):
            raise ControlPlaneConflictError("Gateway creation inventory returned another target")
        gateway = next(
            (
                item
                for item in inventory.gateways
                if item.native_gateway_id == spec.native_gateway_id
            ),
            None,
        )
        if gateway is None or not any(
            value.casefold() == spec.target.api_id.casefold()
            for value in gateway.associated_api_ids
        ):
            raise RetryablePublicationError("Created Gateway API association is not visible yet")
        if (
            gateway.arm_resource_id.casefold()
            != (spec.target.arm_resource_id + "/gateways/" + spec.native_gateway_id).casefold()
            or gateway.description
            != f"Turnstile gateway {spec.target.gateway_profile_id}/{spec.request_id}"
        ):
            raise ControlPlaneConflictError("Created Gateway ownership changed before activation")
        before_write()
        deployment = GatewayDeployment(
            id=spec.deployment_id,
            apim_target_id=spec.target.gateway_profile_id,
            native_gateway_id=spec.native_gateway_id,
            kind=GatewayDeploymentKind.SELF_HOSTED,
            display_name=spec.configuration.display_name,
            endpoint_url=spec.configuration.endpoint_url,
            location=spec.configuration.location,
            hosting_mode=spec.configuration.hosting_mode,
            lifecycle_ownership=GatewayLifecycleOwnership.CUSTOMER,
            release_gate=spec.configuration.release_gate,
            declared_state=GatewayDeclaredState.PRESENT,
            last_arm_sync_at=inventory.discovered_at,
        )
        stored_inventory = GatewayDeploymentInventory(
            deployment_id=spec.deployment_id,
            associated_api_ids=gateway.associated_api_ids,
            hostname_configurations=gateway.hostname_configurations,
            certificate_authorities=gateway.certificate_authorities,
            source_api_version=inventory.source_api_version,
            synced_at=inventory.discovered_at,
        )
        self._repository.finish_self_hosted_gateway_provision(
            UUID(str(row["id"])),
            worker_id,
            spec,
            deployment,
            stored_inventory,
            self._checkpoint(
                row,
                deployment_id=str(spec.deployment_id),
                native_gateway_id=spec.native_gateway_id,
                configuration_ready=True,
                waiting_for_container=True,
                gateway_token_stored=False,
            ),
            enqueue_inventory_sync=self._gateway_protocol_enrollment is not None,
        )

    def _advance_integrity(
        self,
        row: Mapping[str, Any],
        worker_id: str,
        client: ApimPublisherClient | None,
    ) -> None:
        status = str(row["status"])
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if status != "validating_dependencies":
            return
        target = self._target(row)
        if any(field in target.resource_manifest for field in (
            "fixed_route_program", "route_configuration", "fixed_route_snapshot",
        )):
            dependencies = self._fixed_integrity_slice(row, target, worker_id)
            if dependencies is None:
                return
        else:
            if client is None:
                raise PolicyCompilationError("Legacy release integrity client is unavailable")
            dependencies = client.inspect_revision_dependencies(target)
        self._repository.save_gateway_release_integrity_snapshot(
            target.id,
            UUID(str(row["id"])),
            dependencies.live_status,
            dependencies.model_dump(mode="json"),
            dependencies.issues,
            worker_id,
        )
        self._transition(
            row,
            "succeeded",
            {
                "checkpoint": self._checkpoint(
                    row, integrity_status=dependencies.live_status,
                    issue_count=len(dependencies.issues),
                ),
                "error_code": None, "error_message": None,
            },
            worker_id,
        )

    def _fixed_integrity_slice(
        self, row: Mapping[str, Any], target: GatewayPublication, worker_id: str,
    ) -> GatewayReleaseDependencies | None:
        if self._route_integrity_verifier is None:
            raise PolicyCompilationError("Fixed route integrity verifier is unavailable")
        checkpoint = (row.get("checkpoint") or {}).get("route_integrity")
        try:
            return self._route_integrity_verifier(target, checkpoint)
        except RouteIntegrityDeferred as deferred:
            progress = RouteIntegrityCheckpoint.model_validate(deferred.checkpoint)
            previous = (
                RouteIntegrityCheckpoint.model_validate(checkpoint) if checkpoint is not None
                else RouteIntegrityCheckpoint(context_sha256=progress.context_sha256)
            )
            fields = ("entities", "connections", "credentials")
            if (progress.context_sha256 != previous.context_sha256 or any(
                getattr(progress, field) < getattr(previous, field) for field in fields
            ) or progress.connections == previous.connections
                and progress.connection_resources < previous.connection_resources):
                raise PolicyCompilationError("Fixed route integrity progress regressed") from None
            advanced = any(
                getattr(progress, field) > getattr(previous, field) for field in fields
            ) or progress.connection_resources > previous.connection_resources
            self._transition(row, str(row["status"]), {
                "checkpoint": self._checkpoint(
                    row, route_integrity=progress.model_dump(mode="json"),
                    **({"route_integrity_attempt": int(row["attempt_count"])} if advanced else {}),
                ),
                "error_code": None, "error_message": None,
            }, worker_id)
            return None

    def _advance_application_sync(
        self,
        row: Mapping[str, Any],
        worker_id: str,
        client: ApimPublisherClient,
    ) -> None:
        status = str(row["status"])
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if status != "validating_dependencies":
            return
        gateway_profile_id = UUID(str(row["gateway_profile_id"]))
        discovery = client.discover_gateway_applications(gateway_profile_id)
        synchronized = self._application_service.sync_discovery(discovery, worker_id)
        if self._application_rate_limit_projector is None:
            raise RetryablePublicationError("Application TPM projection is not configured")
        for application in synchronized:
            try:
                rate = self._application_rate_limit_projector(UUID(str(application["id"])))
            except Exception as error:
                raise RetryablePublicationError("Application TPM projection is pending") from error
            if rate is None:
                raise RetryablePublicationError("Application TPM configuration is missing")
        self._transition(
            row,
            "succeeded",
            {
                "checkpoint": {
                    "application_count": len(synchronized),
                    "system_application_count": sum(
                        item.system_managed for item in discovery.items
                    ),
                    "stale_subscription_count": sum(
                        not item.scope_exists for item in discovery.items
                    ),
                    "read_only_apim_discovery": True,
                }
            },
            worker_id,
        )

    def _advance_application_provision(
        self,
        row: Mapping[str, Any],
        worker_id: str,
        client: ApimPublisherClient,
    ) -> None:
        status = str(row["status"])
        operation_id = UUID(str(row["id"]))
        spec = GatewayApplicationSubscriptionProvisionSpec.model_validate(
            row["semantic_preview"]
        )
        if spec.provisioning_version == 2 and spec.gateway_profile_id != UUID(
            str(row["gateway_profile_id"])
        ):
            raise ControlPlaneConflictError("Application creation references another gateway")
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if status == "validating_dependencies":
            if spec.provisioning_version == 2 and self._application_projector is None:
                raise RuntimeError("Application admission ledger projection is not configured")
            if self._cipher is None:
                raise RuntimeError("Credential encryption is unavailable")
            ciphertext = self._repository.gateway_release_operation_secret(
                operation_id
            )
            secret = self._cipher.decrypt(ciphertext)
            if secret is None:
                raise RuntimeError("Application provisioning credential is missing")
            values = json.loads(secret)
            primary_key = values.get("primary_key")
            secondary_key = values.get("secondary_key")
            if not isinstance(primary_key, str) or not isinstance(
                secondary_key, str
            ):
                raise RuntimeError("Application provisioning credential is invalid")
            client.ensure_application_subscription(
                spec, primary_key, secondary_key
            )
            self._transition(
                row,
                "promoting",
                {
                    "checkpoint": self._checkpoint(
                        row, apim_subscription_created=True
                    )
                },
                worker_id,
            )
            return
        if status == "verifying_readback" and spec.provisioning_version == 2:
            assert self._application_projector is not None
            try:
                self._application_projector(
                    UUID(str(row["gateway_profile_id"])), spec.application_id
                )
            except (ControlPlaneConflictError, PolicyCompilationError):
                raise
            except Exception as error:
                raise RetryablePublicationError(
                    "Application admission ledger is not yet ready"
                ) from error
            if self._cipher is None:
                raise RuntimeError("Credential encryption is unavailable")
            secret = self._cipher.decrypt(
                self._repository.gateway_release_operation_secret(operation_id)
            )
            if secret is None:
                raise RuntimeError("Application provisioning credential is missing")
            keys = json.loads(secret)
            client.activate_application_subscription(
                spec, keys["primary_key"], keys["secondary_key"]
            )
            self._transition(row, "succeeded", {
                "checkpoint": self._checkpoint(
                    row, admission_ready=True, subscription_active=True,
                    data_plane_authentication_ready=True, key_stored=False,
                ),
            }, worker_id)
            return
        if status != "promoting":
            return
        try:
            application = self._application_service.provision(
                UUID(str(row["gateway_profile_id"])),
                spec,
                str(row["created_by"]),
            )
        except ValueError:
            raise
        except Exception as error:
            raise RetryablePublicationError(
                "Application materialization is temporarily unavailable"
            ) from error
        if spec.provisioning_version == 2:
            self._transition(row, "verifying_readback", {
                "checkpoint": self._checkpoint(row, application_id=str(application["id"]),
                    apim_subscription_id=spec.apim_subscription_id, product_id=spec.scope_id,
                    application_materialized=True),
            }, worker_id)
            return
        self._transition(
            row,
            "succeeded",
            {
                "checkpoint": self._checkpoint(
                    row,
                    application_id=str(application["id"]),
                    apim_subscription_id=spec.apim_subscription_id,
                    product_id=spec.scope_id,
                    key_stored=False,
                )
            },
            worker_id,
        )

    def _advance_target_inventory_sync(
        self,
        row: Mapping[str, Any],
        worker_id: str,
        client: ApimPublisherClient,
    ) -> None:
        status = str(row["status"])
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if status != "validating_dependencies":
            return
        gateway_profile_id = UUID(str(row["gateway_profile_id"]))
        inventory = client.discover_apim_target_inventory(gateway_profile_id)
        topology = self._service.apply_target_inventory(inventory)

        def before_write() -> None:
            if not self._repository.renew_gateway_release_operation_lease(
                UUID(str(row["id"])), worker_id, 180,
            ):
                raise RetryablePublicationError("Worker lease is no longer owned")

        if self._gateway_protocol_enrollment is not None:
            before_write()
        enrolled = (
            self._gateway_protocol_enrollment(inventory, client, before_write)
            if self._gateway_protocol_enrollment is not None else None
        )
        self._transition(
            row,
            "succeeded",
            {
                "checkpoint": {
                    "read_only_arm_discovery": True,
                    "service_tier": inventory.service_tier,
                    "gateway_count": len(topology.deployments),
                    "self_hosted_gateway_count": len(inventory.gateways),
                    "associated_api_count": sum(
                        len(gateway.associated_api_ids)
                        for gateway in inventory.gateways
                    ),
                    "hostname_configuration_count": sum(
                        len(gateway.hostname_configurations)
                        for gateway in inventory.gateways
                    ),
                    "certificate_authority_count": sum(
                        len(gateway.certificate_authorities)
                        for gateway in inventory.gateways
                    ),
                          **({"protocol_enrolled_gateways": list(enrolled)}
                              if enrolled is not None else {}),
                }
            },
            worker_id,
        )

    def _is_fixed_rollback(self, row: Mapping[str, Any]) -> bool:
        if row["operation_kind"] != "rollback":
            return False
        if (row.get("checkpoint") or {}).get("route_rollback_publication_id") is not None:
            return True
        for field in ("target_release_id", "prior_release_id"):
            if row.get(field) is None:
                continue
            release = self._repository.get_gateway_publication(UUID(str(row[field])))
            if release is not None and any(name in release["resource_manifest"] for name in (
                "fixed_route_program", "route_configuration", "fixed_route_snapshot",
                "model_route_addition", "model_route_removal",
            )):
                return True
        return False

    def _advance_configuration_rollback(self, row: Mapping[str, Any], worker_id: str) -> None:
        target, prior = self._target(row), self._prior(row)
        if any(marker in target.resource_manifest for marker in (
            "model_route_addition", "model_route_removal",
        )):
            raise PolicyCompilationError(
                "Fixed route rollback cannot target a model-scoped publication",
            )
        installation = target.resource_manifest.get("fixed_route_program")
        if (
            self._route_integrity_verifier is None or not isinstance(installation, dict)
            or not installation
            or installation != prior.resource_manifest.get("fixed_route_program")
        ):
            raise PolicyCompilationError(
                "Fixed route rollback requires matching programs and its configuration verifier",
            )
        status = str(row["status"])
        linked = (row.get("checkpoint") or {}).get("route_rollback_publication_id")
        if linked is not None:
            publication = GatewayPublication.model_validate(
                self._repository.enqueue_gateway_configuration_rollback(
                    UUID(str(row["id"])), worker_id,
                )
            )
            checkpoint = self._checkpoint(
                row, route_rollback_publication_id=str(publication.id),
                route_rollback_wait_attempt=int(row["attempt_count"]),
                configuration_status=publication.status.value,
            )
            if publication.status.value == "active":
                self._repository.complete_gateway_configuration_rollback(
                    UUID(str(row["id"])), worker_id,
                )
            elif publication.status.value in {"failed", "rolled_back", "superseded"}:
                self._transition(row, "failed", {
                    "checkpoint": checkpoint, "error_code": "configuration_publication_failed",
                    "error_message": "Configuration rollback publication did not become active",
                }, worker_id)
            else:
                self._transition(row, "verifying_readback", {
                    "checkpoint": checkpoint, "error_code": None, "error_message": None,
                }, worker_id)
            return
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if status != "validating_dependencies":
            raise PolicyCompilationError(
                "Fixed route rollback requires a configuration publication",
            )
        dependencies = self._fixed_integrity_slice(row, target, worker_id)
        if dependencies is None:
            return
        if not dependencies.recorded_complete or dependencies.live_status != "healthy":
            raise PolicyCompilationError(
                "Fixed route rollback configuration integrity is not healthy",
            )
        self._repository.save_gateway_release_integrity_snapshot(
            target.id, UUID(str(row["id"])), dependencies.live_status,
            dependencies.model_dump(mode="json"), dependencies.issues, worker_id,
        )
        preview = self._service.rollback_preview(target.id)
        if (preview.current_release_id != prior.id
                or preview.confirmation_sha256 != row.get("confirmation_sha256")
                or not preview.rollback_eligible):
            raise ControlPlaneConflictError("Fixed route rollback confirmation or base changed")
        queued_publication = self._repository.enqueue_gateway_configuration_rollback(
            UUID(str(row["id"])), worker_id,
        )
        self._transition(row, "verifying_readback", {
            "checkpoint": self._checkpoint(
                row, route_rollback_publication_id=str(queued_publication["id"]),
                route_rollback_wait_attempt=int(row["attempt_count"]),
            ),
            "error_code": None, "error_message": None,
        }, worker_id)

    def _advance_rollback(
        self,
        row: Mapping[str, Any],
        worker_id: str,
        client: ApimPublisherClient,
    ) -> None:
        status = str(row["status"])
        target = self._target(row)
        prior = self._prior(row)
        if any(field in publication.resource_manifest for publication in (target, prior)
               for field in ("fixed_route_program", "route_configuration", "fixed_route_snapshot",
                             "model_route_addition", "model_route_removal")):
            self._transition(row, "failed", {
                "error_code": "PolicyCompilationError",
                "error_message": "Fixed route rollback requires a configuration publication",
            }, worker_id)
            return
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if status == "validating_dependencies":
            dependencies = client.inspect_revision_dependencies(target)
            self._repository.save_gateway_release_integrity_snapshot(
                target.id,
                UUID(str(row["id"])),
                dependencies.live_status,
                dependencies.model_dump(mode="json"),
                dependencies.issues,
                worker_id,
            )
            preview = self._service.rollback_preview(target.id)
            if preview.current_release_id != prior.id:
                raise ControlPlaneConflictError(
                    "Effective gateway release changed before rollback"
                )
            if row.get("confirmation_sha256") != preview.confirmation_sha256:
                raise ControlPlaneConflictError(
                    "Rollback confirmation no longer matches the live preview"
                )
            if not preview.rollback_eligible:
                raise ControlPlaneConflictError(
                    f"Rollback dependencies are not healthy: {', '.join(preview.rollback_blockers)}"
                )
            self._transition(
                row,
                "preflight_probing",
                {
                    "checkpoint": {
                        "dependency_validation": "healthy",
                        "target_revision": target.apim_revision,
                        "prior_revision": prior.apim_revision,
                    }
                },
                worker_id,
            )
            return
        if status == "preflight_probing":
            target_revision = self._revision(target)
            target = self._validate_rollback_parent(target, client)
            client.probe_revision(
                target_revision, target, journal=self._probe_journal(row, worker_id)
            )
            self._transition(
                row,
                "promoting",
                {"checkpoint": self._checkpoint(row, preflight_probe="passed")},
                worker_id,
            )
            return
        if status == "promoting":
            target_revision = self._revision(target)
            target = self._validate_rollback_parent(target, client)
            prior_revision = self._revision(prior)
            observed = client.current_revision()
            if observed != prior_revision:
                raise ControlPlaneConflictError(
                    "Current APIM revision changed before rollback promotion"
                )
            client.promote_revision(
                target_revision, f"rollback-{row['id']}"
            )
            self._transition(
                row,
                "verifying_readback",
                {"checkpoint": self._checkpoint(row, promotion="requested")},
                worker_id,
            )
            return
        if status == "verifying_readback":
            target_revision = self._revision(target)
            if client.current_revision() != target_revision:
                raise RetryablePublicationError(
                    "APIM did not report the rollback target as current"
                )
            self._transition(
                row,
                "post_promotion_probing",
                {"checkpoint": self._checkpoint(row, readback="passed")},
                worker_id,
            )
            return
        if status == "post_promotion_probing":
            target_revision = self._revision(target)
            target = self._validate_rollback_parent(target, client)
            client.probe_revision(
                target_revision, target, journal=self._probe_journal(row, worker_id)
            )
            self._repository.complete_gateway_release_rollback(
                UUID(str(row["id"])), target.id, prior.id, worker_id
            )
            return
        if status == "restoring":
            self._restore(row, prior, worker_id, client)

    def _validate_rollback_parent(
        self, publication: GatewayPublication, client: ApimPublisherClient,
    ) -> GatewayPublication:
        canonical = self._parent_policy_overrides.get(
            publication.gateway_profile_id, self._parent_policy,
        )
        if canonical is None:
            raise PolicyCompilationError("Rollback requires the canonical parent policy contract")
        observed = client.revision_api_policy(self._revision(publication))
        availability = publication.resource_manifest.get("model_availability")
        if self._repository.model_availability_supported() or availability is not None:
            if availability is None or parse_policy(observed).find(
                "inbound/set-variable[@name='availabilityProtocolVersion']",
            ) is None:
                raise PolicyCompilationError(
                    "Rollback cannot remove model availability enforcement"
                )
            snapshot = ModelAvailabilityPublication.model_validate(availability)
            snapshot.validate_publication(publication)
            if self._availability_projector is None:
                raise PolicyCompilationError("Rollback availability projection is unavailable")
            probe_states = {}
            for alias, model_id in snapshot.models.items():
                projected = self._availability_projector(model_id)
                if projected.model_id != model_id:
                    raise PolicyCompilationError("Rollback model availability identity changed")
                probe_states[alias] = projected.enabled
            publication = publication.model_copy(update={"resource_manifest": {
                **publication.resource_manifest, "availability_probe_states": probe_states,
            }})
            canonical = compose_availability_parent(canonical)
        profiles = [binding.model.image_profile for binding in publication.desired_spec.bindings
                    if binding.model.image_profile is not None]
        validate_parent_readback(
            observed, canonical, profiles,
            expected_contract=publication.resource_manifest.get("parent_policy_contract_sha256"),
            expected_raw=publication.resource_manifest.get("parent_policy_sha256"),
        )
        return publication

    def _probe_journal(self, row: Mapping[str, Any], worker_id: str) -> PersistentProbeJournal:
        operation_id = UUID(str(row["id"]))

        def heartbeat() -> None:
            if not self._repository.renew_gateway_release_operation_lease(
                operation_id, worker_id, 180
            ):
                raise RetryablePublicationError("Worker lease is no longer owned")

        def record_request(method: str, endpoint: str) -> None:
            limit = self._probe_request_limits.get(UUID(str(row["gateway_profile_id"])))
            if limit is not None and not self._repository.reserve_gateway_release_operation_probe(
                operation_id, worker_id, limit, method,
                hashlib.sha256(endpoint.encode()).hexdigest(),
            ):
                raise ProbeRequestLimitExceeded(
                    "Release probe request allowance is exhausted or its lease is no longer valid"
                )

        return PersistentProbeJournal(
            self._repository, f"release-operation:{operation_id}:{row['status']}",
            operation_id, heartbeat=heartbeat, request_recorder=record_request,
        )

    def _advance_gc_plan(
        self,
        row: Mapping[str, Any],
        worker_id: str,
        client: ApimPublisherClient,
    ) -> None:
        status = str(row["status"])
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if status != "validating_dependencies":
            return
        operation_id = UUID(str(row["id"]))
        existing = self._repository.get_gateway_release_gc_plan(operation_id)
        if existing is None:
            gateway_id = UUID(str(row["gateway_profile_id"]))
            releases = [
                GatewayPublication.model_validate(value)
                for value in self._repository.all_gateway_publications(gateway_id)
            ]
            retained_ids = self._service.retained_release_ids(gateway_id)
            from .route_publication import recorded_route_dependencies

            fixed = {
                str(release.id): recorded_route_dependencies(release)
                for release in releases if any(field in release.resource_manifest for field in (
                    "fixed_route_program", "route_configuration", "fixed_route_snapshot",
                )) or (release.status != "failed" and any(
                    field in release.resource_manifest
                    for field in ("route_preparation", "route_dependencies", "route_rollback")
                ))
            }
            evidence = client.plan_release_garbage_collection(
                releases, {str(value) for value in retained_ids}, fixed_dependencies=fixed,
            )
            self._repository.save_gateway_release_gc_plan(
                operation_id,
                gateway_id,
                sorted(retained_ids, key=str),
                evidence.current_non_release_references,
                evidence.candidates,
                evidence.reference_graph_sha256,
                worker_id,
            )
        self._transition(
            row,
            "succeeded",
            {"checkpoint": {"dry_run_only": True}},
            worker_id,
        )

    def _restore(
        self,
        row: Mapping[str, Any],
        prior: GatewayPublication,
        worker_id: str,
        client: ApimPublisherClient,
    ) -> None:
        prior_revision = self._revision(prior)
        prior = self._validate_rollback_parent(prior, client)
        if client.current_revision() != prior_revision:
            client.promote_revision(
                prior_revision, f"restore-{row['id']}"
            )
        if client.current_revision() != prior_revision:
            raise RetryablePublicationError(
                "APIM did not report the restored revision as current"
            )
        client.probe_revision(
            prior_revision, prior, journal=self._probe_journal(row, worker_id),
        )
        self._transition(
            row,
            "restored",
            {
                "checkpoint": self._checkpoint(
                    row, restoration="passed", effective_pointer="unchanged"
                )
            },
            worker_id,
        )

    def _fail_or_restore(
        self, row: Mapping[str, Any], error: Exception, worker_id: str
    ) -> None:
        status = str(row["status"])
        updates = self._error_updates(row, error)
        if self._is_fixed_rollback(row) or row["operation_kind"] in {
            GatewayReleaseOperationKind.APPLICATION_PROVISION,
            GatewayReleaseOperationKind.APIM_ONBOARDING,
            GatewayReleaseOperationKind.SELF_HOSTED_GATEWAY_PROVISION,
        }:
            self._transition(row, "failed", updates, worker_id)
            return
        if status in {
            "promoting",
            "verifying_readback",
            "post_promotion_probing",
        } or status == "restoring":
            self._transition(row, "restoring", updates, worker_id)
        else:
            self._transition(row, "failed", updates, worker_id)

    def _retry(
        self, row: Mapping[str, Any], error: Exception, worker_id: str
    ) -> None:
        self._transition(row, str(row["status"]), self._error_updates(row, error), worker_id)

    def _handle_attempt_exhaustion(
        self, row: Mapping[str, Any], worker_id: str
    ) -> None:
        status = str(row["status"])
        if self._is_fixed_rollback(row) or row["operation_kind"] in {
            GatewayReleaseOperationKind.APPLICATION_PROVISION,
            GatewayReleaseOperationKind.APIM_ONBOARDING,
            GatewayReleaseOperationKind.SELF_HOSTED_GATEWAY_PROVISION,
        }:
            next_status = "failed"
        elif status in {
            "promoting",
            "verifying_readback",
            "post_promotion_probing",
        }:
            next_status = "restoring"
        else:
            next_status = "failed"
        self._transition(
            row,
            next_status,
            {
                "error_code": "max_attempts_exceeded",
                "error_message": "Gateway release operation exceeded its retry limit",
                "checkpoint": self._checkpoint(row, max_attempts_exceeded=True),
            },
            worker_id,
        )

    def _transition(
        self,
        row: Mapping[str, Any],
        status: str,
        updates: Mapping[str, Any],
        worker_id: str,
    ) -> None:
        if (row["operation_kind"] in {GatewayReleaseOperationKind.APPLICATION_PROVISION,
                         GatewayReleaseOperationKind.SELF_HOSTED_GATEWAY_PROVISION}
            and status != str(row["status"])):
            updates = {"error_code": None, "error_message": None, **updates}
        transitioned = self._repository.transition_gateway_release_operation(
            UUID(str(row["id"])),
            str(row["status"]),
            status,
            updates,
            worker_id,
        )
        if transitioned is None:
            raise RuntimeError("Gateway release operation changed while leased")

    def _target(self, row: Mapping[str, Any]) -> GatewayPublication:
        if row.get("target_release_id") is None:
            raise ControlPlaneConflictError("Release operation has no target release")
        publication = self._publication(UUID(str(row["target_release_id"])))
        self._require_operation_gateway(row, publication)
        return publication

    def _prior(self, row: Mapping[str, Any]) -> GatewayPublication:
        if row.get("prior_release_id") is None:
            raise ControlPlaneConflictError("Rollback operation has no prior release")
        publication = self._publication(UUID(str(row["prior_release_id"])))
        self._require_operation_gateway(row, publication)
        return publication

    def _publication(self, publication_id: UUID) -> GatewayPublication:
        row = self._repository.get_gateway_publication(publication_id)
        if row is None:
            raise ControlPlaneConflictError("Gateway release no longer exists")
        return GatewayPublication.model_validate(row)

    @staticmethod
    def _require_operation_gateway(
        row: Mapping[str, Any], publication: GatewayPublication
    ) -> None:
        if publication.gateway_profile_id != UUID(str(row["gateway_profile_id"])):
            raise ControlPlaneConflictError(
                "Gateway release operation references another gateway"
            )

    @staticmethod
    def _revision(publication: GatewayPublication) -> str:
        if not publication.apim_revision:
            raise ControlPlaneConflictError("Gateway release has no APIM revision")
        return publication.apim_revision

    @staticmethod
    def _checkpoint(row: Mapping[str, Any], **updates: object) -> dict[str, object]:
        current = row.get("checkpoint")
        return {**(dict(current) if isinstance(current, Mapping) else {}), **updates}

    def _error_updates(
        self, row: Mapping[str, Any], error: Exception
    ) -> dict[str, object]:
        if row["operation_kind"] == "rollback":
            latest = self._repository.get_gateway_release_operation(UUID(str(row["id"])))
            if latest is not None:
                row = latest
        elif row["operation_kind"] == "apim_onboarding":
            latest = self._repository.get_gateway_release_operation(UUID(str(row["id"])))
            if latest is not None and all(
                latest.get(key) == row.get(key) for key in (
                    "id", "gateway_profile_id", "operation_kind", "status", "lease_owner",
                    "confirmation_sha256",
                )
            ):
                row = latest
        if row["operation_kind"] == "self_hosted_gateway_provision":
            return {
                "error_code": type(error).__name__,
                "error_message": "Gateway creation readback is pending"
                if isinstance(error, (RetryablePublicationError, httpx.TransportError))
                    else ("Gateway creation could not be verified; "
                        "check target permissions and ownership"),
                "checkpoint": self._checkpoint(row, last_failed_status=row["status"]),
            }
        return {
            "error_code": type(error).__name__,
            "error_message": str(error).replace("\n", " ")[:1000],
            "checkpoint": self._checkpoint(row, last_failed_status=row["status"]),
        }