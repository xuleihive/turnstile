"""Trusted backend delegation shared by structured and native invocation paths."""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from ..config import Settings
from ..integrations.reconciliation import ManagedIdentityTokenProvider
from .budget_settlement import BudgetScopeRepository


class DelegationTokenProvider(Protocol):
    def token(self, resource: str) -> str: ...


class BudgetDelegationError(ValueError):
    def __init__(self, detail: str, *, status_code: int = 503) -> None:
        super().__init__(detail)
        self.status_code = status_code


@lru_cache(maxsize=1)
def delegation_token_provider() -> ManagedIdentityTokenProvider:
    return ManagedIdentityTokenProvider()


def prepare_budget_delegation(
    repository: object,
    settings: Settings,
    *,
    gateway_id: UUID,
    gateway_implementation: str,
    gateway_url: str,
    user_id: str,
    token_provider: DelegationTokenProvider | None = None,
) -> str | None:
    if (not settings.budget_execution_enabled
        or gateway_id not in settings.budget_execution_target_ids):
        return None
    if not isinstance(repository, BudgetScopeRepository):
        raise BudgetDelegationError("Budget delegation requires a durable rollout")
    try:
        rollout = repository.active_budget_execution_rollout()
    except Exception:
        raise BudgetDelegationError("Budget rollout state is unavailable") from None
    if rollout is None or gateway_id not in rollout["gateway_profile_ids"]:
        return None
    configuration = settings.budget_delegation_targets.get(gateway_id)
    if configuration is None or gateway_implementation != "apim":
        raise BudgetDelegationError("Trusted budget delegation is not configured")
    destination = urlsplit(gateway_url)
    origins = {urlsplit(str(value)).netloc.lower() for value in configuration.allowed_origins}
    if (
        destination.scheme != "https" or destination.netloc.lower() not in origins
        or destination.username or destination.password or destination.query or destination.fragment
    ):
        raise BudgetDelegationError("Budget delegation target does not match its trusted origin")
    if (
        not user_id.strip() or user_id == "unattributed" or len(user_id) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in user_id)
    ):
        raise BudgetDelegationError("Budget delegation requires a verified person", status_code=403)
    try:
        return (token_provider or delegation_token_provider()).token(configuration.audience)
    except Exception:
        raise BudgetDelegationError("Budget delegation identity is unavailable") from None
