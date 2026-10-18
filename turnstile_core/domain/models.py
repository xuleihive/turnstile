from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricTotals(StrictModel):
    et: float = Field(ge=0)
    total_tokens: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    # Explicit read-only cache bucket for charts. `cached_tokens` remains the historical full
    # bucket (read + write) so existing aggregate contracts stay compatible.
    cache_read_tokens: int = Field(ge=0, default=0)
    cache_write_tokens: int = Field(ge=0, default=0)
    output_tokens: int = Field(ge=0)
    calls: int = Field(ge=0)
    # Summed from the per-row price snapshot, so a bucket is priced the way its requests were
    # priced when they landed rather than at today's registry rates. It defaults because the
    # same shape is reused by run and optimization aggregates, which have no cost dimension at
    # all; `test_trend_buckets_carry_the_settled_cost` keeps the trend path from regressing to
    # the default silently.
    estimated_cost: float = Field(ge=0, default=0.0)
    # Per-bucket tail latency and failure count, so a quality-over-time chart does not have to
    # be rebuilt in the browser from a capped request page. Same default rationale as above.
    p95_latency_ms: float = Field(ge=0, default=0.0)
    failed_calls: int = Field(ge=0, default=0)


class ExecutiveTotals(StrictModel):
    total_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0, default=0)
    total_requests: int = Field(ge=0)
    estimated_cost: float = Field(ge=0)
    average_latency_ms: float = Field(ge=0)
    error_rate: float = Field(ge=0, le=100)
    # Everything below answers a question a mean cannot: how slow was the slow tail, and how
    # did the failures split. The dashboard used to derive these in the browser from a capped
    # 200-row page and present them as window-wide, which under-reports silently once the
    # window holds more rows than the cap. `DistributionItem` extends this model, so adding a
    # field here also gives every ranking dimension its own tail latency and failure split.
    p50_latency_ms: float = Field(ge=0, default=0.0)
    p95_latency_ms: float = Field(ge=0, default=0.0)
    p99_latency_ms: float = Field(ge=0, default=0.0)
    success_requests: int = Field(ge=0, default=0)
    client_error_requests: int = Field(ge=0, default=0)
    server_error_requests: int = Field(ge=0, default=0)
    latency_under_1s: int = Field(ge=0, default=0)
    latency_1_to_2s: int = Field(ge=0, default=0)
    latency_2_to_5s: int = Field(ge=0, default=0)
    latency_over_5s: int = Field(ge=0, default=0)


class ExecutiveChanges(StrictModel):
    total_tokens: float | None
    cache_read_tokens: float | None = None
    total_requests: float | None
    estimated_cost: float | None
    average_latency_ms: float | None
    error_rate: float | None
    p50_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    p99_latency_ms: float | None = None
    success_requests: float | None = None
    client_error_requests: float | None = None
    server_error_requests: float | None = None
    latency_under_1s: float | None = None
    latency_1_to_2s: float | None = None
    latency_2_to_5s: float | None = None
    latency_over_5s: float | None = None


class ExecutiveOverviewResponse(StrictModel):
    from_: datetime = Field(alias="from")
    to: datetime
    previous_from: datetime
    generated_at: datetime
    totals: ExecutiveTotals
    changes_percent: ExecutiveChanges


class DistributionBreakdown(StrictModel):
    """One slice of a ranking row, used to stack a second dimension inside the first.

    Deliberately narrower than `DistributionItem`: a stacked bar needs volume, not tail
    latency or a failure split, and computing percentiles for every parent-child pair would
    cost far more than the chart can show.
    """

    id: str
    name: str
    total_tokens: int = Field(ge=0)
    total_requests: int = Field(ge=0)


class DistributionItem(ExecutiveTotals):
    """One ranking row. Inherits the window metrics so a dimension carries the same tail
    latency and failure split as the window total, and so adding a metric is one edit
    rather than two that can drift apart. The frontend type has always been
    `ExecutiveTotals & { id, name, share_percent }`; this makes the backend agree."""

    id: str
    name: str
    share_percent: float = Field(ge=0, le=100)
    # Populated only when the caller asks for a second dimension via `split_by`.
    breakdown: list[DistributionBreakdown] = Field(default_factory=list)


class DistributionResponse(StrictModel):
    from_: datetime = Field(alias="from")
    to: datetime
    dimension: str
    items: list[DistributionItem]


class UsageRequestSummary(StrictModel):
    # Caller-visible logical identity. Retries may reuse it.
    request_id: str
    # Unique APIM attempt identity and the exact-detail selector.
    correlation_id: str | None
    trace_id: str | None = None
    usage_available: bool = True
    platform_outcome: Literal["dispatched", "succeeded", "failed"] | None = None
    gateway_status_code: int | None = Field(default=None, ge=100, le=599)
    upstream_request_id: str | None = None
    timestamp: datetime
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
    model_id: str
    model_name: str
    request_source: str
    # The access point the call arrived through. On the summary rather than the detail because
    # separating APIM traffic from CLI traffic is a list-level and aggregate-level question.
    runtime: str
    apim_target_id: UUID | None = None
    gateway_deployment_id: UUID | None = None
    native_gateway_id: str | None = None
    native_gateway_instance_id: str | None = None
    gateway_is_managed: bool | None = None
    apim_region: str | None = None
    api_revision: str | None = None
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    status_code: int | None = Field(ge=100, le=599)
    estimated_cost: float = Field(ge=0)
    cost_available: bool | None = None
    error_message: str | None
    selected_model_id: UUID | None = None
    selected_model_name: str | None = None


class UsageRequestListResponse(StrictModel):
    items: list[UsageRequestSummary]
    page: PageInfo


class UsageRequestDetail(UsageRequestSummary):
    provider: str
    workflow: str
    run_id: str
    turn_index: int = Field(ge=1)
    cached_tokens: int = Field(ge=0)
    input_price_per_million: float | None = Field(default=None, ge=0)
    cached_price_per_million: float | None = Field(default=None, ge=0)
    cache_write_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    price_context_tier: str | None = None
    price_context_snapshot: dict[str, Any] | None = None
    # Write-only subset of cached_tokens. Writes bill far above reads, so two requests with the
    # same token counts can differ in cost by an order of magnitude without this field.
    cache_write_tokens: int = Field(ge=0, default=0)
    estimated: bool
    ingest_source: str
    ingest_error: str | None = None
    # Set when the breakdown was recovered from gateway logs rather than read from the response
    # body. Cache usage remains unavailable on those rows and is surfaced as a lower bound.
    reconciled_at: datetime | None = None
    # Outcome of the gateway budget admission check. Anything other than 'ok' or None means
    # the call was allowed through without a working ledger, which must not be silent.
    budget_admission: str | None = None
    # Outcome of the gateway model access check. Same contract as above: anything other
    # than 'ok' or None means the model policy did not actually bind on this call.
    model_admission: str | None = None


class UsageAnomaly(StrictModel):
    id: str
    detected_at: datetime
    rule_id: str
    severity: str
    title: str
    description: str
    dimension: str
    dimension_id: str
    dimension_name: str
    request_id: str | None = None
    actual_value: float
    threshold_value: float


class UsageAnomalyListResponse(StrictModel):
    items: list[UsageAnomaly]


AnomalyRuleMetric = Literal[
    "error_rate_percent",
    "request_latency_ms",
    "request_cost_usd",
    "agent_request_count",
]
AnomalyThresholdMode = Literal["absolute", "percentile"]
AnomalySeverity = Literal["warning", "critical"]
AnomalyScopeType = Literal[
    "global", "organization", "department", "project", "agent", "model", "user"
]


class AnomalyRuleWrite(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    metric: AnomalyRuleMetric
    threshold_mode: AnomalyThresholdMode = "absolute"
    threshold_value: float = Field(gt=0)
    minimum_sample_size: int = Field(default=1, ge=1, le=100_000)
    severity: AnomalySeverity = "warning"
    scope_type: AnomalyScopeType = "global"
    scope_id: str | None = Field(default=None, max_length=255)
    enabled: bool = True


class AnomalyRule(AnomalyRuleWrite):
    id: UUID
    created_at: datetime
    updated_at: datetime
    updated_by: str


class AnomalyRuleListResponse(StrictModel):
    items: list[AnomalyRule]


class EnterpriseEntity(StrictModel):
    id: str
    name: str
    parent_id: str | None = None


class EnterpriseEntityCatalog(StrictModel):
    organizations: list[EnterpriseEntity]
    departments: list[EnterpriseEntity]
    projects: list[EnterpriseEntity]
    agents: list[EnterpriseEntity]
    users: list[EnterpriseEntity]
    invocation_testers: list[EnterpriseEntity] = Field(default_factory=list)


class UsageQueryEntity(EnterpriseEntity):
    directory_status: Literal["active", "archived", "historical", "unattributed"]
    source: Literal["directory", "historical"]
    parent_ids: list[str] = Field(default_factory=list)


class UsageQueryEntityCatalog(StrictModel):
    organizations: list[UsageQueryEntity]
    departments: list[UsageQueryEntity]
    projects: list[UsageQueryEntity]
    agents: list[UsageQueryEntity]
    users: list[UsageQueryEntity]
    invocation_testers: list[EnterpriseEntity] = Field(default_factory=list)


BudgetScopeType = Literal["organization", "department", "user"]
BudgetStatus = Literal["unallocated", "unverified", "healthy", "warning", "exceeded"]
BudgetMode = Literal["tokens", "usd"]
PeopleBudgetFilter = Literal[
    "all", "assigned", "unallocated", "unverified", "healthy", "warning", "exceeded"
]


class TokenBudgetWrite(StrictModel):
    budget_mode: BudgetMode = "tokens"
    token_limit: int | None = Field(default=None, ge=1)
    usd_limit: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    warning_threshold_percent: int = Field(default=80, ge=1, le=100)

    @model_validator(mode="after")
    def require_one_limit(self) -> TokenBudgetWrite:
        if self.budget_mode == "tokens":
            if self.token_limit is None or self.usd_limit is not None:
                raise ValueError("Token budgets require only token_limit")
        elif self.usd_limit is None or self.token_limit is not None:
            raise ValueError("USD budgets require only usd_limit")
        return self


class TokenBudgetItem(StrictModel):
    scope_type: BudgetScopeType
    scope_id: str
    scope_name: str
    parent_scope_id: str | None
    budget_mode: BudgetMode = "tokens"
    token_limit: int | None
    usd_limit: Decimal | None = None
    warning_threshold_percent: int
    used_tokens: int = Field(ge=0)
    remaining_tokens: int | None
    used_usd: Decimal | None = None
    remaining_usd: Decimal | None = None
    forecast_usd: Decimal | None = None
    usd_cost_available: bool | None = None
    unpriced_usage_count: int = Field(default=0, ge=0)
    usage_percent: float | None
    forecast_tokens: int = Field(ge=0)
    forecast_percent: float | None
    status: BudgetStatus
    updated_at: datetime | None
    updated_by: str | None
    model_policy_configured: bool = False
    allowed_model_ids: list[UUID] = Field(default_factory=list)


class TokenBudgetRiskItem(StrictModel):
    scope_type: BudgetScopeType
    scope_id: str
    scope_name: str
    status: Literal["warning", "exceeded"]
    usage_percent: float = Field(ge=0)
    forecast_percent: float = Field(ge=0)


class TokenBudgetAuditEvent(StrictModel):
    event_type: Literal["budget"] = "budget"
    id: UUID
    period_start: date
    scope_type: BudgetScopeType
    scope_id: str
    scope_name: str
    action: Literal["assigned", "updated", "removed"]
    previous_budget_mode: BudgetMode | None = None
    new_budget_mode: BudgetMode | None = None
    previous_token_limit: int | None
    new_token_limit: int | None
    previous_usd_limit: Decimal | None = None
    new_usd_limit: Decimal | None = None
    previous_warning_threshold_percent: int | None
    new_warning_threshold_percent: int | None
    changed_at: datetime
    changed_by: str


class UserModelAccessAuditEvent(StrictModel):
    event_type: Literal["model_access"] = "model_access"
    id: UUID
    user_id: str
    user_name: str
    previous_model_ids: list[UUID]
    new_model_ids: list[UUID]
    previous_model_names: list[str]
    new_model_names: list[str]
    changed_at: datetime
    changed_by: str


# Enforcement is opt-in per department: a department without a stored row is treated
# as "audit" so that nothing merely unconfigured can block inference.
EnforcementMode = Literal["block", "audit"]


class DepartmentEnforcement(StrictModel):
    department_id: str
    department_name: str
    mode: EnforcementMode
    updated_at: datetime | None
    updated_by: str | None


class DepartmentEnforcementWrite(StrictModel):
    mode: EnforcementMode


class DepartmentEnforcementAuditEvent(StrictModel):
    event_type: Literal["enforcement"] = "enforcement"
    id: UUID
    department_id: str
    department_name: str
    previous_mode: EnforcementMode | None
    new_mode: EnforcementMode
    changed_at: datetime
    changed_by: str


class TokenBudgetResponse(StrictModel):
    coverage: Literal["global", "department"] = "global"
    period: str
    period_start: date
    period_end: date
    generated_at: datetime
    items: list[TokenBudgetItem]
    risk_count: int = Field(ge=0)
    risk_items: list[TokenBudgetRiskItem] = Field(max_length=25)
    # One stream, because an administrator reviewing a month needs budget edits, model
    # access changes and enforcement switches in the order they happened.
    history: list[
        TokenBudgetAuditEvent | UserModelAccessAuditEvent | DepartmentEnforcementAuditEvent
    ]
    enforcement: list[DepartmentEnforcement]


class TokenBudgetPeopleResponse(StrictModel):
    period: str
    department_id: str
    department_name: str
    department_token_limit: int | None
    department_allocated_tokens: int = Field(ge=0)
    department_available_tokens: int | None
    department_usd_limit: Decimal | None = None
    department_allocated_usd: Decimal | None = None
    department_available_usd: Decimal | None = None
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    assigned_count: int = Field(ge=0)
    unallocated_count: int = Field(ge=0)
    risk_count: int = Field(ge=0)
    model_configured_count: int = Field(default=0, ge=0)
    items: list[TokenBudgetItem]


class TokenBudgetBulkWrite(StrictModel):
    department_id: str
    selection: Literal["ids", "all_matching"] = "ids"
    user_ids: list[str] = Field(default_factory=list, max_length=500)
    query: str | None = Field(default=None, max_length=200)
    status: PeopleBudgetFilter = "all"
    allocation_mode: Literal["fixed", "equal_remaining", "preserve"] = "fixed"
    budget_mode: BudgetMode = "tokens"
    token_limit: int | None = Field(default=None, ge=1)
    usd_limit: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    warning_threshold_percent: int = Field(default=80, ge=1, le=100)
    model_ids: list[UUID] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_limit(self) -> TokenBudgetBulkWrite:
        if self.allocation_mode == "preserve":
            if self.token_limit is not None or self.usd_limit is not None:
                raise ValueError("Preserve allocations cannot include a limit")
            return self
        if self.allocation_mode == "equal_remaining":
            if self.token_limit is not None or self.usd_limit is not None:
                raise ValueError("Equal allocations derive their limit from the parent")
            return self
        if self.budget_mode == "tokens":
            if self.token_limit is None or self.usd_limit is not None:
                raise ValueError("Token allocations require only token_limit")
        elif self.usd_limit is None or self.token_limit is not None:
            raise ValueError("USD allocations require only usd_limit")
        return self


class TokenBudgetBulkResult(StrictModel):
    period: str
    department_id: str
    updated_count: int = Field(ge=1)
    token_limit_per_user: int | None = Field(default=None, ge=1)
    usd_limit_per_user: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    total_allocated: int | None = Field(default=None, ge=1)
    total_usd_allocated: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    model_policy_updated_count: int = Field(default=0, ge=0)


class PeriodMetric(StrictModel):
    from_: datetime = Field(alias="from")
    to: datetime
    totals: MetricTotals
    comparison_percent: float | None


class WorkflowMetric(StrictModel):
    workflow: str
    totals: MetricTotals
    share_percent: float = Field(ge=0, le=100)


class OverviewResponse(StrictModel):
    as_of: datetime
    timezone: str
    today: PeriodMetric
    month: PeriodMetric
    top_workflows: list[WorkflowMetric]
    unattributed_percent: float = Field(ge=0, le=100)
    estimated_percent: float = Field(ge=0, le=100)


class TrendPoint(StrictModel):
    bucket_start: datetime
    key: str
    label: str
    totals: MetricTotals


class TrendResponse(StrictModel):
    from_: datetime = Field(alias="from")
    to: datetime
    timezone: str
    interval: str
    group_by: str
    points: list[TrendPoint]


class ApimCacheReadBucket(StrictModel):
    api_id: str = Field(min_length=1)
    dimension_type: Literal[
        "global",
        "organization",
        "department",
        "project",
        "agent",
        "user",
        "model",
        "runtime",
    ]
    dimension_value: str = Field(min_length=1)
    bucket_start: datetime
    cache_read_tokens: int = Field(ge=0)


class RunTurn(StrictModel):
    usage_id: str
    turn_index: int = Field(ge=1)
    ts: datetime
    model: str
    input_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    et: float = Field(ge=0)
    et_coeff_m: float = Field(gt=0)
    latency_ms: int = Field(ge=0)
    status: str
    estimated: bool
    ingest_source: str


class RunSummary(StrictModel):
    run_id: str
    started_at: datetime
    ended_at: datetime
    team: str
    user: str
    agent: str
    workflow: str
    provider: str
    models: list[str]
    turn_count: int = Field(ge=1)
    totals: MetricTotals
    latency_ms: int = Field(ge=0)
    estimated: bool


class RunDetail(RunSummary):
    turns: list[RunTurn]


class AuditStatus(StrEnum):
    NEW = "new"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    IGNORED = "ignored"


class AuditFinding(StrictModel):
    id: UUID
    created_at: datetime
    updated_at: datetime
    rule_id: str
    severity: str
    workflow: str
    run_id: str | None
    evidence: dict[str, Any]
    status: AuditStatus
    assignee: str | None
    suggestion: str | None
    resolution_note: str | None


class AuditFindingUpdate(StrictModel):
    status: AuditStatus
    assignee: str | None = None
    resolution_note: str | None = Field(default=None, max_length=2000)


class OptimizationComparison(StrictModel):
    window_days: int = Field(ge=1, le=90)
    before: MetricTotals
    after: MetricTotals
    et_change_percent: float | None
    token_change_percent: float | None


class OptimizationEventCreate(StrictModel):
    workflow: str = Field(min_length=1, max_length=255)
    occurred_at: datetime
    label: str = Field(min_length=1, max_length=255)
    notes: str | None = Field(default=None, max_length=2000)
    comparison_window_days: int = Field(default=7, ge=1, le=90)


class OptimizationEvent(StrictModel):
    id: UUID
    created_at: datetime
    workflow: str
    occurred_at: datetime
    label: str
    notes: str | None
    comparison: OptimizationComparison


class PageInfo(StrictModel):
    next_cursor: str | None


class RunListResponse(StrictModel):
    items: list[RunSummary]
    page: PageInfo


class AuditFindingListResponse(StrictModel):
    items: list[AuditFinding]
    page: PageInfo


class OptimizationEventListResponse(StrictModel):
    items: list[OptimizationEvent]
    page: PageInfo


class UsageEvent(StrictModel):
    id: str
    budget_execution: list[dict[str, Any]] = Field(default_factory=list, max_length=2)
    budget_price_projection: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    usd_cost: Decimal | None = Field(
        default=None, ge=0, max_digits=30, decimal_places=14,
        exclude_if=lambda value: value is None,
    )
    budget_terminal: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    usage_complete: bool = Field(default=False, strict=True)
    event_schema_version: int = Field(default=1, ge=1, le=2)
    usage_id: str | None = Field(
        default=None,
        pattern=r"^turnstile-v2-[0-9a-f]{64}$",
        max_length=77,
    )
    usage_event_source: Literal["apim", "observer", "reconciliation", "backfill"] = (
        "apim"
    )
    request_id: str | None = None
    correlation_id: str | None = None
    ts: datetime
    team: str = "unattributed"
    organization: str = "unattributed"
    organization_id: str = "unattributed"
    department: str = "unattributed"
    department_id: str = "unattributed"
    project: str = "unattributed"
    project_id: str = "unattributed"
    user: str = "unattributed"
    user_id: str = "unattributed"
    agent: str = "unattributed"
    agent_id: str = "unattributed"
    workflow: str = "unattributed"
    run_id: str = "unattributed"
    turn_index: int = Field(ge=1)
    provider: str
    model: str
    model_id: str = "unattributed"
    runtime: str = "unattributed"
    runtime_authoritative: bool = False
    request_source: str = "unattributed"
    gateway_profile_id: UUID | None = None
    gateway_deployment_id: UUID | None = None
    native_gateway_id: str | None = Field(default=None, min_length=1, max_length=80)
    native_gateway_instance_id: str | None = Field(
        default=None, min_length=1, max_length=255
    )
    gateway_is_managed: bool | None = None
    apim_service_id: str | None = Field(default=None, min_length=1, max_length=2048)
    apim_region: str | None = Field(default=None, min_length=1, max_length=128)
    api_revision: str | None = Field(default=None, min_length=1, max_length=256)
    apim_subscription_id: str | None = Field(default=None, max_length=255)
    application_actor_type: Literal["person", "service"] | None = None
    application_actor_id: str | None = Field(default=None, max_length=255)
    application_admission: str | None = Field(default=None, max_length=64)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    # Write-only subset of cached_tokens. Providers bill writes far above reads, so the two are
    # kept separable. None means the gateway did not report the split.
    cache_write_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    tokens_consumed: int | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)
    status: str
    status_code: int | None = Field(default=None, ge=0, le=599)
    estimated_cost: float | None = Field(default=None, ge=0)
    error_message: str | None = Field(default=None, max_length=2000)
    estimated: bool = False
    ingest_source: str = "eventhub"
    ingest_error: str | None = None
    # How the gateway's budget admission check resolved. None means the request never
    # reached it; 'ok' means the ledger answered. Anything else is a fail-open degradation.
    budget_admission: str | None = Field(default=None, max_length=64)
    # How the gateway's per-person model access check resolved, on the same contract.
    model_admission: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_gateway_attribution(self) -> UsageEvent:
        if (self.usd_cost is not None or self.budget_price_projection is not None) and (
            self.usd_cost is None or self.budget_price_projection is None
            or self.event_schema_version != 2 or self.usage_event_source != "apim"
            or self.usage_complete is not True or self.estimated is not False
            or self.ingest_source != "eventhub"
        ):
            raise ValueError("USD cost requires complete measured APIM price evidence")
        if self.budget_terminal is not None:
            from .budget_completion import validate_terminal_observation

            observed = validate_terminal_observation(self.budget_terminal)
            if (
                self.event_schema_version != 2 or not self.budget_execution
                or self.ingest_source != "eventhub" or self.usage_event_source != observed["source"]
                or self.status_code != observed["rejection"]["status_code"]
                or self.usage_complete or not self.estimated
                or any(value is not None for value in (
                    self.input_tokens, self.cached_tokens, self.cache_write_tokens,
                    self.output_tokens, self.tokens_consumed,
                ))
            ):
                raise ValueError("Terminal evidence requires attributable unknown-usage telemetry")
            self.budget_terminal = observed
        gateway_fields = (
            self.gateway_profile_id,
            self.native_gateway_id,
            self.gateway_is_managed,
            self.apim_service_id,
            self.apim_region,
            self.api_revision,
        )
        if self.event_schema_version == 1:
            if self.usage_id is not None or any(
                value is not None
                for value in (
                    self.gateway_deployment_id,
                    self.native_gateway_id,
                    self.native_gateway_instance_id,
                    self.gateway_is_managed,
                    self.apim_service_id,
                    self.apim_region,
                    self.api_revision,
                )
            ):
                raise ValueError("Gateway attribution fields require usage event schema 2")
            return self
        if self.usage_id is None:
            raise ValueError("usage event schema 2 requires usage_id")
        if self.id != self.usage_id:
            raise ValueError("usage event schema 2 requires id to equal usage_id")
        if self.correlation_id is None:
            raise ValueError("usage event schema 2 requires correlation_id")
        if any(value is None for value in gateway_fields):
            raise ValueError("usage event schema 2 requires complete Gateway attribution")
        assert self.native_gateway_id is not None
        assert self.gateway_is_managed is not None
        if self.gateway_is_managed != (
            self.native_gateway_id.casefold() == "managed"
        ):
            raise ValueError("managed Gateway attribution conflicts with native Gateway ID")
        if (
            self.gateway_is_managed
            and self.native_gateway_instance_id is not None
            and self.native_gateway_instance_id.casefold() != "managed"
        ):
            raise ValueError("managed Gateway instance identity must be 'managed'")
        return self


class TokenUsageRecord(StrictModel):
    id: str
    request_id: str
    correlation_id: str
    ts: datetime
    team: str
    organization: str = "unattributed"
    organization_id: str = "unattributed"
    department: str = "unattributed"
    department_id: str = "unattributed"
    project: str = "unattributed"
    project_id: str = "unattributed"
    user: str
    user_id: str = "unattributed"
    agent: str
    agent_id: str = "unattributed"
    workflow: str
    run_id: str
    turn_index: int
    provider: str
    model: str
    model_id: str = "unattributed"
    runtime: str = "unattributed"
    runtime_authoritative: bool = Field(default=False, exclude=True)
    request_source: str = "unattributed"
    usage_domain: Literal["apim", "github_copilot"] = "apim"
    input_tokens: int
    cached_tokens: int
    # Write-only subset of cached_tokens; cache reads are cached_tokens - cache_write_tokens.
    cache_write_tokens: int = 0
    output_tokens: int
    et: float
    et_coeff_m: float
    latency_ms: int
    status: str
    status_code: int
    estimated_cost: float = Field(ge=0)
    usd_cost: Decimal | None = Field(
        default=None, ge=0, max_digits=30, decimal_places=14,
        exclude_if=lambda value: value is None,
    )
    usd_price_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None,
    )
    usd_price_updated_at: datetime | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    # Unit prices applied when the row was priced. None means the request could not be priced, which
    # stays distinguishable from a genuine zero cost.
    input_price_per_million: float | None = Field(default=None, ge=0)
    cached_price_per_million: float | None = Field(default=None, ge=0)
    cache_write_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    error_message: str | None = Field(default=None, max_length=2000)
    estimated: bool
    ingest_source: str
    ingest_error: str | None = None
    budget_admission: str | None = Field(default=None, max_length=64)
    model_admission: str | None = Field(default=None, max_length=64)

    price_context_tier: str | None = Field(default=None, exclude_if=lambda value: value is None)

    price_context_snapshot: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )


class PriceCatalogRuleSource(StrictModel):
    rule_id: str = Field(min_length=1)
    url: str | None = Field(default=None, pattern=r"^https://")
    revision: str = Field(min_length=1)
    verified_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    origin: Literal["official", "reference", "custom"] = Field(
        default="official", exclude_if=lambda value: value == "official",
    )
    configured_at: datetime | None = Field(default=None, exclude_if=lambda value: value is None)
    note: str | None = Field(default=None, max_length=500, exclude_if=lambda value: value is None)


class ModelPriceTier(StrictModel):
    key: str = Field(min_length=1)
    min_input_tokens: int = Field(ge=0, strict=True)
    max_input_tokens: int | None = Field(default=None, ge=0, strict=True)
    input_price_per_million: float = Field(ge=0, allow_inf_nan=False)
    cached_price_per_million: float = Field(ge=0, allow_inf_nan=False)
    cache_write_price_per_million: float = Field(ge=0, allow_inf_nan=False)
    output_price_per_million: float = Field(ge=0, allow_inf_nan=False)
    input_range_source: PriceCatalogRuleSource | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )


class ModelPrice(StrictModel):
    """Registry unit prices in USD per million tokens."""

    input_price_per_million: float = Field(ge=0)
    cached_price_per_million: float = Field(ge=0)
    cache_write_price_per_million: float = Field(ge=0)
    output_price_per_million: float = Field(ge=0)
    context_tiers: list[ModelPriceTier] = Field(default_factory=list, max_length=32)
    selected_tier: str | None = None

    @classmethod
    def from_registry(cls, values: Mapping[str, Any]) -> ModelPrice | None:
        tiers = values.get("context_price_tiers")
        if tiers:
            return cls(
                input_price_per_million=0, cached_price_per_million=0,
                cache_write_price_per_million=0, output_price_per_million=0,
                context_tiers=[ModelPriceTier.model_validate({
                    key: tier[key] for key in ModelPriceTier.model_fields if key in tier
                }) for tier in tiers],
            )
        input_price = values.get("input_cost_per_million")
        output_price = values.get("output_cost_per_million")
        if input_price is None or output_price is None:
            return None
        cached = values.get("cached_cost_per_million")
        cached = input_price if cached is None else cached
        writes = values.get("cache_write_cost_per_million")
        return cls(
            input_price_per_million=input_price, output_price_per_million=output_price,
            cached_price_per_million=cached,
            cache_write_price_per_million=cached if writes is None else writes,
        )

    @classmethod
    def from_usage_snapshot(cls, values: Mapping[str, Any]) -> ModelPrice | None:
        snapshot = values.get("price_context_snapshot")
        if snapshot is not None:
            return cls.model_validate(snapshot)
        return cls.from_registry({
            "input_cost_per_million": values.get("input_price_per_million"),
            "output_cost_per_million": values.get("output_price_per_million"),
            "cached_cost_per_million": values.get("cached_price_per_million"),
            "cache_write_cost_per_million": values.get("cache_write_price_per_million"),
        })

    def snapshot_prices(
        self, input_tokens: int, cached_tokens: int,
        output_tokens: int, cache_write_tokens: int = 0,
    ) -> dict[str, Any]:
        price = self.for_input_tokens(input_tokens + cached_tokens)
        return {
            "price_context_tier": price.selected_tier,
            "price_context_snapshot": self.model_dump(mode="json"),
            "input_price_per_million": price.input_price_per_million,
            "cached_price_per_million": price.cached_price_per_million,
            "cache_write_price_per_million": price.cache_write_price_per_million,
            "output_price_per_million": price.output_price_per_million,
            "estimated_cost": price.cost(
                input_tokens, cached_tokens, output_tokens, cache_write_tokens,
            ),
        }

    @classmethod
    def restore_usage_prices(
        cls, stored: Mapping[str, Any], input_tokens: int, cached_tokens: int,
        output_tokens: int, cache_write_tokens: int = 0,
    ) -> dict[str, Any]:
        snapshot = stored.get("price_context_snapshot")
        rates = {
            field: stored.get(field) for field in (
                "input_price_per_million", "cached_price_per_million",
                "cache_write_price_per_million", "output_price_per_million",
            )
        }
        price = cls.model_validate(snapshot) if snapshot is not None else cls.from_registry({
            field.replace("_price_", "_cost_"): value for field, value in rates.items()
        })
        restored = {
            **rates, "price_context_tier": stored.get("price_context_tier"),
            "estimated_cost": stored.get("estimated_cost", 0),
        }
        if price is not None:
            restored.update(price.snapshot_prices(
                input_tokens, cached_tokens, output_tokens, cache_write_tokens,
            ))
        restored["price_context_snapshot"] = snapshot
        return restored

    @model_validator(mode="after")
    def require_complete_context_ranges(self) -> ModelPrice:
        if not self.context_tiers:
            return self
        minimum = 0
        keys: set[str] = set()
        for index, tier in enumerate(self.context_tiers):
            if tier.key in keys or tier.min_input_tokens != minimum:
                raise ValueError("Context price tiers must be unique, ordered and contiguous")
            keys.add(tier.key)
            if tier.max_input_tokens is None:
                if index != len(self.context_tiers) - 1:
                    raise ValueError("Only the final context price tier may be unbounded")
            elif tier.max_input_tokens < tier.min_input_tokens:
                raise ValueError("Context price tier bounds must be ordered")
            else:
                minimum = tier.max_input_tokens + 1
        if self.context_tiers[-1].max_input_tokens is not None:
            raise ValueError("Context price tiers must cover all input lengths")
        return self

    def for_input_tokens(self, total_input_tokens: int) -> ModelPrice:
        if type(total_input_tokens) is not int or total_input_tokens < 0:
            raise ValueError("Measured total input tokens must be a nonnegative integer")
        if not self.context_tiers:
            return self
        for tier in self.context_tiers:
            if total_input_tokens >= tier.min_input_tokens and (
                tier.max_input_tokens is None or total_input_tokens <= tier.max_input_tokens
            ):
                return ModelPrice(
                    input_price_per_million=tier.input_price_per_million,
                    cached_price_per_million=tier.cached_price_per_million,
                    cache_write_price_per_million=tier.cache_write_price_per_million,
                    output_price_per_million=tier.output_price_per_million,
                    selected_tier=tier.key,
                )
        raise ValueError("No context price tier covers the request")

    def cost(
        self,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
    ) -> float:
        """Price a request. `cached_tokens` is the full cache bucket; writes are its subset."""
        price = self.for_input_tokens(input_tokens + cached_tokens)
        cache_reads = max(cached_tokens - cache_write_tokens, 0)
        return round(
            (
            input_tokens * price.input_price_per_million
            + cache_reads * price.cached_price_per_million
            + cache_write_tokens * price.cache_write_price_per_million
            + output_tokens * price.output_price_per_million
            )
            / 1_000_000,
            8,
        )

    def cost_decimal(
        self,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
    ) -> Decimal:
        """Return exact USD from the persisted eight-decimal unit-price precision."""
        price = self.for_input_tokens(input_tokens + cached_tokens)
        cache_reads = max(cached_tokens - cache_write_tokens, 0)
        with localcontext() as context:
            context.prec = 50
            million = Decimal(1_000_000)
            return (
                Decimal(input_tokens) * Decimal(str(price.input_price_per_million))
                + Decimal(cache_reads) * Decimal(str(price.cached_price_per_million))
                + Decimal(cache_write_tokens) * Decimal(str(price.cache_write_price_per_million))
                + Decimal(output_tokens) * Decimal(str(price.output_price_per_million))
            ) / million


class ModelIdentity(StrictModel):
    """Canonical registry identity for a model.

    Callers name a model differently: the dashboard BFF sends the registry UUID while an employee
    desktop client sends the model key from its request body. Both resolve to this one identity so
    a single model cannot split into several rows in an aggregate.
    """

    model_id: str
    display_name: str


class ReconciledUsage(StrictModel):
    """Real per-request token counts recovered from APIM telemetry."""

    correlation_id: str
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    cached_tokens: int | None = Field(default=None, ge=0, strict=True)


class ReservationTerminalEvidence(StrictModel):
    """Sanitized APIM terminal evidence for one unresolved ledger reservation."""

    correlation_id: str = Field(min_length=1, max_length=255)
    observed_at: datetime
    status_code: int | None = Field(default=None, ge=0, le=599, strict=True)
    last_error_reason: str | None = Field(default=None, max_length=255)
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)

    @property
    def has_exact_usage(self) -> bool:
        return (
            self.prompt_tokens is not None
            and self.completion_tokens is not None
            and self.total_tokens is not None
            and self.prompt_tokens + self.completion_tokens == self.total_tokens
        )

    @property
    def is_terminal_zero(self) -> bool:
        return (
            self.status_code is not None
            and self.status_code >= 400
            and all(
                value in (None, 0)
                for value in (self.prompt_tokens, self.completion_tokens, self.total_tokens)
            )
        )


class ReconciliationOutcome(StrictModel):
    source: str
    window_start: datetime
    window_end: datetime
    scanned: int = Field(ge=0)
    matched: int = Field(ge=0)
    watermark: datetime
