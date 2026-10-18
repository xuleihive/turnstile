from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.directory import MenuAdministratorScope
from turnstile_core.domain.menu_permissions import MenuPermissionGroup, menu_permissions
from turnstile_core.persistence.auth_store import AuthStore, ExternalIdentityConflict
from turnstile_core.services.directory_catalog import directory_store

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
        require_object_id=settings.directory_source == "database",
        allowed_tenant_ids=tuple(settings.entra_allowed_tenant_ids),
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
    avatar_url: str | None = None
    directory_scope_key: str = "global"
    directory_permission_revision: int = 0
    governance_user_id: str | None = None
    menu_permission_group: str = "user"
    menu_permission_groups: list[str] = Field(default_factory=lambda: ["user"])
    menu_permissions: list[str] = Field(default_factory=list)
    menu_administrator_scopes: list[MenuAdministratorScope] = Field(default_factory=list)


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
    issued = store.create_session(
        user_id=user["id"],
        token_sha256=digest,
        method=method,
        authenticated_at=authenticated_at,
        expires_at=expires_at,
        expected_password_hash=user["password_hash"] if method == "password" else None,
    )
    if not issued:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录。")
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
    response.headers["Cache-Control"] = "no-store"
    owner = store.session_owner(digest)
    scope_key = "global"
    permission_revision = 0
    governance_user_id = None
    menu_group: MenuPermissionGroup = "user"
    menu_groups: tuple[MenuPermissionGroup, ...] = ("user",)
    menu_scopes: tuple[MenuAdministratorScope, ...] = ()
    menus = menu_permissions(owner=user["role"] == "owner")
    if settings.directory_source == "database" and settings.database_url:
        directory = directory_store(settings.database_url)
        principal = directory.principal(UUID(str(user["id"])), user["email"], user["role"])
        if principal.data_scope is not None:
            scope_key = hashlib.sha256(
                json.dumps(principal.data_scope.__dict__, sort_keys=True).encode()
            ).hexdigest()
        elif principal.department_ids is not None:
            scope_key = hashlib.sha256(
                json.dumps(sorted(principal.department_ids)).encode()
            ).hexdigest()
        permission_revision = principal.permission_revision
        menu_group = principal.menu_permission_group
        menu_groups = principal.menu_permission_groups
        menu_scopes = principal.menu_administrator_scopes
        menus = principal.menu_permissions
        person = directory.linked_person(UUID(str(user["id"])))
        governance_user_id = str(person["governance_user_id"]) if person else None
    return Profile(
        id=str(user["id"]),
        email=user["email"],
        name=user["display_name"],
        role=user["role"],
        method=method,  # type: ignore[arg-type]
        session_expires_at=expires_at,
        avatar_url=(
            f"/api/v1/user-settings/me/avatar?v={owner['avatar_revision']}"
            if method == "password" and owner and owner.get("avatar_revision")
            else None
        ),
        directory_scope_key=scope_key,
        directory_permission_revision=permission_revision,
        governance_user_id=governance_user_id,
        menu_permission_group=menu_group,
        menu_permission_groups=list(menu_groups),
        menu_administrator_scopes=list(menu_scopes),
        menu_permissions=list(menus),
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
    if settings.directory_source == "database":
        if not identity.object_id:
            raise HTTPException(status_code=401, detail="Microsoft 账户缺少可信对象身份。")
        try:
            user = store.resolve_entra_identity(
                cloud="public",
                tenant_id=UUID(identity.tenant_id),
                object_id=UUID(identity.object_id),
                email=identity.email,
                display_name=identity.display_name,
            )
        except ExternalIdentityConflict as error:
            raise HTTPException(
                status_code=409,
                detail="该邮箱已有账户，请由管理员明确关联 Entra 身份。",
            ) from error
    else:
        user = store.upsert_entra_user(identity.email, identity.display_name)
    if not user.get("enabled", True):
        raise HTTPException(status_code=403, detail="该账户已被停用。")
    return _issue(response, store, user, "entra", settings)


@router.get("/api/v1/auth/me", response_model=Profile)
def whoami(identity: CurrentSession, response: Response) -> Profile:
    response.headers["Cache-Control"] = "no-store"
    return Profile(
        id=identity.id,
        email=identity.email,
        name=identity.name,
        role=identity.role,
        method=identity.method,
        session_expires_at=identity.session_expires_at,
        avatar_url=identity.avatar_url,
        directory_scope_key=identity.directory_scope_key,
        directory_permission_revision=identity.directory_permission_revision,
        governance_user_id=identity.governance_user_id,
        menu_permission_group=identity.menu_permission_group,
        menu_permission_groups=list(identity.menu_permission_groups),
        menu_administrator_scopes=list(identity.menu_administrator_scopes),
        menu_permissions=list(identity.menu_permissions),
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
