from __future__ import annotations

from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr

from backend.services.model_pricing import ModelPricingService
from backend.services.runtime_service import ModelRuntimeService
from turnstile_core.config import Settings
from turnstile_core.domain.runtime_models import ManagedModelWrite, PricePreviewRequest, PriceSource
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.pricing.catalog import CompositeCatalog
from turnstile_core.pricing.matching import match_model
from turnstile_core.pricing.models_dev import ModelsDevCatalog, parse_reference, reference_for


def document(**overrides: Any) -> dict[str, Any]:
    return {"openai": {"name": "OpenAI", "models": {"gpt-5": {
        "id": "gpt-5", "name": "GPT-5", "canonical_model_id": "openai/gpt-5",
        "cost": {"input": 1.25, "output": 10, "cache_read": 0},
        "limit": {"context": 400000}, "modalities": {"output": ["text"]},
        **overrides,
    }}}}


def catalog(**overrides: Any) -> ModelsDevCatalog:
    return ModelsDevCatalog(fetch=lambda: document(**overrides))


def image_document(**overrides: Any) -> dict[str, Any]:
    return {"azure": {"name": "Azure", "models": {"gpt-image-2.5-flare": {
        "id": "gpt-image-2.5-flare", "name": "GPT Image 2.5 Flare",
        "canonical_model_id": "openai/gpt-image-2.5-flare",
        "cost": {"input": 5, "output": 30, "cache_read": 1.25},
        "modalities": {"input": ["text", "image"], "output": ["image"]},
        "limit": {"context": 0, "output": 0}, **overrides,
    }}}}


def image_service(**overrides: Any) -> tuple[ModelRuntimeService, dict[str, Any]]:
    runtime, _, model = service()
    model.update(
        upstream_model_id="gpt-image-2.5-flare", display_name="GPT Image 2.5 Flare",
        capabilities=["image_generation"], context_window=None,
        input_cost_per_million=5, output_cost_per_million=30, cached_cost_per_million=1.25,
    )
    runtime._price_catalog = CompositeCatalog([
        ModelsDevCatalog(fetch=lambda: image_document(**overrides)),
    ])
    return runtime, model


def service() -> tuple[ModelRuntimeService, InMemoryRepository, dict[str, Any]]:
    repository = InMemoryRepository()
    model = repository.models[0]
    model.update(
        upstream_model_id="gpt-5", display_name="GPT-5",
        input_cost_per_million=None, output_cost_per_million=None,
        cached_cost_per_million=None, cache_write_cost_per_million=None,
    )
    for key in ("price_source", "price_reference", "price_source_configured"):
        model.pop(key, None)
    settings = Settings(credential_encryption_key=SecretStr(Fernet.generate_key().decode()))
    runtime = ModelRuntimeService(
        repository, settings, price_catalog=CompositeCatalog([catalog()]),
    )
    return runtime, repository, model


def test_reference_is_reversible_for_slashes_colons_and_unicode() -> None:
    reference = reference_for("provider:name", "vendor/model:版本")
    assert parse_reference(reference) == ("provider:name", "vendor/model:版本")
    assert parse_reference("models_dev:openai") is None


def test_catalog_maps_context_zero_and_missing_without_rescaling() -> None:
    entry = catalog().entry("models_dev:openai:gpt-5")
    assert entry is not None and entry.context_window == 400000
    assert entry.input_per_million == 1.25
    assert entry.cached_per_million == 0
    assert entry.cache_write_per_million is None
    assert len(str(entry.metadata["entry_digest"])) == 64


@pytest.mark.parametrize("cost", [
    {"input": True, "output": 10},
    {"input": -1, "output": 10},
    {"input": 1, "output": float("inf")},
    {"input": 1, "output": 10, "cache_read": "0.1"},
])
def test_bad_rates_are_not_complete(cost: dict[str, Any]) -> None:
    entry = catalog(cost=cost).entry("models_dev:openai:gpt-5")
    assert entry and not entry.complete


@pytest.mark.parametrize("overrides", [
    {"cost": {"input": 1, "output": 10, "tiers": [{"tier": {"size": 100}}]}},
    {"modalities": {"output": ["image"]}},
    {"status": "deprecated"},
])
def test_special_pricing_is_not_automatically_usable(overrides: dict[str, Any]) -> None:
    entry = catalog(**overrides).entry("models_dev:openai:gpt-5")
    assert entry and entry.unsupported


def test_cached_snapshot_is_not_refetched_for_every_model() -> None:
    calls: list[int] = []

    def fetch() -> dict[str, Any]:
        calls.append(1)
        return document()

    public = ModelsDevCatalog(fetch=fetch)
    public.entries()
    public.entries()
    assert len(calls) == 1
    public.refresh()
    assert len(calls) == 2


def test_failed_refresh_does_not_replace_previous_snapshot() -> None:
    public = catalog()
    original = public.entries()

    def broken() -> dict[str, Any]:
        raise ValueError("invalid catalog")

    public._fetch = broken
    with pytest.raises(ValueError):
        public.refresh()
    assert public._entries is not None
    assert tuple(public._entries.values()) == original
    with pytest.raises(ValueError):
        public.entries()


def test_exact_name_prefers_actual_provider_and_never_calls_ai() -> None:
    payload = document()
    payload["azure"] = {"name": "Azure", "models": payload["openai"]["models"]}
    public = ModelsDevCatalog(fetch=lambda: payload)

    def forbidden(prompt: str) -> str:
        raise AssertionError("Exact match must not incur AI usage")

    result = match_model(
        public.entries(), ["GPT-5"], brand="microsoft_foundry", kind="microsoft_foundry",
        judge=forbidden,
    )
    assert result.entry and result.entry.provider_id == "azure"
    assert result.price_basis == "deployment"


def test_unknown_serving_provider_uses_explicit_origin_reference() -> None:
    result = match_model(catalog().entries(), ["gpt-5"], brand="generic", kind="foundry")
    assert result.entry and result.price_basis == "origin_reference"


def test_family_similarity_does_not_erase_version_or_variant() -> None:
    result = match_model(
        catalog().entries(), ["gpt-5-mini"], brand="generic", kind="generic",
        judge=lambda _: '{"decision":"match","candidate_id":"models_dev:openai:gpt-5"}',
    )
    assert result.entry is None


def test_ai_can_resolve_a_verifiable_prefix_but_not_invent_ids() -> None:
    public = catalog()
    valid = match_model(
        public.entries(), ["FW GPT 5"], brand="generic", kind="generic",
        judge=lambda _: '{"decision":"match","candidate_id":"models_dev:openai:gpt-5"}',
    )
    assert valid.entry and valid.method == "ai"
    invalid = match_model(
        public.entries(), ["FW GPT 5"], brand="generic", kind="generic",
        judge=lambda _: '{"decision":"match","candidate_id":"models_dev:other:fiction"}',
    )
    assert invalid.entry is None and invalid.status == "ambiguous"


def test_preview_is_read_only_and_fills_context() -> None:
    runtime, repository, model = service()
    before = dict(model)
    preview = runtime.price_preview(PricePreviewRequest(model_id=model["id"]), user_id="owner")
    assert preview.status == "matched" and preview.context_window == 400000
    assert model == before
    assert repository.registry()["models"][0]["price_source_configured"] is False


def test_changing_source_does_not_inherit_the_previous_source_reference() -> None:
    runtime, _, model = service()
    model.update(
        price_source="azure_retail", price_reference="azure_retail:old:gpt:Global:*",
    )
    preview = runtime.price_preview(PricePreviewRequest(
        model_id=model["id"], price_source=PriceSource.MODELS_DEV,
    ), user_id="owner")
    assert preview.status == "matched"
    assert preview.match and preview.match.reference == "models_dev:openai:gpt-5"


def test_bulk_default_source_is_saved_and_second_run_is_unchanged() -> None:
    runtime, _, model = service()
    context = model["context_window"]
    first = runtime.sync_prices([model["id"]])
    assert first.updated == 1 and first.total == 1
    assert model["price_reference"] == "models_dev:openai:gpt-5"
    assert model["context_window"] == context
    second = runtime.sync_prices([model["id"]])
    assert second.unchanged == 1 and second.updated == 0


def test_manual_skip_never_downloads_or_changes_prices() -> None:
    runtime, _, model = service()
    model["price_source"] = "manual"
    model["input_cost_per_million"] = 7
    public = runtime._price_catalog.public_catalog()
    public._fetch = lambda: pytest.fail("Manual model requested the catalog")
    result = runtime.sync_prices([model["id"]], refresh=True)
    assert result.skipped_manual == 1 and result.considered == 0
    assert model["input_cost_per_million"] == 7


def test_first_sync_drift_needs_review_repeatedly() -> None:
    runtime, _, model = service()
    model["input_cost_per_million"] = 0.1
    model["output_cost_per_million"] = 1
    for _ in range(2):
        result = runtime.sync_prices([model["id"]])
        assert result.review_needed == 1 and result.updated == 0
        assert model["input_cost_per_million"] == 0.1


def test_missing_cache_write_preserves_existing_value() -> None:
    runtime, _, model = service()
    model["cache_write_cost_per_million"] = 2
    result = runtime.sync_prices([model["id"]])
    assert result.updated == 1 and result.partial_fields == 1
    assert model["cache_write_cost_per_million"] == 2
    assert model["cached_cost_per_million"] == 0


def test_dry_run_does_not_mutate_configuration() -> None:
    runtime, _, model = service()
    before = dict(model)
    result = runtime.sync_prices([model["id"]], dry_run=True)
    assert result.dry_run and result.updated == 1
    assert model == before


def test_save_uses_catalog_prices_and_rejects_changed_digest() -> None:
    runtime, _, model = service()
    preview = runtime.price_preview(PricePreviewRequest(model_id=model["id"]), user_id="owner")
    assert preview.match is not None
    write = ManagedModelWrite.model_validate({
        **{key: model[key] for key in (
            "provider_id", "runtime_id", "model_key", "display_name",
        )}, "price_source": "models_dev", "price_reference": preview.match.reference,
        "price_entry_digest": preview.entry_digest, "input_cost_per_million": 999,
    })
    runtime.save_model(write, model["id"])
    assert model["input_cost_per_million"] == 1.25
    with pytest.raises(HTTPException) as failure:
        runtime.save_model(write.model_copy(update={"price_entry_digest": "wrong"}), model["id"])
    assert failure.value.status_code == 409


def test_generated_pricing_contract_matches_runtime() -> None:
    import json
    from pathlib import Path

    from scripts.export_pricing_contract import export

    root = Path(__file__).resolve().parents[3]
    assert json.loads((root / "contracts/openapi/schemas/pricing.json").read_text()) == export()


def test_exhausted_ai_budget_does_not_disable_exact_matches() -> None:
    runtime, _, model = service()
    pricing = ModelPricingService(
        runtime.registry(), runtime._price_catalog, runtime.invoke, user_id="owner",
    )
    pricing.ai_calls = 20
    assert pricing.preview(PricePreviewRequest(model_id=model["id"])).status == "matched"
    assert pricing.preview(PricePreviewRequest(
        model_id=model["id"], deployment_name="FW GPT 5",
        display_name="FW GPT 5", model_key="fw-gpt-5",
    )).status == "deferred"


def test_one_operation_uses_a_frozen_snapshot() -> None:
    runtime, _, model = service()
    pricing = ModelPricingService(
        runtime.registry(), runtime._price_catalog, runtime.invoke, user_id="owner",
    )
    first = pricing.preview(PricePreviewRequest(model_id=model["id"]))
    public = runtime._price_catalog.public_catalog()
    public._fetch = lambda: document(cost={"input": 2, "output": 12})
    public.refresh()
    second = pricing.preview(PricePreviewRequest(model_id=model["id"]))
    assert first.list_prices and second.list_prices
    assert first.list_prices.input == second.list_prices.input == 1.25


def test_model_edit_during_price_lookup_is_not_overwritten() -> None:
    runtime, repository, model = service()

    def fetch() -> dict[str, Any]:
        repository.update_registry_item(
            "model", model["id"], {"price_source": "manual", "input_cost_per_million": 9},
        )
        return document()

    runtime._price_catalog.public_catalog()._fetch = fetch
    write = ManagedModelWrite.model_validate({
        **{key: model[key] for key in (
            "provider_id", "runtime_id", "model_key", "display_name",
        )}, "price_source": "models_dev", "price_reference": "models_dev:openai:gpt-5",
    })
    with pytest.raises(HTTPException) as failure:
        runtime.save_model(write, model["id"])
    assert failure.value.status_code == 409
    assert model["price_source"] == "manual" and model["input_cost_per_million"] == 9


def test_catalog_timeout_is_bounded_by_operation_deadline() -> None:
    import time

    from turnstile_core.pricing.catalog import CATALOG_DEADLINE, price_request_timeout

    token = CATALOG_DEADLINE.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError):
            price_request_timeout(15)
    finally:
        CATALOG_DEADLINE.reset(token)
    assert price_request_timeout(15) == 15


@pytest.mark.parametrize("outputs", [["image"], ["text", "image"]])
def test_image_preview_save_and_bulk_use_image_token_prices(outputs: list[str]) -> None:
    runtime, model = image_service(modalities={"output": outputs})
    before = dict(model)
    preview = runtime.price_preview(PricePreviewRequest(model_id=model["id"]), user_id="owner")
    assert model == before
    assert preview.status == "matched" and preview.match
    assert preview.match.reference == "models_dev:azure:gpt-image-2.5-flare"
    assert preview.context_window is None
    assert preview.effective_prices
    assert preview.effective_prices.model_dump() == {
        "input": 5, "output": 30, "cached": 1.25, "cache_write": None,
    }
    write = ManagedModelWrite.model_validate({
        **{key: value for key, value in model.items() if key in ManagedModelWrite.model_fields},
        "price_source": "models_dev", "price_reference": preview.match.reference,
        "price_entry_digest": preview.entry_digest,
    })
    runtime.save_model(write, model["id"])
    runtime._price_catalog.public_catalog()._fetch = lambda: image_document(
        cost={"input": 5, "output": 33, "cache_read": 1.25},
        modalities={"output": outputs},
    )
    result = runtime.sync_prices([model["id"]], refresh=True)
    assert result.updated == 1
    assert model["output_cost_per_million"] == 33
    assert model["context_window"] is None and model["cache_write_cost_per_million"] is None
    assert runtime.sync_prices([model["id"]]).unchanged == 1


def test_new_image_preview_and_unconfigured_image_batch_are_supported() -> None:
    runtime, model = image_service()
    preview = runtime.price_preview(PricePreviewRequest(
        runtime_id=model["runtime_id"], deployment_name="gpt-image-2.5-flare",
        operation="image_generation",
    ), user_id="owner")
    assert preview.status == "matched"
    assert runtime.sync_prices([model["id"]]).unchanged == 1
    assert model["price_source"] == "models_dev"
    assert model["price_reference"] == "models_dev:azure:gpt-image-2.5-flare"


def test_image_reference_cannot_price_a_chat_model_or_the_reverse() -> None:
    runtime, model = image_service()
    reference = "models_dev:azure:gpt-image-2.5-flare"
    assert runtime.price_preview(PricePreviewRequest(
        runtime_id=model["runtime_id"], deployment_name="gpt-image-2.5-flare",
        price_reference=reference, operation="chat",
    ), user_id="owner").status == "unsupported"
    runtime._price_catalog = CompositeCatalog([catalog()])
    assert runtime.price_preview(PricePreviewRequest(
        model_id=model["id"], price_reference="models_dev:openai:gpt-5",
    ), user_id="owner").status == "unsupported"
    model.update(price_source="models_dev", price_reference="models_dev:openai:gpt-5")
    assert runtime.sync_prices([model["id"]]).unsupported == 1
    assert model["output_cost_per_million"] == 30


def test_missing_image_cache_price_preserves_existing_but_cannot_price_a_new_image() -> None:
    runtime, model = image_service(cost={"input": 5, "output": 30})
    existing = runtime.price_preview(PricePreviewRequest(model_id=model["id"]), user_id="owner")
    assert existing.status == "matched" and existing.effective_prices
    assert existing.effective_prices.cached == 1.25 and existing.warnings
    new = runtime.price_preview(PricePreviewRequest(
        runtime_id=model["runtime_id"], deployment_name="gpt-image-2.5-flare",
        operation="image_generation",
    ), user_id="owner")
    assert new.status == "unmapped"


def test_image_matching_does_not_silently_drop_a_variant() -> None:
    runtime, model = image_service()
    preview = runtime.price_preview(PricePreviewRequest(
        model_id=model["id"], deployment_name="gpt-image-2.5-sunburst",
        display_name="GPT Image 2.5 Sunburst", model_key="gpt-image-2.5-sunburst",
    ), user_id="owner")
    assert preview.status != "matched"


def test_manual_image_batch_never_reads_public_catalog() -> None:
    runtime, model = image_service()
    model["price_source"] = "manual"
    runtime._price_catalog.public_catalog()._fetch = lambda: pytest.fail("Manual image fetch")
    assert runtime.sync_prices([model["id"]], refresh=True).skipped_manual == 1


def test_batch_rejects_a_reference_from_a_different_configured_source() -> None:
    runtime, _, model = service()
    model.update(price_source="azure_retail", price_reference="models_dev:openai:gpt-5")
    assert runtime.sync_prices([model["id"]]).unsupported == 1
    assert model["input_cost_per_million"] is None
