from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .repository_support import (
    model_router_v3_view,
    model_router_view,
    published_model_router_view,
    router_v3_spec_sha256,
)

_MODEL_ROUTER_REFERENCES = """
SELECT requested.id,
       COALESCE(model.model_key, removed.model_key, usage.model) AS model_key,
       COALESCE(model.display_name, removed.display_name, usage.model) AS display_name,
       COALESCE(provider.name, removed.provider_name, NULLIF(usage.provider, 'unattributed'), '')
           AS provider_name,
       COALESCE(metadata.family_key, removed.family_key, 'generic') AS family_key,
       CASE WHEN model.id IS NULL THEN 'removed'
            WHEN model.enabled THEN 'available' ELSE 'disabled' END AS status
FROM unnest(%s::uuid[]) AS requested(id)
LEFT JOIN managed_model model ON model.id = requested.id
LEFT JOIN model_provider provider ON provider.id = model.provider_id
LEFT JOIN managed_model_metadata metadata ON metadata.model_id = model.id
LEFT JOIN LATERAL (
    SELECT removal.value->>'model_key' AS model_key,
           removal.value->>'display_name' AS display_name,
           binding.value->>'provider_name' AS provider_name,
           binding.value->'model'->>'family_key' AS family_key
    FROM gateway_publication publication
    CROSS JOIN LATERAL jsonb_array_elements(
        COALESCE(publication.desired_spec->'removed_models', '[]'::jsonb)
    ) removal
    LEFT JOIN gateway_publication previous ON previous.id = publication.base_release_id
    LEFT JOIN LATERAL (
        SELECT value FROM jsonb_array_elements(
            COALESCE(previous.desired_spec->'bindings', '[]'::jsonb)
        ) item
        WHERE lower(value->'model'->>'model_key') = lower(removal.value->>'model_key')
        LIMIT 1
    ) binding ON TRUE
    WHERE model.id IS NULL AND publication.status IN ('active', 'superseded')
      AND removal.value->>'model_id' = requested.id::text
    ORDER BY publication.completed_at DESC NULLS LAST, publication.id DESC
    LIMIT 1
) removed ON TRUE
LEFT JOIN LATERAL (
    SELECT model, provider FROM token_usage
    WHERE model_id = requested.id::text AND usage_domain = 'apim'
      AND model IS DISTINCT FROM 'unattributed' AND btrim(model) <> ''
      AND requested.id::text <> model
      AND removed.display_name IS NULL
      AND NOT EXISTS (SELECT 1 FROM managed_model WHERE id = requested.id)
    ORDER BY ts DESC, id DESC LIMIT 1
) usage ON TRUE
WHERE model.id IS NOT NULL OR removed.display_name IS NOT NULL OR usage.model IS NOT NULL
ORDER BY requested.id
"""


class PostgreSqlRouterRepositoryMixin:
    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def model_router_model_references(
        self, model_ids: Sequence[UUID],
    ) -> Sequence[dict[str, Any]]:
        if not model_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                _MODEL_ROUTER_REFERENCES, (sorted(set(model_ids), key=str),),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_model_routers(self, owner_id: UUID) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT *
                   FROM model_router
                         WHERE owner_id = %s AND deleted_at IS NULL
                   ORDER BY updated_at DESC, id""",
                (owner_id,),
            ).fetchall()
        return [model_router_view(row) for row in rows]

    def get_model_router(
        self, router_id: UUID, owner_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                                """SELECT * FROM model_router
                                     WHERE id = %s AND owner_id = %s AND deleted_at IS NULL""",
                (router_id, owner_id),
            ).fetchone()
        return model_router_view(row) if row is not None else None

    def create_model_router(
        self, owner_id: UUID, owner_email: str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        parameters = {
            "id": uuid4(),
            "owner_id": owner_id,
            "created_by": owner_email,
            **values,
            "draft_config": Jsonb(values["definition"]),
        }
        with self._connection() as connection:
            row = connection.execute(
                """INSERT INTO model_router (
                       id, owner_id, name, description, draft_config,
                       created_by, updated_by
                   ) VALUES (
                       %(id)s, %(owner_id)s, %(name)s, %(description)s,
                       %(draft_config)s, %(created_by)s, %(updated_by)s
                   ) ON CONFLICT DO NOTHING
                   RETURNING *""",
                parameters,
            ).fetchone()
        return model_router_view(row) if row is not None else None

    def update_model_router(
        self, router_id: UUID, owner_id: UUID, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        parameters = {
            "id": router_id,
            "owner_id": owner_id,
            **values,
            "draft_config": Jsonb(values["definition"]),
        }
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE model_router SET
                       name = %(name)s,
                       description = %(description)s,
                       draft_config = %(draft_config)s,
                       updated_by = %(updated_by)s,
                       updated_at = now()
                   WHERE id = %(id)s
                     AND owner_id = %(owner_id)s
                                         AND deleted_at IS NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM model_router duplicate
                         WHERE duplicate.owner_id = %(owner_id)s
                           AND duplicate.id <> %(id)s
                                                     AND duplicate.deleted_at IS NULL
                           AND lower(duplicate.name) = lower(%(name)s)
                     )
                   RETURNING *""",
                parameters,
            ).fetchone()
        return model_router_view(row) if row is not None else None

    def publish_model_router(
        self, router_id: UUID, owner_id: UUID, published_by: str,
        *, before_record: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            current = connection.execute(
                """SELECT * FROM model_router
                   WHERE id = %s AND owner_id = %s AND deleted_at IS NULL
                   FOR UPDATE""",
                (router_id, owner_id),
            ).fetchone()
            if current is None:
                return None
            version = int(current["version"]) + 1
            connection.execute(
                """INSERT INTO model_router_revision (
                       router_id, version, config, published_by
                   ) VALUES (%s, %s, %s, %s)""",
                (router_id, version, Jsonb(current["draft_config"]), published_by),
            )
            row = connection.execute(
                """UPDATE model_router SET
                       status = 'published',
                       version = %s,
                       published_config = draft_config,
                       published_at = now(),
                       updated_at = now(),
                       updated_by = %s
                   WHERE id = %s AND owner_id = %s AND deleted_at IS NULL
                   RETURNING *""",
                (version, published_by, router_id, owner_id),
            ).fetchone()
            if row is not None and before_record is not None:
                before_record(model_router_view(row))
        return model_router_view(row) if row is not None else None

    def project_published_model_router(
        self, router_id: UUID, version: int, project: Callable[[dict[str, Any]], bool],
    ) -> bool:
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                """SELECT * FROM model_router
                   WHERE id = %s AND version = %s
                     AND status = 'published' AND deleted_at IS NULL
                     AND published_config IS NOT NULL
                   FOR UPDATE""",
                (router_id, version),
            ).fetchone()
            return project(published_model_router_view(row)) if row is not None else False

    def disable_model_router(
        self, router_id: UUID, owner_id: UUID, changed_by: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE model_router SET
                       status = 'disabled',
                       updated_at = now(),
                       updated_by = %s
                   WHERE id = %s
                     AND owner_id = %s
                                         AND deleted_at IS NULL
                     AND status = 'published'
                     AND published_config IS NOT NULL
                   RETURNING *""",
                (changed_by, router_id, owner_id),
            ).fetchone()
        return model_router_view(row) if row is not None else None

    def delete_model_router(self, router_id: UUID, owner_id: UUID) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                                """UPDATE model_router SET
                                             deleted_at = now(),
                                             updated_at = now()
                                     WHERE id = %s
                                         AND owner_id = %s
                                         AND deleted_at IS NULL
                   RETURNING id""",
                (router_id, owner_id),
            ).fetchone()
        return row is not None

    def list_assigned_model_routers(self, user_id: str) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT router.*
                   FROM model_router router
                   WHERE (
                       lower(router.created_by) = lower(%s)
                       OR EXISTS (
                           SELECT 1
                           FROM user_model_router_access access
                           WHERE access.router_id = router.id
                             AND lower(access.user_id) = lower(%s)
                       )
                   )
                     AND router.status = 'published'
                     AND router.deleted_at IS NULL
                     AND router.published_config IS NOT NULL
                   ORDER BY router.name, router.id""",
                (user_id, user_id),
            ).fetchall()
        return [published_model_router_view(row) for row in rows]

    def list_published_model_routers(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT router.*
                   FROM model_router router
                   WHERE router.status = 'published'
                     AND router.deleted_at IS NULL
                     AND router.published_config IS NOT NULL
                   ORDER BY router.name, router.id"""
            ).fetchall()
        return [published_model_router_view(row) for row in rows]

    def get_callable_model_router(
        self, router_id: UUID, user_id: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT router.*
                   FROM model_router router
                   WHERE router.id = %s
                     AND (
                         lower(router.created_by) = lower(%s)
                         OR EXISTS (
                             SELECT 1
                             FROM user_model_router_access access
                             WHERE access.router_id = router.id
                               AND lower(access.user_id) = lower(%s)
                         )
                     )
                     AND router.status = 'published'
                     AND router.deleted_at IS NULL
                     AND router.published_config IS NOT NULL""",
                (router_id, user_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return published_model_router_view(row)

    def get_published_model_router(
        self, router_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT router.*
                   FROM model_router router
                   WHERE router.id = %s
                     AND router.status = 'published'
                     AND router.deleted_at IS NULL
                     AND router.published_config IS NOT NULL""",
                (router_id,),
            ).fetchone()
        if row is None:
            return None
        return published_model_router_view(row)

    def list_model_routers_v3(self, owner_id: UUID) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                     """SELECT *
                         FROM model_router_spec
                   WHERE owner_id = %s
                     AND deleted_at IS NULL
                   ORDER BY updated_at DESC, id""",
                (owner_id,),
            ).fetchall()
        return [model_router_v3_view(row) for row in rows]

    def get_model_router_v3(
        self, router_id: UUID, owner_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                                """SELECT * FROM model_router_spec
                   WHERE id = %s
                     AND owner_id = %s
                     AND deleted_at IS NULL""",
                (router_id, owner_id),
            ).fetchone()
        return model_router_v3_view(row) if row is not None else None

    def create_model_router_v3(
        self, owner_id: UUID, owner_email: str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        parameters = {
            "id": uuid4(),
            "owner_id": owner_id,
            "created_by": owner_email,
            **values,
            "alias": values["spec"]["identity"]["alias"],
            "draft_spec": Jsonb(values["spec"]),
        }
        with self._connection() as connection:
            row = connection.execute(
                """INSERT INTO model_router_spec (
                       id, owner_id, name, description, alias,
                       draft_spec, created_by, updated_by
                   ) VALUES (
                       %(id)s, %(owner_id)s, %(name)s, %(description)s, %(alias)s,
                       %(draft_spec)s, %(created_by)s, %(updated_by)s
                   ) ON CONFLICT DO NOTHING
                   RETURNING *""",
                parameters,
            ).fetchone()
        return model_router_v3_view(row) if row is not None else None

    def update_model_router_v3(
        self, router_id: UUID, owner_id: UUID, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        parameters = {
            "id": router_id,
            "owner_id": owner_id,
            **values,
            "alias": values["spec"]["identity"]["alias"],
            "draft_spec": Jsonb(values["spec"]),
        }
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE model_router_spec SET
                       name = %(name)s,
                       description = %(description)s,
                       alias = %(alias)s,
                       draft_spec = %(draft_spec)s,
                       updated_by = %(updated_by)s,
                       updated_at = now()
                   WHERE id = %(id)s
                     AND owner_id = %(owner_id)s
                     AND deleted_at IS NULL
                     AND (published_spec IS NULL OR alias = %(alias)s)
                     AND NOT EXISTS (
                         SELECT 1 FROM model_router_spec duplicate
                         WHERE duplicate.owner_id = %(owner_id)s
                           AND duplicate.id <> %(id)s
                           AND duplicate.deleted_at IS NULL
                           AND (
                               lower(duplicate.name) = lower(%(name)s)
                               OR lower(duplicate.alias) = lower(%(alias)s)
                           )
                     )
                   RETURNING *""",
                parameters,
            ).fetchone()
        return model_router_v3_view(row) if row is not None else None

    def publish_model_router_v3(
        self,
        router_id: UUID,
        owner_id: UUID,
        published_by: str,
        compiled_plan: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            current = connection.execute(
                                """SELECT * FROM model_router_spec
                   WHERE id = %s
                     AND owner_id = %s
                     AND deleted_at IS NULL
                   FOR UPDATE""",
                (router_id, owner_id),
            ).fetchone()
            if current is None:
                return None
            expected_spec_sha256 = (
                str(compiled_plan.get("spec_sha256") or "").strip().lower()
            )
            if (
                expected_spec_sha256
                and router_v3_spec_sha256(current["draft_spec"])
                != expected_spec_sha256
            ):
                return None
            version = int(current["version"]) + 1
            connection.execute(
                """INSERT INTO model_router_spec_revision (
                       router_id, version, spec, compiled_plan, published_by
                   ) VALUES (%s, %s, %s, %s, %s)""",
                (
                    router_id,
                    version,
                    Jsonb(current["draft_spec"]),
                    Jsonb(compiled_plan),
                    published_by,
                ),
            )
            row = connection.execute(
                  """UPDATE model_router_spec SET
                       status = 'published',
                       version = %s,
                      published_spec = draft_spec,
                       published_plan = %s,
                       published_at = now(),
                       updated_at = now(),
                       updated_by = %s
                   WHERE id = %s
                     AND owner_id = %s
                     AND deleted_at IS NULL
                   RETURNING *""",
                (version, Jsonb(compiled_plan), published_by, router_id, owner_id),
            ).fetchone()
        return model_router_v3_view(row) if row is not None else None

    def disable_model_router_v3(
        self, router_id: UUID, owner_id: UUID, changed_by: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE model_router_spec SET
                       status = 'disabled',
                       updated_at = now(),
                       updated_by = %s
                   WHERE id = %s
                     AND owner_id = %s
                     AND deleted_at IS NULL
                     AND status = 'published'
                                         AND published_spec IS NOT NULL
                     AND published_plan IS NOT NULL
                   RETURNING *""",
                (changed_by, router_id, owner_id),
            ).fetchone()
        return model_router_v3_view(row) if row is not None else None

    def delete_model_router_v3(self, router_id: UUID, owner_id: UUID) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE model_router_spec SET
                       deleted_at = now(),
                       updated_at = now()
                   WHERE id = %s
                     AND owner_id = %s
                     AND deleted_at IS NULL
                   RETURNING id""",
                (router_id, owner_id),
            ).fetchone()
        return row is not None

    def list_model_router_context_fields(
        self, references: Sequence[tuple[str, int]]
    ) -> Sequence[dict[str, Any]]:
        if not references:
            return []
        values = [(str(name), int(version)) for name, version in references]
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT field.name, field.version, field.value_type,
                          field.source, field.enabled
                   FROM model_router_context_field field
                   JOIN jsonb_to_recordset(%s::jsonb) AS requested(name TEXT, version INTEGER)
                     ON requested.name = field.name
                    AND requested.version = field.version
                   WHERE field.enabled""",
                (Jsonb([{"name": name, "version": version} for name, version in values]),),
            ).fetchall()
        return list(rows)

    def list_model_router_model_facts(
        self, references: Sequence[tuple[UUID, int]]
    ) -> Sequence[dict[str, Any]]:
        if not references:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT facts.model_id, facts.facts_version,
                          facts.logical_model_key, facts.supported_operations,
                          facts.capabilities, facts.foundry_router_eligible,
                          facts.context_window, facts.provider_id, facts.data_zone,
                          (facts.enabled AND model.enabled) AS enabled
                   FROM model_router_model_facts facts
                   JOIN managed_model model ON model.id = facts.model_id
                   JOIN jsonb_to_recordset(%s::jsonb)
                        AS requested(model_id UUID, facts_version INTEGER)
                     ON requested.model_id = facts.model_id
                    AND requested.facts_version = facts.facts_version
                   WHERE facts.enabled
                     AND model.enabled""",
                (
                    Jsonb(
                        [
                            {
                                "model_id": str(model_id),
                                "facts_version": facts_version,
                            }
                            for model_id, facts_version in references
                        ]
                    ),
                ),
            ).fetchall()
        return list(rows)

    def list_model_router_profiles(
        self, references: Sequence[tuple[UUID, int]]
    ) -> Sequence[dict[str, Any]]:
        if not references:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT profile.id, profile.version, profile.name,
                          profile.kind, profile.config, profile.enabled
                   FROM model_router_profile profile
                   JOIN jsonb_to_recordset(%s::jsonb) AS requested(id UUID, version INTEGER)
                     ON requested.id = profile.id
                    AND requested.version = profile.version
                   WHERE profile.enabled""",
                (
                    Jsonb(
                        [
                            {"id": str(profile_id), "version": version}
                            for profile_id, version in references
                        ]
                    ),
                ),
            ).fetchall()
        return list(rows)

    def list_model_router_evaluations(
        self, evaluation_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not evaluation_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id, routing_model_id, routing_model_facts_version,
                          model_ids, passed, enabled
                   FROM model_router_evaluation
                   WHERE id = ANY(%s)
                     AND enabled""",
                (list(evaluation_ids),),
            ).fetchall()
        return list(rows)

    def set_model_router_assignment(
        self, router_id: UUID, user_id: str, assigned: bool, changed_by: str
    ) -> bool:
        with self._connection() as connection, connection.transaction():
            router = connection.execute(
                """SELECT id FROM model_router
                   WHERE id = %s
                     AND status = 'published'
                     AND deleted_at IS NULL
                     AND published_config IS NOT NULL
                   FOR SHARE""",
                (router_id,),
            ).fetchone()
            if router is None:
                return False
            current = connection.execute(
                """SELECT router_id FROM user_model_router_access
                   WHERE user_id = %s ORDER BY router_id
                   FOR UPDATE""",
                (user_id,),
            ).fetchall()
            previous = [row["router_id"] for row in current]
            connection.execute(
                """INSERT INTO user_model_router_policy (user_id, updated_by)
                   VALUES (%s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET
                     updated_at = now(), updated_by = EXCLUDED.updated_by""",
                (user_id, changed_by),
            )
            if assigned:
                connection.execute(
                    """INSERT INTO user_model_router_access (user_id, router_id)
                       VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                    (user_id, router_id),
                )
            else:
                connection.execute(
                    """DELETE FROM user_model_router_access
                       WHERE user_id = %s AND router_id = %s""",
                    (user_id, router_id),
                )
            rows = connection.execute(
                """SELECT router_id FROM user_model_router_access
                   WHERE user_id = %s ORDER BY router_id""",
                (user_id,),
            ).fetchall()
            next_ids = [row["router_id"] for row in rows]
            if previous != next_ids:
                connection.execute(
                    """INSERT INTO user_model_router_access_audit (
                         user_id, previous_router_ids, new_router_ids, changed_by
                       ) VALUES (%s, %s, %s, %s)""",
                    (user_id, previous, next_ids, changed_by),
                )
        return True

    def set_model_router_assignments_bulk(
        self,
        router_id: UUID,
        assignments: Mapping[str, bool],
        changed_by: str,
    ) -> list[str] | None:
        changed_users: list[str] = []
        with self._connection() as connection, connection.transaction():
            router = connection.execute(
                """SELECT id FROM model_router
                   WHERE id = %s
                     AND status = 'published'
                     AND deleted_at IS NULL
                     AND published_config IS NOT NULL
                   FOR SHARE""",
                (router_id,),
            ).fetchone()
            if router is None:
                return None
            for user_id, assigned in assignments.items():
                current = connection.execute(
                    """SELECT router_id FROM user_model_router_access
                       WHERE user_id = %s ORDER BY router_id
                       FOR UPDATE""",
                    (user_id,),
                ).fetchall()
                previous = [row["router_id"] for row in current]
                connection.execute(
                    """INSERT INTO user_model_router_policy (user_id, updated_by)
                       VALUES (%s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET
                         updated_at = now(), updated_by = EXCLUDED.updated_by""",
                    (user_id, changed_by),
                )
                if assigned:
                    connection.execute(
                        """INSERT INTO user_model_router_access (user_id, router_id)
                           VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                        (user_id, router_id),
                    )
                else:
                    connection.execute(
                        """DELETE FROM user_model_router_access
                           WHERE user_id = %s AND router_id = %s""",
                        (user_id, router_id),
                    )
                rows = connection.execute(
                    """SELECT router_id FROM user_model_router_access
                       WHERE user_id = %s ORDER BY router_id""",
                    (user_id,),
                ).fetchall()
                next_ids = [row["router_id"] for row in rows]
                if previous != next_ids:
                    changed_users.append(user_id)
                    connection.execute(
                        """INSERT INTO user_model_router_access_audit (
                             user_id, previous_router_ids, new_router_ids, changed_by
                           ) VALUES (%s, %s, %s, %s)""",
                        (user_id, previous, next_ids, changed_by),
                    )
        return changed_users

    def list_model_router_assignees(self, router_id: UUID) -> Sequence[str]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT user_id FROM user_model_router_access
                   WHERE router_id = %s ORDER BY user_id""",
                (router_id,),
            ).fetchall()
        return [str(row["user_id"]) for row in rows]

    def list_model_router_policy_users(
        self, user_ids: Sequence[str] | None = None
    ) -> Sequence[str]:
        clause = "" if user_ids is None else " WHERE user_id = ANY(%s)"
        params: tuple[Any, ...] = (
            () if user_ids is None else (list(user_ids), list(user_ids))
        )
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT user_id FROM user_model_router_policy{clause}
                    UNION
                    SELECT user_id FROM user_model_router_access{clause}
                    ORDER BY user_id""",
                params,
            ).fetchall()
        return [str(row["user_id"]) for row in rows]

    def get_model_router_run_binding(
        self,
        router_id: UUID,
        router_version: int,
        user_id: str,
        run_id: str,
        *,
        ttl_seconds: int = 1800,
    ) -> UUID | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT selected_model_id
                   FROM model_router_run_binding
                   WHERE router_id = %s
                     AND user_id = %s
                     AND run_id = %s
                     AND updated_at >= now() - make_interval(secs => %s)
                   ORDER BY (router_version = %s) DESC,
                            router_version DESC,
                            updated_at DESC
                   LIMIT 1""",
                (router_id, user_id, run_id, ttl_seconds, router_version),
            ).fetchone()
        return UUID(str(row["selected_model_id"])) if row is not None else None

    def get_model_router_run_bindings_batch(
        self,
        router_id: UUID,
        router_version: int,
        user_id: str,
        run_ids: Sequence[str],
        *,
        ttl_seconds: int = 1800,
    ) -> dict[str, UUID]:
        unique_ids = list(dict.fromkeys(k for k in run_ids if k))
        if not unique_ids:
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT ON (run_id) run_id, selected_model_id
                   FROM model_router_run_binding
                   WHERE router_id = %s
                     AND user_id = %s
                     AND run_id = ANY(%s)
                     AND updated_at >= now() - make_interval(secs => %s)
                   ORDER BY run_id,
                            (router_version = %s) DESC,
                            router_version DESC,
                            updated_at DESC""",
                (router_id, user_id, unique_ids, ttl_seconds, router_version),
            ).fetchall()
        return {
            str(row["run_id"]): UUID(str(row["selected_model_id"]))
            for row in rows
        }

    def save_model_router_run_binding(
        self,
        router_id: UUID,
        router_version: int,
        user_id: str,
        run_id: str,
        selected_model_id: UUID,
        selected_model_name: str,
    ) -> None:
        self.save_model_router_run_bindings_batch(
            router_id,
            router_version,
            user_id,
            [run_id],
            selected_model_id,
            selected_model_name,
        )

    def save_model_router_run_bindings_batch(
        self,
        router_id: UUID,
        router_version: int,
        user_id: str,
        run_ids: Sequence[str],
        selected_model_id: UUID,
        selected_model_name: str,
    ) -> None:
        unique_ids = list(dict.fromkeys(k for k in run_ids if k))
        if not unique_ids:
            return
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO model_router_run_binding (
                     router_id, router_version, user_id, run_id,
                     selected_model_id, selected_model_name
                   ) VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (router_id, router_version, user_id, run_id)
                   DO UPDATE SET
                     selected_model_id = EXCLUDED.selected_model_id,
                     selected_model_name = EXCLUDED.selected_model_name,
                     updated_at = now()""",
                [
                    (
                        router_id,
                        router_version,
                        user_id,
                        run_id,
                        selected_model_id,
                        selected_model_name,
                    )
                    for run_id in unique_ids
                ],
            )

    def save_model_router_invocation(
        self,
        request_id: str,
        router_id: UUID,
        router_name: str,
        router_version: int,
        selected_model_id: UUID,
        selected_model_name: str,
        attempts: Sequence[Mapping[str, Any]],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """DELETE FROM model_router_invocation
                   WHERE router_id = %s
                     AND request_id <> %s
                     AND EXISTS (
                       SELECT 1
                       FROM jsonb_array_elements(attempts) elem
                       WHERE elem->>'request_id' = %s
                     )""",
                (router_id, request_id, request_id),
            )
            connection.execute(
                """INSERT INTO model_router_invocation (
                     request_id, router_id, router_name, router_version,
                     selected_model_id, selected_model_name, attempt_count, attempts
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (request_id) DO UPDATE SET
                     router_id = EXCLUDED.router_id,
                     router_name = EXCLUDED.router_name,
                     router_version = EXCLUDED.router_version,
                     selected_model_id = EXCLUDED.selected_model_id,
                     selected_model_name = EXCLUDED.selected_model_name,
                     attempt_count = EXCLUDED.attempt_count,
                     attempts = EXCLUDED.attempts""",
                (
                    request_id,
                    router_id,
                    router_name,
                    router_version,
                    selected_model_id,
                    selected_model_name,
                    len(attempts),
                    Jsonb(list(attempts)),
                ),
            )

