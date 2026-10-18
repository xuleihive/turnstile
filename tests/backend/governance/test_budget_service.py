from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from backend.services.budget_service import BudgetConflictError, TokenBudgetService
from turnstile_core.domain.models import (
    BudgetScopeType,
    TokenBudgetBulkWrite,
    TokenBudgetWrite,
    TokenUsageRecord,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import BudgetConstraintViolation
from turnstile_core.persistence.repository_budgets import PostgreSqlBudgetRepositoryMixin
from turnstile_core.persistence.repository_support import proportional_budget_tree


def _write_usage(
    repository: InMemoryRepository,
    user_id: str,
    sequence: int,
) -> None:
    repository.write_token_usage(
        TokenUsageRecord(
            id=f"budget-risk-{sequence}",
            request_id=f"budget-risk-{sequence}",
            correlation_id=f"budget-risk-{sequence}",
            ts=datetime(2026, 7, 20, tzinfo=UTC),
            team="AI Platform",
            organization="Contoso Global",
            organization_id="org-contoso-global",
            department="AI Platform",
            department_id="department-platform",
            project="Model FinOps",
            project_id="project-finops",
            user=user_id,
            user_id=user_id,
            agent="Delivery Engineer",
            agent_id="agent-delivery",
            workflow="budget-risk-test",
            run_id=f"budget-risk-{sequence}",
            turn_index=1,
            provider="test",
            model="test",
            model_id="test",
            runtime="test",
            request_source="test",
            input_tokens=300,
            cached_tokens=50,
            output_tokens=150,
            et=500,
            et_coeff_m=1,
            latency_ms=1,
            status="success",
            status_code=200,
            estimated_cost=0,
            estimated=False,
            ingest_source="gateway",
        )
    )


def test_compact_budget_overview_includes_people_in_risk_summary() -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    user_id = "test.user01@contoso.com"
    _write_usage(repository, user_id, 1)

    allocations: tuple[tuple[BudgetScopeType, str, int], ...] = (
        ("organization", "org-contoso-global", 10_000),
        ("department", "department-platform", 6_000),
        ("user", user_id, 400),
    )
    for scope_type, scope_id, token_limit in allocations:
        service.save(
            "2026-07",
            scope_type,
            scope_id,
            TokenBudgetWrite(
                token_limit=token_limit,
                warning_threshold_percent=80,
            ),
            "test",
        )

    compact = service.overview("2026-07")

    assert all(item.scope_type != "user" for item in compact.items)
    assert compact.risk_count == 1
    assert compact.risk_items[0].model_dump() == {
        "scope_type": "user",
        "scope_id": user_id,
        "scope_name": user_id,
        "status": "exceeded",
        "usage_percent": 125.0,
        "forecast_percent": 125.0,
    }


def test_budget_risk_summary_is_bounded_without_losing_total_count() -> None:
    repository = InMemoryRepository()
    period_start = date(2026, 7, 1)
    for sequence in range(30):
        user_id = f"risk.user{sequence:02d}@contoso.com"
        _write_usage(repository, user_id, sequence)
        repository.upsert_token_budget(
            period_start,
            "user",
            user_id,
            "department-platform",
            400,
            80,
            "test",
        )

    compact = TokenBudgetService(repository).overview("2026-07")

    assert all(item.scope_type != "user" for item in compact.items)
    assert compact.risk_count == 30
    assert len(compact.risk_items) == 25
    assert all(item.scope_type == "user" for item in compact.risk_items)


def test_usd_budget_overview_uses_priced_totals_without_unpriced_global_poisoning() -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    user_id = "test.user01@contoso.com"
    _write_usage(repository, user_id, 70)
    repository.usage_records[-1] = repository.usage_records[-1].model_copy(
        update={"usd_cost": Decimal("0.12345678901234")}
    )

    for scope_type, scope_id, amount in (
        ("organization", "org-contoso-global", Decimal("2.00")),
        ("department", "department-platform", Decimal("1.00")),
        ("user", user_id, Decimal("0.50")),
    ):
        service.save(
            "2026-07",
            cast(BudgetScopeType, scope_type),
            scope_id,
            TokenBudgetWrite(budget_mode="usd", usd_limit=amount),
            "owner",
        )

    clock = MagicMock(wraps=datetime)
    clock.now.return_value = datetime(2026, 7, 20, tzinfo=UTC)
    with patch("backend.services.budget_service.datetime", clock):
        user = next(
            item
            for item in service.overview("2026-07", include_users=True).items
            if item.scope_type == "user" and item.scope_id == user_id
        )
    assert user.budget_mode == "usd"
    assert user.used_usd == Decimal("0.12345678901234")
    assert user.remaining_usd == Decimal("0.37654321098766")
    assert user.forecast_usd == Decimal("0.19135802296913")
    assert user.usd_cost_available is True
    assert user.unpriced_usage_count == 0
    assert user.forecast_percent == 38.3
    assert user.status == "healthy"

    _write_usage(repository, user_id, 71)
    original_usage = [item.model_dump() for item in repository.usage_records]
    with patch("backend.services.budget_service.datetime", clock):
        mixed = service.overview("2026-07", include_users=True)
    for item in mixed.items:
        if item.scope_id not in {"org-contoso-global", "department-platform", user_id}:
            continue
        assert item.status == "healthy"
        assert item.used_usd == Decimal("0.12345678901234")
        assert item.usd_limit is not None
        assert item.remaining_usd == item.usd_limit - item.used_usd
        assert item.forecast_usd == Decimal("0.19135802296913")
        assert item.usd_cost_available is False
        assert item.unpriced_usage_count == 1
    assert [item.model_dump() for item in repository.usage_records] == original_usage


@pytest.mark.parametrize("mode", ["tokens", "usd"])
@pytest.mark.parametrize("allocate_department", [False, True])
def test_unallocated_scopes_inherit_budget_mode_without_creating_limits(
    mode: str,
    allocate_department: bool,
) -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    write = (
        TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("100.00"))
        if mode == "usd"
        else TokenBudgetWrite(token_limit=1000)
    )
    service.save("2026-07", "organization", "org-contoso-global", write, "owner")
    if allocate_department:
        department_write = (
            TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("50.00"))
            if mode == "usd"
            else TokenBudgetWrite(token_limit=500)
        )
        service.save("2026-07", "department", "department-platform", department_write, "owner")
    original_budgets = {key: dict(value) for key, value in repository.token_budgets.items()}
    hierarchy = service.overview("2026-07", include_users=True)
    unallocated = [item for item in hierarchy.items if item.status == "unallocated"]
    assert {item.scope_type for item in unallocated} == {"department", "user"}
    assert all(item.budget_mode == mode for item in hierarchy.items)
    for item in unallocated:
        assert item.token_limit is None and item.usd_limit is None
        assert item.remaining_tokens is None and item.remaining_usd is None
    people = service.people("2026-07", "department-platform", None, "unallocated", 0, 50)
    assert people.items
    assert all(item.budget_mode == mode and item.status == "unallocated" for item in people.items)
    assert all(item.token_limit is None and item.usd_limit is None for item in people.items)
    assert repository.token_budgets == original_budgets
    other_month = service.overview("2026-08", include_users=True)
    assert all(item.budget_mode == "tokens" for item in other_month.items)


@pytest.mark.parametrize("mode", ["tokens", "usd"])
def test_subscription_consumption_is_included_once_in_organization_and_department(
    mode: str,
) -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    for scope_type, scope_id, tokens, amount in (
        ("organization", "org-contoso-global", 10_000, Decimal("10.00")),
        ("department", "department-platform", 5_000, Decimal("5.00")),
    ):
        write = (
            TokenBudgetWrite(budget_mode="usd", usd_limit=amount)
            if mode == "usd"
            else TokenBudgetWrite(token_limit=tokens)
        )
        service.save("2026-07", cast(BudgetScopeType, scope_type), scope_id, write, "owner")
    _write_usage(repository, "service:unit-subscription", 201)
    subscription = repository.usage_records[-1].model_copy(
        update={
            "usd_cost": Decimal("0.125"),
            "request_source": "subscription",
        }
    )
    repository.usage_records[-1] = subscription
    repository.write_token_usage(subscription)
    _write_usage(repository, "test.user01@contoso.com", 202)
    repository.usage_records[-1] = repository.usage_records[-1].model_copy(
        update={"usd_cost": Decimal("0.020")}
    )
    original = [record.model_dump() for record in repository.usage_records]
    report = service.overview("2026-07", include_users=True)
    for item in report.items:
        if item.scope_id in {"org-contoso-global", "department-platform"}:
            assert item.used_tokens == 1000
            assert item.used_usd == Decimal("0.145")
    assert len(repository.usage_records) == 2
    assert all(item.scope_id != "service:unit-subscription" for item in report.items)
    rebound = subscription.model_copy(
        update={
            "department_id": "department-commerce",
            "department": "Commerce",
        }
    )
    repository.write_token_usage(rebound)
    assert [record.model_dump() for record in repository.usage_records] == original
    reread = service.overview("2026-07")
    platform = next(item for item in reread.items if item.scope_id == "department-platform")
    commerce = next(item for item in reread.items if item.scope_id == "department-commerce")
    assert platform.used_tokens == 1000 and platform.used_usd == Decimal("0.145")
    assert commerce.used_tokens == 0 and commerce.used_usd == Decimal(0)


def test_usd_usage_cost_migration_requires_only_consumed_component_prices() -> None:
    from pathlib import Path

    from pglast import parse_sql  # type: ignore[import-untyped]

    root = Path(__file__).resolve().parents[3]
    migration = (root / "migrations/025_usage_cost_component_prices.up.sql").read_text()
    parse_sql(migration)
    assert "WHEN is_estimated IS DISTINCT FROM FALSE THEN NULL" in migration
    assert "WHEN stored_cost IS NOT NULL THEN stored_cost::NUMERIC(30,14)" in migration
    assert "input_tokens = 0 OR input_rate IS NOT NULL" in migration
    assert "cache_write_tokens = 0 OR cache_write_rate IS NOT NULL" in migration
    assert "output_tokens = 0 OR output_rate IS NOT NULL" in migration
    assert "GREATEST(cached_tokens - cache_write_tokens, 0) = 0" in migration
    assert "UPDATE token_usage" not in migration
    assert "INSERT" not in migration and "DELETE" not in migration
    assert not (root / "migrations/025_usage_cost_component_prices.down.sql").exists()
    assert "DROP" not in migration


def test_usd_budget_hierarchy_rejects_cross_mode_parent_child() -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    service.save(
        "2026-07",
        "organization",
        "org-contoso-global",
        TokenBudgetWrite(token_limit=1000),
        "owner",
    )
    with pytest.raises(BudgetConflictError, match="same mode"):
        service.save(
            "2026-07",
            "department",
            "department-platform",
            TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("10.00")),
            "owner",
        )


@pytest.mark.parametrize("initial_mode", ["tokens", "usd"])
def test_organization_switches_its_tree_proportionally_without_resetting_usage(
    initial_mode: str,
) -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    user_id = "test.user01@contoso.com"
    _write_usage(repository, user_id, 90)
    repository.usage_records[-1] = repository.usage_records[-1].model_copy(
        update={"usd_cost": Decimal("0.12345678901234")}
    )
    original_usage = repository.usage_records[-1].model_dump()
    allocations = (
        ("organization", "org-contoso-global", 200_000_000, Decimal("1000.00")),
        ("department", "department-platform", 100_000_000, Decimal("500.00")),
        ("user", user_id, 50_000_000, Decimal("250.00")),
    )
    for scope_type, scope_id, tokens, usd in allocations:
        write = (
            TokenBudgetWrite(token_limit=tokens)
            if initial_mode == "tokens"
            else TokenBudgetWrite(budget_mode="usd", usd_limit=usd)
        )
        service.save("2026-07", cast(BudgetScopeType, scope_type), scope_id, write, "owner")
    target_mode = "usd" if initial_mode == "tokens" else "tokens"
    target = (
        TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("1000.00"))
        if target_mode == "usd"
        else TokenBudgetWrite(token_limit=200_000_000)
    )
    service.save("2026-07", "organization", "org-contoso-global", target, "owner")
    for scope_type, scope_id, tokens, usd in allocations:
        budget = repository.token_budgets[(date(2026, 7, 1), scope_type, scope_id)]
        assert budget["budget_mode"] == target_mode
        assert budget["token_limit"] == (tokens if target_mode == "tokens" else None)
        assert budget["usd_limit"] == (usd if target_mode == "usd" else None)
    person = next(
        item
        for item in service.overview("2026-07", include_users=True).items
        if item.scope_type == "user" and item.scope_id == user_id
    )
    assert person.used_tokens == 500
    assert person.used_usd == Decimal("0.12345678901234")
    assert repository.usage_records[-1].model_dump() == original_usage


@pytest.mark.parametrize(
    "amount,department_usd,person_usd,department_tokens,person_tokens",
    [
        ("4.00", "3", "2", 7, 4),
        ("3.20", "2", "1", 6, 3),
    ],
)
def test_organization_switch_rounds_usd_and_does_not_change_other_settings(
    amount: str,
    department_usd: str,
    person_usd: str,
    department_tokens: int,
    person_tokens: int,
) -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    period = date(2026, 7, 1)
    root_id, department_id, user_id = (
        "org-contoso-global",
        "department-platform",
        "test.user01@contoso.com",
    )
    for scope, scope_id, limit in (
        ("organization", root_id, 3),
        ("department", department_id, 2),
        ("user", user_id, 1),
    ):
        service.save(
            "2026-07", cast(BudgetScopeType, scope), scope_id,
            TokenBudgetWrite(token_limit=limit), "owner",
        )
    unrelated = {
        **repository.token_budgets[(period, "organization", root_id)],
        "scope_id": "other-organization",
        "token_limit": 99,
    }
    repository.token_budgets[(period, "organization", "other-organization")] = unrelated
    repository.token_budgets[(date(2026, 8, 1), "organization", root_id)] = {
        **repository.token_budgets[(period, "organization", root_id)],
        "period_start": date(2026, 8, 1),
    }
    enforcement = dict(repository.department_enforcement)
    models = dict(repository.user_model_policies)
    other_month = dict(repository.token_budgets[(date(2026, 8, 1), "organization", root_id)])
    service.save(
        "2026-07",
        "organization",
        root_id,
        TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal(amount)),
        "owner",
    )
    assert repository.token_budgets[(period, "department", department_id)]["usd_limit"] == Decimal(
        department_usd
    )
    assert repository.token_budgets[(period, "user", user_id)]["usd_limit"] == Decimal(person_usd)
    assert repository.token_budgets[(period, "organization", "other-organization")] == unrelated
    assert repository.token_budgets[(date(2026, 8, 1), "organization", root_id)] == other_month
    assert repository.department_enforcement == enforcement
    assert repository.user_model_policies == models
    assert (
        len([event for event in repository.token_budget_audit if event["new_budget_mode"] == "usd"])
        == 3
    )
    service.save("2026-07", "organization", root_id, TokenBudgetWrite(token_limit=10), "owner")
    assert (
        repository.token_budgets[(period, "department", department_id)]["token_limit"]
        == department_tokens
    )
    assert repository.token_budgets[(period, "user", user_id)]["token_limit"] == person_tokens


def test_organization_switch_keeps_small_usd_allocations_within_parent_balance() -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    allocations = (
        ("organization", "org-contoso-global", 200_000_000, Decimal("1000")),
        ("department", "department-platform", 70_000_000, Decimal("350")),
        ("department", "department-commerce", 70_000_000, Decimal("350")),
        ("user", "test.user01@contoso.com", 1200, Decimal("1")),
        ("user", "test.user06@contoso.com", 3000, Decimal("1")),
        ("user", "test.user11@contoso.com", 1_000_000, Decimal("5")),
        ("user", "test.user16@contoso.com", 60_000_000, Decimal("300")),
    )
    for scope_type, scope_id, tokens, _ in allocations:
        service.save(
            "2026-07", cast(BudgetScopeType, scope_type), scope_id,
            TokenBudgetWrite(token_limit=tokens), "owner",
        )
    service.save(
        "2026-07",
        "organization",
        "org-contoso-global",
        TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("1000")),
        "owner",
    )
    for scope_type, scope_id, _, usd in allocations:
        budget = repository.token_budgets[(date(2026, 7, 1), scope_type, scope_id)]
        assert budget["usd_limit"] == usd and budget["token_limit"] is None
    assert len(repository.token_budgets) == len(allocations)
    assert Decimal("350") - sum(row[3] for row in allocations if row[0] == "user") == 43


@pytest.mark.parametrize(
    "level,amount,departments,people",
    [
        ("organization", "1.00", (50, 50), ()),
        ("department", "2.00", (50,), (1, 1)),
    ],
)
def test_organization_switch_rejects_rounded_child_shortfall_before_any_change(
    level: str,
    amount: str,
    departments: tuple[int, ...],
    people: tuple[int, ...],
) -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    service.save(
        "2026-07", "organization", "org-contoso-global", TokenBudgetWrite(token_limit=100), "owner"
    )
    for scope_id, limit in zip(
        ("department-platform", "department-commerce"),
        departments,
        strict=False,
    ):
        service.save(
            "2026-07", "department", scope_id, TokenBudgetWrite(token_limit=limit), "owner"
        )
    for scope_id, limit in zip(
        ("test.user01@contoso.com", "test.user06@contoso.com"),
        people,
        strict=False,
    ):
        service.save("2026-07", "user", scope_id, TokenBudgetWrite(token_limit=limit), "owner")
    before = {key: dict(row) for key, row in repository.token_budgets.items()}
    audit = list(repository.token_budget_audit)
    with pytest.raises(BudgetConflictError, match=level + " budget by USD 1.00"):
        service.save(
            "2026-07",
            "organization",
            "org-contoso-global",
            TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal(amount)),
            "owner",
        )
    assert repository.token_budgets == before and repository.token_budget_audit == audit


def test_organization_switch_rejects_too_small_allocation_before_any_change() -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    service.save(
        "2026-07", "organization", "org-contoso-global", TokenBudgetWrite(token_limit=100), "owner"
    )
    service.save(
        "2026-07", "department", "department-platform", TokenBudgetWrite(token_limit=1), "owner"
    )
    before = {key: dict(row) for key, row in repository.token_budgets.items()}
    audit = list(repository.token_budget_audit)
    with pytest.raises(BudgetConflictError, match="too small"):
        service.save(
            "2026-07",
            "organization",
            "org-contoso-global",
            TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("0.01")),
            "owner",
        )
    assert repository.token_budgets == before and repository.token_budget_audit == audit


@pytest.mark.parametrize("failure", [None, "too_small", "write_failure"])
def test_postgres_organization_switch_is_one_audited_transaction(failure: str | None) -> None:
    from pglast import parse_sql

    rows = [
        {
            "period_start": date(2026, 7, 1),
            "scope_type": scope,
            "scope_id": scope_id,
            "parent_scope_id": parent,
            "budget_mode": "tokens",
            "token_limit": limit,
            "usd_limit": None,
            "warning_threshold_percent": 80,
        }
        for scope, scope_id, parent, limit in (
            ("organization", "org", None, 200_000_000),
            ("department", "dept", "org", 100_000_000),
            ("user", "person", "dept", 50_000_000),
        )
    ]
    connection, context, cursor = MagicMock(), MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    cursor.fetchall.return_value = rows
    updates = 0

    def execute(query: str, parameters: object) -> MagicMock:
        nonlocal updates
        parse_sql(query.replace("%s", "NULL"))
        if query.startswith("UPDATE token_budget"):
            updates += 1
            if failure == "write_failure" and updates == 2:
                raise RuntimeError("unit write failure")
        return cursor

    connection.execute.side_effect = execute
    repository = PostgreSqlBudgetRepositoryMixin()
    with patch.object(repository, "_connection", return_value=context):
        if failure:
            with pytest.raises(ValueError if failure == "too_small" else RuntimeError):
                repository.switch_organization_budget(
                    date(2026, 7, 1),
                    "org",
                    "usd",
                    Decimal("0.01") if failure == "too_small" else Decimal("1000"),
                    80,
                    "owner",
                )
        else:
            repository.switch_organization_budget(
                date(2026, 7, 1),
                "org",
                "usd",
                Decimal("1000"),
                80,
                "owner",
            )
    connection.transaction.assert_called_once_with()
    calls = connection.execute.call_args_list
    assert calls[0].args[1] == ("budget-config:2026-07-01",)
    assert "FOR UPDATE" in calls[1].args[0]
    assert all("token_usage" not in call.args[0] for call in calls)
    if failure:
        assert connection.transaction.return_value.__exit__.call_args.args[0] is not None
        if failure == "too_small":
            assert updates == 0
    else:
        changed = [call.args[1] for call in calls if call.args[0].startswith("UPDATE token_budget")]
        assert [parameters[:3] for parameters in changed] == [
            ("usd", None, Decimal("1000")),
            ("usd", None, Decimal("500")),
            ("usd", None, Decimal("250")),
        ]
        assert (
            len([call for call in calls if "INSERT INTO token_budget_audit" in call.args[0]]) == 3
        )
        assert connection.transaction.return_value.__exit__.call_args.args[0] is None


def test_usd_organization_api_save_requires_existing_atomic_rollout() -> None:
    from unittest.mock import Mock

    from fastapi import HTTPException

    from backend.http.service_dependencies import token_budget_service
    from turnstile_core.config import Settings

    repository = InMemoryRepository()
    with patch("backend.http.service_dependencies.get_settings", return_value=Settings()):
        service = token_budget_service(repository)
    with patch.object(
        repository,
        "upsert_token_budget",
        wraps=repository.upsert_token_budget,
    ) as save:
        with pytest.raises(HTTPException) as error:
            service.save(
                "2026-07",
                "organization",
                "org-contoso-global",
                TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("1000")),
                "owner",
            )
        assert error.value.status_code == 503
        save.assert_not_called()
    guard = Mock()
    service = TokenBudgetService(repository, require_atomic_budget=guard)
    service.save(
        "2026-07",
        "organization",
        "org-contoso-global",
        TokenBudgetWrite(token_limit=200_000_000),
        "owner",
    )
    guard.assert_not_called()
    service.save(
        "2026-07",
        "organization",
        "org-contoso-global",
        TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("1000")),
        "owner",
    )
    guard.assert_called_once_with()


def test_usd_bulk_equal_allocation_preserves_mode_and_totals() -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    user_ids = ["test.user01@contoso.com", "test.user11@contoso.com"]
    for sequence, user_id in enumerate(user_ids, start=80):
        _write_usage(repository, user_id, sequence)
    service.save(
        "2026-07",
        "organization",
        "org-contoso-global",
        TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("20.00")),
        "owner",
    )
    service.save(
        "2026-07",
        "department",
        "department-platform",
        TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("10.00")),
        "owner",
    )

    result = service.bulk_save_people(
        "2026-07",
        TokenBudgetBulkWrite(
            department_id="department-platform",
            user_ids=user_ids,
            allocation_mode="equal_remaining",
            budget_mode="usd",
        ),
        "owner",
    )

    assert result.usd_limit_per_user == Decimal("5.00")
    assert result.total_usd_allocated == Decimal("10.00")
    assert result.token_limit_per_user is None
    assert all(
        repository.token_budgets[(date(2026, 7, 1), "user", user_id)]["budget_mode"] == "usd"
        and repository.token_budgets[(date(2026, 7, 1), "user", user_id)]["token_limit"] is None
        and repository.token_budgets[(date(2026, 7, 1), "user", user_id)]["usd_limit"]
        == Decimal("5.00")
        for user_id in user_ids
    )


def test_postgres_usd_bulk_write_binds_only_usd_child_limits() -> None:
    connection = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = connection
    connection.execute.side_effect = [
        MagicMock(fetchone=MagicMock(return_value={"present": None})),
        MagicMock(fetchone=MagicMock(return_value=None)),
        MagicMock(fetchone=MagicMock(return_value=None)),
        MagicMock(
            fetchone=MagicMock(
                return_value={
                    "budget_mode": "usd",
                    "token_limit": None,
                    "usd_limit": Decimal("10.00"),
                }
            )
        ),
        MagicMock(fetchone=MagicMock(return_value={"total": Decimal(0), "mixed_mode": False})),
        MagicMock(fetchall=MagicMock(return_value=[])),
    ]
    repository = PostgreSqlBudgetRepositoryMixin()
    with patch.object(repository, "_connection", return_value=context):
        repository.bulk_upsert_user_budgets(
            date(2026, 7, 1),
            "department-platform",
            [("test.user01@contoso.com", Decimal("5.00"))],
            80,
            "owner",
            budget_mode="usd",
            selected_user_ids=["test.user01@contoso.com"],
        )

    cursor = connection.cursor.return_value.__enter__.return_value
    budget_sql, budget_rows = cursor.executemany.call_args_list[0].args
    assert "budget_mode, token_limit, usd_limit" in budget_sql
    assert budget_rows[0][3:6] == ("usd", None, Decimal("5.00"))
    audit_sql, audit_rows = cursor.executemany.call_args_list[1].args
    assert "previous_usd_limit, new_usd_limit" in audit_sql
    assert audit_rows[0][4:10] == (None, "usd", None, None, None, Decimal("5.00"))


def test_legacy_user_cache_metric_is_ignored_by_budget_surfaces() -> None:
    repository = InMemoryRepository()
    period_start = date(2026, 7, 1)
    period_end = date(2026, 8, 1)
    user_id = "alex@contoso.com"
    repository.upsert_token_budget(
        period_start,
        "user",
        user_id,
        "department-platform",
        5000,
        80,
        "owner@example.com",
    )
    _write_usage(repository, user_id, 1)
    record = repository.usage_records[-1].model_copy(
        update={
            "ts": datetime(2026, 7, 15, tzinfo=UTC),
            "input_tokens": 100,
            "cached_tokens": 0,
            "output_tokens": 0,
            "ingest_source": "eventhub",
        }
    )
    repository.usage_records[-1] = record
    bucket = record.ts.replace(minute=0, second=0, microsecond=0)
    repository.apim_cache_read_buckets[("turnstile-llm", "user", user_id, bucket)] = 900

    scope_rows = repository.token_usage_by_budget_scope(
        datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
    )
    ledger = repository.budget_ledger_snapshot(period_start, period_end)
    state = repository.person_budget_state(user_id, period_start, period_end)

    user_scope = next(
        row for row in scope_rows if row["scope_type"] == "user" and row["scope_id"] == user_id
    )
    assert user_scope["used_tokens"] == 100
    assert ledger[0]["confirmed_tokens"] == 100
    assert state is not None and state["used_tokens"] == 100


def test_apim_budget_surfaces_exclude_copilot_cli_usage() -> None:
    repository = InMemoryRepository()
    period_start = date(2026, 7, 1)
    period_end = date(2026, 8, 1)
    user_id = "alex@contoso.com"
    repository.upsert_token_budget(
        period_start,
        "user",
        user_id,
        "department-platform",
        5000,
        80,
        "owner@example.com",
    )
    _write_usage(repository, user_id, 1)
    apim = repository.usage_records[-1].model_copy(
        update={"input_tokens": 100, "cached_tokens": 0, "output_tokens": 0}
    )
    copilot = apim.model_copy(
        update={
            "id": "copilot-budget-usage",
            "request_id": "copilot-budget-usage",
            "correlation_id": "copilot-budget-usage",
            "input_tokens": 900,
            "runtime": "GitHub Copilot CLI",
            "ingest_source": "copilot_cli",
            "usage_domain": "github_copilot",
        }
    )
    repository.usage_records[-1] = apim
    repository.write_token_usage(copilot)

    scope_rows = repository.token_usage_by_budget_scope(
        datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
    )
    ledger = repository.budget_ledger_snapshot(period_start, period_end)
    state = repository.person_budget_state(user_id, period_start, period_end)

    user_scope = next(
        row for row in scope_rows if row["scope_type"] == "user" and row["scope_id"] == user_id
    )
    assert user_scope["used_tokens"] == 100
    assert ledger[0]["confirmed_tokens"] == 100
    assert state is not None and state["used_tokens"] == 100


def test_budget_dtos_preserve_exact_mode_and_reject_mixed_limits() -> None:
    token = TokenBudgetWrite(token_limit=100)
    assert token.budget_mode == "tokens" and token.usd_limit is None
    usd = TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("100.25"))
    assert usd.token_limit is None
    assert usd.model_dump(mode="json")["usd_limit"] == "100.25"
    for value in [
        {"budget_mode": "usd", "token_limit": 100, "usd_limit": "1"},
        {"budget_mode": "usd"},
        {"token_limit": 100, "usd_limit": "1"},
        {"budget_mode": "usd", "usd_limit": "0"},
        {"budget_mode": "usd", "usd_limit": "1.001"},
    ]:
        with pytest.raises(ValueError):
            TokenBudgetWrite.model_validate(value)
    assert TokenBudgetBulkWrite(
        department_id="unit", budget_mode="usd", usd_limit=Decimal("12.50")
    ).usd_limit == Decimal("12.50")
    preserved = TokenBudgetBulkWrite(department_id="unit", allocation_mode="preserve")
    assert preserved.token_limit is None
    with pytest.raises(ValueError):
        TokenBudgetBulkWrite(department_id="unit", allocation_mode="equal_remaining", token_limit=1)


def test_organization_unit_plan_preserves_source_ratios_rounding_and_input_rows() -> None:
    rows = [
        {
            "scope_type": scope,
            "scope_id": identity,
            "parent_scope_id": parent,
            "budget_mode": "tokens",
            "token_limit": amount,
            "warning_threshold_percent": 80,
        }
        for scope, identity, parent, amount in [
            ("organization", "org", None, 1000),
            ("department", "dept", "org", 500),
            ("user", "person", "dept", 100),
        ]
    ]
    original = deepcopy(rows)
    result = proportional_budget_tree(rows, "org", "usd", Decimal("101"), 90)
    assert [row["usd_limit"] for row in result] == [Decimal("101"), Decimal("51"), Decimal("10")]
    assert all(row["token_limit"] is None for row in result)
    assert [row["warning_threshold_percent"] for row in result] == [90, 80, 80]
    assert rows == original


def test_organization_unit_plan_rejects_whole_dollar_shortfall_without_mutating_rows() -> None:
    rows = [
        {
            "scope_type": scope,
            "scope_id": identity,
            "parent_scope_id": parent,
            "budget_mode": "tokens",
            "token_limit": amount,
            "warning_threshold_percent": 80,
        }
        for scope, identity, parent, amount in [
            ("organization", "org", None, 1000),
            ("department", "first", "org", 500),
            ("department", "second", "org", 500),
        ]
    ]
    original = deepcopy(rows)
    with pytest.raises(BudgetConstraintViolation, match="rounded allocations exceed"):
        proportional_budget_tree(rows, "org", "usd", Decimal("1"), 80)
    assert rows == original


def test_usd_budget_overview_marks_partial_billed_cost_without_rewriting_unpriced_usage() -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    user_id = "test.user01@contoso.com"
    _write_usage(repository, user_id, 70)
    repository.usage_records[-1] = repository.usage_records[-1].model_copy(
        update={"usd_cost": Decimal("0.12345678901234")},
    )
    allocations: tuple[tuple[BudgetScopeType, str, Decimal], ...] = (
        ("organization", "org-contoso-global", Decimal("2.00")),
        ("department", "department-platform", Decimal("1.00")),
        ("user", user_id, Decimal("0.50")),
    )
    for scope_type, scope_id, amount in allocations:
        service.save(
            "2026-07",
            scope_type,
            scope_id,
            TokenBudgetWrite(budget_mode="usd", usd_limit=amount),
            "owner",
        )
    user = next(
        item
        for item in service.overview("2026-07", include_users=True).items
        if item.scope_type == "user" and item.scope_id == user_id
    )
    assert user.budget_mode == "usd"
    assert user.used_usd == Decimal("0.12345678901234")
    assert user.remaining_usd == Decimal("0.37654321098766")
    assert user.status == "healthy"
    assert user.usd_cost_available is True and user.unpriced_usage_count == 0
    _write_usage(repository, user_id, 71)
    original_usage = [record.model_dump() for record in repository.usage_records]
    partial = next(
        item
        for item in service.overview("2026-07", include_users=True).items
        if item.scope_type == "user" and item.scope_id == user_id
    )
    assert partial.status == "healthy"
    assert partial.used_usd == Decimal("0.12345678901234")
    assert partial.remaining_usd == Decimal("0.37654321098766")
    assert partial.usd_cost_available is False and partial.unpriced_usage_count == 1
    assert [record.model_dump() for record in repository.usage_records] == original_usage


def test_usd_people_filters_count_assigned_and_keep_department_money_units() -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    user_id = "test.user01@contoso.com"
    allocations: tuple[tuple[BudgetScopeType, str, Decimal], ...] = (
        ("organization", "org-contoso-global", Decimal("100.00")),
        ("department", "department-platform", Decimal("50.00")),
        ("user", user_id, Decimal("12.50")),
    )
    for scope_type, scope_id, amount in allocations:
        service.save(
            "2026-07",
            scope_type,
            scope_id,
            TokenBudgetWrite(budget_mode="usd", usd_limit=amount),
            "owner",
        )
    assigned = service.people("2026-07", "department-platform", None, "assigned", 0, 100)
    assert assigned.total == assigned.assigned_count == 1
    assert assigned.items[0].scope_id == user_id
    assert assigned.department_token_limit is None and assigned.department_available_tokens is None
    assert assigned.department_usd_limit == Decimal("50.00")
    assert assigned.department_allocated_usd == Decimal("12.50")
    assert assigned.department_available_usd == Decimal("37.50")
    unallocated = service.people("2026-07", "department-platform", None, "unallocated", 0, 100)
    assert user_id not in {person.scope_id for person in unallocated.items}


@pytest.mark.parametrize("equal", [False, True])
def test_usd_bulk_allocation_uses_source_money_rounding_and_atomic_parent_limit(
    equal: bool,
) -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    service.save(
        "2026-07",
        "organization",
        "org-contoso-global",
        TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("100.00")),
        "owner",
    )
    service.save(
        "2026-07",
        "department",
        "department-platform",
        TokenBudgetWrite(budget_mode="usd", usd_limit=Decimal("10.01")),
        "owner",
    )
    user_ids = [
        entity.id
        for entity in service._entities()["user"]
        if entity.parent_id == "department-platform"
    ][:3]
    assert len(user_ids) == 3
    write = TokenBudgetBulkWrite(
        department_id="department-platform",
        budget_mode="usd",
        user_ids=user_ids,
        allocation_mode="equal_remaining" if equal else "fixed",
        usd_limit=None if equal else Decimal("1.25"),
    )
    result = service.bulk_save_people("2026-07", write, "owner")
    expected = Decimal("3.33") if equal else Decimal("1.25")
    assert result.usd_limit_per_user == expected and result.total_usd_allocated == expected * 3
    assert result.token_limit_per_user is None and result.total_allocated is None
    original = deepcopy(repository.token_budgets)
    with pytest.raises(BudgetConflictError, match="exceed"):
        service.bulk_save_people(
            "2026-07",
            write.model_copy(
                update={
                    "allocation_mode": "fixed",
                    "usd_limit": Decimal("4.00"),
                }
            ),
            "owner",
        )
    assert repository.token_budgets == original


def test_usd_budget_roll_forward_and_removal_keep_unit_history_without_touching_usage() -> None:
    repository = InMemoryRepository()
    service = TokenBudgetService(repository)
    allocations: tuple[tuple[BudgetScopeType, str, Decimal], ...] = (
        ("organization", "org-contoso-global", Decimal("100.00")),
        ("department", "department-platform", Decimal("50.00")),
        ("user", "test.user01@contoso.com", Decimal("12.50")),
    )
    for scope_type, scope_id, amount in allocations:
        service.save(
            "2026-07",
            scope_type,
            scope_id,
            TokenBudgetWrite(budget_mode="usd", usd_limit=amount),
            "owner",
        )
    _write_usage(repository, "test.user01@contoso.com", 91)
    original_usage = list(repository.usage_records)
    outcome = repository.roll_forward_budgets(date(2026, 8, 1), "roll-forward")
    assert outcome is not None and outcome["scope_count"] == 3
    inherited = repository.token_budgets[(date(2026, 8, 1), "user", "test.user01@contoso.com")]
    assert inherited["budget_mode"] == "usd" and inherited["usd_limit"] == Decimal("12.50")
    assert inherited["token_limit"] is None
    assert repository.delete_token_budget(
        date(2026, 8, 1), "user", "test.user01@contoso.com", "owner"
    )
    removed = repository.token_budget_audit[0]
    assert removed["previous_budget_mode"] == "usd" and removed["new_budget_mode"] is None
    assert removed["previous_usd_limit"] == Decimal("12.50") and removed["new_usd_limit"] is None
    assert repository.roll_forward_budgets(date(2026, 8, 1), "roll-forward") is None
    assert repository.usage_records == original_usage


def test_model_access_save_projects_latest_full_policy_without_changing_budgets() -> None:
    repository = InMemoryRepository()
    user_id = "test.user01@contoso.com"
    other_user_id = "test.user02@contoso.com"
    models = [model for model in repository.models if model["enabled"]][:2]
    assert len(models) == 2
    for sequence, person in enumerate((user_id, other_user_id)):
        _write_usage(repository, person, sequence)
        repository.upsert_token_budget(
            date(2026, 7, 1), "user", person, "department-platform", 5_000, 80, "owner"
        )
    repository.bulk_upsert_user_budgets(
        date(2026, 7, 1),
        "department-platform",
        [],
        80,
        "owner",
        selected_user_ids=[other_user_id],
        model_ids=[models[0]["id"]],
    )
    budgets = deepcopy(repository.list_token_budgets(date(2026, 7, 1)))
    other_policy = deepcopy(repository.list_user_model_policies([other_user_id]))
    projected: list[list[dict[str, Any]]] = []

    def project(user_ids: Any) -> None:
        assert user_ids == [user_id]
        projected.append(list(repository.model_access_ledger_snapshot(user_ids)))

    service = TokenBudgetService(repository, project)
    for selection in ([models[0]], models, [models[1]], []):
        result = service.bulk_save_people(
            "2026-07",
            TokenBudgetBulkWrite(
                department_id="department-platform",
                selection="ids",
                user_ids=[user_id],
                allocation_mode="preserve",
                model_ids=[model["id"] for model in selection],
            ),
            "owner",
        )
        assert result.model_policy_updated_count == 1
        assert len(projected[-1]) == 1
        policy = projected[-1][0]
        assert policy["user_id"] == user_id
        assert set(map(str, policy["model_uuids"])) == {str(model["id"]) for model in selection}
        assert set(policy["model_keys"]) == {model["model_key"] for model in selection}
        assert repository.list_token_budgets(date(2026, 7, 1)) == budgets
        assert repository.list_user_model_policies([other_user_id]) == other_policy
    assert len(projected) == 4


@pytest.mark.parametrize("commit_failure", [False, True])
def test_model_access_failed_transaction_does_not_project(
    monkeypatch: pytest.MonkeyPatch,
    commit_failure: bool,
) -> None:
    repository = InMemoryRepository()
    user_id = "test.user01@contoso.com"
    _write_usage(repository, user_id, 1)
    projected: list[Any] = []

    def reject_write(*args: Any, **kwargs: Any) -> None:
        if commit_failure:
            raise RuntimeError("commit failed")
        raise BudgetConstraintViolation("transaction rejected")

    monkeypatch.setattr(repository, "bulk_upsert_user_budgets", reject_write)
    service = TokenBudgetService(repository, projected.append)
    with pytest.raises(RuntimeError if commit_failure else BudgetConflictError):
        service.bulk_save_people(
            "2026-07",
            TokenBudgetBulkWrite(
                department_id="department-platform",
                selection="ids",
                user_ids=[user_id],
                allocation_mode="preserve",
                model_ids=[],
            ),
            "owner",
        )
    assert projected == []
    assert repository.list_user_model_policies([user_id]) == []


def test_model_access_projection_failure_reports_saved_but_not_projected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = InMemoryRepository()
    user_id = "test.user01@contoso.com"
    _write_usage(repository, user_id, 1)

    def unavailable(user_ids: Any) -> None:
        assert user_ids == [user_id]
        raise RuntimeError("ledger unavailable")

    result = TokenBudgetService(repository, unavailable).bulk_save_people(
        "2026-07",
        TokenBudgetBulkWrite(
            department_id="department-platform",
            selection="ids",
            user_ids=[user_id],
            allocation_mode="preserve",
            model_ids=[],
        ),
        "owner",
    )
    assert result.model_policy_updated_count == 1
    assert repository.list_user_model_policies([user_id])[0]["model_ids"] == []
    assert "saved but not projected" in caplog.text
    assert "next ledger sync will repair it" in caplog.text
