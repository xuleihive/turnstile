from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

from ..domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationSubscriptionProvisionSpec,
)
from ..domain.billable_requests import BillableRequestOutcome
from ..domain.control_plane import (
    GatewayModelBinding,
    GatewayPublication,
    GatewayReleaseDependencies,
)
from ..domain.runtime_models import OAuthClientCredentialsConfig


class PolicyCompilationError(ValueError):
    def __init__(self, message: str, *, billable_outcome: BillableRequestOutcome | None = None):
        super().__init__(message)
        self.billable_outcome = billable_outcome


class InfrastructureUpgradeRequiredError(PolicyCompilationError):
    pass


class RetryablePublicationError(RuntimeError):
    pass


class AuthorizationRequiredError(RuntimeError):
    def __init__(self, message: str, *, billable_outcome: BillableRequestOutcome | None = None):
        super().__init__(message)
        self.billable_outcome = billable_outcome


class ImageProbeJournal(Protocol):
    def heartbeat(self) -> None: ...

    def run(
        self,
        binding: GatewayModelBinding,
        revision: str,
        body: Mapping[str, Any],
        send: Callable[[str, str], dict[str, Any]],
    ) -> None: ...


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
    method: Literal["GET", "POST"]
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


@dataclass(frozen=True)
class ReleaseGcPlanEvidence:
    current_non_release_references: dict[str, object]
    candidates: tuple[dict[str, object], ...]
    reference_graph_sha256: str


class ApimSubscriptionKeyClient(Protocol):
    def application_subscription_keys(
        self, apim_subscription_id: str
    ) -> tuple[str, str]: ...

    def regenerate_application_subscription_key(
        self,
        apim_subscription_id: str,
        key_kind: Literal["primary", "secondary"],
    ) -> None: ...

    def cancel_application_subscription(self, apim_subscription_id: str) -> None: ...


class ApimPublisherClient(Protocol):
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
    ) -> None: ...

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
    ) -> ReleaseGcPlanEvidence: ...

    def discover_gateway_applications(
        self, gateway_profile_id: UUID
    ) -> GatewayApplicationDiscovery: ...

    def ensure_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        *,
        legacy_pending_name: str | None = None,
    ) -> None: ...

    def activate_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        *,
        legacy_pending_name: str | None = None,
    ) -> None: ...
