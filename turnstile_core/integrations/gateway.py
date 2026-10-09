from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from ..domain.images import ImageInvocationRequest
from ..domain.runtime_models import (
    GatewayKind,
    HealthStatus,
    InvocationApiFormat,
    InvocationUsage,
    ModelInvocationRequest,
    RuntimeHealth,
    RuntimeKind,
    ToolCall,
    ToolFunctionCall,
)
from .gateway_protocol import model_protocol_route

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
    responses_output: list[dict[str, Any]] | None = field(default=None, repr=False)
    anthropic_content: list[dict[str, Any]] | None = field(default=None, repr=False)


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


def _responses_input(request: ModelInvocationRequest) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in request.messages:
        if message._responses_output is not None:
            items.extend(message._responses_output)
            continue
        if message.role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": message.content,
            })
            continue
        if message.content:
            block: dict[str, Any] = {
                "type": "output_text" if message.role == "assistant" else "input_text",
                "text": message.content,
            }
            if message.role == "assistant":
                block["annotations"] = []
            items.append({
                "type": "message",
                "role": message.role,
                "content": [block],
            })
        for call in message.tool_calls or []:
            items.append({
                "type": "function_call",
                "call_id": call.id,
                "name": call.function.name,
                "arguments": call.function.arguments,
            })
    return items


def _responses_body(request: ModelInvocationRequest, model: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "input": _responses_input(request),
        "store": False,
        "stream": False,
        "include": ["reasoning.encrypted_content"],
    }
    if request.tools:
        # Responses defaults to strict schemas; existing tools have optional arguments
        # and validate locally, so retain Chat Completions' non-strict behavior.
        body["tools"] = [
            {"type": "function", **tool.function.model_dump(), "strict": False}
            for tool in request.tools
        ]
    if request.max_output_tokens is not None:
        body["max_output_tokens"] = request.max_output_tokens
    if request.response_format is not None:
        body["text"] = {"format": {"type": request.response_format}}
    return body


def _responses_message(payload: Any) -> tuple[str, tuple[ToolCall, ...], list[dict[str, Any]]]:
    try:
        if not isinstance(payload, dict) or not isinstance(payload["output"], list):
            raise ValueError("Expected a Responses output array")
        if payload.get("status") in {"failed", "incomplete", "cancelled", "in_progress", "queued"}:
            raise GatewayInvocationError("Gateway returned an unfinished Responses result")
        content: list[str] = []
        calls: list[ToolCall] = []
        output: list[dict[str, Any]] = payload["output"]
        for item in output:
            if item["type"] == "message":
                for block in item["content"]:
                    if block["type"] == "output_text":
                        content.append(block["text"])
                    elif block["type"] == "refusal":
                        content.append(block["refusal"])
            elif item["type"] == "function_call":
                calls.append(ToolCall(
                    id=item["call_id"],
                    function=ToolFunctionCall(name=item["name"], arguments=item["arguments"]),
                ))
        text = "".join(content)
        if not text and not calls:
            raise GatewayInvocationError("Gateway returned neither content nor a tool call")
        return text, tuple(calls), output
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise GatewayInvocationError("Gateway returned an unsupported response schema") from error


def _anthropic_messages(request: ModelInvocationRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    previous_was_tool = False
    try:
        for message in request.messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                result: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
                try:
                    payload = json.loads(message.content or "")
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and set(payload) == {"error"}:
                    result["is_error"] = True
                # All results for parallel calls belong to one immediately following user turn.
                if previous_was_tool:
                    messages[-1]["content"].append(result)
                else:
                    messages.append({"role": "user", "content": [result]})
                previous_was_tool = True
                continue
            previous_was_tool = False
            if message._anthropic_content is not None:
                content: str | list[dict[str, Any]] = message._anthropic_content
            elif message.tool_calls:
                content = []
                if message.content:
                    content.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    arguments = json.loads(call.function.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("Anthropic tool arguments must be an object")
                    content.append({
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.function.name,
                        "input": arguments,
                    })
            else:
                content = message.content or ""
            messages.append({"role": message.role, "content": content})
    except (TypeError, ValueError) as error:
        raise GatewayInvocationError("Invalid Anthropic tool history", status_code=400) from error
    return messages


def _anthropic_message(payload: Any) -> tuple[str, tuple[ToolCall, ...], list[dict[str, Any]]]:
    try:
        if not isinstance(payload, dict) or not isinstance(payload["content"], list):
            raise ValueError("Expected Anthropic content blocks")
        if payload.get("stop_reason") in {"max_tokens", "pause_turn"}:
            raise GatewayInvocationError("Gateway returned an unfinished Anthropic result")
        output: list[dict[str, Any]] = payload["content"]
        text: list[str] = []
        calls: list[ToolCall] = []
        for block in output:
            if block["type"] == "text":
                text.append(block["text"])
            elif block["type"] == "tool_use":
                if not isinstance(block["input"], dict):
                    raise ValueError("Anthropic tool arguments must be an object")
                calls.append(ToolCall(
                    id=block["id"],
                    function=ToolFunctionCall(
                        name=block["name"],
                        arguments=json.dumps(block["input"], ensure_ascii=False),
                    ),
                ))
        content = "\n".join(text)
        if not content and not calls:
            raise GatewayInvocationError("Gateway returned neither content nor a tool call")
        return content, tuple(calls), output
    except (KeyError, TypeError, ValueError) as error:
        raise GatewayInvocationError("Gateway returned an unsupported response schema") from error


def _iter_sse_json_events(response: httpx.Response) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data != "[DONE]":
                    payload = json.loads(data)
                    if isinstance(payload, dict):
                        yield payload
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        data = "\n".join(data_lines)
        if data != "[DONE]":
            payload = json.loads(data)
            if isinstance(payload, dict):
                yield payload


def _sse_json_events(response: httpx.Response) -> list[dict[str, Any]]:
    return list(_iter_sse_json_events(response))


def _anthropic_error_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: response.headers[name]
        for name in (
            "x-request-id", "x-correlation-id", "request-id", "retry-after",
            "x-ratelimit-remaining-tokens", "x-quota-remaining-tokens",
        )
        if name in response.headers
    }


def _anthropic_usage(raw: Any) -> InvocationUsage | None:
    if not isinstance(raw, Mapping) or not {"input_tokens", "output_tokens"} <= raw.keys():
        return None
    if raw["input_tokens"] is None or raw["output_tokens"] is None:
        return None
    values = {}
    for name in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    ):
        value = raw.get(name, 0)
        if value is None:
            value = 0
        if type(value) is not int or value < 0:
            raise ValueError("Anthropic token usage must be a nonnegative integer")
        values[name] = value
    return InvocationUsage(
        input_tokens=values["input_tokens"],
        cached_tokens=values["cache_creation_input_tokens"] + values["cache_read_input_tokens"],
        cache_write_tokens=values["cache_creation_input_tokens"],
        output_tokens=values["output_tokens"],
        estimated=False,
    )


def _anthropic_stream_message(response: httpx.Response) -> dict[str, Any]:
    message: dict[str, Any] | None = None
    blocks: list[dict[str, Any]] = []
    active: set[int] = set()
    arguments: dict[int, list[str]] = {}
    usage: dict[str, Any] = {}
    stopped = False

    def failure(detail: str, status_code: int = 502) -> GatewayInvocationError:
        try:
            partial_usage = _anthropic_usage(usage)
        except ValueError:
            partial_usage = None
        return GatewayInvocationError(
            detail, status_code=status_code,
            headers=_anthropic_error_headers(response), usage=partial_usage,
        )

    try:
        for event in _iter_sse_json_events(response):
            kind = event.get("type")
            if kind == "error":
                error = event["error"]
                if not isinstance(error, dict):
                    raise ValueError("Expected a stream error object")
                status = {
                    "invalid_request_error": 400, "authentication_error": 401,
                    "permission_error": 403, "not_found_error": 404,
                    "rate_limit_error": 429, "api_error": 500, "overloaded_error": 529,
                }.get(str(error.get("type") or ""), 502)
                detail = _http_error_detail(httpx.Response(status, json=event))
                raise failure(f"Gateway stream failed: {detail}", status)
            if kind == "message_start":
                start = event["message"]
                if (
                    message is not None or not isinstance(start, dict)
                    or start.get("type") != "message" or start.get("role") != "assistant"
                    or start.get("content") != []
                ):
                    raise ValueError("Invalid message start")
                message = dict(start)
                if isinstance(start.get("usage"), dict):
                    usage.update(start["usage"])
                continue
            if kind not in {
                "content_block_start", "content_block_delta", "content_block_stop",
                "message_delta", "message_stop",
            }:
                # Ping and future event types do not affect the completed message.
                continue
            if message is None:
                raise ValueError("Stream event preceded message start")
            if kind.startswith("content_block_"):
                index = event["index"]
                if type(index) is not int or index < 0:
                    raise ValueError("Invalid content block index")
                if kind == "content_block_start":
                    block = event["content_block"]
                    if (
                        index != len(blocks) or not isinstance(block, dict)
                        or not isinstance(block.get("type"), str)
                    ):
                        raise ValueError("Invalid content block start")
                    blocks.append(dict(block))
                    active.add(index)
                    continue
                if index not in active:
                    raise ValueError("Content block is not active")
                block = blocks[index]
                if kind == "content_block_stop":
                    encoded = "".join(arguments.pop(index, []))
                    if encoded:
                        decoded = json.loads(encoded)
                        if not isinstance(decoded, dict):
                            raise ValueError("Tool arguments must be an object")
                        block["input"] = decoded
                    active.remove(index)
                    continue
                delta = event["delta"]
                if not isinstance(delta, dict):
                    raise ValueError("Invalid content delta")
                delta_kind = delta.get("type")
                fields = {
                    "text_delta": ("text", "text"),
                    "thinking_delta": ("thinking", "thinking"),
                    "signature_delta": ("thinking", "signature"),
                }
                if delta_kind in fields:
                    block_kind, field_name = fields[delta_kind]
                    fragment = delta[field_name]
                    if block["type"] != block_kind or not isinstance(fragment, str):
                        raise ValueError("Content delta does not match its block")
                    block[field_name] = block.get(field_name, "") + fragment
                elif delta_kind == "input_json_delta":
                    fragment = delta["partial_json"]
                    if block["type"] not in {"tool_use", "server_tool_use"} or not isinstance(
                        fragment, str,
                    ):
                        raise ValueError("Invalid tool argument delta")
                    arguments.setdefault(index, []).append(fragment)
                elif delta_kind == "citations_delta":
                    citation = delta["citation"]
                    if block["type"] != "text" or not isinstance(citation, dict):
                        raise ValueError("Invalid citation delta")
                    block.setdefault("citations", []).append(citation)
            elif kind == "message_delta":
                delta = event["delta"]
                if active or not isinstance(delta, dict):
                    raise ValueError("Invalid message delta")
                for name in ("stop_reason", "stop_sequence"):
                    if name in delta:
                        if delta[name] is not None and not isinstance(delta[name], str):
                            raise ValueError("Invalid message stop reason")
                        message[name] = delta[name]
                if isinstance(event.get("usage"), dict):
                    # These counters are cumulative, not per-event token increments.
                    usage.update(event["usage"])
            elif kind == "message_stop":
                if active or not message.get("stop_reason"):
                    raise ValueError("Message stopped before its content completed")
                stopped = True
                break
    except httpx.TimeoutException as error:
        raise failure("Gateway stream timed out", 504) from error
    except httpx.HTTPError as error:
        raise failure("Gateway stream connection failed") from error
    except (KeyError, TypeError, ValueError) as error:
        raise failure("Gateway returned an unsupported Anthropic stream schema") from error
    if not stopped or message is None:
        raise failure("Gateway returned an unfinished Anthropic stream")
    message["content"] = blocks
    message["usage"] = usage
    return message


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
        has_tool_context = bool(request.tools) or any(
            message.tool_calls or message.role == "tool" or message._responses_output is not None
            for message in request.messages
        )
        try:
            route = model_protocol_route(route, request.api_format, tools=has_tool_context)
        except ValueError as error:
            raise GatewayInvocationError(str(error), status_code=400) from error
        runtime_config = dict(route["runtime_config"])
        use_responses = runtime_config.get("api_format") == InvocationApiFormat.OPENAI_RESPONSES
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
        if use_responses:
            if request.stream:
                raise GatewayInvocationError(
                    "Streaming tool calls are not supported by this invocation endpoint",
                    status_code=400,
                )
            body = _responses_body(request, upstream_model)
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
        responses_output = None
        try:
            if use_responses:
                content, tool_calls, responses_output = _responses_message(payload)
            else:
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
            usage = _openai_usage({
                "prompt_tokens": raw_usage.get("input_tokens"),
                "completion_tokens": raw_usage.get("output_tokens"),
                "prompt_tokens_details": raw_usage.get("input_tokens_details"),
            }) if use_responses else _openai_usage(raw_usage)
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
            responses_output=responses_output,
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
            available = response.status_code < 500
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
        try:
            route = model_protocol_route(
                route, request.api_format,
                tools=bool(request.tools) or any(
                    message.tool_calls or message.role == "tool" for message in request.messages
                ),
            )
        except ValueError as error:
            raise GatewayInvocationError(str(error), status_code=400) from error
        runtime_config = dict(route["runtime_config"])
        path = str(runtime_config.get("path", "/v1/messages")).replace(
            "{model}", str(route["model_key"])
        )
        system_messages = [
            message.content
            for message in request.messages
            if message.role == "system" and message.content
        ]
        body: dict[str, Any] = {
            "model": (
                route["model_key"]
                if self._implementation is GatewayKind.APIM
                else route.get("upstream_model_id") or route["model_key"]
            ),
            "messages": _anthropic_messages(request),
            "max_tokens": request.max_output_tokens
            or int(runtime_config.get("default_max_tokens", 512)),
            "stream": request.stream,
        }
        if system_messages:
            body["system"] = "\n\n".join(system_messages)
        if request.tools:
            body["tools"] = [
                {
                    "name": tool.function.name,
                    "description": tool.function.description,
                    "input_schema": tool.function.parameters,
                }
                for tool in request.tools
            ]
        if request.temperature is not None and runtime_config.get(
            "supports_temperature", False
        ):
            body["temperature"] = request.temperature

        headers = self._headers(request, route)
        headers["anthropic-version"] = str(
            runtime_config.get("anthropic_version", "2023-06-01")
        )
        try:
            if request.stream:
                with self._client.stream(
                    "POST", f"{self._base_url(route)}{path}", headers=headers, json=body,
                    timeout=float(runtime_config.get("timeout_seconds", 120)),
                ) as response:
                    if response.is_error:
                        response.read()
                    response.raise_for_status()
                    payload = _anthropic_stream_message(response)
            else:
                response = self._client.post(
                    f"{self._base_url(route)}{path}",
                    headers=headers,
                    json=body,
                    timeout=float(runtime_config.get("timeout_seconds", 120)),
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            response = error.response
            raise GatewayInvocationError(
                f"Gateway request failed ({response.status_code}): "
                f"{_http_error_detail(response)}",
                status_code=response.status_code,
                headers=_anthropic_error_headers(response),
            ) from error
        except httpx.TimeoutException as error:
            raise GatewayInvocationError(
                "Gateway request timed out", status_code=504
            ) from error
        except httpx.HTTPError as error:
            raise GatewayInvocationError(f"Gateway request failed: {error}") from error
        except ValueError as error:
            raise GatewayInvocationError(
                "Gateway returned an unsupported response schema",
            ) from error

        try:
            usage = _anthropic_usage(payload.get("usage") if isinstance(payload, dict) else None)
        except (TypeError, ValueError) as error:
            raise GatewayInvocationError("Gateway returned an unsupported usage schema") from error
        try:
            content, tool_calls, raw_content = _anthropic_message(payload)
        except GatewayInvocationError as error:
            error.usage = usage
            error.headers = _anthropic_error_headers(response)
            raise
        correlation_id = (
            response.headers.get("x-correlation-id")
            or response.headers.get("request-id")
            or response.headers.get("x-ms-request-id")
            or response.headers.get("apim-request-id")
            or payload.get("id")
        )
        return GatewayResult(
            content=content,
            usage=usage,
            gateway=self._implementation.value,
            correlation_id=correlation_id,
            tool_calls=tool_calls,
            anthropic_content=raw_content,
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
    """CLI runtimes have no tool protocol; both HTTP adapters carry function calls."""
    return RuntimeKind(runtime_kind) is not RuntimeKind.COPILOT_CLI


def elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
