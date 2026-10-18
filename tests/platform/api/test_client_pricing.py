import json
from dataclasses import replace
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest

from backend.services.client_pricing import plugin_model_cost
from tests.backend.model_platform.test_client_user_service import client_service


@pytest.mark.parametrize(("cached", "write", "expected"), [
    (0.2, 2.5, {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5}),
    (None, None, {"input": 2.0, "output": 10.0, "cache_read": 2.0, "cache_write": 2.0}),
    (0.0, 0.0, {"input": 2.0, "output": 10.0, "cache_read": 0.0, "cache_write": 0.0}),
])
def test_user_catalog_and_download_use_platform_accounting_prices(
    cached: float | None, write: float | None, expected: dict[str, float],
) -> None:
    service, repo, identity = client_service()
    selected = service.default_subscription(identity)
    text = next(item for item in selected["models"] if item["protocol"] != "openai_images")
    model = next(row for row in repo.models if str(row["id"]) == text["model_id"])
    model.update(input_cost_per_million=2.0, output_cost_per_million=10.0,
                 cached_cost_per_million=cached, cache_write_cost_per_million=write)
    selection = {k: selected[k] for k in ("application_id", "subscription_id", "gateway_id")}
    grant = {"id": uuid4(), "selection": selection}
    catalog = service.catalog(grant, identity)
    assert catalog["models"][model["model_key"]]["cost"] == expected
    assert plugin_model_cost(repo.model_prices()[str(model["id"])]) == expected
    with ZipFile(BytesIO(service.package(identity))) as archive:
        config = json.loads(archive.read("aigateway-proxy-user-Test/aigateway-user.config.json"))
        assert config["catalog"]["models"][model["model_key"]]["cost"] == expected
        assert {"pricing.mjs", "quota.mjs"} <= {
            name.rsplit("/", 1)[-1] for name in archive.namelist()
        }
        assert not any("subscriptionKey" in name for name in config)
    model["cached_cost_per_million"] = 0.125
    assert service.catalog(grant, identity, fresh=True)["models"][model["model_key"]]["cost"][
        "cache_read"
    ] == 0.125


def test_missing_price_is_unknown_not_zero_and_model_keys_remain_unchanged() -> None:
    service, repo, identity = client_service()
    selected = service.default_subscription(identity)
    text = next(item for item in selected["models"] if item["protocol"] != "openai_images")
    model = next(row for row in repo.models if str(row["id"]) == text["model_id"])
    model["input_cost_per_million"] = None
    grant = {"id": uuid4(), "selection": {
        k: selected[k] for k in ("application_id", "subscription_id", "gateway_id")
    }}
    catalog = service.catalog(grant, replace(identity, name="Edwin"))
    assert catalog["models"][model["model_key"]]["cost"] is None
    assert catalog["models"][model["model_key"]]["id"] == model["model_key"]


@pytest.mark.parametrize("input_price", [2.0, 0.0, None])
def test_image_catalog_download_and_refresh_include_accounting_prices(
    input_price: float | None,
) -> None:
    service, repo, identity = client_service()
    next(model for model in repo.models if model["enabled"])["capabilities"] = ["image_generation"]
    selected = service.default_subscription(identity)
    image = next(item for item in selected["models"] if item["protocol"] == "openai_images")
    model = next(row for row in repo.models if str(row["id"]) == image["model_id"])
    model.update(input_cost_per_million=input_price, output_cost_per_million=10.0,
                 cached_cost_per_million=0.0, cache_write_cost_per_million=0.0)
    grant = {"id": uuid4(), "selection": {
        k: selected[k] for k in ("application_id", "subscription_id", "gateway_id")
    }}
    expected = None if input_price is None else {
        "input": input_price, "output": 10.0, "cache_read": 0.0, "cache_write": 0.0,
    }
    catalog = service.catalog(grant, identity)
    exported = next(item for item in catalog["images"] if item["id"] == image["id"])
    assert exported == {**image, "cost": expected}
    assert image["id"] not in catalog["models"]
    with ZipFile(BytesIO(service.package(identity))) as archive:
        config = json.loads(archive.read("aigateway-proxy-user-Test/aigateway-user.config.json"))
        downloaded = next(item for item in config["catalog"]["images"] if item["id"] == image["id"])
        assert downloaded == exported
    model["input_cost_per_million"] = 3.0
    refreshed = service.catalog(grant, identity, fresh=True)
    assert next(item for item in refreshed["images"] if item["id"] == image["id"])["cost"] == {
        "input": 3.0, "output": 10.0, "cache_read": 0.0, "cache_write": 0.0,
    }
