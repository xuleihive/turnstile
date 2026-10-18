# ruff: noqa: E501 - keep large SQL projections vertically aligned by selected column
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic_core import to_jsonable_python

from ..domain.application_access import (
    GatewayApplicationLedgerState,
    UsageApplicationAttribution,
    application_organization,
)
from ..domain.ledger import validate_application_snapshot
from .repository_budgets import _lock_directory_state


def _json_value(value: object) -> object:
    return to_jsonable_python(value)


class PostgreSqlApplicationRepositoryMixin:
    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def sync_gateway_applications(
        self,
        gateway_profile_id: UUID,
        items: Sequence[Mapping[str, Any]],
        actor: str,
        period_start: date,
        default_token_limit: int,
        default_tokens_per_minute: int,
    ) -> Sequence[dict[str, Any]]:
        discovered_ids = [str(item["apim_subscription_id"]) for item in items]
        rows: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        with self._connection() as connection, connection.transaction():
            for value in items:
                application_id = UUID(str(value["application_id"]))
                subscription_id = UUID(str(value["subscription_id"]))
                existing = connection.execute(
                    "SELECT * FROM gateway_application WHERE id = %s FOR UPDATE",
                    (application_id,),
                ).fetchone()
                application_status = (
                    "retired"
                    if value["state"] == "cancelled"
                    else "suspended"
                    if value["state"] == "suspended"
                    else "active"
                )
                existing_application = dict(existing) if existing is not None else None
                application_type = (
                    existing_application["application_type"]
                    if existing_application is not None
                    and existing_application["application_type"] in {"agent", "delegated_user"}
                    and value["application_type"] == "service"
                    else value["application_type"]
                )
                application_changed = existing_application is None or any(
                    existing_application[field] != expected
                    for field, expected in (
                        ("display_name", value["display_name"]),
                        ("application_type", application_type),
                        ("status", application_status),
                        ("system_managed", value["system_managed"]),
                    )
                )
                before_state: dict[str, object] | None
                if existing_application is None:
                    row = connection.execute(
                        """INSERT INTO gateway_application (
                               id, gateway_profile_id, slug, display_name, description,
                               owner_id, department_id, application_type, status,
                               system_managed, created_by, updated_by
                           ) VALUES (%s, %s, %s, %s, NULL, NULL, NULL, %s, %s, %s, %s, %s)
                           RETURNING *""",
                        (
                            application_id,
                            gateway_profile_id,
                            value["slug"],
                            value["display_name"],
                            application_type,
                            application_status,
                            value["system_managed"],
                            actor,
                            actor,
                        ),
                    ).fetchone()
                    connection.execute(
                        """INSERT INTO gateway_application_budget (
                               period_start, application_id, token_limit,
                               tokens_per_minute, enforce, warning_threshold_percent,
                               updated_by
                           ) VALUES (%s, %s, %s, %s, %s, 80, %s)""",
                        (
                            period_start,
                            application_id,
                            default_token_limit,
                            default_tokens_per_minute,
                            not bool(value["system_managed"]),
                            actor,
                        ),
                    )
                    operation = "adopted"
                    before_state = None
                elif application_changed:
                    row = connection.execute(
                        """UPDATE gateway_application
                           SET display_name = %s, application_type = %s,
                               status = %s, system_managed = %s,
                               updated_by = %s, updated_at = now()
                           WHERE id = %s RETURNING *""",
                        (
                            value["display_name"],
                            application_type,
                            application_status,
                            value["system_managed"],
                            actor,
                            application_id,
                        ),
                    ).fetchone()
                    operation = "updated"
                    before_state = {"application": existing_application}
                else:
                    assert existing_application is not None
                    row = existing_application
                    operation = "subscription_synced"
                    before_state = {"application": existing_application}
                existing_subscription = connection.execute(
                    """SELECT * FROM gateway_application_subscription
                       WHERE gateway_profile_id = %s AND apim_subscription_id = %s
                       FOR UPDATE""",
                    (gateway_profile_id, value["apim_subscription_id"]),
                ).fetchone()
                subscription_before = (
                    dict(existing_subscription) if existing_subscription is not None else None
                )
                subscription_changed = subscription_before is None or any(
                    subscription_before[field] != expected
                    for field, expected in (
                        ("application_id", application_id),
                        ("display_name", value["display_name"]),
                        ("scope_type", value["scope_type"]),
                        ("scope_id", value["scope_id"]),
                        ("state", value["state"]),
                        ("scope_exists", value["scope_exists"]),
                    )
                )
                if before_state is not None:
                    before_state["subscription"] = subscription_before
                subscription = connection.execute(
                    """INSERT INTO gateway_application_subscription (
                           id, application_id, gateway_profile_id,
                           apim_subscription_id, display_name, scope_type, scope_id,
                           state, scope_exists, source, discovered_at, last_synced_at
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (gateway_profile_id, apim_subscription_id) DO UPDATE SET
                           application_id = EXCLUDED.application_id,
                           display_name = EXCLUDED.display_name,
                           scope_type = EXCLUDED.scope_type,
                           scope_id = EXCLUDED.scope_id,
                           state = EXCLUDED.state,
                           scope_exists = EXCLUDED.scope_exists,
                           last_synced_at = EXCLUDED.last_synced_at
                       RETURNING *""",
                    (
                        subscription_id,
                        application_id,
                        gateway_profile_id,
                        value["apim_subscription_id"],
                        value["display_name"],
                        value["scope_type"],
                        value["scope_id"],
                        value["state"],
                        value["scope_exists"],
                        value.get("source", "discovered"),
                        now,
                        now,
                    ),
                ).fetchone()
                after_state = {"application": dict(row), "subscription": dict(subscription)}
                if application_changed or subscription_changed:
                    connection.execute(
                        """INSERT INTO gateway_application_audit (
                               application_id, operation, before_state, after_state, actor
                           ) VALUES (%s, %s, %s, %s, %s)""",
                        (
                            application_id,
                            operation,
                            Jsonb(_json_value(before_state)) if before_state is not None else None,
                            Jsonb(_json_value(after_state)),
                            actor,
                        ),
                    )
                rows.append(dict(row))
            missing = connection.execute(
                """SELECT * FROM gateway_application_subscription
                   WHERE gateway_profile_id = %s
                     AND apim_subscription_id <> ALL(%s::text[])
                     AND (state <> 'cancelled' OR scope_exists)
                   FOR UPDATE""",
                (gateway_profile_id, discovered_ids),
            ).fetchall()
            for prior in missing:
                stale = connection.execute(
                    """UPDATE gateway_application_subscription
                       SET state = 'cancelled', scope_exists = FALSE, last_synced_at = now()
                       WHERE id = %s RETURNING *""",
                    (prior["id"],),
                ).fetchone()
                connection.execute(
                    """INSERT INTO gateway_application_audit (
                           application_id, operation, before_state, after_state, actor
                       ) VALUES (%s, 'subscription_synced', %s, %s, %s)""",
                    (
                        prior["application_id"],
                        Jsonb(_json_value({"subscription": dict(prior)})),
                        Jsonb(_json_value({"subscription": dict(stale)})),
                        actor,
                    ),
                )
        return rows

    def provision_gateway_application(
        self,
        gateway_profile_id: UUID,
        value: Mapping[str, Any],
        actor: str,
        period_start: date,
        default_token_limit: int,
        default_tokens_per_minute: int,
    ) -> dict[str, Any]:
        application_id = UUID(str(value["application_id"]))
        application_subscription_id = UUID(str(value["application_subscription_id"]))
        with self._connection() as connection, connection.transaction():
            existing = connection.execute(
                """SELECT application.*, COALESCE(labels.tags, '{}'::jsonb) AS tags
                   FROM gateway_application application
                   LEFT JOIN gateway_application_tags labels ON labels.application_id = application.id
                   WHERE application.id = %s FOR UPDATE OF application""",
                (application_id,),
            ).fetchone()
            existing_subscription = connection.execute(
                """SELECT * FROM gateway_application_subscription
                   WHERE gateway_profile_id = %s AND apim_subscription_id = %s
                   FOR UPDATE""",
                (gateway_profile_id, value["apim_subscription_id"]),
            ).fetchone()
            if existing is not None or existing_subscription is not None:
                if (
                    existing is not None
                    and existing_subscription is not None
                    and existing_subscription["application_id"] == application_id
                    and existing_subscription["source"] == "managed"
                ):
                    return cast(dict[str, Any], dict(existing))
                raise ValueError("Application or APIM subscription ID already exists")
            application = connection.execute(
                """INSERT INTO gateway_application (
                       id, gateway_profile_id, slug, display_name, description,
                       owner_id, department_id, application_type, status,
                       system_managed, created_by, updated_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, 'active',
                             FALSE, %s, %s)
                   RETURNING *""",
                (
                    application_id,
                    gateway_profile_id,
                    value["slug"],
                    value["display_name"],
                    value.get("description"),
                    actor,
                    value["application_type"],
                    actor,
                    actor,
                ),
            ).fetchone()
            subscription = connection.execute(
                """INSERT INTO gateway_application_subscription (
                       id, application_id, gateway_profile_id,
                       apim_subscription_id, display_name, scope_type, scope_id,
                       state, scope_exists, source
                   ) VALUES (%s, %s, %s, %s, %s, 'product', %s,
                             'active', TRUE, 'managed')
                   RETURNING *""",
                (
                    application_subscription_id,
                    application_id,
                    gateway_profile_id,
                    value["apim_subscription_id"],
                    value["display_name"],
                    value["scope_id"],
                ),
            ).fetchone()
            tags = dict(value.get("tags", {}))
            if tags:
                connection.execute(
                    "INSERT INTO gateway_application_tags (application_id, tags) VALUES (%s, %s)",
                    (application_id, Jsonb(tags)),
                )
            application = {**dict(application), "tags": tags}
            budget_mode = value.get("budget_mode", "tokens")
            token_limit = default_token_limit if budget_mode == "tokens" else None
            usd_limit = value.get("initial_usd_limit")
            connection.execute(
                """INSERT INTO gateway_application_budget (
                       period_start, application_id, budget_mode, token_limit, usd_limit,
                       tokens_per_minute, enforce, warning_threshold_percent,
                       updated_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, 80, %s)""",
                (
                    period_start,
                    application_id,
                    budget_mode,
                    token_limit,
                    usd_limit,
                    default_tokens_per_minute,
                    actor,
                ),
            )
            connection.execute(
                """INSERT INTO gateway_application_audit (
                       application_id, operation, before_state, after_state, actor
                   ) VALUES (%s, 'created', NULL, %s, %s)""",
                (
                    application_id,
                    Jsonb(
                        _json_value(
                            {
                                "application": dict(application),
                                "subscription": dict(subscription),
                                "budget": {
                                    "budget_mode": budget_mode,
                                    "token_limit": token_limit,
                                    "usd_limit": usd_limit,
                                    "tokens_per_minute": default_tokens_per_minute,
                                },
                            }
                        )
                    ),
                    actor,
                ),
            )
        return cast(dict[str, Any], dict(application))

    def list_gateway_applications(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT application.*, COALESCE(labels.tags, '{}'::jsonb) AS tags
                   FROM gateway_application application
                   LEFT JOIN gateway_application_tags labels ON labels.application_id = application.id
                   ORDER BY application.system_managed, application.display_name, application.id"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def get_gateway_application(self, application_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT application.*, COALESCE(labels.tags, '{}'::jsonb) AS tags
                   FROM gateway_application application
                   LEFT JOIN gateway_application_tags labels ON labels.application_id = application.id
                   WHERE application.id = %s""",
                (application_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def list_gateway_application_avatars(
        self, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not application_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT application_id, media_type, updated_at
                   FROM gateway_application_avatar
                   WHERE application_id = ANY(%s)
                   ORDER BY application_id""",
                (list(application_ids),),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def get_gateway_application_avatar(
        self, application_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT application_id, media_type, image_bytes, updated_at
                   FROM gateway_application_avatar
                   WHERE application_id = %s""",
                (application_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def update_gateway_application_organization(
        self, application_id: UUID, organization_id: str | None, actor: str,
        *, department_id: str | None = None, update_department: bool = False,
        owner_id: str | None = None, update_owner: bool = False,
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            directory_active = _lock_directory_state(connection)
            application = connection.execute(
                "SELECT id, department_id, owner_id FROM gateway_application WHERE id = %s FOR UPDATE",
                (application_id,),
            ).fetchone()
            if application is None:
                return None
            stored = connection.execute(
                "SELECT tags FROM gateway_application_tags WHERE application_id = %s FOR UPDATE",
                (application_id,),
            ).fetchone()
            tags = dict(stored["tags"]) if stored is not None else {}
            previous = tags.get("organization_id")
            previous_department = application.get("department_id")
            if not update_department:
                department_id = previous_department if previous == organization_id else None
            previous_owner = application.get("owner_id")
            if not update_owner:
                owner_id = previous_owner if previous == organization_id and previous_department == department_id else None
            if directory_active:
                if organization_id is None and department_id is not None:
                    raise ValueError("Application department requires an organization")
                if organization_id is not None:
                    organization = connection.execute(
                        "SELECT id FROM directory_organization WHERE id=%s AND status='active' FOR SHARE",
                        (organization_id,),
                    ).fetchone()
                    if organization is None:
                        raise ValueError("Unknown application organization")
                if department_id is not None:
                    department = connection.execute(
                        "SELECT organization_id,kind,status FROM directory_unit WHERE id=%s FOR SHARE",
                        (department_id,),
                    ).fetchone()
                    if (department is None or department["kind"] != "department"
                            or department["status"] != "active"):
                        raise ValueError("Unknown application department")
                    if department["organization_id"] != organization_id:
                        raise ValueError("Application department does not belong to the selected organization")
            else:
                application_organization(
                    {"organization_id": organization_id} if organization_id else {}, department_id,
                )
            if previous != organization_id or previous_department != department_id or previous_owner != owner_id:
                if organization_id is None:
                    tags.pop("organization_id", None)
                else:
                    tags["organization_id"] = organization_id
                connection.execute(
                    """INSERT INTO gateway_application_tags (application_id, tags) VALUES (%s, %s)
                       ON CONFLICT (application_id) DO UPDATE SET tags = EXCLUDED.tags""",
                    (application_id, Jsonb(tags)),
                )
                connection.execute(
                    "UPDATE gateway_application SET department_id = %s, owner_id = %s, updated_by = %s, updated_at = now() WHERE id = %s",
                    (department_id, owner_id, actor, application_id),
                )
                connection.execute(
                    """INSERT INTO gateway_application_audit
                       (application_id, operation, before_state, after_state, actor)
                       VALUES (%s, 'updated', %s, %s, %s)""",
                        (application_id, Jsonb({"organization_id": previous,
                                  "department_id": previous_department, "owner_id": previous_owner}),
                         Jsonb({"organization_id": organization_id,
                            "department_id": department_id, "owner_id": owner_id}), actor),
                )
            return {"organization_id": organization_id, "department_id": department_id, "owner_id": owner_id}

    def update_gateway_application_avatar(
        self,
        application_id: UUID,
        media_type: str | None,
        image_bytes: bytes | None,
        actor: str,
    ) -> dict[str, Any] | None:
        if (media_type is None) != (image_bytes is None):
            raise ValueError("Avatar media type and bytes must be set together")
        with self._connection() as connection, connection.transaction():
            application = connection.execute(
                "SELECT id FROM gateway_application WHERE id = %s FOR UPDATE",
                (application_id,),
            ).fetchone()
            if application is None:
                return None
            existing = connection.execute(
                """SELECT media_type, image_bytes, updated_at
                   FROM gateway_application_avatar
                   WHERE application_id = %s FOR UPDATE""",
                (application_id,),
            ).fetchone()
            unchanged = (
                existing is None
                if image_bytes is None
                else existing is not None
                and existing["media_type"] == media_type
                and bytes(existing["image_bytes"]) == image_bytes
            )
            if unchanged:
                return {
                    "application_id": application_id,
                    "media_type": None if existing is None else existing["media_type"],
                    "updated_at": None if existing is None else existing["updated_at"],
                }
            before_state = {
                "configured": existing is not None,
                "media_type": None if existing is None else existing["media_type"],
                "updated_at": None if existing is None else existing["updated_at"],
            }
            if image_bytes is None:
                connection.execute(
                    "DELETE FROM gateway_application_avatar WHERE application_id = %s",
                    (application_id,),
                )
                updated_at = None
            else:
                row = connection.execute(
                    """INSERT INTO gateway_application_avatar (
                           application_id, media_type, image_bytes, updated_by
                       ) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (application_id) DO UPDATE SET
                           media_type = EXCLUDED.media_type,
                           image_bytes = EXCLUDED.image_bytes,
                           updated_by = EXCLUDED.updated_by,
                           updated_at = now()
                       RETURNING updated_at""",
                    (application_id, media_type, image_bytes, actor),
                ).fetchone()
                assert row is not None
                updated_at = row["updated_at"]
            connection.execute(
                """UPDATE gateway_application
                   SET updated_by = %s, updated_at = now()
                   WHERE id = %s""",
                (actor, application_id),
            )
            after_state = {
                "configured": image_bytes is not None,
                "media_type": media_type,
                "updated_at": updated_at,
            }
            connection.execute(
                """INSERT INTO gateway_application_audit (
                       application_id, operation, before_state, after_state, actor
                   ) VALUES (%s, 'updated', %s, %s, %s)""",
                (
                    application_id,
                    Jsonb(_json_value({"avatar": before_state})),
                    Jsonb(_json_value({"avatar": after_state})),
                    actor,
                ),
            )
        return {
            "application_id": application_id,
            "media_type": media_type,
            "updated_at": updated_at,
        }

    def update_gateway_application_budget(
        self,
        application_id: UUID,
        period_start: date,
        value: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            application = connection.execute(
                "SELECT id FROM gateway_application WHERE id = %s FOR UPDATE",
                (application_id,),
            ).fetchone()
            if application is None:
                return None
            existing = connection.execute(
                """SELECT * FROM gateway_application_budget
                   WHERE period_start = %s AND application_id = %s
                   FOR UPDATE""",
                (period_start, application_id),
            ).fetchone()
            budget = connection.execute(
                """INSERT INTO gateway_application_budget (
                       period_start, application_id, budget_mode, token_limit, usd_limit,
                       tokens_per_minute, enforce, warning_threshold_percent,
                       updated_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (period_start, application_id) DO UPDATE SET
                       budget_mode = EXCLUDED.budget_mode,
                       token_limit = EXCLUDED.token_limit,
                       usd_limit = EXCLUDED.usd_limit,
                       tokens_per_minute = EXCLUDED.tokens_per_minute,
                       enforce = EXCLUDED.enforce,
                       warning_threshold_percent = EXCLUDED.warning_threshold_percent,
                       updated_by = EXCLUDED.updated_by,
                       updated_at = GREATEST(clock_timestamp(),
                           gateway_application_budget.updated_at + interval '1 microsecond')
                   RETURNING *""",
                (
                    period_start,
                    application_id,
                    value["budget_mode"],
                    value["token_limit"],
                    value["usd_limit"],
                    value["tokens_per_minute"],
                    value["enforce"],
                    value["warning_threshold_percent"],
                    actor,
                ),
            ).fetchone()
            assert budget is not None
            connection.execute(
                """UPDATE gateway_application
                   SET updated_by = %s, updated_at = now()
                   WHERE id = %s""",
                (actor, application_id),
            )
            connection.execute(
                """INSERT INTO gateway_application_audit (
                       application_id, operation, before_state, after_state, actor
                   ) VALUES (%s, 'budget_updated', %s, %s, %s)""",
                (
                    application_id,
                    Jsonb(_json_value({"budget": dict(existing)}))
                    if existing is not None
                    else None,
                    Jsonb(_json_value({"budget": dict(budget)})),
                    actor,
                ),
            )
        return cast(dict[str, Any], dict(budget))

    def update_gateway_application_model_access(
        self,
        application_id: UUID,
        mode: str,
        model_ids: Sequence[UUID],
        actor: str,
    ) -> dict[str, Any] | None:
        wanted = set(model_ids)
        with self._connection() as connection, connection.transaction():
            application = connection.execute(
                "SELECT id FROM gateway_application WHERE id = %s FOR UPDATE",
                (application_id,),
            ).fetchone()
            if application is None:
                return None
            if wanted:
                available_rows = connection.execute(
                    """SELECT id FROM managed_model
                       WHERE id = ANY(%s) AND enabled
                       FOR SHARE""",
                    (list(wanted),),
                ).fetchall()
                available = {UUID(str(row["id"])) for row in available_rows}
                if available != wanted:
                    raise ValueError("Model access includes an unavailable model")
            existing_policy = connection.execute(
                """SELECT * FROM gateway_application_model_policy
                   WHERE application_id = %s FOR UPDATE""",
                (application_id,),
            ).fetchone()
            existing_access = connection.execute(
                """SELECT model_id FROM gateway_application_model_access
                   WHERE application_id = %s
                   ORDER BY model_id FOR UPDATE""",
                (application_id,),
            ).fetchall()
            connection.execute(
                "DELETE FROM gateway_application_model_access WHERE application_id = %s",
                (application_id,),
            )
            configured = mode == "restricted"
            if configured:
                connection.execute(
                    """INSERT INTO gateway_application_model_policy (
                           application_id, updated_by
                       ) VALUES (%s, %s)
                       ON CONFLICT (application_id) DO UPDATE SET
                           updated_by = EXCLUDED.updated_by,
                           updated_at = now()""",
                    (application_id, actor),
                )
                for model_id in sorted(wanted, key=str):
                    connection.execute(
                        """INSERT INTO gateway_application_model_access (
                               application_id, model_id, created_by
                           ) VALUES (%s, %s, %s)""",
                        (application_id, model_id, actor),
                    )
            else:
                connection.execute(
                    """DELETE FROM gateway_application_model_policy
                       WHERE application_id = %s""",
                    (application_id,),
                )
            connection.execute(
                """UPDATE gateway_application
                   SET updated_by = %s, updated_at = now()
                   WHERE id = %s""",
                (actor, application_id),
            )
            before_state = {
                "configured": existing_policy is not None,
                "model_ids": [row["model_id"] for row in existing_access],
            }
            after_state = {
                "configured": configured,
                "model_ids": sorted(wanted, key=str) if configured else [],
            }
            connection.execute(
                """INSERT INTO gateway_application_audit (
                       application_id, operation, before_state, after_state, actor
                   ) VALUES (%s, 'models_updated', %s, %s, %s)""",
                (
                    application_id,
                    Jsonb(_json_value(before_state)),
                    Jsonb(_json_value(after_state)),
                    actor,
                ),
            )
        return after_state

    def list_gateway_application_subscriptions(
        self, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not application_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_application_subscription
                   WHERE application_id = ANY(%s)
                   ORDER BY application_id, apim_subscription_id""",
                (list(application_ids),),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def list_gateway_application_budgets(
        self, period_start: date, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not application_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_application_budget
                   WHERE period_start = %s AND application_id = ANY(%s)
                   ORDER BY application_id""",
                (period_start, list(application_ids)),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def get_effective_gateway_application_budget(
        self, application_id: UUID, period_start: date,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM gateway_application_budget
                   WHERE application_id = %s AND period_start <= %s
                   ORDER BY period_start DESC LIMIT 1""",
                (application_id, period_start),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def list_gateway_application_ledger_states(
        self, period_start: date, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not application_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                                """SELECT state.*,
                                                    CASE WHEN finalization.snapshot_at >= state.snapshot_at
                                                        THEN finalization.finalized_upper_bound_tokens
                                                        ELSE 0 END AS finalized_upper_bound_tokens,
                                                    CASE WHEN finalization.snapshot_at >= state.snapshot_at
                                                        THEN finalization.finalized_upper_bound_count
                                                        ELSE 0 END AS finalized_upper_bound_count
                                     FROM gateway_application_ledger_state state
                                     LEFT JOIN gateway_application_ledger_finalization_state finalization
                                         ON finalization.period_start = state.period_start
                                        AND finalization.application_id = state.application_id
                                     WHERE state.period_start = %s AND state.application_id = ANY(%s)
                                        AND (finalization.snapshot_at IS NULL
                                                OR finalization.snapshot_at <= state.snapshot_at)
                                     ORDER BY state.application_id""",
                (period_start, list(application_ids)),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def save_gateway_application_ledger_states(
        self, items: Sequence[GatewayApplicationLedgerState]
    ) -> None:
        if not items:
            return
        with self._connection() as connection:
            self._write_application_ledger_states(connection, items)

    def _write_application_ledger_states(
        self, connection: Any, items: Sequence[GatewayApplicationLedgerState],
    ) -> None:
        with connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO gateway_application_ledger_state (
                       period_start, application_id, token_limit, confirmed_tokens,
                      pending_reserved_tokens, pending_reservation_count,
                       finalized_upper_bound_tokens, finalized_upper_bound_count,
                       stale_reservation_count, oldest_reservation_at,
                       available_tokens, snapshot_at
                   ) VALUES (
                       %(period_start)s, %(application_id)s, %(token_limit)s,
                       %(confirmed_tokens)s, %(pending_reserved_tokens)s,
                       %(pending_reservation_count)s, 0, 0, %(stale_reservation_count)s,
                       %(oldest_reservation_at)s, %(available_tokens)s, %(snapshot_at)s
                   ) ON CONFLICT (period_start, application_id) DO UPDATE SET
                       token_limit = EXCLUDED.token_limit,
                       confirmed_tokens = EXCLUDED.confirmed_tokens,
                       pending_reserved_tokens = EXCLUDED.pending_reserved_tokens,
                       pending_reservation_count = EXCLUDED.pending_reservation_count,
                       stale_reservation_count = EXCLUDED.stale_reservation_count,
                       oldest_reservation_at = EXCLUDED.oldest_reservation_at,
                       available_tokens = EXCLUDED.available_tokens,
                                             snapshot_at = EXCLUDED.snapshot_at
                                     WHERE gateway_application_ledger_state.snapshot_at
                                                 <= EXCLUDED.snapshot_at""",
                [item.model_dump(mode="python") for item in items],
            )

    def save_gateway_application_ledger_finalization_states(
        self, items: Sequence[Mapping[str, Any]]
    ) -> None:
        if not items:
            return
        with self._connection() as connection:
            self._write_application_ledger_finalizations(connection, items)

    def _write_application_ledger_finalizations(
        self, connection: Any, items: Sequence[Mapping[str, Any]],
    ) -> None:
        with connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO gateway_application_ledger_finalization_state (
                       period_start, application_id, finalized_upper_bound_tokens,
                       finalized_upper_bound_count, snapshot_at
                   ) VALUES (
                       %(period_start)s, %(application_id)s,
                       %(finalized_upper_bound_tokens)s,
                       %(finalized_upper_bound_count)s, %(snapshot_at)s
                   ) ON CONFLICT (period_start, application_id) DO UPDATE SET
                       finalized_upper_bound_tokens =
                         EXCLUDED.finalized_upper_bound_tokens,
                       finalized_upper_bound_count =
                         EXCLUDED.finalized_upper_bound_count,
                       snapshot_at = EXCLUDED.snapshot_at
                   WHERE gateway_application_ledger_finalization_state.snapshot_at
                         <= EXCLUDED.snapshot_at""",
                items,
            )

    def save_gateway_application_ledger_snapshot(
        self, states: Sequence[GatewayApplicationLedgerState],
        sidecars: Sequence[Mapping[str, Any]],
    ) -> None:
        validate_application_snapshot(states, sidecars)
        if not states:
            return
        with self._connection() as connection, connection.transaction():
            self._write_application_ledger_finalizations(connection, sidecars)
            self._write_application_ledger_states(connection, states)

    def list_gateway_application_model_access(
        self, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not application_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT policy.application_id,
                          policy.updated_by, policy.updated_at,
                          access.model_id
                   FROM gateway_application_model_policy policy
                   LEFT JOIN gateway_application_model_access access
                     ON access.application_id = policy.application_id
                   WHERE policy.application_id = ANY(%s)
                   ORDER BY policy.application_id, access.model_id""",
                (list(application_ids),),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def gateway_application_usage(
        self,
        period_start: date,
        period_end: date,
        application_ids: Sequence[UUID],
    ) -> Sequence[dict[str, Any]]:
        if not application_ids:
            return []
        with self._connection() as connection:
                 rows = connection.execute(
                  """WITH canonical_correlations AS (
                    SELECT DISTINCT attribution.application_id,
                        usage.correlation_id
                    FROM token_usage_application_attribution attribution
                    JOIN token_usage usage ON usage.id = attribution.usage_id
                    WHERE usage.usage_domain = 'apim'
                                     ), combined AS (
                                              SELECT attribution.application_id,
                                                  usage.correlation_id,
                                                  usage.ts, usage.status_code,
                                                  usage.input_tokens, usage.cached_tokens,
                                                  usage.cache_write_tokens,
                                                                                                    usage.output_tokens, usage.estimated_cost,
                                                                                                    budget_usage_usd_cost(
                                                                                                        usage.usd_cost, usage.estimated, usage.input_tokens,
                                                                                                        usage.cached_tokens, usage.cache_write_tokens,
                                                                                                        usage.output_tokens, usage.input_price_per_million,
                                                                                                        usage.cached_price_per_million,
                                                                                                        usage.cache_write_price_per_million,
                                                                                                        usage.output_price_per_million
                                                                                                    ) AS usd_cost
                                         FROM token_usage_application_attribution attribution
                                         JOIN token_usage usage ON usage.id = attribution.usage_id
                                         WHERE attribution.application_id = ANY(%s)
                                             AND usage.ts >= %s AND usage.ts < %s
                                             AND usage.usage_domain = 'apim'
                                         UNION ALL
                                                                                 SELECT recovery.scope_id::UUID, recovery.correlation_id,
                                                                                                                recovery.reservation_created_at,
                                                                                                                recovery.status_code,
                                                                                                                COALESCE(
                                                                                                                    recovery.input_tokens,
                                                                                                                    recovery.total_tokens
                                                                                                                ),
                                                                                                                0::BIGINT, 0::BIGINT,
                                                                                                                COALESCE(recovery.output_tokens, 0),
                                                                                                                0::DOUBLE PRECISION,
                                                                                                                NULL::NUMERIC(30,14)
                                                                                 FROM budget_reservation_finalization_effective recovery
                                                                                 WHERE recovery.scope_type = 'application'
                                                                                     AND recovery.finalization_kind
                                                                                         <> 'unverified_upper_bound'
                                                                                         AND recovery.scope_id::UUID = ANY(%s)
                                                                                         AND recovery.period_start = %s
                                             AND NOT EXISTS (
                                                 SELECT 1 FROM canonical_correlations canonical
                                                 WHERE canonical.application_id =
                                                                                                             recovery.scope_id::UUID
                                                   AND canonical.correlation_id =
                                                       recovery.correlation_id
                                             )
                                     )
                                     SELECT application_id,
                                                    COUNT(*)::BIGINT AS request_count,
                                                    COUNT(*) FILTER (
                                                        WHERE status_code >= 400
                                                    )::BIGINT
                            AS denied_request_count,
                                                    COALESCE(SUM(input_tokens), 0)::BIGINT
                            AS input_tokens,
                                                    COALESCE(SUM(cached_tokens), 0)::BIGINT
                            AS cached_tokens,
                                                    COALESCE(SUM(cache_write_tokens), 0)::BIGINT
                            AS cache_write_tokens,
                                                    COALESCE(SUM(output_tokens), 0)::BIGINT
                            AS output_tokens,
                          COALESCE(SUM(
                                                        input_tokens + cached_tokens + output_tokens
                          ), 0)::BIGINT AS total_tokens,
                                                    COALESCE(
                                                        SUM(estimated_cost), 0
                                                    )::DOUBLE PRECISION
                            AS estimated_cost,
                                                    SUM(usd_cost)::NUMERIC(30,14) AS usd_cost,
                                                    COUNT(*) FILTER (WHERE usd_cost IS NULL)::BIGINT
                                                        AS unpriced_usage_count,
                                                    MAX(ts) AS last_request_at
                                     FROM combined
                                     GROUP BY application_id
                                     ORDER BY application_id""",
                                (
                                        list(application_ids),
                                        period_start,
                                        period_end,
                                        list(application_ids),
                                        period_start,
                                ),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def gateway_application_user_usage(
        self,
        period_start: date,
        period_end: date,
        application_id: UUID,
        limit: int = 100,
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """WITH canonical_correlations AS (
                     SELECT DISTINCT attribution.application_id,
                            usage.correlation_id
                     FROM token_usage_application_attribution attribution
                     JOIN token_usage usage ON usage.id = attribution.usage_id
                     WHERE usage.usage_domain = 'apim'
                                     ), combined AS (
                                         SELECT attribution.actor_type,
                                                attribution.actor_id,
                                                attribution.person_id,
                                                usage.user_ref,
                                                usage.ts, usage.status_code,
                                                usage.input_tokens,
                                                usage.cached_tokens,
                                                usage.output_tokens,
                                                usage.estimated_cost
                                         FROM token_usage_application_attribution attribution
                                         JOIN token_usage usage ON usage.id = attribution.usage_id
                                         WHERE attribution.application_id = %s
                                           AND usage.ts >= %s AND usage.ts < %s
                                           AND usage.usage_domain = 'apim'
                                         UNION ALL
                                         SELECT 'system'::TEXT,
                                                'system:recovered'::TEXT,
                                                NULL::TEXT,
                                                'Recovered APIM telemetry'::TEXT,
                                                                                                recovery.reservation_created_at,
                                                recovery.status_code,
                                                                                                COALESCE(
                                                                                                    recovery.input_tokens,
                                                                                                    recovery.total_tokens
                                                                                                ),
                                                0::BIGINT,
                                                                                                COALESCE(recovery.output_tokens, 0),
                                                0::DOUBLE PRECISION
                                                                                 FROM budget_reservation_finalization_effective recovery
                                                                                 WHERE recovery.scope_type = 'application'
                                                                                     AND recovery.finalization_kind
                                                                                         <> 'unverified_upper_bound'
                                                                                     AND recovery.scope_id::UUID = %s
                                           AND recovery.period_start
                                               = %s
                                           AND NOT EXISTS (
                                             SELECT 1 FROM canonical_correlations canonical
                                               WHERE canonical.application_id =
                                                   recovery.scope_id::UUID
                                               AND canonical.correlation_id =
                                                   recovery.correlation_id
                                           )
                                     ), user_usage AS (
                                         SELECT COALESCE(
                                                            person_id,
                                                            actor_id
                                                        ) AS user_id,
                                                        CASE
                                                            WHEN actor_type = 'person'
                                                                THEN COALESCE(
                                                                    MAX(
                                                                        NULLIF(
                                                                            user_ref,
                                                                            'unattributed'
                                                                        )
                                                                    ),
                                                                    person_id,
                                                                    actor_id
                                                                )
                                                            ELSE actor_id
                                                        END AS display_name,
                                                        actor_type,
                                                        person_id,
                                                        COUNT(*)::BIGINT AS request_count,
                                                        COUNT(*) FILTER (
                                                            WHERE status_code >= 400
                                                        )::BIGINT AS denied_request_count,
                                                        COALESCE(SUM(
                                                            input_tokens
                                                            + cached_tokens
                                                            + output_tokens
                                                        ), 0)::BIGINT AS total_tokens,
                                                        COALESCE(
                                                            SUM(estimated_cost), 0
                                                        )::DOUBLE PRECISION AS estimated_cost,
                                                        MAX(ts) AS last_request_at
                                         FROM combined
                                         GROUP BY actor_type, actor_id, person_id
                                     )
                                     SELECT user_usage.*, COUNT(*) OVER ()::BIGINT AS user_count
                                     FROM user_usage
                                     ORDER BY request_count DESC, total_tokens DESC,
                                                        lower(display_name), user_id
                                     LIMIT %s""",
                (
                    application_id,
                    period_start,
                    period_end,
                    application_id,
                    period_start,
                    limit,
                ),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def list_gateway_application_audit(
        self, application_id: UUID, limit: int = 100
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_application_audit
                   WHERE application_id = %s
                   ORDER BY created_at DESC, id DESC
                   LIMIT %s""",
                (application_id, limit),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def gateway_application_attribution_map(
        self, gateway_profile_id: UUID
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT application.id AS application_id,
                          application.display_name AS application_name,
                          application.application_type,
                          application.status AS application_status,
                          subscription.id AS application_subscription_id,
                          subscription.apim_subscription_id,
                          subscription.state AS subscription_state,
                          subscription.scope_exists
                   FROM gateway_application application
                   JOIN gateway_application_subscription subscription
                     ON subscription.application_id = application.id
                   WHERE application.gateway_profile_id = %s
                   ORDER BY subscription.apim_subscription_id""",
                (gateway_profile_id,),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def gateway_application_ledger_snapshot(
        self, period_start: date, period_end: date
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """WITH canonical_correlations AS (
                     SELECT DISTINCT attribution.application_id,
                            usage.correlation_id
                     FROM token_usage_application_attribution attribution
                     JOIN token_usage usage ON usage.id = attribution.usage_id
                     WHERE usage.usage_domain = 'apim'
                   ), confirmed AS (
                     SELECT attribution.application_id,
                            COALESCE(SUM(
                              usage.input_tokens + usage.cached_tokens + usage.output_tokens
                                                        ), 0)::BIGINT AS confirmed_tokens,
                                                        CASE WHEN count(*) FILTER (WHERE budget_usage_usd_cost(
                                                            usage.usd_cost, usage.estimated, usage.input_tokens,
                                                            usage.cached_tokens, usage.cache_write_tokens, usage.output_tokens,
                                                            usage.input_price_per_million, usage.cached_price_per_million,
                                                            usage.cache_write_price_per_million, usage.output_price_per_million
                                                        ) IS NULL) > 0 THEN NULL ELSE COALESCE(SUM(budget_usage_usd_cost(
                                                            usage.usd_cost, usage.estimated, usage.input_tokens,
                                                            usage.cached_tokens, usage.cache_write_tokens, usage.output_tokens,
                                                            usage.input_price_per_million, usage.cached_price_per_million,
                                                            usage.cache_write_price_per_million, usage.output_price_per_million
                                                        )), 0)::NUMERIC(30,14) END AS confirmed_usd,
                                                        count(*) FILTER (WHERE budget_usage_usd_cost(
                                                            usage.usd_cost, usage.estimated, usage.input_tokens,
                                                            usage.cached_tokens, usage.cache_write_tokens, usage.output_tokens,
                                                            usage.input_price_per_million, usage.cached_price_per_million,
                                                            usage.cache_write_price_per_million, usage.output_price_per_million
                                                        ) IS NULL)::BIGINT AS unpriced_usage_count
                     FROM token_usage_application_attribution attribution
                     JOIN token_usage usage ON usage.id = attribution.usage_id
                     WHERE usage.ts >= %s AND usage.ts < %s
                       AND usage.usage_domain = 'apim'
                     GROUP BY attribution.application_id
                                     ), recovered AS (
                                         SELECT recovery.scope_id::UUID AS application_id,
                                                        COALESCE(
                                                            SUM(recovery.total_tokens),
                                                            0
                                                        )::BIGINT AS recovered_tokens,
                                                        count(*)::BIGINT AS recovered_count
                                         FROM budget_reservation_finalization_effective recovery
                                         WHERE recovery.scope_type = 'application'
                                             AND recovery.finalization_kind
                                                 <> 'unverified_upper_bound'
                                             AND recovery.period_start = %s
                                             AND NOT EXISTS (
                                                 SELECT 1 FROM canonical_correlations canonical
                                                 WHERE canonical.application_id =
                                                       recovery.scope_id::UUID
                                                   AND canonical.correlation_id =
                                                       recovery.correlation_id
                                             )
                                         GROUP BY recovery.scope_id
                   )
                   SELECT application.id AS application_id,
                          application.status,
                          budget.budget_mode, budget.token_limit, budget.usd_limit,
                          budget.tokens_per_minute,
                          budget.enforce, budget.updated_at AS quota_updated_at,
                          CASE WHEN EXISTS (
                            SELECT 1 FROM budget_evidence_policy
                            WHERE version = 2 AND effective_at <= CURRENT_TIMESTAMP
                              AND effective_at < %s::timestamp AT TIME ZONE 'UTC'
                          ) THEN budget_scope_confirmed_tokens('application', application.id::text,
                                                   %s, %s)
                          ELSE (COALESCE(confirmed.confirmed_tokens, 0)
                              + COALESCE(recovered.recovered_tokens, 0))::BIGINT
                                                    END AS confirmed_tokens,
                                                    CASE WHEN budget.budget_mode <> 'usd' THEN NULL
                                                             WHEN EXISTS (SELECT 1 FROM budget_evidence_policy
                                                                     WHERE version = 2 AND effective_at <= CURRENT_TIMESTAMP
                                                                         AND effective_at < %s::timestamp AT TIME ZONE 'UTC')
                                                             THEN budget_scope_confirmed_usd('application', application.id::text,
                                                                         %s)
                                                             WHEN COALESCE(confirmed.unpriced_usage_count, 0) > 0
                                                                 OR COALESCE(recovered.recovered_count, 0) > 0 THEN NULL
                                                             ELSE COALESCE(confirmed.confirmed_usd, 0)::NUMERIC(30,14)
                                                    END AS confirmed_usd,
                                                    (budget.budget_mode <> 'usd' OR (
                                                            CASE WHEN EXISTS (SELECT 1 FROM budget_evidence_policy
                                                                    WHERE version = 2 AND effective_at <= CURRENT_TIMESTAMP
                                                                        AND effective_at < %s::timestamp AT TIME ZONE 'UTC')
                                                            THEN budget_scope_confirmed_usd('application', application.id::text,
                                                                        %s) IS NOT NULL
                                                            ELSE COALESCE(confirmed.unpriced_usage_count, 0) = 0
                                                                AND COALESCE(recovered.recovered_count, 0) = 0
                                                            END
                                                    )) AS usd_cost_available
                   FROM gateway_application_budget budget
                   JOIN gateway_application application
                     ON application.id = budget.application_id
                   LEFT JOIN confirmed ON confirmed.application_id = application.id
                   LEFT JOIN recovered ON recovered.application_id = application.id
                   WHERE budget.period_start = %s
                   ORDER BY application.id""",
                (
                    period_start,
                    period_end,
                    period_start,
                    period_end,
                    period_start,
                    period_end,
                    period_end,
                    period_start,
                    period_end,
                    period_start,
                    period_start,
                ),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def gateway_application_ledger_applications(
        self, period_start: date
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT application.id AS application_id,
                          application.status,
                          budget.budget_mode, budget.token_limit, budget.usd_limit,
                          budget.tokens_per_minute,
                          budget.enforce, budget.updated_at AS quota_updated_at
                   FROM gateway_application_budget budget
                   JOIN gateway_application application
                     ON application.id = budget.application_id
                   WHERE budget.period_start = %s
                   ORDER BY application.id""",
                (period_start,),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def gateway_application_model_ledger_snapshot(
        self,
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT application.id AS application_id,
                          (policy.application_id IS NOT NULL) AS configured,
                          COALESCE(
                            array_agg(
                              access.model_id::text ORDER BY access.model_id
                            ) FILTER (WHERE access.model_id IS NOT NULL),
                            ARRAY[]::text[]
                          ) AS model_uuids,
                          COALESCE(
                            array_agg(
                              model.model_key ORDER BY model.model_key
                            ) FILTER (WHERE model.model_key IS NOT NULL),
                            ARRAY[]::text[]
                          ) AS model_keys
                                     FROM gateway_application application
                                     LEFT JOIN gateway_application_model_policy policy
                                         ON policy.application_id = application.id
                   LEFT JOIN gateway_application_model_access access
                                         ON access.application_id = application.id
                   LEFT JOIN managed_model model ON model.id = access.model_id
                                     GROUP BY application.id, policy.application_id
                                     ORDER BY application.id"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def gateway_application_ledger_mappings(
        self,
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT application.gateway_profile_id,
                          application.id AS application_id,
                          application.slug AS application_slug,
                          application.display_name AS application_name,
                          application.application_type,
                          application.status AS application_status,
                          application.department_id,
                          COALESCE(labels.tags, '{}'::jsonb) AS tags,
                          subscription.apim_subscription_id,
                          subscription.state AS subscription_state,
                          subscription.scope_exists
                   FROM gateway_application application
                   JOIN gateway_application_subscription subscription
                     ON subscription.application_id = application.id
                                     LEFT JOIN gateway_application_tags labels ON labels.application_id = application.id
                   ORDER BY application.gateway_profile_id,
                            subscription.apim_subscription_id"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def roll_forward_gateway_application_budgets(self, period_start: date, actor: str) -> int:
        with self._connection() as connection, connection.transaction():
            rows = connection.execute(
                """WITH latest AS (
                    SELECT DISTINCT ON (budget.application_id)
                        budget.application_id, budget.budget_mode,
                        budget.token_limit, budget.usd_limit,
                            budget.tokens_per_minute, budget.enforce,
                            budget.warning_threshold_percent
                     FROM gateway_application_budget budget
                     JOIN gateway_application application
                       ON application.id = budget.application_id
                     WHERE budget.period_start < %s
                       AND application.status <> 'retired'
                     ORDER BY budget.application_id, budget.period_start DESC
                   )
                                     INSERT INTO gateway_application_budget (
                                         period_start, application_id, budget_mode, token_limit, usd_limit,
                     tokens_per_minute, enforce, warning_threshold_percent,
                     updated_by
                   )
                                     SELECT %s, application_id, budget_mode, token_limit, usd_limit,
                                                    tokens_per_minute,
                          enforce, warning_threshold_percent, %s
                   FROM latest
                   ON CONFLICT (period_start, application_id) DO NOTHING
                   RETURNING application_id""",
                (period_start, period_start, actor),
            ).fetchall()
        return len(rows)

    def _write_usage_application_attribution(
        self,
        connection: Any,
        usage_id: str,
        attribution: UsageApplicationAttribution,
    ) -> None:
        values = {**attribution.model_dump(), "usage_id": usage_id}
        row = connection.execute(
            """INSERT INTO token_usage_application_attribution AS existing (
                   usage_id, application_id, application_subscription_id,
                   application_name_snapshot, apim_subscription_id,
                   actor_type, actor_id, person_id, application_admission
               ) VALUES (
                   %(usage_id)s, %(application_id)s, %(application_subscription_id)s,
                   %(application_name_snapshot)s, %(apim_subscription_id)s,
                   %(actor_type)s, %(actor_id)s, %(person_id)s,
                   %(application_admission)s
               ) ON CONFLICT (usage_id) DO UPDATE SET
                   application_admission = COALESCE(
                     existing.application_admission, EXCLUDED.application_admission
                   )
               WHERE existing.application_id = EXCLUDED.application_id
                 AND existing.application_subscription_id = EXCLUDED.application_subscription_id
                 AND existing.apim_subscription_id = EXCLUDED.apim_subscription_id
                 AND existing.actor_type = EXCLUDED.actor_type
                 AND existing.actor_id = EXCLUDED.actor_id
                 AND existing.person_id IS NOT DISTINCT FROM EXCLUDED.person_id
               RETURNING usage_id""",
            values,
        ).fetchone()
        if row is None:
            raise ValueError("Usage application attribution is immutable")
