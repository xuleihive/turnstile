from __future__ import annotations

from functools import lru_cache
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request

from turnstile_core.config import Settings, get_settings
from turnstile_core.persistence.auth_store import AuthStore

from ..services.auth_service import SessionIdentity as SessionIdentity
from ..services.auth_service import hash_session_token


@lru_cache
def get_auth_store() -> AuthStore:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for authentication")
    return AuthStore(settings.database_url)


Store = Annotated[AuthStore, Depends(get_auth_store)]
Config = Annotated[Settings, Depends(get_settings)]


def require_authenticated_session(
    request: Request,
    store: Store,
    settings: Config,
) -> SessionIdentity:
    session = request.cookies.get(settings.session_cookie_name)
    if not session:
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
        avatar_url=(
            f"/api/v1/user-settings/me/avatar?v={owner['avatar_revision']}"
            if method == "password" and owner.get("avatar_revision") else None
        ),
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
