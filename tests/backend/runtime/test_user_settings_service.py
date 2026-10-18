from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from backend.services.auth_service import SessionIdentity
from backend.services.user_settings_service import UserSettingsService, personal_period
from tests.platform.api.api_support import _usage_record
from turnstile_core.config import Settings
from turnstile_core.persistence.in_memory import InMemoryRepository


def reader(email: str = "test.user01@contoso.com", role: str = "member") -> SessionIdentity:
    return SessionIdentity(
        id=str(UUID(int=1)),
        email=email,
        name=None,
        role="owner" if role == "owner" else "member",
        method="password",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_unlinked_member_is_not_given_owner_default_department() -> None:
    service = UserSettingsService(InMemoryRepository(), Settings())
    account = service.account(reader("unknown@example.com"))
    assert account.department is None and account.organization is None
    assert account.membership_source == "unlinked"


def test_explicit_empty_model_policy_denies_everything() -> None:
    repository = InMemoryRepository()
    repository.user_model_policies["test.user01@contoso.com"] = {
        "user_id": "test.user01@contoso.com",
        "model_ids": [],
    }
    models = UserSettingsService(repository, Settings()).models(reader())
    assert models.policy_state == "deny_all" and models.items == []


def test_unconfigured_policy_does_not_allow_dynamic_models() -> None:
    repository = InMemoryRepository()
    for model in repository.models:
        model["assignment_required"] = True
    models = UserSettingsService(repository, Settings()).models(reader())
    assert models.policy_state == "unconfigured" and models.items == []


def test_model_assignment_is_not_replaced_by_enable_or_health_state() -> None:
    repository = InMemoryRepository()
    repository.user_model_policies.clear()
    service = UserSettingsService(repository, Settings())
    compatible = service.models(reader()).items
    assert compatible
    chosen = compatible[0]
    repository.user_model_policies["test.user01@contoso.com"] = {
        "user_id": "test.user01@contoso.com",
        "model_ids": [chosen.id],
    }
    runtime_id = next(row["runtime_id"] for row in repository.models if row["id"] == chosen.id)
    runtime = next(row for row in repository.runtimes if row["id"] == runtime_id)
    runtime["health_status"] = "unavailable"
    assigned = service.models(reader()).items
    assert len(assigned) == 1
    assert assigned[0].id == chosen.id and assigned[0].runtime_status == "unavailable"
    assert assigned[0].access_source == "explicit"


def test_usage_complete_model_totals_cache_split_and_timezone() -> None:
    repository = InMemoryRepository()
    now = datetime.now(UTC).replace(day=1, hour=18)
    repository.usage_records = [
        _usage_record(f"mine-{index}", "Foundry", 2).model_copy(
            update={
                "ts": now,
                "model_id": f"model-{index}",
                "cached_tokens": 5,
                "cache_write_tokens": 2,
                "input_price_per_million": 1,
                "output_price_per_million": 1,
                "cached_price_per_million": 1,
                "cache_write_price_per_million": 1,
            }
        )
        for index in range(105)
    ]
    repository.usage_records.append(
        _usage_record("other", "Foundry", 99).model_copy(
            update={
                "ts": now,
                "user_id": "other@example.com",
            }
        )
    )
    service = UserSettingsService(repository, Settings(data_backend="demo"))
    usage = service.usage(reader(), now.strftime("%Y-%m"), "day", "Asia/Shanghai")
    assert len(usage.models) == 105
    assert usage.totals.requests == 105
    assert usage.totals.total_tokens == 105 * 7
    assert usage.totals.cache_read_tokens == 105 * 3
    assert usage.totals.cache_write_tokens == 105 * 2
    assert usage.totals.cost_state == "priced"
    assert usage.points[0].bucket_start.day == 1
    assert usage.points[0].bucket_start.hour == 16
    assert usage.demo


def test_no_budget_still_has_personal_evidence_usage() -> None:
    repository = InMemoryRepository()
    now = datetime.now(UTC)
    repository.token_budgets.clear()
    repository.usage_records = [
        _usage_record("unallocated", "Foundry", 42).model_copy(
            update={"ts": now},
        )
    ]
    budget = UserSettingsService(repository, Settings()).budget(reader(), now.strftime("%Y-%m"))
    assert budget.token_limit is None and budget.usage_percent is None
    assert budget.remaining_tokens is None and budget.status == "unallocated"
    assert budget.used_tokens == 42


@pytest.mark.parametrize("unpriced", [False, True])
def test_personal_usd_budget_preserves_amounts_and_unknown_cost(
    monkeypatch: pytest.MonkeyPatch, unpriced: bool,
) -> None:
    repository = InMemoryRepository()
    now = datetime.now(UTC)
    monkeypatch.setattr(repository, "list_token_budgets", lambda *args, **kwargs: [{
        "scope_type": "user", "scope_id": "test.user01@contoso.com",
        "parent_scope_id": "department-platform", "budget_mode": "usd",
        "token_limit": None, "usd_limit": Decimal("10"), "warning_threshold_percent": 80,
    }])
    monkeypatch.setattr(repository, "token_usage_by_budget_scope", lambda *args, **kwargs: [{
        "scope_type": "user", "scope_id": "test.user01@contoso.com", "used_tokens": 43,
        "used_usd": Decimal("0.000226"), "unpriced_usage_count": int(unpriced),
    }])
    budget = UserSettingsService(repository, Settings()).budget(reader(), now.strftime("%Y-%m"))
    assert budget.budget_mode == "usd" and budget.usd_limit == Decimal("10")
    assert budget.token_limit is None and budget.remaining_tokens is None
    assert budget.used_tokens == 43
    if unpriced:
        assert budget.status == "unverified" and budget.used_usd is None
        assert budget.remaining_usd is None and budget.usage_percent is None
    else:
        assert budget.status == "healthy" and budget.used_usd == Decimal("0.000226")
        assert budget.remaining_usd == Decimal("9.999774")


@pytest.mark.parametrize("period", ["2026-13", "invalid", "9999-12", "0000-01"])
def test_personal_period_rejects_invalid_or_unbounded_months(period: str) -> None:
    with pytest.raises(ValueError):
        personal_period(period)


def test_personal_usage_rejects_unknown_timezone() -> None:
    service = UserSettingsService(InMemoryRepository(), Settings())
    with pytest.raises(ValueError, match="时区"):
        service.usage(reader(), None, "day", "Not/A-Timezone")


@pytest.mark.parametrize("priced", [True, False])
def test_usd_budget_uses_own_cost_and_keeps_unpriced_balance_unknown(priced: bool) -> None:
    repository = InMemoryRepository()
    now = datetime.now(UTC)
    person = reader().email
    repository.token_budgets[(now.date().replace(day=1), "user", person)] = {
        "scope_type": "user", "scope_id": person, "parent_scope_id": "department-platform",
        "budget_mode": "usd", "token_limit": None, "usd_limit": Decimal("1000.00"),
        "warning_threshold_percent": 80,
    }
    repository.usage_records = [
        _usage_record("mine", "Foundry", 42).model_copy(update={
            "ts": now, "usd_cost": Decimal("12.50") if priced else None,
        }),
        _usage_record("foreign", "Foundry", 999).model_copy(update={
            "ts": now, "user_id": "other@example.com", "usd_cost": Decimal("999"),
        }),
    ]
    budget = UserSettingsService(repository, Settings()).budget(reader(), now.strftime("%Y-%m"))
    assert budget.budget_mode == "usd" and budget.token_limit is None
    assert budget.usd_limit == Decimal("1000.00") and budget.used_tokens == 42
    assert budget.usd_cost_available is priced
    if priced:
        assert budget.used_usd == Decimal("12.50")
        assert budget.remaining_usd == Decimal("987.50")
        assert budget.status == "healthy"
    else:
        assert budget.status == "unverified" and budget.unpriced_usage_count == 1
        assert budget.remaining_usd is None and budget.usage_percent is None
        assert budget.forecast_usd is None


def test_personal_usage_prefers_formal_usd_and_rejects_missing_cache_prices() -> None:
    repository = InMemoryRepository()
    now = datetime.now(UTC)
    repository.usage_records = [
        _usage_record("formal", "Foundry", 10).model_copy(update={
            "ts": now, "usd_cost": Decimal("0.01234567890123"), "estimated_cost": 50,
        }),
        _usage_record("cache-price-missing", "Foundry", 10).model_copy(update={
            "ts": now, "cached_tokens": 5,
            "input_price_per_million": 1, "output_price_per_million": 1,
        }),
    ]
    usage = UserSettingsService(repository, Settings()).usage(
        reader(), now.strftime("%Y-%m"), "day", "UTC",
    )
    assert usage.totals.cost_state == "partial"
    assert usage.totals.estimated_cost_usd == pytest.approx(0.01234567890123)


def test_usd_budget_does_not_infer_zero_cost_from_token_only_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryRepository()
    now = datetime.now(UTC)
    person = reader().email
    repository.token_budgets[(now.date().replace(day=1), "user", person)] = {
        "scope_type": "user", "scope_id": person, "parent_scope_id": None,
        "budget_mode": "usd", "token_limit": None, "usd_limit": Decimal("100.00"),
        "warning_threshold_percent": 80,
    }
    monkeypatch.setattr(repository, "token_usage_by_budget_scope", lambda *args, **kwargs: [{
        "scope_type": "user", "scope_id": person, "used_tokens": 42,
    }])
    budget = UserSettingsService(repository, Settings()).budget(reader(), now.strftime("%Y-%m"))
    assert budget.status == "unverified" and not budget.usd_cost_available
    assert budget.remaining_usd is None and budget.forecast_usd is None
    assert budget.usage_percent is None and budget.forecast_percent is None
