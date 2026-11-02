"""The tools the assistant is allowed to call, and the charts they author.

Every tool is a thin, validated wrapper over a repository method that already backs a
dashboard page. That is deliberate: the assistant cannot reach data the product does not
already expose, and a number it reports is the same number the corresponding page shows.

Argument models are intentionally **flat** — enums, strings, ints, no nested objects.
`model_json_schema()` then emits a schema with no `$defs`/`$ref`, which is both easier
for the model to fill in correctly and free of the compatibility questions that nested
refs raise across gateways.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from turnstile_core.domain.assistant_models import (
    AssistantRouterProposal,
    AssistantRouterProposalModel,
    ChartQuery,
    ChartSeries,
    ChartSpec,
    TimeRangePreset,
    TrendInterval,
    UsageDimension,
    UsageMetric,
)
from turnstile_core.domain.model_router import (
    ModelRouterCandidate,
    ModelRouterCondition,
    ModelRouterDefinition,
    ModelRouterEdge,
    ModelRouterFallbackEvent,
    ModelRouterNode,
    ModelRouterPosition,
    ModelRouterStrategyModel,
    ModelRouterStrategyType,
)
from turnstile_core.domain.runtime_models import ToolDefinition, ToolFunctionDefinition
from turnstile_core.persistence.repository import QueryRepository, UsageFilters

from .assistant_shared import ToolOutcome

MAX_ROWS = 50

# Charts are authored by tools, so their labels are produced here rather than by the
# model. That means this table is what makes the feature work in a non-Chinese UI: the
# default locale is English, and a chart titled 部门 · Token 用量排名 inside an English
# dashboard is not a translation gap, it is a broken chart. Anything outside this table
# falls back to English rather than to the source language, because a reader who asked
# for Korean is better served by English than by Chinese they may not read.
Locale = str

_METRIC_LABELS: dict[str, dict[str, tuple[str, str]]] = {
    # locale -> metric -> (display label, unit)
    "zh-CN": {
        "total_tokens": ("Token 用量", "Token"),
        "estimated_cost": ("估算费用", "USD"),
        "total_requests": ("请求数", "次"),
        "average_latency_ms": ("平均时延", "ms"),
    },
    "en": {
        "total_tokens": ("Tokens", "Token"),
        "estimated_cost": ("Estimated cost", "USD"),
        "total_requests": ("Requests", "calls"),
        "average_latency_ms": ("Average latency", "ms"),
    },
}

_DIMENSION_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "organization": "组织",
        "department": "部门",
        "project": "项目",
        "agent": "智能体",
        "model": "模型",
        "user": "人员",
        "runtime": "运行时",
    },
    "en": {
        "organization": "Organization",
        "department": "Department",
        "project": "Project",
        "agent": "Agent",
        "model": "Model",
        "user": "Person",
        "runtime": "Runtime",
    },
}

_INTERVAL_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {"hour": "每小时", "day": "每日", "week": "每周"},
    "en": {"hour": "hourly", "day": "daily", "week": "weekly"},
}

_PRESET_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "today": "今天",
        "yesterday": "昨天",
        "last_7_days": "过去 7 天",
        "last_30_days": "过去 30 天",
        "last_90_days": "过去 90 天",
        "this_month": "本月",
        "last_month": "上月",
        "custom": "自定义区间",
    },
    "en": {
        "today": "Today",
        "yesterday": "Yesterday",
        "last_7_days": "Last 7 days",
        "last_30_days": "Last 30 days",
        "last_90_days": "Last 90 days",
        "this_month": "This month",
        "last_month": "Last month",
        "custom": "Custom range",
    },
}

_TEXT: dict[str, dict[str, str]] = {
    "zh-CN": {
        "all_scopes": "全部范围",
        "ranking_title": "{dimension} · {metric}排名",
        "ranking_basis": (
            "来源 token_usage 明细，按{dimension}聚合；筛选：{scope}。"
            "费用为入库时单价结算的存量值。"
        ),
        "trend_title": "{dimension} · {metric}趋势（{interval}）",
        "trend_basis": (
            "来源 token_usage 明细，按{interval}分桶并按{dimension}拆分，"
            "仅保留用量最高的 {count} 项；筛选：{scope}。"
        ),
        "summary_title": "用量总览",
        "summary_basis": "来源 token_usage 明细聚合；筛选：{scope}。",
        "total_tokens": "总 Token",
        "requests": "请求数",
        "cost": "估算费用",
        "latency": "平均时延",
        "error_rate": "错误率",
        "time_field": "时间",
        "metric_field": "指标",
        "value_field": "值",
        "calls_unit": "次",
        "custom_needs_dates": "time_range=custom 需要同时提供 start_date 与 end_date",
        "bad_date": "日期格式必须是 YYYY-MM-DD",
        "bad_order": "end_date 不能早于 start_date",
        "trend_metric_unsupported": (
            "按周期的趋势只支持 total_tokens 与 total_requests。"
            "费用没有按周期聚合的口径，请改用 query_usage_ranking 查费用。"
        ),
        "bad_arguments": "参数不合法：{details}",
        "entities_hit": "命中 {count} 个实体",
        "ranking_summary": "{dimension}×{metric}，{count} 行",
        "trend_summary": "{dimension}趋势，{buckets} 个周期 × {series} 条序列",
        "summary_summary": "总览 5 项指标",
    },
    "en": {
        "all_scopes": "all scopes",
        "ranking_title": "{dimension} · {metric} ranking",
        "ranking_basis": (
            "From token_usage detail, aggregated by {dimension}; filters: {scope}. "
            "Cost is the stored value priced at ingestion."
        ),
        "trend_title": "{dimension} · {metric} trend ({interval})",
        "trend_basis": (
            "From token_usage detail, bucketed {interval} and split by {dimension}, "
            "keeping the {count} highest-usage series; filters: {scope}."
        ),
        "summary_title": "Usage summary",
        "summary_basis": "Aggregated from token_usage detail; filters: {scope}.",
        "total_tokens": "Total tokens",
        "requests": "Requests",
        "cost": "Estimated cost",
        "latency": "Average latency",
        "error_rate": "Error rate",
        "time_field": "Time",
        "metric_field": "Metric",
        "value_field": "Value",
        "calls_unit": "calls",
        "custom_needs_dates": "time_range=custom requires both start_date and end_date",
        "bad_date": "Dates must be formatted YYYY-MM-DD",
        "bad_order": "end_date cannot precede start_date",
        "trend_metric_unsupported": (
            "Per-period trends support only total_tokens and total_requests. "
            "Cost has no per-period aggregate; use query_usage_ranking for cost."
        ),
        "bad_arguments": "Invalid arguments: {details}",
        "entities_hit": "{count} entities matched",
        "ranking_summary": "{dimension} x {metric}, {count} rows",
        "trend_summary": "{dimension} trend, {buckets} periods x {series} series",
        "summary_summary": "5 summary metrics",
    },
}


def _pack(table: dict[str, dict[str, Any]], locale: str) -> dict[str, Any]:
    return table.get(locale) or table["en"]


def text(locale: str, key: str, **values: Any) -> str:
    template = str(_pack(_TEXT, locale)[key])
    return template.format(**values) if values else template


class ToolArguments(BaseModel):
    """Reject unknown keys so a hallucinated argument fails loudly, not silently."""

    model_config = ConfigDict(extra="forbid")


class TimeScopedArguments(ToolArguments):
    time_range: TimeRangePreset = "last_30_days"
    start_date: str | None = Field(
        default=None, description="仅当 time_range=custom 时使用，格式 YYYY-MM-DD"
    )
    end_date: str | None = Field(
        default=None, description="仅当 time_range=custom 时使用，格式 YYYY-MM-DD，含当天"
    )
    organization_id: str | None = None
    department_id: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    model_id: str | None = None
    user_id: str | None = None


class UsageRankingArguments(TimeScopedArguments):
    dimension: UsageDimension
    metric: UsageMetric = "total_tokens"
    limit: int = Field(default=10, ge=1, le=MAX_ROWS)


class UsageTrendArguments(TimeScopedArguments):
    dimension: UsageDimension
    interval: TrendInterval = "day"
    metric: str = Field(
        default="total_tokens",
        description="只支持 total_tokens 或 total_requests；按周期的费用不可得",
    )
    limit: int = Field(default=5, ge=1, le=10)


class UsageSummaryArguments(TimeScopedArguments):
    pass


class EntityArguments(ToolArguments):
    kind: str = Field(
        default="all",
        description="organizations、departments、projects、agents、users 或 all",
    )
    keyword: str | None = Field(default=None, max_length=120)


class RouterModelCatalogArguments(ToolArguments):
    capability: str | None = Field(default=None, max_length=64)


RouterWeight = Annotated[int, Field(ge=1, le=100)]
RouterProposalFallbackEvent = Literal["error", "timeout", "rate_limit"]


class RouterProposalArguments(ToolArguments):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    strategy_type: Literal["priority", "weighted", "model_based"] = "priority"
    routing_model_id: UUID | None = None
    model_ids: list[UUID] = Field(min_length=1, max_length=10)
    model_weights: list[RouterWeight] | None = Field(
        default=None, min_length=1, max_length=10
    )
    condition_field: str | None = Field(default=None, min_length=1, max_length=120)
    condition_operator: Literal[
        "equals", "not_equals", "contains", "greater_than", "greater_than_or_equal",
        "less_than", "less_than_or_equal", "exists"
    ] | None = None
    condition_value: str | float | bool | None = None
    else_model_ids: list[UUID] = Field(default_factory=list, max_length=10)
    else_strategy_type: Literal["priority", "weighted", "model_based"] | None = None
    else_routing_model_id: UUID | None = None
    else_model_weights: list[RouterWeight] | None = Field(default=None, max_length=10)
    fallback_on: list[RouterProposalFallbackEvent] = Field(
        default_factory=list, max_length=3
    )

    @field_validator("name", "condition_field", mode="before")
    @classmethod
    def strip_required_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_router_shape(self) -> RouterProposalArguments:
        has_condition = self.condition_field is not None
        if has_condition != (self.condition_operator is not None):
            raise ValueError("condition_field and condition_operator must be provided together")
        if bool(self.else_model_ids) != has_condition:
            raise ValueError("else_model_ids are required exactly when a condition is configured")
        if self.condition_operator == "exists" and self.condition_value is not None:
            raise ValueError("an exists condition must not provide condition_value")
        if self.condition_operator not in {None, "exists"} and self.condition_value is None:
            raise ValueError("this condition operator requires condition_value")
        if self.model_weights is not None and len(self.model_weights) != len(self.model_ids):
            raise ValueError("model_weights must match model_ids")
        if self.else_model_weights is not None and len(self.else_model_weights) != len(
            self.else_model_ids
        ):
            raise ValueError("else_model_weights must match else_model_ids")
        if len(set(self.model_ids + self.else_model_ids)) != len(
            self.model_ids + self.else_model_ids
        ):
            raise ValueError("a model cannot appear in more than one branch")
        if (self.strategy_type == "model_based") != (self.routing_model_id is not None):
            raise ValueError(
                "routing_model_id is required exactly for a model_based primary branch"
            )
        if not has_condition and self.else_routing_model_id is not None:
            raise ValueError("else_routing_model_id requires a condition branch")
        if has_condition:
            else_strategy = self.else_strategy_type or self.strategy_type
            if (else_strategy == "model_based") != (
                self.else_routing_model_id is not None
            ):
                raise ValueError(
                    "else_routing_model_id is required exactly for a model_based else branch"
                )
        return self


@dataclass(frozen=True)
class ResolvedRange:
    start: datetime
    end: datetime
    label: str
    # The local calendar days the window covers, inclusive. Kept alongside the UTC
    # instants because pinning re-expresses the range as `custom` dates, and deriving
    # those back from the instants would need the timezone all over again.
    first: date
    last: date


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def resolve_range(
    arguments: TimeScopedArguments, timezone: str, locale: str = "en"
) -> ResolvedRange:
    """Turn a preset into an absolute UTC half-open interval.

    Resolved server-side on purpose. If the model were allowed to compute "this month"
    itself it would occasionally be a day out, and nothing downstream could tell.
    """

    preset = arguments.time_range
    # Months follow the UTC budget calendar, so "this month" is the window the budget page
    # and Application usage report.
    if preset in {"this_month", "last_month"}:
        timezone = "UTC"
    zone = _zone(timezone)
    now = datetime.now(zone)
    today = now.date()
    presets = _pack(_PRESET_LABELS, locale)
    if preset == "custom":
        if not arguments.start_date or not arguments.end_date:
            raise ToolInputError(text(locale, "custom_needs_dates"))
        try:
            first = datetime.strptime(arguments.start_date, "%Y-%m-%d").date()
            last = datetime.strptime(arguments.end_date, "%Y-%m-%d").date()
        except ValueError as error:
            raise ToolInputError(text(locale, "bad_date")) from error
        if last < first:
            raise ToolInputError(text(locale, "bad_order"))
        label = f"{first.isoformat()} – {last.isoformat()}"
    elif preset in {"today", "yesterday"}:
        first = last = today if preset == "today" else today - timedelta(days=1)
        label = f"{presets[preset]} ({first.isoformat()})"
    elif preset == "this_month":
        first = today.replace(day=1)
        last = today
        label = f"{presets[preset]} ({first.isoformat()} – {last.isoformat()})"
    elif preset == "last_month":
        last_of_previous = today.replace(day=1) - timedelta(days=1)
        first = last_of_previous.replace(day=1)
        last = last_of_previous.replace(
            day=calendar.monthrange(last_of_previous.year, last_of_previous.month)[1]
        )
        label = f"{presets[preset]} ({first.isoformat()} – {last.isoformat()})"
    else:
        days = {"last_7_days": 7, "last_30_days": 30, "last_90_days": 90}[preset]
        first = today - timedelta(days=days - 1)
        last = today
        label = f"{presets[preset]} ({first.isoformat()} – {last.isoformat()})"

    start = datetime.combine(first, datetime.min.time(), tzinfo=zone)
    end = datetime.combine(last, datetime.min.time(), tzinfo=zone) + timedelta(days=1)
    return ResolvedRange(
        start=start.astimezone(UTC),
        end=end.astimezone(UTC),
        label=f"{label} · {timezone}",
        first=first,
        last=last,
    )


def _filters(arguments: TimeScopedArguments) -> UsageFilters:
    return UsageFilters(
        organization_id=arguments.organization_id,
        department_id=arguments.department_id,
        project_id=arguments.project_id,
        agent_id=arguments.agent_id,
        model_id=arguments.model_id,
        user_id=arguments.user_id,
        runtime=None,
        status_code=None,
    )


def _scope_note(arguments: TimeScopedArguments, locale: str) -> str:
    dimensions = _pack(_DIMENSION_LABELS, locale)
    active = {
        dimensions["organization"]: arguments.organization_id,
        dimensions["department"]: arguments.department_id,
        dimensions["project"]: arguments.project_id,
        dimensions["agent"]: arguments.agent_id,
        dimensions["model"]: arguments.model_id,
        dimensions["user"]: arguments.user_id,
    }
    applied = [f"{name}={value}" for name, value in active.items() if value]
    return ", ".join(applied) if applied else text(locale, "all_scopes")


class ToolInputError(ValueError):
    """Raised when arguments are unusable. Reported back to the model, not to the user."""


class AssistantTools:
    def __init__(
        self, repository: QueryRepository, entity_catalog: Callable[[], Mapping[str, Any]]
    ):
        self._repository = repository
        self._entity_catalog = entity_catalog
        self._router_user_email = ""
        self._router_role = "member"

    def set_router_context(self, user_email: str, role: str) -> None:
        self._router_user_email = user_email
        self._router_role = role

    def _available_router_models(self) -> list[dict[str, Any]]:
        registry = self._repository.registry()
        providers = {row["id"]: row for row in registry["providers"]}
        runtimes = {row["id"]: row for row in registry["runtimes"]}
        gateways = {row["id"]: row for row in registry["gateways"]}
        policy = next(
            (
                row
                for row in self._repository.list_user_model_policies(
                    [self._router_user_email]
                )
                if str(row["user_id"]) == self._router_user_email
            ),
            None,
        )
        assigned = (
            {UUID(str(value)) for value in policy.get("model_ids", [])}
            if policy
            else None
        )
        available: list[dict[str, Any]] = []
        for model in registry["models"]:
            runtime = runtimes.get(model["runtime_id"])
            provider = providers.get(model["provider_id"])
            gateway = gateways.get(runtime.get("gateway_profile_id")) if runtime else None
            model_id = UUID(str(model["id"]))
            if not model.get("enabled") or not runtime or not runtime.get("enabled"):
                continue
            if not provider or not provider.get("enabled"):
                continue
            if gateway is not None and not gateway.get("enabled"):
                continue
            if self._router_role not in (model.get("allowed_roles") or []):
                continue
            if self._router_role not in (runtime.get("allowed_roles") or []):
                continue
            if assigned is not None and model_id not in assigned:
                continue
            if assigned is None and model.get("assignment_required"):
                continue
            available.append(
                {
                    "id": model_id,
                    "display_name": str(model["display_name"]),
                    "provider_name": str(model["provider_name"]),
                    "capabilities": [str(value) for value in model.get("capabilities", [])],
                }
            )
        available.sort(key=lambda item: item["display_name"])
        return available

    def list_router_models(
        self, arguments: RouterModelCatalogArguments, timezone: str, locale: str
    ) -> ToolOutcome:
        del timezone, locale
        capability = (arguments.capability or "").strip().casefold()
        models = [
            model
            for model in self._available_router_models()
            if not capability
            or capability in {value.casefold() for value in model["capabilities"]}
        ]
        return ToolOutcome(
            payload={
                "models": [
                    {
                        **model,
                        "id": str(model["id"]),
                    }
                    for model in models
                ]
            },
            summary=f"{len(models)} Router models available",
            row_count=len(models),
        )

    def propose_model_router(
        self, arguments: RouterProposalArguments, timezone: str, locale: str
    ) -> ToolOutcome:
        del timezone, locale
        available = {model["id"]: model for model in self._available_router_models()}
        requested = [
            *arguments.model_ids,
            *arguments.else_model_ids,
            *(
                [arguments.routing_model_id]
                if arguments.routing_model_id is not None
                else []
            ),
            *(
                [arguments.else_routing_model_id]
                if arguments.else_routing_model_id is not None
                else []
            ),
        ]
        unavailable = [model_id for model_id in requested if model_id not in available]
        if unavailable:
            raise ToolInputError(
                "The proposal contains a model that is unavailable to this user"
            )

        condition = (
            ModelRouterCondition(
                field=arguments.condition_field,
                operator=arguments.condition_operator,
                value=arguments.condition_value,
            )
            if arguments.condition_field is not None
            and arguments.condition_operator is not None
            else None
        )
        start = ModelRouterNode(
            id="start",
            type="start",
            label="Start",
            position=ModelRouterPosition(x=40, y=220),
        )
        output = ModelRouterNode(
            id="output",
            type="output",
            label="Output",
            position=ModelRouterPosition(x=1260, y=220),
        )
        nodes = [start]
        edges: list[ModelRouterEdge] = []
        proposal_models: list[AssistantRouterProposalModel] = []

        def add_branch(
            branch: Literal["primary", "else"],
            model_ids: list[UUID],
            strategy_type: ModelRouterStrategyType,
            routing_model_id: UUID | None,
            weights: list[int] | None,
            y: int,
        ) -> tuple[ModelRouterNode, ModelRouterNode]:
            pool_id = f"{branch}-candidates"
            strategy_id = f"{branch}-strategy"
            resolved_weights = weights or [100] * len(model_ids)
            pool = ModelRouterNode(
                id=pool_id,
                type="candidate_models",
                label="Primary candidates" if branch == "primary" else "Else candidates",
                position=ModelRouterPosition(x=500, y=y),
                candidates=[ModelRouterCandidate(model_id=model_id) for model_id in model_ids],
            )
            strategy = ModelRouterNode(
                id=strategy_id,
                type="strategy",
                label="Primary strategy" if branch == "primary" else "Else strategy",
                position=ModelRouterPosition(x=860, y=y),
                strategy_type=strategy_type,
                routing_model_id=routing_model_id,
                model_configs=[
                    ModelRouterStrategyModel(
                        model_id=model_id,
                        priority=index + 1,
                        weight=resolved_weights[index],
                    )
                    for index, model_id in enumerate(model_ids)
                ],
            )
            edges.extend(
                [
                    ModelRouterEdge(
                        id=f"{pool_id}-{strategy_id}",
                        source=pool_id,
                        target=strategy_id,
                    ),
                    ModelRouterEdge(
                        id=f"{strategy_id}-output",
                        source=strategy_id,
                        target=output.id,
                        source_handle="branch-normal",
                        label="正常输出",
                    ),
                ]
            )
            proposal_models.extend(
                AssistantRouterProposalModel(
                    model_id=model_id,
                    display_name=available[model_id]["display_name"],
                    provider_name=available[model_id]["provider_name"],
                    capabilities=available[model_id]["capabilities"],
                    branch=branch,
                    priority=index + 1,
                    weight=resolved_weights[index],
                )
                for index, model_id in enumerate(model_ids)
            )
            return pool, strategy

        if condition is None:
            primary_pool, primary_strategy = add_branch(
                "primary",
                arguments.model_ids,
                arguments.strategy_type,
                arguments.routing_model_id,
                arguments.model_weights,
                220,
            )
            nodes.extend([primary_pool, primary_strategy, output])
            edges.insert(
                0,
                ModelRouterEdge(
                    id="start-primary-candidates",
                    source=start.id,
                    target=primary_pool.id,
                ),
            )
        else:
            condition_node = ModelRouterNode(
                id="condition",
                type="condition",
                label="Condition",
                position=ModelRouterPosition(x=260, y=220),
                condition=condition,
            )
            primary_pool, primary_strategy = add_branch(
                "primary",
                arguments.model_ids,
                arguments.strategy_type,
                arguments.routing_model_id,
                arguments.model_weights,
                60,
            )
            else_strategy = arguments.else_strategy_type or arguments.strategy_type
            else_pool, else_strategy_node = add_branch(
                "else",
                arguments.else_model_ids,
                else_strategy,
                arguments.else_routing_model_id,
                arguments.else_model_weights,
                380,
            )
            nodes.extend(
                [
                    condition_node,
                    primary_pool,
                    else_pool,
                    primary_strategy,
                    else_strategy_node,
                    output,
                ]
            )
            edges[0:0] = [
                ModelRouterEdge(
                    id="start-condition",
                    source=start.id,
                    target=condition_node.id,
                ),
                ModelRouterEdge(
                    id="condition-primary",
                    source=condition_node.id,
                    target=primary_pool.id,
                    label="Match",
                ),
                ModelRouterEdge(
                    id="condition-else",
                    source=condition_node.id,
                    target=else_pool.id,
                    label="Else",
                ),
            ]

        fallback_events: list[ModelRouterFallbackEvent] = list(arguments.fallback_on)
        definition = ModelRouterDefinition(
            presentation_version=6,
            default_model_id=arguments.model_ids[0],
            nodes=nodes,
            edges=edges,
        )
        if fallback_events:
            output.position = ModelRouterPosition(x=1260, y=124)
            fallback = ModelRouterNode(
                id="fallback",
                type="fallback",
                label="Fallback",
                position=ModelRouterPosition(x=1260, y=340),
                fallback_on=fallback_events,
            )
            nodes.append(fallback)
            for strategy_node in (node for node in nodes if node.type == "strategy"):
                edges.append(
                    ModelRouterEdge(
                        id=f"{strategy_node.id}-fallback",
                        source=strategy_node.id,
                        target=fallback.id,
                        source_handle="branch-exception",
                        label="异常策略",
                    )
                )
            definition = definition.model_copy(update={"nodes": nodes, "edges": edges})

        proposal = AssistantRouterProposal(
            id=uuid4(),
            name=arguments.name,
            description=arguments.description,
            strategy_type=arguments.strategy_type,
            else_strategy_type=arguments.else_strategy_type,
            condition=condition,
            fallback_on=fallback_events,
            models=proposal_models,
            definition=definition,
        )
        return ToolOutcome(
            payload={
                "proposal_id": str(proposal.id),
                "name": proposal.name,
                "status": "awaiting_user_confirmation",
                "models": [
                    {
                        "id": str(model.model_id),
                        "display_name": model.display_name,
                        "branch": model.branch,
                        "priority": model.priority,
                        "weight": model.weight,
                    }
                    for model in proposal.models
                ],
            },
            router_proposal=proposal,
            summary=f"Router proposal ready: {proposal.name}",
            row_count=len(proposal.models),
        )

    # -- catalog ---------------------------------------------------------------

    def list_entities(
        self, arguments: EntityArguments, timezone: str, locale: str
    ) -> ToolOutcome:
        catalog = self._entity_catalog()
        wanted = (
            ["organizations", "departments", "projects", "agents", "users"]
            if arguments.kind == "all"
            else [arguments.kind]
        )
        keyword = (arguments.keyword or "").strip().lower()
        result: dict[str, Any] = {}
        total = 0
        for kind in wanted:
            entries = catalog.get(kind) or []
            rows = [
                {"id": entry["id"], "name": entry["name"]}
                for entry in entries
                if not keyword
                or keyword in str(entry["name"]).lower()
                or keyword in str(entry["id"]).lower()
            ][:MAX_ROWS]
            result[kind] = rows
            total += len(rows)
        return ToolOutcome(
            payload=result,
            summary=text(locale, "entities_hit", count=total),
            row_count=total,
        )

    # -- rankings --------------------------------------------------------------

    def usage_ranking(
        self, arguments: UsageRankingArguments, timezone: str, locale: str
    ) -> ToolOutcome:
        window = resolve_range(arguments, timezone, locale)
        rows = self._repository.distribution(
            window.start,
            window.end,
            arguments.dimension,
            _filters(arguments),
            MAX_ROWS,
        )
        metric = arguments.metric
        # The repository orders by tokens; a cost or latency ranking must order by that.
        rows = sorted(rows, key=lambda row: -float(row[metric] or 0))[: arguments.limit]
        metric_label, unit = _pack(_METRIC_LABELS, locale)[metric]
        dimension_label = _pack(_DIMENSION_LABELS, locale)[arguments.dimension]
        names = [str(row["name"]) for row in rows]
        if arguments.dimension == "model" and len(set(names)) < len(names):
            # Two deployments can share a display name; the dashboard adds the connection.
            registry = self._repository.registry()
            runtimes = {str(item["id"]): str(item["name"]) for item in registry["runtimes"]}
            connections = {
                str(item["id"]): runtimes.get(str(item["runtime_id"]))
                for item in registry["models"]
            }
            names = [
                f"{name} · {connections[str(row['id'])]}"
                if names.count(name) > 1 and connections.get(str(row["id"])) else name
                for name, row in zip(names, rows, strict=True)
            ]
        chart_rows = [
            {
                "name": name,
                metric: float(row[metric]) if metric != "total_requests" else int(row[metric]),
                "share_percent": float(row["share_percent"]),
            }
            for name, row in zip(names, rows, strict=True)
        ]
        chart = ChartSpec(
            id=f"ranking-{arguments.dimension}-{metric}",
            # Decided on the rows that came back, never on the limit that was asked for.
            # The threshold is about legibility -- a bar chart of more than twenty
            # categories is a wall of ticks -- and legibility is a property of the answer.
            # Reading `arguments.limit` instead made "rank all models" a table even when
            # only eleven models existed, and left the reader with no way out: asking again
            # for a chart cannot help, because nothing tells the model that a smaller limit
            # is what changes the renderer.
            kind="table" if len(chart_rows) > 20 else "bar",
            title=text(locale, "ranking_title", dimension=dimension_label, metric=metric_label),
            time_range_label=window.label,
            basis=text(
                locale,
                "ranking_basis",
                dimension=dimension_label,
                scope=_scope_note(arguments, locale),
            ),
            unit=unit,
            category_key="name",
            category_label=dimension_label,
            series=[ChartSeries(key=metric, label=metric_label)],
            rows=chart_rows,
            generated_at=datetime.now(UTC),
            range_start=window.first,
            range_end=window.last,
            query=ChartQuery(
                tool="query_usage_ranking",
                arguments=arguments.model_dump(),
            ),
        )
        return ToolOutcome(
            payload={
                "time_range": window.label,
                "dimension": arguments.dimension,
                "metric": metric,
                "unit": unit,
                "rows": chart_rows,
            },
            chart=chart,
            summary=text(
                locale,
                "ranking_summary",
                dimension=dimension_label,
                metric=metric_label,
                count=len(chart_rows),
            ),
            row_count=len(chart_rows),
        )

    # -- trends ----------------------------------------------------------------

    def usage_trend(
        self, arguments: UsageTrendArguments, timezone: str, locale: str
    ) -> ToolOutcome:
        if arguments.metric not in {"total_tokens", "total_requests"}:
            raise ToolInputError(text(locale, "trend_metric_unsupported"))
        window = resolve_range(arguments, timezone, locale)
        raw = self._repository.trends(
            window.start,
            window.end,
            arguments.interval,
            arguments.dimension,
            timezone,
            _filters(arguments),
        )
        source = "calls" if arguments.metric == "total_requests" else "total_tokens"
        totals: dict[str, float] = {}
        buckets: dict[str, dict[str, Any]] = {}
        for point in raw["points"]:
            label = str(point["label"])
            value = float(point["totals"][source])
            totals[label] = totals.get(label, 0.0) + value
            bucket = str(point["bucket_start"])
            buckets.setdefault(bucket, {"bucket": bucket})[label] = value
        top = sorted(totals, key=lambda name: totals[name], reverse=True)[: arguments.limit]
        chart_rows = [
            {"bucket": row["bucket"], **{name: row.get(name, 0) for name in top}}
            for row in sorted(buckets.values(), key=lambda item: str(item["bucket"]))
        ]
        metric_label, unit = _pack(_METRIC_LABELS, locale)[arguments.metric]
        dimension_label = _pack(_DIMENSION_LABELS, locale)[arguments.dimension]
        interval_label = _pack(_INTERVAL_LABELS, locale)[arguments.interval]
        chart = ChartSpec(
            id=f"trend-{arguments.dimension}-{arguments.metric}",
            kind="line",
            title=text(
                locale,
                "trend_title",
                dimension=dimension_label,
                metric=metric_label,
                interval=interval_label,
            ),
            time_range_label=window.label,
            basis=text(
                locale,
                "trend_basis",
                interval=interval_label,
                dimension=dimension_label,
                count=len(top),
                scope=_scope_note(arguments, locale),
            ),
            unit=unit,
            category_key="bucket",
            category_label=text(locale, "time_field"),
            series=[ChartSeries(key=name, label=name) for name in top] or [
                ChartSeries(key=metric_label, label=metric_label)
            ],
            rows=chart_rows[:365],
            generated_at=datetime.now(UTC),
            range_start=window.first,
            range_end=window.last,
            query=ChartQuery(tool="query_usage_trend", arguments=arguments.model_dump()),
        )
        return ToolOutcome(
            payload={
                "time_range": window.label,
                "interval": arguments.interval,
                "metric": arguments.metric,
                "unit": unit,
                "series": top,
                "totals": {name: totals[name] for name in top},
                "bucket_count": len(chart_rows),
            },
            chart=chart,
            summary=text(
                locale,
                "trend_summary",
                dimension=dimension_label,
                buckets=len(chart_rows),
                series=len(top),
            ),
            row_count=len(chart_rows),
        )

    # -- summary ---------------------------------------------------------------

    def usage_summary(
        self, arguments: UsageSummaryArguments, timezone: str, locale: str
    ) -> ToolOutcome:
        window = resolve_range(arguments, timezone, locale)
        overview = self._repository.executive_overview(
            window.start, window.end, _filters(arguments)
        )
        totals = dict(overview["totals"])
        rows = [
            {
                "name": text(locale, "total_tokens"),
                "value": int(totals["total_tokens"]),
                "unit": "Token",
            },
            {
                "name": text(locale, "requests"),
                "value": int(totals["total_requests"]),
                "unit": text(locale, "calls_unit"),
            },
            {
                "name": text(locale, "cost"),
                "value": float(totals["estimated_cost"]),
                "unit": "USD",
            },
            {
                "name": text(locale, "latency"),
                "value": round(float(totals["average_latency_ms"]), 1),
                "unit": "ms",
            },
            {
                "name": text(locale, "error_rate"),
                "value": round(float(totals["error_rate"]), 2),
                "unit": "%",
            },
        ]
        chart = ChartSpec(
            id="summary-kpi",
            kind="kpi",
            title=text(locale, "summary_title"),
            time_range_label=window.label,
            basis=text(locale, "summary_basis", scope=_scope_note(arguments, locale)),
            unit="",
            category_key="name",
            category_label=text(locale, "metric_field"),
            series=[ChartSeries(key="value", label="值")],
            rows=rows,
            generated_at=datetime.now(UTC),
            range_start=window.first,
            range_end=window.last,
            query=ChartQuery(tool="query_usage_summary", arguments=arguments.model_dump()),
        )
        return ToolOutcome(
            payload={"time_range": window.label, "totals": rows},
            chart=chart,
            summary=text(locale, "summary_summary"),
            row_count=len(rows),
        )


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    arguments_model: type[ToolArguments]
    run: Callable[[AssistantTools, Any, str, str], ToolOutcome]


# Tool descriptions stay in English regardless of UI language: they are read by the
# model, not by a person, and the model's tool-selection quality is best against the
# language its function-calling was trained on.
REGISTRY: tuple[RegisteredTool, ...] = (
    RegisteredTool(
        name="list_enterprise_entities",
        description=(
            "List the real IDs and names of organizations, departments, projects, "
            "agents and people. When the user names a scope (for example 'AI Platform' "
            "or a person's name), call this FIRST to get the exact ID and pass that ID "
            "to the other tools. Never guess an ID."
        ),
        arguments_model=EntityArguments,
        run=lambda tools, arguments, timezone, locale: tools.list_entities(
            arguments, timezone, locale
        ),
    ),
    RegisteredTool(
        name="query_usage_ranking",
        description=(
            "Usage, cost, request count or latency ranked by one dimension. Use for "
            "top-N questions, 'which is highest', comparisons across departments, or "
            "'the most expensive model'. Returns a bar chart."
        ),
        arguments_model=UsageRankingArguments,
        run=lambda tools, arguments, timezone, locale: tools.usage_ranking(
            arguments, timezone, locale
        ),
    ),
    RegisteredTool(
        name="query_usage_trend",
        description=(
            "Usage or request count over time, bucketed hourly, daily or weekly and "
            "optionally split into series by a dimension. Use for 'what is the trend' "
            "or 'how has it changed recently'. Returns a line chart. "
            "Note: per-period cost is not available."
        ),
        arguments_model=UsageTrendArguments,
        run=lambda tools, arguments, timezone, locale: tools.usage_trend(
            arguments, timezone, locale
        ),
    ),
    RegisteredTool(
        name="query_usage_summary",
        description=(
            "Total tokens, request count, estimated cost, average latency and error "
            "rate for a scope and time window. Use for 'overall' or 'how much in "
            "total' questions. Returns metric cards."
        ),
        arguments_model=UsageSummaryArguments,
        run=lambda tools, arguments, timezone, locale: tools.usage_summary(
            arguments, timezone, locale
        ),
    ),
    RegisteredTool(
        name="list_router_models",
        description=(
            "List the real models the current user may include in a Model Router. "
            "Call this before proposing a Router. Never invent or reuse a model ID "
            "that is absent from this result. Optionally filter by capability."
        ),
        arguments_model=RouterModelCatalogArguments,
        run=lambda tools, arguments, timezone, locale: tools.list_router_models(
            arguments, timezone, locale
        ),
    ),
    RegisteredTool(
        name="propose_model_router",
        description=(
            "Create a non-mutating Model Router draft proposal for user confirmation. "
            "Use only model IDs returned by list_router_models. Use model_ids for one "
            "candidate pool. To make a real conditional Router, provide condition_field, "
            "condition_operator, condition_value and else_model_ids; the backend builds "
            "Match and Else branches. This tool never creates, publishes or assigns a Router."
        ),
        arguments_model=RouterProposalArguments,
        run=lambda tools, arguments, timezone, locale: tools.propose_model_router(
            arguments, timezone, locale
        ),
    ),
)

BY_NAME: dict[str, RegisteredTool] = {tool.name: tool for tool in REGISTRY}


def tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            function=ToolFunctionDefinition(
                name=tool.name,
                description=tool.description,
                parameters=tool.arguments_model.model_json_schema(),
            )
        )
        for tool in REGISTRY
    ]


def parse_arguments(
    tool: RegisteredTool, raw: Mapping[str, Any], locale: str = "en"
) -> ToolArguments:
    try:
        return tool.arguments_model.model_validate(raw)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()[:5]
        )
        raise ToolInputError(text(locale, "bad_arguments", details=details)) from error
