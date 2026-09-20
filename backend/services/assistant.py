"""The assistant orchestrator: intent to tool calls to a grounded answer.

Two properties are load-bearing and should not be "simplified" away.

**The model never produces a number or a chart.** It picks tools and writes prose. Every
figure that reaches the screen came out of a repository query, and every chart was
authored by the tool that ran that query. So "不允许模型编造数据" is enforced by the data
flow rather than by asking the model nicely.

**The assistant pays for itself, visibly.** It calls the same gateway as every other
consumer, attributed to the asking employee under the `FinOps Assistant` agent. Its own
spend therefore lands in the dashboard it is analysing, and is subject to the same model
policy and budget admission as any other call. Excluding it would have been easy and
would have made this product dishonest about its own cost.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException

from turnstile_core.config import Settings
from turnstile_core.domain.assistant_models import (
    AssistantAskRequest,
    AssistantModelChoice,
    AssistantReply,
    AssistantSettings,
    AssistantSettingsWrite,
    AssistantStep,
    ChartSpec,
    Conversation,
    ConversationSummary,
    PinnedChartWrite,
    PinnedReport,
    PinnedReportLayout,
)
from turnstile_core.domain.enterprise import (
    governance_directory,
    merge_application_owners,
)
from turnstile_core.domain.models import EnterpriseEntityCatalog
from turnstile_core.domain.runtime_models import (
    ChatMessage,
    InvocationMetadata,
    ModelInvocationRequest,
    ToolCall,
)
from turnstile_core.integrations.gateway import elapsed_ms, supports_tool_calling
from turnstile_core.persistence.repository import QueryRepository

from .assistant_shared import (
    ANSWER_LANGUAGE,
    HISTORY_LIMIT,
    TITLE_LENGTH,
    TITLE_PROMPT,
    clean_title,
)
from .assistant_tools import (
    BY_NAME,
    AssistantTools,
    ToolInputError,
    parse_arguments,
    tool_definitions,
)
from .runtime_service import ModelRuntimeService

logger = logging.getLogger(__name__)

# Four is enough for "resolve the department id, then query it, then maybe compare".
# An unbounded loop is a way to spend an employee's whole month on one question.
MAX_TOOL_ROUNDS = 4
ASSISTANT_AGENT_ID = "agent-finops-assistant"
ASSISTANT_AGENT_NAME = "FinOps Assistant"
ASSISTANT_REQUEST_SOURCE = "assistant"
# Titling is separated from answering in telemetry so the cost of naming threads can be
# read on its own. It is the same agent doing the same job, so it is not a second agent.
TITLE_REQUEST_SOURCE = "assistant-title"

# The dropdown is a way back to a recent thread, not an archive browser. Reading every
# conversation an owner has ever had to render a list would grow without bound, and the
# rows past the first screenful are reached by scrolling a list nobody scrolls.
# The prompt is English regardless of UI language, with the answer language stated as an
# instruction. Keeping one prompt means one set of rules to maintain; translating it per
# locale would let the "never invent a number" rule drift between languages, and that is
# the rule the whole feature rests on.
SYSTEM_PROMPT = """You are the FinOps analysis assistant for the Turnstile platform.
You help IT administrators and cost owners understand AI usage and spend.

Rules:
1. Every number you state must come from a tool result. You have no built-in data and
   must never estimate, recall or invent a figure.
2. When the user names an organization, department, project, agent or person, call
   list_enterprise_entities first to get the exact ID, then pass that ID as a filter to
   the other tools. Never guess an ID.
3. If the user gives no time range, use last_30_days and say which range you used.
4. If the question is missing something you cannot reasonably default, ask the user for
   it instead of guessing an answer.
5. The tools have already produced charts and the UI renders them. Your text should give
   the conclusion, the key figures and anything worth noticing. Do not repeat the chart
   contents as a Markdown table.
6. Be concise and direct: two to four sentences, a short list only when it helps.
7. If a tool returns no rows, say plainly that there is no data in that scope and offer a
   likely reason such as too narrow a window or too strict a filter. Never fabricate.
8. Write your answer in {language}.
"""

# Only the answer language varies. Anything unlisted falls back to English rather than to
# Chinese, so an unexpected locale degrades to a language the reader probably shares.
def _frozen_range(chart: ChartSpec) -> ChartSpec:
    """Rewrite a relative time range onto the dates the chart actually covered.

    A pin is a decision to keep *this* answer, and a relative preset does not keep it: a
    report pinned as "this month" re-resolves on 1 August to a single day, and one pinned
    as "last 7 days" silently answers a different question every morning. The window the
    reader agreed to is the one that gets stored.

    Refresh stays meaningful because the numbers for a fixed window are not fixed --
    streamed requests land with zero usage and are reconciled hours later, so re-running
    yesterday's range legitimately returns more than it did yesterday.

    Expressed as the existing `custom` preset rather than a new field, so the stored query
    remains an ordinary validated tool call and replay needs no special case. Charts made
    before `range_start` existed carry no dates and are left alone: guessing their window
    now would freeze them to today's, which is the bug rather than the fix.
    """
    if chart.range_start is None or chart.range_end is None:
        return chart
    if chart.query.arguments.get("time_range") == "custom":
        return chart
    arguments = {
        **chart.query.arguments,
        "time_range": "custom",
        "start_date": chart.range_start.isoformat(),
        "end_date": chart.range_end.isoformat(),
    }
    return chart.model_copy(
        update={"query": chart.query.model_copy(update={"arguments": arguments})}
    )


class AssistantService:
    def __init__(
        self,
        repository: QueryRepository,
        runtime: ModelRuntimeService,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._settings = settings
        self._tools = AssistantTools(repository, self._catalog_payload)

    # -- catalog ---------------------------------------------------------------

    def _catalog(self) -> EnterpriseEntityCatalog:
        return merge_application_owners(
            governance_directory(
                self._repository.observed_users(),
                include_seeded_people=self._settings.seed_demo_directory,
                units=self._repository.org_units(),
            ),
            self._repository.application_owners(),
        )

    def _catalog_payload(self) -> dict[str, Any]:
        catalog = self._catalog()
        return {
            "organizations": [item.model_dump() for item in catalog.organizations],
            "departments": [item.model_dump() for item in catalog.departments],
            "projects": [item.model_dump() for item in catalog.projects],
            "agents": [item.model_dump() for item in catalog.agents],
            "users": [item.model_dump() for item in catalog.users],
        }

    # -- model selection -------------------------------------------------------

    def _eligible_models(self) -> list[Any]:
        """Registry models that can actually carry this assistant's tool calls.

        Declaring the requirement matters: a model without tool support answers from
        nothing but the prompt, which is the exact failure this design exists to prevent.
        Capability alone is not enough either: a CLI-backed model can declare `tools`
        while its runtime has no tool protocol, so the runtime is checked too.
        """
        registry = self._runtime.registry()
        runtimes = {
            runtime.id
            for runtime in registry.runtimes
            if runtime.enabled
            and supports_tool_calling(runtime.runtime_kind.value, runtime.config)
        }
        candidates = [
            model
            for model in registry.models
            if model.enabled
            and "tools" in model.capabilities
            and model.runtime_id in runtimes
            and "image_generation" not in model.capabilities
        ]
        candidates.sort(key=lambda model: (not model.is_default, model.display_name))
        return candidates

    def _select_model(self) -> tuple[UUID, UUID, str]:
        """Resolve the model to call: the configured one, or the registry's own order.

        A configured model is re-checked against the eligibility rule on every call
        rather than trusted because it was valid when it was saved. Disabling it in Model
        Management, or moving it to a CLI runtime, would otherwise turn every question
        into a 409. Falling back keeps the assistant answering; the settings page reads
        `model_available` so the fallback is visible rather than silent.
        """
        candidates = self._eligible_models()
        if not candidates:
            raise HTTPException(
                status_code=409,
                detail="No enabled model declares the tools capability",
            )
        configured = self._repository.assistant_settings().get("model_id")
        chosen = next(
            (model for model in candidates if model.id == configured), candidates[0]
        )
        return chosen.runtime_id, chosen.id, chosen.model_key

    # -- settings --------------------------------------------------------------

    def settings(self) -> AssistantSettings:
        stored = self._repository.assistant_settings()
        configured = stored.get("model_id")
        candidates = self._eligible_models()
        pinned = next((model for model in candidates if model.id == configured), None)
        effective = pinned or (candidates[0] if candidates else None)
        return AssistantSettings(
            model_id=configured,
            auto_title=bool(stored.get("auto_title", True)),
            effective_model_id=effective.id if effective else None,
            effective_model_name=effective.display_name if effective else None,
            # True when nothing is pinned, because automatic selection cannot be
            # unavailable -- the empty-registry case is reported by a null effective model.
            model_available=configured is None or pinned is not None,
            available_models=[
                AssistantModelChoice(
                    id=model.id,
                    display_name=model.display_name,
                    model_key=model.model_key,
                    runtime_name=model.runtime_name,
                    input_cost_per_million=model.input_cost_per_million,
                    output_cost_per_million=model.output_cost_per_million,
                )
                for model in candidates
            ],
            updated_at=stored.get("updated_at"),
            updated_by=stored.get("updated_by"),
        )

    def save_settings(self, write: AssistantSettingsWrite, updated_by: str) -> AssistantSettings:
        # Validated on write as well as on read. Read-time fallback exists for a model
        # that stopped qualifying later; it is not a licence to store one that never did.
        if write.model_id is not None and not any(
            model.id == write.model_id for model in self._eligible_models()
        ):
            raise HTTPException(
                status_code=409,
                detail="That model is not enabled, or its runtime cannot carry tool calls",
            )
        self._repository.save_assistant_settings(
            model_id=write.model_id, auto_title=write.auto_title, updated_by=updated_by
        )
        return self.settings()

    # -- ask -------------------------------------------------------------------

    def ask(
        self,
        request: AssistantAskRequest,
        *,
        user_id: str,
        user_name: str,
    ) -> AssistantReply:
        started = time.monotonic()
        conversation_id = self._resolve_conversation(request.conversation_id, user_id)
        runtime_id, model_id, model_key = self._select_model()
        language = ANSWER_LANGUAGE.get(request.locale, "English")

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=SYSTEM_PROMPT.format(language=language))
        ]
        for turn in request.history:
            messages.append(ChatMessage(role=turn.role, content=turn.content))
        messages.append(ChatMessage(role="user", content=request.question))

        tools = tool_definitions()
        steps: list[AssistantStep] = []
        charts: list[ChartSpec] = []
        total_tokens = 0
        answer = ""

        for turn_index in range(1, MAX_TOOL_ROUNDS + 1):
            response = self._runtime.invoke(
                ModelInvocationRequest(
                    metadata=self._metadata(
                        user_id=user_id,
                        user_name=user_name,
                        model_id=str(model_id),
                        model=model_key,
                        conversation_id=conversation_id,
                        turn_index=turn_index,
                    ),
                    runtime_id=runtime_id,
                    model_id=model_id,
                    messages=messages,
                    tools=tools,
                    temperature=0,
                    max_output_tokens=1200,
                ),
                enforce_user_model_access=False,
            )
            if response.usage is not None:
                total_tokens += (
                    response.usage.input_tokens
                    + response.usage.cached_tokens
                    + response.usage.output_tokens
                )
            answer = response.content
            if not response.tool_calls:
                break
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                outcome, step = self._run_tool(
                    call, request.timezone, request.locale, len(steps) + 1
                )
                steps.append(step)
                if outcome is not None and outcome.chart is not None:
                    charts.append(outcome.chart)
                messages.append(
                    ChatMessage(
                        role="tool",
                        tool_call_id=call.id,
                        content=json.dumps(
                            outcome.payload if outcome else {"error": step.summary},
                            ensure_ascii=False,
                            default=str,
                        )[:20_000],
                    )
                )
        else:
            # The loop ran out of rounds while the model was still calling tools. Say so
            # rather than presenting a partial answer as complete.
            answer = answer or "Too many analysis steps. Please narrow the question."

        reply = AssistantReply(
            conversation_id=conversation_id,
            message=answer.strip() or "No usable conclusion was produced. Try rephrasing.",
            charts=charts,
            steps=steps,
            latency_ms=elapsed_ms(started),
            model=model_key,
            total_tokens=total_tokens,
        )
        self._record_turn(conversation_id, user_id, request.question, reply)
        return reply

    # -- conversations ---------------------------------------------------------

    def _resolve_conversation(self, requested: str | None, owner_id: str) -> str:
        """Decide which conversation this question belongs to.

        A caller-supplied id is only adopted if it is a well-formed id this owner already
        has. Anything else starts a new conversation rather than failing: the id is a
        thread handle, not an authorization decision, and a stale one left in a browser
        tab should cost the person a new thread, not an error page.
        """
        if requested is None:
            return str(uuid4())
        try:
            candidate = UUID(requested)
        except ValueError:
            return str(uuid4())
        if self._repository.get_conversation(candidate, owner_id) is None:
            return str(uuid4())
        return str(candidate)

    def _record_turn(
        self, conversation_id: str, owner_id: str, question: str, reply: AssistantReply
    ) -> None:
        """Persist the exchange, and never let that failure become the answer's failure.

        The person has already been charged for the tokens and the answer is on screen;
        losing it from history is worth strictly less than losing the answer itself. The
        same reasoning the denial-trace writes follow.
        """
        try:
            self._repository.append_conversation_turn(
                conversation_id=UUID(conversation_id),
                owner_id=owner_id,
                # The first turn names the conversation. No extra model call to summarise
                # it: that would spend tokens on every new thread to restate a question
                # the person just typed.
                title=question.strip()[:TITLE_LENGTH] or "Untitled",
                question=question,
                reply=reply.model_dump(mode="json"),
            )
        except Exception:  # noqa: BLE001 - telemetry must not break the reply
            logger.exception("Failed to record assistant conversation turn")

    def list_conversations(self, owner_id: str) -> list[ConversationSummary]:
        return [
            ConversationSummary.model_validate(row)
            for row in self._repository.list_conversations(owner_id, HISTORY_LIMIT)
        ]

    def get_conversation(self, conversation_id: UUID, owner_id: str) -> Conversation:
        row = self._repository.get_conversation(conversation_id, owner_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        turns = row.get("turns", [])
        return Conversation.model_validate(
            {k: v for k, v in row.items() if k != "turns"}
            | {"exchanges": [dict(turn) for turn in turns]}
        )

    def rename_conversation(
        self, conversation_id: UUID, owner_id: str, title: str
    ) -> ConversationSummary:
        row = self._repository.rename_conversation(conversation_id, owner_id, title)
        if row is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return ConversationSummary.model_validate(row)

    def title_conversation(
        self, conversation_id: UUID, owner_id: str, locale: str
    ) -> ConversationSummary:
        """Replace the placeholder title with one the model wrote.

        This is a separate endpoint rather than a step inside `ask` on purpose. The answer
        is the product, and putting a second model call in front of it would delay every
        first question by the length of a round trip to name a thread the reader can
        already see. Doing it in a background task instead would race the history refetch
        the client fires the moment the answer lands. So the client asks for a name once
        it has an answer on screen, and pays nothing on any later turn.
        """
        conversation = self.get_conversation(conversation_id, owner_id)
        # Never re-summarise. A model title is final, a typed one is the reader's, and an
        # empty thread has nothing to summarise. Turning naming off has to be checked here
        # too, not only in the client: the setting is a spending decision, and a stale tab
        # would otherwise keep buying titles after an administrator switched it off. Each
        # of these returns the current state rather than an error: the caller asked for a
        # name and a name is what it gets.
        if (
            conversation.title_source != "question"
            or not conversation.exchanges
            or not self._repository.assistant_settings().get("auto_title", True)
        ):
            return ConversationSummary.model_validate(
                conversation.model_dump(exclude={"exchanges"})
            )

        title = self._summarise_title(conversation, owner_id, locale)
        if title is None:
            return ConversationSummary.model_validate(
                conversation.model_dump(exclude={"exchanges"})
            )
        row = self._repository.summarise_conversation_title(
            conversation_id, owner_id, title
        )
        if row is None:
            # Renamed between the read and the write. The person's name wins.
            return self.list_one(conversation_id, owner_id)
        return ConversationSummary.model_validate(row)

    def list_one(self, conversation_id: UUID, owner_id: str) -> ConversationSummary:
        conversation = self.get_conversation(conversation_id, owner_id)
        return ConversationSummary.model_validate(
            conversation.model_dump(exclude={"exchanges"})
        )

    def _summarise_title(
        self, conversation: Conversation, owner_id: str, locale: str
    ) -> str | None:
        """One small, tool-free call. Returns None when the result is not usable."""
        first = conversation.exchanges[0]
        language = ANSWER_LANGUAGE.get(locale, "English")
        try:
            runtime_id, model_id, model_key = self._select_model()
            response = self._runtime.invoke(
                ModelInvocationRequest(
                    metadata=self._metadata(
                        user_id=owner_id,
                        user_name=owner_id,
                        model_id=str(model_id),
                        model=model_key,
                        conversation_id=str(conversation.id),
                        # Same thread and same turn: this call exists because of the
                        # first exchange and its cost belongs to it. What separates the
                        # two in telemetry is the request source, not a synthetic index
                        # -- `turn_index` is constrained to start at 1, and inventing a
                        # zeroth turn to mean "not a turn" would be a second meaning for
                        # a field that already has one.
                        turn_index=1,
                        request_source=TITLE_REQUEST_SOURCE,
                        workflow="assistant-title",
                    ),
                    runtime_id=runtime_id,
                    model_id=model_id,
                    messages=[
                        ChatMessage(
                            role="system",
                            content=TITLE_PROMPT.format(language=language),
                        ),
                        ChatMessage(
                            role="user",
                            content=(
                                f"Question: {first.question[:600]}\n\n"
                                f"Answer: {first.reply.message[:600]}"
                            ),
                        ),
                    ],
                    temperature=0,
                    # A title cannot be long, so neither can the budget for one. This is
                    # also the backstop against a model that ignores the prompt and starts
                    # writing the answer again.
                    max_output_tokens=32,
                ),
                enforce_user_model_access=False,
            )
        except Exception:  # noqa: BLE001 - a name is worth less than the conversation
            logger.exception("Failed to summarise conversation title")
            return None
        return clean_title(response.content)

    def delete_conversation(self, conversation_id: UUID, owner_id: str) -> None:
        if not self._repository.delete_conversation(conversation_id, owner_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
    def _run_tool(
        self, call: ToolCall, timezone: str, locale: str, sequence: int
    ) -> tuple[Any | None, AssistantStep]:
        name = call.function.name
        try:
            raw = json.loads(call.function.arguments or "{}")
            if not isinstance(raw, dict):
                raise ToolInputError("Arguments must be a JSON object")
            tool = BY_NAME.get(name)
            if tool is None:
                raise ToolInputError(f"Unknown tool: {name}")
            arguments = parse_arguments(tool, raw, locale)
            outcome = tool.run(self._tools, arguments, timezone, locale)
        except (ToolInputError, json.JSONDecodeError) as error:
            # Handed back to the model as a tool result so it can correct itself. An
            # exception here would turn a fixable argument mistake into a failed answer.
            return None, AssistantStep(
                sequence=sequence,
                tool=name,
                arguments={},
                status="error",
                summary=str(error)[:500],
            )
        except Exception as error:  # noqa: BLE001 - a broken tool must not kill the turn
            logger.exception("Assistant tool %s failed", name)
            return None, AssistantStep(
                sequence=sequence,
                tool=name,
                arguments={},
                status="error",
                summary=f"Tool execution failed: {type(error).__name__}",
            )
        return outcome, AssistantStep(
            sequence=sequence,
            tool=name,
            arguments=arguments.model_dump(exclude_none=True),
            status="ok",
            summary=outcome.summary,
            row_count=outcome.row_count,
        )

    def _metadata(
        self,
        *,
        user_id: str,
        user_name: str,
        model_id: str,
        model: str,
        conversation_id: str,
        turn_index: int,
        request_source: str = ASSISTANT_REQUEST_SOURCE,
        workflow: str = "assistant-analysis",
    ) -> InvocationMetadata:
        catalog = self._catalog()
        user = next((item for item in catalog.users if item.id == user_id), None)
        department = next(
            (item for item in catalog.departments if item.id == (user.parent_id if user else None)),
            None,
        )
        organization = catalog.organizations[0]
        return InvocationMetadata(
            organization_id=organization.id,
            organization=organization.name,
            department_id=department.id if department else "unattributed",
            department=department.name if department else "unattributed",
            project_id="project-finops",
            project="Model FinOps",
            agent_id=ASSISTANT_AGENT_ID,
            agent=ASSISTANT_AGENT_NAME,
            user_id=user_id,
            user=user_name,
            workflow=workflow,
            model_id=model_id,
            model=model,
            runtime="unattributed",
            request_source=request_source,
            run_id=conversation_id,
            turn_index=turn_index,
        )

    # -- pinned reports --------------------------------------------------------

    def list_pinned(self, owner_id: str) -> list[PinnedReport]:
        return [
            PinnedReport.model_validate(row)
            for row in self._repository.list_pinned_reports(owner_id)
        ]

    def pin(self, write: PinnedChartWrite, owner_id: str) -> PinnedReport:
        if write.chart.query.tool not in BY_NAME:
            raise HTTPException(status_code=422, detail="Chart is not backed by a known tool")
        write = write.model_copy(update={"chart": _frozen_range(write.chart)})
        if write.report_id is not None:
            self._managed_pinned(write.report_id, owner_id)
            added = self._repository.add_chart_to_pinned_report(
                report_id=write.report_id,
                owner_id=owner_id,
                original_question=write.original_question,
                chart=write.chart.model_dump(mode="json"),
            )
            if added is None:
                raise HTTPException(status_code=404, detail="Pinned report not found")
            # Return the whole report, not the chart: the caller needs the collection to
            # render the nav item and the report page, and a bare chart would make it
            # fetch again to learn what it now belongs to.
            return self.get_pinned(write.report_id, owner_id)
        assert write.title is not None  # guarded by PinnedChartWrite's validator
        row = self._repository.create_pinned_report(
            owner_id=owner_id,
            title=write.title,
            description=write.description,
            original_question=write.original_question,
            chart=write.chart.model_dump(mode="json"),
        )
        return PinnedReport.model_validate(row)

    def get_pinned(self, report_id: UUID, owner_id: str) -> PinnedReport:
        row = self._repository.get_pinned_report(report_id, owner_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Pinned report not found")
        return PinnedReport.model_validate(row)

    def _managed_pinned(self, report_id: UUID, owner_id: str) -> PinnedReport:
        report = self.get_pinned(report_id, owner_id)
        if not report.can_manage:
            raise HTTPException(status_code=403, detail="Only the report creator can modify it")
        return report

    def rename_pinned(
        self, report_id: UUID, owner_id: str, title: str, description: str
    ) -> PinnedReport:
        self._managed_pinned(report_id, owner_id)
        row = self._repository.update_pinned_report(report_id, owner_id, title, description)
        if row is None:
            raise HTTPException(status_code=404, detail="Pinned report not found")
        return PinnedReport.model_validate(row)

    def set_pinned_visibility(
        self, report_id: UUID, owner_id: str, visibility: str
    ) -> PinnedReport:
        self._managed_pinned(report_id, owner_id)
        row = self._repository.update_pinned_report_visibility(
            report_id, owner_id, visibility
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Pinned report not found")
        return PinnedReport.model_validate(row)

    def set_pinned_layout(
        self, report_id: UUID, owner_id: str, layout: PinnedReportLayout
    ) -> PinnedReport:
        report = self._managed_pinned(report_id, owner_id)
        chart_ids = {chart.id for chart in report.charts}
        unknown = set(layout.spans) - chart_ids
        if unknown:
            raise HTTPException(
                status_code=422,
                detail="Layout contains a chart that is not in this report",
            )
        row = self._repository.update_pinned_report_layout(
            report_id,
            owner_id,
            layout.model_dump(mode="json"),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Pinned report not found")
        return PinnedReport.model_validate(row)

    def unpin(self, report_id: UUID, owner_id: str) -> None:
        self._managed_pinned(report_id, owner_id)
        if not self._repository.delete_pinned_report(report_id, owner_id):
            raise HTTPException(status_code=404, detail="Pinned report not found")

    def remove_chart(self, report_id: UUID, chart_id: UUID, owner_id: str) -> PinnedReport:
        report = self._managed_pinned(report_id, owner_id)
        if not any(chart.id == chart_id for chart in report.charts):
            raise HTTPException(status_code=404, detail="Pinned chart not found")
        if len(report.charts) == 1:
            # A report with no charts is a nav item that opens an empty page. Removing the
            # last one means removing the report, which is what the user is asking for.
            raise HTTPException(
                status_code=409,
                detail="A report needs at least one chart; delete the report instead",
            )
        self._repository.delete_pinned_chart(chart_id, owner_id)
        return self.get_pinned(report_id, owner_id)

    def refresh_pinned(
        self, report_id: UUID, owner_id: str, timezone: str, locale: str = "en"
    ) -> PinnedReport:
        """Re-run every chart in the report against its own stored query.

        Each stored `ChartQuery` is replayed through the same validation the assistant
        used, so a pinned report can never read more than the questions that created it.
        Relative presets re-resolve, which is the point: "this month" means this month now.

        A chart that cannot be rebuilt keeps the data it already had rather than failing
        the whole report — one retired tool should not make the other charts unreadable.
        The refresh only fails outright when nothing at all could be rebuilt.
        """
        report = self._managed_pinned(report_id, owner_id)
        rebuilt = 0
        for pinned in report.charts:
            tool = BY_NAME.get(pinned.chart.query.tool)
            if tool is None:
                continue
            try:
                arguments = parse_arguments(tool, pinned.chart.query.arguments, locale)
                outcome = tool.run(self._tools, arguments, timezone, locale)
            except ToolInputError:
                continue
            if outcome.chart is None:
                continue
            self._repository.replace_pinned_chart_data(
                pinned.id, owner_id, outcome.chart.model_dump(mode="json")
            )
            rebuilt += 1
        if rebuilt == 0:
            raise HTTPException(status_code=409, detail="No chart in this report could be rebuilt")
        return self.get_pinned(report_id, owner_id)

    def reorder_pinned(self, owner_id: str, ordered_ids: Sequence[UUID]) -> list[PinnedReport]:
        for report_id in ordered_ids:
            self._managed_pinned(report_id, owner_id)
        self._repository.reorder_pinned_reports(owner_id, ordered_ids)
        return self.list_pinned(owner_id)

    def reorder_charts(
        self, report_id: UUID, owner_id: str, ordered_ids: Sequence[UUID]
    ) -> PinnedReport:
        """Set the display order of a report's charts.

        The submitted list has to name every chart in the report exactly once. Anything
        else is rejected rather than partially applied: positions the request omitted
        would keep their old values, and because the read orders by `(position,
        created_at)` the surviving ties resolve by creation time -- an order the caller
        never asked for and cannot predict from what it sent.
        """
        report = self._managed_pinned(report_id, owner_id)
        if sorted(str(chart.id) for chart in report.charts) != sorted(
            str(chart_id) for chart_id in ordered_ids
        ):
            raise HTTPException(
                status_code=422,
                detail="The order must list every chart in this report exactly once",
            )
        self._repository.reorder_pinned_charts(report_id, owner_id, ordered_ids)
        return self.get_pinned(report_id, owner_id)
