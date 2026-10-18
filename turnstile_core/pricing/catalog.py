"""Published list prices, read from the places the vendors publish them.

A catalogue answers three questions, in the order a person asks them: which models are priced,
how is this one priced, and what does this exact entry cost today. It never decides which entry
belongs to which registry model -- matching `gpt-4.1-mmai-foundry-f5efed78` against a meter named
`gpt 4.1 Inp glbl Tokens` is a guess, and a wrong guess here is invisible, because the bill still
renders and still adds up. So the mapping is chosen by a person once and stored, and this module
only resolves what was stored.

**Why a vocabulary and not a pattern.** Azure's retail catalogue holds twenty-one product lines
under `Foundry Models`, and each family writes its meter names differently:

    gpt 4.1 Inp glbl Tokens                 Azure OpenAI
    5.4 pp cd inp Dz 1M Tokens              Azure OpenAI GPT5 -- the model name is a bare version
    Phi-3.5-Mini-128K-Instruct-Output       Azure Phi -- no deployment segment at all
    FW GLM 5.2 Inp DZ Tokens                Azure Fireworks
    K2.5 Thinking Outp DZ Tokens            Azure Kimi
    4.6 Outp DZ L Tokens                    Azure Grok

An earlier version matched a sentence shape per family and recognised 281 of 1625 meters; the
rest were dropped without a sound, which reads as "this model has no published price" rather than
"this parser has not met this family". Sentence shapes are unbounded -- Microsoft adds a family
and the parser silently narrows. The vocabulary is not: a bucket word, an optional cached marker,
an optional deployment word, and everything else is the model's name. Scanning for the words
recognises 1037 of the same 1625 and, more importantly, *reports* what it could not read instead
of hiding it.
"""





from __future__ import annotations

import hashlib
import json
import math
import re
import time
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from decimal import Decimal
from importlib.resources import files
from typing import Any, Literal, NamedTuple, Protocol

import httpx

from turnstile_core.domain.runtime_models import (
    PriceCatalogContextTier,
    PriceCatalogInputRange,
    PriceCatalogRuleSource,
    PriceSource,
)

AZURE_RETAIL_ENDPOINT = "https://prices.azure.com/api/retail/prices"
ANTHROPIC_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
CATALOG_TTL_SECONDS = 6 * 60 * 60
CATALOG_DEADLINE: ContextVar[float | None] = ContextVar("catalog_deadline", default=None)


_CONTEXT_RULES: dict[str, Any] = json.loads(
    files("turnstile_core.pricing").joinpath("context-rules.json").read_text(encoding="utf-8")
)


def price_request_timeout(maximum: float) -> float:
    deadline = CATALOG_DEADLINE.get()
    remaining = maximum if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Pricing operation deadline exceeded")
    return min(maximum, remaining)

# The index is read from one region because a model's Global rate is identical in every region --
# measured across 24 to 28 regions per model, always one figure. This region is chosen for
# breadth: it carries every model the narrower regions do.
INDEX_REGION = "eastus"

# --------------------------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------------------------

_BUCKET_INPUT = {"inp", "inpt", "input"}
_BUCKET_OUTPUT = {"outp", "outpt", "output", "out", "opt"}
_CACHED = {"cached", "cchd", "ccchd", "cd", "cache"}
# Follows a cached marker to mean the write side of the cache rather than the read side.
# Azure does publish these, on the GPT5 and GPT6 lines at least.
_CACHE_WRITE = {"wr", "write"}
_DEPLOYMENT = {
    "glbl": "Global",
    "gl": "Global",
    "global": "Global",
    "regnl": "Regional",
    "rgnl": "Regional",
    "regional": "Regional",
    "dz": "Data Zone",
    "dzn": "Data Zone",
    "dzl": "Data Zone",
    "dzone": "Data Zone",
    "datazone": "Data Zone",
}
# Words that make a meter something other than the rate a chat request is billed at. These are
# skipped deliberately, so they are not counted as unreadable.
_NOT_A_CHAT_RATE = {
    "batch", "ft", "finetuned", "hosting", "training", "rft", "grader",
    "provisioned", "managed", "reservation", "surcharge", "deployment",
}
# Units that carry no meaning once the bucket is known.
#
# `pp` and `l` used to be here, described as variants Azure prices identically to the base
# meter. They are not: measured across the catalogue, every meter carrying either marker is
# exactly 2.00x the same meter without it -- 24 matched pairs for `pp`, 12 for `l`, not one of
# them equal. Dropping them merged two different prices into one stem, and whichever row the
# API happened to return last became the rate. So they stay in the stem, which is what already
# happens to `Std` and `Fl`: a service tier makes it a different thing to buy, and the picker
# shows it as one.
_NOISE = {"tokens", "1m", "1k"}
# The only deployment word written as two words. Joined before tokenising so it is read as the
# deployment it is, rather than as two words that happen to sit next to each other.
_TWO_WORD_DEPLOYMENT = re.compile(r"\bdata\s+zone\b", re.IGNORECASE)

_DEPLOYMENT_ORDER = {"Global": 0, "Data Zone": 1, "Regional": 2}
UNSPECIFIED_DEPLOYMENT = "Standard"
_AZURE_FREE_OUTPUT_MODELS = {("Microsoft Decision Models", "Microsoft Decision 1")}


@dataclass(frozen=True)
class CatalogModel:
    """One priceable model, named the way its vendor names it."""

    key: str
    label: str
    product: str
    source: PriceSource
    context_tier: PriceCatalogContextTier | None = None
    text_to_image: bool = False

    @property
    def resolved_context_tier(self) -> PriceCatalogContextTier | None:
        descriptor = self.context_tier
        if descriptor is None or descriptor.input_range is not None:
            return descriptor
        matching = [
            rule for rule in _CONTEXT_RULES["rules"]
            if rule["basis"] == "request_input_tokens" and any(
                match["source"] == self.source and match["product"] == self.product
                and match["group_key"] == descriptor.group_key
                and match["service_tier"] == descriptor.service_tier
                for match in rule["matches"]
            )
        ]
        if len(matching) != 1:
            return descriptor
        rule = matching[0]
        tier = next((tier for tier in rule["tiers"] if tier["key"] == descriptor.key), None)
        if tier is None:
            return descriptor
        return descriptor.model_copy(update={
            "input_range": PriceCatalogInputRange.model_validate(tier["input_range"]),
            "input_range_source": PriceCatalogRuleSource(
                rule_id=rule["id"], url=rule["source_url"],
                revision=rule["source_revision"], verified_on=rule["verified_on"],
                origin=rule.get("origin", "reference"), note=rule.get("note"),
            ),
        })

    @property
    def search_text(self) -> str:
        if self.context_tier is not None:
            return " ".join(filter(None, (
                self.label, self.product,
                self.context_tier.model_label, self.context_tier.service_tier,
            )))
        return f"{self.label} {self.product}"


@dataclass(frozen=True)
class CatalogEntry:
    """One priceable thing, with every bucket the registry can charge for.

    A bucket is `None` when the vendor does not price it separately, which is different from
    free: the registry keeps its existing fallback rather than being given an invented number.
    Most Azure product lines publish no cache-write meter, but the GPT5 and GPT6 lines do, as
    `Cd Wr`, so it is read where it exists rather than assumed absent everywhere.
    """

    reference: str
    label: str
    source: PriceSource
    detail: str | None = None
    input_per_million: float | None = None
    output_per_million: float | None = None
    cached_per_million: float | None = None
    cache_write_per_million: float | None = None
    context_tier: PriceCatalogContextTier | None = None
    # False when the source was read only in part -- a page of the price feed failed and the
    # rest was kept. The buckets that did arrive are correct, but the ones that did not are
    # indistinguishable from buckets the vendor does not publish, and writing that difference
    # into a rate turns a transient 503 into a silent repricing. Readers that only display the
    # entry can ignore this; anything that writes a charged rate must not.
    complete: bool = True
    text_to_image: bool = False

    @property
    def priced(self) -> bool:
        return (
            self.input_per_million is not None and self.output_per_million is not None
            and all(
                value is None or not isinstance(value, bool)
                and math.isfinite(value) and 0 <= value < 10**10
                for value in (
                    self.input_per_million, self.output_per_million,
                    self.cached_per_million, self.cache_write_per_million,
                )
            )
        )

    def discounted(self, percent: float | None) -> CatalogEntry:
        if percent is None or percent == 100:
            return self
        factor = Decimal(str(percent)) / Decimal(100)

        def apply(value: float | None) -> float | None:
            return None if value is None else float(
                (Decimal(str(value)) * factor).quantize(Decimal("0.00000001"))
            )

        return replace(
            self,
            input_per_million=apply(self.input_per_million),
            output_per_million=apply(self.output_per_million),
            cached_per_million=apply(self.cached_per_million),
            cache_write_per_million=apply(self.cache_write_per_million),
        )

    context_window: int | None = None

    provider_id: str | None = None

    model_id: str | None = None

    canonical_model_id: str | None = None

    unsupported: str | None = None

    operation: Literal["chat", "image_generation"] = "chat"

    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.text_to_image:
            object.__setattr__(self, "operation", "image_generation")

    @property
    def digest(self) -> str:
        return str(self.metadata.get("entry_digest") or hashlib.sha256(json.dumps(
            [self.reference, self.input_per_million, self.output_per_million,
             self.cached_per_million, self.cache_write_per_million, self.context_window],
            separators=(",", ":"),
        ).encode()).hexdigest())


@dataclass(frozen=True)
class CatalogOption:
    """How one model is priced under one deployment shape.

    `regions` lists every region that charges exactly these rates. When a shape charges the same
    everywhere -- which is what Global does, in every case measured -- there is nothing for a
    person to choose and `region_required` is false. Asking anyway would be asking for a decision
    that cannot change the answer.

    `references_by_region` gives each of those regions its own reference. Grouping regions that
    charge alike is a display decision; storing one region's reference for a person who picked a
    different one is a data decision, and a wrong one. The prices agree today -- that is why the
    regions are grouped -- so the bill is right either way, right up until the vendor splits the
    group and the model quietly follows whichever region happened to sort first.
    """

    reference: str
    deployment: str
    entry: CatalogEntry
    regions: tuple[str, ...]
    region_required: bool
    references_by_region: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogOptions:
    """Everything known about one model's pricing, including what could not be read."""

    model: CatalogModel
    options: tuple[CatalogOption, ...] = ()
    unreadable: tuple[str, ...] = ()
    other_meters: tuple[str, ...] = ()
    note: str | None = None
    # See CatalogEntry.complete. Repeated here so a caller holding only the options knows.
    complete: bool = True


class _Fetched(NamedTuple):
    """Rows from the price feed, and whether they are all of them."""

    rows: list[dict[str, Any]]
    complete: bool


@dataclass
class _ParsedMeter:
    stem: str
    slot: str
    deployment: str
    region: str
    per_million: float
    product: str


class PriceCatalog(Protocol):
    source: PriceSource

    def models(self) -> Sequence[CatalogModel]: ...

    def options(self, model: CatalogModel) -> CatalogOptions: ...

    def entry(self, reference: str) -> CatalogEntry | None: ...


def _http_get_json(url: str, *, timeout: float = 40.0) -> dict[str, Any]:
    response = httpx.get(url, timeout=price_request_timeout(timeout), follow_redirects=True)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def parse_meter_name(name: str) -> tuple[str, str, str] | None:
    """`(stem, slot, deployment)` for a chat-token meter, or None.

    None means one of two different things, and the caller has to tell them apart: a meter that
    prices something other than a chat request -- batch, fine-tuning, hosting -- or one this
    vocabulary has not met. The first is expected; the second is a gap worth showing someone.
    """
    words = [word for word in re.split(r"[\s\-]+", _TWO_WORD_DEPLOYMENT.sub("DataZone", name))
             if word]
    if not words or words[-1].lower() != "tokens":
        return None
    lowered = [word.lower() for word in words]
    if any(word in _NOT_A_CHAT_RATE for word in lowered):
        return None

    slot: str | None = None
    deployment: str | None = None
    cached = False
    cache_write = False
    kept: list[str] = []
    index = 0
    while index < len(lowered):
        word = lowered[index]
        following = lowered[index + 1] if index + 1 < len(lowered) else ""
        if word in _CACHED and following in _CACHE_WRITE:
            cache_write = True
            index += 2
            continue
        if slot is None and word in _BUCKET_INPUT:
            slot = "input"
            index += 1
            continue
        if slot is None and word in _BUCKET_OUTPUT:
            slot = "output"
            index += 1
            continue
        if deployment is None and word in _DEPLOYMENT:
            deployment = _DEPLOYMENT[word]
            index += 1
            continue
        if word in _CACHED:
            cached = True
            index += 1
            continue
        if word in _NOISE:
            index += 1
            continue
        kept.append(words[index])
        index += 1

    if cache_write:
        resolved = "cache_write"
    elif cached and slot in (None, "input"):
        # A cached marker with no bucket word beside it still prices cached input -- Azure Kimi
        # writes `K2.5 cached glbl Tokens` and nothing else. Reading it as unknown would hide a
        # rate that is plainly there.
        resolved = "cached"
    elif slot is None:
        return None
    else:
        resolved = slot
    stem = " ".join(kept).strip()
    if not stem:
        return None
    return stem, resolved, (deployment or UNSPECIFIED_DEPLOYMENT)


def parse_context_tier(label: str) -> PriceCatalogContextTier | None:
    markers = list(re.finditer(r"\b(short|long)(?:co\b|[\s_-]+context\b)", label, re.IGNORECASE))
    if len(markers) != 1:
        return None
    marker = markers[0]
    name = f"{label[:marker.start()]} {label[marker.end():]}"
    name = re.sub(r"\s+", " ", re.sub(r"\(\s*\)", "", name)).strip()
    if not name:
        return None
    short = marker.group(1).lower() == "short"
    return PriceCatalogContextTier(
        group_key=name.lower(),
        model_label=name,
        key="short" if short else "long",
        label="短上下文" if short else "长上下文",
        order=0 if short else 1,
    )


def prices_a_chat_request(name: str) -> bool:
    """Whether a meter is the sort of thing that could price a chat request at all."""
    normalised = _TWO_WORD_DEPLOYMENT.sub("DataZone", name)
    lowered = [word.lower() for word in re.split(r"[\s\-]+", normalised) if word]
    if not lowered or lowered[-1] != "tokens":
        return False
    return not any(word in _NOT_A_CHAT_RATE for word in lowered)


def _per_million(price: Any, unit: Any) -> float | None:
    if isinstance(price, bool) or not isinstance(price, int | float):
        return None
    text = str(unit or "").strip()
    # Azure quotes most token meters per 1K and the newer ones per 1M; the registry stores per 1M.
    scaled = float(price) * 1000 if text.startswith("1K") else float(price)
    # Rounded because multiplying a per-1K figure by 1000 in binary floating point turns 0.000138
    # into 0.13799999999999998, and a rate printed to fifteen decimals reads as a bug.
    return round(scaled, 8)


def _text_to_image_meter(parsed: _ParsedMeter) -> _ParsedMeter | None:
    if parsed.product not in {"Azure OpenAI", "Azure OpenAI Media"}:
        return None
    stem, _, modality = parsed.stem.rpartition(" ")
    if not stem.lower().startswith(("gpt image ", "image ")):
        return None
    if (modality == "txt" and parsed.slot in {"input", "cached"}
            or modality == "img" and parsed.slot == "output"):
        return replace(parsed, stem=stem)
    return None


# --------------------------------------------------------------------------------------------
# Azure
# --------------------------------------------------------------------------------------------


class AzureRetailCatalog:
    """Azure's public retail price API.

    Queries are filtered server-side and kept narrow on purpose. An earlier version downloaded a
    product line and filtered locally, which meant guessing which products hold chat models --
    and the guess was wrong, because gpt-5 lives under `Azure OpenAI GPT5` rather than
    `Azure OpenAI`. A later version over-corrected into `contains(meterName,'gpt')`, matched tens
    of thousands of meters, earned a 429 partway through paging, and returned nothing at all.
    """

    source = PriceSource.AZURE_RETAIL

    def __init__(self, *, ttl_seconds: int = CATALOG_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._models: tuple[float, tuple[CatalogModel, ...]] | None = None
        self._options: dict[str, tuple[float, CatalogOptions]] = {}

    # -- index -------------------------------------------------------------------------------

    def models(self) -> Sequence[CatalogModel]:
        now = time.monotonic()
        if self._models is not None and now - self._models[0] < self._ttl:
            return self._models[1]
        built = tuple(self._build_index())
        self._models = (now, built)
        return built

    def _build_index(self) -> Iterable[CatalogModel]:
        # A partial index only costs the picker a few entries someone can search for again;
        # unlike a partial price, it cannot reach a bill.
        rows = self._fetch(
            f"serviceName eq 'Foundry Models' and armRegionName eq '{INDEX_REGION}' "
            f"and contains(meterName,'Tokens')"
        ).rows
        seen: dict[tuple[str, str], CatalogModel] = {}
        slots: dict[tuple[str, str], set[str]] = {}
        for row in rows:
            parsed = self._parse_row(row)
            if parsed is None:
                continue
            for candidate in (parsed, _text_to_image_meter(parsed)):
                if candidate is None:
                    continue
                key = (candidate.product, candidate.stem)
                slots.setdefault(key, set()).add(candidate.slot)
                seen.setdefault(
                    key,
                    CatalogModel(
                        key=f"azure_retail:{candidate.product}:{candidate.stem}",
                        label=candidate.stem,
                        product=candidate.product,
                        source=PriceSource.AZURE_RETAIL,
                        context_tier=parse_context_tier(candidate.stem),
                        text_to_image=candidate is not parsed,
                    ),
                )
        # A model with only one side of the trade cannot price a request, so it is left out of
        # the picker rather than offered and then found wanting.
        return [model for key, model in sorted(seen.items()) if "input" in slots[key]
            and ("output" in slots[key] or key in _AZURE_FREE_OUTPUT_MODELS)]

    # -- one model ---------------------------------------------------------------------------

    def options(self, model: CatalogModel) -> CatalogOptions:
        now = time.monotonic()
        cached = self._options.get(model.key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        built = self._build_options(model)
        self._options[model.key] = (now, built)
        return built

    def _build_options(self, model: CatalogModel) -> CatalogOptions:
        clauses = [
            "serviceName eq 'Foundry Models'",
            f"productName eq '{_odata_literal(model.product)}'",
        ]
        name_filter = _meter_name_filter(model.label)
        if name_filter:
            clauses.append(name_filter)
        fetched = self._fetch(" and ".join(clauses))
        rows = fetched.rows

        buckets: dict[tuple[str, str], dict[str, float]] = {}
        unreadable: set[str] = set()
        other_meters: set[str] = set()
        text_to_image = False
        for row in rows:
            name = str(row.get("meterName") or "")
            parsed = self._parse_row(row)
            if parsed is not None and parsed.stem != model.label:
                candidate = _text_to_image_meter(parsed)
                if candidate is None or candidate.stem != model.label:
                    continue
                parsed = candidate
                text_to_image = True
            if parsed is None:
                if not str(row.get("meterName") or "").strip():
                    continue
                if not prices_a_chat_request(name):
                    # Prices something other than a normal chat request: a batch rate, a
                    # fine-tuning rate, a per-hour reservation, a per-second video meter. Named
                    # rather than dropped, so the dialog can say "that one prices something
                    # else" instead of leaving a reader to wonder where it went.
                    if model.label.lower() in name.lower():
                        other_meters.add(f"{name} ({row.get('unitOfMeasure')})")
                elif model.label.lower() in name.lower():
                    unreadable.add(name)
                continue
            if parsed.stem != model.label:
                continue
            buckets.setdefault((parsed.deployment, parsed.region), {})[parsed.slot] = (
                parsed.per_million
            )

        Rates = tuple[float, float, float | None, float | None]
        by_deployment: dict[str, dict[Rates, list[str]]] = {}
        for (deployment, region), slot_prices in buckets.items():
            input_rate = slot_prices.get("input")
            output_rate = slot_prices.get("output")
            if output_rate is None and (model.product, model.label) in _AZURE_FREE_OUTPUT_MODELS:
                output_rate = 0.0
            if input_rate is None or output_rate is None:
                continue
            rates: Rates = (
                input_rate,
                output_rate,
                slot_prices.get("cached"),
                slot_prices.get("cache_write"),
            )
            by_deployment.setdefault(deployment, {}).setdefault(rates, []).append(region)

        options: list[CatalogOption] = []
        for deployment, groups in by_deployment.items():
            region_required = len(groups) > 1
            reference_prefix = f"azure_retail:{model.product}:{model.label}:{deployment}"
            for rates, regions in groups.items():
                regions_sorted = tuple(sorted(regions))

                # A shape that charges one figure everywhere needs no region in its reference:
                # pinning one would make the stored mapping look region-specific when it is not.
                anchor = regions_sorted[0] if region_required else "*"
                reference = f"{reference_prefix}:{anchor}"
                options.append(
                    CatalogOption(
                        reference=reference,
                        deployment=deployment,
                        entry=CatalogEntry(
                            reference=reference,
                            label=model.label,
                            source=PriceSource.AZURE_RETAIL,
                            detail=deployment,
                            input_per_million=rates[0],
                            output_per_million=rates[1],
                            cached_per_million=rates[2],
                            cache_write_per_million=rates[3],
                            context_tier=(
                                model.resolved_context_tier or parse_context_tier(model.label)
                            ),
                            complete=fetched.complete,
                            text_to_image=text_to_image,
                            operation="image_generation" if text_to_image else "chat",
                        ),
                        regions=regions_sorted,
                        region_required=region_required,
                        # One reference per region, so picking the third region in a group stores
                        # the third region. The group exists because they charge alike now, not
                        # because they are the same region.
                        references_by_region={
                            region: f"{reference_prefix}:{region}" for region in regions_sorted
                        },
                    )
                )
        options.sort(
            key=lambda option: (
                _DEPLOYMENT_ORDER.get(option.deployment, 9),
                option.entry.input_per_million or 0,
            )
        )
        return CatalogOptions(
            model=replace(model, text_to_image=text_to_image),
            options=tuple(options),
            unreadable=tuple(sorted(unreadable)),
            other_meters=tuple(sorted(other_meters)),
            complete=fetched.complete,
            note=None if fetched.complete else (
                "价目表只读到一部分(分页中断),这些价格可以看,但不会被写成实际单价。"
            ),
        )

    def entry(self, reference: str) -> CatalogEntry | None:
        parts = reference.split(":")
        if len(parts) != 5 or parts[0] != "azure_retail":
            return None
        _, product, stem, deployment, anchor = parts
        model = CatalogModel(
            key=f"azure_retail:{product}:{stem}",
            label=stem,
            product=product,
            source=PriceSource.AZURE_RETAIL,
        )
        for option in self.options(model).options:
            if option.deployment != deployment:
                continue
            if anchor == "*" and not option.region_required:
                return option.entry
            if anchor in option.regions:
                return option.entry
        return None

    # -- plumbing ----------------------------------------------------------------------------

    def _parse_row(self, row: dict[str, Any]) -> _ParsedMeter | None:
        name = str(row.get("meterName") or "")
        product = str(row.get("productName") or "Azure")
        parsed = parse_meter_name(name)
        stem = name.removesuffix(" 1M Tokens")
        if name.endswith(" 1M Tokens") and (product, stem) in _AZURE_FREE_OUTPUT_MODELS:
            parsed = (stem, "input", "Global")
        if parsed is None:
            return None
        per_million = _per_million(row.get("retailPrice"), row.get("unitOfMeasure"))
        if per_million is None:
            return None
        stem, slot, deployment = parsed
        return _ParsedMeter(
            stem=stem,
            slot=slot,
            deployment=deployment,
            region=str(row.get("armRegionName") or "global"),
            per_million=per_million,
            product=product,
        )

    def _fetch(self, filter_expression: str) -> _Fetched:
        url = (
            AZURE_RETAIL_ENDPOINT
            + "?currencyCode='USD'&$filter="
            + urllib.parse.quote(filter_expression)
        )
        rows: list[dict[str, Any]] = []
        for page in range(25):
            try:
                payload = _http_get_json(url)
            except httpx.HTTPError:
                # A page that fails partway through leaves what was already read, because
                # returning nothing would turn a throttled request into "this model has no
                # published price" -- a different and more misleading answer. But the caller has
                # to be told, or a half-read model looks exactly like a fully-read one whose
                # vendor publishes fewer buckets. Reading pages 1 of 2 and writing the result
                # blanks every rate that lived on page 2.
                if page == 0:
                    raise
                return _Fetched(rows=rows, complete=False)
            items = payload.get("Items")
            if isinstance(items, list):
                rows.extend(item for item in items if isinstance(item, dict))
            next_link = payload.get("NextPageLink")
            if not isinstance(next_link, str) or not next_link:
                return _Fetched(rows=rows, complete=True)
            url = next_link
        # Ran out of page budget with a next link still pending: also a partial read.
        return _Fetched(rows=rows, complete=False)


def _odata_literal(value: str) -> str:
    return value.replace("'", "''")


def _meter_name_filter(label: str) -> str:
    """An OData clause matching a model's name against a meter name.

    Every word has to appear, but nothing says where. An earlier version asked for the words
    joined back together -- `contains(meterName,'6 astra LongCo Std')`, and the hyphenated
    spelling beside it -- which assumes the name survives in the meter as one run of text. It
    often does not: `6-astra LongCo Opt Std DZ 1M Tokens` puts the bucket word *inside* the
    name, so neither spelling matched and gpt-6-astra came back with no prices at all while
    still appearing in the picker.

    Sending the words separately is not the same mistake as sending one of them: a single
    `contains(meterName,'gpt')` matches tens of thousands of meters and gets the request
    throttled, while every word together with `productName` is narrower than the joined form
    ever was.
    """
    terms = [term for term in re.split(r"[^A-Za-z0-9.]+", label.strip()) if term]
    if not terms:
        return ""
    clauses = [f"contains(meterName,'{_odata_literal(term)}')" for term in terms[:4]]
    return "(" + " and ".join(clauses) + ")"


# --------------------------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------------------------

_ANTHROPIC_MODEL = re.compile(r"^Claude\s+[A-Z][A-Za-z]*\s+[\d.]+", re.IGNORECASE)
_ANTHROPIC_PRICE = re.compile(r"^\$\s?([\d,]+(?:\.\d+)?)\s*/\s*MTok", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_MARKDOWN_LINK = re.compile(r"\[([^]]+)]\([^)]+\)")
_ANTHROPIC_MODEL_PRICING_HEADER = (
    "model", "base input tokens", "5m cache writes", "1h cache writes",
    "cache hits and refreshes", "output tokens",
)
ANTHROPIC_PRODUCT = "Anthropic"


class AnthropicCatalog:
    """Anthropic's published list, parsed from its Markdown pricing table.

    The page prints five figures per model in a fixed order -- input, cache write at the
    5-minute TTL, cache write at the 1-hour TTL, cache read, output. The table header binds each
    value to its meaning. Cache writes retain their documented 1.25x/2x relationship; cache reads
    are only bounded because Anthropic now publishes model-specific 0.025x and 0.05x exceptions.
    A malformed row is dropped rather than published as a price.
    """

    source = PriceSource.ANTHROPIC

    def __init__(self, *, ttl_seconds: int = CATALOG_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._cache: tuple[float, tuple[CatalogEntry, ...]] | None = None

    def _entries(self) -> Sequence[CatalogEntry]:
        now = time.monotonic()
        if self._cache is not None and now - self._cache[0] < self._ttl:
            return self._cache[1]
        built = tuple(self._build())
        self._cache = (now, built)
        return built

    def models(self) -> Sequence[CatalogModel]:
        return [
            CatalogModel(
                key=f"anthropic:{ANTHROPIC_PRODUCT}:{entry.label}",
                label=entry.label,
                product=ANTHROPIC_PRODUCT,
                source=PriceSource.ANTHROPIC,
            )
            for entry in self._entries()
        ]

    def options(self, model: CatalogModel) -> CatalogOptions:
        for entry in self._entries():
            if entry.label != model.label:
                continue
            reference = f"anthropic:{ANTHROPIC_PRODUCT}:{entry.label}:List price:*"
            return CatalogOptions(
                model=model,
                options=(
                    CatalogOption(
                        reference=reference,
                        deployment="List price",
                        entry=replace(entry, reference=reference, detail="List price"),
                        regions=(),
                        region_required=False,
                    ),
                ),
                note="Anthropic 对所有区域发布同一份列表价。",
            )
        return CatalogOptions(model=model)

    def entry(self, reference: str) -> CatalogEntry | None:
        parts = reference.split(":")
        if len(parts) != 5 or parts[0] != "anthropic":
            return None
        label = parts[2]
        for item in self._entries():
            if item.label == label:
                return replace(item, reference=reference, detail="List price")
        return None

    def _build(self) -> Iterable[CatalogEntry]:
        response = httpx.get(
            ANTHROPIC_PRICING_URL,
            timeout=price_request_timeout(40.0),
            follow_redirects=True,
            headers={"User-Agent": "turnstile-price-sync"},
        )
        response.raise_for_status()
        seen: set[str] = set()
        built: list[CatalogEntry] = []
        for row in _anthropic_model_pricing_rows(response.text):
            name = _markdown_cell_text(row[0]).split("(")[0].strip()
            if len(name) > 48 or not _ANTHROPIC_MODEL.match(name) or name in seen:
                continue
            base = _anthropic_price(row[1])
            write_5m = _anthropic_price(row[2])
            write_1h = _anthropic_price(row[3])
            read = _anthropic_price(row[4])
            output = _anthropic_price(row[5])
            if (
                base is None or write_5m is None or write_1h is None
                or read is None or output is None
            ):
                continue
            if not _anthropic_prices_hold(base, write_5m, write_1h, read, output):
                continue
            seen.add(name)
            built.append(CatalogEntry(
                reference=f"anthropic:{ANTHROPIC_PRODUCT}:{name}:List price:*",
                label=name,
                source=PriceSource.ANTHROPIC,
                detail="List price",
                input_per_million=base,
                output_per_million=output,
                cached_per_million=read,
                # The 5-minute TTL is what a request gets unless it asks for the hourly one.
                cache_write_per_million=write_5m,
            ))
        if not built:
            raise ValueError("Anthropic model pricing table is empty or unrecognized")
        yield from built


def _anthropic_model_pricing_rows(markdown: str) -> Iterable[list[str]]:
    in_model_pricing = False
    header_found = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line == "## Model pricing":
            in_model_pricing = True
            continue
        if in_model_pricing and line.startswith("## "):
            return
        if not in_model_pricing or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        normalized = tuple(_markdown_cell_text(cell).lower() for cell in cells)
        if not header_found:
            header_found = normalized[:6] == _ANTHROPIC_MODEL_PRICING_HEADER
            continue
        if len(cells) < 6 or all(re.fullmatch(r":?-+:?", cell) for cell in cells[:6]):
            continue
        yield cells


def _markdown_cell_text(value: str) -> str:
    text = _MARKDOWN_LINK.sub(r"\1", value)
    text = _TAG.sub("", text)
    return re.sub(r"\s+", " ", text).strip().strip("*_`")


def _anthropic_price(value: str) -> float | None:
    match = _ANTHROPIC_PRICE.match(_markdown_cell_text(value))
    return None if match is None else float(match.group(1).replace(",", ""))


def _anthropic_prices_hold(
    base: float, write_5m: float, write_1h: float, read: float, output: float,
) -> bool:
    if base <= 0 or read <= 0 or output < base:
        return False

    def close(value: float, expected: float) -> bool:
        return abs(value - expected) <= max(expected * 0.02, 0.005)

    return close(write_5m, base * 1.25) and close(write_1h, base * 2) and read <= base


# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSearch:
    models: tuple[CatalogModel, ...] = ()
    unavailable: tuple[str, ...] = field(default=())


class CompositeCatalog:
    """Every source behind one lookup, because a connection can serve several vendors."""

    def __init__(self, catalogs: Sequence[PriceCatalog]) -> None:
        self._catalogs = list(catalogs)

    def search_models(
        self, query: str, *, limit: int = 60, source: PriceSource | None = None,
    ) -> ModelSearch:
        terms = [term for term in re.split(r"[^a-z0-9.]+", query.lower()) if term]
        scored: list[tuple[int, int, str, CatalogModel]] = []
        unavailable: list[str] = []
        for catalog in self._catalogs:
            if source is not None and catalog.source is not source:
                continue
            try:
                candidates = catalog.models()
            except (httpx.HTTPError, ValueError):
                # One unreachable source must not blank the others: a Claude price that cannot be
                # read is a reason to leave Claude alone, not to stop pricing GPT.
                unavailable.append(str(getattr(catalog, "source", "unknown")))
                continue
            for model in candidates:
                haystack = model.search_text.lower()
                tokens = [token for token in re.split(r"[^a-z0-9.]+", haystack) if token]
                scores = [_term_score(term, tokens) for term in terms]
                if terms and not all(scores):
                    continue
                scored.append((-sum(scores), len(model.label), model.label.lower(), model))
        scored.sort(key=lambda item: item[:3])
        return ModelSearch(
            models=tuple(model for *_, model in scored[:limit]),
            unavailable=tuple(unavailable),
        )

    def options(self, model_key: str) -> CatalogOptions | None:
        for catalog in self._catalogs:
            if model_key.partition(":")[0] != str(catalog.source):
                continue
            try:
                models = catalog.models()
            except (httpx.HTTPError, ValueError):
                continue
            for model in models:
                if model.key == model_key:
                    return catalog.options(model)
        return None

    def lookup(self, reference: str) -> CatalogEntry | None:
        for catalog in self._catalogs:
            if reference.partition(":")[0] != str(catalog.source):
                continue
            return catalog.entry(reference)
        return None

    def public_catalog(self) -> Any:
        return next(
            (catalog for catalog in self._catalogs if catalog.source is PriceSource.MODELS_DEV),
            None,
        )

    def refresh(self, sources: set[PriceSource]) -> dict[PriceSource, str]:
        failures: dict[PriceSource, str] = {}
        for catalog in self._catalogs:
            if catalog.source not in sources:
                continue
            try:
                if refresh := getattr(catalog, "refresh", None):
                    refresh()
                elif isinstance(catalog, AzureRetailCatalog):
                    catalog._options.clear()
                    catalog._models = None
                elif isinstance(catalog, AnthropicCatalog):
                    catalog._cache = None
            except (httpx.HTTPError, ValueError, OSError):
                failures[catalog.source] = "价格来源刷新失败，保留现有单价"
        return failures


def _term_score(term: str, tokens: Sequence[str]) -> int:
    """2 for an exact token, 1 for a token that starts with the term, 0 for no match.

    Never a fragment buried inside another number: substring matching made `gpt 5` find
    `gpt 4o 0513`, because "5" appears inside "0513" -- a match no reader would call one.
    """
    if term in tokens:
        return 2
    return 1 if any(token.startswith(term) for token in tokens) else 0


def build_default_catalog() -> CompositeCatalog:
    from turnstile_core.pricing.models_dev import ModelsDevCatalog

    return CompositeCatalog([ModelsDevCatalog(), AzureRetailCatalog(), AnthropicCatalog()])
