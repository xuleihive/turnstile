from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest

from backend.api import app
from backend.http.client_user import client_user_service
from backend.services.client_user import ClientUserService
from tests.backend.model_platform.test_client_user_service import client_service
from tests.platform.api.api_support import client
from turnstile_core.persistence.in_memory import InMemoryRepository

pytest_plugins = ["tests.platform.api.api_fixtures"]


@pytest.fixture
def download_service(monkeypatch: pytest.MonkeyPatch) -> Iterator[ClientUserService]:
    service, repo, _ = client_service()
    repo.token_budgets.clear()
    monkeypatch.setenv("CLIENT_USER_ENABLED", "true")
    monkeypatch.setenv("CLIENT_PLATFORM_ORIGIN", "https://platform.example.com")
    app.dependency_overrides[client_user_service] = lambda: service
    yield service
    app.dependency_overrides.pop(client_user_service, None)


def test_no_personal_budget_hides_options_and_blocks_direct_download(
    download_service: ClientUserService,
) -> None:
    issue = Mock()
    download_service.store.issue_download = issue  # type: ignore[method-assign]
    selected = Mock(side_effect=AssertionError("No subscription lookup without a budget"))
    download_service.default_subscription = selected  # type: ignore[method-assign]
    options = client.get("/api/v1/client-downloads/user-options")
    assert options.status_code == 200
    assert options.headers["cache-control"] == "no-store"
    assert options.json()["enabled"] is False
    assert options.json()["budget_configured"] is False
    assert options.json()["unavailable_reason"] == "暂未开通预算，请联系部门管理员。"
    assert "default_subscription" not in options.json()
    download = client.post(
        "/api/v1/client-downloads/magpie-user", json={"acknowledged": True},
    )
    assert download.status_code == 403
    assert download.json()["detail"] == options.json()["unavailable_reason"]
    issue.assert_not_called()
    selected.assert_not_called()


@pytest.mark.parametrize("budget_mode", ["tokens", "usd"])
def test_configured_personal_budget_allows_options_and_download(
    download_service: ClientUserService, budget_mode: str,
) -> None:
    repo = download_service.repository
    assert isinstance(repo, InMemoryRepository)
    period = datetime.now(UTC).date().replace(day=1)
    repo.upsert_token_budget(
        period, "user", "owner@contoso.com", None,
        0 if budget_mode == "tokens" else None, 80, "fixture",
        budget_mode=budget_mode, usd_limit=Decimal("10") if budget_mode == "usd" else None,
    )
    options = client.get("/api/v1/client-downloads/user-options")
    assert options.status_code == 200
    assert options.json()["enabled"] is True
    assert options.json()["budget_configured"] is True
    assert options.json()["default_subscription"]["subscription_name"]
    download = client.post(
        "/api/v1/client-downloads/magpie-user", json={"acknowledged": True},
    )
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    # Recheck at transfer time; an earlier successful options read is not authorization.
    repo.token_budgets.clear()
    assert client.post(
        "/api/v1/client-downloads/magpie-user", json={"acknowledged": True},
    ).status_code == 403
