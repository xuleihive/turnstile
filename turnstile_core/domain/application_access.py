from __future__ import annotations

import base64
import binascii
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, field_validator, model_validator

from .models import EnterpriseEntityCatalog, StrictModel

ApplicationType = Literal["service", "agent", "delegated_user", "system"]
ApplicationStatus = Literal["active", "suspended", "retired"]
ApplicationSubscriptionState = Literal["active", "suspended", "cancelled"]
ApplicationScopeType = Literal["product", "api", "service"]
ApplicationSubscriptionSource = Literal["bicep", "discovered", "managed"]
ApplicationActorType = Literal["person", "service", "system"]
MAX_TOKENS_PER_MINUTE = 2_147_483_647
TokensPerMinute = Annotated[int, Field(strict=True, gt=0, le=MAX_TOKENS_PER_MINUTE)]
_APPLICATION_NAMESPACE = UUID("5fcb8381-d9fc-45bb-8de4-f6bc31d6d6a0")
_APPLICATION_AVATAR_MAX_BYTES = 64 * 1024
_APPLICATION_AVATAR_PREFIXES = {
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/webp": b"RIFF",
}


def application_id_for(
    gateway_profile_id: UUID, apim_subscription_id: str
) -> UUID:
    return uuid5(
        _APPLICATION_NAMESPACE,
        f"application:{gateway_profile_id}:{apim_subscription_id.casefold()}",
    )


def application_subscription_id_for(
    gateway_profile_id: UUID, apim_subscription_id: str
) -> UUID:
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
    usd_cost: Decimal | None = Field(default=None, ge=0, max_digits=30, decimal_places=14)
    unpriced_usage_count: int = Field(default=0, ge=0)
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
    budget_mode: Literal["tokens", "usd"] = "tokens"
    token_limit: int | None = Field(default=None, ge=0)
    usd_limit: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    tokens_per_minute: int = Field(gt=0)
    enforce: bool
    warning_threshold_percent: int = Field(ge=1, le=100)
    used_tokens: int = Field(default=0, ge=0)
    remaining_tokens: int | None = Field(default=None, ge=0)
    used_usd: Decimal | None = Field(default=None, ge=0, max_digits=30, decimal_places=14)
    remaining_usd: Decimal | None = Field(default=None, ge=0, max_digits=30, decimal_places=14)
    usd_cost_available: bool | None = None
    usage_percent: float = Field(ge=0)
    pending_reserved_tokens: int | None = Field(default=None, ge=0)
    finalized_upper_bound_tokens: int | None = Field(default=None, ge=0)
    finalized_upper_bound_count: int | None = Field(default=None, ge=0)
    pending_reservation_count: int | None = Field(default=None, ge=0)
    stale_reservation_count: int | None = Field(default=None, ge=0)
    oldest_reservation_at: datetime | None = None
    available_tokens: int | None = Field(default=None, ge=0)
    ledger_snapshot_at: datetime | None = None
    ledger_source: Literal["snapshot", "execution"] = "snapshot"
    ledger_status: Literal["snapshot", "current", "unavailable", "prepared"] = "snapshot"
    ledger_read_at: datetime | None = None
    ledger_version: int | None = Field(default=None, ge=0)
    ledger_balance_tokens: int | None = None
    pending_reserved_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=30, decimal_places=14,
    )
    finalized_upper_bound_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=30, decimal_places=14,
    )
    policy_pending: bool = False
    projected_tokens_per_minute: TokensPerMinute | None = None
    tpm_sync_status: Literal["projected", "pending", "unavailable"] = "unavailable"
    updated_by: str
    updated_at: datetime


class GatewayApplicationBudgetUpdate(StrictModel):
    budget_mode: Literal["tokens", "usd"] = "tokens"
    token_limit: int | None = Field(default=None, gt=0)
    usd_limit: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    tokens_per_minute: TokensPerMinute
    enforce: bool
    warning_threshold_percent: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def require_mode_limit(self) -> GatewayApplicationBudgetUpdate:
        if self.budget_mode == "tokens":
            if self.token_limit is None or self.usd_limit is not None:
                raise ValueError("Token budgets require only token_limit")
        elif self.usd_limit is None or self.token_limit is not None:
            raise ValueError("USD budgets require only usd_limit")
        return self


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
        media_type == "image/webp"
        and (len(image_bytes) < 12 or image_bytes[8:12] != b"WEBP")
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


class GatewayApplicationOrganizationUpdate(StrictModel):
    organization_id: str | None = Field(default=None, min_length=1, max_length=255)
    department_id: str | None = Field(default=None, min_length=1, max_length=255)
    person_id: str | None = Field(default=None, min_length=1, max_length=255)


class GatewayApplicationOrganization(StrictModel):
    organization_id: str | None
    organization_name: str | None
    department_id: str | None = None
    department_name: str | None = None
    person_id: str | None = None
    person_name: str | None = None
    projected: bool = False


def application_organization(
    tags: dict[str, str], department_id: str | None = None,
    *, person_id: str | None = None, catalog: EnterpriseEntityCatalog | None = None,
) -> GatewayApplicationOrganization:
    from .enterprise import enterprise_catalog

    organization_id = tags.get("organization_id")
    if person_id is not None and department_id is None:
        raise ValueError("Application person requires a department")
    if organization_id is None:
        if department_id is not None:
            raise ValueError("Application department requires an organization")
        return GatewayApplicationOrganization(organization_id=None, organization_name=None)
    catalog = catalog if catalog is not None else enterprise_catalog()
    organization = next(
        (item for item in catalog.organizations if item.id == organization_id), None,
    )
    if organization is None:
        raise ValueError("Unknown application organization")
    department = next(
        (item for item in catalog.departments if item.id == department_id), None,
    ) if department_id is not None else None
    if department_id is not None:
        if department is None:
            raise ValueError("Unknown application department")
        if department.parent_id != organization.id:
            raise ValueError("Application department does not belong to the selected organization")
    person = next((item for item in catalog.users if item.id == person_id), None)
    if person_id is not None:
        if person is None:
            raise ValueError("Unknown application person")
        if person.parent_id != department_id:
            raise ValueError("Application person does not belong to the selected department")
    return GatewayApplicationOrganization(
        organization_id=organization.id, organization_name=organization.name,
        department_id=department.id if department is not None else None,
        department_name=department.name if department is not None else None,
        person_id=person.id if person is not None else None,
        person_name=person.name if person is not None else None,
    )


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
    department_id: str | None
    application_type: ApplicationType
    status: ApplicationStatus
    system_managed: bool
    tags: dict[str, str] = Field(default_factory=dict, exclude_if=lambda value: not value)
    avatar_url: str | None = None
    organization_id: str | None = None
    organization_name: str | None = None
    organization_assignment_supported: bool = False
    department_name: str | None = None
    department_assignment_supported: bool = False
    person_id: str | None = None
    person_name: str | None = None
    person_assignment_supported: bool = False
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


class GatewayApplicationBudgetProjection(StrictModel):
    status: Literal["projected", "pending", "unavailable"]
    tokens_per_minute: int | None = None
    configuration_updated_at: datetime | None = None


class GatewayApplicationDetail(GatewayApplicationSummary):
    subscriptions: list[GatewayApplicationSubscription]
    users: list[GatewayApplicationUserUsage]
    user_count: int = Field(ge=0)
    audit: list[GatewayApplicationAuditEvent]
    key_management_available: bool
    key_management_unavailable_reason: str | None = None
    budget_projection: GatewayApplicationBudgetProjection | None = None


class GatewayApplicationProvisioningDefaults(StrictModel):
    monthly_token_limit: int = Field(gt=0)
    tokens_per_minute: TokensPerMinute


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
    provisioning_options_supported: bool = False
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
    budget_mode: Literal["tokens", "usd"] = "tokens"
    token_limit: int | None = Field(default=None, strict=True, gt=0)
    usd_limit: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    tokens_per_minute: TokensPerMinute | None = None
    tags: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() or len(key) > 128 or len(tag) > 256
               for key, tag in value.items()):
            raise ValueError("Tags require nonblank keys up to 128 and values up to 256 characters")
        return value

    @model_validator(mode="after")
    def require_budget_unit(self) -> GatewayApplicationSubscriptionCreate:
        if self.budget_mode == "tokens":
            if self.usd_limit is not None:
                raise ValueError("Token budgets cannot include usd_limit")
        elif self.usd_limit is None or self.token_limit is not None:
            raise ValueError("USD budgets require only usd_limit")
        return self

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
    # Version 1 is retained for queued historical operations; new creates are staged.
    provisioning_version: Literal[1, 2] = 1
    gateway_profile_id: UUID | None = None
    initial_monthly_token_limit: int | None = Field(default=None, gt=0)
    initial_tokens_per_minute: TokensPerMinute | None = None
    budget_mode: Literal["tokens", "usd"] = Field(
        default="tokens", exclude_if=lambda value: value == "tokens",
    )
    initial_usd_limit: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=2,
        exclude_if=lambda value: value is None,
    )
    tags: dict[str, str] = Field(
        default_factory=dict, max_length=32, exclude_if=lambda value: not value,
    )
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
    description: str | None = Field(default=None, max_length=1000)
    application_type: Literal["service", "agent"] = "service"
    scope_type: Literal["product"] = "product"
    scope_id: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def require_staged_identity_and_limits(self) -> GatewayApplicationSubscriptionProvisionSpec:
        if self.provisioning_version == 2 and any(
            value is None for value in (
                self.gateway_profile_id,
                self.initial_usd_limit
                if self.budget_mode == "usd" else self.initial_monthly_token_limit,
                self.initial_tokens_per_minute,
            )
        ):
            raise ValueError("Staged provisioning requires a gateway and snapshotted budget limits")
        if self.budget_mode == "usd" and (
            self.provisioning_version != 2 or self.initial_monthly_token_limit is not None
            or self.initial_usd_limit is None
        ):
            raise ValueError("USD provisioning requires a snapshotted USD budget")
        if self.budget_mode == "tokens" and self.initial_usd_limit is not None:
            raise ValueError("Token provisioning cannot include a USD budget")
        GatewayApplicationSubscriptionCreate.validate_tags(self.tags)
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


class GatewayApplicationLedgerState(StrictModel):
    period_start: date
    application_id: UUID
    budget_mode: Literal["tokens", "usd"] = "tokens"
    token_limit: int | None = Field(default=None, gt=0)
    usd_limit: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    confirmed_tokens: int = Field(ge=0)
    pending_reserved_tokens: int = Field(ge=0)
    pending_reservation_count: int = Field(ge=0)
    finalized_upper_bound_tokens: int = Field(default=0, ge=0)
    finalized_upper_bound_count: int = Field(default=0, ge=0)
    stale_reservation_count: int = Field(ge=0)
    oldest_reservation_at: datetime | None = None
    available_tokens: int | None = Field(default=None, ge=0)
    confirmed_usd: Decimal = Field(
        default=Decimal(0), ge=0, max_digits=30, decimal_places=14,
    )
    pending_reserved_usd: Decimal = Field(
        default=Decimal(0), ge=0, max_digits=30, decimal_places=14,
    )
    upper_bound_usd: Decimal = Field(
        default=Decimal(0), ge=0, max_digits=30, decimal_places=14,
    )
    snapshot_at: datetime

    @property
    def available_usd(self) -> Decimal | None:
        if self.budget_mode != "usd" or self.usd_limit is None:
            return None
        return (
            self.usd_limit - self.confirmed_usd
            - self.pending_reserved_usd - self.upper_bound_usd
        )

    @model_validator(mode="after")
    def validate_pending_state(self) -> GatewayApplicationLedgerState:
        if self.stale_reservation_count > self.pending_reservation_count:
            raise ValueError("stale reservations cannot exceed all pending reservations")
        if (self.pending_reservation_count == 0) != (
            self.oldest_reservation_at is None
        ):
            raise ValueError("oldest reservation is required exactly when pending rows exist")
        if self.budget_mode == "tokens":
            if self.token_limit is None or self.usd_limit is not None or any((
                self.confirmed_usd, self.pending_reserved_usd, self.upper_bound_usd,
            )):
                raise ValueError("Token ledger state has invalid USD fields")
            expected = max(
                self.token_limit - self.confirmed_tokens - self.pending_reserved_tokens
                - self.finalized_upper_bound_tokens,
                0,
            )
            if self.available_tokens != expected:
                raise ValueError("available tokens must match the APIM ledger calculation")
        elif (
            self.token_limit is not None or self.usd_limit is None
            or self.available_tokens not in {None, 0}
        ):
            raise ValueError("USD ledger state has invalid token fields")
        return self
