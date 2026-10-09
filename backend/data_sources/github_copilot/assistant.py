"""Isolated, read-only assistant for the GitHub Copilot billing domain."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from turnstile_core.domain.assistant_models import (
    AssistantAskRequest,
    AssistantReply,
    AssistantStep,
    ChartQuery,
    ChartSeries,
    ChartSpec,
    Conversation,
    ConversationSummary,
)
from turnstile_core.domain.runtime_models import (
    ChatMessage,
    InvocationMetadata,
    ModelInvocationRequest,
    ToolCall,
    ToolDefinition,
    ToolFunctionDefinition,
)
from turnstile_core.integrations.gateway import elapsed_ms
from turnstile_core.integrations.gateway_protocol import invocation_api_paths, registry_model_route

from ...services.assistant_shared import (
    ANSWER_LANGUAGE,
    HISTORY_LIMIT,
    TITLE_LENGTH,
    TITLE_PROMPT,
    ToolOutcome,
    clean_title,
)
from ...services.runtime_service import ModelRuntimeService
from .contracts import CopilotDashboard
from .service import (
    CopilotIdentityRequiredError,
    CopilotNotConfiguredError,
    CopilotService,
)

logger = logging.getLogger(__name__)

COPILOT_OWNER_PREFIX = "github-copilot:"
COPILOT_AGENT_ID = "agent-github-copilot-finops-assistant"
COPILOT_AGENT_NAME = "GitHub Copilot FinOps Assistant"
MAX_TOOL_ROUNDS = 4

SYSTEM_PROMPT = """You are the GitHub Copilot FinOps assistant inside Turnstile.
Use only the supplied read-only tools. Every number must come from a tool result.
Never claim that APIM token telemetry is GitHub organization billing. Never ask to
mutate seats or budgets. If identity or organization configuration is missing, explain
the exact missing step. Keep answers concise and write in {language}.
"""


class ConversationStore(Protocol):
    def list_conversations(self, owner_id: str, limit: int) -> Sequence[dict[str, Any]]: ...
    def get_conversation(
        self, conversation_id: UUID, owner_id: str
    ) -> dict[str, Any] | None: ...
    def append_conversation_turn(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        title: str,
        question: str,
        reply: Mapping[str, Any],
    ) -> None: ...
    def rename_conversation(
        self, conversation_id: UUID, owner_id: str, title: str
    ) -> dict[str, Any] | None: ...
    def summarise_conversation_title(
        self, conversation_id: UUID, owner_id: str, title: str
    ) -> dict[str, Any] | None: ...
    def delete_conversation(self, conversation_id: UUID, owner_id: str) -> bool: ...


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyArguments(ToolArguments):
    pass


class MemberArguments(ToolArguments):
    login: str | None = Field(default=None, max_length=39)
    limit: int = Field(default=20, ge=1, le=50)


class BreakdownArguments(ToolArguments):
    dimension: Literal["model", "feature", "language", "ide"] = "model"
    metric: Literal[
        "interactions", "generations", "acceptances", "loc_added"
    ] = "interactions"
    limit: int = Field(default=10, ge=1, le=20)


class RegisteredTool:
    def __init__(
        self, name: str, description: str, arguments: type[ToolArguments]
    ) -> None:
        self.name = name
        self.description = description
        self.arguments = arguments


TOOLS = (
    RegisteredTool(
        "query_copilot_overview",
        "Read seats, users, interactions, acceptance, code change and billing totals.",
        EmptyArguments,
    ),
    RegisteredTool(
        "query_copilot_members",
        "List Copilot members, activity, adoption phase and budget state.",
        MemberArguments,
    ),
    RegisteredTool(
        "query_copilot_breakdown",
        "Rank usage by model, feature, language or IDE using a selected metric.",
        BreakdownArguments,
    ),
    RegisteredTool(
        "query_copilot_budgets",
        "Read member budget, consumed and remaining amounts. Read-only.",
        MemberArguments,
    ),
    RegisteredTool(
        "query_copilot_budget_requests",
        "Read Turnstile Copilot budget request counts and status. Read-only.",
        EmptyArguments,
    ),
)
BY_NAME = {tool.name: tool for tool in TOOLS}


def tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            function=ToolFunctionDefinition(
                name=tool.name,
                description=tool.description,
                parameters=tool.arguments.model_json_schema(),
            )
        )
        for tool in TOOLS
    ]


class CopilotAssistantService:
    def __init__(
        self,
        repository: ConversationStore,
        runtime: ModelRuntimeService,
        copilot: CopilotService,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._copilot = copilot

    @staticmethod
    def owner(user_id: str) -> str:
        return f"{COPILOT_OWNER_PREFIX}{user_id}"

    def _select_model(self) -> tuple[UUID, UUID, str]:
        registry = self._runtime.registry()
        runtimes = {
            runtime.id
            for runtime in registry.runtimes
            if runtime.enabled
        }
        models = [
            model
            for model in registry.models
            if model.enabled
            and "tools" in model.capabilities
            and model.runtime_id in runtimes
            and "image_generation" not in model.capabilities
            and invocation_api_paths(registry_model_route(registry, model))
        ]
        models.sort(key=lambda model: (not model.is_default, model.display_name))
        if not models:
            raise HTTPException(
                status_code=409,
                detail="No enabled model declares the tools capability",
            )
        chosen = models[0]
        return chosen.runtime_id, chosen.id, chosen.model_key

    def _resolve_conversation(self, requested: str | None, owner: str) -> str:
        if requested is None:
            return str(uuid4())
        try:
            candidate = UUID(requested)
        except ValueError:
            return str(uuid4())
        if self._repository.get_conversation(candidate, owner) is None:
            return str(uuid4())
        return str(candidate)

    async def ask(
        self,
        request: AssistantAskRequest,
        *,
        user_uuid: UUID,
        user_email: str,
        user_name: str,
        role: str,
    ) -> AssistantReply:
        started = time.monotonic()
        owner = self.owner(user_email)
        status = await asyncio.to_thread(self._copilot.status, user_uuid, role)
        if not status.configured:
            raise CopilotNotConfiguredError(
                "GitHub Copilot organization is not configured"
            )
        if role != "owner" and not status.viewer_github_login:
            raise CopilotIdentityRequiredError(
                "Link a GitHub account before using the Copilot assistant"
            )
        conversation_id = await asyncio.to_thread(
            self._resolve_conversation, request.conversation_id, owner
        )
        runtime_id, model_id, model_key = await asyncio.to_thread(self._select_model)
        language = ANSWER_LANGUAGE.get(request.locale, "English")
        messages = [
            ChatMessage(role="system", content=SYSTEM_PROMPT.format(language=language))
        ]
        messages.extend(
            ChatMessage(role=turn.role, content=turn.content) for turn in request.history
        )
        messages.append(ChatMessage(role="user", content=request.question))
        steps: list[AssistantStep] = []
        charts: list[ChartSpec] = []
        total_tokens = 0
        answer = ""
        dashboard: CopilotDashboard | None = None

        for turn_index in range(1, MAX_TOOL_ROUNDS + 1):
            response = await asyncio.to_thread(
                self._runtime.invoke,
                ModelInvocationRequest(
                    metadata=self._metadata(
                        user_email,
                        user_name,
                        str(model_id),
                        model_key,
                        conversation_id,
                        turn_index,
                    ),
                    runtime_id=runtime_id,
                    model_id=model_id,
                    messages=messages,
                    tools=tool_definitions(),
                    temperature=0,
                    max_output_tokens=1200,
                )
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
            messages.append(response.assistant_message())
            for call in response.tool_calls:
                if dashboard is None:
                    dashboard = await self._copilot.dashboard(
                        user_id=user_uuid,
                        role=role,
                    )
                outcome, step = await self._run_tool(
                    call, dashboard, user_uuid, role, len(steps) + 1
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
            answer = "Too many analysis steps. Please narrow the question."

        reply = AssistantReply(
            conversation_id=conversation_id,
            message=answer.strip() or "No usable conclusion was produced. Try rephrasing.",
            charts=charts,
            steps=steps,
            latency_ms=elapsed_ms(started),
            model=model_key,
            total_tokens=total_tokens,
        )
        await asyncio.to_thread(
            self._record_turn, conversation_id, owner, request.question, reply
        )
        return reply

    async def _run_tool(
        self,
        call: ToolCall,
        dashboard: CopilotDashboard,
        user_id: UUID,
        role: str,
        sequence: int,
    ) -> tuple[ToolOutcome | None, AssistantStep]:
        name = call.function.name
        try:
            raw = json.loads(call.function.arguments or "{}")
            if not isinstance(raw, dict):
                raise ValueError("Arguments must be a JSON object")
            registered = BY_NAME.get(name)
            if registered is None:
                raise ValueError(f"Unknown tool: {name}")
            arguments = registered.arguments.model_validate(raw)
            outcome = await asyncio.to_thread(
                self._outcome,
                name,
                arguments,
                dashboard,
                user_id,
                role,
            )
        except (ValueError, ValidationError, json.JSONDecodeError) as error:
            return None, AssistantStep(
                sequence=sequence,
                tool=name,
                arguments={},
                status="error",
                summary=str(error)[:500],
            )
        except Exception as error:  # noqa: BLE001 - tool failure must not kill the turn
            logger.exception("Copilot assistant tool %s failed", name)
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

    def _outcome(
        self,
        name: str,
        arguments: ToolArguments,
        dashboard: CopilotDashboard,
        user_id: UUID,
        role: str,
    ) -> ToolOutcome:
        if name == "query_copilot_overview":
            payload = {
                "organization": dashboard.organization,
                "subscription": dashboard.subscription.model_dump(mode="json"),
                "totals": dashboard.totals.model_dump(mode="json"),
                "report_start_day": dashboard.report_start_day,
                "report_end_day": dashboard.report_end_day,
                "warnings": dashboard.warnings,
            }
            return ToolOutcome(
                payload=payload,
                summary="Copilot organization overview",
                row_count=1,
            )
        if name == "query_copilot_budget_requests":
            requests = self._copilot.budget_requests(user_id, role)
            return ToolOutcome(
                payload=requests.model_dump(mode="json"),
                summary="Copilot budget requests",
                row_count=len(requests.items),
            )
        if name in {"query_copilot_members", "query_copilot_budgets"}:
            assert isinstance(arguments, MemberArguments)
            members = dashboard.members
            if arguments.login:
                members = [
                    item
                    for item in members
                    if item.login.lower() == arguments.login.lower()
                ]
            rows = [
                item.model_dump(mode="json")
                for item in members[: arguments.limit]
            ]
            if name == "query_copilot_budgets":
                keys = (
                    "login",
                    "budget_amount",
                    "budget_consumed",
                    "budget_remaining",
                )
                rows = [{key: row[key] for key in keys} for row in rows]
            return ToolOutcome(
                payload={"organization": dashboard.organization, "items": rows},
                summary=name,
                row_count=len(rows),
            )

        assert isinstance(arguments, BreakdownArguments)
        collection = {
            "model": dashboard.models,
            "feature": dashboard.features,
            "language": dashboard.languages,
            "ide": dashboard.ides,
        }[arguments.dimension]
        ranked = sorted(
            collection,
            key=lambda item: (
                -int(getattr(item, arguments.metric)),
                item.label.casefold(),
                item.key,
            ),
        )
        rows = [item.model_dump(mode="json") for item in ranked[: arguments.limit]]
        chart = ChartSpec(
            id=f"copilot-{arguments.dimension}-{arguments.metric}",
            kind="bar",
            title=f"GitHub Copilot · {arguments.dimension}",
            time_range_label=(
                f"{dashboard.report_start_day} – {dashboard.report_end_day}"
            ),
            basis="GitHub Copilot organization usage metrics API",
            unit="",
            category_key="label",
            category_label=arguments.dimension,
            series=[ChartSeries(key=arguments.metric, label=arguments.metric)],
            rows=rows,
            generated_at=datetime.now(UTC),
            range_start=dashboard.report_start_day,
            range_end=dashboard.report_end_day,
            query=ChartQuery(tool=name, arguments=arguments.model_dump()),
        )
        return ToolOutcome(
            payload={"items": rows},
            chart=chart,
            summary="Copilot usage breakdown",
            row_count=len(rows),
        )

    def _metadata(
        self,
        user_id: str,
        user_name: str,
        model_id: str,
        model: str,
        conversation_id: str,
        turn_index: int,
        *,
        request_source: str = "copilot-assistant",
        workflow: str = "copilot-assistant-analysis",
    ) -> InvocationMetadata:
        return InvocationMetadata(
            organization_id="github-copilot",
            organization="GitHub Copilot",
            department_id="unattributed",
            department="unattributed",
            project_id="github-copilot-finops",
            project="GitHub Copilot FinOps",
            agent_id=COPILOT_AGENT_ID,
            agent=COPILOT_AGENT_NAME,
            user_id=user_id,
            user=user_name,
            workflow=workflow,
            model_id=model_id,
            model=model,
            runtime="unattributed",
            request_source=request_source,
            usage_domain="github_copilot",
            run_id=conversation_id,
            turn_index=turn_index,
        )

    def _record_turn(
        self, conversation_id: str, owner: str, question: str, reply: AssistantReply
    ) -> None:
        try:
            self._repository.append_conversation_turn(
                conversation_id=UUID(conversation_id),
                owner_id=owner,
                title=question.strip()[:TITLE_LENGTH] or "Untitled",
                question=question,
                reply=reply.model_dump(mode="json"),
            )
        except Exception:  # noqa: BLE001 - history must not break the paid answer
            logger.exception("Failed to record Copilot assistant conversation turn")

    def list_conversations(self, user_email: str) -> list[ConversationSummary]:
        return [
            ConversationSummary.model_validate(row)
            for row in self._repository.list_conversations(
                self.owner(user_email), HISTORY_LIMIT
            )
        ]

    def get_conversation(
        self, conversation_id: UUID, user_email: str
    ) -> Conversation:
        row = self._repository.get_conversation(
            conversation_id, self.owner(user_email)
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        turns = row.get("turns", [])
        return Conversation.model_validate(
            {key: value for key, value in row.items() if key != "turns"}
            | {"exchanges": [dict(turn) for turn in turns]}
        )

    def rename_conversation(
        self, conversation_id: UUID, user_email: str, title: str
    ) -> Conversation:
        owner = self.owner(user_email)
        if self._repository.rename_conversation(conversation_id, owner, title) is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return self.get_conversation(conversation_id, user_email)

    def title_conversation(
        self, conversation_id: UUID, user_email: str, locale: str
    ) -> ConversationSummary:
        conversation = self.get_conversation(conversation_id, user_email)
        if conversation.title_source != "question" or not conversation.exchanges:
            return ConversationSummary.model_validate(
                conversation.model_dump(exclude={"exchanges"})
            )
        runtime_id, model_id, model_key = self._select_model()
        first = conversation.exchanges[0]
        language = ANSWER_LANGUAGE.get(locale, "English")
        try:
            response = self._runtime.invoke(
                ModelInvocationRequest(
                    metadata=self._metadata(
                        user_email,
                        user_email,
                        str(model_id),
                        model_key,
                        str(conversation.id),
                        1,
                        request_source="copilot-assistant-title",
                        workflow="copilot-assistant-title",
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
                    max_output_tokens=32,
                )
            )
        except Exception:  # noqa: BLE001 - a title is optional
            logger.exception("Failed to title Copilot assistant conversation")
            return ConversationSummary.model_validate(
                conversation.model_dump(exclude={"exchanges"})
            )
        title = clean_title(response.content)
        if title:
            row = self._repository.summarise_conversation_title(
                conversation_id, self.owner(user_email), title
            )
            if row is not None:
                return ConversationSummary.model_validate(row)
        return ConversationSummary.model_validate(
            conversation.model_dump(exclude={"exchanges"})
        )

    def delete_conversation(self, conversation_id: UUID, user_email: str) -> None:
        if not self._repository.delete_conversation(
            conversation_id, self.owner(user_email)
        ):
            raise HTTPException(status_code=404, detail="Conversation not found")
