from __future__ import annotations

import inspect
from contextlib import contextmanager
from typing import Any

from backend.data_sources.github_copilot.store import CopilotStore
from turnstile_core.config import Settings
from turnstile_core.persistence.auth_store import AuthStore
from turnstile_core.persistence.pool_health import CONNECTION_HEALTH
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy, QueryRepository

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
    assert proxy._apim_api_id == Settings.model_fields["apim_api_id"].default
    configured = PostgreSqlOpsDbProxy(UNREACHABLE, apim_api_id="unit-configured-api")
    assert configured._apim_api_id == "unit-configured-api"
    assert configured._pool_opened is False


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


def test_api_pools_bound_connects_and_drop_connections_a_lost_server_abandoned() -> None:
    pools = [
        PostgreSqlOpsDbProxy(UNREACHABLE)._pool, AuthStore(UNREACHABLE)._pool,
        CopilotStore(UNREACHABLE)._pool,
    ]
    for pool in pools:
        assert isinstance(pool.kwargs, dict)
        assert pool.kwargs.items() >= CONNECTION_HEALTH.items()


class _EnforcementConnection:
    """No stored mode row; records the parameters of every write."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, Any]] = []

    def execute(self, statement: str, parameters: Any = None) -> _EnforcementConnection:
        if statement.lstrip().startswith("INSERT"):
            self.writes.append((" ".join(statement.split()), parameters))
        return self

    def fetchone(self) -> None:
        return None

    @contextmanager
    def transaction(self) -> Any:
        yield self


def test_first_department_enforcement_change_audits_the_implicit_audit_mode() -> None:
    # A department with no stored row is in audit mode; recording null made the first
    # change read as if there had been no mode at all.
    proxy = PostgreSqlOpsDbProxy(UNREACHABLE)
    connection = _EnforcementConnection()

    @contextmanager
    def connect() -> Any:
        yield connection

    proxy._connection = connect  # type: ignore[method-assign]

    assert proxy.set_department_enforcement("department-platform", "block", "owner")
    [audit] = [params for sql, params in connection.writes if "department_enforcement_audit" in sql]
    assert audit[1:4] == ("department-platform", "audit", "block")
    assert proxy.set_department_enforcement("department-platform", "audit", "owner") is False
