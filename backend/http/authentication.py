from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from turnstile_core.config import Settings, get_settings
from turnstile_core.persistence.auth_store import AuthStore

from ..services.auth_service import (
    AuthError,
    EntraTokenVerifier,
    hash_session_token,
    new_session_token,
    session_expiry,
    verify_password,
)
from .session import Config, CurrentSession, Store, require_allowed_write_origin

router = APIRouter()


@lru_cache
def get_entra_verifier() -> EntraTokenVerifier:
    settings = get_settings()
    return EntraTokenVerifier(
        client_id=settings.entra_client_id,
        allowed_email_domains=tuple(settings.entra_allowed_email_domains),
        tenant_ids=tuple(settings.entra_tenant_ids),
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
    role: Literal["owner", "member"]
    method: Literal["password", "entra"]
    session_expires_at: datetime


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
def login(body: PasswordLogin, response: Response, store: Store, settings: Config) -> Profile:
    user = store.find_user_by_email(body.email)
    if not user or not user["enabled"] or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码不正确，请重新输入。")
    return _issue(response, store, user, "password", settings)


@router.post(
    "/api/v1/auth/entra",
    response_model=Profile,
    dependencies=[Depends(require_allowed_write_origin)],
)
def login_with_entra(
    body: EntraLogin, response: Response, store: Store, verifier: Verifier, settings: Config
) -> Profile:
    try:
        identity = verifier.verify(body.id_token)
    except AuthError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    role: str | None = None
    if settings.entra_admin_role:
        # Checked before any row is written: someone without the role must not be left
        # with an account, even a disabled one, because the role assignment in Entra is
        # the only record of who administers this console.
        if settings.entra_admin_role not in identity.roles:
            raise HTTPException(status_code=403, detail="此控制台仅限管理员使用。")
        role = "owner"
    user = store.upsert_entra_user(identity.email, identity.display_name, role=role)
    if not user.get("enabled", True):
        raise HTTPException(status_code=403, detail="该账户已被停用。")
    return _issue(response, store, user, "entra", settings)


@router.get("/api/v1/auth/me", response_model=Profile)
def whoami(identity: CurrentSession) -> Profile:
    return Profile(
        id=identity.id,
        email=identity.email,
        name=identity.name,
        role=identity.role,
        method=identity.method,
        session_expires_at=identity.session_expires_at,
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
) -> None:
    session = request.cookies.get(settings.session_cookie_name)
    if session:
        store.delete_session(hash_session_token(session))
    response.delete_cookie(settings.session_cookie_name, path="/")