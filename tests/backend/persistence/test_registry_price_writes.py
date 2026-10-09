"""Where the price fields are written, and where they must never be written.

The registry reads `SELECT model.*` and `SELECT runtime.*` into Pydantic models declared
extra="forbid". A price column added to managed_model or model_runtime is therefore a field the
*previous* release cannot parse, which turns a code rollback into hand-written DDL on a live
database. These tests fail if anyone puts one back.

The connection is a fake that records SQL rather than running it -- the same approach as
test_repository_pool.py. That is enough here: the question is which statement goes to which
table, not what PostgreSQL does with it.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest

from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy

UNREACHABLE = "postgresql://nobody@127.0.0.1:1/does-not-exist"

PRICE_FIELDS = (
    "price_source",
    "price_reference",
    "price_discount_percent",
    "list_input_cost_per_million",
    "list_output_cost_per_million",
    "list_cached_cost_per_million",
    "list_cache_write_cost_per_million",
    "price_synced_at",
    "price_sync_status",
    "price_sync_message",
)


Row = dict[str, Any] | None


class Recorder:
    """Records every statement and hands back whatever row the test queued."""

    def __init__(self, rows: list[Row]) -> None:
        self.statements: list[str] = []
        self._rows = rows

    def execute(self, statement: str, parameters: Any = None) -> Recorder:
        self.statements.append(" ".join(statement.split()))
        return self

    def fetchone(self) -> Row:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return []

    @contextmanager
    def transaction(self) -> Any:
        yield self

    def writes_to(self, table: str) -> list[str]:
        pattern = re.compile(rf"^(INSERT INTO|UPDATE)\s+{re.escape(table)}\b", re.IGNORECASE)
        return [item for item in self.statements if pattern.match(item)]


def proxy_with(rows: list[Row]) -> tuple[PostgreSqlOpsDbProxy, Recorder]:
    recorder = Recorder(rows)
    proxy = PostgreSqlOpsDbProxy(UNREACHABLE)

    @contextmanager
    def connection() -> Any:
        yield recorder

    proxy._connection = connection  # type: ignore[method-assign]
    return proxy, recorder


def model_row(model_id: UUID) -> dict[str, Any]:
    return {"id": model_id, "model_key": "gpt-4.1-mini"}


# ------------------------------------------------------------------------------------------
# Creating
# ------------------------------------------------------------------------------------------


def test_creating_a_model_keeps_the_price_fields_off_managed_model() -> None:
    model_id = uuid4()
    proxy, recorder = proxy_with([model_row(model_id)])

    proxy.create_registry_item("model", {
        "provider_id": uuid4(), "runtime_id": uuid4(), "model_key": "gpt-4.1-mini",
        "display_name": "gpt 4.1 mini", "enabled": True, "is_default": False,
        "capabilities": ["chat"], "context_window": None,
        "input_cost_per_million": 0.36, "output_cost_per_million": 1.44,
        "cached_cost_per_million": None, "cache_write_cost_per_million": None,
        "allowed_roles": ["owner"],
        "price_source": "azure_retail",
        "price_reference": "azure_retail:Azure OpenAI:gpt 4.1 mini:Global:*",
        "price_discount_percent": 90,
    })

    inserted = recorder.writes_to("managed_model")
    assert len(inserted) == 1
    for field in PRICE_FIELDS:
        assert field not in inserted[0], (
            f"{field} is being written to managed_model; the previous release reads "
            "SELECT model.* into a forbid-extra model and will reject it"
        )
    assert recorder.writes_to("managed_model_price"), "the price row was never written"


def test_creating_a_model_priced_by_hand_records_the_explicit_choice() -> None:
    """An absent row now follows the public default, so explicit manual must be persisted."""
    proxy, recorder = proxy_with([model_row(uuid4())])

    proxy.create_registry_item("model", {
        "provider_id": uuid4(), "runtime_id": uuid4(), "model_key": "m",
        "display_name": "m", "enabled": True, "is_default": False,
        "capabilities": ["chat"], "context_window": None,
        "input_cost_per_million": 1.0, "output_cost_per_million": 2.0,
        "cached_cost_per_million": None, "cache_write_cost_per_million": None,
        "allowed_roles": ["owner"],
        "price_source": "manual", "price_reference": None,
        "price_discount_percent": None,
    })

    assert recorder.writes_to("managed_model_price")


def test_creating_a_connection_keeps_the_discount_off_model_runtime() -> None:
    proxy, recorder = proxy_with([{"id": uuid4()}])

    proxy.create_registry_item("runtime", {
        "provider_id": uuid4(), "gateway_profile_id": None, "name": "conn",
        "runtime_kind": "azure_openai", "enabled": True, "is_default": False,
        "config": {}, "allowed_roles": ["owner"], "price_discount_percent": 90,
    })

    inserted = recorder.writes_to("model_runtime")
    assert len(inserted) == 1
    assert "price_discount_percent" not in inserted[0]
    assert recorder.writes_to("model_runtime_price")


# ------------------------------------------------------------------------------------------
# Updating
# ------------------------------------------------------------------------------------------


def test_updating_a_price_does_not_update_managed_model() -> None:
    model_id = uuid4()
    proxy, recorder = proxy_with([None, model_row(model_id), None])

    proxy.update_registry_item("model", model_id, {
        "price_source": "azure_retail",
        "price_reference": "azure_retail:Azure OpenAI:gpt 4.1 mini:Global:*",
    })

    assert recorder.writes_to("managed_model") == []
    assert recorder.writes_to("managed_model_price")


def test_a_price_edit_arriving_alone_is_still_applied() -> None:
    """Pricing a model is the one edit that always arrives on its own: no other field moves."""
    model_id = uuid4()
    proxy, recorder = proxy_with([None, model_row(model_id), None])

    result = proxy.update_registry_item("model", model_id,
                                        {"price_discount_percent": 66})

    assert result is not None
    assert result["price_discount_percent"] == 66


def test_going_back_to_manual_drops_the_catalog_reference() -> None:
    """Otherwise the model reads as "following gpt 4.1 mini" while charging a typed number."""
    model_id = uuid4()
    proxy, recorder = proxy_with([
        None,
        model_row(model_id),
        {"price_source": "azure_retail", "price_reference": "azure_retail:x:y:Global:*",
         "price_discount_percent": 90},
    ])

    result = proxy.update_registry_item("model", model_id, {"price_source": "manual"})

    assert result is not None
    assert result["price_reference"] is None


def test_an_edit_that_names_no_price_field_writes_no_price_row() -> None:
    model_id = uuid4()
    proxy, recorder = proxy_with([None, model_row(model_id)])

    proxy.update_registry_item("model", model_id, {"display_name": "renamed"})

    assert recorder.writes_to("managed_model_price") == []


# ------------------------------------------------------------------------------------------
# Syncing
# ------------------------------------------------------------------------------------------


def sync_result(**overrides: Any) -> dict[str, Any]:
    planned: dict[str, Any] = {
        "model_id": uuid4(), "writes": True, "status": "ok", "message": None,
        "input_cost_per_million": 0.36, "output_cost_per_million": 1.44,
        "cached_cost_per_million": 0.09, "cache_write_cost_per_million": None,
        "list_input_cost_per_million": 0.40, "list_output_cost_per_million": 1.60,
        "list_cached_cost_per_million": 0.10, "list_cache_write_cost_per_million": None,
        "pending_list_price": None,
        "expected_source": "azure_retail",
        "expected_reference": "azure_retail:Azure OpenAI:gpt 4.1 mini:Global:*",
        "expected_discount_percent": 90,
    }
    planned.update(overrides)
    return planned


@pytest.mark.parametrize("kind", ["model", "runtime"])
def test_sync_and_edits_lock_pricing_before_reading_configuration(kind: str) -> None:
    sync_proxy, sync_recorder = proxy_with([{"id": uuid4()}])
    sync_proxy.apply_model_price_sync([sync_result()])
    rows: list[Row] = [model_row(uuid4()), None]
    if kind == "model":
        rows.insert(0, None)
    edit_proxy, edit_recorder = proxy_with(rows)

    edit_proxy.update_registry_item(kind, uuid4(), {"price_discount_percent": 80})

    lock = "SELECT pg_advisory_xact_lock(hashtextextended('model-pricing', 0))"
    assert sync_recorder.statements[0] == lock
    assert edit_recorder.statements[0] == lock


def test_a_sync_writes_the_charged_rates_to_managed_model_and_nothing_else() -> None:
    """The rates are what the billing path reads and have always lived there. Only the record
    of where they came from is new, and only that record moved."""
    proxy, recorder = proxy_with([{"id": uuid4()}])

    written = proxy.apply_model_price_sync([sync_result()])

    assert written == 1
    updated = recorder.writes_to("managed_model")
    assert len(updated) == 1
    assert "input_cost_per_million" in updated[0]
    for field in ("list_input_cost_per_million", "price_sync_status", "price_synced_at"):
        assert field not in updated[0]
    assert recorder.writes_to("managed_model_price")


def test_the_rate_write_is_guarded_by_the_configuration_it_was_planned_against() -> None:
    """Without the guard, a sync that started before someone switched the model back to manual
    finishes afterwards and puts a synced rate on a row the screen calls hand-typed."""
    proxy, recorder = proxy_with([{"id": uuid4()}])

    proxy.apply_model_price_sync([sync_result()])

    statement = recorder.writes_to("managed_model")[0]
    assert "expected_source" in statement
    assert "expected_reference" in statement
    assert "expected_discount_percent" in statement
    assert "RETURNING" in statement, "the write has to report whether it matched anything"


def test_a_guarded_write_that_matched_nothing_is_recorded_as_superseded() -> None:
    proxy, recorder = proxy_with([])  # the guarded UPDATE returns no row

    written = proxy.apply_model_price_sync([sync_result()])

    assert written == 0
    price_writes = recorder.writes_to("managed_model_price")
    assert len(price_writes) == 1
    assert "superseded" in price_writes[0]
    assert "list_input_cost_per_million" not in price_writes[0], (
        "a write that did not happen must not move the baseline either"
    )


def test_a_price_held_for_review_does_not_move_the_accepted_baseline() -> None:
    """The whole point of the threshold: the figure it stopped must not become the figure it
    compares against, or the next run finds no drift and charges it."""
    proxy, recorder = proxy_with([])

    proxy.apply_model_price_sync([sync_result(
        writes=False, status="review_needed", message="变动 900%",
        pending_list_price={"input": 4.0, "output": 16.0,
                            "cached": None, "cache_write": None},
    )])

    assert recorder.writes_to("managed_model") == []
    statement = next(
        item for item in recorder.writes_to("managed_model_price") if "pending_list_price" in item
    )
    assert "pending_list_price" in statement
    # The baseline columns appear, but only inside a CASE that leaves them alone unless the
    # price was accepted.
    assert "CASE" in statement and "%(writes)s" in statement


def test_the_proposal_parameter_is_cast_so_postgresql_can_type_it() -> None:
    """Without the cast this statement is rejected on every ordinary sync.

    The parameter appears only inside `IS NOT NULL` and a CASE branch, so when it is NULL --
    which it is whenever nothing is waiting for review, i.e. almost always -- PostgreSQL has
    nothing to infer a type from and answers `could not determine data type of parameter $3`.
    A recording fake cannot see that; this pins the cast instead.
    """
    proxy, recorder = proxy_with([{"id": uuid4()}])

    proxy.apply_model_price_sync([sync_result()])

    statement = next(
        item for item in recorder.writes_to("managed_model_price") if "pending_list_price" in item
    )
    assert "%(pending_list_price)s::jsonb IS NOT NULL" in statement
    assert "THEN %(pending_list_price)s::jsonb" in statement


def test_a_skipped_sync_touches_only_the_record() -> None:
    proxy, recorder = proxy_with([])

    written = proxy.apply_model_price_sync([sync_result(
        writes=False, status="unmapped", message="找不到",
        list_input_cost_per_million=None, list_output_cost_per_million=None,
        list_cached_cost_per_million=None, list_cache_write_cost_per_million=None,
    )])

    assert written == 0
    assert recorder.writes_to("managed_model") == []
    assert recorder.writes_to("managed_model_price")


# ------------------------------------------------------------------------------------------
# Reading
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["managed_model_price", "model_runtime_price"])
def test_the_registry_reads_the_price_side_tables(table: str) -> None:
    proxy, recorder = proxy_with([])
    proxy.registry()
    assert any(table in statement for statement in recorder.statements)


def test_the_registry_still_selects_the_whole_model_row() -> None:
    """`SELECT model.*` is what makes a rollback survivable. Narrowing it would be a silent
    break the other way: a column added later would stop reaching the API."""
    proxy, recorder = proxy_with([])
    proxy.registry()
    assert any("SELECT model.*" in statement for statement in recorder.statements)
