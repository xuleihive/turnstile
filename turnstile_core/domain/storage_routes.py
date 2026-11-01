from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, TypeVar
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from pydantic import AfterValidator, AwareDatetime, ConfigDict, Field, model_validator

from .budget_counting import BudgetOperation, ModelCountContract
from .control_plane import (
    GatewayPublication,
    GatewayRateLimitResilience,
)
from .image_profiles import ImageGenerationLimits, ImageGenerationProfile, validate_image_profile
from .models import StrictModel
from .runtime_models import OAuthClientCredentialsConfig

POLICY_ABI = 2
MAX_ROUTE_BYTES = 16384
MAX_MEMBER_BYTES = 24576
MAX_HEAD_BYTES = 4096
PROFILE_OPERATIONS: Mapping[str, frozenset[str]] = {
    "openai-v1": frozenset({"chat-completions", "responses", "responses-compact"}),
    "anthropic-messages": frozenset({"anthropic-messages"}),
    "openai-images": frozenset({"images-generations"}),
    "microsoft-decision": frozenset({"microsoft-decision"}),
}
RouteProfile = Literal["openai-v1", "anthropic-messages", "openai-images", "microsoft-decision"]
RouteRequestAdapter = Literal["bedrock-anthropic-v1"]
Digest = Annotated[str, Field(pattern=re.compile(r"\A[0-9a-f]{64}\Z"))]
EpochSeconds = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
Generation = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
Alias = Annotated[str, Field(pattern=re.compile(r"\A[a-zA-Z0-9._:-]{1,255}\Z"))]
Identifier = Annotated[str, Field(pattern=re.compile(r"\A[a-zA-Z0-9._-]{1,180}\Z"))]


def canonical_uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("ROUTE_NONCANONICAL_UUID")
    return value


CanonicalUuid = Annotated[str, AfterValidator(canonical_uuid)]


def canonical_document(value: Mapping[str, Any] | StrictModel) -> str:
    fields = value.model_dump(mode="json") if isinstance(value, StrictModel) else dict(value)
    return json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def document_digest(value: Mapping[str, Any] | StrictModel) -> str:
    return hashlib.sha256(canonical_document(value).encode()).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("ROUTE_DUPLICATE_JSON_PROPERTY")
        result[name] = value
    return result


class RouteModel(StrictModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )


RouteModelType = TypeVar("RouteModelType", bound=RouteModel)


def parse_document(model: type[RouteModelType], document: str, maximum: int) -> RouteModelType:
    if len(document.encode()) > maximum or len(document.encode("utf-16-le")) > 32768:
        raise ValueError("ROUTE_DOCUMENT_TOO_LARGE")
    json.loads(document, object_pairs_hook=unique_object)
    return model.model_validate_json(document, strict=True)


class RouteScope(RouteModel):
    target_id: CanonicalUuid
    api_id: Identifier
    gateway_id: Identifier
    policy_abi: Annotated[int, Field(strict=True, ge=POLICY_ABI, le=POLICY_ABI)] = POLICY_ABI

    @property
    def partition(self) -> str:
        return "route-v2:" + document_digest(self)


class RouteCredential(RouteModel):
    mode: Literal[
        "none", "managed_identity", "key_vault_api_key", "key_vault_bearer",
        "oauth_client_credentials",
    ]
    header: Literal["", "api-key", "x-api-key", "Authorization"] = ""
    secret_id: str = ""
    audience: str = ""
    oauth: OAuthClientCredentialsConfig | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_credential(self) -> RouteCredential:
        if self.mode != "oauth_client_credentials" and self.oauth is not None:
            raise ValueError("ROUTE_OAUTH_CREDENTIAL_MODE_INVALID")
        if self.mode == "oauth_client_credentials":
            if (self.header != "Authorization" or self.secret_id
                    or self.audience or self.oauth is None):
                raise ValueError("ROUTE_OAUTH_CREDENTIAL_INVALID")
        elif self.mode == "none":
            if self.header or self.secret_id or self.audience:
                raise ValueError("ROUTE_UNAUTHENTICATED_CREDENTIAL_FIELDS")
        elif self.mode == "managed_identity":
            if self.header != "Authorization" or self.secret_id or not self.audience:
                raise ValueError("ROUTE_MANAGED_IDENTITY_INVALID")
            if not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                self.audience,
            ):
                validate_origin(self.audience.rstrip("/"))
        else:
            secret = urlsplit(self.secret_id)
            if (
                secret.scheme != "https"
                or secret.port not in {None, 443}
                or secret.username
                or secret.password
                or secret.query
                or secret.fragment
                or not re.fullmatch(r"[a-z0-9-]{3,24}\.vault\.azure\.net", secret.hostname or "")
                or not re.fullmatch(r"/secrets/[a-zA-Z0-9-]{1,127}/[0-9a-f]{32}", secret.path)
                or self.audience
                or (self.mode == "key_vault_bearer" and self.header != "Authorization")
                or (
                    self.mode == "key_vault_api_key" and self.header not in {"api-key", "x-api-key"}
                )
            ):
                raise ValueError("ROUTE_VERSIONED_SECRET_INVALID")
        return self


class CountCredential(RouteCredential):
    origin: str
    endpoints: tuple[str, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_endpoints(self) -> CountCredential:
        validate_origin(self.origin)
        if len(set(self.endpoints)) != len(self.endpoints):
            raise ValueError("ROUTE_COUNT_ENDPOINTS_INVALID")
        for endpoint in self.endpoints:
            validate_path(endpoint)
            if not endpoint.endswith(("/messages/count_tokens", "/responses/input_tokens")):
                raise ValueError("ROUTE_COUNT_ENDPOINT_INVALID")
        return self


def validate_path(value: str) -> None:
    address = urlsplit(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or address.scheme
        or address.netloc
        or address.query
        or address.fragment
        or ".." in value.split("/")
        or not value.isascii()
        or len(value) > 1000
        or "\\" in value
        or "%" in value
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError("ROUTE_BACKEND_PATH_INVALID")


def validate_origin(value: str) -> None:
    address = urlsplit(value)
    if (
        address.scheme != "https"
        or not address.hostname
        or address.username
        or address.password
        or address.port not in {None, 443}
        or address.path
        or address.query
        or address.fragment
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        raise ValueError("ROUTE_HTTPS_ORIGIN_REQUIRED")


class RoutePoolMember(RouteModel):
    runtime_id: CanonicalUuid
    backend_id: Identifier
    dependency_sha256: Digest
    priority: int = Field(strict=True, ge=0, le=100)
    weight: int = Field(strict=True, ge=0, le=100)


class RouteBackendPool(RouteModel):
    members: tuple[RoutePoolMember, ...] = Field(min_length=2, max_length=30)
    rate_limit: GatewayRateLimitResilience
    session_cookie_name: str | None = Field(
        default=None, pattern=re.compile(r"\ATurnstileAffinity-[0-9a-f]{16}\Z"),
    )
    credential_versions: dict[Identifier, str] = Field(default_factory=dict, max_length=30)
    header_values: dict[Identifier, str] = Field(
        default_factory=dict, max_length=90, exclude_if=lambda value: not value,
    )
    configuration_sha256: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_members(self) -> RouteBackendPool:
        priority_weights: dict[int, int] = {}
        for member in self.members:
            priority_weights[member.priority] = (
                priority_weights.get(member.priority, 0) + member.weight
            )
        if any(weight == 0 for weight in priority_weights.values()):
            raise ValueError("every backend priority group needs a positive weight")
        if len({member.backend_id.casefold() for member in self.members}) != len(self.members):
            raise ValueError("ROUTE_POOL_BACKEND_REPEATED")
        for secret_id in self.credential_versions.values():
            RouteCredential(mode="key_vault_bearer", header="Authorization", secret_id=secret_id)
        if self.header_values.keys() & self.credential_versions.keys() or any(
            not value or len(value) > 255 or any(ord(character) < 32 for character in value)
            or any(marker in value for marker in ("@(", "@{", "{{", "}}"))
            for value in self.header_values.values()
        ):
            raise ValueError("ROUTE_POOL_PUBLIC_HEADER_INVALID")
        return self


class ConnectionRevision(RouteModel):
    scope: RouteScope
    revision_id: CanonicalUuid
    backend_id: Identifier
    origin: str
    path: str = Field(min_length=1, max_length=1000)
    protocol_paths: dict[BudgetOperation, str] = Field(default_factory=dict, max_length=5)
    dependency_sha256: Digest
    credential: RouteCredential
    observer: bool = False
    count: CountCredential | None = None
    public_count_path: str | None = Field(default=None, exclude_if=lambda value: value is None)
    pool: RouteBackendPool | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_destination(self) -> ConnectionRevision:
        validate_origin(self.origin)
        for value in (self.path, *self.protocol_paths.values()):
            validate_path(value)
        if self.public_count_path is not None:
            validate_path(self.public_count_path)
            if not self.public_count_path.endswith("/messages/count_tokens"):
                raise ValueError("ROUTE_PUBLIC_COUNT_PATH_INVALID")
        if self.count is not None and self.count.origin != self.origin:
            raise ValueError("ROUTE_COUNTER_ORIGIN_MISMATCH")
        for credential in (self.credential, self.count):
            if credential is not None and credential.oauth is not None and (
                self.scope.gateway_id != "managed"
                or str(credential.oauth.token_url) != self.origin + "/oidc/v1/token"
            ):
                raise ValueError("ROUTE_OAUTH_GATEWAY_OR_WORKSPACE_MISMATCH")
        if self.pool is not None and (
            self.backend_id.casefold() in {
                member.backend_id.casefold() for member in self.pool.members
            }
            or self.credential.mode not in {"none", "managed_identity"}
        ):
            raise ValueError("ROUTE_POOL_CONNECTION_INVALID")
        return self

    def require_probe_member(self, member_backend_id: str | None) -> None:
        if self.pool is None:
            if member_backend_id is not None:
                raise ValueError("ROUTE_PROBE_POOL_UNEXPECTED")
        elif member_backend_id not in {member.backend_id for member in self.pool.members}:
            raise ValueError("ROUTE_PROBE_POOL_MEMBER_INVALID")


class ConnectionApproval(RouteModel):
    approval_id: CanonicalUuid
    connection: ConnectionRevision
    state: Literal["approved", "revoked"]
    evidence_sha256: Digest
    issued_epoch: EpochSeconds
    expires_epoch: EpochSeconds

    @model_validator(mode="after")
    def validate_window(self) -> ConnectionApproval:
        if not 0 < self.expires_epoch - self.issued_epoch <= 86400:
            raise ValueError("ROUTE_CONNECTION_APPROVAL_WINDOW_INVALID")
        return self

    def require_current(self, connection: ConnectionRevision, now: int) -> None:
        if (
            type(now) is not int
            or self.connection != connection
            or self.state != "approved"
            or self.issued_epoch > now
        ):
            raise ValueError("ROUTE_CONNECTION_APPROVAL_INVALID")


class RouteContent(RouteModel):
    scope: RouteScope
    model_id: CanonicalUuid
    alias: Alias
    display_name: str = Field(min_length=1, max_length=255)
    created_at: AwareDatetime
    upstream_model: str = Field(pattern=re.compile(r"\A[a-zA-Z0-9._:/-]{1,255}\Z"))
    profile: RouteProfile
    provider_name: str = Field(default="unattributed", min_length=1, max_length=120)
    runtime_name: str = Field(default="unattributed", min_length=1, max_length=160)
    context_window: int = Field(default=0, strict=True, ge=0, le=2**53 - 1)
    assignment_required: bool = False
    streaming_mode: Literal["native", "buffered"] = "native"
    operations: tuple[BudgetOperation, ...] = Field(min_length=1, max_length=5)
    connection: ConnectionRevision
    budget_epoch: CanonicalUuid
    budget: ModelCountContract
    image_profile: ImageGenerationProfile | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    request_adapter: RouteRequestAdapter | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_bindings(self) -> RouteContent:
        if self.connection.scope != self.scope or str(self.budget.model_id) != self.model_id:
            raise ValueError("ROUTE_CONTENT_IDENTITY_MISMATCH")
        if self.connection.public_count_path is not None and self.profile != "anthropic-messages":
            raise ValueError("ROUTE_PUBLIC_COUNT_PROFILE_INVALID")
        pool = self.connection.pool
        if pool is not None and (
            self.profile == "openai-images" or pool.session_cookie_name is not None
            and pool.session_cookie_name != "TurnstileAffinity-" + text_digest(
                self.scope.target_id + ":" + self.alias,
            )[:16]
        ):
            raise ValueError("ROUTE_POOL_MODEL_BINDING_INVALID")
        if self.request_adapter is not None and (
            self.profile != "anthropic-messages" or self.streaming_mode != "buffered"
            or self.operations != ("anthropic-messages",)
            or self.connection.path != "/model/" + self.upstream_model + "/invoke"
            or self.connection.protocol_paths.get("anthropic-messages", self.connection.path)
            != self.connection.path
            or self.connection.public_count_path is not None
        ):
            raise ValueError("ROUTE_BEDROCK_ADAPTER_BINDING_INVALID")
        if self.profile == "openai-images":
            profile = validate_image_profile(self.image_profile)
            if profile.provider_model != self.upstream_model:
                raise ValueError("ROUTE_IMAGE_PROFILE_MODEL_INVALID")
        elif self.image_profile is not None:
            raise ValueError("ROUTE_IMAGE_PROFILE_UNEXPECTED")
        if (
            len(set(self.operations)) != len(self.operations)
            or not set(self.operations) <= PROFILE_OPERATIONS[self.profile]
        ):
            raise ValueError("ROUTE_PROFILE_OPERATION_MISMATCH")
        for operation in self.operations:
            if len(self.operations) > 1 and operation not in self.connection.protocol_paths:
                raise ValueError("ROUTE_PROTOCOL_PATH_MISSING")
            contract = self.budget.protocols.get(operation, self.budget)
            if operation not in contract.operations:
                raise ValueError("ROUTE_BUDGET_OPERATION_MISSING")
            counter = contract.native_counter
            if counter is not None and (
                self.connection.count is None
                or str(counter.endpoint)
                not in {
                    self.connection.count.origin + path for path in self.connection.count.endpoints
                }
            ):
                raise ValueError("ROUTE_COUNTER_NOT_APPROVED")
        if len(canonical_document(self).encode()) > MAX_ROUTE_BYTES:
            raise ValueError("ROUTE_CONTENT_TOO_LARGE")
        return self

    @property
    def sha256(self) -> str:
        return document_digest(self)


class ProgramIdentity(RouteModel):
    scope: RouteScope
    api_revision: Identifier
    instance_id: str = Field(min_length=1, max_length=256)
    program_sha256: Digest
    trust_binding_sha256: Digest


def require_route_storage(endpoint: str, table: str) -> None:
    if not isinstance(endpoint, str) or not isinstance(table, str):
        raise ValueError("ROUTE_PRIMARY_DEDICATED_TABLE_REQUIRED")
    address = urlsplit(endpoint)
    if (
        address.scheme != "https" or address.username or address.password
        or address.query or address.fragment or address.port not in {None, 443}
        or address.path not in {"", "/"}
        or not re.fullmatch(r"[a-z0-9]{3,24}\.table\.core\.windows\.net", address.hostname or "")
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{2,62}", table)
        or table.casefold() == "turnstileledger"
    ):
        raise ValueError("ROUTE_PRIMARY_DEDICATED_TABLE_REQUIRED")


class FixedProgramInstallation(RouteModel):
    version: int = Field(default=1, ge=1, le=1, strict=True)
    program: ProgramIdentity
    target_identity_sha256: Digest
    storage_endpoint: str
    storage_table: str
    gateway_endpoint: str
    publisher_subscription_id: Identifier
    parent_policy_sha256: Digest
    operation_policies_sha256: dict[Identifier, Digest] = Field(min_length=1, max_length=30)
    operation_contracts: dict[Identifier, tuple[str, str]] = Field(min_length=1, max_length=30)
    retired_operations: dict[Identifier, tuple[Literal["POST"], str]] = Field(
        default_factory=dict, max_length=16, exclude_if=lambda value: not value,
    )
    supported_profiles: tuple[RouteProfile, ...] = Field(min_length=1)
    credential_vault_origin: str | None = None
    trusted_vault_origins: tuple[str, ...] = Field(default=(), max_length=8)
    image_limits: ImageGenerationLimits | None = None
    oauth_enabled: bool = Field(default=False, exclude_if=lambda value: value is False)
    pool_enabled: bool = Field(default=False, exclude_if=lambda value: value is False)
    router_enabled: bool = Field(default=False, exclude_if=lambda value: value is False)
    decision_enabled: bool = Field(default=False, exclude_if=lambda value: value is False)
    model_additions_enabled: bool = Field(default=False, exclude_if=lambda value: value is False)
    native_gateway_scope: bool = Field(default=False, exclude_if=lambda value: value is False)
    legacy_program_sha256: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    predecessor_program_sha256: tuple[Digest, ...] = Field(
        default=(), max_length=32, exclude_if=lambda value: not value,
    )
    supported_adapters: tuple[RouteRequestAdapter, ...] = Field(
        default=(), max_length=1, exclude_if=lambda value: not value,
    )

    def for_native_gateway(
        self, gateway_id: str, instance_id: str, endpoint: str,
    ) -> FixedProgramInstallation:
        if not self.native_gateway_scope and (
            gateway_id != self.program.scope.gateway_id
            or instance_id != self.program.instance_id or endpoint != self.gateway_endpoint
        ):
            raise ValueError("ROUTE_PROGRAM_NATIVE_GATEWAY_SCOPE_REQUIRED")
        scope = RouteScope(
            target_id=self.program.scope.target_id, api_id=self.program.scope.api_id,
            gateway_id=gateway_id,
        )
        identity = ProgramIdentity(
            scope=scope, api_revision=self.program.api_revision, instance_id=instance_id,
            program_sha256=self.program.program_sha256,
            trust_binding_sha256=self.program.trust_binding_sha256,
        )
        return FixedProgramInstallation.model_validate_json(canonical_document({
            **self.model_dump(mode="json"), "program": identity.model_dump(mode="json"),
            "gateway_endpoint": endpoint,
        }))

    @model_validator(mode="after")
    def require_bound_installation(self) -> FixedProgramInstallation:
        require_route_storage(self.storage_endpoint, self.storage_table)
        if self.legacy_program_sha256 is not None and not self.model_additions_enabled:
            raise ValueError("MODEL_ADDITION_LEGACY_PROGRAM_REQUIRES_CAPABILITY")
        if self.predecessor_program_sha256 and (
            not self.model_additions_enabled
            or len(set(self.predecessor_program_sha256)) != len(self.predecessor_program_sha256)
            or self.program.program_sha256 in self.predecessor_program_sha256
            or self.legacy_program_sha256 in self.predecessor_program_sha256
        ):
            raise ValueError("ROUTE_PREDECESSOR_PROGRAM_BINDING_INVALID")
        if ("openai-images" in self.supported_profiles) != (self.image_limits is not None):
            raise ValueError("ROUTE_PROGRAM_IMAGE_CAPACITY_REQUIRED")
        if ("microsoft-decision" in self.supported_profiles) != self.decision_enabled or (
            self.decision_enabled and self.operation_contracts.get("microsoft-decision")
            != ("POST", "/providers/microsoft/v1/systemone")
        ):
            raise ValueError("ROUTE_PROGRAM_DECISION_CAPABILITY_REQUIRED")
        if self.oauth_enabled and self.program.scope.gateway_id != "managed":
            raise ValueError("ROUTE_OAUTH_MANAGED_GATEWAY_REQUIRED")
        endpoint = urlsplit(self.gateway_endpoint)
        if (
            endpoint.scheme != "https" or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.port not in {None, 443} or self.gateway_endpoint.endswith("/")
            or ";" in endpoint.path or "%" in endpoint.path
            or set(self.operation_contracts) != set(self.operation_policies_sha256)
            or len(set(self.supported_profiles)) != len(self.supported_profiles)
            or len(set(self.trusted_vault_origins)) != len(self.trusted_vault_origins)
            or any(not re.fullmatch(r"https://[a-z0-9-]{3,24}\.vault\.azure\.net", value)
                   for value in self.trusted_vault_origins)
            or self.credential_vault_origin is not None
            and self.credential_vault_origin not in self.trusted_vault_origins
        ):
            raise ValueError("ROUTE_PROGRAM_INSTALLATION_INVALID")
        return self


class FixedProgramUpgrade(RouteModel):
    version: int = Field(default=1, ge=1, le=1, strict=True)
    previous_publication_id: CanonicalUuid
    previous_installation_sha256: Digest
    installation: FixedProgramInstallation

    def require_previous(
        self, publication_id: str, previous: FixedProgramInstallation,
    ) -> None:
        current = self.installation
        changed = {"program", "parent_policy_sha256", "model_additions_enabled",
                   "legacy_program_sha256", "operation_policies_sha256",
                   "predecessor_program_sha256"}
        if current.pool_enabled and not previous.pool_enabled:
            changed.add("pool_enabled")
        if current.router_enabled and not previous.router_enabled:
            changed.add("router_enabled")
        profiles = previous.supported_profiles
        if current.image_limits is not None and previous.image_limits is None:
            profiles = (*profiles, "openai-images")
            changed.update({"image_limits", "supported_profiles"})
        if current.decision_enabled and not previous.decision_enabled:
            if (current.operation_contracts != {
                **previous.operation_contracts,
                "microsoft-decision": ("POST", "/providers/microsoft/v1/systemone"),
            } or current.supported_profiles != (*profiles, "microsoft-decision")):
                raise ValueError("DECISION_PROGRAM_UPGRADE_CONTRACT_CHANGED")
            changed.update({"decision_enabled", "operation_contracts", "supported_profiles"})
        elif "image_limits" in changed and current.supported_profiles != profiles:
            raise ValueError("IMAGE_PROGRAM_UPGRADE_CONTRACT_CHANGED")
        if (
            self.previous_publication_id != publication_id
            or self.previous_installation_sha256 != document_digest(previous)
            or not current.model_additions_enabled
            or current.legacy_program_sha256
            != (previous.legacy_program_sha256 or previous.program.program_sha256)
            or current.predecessor_program_sha256 and (
                previous.program.program_sha256 != current.legacy_program_sha256
                and previous.program.program_sha256 not in current.predecessor_program_sha256
            )
            or not set(previous.predecessor_program_sha256)
            <= set(current.predecessor_program_sha256)
            or current.program.program_sha256 == previous.program.program_sha256
            or current.parent_policy_sha256 == previous.parent_policy_sha256
            or current.program.model_dump(exclude={"program_sha256"})
            != previous.program.model_dump(exclude={"program_sha256"})
            or current.model_dump(exclude=changed) != previous.model_dump(exclude=changed)
        ):
            raise ValueError("MODEL_PROGRAM_UPGRADE_BINDING_CHANGED")


class FixedProgramCutover(RouteModel):
    upgrade: FixedProgramUpgrade
    parent_policy: str = Field(min_length=1, max_length=1048576)
    expected_parent_etag: str = Field(pattern=r'^"[A-Za-z0-9+/=]{1,160}"$')
    operation_policies: dict[Identifier, str] = Field(
        default_factory=dict, max_length=30, exclude_if=lambda value: not value,
    )
    expected_operation_etags: dict[Identifier, str | None] = Field(
        default_factory=dict, max_length=30, exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def validate_operation_versions(self) -> FixedProgramCutover:
        if (set(self.operation_policies) != set(self.expected_operation_etags)
                or self.operation_policies and set(self.operation_policies)
                != set(self.upgrade.installation.operation_policies_sha256)
                or any(not policy or len(policy) > 524288
                       for policy in self.operation_policies.values())
                or any(
                    name != "microsoft-decision" or not self.upgrade.installation.decision_enabled
                    if value is None else re.fullmatch(r'"[A-Za-z0-9+/=]{1,160}"', value) is None
                    for name, value in self.expected_operation_etags.items()
                )):
            raise ValueError("PROGRAM_CUTOVER_OPERATION_VERSIONS_CHANGED")
        return self


class ProgramCapabilityProof(RouteModel):
    proof_id: CanonicalUuid
    identity: ProgramIdentity
    supported_profiles: tuple[RouteProfile, ...] = Field(min_length=1)
    issued_epoch: EpochSeconds
    expires_epoch: EpochSeconds

    @model_validator(mode="after")
    def validate_window(self) -> ProgramCapabilityProof:
        if not 0 < self.expires_epoch - self.issued_epoch <= 120 or len(
            set(self.supported_profiles)
        ) != len(self.supported_profiles):
            raise ValueError("ROUTE_PROGRAM_PROOF_INVALID")
        return self


class RouteMember(RouteModel):
    scope: RouteScope
    route_version: CanonicalUuid
    alias: Alias
    model_id: CanonicalUuid
    content_sha256: Digest
    connection_approval: ConnectionApproval
    approval_sha256: Digest

    @model_validator(mode="after")
    def validate_approval(self) -> RouteMember:
        if (
            self.connection_approval.connection.scope != self.scope
            or document_digest(self.connection_approval) != self.approval_sha256
        ):
            raise ValueError("ROUTE_MEMBER_APPROVAL_MISMATCH")
        return self


class RouteApprovalRenewal(RouteModel):
    version: Literal[1] = 1
    scope: RouteScope
    content_sha256: Digest
    original_approval_sha256: Digest
    required_program_sha256: Digest
    budget_epoch: CanonicalUuid
    approval: ConnectionApproval

    @property
    def row_key(self) -> str:
        return "approval:" + text_digest(self.content_sha256 + self.original_approval_sha256)

    def require_current(
        self, route: RouteContent, member: RouteMember, program: ProgramIdentity, now: int,
    ) -> None:
        if (
            self.scope != route.scope or program.scope != route.scope
            or self.content_sha256 != route.sha256 or member.content_sha256 != route.sha256
            or self.original_approval_sha256 != member.approval_sha256
            or self.budget_epoch != route.budget_epoch
            or self.approval.issued_epoch < member.connection_approval.issued_epoch
            or self.approval.expires_epoch <= member.connection_approval.expires_epoch
        ):
            raise ValueError("ROUTE_APPROVAL_RENEWAL_BINDING_CHANGED")
        self.approval.require_current(route.connection, now)


class ConfigurationApproval(RouteModel):
    approval_id: CanonicalUuid
    scope: RouteScope
    route_version: CanonicalUuid
    generation: Generation
    manifest_sha256: Digest
    dependency_manifest_sha256: Digest
    catalog_sha256: Digest
    entry_count: int = Field(strict=True, ge=0, le=10000)
    required_program_sha256: Digest
    required_profiles: tuple[RouteProfile, ...]
    budget_epoch: CanonicalUuid
    issued_epoch: EpochSeconds
    activate_before_epoch: EpochSeconds

    @model_validator(mode="after")
    def validate_window(self) -> ConfigurationApproval:
        if not 0 < self.activate_before_epoch - self.issued_epoch <= 600 or len(
            set(self.required_profiles)
        ) != len(self.required_profiles):
            raise ValueError("ROUTE_CONFIGURATION_APPROVAL_INVALID")
        return self

    def require_same_configuration(self, previous: ConfigurationApproval) -> None:
        metadata = {"approval_id", "issued_epoch", "activate_before_epoch"}
        if (
            self.model_dump(exclude=metadata) != previous.model_dump(exclude=metadata)
            or self.issued_epoch < previous.issued_epoch
        ):
            raise ValueError("ROUTE_CONFIGURATION_APPROVAL_CHANGED")


class ActiveRouteHead(RouteModel):
    state: Literal["active"] = "active"
    approval: ConfigurationApproval
    activated_epoch: EpochSeconds

    @model_validator(mode="after")
    def validate_activation(self) -> ActiveRouteHead:
        if (
            not self.approval.issued_epoch
            <= self.activated_epoch
            < self.approval.activate_before_epoch
        ):
            raise ValueError("ROUTE_ACTIVATION_TIME_INVALID")
        if len(canonical_document(self).encode()) > MAX_HEAD_BYTES:
            raise ValueError("ROUTE_HEAD_TOO_LARGE")
        return self

    def require_ready(
        self,
        identity: ProgramIdentity,
        proof: ProgramCapabilityProof,
        budget_epoch: str,
        now: int,
        *,
        activating: bool = False,
        legacy_program_sha256: str | None = None,
        predecessor_program_sha256: tuple[str, ...] = (),
    ) -> None:
        checked = parse_document(ActiveRouteHead, canonical_document(self), MAX_HEAD_BYTES)
        proof = parse_document(ProgramCapabilityProof, canonical_document(proof), MAX_MEMBER_BYTES)
        identity = parse_document(ProgramIdentity, canonical_document(identity), MAX_MEMBER_BYTES)
        approval = checked.approval
        if (
            type(now) is not int
            or proof.identity != identity
            or approval.scope != identity.scope
            or not proof.issued_epoch <= now < proof.expires_epoch
            or approval.required_program_sha256 != identity.program_sha256
              and (activating or approval.required_program_sha256 != legacy_program_sha256
                  and approval.required_program_sha256 not in predecessor_program_sha256)
            or approval.budget_epoch != budget_epoch
            or not set(approval.required_profiles) <= set(proof.supported_profiles)
            or checked.activated_epoch > now
            or (activating and checked.activated_epoch != now)
        ):
            raise ValueError("ROUTE_PROGRAM_CONFIGURATION_BINDING_INVALID")


def configuration_manifests(
    scope: RouteScope,
    version: str,
    routes: Sequence[RouteContent],
    approvals: Mapping[str, ConnectionApproval],
    now: int,
) -> tuple[tuple[RouteMember, ...], str, str]:
    canonical_uuid(version)
    members, manifest, dependencies, aliases = [], {}, {}, set()
    for candidate in routes:
        route = parse_document(RouteContent, canonical_document(candidate), MAX_ROUTE_BYTES)
        if route.scope != scope or route.alias.casefold() in aliases:
            raise ValueError("ROUTE_SCOPE_OR_DUPLICATE_ALIAS")
        aliases.add(route.alias.casefold())
        approval = approvals.get(route.connection.revision_id)
        if approval is None:
            raise ValueError("ROUTE_INDEPENDENT_CONNECTION_APPROVAL_REQUIRED")
        approval = parse_document(
            ConnectionApproval, canonical_document(approval), MAX_MEMBER_BYTES
        )
        approval.require_current(route.connection, now)
        member = RouteMember(
            scope=scope,
            route_version=version,
            alias=route.alias,
            model_id=route.model_id,
            content_sha256=route.sha256,
            connection_approval=approval,
            approval_sha256=document_digest(approval),
        )
        members.append(member)
        manifest[route.alias] = {
            "content_sha256": route.sha256,
            "approval_sha256": member.approval_sha256,
        }
        dependencies[route.alias] = document_digest(route.connection)
    return (
        tuple(sorted(members, key=lambda item: item.alias)),
        document_digest(manifest),
        document_digest(dependencies),
    )


def changed_route_aliases(
    before: Sequence[RouteContent], after: Sequence[RouteContent]
) -> tuple[str, ...]:
    def index(routes: Sequence[RouteContent]) -> dict[str, str]:
        result, normalized = {}, set()
        for candidate in routes:
            route = parse_document(RouteContent, canonical_document(candidate), MAX_ROUTE_BYTES)
            if route.alias.casefold() in normalized:
                raise ValueError("ROUTE_DUPLICATE_ALIAS")
            normalized.add(route.alias.casefold())
            result[route.alias] = document_digest(
                route.model_dump(mode="json", exclude={"display_name", "created_at"})
            )
        return result

    previous, current = index(before), index(after)
    return tuple(
        sorted(alias for alias, digest in current.items() if previous.get(alias) != digest)
    )


def catalog_entry(route: RouteContent) -> dict[str, Any]:
    return {
        "type": "model",
        "id": route.alias,
        "display_name": route.display_name,
        "created_at": route.model_dump(mode="json")["created_at"],
        "logical_model_id": route.model_id,
    }


def catalog_digest(routes: Sequence[RouteContent]) -> str:
    return document_digest({route.alias: catalog_entry(route) for route in routes})


class RouteSnapshot(RouteModel):
    approval: ConfigurationApproval
    routes: tuple[RouteContent, ...] = Field(max_length=10000)
    members: tuple[RouteMember, ...] = Field(max_length=10000)

    @property
    def configuration_sha256(self) -> str:
        return document_digest({
            "approval": self.approval.model_dump(
                mode="json", exclude={"approval_id", "issued_epoch", "activate_before_epoch"},
            ),
            "routes": [route.model_dump(mode="json") for route in self.routes],
            "members": [member.model_dump(mode="json") for member in self.members],
        })

    @classmethod
    def prepare(
        cls,
        approval: ConfigurationApproval,
        routes: Sequence[RouteContent],
        connections: Mapping[str, ConnectionApproval],
        checked_at: int,
    ) -> RouteSnapshot:
        approval = parse_document(
            ConfigurationApproval, canonical_document(approval), MAX_HEAD_BYTES
        )
        validated = tuple(
            parse_document(RouteContent, canonical_document(route), MAX_ROUTE_BYTES)
            for route in routes
        )
        members, manifest, dependencies = configuration_manifests(
            approval.scope,
            approval.route_version,
            validated,
            connections,
            checked_at,
        )
        if (
            type(checked_at) is not int
            or not approval.issued_epoch <= checked_at < approval.activate_before_epoch
            or approval.entry_count != len(validated)
            or approval.manifest_sha256 != manifest
            or approval.dependency_manifest_sha256 != dependencies
            or approval.catalog_sha256 != catalog_digest(validated)
            or set(approval.required_profiles) != {route.profile for route in validated}
            or any(route.budget_epoch != approval.budget_epoch for route in validated)
        ):
            raise ValueError("ROUTE_SNAPSHOT_APPROVAL_MISMATCH")
        result = cls(approval=approval, routes=validated, members=members)
        result.require_valid(checked_at)
        return result

    def require_valid(self, checked_at: int) -> None:
        approval = parse_document(
            ConfigurationApproval, canonical_document(self.approval), MAX_HEAD_BYTES
        )
        if len(self.routes) != len(self.members) or len(self.routes) != approval.entry_count:
            raise ValueError("ROUTE_SNAPSHOT_INCOMPLETE")
        connections: dict[str, ConnectionApproval] = {}
        for candidate in self.members:
            member = parse_document(RouteMember, canonical_document(candidate), MAX_MEMBER_BYTES)
            revision = member.connection_approval.connection.revision_id
            if revision in connections and connections[revision] != member.connection_approval:
                raise ValueError("ROUTE_CONNECTION_APPROVAL_CONFLICT")
            connections[revision] = member.connection_approval
        members, manifest, dependencies = configuration_manifests(
            approval.scope,
            approval.route_version,
            self.routes,
            connections,
            checked_at,
        )
        if (
            self.members != members
            or manifest != approval.manifest_sha256
            or dependencies != approval.dependency_manifest_sha256
            or approval.catalog_sha256 != catalog_digest(self.routes)
            or any(route.budget_epoch != approval.budget_epoch for route in self.routes)
            or set(approval.required_profiles) != {route.profile for route in self.routes}
        ):
            raise ValueError("ROUTE_SNAPSHOT_APPROVAL_MISMATCH")


class ManagedProgramInitialization(RouteModel):
    version: Literal[1] = 1
    previous_publication_id: UUID
    previous_spec_sha256: Digest
    previous_revision: Identifier
    previous_policy_sha256: Digest
    installation: FixedProgramInstallation
    snapshot: RouteSnapshot
    head: ActiveRouteHead

    def require_previous(self, previous: GatewayPublication) -> None:
        installed = self.installation
        if (
            previous.id != self.previous_publication_id
            or previous.status.value != "active"
            or previous.desired_spec_sha256 != self.previous_spec_sha256
            or previous.apim_revision != self.previous_revision
            or previous.policy_sha256 != self.previous_policy_sha256
            or str(previous.gateway_profile_id) != installed.program.scope.target_id
            or installed.program.scope.gateway_id != "managed"
            or installed.program.api_revision == self.previous_revision
            or not installed.model_additions_enabled or installed.legacy_program_sha256 is not None
            or any(name in previous.resource_manifest for name in (
                "fixed_route_program", "route_configuration", "route_activation",
            ))
            or self.snapshot.routes or self.snapshot.members
            or self.snapshot.approval.entry_count != 0
            or self.snapshot.approval.generation != 1
            or self.head.approval != self.snapshot.approval
            or self.head.approval.scope != installed.program.scope
            or self.head.approval.required_program_sha256 != installed.program.program_sha256
        ):
            raise ValueError("Managed program initialization binding changed")
        self.snapshot.require_valid(self.head.activated_epoch)


class ModelRouteHead(RouteModel):
    version: Literal[1] = 1
    publication_id: CanonicalUuid
    alias: Alias
    model_id: CanonicalUuid
    display_name: str = Field(min_length=1, max_length=255)
    created_at: AwareDatetime
    content_sha256: Digest
    head: ActiveRouteHead

    @property
    def row_key(self) -> str:
        return "model:" + member_row(self.alias)

    @model_validator(mode="after")
    def validate_model_head(self) -> ModelRouteHead:
        expected_catalog = {
            "type": "model", "id": self.alias, "display_name": self.display_name,
            "created_at": self.model_dump(mode="json")["created_at"],
            "logical_model_id": self.model_id,
        }
        if (self.head.approval.entry_count != 1
                or self.head.approval.catalog_sha256 != document_digest({
                    self.alias: expected_catalog,
                })):
            raise ValueError("MODEL_ROUTE_HEAD_CATALOG_CHANGED")
        if len(canonical_document(self).encode()) > MAX_HEAD_BYTES:
            raise ValueError("MODEL_ROUTE_HEAD_TOO_LARGE")
        return self


class ModelRouteAddition(RouteModel):
    version: Literal[1] = 1
    kind: Literal["model_addition"] = "model_addition"
    publication_id: CanonicalUuid
    program: ProgramIdentity
    snapshot: RouteSnapshot

    @model_validator(mode="after")
    def validate_addition(self) -> ModelRouteAddition:
        snapshot = self.snapshot
        if (len(snapshot.routes) != 1 or len(snapshot.members) != 1
                or snapshot.approval.entry_count != 1 or snapshot.approval.generation != 1
                or snapshot.approval.scope != self.program.scope
                or snapshot.approval.required_program_sha256 != self.program.program_sha256):
            raise ValueError("MODEL_ADDITION_REQUIRES_SINGLE_MODEL")
        snapshot.require_valid(snapshot.approval.issued_epoch)
        return self

    @classmethod
    def prepare(
        cls, publication_id: str, program: ProgramIdentity, proof: ProgramCapabilityProof,
        route: RouteContent, connection: ConnectionApproval, budget_epoch: str, now: int,
    ) -> ModelRouteAddition:
        publication_id = canonical_uuid(publication_id)
        route = parse_document(RouteContent, canonical_document(route), MAX_ROUTE_BYTES)
        connection = parse_document(
            ConnectionApproval, canonical_document(connection), MAX_MEMBER_BYTES,
        )
        if route.scope != program.scope or route.budget_epoch != budget_epoch:
            raise ValueError("MODEL_ADDITION_PROGRAM_OR_BUDGET_CHANGED")
        connection.require_current(route.connection, now)
        version = str(uuid5(UUID(publication_id), "model-route-version"))
        connections = {connection.connection.revision_id: connection}
        _, manifest, dependencies = configuration_manifests(
            program.scope, version, (route,), connections, now,
        )
        approval = ConfigurationApproval(
            approval_id=str(uuid5(UUID(publication_id), "model-route-approval")),
            scope=program.scope, route_version=version, generation=1,
            manifest_sha256=manifest, dependency_manifest_sha256=dependencies,
            catalog_sha256=catalog_digest((route,)), entry_count=1,
            required_program_sha256=program.program_sha256,
            required_profiles=(route.profile,), budget_epoch=budget_epoch,
            issued_epoch=now, activate_before_epoch=now + 600,
        )
        addition = cls(
            publication_id=publication_id, program=program,
            snapshot=RouteSnapshot.prepare(approval, (route,), connections, now),
        )
        addition.active_head(proof, budget_epoch, now)
        return addition

    def active_head(
        self, proof: ProgramCapabilityProof, budget_epoch: str, now: int,
    ) -> ModelRouteHead:
        self.snapshot.require_valid(now)
        head = ActiveRouteHead(approval=self.snapshot.approval, activated_epoch=now)
        head.require_ready(self.program, proof, budget_epoch, now, activating=True)
        route = self.snapshot.routes[0]
        return ModelRouteHead(
            publication_id=self.publication_id, alias=route.alias, model_id=route.model_id,
            display_name=route.display_name, created_at=route.created_at,
            content_sha256=route.sha256, head=head,
        )


class RouteCandidateProbe(RouteModel):
    probe_id: CanonicalUuid
    publication_id: CanonicalUuid
    request_id: CanonicalUuid
    subscription_id: Identifier
    alias: Alias
    operation: BudgetOperation | Literal["anthropic-count-tokens"]
    body_sha256: Digest
    allow_dispatch: bool = True
    member_backend_id: Identifier | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    head: ActiveRouteHead
    issued_epoch: EpochSeconds
    expires_epoch: EpochSeconds

    @model_validator(mode="after")
    def validate_window(self) -> RouteCandidateProbe:
        if (
            not 0 < self.expires_epoch - self.issued_epoch <= 120
            or self.issued_epoch < self.head.activated_epoch
            or self.expires_epoch > self.head.approval.activate_before_epoch
        ):
            raise ValueError("ROUTE_CANDIDATE_WINDOW_INVALID")
        return self


class RoutePendingActivation(RouteModel):
    operation_id: CanonicalUuid
    base_generation: int = Field(strict=True, ge=0, le=2**53 - 1)
    approval: ConfigurationApproval
    snapshot_sha256: Digest

    @model_validator(mode="after")
    def validate_generation(self) -> RoutePendingActivation:
        if self.approval.generation != self.base_generation + 1:
            raise ValueError("ROUTE_PENDING_GENERATION_INVALID")
        return self


class RouteWriterState(RouteModel):
    scope: RouteScope
    owner_token: CanonicalUuid
    publication_id: CanonicalUuid
    writer_epoch: Generation
    leased_epoch: EpochSeconds
    lease_until_epoch: EpochSeconds
    active_generation: int = Field(strict=True, ge=0, le=2**53 - 1)
    active_version: CanonicalUuid | None
    active_sha256: Digest | None
    pending: RoutePendingActivation | None = None

    @model_validator(mode="after")
    def validate_state(self) -> RouteWriterState:
        if (
            not 0 < self.lease_until_epoch - self.leased_epoch <= 30
            or (self.active_generation == 0)
            != (self.active_version is None and self.active_sha256 is None)
            or (self.active_version is None) != (self.active_sha256 is None)
            or self.pending is not None and (
                self.pending.approval.scope != self.scope
                or self.pending.base_generation != self.active_generation
            )
            or len(canonical_document(self).encode()) > MAX_HEAD_BYTES
        ):
            raise ValueError("ROUTE_WRITER_STATE_INVALID")
        return self

    def require_owner(self, owner_token: str, publication_id: str, now: int) -> None:
        checked = parse_document(RouteWriterState, canonical_document(self), MAX_HEAD_BYTES)
        if (
            checked.owner_token != owner_token or checked.publication_id != publication_id
            or type(now) is not int or not checked.leased_epoch <= now < checked.lease_until_epoch
        ):
            raise ValueError("ROUTE_WRITER_OWNER_OR_LEASE_CHANGED")

    def takeover(self, owner_token: str, publication_id: str, now: int) -> RouteWriterState:
        checked = parse_document(RouteWriterState, canonical_document(self), MAX_HEAD_BYTES)
        if type(now) is not int or now < checked.lease_until_epoch:
            raise ValueError("ROUTE_WRITER_LEASE_NOT_EXPIRED")
        if owner_token == checked.owner_token:
            raise ValueError("ROUTE_WRITER_OWNER_TOKEN_REUSED")
        return RouteWriterState.model_validate({
            **checked.model_dump(mode="python"), "owner_token": owner_token,
            "publication_id": publication_id, "writer_epoch": checked.writer_epoch + 1,
            "leased_epoch": now, "lease_until_epoch": now + 30,
        })

    def renew(self, owner_token: str, publication_id: str, now: int) -> RouteWriterState:
        self.require_owner(owner_token, publication_id, now)
        return RouteWriterState.model_validate({
            **self.model_dump(mode="python"), "leased_epoch": now, "lease_until_epoch": now + 30,
        })

    def prepare(
        self, pending: RoutePendingActivation, owner_token: str, publication_id: str, now: int,
    ) -> RouteWriterState:
        self.require_owner(owner_token, publication_id, now)
        if self.pending is not None:
            raise ValueError("ROUTE_WRITER_PENDING_RECOVER_ONLY")
        return RouteWriterState.model_validate({
            **self.model_dump(mode="python"), "pending": pending,
        })


class RouteWriterReceipt(RouteModel):
    operation_id: CanonicalUuid
    scope: RouteScope
    kind: Literal["claim", "takeover", "renew", "prepare", "abort"]
    before_etag: str | None
    before_sha256: Digest | None
    after_sha256: Digest
    owner_token: CanonicalUuid
    writer_epoch: Generation


class RouteTransitionReceipt(RouteModel):
    operation_id: CanonicalUuid
    scope: RouteScope
    before_etag: str | None
    before_generation: int = Field(strict=True, ge=0, le=2**53 - 1)
    after_sha256: Digest
    route_version: CanonicalUuid
    approval_id: CanonicalUuid


class RouteConfigurationPlan(RouteModel):
    kind: Literal["configuration"] = "configuration"
    policy_write_count: Literal[0] = 0
    program: ProgramIdentity
    snapshot: RouteSnapshot
    changed_aliases: tuple[Alias, ...]
    removed_aliases: tuple[Alias, ...]
    previous_semantics_sha256: Digest
    planned_epoch: EpochSeconds

    @classmethod
    def prepare(
        cls,
        previous: Sequence[RouteContent],
        snapshot: RouteSnapshot,
        program: ProgramIdentity,
        proof: ProgramCapabilityProof,
        active_budget_epoch: str,
        now: int,
    ) -> RouteConfigurationPlan:
        snapshot.require_valid(now)
        head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=now)
        head.require_ready(program, proof, active_budget_epoch, now, activating=True)
        before = tuple(
            parse_document(RouteContent, canonical_document(route), MAX_ROUTE_BYTES)
            for route in previous
        )
        if any(route.scope != program.scope for route in before):
            raise ValueError("ROUTE_PREVIOUS_SCOPE_MISMATCH")
        changed = changed_route_aliases(before, snapshot.routes)
        current = {route.alias for route in snapshot.routes}
        return cls(
            program=program,
            snapshot=snapshot,
            changed_aliases=changed,
            removed_aliases=tuple(
                sorted(route.alias for route in before if route.alias not in current)
            ),
            previous_semantics_sha256=document_digest(
                {
                    route.alias: route.model_dump(
                        mode="json", exclude={"display_name", "created_at"}
                    )
                    for route in before
                }
            ),
            planned_epoch=now,
        )


class RoutePublicationTask(RouteModel):
    version: Literal[1] = 1
    model_addition: bool = Field(default=False, exclude_if=lambda value: value is False)
    configuration_only: bool = Field(default=False, exclude_if=lambda value: value is False)
    publication_id: CanonicalUuid
    base_publication_id: CanonicalUuid | None
    desired_spec_sha256: Digest
    target_identity_sha256: Digest
    storage_endpoint: str
    storage_table: str
    plan: RouteConfigurationPlan
    serving_programs: tuple[ProgramIdentity, ...] = Field(
        default=(), max_length=128, exclude_if=lambda value: not value,
    )
    serving_installation_sha256: dict[str, Digest] = Field(
        default_factory=dict, max_length=128, exclude_if=lambda value: not value,
    )
    additional_scope_tasks: tuple[RoutePublicationTask, ...] = Field(
        default=(), max_length=31, exclude_if=lambda value: not value,
    )
    previous_routes: tuple[RouteContent, ...] = Field(max_length=10000)
    previous_head: ActiveRouteHead | None
    previous_members: tuple[RouteMember, ...] = Field(default=(), max_length=10000)
    credential_bindings: dict[Digest, str] = Field(default_factory=dict, max_length=10000)
    program_migration_sha256: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    previous_program_sha256: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    known_unavailable_bindings: dict[str, Digest] = Field(
        default_factory=dict, max_length=10000, exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def validate_task(self) -> RoutePublicationTask:
        if self.configuration_only and self.program_migration_sha256 is not None:
            raise ValueError("CONFIGURATION_PUBLICATION_FORBIDS_PROGRAM_MIGRATION")
        if self.additional_scope_tasks:
            if (not self.model_addition or not self.configuration_only
                    or len({task.plan.program.scope for task in self.scope_tasks})
                    != len(self.scope_tasks)):
                raise ValueError("ROUTE_SCOPE_TASK_SET_INVALID")
            for task in self.additional_scope_tasks:
                if (task.additional_scope_tasks or not task.model_addition
                        or not task.configuration_only or any(
                            getattr(task, field) != getattr(self, field) for field in (
                                "publication_id", "base_publication_id", "desired_spec_sha256",
                                "target_identity_sha256", "storage_endpoint", "storage_table",
                                "credential_bindings",
                            )
                        ) or task.plan.program.model_dump(exclude={"scope", "instance_id"})
                        != self.plan.program.model_dump(exclude={"scope", "instance_id"})
                        or task.plan.program.scope.model_dump(exclude={"gateway_id"})
                        != self.plan.program.scope.model_dump(exclude={"gateway_id"})
                        or task.plan.snapshot.routes[0].model_dump(exclude={"scope", "connection"})
                        != self.plan.snapshot.routes[0].model_dump(exclude={"scope", "connection"})
                        or task.plan.snapshot.routes[0].connection.model_dump(exclude={"scope"})
                        != self.plan.snapshot.routes[0].connection.model_dump(exclude={"scope"})):
                    raise ValueError("ROUTE_SCOPE_TASK_BINDING_CHANGED")
        for secret_id in self.credential_bindings.values():
            RouteCredential(mode="key_vault_bearer", header="Authorization", secret_id=secret_id)
        plan = self.plan
        snapshot = plan.snapshot
        if self.serving_programs and (
            plan.program not in self.serving_programs
            or len({item.instance_id for item in self.serving_programs})
            != len(self.serving_programs)
            or any(
                item.model_dump(exclude={"instance_id"})
                != plan.program.model_dump(exclude={"instance_id"})
                for item in self.serving_programs
            )
        ):
            raise ValueError("ROUTE_SERVING_PROGRAM_SET_INVALID")
        if self.serving_installation_sha256 and set(self.serving_installation_sha256) != {
            identity.instance_id for identity in self.serving_programs
        }:
            raise ValueError("ROUTE_SERVING_INSTALLATION_SET_INVALID")
        if self.model_addition:
            if self.serving_programs and not self.serving_installation_sha256:
                raise ValueError("MODEL_ADDITION_SERVING_INSTALLATIONS_REQUIRED")
            ModelRouteAddition(
                publication_id=self.publication_id, program=plan.program, snapshot=snapshot,
            )
            if (self.base_publication_id is None or self.previous_head is not None
                    or self.previous_routes or self.previous_members
                    or self.program_migration_sha256 is not None
                    or self.known_unavailable_bindings):
                raise ValueError("MODEL_ADDITION_CANNOT_INCLUDE_PREVIOUS_MODELS")
        if self.known_unavailable_bindings and (
            self.program_migration_sha256 is None
            or not self.known_unavailable_bindings.keys() <= {
                route.alias for route in snapshot.routes
            }
        ):
            raise ValueError("ROUTE_UPSTREAM_EXCLUSION_REQUIRES_BOUND_MIGRATION")
        snapshot.require_valid(plan.planned_epoch)
        ActiveRouteHead(approval=snapshot.approval, activated_epoch=plan.planned_epoch)
        previous = self.previous_head
        if self.previous_program_sha256 is not None and (
            previous is None or self.model_addition or self.program_migration_sha256 is not None
            or previous.approval.required_program_sha256 != self.previous_program_sha256
            or self.previous_program_sha256 == plan.program.program_sha256
        ):
            raise ValueError("ROUTE_PREVIOUS_PROGRAM_BINDING_INVALID")
        if previous is None:
            if self.previous_members:
                raise ValueError("ROUTE_PUBLICATION_BASELINE_INVALID")
        else:
            try:
                RouteSnapshot(
                    approval=previous.approval, routes=self.previous_routes,
                    members=self.previous_members,
                ).require_valid(previous.activated_epoch)
            except ValueError as error:
                raise ValueError("ROUTE_PUBLICATION_BASELINE_INVALID") from error
        if (
            plan.program.scope != snapshot.approval.scope
            or plan.program.program_sha256 != snapshot.approval.required_program_sha256
            or type(plan.policy_write_count) is not int or plan.policy_write_count != 0
            or any(route.scope != plan.program.scope for route in self.previous_routes)
            or plan.changed_aliases != changed_route_aliases(self.previous_routes, snapshot.routes)
            or plan.removed_aliases != tuple(sorted(
                route.alias for route in self.previous_routes
                if route.alias not in {candidate.alias for candidate in snapshot.routes}
            ))
            or plan.previous_semantics_sha256 != document_digest({
                route.alias: route.model_dump(mode="json", exclude={"display_name", "created_at"})
                for route in self.previous_routes
            })
            or (previous is None and (
                self.previous_routes or snapshot.approval.generation != 1
                or (self.base_publication_id is not None)
                != (self.program_migration_sha256 is not None or self.model_addition)
            ))
            or previous is not None and (
                self.base_publication_id is None or previous.approval.scope != plan.program.scope
                or self.program_migration_sha256 is not None
                or snapshot.approval.generation != previous.approval.generation + 1
                or previous.approval.entry_count != len(self.previous_routes)
                or previous.approval.catalog_sha256 != catalog_digest(self.previous_routes)
                or previous.approval.required_program_sha256
                != (self.previous_program_sha256 or plan.program.program_sha256)
            )
        ):
            raise ValueError("ROUTE_PUBLICATION_TASK_INVALID")
        address = urlsplit(self.storage_endpoint)
        if (
            address.scheme != "https" or address.username or address.password
            or address.query or address.fragment or address.port is not None or address.path
            or not re.fullmatch(
                r"[a-z0-9]{3,24}\.table\.core\.windows\.net", address.hostname or "",
            )
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{2,62}", self.storage_table)
            or self.storage_table.casefold() == "turnstileledger"
        ):
            raise ValueError("ROUTE_PUBLICATION_STORAGE_INVALID")
        return self

    def addition(self) -> ModelRouteAddition:
        if not self.model_addition:
            raise ValueError("MODEL_ADDITION_TASK_REQUIRED")
        return ModelRouteAddition(
            publication_id=self.publication_id, program=self.plan.program,
            snapshot=self.plan.snapshot,
        )

    @property
    def context_sha256(self) -> str:
        return document_digest(self)

    @property
    def scope_tasks(self) -> tuple[RoutePublicationTask, ...]:
        return (self, *self.additional_scope_tasks)

    def require_publication(
        self, publication_id: str, base_publication_id: str | None,
        desired_spec_sha256: str, target_id: str,
    ) -> None:
        if (
            self.publication_id != publication_id or self.base_publication_id != base_publication_id
            or self.desired_spec_sha256 != desired_spec_sha256
            or self.plan.program.scope.target_id != target_id
        ):
            raise ValueError("ROUTE_PUBLICATION_CHANGED")


def content_partition(scope: RouteScope) -> str:
    return scope.partition + ":content"


def member_partition(scope: RouteScope, version: str) -> str:
    return scope.partition + ":" + canonical_uuid(version)


def member_row(alias: str) -> str:
    return text_digest(alias.casefold())


def catalog_partition(scope: RouteScope, version: str) -> str:
    return member_partition(scope, version) + ":catalog"
