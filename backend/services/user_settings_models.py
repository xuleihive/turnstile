from __future__ import annotations

import unicodedata
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator

from turnstile_core.domain.models import StrictModel


class ProfileUpdate(StrictModel):
    display_name: str | None = Field(max_length=160)

    @field_validator("display_name", mode="before")
    @classmethod
    def clean_name(cls, value: object) -> object:
        if isinstance(value, str):
            if any(unicodedata.category(char) == "Cc" for char in value):
                raise ValueError("显示名称不能包含控制字符。")
            return value.strip() or None
        return value


class PasswordUpdate(StrictModel):
    current_password: SecretStr = Field(min_length=1, max_length=256)
    new_password: SecretStr = Field(min_length=12, max_length=256)
    confirm_password: SecretStr = Field(min_length=12, max_length=256)

    @model_validator(mode="after")
    def matching_passwords(self) -> PasswordUpdate:
        if self.new_password.get_secret_value() != self.confirm_password.get_secret_value():
            raise ValueError("两次输入的新密码不一致。")
        if self.new_password.get_secret_value() == self.current_password.get_secret_value():
            raise ValueError("新密码不能与当前密码相同。")
        return self


class AvatarUpdate(StrictModel):
    avatar_data_url: str | None = Field(max_length=90_000)


class PersonalAvatar(StrictModel):
    avatar_url: str | None
    updated_at: datetime | None


class AccountEntity(StrictModel):
    id: str
    name: str


class PermissionGroup(StrictModel):
    key: Literal["owner", "member"]
    name: str
    source: Literal["turnstile"] = "turnstile"


class EditableProfile(StrictModel):
    avatar: bool
    display_name: bool
    password: bool


class AccountInformation(StrictModel):
    account_id: UUID
    permission_groups: list[PermissionGroup]
    organization: AccountEntity | None
    department: AccountEntity | None
    membership_source: Literal[
        "catalog", "observed_usage", "owner_default", "unlinked", "directory"
    ]
    editable: EditableProfile
    generated_at: datetime


class PersonalBudget(StrictModel):
    period: str
    period_start: date
    period_end: date
    min_period: str
    max_period: str
    token_limit: int | None
    used_tokens: int
    remaining_tokens: int | None
    warning_threshold_percent: int | None
    usage_percent: float | None
    forecast_tokens: int
    forecast_percent: float | None
    status: Literal["unallocated", "healthy", "warning", "exceeded"]
    enforcement_mode: Literal["audit", "block"] | None
    enforcement_department_id: str | None
    basis: Literal["budget_evidence"] = "budget_evidence"
    generated_at: datetime


class PersonalModel(StrictModel):
    id: UUID
    name: str
    model_key: str
    provider_name: str
    context_window: int | None
    capabilities: list[str]
    access_source: Literal["explicit", "legacy_compatibility"]
    runtime_status: Literal["available", "unavailable", "unknown"]


class PersonalModelList(StrictModel):
    policy_state: Literal["unconfigured", "assigned", "deny_all"]
    items: list[PersonalModel]
    generated_at: datetime


class PersonalUsageTotals(StrictModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    error_rate: float | None = None
    average_latency_ms: float | None = None
    estimated_cost_usd: float | None = None
    cost_state: Literal["priced", "partial", "unpriced"] = "unpriced"


class PersonalUsagePoint(StrictModel):
    bucket_start: datetime
    totals: PersonalUsageTotals


class PersonalModelUsage(StrictModel):
    model_id: str
    model_name: str
    totals: PersonalUsageTotals


class PersonalUsage(StrictModel):
    period: str
    from_: datetime = Field(alias="from")
    to: datetime
    interval: Literal["hour", "day"]
    timezone: str
    usage_domain: Literal["apim"] = "apim"
    totals: PersonalUsageTotals
    points: list[PersonalUsagePoint]
    models: list[PersonalModelUsage]
    generated_at: datetime
    last_observed_at: datetime | None
    demo: bool = False
