from pydantic import SecretStr

from backend.services.gateway_credentials import with_platform_gateway_credential
from turnstile_core.config import Settings


def test_platform_credential_fallback_does_not_cross_gateway_targets() -> None:
    settings = Settings(
        apim_gateway_url="https://platform.example/turnstile/llm/",
        apim_dashboard_subscription_key=SecretStr("platform-secret"),
    )
    platform = {
        "gateway_implementation": "apim",
        "gateway_base_url": "https://platform.example/turnstile/llm",
    }
    assert with_platform_gateway_credential(platform, settings)["gateway_credential"] == (
        "platform-secret"
    )
    other = {**platform, "gateway_base_url": "https://other.example/turnstile/llm"}
    assert "gateway_credential" not in with_platform_gateway_credential(other, settings)
    assert "gateway_credential" not in platform


def test_explicit_gateway_credentials_and_non_apim_routes_are_preserved() -> None:
    settings = Settings(apim_dashboard_subscription_key=SecretStr("platform-secret"))
    explicit = {"gateway_implementation": "apim", "gateway_credential": "target-secret"}
    assert with_platform_gateway_credential(explicit, settings) == explicit
    direct = {"gateway_implementation": "direct"}
    assert with_platform_gateway_credential(direct, settings) == direct
