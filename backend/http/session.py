from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request

from turnstile_core.config import Settings, get_settings
from turnstile_core.persistence.auth_store import AuthStore

from ..services.auth_service import AuthError, EntraAccessTokenVerifier, hash_session_token


@lru_cache
def get_auth_store() -> AuthStore:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for authentication")
    return AuthStore(settings.database_url)


Store = Annotated[AuthStore, Depends(get_auth_store)]
Config = Annotated[Settings, Depends(get_settings)]


@dataclass(frozen=True)
class SessionIdentity:
    id: str
    email: str
    name: str | None
    role: Literal["owner", "member"]
    method: Literal["password", "entra"]
    session_expires_at: datetime


@lru_cache
def get_entra_access_verifier() -> EntraAccessTokenVerifier:
    settings = get_settings()
    return EntraAccessTokenVerifier(
        client_id=settings.entra_client_id,
        allowed_email_domains=tuple(settings.entra_allowed_email_domains),
        tenant_ids=tuple(settings.entra_tenant_ids),
    )


AccessVerifier = Annotated[EntraAccessTokenVerifier, Depends(get_entra_access_verifier)]


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def _token_identity(
    token: str, store: AuthStore, settings: Settings, verifier: EntraAccessTokenVerifier
) -> SessionIdentity:
    """An Entra access token in place of a session, for scripts and automation.

    Accepted only on an admin-only, tenant-pinned deployment, and only with the admin
    role -- so it is exactly as strong as signing in, never weaker, and a deployment that
    has not opted in behaves as it always did.
    """
    if not settings.entra_admin_role or not settings.entra_tenant_ids:
        raise HTTPException(status_code=401, detail="未登录。")
    try:
        caller = verifier.verify(token)
    except AuthError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    if settings.entra_admin_role not in caller.roles:
        raise HTTPException(status_code=403, detail="此控制台仅限管理员使用。")
    subject = caller.subject
    if caller.delegated:
        # The same person signing in would be provisioned as Owner; a token must not give
        # them an identity that the rest of the application cannot find.
        user = store.find_user_by_email(caller.email)
        if not user or user.get("role") != "owner":
            user = store.upsert_entra_user(caller.email, caller.display_name, role="owner")
        if not user.get("enabled", True):
            raise HTTPException(status_code=403, detail="该账户已被停用。")
        subject = str(user["id"])
    return SessionIdentity(
        id=subject,
        email=caller.email,
        name=caller.display_name,
        role="owner",
        method="entra",
        session_expires_at=caller.expires_at,
    )


def require_authenticated_session(
    request: Request,
    store: Store,
    settings: Config,
    verifier: AccessVerifier,
) -> SessionIdentity:
    session = request.cookies.get(settings.session_cookie_name)
    if not session:
        token = _bearer_token(request)
        if token is not None:
            return _token_identity(token, store, settings, verifier)
        raise HTTPException(status_code=401, detail="未登录。")
    owner = store.session_owner(hash_session_token(session))
    if not owner:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录。")
    role = str(owner["role"])
    method = str(owner["method"])
    if role not in {"owner", "member"} or method not in {"password", "entra"}:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录。")
    return SessionIdentity(
        id=str(owner["id"]),
        email=str(owner["email"]),
        name=owner.get("display_name"),
        role=role,  # type: ignore[arg-type]
        method=method,  # type: ignore[arg-type]
        session_expires_at=owner["expires_at"],
    )


CurrentSession = Annotated[SessionIdentity, Depends(require_authenticated_session)]


def require_owner_session(identity: CurrentSession) -> SessionIdentity:
    if identity.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role is required")
    return identity


OwnerSession = Annotated[SessionIdentity, Depends(require_owner_session)]


def require_allowed_write_origin(request: Request, settings: Config) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    origin = request.headers.get("origin")
    if origin is None:
        return
    normalized = origin.rstrip("/")
    allowed = {value.rstrip("/") for value in settings.cors_origins}
    same_host = urlsplit(normalized).netloc == request.headers.get("host")
    if normalized not in allowed and not same_host:
        raise HTTPException(status_code=403, detail="不允许从该来源执行写操作。")
