"""The existing platform subscription-key fallback shared by console and client calls."""

from typing import Any

from turnstile_core.config import Settings


def with_platform_gateway_credential(route: dict[str, Any], settings: Settings) -> dict[str, Any]:
    result = dict(route)
    target_url = str(result.get("gateway_base_url") or "").rstrip("/")
    platform_url = (settings.apim_gateway_url or "").rstrip("/")
    if target_url and target_url != platform_url:
        return result
    if (
        result.get("gateway_implementation") == "apim"
        and not result.get("gateway_credential")
        and settings.apim_dashboard_subscription_key
    ):
        result["gateway_credential"] = settings.apim_dashboard_subscription_key.get_secret_value()
        result["gateway_auth_type"] = "api_key"
    return result
