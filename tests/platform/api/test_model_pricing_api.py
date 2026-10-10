from __future__ import annotations

from copy import deepcopy

from backend.api import app
from backend.http.session import require_authenticated_session
from backend.services.runtime_service import ModelRuntimeService
from tests.platform.api.test_model_management_api import (
    bedrock_publication,
    client,
    gateway_id,
)
from tests.platform.api.test_model_management_api import (
    management_runtime_service as management_runtime_service,
)
from tests.platform.api.test_model_management_api import (
    publication_api as publication_api,
)
from tests.platform.pricing.test_models_dev import document, image_document
from tests.platform.pricing.test_price_catalog import StubAzure, meter
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.pricing.catalog import CompositeCatalog
from turnstile_core.pricing.models_dev import ModelsDevCatalog


def test_owner_preview_is_read_only(
    publication_api: InMemoryRepository, management_runtime_service: ModelRuntimeService,
) -> None:
    model = publication_api.models[0]
    management_runtime_service._price_catalog = CompositeCatalog([
        ModelsDevCatalog(fetch=document),
    ])
    before = deepcopy(publication_api.models)
    response = client.post(
        "/api/v1/model-management/price-preview", headers={"Origin": "http://localhost:5173"},
        json={"model_id": str(model["id"]), "upstream_model_id": "gpt-5"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "matched"
    assert response.json()["context_window"] == 400000
    assert publication_api.models == before


def test_preview_rejects_an_untrusted_origin(
    publication_api: InMemoryRepository, management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    response = client.post(
        "/api/v1/model-management/price-preview", headers={"Origin": "https://untrusted.test"},
        json={"model_id": str(publication_api.models[0]["id"])},
    )
    assert response.status_code == 403


def test_preview_is_owner_only(
    publication_api: InMemoryRepository, management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    from dataclasses import replace

    owner = app.dependency_overrides[require_authenticated_session]()
    app.dependency_overrides[require_authenticated_session] = lambda: replace(owner, role="member")
    try:
        response = client.post(
            "/api/v1/model-management/price-preview",
            headers={"Origin": "http://localhost:5173"},
            json={"model_id": str(publication_api.models[0]["id"])},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides[require_authenticated_session] = lambda: owner


def test_creation_carries_manual_choice_in_the_publication_snapshot(
    publication_api: InMemoryRepository, management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    payload = bedrock_publication(gateway_id(publication_api))
    payload["model"]["price_source"] = "manual"  # type: ignore[index]
    response = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"}, json=payload,
    )
    assert response.status_code == 202
    snapshot = publication_api.gateway_publications[-1]["desired_spec"]["bindings"][-1]["model"]
    assert snapshot["price_configuration"]["price_source"] == "manual"


def test_catalog_api_reads_only_the_requested_source(
    publication_api: InMemoryRepository, management_runtime_service: ModelRuntimeService,
) -> None:
    del publication_api

    def forbidden() -> dict[str, object]:
        raise AssertionError("Azure query must not fetch models.dev")

    management_runtime_service._price_catalog = CompositeCatalog([
        ModelsDevCatalog(fetch=forbidden),
        StubAzure([
            meter("4.7 Inp glbl Tokens", 0.002, product="Azure Grok"),
            meter("4.7 Outp glbl Tokens", 0.006, product="Azure Grok"),
        ]),
    ])
    response = client.get(
        "/api/v1/model-management/price-catalog/models",
        params={"q": "grok", "source": "azure_retail"},
    )
    assert response.status_code == 200
    assert [row["source"] for row in response.json()["models"]] == ["azure_retail"]
    assert response.json()["unavailable"] == []
    response = client.get(
        "/api/v1/model-management/price-catalog/models", params={"source": "unknown"},
    )
    assert response.status_code == 422


def test_image_publication_carries_accepted_public_prices_without_paid_probe(
    publication_api: InMemoryRepository, management_runtime_service: ModelRuntimeService,
) -> None:
    from backend.api import control_plane_service
    from tests.backend.model_platform.test_image_publication import image_publication
    from turnstile_core.services.control_plane import GatewayControlPlaneService

    service = GatewayControlPlaneService(
        publication_api, image_generation_enabled=True, apim_principal_id="unit-principal",
    )
    app.dependency_overrides[control_plane_service] = lambda: service
    management_runtime_service._price_catalog = CompositeCatalog([
        ModelsDevCatalog(fetch=image_document),
    ])
    payload = image_publication(publication_api).model_dump(mode="json")
    runtime_id = publication_api.models[0]["runtime_id"]
    runtime = next(item for item in publication_api.runtimes if item["id"] == runtime_id)
    endpoint = "https://unit.services.ai.azure.com/api/projects/unit"
    runtime["config"].update(
        backend_url=endpoint, project_endpoint=endpoint, auth_strategy="managed_identity",
        managed_identity_resource="https://ai.azure.com", control_plane_managed=True,
    )
    payload["provider"] = {"existing_id": str(publication_api.models[0]["provider_id"])}
    payload["runtime"] = {"existing_id": str(runtime_id)}
    payload["model"].update({
        "deployment_name": "gpt-image-2.5-flare", "operation": "image_generation",
        "input_cost_per_million": 5, "output_cost_per_million": 30,
        "cached_cost_per_million": 1.25, "price_source": "models_dev",
        "price_reference": "models_dev:azure:gpt-image-2.5-flare",
    })
    response = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"}, json=payload,
    )
    assert response.status_code == 202, response.text
    snapshot = publication_api.gateway_publications[-1]["desired_spec"]["bindings"][-1]["model"]
    assert snapshot["price_configuration"]["price_source"] == "models_dev"
    assert snapshot["price_configuration"]["price_reference"] == (
        "models_dev:azure:gpt-image-2.5-flare"
    )
    assert snapshot["output_cost_per_million"] == 30
