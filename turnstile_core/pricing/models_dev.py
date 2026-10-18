"""Bounded, provider-specific snapshots of the public models.dev catalog."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote

import httpx

from turnstile_core.domain.runtime_models import PriceSource
from turnstile_core.pricing.catalog import (
    CATALOG_TTL_SECONDS,
    CatalogEntry,
    CatalogModel,
    CatalogOption,
    CatalogOptions,
    price_request_timeout,
)

ENDPOINT = "https://models.dev/api.json"
MAX_BYTES = 32 * 1024 * 1024


def reference_for(provider: str, model: str) -> str:
    return f"models_dev:{quote(provider, safe='')}:{quote(model, safe='')}"


def parse_reference(reference: str) -> tuple[str, str] | None:
    parts = reference.split(":")
    if len(parts) != 3 or parts[0] != "models_dev" or not all(parts[1:]):
        return None
    return unquote(parts[1]), unquote(parts[2])


def _rate(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value < 10**10 else None


class ModelsDevCatalog:
    source = PriceSource.MODELS_DEV

    def __init__(
        self,
        *,
        fetch: Callable[[], Mapping[str, Any]] | None = None,
        ttl_seconds: int = CATALOG_TTL_SECONDS,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._read_at = 0.0
        self._entries: dict[str, CatalogEntry] | None = None
        self._etag: str | None = None
        self._last_modified: str | None = None
        self._pending_headers: tuple[str | None, str | None] = (None, None)
        self._snapshot = ""

    def _download(self) -> Mapping[str, Any] | None:
        headers = {"User-Agent": "turnstile-price-sync", "Accept": "application/json"}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified
        with httpx.stream(
            "GET",
            ENDPOINT,
            headers=headers,
            timeout=price_request_timeout(15),
            follow_redirects=False,
        ) as response:
            if response.status_code == 304 and self._entries is not None:
                return None
            response.raise_for_status()
            payload = bytearray()
            for chunk in response.iter_bytes():
                price_request_timeout(15)
                payload.extend(chunk)
                if len(payload) > MAX_BYTES:
                    raise ValueError("Public catalog exceeds response size limit")
            document = json.loads(payload)
            if not isinstance(document, dict) or not document:
                raise ValueError("Invalid public catalog")
            self._pending_headers = (
                response.headers.get("ETag"), response.headers.get("Last-Modified"),
            )
            return document

    def refresh(self) -> None:
        with self._lock:
            self._read_at = float("-inf")
            document = self._fetch() if self._fetch else self._download()
            fetched_at = datetime.now(UTC).isoformat()
            if document is None:
                assert self._entries is not None
                from dataclasses import replace

                self._entries = {
                    key: replace(entry, metadata={**entry.metadata, "fetched_at": fetched_at})
                    for key, entry in self._entries.items()
                }
                self._read_at = time.monotonic()
                return
            if not isinstance(document, Mapping) or not document:
                raise ValueError("Invalid public catalog")
            snapshot = hashlib.sha256(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            entries: dict[str, CatalogEntry] = {}
            for provider_id, provider in document.items():
                if not isinstance(provider, dict) or not isinstance(provider.get("models"), dict):
                    continue
                for model_id, model in provider["models"].items():
                    if not isinstance(model, dict) or not isinstance(model.get("name"), str):
                        continue
                    if len(str(model_id)) > 500 or len(str(provider_id)) > 200:
                        continue
                    cost = model.get("cost") or {}
                    limit = model.get("limit") or {}
                    if not isinstance(cost, dict) or not isinstance(limit, dict):
                        continue
                    modalities = model.get("modalities") or {}
                    if not isinstance(modalities, dict):
                        continue
                    output = modalities.get("output")
                    # GPT Image publishes text input/cache and image output in USD/1M tokens.
                    # Other image families may put text-output or per-image rates in cost.output.
                    image_tokens = (
                        isinstance(output, list)
                        and "image" in output
                        and all(value in ("text", "image") for value in output)
                        and any(
                            re.search(r"(?:^|/)gpt-image-\d", identity)
                            for identity in (
                                str(model_id), str(model.get("canonical_model_id") or ""),
                            )
                        )
                    )
                    unsupported = None
                    if cost.get("tiers") or cost.get("context_over_200k"):
                        unsupported = "阶梯价格不能用当前四项单价表示"
                    elif output != ["text"] and not image_tokens:
                        unsupported = "该条目的输出用途或计费单位无法用于当前 Token 单价"
                    elif model.get("status") == "deprecated":
                        unsupported = "目录条目已废弃，请人工确认价格来源"
                    elif any(cost.get(key) is not None for key in ("input_audio", "output_audio")):
                        unsupported = "存在当前计费合同无法表达的独立音频价格"
                    elif cost.get("reasoning") not in (None, cost.get("output")):
                        unsupported = "独立推理价格不能用当前四项单价表示"
                    context = limit.get("context")
                    context = context if (type(context) is int and 0 < context < 2**31) else None
                    entry_digest = hashlib.sha256(
                        json.dumps(
                            {
                                key: model.get(key)
                                for key in (
                                    "id",
                                    "name",
                                    "canonical_model_id",
                                    "cost",
                                    "limit",
                                    "modalities",
                                    "status",
                                )
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    reference = reference_for(str(provider_id), str(model_id))
                    entries[reference] = CatalogEntry(
                        reference=reference,
                        label=model["name"][:255],
                        source=self.source,
                        detail=str(provider.get("name") or provider_id)[:255],
                        input_per_million=_rate(cost.get("input")),
                        output_per_million=_rate(cost.get("output")),
                        cached_per_million=_rate(cost.get("cache_read")),
                        cache_write_per_million=None
                        if image_tokens else _rate(cost.get("cache_write")),
                        complete=all(
                            key not in cost or _rate(cost[key]) is not None
                            for key in ("input", "output", "cache_read", "cache_write")
                        ),
                        context_window=None if image_tokens else context,
                        provider_id=str(provider_id),
                        model_id=str(model_id),
                        canonical_model_id=model.get("canonical_model_id")
                        if isinstance(model.get("canonical_model_id"), str) else None,
                        unsupported=unsupported,
                        operation="image_generation" if image_tokens else "chat",
                        metadata={
                            "version": 1,
                            "snapshot_id": snapshot,
                            "entry_digest": entry_digest,
                            "fetched_at": fetched_at,
                            "source_updated_at": model.get("last_updated")
                            if isinstance(model.get("last_updated"), str) else None,
                        },
                    )
            if not entries:
                raise ValueError("Public catalog has no valid entries")
            self._entries = entries
            self._etag, self._last_modified = self._pending_headers
            self._snapshot = snapshot
            self._read_at = time.monotonic()

    def entries(self) -> tuple[CatalogEntry, ...]:
        if self._entries is None or time.monotonic() - self._read_at >= self._ttl:
            self.refresh()
        assert self._entries is not None
        return tuple(self._entries.values())

    def models(self) -> Sequence[CatalogModel]:
        return [
            CatalogModel(
                key=entry.reference,
                label=entry.label,
                product=entry.detail or "",
                source=self.source,
            )
            for entry in self.entries()
            if entry.priced and not entry.unsupported
        ]

    def entry(self, reference: str) -> CatalogEntry | None:
        if parse_reference(reference) is None:
            return None
        self.entries()
        assert self._entries is not None
        return self._entries.get(reference)

    def options(self, model: CatalogModel) -> CatalogOptions:
        entry = self.entry(model.key)
        return CatalogOptions(
            model=model,
            complete=bool(entry and entry.complete),
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
            note=entry.unsupported if entry else None,
        )
