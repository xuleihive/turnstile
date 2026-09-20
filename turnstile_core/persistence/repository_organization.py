from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Any, cast

from psycopg.types.json import Jsonb


class PostgreSqlOrganizationRepositoryMixin:
    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def org_units(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM org_unit
                   ORDER BY unit_type DESC, display_name, id"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def create_org_unit(
        self,
        unit_id: str,
        unit_type: str,
        parent_id: str | None,
        display_name: str,
        actor: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            existing = connection.execute(
                "SELECT id FROM org_unit WHERE id = %s", (unit_id,)
            ).fetchone()
            if existing is not None:
                raise ValueError(f"An organization unit already exists with id {unit_id}")
            if parent_id is not None:
                parent = connection.execute(
                    "SELECT id, status FROM org_unit WHERE id = %s FOR SHARE", (parent_id,)
                ).fetchone()
                if parent is None:
                    raise ValueError(f"Unknown parent: {parent_id}")
            row = connection.execute(
                """INSERT INTO org_unit (
                       id, unit_type, parent_id, display_name, created_by, updated_by
                   ) VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (unit_id, unit_type, parent_id, display_name, actor, actor),
            ).fetchone()
            connection.execute(
                """INSERT INTO org_unit_audit (unit_id, operation, after_state, actor)
                   VALUES (%s, 'created', %s, %s)""",
                (unit_id, Jsonb({"display_name": display_name, "parent_id": parent_id}), actor),
            )
        return cast(dict[str, Any], row)

    def rename_org_unit(
        self, unit_id: str, display_name: str, actor: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            existing = connection.execute(
                "SELECT * FROM org_unit WHERE id = %s FOR UPDATE", (unit_id,)
            ).fetchone()
            if existing is None:
                return None
            if existing["display_name"] == display_name:
                return dict(existing)
            row = connection.execute(
                """UPDATE org_unit
                   SET display_name = %s, updated_by = %s, updated_at = now()
                   WHERE id = %s RETURNING *""",
                (display_name, actor, unit_id),
            ).fetchone()
            connection.execute(
                """INSERT INTO org_unit_audit (
                       unit_id, operation, before_state, after_state, actor
                   ) VALUES (%s, 'renamed', %s, %s, %s)""",
                (
                    unit_id,
                    Jsonb({"display_name": existing["display_name"]}),
                    Jsonb({"display_name": display_name}),
                    actor,
                ),
            )
        return cast(dict[str, Any], row)

    def set_org_unit_status(
        self, unit_id: str, status: str, actor: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            existing = connection.execute(
                "SELECT * FROM org_unit WHERE id = %s FOR UPDATE", (unit_id,)
            ).fetchone()
            if existing is None:
                return None
            if existing["status"] == status:
                return dict(existing)
            if status == "retired" and existing["unit_type"] == "department":
                children = connection.execute(
                    """SELECT count(*) AS total FROM org_unit
                       WHERE parent_id = %s AND status = 'active'""",
                    (unit_id,),
                ).fetchone()
                if children and int(children["total"]):
                    raise ValueError("Retire the units under this one first")
            row = connection.execute(
                """UPDATE org_unit
                   SET status = %s, updated_by = %s, updated_at = now()
                   WHERE id = %s RETURNING *""",
                (status, actor, unit_id),
            ).fetchone()
            connection.execute(
                """INSERT INTO org_unit_audit (
                       unit_id, operation, before_state, after_state, actor
                   ) VALUES (%s, %s, %s, %s, %s)""",
                (
                    unit_id,
                    "retired" if status == "retired" else "restored",
                    Jsonb({"status": existing["status"]}),
                    Jsonb({"status": status}),
                    actor,
                ),
            )
        return cast(dict[str, Any], row)

    def org_unit_references(self, unit_id: str) -> dict[str, int]:
        """What would keep pointing at this id after it stops being offered.

        Read before retiring so the screen can say what is behind the department rather than
        asking for a decision with no information. None of these are foreign keys -- every one
        of them stores the id as text -- so nothing breaks when the unit is retired; the rows
        simply go on naming something that is no longer on the menu.
        """
        with self._connection() as connection:
            budgets = connection.execute(
                """SELECT count(*) AS total FROM token_budget
                   WHERE scope_id = %s OR parent_scope_id = %s""",
                (unit_id, unit_id),
            ).fetchone()
            usage = connection.execute(
                """SELECT count(*) AS total FROM token_usage
                   WHERE department_id = %s OR organization_id = %s""",
                (unit_id, unit_id),
            ).fetchone()
            applications = connection.execute(
                "SELECT count(*) AS total FROM gateway_application WHERE department_id = %s",
                (unit_id,),
            ).fetchone()
        return {
            "budgets": int(budgets["total"]) if budgets else 0,
            "usage_records": int(usage["total"]) if usage else 0,
            "applications": int(applications["total"]) if applications else 0,
        }

    def list_org_unit_audit(self, unit_id: str, limit: int = 20) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM org_unit_audit
                   WHERE unit_id = %s
                   ORDER BY created_at DESC, id DESC
                   LIMIT %s""",
                (unit_id, limit),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)
