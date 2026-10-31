"""Persistence for the GitHub Copilot billing data domain.

This is deliberately separate from ``QueryRepository``. Copilot organization billing is
not an alternate implementation of APIM observability: it has different credentials,
identities, units, authorization rules, and lifecycle data.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from turnstile_core.persistence.pool_health import CONNECTION_HEALTH


class CopilotStoreConflictError(ValueError):
    pass


class CopilotStore:
    def __init__(
        self,
        database_url: str,
        pool_min_size: int = 1,
        pool_max_size: int = 2,
    ) -> None:
        self._pool = ConnectionPool(
            database_url,
            min_size=pool_min_size,
            max_size=pool_max_size,
            kwargs={"row_factory": dict_row, **CONNECTION_HEALTH},
            open=False,
        )
        self._pool_lock = threading.Lock()
        self._pool_opened = False

    @contextmanager
    def _connection(self) -> Any:
        if not self._pool_opened:
            with self._pool_lock:
                if not self._pool_opened:
                    self._pool.open()
                    self._pool_opened = True
        with self._pool.connection() as connection:
            yield connection

    def close(self) -> None:
        with self._pool_lock:
            if self._pool_opened:
                self._pool.close()
                self._pool_opened = False

    def list_connections(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id, organization, display_name, credential_hint,
                          write_enabled, is_default, created_at, updated_at, updated_by
                   FROM copilot_connection
                   ORDER BY is_default DESC, display_name, organization"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def connection(self, organization: str | None = None) -> dict[str, Any] | None:
        with self._connection() as connection:
            if organization:
                row = connection.execute(
                    """SELECT * FROM copilot_connection WHERE organization = %s""",
                    (organization.strip().lower(),),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT * FROM copilot_connection
                       ORDER BY is_default DESC, created_at
                       LIMIT 1"""
                ).fetchone()
        return dict(row) if row else None

    def save_connection(
        self,
        *,
        organization: str,
        display_name: str,
        credential_ciphertext: bytes,
        credential_hint: str,
        write_enabled: bool,
        set_default: bool,
        updated_by: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            if set_default:
                connection.execute("UPDATE copilot_connection SET is_default = false")
            row = connection.execute(
                """INSERT INTO copilot_connection (
                       organization, display_name, credential_ciphertext, credential_hint,
                       write_enabled, is_default, updated_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (organization) DO UPDATE
                       SET display_name = EXCLUDED.display_name,
                           credential_ciphertext = EXCLUDED.credential_ciphertext,
                           credential_hint = EXCLUDED.credential_hint,
                           write_enabled = EXCLUDED.write_enabled,
                           is_default = EXCLUDED.is_default,
                           updated_at = now(),
                           updated_by = EXCLUDED.updated_by
                   RETURNING *""",
                (
                    organization.strip().lower(),
                    display_name.strip(),
                    credential_ciphertext,
                    credential_hint,
                    write_enabled,
                    set_default,
                    updated_by,
                ),
            ).fetchone()
        assert row is not None
        return dict(row)

    def identity(self, app_user_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT identity.app_user_id, user_account.email,
                          user_account.display_name, identity.github_login
                   FROM copilot_identity AS identity
                   JOIN app_user AS user_account ON user_account.id = identity.app_user_id
                   WHERE identity.app_user_id = %s""",
                (app_user_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_identities(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT user_account.id AS app_user_id, user_account.email,
                          user_account.display_name, identity.github_login
                   FROM app_user AS user_account
                   LEFT JOIN copilot_identity AS identity
                     ON identity.app_user_id = user_account.id
                   WHERE user_account.enabled
                   ORDER BY lower(COALESCE(user_account.display_name, user_account.email))"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def save_identity(
        self, app_user_id: UUID, github_login: str, updated_by: str
    ) -> dict[str, Any]:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """INSERT INTO copilot_identity (app_user_id, github_login, updated_by)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (app_user_id) DO UPDATE
                           SET github_login = EXCLUDED.github_login,
                               updated_at = now(),
                               updated_by = EXCLUDED.updated_by
                       RETURNING app_user_id, github_login""",
                    (app_user_id, github_login.strip().lower(), updated_by),
                ).fetchone()
        except UniqueViolation as error:
            raise CopilotStoreConflictError(
                "This GitHub login is already linked to another application user"
            ) from error
        if row is None:
            raise KeyError(str(app_user_id))
        mapped = self.identity(app_user_id)
        if mapped is None:
            raise KeyError(str(app_user_id))
        return mapped

    def oauth_setting(self, origin: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM copilot_oauth_setting WHERE origin = %s",
                (origin,),
            ).fetchone()
        return dict(row) if row else None

    def save_oauth_setting(
        self,
        *,
        origin: str,
        client_id: str,
        client_secret_ciphertext: bytes,
        client_secret_hint: str,
        callback_url: str | None,
        updated_by: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """INSERT INTO copilot_oauth_setting (
                       origin, client_id, client_secret_ciphertext,
                       client_secret_hint, callback_url, updated_by
                   ) VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (origin) DO UPDATE
                       SET client_id = EXCLUDED.client_id,
                           client_secret_ciphertext = EXCLUDED.client_secret_ciphertext,
                           client_secret_hint = EXCLUDED.client_secret_hint,
                           callback_url = EXCLUDED.callback_url,
                           updated_at = now(),
                           updated_by = EXCLUDED.updated_by
                   RETURNING *""",
                (
                    origin,
                    client_id,
                    client_secret_ciphertext,
                    client_secret_hint,
                    callback_url,
                    updated_by,
                ),
            ).fetchone()
        assert row is not None
        return dict(row)

    def create_oauth_state(
        self,
        state_sha256: str,
        app_user_id: UUID,
        origin: str,
        return_origin: str,
        return_path: str,
        purpose: str,
        organization: str | None,
        expires_at: datetime,
    ) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM copilot_oauth_state WHERE expires_at <= now()")
            connection.execute(
                """INSERT INTO copilot_oauth_state (
                       state_sha256, app_user_id, origin, return_origin,
                       return_path, purpose, organization, expires_at
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    state_sha256,
                    app_user_id,
                    origin,
                    return_origin,
                    return_path,
                    purpose,
                    organization,
                    expires_at,
                ),
            )

    def consume_oauth_state(
        self, state_sha256: str, origin: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """WITH consumed AS (
                       DELETE FROM copilot_oauth_state
                        WHERE state_sha256 = %s
                          AND origin = %s
                          AND expires_at > now()
                    RETURNING app_user_id, return_origin, return_path,
                              purpose, organization
                   )
                   SELECT consumed.app_user_id, consumed.return_origin,
                          consumed.return_path, consumed.purpose,
                          consumed.organization, user_account.email,
                          user_account.role
                     FROM consumed
                     JOIN app_user AS user_account
                       ON user_account.id = consumed.app_user_id
                    WHERE user_account.enabled""",
                (state_sha256, origin),
            ).fetchone()
        return dict(row) if row else None

    def create_budget_request(
        self,
        *,
        connection_id: UUID,
        organization: str,
        app_user_id: UUID,
        user_email: str,
        user_display_name: str | None,
        github_login: str,
        requested_amount_usd: int,
        reason: str,
    ) -> dict[str, Any]:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """INSERT INTO copilot_budget_request (
                           connection_id, organization, app_user_id, user_email,
                           user_display_name, github_login, requested_amount_usd, reason
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING *""",
                    (
                        connection_id,
                        organization,
                        app_user_id,
                        user_email,
                        user_display_name,
                        github_login,
                        requested_amount_usd,
                        reason,
                    ),
                ).fetchone()
                assert row is not None
                connection.execute(
                    """INSERT INTO copilot_budget_request_audit (
                           request_id, action, actor, amount_usd, github_sync_status
                       ) VALUES (%s, 'created', %s, %s, 'not_requested')""",
                    (row["id"], user_email, requested_amount_usd),
                )
        except UniqueViolation as error:
            raise CopilotStoreConflictError(
                "A pending budget request already exists for this organization"
            ) from error
        return dict(row)

    def list_budget_requests(
        self, app_user_id: UUID | None = None
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            if app_user_id is None:
                rows = connection.execute(
                    """SELECT * FROM copilot_budget_request ORDER BY created_at DESC"""
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM copilot_budget_request
                       WHERE app_user_id = %s ORDER BY created_at DESC""",
                    (app_user_id,),
                ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def budget_request(self, request_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM copilot_budget_request WHERE id = %s",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def claim_budget_review(self, request_id: UUID, actor: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE copilot_budget_request
                   SET processing_by = %s, processing_at = now(), updated_at = now()
                   WHERE id = %s
                     AND status = 'pending'
                     AND (
                         processing_at IS NULL
                         OR processing_at < now() - %s
                     )
                   RETURNING *""",
                (actor, request_id, timedelta(minutes=5)),
            ).fetchone()
        return dict(row) if row else None

    def complete_budget_review(
        self,
        *,
        request_id: UUID,
        actor: str,
        status: str,
        approved_amount_usd: int | None,
        review_comment: str,
        github_sync_status: str,
        github_sync_error: str | None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE copilot_budget_request
                   SET status = %s,
                       approved_amount_usd = %s,
                       reviewed_by = %s,
                       review_comment = %s,
                       reviewed_at = now(),
                       github_sync_status = %s,
                       github_sync_error = %s,
                       processing_by = NULL,
                       processing_at = NULL,
                       updated_at = now()
                   WHERE id = %s AND status = 'pending' AND processing_by = %s
                   RETURNING *""",
                (
                    status,
                    approved_amount_usd,
                    actor,
                    review_comment,
                    github_sync_status,
                    github_sync_error,
                    request_id,
                    actor,
                ),
            ).fetchone()
            if row is None:
                raise CopilotStoreConflictError("Budget request review is no longer available")
            connection.execute(
                """INSERT INTO copilot_budget_request_audit (
                       request_id, action, actor, amount_usd, comment, github_sync_status
                   ) VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    request_id,
                    status,
                    actor,
                    approved_amount_usd,
                    review_comment,
                    github_sync_status,
                ),
            )
        return dict(row)

    def save_usage_import(
        self,
        *,
        connection_id: UUID,
        source_kind: str,
        filename: str,
        content_sha256: str,
        file_size_bytes: int,
        rows: Sequence[dict[str, Any]],
        first_usage_date: object,
        last_usage_date: object,
        uploaded_by: UUID,
        uploaded_by_email: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            existing = connection.execute(
                """SELECT * FROM copilot_usage_import
                   WHERE connection_id = %s AND source_kind = %s
                     AND content_sha256 = %s""",
                (connection_id, source_kind, content_sha256),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            hashes = [str(row["row_sha256"]) for row in rows]
            existing_hashes = {
                str(row["row_sha256"])
                for row in connection.execute(
                    """SELECT row_sha256 FROM copilot_usage_import_row
                       WHERE connection_id = %s AND source_kind = %s
                         AND row_sha256 = ANY(%s)""",
                    (connection_id, source_kind, hashes),
                ).fetchall()
            }
            seen_hashes = set(existing_hashes)
            new_rows: list[dict[str, Any]] = []
            for row in rows:
                row_hash = str(row["row_sha256"])
                if row_hash in seen_hashes:
                    continue
                seen_hashes.add(row_hash)
                new_rows.append(row)
            upload = connection.execute(
                """INSERT INTO copilot_usage_import (
                       connection_id, source_kind, filename, content_sha256,
                       file_size_bytes, row_count, inserted_count, duplicate_count,
                       first_usage_date, last_usage_date, uploaded_by, uploaded_by_email
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (
                    connection_id,
                    source_kind,
                    filename,
                    content_sha256,
                    file_size_bytes,
                    len(rows),
                    len(new_rows),
                    len(rows) - len(new_rows),
                    first_usage_date,
                    last_usage_date,
                    uploaded_by,
                    uploaded_by_email,
                ),
            ).fetchone()
            assert upload is not None
            if new_rows:
                connection.executemany(
                    """INSERT INTO copilot_usage_import_row (
                           upload_id, connection_id, source_kind, organization,
                           usage_date, username, model, product, sku, unit_type,
                           cost_center_name, quantity, gross_amount, discount_amount,
                           net_amount, total_monthly_quota, row_sha256, raw_row
                       ) VALUES (
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s
                       )""",
                    [
                        (
                            upload["id"],
                            connection_id,
                            source_kind,
                            row["organization"],
                            row["usage_date"],
                            row["username"],
                            row.get("model"),
                            row.get("product"),
                            row.get("sku"),
                            row.get("unit_type"),
                            row.get("cost_center_name"),
                            row["quantity"],
                            row["gross_amount"],
                            row["discount_amount"],
                            row["net_amount"],
                            row.get("total_monthly_quota"),
                            row["row_sha256"],
                            Jsonb(row["raw_row"]),
                        )
                        for row in new_rows
                    ],
                )
        return dict(upload)

    def usage_imports(
        self, connection_id: UUID, source_kind: str
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM copilot_usage_import
                   WHERE connection_id = %s AND source_kind = %s
                   ORDER BY created_at DESC""",
                (connection_id, source_kind),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def imported_usage(
        self, connection_id: UUID, source_kind: str
    ) -> dict[str, Any]:
        with self._connection() as connection:
            totals = connection.execute(
                """SELECT min(usage_date) AS first_usage_date,
                          max(usage_date) AS last_usage_date,
                          COALESCE(sum(quantity), 0) AS total_quantity,
                          COALESCE(sum(gross_amount), 0) AS total_gross_amount,
                          COALESCE(sum(net_amount), 0) AS total_net_amount,
                          count(DISTINCT username) AS unique_users,
                          count(DISTINCT organization) AS unique_organizations
                   FROM copilot_usage_import_row
                   WHERE connection_id = %s AND source_kind = %s""",
                (connection_id, source_kind),
            ).fetchone()
            daily = connection.execute(
                """SELECT usage_date AS day, sum(quantity) AS quantity,
                          sum(gross_amount) AS gross_amount,
                          sum(net_amount) AS net_amount,
                          count(DISTINCT username) AS active_users
                   FROM copilot_usage_import_row
                   WHERE connection_id = %s AND source_kind = %s
                   GROUP BY usage_date ORDER BY usage_date""",
                (connection_id, source_kind),
            ).fetchall()
            primary = connection.execute(
                """SELECT CASE WHEN source_kind = 'ai_usage' THEN model ELSE sku END AS key,
                          sum(quantity) AS quantity, sum(gross_amount) AS gross_amount,
                          sum(net_amount) AS net_amount,
                          count(DISTINCT username) AS user_count
                   FROM copilot_usage_import_row
                   WHERE connection_id = %s AND source_kind = %s
                   GROUP BY 1 ORDER BY gross_amount DESC, key LIMIT 100""",
                (connection_id, source_kind),
            ).fetchall()
            products = connection.execute(
                """SELECT product AS key, sum(quantity) AS quantity,
                          sum(gross_amount) AS gross_amount,
                          sum(net_amount) AS net_amount,
                          count(DISTINCT username) AS user_count
                   FROM copilot_usage_import_row
                   WHERE connection_id = %s AND source_kind = %s
                     AND product IS NOT NULL
                   GROUP BY product ORDER BY gross_amount DESC, product""",
                (connection_id, source_kind),
            ).fetchall()
            organizations = connection.execute(
                """SELECT organization AS key, sum(quantity) AS quantity,
                          sum(gross_amount) AS gross_amount,
                          sum(net_amount) AS net_amount,
                          count(DISTINCT username) AS user_count
                   FROM copilot_usage_import_row
                   WHERE connection_id = %s AND source_kind = %s
                   GROUP BY organization ORDER BY gross_amount DESC, organization""",
                (connection_id, source_kind),
            ).fetchall()
            cost_centers = connection.execute(
                """SELECT COALESCE(cost_center_name, 'Unassigned') AS key,
                          sum(quantity) AS quantity, sum(gross_amount) AS gross_amount,
                          sum(net_amount) AS net_amount,
                          count(DISTINCT username) AS user_count
                   FROM copilot_usage_import_row
                   WHERE connection_id = %s AND source_kind = %s
                   GROUP BY 1 ORDER BY gross_amount DESC, key""",
                (connection_id, source_kind),
            ).fetchall()
            users = connection.execute(
                """SELECT username AS login, min(organization) AS organization,
                          min(cost_center_name) AS cost_center_name,
                          sum(quantity) AS quantity, sum(gross_amount) AS gross_amount,
                          sum(net_amount) AS net_amount,
                          count(DISTINCT usage_date) AS active_days,
                          max(total_monthly_quota) AS monthly_quota
                   FROM copilot_usage_import_row
                   WHERE connection_id = %s AND source_kind = %s
                   GROUP BY username ORDER BY gross_amount DESC, username LIMIT 500""",
                (connection_id, source_kind),
            ).fetchall()
            filters = connection.execute(
                """SELECT array_agg(DISTINCT organization ORDER BY organization)
                              FILTER (WHERE organization <> '') AS organizations,
                          array_agg(DISTINCT cost_center_name ORDER BY cost_center_name)
                              FILTER (WHERE cost_center_name IS NOT NULL) AS cost_centers,
                          array_agg(DISTINCT product ORDER BY product)
                              FILTER (WHERE product IS NOT NULL) AS products,
                          array_agg(DISTINCT sku ORDER BY sku)
                              FILTER (WHERE sku IS NOT NULL) AS skus
                   FROM copilot_usage_import_row
                   WHERE connection_id = %s AND source_kind = %s""",
                (connection_id, source_kind),
            ).fetchone()
        return {
            "totals": dict(totals) if totals else {},
            "daily": [dict(row) for row in daily],
            "primary_breakdown": [dict(row) for row in primary],
            "product_breakdown": [dict(row) for row in products],
            "organization_breakdown": [dict(row) for row in organizations],
            "cost_center_breakdown": [dict(row) for row in cost_centers],
            "users": [dict(row) for row in users],
            "filters": dict(filters) if filters else {},
        }

    def create_cost_center_request(self, **values: Any) -> dict[str, Any]:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """INSERT INTO copilot_cost_center_request (
                           connection_id, organization, app_user_id, user_email,
                           user_display_name, github_login, cost_center_id,
                           cost_center_name, reason
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING *""",
                    (
                        values["connection_id"],
                        values["organization"],
                        values["app_user_id"],
                        values["user_email"],
                        values["user_display_name"],
                        values["github_login"],
                        values["cost_center_id"],
                        values["cost_center_name"],
                        values["reason"],
                    ),
                ).fetchone()
                assert row is not None
                connection.execute(
                    """INSERT INTO copilot_cost_center_request_audit (
                           request_id, action, actor, github_sync_status
                       ) VALUES (%s, 'created', %s, 'not_requested')""",
                    (row["id"], values["user_email"]),
                )
        except UniqueViolation as error:
            raise CopilotStoreConflictError(
                "A pending cost center request already exists for this organization"
            ) from error
        return dict(row)

    def list_cost_center_requests(
        self, app_user_id: UUID | None = None
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            if app_user_id is None:
                rows = connection.execute(
                    """SELECT * FROM copilot_cost_center_request
                       ORDER BY created_at DESC"""
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM copilot_cost_center_request
                       WHERE app_user_id = %s ORDER BY created_at DESC""",
                    (app_user_id,),
                ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def cost_center_request(self, request_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM copilot_cost_center_request WHERE id = %s",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def claim_cost_center_review(
        self, request_id: UUID, actor: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE copilot_cost_center_request
                   SET processing_by = %s, processing_at = now(), updated_at = now()
                   WHERE id = %s AND status = 'pending'
                     AND (processing_at IS NULL OR processing_at < now() - %s)
                   RETURNING *""",
                (actor, request_id, timedelta(minutes=5)),
            ).fetchone()
        return dict(row) if row else None

    def complete_cost_center_review(
        self,
        *,
        request_id: UUID,
        actor: str,
        status: str,
        review_comment: str,
        github_sync_status: str,
        github_sync_error: str | None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE copilot_cost_center_request
                   SET status = %s, reviewed_by = %s, review_comment = %s,
                       reviewed_at = now(), github_sync_status = %s,
                       github_sync_error = %s, processing_by = NULL,
                       processing_at = NULL, updated_at = now()
                   WHERE id = %s AND status = 'pending' AND processing_by = %s
                   RETURNING *""",
                (
                    status,
                    actor,
                    review_comment,
                    github_sync_status,
                    github_sync_error,
                    request_id,
                    actor,
                ),
            ).fetchone()
            if row is None:
                raise CopilotStoreConflictError(
                    "Cost center request review is no longer available"
                )
            connection.execute(
                """INSERT INTO copilot_cost_center_request_audit (
                       request_id, action, actor, comment, github_sync_status
                   ) VALUES (%s, %s, %s, %s, %s)""",
                (request_id, status, actor, review_comment, github_sync_status),
            )
        return dict(row)