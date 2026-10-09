"""Exercise overlapping pricing transactions in an owned local PostgreSQL cluster."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from backend.services.runtime_service import ModelRuntimeService
from turnstile_core.config import Settings
from turnstile_core.domain.runtime_models import ManagedModel, ManagedModelWrite, PriceSource
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.pricing.catalog import CatalogEntry, CompositeCatalog
from turnstile_core.pricing.sync import plan_price_sync


@pytest.fixture(scope="module")
def pricing_postgres() -> Iterator[tuple[str, Event]]:
    initdb = shutil.which("initdb")
    postgres = shutil.which("postgres")
    if initdb is None or postgres is None:
        pytest.skip("Local initdb and postgres are required for transaction validation")
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
            root = Path(__file__).resolve().parents[3]
            with psycopg.connect(info, autocommit=True) as connection:
                connection.execute((root / "migrations/001_initial_schema.up.sql").read_text())
                connection.execute("SET search_path TO public")
                for name in (
                    "007_model_price_source", "008_price_review_and_guard",
                    "010_models_dev_pricing",
                ):
                    connection.execute((root / f"migrations/{name}.up.sql").read_text())
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
        if kind == "model":
            assert saved["price_sync_status"] is None
        else:
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
