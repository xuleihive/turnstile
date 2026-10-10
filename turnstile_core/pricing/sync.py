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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from turnstile_core.domain.runtime_models import ManagedModel, PriceSource, PriceSyncStatus
from turnstile_core.pricing.catalog import CatalogEntry, CompositeCatalog

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
    # What the model's pricing looked like when this was planned. The write refuses if any of it
    # has moved since -- otherwise a sync that started before someone switched the model back to
    # manual finishes afterwards and puts its rate on a row that no longer follows a list.
    expected_source: str | None = None
    expected_reference: str | None = None
    expected_discount_percent: float | None = None
    expected_configured: bool = True
    expected_updated_at: Any = None
    expected_price_updated_at: Any = None
    price_source: str | None = None
    price_reference: str | None = None
    match_metadata: dict[str, Any] | None = None
    source_snapshot: dict[str, Any] | None = None


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
    resolved_entries: Mapping[UUID, CatalogEntry] | None = None,
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
        updates.append(_plan_one(
            model, catalog, review_threshold,
            entry=resolved_entries.get(model.id) if resolved_entries else None,
        ))
    return SyncSummary(updates=tuple(updates))


def _planned_against(model: ManagedModel, **fields: Any) -> ModelPriceUpdate:
    """Every decision carries the configuration it was made against, so the write can check it."""
    return ModelPriceUpdate(
        model_id=model.id,
        model_key=model.model_key,
        expected_source=str(model.price_source),
        expected_reference=model.price_reference,
        expected_discount_percent=model.effective_discount_percent,
        expected_configured=model.price_source_configured,
        expected_updated_at=model.updated_at,
        expected_price_updated_at=model.price_config_updated_at,
        price_source=model.price_source.value,
        price_reference=model.price_reference,
        **fields,
    )


def _plan_one(
    model: ManagedModel, catalog: CompositeCatalog, review_threshold: float,
    *, entry: CatalogEntry | None = None,
) -> ModelPriceUpdate:
    reference = model.price_reference or ""
    if reference and reference.partition(":")[0] != model.price_source.value:
        return _planned_against(
            model, status=PriceSyncStatus.UNSUPPORTED,
            message="价格引用与所选来源不一致，保留已有单价",
        )
    try:
        if entry is None:
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
    if entry.unsupported:
        return _planned_against(
            model, status=PriceSyncStatus.UNSUPPORTED, message=entry.unsupported,
        )
    operation = "image_generation" if "image_generation" in model.capabilities else "chat"
    if entry.operation != operation:
        return _planned_against(
            model, status=PriceSyncStatus.UNSUPPORTED,
            message="目录价格用途与模型不一致，保留已有单价",
        )
    if operation == "image_generation" and entry.cached_per_million is None \
            and model.cached_cost_per_million is None:
        return _planned_against(
            model, status=PriceSyncStatus.UNMAPPED,
            message="目录缺少缓存文字单价，请使用手动定价",
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

    moved = _largest_drift(model, entry)
    if moved is not None and moved[1] > review_threshold:
        bucket, drift = moved
        published = "公开" if model.price_source is PriceSource.MODELS_DEV else "官方"
        return _planned_against(
            model,
            status=PriceSyncStatus.REVIEW_NEEDED,
            message=(
                f"{published}{bucket}较上次接受的价格变动 {drift:.0%}，"
                f"超过 {review_threshold:.0%} 复核阈值，未自动写入"
            ) if drift != float("inf") else "价格在免费与收费之间变化，待复核，未自动写入",
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
        cached_cost_per_million=charged.cached_per_million
        if charged.cached_per_million is not None else model.cached_cost_per_million,
        cache_write_cost_per_million=charged.cache_write_per_million
        if charged.cache_write_per_million is not None else model.cache_write_cost_per_million,
        list_input_cost_per_million=entry.input_per_million,
        list_output_cost_per_million=entry.output_per_million,
        list_cached_cost_per_million=entry.cached_per_million,
        list_cache_write_cost_per_million=entry.cache_write_per_million,
    )


def _largest_drift(model: ManagedModel, entry: CatalogEntry) -> tuple[str, float] | None:
    """The bucket that moved furthest since the last accepted price, or None on the first sync.

    Measured against the stored list price rather than the charged rate, so changing a discount
    never looks like a suspicious price move. Every bucket is checked: an output rate tripling
    while the input rate holds is exactly the move a person should see, and checking only the
    input rate let it through.
    """
    worst: tuple[str, float] | None = None
    for field, baseline_field, label in _BUCKETS:
        previous = getattr(model, baseline_field)
        current = getattr(entry, field)
        if previous is None and model.price_source is PriceSource.MODELS_DEV:
            actual_field = baseline_field.removeprefix("list_")
            previous = getattr(model, actual_field)
            current = getattr(entry.discounted(model.effective_discount_percent), field)
        if previous is None or current is None or previous == current:
            continue
        if previous == 0 or current == 0:
            return label, float("inf")
        drift = abs(current - previous) / previous
        if worst is None or drift > worst[1]:
            worst = (label, drift)
    return worst
