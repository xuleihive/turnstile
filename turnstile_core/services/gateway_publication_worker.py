from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from traceback import extract_tb
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import httpx
import psycopg
from pydantic import ValidationError

from ..domain.budget_counting import BudgetPublicationContracts
from ..domain.control_plane import (
    GatewayPublication,
    ImageProbeAuthorization,
    PublicationKind,
    PublicationStatus,
    publication_credential_evidence,
    publication_credential_states,
    publication_model_id,
)
from ..domain.gateway_targets import CapabilitySupport
from ..domain.model_availability import ModelAvailabilityPublication, ModelAvailabilityState
from ..integrations.apim_control_plane import (
    ApimPolicyCompiler,
    ApimPublisherClient,
    AuthorizationRequiredError,
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.apim_control_plane_contract import (
    ApimPublisherClientResolver,
    CredentialSourceMissingError,
    OperationResource,
    ProbeRequestLimitExceeded,
    ProbeVerificationDeferred,
    PublisherPermissionError,
    ResolvedApimPublisherClient,
    coerce_apim_publisher_client_resolver,
)
from ..integrations.apim_image_policy import IMAGE_OPERATION_ID
from ..integrations.apim_model_availability import compose_availability_parent
from ..integrations.apim_policy_components import (
    component_digest,
    compose_image_parent_policy,
    parse_policy,
    policy_document_digest,
    validate_parent_policy,
    validate_parent_readback,
)
from ..persistence.repository import QueryRepository
from ..security import CredentialCipher
from .control_plane import GatewayControlPlaneService
from .model_availability import ModelAvailabilityRepository
from .probe_journal import PersistentProbeJournal, PublicationProbeProgress
from .route_publication import RouteActivationNotApplied
from .worker_lease import WorkerLease

logger = logging.getLogger(__name__)


class GatewayPublicationWorker:
    _NEXT_STATUS = {
        "queued": "validating",
        "validating": "provisioning",
        "provisioning": "building_revision",
        "building_revision": "verifying",
        "verifying": "promoting",
    }

    def __init__(
        self,
        repository: QueryRepository,
        client: ApimPublisherClient | ApimPublisherClientResolver,
        parent_policy: str,
        compiler: ApimPolicyCompiler | None = None,
        cipher: CredentialCipher | None = None,
        chat_completions_operation_id: str = "chat-completions",
        responses_operation_id: str = "responses",
        responses_compact_operation_id: str = "responses-compact",
        messages_operation_id: str = "anthropic-messages",
        count_tokens_operation_id: str = "anthropic-count-tokens",
        models_operation_id: str = "anthropic-models",
        parent_policy_overrides: Mapping[UUID, str] | None = None,
        usage_observer_target_id: UUID | None = None,
        budget_configuration: Callable[[UUID], Mapping[str, object]] | None = None,
        probe_request_limits: Mapping[UUID, int] | None = None,
        availability_projector: Callable[[UUID], ModelAvailabilityState] | None = None,
        route_configuration_executor: Callable[
            [GatewayPublication, str, Callable[[], None]], GatewayPublication
        ] | None = None,
    ) -> None:
        self._repository = repository
        self._client_resolver = coerce_apim_publisher_client_resolver(client)
        self._parent_policy = parent_policy
        self._parent_policy_overrides = dict(parent_policy_overrides or {})
        self._compiler = compiler or ApimPolicyCompiler()
        self._usage_observer_target_id = usage_observer_target_id
        self._budget_configuration = budget_configuration
        self._probe_request_limits = dict(probe_request_limits or {})
        self._availability_projector = availability_projector
        self._route_configuration_executor = route_configuration_executor
        self._cipher = cipher
        self._chat_completions_operation_id = chat_completions_operation_id
        self._responses_operation_id = responses_operation_id
        self._responses_compact_operation_id = responses_compact_operation_id
        self._messages_operation_id = messages_operation_id
        self._count_tokens_operation_id = count_tokens_operation_id
        self._models_operation_id = models_operation_id

    def _record_probe_request(
        self, publication: GatewayPublication, worker_id: str, method: str, endpoint: str,
    ) -> None:
        request_limit = self._probe_request_limits.get(publication.gateway_profile_id)
        if request_limit is None:
            return
        if not self._repository.reserve_gateway_publication_probe(
            publication.id, worker_id, request_limit, method,
            hashlib.sha256(endpoint.encode()).hexdigest(),
        ):
            raise ProbeRequestLimitExceeded(
                f"Publication probe allowance of {request_limit} requests is exhausted "
                "or its worker lease is no longer valid; no request was sent"
            )

    def run_once(
        self, worker_id: str, lease_seconds: int = 180, max_attempts: int = 30
    ) -> GatewayPublication | None:
        verification_deadline = monotonic() + 270
        worker_id = f"{worker_id}:{uuid4().hex}"
        claimed = self._repository.claim_gateway_publication(worker_id, lease_seconds)
        if claimed is None:
            return None
        try:
            publication = GatewayPublication.model_validate(claimed)
        except ValidationError:
            publication_id = UUID(str(claimed["id"]))
            failed = self._repository.transition_gateway_publication(
                publication_id,
                str(claimed["status"]),
                "failed",
                {
                    "error_code": "invalid_publication",
                    "error_message": (
                        "Stored publication failed validation; original snapshot retained"
                    ),
                },
                worker_id,
            )
            if failed is not None:
                logger.error("Gateway publication %s rejected: invalid snapshot", publication_id)
            return None
        client: ApimPublisherClient | None = None
        observed_credentials: dict[str, Literal["present", "missing", "unverified"]] = {}
        verification_started = False
        configuration_publication = False

        def before_verification() -> None:
            nonlocal verification_started
            verification_started = True

        def failure_updates(code: str, message: str) -> dict[str, Any]:
            updates: dict[str, Any] = {"error_code": code, "error_message": message}
            if observed_credentials:
                states = {**(publication_credential_states(publication) or {}),
                          **observed_credentials}
                updates["resource_manifest"] = {
                    **publication.resource_manifest,
                    "credential_evidence": publication_credential_evidence(publication, states),
                }
            if configuration_publication and publication.status is PublicationStatus.QUEUED:
                latest_row = self._repository.get_gateway_publication(publication.id)
                if latest_row is not None:
                    latest = GatewayPublication.model_validate(latest_row)
                    if (
                        latest.id == publication.id and latest.status == publication.status
                        and latest.attempt_count == publication.attempt_count
                        and latest.gateway_profile_id == publication.gateway_profile_id
                        and latest.base_release_id == publication.base_release_id
                        and latest.desired_spec == publication.desired_spec
                        and latest.desired_spec_sha256 == publication.desired_spec_sha256
                        and any(latest.resource_manifest.get(field)
                                != publication.resource_manifest.get(field) for field in (
                                    "route_preparation", "route_credentials", "route_dependencies",
                                ))
                    ):
                        updates["resource_manifest"] = {
                            **latest.resource_manifest,
                            "route_last_progress_attempt": publication.attempt_count,
                        }
            return updates

        try:
            with WorkerLease(lambda: self._repository.renew_gateway_publication_lease(
                publication.id, worker_id, lease_seconds
            ), lease_seconds) as lease:
                configuration_publication = self._uses_route_configuration(publication)
                attempts = publication.attempt_count
                if configuration_publication:
                    last_progress = publication.resource_manifest.get(
                        "route_last_progress_attempt", 0,
                    )
                    if (type(last_progress) is not int
                            or not 0 <= last_progress < publication.attempt_count):
                        raise PolicyCompilationError(
                            "Route publication progress attempt is invalid",
                        )
                    attempts -= last_progress
                if attempts > max_attempts and not (
                    publication.status is PublicationStatus.PROMOTING
                    and publication.apim_revision is not None
                ):
                    return self._fail_attempt_limit(publication, worker_id)
                if configuration_publication:
                    if self._route_configuration_executor is None:
                        raise PolicyCompilationError(
                            "Fixed route configuration executor is unavailable; "
                            "configuration tasks cannot publish APIM policies"
                        )
                    return self._route_configuration_executor(
                        publication, worker_id, lease.heartbeat,
                    )
                resolved = self._client_resolver.resolve(
                    publication.gateway_profile_id,
                    include_probe_credentials=publication.status is not PublicationStatus.PROMOTING,
                )
                self._validate_target_identity(publication, resolved)
                client = resolved.client
                return self._advance(
                    publication, worker_id, resolved, lease.heartbeat, max_attempts=max_attempts,
                    observed_credentials=observed_credentials,
                    before_verification=before_verification,
                    verification_deadline=verification_deadline,
                )
        except AuthorizationRequiredError:
            waiting = self._repository.transition_gateway_publication(
                publication.id,
                publication.status.value,
                "awaiting_authorization",
                {
                    "error_code": "provider_authorization_required",
                    "error_message": "Provider authorization is required",
                },
                worker_id,
            )
            return GatewayPublication.model_validate(waiting) if waiting else publication
        except RouteActivationNotApplied as error:
            failed = self._repository.transition_gateway_publication(
                publication.id, publication.status.value, "failed",
                {"error_code": type(error).__name__, "error_message": str(error)}, worker_id,
            )
            return GatewayPublication.model_validate(failed) if failed else publication
        except (RetryablePublicationError, httpx.TransportError) as error:
            code = type(error).__name__
            message = str(error).replace("\n", " ")[:1000]
            logger.info(
                "Gateway publication %s requeued at %s: error_code=%s error_message=%r",
                publication.id, publication.status.value, code, message,
            )
            queued = self._repository.transition_gateway_publication(
                publication.id,
                publication.status.value,
                publication.status.value,
                failure_updates(code, message),
                worker_id,
            )
            return GatewayPublication.model_validate(queued) if queued else publication
        except Exception as error:
            if (
                publication.status is PublicationStatus.PROMOTING
                and self._uses_route_configuration(publication)
            ):
                latest = self._repository.get_gateway_publication(publication.id)
                if latest is not None and latest.get("resource_manifest", {}).get(
                    "route_activation",
                ) is not None:
                    queued = self._repository.transition_gateway_publication(
                        publication.id, "promoting", "promoting",
                        {"error_code": type(error).__name__, "error_message":
                         "Configuration activation confirmation is incomplete; "
                         "retain the original intent and recover without replay"},
                        worker_id,
                    )
                    return GatewayPublication.model_validate(queued or latest)
            database_message: str | None = None
            if isinstance(error, psycopg.Error):
                retryable = isinstance(error, psycopg.OperationalError) and (
                    error.sqlstate is None or error.sqlstate.startswith("08")
                    or error.sqlstate in {"57P01", "57P02", "57P03"}
                ) and not verification_started
                locations = ",".join(
                    f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                    for frame in extract_tb(error.__traceback__)[-4:]
                )
                logger.warning(
                    "Gateway publication database error publication=%s target=%s phase=%s "
                    "attempt=%s error=%s sqlstate=%s retryable=%s "
                    "verification_started=%s locations=%s",
                    publication.id, publication.gateway_profile_id, publication.status.value,
                    publication.attempt_count, type(error).__name__, error.sqlstate,
                    retryable, verification_started, locations,
                )
                if retryable:
                    queued = self._repository.transition_gateway_publication(
                        publication.id, publication.status.value, publication.status.value,
                        failure_updates(
                            type(error).__name__,
                            "Database connection interrupted; resume from the persisted "
                            "publication checkpoint after lease and target revalidation",
                        ),
                        worker_id,
                    )
                    return GatewayPublication.model_validate(queued) if queued else publication
                database_message = (
                    "Database connection interrupted after verification began; "
                    "verification outcome is unconfirmed and requires explicit recovery"
                    if verification_started else
                    "Database operation failed; inspect the publication's error type and SQLSTATE"
                )
            code = type(error).__name__
            message = database_message or str(error).replace("\n", " ")[
                :8192 if isinstance(error, PublisherPermissionError) else 1000
            ]
            if publication.status.value == "promoting" and publication.apim_revision is not None:
                try:
                    current_revision = client.current_revision() if client is not None else None
                except Exception:
                    current_revision = None
                if current_revision is None or current_revision == publication.apim_revision:
                    queued = self._repository.transition_gateway_publication(
                        publication.id,
                        publication.status.value,
                        publication.status.value,
                        failure_updates(code, message),
                        worker_id,
                    )
                    return GatewayPublication.model_validate(queued) if queued else publication
            failed = self._repository.transition_gateway_publication(
                publication.id,
                publication.status.value,
                "failed",
                failure_updates(code, message),
                worker_id,
            )
            return GatewayPublication.model_validate(failed) if failed else publication

    def _uses_route_configuration(self, publication: GatewayPublication) -> bool:
        if any(field in publication.resource_manifest for field in (
            "route_configuration", "route_rollback", "route_preparation", "route_dependencies",
            "route_credentials", "fixed_route_program", "fixed_route_migration",
            "model_route_addition", "model_route_removal",
        )):
            return True
        base = (
            self._repository.get_gateway_publication(publication.base_release_id)
            if publication.base_release_id is not None else None
        )
        effective = self._repository.effective_gateway_publication(publication.gateway_profile_id)
        for row in (base, effective):
            if row is None:
                continue
            if str(row["gateway_profile_id"]) != str(publication.gateway_profile_id):
                raise PolicyCompilationError("Publication base belongs to another APIM target")
            if any(
                name in row.get("resource_manifest", {})
                for name in ("route_configuration", "fixed_route_program")
            ):
                return True
        return False

    def _fail_attempt_limit(
        self, publication: GatewayPublication, worker_id: str,
    ) -> GatewayPublication:
        failed = self._repository.transition_gateway_publication(
            publication.id, publication.status.value, "failed",
            {
                "error_code": "max_attempts_exceeded",
                "error_message": "Gateway publication exceeded its retry limit",
            },
            worker_id,
        )
        return GatewayPublication.model_validate(failed) if failed else publication

    @staticmethod
    def _validate_target_identity(
        publication: GatewayPublication,
        resolved: ResolvedApimPublisherClient,
    ) -> None:
        if resolved.gateway_profile_id != publication.gateway_profile_id:
            raise PolicyCompilationError("APIM client resolved another publication target")
        snapshot = publication.resource_manifest.get(
            "apim_target_identity_sha256"
        )
        if snapshot is not None and snapshot != resolved.target_identity_sha256:
            raise PolicyCompilationError(
                "APIM target identity changed after publication was queued"
            )
        if (
            snapshot is None
            and resolved.enforce_identity_snapshot
            and publication.status.value != "queued"
        ):
            raise PolicyCompilationError(
                "APIM target identity snapshot is missing from an in-flight publication"
            )

    def _advance(
        self,
        publication: GatewayPublication,
        worker_id: str,
        resolved: ResolvedApimPublisherClient,
        heartbeat: Callable[[], None] | None = None,
        *,
        max_attempts: int = 30,
        observed_credentials: dict[str, Literal["present", "missing", "unverified"]] | None = None,
        before_verification: Callable[[], None] | None = None,
        verification_deadline: float | None = None,
    ) -> GatewayPublication:
        if observed_credentials is None:
            observed_credentials = {}
        client = resolved.client
        status = publication.status.value
        if (status in {"validating", "provisioning", "building_revision"}
                and "native_stream_contract_version" not in publication.resource_manifest):
            raise PolicyCompilationError(
                "Legacy in-flight publication requires a reviewed stream-contract handoff"
            )
        if (status == "queued" and publication.policy_sha256 is None
                and "native_stream_contract_version" not in publication.resource_manifest):
            publication = publication.model_copy(update={"resource_manifest": {
                **publication.resource_manifest, "native_stream_contract_version": 1,
            }})
        already_promoted = False
        if status == "promoting" and publication.apim_revision is not None:
            current_revision = client.current_revision()
            if current_revision is None:
                raise RetryablePublicationError("APIM current revision could not be verified")
            already_promoted = current_revision == publication.apim_revision
        if publication.attempt_count > max_attempts and not already_promoted:
            return self._fail_attempt_limit(publication, worker_id)
        canonical_parent = self._parent_policy_overrides.get(
            publication.gateway_profile_id, self._parent_policy,
        )
        registry = self._repository.registry()
        routed_publication = publication.model_copy(
            update={
                "desired_spec": GatewayControlPlaneService._reconciled_release_spec(
                    publication.desired_spec,
                    registry,
                )
            }
        )
        observed_current: tuple[str, str] | None = None
        availability_snapshot = publication.resource_manifest.get("model_availability")
        if self._repository.model_availability_supported() and availability_snapshot is None:
            if status != "queued":
                raise PolicyCompilationError("In-flight publication requires availability identity")
            observed_current = client.current_api_policy()
            if parse_policy(observed_current[1]).find(
                "inbound/set-variable[@name='availabilityProtocolVersion']",
            ) is None:
                raise PolicyCompilationError("Publication requires installed model availability")
            target = self._repository.get_apim_target(publication.gateway_profile_id)
            if target is None:
                raise PolicyCompilationError("Availability requires a registered APIM identity")
            availability_snapshot = ModelAvailabilityPublication.capture(
                routed_publication, registry, str(target["api_id"]),
            ).model_dump(mode="json")
        if availability_snapshot is not None:
            snapshot_availability = ModelAvailabilityPublication.model_validate(
                availability_snapshot,
            )
            snapshot_availability.validate_publication(routed_publication)
            manifest = {**publication.resource_manifest,
                        "model_availability": snapshot_availability.model_dump(mode="json")}
            publication = publication.model_copy(update={"resource_manifest": manifest})
            routed_publication = routed_publication.model_copy(
                update={"resource_manifest": manifest},
            )
            canonical_parent = compose_availability_parent(canonical_parent)
            if self._availability_projector is None:
                raise PolicyCompilationError("Availability publication projection is unavailable")
            if status in {"verifying", "promoting"}:
                availability_repository = cast(ModelAvailabilityRepository, self._repository)
                probe_states = {}
                probe_revisions = {}
                for alias, model_id in snapshot_availability.models.items():
                    availability_repository.stage_model_availability(model_id, publication.id)
                    projected = self._availability_projector(model_id)
                    if projected.model_id != model_id or projected.lifecycle == "deleted":
                        raise PolicyCompilationError(
                            "Publication availability identity is not current"
                        )
                    probe_states[alias] = projected.enabled
                    probe_revisions[alias] = projected.revision
                routed_publication = routed_publication.model_copy(update={"resource_manifest": {
                    **routed_publication.resource_manifest,
                    "availability_probe_states": probe_states,
                    "availability_probe_revisions": probe_revisions,
                }})
        execution_enabled = parse_policy(canonical_parent).find(
            "inbound/set-variable[@name='budgetExecutionVersion']",
        ) is not None
        if execution_enabled:
            if self._budget_configuration is None:
                raise PolicyCompilationError("Budget publication configuration is unavailable")
            fence = self._budget_configuration(publication.gateway_profile_id)
            if (
                fence.get("protocol_version") != 2 or fence.get("mode") != "active"
                or fence.get("gateway_profile_id") != str(publication.gateway_profile_id)
            ):
                raise PolicyCompilationError("Budget publication requires its active Gateway fence")
            raw_snapshot = publication.resource_manifest.get("budget_contracts")
            if raw_snapshot is None:
                if status != "queued":
                    raise PolicyCompilationError(
                        "In-flight publication has no budget contract snapshot"
                    )
                previous_row = (
                    self._repository.get_gateway_publication(publication.base_release_id)
                    if publication.base_release_id is not None else None
                )
                snapshot = BudgetPublicationContracts.capture(
                    routed_publication, registry, fence,
                    GatewayPublication.model_validate(previous_row) if previous_row else None,
                    new_model_key=(publication.desired_spec.bindings[-1].model.model_key
                                   if publication.desired_spec.bindings else None),
                )
            else:
                snapshot = BudgetPublicationContracts.model_validate(raw_snapshot)
            snapshot.validate_publication(routed_publication)
            if str(snapshot.epoch) != fence.get("epoch"):
                raise PolicyCompilationError("Budget publication epoch changed after queueing")
            manifest = {
                **publication.resource_manifest,
                "budget_contracts": snapshot.model_dump(mode="json"),
            }
            publication = publication.model_copy(update={"resource_manifest": manifest})
            routed_publication = routed_publication.model_copy(
                update={"resource_manifest": {
                    **routed_publication.resource_manifest,
                    "budget_contracts": snapshot.model_dump(mode="json"),
                }}
            )
        elif publication.resource_manifest.get("budget_contracts") is not None:
            raise PolicyCompilationError("Budget contract publication requires an execution parent")
        compiler = self._compiler_for(publication, resolved)
        compiled = compiler.compile(routed_publication)
        client.verify_publication_permissions(compiled, recovery_only=already_promoted)
        if (
            status in {"verifying", "promoting"}
            and publication.policy_sha256 != compiled.policy_sha256
        ):
            raise PolicyCompilationError(
                "Candidate policy no longer matches the active compiler"
            )
        updates: dict[str, Any] = {}
        if status == "queued":
            validate_parent_policy(canonical_parent, canonical_parent)
            base_revision, live_policy = observed_current or client.current_api_policy()
            self._validate_current_parent(live_policy, publication)
            updates["resource_manifest"] = {
                **publication.resource_manifest,
                "apim_target_identity_sha256": resolved.target_identity_sha256,
                "base_apim_revision": base_revision,
                "base_policy_sha256": hashlib.sha256(live_policy.encode("utf-8")).hexdigest(),
                "base_policy_document_sha256": policy_document_digest(live_policy),
            }
        elif status == "validating":
            if any(
                binding.provider_brand_key.value == "microsoft_foundry"
                and binding.backend_pool is not None and any(
                    member.model_id is not None or (
                        member.upstream_model_id is not None
                        and member.upstream_model_id != binding.model.upstream_model_id
                    )
                    for member in binding.backend_pool.members
                ) for binding in publication.desired_spec.bindings
            ):
                validate_pool = getattr(client, "validate_pool_deployments", None)
                if not callable(validate_pool):
                    raise PolicyCompilationError("Pool deployment validation is unavailable")
                try:
                    validate_pool(publication, runtimes=registry["runtimes"])
                except TypeError:
                    validate_pool(publication)
            encrypted = self._repository.gateway_publication_credential(publication.id)
            credential = None
            if encrypted is not None:
                if self._cipher is None:
                    raise PolicyCompilationError("Credential decryption is unavailable")
                credential = self._cipher.decrypt(encrypted)
            latest_named_value = (
                publication.desired_spec.bindings[-1].named_value_name
                if publication.publication_kind is not PublicationKind.MODEL_REMOVE
                else None
            )
            latest_oauth = (
                publication.desired_spec.bindings[-1].oauth
                if publication.desired_spec.bindings else None
            )
            for oauth in compiled.oauth_credentials:
                materialized_oauth = (
                    replace(oauth, client_secret=credential)
                    if credential is not None and latest_oauth == oauth.config
                    else oauth
                )
                client.ensure_oauth_credential(materialized_oauth)
            for named_value in compiled.named_values:
                materialized = (
                    replace(named_value, value=credential)
                    if credential is not None and named_value.id == latest_named_value
                    else named_value
                )
                observed_credentials[named_value.id] = "unverified"
                try:
                    client.ensure_named_value(materialized)
                except CredentialSourceMissingError as error:
                    if error.named_value_id == named_value.id:
                        observed_credentials[named_value.id] = "missing"
                    raise
                observed_credentials[named_value.id] = "present"
            for backend in compiled.backends:
                client.ensure_backend(backend)
            updates["resource_manifest"] = {
                **publication.resource_manifest,
                "backends": [backend.id for backend in compiled.backends],
                "named_values": [value.id for value in compiled.named_values],
                "credential_evidence": publication_credential_evidence(
                    publication, observed_credentials,
                ),
                **({"oauth_credentials": [
                    item.config.provider_id for item in compiled.oauth_credentials
                ]} if compiled.oauth_credentials else {}),
            }
        elif status == "provisioning":
            revision = f"turnstile-{publication.generation}-{publication.id.hex[:8]}"
            client.ensure_revision(revision, f"Turnstile publication {publication.id}")
            updates["apim_revision"] = revision
        elif status == "building_revision":
            if not publication.apim_revision:
                raise RuntimeError("Publication is missing its APIM revision")
            base_revision, live_policy = client.current_api_policy()
            manifest = publication.resource_manifest
            live_hash = hashlib.sha256(live_policy.encode("utf-8")).hexdigest()
            expected_document = manifest.get("base_policy_document_sha256")
            matching_document = (
                isinstance(expected_document, str)
                and len(expected_document) == 64
                and policy_document_digest(live_policy) == expected_document
            )
            if (
                manifest.get("base_apim_revision") != base_revision
                or (manifest.get("base_policy_sha256") != live_hash and not matching_document)
            ):
                raise PolicyCompilationError(
                    "The current APIM policy changed after publication was queued"
                )
            parent = self._validate_current_parent(live_policy, publication)
            client.ensure_revision(
                publication.apim_revision, f"Turnstile publication {publication.id}"
            )
            if compiled.images_generations_policy is not None:
                effective = self._repository.effective_gateway_publication(
                    publication.gateway_profile_id
                )
                previous = GatewayPublication.model_validate(effective) if effective else None
                parent = compose_image_parent_policy(
                    parent,
                    canonical_parent,
                    [
                        binding.model.image_profile
                        for binding in routed_publication.desired_spec.bindings
                        if binding.model.image_profile is not None
                    ],
                    previous_profiles=[
                        binding.model.image_profile
                        for binding in previous.desired_spec.bindings
                        if binding.model.image_profile is not None
                    ]
                    if previous
                    else [],
                )
            client.put_api_policy(publication.apim_revision, parent)
            text_operations = (
                OperationResource(
                    self._chat_completions_operation_id, "Chat completions", "POST",
                    "/chat/completions",
                ),
                OperationResource(self._responses_operation_id, "Responses", "POST", "/responses"),
                OperationResource(
                    self._responses_compact_operation_id, "Compact responses", "POST",
                    "/responses/compact",
                ),
                OperationResource(self._messages_operation_id, "Messages", "POST", "/v1/messages"),
                OperationResource(
                    self._count_tokens_operation_id, "Count tokens", "POST",
                    "/v1/messages/count_tokens",
                ),
                OperationResource(self._models_operation_id, "Models", "GET", "/v1/models"),
            )
            for operation in (*text_operations, *compiled.operations):
                client.ensure_operation(publication.apim_revision, operation)
            for operation_id, policy in compiled.availability_probe_policies:
                client.put_operation_policy(publication.apim_revision, operation_id, policy)
            client.put_operation_policy(
                publication.apim_revision,
                self._chat_completions_operation_id,
                compiled.chat_completions_policy,
            )
            client.put_operation_policy(
                publication.apim_revision,
                self._responses_operation_id,
                compiled.responses_policy,
            )
            client.put_operation_policy(
                publication.apim_revision,
                self._responses_compact_operation_id,
                compiled.responses_compact_policy,
            )
            client.put_operation_policy(
                publication.apim_revision,
                self._messages_operation_id,
                compiled.messages_policy,
            )
            client.put_operation_policy(
                publication.apim_revision,
                self._count_tokens_operation_id,
                compiled.count_tokens_policy,
            )
            client.put_operation_policy(
                publication.apim_revision,
                self._models_operation_id,
                compiled.models_policy,
            )
            if compiled.images_generations_policy is not None:
                client.put_operation_policy(
                    publication.apim_revision, IMAGE_OPERATION_ID,
                    compiled.images_generations_policy,
                )
            updates["policy_sha256"] = compiled.policy_sha256
            updates["resource_manifest"] = {
                **publication.resource_manifest,
                **({"images_generations_operation": IMAGE_OPERATION_ID}
                   if compiled.images_generations_policy is not None else {}),
                "parent_policy_sha256": hashlib.sha256(
                    parent.encode("utf-8")
                ).hexdigest(),
                "parent_policy_contract_sha256": component_digest(
                    parse_policy(parent), normalize_text_defaults=False,
                ),
            }
        elif status == "verifying":
            if not publication.apim_revision:
                raise RuntimeError("Publication is missing its APIM revision")
            self._require_availability_coverage(routed_publication)
            self._validate_candidate_parent(publication, routed_publication, client)
            authorization = ImageProbeAuthorization.model_validate(
                publication.resource_manifest.get(
                    "image_probe_authorization", {"id": str(publication.id)}
                )
            )
            baseline_row = (
                self._repository.get_gateway_publication(publication.base_release_id)
                if publication.base_release_id is not None else None
            )
            verification_context = hashlib.sha256(json.dumps({
                "publication_id": str(publication.id),
                "revision": publication.apim_revision,
                "desired_spec_sha256": publication.desired_spec_sha256,
                "policy_sha256": compiled.policy_sha256,
                "parent_policy_sha256": publication.resource_manifest.get("parent_policy_sha256"),
                "target_identity_sha256": resolved.target_identity_sha256,
                "credential_generation": publication.resource_manifest.get("credential_generation"),
                "image_authorization": authorization.model_dump(mode="json"),
                "availability_revisions": routed_publication.resource_manifest.get(
                    "availability_probe_revisions", {},
                ),
                "availability_states": routed_publication.resource_manifest.get(
                    "availability_probe_states", {},
                ),
            }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

            def persist_probe_progress(
                expected: dict[str, Any] | None, checkpoint: dict[str, Any],
            ) -> None:
                if heartbeat is not None:
                    heartbeat()
                if not self._repository.checkpoint_gateway_publication_probes(
                    publication.id, worker_id, expected, checkpoint,
                ):
                    raise RuntimeError("Publication probe checkpoint or lease changed")

            verification = PublicationProbeProgress(
                verification_context, publication.resource_manifest.get("probe_verification"),
                persist_probe_progress, hard_deadline=verification_deadline,
            )
            if before_verification is not None:
                before_verification()
            journal = PersistentProbeJournal(
                    self._repository,
                    f"publication:{publication.id}",
                    authorization.id,
                    attempt_limit=authorization.attempt_limit,
                    heartbeat=heartbeat,
                    request_recorder=lambda method, endpoint: self._record_probe_request(
                        publication, worker_id, method, endpoint,
                    ),
                    verification=verification,
                    planned_model_ids={
                        publication.desired_spec.bindings[-1].model.model_key: (
                            publication.desired_spec.bindings[-1].logical_model_id
                            or publication_model_id(
                                publication.id,
                                publication.desired_spec.bindings[-1].model.model_key,
                            )
                        )
                    } if publication.publication_kind is PublicationKind.MODEL_ADD else None,
                    plan_context={
                        "credential_generation": publication.resource_manifest.get(
                            "credential_generation"
                        ),
                        "policy_sha256": compiled.policy_sha256,
                        "parent_policy_sha256": publication.resource_manifest.get(
                            "parent_policy_sha256"
                        ),
                    },
            )
            try:
                probe_results = client.probe_revision(
                    publication.apim_revision, routed_publication,
                    baseline_publication=(
                        GatewayPublication.model_validate(baseline_row)
                        if baseline_row is not None else None
                    ),
                    journal=journal,
                )
            except ProbeVerificationDeferred:
                row = self._repository.transition_gateway_publication(
                    publication.id, status, status,
                    {"error_code": "ProbeVerificationDeferred", "error_message":
                     "Continue verification from persisted successful steps"}, worker_id,
                )
                if row is None:
                    raise RuntimeError("Publication changed before slice yield") from None
                return GatewayPublication.model_validate(row)
            updates["resource_manifest"] = {
                **publication.resource_manifest, "probe_verification": verification.snapshot(),
            }
            if probe_results:
                updates["resource_manifest"] = {
                    **updates["resource_manifest"],
                    "gateway_probe_results": [
                        {
                            "deployment_id": str(result.deployment_id),
                            "native_gateway_id": result.native_gateway_id,
                            "required": result.required,
                            "status": result.status,
                            "error_code": result.error_code,
                            "error_message": result.error_message,
                        }
                        for result in probe_results
                    ],
                    "optional_gateway_probes_pending": sum(
                        result.status == "optional_pending"
                        for result in probe_results
                    ),
                }
        elif status == "promoting":
            if not publication.apim_revision:
                raise RuntimeError("Publication is missing its APIM revision")
            self._validate_candidate_parent(publication, routed_publication, client)
            if not already_promoted:
                if heartbeat is not None:
                    heartbeat()
                client.promote_revision(
                    publication.apim_revision, f"turnstile-{publication.id.hex[:12]}"
                )
            if client.current_revision() != publication.apim_revision:
                raise RetryablePublicationError(
                    "APIM did not report the promoted revision as current"
                )
            if heartbeat is not None:
                heartbeat()
            active = self._repository.activate_gateway_publication(publication.id, worker_id)
            return GatewayPublication.model_validate(active)
        else:
            return publication

        next_status = self._NEXT_STATUS[status]
        row = self._repository.transition_gateway_publication(
            publication.id, status, next_status, updates, worker_id
        )
        if row is None:
            raise RuntimeError("Publication changed while the worker held its lease")
        return GatewayPublication.model_validate(row)

    def _require_availability_coverage(self, publication: GatewayPublication) -> None:
        if publication.resource_manifest.get("model_availability") is None:
            return
        repository = cast(ModelAvailabilityRepository, self._repository)
        inventory = repository.get_model_availability_inventory()
        digest = inventory.get(publication.gateway_profile_id)
        coverage = repository.get_model_availability_coverage(publication.gateway_profile_id)
        revision = publication.apim_revision
        if (
            digest is None or coverage is None or revision is None
            or coverage.target_id != publication.gateway_profile_id
            or not coverage.current(datetime.now(UTC), digest)
        ):
            raise RetryablePublicationError("Candidate availability coverage is not ready")
        surfaces = [surface.split("/") for surface in coverage.verified_surfaces]
        if not any(parts[:2] == ["managed", revision] for parts in surfaces):
            raise RetryablePublicationError("Candidate Managed proof is not ready")
        for deployment in publication.desired_spec.gateway_deployments:
            if (
                deployment.native_gateway_id == "managed"
                or deployment.release_gate.value != "required"
            ):
                continue
            if not any(len(parts) == 4 and parts[0] == deployment.native_gateway_id
                       and parts[2] == revision for parts in surfaces):
                raise RetryablePublicationError("Candidate node proof is not ready")

    def _validate_current_parent(self, source: str, publication: GatewayPublication) -> str:
        effective = self._repository.effective_gateway_publication(publication.gateway_profile_id)
        previous = GatewayPublication.model_validate(effective) if effective else None
        profiles = [binding.model.image_profile for binding in previous.desired_spec.bindings
                    if binding.model.image_profile is not None] if previous else []
        canonical = self._parent_policy_overrides.get(
            publication.gateway_profile_id, self._parent_policy,
        )
        if publication.resource_manifest.get("model_availability") is not None:
            canonical = compose_availability_parent(canonical)
        return validate_parent_policy(source, canonical, profiles)

    def _validate_candidate_parent(
        self, publication: GatewayPublication, routed: GatewayPublication,
        client: ApimPublisherClient,
    ) -> None:
        assert publication.apim_revision is not None
        observed = client.revision_api_policy(publication.apim_revision)
        profiles = [binding.model.image_profile for binding in routed.desired_spec.bindings
                    if binding.model.image_profile is not None]
        canonical = self._parent_policy_overrides.get(
            publication.gateway_profile_id, self._parent_policy,
        )
        if publication.resource_manifest.get("model_availability") is not None:
            canonical = compose_availability_parent(canonical)
        validate_parent_readback(
            observed, canonical, profiles,
            expected_contract=publication.resource_manifest.get("parent_policy_contract_sha256"),
            expected_raw=publication.resource_manifest.get("parent_policy_sha256"),
        )

    def _compiler_for(
        self, publication: GatewayPublication,
        resolved: ResolvedApimPublisherClient | None = None,
    ) -> ApimPolicyCompiler:
        row = self._repository.get_apim_target(publication.gateway_profile_id)
        compiler = self._compiler
        if row is not None and (probe_subscription_id := row.get("probe_subscription_id")):
            compiler = compiler.with_probe_subscription(str(probe_subscription_id))
        if resolved is not None and resolved.usage_observer is not None:
            if resolved.gateway_profile_id != publication.gateway_profile_id:
                raise PolicyCompilationError(
                    "Observer configuration belongs to another APIM target"
                )
            return compiler.with_usage_observer(*resolved.usage_observer)
        if row is None:
            return compiler
        if self._usage_observer_target_id is not None:
            return (
                compiler
                if publication.gateway_profile_id == self._usage_observer_target_id
                else compiler.without_usage_observer()
            )
        capabilities = dict(row.get("capabilities") or {})
        if (
            capabilities.get("response_observer")
            == CapabilitySupport.UNSUPPORTED.value
        ):
            return compiler.without_usage_observer()
        return compiler
