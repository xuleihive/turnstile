from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from turnstile_core.persistence.auth_store import (
    AccountSessionInvalid,
    PasswordMismatch,
    PasswordRateLimited,
)

from ..services.auth_service import hash_password, hash_session_token, verify_password
from ..services.user_avatar_service import AVATAR_MAX_BODY_BYTES, normalize_avatar
from ..services.user_settings_models import (
    AccountInformation,
    AvatarUpdate,
    PasswordUpdate,
    PersonalAvatar,
    PersonalBudget,
    PersonalModelList,
    PersonalUsage,
    ProfileUpdate,
)
from ..services.user_settings_service import UserSettingsService
from .authentication import Profile, whoami
from .dependencies import Repository
from .session import (
    Config,
    CurrentSession,
    SessionIdentity,
    Store,
    require_allowed_write_origin,
)


class UserSettingsBodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/user-settings/"):
            await self.app(scope, receive, send)
            return
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > AVATAR_MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="请求内容过大。")
            return message

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ]
                message["headers"] = [*headers, (b"cache-control", b"no-store")]
            await send(message)

        await self.app(scope, bounded_receive, private_send)


def private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/api/v1/user-settings/me",
    dependencies=[Depends(private_response), Depends(require_allowed_write_origin)],
)


def service(repository: Repository, settings: Config) -> UserSettingsService:
    return UserSettingsService(repository, settings)


Service = Annotated[UserSettingsService, Depends(service)]


def password_session(identity: CurrentSession) -> SessionIdentity:
    if identity.method != "password":
        raise HTTPException(status_code=403, detail="Microsoft 账号信息仅支持查看。")
    return identity


PasswordSession = Annotated[SessionIdentity, Depends(password_session)]


def digest(request: Request, settings: Config) -> str:
    return hash_session_token(request.cookies[settings.session_cookie_name])


def query_parameters(request: Request, allowed: set[str]) -> None:
    if set(request.query_params) - allowed:
        raise HTTPException(status_code=422, detail="包含不支持的查询参数。")


@router.get("/account", response_model=AccountInformation)
def account(identity: CurrentSession, service: Service, request: Request) -> AccountInformation:
    query_parameters(request, set())
    return service.account(identity)


@router.get("/budget", response_model=PersonalBudget)
def budget(
    identity: CurrentSession, service: Service, request: Request, period: str | None = None
) -> PersonalBudget:
    query_parameters(request, {"period"})
    try:
        return service.budget(identity, period)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/models", response_model=PersonalModelList)
def models(identity: CurrentSession, service: Service, request: Request) -> PersonalModelList:
    query_parameters(request, set())
    return service.models(identity)


@router.get("/usage", response_model=PersonalUsage)
def usage(
    identity: CurrentSession,
    service: Service,
    request: Request,
    period: str | None = None,
    interval: Literal["hour", "day"] = "day",
    timezone: Annotated[str, Query(max_length=100)] = "UTC",
) -> PersonalUsage:
    query_parameters(request, {"period", "interval", "timezone"})
    try:
        return service.usage(identity, period, interval, timezone)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.patch("/profile", response_model=Profile)
def update_profile(
    body: ProfileUpdate,
    identity: PasswordSession,
    store: Store,
    settings: Config,
    request: Request,
    response: Response,
) -> Profile:
    query_parameters(request, set())
    try:
        store.update_display_name(UUID(identity.id), digest(request, settings), body.display_name)
    except AccountSessionInvalid as error:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录。") from error
    return whoami(replace(identity, name=body.display_name), response)


@router.put("/avatar", response_model=PersonalAvatar)
def update_avatar(
    body: AvatarUpdate,
    identity: PasswordSession,
    store: Store,
    settings: Config,
    request: Request,
) -> PersonalAvatar:
    query_parameters(request, set())
    media_type, content = None, None
    if body.avatar_data_url is not None:
        try:
            media_type, content = normalize_avatar(body.avatar_data_url)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        row = store.update_avatar(UUID(identity.id), digest(request, settings), media_type, content)
    except AccountSessionInvalid as error:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录。") from error
    return PersonalAvatar(
        avatar_url=f"/api/v1/user-settings/me/avatar?v={row['revision']}" if row else None,
        updated_at=row["updated_at"] if row else None,
    )


@router.get(
    "/avatar",
    response_class=Response,
    responses={
        200: {
            "description": "Private normalized avatar",
            "content": {
                media_type: {"schema": {"type": "string", "format": "binary"}}
                for media_type in ("image/png", "image/jpeg", "image/webp")
            },
        },
    },
)
def avatar(
    identity: PasswordSession, store: Store, request: Request, v: UUID | None = None
) -> Response:
    query_parameters(request, {"v"})
    row = store.avatar(UUID(identity.id))
    if not row or (v is not None and UUID(str(row["revision"])) != v):
        raise HTTPException(status_code=404, detail="未设置头像或头像已更新。")
    return Response(
        bytes(row["image_bytes"]),
        media_type=row["media_type"],
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/password", status_code=204)
def password(
    body: PasswordUpdate,
    identity: PasswordSession,
    store: Store,
    settings: Config,
    request: Request,
    response: Response,
) -> None:
    query_parameters(request, set())
    try:
        store.change_password(
            UUID(identity.id),
            digest(request, settings),
            body.current_password.get_secret_value(),
            body.new_password.get_secret_value(),
            verify_password,
            hash_password,
        )
    except AccountSessionInvalid as error:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录。") from error
    except PasswordMismatch as error:
        raise HTTPException(status_code=400, detail="当前密码不正确。") from error
    except PasswordRateLimited as error:
        raise HTTPException(
            status_code=429,
            detail="密码验证次数过多，请稍后重试。",
            headers={"Retry-After": str(error.retry_after)},
        ) from error
    response.delete_cookie(settings.session_cookie_name, path="/")
