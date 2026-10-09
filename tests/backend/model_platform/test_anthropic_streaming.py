from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.backend.model_platform.test_anthropic_tools import _anthropic_route
from tests.backend.model_platform.test_responses_gateway import invocation as invocation
from tests.backend.model_platform.test_responses_gateway import route as route
from turnstile_core.domain.runtime_models import (
    ChatMessage,
    GatewayKind,
    ModelInvocationRequest,
    ModelInvocationResponse,
)
from turnstile_core.integrations.gateway import (
    AnthropicMessagesGatewayAdapter,
    GatewayInvocationError,
)


def start() -> dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg-stream-1",
            "type": "message",
            "role": "assistant",
            "content": [],
            "stop_reason": None,
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
                "output_tokens": 1,
            },
        },
    }


def text_events(text: str = "OK") -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
    ]


def finish(reason: str = "end_turn", tokens: int = 7) -> list[dict[str, Any]]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": reason, "stop_sequence": None},
            "usage": {"output_tokens": tokens},
        },
        {"type": "message_stop"},
    ]


def sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        for event in events
    ).encode()


class ChunkedStream(httpx.SyncByteStream):
    def __init__(self, data: bytes, error: httpx.HTTPError | None = None) -> None:
        self.data = data
        self.error = error
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for offset in range(0, len(self.data), 7):
            yield self.data[offset : offset + 7]
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("kind", [GatewayKind.APIM, GatewayKind.DIRECT, GatewayKind.LITELLM])
def test_anthropic_native_stream_assembles_text_and_cumulative_usage(
    invocation: ModelInvocationRequest,
    route: dict[str, Any],
    kind: GatewayKind,
) -> None:
    invocation.stream = True
    invocation.tools = None
    events = (
        [start(), {"type": "ping"}, {"type": "future_event"}]
        + [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": "Hello "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "\u4e16\u754c"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "citations_delta",
                    "citation": {"type": "char_location", "start_char_index": 0},
                },
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 3}},
        ]
        + finish()
    )
    stream = ChunkedStream(b": keepalive\n\n" + sse(events))
    sent = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        sent.append(incoming)
        body = json.loads(incoming.content)
        assert body["stream"] is True
        assert body["messages"] == [{"role": "user", "content": "Which department uses the most?"}]
        assert body["system"] == "Use tools to query actual usage."
        assert body["model"] == (
            "department-assistant" if kind is GatewayKind.APIM else "claude-opus-5-5"
        )
        assert incoming.headers["anthropic-version"] == "2023-06-01"
        assert incoming.headers["x-user-id"] == "user-1"
        assert incoming.headers["x-hive-run-id"] == "conversation-1"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "request-id": "claude-stream-1"},
            stream=stream,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = AnthropicMessagesGatewayAdapter(kind, client).invoke(
            invocation, _anthropic_route(route)
        )
    assert result.content == "Hello \u4e16\u754c"
    assert result.tool_calls == ()
    assert result.correlation_id == "claude-stream-1"
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.cached_tokens == 25
    assert result.usage.cache_write_tokens == 5
    assert result.usage.output_tokens == 7
    assert result.usage.estimated is False
    assert result.anthropic_content is not None
    assert result.anthropic_content[0]["citations"][0]["type"] == "char_location"
    assert stream.closed and len(sent) == 1


def test_streamed_thinking_signature_and_parallel_tool_parameters_replay_privately(
    invocation: ModelInvocationRequest,
    route: dict[str, Any],
) -> None:
    invocation.stream = True
    events = [
        start(),
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "private reasoning"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "opaque-"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "signature"},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    for index in (1, 2):
        events.extend(
            [
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": f"toolu-{index}",
                        "name": "query_usage",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": '{"dimension":'},
                },
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": '"department"}'},
                },
                {"type": "content_block_stop", "index": index},
            ]
        )
    events += finish("tool_use")
    sent = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        body = json.loads(incoming.content)
        sent.append(body)
        if len(sent) == 1:
            assert body["tools"][0]["input_schema"]["type"] == "object"
            return httpx.Response(200, stream=ChunkedStream(sse(events)))
        assert body["messages"][1] == {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "private reasoning",
                    "signature": "opaque-signature",
                },
                {
                    "type": "tool_use",
                    "id": "toolu-1",
                    "name": "query_usage",
                    "input": {"dimension": "department"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu-2",
                    "name": "query_usage",
                    "input": {"dimension": "department"},
                },
            ],
        }
        assert body["messages"][2] == {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu-1", "content": '{"tokens":120}'},
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu-2",
                    "content": '{"error":"Invalid dimension"}',
                    "is_error": True,
                },
            ],
        }
        return httpx.Response(200, stream=ChunkedStream(sse([start()] + text_events() + finish())))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client)
        result = adapter.invoke(invocation, _anthropic_route(route))
        assert [call.id for call in result.tool_calls] == ["toolu-1", "toolu-2"]
        completion = ModelInvocationResponse(
            request_id="r",
            correlation_id="c",
            content=result.content,
            tool_calls=list(result.tool_calls),
            provider="Foundry",
            runtime="Foundry",
            model="department-assistant",
            gateway="apim",
            latency_ms=1,
            usage=result.usage,
        )
        completion._anthropic_content = result.anthropic_content
        message = completion.assistant_message()
        invocation.messages += [
            message,
            ChatMessage(role="tool", tool_call_id="toolu-1", content='{"tokens":120}'),
            ChatMessage(
                role="tool", tool_call_id="toolu-2", content='{"error":"Invalid dimension"}'
            ),
        ]
        assert adapter.invoke(invocation, _anthropic_route(route)).content == "OK"
    assert "private reasoning" not in completion.model_dump_json()
    assert "opaque-signature" not in message.model_dump_json()
    assert "opaque-signature" not in repr(result)


@pytest.mark.parametrize(
    "error_type,status",
    [
        ("overloaded_error", 529),
        ("rate_limit_error", 429),
        ("api_error", 500),
        ("authentication_error", 401),
        ("unknown_error", 502),
    ],
)
def test_in_band_errors_preserve_headers_and_known_usage_without_partial_answers(
    invocation: ModelInvocationRequest,
    route: dict[str, Any],
    error_type: str,
    status: int,
) -> None:
    invocation.stream = True
    events = (
        [start()]
        + text_events("Partial")
        + [
            {"type": "error", "error": {"type": error_type, "message": "Unavailable"}},
        ]
    )
    stream = ChunkedStream(sse(events))
    sent = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        sent.append(incoming)
        return httpx.Response(
            200,
            stream=stream,
            headers={"request-id": "failed-stream", "retry-after": "60"},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(GatewayInvocationError) as caught,
    ):
        AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation,
            _anthropic_route(route),
        )
    assert caught.value.status_code == status
    assert caught.value.headers == {"request-id": "failed-stream", "retry-after": "60"}
    assert error_type in str(caught.value) and "Partial" not in str(caught.value)
    assert caught.value.usage is not None and caught.value.usage.output_tokens == 1
    assert stream.closed and len(sent) == 1


@pytest.mark.parametrize(
    "events",
    [
        text_events() + finish(),
        [start()] + text_events(),
        [start(), start()] + text_events() + finish(),
        [
            start(),
            {
                "type": "content_block_start",
                "index": True,
                "content_block": {"type": "text", "text": ""},
            },
        ]
        + finish(),
        [
            start(),
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {"type": "text", "text": ""},
            },
        ]
        + finish(),
        [
            start(),
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Unexpected"},
            },
        ]
        + finish(),
        [start()] + text_events()[:2] + finish(),
        [start()] + text_events() + [{"type": "message_stop"}],
        [
            start(),
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{}"},
            },
        ]
        + finish(),
    ],
)
def test_invalid_or_unfinished_streams_cannot_be_reported_as_success(
    invocation: ModelInvocationRequest,
    route: dict[str, Any],
    events: list[dict[str, Any]],
) -> None:
    invocation.stream = True
    stream = ChunkedStream(sse(events))
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        ) as client,
        pytest.raises(GatewayInvocationError),
    ):
        AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation,
            _anthropic_route(route),
        )
    assert stream.closed


@pytest.mark.parametrize("encoded", ['{"dimension":', "[]", '"not an object"'])
def test_invalid_streamed_tool_arguments_are_rejected(
    invocation: ModelInvocationRequest,
    route: dict[str, Any],
    encoded: str,
) -> None:
    invocation.stream = True
    events = [
        start(),
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu-1",
                "name": "query_usage",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": encoded},
        },
        {"type": "content_block_stop", "index": 0},
    ] + finish("tool_use")
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=ChunkedStream(sse(events))),
            )
        ) as client,
        pytest.raises(GatewayInvocationError),
    ):
        AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation,
            _anthropic_route(route),
        )


@pytest.mark.parametrize("reason", ["max_tokens", "pause_turn"])
def test_truncated_streams_keep_usage_but_never_return_the_partial_text(
    invocation: ModelInvocationRequest,
    route: dict[str, Any],
    reason: str,
) -> None:
    invocation.stream = True
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    stream=ChunkedStream(sse([start()] + text_events("Partial") + finish(reason))),
                ),
            )
        ) as client,
        pytest.raises(GatewayInvocationError) as caught,
    ):
        AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation,
            _anthropic_route(route),
        )
    assert caught.value.usage is not None and caught.value.usage.output_tokens == 7
    assert "Partial" not in str(caught.value)


@pytest.mark.parametrize(
    "error,status",
    [
        (httpx.ReadTimeout("Timed out"), 504),
        (httpx.RemoteProtocolError("Disconnected"), 502),
    ],
)
def test_transport_failures_close_the_stream_and_keep_known_usage(
    invocation: ModelInvocationRequest,
    route: dict[str, Any],
    error: httpx.HTTPError,
    status: int,
) -> None:
    invocation.stream = True
    stream = ChunkedStream(sse([start()] + text_events("Partial")), error)
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        ) as client,
        pytest.raises(GatewayInvocationError) as caught,
    ):
        AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation,
            _anthropic_route(route),
        )
    assert caught.value.status_code == status
    assert caught.value.usage is not None and caught.value.usage.input_tokens == 10
    assert stream.closed


@pytest.mark.parametrize("status", [400, 403, 429, 500])
def test_http_stream_errors_are_read_and_propagated_without_retry(
    invocation: ModelInvocationRequest,
    route: dict[str, Any],
    status: int,
) -> None:
    invocation.stream = True
    sent = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        sent.append(incoming)
        return httpx.Response(
            status,
            json={"error": {"message": "Denied", "type": "policy_denied"}},
            headers={"request-id": "denied-stream", "retry-after": "60"},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(GatewayInvocationError) as caught,
    ):
        AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation,
            _anthropic_route(route),
        )
    assert caught.value.status_code == status and len(sent) == 1
    assert caught.value.headers == {"request-id": "denied-stream", "retry-after": "60"}
    assert "policy_denied" in str(caught.value)


def test_multiline_sse_and_final_event_without_blank_line_are_supported(
    invocation: ModelInvocationRequest,
    route: dict[str, Any],
) -> None:
    invocation.stream = True
    body = sse([start()] + text_events())
    body += (
        b'data: {"type":"message_delta",\n'
        b'data: "delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}\n\n'
        b'data: {"type":"message_stop"}'
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=ChunkedStream(body)),
        )
    ) as client:
        result = AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation,
            _anthropic_route(route),
        )
    assert result.content == "OK" and result.correlation_id == "msg-stream-1"


def test_malformed_sse_json_becomes_a_safe_gateway_error(
    invocation: ModelInvocationRequest,
    route: dict[str, Any],
) -> None:
    invocation.stream = True
    stream = ChunkedStream(sse([start()]) + b'data: {"private reasoning"\n\n')
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        ) as client,
        pytest.raises(GatewayInvocationError) as caught,
    ):
        AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation,
            _anthropic_route(route),
        )
    assert "private reasoning" not in str(caught.value) and stream.closed


def test_message_stop_is_terminal_and_does_not_wait_for_more_network_data(
    invocation: ModelInvocationRequest, route: dict[str, Any],
) -> None:
    invocation.stream = True
    stream = ChunkedStream(
        sse([start()] + text_events() + finish()), httpx.ReadTimeout("After message stop"),
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    ) as client:
        result = AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation, _anthropic_route(route),
        )
    assert result.content == "OK" and stream.closed


def test_final_usage_can_update_input_and_cache_counters(
    invocation: ModelInvocationRequest, route: dict[str, Any],
) -> None:
    invocation.stream = True
    ending = finish()
    ending[0]["usage"].update(
        input_tokens=12, cache_read_input_tokens=30, cache_creation_input_tokens=8,
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=ChunkedStream(sse([start()] + text_events() + ending)),
            ),
        ),
    ) as client:
        result = AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation, _anthropic_route(route),
        )
    assert result.usage is not None
    assert result.usage.input_tokens == 12
    assert result.usage.cached_tokens == 38
    assert result.usage.cache_write_tokens == 8
    assert result.usage.output_tokens == 7


def test_missing_usage_is_not_reported_as_zero_actual_usage(
    invocation: ModelInvocationRequest, route: dict[str, Any],
) -> None:
    invocation.stream = True
    beginning = start()
    del beginning["message"]["usage"]
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=ChunkedStream(sse([beginning] + text_events() + finish())),
            ),
        ),
    ) as client:
        result = AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation, _anthropic_route(route),
        )
    assert result.content == "OK" and result.usage is None


@pytest.mark.parametrize("invalid", [-1, True, "7"])
def test_invalid_token_counters_are_rejected(
    invocation: ModelInvocationRequest, route: dict[str, Any], invalid: Any,
) -> None:
    invocation.stream = True
    ending = finish()
    ending[0]["usage"]["output_tokens"] = invalid
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, stream=ChunkedStream(sse([start()] + text_events() + ending)),
                ),
            ),
        ) as client,
        pytest.raises(GatewayInvocationError),
    ):
        AnthropicMessagesGatewayAdapter(GatewayKind.APIM, client).invoke(
            invocation, _anthropic_route(route),
        )
