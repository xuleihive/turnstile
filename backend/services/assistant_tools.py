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
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from turnstile_core.domain.assistant_models import (
    ChartQuery,
    ChartSeries,
    ChartSpec,
    TimeRangePreset,
    TrendInterval,
    UsageDimension,
    UsageMetric,
)
from turnstile_core.domain.data_access import DataAccessScope
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

    zone = _zone(timezone)
    now = datetime.now(zone)
    today = now.date()
    preset = arguments.time_range
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
        self, repository: QueryRepository, entity_catalog: Callable[[], Mapping[str, Any]],
        department_ids: tuple[str, ...] | None = None,
        data_scope: DataAccessScope | None = None,
    ):
        self._repository = repository
        self._entity_catalog = entity_catalog
        self._department_ids = department_ids
        self._data_scope = data_scope

    def _filters(self, arguments: TimeScopedArguments) -> UsageFilters:
        return replace(
            _filters(arguments),
            allowed_department_ids=None if self._data_scope is not None else self._department_ids,
            data_scope=self._data_scope,
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
            self._filters(arguments),
            arguments.limit,
        )
        metric = arguments.metric
        metric_label, unit = _pack(_METRIC_LABELS, locale)[metric]
        dimension_label = _pack(_DIMENSION_LABELS, locale)[arguments.dimension]
        chart_rows = [
            {
                "name": str(row["name"]),
                metric: float(row[metric]) if metric != "total_requests" else int(row[metric]),
                "share_percent": float(row["share_percent"]),
            }
            for row in rows
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
            self._filters(arguments),
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
            window.start, window.end, self._filters(arguments)
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
