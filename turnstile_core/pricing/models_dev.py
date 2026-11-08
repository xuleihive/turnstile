from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from turnstile_core.domain.runtime_models import PriceSource
from turnstile_core.pricing.catalog import (
    CATALOG_TTL_SECONDS,
    CatalogEntry,
    CatalogModel,
    CatalogOption,
    CatalogOptions,
)

ENDPOINT = "https://models.dev/api.json"


def rate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


class ModelsDevCatalog:
    source = PriceSource.MODELS_DEV

    def __init__(self, *, fetch: Callable[[], Mapping[str, Any]] | None = None) -> None:
        self._fetch = fetch
        self._entries: dict[str, CatalogEntry] = {}
        self._read_at = 0.0

    def invalidate(self) -> None:
        self._read_at = 0.0

    def refresh(self) -> None:
        self._read_at = 0.0
        if self._fetch is not None:
            document = self._fetch()
        else:
            response = httpx.get(ENDPOINT, timeout=15, follow_redirects=False)
            response.raise_for_status()
            document = response.json()
        if not isinstance(document, Mapping) or not document:
            raise ValueError("Invalid models.dev catalog")
        snapshot = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        fetched_at = datetime.now(UTC).isoformat()
        entries: dict[str, CatalogEntry] = {}
        for provider_id, provider in document.items():
            if not isinstance(provider, dict) or not isinstance(provider.get("models"), dict):
                continue
            for model_id, model in provider["models"].items():
                if not isinstance(model, dict):
                    continue
                cost = model.get("cost") or {}
                modalities = model.get("modalities") or {}
                if not isinstance(cost, dict) or not isinstance(modalities, dict):
                    continue
                output = modalities.get("output")
                image = "gpt-image-" in str(model_id) and output in (
                    ["image"],
                    ["text", "image"],
                )
                if (
                    output != ["text"]
                    and not image
                    or cost.get("tiers")
                    or cost.get("context_over_200k")
                    or model.get("status") == "deprecated"
                    or any(cost.get(key) is not None for key in ("input_audio", "output_audio"))
                    or cost.get("reasoning") not in (None, cost.get("output"))
                ):
                    continue
                reference = (
                    f"models_dev:{quote(str(provider_id), safe='')}:{quote(str(model_id), safe='')}"
                )
                if len(reference) > 255:
                    continue
                entry = CatalogEntry(
                    reference=reference,
                    label=str(model.get("name") or model_id),
                    source=self.source,
                    detail=str(provider.get("name") or provider_id),
                    input_per_million=rate(cost.get("input")),
                    output_per_million=rate(cost.get("output")),
                    cached_per_million=rate(cost.get("cache_read")),
                    cache_write_per_million=None if image else rate(cost.get("cache_write")),
                    complete=all(
                        rate(cost[key]) is not None
                        for key in (
                            "input",
                            "output",
                            "cache_read",
                            "cache_write",
                        )
                        if key in cost
                    ),
                    text_to_image=image,
                    metadata={
                        "url": ENDPOINT,
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "canonical_model_id": model.get("canonical_model_id"),
                        "snapshot_id": snapshot,
                        "fetched_at": fetched_at,
                        "source_updated_at": model.get("last_updated"),
                        "context_window": (
                            None if image else (model.get("limit") or {}).get("context")
                        ),
                    },
                )
                if (
                    entry.priced
                    and entry.complete
                    and (not image or entry.cached_per_million is not None)
                ):
                    entries[reference] = entry
        if not entries:
            raise ValueError("No supported models.dev token prices")
        self._entries = entries
        self._read_at = time.monotonic()

    def entries(self) -> Sequence[CatalogEntry]:
        if not self._read_at or time.monotonic() - self._read_at >= CATALOG_TTL_SECONDS:
            self.refresh()
        return tuple(self._entries.values())

    def models(self) -> Sequence[CatalogModel]:
        return [
            CatalogModel(
                key=entry.reference,
                label=entry.label,
                product=entry.detail or "",
                source=self.source,
                text_to_image=entry.text_to_image,
            )
            for entry in self.entries()
        ]

    def entry(self, reference: str) -> CatalogEntry | None:
        if not reference.startswith("models_dev:"):
            return None
        self.entries()
        return self._entries.get(reference)

    def options(self, model: CatalogModel) -> CatalogOptions:
        entry = self.entry(model.key)
        return CatalogOptions(
            model=model,
            options=(
                CatalogOption(
                    reference=entry.reference,
                    deployment="Public reference",
                    entry=entry,
                    regions=(),
                    region_required=False,
                ),
            )
            if entry
            else (),
            note="Third-party reference price; verify the provider and deployment contract.",
        )
