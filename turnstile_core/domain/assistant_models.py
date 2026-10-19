"""Contracts for the FinOps assistant.

The shape here encodes one decision that the rest of the feature depends on: **a chart
is authored by the tool that fetched its data, never by the model.** The model chooses
which tool to call and writes the prose around the result; it never emits chart rows or
a chart type. That is what makes "图表数据与后端查询结果一致" a structural property
rather than a prompt instruction the model may ignore.

The same decision is what makes Pin cheap. A pinned chart stores its `ChartQuery` — the
tool name plus the validated arguments — so refreshing is just running that tool again.
No SQL is persisted, so a pinned report cannot drift from what the assistant showed and
cannot become an injection surface.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .models import StrictModel
from .runtime_models import InvocationApiFormat

ChartKind = Literal["bar", "line", "table", "kpi"]

TimeRangePreset = Literal[
    "today",
    "yesterday",
    "last_7_days",
    "last_30_days",
    "last_90_days",
    "this_month",
    "last_month",
    "custom",
]

UsageDimension = Literal[
    "organization",
    "department",
    "project",
    "agent",
    "model",
    "user",
    "runtime",
]

UsageMetric = Literal["total_tokens", "estimated_cost", "total_requests", "average_latency_ms"]

TrendInterval = Literal["hour", "day", "week"]


class ChartSeries(StrictModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)


class ChartQuery(StrictModel):
    """Everything needed to reproduce a chart, and nothing else.

    Persisted verbatim by Pin. `arguments` has already been validated against the tool's
    own argument model before it lands here, so replaying it cannot widen what the
    original question was allowed to read.
    """

    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any]


class ChartSpec(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    kind: ChartKind
    title: str = Field(min_length=1, max_length=200)
    # PRD 5.4 requires every chart to state its range, basis and unit. They are fields
    # rather than prose so the pinned report renders them identically months later.
    time_range_label: str = Field(min_length=1, max_length=120)
    basis: str = Field(min_length=1, max_length=400)
    unit: str = Field(max_length=60)
    category_key: str = Field(min_length=1, max_length=64)
    category_label: str = Field(min_length=1, max_length=120)
    series: list[ChartSeries] = Field(min_length=1, max_length=12)
    rows: list[dict[str, Any]] = Field(max_length=500)
    generated_at: datetime
    # The calendar days this chart actually covered, inclusive. Present so pinning can
    # freeze a relative preset onto the window the reader saw; absent on charts stored
    # before that existed, which keep resolving their preset on every refresh.
    range_start: date | None = None
    range_end: date | None = None
    query: ChartQuery


class AssistantStep(StrictModel):
    """One tool call, surfaced in the UI so an answer can be audited."""

    sequence: int = Field(ge=1)
    tool: str
    arguments: dict[str, Any]
    status: Literal["ok", "error"]
    summary: str = Field(max_length=500)
    row_count: int = Field(default=0, ge=0)


class AssistantTurn(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class AssistantAskRequest(StrictModel):
    question: str = Field(min_length=1, max_length=4_000)
    history: list[AssistantTurn] = Field(default_factory=list, max_length=20)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=64)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    # The chart labels are authored by tools, so the answer and the chart would otherwise
    # be in different languages inside the same reply.
    locale: str = Field(default="en", min_length=2, max_length=16)


class AssistantReply(StrictModel):
    conversation_id: str
    message: str
    charts: list[ChartSpec]
    steps: list[AssistantStep]
    latency_ms: int = Field(ge=0)
    model: str
    # Present so the assistant's own spend is visible in the product it analyses.
    total_tokens: int = Field(default=0, ge=0)


class PinnedChartWrite(StrictModel):
    """Pin one chart.

    `report_id` decides between the two cases the dialog offers: absent means "start a
    new report", present means "add to that one". `title` is therefore only required in
    the first case — an existing report already has a name, and letting this field rename
    it would make adding a chart a destructive act.
    """

    report_id: UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    original_question: str = Field(min_length=1, max_length=4_000)
    chart: ChartSpec

    @model_validator(mode="after")
    def _new_report_needs_a_title(self) -> PinnedChartWrite:
        if self.report_id is None and not self.title:
            raise ValueError("A new report needs a title")
        return self


class PinnedChartRename(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class PinnedReportVisibilityWrite(StrictModel):
    visibility: Literal["private", "public"]


class PinnedChartOrder(StrictModel):
    """The report's charts, in their new order.

    The whole list is sent rather than a moved id and an index. A partial list would
    leave the charts it omitted on their old positions, and since ties break on
    `created_at` the result would be an order nobody chose. Sending everything makes the
    request state the outcome instead of describing an edit.
    """

    chart_ids: list[UUID] = Field(min_length=1)


PinnedColumnSpan = Annotated[int, Field(ge=1, le=12)]
PinnedRowIndex = Annotated[int, Field(ge=0)]
PinnedRowHeight = Annotated[int, Field(ge=200, le=1400)]


class PinnedReportLayout(StrictModel):
    """Canonical desktop layout shared by every viewer of one report."""

    version: Literal[1] = 1
    spans: dict[UUID, PinnedColumnSpan] = Field(default_factory=dict, max_length=500)
    row_heights: dict[PinnedRowIndex, PinnedRowHeight] = Field(
        default_factory=dict, max_length=500
    )


class PinnedChart(StrictModel):
    id: UUID
    report_id: UUID
    original_question: str
    chart: ChartSpec
    position: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class PinnedReport(StrictModel):
    """One nav item, holding one or more charts."""

    id: UUID
    title: str
    description: str
    owner_id: str
    visibility: Literal["private", "public"] = "private"
    can_manage: bool = False
    position: int = Field(ge=0)
    layout: PinnedReportLayout = Field(default_factory=PinnedReportLayout)
    created_at: datetime
    updated_at: datetime
    charts: list[PinnedChart] = Field(default_factory=list)


class PinnedReportList(StrictModel):
    items: list[PinnedReport]


class ConversationExchange(StrictModel):
    """One question and the reply it produced, as it was shown at the time."""

    position: int = Field(ge=0)
    question: str
    reply: AssistantReply
    created_at: datetime


class ConversationSummary(StrictModel):
    """A row in the history dropdown.

    Carries no turns: the dropdown lists conversations and would otherwise download every
    transcript, including their charts, to render a list of titles.
    """

    id: UUID
    title: str
    title_source: Literal["question", "model", "user"] = "question"
    owner_id: str
    turn_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class Conversation(ConversationSummary):
    exchanges: list[ConversationExchange] = Field(default_factory=list)


class ConversationList(StrictModel):
    items: list[ConversationSummary]


class ConversationRename(StrictModel):
    title: str = Field(min_length=1, max_length=200)


class ConversationTitleRequest(StrictModel):
    """Ask the model to name a conversation.

    The locale is the reader's current one rather than the one the conversation was held
    in, because the title is read now, in the sidebar they are looking at now.
    """

    locale: str = Field(default="en", min_length=2, max_length=16)


class AssistantSettingsWrite(StrictModel):
    """What an administrator can change about how the assistant runs."""

    model_id: UUID | None = None
    auto_title: bool = True
    api_format: InvocationApiFormat | None = None


class AssistantModelChoice(StrictModel):
    """A model the assistant is allowed to use, with what it costs to use it.

    The prices are here because choosing a model is a cost decision -- the registry spans
    $1.00 to $30.00 per million output tokens -- and a picker that hides them makes the
    expensive choice as easy as the cheap one.
    """

    id: UUID
    display_name: str
    model_key: str
    runtime_name: str
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    api_formats: list[InvocationApiFormat] = Field(default_factory=list)


class AssistantSettings(AssistantSettingsWrite):
    """The configuration plus what it actually resolves to right now.

    `effective_*` exists because the two can legitimately differ: a pinned model that is
    later disabled, or moved to a runtime that cannot carry tool calls, stops qualifying
    and the assistant falls back to automatic selection rather than refusing to answer.
    That fallback has to be visible somewhere or it is just a setting that quietly does
    nothing -- `model_available` is what the settings page reads to say so.

    `available_models` is served rather than derived in the browser on purpose. Deciding
    which models qualify is a server rule with three parts, one of which is the runtime's
    tool protocol; reimplementing it client-side would let the picker offer a model the
    server then rejects, and the two copies would drift on the next adapter added.
    """

    effective_model_id: UUID | None = None
    effective_model_name: str | None = None
    model_available: bool = True
    api_available: bool = True
    effective_api_format: InvocationApiFormat | None = None
    effective_api_path: str | None = None
    available_api_formats: list[InvocationApiFormat] = Field(default_factory=list)
    available_models: list[AssistantModelChoice] = Field(default_factory=list)
    updated_at: datetime | None = None
    updated_by: str | None = None
