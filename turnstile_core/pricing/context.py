from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from turnstile_core.domain.models import ModelPrice, ModelPriceTier
from turnstile_core.domain.runtime_models import (
    ContextPriceRule,
    PendingListPrice,
    PriceCatalogInputRange,
    PriceCatalogRuleSource,
    PriceSource,
    StoredContextPriceTier,
    custom_context_rule,
)
from turnstile_core.pricing.catalog import CatalogEntry, CompositeCatalog


def context_price_reference(source: PriceSource, references: Sequence[str]) -> str:
    digest = hashlib.sha256(json.dumps(sorted(references)).encode()).hexdigest()
    return f"{source.value}:context:{digest}"


def model_price_from_tiers(tiers: Sequence[StoredContextPriceTier]) -> ModelPrice:
    return ModelPrice(
        input_price_per_million=0, cached_price_per_million=0,
        cache_write_price_per_million=0, output_price_per_million=0,
        context_tiers=[ModelPriceTier.model_validate(
            tier.model_dump(include=set(ModelPriceTier.model_fields))
        ) for tier in tiers],
    )


def model_price_from_values(values: Mapping[str, Any]) -> ModelPrice | None:
    return ModelPrice.from_registry(values)


def rediscount_context_tiers(
    tiers: Sequence[StoredContextPriceTier], source: PriceSource, discount_percent: float | None,
) -> list[StoredContextPriceTier]:
    """The same tiers charged at another discount, from their accepted list prices."""
    return [tier.model_copy(update=_charged_tier_prices(CatalogEntry(
        reference=tier.reference, label=tier.label, source=source,
        input_per_million=tier.list_prices.input, output_per_million=tier.list_prices.output,
        cached_per_million=tier.list_prices.cached,
        cache_write_per_million=tier.list_prices.cache_write,
    ), discount_percent)) for tier in tiers]


def _charged_tier_prices(entry: CatalogEntry, discount_percent: float | None) -> dict[str, float]:
    charged = entry.discounted(discount_percent)
    assert charged.input_per_million is not None and charged.output_per_million is not None
    cached = charged.cached_per_million
    cached = charged.input_per_million if cached is None else cached
    writes = charged.cache_write_per_million
    return {
        "input_price_per_million": charged.input_per_million,
        "output_price_per_million": charged.output_per_million,
        "cached_price_per_million": cached,
        "cache_write_price_per_million": cached if writes is None else writes,
    }


def resolve_context_prices(
    catalog: CompositeCatalog,
    source: PriceSource,
    references: Sequence[str],
    discount_percent: float | None,
    *,
    rule: ContextPriceRule | None = None,
    previous_tiers: Sequence[StoredContextPriceTier] = (),
) -> list[StoredContextPriceTier]:
    if source is PriceSource.MANUAL or not 2 <= len(references) <= 32:
        raise ValueError("Context pricing requires a complete catalog tier selection")
    if len(set(references)) != len(references):
        raise ValueError("Context tier references must be distinct")
    if discount_percent is not None and not 0 < discount_percent <= 100:
        raise ValueError("Discount must be greater than zero and at most 100")
    configured_source = None
    if rule is not None:
        if len(references) != 2:
            raise ValueError("A custom context rule requires both short and long catalog tiers")
        if (sorted(tier.reference for tier in previous_tiers) == sorted(references)
            and custom_context_rule(list(previous_tiers)) == rule):
            configured_source = previous_tiers[0].input_range_source
        else:
            configured_source = PriceCatalogRuleSource(
                rule_id="custom-context-v1", revision=rule.revision,
                origin="custom", configured_at=datetime.now(UTC),
                url=rule.source_url, note=rule.note or None,
            )
    tiers: list[StoredContextPriceTier] = []
    expected_scope: tuple[str, str, str, str, str] | None = None
    common_regions: set[str] | None = None
    for reference in references:
        parts = reference.split(":")
        if len(parts) != 5 or parts[0] != source.value:
            raise ValueError("Context reference does not match its source")
        found = catalog.options(":".join(parts[:3]))
        if found is None or not found.complete:
            raise ValueError("Complete context prices are unavailable")
        descriptor = found.model.resolved_context_tier
        if descriptor is None:
            raise ValueError("Context tier identity is unavailable")
        input_range = descriptor.input_range
        range_source = descriptor.input_range_source
        if rule is None and range_source is not None and range_source.origin != "official":
            raise ValueError("Catalog context thresholds are unverified; configure a custom rule")
        if rule is not None:
            if descriptor.key not in {"short", "long"}:
                raise ValueError("A custom context rule requires short and long catalog tiers")
            input_range = PriceCatalogInputRange(
                min_input_tokens=0 if descriptor.key == "short"
                else rule.short_context_max_tokens + 1,
                max_input_tokens=rule.short_context_max_tokens
                if descriptor.key == "short" else None,
            )
            range_source = configured_source
        if input_range is None:
            raise ValueError("Verified context price thresholds are unavailable")
        matches = [option for option in found.options if option.reference == reference
                   or reference in option.references_by_region.values()]
        if len(matches) != 1:
            raise ValueError("Context reference is missing or ambiguous")
        option = matches[0]
        entry = option.entry
        if not entry.complete or not entry.priced or found.model.source is not source:
            raise ValueError("Complete context prices are unavailable")
        scope = (
            source.value, found.model.product, descriptor.group_key,
            descriptor.service_tier, option.deployment,
        )
        if expected_scope is not None and scope != expected_scope:
            raise ValueError(
                "Context tiers must share a source, model, service tier and deployment"
            )
        expected_scope = scope
        regions = set(option.regions) if parts[-1] == "*" else {parts[-1]}
        if parts[-1] == "*" and option.region_required or not regions <= set(option.regions):
            raise ValueError("Context reference does not identify a valid region")
        common_regions = regions if common_regions is None else common_regions & regions
        if not common_regions:
            raise ValueError("Context tiers must use the same region")
        tiers.append(StoredContextPriceTier(
            key=descriptor.key, label=descriptor.label, reference=reference,
            min_input_tokens=input_range.min_input_tokens,
            max_input_tokens=input_range.max_input_tokens,
            **_charged_tier_prices(entry, discount_percent),
            list_prices=PendingListPrice(
                input=entry.input_per_million, output=entry.output_per_million,
                cached=entry.cached_per_million, cache_write=entry.cache_write_per_million,
            ),
            input_range_source=range_source,
            model_key=found.model.key, model_label=found.model.label,
            product=found.model.product, group_key=descriptor.group_key,
            service_tier=descriptor.service_tier, deployment=option.deployment,
            regions=sorted(regions),
        ))
    tiers.sort(key=lambda tier: tier.min_input_tokens)
    model_price_from_tiers(tiers)
    return tiers