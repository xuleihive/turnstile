from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException

from backend.services.assistant import AssistantService
from backend.services.assistant_shared import clean_title
from backend.services.assistant_tools import REGISTRY, TimeScopedArguments, resolve_range
from backend.services.runtime_service import ModelRuntimeService
from turnstile_core.config import Settings
from turnstile_core.domain.assistant_models import (
    AssistantAskRequest,
    AssistantSettingsWrite,
    ChartSpec,
    PinnedChartWrite,
    PinnedReportLayout,
)
from turnstile_core.domain.models import TokenUsageRecord
from turnstile_core.domain.runtime_models import InvocationApiFormat
from turnstile_core.integrations.gateway import GatewayRouter
from turnstile_core.persistence.in_memory import InMemoryRepository

USER = "test.user01@contoso.com"
SEED_TIMESTAMP = (datetime.now(UTC) - timedelta(days=1)).isoformat()


def _has_han(value: str) -> bool:
    return any("\u4e00" <= character <= "\u9fff" for character in value)


def _any_chart() -> ChartSpec:
    """A structurally valid chart, for assertions that are about the write, not the data."""
    return ChartSpec.model_validate(
        {
            "id": "chart-1",
            "kind": "bar",
            "title": "t",
            "time_range_label": "r",
            "basis": "b",
            "unit": "tokens",
            "category_key": "name",
            "category_label": "名称",
            "series": [{"key": "total_tokens", "label": "Tokens"}],
            "rows": [],
            "generated_at": SEED_TIMESTAMP,
            "query": {"tool": "query_usage_ranking", "arguments": {"dimension": "department"}},
        }
    )


def _usage(**overrides: Any) -> TokenUsageRecord:
    base: dict[str, Any] = {
        "id": "seed-1",
        "request_id": "seed-1",
        "correlation_id": "seed-1",
        "ts": SEED_TIMESTAMP,
        "team": "AI Platform",
        "organization": "Contoso Global",
        "organization_id": "org-contoso-global",
        "department": "AI Platform",
        "department_id": "department-platform",
        "project": "Model FinOps",
        "project_id": "project-finops",
        "user": USER,
        "user_id": USER,
        "agent": "Delivery Engineer",
        "agent_id": "agent-delivery",
        "workflow": "release-review",
        "run_id": "run-1",
        "turn_index": 1,
        "provider": "aoai",
        "model": "gpt-5.6-luna",
        "model_id": "9b45b7e1-403d-4c6c-a877-5dc77775b911",
        "runtime": "Microsoft Foundry via APIM",
        "request_source": "test",
        "input_tokens": 100,
        "cached_tokens": 0,
        "output_tokens": 20,
        "et": 1.0,
        "et_coeff_m": 1.0,
        "latency_ms": 900,
        "status": "200",
        "status_code": 200,
        "estimated_cost": 0.5,
        "estimated": False,
        "ingest_source": "eventhub",
    }
    base.update(overrides)
    return TokenUsageRecord.model_validate(base)


def _service(
    responses: list[dict[str, Any]],
) -> tuple[AssistantService, InMemoryRepository, list[Any]]:
    """Wire the service to a scripted model.

    The transport records the outgoing bodies so a test can assert what the model was
    actually offered and told, which is where the interesting regressions live.
    """
    repository = InMemoryRepository()
    repository.write_token_usage(_usage())
    repository.write_token_usage(
        _usage(
            id="seed-2",
            request_id="seed-2",
            correlation_id="seed-2",
            department="Commerce",
            department_id="commerce",
            input_tokens=10,
            output_tokens=5,
            estimated_cost=0.1,
        )
    )
    sent: list[Any] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # Under a key no OpenAI field can collide with, so tests that assert on the body
        # are unaffected. The attribution headers are the only record of who a call was
        # made for, and they are what the telemetry pipeline reads.
        body["_headers"] = dict(request.headers)
        sent.append(body)
        return httpx.Response(200, json=queue.pop(0))

    runtime = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )
    return AssistantService(repository, runtime, Settings()), repository, sent


def _tool_call(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }


def _answer(text: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 60, "completion_tokens": 12},
    }


def test_a_generous_limit_still_charts_a_small_result() -> None:
    # The table threshold exists because a bar chart of more than ~20 categories is
    # unreadable, which is a property of the ANSWER, not of the question. Deciding it on
    # the requested limit made "rank all models" render as a table even when only 11 came
    # back -- and the reader could not argue their way out of it, because nothing in the
    # conversation tells the model that lowering `limit` is what switches the renderer.
    service, _, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department", "limit": 50}),
            _answer("AI Platform 用量最高。"),
        ]
    )

    reply = service.ask(
        AssistantAskRequest(question="Rank all departments and show it as a chart."),
        user_id=USER,
        user_name=USER,
    )

    chart = reply.charts[0]
    assert len(chart.rows) == 2
    assert chart.kind == "bar"


def test_a_result_too_wide_to_chart_becomes_a_table() -> None:
    # The other half of the same rule: the table is not gone, it is now triggered by the
    # thing it was always meant to describe. Twenty-odd bars is a wall of ticks, and that
    # is true whether the reader asked for twenty or for everything.
    service, repository, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department", "limit": 50}),
            _answer("Twenty-three departments."),
        ]
    )
    for index in range(21):
        repository.write_token_usage(
            _usage(
                id=f"wide-{index}",
                request_id=f"wide-{index}",
                correlation_id=f"wide-{index}",
                department=f"Dept {index:02d}",
                department_id=f"dept-{index:02d}",
            )
        )

    reply = service.ask(
        AssistantAskRequest(question="Rank every department."),
        user_id=USER,
        user_name=USER,
    )

    chart = reply.charts[0]
    assert len(chart.rows) > 20
    assert chart.kind == "table"


def test_the_chart_comes_from_the_repository_not_from_the_model() -> None:
    # The whole design rests on this: the model picks a tool, the tool authors the chart
    # from a real query. If a chart could ever be assembled from model output, every
    # other guarantee in this feature would be a prompt instruction rather than a fact.
    service, _, sent = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department", "limit": 5}),
            _answer("AI Platform 用量最高。"),
        ]
    )

    reply = service.ask(
        AssistantAskRequest(question="哪个部门 Token 用得最多？"),
        user_id=USER,
        user_name=USER,
    )

    assert len(reply.charts) == 1
    chart = reply.charts[0]
    assert chart.kind == "bar"
    assert [row["name"] for row in chart.rows] == ["AI Platform", "Commerce"]
    assert chart.rows[0]["total_tokens"] == 120
    assert reply.message == "AI Platform 用量最高。"
    assert [step.status for step in reply.steps] == ["ok"]
    # Every chart states its range, basis and unit, because a pinned copy has to be
    # readable months later without the conversation that produced it.
    assert chart.time_range_label
    assert chart.basis
    assert chart.unit == "Token"
    # The tool result was handed back to the model as a tool message, so the prose is
    # written against real numbers rather than against the question alone.
    tool_turn = sent[1]["messages"][-1]
    assert tool_turn["role"] == "tool"
    assert tool_turn["tool_call_id"] == "call-1"
    assert "AI Platform" in tool_turn["content"]


def test_the_model_is_offered_every_registered_tool() -> None:
    service, _, sent = _service([_answer("请补充时间范围。")])

    service.ask(AssistantAskRequest(question="用量如何？"), user_id=USER, user_name=USER)

    offered = {tool["function"]["name"] for tool in sent[0]["tools"]}
    assert offered == {tool.name for tool in REGISTRY}
    # Flat argument schemas: a $ref would mean the schema depends on a $defs block that
    # not every gateway forwards, and the failure mode is a silently malformed call.
    for tool in sent[0]["tools"]:
        assert "$ref" not in json.dumps(tool["function"]["parameters"])


def test_gpt6_assistant_queries_real_data_through_stateless_responses() -> None:
    reasoning = {
        "type": "reasoning",
        "id": "rs-1",
        "summary": [],
        "encrypted_content": "opaque-analysis",
    }
    function_call = {
        "type": "function_call",
        "id": "fc-1",
        "call_id": "call-1",
        "name": "query_usage_trend",
        "arguments": '{"dimension":"department","time_range":"last_7_days"}',
        "status": "completed",
    }
    answer = "Usage is highest in AI Platform."
    service, repository, sent = _service([
        {
            "status": "completed",
            "output": [reasoning, function_call],
            "usage": {"input_tokens": 50, "output_tokens": 10},
        },
        {
            "status": "completed",
            "output": [{
                "type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": answer, "annotations": []}],
            }],
            "usage": {"input_tokens": 60, "output_tokens": 12},
        },
    ])
    picked = service.settings().available_models[0]
    model = next(item for item in repository.models if item["id"] == picked.id)
    model["upstream_model_id"] = "gpt-6.1-sol"
    runtime = next(item for item in repository.runtimes if item["id"] == model["runtime_id"])
    runtime["config"]["control_plane_managed"] = True

    reply = service.ask(
        AssistantAskRequest(
            question="How has each department's usage changed over the past 7 days?",
            locale="en",
        ),
        user_id=USER,
        user_name=USER,
    )

    assert len(sent) == 2
    assert "messages" not in sent[0]
    assert sent[0]["store"] is False
    assert sent[0]["model"] == picked.model_key
    assert all(item["type"] == "message" for item in sent[0]["input"])
    assert all(
        block["type"] == "input_text"
        for item in sent[0]["input"] for block in item["content"]
    )
    assert {tool["name"] for tool in sent[0]["tools"]} == {tool.name for tool in REGISTRY}
    assert sent[1]["input"][2:4] == [reasoning, function_call]
    assert sent[1]["input"][-1]["type"] == "function_call_output"
    assert sent[1]["input"][-1]["call_id"] == "call-1"
    assert "AI Platform" in sent[1]["input"][-1]["output"]
    assert sent[0]["_headers"]["x-user-id"] == USER
    assert sent[1]["_headers"]["x-hive-run-id"] == reply.conversation_id
    assert reply.message == answer
    assert reply.total_tokens == 132
    assert [step.status for step in reply.steps] == ["ok"]
    assert len(reply.charts) == 1
    assert "opaque-analysis" not in reply.model_dump_json()
    stored = service.get_conversation(UUID(reply.conversation_id), USER)
    assert "opaque-analysis" not in stored.model_dump_json()


def test_foundry_claude_on_shared_runtime_queries_real_data_and_preserves_tool_history() -> None:
    blocks = [
        {"type": "thinking", "thinking": "private analysis", "signature": "private-signature"},
        {"type": "tool_use", "id": "toolu-1", "name": "query_usage_ranking",
         "input": {"dimension": "department"}},
    ]
    service, repository, sent = _service([
        {"content": blocks, "stop_reason": "tool_use",
         "usage": {"input_tokens": 50, "output_tokens": 10}},
        {"content": [{"type": "text", "text": "AI Platform leads."}],
         "usage": {"input_tokens": 60, "output_tokens": 12}},
    ])
    picked = service.settings().available_models[0]
    model = next(item for item in repository.models if item["id"] == picked.id)
    model.update(family_key="claude", upstream_model_id="claude-opus-5-5")
    settings = service.save_settings(AssistantSettingsWrite(model_id=picked.id), USER)

    assert settings.effective_api_format == "anthropic_messages"
    assert settings.available_api_formats == ["anthropic_messages"]
    assert settings.effective_api_path == "/v1/messages"
    reply = service.ask(
        AssistantAskRequest(question="Which department uses the most?", locale="en"),
        user_id=USER, user_name=USER,
    )

    assert len(sent) == 2
    assert sent[0]["tools"][0]["input_schema"]
    assert sent[1]["messages"][-2] == {"role": "assistant", "content": blocks}
    assert sent[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    assert "AI Platform" in sent[1]["messages"][-1]["content"][0]["content"]
    assert reply.message == "AI Platform leads."
    assert reply.charts[0].rows[0]["total_tokens"] == 120
    assert reply.total_tokens == 132
    assert sent[0]["_headers"]["x-agent-id"] == "agent-finops-assistant"
    assert "private-signature" not in service.get_conversation(
        UUID(reply.conversation_id), USER,
    ).model_dump_json()


def test_selected_responses_protocol_is_saved_and_used_for_answer_and_title() -> None:
    def answer(text: str) -> dict[str, Any]:
        return {"output": [{
            "type": "message", "content": [{"type": "output_text", "text": text}],
        }]}

    service, repository, sent = _service([answer("Answer."), answer("Usage review")])
    picked = service.settings().available_models[0]
    saved = service.save_settings(
        AssistantSettingsWrite(
            model_id=picked.id, api_format=InvocationApiFormat.OPENAI_RESPONSES,
        ), USER,
    )
    assert saved.api_format == "openai_responses"
    assert saved.effective_api_format == "openai_responses"
    assert saved.available_api_formats == ["openai_chat", "openai_responses"]
    assert repository.assistant_settings()["api_format"] == "openai_responses"
    reply = service.ask(AssistantAskRequest(question="Usage?"), user_id=USER, user_name=USER)
    service.title_conversation(UUID(reply.conversation_id), USER, "en")
    assert all("input" in body and "messages" not in body for body in sent)
    assert sent[-1]["max_output_tokens"] == 256


@pytest.mark.parametrize("api", ["openai_chat", "openai_responses"])
def test_claude_rejects_openai_protocol_override_without_saving_it(api: str) -> None:
    service, repository, sent = _service([])
    picked = service.settings().available_models[0]
    model = next(item for item in repository.models if item["id"] == picked.id)
    model.update(family_key="claude", upstream_model_id="claude-opus-5-5")
    with pytest.raises(HTTPException) as caught:
        service.save_settings(
            AssistantSettingsWrite.model_validate({"model_id": picked.id, "api_format": api}), USER,
        )
    assert caught.value.status_code == 409
    assert repository.assistant_settings()["model_id"] is None
    assert sent == []


def test_gpt6_is_only_offered_with_responses_and_requires_a_compatible_apim_binding() -> None:
    service, repository, _ = _service([])
    picked = service.settings().available_models[0]
    model = next(item for item in repository.models if item["id"] == picked.id)
    model["upstream_model_id"] = "gpt-6.1-sol"
    runtime = next(item for item in repository.runtimes if item["id"] == model["runtime_id"])
    runtime["config"]["control_plane_managed"] = True
    chosen = next(
        choice for choice in service.settings().available_models if choice.id == picked.id
    )
    assert chosen.api_formats == ["openai_responses"]
    with pytest.raises(HTTPException):
        service.save_settings(
            AssistantSettingsWrite(
                model_id=picked.id, api_format=InvocationApiFormat.OPENAI_CHAT,
            ), USER,
        )
    runtime["config"]["backend_path"] = "/v1/chat/completions"
    assert picked.id not in {choice.id for choice in service.settings().available_models}


def test_unavailable_saved_protocol_falls_back_visibly_and_never_reaches_wrong_endpoint() -> None:
    service, repository, _ = _service([])
    picked = service.settings().available_models[0]
    service.save_settings(
        AssistantSettingsWrite(
            model_id=picked.id, api_format=InvocationApiFormat.OPENAI_RESPONSES,
        ), USER,
    )
    model = next(item for item in repository.models if item["id"] == picked.id)
    model.update(family_key="claude", upstream_model_id="claude-opus-5-5")
    settings = service.settings()
    assert settings.model_available is True
    assert settings.api_available is False
    assert settings.api_format == "openai_responses"
    assert settings.effective_api_format == "anthropic_messages"


def test_legacy_title_toggle_preserves_protocol_and_explicit_null_restores_automatic() -> None:
    service, repository, _ = _service([])
    picked = service.settings().available_models[0]
    service.save_settings(
        AssistantSettingsWrite(
            model_id=picked.id, api_format=InvocationApiFormat.OPENAI_RESPONSES,
        ), USER,
    )
    legacy = service.save_settings(
        AssistantSettingsWrite(model_id=picked.id, auto_title=False), USER,
    )
    assert legacy.api_format == "openai_responses"
    automatic = service.save_settings(
        AssistantSettingsWrite(model_id=picked.id, auto_title=False, api_format=None), USER,
    )
    assert automatic.effective_api_format == "openai_chat"
    assert repository.assistant_settings()["api_format"] is None


def test_a_bad_tool_argument_is_returned_to_the_model_not_raised() -> None:
    # A hallucinated argument is recoverable — the model can read the error and retry.
    # Raising would turn a fixable mistake into a failed answer for the user.
    service, _, sent = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "galaxy"}),
            _answer("已改用部门维度。"),
        ]
    )

    reply = service.ask(AssistantAskRequest(question="按星系统计"), user_id=USER, user_name=USER)

    assert [step.status for step in reply.steps] == ["error"]
    assert reply.charts == []
    assert reply.message == "已改用部门维度。"
    assert "galaxy" not in sent[1]["messages"][-1]["content"]
    assert "dimension" in sent[1]["messages"][-1]["content"]


def test_an_unknown_tool_name_does_not_reach_the_repository() -> None:
    service, _, _ = _service(
        [_tool_call("drop_all_usage", {}), _answer("我只能查询用量数据。")]
    )

    reply = service.ask(AssistantAskRequest(question="删掉所有数据"), user_id=USER, user_name=USER)

    assert reply.steps[0].status == "error"
    assert "Unknown tool" in reply.steps[0].summary


def test_the_chart_speaks_the_language_the_ui_asked_for() -> None:
    # Charts are authored by tools, not by the model, so their labels do not follow the
    # answer language automatically. English is the default UI locale, and a chart
    # labelled in Chinese inside an English dashboard is a broken chart, not a
    # translation gap.
    service, _, sent = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department"}),
            _answer("AI Platform leads."),
        ]
    )

    reply = service.ask(
        AssistantAskRequest(question="Which department uses the most?", locale="en"),
        user_id=USER,
        user_name=USER,
    )

    chart = reply.charts[0]
    assert chart.title == "Department · Tokens ranking"
    assert chart.category_label == "Department"
    assert "token_usage" in chart.basis
    assert not _has_han(chart.title + chart.basis + chart.time_range_label)
    # The answer language is an instruction in the prompt, so the two halves of one
    # reply cannot end up in different languages.
    assert "Write your answer in English" in sent[0]["messages"][0]["content"]


def test_a_chinese_ui_gets_chinese_charts() -> None:
    service, _, sent = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department"}),
            _answer("AI Platform 最高。"),
        ]
    )

    reply = service.ask(
        AssistantAskRequest(question="哪个部门最多？", locale="zh-CN"),
        user_id=USER,
        user_name=USER,
    )

    assert reply.charts[0].title == "部门 · Token 用量排名"
    assert "Write your answer in Simplified Chinese" in sent[0]["messages"][0]["content"]


def test_an_unknown_locale_falls_back_to_english_not_to_the_source_language() -> None:
    # A reader who asked for Korean is better served by English than by Chinese they may
    # not read, and the source language happening to be Chinese is an implementation
    # detail they never agreed to.
    service, _, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department"}),
            _answer("ok"),
        ]
    )

    reply = service.ask(
        AssistantAskRequest(question="q", locale="ko"), user_id=USER, user_name=USER
    )

    assert not _has_han(reply.charts[0].title)


def test_assistant_spend_is_attributed_to_the_asking_person() -> None:
    # The assistant is not exempt from the product it analyses. Its own tokens land under
    # the asking employee and the FinOps Assistant agent, which is also what subjects it
    # to that person's model policy and budget.
    service, _, _ = _service([_answer("好的。")])

    metadata = service._metadata(  # noqa: SLF001 - asserting the attribution contract
        user_id=USER,
        user_name=USER,
        model_id="m",
        model="gpt-5.6-luna",
        conversation_id="c",
        turn_index=1,
    )

    assert metadata.agent_id == "agent-finops-assistant"
    assert metadata.request_source == "assistant"
    assert metadata.user_id == USER
    assert metadata.department_id == "department-platform"


def test_a_pinned_chart_refreshes_by_replaying_its_own_query() -> None:
    # Pin stores the tool plus its validated arguments, never SQL. Refresh therefore
    # cannot read more than the question that created the pin, and cannot drift from
    # what the assistant showed.
    service, repository, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department", "limit": 5}),
            _answer("AI Platform 最高。"),
        ]
    )
    reply = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    pinned = service.pin(
        PinnedChartWrite(
            title="部门 Token 排名",
            original_question="部门排名",
            chart=reply.charts[0],
        ),
        USER,
    )
    assert pinned.charts[0].chart.query.tool == "query_usage_ranking"
    assert pinned.charts[0].chart.rows[0]["total_tokens"] == 120

    repository.write_token_usage(
        _usage(id="seed-3", request_id="seed-3", correlation_id="seed-3", input_tokens=1_000)
    )
    refreshed = service.refresh_pinned(pinned.id, USER, "UTC")

    assert refreshed.id == pinned.id
    assert refreshed.charts[0].chart.rows[0]["total_tokens"] == 1_140
    assert service.list_pinned(USER)[0].charts[0].chart.rows[0]["total_tokens"] == 1_140


def test_a_report_holds_many_charts_and_refreshes_every_one() -> None:
    # The whole point of the collection: one nav item, several charts, one refresh.
    service, repository, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department", "limit": 5}),
            _answer("好的。"),
            _tool_call("query_usage_ranking", {"dimension": "model", "limit": 5}),
            _answer("好的。"),
        ]
    )
    first = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    report = service.pin(
        PinnedChartWrite(title="月度回顾", original_question="部门排名", chart=first.charts[0]),
        USER,
    )
    second = service.ask(AssistantAskRequest(question="模型排名"), user_id=USER, user_name=USER)
    grown = service.pin(
        PinnedChartWrite(
            report_id=report.id, original_question="模型排名", chart=second.charts[0]
        ),
        USER,
    )

    # Adding a chart returns the whole report, and does not create a second nav item.
    assert grown.id == report.id
    assert len(grown.charts) == 2
    assert grown.title == "月度回顾"
    assert len(service.list_pinned(USER)) == 1
    # Order is the order they were added, so the report reads the way it was built.
    assert [chart.position for chart in grown.charts] == [0, 1]

    repository.write_token_usage(
        _usage(id="seed-3", request_id="seed-3", correlation_id="seed-3", input_tokens=1_000)
    )
    refreshed = service.refresh_pinned(report.id, USER, "UTC")

    # Each chart replays its OWN query, so the two land on different numbers: the
    # department chart sees AI Platform alone, the model chart sees every department's
    # traffic on that one model. A single shared value would not prove that.
    assert len(refreshed.charts) == 2
    assert refreshed.charts[0].chart.rows[0] == {
        "name": "AI Platform",
        "total_tokens": 1_140.0,
        "share_percent": 90.91,
    }
    assert refreshed.charts[1].chart.rows[0] == {
        "name": "gpt-5.6-luna",
        "total_tokens": 1_155.0,
        "share_percent": 100.0,
    }


def test_a_chart_can_only_be_added_to_a_report_its_owner_holds() -> None:
    service, _, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department"}),
            _answer("好的。"),
        ]
    )
    reply = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    report = service.pin(
        PinnedChartWrite(title="我的报表", original_question="部门排名", chart=reply.charts[0]),
        USER,
    )

    with pytest.raises(HTTPException) as error:
        service.pin(
            PinnedChartWrite(
                report_id=report.id, original_question="部门排名", chart=reply.charts[0]
            ),
            "someone.else@contoso.com",
        )
    assert error.value.status_code == 404


def test_a_new_report_needs_a_title_but_adding_to_one_does_not() -> None:
    # An existing report already has a name. Letting this field rename it would make
    # adding a chart a destructive act.
    with pytest.raises(ValueError):
        PinnedChartWrite(original_question="x", chart=_any_chart())
    added = PinnedChartWrite(
        report_id=UUID("11111111-1111-4111-8111-111111111111"),
        original_question="x",
        chart=_any_chart(),
    )
    assert added.title is None


def test_pins_are_scoped_to_their_owner() -> None:
    service, _, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department"}),
            _answer("好的。"),
        ]
    )
    reply = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    pinned = service.pin(
        PinnedChartWrite(title="我的报表", original_question="部门排名", chart=reply.charts[0]),
        USER,
    )

    assert service.list_pinned("someone.else@contoso.com") == []
    with pytest.raises(HTTPException) as error:
        service.refresh_pinned(pinned.id, "someone.else@contoso.com", "UTC")
    assert error.value.status_code == 404


def test_creator_can_share_a_report_but_public_readers_cannot_modify_it() -> None:
    service, _, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department"}),
            _answer("好的。"),
        ]
    )
    other = "someone.else@contoso.com"
    reply = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    report = service.pin(
        PinnedChartWrite(title="共享报表", original_question="部门排名", chart=reply.charts[0]),
        USER,
    )

    assert report.visibility == "private"
    assert report.can_manage is True
    assert service.list_pinned(other) == []

    shared = service.set_pinned_visibility(report.id, USER, "public")
    visible = service.get_pinned(report.id, other)

    assert shared.visibility == "public"
    assert visible.visibility == "public"
    assert visible.owner_id == USER
    assert visible.can_manage is False
    assert [item.id for item in service.list_pinned(other)] == [report.id]

    with pytest.raises(HTTPException) as rename_error:
        service.rename_pinned(report.id, other, "伪造名称", "")
    assert rename_error.value.status_code == 403
    with pytest.raises(HTTPException) as refresh_error:
        service.refresh_pinned(report.id, other, "UTC")
    assert refresh_error.value.status_code == 403
    with pytest.raises(HTTPException) as visibility_error:
        service.set_pinned_visibility(report.id, other, "private")
    assert visibility_error.value.status_code == 403

    private = service.set_pinned_visibility(report.id, USER, "private")
    assert private.visibility == "private"
    assert service.list_pinned(other) == []


def test_report_layout_is_persisted_only_by_its_creator() -> None:
    service, _, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department"}),
            _answer("好的。"),
        ]
    )
    reply = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    report = service.pin(
        PinnedChartWrite(title="布局报表", original_question="部门排名", chart=reply.charts[0]),
        USER,
    )
    assert report.layout == PinnedReportLayout()

    layout = PinnedReportLayout(
        spans={report.charts[0].id: 6},
        row_heights={0: 420},
    )
    saved = service.set_pinned_layout(report.id, USER, layout)

    assert saved.layout == layout
    assert service.get_pinned(report.id, USER).layout == layout

    service.set_pinned_visibility(report.id, USER, "public")
    with pytest.raises(HTTPException) as permission_error:
        service.set_pinned_layout(report.id, "reader@contoso.com", layout)
    assert permission_error.value.status_code == 403

    unknown_chart = PinnedReportLayout(spans={uuid4(): 6})
    with pytest.raises(HTTPException) as chart_error:
        service.set_pinned_layout(report.id, USER, unknown_chart)
    assert chart_error.value.status_code == 422
    assert service.get_pinned(report.id, USER).layout == layout


def test_the_same_query_cannot_be_pinned_twice_into_one_report() -> None:
    # Two cards in one report that refresh identically is still a defect. Two reports
    # holding the same chart is not: a report is a question, and two questions can
    # legitimately share a measure.
    service, _, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department"}),
            _answer("好的。"),
        ]
    )
    reply = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    report = service.pin(
        PinnedChartWrite(title="报表 A", original_question="部门排名", chart=reply.charts[0]),
        USER,
    )
    same = service.pin(
        PinnedChartWrite(
            report_id=report.id, original_question="部门排名", chart=reply.charts[0]
        ),
        USER,
    )
    other = service.pin(
        PinnedChartWrite(title="报表 B", original_question="部门排名", chart=reply.charts[0]),
        USER,
    )

    assert len(same.charts) == 1
    assert [item.title for item in service.list_pinned(USER)] == ["报表 A", "报表 B"]
    assert other.id != report.id


def test_pinning_freezes_a_relative_range_onto_the_dates_it_covered() -> None:
    # A relative preset does not survive being kept: "this month" pinned in July answers
    # a one-day question on 1 August, and "last 7 days" answers a different question every
    # morning. Pinning stores the window the reader actually agreed to.
    service, _, _ = _service(
        [
            _tool_call(
                "query_usage_ranking",
                {"dimension": "department", "time_range": "last_7_days"},
            ),
            _answer("好的。"),
        ]
    )
    reply = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    chart = reply.charts[0]
    # The conversational chart keeps the relative preset: in a live answer "last 7 days"
    # is the more useful phrasing, and nothing replays it.
    assert chart.query.arguments["time_range"] == "last_7_days"
    assert chart.range_start is not None and chart.range_end is not None

    report = service.pin(
        PinnedChartWrite(title="报表", original_question="部门排名", chart=chart),
        USER,
    )
    stored = report.charts[0].chart.query.arguments
    assert stored["time_range"] == "custom"
    assert stored["start_date"] == chart.range_start.isoformat()
    assert stored["end_date"] == chart.range_end.isoformat()

    # Refresh replays that fixed window rather than re-resolving the preset.
    refreshed = service.refresh_pinned(report.id, USER, "UTC")
    replayed = refreshed.charts[0].chart.query.arguments
    assert replayed["start_date"] == stored["start_date"]
    assert replayed["end_date"] == stored["end_date"]


def test_reordering_charts_must_name_every_chart_in_the_report() -> None:
    # A partial list would leave the omitted charts on their old positions, and the read
    # breaks ties on created_at, so the result would be an order the caller never chose.
    # Rejecting is the only outcome that keeps the request's meaning equal to its effect.
    service, _, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department"}),
            _answer("好的。"),
            _tool_call("query_usage_ranking", {"dimension": "model"}),
            _answer("好的。"),
        ]
    )
    first = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    report = service.pin(
        PinnedChartWrite(title="报表", original_question="部门排名", chart=first.charts[0]),
        USER,
    )
    second = service.ask(AssistantAskRequest(question="模型排名"), user_id=USER, user_name=USER)
    report = service.pin(
        PinnedChartWrite(
            report_id=report.id, original_question="模型排名", chart=second.charts[0]
        ),
        USER,
    )
    original = [chart.id for chart in report.charts]
    assert len(original) == 2

    reordered = service.reorder_charts(report.id, USER, list(reversed(original)))
    assert [chart.id for chart in reordered.charts] == list(reversed(original))

    for invalid in ([original[0]], [*original, original[0]]):
        with pytest.raises(HTTPException) as error:
            service.reorder_charts(report.id, USER, invalid)
        assert error.value.status_code == 422

    # The rejected attempts left the order the successful call established.
    assert [
        chart.id for chart in service.get_pinned(report.id, USER).charts
    ] == list(reversed(original))


def test_removing_the_last_chart_is_refused_so_no_empty_report_survives() -> None:
    # A report with no charts is a nav item that opens an empty page.
    service, _, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department", "limit": 5}),
            _answer("好的。"),
            _tool_call("query_usage_ranking", {"dimension": "model", "limit": 5}),
            _answer("好的。"),
        ]
    )
    first = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    report = service.pin(
        PinnedChartWrite(title="月度回顾", original_question="部门排名", chart=first.charts[0]),
        USER,
    )
    second = service.ask(AssistantAskRequest(question="模型排名"), user_id=USER, user_name=USER)
    grown = service.pin(
        PinnedChartWrite(
            report_id=report.id, original_question="模型排名", chart=second.charts[0]
        ),
        USER,
    )
    service.set_pinned_layout(
        report.id,
        USER,
        PinnedReportLayout(
            spans={grown.charts[0].id: 6, grown.charts[1].id: 8},
            row_heights={0: 420},
        ),
    )

    trimmed = service.remove_chart(report.id, grown.charts[1].id, USER)
    assert len(trimmed.charts) == 1
    assert trimmed.layout.spans == {grown.charts[0].id: 6}
    assert trimmed.layout.row_heights == {0: 420}

    with pytest.raises(HTTPException) as error:
        service.remove_chart(report.id, trimmed.charts[0].id, USER)
    assert error.value.status_code == 409
    assert len(service.get_pinned(report.id, USER).charts) == 1


def test_deleting_a_report_takes_its_charts_with_it() -> None:
    service, _, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department", "limit": 5}),
            _answer("好的。"),
            _tool_call("query_usage_ranking", {"dimension": "model", "limit": 5}),
            _answer("好的。"),
        ]
    )
    first = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    report = service.pin(
        PinnedChartWrite(title="月度回顾", original_question="部门排名", chart=first.charts[0]),
        USER,
    )
    second = service.ask(AssistantAskRequest(question="模型排名"), user_id=USER, user_name=USER)
    service.pin(
        PinnedChartWrite(
            report_id=report.id, original_question="模型排名", chart=second.charts[0]
        ),
        USER,
    )

    service.unpin(report.id, USER)

    assert service.list_pinned(USER) == []
    with pytest.raises(HTTPException):
        service.get_pinned(report.id, USER)


def test_a_pin_must_be_backed_by_a_real_tool() -> None:
    service, _, _ = _service(
        [
            _tool_call("query_usage_ranking", {"dimension": "department"}),
            _answer("好的。"),
        ]
    )
    reply = service.ask(AssistantAskRequest(question="部门排名"), user_id=USER, user_name=USER)
    forged = ChartSpec.model_validate(
        reply.charts[0].model_dump() | {"query": {"tool": "sql", "arguments": {}}}
    )

    with pytest.raises(HTTPException) as error:
        service.pin(
            PinnedChartWrite(title="伪造", original_question="x", chart=forged), USER
        )
    assert error.value.status_code == 422


def test_the_tool_loop_is_bounded() -> None:
    # An unbounded loop is a way to spend an employee's whole month on one question.
    service, _, sent = _service(
        [_tool_call("query_usage_ranking", {"dimension": "department"}) for _ in range(10)]
    )

    reply = service.ask(AssistantAskRequest(question="一直查"), user_id=USER, user_name=USER)

    assert len(sent) == 4
    assert len(reply.steps) == 4


def test_tool_loop_limit_does_not_present_claude_planning_text_as_a_final_answer() -> None:
    response = {
        "content": [
            {"type": "text", "text": "I am still checking."},
            {"type": "tool_use", "id": "toolu-1", "name": "query_usage_ranking",
             "input": {"dimension": "department"}},
        ],
        "stop_reason": "tool_use",
    }
    service, repository, sent = _service([response for _ in range(4)])
    picked = service.settings().available_models[0]
    model = next(item for item in repository.models if item["id"] == picked.id)
    model.update(family_key="claude", upstream_model_id="claude-opus-5-5")
    service.save_settings(AssistantSettingsWrite(model_id=picked.id), USER)

    reply = service.ask(
        AssistantAskRequest(question="Keep analysing."), user_id=USER, user_name=USER,
    )
    assert len(sent) == 4
    assert len(reply.steps) == 4
    assert reply.message == "Too many analysis steps. Please narrow the question."


def test_relative_ranges_resolve_in_the_viewer_timezone() -> None:
    # "本月" is a calendar fact, and the calendar depends on where the viewer is. The
    # model is never asked to compute it, so a preset cannot be a day out.
    tool = next(item for item in REGISTRY if item.name == "query_usage_ranking")
    arguments = tool.arguments_model.model_validate(
        {"dimension": "department", "time_range": "this_month"}
    )
    assert isinstance(arguments, TimeScopedArguments)
    shanghai = resolve_range(arguments, "Asia/Shanghai")
    utc = resolve_range(arguments, "UTC")

    assert shanghai.start != utc.start
    assert "Asia/Shanghai" in shanghai.label
    assert shanghai.start.tzinfo is not None


def test_a_model_without_tool_support_is_never_selected() -> None:
    # A model that cannot call tools would answer from the prompt alone, which is the
    # exact failure this feature exists to prevent. Refusing is the honest outcome.
    repository = InMemoryRepository()
    for model in repository.models:
        model["capabilities"] = ["chat"]
    service = AssistantService(
        repository,
        ModelRuntimeService(repository, Settings(), GatewayRouter(httpx.Client())),
        Settings(),
    )

    with pytest.raises(HTTPException) as error:
        service.ask(AssistantAskRequest(question="用量"), user_id=USER, user_name=USER)
    assert error.value.status_code == 409


def test_cli_runtimes_are_never_selected_for_tool_calling() -> None:
    # The CLI adapter has no tool protocol at all. Add a test-only default model that
    # declares tools: a selection rule that only read capabilities would pick it and every
    # call would silently lose its tools. Production no longer carries this fake model.
    repository = InMemoryRepository()
    cli_model = dict(
        next(model for model in repository.models if model["model_key"] == "gpt-5.3-chat")
    )
    cli_model.update(
        id=UUID("40000000-0000-4000-8000-000000000099"),
        provider_id=repository.providers[0]["id"],
        runtime_id=repository.runtimes[0]["id"],
        model_key="test-only-cli-model",
        display_name="Test-only CLI model",
        is_default=True,
        capabilities=["chat", "tools"],
    )
    for model in repository.models:
        model["is_default"] = False
    repository.models.append(cli_model)
    service = AssistantService(
        repository,
        ModelRuntimeService(repository, Settings(), GatewayRouter(httpx.Client())),
        Settings(),
    )

    runtime_id, _, model_key = service._select_model()  # noqa: SLF001 - selection contract

    runtime = next(item for item in repository.runtimes if item["id"] == runtime_id)
    assert runtime["runtime_kind"] != "copilot_cli"
    assert model_key != "test-only-cli-model"
    assert runtime_id == UUID("30000000-0000-4000-8000-000000000003")


def test_a_conversation_is_persisted_turn_by_turn_under_one_id() -> None:
    # The id already existed as the telemetry `run_id`; what is new is that the
    # transcript survives the React state that used to be its only home.
    service, repository, _ = _service([_answer("第一轮。"), _answer("第二轮。")])

    first = service.ask(
        AssistantAskRequest(question="哪个部门 Token 用得最多？"), user_id=USER, user_name=USER
    )
    second = service.ask(
        AssistantAskRequest(question="那成本呢？", conversation_id=first.conversation_id),
        user_id=USER,
        user_name=USER,
    )

    assert second.conversation_id == first.conversation_id
    stored = service.get_conversation(UUID(first.conversation_id), USER)
    assert [exchange.question for exchange in stored.exchanges] == [
        "哪个部门 Token 用得最多？",
        "那成本呢？",
    ]
    assert [exchange.position for exchange in stored.exchanges] == [0, 1]
    # The reply is stored whole, so reopening shows what was said rather than a
    # re-derived answer that could cite different numbers than its own prose.
    assert stored.exchanges[0].reply.message == "第一轮。"
    # The first question names the thread; the second must not rename it.
    assert stored.title == "哪个部门 Token 用得最多？"
    assert stored.turn_count == 2
    assert len(repository.conversations) == 1


def test_a_conversation_id_from_another_owner_starts_a_new_thread() -> None:
    # A thread handle is not an authorization decision, but it must not be a way to
    # append to, or read, somebody else's conversation either.
    service, _, _ = _service([_answer("其一。"), _answer("其二。")])

    mine = service.ask(AssistantAskRequest(question="我的问题"), user_id=USER, user_name=USER)
    theirs = service.ask(
        AssistantAskRequest(question="别人的问题", conversation_id=mine.conversation_id),
        user_id="someone-else",
        user_name="someone-else",
    )

    assert theirs.conversation_id != mine.conversation_id
    assert len(service.get_conversation(UUID(mine.conversation_id), USER).exchanges) == 1
    with pytest.raises(HTTPException) as error:
        service.get_conversation(UUID(mine.conversation_id), "someone-else")
    assert error.value.status_code == 404


def test_a_malformed_conversation_id_does_not_fail_the_question() -> None:
    # A stale handle left in a browser tab should cost a new thread, not an error page.
    service, _, _ = _service([_answer("好的。")])

    reply = service.ask(
        AssistantAskRequest(question="用量如何？", conversation_id="not-a-uuid"),
        user_id=USER,
        user_name=USER,
    )

    assert UUID(reply.conversation_id)
    assert service.get_conversation(UUID(reply.conversation_id), USER).turn_count == 1


def test_history_lists_most_recent_first_and_carries_no_transcripts() -> None:
    service, _, _ = _service([_answer("一。"), _answer("二。")])
    first = service.ask(AssistantAskRequest(question="最早的问题"), user_id=USER, user_name=USER)
    second = service.ask(AssistantAskRequest(question="最新的问题"), user_id=USER, user_name=USER)

    items = service.list_conversations(USER)

    assert [item.id for item in items] == [
        UUID(second.conversation_id),
        UUID(first.conversation_id),
    ]
    assert [item.title for item in items] == ["最新的问题", "最早的问题"]
    # The dropdown renders titles; shipping every transcript to draw a list would send
    # every chart with it.
    assert not any(hasattr(item, "exchanges") for item in items)


def test_deleting_a_conversation_removes_its_turns() -> None:
    service, repository, _ = _service([_answer("一。")])
    reply = service.ask(AssistantAskRequest(question="要删除的"), user_id=USER, user_name=USER)

    service.delete_conversation(UUID(reply.conversation_id), USER)

    assert service.list_conversations(USER) == []
    assert repository.conversations == []
    with pytest.raises(HTTPException) as error:
        service.delete_conversation(UUID(reply.conversation_id), USER)
    assert error.value.status_code == 404


def test_a_failed_history_write_never_costs_the_answer() -> None:
    # The tokens are already spent and the answer is on screen. Losing it from history
    # is worth strictly less than losing the answer itself.
    service, repository, _ = _service([_answer("答案在这里。")])

    def explode(**_: Any) -> None:
        raise RuntimeError("storage is down")

    repository.append_conversation_turn = explode  # type: ignore[method-assign]

    reply = service.ask(AssistantAskRequest(question="用量如何？"), user_id=USER, user_name=USER)

    assert reply.message == "答案在这里。"
    assert service.list_conversations(USER) == []


def test_the_model_names_the_conversation_instead_of_its_first_question() -> None:
    # The placeholder is the question itself, truncated -- which fills the whole row with
    # the sentence the reader just typed and makes two questions about one subject
    # indistinguishable once they share a prefix.
    service, _, sent = _service([_answer("按部门看，AI Platform 最高。"), _answer("部门用量排名")])
    reply = service.ask(
        AssistantAskRequest(question="过去 7 天每个部门的调用量趋势如何？"),
        user_id=USER,
        user_name=USER,
    )
    assert service.list_conversations(USER)[0].title.startswith("过去 7 天")

    summary = service.title_conversation(UUID(reply.conversation_id), USER, "zh-CN")

    assert summary.title == "部门用量排名"
    assert summary.title_source == "model"
    assert service.list_conversations(USER)[0].title == "部门用量排名"
    # Named from the exchange, not from the question alone: the answer is what the thread
    # turned out to be about, and it is already paid for.
    naming = sent[-1]["messages"]
    assert "过去 7 天每个部门的调用量趋势如何？" in naming[-1]["content"]
    assert "按部门看，AI Platform 最高。" in naming[-1]["content"]
    # No tools, and a budget too small to restate the answer.
    assert "tools" not in sent[-1]
    assert sent[-1]["max_tokens"] <= 32
    # Separable in telemetry: the assistant pays for itself visibly, and the cost of
    # naming threads has to be readable apart from the cost of answering in them.
    assert sent[-1]["_headers"]["x-request-source"] == "assistant-title"
    assert sent[0]["_headers"]["x-request-source"] == "assistant"
    # Same agent and same thread, so the naming cost lands on the conversation it named.
    assert sent[-1]["_headers"]["x-agent-id"] == sent[0]["_headers"]["x-agent-id"]
    assert sent[-1]["_headers"]["x-hive-run-id"] == reply.conversation_id


def test_naming_a_conversation_is_not_a_turn_in_it() -> None:
    # It must not inflate the turn count, and it must not reorder the history list by
    # touching updated_at -- the reader did not act in this thread.
    service, _, _ = _service([_answer("一。"), _answer("二。"), _answer("旧线程名")])
    older = service.ask(AssistantAskRequest(question="较早的"), user_id=USER, user_name=USER)
    newer = service.ask(AssistantAskRequest(question="较新的"), user_id=USER, user_name=USER)

    service.title_conversation(UUID(older.conversation_id), USER, "zh-CN")

    items = service.list_conversations(USER)
    assert [item.id for item in items] == [UUID(newer.conversation_id), UUID(older.conversation_id)]
    assert service.get_conversation(UUID(older.conversation_id), USER).turn_count == 1


def test_a_title_the_reader_typed_is_never_replaced_by_the_model() -> None:
    # The failure this guards is unrecoverable: a generated name overwriting a chosen one
    # cannot be undone, and the reader has no reason to expect it to happen.
    service, _, sent = _service([_answer("答案。")])
    reply = service.ask(AssistantAskRequest(question="原始问题"), user_id=USER, user_name=USER)
    service.rename_conversation(UUID(reply.conversation_id), USER, "我自己起的名字")
    calls_before = len(sent)

    summary = service.title_conversation(UUID(reply.conversation_id), USER, "zh-CN")

    assert summary.title == "我自己起的名字"
    assert summary.title_source == "user"
    # And it did not merely discard the result -- it never asked, so it cost nothing.
    assert len(sent) == calls_before


def test_a_conversation_is_never_summarised_twice() -> None:
    # The client calls this after the first answer, so a retry or a double render must
    # not buy a second summary of a thread that already has one.
    service, _, sent = _service([_answer("答案。"), _answer("第一个标题")])
    reply = service.ask(AssistantAskRequest(question="问题"), user_id=USER, user_name=USER)
    service.title_conversation(UUID(reply.conversation_id), USER, "zh-CN")
    calls_before = len(sent)

    again = service.title_conversation(UUID(reply.conversation_id), USER, "zh-CN")

    assert again.title == "第一个标题"
    assert len(sent) == calls_before


def test_a_failed_naming_call_leaves_the_placeholder_rather_than_erroring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A name is worth less than the conversation. The visible symptom of a broken title
    # is the question staying in the header, which is exactly the placeholder.
    service, _, _ = _service([_answer("答案。")])
    reply = service.ask(
        AssistantAskRequest(question="仍然可读的问题"), user_id=USER, user_name=USER
    )

    def explode(_request: Any) -> Any:
        raise RuntimeError("the gateway is down")

    monkeypatch.setattr(service._runtime, "invoke", explode)

    summary = service.title_conversation(UUID(reply.conversation_id), USER, "zh-CN")

    assert summary.title == "仍然可读的问题"
    assert summary.title_source == "question"


def test_a_title_the_model_could_not_produce_leaves_the_placeholder() -> None:
    # An empty or unusable answer must not be written: a blank title would be worse than
    # the question it replaced, and the column will not hold one anyway.
    service, _, _ = _service([_answer("答案。"), _answer("   ")])
    reply = service.ask(
        AssistantAskRequest(question="仍然可读的问题"), user_id=USER, user_name=USER
    )

    summary = service.title_conversation(UUID(reply.conversation_id), USER, "zh-CN")

    assert summary.title == "仍然可读的问题"
    assert summary.title_source == "question"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("部门用量排名", "部门用量排名"),
        ('"Department token cost"', "Department token cost"),
        ("Title: Department token cost", "Department token cost"),
        ("标题：部门用量", "部门用量"),
        ("《部门用量》", "部门用量"),
        ("部门用量排名。", "部门用量排名"),
        ("Department cost\nand then it kept explaining itself", "Department cost"),
        ("   ", None),
        ("", None),
    ],
)
def test_a_model_that_ignores_the_title_prompt_cannot_reach_the_sidebar(
    raw: str, expected: str | None
) -> None:
    # Every failure mode here is the model answering the question again, or wrapping the
    # title in the punctuation the prompt told it to omit.
    assert clean_title(raw) == expected


def test_a_title_is_cut_to_the_stored_length() -> None:
    assert len(clean_title("很长的标题" * 40) or "") == 60


def test_the_configured_model_is_the_one_called() -> None:
    # The whole point of the setting: without it the registry's own order decides, and an
    # administrator who wants the cheap model has no way to say so.
    service, repository, sent = _service([_answer("答案。")])
    choices = service.settings().available_models
    assert len(choices) > 1, "the fixture needs more than one candidate to prove anything"
    picked = choices[-1]

    saved = service.save_settings(AssistantSettingsWrite(model_id=picked.id), USER)
    service.ask(AssistantAskRequest(question="用量如何？"), user_id=USER, user_name=USER)

    assert saved.effective_model_id == picked.id
    assert saved.model_available is True
    assert sent[0]["model"] == picked.model_key
    assert repository.assistant_settings()["updated_by"] == USER


def test_automatic_selection_is_a_value_not_an_absence() -> None:
    # Returning to automatic has to be expressible, or the first choice is permanent.
    service, _, _ = _service([])
    choices = service.settings().available_models
    service.save_settings(AssistantSettingsWrite(model_id=choices[-1].id), USER)

    back = service.save_settings(AssistantSettingsWrite(model_id=None), USER)

    assert back.model_id is None
    assert back.effective_model_id == choices[0].id
    assert back.model_available is True


def test_a_model_that_stopped_qualifying_falls_back_instead_of_failing() -> None:
    # Disabling the pinned model in Model Management must not turn every question into a
    # 409. The fallback is only acceptable because the settings page can see it.
    service, repository, sent = _service([_answer("答案。")])
    choices = service.settings().available_models
    picked = choices[-1]
    service.save_settings(AssistantSettingsWrite(model_id=picked.id), USER)
    for model in repository.models:
        if model["id"] == picked.id:
            model["enabled"] = False

    settings = service.settings()
    service.ask(AssistantAskRequest(question="用量如何？"), user_id=USER, user_name=USER)

    assert settings.model_id == picked.id
    assert settings.model_available is False
    assert settings.effective_model_id != picked.id
    assert sent[0]["model"] != picked.model_key


def test_a_model_that_never_qualified_is_refused_rather_than_stored() -> None:
    # Read-time fallback exists for a model that stopped qualifying later. Saving one that
    # never did would leave a setting that silently does nothing from the moment it is set.
    service, repository, _ = _service([])
    disabled = next(model for model in repository.models if not model["enabled"])

    with pytest.raises(HTTPException) as error:
        service.save_settings(AssistantSettingsWrite(model_id=disabled["id"]), USER)

    assert error.value.status_code == 409
    assert service.settings().model_id is None


def test_the_picker_only_offers_models_the_gateway_can_actually_carry() -> None:
    service, repository, _ = _service([])

    offered = service.settings().available_models

    assert offered, "at least one model must remain, or the assistant cannot answer"
    offered_keys = {model.model_key for model in offered}
    offered_runtime_ids = {
        model["runtime_id"]
        for model in repository.models
        if model["model_key"] in offered_keys
    }
    assert all(
        runtime["runtime_kind"] != "copilot_cli"
        for runtime in repository.runtimes
        if runtime["id"] in offered_runtime_ids
    )


def test_turning_off_automatic_naming_stops_the_extra_call() -> None:
    # The switch is a spending decision, so it is enforced server-side: a stale tab must
    # not keep buying titles after an administrator switched it off.
    service, _, sent = _service([_answer("答案。")])
    service.save_settings(AssistantSettingsWrite(auto_title=False), USER)
    reply = service.ask(AssistantAskRequest(question="原始问题"), user_id=USER, user_name=USER)
    calls_before = len(sent)

    summary = service.title_conversation(UUID(reply.conversation_id), USER, "zh-CN")

    assert summary.title == "原始问题"
    assert summary.title_source == "question"
    assert len(sent) == calls_before
