from copy import deepcopy
from threading import Event
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend.services.runtime_service import ModelRuntimeService
from tests.backend.persistence.test_price_sync_concurrency import (
    pricing_postgres as pricing_postgres,
)
from tests.platform.pricing.test_models_dev import document
from tests.platform.pricing.test_price_sync import context_model
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.domain.runtime_models import ManagedModelWrite, PriceSource
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.pricing.catalog import (
    CatalogEntry,
    CatalogModel,
    CatalogOptions,
    CompositeCatalog,
)
from turnstile_core.pricing.models_dev import ModelsDevCatalog


def service() -> tuple[ModelRuntimeService, InMemoryRepository, dict[str, Any], ModelsDevCatalog]:
    repository = InMemoryRepository()
    model = repository.models[0]
    model.update(
        price_source="models_dev",
        price_reference="models_dev:openai:gpt-example",
        input_cost_per_million=2,
        output_cost_per_million=8,
        list_input_cost_per_million=2,
        list_output_cost_per_million=8,
    )
    catalog = ModelsDevCatalog(fetch=document)
    runtime = ModelRuntimeService(repository, Settings(), price_catalog=CompositeCatalog([catalog]))
    return runtime, repository, model, catalog


def test_preview_is_read_only_and_confirmed_sync_is_bound_to_prices() -> None:
    runtime, repository, model, catalog = service()
    before = deepcopy(repository.models)
    preview = runtime.sync_prices([model["id"]], dry_run=True)
    assert preview.dry_run and preview.confirmation_digest
    assert repository.models == before
    catalog._fetch = lambda: document({"input": 2.1, "output": 8})
    catalog.refresh()
    with pytest.raises(HTTPException) as error:
        runtime.sync_prices([model["id"]], confirmation_digest=preview.confirmation_digest)
    assert error.value.status_code == 409 and repository.models == before


def test_sync_refreshes_once_and_http_failure_recovers_without_changing_prices() -> None:
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from uuid import uuid4

    import httpx

    runtime, repository, model, _ = service()
    second = deepcopy(model)
    second.update(id=uuid4(), model_key="second-price-model",
                  price_reference="models_dev:openai:gpt-second")
    repository.models.append(second)
    state: dict[str, Any] = {"status": 200, "requests": 0, "input": 2}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state["requests"] += 1
            payload = document({"input": state["input"], "output": 8})
            payload["openai"]["models"]["gpt-second"] = deepcopy(
                payload["openai"]["models"]["gpt-example"]
            )
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *_args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def fetch() -> dict[str, Any]:
            response = httpx.get(f"http://127.0.0.1:{server.server_port}/prices", timeout=2)
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result

        catalog = ModelsDevCatalog(fetch=fetch)
        catalog.entries()
        runtime._price_catalog = CompositeCatalog([catalog])
        before = deepcopy(repository.models)
        state.update(status=503, requests=0)
        failed = runtime.sync_prices([model["id"], second["id"]], dry_run=True)
        assert failed.stale == 2 and failed.unmapped == 0
        assert state["requests"] == 1 and repository.models == before
        state.update(status=200, requests=0, input=2.1)
        recovered = runtime.sync_prices([model["id"], second["id"]], dry_run=True)
        assert recovered.updated == 2 and recovered.stale == 0
        assert state["requests"] == 1 and repository.models == before
        assert catalog.entries()[0].input_per_million == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_context_sync_does_not_retry_a_failed_batch_quote() -> None:
    import httpx

    calls: list[str] = []

    class FailedCatalog:
        source = PriceSource.AZURE_RETAIL

        def invalidate(self) -> None:
            return

        def entry(self, reference: str) -> None:
            calls.append(reference)
            raise httpx.HTTPError("catalog unavailable")

        def models(self) -> tuple[CatalogModel, ...]:
            calls.append("models")
            raise AssertionError("A failed batch quote must not reread its model index")

        def options(self, _model: CatalogModel) -> CatalogOptions:
            calls.append("options")
            raise AssertionError("A failed batch quote must not reread its options")

    model = context_model()
    references = [tier.reference for tier in model.context_price_tiers]
    snapshot = CompositeCatalog([FailedCatalog()]).for_sync(references)
    from turnstile_core.pricing.sync import plan_price_sync

    result = plan_price_sync([model], snapshot)
    assert result.stale == 1 and not result.updates[0].writes
    assert calls == references


def test_batch_failure_is_scoped_to_each_azure_model() -> None:
    from copy import deepcopy
    from uuid import uuid4

    import httpx

    calls: list[str] = []
    first = "azure_retail:Unit:first:Global:*"
    second = "azure_retail:Unit:second:Global:*"

    class AzureCatalog:
        source = PriceSource.AZURE_RETAIL

        def invalidate(self) -> None:
            return

        def entry(self, reference: str) -> CatalogEntry:
            calls.append(reference)
            if reference == second:
                raise httpx.HTTPError("second model unavailable")
            return CatalogEntry(reference, "First", self.source, input_per_million=2,
                                output_per_million=8)

        def models(self) -> tuple[CatalogModel, ...]:
            return ()

        def options(self, model: CatalogModel) -> CatalogOptions:
            return CatalogOptions(model)

    runtime, repository, model, _ = service()
    model.update(price_source="azure_retail", price_reference=first)
    other = deepcopy(model)
    other.update(id=uuid4(), model_key="second-azure-model", price_reference=second)
    repository.models.append(other)
    runtime._price_catalog = CompositeCatalog([AzureCatalog()])
    before = deepcopy(repository.models)
    result = runtime.sync_prices([model["id"], other["id"]], dry_run=True)
    assert result.stale == 1 and result.unchanged == 1 and result.unmapped == 0
    assert calls == [first, second]
    assert repository.models == before


def test_unexpected_source_failure_keeps_other_sources_available() -> None:
    from uuid import uuid4

    runtime, repository, model, _ = service()

    def fail() -> dict[str, Any]:
        raise RuntimeError("source read failed")

    class AvailableCatalog:
        source = PriceSource.ANTHROPIC

        def invalidate(self) -> None:
            return

        def entry(self, reference: str) -> CatalogEntry:
            return CatalogEntry(reference, "Other source", self.source,
                                input_per_million=2, output_per_million=8)

        def models(self) -> tuple[CatalogModel, ...]:
            return ()

        def options(self, model: CatalogModel) -> CatalogOptions:
            return CatalogOptions(model)

    other = deepcopy(model)
    other.update(id=uuid4(), model_key="other-source-model", price_source="anthropic",
                 price_reference="anthropic:fixture")
    repository.models.append(other)
    runtime._price_catalog = CompositeCatalog([ModelsDevCatalog(fetch=fail), AvailableCatalog()])
    before = deepcopy(repository.models)
    result = runtime.sync_prices([model["id"], other["id"]], dry_run=True)
    assert result.stale == 1 and result.unmapped == 0 and result.unchanged == 1
    assert repository.models == before


def test_confirmed_preview_uses_existing_write_and_review_rules() -> None:
    runtime, _, model, catalog = service()
    catalog._fetch = lambda: document({"input": 2.1, "output": 8})
    preview = runtime.sync_prices([model["id"]], dry_run=True)
    assert preview.updated == 1
    runtime.sync_prices([model["id"]], confirmation_digest=preview.confirmation_digest)
    assert model["input_cost_per_million"] == 2.1
    catalog._fetch = lambda: document({"input": 9, "output": 80})
    catalog.refresh()
    review = runtime.sync_prices([model["id"]], dry_run=True)
    assert review.review_needed == 1 and model["input_cost_per_million"] == 2.1
    assert review.details[0].proposed_prices is not None
    assert review.details[0].proposed_prices.input == 9


def test_save_refuses_a_quote_that_changed_after_selection() -> None:
    runtime, _, model, catalog = service()
    entry = catalog.entries()[0]
    write = ManagedModelWrite.model_validate(
        {
            **{
                key: model[key]
                for key in ("provider_id", "runtime_id", "model_key", "display_name")
            },
            "price_source": "models_dev",
            "price_reference": entry.reference,
            "price_entry_digest": entry.digest,
        }
    )
    catalog._fetch = lambda: document({"input": 7, "output": 8})
    catalog.refresh()
    with pytest.raises(HTTPException) as error:
        runtime.save_model(write, model["id"])
    assert error.value.status_code == 409 and model["input_cost_per_million"] == 2


@pytest.mark.parametrize("create_manual", [False, True])
def test_source_snapshot_roundtrips_in_postgres(
    pricing_postgres: tuple[str, Event],
    create_manual: bool,
) -> None:
    from uuid import uuid4

    import psycopg

    from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy

    info, _ = pricing_postgres
    provider_id, runtime_id = uuid4(), uuid4()
    with psycopg.connect(info, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO model_provider(id,name,provider_kind) VALUES(%s,%s,'openai_compatible')",
            (provider_id, str(provider_id)),
        )
        connection.execute(
            "INSERT INTO model_runtime(id,provider_id,name,runtime_kind) "
            "VALUES(%s,%s,%s,'openai_compatible')",
            (runtime_id, provider_id, str(runtime_id)),
        )
    repository = PostgreSqlOpsDbProxy(info)
    try:
        entry = ModelsDevCatalog(fetch=document).entries()[0]
        row = repository.create_registry_item(
            "model",
            {
                **ManagedModelWrite(
                    provider_id=provider_id,
                    runtime_id=runtime_id,
                    model_key=str(uuid4()),
                    display_name="Price provenance",
                    capabilities=["chat"],
                    price_source=PriceSource.MANUAL if create_manual else PriceSource.MODELS_DEV,
                    price_reference=None if create_manual else entry.reference,
                ).model_dump(mode="python"),
                "price_source_snapshot": None if create_manual else entry.provenance,
            },
        )
        if create_manual:
            repository.update_registry_item(
                "model",
                row["id"],
                {
                    "price_source": PriceSource.MODELS_DEV,
                    "price_reference": entry.reference,
                    "price_source_snapshot": entry.provenance,
                },
            )
        saved = next(model for model in repository.registry()["models"] if model["id"] == row["id"])
        assert saved["price_source_snapshot"] == entry.provenance
        repository.update_registry_item("model", row["id"], {"price_source": "manual"})
        saved = next(model for model in repository.registry()["models"] if model["id"] == row["id"])
        assert saved["price_source_snapshot"] is None
    finally:
        repository.close()


def test_provenance_upgrade_preserves_legacy_prices_in_postgres(
    pricing_postgres: tuple[str, Event],
) -> None:
    from uuid import uuid4

    import psycopg

    info, _ = pricing_postgres
    with psycopg.connect(info) as connection, connection.transaction(force_rollback=True):
        connection.execute(
            "ALTER TABLE public.managed_model_price "
            "RENAME TO managed_model_price_upgrade_baseline"
        )
        connection.execute(
            "CREATE TABLE public.managed_model_price "
            "(LIKE public.managed_model_price_upgrade_baseline INCLUDING ALL)"
        )
        connection.execute(
            "ALTER TABLE public.managed_model_price "
            "DROP COLUMN price_source_snapshot, "
            "DROP CONSTRAINT managed_model_price_source_check, "
            "ADD CONSTRAINT managed_model_price_source_check "
            "CHECK (price_source IN ('manual','azure_retail','anthropic'))"
        )
        for source in ("manual", "azure_retail", "anthropic"):
            connection.execute(
                "INSERT INTO public.managed_model_price "
                "(model_id,price_source,price_reference,list_input_cost_per_million,"
                "list_output_cost_per_million) VALUES(%s,%s,%s,1.25,8.5)",
                (uuid4(), source, None if source == "manual" else "legacy:" + source),
            )
        query = (
            "SELECT model_id,price_source,list_input_cost_per_million,"
            "list_output_cost_per_million "
            "FROM public.managed_model_price ORDER BY model_id"
        )
        before = connection.execute(query).fetchall()
        rate_query = (
            "SELECT id,input_cost_per_million,output_cost_per_million "
            "FROM public.managed_model ORDER BY id"
        )
        rates_before = connection.execute(rate_query).fetchall()
        migration = REPOSITORY_ROOT / "migrations/031_model_price_provenance.up.sql"
        connection.execute(migration.read_text(encoding="utf-8"))
        assert connection.execute(query).fetchall() == before
        assert connection.execute(rate_query).fetchall() == rates_before
        assert connection.execute(
            "SELECT count(*) FROM public.managed_model_price "
            "WHERE price_source_snapshot IS NOT NULL"
        ).fetchone() == (0,)
        connection.execute(
            "UPDATE public.managed_model_price SET price_source='models_dev', "
            "price_reference='models_dev:openai:fixture', "
            "price_source_snapshot='{}'::jsonb WHERE model_id=%s",
            (before[0][0],),
        )


def test_publication_checks_the_accepted_quote_and_keeps_provenance() -> None:
    from turnstile_core.domain.control_plane import ModelTarget
    from turnstile_core.services.control_plane import (
        ControlPlaneConflictError,
        GatewayControlPlaneService,
    )

    entry = ModelsDevCatalog(fetch=document).entries()[0]
    publisher = GatewayControlPlaneService(InMemoryRepository(), apim_principal_id="unit")
    publisher._price_catalog = CompositeCatalog([ModelsDevCatalog(fetch=document)])
    target = ModelTarget(
        model_key="gpt-example",
        display_name="GPT Example",
        upstream_model_id="gpt-example",
        price_source=PriceSource.MODELS_DEV,
        price_reference=entry.reference,
        price_entry_digest=entry.digest,
    )
    accepted = publisher._price_model(target, {})
    assert accepted.price_source_snapshot is not None
    assert accepted.price_source_snapshot["entry_digest"] == entry.digest
    assert accepted.input_cost_per_million == 2 and accepted.price_entry_digest is None
    with pytest.raises(ControlPlaneConflictError, match="quote changed"):
        publisher._price_model(target.model_copy(update={"price_entry_digest": "f" * 64}), {})


@pytest.mark.parametrize("metadata_change", ["reread", "unrelated_entry", "updated_at", "context"])
def test_repeated_public_quote_reuses_publication_after_catalog_refresh(
    metadata_change: str,
) -> None:
    from tests.backend.model_platform.control_plane_support import (
        GatewayControlPlaneService,
        foundry_publication,
    )

    payload = document()
    public = ModelsDevCatalog(fetch=lambda: payload)
    entry = public.entries()[0]
    repository = InMemoryRepository()
    publisher = GatewayControlPlaneService(
        repository,
        price_catalog=CompositeCatalog([public]),
        apim_principal_id="00000000-0000-0000-0000-000000000705",
    )
    write = foundry_publication("quote-retry-model")
    from turnstile_core.domain.runtime_models import ProviderTarget

    write.provider = ProviderTarget(
        existing_id=next(
            row["id"] for row in repository.providers if row["brand_key"] == "microsoft_foundry"
        )
    )
    write.model.price_source = PriceSource.MODELS_DEV
    write.model.price_reference = entry.reference
    write.model.price_entry_digest = entry.digest
    first = publisher.publish(write, "owner@example.test")
    if metadata_change == "unrelated_entry":
        payload["other"] = {
            "models": {
                "other": {
                    "cost": {"input": 1, "output": 2},
                    "modalities": {"output": ["text"]},
                }
            }
        }
    elif metadata_change == "updated_at":
        payload["openai"]["models"]["gpt-example"]["last_updated"] = "2026-11-06"
    elif metadata_change == "context":
        payload["openai"]["models"]["gpt-example"]["limit"]["context"] = 256000
    public.refresh()
    assert public.entries()[0].digest == entry.digest
    repeated = publisher.publish(write, "owner@example.test")
    assert repeated.id == first.id
    assert repeated.desired_spec_sha256 == first.desired_spec_sha256


def test_stored_catalog_model_ignores_catalog_observation_metadata() -> None:
    from turnstile_core.services.control_plane import _stored_model_target

    entry = ModelsDevCatalog(fetch=document).entries()[0]
    row = {
        "model_key": "gpt-example",
        "display_name": "GPT Example",
        "upstream_model_id": "gpt-example",
        "price_source": PriceSource.MODELS_DEV,
        "price_reference": entry.reference,
        "price_source_snapshot": entry.provenance,
        "input_cost_per_million": 2,
        "output_cost_per_million": 8,
    }
    first = _stored_model_target(row)
    updated = _stored_model_target(
        {
            **row,
            "price_source_snapshot": {
                **entry.provenance,
                "fetched_at": "later",
                "snapshot_id": "other-catalog",
                "source_updated_at": "2026-11-06",
                "context_window": 256000,
            },
        }
    )
    assert first.model_dump(mode="json") == updated.model_dump(mode="json")
    assert first.price_source_snapshot is not None
    assert first.price_source_snapshot["entry_digest"] == entry.digest
    assert first.price_source_snapshot["provider_id"] == "openai"


def test_image_public_quote_saves_syncs_and_publishes_without_chat_prices() -> None:
    from tests.backend.model_platform.test_image_publication import image_publication
    from turnstile_core.services.control_plane import GatewayControlPlaneService

    payload: dict[str, Any] = {
        "openai": {
            "models": {
                "gpt-image-2.5-flare": {
                    "cost": {"input": 5, "output": 30, "cache_read": 1.25},
                    "modalities": {"output": ["image"]},
                }
            }
        },
    }
    catalog = ModelsDevCatalog(fetch=lambda: payload)
    entry = catalog.entries()[0]
    repository = InMemoryRepository()
    provider = next(row for row in repository.providers if row["brand_key"] == "microsoft_foundry")
    connection = next(row for row in repository.runtimes if row["provider_id"] == provider["id"])
    write = ManagedModelWrite(
        provider_id=provider["id"],
        runtime_id=connection["id"],
        model_key="public-image-price",
        display_name="Public image price",
        capabilities=["image_generation"],
        input_cost_per_million=5,
        cached_cost_per_million=1.25,
        output_cost_per_million=30,
    )
    row = repository.create_registry_item("model", write.model_dump(mode="python"))
    runtime = ModelRuntimeService(repository, Settings(), price_catalog=CompositeCatalog([catalog]))
    runtime.save_model(
        write.model_copy(
            update={
                "price_source": PriceSource.MODELS_DEV,
                "price_reference": entry.reference,
                "price_entry_digest": entry.digest,
            }
        ),
        row["id"],
    )
    saved = next(model for model in repository.models if model["id"] == row["id"])
    assert saved["input_cost_per_million"] == 5
    assert saved["cached_cost_per_million"] == 1.25
    assert saved["cache_write_cost_per_million"] is None
    payload["openai"]["models"]["gpt-image-2.5-flare"]["cost"]["output"] = 31
    catalog.refresh()
    preview = runtime.sync_prices([row["id"]], dry_run=True)
    assert preview.updated == 1 and saved["output_cost_per_million"] == 30
    runtime.sync_prices([row["id"]], confirmation_digest=preview.confirmation_digest)
    assert saved["output_cost_per_million"] == 31

    request = image_publication(repository)
    request.model.price_source = PriceSource.MODELS_DEV
    request.model.price_reference = entry.reference
    request.model.price_entry_digest = catalog.entries()[0].digest
    publication = GatewayControlPlaneService(
        repository,
        apim_principal_id="unit-principal",
        price_catalog=CompositeCatalog([catalog]),
    ).publish(request, "owner@example.test")
    model = publication.desired_spec.bindings[-1].model
    assert model.capabilities == ["image_generation"]
    assert model.input_cost_per_million == 5 and model.output_cost_per_million == 31
    assert model.cached_cost_per_million == 1.25 and model.cache_write_cost_per_million is None
    assert (
        model.price_source_snapshot and model.price_source_snapshot["reference"] == entry.reference
    )


def test_image_sync_rejects_a_chat_quote_without_changing_accepted_prices() -> None:
    payload: dict[str, Any] = {
        "openai": {
            "models": {
                "gpt-image-2": {
                    "cost": {"input": 5, "output": 30, "cache_read": 1.25},
                    "modalities": {"output": ["image"]},
                }
            }
        }
    }
    catalog = ModelsDevCatalog(fetch=lambda: payload)
    entry = catalog.entries()[0]
    repository = InMemoryRepository()
    connection = repository.runtimes[-1]
    write = ManagedModelWrite(
        provider_id=connection["provider_id"],
        runtime_id=connection["id"],
        model_key="image-price-drift",
        display_name="Image price drift",
        capabilities=["image_generation"],
        price_source=PriceSource.MODELS_DEV,
        price_reference=entry.reference,
        input_cost_per_million=5,
        cached_cost_per_million=1.25,
        output_cost_per_million=30,
    )
    row = repository.create_registry_item("model", write.model_dump(mode="python"))
    runtime = ModelRuntimeService(repository, Settings(), price_catalog=CompositeCatalog([catalog]))
    payload["openai"]["models"]["gpt-image-2"]["modalities"]["output"] = ["text"]
    payload["openai"]["models"]["gpt-image-2"]["cost"]["input"] = 5.1
    catalog.refresh()
    preview = runtime.sync_prices([row["id"]], dry_run=True)
    assert preview.updated == 0 and preview.stale == 1
    assert preview.details[0].proposed_prices is None
    runtime.sync_prices([row["id"]], confirmation_digest=preview.confirmation_digest)
    saved = next(model for model in repository.models if model["id"] == row["id"])
    assert saved["input_cost_per_million"] == 5
    assert saved["output_cost_per_million"] == 30
    assert saved["cached_cost_per_million"] == 1.25


def test_unchanged_quote_can_refresh_source_status_without_repricing() -> None:
    runtime, repository, row, _ = service()
    row.update(
        cached_cost_per_million=0,
        list_cached_cost_per_million=0,
        cache_write_cost_per_million=None,
        list_cache_write_cost_per_million=None,
        price_sync_status="stale",
        price_sync_message="Previous source outage",
    )
    before = deepcopy(repository.models)
    preview = runtime.sync_prices([row["id"]], dry_run=True)
    assert preview.updated == 0 and preview.unchanged == 1
    assert repository.models == before
    response = runtime.sync_prices([row["id"]], confirmation_digest=preview.confirmation_digest)
    assert response.unchanged == 1 and row["price_sync_status"] == "ok"
    assert row["price_sync_message"] is None
    assert row["input_cost_per_million"] == 2 and row["cached_cost_per_million"] == 0


def test_superseded_unchanged_sync_is_not_counted_twice() -> None:
    runtime, repository, row, _ = service()
    row.update(
        cached_cost_per_million=0,
        list_cached_cost_per_million=0,
        cache_write_cost_per_million=None,
        list_cache_write_cost_per_million=None,
    )
    apply = repository.apply_model_price_sync

    def concurrent_edit(updates: Any) -> Any:
        row["price_reference"] = "models_dev:openai:concurrent-owner-choice"
        return apply(updates)

    with patch.object(repository, "apply_model_price_sync", side_effect=concurrent_edit):
        response = runtime.sync_prices([row["id"]])
    assert response.total == 1 and response.superseded == 1
    assert response.details[0].outcome == "superseded"
    assert response.unchanged == 0 and response.updated == 0


def test_unchanged_quote_refreshes_status_and_provenance_in_postgres(
    pricing_postgres: tuple[str, Event],
) -> None:
    from uuid import uuid4

    import psycopg

    from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy

    info, _ = pricing_postgres
    provider_id, runtime_id = uuid4(), uuid4()
    with psycopg.connect(info, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO model_provider(id,name,provider_kind) VALUES(%s,%s,'openai_compatible')",
            (provider_id, str(provider_id)),
        )
        connection.execute(
            "INSERT INTO model_runtime(id,provider_id,name,runtime_kind) "
            "VALUES(%s,%s,%s,'openai_compatible')",
            (runtime_id, provider_id, str(runtime_id)),
        )
    repository = PostgreSqlOpsDbProxy(info)
    try:
        catalog = ModelsDevCatalog(fetch=document)
        entry = catalog.entries()[0]
        values = ManagedModelWrite(
            provider_id=provider_id,
            runtime_id=runtime_id,
            model_key=str(uuid4()),
            display_name="Unchanged catalog recovery",
            capabilities=["chat"],
            price_source=PriceSource.MODELS_DEV,
            price_reference=entry.reference,
            input_cost_per_million=2,
            output_cost_per_million=8,
            cached_cost_per_million=0,
        ).model_dump(mode="python")
        values.update(
            list_input_cost_per_million=2,
            list_output_cost_per_million=8,
            list_cached_cost_per_million=0,
            price_sync_status="stale",
            price_sync_message="Previous source outage",
        )
        row = repository.create_registry_item("model", values)
        runtime = ModelRuntimeService(
            repository,
            Settings(),
            price_catalog=CompositeCatalog([catalog]),
        )
        preview = runtime.sync_prices([row["id"]], dry_run=True)
        assert preview.updated == 0 and preview.unchanged == 1
        stored = next(
            model for model in repository.registry()["models"] if model["id"] == row["id"]
        )
        assert stored["price_sync_status"] == "stale"
        runtime.sync_prices([row["id"]], confirmation_digest=preview.confirmation_digest)
        saved = next(model for model in repository.registry()["models"] if model["id"] == row["id"])
        assert saved["price_sync_status"] == "ok" and saved["price_sync_message"] is None
        assert saved["input_cost_per_million"] == 2 and saved["cached_cost_per_million"] == 0
        snapshot = saved["price_source_snapshot"]
        assert {key: value for key, value in snapshot.items() if key != "fetched_at"} == {
            key: value for key, value in entry.provenance.items() if key != "fetched_at"
        }
        assert snapshot["fetched_at"] >= entry.provenance["fetched_at"]
    finally:
        repository.close()


@pytest.mark.parametrize("needs_review", [False, True])
def test_context_preview_keeps_both_tiers_and_never_uses_scalar_prices(needs_review: bool) -> None:
    runtime, repository, row, _ = service()
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
    before = deepcopy(repository.models)
    proposed = [tier.model_copy(deep=True) for tier in tiered.context_price_tiers]
    factor = 2 if needs_review else 1.05
    proposed[1].output_price_per_million *= factor
    assert proposed[1].list_prices.output is not None
    proposed[1].list_prices.output *= factor
    with patch("turnstile_core.pricing.sync.resolve_context_prices", return_value=proposed):
        preview = runtime.sync_prices([row["id"]], dry_run=True)
    detail = preview.details[0]
    assert detail.current_context_price_tiers == tiered.context_price_tiers
    assert detail.proposed_context_price_tiers == proposed
    assert detail.current_prices is None and detail.proposed_prices is None
    assert preview.review_needed == int(needs_review)
    assert repository.models == before


def test_context_preview_failure_keeps_original_schedule_without_inventing_a_proposal() -> None:
    runtime, repository, row, _ = service()
    tiered = context_model()
    row.update(
        tiered.model_dump(
            mode="python",
            include={
                "price_source",
                "price_reference",
                "context_price_tiers",
            },
        )
    )
    before = deepcopy(repository.models)
    with patch("turnstile_core.pricing.sync.resolve_context_prices", side_effect=ValueError):
        preview = runtime.sync_prices([row["id"]], dry_run=True)
    assert preview.details[0].current_context_price_tiers == tiered.context_price_tiers
    assert preview.details[0].proposed_context_price_tiers is None
    assert preview.details[0].proposed_prices is None
    assert preview.stale == 1 and repository.models == before


def test_context_threshold_change_is_visible_and_invalidates_the_previous_confirmation() -> None:
    runtime, repository, row, _ = service()
    tiered = context_model()
    row.update(
        tiered.model_dump(
            mode="python",
            include={
                "price_source",
                "price_reference",
                "context_price_tiers",
            },
        )
    )
    before = deepcopy(repository.models)
    changed = [tier.model_copy(deep=True) for tier in tiered.context_price_tiers]
    changed[0].max_input_tokens = 900
    changed[1].min_input_tokens = 901
    with patch(
        "turnstile_core.pricing.sync.resolve_context_prices",
        return_value=tiered.context_price_tiers,
    ):
        original = runtime.sync_prices([row["id"]], dry_run=True)
    with patch("turnstile_core.pricing.sync.resolve_context_prices", return_value=changed):
        preview = runtime.sync_prices([row["id"]], dry_run=True)
        assert preview.review_needed == 1
        proposal = preview.details[0].proposed_context_price_tiers
        assert proposal is not None and proposal[0].max_input_tokens == 900
        assert preview.details[0].current_context_price_tiers[0].max_input_tokens == 1000
        with pytest.raises(HTTPException) as error:
            runtime.sync_prices([row["id"]], confirmation_digest=original.confirmation_digest)
    assert error.value.status_code == 409 and repository.models == before
