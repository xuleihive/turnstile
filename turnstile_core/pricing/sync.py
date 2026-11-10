"""Turn published list prices into the rates the registry charges.

The arithmetic is trivial -- list price times discount. Everything interesting here is about
refusing to write a number when there is any doubt about it, because a wrong rate is silent: the
dashboard still renders, the bill still adds up, and nobody notices until someone reconciles it
against an invoice weeks later.

So: a model whose mapping resolves to nothing is marked `unmapped` and keeps its current rates. A
source that cannot be read, or could only be read in part, leaves the rates alone and marks the
row `stale`. A list price that moved further than the review threshold is reported as
`review_needed` and, again, does not write -- and does not move the baseline it will be compared
against next time, or the second run would approve the price by comparing it with itself. Only an
unambiguous, small-enough change writes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from turnstile_core.domain.runtime_models import (
    ManagedModel,
    PendingListPrice,
    PriceSource,
    PriceSyncChangeReason,
    PriceSyncStatus,
    StoredContextPriceTier,
)
from turnstile_core.pricing.catalog import CatalogEntry, CompositeCatalog, parse_context_tier
from turnstile_core.pricing.context import rediscount_context_tiers, resolve_context_prices

# A published rate rarely moves more than a few percent. Twenty is generous enough that ordinary
# repricing passes unattended, and tight enough that a parser reading the wrong column stops.
DEFAULT_REVIEW_THRESHOLD = 0.20

# Every bucket the registry charges for, paired with the accepted baseline it is measured against.
# Checking only the input rate let an output price triple with nobody asked.
_BUCKETS = (
    ("input_per_million", "list_input_cost_per_million", "输入价"),
    ("output_per_million", "list_output_cost_per_million", "输出价"),
    ("cached_per_million", "list_cached_cost_per_million", "缓存读取价"),
    ("cache_write_per_million", "list_cache_write_cost_per_million", "缓存写入价"),
)


@dataclass(frozen=True)
class ModelPriceUpdate:
    """What the sync decided for one model. `writes` is False when only the status changes."""

    model_id: UUID
    model_key: str
    status: PriceSyncStatus
    message: str | None = None
    writes: bool = False
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    cached_cost_per_million: float | None = None
    cache_write_cost_per_million: float | None = None
    list_input_cost_per_million: float | None = None
    list_output_cost_per_million: float | None = None
    list_cached_cost_per_million: float | None = None
    list_cache_write_cost_per_million: float | None = None
    # The published price a person still has to accept. Kept apart from the list_* baseline on
    # purpose: writing it there would make the next run compare the figure against itself.
    pending_list_price: dict[str, float | None] | None = None
    context_price_tiers: list[dict[str, Any]] | None = None
    pending_context_price_tiers: list[dict[str, Any]] | None = None
    expected_context_price_tiers: list[dict[str, Any]] | None = None
    # What the model's pricing looked like when this was planned. The write refuses if any of it
    # has moved since -- otherwise a sync that started before someone switched the model back to
    # manual finishes afterwards and puts its rate on a row that no longer follows a list.
    expected_source: str | None = None
    expected_reference: str | None = None
    expected_discount_percent: float | None = None
    expected_list_prices: dict[str, float | None] | None = None
    price_source_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class SyncSummary:
    updates: tuple[ModelPriceUpdate, ...]

    @property
    def written(self) -> int:
        return sum(1 for update in self.updates if update.writes)

    @property
    def unmapped(self) -> int:
        return sum(1 for u in self.updates if u.status is PriceSyncStatus.UNMAPPED)

    @property
    def review_needed(self) -> int:
        return sum(1 for u in self.updates if u.status is PriceSyncStatus.REVIEW_NEEDED)

    @property
    def stale(self) -> int:
        return sum(1 for u in self.updates if u.status is PriceSyncStatus.STALE)


def plan_price_sync(
    models: Iterable[ManagedModel],
    catalog: CompositeCatalog,
    *,
    only: Sequence[UUID] | None = None,
    review_threshold: float = DEFAULT_REVIEW_THRESHOLD,
) -> SyncSummary:
    selected = set(only) if only is not None else None
    updates: list[ModelPriceUpdate] = []
    for model in models:
        if selected is not None and model.id not in selected:
            continue
        # A model priced by hand is left exactly as it is. That is the contract that lets this
        # feature ship without touching anyone's existing rates.
        if model.price_source is PriceSource.MANUAL:
            continue
        updates.append(_plan_one(model, catalog, review_threshold))
    return SyncSummary(updates=tuple(updates))


def _context_rules(
    tiers: Sequence[StoredContextPriceTier],
) -> dict[str, tuple[int, int | None, str, str | None]]:
    return {tier.key: (
        tier.min_input_tokens, tier.max_input_tokens, tier.reference,
        tier.input_range_source.origin if tier.input_range_source else None,
    ) for tier in tiers}


def price_sync_changes(
    model: ManagedModel, proposed: PendingListPrice | None,
    proposed_tiers: Sequence[StoredContextPriceTier] | None,
) -> tuple[list[PriceSyncChangeReason], bool | None]:
    reasons: list[PriceSyncChangeReason] = []
    if model.context_price_tiers:
        if proposed_tiers is None:
            return reasons, None
        previous = {tier.key: tier for tier in model.context_price_tiers}
        latest = {tier.key: tier for tier in proposed_tiers}
        baseline_known = all(
            tier.list_prices.input is not None and tier.list_prices.output is not None
            for tier in previous.values()
        )
        complete = set(previous) == set(latest) and baseline_known
        changed = any(
            previous[key].list_prices != latest[key].list_prices
            for key in previous.keys() & latest.keys()
            if previous[key].list_prices.input is not None
            and previous[key].list_prices.output is not None
        )
        catalog_changed = True if changed else False if complete else None
        if _context_rules(model.context_price_tiers) != _context_rules(proposed_tiers):
            reasons.append("pricing_rule_changed")
    else:
        if proposed is None:
            return reasons, None
        accepted = PendingListPrice(**{
            bucket: getattr(model, column)
            for bucket, (_, column, _) in zip(
                ("input", "output", "cached", "cache_write"), _BUCKETS, strict=True,
            )
        })
        baseline_known = accepted.input is not None and accepted.output is not None
        catalog_changed = accepted != proposed if baseline_known else None
    if catalog_changed:
        reasons.insert(0, "catalog_price_changed")
    pending_discount = repriced_at_discount(model) if baseline_known else None
    if pending_discount is not None:
        reasons.append("discount_pending")
    return reasons, catalog_changed


def _planned_against(model: ManagedModel, **fields: Any) -> ModelPriceUpdate:
    """Every decision carries the configuration it was made against, so the write can check it."""
    return ModelPriceUpdate(
        model_id=model.id,
        model_key=model.model_key,
        expected_source=str(model.price_source),
        expected_reference=model.price_reference,
        expected_discount_percent=model.effective_discount_percent,
        expected_list_prices={column: getattr(model, column) for _, column, _ in _BUCKETS},
        expected_context_price_tiers=[
            tier.model_dump(mode="json") for tier in model.context_price_tiers
        ],
        **fields,
    )


def _plan_one(
    model: ManagedModel, catalog: CompositeCatalog, review_threshold: float
) -> ModelPriceUpdate:
    if model.context_price_tiers:
        return _plan_context(model, catalog, review_threshold)
    reference = model.price_reference or ""
    try:
        entry = catalog.lookup(reference)
    except Exception as error:  # noqa: BLE001 - any source failure means "keep what we have"
        return _planned_against(
            model,
            status=PriceSyncStatus.STALE,
            message=f"价目表读取失败，保留现有单价：{type(error).__name__}",
        )
    if entry is None:
        return _planned_against(
            model,
            status=PriceSyncStatus.UNMAPPED,
            message=f"价目表中找不到 {reference}，保留现有单价",
        )
    if not entry.priced:
        return _planned_against(
            model,
            status=PriceSyncStatus.UNMAPPED,
            message=f"{entry.label} 未同时发布输入与输出价，保留现有单价",
        )
    if not entry.complete:
        # Buckets are missing because a page of the feed failed, not because the vendor stopped
        # publishing them. Writing would blank whichever rates happened to live on that page.
        return _planned_against(
            model,
            status=PriceSyncStatus.STALE,
            message=f"{entry.label} 的价目只读到一部分（分页中断），保留现有单价",
        )

    if entry.context_tier is not None or parse_context_tier(entry.label) is not None:
        return _planned_against(
            model, status=PriceSyncStatus.REVIEW_NEEDED,
            message="价目包含上下文档位，需选择并确认完整分档规则，未按单档覆盖现有价格。",
        )

    if entry.text_to_image != ("image_generation" in model.capabilities):
        return _planned_against(
            model, status=PriceSyncStatus.STALE,
            message="目录报价用途与模型不一致。",
        )

    moved = _largest_drift(model, entry)
    if moved is not None and moved[1] > review_threshold:
        bucket, drift = moved
        return _planned_against(
            model,
            status=PriceSyncStatus.REVIEW_NEEDED,
            message=(
                f"目录{bucket}较上次接受的价格变动 {drift:.0%}，"
                f"超过 {review_threshold:.0%} 复核阈值，未自动写入"
            ),
            pending_list_price={
                "input": entry.input_per_million,
                "output": entry.output_per_million,
                "cached": entry.cached_per_million,
                "cache_write": entry.cache_write_per_million,
            },
        )

    charged = entry.discounted(model.effective_discount_percent)
    return _planned_against(
        model,
        status=PriceSyncStatus.OK,
        writes=True,
        message=None,
        input_cost_per_million=charged.input_per_million,
        output_cost_per_million=charged.output_per_million,
        cached_cost_per_million=charged.cached_per_million,
        cache_write_cost_per_million=charged.cache_write_per_million,
        list_input_cost_per_million=entry.input_per_million,
        list_output_cost_per_million=entry.output_per_million,
        list_cached_cost_per_million=entry.cached_per_million,
        list_cache_write_cost_per_million=entry.cache_write_per_million,
        price_source_snapshot=entry.provenance,
    )


def _plan_context(
    model: ManagedModel, catalog: CompositeCatalog, review_threshold: float,
) -> ModelPriceUpdate:
    try:
        updated = resolve_context_prices(
            catalog, model.price_source, model.context_price_references,
            model.effective_discount_percent,
            rule=model.context_price_rule, previous_tiers=model.context_price_tiers,
        )
    except Exception as error:
        return _planned_against(
            model, status=PriceSyncStatus.STALE,
            message=f"分档价目读取失败，保留已接受的完整规则：{type(error).__name__}",
        )
    previous = {tier.key: tier for tier in model.context_price_tiers}
    changed_rules = _context_rules(updated) != _context_rules(model.context_price_tiers)
    drift = 0.0
    for tier in updated:
        old = previous.get(tier.key)
        if old is None:
            continue
        for bucket in ("input", "output", "cached", "cache_write"):
            baseline = getattr(old.list_prices, bucket)
            current = getattr(tier.list_prices, bucket)
            if baseline != current and (baseline is None or current is None or baseline == 0):
                drift = float("inf")
            elif baseline is not None and current is not None and baseline > 0:
                drift = max(drift, abs(current - baseline) / baseline)
    payload = [tier.model_dump(mode="json") for tier in updated]
    if changed_rules or drift > review_threshold:
        return _planned_against(
            model, status=PriceSyncStatus.REVIEW_NEEDED,
            message="分档计价规则或目录价发生较大变化，需确认后才能采用。",
            pending_context_price_tiers=payload,
        )
    return _planned_against(
        model, status=PriceSyncStatus.OK, writes=True, context_price_tiers=payload,
    )


def repriced_at_discount(model: ManagedModel) -> dict[str, Any] | None:
    """The charged rates of a model's accepted list prices at its effective discount.

    A discount change is not a price move: no catalog read, no newly published price adopted.
    None when the model has no accepted list price or already charges these rates.
    """
    if model.price_source is PriceSource.MANUAL:
        return None
    discount = model.effective_discount_percent
    if model.context_price_tiers:
        tiers = rediscount_context_tiers(model.context_price_tiers, model.price_source, discount)
        if tiers == model.context_price_tiers:
            return None
        return {"context_price_tiers": [tier.model_dump(mode="json") for tier in tiers]}
    if model.list_input_cost_per_million is None or model.list_output_cost_per_million is None:
        return None
    charged = CatalogEntry(
        reference=model.price_reference or "", label=model.model_key, source=model.price_source,
        **{bucket: getattr(model, column) for bucket, column, _ in _BUCKETS},
    ).discounted(discount)
    values = {
        column.removeprefix("list_"): getattr(charged, bucket) for bucket, column, _ in _BUCKETS
    }
    if all(getattr(model, column) == value for column, value in values.items()):
        return None
    return values


def _largest_drift(model: ManagedModel, entry: CatalogEntry) -> tuple[str, float] | None:
    """The bucket that moved furthest since the last accepted price, or None on the first sync.

    Measured against the stored list price rather than the charged rate, so changing a discount
    never looks like a suspicious price move. Every bucket is checked: an output rate tripling
    while the input rate holds is exactly the move a person should see, and checking only the
    input rate let it through.
    """
    worst: tuple[str, float] | None = None
    accepted = (
        model.list_input_cost_per_million is not None
        and model.list_output_cost_per_million is not None
    )
    for field, baseline_field, label in _BUCKETS:
        previous = getattr(model, baseline_field)
        current = getattr(entry, field)
        if previous == current:
            continue
        if previous is None:
            if not accepted:
                continue
            drift = float("inf")
        elif current is None or previous == 0:
            drift = float("inf")
        else:
            drift = abs(current - previous) / previous
        if worst is None or drift > worst[1]:
            worst = (label, drift)
    return worst
