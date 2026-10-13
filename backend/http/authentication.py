from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.security_audit import AuditEventWrite
from turnstile_core.persistence.auth_store import AuthStore

from ..services.auth_service import (
    AuthError,
    EntraTokenVerifier,
    hash_session_token,
    new_session_token,
    session_expiry,
    verify_password,
)
from .security_audit_support import (
    OptionalAuditService,
    audit_request_context,
    record_audit_best_effort,
)
from .session import Config, CurrentSession, Store, require_allowed_write_origin

router = APIRouter()


@lru_cache
def get_entra_verifier() -> EntraTokenVerifier:
    settings = get_settings()
    return EntraTokenVerifier(
        client_id=settings.entra_client_id,
        allowed_email_domains=tuple(settings.entra_allowed_email_domains),
    )


Verifier = Annotated[EntraTokenVerifier, Depends(get_entra_verifier)]


class PasswordLogin(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class EntraLogin(BaseModel):
    id_token: str = Field(min_length=1)


class Profile(BaseModel):
    id: str
    email: str
    name: str | None
    role: str
    method: Literal["password", "entra"]
    session_expires_at: datetime


class AuthMeProfile(Profile):
    permissions: list[str]


def _issue(
    response: Response, store: AuthStore, user: dict[str, Any], method: str, settings: Settings
) -> Profile:
    token, digest = new_session_token()
    authenticated_at = datetime.now(UTC)
    expires_at = session_expiry(
        settings.session_ttl_hours_for(str(user["role"])),
        authenticated_at,
    )
    store.delete_expired_sessions()
    store.create_session(
        user_id=user["id"],
        token_sha256=digest,
        method=method,
        authenticated_at=authenticated_at,
        expires_at=expires_at,
    )
    store.touch_last_login(user["id"])
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_hours_for(str(user["role"])) * 3600,
        httponly=True,
        secure=settings.production,
        samesite="lax",
        path="/",
    )
    return Profile(
        id=str(user["id"]),
        email=user["email"],
        name=user["display_name"],
        role=user["role"],
        method=method,  # type: ignore[arg-type]
        session_expires_at=expires_at,
    )


@router.post(
    "/api/v1/auth/login",
    response_model=Profile,
    dependencies=[Depends(require_allowed_write_origin)],
)
def login(
    body: PasswordLogin,
    response: Response,
    store: Store,
    settings: Config,
    request: Request,
    audit_service: OptionalAuditService,
) -> Profile:
    user = store.find_user_by_email(body.email)
    if not user or not user["enabled"] or not verify_password(body.password, user["password_hash"]):
        context = audit_request_context(request, settings)
        disabled = bool(user and not user["enabled"])
        record_audit_best_effort(
            audit_service,
            AuditEventWrite(
                action="login.password",
                result="denied" if disabled else "failure",
                resource_type="session",
                actor_id=str(user["id"]) if user else None,
                actor_email=str(user["email"]) if user else None,
                actor_role=str(user["role"]) if user else None,
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                client_ip=context.client_ip,
                user_agent=context.user_agent,
                after={"reason_code": "account_disabled" if disabled else "invalid_credentials"},
            ),
        )
        raise HTTPException(status_code=401, detail="邮箱或密码不正确，请重新输入。")
    profile = _issue(response, store, user, "password", settings)
    context = audit_request_context(request, settings)
    record_audit_best_effort(
        audit_service,
        AuditEventWrite(
            action="login.password",
            result="success",
            resource_type="session",
            actor_id=str(user["id"]),
            actor_email=str(user["email"]),
            actor_role=str(user["role"]),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            client_ip=context.client_ip,
            user_agent=context.user_agent,
        ),
    )
    return profile


@router.post(
    "/api/v1/auth/entra",
    response_model=Profile,
    dependencies=[Depends(require_allowed_write_origin)],
)
def login_with_entra(
    body: EntraLogin,
    response: Response,
    store: Store,
    verifier: Verifier,
    settings: Config,
    request: Request,
    audit_service: OptionalAuditService,
) -> Profile:
    try:
        identity = verifier.verify(body.id_token)
    except AuthError as error:
        context = audit_request_context(request, settings)
        record_audit_best_effort(
            audit_service,
            AuditEventWrite(
                action="login.entra",
                result="failure",
                resource_type="session",
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                client_ip=context.client_ip,
                user_agent=context.user_agent,
                after={"reason_code": "token_invalid"},
            ),
        )
        raise HTTPException(status_code=401, detail=str(error)) from error
    user = store.upsert_entra_user(identity.email, identity.display_name)
    if not user.get("enabled", True):
        context = audit_request_context(request, settings)
        record_audit_best_effort(
            audit_service,
            AuditEventWrite(
                action="login.entra",
                result="denied",
                resource_type="session",
                actor_id=str(user.get("id")) if user.get("id") else None,
                actor_email=str(user.get("email")) if user.get("email") else None,
                actor_role=str(user.get("role")) if user.get("role") else None,
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                client_ip=context.client_ip,
                user_agent=context.user_agent,
                after={"reason_code": "account_disabled"},
            ),
        )
        raise HTTPException(status_code=403, detail="该账户已被停用。")
    profile = _issue(response, store, user, "entra", settings)
    context = audit_request_context(request, settings)
    record_audit_best_effort(
        audit_service,
        AuditEventWrite(
            action="login.entra",
            result="success",
            resource_type="session",
            actor_id=str(user["id"]),
            actor_email=str(user["email"]),
            actor_role=str(user["role"]),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            client_ip=context.client_ip,
            user_agent=context.user_agent,
        ),
    )
    return profile


@router.get("/api/v1/auth/me", response_model=AuthMeProfile)
def whoami(identity: CurrentSession) -> AuthMeProfile:
    return AuthMeProfile(
        id=identity.id,
        email=identity.email,
        name=identity.name,
        role=identity.role,
        method=identity.method,
        session_expires_at=identity.session_expires_at,
        permissions=sorted(identity.permissions),
    )


@router.post(
    "/api/v1/auth/logout",
    status_code=204,
    dependencies=[Depends(require_allowed_write_origin)],
)
def logout(
    request: Request,
    response: Response,
    store: Store,
    settings: Config,
    audit_service: OptionalAuditService,
) -> None:
    session = request.cookies.get(settings.session_cookie_name)
    user = store.session_owner(hash_session_token(session)) if session else None
    if session:
        store.delete_session(hash_session_token(session))
    response.delete_cookie(settings.session_cookie_name, path="/")
    context = audit_request_context(request, settings)
    record_audit_best_effort(
        audit_service,
        AuditEventWrite(
            action="session.logout",
            result="success",
            resource_type="session",
            actor_id=str(user["id"]) if user else None,
            actor_email=str(user["email"]) if user else None,
            actor_role=str(user["role"]) if user else None,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            client_ip=context.client_ip,
            user_agent=context.user_agent,
        ),
    )
