"""What the sync decides, and what it refuses to decide.

A wrong rate here is silent -- the dashboard renders, the bill adds up, and nobody notices for
weeks. So most of these cases are about *not* writing.

The last two are contract tests: the in-memory repository stands in for PostgreSQL in every
other test in this suite, so wherever the two disagree, the suite agrees with the fake.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from turnstile_core.domain.runtime_models import ManagedModel, PriceSource, PriceSyncStatus
from turnstile_core.persistence.in_memory_registry import InMemoryRegistryRepositoryMixin
from turnstile_core.pricing.catalog import CatalogEntry, CompositeCatalog
from turnstile_core.pricing.sync import plan_price_sync

REFERENCE = "azure_retail:Azure OpenAI:gpt 4.1 mini:Global:*"


class StubCatalog(CompositeCatalog):
    """Answers one reference, and can be told to fail the way a network source fails."""

    def __init__(self, entry: CatalogEntry | None, *, raises: bool = False) -> None:
        self._entry = entry
        self._raises = raises
        self.lookups: list[str] = []

    def lookup(self, reference: str) -> CatalogEntry | None:
        self.lookups.append(reference)
        if self._raises:
            raise TimeoutError("price API unreachable")
        return self._entry if reference == REFERENCE else None


def entry(
    input_per_million: float = 0.40,
    output_per_million: float = 1.60,
    cached_per_million: float | None = 0.10,
) -> CatalogEntry:
    return CatalogEntry(
        reference=REFERENCE,
        label="gpt 4.1 mini",
        source=PriceSource.AZURE_RETAIL,
        input_per_million=input_per_million,
        output_per_million=output_per_million,
        cached_per_million=cached_per_million,
        cache_write_per_million=None,
    )


def model(**overrides: Any) -> ManagedModel:
    base: dict[str, Any] = {
        "id": uuid4(),
        "provider_id": uuid4(),
        "runtime_id": uuid4(),
        "model_key": "gpt-4.1-mini",
        "display_name": "gpt 4.1 mini",
        "provider_name": "Foundry",
        "runtime_name": "connection",
        "family_key": "generic",
        "upstream_model_id": "gpt-4.1-mini",
        "price_source": PriceSource.AZURE_RETAIL,
        "price_reference": REFERENCE,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    return ManagedModel.model_validate(base | overrides)


def only(summary: Any) -> Any:
    assert len(summary.updates) == 1, summary.updates
    return summary.updates[0]


# ------------------------------------------------------------------------------------------
# The arithmetic
# ------------------------------------------------------------------------------------------


def test_the_charged_rate_is_the_list_price_times_the_discount() -> None:
    update = only(plan_price_sync([model(effective_discount_percent=90)], StubCatalog(entry())))
    assert update.status is PriceSyncStatus.OK
    assert update.writes is True
    assert update.list_input_cost_per_million == pytest.approx(0.40)
    assert update.input_cost_per_million == pytest.approx(0.36)
    assert update.output_cost_per_million == pytest.approx(1.44)
    assert update.cached_cost_per_million == pytest.approx(0.09)


def test_no_discount_charges_the_list_price() -> None:
    update = only(plan_price_sync([model()], StubCatalog(entry())))
    assert update.input_cost_per_million == pytest.approx(0.40)


def test_a_bucket_the_vendor_does_not_publish_stays_empty() -> None:
    """Not zero. Zero is a price, and it would quietly stop charging for cache writes."""
    update = only(
        plan_price_sync(
            [model(effective_discount_percent=66)],
            StubCatalog(entry(cached_per_million=None)),
        )
    )
    assert update.cached_cost_per_million is None
    assert update.cache_write_cost_per_million is None


# ------------------------------------------------------------------------------------------
# The refusals
# ------------------------------------------------------------------------------------------


def test_a_model_priced_by_hand_is_not_touched_at_all() -> None:
    """The contract that lets this ship without disturbing anyone's existing rates."""
    catalog = StubCatalog(entry())
    summary = plan_price_sync(
        [model(price_source=PriceSource.MANUAL, price_reference=None)], catalog
    )
    assert summary.updates == ()
    assert catalog.lookups == [], "a manual model should not even be looked up"


def test_a_reference_that_resolves_to_nothing_keeps_the_current_rates() -> None:
    update = only(plan_price_sync([model(price_reference="azure_retail:gone:x:Global:*")],
                                  StubCatalog(entry())))
    assert update.status is PriceSyncStatus.UNMAPPED
    assert update.writes is False
    assert update.input_cost_per_million is None


def test_a_source_that_cannot_be_read_keeps_the_current_rates() -> None:
    update = only(plan_price_sync([model()], StubCatalog(entry(), raises=True)))
    assert update.status is PriceSyncStatus.STALE
    assert update.writes is False


def test_an_entry_missing_half_its_price_is_not_used() -> None:
    update = only(plan_price_sync([model()], StubCatalog(entry(output_per_million=None))))  # type: ignore[arg-type]
    assert update.status is PriceSyncStatus.UNMAPPED
    assert update.writes is False


def test_a_list_price_that_jumped_waits_for_a_person() -> None:
    """A parser reading the wrong column looks exactly like a repricing. Stop for both."""
    update = only(
        plan_price_sync(
            [model(list_input_cost_per_million=0.40, effective_discount_percent=90)],
            StubCatalog(entry(input_per_million=4.0)),
        )
    )
    assert update.status is PriceSyncStatus.REVIEW_NEEDED
    assert update.writes is False
    assert update.input_cost_per_million is None
    assert update.pending_list_price == {
        "input": 4.0, "output": 1.60, "cached": 0.10, "cache_write": None,
    }, "the figure is still reported, so a person can see what it wants to become"
    assert update.list_input_cost_per_million is None, (
        "but it must not reach the accepted baseline, or the next run compares it with itself"
    )


def test_a_price_held_for_review_is_still_held_on_the_next_run() -> None:
    """The first version marked the jump, then moved the baseline to the unapproved figure.

    The second run then measured 4 against 4, found no drift, and charged the discounted price
    that nobody had approved. A threshold that approves itself on the second attempt is not a
    threshold, so the baseline only moves when a run actually accepts the price.
    """
    catalog = StubCatalog(entry(input_per_million=4.0))
    priced = model(list_input_cost_per_million=0.40, effective_discount_percent=90)

    first = only(plan_price_sync([priced], catalog))
    assert first.status is PriceSyncStatus.REVIEW_NEEDED

    # The registry after the first run: baseline untouched, proposal recorded.
    after_first = model(
        id=priced.id,
        list_input_cost_per_million=first.list_input_cost_per_million or 0.40,
        effective_discount_percent=90,
    )
    second = only(plan_price_sync([after_first], catalog))
    assert second.status is PriceSyncStatus.REVIEW_NEEDED, (
        "the same unapproved price must stop the second run exactly as it stopped the first"
    )
    assert second.writes is False


@pytest.mark.parametrize(
    ("moved", "label"),
    [
        ({"output_per_million": 16.0}, "输出价"),
        ({"cached_per_million": 1.0}, "缓存读取价"),
    ],
)
def test_any_bucket_moving_far_enough_waits_for_a_person(
    moved: dict[str, float], label: str
) -> None:
    """Checking only the input rate let an output price triple with nobody asked."""
    update = only(
        plan_price_sync(
            [model(list_input_cost_per_million=0.40, list_output_cost_per_million=1.60,
                   list_cached_cost_per_million=0.10, effective_discount_percent=90)],
            StubCatalog(entry(**moved)),
        )
    )
    assert update.status is PriceSyncStatus.REVIEW_NEEDED
    assert update.writes is False
    assert label in (update.message or "")


def test_a_source_read_only_in_part_keeps_the_rates_it_cannot_confirm() -> None:
    """Page 2 of the price feed fails. Page 1 carried input and output; cached lived on page 2.

    Writing what arrived would set the cached rate to NULL -- indistinguishable from a vendor
    that does not publish one -- and the billing path would silently start charging cached
    tokens at the full input rate.
    """
    partial = entry(cached_per_million=None)
    update = only(
        plan_price_sync(
            [model(cached_cost_per_million=0.45, effective_discount_percent=90)],
            StubCatalog(replace(partial, complete=False)),
        )
    )
    assert update.status is PriceSyncStatus.STALE
    assert update.writes is False
    assert update.cached_cost_per_million is None
    assert "只读到一部分" in (update.message or "")


def test_an_ordinary_repricing_passes_unattended() -> None:
    update = only(
        plan_price_sync(
            [model(list_input_cost_per_million=0.40, effective_discount_percent=90)],
            StubCatalog(entry(input_per_million=0.44)),
        )
    )
    assert update.status is PriceSyncStatus.OK
    assert update.input_cost_per_million == pytest.approx(0.396)


def test_drift_is_measured_against_the_list_price_not_the_charged_rate() -> None:
    """Otherwise changing a discount would read as a suspicious price move and block itself."""
    update = only(
        plan_price_sync(
            [model(list_input_cost_per_million=0.40, input_cost_per_million=0.36,
                   effective_discount_percent=20)],
            StubCatalog(entry(input_per_million=0.40)),
        )
    )
    assert update.status is PriceSyncStatus.OK
    assert update.input_cost_per_million == pytest.approx(0.08)


def test_only_narrows_the_run_to_the_models_asked_for() -> None:
    wanted, other = model(), model()
    summary = plan_price_sync([wanted, other], StubCatalog(entry()), only=[wanted.id])
    assert [update.model_id for update in summary.updates] == [wanted.id]


# ------------------------------------------------------------------------------------------
# Repository contract: the fake has to answer the same question as the database
# ------------------------------------------------------------------------------------------


class Registry(InMemoryRegistryRepositoryMixin):
    def __init__(self, runtimes: list[dict[str, Any]], models: list[dict[str, Any]]) -> None:
        self.gateways = []
        self.providers = []
        self.runtimes = runtimes
        self.models = models
        self.gateway_publications = []
        self.effective_gateway_releases = {}


def registry_with(model_discount: float | None, runtime_discount: float | None) -> Registry:
    runtime_id = uuid4()
    return Registry(
        runtimes=[{"id": runtime_id, "provider_id": uuid4(),
                   "price_discount_percent": runtime_discount}],
        models=[{"id": uuid4(), "runtime_id": runtime_id, "model_key": "m",
                 "price_source": "azure_retail", "price_reference": REFERENCE,
                 "price_discount_percent": model_discount}],
    )


def sync_result(model_id: UUID, **overrides: Any) -> dict[str, Any]:
    """A planned write, carrying the configuration it was planned against."""
    planned: dict[str, Any] = {
        "model_id": model_id, "writes": True, "status": "ok", "message": None,
        "input_cost_per_million": 0.36, "output_cost_per_million": 1.44,
        "cached_cost_per_million": 0.09, "cache_write_cost_per_million": None,
        "list_input_cost_per_million": 0.40, "list_output_cost_per_million": 1.60,
        "list_cached_cost_per_million": 0.10, "list_cache_write_cost_per_million": None,
        "pending_list_price": None,
        "expected_source": "azure_retail", "expected_reference": REFERENCE,
        "expected_discount_percent": 90,
    }
    planned.update(overrides)
    return planned


@pytest.mark.parametrize(
    ("model_discount", "runtime_discount", "expected"),
    [
        (None, 90, 90),   # inherited from the connection -- the usual case
        (66, 90, 66),     # the model overrides its connection
        (None, None, None),  # nobody set one: list price
        (66, None, 66),
    ],
)
def test_the_fake_resolves_the_discount_the_way_the_query_does(
    model_discount: float | None, runtime_discount: float | None, expected: float | None
) -> None:
    registry = registry_with(model_discount, runtime_discount)
    assert registry.registry()["models"][0]["effective_discount_percent"] == expected


def test_a_sync_result_lands_on_the_model_the_fake_reports() -> None:
    registry = registry_with(None, 90)
    model_id: UUID = registry.models[0]["id"]
    written = registry.apply_model_price_sync([sync_result(model_id)])
    assert written == 1
    stored = registry.registry()["models"][0]
    assert stored["input_cost_per_million"] == pytest.approx(0.36)
    assert stored["list_input_cost_per_million"] == pytest.approx(0.40)
    assert stored["price_sync_status"] == "ok"


@pytest.mark.parametrize(
    ("edit", "what_changed"),
    [
        ({"price_source": "manual", "price_reference": None, "input_cost_per_million": 7.0},
         "切回手工并自己填了单价"),
        ({"price_reference": "azure_retail:Azure OpenAI:gpt 4.1:Global:*"},
         "改指向了另一个价目条目"),
        ({"price_discount_percent": 50}, "改了这个模型的折扣"),
    ],
)
def test_a_result_planned_before_an_edit_does_not_land_after_it(
    edit: dict[str, Any], what_changed: str
) -> None:
    """The sync reads every model, talks to a price feed over the network, then writes.

    Someone opening the model in that window and saving is not a race anyone can see: the write
    lands on a row that no longer means what the plan assumed, and the rate it leaves behind is
    one nobody chose. `manual` with a synced rate on it is the worst version -- the screen says
    the number was typed by a person.
    """
    registry = registry_with(None, 90)
    model = registry.models[0]
    model["input_cost_per_million"] = 7.0
    planned = sync_result(model["id"])

    model.update(edit)  # the person saves while the sync is still out on the network

    written = registry.apply_model_price_sync([planned])

    assert written == 0, what_changed
    stored = registry.registry()["models"][0]
    assert stored["input_cost_per_million"] == pytest.approx(
        edit.get("input_cost_per_million", 7.0)
    ), "the rate the person is looking at has to survive"
    assert stored.get("price_sync_status") is None, (
        "do not stamp an old result on new configuration"
    )


def test_an_unchanged_configuration_still_lets_the_write_through() -> None:
    """The guard has to be narrow enough to be invisible in the normal case."""
    registry = registry_with(None, 90)
    written = registry.apply_model_price_sync([sync_result(registry.models[0]["id"])])
    assert written == 1


def test_accepting_a_price_clears_the_proposal_waiting_on_it() -> None:
    registry = registry_with(None, 90)
    model = registry.models[0]
    model["pending_list_price"] = {"input": 4.0, "output": 16.0,
                                   "cached": None, "cache_write": None}
    registry.apply_model_price_sync([sync_result(model["id"])])
    assert registry.registry()["models"][0]["pending_list_price"] is None


def test_a_run_that_could_not_read_the_source_leaves_the_proposal_standing() -> None:
    """Forgetting what is waiting for review because the next run timed out loses the ask."""
    registry = registry_with(None, 90)
    model = registry.models[0]
    model["pending_list_price"] = {"input": 4.0, "output": 16.0,
                                   "cached": None, "cache_write": None}
    registry.apply_model_price_sync([
        sync_result(model["id"], writes=False, status="stale", message="读不到",
                    pending_list_price=None),
    ])
    stored = registry.registry()["models"][0]
    assert stored["pending_list_price"] == {"input": 4.0, "output": 16.0,
                                            "cached": None, "cache_write": None}


def test_a_skipped_sync_records_the_reason_without_moving_the_rates() -> None:
    registry = registry_with(None, 90)
    registry.models[0]["input_cost_per_million"] = 1.2345
    registry.apply_model_price_sync([
        {"model_id": registry.models[0]["id"], "writes": False,
         "status": "unmapped", "message": "找不到",
         "list_input_cost_per_million": None, "list_output_cost_per_million": None,
         "list_cached_cost_per_million": None, "list_cache_write_cost_per_million": None},
    ])
    stored = registry.registry()["models"][0]
    assert stored["input_cost_per_million"] == pytest.approx(1.2345)
    assert stored["price_sync_status"] == "unmapped"
    assert stored["price_sync_message"] == "找不到"
