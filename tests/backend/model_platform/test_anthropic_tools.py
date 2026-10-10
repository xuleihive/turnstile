from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.backend.model_platform.test_responses_gateway import invocation as invocation
from tests.backend.model_platform.test_responses_gateway import route as route
from turnstile_core.domain.runtime_models import (
    ChatMessage,
    GatewayKind,
    InvocationApiFormat,
    ModelInvocationRequest,
    ModelInvocationResponse,
    ToolCall,
    ToolFunctionCall,
)
from turnstile_core.integrations.gateway import (
    AnthropicMessagesGatewayAdapter,
    GatewayInvocationError,
)

THINKING = {"type": "thinking", "thinking": "private reasoning", "signature": "opaque-signature"}
TOOL_USE: dict[str, Any] = {
    "type": "tool_use", "id": "toolu-1", "name": "query_usage",
    "input": {"dimension": "department"},
}


def _anthropic_route(route: dict[str, Any]) -> dict[str, Any]:
    return {
        **route,
        "upstream_model_id": "claude-opus-5-5",
        "runtime_config": {
            **route["runtime_config"], "path": "/v1/messages", "api_format": "anthropic_messages",
        },
    }


@pytest.mark.parametrize("kind", [GatewayKind.APIM, GatewayKind.DIRECT, GatewayKind.LITELLM])
def test_anthropic_tools_map_definitions_calls_cache_usage_and_attribution(
    invocation: ModelInvocationRequest, route: dict[str, Any], kind: GatewayKind,
) -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        body = json.loads(incoming.content)
        assert incoming.url.path == "/turnstile/llm/v1/messages"
        assert body["model"] == (
            "department-assistant" if kind is GatewayKind.APIM else "claude-opus-5-5"
        )
        assert body["system"] == "Use tools to query actual usage."
        assert body["messages"] == [{"role": "user", "content": "Which department uses the most?"}]
        assert body["tools"] == [{
            "name": "query_usage",
            "description": "Query actual usage.",
            "input_schema": {"type": "object", "properties": {"dimension": {"type": "string"}}},
        }]
        assert body["max_tokens"] == 1200
        assert body["stream"] is False
        assert "temperature" not in body
        assert incoming.headers["anthropic-version"] == "2023-06-01"
        assert incoming.headers["x-user-id"] == "user-1"
        assert incoming.headers["x-request-source"] == "assistant"
        assert incoming.headers["x-hive-model"] == "department-assistant"
        return httpx.Response(200, headers={"request-id": "claude-request-1"}, json={
            "stop_reason": "tool_use", "content": [THINKING, TOOL_USE],
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 20,
                      "cache_creation_input_tokens": 5, "output_tokens": 4},
        })

    result = AnthropicMessagesGatewayAdapter(
        kind, httpx.Client(transport=httpx.MockTransport(handler)),
    ).invoke(invocation, _anthropic_route(route))
    assert result.content == ""
    assert result.tool_calls[0].id == "toolu-1"
    assert json.loads(result.tool_calls[0].function.arguments) == {"dimension": "department"}
    assert result.correlation_id == "claude-request-1"
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.cached_tokens == 25
    assert result.usage.cache_write_tokens == 5
    assert result.usage.output_tokens == 4
    assert "opaque-signature" not in repr(result)


def test_anthropic_parallel_results_follow_signed_thinking_without_leaking_it(
    invocation: ModelInvocationRequest, route: dict[str, Any],
) -> None:
    second = {**TOOL_USE, "id": "toolu-2"}
    response = ModelInvocationResponse(
        request_id="r", correlation_id="c", content="Checking usage.",
        tool_calls=[
            ToolCall(id=block["id"], function=ToolFunctionCall(
                name=block["name"], arguments=json.dumps(block["input"]),
            ))
            for block in (TOOL_USE, second)
        ],
        provider="Foundry", runtime="Foundry", model="department-assistant",
        gateway="apim", latency_ms=1, usage=None,
    )
    blocks: list[dict[str, Any]] = [
        THINKING, {"type": "text", "text": "Checking usage."}, TOOL_USE, second,
    ]
    response._anthropic_content = blocks
    message = response.assistant_message()
    invocation.messages.extend([
        message,
        ChatMessage(role="tool", tool_call_id="toolu-1", content='{"tokens":120}'),
        ChatMessage(role="tool", tool_call_id="toolu-2", content='{"error":"Invalid dimension"}'),
    ])

    def handler(incoming: httpx.Request) -> httpx.Response:
        body = json.loads(incoming.content)
        assert body["messages"][1:] == [
            {"role": "assistant", "content": blocks},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu-1", "content": '{"tokens":120}'},
                {"type": "tool_result", "tool_use_id": "toolu-2",
                 "content": '{"error":"Invalid dimension"}', "is_error": True},
            ]},
        ]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Done."}]})

    result = AnthropicMessagesGatewayAdapter(
        GatewayKind.APIM, httpx.Client(transport=httpx.MockTransport(handler)),
    ).invoke(invocation, _anthropic_route(route))
    assert result.content == "Done."
    assert result.tool_calls == ()
    assert "opaque-signature" not in response.model_dump_json()
    assert "private reasoning" not in message.model_dump_json()


def test_anthropic_converts_platform_tool_history_without_native_blocks(
    invocation: ModelInvocationRequest, route: dict[str, Any],
) -> None:
    invocation.messages.extend([
        ChatMessage(role="assistant", tool_calls=[ToolCall(
            id="toolu-1", function=ToolFunctionCall(
                name="query_usage", arguments='{"dimension":"department"}',
            ),
        )]),
        ChatMessage(role="tool", tool_call_id="toolu-1", content="120"),
    ])

    def handler(incoming: httpx.Request) -> httpx.Response:
        assert json.loads(incoming.content)["messages"][1]["content"] == [TOOL_USE]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Done."}]})

    AnthropicMessagesGatewayAdapter(
        GatewayKind.APIM, httpx.Client(transport=httpx.MockTransport(handler)),
    ).invoke(invocation, _anthropic_route(route))


@pytest.mark.parametrize("payload", [
    {"content": []},
    {"content": [THINKING]},
    {"content": None},
    {"content": [{"type": "tool_use", "id": "toolu-1"}]},
    {"content": [{**TOOL_USE, "input": "not an object"}]},
    {"content": [{"type": "text", "text": 42}]},
    {"content": [{"type": "text", "text": "Partial"}], "stop_reason": "max_tokens"},
])
def test_malformed_anthropic_output_cannot_become_an_answer(
    invocation: ModelInvocationRequest, route: dict[str, Any], payload: dict[str, Any],
) -> None:
    adapter = AnthropicMessagesGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))),
    )
    with pytest.raises(GatewayInvocationError):
        adapter.invoke(invocation, _anthropic_route(route))


@pytest.mark.parametrize("status", [400, 403, 429, 500])
def test_anthropic_errors_keep_status_and_correlation_without_retry(
    invocation: ModelInvocationRequest, route: dict[str, Any], status: int,
) -> None:
    sent = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        sent.append(incoming)
        return httpx.Response(status, headers={"request-id": "claude-denied", "retry-after": "60"},
                              json={"error": {"message": "Denied", "type": "policy_denied"}})

    with pytest.raises(GatewayInvocationError) as caught:
        AnthropicMessagesGatewayAdapter(
            GatewayKind.APIM, httpx.Client(transport=httpx.MockTransport(handler)),
        ).invoke(invocation, _anthropic_route(route))
    assert len(sent) == 1
    assert caught.value.status_code == status
    assert caught.value.headers == {"request-id": "claude-denied", "retry-after": "60"}
    assert "policy_denied" in str(caught.value)


@pytest.mark.parametrize("stream", [True, False])
def test_incompatible_anthropic_protocol_is_rejected_before_dispatch(
    invocation: ModelInvocationRequest, route: dict[str, Any],
    stream: bool,
) -> None:
    invocation.stream = stream
    invocation.api_format = InvocationApiFormat.OPENAI_RESPONSES

    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("An unsupported request must not be dispatched")

    with pytest.raises(GatewayInvocationError) as caught:
        AnthropicMessagesGatewayAdapter(
            GatewayKind.APIM, httpx.Client(transport=httpx.MockTransport(handler)),
        ).invoke(invocation, _anthropic_route(route))
    assert caught.value.status_code == 400
