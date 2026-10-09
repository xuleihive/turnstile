from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from turnstile_core.domain.runtime_models import (
    ChatMessage,
    GatewayKind,
    InvocationMetadata,
    ModelInvocationRequest,
    ModelInvocationResponse,
    ToolCall,
    ToolDefinition,
    ToolFunctionCall,
    ToolFunctionDefinition,
)
from turnstile_core.integrations.gateway import (
    GatewayInvocationError,
    OpenAICompatibleGatewayAdapter,
)

REASONING = {
    "id": "rs-1",
    "type": "reasoning",
    "summary": [],
    "encrypted_content": "opaque-current-turn-reasoning",
}
FUNCTION_CALL = {
    "id": "fc-1",
    "type": "function_call",
    "status": "completed",
    "call_id": "call-1",
    "name": "query_usage",
    "arguments": '{"dimension":"department"}',
}
ANSWER = {
    "id": "msg-1",
    "type": "message",
    "role": "assistant",
    "status": "completed",
    "phase": "final_answer",
    "content": [{"type": "output_text", "text": "OK", "annotations": []}],
}


@pytest.fixture
def invocation() -> ModelInvocationRequest:
    return ModelInvocationRequest(
        metadata=InvocationMetadata(
            organization_id="org-1",
            organization="Contoso",
            department_id="department-1",
            department="Platform",
            project_id="project-1",
            project="FinOps",
            agent_id="agent-finops-assistant",
            agent="FinOps Assistant",
            user_id="user-1",
            user="user@contoso.com",
            workflow="assistant",
            model_id="model-1",
            model="gpt-6.1-sol",
            runtime="Foundry",
            request_source="assistant",
            run_id="conversation-1",
        ),
        messages=[
            ChatMessage(role="system", content="Use tools to query actual usage."),
            ChatMessage(role="user", content="Which department uses the most?"),
        ],
        tools=[ToolDefinition(function=ToolFunctionDefinition(
            name="query_usage",
            description="Query actual usage.",
            parameters={
                "type": "object",
                "properties": {"dimension": {"type": "string"}},
            },
        ))],
        temperature=0,
        max_output_tokens=1200,
    )


@pytest.fixture
def route() -> dict[str, Any]:
    return {
        "request_id": "request-1",
        "model_id": UUID("40000000-0000-4000-8000-000000000003"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
        "runtime_config": {
            "path": "/chat/completions",
            "backend_path": "/openai/v1/chat/completions",
            "api_format": "openai_chat",
            "control_plane_managed": True,
            "max_tokens_field": "max_completion_tokens",
            "timeout_seconds": 12,
        },
        "model_key": "department-assistant",
        "upstream_model_id": "gpt-6.1-sol",
        "runtime_name": "Foundry",
        "gateway_base_url": "https://gateway.test/turnstile/llm",
        "gateway_auth_type": "api_key",
        "gateway_credential": "test-subscription",
    }


@pytest.mark.parametrize("model", [
    "gpt-6.1-sol", "gpt-6.1-sol-2026-10-01", "gpt-6-astra", "gpt-6-sol",
])
def test_gpt6_tools_use_responses_and_keep_apim_alias_and_attribution(
    invocation: ModelInvocationRequest, route: dict[str, Any], model: str,
) -> None:
    route["upstream_model_id"] = model

    def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.url.path == "/turnstile/llm/responses"
        body = json.loads(incoming.content)
        assert body == {
            "model": "department-assistant",
            "input": [
                {
                    "type": "message", "role": "system",
                    "content": [{"type": "input_text", "text": "Use tools to query actual usage."}],
                },
                {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "Which department uses the most?"}],
                },
            ],
            "tools": [{
                "type": "function",
                "name": "query_usage",
                "description": "Query actual usage.",
                "parameters": {
                    "type": "object",
                    "properties": {"dimension": {"type": "string"}},
                },
                "strict": False,
            }],
            "max_output_tokens": 1200,
            "store": False,
            "stream": False,
            "include": ["reasoning.encrypted_content"],
        }
        assert incoming.headers["api-key"] == "test-subscription"
        assert incoming.headers["x-hive-model"] == "department-assistant"
        assert incoming.headers["x-request-id"] == "request-1"
        assert incoming.headers["x-user-id"] == "user-1"
        assert incoming.headers["x-agent-id"] == "agent-finops-assistant"
        assert incoming.headers["x-request-source"] == "assistant"
        assert incoming.headers["x-hive-run-id"] == "conversation-1"
        return httpx.Response(200, headers={"apim-request-id": "apim-1"}, json={
            "status": "completed",
            "output": [REASONING, FUNCTION_CALL],
            "usage": {
                "input_tokens": 1200,
                "input_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 128},
                "output_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 20},
            },
        })

    result = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM, httpx.Client(transport=httpx.MockTransport(handler)),
    ).invoke(invocation, route)

    assert result.content == ""
    assert result.tool_calls == (ToolCall(
        id="call-1", function=ToolFunctionCall(
            name="query_usage", arguments='{"dimension":"department"}',
        ),
    ),)
    assert result.responses_output == [REASONING, FUNCTION_CALL]
    assert result.correlation_id == "apim-1"
    assert result.usage is not None
    assert result.usage.input_tokens == 72
    assert result.usage.cached_tokens == 1128
    assert result.usage.cache_write_tokens == 128
    assert result.usage.output_tokens == 30
    assert result.usage.estimated is False


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/openai/v1/chat/completions"])
@pytest.mark.parametrize("kind", [GatewayKind.DIRECT, GatewayKind.LITELLM])
def test_direct_responses_preserve_provider_path_and_upstream_model(
    invocation: ModelInvocationRequest, route: dict[str, Any], path: str, kind: GatewayKind,
) -> None:
    route["runtime_config"]["path"] = path

    def handler(incoming: httpx.Request) -> httpx.Response:
        expected_path = "/turnstile/llm" + path.removesuffix("/chat/completions") + "/responses"
        assert incoming.url.path == expected_path
        assert json.loads(incoming.content)["model"] == "gpt-6.1-sol"
        return httpx.Response(200, json={"output": [ANSWER]})

    result = OpenAICompatibleGatewayAdapter(
        kind, httpx.Client(transport=httpx.MockTransport(handler)),
    ).invoke(invocation, route)
    assert result.content == "OK"
    assert result.usage is not None and result.usage.estimated


def test_responses_followup_replays_reasoning_phase_and_parallel_calls_once(
    invocation: ModelInvocationRequest, route: dict[str, Any],
) -> None:
    second_call = {**FUNCTION_CALL, "id": "fc-2", "call_id": "call-2"}
    commentary = {**ANSWER, "phase": "commentary"}
    output: list[dict[str, Any]] = [REASONING, commentary, FUNCTION_CALL, second_call]
    response = ModelInvocationResponse(
        request_id="request-1", correlation_id="apim-1", content="OK",
        tool_calls=[
            ToolCall(id=call["call_id"], function=ToolFunctionCall(
                name=call["name"], arguments=call["arguments"],
            ))
            for call in (FUNCTION_CALL, second_call)
        ],
        provider="Foundry", runtime="Foundry", model="department-assistant",
        gateway="apim", latency_ms=1, usage=None,
    )
    response._responses_output = output
    assistant = response.assistant_message()
    invocation.messages.extend([
        assistant,
        ChatMessage(role="tool", tool_call_id="call-1", content='{"tokens":120}'),
        ChatMessage(role="tool", tool_call_id="call-2", content='{"tokens":15}'),
    ])
    # A final synthesis may omit new tools but must stay on the same protocol.
    invocation.tools = None

    def handler(incoming: httpx.Request) -> httpx.Response:
        body = json.loads(incoming.content)
        assert incoming.url.path.endswith("/responses")
        assert body["input"][2:] == output + [
            {"type": "function_call_output", "call_id": "call-1", "output": '{"tokens":120}'},
            {"type": "function_call_output", "call_id": "call-2", "output": '{"tokens":15}'},
        ]
        return httpx.Response(200, json={"output": [ANSWER]})

    result = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM, httpx.Client(transport=httpx.MockTransport(handler)),
    ).invoke(invocation, route)
    assert result.content == "OK"
    assert result.tool_calls == ()
    assert "opaque-current-turn-reasoning" not in response.model_dump_json()
    assert "opaque-current-turn-reasoning" not in assistant.model_dump_json()
    assert "_responses_output" not in response.model_json_schema()["properties"]
    assert "_responses_output" not in assistant.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        ChatMessage.model_validate({
            "role": "assistant", "content": "OK", "_responses_output": output,
        })


def test_responses_translate_chat_tool_history_and_json_format(
    invocation: ModelInvocationRequest, route: dict[str, Any],
) -> None:
    invocation.response_format = "json_object"
    invocation.messages.extend([
        ChatMessage(role="assistant", tool_calls=[ToolCall(
            id="call-1", function=ToolFunctionCall(
                name="query_usage", arguments='{"dimension":"department"}',
            ),
        )]),
        ChatMessage(role="tool", tool_call_id="call-1", content='{"tokens":120}'),
    ])

    def handler(incoming: httpx.Request) -> httpx.Response:
        body = json.loads(incoming.content)
        assert body["text"] == {"format": {"type": "json_object"}}
        assert body["input"][2:] == [
            {
                "type": "function_call", "call_id": "call-1", "name": "query_usage",
                "arguments": '{"dimension":"department"}',
            },
            {"type": "function_call_output", "call_id": "call-1", "output": '{"tokens":120}'},
        ]
        return httpx.Response(200, json={"output": [ANSWER]})

    OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM, httpx.Client(transport=httpx.MockTransport(handler)),
    ).invoke(invocation, route)


def test_responses_send_explicit_message_types_for_all_history_roles(
    invocation: ModelInvocationRequest, route: dict[str, Any],
) -> None:
    invocation.messages.extend([
        ChatMessage(role="assistant", content="The previous result was 120 tokens."),
        ChatMessage(role="user", content="Compare that with this month."),
        ChatMessage(
            role="assistant", content="Checking the latest usage.",
            tool_calls=[ToolCall(id="call-1", function=ToolFunctionCall(
                name="query_usage", arguments='{"dimension":"department"}',
            ))],
        ),
        ChatMessage(role="tool", tool_call_id="call-1", content='{"tokens":140}'),
    ])

    def handler(incoming: httpx.Request) -> httpx.Response:
        body = json.loads(incoming.content)
        for item in body["input"]:
            assert item["type"] in {"message", "function_call", "function_call_output"}
            if item["type"] != "message":
                continue
            assert isinstance(item["content"], list)
            for block in item["content"]:
                expected_type = "output_text" if item["role"] == "assistant" else "input_text"
                assert block["type"] == expected_type
                assert isinstance(block["text"], str)
        assert body["input"][2] == {
            "type": "message", "role": "assistant",
            "content": [{
                "type": "output_text", "text": "The previous result was 120 tokens.",
                "annotations": [],
            }],
        }
        assert body["input"][-2]["type"] == "function_call"
        assert body["input"][-1] == {
            "type": "function_call_output", "call_id": "call-1", "output": '{"tokens":140}',
        }
        return httpx.Response(200, json={"output": [ANSWER]})

    result = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM, httpx.Client(transport=httpx.MockTransport(handler)),
    ).invoke(invocation, route)
    assert result.content == "OK"


@pytest.mark.parametrize("model,tools", [("gpt-5.6-luna", True), ("gpt-6.1-sol", False)])
def test_unaffected_requests_still_use_chat_completions(
    invocation: ModelInvocationRequest, route: dict[str, Any], model: str, tools: bool,
) -> None:
    route["upstream_model_id"] = model
    if not tools:
        invocation.tools = None

    def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.url.path.endswith("/chat/completions")
        body = json.loads(incoming.content)
        assert body["max_completion_tokens"] == 1200
        assert ("tools" in body) is tools
        assert "input" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    result = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM, httpx.Client(transport=httpx.MockTransport(handler)),
    ).invoke(invocation, route)
    assert result.content == "OK"
    assert result.responses_output is None


@pytest.mark.parametrize("payload", [
    {"output": []},
    {"output": [REASONING]},
    {"output": None},
    {"choices": [{"message": {"content": "wrong protocol"}}]},
    {"output": [{"type": "function_call", "name": "query_usage"}]},
    {"output": [{"type": "message", "content": None}]},
    {"output": [{"type": "message", "content": [{"type": "output_text", "text": 42}]}]},
    {"output": [ANSWER], "status": "incomplete"},
    {"output": [ANSWER], "status": "failed"},
])
def test_invalid_or_unfinished_responses_are_not_reported_as_answers(
    invocation: ModelInvocationRequest, route: dict[str, Any], payload: dict[str, Any],
) -> None:
    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))),
    )
    with pytest.raises(GatewayInvocationError):
        adapter.invoke(invocation, route)


@pytest.mark.parametrize("status", [400, 403, 429, 500])
def test_responses_errors_preserve_details_and_headers_without_retry(
    invocation: ModelInvocationRequest, route: dict[str, Any], status: int,
) -> None:
    sent = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        sent.append(incoming)
        return httpx.Response(
            status,
            headers={"x-correlation-id": "apim-error", "retry-after": "60"},
            json={"error": {
                "message": "Rejected", "param": "model", "request_body": "private request",
            }},
        )

    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM, httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GatewayInvocationError) as caught:
        adapter.invoke(invocation, route)
    assert len(sent) == 1
    assert caught.value.status_code == status
    assert caught.value.headers == {"x-correlation-id": "apim-error", "retry-after": "60"}
    assert "param=model" in str(caught.value)
    assert "private request" not in str(caught.value)


def test_streamed_responses_tools_are_rejected_before_dispatch(
    invocation: ModelInvocationRequest, route: dict[str, Any],
) -> None:
    invocation.stream = True

    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Streaming tool calls must not be sent to the provider")

    with pytest.raises(GatewayInvocationError) as caught:
        OpenAICompatibleGatewayAdapter(
            GatewayKind.APIM, httpx.Client(transport=httpx.MockTransport(handler)),
        ).invoke(invocation, route)
    assert caught.value.status_code == 400
