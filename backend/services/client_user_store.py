"""Transactional, hashed desktop grants. No inference or browser cookies are stored here."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from turnstile_core.persistence.auth_store import AuthStore


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ClientUserStore(AuthStore):
    def issue_download(
        self,
        user_id: str,
        installation: UUID,
        method: str,
        selection: dict[str, Any],
        access: str,
        refresh: str,
        grant_id: UUID,
    ) -> None:
        with self._connection() as c, c.transaction():
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (user_id,))
            user = c.execute(
                "SELECT enabled FROM app_user WHERE id=%s FOR SHARE", (user_id,)
            ).fetchone()
            if not user or not user["enabled"]:
                raise HTTPException(401, "Account is no longer enabled")
            count = c.execute(
                "SELECT count(*) AS n FROM client_user_grant WHERE user_id=%s "
                "AND created_at>now()-interval '1 minute'",
                (user_id,),
            ).fetchone()["n"]
            if count >= 6:
                raise HTTPException(
                    429, "Please wait before downloading another authorized package"
                )
            c.execute(
                "INSERT INTO client_user_installation(user_id,installation_id) VALUES(%s,%s)",
                (user_id, installation),
            )
            c.execute(
                "INSERT INTO client_user_grant (id,user_id,installation_id,method,access_digest,"
                "refresh_digest,access_expires_at,refresh_expires_at,expires_at,"
                "selection,activated) "
                "VALUES(%s,%s,%s,%s,%s,%s,now()+interval '15 minutes',"
                "now()+interval '7 days',now()+interval '30 days',%s,TRUE)",
                (
                    grant_id,
                    user_id,
                    installation,
                    method,
                    digest(access),
                    digest(refresh),
                    Jsonb(selection),
                ),
            )
            c.execute(
                "UPDATE client_user_installation SET generation=1,active_grant=%s "
                "WHERE user_id=%s AND installation_id=%s",
                (grant_id, user_id, installation),
            )

    def create_device(
        self,
        user_id: str,
        installation: UUID,
        purpose: str,
        device: str,
        code: str,
    ) -> None:
        with self._connection() as c, c.transaction():
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (user_id,))
            count = c.execute(
                "SELECT count(*) AS n FROM client_device_authorization WHERE user_id=%s "
                "AND created_at>now()-interval '1 minute'",
                (user_id,),
            ).fetchone()["n"]
            if count >= 6:
                raise HTTPException(429, "Please wait before starting another sign-in")
            c.execute(
                "DELETE FROM client_device_authorization WHERE expires_at<now()-interval '1 day'"
            )
            c.execute(
                "INSERT INTO client_device_authorization "
                "(id,user_id,installation_id,purpose,device_digest,code_digest,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,now()+interval '5 minutes')",
                (uuid4(), user_id, installation, purpose, digest(device), digest(code)),
            )

    def device(self, code: str, user_id: str) -> dict[str, Any]:
        with self._connection() as c:
            row = c.execute(
                "SELECT * FROM client_device_authorization WHERE code_digest=%s "
                "AND user_id=%s AND expires_at>now()",
                (digest(code), user_id),
            ).fetchone()
        if not row:
            raise HTTPException(404, "Authorization expired or belongs to another user")
        return dict(row)

    def decide(
        self,
        code: str,
        user_id: str,
        approve: bool,
        selection: dict[str, Any] | None,
        method: str,
        authenticated_at: datetime,
    ) -> None:
        with self._connection() as c, c.transaction():
            row = c.execute(
                "SELECT * FROM client_device_authorization WHERE code_digest=%s "
                "AND user_id=%s AND expires_at>now() FOR UPDATE",
                (digest(code), user_id),
            ).fetchone()
            if not row or row["state"] != "pending":
                raise HTTPException(409, "This authorization is no longer pending")
            if approve and row["purpose"] == "reauth" and authenticated_at < row["created_at"]:
                raise HTTPException(409, "Sign in to the platform again before confirming")
            c.execute(
                "UPDATE client_device_authorization SET state=%s,selection=%s,"
                "authenticated_method=%s WHERE id=%s",
                ("approved" if approve else "denied", Jsonb(selection), method, row["id"]),
            )

    def poll(self, device: str) -> dict[str, Any]:
        with self._connection() as c, c.transaction():
            row = c.execute(
                "SELECT * FROM client_device_authorization WHERE device_digest=%s FOR UPDATE",
                (digest(device),),
            ).fetchone()
            now = datetime.now(UTC)
            if not row or row["expires_at"] <= now:
                raise HTTPException(400, "expired_token")
            if row["state"] == "denied":
                raise HTTPException(400, "access_denied")
            if row["state"] == "consumed":
                if row["result_until"] and row["result_until"] > now:
                    return dict(row)
                raise HTTPException(400, "invalid_grant")
            if row["last_poll_at"] and (now - row["last_poll_at"]).total_seconds() < 4:
                raise HTTPException(400, "slow_down")
            c.execute(
                "UPDATE client_device_authorization SET last_poll_at=now() WHERE id=%s",
                (row["id"],),
            )
            return dict(row)

    def issue(
        self,
        device_id: UUID,
        access: str,
        refresh: str,
        result_cipher: bytes,
        grant_id: UUID,
    ) -> bool:
        with self._connection() as c, c.transaction():
            row = c.execute(
                "SELECT * FROM client_device_authorization WHERE id=%s FOR UPDATE",
                (device_id,),
            ).fetchone()
            if not row or row["state"] != "approved" or row["expires_at"] <= datetime.now(UTC):
                return False
            c.execute(
                "INSERT INTO client_user_installation(user_id,installation_id) VALUES(%s,%s) "
                "ON CONFLICT DO NOTHING",
                (row["user_id"], row["installation_id"]),
            )
            current = c.execute(
                "SELECT i.*,g.selection FROM client_user_installation i "
                "LEFT JOIN client_user_grant g ON i.active_grant=g.id "
                "WHERE i.user_id=%s AND i.installation_id=%s FOR UPDATE OF i",
                (row["user_id"], row["installation_id"]),
            ).fetchone()
            selection = row["selection"]
            if selection is None and row["purpose"] in {"login", "reauth"}:
                selection = current["selection"]
            c.execute(
                "INSERT INTO client_user_grant (id,user_id,installation_id,method,access_digest,"
                "refresh_digest,access_expires_at,refresh_expires_at,expires_at,selection,"
                "expected_generation) VALUES(%s,%s,%s,%s,%s,%s,now()+interval '15 minutes',"
                "now()+interval '7 days',now()+interval '30 days',%s,%s)",
                (
                    grant_id,
                    row["user_id"],
                    row["installation_id"],
                    row["authenticated_method"],
                    digest(access),
                    digest(refresh),
                    Jsonb(selection),
                    current["generation"],
                ),
            )
            c.execute(
                "UPDATE client_device_authorization SET state='consumed',result_cipher=%s,"
                "result_until=now()+interval '30 seconds' WHERE id=%s",
                (result_cipher, device_id),
            )
        return True

    def grant(self, token: str, *, image: bool = False) -> dict[str, Any]:
        column = "image_digest" if image else "access_digest"
        expiry = "image_expires_at" if image else "access_expires_at"
        with self._connection() as c:
            row = c.execute(
                f"SELECT g.*,u.email,u.display_name,u.role FROM client_user_grant g "
                f"JOIN app_user u ON u.id=g.user_id WHERE g.{column}=%s "
                f"AND g.{expiry}>now() AND g.expires_at>now() AND NOT g.revoked AND u.enabled",
                (digest(token),),
            ).fetchone()
        if not row:
            raise HTTPException(401, "Sign in again; authorization expired or revoked")
        return dict(row)

    def activate(self, grant_id: UUID) -> dict[str, Any]:
        with self._connection() as c, c.transaction():
            row = c.execute(
                "SELECT * FROM client_user_grant WHERE id=%s AND NOT revoked "
                "AND expires_at>now() FOR UPDATE",
                (grant_id,),
            ).fetchone()
            if not row:
                raise HTTPException(401, "Authorization revoked")
            if row["activated"] or row["selection"] is None:
                return dict(row)
            state = c.execute(
                "SELECT * FROM client_user_installation WHERE user_id=%s AND installation_id=%s "
                "FOR UPDATE",
                (row["user_id"], row["installation_id"]),
            ).fetchone()
            if state["generation"] != row["expected_generation"]:
                raise HTTPException(409, "A newer import is active; sign in and import again")
            c.execute(
                "UPDATE client_user_installation SET generation=generation+1,active_grant=%s,"
                "updated_at=now() WHERE user_id=%s AND installation_id=%s",
                (grant_id, row["user_id"], row["installation_id"]),
            )
            c.execute(
                "UPDATE client_user_grant SET revoked=TRUE WHERE user_id=%s AND installation_id=%s "
                "AND id<>%s",
                (row["user_id"], row["installation_id"], grant_id),
            )
            c.execute("UPDATE client_user_grant SET activated=TRUE WHERE id=%s", (grant_id,))
            row["activated"] = True
        return dict(row)

    def rotate(
        self,
        refresh: str,
        request_id: str,
        access: str,
        new_refresh: str,
        cipher: bytes,
    ) -> bytes | None:
        with self._connection() as c, c.transaction():
            row = c.execute(
                "SELECT g.* FROM client_user_grant g JOIN app_user u ON u.id=g.user_id "
                "WHERE (refresh_digest=%s OR previous_refresh_digest=%s) AND NOT revoked "
                "AND u.enabled AND refresh_expires_at>now() AND g.expires_at>now() FOR UPDATE OF g",
                (digest(refresh), digest(refresh)),
            ).fetchone()
            if not row:
                raise HTTPException(400, "invalid_grant")
            if row["previous_refresh_digest"] == digest(refresh):
                if (
                    row["refresh_request_id"] == request_id
                    and row["refresh_result_until"]
                    and row["refresh_result_until"] > datetime.now(UTC)
                ):
                    return bytes(row["refresh_result_cipher"])
                c.execute("UPDATE client_user_grant SET revoked=TRUE WHERE id=%s", (row["id"],))
                return None
            c.execute(
                "UPDATE client_user_grant SET previous_refresh_digest=refresh_digest,"
                "refresh_digest=%s,access_digest=%s,access_expires_at=now()+interval '15 minutes',"
                "refresh_expires_at=LEAST(expires_at,now()+interval '7 days'),"
                "refresh_request_id=%s,refresh_result_cipher=%s,"
                "refresh_result_until=now()+interval '30 seconds' WHERE id=%s",
                (digest(new_refresh), digest(access), request_id, cipher, row["id"]),
            )
        return cipher

    def image_key(self, grant_id: UUID, token: str) -> None:
        with self._connection() as c:
            c.execute(
                "UPDATE client_user_grant SET image_digest=%s,"
                "image_expires_at=LEAST(expires_at,now()+interval '1 day') "
                "WHERE id=%s AND activated AND NOT revoked",
                (digest(token), grant_id),
            )

    def revoke(self, grant_id: UUID) -> None:
        with self._connection() as c:
            c.execute("UPDATE client_user_grant SET revoked=TRUE WHERE id=%s", (grant_id,))

    def remove_image_key(self, grant_id: UUID) -> None:
        with self._connection() as c:
            c.execute(
                "UPDATE client_user_grant SET image_digest=NULL,image_expires_at=NULL WHERE id=%s",
                (grant_id,),
            )
