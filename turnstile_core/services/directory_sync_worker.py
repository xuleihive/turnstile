"""Leased, reviewed, read-only Graph synchronization.

Selected Groups are reconciled from complete member snapshots even in delta mode:
Group deltas alone cannot prove nested membership or user-profile completeness.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from ..domain.directory import DirectoryError, DirectoryPrincipal, PersonWrite, normalized_email
from ..integrations.entra_directory import USER_FIELDS
from ..persistence.directory_store import DirectoryConnection, DirectoryStore
from ..security import CredentialCipher, CredentialError
from .directory_connections import GraphFactory
from .directory_service import DirectoryService, json_value
from .directory_sync_apply import apply_staged_directory

logger = logging.getLogger(__name__)
MAX_OBJECTS = 100_000


class DirectorySyncWorker:
    def __init__(
        self,
        store: DirectoryStore,
        graph_factory: GraphFactory,
        cipher: CredentialCipher,
    ) -> None:
        self.store = store
        self.directory = DirectoryService(store)
        self.graph_factory = graph_factory
        self.cipher = cipher

    def enqueue_due(self) -> int:
        self.store.require_ready()
        with self.store.connection() as connection:
            rows = connection.execute(
                """INSERT INTO directory_sync_job(
                     connection_id,connection_revision,directory_version,mode,requested_by,idempotency_key)
                   SELECT c.id,c.revision,s.active_version,'delta','directory-scheduler',
                     'scheduled:'||c.id::text||':'||floor(
                       extract(epoch FROM now())/(c.sync_interval_minutes*60))::text
                   FROM directory_connection c CROSS JOIN directory_control_state s
                   WHERE c.enabled AND c.validated_revision=c.revision
                     AND (c.last_success_at IS NULL OR c.last_success_at
                       +make_interval(mins=>c.sync_interval_minutes)<=now())
                     AND NOT EXISTS(SELECT 1 FROM directory_sync_job j WHERE j.connection_id=c.id
                       AND j.status IN ('queued','running','awaiting_review','applying'))
                     AND NOT EXISTS(SELECT 1 FROM directory_sync_job j WHERE j.connection_id=c.id
                       AND j.created_at+make_interval(mins=>c.sync_interval_minutes)>now())
                   ON CONFLICT DO NOTHING RETURNING id"""
            ).fetchall()
        return len(rows)

    def _claim(self, worker_id: str) -> dict[str, Any] | None:
        with self.store.connection() as connection, connection.transaction():
            row = connection.execute(
                """SELECT * FROM directory_sync_job
                   WHERE status IN ('queued','running','applying')
                     AND (lease_expires_at IS NULL OR lease_expires_at<now())
                   ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            status = "applying" if row["status"] == "applying" else "running"
            claimed = connection.execute(
                """UPDATE directory_sync_job SET status=%s,lease_owner=%s,
                     lease_expires_at=now()+interval '5 minutes',
                     attempts=attempts+CASE WHEN lease_owner IS NOT NULL THEN 1 ELSE 0 END,
                     started_at=COALESCE(started_at,now()) WHERE id=%s RETURNING *""",
                (status, worker_id[:128], row["id"]),
            ).fetchone()
            assert claimed is not None
            return dict(claimed)

    def _renew(self, job_id: UUID, worker_id: str) -> None:
        with self.store.connection() as connection:
            row = connection.execute(
                """UPDATE directory_sync_job SET lease_expires_at=now()+interval '5 minutes'
                   WHERE id=%s AND lease_owner=%s AND lease_expires_at>now()
                     AND status IN ('running','applying') RETURNING id""",
                (job_id, worker_id),
            ).fetchone()
        if row is None:
            raise DirectoryError("sync_lease_lost", "Synchronization no longer owns its lease")

    @staticmethod
    def _assert_current(
        connection: DirectoryConnection,
        job: dict[str, Any],
        worker_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        state = connection.execute("SELECT * FROM directory_control_state FOR UPDATE").fetchone()
        current = connection.execute(
            "SELECT * FROM directory_sync_job WHERE id=%s FOR UPDATE",
            (job["id"],),
        ).fetchone()
        saved = connection.execute(
            "SELECT * FROM directory_connection WHERE id=%s FOR UPDATE",
            (job["connection_id"],),
        ).fetchone()
        if (
            current is None
            or current["lease_owner"] != worker_id
            or current["lease_expires_at"] <= datetime.now(UTC)
            or current["status"] != job["status"]
        ):
            raise DirectoryError("sync_lease_lost", "Synchronization no longer owns its lease")
        if (
            state is None
            or state["source"] != "database"
            or state["active_version"] != job["directory_version"]
            or saved is None
            or not saved["enabled"]
            or saved["revision"] != job["connection_revision"]
            or saved["validated_revision"] != saved["revision"]
        ):
            raise DirectoryError(
                "preview_stale", "Directory or connection changed; generate a new preview"
            )
        return dict(saved), dict(state)

    def run_once(self, *, worker_id: str) -> dict[str, Any] | None:
        worker_id = worker_id[:128]
        self.store.require_ready()
        job = self._claim(worker_id)
        if job is None:
            return None
        try:
            if job["attempts"] > 5:
                raise DirectoryError("sync_retry_limit", "Synchronization retry limit exceeded")
            if job["status"] == "applying":
                self._apply(job, worker_id)
            else:
                if not self._read_segment(job, worker_id):
                    return self._public_progress(job["id"])
                self._analyze_segment(job, worker_id)
        except (DirectoryError, CredentialError) as error:
            self._fail(job, worker_id, getattr(error, "code", "checkpoint_unavailable"))
        except Exception:
            logger.warning("Directory synchronization failed (%s)", job["id"])
            self._fail(job, worker_id, "sync_execution_failed")
            raise
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT id,status,error_code,summary FROM directory_sync_job WHERE id=%s",
                (job["id"],),
            ).fetchone()
        return json_value(dict(row)) if row else None

    def _public_progress(self, job_id: UUID) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT id,status,error_code,summary FROM directory_sync_job WHERE id=%s",
                (job_id,),
            ).fetchone()
        return json_value(dict(row)) if row else None

    def _release_segment(
        self,
        connection: DirectoryConnection,
        job: dict[str, Any],
    ) -> None:
        connection.execute(
            """UPDATE directory_sync_job SET lease_owner=NULL,lease_expires_at=NULL
               WHERE id=%s""",
            (job["id"],),
        )

    def _read_segment(self, job: dict[str, Any], worker_id: str) -> bool:
        with self.store.connection() as connection, connection.transaction():
            saved, _ = self._assert_current(connection, job, worker_id)
            current = connection.execute(
                "SELECT * FROM directory_sync_job WHERE id=%s",
                (job["id"],),
            ).fetchone()
            assert current is not None
            if current["snapshot_completed_at"]:
                return True
            progress_raw = self.cipher.decrypt(current["read_progress_ciphertext"])
            if progress_raw:
                progress = json.loads(progress_raw)
            else:
                checkpoints = (
                    {
                        row["resource"]: self.cipher.decrypt(row["checkpoint_ciphertext"])
                        for row in connection.execute(
                            """SELECT * FROM directory_sync_checkpoint WHERE connection_id=%s
                           AND rule_revision=%s""",
                            (saved["id"], saved["revision"]),
                        ).fetchall()
                    }
                    if job["mode"] == "delta"
                    else {}
                )
                resources: list[dict[str, Any]] = []
                if saved["scope"] == "all_users":
                    resources.append(
                        {
                            "kind": "users",
                            "path": "users",
                            "params": {"$select": USER_FIELDS, "$top": "50"},
                            "unit_id": saved["default_department_id"],
                        }
                    )
                    resources.append(
                        {
                            "kind": "delta",
                            "resource": "users",
                            "path": checkpoints.get("users") or "users/delta",
                            "params": None
                            if checkpoints.get("users")
                            else {"$select": USER_FIELDS, "$top": "50"},
                        }
                    )
                else:
                    mappings = connection.execute(
                        """SELECT * FROM directory_group_mapping WHERE connection_id=%s AND enabled
                           ORDER BY group_id""",
                        (saved["id"],),
                    ).fetchall()
                    if not mappings:
                        raise DirectoryError(
                            "group_selection_required", "Map selected Groups before synchronization"
                        )
                    for mapping in mappings:
                        group = str(mapping["group_id"])
                        path = (
                            "transitiveMembers"
                            if mapping["membership_mode"] == "transitive"
                            else "members"
                        )
                        resources.append(
                            {
                                "kind": "members",
                                "path": f"groups/{group}/{path}",
                                "params": {"$select": "id", "$top": "50"},
                                "group_id": group,
                                "unit_id": mapping["unit_id"],
                                "group_checked": False,
                            }
                        )
                        key = f"group:{group}"
                        resources.append(
                            {
                                "kind": "delta",
                                "resource": key,
                                "path": checkpoints.get(key) or "groups/delta",
                                "params": None
                                if checkpoints.get(key)
                                else {
                                    "$filter": f"id eq '{group}'",
                                    "$select": "id,displayName",
                                    "$top": "50",
                                },
                            }
                        )
                progress = {
                    "resources": resources,
                    "index": 0,
                    "cursors": {},
                    "pages": 0,
                    "visited": [],
                }
        graph = self.graph_factory(saved)
        deadline = time.monotonic() + 45
        processed = 0
        try:
            while progress["index"] < len(progress["resources"]) and processed < 100:
                if time.monotonic() >= deadline:
                    break
                resource = progress["resources"][progress["index"]]
                self._renew(job["id"], worker_id)
                if resource["kind"] == "members" and not resource["group_checked"]:
                    group_details = graph.group(UUID(resource["group_id"]))
                    if group_details.get("visibility") == "HiddenMembership":
                        raise DirectoryError(
                            "hidden_group_not_supported",
                            "Hidden group synchronization requires separate permission review",
                        )
                    resource["group_checked"] = True
                if "page" not in resource:
                    page_key = hashlib.sha256(
                        f"{progress['index']}:{resource['path']}".encode(),
                    ).hexdigest()
                    if page_key in progress["visited"]:
                        raise DirectoryError(
                            "graph_paging_loop", "Directory paging did not advance"
                        )
                    progress["visited"].append(page_key)
                    items, next_link, delta_link = graph.collection_page(
                        resource["path"],
                        resource.get("params"),
                    )
                    progress["pages"] += 1
                    if progress["pages"] > 5000:
                        raise DirectoryError("graph_page_limit", "Directory page limit exceeded")
                    if next_link == resource["path"]:
                        raise DirectoryError(
                            "graph_paging_loop", "Directory paging did not advance"
                        )
                    resource.update(
                        page=items, offset=0, next_link=next_link, delta_link=delta_link
                    )
                source_rows: list[tuple[UUID, dict[str, Any]]] = []
                while resource["offset"] < len(resource["page"]) and processed < 100:
                    if time.monotonic() >= deadline:
                        break
                    item = resource["page"][resource["offset"]]
                    if resource["kind"] != "delta":
                        if resource["kind"] == "members":
                            if not isinstance(item.get("@odata.type"), str):
                                raise DirectoryError(
                                    "graph_incomplete_member", "Directory member type is incomplete"
                                )
                            if item["@odata.type"] != "#microsoft.graph.user":
                                resource["offset"] += 1
                                continue
                            item = graph.user(UUID(str(item["id"])))
                        identity = UUID(str(item["id"]))
                        if not isinstance(item.get("accountEnabled"), bool):
                            raise DirectoryError(
                                "graph_incomplete_user", "Directory user fields are incomplete"
                            )
                        source_rows.append(
                            (identity, {"profile": item, "unit_ids": [resource["unit_id"]]})
                        )
                    resource["offset"] += 1
                    processed += 1
                if resource["offset"] == len(resource["page"]):
                    next_link = resource["next_link"]
                    if resource["kind"] == "delta" and next_link is None:
                        if not resource["delta_link"]:
                            raise DirectoryError(
                                "graph_checkpoint_missing", "Directory delta checkpoint is missing"
                            )
                        progress["cursors"][resource["resource"]] = resource["delta_link"]
                    if next_link:
                        resource["path"], resource["params"] = next_link, None
                        for key in ("page", "offset", "next_link", "delta_link"):
                            resource.pop(key)
                    else:
                        progress["index"] += 1
                        for key in ("page", "offset", "next_link", "delta_link"):
                            resource.pop(key, None)
                with self.store.connection() as connection, connection.transaction():
                    self._assert_current(connection, job, worker_id)
                    for object_id, payload in source_rows:
                        connection.execute(
                            """INSERT INTO directory_sync_source(job_id,object_id,payload)
                               VALUES (%s,%s,%s) ON CONFLICT(job_id,object_id) DO UPDATE
                               SET payload=jsonb_build_object(
                                 'profile',EXCLUDED.payload->'profile',
                                 'unit_ids',(directory_sync_source.payload->'unit_ids')
                                   ||(EXCLUDED.payload->'unit_ids')),observed_at=now()""",
                            (job["id"], object_id, Jsonb(payload)),
                        )
                    count = connection.execute(
                        "SELECT count(*) AS n FROM directory_sync_source WHERE job_id=%s",
                        (job["id"],),
                    ).fetchone()
                    if count and count["n"] > MAX_OBJECTS:
                        raise DirectoryError(
                            "graph_object_limit", "Directory object limit exceeded"
                        )
                    connection.execute(
                        "UPDATE directory_sync_job SET read_progress_ciphertext=%s WHERE id=%s",
                        (self.cipher.encrypt(json.dumps(progress)), job["id"]),
                    )
            complete = bool(progress["index"] >= len(progress["resources"]))
            with self.store.connection() as connection, connection.transaction():
                self._assert_current(connection, job, worker_id)
                if complete:
                    connection.execute(
                        """UPDATE directory_sync_job SET snapshot_completed_at=clock_timestamp(),
                             checkpoint_ciphertext=%s WHERE id=%s""",
                        (self.cipher.encrypt(json.dumps(progress["cursors"])), job["id"]),
                    )
                else:
                    self._release_segment(connection, job)
            return complete
        finally:
            graph.close()

    def _analyze_segment(self, job: dict[str, Any], worker_id: str) -> None:
        with self.store.connection() as connection, connection.transaction():
            saved, _ = self._assert_current(connection, job, worker_id)
            current = connection.execute(
                "SELECT * FROM directory_sync_job WHERE id=%s",
                (job["id"],),
            ).fetchone()
            assert current is not None
            units = {
                row["id"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM directory_unit WHERE organization_id=%s",
                    (saved["organization_id"],),
                ).fetchall()
            }
            rows = connection.execute(
                """SELECT * FROM directory_sync_source WHERE job_id=%s
                   AND (%s::uuid IS NULL OR object_id>%s) ORDER BY object_id LIMIT 100""",
                (job["id"], current["analysis_cursor"], current["analysis_cursor"]),
            ).fetchall()
            for row in rows:
                value = dict(row["payload"])
                decision, conflict = self._decision(
                    connection, saved, units, str(row["object_id"]), value
                )
                email = value.get("governance_user_id")
                if decision == "create" and email:
                    duplicate = connection.execute(
                        """SELECT 1 FROM directory_sync_source WHERE job_id=%s AND object_id<>%s
                           AND lower(COALESCE(NULLIF(payload->'profile'->>'mail',''),
                             payload->'profile'->>'userPrincipalName'))=%s LIMIT 1""",
                        (job["id"], row["object_id"], email),
                    ).fetchone()
                    if duplicate:
                        decision, conflict = "conflict", "duplicate_governance_identity"
                value["observed_at"] = row["observed_at"].isoformat()
                connection.execute(
                    """INSERT INTO directory_sync_stage(
                         job_id,object_id,payload,decision,conflict_code)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT(job_id,object_id) DO NOTHING""",
                    (job["id"], row["object_id"], Jsonb(json_value(value)), decision, conflict),
                )
            cursor = rows[-1]["object_id"] if rows else current["analysis_cursor"]
            remaining = connection.execute(
                """SELECT 1 FROM directory_sync_source WHERE job_id=%s
                   AND (%s::uuid IS NULL OR object_id>%s) LIMIT 1""",
                (job["id"], cursor, cursor),
            ).fetchone()
            if remaining:
                connection.execute(
                    "UPDATE directory_sync_job SET analysis_cursor=%s WHERE id=%s",
                    (cursor, job["id"]),
                )
                self._release_segment(connection, job)
                return
            connection.execute(
                """INSERT INTO directory_sync_stage(job_id,object_id,payload,decision)
                   SELECT %s,b.object_id,jsonb_build_object('person_id',b.person_id),'missing'
                   FROM directory_external_binding b
                   WHERE b.connection_id=%s AND b.object_type='user'
                     AND NOT EXISTS(SELECT 1 FROM directory_sync_source s
                       WHERE s.job_id=%s AND s.object_id=b.object_id)
                   ON CONFLICT DO NOTHING""",
                (job["id"], saved["id"], job["id"]),
            )
            counts = {
                row["decision"]: row["n"]
                for row in connection.execute(
                    """SELECT decision,count(*) AS n FROM directory_sync_stage
                   WHERE job_id=%s GROUP BY decision""",
                    (job["id"],),
                ).fetchall()
            }
            bound = connection.execute(
                """SELECT count(*) AS n FROM directory_external_binding
                   WHERE connection_id=%s AND object_type='user'""",
                (saved["id"],),
            ).fetchone()
            summary = {
                key: counts.get("conflict" if key == "conflicts" else key, 0)
                for key in ("create", "update", "conflicts", "ignored", "missing")
            }
            summary["mass_change"] = bool(
                bound and bound["n"] and summary["missing"] / bound["n"] > 0.2
            )
            connection.execute(
                """UPDATE directory_sync_job
                   SET status='awaiting_review',summary=%s,analysis_cursor=%s,
                     lease_owner=NULL,lease_expires_at=NULL,previewed_at=snapshot_completed_at,
                     preview_expires_at=clock_timestamp()+interval '30 minutes' WHERE id=%s""",
                (Jsonb(summary), cursor, job["id"]),
            )

    def _fail(self, job: dict[str, Any], worker_id: str, code: str) -> None:
        with self.store.connection() as connection:
            connection.execute(
                """UPDATE directory_sync_job SET status=%s,error_code=%s,completed_at=now(),
                     lease_owner=NULL,lease_expires_at=NULL
                   WHERE id=%s AND lease_owner=%s AND status IN ('running','applying')""",
                ("conflicted" if code == "preview_stale" else "failed", code, job["id"], worker_id),
            )

    def _decision(
        self,
        connection: DirectoryConnection,
        saved: dict[str, Any],
        units: dict[str, dict[str, Any]],
        identity: str,
        value: dict[str, Any],
    ) -> tuple[str, str | None]:
        profile = value["profile"]
        if profile.get("userType") not in {"Guest", "Member"}:
            return "conflict", "incomplete_profile"
        if profile.get("userType") == "Guest" and not saved["include_guests"]:
            bound = connection.execute(
                """SELECT person_id FROM directory_external_binding WHERE connection_id=%s
                   AND object_type='user' AND object_id=%s""",
                (saved["id"], UUID(identity)),
            ).fetchone()
            if bound:
                value["person_id"] = str(bound["person_id"])
                return "missing", None
            return "ignored", None
        if not isinstance(profile.get("accountEnabled"), bool) or not profile.get("displayName"):
            return "conflict", "incomplete_profile"
        try:
            email = normalized_email(profile.get("mail") or profile.get("userPrincipalName") or "")
            if len(email) > 255:
                raise ValueError("Governance identity is too long")
        except ValueError:
            return "conflict", "governance_identity_missing"
        try:
            validated = PersonWrite(
                display_name=profile["displayName"],
                contact_email=email,
                job_title=profile.get("jobTitle") or "",
            )
        except ValueError:
            return "conflict", "incomplete_profile"
        profile["displayName"] = validated.display_name
        profile["jobTitle"] = validated.job_title
        value["governance_user_id"] = email
        unit_ids = set(value["unit_ids"])
        departments: set[str] = set()
        for unit_id in unit_ids:
            unit = units.get(unit_id)
            if unit is None or unit["status"] != "active":
                return "conflict", "inactive_mapping"
            departments.add(unit["id"] if unit["kind"] == "department" else unit["parent_unit_id"])
        if len(departments) != 1:
            return "conflict", "multiple_primary_departments"
        value["department_id"] = next(iter(departments))
        value["team_ids"] = sorted(unit for unit in unit_ids if units[unit]["kind"] == "team")
        existing = connection.execute(
            """SELECT * FROM directory_external_binding WHERE cloud=%s AND tenant_id=%s
               AND object_type='user' AND object_id=%s""",
            (saved["cloud"], saved["tenant_id"], UUID(identity)),
        ).fetchone()
        if existing:
            if existing["connection_id"] != saved["id"]:
                return "conflict", "identity_owned_by_another_connection"
            person = self.directory._person(connection, existing["person_id"])
            value["person_id"] = str(person["id"])
            value["expected_revision"] = person["revision"]
            if person["department_id"] != value["department_id"]:
                return "conflict", "department_change_requires_transfer"
            source_teams = {
                row["unit_id"]
                for row in connection.execute(
                    """SELECT unit_id FROM directory_membership WHERE person_id=%s
                       AND source_key=%s AND membership_kind='team' AND valid_to IS NULL""",
                    (person["id"], f"entra:{saved['id']}"),
                ).fetchall()
            }
            if (
                person["display_name"] == validated.display_name
                and person["contact_email"] == email
                and person["job_title"] == validated.job_title
                and existing["source_disabled"] == (not profile["accountEnabled"])
                and source_teams == set(value["team_ids"])
            ):
                return "ignored", None
            return "update", None
        collision = connection.execute(
            """SELECT id FROM directory_person
               WHERE governance_user_id=%s OR lower(contact_email)=%s
               UNION SELECT l.person_id FROM app_user a
                 JOIN directory_account_link l ON l.app_user_id=a.id
                 WHERE lower(a.email)=%s""",
            (email, email, email),
        ).fetchone()
        if collision:
            return "conflict", "explicit_person_binding_required"
        return "create", None

    def _apply(self, job: dict[str, Any], worker_id: str) -> None:
        with self.store.connection() as connection, connection.transaction():
            saved, state = self._assert_current(connection, job, worker_id)
            if job["preview_expires_at"] is None or job["preview_expires_at"] <= datetime.now(UTC):
                raise DirectoryError(
                    "preview_expired",
                    "Synchronization preview expired; read the source again",
                )
            approver = connection.execute(
                "SELECT 1 FROM app_user WHERE id=%s AND enabled AND role='owner' FOR UPDATE",
                (UUID(job["summary"]["approved_by"]),),
            ).fetchone()
            if approver is None:
                raise DirectoryError(
                    "sync_approval_revoked",
                    "Synchronization approver is no longer an enabled Owner",
                )
            principal = DirectoryPrincipal(email="entra-directory-worker", role="system")
            apply_staged_directory(connection, job, saved)
            version = state["active_version"] + 1
            connection.execute(
                """UPDATE directory_control_state SET active_version=%s,
                     permission_revision=permission_revision+1,updated_at=now()""",
                (version,),
            )
            connection.execute(
                "INSERT INTO directory_outbox(entity_id,directory_version) VALUES (%s,%s)",
                (str(saved["id"]), version),
            )
            cursors = json.loads(self.cipher.decrypt(job["checkpoint_ciphertext"]) or "{}")
            for resource, cursor in cursors.items():
                connection.execute(
                    """INSERT INTO directory_sync_checkpoint(
                         connection_id,resource,rule_revision,checkpoint_ciphertext)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT(connection_id,resource) DO UPDATE SET
                         rule_revision=EXCLUDED.rule_revision,checkpoint_ciphertext=EXCLUDED.checkpoint_ciphertext,
                         updated_at=now()""",
                    (saved["id"], resource, saved["revision"], self.cipher.encrypt(cursor)),
                )
            connection.execute(
                """UPDATE directory_sync_job SET status='succeeded',completed_at=now(),
                     lease_owner=NULL,lease_expires_at=NULL WHERE id=%s""",
                (job["id"],),
            )
            connection.execute(
                "UPDATE directory_connection SET last_success_at=now() WHERE id=%s",
                (saved["id"],),
            )
            self.directory._audit(
                connection,
                principal,
                "sync_job",
                str(job["id"]),
                "sync_succeeded",
                organization_id=saved["organization_id"],
                source="entra",
                job_id=job["id"],
                after=job["summary"],
            )
