"""The catalogue's three jobs: read a vendor's meter names, group a model's prices, resolve one.

Every fixture below is a meter name copied verbatim from the Azure retail price API. The naming
is not consistent between product lines, and each inconsistency is a chance to drop a model's
price without anyone noticing -- which is exactly what a shape-matching parser did before this.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from turnstile_core.domain.runtime_models import (
    PriceCatalogContextTier,
    PriceCatalogInputRange,
    PriceSource,
)
from turnstile_core.pricing.catalog import (
    AzureRetailCatalog,
    CatalogModel,
    CatalogOptions,
    CompositeCatalog,
    _Fetched,
    parse_context_tier,
    parse_meter_name,
)
from turnstile_core.pricing.models_dev import ModelsDevCatalog


def meter(name: str, price: float, region: str = "eastus",
          product: str = "Azure OpenAI", unit: str = "1K") -> dict[str, Any]:
    return {
        "meterName": name,
        "retailPrice": price,
        "unitOfMeasure": unit,
        "armRegionName": region,
        "productName": product,
        "serviceName": "Foundry Models",
    }


class StubAzure(AzureRetailCatalog):
    """Same parsing and grouping, no network."""

    def __init__(self, rows: list[dict[str, Any]], *, complete: bool = True) -> None:
        super().__init__()
        self._rows = rows
        self._complete = complete
        self.filters: list[str] = []

    def _fetch(self, filter_expression: str) -> Any:
        self.filters.append(filter_expression)
        return _Fetched(rows=self._rows, complete=self._complete)


def test_source_filter_does_not_fetch_other_adapters_even_when_azure_is_empty() -> None:
    def forbidden() -> dict[str, Any]:
        pytest.fail("Azure search fetched the public catalog")

    composite = CompositeCatalog([ModelsDevCatalog(fetch=forbidden), StubAzure([])])
    result = composite.search_models("grok", source=PriceSource.AZURE_RETAIL)
    assert not result.models and not result.unavailable
    assert composite.options("azure_retail:Azure Grok:4.7") is None


def test_unavailable_selected_source_does_not_fall_back_to_another_source() -> None:
    class BrokenAzure(StubAzure):
        def models(self) -> Any:
            raise httpx.ConnectError("Azure unavailable")

    composite = CompositeCatalog([BrokenAzure([]), StubAzure([])])
    result = composite.search_models("grok", source=PriceSource.AZURE_RETAIL)
    assert not result.models and result.unavailable == ("azure_retail",)


# --------------------------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Azure OpenAI
        ("gpt 4.1 Inp glbl Tokens", ("gpt 4.1", "input", "Global")),
        ("gpt 4.1 Outp glbl Tokens", ("gpt 4.1", "output", "Global")),
        ("gpt 4.1 cached Inp glbl Tokens", ("gpt 4.1", "cached", "Global")),
        ("gpt 4.1 Inp Data Zone Tokens", ("gpt 4.1", "input", "Data Zone")),
        ("gpt 4.1 Inp regnl Tokens", ("gpt 4.1", "input", "Regional")),
        # Azure OpenAI GPT5 -- the model name is a bare version, `opt` means output,
        # `cd` means cached, `Gl`/`Dz` are the deployment.
        ("5.4 inp Gl 1M Tokens", ("5.4", "input", "Global")),
        ("5.4 opt Gl 1M Tokens", ("5.4", "output", "Global")),
        ("5.1 cd inp Dz 1M Tokens", ("5.1", "cached", "Data Zone")),
        ("gpt 5 pro out glbl Tokens", ("gpt 5 pro", "output", "Global")),
        ("gpt-5-codex-inp-glbl Tokens", ("gpt 5 codex", "input", "Global")),
        ("gpt-5-codex-ccchd-inp-glbl Tokens", ("gpt 5 codex", "cached", "Global")),
        # Spelling variants of the bucket words themselves.
        ("GPT 5 Nano Inpt Glbl 1M Tokens", ("GPT 5 Nano", "input", "Global")),
        ("GPT 5 Mini outpt Glbl 1M Tokens", ("GPT 5 Mini", "output", "Global")),
        # The write side of the cache. Azure publishes it on GPT5 and GPT6.
        ("5.6 sol LongCo Cd Wr Std Gl 1M Tokens",
         ("5.6 sol LongCo Std", "cache_write", "Global")),
        ("6-astra ShortCo Cd Wr Std DZ 1M Tokens",
         ("6 astra ShortCo Std", "cache_write", "Data Zone")),
        # A cached marker with no bucket word beside it still prices cached input.
        ("K2.5 cached glbl Tokens", ("K2.5", "cached", "Global")),
        # Other vendors, each with its own spelling.
        ("FW GLM 5.2 Inp DZ Tokens", ("FW GLM 5.2", "input", "Data Zone")),
        ("K2.5 Thinking Outp DZ Tokens", ("K2.5 Thinking", "output", "Data Zone")),
        ("V3.1 Inp DZone Tokens", ("V3.1", "input", "Data Zone")),
        # No deployment segment at all.
        ("Phi-3.5-Mini-128K-Instruct-Output Tokens",
         ("Phi 3.5 Mini 128K Instruct", "output", "Standard")),
    ],
)
def test_reads_every_naming_convention_in_the_catalogue(
    name: str, expected: tuple[str, str, str]
) -> None:
    assert parse_meter_name(name) == expected


@pytest.mark.parametrize(
    ("premium", "base", "stem"),
    [
        ("5.4 pp inp Gl 1M Tokens", "5.4 inp Gl 1M Tokens", "5.4"),
        ("4.6 Outp DZ L Tokens", "4.6 Outp DZ Tokens", "4.6"),
    ],
)
def test_a_service_tier_is_a_different_thing_to_buy(
    premium: str, base: str, stem: str
) -> None:
    """`pp` and `l` were read as noise, on the belief that Azure prices them the same.

    It does not. Every meter carrying either marker is exactly twice the same meter without it
    -- 24 matched pairs for `pp`, 12 for `l`, not one of them equal. Collapsing them onto one
    stem meant two prices landed in the same slot and the later row won, so a model could
    quietly start charging the premium rate.
    """
    premium_parsed = parse_meter_name(premium)
    base_parsed = parse_meter_name(base)
    assert premium_parsed is not None
    assert base_parsed is not None
    assert base_parsed[0] == stem
    assert premium_parsed[0] != base_parsed[0], (
        "the premium tier has to reach the picker as its own entry, or its price overwrites "
        "the standard one"
    )
    assert premium_parsed[1:] == base_parsed[1:]


@pytest.mark.parametrize(
    "name",
    [
        "gpt 4o 0513 Batch Inp glbl Tokens",
        "o4-mini-ft model grader output Tokens",
        "Phi-3-Mini-128K-Instruct-Input-Finetuned Tokens",
        "Provisioned Managed Global Unit",
        "Sora 2 pro glbl Second",
        "gpt-transcribe Gl Unit",
    ],
)
def test_declines_meters_that_do_not_price_a_chat_request(name: str) -> None:
    assert parse_meter_name(name) is None


# --------------------------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------------------------

GPT_41 = [
    meter("gpt 4.1 Inp glbl Tokens", 0.002, region=region)
    for region in ("eastus", "brazilsouth", "canadaeast")
] + [
    meter("gpt 4.1 Outp glbl Tokens", 0.008, region=region)
    for region in ("eastus", "brazilsouth", "canadaeast")
] + [
    meter("gpt 4.1 cached Inp glbl Tokens", 0.0005, region=region)
    for region in ("eastus", "brazilsouth", "canadaeast")
] + [
    # Regional genuinely differs by region, which is the case that needs a third choice.
    meter("gpt 4.1 Inp regnl Tokens", 0.0022, region="eastus"),
    meter("gpt 4.1 Outp regnl Tokens", 0.0088, region="eastus"),
    meter("gpt 4.1 Inp regnl Tokens", 0.0024, region="northeurope"),
    meter("gpt 4.1 Outp regnl Tokens", 0.0096, region="northeurope"),
]

GPT_41_MODEL = CatalogModel(
    key="azure_retail:Azure OpenAI:gpt 4.1",
    label="gpt 4.1",
    product="Azure OpenAI",
    source=PriceSource.AZURE_RETAIL,
)


def options_for(rows: list[dict[str, Any]], model: CatalogModel) -> CatalogOptions:
    return StubAzure(rows).options(model)


@pytest.mark.parametrize("name", ["Any Model", "New Vendor 99", "Future Generation", "6 astra"])
@pytest.mark.parametrize("short_marker,long_marker", [
    ("ShortCo", "LongCo"), ("short context", "long context"), ("Short-Context", "Long-Context"),
])
def test_context_tier_metadata_does_not_depend_on_a_model_or_guessed_threshold(
    name: str, short_marker: str, long_marker: str,
) -> None:
    short = parse_context_tier(f"{name} {short_marker} Std")
    long = parse_context_tier(f"{name} {long_marker} Std")
    assert short is not None and long is not None
    assert short.group_key == long.group_key == f"{name} Std".lower()
    assert short.key == "short" and long.key == "long"
    assert short.input_range is None and long.input_range is None
    priority = parse_context_tier(f"{name} {long_marker} PP")
    assert priority is not None and priority.group_key != short.group_key


@pytest.mark.parametrize("label", ["Any Model", "ShortCo", "Long Model", "Any ShortCo LongCo Std"])
def test_context_tier_metadata_does_not_guess_ambiguous_names(label: str) -> None:
    assert parse_context_tier(label) is None


def test_context_threshold_requires_reference_provenance_not_only_a_model_name() -> None:
    label = "5.6 sol ShortCo Std"
    descriptor = parse_context_tier(label)
    model = CatalogModel(
        key=f"azure_retail:Azure OpenAI GPT5:{label}", label=label,
        source=PriceSource.AZURE_RETAIL, product="Azure OpenAI GPT5",
        context_tier=descriptor,
    )
    resolved = model.resolved_context_tier
    assert resolved is not None and resolved.input_range is not None
    assert resolved.input_range.min_input_tokens == 0
    assert resolved.input_range.max_input_tokens == 272000
    assert resolved.input_range_source is not None
    assert resolved.input_range_source.origin == "reference"
    assert resolved.input_range_source.url is not None
    assert resolved.input_range_source.url.startswith("https://learn.microsoft.com/")
    assert "not a verified Standard billing threshold" in (resolved.input_range_source.note or "")
    assert descriptor is not None and descriptor.input_range is None


@pytest.mark.parametrize("source,product,label", [
    (PriceSource.ANTHROPIC, "Azure OpenAI GPT5", "5.6 sol ShortCo Std"),
    (PriceSource.AZURE_RETAIL, "Other product", "5.6 sol ShortCo Std"),
    (PriceSource.AZURE_RETAIL, "Azure OpenAI GPT5", "5.6 sol ShortCo PP"),
    (PriceSource.AZURE_RETAIL, "Azure OpenAI GPT6", "6 astra ShortCo Std"),
])
def test_context_tier_rules_do_not_spread_a_threshold_to_other_scopes(
    source: PriceSource, product: str, label: str,
) -> None:
    descriptor = parse_context_tier(label)
    model = CatalogModel(
        key="unit", label=label, source=source, product=product, context_tier=descriptor,
    )
    assert model.resolved_context_tier is descriptor
    assert descriptor is not None and descriptor.input_range is None


@pytest.mark.parametrize("product,stem", [
    ("Azure OpenAI GPT5", "5.6 sol"),
    ("Azure OpenAI GPT6", "6 astra"),
    ("Any product", "New model"),
])
def test_azure_context_details_do_not_fetch_threshold_webpages(
    product: str, stem: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstile_core.pricing.context import resolve_context_prices

    def unexpected_http(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Threshold lookup must not request a webpage")

    monkeypatch.setattr(httpx, "get", unexpected_http)
    catalog = StubAzure([
        meter(f"{stem} {band} {slot} Std Gl 1M Tokens", price, product=product, unit="1M")
        for band, price in [("ShortCo", 7), ("LongCo", 19)] for slot in ["Inp", "Opt"]
    ])
    references = []
    for model in catalog.models():
        found = catalog.options(model)
        assert catalog.options(model) is found
        descriptor = found.model.resolved_context_tier
        assert descriptor is not None
        if product == "Azure OpenAI GPT5":
            assert descriptor.input_range is not None
            assert descriptor.input_range_source is not None
            assert descriptor.input_range_source.origin == "reference"
        else:
            assert descriptor.input_range is None
            assert descriptor.input_range_source is None
        option = found.options[0]
        assert option.entry.input_per_million == (7 if descriptor.key == "short" else 19)
        assert catalog.entry(option.reference) == option.entry
        references.append(option.reference)
    message = "unverified" if product == "Azure OpenAI GPT5" else "thresholds are unavailable"
    with pytest.raises(ValueError, match=message):
        resolve_context_prices(
            CompositeCatalog([catalog]), PriceSource.AZURE_RETAIL, references, 80,
        )


def test_context_tier_rules_do_not_override_explicit_source_ranges() -> None:
    descriptor = parse_context_tier("5.6 sol ShortCo Std")
    assert descriptor is not None
    descriptor.input_range = PriceCatalogInputRange(min_input_tokens=0, max_input_tokens=12345)
    model = CatalogModel(
        key="unit", label="5.6 sol ShortCo Std", source=PriceSource.AZURE_RETAIL,
        product="Azure OpenAI GPT5", context_tier=descriptor,
    )
    assert model.resolved_context_tier is descriptor
    assert model.resolved_context_tier.input_range is not None
    assert model.resolved_context_tier.input_range.max_input_tokens == 12345


@pytest.mark.parametrize("name", ["New Model 99", "Another Future Model"])
def test_custom_context_rule_uses_catalog_prices_and_preserves_rule_version(name: str) -> None:
    from turnstile_core.domain.runtime_models import ContextPriceRule, custom_context_rule
    from turnstile_core.pricing.context import model_price_from_tiers, resolve_context_prices

    catalog = CompositeCatalog([StubAzure([
        meter(f"{name} {band} {slot} Std Gl 1M Tokens", rate,
              product="Any product", unit="1M")
        for band, rate in [("ShortCo", 3), ("LongCo", 8)] for slot in ["Inp", "Opt"]
    ])])
    references = [f"azure_retail:Any product:{name} {band} Std:Global:*"
                  for band in ["ShortCo", "LongCo"]]
    rule = ContextPriceRule(short_context_max_tokens=12345)
    tiers = resolve_context_prices(catalog, PriceSource.AZURE_RETAIL, references, 80, rule=rule)
    assert custom_context_rule(tiers) == rule
    assert [tier.input_price_per_million for tier in tiers] == [2.4, 6.4]
    assert tiers[0].input_range_source == tiers[1].input_range_source
    source = tiers[0].input_range_source
    assert source is not None and source.origin == "custom"
    assert source.revision == rule.revision and source.configured_at is not None
    refreshed = resolve_context_prices(
        catalog, PriceSource.AZURE_RETAIL, references, 80, rule=rule, previous_tiers=tiers,
    )
    assert refreshed == tiers
    price = model_price_from_tiers(tiers)
    assert price.for_input_tokens(12345).selected_tier == "short"
    assert price.for_input_tokens(12346).selected_tier == "long"
    with pytest.raises(ValueError, match="thresholds are unavailable"):
        resolve_context_prices(catalog, PriceSource.AZURE_RETAIL, references, 80)


@pytest.mark.parametrize("threshold", [0, -1, True, 1.5, "12345", 9007199254740991])
def test_custom_context_rule_rejects_invalid_thresholds(threshold: Any) -> None:
    from turnstile_core.domain.runtime_models import ContextPriceRule

    with pytest.raises(ValueError):
        ContextPriceRule.model_validate({"short_context_max_tokens": threshold})


def test_context_tier_metadata_survives_the_catalog_index() -> None:
    catalog = StubAzure([
        meter(f"Any Model {band} Inp Std Gl 1M Tokens", rate, product="Any Product", unit="1M")
        for band, rate in [("ShortCo", 3), ("LongCo", 7)]
    ] + [
        meter(f"Any Model {band} Opt Std Gl 1M Tokens", rate, product="Any Product", unit="1M")
        for band, rate in [("ShortCo", 10), ("LongCo", 19)]
    ])
    models = catalog.models()
    assert len(models) == 2
    assert all(model.context_tier is not None for model in models)
    assert {model.context_tier.key for model in models if model.context_tier} == {"short", "long"}
    assert all(model.context_tier.input_range is None for model in models if model.context_tier)


def test_context_tier_metadata_can_express_source_defined_ranges() -> None:
    descriptor = PriceCatalogContextTier(
        group_key="source-model", model_label="Model", service_tier="flexible",
        key="medium", label="Medium context", order=1,
        input_range=PriceCatalogInputRange(min_input_tokens=12346, max_input_tokens=98765),
    )
    assert descriptor.input_range is not None
    assert descriptor.input_range.model_dump() == {
        "min_input_tokens": 12346, "max_input_tokens": 98765,
    }
    for lower, upper in [(10, 9), (-1, 10), (True, 10), (1, 1.5)]:
        with pytest.raises(ValueError):
            PriceCatalogInputRange.model_validate({
                "min_input_tokens": lower, "max_input_tokens": upper,
            })


def test_context_tier_metadata_makes_opaque_skus_searchable_by_model() -> None:
    model = CatalogModel(
        key="opaque:sku-42", label="SKU 42", product="Example product",
        source=PriceSource.ANTHROPIC,
        context_tier=PriceCatalogContextTier(
            group_key="example", model_label="A model name", service_tier="Flexible",
            key="medium", label="Medium input", order=1,
        ),
    )

    class MetadataCatalog(StubAzure):
        def models(self) -> list[CatalogModel]:
            return [model]

    catalog = CompositeCatalog([MetadataCatalog([])])
    assert catalog.search_models("A model name Flexible").models == (model,)
    assert catalog.search_models("SKU 42").models == (model,)


def test_context_prices_resolve_authoritatively_and_apply_one_discount_to_all_tiers() -> None:
    from turnstile_core.pricing.context import model_price_from_tiers, resolve_context_prices

    class MetadataAzure(StubAzure):
        def _build_index(self) -> list[CatalogModel]:
            models = list(super()._build_index())
            for model in models:
                descriptor = model.context_tier
                assert descriptor is not None
                descriptor.input_range = PriceCatalogInputRange(
                    min_input_tokens=0 if descriptor.key == "short" else 272001,
                    max_input_tokens=272000 if descriptor.key == "short" else None,
                )
            return models

    catalog = CompositeCatalog([MetadataAzure([
        meter(f"5.6 sol {band} {bucket} Std Gl 1M Tokens", price,
              product="Azure OpenAI GPT5", unit="1M")
        for band, rates in [("ShortCo", [4.0, 20.0, 0.4, 5.0]),
                            ("LongCo", [8.0, 30.0, 0.8, 10.0])]
        for bucket, price in zip(["Inp", "Opt", "Cd Inp", "Cd Wr"], rates, strict=True)
    ])])
    references = [f"azure_retail:Azure OpenAI GPT5:5.6 sol {band} Std:Global:*"
                  for band in ["LongCo", "ShortCo"]]
    tiers = resolve_context_prices(catalog, PriceSource.AZURE_RETAIL, references, 80)
    assert [tier.key for tier in tiers] == ["short", "long"]
    assert [tier.input_price_per_million for tier in tiers] == [3.2, 6.4]
    assert [tier.output_price_per_million for tier in tiers] == [16, 24]
    assert [tier.list_prices.input for tier in tiers] == [4, 8]
    price = model_price_from_tiers(tiers)
    assert price.for_input_tokens(272000).selected_tier == "short"
    assert price.for_input_tokens(272001).selected_tier == "long"
    with pytest.raises(ValueError, match="distinct"):
        resolve_context_prices(catalog, PriceSource.AZURE_RETAIL, [references[0]] * 2, 80)
    with pytest.raises(ValueError, match="source"):
        resolve_context_prices(catalog, PriceSource.ANTHROPIC, references, 80)


def test_a_shape_priced_the_same_everywhere_asks_for_no_region() -> None:
    found = options_for(GPT_41, GPT_41_MODEL)
    globals_ = [option for option in found.options if option.deployment == "Global"]
    assert len(globals_) == 1, "three regions charging one figure is one choice, not three"
    option = globals_[0]
    assert option.region_required is False
    assert option.reference.endswith(":Global:*")
    assert option.regions == ("brazilsouth", "canadaeast", "eastus")
    assert option.entry.input_per_million == pytest.approx(2.0)
    assert option.entry.output_per_million == pytest.approx(8.0)
    assert option.entry.cached_per_million == pytest.approx(0.5)


def test_a_shape_whose_price_varies_does_ask_for_a_region() -> None:
    found = options_for(GPT_41, GPT_41_MODEL)
    regional = [option for option in found.options if option.deployment == "Regional"]
    assert len(regional) == 2
    assert all(option.region_required for option in regional)
    assert {option.regions for option in regional} == {("eastus",), ("northeurope",)}
    assert sorted(option.entry.input_per_million or 0 for option in regional) == pytest.approx(
        [2.2, 2.4]
    )


def test_options_are_ordered_so_the_usual_choice_comes_first() -> None:
    found = options_for(GPT_41, GPT_41_MODEL)
    assert [option.deployment for option in found.options][0] == "Global"


def test_a_published_cache_write_rate_reaches_the_entry() -> None:
    """The registry charges cache writes separately, and GPT5/GPT6 publish the rate. Leaving it
    empty would silently fall back to the cached read rate, which is 12.5x too cheap here."""
    rows = [
        meter("5.6 sol ShortCo Inp Std Gl 1M Tokens", 4.0,
              product="Azure OpenAI GPT5", unit="1M"),
        meter("5.6 sol ShortCo Opt Std Gl 1M Tokens", 20.0,
              product="Azure OpenAI GPT5", unit="1M"),
        meter("5.6 sol ShortCo Cd Inp Std Gl 1M Tokens", 0.4,
              product="Azure OpenAI GPT5", unit="1M"),
        meter("5.6 sol ShortCo Cd Wr Std Gl 1M Tokens", 5.0,
              product="Azure OpenAI GPT5", unit="1M"),
    ]
    model = CatalogModel(
        key="azure_retail:Azure OpenAI GPT5:5.6 sol ShortCo Std",
        label="5.6 sol ShortCo Std",
        product="Azure OpenAI GPT5",
        source=PriceSource.AZURE_RETAIL,
    )
    entry = options_for(rows, model).options[0].entry
    assert entry.cached_per_million == pytest.approx(0.4)
    assert entry.cache_write_per_million == pytest.approx(5.0)


def test_every_region_in_a_group_keeps_its_own_reference() -> None:
    """Regions that charge alike are shown as one choice. What gets stored is still the region.

    Grouping is a display decision; storing the group's first region for someone who picked the
    third is a data decision, and it only looks harmless while the prices agree.
    """
    found = options_for(GPT_41, GPT_41_MODEL)
    regional = [option for option in found.options if option.deployment == "Regional"]
    assert regional
    for option in regional:
        assert set(option.references_by_region) == set(option.regions)
        for region, reference in option.references_by_region.items():
            assert reference.endswith(f":{region}")


def test_a_region_reference_survives_the_group_being_split_by_price() -> None:
    """The day the vendor prices two grouped regions apart, each stored reference has to resolve
    to its own region -- not to whichever one sorted first while they agreed."""
    together = [
        meter("gpt 4.1 Inp regnl Tokens", 0.002, region=region)
        for region in ("eastus", "westus")
    ] + [
        meter("gpt 4.1 Outp regnl Tokens", 0.008, region=region)
        for region in ("eastus", "westus")
    ]
    grouped = options_for(together, GPT_41_MODEL).options[0]
    assert grouped.regions == ("eastus", "westus")
    chosen = grouped.references_by_region["westus"]

    # Same catalogue, later: westus is repriced and the group splits.
    apart = [
        meter("gpt 4.1 Inp regnl Tokens", 0.002, region="eastus"),
        meter("gpt 4.1 Outp regnl Tokens", 0.008, region="eastus"),
        meter("gpt 4.1 Inp regnl Tokens", 0.004, region="westus"),
        meter("gpt 4.1 Outp regnl Tokens", 0.016, region="westus"),
    ]
    resolved = StubAzure(apart).entry(chosen)
    assert resolved is not None
    assert resolved.input_per_million == pytest.approx(4.0), (
        "the model follows the region that was chosen, not the one that sorted first"
    )


def test_a_partially_read_price_is_marked_as_such() -> None:
    """A page of the feed failing leaves rates that look published-and-absent rather than
    unread. The entry has to carry that, because the sync must refuse to write it."""
    found = StubAzure(GPT_41, complete=False).options(GPT_41_MODEL)
    assert found.complete is False
    assert all(option.entry.complete is False for option in found.options)
    assert found.note is not None


def test_a_fully_read_price_is_not_marked_partial() -> None:
    found = options_for(GPT_41, GPT_41_MODEL)
    assert found.complete is True
    assert all(option.entry.complete for option in found.options)


def test_a_meter_it_cannot_read_is_reported_not_dropped() -> None:
    rows = GPT_41 + [meter("gpt 4.1 weirdly-worded glbl Tokens", 0.003)]
    found = options_for(rows, GPT_41_MODEL)
    assert "gpt 4.1 weirdly-worded glbl Tokens" in found.unreadable


def test_a_meter_billed_in_something_else_says_so() -> None:
    rows = GPT_41 + [meter("gpt 4.1 hosting global Unit", 1.5, unit="1 Hour")]
    found = options_for(rows, GPT_41_MODEL)
    assert any("gpt 4.1 hosting global Unit" in item for item in found.other_meters)
    assert not found.unreadable


def test_per_million_meters_are_not_multiplied_again() -> None:
    rows = [
        meter("5.4 inp Gl 1M Tokens", 1.25, product="Azure OpenAI GPT5", unit="1M"),
        meter("5.4 opt Gl 1M Tokens", 10.0, product="Azure OpenAI GPT5", unit="1M"),
    ]
    model = CatalogModel(
        key="azure_retail:Azure OpenAI GPT5:5.4",
        label="5.4",
        product="Azure OpenAI GPT5",
        source=PriceSource.AZURE_RETAIL,
    )
    option = options_for(rows, model).options[0]
    assert option.entry.input_per_million == pytest.approx(1.25)
    assert option.entry.output_per_million == pytest.approx(10.0)


# --------------------------------------------------------------------------------------------
# The index and the search
# --------------------------------------------------------------------------------------------


def test_the_index_lists_each_model_once_and_only_if_it_can_be_priced() -> None:
    rows = GPT_41 + [
        # Output only: cannot price a request, so it is not offered.
        meter("gpt 4.1 orphan Outp glbl Tokens", 0.01),
    ]
    models = list(StubAzure(rows).models())
    assert [model.label for model in models] == ["gpt 4.1"]


def test_a_term_never_matches_a_fragment_of_another_number() -> None:
    rows = [
        meter("gpt 5 pro inp glbl Tokens", 0.015, product="Azure OpenAI GPT5"),
        meter("gpt 5 pro out glbl Tokens", 0.12, product="Azure OpenAI GPT5"),
        meter("gpt 4o 0513 Input global Tokens", 0.005),
        meter("gpt 4o 0513 Output global Tokens", 0.015),
    ]
    found = CompositeCatalog([StubAzure(rows)]).search_models("gpt 5")
    labels = [model.label for model in found.models]
    assert labels == ["gpt 5 pro"], labels


def test_every_typed_term_has_to_land() -> None:
    rows = GPT_41 + [
        meter("gpt 4.1 mini Inp glbl Tokens", 0.0004),
        meter("gpt 4.1 mini Outp glbl Tokens", 0.0016),
    ]
    found = CompositeCatalog([StubAzure(rows)]).search_models("gpt 4.1 mini")
    assert [model.label for model in found.models] == ["gpt 4.1 mini"]


def test_the_typed_words_go_into_the_api_filter() -> None:
    """Filtering locally meant guessing a product name, and the guess excluded gpt-5."""
    catalog = StubAzure(GPT_41)
    catalog.options(GPT_41_MODEL)
    sent = catalog.filters[-1]
    assert "serviceName eq 'Foundry Models'" in sent
    assert "productName eq 'Azure OpenAI'" in sent
    assert "contains(meterName,'gpt') and contains(meterName,'4.1')" in sent


def test_the_filter_does_not_assume_the_name_survives_as_one_run_of_text() -> None:
    """`6-astra LongCo Opt Std DZ 1M Tokens` puts the bucket word inside the model's name.

    Asking for the words joined back together matched nothing there, so gpt-6-astra appeared in
    the picker and then offered no prices at all -- which reads as "Azure does not publish a
    price for this", the one conclusion that was not true.
    """
    rows = [
        meter("6-astra ShortCo Inp Std Gl 1M Tokens", 10.0,
              product="Azure OpenAI GPT6", unit="1M"),
        meter("6-astra ShortCo Opt Std Gl 1M Tokens", 50.0,
              product="Azure OpenAI GPT6", unit="1M"),
    ]
    model = CatalogModel(
        key="azure_retail:Azure OpenAI GPT6:6 astra ShortCo Std",
        label="6 astra ShortCo Std",
        product="Azure OpenAI GPT6",
        source=PriceSource.AZURE_RETAIL,
    )
    catalog = StubAzure(rows)
    found = catalog.options(model)
    sent = catalog.filters[-1]

    assert "contains(meterName,'6 astra ShortCo Std')" not in sent
    assert found.options, "the model must reach a price, not just a name"
    assert found.options[0].entry.input_per_million == pytest.approx(10.0)


def test_an_unreachable_source_is_named_rather_than_pretended_empty() -> None:
    class Broken:
        source = PriceSource.ANTHROPIC

        def models(self) -> list[CatalogModel]:
            raise ValueError("pricing page moved")

        def options(self, model: CatalogModel) -> CatalogOptions:
            raise ValueError("pricing page moved")

        def entry(self, reference: str) -> None:
            return None

    found = CompositeCatalog([Broken(), StubAzure(GPT_41)]).search_models("gpt 4.1")
    assert [model.label for model in found.models] == ["gpt 4.1"]
    assert found.unavailable == (str(PriceSource.ANTHROPIC),)


@pytest.mark.parametrize("error", [
    httpx.ReadTimeout("options unavailable"),
    ValueError("options unavailable"),
])
def test_known_model_options_preserve_source_failures(error: Exception) -> None:
    class BrokenOptions(StubAzure):
        def options(self, model: CatalogModel) -> CatalogOptions:
            raise error

    catalog = CompositeCatalog([BrokenOptions(GPT_41)])
    assert catalog.search_models("gpt 4.1").models == (GPT_41_MODEL,)
    with pytest.raises(type(error), match="options unavailable"):
        catalog.options(GPT_41_MODEL.key)


# --------------------------------------------------------------------------------------------
# Resolving a stored mapping
# --------------------------------------------------------------------------------------------


def test_a_stored_reference_resolves_back_to_the_same_rates() -> None:
    catalog = StubAzure(GPT_41)
    chosen = next(
        option for option in catalog.options(GPT_41_MODEL).options
        if option.deployment == "Global"
    )
    resolved = catalog.entry(chosen.reference)
    assert resolved is not None
    assert resolved.input_per_million == chosen.entry.input_per_million
    assert resolved.output_per_million == chosen.entry.output_per_million


def test_a_region_pinned_reference_resolves_to_that_region_only() -> None:
    catalog = StubAzure(GPT_41)
    northeurope = next(
        option for option in catalog.options(GPT_41_MODEL).options
        if option.regions == ("northeurope",)
    )
    resolved = catalog.entry(northeurope.reference)
    assert resolved is not None
    assert resolved.input_per_million == pytest.approx(2.4)


def test_a_reference_that_no_longer_exists_resolves_to_nothing() -> None:
    catalog = StubAzure(GPT_41)
    assert catalog.entry("azure_retail:Azure OpenAI:gpt 4.1:Regional:antarctica") is None
    assert catalog.entry("nonsense") is None
