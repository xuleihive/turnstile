from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from turnstile_core.domain.runtime_models import PriceSource
from turnstile_core.pricing.catalog import CatalogEntry, CompositeCatalog
from turnstile_core.pricing.matching import match_model
from turnstile_core.pricing.models_dev import ModelsDevCatalog


def document(cost: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "openai": {
            "name": "OpenAI",
            "models": {
                "gpt-example": {
                    "name": "GPT Example",
                    "modalities": {"output": ["text"]},
                    "cost": cost
                    if cost is not None
                    else {"input": 2, "output": 8, "cache_read": 0},
                    "limit": {"context": 128000},
                }
            },
        }
    }


def test_reference_prices_preserve_zero_and_provenance() -> None:
    catalog = ModelsDevCatalog(fetch=document)
    entry = catalog.entry("models_dev:openai:gpt-example")
    assert entry is not None and entry.input_per_million == 2
    assert entry.cached_per_million == 0 and entry.cache_write_per_million is None
    assert entry.metadata["provider_id"] == "openai"
    assert len(entry.digest) == 64


@pytest.mark.parametrize("name", ["gpt-5", "FW GPT 5"])
def test_provider_preference_never_replaces_a_model_with_a_canonical_variant(name: str) -> None:
    payload = {
        "openai": {
            "models": {
                "gpt-5": {
                    "name": "GPT 5",
                    "canonical_model_id": "openai/gpt-5",
                    "cost": {"input": 1, "output": 2},
                    "modalities": {"output": ["text"]},
                }
            }
        },
        "azure": {
            "models": {
                "gpt-5-codex": {
                    "name": "GPT 5 Codex",
                    "canonical_model_id": "openai/gpt-5",
                    "cost": {"input": 9, "output": 20},
                    "modalities": {"output": ["text"]},
                }
            }
        },
    }
    entries = ModelsDevCatalog(fetch=lambda: payload).entries()
    result = match_model(
        entries, [name], brand="azure_openai", kind="openai",
        judge=lambda _: '{"decision":"match","candidate_id":"models_dev:openai:gpt-5"}',
    )
    assert result.entry is None or result.entry.reference == "models_dev:openai:gpt-5"
    assert result.entry is None or result.entry.input_per_million == 1


@pytest.mark.parametrize(
    "cost",
    [
        {"input": True, "output": 8},
        {"input": -1, "output": 8},
        {"input": 2, "output": float("inf")},
        {"input": 2, "output": 8, "tiers": [{"limit": 200000}]},
    ],
)
def test_unsupported_prices_are_not_offered(cost: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="No supported"):
        ModelsDevCatalog(fetch=lambda: document(cost)).models()


def test_source_selection_never_downloads_another_catalog() -> None:
    catalog = ModelsDevCatalog(fetch=lambda: pytest.fail("Unexpected public catalog read"))
    combined = CompositeCatalog([catalog])
    assert combined.search_models("gpt", source=PriceSource.AZURE_RETAIL).models == ()
    assert combined.lookup("azure_retail:example") is None


def test_digest_changes_only_when_the_quote_changes() -> None:
    catalog = ModelsDevCatalog(fetch=document)
    original = catalog.entries()[0].digest
    catalog.refresh()
    assert catalog.entries()[0].digest == original
    catalog._fetch = lambda: document({"input": 3, "output": 8})
    catalog.refresh()
    assert catalog.entries()[0].digest != original


def matching_entry(provider: str, model: str, price: float = 2) -> CatalogEntry:
    return CatalogEntry(
        reference=f"models_dev:{provider}:{model}",
        label=model,
        source=PriceSource.MODELS_DEV,
        input_per_million=price,
        output_per_million=price * 2,
        metadata={
            "provider_id": provider,
            "model_id": model,
            "canonical_model_id": f"openai/{model}",
        },
    )


def test_exact_matching_prefers_serving_provider_without_ai() -> None:
    entries = [matching_entry("openai", "gpt-5", 1), matching_entry("azure", "gpt-5", 9)]
    result = match_model(
        entries,
        ["gpt-5-foundry-1234abcd"],
        brand="microsoft_foundry",
        kind="microsoft_foundry",
        judge=lambda _: pytest.fail("Exact matching invoked AI"),
    )
    assert result.entry is entries[1] and result.method == "exact"


def test_primary_model_identity_wins_over_a_generic_display_name() -> None:
    entries = [matching_entry("openai", "gpt-5"), matching_entry("openai", "gpt-5-mini")]
    result = match_model(entries, ["gpt-5-mini", "GPT 5"], brand="openai", kind="generic")
    assert result.entry is entries[1]


@pytest.mark.parametrize("identity", ["gpt-5-mini", "gpt-5-2026-01-01"])
def test_missing_primary_identity_never_falls_back_to_a_generic_display_name(identity: str) -> None:
    result = match_model(
        [matching_entry("openai", "gpt-5")],
        [identity, "GPT 5"],
        brand="openai",
        kind="generic",
    )
    assert result.entry is None


@pytest.mark.parametrize("model_id", ["gpt-5-codex", "gpt-5-chat-latest"])
@pytest.mark.parametrize("label", [None, "GPT 5"])
def test_ai_cannot_add_a_variant_hidden_by_a_display_label(
    model_id: str,
    label: str | None,
) -> None:
    entry = matching_entry("openai", model_id)
    entry = replace(entry, label=label or entry.label)
    result = match_model(
        [entry],
        ["FW GPT 5"],
        brand="openai",
        kind="generic",
        judge=lambda _: f'{{"decision":"match","candidate_id":"{entry.reference}"}}',
    )
    assert result.entry is None


def test_catalog_display_label_never_broadens_its_model_identity() -> None:
    entry = replace(matching_entry("openai", "gpt-5-codex"), label="GPT 5")
    result = match_model([entry], ["gpt-5"], brand="openai", kind="generic")
    assert result.entry is None


@pytest.mark.parametrize("name", ["FW GPT 5 mini", "FW GPT 5.1", "FW GPT 5 2026 01 01"])
def test_ai_cannot_drop_variants_versions_or_dates(name: str) -> None:
    result = match_model(
        [matching_entry("openai", "gpt-5")],
        [name],
        brand="openai",
        kind="generic",
        judge=lambda _: '{"decision":"match","candidate_id":"models_dev:openai:gpt-5"}',
    )
    assert result.entry is None


def test_ai_can_only_choose_an_existing_candidate_and_never_its_price() -> None:
    entry = matching_entry("openai", "gpt-5", 7)
    result = match_model(
        [entry],
        ["FW GPT 5"],
        brand="openai",
        kind="generic",
        judge=lambda _: '{"decision":"match","candidate_id":"models_dev:openai:gpt-5","input":999}',
    )
    assert result.entry is entry and result.entry.input_per_million == 7
    invalid = match_model(
        [entry],
        ["FW GPT 5"],
        brand="openai",
        kind="generic",
        judge=lambda _: '{"decision":"match","candidate_id":"invented"}',
    )
    assert invalid.entry is None


def test_oversized_candidate_names_do_not_trigger_ai() -> None:
    entry = matching_entry("openai", "gpt-5")
    from dataclasses import replace

    entry = replace(entry, label="GPT 5 " + "model " * 4000)
    result = match_model(
        [entry],
        ["FW GPT 5"],
        brand="openai",
        kind="generic",
        judge=lambda _: pytest.fail("Oversized matching prompt invoked AI"),
    )
    assert result.entry is None


def test_image_catalog_and_matching_never_use_chat_prices() -> None:
    payload = {
        "azure": {
            "name": "Azure",
            "models": {
                "gpt-image-2.5-flare": {
                    "name": "GPT Image 2.5 Flare",
                    "canonical_model_id": "openai/gpt-image-2.5-flare",
                    "cost": {"input": 5, "output": 30, "cache_read": 1.25},
                    "modalities": {"output": ["image"]},
                    "limit": {"context": 128000},
                }
            },
        }
    }
    catalog = ModelsDevCatalog(fetch=lambda: payload)
    image = match_model(
        catalog.entries(),
        ["gpt-image-2.5-flare"],
        brand="microsoft_foundry",
        kind="microsoft_foundry",
        operation="image_generation",
    )
    assert image.entry and image.entry.text_to_image
    assert image.entry.cached_per_million == 1.25
    assert image.entry.cache_write_per_million is None
    assert image.entry.metadata["context_window"] is None
    chat = match_model(
        catalog.entries(),
        ["gpt-image-2.5-flare"],
        brand="microsoft_foundry",
        kind="microsoft_foundry",
    )
    assert chat.entry is None
