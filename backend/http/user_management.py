from __future__ import annotations

import logging
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, SecretStr

from turnstile_core.config import get_settings
from turnstile_core.domain.access_control import (
    AccessActor,
    AccessControlError,
    RequestAuditContext,
)
from turnstile_core.persistence.access_control_store import AccessControlStore
from turnstile_core.services.access_control import AccessControlService

from ..services.auth_service import hash_password
from .security_audit_support import audit_request_context
from .service_dependencies import ApplicationAccessServiceDependency
from .session import SessionIdentity, require_permission

router = APIRouter()
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_access_control_store() -> AccessControlStore:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for user management")
    return AccessControlStore(settings.database_url)


def get_access_control_service(
    store: Annotated[AccessControlStore, Depends(get_access_control_store)],
) -> AccessControlService:
    return AccessControlService(store)


AccessService = Annotated[AccessControlService, Depends(get_access_control_service)]
UserReader = Annotated[SessionIdentity, Depends(require_permission("users.read"))]
UserManager = Annotated[SessionIdentity, Depends(require_permission("users.manage"))]
RoleReader = Annotated[SessionIdentity, Depends(require_permission("roles.read"))]
OrganizationReader = Annotated[SessionIdentity, Depends(require_permission("org.read"))]
OrganizationManager = Annotated[SessionIdentity, Depends(require_permission("org.manage"))]


class UserDirectoryEntry(BaseModel):
    id: UUID
    email: str
    display_name: str | None
    role: str
    enabled: bool
    organization_unit_id: UUID | None
    organization_unit_name: str | None = None
    created_at: datetime | None = None
    last_login_at: datetime | None = None


class RoleEntry(BaseModel):
    code: str
    display_name: str
    is_builtin: bool
    permissions: list[str]


class OrganizationUnitEntry(BaseModel):
    id: UUID
    parent_id: UUID | None
    code: str
    display_name: str
    enabled: bool
    created_at: datetime | None = None


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=160)
    role: str = Field(default="member", min_length=1, max_length=64)
    department_id: UUID | None = None
    initial_password: SecretStr | None = None


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)
    role: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    department_id: UUID | None = None


class OrganizationUnitCreate(BaseModel):
    parent_id: UUID | None = None
    code: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=160)


class OrganizationUnitPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)
    enabled: bool | None = None


def _actor(identity: SessionIdentity) -> AccessActor:
    return AccessActor(user_id=identity.id, email=identity.email, role=identity.role)


def _audit_context(request: Request) -> RequestAuditContext:
    return audit_request_context(request, get_settings())


def _http_error(error: AccessControlError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


@router.get("/api/v1/admin/users", response_model=list[UserDirectoryEntry])
def list_users(
    _identity: UserReader,
    service: AccessService,
    search: Annotated[str | None, Query(max_length=200)] = None,
    role: Annotated[str | None, Query(max_length=64)] = None,
    enabled: bool | None = None,
    department_id: UUID | None = None,
) -> list[UserDirectoryEntry]:
    rows = service.list_users(
        search=search,
        role=role,
        enabled=enabled,
        organization_unit_id=str(department_id) if department_id else None,
    )
    return [UserDirectoryEntry.model_validate(row) for row in rows]


@router.get("/api/v1/admin/roles", response_model=list[RoleEntry])
def list_roles(identity: RoleReader, service: AccessService) -> list[RoleEntry]:
    del identity
    return [RoleEntry.model_validate(row) for row in service.list_roles()]


@router.get(
    "/api/v1/admin/organization-units", response_model=list[OrganizationUnitEntry]
)
def list_organization_units(
    _identity: OrganizationReader, service: AccessService
) -> list[OrganizationUnitEntry]:
    return [
        OrganizationUnitEntry.model_validate(row)
        for row in service.list_organization_units()
    ]


@router.post("/api/v1/admin/users", response_model=UserDirectoryEntry, status_code=201)
def create_user(
    identity: UserManager,
    service: AccessService,
    body: UserCreate,
    request: Request,
) -> UserDirectoryEntry:
    initial_password = (
        body.initial_password.get_secret_value()
        if body.initial_password is not None
        else None
    )
    if initial_password is not None and not 12 <= len(initial_password) <= 256:
        raise HTTPException(
            status_code=422,
            detail="Initial password must contain between 12 and 256 characters",
        )
    try:
        result = service.create_user(
            email=body.email,
            display_name=body.display_name,
            role=body.role,
            organization_unit_id=body.department_id,
            password_hash=hash_password(initial_password) if initial_password is not None else None,
            actor=_actor(identity),
            audit=_audit_context(request),
        )
    except AccessControlError as error:
        raise _http_error(error) from error
    return UserDirectoryEntry.model_validate(result)


@router.patch("/api/v1/admin/users/{user_id}", response_model=UserDirectoryEntry)
def update_user(
    user_id: UUID,
    identity: UserManager,
    service: AccessService,
    application_service: ApplicationAccessServiceDependency,
    body: UserPatch,
    request: Request,
) -> UserDirectoryEntry:
    changes: dict[str, Any] = body.model_dump(exclude_unset=True, exclude={"department_id"})
    if "department_id" in body.model_fields_set:
        changes["organization_unit_id"] = body.department_id
    if any(value is None for key, value in changes.items() if key in {"role", "enabled"}):
        raise HTTPException(status_code=422, detail="Role and enabled cannot be null")
    try:
        result = service.update_user(
            user_id,
            changes,
            actor=_actor(identity),
            audit=_audit_context(request),
        )
    except AccessControlError as error:
        raise _http_error(error) from error
    if changes.keys() & {"enabled", "display_name", "organization_unit_id"}:
        try:
            application_service.refresh_application_mappings(user_id)
        except Exception as error:
            logger.warning(
                "Application access mapping refresh failed after user update: "
                "user_id=%s error_type=%s",
                user_id,
                type(error).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="User state was saved, but APIM access mapping refresh is pending.",
            ) from None
    return UserDirectoryEntry.model_validate(result)


@router.post(
    "/api/v1/admin/organization-units",
    response_model=OrganizationUnitEntry,
    status_code=201,
)
def create_organization_unit(
    identity: OrganizationManager,
    service: AccessService,
    body: OrganizationUnitCreate,
    request: Request,
) -> OrganizationUnitEntry:
    try:
        result = service.create_organization_unit(
            parent_id=body.parent_id,
            code=body.code,
            display_name=body.display_name,
            actor=_actor(identity),
            audit=_audit_context(request),
        )
    except AccessControlError as error:
        raise _http_error(error) from error
    return OrganizationUnitEntry.model_validate(result)


@router.patch(
    "/api/v1/admin/organization-units/{unit_id}",
    response_model=OrganizationUnitEntry,
)
def update_organization_unit(
    unit_id: UUID,
    identity: OrganizationManager,
    service: AccessService,
    application_service: ApplicationAccessServiceDependency,
    body: OrganizationUnitPatch,
    request: Request,
) -> OrganizationUnitEntry:
    changes = body.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise HTTPException(status_code=422, detail="Organization unit fields cannot be null")
    try:
        result = service.update_organization_unit(
            unit_id,
            changes,
            actor=_actor(identity),
            audit=_audit_context(request),
        )
    except AccessControlError as error:
        raise _http_error(error) from error
    try:
        application_service.refresh_application_mappings()
    except Exception as error:
        logger.warning(
            "Application access mapping refresh failed after organization update: "
            "organization_unit_id=%s error_type=%s",
            unit_id,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Organization unit state was saved, but APIM access mapping refresh is pending."
            ),
        ) from None
    return OrganizationUnitEntry.model_validate(result)


@router.delete("/api/v1/admin/organization-units/{unit_id}", status_code=204)
def delete_organization_unit(
    unit_id: UUID,
    identity: OrganizationManager,
    service: AccessService,
    request: Request,
) -> Response:
    try:
        service.delete_organization_unit(
            unit_id,
            actor=_actor(identity),
            audit=_audit_context(request),
        )
    except AccessControlError as error:
        raise _http_error(error) from error
    return Response(status_code=204)
