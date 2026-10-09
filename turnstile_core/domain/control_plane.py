from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, cast
from uuid import UUID, uuid5

from pydantic import (
    Field,
    HttpUrl,
    SecretStr,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from .image_profiles import ImageGenerationLimits, ImageGenerationProfile, validate_image_profile
from .models import StrictModel
from .runtime_models import (
    BrandKey,
    ModelCapability,
    ModelFamilyKey,
    ModelVendorKey,
    OAuthClientCredentialsConfig,
    PriceSource,
    ProviderKind,
    ProviderTarget,
    RuntimeKind,
    default_capabilities,
)


class ApiFormat(StrEnum):
    OPENAI_CHAT = "openai_chat"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    OPENAI_IMAGES = "openai_images"


class AuthStrategy(StrEnum):
    NONE = "none"
    MANAGED_IDENTITY = "managed_identity"
    NAMED_VALUE_BEARER = "named_value_bearer"
    NAMED_VALUE_API_KEY = "named_value_api_key"
    OAUTH_CLIENT_CREDENTIALS = "oauth_client_credentials"


class StreamingMode(StrEnum):
    NATIVE = "native"
    BUFFERED = "buffered"


class PublicationStatus(StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    PROVISIONING = "provisioning"
    BUILDING_REVISION = "building_revision"
    VERIFYING = "verifying"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    PROMOTING = "promoting"
    ACTIVE = "active"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class PublicationKind(StrEnum):
    MODEL_ADD = "model_add"
    MODEL_REMOVE = "model_remove"
    CREDENTIAL_ROTATION = "credential_rotation"
    ROUTE_RECONCILE = "route_reconcile"


class GatewayReleaseRole(StrEnum):
    CURRENT = "current"
    IMMEDIATE_ROLLBACK = "immediate_rollback"
    PINNED = "pinned"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    EXPIRED = "expired"
    IN_PROGRESS = "in_progress"
    ROLLED_BACK = "rolled_back"


class GatewayReleaseOperationKind(StrEnum):
    ROLLBACK = "rollback"
    INTEGRITY_CHECK = "integrity_check"
    GC_PLAN = "gc_plan"
    APPLICATION_SYNC = "application_sync"
    APPLICATION_PROVISION = "application_provision"


class GatewayReleaseOperationStatus(StrEnum):
    QUEUED = "queued"
    VALIDATING_DEPENDENCIES = "validating_dependencies"
    PREFLIGHT_PROBING = "preflight_probing"
    PROMOTING = "promoting"
    VERIFYING_READBACK = "verifying_readback"
    POST_PROMOTION_PROBING = "post_promotion_probing"
    RESTORING = "restoring"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RESTORED = "restored"


class RuntimeTarget(StrictModel):
    existing_id: UUID | None = None
    bedrock_runtime_url: HttpUrl | None = None
    foundry_project_endpoint: HttpUrl | None = None
    foundry_inference_endpoint: HttpUrl | None = None
    openai_base_url: HttpUrl | None = None
    model_vendor: ModelVendorKey | None = None
    api_key: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
    )
    oauth_client_secret: SecretStr | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def require_existing_or_new_runtime(self) -> RuntimeTarget:
        if self.oauth_client_secret is not None and (
            self.existing_id is None or self.api_key is not None
        ):
            raise ValueError(
                "OAuth credentials require an existing connection and cannot use API key"
            )
        if self.existing_id is not None and (
            self.bedrock_runtime_url is not None
            or self.foundry_project_endpoint is not None
            or self.foundry_inference_endpoint is not None
            or self.openai_base_url is not None
            or self.model_vendor is not None
        ):
            raise ValueError("an existing runtime cannot redefine its connection")
        if self.existing_id is not None:
            return self
        has_bedrock = self.bedrock_runtime_url is not None
        has_foundry = self.foundry_project_endpoint is not None
        has_openai = self.openai_base_url is not None
        if sum((has_bedrock, has_foundry, has_openai)) > 1:
            raise ValueError("a new runtime must use exactly one provider connection")
        if self.foundry_inference_endpoint is not None and not has_foundry:
            raise ValueError(
                "a Foundry inference endpoint requires a Foundry project endpoint"
            )
        if has_bedrock and (
            self.bedrock_runtime_url is None or self.api_key is None
        ):
            raise ValueError("a new Bedrock runtime requires its Runtime URL and API key")
        if has_openai and self.api_key is None:
            raise ValueError("a new OpenAI-compatible runtime requires its Base URL and API key")
        if self.model_vendor is not None and not has_openai:
            raise ValueError("model_vendor belongs only to a new OpenAI-compatible runtime")
        if not has_bedrock and not has_foundry and not has_openai:
            raise ValueError("a new runtime requires a supported provider connection")
        if has_foundry and (self.foundry_inference_endpoint is None) != (
            self.api_key is None
        ):
            raise ValueError(
                "Foundry API-key authentication requires inference endpoint and API key"
            )
        return self


class ModelTarget(StrictModel):
    model_key: str = Field(min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9._:-]+$")
    display_name: str = Field(min_length=1, max_length=255)
    upstream_model_id: str = Field(min_length=1, max_length=500)
    family_key: ModelFamilyKey = ModelFamilyKey.GENERIC
    capabilities: list[ModelCapability] = Field(default_factory=default_capabilities)
    context_window: int | None = Field(default=None, ge=1)
    input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)
    cached_cost_per_million: float | None = Field(default=None, ge=0)
    cache_write_cost_per_million: float | None = Field(default=None, ge=0)
    allowed_roles: list[str] = Field(default_factory=lambda: ["owner", "admin", "member"])
    assignment_required: bool = True
    image_profile: ImageGenerationProfile | None = None
    price_configuration: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def serialize_pricing(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        values = handler(self)
        if self.price_configuration is None:
            values.pop("price_configuration", None)
        return cast(dict[str, Any], values)

    @model_validator(mode="after")
    def validate_image_contract(self) -> ModelTarget:
        if self.image_profile is not None:
            if self.capabilities != ["image_generation"]:
                raise ValueError("An image profile belongs only to an image model")
            validate_image_profile(self.image_profile)
        return self


class ModelCreateTarget(StrictModel):
    operation: Literal["chat", "image_generation"] = "chat"
    image_configuration: ImageGenerationLimits | None = None
    deployment_name: str | None = Field(default=None, min_length=1, max_length=500)
    model_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[a-zA-Z0-9._:-]+$",
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    upstream_model_id: str | None = Field(default=None, min_length=1, max_length=500)
    context_window: int | None = Field(default=None, ge=1)
    input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)
    cached_cost_per_million: float | None = Field(default=None, ge=0)
    cache_write_cost_per_million: float | None = Field(default=None, ge=0)
    price_source: PriceSource | None = None
    price_reference: str | None = Field(default=None, max_length=1500)
    price_discount_percent: float | None = Field(default=None, gt=0, le=100)
    price_entry_digest: str | None = Field(default=None, max_length=64)
    allow_unpriced: bool = False

    @model_validator(mode="after")
    def require_deployment_or_explicit_identity(self) -> ModelCreateTarget:
        if self.operation == "image_generation":
            if self.deployment_name is None:
                raise ValueError("Image generation requires an existing Foundry deployment")
            if self.context_window is not None or self.cache_write_cost_per_million is not None:
                raise ValueError("Image generation has no text context window or cache-write rate")
        elif self.image_configuration is not None:
            raise ValueError("An image profile cannot be attached to a text model")
        explicit = (self.model_key, self.display_name, self.upstream_model_id)
        if self.deployment_name is not None:
            if any(value is not None for value in explicit):
                raise ValueError(
                    "a deployment-derived model cannot supply registry identity fields"
                )
            return self
        if any(value is None for value in explicit):
            raise ValueError(
                "an explicit model requires model_key, display_name and upstream_model_id"
            )
        return self


class GatewayPublicationCreate(StrictModel):
    gateway_profile_id: UUID
    provider: ProviderTarget
    runtime: RuntimeTarget
    model: ModelCreateTarget


class GatewayPublicationRetry(StrictModel):
    api_key: SecretStr | None = None
    oauth_client_secret: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    authorize_image_probes: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def require_one_credential_type(self) -> GatewayPublicationRetry:
        if self.api_key is not None and self.oauth_client_secret is not None:
            raise ValueError("Select only one credential type")
        return self


class ImageProbeAuthorization(StrictModel):
    id: UUID
    attempt_limit: int = Field(default=1, ge=1, le=32)


class GatewayReconcileRequest(StrictModel):
    image_configurations: dict[UUID, ImageGenerationLimits] = Field(default_factory=dict)


class GatewayCredentialRotation(StrictModel):
    model_key: str = Field(min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9._:-]+$")
    api_key: SecretStr | None = None
    oauth_client_secret: SecretStr | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def require_one_credential_type(self) -> GatewayCredentialRotation:
        if (self.api_key is None) == (self.oauth_client_secret is None):
            raise ValueError("Provide exactly one replacement credential")
        return self


class GatewayBackendPoolMemberWrite(StrictModel):
    runtime_id: UUID
    priority: int = Field(ge=0, le=100)
    weight: int = Field(ge=0, le=100)


class GatewayBackendFailureStatusCodeRange(StrictModel):
    minimum: int = Field(ge=200, le=599)
    maximum: int = Field(ge=200, le=599)

    @model_validator(mode="after")
    def validate_order(self) -> GatewayBackendFailureStatusCodeRange:
        if self.minimum > self.maximum:
            raise ValueError("failure status code minimum cannot exceed maximum")
        return self


class GatewayRateLimitCircuitBreaker(StrictModel):
    failure_count: Literal[1]
    interval_seconds: Literal[60]
    trip_duration_seconds: Literal[60]
    accept_retry_after: Literal[True]
    status_code_ranges: tuple[GatewayBackendFailureStatusCodeRange, ...] = (
        GatewayBackendFailureStatusCodeRange(minimum=429, maximum=429),
        GatewayBackendFailureStatusCodeRange(minimum=408, maximum=408),
        GatewayBackendFailureStatusCodeRange(minimum=500, maximum=599),
    )
    error_reasons: tuple[Literal["BackendConnectionFailure", "Timeout"], ...] = (
        "BackendConnectionFailure",
        "Timeout",
    )

    @model_validator(mode="after")
    def validate_failure_signals(self) -> GatewayRateLimitCircuitBreaker:
        ranges = tuple(
            (item.minimum, item.maximum) for item in self.status_code_ranges
        )
        if ranges != ((429, 429), (408, 408), (500, 599)):
            raise ValueError("the platform breaker requires 429, 408, and 500-599 ranges")
        if self.error_reasons != ("BackendConnectionFailure", "Timeout"):
            raise ValueError(
                "the platform breaker requires connection failure and timeout reasons"
            )
        return self


class GatewayRateLimitResilience(StrictModel):
    max_attempts_per_request: Literal[2]
    retry_interval_seconds: Literal[1]
    first_fast_retry: Literal[True]
    backend_timeout_seconds: Literal[120] = 120
    circuit_breaker: GatewayRateLimitCircuitBreaker


class GatewayBackendPoolWrite(StrictModel):
    members: list[GatewayBackendPoolMemberWrite] = Field(min_length=2, max_length=30)
    rate_limit: GatewayRateLimitResilience
    session_affinity: bool = False

    @model_validator(mode="after")
    def validate_members(self) -> GatewayBackendPoolWrite:
        runtime_ids = [member.runtime_id for member in self.members]
        if len(runtime_ids) != len(set(runtime_ids)):
            raise ValueError("a backend pool cannot repeat a runtime")
        priority_weights: dict[int, int] = {}
        for member in self.members:
            priority_weights[member.priority] = (
                priority_weights.get(member.priority, 0) + member.weight
            )
        if any(total == 0 for total in priority_weights.values()):
            raise ValueError("every backend priority group needs a positive weight")
        return self


class GatewayBackendPoolMember(StrictModel):
    runtime_id: UUID
    runtime_name: str = Field(min_length=1, max_length=160)
    backend_url: HttpUrl
    auth_strategy: AuthStrategy = AuthStrategy.NONE
    named_value_name: str | None = None
    priority: int = Field(ge=0, le=100)
    weight: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def require_member_credential(self) -> GatewayBackendPoolMember:
        if self.auth_strategy in {
            AuthStrategy.NAMED_VALUE_BEARER,
            AuthStrategy.NAMED_VALUE_API_KEY,
        } and not self.named_value_name:
            raise ValueError("a named-value pool member requires named_value_name")
        return self


class GatewayBackendPoolConfig(StrictModel):
    schema_version: Literal[1] = 1
    members: list[GatewayBackendPoolMember] = Field(min_length=2, max_length=30)
    rate_limit: GatewayRateLimitResilience
    session_affinity: bool = False

    @model_validator(mode="after")
    def validate_members(self) -> GatewayBackendPoolConfig:
        GatewayBackendPoolWrite(
            members=[
                GatewayBackendPoolMemberWrite(
                    runtime_id=member.runtime_id,
                    priority=member.priority,
                    weight=member.weight,
                )
                for member in self.members
            ],
            rate_limit=self.rate_limit,
            session_affinity=self.session_affinity,
        )
        return self


class DiscoveryModel(StrictModel):
    id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    api_format: ApiFormat = ApiFormat.ANTHROPIC_MESSAGES


class ModelRemovalTarget(StrictModel):
    model_id: UUID
    model_key: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    api_format: ApiFormat


class GatewayModelBinding(StrictModel):
    provider_id: UUID | None = None
    provider_name: str = Field(min_length=1, max_length=120)
    provider_kind: ProviderKind
    provider_brand_key: BrandKey
    provider_config: dict[str, object] = Field(default_factory=dict)
    runtime_id: UUID | None = None
    routing_managed: bool = False
    runtime_name: str = Field(min_length=1, max_length=160)
    runtime_kind: RuntimeKind
    runtime_brand_key: BrandKey
    api_format: ApiFormat
    backend_url: HttpUrl | None = None
    backend_path: str = Field(min_length=1, max_length=1000)
    auth_strategy: AuthStrategy
    named_value_name: str | None = None
    key_vault_secret_id: HttpUrl | None = None
    managed_identity_resource: str | None = None
    streaming_mode: StreamingMode
    runtime_config: dict[str, object] = Field(default_factory=dict)
    model: ModelTarget

    @property
    def oauth(self) -> OAuthClientCredentialsConfig | None:
        if self.auth_strategy is not AuthStrategy.OAUTH_CLIENT_CREDENTIALS:
            return None
        return OAuthClientCredentialsConfig.model_validate(self.runtime_config.get("oauth"))

    @property
    def backend_pool(self) -> GatewayBackendPoolConfig | None:
        raw_pool = self.runtime_config.get("apim_backend_pool")
        if raw_pool is None:
            return None
        pool = GatewayBackendPoolConfig.model_validate(raw_pool)
        affinity = self.runtime_config.get("apim_backend_pool_session_affinity")
        if affinity is not None:
            if not isinstance(affinity, bool):
                raise ValueError("backend pool session affinity must be a boolean")
            pool = pool.model_copy(update={"session_affinity": affinity})
        return pool

    @model_validator(mode="after")
    def require_managed_backend_details(self) -> GatewayModelBinding:
        if self.auth_strategy is AuthStrategy.OAUTH_CLIENT_CREDENTIALS:
            oauth = self.oauth
            if (
                self.provider_brand_key is not BrandKey.AZURE_DATABRICKS
                or oauth is None or not self.routing_managed
                or self.named_value_name is not None or self.managed_identity_resource is not None
                or str(oauth.token_url) != str(self.backend_url).rstrip("/") + "/oidc/v1/token"
            ):
                raise ValueError("OAuth credentials must belong to their Databricks Workspace")
        if self.runtime_id is None and self.backend_url is None:
            raise ValueError("a new runtime binding requires backend_url")
        if self.auth_strategy in {
            AuthStrategy.NAMED_VALUE_BEARER,
            AuthStrategy.NAMED_VALUE_API_KEY,
        } and not self.named_value_name:
            raise ValueError("a named-value binding requires named_value_name")
        if (
            self.auth_strategy is AuthStrategy.MANAGED_IDENTITY
            and not self.managed_identity_resource
        ):
            raise ValueError("a managed-identity binding requires managed_identity_resource")
        return self


class GatewayReleaseSpec(StrictModel):
    gateway_profile_id: UUID
    discovery_models: list[DiscoveryModel]
    bindings: list[GatewayModelBinding]
    removed_models: list[ModelRemovalTarget] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def require_unique_aliases(self) -> GatewayReleaseSpec:
        aliases = [item.id.casefold() for item in self.discovery_models]
        if len(aliases) != len(set(aliases)):
            raise ValueError("model aliases must be unique within a gateway release")
        binding_aliases = [item.model.model_key.casefold() for item in self.bindings]
        if len(binding_aliases) != len(set(binding_aliases)):
            raise ValueError("dynamic model bindings must have unique aliases")
        if not set(binding_aliases).issubset(set(aliases)):
            raise ValueError("every dynamic binding must be present in model discovery")
        removed_aliases = [item.model_key.casefold() for item in self.removed_models]
        if set(removed_aliases) & (set(aliases) | set(binding_aliases)):
            raise ValueError("a removed model cannot remain active in the gateway release")
        for binding in self.bindings:
            pool = binding.backend_pool
            if pool is None:
                continue
            if not binding.routing_managed:
                raise ValueError("only managed routes can use an APIM backend pool")
            if binding.runtime_id is None:
                raise ValueError("a backend pool requires an existing primary runtime")
            primary = next(
                (
                    member
                    for member in pool.members
                    if member.runtime_id == binding.runtime_id
                ),
                None,
            )
            if primary is None:
                raise ValueError("a backend pool must include its primary runtime")
            if binding.backend_url is None or str(primary.backend_url).rstrip("/") != str(
                binding.backend_url
            ).rstrip("/"):
                raise ValueError("the primary pool member must preserve the binding backend URL")
        return self


class GatewayPublication(StrictModel):
    id: UUID
    gateway_profile_id: UUID
    generation: int
    publication_kind: PublicationKind = PublicationKind.MODEL_ADD
    desired_spec: GatewayReleaseSpec
    desired_spec_sha256: str
    status: PublicationStatus
    base_release_id: UUID | None
    apim_revision: str | None
    policy_sha256: str | None
    resource_manifest: dict[str, object]
    error_code: str | None
    error_message: str | None
    attempt_count: int
    created_by: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime

    @model_validator(mode="after")
    def require_operation_target(self) -> GatewayPublication:
        if self.publication_kind is PublicationKind.MODEL_REMOVE:
            if len(self.desired_spec.removed_models) != 1:
                raise ValueError("a model removal publication requires one removal target")
        elif self.desired_spec.removed_models:
            raise ValueError("only model removal publications can carry a removal target")
        if (
            self.publication_kind is not PublicationKind.MODEL_REMOVE
            and not self.desired_spec.bindings
        ):
            raise ValueError("non-removal publications require a binding")
        return self


class GatewayAuthorizationRequirement(StrictModel):
    kind: Literal["azure_rbac"]
    principal_id: UUID
    resource_endpoint: HttpUrl
    role_id: UUID
    role_name: str = Field(min_length=1, max_length=120)


class DatabricksAuthorizationRequirement(StrictModel):
    kind: Literal["databricks_workspace"]
    principal_id: UUID
    resource_endpoint: HttpUrl
    role_name: Literal["CAN_QUERY"]


class DatabricksOAuthAuthorizationRequirement(StrictModel):
    kind: Literal["databricks_oauth"]
    client_id: UUID
    resource_endpoint: HttpUrl
    role_name: Literal["CAN_QUERY"]


def publication_model_id(publication_id: UUID, model_key: str) -> UUID:
    return uuid5(publication_id, model_key)


def publication_materialized_named_values(
    publication: GatewayPublication,
    *,
    provisioning_completed: bool = False,
) -> list[str]:
    if publication.publication_kind in {
        PublicationKind.MODEL_REMOVE,
        PublicationKind.ROUTE_RECONCILE,
    }:
        return []
    binding = publication.desired_spec.bindings[-1]
    if (
        binding.auth_strategy
        not in {AuthStrategy.NAMED_VALUE_BEARER, AuthStrategy.NAMED_VALUE_API_KEY}
        or binding.key_vault_secret_id is not None
        or binding.named_value_name is None
    ):
        return []
    raw_names = publication.resource_manifest.get("named_values")
    names = (
        [value for value in raw_names if isinstance(value, str)]
        if isinstance(raw_names, list)
        else []
    )
    if binding.named_value_name not in names and (
        publication.apim_revision is not None
        or provisioning_completed
        or binding.runtime_config.get("credential_provisioned") is True
    ):
        names.append(binding.named_value_name)
    return names


def publication_retry_requires_credential(publication: GatewayPublication) -> bool:
    if publication.publication_kind in {
        PublicationKind.MODEL_REMOVE,
        PublicationKind.ROUTE_RECONCILE,
    }:
        return False
    binding = publication.desired_spec.bindings[-1]
    if binding.oauth is not None:
        materialized = publication.resource_manifest.get("oauth_credentials")
        return not isinstance(materialized, list) or binding.oauth.provider_id not in materialized
    return (
        binding.auth_strategy
        in {AuthStrategy.NAMED_VALUE_BEARER, AuthStrategy.NAMED_VALUE_API_KEY}
        and binding.key_vault_secret_id is None
        and binding.named_value_name not in publication_materialized_named_values(publication)
    )


class GatewayPublicationView(StrictModel):
    id: UUID
    gateway_profile_id: UUID
    generation: int
    publication_kind: PublicationKind
    model_key: str
    display_name: str
    status: PublicationStatus
    error_code: str | None
    error_message: str | None
    authorization: (
        GatewayAuthorizationRequirement | DatabricksAuthorizationRequirement
        | DatabricksOAuthAuthorizationRequirement | None
    ) = None
    retry_requires_credential: bool = False
    credential_kind: Literal["api_key", "oauth_m2m"] | None = None
    retry_can_authorize_image_probes: bool = False
    attempt_count: int
    created_by: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime

    @classmethod
    def from_publication(cls, publication: GatewayPublication) -> GatewayPublicationView:
        if publication.publication_kind is PublicationKind.MODEL_REMOVE:
            target = publication.desired_spec.removed_models[0]
            model_key = target.model_key
            display_name = target.display_name
        elif publication.publication_kind is PublicationKind.ROUTE_RECONCILE:
            model_key = "all-models"
            display_name = "All models"
        else:
            model = publication.desired_spec.bindings[-1].model
            model_key = model.model_key
            display_name = model.display_name
        authorization: (
            GatewayAuthorizationRequirement | DatabricksAuthorizationRequirement
            | DatabricksOAuthAuthorizationRequirement | None
        ) = None
        if (
            publication.status is PublicationStatus.AWAITING_AUTHORIZATION
            and publication.publication_kind is not PublicationKind.MODEL_REMOVE
        ):
            raw_authorization = publication.desired_spec.bindings[-1].runtime_config.get(
                "authorization"
            )
            if isinstance(raw_authorization, dict):
                requirement_type = (
                    DatabricksOAuthAuthorizationRequirement
                    if raw_authorization.get("kind") == "databricks_oauth"
                    else DatabricksAuthorizationRequirement
                    if raw_authorization.get("kind") == "databricks_workspace"
                    else GatewayAuthorizationRequirement
                )
                authorization = requirement_type.model_validate(raw_authorization)
        probe_authorization = publication.resource_manifest.get("image_probe_authorization", {})
        probe_limit = (
            probe_authorization.get("attempt_limit", 1)
            if isinstance(probe_authorization, dict)
            else None
        )
        can_authorize_probes = (
            publication.status is PublicationStatus.FAILED
            and publication.publication_kind
            in {PublicationKind.MODEL_ADD, PublicationKind.CREDENTIAL_ROTATION}
            and any(
                binding.api_format is ApiFormat.OPENAI_IMAGES
                for binding in publication.desired_spec.bindings
            )
            and type(probe_limit) is int and 1 <= probe_limit < 32
        )
        infrastructure_upgrade_required = (
            publication.status is PublicationStatus.FAILED
            and publication.error_code == "InfrastructureUpgradeRequiredError"
        )
        return cls(
            id=publication.id,
            gateway_profile_id=publication.gateway_profile_id,
            generation=publication.generation,
            publication_kind=publication.publication_kind,
            model_key=model_key,
            display_name=display_name,
            status=publication.status,
            error_code=(
                "apim_infrastructure_upgrade_required"
                if infrastructure_upgrade_required
                else "publication_failed"
                if publication.status is PublicationStatus.FAILED
                else "provider_authorization_required"
                if publication.status is PublicationStatus.AWAITING_AUTHORIZATION
                else None
            ),
            error_message=(
                "The gateway infrastructure must be upgraded before publishing image models. "
                "Ask the deployment administrator to run the APIM infrastructure upgrade."
                if infrastructure_upgrade_required else None
            ),
            authorization=authorization,
            retry_can_authorize_image_probes=can_authorize_probes,
            retry_requires_credential=publication_retry_requires_credential(publication),
            credential_kind=(
                "oauth_m2m" if publication.desired_spec.bindings
                and publication.desired_spec.bindings[-1].oauth is not None
                else "api_key" if publication_retry_requires_credential(publication)
                else None
            ),
            attempt_count=publication.attempt_count,
            created_by=publication.created_by,
            created_at=publication.created_at,
            started_at=publication.started_at,
            completed_at=publication.completed_at,
            updated_at=publication.updated_at,
        )


class GatewayPublicationList(StrictModel):
    items: list[GatewayPublicationView]


class GatewayReleaseChangeSet(StrictModel):
    added_models: list[str] = Field(default_factory=list)
    removed_models: list[str] = Field(default_factory=list)
    changed_models: list[str] = Field(default_factory=list)
    added_backend_pools: list[str] = Field(default_factory=list)
    removed_backend_pools: list[str] = Field(default_factory=list)
    changed_backend_pools: list[str] = Field(default_factory=list)


class GatewayReleaseDependencies(StrictModel):
    apim_revision: str | None
    parent_policy_sha256: str | None
    compiled_policy_sha256: str | None
    backends: list[str] = Field(default_factory=list)
    backend_pools: list[str] = Field(default_factory=list)
    named_values: list[str] = Field(default_factory=list)
    oauth_credentials: list[str] = Field(default_factory=list)
    recorded_complete: bool
    live_status: Literal["not_checked", "healthy", "missing", "mismatched"]
    live_checked_at: datetime | None = None
    issues: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def serialize_legacy_compatible_dependencies(
        self, handler: SerializerFunctionWrapHandler,
    ) -> dict[str, object]:
        result: dict[str, object] = handler(self)
        if not self.oauth_credentials:
            result.pop("oauth_credentials", None)
        return result


class GatewayReleaseRetentionPolicy(StrictModel):
    retained_count: int = Field(ge=1, le=1000)
    retained_days: int = Field(ge=1, le=3650)
    failed_retained_days: int = Field(ge=1, le=3650)
    protected_labels: list[str] = Field(default_factory=list)


class GatewayReleaseProtectionWrite(StrictModel):
    pinned: bool = True
    protected_label: str | None = Field(default=None, min_length=1, max_length=80)
    retain_until: datetime | None = None


class GatewayReleaseProtection(StrictModel):
    publication_id: UUID
    pinned: bool
    protected_label: str | None
    retain_until: datetime | None
    updated_by: str
    updated_at: datetime


class GatewayReleaseProtectionAuditEvent(StrictModel):
    id: UUID
    publication_id: UUID
    pinned: bool
    protected_label: str | None
    retain_until: datetime | None
    actor: str
    created_at: datetime


class GatewayReleaseAuditEvent(StrictModel):
    id: UUID
    publication_id: UUID
    from_status: PublicationStatus | None
    to_status: PublicationStatus
    actor: str
    detail: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class GatewayReleaseSummary(StrictModel):
    id: UUID
    gateway_profile_id: UUID
    gateway_name: str
    generation: int
    role: GatewayReleaseRole
    publication_kind: PublicationKind
    model_key: str
    display_name: str
    status: PublicationStatus
    apim_revision: str | None
    policy_sha256: str | None
    desired_spec_sha256: str
    change_set: GatewayReleaseChangeSet
    dependency_count: int
    recorded_dependencies_complete: bool
    live_integrity_status: Literal["not_checked", "healthy", "missing", "mismatched"]
    rollback_eligible: bool
    rollback_blockers: list[str] = Field(default_factory=list)
    pinned: bool = False
    protected_label: str | None = None
    retain_until: datetime | None = None
    retention_eligible: bool = False
    retention_reasons: list[str] = Field(default_factory=list)
    created_by: str
    created_at: datetime
    completed_at: datetime | None


class GatewayReleaseList(StrictModel):
    items: list[GatewayReleaseSummary]
    retention_policy: GatewayReleaseRetentionPolicy | None = None
    operations_enabled: bool
    operations_disabled_reason: str | None = None


class GatewayReleaseDiff(StrictModel):
    release_id: UUID
    against_release_id: UUID | None
    changes: GatewayReleaseChangeSet


class GatewayReleaseIntegrity(StrictModel):
    release_id: UUID
    dependencies: GatewayReleaseDependencies
    rollback_eligible: bool
    rollback_blockers: list[str] = Field(default_factory=list)


class GatewayReleaseRollbackPreview(StrictModel):
    target_release_id: UUID
    current_release_id: UUID
    changes: GatewayReleaseChangeSet
    dependencies: GatewayReleaseDependencies
    rollback_eligible: bool
    rollback_blockers: list[str] = Field(default_factory=list)
    confirmation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GatewayReleaseRollbackRequest(StrictModel):
    confirmation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GatewayReleaseOperationAuditEvent(StrictModel):
    id: UUID
    operation_id: UUID
    from_status: GatewayReleaseOperationStatus | None
    to_status: GatewayReleaseOperationStatus
    actor: str
    detail: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class GatewayReleaseGcCandidate(StrictModel):
    resource_type: str
    resource_id: str
    reasons: list[str] = Field(default_factory=list)


class GatewayReleaseGcPlan(StrictModel):
    id: UUID
    operation_id: UUID
    gateway_profile_id: UUID
    retained_release_ids: list[UUID]
    current_non_release_references: dict[str, object]
    candidates: list[GatewayReleaseGcCandidate]
    reference_graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: str
    created_at: datetime


class GatewayReleaseOperation(StrictModel):
    id: UUID
    gateway_profile_id: UUID
    operation_kind: GatewayReleaseOperationKind
    target_release_id: UUID | None
    prior_release_id: UUID | None
    status: GatewayReleaseOperationStatus
    semantic_preview: dict[str, object] = Field(default_factory=dict)
    checkpoint: dict[str, object] = Field(default_factory=dict)
    error_code: str | None
    error_message: str | None
    attempt_count: int = Field(ge=0)
    created_by: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime
    worker_available: bool
    worker_unavailable_reason: str | None = None
    audit: list[GatewayReleaseOperationAuditEvent] = Field(default_factory=list)
    gc_plan: GatewayReleaseGcPlan | None = None


class GatewayReleaseOperationAccepted(StrictModel):
    operation: GatewayReleaseOperation
    status_url: str


class GatewayApplicationSubscriptionProvisionAccepted(GatewayReleaseOperationAccepted):
    primary_key: str = Field(min_length=1, max_length=256, repr=False)


class GatewayReleaseDetail(GatewayReleaseSummary):
    base_release_id: UUID | None
    diff_from_base: GatewayReleaseDiff
    dependencies: GatewayReleaseDependencies
    audit: list[GatewayReleaseAuditEvent] = Field(default_factory=list)
    protection_audit: list[GatewayReleaseProtectionAuditEvent] = Field(
        default_factory=list
    )


class GatewayPublicationRequestAccepted(StrictModel):
    publication: GatewayPublicationView
    status_url: str
