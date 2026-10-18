from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, HttpUrl, model_validator

from .ledger import LedgerAttempt, LedgerSummary
from .models import StrictModel
from .runtime_models import ModelInvocationResponse


class BudgetCapabilities(StrictModel):
    token_available: bool = True
    usd_available: bool
    reason_code: str
    message: str


class BudgetDelegationTarget(StrictModel):
    audience: str = Field(min_length=1, max_length=2048)
    allowed_origins: tuple[HttpUrl, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_target(self) -> BudgetDelegationTarget:
        if any(character.isspace() for character in self.audience):
            raise ValueError("Delegation requires one exact token audience")
        for origin in self.allowed_origins:
            if (
                origin.scheme != "https"
                or origin.username
                or origin.password
                or origin.path not in {None, "/"}
                or origin.query
                or origin.fragment
            ):
                raise ValueError("Delegation requires exact credential-free HTTPS origins")
        return self


class BudgetDelegationTrust(StrictModel):
    tenant_id: UUID
    audience: str = Field(min_length=1, max_length=2048)
    client_ids: tuple[UUID, ...] = Field(min_length=1)
    object_ids: tuple[UUID, ...] = Field(min_length=1)


class BudgetDelegationCheck(StrictModel):
    case: Literal[
        "valid_identity", "wrong_audience", "missing_person", "current_fenced", "candidate_fenced",
    ]
    expected_status: int
    actual_status: int | None
    native_gateway_id: str | None = None
    expected_error_code: str | None = None
    actual_error_code: str | None = None


class BudgetExecutionVerificationOptions(StrictModel):
    application_id: UUID
    model_ids: list[UUID]


class BudgetDelegationReadiness(StrictModel):
    gateway_profile_id: UUID
    native_gateway_id: str
    revision: str
    checked_at: datetime
    ready: bool
    checks: list[BudgetDelegationCheck]
    protocol_mode: Literal["prepared", "paused", "active"] = "prepared"
    verification: BudgetExecutionVerificationOptions | None = None


class BudgetExecutionVerificationRequest(StrictModel):
    application_id: UUID
    model_id: UUID


class BudgetExecutionScopeReadback(StrictModel):
    before: LedgerSummary
    after: LedgerSummary | None
    attempt: LedgerAttempt | None


class BudgetExecutionVerification(StrictModel):
    gateway_profile_id: UUID
    native_gateway_id: str
    revision: str
    checked_at: datetime
    request_id: str
    actual_status: int
    invocation: ModelInvocationResponse | None
    scopes: list[BudgetExecutionScopeReadback]
    settled: bool
