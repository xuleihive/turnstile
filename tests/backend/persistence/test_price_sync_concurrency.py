"""Exercise overlapping pricing transactions in an owned local PostgreSQL cluster."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import HttpUrl

from backend.services.budget_service import TokenBudgetService
from backend.services.runtime_service import ModelRuntimeService
from turnstile_core.config import Settings
from turnstile_core.domain.control_plane import (
    GatewayPublicationCreate,
    ModelCreateTarget,
    RuntimeTarget,
)
from turnstile_core.domain.models import ReconciledUsage
from turnstile_core.domain.runtime_models import (
    ManagedModel,
    ManagedModelWrite,
    PriceCatalogContextTier,
    PriceCatalogInputRange,
    PriceSource,
    ProviderTarget,
)
from turnstile_core.ingestion.processor import CoefficientResolver, UsageProcessor
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.pricing.catalog import (
    CatalogEntry,
    CatalogModel,
    CatalogOption,
    CatalogOptions,
    CompositeCatalog,
)
from turnstile_core.pricing.sync import plan_price_sync
from turnstile_core.services.control_plane import GatewayControlPlaneService


def _apply_migrations(info: str) -> None:
    root = Path(__file__).resolve().parents[3]
    with psycopg.connect(info, autocommit=True) as connection:
        connection.execute((root / "migrations/001_initial_schema.up.sql").read_text())
        connection.execute("SET search_path TO public")
        for migration in sorted((root / "migrations").glob("*.up.sql"))[1:]:
            connection.execute(migration.read_text())


@pytest.fixture(scope="module")
def pricing_postgres() -> Iterator[tuple[str, Event]]:
    initdb = shutil.which("initdb")
    postgres = shutil.which("postgres")
    if initdb is None or postgres is None:
        pytest.skip("Local initdb and postgres are required for transaction validation")
    version = subprocess.run(
        [postgres, "--version"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    major = re.search(r"\(PostgreSQL\) (\d+)", version)
    if major is None or int(major.group(1)) < 15:
        pytest.fail(
            "The migrated schema needs PostgreSQL 15 or newer (UNIQUE NULLS NOT DISTINCT); "
            f"the first postgres on PATH reports {version!r}"
        )
    with TemporaryDirectory(prefix="ts-price-") as temporary:
        folder = Path(temporary)
        data = folder / "data"
        sockets = folder / "sockets"
        sockets.mkdir(mode=0o700)
        subprocess.run(
            [initdb, "-D", str(data), "-U", "price_test", "--encoding=UTF8",
             "--no-locale", "--auth-local=trust", "--auth-host=reject"],
            check=True, capture_output=True, text=True,
        )
        server = subprocess.Popen(
            [postgres, "-D", str(data), "-h", "", "-k", str(sockets), "-F",
             "-c", "log_lock_waits=on", "-c", "deadlock_timeout=50ms",
             "-c", "statement_timeout=10000"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        ready = Event()
        waiting = Event()
        output: list[str] = []

        def read_output() -> None:
            assert server.stdout is not None
            for line in server.stdout:
                output.append(line)
                if "database system is ready to accept connections" in line:
                    ready.set()
                if "still waiting for" in line:
                    waiting.set()

        reader = Thread(target=read_output, daemon=True)
        reader.start()
        try:
            assert ready.wait(15), "".join(output[-20:])
            info = make_conninfo(
                host=str(sockets), dbname="postgres", user="price_test", connect_timeout=5,
            )
            _apply_migrations(info)
            yield info, waiting
        finally:
            if server.poll() is None:
                assert int((data / "postmaster.pid").read_text().splitlines()[0]) == server.pid
                server.terminate()
                server.wait(timeout=15)
            reader.join(timeout=5)
            assert server.stdout is not None
            server.stdout.close()


@pytest.mark.parametrize(
    ("kind", "changes", "initial_discount"),
    [
        ("model", {"price_source": "manual", "input_cost_per_million": 7.0}, 90),
        ("model", {"price_reference": "azure_retail:changed:model:Global:*"}, 90),
        ("model", {"price_discount_percent": 80}, 90),
        ("runtime", {"price_discount_percent": 80}, 90),
        ("runtime", {"price_discount_percent": 80}, None),
    ],
    ids=["manual-price", "reference-only", "model-discount", "runtime-discount",
         "first-runtime-discount"],
)
def test_sync_waits_for_edit_then_rechecks_configuration(
    pricing_postgres: tuple[str, Event],
    kind: str,
    changes: dict[str, Any],
    initial_discount: int | None,
) -> None:
    info, waiting = pricing_postgres
    provider_id, runtime_id = uuid4(), uuid4()
    reference = "azure_retail:Azure OpenAI:gpt 4.1 mini:Global:*"
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
        if initial_discount is not None:
            connection.execute(
                "INSERT INTO model_runtime_price(runtime_id,price_discount_percent) VALUES(%s,%s)",
                (runtime_id, initial_discount),
            )
    repository = PostgreSqlOpsDbProxy(info)
    editor_repository = PostgreSqlOpsDbProxy(info)
    try:
        write = ManagedModelWrite(
            provider_id=provider_id, runtime_id=runtime_id,
            model_key=str(uuid4()), display_name="Concurrent price test",
            capabilities=["chat"], allowed_roles=["owner"],
            input_cost_per_million=0.36, output_cost_per_million=1.44,
            cached_cost_per_million=0.09, price_source=PriceSource.AZURE_RETAIL,
            price_reference=reference,
        )
        created = repository.create_registry_item("model", write.model_dump(mode="python"))
        snapshot = next(
            row for row in repository.registry()["models"] if row["id"] == created["id"]
        )
        catalog = Mock(spec=CompositeCatalog)
        catalog.lookup.return_value = CatalogEntry(
            reference=reference, label="gpt 4.1 mini", source=PriceSource.AZURE_RETAIL,
            input_per_million=0.40, output_per_million=1.60,
            cached_per_million=0.10, cache_write_per_million=None,
        )
        planned = plan_price_sync([ManagedModel.model_validate(snapshot)], catalog).updates[0]
        update = asdict(planned) | {"status": planned.status.value}
        with ThreadPoolExecutor(max_workers=1) as executor, psycopg.connect(
            info, row_factory=dict_row,
        ) as editor:
            editor.execute("SELECT 1")

            @contextmanager
            def editor_connection() -> Iterator[Any]:
                yield editor

            with patch.object(editor_repository, "_connection", editor_connection):
                editor_repository.update_registry_item(
                    kind, created["id"] if kind == "model" else runtime_id, changes,
                )
            waiting.clear()
            future = executor.submit(repository.apply_model_price_sync, [update])
            try:
                assert waiting.wait(5), "The sync did not wait for the overlapping edit"
            finally:
                editor.commit()
            assert future.result(timeout=10) == 0
        saved = next(row for row in repository.registry()["models"] if row["id"] == created["id"])
        assert float(saved["input_cost_per_million"]) == pytest.approx(
            changes.get("input_cost_per_million", 0.36)
        )
        assert saved["price_source"] == changes.get("price_source", "azure_retail")
        assert saved["price_sync_status"] == "superseded"
        assert saved["list_input_cost_per_million"] is None
    finally:
        editor_repository.close()
        repository.close()


def test_catalog_save_guard_preserves_sql_prices_and_allows_metadata_edits(
    pricing_postgres: tuple[str, Event], tmp_path: Path,
) -> None:
    info, _waiting = pricing_postgres
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
        write = ManagedModelWrite(
            provider_id=provider_id, runtime_id=runtime_id, model_key=str(uuid4()),
            display_name="SQL catalog save test", capabilities=["chat"], allowed_roles=["owner"],
            input_cost_per_million=0.36, output_cost_per_million=1.44,
            cached_cost_per_million=0.09, price_source=PriceSource.AZURE_RETAIL,
            price_reference="azure_retail:Azure OpenAI:gpt 4.1 mini:Global:*",
            price_discount_percent=90,
        )
        created = repository.create_registry_item("model", write.model_dump(mode="python"))
        catalog = Mock(spec=CompositeCatalog)
        catalog.lookup.side_effect = TimeoutError("price source unavailable")
        service = ModelRuntimeService(
            repository, Settings(credential_key_file=tmp_path / "credential.key"),
            price_catalog=catalog,
        )
        current = next(model for model in service.registry().models if model.id == created["id"])
        write = ManagedModelWrite.model_validate(
            current.model_dump(include=set(ManagedModelWrite.model_fields))
        )

        renamed = service.save_model(
            write.model_copy(update={"display_name": "Renamed without repricing"}), created["id"],
        )

        saved = next(model for model in renamed.models if model.id == created["id"])
        assert saved.cached_cost_per_million == 0.09
        catalog.lookup.assert_not_called()
        catalog.lookup.side_effect = None
        catalog.lookup.return_value = CatalogEntry(
            reference=write.price_reference or "", label="gpt 4.1 mini",
            source=PriceSource.AZURE_RETAIL, input_per_million=0.40,
            output_per_million=1.60, complete=False,
        )

        with pytest.raises(HTTPException) as caught:
            service.save_model(
                write.model_copy(update={"cached_cost_per_million": None}), created["id"],
            )

        assert caught.value.status_code == 409
        assert service.registry().models == renamed.models
    finally:
        repository.close()


def test_registry_reports_no_discount_for_manually_priced_models(
    pricing_postgres: tuple[str, Event],
) -> None:
    info, _waiting = pricing_postgres
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
        connection.execute(
            "INSERT INTO model_runtime_price(runtime_id,price_discount_percent) VALUES(%s,%s)",
            (runtime_id, 90),
        )
    repository = PostgreSqlOpsDbProxy(info)
    try:
        common: dict[str, Any] = {
            "provider_id": provider_id, "runtime_id": runtime_id, "capabilities": ["chat"],
            "allowed_roles": ["owner"], "input_cost_per_million": 0.36,
            "output_cost_per_million": 1.44,
        }
        manual = repository.create_registry_item("model", ManagedModelWrite(
            **common, model_key=str(uuid4()), display_name="Manual price",
        ).model_dump(mode="python"))
        retail = repository.create_registry_item("model", ManagedModelWrite(
            **common, model_key=str(uuid4()), display_name="Retail price",
            cached_cost_per_million=0.09, price_source=PriceSource.AZURE_RETAIL,
            price_reference="azure_retail:Azure OpenAI:gpt 4.1 mini:Global:*",
        ).model_dump(mode="python"))

        saved = {row["id"]: row for row in repository.registry()["models"]}

        assert saved[manual["id"]]["effective_discount_percent"] is None
        assert float(saved[retail["id"]]["effective_discount_percent"]) == 90
    finally:
        repository.close()


def test_context_prices_persist_and_reconcile_against_original_postgres_snapshot(
    pricing_postgres: tuple[str, Event], tmp_path: Path,
) -> None:
    info, _waiting = pricing_postgres
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
    catalog = Mock(spec=CompositeCatalog)
    entries = {}
    references = []
    for index, (minimum, maximum) in enumerate([(0, 1200), (1201, None)]):
        model = CatalogModel(
            key=f"azure_retail:Unit product:opaque-{index}", label=f"Opaque {index}",
            product="Unit product", source=PriceSource.AZURE_RETAIL,
            context_tier=PriceCatalogContextTier(
                group_key="unit-model", model_label="Unit model", key=f"tier-{index}",
                label=f"Tier {index}", order=index,
                input_range=PriceCatalogInputRange(
                    min_input_tokens=minimum, max_input_tokens=maximum,
                ),
            ),
        )
        reference = f"{model.key}:Global:*"
        references.append(reference)
        entry = CatalogEntry(
            reference=reference, label=model.label, source=model.source,
            input_per_million=3 + index * 3, output_per_million=15 + index * 7.5,
            cached_per_million=0.3 + index * 0.3, cache_write_per_million=3.75 + index * 3.75,
        )
        entries[model.key] = CatalogOptions(model=model, options=(CatalogOption(
            reference=reference, deployment="Global", entry=entry, regions=("eastus",),
            region_required=False,
        ),))
    catalog.options.side_effect = entries.get
    repository = PostgreSqlOpsDbProxy(info)
    try:
        service = ModelRuntimeService(
            repository, Settings(credential_key_file=tmp_path / "context.key"),
            price_catalog=catalog,
        )
        write = ManagedModelWrite(
            provider_id=provider_id, runtime_id=runtime_id, model_key=str(uuid4()),
            display_name="Context SQL test", capabilities=["chat"], allowed_roles=["owner"],
            context_price_references=references, price_source=PriceSource.AZURE_RETAIL,
            price_discount_percent=80,
        )
        registry = service.save_model(write)
        saved = next(model for model in registry.models if model.model_key == write.model_key)
        assert saved.context_price_references == references
        assert [tier.input_price_per_million for tier in saved.context_price_tiers] == [2.4, 4.8]
        assert saved.input_cost_per_million is None
        original_price = repository.model_prices()[str(saved.id)]
        processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
        attempts = [str(uuid4()) for _ in range(3)]

        def usage(attempt: str, **values: Any) -> dict[str, Any]:
            return {
                "id": attempt, "request_id": attempt, "correlation_id": attempt,
                "ts": datetime.now(UTC).isoformat(), "provider": "Unit", "model": write.model_key,
                "model_id": str(saved.id), "user_id": "context-test", "input_tokens": None,
                "cached_tokens": None, "output_tokens": None, "latency_ms": 10,
                "turn_index": 1,
                "status": "success", "status_code": 200, "estimated": False,
                "ingest_source": "eventhub", **values,
            }

        for attempt in attempts:
            assert processor.process(usage(attempt))
        changed = [tier.model_dump(mode="json") for tier in saved.context_price_tiers]
        for tier in changed:
            tier["input_price_per_million"] = 999
        repository.update_registry_item("model", saved.id, {"context_price_tiers": changed})
        fresh = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
        assert fresh.process(usage(
            attempts[0], input_tokens=1001, cached_tokens=200, output_tokens=100,
        ))
        correction = ReconciledUsage(
            correlation_id=attempts[1], input_tokens=1001, cached_tokens=200, output_tokens=100,
        )
        assert repository.apply_reconciled_usage([correction]) == 1
        assert repository.apply_reconciled_usage([correction]) == 0
        assert repository.apply_reconciled_usage([ReconciledUsage(
            correlation_id=attempts[2], input_tokens=1001, cached_tokens=None, output_tokens=100,
        )]) == 1
        with psycopg.connect(info, row_factory=dict_row) as row_connection:
            pending = row_connection.execute(
                "SELECT * FROM token_usage WHERE correlation_id=%s", (attempts[2],),
            ).fetchone()
            assert pending is not None and pending["input_price_per_million"] is None
            assert pending["price_context_tier"] is None and pending["reconciled_at"] is None
        assert repository.apply_reconciled_usage([correction.model_copy(update={
            "correlation_id": attempts[2],
        })]) == 1
        with psycopg.connect(info, row_factory=dict_row) as row_connection:
            rows = row_connection.execute(
                "SELECT * FROM token_usage WHERE correlation_id=ANY(%s)", (attempts,),
            ).fetchall()
        assert len(rows) == 3
        for row in rows:
            assert row["price_context_tier"] == "tier-1"
            assert float(row["input_price_per_million"]) == 4.8
            assert float(row["estimated_cost"]) == original_price.cost(1001, 200, 100)
            assert row["price_context_snapshot"] == original_price.model_dump(mode="json")
    finally:
        repository.close()


@pytest.mark.parametrize("source", list(PriceSource))
def test_publication_activation_persists_catalog_pricing_in_postgres(
    pricing_postgres: tuple[str, Event], source: PriceSource,
) -> None:
    info, _waiting = pricing_postgres
    # Each case publishes the gateway's first release, so it needs a gateway of its own.
    database = f"activation_{source.value}"
    with psycopg.connect(info, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    info = make_conninfo(info, dbname=database)
    _apply_migrations(info)
    repository = PostgreSqlOpsDbProxy(info)
    try:
        registry = repository.registry()
        gateway = next(row for row in registry["gateways"] if row["implementation"] == "apim")
        provider = next(
            (row for row in registry["providers"] if row["brand_key"] == "microsoft_foundry"), None,
        )
        catalog = Mock(spec=CompositeCatalog)
        reference = f"{source.value}:Unit catalog:Unit model:Regional:westus"
        catalog.lookup.return_value = CatalogEntry(
            reference=reference, label="Unit model", source=source,
            input_per_million=2, output_per_million=8, cached_per_million=0.5,
            cache_write_per_million=0,
        )
        following = source is not PriceSource.MANUAL
        service = GatewayControlPlaneService(
            repository, apim_principal_id=str(UUID(int=99)), price_catalog=catalog,
        )
        publication = service.publish(GatewayPublicationCreate(
            gateway_profile_id=gateway["id"],
            provider=ProviderTarget(existing_id=provider["id"])
            if provider else ProviderTarget(template="microsoft_foundry"),
            runtime=RuntimeTarget(foundry_project_endpoint=HttpUrl(
                f"https://unit-{uuid4().hex}.services.ai.azure.com/api/projects/unit"
            )),
            model=ModelCreateTarget(
                deployment_name=f"unit-{uuid4().hex}",
                input_cost_per_million=999 if following else 0,
                output_cost_per_million=999 if following else 4,
                price_source=source, price_reference=reference if following else None,
                price_discount_percent=80 if following else None,
            ),
        ), "sql-price-test")
        actor = f"price-worker-{uuid4()}"
        for before, after in zip(
            ("queued", "validating", "provisioning", "building_revision", "verifying"),
            ("validating", "provisioning", "building_revision", "verifying", "promoting"),
            strict=True,
        ):
            claimed = repository.claim_gateway_publication(actor, 180)
            assert claimed is not None and claimed["id"] == publication.id
            assert repository.transition_gateway_publication(
                publication.id, before, after, {}, actor,
            ) is not None
        claimed = repository.claim_gateway_publication(actor, 180)
        assert claimed is not None and claimed["id"] == publication.id
        activated = repository.activate_gateway_publication(publication.id, actor)
        assert activated["status"] == "active"
        saved = ManagedModel.model_validate(next(
            row for row in repository.registry()["models"]
            if row["publication_id"] == publication.id
        ))
        assert saved.price_source is source
        assert saved.price_reference == (reference if following else None)
        assert saved.price_discount_percent == (80 if following else None)
        assert saved.input_cost_per_million == (1.6 if following else 0)
        assert saved.output_cost_per_million == (6.4 if following else 4)
        assert saved.list_input_cost_per_million == (2 if following else None)
        assert saved.list_output_cost_per_million == (8 if following else None)
        assert saved.cached_cost_per_million == (0.4 if following else None)
        assert saved.cache_write_cost_per_million == (0 if following else None)
        assert saved.price_sync_status == ("ok" if following else None)
        assert saved.price_synced_at == (publication.created_at if following else None)
        if following:
            catalog.lookup.assert_called_once_with(reference)
        else:
            catalog.lookup.assert_not_called()
        repository.update_registry_item("model", saved.id, {
            "price_source": "manual", "input_cost_per_million": 7,
        })
        repository.activate_gateway_publication(publication.id, actor)
        with psycopg.connect(info, row_factory=dict_row) as connection:
            rows = connection.execute(
                """SELECT price.price_source, model.input_cost_per_million
                   FROM managed_model_price price
                   JOIN managed_model model ON model.id = price.model_id
                   WHERE model.id = %s""", (saved.id,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["price_source"] == "manual"
        assert rows[0]["input_cost_per_million"] == 7
    finally:
        repository.close()


def test_budget_usage_prices_free_pre_dispatch_denials_like_the_ledger_in_postgres(
    pricing_postgres: tuple[str, Event],
) -> None:
    info, _waiting = pricing_postgres
    repository = PostgreSqlOpsDbProxy(info)
    try:
        processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
        moment = datetime.now(UTC)
        users = {"denied": f"denied-{uuid4()}", "unpriced": f"unpriced-{uuid4()}"}
        outcomes = {
            "denied": {
                "status": "429", "status_code": 429,
                "ingest_error": "budget_pre_dispatch_denial",
                "budget_admission": "budget_exhausted",
            },
            "unpriced": {
                "status": "success", "status_code": 200,
                "input_tokens": 10, "cached_tokens": 0, "output_tokens": 1,
            },
        }
        for kind, user_id in users.items():
            attempt = str(uuid4())
            assert processor.process({
                "id": attempt, "request_id": attempt, "correlation_id": attempt,
                "ts": moment.isoformat(), "provider": "Unit", "model": "unit-model",
                "model_id": "unit-model", "user_id": user_id, "turn_index": 1,
                "latency_ms": 1, "estimated": True, "ingest_source": "eventhub",
                **outcomes[kind],
            })
        period = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        rows = {
            row["scope_id"]: row
            for row in repository.token_usage_by_budget_scope(
                period, moment + timedelta(seconds=1),
            )
            if row["scope_type"] == "user"
        }
        assert rows[users["denied"]]["unpriced_usage_count"] == 0
        assert rows[users["denied"]]["used_usd"] == 0
        assert rows[users["unpriced"]]["unpriced_usage_count"] == 1
        assert repository.gateway_application_usage(
            period.date(), (period + timedelta(days=32)).date(), [uuid4()],
        ) == []
    finally:
        repository.close()


def test_budget_overview_reports_people_model_assignments_in_postgres(
    pricing_postgres: tuple[str, Event],
) -> None:
    info, _waiting = pricing_postgres
    database = "budget_model_assignments"
    with psycopg.connect(info, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    info = make_conninfo(info, dbname=database)
    _apply_migrations(info)
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
        model = repository.create_registry_item("model", ManagedModelWrite(
            provider_id=provider_id, runtime_id=runtime_id, model_key=str(uuid4()),
            display_name="Assigned model", capabilities=["chat"], allowed_roles=["owner"],
            input_cost_per_million=1, output_cost_per_million=4,
        ).model_dump(mode="python"))
        today = datetime.now(UTC).date()
        repository.bulk_upsert_user_budgets(
            today.replace(day=1), "department-platform", [], 80, "sql-test",
            selected_user_ids=["test.user01@contoso.com"], model_ids=[model["id"]],
        )
        overview = TokenBudgetService(repository).overview(
            today.strftime("%Y-%m"), include_users=True,
        )
        people = {
            item.scope_id: (item.model_policy_configured, item.allowed_model_ids)
            for item in overview.items if item.scope_type == "user"
        }
        assert people["test.user01@contoso.com"] == (True, [model["id"]])
        assert people["test.user02@contoso.com"] == (False, [])
    finally:
        repository.close()


def test_backend_recorded_person_is_matched_by_request_and_person_in_postgres(
    pricing_postgres: tuple[str, Event],
) -> None:
    from tests.backend.model_platform.test_platform_invocation_trace import invocation_attempt

    info, _waiting = pricing_postgres
    repository = PostgreSqlOpsDbProxy(info)
    try:
        platform = invocation_attempt(f"platform-{uuid4()}")
        repository.begin_platform_invocation(platform)
        image_id, period = uuid4(), datetime.now(UTC).date().replace(day=1)
        with psycopg.connect(info, autocommit=True) as connection:
            connection.execute(
                """INSERT INTO billable_request_attempt (
                       id, operation_key, plan_sha256, scope_type, scope_id, period_start,
                       model_id, model_key, reserved_tokens, attempt_index, state)
                   VALUES (%s, %s, %s, 'person', 'alex@contoso.com', %s,
                           'unit-model', 'unit-model', 1, 1, 'started')""",
                (image_id, f"invocation:{image_id}", "0" * 64, period),
            )
        assert repository.platform_request_person_recorded(platform.request_id, "unit")
        assert repository.platform_request_person_recorded(str(image_id), "alex@contoso.com")
        assert not repository.platform_request_person_recorded(platform.request_id, "other")
        assert not repository.platform_request_person_recorded(str(image_id), "unit")
        assert not repository.platform_request_person_recorded("not-a-uuid", "unit")
    finally:
        repository.close()


def test_inventory_sync_adopts_the_application_already_bound_to_a_subscription_in_postgres(
    pricing_postgres: tuple[str, Event],
) -> None:
    from uuid import uuid5

    from turnstile_core.domain.application_access import (
        application_id_for,
        application_subscription_id_for,
    )

    info, _waiting = pricing_postgres
    repository = PostgreSqlOpsDbProxy(info)
    gateway = UUID("10000000-0000-4000-8000-000000000001")
    period = datetime.now(UTC).date().replace(day=1)
    probe = f"probe-{uuid4().hex[:8]}"

    def item(application_id: UUID, subscription_id: UUID) -> dict[str, Any]:
        return {
            "application_id": application_id, "subscription_id": subscription_id,
            "apim_subscription_id": probe, "slug": "publisher", "display_name": "Unit publisher",
            "application_type": "system", "system_managed": True, "state": "active",
            "scope_type": "api", "scope_id": "finops-foundry-llm", "scope_exists": True,
        }

    try:
        # v1.3 bootstrap keyed the system application by name, not by its subscription.
        legacy = uuid5(gateway, "publisher")
        repository.sync_gateway_applications(
            gateway, [item(legacy, uuid5(gateway, probe))], "installation", period, 1, 1,
        )
        repository.sync_gateway_applications(gateway, [{
            **item(application_id_for(gateway, probe),
                   application_subscription_id_for(gateway, probe)),
            "slug": probe, "audit_id": uuid4(), "source": "discovered",
        }], "unit-owner", period, 1, 1)
        with psycopg.connect(info) as connection:
            assert connection.execute(
                "SELECT id FROM gateway_application WHERE id = ANY(%s)",
                ([legacy, application_id_for(gateway, probe)],),
            ).fetchall() == [(legacy,)]
            assert connection.execute(
                """SELECT application_id FROM gateway_application_subscription
                   WHERE gateway_profile_id = %s AND apim_subscription_id = %s""",
                (gateway, probe),
            ).fetchall() == [(legacy,)]
    finally:
        repository.close()
