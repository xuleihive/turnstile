"""Export the same effective token prices used by platform accounting."""

import math

from turnstile_core.domain.models import ModelPrice


def plugin_model_cost(price: ModelPrice | None) -> dict[str, float] | None:
    if price is None:
        return None
    cost = {
        "input": price.input_price_per_million,
        "output": price.output_price_per_million,
        "cache_read": price.cached_price_per_million,
        "cache_write": price.cache_write_price_per_million,
    }
    return cost if all(math.isfinite(value) and value >= 0 for value in cost.values()) else None
