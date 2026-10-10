from __future__ import annotations

from functools import lru_cache
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, HTTPException, Request

from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.directory import DirectoryError, MenuAdministratorScope
from turnstile_core.domain.menu_permissions import menu_permissions
from turnstile_core.persistence.auth_store import AuthStore
from turnstile_core.services.directory_catalog import directory_store

from ..services.auth_service import SessionIdentity as SessionIdentity
from ..services.auth_service import hash_session_token
from .menu_access import require_menu_access


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
    department_ids: tuple[str, ...] | None = None
    permission_revision = 0
    governance_user_id: str | None = None
    directory_person_active: bool | None = None
    menu_group = "user"
    menu_groups: tuple[str, ...] = ("user",)
    menu_scopes: tuple[MenuAdministratorScope, ...] = ()
    menus = menu_permissions(owner=role == "owner")
    data_scope = None
    if settings.directory_source == "legacy" and settings.database_url:
        state = directory_store(settings.database_url).state_if_present()
        if state is not None and state["source"] != "legacy":
            raise DirectoryError(
                "directory_source_mismatch",
                "Directory authority changed; update the application configuration",
                503,
            )
    if settings.directory_source == "database" and settings.database_url:
        directory = directory_store(settings.database_url)
        principal = directory.principal(UUID(str(owner["id"])), str(owner["email"]), role)
        department_ids = principal.department_ids
        permission_revision = principal.permission_revision
        menu_group = principal.menu_permission_group
        menu_groups = principal.menu_permission_groups
        menu_scopes = principal.menu_administrator_scopes
        menus = principal.menu_permissions
        data_scope = principal.data_scope
        person = directory.linked_person(UUID(str(owner["id"])))
        if person:
            directory_person_active = (
                person["status"] == "active"
                and not person["manual_disabled"]
                and not person["source_disabled"]
            )
            governance_user_id = str(person["governance_user_id"])
    identity = SessionIdentity(
        id=str(owner["id"]),
        email=str(owner["email"]),
        name=owner.get("display_name"),
        role=role,  # type: ignore[arg-type]
        method=method,  # type: ignore[arg-type]
        session_expires_at=owner["expires_at"],
        avatar_url=(
            f"/api/v1/user-settings/me/avatar?v={owner['avatar_revision']}"
            if method == "password" and owner.get("avatar_revision")
            else None
        ),
        directory_department_ids=department_ids,
        directory_permission_revision=permission_revision,
        governance_user_id=governance_user_id,
        directory_person_active=directory_person_active,
        menu_permission_group=menu_group,
        menu_permission_groups=menu_groups,
        menu_administrator_scopes=menu_scopes,
        menu_permissions=menus,
        data_scope=data_scope,
    )
    require_menu_access(request, identity)
    return identity


CurrentSession = Annotated[SessionIdentity, Depends(require_authenticated_session)]


def require_owner_session(identity: CurrentSession) -> SessionIdentity:
    if identity.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role is required")
    return identity


OwnerSession = Annotated[SessionIdentity, Depends(require_owner_session)]


def require_unscoped_read(identity: CurrentSession) -> None:
    if identity.directory_department_ids is not None:
        raise HTTPException(
            status_code=403,
            detail="This global data source has no verified department mapping",
        )


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
