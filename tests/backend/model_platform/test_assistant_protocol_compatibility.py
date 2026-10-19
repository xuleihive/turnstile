from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.backend.model_platform.test_responses_gateway import ANSWER
from tests.backend.model_platform.test_responses_gateway import invocation as invocation
from tests.backend.model_platform.test_responses_gateway import route as route
from turnstile_core.domain.runtime_models import (
    GatewayKind,
    InvocationApiFormat,
    ModelInvocationRequest,
)
from turnstile_core.integrations.gateway import (
    AnthropicMessagesGatewayAdapter,
    GatewayInvocationError,
    OpenAICompatibleGatewayAdapter,
    supports_tool_calling,
)
from turnstile_core.integrations.gateway_protocol import invocation_api_paths

PROTOCOLS = list(InvocationApiFormat)


def adapter(
    protocol: InvocationApiFormat, client: httpx.Client,
) -> OpenAICompatibleGatewayAdapter:
    adapter_type = (
        AnthropicMessagesGatewayAdapter
        if protocol is InvocationApiFormat.ANTHROPIC_MESSAGES
        else OpenAICompatibleGatewayAdapter
    )
    return adapter_type(GatewayKind.APIM, client)


def configure(
    invocation: ModelInvocationRequest, route: dict[str, Any], protocol: InvocationApiFormat,
) -> None:
    invocation.api_format = protocol
    route["runtime_config"]["api_format"] = protocol.value
    route["runtime_config"]["path"] = {
        InvocationApiFormat.OPENAI_CHAT: "/chat/completions",
        InvocationApiFormat.OPENAI_RESPONSES: "/responses",
        InvocationApiFormat.ANTHROPIC_MESSAGES: "/v1/messages",
    }[protocol]
    route["upstream_model_id"] = "compatible-model"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_protocols_keep_gateway_and_provider_request_ids_separate(
    invocation: ModelInvocationRequest, route: dict[str, Any], protocol: InvocationApiFormat,
) -> None:
    configure(invocation, route, protocol)
    payloads = {
        InvocationApiFormat.OPENAI_CHAT: {"choices": [{"message": {"content": "OK"}}]},
        InvocationApiFormat.OPENAI_RESPONSES: {"status": "completed", "output": [ANSWER]},
        InvocationApiFormat.ANTHROPIC_MESSAGES: {
            "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
        },
    }
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"x-correlation-id": "gateway-attempt", "request-id": "provider-call"},
        json=payloads[protocol],
    ))) as client:
        result = adapter(protocol, client).invoke(invocation, route)
    assert result.correlation_id == "gateway-attempt"
    assert result.upstream_request_id == "provider-call"


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("failure", ["timeout", "transport_error", "invalid_response"])
def test_protocol_failures_preserve_trace_classification_and_response_headers(
    invocation: ModelInvocationRequest, route: dict[str, Any],
    protocol: InvocationApiFormat, failure: str,
) -> None:
    configure(invocation, route, protocol)

    class BrokenResponse(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            if failure == "timeout":
                raise httpx.ReadTimeout("interrupted")
            if failure == "transport_error":
                raise httpx.ReadError("interrupted")
            yield b"not-json"

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, stream=BrokenResponse(),
        headers={"x-correlation-id": "gateway-attempt", "request-id": "provider-call"},
    ))) as client, pytest.raises(GatewayInvocationError) as caught:
        adapter(protocol, client).invoke(invocation, route)
    assert caught.value.failure_kind == failure
    assert caught.value.headers["x-correlation-id"] == "gateway-attempt"
    assert caught.value.headers["request-id"] == "provider-call"
    assert caught.value.status_code == (504 if failure == "timeout" else 502)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_protocol_requests_never_follow_redirects_with_provider_credentials(
    invocation: ModelInvocationRequest, route: dict[str, Any], protocol: InvocationApiFormat,
) -> None:
    configure(invocation, route, protocol)
    seen = []

    def redirect(incoming: httpx.Request) -> httpx.Response:
        seen.append(incoming)
        return httpx.Response(302, headers={"location": "https://untrusted.test/collect"})

    with (
        httpx.Client(follow_redirects=True, transport=httpx.MockTransport(redirect)) as client,
        pytest.raises(GatewayInvocationError) as caught,
    ):
        adapter(protocol, client).invoke(invocation, route)
    assert len(seen) == 1
    assert seen[0].url.host == "gateway.test"
    assert caught.value.failure_kind == "http_error"


def test_decision_models_are_not_advertised_as_assistant_tool_routes(route: dict[str, Any]) -> None:
    route["model_capabilities"] = ["decision", "tools"]
    assert invocation_api_paths(route) == {}
    route["model_capabilities"] = ["tools"]
    route["runtime_config"]["api_format"] = "microsoft_decision"
    assert invocation_api_paths(route) == {}
    assert not supports_tool_calling("openai_compatible", route["runtime_config"])
