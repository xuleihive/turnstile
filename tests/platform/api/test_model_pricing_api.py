from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.api import app
from backend.http.session import require_authenticated_session
from backend.services.runtime_service import ModelRuntimeService
from tests.platform.api.test_model_management_api import client
from tests.platform.api.test_model_management_api import (
    management_runtime_service as management_runtime_service,
)
from tests.platform.api.test_model_management_api import publication_api as publication_api
from tests.platform.pricing.test_models_dev import document
from tests.platform.pricing.test_price_catalog import GPT_41_MODEL, StubAzure, meter
from tests.platform.pricing.test_price_sync import context_model
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.pricing.catalog import CompositeCatalog
from turnstile_core.pricing.models_dev import ModelsDevCatalog


def test_source_filter_returns_quote_digest_without_other_sources(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    management_runtime_service._price_catalog = CompositeCatalog([ModelsDevCatalog(fetch=document)])
    response = client.get(
        "/api/v1/model-management/price-catalog/models",
        params={"source": "models_dev"},
    )
    assert response.status_code == 200
    entry = response.json()["models"][0]
    assert entry["source"] == "models_dev"
    options = client.get(
        "/api/v1/model-management/price-catalog/options",
        params={"model": entry["key"]},
    )
    assert options.status_code == 200
    quote = options.json()["options"][0]
    assert len(quote["entry_digest"]) == 64
    assert quote["source_metadata"]["provider_id"] == "openai"


@pytest.mark.parametrize("role", ["owner", "member"])
def test_price_options_expose_each_selected_region_digest_on_the_real_route(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
    role: str,
) -> None:
    rows = [
        meter(name, price, region=region)
        for region, factor in (("eastus", 1), ("westus", 1), ("northeurope", 2))
        for name, price in (
            ("gpt 4.1 Inp regnl Tokens", 0.002 * factor),
            ("gpt 4.1 Outp regnl Tokens", 0.008 * factor),
        )
    ]
    catalog = StubAzure(rows)
    management_runtime_service._price_catalog = CompositeCatalog([catalog])
    identity = app.dependency_overrides[require_authenticated_session]()
    app.dependency_overrides[require_authenticated_session] = lambda: replace(identity, role=role)
    try:
        response = client.get(
            "/api/v1/model-management/price-catalog/options",
            params={"model": GPT_41_MODEL.key},
        )
    finally:
        app.dependency_overrides[require_authenticated_session] = lambda: identity
    assert response.status_code == 200
    grouped = next(option for option in response.json()["options"] if "westus" in option["regions"])
    for reference in grouped["references_by_region"].values():
        entry = catalog.entry(reference)
        assert entry is not None and entry.reference == reference
        assert grouped["entry_digests_by_reference"][reference] == entry.digest
    assert len(set(grouped["entry_digests_by_reference"].values())) == 2


def test_price_match_is_read_only_uses_the_serving_provider_and_never_implicitly_calls_ai(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    payload = document()
    payload["azure"] = {"name": "Azure", "models": payload["openai"]["models"]}
    management_runtime_service._price_catalog = CompositeCatalog(
        [
            ModelsDevCatalog(fetch=lambda: payload),
        ]
    )
    row = publication_api.models[0]
    before = deepcopy(publication_api.models)
    with patch.object(management_runtime_service, "invoke") as invoke:
        response = client.post(
            "/api/v1/model-management/price-match",
            headers={"Origin": "http://localhost:5173"},
            json={"runtime_id": str(row["runtime_id"]), "names": ["gpt-example"]},
        )
        invoke.assert_not_called()
    assert response.status_code == 200
    assert response.json()["model_entry"]["key"] == "models_dev:azure:gpt-example"
    assert response.json()["method"] == "exact" and publication_api.models == before


@pytest.mark.parametrize("upstream", ["gpt-5-mini", "gpt-5-2026-01-01"])
def test_price_match_route_never_replaces_primary_identity_with_a_generic_display_name(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
    upstream: str,
) -> None:
    row = publication_api.models[0]
    row.update(upstream_model_id=upstream, display_name="GPT 5")
    payload = {
        "azure": {
            "models": {
                "gpt-5": {
                    "name": "GPT 5",
                    "cost": {"input": 7, "output": 20},
                    "modalities": {"output": ["text"]},
                }
            }
        }
    }
    management_runtime_service._price_catalog = CompositeCatalog(
        [
            ModelsDevCatalog(fetch=lambda: payload),
        ]
    )
    before = deepcopy(publication_api.models)
    with patch.object(management_runtime_service, "invoke") as invoke:
        response = client.post(
            "/api/v1/model-management/price-match",
            headers={"Origin": "http://localhost:5173"},
            json={
                "runtime_id": str(row["runtime_id"]),
                "model_id": str(row["id"]),
                "names": [upstream],
            },
        )
        invoke.assert_not_called()
    assert response.status_code == 200 and publication_api.models == before
    assert response.json()["model_entry"] is None


def test_price_match_is_owner_only_and_rejects_an_untrusted_origin(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    row = publication_api.models[0]
    values = {"runtime_id": str(row["runtime_id"]), "names": ["gpt-example"], "allow_ai": True}
    with patch.object(management_runtime_service, "invoke") as invoke:
        denied_origin = client.post(
            "/api/v1/model-management/price-match",
            headers={"Origin": "https://untrusted.test"},
            json=values,
        )
        assert denied_origin.status_code == 403
        owner = app.dependency_overrides[require_authenticated_session]()
        app.dependency_overrides[require_authenticated_session] = lambda: replace(
            owner, role="member"
        )
        try:
            denied_member = client.post(
                "/api/v1/model-management/price-match",
                headers={"Origin": "http://localhost:5173"},
                json=values,
            )
            assert denied_member.status_code == 403
        finally:
            app.dependency_overrides[require_authenticated_session] = lambda: owner
        invoke.assert_not_called()


def test_ai_match_requires_explicit_request_and_uses_the_signed_in_actor(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    model = publication_api.models[0]
    model.update(is_default=True, enabled=True, capabilities=["chat"])
    next(item for item in publication_api.runtimes if item["id"] == model["runtime_id"])[
        "enabled"
    ] = True
    next(item for item in publication_api.providers if item["id"] == model["provider_id"])[
        "enabled"
    ] = True
    payload = {
        "azure": {
            "name": "Azure",
            "models": {
                "gpt-5": {
                    "name": "GPT 5",
                    "cost": {"input": 7, "output": 20},
                    "canonical_model_id": "openai/gpt-5",
                    "modalities": {"output": ["text"]},
                }
            },
        }
    }
    management_runtime_service._price_catalog = CompositeCatalog(
        [
            ModelsDevCatalog(fetch=lambda: payload),
        ]
    )
    values = {"runtime_id": str(model["runtime_id"]), "names": ["FW GPT 5"]}
    before = deepcopy(publication_api.models)
    with patch.object(
        management_runtime_service,
        "invoke",
        return_value=SimpleNamespace(
            content='{"decision":"match","candidate_id":"models_dev:azure:gpt-5"}',
        ),
    ) as invoke:
        response = client.post(
            "/api/v1/model-management/price-match",
            headers={"Origin": "http://localhost:5173"},
            json=values,
        )
        assert response.json()["requires_ai"] is True
        invoke.assert_not_called()
        matched = client.post(
            "/api/v1/model-management/price-match",
            headers={"Origin": "http://localhost:5173"},
            json={**values, "allow_ai": True},
        )
        assert matched.status_code == 200 and matched.json()["method"] == "ai"
        assert matched.json()["model_entry"]["key"] == "models_dev:azure:gpt-5"
        invoke.assert_called_once()
        request = invoke.call_args.args[0]
        actor = app.dependency_overrides[require_authenticated_session]()
        assert request.metadata.user_id == actor.email
        assert request.metadata.request_source == "model-pricing-match"
        assert request.max_output_tokens == 512
        assert invoke.call_args.kwargs["timeout_ms"] == 15000
    assert publication_api.models == before


def test_owner_price_preview_is_read_only_and_member_is_denied(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    model = publication_api.models[0]
    model.update(price_source="models_dev", price_reference="models_dev:openai:gpt-example")
    management_runtime_service._price_catalog = CompositeCatalog([ModelsDevCatalog(fetch=document)])
    before = deepcopy(publication_api.models)
    response = client.post(
        "/api/v1/model-management/price-sync",
        headers={"Origin": "http://localhost:5173"},
        json={"model_ids": [str(model["id"])], "dry_run": True},
    )
    assert response.status_code == 200
    assert response.json()["dry_run"] and response.json()["confirmation_digest"]
    assert "change_reasons" in response.json()["details"][0]
    assert "catalog_price_changed" in response.json()["details"][0]
    assert publication_api.models == before
    owner = app.dependency_overrides[require_authenticated_session]()
    app.dependency_overrides[require_authenticated_session] = lambda: replace(owner, role="member")
    try:
        denied = client.post(
            "/api/v1/model-management/price-sync",
            headers={"Origin": "http://localhost:5173"},
            json={"dry_run": True},
        )
        assert denied.status_code == 403 and publication_api.models == before
    finally:
        app.dependency_overrides[require_authenticated_session] = lambda: owner


def test_price_preview_route_returns_both_context_schedules_without_scalar_replacement(
    publication_api: InMemoryRepository,
    management_runtime_service: ModelRuntimeService,
) -> None:
    row = publication_api.models[0]
    tiered = context_model()
    row.update(
        tiered.model_dump(
            mode="python",
            include={
                "price_source",
                "price_reference",
                "context_price_tiers",
                "input_cost_per_million",
                "output_cost_per_million",
                "cached_cost_per_million",
                "cache_write_cost_per_million",
            },
        )
    )
    before = deepcopy(publication_api.models)
    proposed = [tier.model_copy(deep=True) for tier in tiered.context_price_tiers]
    proposed[1].list_prices.output = 11.5
    proposed[1].output_price_per_million = 11.5
    with patch("turnstile_core.pricing.sync.resolve_context_prices", return_value=proposed):
        response = client.post(
            "/api/v1/model-management/price-sync",
            headers={"Origin": "http://localhost:5173"},
            json={"model_ids": [str(row["id"])], "dry_run": True},
        )
    assert response.status_code == 200
    detail = response.json()["details"][0]
    assert len(detail["current_context_price_tiers"]) == 2
    assert len(detail["proposed_context_price_tiers"]) == 2
    assert detail["current_context_price_tiers"][0]["max_input_tokens"] == 1000
    assert detail["proposed_context_price_tiers"][1]["output_price_per_million"] == 11.5
    assert detail["catalog_price_changed"] is True
    assert detail["change_reasons"] == ["catalog_price_changed"]
    assert detail["current_prices"] is None and detail["proposed_prices"] is None
    assert publication_api.models == before
