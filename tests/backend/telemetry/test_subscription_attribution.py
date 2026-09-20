"""Usage that declared no identity is rolled up against the subscription that produced it.

Department and person budgets are summed from `token_usage.department_id` and `.user_id`, which
arrive in request headers and default to `unattributed` -- and the rollup's final
`WHERE scope_id <> 'unattributed'` throws those rows away. At an install where nothing sets
those headers that is every row, so the budget page reports zero forever.

Resolved on read rather than written at ingest, deliberately: `guard_apim_usage_identity` makes
`token_usage.user_id` immutable per correlation, so deriving it from a field an administrator
can edit makes the second event for a request conflict with the first and be rejected for good.
That is not hypothetical -- it is what happened when this was first built at ingest, and every
gateway call afterwards failed with "APIM correlation identity conflicts with stored usage".
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from backend.services.budget_service import TokenBudgetService
from turnstile_core.domain.application_access import (
    GatewayApplicationDepartmentUpdate,
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
    GatewayApplicationOwnerUpdate,
)
from turnstile_core.ingestion.processor import CoefficientResolver, UsageProcessor
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.application_access import ApplicationAccessService

GATEWAY_ID = UUID("10000000-0000-4000-8000-000000000001")
KEY = "dongyuli-it"
PERIOD = "2026-09"
OWNER = "dongyuli@insilicomedicine.com"


def _event(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "usage-1",
        "ts": datetime(2026, 9, 20, tzinfo=UTC).isoformat(),
        "provider": "aoai",
        "model": "gpt-4.1-mini",
        "input_tokens": 100,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 20,
        "turn_index": 1,
        "latency_ms": 900,
        "status": "success",
        "ingest_source": "eventhub",
        "gateway_profile_id": str(GATEWAY_ID),
        "apim_subscription_id": KEY,
        "application_actor_type": "service",
        "application_actor_id": "service:" + KEY,
    }
    value.update(overrides)
    return value


def _install(
    *, department: str | None, owner: str | None, system_managed: bool = False
) -> tuple[InMemoryRepository, UsageProcessor]:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    service.sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=GATEWAY_ID,
            discovered_at=datetime(2026, 9, 20, tzinfo=UTC),
            items=[
                GatewayApplicationDiscoveryItem(
                    apim_subscription_id=KEY,
                    display_name="Turnstile Dashboard" if system_managed else "dongyuli-IT",
                    state="active",
                    scope_type="product",
                    scope_id="finops-applications",
                    scope_exists=True,
                    application_type="system" if system_managed else "service",
                    system_managed=system_managed,
                )
            ],
        ),
        "application-sync-worker",
    )
    application_id = repository.gateway_applications[0]["id"]
    if department is not None:
        service.update_application_department(
            application_id, GatewayApplicationDepartmentUpdate(department_id=department), "owner"
        )
    if owner is not None:
        service.update_application_owner(
            application_id, GatewayApplicationOwnerUpdate(owner_id=owner), "owner"
        )
    return repository, UsageProcessor(repository, CoefficientResolver({"default": 1.0}))


def _rolled_up(repository: InMemoryRepository) -> dict[tuple[str, str], int]:
    rows = repository.subscription_attributed_usage(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
    )
    return {(row["scope_type"], row["scope_id"]): int(row["used_tokens"]) for row in rows}


def test_a_request_declaring_nothing_rolls_up_to_the_filed_department() -> None:
    repository, processor = _install(department="department-security", owner=OWNER)

    assert processor.process(_event()) is True

    totals = _rolled_up(repository)
    assert totals[("department", "department-security")] == 120
    assert totals[("user", OWNER)] == 120
    # The organization total has to move with it, or the page shows departments spending
    # against an organization that spent nothing.
    assert totals[("organization", "org-contoso-global")] == 120


def test_the_usage_row_itself_is_left_alone() -> None:
    repository, processor = _install(department="department-security", owner=OWNER)

    assert processor.process(_event()) is True

    # `guard_apim_usage_identity` makes this immutable per correlation. Writing a derived value
    # here is what made every gateway call fail when this was first built at ingest.
    record = repository.usage_records[0]
    assert record.department_id == "unattributed"
    assert record.user_id == "unattributed"


def test_an_unfiled_subscription_contributes_nothing() -> None:
    repository, processor = _install(department=None, owner=None)

    assert processor.process(_event()) is True

    assert _rolled_up(repository) == {}


def test_a_declared_identity_is_not_counted_twice() -> None:
    repository, processor = _install(department="department-security", owner=OWNER)

    assert processor.process(
        _event(department_id="department-finance", user_id="someone@insilicomedicine.com")
    ) is True

    # The main rollup already counts this row under what it declared. Adding it again under the
    # subscription's filing would bill the same tokens to two departments.
    assert _rolled_up(repository) == {}


def test_a_half_declared_request_is_completed_rather_than_doubled() -> None:
    repository, processor = _install(department="department-security", owner=OWNER)

    assert processor.process(_event(department_id="department-finance")) is True

    totals = _rolled_up(repository)
    assert ("department", "department-security") not in totals
    assert totals[("user", OWNER)] == 120


def test_the_platforms_own_keys_are_nobodys_spend() -> None:
    repository, processor = _install(
        department="department-platform", owner=None, system_managed=True
    )

    assert processor.process(_event()) is True

    assert _rolled_up(repository) == {}


def test_the_budget_page_reports_it() -> None:
    repository, processor = _install(department="department-security", owner=OWNER)
    assert processor.process(_event()) is True

    overview = TokenBudgetService(repository).overview(PERIOD, include_users=True)
    security = next(
        item for item in overview.items
        if item.scope_type == "department" and item.scope_id == "department-security"
    )
    assert security.used_tokens == 120


def test_the_holder_can_be_given_a_budget() -> None:
    repository, processor = _install(department="department-security", owner=OWNER)
    assert processor.process(_event()) is True

    overview = TokenBudgetService(repository).overview(PERIOD, include_users=True)
    holder = next(
        (item for item in overview.items
         if item.scope_type == "user" and item.scope_id == OWNER),
        None,
    )
    # Attributing through the subscription leaves `token_usage.user_id` as `unattributed`, so
    # the holder never shows up in the roster built from observed traffic. Without admitting
    # them explicitly the page would show their spend under a department and offer no way to
    # cap the person who spent it.
    assert holder is not None, "the subscription's holder is missing from the budget roster"
    assert holder.used_tokens == 120
    assert holder.parent_scope_id == "department-security"
