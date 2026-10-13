from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request

from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.security_audit import AuditEventWrite
from turnstile_core.persistence.auth_store import AuthStore

from ..services.auth_service import hash_session_token
from .security_audit_support import (
    OptionalAuditService,
    audit_request_context,
    record_audit_best_effort,
)


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
    role: str
    permissions: frozenset[str]
    method: Literal["password", "entra"]
    session_expires_at: datetime


def optional_authenticated_session(
    request: Request,
    store: Store,
    settings: Config,
) -> SessionIdentity | None:
    session = request.cookies.get(settings.session_cookie_name)
    if not session:
        return None
    owner = store.session_owner(hash_session_token(session))
    if not owner:
        return None
    role = str(owner["role"])
    method = str(owner["method"])
    if not role or method not in {"password", "entra"}:
        return None
    return SessionIdentity(
        id=str(owner["id"]),
        email=str(owner["email"]),
        name=owner.get("display_name"),
        role=role,
        permissions=frozenset(str(value) for value in owner.get("permissions", ())),
        method=method,  # type: ignore[arg-type]
        session_expires_at=owner["expires_at"],
    )


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
    if not role or method not in {"password", "entra"}:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录。")
    return SessionIdentity(
        id=str(owner["id"]),
        email=str(owner["email"]),
        name=owner.get("display_name"),
        role=role,
        permissions=frozenset(str(value) for value in owner.get("permissions", ())),
        method=method,  # type: ignore[arg-type]
        session_expires_at=owner["expires_at"],
    )


CurrentSession = Annotated[SessionIdentity, Depends(require_authenticated_session)]


def require_permission(permission: str) -> Callable[..., SessionIdentity]:
    def dependency(
        identity: CurrentSession,
        request: Request,
        settings: Config,
        audit_service: OptionalAuditService,
    ) -> SessionIdentity:
        if permission not in identity.permissions:
            context = audit_request_context(request, settings)
            record_audit_best_effort(
                audit_service,
                AuditEventWrite(
                    action="authorization.denied",
                    result="denied",
                    resource_type="permission",
                    resource_id=permission,
                    actor_id=identity.id,
                    actor_email=identity.email,
                    actor_role=identity.role,
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    client_ip=context.client_ip,
                    user_agent=context.user_agent,
                    after={"permission": permission},
                ),
            )
            raise HTTPException(status_code=403, detail="Permission denied")
        return identity

    dependency.__name__ = f"require_{permission.replace('.', '_').replace('-', '_')}"
    return dependency


def require_any_permission(*permissions: str) -> Callable[..., SessionIdentity]:
    def dependency(
        identity: CurrentSession,
        request: Request,
        settings: Config,
        audit_service: OptionalAuditService,
    ) -> SessionIdentity:
        if not identity.permissions.intersection(permissions):
            context = audit_request_context(request, settings)
            required = "|".join(permissions)
            record_audit_best_effort(
                audit_service,
                AuditEventWrite(
                    action="authorization.denied",
                    result="denied",
                    resource_type="permission",
                    resource_id=required,
                    actor_id=identity.id,
                    actor_email=identity.email,
                    actor_role=identity.role,
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    client_ip=context.client_ip,
                    user_agent=context.user_agent,
                    after={"permission": required},
                ),
            )
            raise HTTPException(status_code=403, detail="Permission denied")
        return identity

    dependency.__name__ = "require_any_permission"
    return dependency


def require_owner_session(
    identity: Annotated[
        SessionIdentity, Depends(require_permission("platform.manage"))
    ],
) -> SessionIdentity:
    """Keep the named platform-management boundary visible to route contracts."""
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
