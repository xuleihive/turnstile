from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


class AccessControlStore:
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
        row = cast(
            dict[str, Any] | None,
            connection.execute(
                "SELECT id FROM tenant WHERE is_primary AND enabled"
            ).fetchone(),
        )
        if row is None:
            raise RuntimeError("The primary tenant is not configured")
        return cast(UUID, row["id"])

    @staticmethod
    def list_users(
        connection: Connection[Any],
        tenant_id: UUID,
        *,
        search: str | None,
        role: str | None,
        enabled: bool | None,
        organization_unit_id: str | None,
    ) -> list[dict[str, Any]]:
        conditions = ["user_row.tenant_id = %s"]
        parameters: list[Any] = [tenant_id]
        if search:
            conditions.append("(user_row.email ILIKE %s OR user_row.display_name ILIKE %s)")
            parameters.extend((f"%{search.strip()}%", f"%{search.strip()}%"))
        if role:
            conditions.append("user_row.role = %s")
            parameters.append(role)
        if enabled is not None:
            conditions.append("user_row.enabled = %s")
            parameters.append(enabled)
        if organization_unit_id:
            conditions.append("user_row.organization_unit_id = %s")
            parameters.append(organization_unit_id)
        rows = connection.execute(
            f"""
            SELECT user_row.id, user_row.email, user_row.display_name, user_row.role,
                   user_row.enabled, user_row.organization_unit_id,
                   organization_unit.display_name AS organization_unit_name,
                   user_row.created_at, user_row.last_login_at
            FROM app_user AS user_row
            LEFT JOIN organization_unit
              ON organization_unit.id = user_row.organization_unit_id
             AND organization_unit.tenant_id = user_row.tenant_id
            WHERE {' AND '.join(conditions)}
            ORDER BY user_row.email, user_row.id
            LIMIT 500
            """,
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def list_roles(connection: Connection[Any]) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT role.code, role.display_name, role.is_builtin,
                   COALESCE(
                       array_agg(permission.code ORDER BY permission.code)
                           FILTER (WHERE permission.code IS NOT NULL),
                       ARRAY[]::text[]
                   ) AS permissions
            FROM app_role AS role
            LEFT JOIN app_role_permission AS role_permission
              ON role_permission.role_code = role.code
            LEFT JOIN app_permission AS permission
              ON permission.code = role_permission.permission_code
            GROUP BY role.code, role.display_name, role.is_builtin
            ORDER BY role.is_builtin DESC, role.code
            """
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def list_organization_units(
        connection: Connection[Any], tenant_id: UUID
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT id, parent_id, code, display_name, enabled, created_at
            FROM organization_unit
            WHERE tenant_id = %s
            ORDER BY parent_id NULLS FIRST, display_name, id
            """,
            (tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def user_by_email(
        connection: Connection[Any], tenant_id: UUID, email: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT id, email, display_name, role, enabled, organization_unit_id, created_at,
                   last_login_at
            FROM app_user
            WHERE tenant_id = %s AND email = %s
            """,
            (tenant_id, email),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def user_by_id(
        connection: Connection[Any], tenant_id: UUID, user_id: UUID
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT user_row.id, user_row.email, user_row.display_name, user_row.role,
                   user_row.enabled, user_row.tenant_id, user_row.organization_unit_id,
                   organization_unit.display_name AS organization_unit_name,
                   user_row.created_at, user_row.last_login_at
            FROM app_user AS user_row
            LEFT JOIN organization_unit
              ON organization_unit.id = user_row.organization_unit_id
             AND organization_unit.tenant_id = user_row.tenant_id
            WHERE user_row.tenant_id = %s AND user_row.id = %s
            """,
            (tenant_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def role_exists(connection: Connection[Any], role: str) -> bool:
        row = cast(
            dict[str, Any] | None,
            connection.execute(
                "SELECT EXISTS (SELECT 1 FROM app_role WHERE code = %s) AS exists", (role,)
            ).fetchone(),
        )
        if row is None:
            raise RuntimeError("Could not check whether the role exists")
        return bool(row["exists"])

    @staticmethod
    def count_enabled_owners(connection: Connection[Any], tenant_id: UUID) -> int:
        rows = connection.execute(
            """
            SELECT id
            FROM app_user
            WHERE tenant_id = %s AND role = 'owner' AND enabled
            ORDER BY id
            FOR UPDATE
            """,
            (tenant_id,),
        ).fetchall()
        return len(rows)

    @staticmethod
    def create_user(
        connection: Connection[Any],
        tenant_id: UUID,
        *,
        email: str,
        display_name: str | None,
        role: str,
        organization_unit_id: UUID | None,
        password_hash: str | None,
    ) -> dict[str, Any]:
        row = cast(dict[str, Any] | None, connection.execute(
            """
            INSERT INTO app_user (
                tenant_id, email, display_name, password_hash, role, organization_unit_id
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, email, display_name, role, enabled, organization_unit_id,
                      created_at, last_login_at
            """,
            (tenant_id, email, display_name, password_hash, role, organization_unit_id),
        ).fetchone())
        if row is None:
            raise RuntimeError("Could not create user")
        return dict(row)

    @staticmethod
    def update_user(
        connection: Connection[Any],
        tenant_id: UUID,
        user_id: UUID,
        patch: Mapping[str, Any],
        *,
        actor: str,
    ) -> dict[str, Any] | None:
        column_names = {
            "display_name": "display_name",
            "role": "role",
            "enabled": "enabled",
            "organization_unit_id": "organization_unit_id",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, column in column_names.items():
            if key in patch:
                assignments.append(f"{column} = %s")
                values.append(patch[key])
        if not assignments:
            return AccessControlStore.user_by_id(connection, tenant_id, user_id)
        values.extend((tenant_id, user_id))
        row = connection.execute(
            f"""
            UPDATE app_user SET {', '.join(assignments)}
            WHERE tenant_id = %s AND id = %s
            RETURNING id, email, display_name, role, enabled, organization_unit_id,
                      created_at, last_login_at
            """,
            values,
        ).fetchone()
        if row is None:
            return None
        if "organization_unit_id" in patch:
            organization_unit_id = patch["organization_unit_id"]
            connection.execute(
                """UPDATE gateway_application
                   SET organization_unit_id = %s,
                       department_id = %s,
                       updated_by = %s,
                       updated_at = now()
                   WHERE tenant_id = %s
                     AND owner_user_id = %s
                     AND system_managed = FALSE""",
                (
                    organization_unit_id,
                    str(organization_unit_id) if organization_unit_id else None,
                    actor,
                    tenant_id,
                    user_id,
                ),
            )
        return dict(row)

    @staticmethod
    def revoke_user_sessions(connection: Connection[Any], user_id: UUID) -> None:
        connection.execute("DELETE FROM user_session WHERE user_id = %s", (user_id,))

    @staticmethod
    def organization_unit_by_id(
        connection: Connection[Any], tenant_id: UUID, unit_id: UUID
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT id, parent_id, code, display_name, enabled, created_at
            FROM organization_unit WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, unit_id),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def organization_unit_in_use(
        connection: Connection[Any], tenant_id: UUID, unit_id: UUID
    ) -> bool:
        row = cast(
            dict[str, Any] | None,
            connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM app_user
                    WHERE tenant_id = %s AND organization_unit_id = %s
                ) OR EXISTS (
                    SELECT 1 FROM organization_unit
                    WHERE tenant_id = %s AND parent_id = %s
                ) AS in_use
                """,
                (tenant_id, unit_id, tenant_id, unit_id),
            ).fetchone(),
        )
        if row is None:
            raise RuntimeError("Could not check organization unit usage")
        return bool(row["in_use"])

    @staticmethod
    def create_organization_unit(
        connection: Connection[Any],
        tenant_id: UUID,
        *,
        parent_id: UUID | None,
        code: str,
        display_name: str,
    ) -> dict[str, Any]:
        row = cast(dict[str, Any] | None, connection.execute(
            """
            INSERT INTO organization_unit (tenant_id, parent_id, code, display_name)
            VALUES (%s, %s, %s, %s)
            RETURNING id, parent_id, code, display_name, enabled, created_at
            """,
            (tenant_id, parent_id, code, display_name),
        ).fetchone())
        if row is None:
            raise RuntimeError("Could not create organization unit")
        return dict(row)

    @staticmethod
    def update_organization_unit(
        connection: Connection[Any],
        tenant_id: UUID,
        unit_id: UUID,
        patch: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        assignments: list[str] = []
        values: list[Any] = []
        for key in ("display_name", "enabled"):
            if key in patch:
                assignments.append(f"{key} = %s")
                values.append(patch[key])
        if not assignments:
            return AccessControlStore.organization_unit_by_id(connection, tenant_id, unit_id)
        values.extend((tenant_id, unit_id))
        row = connection.execute(
            f"""
            UPDATE organization_unit SET {', '.join(assignments)}
            WHERE tenant_id = %s AND id = %s
            RETURNING id, parent_id, code, display_name, enabled, created_at
            """,
            values,
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def delete_organization_unit(
        connection: Connection[Any], tenant_id: UUID, unit_id: UUID
    ) -> bool:
        cursor = connection.execute(
            "DELETE FROM organization_unit WHERE tenant_id = %s AND id = %s",
            (tenant_id, unit_id),
        )
        return bool(cursor.rowcount)

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
                event["actor_user_id"],
                event["actor_email"],
                event["actor_role"],
                event["action"],
                event["resource_type"],
                event.get("resource_id"),
                event.get("result", "success"),
                event.get("request_id"),
                event.get("correlation_id"),
                event.get("client_ip"),
                event.get("user_agent"),
                Jsonb(event.get("before_data") or {}),
                Jsonb(event.get("after_data") or {}),
            ),
        )
