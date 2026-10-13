from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


class AuditStore:
    def __init__(self, database_url: str, pool_min_size: int = 1, pool_max_size: int = 2) -> None:
        self._pool = ConnectionPool(
            database_url,
            min_size=pool_min_size,
            max_size=pool_max_size,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        self._pool_lock = threading.Lock()
        self._pool_opened = False

    def _open_pool(self) -> None:
        if not self._pool_opened:
            with self._pool_lock:
                if not self._pool_opened:
                    self._pool.open()
                    self._pool_opened = True

    @contextmanager
    def transaction(self) -> Iterator[Connection[Any]]:
        self._open_pool()
        with self._pool.connection() as connection, connection.transaction():
            yield connection

    def close(self) -> None:
        with self._pool_lock:
            if self._pool_opened:
                self._pool.close()
                self._pool_opened = False

    @staticmethod
    def primary_tenant_id(connection: Connection[Any]) -> UUID:
        row = connection.execute("SELECT id FROM tenant WHERE is_primary AND enabled").fetchone()
        if row is None:
            raise RuntimeError("The primary tenant is not configured")
        return cast(UUID, row["id"])

    @staticmethod
    def write_audit_event(connection: Connection[Any], **event: Any) -> None:
        connection.execute(
            """
            INSERT INTO security_audit_event (
                tenant_id, actor_user_id, actor_email, actor_role, action,
                resource_type, resource_id, result, request_id, correlation_id,
                client_ip, user_agent, before_data, after_data
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::inet, %s, %s, %s
            )
            """,
            (
                event["tenant_id"],
                event.get("actor_user_id"),
                event.get("actor_email"),
                event.get("actor_role"),
                event["action"],
                event["resource_type"],
                event.get("resource_id"),
                event["result"],
                event.get("request_id"),
                event.get("correlation_id"),
                event.get("client_ip"),
                event.get("user_agent"),
                Jsonb(event.get("before_data") or {}),
                Jsonb(event.get("after_data") or {}),
            ),
        )

    @staticmethod
    def list_events(
        connection: Connection[Any],
        tenant_id: UUID,
        *,
        from_: datetime | None,
        to: datetime | None,
        actor_id: UUID | None,
        action: str | None,
        result: str | None,
        cursor: UUID | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id = %s"]
        parameters: list[Any] = [tenant_id]
        for column, value in (
            ("created_at >= %s", from_),
            ("created_at < %s", to),
            ("actor_user_id = %s", actor_id),
            ("action = %s", action),
            ("result = %s", result),
        ):
            if value is not None:
                conditions.append(column)
                parameters.append(value)
        if cursor is not None:
            conditions.append(
                "(created_at, id) < (SELECT created_at, id FROM security_audit_event "
                "WHERE tenant_id = %s AND id = %s)"
            )
            parameters.extend((tenant_id, cursor))
        parameters.append(limit)
        rows = connection.execute(
            f"""
            SELECT id, created_at, actor_user_id, actor_email, actor_role, action,
                   resource_type, resource_id, result, request_id, correlation_id,
                   client_ip::text AS client_ip, user_agent, before_data, after_data
            FROM security_audit_event
            WHERE {" AND ".join(conditions)}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def delete_expired_events(connection: Connection[Any], cutoff: datetime) -> int:
        cursor = connection.execute(
            "DELETE FROM security_audit_event WHERE created_at < %s", (cutoff,)
        )
        return cursor.rowcount or 0
