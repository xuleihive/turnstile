from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from tests.backend.model_platform.control_plane_support import foundry_publication
from tests.platform.pricing.test_models_dev import image_service, service
from tests.platform.pricing.test_price_sync import context_model
from turnstile_core.domain.control_plane import ModelTarget
from turnstile_core.domain.runtime_models import (
    ManagedModelWrite,
    PricePreviewRequest,
    PriceSource,
    ProviderTarget,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.pricing.catalog import CatalogEntry
from turnstile_core.pricing.context import context_price_reference
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    GatewayControlPlaneService,
)


def test_service_sync_preserves_complete_context_tiers_and_reports_changes() -> None:
    runtime, _, row = service()
    tiers = context_model().context_price_tiers
    row.update(
        price_source="azure_retail",
        price_reference=context_price_reference(
            PriceSource.AZURE_RETAIL, [tier.reference for tier in tiers],
        ),
        context_price_tiers=[tier.model_dump(mode="json") for tier in tiers],
    )
    updated = [tier.model_copy(deep=True) for tier in tiers]
    for tier in updated:
        assert tier.list_prices.input is not None
        tier.list_prices.input *= 1.05
        tier.input_price_per_million *= 1.05
    with patch("turnstile_core.pricing.sync.resolve_context_prices", return_value=updated):
        result = runtime.sync_prices([row["id"]])
        assert result.updated == 1 and result.review_needed == 0
        assert runtime.sync_prices([row["id"]]).unchanged == 1
    saved = next(model for model in result.registry.models if model.id == row["id"])
    assert saved.context_price_tiers == updated
    assert saved.input_cost_per_million is None and saved.output_cost_per_million is None
    assert saved.context_window == row["context_window"]


def test_direct_public_publication_requires_explicit_unpriced_confirmation() -> None:
    repository = InMemoryRepository()
    publisher = GatewayControlPlaneService(repository, apim_principal_id="unit-principal")
    write = foundry_publication("public-unpriced-model")
    write.provider = ProviderTarget(existing_id=next(
        row["id"] for row in repository.providers if row["brand_key"] == "microsoft_foundry"
    ))
    write.model.price_source = PriceSource.MODELS_DEV
    with pytest.raises(ControlPlaneConflictError, match="explicitly confirm"):
        publisher.publish(write, "owner")
    write.model.allow_unpriced = True
    with patch.object(publisher._price_catalog, "lookup") as lookup:
        publication = publisher.publish(write, "owner")
        lookup.assert_not_called()
    model = publication.desired_spec.bindings[-1].model
    assert model.price_source is PriceSource.MODELS_DEV and model.price_reference is None


def test_direct_public_quote_is_operation_checked_and_snapshot_guarded() -> None:
    publisher = GatewayControlPlaneService(InMemoryRepository(), apim_principal_id="unit")
    entry = CatalogEntry(
        reference="models_dev:openai:gpt-example", label="GPT Example",
        source=PriceSource.MODELS_DEV, provider_id="openai", model_id="gpt-example",
        input_per_million=2, output_per_million=8, context_window=128000,
    )
    model = ModelTarget(
        model_key="gpt-example", display_name="GPT Example", upstream_model_id="gpt-example",
        price_source=PriceSource.MODELS_DEV, price_reference=entry.reference,
    )
    with patch.object(publisher._price_catalog, "lookup", return_value=entry):
        with pytest.raises(ControlPlaneConflictError, match="prices changed"):
            publisher._price_model(model, {}, expected_digest="old-quote")
        accepted = publisher._price_model(model, {}, expected_digest=entry.digest)
        assert accepted.input_cost_per_million == 2
        assert accepted.price_configuration
        assert accepted.price_configuration["source_snapshot"]["entry_digest"] == entry.digest
    for invalid in (replace(entry, operation="image_generation"),
                    replace(entry, unsupported="Not token-priced")):
        with (
            patch.object(publisher._price_catalog, "lookup", return_value=invalid),
            pytest.raises(ControlPlaneConflictError, match="incomplete or unavailable"),
        ):
            publisher._price_model(model, {})


def test_repeated_accepted_public_quote_reuses_publication() -> None:
    runtime, _, row = service()
    preview = runtime.price_preview(PricePreviewRequest(model_id=row["id"]), user_id="owner")
    assert preview.match is not None
    reference = preview.match.reference
    repository = InMemoryRepository()
    publisher = GatewayControlPlaneService(repository, apim_principal_id="unit")
    write = foundry_publication("repeat-public-quote")
    write.provider = ProviderTarget(existing_id=next(
        item["id"] for item in repository.providers if item["brand_key"] == "microsoft_foundry"
    ))
    write.model.price_source = PriceSource.MODELS_DEV
    write.model.price_reference = reference

    def accepted() -> dict[str, Any]:
        return {
            **runtime._accepted_price_values(preview),
            "price_source": "models_dev", "price_reference": reference,
        }

    first = publisher.publish(write, "owner", pricing_values=accepted())
    repeated = publisher.publish(write, "owner", pricing_values=accepted())
    assert repeated.id == first.id
    assert repeated.desired_spec_sha256 == first.desired_spec_sha256


def test_catalog_metadata_edit_cannot_change_model_availability() -> None:
    runtime, _, row = service()
    before = dict(row)
    write = ManagedModelWrite.model_validate({
        **{key: value for key, value in row.items() if key in ManagedModelWrite.model_fields},
        "enabled": not row["enabled"], "price_source": "models_dev", "allow_unpriced": True,
    })
    with pytest.raises(HTTPException) as failure:
        runtime.save_model(write, row["id"])
    assert failure.value.status_code == 409
    assert row == before


@pytest.mark.parametrize("existing", [False, True])
def test_decision_public_preview_never_downloads_or_judges_text_prices(existing: bool) -> None:
    runtime, _, row = service()
    if existing:
        row.update(capabilities=["decision"], is_default=False)
    public = runtime._price_catalog.public_catalog()
    public._fetch = lambda: pytest.fail("Decision must not read the public chat catalog")
    values: dict[str, Any] = {"model_id": row["id"]} if existing else {
        "runtime_id": row["runtime_id"], "deployment_name": "microsoft-decision-1",
        "operation": "decision",
    }
    with patch.object(runtime, "invoke") as invoke:
        preview = runtime.price_preview(PricePreviewRequest(**values), user_id="owner")
        assert preview.status == "unsupported"
        invoke.assert_not_called()


def test_azure_image_flag_is_compatible_with_operation_checked_preview() -> None:
    runtime, row = image_service()
    entry = CatalogEntry(
        reference="azure_retail:Azure OpenAI Media:image 2.5 Flare:Global:*",
        label="image 2.5 Flare", source=PriceSource.AZURE_RETAIL, text_to_image=True,
        input_per_million=5, output_per_million=30, cached_per_million=1.25,
    )
    assert entry.operation == "image_generation"
    with patch.object(runtime._price_catalog, "lookup", return_value=entry):
        preview = runtime.price_preview(PricePreviewRequest(
            model_id=row["id"], price_source=PriceSource.AZURE_RETAIL,
            price_reference=entry.reference,
        ), user_id="owner")
    assert preview.status == "matched" and preview.effective_prices
    assert preview.effective_prices.output == 30


def test_explicit_unpriced_edit_accepts_a_null_reference_without_catalog_io() -> None:
    runtime, _, row = service()
    public = runtime._price_catalog.public_catalog()
    public._fetch = lambda: pytest.fail("Explicit unpriced save must not download the catalog")
    write = ManagedModelWrite.model_validate({
        **{key: value for key, value in row.items() if key in ManagedModelWrite.model_fields},
        "price_source": "models_dev", "price_reference": None, "allow_unpriced": True,
    })
    result = runtime.save_model(write, row["id"])
    saved = next(model for model in result.models if model.id == row["id"])
    assert saved.price_source is PriceSource.MODELS_DEV
    assert saved.price_reference is None and saved.price_sync_status is None
    assert saved.input_cost_per_million is None and saved.output_cost_per_million is None
