from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, time
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ..domain.ledger import (
    BudgetReservationAdmission,
    BudgetReservationFinalization,
    LedgerScopeType,
)
from .repository_support import BudgetConstraintViolation

_LEDGER_CORRELATION_BATCH_SIZE = 5000


class PostgreSqlBudgetRepositoryMixin:
    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def budget_evidence_effective_at(self) -> datetime | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT effective_at FROM budget_evidence_policy WHERE version = 2"
            ).fetchone()
        return cast(datetime | None, row["effective_at"])

    def register_budget_reservations(self, items: Sequence[BudgetReservationAdmission]) -> None:
        if not items:
            return
        values = Jsonb([item.model_dump(mode="json") for item in items])
        with self._connection() as connection, connection.transaction():
            connection.execute(
                """INSERT INTO budget_reservation_admission
                     (scope_type, scope_id, correlation_id, created_at, reserved_tokens)
                   SELECT * FROM jsonb_to_recordset(%s::jsonb) AS incoming(
                     scope_type TEXT, scope_id TEXT, correlation_id TEXT,
                     created_at TIMESTAMPTZ, reserved_tokens BIGINT)
                   ON CONFLICT DO NOTHING""",
                (values,),
            )
            mismatch = connection.execute(
                """SELECT 1 FROM jsonb_to_recordset(%s::jsonb) AS incoming(
                     scope_type TEXT, scope_id TEXT, correlation_id TEXT,
                     created_at TIMESTAMPTZ, reserved_tokens BIGINT)
                   JOIN budget_reservation_admission stored
                     USING (scope_type, scope_id, correlation_id)
                   WHERE (stored.created_at, stored.reserved_tokens)
                     IS DISTINCT FROM (incoming.created_at, incoming.reserved_tokens)
                   LIMIT 1""",
                (values,),
            ).fetchone()
            if mismatch is not None:
                raise ValueError("Budget reservation admission is immutable")

    def list_token_budgets(self, period_start: date) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT period_start, scope_type, scope_id, parent_scope_id,
                          token_limit, warning_threshold_percent, updated_at, updated_by
                   FROM token_budget
                   WHERE period_start = %s
                   ORDER BY scope_type, scope_id""",
                (period_start,),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def subscription_attributed_usage(
        self, from_: datetime, to: datetime
    ) -> Sequence[dict[str, Any]]:
        """Usage that declared no identity, attributed to the subscription that produced it.

        `token_usage.department_id` and `.user_id` come from request headers and default to
        `unattributed`, which `token_usage_by_budget_scope` discards in its final
        `WHERE scope_id <> 'unattributed'`. At an install where nothing sets those headers that
        is every row, so department and person budgets there report zero no matter how they are
        configured.

        The subscription is identified on every request regardless of what the caller says, and
        `token_usage_application_attribution` already records which one produced each row. So the
        discarded usage can be attributed here instead -- at read time, deliberately. Writing it
        into `token_usage` is not available: `guard_apim_usage_identity` makes `user_id`
        immutable per correlation, and a value derived from a field an administrator can edit
        would make a second event for the same request conflict with the first and be rejected
        for good. Resolving on read also means filing a subscription corrects what its past
        usage rolls up to, which is what an administrator filing three hundred keys expects.

        This only ever adds rows the other query threw away: every branch here requires the
        usage row's own attribution to be `unattributed`, which is exactly the condition under
        which that query drops it. So the two cannot double count.
        """
        with self._connection() as connection:
            rows = connection.execute(
                """WITH filed AS (
                       SELECT usage.id,
                              usage.department_id AS declared_department,
                              usage.user_id AS declared_user,
                              usage.input_tokens + usage.cached_tokens
                                  + usage.output_tokens AS total_tokens,
                              application.department_id,
                              application.owner_id
                       FROM token_usage usage
                       JOIN token_usage_application_attribution attribution
                         ON attribution.usage_id = usage.id
                       JOIN gateway_application application
                         ON application.id = attribution.application_id
                       WHERE usage.ts >= %s AND usage.ts < %s
                         AND usage.usage_domain = 'apim'
                         AND application.system_managed = FALSE
                   )
                   SELECT 'department'::TEXT AS scope_type,
                          department_id AS scope_id,
                          SUM(total_tokens)::BIGINT AS used_tokens
                   FROM filed
                   WHERE department_id IS NOT NULL
                     AND declared_department = 'unattributed'
                   GROUP BY department_id
                   UNION ALL
                   SELECT 'user'::TEXT, owner_id, SUM(total_tokens)::BIGINT
                   FROM filed
                   WHERE owner_id IS NOT NULL
                     AND declared_user = 'unattributed'
                   GROUP BY owner_id
                   UNION ALL
                   SELECT 'organization'::TEXT, unit.parent_id, SUM(filed.total_tokens)::BIGINT
                   FROM filed
                   JOIN org_unit unit ON unit.id = filed.department_id
                   WHERE filed.department_id IS NOT NULL
                     AND filed.declared_department = 'unattributed'
                     AND unit.parent_id IS NOT NULL
                   GROUP BY unit.parent_id""",
                (from_, to),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def token_usage_by_budget_scope(
        self, from_: datetime, to: datetime
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """WITH period_usage AS (
                       SELECT organization_id, department_id, user_id,
                              input_tokens + cached_tokens + output_tokens AS total_tokens
                       FROM token_usage usage
                       WHERE ts >= %s AND ts < %s AND usage_domain = 'apim'
                       AND EXISTS (
                       SELECT 1 FROM budget_legacy_usage legacy
                       WHERE legacy.scope_type = 'person'
                       AND legacy.scope_id = usage.user_id
                       AND legacy.correlation_id = usage.correlation_id
                       )
                       AND NOT EXISTS (
                       SELECT 1 FROM budget_reservation_recovery recovery
                       WHERE recovery.scope_type = 'person'
                       AND recovery.scope_id = usage.user_id
                       AND recovery.correlation_id = usage.correlation_id
                       )
                       UNION ALL
                       SELECT COALESCE(department.parent_scope_id, 'unattributed'),
                       COALESCE(person.parent_scope_id, 'unattributed'),
                       recovery.scope_id, recovery.total_tokens
                       FROM budget_reservation_recovery recovery
                       LEFT JOIN token_budget person
                       ON person.period_start = recovery.period_start
                       AND person.scope_type = 'user' AND person.scope_id = recovery.scope_id
                       LEFT JOIN token_budget department
                       ON department.period_start = recovery.period_start
                       AND department.scope_type = 'department'
                       AND department.scope_id = person.parent_scope_id
                       WHERE recovery.scope_type = 'person'
                       AND recovery.reservation_created_at >= %s
                       AND recovery.reservation_created_at < %s
                       AND EXISTS (
                       SELECT 1 FROM budget_legacy_usage legacy
                       WHERE legacy.scope_type = recovery.scope_type
                       AND legacy.scope_id = recovery.scope_id
                       AND legacy.correlation_id = recovery.correlation_id
                       )
                       UNION ALL
                       SELECT organization_id, department_id, scope_id, total_tokens
                       FROM budget_evidence_selected
                       WHERE scope_type = 'person' AND admitted_at >= %s AND admitted_at < %s
                       UNION ALL
                       SELECT 'unattributed', 'unattributed', scope_id, effective_tokens
                       FROM billable_request_effective_v1
                       WHERE scope_type = 'person' AND effective_state = 'exact'
                       AND NOT strict_budget_evidence(created_at)
                       AND period_start >= %s::date AND period_start < %s::date
                   ), scoped_usage AS (
                       SELECT 'organization'::TEXT AS scope_type,
                              organization_id AS scope_id,
                              SUM(total_tokens)::BIGINT AS used_tokens
                       FROM period_usage GROUP BY organization_id
                       UNION ALL
                       SELECT 'department'::TEXT, department_id,
                              SUM(total_tokens)::BIGINT
                       FROM period_usage GROUP BY department_id
                       UNION ALL
                       SELECT 'user'::TEXT, user_id, SUM(total_tokens)::BIGINT
                       FROM period_usage GROUP BY user_id
                   )
                   SELECT scope_type, scope_id, used_tokens
                   FROM scoped_usage
                   WHERE scope_id <> 'unattributed'""",
                (from_, to, from_, to, from_, to, from_, to),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def upsert_token_budget(
        self,
        period_start: date,
        scope_type: str,
        scope_id: str,
        parent_scope_id: str | None,
        token_limit: int,
        warning_threshold_percent: int,
        changed_by: str,
    ) -> None:
        with self._connection() as connection, connection.transaction():
            lock_key = f"{period_start}:{scope_type}:{scope_id}"
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
            parent_type = {
                "department": "organization",
                "user": "department",
            }.get(scope_type)
            if parent_type is not None and parent_scope_id is not None:
                parent_lock_key = f"{period_start}:{parent_type}:{parent_scope_id}"
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (parent_lock_key,),
                )
                parent = connection.execute(
                    """SELECT token_limit
                       FROM token_budget
                       WHERE period_start = %s AND scope_type = %s AND scope_id = %s
                       FOR UPDATE""",
                    (period_start, parent_type, parent_scope_id),
                ).fetchone()
                if parent is None:
                    raise BudgetConstraintViolation(
                        f"Assign the {parent_type} budget before allocating {scope_type} budgets"
                    )
                allocated = connection.execute(
                    """SELECT COALESCE(SUM(token_limit), 0)::BIGINT AS total
                       FROM token_budget
                       WHERE period_start = %s AND scope_type = %s
                         AND parent_scope_id = %s AND scope_id <> %s""",
                    (period_start, scope_type, parent_scope_id, scope_id),
                ).fetchone()
                if int(allocated["total"]) + token_limit > int(parent["token_limit"]):
                    raise BudgetConstraintViolation(
                        f"{scope_type.title()} allocations would exceed the "
                        f"{parent_type} budget of {int(parent['token_limit'])} tokens"
                    )

            child_type = {
                "organization": "department",
                "department": "user",
            }.get(scope_type)
            if child_type is not None:
                child_allocated = connection.execute(
                    """SELECT COALESCE(SUM(token_limit), 0)::BIGINT AS total
                       FROM token_budget
                       WHERE period_start = %s AND scope_type = %s
                         AND parent_scope_id = %s""",
                    (period_start, child_type, scope_id),
                ).fetchone()
                if int(child_allocated["total"]) > token_limit:
                    raise BudgetConstraintViolation(
                        f"The {scope_type} budget cannot be lower than its "
                        f"{int(child_allocated['total'])} allocated child tokens"
                    )

            previous = connection.execute(
                """SELECT parent_scope_id, token_limit, warning_threshold_percent
                   FROM token_budget
                   WHERE period_start = %s AND scope_type = %s AND scope_id = %s
                   FOR UPDATE""",
                (period_start, scope_type, scope_id),
            ).fetchone()
            if previous is not None and (
                previous["parent_scope_id"] == parent_scope_id
                and previous["token_limit"] == token_limit
                and previous["warning_threshold_percent"] == warning_threshold_percent
            ):
                return
            connection.execute(
                """INSERT INTO token_budget (
                       period_start, scope_type, scope_id, parent_scope_id,
                       token_limit, warning_threshold_percent, updated_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (period_start, scope_type, scope_id) DO UPDATE SET
                       parent_scope_id = EXCLUDED.parent_scope_id,
                       token_limit = EXCLUDED.token_limit,
                       warning_threshold_percent = EXCLUDED.warning_threshold_percent,
                       updated_at = now(),
                       updated_by = EXCLUDED.updated_by""",
                (
                    period_start,
                    scope_type,
                    scope_id,
                    parent_scope_id,
                    token_limit,
                    warning_threshold_percent,
                    changed_by,
                ),
            )
            connection.execute(
                """INSERT INTO token_budget_audit (
                       id, period_start, scope_type, scope_id, action,
                       previous_token_limit, new_token_limit,
                       previous_warning_threshold_percent,
                       new_warning_threshold_percent, changed_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    uuid4(),
                    period_start,
                    scope_type,
                    scope_id,
                    "assigned" if previous is None else "updated",
                    previous["token_limit"] if previous else None,
                    token_limit,
                    previous["warning_threshold_percent"] if previous else None,
                    warning_threshold_percent,
                    changed_by,
                ),
            )

    def delete_token_budget(
        self, period_start: date, scope_type: str, scope_id: str, changed_by: str
    ) -> bool:
        with self._connection() as connection, connection.transaction():
            lock_key = f"{period_start}:{scope_type}:{scope_id}"
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
            child_type = {
                "organization": "department",
                "department": "user",
            }.get(scope_type)
            if child_type is not None:
                has_children = connection.execute(
                    """SELECT EXISTS (
                           SELECT 1 FROM token_budget
                           WHERE period_start = %s AND scope_type = %s
                             AND parent_scope_id = %s
                       ) AS value""",
                    (period_start, child_type, scope_id),
                ).fetchone()
                if has_children["value"]:
                    raise BudgetConstraintViolation(
                        f"Remove allocated {child_type} budgets before removing "
                        f"this {scope_type} budget"
                    )
            previous = connection.execute(
                """DELETE FROM token_budget
                   WHERE period_start = %s AND scope_type = %s AND scope_id = %s
                   RETURNING token_limit, warning_threshold_percent""",
                (period_start, scope_type, scope_id),
            ).fetchone()
            if previous is None:
                return False
            connection.execute(
                """INSERT INTO token_budget_audit (
                       id, period_start, scope_type, scope_id, action,
                       previous_token_limit, new_token_limit,
                       previous_warning_threshold_percent,
                       new_warning_threshold_percent, changed_by
                   ) VALUES (%s, %s, %s, %s, 'removed', %s, NULL, %s, NULL, %s)""",
                (
                    uuid4(),
                    period_start,
                    scope_type,
                    scope_id,
                    previous["token_limit"],
                    previous["warning_threshold_percent"],
                    changed_by,
                ),
            )
        return True

    def roll_forward_budgets(self, period_start: date, changed_by: str) -> dict[str, Any] | None:
        """Give a period the previous period's budgets, at most once.

        Returns the outcome the first time it decides anything for this period, and None
        on every later call, so the caller can log a real event instead of a heartbeat.

        Three rules, each load-bearing:

        * **The marker is written even when nothing is copied.** It records that the
          period has had its one chance to inherit. Without that, a budget the
          administrator deliberately removed today would come back on the next timer tick
          a few minutes later.
        * **A period that already has any budget row is left alone entirely.** Copying
          into a half-configured month could push the sum of inherited children past a
          parent the administrator had just lowered, and the parent/child invariant is
          checked on write, not here. Someone editing the month is managing it.
        * **The copy skips validation deliberately.** It is not a new allocation, it is
          the previous period's set reproduced whole, and that set was already validated
          when it was written. Re-checking it row by row would fail on the first child
          copied before its parent.
        """
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"budget-roll-forward:{period_start}",),
            )
            already = connection.execute(
                "SELECT 1 FROM budget_roll_forward WHERE period_start = %s",
                (period_start,),
            ).fetchone()
            if already is not None:
                return None

            occupied = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM token_budget WHERE period_start = %s) AS value",
                (period_start,),
            ).fetchone()
            source = connection.execute(
                "SELECT MAX(period_start) AS value FROM token_budget WHERE period_start < %s",
                (period_start,),
            ).fetchone()
            source_period = None if occupied["value"] else source["value"]

            copied: list[dict[str, Any]] = []
            if source_period is not None:
                copied = connection.execute(
                    """INSERT INTO token_budget (
                           period_start, scope_type, scope_id, parent_scope_id,
                           token_limit, warning_threshold_percent, updated_by
                       )
                       SELECT %(target)s, scope_type, scope_id, parent_scope_id,
                              token_limit, warning_threshold_percent, %(changed_by)s
                       FROM token_budget
                       WHERE period_start = %(source)s
                       RETURNING scope_type, scope_id, token_limit,
                                 warning_threshold_percent""",
                    {
                        "target": period_start,
                        "source": source_period,
                        "changed_by": changed_by,
                    },
                ).fetchall()
                if copied:
                    # Audited like any other assignment, so the monthly timeline can say
                    # the allowance was inherited rather than leaving it unexplained.
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            """INSERT INTO token_budget_audit (
                                   id, period_start, scope_type, scope_id, action,
                                   previous_token_limit, new_token_limit,
                                   previous_warning_threshold_percent,
                                   new_warning_threshold_percent, changed_by
                               ) VALUES (%s, %s, %s, %s, 'assigned', NULL, %s, NULL, %s, %s)""",
                            [
                                (
                                    uuid4(),
                                    period_start,
                                    row["scope_type"],
                                    row["scope_id"],
                                    row["token_limit"],
                                    row["warning_threshold_percent"],
                                    changed_by,
                                )
                                for row in copied
                            ],
                        )

            connection.execute(
                """INSERT INTO budget_roll_forward (
                       period_start, source_period_start, scope_count
                   ) VALUES (%s, %s, %s)""",
                (period_start, source_period, len(copied)),
            )
        return {
            "period_start": period_start,
            "source_period_start": source_period,
            "scope_count": len(copied),
        }

    def list_token_budget_audit(self, period_start: date, limit: int) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id, period_start, scope_type, scope_id, action,
                          previous_token_limit, new_token_limit,
                          previous_warning_threshold_percent,
                          new_warning_threshold_percent, changed_at, changed_by
                   FROM token_budget_audit
                   WHERE period_start = %s
                   ORDER BY changed_at DESC, id DESC
                   LIMIT %s""",
                (period_start, limit),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def list_user_model_policies(self, user_ids: Sequence[str]) -> Sequence[dict[str, Any]]:
        if not user_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT policy.user_id, policy.updated_at, policy.updated_by,
                          COALESCE(
                              array_agg(access.model_id ORDER BY access.model_id)
                                  FILTER (WHERE access.model_id IS NOT NULL),
                              ARRAY[]::UUID[]
                          ) AS model_ids
                   FROM user_model_policy policy
                   LEFT JOIN user_model_access access ON access.user_id = policy.user_id
                   WHERE policy.user_id = ANY(%s)
                   GROUP BY policy.user_id, policy.updated_at, policy.updated_by""",
                (list(user_ids),),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def list_user_model_access_audit(
        self,
        user_ids: Sequence[str],
        from_: datetime,
        to: datetime,
        limit: int,
    ) -> Sequence[dict[str, Any]]:
        if not user_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id, user_id, previous_model_ids, new_model_ids,
                          changed_at, changed_by
                   FROM user_model_access_audit
                   WHERE user_id = ANY(%s)
                     AND changed_at >= %s AND changed_at < %s
                   ORDER BY changed_at DESC, id DESC
                   LIMIT %s""",
                (list(user_ids), from_, to, limit),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def list_department_enforcement(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT department_id, mode, updated_at, updated_by
                   FROM department_enforcement
                   ORDER BY department_id"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def set_department_enforcement(self, department_id: str, mode: str, changed_by: str) -> bool:
        """Return True when the mode actually changed, so no-op saves stay out of the audit."""
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"enforcement:{department_id}",),
            )
            current = connection.execute(
                """SELECT mode FROM department_enforcement
                   WHERE department_id = %s
                   FOR UPDATE""",
                (department_id,),
            ).fetchone()
            previous = None if current is None else str(current["mode"])
            if previous == mode:
                return False
            connection.execute(
                """INSERT INTO department_enforcement
                       (department_id, mode, updated_at, updated_by)
                   VALUES (%s, %s, now(), %s)
                   ON CONFLICT (department_id) DO UPDATE
                       SET mode = EXCLUDED.mode,
                           updated_at = EXCLUDED.updated_at,
                           updated_by = EXCLUDED.updated_by""",
                (department_id, mode, changed_by),
            )
            connection.execute(
                """INSERT INTO department_enforcement_audit
                       (id, department_id, previous_mode, new_mode, changed_by)
                   VALUES (%s, %s, %s, %s, %s)""",
                (uuid4(), department_id, previous, mode, changed_by),
            )
        return True

    def list_department_enforcement_audit(
        self, from_: datetime, to: datetime, limit: int
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id, department_id, previous_mode, new_mode,
                          changed_at, changed_by
                   FROM department_enforcement_audit
                   WHERE changed_at >= %s AND changed_at < %s
                   ORDER BY changed_at DESC, id DESC
                   LIMIT %s""",
                (from_, to, limit),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def budget_ledger_people(self, period_start: date) -> Sequence[dict[str, Any]]:
        """Budgeted people whose Table Storage partitions must be synchronized."""
        with self._connection() as connection:
            rows = connection.execute(
                """WITH active AS (
                  SELECT scope_id AS user_id,
                      parent_scope_id AS department_id,
                      token_limit
                  FROM token_budget
                  WHERE period_start = %(period_start)s AND scope_type = 'user'
                 ),
                 retired AS (
                  SELECT DISTINCT audit.scope_id AS user_id
                  FROM token_budget_audit audit
                  WHERE audit.period_start = %(period_start)s
                    AND audit.scope_type = 'user'
                    AND audit.action = 'removed'
                    AND NOT EXISTS (
                      SELECT 1 FROM active WHERE active.user_id = audit.scope_id
                    )
                 )
                 SELECT active.user_id,
                     active.department_id,
                     active.token_limit,
                     COALESCE(enforcement.mode, 'audit') AS mode
                 FROM active
                   LEFT JOIN department_enforcement enforcement
                     ON enforcement.department_id = active.department_id
                 UNION ALL
                 SELECT retired.user_id, '' AS department_id, 0 AS token_limit, 'audit' AS mode
                 FROM retired
                 ORDER BY user_id""",
                {"period_start": period_start},
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def budget_ledger_snapshot(
        self, period_start: date, period_end: date
    ) -> Sequence[dict[str, Any]]:
        """Per-person allowance and full stored usage for the Table Storage ledger.

        Successful streamed rows that still need reconciliation currently contribute zero;
        their own reservation remains in Table Storage until that exact correlation becomes
        final. Every other row contributes its stored usage here, so settlement no longer
        depends on a global timestamp frontier shared by unrelated requests.
        """
        with self._connection() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    """WITH active AS (
                       SELECT scope_id AS user_id,
                              parent_scope_id AS department_id,
                              token_limit
                       FROM token_budget
                       WHERE period_start = %(period_start)s AND scope_type = 'user'
                   ),
                                     retired AS (
                                             SELECT DISTINCT audit.scope_id AS user_id
                                             FROM token_budget_audit audit
                                             WHERE audit.period_start = %(period_start)s
                                                 AND audit.scope_type = 'user'
                                                 AND audit.action = 'removed'
                                                 AND NOT EXISTS (
                                                                     SELECT 1
                                                                     FROM active
                                                                         WHERE active.user_id =
                                                                             audit.scope_id
                                                 )
                                     ),
                                     budgets AS (
                                             SELECT user_id, department_id, token_limit FROM active
                                             UNION ALL
                                               SELECT user_id,
                                                   '' AS department_id,
                                                   0 AS token_limit
                                                             FROM retired
                                     ),
                   confirmed AS (
                       SELECT usage.user_id,
                              COALESCE(SUM(
                                  usage.input_tokens + usage.cached_tokens
                                  + usage.output_tokens
                              ), 0)::BIGINT AS confirmed_tokens
                       FROM token_usage usage
                                             JOIN budgets ON budgets.user_id = usage.user_id
                       WHERE usage.ts >= %(from)s AND usage.ts < %(to)s
                                                 AND usage.usage_domain = 'apim'
                       GROUP BY usage.user_id
                   )
                   SELECT budgets.user_id,
                          budgets.department_id,
                          budgets.token_limit,
                          budget_scope_confirmed_tokens(
                              'person', budgets.user_id, %(period_start)s, %(period_end)s
                          ) AS confirmed_tokens,
                          COALESCE(enforcement.mode, 'audit') AS mode
                   FROM budgets
                   LEFT JOIN confirmed ON confirmed.user_id = budgets.user_id
                   LEFT JOIN department_enforcement enforcement
                          ON enforcement.department_id = budgets.department_id
                   ORDER BY budgets.user_id""",
                    {
                        "period_start": period_start,
                        "period_end": period_end,
                        "from": datetime.combine(period_start, time.min, tzinfo=UTC),
                        "to": datetime.combine(period_end, time.min, tzinfo=UTC),
                    },
                ).fetchall()
            ]
        return rows

    def settled_reservation_correlations(
        self,
        correlation_ids: Sequence[str],
        *,
        scope_type: LedgerScopeType | None = None,
        scope_id: str | None = None,
    ) -> set[str]:
        """Correlations whose reservation can be replaced by stored usage.

        Missing telemetry and successful estimated rows remain reserved. Failed requests
        are final at zero, while non-estimated and reconciled successes are final at their
        stored token counts. Inputs are chunked so a catch-up run cannot create one
        unbounded PostgreSQL parameter.
        """
        if (scope_type is None) != (scope_id is None):
            raise ValueError("ledger scope type and ID must be supplied together")
        wanted = sorted({value for value in correlation_ids if value})
        settled: set[str] = set()
        if not wanted:
            return settled
        if scope_type is not None and scope_id is not None:
            with self._connection() as connection:
                for offset in range(0, len(wanted), _LEDGER_CORRELATION_BATCH_SIZE):
                    chunk = wanted[offset : offset + _LEDGER_CORRELATION_BATCH_SIZE]
                    rows = connection.execute(
                        """SELECT correlation_id FROM settled_budget_reservations(%s, %s, %s)
                           UNION SELECT id::text FROM billable_request_effective
                           WHERE scope_type = %s AND scope_id = %s AND id::text = ANY(%s)
                             AND effective_state = 'exact'""",
                        (scope_type, scope_id, chunk, scope_type, scope_id, chunk),
                    ).fetchall()
                    settled.update(str(row["correlation_id"]) for row in rows)
            return settled
        with self._connection() as connection:
            for offset in range(0, len(wanted), _LEDGER_CORRELATION_BATCH_SIZE):
                chunk = wanted[offset : offset + _LEDGER_CORRELATION_BATCH_SIZE]
                rows = connection.execute(
                    """SELECT DISTINCT usage.correlation_id
                       FROM token_usage usage
                       LEFT JOIN token_usage_application_attribution attribution
                         ON attribution.usage_id = usage.id
                       WHERE usage.correlation_id = ANY(%(ids)s) AND usage.usage_domain = 'apim'
                         AND (%(scope_type)s::TEXT IS NULL
                           OR (%(scope_type)s = 'person' AND usage.user_id = %(scope_id)s)
                           OR (%(scope_type)s = 'application'
                             AND attribution.application_id::TEXT = %(scope_id)s))
                         AND (usage.status_code >= 400 OR NOT usage.estimated
                           OR (usage.reconciled_at IS NOT NULL
                             AND usage.ingest_error = 'stream_cache_usage_unavailable'))""",
                    {"ids": chunk, "scope_type": scope_type, "scope_id": scope_id},
                ).fetchall()
                settled.update(str(row["correlation_id"]) for row in rows)
        if scope_type is not None and scope_id is not None:
            settled.update(
                correlation
                for correlation, kind in self.reservation_finalization_kinds(
                    scope_type, scope_id, wanted
                ).items()
                if kind != "unverified_upper_bound"
            )
        return settled

    def existing_usage_correlations(
        self,
        correlation_ids: Sequence[str],
        *,
        scope_type: LedgerScopeType,
        scope_id: str,
    ) -> set[str]:
        if not correlation_ids:
            return set()
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT usage.correlation_id FROM token_usage usage
                   LEFT JOIN token_usage_application_attribution attribution
                     ON attribution.usage_id = usage.id
                   WHERE usage.usage_domain = 'apim' AND usage.correlation_id = ANY(%s)
                     AND ((%s = 'person' AND usage.user_id = %s)
                       OR (%s = 'application' AND attribution.application_id::TEXT = %s))""",
                (list(correlation_ids), scope_type, scope_id, scope_type, scope_id),
            ).fetchall()
        return {str(row["correlation_id"]) for row in rows}

    def save_budget_reservation_finalizations(
        self,
        items: Sequence[BudgetReservationFinalization],
    ) -> int:
        if not items:
            return 0
        with self._connection() as connection:
            rows = connection.execute(
                """INSERT INTO budget_reservation_finalization (
                     scope_type, scope_id, period_start, correlation_id, reservation_created_at,
                     reservation_tokens, evidence_at, status_code, input_tokens, output_tokens,
                     total_tokens, finalization_kind, source)
                   SELECT * FROM jsonb_to_recordset(%s::JSONB) AS incoming(
                     scope_type TEXT, scope_id TEXT, period_start DATE, correlation_id TEXT,
                     reservation_created_at TIMESTAMPTZ, reservation_tokens BIGINT,
                     evidence_at TIMESTAMPTZ, status_code INTEGER, input_tokens BIGINT,
                     output_tokens BIGINT, total_tokens BIGINT, finalization_kind TEXT, source TEXT)
                   ON CONFLICT (scope_type, scope_id, correlation_id, finalization_kind,
                                source, evidence_at) DO NOTHING RETURNING id""",
                (Jsonb([item.model_dump(mode="json") for item in items]),),
            ).fetchall()
        return len(rows)

    def reservation_finalization_kinds(
        self,
        scope_type: LedgerScopeType,
        scope_id: str,
        correlation_ids: Sequence[str],
    ) -> dict[str, str]:
        if not correlation_ids:
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT correlation_id, finalization_kind
                   FROM budget_reservation_finalization_effective
                   WHERE scope_type = %s AND scope_id = %s AND correlation_id = ANY(%s)""",
                (scope_type, scope_id, list(correlation_ids)),
            ).fetchall()
        return {str(row["correlation_id"]): str(row["finalization_kind"]) for row in rows}

    def budget_scope_confirmed_tokens(
        self,
        scope_type: LedgerScopeType,
        scope_id: str,
        period_start: date,
        period_end: date,
    ) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT budget_scope_confirmed_tokens(%s, %s, %s, %s) AS total",
                (scope_type, scope_id, period_start, period_end),
            ).fetchone()
        return int(row["total"])

    def model_access_ledger_snapshot(
        self, user_ids: Sequence[str] | None = None
    ) -> Sequence[dict[str, Any]]:
        """Every configured model policy, expanded into the identifiers a caller may send.

        The gateway sees two identifier spaces for the same model: the dashboard BFF sends
        the registry UUID while a desktop client sends the model key from its request body.
        The policy cannot resolve one to the other, so both are projected and the check is a
        membership test over the union. A policy row with no access rows is deny-all, which
        is why the join is a LEFT JOIN and an empty array is a meaningful result rather than
        a missing one.
        """
        clause = "" if user_ids is None else " WHERE policy.user_id = ANY(%(user_ids)s)"
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT policy.user_id,
                          COALESCE(
                              array_agg(model.id::TEXT ORDER BY model.id)
                                  FILTER (WHERE model.id IS NOT NULL),
                              ARRAY[]::TEXT[]
                          ) AS model_uuids,
                          COALESCE(
                              array_agg(model.model_key ORDER BY model.id)
                                  FILTER (WHERE model.model_key IS NOT NULL),
                              ARRAY[]::TEXT[]
                          ) AS model_keys
                   FROM user_model_policy policy
                   LEFT JOIN user_model_access access ON access.user_id = policy.user_id
                   LEFT JOIN managed_model model ON model.id = access.model_id
                   {clause}
                   GROUP BY policy.user_id
                   ORDER BY policy.user_id""",
                {} if user_ids is None else {"user_ids": list(user_ids)},
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def person_budget_state(
        self, user_id: str, period_start: date, period_end: date
    ) -> dict[str, Any] | None:
        """One person's allowance, usage so far and their department's enforcement mode.

        This path writes no real-time reservation, so it reads the full stored period
        directly. The employee path uses the same stored total plus one exact R row for
        each APIM correlation that has not reached a final state yet.

        Returns None when the person has no budget for the period, which stays distinct
        from a zero allowance.
        """
        with self._connection() as connection:
            row = connection.execute(
                """SELECT budget.token_limit,
                          budget.parent_scope_id AS department_id,
                          COALESCE(enforcement.mode, 'audit') AS mode,
                          budget_scope_confirmed_tokens(
                              'person', budget.scope_id, %(period_start)s, %(period_end)s
                          ) AS used_tokens
                   FROM token_budget budget
                   LEFT JOIN department_enforcement enforcement
                          ON enforcement.department_id = budget.parent_scope_id
                   LEFT JOIN LATERAL (
                       SELECT SUM(
                                  input_tokens + cached_tokens + output_tokens
                              )::BIGINT AS used_tokens
                       FROM token_usage
                       WHERE user_id = budget.scope_id
                         AND ts >= %(from)s AND ts < %(to)s
                                                 AND usage_domain = 'apim'
                   ) usage ON TRUE
                   WHERE budget.period_start = %(period_start)s
                     AND budget.scope_type = 'user'
                     AND budget.scope_id = %(user_id)s""",
                {
                    "user_id": user_id,
                    "period_start": period_start,
                    "period_end": period_end,
                    "from": datetime.combine(period_start, time.min, tzinfo=UTC),
                    "to": datetime.combine(period_end, time.min, tzinfo=UTC),
                },
            ).fetchone()
            if row is not None:
                return dict(row)
        return None

    def bulk_upsert_user_budgets(
        self,
        period_start: date,
        department_id: str,
        entries: Sequence[tuple[str, int]],
        warning_threshold_percent: int,
        changed_by: str,
        *,
        selected_user_ids: Sequence[str] | None = None,
        model_ids: Sequence[UUID] | None = None,
    ) -> None:
        user_ids = list(selected_user_ids or [user_id for user_id, _ in entries])
        normalized_model_ids = list(dict.fromkeys(model_ids)) if model_ids is not None else None
        with self._connection() as connection, connection.transaction():
            if entries:
                parent_lock_key = f"{period_start}:department:{department_id}"
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (parent_lock_key,),
                )
                parent = connection.execute(
                    """SELECT token_limit
                       FROM token_budget
                       WHERE period_start = %s AND scope_type = 'department'
                         AND scope_id = %s
                       FOR UPDATE""",
                    (period_start, department_id),
                ).fetchone()
                if parent is None:
                    raise BudgetConstraintViolation(
                        "Assign the department budget before allocating user budgets"
                    )
                unselected = connection.execute(
                    """SELECT COALESCE(SUM(token_limit), 0)::BIGINT AS total
                       FROM token_budget
                       WHERE period_start = %s AND scope_type = 'user'
                         AND parent_scope_id = %s
                         AND NOT (scope_id = ANY(%s))""",
                    (period_start, department_id, user_ids),
                ).fetchone()
                proposed_total = int(unselected["total"]) + sum(limit for _, limit in entries)
                if proposed_total > int(parent["token_limit"]):
                    raise BudgetConstraintViolation(
                        "User allocations would exceed the department budget of "
                        f"{int(parent['token_limit'])} tokens"
                    )

                previous_rows = connection.execute(
                    """SELECT scope_id, parent_scope_id, token_limit,
                              warning_threshold_percent
                       FROM token_budget
                       WHERE period_start = %s AND scope_type = 'user'
                         AND scope_id = ANY(%s)
                       FOR UPDATE""",
                    (period_start, user_ids),
                ).fetchall()
                previous = {row["scope_id"]: row for row in previous_rows}
                changed_entries = [
                    (user_id, token_limit)
                    for user_id, token_limit in entries
                    if user_id not in previous
                    or previous[user_id]["parent_scope_id"] != department_id
                    or previous[user_id]["token_limit"] != token_limit
                    or previous[user_id]["warning_threshold_percent"] != warning_threshold_percent
                ]
                audit_entries = [
                    (user_id, token_limit)
                    for user_id, token_limit in entries
                    if user_id not in previous
                    or previous[user_id]["token_limit"] != token_limit
                    or previous[user_id]["warning_threshold_percent"] != warning_threshold_percent
                ]
                if changed_entries:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            """INSERT INTO token_budget (
                                   period_start, scope_type, scope_id, parent_scope_id,
                                   token_limit, warning_threshold_percent, updated_by
                               ) VALUES (%s, 'user', %s, %s, %s, %s, %s)
                               ON CONFLICT (period_start, scope_type, scope_id) DO UPDATE SET
                                   parent_scope_id = EXCLUDED.parent_scope_id,
                                   token_limit = EXCLUDED.token_limit,
                                   warning_threshold_percent = EXCLUDED.warning_threshold_percent,
                                   updated_at = now(),
                                   updated_by = EXCLUDED.updated_by""",
                            [
                                (
                                    period_start,
                                    user_id,
                                    department_id,
                                    token_limit,
                                    warning_threshold_percent,
                                    changed_by,
                                )
                                for user_id, token_limit in changed_entries
                            ],
                        )
                if audit_entries:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            """INSERT INTO token_budget_audit (
                                   id, period_start, scope_type, scope_id, action,
                                   previous_token_limit, new_token_limit,
                                   previous_warning_threshold_percent,
                                   new_warning_threshold_percent, changed_by
                               ) VALUES (%s, %s, 'user', %s, %s, %s, %s, %s, %s, %s)""",
                            [
                                (
                                    uuid4(),
                                    period_start,
                                    user_id,
                                    "updated" if user_id in previous else "assigned",
                                    previous[user_id]["token_limit"]
                                    if user_id in previous
                                    else None,
                                    token_limit,
                                    previous[user_id]["warning_threshold_percent"]
                                    if user_id in previous
                                    else None,
                                    warning_threshold_percent,
                                    changed_by,
                                )
                                for user_id, token_limit in audit_entries
                            ],
                        )

            if normalized_model_ids is not None:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"model-access:{department_id}",),
                )
                requested_models = set(normalized_model_ids)
                if requested_models:
                    available_rows = connection.execute(
                        "SELECT id FROM managed_model WHERE enabled AND id = ANY(%s)",
                        (list(requested_models),),
                    ).fetchall()
                    if {row["id"] for row in available_rows} != requested_models:
                        raise BudgetConstraintViolation(
                            "One or more selected models are unavailable"
                        )
                connection.execute(
                    "SELECT user_id FROM user_model_policy WHERE user_id = ANY(%s) FOR UPDATE",
                    (user_ids,),
                ).fetchall()
                previous_rows = connection.execute(
                    """SELECT policy.user_id,
                              COALESCE(
                                  array_agg(access.model_id ORDER BY access.model_id)
                                      FILTER (WHERE access.model_id IS NOT NULL),
                                  ARRAY[]::UUID[]
                              ) AS model_ids
                       FROM user_model_policy policy
                       LEFT JOIN user_model_access access
                           ON access.user_id = policy.user_id
                       WHERE policy.user_id = ANY(%s)
                       GROUP BY policy.user_id""",
                    (user_ids,),
                ).fetchall()
                previous_models = {row["user_id"]: list(row["model_ids"]) for row in previous_rows}
                changed_user_ids = [
                    user_id
                    for user_id in user_ids
                    if user_id not in previous_models
                    or set(previous_models[user_id]) != set(normalized_model_ids)
                ]
                if not changed_user_ids:
                    return
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """INSERT INTO user_model_policy (user_id, updated_by)
                           VALUES (%s, %s)
                           ON CONFLICT (user_id) DO UPDATE SET
                               updated_at = now(), updated_by = EXCLUDED.updated_by""",
                        [(user_id, changed_by) for user_id in changed_user_ids],
                    )
                connection.execute(
                    "DELETE FROM user_model_access WHERE user_id = ANY(%s)",
                    (changed_user_ids,),
                )
                access_entries = [
                    (user_id, model_id)
                    for user_id in changed_user_ids
                    for model_id in normalized_model_ids
                ]
                if access_entries:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            "INSERT INTO user_model_access (user_id, model_id) VALUES (%s, %s)",
                            access_entries,
                        )
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """INSERT INTO user_model_access_audit (
                               id, user_id, previous_model_ids, new_model_ids, changed_by
                           ) VALUES (%s, %s, %s, %s, %s)""",
                        [
                            (
                                uuid4(),
                                user_id,
                                previous_models.get(user_id, []),
                                normalized_model_ids,
                                changed_by,
                            )
                            for user_id in changed_user_ids
                        ],
                    )
