"""One protocol decision shared by model selection and actual gateway dispatch."""

from __future__ import annotations

from typing import Any

from ..domain.runtime_models import (
    GatewayKind,
    InvocationApiFormat,
    ManagedModel,
    ModelFamilyKey,
    ProviderKind,
    RegistryResponse,
    RuntimeKind,
)


def registry_model_route(registry: RegistryResponse, model: ManagedModel) -> dict[str, Any]:
    runtime = next(item for item in registry.runtimes if item.id == model.runtime_id)
    provider = next(item for item in registry.providers if item.id == model.provider_id)
    gateway = next(
        (item for item in registry.gateways if item.id == runtime.gateway_profile_id), None
    )
    return {
        "model_key": model.model_key,
        "upstream_model_id": model.upstream_model_id,
        "model_family_key": model.family_key,
        "provider_kind": provider.provider_kind,
        "provider_endpoint_url": provider.endpoint_url,
        "runtime_kind": runtime.runtime_kind,
        "runtime_config": runtime.config,
        "gateway_implementation": gateway.implementation if gateway else GatewayKind.DIRECT,
    }


def _native_config(route: dict[str, Any]) -> dict[str, Any]:
    config = dict(route.get("runtime_config") or {})
    if (
        route.get("provider_kind") == ProviderKind.MICROSOFT_FOUNDRY
        and route.get("model_family_key") == ModelFamilyKey.CLAUDE
    ):
        config.update(
            path="/v1/messages",
            api_format=InvocationApiFormat.ANTHROPIC_MESSAGES.value,
            anthropic_version="2023-06-01",
        )
    return config


def _requires_responses_tools(route: dict[str, Any]) -> bool:
    model = str(route.get("upstream_model_id") or route["model_key"]).casefold()
    return model == "gpt-6" or model.startswith(("gpt-6-", "gpt-6."))


def invocation_api_paths(
    route: dict[str, Any], *, tools: bool = True
) -> dict[InvocationApiFormat, str]:
    if route.get("runtime_kind") == RuntimeKind.COPILOT_CLI:
        return {}
    config = _native_config(route)
    native = config.get("api_format", InvocationApiFormat.OPENAI_CHAT.value)
    if native == InvocationApiFormat.ANTHROPIC_MESSAGES:
        return {InvocationApiFormat.ANTHROPIC_MESSAGES: str(config.get("path", "/v1/messages"))}
    if native == InvocationApiFormat.OPENAI_RESPONSES:
        return {InvocationApiFormat.OPENAI_RESPONSES: str(config.get("path", "/v1/responses"))}
    path = str(config.get("path", "/v1/chat/completions"))
    paths = {}
    if not (tools and _requires_responses_tools(route)):
        paths[InvocationApiFormat.OPENAI_CHAT] = path
    if (
        route.get("gateway_implementation") == GatewayKind.APIM
        and config.get("control_plane_managed")
    ):
        # This is the same binding boundary used by the APIM policy compiler.
        responses_supported = str(config.get("backend_path", "")).rstrip("/") == (
            "/openai/v1/chat/completions"
        )
    else:
        responses_supported = (
            _requires_responses_tools(route)
            or config.get("supports_responses") is True
            or str(config.get("backend_path", "")).rstrip("/") == "/openai/v1/chat/completions"
            or str(route.get("provider_endpoint_url") or "").rstrip("/") in {
                "https://api.openai.com", "https://api.openai.com/v1",
            }
        )
    if responses_supported and path.endswith("/chat/completions"):
        paths[InvocationApiFormat.OPENAI_RESPONSES] = (
            path.removesuffix("/chat/completions") + "/responses"
        )
    return paths


def model_protocol_route(
    route: dict[str, Any],
    requested: InvocationApiFormat | None = None,
    *,
    tools: bool = False,
) -> dict[str, Any]:
    if route.get("runtime_kind") == RuntimeKind.COPILOT_CLI and requested is None:
        return route
    paths = invocation_api_paths(route, tools=tools)
    if not paths:
        raise ValueError(
            "No compatible API route is configured for this model's tool calls; "
            "publish a supported model route in APIM"
        )
    selected = requested or next(iter(paths))
    if selected not in paths:
        raise ValueError(
            f"The selected model route does not support {selected.value}"
            + (" with tool calls" if tools else "")
        )
    return {
        **route,
        "runtime_config": {
            **_native_config(route),
            "path": paths[selected],
            "api_format": selected.value,
        },
    }
