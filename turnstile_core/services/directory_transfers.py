"""Monthly transfers coordinated with the budget writer's directory/period locks."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from ..domain.directory import DirectoryError, DirectoryPrincipal
from ..persistence.directory_store import DirectoryConnection, DirectoryStore
from .directory_service import DirectoryService


class DirectoryTransferWorker:
    def __init__(self, store: DirectoryStore) -> None:
        self.store = store
        self.directory = DirectoryService(store)

    def run(self, period_start: date, *, now: datetime | None = None) -> dict[str, int]:
        moment = now or datetime.now(UTC)
        self.store.require_ready()
        result = {"completed": 0, "conflicted": 0}
        with self.store.connection() as connection, connection.transaction():
            state = connection.execute(
                "SELECT * FROM directory_control_state FOR UPDATE"
            ).fetchone()
            if not state or state["source"] != "database":
                raise DirectoryError("directory_not_ready", "Directory is not ready", 503)
            transfers = connection.execute(
                """SELECT * FROM directory_transfer WHERE status='scheduled'
                   AND effective_month<=%s ORDER BY person_id,id FOR UPDATE""",
                (period_start,),
            ).fetchall()
            if transfers and state["person_admission_mode"] == "unverified":
                raise DirectoryError(
                    "transfer_admission_unverified",
                    "Employee admission fencing is not verified",
                    503,
                )
            for transfer in transfers:
                try:
                    with connection.transaction():
                        self._activate(connection, dict(transfer), period_start, moment)
                except DirectoryError as error:
                    connection.execute(
                        """UPDATE directory_transfer SET status='conflicted',
                           conflict_code=%s,completed_at=now() WHERE id=%s""",
                        (error.code, transfer["id"]),
                    )
                    result["conflicted"] += 1
                else:
                    result["completed"] += 1
            if any(result.values()):
                version = state["active_version"] + 1
                connection.execute(
                    """UPDATE directory_control_state SET active_version=%s,
                       permission_revision=permission_revision+1,updated_at=now()""",
                    (version,),
                )
                connection.execute(
                    "INSERT INTO directory_outbox(entity_id,directory_version) VALUES (%s,%s)",
                    (f"monthly-transfers:{period_start}", version),
                )
        return result

    def _activate(
        self,
        connection: DirectoryConnection,
        transfer: dict[str, Any],
        period_start: date,
        now: datetime,
    ) -> None:
        if transfer["effective_month"] != period_start or now.date().replace(day=1) != period_start:
            raise DirectoryError("activation_window_missed", "Transfer month is no longer current")
        person = self.directory._person(connection, transfer["person_id"])
        if (
            person["revision"] != transfer["expected_person_revision"]
            or person["department_id"] != transfer["source_department_id"]
            or person["status"] != "active"
            or person["manual_disabled"]
            or person["source_disabled"]
        ):
            raise DirectoryError(
                "transfer_person_changed", "Person changed before transfer activation"
            )
        target = self.directory._entity(connection, "unit", transfer["target_department_id"])
        self.directory._active(target)
        self.directory._employee_number(
            connection,
            target["organization_id"],
            person["employee_number"],
            person["id"],
        )
        self.directory._active(
            self.directory._entity(
                connection,
                "organization",
                target["organization_id"],
            )
        )
        effective_at = datetime.combine(period_start, datetime.min.time(), tzinfo=UTC)
        unfinished = connection.execute(
            """SELECT 1 FROM directory_invocation_admission a
               LEFT JOIN directory_invocation_terminal t ON t.request_id=a.request_id
               WHERE a.governance_user_id=%s AND (t.completed_at IS NULL OR t.completed_at>=%s)
               LIMIT 1""",
            (person["governance_user_id"], effective_at),
        ).fetchone()
        if unfinished:
            raise DirectoryError(
                "transfer_invocation_pending", "A prior invocation crossed the transfer boundary"
            )
        evidence = connection.execute(
            """SELECT EXISTS(SELECT 1 FROM token_usage
                 WHERE user_id=%s AND ts>=%s AND usage_domain='apim')
               OR EXISTS(SELECT 1 FROM budget_reservation_admission
                 WHERE scope_type='person' AND scope_id=%s AND created_at>=%s)
               OR EXISTS(SELECT 1 FROM budget_usage_evidence
                 WHERE scope_type='person' AND scope_id=%s AND admitted_at>=%s)
               OR EXISTS(SELECT 1 FROM directory_invocation_admission
                 WHERE governance_user_id=%s AND admitted_at>=%s) AS present""",
            (person["governance_user_id"], effective_at) * 4,
        ).fetchone()
        if evidence and evidence["present"]:
            raise DirectoryError(
                "transfer_usage_started", "Current-period admission already started"
            )
        if person["app_user_id"]:
            late_grants = connection.execute(
                """SELECT 1 FROM directory_department_grant WHERE app_user_id=%s
                   AND valid_to IS NULL AND valid_from>%s LIMIT 1""",
                (person["app_user_id"], effective_at),
            ).fetchone()
            if late_grants:
                raise DirectoryError(
                    "transfer_grant_changed", "Department grants changed after month boundary"
                )
        budgets = connection.execute(
            """SELECT * FROM token_budget WHERE scope_type='user' AND scope_id=%s
               AND period_start>=%s ORDER BY period_start FOR UPDATE""",
            (person["governance_user_id"], period_start),
        ).fetchall()
        approved = {row["period_start"]: row for row in transfer["approved_future_budgets"]}
        for budget in budgets:
            period = str(budget["period_start"])
            approval = approved.get(period)
            if approval is None and budget["period_start"] != period_start:
                raise DirectoryError(
                    "transfer_future_budget_unapproved",
                    "A future budget was added after transfer approval",
                )
            if approval and (
                budget["token_limit"] != approval["token_limit"]
                or budget["parent_scope_id"] != approval["parent_scope_id"]
            ):
                raise DirectoryError(
                    "transfer_future_budget_changed", "An approved future budget changed"
                )
        for budget in budgets:
            period = budget["period_start"]
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"budget-roll-forward:{period}",),
            )
            for scope_type, scope_id in sorted(
                [
                    ("user", person["governance_user_id"]),
                    ("department", target["id"]),
                    ("department", transfer["source_department_id"]),
                ]
            ):
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"{period}:{scope_type}:{scope_id}",),
                )
            parent = connection.execute(
                """SELECT * FROM token_budget WHERE period_start=%s
                   AND scope_type='department' AND scope_id=%s FOR UPDATE""",
                (period, target["id"]),
            ).fetchone()
            allocated = connection.execute(
                """SELECT COALESCE(SUM(token_limit),0) AS total FROM token_budget
                   WHERE period_start=%s AND scope_type='user' AND parent_scope_id=%s
                   AND scope_id<>%s""",
                (period, target["id"], person["governance_user_id"]),
            ).fetchone()
            if (
                parent is None
                or parent["parent_scope_id"] != target["organization_id"]
                or allocated is None
                or allocated["total"] + budget["token_limit"] > parent["token_limit"]
            ):
                raise DirectoryError(
                    "transfer_budget_capacity", "Target budget has insufficient capacity"
                )
            if budget["parent_scope_id"] != transfer["source_department_id"]:
                raise DirectoryError(
                    "transfer_budget_parent", "Existing period attribution conflicts"
                )
        current = connection.execute(
            """SELECT * FROM directory_membership WHERE person_id=%s
               AND membership_kind='primary_department' AND valid_to IS NULL FOR UPDATE""",
            (person["id"],),
        ).fetchone()
        if current is None or current["valid_from"] >= effective_at:
            raise DirectoryError(
                "transfer_interval_conflict", "Primary membership interval conflicts"
            )
        changed_teams = connection.execute(
            """SELECT 1 FROM directory_membership WHERE person_id=%s AND valid_to IS NULL
               AND valid_from>=%s LIMIT 1""",
            (person["id"], effective_at),
        ).fetchone()
        if changed_teams:
            raise DirectoryError(
                "transfer_interval_conflict",
                "Current-month membership already changed",
            )
        connection.execute(
            """UPDATE directory_membership SET valid_to=%s,revision=revision+1
               WHERE person_id=%s AND valid_to IS NULL""",
            (effective_at, person["id"]),
        )
        connection.execute(
            """INSERT INTO directory_membership(
                 person_id,unit_id,organization_id,membership_kind,valid_from)
               VALUES (%s,%s,%s,'primary_department',%s)""",
            (person["id"], target["id"], target["organization_id"], effective_at),
        )
        for budget in budgets:
            connection.execute(
                """UPDATE token_budget SET parent_scope_id=%s,updated_by=%s,updated_at=now()
                   WHERE period_start=%s AND scope_type='user' AND scope_id=%s""",
                (
                    target["id"],
                    "directory-transfer-worker",
                    budget["period_start"],
                    person["governance_user_id"],
                ),
            )
            connection.execute(
                """INSERT INTO token_budget_audit(
                     id,period_start,scope_type,scope_id,action,previous_token_limit,new_token_limit,
                     previous_warning_threshold_percent,new_warning_threshold_percent,changed_by)
                   VALUES (gen_random_uuid(),%s,'user',%s,'updated',%s,%s,%s,%s,%s)""",
                (
                    budget["period_start"],
                    person["governance_user_id"],
                    budget["token_limit"],
                    budget["token_limit"],
                    budget["warning_threshold_percent"],
                    budget["warning_threshold_percent"],
                    "directory-transfer-worker",
                ),
            )
        if person["app_user_id"]:
            connection.execute(
                """UPDATE directory_department_grant SET valid_to=%s
                   WHERE app_user_id=%s AND valid_to IS NULL""",
                (effective_at, person["app_user_id"]),
            )
        self.directory._update(
            connection,
            "directory_person",
            UUID(str(person["id"])),
            {"updated_by": "directory-transfer-worker"},
        )
        connection.execute(
            """UPDATE directory_transfer SET status='completed',completed_at=now() WHERE id=%s""",
            (transfer["id"],),
        )
        self.directory._audit(
            connection,
            DirectoryPrincipal(email="directory-transfer-worker", role="system"),
            "transfer",
            str(transfer["id"]),
            "completed",
            before={
                "department_id": transfer["source_department_id"],
                "budgets": [dict(budget) for budget in budgets],
            },
            after={"department_id": target["id"], "effective_month": str(period_start)},
            organization_id=target["organization_id"],
            department_id=target["id"],
            reason=transfer["reason"],
            source="monthly_transfer",
        )
