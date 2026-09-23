from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    request_id: str
    correlation_id: str
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
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    status_code: int = Field(ge=100, le=599)
    estimated_cost: float = Field(ge=0)
    error_message: str | None


class UsageRequestListResponse(StrictModel):
    items: list[UsageRequestSummary]
    page: PageInfo


class UsageRequestDetail(UsageRequestSummary):
    provider: str
    workflow: str
    run_id: str
    turn_index: int = Field(ge=1)
    cached_tokens: int = Field(ge=0)
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
    # Where Owners are listed before they generate traffic. None means the seeded default.
    default_department_id: str | None = None


# Catalog ids travel in URLs, budget scopes and usage rows, so they are kept to characters
# that need no escaping anywhere those go.
CATALOG_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}$"
CatalogAttributeValue = str | int | float | bool


class CatalogOrganizationWrite(StrictModel):
    id: str = Field(pattern=CATALOG_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    external_ref: str | None = Field(default=None, max_length=400)
    attributes: dict[str, CatalogAttributeValue] = Field(default_factory=dict, max_length=20)


class CatalogDepartmentWrite(CatalogOrganizationWrite):
    parent_id: str = Field(pattern=CATALOG_ID_PATTERN)


class EnterpriseCatalogWrite(StrictModel):
    """A whole catalog. Writing one replaces the previous one."""

    organizations: list[CatalogOrganizationWrite] = Field(min_length=1, max_length=2000)
    departments: list[CatalogDepartmentWrite] = Field(default_factory=list, max_length=10000)
    default_department_id: str | None = Field(default=None, pattern=CATALOG_ID_PATTERN)


class EnterpriseCatalogEntity(StrictModel):
    id: str
    name: str
    parent_id: str | None = None
    external_ref: str | None = None
    attributes: dict[str, CatalogAttributeValue] = Field(default_factory=dict)


class EnterpriseCatalogResponse(StrictModel):
    # "seeded" is the built-in demonstration catalog, in use until one is configured.
    source: Literal["configured", "seeded"]
    organizations: list[EnterpriseCatalogEntity]
    departments: list[EnterpriseCatalogEntity]
    default_department_id: str | None
    updated_at: datetime | None = None
    updated_by: str | None = None


BudgetScopeType = Literal["organization", "department", "user"]
BudgetStatus = Literal["unallocated", "healthy", "warning", "exceeded"]
PeopleBudgetFilter = Literal["all", "assigned", "unallocated", "healthy", "warning", "exceeded"]


class TokenBudgetWrite(StrictModel):
    token_limit: int = Field(ge=1)
    warning_threshold_percent: int = Field(default=80, ge=1, le=100)


class TokenBudgetItem(StrictModel):
    scope_type: BudgetScopeType
    scope_id: str
    scope_name: str
    parent_scope_id: str | None
    token_limit: int | None
    warning_threshold_percent: int
    used_tokens: int = Field(ge=0)
    remaining_tokens: int | None
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
    previous_token_limit: int | None
    new_token_limit: int | None
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
    token_limit: int | None = Field(default=None, ge=1)
    warning_threshold_percent: int = Field(default=80, ge=1, le=100)
    model_ids: list[UUID] | None = Field(default=None, max_length=100)


class TokenBudgetBulkResult(StrictModel):
    period: str
    department_id: str
    updated_count: int = Field(ge=1)
    token_limit_per_user: int | None = Field(default=None, ge=1)
    total_allocated: int | None = Field(default=None, ge=1)
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


class ModelPrice(StrictModel):
    """Registry unit prices in USD per million tokens."""

    input_price_per_million: float = Field(ge=0)
    cached_price_per_million: float = Field(ge=0)
    cache_write_price_per_million: float = Field(ge=0)
    output_price_per_million: float = Field(ge=0)

    def cost(
        self,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
    ) -> float:
        """Price a request. `cached_tokens` is the full cache bucket; writes are its subset."""
        cache_reads = max(cached_tokens - cache_write_tokens, 0)
        return round(
            (
                input_tokens * self.input_price_per_million
                + cache_reads * self.cached_price_per_million
                + cache_write_tokens * self.cache_write_price_per_million
                + output_tokens * self.output_price_per_million
            )
            / 1_000_000,
            8,
        )


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
