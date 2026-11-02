from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import time
from typing import Any, cast
from uuid import UUID, uuid5

import httpx
from pydantic import HttpUrl, SecretStr, ValidationError

from ..config import Settings
from ..domain.control_plane import (
    ApimOnboardingOperations,
    GatewayPublication,
    GatewayReleaseOperationAccepted,
    GatewayReleaseSpec,
)
from ..domain.gateway_targets import (
    ApimOnboardingConfiguration,
    ApimOnboardingPlan,
    ApimOnboardingRequest,
    ApimOnboardingStart,
    ApimTarget,
    ApimTargetCapabilitySnapshot,
    ApimTargetDriftState,
    ApimTargetOwnershipMode,
    ApimTierFamily,
    CapabilitySupport,
    GatewayDeclaredState,
    GatewayDeployment,
    GatewayDeploymentInventory,
    GatewayDeploymentKind,
    GatewayHostingMode,
    GatewayLifecycleOwnership,
    GatewayObservationState,
    GatewayReachabilityState,
    gateway_deployment_id_for,
    self_hosted_gateway_capability_for_tier,
)
from ..domain.ledger import LedgerScope
from ..domain.storage_routes import (
    ActiveRouteHead,
    ConfigurationApproval,
    FixedProgramInstallation,
    RouteSnapshot,
    canonical_document,
    catalog_digest,
    configuration_manifests,
    document_digest,
)
from ..integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.apim_onboarding_client import (
    AzureApimOnboardingClient,
    AzureApimPlatformConfigurationClient,
    onboarding_plan_digest,
)
from ..integrations.apim_onboarding_policy import (
    onboarding_policy_configuration_digest,
    render_onboarding_fixed_program,
)
from ..integrations.apim_storage_routes import FixedProgramVerifier, FixedRouteProgram
from ..integrations.apim_target_client_factory import (
    AzureApimTargetProbeCredentialResolver,
    RepositoryApimTargetClientConfigResolver,
)
from ..integrations.atomic_ledger import AtomicTableLedger
from ..integrations.reconciliation import ManagedIdentityTokenProvider
from ..integrations.storage_route_projection import RouteActivationIntent, TableRouteProjection
from ..persistence.repository import QueryRepository
from ..security import CredentialCipher, credential_hint
from .apim_onboarding_ledger import project_onboarding_ledger
from .budget_settlement import BudgetScopeRepository, BudgetScopeService
from .control_plane import (
    ControlPlaneConflictError,
    ControlPlaneUnavailableError,
    GatewayControlPlaneService,
)

OnboardingProjector = Callable[[ApimOnboardingPlan, UUID, str], None]
OnboardingFixedInitializer = Callable[
    [ApimOnboardingPlan, FixedRouteProgram, UUID, str], GatewayPublication,
]


def onboarding_fixed_publication(
    plan: ApimOnboardingPlan, program: FixedRouteProgram, operation_id: UUID,
    installed: FixedProgramInstallation, snapshot: RouteSnapshot, head: ActiveRouteHead,
    actor: str,
) -> GatewayPublication:
    moment = datetime.now(UTC)
    specification = GatewayReleaseSpec(
        gateway_profile_id=plan.gateway_profile_id, bindings=[], discovery_models=[],
    )
    publication = GatewayPublication.model_validate({
        "id": uuid5(operation_id, "fixed-program-installation"),
        "gateway_profile_id": plan.gateway_profile_id,
        "generation": 1, "publication_kind": "route_reconcile", "desired_spec": specification,
        "desired_spec_sha256": document_digest(specification), "status": "active",
        "base_release_id": None, "apim_revision": program.binding.api_revision,
        "policy_sha256": program.binding.program_sha256,
        "resource_manifest": {
            "fixed_route_program": installed.model_dump(mode="json"),
            "fixed_route_snapshot": snapshot.model_dump(mode="json"),
            "fixed_route_head": head.model_dump(mode="json"),
            "onboarding_operation_id": str(operation_id),
            "initialization_program_artifact_sha256": program.artifact_sha256,
        },
        "error_code": None, "error_message": None, "attempt_count": 0, "created_by": actor,
        "created_at": moment, "started_at": moment, "completed_at": moment, "updated_at": moment,
    })
    publication.require_onboarding_installation(
        plan.gateway_profile_id, operation_id, plan.fixed_program_sha256,
    )
    return publication


class _OnboardingActivationJournal:
    def __init__(
        self, state: dict[str, Any], save: Callable[[dict[str, Any]], None],
    ) -> None:
        self.state = state
        self._save = save

    def begin_once(self, intent: RouteActivationIntent) -> None:
        if self.state.get("activation") is not None:
            raise PolicyCompilationError("APIM initialization activation is recovery-only")
        self.state = {
            **self.state, "activation": {"intent": intent.to_record(), "outcome": "started"},
        }
        self._save(self.state)

    def record_outcome(self, operation_id: str, outcome: str) -> None:
        activation = self.state.get("activation")
        if not isinstance(activation, dict):
            raise PolicyCompilationError("APIM initialization activation intent is missing")
        intent = RouteActivationIntent.from_record(activation["intent"])
        if intent.receipt.operation_id != operation_id:
            raise PolicyCompilationError("APIM initialization activation operation changed")
        self.state = {**self.state, "activation": {**activation, "outcome": outcome}}
        self._save(self.state)


@contextmanager
def onboarding_client(request: ApimOnboardingRequest) -> Iterator[AzureApimOnboardingClient]:
    with httpx.Client(timeout=30.0, follow_redirects=False) as http:
        yield AzureApimOnboardingClient(request, client=http)


class ApimOnboardingService:
    def __init__(
        self,
        repository: QueryRepository,
        *,
        enabled: bool,
        configuration: ApimOnboardingConfiguration | None,
        canonical_parent: str | None,
        cipher: CredentialCipher | None,
        budget_ready: Callable[[], bool],
        client_factory: Callable[
            [ApimOnboardingRequest], AbstractContextManager[AzureApimOnboardingClient]
        ] = onboarding_client,
        projector: OnboardingProjector | None = None,
        fixed_initializer: OnboardingFixedInitializer | None = None,
        monthly_token_limit: int = 100_000,
        tokens_per_minute: int = 100_000,
        configuration_provider: Callable[[], ApimOnboardingConfiguration] | None = None,
    ) -> None:
        self._repository = repository
        self._configuration = configuration
        self._configuration_provider = configuration_provider
        self._canonical_parent = canonical_parent
        self._cipher = cipher
        self._client_factory = client_factory
        self._projector = projector
        self._fixed_initializer = fixed_initializer
        self._budget_ready = budget_ready
        self._monthly_token_limit = monthly_token_limit
        self._tokens_per_minute = tokens_per_minute
        self._available = (
            enabled
            and (configuration is not None or configuration_provider is not None)
            and canonical_parent is not None
            and cipher is not None
        )
        self._operations = GatewayControlPlaneService(repository, release_worker_enabled=enabled)

    def operations(self) -> ApimOnboardingOperations:
        return ApimOnboardingOperations(
            available=self._available,
            unavailable_reason=None
            if self._available
            else ("APIM automatic initialization is not configured on this platform."),
            items=[
                self._operations.release_operation(UUID(str(row["id"])))
                for row in self._repository.list_apim_onboarding_operations()
            ],
        )

    def _configuration_digest(self) -> str:
        if not self._available or self._canonical_parent is None:
            raise ControlPlaneUnavailableError("APIM automatic initialization is unavailable")
        if self._configuration is None and self._configuration_provider is not None:
            self._configuration = self._configuration_provider()
        if self._configuration is None:
            raise ControlPlaneUnavailableError(
                "The platform governance configuration is unavailable"
            )
        return onboarding_policy_configuration_digest(self._configuration, self._canonical_parent)

    def preview(self, request: ApimOnboardingRequest) -> ApimOnboardingPlan:
        digest = self._configuration_digest()
        assert self._configuration is not None
        if request.tenant_id != self._configuration.management_tenant_id:
            raise ControlPlaneConflictError(
                "The tenant ID does not match the tenant of the Turnstile management identity"
            )
        blockers = () if self._budget_ready() else ("budget_execution_not_ready",)
        with self._client_factory(request) as client:
            plan = client.inspect(
                management_tenant_id=self._configuration.management_tenant_id,
                configuration_digest=digest,
                configuration_blockers=blockers,
            )
        if any(
            str(row["arm_resource_id"]).casefold() == request.resource_id.casefold()
            and str(row["gateway_profile_id"]) != str(request.gateway_profile_id)
            for row in self._repository.list_apim_targets()
        ):
            plan = plan.model_copy(
                update={"ready": False, "blockers": [*plan.blockers, "apim_already_registered"]}
            )
        capability = self_hosted_gateway_capability_for_tier(plan.service_tier)
        fixed_program_sha256 = None
        if capability is not CapabilitySupport.UNKNOWN:
            fixed_program_sha256 = render_onboarding_fixed_program(
                request, self._configuration, cast(str, self._canonical_parent),
                self_hosted_gateway=capability,
            ).artifact_sha256
        plan = plan.model_copy(
            update={
                "initial_monthly_token_limit": self._monthly_token_limit,
                "initial_tokens_per_minute": self._tokens_per_minute,
                "fixed_program_sha256": fixed_program_sha256,
            }
        )
        return plan.model_copy(update={"confirmation_sha256": onboarding_plan_digest(plan, digest)})

    def _accepted(self, operation_id: UUID) -> GatewayReleaseOperationAccepted:
        return GatewayReleaseOperationAccepted(
            operation=self._operations.release_operation(operation_id),
            status_url=f"/api/v1/model-management/release-operations/{operation_id}",
        )

    def start(self, write: ApimOnboardingStart, actor: str) -> GatewayReleaseOperationAccepted:
        self._configuration_digest()
        if self._fixed_initializer is None:
            raise ControlPlaneUnavailableError("APIM fixed program initialization is unavailable")
        assert self._cipher is not None
        existing = next(
            (
                row
                for row in self._repository.list_apim_onboarding_operations()
                if str(row["gateway_profile_id"]) == str(write.request.gateway_profile_id)
            ),
            None,
        )
        if existing is not None:
            if existing["confirmation_sha256"] != write.confirmation_sha256:
                raise ControlPlaneConflictError(
                    "This APIM already has a different initialization plan"
                )
            return self._accepted(UUID(str(existing["id"])))
        plan = self.preview(write.request)
        if not plan.ready or plan.confirmation_sha256 != write.confirmation_sha256:
            raise ControlPlaneConflictError(
                "APIM prerequisites or configuration changed; review the plan again"
            )
        keys = {
            purpose: {kind: secrets.token_hex(32) for kind in ("primary", "secondary")}
            for purpose in ("dashboard", "probe")
        }
        row = self._repository.create_apim_onboarding_operation(
            plan,
            self._cipher.encrypt(json.dumps(keys)),
            actor,
        )
        return self._accepted(UUID(str(row["id"])))

    def retry(self, operation_id: UUID, actor: str) -> GatewayReleaseOperationAccepted:
        self._configuration_digest()
        prior = self._repository.get_gateway_release_operation(operation_id)
        if prior is None or prior["operation_kind"] != "apim_onboarding":
            raise ControlPlaneConflictError("APIM initialization operation was not found")
        plan = ApimOnboardingPlan.model_validate(prior["semantic_preview"])
        latest = self.preview(plan.request)
        if not latest.ready or latest.confirmation_sha256 != plan.confirmation_sha256:
            raise ControlPlaneConflictError(
                "The initialization plan changed or prerequisites remain unavailable"
            )
        row = self._repository.retry_apim_onboarding_operation(operation_id, actor)
        return self._accepted(UUID(str(row["id"])))

    def _keys(self, operation_id: UUID) -> dict[str, dict[str, str]]:
        assert self._cipher is not None
        plaintext = self._cipher.decrypt(
            self._repository.gateway_release_operation_secret(operation_id)
        )
        if plaintext is None:
            raise PolicyCompilationError("APIM initialization credentials are unavailable")
        try:
            values = json.loads(plaintext)
            if set(values) != {"dashboard", "probe"} or any(
                set(values[purpose]) != {"primary", "secondary"}
                or any(
                    not isinstance(value, str) or len(value) < 32
                    for value in values[purpose].values()
                )
                or values[purpose]["primary"] == values[purpose]["secondary"]
                for purpose in ("dashboard", "probe")
            ):
                raise ValueError
        except (TypeError, ValueError, KeyError) as error:
            raise PolicyCompilationError("APIM initialization credentials are invalid") from error
        return cast(dict[str, dict[str, str]], values)

    def _transition(
        self,
        row: Mapping[str, Any],
        worker_id: str,
        status: str,
        completed: list[str],
        **details: object,
    ) -> None:
        changed = self._repository.transition_gateway_release_operation(
            UUID(str(row["id"])),
            str(row["status"]),
            status,
            {
                "checkpoint": {
                    **dict(row.get("checkpoint") or {}),
                    **details,
                    "step": completed[-1],
                    "completed_steps": completed,
                },
                "error_code": None,
                "error_message": None,
            },
            worker_id,
        )
        if changed is None:
            raise ControlPlaneConflictError("The initialization lease changed")

    def advance(self, row: Mapping[str, Any], worker_id: str) -> None:
        digest = self._configuration_digest()
        if self._projector is None:
            raise ControlPlaneUnavailableError(
                "APIM initialization ledger projection is unavailable"
            )
        try:
            plan = ApimOnboardingPlan.model_validate(row["semantic_preview"])
        except (ValidationError, KeyError) as error:
            raise PolicyCompilationError(
                "The stored APIM initialization plan is invalid"
            ) from error
        if (
            row["operation_kind"] != "apim_onboarding"
            or str(row["gateway_profile_id"]) != str(plan.request.gateway_profile_id)
            or plan.gateway_profile_id != plan.request.gateway_profile_id
            or plan.confirmation_sha256 != row["confirmation_sha256"]
            or onboarding_plan_digest(plan, digest) != plan.confirmation_sha256
        ):
            raise PolicyCompilationError(
                "The APIM initialization plan identity or configuration changed"
            )
        if not self._budget_ready():
            raise ControlPlaneUnavailableError("Budget execution prerequisites are not ready")
        assert self._configuration is not None and self._canonical_parent is not None
        assert self._cipher is not None
        operation_id = UUID(str(row["id"]))
        status = str(row["status"])
        program = render_onboarding_fixed_program(
            plan.request, self._configuration, self._canonical_parent,
            self_hosted_gateway=self_hosted_gateway_capability_for_tier(plan.service_tier),
        )
        if self._fixed_initializer is None or plan.fixed_program_sha256 != program.artifact_sha256:
            raise PolicyCompilationError("The initialization fixed program is missing or changed")
        current_row = dict(row)

        def record_program_intent(resource: str, request_sha256: str) -> bool:
            nonlocal current_row
            checkpoint = dict(current_row.get("checkpoint") or {})
            requests = dict(checkpoint.get("fixed_program_requests") or {})
            if resource in requests:
                if requests[resource] != request_sha256:
                    raise PolicyCompilationError("The initialization program write intent changed")
                return False
            requests[resource] = request_sha256
            checkpoint["fixed_program_requests"] = requests
            changed = self._repository.transition_gateway_release_operation(
                operation_id, status, status, {"checkpoint": checkpoint}, worker_id,
                keep_lease=True,
            )
            if changed is None:
                raise ControlPlaneConflictError("The initialization lease changed before writing")
            current_row = dict(changed)
            return True

        with self._client_factory(plan.request) as client:
            if status == "queued":
                current = self.preview(plan.request)
                if not current.ready or current.confirmation_sha256 != plan.confirmation_sha256:
                    raise PolicyCompilationError(
                        "The APIM initialization preflight no longer matches"
                    )
                client.verify_initialization_permissions(self._configuration)
                self._transition(row, worker_id, "validating_dependencies", plan.steps[:1])
            elif status == "validating_dependencies":
                client.ensure_api()
                self._transition(row, worker_id, "promoting", plan.steps[:2])
            elif status == "promoting":
                principal_id = client.ensure_governance_dependencies(self._configuration)
                client.ensure_fixed_program(program, record_program_intent)
                self._transition(
                    current_row,
                    worker_id,
                    "verifying_readback",
                    plan.steps[:3],
                    apim_principal_id=str(principal_id),
                )
            elif status == "verifying_readback":
                keys = self._keys(operation_id)
                client.ensure_subscriptions(keys)
                self._materialize(plan, UUID(str(row["checkpoint"]["apim_principal_id"])), False)
                self._projector(plan, operation_id, worker_id)
                self._transition(row, worker_id, "preflight_probing", plan.steps[:5])
            elif status == "preflight_probing":
                keys = self._keys(operation_id)
                client.ensure_api()
                client.ensure_fixed_program(program, record_program_intent)
                client.ensure_subscriptions(keys, activate=True)
                installation = self._fixed_initializer(plan, program, operation_id, worker_id)
                installation.require_empty_fixed_installation()
                if (
                    installation.gateway_profile_id != plan.gateway_profile_id
                    or installation.policy_sha256 != program.binding.program_sha256
                ):
                    raise PolicyCompilationError("The verified initialization program changed")
                raw_installation = installation.resource_manifest["fixed_route_program"]
                if not isinstance(raw_installation, dict):
                    raise PolicyCompilationError("The verified initialization program is missing")
                client.verify_managed_gateway(
                    plan, keys, fixed_installation=FixedProgramInstallation.model_validate_json(
                        canonical_document(raw_installation),
                    ),
                )
                self._materialize(plan, UUID(str(row["checkpoint"]["apim_principal_id"])), True)
                result = self._repository.finish_apim_onboarding_operation(
                    operation_id,
                    worker_id,
                    self._cipher.encrypt(keys["dashboard"]["primary"]),
                    credential_hint(keys["dashboard"]["primary"]),
                    digest,
                    installation,
                )
                if result is None:
                    raise ControlPlaneConflictError(
                        "The initialization lease changed before activation"
                    )
            else:
                raise PolicyCompilationError("APIM initialization has an unsupported state")

    def _materialize(self, plan: ApimOnboardingPlan, principal_id: UUID, verified: bool) -> None:
        now = datetime.now(UTC)
        family = (
            ApimTierFamily.V2
            if plan.service_tier.casefold().endswith("v2")
            else ApimTierFamily.CLASSIC
        )
        target = ApimTarget(
            gateway_profile_id=plan.gateway_profile_id,
            arm_resource_id=plan.request.resource_id,
            tenant_id=plan.request.tenant_id,
            subscription_id=plan.request.subscription_id,
            resource_group=plan.request.resource_group,
            service_name=plan.service_name,
            api_id=plan.api_id,
            api_path=plan.api_path,
            service_tier=plan.service_tier,
            tier_family=family,
            service_location=plan.location,
            principal_id=principal_id,
            management_auth_profile_ref="system_managed_identity",
            probe_subscription_id=plan.probe_subscription_id,
            ownership_mode=ApimTargetOwnershipMode.TURNSTILE_MANAGED,
            capabilities=ApimTargetCapabilitySnapshot(
                    self_hosted_gateway=self_hosted_gateway_capability_for_tier(plan.service_tier),
                backend_pools=CapabilitySupport.SUPPORTED,
                managed_identity_policy=CapabilitySupport.SUPPORTED,
                event_hub_logger=CapabilitySupport.SUPPORTED,
                table_ledger=CapabilitySupport.SUPPORTED,
                responses=CapabilitySupport.SUPPORTED,
                anthropic_messages=CapabilitySupport.SUPPORTED,
                response_observer=CapabilitySupport.UNSUPPORTED,
                source_api_version="2024-05-01",
                observed_at=now,
            ),
            last_inventory_sync_at=now,
            last_validated_at=now if verified else None,
            drift_state=ApimTargetDriftState.IN_SYNC if verified else ApimTargetDriftState.UNKNOWN,
        )
        self._repository.upsert_apim_target(target)
        deployment = GatewayDeployment(
            id=gateway_deployment_id_for(plan.gateway_profile_id, "managed"),
            apim_target_id=plan.gateway_profile_id,
            native_gateway_id="managed",
            kind=GatewayDeploymentKind.MANAGED,
            display_name=f"{plan.service_name} Managed",
            location=plan.location,
            endpoint_url=HttpUrl(
                str(plan.managed_gateway_origin).rstrip("/") + "/" + plan.api_path
            ),
            hosting_mode=GatewayHostingMode.AZURE_MANAGED,
            lifecycle_ownership=GatewayLifecycleOwnership.EXTERNAL,
            declared_state=GatewayDeclaredState.PRESENT,
            reachability_state=(
                GatewayReachabilityState.REACHABLE if verified else GatewayReachabilityState.UNKNOWN
            ),
            observation_state=GatewayObservationState.OBSERVED
            if verified
            else GatewayObservationState.UNKNOWN,
            last_arm_sync_at=now,
            last_probe_at=now if verified else None,
            last_observed_at=now if verified else None,
        )
        self._repository.upsert_gateway_deployment(deployment)
        self._repository.upsert_gateway_deployment_inventory(
            GatewayDeploymentInventory(
                deployment_id=deployment.id,
                associated_api_ids=(plan.api_id,),
                source_api_version="2024-05-01",
                synced_at=now,
            )
        )


def onboarding_template_path(settings: Settings) -> Path:
    path = settings.control_plane_parent_policy_path
    if path.is_absolute() or path.is_file():
        return path
    root = Path(__file__).resolve().parents[2]
    packaged = root / path
    if packaged.is_file() or path != Path("policies/foundry-finops-policy.xml"):
        return packaged
    return root / "infra" / path


def initialized_apim_targets(repository: QueryRepository) -> dict[UUID, ApimOnboardingRequest]:
    requests: dict[UUID, ApimOnboardingRequest] = {}
    targets = {str(row["gateway_profile_id"]): row for row in repository.list_apim_targets()}
    for profile in repository.get_gateway_profiles():
        config = profile.get("config") or {}
        if (
            profile.get("implementation") != "apim"
            or not profile.get("enabled")
            or config.get("onboarding_ready") is not True
        ):
            continue
        try:
            request = ApimOnboardingRequest.model_validate(config.get("onboarding_request"))
        except ValidationError as error:
            raise PolicyCompilationError(
                "An initialized APIM has invalid persisted identity"
            ) from error
        target = targets.get(str(profile["id"]))
        if (
            str(request.gateway_profile_id) != str(profile["id"])
            or target is None
            or target["ownership_mode"] != "turnstile_managed"
            or str(target["arm_resource_id"]).casefold() != request.resource_id.casefold()
            or str(target["tenant_id"]) != str(request.tenant_id)
            or target["api_id"] != request.api_id
            or target["api_path"] != request.api_path
            or target["probe_subscription_id"] != request.probe_subscription_id
        ):
            raise PolicyCompilationError(
                "An initialized APIM target no longer matches its identity"
            )
        requests[request.gateway_profile_id] = request
    return requests


def apim_onboarding_available(settings: Settings) -> bool:
    return bool(
        settings.apim_target_registry_enabled
        and settings.gateway_release_worker_enabled
        and settings.budget_execution_enabled
        and settings.ledger_table_endpoint
        and settings.credential_encryption_key
        and settings.legacy_apim_identity
        and settings.data_backend == "postgresql"
        and onboarding_template_path(settings).is_file()
    )


def build_apim_onboarding_service(
    repository: QueryRepository,
    settings: Settings,
) -> ApimOnboardingService:
    enabled = apim_onboarding_available(settings)
    source = onboarding_template_path(settings).read_text(encoding="utf-8") if enabled else None
    resolved_configuration: ApimOnboardingConfiguration | None = None

    def configuration() -> ApimOnboardingConfiguration:
        nonlocal resolved_configuration
        if resolved_configuration is None:
            management_settings = settings.model_copy(update={
                "apim_gateway_url": settings.apim_gateway_url
                or f"https://{settings.apim_service_name}.azure-api.net",
            })
            with httpx.Client(timeout=30.0, follow_redirects=False) as http:
                current = AzureApimPlatformConfigurationClient(
                    management_settings, client=http,
                ).configuration()
            ledger = current.ledger_table_resource_id.split("/")
            if (
                (settings.ledger_table_endpoint or "").rstrip("/")
                != f"https://{ledger[8]}.table.core.windows.net"
                or settings.ledger_table_name != ledger[-1]
            ):
                raise PolicyCompilationError(
                    "The platform governance configuration does not match its budget ledger"
                )
            resolved_configuration = current
        return resolved_configuration

    def budget_ready() -> bool:
        if not isinstance(repository, BudgetScopeRepository):
            return False
        rollout = repository.active_budget_execution_rollout()
        return rollout is not None and rollout["prepared_period"] == datetime.now(
            UTC
        ).date().replace(day=1)

    def project(plan: ApimOnboardingPlan, operation_id: UUID, worker_id: str) -> None:
        current = configuration()
        if not settings.ledger_table_endpoint:
            raise PolicyCompilationError("APIM initialization dependencies are unavailable")
        parts = current.ledger_table_resource_id.split("/")
        if (
            settings.ledger_table_endpoint.rstrip("/")
            != f"https://{parts[8]}.table.core.windows.net"
            or settings.ledger_table_name != parts[-1]
        ):
            raise PolicyCompilationError(
                "APIM initialization must use the platform's configured ledger"
            )
        if not isinstance(repository, BudgetScopeRepository):
            raise PolicyCompilationError("APIM initialization requires durable budget scopes")
        with AtomicTableLedger(
            settings.ledger_table_endpoint, settings.ledger_table_name
        ) as ledger:

            def prepare_scope(application_id: UUID, epoch: UUID) -> None:
                if not budget_ready():
                    raise RetryablePublicationError("Budget period preparation is pending")
                prepared = BudgetScopeService(repository, ledger).prepare(
                    application_id=application_id,
                    gateway_profile_id=plan.gateway_profile_id,
                )
                scope = LedgerScope(
                    scope_type="application",
                    scope_id=str(application_id),
                    period_start=datetime.now(UTC).date().replace(day=1),
                )
                summary = ledger.read_summary(scope)
                if prepared != 1 or summary.epoch != epoch or not summary.enabled:
                    raise RetryablePublicationError("The new Application budget is not yet active")

            project_onboarding_ledger(
                repository, ledger, plan, operation_id, worker_id, prepare_scope
            )

    def initialize(
        plan: ApimOnboardingPlan, program: FixedRouteProgram, operation_id: UUID, worker_id: str,
    ) -> GatewayPublication:
        if not isinstance(repository, BudgetScopeRepository):
            raise ControlPlaneUnavailableError("APIM initialization requires durable budget scopes")

        def operation() -> dict[str, Any]:
            current = repository.get_gateway_release_operation(operation_id)
            if (
                current is None or current["operation_kind"] != "apim_onboarding"
                or current["status"] != "preflight_probing"
                or current["lease_owner"] != worker_id
                or current["lease_expires_at"] is None
                or current["lease_expires_at"] <= datetime.now(UTC)
                or str(current["gateway_profile_id"]) != str(plan.gateway_profile_id)
                or current["confirmation_sha256"] != plan.confirmation_sha256
            ):
                raise ControlPlaneConflictError("APIM initialization ownership changed")
            return dict(current)

        def save(state: dict[str, Any]) -> None:
            current = operation()
            checkpoint = {**current["checkpoint"], "fixed_route_initialization": state}
            changed = repository.transition_gateway_release_operation(
                operation_id, "preflight_probing", "preflight_probing",
                {"checkpoint": checkpoint}, worker_id, keep_lease=True,
            )
            if changed is None:
                raise ControlPlaneConflictError("APIM initialization lease changed")

        current = operation()
        rollout = repository.active_budget_execution_rollout()
        if rollout is None or not budget_ready():
            raise ControlPlaneUnavailableError("APIM initialization budget epoch is unavailable")
        epoch = str(UUID(str(rollout["epoch"])))
        endpoint = str(plan.managed_gateway_origin).rstrip("/") + "/" + plan.api_path
        keys = service._keys(operation_id)
        with httpx.Client(timeout=30.0, follow_redirects=False) as http:
            tokens = ManagedIdentityTokenProvider(client=http)
            management = AzureApimOnboardingClient(plan.request, token_provider=tokens, client=http)
            installed = program.installation(document_digest(plan.request), endpoint)
            verifier = FixedProgramVerifier(
                installed, management, SecretStr(keys["probe"]["primary"]), http,
            )
            verifier.verify()
            service._materialize(
                plan, UUID(str(current["checkpoint"]["apim_principal_id"])), True,
            )
            target = RepositoryApimTargetClientConfigResolver(
                repository, settings, AzureApimTargetProbeCredentialResolver(),
            ).resolve(plan.gateway_profile_id, include_probe_credentials=False)
            if target.route_target_identity_sha256 is None:
                raise PolicyCompilationError("APIM initialization target binding is unavailable")
            installed = program.installation(target.route_target_identity_sha256, endpoint)
            verifier = FixedProgramVerifier(
                installed, management, SecretStr(keys["probe"]["primary"]), http,
            )
            proof = verifier.verify()
            projection = TableRouteProjection(
                installed.storage_endpoint, installed.storage_table,
                installed.program.scope, tokens, http,
            )
            previous_proof = projection.read_program_proof(installed.program)
            projection.publish_program_proof(
                proof, installed.program,
                previous_proof[1] if previous_proof else None, int(time()),
            )
            state = dict(operation()["checkpoint"].get("fixed_route_initialization") or {})
            if state:
                if state.get("program_artifact_sha256") != program.artifact_sha256:
                    raise PolicyCompilationError("APIM initialization route program changed")
                snapshot = RouteSnapshot.model_validate_json(canonical_document(state["snapshot"]))
                if snapshot.approval.budget_epoch != epoch:
                    raise PolicyCompilationError("APIM initialization budget epoch changed")
            else:
                instant = int(time())
                version = str(uuid5(operation_id, "initial-empty-configuration"))
                _, manifest, dependencies = configuration_manifests(
                    installed.program.scope, version, (), {}, instant,
                )
                approval = ConfigurationApproval(
                    approval_id=str(uuid5(operation_id, "initial-configuration-approval")),
                    scope=installed.program.scope, route_version=version, generation=1,
                    manifest_sha256=manifest, dependency_manifest_sha256=dependencies,
                    catalog_sha256=catalog_digest(()), entry_count=0,
                    required_program_sha256=installed.program.program_sha256,
                    required_profiles=(), budget_epoch=epoch, issued_epoch=instant,
                    activate_before_epoch=instant + 600,
                )
                snapshot = RouteSnapshot.prepare(approval, (), {}, instant)
                state = {"program_artifact_sha256": program.artifact_sha256,
                         "snapshot": snapshot.model_dump(mode="json")}
                save(state)
            journal = _OnboardingActivationJournal(state, save)
            if state.get("activation") is not None:
                intent = RouteActivationIntent.from_record(state["activation"]["intent"])
                if intent.head.approval != snapshot.approval or projection.recover_activation(
                    intent,
                ) != "confirmed":
                    raise RetryablePublicationError("APIM initialization activation is unconfirmed")
                head = intent.head
            else:
                operation()
                projection.stage(snapshot, int(time()))
                operation()
                projection.activate(
                    snapshot, installed.program, proof, epoch,
                    str(uuid5(operation_id, "initial-route-activation")), None,
                    lambda: int(time()), journal,
                )
                head = RouteActivationIntent.from_record(
                    journal.state["activation"]["intent"],
                ).head
            observed = projection.read_active()
            if observed is None or observed[0] != head:
                raise PolicyCompilationError("APIM initialization route head changed")
            verifier.confirm(head)
            if repository.active_budget_execution_rollout() != rollout:
                raise ControlPlaneConflictError("APIM initialization budget rollout changed")
            return onboarding_fixed_publication(
                plan, program, operation_id, installed, snapshot, head, str(current["created_by"]),
            )

    service = ApimOnboardingService(
        repository,
        enabled=enabled,
        configuration=None,
        configuration_provider=configuration if enabled else None,
        canonical_parent=source,
        cipher=CredentialCipher.from_settings(settings) if enabled else None,
        budget_ready=budget_ready,
        projector=project if enabled else None,
        fixed_initializer=initialize if enabled else None,
        monthly_token_limit=settings.gateway_application_default_monthly_token_limit,
        tokens_per_minute=settings.gateway_application_default_tokens_per_minute,
    )
    return service
