"""Accounts and sessions.

Deliberately not part of `QueryRepository`. That protocol is the usage-query surface and it
has a second implementation, `InMemoryRepository`, selected by `DATA_BACKEND=demo` for
running without PostgreSQL. Authentication has no meaningful in-memory form -- an account
store that forgets every password on restart is not a weaker version of this, it is a
different thing wearing the same name -- so putting sign-in behind the same protocol would
force a fake into the tree and let a misconfiguration silently select it.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class AuthStore:
    def __init__(
        self,
        database_url: str,
        pool_min_size: int = 1,
        pool_max_size: int = 2,
    ) -> None:
        # Smaller than the query pool on purpose: sign-in is a handful of statements at the
        # start of a session, not a per-request cost, and the server is a Standard_B1ms
        # already sharing connections with the query pool and the Function app.
        self._pool = ConnectionPool(
            database_url,
            min_size=pool_min_size,
            max_size=pool_max_size,
            kwargs={"row_factory": dict_row},
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

    # --- accounts ---------------------------------------------------------------------

    def list_users(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, email, display_name, role, enabled,
                       password_hash IS NOT NULL AS has_password,
                       created_at, last_login_at
                FROM app_user
                ORDER BY role, email
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def set_user_role(self, email: str, role: str, actor: str) -> dict[str, Any] | None:
        """Promote or demote an account, refusing to remove the last way back in.

        Both guards exist because the only repair for either mistake is shell access to the
        deployment: `backend.accounts` is a command rather than a route precisely so that
        minting the first account needs something stronger than an HTTP call, and that same
        property makes it a poor recovery path for an administrator who has locked themselves
        out of a running install at four in the afternoon.

        Demoting yourself is refused even when another Owner exists. The account that can undo
        it is someone else's, so the mistake is only recoverable by asking a colleague, and the
        action reads as routine right up until it isn't.
        """
        normalized = email.strip().lower()
        with self._connection() as connection, connection.transaction():
            existing = connection.execute(
                "SELECT id, email, role FROM app_user WHERE email = %s FOR UPDATE",
                (normalized,),
            ).fetchone()
            if existing is None:
                return None
            if existing["role"] == role:
                return dict(existing)
            if role != "owner" and normalized == actor.strip().lower():
                raise ValueError("You cannot remove your own Owner role")
            if role != "owner" and existing["role"] == "owner":
                remaining = connection.execute(
                    """SELECT count(*) AS total FROM app_user
                       WHERE role = 'owner' AND enabled AND email <> %s""",
                    (normalized,),
                ).fetchone()
                if not remaining or int(remaining["total"]) == 0:
                    raise ValueError("This is the last Owner; promote someone else first")
            row = connection.execute(
                """UPDATE app_user SET role = %s WHERE email = %s
                   RETURNING id, email, display_name, role, enabled""",
                (role, normalized),
            ).fetchone()
            # A demoted Owner keeps an Owner-shaped session until it expires otherwise, and
            # `session_owner` reads the role at sign-in rather than per request.
            connection.execute(
                "DELETE FROM user_session WHERE user_id = %s", (existing["id"],)
            )
        return dict(row) if row else None

    def set_user_enabled(self, email: str, enabled: bool, actor: str) -> dict[str, Any] | None:
        normalized = email.strip().lower()
        with self._connection() as connection, connection.transaction():
            existing = connection.execute(
                "SELECT id, email, role, enabled FROM app_user WHERE email = %s FOR UPDATE",
                (normalized,),
            ).fetchone()
            if existing is None:
                return None
            if bool(existing["enabled"]) == enabled:
                return dict(existing)
            if not enabled and normalized == actor.strip().lower():
                raise ValueError("You cannot disable your own account")
            if not enabled and existing["role"] == "owner":
                remaining = connection.execute(
                    """SELECT count(*) AS total FROM app_user
                       WHERE role = 'owner' AND enabled AND email <> %s""",
                    (normalized,),
                ).fetchone()
                if not remaining or int(remaining["total"]) == 0:
                    raise ValueError("This is the last Owner; promote someone else first")
            row = connection.execute(
                """UPDATE app_user SET enabled = %s WHERE email = %s
                   RETURNING id, email, display_name, role, enabled""",
                (enabled, normalized),
            ).fetchone()
            if not enabled:
                connection.execute(
                    "DELETE FROM user_session WHERE user_id = %s", (existing["id"],)
                )
        return dict(row) if row else None

    def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, email, display_name, password_hash, role, enabled
                FROM app_user
                WHERE email = %s
                """,
                (email.strip().lower(),),
            ).fetchone()
        return dict(row) if row else None

    def upsert_entra_user(self, email: str, display_name: str | None) -> dict[str, Any]:
        """Provision on first Microsoft sign-in.

        The token has already been verified and the domain already checked by the time this
        runs, so the account is created rather than refused -- requiring an administrator to
        pre-create a row for every colleague would make the Microsoft path useless without
        adding a decision, since the same people would be approved anyway.

        `enabled` is untouched on conflict. Disabling someone must not be undone by them
        signing in again, which is exactly what a naive upsert of every column would do.

        A token without a `name` claim stores NULL rather than a slug cut out of the
        address. The caller already has the email and can show it; a fabricated name is
        indistinguishable from a real one once it is on screen.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO app_user (email, display_name)
                VALUES (%s, %s)
                ON CONFLICT (email) DO UPDATE
                    SET display_name = COALESCE(EXCLUDED.display_name, app_user.display_name)
                RETURNING id, email, display_name, password_hash, role, enabled
                """,
                (email.strip().lower(), (display_name or "").strip() or None),
            ).fetchone()
        return dict(row) if row else {}

    def create_password_user(
        self, email: str, display_name: str | None, password_hash: str, role: str = "member"
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO app_user (email, display_name, password_hash, role)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE
                    SET display_name = COALESCE(EXCLUDED.display_name, app_user.display_name),
                        password_hash = EXCLUDED.password_hash,
                        role = EXCLUDED.role
                RETURNING id, email, display_name, password_hash, role, enabled
                """,
                (email.strip().lower(), (display_name or "").strip() or None, password_hash, role),
            ).fetchone()
            if row is not None:
                connection.execute("DELETE FROM user_session WHERE user_id = %s", (row["id"],))
        return dict(row) if row else {}

    def create_initial_owner(self, email: str, password_hash: str) -> dict[str, Any] | None:
        """Create the first account only while the user table is empty."""
        with self._connection() as connection, connection.transaction():
            connection.execute("LOCK TABLE app_user IN SHARE ROW EXCLUSIVE MODE")
            if connection.execute("SELECT EXISTS (SELECT 1 FROM app_user)").fetchone()[
                "exists"
            ]:
                return None
            row = connection.execute(
                """INSERT INTO app_user (email, password_hash, role)
                   VALUES (%s, %s, 'owner')
                   RETURNING id, email, display_name, password_hash, role, enabled""",
                (email.strip().lower(), password_hash),
            ).fetchone()
        return dict(row) if row else None

    def touch_last_login(self, user_id: UUID) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE app_user SET last_login_at = now() WHERE id = %s", (user_id,)
            )

    # --- sessions ---------------------------------------------------------------------

    def create_session(
        self,
        user_id: UUID,
        token_sha256: str,
        method: str,
        authenticated_at: datetime,
        expires_at: datetime,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO user_session (
                    token_sha256, user_id, method, created_at, expires_at
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    token_sha256,
                    user_id,
                    method,
                    authenticated_at,
                    expires_at,
                ),
            )

    def session_owner(self, token_sha256: str) -> dict[str, Any] | None:
        """Who a cookie belongs to, or None.

        Expiry and `enabled` are both conditions of this one statement rather than checks
        the caller makes afterwards. That is what makes "disable this account" take effect
        on the next request instead of whenever the cookie happens to lapse -- and it means
        no caller can forget the check, because there is no result to forget it on.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    u.id,
                    u.email,
                    u.display_name,
                    u.role,
                    s.method,
                    s.created_at,
                    s.expires_at
                FROM user_session AS s
                JOIN app_user AS u ON u.id = s.user_id
                WHERE s.token_sha256 = %s
                  AND s.expires_at > now()
                  AND u.enabled
                """,
                (token_sha256,),
            ).fetchone()
        return dict(row) if row else None

    def delete_session(self, token_sha256: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM user_session WHERE token_sha256 = %s", (token_sha256,)
            )

    def delete_expired_sessions(self) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM user_session WHERE expires_at <= now()"
            )
            return cursor.rowcount or 0
