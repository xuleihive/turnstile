from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, HttpUrl, JsonValue, PrivateAttr, SecretStr, model_validator

from .gateway_targets import ApimTargetTopologyList
from .image_profiles import ImageGenerationLimits, ImageGenerationProfile
from .models import ModelPriceTier, StrictModel
from .models import PriceCatalogRuleSource as PriceCatalogRuleSource

ModelCapability = Literal[
    "chat", "tools", "vision", "reasoning", "streaming", "embeddings", "image_generation",
    "decision",
]
FOUNDRY_INFERENCE_RESOURCE = "https://ai.azure.com"
FOUNDRY_INFERENCE_ROLE_ID = "a97b65f3-24c7-4388-baec-2e87135dc908"
DATABRICKS_INFERENCE_RESOURCE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
DATABRICKS_ANTHROPIC_PATH = "/serving-endpoints/anthropic/v1/messages"


class ModelVendorKey(StrEnum):
    GENERIC = "generic"
    KIMI = "kimi"
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


def model_vendor_label(value: ModelVendorKey) -> str:
    return {
        ModelVendorKey.GENERIC: "Other provider",
        ModelVendorKey.KIMI: "Kimi",
        ModelVendorKey.DEEPSEEK: "DeepSeek",
        ModelVendorKey.OPENAI: "OpenAI",
        ModelVendorKey.ANTHROPIC: "Anthropic",
    }[value]


def default_capabilities() -> list[ModelCapability]:
    return ["chat"]


def foundry_runtime_name(account: str, project: str) -> str:
    prefix = "Microsoft Foundry "
    suffix = " via APIM"
    identity = f"{account}/{project}"
    maximum_identity_length = 160 - len(prefix) - len(suffix)
    if len(identity) > maximum_identity_length:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
        identity = f"{identity[: maximum_identity_length - len(digest) - 1]}-{digest}"
    return f"{prefix}{identity}{suffix}"


def openai_compatible_endpoint_values(value: str) -> tuple[str, str, str]:
    endpoint = urlsplit(value.strip())
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError(
            "The OpenAI-compatible Base URL must be HTTPS without credentials, query, or fragment"
        )
    path = endpoint.path.rstrip("/")
    chat_suffix = "/chat/completions"
    if path.casefold().endswith(chat_suffix):
        path = path[: -len(chat_suffix)].rstrip("/")
    origin = f"{endpoint.scheme}://{endpoint.netloc}"
    base_url = f"{origin}{path}"
    return base_url, origin, f"{path}{chat_suffix}"


def databricks_workspace_url(value: str) -> str:
    endpoint = urlsplit(value.strip())
    host = (endpoint.hostname or "").casefold()
    if (
        endpoint.scheme != "https"
        or not host.endswith(".azuredatabricks.net")
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.port is not None
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError(
            "Use the Azure Databricks HTTPS workspace URL without a path, port or credentials"
        )
    return f"https://{host}"


def databricks_runtime_name(workspace_url: str) -> str:
    identity = urlsplit(workspace_url).netloc
    prefix, suffix = "Azure Databricks ", " via APIM"
    maximum = 160 - len(prefix) - len(suffix)
    if len(identity) > maximum:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
        identity = f"{identity[: maximum - len(digest) - 1]}-{digest}"
    return f"{prefix}{identity}{suffix}"


def databricks_connection_config(workspace_url: str, principal_id: str) -> dict[str, Any]:
    workspace = databricks_workspace_url(workspace_url)
    return {
        "workspace_url": workspace,
        "path": "/v1/messages",
        "api_format": "anthropic_messages",
        "streaming_mode": "native",
        "control_plane_managed": True,
        "backend_url": workspace,
        "backend_path": DATABRICKS_ANTHROPIC_PATH,
        "auth_strategy": "managed_identity",
        "managed_identity_resource": DATABRICKS_INFERENCE_RESOURCE,
        "anthropic_version": "2023-06-01",
        "authorization": {
            "kind": "databricks_workspace",
            "principal_id": principal_id,
            "resource_endpoint": workspace,
            "role_name": "CAN_QUERY",
        },
    }


class OAuthClientCredentialsConfig(StrictModel):
    provider_id: str = Field(pattern=r"^turnstile-oauth-[a-f0-9]{32}$")
    authorization_id: Literal["connection"] = "connection"
    client_id: UUID
    token_url: HttpUrl
    scopes: Literal["all-apis"] = "all-apis"

    @model_validator(mode="after")
    def require_databricks_token_endpoint(self) -> OAuthClientCredentialsConfig:
        endpoint = urlsplit(str(self.token_url))
        workspace = databricks_workspace_url(f"{endpoint.scheme}://{endpoint.netloc}")
        if str(self.token_url) != f"{workspace}/oidc/v1/token":
            raise ValueError("Databricks OAuth requires the Workspace token endpoint")
        return self


def apply_databricks_adoption(
    runtime: dict[str, Any], binding_config: dict[str, Any] | None,
) -> dict[str, Any]:
    config = dict(runtime.get("config") or {})
    if (
        runtime.get("brand_key") == "azure_databricks" and binding_config
        and config.get("auth_strategy") == "oauth_client_credentials"
        and binding_config.get("auth_strategy") == "oauth_client_credentials"
    ):
        oauth = OAuthClientCredentialsConfig.model_validate(binding_config.get("oauth"))
        return {**runtime, "config": {
            **config, "oauth": oauth.model_dump(mode="json"), "credential_provisioned": True,
        }}
    if (
        runtime.get("brand_key") != "azure_databricks"
        or config.get("control_plane_managed")
        or not binding_config
        or binding_config.get("databricks_connection_adoption") is not True
    ):
        return runtime
    adopted = databricks_connection_config(
        binding_config["workspace_url"], binding_config["authorization"]["principal_id"],
    )
    return {**runtime, "config": {**config, **adopted}}


def openai_compatible_runtime_name(
    base_url: str,
    model_vendor: ModelVendorKey = ModelVendorKey.GENERIC,
) -> str:
    endpoint = urlsplit(base_url)
    prefix = f"{model_vendor_label(model_vendor)} "
    suffix = " via APIM"
    identity = f"{endpoint.netloc}{endpoint.path}"
    maximum_identity_length = 160 - len(prefix) - len(suffix)
    if len(identity) > maximum_identity_length:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
        identity = f"{identity[: maximum_identity_length - len(digest) - 1]}-{digest}"
    return f"{prefix}{identity}{suffix}"


class ProviderKind(StrEnum):
    GITHUB = "github"
    ANTHROPIC = "anthropic"
    MICROSOFT_FOUNDRY = "microsoft_foundry"
    OPENAI_COMPATIBLE = "openai_compatible"


class BrandKey(StrEnum):
    GENERIC = "generic"
    AMAZON_BEDROCK = "amazon_bedrock"
    ANTHROPIC = "anthropic"
    AZURE_DATABRICKS = "azure_databricks"
    GITHUB = "github"
    MICROSOFT = "microsoft"
    MICROSOFT_FOUNDRY = "microsoft_foundry"
    OPENAI = "openai"


class ModelFamilyKey(StrEnum):
    GENERIC = "generic"
    CLAUDE = "claude"
    COPILOT = "copilot"
    OPENAI = "openai"


class GatewayKind(StrEnum):
    APIM = "apim"
    LITELLM = "litellm"
    DIRECT = "direct"


class InvocationApiFormat(StrEnum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class RuntimeKind(StrEnum):
    COPILOT_CLI = "copilot_cli"
    FOUNDRY = "foundry"
    OPENAI_COMPATIBLE = "openai_compatible"


class ConnectionAuthMode(StrEnum):
    MANAGED_IDENTITY = "managed_identity"
    API_KEY = "api_key"
    OAUTH_M2M = "oauth_m2m"


class AuthType(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    AZURE_AD = "azure_ad"


class HealthStatus(StrEnum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class PriceSource(StrEnum):
    """Where a model's charged rates come from.

    `MANUAL` is what every model did before this existed: a person typed four numbers and they
    stay until that person changes them. The other two derive the rates as list price times
    discount and keep them current, which only works because the discount is stored separately
    -- a published list price is a fact, the discount is a contract term.
    """

    MANUAL = "manual"
    MODELS_DEV = "models_dev"
    AZURE_RETAIL = "azure_retail"
    ANTHROPIC = "anthropic"


class PriceSyncStatus(StrEnum):
    OK = "ok"
    UNMAPPED = "unmapped"
    STALE = "stale"
    REVIEW_NEEDED = "review_needed"
    # The model's pricing configuration changed between the plan and the write, so the result
    # was discarded. Distinct from `stale`: the source was read fine, the answer just no longer
    # applies to the row it was computed for.
    SUPERSEDED = "superseded"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    DEFERRED = "deferred"


class PriceCatalogInputRange(StrictModel):
    min_input_tokens: int = Field(ge=0, strict=True)
    max_input_tokens: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def require_ordered_bounds(self) -> PriceCatalogInputRange:
        if self.max_input_tokens is not None and self.max_input_tokens < self.min_input_tokens:
            raise ValueError("Context pricing bounds must be ordered")
        return self


class ContextPriceRule(StrictModel):
    short_context_max_tokens: int = Field(gt=0, le=9007199254740990, strict=True)
    note: str = Field(default="", max_length=500)
    source_url: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def normalize_rule(self) -> ContextPriceRule:
        self.note = self.note.strip()
        if self.source_url is not None:
            self.source_url = self.source_url.strip()
            parsed = urlsplit(self.source_url)
            if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(character.isspace() for character in self.source_url)):
                raise ValueError("The rule source must be an HTTPS URL without credentials")
            HttpUrl(self.source_url)
        return self

    @property
    def revision(self) -> str:
        return "sha256:" + hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class PriceCatalogContextTier(StrictModel):
    group_key: str = Field(min_length=1)
    model_label: str = Field(min_length=1)
    service_tier: str = ""
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    order: int = Field(ge=0, strict=True)
    input_range: PriceCatalogInputRange | None = None
    input_range_source: PriceCatalogRuleSource | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )


class PriceCatalogModel(StrictModel):
    """One priceable model in a vendor's published list, named the way that vendor names it."""

    key: str
    label: str
    product: str
    source: PriceSource
    context_tier: PriceCatalogContextTier | None = None
    text_to_image: bool = Field(default=False, exclude_if=lambda value: not value)


class PriceCatalogModelsResponse(StrictModel):
    models: list[PriceCatalogModel]
    # Named rather than silently omitted: a source that cannot be read right now is different
    # from a source with nothing in it, and only one of those is worth waiting out.
    unavailable: list[str] = Field(default_factory=list)


class PriceCatalogOption(StrictModel):
    """How one model is priced under one deployment shape.

    `regions` lists every region charging exactly these rates. `region_required` is false when
    the shape charges one figure everywhere -- Global always does -- because asking for a region
    that cannot change the answer is asking someone to guess at nothing.
    """

    reference: str
    deployment: str
    regions: list[str] = Field(default_factory=list)
    # Each region's own reference. The group is how the choice is shown; what gets stored is the
    # region the person picked, so a later price split follows their region and not the first one.
    references_by_region: dict[str, str] = Field(default_factory=dict)
    region_required: bool = False
    input_per_million: float | None = None
    output_per_million: float | None = None
    cached_per_million: float | None = None
    cache_write_per_million: float | None = None
    context_window: int | None = None


class PriceCatalogOptionsResponse(StrictModel):
    model_entry: PriceCatalogModel
    options: list[PriceCatalogOption] = Field(default_factory=list)
    complete: bool = True
    # Meters that mention this model but that the vocabulary could not read. Reported rather
    # than dropped: a parser meeting an unfamiliar naming convention should look like a gap,
    # not like a model with no published price.
    unreadable: list[str] = Field(default_factory=list)
    # Other meters that mention this model but price something else: a batch rate, a
    # fine-tuning rate, a per-hour reservation. Named so the answer is "that one prices
    # something else" rather than silence.
    other_meters: list[str] = Field(default_factory=list)
    note: str | None = None


class GatewayProfileWrite(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    implementation: GatewayKind
    base_url: str | None = Field(default=None, max_length=2000)
    auth_type: AuthType = AuthType.NONE
    credential: SecretStr | None = None
    enabled: bool = True
    is_default: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


class GatewayProfileNameWrite(StrictModel):
    name: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def normalize_name(self) -> GatewayProfileNameWrite:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("The display name cannot be blank")
        return self


class GatewayProfile(GatewayProfileWrite):
    id: UUID
    created_at: datetime
    updated_at: datetime
    credential: None = None
    credential_configured: bool
    credential_hint: str | None


class ProviderWrite(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    provider_kind: ProviderKind
    endpoint_url: str | None = Field(default=None, max_length=2000)
    auth_type: AuthType = AuthType.NONE
    credential: SecretStr | None = None
    enabled: bool = True
    brand_key: BrandKey = BrandKey.GENERIC
    config: dict[str, Any] = Field(default_factory=dict)


class Provider(ProviderWrite):
    id: UUID
    created_at: datetime
    updated_at: datetime
    credential: None = None
    credential_configured: bool
    credential_hint: str | None


class RuntimeWrite(StrictModel):
    provider_id: UUID
    gateway_profile_id: UUID | None = None
    name: str = Field(min_length=1, max_length=160)
    runtime_kind: RuntimeKind
    enabled: bool = True
    is_default: bool = False
    brand_key: BrandKey = BrandKey.GENERIC
    config: dict[str, Any] = Field(default_factory=dict)
    allowed_roles: list[str] = Field(default_factory=lambda: ["owner", "admin", "member"])

    price_discount_percent: float | None = Field(default=None, gt=0, le=100)


class ProviderTarget(StrictModel):
    existing_id: UUID | None = None
    template: Literal[
        "amazon_bedrock", "microsoft_foundry", "openai_compatible", "azure_databricks"
    ] | None = None

    @model_validator(mode="after")
    def require_existing_or_new_provider(self) -> ProviderTarget:
        if (self.existing_id is None) == (self.template is None):
            raise ValueError("select an existing provider or one provider template")
        return self


class ModelConnectionCreate(StrictModel):
    gateway_profile_id: UUID
    provider: ProviderTarget
    auth_mode: ConnectionAuthMode | None = None
    foundry_project_endpoint: HttpUrl | None = None
    foundry_inference_endpoint: HttpUrl | None = None
    bedrock_runtime_url: HttpUrl | None = None
    openai_base_url: HttpUrl | None = None
    databricks_workspace_url: HttpUrl | None = None
    oauth_client_id: UUID | None = None
    model_vendor: ModelVendorKey | None = None

    @model_validator(mode="after")
    def require_one_connection_shape(self) -> ModelConnectionCreate:
        has_foundry = self.foundry_project_endpoint is not None
        has_bedrock = self.bedrock_runtime_url is not None
        has_openai = self.openai_base_url is not None
        has_databricks = self.databricks_workspace_url is not None
        if sum((has_foundry, has_bedrock, has_openai, has_databricks)) != 1:
            raise ValueError("select exactly one supported provider connection")
        if self.provider.template == "azure_databricks" and not has_databricks:
            raise ValueError("a Databricks connection requires its Workspace URL")
        if has_databricks:
            if self.provider.template not in {None, "azure_databricks"}:
                raise ValueError("the Workspace URL requires a Databricks provider")
            if self.auth_mode not in {
                ConnectionAuthMode.MANAGED_IDENTITY, ConnectionAuthMode.OAUTH_M2M,
            }:
                raise ValueError("Databricks requires managed identity or OAuth M2M")
            if (self.auth_mode is ConnectionAuthMode.OAUTH_M2M) != (
                self.oauth_client_id is not None
            ):
                raise ValueError("Databricks OAuth M2M requires its service principal Client ID")
            if self.foundry_inference_endpoint is not None or self.model_vendor is not None:
                raise ValueError("a Databricks connection accepts only its workspace configuration")
            databricks_workspace_url(str(self.databricks_workspace_url))
            return self
        if self.oauth_client_id is not None or self.auth_mode is ConnectionAuthMode.OAUTH_M2M:
            raise ValueError("OAuth M2M belongs only to a Databricks connection")
        if has_bedrock:
            if (
                self.auth_mode is not None
                or self.foundry_inference_endpoint is not None
                or self.model_vendor is not None
            ):
                raise ValueError("a Bedrock connection accepts only its Runtime URL")
            return self
        if has_openai:
            if self.auth_mode is not None or self.foundry_inference_endpoint is not None:
                raise ValueError("an OpenAI-compatible connection accepts only its Base URL")
            return self
        if self.model_vendor is not None:
            raise ValueError("model_vendor belongs only to an OpenAI-compatible connection")
        if self.auth_mode is None:
            raise ValueError("a Foundry connection requires an authentication mode")
        if self.auth_mode is ConnectionAuthMode.MANAGED_IDENTITY:
            if self.foundry_inference_endpoint is not None:
                raise ValueError(
                    "a managed-identity connection does not use an inference endpoint"
                )
            return self
        if self.foundry_inference_endpoint is None:
            raise ValueError("an API-key connection requires an inference endpoint")
        return self


class DatabricksConnectionAdopt(StrictModel):
    workspace_url: HttpUrl

    @model_validator(mode="after")
    def require_workspace_origin(self) -> DatabricksConnectionAdopt:
        databricks_workspace_url(str(self.workspace_url))
        return self


class ModelConnectionUpdate(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    enabled: bool
    is_default: bool

    @model_validator(mode="after")
    def require_enabled_default(self) -> ModelConnectionUpdate:
        if self.is_default and not self.enabled:
            raise ValueError("a default connection must be enabled")
        return self

    price_discount_percent: float | None = Field(default=None, gt=0, le=100)


class Runtime(RuntimeWrite):
    id: UUID
    provider_name: str
    gateway_name: str | None
    health_status: HealthStatus
    health_message: str | None
    last_checked_at: datetime | None
    diagnostic: RuntimeHealth | None = None
    created_at: datetime
    updated_at: datetime


class PendingListPrice(StrictModel):
    """A published price waiting for a person, in the same four buckets as the accepted one."""

    input: float | None = None
    output: float | None = None
    cached: float | None = None
    cache_write: float | None = None


class StoredContextPriceTier(ModelPriceTier):
    reference: str = Field(min_length=1, max_length=1000)
    label: str = Field(min_length=1)
    list_prices: PendingListPrice
    input_range_source: PriceCatalogRuleSource | None = None
    model_key: str = ""
    model_label: str = ""
    product: str = ""
    group_key: str = ""
    service_tier: str = ""
    deployment: str = ""
    regions: list[str] = Field(default_factory=list)


def custom_context_rule(tiers: list[StoredContextPriceTier]) -> ContextPriceRule | None:
    sources = [tier.input_range_source for tier in tiers]
    if not any(source is not None and source.origin == "custom" for source in sources):
        return None
    ordered = sorted(tiers, key=lambda tier: tier.min_input_tokens)
    source = ordered[0].input_range_source
    if (len(ordered) != 2 or source is None or source.origin != "custom"
        or any(item != source for item in sources)
        or ordered[0].key != "short" or ordered[1].key != "long"
        or ordered[0].min_input_tokens != 0 or ordered[0].max_input_tokens is None
        or ordered[1].min_input_tokens != ordered[0].max_input_tokens + 1
        or ordered[1].max_input_tokens is not None or source.configured_at is None):
        raise ValueError("Stored custom context rules must describe one complete schedule")
    rule = ContextPriceRule(
        short_context_max_tokens=ordered[0].max_input_tokens,
        note=source.note or "", source_url=source.url,
    )
    if source.rule_id != "custom-context-v1" or source.revision != rule.revision:
        raise ValueError("Stored custom context rule revision does not match its contents")
    return rule


class ManagedModelWrite(StrictModel):
    provider_id: UUID
    runtime_id: UUID
    model_key: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    family_key: ModelFamilyKey = ModelFamilyKey.GENERIC
    upstream_model_id: str | None = Field(default=None, min_length=1, max_length=500)
    assignment_required: bool = False
    enabled: bool = True
    is_default: bool = False
    capabilities: list[ModelCapability] = Field(default_factory=default_capabilities)
    context_window: int | None = Field(default=None, ge=1)
    input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)
    # Providers bill cached input well below the base rate. None keeps the conservative fallback
    # of charging cached tokens at input_cost_per_million.
    cached_cost_per_million: float | None = Field(default=None, ge=0)
    # Cache writes are billed above the base input rate (Anthropic 1.25x for a 5-minute TTL,
    # OpenAI 1.25x on GPT-5.6 and later). None falls back to the cached rate.
    cache_write_cost_per_million: float | None = Field(default=None, ge=0)
    allowed_roles: list[str] = Field(default_factory=lambda: ["owner", "admin", "member"])
    price_entry_digest: str | None = Field(default=None, max_length=64)
    allow_unpriced: bool = False

    @model_validator(mode="after")
    def validate_image_model(self) -> ManagedModelWrite:
        if "image_generation" in self.capabilities:
            if set(self.capabilities) != {"image_generation"}:
                raise ValueError("Image generation cannot declare text or streaming capabilities")
            if self.is_default:
                raise ValueError("An image model cannot be the default chat model")
            if self.context_price_references:
                raise ValueError("Image models cannot use context-tier prices")
        return self

    @model_validator(mode="after")
    def validate_decision_model(self) -> ManagedModelWrite:
        if "decision" in self.capabilities:
            if set(self.capabilities) != {"decision"}:
                raise ValueError("Decision cannot declare chat or streaming capabilities")
            if self.is_default:
                raise ValueError("A Decision model cannot be the default chat model")
        return self

    price_source: PriceSource = PriceSource.MODELS_DEV

    price_reference: str | None = Field(default=None, min_length=1, max_length=1500)

    context_price_references: list[str] = Field(
        default_factory=list, max_length=32, exclude_if=lambda value: not value,
    )

    context_price_refresh: bool = Field(default=False, exclude_if=lambda value: not value)

    context_price_rule: ContextPriceRule | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    price_discount_percent: float | None = Field(default=None, gt=0, le=100)

    @model_validator(mode="after")
    def validate_price_source(self) -> ManagedModelWrite:
        if self.context_price_rule is not None and len(self.context_price_references) != 2:
            raise ValueError("A custom context rule requires both short and long catalog tiers")
        if self.context_price_refresh and not self.context_price_references:
            raise ValueError("Refreshing context prices requires the complete tier selection")
        if self.context_price_references:
            if self.price_source is PriceSource.MANUAL:
                raise ValueError("Context-tier references require a catalog price source")
            if (len(self.context_price_references) < 2
                or len(set(self.context_price_references)) != len(self.context_price_references)):
                raise ValueError("Context pricing requires distinct tier references")
            if any(not reference.startswith(f"{self.price_source.value}:")
                   for reference in self.context_price_references):
                raise ValueError("Context references must use the selected price source")
        elif self.price_source in {PriceSource.AZURE_RETAIL, PriceSource.ANTHROPIC} \
                and not self.price_reference:
            raise ValueError("Following a list price requires the catalog entry to follow")
        return self


class ManagedModel(ManagedModelWrite):
    id: UUID
    provider_name: str
    runtime_name: str
    publication_id: UUID | None = None
    image_profile: ImageGenerationProfile | None = None
    price_source_configured: bool = True
    price_config_updated_at: datetime | None = None
    match_metadata: dict[str, Any] | None = None
    source_snapshot: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    list_input_cost_per_million: float | None = None

    list_output_cost_per_million: float | None = None

    list_cached_cost_per_million: float | None = None

    list_cache_write_cost_per_million: float | None = None

    price_synced_at: datetime | None = None

    price_sync_status: PriceSyncStatus | None = None

    price_sync_message: str | None = None

    pending_list_price: PendingListPrice | None = None

    context_price_tiers: list[StoredContextPriceTier] = Field(default_factory=list, max_length=32)

    pending_context_price_tiers: list[StoredContextPriceTier] | None = None

    effective_discount_percent: float | None = None

    @model_validator(mode="before")
    @classmethod
    def restore_context_references(cls, values: Any) -> Any:
        if isinstance(values, dict) and values.get("context_price_tiers"):
            values = dict(values)
            tiers = [StoredContextPriceTier.model_validate(tier)
                     for tier in values["context_price_tiers"]]
            values["context_price_references"] = [tier.reference for tier in tiers]
            values["context_price_rule"] = custom_context_rule(tiers)
        return values


class PriceSyncRequest(StrictModel):
    """Which models to sync. Omitting the list syncs everything that follows a list price."""

    model_ids: list[UUID] | None = None
    refresh: bool = True
    dry_run: bool = False


class PricePreviewRequest(StrictModel):
    model_id: UUID | None = None
    runtime_id: UUID | None = None
    price_source: PriceSource = PriceSource.MODELS_DEV
    deployment_name: str | None = Field(default=None, max_length=500)
    upstream_model_id: str | None = Field(default=None, max_length=500)
    display_name: str | None = Field(default=None, max_length=255)
    model_key: str | None = Field(default=None, max_length=255)
    operation: Literal["chat", "image_generation", "decision"] = "chat"
    price_reference: str | None = Field(default=None, max_length=1500)
    price_discount_percent: float | None = Field(default=None, gt=0, le=100)
    refresh: bool = False
    rematch: bool = False


class PriceMatch(StrictModel):
    reference: str
    provider_id: str
    model_id: str
    name: str
    canonical_model_id: str | None = None
    method: Literal["exact", "ai", "manual", "stored"] = "exact"
    price_basis: Literal["deployment", "origin_reference"] = "deployment"
    reason: str | None = None
    default_model_id: UUID | None = None


class PricePreviewResponse(StrictModel):
    status: Literal["matched", "unmapped", "ambiguous", "stale", "unsupported", "deferred"]
    match: PriceMatch | None = None
    context_window: int | None = None
    list_prices: PendingListPrice | None = None
    effective_prices: PendingListPrice | None = None
    effective_discount_percent: float | None = None
    entry_digest: str | None = None
    catalog_snapshot_id: str | None = None
    fetched_at: datetime | None = None
    source_updated_at: str | None = None
    fresh: bool = False
    warnings: list[str] = Field(default_factory=list)
    candidates: list[PriceMatch] = Field(default_factory=list)


class PriceSyncDetail(StrictModel):
    model_id: UUID
    model_key: str
    status: PriceSyncStatus | None = None
    message: str | None = None
    outcome: str | None = None
    price_source: PriceSource | None = None
    price_reference: str | None = None


class RegistryResponse(StrictModel):
    gateway_name_edit_supported: bool = True
    runtime_diagnostics_supported: bool = False
    failed_publication_archive_supported: bool = False
    providers: list[Provider]
    gateways: list[GatewayProfile]
    runtimes: list[Runtime]
    models: list[ManagedModel]
    backend_pool_session_affinity_supported: bool = False
    databricks_connections_supported: bool = False
    databricks_oauth_supported: bool = False
    decision_supported: bool = Field(default=False, exclude_if=lambda value: not value)
    image_generation_supported: bool = False
    image_configuration_defaults: ImageGenerationLimits | None = None
    image_configuration_schema_version: Literal[4] = 4
    apim_target_topology_available: bool = False
    apim_target_topology: ApimTargetTopologyList | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    explicit_gateway_selection_supported: bool = False
    self_hosted_gateway_enabled: bool = False
    multi_apim_target_rollout_enabled: bool = False

    context_pricing_supported: bool = False
    context_price_rules_supported: bool = False


class PriceSyncResponse(StrictModel):
    """What a sync run did, reported per outcome rather than as a single count.

    `updated` counts rows whose charged rates changed. The rest is why the others did not, which
    is the part worth reading: a sync that silently prices nothing looks identical to one that
    prices everything unless the skips are named. The refreshed registry rides along so the
    dashboard can show the new rates without a second round trip.
    """

    considered: int
    updated: int
    unmapped: int
    review_needed: int
    stale: int
    # Planned a write, then found the model's pricing had been edited in the meantime and left
    # it alone. Counted separately from `stale`: the source was fine, the plan was not.
    superseded: int = 0
    total: int = 0
    unchanged: int = 0
    skipped_manual: int = 0
    ambiguous: int = 0
    unsupported: int = 0
    deferred: int = 0
    partial_fields: int = 0
    dry_run: bool = False
    details: list[PriceSyncDetail]
    registry: RegistryResponse


class RuntimeCheckSelection(StrictModel):
    model_id: UUID
    gateway_deployment_id: UUID


class RuntimeCheckOption(RuntimeCheckSelection):
    model_key: str
    model_name: str
    gateway_profile_id: UUID
    gateway_name: str
    native_gateway_id: str


class RuntimeDiagnosticAccess(StrictModel):
    status: Literal["ready", "missing_permissions", "unverified"] = "unverified"
    reason: Literal[
        "permission_missing", "permission_unverified", "identity_unavailable", "tenant_mismatch",
        "probe_subscription_unconfigured",
    ] | None = None
    scope: str
    api_principal_id: UUID | None = None
    tenant_id: UUID
    subscription_id: UUID
    resource_group: str
    apim_name: str
    probe_subscription_id: str
    required_actions: list[str]
    missing_actions: list[str] = Field(default_factory=list)
    checked_at: datetime
    template_path: Literal["infra/modules/runtime-diagnostic-rbac.bicep"] = (
        "infra/modules/runtime-diagnostic-rbac.bicep"
    )


class RuntimeCheckPlan(StrictModel):
    runtime_id: UUID
    options: list[RuntimeCheckOption]
    unavailable_reason: str | None = None
    access: RuntimeDiagnosticAccess | None = None
    max_output_tokens: Literal[64] = 64
    request_limit: Literal[1] = 1


class RuntimeHealth(StrictModel):
    runtime_id: UUID
    status: HealthStatus
    message: str
    checked_at: datetime
    check_kind: Literal["legacy", "model_inference"] = "legacy"
    outcome: Literal[
        "passed", "blocked", "configuration_error", "upstream_error", "inconclusive",
    ] | None = None
    selection: RuntimeCheckOption | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    http_status: int | None = None
    latency_ms: int | None = None
    error_code: str | None = None
    valid_until: datetime | None = Field(
        default=None, description="Legacy expiry; historical diagnostic outcomes do not expire.",
    )
    configuration_fingerprint: str | None = Field(
        default=None, max_length=80,
        description=(
            "Server-owned digest of the selected persisted call configuration, not secrets."
        ),
    )
    configuration_status: Literal["current", "changed", "unverified"] = Field(
        default="unverified",
        description=(
            "Configuration comparison only; not a current availability or permission claim."
        ),
    )
    configuration_checked_at: datetime | None = None


class InvocationMetadata(StrictModel):
    organization_id: str = Field(min_length=1, max_length=255)
    organization: str = Field(min_length=1, max_length=255)
    department_id: str = Field(min_length=1, max_length=255)
    department: str = Field(min_length=1, max_length=255)
    project_id: str = Field(min_length=1, max_length=255)
    project: str = Field(min_length=1, max_length=255)
    agent_id: str = Field(min_length=1, max_length=255)
    agent: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=255)
    user: str = Field(min_length=1, max_length=255)
    workflow: str = Field(min_length=1, max_length=255)
    model_id: str = Field(min_length=1, max_length=255)
    model: str = Field(min_length=1, max_length=255)
    runtime: str = Field(min_length=1, max_length=255)
    request_source: str = Field(min_length=1, max_length=255)
    usage_domain: Literal["apim", "github_copilot"] = "apim"
    run_id: str = Field(min_length=1, max_length=255)
    turn_index: int = Field(default=1, ge=1)


class ToolFunctionCall(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(default="{}", max_length=100_000)


class ToolCall(StrictModel):
    id: str = Field(min_length=1, max_length=255)
    type: Literal["function"] = "function"
    function: ToolFunctionCall


class ToolFunctionDefinition(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=4_000)
    parameters: dict[str, Any]


class ToolDefinition(StrictModel):
    type: Literal["function"] = "function"
    function: ToolFunctionDefinition


class ChatMessage(StrictModel):
    """One turn in an OpenAI-shaped conversation.

    `content` is optional because an assistant turn that only calls tools carries no
    text, and a `tool` turn carries the result of exactly one call. The validator below
    is what keeps that from degrading into "any field may be missing".
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = Field(default=None, max_length=200_000)
    tool_calls: list[ToolCall] | None = Field(default=None, max_length=20)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=255)
    # Opaque reasoning and output items live only within the current tool loop.
    _responses_output: list[dict[str, Any]] | None = PrivateAttr(default=None)
    _anthropic_content: list[dict[str, Any]] | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def check_shape_matches_role(self) -> ChatMessage:
        if self.role == "tool":
            if not self.tool_call_id:
                raise ValueError("a tool message must name the call it answers")
            if not self.content:
                raise ValueError("a tool message must carry the tool result")
            return self
        if self.tool_call_id is not None:
            raise ValueError("tool_call_id belongs only to a tool message")
        if self.role == "assistant":
            if not self.content and not self.tool_calls:
                raise ValueError("an assistant message must carry content or tool calls")
            return self
        if self.tool_calls is not None:
            raise ValueError("only an assistant message may carry tool calls")
        if not self.content:
            raise ValueError(f"a {self.role} message must carry content")
        return self


class NoulQuestion(StrictModel):
    type: Literal["noul"]
    instructions: str = Field(min_length=1)
    criteria: dict[Literal["true", "false"], str] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )


class ChoiceQuestion(StrictModel):
    type: Literal["choice"]
    instructions: str = Field(min_length=1)
    criteria: dict[str, Annotated[str, Field(min_length=1)]] = Field(min_length=2)


class ScoreQuestion(StrictModel):
    type: Literal["score"]
    instructions: str = Field(min_length=1)
    criteria: list[Annotated[str, Field(min_length=1)]] = Field(min_length=2)


DecisionQuestion = Annotated[
    NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type"),
]


class DecisionInput(StrictModel):
    state: JsonValue
    questions: dict[str, DecisionQuestion] = Field(min_length=1)


class DecisionRequest(DecisionInput):
    model: str = Field(min_length=1, max_length=500)


class DecisionInvocationRequest(DecisionInput):
    metadata: InvocationMetadata
    model_id: UUID
    runtime_id: UUID | None = None
    gateway_profile_id: UUID | None = None
    gateway_deployment_id: UUID | None = None

    @model_validator(mode="after")
    def validate_gateway_selection(self) -> DecisionInvocationRequest:
        if (self.gateway_profile_id is None) != (self.gateway_deployment_id is None):
            raise ValueError("APIM instance and Gateway must be selected together")
        return self


class ModelInvocationRequest(StrictModel):
    metadata: InvocationMetadata
    runtime_id: UUID | None = None
    model_id: UUID | None = None
    gateway_profile_id: UUID | None = None
    gateway_deployment_id: UUID | None = None
    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    tools: list[ToolDefinition] | None = Field(default=None, max_length=32)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    response_format: Literal["json_object"] | None = None
    stream: bool = False
    api_format: InvocationApiFormat | None = None

    @model_validator(mode="after")
    def validate_gateway_selection(self) -> ModelInvocationRequest:
        if (self.gateway_profile_id is None) != (self.gateway_deployment_id is None):
            raise ValueError("APIM instance and Gateway must be selected together")
        if self.gateway_profile_id is not None and self.model_id is None:
            raise ValueError(
                "Gateway selection requires a specific model without automatic routing"
            )
        return self


class InvocationUsage(StrictModel):
    input_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(ge=0)
    estimated: bool

    @model_validator(mode="after")
    def cache_write_is_a_subset(self) -> InvocationUsage:
        if self.cache_write_tokens > self.cached_tokens:
            raise ValueError("cache_write_tokens cannot exceed cached_tokens")
        return self


class ModelInvocationResponse(StrictModel):
    request_id: str
    correlation_id: str | None = None
    upstream_request_id: str | None = None
    content: str
    tool_calls: list[ToolCall] | None = None
    provider: str
    runtime: str
    model: str
    gateway: str
    gateway_profile_id: UUID | None = None
    gateway_deployment_id: UUID | None = None
    native_gateway_id: str | None = None
    latency_ms: int = Field(ge=0)
    usage: InvocationUsage | None
    estimated_cost: float | None = Field(default=None, ge=0)
    selected_model_id: UUID | None = None
    selected_model_name: str | None = None
    _responses_output: list[dict[str, Any]] | None = PrivateAttr(default=None)
    _anthropic_content: list[dict[str, Any]] | None = PrivateAttr(default=None)

    def assistant_message(self) -> ChatMessage:
        message = ChatMessage(
            role="assistant",
            content=self.content or None,
            tool_calls=self.tool_calls,
        )
        message._responses_output = self._responses_output
        message._anthropic_content = self._anthropic_content
        return message


class DecisionInvocationResponse(StrictModel):
    request_id: str
    correlation_id: str | None = None
    upstream_request_id: str | None = None
    answers: dict[str, JsonValue]
    provider: str
    runtime: str
    model: str
    gateway: str
    gateway_profile_id: UUID | None = None
    gateway_deployment_id: UUID | None = None
    native_gateway_id: str | None = None
    latency_ms: int = Field(ge=0)
    usage: InvocationUsage | None
    estimated_cost: float | None = Field(default=None, ge=0)


class PlatformInvocationAttempt(StrictModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=512)
    started_at: datetime
    metadata: InvocationMetadata
    model_id: UUID
    model_key: str
    model_name: str
    runtime_id: UUID
    runtime_name: str
    provider_name: str
    apim_target_id: UUID | None = None
    gateway_deployment_id: UUID | None = None
    native_gateway_id: str | None = None
    apim_service_id: str | None = None
    application_id: UUID | None = None


class PlatformInvocationCompletion(StrictModel):
    request_id: str = Field(min_length=1, max_length=512)
    completed_at: datetime
    outcome: Literal["succeeded", "failed"]
    status_code: int = Field(ge=100, le=599)
    latency_ms: int = Field(ge=0)
    gateway_correlation_id: str | None = Field(default=None, min_length=1, max_length=512)
    upstream_request_id: str | None = Field(default=None, min_length=1, max_length=512)
    failure_kind: (
        Literal[
            "gateway_error",
            "http_error",
            "timeout",
            "transport_error",
            "invalid_response",
            "internal_error",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> PlatformInvocationCompletion:
        if self.outcome == "failed" and self.failure_kind is None:
            raise ValueError("A failed invocation requires a failure kind")
        if self.outcome == "succeeded" and (
            self.failure_kind is not None or not 200 <= self.status_code < 300
        ):
            raise ValueError("A successful invocation requires a successful response")
        return self


class TrafficGenerationRequest(StrictModel):
    model_ids: list[UUID] = Field(default_factory=list, max_length=20)
    requests_per_model: int = Field(default=4, ge=1, le=100)
    max_output_tokens: int = Field(default=48, ge=1, le=512)
    budget_usd: float = Field(default=5.0, gt=0, le=20)
    dry_run: bool = True


class TrafficPlanItem(StrictModel):
    sequence: int = Field(ge=1)
    model_id: UUID
    model_name: str
    organization_id: str
    organization_name: str
    department_id: str
    department_name: str
    project_id: str
    project_name: str
    agent_id: str
    agent_name: str
    user_id: str
    user_name: str
    workflow: str
    prompt: str
    max_output_tokens: int = Field(ge=1)
    conservative_cost_ceiling: float = Field(ge=0)


class TrafficGenerationPlan(StrictModel):
    dry_run: bool
    request_count: int = Field(ge=0)
    budget_usd: float = Field(gt=0)
    conservative_cost_ceiling: float = Field(ge=0)
    items: list[TrafficPlanItem]


class TrafficExecutionItem(StrictModel):
    sequence: int = Field(ge=1)
    model_id: UUID
    request_id: str | None = None
    correlation_id: str | None = None
    status_code: int = Field(ge=100, le=599)
    estimated_cost: float = Field(ge=0)
    error_message: str | None = None


class TrafficGenerationResult(StrictModel):
    planned_requests: int = Field(ge=0)
    completed_requests: int = Field(ge=0)
    successful_requests: int = Field(ge=0)
    failed_requests: int = Field(ge=0)
    total_estimated_cost: float = Field(ge=0)
    stopped_reason: str | None = None
    items: list[TrafficExecutionItem]
