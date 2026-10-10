from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ..domain.control_plane import (
    GatewayPublication,
    publication_materialized_named_values,
    publication_model_id,
)
from .repository_support import _activation_runtime_config


class PostgreSqlPublicationRepositoryMixin:
    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def _invalidate_identities(self) -> None:
        raise NotImplementedError

    def create_gateway_publication(
        self,
        gateway_profile_id: UUID,
        desired_spec: Mapping[str, Any],
        desired_spec_sha256: str,
        publication_kind: str,
        created_by: str,
        credential_ciphertext: bytes | None = None,
        expected_base_release_id: UUID | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"gateway-publication:{gateway_profile_id}",),
            )
            gateway = connection.execute(
                """SELECT id, enabled, implementation FROM gateway_profile
                   WHERE id = %s""",
                (gateway_profile_id,),
            ).fetchone()
            if (
                gateway is None
                or gateway["implementation"] != "apim"
                or (not gateway["enabled"] and publication_kind != "model_remove")
            ):
                raise ValueError("The selected APIM gateway is unavailable")
            if publication_kind == "route_reconcile":
                effective = connection.execute(
                    """SELECT publication_id FROM effective_gateway_release
                       WHERE gateway_profile_id = %s""",
                    (gateway_profile_id,),
                ).fetchone()
                effective_id = effective["publication_id"] if effective else None
                if expected_base_release_id is None or effective_id != expected_base_release_id:
                    raise ValueError("The effective gateway release changed; reconcile again")
            existing = connection.execute(
                """SELECT * FROM gateway_publication
                                     WHERE gateway_profile_id = %s AND desired_spec_sha256 = %s
                                         AND status NOT IN ('superseded', 'rolled_back')
                                     ORDER BY generation DESC
                                     LIMIT 1""",
                (gateway_profile_id, desired_spec_sha256),
            ).fetchone()
            if existing is not None:
                if existing["status"] != "failed":
                    return cast(dict[str, Any], existing)
                desired = dict(existing["desired_spec"])
                bindings = list(desired.get("bindings") or [])
                latest = dict(bindings[-1]) if bindings else {}
                requires_credential = (
                    latest.get("auth_strategy")
                    in {"named_value_bearer", "named_value_api_key"}
                    and latest.get("key_vault_secret_id") is None
                    and publication_kind != "route_reconcile"
                )
                if (
                    credential_ciphertext is None
                    and requires_credential
                ):
                    raise ValueError("A failed publication requires a replacement API key")
                return self._requeue_gateway_publication(
                    connection, existing, created_by, credential_ciphertext
                )
            in_flight = connection.execute(
                """SELECT id FROM gateway_publication
                   WHERE gateway_profile_id = %s
                     AND status IN (
                       'queued', 'validating', 'provisioning', 'building_revision',
                                             'verifying', 'awaiting_authorization', 'promoting',
                                             'rolling_back'
                     )""",
                (gateway_profile_id,),
            ).fetchone()
            if in_flight is not None:
                raise ValueError("Another gateway publication is already in progress")
            generation = connection.execute(
                """SELECT COALESCE(MAX(generation), 0) + 1 AS generation
                   FROM gateway_publication WHERE gateway_profile_id = %s""",
                (gateway_profile_id,),
            ).fetchone()["generation"]
            base = connection.execute(
                """SELECT publication_id FROM effective_gateway_release
                   WHERE gateway_profile_id = %s""",
                (gateway_profile_id,),
            ).fetchone()
            row = connection.execute(
                """INSERT INTO gateway_publication (
                       gateway_profile_id, generation, desired_spec, desired_spec_sha256,
                       publication_kind, base_release_id, created_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (
                    gateway_profile_id,
                    generation,
                    Jsonb(dict(desired_spec)),
                    desired_spec_sha256,
                    publication_kind,
                    base["publication_id"] if base else None,
                    created_by,
                ),
            ).fetchone()
            connection.execute(
                "INSERT INTO gateway_publication_outbox (publication_id) VALUES (%s)",
                (row["id"],),
            )
            if credential_ciphertext is not None:
                connection.execute(
                    """INSERT INTO gateway_publication_secret (
                           publication_id, credential_ciphertext
                       ) VALUES (%s, %s)""",
                    (row["id"], credential_ciphertext),
                )
            connection.execute(
                """INSERT INTO gateway_publication_audit (
                       publication_id, from_status, to_status, actor
                   ) VALUES (%s, NULL, 'queued', %s)""",
                (row["id"], created_by),
            )
        return cast(dict[str, Any], row)

    def requeue_gateway_publication(
        self,
        publication_id: UUID,
        created_by: str,
        credential_ciphertext: bytes | None,
        desired_spec: Mapping[str, Any] | None = None,
        desired_spec_sha256: str | None = None,
        *,
        image_probe_authorization: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            publication = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = %s FOR UPDATE",
                (publication_id,),
            ).fetchone()
            if publication is None:
                raise ValueError("Gateway publication not found")
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"gateway-publication:{publication['gateway_profile_id']}",),
            )
            return self._requeue_gateway_publication(
                connection,
                publication,
                created_by,
                credential_ciphertext,
                desired_spec,
                desired_spec_sha256,
                image_probe_authorization=image_probe_authorization,
            )

    @staticmethod
    def _requeue_gateway_publication(
        connection: Any,
        publication: Mapping[str, Any],
        created_by: str,
        credential_ciphertext: bytes | None,
        desired_spec: Mapping[str, Any] | None = None,
        desired_spec_sha256: str | None = None,
        *,
        image_probe_authorization: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if publication["status"] != "failed":
            raise ValueError("Only a failed publication can be retried")
        if (desired_spec is None) != (desired_spec_sha256 is None):
            raise ValueError("A replacement release requires both its spec and hash")
        in_flight = connection.execute(
            """SELECT id FROM gateway_publication
               WHERE gateway_profile_id = %s
                 AND status IN (
                   'queued', 'validating', 'provisioning', 'building_revision',
                                     'verifying', 'awaiting_authorization', 'promoting',
                                     'rolling_back'
                 )""",
            (publication["gateway_profile_id"],),
        ).fetchone()
        if in_flight is not None:
            raise ValueError("Another gateway publication is already in progress")
        provisioning_completed = connection.execute(
            """SELECT 1 FROM gateway_publication_audit
               WHERE publication_id = %s
                 AND from_status = 'validating' AND to_status = 'provisioning'
               LIMIT 1""",
            (publication["id"],),
        ).fetchone()
        materialized = publication_materialized_named_values(
            GatewayPublication.model_validate(publication),
            provisioning_completed=provisioning_completed is not None,
        )
        preserved_manifest: dict[str, Any] = (
            {"named_values": materialized} if credential_ciphertext is None and materialized else {}
        )
        oauth_credentials = publication["resource_manifest"].get("oauth_credentials")
        if credential_ciphertext is None and isinstance(oauth_credentials, list):
            preserved_manifest["oauth_credentials"] = list(oauth_credentials)
        authorization = image_probe_authorization or publication["resource_manifest"].get(
            "image_probe_authorization"
        )
        if authorization is not None:
            preserved_manifest["image_probe_authorization"] = dict(authorization)
        generation = publication["resource_manifest"].get("credential_generation")
        if credential_ciphertext is not None:
            preserved_manifest["credential_generation"] = str(uuid4())
        elif generation is not None:
            preserved_manifest["credential_generation"] = generation
        row = connection.execute(
            """UPDATE gateway_publication SET
                   status = 'queued', apim_revision = NULL,
                   policy_sha256 = NULL, resource_manifest = %s,
                   error_code = NULL, error_message = NULL,
                   attempt_count = 0, started_at = NULL, completed_at = NULL,
                   desired_spec = COALESCE(%s, desired_spec),
                   desired_spec_sha256 = COALESCE(%s, desired_spec_sha256),
                   created_by = %s, updated_at = now()
               WHERE id = %s RETURNING *""",
            (
                Jsonb(preserved_manifest),
                Jsonb(dict(desired_spec)) if desired_spec is not None else None,
                desired_spec_sha256,
                created_by,
                publication["id"],
            ),
        ).fetchone()
        connection.execute(
            """UPDATE gateway_publication_outbox SET
                   status = 'queued', available_at = now(),
                   lease_owner = NULL, lease_expires_at = NULL,
                   attempts = 0, last_error_code = NULL,
                   last_error_message = NULL, updated_at = now()
               WHERE publication_id = %s""",
            (publication["id"],),
        )
        if credential_ciphertext is not None:
            connection.execute(
                """INSERT INTO gateway_publication_secret (
                       publication_id, credential_ciphertext
                   ) VALUES (%s, %s)
                   ON CONFLICT (publication_id) DO UPDATE SET
                     credential_ciphertext = EXCLUDED.credential_ciphertext,
                     created_at = now()""",
                (publication["id"], credential_ciphertext),
            )
        else:
            connection.execute(
                "DELETE FROM gateway_publication_secret WHERE publication_id = %s",
                (publication["id"],),
            )
        connection.execute(
            """INSERT INTO gateway_publication_audit (
                   publication_id, from_status, to_status, actor, detail
               ) VALUES (%s, 'failed', 'queued', %s, %s)""",
            (
                publication["id"],
                created_by,
                Jsonb({"image_probe_authorization": image_probe_authorization}),
            ),
        )
        return cast(dict[str, Any], row)

    def resume_gateway_publication_authorization(
        self,
        publication_id: UUID,
        created_by: str,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            publication = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = %s FOR UPDATE",
                (publication_id,),
            ).fetchone()
            if publication is None:
                raise ValueError("Gateway publication not found")
            if publication["status"] != "awaiting_authorization":
                raise ValueError("The publication is not waiting for provider authorization")
            row = connection.execute(
                """UPDATE gateway_publication SET
                       status = 'verifying', error_code = NULL, error_message = NULL,
                       updated_at = now()
                   WHERE id = %s RETURNING *""",
                (publication_id,),
            ).fetchone()
            connection.execute(
                """UPDATE gateway_publication_outbox SET
                       status = 'queued', available_at = now(),
                       lease_owner = NULL, lease_expires_at = NULL,
                       last_error_code = NULL, last_error_message = NULL,
                       updated_at = now()
                   WHERE publication_id = %s""",
                (publication_id,),
            )
            connection.execute(
                """INSERT INTO gateway_publication_audit (
                       publication_id, from_status, to_status, actor
                   ) VALUES (%s, 'awaiting_authorization', 'verifying', %s)""",
                (publication_id, created_by),
            )
        return cast(dict[str, Any], row)

    def gateway_publication_credential(self, publication_id: UUID) -> bytes | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT credential_ciphertext FROM gateway_publication_secret
                   WHERE publication_id = %s""",
                (publication_id,),
            ).fetchone()
        return bytes(row["credential_ciphertext"]) if row is not None else None

    def delete_gateway_publication_credential(self, publication_id: UUID) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM gateway_publication_secret WHERE publication_id = %s",
                (publication_id,),
            )

    def list_gateway_publications(
        self, gateway_profile_id: UUID | None = None, limit: int = 50
    ) -> Sequence[dict[str, Any]]:
        clause = "" if gateway_profile_id is None else "WHERE gateway_profile_id = %(gateway)s"
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT * FROM gateway_publication {clause}
                    ORDER BY created_at DESC LIMIT %(limit)s""",
                {"gateway": gateway_profile_id, "limit": limit},
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def all_gateway_publications(
        self, gateway_profile_id: UUID
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_publication
                   WHERE gateway_profile_id = %s ORDER BY created_at, id""",
                (gateway_profile_id,),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def get_gateway_publication(self, publication_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = %s", (publication_id,)
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def get_gateway_publications(
        self, publication_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not publication_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = ANY(%s)",
                (list(publication_ids),),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def get_gateway_profiles(
        self, gateway_profile_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not gateway_profile_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM gateway_profile WHERE id = ANY(%s)",
                (list(gateway_profile_ids),),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def list_gateway_publication_audit(
        self, publication_id: UUID
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id, publication_id, from_status, to_status, actor, detail,
                          created_at
                   FROM gateway_publication_audit
                   WHERE publication_id = %s
                   ORDER BY created_at, id""",
                (publication_id,),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def effective_gateway_publication(
        self, gateway_profile_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT publication.*
                   FROM effective_gateway_release effective
                   JOIN gateway_publication publication
                     ON publication.id = effective.publication_id
                   WHERE effective.gateway_profile_id = %s""",
                (gateway_profile_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def get_gateway_release_protection(
        self, publication_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_release_protection WHERE publication_id = %s",
                (publication_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def list_gateway_release_protections(
        self, publication_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not publication_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_release_protection
                   WHERE publication_id = ANY(%s)""",
                (list(publication_ids),),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def list_gateway_release_protection_audit(
        self, publication_id: UUID
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_release_protection_audit
                   WHERE publication_id = %s ORDER BY created_at, id""",
                (publication_id,),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def set_gateway_release_protection(
        self,
        publication_id: UUID,
        *,
        pinned: bool,
        protected_label: str | None,
        retain_until: datetime | None,
        updated_by: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            if not pinned and protected_label is None and retain_until is None:
                connection.execute(
                    "DELETE FROM gateway_release_protection WHERE publication_id = %s",
                    (publication_id,),
                )
                row = None
            else:
                row = connection.execute(
                    """INSERT INTO gateway_release_protection (
                           publication_id, pinned, protected_label, retain_until, updated_by
                       ) VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (publication_id) DO UPDATE SET
                           pinned = EXCLUDED.pinned,
                           protected_label = EXCLUDED.protected_label,
                           retain_until = EXCLUDED.retain_until,
                           updated_by = EXCLUDED.updated_by,
                           updated_at = now()
                       RETURNING *""",
                    (
                        publication_id,
                        pinned,
                        protected_label,
                        retain_until,
                        updated_by,
                    ),
                ).fetchone()
            connection.execute(
                """INSERT INTO gateway_release_protection_audit (
                       publication_id, pinned, protected_label, retain_until, actor
                   ) VALUES (%s, %s, %s, %s, %s)""",
                (
                    publication_id,
                    pinned,
                    protected_label,
                    retain_until,
                    updated_by,
                ),
            )
        return cast(dict[str, Any], row)

    def create_gateway_release_operation(
        self,
        gateway_profile_id: UUID,
        operation_kind: str,
        created_by: str,
        *,
        target_release_id: UUID | None = None,
        prior_release_id: UUID | None = None,
        confirmation_sha256: str | None = None,
        semantic_preview: Mapping[str, Any] | None = None,
        credential_ciphertext: bytes | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"gateway-release-operation:{gateway_profile_id}",),
            )
            open_operation = connection.execute(
                """SELECT id FROM gateway_release_operation
                   WHERE gateway_profile_id = %s
                     AND status NOT IN ('succeeded', 'failed', 'restored')""",
                (gateway_profile_id,),
            ).fetchone()
            if open_operation is not None:
                raise ValueError("Another gateway release operation is already in progress")
            row = connection.execute(
                """INSERT INTO gateway_release_operation (
                       gateway_profile_id, operation_kind, target_release_id,
                       prior_release_id, confirmation_sha256, semantic_preview, created_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (
                    gateway_profile_id,
                    operation_kind,
                    target_release_id,
                    prior_release_id,
                    confirmation_sha256,
                    Jsonb(dict(semantic_preview or {})),
                    created_by,
                ),
            ).fetchone()
            connection.execute(
                """INSERT INTO gateway_release_operation_audit (
                       operation_id, from_status, to_status, actor
                   ) VALUES (%s, NULL, 'queued', %s)""",
                (row["id"], created_by),
            )
            if credential_ciphertext is not None:
                connection.execute(
                    """INSERT INTO gateway_release_operation_secret (
                           operation_id, credential_ciphertext
                       ) VALUES (%s, %s)""",
                    (row["id"], credential_ciphertext),
                )
        return cast(dict[str, Any], row)

    def gateway_release_operation_secret(self, operation_id: UUID) -> bytes | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT credential_ciphertext
                   FROM gateway_release_operation_secret
                   WHERE operation_id = %s""",
                (operation_id,),
            ).fetchone()
        return bytes(row["credential_ciphertext"]) if row is not None else None

    def delete_gateway_release_operation_secret(self, operation_id: UUID) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM gateway_release_operation_secret WHERE operation_id = %s",
                (operation_id,),
            )

    def get_gateway_release_operation(
        self, operation_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_release_operation WHERE id = %s",
                (operation_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def list_gateway_release_operations(
        self, gateway_profile_id: UUID, limit: int = 100
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_release_operation
                   WHERE gateway_profile_id = %s
                   ORDER BY created_at DESC, id DESC LIMIT %s""",
                (gateway_profile_id, limit),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def claim_gateway_release_operation(
        self, worker_id: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            operation = connection.execute(
                """SELECT * FROM gateway_release_operation
                   WHERE status NOT IN ('succeeded', 'failed', 'restored')
                     AND (lease_expires_at IS NULL OR lease_expires_at < now())
                   ORDER BY created_at, id
                   FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if operation is None:
                return None
            row = connection.execute(
                """UPDATE gateway_release_operation SET
                       lease_owner = %s,
                       lease_expires_at = now() + (%s * interval '1 second'),
                       attempt_count = attempt_count + 1,
                       updated_at = now()
                   WHERE id = %s RETURNING *""",
                (worker_id, lease_seconds, operation["id"]),
            ).fetchone()
        return cast(dict[str, Any], row)

    def transition_gateway_release_operation(
        self,
        operation_id: UUID,
        expected_status: str,
        status: str,
        updates: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any] | None:
        allowed_updates = {"checkpoint", "error_code", "error_message"}
        unexpected = set(updates) - allowed_updates
        if unexpected:
            raise ValueError(f"Unsupported release operation updates: {sorted(unexpected)}")
        assignments = [
            "status = %(status)s",
            "lease_owner = NULL",
            "lease_expires_at = NULL",
            "updated_at = now()",
        ]
        parameters: dict[str, Any] = {
            "id": operation_id,
            "expected": expected_status,
            "status": status,
            "actor": actor,
        }
        for key, value in updates.items():
            assignments.append(f"{key} = %({key})s")
            parameters[key] = Jsonb(value) if key == "checkpoint" else value
        if expected_status == "queued":
            assignments.append("started_at = COALESCE(started_at, now())")
        if status in {"succeeded", "failed", "restored"}:
            assignments.append("completed_at = now()")
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                f"""UPDATE gateway_release_operation SET {", ".join(assignments)}
                    WHERE id = %(id)s AND status = %(expected)s
                      AND lease_owner = %(actor)s AND lease_expires_at > now() RETURNING *""",
                parameters,
            ).fetchone()
            if row is not None:
                connection.execute(
                    """INSERT INTO gateway_release_operation_audit (
                           operation_id, from_status, to_status, actor, detail
                       ) VALUES (%s, %s, %s, %s, %s)""",
                    (
                        operation_id,
                        expected_status,
                        status,
                        actor,
                        Jsonb(dict(updates)),
                    ),
                )
                if (
                    row["operation_kind"] == "application_provision"
                    and status in {"succeeded", "failed", "restored"}
                ):
                    connection.execute(
                        "DELETE FROM gateway_release_operation_secret WHERE operation_id = %s",
                        (operation_id,),
                    )
        return cast(dict[str, Any] | None, row)

    def list_gateway_release_operation_audit(
        self, operation_id: UUID
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_release_operation_audit
                   WHERE operation_id = %s ORDER BY created_at, id""",
                (operation_id,),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def complete_gateway_release_rollback(
        self,
        operation_id: UUID,
        target_release_id: UUID,
        prior_release_id: UUID,
        actor: str,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            operation = connection.execute(
                """SELECT * FROM gateway_release_operation
                   WHERE id = %s AND lease_owner = %s AND lease_expires_at > now() FOR UPDATE""",
                (operation_id, actor),
            ).fetchone()
            if (
                operation is None
                or operation["status"] != "post_promotion_probing"
                or operation["lease_owner"] != actor
            ):
                raise ValueError("Rollback operation is not ready to complete")
            target = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = %s FOR UPDATE",
                (target_release_id,),
            ).fetchone()
            prior = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = %s FOR UPDATE",
                (prior_release_id,),
            ).fetchone()
            if target is None or prior is None:
                raise ValueError("Rollback release no longer exists")
            if (
                target["gateway_profile_id"] != operation["gateway_profile_id"]
                or prior["gateway_profile_id"] != operation["gateway_profile_id"]
            ):
                raise ValueError("Rollback releases belong to another gateway")
            effective = connection.execute(
                """SELECT publication_id FROM effective_gateway_release
                   WHERE gateway_profile_id = %s FOR UPDATE""",
                (operation["gateway_profile_id"],),
            ).fetchone()
            if effective is None or effective["publication_id"] != prior_release_id:
                raise ValueError("Effective gateway release changed during rollback")
            connection.execute(
                """UPDATE gateway_publication SET status = 'rolled_back', updated_at = now()
                   WHERE id = %s""",
                (prior_release_id,),
            )
            connection.execute(
                """UPDATE gateway_publication SET status = 'active', updated_at = now()
                   WHERE id = %s""",
                (target_release_id,),
            )
            connection.execute(
                """UPDATE effective_gateway_release
                   SET publication_id = %s, updated_at = now()
                   WHERE gateway_profile_id = %s""",
                (target_release_id, operation["gateway_profile_id"]),
            )
            detail = Jsonb({"operation_id": str(operation_id)})
            connection.execute(
                """INSERT INTO gateway_publication_audit (
                       publication_id, from_status, to_status, actor, detail
                   ) VALUES (%s, %s, 'active', %s, %s),
                            (%s, %s, 'rolled_back', %s, %s)""",
                (
                    target_release_id,
                    target["status"],
                    actor,
                    detail,
                    prior_release_id,
                    prior["status"],
                    actor,
                    detail,
                ),
            )
            row = connection.execute(
                """UPDATE gateway_release_operation SET status = 'succeeded',
                       lease_owner = NULL, lease_expires_at = NULL,
                       completed_at = now(), updated_at = now()
                   WHERE id = %s RETURNING *""",
                (operation_id,),
            ).fetchone()
            connection.execute(
                """INSERT INTO gateway_release_operation_audit (
                       operation_id, from_status, to_status, actor
                   ) VALUES (%s, 'post_promotion_probing', 'succeeded', %s)""",
                (operation_id, actor),
            )
        return cast(dict[str, Any], row)

    def save_gateway_release_integrity_snapshot(
        self,
        publication_id: UUID,
        operation_id: UUID | None,
        status: str,
        dependencies: Mapping[str, Any],
        issues: Sequence[str],
        checked_by: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """INSERT INTO gateway_release_integrity_snapshot (
                       publication_id, operation_id, status, dependencies, issues, checked_by
                   ) VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (
                    publication_id,
                    operation_id,
                    status,
                    Jsonb(dict(dependencies)),
                    Jsonb(list(issues)),
                    checked_by,
                ),
            ).fetchone()
        return cast(dict[str, Any], row)

    def latest_gateway_release_integrity_snapshot(
        self, publication_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM gateway_release_integrity_snapshot
                   WHERE publication_id = %s
                   ORDER BY checked_at DESC, id DESC LIMIT 1""",
                (publication_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def latest_gateway_release_integrity_snapshots(
        self, publication_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not publication_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT ON (publication_id) *
                   FROM gateway_release_integrity_snapshot
                   WHERE publication_id = ANY(%s)
                   ORDER BY publication_id, checked_at DESC, id DESC""",
                (list(publication_ids),),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def save_gateway_release_gc_plan(
        self,
        operation_id: UUID,
        gateway_profile_id: UUID,
        retained_release_ids: Sequence[UUID],
        current_non_release_references: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        reference_graph_sha256: str,
        created_by: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """INSERT INTO gateway_release_gc_plan (
                       operation_id, gateway_profile_id, retained_release_ids,
                       current_non_release_references, candidates,
                       reference_graph_sha256, created_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (
                    operation_id,
                    gateway_profile_id,
                    Jsonb([str(value) for value in retained_release_ids]),
                    Jsonb(dict(current_non_release_references)),
                    Jsonb([dict(value) for value in candidates]),
                    reference_graph_sha256,
                    created_by,
                ),
            ).fetchone()
        return cast(dict[str, Any], row)

    def get_gateway_release_gc_plan(
        self, operation_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_release_gc_plan WHERE operation_id = %s",
                (operation_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def claim_gateway_publication(
        self, worker_id: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            item = connection.execute(
                """SELECT outbox.id, outbox.publication_id
                   FROM gateway_publication_outbox outbox
                   JOIN gateway_publication publication ON publication.id = outbox.publication_id
                   WHERE (
                       outbox.status = 'queued' AND outbox.available_at <= now()
                   ) OR (
                       outbox.status = 'leased' AND outbox.lease_expires_at < now()
                   )
                   ORDER BY outbox.id
                   FOR UPDATE OF outbox SKIP LOCKED
                   LIMIT 1"""
            ).fetchone()
            if item is None:
                return None
            connection.execute(
                """UPDATE gateway_publication_outbox
                   SET status = 'leased', lease_owner = %s,
                       lease_expires_at = now() + (%s * interval '1 second'),
                       attempts = attempts + 1, updated_at = now()
                   WHERE id = %s""",
                (worker_id, lease_seconds, item["id"]),
            )
            row = connection.execute(
                """UPDATE gateway_publication
                   SET attempt_count = attempt_count + 1,
                       started_at = COALESCE(started_at, now()), updated_at = now()
                   WHERE id = %s RETURNING *""",
                (item["publication_id"],),
            ).fetchone()
        return cast(dict[str, Any], row)

    def cancel_unstarted_gateway_publication(
        self, publication_id: UUID, actor: str
    ) -> dict[str, Any] | None:
        detail = {
            "error_code": "cancelled_by_owner",
            "error_message": "Publication cancelled before processing",
        }
        with self._connection() as connection, connection.transaction():
            outbox = connection.execute(
                """SELECT id, status FROM gateway_publication_outbox
                   WHERE publication_id = %s FOR UPDATE""",
                (publication_id,),
            ).fetchone()
            if outbox is None or outbox["status"] != "queued":
                return None
            row = connection.execute(
                """UPDATE gateway_publication
                   SET status = 'rolled_back', error_code = %s, error_message = %s,
                       completed_at = now(), updated_at = now()
                   WHERE id = %s AND status = 'queued' AND attempt_count = 0
                         AND started_at IS NULL
                   RETURNING *""",
                (
                    detail["error_code"],
                    detail["error_message"],
                    publication_id,
                ),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "DELETE FROM gateway_publication_secret WHERE publication_id = %s",
                (publication_id,),
            )
            connection.execute(
                """UPDATE gateway_publication_outbox
                   SET status = 'completed', lease_owner = NULL,
                       lease_expires_at = NULL, available_at = now(), updated_at = now()
                   WHERE id = %s""",
                (outbox["id"],),
            )
            connection.execute(
                """INSERT INTO gateway_publication_audit (
                       publication_id, from_status, to_status, actor, detail
                   ) VALUES (%s, 'queued', 'rolled_back', %s, %s)""",
                (publication_id, actor, Jsonb(detail)),
            )
        return cast(dict[str, Any], row)

    def transition_gateway_publication(
        self,
        publication_id: UUID,
        expected_status: str,
        status: str,
        updates: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any] | None:
        allowed_updates = {
            "apim_revision",
            "policy_sha256",
            "resource_manifest",
            "error_code",
            "error_message",
        }
        unexpected = set(updates) - allowed_updates
        if unexpected:
            raise ValueError(f"Unsupported publication updates: {sorted(unexpected)}")
        assignments = ["status = %(status)s", "updated_at = now()"]
        parameters: dict[str, Any] = {
            "id": publication_id,
            "expected": expected_status,
            "status": status,
        }
        for key, value in updates.items():
            assignments.append(f"{key} = %({key})s")
            parameters[key] = Jsonb(value) if key == "resource_manifest" else value
        if status in {"active", "failed", "rolled_back"}:
            assignments.append("completed_at = now()")
        with self._connection() as connection, connection.transaction():
            outbox = connection.execute(
                """SELECT id FROM gateway_publication_outbox WHERE publication_id = %s
                         AND status = 'leased' AND lease_owner = %s
                         AND lease_expires_at > now() FOR UPDATE""",
                (publication_id, actor),
            ).fetchone()
            if outbox is None and expected_status not in {"failed", "awaiting_authorization"}:
                return None
            row = connection.execute(
                f"""UPDATE gateway_publication SET {', '.join(assignments)}
                    WHERE id = %(id)s AND status = %(expected)s RETURNING *""",
                parameters,
            ).fetchone()
            if row is None:
                return None
            terminal = status in {"active", "failed", "rolled_back"}
            paused = status == "awaiting_authorization"
            if terminal:
                connection.execute(
                    "DELETE FROM gateway_publication_secret WHERE publication_id = %s",
                    (publication_id,),
                )
            connection.execute(
                """UPDATE gateway_publication_outbox
                   SET status = %s, lease_owner = NULL, lease_expires_at = NULL,
                       available_at = now(), updated_at = now()
                   WHERE publication_id = %s AND status IN ('queued', 'leased')""",
                ("completed" if terminal or paused else "queued", publication_id),
            )
            connection.execute(
                """INSERT INTO gateway_publication_audit (
                       publication_id, from_status, to_status, actor, detail
                   ) VALUES (%s, %s, %s, %s, %s)""",
                (publication_id, expected_status, status, actor, Jsonb(dict(updates))),
            )
        return cast(dict[str, Any], row)

    def renew_gateway_publication_lease(
        self,
        publication_id: UUID,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        with self._connection() as connection:
            return (
                connection.execute(
                    """UPDATE gateway_publication_outbox
                   SET lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
                   WHERE publication_id = %s AND status = 'leased' AND lease_owner = %s
                     AND lease_expires_at > now() RETURNING id""",
                    (lease_seconds, publication_id, worker_id),
                ).fetchone()
                is not None
            )

    def renew_gateway_release_operation_lease(
        self,
        operation_id: UUID,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        with self._connection() as connection:
            return (
                connection.execute(
                    """UPDATE gateway_release_operation
                   SET lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
                   WHERE id = %s AND lease_owner = %s AND lease_expires_at > now() RETURNING id""",
                    (lease_seconds, operation_id, worker_id),
                ).fetchone()
                is not None
            )

    def activate_gateway_publication(
        self, publication_id: UUID, actor: str
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            outbox = connection.execute(
                """SELECT id FROM gateway_publication_outbox WHERE publication_id = %s
                         AND status = 'leased' AND lease_owner = %s
                         AND lease_expires_at > now() FOR UPDATE""",
                (publication_id, actor),
            ).fetchone()
            publication = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = %s FOR UPDATE",
                (publication_id,),
            ).fetchone()
            if publication is None:
                raise ValueError("Unknown gateway publication")
            if publication["status"] == "active":
                return cast(dict[str, Any], publication)
            if outbox is None:
                raise ValueError("Publication activation requires the current worker lease")
            if publication["status"] != "promoting":
                raise ValueError("Only a promoted gateway publication can be activated")
            if publication["publication_kind"] == "model_remove":
                target = dict(publication["desired_spec"])["removed_models"][0]
                model_id = UUID(str(target["model_id"]))
                model = connection.execute(
                    "SELECT id, model_key, is_default FROM managed_model WHERE id = %s FOR UPDATE",
                    (model_id,),
                ).fetchone()
                if model is None:
                    raise ValueError("The model removal target no longer exists")
                if str(model["model_key"]).casefold() != str(target["model_key"]).casefold():
                    raise ValueError("The model removal target changed before activation")
                if model["is_default"]:
                    raise ValueError("The default model cannot be deleted")
                affected_users = connection.execute(
                    "SELECT user_id FROM user_model_access WHERE model_id = %s ORDER BY user_id",
                    (model_id,),
                ).fetchall()
                for affected in affected_users:
                    user_id = str(affected["user_id"])
                    previous = [
                        row["model_id"]
                        for row in connection.execute(
                            """SELECT model_id FROM user_model_access
                               WHERE user_id = %s ORDER BY model_id""",
                            (user_id,),
                        ).fetchall()
                    ]
                    current = [value for value in previous if value != model_id]
                    connection.execute(
                        """UPDATE user_model_policy SET updated_at = now(), updated_by = %s
                           WHERE user_id = %s""",
                        (publication["created_by"], user_id),
                    )
                    connection.execute(
                        """INSERT INTO user_model_access_audit (
                               id, user_id, previous_model_ids, new_model_ids, changed_by
                           ) VALUES (gen_random_uuid(), %s, %s, %s, %s)""",
                        (
                            user_id,
                            previous,
                            current,
                            publication["created_by"],
                        ),
                    )
                connection.execute(
                    """UPDATE assistant_setting SET model_id = NULL, updated_at = now(),
                           updated_by = %s WHERE model_id = %s""",
                    (publication["created_by"], model_id),
                )
                connection.execute("DELETE FROM managed_model WHERE id = %s", (model_id,))
                row = self._complete_gateway_activation(
                    connection, publication, publication_id, actor
                )
                self._invalidate_identities()
                return row
            if publication["publication_kind"] == "route_reconcile" or (
                publication["publication_kind"] == "credential_rotation"
                and publication["desired_spec"]["bindings"][-1]["auth_strategy"]
                == "oauth_client_credentials"
            ):
                return self._complete_gateway_activation(
                    connection, publication, publication_id, actor
                )
            binding = dict(publication["desired_spec"])["bindings"][-1]
            if publication["publication_kind"] == "credential_rotation":
                runtime = connection.execute(
                    """SELECT * FROM model_runtime
                       WHERE gateway_profile_id = %s
                       AND (id = %s::uuid OR (%s::uuid IS NULL AND lower(name) = lower(%s)))
                       FOR UPDATE""",
                    (
                        publication["gateway_profile_id"],
                        binding.get("runtime_id"),
                        binding.get("runtime_id"),
                        binding["runtime_name"],
                    ),
                ).fetchone()
                if runtime is None:
                    raise ValueError("The managed runtime no longer exists")
                runtime_config = dict(runtime["config"] or {})
                if not runtime_config.get("control_plane_managed"):
                    raise ValueError("The runtime is not managed by the control plane")
                runtime_config["named_value_name"] = binding["named_value_name"]
                connection.execute(
                    "UPDATE model_runtime SET config = %s, updated_at = now() WHERE id = %s",
                    (Jsonb(runtime_config), runtime["id"]),
                )
                row = self._complete_gateway_activation(
                    connection, publication, publication_id, actor
                )
                self._invalidate_identities()
                return row
            model_spec = binding["model"]

            if binding.get("provider_id"):
                provider_id = UUID(binding["provider_id"])
                provider = connection.execute(
                    "SELECT * FROM model_provider WHERE id = %s", (provider_id,)
                ).fetchone()
                if provider is None or not provider["enabled"]:
                    raise ValueError("The selected provider no longer exists")
            else:
                provider = connection.execute(
                    """INSERT INTO model_provider (
                           name, provider_kind, auth_type, enabled, config
                       ) VALUES (%s, %s, 'none', TRUE, %s)
                       ON CONFLICT (name) DO UPDATE SET updated_at = model_provider.updated_at
                       RETURNING *""",
                    (
                        binding["provider_name"],
                        binding["provider_kind"],
                        Jsonb(binding.get("provider_config") or {}),
                    ),
                ).fetchone()
                provider_id = provider["id"]
                if (
                    not provider["enabled"]
                    or provider["provider_kind"] != binding["provider_kind"]
                    or dict(provider["config"] or {})
                    != dict(binding.get("provider_config") or {})
                ):
                    raise ValueError(
                        "A provider with this name exists with incompatible settings"
                    )
                connection.execute(
                    """INSERT INTO model_provider_metadata (provider_id, brand_key)
                       VALUES (%s, %s)
                       ON CONFLICT (provider_id) DO UPDATE SET
                         brand_key = EXCLUDED.brand_key""",
                    (provider_id, binding["provider_brand_key"]),
                )

            if binding.get("runtime_id"):
                runtime_id = UUID(binding["runtime_id"])
                runtime = connection.execute(
                    "SELECT * FROM model_runtime WHERE id = %s", (runtime_id,)
                ).fetchone()
                if (
                    runtime is None
                    or not runtime["enabled"]
                    or runtime["provider_id"] != provider_id
                    or runtime["gateway_profile_id"]
                    != publication["gateway_profile_id"]
                ):
                    raise ValueError("The selected runtime no longer belongs to the provider")
                runtime_config = dict(runtime["config"] or {})
                binding_config = dict(binding.get("runtime_config") or {})
                if (
                    runtime_config.get("credential_provisioned") is False
                    and binding_config.get("credential_provisioned") is True
                ):
                    runtime_config["credential_provisioned"] = True
                    connection.execute(
                        "UPDATE model_runtime SET config = %s, updated_at = now() WHERE id = %s",
                        (Jsonb(runtime_config), runtime_id),
                    )
            else:
                runtime_config = _activation_runtime_config(binding)
                runtime = connection.execute(
                    """INSERT INTO model_runtime (
                           provider_id, gateway_profile_id, name, runtime_kind,
                           enabled, is_default, config
                       ) VALUES (%s, %s, %s, %s, TRUE, FALSE, %s)
                       ON CONFLICT (name) DO UPDATE SET updated_at = model_runtime.updated_at
                       RETURNING *""",
                    (
                        provider_id,
                        publication["gateway_profile_id"],
                        binding["runtime_name"],
                        binding["runtime_kind"],
                        Jsonb(runtime_config),
                    ),
                ).fetchone()
                runtime_id = runtime["id"]
                observed_config = dict(runtime["config"] or {})
                if (
                    not runtime["enabled"]
                    or runtime["provider_id"] != provider_id
                    or runtime["gateway_profile_id"]
                    != publication["gateway_profile_id"]
                    or runtime["runtime_kind"] != binding["runtime_kind"]
                    or not observed_config.get("control_plane_managed")
                    or str(observed_config.get("backend_url", "")).rstrip("/")
                    != str(runtime_config.get("backend_url") or "").rstrip("/")
                ):
                    raise ValueError(
                        "A runtime with this name exists with incompatible settings"
                    )
                connection.execute(
                    """INSERT INTO model_runtime_metadata (runtime_id, brand_key)
                       VALUES (%s, %s)
                       ON CONFLICT (runtime_id) DO UPDATE SET
                         brand_key = EXCLUDED.brand_key""",
                    (runtime_id, binding["runtime_brand_key"]),
                )

            existing_model = connection.execute(
                """SELECT model.id, metadata.publication_id
                   FROM managed_model model
                   LEFT JOIN managed_model_metadata metadata
                     ON metadata.model_id = model.id
                   WHERE lower(model.model_key) = lower(%s)""",
                (model_spec["model_key"],),
            ).fetchone()
            if existing_model is not None and existing_model["publication_id"] != publication_id:
                raise ValueError("The model alias is already active")
            if existing_model is None:
                model = connection.execute(
                    """INSERT INTO managed_model (
                           id, provider_id, runtime_id, model_key, display_name,
                              enabled, is_default, capabilities, context_window,
                              input_cost_per_million,
                           output_cost_per_million, cached_cost_per_million,
                           cache_write_cost_per_million, allowed_roles
                       ) VALUES (
                           %s, %s, %s, %s, %s, TRUE, FALSE, %s, %s,
                           %s, %s, %s, %s, %s
                       ) RETURNING id""",
                    (
                        publication_model_id(publication_id, model_spec["model_key"]),
                        provider_id,
                        runtime_id,
                        model_spec["model_key"],
                        model_spec["display_name"],
                        model_spec["capabilities"],
                        model_spec.get("context_window"),
                        model_spec.get("input_cost_per_million"),
                        model_spec.get("output_cost_per_million"),
                        model_spec.get("cached_cost_per_million"),
                        model_spec.get("cache_write_cost_per_million"),
                        model_spec["allowed_roles"],
                    ),
                ).fetchone()
                connection.execute(
                    """INSERT INTO managed_model_metadata (
                           model_id, family_key, upstream_model_id,
                           assignment_required, publication_id
                       ) VALUES (%s, %s, %s, %s, %s)""",
                    (
                        model["id"],
                        model_spec["family_key"],
                        model_spec["upstream_model_id"],
                        model_spec.get("assignment_required", True),
                        publication_id,
                    ),
                )
                if pricing := model_spec.get("price_configuration"):
                    from .repository_registry import PostgreSqlRegistryRepositoryMixin

                    PostgreSqlRegistryRepositoryMixin._write_price(
                        connection, "model", model["id"], pricing,
                    )
            row = self._complete_gateway_activation(
                connection, publication, publication_id, actor
            )
        self._invalidate_identities()
        return row

    @staticmethod
    def _complete_gateway_activation(
        connection: Any,
        publication: Mapping[str, Any],
        publication_id: UUID,
        actor: str,
    ) -> dict[str, Any]:
        previous = connection.execute(
            """SELECT publication_id FROM effective_gateway_release
               WHERE gateway_profile_id = %s""",
            (publication["gateway_profile_id"],),
        ).fetchone()
        if previous is not None and previous["publication_id"] != publication_id:
            connection.execute(
                """UPDATE gateway_publication SET status = 'superseded', updated_at = now()
                   WHERE id = %s AND status = 'active'""",
                (previous["publication_id"],),
            )
        connection.execute(
            """INSERT INTO effective_gateway_release (gateway_profile_id, publication_id)
               VALUES (%s, %s)
               ON CONFLICT (gateway_profile_id) DO UPDATE SET
                   publication_id = EXCLUDED.publication_id, updated_at = now()""",
            (publication["gateway_profile_id"], publication_id),
        )
        row = connection.execute(
            """UPDATE gateway_publication SET status = 'active', completed_at = now(),
                   updated_at = now(), error_code = NULL, error_message = NULL
               WHERE id = %s RETURNING *""",
            (publication_id,),
        ).fetchone()
        connection.execute(
            """UPDATE gateway_publication_outbox SET status = 'completed',
                   lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
               WHERE publication_id = %s""",
            (publication_id,),
        )
        connection.execute(
            """INSERT INTO gateway_publication_audit (
                   publication_id, from_status, to_status, actor
               ) VALUES (%s, 'promoting', 'active', %s)""",
            (publication_id, actor),
        )
        return cast(dict[str, Any], row)

    def invocation_route(
        self, runtime_id: UUID | None, model_id: UUID | None
    ) -> dict[str, Any] | None:
        conditions = [
            "model.enabled",
            "runtime.enabled",
            "provider.enabled",
            "(gateway.id IS NULL OR gateway.enabled)",
        ]
        parameters: list[Any] = []
        if model_id is not None:
            conditions.append("model.id = %s")
            parameters.append(model_id)
        elif runtime_id is not None:
            conditions.append("runtime.id = %s")
            parameters.append(runtime_id)
        else:
            conditions.extend(["model.is_default", "runtime.is_default"])
        if runtime_id is not None and model_id is not None:
            conditions.append("runtime.id = %s")
            parameters.append(runtime_id)
        with self._connection() as connection:
            row = connection.execute(
                f"""SELECT
                    model.id AS model_id, model.model_key, model.display_name,
                    model.capabilities AS model_capabilities,
                    binding.value->'model'->'image_profile' AS image_profile,
                                        COALESCE(model_metadata.family_key, 'generic')
                                            AS model_family_key,
                                        COALESCE(model_metadata.upstream_model_id, model.model_key)
                                            AS upstream_model_id,
                                        COALESCE(model_metadata.assignment_required, FALSE)
                                            AS assignment_required,
                    model.input_cost_per_million, model.output_cost_per_million,
                    model.cached_cost_per_million, model.cache_write_cost_per_million,
                    model.allowed_roles AS model_allowed_roles,
                    runtime.id AS runtime_id, runtime.name AS runtime_name,
                    runtime.runtime_kind, runtime.config AS runtime_config,
                    runtime.allowed_roles AS runtime_allowed_roles,
                    provider.id AS provider_id, provider.name AS provider_name,
                    provider.provider_kind,
                    provider.endpoint_url AS provider_endpoint_url,
                    provider.auth_type AS provider_auth_type,
                    provider.credential_ciphertext AS provider_credential_ciphertext,
                    gateway.id AS gateway_id, gateway.name AS gateway_name,
                    gateway.implementation AS gateway_implementation,
                    gateway.base_url AS gateway_base_url,
                    gateway.auth_type AS gateway_auth_type,
                    gateway.credential_ciphertext AS gateway_credential_ciphertext,
                    gateway.config AS gateway_config
                FROM managed_model model
                JOIN model_runtime runtime ON runtime.id = model.runtime_id
                JOIN model_provider provider ON provider.id = model.provider_id
                                LEFT JOIN managed_model_metadata model_metadata
                                    ON model_metadata.model_id = model.id
                LEFT JOIN gateway_profile gateway ON gateway.id = runtime.gateway_profile_id
                LEFT JOIN effective_gateway_release effective
                    ON effective.gateway_profile_id = runtime.gateway_profile_id
                LEFT JOIN gateway_publication publication
                    ON publication.id = effective.publication_id
                LEFT JOIN LATERAL (
                    SELECT value FROM jsonb_array_elements(
                        COALESCE(publication.desired_spec->'bindings', '[]'::jsonb)
                    ) binding
                    WHERE lower(value->'model'->>'model_key') = lower(model.model_key)
                    LIMIT 1
                ) binding ON TRUE
                WHERE {" AND ".join(conditions)}
                ORDER BY model.is_default DESC, model.created_at
                LIMIT 1""",
                parameters,
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def update_runtime_health(
        self, runtime_id: UUID, status: str, message: str, checked_at: datetime
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE model_runtime SET health_status=%s, health_message=%s,
                   last_checked_at=%s, updated_at=now() WHERE id=%s""",
                (status, message, checked_at, runtime_id),
            )
