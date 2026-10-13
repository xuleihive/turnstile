from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic_core import to_jsonable_python

from ..domain.application_access import GatewayApplicationLedgerState, UsageApplicationAttribution


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
                "SELECT * FROM gateway_application WHERE id = %s FOR UPDATE",
                (application_id,),
            ).fetchone()
            existing_subscription = connection.execute(
                """SELECT * FROM gateway_application_subscription
                   WHERE gateway_profile_id = %s AND apim_subscription_id = %s
                   FOR UPDATE""",
                (gateway_profile_id, value["apim_subscription_id"]),
            ).fetchone()
            if existing_subscription is not None:
                if (
                    existing is not None
                    and existing_subscription["application_id"] == application_id
                    and existing_subscription["source"] == "managed"
                ):
                    return cast(dict[str, Any], dict(existing))
                raise ValueError("Application or APIM subscription ID already exists")
            created_application = existing is None
            if existing is not None:
                if (
                    existing["gateway_profile_id"] != gateway_profile_id
                    or existing["slug"] != value["slug"]
                    or bool(existing["system_managed"])
                    or value.get("owner_user_id") is not None
                    and existing["owner_user_id"] != value.get("owner_user_id")
                    or value.get("tenant_id") is not None
                    and existing["tenant_id"] != value.get("tenant_id")
                ):
                    raise ValueError("Application owner or identity does not match")
                application = dict(existing)
            else:
                application = connection.execute(
                    """INSERT INTO gateway_application (
                           id, gateway_profile_id, slug, display_name, description,
                           owner_id, department_id, application_type, status,
                           system_managed, created_by, updated_by, owner_user_id,
                           tenant_id, organization_unit_id
                       ) VALUES (
                           %s, %s, %s, %s, %s, %s, %s, %s, 'active', FALSE,
                           %s, %s, %s,
                           COALESCE(%s, (SELECT id FROM tenant WHERE is_primary)), %s
                       ) RETURNING *""",
                    (
                        application_id,
                        gateway_profile_id,
                        value["slug"],
                        value.get("application_display_name") or value["display_name"],
                        value.get("description"),
                        value.get("owner_email") or actor,
                        str(value["organization_unit_id"])
                        if value.get("organization_unit_id")
                        else None,
                        value["application_type"],
                        actor,
                        actor,
                        value.get("owner_user_id"),
                        value.get("tenant_id"),
                        value.get("organization_unit_id"),
                    ),
                ).fetchone()
                assert application is not None
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
            if created_application:
                connection.execute(
                    """INSERT INTO gateway_application_budget (
                           period_start, application_id, token_limit,
                           tokens_per_minute, enforce, warning_threshold_percent,
                           updated_by
                       ) VALUES (%s, %s, %s, %s, TRUE, 80, %s)""",
                    (
                        period_start,
                        application_id,
                        default_token_limit,
                        default_tokens_per_minute,
                        actor,
                    ),
                )
            connection.execute(
                """INSERT INTO gateway_application_audit (
                       application_id, operation, before_state, after_state, actor
                   ) VALUES (%s, %s, NULL, %s, %s)""",
                (
                    application_id,
                    "created" if created_application else "subscription_created",
                    Jsonb(
                        _json_value(
                            {
                                "application": dict(application),
                                "subscription": dict(subscription),
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
                "SELECT * FROM gateway_application ORDER BY system_managed, display_name, id"
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def get_gateway_application(self, application_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_application WHERE id = %s",
                (application_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def list_gateway_applications_for_owner(
        self, owner_user_id: UUID
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_application
                   WHERE owner_user_id = %s AND system_managed = FALSE
                   ORDER BY display_name, id""",
                (owner_user_id,),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def update_gateway_application_owner(
        self,
        application_id: UUID,
        owner_user_id: UUID | None,
        tenant_id: UUID | None,
        organization_unit_id: UUID | None,
        owner_email: str | None,
        actor: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            existing = connection.execute(
                "SELECT * FROM gateway_application WHERE id = %s FOR UPDATE",
                (application_id,),
            ).fetchone()
            if existing is None:
                return None
            if bool(existing["system_managed"]):
                raise ValueError("System-managed application ownership cannot be changed")
            if tenant_id is not None and existing["tenant_id"] != tenant_id:
                raise ValueError("Application and owner must belong to the same tenant")
            before = {
                "owner_user_id": existing["owner_user_id"],
                "owner_id": existing["owner_id"],
                "organization_unit_id": existing["organization_unit_id"],
            }
            row = connection.execute(
                """UPDATE gateway_application
                   SET owner_user_id = %s, organization_unit_id = %s,
                       department_id = %s, owner_id = %s,
                       updated_by = %s, updated_at = now()
                   WHERE id = %s RETURNING *""",
                (
                    owner_user_id,
                    organization_unit_id,
                    str(organization_unit_id) if organization_unit_id else None,
                    owner_email,
                    actor,
                    application_id,
                ),
            ).fetchone()
            assert row is not None
            connection.execute(
                """INSERT INTO gateway_application_audit (
                       application_id, operation, before_state, after_state, actor
                   ) VALUES (%s, 'owner_changed', %s, %s, %s)""",
                (
                    application_id,
                    Jsonb(_json_value(before)),
                    Jsonb(
                        _json_value(
                            {
                                "owner_user_id": owner_user_id,
                                "owner_id": owner_email,
                                "organization_unit_id": organization_unit_id,
                            }
                        )
                    ),
                    actor,
                ),
            )
        return cast(dict[str, Any], dict(row))

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

    def get_gateway_application_avatar(self, application_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT application_id, media_type, image_bytes, updated_at
                   FROM gateway_application_avatar
                   WHERE application_id = %s""",
                (application_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

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
                       period_start, application_id, token_limit,
                       tokens_per_minute, enforce, warning_threshold_percent,
                       updated_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (period_start, application_id) DO UPDATE SET
                       token_limit = EXCLUDED.token_limit,
                       tokens_per_minute = EXCLUDED.tokens_per_minute,
                       enforce = EXCLUDED.enforce,
                       warning_threshold_percent = EXCLUDED.warning_threshold_percent,
                       updated_by = EXCLUDED.updated_by,
                       updated_at = now()
                   RETURNING *""",
                (
                    period_start,
                    application_id,
                    value["token_limit"],
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

    def update_gateway_application_subscription_state(
        self,
        application_subscription_id: UUID,
        state: str,
        actor: str,
    ) -> dict[str, Any] | None:
        if state not in {"active", "suspended", "cancelled"}:
            raise ValueError("Invalid APIM subscription state")
        with self._connection() as connection, connection.transaction():
            existing = connection.execute(
                """SELECT * FROM gateway_application_subscription
                   WHERE id = %s FOR UPDATE""",
                (application_subscription_id,),
            ).fetchone()
            if existing is None:
                return None
            if existing["state"] == state:
                return cast(dict[str, Any], dict(existing))
            row = connection.execute(
                """UPDATE gateway_application_subscription
                   SET state = %s, last_synced_at = now()
                   WHERE id = %s RETURNING *""",
                (state, application_subscription_id),
            ).fetchone()
            assert row is not None
            connection.execute(
                """INSERT INTO gateway_application_audit (
                       application_id, operation, before_state, after_state, actor
                   ) VALUES (%s, 'subscription_cancelled', %s, %s, %s)""",
                (
                    existing["application_id"],
                    Jsonb(_json_value({"subscription": dict(existing)})),
                    Jsonb(_json_value({"subscription": dict(row)})),
                    actor,
                ),
            )
        return cast(dict[str, Any], dict(row))

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

    def save_gateway_application_ledger_states(
        self,
        states: Sequence[GatewayApplicationLedgerState],
    ) -> None:
        if not states:
            return
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO gateway_application_ledger_state AS existing (
                     period_start, application_id, token_limit, confirmed_tokens,
                     pending_reserved_tokens, pending_reservation_count,
                     finalized_upper_bound_tokens,
                     finalized_upper_bound_count, stale_reservation_count, oldest_reservation_at,
                     available_tokens, snapshot_at)
                   SELECT * FROM jsonb_to_recordset(%s::JSONB) AS incoming (
                     period_start DATE, application_id UUID, token_limit BIGINT,
                     confirmed_tokens BIGINT, pending_reserved_tokens BIGINT,
                     pending_reservation_count INTEGER, finalized_upper_bound_tokens BIGINT,
                     finalized_upper_bound_count INTEGER, stale_reservation_count INTEGER,
                                         oldest_reservation_at TIMESTAMPTZ, available_tokens BIGINT,
                                         snapshot_at TIMESTAMPTZ)
                   ON CONFLICT (period_start, application_id) DO UPDATE SET
                                         token_limit = EXCLUDED.token_limit,
                                         confirmed_tokens = EXCLUDED.confirmed_tokens,
                     pending_reserved_tokens = EXCLUDED.pending_reserved_tokens,
                     pending_reservation_count = EXCLUDED.pending_reservation_count,
                     finalized_upper_bound_tokens = EXCLUDED.finalized_upper_bound_tokens,
                     finalized_upper_bound_count = EXCLUDED.finalized_upper_bound_count,
                     stale_reservation_count = EXCLUDED.stale_reservation_count,
                     oldest_reservation_at = EXCLUDED.oldest_reservation_at,
                     available_tokens = EXCLUDED.available_tokens,
                     snapshot_at = EXCLUDED.snapshot_at
                   WHERE existing.snapshot_at < EXCLUDED.snapshot_at""",
                (Jsonb([state.model_dump(mode="json") for state in states]),),
            )

    def list_gateway_application_ledger_states(
        self,
        period_start: date,
        application_ids: Sequence[UUID],
    ) -> Sequence[dict[str, Any]]:
        if not application_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT period_start, application_id, token_limit, confirmed_tokens,
                          pending_reserved_tokens, pending_reservation_count,
                          finalized_upper_bound_tokens, finalized_upper_bound_count,
                          stale_reservation_count, oldest_reservation_at,
                          available_tokens, snapshot_at
                   FROM gateway_application_ledger_state
                   WHERE period_start = %s AND application_id = ANY(%s)""",
                (period_start, list(application_ids)),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

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
                """SELECT attribution.application_id,
                          COUNT(*)::BIGINT AS request_count,
                          COUNT(*) FILTER (WHERE usage.status_code >= 400)::BIGINT
                            AS denied_request_count,
                          COALESCE(SUM(usage.input_tokens), 0)::BIGINT AS input_tokens,
                          COALESCE(SUM(usage.cached_tokens), 0)::BIGINT AS cached_tokens,
                          COALESCE(SUM(usage.cache_write_tokens), 0)::BIGINT
                            AS cache_write_tokens,
                          COALESCE(SUM(usage.output_tokens), 0)::BIGINT AS output_tokens,
                          COALESCE(SUM(
                            usage.input_tokens + usage.cached_tokens + usage.output_tokens
                          ), 0)::BIGINT AS total_tokens,
                          COALESCE(SUM(usage.estimated_cost), 0)::DOUBLE PRECISION
                            AS estimated_cost,
                       MAX(usage.ts) AS last_request_at
                       FROM token_usage usage
                       JOIN token_usage_application_attribution attribution
                       ON attribution.usage_id = usage.id
                       WHERE usage.usage_domain = 'apim'
                       AND attribution.application_id = ANY(%s)
                       AND usage.ts >= %s::TIMESTAMP AT TIME ZONE 'UTC'
                       AND usage.ts < %s::TIMESTAMP AT TIME ZONE 'UTC'
                       GROUP BY attribution.application_id ORDER BY attribution.application_id""",
                (
                    list(application_ids),
                    period_start,
                    period_end,
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
                """WITH user_usage AS (
                                         SELECT COALESCE(
                                                            attribution.person_id,
                                                            attribution.actor_id
                                                        ) AS user_id,
                                                        CASE
                                                            WHEN attribution.actor_type = 'person'
                                                                THEN COALESCE(
                                                                    MAX(
                                                                        NULLIF(
                                                                            usage.user_ref,
                                                                            'unattributed'
                                                                        )
                                                                    ),
                                                                    attribution.person_id,
                                                                    attribution.actor_id
                                                                )
                                                            ELSE attribution.actor_id
                                                        END AS display_name,
                                                        attribution.actor_type,
                                                        attribution.person_id,
                                                        COUNT(*)::BIGINT AS request_count,
                                                        COUNT(*) FILTER (
                                                            WHERE usage.status_code >= 400
                                                        )::BIGINT AS denied_request_count,
                                                        COALESCE(SUM(
                                                            usage.input_tokens
                                                            + usage.cached_tokens
                                                            + usage.output_tokens
                                                        ), 0)::BIGINT AS total_tokens,
                                                        COALESCE(
                                                            SUM(usage.estimated_cost), 0
                                                        )::DOUBLE PRECISION AS estimated_cost,
                                                        MAX(usage.ts) AS last_request_at
                                         FROM token_usage_application_attribution attribution
                                         JOIN token_usage usage ON usage.id = attribution.usage_id
                                         WHERE attribution.application_id = %s
                                             AND usage.ts >= %s AND usage.ts < %s
                                             AND usage.usage_domain = 'apim'
                                         GROUP BY attribution.actor_type,
                                                            attribution.actor_id,
                                                            attribution.person_id
                                     )
                                     SELECT user_usage.*, COUNT(*) OVER ()::BIGINT AS user_count
                                     FROM user_usage
                                     ORDER BY request_count DESC, total_tokens DESC,
                                                        lower(display_name), user_id
                                     LIMIT %s""",
                (application_id, period_start, period_end, limit),
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
                          subscription.scope_exists,
                          owner.id AS owner_user_id,
                          owner.email AS owner_email,
                          owner.display_name AS owner_display_name,
                          COALESCE(owner.enabled, FALSE) AS owner_enabled,
                          owner.tenant_id AS owner_tenant_id,
                          tenant.id AS tenant_id,
                          tenant.display_name AS tenant_name,
                          tenant.enabled AS tenant_enabled,
                          organization_unit.id AS organization_unit_id,
                          organization_unit.display_name AS organization_unit_name,
                          COALESCE(organization_unit.enabled, FALSE)
                              AS organization_unit_enabled
                   FROM gateway_application application
                   JOIN gateway_application_subscription subscription
                     ON subscription.application_id = application.id
                   JOIN tenant ON tenant.id = application.tenant_id
                   LEFT JOIN app_user owner
                     ON owner.id = application.owner_user_id
                    AND owner.tenant_id = application.tenant_id
                   LEFT JOIN organization_unit
                     ON organization_unit.id = application.organization_unit_id
                    AND organization_unit.tenant_id = application.tenant_id
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
                """WITH confirmed AS (
                     SELECT attribution.application_id,
                            COALESCE(SUM(
                              usage.input_tokens + usage.cached_tokens + usage.output_tokens
                            ), 0)::BIGINT AS confirmed_tokens
                     FROM token_usage_application_attribution attribution
                     JOIN token_usage usage ON usage.id = attribution.usage_id
                     WHERE usage.ts >= %s AND usage.ts < %s
                       AND usage.usage_domain = 'apim'
                     GROUP BY attribution.application_id
                   )
                   SELECT application.id AS application_id,
                          application.status,
                          budget.token_limit, budget.tokens_per_minute,
                          budget.enforce,
                                                    budget_scope_confirmed_tokens(
                                                        'application', application.id::TEXT, %s, %s
                                                    ) AS confirmed_tokens
                   FROM gateway_application_budget budget
                   JOIN gateway_application application
                     ON application.id = budget.application_id
                   LEFT JOIN confirmed ON confirmed.application_id = application.id
                   WHERE budget.period_start = %s
                   ORDER BY application.id""",
                (period_start, period_end, period_start, period_end, period_start),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def gateway_application_ledger_applications(
        self, period_start: date
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT application.id AS application_id,
                          application.status,
                          budget.token_limit, budget.tokens_per_minute,
                          budget.enforce
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
                """SELECT policy.application_id,
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
                   FROM gateway_application_model_policy policy
                   LEFT JOIN gateway_application_model_access access
                     ON access.application_id = policy.application_id
                   LEFT JOIN managed_model model ON model.id = access.model_id
                   GROUP BY policy.application_id
                   ORDER BY policy.application_id"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def gateway_application_ledger_mappings(
        self, owner_user_id: UUID | None = None
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT application.gateway_profile_id,
                          application.id AS application_id,
                          application.slug AS application_slug,
                          application.display_name AS application_name,
                          application.application_type,
                          application.status AS application_status,
                          subscription.apim_subscription_id,
                          subscription.state AS subscription_state,
                          subscription.scope_exists,
                          owner.id AS owner_user_id,
                          owner.email AS owner_email,
                          owner.display_name AS owner_display_name,
                          COALESCE(owner.enabled, FALSE) AS owner_enabled,
                          owner.tenant_id AS owner_tenant_id,
                          tenant.id AS tenant_id,
                          tenant.display_name AS tenant_name,
                          tenant.enabled AS tenant_enabled,
                          organization_unit.id AS organization_unit_id,
                          organization_unit.display_name AS organization_unit_name,
                          COALESCE(organization_unit.enabled, FALSE)
                              AS organization_unit_enabled
                   FROM gateway_application application
                   JOIN gateway_application_subscription subscription
                     ON subscription.application_id = application.id
                   JOIN tenant ON tenant.id = application.tenant_id
                   LEFT JOIN app_user owner
                     ON owner.id = application.owner_user_id
                    AND owner.tenant_id = application.tenant_id
                   LEFT JOIN organization_unit
                     ON organization_unit.id = application.organization_unit_id
                    AND organization_unit.tenant_id = application.tenant_id
                   WHERE (%s::uuid IS NULL OR application.owner_user_id = %s)
                   ORDER BY application.gateway_profile_id,
                            subscription.apim_subscription_id""",
                (owner_user_id, owner_user_id),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def roll_forward_gateway_application_budgets(self, period_start: date, actor: str) -> int:
        with self._connection() as connection, connection.transaction():
            rows = connection.execute(
                """WITH latest AS (
                     SELECT DISTINCT ON (budget.application_id)
                            budget.application_id, budget.token_limit,
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
                     period_start, application_id, token_limit,
                     tokens_per_minute, enforce, warning_threshold_percent,
                     updated_by
                   )
                   SELECT %s, application_id, token_limit, tokens_per_minute,
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
