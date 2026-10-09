from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from backend.api import app, runtime_service
from backend.http.dependencies import get_repository
from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.test_anthropic_streaming import finish, sse, start, text_events
from tests.backend.model_platform.test_model_runtime import _foundry_route, request
from tests.platform.api.api_support import client
from turnstile_core.config import Settings
from turnstile_core.integrations.gateway import GatewayRouter
from turnstile_core.persistence.in_memory import InMemoryRepository

pytest_plugins = ("tests.platform.api.api_fixtures",)


@pytest.mark.parametrize("error", [False, True])
def test_invocation_console_can_stream_claude_on_the_shared_foundry_runtime(error: bool) -> None:
    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    model.update(
        model_key="claude-opus-5-5-foundry-test",
        display_name="Claude Opus 5.5",
        upstream_model_id="claude-opus-5-5",
        family_key="claude",
    )
    seen: list[dict[str, Any]] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.url.path.endswith("/v1/messages")
        body = json.loads(incoming.content)
        seen.append(body)
        assert body["stream"] is True
        assert body["model"] == model["model_key"]
        events = [start()]
        if error:
            events.append({
                "type": "error",
                "error": {"type": "overloaded_error", "message": "Busy"},
            })
        else:
            events += text_events("CLAUDE_STREAM_OK") + finish()
        return httpx.Response(
            200, content=sse(events),
            headers={"content-type": "text/event-stream", "request-id": "claude-api-stream",
                     "retry-after": "60"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as gateway_client:
        service = ModelRuntimeService(repository, Settings(), GatewayRouter(gateway_client))
        app.dependency_overrides[get_repository] = lambda: repository
        app.dependency_overrides[runtime_service] = lambda: service
        try:
            payload = request(model=model["model_key"], runtime=runtime["name"]).model_copy(
                update={"model_id": model["id"], "runtime_id": runtime["id"], "stream": True},
            ).model_dump(mode="json")
            payload["metadata"]["request_source"] = "agent-invocation-module"
            response = client.post("/api/v1/model-gateway/invoke", json=payload)
        finally:
            app.dependency_overrides.pop(runtime_service, None)
            app.dependency_overrides.pop(get_repository, None)
    assert len(seen) == 1
    if error:
        assert response.status_code == 529
        assert response.headers["request-id"] == "claude-api-stream"
        assert response.headers["retry-after"] == "60"
        assert "overloaded_error" in response.json()["detail"]
    else:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        body = response.json()
        assert body["content"] == "CLAUDE_STREAM_OK"
        assert body["correlation_id"] == "claude-api-stream"
        assert body["usage"]["input_tokens"] == 10
        assert body["usage"]["cached_tokens"] == 25
        assert body["usage"]["cache_write_tokens"] == 5
        assert body["usage"]["output_tokens"] == 7
        assert body["usage"]["estimated"] is False
        assert "anthropic_content" not in body
