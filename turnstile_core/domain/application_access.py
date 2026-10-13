from __future__ import annotations

import base64
import binascii
from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .models import StrictModel

ApplicationType = Literal["service", "agent", "delegated_user", "system"]
ApplicationStatus = Literal["active", "suspended", "retired"]
ApplicationSubscriptionState = Literal["active", "suspended", "cancelled"]
ApplicationScopeType = Literal["product", "api", "service"]
ApplicationSubscriptionSource = Literal["bicep", "discovered", "managed"]
ApplicationActorType = Literal["person", "service", "system"]
_APPLICATION_NAMESPACE = UUID("5fcb8381-d9fc-45bb-8de4-f6bc31d6d6a0")
_APPLICATION_AVATAR_MAX_BYTES = 64 * 1024
_APPLICATION_AVATAR_PREFIXES = {
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/webp": b"RIFF",
}


def application_id_for(gateway_profile_id: UUID, apim_subscription_id: str) -> UUID:
    return uuid5(
        _APPLICATION_NAMESPACE,
        f"application:{gateway_profile_id}:{apim_subscription_id.casefold()}",
    )


def application_subscription_id_for(gateway_profile_id: UUID, apim_subscription_id: str) -> UUID:
    return uuid5(
        _APPLICATION_NAMESPACE,
        f"subscription:{gateway_profile_id}:{apim_subscription_id.casefold()}",
    )


class GatewayApplicationUsage(StrictModel):
    request_count: int = Field(default=0, ge=0)
    denied_request_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost: float = Field(default=0, ge=0)
    last_request_at: datetime | None = None


class GatewayApplicationUserUsage(StrictModel):
    user_id: str
    display_name: str
    actor_type: ApplicationActorType
    person_id: str | None = None
    request_count: int = Field(ge=0)
    denied_request_count: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost: float = Field(ge=0)
    last_request_at: datetime


class GatewayApplicationBudget(StrictModel):
    period_start: date
    token_limit: int = Field(gt=0)
    tokens_per_minute: int = Field(gt=0)
    enforce: bool
    warning_threshold_percent: int = Field(ge=1, le=100)
    used_tokens: int = Field(default=0, ge=0)
    remaining_tokens: int = Field(ge=0)
    usage_percent: float = Field(ge=0)
    updated_by: str
    updated_at: datetime
    pending_reserved_tokens: int | None = Field(default=None, ge=0)
    pending_reservation_count: int | None = Field(default=None, ge=0)
    finalized_upper_bound_tokens: int | None = Field(default=None, ge=0)
    finalized_upper_bound_count: int | None = Field(default=None, ge=0)
    stale_reservation_count: int | None = Field(default=None, ge=0)
    oldest_reservation_at: datetime | None = None
    available_tokens: int | None = Field(default=None, ge=0)
    ledger_snapshot_at: datetime | None = None


class GatewayApplicationLedgerState(StrictModel):
    period_start: date
    application_id: UUID
    token_limit: int = Field(gt=0)
    confirmed_tokens: int = Field(ge=0)
    pending_reserved_tokens: int = Field(ge=0)
    pending_reservation_count: int = Field(ge=0)
    finalized_upper_bound_tokens: int = Field(ge=0)
    finalized_upper_bound_count: int = Field(ge=0)
    stale_reservation_count: int = Field(ge=0)
    oldest_reservation_at: AwareDatetime | None = None
    available_tokens: int = Field(ge=0)
    snapshot_at: AwareDatetime

    @model_validator(mode="after")
    def validate_balance(self) -> GatewayApplicationLedgerState:
        if self.period_start.day != 1:
            raise ValueError("ledger period must start on the first day of the month")
        if self.stale_reservation_count > self.pending_reservation_count:
            raise ValueError("stale count cannot exceed pending count")
        if (self.pending_reservation_count == 0) != (self.oldest_reservation_at is None):
            raise ValueError("pending reservations require an oldest timestamp")
        expected = max(
            self.token_limit
            - self.confirmed_tokens
            - self.pending_reserved_tokens
            - self.finalized_upper_bound_tokens,
            0,
        )
        if self.available_tokens != expected:
            raise ValueError("available balance must include pending and finalized reservations")
        return self


class GatewayApplicationBudgetUpdate(StrictModel):
    token_limit: int = Field(gt=0)
    tokens_per_minute: int = Field(gt=0)
    enforce: bool
    warning_threshold_percent: int = Field(ge=1, le=100)


class GatewayApplicationModelAccessUpdate(StrictModel):
    mode: Literal["unrestricted", "restricted"]
    model_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_model_ids(self) -> GatewayApplicationModelAccessUpdate:
        if len(self.model_ids) != len(set(self.model_ids)):
            raise ValueError("model_ids must be unique")
        if self.mode == "unrestricted" and self.model_ids:
            raise ValueError("unrestricted model access cannot include model_ids")
        return self


class GatewayApplicationSubscription(StrictModel):
    id: UUID
    application_id: UUID
    gateway_profile_id: UUID
    apim_subscription_id: str
    display_name: str
    scope_type: ApplicationScopeType
    scope_id: str
    state: ApplicationSubscriptionState
    scope_exists: bool
    source: ApplicationSubscriptionSource
    discovered_at: datetime
    last_synced_at: datetime


class GatewayUserSubscriptionEntry(StrictModel):
    id: UUID
    application_id: UUID
    application_display_name: str
    application_type: ApplicationType
    apim_subscription_id: str
    display_name: str
    scope_id: str
    state: ApplicationSubscriptionState
    source: ApplicationSubscriptionSource
    created_at: datetime


class GatewayUserSubscriptionList(StrictModel):
    user_id: UUID
    email: str
    display_name: str | None
    role: str
    items: list[GatewayUserSubscriptionEntry]


class GatewayUserSubscriptionCreate(StrictModel):
    display_name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    application_type: Literal["service", "agent"] = "service"

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name cannot be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class GatewayApplicationOwnerUpdate(StrictModel):
    user_id: UUID | None


class GatewayApplicationSubscriptionCancel(StrictModel):
    confirmation: str = Field(min_length=1, max_length=256)


class GatewaySubscriptionKeysExportFilter(StrictModel):
    user_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    department_id: UUID | None = None
    subscription_ids: list[str] = Field(default_factory=list, max_length=1000)


class GatewayApplicationSubscriptionKeySecret(StrictModel):
    key_kind: Literal["primary", "secondary"]
    value: str = Field(min_length=1, max_length=256, repr=False)


class GatewayApplicationSubscriptionKeyRotation(StrictModel):
    confirmation: str = Field(min_length=1, max_length=256)


class GatewayApplicationAuditEvent(StrictModel):
    id: UUID
    application_id: UUID
    operation: Literal[
        "created",
        "adopted",
        "updated",
        "budget_updated",
        "models_updated",
        "subscription_synced",
        "subscription_created",
        "subscription_cancelled",
        "owner_changed",
        "suspended",
        "resumed",
        "retired",
    ]
    before_state: dict[str, object] | None
    after_state: dict[str, object] | None
    actor: str
    created_at: datetime


def decode_application_avatar_data_url(value: str) -> tuple[str, bytes]:
    header, separator, encoded = value.partition(",")
    if not separator or not header.startswith("data:") or not header.endswith(";base64"):
        raise ValueError("avatar_data_url must be a base64 image data URL")
    media_type = header[5:-7].casefold()
    prefix = _APPLICATION_AVATAR_PREFIXES.get(media_type)
    if prefix is None:
        raise ValueError("avatar must be a PNG, JPEG, or WebP image")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("avatar_data_url contains invalid base64") from error
    if not image_bytes or len(image_bytes) > _APPLICATION_AVATAR_MAX_BYTES:
        raise ValueError("avatar image must be between 1 byte and 64 KiB")
    if not image_bytes.startswith(prefix) or (
        media_type == "image/webp" and (len(image_bytes) < 12 or image_bytes[8:12] != b"WEBP")
    ):
        raise ValueError("avatar image bytes do not match the declared media type")
    return media_type, image_bytes


class GatewayApplicationAvatarUpdate(StrictModel):
    avatar_data_url: str | None = Field(default=None, max_length=90_000)

    @field_validator("avatar_data_url")
    @classmethod
    def validate_avatar_data_url(cls, value: str | None) -> str | None:
        if value is not None:
            decode_application_avatar_data_url(value)
        return value


class GatewayApplicationAvatar(StrictModel):
    avatar_url: str | None
    updated_at: datetime | None


class GatewayApplicationSummary(StrictModel):
    id: UUID
    gateway_profile_id: UUID
    slug: str
    display_name: str
    description: str | None
    owner_id: str | None
    owner_user_id: UUID | None = None
    tenant_id: UUID | None = None
    organization_unit_id: UUID | None = None
    department_id: str | None
    application_type: ApplicationType
    status: ApplicationStatus
    system_managed: bool
    avatar_url: str | None = None
    governance_write_available: bool = True
    subscription_count: int = Field(ge=0)
    active_subscription_count: int = Field(ge=0)
    stale_subscription_count: int = Field(ge=0)
    model_policy_configured: bool
    allowed_model_ids: list[UUID] = Field(default_factory=list)
    budget: GatewayApplicationBudget | None
    usage: GatewayApplicationUsage
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class GatewayApplicationDetail(GatewayApplicationSummary):
    subscriptions: list[GatewayApplicationSubscription]
    users: list[GatewayApplicationUserUsage]
    user_count: int = Field(ge=0)
    audit: list[GatewayApplicationAuditEvent]
    key_management_available: bool
    key_management_unavailable_reason: str | None = None


class GatewayApplicationProvisioningDefaults(StrictModel):
    monthly_token_limit: int = Field(gt=0)
    tokens_per_minute: int = Field(gt=0)


class GatewayApplicationList(StrictModel):
    items: list[GatewayApplicationSummary]
    period_start: date
    total: int = Field(ge=0)
    active: int = Field(ge=0)
    suspended: int = Field(ge=0)
    retired: int = Field(ge=0)
    stale_subscriptions: int = Field(ge=0)
    sync_available: bool
    sync_unavailable_reason: str | None = None
    provisioning_available: bool
    provisioning_unavailable_reason: str | None = None
    provisioning_defaults: GatewayApplicationProvisioningDefaults | None = None
    key_management_available: bool
    key_management_unavailable_reason: str | None = None


class GatewayApplicationSubscriptionCreate(StrictModel):
    subscription_id: str = Field(
        min_length=1,
        max_length=127,
        pattern=r"^[a-z0-9][a-z0-9-]{0,126}$",
    )
    display_name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    application_type: Literal["service", "agent"] = "service"

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name cannot be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class GatewayApplicationSubscriptionProvisionSpec(StrictModel):
    provisioning_version: Literal[1, 2] = 1
    gateway_profile_id: UUID | None = None
    initial_monthly_token_limit: int | None = Field(default=None, gt=0)
    initial_tokens_per_minute: int | None = Field(default=None, gt=0)
    application_id: UUID
    application_subscription_id: UUID
    apim_subscription_id: str = Field(
        min_length=1,
        max_length=127,
        pattern=r"^[a-z0-9][a-z0-9-]{0,126}$",
    )
    slug: str = Field(
        min_length=1,
        max_length=127,
        pattern=r"^[a-z0-9][a-z0-9-]{0,126}$",
    )
    display_name: str = Field(min_length=1, max_length=100)
    application_display_name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    owner_user_id: UUID | None = None
    owner_email: str | None = Field(default=None, max_length=320)
    tenant_id: UUID | None = None
    organization_unit_id: UUID | None = None
    application_type: Literal["service", "agent"] = "service"
    scope_type: Literal["product"] = "product"
    scope_id: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def require_staged_identity_and_limits(self) -> GatewayApplicationSubscriptionProvisionSpec:
        if self.provisioning_version == 2 and any(value is None for value in (
            self.gateway_profile_id,
            self.initial_monthly_token_limit,
            self.initial_tokens_per_minute,
        )):
            raise ValueError("Staged provisioning requires a gateway and snapshotted budget limits")
        if self.owner_user_id is not None and self.tenant_id is None:
            raise ValueError("Owned applications require a tenant ID")
        return self


class GatewayApplicationDiscoveryItem(StrictModel):
    apim_subscription_id: str
    display_name: str
    state: ApplicationSubscriptionState
    scope_type: ApplicationScopeType
    scope_id: str
    scope_exists: bool
    application_type: ApplicationType
    system_managed: bool

    @model_validator(mode="after")
    def system_type_matches_management(self) -> GatewayApplicationDiscoveryItem:
        if self.system_managed != (self.application_type == "system"):
            raise ValueError("system-managed subscriptions must be system applications")
        return self


class GatewayApplicationDiscovery(StrictModel):
    gateway_profile_id: UUID
    items: list[GatewayApplicationDiscoveryItem]
    discovered_at: datetime


class UsageApplicationAttribution(StrictModel):
    application_id: UUID
    application_subscription_id: UUID
    application_name_snapshot: str
    apim_subscription_id: str
    actor_type: ApplicationActorType
    actor_id: str
    person_id: str | None = None
    application_admission: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def person_actor_requires_person(self) -> UsageApplicationAttribution:
        if (self.actor_type == "person") != (self.person_id is not None):
            raise ValueError("person_id is required only for person actors")
        return self
