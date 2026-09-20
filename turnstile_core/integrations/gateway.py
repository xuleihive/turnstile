from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from ..domain.images import ImageInvocationRequest
from ..domain.runtime_models import (
    GatewayKind,
    HealthStatus,
    InvocationUsage,
    ModelInvocationRequest,
    RuntimeHealth,
    RuntimeKind,
    ToolCall,
)

if TYPE_CHECKING:
    from .image_generation import ImageGatewayResult


class GatewayInvocationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        headers: dict[str, str] | None = None,
        usage: InvocationUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}
        self.usage = usage


def _http_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if not isinstance(payload, dict):
        return f"HTTP {response.status_code}"
    nested_error = payload.get("error")
    error = nested_error if isinstance(nested_error, dict) else payload
    parts = [
        f"{field}={str(error[field]).replace(chr(10), ' ')[:500]}"
        for field in ("message", "type", "param", "code", "error_code")
        if error.get(field) is not None
    ]
    return "; ".join(parts) or f"HTTP {response.status_code}"


@dataclass(frozen=True)
class GatewayResult:
    content: str
    usage: InvocationUsage | None
    gateway: str
    correlation_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


class GatewayAdapter(Protocol):
    def invoke(self, request: ModelInvocationRequest, route: dict[str, Any]) -> GatewayResult: ...

    def check(self, route: dict[str, Any]) -> RuntimeHealth: ...


def _prompt(messages: list[dict[str, Any]]) -> str:
    # `content` is absent on an assistant turn that only called tools, and on nothing
    # else. Estimating its length as zero is right: there was no text.
    return "\n\n".join(
        f"{message['role'].upper()}: {message.get('content') or ''}" for message in messages
    )


def _estimate_usage(prompt: str, response: str) -> InvocationUsage:
    return InvocationUsage(
        input_tokens=max(1, len(prompt) // 4),
        cached_tokens=0,
        output_tokens=max(1, len(response) // 4),
        estimated=True,
    )


def _openai_usage(raw_usage: Mapping[str, Any]) -> InvocationUsage:
    details = raw_usage.get("prompt_tokens_details")
    prompt_details = details if isinstance(details, Mapping) else {}
    nested_cache_read = prompt_details.get("cached_tokens")
    cache_read = raw_usage.get("cached_tokens") if nested_cache_read is None else nested_cache_read
    if cache_read is None:
        cache_read = 0
    if type(cache_read) is not int or cache_read < 0:
        raise ValueError("Cached token usage must be a nonnegative integer or null")
    cache_write = int(prompt_details.get("cache_write_tokens", 0) or 0)
    cached = cache_read + cache_write
    prompt = int(raw_usage.get("prompt_tokens", 0) or 0)
    return InvocationUsage(
        input_tokens=max(prompt - cached, 0),
        cached_tokens=cached,
        cache_write_tokens=cache_write,
        output_tokens=int(raw_usage.get("completion_tokens", 0) or 0),
        estimated=False,
    )


def _sse_json_events(response: httpx.Response) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data != "[DONE]":
                    payload = json.loads(data)
                    if isinstance(payload, dict):
                        events.append(payload)
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        data = "\n".join(data_lines)
        if data != "[DONE]":
            payload = json.loads(data)
            if isinstance(payload, dict):
                events.append(payload)
    return events


class CliGatewayAdapter:
    def invoke(self, request: ModelInvocationRequest, route: dict[str, Any]) -> GatewayResult:
        config = dict(route["runtime_config"])
        command = str(config.get("command", ""))
        executable = shutil.which(command)
        if executable is None:
            raise GatewayInvocationError(f"Runtime command is unavailable: {command}")
        messages = [message.model_dump() for message in request.messages]
        prompt = _prompt(messages)
        model_key = str(route["model_key"])
        args = [
            str(value).replace("{prompt}", prompt).replace("{model}", model_key)
            for value in config.get("args", [])
        ]
        environment = os.environ.copy()
        provider_credential = route.get("provider_credential")
        credential_env = config.get("credential_env")
        if provider_credential and credential_env:
            environment[str(credential_env)] = str(provider_credential)
        working_directory = Path(str(config.get("working_directory", os.getcwd()))).resolve()
        if not working_directory.is_dir():
            raise GatewayInvocationError("Configured working directory does not exist")
        try:
            completed = subprocess.run(
                [executable, *args],
                cwd=working_directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=float(config.get("timeout_seconds", 120)),
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise GatewayInvocationError("CLI runtime timed out", status_code=504) from error
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()[-1:] or ["unknown CLI error"]
            raise GatewayInvocationError(detail[0][:500])
        content = completed.stdout.strip()
        if not content:
            raise GatewayInvocationError("CLI runtime returned an empty response")
        return GatewayResult(
            content=content,
            usage=_estimate_usage(prompt, content),
            gateway="local-cli",
        )

    def check(self, route: dict[str, Any]) -> RuntimeHealth:
        from datetime import UTC, datetime

        command = str(dict(route["runtime_config"]).get("command", ""))
        executable = shutil.which(command)
        if executable is None:
            status = HealthStatus.UNAVAILABLE
            message = f"Command not found: {command}"
        else:
            completed = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, timeout=10, check=False
            )
            status = (
                HealthStatus.AVAILABLE
                if completed.returncode == 0
                else HealthStatus.UNAVAILABLE
            )
            message = (completed.stdout or completed.stderr).strip().splitlines()[0][:500]
        return RuntimeHealth(
            runtime_id=route["runtime_id"],
            status=status,
            message=message,
            checked_at=datetime.now(UTC),
        )


class OpenAICompatibleGatewayAdapter:
    def __init__(self, implementation: GatewayKind, client: httpx.Client | None = None) -> None:
        self._implementation = implementation
        self._client = client or httpx.Client()

    def _base_url(self, route: dict[str, Any]) -> str:
        value = route.get("gateway_base_url") or route.get("provider_endpoint_url")
        if not value:
            raise GatewayInvocationError("No gateway or provider endpoint is configured")
        return str(value).rstrip("/")

    def _auth_headers(self, route: dict[str, Any]) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        credential = route.get("gateway_credential") or route.get("provider_credential")
        auth_type = route.get("gateway_auth_type") or route.get("provider_auth_type")
        if credential and auth_type == "bearer":
            headers["authorization"] = f"Bearer {credential}"
        elif credential and auth_type == "api_key":
            config = dict(route.get("gateway_config") or {})
            headers[str(config.get("header_name", "api-key"))] = str(credential)
        return headers

    def _headers(
        self, request: ModelInvocationRequest | ImageInvocationRequest, route: dict[str, Any]
    ) -> dict[str, str]:
        metadata = request.metadata
        return {
            **self._auth_headers(route),
            "x-request-id": str(route.get("request_id", "")),
            "x-org-id": metadata.organization_id,
            "x-org-name": metadata.organization,
            "x-department-id": metadata.department_id,
            "x-department-name": metadata.department,
            "x-project-id": metadata.project_id,
            "x-project-name": metadata.project,
            "x-agent-id": metadata.agent_id,
            "x-agent-name": metadata.agent,
            "x-model-id": str(route["model_id"]),
            "x-runtime-id": str(route["runtime_id"]),
            "x-user-id": metadata.user_id,
            "x-user-name": metadata.user,
            "x-request-source": metadata.request_source,
            "x-hive-organization": metadata.organization,
            "x-hive-department": metadata.department,
            "x-hive-project": metadata.project,
            "x-hive-agent": metadata.agent,
            "x-hive-user": metadata.user,
            "x-hive-workflow": metadata.workflow,
            "x-hive-run-id": metadata.run_id,
            "x-hive-turn-index": str(metadata.turn_index),
            "x-hive-model": str(route["model_key"]),
            "x-hive-runtime": str(route["runtime_name"]),
        }

    def invoke(self, request: ModelInvocationRequest, route: dict[str, Any]) -> GatewayResult:
        runtime_config = dict(route["runtime_config"])
        upstream_model = str(
            route["model_key"]
            if self._implementation is GatewayKind.APIM
            and runtime_config.get("control_plane_managed")
            else route.get("upstream_model_id") or route["model_key"]
        )
        path = str(runtime_config.get("path", "/v1/chat/completions")).replace(
            "{model}", upstream_model
        )
        body: dict[str, Any] = {
            "model": upstream_model,
            # exclude_none matters: OpenAI rejects an explicit "tool_calls": null, and
            # every non-tool turn would carry one otherwise.
            "messages": [
                message.model_dump(exclude_none=True) for message in request.messages
            ],
            "stream": request.stream,
        }
        if request.stream:
            if request.tools:
                raise GatewayInvocationError(
                    "Streaming tool calls are not supported by this invocation endpoint",
                    status_code=400,
                )
            body["stream_options"] = {"include_usage": True}
        if request.tools:
            body["tools"] = [tool.model_dump() for tool in request.tools]
        supports_temperature = runtime_config.get("supports_temperature")
        if supports_temperature is None:
            supports_temperature = not (
                runtime_config.get("control_plane_managed")
                and runtime_config.get("api_format") == "openai_chat"
            )
        if request.temperature is not None and supports_temperature:
            body["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            max_tokens_field = str(runtime_config.get("max_tokens_field", "max_tokens"))
            body[max_tokens_field] = request.max_output_tokens
        if request.response_format is not None:
            body["response_format"] = {"type": request.response_format}
        try:
            if request.stream:
                with self._client.stream(
                    "POST",
                    f"{self._base_url(route)}{path}",
                    headers=self._headers(request, route),
                    json=body,
                    timeout=float(runtime_config.get("timeout_seconds", 120)),
                ) as response:
                    if response.is_error:
                        response.read()
                    response.raise_for_status()
                    response_headers = dict(response.headers)
                    events = _sse_json_events(response)
                content = "".join(
                    str(delta.get("content") or "")
                    for event in events
                    for choice in event.get("choices", [])
                    if isinstance(choice, dict)
                    and isinstance((delta := choice.get("delta")), dict)
                )
                raw_usage = next(
                    (
                        event["usage"]
                        for event in reversed(events)
                        if isinstance(event.get("usage"), dict)
                    ),
                    None,
                )
                if not content:
                    raise GatewayInvocationError("Gateway stream returned no text content")
                usage = _openai_usage(raw_usage) if raw_usage is not None else None
                if usage is None:
                    prompt = _prompt(
                        [message.model_dump() for message in request.messages]
                    )
                    usage = _estimate_usage(prompt, content)
                correlation_id = (
                    response_headers.get("x-correlation-id")
                    or response_headers.get("x-ms-request-id")
                    or response_headers.get("apim-request-id")
                    or next(
                        (str(event["id"]) for event in events if event.get("id")),
                        None,
                    )
                )
                return GatewayResult(
                    content=content,
                    usage=usage,
                    gateway=self._implementation.value,
                    correlation_id=correlation_id,
                )
            response = self._client.post(
                f"{self._base_url(route)}{path}",
                headers=self._headers(request, route),
                json=body,
                timeout=float(runtime_config.get("timeout_seconds", 120)),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            response = error.response
            propagated_headers = {
                name: response.headers[name]
                for name in (
                    "x-request-id",
                    "x-correlation-id",
                    "retry-after",
                    "x-ratelimit-remaining-tokens",
                    "x-quota-remaining-tokens",
                )
                if name in response.headers
            }
            raise GatewayInvocationError(
                f"Gateway request failed ({response.status_code}): "
                f"{_http_error_detail(response)}",
                status_code=response.status_code,
                headers=propagated_headers,
            ) from error
        except httpx.TimeoutException as error:
            raise GatewayInvocationError(
                "Gateway request timed out", status_code=504
            ) from error
        except httpx.HTTPError as error:
            raise GatewayInvocationError(f"Gateway request failed: {error}") from error
        payload = response.json()
        try:
            message = payload["choices"][0]["message"]
            # A turn that only calls tools has content null, which is not a schema error.
            content = str(message.get("content") or "")
            tool_calls = tuple(
                ToolCall.model_validate(call) for call in (message.get("tool_calls") or [])
            )
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise GatewayInvocationError(
                "Gateway returned an unsupported response schema"
            ) from error
        if not content and not tool_calls:
            raise GatewayInvocationError("Gateway returned neither content nor a tool call")
        raw_usage = payload.get("usage")
        usage = None
        if isinstance(raw_usage, dict):
            usage = _openai_usage(raw_usage)
        if usage is None:
            prompt = _prompt([message.model_dump() for message in request.messages])
            usage = _estimate_usage(prompt, content)
        correlation_id = (
            response.headers.get("x-correlation-id")
            or response.headers.get("x-ms-request-id")
            or response.headers.get("apim-request-id")
        )
        return GatewayResult(
            content=content,
            usage=usage,
            gateway=self._implementation.value,
            correlation_id=correlation_id,
            tool_calls=tool_calls,
        )

    def check(self, route: dict[str, Any]) -> RuntimeHealth:
        from datetime import UTC, datetime

        try:
            runtime_config = dict(route["runtime_config"])
            health_url = f"{self._base_url(route)}{runtime_config.get('health_path', '/health')}"
            response = self._client.get(
                health_url,
                headers=self._auth_headers(route),
                timeout=10,
            )
            available = response.is_success
            message = f"HTTP {response.status_code} from {self._implementation.value}"
        except httpx.HTTPError as error:
            available = False
            message = str(error)[:500]
        return RuntimeHealth(
            runtime_id=route["runtime_id"],
            status=HealthStatus.AVAILABLE if available else HealthStatus.UNAVAILABLE,
            message=message,
            checked_at=datetime.now(UTC),
        )


class ApimGatewayAdapter(OpenAICompatibleGatewayAdapter):
    def __init__(self, client: httpx.Client | None = None) -> None:
        super().__init__(GatewayKind.APIM, client)


class AnthropicMessagesGatewayAdapter(OpenAICompatibleGatewayAdapter):
    def invoke(self, request: ModelInvocationRequest, route: dict[str, Any]) -> GatewayResult:
        runtime_config = dict(route["runtime_config"])
        path = str(runtime_config.get("path", "/v1/messages")).replace(
            "{model}", str(route["model_key"])
        )
        if request.tools:
            # Anthropic's tool schema is a different shape from OpenAI's, and the
            # assistant only ever routes to an OpenAI-compatible model. Failing loudly
            # beats silently dropping the tools and letting the model invent an answer.
            raise GatewayInvocationError(
                "Tool calling is not supported on the Anthropic Messages route",
                status_code=400,
            )
        system_messages = [
            message.content
            for message in request.messages
            if message.role == "system" and message.content
        ]
        body: dict[str, Any] = {
            "model": route["model_key"],
            "messages": [
                message.model_dump(exclude_none=True)
                for message in request.messages
                if message.role != "system"
            ],
            "max_tokens": request.max_output_tokens
            or int(runtime_config.get("default_max_tokens", 512)),
            "stream": False,
        }
        if system_messages:
            body["system"] = "\n\n".join(system_messages)
        if request.temperature is not None and runtime_config.get(
            "supports_temperature", False
        ):
            body["temperature"] = request.temperature

        headers = self._headers(request, route)
        headers["anthropic-version"] = str(
            runtime_config.get("anthropic_version", "2023-06-01")
        )
        try:
            response = self._client.post(
                f"{self._base_url(route)}{path}",
                headers=headers,
                json=body,
                timeout=float(runtime_config.get("timeout_seconds", 120)),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            response = error.response
            propagated_headers = {
                name: response.headers[name]
                for name in (
                    "x-request-id",
                    "x-correlation-id",
                    "request-id",
                    "retry-after",
                    "x-ratelimit-remaining-tokens",
                    "x-quota-remaining-tokens",
                )
                if name in response.headers
            }
            raise GatewayInvocationError(
                f"Gateway request failed ({response.status_code}): "
                f"{_http_error_detail(response)}",
                status_code=response.status_code,
                headers=propagated_headers,
            ) from error
        except httpx.TimeoutException as error:
            raise GatewayInvocationError(
                "Gateway request timed out", status_code=504
            ) from error
        except httpx.HTTPError as error:
            raise GatewayInvocationError(f"Gateway request failed: {error}") from error

        payload = response.json()
        raw_content = payload.get("content")
        if not isinstance(raw_content, list):
            raise GatewayInvocationError("Gateway returned an unsupported response schema")
        text_blocks = [
            str(block["text"])
            for block in raw_content
            if isinstance(block, dict) and block.get("type") == "text" and "text" in block
        ]
        if not text_blocks:
            raise GatewayInvocationError("Gateway returned no text content")

        raw_usage = payload.get("usage")
        usage = None
        if isinstance(raw_usage, dict):
            usage = InvocationUsage(
                input_tokens=int(raw_usage.get("input_tokens", 0)),
                cached_tokens=int(raw_usage.get("cache_creation_input_tokens", 0))
                + int(raw_usage.get("cache_read_input_tokens", 0)),
                output_tokens=int(raw_usage.get("output_tokens", 0)),
                estimated=False,
            )
        correlation_id = (
            response.headers.get("x-correlation-id")
            or response.headers.get("request-id")
            or response.headers.get("x-ms-request-id")
            or response.headers.get("apim-request-id")
        )
        return GatewayResult(
            content="\n".join(text_blocks),
            usage=usage,
            gateway=self._implementation.value,
            correlation_id=correlation_id,
        )


class LiteLlmGatewayAdapter(OpenAICompatibleGatewayAdapter):
    def __init__(self, client: httpx.Client | None = None) -> None:
        super().__init__(GatewayKind.LITELLM, client)


class DirectGatewayAdapter(OpenAICompatibleGatewayAdapter):
    def __init__(self, client: httpx.Client | None = None) -> None:
        super().__init__(GatewayKind.DIRECT, client)


class GatewayRouter:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._http_client = http_client

    def generate_image(
        self, request: ImageInvocationRequest, route: dict[str, Any]
    ) -> ImageGatewayResult:
        from .image_generation import OpenAIImageGatewayAdapter

        return OpenAIImageGatewayAdapter(self._http_client).generate(request, route)

    def adapter(self, route: dict[str, Any]) -> GatewayAdapter:
        runtime_kind = RuntimeKind(route["runtime_kind"])
        if runtime_kind is RuntimeKind.COPILOT_CLI:
            return CliGatewayAdapter()
        implementation = GatewayKind(route.get("gateway_implementation") or "direct")
        runtime_config = dict(route.get("runtime_config") or {})
        if runtime_config.get("api_format") == "anthropic_messages":
            return AnthropicMessagesGatewayAdapter(implementation, self._http_client)
        if implementation is GatewayKind.APIM:
            return ApimGatewayAdapter(self._http_client)
        if implementation is GatewayKind.LITELLM:
            return LiteLlmGatewayAdapter(self._http_client)
        return DirectGatewayAdapter(self._http_client)


def supports_tool_calling(runtime_kind: str, runtime_config: Mapping[str, Any] | None) -> bool:
    """Whether `adapter()` would return an adapter that can carry OpenAI tool calls.

    This mirrors the branch order in `GatewayRouter.adapter` and lives beside it so the
    two cannot drift silently: adding an adapter forces a decision here in the same edit.
    A CLI runtime has no tool protocol at all, and the Anthropic route uses a different
    tool schema that this project does not translate.
    """
    if RuntimeKind(runtime_kind) is RuntimeKind.COPILOT_CLI:
        return False
    return dict(runtime_config or {}).get("api_format") != "anthropic_messages"


def elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
