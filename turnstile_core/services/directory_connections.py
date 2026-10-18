from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from psycopg.types.json import Jsonb

from ..domain.directory import (
    ConnectionWrite,
    DirectoryError,
    DirectoryPrincipal,
    ExternalBindingWrite,
    GroupMappingsWrite,
    SyncApplyWrite,
    SyncJobWrite,
)
from ..integrations.entra_directory import CLOUD_ENDPOINTS, GraphDirectoryClient
from ..persistence.directory_store import DirectoryConnection, DirectoryStore
from .directory_service import DirectoryService, json_value

GraphFactory = Callable[[dict[str, Any]], GraphDirectoryClient]


def public_connection(value: dict[str, Any]) -> dict[str, Any]:
    return json_value(
        {key: item for key, item in value.items() if key not in {"credential_ref"}}
        | {"credential_configured": bool(value.get("credential_ref"))}
    )


class DirectoryConnectionService:
    def __init__(
        self,
        directory: DirectoryService,
        graph_factory: GraphFactory,
        *,
        sync_enabled: bool,
        deployment_cloud: str = "public",
    ) -> None:
        self.directory = directory
        self.store: DirectoryStore = directory.store
        self.graph_factory = graph_factory
        self.sync_enabled = sync_enabled
        self.deployment_cloud = deployment_cloud

    @staticmethod
    def _connection(connection: DirectoryConnection, connection_id: UUID) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM directory_connection WHERE id=%s FOR UPDATE",
            (connection_id,),
        ).fetchone()
        if row is None:
            raise DirectoryError("connection_not_found", "Directory connection not found", 404)
        return dict(row)

    def connections(self, principal: DirectoryPrincipal) -> list[dict[str, Any]]:
        principal.require_owner()
        self.store.require_ready()
        with self.store.connection() as connection:
            return [
                public_connection(dict(row))
                for row in connection.execute(
                    "SELECT * FROM directory_connection ORDER BY name,id"
                ).fetchall()
            ]

    def _validate_configuration(
        self,
        connection: DirectoryConnection,
        write: ConnectionWrite,
    ) -> None:
        organization = self.directory._entity(connection, "organization", write.organization_id)
        self.directory._active(organization)
        if write.scope == "all_users":
            if write.default_department_id is None:
                raise DirectoryError(
                    "default_department_required", "Select an explicit default department", 422
                )
            department = self.directory._entity(connection, "unit", write.default_department_id)
            self.directory._active(department)
            if (
                department["organization_id"] != write.organization_id
                or department["kind"] != "department"
            ):
                raise DirectoryError(
                    "invalid_default_department",
                    "Default department is outside this organization",
                    422,
                )
        if write.authentication_mode == "key_vault_secret":
            parsed = urlparse(write.credential_ref or "")
            if (
                not write.client_id
                or parsed.scheme != "https"
                or not parsed.hostname
                or not parsed.hostname.endswith(CLOUD_ENDPOINTS[self.deployment_cloud][2])
                or parsed.username
                or parsed.password
                or parsed.port not in {None, 443}
                or parsed.query
                or parsed.fragment
                or not parsed.path.startswith("/secrets/")
                or len(parsed.path.strip("/").split("/")) not in {2, 3}
            ):
                raise DirectoryError(
                    "invalid_credential_reference", "Select a Key Vault secret reference", 422
                )
        elif write.credential_ref is not None or write.client_id is not None:
            raise DirectoryError(
                "managed_identity_configuration",
                "System managed identity does not accept application credentials",
                422,
            )

    def create(
        self,
        principal: DirectoryPrincipal,
        write: ConnectionWrite,
        key: str | None,
    ) -> dict[str, Any]:
        principal.require_owner()
        if write.enabled:
            raise DirectoryError(
                "connection_unvalidated", "Validate the connection before enabling it"
            )

        def create(connection: DirectoryConnection) -> dict[str, Any]:
            self._validate_configuration(connection, write)
            row = self.directory._insert(
                connection,
                "directory_connection",
                {
                    **write.model_dump(exclude={"expected_revision", "reason"}),
                    "updated_by": principal.email,
                },
            )
            self.directory._audit(
                connection,
                principal,
                "connection",
                str(row["id"]),
                "created",
                after=public_connection(row),
                organization_id=write.organization_id,
                reason=write.reason,
            )
            return public_connection(row)

        return self.directory._mutate(
            principal,
            "create-connection",
            write.model_dump(),
            create,
            key,
        )

    def update(
        self,
        principal: DirectoryPrincipal,
        connection_id: UUID,
        write: ConnectionWrite,
    ) -> dict[str, Any]:
        principal.require_owner()

        def update(connection: DirectoryConnection) -> dict[str, Any]:
            before = self._connection(connection, connection_id)
            self.directory._revision(before, write)
            if "credential_ref" not in write.model_fields_set:
                write.credential_ref = before["credential_ref"]
            self._validate_configuration(connection, write)
            values = write.model_dump(exclude={"expected_revision", "reason"})
            changed = any(
                values[field] != before[field]
                for field in (
                    "organization_id",
                    "cloud",
                    "tenant_id",
                    "client_id",
                    "credential_ref",
                    "authentication_mode",
                    "scope",
                    "default_department_id",
                    "include_guests",
                )
            )
            if write.enabled and (changed or before["validated_revision"] != before["revision"]):
                raise DirectoryError(
                    "connection_unvalidated", "Validate the revised connection before enabling it"
                )
            row = self.directory._update(
                connection,
                "directory_connection",
                connection_id,
                {
                    **values,
                    "updated_by": principal.email,
                    "validated_revision": (
                        before["revision"] + 1
                        if not changed and before["validated_revision"] == before["revision"]
                        else None
                    ),
                    "validated_at": None if changed else before["validated_at"],
                },
            )
            self.directory._audit(
                connection,
                principal,
                "connection",
                str(connection_id),
                "updated",
                before=public_connection(before),
                after=public_connection(row),
                organization_id=write.organization_id,
                reason=write.reason,
            )
            return public_connection(row)

        return self.directory._mutate(
            principal,
            f"update-connection:{connection_id}",
            write.model_dump(),
            update,
        )

    def mappings(
        self,
        principal: DirectoryPrincipal,
        connection_id: UUID,
    ) -> list[dict[str, Any]]:
        principal.require_owner()
        with self.store.connection() as connection:
            self._connection(connection, connection_id)
            return json_value(
                [
                    dict(row)
                    for row in connection.execute(
                        """SELECT m.*,u.name AS unit_name,u.kind FROM directory_group_mapping m
                   JOIN directory_unit u ON u.id=m.unit_id WHERE m.connection_id=%s
                   ORDER BY u.name,m.group_id""",
                        (connection_id,),
                    ).fetchall()
                ]
            )

    def set_mappings(
        self,
        principal: DirectoryPrincipal,
        connection_id: UUID,
        write: GroupMappingsWrite,
    ) -> dict[str, Any]:
        principal.require_owner()

        def update(connection: DirectoryConnection) -> dict[str, Any]:
            before = self._connection(connection, connection_id)
            self.directory._revision(before, write)
            group_ids = [item.group_id for item in write.items]
            if len(group_ids) != len(set(group_ids)):
                raise DirectoryError(
                    "duplicate_group", "A Group may have only one explicit unit mapping", 422
                )
            for item in write.items:
                unit = self.directory._entity(connection, "unit", item.unit_id)
                self.directory._active(unit)
                if unit["organization_id"] != before["organization_id"]:
                    raise DirectoryError(
                        "mapping_scope", "Group unit is outside the connection organization", 422
                    )
            connection.execute(
                "UPDATE directory_group_mapping SET enabled=FALSE WHERE connection_id=%s",
                (connection_id,),
            )
            for item in write.items:
                connection.execute(
                    """INSERT INTO directory_group_mapping(
                         connection_id,group_id,unit_id,membership_mode,enabled)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT(connection_id,group_id) DO UPDATE
                       SET unit_id=EXCLUDED.unit_id,membership_mode=EXCLUDED.membership_mode,
                           enabled=EXCLUDED.enabled,revision=directory_group_mapping.revision+1""",
                    (
                        connection_id,
                        item.group_id,
                        item.unit_id,
                        item.membership_mode,
                        item.enabled,
                    ),
                )
            row = self.directory._update(
                connection,
                "directory_connection",
                connection_id,
                {
                    "validated_revision": None,
                    "validated_at": None,
                    "enabled": False,
                    "updated_by": principal.email,
                },
            )
            self.directory._audit(
                connection,
                principal,
                "connection",
                str(connection_id),
                "group_mappings_updated",
                after={"mappings": write.model_dump(mode="json")["items"]},
                organization_id=before["organization_id"],
                reason=write.reason,
            )
            return public_connection(row)

        return self.directory._mutate(
            principal,
            f"group-mappings:{connection_id}",
            write.model_dump(),
            update,
        )

    def validate(
        self,
        principal: DirectoryPrincipal,
        connection_id: UUID,
    ) -> dict[str, Any]:
        principal.require_owner()
        with self.store.connection() as connection:
            saved = self._connection(connection, connection_id)
            mappings = connection.execute(
                "SELECT * FROM directory_group_mapping WHERE connection_id=%s AND enabled",
                (connection_id,),
            ).fetchall()
        graph = self.graph_factory(saved)
        try:
            if saved["scope"] == "selected_groups" and not mappings:
                raise DirectoryError(
                    "group_selection_required", "Map at least one selected Group", 422
                )
            users = graph.collection("users", {"$select": "id,accountEnabled", "$top": "1"})
            first_user = next(users, None)
            if first_user is not None and not isinstance(first_user.get("accountEnabled"), bool):
                raise DirectoryError(
                    "graph_incomplete_user", "Directory user fields are incomplete", 503
                )
            for mapping in mappings:
                # Validate the actual member and user-detail permissions.
                next(
                    graph.members(
                        mapping["group_id"],
                        transitive=mapping["membership_mode"] == "transitive",
                    ),
                    None,
                )
        finally:
            graph.close()

        def validate(connection: DirectoryConnection) -> dict[str, Any]:
            current = self._connection(connection, connection_id)
            if current["revision"] != saved["revision"]:
                raise DirectoryError("revision_conflict", "Connection changed during validation")
            connection.execute(
                """UPDATE directory_connection SET validated_revision=revision,
                   validated_at=now() WHERE id=%s""",
                (connection_id,),
            )
            return {"id": str(connection_id), "validated": True, "revision": current["revision"]}

        return self.directory._mutate(principal, f"validate:{connection_id}", {}, validate)

    def bind_person(
        self,
        principal: DirectoryPrincipal,
        connection_id: UUID,
        object_id: UUID,
        write: ExternalBindingWrite,
    ) -> dict[str, Any]:
        principal.require_owner()

        def bind(connection: DirectoryConnection) -> dict[str, Any]:
            saved = self._connection(connection, connection_id)
            person = self.directory._person(connection, write.person_id)
            self.directory._revision(person, write)
            stage = connection.execute(
                """SELECT s.payload FROM directory_sync_stage s
                   JOIN directory_sync_job j ON j.id=s.job_id
                   WHERE s.job_id=%s AND s.object_id=%s AND j.connection_id=%s
                     AND j.status='awaiting_review' AND j.connection_revision=%s
                     AND s.conflict_code='explicit_person_binding_required'""",
                (write.job_id, object_id, connection_id, saved["revision"]),
            ).fetchone()
            if stage is None or person["organization_id"] != saved["organization_id"]:
                raise DirectoryError(
                    "binding_candidate_required",
                    "Select a current scoped synchronization conflict",
                )
            connection.execute(
                """INSERT INTO directory_external_binding(
                     connection_id,cloud,tenant_id,object_type,object_id,person_id,
                     source_fields,source_disabled)
                   VALUES (%s,%s,%s,'user',%s,%s,%s,%s)""",
                (
                    connection_id,
                    saved["cloud"],
                    saved["tenant_id"],
                    object_id,
                    write.person_id,
                    Jsonb(stage["payload"]),
                    not stage["payload"]["profile"]["accountEnabled"],
                ),
            )
            if write.bind_login_identity:
                if saved["cloud"] != "public":
                    raise DirectoryError(
                        "login_cloud_unconfigured",
                        "Sovereign-cloud directory sync does not configure console sign-in",
                    )
                if not person["app_user_id"]:
                    raise DirectoryError(
                        "account_link_required",
                        "Link an existing account before authorizing sign-in",
                    )
                connection.execute(
                    """INSERT INTO app_user_external_identity(cloud,tenant_id,object_id,app_user_id)
                       VALUES (%s,%s,%s,%s)""",
                    (saved["cloud"], saved["tenant_id"], object_id, person["app_user_id"]),
                )
            connection.execute(
                """UPDATE directory_sync_job SET status='cancelled',completed_at=now()
                   WHERE id=%s""",
                (write.job_id,),
            )
            self.directory._update(
                connection,
                "directory_person",
                write.person_id,
                {"updated_by": principal.email},
            )
            self.directory._audit(
                connection,
                principal,
                "person",
                str(write.person_id),
                "external_identity_bound",
                after={
                    "cloud": saved["cloud"],
                    "tenant_id": str(saved["tenant_id"]),
                    "object_id": str(object_id),
                    "login_authorized": write.bind_login_identity,
                },
                organization_id=saved["organization_id"],
                department_id=person["department_id"],
                reason=write.reason,
            )
            return {"id": str(write.person_id), "requires_new_preview": True}

        return self.directory._mutate(
            principal,
            f"external-binding:{connection_id}:{object_id}",
            write.model_dump(),
            bind,
        )

    def create_job(
        self,
        principal: DirectoryPrincipal,
        connection_id: UUID,
        write: SyncJobWrite,
        key: str,
    ) -> dict[str, Any]:
        principal.require_owner()
        if not self.sync_enabled:
            raise DirectoryError(
                "directory_worker_disabled", "Directory synchronization worker is not enabled", 503
            )
        if not 1 <= len(key) <= 128:
            raise DirectoryError("invalid_idempotency_key", "Invalid idempotency key", 422)
        with self.store.connection() as connection, connection.transaction():
            state = connection.execute(
                "SELECT * FROM directory_control_state FOR UPDATE"
            ).fetchone()
            saved = self._connection(connection, connection_id)
            if (
                not saved["enabled"]
                or saved["validated_revision"] != saved["revision"]
                or state is None
                or state["source"] != "database"
            ):
                raise DirectoryError(
                    "connection_unvalidated", "An enabled validated connection is required"
                )
            existing = connection.execute(
                "SELECT * FROM directory_sync_job WHERE idempotency_key=%s",
                (key,),
            ).fetchone()
            if existing:
                if existing["connection_id"] != connection_id or existing["mode"] != write.mode:
                    raise DirectoryError(
                        "idempotency_conflict", "Sync retry key belongs to another request"
                    )
                return self.public_job(dict(existing))
            if connection.execute(
                """SELECT 1 FROM directory_sync_job WHERE connection_id=%s
                   AND status IN ('queued','running','awaiting_review','applying')""",
                (connection_id,),
            ).fetchone():
                raise DirectoryError(
                    "sync_in_progress", "Review or finish the current synchronization first"
                )
            row = self.directory._insert(
                connection,
                "directory_sync_job",
                {
                    "connection_id": connection_id,
                    "connection_revision": saved["revision"],
                    "directory_version": state["active_version"],
                    "mode": write.mode,
                    "requested_by": principal.email,
                    "idempotency_key": key,
                },
            )
            return self.public_job(row)

    @staticmethod
    def public_job(value: dict[str, Any]) -> dict[str, Any]:
        return json_value(
            {
                key: item
                for key, item in value.items()
                if key
                not in {
                    "checkpoint_ciphertext",
                    "read_progress_ciphertext",
                    "lease_owner",
                    "lease_expires_at",
                    "analysis_cursor",
                }
            }
        )

    def jobs(
        self,
        principal: DirectoryPrincipal,
        connection_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        principal.require_owner()
        with self.store.connection() as connection:
            clause = " WHERE connection_id=%s" if connection_id else ""
            return [
                self.public_job(dict(row))
                for row in connection.execute(
                    f"SELECT * FROM directory_sync_job{clause} ORDER BY created_at DESC LIMIT 100",
                    (connection_id,) if connection_id else (),
                ).fetchall()
            ]

    def job(self, principal: DirectoryPrincipal, job_id: UUID) -> dict[str, Any]:
        principal.require_owner()
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM directory_sync_job WHERE id=%s", (job_id,)
            ).fetchone()
        if row is None:
            raise DirectoryError("sync_not_found", "Synchronization not found", 404)
        return self.public_job(dict(row))

    def changes(
        self,
        principal: DirectoryPrincipal,
        job_id: UUID,
        *,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self.job(principal, job_id)
        with self.store.connection() as connection:
            count = connection.execute(
                "SELECT count(*) AS count FROM directory_sync_stage WHERE job_id=%s",
                (job_id,),
            ).fetchone()
            rows = connection.execute(
                """SELECT object_id,payload,decision,conflict_code FROM directory_sync_stage
                   WHERE job_id=%s ORDER BY object_id LIMIT %s OFFSET %s""",
                (job_id, limit, offset),
            ).fetchall()
        return {"total": count["count"] if count else 0, "items": json_value(rows)}

    def apply_job(
        self,
        principal: DirectoryPrincipal,
        job_id: UUID,
        write: SyncApplyWrite,
    ) -> dict[str, Any]:
        principal.require_owner()
        with self.store.connection() as connection, connection.transaction():
            state = connection.execute(
                "SELECT * FROM directory_control_state FOR UPDATE"
            ).fetchone()
            job = connection.execute(
                "SELECT * FROM directory_sync_job WHERE id=%s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None:
                raise DirectoryError("sync_not_found", "Synchronization not found", 404)
            saved = self._connection(connection, job["connection_id"])
            if (
                state is None
                or write.expected_directory_version != state["active_version"]
                or job["directory_version"] != state["active_version"]
                or saved["revision"] != job["connection_revision"]
            ):
                raise DirectoryError(
                    "preview_stale", "Directory or mapping changed; generate a new preview"
                )
            if job["status"] != "awaiting_review":
                raise DirectoryError(
                    "sync_not_reviewable", "Synchronization is not awaiting review"
                )
            if job["summary"].get("conflicts"):
                raise DirectoryError(
                    "sync_conflicts", "Resolve synchronization conflicts before applying"
                )
            if job["preview_expires_at"] is None or job["preview_expires_at"] <= datetime.now(UTC):
                raise DirectoryError(
                    "preview_expired",
                    "Synchronization preview expired; read the source again",
                )
            if job["summary"].get("missing") and not write.approve_missing:
                raise DirectoryError(
                    "missing_approval", "Explicitly approve missing-member changes"
                )
            if job["summary"].get("mass_change") and not write.approve_mass_changes:
                raise DirectoryError(
                    "mass_change_approval", "Explicitly approve a large membership reduction"
                )
            summary = {
                **job["summary"],
                "approved_by": str(principal.account_id),
                "approved_label": principal.email,
                "approve_missing": write.approve_missing,
                "approve_mass_changes": write.approve_mass_changes,
            }
            connection.execute(
                "UPDATE directory_sync_job SET status='applying',summary=%s WHERE id=%s",
                (Jsonb(summary), job_id),
            )
        return self.job(principal, job_id)

    def cancel(self, principal: DirectoryPrincipal, job_id: UUID) -> dict[str, Any]:
        principal.require_owner()
        with self.store.connection() as connection:
            row = connection.execute(
                """UPDATE directory_sync_job SET status='cancelled',completed_at=now(),
                     lease_owner=NULL,lease_expires_at=NULL
                   WHERE id=%s AND status IN ('queued','running','awaiting_review','conflicted')
                   RETURNING *""",
                (job_id,),
            ).fetchone()
        if row is None:
            raise DirectoryError(
                "sync_not_cancellable", "Applied or applying synchronization cannot be cancelled"
            )
        return self.public_job(dict(row))
