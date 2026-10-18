from __future__ import annotations

from typing import Any
from unittest.mock import Mock
from urllib.parse import unquote
from uuid import uuid4

import pytest

from turnstile_core.config import Settings
from turnstile_core.domain.budget_delegation import BudgetDelegationTarget
from turnstile_core.integrations.budget_delegation_headers import encode_delegation_metadata
from turnstile_core.services.budget_delegation import (
    BudgetDelegationError,
    prepare_budget_delegation,
)
from turnstile_core.services.budget_settlement import BudgetScopeRepository


def delegation() -> tuple[Mock, Settings, dict[str, Any], Mock]:
    target = uuid4()
    repository = Mock(spec=BudgetScopeRepository)
    repository.active_budget_execution_rollout.return_value = {"gateway_profile_ids": [target]}
    settings = Settings(
        budget_execution_enabled=True,
        budget_execution_target_ids=[target],
        budget_delegation_targets={target: BudgetDelegationTarget.model_validate({
            "audience": "api://trusted-backend", "allowed_origins": ["https://gateway.example.com"],
        })},
    )
    arguments: dict[str, Any] = {
        "gateway_id": target, "gateway_implementation": "apim",
        "gateway_url": "https://gateway.example.com/turnstile/llm", "user_id": "user@example.com",
    }
    provider = Mock()
    provider.token.return_value = "server-identity-token"
    return repository, settings, arguments, provider


def test_backend_delegation_uses_explicit_audience_and_verified_target() -> None:
    repository, settings, arguments, provider = delegation()
    assert prepare_budget_delegation(
        repository, settings, token_provider=provider, **arguments,
    ) == "server-identity-token"
    provider.token.assert_called_once_with("api://trusted-backend")


@pytest.mark.parametrize("url", [
    "http://gateway.example.com/llm", "https://other.example.com/llm",
    "https://gateway.example.com@evil.example.com/llm",
    "https://gateway.example.com/llm?target=other", "https://gateway.example.com/llm#other",
])
def test_backend_delegation_never_obtains_credentials_for_an_untrusted_target(url: str) -> None:
    repository, settings, arguments, provider = delegation()
    arguments["gateway_url"] = url
    with pytest.raises(BudgetDelegationError, match="trusted origin"):
        prepare_budget_delegation(repository, settings, token_provider=provider, **arguments)
    provider.token.assert_not_called()


@pytest.mark.parametrize("user", ["", " ", "unattributed", "user\r\nforged", "x" * 256])
def test_backend_delegation_rejects_unusable_person_identity(user: str) -> None:
    repository, settings, arguments, provider = delegation()
    arguments["user_id"] = user
    with pytest.raises(BudgetDelegationError) as error:
        prepare_budget_delegation(repository, settings, token_provider=provider, **arguments)
    assert error.value.status_code == 403
    provider.token.assert_not_called()


def test_disabled_or_unprepared_delegation_retains_legacy_path_without_token_fetch() -> None:
    repository, settings, arguments, provider = delegation()
    repository.active_budget_execution_rollout.return_value = None
    assert prepare_budget_delegation(
        repository, settings, token_provider=provider, **arguments,
    ) is None
    provider.token.assert_not_called()


def test_identity_endpoint_failure_does_not_expose_token_or_upstream_error() -> None:
    repository, settings, arguments, provider = delegation()
    provider.token.side_effect = RuntimeError("private identity endpoint material")
    with pytest.raises(BudgetDelegationError, match="identity is unavailable") as error:
        prepare_budget_delegation(repository, settings, token_provider=provider, **arguments)
    assert "private" not in str(error.value)


def test_unicode_delegated_metadata_is_encoded_without_altering_credentials() -> None:
    headers = {
        "x-user-id": "user@example.com", "x-user-name": "研发用户",
        "x-department-name": "平台 / 预算 100%", "Ocp-Apim-Subscription-Key": "key",
        "x-turnstile-delegation-token": "Bearer server-token",
    }
    encoded = encode_delegation_metadata(headers)
    assert all(value.isascii() for value in encoded.values())
    assert encoded["x-turnstile-metadata-encoding"] == "percent-v1"
    assert encoded["x-turnstile-delegation-token"] == headers["x-turnstile-delegation-token"]
    assert encoded["Ocp-Apim-Subscription-Key"] == "key"
    for name in ("x-user-id", "x-user-name", "x-department-name"):
        assert unquote(encoded[name]) == headers[name]
    assert "x-turnstile-metadata-encoding" not in headers


def test_delegated_metadata_does_not_accept_header_injection() -> None:
    with pytest.raises(ValueError, match="invalid header"):
        encode_delegation_metadata({"x-user-name": "name\r\nAuthorization: value"})
