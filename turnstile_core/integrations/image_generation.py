from __future__ import annotations

import base64
import binascii
import json
import re
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Literal, cast

import httpx
from PIL import Image

from ..domain.images import (
    IMAGE_MAX_RESPONSE_BYTES,
    GeneratedImage,
    ImageFormat,
    ImageInvocationRequest,
)
from ..domain.runtime_models import GatewayKind, InvocationUsage
from .gateway import GatewayInvocationError, OpenAICompatibleGatewayAdapter


def image_usage(payload: Mapping[str, Any]) -> InvocationUsage | None:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    if not isinstance(input_details, Mapping) or not isinstance(output_details, Mapping):
        return None
    cached = input_details.get("cached_tokens", 0)
    counters = (
        input_tokens,
        output_tokens,
        total_tokens,
        cached,
        input_details.get("text_tokens"),
        input_details.get("image_tokens"),
        output_details.get("text_tokens"),
        output_details.get("image_tokens"),
    )
    if any(type(value) is not int or value < 0 for value in counters):
        return None
    input_tokens = cast(int, input_tokens)
    output_tokens = cast(int, output_tokens)
    if (
        total_tokens != input_tokens + output_tokens
        or cached > input_tokens
        or input_details["text_tokens"] != input_tokens
        or input_details["image_tokens"] != 0
        or output_details["text_tokens"] != 0
        or output_details["image_tokens"] != output_tokens
    ):
        return None
    return InvocationUsage(
        input_tokens=input_tokens - cached,
        cached_tokens=cached,
        output_tokens=output_tokens,
        estimated=False,
    )


def _decode_generated_image(
    payload: Mapping[str, Any], output_format: str | None = None, expected_size: str | None = None,
) -> tuple[GeneratedImage, str]:
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("Expected one generated image")
    encoded = data[0].get("b64_json")
    if not isinstance(encoded, str) or not 0 < len(encoded) <= IMAGE_MAX_RESPONSE_BYTES:
        raise ValueError("Missing or oversized generated image")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Invalid generated image encoding") from error
    explicit_size = re.fullmatch(r"([0-9]+)x([0-9]+)", expected_size or "")
    expected_dimensions = (
        tuple(int(edge) for edge in explicit_size.groups()) if explicit_size else None
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as decoded:
                if decoded.format not in ("PNG", "JPEG", "WEBP"):
                    raise ValueError("Generated image format cannot be previewed safely")
                if (output_format is not None and decoded.format != output_format.upper()) or (
                    expected_dimensions is not None and decoded.size != expected_dimensions
                ):
                    raise ValueError("Generated image does not match the requested format and size")
                if getattr(decoded, "n_frames", 1) != 1:
                    raise ValueError("Animated images are not supported")
                size = f"{decoded.width}x{decoded.height}"
                decoded_format = decoded.format.lower()
                decoded.verify()
            with Image.open(BytesIO(content)) as decoded:
                decoded.load()
    except (
        OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning,
    ) as error:
        raise ValueError("Generated image could not be decoded safely") from error
    media_type = cast(Literal["image/png", "image/jpeg", "image/webp"], f"image/{decoded_format}")
    return GeneratedImage(b64_json=encoded, media_type=media_type), size


def generated_image(
    payload: Mapping[str, Any], output_format: str | None = None, expected_size: str | None = None,
) -> GeneratedImage:
    image, _ = _decode_generated_image(payload, output_format, expected_size)
    return image


_PROVIDER_ERROR_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")


def _provider_error_reason(response: httpx.Response) -> str:
    # Only identifier fields leave the provider body; messages can echo prompts.
    reason = f"HTTP {response.status_code}"
    body = bytearray()
    try:
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 65_536:
                return reason
        error = json.loads(body).get("error")
    except (httpx.HTTPError, ValueError, AttributeError):
        return reason
    if not isinstance(error, Mapping):
        return reason
    return "; ".join([reason, *(
        f"{name}={value}" for name in ("code", "param")
        if isinstance(value := error.get(name), str)
        and _PROVIDER_ERROR_IDENTIFIER.fullmatch(value)
    )])


@dataclass(frozen=True)
class ImageGatewayResult:
    image: GeneratedImage = field(repr=False)
    usage: InvocationUsage | None
    correlation_id: str | None
    size: str
    quality: str | None
    gateway: str = GatewayKind.APIM.value

    @property
    def output_format(self) -> ImageFormat:
        return cast(ImageFormat, self.image.media_type.removeprefix("image/"))


class OpenAIImageGatewayAdapter(OpenAICompatibleGatewayAdapter):
    def __init__(self, client: httpx.Client | None = None) -> None:
        super().__init__(GatewayKind.APIM, client)

    def generate(
        self, request: ImageInvocationRequest, route: dict[str, Any]
    ) -> ImageGatewayResult:
        if (
            route.get("gateway_implementation") != GatewayKind.APIM
            or not route.get("gateway_base_url")
            or not (route.get("runtime_config") or {}).get("control_plane_managed")
        ):
            raise GatewayInvocationError(
                "Image generation requires a managed APIM route", status_code=409
            )
        try:
            profile = request.validate_profile(route.get("image_profile"))
            request.reservation_tokens(str(route["model_key"]), profile)
            body = request.provider_payload(str(route["model_key"]), profile)
        except ValueError as error:
            raise GatewayInvocationError(str(error), status_code=409) from error
        response_headers: dict[str, str] = {}
        measured_usage: InvocationUsage | None = None
        try:
            with self._client.stream(
                "POST",
                self._base_url(route) + profile.public_path,
                headers=self._headers(request, route),
                json=body,
                timeout=profile.timeout_seconds,
            ) as response:
                response_headers = {
                    name: response.headers[name]
                    for name in ("x-request-id", "x-correlation-id", "retry-after")
                    if name in response.headers
                }
                if response.is_error:
                    raise GatewayInvocationError(
                        f"Image gateway request failed ({_provider_error_reason(response)})",
                        status_code=response.status_code,
                        headers=response_headers,
                    )
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > min(
                        profile.max_response_bytes, IMAGE_MAX_RESPONSE_BYTES
                    ):
                        raise ValueError("Image response is too large")
                    content.extend(chunk)
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError("Image response must be an object")
            measured_usage = image_usage(payload)
            image, size = _decode_generated_image(payload)
        except httpx.TimeoutException as error:
            raise GatewayInvocationError(
                "Image generation timed out; it was not retried",
                status_code=504,
                headers=response_headers,
            ) from error
        except (ValueError, httpx.HTTPError) as error:
            raise GatewayInvocationError(
                "The image gateway returned an invalid or incomplete response",
                headers=response_headers,
                usage=measured_usage,
            ) from error
        quality = payload.get("quality", request.quality)
        return ImageGatewayResult(
            image=image,
            usage=measured_usage,
            correlation_id=response_headers.get("x-correlation-id"),
            size=size,
            quality=quality if isinstance(quality, str) else None,
        )
