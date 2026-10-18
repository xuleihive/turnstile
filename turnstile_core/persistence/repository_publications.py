from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ..domain.control_plane import (
    GatewayPublication,
    GatewayPublicationIntent,
    publication_credential_evidence,
    publication_materialized_named_values,
    publication_model_id,
    publication_provisioning_completed,
)
from ..domain.gateway_targets import apim_target_route_binding_id_for
from .repository_support import (
    ModelActivationValidator,
    _activated_runtime_credentials,
    _activation_runtime_config,
    _configuration_rollback_values,
    _fixed_program_upgrade_config,
    _managed_model_request_revision,
    _managed_program_initialization_config,
    _managed_program_publication,
    _model_addition_credential_retry,
    _model_addition_manifest,
    _model_removal_manifest,
    _program_migration_manifest,
    _program_migration_retry,
    _require_program_migration_activation,
    _validate_gateway_deployment_snapshots,
)


class PostgreSqlPublicationRepositoryMixin:
    @staticmethod
    def _validate_publication_deployments(
        connection: Any, gateway_profile_id: UUID, desired_spec: Mapping[str, Any],
    ) -> None:
        snapshots = desired_spec.get("gateway_deployments") or ()
        if snapshots:
            rows = connection.execute(
                """SELECT id, apim_target_id, native_gateway_id, endpoint_url, retired_at
                   FROM gateway_deployment WHERE id = ANY(%s)""",
                ([UUID(str(item["deployment_id"])) for item in snapshots],),
            ).fetchall()
            _validate_gateway_deployment_snapshots(gateway_profile_id, desired_spec, rows)

    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def _invalidate_identities(self) -> None:
        raise NotImplementedError

    def record_fixed_route_installation(self, publication: GatewayPublication) -> dict[str, Any]:
        publication = GatewayPublication.model_validate(publication)
        publication.require_empty_fixed_installation()
        with self._connection() as connection, connection.transaction():
            return self._record_fixed_route_installation(connection, publication)

    @staticmethod
    def _record_fixed_route_installation(
        connection: Any, publication: GatewayPublication,
    ) -> dict[str, Any]:
        publication.require_empty_fixed_installation()
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"gateway-publication:{publication.gateway_profile_id}",),
        )
        current = connection.execute(
            "SELECT * FROM gateway_publication WHERE gateway_profile_id = %s FOR UPDATE",
            (publication.gateway_profile_id,),
        ).fetchone()
        if current is not None:
            recorded = GatewayPublication.model_validate(current)
            effective = connection.execute(
                "SELECT publication_id FROM effective_gateway_release "
                "WHERE gateway_profile_id = %s",
                (publication.gateway_profile_id,),
            ).fetchone()
            if recorded != publication or effective is None or effective["publication_id"] != (
                publication.id
            ):
                raise ValueError("Fixed installation cannot replace an existing publication")
            return dict(current)
        gateway = connection.execute(
            """SELECT profile.id FROM gateway_profile profile
               JOIN apim_target_metadata target ON target.gateway_profile_id = profile.id
               WHERE profile.id = %s AND profile.enabled AND profile.implementation = 'apim'
               FOR UPDATE OF profile, target""",
            (publication.gateway_profile_id,),
        ).fetchone()
        if gateway is None:
            raise ValueError("Fixed installation requires an enabled registered APIM target")
        inserted = connection.execute(
            """INSERT INTO gateway_publication (
                id, gateway_profile_id, generation, publication_kind, desired_spec,
                desired_spec_sha256, status, base_release_id, apim_revision, policy_sha256,
                resource_manifest, created_by, created_at, started_at, completed_at, updated_at
            ) VALUES (%s,%s,1,'route_reconcile',%s,%s,'active',NULL,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *""",
            (publication.id, publication.gateway_profile_id,
             Jsonb(publication.desired_spec.model_dump(mode="json")),
             publication.desired_spec_sha256, publication.apim_revision,
             publication.policy_sha256, Jsonb(publication.resource_manifest),
             publication.created_by, publication.created_at, publication.started_at,
             publication.completed_at, publication.updated_at),
        ).fetchone()
        connection.execute(
            "INSERT INTO effective_gateway_release (gateway_profile_id, publication_id) "
            "VALUES (%s,%s)",
            (publication.gateway_profile_id, publication.id),
        )
        connection.execute(
            """INSERT INTO gateway_publication_audit (
                publication_id, from_status, to_status, actor, detail
            ) VALUES (%s,NULL,'active',%s,%s)""",
            (publication.id, publication.created_by,
             Jsonb({"kind": "fixed_program_installation",
                    "program_sha256": publication.policy_sha256})),
        )
        if inserted is None or GatewayPublication.model_validate(inserted) != publication:
            raise ValueError("Fixed installation was not recorded exactly")
        return dict(inserted)

    def create_gateway_publication(
        self,
        gateway_profile_id: UUID,
        desired_spec: Mapping[str, Any],
        desired_spec_sha256: str,
        publication_kind: str,
        created_by: str,
        credential_ciphertext: bytes | None = None,
        expected_base_release_id: UUID | None = None,
        initial_manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        GatewayPublicationIntent.model_validate({
            "gateway_profile_id": gateway_profile_id,
            "publication_kind": publication_kind,
            "desired_spec": desired_spec,
        })
        model_addition = initial_manifest is not None and set(initial_manifest) == {
            "model_route_addition",
        }
        model_removal = initial_manifest is not None and set(initial_manifest) == {
            "model_route_removal",
        }
        if model_removal and (publication_kind != "model_remove"
                              or expected_base_release_id is None
                              or credential_ciphertext is not None):
            raise ValueError("Model removal requires its current base without credentials")
        if model_addition and (publication_kind != "model_add" or expected_base_release_id is None):
            raise ValueError("Model addition requires its current program base")
        if initial_manifest is not None and not model_addition and not model_removal and (
            publication_kind != "route_reconcile" or credential_ciphertext is not None
            or expected_base_release_id is None
        ):
            raise ValueError("Program migration requires a new route reconcile without credentials")
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"gateway-publication:{gateway_profile_id}",),
            )
            gateway = connection.execute(
                     """SELECT id, enabled, implementation, config FROM gateway_profile
                         WHERE id = %s FOR SHARE""",
                (gateway_profile_id,),
            ).fetchone()
            if (
                gateway is None
                or gateway["implementation"] != "apim"
                or (not gateway["enabled"] and publication_kind != "model_remove")
            ):
                raise ValueError("The selected APIM gateway is unavailable")
            if publication_kind == "route_reconcile" or expected_base_release_id is not None:
                effective = connection.execute(
                    """SELECT publication_id FROM effective_gateway_release
                       WHERE gateway_profile_id = %s""",
                    (gateway_profile_id,),
                ).fetchone()
                effective_id = effective["publication_id"] if effective else None
                if expected_base_release_id is None or effective_id != expected_base_release_id:
                    raise ValueError("The effective gateway release changed; reconcile again")
            manifest: dict[str, Any] = {}
            if initial_manifest is not None:
                previous = connection.execute(
                    "SELECT * FROM gateway_publication WHERE id = %s FOR UPDATE",
                    (expected_base_release_id,),
                ).fetchone()
                if model_addition:
                    previous = _managed_program_publication(previous, gateway.get("config") or {})
                validate_manifest = _model_addition_manifest if model_addition else (
                    _program_migration_manifest
                )
                if model_removal:
                    marker = initial_manifest["model_route_removal"]
                    source = connection.execute(
                        "SELECT * FROM gateway_publication WHERE id = %s FOR SHARE",
                        (UUID(marker["source_publication_id"]),),
                    ).fetchone()
                    manifest = _model_removal_manifest(
                        previous, source, desired_spec, desired_spec_sha256, initial_manifest,
                    )
                else:
                    manifest = validate_manifest(
                        previous, desired_spec, desired_spec_sha256, initial_manifest,
                    )
            existing = connection.execute(
                """SELECT * FROM gateway_publication
                                     WHERE gateway_profile_id = %s AND desired_spec_sha256 = %s
                                         AND status NOT IN ('superseded', 'rolled_back')
                                     ORDER BY generation DESC
                                     LIMIT 1""",
                (gateway_profile_id, desired_spec_sha256),
            ).fetchone()
            if existing is not None and model_addition and existing["status"] == "active":
                removed = connection.execute(
                    """SELECT id FROM gateway_publication
                       WHERE gateway_profile_id = %s AND publication_kind = 'model_remove'
                         AND status = 'active'
                         AND resource_manifest->'model_route_removal'->>'source_publication_id' = %s
                         AND resource_manifest->'route_activation'->>'outcome' = 'confirmed'
                         AND NOT EXISTS (
                             SELECT 1 FROM apim_target_route_binding
                             WHERE apim_target_id = %s AND current_publication_id = %s
                         )
                       ORDER BY generation DESC LIMIT 1""",
                    (gateway_profile_id, str(existing["id"]), gateway_profile_id, existing["id"]),
                ).fetchone()
                if removed is not None:
                    connection.execute(
                        "UPDATE gateway_publication SET status = 'superseded', updated_at = now() "
                        "WHERE id = %s AND status = 'active'",
                        (existing["id"],),
                    )
                    connection.execute(
                        """INSERT INTO gateway_publication_audit (
                               publication_id, from_status, to_status, actor, detail
                           ) VALUES (%s,'active','superseded',%s,%s)""",
                        (existing["id"], created_by, Jsonb({
                            "kind": "confirmed_model_removal",
                            "removal_publication_id": str(removed["id"]),
                        })),
                    )
                    existing = None
            if (existing is not None and (model_addition or model_removal)
                    and existing["status"] == "failed"
                    and existing.get("base_release_id") == expected_base_release_id
                    and existing.get("desired_spec") == dict(desired_spec)
                    and existing.get("resource_manifest") == manifest
                    and existing.get("apim_revision") is None
                    and existing.get("policy_sha256") is None):
                effects = connection.execute(
                    """SELECT (
                           SELECT count(*) FROM gateway_publication_audit
                           WHERE publication_id = %s AND detail ? 'probe_request'
                       ) + (
                           SELECT count(*) FROM billable_request_attempt
                           WHERE scope_type = 'system' AND scope_id = %s
                       ) AS total, (
                           SELECT count(*) FROM gateway_publication_outbox
                           WHERE publication_id = %s AND status = 'completed'
                               AND lease_owner IS NULL AND lease_expires_at IS NULL
                       ) AS completed_outboxes""",
                    (existing["id"], f"publication:{existing['id']}", existing["id"]),
                ).fetchone()
                if (effects is not None and effects["total"] == 0
                        and effects["completed_outboxes"] == 1):
                    existing = None
            if existing is not None:
                if initial_manifest is not None and not model_addition and not model_removal and (
                    existing["status"] == "failed"
                    or existing["id"] != UUID(manifest["fixed_route_migration"]["request_id"])
                    or existing["resource_manifest"].get("fixed_route_migration")
                    != manifest["fixed_route_migration"]
                ):
                    raise ValueError("Program migration requires its original active intent")
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
            self._validate_publication_deployments(connection, gateway_profile_id, desired_spec)
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
                       publication_kind, base_release_id, created_by, resource_manifest, id
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING RETURNING *""",
                (
                    gateway_profile_id,
                    generation,
                    Jsonb(dict(desired_spec)),
                    desired_spec_sha256,
                    publication_kind,
                    base["publication_id"] if base else None,
                    created_by,
                    Jsonb(manifest),
                    UUID(manifest["fixed_route_migration"]["request_id"])
                    if "fixed_route_migration" in manifest else uuid4(),
                ),
            ).fetchone()
            if row is None:
                raise ValueError("The publication request ID is already used by another intent")
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
                       publication_id, from_status, to_status, actor, detail
                   ) VALUES (%s, NULL, 'queued', %s, %s)""",
                (row["id"], created_by, Jsonb(manifest)),
            )
        return cast(dict[str, Any], row)

    def create_multi_target_rollout(
        self,
        rollout_id: UUID,
        rollout_kind: str,
        logical_model_id: UUID | None,
        request_sha256: str,
        created_by: str,
        children: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if len(children) < 2:
            raise ValueError("A multi-target rollout requires at least two children")
        if rollout_kind in {"model_add", "model_remove"} and logical_model_id is None:
            raise ValueError("A model rollout requires a logical model identity")
        target_ids = [UUID(str(child["gateway_profile_id"])) for child in children]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("A multi-target rollout cannot repeat an APIM target")
        for child, target_id in zip(children, target_ids, strict=True):
            GatewayPublicationIntent.model_validate({
                "gateway_profile_id": target_id,
                "publication_kind": rollout_kind,
                "desired_spec": child["desired_spec"],
            })
        with self._connection() as connection, connection.transaction():
            for target_id in sorted(target_ids, key=str):
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"gateway-publication:{target_id}",),
                )
            existing_rollout = connection.execute(
                "SELECT * FROM multi_target_rollout WHERE request_sha256 = %s",
                (request_sha256,),
            ).fetchone()
            if existing_rollout is not None:
                return self._multi_target_rollout_view(
                    connection,
                    existing_rollout,
                )
            for child, target_id in zip(children, target_ids, strict=True):
                target = connection.execute(
                    """SELECT gateway.id, gateway.enabled, gateway.implementation
                         FROM gateway_profile gateway
                         JOIN apim_target_metadata target
                           ON target.gateway_profile_id = gateway.id
                        WHERE gateway.id = %s""",
                    (target_id,),
                ).fetchone()
                if (
                    target is None
                    or target["implementation"] != "apim"
                    or (not target["enabled"] and rollout_kind != "model_remove")
                ):
                    raise ValueError("The selected APIM target is unavailable")
                duplicate = connection.execute(
                    """SELECT id FROM gateway_publication
                        WHERE gateway_profile_id = %s
                          AND desired_spec_sha256 = %s
                          AND status NOT IN ('superseded', 'rolled_back')
                        LIMIT 1""",
                    (target_id, str(child["desired_spec_sha256"])),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError(
                        "A target already has this desired release; use its existing publication"
                    )
                in_flight = connection.execute(
                    """SELECT id FROM gateway_publication
                        WHERE gateway_profile_id = %s
                          AND status IN (
                            'queued', 'validating', 'provisioning',
                            'building_revision', 'verifying',
                            'awaiting_authorization', 'promoting', 'rolling_back'
                          )
                        LIMIT 1""",
                    (target_id,),
                ).fetchone()
                if in_flight is not None:
                    raise ValueError("Another gateway publication is already in progress")
                self._validate_publication_deployments(connection, target_id, child["desired_spec"])

            parent = connection.execute(
                """INSERT INTO multi_target_rollout (
                       id, rollout_kind, logical_model_id, request_sha256, created_by
                   ) VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                (
                    rollout_id,
                    rollout_kind,
                    logical_model_id,
                    request_sha256,
                    created_by,
                ),
            ).fetchone()
            for child, target_id in zip(children, target_ids, strict=True):
                generation = connection.execute(
                    """SELECT COALESCE(MAX(generation), 0) + 1 AS generation
                        FROM gateway_publication WHERE gateway_profile_id = %s""",
                    (target_id,),
                ).fetchone()["generation"]
                base = connection.execute(
                    """SELECT publication_id FROM effective_gateway_release
                        WHERE gateway_profile_id = %s""",
                    (target_id,),
                ).fetchone()
                initial_manifest = child.get("initial_manifest")
                manifest = {}
                if initial_manifest is not None:
                    expected_base = child.get("expected_base_release_id")
                    if (rollout_kind != "model_add" or expected_base is None
                            or base is None or base["publication_id"] != expected_base
                            or set(initial_manifest) != {"model_route_addition"}):
                        raise ValueError("Multi-target fixed program base changed")
                    previous = connection.execute(
                        "SELECT * FROM gateway_publication WHERE id=%s FOR UPDATE",
                        (expected_base,),
                    ).fetchone()
                    gateway = connection.execute(
                        "SELECT config FROM gateway_profile WHERE id=%s FOR SHARE", (target_id,),
                    ).fetchone()
                    manifest = _model_addition_manifest(
                        _managed_program_publication(previous, (gateway or {}).get("config") or {}),
                        child["desired_spec"], str(child["desired_spec_sha256"]), initial_manifest,
                    )
                publication = connection.execute(
                    """INSERT INTO gateway_publication (
                           gateway_profile_id, generation, desired_spec,
                           desired_spec_sha256, publication_kind, base_release_id,
                           created_by, resource_manifest
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                    (
                        target_id,
                        generation,
                        Jsonb(dict(child["desired_spec"])),
                        str(child["desired_spec_sha256"]),
                        rollout_kind,
                        base["publication_id"] if base else None,
                        created_by,
                        Jsonb(manifest),
                    ),
                ).fetchone()
                connection.execute(
                    "INSERT INTO gateway_publication_outbox (publication_id) VALUES (%s)",
                    (publication["id"],),
                )
                credential_ciphertext = child.get("credential_ciphertext")
                if credential_ciphertext is not None:
                    connection.execute(
                        """INSERT INTO gateway_publication_secret (
                               publication_id, credential_ciphertext
                           ) VALUES (%s, %s)""",
                        (publication["id"], credential_ciphertext),
                    )
                connection.execute(
                    """INSERT INTO gateway_publication_audit (
                           publication_id, from_status, to_status, actor
                       ) VALUES (%s, NULL, 'queued', %s)""",
                    (publication["id"], created_by),
                )
                connection.execute(
                    """INSERT INTO multi_target_rollout_child (
                           rollout_id, apim_target_id, publication_id, required
                       ) VALUES (%s, %s, %s, %s)""",
                    (
                        rollout_id,
                        target_id,
                        publication["id"],
                        bool(child.get("required", True)),
                    ),
                )
            return self._multi_target_rollout_view(connection, parent)

    def get_multi_target_rollout(
        self, rollout_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            rollout = connection.execute(
                "SELECT * FROM multi_target_rollout WHERE id = %s",
                (rollout_id,),
            ).fetchone()
            if rollout is None:
                return None
            return self._multi_target_rollout_view(connection, rollout)

    def list_multi_target_rollouts(
        self, limit: int = 50
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM multi_target_rollout
                   ORDER BY created_at DESC, id DESC LIMIT %s""",
                (limit,),
            ).fetchall()
            return [
                self._multi_target_rollout_view(connection, row) for row in rows
            ]

    @staticmethod
    def _multi_target_rollout_view(
        connection: Any,
        rollout: Mapping[str, Any],
    ) -> dict[str, Any]:
        child_rows = connection.execute(
            """SELECT child.*, publication.status AS publication_status
                 FROM multi_target_rollout_child child
                 JOIN gateway_publication publication
                   ON publication.id = child.publication_id
                WHERE child.rollout_id = %s
                ORDER BY child.apim_target_id""",
            (rollout["id"],),
        ).fetchall()
        children: list[dict[str, Any]] = []
        for child in child_rows:
            publication = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = %s",
                (child["publication_id"],),
            ).fetchone()
            children.append({**child, "publication": publication})
        return {**rollout, "children": children}

    def requeue_gateway_publication(
        self,
        publication_id: UUID,
        created_by: str,
        credential_ciphertext: bytes | None,
        desired_spec: Mapping[str, Any] | None = None,
        desired_spec_sha256: str | None = None,
        *, image_probe_authorization: Mapping[str, Any] | None = None,
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
        *, image_probe_authorization: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if publication["status"] != "failed":
            raise ValueError("Only a failed publication can be retried")
        if "fixed_route_migration" in publication.get("resource_manifest", {}):
            if any(value is not None for value in (
                credential_ciphertext, desired_spec, desired_spec_sha256, image_probe_authorization,
            )):
                raise ValueError("Program migration retry cannot replace its immutable intent")
            return PostgreSqlPublicationRepositoryMixin._resume_program_migration(
                connection, publication, created_by,
            )
        if set(publication.get("resource_manifest") or {}) == {"model_route_addition"}:
            if any(value is not None for value in (
                credential_ciphertext, desired_spec, desired_spec_sha256, image_probe_authorization,
            )):
                raise ValueError("Model addition recovery cannot replace its immutable intent")
            return PostgreSqlPublicationRepositoryMixin._resume_program_migration(
                connection, publication, created_by, model_addition=True,
            )
        if any(field in publication.get("resource_manifest", {}) for field in (
            "route_configuration", "route_preparation", "route_credentials", "route_dependencies",
            "route_rollback", "model_route_addition",
        )):
            raise ValueError(
                "Configuration publication requires explicit recovery or a new approval; "
                "its immutable intent and probe evidence cannot be reset",
            )
        if (desired_spec is None) != (desired_spec_sha256 is None):
            raise ValueError("A replacement release requires both its spec and hash")
        GatewayPublicationIntent.model_validate({
            "gateway_profile_id": publication["gateway_profile_id"],
            "publication_kind": publication["publication_kind"],
            "desired_spec": publication["desired_spec"] if desired_spec is None else desired_spec,
        })
        effective = connection.execute(
            """SELECT publication.resource_manifest FROM effective_gateway_release effective
               JOIN gateway_publication publication ON publication.id = effective.publication_id
               WHERE effective.gateway_profile_id = %s FOR SHARE OF effective, publication""",
            (publication["gateway_profile_id"],),
        ).fetchone()
        if effective is not None and any(
            field in (effective.get("resource_manifest") or {})
            for field in ("fixed_route_program", "route_configuration")
        ):
            raise ValueError("publication_retry_requires_new_release")
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
        PostgreSqlPublicationRepositoryMixin._validate_publication_deployments(
            connection, UUID(str(publication["gateway_profile_id"])),
            publication["desired_spec"] if desired_spec is None else desired_spec,
        )
        provisioning_audit = connection.execute(
            """SELECT from_status, to_status FROM gateway_publication_audit
               WHERE publication_id = %s
               ORDER BY created_at, id""",
            (publication["id"],),
        ).fetchall()
        materialized_named_values = publication_materialized_named_values(
            GatewayPublication.model_validate(publication),
            provisioning_completed=publication_provisioning_completed(provisioning_audit),
        )
        preserved_manifest: dict[str, Any] = (
            {"named_values": list(materialized_named_values),
             "credential_evidence": publication_credential_evidence(
                 GatewayPublication.model_validate(publication),
                 {name: "present" for name in materialized_named_values},
             )}
            if credential_ciphertext is None
            and materialized_named_values
            else {}
        )
        oauth_credentials = publication["resource_manifest"].get("oauth_credentials")
        if credential_ciphertext is None and isinstance(oauth_credentials, list):
            preserved_manifest["oauth_credentials"] = list(oauth_credentials)
        authorization = image_probe_authorization or publication["resource_manifest"].get(
            "image_probe_authorization"
        )
        if authorization is not None:
            preserved_manifest["image_probe_authorization"] = dict(authorization)
        prior_credential_version = publication["resource_manifest"].get("credential_generation")
        if credential_ciphertext is not None:
            preserved_manifest["credential_generation"] = str(uuid4())
        elif prior_credential_version is not None:
            preserved_manifest["credential_generation"] = prior_credential_version
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

    @staticmethod
    def _resume_program_migration(
        connection: Any, publication: Mapping[str, Any], created_by: str,
        *, model_addition: bool = False,
    ) -> dict[str, Any]:
        previous = connection.execute(
            """SELECT publication.* FROM effective_gateway_release effective
               JOIN gateway_publication publication ON publication.id=effective.publication_id
               WHERE effective.gateway_profile_id=%s FOR UPDATE OF effective, publication""",
            (publication["gateway_profile_id"],),
        ).fetchone()
        probes = connection.execute(
            """SELECT count(*) AS total FROM gateway_publication_audit
               WHERE publication_id=%s AND detail ? 'probe_request'""",
            (publication["id"],),
        ).fetchone()
        if model_addition:
            gateway = connection.execute(
                "SELECT config FROM gateway_profile WHERE id=%s FOR SHARE",
                (publication["gateway_profile_id"],),
            ).fetchone()
            previous = _managed_program_publication(previous, (gateway or {}).get("config") or {})
        detail = (_model_addition_credential_retry(publication, previous, probes["total"])
            if model_addition else _program_migration_retry(publication, previous, probes["total"]))
        PostgreSqlPublicationRepositoryMixin._validate_publication_deployments(
            connection, UUID(str(publication["gateway_profile_id"])), publication["desired_spec"],
        )
        pending = connection.execute(
            """SELECT id FROM gateway_publication WHERE gateway_profile_id=%s
               AND status NOT IN ('active','failed','superseded','rolled_back')""",
            (publication["gateway_profile_id"],),
        ).fetchone()
        outbox = connection.execute(
            "SELECT * FROM gateway_publication_outbox WHERE publication_id=%s FOR UPDATE",
            (publication["id"],),
        ).fetchone()
        if (pending is not None or outbox is None or outbox["status"] != "completed"
            or outbox.get("lease_owner") is not None
            or outbox.get("lease_expires_at") is not None):
            raise ValueError("Program migration retry requires an idle completed worker")
        row = connection.execute(
            """UPDATE gateway_publication SET status='queued', error_code=NULL,
               error_message=NULL, completed_at=NULL, updated_at=now()
               WHERE id=%s AND status='failed' RETURNING *""",
            (publication["id"],),
        ).fetchone()
        if row is None:
            raise ValueError("Program migration retry changed before enqueue")
        connection.execute(
            """UPDATE gateway_publication_outbox SET status='queued', available_at=now(),
               last_error_code=NULL, last_error_message=NULL, updated_at=now()
               WHERE publication_id=%s""",
            (publication["id"],),
        )
        audit_detail = {**detail, "outbox_attempts": outbox["attempts"]}
        connection.execute(
            """INSERT INTO gateway_publication_audit
               (publication_id,from_status,to_status,actor,detail)
               VALUES (%s,'failed','queued',%s,%s)""",
            (publication["id"], created_by, Jsonb(audit_detail)),
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
        clause = ("" if gateway_profile_id is None
                  else "AND publication.gateway_profile_id = %(gateway)s")
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT publication.* FROM gateway_publication publication
                    WHERE NOT EXISTS(SELECT 1 FROM gateway_publication_audit audit
                        WHERE audit.publication_id = publication.id
                          AND audit.detail @> '{{"archived_from_model_list":true}}'::jsonb)
                    {clause} ORDER BY publication.created_at DESC LIMIT %(limit)s""",
                {"gateway": gateway_profile_id, "limit": limit},
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def archive_gateway_publication(self, publication_id: UUID, actor: str) -> None:
        with self._connection() as connection, connection.transaction():
            publication = connection.execute(
                "SELECT status FROM gateway_publication WHERE id = %s FOR UPDATE",
                (publication_id,),
            ).fetchone()
            outbox = connection.execute(
                """SELECT status, lease_owner, lease_expires_at FROM gateway_publication_outbox
                   WHERE publication_id = %s FOR UPDATE""", (publication_id,),
            ).fetchone()
            if (publication is None or publication["status"] != "failed" or outbox is None
                    or outbox["status"] != "completed" or outbox["lease_owner"] is not None
                    or outbox["lease_expires_at"] is not None):
                raise ValueError(
                    "Only a terminal failed publication can be removed from the model list",
                )
            existing = connection.execute(
                """SELECT id FROM gateway_publication_audit WHERE publication_id = %s
                   AND detail @> '{"archived_from_model_list":true}'::jsonb LIMIT 1""",
                (publication_id,),
            ).fetchone()
            if existing is not None:
                return
            connection.execute(
                """INSERT INTO gateway_publication_audit
                   (publication_id, from_status, to_status, actor, detail)
                   VALUES (%s, 'failed', 'failed', %s, %s)""",
                (publication_id, actor, Jsonb({"archived_from_model_list": True})),
            )

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

    def record_managed_program_initialization(
        self, descriptor: Mapping[str, Any], expected_config_sha256: str,
    ) -> None:
        from ..domain.storage_routes import ManagedProgramInitialization, canonical_document

        initialized = ManagedProgramInitialization.model_validate_json(
            canonical_document(descriptor),
        )
        target_id = UUID(initialized.installation.program.scope.target_id)
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"gateway-publication:{target_id}",),
            )
            pending = connection.execute(
                """SELECT id FROM gateway_publication WHERE gateway_profile_id = %s
                   AND status NOT IN ('active','failed','superseded','rolled_back')
                   UNION ALL SELECT id FROM gateway_release_operation WHERE gateway_profile_id = %s
                   AND status NOT IN ('succeeded','failed','restored') LIMIT 1""",
                (target_id, target_id),
            ).fetchone()
            if pending is not None:
                raise ValueError("Managed initialization requires an idle target")
            previous = connection.execute(
                """SELECT publication.* FROM effective_gateway_release effective
                   JOIN gateway_publication publication ON publication.id = effective.publication_id
                   WHERE effective.gateway_profile_id = %s FOR SHARE OF effective, publication""",
                (target_id,),
            ).fetchone()
            gateway = connection.execute(
                """SELECT config FROM gateway_profile WHERE id = %s
                   AND enabled AND implementation = 'apim' FOR UPDATE""",
                (target_id,),
            ).fetchone()
            if gateway is None:
                raise ValueError("Managed initialization Gateway is unavailable")
            config = _managed_program_initialization_config(
                previous, gateway.get("config") or {}, descriptor, expected_config_sha256,
            )
            if config is not None:
                connection.execute(
                    "UPDATE gateway_profile SET config = %s, updated_at = now() WHERE id = %s",
                    (Jsonb(config), target_id),
                )
        self._invalidate_identities()

    def record_fixed_program_upgrade(
        self, descriptor: Mapping[str, Any], expected_config_sha256: str,
        *, before_record: Callable[[], None] | None = None,
    ) -> None:
        from ..domain.storage_routes import FixedProgramUpgrade, canonical_document

        upgrade = FixedProgramUpgrade.model_validate_json(canonical_document(descriptor))
        target_id = UUID(upgrade.installation.program.scope.target_id)
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"gateway-publication:{target_id}",),
            )
            pending = connection.execute(
                """SELECT id FROM gateway_publication WHERE gateway_profile_id = %s
                   AND status NOT IN ('active','failed','superseded','rolled_back')
                   UNION ALL SELECT id FROM gateway_release_operation WHERE gateway_profile_id = %s
                   AND status NOT IN ('succeeded','failed','restored') LIMIT 1""",
                (target_id, target_id),
            ).fetchone()
            if pending is not None:
                raise ValueError("Program upgrade requires an idle target")
            previous = connection.execute(
                """SELECT publication.* FROM effective_gateway_release effective
                   JOIN gateway_publication publication ON publication.id = effective.publication_id
                   WHERE effective.gateway_profile_id = %s FOR SHARE OF effective, publication""",
                (target_id,),
            ).fetchone()
            gateway = connection.execute(
                """SELECT config FROM gateway_profile WHERE id = %s
                   AND enabled AND implementation = 'apim' FOR UPDATE""",
                (target_id,),
            ).fetchone()
            if gateway is None:
                raise ValueError("Program upgrade Gateway is unavailable")
            config = _fixed_program_upgrade_config(
                previous, gateway.get("config") or {}, descriptor, expected_config_sha256,
            )
            if config is not None:
                if before_record is not None:
                    before_record()
                connection.execute(
                    "UPDATE gateway_profile SET config = %s, updated_at = now() WHERE id = %s",
                    (Jsonb(config), target_id),
                )
        self._invalidate_identities()

    def active_model_route_publications(
        self, gateway_profile_id: UUID,
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                                """SELECT publication.* FROM gateway_publication publication
                                     WHERE publication.gateway_profile_id = %s
                                         AND publication.status = 'active'
                                         AND publication.publication_kind = 'model_add'
                                         AND publication.resource_manifest ? 'model_route_addition'
                                         AND NOT EXISTS (
                                                 SELECT 1 FROM gateway_publication removal
                                                 WHERE removal.gateway_profile_id
                                                     = publication.gateway_profile_id
                                                     AND removal.publication_kind = 'model_remove'
                                                     AND removal.status = 'active'
                                                     AND removal.resource_manifest #>>
                                                               '{model_route_removal,source_publication_id}'
                                                               = publication.id::text
                                         )
                                     ORDER BY publication.id LIMIT 10001""",
                (gateway_profile_id,),
            ).fetchall()
        if len(rows) > 10000:
            raise ValueError("Model route publication catalog exceeds its bound")
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
        self, gateway_profile_ids: Sequence[UUID] | None = None
    ) -> Sequence[dict[str, Any]]:
        if gateway_profile_ids is not None and not gateway_profile_ids:
            return []
        statement = "SELECT * FROM gateway_profile"
        parameters: tuple[list[UUID], ...] = ()
        if gateway_profile_ids is not None:
            statement += " WHERE id = ANY(%s)"
            parameters = (list(gateway_profile_ids),)
        with self._connection() as connection:
            rows = connection.execute(statement, parameters).fetchall()
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

    def effective_gateway_publications(
        self, gateway_profile_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not gateway_profile_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT publication.*
                   FROM effective_gateway_release effective
                   JOIN gateway_publication publication
                     ON publication.id = effective.publication_id
                   WHERE effective.gateway_profile_id = ANY(%s)""",
                (list(gateway_profile_ids),),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

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
            if operation_kind == "self_hosted_gateway_provision":
                from ..domain.gateway_targets import (
                    SelfHostedGatewayProvisionSpec,
                    gateway_provision_digest,
                )

                spec = SelfHostedGatewayProvisionSpec.model_validate(semantic_preview)
                if (spec.target.gateway_profile_id != gateway_profile_id
                        or confirmation_sha256 != gateway_provision_digest(spec)
                        or target_release_id is not None or prior_release_id is not None):
                    raise ValueError("Gateway creation requires a target-bound idempotent request")
                if credential_ciphertext is not None:
                    raise ValueError("Gateway creation must not persist credentials")
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"gateway-provision-request:{spec.request_id}",),
                )
                previous = connection.execute(
                    """SELECT * FROM gateway_release_operation
                       WHERE operation_kind = 'self_hosted_gateway_provision'
                         AND semantic_preview->>'request_id' = %s""",
                    (str(spec.request_id),),
                ).fetchone()
                if previous is not None:
                    if (previous["gateway_profile_id"] != gateway_profile_id
                            or previous["confirmation_sha256"] != confirmation_sha256
                            or previous["created_by"] != created_by):
                        raise ValueError("Gateway creation request was already used differently")
                    return cast(dict[str, Any], previous)
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

    def list_gateway_release_operations_for_gateways(
        self, gateway_profile_ids: Sequence[UUID], limit: int = 100
    ) -> Sequence[dict[str, Any]]:
        if not gateway_profile_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM (
                       SELECT operation.*, row_number() OVER (
                           PARTITION BY gateway_profile_id
                           ORDER BY created_at DESC, id DESC
                       ) AS release_rank
                       FROM gateway_release_operation operation
                       WHERE gateway_profile_id = ANY(%s)
                   ) ranked WHERE release_rank <= %s
                   ORDER BY gateway_profile_id, created_at DESC, id DESC""",
                (list(gateway_profile_ids), limit),
            ).fetchall()
        for row in rows:
            row.pop("release_rank")
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
        *, keep_lease: bool = False,
    ) -> dict[str, Any] | None:
        if keep_lease and (
            status != expected_status or status in {"succeeded", "failed", "restored"}
        ):
            raise ValueError("Only an in-progress checkpoint can retain its release lease")
        allowed_updates = {"checkpoint", "error_code", "error_message"}
        unexpected = set(updates) - allowed_updates
        if unexpected:
            raise ValueError(f"Unsupported release operation updates: {sorted(unexpected)}")
        assignments = [
            "status = %(status)s",
            "updated_at = now()",
        ]
        if not keep_lease:
            assignments.extend(["lease_owner = NULL", "lease_expires_at = NULL"])
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
                                            AND lease_owner = %(actor)s
                                            AND lease_expires_at > now()
                    RETURNING *""",
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
                if (row["operation_kind"] == "application_provision"
                        and status in {"succeeded", "failed", "restored"}):
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

    def enqueue_gateway_configuration_rollback(
        self, operation_id: UUID, actor: str,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            operation = connection.execute(
                """SELECT * FROM gateway_release_operation WHERE id = %s
                   AND lease_owner = %s AND lease_expires_at > clock_timestamp() FOR UPDATE""",
                (operation_id, actor),
            ).fetchone()
            if operation is None:
                raise ValueError("Configuration rollback requires the current worker lease")
            gateway_id = operation["gateway_profile_id"]
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"gateway-publication:{gateway_id}",),
            )
            target = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = %s",
                (operation["target_release_id"],),
            ).fetchone()
            prior = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = %s",
                (operation["prior_release_id"],),
            ).fetchone()
            if target is None or prior is None:
                raise ValueError("Configuration rollback release is missing")
            spec, digest, detail = _configuration_rollback_values(operation, target, prior)
            linked = operation["checkpoint"].get("route_rollback_publication_id")
            if linked is not None:
                publication = connection.execute(
                    "SELECT * FROM gateway_publication WHERE id = %s", (UUID(linked),),
                ).fetchone()
                if (publication is None or publication["desired_spec_sha256"] != digest
                        or publication["resource_manifest"].get("route_rollback") != detail
                        or publication["base_release_id"] != prior["id"]
                        or publication["gateway_profile_id"] != gateway_id
                        or publication["desired_spec"] != spec):
                    raise ValueError("Configuration rollback publication changed")
                return cast(dict[str, Any], publication)
            effective = connection.execute(
                """SELECT publication_id FROM effective_gateway_release
                   WHERE gateway_profile_id = %s FOR UPDATE""", (gateway_id,),
            ).fetchone()
            if effective is None or effective["publication_id"] != prior["id"]:
                raise ValueError(
                    "The effective gateway release changed before configuration rollback",
                )
            gateway = connection.execute(
                "SELECT enabled, implementation FROM gateway_profile WHERE id = %s", (gateway_id,),
            ).fetchone()
            if gateway is None or not gateway["enabled"] or gateway["implementation"] != "apim":
                raise ValueError("The selected APIM gateway is unavailable")
            pending = connection.execute(
                """SELECT id FROM gateway_publication WHERE gateway_profile_id = %s
                   AND status IN ('queued', 'validating', 'provisioning', 'building_revision',
                                             'verifying', 'awaiting_authorization',
                                             'promoting', 'rolling_back')""",
                (gateway_id,),
            ).fetchone()
            if pending is not None:
                raise ValueError("Another gateway publication is already in progress")
            self._validate_publication_deployments(connection, gateway_id, spec)
            publication = connection.execute(
                """INSERT INTO gateway_publication (
                       gateway_profile_id, generation, desired_spec, desired_spec_sha256,
                       publication_kind, base_release_id, created_by, resource_manifest
                   ) SELECT %s, COALESCE(MAX(generation), 0) + 1, %s, %s,
                            'route_reconcile', %s, %s, %s
                     FROM gateway_publication WHERE gateway_profile_id = %s RETURNING *""",
                (gateway_id, Jsonb(spec), digest, prior["id"], operation["created_by"],
                 Jsonb({"route_rollback": detail}), gateway_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO gateway_publication_outbox (publication_id) VALUES (%s)",
                (publication["id"],),
            )
            connection.execute(
                """INSERT INTO gateway_publication_audit (
                       publication_id, from_status, to_status, actor, detail
                   ) VALUES (%s, NULL, 'queued', %s, %s)""",
                (publication["id"], actor, Jsonb({"route_rollback": detail})),
            )
            checkpoint = {**operation["checkpoint"],
                          "route_rollback_publication_id": str(publication["id"])}
            linked_row = connection.execute(
                """UPDATE gateway_release_operation SET checkpoint = %s, updated_at = now()
                   WHERE id = %s AND lease_owner = %s AND lease_expires_at > clock_timestamp()
                   RETURNING id""",
                (Jsonb(checkpoint), operation_id, actor),
            ).fetchone()
            if linked_row is None:
                raise ValueError("Configuration rollback lease expired during enqueue")
            connection.execute(
                """INSERT INTO gateway_release_operation_audit (
                       operation_id, from_status, to_status, actor, detail
                   ) VALUES (%s, %s, %s, %s, %s)""",
                (operation_id, operation["status"], operation["status"], actor,
                 Jsonb({"checkpoint": checkpoint})),
            )
        return cast(dict[str, Any], publication)

    def complete_gateway_configuration_rollback(
        self, operation_id: UUID, actor: str,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            operation = connection.execute(
                """SELECT * FROM gateway_release_operation WHERE id = %s
                   AND operation_kind = 'rollback' AND status = 'verifying_readback'
                   AND lease_owner = %s AND lease_expires_at > clock_timestamp() FOR UPDATE""",
                (operation_id, actor),
            ).fetchone()
            if operation is None:
                raise ValueError("Configuration rollback is not ready to complete")
            linked = operation["checkpoint"].get("route_rollback_publication_id")
            if linked is None:
                raise ValueError("Configuration rollback publication association is missing")
            child = connection.execute(
                """SELECT publication.* FROM gateway_publication publication
                   JOIN effective_gateway_release effective
                     ON effective.gateway_profile_id = publication.gateway_profile_id
                    AND effective.publication_id = publication.id
                   WHERE publication.id = %s AND publication.gateway_profile_id = %s
                     AND publication.status = 'active'
                   FOR UPDATE OF publication, effective""",
                (UUID(linked), operation["gateway_profile_id"]),
            ).fetchone()
            if child is None:
                raise ValueError("Configuration rollback publication is not currently active")
            target = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = %s",
                (operation["target_release_id"],),
            ).fetchone()
            prior = connection.execute(
                "SELECT * FROM gateway_publication WHERE id = %s",
                (operation["prior_release_id"],),
            ).fetchone()
            if target is None or prior is None:
                raise ValueError("Configuration rollback release is missing")
            spec, digest, detail = _configuration_rollback_values(operation, target, prior)
            if (child["desired_spec_sha256"] != digest or child["desired_spec"] != spec
                    or child["base_release_id"] != prior["id"]
                    or child["resource_manifest"].get("route_rollback") != detail):
                raise ValueError("Configuration rollback publication changed")
            completed = connection.execute(
                """UPDATE gateway_release_operation
                   SET status = 'succeeded', completed_at = now(), updated_at = now(),
                       lease_owner = NULL, lease_expires_at = NULL,
                       error_code = NULL, error_message = NULL
                   WHERE id = %s AND lease_owner = %s AND lease_expires_at > clock_timestamp()
                   RETURNING *""",
                (operation_id, actor),
            ).fetchone()
            if completed is None:
                raise ValueError("Configuration rollback lease expired during completion")
            connection.execute(
                """INSERT INTO gateway_release_operation_audit (
                       operation_id, from_status, to_status, actor, detail
                   ) VALUES (%s, 'verifying_readback', 'succeeded', %s, %s)""",
                (operation_id, actor, Jsonb({"checkpoint": operation["checkpoint"]})),
            )
        return cast(dict[str, Any], completed)

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
            self._sync_active_route_bindings(connection, target)
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
            row = connection.execute(
                """WITH candidate AS (
                   SELECT outbox.id, outbox.publication_id
                   FROM gateway_publication_outbox outbox
                   JOIN gateway_publication publication ON publication.id = outbox.publication_id
                   WHERE (
                       outbox.status = 'queued' AND outbox.available_at <= now()
                   ) OR (
                       outbox.status = 'leased' AND outbox.lease_expires_at < now()
                   )
                   ORDER BY outbox.id
                   FOR UPDATE OF outbox SKIP LOCKED
                   LIMIT 1
                   ), claimed AS (
                   UPDATE gateway_publication_outbox outbox
                   SET status = 'leased', lease_owner = %s,
                       lease_expires_at = now() + (%s * interval '1 second'),
                       attempts = outbox.attempts + 1, updated_at = now()
                   FROM candidate WHERE outbox.id = candidate.id
                   RETURNING outbox.publication_id
                   )
                   UPDATE gateway_publication publication
                   SET attempt_count = publication.attempt_count + 1,
                       started_at = COALESCE(publication.started_at, now()), updated_at = now()
                   FROM claimed WHERE publication.id = claimed.publication_id
                   RETURNING publication.*""",
                (worker_id, lease_seconds),
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

    def reserve_gateway_publication_probe(
        self, publication_id: UUID, worker_id: str, request_limit: int,
        method: str, endpoint_sha256: str,
    ) -> bool:
        if request_limit < 1:
            raise ValueError("Publication probe request limit must be positive")
        with self._connection() as connection, connection.transaction():
            owner = connection.execute(
                """SELECT publication.status FROM gateway_publication_outbox outbox
                   JOIN gateway_publication publication ON publication.id = outbox.publication_id
                   WHERE outbox.publication_id = %s AND outbox.status = 'leased'
                     AND outbox.lease_owner = %s AND outbox.lease_expires_at > now()
                     AND publication.status = 'verifying'
                   FOR UPDATE OF outbox""",
                (publication_id, worker_id),
            ).fetchone()
            if owner is None:
                return False
            usage = connection.execute(
                """SELECT count(*) AS requests FROM gateway_publication_audit
                   WHERE publication_id = %s AND detail ? 'probe_request'""",
                (publication_id,),
            ).fetchone()
            if usage is None or int(usage["requests"]) >= request_limit:
                return False
            connection.execute(
                """INSERT INTO gateway_publication_audit (
                       publication_id, from_status, to_status, actor, detail
                   ) VALUES (%s, %s, %s, %s, %s)""",
                (publication_id, owner["status"], owner["status"], worker_id, Jsonb({
                    "probe_request": {
                        "method": method, "endpoint_sha256": endpoint_sha256,
                        "request_index": int(usage["requests"]) + 1, "request_limit": request_limit,
                    },
                })),
            )
        return True

    def checkpoint_gateway_publication_probes(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "probe_verification",
        )

    def checkpoint_gateway_publication_activation(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_activation",
        )

    def checkpoint_gateway_publication_writer(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_writer",
        )

    def checkpoint_gateway_publication_dependencies(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_dependencies",
        )

    def checkpoint_gateway_publication_credentials(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_credentials",
        )

    def checkpoint_gateway_publication_preparation(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_preparation",
        )

    def checkpoint_gateway_program_promotion(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_program_promotion",
        )

    def checkpoint_gateway_publication_approval(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_commit_approval",
        )

    def _checkpoint_gateway_publication(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any], field: str,
    ) -> bool:
        phase = {
            "probe_verification": "verifying", "route_activation": "promoting",
            "route_writer": "promoting", "route_dependencies": "queued",
            "route_commit_approval": "promoting", "route_preparation": "queued",
            "route_credentials": "queued", "route_program_promotion": "promoting",
        }[field]
        statement = """WITH owned AS (
            SELECT publication.status FROM gateway_publication_outbox outbox
            JOIN gateway_publication publication ON publication.id = outbox.publication_id
            WHERE outbox.publication_id = %(id)s AND outbox.status = 'leased'
              AND outbox.lease_owner = %(worker)s AND outbox.lease_expires_at > now()
              AND publication.status = %(phase)s
            FOR UPDATE OF outbox
        ), recorded AS (
            UPDATE gateway_publication publication
            SET resource_manifest = jsonb_set(
                    publication.resource_manifest, ARRAY[%(field)s], %(checkpoint)s::jsonb),
                updated_at = now()
            FROM owned WHERE publication.id = %(id)s AND publication.status = %(phase)s
              AND NULLIF(publication.resource_manifest -> %(field)s, 'null'::jsonb)
                  IS NOT DISTINCT FROM %(expected)s::jsonb
            RETURNING publication.id
        ), audited AS (
            INSERT INTO gateway_publication_audit (
                publication_id, from_status, to_status, actor, detail
            ) SELECT recorded.id, %(phase)s, %(phase)s, %(worker)s, %(detail)s::jsonb
              FROM recorded RETURNING publication_id
        ) SELECT publication_id AS id FROM audited"""
        parameters = {
            "id": publication_id, "checkpoint": Jsonb(dict(checkpoint)),
            "field": field, "phase": phase, "worker": worker_id,
            "detail": Jsonb({field: dict(checkpoint)}),
            "expected": Jsonb(dict(expected)) if expected is not None else None,
        }
        with self._connection() as connection, connection.transaction():
            recorded = connection.execute(statement, parameters).fetchone()
        return recorded is not None

    def reserve_gateway_release_operation_probe(
        self, operation_id: UUID, worker_id: str, request_limit: int,
        method: str, endpoint_sha256: str,
    ) -> bool:
        if request_limit < 1:
            raise ValueError("Release probe request limit must be positive")
        with self._connection() as connection, connection.transaction():
            owner = connection.execute(
                """SELECT status FROM gateway_release_operation
                   WHERE id = %s AND operation_kind = 'rollback' AND lease_owner = %s
                     AND lease_expires_at > now()
                     AND status IN ('preflight_probing', 'post_promotion_probing', 'restoring')
                   FOR UPDATE""",
                (operation_id, worker_id),
            ).fetchone()
            if owner is None:
                return False
            usage = connection.execute(
                """SELECT count(*) AS requests FROM gateway_release_operation_audit
                   WHERE operation_id = %s AND detail ? 'probe_request'""",
                (operation_id,),
            ).fetchone()
            if usage is None or int(usage["requests"]) >= request_limit:
                return False
            connection.execute(
                """INSERT INTO gateway_release_operation_audit
                   (operation_id, from_status, to_status, actor, detail)
                   VALUES (%s, %s, %s, %s, %s)""",
                (operation_id, owner["status"], owner["status"], worker_id, Jsonb({
                    "probe_request": {
                        "method": method, "endpoint_sha256": endpoint_sha256,
                        "request_index": int(usage["requests"]) + 1,
                        "request_limit": request_limit,
                    },
                })),
            )
        return True

    def renew_gateway_publication_lease(
        self, publication_id: UUID, worker_id: str, lease_seconds: int,
    ) -> bool:
        with self._connection() as connection:
            return connection.execute(
                """UPDATE gateway_publication_outbox
                   SET lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
                   WHERE publication_id = %s AND status = 'leased' AND lease_owner = %s
                     AND lease_expires_at > now() RETURNING id""",
                (lease_seconds, publication_id, worker_id),
            ).fetchone() is not None

    def renew_gateway_release_operation_lease(
        self, operation_id: UUID, worker_id: str, lease_seconds: int,
    ) -> bool:
        with self._connection() as connection:
            return connection.execute(
                """UPDATE gateway_release_operation
                   SET lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
                   WHERE id = %s AND lease_owner = %s AND lease_expires_at > now()
                   RETURNING id""",
                (lease_seconds, operation_id, worker_id),
            ).fetchone() is not None

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
                """SELECT id FROM gateway_publication_outbox
                   WHERE publication_id = %s AND status = 'leased'
                     AND lease_owner = %s AND lease_expires_at > now() FOR UPDATE""",
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
            if terminal or (expected_status == "validating" and status == "provisioning"):
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

    @staticmethod
    def _sync_active_route_bindings(
        connection: Any,
        publication: Mapping[str, Any],
    ) -> None:
        target_id = UUID(str(publication["gateway_profile_id"]))
        model_addition = "model_route_addition" in publication.get("resource_manifest", {})
        target = connection.execute(
            "SELECT gateway_profile_id FROM apim_target_metadata WHERE gateway_profile_id = %s",
            (target_id,),
        ).fetchone()
        if target is None:
            return
        desired_spec = dict(publication["desired_spec"])
        for raw_binding in desired_spec.get("bindings", []):
            binding = dict(raw_binding)
            model_spec = dict(binding["model"])
            logical_model_id = binding.get("logical_model_id")
            if logical_model_id is not None:
                model = connection.execute(
                    "SELECT id, runtime_id, model_key FROM managed_model WHERE id = %s",
                    (UUID(str(logical_model_id)),),
                ).fetchone()
            else:
                model = connection.execute(
                    """SELECT id, runtime_id, model_key FROM managed_model
                       WHERE lower(model_key) = lower(%s)""",
                    (model_spec["model_key"],),
                ).fetchone()
            if model is None:
                raise ValueError("An active release binding has no logical model")
            if str(model["model_key"]).casefold() != str(
                model_spec["model_key"]
            ).casefold():
                raise ValueError("An active release binding changed logical model alias")
            runtime_id = binding.get("runtime_id")
            if runtime_id is not None:
                runtime = connection.execute(
                    """SELECT id, provider_id, gateway_profile_id FROM model_runtime
                       WHERE id = %s""",
                    (UUID(str(runtime_id)),),
                ).fetchone()
            else:
                runtime = connection.execute(
                    """SELECT id, provider_id, gateway_profile_id FROM model_runtime
                       WHERE gateway_profile_id = %s AND lower(name) = lower(%s)""",
                    (target_id, binding["runtime_name"]),
                ).fetchone()
            if runtime is None or runtime["gateway_profile_id"] != target_id:
                raise ValueError("An active release binding has no target Runtime")
            is_default = model["runtime_id"] == runtime["id"]
            if is_default and not model_addition:
                connection.execute(
                    """UPDATE apim_target_route_binding
                       SET is_default_serving = FALSE, updated_at = now()
                       WHERE logical_model_id = %s AND apim_target_id <> %s
                         AND is_default_serving""",
                    (model["id"], target_id),
                )
            scope = dict(binding.get("deployment_scope") or {})
            mode = str(scope.get("mode") or "all")
            deployment_ids = [UUID(str(value)) for value in scope.get("deployment_ids", [])]
            conflict_action = "DO NOTHING" if model_addition else """DO UPDATE SET
                       runtime_id = EXCLUDED.runtime_id,
                       enabled = TRUE,
                       is_default_serving = EXCLUDED.is_default_serving,
                       deployment_scope_mode = EXCLUDED.deployment_scope_mode,
                       current_publication_id = EXCLUDED.current_publication_id,
                       capability_state = EXCLUDED.capability_state,
                       updated_at = now()"""
            route = connection.execute(
                f"""INSERT INTO apim_target_route_binding (
                       id, logical_model_id, apim_target_id, runtime_id, enabled,
                       is_default_serving, deployment_scope_mode,
                       current_publication_id, capability_state
                   ) VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, 'supported')
                   ON CONFLICT (logical_model_id, apim_target_id) {conflict_action}
                   RETURNING id""",
                (
                    apim_target_route_binding_id_for(model["id"], target_id),
                    model["id"],
                    target_id,
                    runtime["id"],
                    is_default,
                    mode,
                    publication["id"],
                ),
            ).fetchone()
            if route is None:
                raise ValueError("Model addition cannot replace an existing model route")
            if not model_addition:
                connection.execute(
                    """DELETE FROM apim_target_route_binding_deployment
                       WHERE route_binding_id = %s""",
                    (route["id"],),
                )
            for deployment_id in deployment_ids:
                deployment = connection.execute(
                    """SELECT id FROM gateway_deployment
                       WHERE id = %s AND apim_target_id = %s
                         AND retired_at IS NULL""",
                    (deployment_id, target_id),
                ).fetchone()
                if deployment is None:
                    raise ValueError(
                        "Selected Gateway deployment no longer belongs to the APIM target"
                    )
                connection.execute(
                    """INSERT INTO apim_target_route_binding_deployment (
                           route_binding_id, apim_target_id, gateway_deployment_id
                       ) VALUES (%s, %s, %s)""",
                    (route["id"], target_id, deployment_id),
                )

    def activate_gateway_publication(
        self, publication_id: UUID, actor: str,
        *, validate_model_activation: ModelActivationValidator | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            outbox = connection.execute(
                """SELECT id FROM gateway_publication_outbox
                   WHERE publication_id = %s AND status = 'leased'
                     AND lease_owner = %s AND lease_expires_at > now() FOR UPDATE""",
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
            if any(name in publication["resource_manifest"] for name in (
                "model_route_addition", "model_route_removal",
            )):
                if validate_model_activation is None:
                    raise ValueError("Model activation requires its complete transaction validator")
                effective = connection.execute(
                    """SELECT publication_id FROM effective_gateway_release
                       WHERE gateway_profile_id = %s FOR UPDATE""",
                    (publication["gateway_profile_id"],),
                ).fetchone()
                if (effective is None
                        or effective["publication_id"] != publication["base_release_id"]):
                    raise ValueError("Model addition program base changed")
                previous = connection.execute(
                    "SELECT * FROM gateway_publication WHERE id = %s FOR UPDATE",
                    (publication["base_release_id"],),
                ).fetchone()
                gateway = connection.execute(
                    "SELECT config FROM gateway_profile WHERE id = %s FOR SHARE",
                    (publication["gateway_profile_id"],),
                ).fetchone()
                if gateway is None:
                    raise ValueError("Model addition Gateway no longer exists")
                validate_model_activation(
                    publication, _managed_program_publication(
                        previous, gateway.get("config") or {},
                    ),
                    (gateway.get("config") or {}).get(
                        "fixed_route_program_upgrade",
                    ),
                )
            if "fixed_route_migration" in publication["resource_manifest"]:
                effective = connection.execute(
                    """SELECT publication_id FROM effective_gateway_release
                       WHERE gateway_profile_id = %s FOR UPDATE""",
                    (publication["gateway_profile_id"],),
                ).fetchone()
                if (
                    effective is None
                    or effective["publication_id"] != publication["base_release_id"]
                ):
                    raise ValueError("Program migration activation confirmation base changed")
                previous = connection.execute(
                    "SELECT * FROM gateway_publication WHERE id = %s FOR UPDATE",
                    (publication["base_release_id"],),
                ).fetchone()
                _require_program_migration_activation(publication, previous)
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
                target_id = UUID(str(publication["gateway_profile_id"]))
                route = connection.execute(
                    """SELECT id FROM apim_target_route_binding
                       WHERE logical_model_id = %s AND apim_target_id = %s""",
                    (model_id, target_id),
                ).fetchone()
                if route is not None:
                    connection.execute(
                        """DELETE FROM apim_target_route_binding_deployment
                           WHERE route_binding_id = %s""",
                        (route["id"],),
                    )
                    connection.execute(
                        "DELETE FROM apim_target_route_binding WHERE id = %s",
                        (route["id"],),
                    )
                remaining_routes = connection.execute(
                    """SELECT route.id, route.runtime_id,
                              route.is_default_serving, runtime.provider_id
                         FROM apim_target_route_binding route
                         JOIN model_runtime runtime ON runtime.id = route.runtime_id
                        WHERE route.logical_model_id = %s AND route.enabled
                        ORDER BY route.is_default_serving DESC,
                                 route.apim_target_id, route.id""",
                    (model_id,),
                ).fetchall()
                if remaining_routes:
                    selected = remaining_routes[0]
                    connection.execute(
                        """UPDATE apim_target_route_binding
                           SET is_default_serving = (id = %s), updated_at = now()
                           WHERE logical_model_id = %s""",
                        (selected["id"], model_id),
                    )
                    connection.execute(
                        """UPDATE managed_model
                           SET runtime_id = %s, provider_id = %s, updated_at = now()
                           WHERE id = %s""",
                        (
                            selected["runtime_id"],
                            selected["provider_id"],
                            model_id,
                        ),
                    )
                else:
                    affected_users = connection.execute(
                        """SELECT user_id FROM user_model_access
                           WHERE model_id = %s ORDER BY user_id""",
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
                            """UPDATE user_model_policy
                               SET updated_at = now(), updated_by = %s
                               WHERE user_id = %s""",
                            (publication["created_by"], user_id),
                        )
                        connection.execute(
                            """INSERT INTO user_model_access_audit (
                                   id, user_id, previous_model_ids,
                                   new_model_ids, changed_by
                               ) VALUES (gen_random_uuid(), %s, %s, %s, %s)""",
                            (
                                user_id,
                                previous,
                                current,
                                publication["created_by"],
                            ),
                        )
                    connection.execute(
                        """UPDATE assistant_setting SET model_id = NULL,
                               updated_at = now(), updated_by = %s
                           WHERE model_id = %s""",
                        (publication["created_by"], model_id),
                    )
                    connection.execute(
                        "DELETE FROM managed_model WHERE id = %s",
                        (model_id,),
                    )
                if "model_route_removal" not in publication["resource_manifest"]:
                    self._sync_active_route_bindings(connection, publication)
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
                self._sync_active_route_bindings(connection, publication)
                return self._complete_gateway_activation(
                    connection, publication, publication_id, actor
                )
            binding = dict(publication["desired_spec"])["bindings"][-1]
            if publication["publication_kind"] == "credential_rotation":
                if binding.get("runtime_id") is not None:
                    runtime = connection.execute(
                        """SELECT * FROM model_runtime
                           WHERE gateway_profile_id = %s AND id = %s FOR UPDATE""",
                        (publication["gateway_profile_id"], binding["runtime_id"]),
                    ).fetchone()
                else:
                    runtime = connection.execute(
                        """SELECT * FROM model_runtime
                           WHERE gateway_profile_id = %s AND lower(name) = lower(%s)
                           FOR UPDATE""",
                        (publication["gateway_profile_id"], binding["runtime_name"]),
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
                self._sync_active_route_bindings(connection, publication)
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
                activated_config = _activated_runtime_credentials(
                    runtime_config, binding, publication["resource_manifest"],
                )
                if activated_config != runtime_config:
                    connection.execute(
                        "UPDATE model_runtime SET config = %s, updated_at = now() WHERE id = %s",
                        (Jsonb(activated_config), runtime_id),
                    )
            else:
                runtime_config = _activation_runtime_config(binding)
                runtime = connection.execute(
                    """INSERT INTO model_runtime (
                           provider_id, gateway_profile_id, name, runtime_kind,
                           enabled, is_default, config
                       ) VALUES (%s, %s, %s, %s, TRUE, FALSE, %s)
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

            logical_model_id = (
                UUID(str(binding["logical_model_id"]))
                if binding.get("logical_model_id") is not None
                else None
            )
            existing_model = connection.execute(
                """SELECT model.*, metadata.family_key,
                          model.input_cost_per_million::DOUBLE PRECISION
                              AS input_cost_per_million,
                          model.output_cost_per_million::DOUBLE PRECISION
                              AS output_cost_per_million,
                          model.cached_cost_per_million::DOUBLE PRECISION
                              AS cached_cost_per_million,
                          model.cache_write_cost_per_million::DOUBLE PRECISION
                              AS cache_write_cost_per_million,
                          metadata.assignment_required, metadata.publication_id,
                          COALESCE(price.price_source, 'manual') AS price_source,
                          price.price_reference,
                          price.price_discount_percent::DOUBLE PRECISION
                              AS price_discount_percent,
                          price.list_input_cost_per_million::DOUBLE PRECISION
                              AS list_input_cost_per_million,
                          price.list_output_cost_per_million::DOUBLE PRECISION
                              AS list_output_cost_per_million,
                          price.list_cached_cost_per_million::DOUBLE PRECISION
                              AS list_cached_cost_per_million,
                          price.list_cache_write_cost_per_million::DOUBLE PRECISION
                              AS list_cache_write_cost_per_million,
                          COALESCE(price.context_price_tiers, '[]'::jsonb)
                              AS context_price_tiers
                   FROM managed_model model
                   LEFT JOIN managed_model_metadata metadata
                     ON metadata.model_id = model.id
                   LEFT JOIN managed_model_price price ON price.model_id = model.id
                   WHERE lower(model.model_key) = lower(%s)""",
                (model_spec["model_key"],),
            ).fetchone()
            if existing_model is not None:
                same_publication = existing_model["publication_id"] == publication_id
                same_logical_model = (
                    logical_model_id is not None
                    and existing_model["id"] == logical_model_id
                )
                if not same_publication and not same_logical_model:
                    raise ValueError("The model alias is already active")
                if same_logical_model:
                    common_fields = (
                        "display_name",
                        "family_key",
                        "capabilities",
                        "context_window",
                        "input_cost_per_million",
                        "output_cost_per_million",
                        "cached_cost_per_million",
                        "cache_write_cost_per_million",
                        "price_reference",
                        "price_discount_percent",
                        "list_input_cost_per_million",
                        "list_output_cost_per_million",
                        "list_cached_cost_per_million",
                        "list_cache_write_cost_per_million",
                        "allowed_roles",
                        "assignment_required",
                    )
                    if any(
                        existing_model.get(field) != model_spec.get(field)
                        for field in common_fields
                    ) or existing_model.get("price_source", "manual") != model_spec.get(
                        "price_source", "manual"
                    ) or (existing_model.get("context_price_tiers") or []) != (
                        model_spec.get("context_price_tiers") or []
                    ):
                        raise ValueError(
                            "Multi-target child conflicts with the logical model identity"
                        )
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
                        logical_model_id or publication_model_id(
                            publication_id, model_spec["model_key"]
                        ),
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
                connection.execute(
                    """INSERT INTO managed_model_price (
                           model_id, price_source, price_reference, price_discount_percent,
                           list_input_cost_per_million, list_output_cost_per_million,
                           list_cached_cost_per_million, list_cache_write_cost_per_million,
                           price_synced_at, price_sync_status, context_price_tiers
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        model["id"], model_spec.get("price_source", "manual"),
                        model_spec.get("price_reference"), model_spec.get("price_discount_percent"),
                        model_spec.get("list_input_cost_per_million"),
                        model_spec.get("list_output_cost_per_million"),
                        model_spec.get("list_cached_cost_per_million"),
                        model_spec.get("list_cache_write_cost_per_million"),
                        publication["created_at"]
                        if model_spec.get("price_source", "manual") != "manual"
                        and model_spec.get("price_reference") else None,
                        "ok" if model_spec.get("price_source", "manual") != "manual"
                        and model_spec.get("price_reference") else None,
                        Jsonb(model_spec.get("context_price_tiers") or []),
                    ),
                )
                if pricing := model_spec.get("price_configuration"):
                    from .repository_registry import PostgreSqlRegistryRepositoryMixin

                    PostgreSqlRegistryRepositoryMixin._write_price(
                        connection, "model", model["id"], pricing,
                    )
            self._sync_active_route_bindings(connection, publication)
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
        if not any(name in publication.get("resource_manifest", {}) for name in (
            "model_route_addition", "model_route_removal",
        )):
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
        self, runtime_id: UUID | None, model_id: UUID | None,
        *, gateway_profile_id: UUID | None = None,
        gateway_deployment_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        if (gateway_profile_id is None) != (gateway_deployment_id is None):
            return None
        if gateway_profile_id is not None and model_id is None:
            return None
        conditions = [
            "model.enabled",
            "runtime.enabled",
            "provider.enabled",
            "(gateway.id IS NULL OR gateway.enabled)",
            "(target_route.id IS NULL OR target_route.current_publication_id = publication.id)",
            """(target_route.id IS NULL OR publication.id = effective.publication_id OR (
                publication.status = 'active' AND publication.publication_kind = 'model_add'
                AND publication.gateway_profile_id = gateway.id
                AND publication.base_release_id = effective.publication_id
                AND publication.resource_manifest->'model_route_addition'->'version' = '1'::jsonb
                AND publication.resource_manifest->'model_route_addition'->>'program_release_id'
                    = effective.publication_id::text
                AND lower(publication.resource_manifest->'model_route_addition'->>'alias')
                    = lower(model.model_key)
                AND jsonb_array_length(publication.desired_spec->'bindings') = 1
                AND binding.value IS NOT NULL
                AND (binding.value->>'logical_model_id' IS NULL
                    OR binding.value->>'logical_model_id' = model.id::text)
                AND (binding.value->>'runtime_id' IS NULL
                    OR binding.value->>'runtime_id' = runtime.id::text)
            ))""",
            """(target_route.id IS NOT NULL OR NOT EXISTS (
                SELECT 1 FROM apim_target_route_binding known_route
                WHERE known_route.logical_model_id = model.id
            ))""",
            """(selected_deployment.id IS NOT NULL OR (
                target_route.id IS NULL AND invocation_target.gateway_profile_id IS NULL
            ))""",
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
        target_join = ""
        runtime_identity = "model.runtime_id"
        route_join = """LEFT JOIN apim_target_route_binding target_route
            ON target_route.logical_model_id = model.id
            AND target_route.runtime_id = runtime.id
            AND target_route.enabled AND target_route.is_default_serving"""
        join_parameters: list[Any] = []
        deployment_selection = ""
        if gateway_profile_id is not None:
            target_join = """LEFT JOIN apim_target_route_binding target_route
                ON target_route.logical_model_id = model.id
                AND target_route.apim_target_id = %s AND target_route.enabled"""
            runtime_identity = "COALESCE(target_route.runtime_id, model.runtime_id)"
            route_join = ""
            join_parameters = [gateway_profile_id, gateway_deployment_id]
            conditions.append("gateway.id = %s")
            parameters.append(gateway_profile_id)
            deployment_selection = "AND deployment.id = %s"
            conditions.append("selected_deployment.id IS NOT NULL")
        deployment_join = f"""LEFT JOIN LATERAL (
                SELECT deployment.id, deployment.endpoint_url, deployment.native_gateway_id,
                    to_jsonb(deployment) || jsonb_build_object(
                        'provisioning_operation_id', provisioning.operation_id
                    ) AS deployment,
                    jsonb_build_object(
                        'provisioning_operation', jsonb_build_object(
                            'id', provision.id, 'gateway_profile_id', provision.gateway_profile_id,
                            'status', provision.status, 'operation_kind', provision.operation_kind,
                            'semantic_preview', provision.semantic_preview,
                            'confirmation_sha256', provision.confirmation_sha256
                        ),
                        'associated_api_ids', COALESCE((
                            SELECT jsonb_agg(association.api_id)
                            FROM gateway_deployment_api_association association
                            WHERE association.deployment_id = deployment.id
                        ), '[]'::jsonb),
                        'hostname_configurations', COALESCE((
                            SELECT jsonb_agg(jsonb_build_object('hostname', hostname.hostname))
                            FROM gateway_deployment_hostname_inventory hostname
                            WHERE hostname.deployment_id = deployment.id
                        ), '[]'::jsonb)
                    ) AS inventory
                FROM gateway_deployment deployment
                LEFT JOIN gateway_deployment_provisioning provisioning
                    ON provisioning.deployment_id = deployment.id
                LEFT JOIN gateway_release_operation provision
                    ON provision.id = provisioning.operation_id
                WHERE deployment.apim_target_id = gateway.id
                    {deployment_selection}
                    AND deployment.retired_at IS NULL AND deployment.declared_state = 'present'
                    AND deployment.endpoint_url IS NOT NULL
                    AND (
                        (target_route.id IS NULL AND deployment.kind = 'managed')
                        OR (target_route.id IS NOT NULL AND (
                            target_route.deployment_scope_mode = 'all'
                            OR EXISTS (
                                SELECT 1 FROM apim_target_route_binding_deployment selected
                                WHERE selected.route_binding_id = target_route.id
                                    AND selected.apim_target_id = gateway.id
                                    AND selected.gateway_deployment_id = deployment.id
                            )
                        ))
                    )
                    AND (deployment.kind = 'managed' OR EXISTS (
                        SELECT 1 FROM gateway_deployment_api_association association
                        JOIN apim_target_metadata target ON target.gateway_profile_id = gateway.id
                        WHERE association.deployment_id = deployment.id
                            AND lower(association.api_id) = lower(target.api_id)
                    ))
                ORDER BY CASE
                    WHEN target_route.deployment_scope_mode = 'all'
                        THEN CASE deployment.kind WHEN 'managed' THEN 0 ELSE 1 END
                    ELSE CASE deployment.kind WHEN 'self_hosted' THEN 0 ELSE 1 END
                END, deployment.id
                LIMIT 1
            ) selected_deployment ON TRUE"""
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
                    COALESCE(model_price.context_price_tiers, '[]'::jsonb) AS context_price_tiers,
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
                    COALESCE(selected_deployment.endpoint_url, gateway.base_url)
                        AS gateway_base_url,
                    selected_deployment.id AS gateway_deployment_id,
                    selected_deployment.native_gateway_id,
                    to_jsonb(invocation_target) AS gateway_target,
                    selected_deployment.deployment AS gateway_deployment,
                    selected_deployment.inventory AS gateway_inventory,
                    gateway.auth_type AS gateway_auth_type,
                    gateway.credential_ciphertext AS gateway_credential_ciphertext,
                    gateway.config AS gateway_config,
                    to_jsonb(publication) AS gateway_model_publication,
                    effective.publication_id AS gateway_effective_publication_id
                FROM managed_model model
                LEFT JOIN managed_model_price model_price ON model_price.model_id = model.id
                {target_join}
                JOIN model_runtime runtime ON runtime.id = {runtime_identity}
                JOIN model_provider provider ON provider.id = model.provider_id
                                LEFT JOIN managed_model_metadata model_metadata
                                    ON model_metadata.model_id = model.id
                LEFT JOIN gateway_profile gateway ON gateway.id = runtime.gateway_profile_id
                LEFT JOIN apim_target_metadata invocation_target
                    ON invocation_target.gateway_profile_id = gateway.id
                {route_join}
                LEFT JOIN effective_gateway_release effective
                    ON effective.gateway_profile_id = runtime.gateway_profile_id
                LEFT JOIN gateway_publication publication
                    ON publication.id =
                        COALESCE(target_route.current_publication_id, effective.publication_id)
                LEFT JOIN LATERAL (
                    SELECT value FROM jsonb_array_elements(
                        COALESCE(publication.desired_spec->'bindings', '[]'::jsonb)
                    ) binding
                    WHERE lower(value->'model'->>'model_key') = lower(model.model_key)
                    LIMIT 1
                ) binding ON TRUE
                {deployment_join}
                WHERE {' AND '.join(conditions)}
                ORDER BY model.is_default DESC, model.created_at
                LIMIT 1""",
                [*join_parameters, *parameters],
            ).fetchone()
        if row is not None:
            publication = row.pop("gateway_model_publication", None)
            effective_id = row.pop("gateway_effective_publication_id", None)
            try:
                revision = _managed_model_request_revision(
                    publication, effective_id, row.get("gateway_config") or {},
                )
            except ValueError:
                return None
            if revision is not None:
                row["gateway_api_revision"] = revision
        return cast(dict[str, Any] | None, row)

    def runtime_diagnostic_contexts(
        self, selections: Sequence[tuple[UUID, UUID, UUID, UUID]],
    ) -> Sequence[dict[str, Any]]:
        if not selections:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT requested.runtime_id, requested.model_id,
                          requested.gateway_profile_id, requested.gateway_deployment_id,
                          to_jsonb(runtime) AS runtime, to_jsonb(provider) AS provider,
                          to_jsonb(gateway) AS gateway, to_jsonb(model) AS model,
                          to_jsonb(metadata) AS model_metadata, to_jsonb(target) AS target,
                          to_jsonb(deployment) AS deployment, to_jsonb(route) AS binding,
                          CASE WHEN route.id IS NULL THEN deployment.kind = 'managed'
                               ELSE route.deployment_scope_mode = 'all' OR EXISTS (
                                   SELECT 1 FROM apim_target_route_binding_deployment selected
                                   WHERE selected.route_binding_id = route.id
                                     AND selected.apim_target_id = requested.gateway_profile_id
                                     AND selected.gateway_deployment_id = deployment.id
                               ) END AS deployment_in_scope,
                          published.value AS published_route
                   FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[], %s::uuid[])
                       AS requested(runtime_id, model_id, gateway_profile_id,
                                    gateway_deployment_id)
                   LEFT JOIN model_runtime runtime ON runtime.id = requested.runtime_id
                   LEFT JOIN managed_model model ON model.id = requested.model_id
                   LEFT JOIN managed_model_metadata metadata ON metadata.model_id = model.id
                   LEFT JOIN model_provider provider ON provider.id = runtime.provider_id
                   LEFT JOIN gateway_profile gateway ON gateway.id = requested.gateway_profile_id
                   LEFT JOIN apim_target_metadata target
                       ON target.gateway_profile_id = gateway.id
                   LEFT JOIN gateway_deployment deployment
                       ON deployment.id = requested.gateway_deployment_id
                   LEFT JOIN apim_target_route_binding route
                       ON route.logical_model_id = model.id AND route.apim_target_id = gateway.id
                   LEFT JOIN effective_gateway_release effective
                       ON effective.gateway_profile_id = gateway.id
                   LEFT JOIN gateway_publication publication
                       ON publication.id =
                           COALESCE(route.current_publication_id, effective.publication_id)
                       AND publication.status = 'active'
                       AND publication.gateway_profile_id = gateway.id
                   LEFT JOIN LATERAL (
                       SELECT value FROM jsonb_array_elements(
                           COALESCE(publication.desired_spec->'bindings', '[]'::jsonb)
                       ) binding
                       WHERE lower(value->'model'->>'model_key') = lower(model.model_key)
                         AND value->>'runtime_id' = requested.runtime_id::text
                       LIMIT 1
                   ) published ON TRUE""",
                tuple([selection[index] for selection in selections] for index in range(4)),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def update_runtime_health(
        self, runtime_id: UUID, status: str, message: str, checked_at: datetime
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE model_runtime SET health_status=%s, health_message=%s,
                   last_checked_at=%s, updated_at=now() WHERE id=%s""",
                (status, message, checked_at, runtime_id),
            )

    def claim_runtime_diagnostic(
        self, runtime_id: UUID, marker: str, checked_at: datetime, before: datetime,
    ) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE model_runtime SET health_status='unknown', health_message=%s,
                   last_checked_at=%s, updated_at=now()
                   WHERE id=%s AND enabled
                     AND (last_checked_at IS NULL OR last_checked_at <= %s)
                   RETURNING id""",
                (marker, checked_at, runtime_id, before),
            ).fetchone()
        return row is not None

    def finish_runtime_diagnostic(
        self, runtime_id: UUID, marker: str, status: str, message: str, checked_at: datetime,
    ) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE model_runtime SET health_status=%s, health_message=%s,
                   last_checked_at=%s, updated_at=now()
                   WHERE id=%s AND health_message=%s RETURNING id""",
                (status, message, checked_at, runtime_id, marker),
            ).fetchone()
        return row is not None
