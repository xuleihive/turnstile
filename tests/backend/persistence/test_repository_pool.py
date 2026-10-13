from __future__ import annotations

import inspect
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy, QueryRepository
from turnstile_core.persistence.repository_support import UsageFilters

UNREACHABLE = "postgresql://nobody@127.0.0.1:1/does-not-exist"


def test_postgresql_repository_preserves_the_public_contract() -> None:
    assert PostgreSqlOpsDbProxy.__module__ == "turnstile_core.persistence.repository"
    assert not inspect.isabstract(PostgreSqlOpsDbProxy)
    assert all(
        callable(getattr(PostgreSqlOpsDbProxy, method_name))
        for method_name in QueryRepository.__abstractmethods__
    )


def test_constructing_the_repository_does_not_connect() -> None:
    # The pool is opened on first use, not in __init__. Connecting eagerly would make an
    # unreachable database fail process start-up instead of failing the request that
    # actually needs it, and would break every test that only builds the object.
    proxy = PostgreSqlOpsDbProxy(UNREACHABLE)

    assert proxy._pool_opened is False


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def execute(self, *_: Any, **__: Any) -> _Cursor:
        return _Cursor(self._rows)


def _stub(proxy: PostgreSqlOpsDbProxy, rows: list[dict[str, Any]], reads: list[int]) -> None:
    @contextmanager
    def connection() -> Any:
        reads.append(1)
        yield _Connection(rows)

    proxy._connection = connection  # type: ignore[method-assign]


def test_model_identities_are_cached_between_calls() -> None:
    # This is read on every denied invocation. Against the deployed cross-region database a
    # full registry read cost about two seconds each time, purely to turn a UUID into a name.
    rows = [{"model_uuid": "uuid-1", "model_key": "gpt-5.6-luna", "display_name": "Luna"}]
    reads: list[int] = []
    proxy = PostgreSqlOpsDbProxy(UNREACHABLE)
    _stub(proxy, rows, reads)

    first = proxy.model_identities()
    second = proxy.model_identities()

    assert len(reads) == 1
    assert first["uuid-1"].display_name == "Luna"
    assert second["gpt-5.6-luna"].model_id == "uuid-1"


def test_an_expired_window_reads_the_registry_again() -> None:
    rows = [{"model_uuid": "uuid-1", "model_key": "gpt-5.6-luna", "display_name": "Luna"}]
    reads: list[int] = []
    proxy = PostgreSqlOpsDbProxy(UNREACHABLE, identity_cache_seconds=0)
    _stub(proxy, rows, reads)

    proxy.model_identities()
    proxy.model_identities()

    assert len(reads) == 2


def test_a_registry_write_invalidates_the_cached_identities() -> None:
    # Without invalidation an administrator could rename a model and keep seeing the old name
    # in traces for a full window, which reads as the save having failed.
    rows = [{"model_uuid": "uuid-1", "model_key": "gpt-5.6-luna", "display_name": "Luna"}]
    reads: list[int] = []
    proxy = PostgreSqlOpsDbProxy(UNREACHABLE)
    _stub(proxy, rows, reads)

    assert proxy.model_identities()["uuid-1"].display_name == "Luna"
    rows[0]["display_name"] = "Luna Renamed"
    assert proxy.model_identities()["uuid-1"].display_name == "Luna"  # still cached

    proxy._invalidate_identities()

    assert proxy.model_identities()["uuid-1"].display_name == "Luna Renamed"
    assert len(reads) == 2


class _ExportCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.fetch_sizes: list[int] = []
        self.query = ""
        self.parameters: list[Any] = []
        self.name = ""

    def __enter__(self) -> _ExportCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, parameters: list[Any]) -> None:
        self.query = query
        self.parameters = parameters

    def fetchmany(self, size: int) -> list[dict[str, Any]]:
        self.fetch_sizes.append(size)
        rows, self.rows = self.rows[:size], self.rows[size:]
        return rows


class _ExportConnection:
    def __init__(self, cursor: _ExportCursor) -> None:
        self.export_cursor = cursor

    def cursor(self, *, name: str) -> _ExportCursor:
        self.export_cursor.name = name
        return self.export_cursor


def test_usage_export_uses_filtered_server_cursor_in_batches() -> None:
    rows = [{"request_id": f"request-{index}"} for index in range(1001)]
    cursor = _ExportCursor(rows)
    proxy = PostgreSqlOpsDbProxy(UNREACHABLE)

    @contextmanager
    def connection() -> Any:
        yield _ExportConnection(cursor)

    proxy._connection = connection  # type: ignore[method-assign]
    from_ = datetime(2026, 7, 1, tzinfo=UTC)
    to = datetime(2026, 8, 1, tzinfo=UTC)

    exported = list(
        proxy.iter_usage_requests(
            from_, to, UsageFilters(organization_id="org-contoso-global")
        )
    )

    assert len(exported) == 1001
    assert cursor.name == "turnstile_usage_export"
    assert cursor.parameters == [from_, to, "org-contoso-global"]
    assert "usage_domain = 'apim'" in cursor.query
    assert "LIMIT %s" not in cursor.query
    assert cursor.fetch_sizes == [1000, 1000, 1000]
