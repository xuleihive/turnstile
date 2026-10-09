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
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class AccountSessionInvalid(ValueError):
    pass


class PasswordMismatch(ValueError):
    pass


class PasswordRateLimited(ValueError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Too many password verification failures")
        self.retry_after = retry_after


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
            if connection.execute("SELECT EXISTS (SELECT 1 FROM app_user)").fetchone()["exists"]:
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
        expected_password_hash: str | None = None,
    ) -> bool:
        with self._connection() as connection, connection.transaction():
            user = connection.execute(
                "SELECT password_hash, enabled FROM app_user WHERE id = %s FOR UPDATE",
                (user_id,),
            ).fetchone()
            if (
                not user
                or not user["enabled"]
                or (
                    method == "password"
                    and (
                        not expected_password_hash
                        or user["password_hash"] != expected_password_hash
                    )
                )
            ):
                return False
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
        return True

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
                    s.expires_at,
                    CASE WHEN s.method = 'password' THEN avatar.revision END AS avatar_revision
                FROM user_session AS s
                JOIN app_user AS u ON u.id = s.user_id
                LEFT JOIN app_user_avatar avatar ON avatar.app_user_id = u.id
                WHERE s.token_sha256 = %s
                  AND s.expires_at > now()
                  AND u.enabled
                """,
                (token_sha256,),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _locked_account(connection: Any, user_id: UUID, token_sha256: str) -> dict[str, Any]:
        # Account-row locking shares the same ordering as login and password replacement.
        user = connection.execute(
            "SELECT id, password_hash FROM app_user WHERE id = %s AND enabled FOR UPDATE",
            (user_id,),
        ).fetchone()
        session = connection.execute(
            """SELECT 1 FROM user_session
               WHERE user_id = %s AND token_sha256 = %s
                 AND method = 'password' AND expires_at > clock_timestamp()""",
            (user_id, token_sha256),
        ).fetchone()
        if not user or not session:
            raise AccountSessionInvalid("Session is no longer valid")
        return dict(user)

    @staticmethod
    def _record_event(connection: Any, user_id: UUID, action: str) -> None:
        connection.execute(
            "INSERT INTO account_security_event(app_user_id, action) VALUES (%s, %s)",
            (user_id, action),
        )

    def update_display_name(
        self, user_id: UUID, token_sha256: str, display_name: str | None
    ) -> None:
        with self._connection() as connection, connection.transaction():
            self._locked_account(connection, user_id, token_sha256)
            changed = connection.execute(
                """UPDATE app_user SET display_name = %s
                   WHERE id = %s AND display_name IS DISTINCT FROM %s RETURNING id""",
                (display_name, user_id, display_name),
            ).fetchone()
            if changed:
                self._record_event(connection, user_id, "display_name_updated")

    def avatar(self, user_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM app_user_avatar WHERE app_user_id = %s", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_avatar(
        self, user_id: UUID, token_sha256: str, media_type: str | None, image_bytes: bytes | None
    ) -> dict[str, Any] | None:
        if (media_type is None) != (image_bytes is None):
            raise ValueError("Avatar media type and image must be set together")
        with self._connection() as connection, connection.transaction():
            self._locked_account(connection, user_id, token_sha256)
            if image_bytes is None:
                removed = connection.execute(
                    "DELETE FROM app_user_avatar WHERE app_user_id = %s RETURNING app_user_id",
                    (user_id,),
                ).fetchone()
                if removed:
                    self._record_event(connection, user_id, "avatar_removed")
                return None
            row = connection.execute(
                """INSERT INTO app_user_avatar(app_user_id, media_type, image_bytes, revision)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT(app_user_id) DO UPDATE SET media_type = EXCLUDED.media_type,
                     image_bytes = EXCLUDED.image_bytes, revision = EXCLUDED.revision,
                     updated_at = clock_timestamp()
                   RETURNING revision, updated_at""",
                (user_id, media_type, image_bytes, uuid4()),
            ).fetchone()
            self._record_event(connection, user_id, "avatar_updated")
        return dict(row) if row else None

    def change_password(
        self,
        user_id: UUID,
        token_sha256: str,
        current_password: str,
        new_password: str,
        verifier: Callable[[str, str | None], bool],
        hasher: Callable[[str], str],
    ) -> None:
        mismatch = False
        with self._connection() as connection, connection.transaction():
            user = self._locked_account(connection, user_id, token_sha256)
            attempts = connection.execute(
                """SELECT count(*) AS failures,
                     ceil(extract(epoch FROM (
                       min(created_at) + interval '15 minutes' - clock_timestamp()
                     )))::int AS retry_after
                   FROM account_security_event
                   WHERE app_user_id = %s AND action = 'password_verification_failed'
                     AND created_at > clock_timestamp() - interval '15 minutes'""",
                (user_id,),
            ).fetchone()
            if attempts["failures"] >= 5:
                raise PasswordRateLimited(max(1, attempts["retry_after"]))
            if not verifier(current_password, user["password_hash"]):
                self._record_event(connection, user_id, "password_verification_failed")
                mismatch = True
            else:
                connection.execute(
                    "UPDATE app_user SET password_hash = %s WHERE id = %s",
                    (hasher(new_password), user_id),
                )
                connection.execute("DELETE FROM user_session WHERE user_id = %s", (user_id,))
                self._record_event(connection, user_id, "password_changed")
        # Incorrect-password attempts must commit before returning the HTTP error.
        if mismatch:
            raise PasswordMismatch("Current password is incorrect")

    def delete_session(self, token_sha256: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM user_session WHERE token_sha256 = %s", (token_sha256,))

    def delete_expired_sessions(self) -> int:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM user_session WHERE expires_at <= now()")
            return cursor.rowcount or 0
