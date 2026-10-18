from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast
from uuid import UUID, uuid5

from pydantic import (
    Field,
    HttpUrl,
    SecretStr,
    SerializerFunctionWrapHandler,
    ValidationError,
    model_serializer,
    model_validator,
)

from .gateway_targets import (
    GatewayDeploymentScope,
    GatewayDeploymentScopeMode,
    GatewayServingDeployment,
    MultiTargetRollout,
)
from .image_profiles import ImageGenerationLimits, ImageGenerationProfile, validate_image_profile
from .models import StrictModel
from .runtime_models import (
    BrandKey,
    ContextPriceRule,
    ModelCapability,
    ModelFamilyKey,
    ModelVendorKey,
    OAuthClientCredentialsConfig,
    PriceSource,
    ProviderKind,
    ProviderTarget,
    RuntimeKind,
    StoredContextPriceTier,
    custom_context_rule,
    default_capabilities,
)

_PUBLICATION_PERMISSIONS = json.loads(
    Path(__file__).with_name("apim_publication_permissions.json").read_text(encoding="utf-8")
)
PUBLICATION_REQUIRED_ACTIONS: tuple[str, ...] = tuple(_PUBLICATION_PERMISSIONS["publication"])
PUBLICATION_RECOVERY_ACTIONS: tuple[str, ...] = tuple(_PUBLICATION_PERMISSIONS["recovery"])
PUBLICATION_PROBE_ACTIONS: tuple[str, ...] = tuple(_PUBLICATION_PERMISSIONS["probe"])
PUBLICATION_OAUTH_ACTIONS: tuple[str, ...] = tuple(_PUBLICATION_PERMISSIONS["oauth"])
GATEWAY_PROVISION_ACTIONS: tuple[str, ...] = tuple(_PUBLICATION_PERMISSIONS["gateway_provision"])
GATEWAY_BOOTSTRAP_ACTIONS: tuple[str, ...] = tuple(_PUBLICATION_PERMISSIONS["gateway_bootstrap"])


class MultiTargetPublicationRequestAccepted(StrictModel):
    rollout: MultiTargetRollout
    publications: list[GatewayPublicationView]
    status_url: str


class ApiFormat(StrEnum):
    OPENAI_CHAT = "openai_chat"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    OPENAI_IMAGES = "openai_images"
    MICROSOFT_DECISION = "microsoft_decision"


class AuthStrategy(StrEnum):
    NONE = "none"
    MANAGED_IDENTITY = "managed_identity"
    NAMED_VALUE_BEARER = "named_value_bearer"
    NAMED_VALUE_API_KEY = "named_value_api_key"
    OAUTH_CLIENT_CREDENTIALS = "oauth_client_credentials"


class StreamingMode(StrEnum):
    NATIVE = "native"
    BUFFERED = "buffered"


@dataclass(frozen=True)
class CountTokensOperation:
    support: Literal["supported", "unsupported", "unknown"]
    backend_path: str | None = None


@dataclass(frozen=True)
class CountTokensRejection:
    status_code: int
    reason: str
    error_type: str
    code: str
    message: str

    def payload(self) -> dict[str, object]:
        return {"type": "error", "error": {
            "type": self.error_type, "code": self.code, "message": self.message,
        }}

    def matches(self, status_code: int, payload: object) -> bool:
        if status_code != self.status_code or not isinstance(payload, dict):
            return False
        error = payload.get("error")
        return (
            payload.get("type") == "error"
            and isinstance(error, dict)
            and error.get("type") == self.error_type
            and error.get("code") == self.code
            and isinstance(error.get("message"), str)
            and bool(error["message"].strip())
        )


COUNT_TOKENS_REJECTIONS = MappingProxyType({
    "unavailable": CountTokensRejection(
        404, "Not Found", "not_found_error", "count_tokens_unavailable",
        "Token counting is unavailable for this model",
    ),
    "unpublished": CountTokensRejection(
        400, "Bad Request", "invalid_request_error", "model_not_published",
        "The requested model is not published by this gateway revision",
    ),
})


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


def program_migration_spec_matches(
    previous: GatewayReleaseSpec, candidate: GatewayReleaseSpec,
) -> bool:
    before = previous.model_copy(update={"removed_models": []}).model_dump(mode="json")
    after = candidate.model_dump(mode="json")
    if len(before["bindings"]) != len(after["bindings"]):
        return False
    for old, new in zip(before["bindings"], after["bindings"], strict=True):
        if old.get("runtime_id") is None and new.get("runtime_id") is not None:
            try:
                if str(UUID(new["runtime_id"])) != new["runtime_id"]:
                    return False
            except (ValueError, TypeError):
                return False
            old["runtime_id"] = new["runtime_id"]
    return before == after


class GatewayReleaseOperationKind(StrEnum):
    ROLLBACK = "rollback"
    INTEGRITY_CHECK = "integrity_check"
    GC_PLAN = "gc_plan"
    APPLICATION_SYNC = "application_sync"
    APPLICATION_PROVISION = "application_provision"
    TARGET_INVENTORY_SYNC = "target_inventory_sync"
    APIM_ONBOARDING = "apim_onboarding"
    SELF_HOSTED_GATEWAY_PROVISION = "self_hosted_gateway_provision"


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
            raise ValueError(
                "a new OpenAI-compatible runtime requires its Base URL and API key"
            )
        if self.model_vendor is not None and not has_openai:
            raise ValueError(
                "model_vendor belongs only to a new OpenAI-compatible runtime"
            )
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
        if self.context_price_tiers:
            stored_rule = custom_context_rule(self.context_price_tiers)
            if self.context_price_rule is not None and self.context_price_rule != stored_rule:
                raise ValueError("The context rule must match the stored schedule")
            self.context_price_rule = stored_rule
        if self.context_price_rule is not None and (
            self.price_source is PriceSource.MANUAL or len(self.context_price_references) != 2
        ):
            raise ValueError("A custom context rule requires both short and long catalog tiers")
        if self.price_source is not PriceSource.MANUAL:
            reference = (self.price_reference or "").strip()
            if not self.context_price_references and (
                (not reference and self.price_source is not PriceSource.MODELS_DEV)
                or (reference and not reference.startswith(f"{self.price_source.value}:"))
            ):
                raise ValueError("Catalog pricing requires a reference from its selected source")
            self.price_reference = reference or None
        if self.image_profile is not None:
            if self.capabilities != ["image_generation"]:
                raise ValueError("An image profile belongs only to an image model")
            if self.price_source not in {
                PriceSource.MANUAL, PriceSource.MODELS_DEV, PriceSource.AZURE_RETAIL,
            }:
                raise ValueError("Image generation requires manual or a compatible catalog price")
            validate_image_profile(self.image_profile)
        return self

    price_source: PriceSource = Field(
        default=PriceSource.MANUAL, exclude_if=lambda value: value is PriceSource.MANUAL,
    )

    price_reference: str | None = Field(
        default=None, max_length=1500, exclude_if=lambda value: value is None,
    )

    price_discount_percent: float | None = Field(
        default=None, gt=0, le=100, exclude_if=lambda value: value is None,
    )

    context_price_references: list[str] = Field(
        default_factory=list, max_length=32, exclude_if=lambda value: not value,
    )

    context_price_tiers: list[StoredContextPriceTier] = Field(
        default_factory=list, max_length=32, exclude_if=lambda value: not value,
    )

    context_price_rule: ContextPriceRule | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    list_input_cost_per_million: float | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None,
    )

    list_output_cost_per_million: float | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None,
    )

    list_cached_cost_per_million: float | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None,
    )

    list_cache_write_cost_per_million: float | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None,
    )


class ModelCreateTarget(StrictModel):
    operation: Literal["chat", "image_generation", "decision"] = "chat"
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

    @model_validator(mode="after")
    def require_deployment_or_explicit_identity(self) -> ModelCreateTarget:
        if self.context_price_rule is not None and len(self.context_price_references) != 2:
            raise ValueError("A custom context rule requires both short and long catalog tiers")
        if self.context_price_references:
            if (self.price_source in {None, PriceSource.MANUAL}
                or self.operation == "image_generation"):
                raise ValueError("Context pricing requires a catalog-priced text model")
            if (len(self.context_price_references) < 2
                or len(set(self.context_price_references)) != len(self.context_price_references)):
                raise ValueError("Context pricing requires distinct tier references")
            assert self.price_source is not None
            if any(not reference.startswith(f"{self.price_source.value}:")
                   for reference in self.context_price_references):
                raise ValueError("Context references must use the selected source")
        if self.price_source is PriceSource.MANUAL:
            self.price_reference = None
        elif self.price_source is not None and not self.context_price_references:
            reference = (self.price_reference or "").strip()
            if (not reference and self.price_source is not PriceSource.MODELS_DEV) \
                    or (reference and not reference.startswith(f"{self.price_source.value}:")):
                raise ValueError("Catalog pricing requires a reference from its selected source")
            self.price_reference = reference or None
        if self.operation == "image_generation":
            if self.price_source not in {
                None, PriceSource.MODELS_DEV, PriceSource.MANUAL, PriceSource.AZURE_RETAIL,
            }:
                raise ValueError("Image generation requires manual or a compatible catalog price")
            if self.deployment_name is None:
                raise ValueError("Image generation requires an existing Foundry deployment")
            if self.context_window is not None or self.cache_write_cost_per_million is not None:
                raise ValueError("Image generation has no text context window or cache-write rate")
        elif self.image_configuration is not None:
            raise ValueError("An image profile cannot be attached to a text model")
        if self.operation == "decision":
            if self.deployment_name is None:
                raise ValueError("Decision requires an existing Foundry deployment")
            if self.price_source not in {
                None, PriceSource.MANUAL, PriceSource.AZURE_RETAIL,
            }:
                raise ValueError("Decision requires manual or Azure retail prices")
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

    price_source: PriceSource | None = None

    price_reference: str | None = Field(default=None, max_length=1500)

    price_discount_percent: float | None = Field(default=None, gt=0, le=100)

    context_price_references: list[str] = Field(default_factory=list, max_length=32)

    context_price_rule: ContextPriceRule | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    price_entry_digest: str | None = Field(default=None, max_length=64)

    allow_unpriced: bool = False


class GatewayPublicationCreate(StrictModel):
    gateway_profile_id: UUID
    provider: ProviderTarget
    runtime: RuntimeTarget
    model: ModelCreateTarget
    deployment_scope: GatewayDeploymentScope = Field(
        default_factory=GatewayDeploymentScope
    )


class MultiTargetPublicationChildCreate(StrictModel):
    publication: GatewayPublicationCreate
    required: bool = True


class MultiTargetPublicationCreate(StrictModel):
    children: tuple[MultiTargetPublicationChildCreate, ...] = Field(
        min_length=2,
        max_length=20,
    )

    @model_validator(mode="after")
    def require_distinct_targets(self) -> MultiTargetPublicationCreate:
        target_ids = [
            child.publication.gateway_profile_id for child in self.children
        ]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("a multi-target publication cannot repeat an APIM target")
        return self


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
    deployment_scope: GatewayDeploymentScope = Field(
        default_factory=GatewayDeploymentScope
    )
    serving_deployments: tuple[GatewayServingDeployment, ...] = ()
    logical_model_id: UUID | None = None
    model: ModelTarget

    @property
    def count_tokens_operation(self) -> CountTokensOperation:
        if self.api_format is not ApiFormat.ANTHROPIC_MESSAGES:
            return CountTokensOperation("unsupported")
        if self.provider_brand_key in {BrandKey.AMAZON_BEDROCK, BrandKey.AZURE_DATABRICKS}:
            return CountTokensOperation("unsupported")
        if (
            self.routing_managed
            and self.backend_url is not None
            and self.provider_brand_key is BrandKey.MICROSOFT_FOUNDRY
            and self.backend_path.rstrip("/") == "/anthropic/v1/messages"
        ):
            return CountTokensOperation("supported", "/anthropic/v1/messages/count_tokens")
        return CountTokensOperation("unknown")

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
        serving_ids = [item.deployment_id for item in self.serving_deployments]
        native_ids = [item.native_gateway_id.casefold() for item in self.serving_deployments]
        if len(serving_ids) != len(set(serving_ids)) or len(native_ids) != len(
            set(native_ids)
        ):
            raise ValueError("a model binding cannot repeat a serving Gateway deployment")
        if self.deployment_scope.mode is GatewayDeploymentScopeMode.ALL:
            if self.deployment_scope.deployment_ids:
                raise ValueError("an all-deployments binding cannot select deployment IDs")
        elif set(serving_ids) != set(self.deployment_scope.deployment_ids):
            raise ValueError(
                "selected deployment scope must match its immutable serving deployments"
            )
        return self


class GatewayReleaseSpec(StrictModel):
    gateway_profile_id: UUID
    discovery_models: list[DiscoveryModel]
    bindings: list[GatewayModelBinding]
    gateway_deployments: tuple[GatewayServingDeployment, ...] = ()
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
        release_deployment_ids = {
            deployment.deployment_id for deployment in self.gateway_deployments
        }
        release_native_ids = {
            deployment.native_gateway_id.casefold()
            for deployment in self.gateway_deployments
        }
        if len(release_deployment_ids) != len(self.gateway_deployments) or len(
            release_native_ids
        ) != len(self.gateway_deployments):
            raise ValueError("a gateway release cannot repeat a serving deployment")
        for binding in self.bindings:
            if not {
                deployment.deployment_id
                for deployment in binding.serving_deployments
            }.issubset(release_deployment_ids):
                raise ValueError(
                    "binding serving deployments must belong to the gateway release"
                )
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


class GatewayPublicationIntent(StrictModel):
    gateway_profile_id: UUID
    publication_kind: PublicationKind = PublicationKind.MODEL_ADD
    desired_spec: GatewayReleaseSpec

    @model_validator(mode="after")
    def require_operation_target(self) -> GatewayPublicationIntent:
        if self.gateway_profile_id != self.desired_spec.gateway_profile_id:
            raise ValueError("publication target must match its desired gateway release")
        if self.publication_kind is PublicationKind.MODEL_REMOVE:
            if len(self.desired_spec.removed_models) != 1:
                raise ValueError("a model removal publication requires one removal target")
        elif self.desired_spec.removed_models:
            raise ValueError("only model removal publications can carry a removal target")
        if (
            self.publication_kind is not PublicationKind.MODEL_REMOVE
            and not self.desired_spec.bindings
        ):
            if isinstance(self, GatewayPublication):
                self.require_empty_fixed_installation()
                return self
            raise ValueError("non-removal publications require a binding")
        return self


class GatewayPublication(GatewayPublicationIntent):
    id: UUID
    generation: int
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

    def require_onboarding_installation(
        self, target_id: UUID, operation_id: UUID, artifact_sha256: str | None,
    ) -> None:
        from .storage_routes import FixedProgramInstallation, canonical_document, document_digest

        self.require_empty_fixed_installation()
        raw_program = self.resource_manifest.get("fixed_route_program")
        if not isinstance(raw_program, dict):
            raise ValueError("APIM initialization fixed program is missing")
        installed = FixedProgramInstallation.model_validate_json(canonical_document(raw_program))
        if (
            artifact_sha256 is None or self.gateway_profile_id != target_id
            or self.id != uuid5(operation_id, "fixed-program-installation")
            or self.desired_spec_sha256 != document_digest(self.desired_spec)
            or not installed.model_additions_enabled
            or installed.program.scope.gateway_id != "managed"
            or self.resource_manifest.get("onboarding_operation_id") != str(operation_id)
            or self.resource_manifest.get("initialization_program_artifact_sha256")
            != artifact_sha256
        ):
            raise ValueError("APIM initialization requires its exact verified fixed program")

    def require_empty_fixed_installation(self) -> None:
        from .storage_routes import (
            ActiveRouteHead,
            ProgramIdentity,
            RouteSnapshot,
            canonical_document,
        )

        if (
            self.publication_kind is not PublicationKind.ROUTE_RECONCILE
            or self.status is not PublicationStatus.ACTIVE or self.base_release_id is not None
            or self.generation != 1 or self.desired_spec.discovery_models
            or self.desired_spec.removed_models
        ):
            raise ValueError("Only an initial fixed installation can have an empty release")
        raw_program = self.resource_manifest.get("fixed_route_program")
        if not isinstance(raw_program, dict):
            raise ValueError("Empty installation requires a fixed program")
        identity = raw_program.get("program")
        raw_snapshot = self.resource_manifest.get("fixed_route_snapshot")
        raw_head = self.resource_manifest.get("fixed_route_head")
        if (not isinstance(identity, dict) or not isinstance(raw_snapshot, dict)
                or not isinstance(raw_head, dict)):
            raise ValueError("Empty installation records are incomplete")
        program = ProgramIdentity.model_validate_json(canonical_document(identity))
        snapshot = RouteSnapshot.model_validate_json(canonical_document(raw_snapshot))
        head = ActiveRouteHead.model_validate_json(canonical_document(raw_head))
        snapshot.require_valid(head.activated_epoch)
        if (
            snapshot.routes or snapshot.members or snapshot.approval.entry_count != 0
            or head.approval != snapshot.approval or head.approval.generation != 1
            or program.scope != head.approval.scope
            or program.scope.target_id != str(self.gateway_profile_id)
            or program.api_revision != self.apim_revision
            or program.program_sha256 != self.policy_sha256
            or program.program_sha256 != head.approval.required_program_sha256
        ):
            raise ValueError("Empty installation program and configuration binding changed")


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


class PublicationCredentialResource(StrictModel):
    name: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["present", "missing", "unverified"]


class PublicationCredentialEvidence(StrictModel):
    version: Literal[1] = 1
    publication_id: UUID
    gateway_profile_id: UUID
    resources: tuple[PublicationCredentialResource, ...]

    @model_validator(mode="after")
    def unique_resources(self) -> PublicationCredentialEvidence:
        if len({resource.name for resource in self.resources}) != len(self.resources):
            raise ValueError("Credential evidence must contain unique resources")
        return self


def _credential_binding_documents(publication: GatewayPublication) -> dict[str, set[str]]:
    documents: dict[str, set[str]] = {}
    for binding in publication.desired_spec.bindings:
        sources: list[GatewayModelBinding | GatewayBackendPoolMember] = [binding]
        pool = binding.backend_pool
        if pool is not None:
            sources.extend(pool.members)
        for source in sources:
            if source.named_value_name is None:
                continue
            secret_id = (
                source.key_vault_secret_id if isinstance(source, GatewayModelBinding) else None
            )
            provider_id = source.provider_id if isinstance(source, GatewayModelBinding) else None
            documents.setdefault(source.named_value_name, set()).add(json.dumps({
                "name": source.named_value_name,
                "auth": source.auth_strategy.value,
                "key_vault_secret_id": str(secret_id) if secret_id is not None else None,
                "backend_url": str(source.backend_url or "").rstrip("/"),
                "runtime_id": str(source.runtime_id or ""),
                "provider_id": str(provider_id or ""),
            }, sort_keys=True, separators=(",", ":")))
    return documents


def _credential_binding_digest(
    publication: GatewayPublication, documents: set[str],
) -> str:
    payload = {"bindings": sorted(documents),
               "credential_generation": publication.resource_manifest.get("credential_generation")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def publication_credential_evidence(
    publication: GatewayPublication,
    states: Mapping[str, Literal["present", "missing", "unverified"]],
) -> dict[str, object]:
    references = _credential_binding_documents(publication)
    if not set(states).issubset(references):
        raise ValueError("Credential evidence refers to an unrelated binding")
    return PublicationCredentialEvidence(
        publication_id=publication.id, gateway_profile_id=publication.gateway_profile_id,
        resources=tuple(PublicationCredentialResource(
            name=name, binding_sha256=_credential_binding_digest(publication, references[name]),
            state=state,
        ) for name, state in sorted(states.items())),
    ).model_dump(mode="json")


def publication_credential_states(
    publication: GatewayPublication,
) -> dict[str, Literal["present", "missing", "unverified"]] | None:
    if "credential_evidence" not in publication.resource_manifest:
        return None
    try:
        evidence = PublicationCredentialEvidence.model_validate(
            publication.resource_manifest["credential_evidence"],
        )
    except ValidationError:
        return {}
    if (evidence.publication_id != publication.id
            or evidence.gateway_profile_id != publication.gateway_profile_id):
        return {}
    references = _credential_binding_documents(publication)
    return {resource.name: resource.state for resource in evidence.resources
            if resource.name in references
            and resource.binding_sha256 == _credential_binding_digest(
                publication, references[resource.name],
            )}


def publication_provisioning_completed(audit: Sequence[Mapping[str, object]]) -> bool:
    completed = False
    for event in audit:
        if event.get("to_status") == "queued":
            completed = False
        elif event.get("from_status") == "validating" and event.get("to_status") == "provisioning":
            completed = True
    return completed


def publication_missing_credential_source(publication: GatewayPublication) -> bool:
    states = publication_credential_states(publication)
    if states is not None and publication.desired_spec.bindings:
        reference = publication.desired_spec.bindings[-1].named_value_name
        return reference is not None and states.get(reference) == "missing"
    return (
        publication.status is PublicationStatus.FAILED
        and publication.error_code == "CredentialSourceMissingError"
    )


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
    directly_managed = (
        binding.auth_strategy
        in {
            AuthStrategy.NAMED_VALUE_BEARER,
            AuthStrategy.NAMED_VALUE_API_KEY,
        }
        and binding.key_vault_secret_id is None
    )
    if not directly_managed or binding.named_value_name is None:
        return []
    states = publication_credential_states(publication)
    if states is not None:
        return [name for name, state in states.items() if state == "present"]
    confirmed = publication.apim_revision is not None or provisioning_completed
    raw_materialized = publication.resource_manifest.get("named_values") if confirmed else None
    materialized = (
        [str(value) for value in raw_materialized]
        if isinstance(raw_materialized, list)
        else []
    )
    if publication_missing_credential_source(publication):
        return [reference for reference in materialized if reference != binding.named_value_name]
    if binding.named_value_name not in materialized and confirmed:
        materialized.append(binding.named_value_name)
    return materialized


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
    directly_managed = (
        binding.auth_strategy
        in {
            AuthStrategy.NAMED_VALUE_BEARER,
            AuthStrategy.NAMED_VALUE_API_KEY,
        }
        and binding.key_vault_secret_id is None
    )
    return directly_managed and binding.named_value_name not in (
        publication_materialized_named_values(publication)
    )


class GatewayPublicationFailure(StrictModel):
    code: Literal[
        "publisher_permissions_missing", "publisher_permissions_unverified",
        "publisher_authorization_failed", "parent_policy_changed", "credential_source_missing",
        "apim_infrastructure_upgrade_required",
        "publication_failed", "deployment_not_found", "deployment_metadata_unavailable",
        "upstream_request_failed",
    ]
    message: str
    stage: PublicationStatus | None = None
    request_id: UUID | None = None
    http_status: int | None = Field(default=None, strict=True, ge=100, le=599)
    missing_actions: list[str] = Field(default_factory=list)


def publication_failure(
    publication: GatewayPublication, *, stage: PublicationStatus | None = None,
) -> GatewayPublicationFailure | None:
    if publication.status is not PublicationStatus.FAILED:
        return None
    failure = GatewayPublicationFailure(
        code="publication_failed", message="Publication failed; review its diagnostic reference.",
        stage=stage,
    )
    if publication.error_code == "InfrastructureUpgradeRequiredError":
        return failure.model_copy(update={
            "code": "apim_infrastructure_upgrade_required",
            "message": (
                "The gateway infrastructure must be upgraded before publishing image models. "
                "Ask the deployment administrator to run the APIM infrastructure upgrade."
            ),
        })
    if publication_missing_credential_source(publication):
        return failure.model_copy(update={
            "code": "credential_source_missing",
            "message": "The APIM credential resource is missing; provide the API key to retry.",
        })
    message = publication.error_message or ""
    if not isinstance(message, str) or len(message) > 8192:
        return failure
    if message.startswith("Deployment validation failed: "):
        try:
            details = json.loads(message.split(": ", 1)[1])
        except (ValueError, TypeError):
            return failure
        if (not isinstance(details, dict)
                or details.get("code") not in {
                    "deployment_not_found", "deployment_metadata_unavailable",
                }
                or not isinstance(details.get("deployment"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", details["deployment"])
                or type(details.get("http_status")) is not int
                or not 100 <= details["http_status"] <= 599):
            return failure
        deployment = details["deployment"]
        reason = (f"Deployment '{deployment}' does not exist in the selected connection. "
                  "Use the deployment name, not the model name."
                  if details["code"] == "deployment_not_found" else
                  f"Deployment '{deployment}' could not be verified; "
                  f"the deployment directory returned HTTP {details['http_status']}.")
        return failure.model_copy(update={
            "code": details["code"], "message": reason, "http_status": details["http_status"],
        })
    probe = re.fullmatch(
        r"Route probe returned HTTP ([1-5][0-9]{2}); no request replayed", message,
    )
    if probe is not None:
        status = int(probe.group(1))
        return failure.model_copy(update={
            "code": "upstream_request_failed", "http_status": status,
            "message": f"Model validation returned HTTP {status}. "
            + ("Check the deployment name and endpoint. " if status == 404 else "")
            + "This request was not replayed.",
        })
    if message == "The current APIM policy changed after publication was queued":
        return failure.model_copy(update={
            "code": "parent_policy_changed",
            "message": (
                "The APIM parent policy differs from the queued snapshot; review before retrying."
            ),
        })
    preflight = message.startswith("Publisher permission preflight failed: ")
    management = message.startswith("APIM management failed: ")
    if not (preflight or management):
        return failure
    try:
        details = json.loads(message.split(": ", 1)[1])
    except (ValueError, TypeError):
        return failure
    if not isinstance(details, dict):
        return failure
    request_id = None
    try:
        if isinstance(details.get("request_id"), str):
            request_id = UUID(details["request_id"])
    except ValueError:
        pass
    if preflight:
        allowed = {
            *PUBLICATION_REQUIRED_ACTIONS, *PUBLICATION_PROBE_ACTIONS, *PUBLICATION_OAUTH_ACTIONS,
        }
        supplied = details.get("missing_actions", [])
        if not isinstance(supplied, list) or any(
            not isinstance(action, str) or action not in allowed for action in supplied
        ):
            return failure
        missing = sorted(set(supplied))
        code = details.get("code")
        if code == "publisher_permissions_missing" and missing:
            return failure.model_copy(update={
                "code": code, "request_id": request_id, "missing_actions": missing,
                "message": "The Publisher identity lacks required APIM publication permissions.",
            })
        if code == "publisher_permissions_unverified" and not missing:
            return failure.model_copy(update={
                "code": code, "request_id": request_id,
                "message": (
                    "The Publisher identity's APIM publication permissions could not be verified."
                ),
            })
    if management and details.get("code") == "AuthorizationFailed" and type(
        details.get("status")
    ) is int and details["status"] in {401, 403}:
        return failure.model_copy(update={
            "code": "publisher_authorization_failed", "request_id": request_id,
            "http_status": details["status"],
            "message": "Azure rejected the Publisher identity's APIM management request.",
        })
    return failure


class GatewayPublicationView(StrictModel):
    id: UUID
    gateway_profile_id: UUID
    target_deployments: tuple[GatewayServingDeployment, ...] | None = None
    generation: int
    publication_kind: PublicationKind
    model_key: str
    display_name: str
    status: PublicationStatus
    configuration_only: bool = Field(default=False, exclude_if=lambda value: value is False)
    error_code: str | None
    error_message: str | None
    failure: GatewayPublicationFailure | None = None
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
        failure = publication_failure(publication)
        probe_authorization = publication.resource_manifest.get("image_probe_authorization")
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
        target_deployments: tuple[GatewayServingDeployment, ...] | None = None
        if publication.publication_kind in {
            PublicationKind.MODEL_ADD, PublicationKind.CREDENTIAL_ROTATION,
        }:
            binding = publication.desired_spec.bindings[-1]
            target_deployments = (
                publication.desired_spec.gateway_deployments
                if binding.deployment_scope.mode is GatewayDeploymentScopeMode.ALL
                else binding.serving_deployments
            ) or None
        route_configuration = publication.resource_manifest.get("route_configuration")
        return cls(
            id=publication.id,
            gateway_profile_id=publication.gateway_profile_id,
            configuration_only=isinstance(route_configuration, dict)
            and route_configuration.get("configuration_only") is True,
            target_deployments=target_deployments,
            generation=publication.generation,
            publication_kind=publication.publication_kind,
            model_key=model_key,
            display_name=display_name,
            status=publication.status,
            error_code=(
                "apim_infrastructure_upgrade_required"
                if failure and failure.code == "apim_infrastructure_upgrade_required"
                else "publication_failed"
                if publication.status is PublicationStatus.FAILED
                else "provider_authorization_required"
                if publication.status is PublicationStatus.AWAITING_AUTHORIZATION
                else None
            ),
            error_message=(
                failure.message if failure and failure.code != "publication_failed" else None
            ),
            failure=failure,
            authorization=authorization,
            retry_can_authorize_image_probes=can_authorize_probes,
            retry_requires_credential=publication_retry_requires_credential(
                publication
            ),
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
    route_configuration_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    route_head_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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
        for field in ("route_configuration_sha256", "route_head_sha256"):
            if getattr(self, field) is None:
                result.pop(field, None)
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
    failure: GatewayPublicationFailure | None = None
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


class ApimOnboardingOperations(StrictModel):
    available: bool
    unavailable_reason: str | None = None
    items: list[GatewayReleaseOperation] = Field(default_factory=list)


class SelfHostedGatewayProvisionOperations(StrictModel):
    available: bool
    unavailable_reason: str | None = None
    items: list[GatewayReleaseOperation] = Field(default_factory=list)


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
