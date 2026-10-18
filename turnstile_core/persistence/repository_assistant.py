from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from ..domain.models import AuditFindingUpdate, OptimizationEventCreate
from .repository_support import (
    CACHE_DIMENSION_FIELDS,
    UsageFilters,
    attach_charts,
    cache_distribution_supported,
    cache_scope_for_filters,
    conversation_for_creator,
    report_for_viewer,
)


class PostgreSqlAssistantRepositoryMixin:
    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    @staticmethod
    def _filter_sql(filters: UsageFilters, *, alias: str = "usage") -> tuple[str, list[Any]]:
        raise NotImplementedError

    @staticmethod
    def _apply_cache_read_delta(totals: dict[str, Any], delta: int) -> dict[str, Any]:
        raise NotImplementedError

    def _cache_read_trend_deltas(
        self,
        connection: Any,
        from_: datetime,
        to: datetime,
        interval: str,
        timezone: str,
        dimension_type: str,
        filters: UsageFilters,
        dimension_values: Sequence[str] | None = None,
    ) -> dict[tuple[datetime, str], int]:
        raise NotImplementedError

    def list_pinned_reports(self, owner_id: str) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            reports = connection.execute(
                """SELECT report.*, access.created_by, access.visibility,
                                COALESCE(
                                    saved_layout.layout,
                                    '{"version":1,"spans":{},"row_heights":{}}'::jsonb
                                ) AS layout
                         FROM pinned_report AS report
                         JOIN pinned_report_access AS access ON access.report_id = report.id
                         LEFT JOIN pinned_report_layout AS saved_layout
                           ON saved_layout.report_id = report.id
                         WHERE access.created_by = %s OR access.visibility = 'public'
                         ORDER BY (access.created_by <> %s), report.position, report.created_at""",
                (owner_id, owner_id),
            ).fetchall()
            if not reports:
                return []
            reports = [report_for_viewer(row, owner_id) for row in reports]
            # One query for every child rather than one per report: the sidebar reads
            # this on each page load, so an N+1 here is an N+1 on every navigation.
            charts = connection.execute(
                """SELECT * FROM pinned_chart WHERE report_id = ANY(%s)
                   ORDER BY position, created_at""",
                ([row["id"] for row in reports],),
            ).fetchall()
        return attach_charts(cast(Sequence[dict[str, Any]], reports), charts)

    def get_pinned_report(self, report_id: UUID, owner_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            report = connection.execute(
                """SELECT report.*, access.created_by, access.visibility,
                                COALESCE(
                                    saved_layout.layout,
                                    '{"version":1,"spans":{},"row_heights":{}}'::jsonb
                                ) AS layout
                         FROM pinned_report AS report
                         JOIN pinned_report_access AS access ON access.report_id = report.id
                         LEFT JOIN pinned_report_layout AS saved_layout
                           ON saved_layout.report_id = report.id
                         WHERE report.id = %s
                            AND (access.created_by = %s OR access.visibility = 'public')""",
                (report_id, owner_id),
            ).fetchone()
            if report is None:
                return None
            report = report_for_viewer(report, owner_id)
            charts = connection.execute(
                """SELECT * FROM pinned_chart WHERE report_id = %s
                   ORDER BY position, created_at""",
                (report_id,),
            ).fetchall()
        return attach_charts([report], charts)[0]

    def create_pinned_report(
        self,
        *,
        owner_id: str,
        title: str,
        description: str,
        original_question: str,
        chart: Mapping[str, Any],
        directory_scope: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            report = connection.execute(
                """INSERT INTO pinned_report (owner_id, title, description, position)
                   VALUES (
                       %s, %s, %s,
                       COALESCE(
                           (SELECT MAX(report.position) + 1
                            FROM pinned_report AS report
                            JOIN pinned_report_access AS access
                              ON access.report_id = report.id
                            WHERE access.created_by = %s), 0
                       )
                   )
                   RETURNING *""",
                (owner_id, title, description, owner_id),
            ).fetchone()
            assert report is not None
            if directory_scope is not None:
                connection.execute(
                    "UPDATE pinned_report SET directory_scope=%s WHERE id=%s",
                    (directory_scope, report["id"]),
                )
            connection.execute(
                """INSERT INTO pinned_chart (report_id, original_question, chart, position)
                   VALUES (%s, %s, %s, 0) RETURNING *""",
                (report["id"], original_question, Jsonb(dict(chart))),
            ).fetchone()
        created = self.get_pinned_report(report["id"], owner_id)
        assert created is not None
        return created

    def add_chart_to_pinned_report(
        self,
        *,
        report_id: UUID,
        owner_id: str,
        original_question: str,
        chart: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            # The owner check is a SELECT inside the INSERT rather than a prior round
            # trip: a report deleted between the two would otherwise let an orphan
            # through on the FK's error path instead of returning a clean 404.
            row = connection.execute(
                """INSERT INTO pinned_chart (report_id, original_question, chart, position)
                   SELECT
                       %s, %s, %s,
                       COALESCE(
                           (SELECT MAX(position) + 1 FROM pinned_chart WHERE report_id = %s), 0
                       )
                   WHERE EXISTS (
                       SELECT 1 FROM pinned_report_access
                       WHERE report_id = %s AND created_by = %s
                   )
                   ON CONFLICT (report_id, (chart -> 'query')) DO UPDATE
                       SET original_question = EXCLUDED.original_question,
                           chart = EXCLUDED.chart,
                           updated_at = now()
                   RETURNING *""",
                (
                    report_id,
                    original_question,
                    Jsonb(dict(chart)),
                    report_id,
                    report_id,
                    owner_id,
                ),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE pinned_report SET updated_at = now() WHERE id = %s",
                (report_id,),
            )
        return cast(dict[str, Any], row)

    def update_pinned_report(
        self, report_id: UUID, owner_id: str, title: str, description: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE pinned_report AS report
                                     SET title = %s, description = %s, updated_at = now()
                                     FROM pinned_report_access AS access
                                     WHERE report.id = %s
                                         AND access.report_id = report.id
                                         AND access.created_by = %s
                                     RETURNING report.id""",
                (title, description, report_id, owner_id),
            ).fetchone()
        if row is None:
            return None
        return self.get_pinned_report(report_id, owner_id)

    def update_pinned_report_visibility(
        self, report_id: UUID, owner_id: str, visibility: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE pinned_report_access SET visibility = %s
                   WHERE report_id = %s AND created_by = %s RETURNING report_id""",
                (visibility, report_id, owner_id),
            ).fetchone()
        if row is None:
            return None
        return self.get_pinned_report(report_id, owner_id)

    def update_pinned_report_layout(
        self, report_id: UUID, owner_id: str, layout: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """INSERT INTO pinned_report_layout (report_id, layout, updated_by)
                   SELECT report.id, %s, %s
                   FROM pinned_report AS report
                   JOIN pinned_report_access AS access ON access.report_id = report.id
                   WHERE report.id = %s AND access.created_by = %s
                   ON CONFLICT (report_id) DO UPDATE
                       SET layout = EXCLUDED.layout,
                           updated_by = EXCLUDED.updated_by,
                           updated_at = now()
                   RETURNING report_id""",
                (Jsonb(dict(layout)), owner_id, report_id, owner_id),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE pinned_report SET updated_at = now() WHERE id = %s",
                    (report_id,),
                )
        if row is None:
            return None
        return self.get_pinned_report(report_id, owner_id)

    def replace_pinned_chart_data(
        self, chart_id: UUID, owner_id: str, chart: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE pinned_chart AS target
                   SET chart = %s, updated_at = now()
                                     FROM pinned_report_access AS access
                   WHERE target.id = %s
                                         AND target.report_id = access.report_id
                                         AND access.created_by = %s
                   RETURNING target.*""",
                (Jsonb(dict(chart)), chart_id, owner_id),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def delete_pinned_report(self, report_id: UUID, owner_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """DELETE FROM pinned_report AS report
                                     USING pinned_report_access AS access
                                     WHERE report.id = %s
                                         AND access.report_id = report.id
                                         AND access.created_by = %s
                                     RETURNING report.id""",
                (report_id, owner_id),
            ).fetchone()
        return row is not None

    def delete_pinned_chart(self, chart_id: UUID, owner_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """WITH deleted AS (
                       DELETE FROM pinned_chart AS target
                       USING pinned_report_access AS access
                       WHERE target.id = %s
                         AND target.report_id = access.report_id
                         AND access.created_by = %s
                       RETURNING target.id, target.report_id
                   ), cleaned_layout AS (
                       UPDATE pinned_report_layout AS saved
                       SET layout = jsonb_set(
                               saved.layout,
                               '{spans}',
                               COALESCE(saved.layout -> 'spans', '{}'::jsonb)
                                   - deleted.id::text
                           ),
                           updated_by = %s,
                           updated_at = now()
                       FROM deleted
                       WHERE saved.report_id = deleted.report_id
                       RETURNING saved.report_id
                   ), touched_report AS (
                       UPDATE pinned_report AS report
                       SET updated_at = now()
                       FROM deleted
                       WHERE report.id = deleted.report_id
                       RETURNING report.id
                   )
                   SELECT id FROM deleted""",
                (chart_id, owner_id, owner_id),
            ).fetchone()
        return row is not None

    def reorder_pinned_reports(self, owner_id: str, ordered_ids: Sequence[UUID]) -> None:
        if not ordered_ids:
            return
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """UPDATE pinned_report AS report
                         SET position = %s, updated_at = now()
                         FROM pinned_report_access AS access
                         WHERE report.id = %s
                            AND access.report_id = report.id
                            AND access.created_by = %s""",
                [(index, report_id, owner_id) for index, report_id in enumerate(ordered_ids)],
            )

    def reorder_pinned_charts(
        self, report_id: UUID, owner_id: str, ordered_ids: Sequence[UUID]
    ) -> None:
        if not ordered_ids:
            return
        # Ownership is checked in the statement itself, through the report, rather than
        # by reading the report first: a separate check would leave a window in which the
        # report changed hands between the read and the write.
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """UPDATE pinned_chart AS target SET position = %s, updated_at = now()
                                     FROM pinned_report_access AS access
                   WHERE target.id = %s
                                         AND target.report_id = access.report_id
                                         AND access.report_id = %s
                                         AND access.created_by = %s""",
                [
                    (index, chart_id, report_id, owner_id)
                    for index, chart_id in enumerate(ordered_ids)
                ],
            )

    def list_conversations(self, owner_id: str, limit: int) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT c.*, ownership.created_by, count(t.id) AS turn_count
                   FROM assistant_conversation AS c
                                     JOIN assistant_conversation_owner AS ownership
                                         ON ownership.conversation_id = c.id
                   LEFT JOIN assistant_turn AS t ON t.conversation_id = c.id
                                     WHERE ownership.created_by = %s
                                     GROUP BY c.id, ownership.created_by
                   ORDER BY c.updated_at DESC
                   LIMIT %s""",
                (owner_id, limit),
            ).fetchall()
        return [conversation_for_creator(row) for row in rows]

    def get_conversation(self, conversation_id: UUID, owner_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT c.*, ownership.created_by
                                     FROM assistant_conversation AS c
                                     JOIN assistant_conversation_owner AS ownership
                                         ON ownership.conversation_id = c.id
                                     WHERE c.id = %s AND ownership.created_by = %s""",
                (conversation_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            turns = connection.execute(
                """SELECT position, question, reply, created_at FROM assistant_turn
                   WHERE conversation_id = %s ORDER BY position""",
                (conversation_id,),
            ).fetchall()
        conversation = conversation_for_creator(row)
        conversation["turn_count"] = len(turns)
        conversation["turns"] = [dict(turn) for turn in turns]
        return conversation

    def append_conversation_turn(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        title: str,
        question: str,
        reply: Mapping[str, Any],
        directory_scope: list[str] | None = None,
    ) -> None:
        with self._connection() as connection:
            # Upsert rather than "read, branch, write": the first two turns of a new
            # conversation can be in flight together, and the branch version would then
            # try to insert the same id twice. The title is only set by the row that
            # creates the conversation, so a later turn cannot rewrite a name the person
            # may have edited.
            connection.execute(
                """INSERT INTO assistant_conversation (id, owner_id, title)
                         VALUES (%s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET updated_at = now()
                         WHERE EXISTS (
                              SELECT 1 FROM assistant_conversation_owner AS ownership
                              WHERE ownership.conversation_id = assistant_conversation.id
                                 AND ownership.created_by = %s
                         )""",
                (conversation_id, owner_id, title, owner_id),
            )
            if directory_scope is not None:
                connection.execute(
                    """UPDATE assistant_conversation SET directory_scope=%s
                       WHERE id=%s AND directory_scope IS NULL
                         AND NOT EXISTS(SELECT 1 FROM assistant_turn WHERE conversation_id=%s)
                         AND EXISTS(SELECT 1 FROM assistant_conversation_owner
                           WHERE conversation_id=%s AND created_by=%s)""",
                    (directory_scope, conversation_id, conversation_id, conversation_id, owner_id),
                )
            connection.execute(
                """INSERT INTO assistant_turn (conversation_id, position, question, reply)
                   SELECT
                       %s,
                       COALESCE(
                           (SELECT MAX(position) + 1 FROM assistant_turn
                            WHERE conversation_id = %s),
                           0
                       ),
                       %s, %s
                   WHERE EXISTS (
                       SELECT 1 FROM assistant_conversation
                                             JOIN assistant_conversation_owner AS ownership
                                                 ON ownership.conversation_id =
                                                        assistant_conversation.id
                                             WHERE assistant_conversation.id = %s
                                                 AND ownership.created_by = %s
                   )""",
                (
                    conversation_id,
                    conversation_id,
                    question,
                    Jsonb(dict(reply)),
                    conversation_id,
                    owner_id,
                ),
            )

    def rename_conversation(
        self, conversation_id: UUID, owner_id: str, title: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE assistant_conversation AS conversation
                   SET title = %s, title_source = 'user', updated_at = now()
                                     FROM assistant_conversation_owner AS ownership
                                     WHERE conversation.id = %s
                                         AND ownership.conversation_id = conversation.id
                                         AND ownership.created_by = %s
                                     RETURNING conversation.*, ownership.created_by""",
                (title, conversation_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            count = connection.execute(
                "SELECT count(*) AS turn_count FROM assistant_turn WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
        conversation = conversation_for_creator(row)
        conversation["turn_count"] = (count or {}).get("turn_count", 0)
        return conversation

    def summarise_conversation_title(
        self, conversation_id: UUID, owner_id: str, title: str
    ) -> dict[str, Any] | None:
        """Replace a placeholder title, and only a placeholder title.

        The `title_source = 'question'` predicate is the whole point of this method
        existing beside `rename_conversation`: it makes overwriting a name a person typed
        impossible in SQL rather than conditional on the caller checking first. It also
        makes a duplicate request from a retrying client a no-op instead of a second
        billable summary of the same thread.

        `updated_at` is deliberately not touched -- naming a conversation is not activity
        in it, and bumping the timestamp would reorder the history dropdown under the
        reader for a change they did not make.
        """
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE assistant_conversation AS conversation
                                     SET title = %s, title_source = 'model'
                                     FROM assistant_conversation_owner AS ownership
                                     WHERE conversation.id = %s
                                         AND ownership.conversation_id = conversation.id
                                         AND ownership.created_by = %s
                                         AND conversation.title_source = 'question'
                                     RETURNING conversation.*, ownership.created_by""",
                (title, conversation_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            count = connection.execute(
                "SELECT count(*) AS turn_count FROM assistant_turn WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
        conversation = conversation_for_creator(row)
        conversation["turn_count"] = (count or {}).get("turn_count", 0)
        return conversation

    def delete_conversation(self, conversation_id: UUID, owner_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """DELETE FROM assistant_conversation AS conversation
                                     USING assistant_conversation_owner AS ownership
                                     WHERE conversation.id = %s
                                         AND ownership.conversation_id = conversation.id
                                         AND ownership.created_by = %s
                                     RETURNING conversation.id""",
                (conversation_id, owner_id),
            ).fetchone()
        return row is not None

    def assistant_settings(self) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM assistant_setting WHERE id").fetchone()
        # The migration seeds the row, so this only guards a database restored without it.
        # Answering with defaults is better than a 500 on a page that is otherwise fine.
        return dict(cast(dict[str, Any], row)) if row else {"model_id": None, "auto_title": True}

    def save_assistant_settings(
        self,
        *,
        model_id: UUID | None,
        auto_title: bool,
        updated_by: str,
        api_format: str | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """INSERT INTO assistant_setting (id, model_id, auto_title, api_format, updated_by)
                   VALUES (true, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                       model_id = EXCLUDED.model_id,
                       auto_title = EXCLUDED.auto_title,
                       api_format = EXCLUDED.api_format,
                       updated_at = now(),
                       updated_by = EXCLUDED.updated_by
                   RETURNING *""",
                (model_id, auto_title, api_format, updated_by),
            ).fetchone()
        return dict(cast(dict[str, Any], row))

    def trends(
        self,
        from_: datetime,
        to: datetime,
        interval: str,
        group_by: str,
        timezone: str,
        filters: UsageFilters,
    ) -> dict[str, Any]:
        fields = {
            "organization": ("organization_id", "organization"),
            "department": ("department_id", "department"),
            "project": ("project_id", "project"),
            "agent": ("agent_id", "agent"),
            "user": ("user_id", "user_ref"),
            "model": ("model_id", "model"),
            "runtime": ("runtime", "runtime"),
            "workflow": ("workflow", "workflow"),
            "team": ("team", "team"),
        }
        # A percentile cannot be merged across groups, so a chart of the window's own tail
        # latency per bucket needs the bucket ungrouped. Grouping by a constant gives one row
        # per bucket whose percentile is the real one.
        id_field, name_field = (
            ("'all'::text", "'all'::text") if group_by == "none" else fields[group_by]
        )
        filter_sql, filter_parameters = self._filter_sql(filters)
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT
                        date_trunc(%s, usage.ts AT TIME ZONE %s) AT TIME ZONE %s
                            AS bucket_start,
                        {id_field} AS key,
                        (array_agg({name_field} ORDER BY usage.ts DESC, usage.id DESC))[1] AS label,
                        jsonb_build_object(
                            'et', SUM(usage.et)::DOUBLE PRECISION,
                            'total_tokens', SUM(
                                usage.input_tokens + usage.cached_tokens + usage.output_tokens
                            )::BIGINT,
                            'input_tokens', SUM(usage.input_tokens)::BIGINT,
                            'cached_tokens', SUM(usage.cached_tokens)::BIGINT,
                            'cache_read_tokens', SUM(
                                GREATEST(usage.cached_tokens - usage.cache_write_tokens, 0)
                            )::BIGINT,
                            'cache_write_tokens', SUM(usage.cache_write_tokens)::BIGINT,
                            'output_tokens', SUM(usage.output_tokens)::BIGINT,
                            'calls', COUNT(*)::BIGINT,
                            'estimated_cost',
                                COALESCE(SUM(usage.estimated_cost), 0)::DOUBLE PRECISION,
                            'p95_latency_ms', COALESCE(
                                percentile_cont(0.95) WITHIN GROUP (ORDER BY usage.latency_ms),
                                0
                            )::DOUBLE PRECISION,
                            'failed_calls',
                                COUNT(*) FILTER (WHERE usage.status_code >= 400)::BIGINT
                        ) AS totals
                    FROM token_usage usage
                    WHERE usage.ts >= %s AND usage.ts < %s{filter_sql}
                    GROUP BY 1, 2 ORDER BY 1, 2""",
                [interval, timezone, timezone, from_, to, *filter_parameters],
            ).fetchall()
            cache_dimension: str | None = None
            cache_values: tuple[str, ...] | None = None
            collapse_cache = group_by == "none"
            if group_by in CACHE_DIMENSION_FIELDS and cache_distribution_supported(
                group_by, filters
            ):
                cache_dimension = group_by
                cache_values = tuple(sorted({str(row["key"]) for row in rows}))
            elif collapse_cache:
                scope = cache_scope_for_filters(filters)
                if scope is not None:
                    cache_dimension, cache_values = scope
            if cache_dimension is not None:
                deltas = self._cache_read_trend_deltas(
                    connection,
                    from_,
                    to,
                    interval,
                    timezone,
                    cache_dimension,
                    filters,
                    cache_values,
                )
                if collapse_cache:
                    collapsed: dict[datetime, int] = {}
                    for (bucket_start, _), delta in deltas.items():
                        collapsed[bucket_start] = collapsed.get(bucket_start, 0) + delta
                    deltas = {
                        (bucket_start, "all"): delta for bucket_start, delta in collapsed.items()
                    }
                rows_by_key = {(row["bucket_start"], str(row["key"])): row for row in rows}
                for key, delta in deltas.items():
                    row = rows_by_key.get(key)
                    if row is None:
                        row = {
                            "bucket_start": key[0],
                            "key": key[1],
                            "label": key[1],
                            "totals": {
                                "et": 0.0,
                                "total_tokens": 0,
                                "input_tokens": 0,
                                "cached_tokens": 0,
                                "cache_read_tokens": 0,
                                "cache_write_tokens": 0,
                                "output_tokens": 0,
                                "calls": 0,
                                "estimated_cost": 0.0,
                                "p95_latency_ms": 0.0,
                                "failed_calls": 0,
                            },
                        }
                        rows.append(row)
                        rows_by_key[key] = row
                    self._apply_cache_read_delta(row["totals"], delta)
                rows.sort(key=lambda row: (row["bucket_start"], str(row["key"])))
        return {
            "from": from_,
            "to": to,
            "timezone": timezone,
            "interval": interval,
            "group_by": group_by,
            "points": list(rows),
        }

    def list_runs(self, from_: datetime, to: datetime, limit: int) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM token_observability_runs(%s, %s, %s)", (from_, to, limit)
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT token_observability_run_detail(%s) AS payload", (run_id,)
            ).fetchone()
        return None if row is None or row["payload"] is None else dict(row["payload"])

    def list_findings(self, from_: datetime, to: datetime, limit: int) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM audit_finding
                   WHERE created_at >= %s AND created_at < %s
                   ORDER BY created_at DESC LIMIT %s""",
                (from_, to, limit),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def update_finding(self, finding_id: UUID, update: AuditFindingUpdate) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE audit_finding SET status=%s, assignee=%s, resolution_note=%s,
                   updated_at=now() WHERE id=%s RETURNING *""",
                (update.status.value, update.assignee, update.resolution_note, finding_id),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def list_optimizations(self, limit: int) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT token_observability_optimization(id) AS payload
                   FROM optimization_event ORDER BY occurred_at DESC LIMIT %s""",
                (limit,),
            ).fetchall()
        return [dict(row["payload"]) for row in rows]

    def create_optimization(self, create: OptimizationEventCreate) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """INSERT INTO optimization_event (
                       workflow, occurred_at, label, notes, comparison_window_days
                   )
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (
                    create.workflow,
                    create.occurred_at,
                    create.label,
                    create.notes,
                    create.comparison_window_days,
                ),
            ).fetchone()
            result = connection.execute(
                "SELECT token_observability_optimization(%s) AS payload", (row["id"],)
            ).fetchone()
        return cast(dict[str, Any], json.loads(json.dumps(result["payload"], default=str)))
