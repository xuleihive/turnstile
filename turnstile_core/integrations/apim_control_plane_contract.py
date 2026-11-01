from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Literal, Protocol, cast, runtime_checkable
from uuid import UUID

from pydantic import SecretStr

from ..domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationSubscriptionProvisionSpec,
)
from ..domain.billable_requests import BillableRequestOutcome
from ..domain.control_plane import (
    PUBLICATION_OAUTH_ACTIONS as PUBLICATION_OAUTH_ACTIONS,
)
from ..domain.control_plane import (
    PUBLICATION_PROBE_ACTIONS as PUBLICATION_PROBE_ACTIONS,
)
from ..domain.control_plane import (
    PUBLICATION_RECOVERY_ACTIONS as PUBLICATION_RECOVERY_ACTIONS,
)
from ..domain.control_plane import (
    PUBLICATION_REQUIRED_ACTIONS as PUBLICATION_REQUIRED_ACTIONS,
)
from ..domain.control_plane import (
    GatewayModelBinding,
    GatewayPublication,
    GatewayReleaseDependencies,
)
from ..domain.gateway_targets import (
    ApimTargetArmInventory,
    SelfHostedGatewayBootstrap,
    SelfHostedGatewayBootstrapRequest,
    SelfHostedGatewayProvisionSpec,
)
from ..domain.runtime_models import OAuthClientCredentialsConfig


class PolicyCompilationError(ValueError):
    def __init__(self, message: str, *, billable_outcome: BillableRequestOutcome | None = None):
        super().__init__(message)
        self.billable_outcome = billable_outcome


class InfrastructureUpgradeRequiredError(PolicyCompilationError):
    pass


class GatewayCapabilityUnavailableError(PolicyCompilationError):
    def __init__(self, profile: str) -> None:
        self.profile = profile
        super().__init__("Gateway capability unavailable: " + json.dumps({
            "code": "gateway_capability_unavailable", "profile": profile,
        }, separators=(",", ":")))


class CredentialSourceMissingError(PolicyCompilationError):
    def __init__(self, named_value_id: str) -> None:
        self.named_value_id = named_value_id
        super().__init__(f"Named value {named_value_id} has no credential source")


class PublisherPermissionError(PolicyCompilationError):
    def __init__(
        self, *, missing_actions: Sequence[str] = (), request_id: str | None = None,
    ) -> None:
        self.missing_actions = tuple(sorted(set(missing_actions)))
        self.request_id = request_id
        super().__init__("Publisher permission preflight failed: " + json.dumps({
            "code": "publisher_permissions_missing"
            if self.missing_actions else "publisher_permissions_unverified",
            "missing_actions": list(self.missing_actions),
            "request_id": request_id,
        }, separators=(",", ":")))


def missing_management_actions(payload: object, required: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(payload, dict) or payload.get("nextLink"):
        raise ValueError("Permission inventory is incomplete")
    permissions = payload.get("value")
    if not isinstance(permissions, list):
        raise ValueError("Permission inventory is invalid")
    granted: set[str] = set()
    for permission in permissions:
        if not isinstance(permission, dict) or permission.get("condition"):
            raise ValueError("Permission conditions cannot be verified")
        actions, excluded = permission.get("actions"), permission.get("notActions", [])
        if (
            not isinstance(actions, list) or not isinstance(excluded, list)
            or any(not isinstance(action, str) for action in [*actions, *excluded])
        ):
            raise ValueError("Permission inventory has invalid actions")
        granted.update(action for action in required if any(
            fnmatchcase(action.casefold(), allowed.casefold()) for allowed in actions
        ) and not any(
            fnmatchcase(action.casefold(), denied.casefold()) for denied in excluded
        ))
    return tuple(sorted(set(required) - granted))


class RetryablePublicationError(RuntimeError):
    pass


class ProbeVerificationDeferred(RetryablePublicationError):
    pass


class ProbeVerificationJournal(Protocol):
    def run_step(self, key: str, verify: Callable[[], None]) -> None: ...

    def request_timeout(self, maximum: float) -> float: ...


class ProbeRequestLimitExceeded(PolicyCompilationError):
    pass


class AuthorizationRequiredError(RuntimeError):
    def __init__(self, message: str, *, billable_outcome: BillableRequestOutcome | None = None):
        super().__init__(message)
        self.billable_outcome = billable_outcome


class ManagementTokenProvider(Protocol):
    def token(self, resource: str) -> str: ...


class ImageProbeJournal(Protocol):
    def heartbeat(self) -> None: ...

    def record_request(self, method: str, endpoint: str) -> None: ...

    def run(
        self, binding: GatewayModelBinding, revision: str, body: Mapping[str, Any],
        send: Callable[[str, str], dict[str, Any]],
        *, gateway_endpoint: str | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class GatewayEndpointProbeResult:
    deployment_id: UUID
    native_gateway_id: str
    required: bool
    status: Literal["succeeded", "optional_pending"]
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class BackendResource:
    id: str
    title: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    circuit_breaker: BackendCircuitBreakerResource | None = None


@dataclass(frozen=True)
class BackendCircuitBreakerResource:
    failure_count: int
    interval_seconds: int
    trip_duration_seconds: int
    accept_retry_after: bool
    status_code_ranges: tuple[tuple[int, int], ...] = (
        (429, 429),
        (408, 408),
        (500, 599),
    )
    error_reasons: tuple[str, ...] = ("BackendConnectionFailure", "Timeout")


@dataclass(frozen=True)
class BackendPoolMemberResource:
    backend_id: str
    priority: int
    weight: int


@dataclass(frozen=True)
class BackendPoolResource(BackendResource):
    members: tuple[BackendPoolMemberResource, ...] = ()
    session_cookie_name: str | None = None


@dataclass(frozen=True)
class NamedValueResource:
    id: str
    key_vault_secret_id: str | None
    owner_publication_id: str | None = None
    value: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OAuthCredentialResource:
    config: OAuthClientCredentialsConfig
    client_secret: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OperationResource:
    id: str
    display_name: str
    method: Literal["GET", "POST", "HEAD"]
    path: str


@dataclass(frozen=True)
class CompiledGatewayRelease:
    chat_completions_policy: str
    responses_policy: str
    responses_compact_policy: str
    messages_policy: str
    count_tokens_policy: str
    models_policy: str
    policy_sha256: str
    backends: tuple[BackendResource, ...]
    named_values: tuple[NamedValueResource, ...]
    images_generations_policy: str | None = None
    operations: tuple[OperationResource, ...] = ()
    oauth_credentials: tuple[OAuthCredentialResource, ...] = ()
    availability_probe_policies: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ReleaseGcPlanEvidence:
    current_non_release_references: dict[str, object]
    candidates: tuple[dict[str, object], ...]
    reference_graph_sha256: str


class ApimClientSettings(Protocol):
    @property
    def apim_gateway_profile_id(self) -> UUID | None: ...

    @property
    def ledger_table_endpoint(self) -> str | None: ...

    @property
    def ledger_table_name(self) -> str | None: ...

    databricks_oauth_enabled: bool
    apim_principal_id: str | None
    credential_encryption_key: SecretStr | None
    azure_subscription_id: str | None
    apim_resource_group: str | None
    apim_service_name: str | None
    apim_api_id: str
    apim_gateway_url: str | None
    apim_probe_subscription_key: SecretStr | None
    apim_probe_subscription_id: str
    @property
    def apim_regression_model_key(self) -> str | None: ...
    apim_usage_observer_url: str | None
    apim_usage_observer_key_named_value: str | None
    apim_subscription_agent_map: dict[str, dict[str, str]]
    apim_chat_completions_operation_id: str
    apim_responses_operation_id: str
    apim_responses_compact_operation_id: str
    apim_messages_operation_id: str
    apim_count_tokens_operation_id: str
    apim_models_operation_id: str


class ApimSubscriptionKeyClient(Protocol):
    def application_subscription_keys(
        self, apim_subscription_id: str
    ) -> tuple[str, str]: ...

    def regenerate_application_subscription_key(
        self,
        apim_subscription_id: str,
        key_kind: Literal["primary", "secondary"],
    ) -> None: ...


class ApimSubscriptionKeyClientResolver(Protocol):
    def resolve(self, gateway_profile_id: UUID) -> ApimSubscriptionKeyClient: ...


@runtime_checkable
class SelfHostedGatewayManagementClient(Protocol):
    def ensure_self_hosted_gateway(
        self, spec: SelfHostedGatewayProvisionSpec, before_write: Callable[[], None],
    ) -> None: ...

    def self_hosted_gateway_bootstrap(
        self, spec: SelfHostedGatewayProvisionSpec, request: SelfHostedGatewayBootstrapRequest,
        image: str,
    ) -> SelfHostedGatewayBootstrap: ...


class ApimPublisherClient(Protocol):
    def verify_publication_permissions(
        self, compiled: CompiledGatewayRelease, *, recovery_only: bool = False,
    ) -> None: ...

    def discover_apim_target_inventory(
        self, gateway_profile_id: UUID
    ) -> ApimTargetArmInventory: ...

    def ensure_backend(self, backend: BackendResource) -> None: ...

    def ensure_named_value(self, named_value: NamedValueResource) -> None: ...

    def ensure_oauth_credential(self, credential: OAuthCredentialResource) -> None: ...

    def ensure_revision(self, revision: str, description: str) -> None: ...

    def put_api_policy(self, revision: str, policy: str) -> None: ...

    def ensure_operation(self, revision: str, operation: OperationResource) -> None: ...

    def put_operation_policy(self, revision: str, operation: str, policy: str) -> None: ...

    def probe_revision(
        self,
        revision: str,
        publication: GatewayPublication,
        *,
        journal: ImageProbeJournal | None = None,
        baseline_publication: GatewayPublication | None = None,
    ) -> tuple[GatewayEndpointProbeResult, ...] | None: ...

    def promote_revision(self, revision: str, release_name: str) -> None: ...

    def current_revision(self) -> str | None: ...

    def current_api_policy(self) -> tuple[str, str]: ...

    def revision_api_policy(self, revision: str) -> str: ...

    def inspect_revision_dependencies(
        self, publication: GatewayPublication
    ) -> GatewayReleaseDependencies: ...

    def plan_release_garbage_collection(
        self,
        releases: Sequence[GatewayPublication],
        retained_release_ids: set[str],
        *, fixed_dependencies: Mapping[str, GatewayReleaseDependencies] | None = None,
    ) -> ReleaseGcPlanEvidence: ...

    def discover_gateway_applications(
        self, gateway_profile_id: UUID
    ) -> GatewayApplicationDiscovery: ...

    def ensure_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str,
        secondary_key: str,
    ) -> None: ...

    def activate_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str,
        secondary_key: str,
    ) -> None: ...


@dataclass(frozen=True)
class ResolvedApimPublisherClient:
    gateway_profile_id: UUID
    target_identity_sha256: str
    enforce_identity_snapshot: bool
    client: ApimPublisherClient
    usage_observer: tuple[str | None, str | None] | None = None
    route_target_identity_sha256: str | None = None
    legacy_route_target_identity_sha256: str | None = None


class ApimPublisherClientResolver(Protocol):
    def resolve(
        self, gateway_profile_id: UUID, *, include_probe_credentials: bool = True,
    ) -> ResolvedApimPublisherClient: ...


class FixedApimPublisherClientResolver:
    """Compatibility adapter for isolated tests and one-client callers."""

    def __init__(self, client: ApimPublisherClient) -> None:
        self._client = client

    def resolve(
        self, gateway_profile_id: UUID, *, include_probe_credentials: bool = True,
    ) -> ResolvedApimPublisherClient:
        del include_probe_credentials
        return ResolvedApimPublisherClient(
            gateway_profile_id=gateway_profile_id,
            target_identity_sha256=hashlib.sha256(
                f"fixed-client:{gateway_profile_id}".encode()
            ).hexdigest(),
            enforce_identity_snapshot=False,
            client=self._client,
        )


class FixedApimSubscriptionKeyClientResolver:
    """Compatibility adapter for isolated tests and one-client callers."""

    def __init__(self, client: ApimSubscriptionKeyClient) -> None:
        self._client = client

    def resolve(self, gateway_profile_id: UUID) -> ApimSubscriptionKeyClient:
        del gateway_profile_id
        return self._client


def coerce_apim_publisher_client_resolver(
    value: ApimPublisherClient | ApimPublisherClientResolver,
) -> ApimPublisherClientResolver:
    if callable(getattr(value, "resolve", None)):
        return cast(ApimPublisherClientResolver, value)
    return FixedApimPublisherClientResolver(cast(ApimPublisherClient, value))


def coerce_apim_subscription_key_client_resolver(
    value: ApimSubscriptionKeyClient | ApimSubscriptionKeyClientResolver,
) -> ApimSubscriptionKeyClientResolver:
    if callable(getattr(value, "resolve", None)):
        return cast(ApimSubscriptionKeyClientResolver, value)
    return FixedApimSubscriptionKeyClientResolver(
        cast(ApimSubscriptionKeyClient, value)
    )
