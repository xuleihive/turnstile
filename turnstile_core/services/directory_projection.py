"""Durable bounded identity projection; never updates budget or usage ledger rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..domain.directory import DirectoryError, DirectoryPrincipal
from ..domain.directory_identity import DirectoryIdentity, DirectoryIdentityWriter
from ..persistence.directory_store import DirectoryStore
from .directory_service import DirectoryService


class DirectoryProjectionWorker:
    def __init__(self, store: DirectoryStore, writer: DirectoryIdentityWriter) -> None:
        self.store = store
        self.writer = writer

    def _claim(self, worker_id: str) -> dict[str, Any] | None:
        self.store.require_ready()
        with self.store.connection() as connection, connection.transaction():
            state = connection.execute(
                "SELECT * FROM directory_control_state FOR UPDATE",
            ).fetchone()
            assert state is not None
            if state["source"] != "database" or state["phase"] not in {
                "active",
                "fresh_empty_ready",
            }:
                raise DirectoryError("directory_not_ready", "Directory authority changed", 503)
            connection.execute(
                """UPDATE directory_projection_run SET status='superseded',completed_at=now(),
                     lease_owner=NULL,lease_expires_at=NULL
                   WHERE status IN ('queued','running','failed') AND directory_version<>%s""",
                (state["active_version"],),
            )
            run = connection.execute(
                """SELECT * FROM directory_projection_run
                   WHERE status IN ('queued','running','failed') FOR UPDATE LIMIT 1""",
            ).fetchone()
            if run and (
                run["available_at"] > datetime.now(UTC)
                or (run["lease_expires_at"] and run["lease_expires_at"] > datetime.now(UTC))
            ):
                return None
            if run is None:
                if (
                    state["projected_at"] is not None
                    and state["projected_version"] == state["active_version"]
                    and (datetime.now(UTC) - state["projected_at"]).total_seconds() < 120
                ):
                    return None
                sequence = state["projection_sequence"] + 1
                connection.execute(
                    "UPDATE directory_control_state SET projection_sequence=%s",
                    (sequence,),
                )
                run = connection.execute(
                    """INSERT INTO directory_projection_run(directory_version,projection_sequence)
                       VALUES (%s,%s) RETURNING *""",
                    (state["active_version"], sequence),
                ).fetchone()
                assert run is not None
                # One snapshot includes all verified identities, even people without budgets.
                connection.execute(
                    """INSERT INTO directory_projection_stage(
                         run_id,cloud,tenant_id,object_id,payload)
                       WITH identities AS (
                         SELECT cloud,tenant_id,object_id,person_id
                         FROM directory_external_binding WHERE object_type='user'
                         UNION
                         SELECT e.cloud,e.tenant_id,e.object_id,l.person_id
                         FROM app_user_external_identity e
                         JOIN directory_account_link l ON l.app_user_id=e.app_user_id
                       )
                       SELECT %s,e.cloud,e.tenant_id,e.object_id,jsonb_build_object(
                         'cloud',e.cloud,'tenant_id',e.tenant_id,'object_id',e.object_id,
                         'governance_user_id',p.governance_user_id,'display_name',p.display_name,
                         'organization_id',COALESCE(o.id,'unattributed'),
                         'organization_name',COALESCE(o.name,'unattributed'),
                         'department_id',COALESCE(u.id,'unattributed'),
                         'department_name',COALESCE(u.name,'unattributed'),
                         'disabled',p.status<>'active' OR p.manual_disabled
                           OR COALESCE(u.status<>'active',TRUE) OR COALESCE(o.status<>'active',TRUE)
                           OR COALESCE(NOT a.enabled,FALSE)
                           OR EXISTS(SELECT 1 FROM directory_external_binding b
                             WHERE b.person_id=p.id AND b.source_disabled),
                         'directory_version',%s::bigint,'projection_sequence',%s::bigint,
                         'valid_until',LEAST(
                           (SELECT extract(epoch FROM min(t.effective_month::timestamp
                              AT TIME ZONE 'UTC'))::bigint FROM directory_transfer t
                            WHERE t.person_id=p.id AND t.status='scheduled'),
                           (SELECT extract(epoch FROM min(b.last_seen_at+make_interval(
                              mins=>c.sync_interval_minutes*2+15)))::bigint
                            FROM directory_external_binding b
                            JOIN directory_connection c ON c.id=b.connection_id
                            WHERE b.person_id=p.id AND b.object_type='user')
                         )
                       )
                       FROM identities e JOIN directory_person p ON p.id=e.person_id
                       LEFT JOIN directory_membership m ON m.person_id=p.id
                         AND m.membership_kind='primary_department' AND m.valid_from<=now()
                         AND (m.valid_to IS NULL OR m.valid_to>now())
                       LEFT JOIN directory_unit u ON u.id=m.unit_id
                       LEFT JOIN directory_organization o ON o.id=u.organization_id
                       LEFT JOIN directory_account_link l ON l.person_id=p.id
                       LEFT JOIN app_user a ON a.id=l.app_user_id""",
                    (run["id"], state["active_version"], sequence),
                )
            claimed = connection.execute(
                """UPDATE directory_projection_run SET status='running',attempts=attempts+1,
                     lease_owner=%s,lease_expires_at=now()+interval '2 minutes',error_code=NULL
                   WHERE id=%s RETURNING *""",
                (worker_id, run["id"]),
            ).fetchone()
            assert claimed is not None
            return dict(claimed)

    def run_once(self, *, worker_id: str, batch_size: int = 100) -> dict[str, Any] | None:
        if not 1 <= batch_size <= 500:
            raise ValueError("Projection batch size must be between 1 and 500")
        worker_id = worker_id[:128]
        run = self._claim(worker_id)
        if run is None:
            return None
        try:
            with self.store.connection() as connection:
                rows = connection.execute(
                    """SELECT ordinal,payload FROM directory_projection_stage WHERE run_id=%s
                       AND ordinal>%s ORDER BY ordinal LIMIT %s""",
                    (run["id"], run["cursor"], batch_size),
                ).fetchall()
            superseded = False
            for row in rows:
                with self.store.connection() as connection:
                    owned = connection.execute(
                        """UPDATE directory_projection_run r
                           SET lease_expires_at=now()+interval '2 minutes'
                           FROM directory_control_state s
                           WHERE r.id=%s AND r.status='running' AND r.lease_owner=%s
                             AND r.lease_expires_at>now() AND r.directory_version=s.active_version
                             AND s.source='database' RETURNING r.id""",
                        (run["id"], worker_id),
                    ).fetchone()
                if not owned:
                    raise DirectoryError(
                        "projection_lease_lost", "Projection lease or snapshot changed"
                    )
                outcome = self.writer.project(
                    DirectoryIdentity.model_validate(row["payload"]),
                    now=int(datetime.now(UTC).timestamp()),
                )
                superseded |= outcome == "superseded"
            with self.store.connection() as connection, connection.transaction():
                state = connection.execute(
                    "SELECT * FROM directory_control_state FOR UPDATE",
                ).fetchone()
                current = connection.execute(
                    "SELECT * FROM directory_projection_run WHERE id=%s FOR UPDATE",
                    (run["id"],),
                ).fetchone()
                if (
                    not current
                    or current["lease_owner"] != worker_id
                    or current["status"] != "running"
                    or current["lease_expires_at"] <= datetime.now(UTC)
                    or not state
                    or state["active_version"] != run["directory_version"]
                ):
                    raise DirectoryError(
                        "projection_lease_lost", "Projection lease or snapshot changed"
                    )
                if superseded:
                    raise DirectoryError(
                        "projection_version_ahead",
                        "Table identity version is ahead of this directory",
                    )
                cursor = rows[-1]["ordinal"] if rows else run["cursor"]
                remaining = connection.execute(
                    """SELECT 1 FROM directory_projection_stage
                       WHERE run_id=%s AND ordinal>%s LIMIT 1""",
                    (run["id"], cursor),
                ).fetchone()
                complete = remaining is None
                connection.execute(
                    """UPDATE directory_projection_run SET cursor=%s,status=%s,lease_owner=NULL,
                         lease_expires_at=NULL,completed_at=CASE WHEN %s THEN now() END
                       WHERE id=%s""",
                    (cursor, "completed" if complete else "queued", complete, run["id"]),
                )
                if complete:
                    connection.execute(
                        """UPDATE directory_control_state SET projected_version=%s,
                             projected_sequence=%s,projected_at=now()""",
                        (run["directory_version"], run["projection_sequence"]),
                    )
                    connection.execute(
                        """UPDATE directory_outbox SET status='completed',completed_at=now(),
                             lease_owner=NULL,lease_expires_at=NULL WHERE directory_version<=%s
                             AND status<>'completed'""",
                        (run["directory_version"],),
                    )
                    DirectoryService._audit(
                        connection,
                        DirectoryPrincipal(email=worker_id, role="system"),
                        "projection",
                        str(run["id"]),
                        "projection_completed",
                        source="projection",
                        after={
                            "directory_version": run["directory_version"],
                            "projection_sequence": run["projection_sequence"],
                        },
                    )
                return {
                    "id": str(run["id"]),
                    "status": "completed" if complete else "queued",
                    "projected": len(rows),
                    "directory_version": run["directory_version"],
                }
        except Exception as error:
            code = getattr(error, "code", "projection_unavailable")
            with self.store.connection() as connection:
                connection.execute(
                    """UPDATE directory_projection_run SET status='failed',error_code=%s,
                         lease_owner=NULL,lease_expires_at=NULL,
                         available_at=now()+interval '1 minute'
                       WHERE id=%s AND status='running' AND lease_owner=%s""",
                    (code, run["id"], worker_id),
                )
                DirectoryService._audit(
                    connection,
                    DirectoryPrincipal(email=worker_id, role="system"),
                    "projection",
                    str(run["id"]),
                    "projection_failed",
                    source="projection",
                    after={"error_code": code},
                )
            raise
