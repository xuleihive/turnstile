from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from threading import Lock
from time import monotonic, time
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid5
from xml.etree import ElementTree as ET

import httpx
from pydantic import Field, SecretStr

from ..domain.billable_requests import BillableRequestOutcome
from ..domain.budget_counting import BudgetOperation, BudgetPublicationContracts
from ..domain.control_plane import (
    COUNT_TOKENS_REJECTIONS,
    ApiFormat,
    AuthStrategy,
    GatewayModelBinding,
    GatewayPublication,
    GatewayReleaseDependencies,
    PublicationKind,
    program_migration_spec_matches,
)
from ..domain.image_profiles import ImageGenerationLimits, validate_image_profile
from ..domain.model_availability import (
    AVAILABILITY_PARTITION,
    ModelAvailabilityCoverage,
    ModelAvailabilityNodeEnrollment,
    ModelAvailabilityState,
    ModelAvailabilityUnavailable,
)
from ..domain.runtime_models import InvocationUsage, OAuthClientCredentialsConfig
from ..domain.storage_routes import (
    ActiveRouteHead,
    ConfigurationApproval,
    ConnectionApproval,
    ConnectionRevision,
    Digest,
    EpochSeconds,
    FixedProgramUpgrade,
    Identifier,
    ModelRouteHead,
    ProgramCapabilityProof,
    RouteApprovalRenewal,
    RouteBackendPool,
    RouteCandidateProbe,
    RouteContent,
    RouteCredential,
    RouteModel,
    RoutePoolMember,
    RoutePublicationTask,
    RouteScope,
    RouteSnapshot,
    canonical_document,
    catalog_entry,
    document_digest,
    text_digest,
)
from ..domain.storage_routes import (
    FixedProgramInstallation as FixedProgramInstallation,
)
from ..domain.storage_routes import (
    ManagedProgramInitialization as ManagedProgramInitialization,
)
from ..integrations.apim_control_plane_contract import (
    BackendCircuitBreakerResource,
    BackendResource,
    CredentialSourceMissingError,
    GatewayCapabilityUnavailableError,
    OAuthCredentialResource,
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.apim_policy_components import (
    component_digest,
    encode_policy_xml,
    parse_policy,
    validate_policy_upload,
)
from ..integrations.apim_publisher_client import AzureApimPublisherClient
from ..integrations.apim_storage_routes import (
    ROUTE_DISPATCH_READY,
    fixed_operation_contracts,
    fixed_policy_digest,
    fixed_program_digest,
)
from ..integrations.apim_storage_routes import (
    FixedProgramVerifier as FixedProgramVerifier,
)
from ..integrations.apim_target_client_factory import (
    ApimTargetClientConfig,
    ApimTargetClientConfigResolver,
    route_target_identity_matches,
)
from ..integrations.gateway import _decision_usage, _openai_usage
from ..integrations.image_generation import generated_image, image_usage
from ..integrations.ledger import TableStorageLedger
from ..integrations.reconciliation import ManagedIdentityTokenProvider
from ..integrations.route_credential_vault import (
    RouteCredentialVault as RouteCredentialVault,
)
from ..integrations.route_credential_vault import (
    VaultCheckpoint as _VaultCheckpoint,
)
from ..integrations.route_credential_vault import (
    _CredentialBindings as _CredentialBindings,
)
from ..integrations.route_credential_vault import credential_bindings
from ..integrations.storage_route_projection import (
    RouteActivationIntent,
    RouteProjectionConflict,
    TableRouteProjection,
)
from ..persistence.repository_contract import QueryRepository
from ..security import CredentialCipher

if TYPE_CHECKING:
    from ..services.model_availability import ModelAvailabilityRepository
    from ..services.probe_journal import PersistentProbeJournal

logger = logging.getLogger(__name__)


def native_gateway_programs(
    installed: FixedProgramInstallation,
    deployment: Mapping[str, Any],
    enrollment: ModelAvailabilityNodeEnrollment,
) -> tuple[FixedProgramInstallation, ...]:
    if (
        not installed.native_gateway_scope
        or deployment.get("retired_at") is not None
        or str(deployment.get("apim_target_id")) != installed.program.scope.target_id
        or str(deployment.get("id")) != str(enrollment.deployment_id)
        or str(enrollment.target_id) != installed.program.scope.target_id
        or enrollment.api_id != installed.program.scope.api_id
        or deployment.get("native_gateway_id") != enrollment.native_gateway_id
        or deployment.get("declared_state") != "present"
        or not deployment.get("endpoint_url")
    ):
        raise PolicyCompilationError("Native Gateway program requires its current node enrollment")
    return tuple(
        installed.for_native_gateway(
            enrollment.native_gateway_id, node.instance_id,
            str(node.endpoint_url).rstrip("/"),
        )
        for node in sorted(enrollment.nodes, key=lambda value: value.instance_id)
    )


class _ConnectionPreparation(RouteModel):
    connection: ConnectionRevision
    issued_epoch: EpochSeconds
    intent: dict[str, Any]


class _PoolWriteIntent(RouteModel):
    version: int = Field(strict=True, ge=1, le=1)
    method: Literal["PUT"]
    resource_id: str = Field(min_length=1, max_length=2048)
    api_version: Literal["2024-05-01", "2025-09-01-preview"]
    if_none_match: Literal["*"]
    connection_sha256: Digest
    body: str = Field(min_length=1, max_length=65536)
    body_sha256: Digest


class _OAuthWriteIntent(RouteModel):
    version: Literal[1] = 1
    method: Literal["PUT"] = "PUT"
    resource_id: str = Field(min_length=1, max_length=2048)
    api_version: Literal["2024-05-01"] = "2024-05-01"
    if_none_match: Literal["*"] = "*"
    configuration_sha256: Digest
    body_sha256: Digest
    body_ciphertext: str = Field(repr=False, min_length=1, max_length=131072)


class _OAuthPreparation(RouteModel):
    configuration: OAuthClientCredentialsConfig
    source_ciphertext: str | None = Field(default=None, repr=False, max_length=65536)
    requests: dict[Digest, _OAuthWriteIntent] = Field(default_factory=dict, max_length=3)


class _PreparationCheckpoint(RouteModel):
    version: Literal[1] = 1
    context_sha256: Digest
    connections: dict[str, _ConnectionPreparation] = Field(default_factory=dict, max_length=10000)
    credentials: dict[Digest, dict[str, Any]] = Field(default_factory=dict, max_length=10000)
    oauth: dict[Identifier, _OAuthPreparation] = Field(
        default_factory=dict, max_length=10000, exclude_if=lambda value: not value,
    )


def register_verified_managed_program(
    repository: QueryRepository, configurations: ApimTargetClientConfigResolver,
    initialized: ManagedProgramInitialization, expected_config_sha256: str,
    *, clock: Callable[[], int] = lambda: int(time()), validate_only: bool = False,
) -> None:
    from ..persistence.repository_support import _managed_program_initialization_config

    installed = initialized.installation
    target_id = UUID(installed.program.scope.target_id)
    availability = cast("ModelAvailabilityRepository", repository)
    with availability.model_availability_proof_guard(target_id, wait=True):
        previous = repository.effective_gateway_publication(target_id)
        gateways = repository.get_gateway_profiles([target_id])
        if previous is None or len(gateways) != 1 or gateways[0]["id"] != target_id:
            raise PolicyCompilationError("Managed initialization Gateway is unavailable")
        _managed_program_initialization_config(
            previous, gateways[0].get("config") or {}, initialized.model_dump(mode="json"),
            expected_config_sha256,
        )
        config = configurations.resolve(target_id, include_probe_credentials=True)
        if (
            config.gateway_profile_id != target_id
            or not route_target_identity_matches(config, installed.target_identity_sha256)
            or config.settings.apim_api_id != installed.program.scope.api_id
            or str(config.settings.apim_gateway_url or "").rstrip("/") != installed.gateway_endpoint
            or config.settings.apim_probe_subscription_id != installed.publisher_subscription_id
            or config.settings.apim_probe_subscription_key is None
        ):
            raise PolicyCompilationError("Managed initialization target binding changed")
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            management = AzureApimPublisherClient(config.settings, client=client)
            verifier = FixedProgramVerifier(
                installed, management, config.settings.apim_probe_subscription_key,
                client, clock=clock, expected_current_revision=initialized.previous_revision,
            )
            projection = TableRouteProjection(
                installed.storage_endpoint, installed.storage_table,
                installed.program.scope, client=client,
            )
            verifier.verify()
            proof = projection.read_program_proof(installed.program)
            now = clock()
            if (proof is None or not proof[0].issued_epoch <= now < proof[0].expires_epoch
                    or proof[0].supported_profiles != installed.supported_profiles):
                raise PolicyCompilationError("Managed initialization current proof is unavailable")
            observed = projection.read_active()
            if observed is None or observed[0] != initialized.head:
                raise PolicyCompilationError("Managed initialization empty configuration changed")
            verifier.confirm(initialized.head)
            if (projection.read_active() != observed or clock() >= proof[0].expires_epoch
                    or management.current_revision() != initialized.previous_revision):
                raise PolicyCompilationError("Managed initialization changed during verification")
            if validate_only:
                return
            repository.record_managed_program_initialization(
                initialized.model_dump(mode="json"), expected_config_sha256,
            )


def _validate_program_cutover(
    parent_policy: str, previous: FixedProgramInstallation,
    installed: FixedProgramInstallation, config: ApimTargetClientConfig,
    management: AzureApimPublisherClient,
    operation_policies: Mapping[str, str] | None = None,
    *, previous_publication_id: str | None = None, original_parent_policy: str | None = None,
) -> None:
    from ..integrations.apim_budget_execution import (
        bind_budget_delegation,
        eventhub_logger_id,
        usage_identity_expression,
    )
    from ..integrations.apim_storage_routes import (
        FixedRouteBinding,
        auxiliary_operation,
        fixed_operation,
        installed_route_binding,
        prepare_budget_delegation_upgrade,
        refresh_fixed_budget_execution_policy,
        retired_operation,
        upgrade_fixed_pool_program,
    )

    validate_policy_upload(
        parent_policy,
        gateway_profile_id=config.gateway_profile_id,
        api_id=installed.program.scope.api_id,
        api_revision=installed.program.api_revision,
        table_endpoint=config.settings.ledger_table_endpoint,
        table_name=config.settings.ledger_table_name,
    )
    operations = management.route_program_operations(previous.program.api_revision)
    enables_pool = ((installed.pool_enabled and not previous.pool_enabled)
                    or (installed.router_enabled and not previous.router_enabled))
    enables_decision = installed.decision_enabled and not previous.decision_enabled
    enables_images = installed.image_limits is not None and previous.image_limits is None
    refreshes = original_parent_policy is not None
    rewrites = bool(operation_policies) and (
        enables_pool or refreshes or enables_decision or enables_images
    )
    # A resumed Decision cutover may have created its Operation before the interruption.
    if (set(operations) not in ([set(previous.operation_contracts)] + (
            [set(installed.operation_contracts)] if enables_decision and rewrites else []))
            or any((method, path) != installed.operation_contracts[name]
                   or fixed_policy_digest(policy) not in {
                       previous.operation_policies_sha256.get(name),
                       installed.operation_policies_sha256[name] if rewrites
                       else previous.operation_policies_sha256.get(name),
                   }
                   for name, (method, path, policy) in operations.items())):
        raise PolicyCompilationError("Program cutover operation contract changed")
    contracts = fixed_operation_contracts(
        installed.operation_contracts["models"][1],
        installed.operation_contracts["anthropic-messages"][1], installed.retired_operations,
        decision_enabled=installed.decision_enabled,
    )
    selected = {name: operation[2] for name, operation in operations.items()}
    pool_binding = installed_route_binding(
        installed, config.settings.ledger_table_endpoint, config.settings.ledger_table_name,
        config.settings.apim_usage_observer_key_named_value,
    ) if enables_pool else None
    refreshed_operations = None
    if original_parent_policy is not None:
        from ..integrations.apim_model_availability import compose_availability_parent
        from ..integrations.apim_onboarding_policy import onboarding_canonical_parent
        from ..services.apim_onboarding import onboarding_template_path

        if (previous_publication_id is None
                or fixed_policy_digest(original_parent_policy) != previous.parent_policy_sha256):
            raise PolicyCompilationError("Program refresh requires its approved original parent")
        binding = installed_route_binding(
            previous, config.settings.ledger_table_endpoint, config.settings.ledger_table_name,
            config.settings.apim_usage_observer_key_named_value,
        )
        canonical = compose_availability_parent(onboarding_canonical_parent(
            onboarding_template_path(config.settings).read_text(encoding="utf-8"),
            binding.scope.api_id,
        ))
        canonical = bind_budget_delegation(
            canonical, config.settings.budget_delegation_trust.get(config.gateway_profile_id),
            gateway_profile_id=config.gateway_profile_id,
        )
        refreshed = upgrade_fixed_pool_program(
            refresh_fixed_budget_execution_policy(original_parent_policy, binding, canonical),
            binding,
        )
        refreshed_operations = dict(refreshed.operation_policies)
        expected, expected_parent = prepare_budget_delegation_upgrade(
            previous, previous_publication_id, refreshed.parent_policy,
            {name: (method, path, refreshed_operations[name])
             for name, method, path in refreshed.operation_contracts},
        )
        if expected.installation != installed or fixed_policy_digest(expected_parent) != (
            installed.parent_policy_sha256
        ):
            raise PolicyCompilationError("Program refresh changed its compiler-bound artifact")
    if operation_policies:
        parent = parse_policy(parent_policy)
        if set(operation_policies) != set(contracts):
            raise PolicyCompilationError("Program cutover operation set changed")
        for name, policy in operation_policies.items():
            if refreshed_operations is not None:
                original = parse_policy(refreshed_operations[name])
            elif pool_binding is not None:
                original = parse_policy(
                    retired_operation() if name in installed.retired_operations else
                    auxiliary_operation() if name in {
                        "models", "storage-route-program-probe",
                        "storage-route-configuration-probe",
                    } else fixed_operation(
                        name, pool_binding, logger_id=eventhub_logger_id(parent),
                        usage_identity=usage_identity_expression(parent),
                    )
                )
            elif name == "images-generations" and enables_images:
                original = parse_policy(fixed_operation(
                    name, installed_route_binding(
                        installed, config.settings.ledger_table_endpoint,
                        config.settings.ledger_table_name,
                        config.settings.apim_usage_observer_key_named_value,
                    ), logger_id=eventhub_logger_id(parent),
                    usage_identity=usage_identity_expression(parent),
                ))
            elif name not in selected or name == "microsoft-decision" and enables_decision:
                if (name != "microsoft-decision" or previous.decision_enabled
                        or not installed.decision_enabled):
                    raise PolicyCompilationError("Program cutover adds an unsupported Operation")
                binding = FixedRouteBinding.model_validate({
                    "scope": installed.program.scope,
                    "api_revision": installed.program.api_revision,
                    "table_endpoint": installed.storage_endpoint,
                    "table_name": installed.storage_table,
                    "ledger_table_endpoint": config.settings.ledger_table_endpoint,
                    "ledger_table_name": config.settings.ledger_table_name,
                    "trust_binding_sha256": installed.program.trust_binding_sha256,
                    "program_sha256": installed.program.program_sha256,
                    "supported_profiles": installed.supported_profiles,
                    "image_limits": installed.image_limits, "pool_enabled": installed.pool_enabled,
                    "oauth_enabled": installed.oauth_enabled, "decision_enabled": True,
                    "observer_key_named_value": config.settings.apim_usage_observer_key_named_value,
                })
                original = parse_policy(fixed_operation(
                    name, binding, logger_id=eventhub_logger_id(parent),
                    usage_identity=usage_identity_expression(parent),
                ))
            else:
                original = parse_policy(selected[name])
            for guard in original.findall(".//set-variable[@name='routeDispatchReady']"):
                guard.set("value", ROUTE_DISPATCH_READY)
            if (component_digest(parse_policy(policy), normalize_text_defaults=False)
                    != component_digest(original, normalize_text_defaults=False)
                    or fixed_policy_digest(policy) != installed.operation_policies_sha256[name]):
                raise PolicyCompilationError("Program cutover changed protected operation behavior")
            selected[name] = policy
    if fixed_program_digest(
        parent_policy, selected,
        tuple((name, *contract) for name, contract in contracts.items()),
    ) != installed.program.program_sha256 or any(
        fixed_policy_digest(policy) != installed.operation_policies_sha256[name]
        for name, policy in selected.items()
    ):
        raise PolicyCompilationError("Program cutover complete artifact changed")


def register_verified_program_upgrade(
    repository: QueryRepository, configurations: ApimTargetClientConfigResolver,
    upgrade: FixedProgramUpgrade, expected_config_sha256: str,
    *, clock: Callable[[], int] = lambda: int(time()),
    parent_policy: str | None = None, expected_parent_etag: str | None = None,
    operation_policies: Mapping[str, str] | None = None,
    expected_operation_etags: Mapping[str, str | None] | None = None,
    original_parent_policy: str | None = None,
) -> None:
    from ..persistence.repository_support import (
        _fixed_program_upgrade_config,
        _registered_program_installation,
    )
    from ..services.route_publication import recorded_route_snapshot

    installed = upgrade.installation
    target_id = UUID(installed.program.scope.target_id)
    if ((parent_policy is None) != (expected_parent_etag is None)
            or expected_parent_etag is not None
            and re.fullmatch(r'"[A-Za-z0-9+/=]{1,160}"', expected_parent_etag) is None
            or bool(operation_policies) != bool(expected_operation_etags)
            or operation_policies and (parent_policy is None
                or set(operation_policies) != set(expected_operation_etags or {})
                or any(
                    name != "microsoft-decision" or not installed.decision_enabled
                    if value is None else re.fullmatch(r'"[A-Za-z0-9+/=]{1,160}"', value) is None
                    for name, value in (expected_operation_etags or {}).items()
                ))):
        raise PolicyCompilationError("Program cutover requires its exact native parent version")
    availability = cast("ModelAvailabilityRepository", repository)
    with availability.model_availability_proof_guard(target_id, wait=True):
        previous_row = repository.effective_gateway_publication(target_id)
        gateways = repository.get_gateway_profiles([target_id])
        if len(gateways) != 1 or gateways[0]["id"] != target_id or previous_row is None:
            raise PolicyCompilationError("Program upgrade Gateway is unavailable")
        _fixed_program_upgrade_config(
            previous_row, gateways[0].get("config") or {}, upgrade.model_dump(mode="json"),
            expected_config_sha256,
        )
        registered = (gateways[0].get("config") or {}).get("fixed_route_program_upgrade")
        needs_cutover = parent_policy is not None and registered != upgrade.model_dump(mode="json")
        previous = GatewayPublication.model_validate(previous_row)
        snapshot, head = recorded_route_snapshot(previous)
        config = configurations.resolve(target_id, include_probe_credentials=True)
        if (
            config.gateway_profile_id != target_id
            or not route_target_identity_matches(config, installed.target_identity_sha256)
            or config.settings.apim_api_id != installed.program.scope.api_id
            or str(config.settings.apim_gateway_url or "").rstrip("/") != installed.gateway_endpoint
            or config.settings.apim_probe_subscription_id != installed.publisher_subscription_id
            or config.settings.apim_probe_subscription_key is None
        ):
            raise PolicyCompilationError("Program upgrade target binding changed")
        probe_key = config.settings.apim_probe_subscription_key
        def verify_native() -> None:
            with httpx.Client(timeout=10.0, follow_redirects=False) as client:
                management = AzureApimPublisherClient(config.settings, client=client)
                verifier = FixedProgramVerifier(
                    installed, management, probe_key, client, clock=clock,
                )
                projection = TableRouteProjection(
                    installed.storage_endpoint, installed.storage_table,
                    installed.program.scope, client=client,
                )
                prepared_proof = None
                if needs_cutover:
                    original_installation = FixedProgramInstallation.model_validate_json(
                        canonical_document(cast(
                            dict[str, Any], previous.resource_manifest["fixed_route_program"],
                        )),
                    )
                    previous_installation = _registered_program_installation(
                        str(previous.id), original_installation, gateways[0].get("config") or {},
                    )
                    assert parent_policy is not None
                    if fixed_policy_digest(parent_policy) != installed.parent_policy_sha256:
                        raise PolicyCompilationError("Program cutover parent bytes changed")
                    path = "/apis/" + quote(installed.program.scope.api_id, safe="")
                    path += ";rev=" + quote(installed.program.api_revision, safe="")
                    path += "/policies/policy"
                    native_response, native_parent = management._read_policy(path)
                    if (native_response.status_code != 200
                            or native_response.headers.get("ETag") != expected_parent_etag):
                        raise PolicyCompilationError(
                            "Program cutover native parent version changed",
                        )
                    native_digest = fixed_policy_digest(native_parent)
                    candidate_written = native_digest == installed.parent_policy_sha256
                    normalized = ET.tostring(ET.fromstring(parent_policy), encoding="unicode")
                    normalized_written = (
                        native_digest != installed.parent_policy_sha256
                        and native_digest == fixed_policy_digest(normalized)
                    )
                    enables_capability = (
                        installed.pool_enabled and not previous_installation.pool_enabled
                        or installed.router_enabled and not previous_installation.router_enabled
                        or installed.decision_enabled and not previous_installation.decision_enabled
                        or installed.image_limits is not None
                        and previous_installation.image_limits is None
                    )
                    rewrites_operations = (
                        enables_capability or original_parent_policy is not None
                    ) and bool(operation_policies)
                    native_operations = management.route_program_operations(
                        previous_installation.program.api_revision,
                    ) if rewrites_operations else {}
                    written_operations = any(
                        fixed_policy_digest(value[2]) == (
                            installed.operation_policies_sha256.get(name)
                        )
                        and installed.operation_policies_sha256.get(name)
                        != previous_installation.operation_policies_sha256.get(name)
                        for name, value in native_operations.items()
                    )
                    recovering = candidate_written or normalized_written or written_operations
                    if not (candidate_written or normalized_written) and native_digest != (
                        previous_installation.parent_policy_sha256
                    ):
                        raise PolicyCompilationError(
                            "Program cutover native parent is not an approved candidate",
                        )
                    if recovering and operation_policies and not rewrites_operations:
                        raise PolicyCompilationError(
                            "Program cutover partial Operations require explicit recovery",
                        )
                    previous_verifier = FixedProgramVerifier(
                        previous_installation, management, probe_key,
                        client, clock=clock,
                    )
                    if not recovering:
                        previous_verifier.verify()
                    candidate_proof = None
                    try:
                        old_proof = projection.read_program_proof(previous_installation.program)
                    except RouteProjectionConflict as error:
                        if not recovering or str(error) != "ROUTE_PROGRAM_PROOF_IDENTITY_CHANGED":
                            raise
                        candidate_proof = projection.read_program_proof(installed.program)
                        old_proof = candidate_proof
                    instant = clock()
                    if (old_proof is None or not old_proof[0].issued_epoch <= instant
                            or not recovering and old_proof[0].expires_epoch <= instant + 60):
                        raise PolicyCompilationError(
                            "Program cutover previous proof window is too short",
                        )
                    snapshot.require_valid(instant)
                    old_head = projection.read_active()
                    if old_head is None or old_head[0] != head:
                        raise PolicyCompilationError("Program cutover legacy route head changed")
                    _validate_program_cutover(
                        parent_policy, previous_installation, installed, config, management,
                        operation_policies,
                        previous_publication_id=upgrade.previous_publication_id,
                        original_parent_policy=original_parent_policy,
                    )
                    observed_parent = management._request("HEAD", path)
                    if (observed_parent.status_code != 200
                            or observed_parent.headers.get("ETag") != expected_parent_etag):
                        raise PolicyCompilationError(
                            "Program cutover native parent version changed",
                        )
                    operation_paths: dict[str, str] = {}
                    for name in operation_policies or {}:
                        operation_path = path.removesuffix("/policies/policy")
                        operation_path += "/operations/" + quote(name, safe="") + "/policies/policy"
                        version = (expected_operation_etags or {})[name]
                        response = management._request(
                            "HEAD", operation_path.removesuffix("/policies/policy")
                            if version is None else operation_path, allow_not_found=version is None,
                        ) if version is None else management._request("HEAD", operation_path)
                        if ((version is None and response.status_code != 404)
                                or version is not None and (response.status_code != 200
                                    or response.headers.get("ETag") != version)):
                            raise PolicyCompilationError(
                                "Program cutover operation version changed",
                            )
                        operation_paths[name] = operation_path
                    adds_operation = any(version is None for version in (
                        expected_operation_etags or {}
                    ).values())
                    if adds_operation:
                        management._request(
                            "PUT", path,
                            json_body={"properties": {"format": "xml", "value": encode_policy_xml(
                                parse_policy(parent_policy),
                            )}},
                            extra_headers={"If-Match": cast(str, expected_parent_etag)},
                        )
                        readback = management._request("GET", path + "?format=xml")
                        if (readback.status_code != 200 or fixed_policy_digest(
                            management._policy_response_value(readback),
                        ) != installed.parent_policy_sha256):
                            raise PolicyCompilationError(
                                "Decision admission parent readback changed",
                            )
                    for name, policy in (operation_policies or {}).items():
                        version = (expected_operation_etags or {})[name]
                        if version is None:
                            from ..integrations.apim_control_plane_contract import OperationResource

                            _, template = installed.operation_contracts[name]
                            management.ensure_operation(
                                installed.program.api_revision,
                                OperationResource(id=name, display_name="Decision", method="POST",
                                                  path=template), create_only=True,
                            )
                            management.put_operation_policy(
                                installed.program.api_revision, name, policy, create_only=True,
                            )
                            continue
                        management._request(
                            "PUT", operation_paths[name],
                            json_body={"properties": {"format": "xml", "value": encode_policy_xml(
                                parse_policy(policy),
                            )}},
                            extra_headers={"If-Match": version},
                        )
                    if not adds_operation and not candidate_written:
                        management._request(
                            "PUT", path,
                            json_body={"properties": {"format": "xml", "value": encode_policy_xml(
                                parse_policy(parent_policy),
                            )}},
                            extra_headers={"If-Match": cast(str, expected_parent_etag)},
                        )
                    prepared_proof = verifier.verify()
                    if candidate_proof is None:
                        projection.publish_upgraded_program_proof(
                            upgrade, previous_installation, prepared_proof, old_proof[1], clock(),
                        )
                    elif candidate_proof[0].expires_epoch <= clock():
                        projection.publish_program_proof(
                            prepared_proof, installed.program, candidate_proof[1], clock(),
                        )
                if prepared_proof is None:
                    verifier.verify()
                proof = projection.read_program_proof(installed.program)
                now = clock()
                if (proof is None or not proof[0].issued_epoch <= now < proof[0].expires_epoch
                        or not set(head.approval.required_profiles)
                        <= set(proof[0].supported_profiles)):
                    raise PolicyCompilationError("Program upgrade current proof is unavailable")
                snapshot.require_valid(now)
                observed = projection.read_active()
                if observed is None or observed[0] != head:
                    raise PolicyCompilationError("Program upgrade legacy route head changed")
                verifier.confirm(head)
                if projection.read_active() != observed or clock() >= proof[0].expires_epoch:
                    raise PolicyCompilationError("Program upgrade changed during verification")

        if needs_cutover:
            repository.record_fixed_program_upgrade(
                upgrade.model_dump(mode="json"), expected_config_sha256,
                before_record=verify_native,
            )
        else:
            verify_native()
            repository.record_fixed_program_upgrade(
                upgrade.model_dump(mode="json"), expected_config_sha256,
            )


def _default_route_clock() -> int:
    return int(time())


_PROGRAM_RENEW_BACKOFF_SECONDS = 300.0
_PROGRAM_RENEW_BACKOFF_LOCK = Lock()
_PROGRAM_RENEW_BACKOFF_UNTIL: dict[tuple[UUID, str], float] = {}
# Releases that absorb the target's active model additions into one route set.
_FULL_ROUTE_RELEASES = frozenset({
    PublicationKind.ROUTE_RECONCILE, PublicationKind.MODEL_REMOVE,
    PublicationKind.CREDENTIAL_ROTATION,
})


class FixedRoutePublisherFactory:
    def __init__(
        self, repository: QueryRepository, configurations: ApimTargetClientConfigResolver,
        *, active_fence: Callable[[UUID], Mapping[str, Any]],
        availability_projector: Callable[[UUID], ModelAvailabilityState],
        clock: Callable[[], int] = _default_route_clock,
        cipher: CredentialCipher | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._repository = repository
        self._configurations = configurations
        self._active_fence = active_fence
        self._availability_projector = availability_projector
        self._clock = clock
        self._cipher = cipher
        self._client = client
        self._tokens = ManagedIdentityTokenProvider(client)
        self._verified_connections: set[str] = set()

    def installed(
        self, target_id: UUID, base_publication_id: UUID,
        *, migration: FixedProgramMigration | None = None,
    ) -> tuple[FixedProgramInstallation, ApimTargetClientConfig]:
        effective = self._repository.effective_gateway_publication(target_id)
        if (
            effective is None or str(effective["id"]) != str(base_publication_id)
            or str(effective["gateway_profile_id"]) != str(target_id)
            or effective["status"] != "active"
        ):
            raise PolicyCompilationError("Fixed route effective publication changed")
        raw = effective.get("resource_manifest", {}).get("fixed_route_program")
        if migration is not None:
            previous = GatewayPublication.model_validate(effective)
            migration.require_previous(previous)
            if raw is not None:
                raise PolicyCompilationError("Fixed route initial migration is already installed")
            raw = migration.installation.model_dump(mode="json")
        if migration is None:
            gateways = self._repository.get_gateway_profiles([target_id])
            if len(gateways) != 1 or gateways[0]["id"] != target_id:
                raise PolicyCompilationError("Fixed route program Gateway is unavailable")
            initialization = (gateways[0].get("config") or {}).get(
                "managed_route_program_initialization",
            )
            if initialization is not None:
                initialized = ManagedProgramInitialization.model_validate_json(
                    canonical_document(initialization),
                )
                initialized.require_previous(GatewayPublication.model_validate(effective))
                raw = initialized.installation.model_dump(mode="json")
        if not isinstance(raw, dict):
            raise PolicyCompilationError("Fixed route program installation is unavailable")
        installation = FixedProgramInstallation.model_validate_json(canonical_document(raw))
        if migration is None:
            upgrade_record = (gateways[0].get("config") or {}).get("fixed_route_program_upgrade")
            if upgrade_record is not None:
                from ..persistence.repository_support import _registered_program_installation

                installation = _registered_program_installation(
                    str(base_publication_id), installation, gateways[0].get("config") or {},
                )
        config = self._configurations.resolve(target_id, include_probe_credentials=True)
        if (
            installation.program.scope.target_id != str(target_id)
            or config.gateway_profile_id != target_id
            or not route_target_identity_matches(config, installation.target_identity_sha256)
            or config.settings.apim_api_id != installation.program.scope.api_id
            or str(config.settings.apim_gateway_url or "").rstrip("/")
            != installation.gateway_endpoint
            or config.settings.apim_probe_subscription_id != installation.publisher_subscription_id
            or config.settings.apim_probe_subscription_key is None
        ):
            raise PolicyCompilationError("Fixed route program target binding changed")
        return installation, config

    def bound_gateway_programs(
        self, installed: FixedProgramInstallation, deployment: Mapping[str, Any],
        enrollments: Sequence[ModelAvailabilityNodeEnrollment] = (),
    ) -> tuple[FixedProgramInstallation, ...]:
        if (deployment.get("retired_at") is not None
                or str(deployment.get("apim_target_id")) != installed.program.scope.target_id
                or deployment.get("declared_state") != "present"
                or deployment.get("kind") != "self_hosted"
                or not deployment.get("endpoint_url")
                or not deployment.get("native_gateway_id")):
            raise PolicyCompilationError("Self-hosted Gateway binding is unavailable")
        nodes = [item for item in enrollments if str(item.deployment_id) == str(deployment["id"])]
        if nodes:
            if len(nodes) != 1:
                raise PolicyCompilationError("Self-hosted Gateway binding is ambiguous")
            return native_gateway_programs(installed, deployment, nodes[0])
        config = self._configurations.resolve(
            UUID(installed.program.scope.target_id), include_probe_credentials=True,
        )
        if config.settings.apim_probe_subscription_key is None:
            raise PolicyCompilationError("Self-hosted Gateway probe credential is unavailable")
        context = nullcontext(self._client) if self._client is not None else httpx.Client(
            timeout=10.0, follow_redirects=False,
        )
        with context as client:
            management = AzureApimPublisherClient(
                config.settings, token_provider=self._tokens, client=client,
            )
            return (FixedProgramVerifier.native_installation(
                installed, str(deployment["native_gateway_id"]), str(deployment["endpoint_url"]),
                management, config.settings.apim_probe_subscription_key, client,
            ),)

    def serving_programs(
        self, publication: GatewayPublication,
    ) -> tuple[FixedProgramInstallation, ...]:
        if publication.base_release_id is None:
            raise PolicyCompilationError("Serving programs require a current fixed baseline")
        installed, _ = self.installed(publication.gateway_profile_id, publication.base_release_id)
        deployments = self._repository.list_gateway_deployments(publication.gateway_profile_id)
        selected = {
            item.deployment_id: item
            for binding in publication.desired_spec.bindings
            for item in binding.serving_deployments or ()
        }
        programs: list[FixedProgramInstallation] = []
        availability = cast("ModelAvailabilityRepository", self._repository)
        enrollments = availability.list_model_availability_node_enrollments(
            publication.gateway_profile_id,
        ) if any(item.native_gateway_id != "managed" for item in selected.values()) else ()
        for deployment_id, snapshot in sorted(selected.items(), key=lambda value: str(value[0])):
            matches = [row for row in deployments if str(row.get("id")) == str(deployment_id)]
            if len(matches) != 1:
                raise PolicyCompilationError("Serving Gateway deployment is unavailable")
            deployment = matches[0]
            if (
                deployment.get("retired_at") is not None
                or deployment.get("native_gateway_id") != snapshot.native_gateway_id
                or str(deployment.get("endpoint_url", "")).rstrip("/")
                != str(snapshot.endpoint_url).rstrip("/")
            ):
                raise PolicyCompilationError("Serving Gateway deployment snapshot changed")
            if snapshot.native_gateway_id == "managed":
                programs.append(installed)
                continue
            programs.extend(self.bound_gateway_programs(installed, deployment, enrollments))
        return tuple(programs) if selected else (installed,)

    def promote_program(
        self, publication: GatewayPublication, task: RoutePublicationTask,
        worker_id: str, heartbeat: Callable[[], None],
    ) -> None:
        row = self._repository.get_gateway_publication(publication.id)
        previous_row = self._repository.get_gateway_publication(publication.base_release_id) \
            if publication.base_release_id is not None else None
        if row is None or previous_row is None:
            raise PolicyCompilationError("Fixed program migration publication is unavailable")
        current = GatewayPublication.model_validate(row)
        previous = GatewayPublication.model_validate(previous_row)
        raw = current.resource_manifest.get("fixed_route_migration")
        if not isinstance(raw, dict) or current.status.value != "promoting":
            raise PolicyCompilationError("Fixed program migration promotion phase changed")
        migration = FixedProgramMigration.model_validate_json(canonical_document(raw))
        migration.require_candidate(current, previous)
        task.require_publication(str(current.id), str(previous.id), current.desired_spec_sha256,
                                 str(current.gateway_profile_id))
        installation, config = self.installed(current.gateway_profile_id, previous.id,
                                              migration=migration)
        activation = current.resource_manifest.get("route_activation")
        if not isinstance(activation, dict) or activation.get("outcome") != "confirmed":
            raise PolicyCompilationError("Fixed program promotion requires confirmed configuration")
        active_head = RouteActivationIntent.from_record(activation["intent"]).head
        active_head.approval.require_same_configuration(task.plan.snapshot.approval)
        context = document_digest({"migration": raw, "task": task.context_sha256,
                                   "publication_id": str(current.id)})
        recorded = current.resource_manifest.get("route_program_promotion")
        if recorded is not None and not isinstance(recorded, dict):
            raise PolicyCompilationError("Fixed program promotion checkpoint is invalid")

        def persist(expected: Mapping[str, Any] | None, value: Mapping[str, Any]) -> None:
            heartbeat()
            if not self._repository.checkpoint_gateway_program_promotion(
                current.id, worker_id, expected, value,
            ):
                raise RetryablePublicationError("Fixed program promotion lease changed")

        with self._open(installation, config) as (port, management):
            def verify(expected_revision: str) -> None:
                if config.settings.apim_probe_subscription_key is None:
                    raise PolicyCompilationError("Fixed program migration credential unavailable")
                verifier = FixedProgramVerifier(
                    installation,
                    management,
                    config.settings.apim_probe_subscription_key,
                    port._client,
                    clock=self._clock,
                    expected_current_revision=expected_revision,
                )
                proof = verifier.verify()
                observed = port.projection.read_active()
                if observed is None or observed[0] != active_head:
                    raise PolicyCompilationError("Fixed program promotion configuration changed")
                previous_proof = port.projection.read_program_proof(installation.program)
                port.projection.publish_program_proof(
                    proof, installation.program, previous_proof[1] if previous_proof else None,
                    self._clock(),
                )
                verifier.confirm(active_head)
                heartbeat()

            promote_program_once(
                management, migration.previous_revision, installation.program.api_revision,
                "fixed-program-" + str(migration.request_id), context, recorded, persist, verify,
            )

    def verify_availability(
        self, target_id: UUID, inventory_digest: str,
        projected: ModelAvailabilityState | None = None,
        before_proof: Callable[[], None] | None = None, *, renew: bool = False,
        scope_installation: FixedProgramInstallation | None = None,
    ) -> ModelAvailabilityCoverage:
        from ..domain.model_availability import ModelAvailabilityStagedProgram
        from ..persistence.repository_support import _managed_program_publication
        from ..services.route_publication import recorded_route_scopes, recorded_route_snapshot

        availability = cast("ModelAvailabilityRepository", self._repository)
        source = self._repository.effective_gateway_publication(target_id)
        if source is None or availability.get_model_availability_inventory().get(target_id) != (
            inventory_digest
        ):
            raise PolicyCompilationError("Fixed route availability inventory changed")
        gateways = self._repository.get_gateway_profiles([target_id]) if (
            "fixed_route_program" not in source.get("resource_manifest", {})
        ) else []
        gateway_config = gateways[0].get("config") or {} if (
            len(gateways) == 1 and gateways[0]["id"] == target_id
        ) else {}
        initialized_record = gateway_config.get("managed_route_program_initialization")
        publication = GatewayPublication.model_validate(
            _managed_program_publication(source, gateway_config),
        )
        installation, config = self.installed(target_id, publication.id)
        if installation.native_gateway_scope is True and scope_installation is None:
            return self._verify_native_scope_availability(
                target_id, inventory_digest, projected, before_proof, renew=renew,
            )
        base_scope = installation.program.scope
        if scope_installation is not None:
            if (
                scope_installation.program.program_sha256 != installation.program.program_sha256
                or scope_installation.target_identity_sha256 != installation.target_identity_sha256
            ):
                raise PolicyCompilationError("Native scope availability program changed")
            installation = scope_installation
        snapshot, head = recorded_route_snapshot(publication)
        if installation.program.scope != base_scope:
            with self._open(installation, config) as (port, _):
                native_head = port.projection.read_active()
            if native_head is None or native_head[0].approval.entry_count != 0:
                raise PolicyCompilationError("Native scope empty program baseline is unavailable")
            head = native_head[0]
            snapshot = RouteSnapshot(approval=head.approval, routes=(), members=())
        addition_rows = self._repository.active_model_route_publications(target_id) \
            if installation.model_additions_enabled else []
        additions = [GatewayPublication.model_validate(row) for row in addition_rows]
        addition_digests = [document_digest(item.model_dump(mode="json")) for item in additions]
        routes = list(snapshot.routes)
        aliases = {route.alias.casefold() for route in routes}
        model_heads: dict[str, ModelRouteHead] = {}
        configurations: dict[str, str] = {}
        for item in additions:
            if scope_installation and installation.program.scope not in recorded_route_scopes(item):
                continue
            if scope_installation is None:
                own_snapshot, own_head = recorded_route_snapshot(item)
            else:
                own_snapshot, own_head = recorded_route_snapshot(
                    item, scope=installation.program.scope,
                )
            if scope_installation and own_head.approval.scope != installation.program.scope:
                continue
            if (item.gateway_profile_id != target_id or item.status.value != "active"
                    or item.publication_kind.value != "model_add" or len(own_snapshot.routes) != 1
                    or own_head.approval.required_program_sha256 not in {
                        installation.program.program_sha256, installation.legacy_program_sha256,
                        *installation.predecessor_program_sha256,
                    }):
                raise PolicyCompilationError("Model route availability publication changed")
            route = own_snapshot.routes[0]
            if route.alias.casefold() in aliases:
                raise PolicyCompilationError("Model route availability alias is duplicated")
            aliases.add(route.alias.casefold())
            own_snapshot.require_valid(own_head.activated_epoch)
            model_heads[route.alias] = ModelRouteHead(
                publication_id=str(item.id), alias=route.alias, model_id=route.model_id,
                display_name=route.display_name, created_at=route.created_at,
                content_sha256=route.sha256, head=own_head,
            )
            configurations[str(item.id)] = own_snapshot.configuration_sha256
            routes.append(route)
        if len(routes) > 10000:
            raise PolicyCompilationError("Model route availability catalog exceeds its bound")
        deployments = self._repository.list_gateway_deployments(target_id, include_retired=True)
        if initialized_record is None and any(
            route.scope != installation.program.scope for route in routes
        ):
            raise PolicyCompilationError("Fixed route availability program scope changed")
        if initialized_record is None and any(
            deployment.native_gateway_id != installation.program.scope.gateway_id
            for current in (publication, *additions)
            if scope_installation is None or (
                current.id == publication.id and installation.program.scope == base_scope
                or current.id != publication.id
                and installation.program.scope in recorded_route_scopes(current)
            )
            for binding in current.desired_spec.bindings
            for deployment in binding.serving_deployments or ()
            if not scope_installation or deployment.native_gateway_id
            == installation.program.scope.gateway_id
        ):
            raise PolicyCompilationError(
                "Fixed route availability serving deployment is unverified",
            )
        active = [row for row in deployments if row.get("retired_at") is None
                  and row.get("native_gateway_id") == installation.program.scope.gateway_id]
        if (len(active) != 1
            or scope_installation is None and active[0].get("native_gateway_id") != "managed"
            or installation.program.scope.gateway_id == "managed"
            and str(active[0].get("endpoint_url", "")).rstrip("/")
            != installation.gateway_endpoint or not config.settings.ledger_table_endpoint
            or not config.settings.ledger_table_name):
            raise PolicyCompilationError("Fixed route availability requires one Managed deployment")
        identities = {UUID(route.model_id) for route in routes}
        if projected is not None:
            identities.add(projected.model_id)
        ordered = sorted(identities, key=str)
        states = availability.get_model_availability_states(ordered)
        projected_matches = projected is None or states.get(projected.model_id) == projected
        if set(states) != identities or not projected_matches:
            raise PolicyCompilationError("Fixed route availability source changed")
        legacy_coverage: ModelAvailabilityCoverage | None = None
        with (
            ExitStack() as proofs,
            self._open(installation, config) as (port, management),
            TableStorageLedger(
                config.settings.ledger_table_endpoint, config.settings.ledger_table_name,
                token_provider=self._tokens, timeout=2,
            ) as store,
        ):
            acquired = False

            def acquire_proof() -> None:
                nonlocal acquired
                if not acquired:
                    if before_proof is not None:
                        before_proof()
                    else:
                        proofs.enter_context(availability.model_availability_proof_guard(
                            target_id, wait=not renew,
                        ))
                    acquired = True

            if initialized_record is not None:
                initialized = ManagedProgramInitialization.model_validate_json(
                    canonical_document(initialized_record),
                )
                if renew:
                    acquire_proof()
                    legacy_coverage = management.renew_model_availability(
                        target_id, inventory_digest, deployments,
                        config.settings.ledger_table_endpoint, config.settings.ledger_table_name,
                        availability.get_model_availability_states,
                        node_enrollment_reader=lambda: (
                            availability.list_model_availability_node_enrollments(target_id)
                        ),
                    )
                else:
                    legacy_coverage = management.verify_model_availability(
                        target_id, inventory_digest, deployments,
                        config.settings.ledger_table_endpoint, config.settings.ledger_table_name,
                        availability.get_model_availability_state,
                        state_batch_reader=availability.get_model_availability_states,
                        node_enrollment_reader=lambda: (
                            availability.list_model_availability_node_enrollments(target_id)
                        ),
                        before_proof=acquire_proof,
                        staged_programs=(ModelAvailabilityStagedProgram(
                            installation=installation,
                            expected_current_revision=initialized.previous_revision,
                        ),),
                    )
            acquire_proof()
            proof = port._verifier.verify()
            started = self._clock()
            port.projection.require_current_connections(snapshot, installation.program, started)
            observed_head = port.projection.read_active()
            if observed_head is None or observed_head[0] != head:
                raise PolicyCompilationError("Fixed route availability head changed")
            observed_models = {}
            for alias, model_head in model_heads.items():
                model_observation = port.projection.read_model_active(alias)
                if model_observation is None or model_observation[0] != model_head:
                    raise PolicyCompilationError("Model route availability head changed")
                observed_models[alias] = model_observation
                source_snapshot, _ = recorded_route_snapshot(next(
                    item for item in additions if str(item.id) == model_head.publication_id
                ))
                port.projection.require_current_connections(
                    source_snapshot, installation.program, started,
                )
            for model_id in ordered:
                if self._clock() >= min(started + 60, proof.expires_epoch):
                    raise PolicyCompilationError("Fixed route availability verification expired")
                observed = store.read_versioned_entity(AVAILABILITY_PARTITION, str(model_id))
                actual_state = (
                    ModelAvailabilityState.from_entity(observed[0], model_id) if observed else None
                )
                if actual_state != states[model_id]:
                    logging.getLogger(__name__).warning(
                        "Fixed route availability projection mismatch target=%s model=%s "
                        "desired=%s observed=%s",
                        target_id, model_id, states[model_id].model_dump(mode="json"),
                        actual_state.model_dump(mode="json") if actual_state else None,
                    )
                    raise PolicyCompilationError("Fixed route availability projection changed")
            expected = [
                {name: value for name, value in catalog_entry(route).items()
                 if name != "logical_model_id"}
                for route in routes if states[UUID(route.model_id)].enabled
            ]
            previous_proof = port.projection.read_program_proof(installation.program)
            port.projection.publish_program_proof(
                proof, installation.program, previous_proof[1] if previous_proof else None,
                self._clock(),
            )
            actual = port.read_catalog()
            if sorted(actual, key=lambda item: item["id"]) != sorted(
                expected, key=lambda item: item["id"],
            ):
                raise PolicyCompilationError("Fixed route availability catalog changed")
            port._verifier.confirm(head)
            for model_head in model_heads.values():
                port._verifier.confirm_model(model_head)
            if any(port.projection.read_model_active(alias) != observed
                   for alias, observed in observed_models.items()):
                raise PolicyCompilationError("Model route availability changed during verification")
            if installation.model_additions_enabled and addition_digests != [
                document_digest(GatewayPublication.model_validate(row).model_dump(mode="json"))
                for row in self._repository.active_model_route_publications(target_id)
            ]:
                raise PolicyCompilationError("Model route publications changed during verification")
            if (
                port.projection.read_active() != observed_head
                or self._repository.effective_gateway_publication(target_id) != source
                or availability.get_model_availability_states(ordered) != states
                or availability.get_model_availability_inventory().get(target_id)
                != inventory_digest
            ):
                raise PolicyCompilationError("Fixed route availability changed during verification")
        finished = self._clock()
        if not started <= finished < min(started + 60, proof.expires_epoch):
            raise PolicyCompilationError("Fixed route availability verification expired")
        surfaces = tuple(sorted(
            installation.program.scope.gateway_id + "/" + installation.program.instance_id + "/"
            + installation.program.api_revision + "/" + name
            if scope_installation is not None else
            "managed/" + installation.program.api_revision + "/" + name
            for name in installation.operation_contracts
        ))
        coverage = ModelAvailabilityCoverage(
            target_id=target_id, inventory_digest=inventory_digest,
            policy_digest=document_digest({
                "program": installation.program.model_dump(mode="json"),
                "configuration": snapshot.configuration_sha256,
                **({"model_configurations": configurations} if configurations else {}),
            }),
            serving_surfaces=surfaces, verified_surfaces=surfaces,
            verified_at=datetime.fromtimestamp(finished, UTC),
            expires_at=datetime.fromtimestamp(proof.expires_epoch, UTC),
        )
        if legacy_coverage is not None:
            finished_at = datetime.now(UTC)
            if (finished_at.timestamp() >= min(started + 60, proof.expires_epoch)
                    or not legacy_coverage.current(finished_at, inventory_digest)):
                raise PolicyCompilationError("Managed initialization legacy coverage is expired")
            surfaces = tuple(sorted(set(coverage.serving_surfaces) | set(
                legacy_coverage.serving_surfaces,
            )))
            coverage = coverage.model_copy(update={
                "policy_digest": document_digest({
                    "managed": coverage.policy_digest, "legacy": legacy_coverage.policy_digest,
                }),
                "serving_surfaces": surfaces, "verified_surfaces": surfaces,
                "verified_at": finished_at,
                "expires_at": min(coverage.expires_at, legacy_coverage.expires_at),
            })
        return coverage

    def inspect_integrity(
        self, publication: GatewayPublication, checkpoint: object = None,
        *, maximum_reads: int = 16,
    ) -> GatewayReleaseDependencies:
        from ..services.route_publication import (
            RouteIntegrityCheckpoint,
            RouteIntegrityDeferred,
            recorded_route_dependencies,
            recorded_route_snapshot,
            require_route_specification,
        )

        if type(maximum_reads) is not int or not 1 <= maximum_reads <= 100:
            raise PolicyCompilationError("Fixed route integrity read limit is invalid")
        row = self._repository.get_gateway_publication(publication.id)
        if row is None or GatewayPublication.model_validate(row) != publication:
            raise PolicyCompilationError("Fixed route integrity source changed")
        current_row = self._repository.effective_gateway_publication(publication.gateway_profile_id)
        if current_row is None:
            raise PolicyCompilationError("Fixed route integrity effective release is unavailable")
        current = GatewayPublication.model_validate(current_row)
        installation, config = self.installed(publication.gateway_profile_id, current.id)
        dependencies = recorded_route_dependencies(publication)
        snapshot, head = recorded_route_snapshot(publication)
        _, current_head = recorded_route_snapshot(current)
        raw_installation = publication.resource_manifest.get("fixed_route_program")
        if not isinstance(raw_installation, dict):
            raise PolicyCompilationError("Fixed route integrity installation is unavailable")
        captured_installation = FixedProgramInstallation.model_validate_json(
            canonical_document(raw_installation),
        )
        if captured_installation != installation:
            if publication.id != current.id:
                raise PolicyCompilationError("Fixed route integrity program installation changed")
            FixedProgramUpgrade(
                previous_publication_id=str(current.id),
                previous_installation_sha256=document_digest(captured_installation),
                installation=installation,
            ).require_previous(str(publication.id), captured_installation)
        raw_task = publication.resource_manifest.get("route_configuration")
        task = RoutePublicationTask.model_validate_json(canonical_document(raw_task)) if (
            isinstance(raw_task, dict)
        ) else None
        if task is not None:
            require_route_specification(publication, task)
        previous = publication
        if publication.base_release_id is not None:
            previous_row = self._repository.get_gateway_publication(publication.base_release_id)
            if previous_row is None:
                raise PolicyCompilationError("Fixed route integrity baseline is unavailable")
            previous = GatewayPublication.model_validate(previous_row)
            if previous.id != publication.base_release_id:
                raise PolicyCompilationError("Fixed route integrity baseline changed")
        self._preflight_dependencies(publication, installation)
        with self._open(installation, config) as (port, management):
            proof = port._verifier.verify()
            observed = port.projection.read_active()
            if observed is None or observed[0] != current_head:
                raise PolicyCompilationError("Fixed route integrity active head changed")
            fence = self._active_fence(publication.gateway_profile_id)
            if (
                type(fence.get("protocol_version")) is not int or fence["protocol_version"] != 2
                or fence.get("mode") != "active"
                or fence.get("gateway_profile_id") != installation.program.scope.target_id
                or fence.get("epoch") != head.approval.budget_epoch
            ):
                raise PolicyCompilationError("Fixed route integrity budget fence changed")
            now = self._clock()
            head.require_ready(
                installation.program, proof, head.approval.budget_epoch, now,
                legacy_program_sha256=installation.legacy_program_sha256,
                predecessor_program_sha256=installation.predecessor_program_sha256,
            )
            snapshot.require_valid(now)
            budgets = self._budget_contracts(publication, previous)
            budgets.validate_publication(publication)
            if (
                str(budgets.epoch) != head.approval.budget_epoch
                or budgets.model_contracts != {
                    route.alias: route.budget for route in snapshot.routes
                }
            ):
                raise PolicyCompilationError("Fixed route integrity budget contracts changed")
            baseline = (
                RouteSnapshot(
                    approval=task.previous_head.approval, routes=task.previous_routes,
                    members=task.previous_members,
                ) if task is not None and task.previous_head is not None else snapshot
            )
            dependency_source, baseline = self._rollback_source(
                publication, previous, installation, baseline,
            )
            references = self._credential_references(publication, dependency_source, installation)
            approvals = self.approved_connections(
                publication, captured_installation, management,
                baseline=baseline, verify_readback=False,
            )
            if set(approvals) != {route.alias for route in snapshot.routes} or any(
                approvals[route.alias].connection != route.connection for route in snapshot.routes
            ):
                raise PolicyCompilationError("Fixed route integrity connections changed")
            connections = sorted(
                {route.connection.revision_id: route.connection
                 for route in snapshot.routes}.values(),
                key=lambda connection: connection.revision_id,
            )
            credentials = sorted(references)
            context = document_digest({
                "publication": publication.model_dump(mode="json"),
                "current_id": str(current.id), "current_head": current_head.model_dump(mode="json"),
                "budgets": budgets.model_dump(mode="json"),
                "approval_sources": {
                    alias: approval.model_dump(mode="json", exclude={
                        "approval_id", "issued_epoch", "expires_epoch",
                    }) for alias, approval in approvals.items()
                },
                "credentials": credentials,
            })
            progress = (
                RouteIntegrityCheckpoint.model_validate(checkpoint) if checkpoint is not None
                else RouteIntegrityCheckpoint(context_sha256=context)
            )
            total = 3 * len(snapshot.routes)
            if progress.context_sha256 != context:
                raise PolicyCompilationError("Fixed route integrity context changed")
            dependencies_observed = bool(
                progress.connections or progress.connection_resources or progress.credentials
            )
            if (
                progress.entities > total or progress.connections > len(connections)
                or progress.credentials > len(credentials)
                or (progress.connections or progress.connection_resources or progress.credentials)
                and progress.entities != total
                or progress.credentials and progress.connections != len(connections)
                or progress.connection_resources and progress.connections == len(connections)
                or dependencies_observed != (progress.dependencies_started_epoch is not None)
            ):
                raise PolicyCompilationError("Fixed route integrity progress is invalid")
            if progress.connection_resources:
                pool = connections[progress.connections].pool
                if pool is None or progress.connection_resources >= (
                    1 + len(pool.members) + len(pool.credential_versions) + len(pool.header_values)
                ):
                    raise PolicyCompilationError("Fixed route Pool integrity progress is invalid")
            if progress.dependencies_started_epoch is not None and not (
                progress.dependencies_started_epoch <= now
                < progress.dependencies_started_epoch + 120
            ):
                raise PolicyCompilationError("Fixed route integrity dependency evidence expired")
            remaining = min(maximum_reads, max(0, min(60, proof.expires_epoch - now - 10) // 10))
            if progress.entities < total and remaining:
                end = port.projection.verify_slice(
                    snapshot, now, progress.entities, maximum=remaining,
                )
                if type(end) is not int or not (
                    progress.entities < end <= min(total, progress.entities + remaining)
                ):
                    raise PolicyCompilationError("Fixed route integrity returned an invalid cursor")
                remaining -= end - progress.entities
                progress = progress.model_copy(update={"entities": end})
            for connection in connections[progress.connections:]:
                if progress.entities != total or not remaining or self._clock() >= now + 60:
                    break
                started = progress.dependencies_started_epoch
                if connection.pool is not None:
                    resources = 1 + len(connection.pool.members) + len(
                        connection.pool.credential_versions,
                    ) + len(connection.pool.header_values)
                    cursor = progress.connection_resources
                    end = management.verify_route_pool_slice(connection, cursor, maximum=remaining)
                    if type(end) is not int or not (
                        cursor < end <= min(resources, cursor + remaining)
                    ):
                        raise PolicyCompilationError("Fixed route Pool integrity cursor changed")
                    completed = end == resources
                    remaining -= end - cursor
                else:
                    observed_dependency = management.verify_route_connection(connection)
                    if observed_dependency != connection.dependency_sha256:
                        raise PolicyCompilationError("Fixed route integrity dependency changed")
                    completed = True
                    remaining -= 1
                progress = progress.model_copy(update={
                    "connections": progress.connections + int(completed),
                    "connection_resources": 0 if completed else end,
                    "dependencies_started_epoch": now if started is None else started,
                })
                if not completed:
                    break
            for secret_id in credentials[progress.credentials:]:
                if (progress.connections != len(connections) or not remaining
                        or self._clock() >= now + 60):
                    break
                started = progress.dependencies_started_epoch
                self._verify_credential_references({secret_id: references[secret_id]})
                progress = progress.model_copy(update={
                    "credentials": progress.credentials + 1,
                    "dependencies_started_epoch": now if started is None else started,
                })
                remaining -= 1
            port._verifier.confirm(current_head)
            latest = self._repository.effective_gateway_publication(publication.gateway_profile_id)
            if (
                latest is None or GatewayPublication.model_validate(latest) != current
                or port.projection.read_active() != observed
            ):
                raise PolicyCompilationError("Fixed route integrity changed during verification")
            checked_at = self._clock()
            head.require_ready(
                installation.program, proof, head.approval.budget_epoch, checked_at,
                legacy_program_sha256=installation.legacy_program_sha256,
                predecessor_program_sha256=installation.predecessor_program_sha256,
            )
            snapshot.require_valid(checked_at)
            if progress.dependencies_started_epoch is not None and not (
                progress.dependencies_started_epoch <= checked_at
                < progress.dependencies_started_epoch + 120
            ):
                raise PolicyCompilationError("Fixed route integrity dependency evidence expired")
            if (progress.entities, progress.connections, progress.credentials) != (
                total, len(connections), len(credentials),
            ):
                raise RouteIntegrityDeferred(progress)
        return dependencies.model_copy(update={
            "live_status": "healthy", "live_checked_at": datetime.fromtimestamp(checked_at, UTC),
        })

    def _verify_native_scope_availability(
        self, target_id: UUID, inventory_digest: str, projected: ModelAvailabilityState | None,
        before_proof: Callable[[], None] | None, *, renew: bool,
    ) -> ModelAvailabilityCoverage:
        from ..domain.control_plane import GatewayReleaseSpec
        from ..services.route_publication import recorded_route_scopes

        availability = cast("ModelAvailabilityRepository", self._repository)
        source = self._repository.effective_gateway_publication(target_id)
        if source is None:
            raise PolicyCompilationError("Native scope fixed baseline is unavailable")
        installed, _ = self.installed(target_id, UUID(str(source["id"])))
        enrollments = availability.list_model_availability_node_enrollments(target_id)
        deployments = self._repository.list_gateway_deployments(target_id)
        specification = GatewayReleaseSpec.model_validate(source["desired_spec"])
        required_scopes = {installed.program.scope.gateway_id}
        required_scopes.update(
            snapshot.native_gateway_id for binding in specification.bindings
            for snapshot in binding.serving_deployments
        )
        for row in self._repository.active_model_route_publications(target_id):
            publication = GatewayPublication.model_validate(row)
            if publication.gateway_profile_id != target_id or publication.status.value != "active":
                raise PolicyCompilationError("Published model scope source changed")
            required_scopes.update(scope.gateway_id for scope in recorded_route_scopes(publication))
        programs = [installed]
        for deployment in deployments:
            if (deployment.get("native_gateway_id") not in required_scopes
                    or deployment.get("kind") != "self_hosted"):
                continue
            programs.extend(self.bound_gateway_programs(installed, deployment, enrollments))
        native_ids = {program.program.scope.gateway_id for program in programs}
        if not required_scopes <= native_ids:
            raise PolicyCompilationError("Published model scope has no current Gateway binding")
        coverage: list[ModelAvailabilityCoverage] = []
        guard = nullcontext() if before_proof is not None else (
            availability.model_availability_proof_guard(target_id, wait=not renew)
        )
        with guard:
            if before_proof is not None:
                before_proof()
            for program in programs:
                coverage.append(self.verify_availability(
                    target_id, inventory_digest, projected, lambda: None,
                    renew=renew, scope_installation=program,
                ))
            if (
                availability.list_model_availability_node_enrollments(target_id) != enrollments
                or self._repository.effective_gateway_publication(target_id) != source
                or availability.get_model_availability_inventory().get(target_id)
                != inventory_digest
            ):
                raise PolicyCompilationError("Native scope coverage changed during verification")
        finished = datetime.fromtimestamp(self._clock(), UTC)
        expires = min(item.expires_at for item in coverage)
        if finished >= expires:
            raise RetryablePublicationError(
                "Native scope coverage expired before final confirmation",
            )
        surfaces = tuple(sorted(surface for item in coverage for surface in item.serving_surfaces))
        return ModelAvailabilityCoverage(
            target_id=target_id, inventory_digest=inventory_digest,
            policy_digest=document_digest({item.policy_digest: item.serving_surfaces
                                           for item in coverage}),
            serving_surfaces=surfaces, verified_surfaces=surfaces,
            verified_at=finished, expires_at=expires,
        )

    def renew_program_proofs(self) -> None:
        from ..persistence.repository_support import _managed_program_publication

        targets = self._repository.list_apim_targets()
        if len(targets) > 32:
            raise PolicyCompilationError("Fixed route proof renewal target batch exceeds its bound")
        repository = cast("ModelAvailabilityRepository", self._repository)
        network_error: httpx.HTTPError | None = None
        last_error: Exception | None = None
        use_backoff = self._clock is _default_route_clock
        now_mono = monotonic()
        for target in targets:
            target_id = UUID(str(target["gateway_profile_id"]))
            gateways = self._repository.get_gateway_profiles([target_id])
            if any(gateway["id"] == target_id and gateway["enabled"] is False
                   for gateway in gateways):
                continue
            current = self._repository.effective_gateway_publication(target_id)
            if (current is not None and "fixed_route_program" not in current.get(
                    "resource_manifest", {},
            ) and len(gateways) == 1 and gateways[0]["id"] == target_id):
                current = _managed_program_publication(current, gateways[0].get("config") or {})
            if current is None or "fixed_route_program" not in current.get("resource_manifest", {}):
                continue
            backoff_key = (target_id, str(current["id"]))
            if use_backoff:
                with _PROGRAM_RENEW_BACKOFF_LOCK:
                    if _PROGRAM_RENEW_BACKOFF_UNTIL.get(backoff_key, 0.0) > now_mono:
                        continue
            attempt_started = monotonic()
            try:
                with repository.model_availability_proof_guard(target_id, wait=False):
                    installed, config = self.installed(target_id, UUID(str(current["id"])))
                    with self._open(installed, config) as (port, _):
                        observed = port.projection.read_program_proof(installed.program)
                        if observed is None or observed[0].expires_epoch <= self._clock() + 30:
                            proof = port._verifier.verify()
                            port.projection.publish_program_proof(
                                proof, installed.program,
                                observed[1] if observed is not None else None, self._clock(),
                            )
                    if installed.native_gateway_scope:
                        self._renew_native_gateway_programs(installed, config)
                if use_backoff:
                    with _PROGRAM_RENEW_BACKOFF_LOCK:
                        _PROGRAM_RENEW_BACKOFF_UNTIL.pop(backoff_key, None)
            except ModelAvailabilityUnavailable as error:
                if str(error) != "Availability proof writer is busy":
                    if use_backoff:
                        with _PROGRAM_RENEW_BACKOFF_LOCK:
                            _PROGRAM_RENEW_BACKOFF_UNTIL[backoff_key] = (
                                monotonic() + _PROGRAM_RENEW_BACKOFF_SECONDS
                            )
                    raise
                logging.getLogger(__name__).warning(
                    "Fixed route proof renewal deferred target=%s reason=writer_busy", target_id,
                )
            except httpx.HTTPError as error:
                if use_backoff:
                    with _PROGRAM_RENEW_BACKOFF_LOCK:
                        _PROGRAM_RENEW_BACKOFF_UNTIL[backoff_key] = (
                            monotonic() + _PROGRAM_RENEW_BACKOFF_SECONDS
                        )
                network_error = error
                logging.getLogger(__name__).warning(
                    "Fixed route proof renewal failed target=%s error_type=%s "
                    "elapsed_seconds=%.1f",
                    target_id, type(error).__name__, monotonic() - attempt_started,
                )
            except Exception as error:
                if use_backoff:
                    with _PROGRAM_RENEW_BACKOFF_LOCK:
                        _PROGRAM_RENEW_BACKOFF_UNTIL[backoff_key] = (
                            monotonic() + _PROGRAM_RENEW_BACKOFF_SECONDS
                        )
                    last_error = error
                    from .model_availability import loggable_reason

                    logging.getLogger(__name__).warning(
                        "Fixed route proof renewal failed target=%s error_type=%s "
                        "elapsed_seconds=%.1f reason=%s",
                        target_id, type(error).__name__, monotonic() - attempt_started,
                        loggable_reason(error),
                    )
                else:
                    raise
        if last_error is not None:
            raise last_error
        if network_error is not None:
            raise network_error

    def _renew_native_program_proof(
        self, installed: FixedProgramInstallation, node: FixedProgramInstallation,
        port: FixedRoutePublisherPort,
    ) -> None:
        try:
            observed = port.projection.read_program_proof(node.program)
        except RouteProjectionConflict as error:
            if str(error) != "ROUTE_PROGRAM_PROOF_IDENTITY_CHANGED":
                raise
            from ..persistence.repository_support import _registered_program_installation

            target_id = UUID(installed.program.scope.target_id)
            source = self._repository.effective_gateway_publication(target_id)
            gateways = self._repository.get_gateway_profiles([target_id])
            if source is None or len(gateways) != 1 or gateways[0]["id"] != target_id:
                raise PolicyCompilationError("Native program upgrade source changed") from error
            original = FixedProgramInstallation.model_validate_json(canonical_document(
                source["resource_manifest"]["fixed_route_program"],
            ))
            config = gateways[0].get("config") or {}
            if _registered_program_installation(str(source["id"]), original, config) != installed:
                raise PolicyCompilationError("Native program upgrade history changed") from error
            previous_installations = [original]
            previous_installations.extend(FixedProgramUpgrade.model_validate_json(
                canonical_document(record),
            ).installation for record in config.get("fixed_route_program_upgrade_history", []))
            for previous in reversed(previous_installations):
                old_node = previous.for_native_gateway(
                    node.program.scope.gateway_id, node.program.instance_id, node.gateway_endpoint,
                )
                try:
                    observed = port.projection.read_program_proof(old_node.program)
                except RouteProjectionConflict as mismatch:
                    if str(mismatch) != "ROUTE_PROGRAM_PROOF_IDENTITY_CHANGED":
                        raise
                    continue
                if observed is None:
                    continue
                upgrade = FixedProgramUpgrade(
                    previous_publication_id=str(source["id"]),
                    previous_installation_sha256=document_digest(old_node), installation=node,
                )
                upgrade.require_previous(str(source["id"]), old_node)
                proof = port._verifier.verify()
                port.projection.publish_upgraded_program_proof(
                    upgrade, old_node, proof, observed[1], self._clock(),
                )
                return
            raise PolicyCompilationError("Native proof does not match approved history") from error
        if observed is None or observed[0].expires_epoch <= self._clock() + 30:
            proof = port._verifier.verify()
            port.projection.publish_program_proof(
                proof, node.program, observed[1] if observed is not None else None, self._clock(),
            )

    def _renew_native_gateway_programs(
        self, installed: FixedProgramInstallation, config: ApimTargetClientConfig,
    ) -> None:
        target_id = UUID(installed.program.scope.target_id)
        availability = cast("ModelAvailabilityRepository", self._repository)
        enrollments = availability.list_model_availability_node_enrollments(target_id)
        deployments = self._repository.list_gateway_deployments(target_id)
        for deployment in deployments:
            if deployment.get("kind") != "self_hosted" or deployment.get("retired_at") is not None:
                continue
            programs = self.bound_gateway_programs(installed, deployment, enrollments)
            for node in programs:
                with self._open(node, config) as (port, _):
                    self._renew_native_program_proof(installed, node, port)
            if availability.list_model_availability_node_enrollments(target_id) != enrollments:
                raise RetryablePublicationError("Native Gateway node enrollment changed")
            with self._open(programs[0], config) as (port, _):
                proofs = [port.projection.read_program_proof(node.program) for node in programs]
                if any(
                    value is None or value[0].identity != node.program
                    or not value[0].issued_epoch <= self._clock() < value[0].expires_epoch
                    for node, value in zip(programs, proofs, strict=True)
                ):
                    raise RetryablePublicationError("Native Gateway full-node proof is unavailable")
                primary = proofs[0]
                if primary is None:
                    raise RetryablePublicationError("Native Gateway program proof is unavailable")
                fence = self._active_fence(target_id)
                if (
                    fence.get("mode") != "active" or fence.get("protocol_version") != 2
                    or fence.get("gateway_profile_id") != str(target_id)
                ):
                    raise PolicyCompilationError(
                        "Native Gateway initialization budget fence changed",
                    )
                epoch = str(UUID(str(fence.get("epoch"))))
                observed_head = port.projection.read_active()
                if observed_head is None:
                    operation_id = str(uuid5(target_id, "native-gateway-initialization:" +
                        document_digest({"scope": programs[0].program.scope.model_dump(mode="json"),
                                         "program": installed.program.program_sha256,
                                         "epoch": epoch})))
                    head = port.projection.initialize_empty_scope(
                        programs[0].program, primary[0], epoch, operation_id, self._clock,
                    )
                else:
                    head = observed_head[0]
                    head.require_ready(
                        programs[0].program, primary[0], epoch, self._clock(),
                        legacy_program_sha256=installed.legacy_program_sha256,
                        predecessor_program_sha256=installed.predecessor_program_sha256,
                    )
            for node in programs:
                with self._open(node, config) as (port, _):
                    port._verifier.confirm(head)

    def queue_approval_renewals(self) -> None:
        from ..persistence.repository_support import _managed_program_publication

        targets = self._repository.list_apim_targets()
        if len(targets) > 32:
            raise PolicyCompilationError("Route approval renewal target batch exceeds its bound")
        for target in targets:
            target_id = UUID(str(target["gateway_profile_id"]))
            row = self._repository.effective_gateway_publication(target_id)
            if row is not None and "fixed_route_program" not in row.get("resource_manifest", {}):
                gateways = self._repository.get_gateway_profiles([target_id])
                if len(gateways) == 1 and gateways[0]["id"] == target_id:
                    row = _managed_program_publication(row, gateways[0].get("config") or {})
            if row is None or "fixed_route_program" not in row.get("resource_manifest", {}):
                continue
            current = GatewayPublication.model_validate(row)
            if current.gateway_profile_id != target_id or current.status.value != "active":
                raise PolicyCompilationError("Route renewal effective publication changed")
            installed, config = self.installed(target_id, current.id)
            if installed.model_additions_enabled:
                try:
                    self._renew_independent_approvals(current, installed, config)
                except ModelAvailabilityUnavailable as error:
                    if str(error) != "Availability proof writer is busy":
                        raise
                    logging.getLogger(__name__).warning(
                        "Route approval renewal deferred target=%s reason=writer_busy", target_id,
                    )
                continue
            manifest = current.resource_manifest
            raw_task = manifest.get("route_configuration")
            if isinstance(raw_task, dict):
                task = RoutePublicationTask.model_validate_json(canonical_document(raw_task))
                activation = manifest.get("route_activation")
                if not isinstance(activation, dict) or activation.get("outcome") != "confirmed":
                    raise PolicyCompilationError("Route renewal base activation is unconfirmed")
                head = RouteActivationIntent.from_record(activation["intent"]).head
                snapshot = task.plan.snapshot
            else:
                raw_snapshot, raw_head = (
                    manifest.get("fixed_route_snapshot"), manifest.get("fixed_route_head"),
                )
                if not isinstance(raw_snapshot, dict) or not isinstance(raw_head, dict):
                    raise PolicyCompilationError(
                        "Route renewal installation snapshot is unavailable",
                    )
                snapshot = RouteSnapshot.model_validate_json(canonical_document(raw_snapshot))
                head = ActiveRouteHead.model_validate_json(canonical_document(raw_head))
            head.approval.require_same_configuration(snapshot.approval)
            snapshot.model_copy(update={"approval": head.approval}).require_valid(
                head.activated_epoch,
            )
            if not snapshot.members or min(
                member.connection_approval.expires_epoch for member in snapshot.members
            ) > self._clock() + 3600:
                continue
            spec = current.desired_spec.model_copy(update={"removed_models": []})
            digest = document_digest({
                "kind": "route-approval-renewal", "base_publication_id": str(current.id),
                "specification": spec.model_dump(mode="json"),
            })
            try:
                self._repository.create_gateway_publication(
                    target_id, spec.model_dump(mode="json"), digest, "route_reconcile",
                    "system-route-approval-renewal", expected_base_release_id=current.id,
                )
            except ValueError:
                logger.warning("Route approval renewal is blocked for target %s", target_id)

    def _renew_independent_approvals(
        self, current: GatewayPublication, installed: FixedProgramInstallation,
        config: ApimTargetClientConfig, *, wait: bool = False,
    ) -> None:
        from ..services.route_publication import recorded_route_scopes, recorded_route_snapshot

        target_id = current.gateway_profile_id
        source = self._repository.effective_gateway_publication(target_id)
        if (source is None or str(source["id"]) != str(current.id)
                or str(source["gateway_profile_id"]) != str(target_id)
                or source["status"] != "active"):
            raise PolicyCompilationError("Approval renewal program base changed")
        repository = cast("ModelAvailabilityRepository", self._repository)
        publications = [current, *[
            GatewayPublication.model_validate(row)
            for row in self._repository.active_model_route_publications(target_id)
        ]]
        snapshots = [(publication, scope, len(scopes)) for publication in publications
                 for scopes in (recorded_route_scopes(publication),) for scope in scopes]
        runtimes = {str(row["id"]): row for row in self._repository.registry()["runtimes"]}
        completed = 0
        with (
            repository.model_availability_proof_guard(target_id, wait=wait),
            ExitStack() as readers,
        ):
            base_port, base_management = readers.enter_context(self._open(installed, config))
            base_proof = base_port._verifier.verify()
            started = self._clock()
            fence = self._active_fence(target_id)
            if (fence.get("protocol_version") != 2 or fence.get("mode") != "active"
                    or fence.get("gateway_profile_id") != str(target_id)):
                raise PolicyCompilationError("Approval renewal budget fence changed")
            for publication, scope, scope_count in snapshots:
                if scope_count == 1:
                    snapshot, head = recorded_route_snapshot(publication)
                else:
                    snapshot, head = recorded_route_snapshot(publication, scope=scope)
                if head.approval.budget_epoch != str(fence.get("epoch")):
                    raise PolicyCompilationError("Approval renewal budget epoch changed")
                installation, port, management, proof = (
                    installed, base_port, base_management, base_proof,
                )
                programs: tuple[FixedProgramInstallation, ...] = ()
                if head.approval.scope != installed.program.scope:
                    if not installed.native_gateway_scope:
                        raise PolicyCompilationError("Approval renewal program scope changed")
                    programs = tuple(node for node in self.serving_programs(publication)
                                     if node.program.scope == head.approval.scope)
                    if not programs:
                        raise PolicyCompilationError("Approval renewal serving scope changed")
                    for index, node in enumerate(programs):
                        node_port, node_management = readers.enter_context(self._open(node, config))
                        observed_proof = node_port.projection.read_program_proof(node.program)
                        now = self._clock()
                        if (observed_proof is None or observed_proof[0].identity != node.program
                                or not observed_proof[0].issued_epoch <= now
                                < observed_proof[0].expires_epoch
                                or not set(head.approval.required_profiles)
                                <= set(observed_proof[0].supported_profiles)):
                            raise RetryablePublicationError(
                                "Approval renewal requires every serving node proof",
                            )
                        node_port._verifier.confirm(head)
                        if index == 0:
                            installation, port, management, proof = (
                                node, node_port, node_management, observed_proof[0],
                            )
                own = "model_route_addition" in publication.resource_manifest
                observed = port.projection.read_model_active(snapshot.routes[0].alias) \
                    if own else port.projection.read_active()
                observed_value = observed[0] if observed is not None else None
                observed_head = observed_value.head if isinstance(
                    observed_value, ModelRouteHead,
                ) else observed_value
                if observed_head != head:
                    raise PolicyCompilationError("Approval renewal activity head changed")
                if not snapshot.routes:
                    continue
                references = self._credential_references(publication, publication, installation)
                members_by_alias = {member.alias: member for member in snapshot.members}
                for route in snapshot.routes:
                    member = members_by_alias[route.alias]
                    now = self._clock()
                    if completed >= 4 or now >= min(started + 60, proof.expires_epoch - 10):
                        return
                    original = member.connection_approval
                    if original.expires_epoch > now + 3600:
                        continue
                    saved = port.projection.read_approval_renewal_history(
                        route, member, installation.program,
                    )
                    if saved is not None and saved[0].approval.expires_epoch > now + 3600:
                        continue
                    binding = next((item for item in publication.desired_spec.bindings
                                    if item.model.model_key == route.alias), None)
                    runtime = runtimes.get(str(binding.runtime_id)) if binding else None
                    if (binding is None or runtime is None or not runtime["enabled"]
                            or str(runtime.get("gateway_profile_id")) != str(target_id)
                            or not self._runtime_destination_matches(
                                publication, binding, runtime.get("config") or {},
                            )):
                        raise PolicyCompilationError("Approval renewal Runtime changed")
                    actual = management.verify_route_connection(route.connection)
                    if actual != route.connection.dependency_sha256:
                        raise PolicyCompilationError("Approval renewal connection changed")
                    self._verify_credential_references(references)
                    issued = max(
                        now - now % 3600, original.issued_epoch,
                        saved[0].approval.issued_epoch if saved is not None else 0,
                    )
                    identifier = str(uuid5(
                        publication.id, "approval-renewal:" + route.sha256
                        + member.approval_sha256 + str(issued),
                    ))
                    renewal = RouteApprovalRenewal(
                        scope=installation.program.scope, content_sha256=route.sha256,
                        original_approval_sha256=member.approval_sha256,
                        required_program_sha256=installation.program.program_sha256,
                        budget_epoch=route.budget_epoch,
                        approval=ConnectionApproval(
                            approval_id=identifier, connection=route.connection,
                            state="approved", evidence_sha256=document_digest({
                                "publication_id": str(publication.id), "dependency": actual,
                                "runtime": runtime["config"], "issued_epoch": issued,
                            }), issued_epoch=issued, expires_epoch=issued + 86400,
                        ),
                    )
                    if self._repository.effective_gateway_publication(target_id) != source:
                        raise PolicyCompilationError("Approval renewal program base changed")
                    current_programs = tuple(node for node in self.serving_programs(publication)
                                             if node.program.scope == head.approval.scope) \
                        if programs else ()
                    if programs and current_programs != programs:
                        raise PolicyCompilationError("Approval renewal serving nodes changed")
                    if self._active_fence(target_id) != fence:
                        raise PolicyCompilationError("Approval renewal budget fence changed")
                    port.projection.publish_approval_renewal(
                        renewal, route, member, proof,
                        saved[1] if saved else None, self._clock(),
                    )
                    completed += 1

    def _budget_contracts(
        self, publication: GatewayPublication, previous: GatewayPublication,
        registry: Mapping[str, Sequence[dict[str, Any]]] | None = None,
        fence: Mapping[str, Any] | None = None,
    ) -> BudgetPublicationContracts:
        return BudgetPublicationContracts.capture(
            publication, registry if registry is not None else self._repository.registry(),
            fence if fence is not None else self._active_fence(publication.gateway_profile_id),
            previous,
            inherited_publications=tuple(
                GatewayPublication.model_validate(row)
                for row in self._repository.active_model_route_publications(
                    publication.gateway_profile_id,
                )
            ) if publication.publication_kind in _FULL_ROUTE_RELEASES else (),
        )

    @staticmethod
    def _dependency_context(
        publication: GatewayPublication, installation: FixedProgramInstallation,
    ) -> str:
        return document_digest({
            "publication_id": str(publication.id),
            "desired_spec_sha256": publication.desired_spec_sha256,
            "specification": publication.desired_spec.model_dump(mode="json"),
            "installation": installation.model_dump(mode="json"),
        })

    def _preparation_context(
        self, publication: GatewayPublication, installation: FixedProgramInstallation,
    ) -> str:
        context = self._dependency_context(publication, installation)
        checkpoint = publication.resource_manifest.get("route_preparation")
        if (not isinstance(checkpoint, dict) or checkpoint.get("context_sha256") == context
                or "model_route_addition" not in publication.resource_manifest):
            return context
        from ..persistence.repository_support import (
            _approved_preparation_installations,
            _managed_program_publication,
        )

        previous = self._repository.effective_gateway_publication(publication.gateway_profile_id)
        gateways = self._repository.get_gateway_profiles([publication.gateway_profile_id])
        if (previous is None or str(previous["id"]) != str(publication.base_release_id)
                or len(gateways) != 1 or gateways[0]["id"] != publication.gateway_profile_id):
            raise PolicyCompilationError("Route preparation effective program changed")
        config = gateways[0].get("config") or {}
        origin = _managed_program_publication(previous, {
            "managed_route_program_initialization": config.get(
                "managed_route_program_initialization",
            ),
        })
        assert origin is not None
        initial = FixedProgramInstallation.model_validate_json(canonical_document(
            origin["resource_manifest"]["fixed_route_program"],
        ))
        approved, candidates = _approved_preparation_installations(
            str(previous["id"]), initial, config,
        )
        if approved != installation:
            raise PolicyCompilationError("Route preparation current program is not approved")
        recorded = checkpoint.get("context_sha256")
        if any(self._dependency_context(publication, candidate) == recorded
               for candidate in candidates):
            return str(recorded)
        return context

    @staticmethod
    def _runtime_destination_matches(
        publication: GatewayPublication, binding: GatewayModelBinding,
        config: Mapping[str, Any],
    ) -> bool:
        from ..services.control_plane import GatewayControlPlaneService
        from ..services.route_publication import _binding_route_origin

        observed = binding.model_copy(update={
            "backend_url": config.get("backend_url"),
            "backend_path": config.get("backend_path"), "runtime_config": dict(config),
        })
        try:
            normalized = GatewayControlPlaneService._normalize_release_spec(
                publication.desired_spec.model_copy(update={"bindings": [observed]}),
            ).bindings[0]
            return (
                _binding_route_origin(normalized) == _binding_route_origin(binding)
                and urlsplit(str(normalized.backend_url)).path.rstrip("/") + normalized.backend_path
                == urlsplit(str(binding.backend_url)).path.rstrip("/") + binding.backend_path
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            return False

    def _runtime_oauth_matches(
        self, publication: GatewayPublication, binding: GatewayModelBinding,
        config: Mapping[str, Any],
    ) -> bool:
        observed = OAuthClientCredentialsConfig.model_validate(config.get("oauth"))
        desired = binding.oauth
        if observed == desired:
            return True
        if (
            desired is None or observed.provider_id == desired.provider_id
            or publication.publication_kind is not PublicationKind.CREDENTIAL_ROTATION
            or publication.base_release_id is None
        ):
            return False
        row = self._repository.get_gateway_publication(publication.base_release_id)
        if row is None:
            return False
        previous = GatewayPublication.model_validate(row)
        if (previous.id != publication.base_release_id
                or previous.gateway_profile_id != publication.gateway_profile_id):
            return False
        return any(
            prior.runtime_id == binding.runtime_id and prior.oauth == observed
            and prior.backend_url == binding.backend_url
            and prior.auth_strategy == binding.auth_strategy
            for prior in previous.desired_spec.bindings
        )

    def _runtime_rotation_matches(
        self, publication: GatewayPublication, binding: GatewayModelBinding,
        config: Mapping[str, Any],
    ) -> bool:
        from ..services.route_publication import route_credential_source

        recorded = config.get("key_vault_secret_id")
        if (
            recorded is None
            or publication.publication_kind is not PublicationKind.CREDENTIAL_ROTATION
            or publication.base_release_id is None
        ):
            return False
        row = self._repository.get_gateway_publication(publication.base_release_id)
        if row is None:
            return False
        previous = GatewayPublication.model_validate(row)
        if (previous.id != publication.base_release_id
                or previous.gateway_profile_id != publication.gateway_profile_id):
            return False
        inherited = self._inherited_credential_bindings(publication, previous)
        return any(
            prior.runtime_id == binding.runtime_id
            and prior.auth_strategy == binding.auth_strategy
            and prior.named_value_name == config.get("named_value_name")
            and recorded == (
                str(prior.key_vault_secret_id) if prior.key_vault_secret_id is not None
                else inherited.get(route_credential_source(prior))
            )
            for prior in self._previous_credential_sources(publication, previous)
        )

    def _preflight_dependencies(
        self, publication: GatewayPublication, installation: FixedProgramInstallation,
        registry: Mapping[str, Sequence[dict[str, Any]]] | None = None,
    ) -> None:
        from ..services.route_publication import (
            _binding_route_origin,
            binding_request_adapter,
            pool_member_bindings,
        )

        profiles: dict[ApiFormat, str] = {
            ApiFormat.OPENAI_CHAT: "openai-v1", ApiFormat.ANTHROPIC_MESSAGES: "anthropic-messages",
            ApiFormat.OPENAI_IMAGES: "openai-images",
            ApiFormat.MICROSOFT_DECISION: "microsoft-decision",
        }
        runtimes = (registry if registry is not None else self._repository.registry())["runtimes"]
        for binding in publication.desired_spec.bindings:
            pool = binding.backend_pool
            if pool is not None:
                if (not installation.pool_enabled or binding.oauth is not None
                        or binding.api_format is ApiFormat.OPENAI_IMAGES):
                    raise PolicyCompilationError("Fixed route Pool capability is unavailable")
                for source in pool_member_bindings(binding):
                    member_runtime = next(
                        (item for item in runtimes if item["id"] == source.runtime_id), None,
                    )
                    member_config = (
                        member_runtime.get("config") if member_runtime is not None else None
                    )
                    if (
                        member_runtime is None or member_runtime.get("enabled") is not True
                        or member_runtime.get("gateway_profile_id")
                        != publication.gateway_profile_id
                        or member_runtime.get("provider_id") != source.provider_id
                        or not isinstance(member_config, dict)
                        or member_config.get("control_plane_managed") is not True
                        or member_config.get("auth_strategy") != source.auth_strategy.value
                        or member_config.get("named_value_name") != source.named_value_name
                        or source.auth_strategy is AuthStrategy.MANAGED_IDENTITY
                        and source.api_format is not ApiFormat.MICROSOFT_DECISION
                        and member_config.get("managed_identity_resource")
                        != source.managed_identity_resource
                        or not self._runtime_destination_matches(publication, source, member_config)
                    ):
                        raise PolicyCompilationError("Fixed route Pool member Runtime changed")
            runtime = next((item for item in runtimes if item["id"] == binding.runtime_id), None)
            config = runtime.get("config") if runtime is not None else None
            profile = profiles.get(binding.api_format)
            if (
                runtime is None or runtime.get("enabled") is not True
                or str(runtime.get("gateway_profile_id")) != str(publication.gateway_profile_id)
                or not isinstance(config, dict) or profile is None
                or binding.serving_deployments and all(
                    deployment.native_gateway_id != installation.program.scope.gateway_id
                       for deployment in binding.serving_deployments)
            ):
                raise PolicyCompilationError("Fixed route dependency preflight is unsupported")
            if profile not in installation.supported_profiles:
                raise GatewayCapabilityUnavailableError(profile)
            adapter = binding_request_adapter(binding)
            if adapter is not None and adapter not in installation.supported_adapters:
                raise PolicyCompilationError("Fixed route Bedrock adapter is not installed")
            if binding.auth_strategy is AuthStrategy.OAUTH_CLIENT_CREDENTIALS and (
                not installation.oauth_enabled or installation.program.scope.gateway_id != "managed"
                or config.get("auth_strategy") != binding.auth_strategy.value
                or not self._runtime_oauth_matches(publication, binding, config)
            ):
                raise PolicyCompilationError(
                    "Fixed route OAuth capability or Runtime binding changed",
                )
            if binding.api_format is ApiFormat.OPENAI_IMAGES:
                image = validate_image_profile(binding.model.image_profile)
                if installation.image_limits is None or any(
                    getattr(image, field) > getattr(installation.image_limits, field)
                    for field in ImageGenerationLimits.model_fields
                ):
                    raise PolicyCompilationError("Fixed route image exceeds installed capacity")
            _binding_route_origin(binding)
            if "storage_route_approvals" not in config and (
                config.get("control_plane_managed") is not True
                or config.get("auth_strategy") != binding.auth_strategy.value
                or not self._runtime_destination_matches(publication, binding, config)
            ):
                raise PolicyCompilationError("Fixed route source Runtime destination changed")

    @staticmethod
    def _credential_bindings(publication: GatewayPublication) -> dict[str, str]:
        return credential_bindings(publication.resource_manifest)

    def _inherited_credential_bindings(
        self, publication: GatewayPublication, previous: GatewayPublication,
    ) -> dict[str, str]:
        inherited = self._credential_bindings(previous)
        if publication.publication_kind in _FULL_ROUTE_RELEASES:
            for row in self._repository.active_model_route_publications(
                publication.gateway_profile_id,
            ):
                if row.get("id") != publication.id:
                    inherited.update(credential_bindings(row.get("resource_manifest") or {}))
        return inherited

    def _previous_credential_sources(
        self, publication: GatewayPublication, previous: GatewayPublication,
    ) -> tuple[GatewayModelBinding, ...]:
        from ..services.route_publication import pool_member_bindings

        releases = (previous, *(
            GatewayPublication.model_validate(row)
            for row in self._repository.active_model_route_publications(
                publication.gateway_profile_id,
            )
            if row.get("id") != publication.id
        )) if publication.publication_kind in _FULL_ROUTE_RELEASES else (previous,)
        return tuple(
            source for release in releases for binding in release.desired_spec.bindings
            for source in pool_member_bindings(binding) or (binding,)
        )

    def _prepare_credentials(
        self, publication: GatewayPublication, previous: GatewayPublication,
        installation: FixedProgramInstallation, management: FixedProgramManagement,
        worker_id: str, heartbeat: Callable[[], None],
        *, registry: Mapping[str, Sequence[dict[str, Any]]] | None = None,
    ) -> tuple[GatewayPublication, dict[str, str]]:
        from ..services.route_publication import pool_member_bindings, route_credential_source

        keyed = [
            source for binding in publication.desired_spec.bindings
            for source in pool_member_bindings(binding) or (binding,) if source.auth_strategy in {
                AuthStrategy.NAMED_VALUE_API_KEY, AuthStrategy.NAMED_VALUE_BEARER,
            }
        ]
        bindings = self._credential_bindings(publication)
        if not keyed:
            if bindings:
                raise PolicyCompilationError("Route credential bindings have no source")
            return publication, {}
        inherited = self._inherited_credential_bindings(publication, previous)
        previous_bindings = self._previous_credential_sources(publication, previous)
        previous_sources = {
            route_credential_source(binding) for binding in previous_bindings
            if binding.auth_strategy in {
                AuthStrategy.NAMED_VALUE_API_KEY, AuthStrategy.NAMED_VALUE_BEARER,
            }
        }
        new_sources = {route_credential_source(binding) for binding in keyed} - previous_sources
        context = self._preparation_context(publication, installation)
        raw_preparation = publication.resource_manifest.get("route_preparation")
        if raw_preparation is not None and not isinstance(raw_preparation, dict):
            raise PolicyCompilationError("Route credential preparation is invalid")
        preparation = (
            _PreparationCheckpoint.model_validate_json(canonical_document(raw_preparation))
            if raw_preparation is not None else _PreparationCheckpoint(context_sha256=context)
        )
        if preparation.context_sha256 != context:
            raise PolicyCompilationError("Route credential preparation context changed")
        stored = deepcopy(raw_preparation)
        runtimes = (registry if registry is not None else self._repository.registry())["runtimes"]
        result: dict[str, str] = {}
        for binding in keyed:
            runtime = next((item for item in runtimes if item["id"] == binding.runtime_id), None)
            config = runtime.get("config") if runtime is not None else None
            rotating_source = (
                isinstance(config, dict)
                and publication.publication_kind is PublicationKind.CREDENTIAL_ROTATION
                and any(
                    prior.runtime_id == binding.runtime_id
                    and prior.auth_strategy == binding.auth_strategy
                    and prior.named_value_name == config.get("named_value_name")
                    and (prior.key_vault_secret_id == binding.key_vault_secret_id
                         or prior.key_vault_secret_id is not None
                         and str(prior.key_vault_secret_id) == config.get("key_vault_secret_id"))
                    for prior in previous_bindings
                )
            )
            if (
                runtime is None or runtime.get("enabled") is not True
                or str(runtime.get("gateway_profile_id")) != str(publication.gateway_profile_id)
                or not isinstance(config, dict) or config.get("control_plane_managed") is not True
                or config.get("auth_strategy") != binding.auth_strategy.value
                or (config.get("named_value_name") != binding.named_value_name
                    and not rotating_source)
            ):
                raise PolicyCompilationError("Route credential source Runtime changed")
            if binding.key_vault_secret_id is not None:
                self._require_credential_vault(str(binding.key_vault_secret_id), installation)
                continue
            source = route_credential_source(binding)
            if source in result:
                continue
            existing = bindings.get(source) or inherited.get(source)
            if existing is not None:
                self._require_credential_vault(existing, installation)
                if inherited.get(source) != existing:
                    raw_credential = preparation.credentials.get(source)
                    if raw_credential is None or self._cipher is None:
                        raise PolicyCompilationError(
                            "Route credential binding has no source intent",
                        )
                    try:
                        intent = _VaultCheckpoint.model_validate_json(
                            canonical_document(raw_credential),
                        )
                        decoded = self._cipher.decrypt(intent.body_ciphertext.encode("ascii"))
                        body = json.loads(decoded) if decoded is not None else None
                        valid = (
                            intent.publication_id == str(publication.id)
                            and intent.source_sha256 == source and intent.secret_id == existing
                            and existing.rsplit("/", 1)[0] == intent.url
                            and decoded is not None and text_digest(decoded) == intent.body_sha256
                            and isinstance(body, dict)
                            and set(body) == {"value", "tags", "attributes"}
                            and isinstance(body["value"], str) and bool(body["value"])
                            and body["attributes"] == {"enabled": True}
                            and body["tags"] == {
                                "turnstile-publication": str(publication.id),
                                "turnstile-source": source,
                            }
                        )
                    except (ValueError, TypeError, UnicodeError):
                        raise PolicyCompilationError(
                            "Route credential source intent is invalid",
                        ) from None
                    if not valid:
                        raise PolicyCompilationError(
                            "Route credential binding source intent changed",
                        )
                result[source] = existing
                continue
            if self._cipher is None or installation.credential_vault_origin is None:
                raise PolicyCompilationError("Route credential materialization is unavailable")
            encrypted = self._repository.gateway_publication_credential(publication.id)
            if encrypted is not None and new_sources == {source}:
                value = self._cipher.decrypt(encrypted)
                if value is None:
                    raise PolicyCompilationError("Route publication credential is unavailable")
                secret = SecretStr(value)
            elif any(
                prior.auth_strategy in {
                    AuthStrategy.NAMED_VALUE_API_KEY, AuthStrategy.NAMED_VALUE_BEARER,
                } and route_credential_source(prior) == source
                for prior in previous_bindings
            ):
                assert binding.named_value_name is not None
                secret = management.route_credential_value(binding.named_value_name)
            else:
                raise PolicyCompilationError("Route credential has no approved original source")

            def persist(
                expected: Mapping[str, Any] | None, candidate: dict[str, Any],
                *, source_key: str = source,
            ) -> None:
                nonlocal preparation, stored
                if preparation.credentials.get(source_key) != expected:
                    raise PolicyCompilationError("Route credential intent changed")
                updated = preparation.model_copy(update={"credentials": {
                    **preparation.credentials, source_key: deepcopy(candidate),
                }})
                recorded = updated.model_dump(mode="json")
                heartbeat()
                if not self._repository.checkpoint_gateway_publication_preparation(
                    publication.id, worker_id, stored, recorded,
                ):
                    raise RetryablePublicationError("Route credential preparation lease changed")
                preparation, stored = updated, recorded

            with httpx.Client(timeout=10.0, follow_redirects=False) as client:
                vault = RouteCredentialVault(
                    installation.credential_vault_origin, self._cipher,
                    ManagedIdentityTokenProvider(), client, clock=self._clock,
                )
                result[source] = vault.materialize(
                    publication.id, source, secret,
                    checkpoint=preparation.credentials.get(source), persist=persist,
                )
                self._require_credential_vault(result[source], installation)
        if set(bindings) - set(result) or bindings and bindings != result:
            raise PolicyCompilationError("Route credential bindings changed after preparation")
        recorded_credentials = _CredentialBindings(bindings=result).model_dump(mode="json")
        if publication.resource_manifest.get("route_credentials") is None:
            heartbeat()
            if not self._repository.checkpoint_gateway_publication_credentials(
                publication.id, worker_id, None, recorded_credentials,
            ):
                raise RetryablePublicationError("Route credential bindings lease changed")
        return publication.model_copy(update={"resource_manifest": {
            **publication.resource_manifest, "route_credentials": recorded_credentials,
            **({"route_preparation": stored} if stored is not None else {}),
        }}), result

    def _prepare_oauth(
        self, publication: GatewayPublication, previous: GatewayPublication,
        installation: FixedProgramInstallation, management: FixedProgramManagement,
        worker_id: str, heartbeat: Callable[[], None],
    ) -> GatewayPublication:
        configurations: dict[str, OAuthClientCredentialsConfig] = {}
        for binding in publication.desired_spec.bindings:
            if binding.oauth is None:
                continue
            config = binding.oauth
            existing = configurations.get(config.provider_id)
            if existing is not None and existing != config:
                raise PolicyCompilationError("Fixed route OAuth provider has conflicting sources")
            configurations[config.provider_id] = config
        if not configurations:
            return publication
        self._preflight_dependencies(publication, installation)
        inherited = {
            binding.oauth.provider_id: binding.oauth for binding in previous.desired_spec.bindings
            if binding.oauth is not None
        }
        new_sources = set(configurations) - set(inherited)
        if any(
            inherited[key] != value for key, value in configurations.items() if key in inherited
        ):
            raise PolicyCompilationError("Fixed route OAuth source changed without a new provider")
        context = self._preparation_context(publication, installation)
        raw = publication.resource_manifest.get("route_preparation")
        if raw is not None and not isinstance(raw, dict):
            raise PolicyCompilationError("Fixed route OAuth preparation is invalid")
        preparation = (
            _PreparationCheckpoint.model_validate_json(canonical_document(raw)) if raw is not None
            else _PreparationCheckpoint(context_sha256=context)
        )
        if preparation.context_sha256 != context:
            raise PolicyCompilationError("Fixed route OAuth preparation context changed")
        stored = deepcopy(raw)

        def save(candidate: _PreparationCheckpoint) -> None:
            nonlocal preparation, stored
            recorded = candidate.model_dump(mode="json")
            heartbeat()
            if not self._repository.checkpoint_gateway_publication_preparation(
                publication.id, worker_id, stored, recorded,
            ):
                raise RetryablePublicationError("Fixed route OAuth preparation lease changed")
            preparation, stored = candidate, recorded

        for provider_id, config in configurations.items():
            encrypted = (
                self._repository.gateway_publication_credential(publication.id)
                if provider_id in new_sources else None
            )
            if encrypted is not None and (self._cipher is None or len(new_sources) != 1):
                raise PolicyCompilationError("Fixed route OAuth secret source is ambiguous")
            source = preparation.oauth.get(provider_id)
            if source is None:
                source = _OAuthPreparation(
                    configuration=config,
                    source_ciphertext=encrypted.decode("ascii") if encrypted is not None else None,
                )
                save(preparation.model_copy(update={"oauth": {
                    **preparation.oauth, provider_id: source,
                }}))
            if source.configuration != config:
                raise PolicyCompilationError("Fixed route OAuth source configuration changed")
            secret = None
            if source.source_ciphertext is not None:
                if self._cipher is None:
                    raise PolicyCompilationError("Fixed route OAuth source cipher is unavailable")
                secret = self._cipher.decrypt(source.source_ciphertext.encode("ascii"))
                if secret is None or encrypted is not None and (
                    self._cipher.decrypt(encrypted) != secret
                ):
                    raise PolicyCompilationError("Fixed route OAuth secret source changed")
            elif encrypted is not None:
                raise PolicyCompilationError("Fixed route OAuth secret source changed")

            def persist(
                intent: dict[str, Any], *, identifier: str = provider_id,
                configuration: OAuthClientCredentialsConfig = config,
            ) -> None:
                state = preparation.oauth[identifier]
                body = intent.get("body")
                if (
                    self._cipher is None or not isinstance(body, str)
                    or intent.get("body_sha256") != text_digest(body)
                    or intent.get("configuration_sha256")
                    != text_digest(configuration.model_dump_json())
                ):
                    raise PolicyCompilationError("Fixed route OAuth request intent is invalid")
                resource = intent.get("resource_id")
                if (not isinstance(resource, str)
                    or "/authorizationProviders/" + identifier not in resource):
                    raise PolicyCompilationError("Fixed route OAuth request target changed")
                key = text_digest(resource)
                saved = state.requests.get(key)
                metadata = {name: value for name, value in intent.items() if name != "body"}
                if saved is not None:
                    if (saved.model_dump(mode="json", exclude={"body_ciphertext"}) != metadata
                            or self._cipher.decrypt(saved.body_ciphertext.encode("ascii")) != body):
                        raise PolicyCompilationError("Fixed route OAuth request intent changed")
                    raise RetryablePublicationError("Fixed route OAuth requires read-only recovery")
                recorded = _OAuthWriteIntent.model_validate({
                    **metadata, "body_ciphertext": self._cipher.encrypt(body).decode("ascii"),
                })
                updated = _OAuthPreparation.model_validate({
                    **state.model_dump(mode="python"),
                    "requests": {**state.requests, key: recorded},
                })
                save(preparation.model_copy(update={"oauth": {
                    **preparation.oauth, identifier: updated,
                }}))

            management.prepare_route_oauth(OAuthCredentialResource(config, secret), persist)
        return publication.model_copy(update={"resource_manifest": {
            **publication.resource_manifest, "route_preparation": stored,
        }})

    @staticmethod
    def _require_credential_vault(secret_id: str, installation: FixedProgramInstallation) -> None:
        RouteCredential(mode="key_vault_bearer", header="Authorization", secret_id=secret_id)
        address = urlsplit(secret_id)
        if address.scheme + "://" + address.netloc not in installation.trusted_vault_origins:
            raise PolicyCompilationError("Route credential Vault is not approved for this program")

    def _credential_references(
        self, publication: GatewayPublication, previous: GatewayPublication,
        installation: FixedProgramInstallation,
        *, changed_aliases: frozenset[str] | None = None,
    ) -> dict[str, SecretStr | None]:
        from ..services.route_publication import pool_member_bindings, route_credential_source

        bindings = self._credential_bindings(publication)
        inherited = self._inherited_credential_bindings(publication, previous)
        raw = publication.resource_manifest.get("route_preparation")
        if raw is not None and not isinstance(raw, dict):
            raise PolicyCompilationError("Route credential preparation is invalid")
        preparation = (
            _PreparationCheckpoint.model_validate_json(canonical_document(raw))
            if raw is not None else None
        )
        references: dict[str, SecretStr | None] = {}
        sources = (
            source for binding in publication.desired_spec.bindings
            for source in pool_member_bindings(binding) or (binding,)
        )
        for binding in sources:
            if binding.auth_strategy not in {
                AuthStrategy.NAMED_VALUE_API_KEY, AuthStrategy.NAMED_VALUE_BEARER,
            }:
                continue
            source = route_credential_source(binding)
            secret_id = str(binding.key_vault_secret_id) if binding.key_vault_secret_id else (
                bindings.get(source)
            )
            if secret_id is None:
                raise PolicyCompilationError("Route credential binding is missing")
            self._require_credential_vault(secret_id, installation)
            expected = None
            if binding.key_vault_secret_id is None and inherited.get(source) != secret_id:
                if (
                    preparation is None or self._cipher is None
                    or preparation.context_sha256
                    != self._preparation_context(publication, installation)
                    or source not in preparation.credentials
                ):
                    raise PolicyCompilationError("Route credential source intent is missing")
                intent = _VaultCheckpoint.model_validate_json(
                    canonical_document(preparation.credentials[source]),
                )
                decoded = self._cipher.decrypt(intent.body_ciphertext.encode("ascii"))
                body = json.loads(decoded) if decoded else None
                if (
                    intent.publication_id != str(publication.id) or intent.source_sha256 != source
                    or intent.secret_id != secret_id or decoded is None
                    or text_digest(decoded) != intent.body_sha256 or not isinstance(body, dict)
                    or not isinstance(body.get("value"), str) or not body["value"]
                ):
                    raise PolicyCompilationError("Route credential source intent changed")
                expected = SecretStr(body["value"])
            if (
                changed_aliases is not None
                and binding.model.model_key not in changed_aliases
                and expected is None
            ):
                continue
            references[secret_id] = expected
        if references and self._cipher is None:
            raise PolicyCompilationError("Route credential verification is unavailable")
        return references

    def _verify_credentials(
        self, publication: GatewayPublication, previous: GatewayPublication,
        installation: FixedProgramInstallation,
        *, changed_aliases: frozenset[str] | None = None,
    ) -> None:
        self._verify_credential_references(
            self._credential_references(
                publication, previous, installation, changed_aliases=changed_aliases,
            ),
        )

    def _verify_credential_references(self, references: Mapping[str, SecretStr | None]) -> None:
        for secret_id, expected in references.items():
            assert self._cipher is not None
            address = urlsplit(secret_id)
            client_context = (nullcontext(self._client) if self._client is not None
                              else httpx.Client(timeout=10.0, follow_redirects=False))
            with client_context as client:
                RouteCredentialVault(
                    address.scheme + "://" + address.netloc, self._cipher,
                    self._tokens, client, clock=self._clock,
                ).verify(secret_id, expected)

    @staticmethod
    def _new_connection(
        binding: GatewayModelBinding, installation: FixedProgramInstallation,
        config: ApimTargetClientConfig,
        credential_bindings: Mapping[str, str] | None = None,
    ) -> ConnectionRevision:
        from ..services.route_publication import (
            _binding_route_contract,
            _binding_route_origin,
            binding_public_count_path,
        )

        profile, paths, credential = _binding_route_contract(
            binding, credential_bindings=credential_bindings,
        )
        if (
            binding.runtime_id is None or not binding.routing_managed
            or binding.backend_pool is not None or profile not in installation.supported_profiles
            or any(deployment.native_gateway_id != installation.program.scope.gateway_id
                   for deployment in binding.serving_deployments or ())
        ):
            raise PolicyCompilationError("Fixed route connection preparation is unsupported")
        origin = _binding_route_origin(binding)
        observer = bool(config.settings.apim_usage_observer_url) and binding.api_format not in {
            ApiFormat.OPENAI_IMAGES, ApiFormat.MICROSOFT_DECISION,
        }
        credentials = (
            {
                "header": {
                    "x-adapter-key": [
                        "{{" + str(config.settings.apim_usage_observer_key_named_value) + "}}"
                    ],
                }
            }
            if observer
            else {}
        )
        dependency = document_digest({
            "type": "Single", "protocol": "http",
            "url": config.settings.apim_usage_observer_url if observer else origin,
            "tls": {"validateCertificateChain": True, "validateCertificateName": True},
            "credentials": credentials, "circuitBreaker": None,
        })
        identity = document_digest({
            "scope": installation.program.scope.model_dump(mode="json"),
            "runtime_id": str(binding.runtime_id), "origin": origin, "paths": paths,
            "credential": credential.model_dump(mode="json"), "dependency": dependency,
            "public_count_path": binding_public_count_path(binding),
        })
        return ConnectionRevision(
            scope=installation.program.scope,
            revision_id=str(uuid5(binding.runtime_id, identity)),
            backend_id="turnstile-route-" + identity[:40], origin=origin,
            path=next(iter(paths.values())), protocol_paths=paths,
            dependency_sha256=dependency, credential=credential, observer=observer,
            public_count_path=binding_public_count_path(binding),
        )

    @staticmethod
    def _new_pool_connection(
        binding: GatewayModelBinding, installation: FixedProgramInstallation,
        config: ApimTargetClientConfig, credential_bindings: Mapping[str, str],
    ) -> tuple[ConnectionRevision, tuple[BackendResource, ...]]:
        from ..services.route_publication import (
            _binding_route_contract,
            _binding_route_origin,
            binding_public_count_path,
            pool_credential_name,
            pool_header_name,
            pool_member_bindings,
        )

        pool = binding.backend_pool
        if (
            pool is None or not installation.pool_enabled or binding.runtime_id is None
            or binding.oauth is not None or binding.api_format is ApiFormat.OPENAI_IMAGES
        ):
            raise PolicyCompilationError("Fixed route native Pool capability is unavailable")
        target = (
            config.settings.azure_subscription_id,
            config.settings.apim_resource_group,
            config.settings.apim_service_name,
        )
        if not all(target):
            raise PolicyCompilationError("Fixed route native Pool target is incomplete")
        resource_base = (
            f"/subscriptions/{config.settings.azure_subscription_id}/resourceGroups/{config.settings.apim_resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{config.settings.apim_service_name}"
        )
        breaker_contract = pool.rate_limit.circuit_breaker
        breaker = BackendCircuitBreakerResource(
            breaker_contract.failure_count, breaker_contract.interval_seconds,
            breaker_contract.trip_duration_seconds, breaker_contract.accept_retry_after,
            tuple((item.minimum, item.maximum) for item in breaker_contract.status_code_ranges),
            breaker_contract.error_reasons,
        )
        resources: list[BackendResource] = []
        members: list[RoutePoolMember] = []
        versions: dict[str, str] = {}
        public_headers: dict[str, str] = {}
        observer = bool(config.settings.apim_usage_observer_url)
        for source, member in zip(pool_member_bindings(binding), pool.members, strict=True):
            deployment = source.model.upstream_model_id
            deployment_override = deployment != binding.model.upstream_model_id
            if deployment_override and not observer:
                raise PolicyCompilationError("Deployment-specific pools require the Observer")
            origin = _binding_route_origin(source)
            address = urlsplit(str(source.backend_url))
            _, _, credential = _binding_route_contract(
                source, credential_bindings=credential_bindings,
            )
            headers: list[tuple[str, str]] = []
            url = str(source.backend_url).rstrip("/")
            if observer:
                if not config.settings.apim_usage_observer_key_named_value:
                    raise PolicyCompilationError(
                        "Fixed route Pool Observer credential is unavailable",
                    )
                url = str(config.settings.apim_usage_observer_url).rstrip(
                    "/"
                ) + address.path.rstrip("/")
                host_name = pool_header_name(installation.program.scope, urlsplit(origin).netloc)
                runtime_name = pool_header_name(installation.program.scope, member.runtime_name)
                public_headers[host_name] = urlsplit(origin).netloc
                public_headers[runtime_name] = member.runtime_name
                headers.extend(
                    [
                        (
                            "x-adapter-key",
                            "{{" + config.settings.apim_usage_observer_key_named_value + "}}",
                        ),
                        ("x-turnstile-upstream-host", "{{" + host_name + "}}"),
                        ("x-hive-runtime", "{{" + runtime_name + "}}"),
                        ("x-turnstile-pool-runtime", "{{" + runtime_name + "}}"),
                    ]
                )
                if deployment_override:
                    deployment_name = pool_header_name(installation.program.scope, deployment)
                    public_headers[deployment_name] = deployment
                    headers.append(("x-turnstile-pool-deployment", "{{" + deployment_name + "}}"))
            if credential.secret_id:
                name = pool_credential_name(
                    source, installation.program.scope, credential.secret_id,
                )
                versions[name] = credential.secret_id
                prefix = "Bearer " if credential.header == "Authorization" else ""
                headers.append((credential.header, prefix + "{{" + name + "}}"))
            values = {
                "type": "Single", "protocol": "http", "url": url,
                "tls": {"validateCertificateChain": True, "validateCertificateName": True},
                "credentials": {"header": {name: [value] for name, value in headers}}
                if headers else {},
                "circuitBreaker": AzureApimPublisherClient._circuit_breaker_properties(breaker),
            }
            digest = document_digest(values)
            backend_id = "turnstile-route-member-" + document_digest({
                "scope": installation.program.scope.model_dump(mode="json"),
                "runtime_id": str(member.runtime_id), "dependency": digest,
            })[:40]
            resources.append(BackendResource(
                backend_id, member.runtime_name, url, tuple(headers), breaker,
            ))
            members.append(RoutePoolMember(
                runtime_id=str(member.runtime_id), backend_id=backend_id,
                dependency_sha256=digest, priority=member.priority, weight=member.weight,
            ))
        cookie = "TurnstileAffinity-" + text_digest(
            installation.program.scope.target_id + ":" + binding.model.model_key,
        )[:16] if pool.session_affinity else None
        snapshot = RouteBackendPool(
            members=tuple(members), rate_limit=pool.rate_limit,
            session_cookie_name=cookie, credential_versions=versions, header_values=public_headers,
            configuration_sha256=(document_digest(pool) if any(
                member.upstream_model_id is not None
                and member.upstream_model_id != binding.model.upstream_model_id
                for member in pool.members
            ) else None),
        )
        services: dict[str, Any] = {"services": [
            {"id": resource_base + "/backends/" + member.backend_id,
             "priority": member.priority + 1, "weight": member.weight} for member in members
        ]}
        if cookie is not None:
            services["sessionAffinity"] = {"sessionId": {"source": "cookie", "name": cookie}}
        dependency = document_digest({"type": "Pool", "pool": services})
        identity = document_digest({
            "scope": installation.program.scope.model_dump(mode="json"),
            "pool": snapshot.model_dump(mode="json"), "dependency": dependency,
        })
        _, paths, credential = _binding_route_contract(
            binding, credential_bindings=credential_bindings,
        )
        origin = _binding_route_origin(binding)
        count_path = binding_public_count_path(binding)
        revision = document_digest({
            "pool": identity, "origin": origin, "paths": paths,
            "credential": credential.model_dump(mode="json"), "observer": observer,
            "public_count_path": count_path,
        })
        connection = ConnectionRevision(
            scope=installation.program.scope, revision_id=str(uuid5(binding.runtime_id, revision)),
            backend_id="turnstile-route-pool-" + identity[:40],
            origin=origin,
            path=next(iter(paths.values())), protocol_paths=paths, credential=credential,
            dependency_sha256=dependency, observer=observer, pool=snapshot,
            public_count_path=count_path,
        )
        return connection, tuple(resources)

    def _connection_preparer(
        self, publication: GatewayPublication, installation: FixedProgramInstallation,
        config: ApimTargetClientConfig, management: FixedProgramManagement,
        worker_id: str, heartbeat: Callable[[], None],
    ) -> Callable[[GatewayModelBinding], tuple[ConnectionRevision, int]]:
        context = self._preparation_context(publication, installation)
        raw = publication.resource_manifest.get("route_preparation")
        if raw is not None and not isinstance(raw, dict):
            raise PolicyCompilationError("Route connection preparation checkpoint is invalid")
        checkpoint = (
            _PreparationCheckpoint.model_validate_json(canonical_document(raw))
            if raw is not None else _PreparationCheckpoint(context_sha256=context)
        )
        if checkpoint.context_sha256 != context:
            raise PolicyCompilationError("Route connection preparation context changed")
        stored = deepcopy(raw)
        issued: dict[str, int] = {}

        def prepare(binding: GatewayModelBinding) -> tuple[ConnectionRevision, int]:
            nonlocal checkpoint, stored
            if binding.backend_pool is not None:
                connection, backends = self._new_pool_connection(
                    binding, installation, config, self._credential_bindings(publication),
                )
            else:
                connection = self._new_connection(
                    binding, installation, config, self._credential_bindings(publication),
                )
                backends = ()
            key = connection.revision_id
            prior = checkpoint.connections.get(key)
            if prior is not None and prior.connection != connection:
                raise PolicyCompilationError("Route connection preparation changed")
            # Aliases sharing one connection must carry one identical approval.
            instant = prior.issued_epoch if prior is not None else issued.setdefault(
                key, self._clock(),
            )

            def validate_pool_intent(raw_intent: Mapping[str, Any]) -> dict[str, Any]:
                intent = _PoolWriteIntent.model_validate_json(canonical_document(raw_intent))
                pool = connection.pool
                if pool is None:
                    raise PolicyCompilationError("Fixed route Pool intent has no Pool")
                prefix = (
                    f"/subscriptions/{config.settings.azure_subscription_id}"
                    f"/resourceGroups/{config.settings.apim_resource_group}"
                    f"/providers/Microsoft.ApiManagement/service/{config.settings.apim_service_name}"
                )
                targets = {
                    prefix + "/backends/" + member.backend_id: (
                        member.dependency_sha256, "2024-05-01",
                    ) for member in pool.members
                }
                targets[prefix + "/backends/" + connection.backend_id] = (
                    connection.dependency_sha256,
                    "2025-09-01-preview" if pool.session_cookie_name else "2024-05-01",
                )
                targets.update({prefix + "/namedValues/" + name: (
                    document_digest({"displayName": name, "secret": True,
                                     "keyVault": {"secretIdentifier": secret_id}}),
                    "2024-05-01",
                ) for name, secret_id in pool.credential_versions.items()})
                targets.update({prefix + "/namedValues/" + name: (
                    document_digest({"displayName": name, "secret": False, "value": value}),
                    "2024-05-01",
                ) for name, value in pool.header_values.items()})
                document = json.loads(intent.body)
                expected = targets.get(intent.resource_id)
                if (
                    not isinstance(document, dict) or set(document) != {"properties"}
                    or not isinstance(document["properties"], dict) or expected is None
                    or canonical_document(document) != intent.body
                    or intent.body_sha256 != text_digest(intent.body)
                    or intent.connection_sha256 != document_digest(connection)
                    or intent.api_version != expected[1]
                    or document_digest(document["properties"]) != expected[0]
                ):
                    raise PolicyCompilationError("Fixed route Pool request binding changed")
                return intent.model_dump(mode="json")

            if prior is not None and connection.pool is not None:
                requests = prior.intent.get("requests")
                if (set(prior.intent) != {"requests"} or not isinstance(requests, dict)
                    or not 0 < len(requests) <= 151):
                    raise PolicyCompilationError("Fixed route Pool request history is invalid")
                for resource_id, intent in requests.items():
                    if not isinstance(intent, dict):
                        raise PolicyCompilationError("Fixed route Pool request history is invalid")
                    if validate_pool_intent(intent)["resource_id"] != resource_id:
                        raise PolicyCompilationError("Fixed route Pool request identity changed")

            def persist(intent: dict[str, Any]) -> None:
                nonlocal checkpoint, stored
                candidate_intent = deepcopy(intent)
                if connection.pool is not None:
                    candidate_intent = validate_pool_intent(intent)
                    state = checkpoint.connections.get(key)
                    requests = dict(state.intent.get("requests", {})) if state is not None else {}
                    resource_id = intent.get("resource_id")
                    body = intent.get("body")
                    if (
                        not isinstance(resource_id, str) or not isinstance(body, str)
                        or intent.get("connection_sha256") != document_digest(connection)
                        or intent.get("body_sha256") != text_digest(body)
                        or intent.get("method") != "PUT" or intent.get("if_none_match") != "*"
                    ):
                        raise PolicyCompilationError("Fixed route Pool request intent is invalid")
                    if resource_id in requests:
                        if requests[resource_id] != intent:
                            raise PolicyCompilationError("Fixed route Pool request intent changed")
                        raise RetryablePublicationError(
                            "Fixed route Pool requires read-only recovery",
                        )
                    if len(requests) >= 151:
                        raise PolicyCompilationError("Fixed route Pool request limit exceeded")
                    requests[resource_id] = candidate_intent
                    candidate_intent = {"requests": requests}
                elif prior is not None:
                    raise PolicyCompilationError("Route connection request is recovery-only")
                candidate = checkpoint.model_copy(update={"connections": {
                    **checkpoint.connections, key: _ConnectionPreparation(
                        connection=connection, issued_epoch=instant, intent=candidate_intent,
                    ),
                }})
                heartbeat()
                recorded = candidate.model_dump(mode="json")
                if not self._repository.checkpoint_gateway_publication_preparation(
                    publication.id, worker_id, stored, recorded,
                ):
                    raise RetryablePublicationError("Route connection preparation lease changed")
                checkpoint, stored = candidate, recorded

            heartbeat()
            if connection.pool is not None:
                management.prepare_route_pool(connection, backends, persist)
                observed = connection.dependency_sha256
            else:
                observed = management.prepare_route_connection(
                    connection, persist, recovery_only=prior is not None,
                )
            if observed != connection.dependency_sha256:
                raise PolicyCompilationError("Route prepared dependency readback changed")
            return connection, instant

        return prepare

    def _rollback_source(
        self, publication: GatewayPublication, previous: GatewayPublication,
        installation: FixedProgramInstallation, baseline: RouteSnapshot,
    ) -> tuple[GatewayPublication, RouteSnapshot]:
        from ..services.route_publication import recorded_route_snapshot

        detail = publication.resource_manifest.get("route_rollback")
        if detail is None:
            return previous, baseline
        if (
            not isinstance(detail, dict)
            or set(detail) != {
                "operation_id", "target_release_id", "prior_release_id", "confirmation_sha256",
            }
            or publication.publication_kind is not PublicationKind.ROUTE_RECONCILE
            or detail["prior_release_id"] != str(previous.id)
            or publication.base_release_id != previous.id
        ):
            raise PolicyCompilationError("Fixed route rollback source binding is invalid")
        try:
            operation_id = UUID(str(detail["operation_id"]))
            target_id = UUID(str(detail["target_release_id"]))
        except ValueError:
            raise PolicyCompilationError("Fixed route rollback source binding is invalid") from None
        operation = self._repository.get_gateway_release_operation(operation_id)
        target_row = self._repository.get_gateway_publication(target_id)
        if (
            operation is None or target_row is None or target_id == previous.id
            or operation["id"] != operation_id or operation["operation_kind"] != "rollback"
            or str(operation["gateway_profile_id"]) != str(publication.gateway_profile_id)
            or str(operation["target_release_id"]) != str(target_id)
            or str(operation["prior_release_id"]) != str(previous.id)
            or operation["confirmation_sha256"] != detail["confirmation_sha256"]
            or operation["checkpoint"].get("route_rollback_publication_id") != str(publication.id)
        ):
            raise PolicyCompilationError("Fixed route rollback operation binding changed")
        target = GatewayPublication.model_validate(target_row)
        if any(marker in target.resource_manifest for marker in (
            "model_route_addition", "model_route_removal",
        )):
            raise PolicyCompilationError(
                "Model-scoped release cannot restore a gateway configuration",
            )
        spec = {**target.desired_spec.model_dump(mode="json"), "removed_models": []}
        if (
            target.id != target_id or target.gateway_profile_id != publication.gateway_profile_id
            or target.resource_manifest.get("fixed_route_program")
            != installation.model_dump(mode="json")
            or publication.desired_spec.model_dump(mode="json") != spec
            or publication.desired_spec_sha256 != document_digest({
                "route_rollback": detail, "desired_spec": spec,
            })
        ):
            raise PolicyCompilationError("Fixed route rollback specification binding changed")
        snapshot, _ = recorded_route_snapshot(target)
        return target, snapshot

    def prepare(
        self, publication: GatewayPublication, worker_id: str, heartbeat: Callable[[], None],
    ) -> RoutePublicationTask:
        from ..services.route_publication import build_route_publication_task

        if "model_route_addition" in publication.resource_manifest:
            return self._prepare_model_addition(publication, worker_id, heartbeat)
        if publication.base_release_id is None:
            raise PolicyCompilationError(
                "Fixed route preparation requires an installed base release",
            )
        row = self._repository.get_gateway_publication(publication.base_release_id)
        if row is None:
            raise PolicyCompilationError("Fixed route base publication is unavailable")
        previous = GatewayPublication.model_validate(row)
        if previous.id != publication.base_release_id:
            raise PolicyCompilationError("Fixed route base publication changed")
        raw_migration = publication.resource_manifest.get("fixed_route_migration")
        if raw_migration is not None and not isinstance(raw_migration, dict):
            raise PolicyCompilationError("Fixed route migration descriptor is invalid")
        migration = FixedProgramMigration.model_validate_json(canonical_document(raw_migration)) \
            if raw_migration is not None else None
        if migration is not None:
            migration.require_candidate(publication, previous)
            installation, config = self.installed(
                publication.gateway_profile_id, publication.base_release_id, migration=migration,
            )
        else:
            installation, config = self.installed(
                publication.gateway_profile_id, publication.base_release_id,
            )
        manifest = previous.resource_manifest
        raw_task = manifest.get("route_configuration")
        snapshot: RouteSnapshot | None = None
        previous_head: ActiveRouteHead | None = None
        previous_program_sha256: str | None = None
        if migration is not None:
            dependency_source = previous.model_copy(
                update={"desired_spec": publication.desired_spec}
            )
            dependency_snapshot = None
        elif isinstance(raw_task, dict):
            base_task = RoutePublicationTask.model_validate_json(canonical_document(raw_task))
            raw_activation = manifest.get("route_activation")
            if not isinstance(raw_activation, dict) or raw_activation.get("outcome") != "confirmed":
                raise PolicyCompilationError("Fixed route base activation is not confirmed")
            previous_head = RouteActivationIntent.from_record(raw_activation["intent"]).head
            snapshot = base_task.plan.snapshot
        else:
            raw_snapshot = manifest.get("fixed_route_snapshot")
            raw_head = manifest.get("fixed_route_head")
            if not isinstance(raw_snapshot, dict) or not isinstance(raw_head, dict):
                raise PolicyCompilationError("Fixed route installation snapshot is unavailable")
            snapshot = RouteSnapshot.model_validate_json(canonical_document(raw_snapshot))
            previous_head = ActiveRouteHead.model_validate_json(canonical_document(raw_head))
        if migration is None:
            if snapshot is None or previous_head is None:
                raise PolicyCompilationError("Fixed route installation snapshot is unavailable")
            previous_head.approval.require_same_configuration(snapshot.approval)
            snapshot = snapshot.model_copy(update={"approval": previous_head.approval})
            snapshot.require_valid(previous_head.activated_epoch)
            prior_program = previous_head.approval.required_program_sha256
            if prior_program != installation.program.program_sha256:
                if prior_program not in {
                    installation.legacy_program_sha256,
                    *installation.predecessor_program_sha256,
                }:
                    raise PolicyCompilationError("Fixed route previous program is not approved")
                previous_program_sha256 = prior_program
            dependency_source, dependency_snapshot = self._rollback_source(
                publication, previous, installation, snapshot,
            )
        self._preflight_dependencies(publication, installation)
        heartbeat()
        open_options = (
            {"expected_current_revision": migration.previous_revision} if migration else {}
        )
        with self._open(
            installation, config,
            expected_current_revision=open_options.get("expected_current_revision"),
        ) as (port, management):
            current = port.projection.read_active()
            if (current[0] if current is not None else None) != previous_head:
                raise PolicyCompilationError("Fixed route base head changed in Storage")
            if migration is not None and port.projection.read_writer() is not None:
                raise PolicyCompilationError("Fixed route migration partition already has a writer")
            from .route_publication import pool_member_bindings

            has_deployment_override = any(
                source.model.upstream_model_id != binding.model.upstream_model_id
                for binding in publication.desired_spec.bindings
                for source in pool_member_bindings(binding)
            )
            observed_proof = (
                port.projection.read_program_proof(installation.program)
                if migration is None
                else None
            )
            now_epoch = self._clock()
            if (
                observed_proof is not None
                and observed_proof[0].identity == installation.program
                and observed_proof[0].issued_epoch <= now_epoch
                and observed_proof[0].expires_epoch > now_epoch + 30
            ):
                proof = observed_proof[0]
            else:
                proof = port._verifier.verify()
                if migration is None:
                    latest_proof = (
                        observed_proof
                        if observed_proof is not None
                        else port.projection.read_program_proof(installation.program)
                    )
                    port.projection.publish_program_proof(
                        proof,
                        installation.program,
                        latest_proof[1] if latest_proof is not None else None,
                        self._clock(),
                    )
            if has_deployment_override:
                if port._verifier._verified_parent is None:
                    port._verifier._verified_parent = management.revision_api_policy(
                        installation.program.api_revision,
                    )
                port._verifier.require_deployment_pool_reader()
                from ..integrations.gateway_connection_probe import verify_pool_deployment_observer

                observer = config.settings.apim_usage_observer_url
                key_name = config.settings.apim_usage_observer_key_named_value
                if not observer or not key_name:
                    raise PolicyCompilationError("Deployment-specific pools require the Observer")
                management.verify_observer_permissions(key_name)
                verify_pool_deployment_observer(
                    port._client, str(observer),
                    management.route_credential_value(key_name).get_secret_value(),
                )
            publication, credentials = self._prepare_credentials(
                publication, dependency_source, installation, management, worker_id, heartbeat,
            )
            self._validate_pool_deployments(
                publication, management, port._client, installation, previous=dependency_source,
            )
            publication = self._prepare_oauth(
                publication, dependency_source, installation, management, worker_id, heartbeat,
            )
            previous_by_alias = {
                item.model.model_key: item for item in dependency_source.desired_spec.bindings
            }
            prepare_changed_aliases = (
                frozenset(
                    binding.model.model_key
                    for binding in publication.desired_spec.bindings
                    if previous_by_alias.get(binding.model.model_key) != binding
                )
                if migration is None
                and "route_rollback" not in publication.resource_manifest
                and dependency_snapshot is not None
                else None
            )
            connections = self.approved_connections(
                publication, installation, management, baseline=dependency_snapshot,
                initial_migration=migration is not None,
                prepare_connection=(None if dependency_source.id != previous.id
                                    else self._connection_preparer(
                    publication, installation, config, management, worker_id, heartbeat,
                )),
                changed_aliases=prepare_changed_aliases,
            )
            dependencies = {
                "version": 1,
                "context_sha256": self._dependency_context(publication, installation),
                "connections": {
                    alias: approval.model_dump(mode="json")
                    for alias, approval in connections.items()
                },
            }
            previous_dependencies = publication.resource_manifest.get("route_dependencies")
            if previous_dependencies is None:
                heartbeat()
                if not self._repository.checkpoint_gateway_publication_dependencies(
                    publication.id, worker_id, None, dependencies,
                ):
                    raise PolicyCompilationError("Route dependency preparation lease changed")
            elif previous_dependencies != dependencies:
                raise PolicyCompilationError(
                    "Route dependency preparation changed after checkpoint",
                )
            registry = self._repository.registry()
            budgets = self._budget_contracts(publication, previous, registry)
            heartbeat()
            task = build_route_publication_task(
                publication, installation.program, proof, previous_head,
                snapshot.routes if snapshot else (),
                budgets, connections, installation.storage_endpoint, installation.storage_table,
                installation.target_identity_sha256, self._clock(),
                previous_members=snapshot.members if snapshot else (),
                credential_bindings=credentials,
                configuration_only=(
                    migration is None and "route_rollback" not in publication.resource_manifest
                ),
                previous_program_sha256=previous_program_sha256,
            )
            if migration is None:
                runtime_ids = {binding.runtime_id for binding in publication.desired_spec.bindings}
                self._verified_connections.add(document_digest({
                    "task": task.context_sha256, "installation": document_digest(installation),
                    "source": str(dependency_source.id),
                    "credentials": self._credential_bindings(dependency_source),
                    "runtime_configurations": {
                        str(row["id"]): row.get("config") or {}
                        for row in registry["runtimes"] if row["id"] in runtime_ids
                    },
                }))
            return task

    def remove_model(
        self, publication: GatewayPublication, worker_id: str, heartbeat: Callable[[], None],
    ) -> GatewayPublication:
        from ..persistence.repository_support import _model_removal_manifest
        from ..services.control_plane import GatewayControlPlaneService
        from ..services.route_publication import recorded_route_snapshot

        marker = publication.resource_manifest.get("model_route_removal")
        if (not isinstance(marker, dict)
            or publication.publication_kind is not PublicationKind.MODEL_REMOVE
            or publication.base_release_id is None):
            raise PolicyCompilationError("Independent model removal intent is invalid")
        source_row = self._repository.get_gateway_publication(UUID(marker["source_publication_id"]))
        previous = self._repository.effective_gateway_publication(publication.gateway_profile_id)
        payload, _ = GatewayControlPlaneService._release_payload(publication.desired_spec)
        _model_removal_manifest(previous, source_row, payload, publication.desired_spec_sha256,
                                {"model_route_removal": marker})
        source = GatewayPublication.model_validate(source_row)
        snapshot, head = recorded_route_snapshot(source)
        if (len(snapshot.routes) != 1 or snapshot.routes[0].model_id != marker["model_id"]
                or snapshot.routes[0].alias != marker["alias"]):
            raise PolicyCompilationError("Independent model removal source changed")
        installed, config = self.installed(
            publication.gateway_profile_id, publication.base_release_id,
        )
        if not installed.model_additions_enabled:
            raise PolicyCompilationError("Independent model removal program is unavailable")
        programs = self.serving_programs(source) if installed.native_gateway_scope else (installed,)
        if len({node.program.scope for node in programs}) > 1:
            return self._remove_model_scopes(
                publication, source, source_row, config, programs, worker_id, heartbeat,
            )
        if any(item.program.scope != head.approval.scope for item in programs):
            raise PolicyCompilationError("Model removal serving scope changed")
        installed = programs[0]
        fence = self._active_fence(publication.gateway_profile_id)
        if (fence.get("mode") != "active" or fence.get("protocol_version") != 2
                or fence.get("gateway_profile_id") != str(publication.gateway_profile_id)
                or fence.get("epoch") != snapshot.approval.budget_epoch):
            raise PolicyCompilationError("Independent model removal budget fence changed")
        with self._open(installed, config) as (port, _):
            proof = port._verifier.verify()
            instant = self._clock()
            if not proof.issued_epoch <= instant < proof.expires_epoch:
                raise RetryablePublicationError("Model removal program proof is expired")
            for node in programs:
                node_proof = port.projection.read_program_proof(node.program)
                if installed.native_gateway_scope and (
                    node_proof is None or node_proof[0].identity != node.program
                    or not node_proof[0].issued_epoch <= instant < node_proof[0].expires_epoch
                ):
                    raise RetryablePublicationError(
                        "Model removal requires every serving node proof",
                    )
            observed = port.projection.read_model_active(marker["alias"])
            expected = ModelRouteHead(
                publication_id=str(source.id), model_id=marker["model_id"], alias=marker["alias"],
                display_name=snapshot.routes[0].display_name,
                created_at=snapshot.routes[0].created_at,
                content_sha256=snapshot.routes[0].sha256, head=head,
            )
            phase = publication.status.value
            if phase != "promoting":
                if observed is None or observed[0] != expected:
                    raise PolicyCompilationError("Independent model removal head changed")
                port.projection.require_current_connections(snapshot, installed.program, instant)
                next_phase = {"queued": "validating", "validating": "provisioning",
                              "provisioning": "building_revision", "building_revision": "verifying",
                              "verifying": "promoting"}.get(phase)
                if next_phase is None:
                    raise PolicyCompilationError("Independent model removal phase is invalid")
                manifest = {**publication.resource_manifest,
                            "route_last_progress_attempt": publication.attempt_count}
                if phase == "verifying":
                    manifest["route_activation"] = {
                        "context_sha256": document_digest(marker), "outcome": "prepared",
                        "intent": {
                            "kind": "model_removal", "head": expected.model_dump(mode="json"),
                            "etag": observed[1],
                            "program_sha256": installed.program.program_sha256,
                        },
                    }
                heartbeat()
                row = self._repository.transition_gateway_publication(
                    publication.id, phase, next_phase,
                    {"resource_manifest": manifest, "apim_revision": installed.program.api_revision,
                     "policy_sha256": installed.program.program_sha256}, worker_id,
                )
                if row is None:
                    raise RetryablePublicationError("Independent model removal lease changed")
                return GatewayPublication.model_validate(row)
            activation = publication.resource_manifest.get("route_activation")
            if (not isinstance(activation, dict)
                    or activation.get("context_sha256") != document_digest(marker)
                    or activation.get("intent", {}).get("head") != expected.model_dump(mode="json")
                    or activation["intent"].get("program_sha256")
                    != installed.program.program_sha256):
                raise PolicyCompilationError("Independent model removal activation changed")
            if activation.get("outcome") == "prepared":
                if observed is None or observed != (expected, activation["intent"]["etag"]):
                    raise PolicyCompilationError("Independent model removal head changed")
                dispatched = {**activation, "outcome": "unknown_no_replay"}
                heartbeat()
                if not self._repository.checkpoint_gateway_publication_activation(
                    publication.id, worker_id, activation, dispatched,
                ):
                    raise RetryablePublicationError("Model removal intent lease changed")
                try:
                    port.projection.remove_model_head(expected, activation["intent"]["etag"])
                except RouteProjectionConflict as error:
                    from ..services.route_publication import RouteActivationNotApplied

                    conflict = {**dispatched, "outcome": "conditional_conflict"}
                    if not self._repository.checkpoint_gateway_publication_activation(
                        publication.id, worker_id, dispatched, conflict,
                    ):
                        raise RetryablePublicationError(
                            "Model removal conflict lease changed",
                        ) from error
                    raise RouteActivationNotApplied("Model removal was not applied") from error
                except Exception as error:
                    raise RetryablePublicationError(
                        "Model removal outcome requires readback",
                    ) from error
                activation = dispatched
            if activation.get("outcome") not in {"unknown_no_replay", "confirmed"}:
                raise PolicyCompilationError("Model removal outcome requires explicit recovery")
            if port.projection.read_model_active(marker["alias"]) is not None:
                raise RetryablePublicationError("Model removal outcome is unconfirmed; no replay")
            for node in programs:
                with self._open(node, config) as (peer, _):
                    peer.require_model_absent(marker["alias"])
            confirmed = {**activation, "outcome": "confirmed"}
            if (activation != confirmed
                    and not self._repository.checkpoint_gateway_publication_activation(
                publication.id, worker_id, activation, confirmed,
            )):
                raise RetryablePublicationError("Model removal confirmation lease changed")

        def validate(
            row: Mapping[str, Any], base: Mapping[str, Any] | None, upgrade: Any,
        ) -> None:
            _model_removal_manifest(base, source_row, payload, publication.desired_spec_sha256,
                                    {"model_route_removal": marker})
            if row.get("resource_manifest", {}).get("route_activation") != confirmed:
                raise PolicyCompilationError("Model removal transaction receipt changed")
            if self._active_fence(publication.gateway_profile_id) != fence:
                raise PolicyCompilationError("Model removal transaction budget fence changed")
            for node in programs:
                with self._open(node, config) as (current_port, _):
                    current_port._verifier.verify()
                    current_port.require_model_absent(marker["alias"])

        heartbeat()
        return GatewayPublication.model_validate(self._repository.activate_gateway_publication(
            publication.id, worker_id, validate_model_activation=validate,
        ))

    def _validate_deployment(
        self,
        publication: GatewayPublication,
        management: FixedProgramManagement,
        client: httpx.Client,
        installation: FixedProgramInstallation,
        *, binding: GatewayModelBinding | None = None,
        previous: GatewayPublication | None = None,
    ) -> dict[str, Any] | None:
        from ..integrations.gateway_connection_probe import foundry_deployment_metadata
        from .route_publication import route_credential_source

        binding = binding or publication.desired_spec.bindings[0]
        project = binding.runtime_config.get("project_endpoint")
        if (
            binding.runtime_id is not None
            and (project is None or "/api/projects/" not in str(project))
        ):
            runtime = next(
                (
                    row
                    for row in self._repository.registry()["runtimes"]
                    if row["id"] == binding.runtime_id
                ),
                None,
            )
            runtime_project = (
                (runtime.get("config") or {}).get("project_endpoint") if runtime else None
            )
            if runtime_project:
                project = runtime_project
        if project is None:
            return None

        def headers() -> dict[str, str]:
            assert binding is not None
            if binding.auth_strategy in {
                AuthStrategy.NAMED_VALUE_API_KEY,
                AuthStrategy.NAMED_VALUE_BEARER,
            }:
                materialized_secret_id = (
                    str(binding.key_vault_secret_id) if binding.key_vault_secret_id is not None
                    else self._credential_bindings(publication).get(
                        route_credential_source(binding),
                    )
                )
                encrypted = self._repository.gateway_publication_credential(
                    publication.id,
                ) if materialized_secret_id is None else None
                if encrypted is not None and self._cipher is not None:
                    key = self._cipher.decrypt(encrypted)
                    if key is None:
                        raise PolicyCompilationError(
                            "Deployment metadata credential is unavailable"
                        )
                else:
                    secret_id = materialized_secret_id
                    if secret_id is None and previous is not None:
                        source = route_credential_source(binding)
                        secret_id = self._inherited_credential_bindings(
                            publication, previous,
                        ).get(source)
                        if secret_id is None and any(
                            prior.auth_strategy in {
                                AuthStrategy.NAMED_VALUE_API_KEY,
                                AuthStrategy.NAMED_VALUE_BEARER,
                            } and route_credential_source(prior) == source
                            for prior in self._previous_credential_sources(publication, previous)
                        ):
                            assert binding.named_value_name is not None
                            return {"api-key": management.route_credential_value(
                                binding.named_value_name,
                            ).get_secret_value()}
                    if secret_id is None:
                        raise CredentialSourceMissingError(str(binding.named_value_name))
                    self._require_credential_vault(secret_id, installation)
                    if self._cipher is None or installation.credential_vault_origin is None:
                        raise PolicyCompilationError("Deployment credential vault is unavailable")
                    vault = RouteCredentialVault(
                        installation.credential_vault_origin, self._cipher, self._tokens, client,
                    )
                    key = vault.verify(secret_id).get_secret_value()
                return {"api-key": key}
            if binding.auth_strategy is AuthStrategy.MANAGED_IDENTITY:
                return {"Authorization": "Bearer " + self._tokens.token("https://ai.azure.com")}
            raise PolicyCompilationError("Deployment metadata authentication is unsupported")

        return foundry_deployment_metadata(
            client, str(project), str(binding.backend_url),
            binding.model.upstream_model_id, headers,
        )

    def _validate_pool_deployments(
        self, publication: GatewayPublication, management: FixedProgramManagement,
        client: httpx.Client, installation: FixedProgramInstallation,
        *, previous: GatewayPublication | None = None,
    ) -> None:
        from ..integrations.gateway_connection_probe import foundry_model_identity
        from .route_publication import pool_member_bindings

        previous_bindings = {
            item.model.model_key: item for item in previous.desired_spec.bindings
        } if previous is not None else {}
        for binding in publication.desired_spec.bindings:
            if previous_bindings.get(binding.model.model_key) == binding:
                continue
            pool = binding.backend_pool
            if pool is None or binding.provider_brand_key.value != "microsoft_foundry" or not any(
                member.model_id is not None or (
                    member.upstream_model_id is not None
                    and member.upstream_model_id != binding.model.upstream_model_id
                )
                for member in pool.members
            ):
                continue
            identities = {
                foundry_model_identity(self._validate_deployment(
                    publication, management, client, installation, binding=source,
                ))
                for source in pool_member_bindings(binding)
            }
            if len(identities) != 1:
                raise PolicyCompilationError("Pool deployments must use the same model and version")

    def _remove_model_scopes(
        self, publication: GatewayPublication, source: GatewayPublication,
        source_row: Mapping[str, Any] | None, config: ApimTargetClientConfig,
        programs: tuple[FixedProgramInstallation, ...], worker_id: str,
        heartbeat: Callable[[], None],
    ) -> GatewayPublication:
        from ..persistence.repository_support import _model_removal_manifest
        from ..services.control_plane import GatewayControlPlaneService
        from ..services.route_publication import (
            RouteActivationNotApplied,
            recorded_route_scopes,
            recorded_route_snapshot,
        )

        marker = publication.resource_manifest.get("model_route_removal")
        if not isinstance(marker, dict):
            raise PolicyCompilationError("Model scope removal intent is unavailable")
        groups: dict[RouteScope, list[FixedProgramInstallation]] = {}
        for node in programs:
            groups.setdefault(node.program.scope, []).append(node)
        if set(groups) != set(recorded_route_scopes(source)):
            raise PolicyCompilationError("Model scope removal serving set changed")
        fence = self._active_fence(publication.gateway_profile_id)
        if (fence.get("mode") != "active" or fence.get("protocol_version") != 2
                or fence.get("gateway_profile_id") != str(publication.gateway_profile_id)):
            raise PolicyCompilationError("Model scope removal budget fence changed")
        with ExitStack() as readers:
            ports: dict[str, list[FixedRoutePublisherPort]] = {}
            heads: dict[str, ModelRouteHead] = {}
            observed: dict[str, tuple[ModelRouteHead, str] | None] = {}
            for scope, nodes in groups.items():
                snapshot, head = recorded_route_snapshot(source, scope=scope)
                route = snapshot.routes[0]
                if (len(snapshot.routes) != 1 or route.model_id != marker["model_id"]
                        or route.alias != marker["alias"]
                        or fence.get("epoch") != snapshot.approval.budget_epoch):
                    raise PolicyCompilationError("Model scope removal source changed")
                peers = []
                for node in nodes:
                    port, _ = readers.enter_context(self._open(node, config))
                    proof = port.projection.read_program_proof(node.program)
                    now = self._clock()
                    if (proof is None or proof[0].identity != node.program
                            or not proof[0].issued_epoch <= now < proof[0].expires_epoch):
                        raise RetryablePublicationError("Model removal requires every node proof")
                    peers.append(port)
                ports[scope.partition] = peers
                heads[scope.partition] = ModelRouteHead(
                    publication_id=str(source.id), model_id=route.model_id, alias=route.alias,
                    display_name=route.display_name, created_at=route.created_at,
                    content_sha256=route.sha256, head=head,
                )
                observed[scope.partition] = peers[0].projection.read_model_active(route.alias)
                if publication.status.value != "promoting":
                    observed_head = observed[scope.partition]
                    if observed_head is None or observed_head[0] != heads[scope.partition]:
                        raise PolicyCompilationError("Model scope removal head changed")
                    peers[0].projection.require_current_connections(snapshot, nodes[0].program,
                                                                    self._clock())
            phase = publication.status.value
            if phase != "promoting":
                next_phase = {"queued": "validating", "validating": "provisioning",
                              "provisioning": "building_revision", "building_revision": "verifying",
                              "verifying": "promoting"}.get(phase)
                if next_phase is None:
                    raise PolicyCompilationError("Model scope removal phase is invalid")
                manifest = {**publication.resource_manifest,
                            "route_last_progress_attempt": publication.attempt_count}
                if phase == "verifying":
                    plans = {}
                    for scope, nodes in groups.items():
                        current = observed[scope.partition]
                        if current is None:
                            raise PolicyCompilationError("Model scope removal head is unavailable")
                        plans[scope.partition] = {
                            "head": heads[scope.partition].model_dump(mode="json"),
                            "etag": current[1],
                            "installations": {node.program.instance_id: document_digest(node)
                                              for node in nodes},
                        }
                    manifest["route_activation"] = {
                        "context_sha256": document_digest(marker), "outcome": "prepared",
                        "intent": {"kind": "model_removal", "scopes": plans},
                    }
                heartbeat()
                row = self._repository.transition_gateway_publication(
                    publication.id, phase, next_phase, {"resource_manifest": manifest,
                        "apim_revision": programs[0].program.api_revision,
                        "policy_sha256": programs[0].program.program_sha256}, worker_id,
                )
                if row is None:
                    raise RetryablePublicationError("Model scope removal lease changed")
                return GatewayPublication.model_validate(row)
            raw = publication.resource_manifest.get("route_activation")
            if (not isinstance(raw, dict) or raw.get("context_sha256") != document_digest(marker)
                    or not isinstance(raw.get("intent"), dict)
                    or raw["intent"].get("kind") != "model_removal"
                    or set(raw["intent"].get("scopes", {})) != set(heads)
                    or raw.get("outcome") not in {"prepared", "unknown_no_replay", "confirmed"}):
                raise PolicyCompilationError("Model scope removal activation changed")
            activation = dict(raw)
            for scope, nodes in groups.items():
                key = scope.partition
                plan = activation["intent"]["scopes"][key]
                if (plan.get("head") != heads[key].model_dump(mode="json")
                        or plan.get("installations") != {
                            node.program.instance_id: document_digest(node) for node in nodes
                        }):
                    raise PolicyCompilationError("Model scope removal installation changed")
                state = activation.get("scopes", {}).get(key)
                if state is None:
                    if observed[key] != (heads[key], plan["etag"]):
                        raise PolicyCompilationError("Model scope removal conditional head changed")
                    dispatched = {**activation, "outcome": "unknown_no_replay", "scopes": {
                        **activation.get("scopes", {}), key: "unknown_no_replay",
                    }}
                    heartbeat()
                    if not self._repository.checkpoint_gateway_publication_activation(
                        publication.id, worker_id, activation, dispatched,
                    ):
                        raise RetryablePublicationError("Model scope removal intent lease changed")
                    activation = dispatched
                    try:
                        ports[key][0].projection.remove_model_head(heads[key], plan["etag"])
                    except RouteProjectionConflict as error:
                        conflict = {**activation, "scopes": {
                            **activation["scopes"], key: "conditional_conflict",
                        }}
                        if not self._repository.checkpoint_gateway_publication_activation(
                            publication.id, worker_id, activation, conflict,
                        ):
                            raise RetryablePublicationError(
                                "Model scope conflict lease changed",
                            ) from error
                        raise RouteActivationNotApplied(
                            "Model scope removal was not applied",
                        ) from error
                    except Exception as error:
                        raise RetryablePublicationError(
                            "Model scope removal requires readback",
                        ) from error
                elif state not in {"unknown_no_replay", "confirmed"}:
                    raise PolicyCompilationError("Model scope removal requires explicit recovery")
                if ports[key][0].projection.read_model_active(marker["alias"]) is not None:
                    raise RetryablePublicationError("Model scope removal is unconfirmed; no replay")
                for peer in ports[key]:
                    peer.require_model_absent(marker["alias"])
                confirmed = {**activation, "scopes": {**activation["scopes"], key: "confirmed"}}
                if confirmed != activation:
                    if not self._repository.checkpoint_gateway_publication_activation(
                        publication.id, worker_id, activation, confirmed,
                    ):
                        raise RetryablePublicationError("Model scope confirmation lease changed")
                    activation = confirmed
            confirmed = {**activation, "outcome": "confirmed"}
            if confirmed != activation:
                committed = self._repository.checkpoint_gateway_publication_activation(
                    publication.id, worker_id, activation, confirmed,
                )
                if not committed:
                    raise RetryablePublicationError("Model scope final confirmation lease changed")
            payload, _ = GatewayControlPlaneService._release_payload(publication.desired_spec)

            def validate(
                row: Mapping[str, Any], base: Mapping[str, Any] | None, upgrade: Any,
            ) -> None:
                _model_removal_manifest(base, source_row, payload, publication.desired_spec_sha256,
                                        {"model_route_removal": marker})
                if (row.get("resource_manifest", {}).get("route_activation") != confirmed
                        or self._active_fence(publication.gateway_profile_id) != fence
                        or self.serving_programs(source) != programs):
                    raise PolicyCompilationError("Model scope removal transaction evidence changed")
                for scope_ports in ports.values():
                    for peer in scope_ports:
                        peer._verifier.verify()
                        peer.require_model_absent(marker["alias"])

            heartbeat()
            return GatewayPublication.model_validate(self._repository.activate_gateway_publication(
                publication.id, worker_id, validate_model_activation=validate,
            ))

    def _connection_source(
        self,
        publication: GatewayPublication,
        installation: FixedProgramInstallation,
        previous: GatewayPublication,
        *, projection: TableRouteProjection | None = None,
    ) -> tuple[GatewayPublication, RouteSnapshot | None]:
        from ..integrations.storage_route_projection import RouteProjectionConflict
        from ..services.route_publication import recorded_route_snapshot

        fields = {"runtime_id", "api_format", "backend_url", "backend_path", "auth_strategy",
                  "named_value_name", "key_vault_secret_id", "managed_identity_resource",
                  "oauth", "backend_pool"}
        expected = publication.desired_spec.bindings[0].model_dump(mode="json", include=fields)
        for row in self._repository.active_model_route_publications(publication.gateway_profile_id):
            source = GatewayPublication.model_validate(row)
            if (source.id == publication.id or source.status.value != "active"
                    or len(source.desired_spec.bindings) != 1
                    or source.desired_spec.bindings[0].model_dump(mode="json", include=fields)
                    != expected):
                continue
            snapshot, _ = recorded_route_snapshot(source)
            if (snapshot.approval.scope != installation.program.scope
                    or snapshot.approval.required_program_sha256 not in {
                        installation.program.program_sha256, installation.legacy_program_sha256,
                    }):
                continue
            instant = self._clock()
            if projection is not None:
                try:
                    projection.require_current_connections(snapshot, installation.program, instant)
                except (ValueError, RouteProjectionConflict):
                    continue
            elif any(member.connection_approval.state != "approved"
                     or member.connection_approval.issued_epoch > instant
                     for member in snapshot.members):
                continue
            return source, snapshot
        return previous, None

    def _prepare_model_addition(
        self, publication: GatewayPublication, worker_id: str, heartbeat: Callable[[], None],
    ) -> RoutePublicationTask:
        from ..persistence.repository_support import (
            _managed_program_publication,
            _model_addition_manifest,
        )
        from ..services.control_plane import GatewayControlPlaneService
        from ..services.route_publication import require_route_specification

        if publication.base_release_id is None or publication.publication_kind.value != "model_add":
            raise PolicyCompilationError("Model addition requires an installed program")
        previous_row = self._repository.get_gateway_publication(publication.base_release_id)
        gateways = self._repository.get_gateway_profiles([publication.gateway_profile_id])
        if len(gateways) != 1 or gateways[0]["id"] != publication.gateway_profile_id:
            raise PolicyCompilationError("Model addition Gateway is unavailable")
        previous_row = _managed_program_publication(previous_row, gateways[0].get("config") or {})
        payload, _ = GatewayControlPlaneService._release_payload(publication.desired_spec)
        _model_addition_manifest(previous_row, payload, publication.desired_spec_sha256, {
            "model_route_addition": publication.resource_manifest["model_route_addition"],
        })
        previous = GatewayPublication.model_validate(previous_row)
        installed, config = self.installed(
            publication.gateway_profile_id, publication.base_release_id,
        )
        if not installed.model_additions_enabled:
            raise PolicyCompilationError("Single-model publication program is not installed")
        programs = self.serving_programs(publication)
        groups: dict[RouteScope, list[FixedProgramInstallation]] = {}
        for node in programs:
            groups.setdefault(node.program.scope, []).append(node)
        if not 1 <= len(groups) <= 32:
            raise PolicyCompilationError("Model route scope batch exceeds its bound")
        registry = self._repository.registry()
        tasks, dependencies = [], {}
        credentials: dict[str, str] | None = None
        for scope, nodes in sorted(groups.items(), key=lambda item: (
            item[0].gateway_id != "managed", item[0].gateway_id,
        )):
            publication, task, dependency, credentials = self._prepare_model_scope(
                publication, previous, tuple(nodes), config, registry, worker_id, heartbeat,
                credentials=credentials, scope_only=len(groups) > 1,
            )
            tasks.append(task)
            dependencies[scope.partition] = dependency
        prepared = RoutePublicationTask.model_validate_json(canonical_document({
            **tasks[0].model_dump(mode="json"),
            **({"additional_scope_tasks": [task.model_dump(mode="json") for task in tasks[1:]]}
               if len(tasks) > 1 else {}),
        }))
        require_route_specification(publication, prepared)
        checkpoint = (dependencies[prepared.plan.program.scope.partition] if len(tasks) == 1 else
                      {"version": 2, "scopes": dependencies})
        recorded = publication.resource_manifest.get("route_dependencies")
        if recorded is None:
            heartbeat()
            if not self._repository.checkpoint_gateway_publication_dependencies(
                publication.id, worker_id, None, checkpoint,
            ):
                raise RetryablePublicationError("Model dependency preparation lease changed")
        elif recorded != checkpoint:
            raise PolicyCompilationError("Model dependency checkpoint changed")
        return prepared

    def _prepare_model_scope(
        self, publication: GatewayPublication, previous: GatewayPublication,
        programs: tuple[FixedProgramInstallation, ...], config: ApimTargetClientConfig,
        registry: Mapping[str, Sequence[dict[str, Any]]], worker_id: str,
        heartbeat: Callable[[], None], *, credentials: dict[str, str] | None,
        scope_only: bool,
    ) -> tuple[GatewayPublication, RoutePublicationTask, dict[str, Any], dict[str, str]]:
        from ..services.route_publication import build_route_publication_task

        installed = programs[0]
        self._preflight_dependencies(publication, installed, registry)
        with self._open(installed, config) as (port, management):
            alias = publication.desired_spec.bindings[0].model.model_key
            if port.projection.read_model_active(alias) is not None:
                raise PolicyCompilationError(
                    "Model addition is already active; read back its intent",
                )
            observed = port.projection.read_program_proof(installed.program)
            instant = self._clock()
            if (observed is None or observed[0].identity != installed.program
                    or not observed[0].issued_epoch <= instant < observed[0].expires_epoch):
                raise RetryablePublicationError(
                    "Installed program proof is not current; wait for independent renewal",
                )
            proof = observed[0]
            connection_source, connection_snapshot = self._connection_source(
                publication, installed, previous, projection=port.projection,
            )
            if credentials is None:
                self._validate_deployment(
                    publication, management, port._client, installed,
                    previous=connection_source,
                )
                publication, credentials = self._prepare_credentials(
                    publication, connection_source, installed, management, worker_id, heartbeat,
                    registry=registry,
                )
                publication = self._prepare_oauth(
                    publication, connection_source, installed, management, worker_id, heartbeat,
                )
            connections = self.approved_connections(
                publication, installed, management, baseline=connection_snapshot,
                prepare_connection=self._connection_preparer(
                    publication, installed, config, management, worker_id, heartbeat,
                ),
                registry=registry,
            )
            dependencies = {
                "version": 1, "context_sha256": self._dependency_context(publication, installed),
                "connections": {key: value.model_dump(mode="json")
                                for key, value in connections.items()},
            }
            task = build_route_publication_task(
                publication, installed.program, proof, None, (),
                self._budget_contracts(publication, previous, registry), connections,
                installed.storage_endpoint, installed.storage_table,
                installed.target_identity_sha256, self._clock(), credential_bindings=credentials,
                configuration_only=True, scope_only=scope_only,
            )
            if installed.native_gateway_scope:
                task = RoutePublicationTask.model_validate_json(canonical_document({
                    **task.model_dump(mode="json"),
                    "serving_programs": [item.program.model_dump(mode="json") for item in programs],
                    "serving_installation_sha256": {
                        item.program.instance_id: document_digest(item) for item in programs
                    },
                }))
            return publication, task, dependencies, credentials

    @contextmanager
    def resolve(self, task: RoutePublicationTask) -> Iterator[FixedRoutePublisherPort]:
        if task.base_publication_id is None:
            raise PolicyCompilationError("Fixed route task requires an installed base release")
        row = self._repository.get_gateway_publication(UUID(task.publication_id))
        if row is None:
            raise PolicyCompilationError("Fixed route task publication is unavailable")
        recorded = RoutePublicationTask.model_validate_json(canonical_document(
            row["resource_manifest"]["route_configuration"],
        ))
        if task not in recorded.scope_tasks:
            raise PolicyCompilationError("Fixed route scope task is not part of its publication")
        raw = row.get("resource_manifest", {}).get("fixed_route_migration")
        options: dict[str, Any] = {}
        open_options: dict[str, str] = {}
        if raw is not None:
            migration = FixedProgramMigration.model_validate_json(canonical_document(raw))
            previous_row = self._repository.get_gateway_publication(UUID(task.base_publication_id))
            if previous_row is None:
                raise PolicyCompilationError("Fixed route migration base is unavailable")
            migration.require_candidate(
                GatewayPublication.model_validate(row),
                GatewayPublication.model_validate(previous_row),
            )
            options["migration"] = migration
            open_options["expected_current_revision"] = migration.previous_revision
        installation, config = self.installed(
            UUID(task.plan.program.scope.target_id), UUID(task.base_publication_id), **options,
        )
        programs: tuple[FixedProgramInstallation, ...] = ()
        if task.serving_programs:
            current_programs = self.serving_programs(GatewayPublication.model_validate(row))
            programs = tuple(node for node in current_programs
                             if node.program.scope == task.plan.program.scope)
            if (tuple(item.program for item in programs) != task.serving_programs
                    or task.serving_installation_sha256 != {
                        item.program.instance_id: document_digest(item) for item in programs
                    }):
                raise PolicyCompilationError("Fixed route serving node enrollment changed")
            installation = programs[0]
        if task.plan.program != installation.program:
            raise PolicyCompilationError("Fixed route task program changed")
        if task.previous_program_sha256 is not None and task.previous_program_sha256 not in {
            installation.legacy_program_sha256, *installation.predecessor_program_sha256,
        }:
            raise PolicyCompilationError("Fixed route previous program approval changed")
        if task.model_addition and not installation.model_additions_enabled:
            raise PolicyCompilationError("Single-model publication program is not installed")
        with self._open(
            installation, config,
            expected_current_revision=open_options.get("expected_current_revision"),
            serving_programs=programs,
        ) as (port, _):
            if raw is not None and row.get("resource_manifest", {}).get(
                "route_program_promotion",
            ) is not None:
                promotion = row["resource_manifest"]["route_program_promotion"]
                context = document_digest({"migration": raw, "task": task.context_sha256,
                                           "publication_id": task.publication_id})
                if (promotion.get("context_sha256") != context
                        or promotion.get("revision") != task.plan.program.api_revision
                        or promotion.get("previous_revision") != migration.previous_revision
                        or promotion.get("outcome") not in {"started", "confirmed"}):
                    raise PolicyCompilationError("Fixed program recovery promotion intent changed")
                native = port._verifier._management.current_revision()
                if native not in {migration.previous_revision, task.plan.program.api_revision}:
                    raise PolicyCompilationError("Fixed program recovery current revision changed")
                port._verifier._expected_current_revision = native
            yield port

    @contextmanager
    def _open(
        self, installation: FixedProgramInstallation, config: ApimTargetClientConfig,
        *, expected_current_revision: str | None = None,
        serving_programs: tuple[FixedProgramInstallation, ...] = (),
    ) -> Iterator[tuple[FixedRoutePublisherPort, AzureApimPublisherClient]]:
        if config.settings.apim_probe_subscription_key is None:
            raise PolicyCompilationError("Fixed route Publisher credential is unavailable")
        if expected_current_revision is None:
            target_id = UUID(installation.program.scope.target_id)
            gateways = self._repository.get_gateway_profiles([target_id])
            record = (gateways[0].get("config") or {}).get(
                "managed_route_program_initialization",
            ) if len(gateways) == 1 and gateways[0]["id"] == target_id else None
            if record is not None:
                initialized = ManagedProgramInitialization.model_validate_json(
                    canonical_document(record),
                )
                previous = self._repository.effective_gateway_publication(target_id)
                initialized.require_previous(GatewayPublication.model_validate(previous))
                if initialized.installation != installation:
                    raise PolicyCompilationError("Managed program installation changed")
                expected_current_revision = initialized.previous_revision
        client_context = (nullcontext(self._client) if self._client is not None
                          else httpx.Client(timeout=10.0, follow_redirects=False))
        with client_context as client:
            management = AzureApimPublisherClient(
                config.settings, token_provider=self._tokens, client=client,
            )
            projection = TableRouteProjection(
                installation.storage_endpoint, installation.storage_table,
                installation.program.scope, token_provider=self._tokens, client=client,
            )
            verifier = FixedProgramVerifier(
                installation, management, config.settings.apim_probe_subscription_key, client,
                clock=self._clock,
                expected_current_revision=expected_current_revision,
            )
            yield FixedRoutePublisherPort(
                projection, verifier, config.settings.apim_probe_subscription_key, client,
                active_fence=partial(self._active_fence, config.gateway_profile_id),
                serving_verifiers=tuple(
                    FixedProgramVerifier(
                        peer, management, config.settings.apim_probe_subscription_key, client,
                        clock=self._clock, expected_current_revision=expected_current_revision,
                    )
                    for peer in serving_programs
                ),
                verify_connections=partial(
                    self._verify_connections, installation, management, projection=projection,
                ),
                prepare_availability=self._prepare_availability,
                clock=self._clock,
                absorbed_model_aliases=lambda: tuple(
                    str(
                        row.get("resource_manifest", {})
                        .get("model_route_addition", {})
                        .get("alias", "")
                    )
                    for row in self._repository.active_model_route_publications(
                        config.gateway_profile_id
                    )
                    if row.get("resource_manifest", {})
                    .get("model_route_addition", {})
                    .get("alias")
                ),
            ), management

    def _prepare_availability(self, task: RoutePublicationTask, route: RouteContent) -> bool:
        repository = cast("ModelAvailabilityRepository", self._repository)
        model_id = UUID(route.model_id)
        repository.stage_model_availability(model_id, UUID(task.publication_id))
        projected = self._availability_projector(model_id)
        current = repository.get_model_availability_state(model_id)
        if (
            current is None or current != projected or current.model_id != model_id
            or current.lifecycle == "deleted"
        ):
            raise PolicyCompilationError("Route publication availability changed")
        return current.enabled

    def _verify_connections(
        self, installation: FixedProgramInstallation, management: FixedProgramManagement,
        task: RoutePublicationTask,
        fence: Mapping[str, Any] | None = None,
        *, projection: TableRouteProjection | None = None,
    ) -> None:
        from ..services.route_publication import require_route_specification

        row = self._repository.get_gateway_publication(UUID(task.publication_id))
        if row is None:
            raise PolicyCompilationError("Fixed route publication is unavailable")
        publication = GatewayPublication.model_validate(row)
        task.require_publication(
            str(publication.id), str(publication.base_release_id) if publication.base_release_id
            else None, publication.desired_spec_sha256, str(publication.gateway_profile_id),
        )
        raw_task = publication.resource_manifest.get("route_configuration")
        if raw_task is None and not task.serving_programs and not task.additional_scope_tasks:
            recorded = task
        elif not isinstance(raw_task, dict):
            raise PolicyCompilationError("Fixed route dependency task is unavailable")
        else:
            recorded = RoutePublicationTask.model_validate_json(canonical_document(raw_task))
        if task not in recorded.scope_tasks:
            raise PolicyCompilationError("Fixed route dependency scope task changed")
        require_route_specification(publication, recorded)
        if task.credential_bindings != self._credential_bindings(publication):
            raise PolicyCompilationError("Route credential bindings changed after preparation")
        previous_row = (
            self._repository.get_gateway_publication(publication.base_release_id)
            if publication.base_release_id is not None else None
        )
        if previous_row is None:
            raise PolicyCompilationError("Fixed route budget baseline is unavailable")
        previous = GatewayPublication.model_validate(previous_row)
        if previous.id != publication.base_release_id:
            raise PolicyCompilationError("Fixed route budget baseline identity changed")
        registry = self._repository.registry()
        self._preflight_dependencies(publication, installation, registry)
        budgets = self._budget_contracts(publication, previous, registry, fence)
        if (
            str(budgets.epoch) != task.plan.snapshot.approval.budget_epoch
            or budgets.model_contracts != {
                route.alias: route.budget for route in task.plan.snapshot.routes
            }
        ):
            raise PolicyCompilationError("Fixed route budget contract changed after preparation")
        baseline = (
            RouteSnapshot(
                approval=task.previous_head.approval, routes=task.previous_routes,
                members=task.previous_members,
            ) if task.previous_head is not None else None
        )
        dependency_source = previous
        migration = publication.resource_manifest.get("fixed_route_migration")
        if migration is not None:
            if not isinstance(migration, dict):
                raise PolicyCompilationError("Fixed route migration descriptor is invalid")
            descriptor = FixedProgramMigration.model_validate_json(canonical_document(migration))
            descriptor.require_candidate(publication, previous)
            if baseline is not None or descriptor.installation != installation:
                raise PolicyCompilationError("Fixed route migration verification baseline changed")
            dependency_source = previous.model_copy(
                update={"desired_spec": publication.desired_spec},
            )
        if baseline is not None:
            dependency_source, baseline = self._rollback_source(
                publication, previous, installation, baseline,
            )
        elif task.model_addition:
            dependency_source, baseline = self._connection_source(
                publication, installation, previous, projection=projection,
            )
        runtime_ids = {binding.runtime_id for binding in publication.desired_spec.bindings}
        verification_key = document_digest({
            "task": task.context_sha256, "installation": document_digest(installation),
            "source": str(dependency_source.id),
            "credentials": self._credential_bindings(dependency_source),
            "runtime_configurations": {
                str(row["id"]): row.get("config") or {}
                for row in registry["runtimes"] if row["id"] in runtime_ids
            },
        })
        verified = (
            (task.configuration_only or migration is None)
            and verification_key in self._verified_connections
        )
        if verified:
            return
        changed_aliases = (
            frozenset(task.plan.changed_aliases)
            if migration is None and "route_rollback" not in publication.resource_manifest
            else None
        )
        self._verify_credentials(
            publication, dependency_source, installation, changed_aliases=changed_aliases,
        )
        approved = self.approved_connections(
            publication, installation, management, baseline=baseline,
            initial_migration=migration is not None,
            verify_readback=True,
            changed_aliases=changed_aliases,
            registry=registry,
        )
        if any(approved.get(member.alias) != member.connection_approval
               for member in task.plan.snapshot.members):
            raise PolicyCompilationError("Fixed route independent connection approval changed")
        if task.configuration_only or migration is None:
            self._verified_connections.add(verification_key)

    def approved_connections(
        self, publication: GatewayPublication, installation: FixedProgramInstallation,
        management: FixedProgramManagement,
        *, baseline: RouteSnapshot | None = None,
        initial_migration: bool = False,
        prepare_connection: Callable[
            [GatewayModelBinding], tuple[ConnectionRevision, int]
        ] | None = None,
        verify_readback: bool = True,
        changed_aliases: frozenset[str] | None = None,
        registry: Mapping[str, Sequence[dict[str, Any]]] | None = None,
    ) -> dict[str, ConnectionApproval]:
        from ..services.route_publication import (
            _binding_route_contract,
            _binding_route_origin,
            binding_public_count_path,
            route_credential_source,
        )

        runtimes = (registry if registry is not None else self._repository.registry())["runtimes"]
        initial_migration_source: dict[str, Any] | None = None
        if initial_migration:
            raw = publication.resource_manifest.get("fixed_route_migration")
            previous_row = self._repository.get_gateway_publication(publication.base_release_id) \
                if publication.base_release_id is not None else None
            if not isinstance(raw, dict) or previous_row is None or baseline is not None:
                raise PolicyCompilationError("Initial route approval requires a verified migration")
            migration = FixedProgramMigration.model_validate_json(canonical_document(raw))
            migration.require_candidate(
                publication, GatewayPublication.model_validate(previous_row)
            )
            if migration.installation != installation:
                raise PolicyCompilationError("Initial route approval program changed")
            initial_migration_source = migration.model_dump(mode="json")
        approved: dict[str, ConnectionApproval] = {}
        checked: set[str] = set()
        recorded: dict[str, ConnectionApproval] = {}
        checkpoint = publication.resource_manifest.get("route_dependencies")
        if isinstance(checkpoint, dict) and checkpoint.get("version") == 2:
            if (set(checkpoint) != {"version", "scopes"}
                    or not isinstance(checkpoint["scopes"], dict)):
                raise PolicyCompilationError("Route scope dependency checkpoint changed")
            checkpoint = checkpoint["scopes"].get(installation.program.scope.partition)
            if checkpoint is None:
                raise PolicyCompilationError("Route scope dependency checkpoint is unavailable")
        if checkpoint is not None:
            aliases = {binding.model.model_key for binding in publication.desired_spec.bindings}
            if (
                not isinstance(checkpoint, dict)
                or set(checkpoint) != {"version", "context_sha256", "connections"}
                or type(checkpoint["version"]) is not int or checkpoint["version"] != 1
                or checkpoint["context_sha256"]
                != self._dependency_context(publication, installation)
                or not isinstance(checkpoint["connections"], dict)
                or set(checkpoint["connections"]) != aliases
            ):
                raise PolicyCompilationError("Route dependency checkpoint context changed")
            recorded = {
                alias: ConnectionApproval.model_validate_json(canonical_document(value))
                for alias, value in checkpoint["connections"].items()
            }
        issued_at = self._clock()
        pool_config: ApimTargetClientConfig | None = None
        baseline_members = {
            member.alias: member.connection_approval
            for member in (baseline.members if baseline is not None else ())
        }
        for binding in publication.desired_spec.bindings:
            runtime = next((row for row in runtimes if row["id"] == binding.runtime_id), None)
            if (
                runtime is None or runtime.get("enabled") is not True
                or str(runtime.get("gateway_profile_id")) != str(publication.gateway_profile_id)
            ):
                raise PolicyCompilationError("Route requires an independent connection approval")
            _, paths, credential = _binding_route_contract(
                binding, credential_bindings=self._credential_bindings(publication),
            )
            expected_pool = None
            if binding.backend_pool is not None:
                if pool_config is None:
                    pool_config = self._configurations.resolve(
                        publication.gateway_profile_id, include_probe_credentials=False,
                    )
                    if (
                        pool_config.gateway_profile_id != publication.gateway_profile_id
                        or pool_config.settings.apim_api_id != installation.program.scope.api_id
                        or not route_target_identity_matches(
                            pool_config, installation.target_identity_sha256
                        )
                    ):
                        raise PolicyCompilationError("Fixed route Pool target binding changed")
                expected_pool, _ = self._new_pool_connection(
                    binding, installation, pool_config, self._credential_bindings(publication),
                )
            runtime_config = runtime.get("config") or {}
            records = runtime_config.get("storage_route_approvals")
            addition = publication.resource_manifest.get("model_route_addition")
            if addition is not None and not isinstance(addition, dict):
                raise PolicyCompilationError("Model addition approval source is invalid")
            renew = records is None and (
                baseline is not None or initial_migration or addition is not None
            )
            if renew:
                declared_secret = (str(binding.key_vault_secret_id)
                                   if binding.key_vault_secret_id else None)
                materialized_secret = (
                    self._credential_bindings(publication).get(route_credential_source(binding))
                    if binding.auth_strategy in {
                        AuthStrategy.NAMED_VALUE_API_KEY, AuthStrategy.NAMED_VALUE_BEARER,
                    } else None
                )
                if (declared_secret is not None
                    and materialized_secret not in {None, declared_secret}):
                    raise PolicyCompilationError("Fixed route declared credential version changed")
                # Activation records the credential, so a never-activated connection has none yet.
                accepted_secrets = (
                    {declared_secret} if declared_secret is not None
                    else {None, materialized_secret}
                )
                if (
                    runtime_config.get("control_plane_managed") is not True
                    or not self._runtime_destination_matches(publication, binding, runtime_config)
                    or runtime_config.get("auth_strategy") != binding.auth_strategy.value
                    or (runtime_config.get("key_vault_secret_id") not in accepted_secrets
                        and not self._runtime_rotation_matches(
                            publication, binding, runtime_config,
                        ))
                    or binding.api_format is not ApiFormat.MICROSOFT_DECISION
                    and runtime_config.get("managed_identity_resource")
                    != binding.managed_identity_resource
                ):
                    raise PolicyCompilationError(
                        "Route requires an independent connection approval",
                    )
                records = list({
                    member.connection_approval.connection.revision_id:
                    member.connection_approval.model_dump(mode="json")
                    for member in (baseline.members if baseline is not None else ())
                }.values())
                if binding.model.model_key in recorded:
                    recorded_approval = recorded[binding.model.model_key]
                    records = [record for record in records if (
                        record["connection"]["revision_id"]
                        != recorded_approval.connection.revision_id
                    )] + [recorded_approval.model_dump(mode="json")]
            if not isinstance(records, list) or len(records) > 128:
                raise PolicyCompilationError("Route requires an independent connection approval")
            if not records and not (renew and prepare_connection is not None):
                raise PolicyCompilationError("Route requires an independent connection approval")
            matches: list[ConnectionApproval] = []
            for record in records:
                if not isinstance(record, dict):
                    raise PolicyCompilationError("Independent connection approval is invalid")
                approval = ConnectionApproval.model_validate_json(canonical_document(record))
                connection = approval.connection
                if (
                    connection.scope == installation.program.scope
                    and (connection == expected_pool if expected_pool is not None
                         else connection.pool is None)
                    and connection.origin == _binding_route_origin(binding)
                    and connection.path == next(iter(paths.values()))
                    and connection.public_count_path == binding_public_count_path(binding)
                    and connection.credential == credential
                    and all(connection.protocol_paths.get(name, connection.path) == path
                            for name, path in paths.items())
                    and approval.state == "approved"
                        and (renew or approval.issued_epoch <= self._clock())
                ):
                    matches.append(approval)
            prepared_at: int | None = None
            if not matches and renew and prepare_connection is not None:
                prepared, prepared_at = prepare_connection(binding)
                matches = [ConnectionApproval(
                    approval_id=str(uuid5(
                        publication.id, "connection:" + canonical_document(prepared),
                    )),
                    connection=prepared, state="approved",
                    evidence_sha256=document_digest(prepared),
                    issued_epoch=prepared_at, expires_epoch=prepared_at + 86400,
                )]
            if len(matches) != 1:
                raise PolicyCompilationError(
                    "Route requires one current independent connection approval",
                )
            selected = matches[0]
            key = canonical_document(selected.connection)
            baseline_approval = baseline_members.get(binding.model.model_key)
            now_epoch = self._clock()
            unchanged_baseline = (
                changed_aliases is not None
                and binding.model.model_key not in changed_aliases
                and baseline_approval is not None
                and baseline_approval.connection == selected.connection
                and baseline_approval.state == "approved"
                and baseline_approval.issued_epoch <= now_epoch < baseline_approval.expires_epoch
            )
            if verify_readback and not unchanged_baseline and key not in checked:
                if management.verify_route_connection(selected.connection) != (
                    selected.connection.dependency_sha256
                ):
                    raise PolicyCompilationError("Approved route dependency readback changed")
                checked.add(key)
            if renew:
                approval_source = (baseline if baseline is not None else initial_migration_source
                                   if initial_migration else addition)
                if approval_source is None:
                    raise PolicyCompilationError("Route renewal approval source is unavailable")
                baseline_sha256 = document_digest(approval_source)
                saved = recorded.get(binding.model.model_key)
                instant = (saved.issued_epoch if saved is not None else prepared_at
                           if prepared_at is not None else issued_at)
                selected = ConnectionApproval(
                    approval_id=str(uuid5(publication.id, "connection:" + key)),
                    connection=selected.connection, state="approved",
                    evidence_sha256=document_digest({
                        "baseline": baseline_sha256,
                        "runtime_id": str(runtime["id"]),
                        "runtime_config": runtime_config,
                        "connection": selected.connection.model_dump(mode="json"),
                        "observed_dependency": selected.connection.dependency_sha256,
                    }),
                    issued_epoch=instant, expires_epoch=instant + 86400,
                )
            selected.require_current(selected.connection, self._clock())
            if recorded and recorded[binding.model.model_key] != selected:
                raise PolicyCompilationError("Route dependency approval changed after checkpoint")
            approved[binding.model.model_key] = selected
        return approved


class FixedProgramMigration(RouteModel):
    version: Literal[1] = 1
    request_id: UUID
    previous_publication_id: UUID
    previous_revision: Identifier
    previous_spec_sha256: Digest
    previous_policy_sha256: Digest
    installation: FixedProgramInstallation
    known_unavailable_bindings: dict[str, Digest] = Field(
        default_factory=dict, max_length=10000, exclude_if=lambda value: not value,
    )

    def require_previous(self, previous: GatewayPublication) -> None:
        bindings = {binding.model.model_key: document_digest(binding)
                    for binding in previous.desired_spec.bindings}
        if any(bindings.get(alias) != digest
               for alias, digest in self.known_unavailable_bindings.items()):
            raise PolicyCompilationError("Known unavailable upstream binding changed")
        if (
            previous.id != self.previous_publication_id
            or previous.apim_revision != self.previous_revision
            or previous.desired_spec_sha256 != self.previous_spec_sha256
            or previous.policy_sha256 != self.previous_policy_sha256
            or previous.status.value != "active"
            or str(previous.gateway_profile_id) != self.installation.program.scope.target_id
            or self.previous_revision == self.installation.program.api_revision
            or self.installation.program.scope.gateway_id != "managed"
            or not previous.desired_spec.bindings
            or any(name in previous.resource_manifest for name in (
                "fixed_route_program", "route_configuration", "route_activation",
            ))
            or len(previous.desired_spec.gateway_deployments) != 1
            or previous.desired_spec.gateway_deployments[0].native_gateway_id != "managed"
        ):
            raise PolicyCompilationError("Fixed route migration previous publication changed")

    def require_candidate(
        self, candidate: GatewayPublication, previous: GatewayPublication,
    ) -> None:
        self.require_previous(previous)
        if (
            candidate.id != self.request_id
            or candidate.gateway_profile_id != previous.gateway_profile_id
            or candidate.base_release_id != previous.id
            or candidate.publication_kind is not PublicationKind.ROUTE_RECONCILE
            or not program_migration_spec_matches(previous.desired_spec, candidate.desired_spec)
            or candidate.resource_manifest.get("fixed_route_migration")
            != self.model_dump(mode="json")
        ):
            raise PolicyCompilationError("Fixed route migration candidate changes existing routes")


def promote_program_once(
    management: Any, previous_revision: str, revision: str, release_name: str,
    context_sha256: str, recorded: Mapping[str, Any] | None,
    persist: Callable[[Mapping[str, Any] | None, Mapping[str, Any]], None],
    verify: Callable[[str], None],
) -> None:
    expected = {
        "version": 1, "previous_revision": previous_revision, "revision": revision,
        "release_name": release_name, "context_sha256": context_sha256,
    }
    current = management.current_revision()
    if recorded is None:
        if current != previous_revision:
            raise PolicyCompilationError("Fixed program promotion base changed before intent")
        verify(previous_revision)
        recorded = {**expected, "outcome": "started"}
        persist(None, recorded)
        management.promote_fixed_revision(revision, release_name, previous_revision)
        current = management.current_revision()
    elif (
        dict(recorded) not in ({**expected, "outcome": "started"},
                              {**expected, "outcome": "confirmed"})
    ):
        raise PolicyCompilationError("Fixed program promotion intent changed")
    if current != revision:
        raise RetryablePublicationError("Fixed program promotion outcome unknown; read back only")
    verify(revision)
    if recorded["outcome"] != "confirmed":
        persist(recorded, {**expected, "outcome": "confirmed"})


class FixedProgramManagement(Protocol):
    def verify_observer_permissions(self, named_value_id: str) -> None: ...

    def current_revision(self) -> str | None: ...

    def revision_api_policy(self, revision: str) -> str: ...

    def route_program_operations(self, revision: str) -> Mapping[str, tuple[str, str, str]]: ...

    def verify_route_connection(self, connection: ConnectionRevision) -> str: ...

    def verify_route_pool_slice(
        self, connection: ConnectionRevision, cursor: int, *, maximum: int = 4,
    ) -> int: ...

    def route_credential_value(self, named_value_id: str) -> SecretStr: ...

    def prepare_route_oauth(
        self, credential: OAuthCredentialResource, checkpoint: Callable[[dict[str, Any]], None],
    ) -> None: ...

    def prepare_route_pool(
        self, connection: ConnectionRevision, backends: tuple[BackendResource, ...],
        checkpoint: Callable[[dict[str, Any]], None],
    ) -> None: ...

    def prepare_route_connection(
        self, connection: ConnectionRevision, checkpoint: Callable[[dict[str, Any]], None],
        *, recovery_only: bool = False,
    ) -> str: ...


class FixedRoutePublisherPort:
    def __init__(
        self, projection: TableRouteProjection, verifier: FixedProgramVerifier,
        subscription_key: SecretStr, client: httpx.Client,
        *, active_fence: Callable[[], Mapping[str, Any]],
        verify_connections: Callable[[RoutePublicationTask, Mapping[str, Any]], None],
        prepare_availability: Callable[[RoutePublicationTask, RouteContent], bool],
        clock: Callable[[], int] = lambda: int(time()),
        serving_verifiers: tuple[FixedProgramVerifier, ...] = (),
        absorbed_model_aliases: Callable[[], tuple[str, ...]] = lambda: (),
    ) -> None:
        self.projection = projection
        self._verifier = verifier
        self._subscription_key = subscription_key
        self._client = client
        self._active_fence = active_fence
        self._verify_connections = verify_connections
        self._prepare_availability = prepare_availability
        self._clock = clock
        self._serving_verifiers = serving_verifiers
        self._absorbed_model_aliases = absorbed_model_aliases

    @property
    def program_record(self) -> dict[str, Any]:
        return self._verifier.installation.model_dump(mode="json")

    def check(self, task: RoutePublicationTask) -> ProgramCapabilityProof:
        installed = self._verifier.installation
        if (
            task.plan.program != installed.program
            or task.target_identity_sha256 != installed.target_identity_sha256
            or task.storage_endpoint != installed.storage_endpoint
            or task.storage_table != installed.storage_table
        ):
            raise PolicyCompilationError("Route publication target or installed program changed")
        if task.serving_installation_sha256 and task.serving_installation_sha256 != {
            verifier.installation.program.instance_id: document_digest(verifier.installation)
            for verifier in self._serving_verifiers or (self._verifier,)
        }:
            raise PolicyCompilationError("Route publication serving installations changed")
        if any(route.connection.pool is not None for route in task.plan.snapshot.routes) and (
            installed.pool_enabled is not True
        ):
            raise PolicyCompilationError("Route publication Pool capability is unavailable")
        fence = self._active_fence()
        if (
            type(fence.get("protocol_version")) is not int or fence["protocol_version"] != 2
            or fence.get("mode") != "active"
            or fence.get("gateway_profile_id") != task.plan.program.scope.target_id
            or fence.get("epoch") != task.plan.snapshot.approval.budget_epoch
        ):
            raise PolicyCompilationError("Route publication budget fence changed")
        task.plan.snapshot.require_valid(self._clock())
        self._verify_connections(task, fence)
        observed = self.projection.read_program_proof(installed.program)
        if task.model_addition and (
            observed is None or not observed[0].issued_epoch <= self._clock()
            < observed[0].expires_epoch or observed[0].identity != installed.program
        ):
            raise RetryablePublicationError(
                "Installed program proof is not current; wait for independent renewal",
            )
        if not task.model_addition and (
            observed is None or observed[0].expires_epoch <= self._clock() + 30
        ):
            proof = self._verifier.verify()
            self.projection.publish_program_proof(
                proof, installed.program, observed[1] if observed is not None else None,
                self._clock(),
            )
        else:
            if observed is None:
                raise PolicyCompilationError("Route program proof is unavailable")
            proof = observed[0]
        if (proof.identity != installed.program
                or not proof.issued_epoch <= self._clock() < proof.expires_epoch):
            raise PolicyCompilationError("Route program proof is unavailable")
        for identity in task.serving_programs:
            observed_node = self.projection.read_program_proof(identity)
            if (
                observed_node is None or observed_node[0].identity != identity
                or not observed_node[0].issued_epoch <= self._clock()
                < observed_node[0].expires_epoch
                or not set(task.plan.snapshot.approval.required_profiles)
                <= set(observed_node[0].supported_profiles)
            ):
                raise RetryablePublicationError(
                    "Every serving node requires a current program proof",
                )
        return proof

    def validate_configuration(self, task: RoutePublicationTask) -> ProgramCapabilityProof:
        if not task.configuration_only:
            raise PolicyCompilationError("Configuration validation requires a model addition")
        proof = self.check(task)
        routes = (
            task.plan.snapshot.routes
            if task.model_addition or not task.plan.changed_aliases
            else tuple(
                route
                for route in task.plan.snapshot.routes
                if route.alias in task.plan.changed_aliases
            )
        )
        for route in routes:
            if type(self._prepare_availability(task, route)) is not bool:
                raise PolicyCompilationError("Route publication availability is unverified")
        self.projection.require_verified_snapshot(task.plan.snapshot, self._clock())
        return proof

    def confirm(self, task: RoutePublicationTask, head: ActiveRouteHead) -> None:
        head.approval.require_same_configuration(task.plan.snapshot.approval)
        proof = self.check(task)
        head.require_ready(task.plan.program, proof, task.plan.snapshot.approval.budget_epoch,
                           self._clock())
        self._verifier.confirm(head)
        if not task.model_addition:
            absorbed = {alias.casefold() for alias in self._absorbed_model_aliases()}
            for route in task.plan.snapshot.routes:
                if route.alias.casefold() in absorbed:
                    current = self.projection.read_model_active(route.alias)
                    if current is not None:
                        self.projection.remove_model_head(current[0], current[1])

    def confirm_model(self, task: RoutePublicationTask, model: ModelRouteHead) -> None:
        if (not task.model_addition or model.publication_id != task.publication_id
                or model.alias != task.plan.snapshot.routes[0].alias):
            raise PolicyCompilationError("Model confirmation task changed")
        model.head.approval.require_same_configuration(task.plan.snapshot.approval)
        proof = self.check(task)
        model.head.require_ready(
            task.plan.program, proof, task.plan.snapshot.approval.budget_epoch, self._clock(),
        )
        route = self.projection.read_model_route(
            model.alias, task.plan.program, proof,
            task.plan.snapshot.approval.budget_epoch, self._clock(),
        )
        if route != task.plan.snapshot.routes[0]:
            raise PolicyCompilationError("Model activation changed before confirmation")
        verifiers = self._serving_verifiers or (self._verifier,)
        if task.serving_programs and tuple(
            verifier.installation.program for verifier in verifiers
        ) != task.serving_programs:
            raise PolicyCompilationError("Model confirmation requires every serving node")
        for verifier in verifiers:
            verifier.confirm_model(model)

    def read_catalog(self) -> list[dict[str, Any]]:
        installed = self._verifier.installation
        contract = installed.operation_contracts.get("models")
        if contract not in {("GET", "/models"), ("GET", "/v1/models")}:
            raise PolicyCompilationError("Fixed route catalog contract is unsupported")
        endpoint = (installed.gateway_endpoint + ";rev="
                    + quote(installed.program.api_revision, safe="") + contract[1])
        with self._client.stream("GET", endpoint, headers={
            "Ocp-Apim-Subscription-Key": self._subscription_key.get_secret_value(),
            "Cache-Control": "no-cache",
        }, timeout=10, follow_redirects=False) as response:
            if response.status_code != 200:
                raise PolicyCompilationError("Fixed route catalog read failed")
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > 4 * 1024 * 1024:
                    raise PolicyCompilationError("Fixed route catalog response exceeds its limit")
                content.extend(chunk)
        value = json.loads(content)
        data = value.get("data") if isinstance(value, dict) else None
        if (not isinstance(data, list) or len(data) > 10000
                or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                       for item in data)):
            raise PolicyCompilationError("Fixed route catalog response is invalid")
        return cast(list[dict[str, Any]], data)

    def require_model_absent(self, alias: str) -> None:
        if self.projection.read_model_active(alias) is not None:
            raise RetryablePublicationError("Model removal head is still active")
        self._verifier.verify()
        installed = self._verifier.installation
        endpoint = installed.gateway_endpoint + ";rev=" + quote(
            installed.program.api_revision, safe="",
        ) + "/responses"
        response = self._client.post(
            endpoint, json={"model": alias, "input": False},
            headers={"Ocp-Apim-Subscription-Key": self._subscription_key.get_secret_value(),
                     "Cache-Control": "no-cache"}, timeout=10, follow_redirects=False,
        )
        value = response.json()
        error = value.get("error") if isinstance(value, dict) else None
        if (response.status_code != 503 or not isinstance(error, dict)
                or error.get("code") != "route_configuration_unavailable"):
            raise PolicyCompilationError("Model removal native rejection is unconfirmed")

    @staticmethod
    def _selected_probe_operations(
        route: RouteContent, member_backend_id: str | None,
        probe_operation: BudgetOperation | None,
    ) -> tuple[BudgetOperation, ...]:
        route.connection.require_probe_member(member_backend_id)
        if probe_operation is not None:
            if probe_operation not in route.operations:
                raise PolicyCompilationError("Route probe operation is not approved")
            return (probe_operation,)
        if route.connection.pool is not None:
            raise PolicyCompilationError("Pool probe requires one approved operation")
        return route.operations

    def probe_changed(
        self, task: RoutePublicationTask, alias: str, journal: PersistentProbeJournal,
        *, member_backend_id: str | None = None, probe_operation: BudgetOperation | None = None,
    ) -> None:
        if alias in task.known_unavailable_bindings:
            raise PolicyCompilationError("Known unavailable upstream cannot be dispatched")
        if alias not in task.plan.changed_aliases:
            raise PolicyCompilationError("Route probe alias was not approved as changed")
        route = next((route for route in task.plan.snapshot.routes if route.alias == alias), None)
        if route is None:
            raise PolicyCompilationError("Route probe alias is not in the candidate snapshot")
        operations = self._selected_probe_operations(route, member_backend_id, probe_operation)
        member_options: dict[str, Any] = (
            {"member_backend_id": member_backend_id} if member_backend_id else {}
        )
        enabled = self._prepare_availability(task, route)
        if type(enabled) is not bool:
            raise PolicyCompilationError("Route publication availability is unverified")
        proof = self.check(task)
        installed = self._verifier.installation
        for operation in operations:
            contract = installed.operation_contracts.get(operation)
            if contract is None or contract[0] != "POST":
                raise PolicyCompilationError("Route probe operation is not installed")
            body = self._probe_body(route, operation)
            endpoint = (installed.gateway_endpoint + ";rev="
                        + quote(installed.program.api_revision, safe="") + contract[1])
            if not enabled:
                self._send_probe(
                    task, route, operation, proof, endpoint, body, journal,
                    str(uuid5(UUID(task.publication_id), "disabled:" + alias + ":" + operation
                              + (":" + member_backend_id if member_backend_id else ""))),
                    allow_dispatch=False, member_backend_id=member_backend_id,
                )
                continue
            journal.run_route(
                route, operation, endpoint, body,
                partial(self._send_probe, task, route, operation, proof, endpoint, body, journal,
                    member_backend_id=member_backend_id),
                **member_options,
            )

    def probe_unavailable(
        self, task: RoutePublicationTask, alias: str, journal: PersistentProbeJournal,
        *, member_backend_id: str | None = None,
        probe_operation: BudgetOperation | None = None,
    ) -> None:
        if (
            alias not in task.known_unavailable_bindings
            or task.program_migration_sha256 is None or alias not in task.plan.changed_aliases
        ):
            raise PolicyCompilationError("Upstream exclusion requires an acknowledged migration")
        route = next(route for route in task.plan.snapshot.routes if route.alias == alias)
        enabled = self._prepare_availability(task, route)
        if type(enabled) is not bool:
            raise PolicyCompilationError("Unavailable route availability is unverified")
        proof = self.check(task)
        installed = self._verifier.installation
        operations = self._selected_probe_operations(route, member_backend_id, probe_operation)
        for operation in operations:
            contract = installed.operation_contracts.get(operation)
            if contract is None or contract[0] != "POST":
                raise PolicyCompilationError("Unavailable route operation is not installed")
            endpoint = (installed.gateway_endpoint + ";rev="
                        + quote(installed.program.api_revision, safe="") + contract[1])
            self._send_probe(
                task, route, operation, proof, endpoint,
                self._probe_body(route, operation), journal,
                str(uuid5(UUID(task.publication_id), "no-dispatch:" + alias + ":" + operation
                          + (":" + member_backend_id if member_backend_id else ""))),
                allow_dispatch=False, member_backend_id=member_backend_id,
                expected_denial=(503, "route_disabled_probe_changed") if enabled
                else (403, "model_disabled"),
            )

    def probe_count(
        self, task: RoutePublicationTask, alias: str, journal: PersistentProbeJournal,
        *, member_backend_id: str | None = None,
    ) -> None:
        if alias in task.known_unavailable_bindings:
            raise PolicyCompilationError("Known unavailable upstream cannot be counted")
        route = next((value for value in task.plan.snapshot.routes if value.alias == alias), None)
        if alias not in task.plan.changed_aliases or route is None or (
            route.profile != "anthropic-messages"
        ):
            raise PolicyCompilationError("Route Count probe is not an approved changed route")
        route.connection.require_probe_member(member_backend_id)
        installed = self._verifier.installation
        contract = installed.operation_contracts.get("anthropic-count-tokens")
        if contract is None or contract[0] != "POST":
            raise PolicyCompilationError("Route Count operation is not installed")
        enabled = self._prepare_availability(task, route)
        if type(enabled) is not bool:
            raise PolicyCompilationError("Route Count availability is unverified")
        proof = self.check(task)
        endpoint = (installed.gateway_endpoint + ";rev="
                    + quote(installed.program.api_revision, safe="") + contract[1])
        self._send_probe(
            task, route, "anthropic-count-tokens", proof, endpoint,
            {"model": route.alias, "messages": [{"role": "user", "content": "Say OK."}]},
            journal, str(uuid5(UUID(task.publication_id), "count:" + alias + task.context_sha256
                              + (":" + member_backend_id if member_backend_id else ""))),
            allow_dispatch=enabled, member_backend_id=member_backend_id,
        )

    def recover_changed(
        self, task: RoutePublicationTask, alias: str, journal: PersistentProbeJournal,
        *, member_backend_id: str | None = None, probe_operation: BudgetOperation | None = None,
    ) -> None:
        if alias not in task.plan.changed_aliases:
            raise PolicyCompilationError("Route recovery alias was not approved as changed")
        route = next((value for value in task.plan.snapshot.routes if value.alias == alias), None)
        if route is None:
            raise PolicyCompilationError("Route recovery alias is not in the candidate snapshot")
        operations = self._selected_probe_operations(route, member_backend_id, probe_operation)
        installed = self._verifier.installation

        def no_dispatch(request_id: str) -> dict[str, Any]:
            raise PolicyCompilationError("Route recovery cannot dispatch a model request")

        for operation in operations:
            contract = installed.operation_contracts.get(operation)
            if contract is None or contract[0] != "POST":
                raise PolicyCompilationError("Route recovery operation is not installed")
            endpoint = (installed.gateway_endpoint + ";rev="
                        + quote(installed.program.api_revision, safe="") + contract[1])
            journal.run_route(
                route, operation, endpoint, self._probe_body(route, operation), no_dispatch,
                recovery_only=True,
                **({"member_backend_id": member_backend_id} if member_backend_id else {}),
            )

    @staticmethod
    def _probe_body(route: RouteContent, operation: BudgetOperation) -> dict[str, Any]:
        contract = route.budget.protocols.get(operation, route.budget)
        body: dict[str, Any] = {"model": route.alias}
        if operation in {"chat-completions", "anthropic-messages"}:
            body.update(messages=[{"role": "user", "content": "Say OK."}], stream=False)
        elif operation == "responses":
            body.update(input="Say OK.", stream=False)
        elif operation == "responses-compact":
            body["input"] = [{"role": "user", "content": "Say OK."}]
        elif operation == "microsoft-decision":
            body.update(state="OK", questions={
                "ok": {"type": "noul", "instructions": "Is the state OK?"},
            })
        elif operation == "images-generations":
            validate_image_profile(route.image_profile)
            body.update(prompt="A small blue square on a white background.", n=1, stream=False)
        else:
            raise PolicyCompilationError("Route probe requires a supported operation")
        if contract.output_field != "none":
            body[contract.output_field] = min(1024, contract.output_ceiling or 1024)
        return body

    def _send_probe(
        self, task: RoutePublicationTask, route: RouteContent,
        operation: BudgetOperation | Literal["anthropic-count-tokens"],
        proof: ProgramCapabilityProof, endpoint: str, body: dict[str, Any],
        journal: PersistentProbeJournal, request_id: str,
        *, allow_dispatch: bool = True, member_backend_id: str | None = None,
        expected_denial: tuple[int, str] = (403, "model_disabled"),
    ) -> dict[str, Any]:
        route.connection.require_probe_member(member_backend_id)
        instant = self._clock()
        payload_bytes = canonical_document(body).encode("utf-8")
        maximum_response_bytes = 1024 * 1024
        timeout_seconds = 120
        if operation == "images-generations":
            profile = validate_image_profile(route.image_profile)
            if len(payload_bytes) > profile.max_request_bytes:
                raise PolicyCompilationError("Route image probe request exceeded its bound")
            maximum_response_bytes = profile.max_response_bytes
            timeout_seconds = profile.timeout_seconds
        approval = ConfigurationApproval.model_validate({
            **task.plan.snapshot.approval.model_dump(mode="python"),
            "approval_id": str(uuid5(
                UUID(request_id), "candidate-approval:" + task.context_sha256,
            )),
            "issued_epoch": instant, "activate_before_epoch": instant + 600,
        })
        approval.require_same_configuration(task.plan.snapshot.approval)
        snapshot = task.plan.snapshot.model_copy(update={"approval": approval})
        candidate = RouteCandidateProbe(
            probe_id=str(uuid5(UUID(request_id), "route-candidate:" + task.context_sha256)),
            publication_id=task.publication_id, request_id=request_id,
            subscription_id=self._verifier.installation.publisher_subscription_id,
            alias=route.alias, operation=operation,
            body_sha256=text_digest(payload_bytes.decode("utf-8")),
            allow_dispatch=allow_dispatch, member_backend_id=member_backend_id,
            head=ActiveRouteHead(approval=approval, activated_epoch=instant),
            issued_epoch=instant, expires_epoch=min(
                instant + 120, proof.expires_epoch, approval.activate_before_epoch,
            ),
        )
        journal.heartbeat()
        self.projection.stage_candidate(
            candidate, snapshot, task.plan.program, proof, instant,
        )
        journal.record_request("POST", endpoint)
        if self._clock() >= candidate.expires_epoch:
            raise PolicyCompilationError("Route candidate authorization expired before dispatch")
        with self._client.stream(
            "POST", endpoint, content=payload_bytes, headers={
                "Ocp-Apim-Subscription-Key": self._subscription_key.get_secret_value(),
                "Content-Type": "application/json", "Accept-Encoding": "identity",
                "x-request-id": request_id, "x-turnstile-route-candidate": candidate.probe_id,
                "x-request-source": "gateway-publication-probe",
            }, timeout=timeout_seconds, follow_redirects=False,
        ) as response:
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > maximum_response_bytes:
                    raise PolicyCompilationError("Route probe response exceeded its bound")
                raw.extend(chunk)
            headers = dict(response.headers)
            status = response.status_code
        if not allow_dispatch:
            payload = json.loads(raw)
            error = payload.get("error") if isinstance(payload, dict) else None
            if (
                status != expected_denial[0] or not isinstance(error, dict)
                or error.get("code") != expected_denial[1]
            ):
                raise PolicyCompilationError("Disabled route was not rejected before dispatch")
            return {"route_disabled": True}
        if operation == "anthropic-count-tokens":
            expected_count_headers = {
                "x-turnstile-route-version": candidate.head.approval.route_version,
                "x-turnstile-route-candidate": candidate.probe_id,
                "x-turnstile-route-request": request_id,
                **({"x-turnstile-route-member": member_backend_id} if member_backend_id else {}),
            }
            if any(headers.get(name) != value for name, value in expected_count_headers.items()):
                raise PolicyCompilationError("Route Count response identity changed")
            payload = json.loads(raw)
            if route.connection.public_count_path is None:
                if not COUNT_TOKENS_REJECTIONS["unavailable"].matches(status, payload):
                    raise PolicyCompilationError("Route Count unavailable response changed")
            elif (
                status != 200 or not isinstance(payload, dict)
                or type(payload.get("input_tokens")) is not int
                or not 0 <= payload["input_tokens"] <= 2**53 - 1
            ):
                raise PolicyCompilationError("Route Count response is invalid")
            return {"route_count_validated": True}
        if status != 200:
            raise PolicyCompilationError(f"Route probe returned HTTP {status}; no request replayed")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise PolicyCompilationError("Route probe returned an invalid response")
        usage = self._usage(payload, operation)
        evidence: dict[str, Any] = {
            "route_validated": False, "total_tokens": (
                usage.input_tokens + usage.cached_tokens + usage.output_tokens
            ), "usage": usage.model_dump(mode="json"),
            "correlation_id": headers.get("x-correlation-id"),
            "route_version": task.plan.snapshot.approval.route_version,
            "route_content_sha256": route.sha256, "operation": operation,
            **({"member_backend_id": member_backend_id} if member_backend_id else {}),
        }
        expected = {
            "x-turnstile-route-version": candidate.head.approval.route_version,
            "x-turnstile-route-candidate": candidate.probe_id,
            "x-turnstile-route-request": request_id,
            **({"x-turnstile-route-member": member_backend_id} if member_backend_id else {}),
        }
        try:
            correlation = UUID(str(evidence["correlation_id"]))
            valid_correlation = str(correlation) == evidence["correlation_id"]
        except ValueError:
            valid_correlation = False
        if not valid_correlation or any(
            headers.get(name) != value for name, value in expected.items()
        ):
            raise PolicyCompilationError(
                "Route probe response identity differs from the candidate request",
                billable_outcome=BillableRequestOutcome(
                    actual_tokens=evidence["total_tokens"],
                    correlation_id=evidence["correlation_id"], evidence=evidence,
                ),
            )
        if operation == "microsoft-decision":
            answers = payload.get("answers")
            if not isinstance(answers, dict) or set(answers) != set(body["questions"]):
                raise PolicyCompilationError(
                    "Route Decision probe returned invalid answers",
                    billable_outcome=BillableRequestOutcome(
                        actual_tokens=evidence["total_tokens"],
                        correlation_id=evidence["correlation_id"], evidence=evidence,
                    ),
                )
            evidence["decision_validated"] = True
        if operation == "images-generations":
            try:
                generated_image(payload)
            except ValueError:
                raise PolicyCompilationError(
                    "Route image probe returned an invalid image",
                    billable_outcome=BillableRequestOutcome(
                        actual_tokens=evidence["total_tokens"],
                        correlation_id=evidence["correlation_id"], evidence=evidence,
                    ),
                ) from None
            evidence["image_validated"] = True
        if operation == "responses" and payload.get("status") != "completed":
            raise PolicyCompilationError(
                "Route Responses probe did not complete; no request replayed",
                billable_outcome=BillableRequestOutcome(
                    actual_tokens=evidence["total_tokens"],
                    correlation_id=evidence["correlation_id"], evidence=evidence,
                ),
            )
        evidence["route_validated"] = True
        return evidence

    @staticmethod
    def _usage(payload: Mapping[str, Any], operation: BudgetOperation) -> InvocationUsage:
        if operation == "microsoft-decision":
            usage = _decision_usage(payload.get("usage"))
            if usage is None:
                raise PolicyCompilationError("Route Decision probe omitted exact measured usage")
            return usage
        if operation == "images-generations":
            usage = image_usage(payload)
            if usage is None:
                raise PolicyCompilationError("Route image probe omitted exact modality usage")
            return usage
        raw = payload.get("usage")
        if not isinstance(raw, dict):
            raise PolicyCompilationError("Route probe response omitted measured usage")
        if operation == "chat-completions":
            fields: tuple[str, ...] = ("prompt_tokens", "completion_tokens", "total_tokens")
            if any(type(raw.get(name)) is not int or raw[name] < 0 for name in fields):
                raise PolicyCompilationError("Route probe usage counters are invalid")
            choices = payload.get("choices")
            if (not isinstance(choices, list) or not choices
                    or raw["total_tokens"] != raw["prompt_tokens"] + raw["completion_tokens"]):
                raise PolicyCompilationError("Route probe response or usage is incomplete")
            return _openai_usage(raw)
        fields = ("input_tokens", "output_tokens")
        if any(type(raw.get(name)) is not int or raw[name] < 0 for name in fields):
            raise PolicyCompilationError("Route probe usage counters are invalid")
        if operation == "anthropic-messages":
            cached = raw.get("cache_read_input_tokens", 0)
            written = raw.get("cache_creation_input_tokens", 0)
            if (type(cached) is not int or type(written) is not int or min(cached, written) < 0
                    or not isinstance(payload.get("content"), list)
                    or not payload.get("stop_reason")):
                raise PolicyCompilationError("Route probe response or usage is incomplete")
            return InvocationUsage(
                input_tokens=raw["input_tokens"], cached_tokens=cached + written,
                cache_write_tokens=written, output_tokens=raw["output_tokens"], estimated=False,
            )
        details = raw.get("input_tokens_details", {})
        if not isinstance(details, dict):
            raise PolicyCompilationError("Route probe usage details are invalid")
        cached = details.get("cached_tokens", 0)
        if (type(cached) is not int or not 0 <= cached <= raw["input_tokens"]
                or type(raw.get("total_tokens")) is not int
                or raw["total_tokens"] != raw["input_tokens"] + raw["output_tokens"]
                or not isinstance(payload.get("output"), list)):
            raise PolicyCompilationError("Route probe response or usage is incomplete")
        return InvocationUsage(
            input_tokens=raw["input_tokens"] - cached, cached_tokens=cached,
            output_tokens=raw["output_tokens"], estimated=False,
        )