"""Persistent directory data, independent of web sessions and usage repositories."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, cast
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..domain.data_access import DataAccessScope
from ..domain.directory import (
    DIRECTORY_PROTOCOL_VERSION,
    DirectoryError,
    DirectoryPrincipal,
    MenuAdministratorScope,
)
from ..domain.menu_permissions import MenuPermissionGroup
from ..domain.models import EnterpriseEntity, EnterpriseEntityCatalog

DirectoryConnection = Connection[dict[str, Any]]


class DirectoryStore:
    def __init__(self, database_url: str) -> None:
        self.pool = ConnectionPool(
            database_url,
            min_size=0,
            max_size=2,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        self._opened = False
        self._lock = threading.Lock()

    @contextmanager
    def connection(self) -> Iterator[DirectoryConnection]:
        with self._lock:
            if not self._opened:
                self.pool.open()
                self._opened = True
        with self.pool.connection() as connection:
            yield cast(DirectoryConnection, connection)

    def close(self) -> None:
        with self._lock:
            if self._opened:
                self.pool.close()
                self._opened = False

    def state_if_present(self) -> dict[str, Any] | None:
        with self.connection() as connection:
            present = connection.execute(
                "SELECT to_regclass('directory_control_state') AS present"
            ).fetchone()
            if present is None or present["present"] is None:
                return None
            state = connection.execute("SELECT * FROM directory_control_state").fetchone()
        if state is None or state["protocol_version"] != DIRECTORY_PROTOCOL_VERSION:
            raise DirectoryError(
                "directory_version_mismatch",
                "Directory protocol version is incompatible",
                503,
            )
        return dict(state)

    def state(self) -> dict[str, Any]:
        state = self.state_if_present()
        if state is None:
            raise DirectoryError(
                "directory_schema_missing",
                "Apply the organization directory migration",
                503,
            )
        return state

    def require_ready(self) -> dict[str, Any]:
        state = self.state()
        if state["source"] != "database" or state["phase"] not in {"active", "fresh_empty_ready"}:
            raise DirectoryError(
                "directory_not_ready",
                "Directory upgrade has not been activated",
                503,
            )
        return state

    def initialize_empty(self, *, person_admission_mode: str = "unverified") -> None:
        if person_admission_mode not in {"unverified", "bff_only", "identity_v1"}:
            raise ValueError("Unknown person admission mode")
        with self.connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('directory-initialization', 0))"
            )
            state = connection.execute(
                "SELECT * FROM directory_control_state FOR UPDATE"
            ).fetchone()
            if state is None:
                raise DirectoryError(
                    "directory_schema_missing", "Directory migration is missing", 503
                )
            if state["phase"] in {"active", "fresh_empty_ready"}:
                return
            if state["phase"] != "schema_only":
                raise DirectoryError(
                    "upgrade_in_progress", "An existing directory upgrade is pending"
                )
            occupied = connection.execute(
                """SELECT EXISTS(SELECT 1 FROM token_usage)
                   OR EXISTS(SELECT 1 FROM token_budget)
                   OR EXISTS(SELECT 1 FROM user_model_policy)
                   OR EXISTS(SELECT 1 FROM user_model_access)
                   OR EXISTS(SELECT 1 FROM gateway_application)
                   OR EXISTS(SELECT 1 FROM directory_organization)
                   OR EXISTS(SELECT 1 FROM directory_upgrade_run) AS occupied"""
            ).fetchone()
            assert occupied is not None
            if occupied["occupied"]:
                raise DirectoryError(
                    "existing_installation",
                    "Existing data requires an approved directory upgrade",
                )
            connection.execute(
                """UPDATE directory_control_state SET source = 'database',
                   phase = 'fresh_empty_ready', active_version = 1,
                   person_admission_mode=%s,updated_at = now()""",
                (person_admission_mode,),
            )

    def principal(
        self,
        account_id: UUID,
        email: str,
        role: str,
    ) -> DirectoryPrincipal:
        state = self.require_ready()
        with self.connection() as connection:
            policy_exists = connection.execute(
                "SELECT to_regclass('permission_policy') AS relation"
            ).fetchone()
            policy_row = connection.execute(
                "SELECT groups FROM permission_policy WHERE singleton"
            ).fetchone() if policy_exists and policy_exists["relation"] else None
            policy = dict(policy_row["groups"]) if policy_row else None
            rows = self.people_rows(connection, account_id=account_id)
            person = next(
                (p for p in rows if p["status"] == "active" and not p["manual_disabled"]
                 and not p["source_disabled"] and p["account_enabled"]),
                None,
            )
            if role == "owner":
                return DirectoryPrincipal(
                    account_id=account_id, email=email, role="owner",
                    permission_revision=state["permission_revision"],
                    menu_permission_groups=tuple(cast(
                        list[MenuPermissionGroup], person["menu_permission_groups"]
                    )) if person else ("user",),
                    menu_permission_group=cast(
                        MenuPermissionGroup, person["menu_permission_group"] if person else "user"
                    ),
                    menu_administrator_scopes=(
                        tuple(MenuAdministratorScope.model_validate(item)
                              for item in person["menu_administrator_scopes"]) if person else ()
                    ),
                    permission_policy=policy,
                )
            subject = connection.execute(
                "SELECT 1 FROM directory_department_grant WHERE app_user_id = %s LIMIT 1",
                (account_id,),
            ).fetchone()
            grants = connection.execute(
                """SELECT g.department_id, g.capabilities
                   FROM directory_department_grant g
                   JOIN app_user a ON a.id = g.app_user_id AND a.enabled
                   JOIN directory_account_link l ON l.app_user_id = a.id
                   JOIN directory_person p ON p.id = l.person_id
                     AND p.status = 'active' AND NOT p.manual_disabled
                   JOIN directory_membership m ON m.person_id = p.id
                     AND m.unit_id = g.department_id AND m.membership_kind = 'primary_department'
                     AND m.valid_from <= now() AND (m.valid_to IS NULL OR m.valid_to > now())
                   JOIN directory_unit u ON u.id = g.department_id AND u.status = 'active'
                   JOIN directory_organization o ON o.id = u.organization_id AND o.status = 'active'
                   WHERE g.app_user_id = %s AND g.valid_from <= now()
                     AND (g.valid_to IS NULL OR g.valid_to > now())
                     AND NOT EXISTS(SELECT 1 FROM directory_external_binding e
                       WHERE e.person_id=p.id AND e.source_disabled)""",
                (account_id,),
            ).fetchall()
        principal = DirectoryPrincipal(
            account_id=account_id,
            email=email,
            role="member",
            department_ids=tuple(sorted({str(row["department_id"]) for row in grants}))
            if subject
            else None,
            capabilities=tuple(sorted({item for row in grants for item in row["capabilities"]})),
            department_capabilities={
                department_id: tuple(sorted({
                    capability for row in grants if row["department_id"] == department_id
                    for capability in row["capabilities"]
                }))
                for department_id in {row["department_id"] for row in grants}
            },
            permission_revision=state["permission_revision"],
            menu_permission_group=cast(
                MenuPermissionGroup, person["menu_permission_group"] if person else "user"
            ),
            menu_permission_groups=tuple(
                cast(list[MenuPermissionGroup], person["menu_permission_groups"])
                if person
                else ["user"]
            ),
            menu_administrator_scopes=(
                tuple(MenuAdministratorScope.model_validate(item)
                      for item in person["menu_administrator_scopes"]) if person else ()
            ),
            permission_policy=policy,
        )
        if policy is None:
            raise DirectoryError(
                "permission_policy_missing", "Apply permission migration before Member access", 503,
            )
        scope = self.data_access_scope(principal, person)
        return principal.model_copy(update={
            "data_scope": scope,
            "department_ids": scope.visible_department_ids,
            "capabilities": tuple(sorted(set(principal.capabilities) | {"directory.read"})),
            "department_capabilities": {
                identity: tuple(sorted(
                    set((principal.department_capabilities or {}).get(identity, ()))
                    | {"directory.read"}
                ))
                for identity in scope.visible_department_ids
            },
        })

    def data_access_scope(
        self, principal: DirectoryPrincipal, person: dict[str, Any] | None,
    ) -> DataAccessScope:
        if person is None:
            return DataAccessScope(user_ids=(principal.email,))
        with self.connection() as connection:
            memberships = connection.execute(
                """SELECT DISTINCT u.id,u.kind,u.organization_id,u.parent_unit_id
                   FROM directory_membership m JOIN directory_unit u ON u.id=m.unit_id
                   JOIN directory_organization o ON o.id=u.organization_id
                   WHERE m.person_id=%s AND m.valid_from<=now()
                     AND (m.valid_to IS NULL OR m.valid_to>now())
                     AND u.status='active' AND o.status='active'
                     AND (u.kind='department' OR EXISTS(
                       SELECT 1 FROM directory_unit parent
                       WHERE parent.id=u.parent_unit_id AND parent.status='active'))""",
                (person["id"],),
            ).fetchall()
            scopes = principal.menu_administrator_scopes
            orgs = {item.organization_id for item in scopes if item.scope_kind == "organization"}
            departments = {item.scope_id for item in scopes if item.scope_kind == "department"}
            teams = {item.scope_id for item in scopes if item.scope_kind == "team"}
            if departments:
                departments.update(row["id"] for row in memberships if row["kind"] == "department")
            if teams:
                teams.update(row["id"] for row in memberships if row["kind"] == "team")
            if orgs:
                departments.update(row["id"] for row in connection.execute(
                    "SELECT id FROM directory_unit "
                    "WHERE kind='department' AND organization_id=ANY(%s)",
                    (sorted(orgs),),
                ).fetchall())
            visible = set(departments)
            visible.update(row["parent_unit_id"] for row in memberships if row["id"] in teams)
            users = {person["governance_user_id"]}
            if departments or teams:
                users.update(row["governance_user_id"] for row in connection.execute(
                    """SELECT DISTINCT p.governance_user_id FROM directory_person p
                       JOIN directory_membership m ON m.person_id=p.id
                       WHERE m.unit_id=ANY(%s) AND m.valid_from<=now()
                         AND (m.valid_to IS NULL OR m.valid_to>now())""",
                    (sorted(departments | teams),),
                ).fetchall())
            if orgs:
                users.update(row["governance_user_id"] for row in connection.execute(
                    "SELECT governance_user_id FROM directory_person WHERE organization_id=ANY(%s)",
                    (sorted(orgs),),
                ).fetchall())
            if users:
                visible.update(row["unit_id"] for row in connection.execute(
                    """SELECT DISTINCT m.unit_id FROM directory_membership m
                       JOIN directory_person p ON p.id=m.person_id
                       WHERE p.governance_user_id=ANY(%s)
                         AND m.membership_kind IN ('primary_department','department')
                         AND m.valid_from<=now() AND (m.valid_to IS NULL OR m.valid_to>now())""",
                    (sorted(users),),
                ).fetchall())
            if not scopes:
                visible.update(row["id"] if row["kind"] == "department" else row["parent_unit_id"]
                               for row in memberships)
            orgs.update(row["organization_id"] for row in memberships)
        return DataAccessScope(
            department_ids=tuple(sorted(departments)), user_ids=tuple(sorted(users)),
            visible_department_ids=tuple(sorted(value for value in visible if value)),
            organization_ids=tuple(sorted(orgs)), team_ids=tuple(sorted(teams)),
        )

    def linked_person(self, account_id: UUID) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT p.*,EXISTS(SELECT 1 FROM directory_external_binding e
                     WHERE e.person_id=p.id AND e.source_disabled) AS source_disabled
                   FROM directory_person p
                   JOIN directory_account_link l ON l.person_id = p.id
                   WHERE l.app_user_id = %s""",
                (account_id,),
            ).fetchone()
        return dict(row) if row else None

    def admit_invocation(
        self,
        *,
        request_id: str,
        governance_user_id: str,
        organization_id: str,
        department_id: str,
        admitted_at: datetime | None = None,
    ) -> None:
        moment = admitted_at or datetime.now(UTC)
        if moment.tzinfo is None:
            raise ValueError("Admission time must be timezone-aware")
        period = moment.astimezone(UTC).date().replace(day=1)
        with self.connection() as connection, connection.transaction():
            state = connection.execute(
                "SELECT * FROM directory_control_state FOR UPDATE",
            ).fetchone()
            if not state or state["source"] != "database":
                raise DirectoryError("directory_not_ready", "Directory authority is not ready", 503)
            prior = connection.execute(
                "SELECT * FROM directory_invocation_admission WHERE request_id=%s",
                (request_id,),
            ).fetchone()
            if prior and (
                prior["governance_user_id"] != governance_user_id
                or prior["organization_id"] != organization_id
                or prior["department_id"] != department_id
                or prior["period_start"] != period
            ):
                raise DirectoryError(
                    "admission_conflict",
                    "Invocation attribution is already sealed",
                )
            person = connection.execute(
                "SELECT * FROM directory_person WHERE governance_user_id=%s FOR UPDATE",
                (governance_user_id,),
            ).fetchone()
            if not person:
                raise DirectoryError(
                    "person_not_bound", "An active directory identity is required", 409
                )
            current = connection.execute(
                """SELECT m.unit_id,m.organization_id FROM directory_membership m
                   JOIN directory_unit u ON u.id=m.unit_id AND u.status='active'
                   JOIN directory_organization o ON o.id=m.organization_id AND o.status='active'
                   WHERE m.person_id=%s AND m.membership_kind='primary_department'
                     AND m.valid_from<=%s AND (m.valid_to IS NULL OR m.valid_to>%s)""",
                (person["id"], moment, moment),
            ).fetchone()
            disabled = connection.execute(
                """SELECT EXISTS(SELECT 1 FROM directory_external_binding e
                     WHERE e.person_id=%s AND e.source_disabled)
                   OR EXISTS(SELECT 1 FROM directory_account_link l
                     JOIN app_user a ON a.id=l.app_user_id
                     WHERE l.person_id=%s AND NOT a.enabled) AS disabled""",
                (person["id"], person["id"]),
            ).fetchone()
            pending = connection.execute(
                """SELECT 1 FROM directory_transfer WHERE person_id=%s AND status='scheduled'
                   AND effective_month<=%s LIMIT 1""",
                (person["id"], period),
            ).fetchone()
            if pending:
                raise DirectoryError(
                    "transfer_activation_pending", "Month-boundary transfer is not activated", 409
                )
            if (
                person["status"] != "active"
                or person["manual_disabled"]
                or (disabled and disabled["disabled"])
                or current is None
                or current["unit_id"] != department_id
                or current["organization_id"] != organization_id
            ):
                raise DirectoryError(
                    "invocation_identity_changed", "Invocation directory identity changed", 409
                )
            if prior:
                return
            connection.execute(
                """INSERT INTO directory_invocation_admission(
                     request_id,governance_user_id,organization_id,department_id,
                     directory_version,admitted_at,period_start) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    request_id,
                    governance_user_id,
                    organization_id,
                    department_id,
                    state["active_version"],
                    moment,
                    period,
                ),
            )

    def finish_invocation(self, request_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO directory_invocation_terminal(request_id,completed_at)
                   VALUES (%s,clock_timestamp()) ON CONFLICT DO NOTHING""",
                (request_id,),
            )

    def people_rows(
        self,
        connection: DirectoryConnection,
        *,
        at_time: datetime | None = None,
        person_id: UUID | None = None,
        account_id: UUID | None = None,
        account_ids: tuple[UUID, ...] | None = None,
        department_ids: tuple[str, ...] | None = None,
        allowed_user_ids: tuple[str, ...] | None = None,
        department_id: str | None = None,
        team_id: str | None = None,
        organization_id: str | None = None,
        query: str = "",
        status: str | None = None,
        available_unit_id: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        moment = at_time or datetime.now(UTC)
        clauses: list[str] = []
        parameters: list[Any] = [moment, moment, moment, moment]
        for clause, value in (
            ("p.id=%s", person_id),
            ("l.app_user_id=%s", account_id),
            ("COALESCE(u.organization_id,p.organization_id)=%s", organization_id),
        ):
            if value is not None:
                clauses.append(clause)
                parameters.append(value)
        if status == "archived":
            clauses.append("p.status IN ('inactive','archived')")
        elif status is not None:
            clauses.append("p.status=%s")
            parameters.append(status)
        if department_ids is not None:
            clauses.append("membership.unit_id=ANY(%s)")
            parameters.append(list(department_ids))
        if allowed_user_ids is not None:
            clauses.append("p.governance_user_id=ANY(%s)")
            parameters.append(list(allowed_user_ids))
        if department_id is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM directory_membership department_member "
                "WHERE department_member.person_id=p.id AND department_member.unit_id=%s "
                "AND department_member.membership_kind IN ('primary_department','department') "
                "AND department_member.valid_from<=%s "
                "AND (department_member.valid_to IS NULL OR department_member.valid_to>%s))"
            )
            parameters.extend([department_id, moment, moment])
        if account_ids is not None:
            clauses.append("l.app_user_id=ANY(%s)")
            parameters.append(list(account_ids))
        if available_unit_id is not None:
            clauses.extend([
                "NOT p.manual_disabled",
                "NOT EXISTS(SELECT 1 FROM directory_external_binding e "
                "WHERE e.person_id=p.id AND e.source_disabled)",
            ])
            clauses.append(
                "NOT EXISTS(SELECT 1 FROM directory_membership available "
                "WHERE available.person_id=p.id AND available.unit_id=%s "
                "AND available.valid_from<=%s "
                "AND (available.valid_to IS NULL OR available.valid_to>%s))"
            )
            parameters.extend([available_unit_id, moment, moment])
        if team_id is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM directory_membership team_member "
                "WHERE team_member.person_id=p.id AND team_member.unit_id=%s "
                "AND team_member.membership_kind='team' AND team_member.valid_from<=%s "
                "AND (team_member.valid_to IS NULL OR team_member.valid_to>%s))"
            )
            parameters.extend([team_id, moment, moment])
        if query:
            clauses.append(
                "concat_ws(' ',p.display_name,p.governance_user_id,p.contact_email,"
                "p.employee_number) ILIKE %s"
            )
            escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"%{escaped_query}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        paging = ""
        if limit is not None:
            paging = " LIMIT %s OFFSET %s"
            parameters.extend([limit, offset])
        return [
            dict(row)
            for row in connection.execute(
                f"""SELECT p.*, count(*) OVER() AS _total_count,
                          membership.unit_id AS department_id,
                          u.name AS department_name,
                          COALESCE(u.organization_id,p.organization_id) AS organization_id,
                          l.app_user_id, a.email AS account_email, a.enabled AS account_enabled,
                          COALESCE(teams.ids, ARRAY[]::TEXT[]) AS team_ids,
                          COALESCE(teams.manual_ids, ARRAY[]::TEXT[]) AS manual_team_ids,
                          COALESCE(teams.source_ids, ARRAY[]::TEXT[]) AS source_team_ids,
                          EXISTS(SELECT 1 FROM directory_external_binding external
                            WHERE external.person_id = p.id) AS externally_managed,
                          EXISTS(SELECT 1 FROM directory_external_binding external
                            WHERE external.person_id = p.id AND external.source_disabled)
                            AS source_disabled,
                          p.menu_permission_groups AS assigned_menu_permission_groups,
                          COALESCE(menu_groups.ids, ARRAY['user']::text[])
                            AS menu_permission_groups,
                          COALESCE(menu_groups.ids[1], 'user') AS menu_permission_group,
                          COALESCE(menu_scopes.items, '[]'::jsonb) AS menu_administrator_scopes
                   FROM directory_person p
                   LEFT JOIN LATERAL (
                     SELECT m.unit_id FROM directory_membership m
                     WHERE m.person_id = p.id AND m.membership_kind = 'primary_department'
                       AND m.valid_from <= %s AND (m.valid_to IS NULL OR m.valid_to > %s)
                   ) membership ON TRUE
                   LEFT JOIN directory_unit u ON u.id = membership.unit_id
                   LEFT JOIN directory_account_link l ON l.person_id = p.id
                   LEFT JOIN app_user a ON a.id = l.app_user_id
                   LEFT JOIN LATERAL (
                     SELECT array_agg(DISTINCT menu_group ORDER BY menu_group) AS ids
                     FROM (
                       SELECT unnest(p.menu_permission_groups) AS menu_group
                       UNION ALL
                       SELECT g.menu_group FROM directory_effective_menu_administrator g
                       WHERE g.app_user_id=l.app_user_id
                     ) appointments WHERE menu_group <> 'user'
                   ) menu_groups ON TRUE
                   LEFT JOIN LATERAL (
                     SELECT jsonb_agg(jsonb_build_object(
                       'scope_kind', g.scope_kind,
                       'scope_id', COALESCE(g.unit_id,g.organization_id),
                       'organization_id', g.organization_id,
                       'scope_name', COALESCE(target.name,org.name)
                     ) ORDER BY g.scope_kind,COALESCE(g.unit_id,g.organization_id)) AS items
                     FROM directory_effective_menu_administrator g
                     JOIN directory_organization org ON org.id=g.organization_id
                     LEFT JOIN directory_unit target ON target.id=g.unit_id
                     WHERE g.app_user_id=l.app_user_id
                   ) menu_scopes ON TRUE
                   LEFT JOIN LATERAL (
                     SELECT array_agg(DISTINCT m.unit_id ORDER BY m.unit_id) AS ids,
                       array_agg(DISTINCT m.unit_id ORDER BY m.unit_id)
                         FILTER(WHERE m.source_key='manual') AS manual_ids,
                       array_agg(DISTINCT m.unit_id ORDER BY m.unit_id)
                         FILTER(WHERE m.source_key<>'manual') AS source_ids
                     FROM directory_membership m
                     WHERE m.person_id = p.id AND m.membership_kind = 'team'
                       AND m.valid_from <= %s AND (m.valid_to IS NULL OR m.valid_to > %s)
                   ) teams ON TRUE{where}
                   ORDER BY lower(p.display_name), p.id{paging}""",
                parameters,
            ).fetchall()
        ]

    def catalog(
        self,
        *,
        at_time: datetime | None = None,
        include_inactive: bool = False,
        department_ids: tuple[str, ...] | None = None,
    ) -> EnterpriseEntityCatalog:
        self.require_ready()
        with self.connection() as connection:
            scope = list(department_ids) if department_ids is not None else None
            organizations = connection.execute(
                """SELECT o.* FROM directory_organization o
                   WHERE %s::text[] IS NULL OR EXISTS(SELECT 1 FROM directory_unit u
                     WHERE u.organization_id=o.id AND u.id=ANY(%s)) ORDER BY o.name,o.id""",
                (scope, scope),
            ).fetchall()
            units = connection.execute(
                """SELECT * FROM directory_unit WHERE %s::text[] IS NULL OR id=ANY(%s)
                   OR parent_unit_id=ANY(%s) ORDER BY name,id""",
                (scope, scope, scope),
            ).fetchall()
            people = self.people_rows(connection, at_time=at_time, department_ids=department_ids)
            projects = connection.execute(
                """SELECT * FROM directory_project_reference
                   WHERE %s::text[] IS NULL OR department_id=ANY(%s) ORDER BY id""",
                (scope, scope),
            ).fetchall()
            agents = connection.execute(
                """SELECT a.* FROM directory_agent_reference a
                   JOIN directory_project_reference p ON p.id=a.project_id
                   WHERE %s::text[] IS NULL OR p.department_id=ANY(%s) ORDER BY a.id""",
                (scope, scope),
            ).fetchall()
        organizations = [
            row for row in organizations if include_inactive or row["status"] == "active"
        ]
        organization_ids = {row["id"] for row in organizations}
        departments = [
            row
            for row in units
            if row["kind"] == "department"
            and row["organization_id"] in organization_ids
            and (include_inactive or row["status"] == "active")
        ]
        catalog_department_ids = {row["id"] for row in departments}
        projects = [row for row in projects if row["department_id"] in catalog_department_ids]
        project_ids = {row["id"] for row in projects}
        return EnterpriseEntityCatalog(
            organizations=[
                EnterpriseEntity(id=row["id"], name=row["name"]) for row in organizations
            ],
            departments=[
                EnterpriseEntity(id=row["id"], name=row["name"], parent_id=row["organization_id"])
                for row in departments
            ],
            users=[
                EnterpriseEntity(
                    id=row["governance_user_id"],
                    name=row["display_name"],
                    parent_id=row["department_id"],
                )
                for row in people
                if row["department_id"] in catalog_department_ids
                and (
                    include_inactive
                    or (
                        row["status"] == "active"
                        and not row["manual_disabled"]
                        and not row["source_disabled"]
                    )
                )
            ],
            projects=[
                EnterpriseEntity(id=row["id"], name=row["name"], parent_id=row["department_id"])
                for row in projects
            ],
            agents=[
                EnterpriseEntity(id=row["id"], name=row["name"], parent_id=row["project_id"])
                for row in agents
                if row["project_id"] in project_ids
            ],
        )


@lru_cache(maxsize=4)
def directory_store(database_url: str) -> DirectoryStore:
    return DirectoryStore(database_url)
