from __future__ import annotations

import base64
import json
from io import BytesIO
from typing import Any

import httpx
import pytest
from PIL import Image

from tests.backend.model_platform.test_image_options import _limits, _request
from turnstile_core.domain.image_profiles import create_image_profile
from turnstile_core.integrations.gateway import GatewayInvocationError
from turnstile_core.integrations.image_generation import (
    OpenAIImageGatewayAdapter,
    generated_image,
    image_usage,
)


def unit_image_bytes(image_format: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 48), "white").save(output, format=image_format)
    return output.getvalue()


def usage_payload() -> dict[str, Any]:
    return {
        "usage": {
            "input_tokens": 26,
            "output_tokens": 196,
            "total_tokens": 222,
            "input_tokens_details": {"text_tokens": 26, "image_tokens": 0},
            "output_tokens_details": {"text_tokens": 0, "image_tokens": 196},
        }
    }


def _route(**overrides: Any) -> dict[str, Any]:
    return {
        "gateway_implementation": "apim",
        "gateway_base_url": "https://gateway.example/llm",
        "runtime_config": {"control_plane_managed": True, "path": "/chat/completions"},
        "model_key": "image-alias",
        "model_id": "model-id",
        "runtime_id": "runtime-id",
        "runtime_name": "Foundry",
        "request_id": "image-request",
        "image_profile": create_image_profile(_limits(), "unit-image").model_dump(),
        **overrides,
    }


def test_image_usage_retains_modality_and_cached_subset() -> None:
    payload = usage_payload()
    payload["usage"]["input_tokens_details"]["cached_tokens"] = 20
    result = image_usage(payload)
    assert result is not None
    assert (result.input_tokens, result.cached_tokens, result.output_tokens) == (6, 20, 196)
    assert result.estimated is False


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "total_tokens"])
@pytest.mark.parametrize("value", [True, -1, 0.5, "26", None])
def test_image_usage_invalid_types_are_not_exact(field: str, value: object) -> None:
    payload = usage_payload()
    payload["usage"][field] = value
    assert image_usage(payload) is None


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 0}, "invalid"])
def test_missing_image_usage_is_unknown(usage: object) -> None:
    assert image_usage({"usage": usage}) is None


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_safe_image_decode_detects_format(image_format: str) -> None:
    result = generated_image(
        {"data": [{"b64_json": base64.b64encode(unit_image_bytes(image_format)).decode()}]}
    )
    assert result.media_type == "image/" + image_format.lower()
    assert result.b64_json not in repr(result)


@pytest.mark.parametrize("content", [b"\x89PNG\r\n\x1a\njunk", b"<svg></svg>", b"\xff\xd8\xffjunk"])
def test_corrupt_or_nonpreview_image_rejected(content: bytes) -> None:
    with pytest.raises(ValueError):
        generated_image({"data": [{"b64_json": base64.b64encode(content).decode()}]})


def test_image_transport_is_bounded_attributed_and_provider_owned() -> None:
    captured = []

    def respond(incoming: httpx.Request) -> httpx.Response:
        captured.append(incoming)
        return httpx.Response(
            200,
            json={
                **usage_payload(),
                "data": [{"b64_json": base64.b64encode(unit_image_bytes()).decode()}],
            },
            headers={"x-correlation-id": "image-correlation"},
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIImageGatewayAdapter(client)
        result = adapter.generate(_request(quality=None, vendor_option={"new": True}), _route())
        assert result.size == "32x48" and result.output_format == "png"
        assert result.correlation_id == "image-correlation"
        assert result.usage is not None and result.usage.output_tokens == 196
        assert captured[0].url.path == "/llm/images/generations"
        assert captured[0].headers["x-request-id"] == "image-request"
        assert json.loads(captured[0].content) == {
            "prompt": "A ceramic cup",
            "model": "image-alias",
            "quality": None,
            "vendor_option": {"new": True},
        }
        with pytest.raises(GatewayInvocationError, match="managed APIM"):
            adapter.generate(_request(), _route(gateway_implementation="direct"))
    assert len(captured) == 1


@pytest.mark.parametrize("status_code", [400, 422, 429, 500])
def test_provider_error_is_not_retried_or_exposed(status_code: int) -> None:
    captured = []

    def respond(incoming: httpx.Request) -> httpx.Response:
        captured.append(incoming)
        return httpx.Response(
            status_code,
            json={"error": "sensitive body"},
            headers={
                "x-correlation-id": "error-correlation",
                "retry-after": "5",
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        pytest.raises(GatewayInvocationError) as raised,
    ):
        OpenAIImageGatewayAdapter(client).generate(_request(), _route())
    assert raised.value.status_code == status_code
    assert raised.value.headers["x-correlation-id"] == "error-correlation"
    assert raised.value.usage is None
    assert "sensitive body" not in str(raised.value)
    assert len(captured) == 1


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            {"code": "invalid_value", "param": "output_format",
             "message": "Invalid value: 'webp'. Prompt: A ceramic cup"},
            "HTTP 400; code=invalid_value; param=output_format",
        ),
        ({"code": "contentFilter", "param": None}, "HTTP 400; code=contentFilter"),
        ({"code": "A ceramic cup", "param": "x" * 65}, "HTTP 400"),
    ],
)
def test_provider_error_reason_keeps_identifiers_but_never_messages(
    error: dict[str, Any], reason: str,
) -> None:
    with httpx.Client(transport=httpx.MockTransport(
        lambda incoming: httpx.Response(400, json={"error": error}),
    )) as client, pytest.raises(GatewayInvocationError) as raised:
        OpenAIImageGatewayAdapter(client).generate(_request(), _route())
    assert str(raised.value) == f"Image gateway request failed ({reason})"
    assert raised.value.status_code == 400
    assert "ceramic" not in str(raised.value)


def test_invalid_image_response_preserves_measured_usage() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(
                200,
                json={**usage_payload(), "data": [{"b64_json": "invalid"}]},
                headers={"x-correlation-id": "invalid-image"},
            )
        )
    ) as client, pytest.raises(GatewayInvocationError) as raised:
        OpenAIImageGatewayAdapter(client).generate(_request(), _route())
    assert raised.value.usage is not None and raised.value.usage.output_tokens == 196
    assert raised.value.headers["x-correlation-id"] == "invalid-image"
