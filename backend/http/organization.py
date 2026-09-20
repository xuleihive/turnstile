from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from turnstile_core.domain.organization import (
    ConsoleMember,
    ConsoleMemberList,
    ConsoleMemberRoleUpdate,
    ConsoleMemberStatusUpdate,
    OrganizationDirectory,
    OrgUnitCreate,
    OrgUnitRename,
    OrgUnitStatusUpdate,
)
from turnstile_core.services.organization import (
    OrganizationNotFoundError,
    OrganizationService,
)

from .dependencies import Repository
from .session import (
    CurrentSession,
    OwnerSession,
    Store,
    require_allowed_write_origin,
    require_authenticated_session,
)

router = APIRouter(
    prefix="/api/v1/organization",
    tags=["Organization"],
    dependencies=[
        Depends(require_authenticated_session),
        Depends(require_allowed_write_origin),
    ],
)


def organization_service(repository: Repository) -> OrganizationService:
    return OrganizationService(repository)


@router.get("/directory", response_model=OrganizationDirectory)
def get_organization_directory(
    repository: Repository,
    identity: CurrentSession,
) -> OrganizationDirectory:
    del identity
    return organization_service(repository).directory()


@router.post("/departments", response_model=OrganizationDirectory, status_code=201)
def create_department(
    request: OrgUnitCreate,
    repository: Repository,
    identity: OwnerSession,
) -> OrganizationDirectory:
    try:
        return organization_service(repository).create_department(request, identity.email)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put("/units/{unit_id}/name", response_model=OrganizationDirectory)
def rename_unit(
    unit_id: str,
    request: OrgUnitRename,
    repository: Repository,
    identity: OwnerSession,
) -> OrganizationDirectory:
    try:
        return organization_service(repository).rename(unit_id, request, identity.email)
    except OrganizationNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put("/units/{unit_id}/status", response_model=OrganizationDirectory)
def set_unit_status(
    unit_id: str,
    request: OrgUnitStatusUpdate,
    repository: Repository,
    identity: OwnerSession,
) -> OrganizationDirectory:
    try:
        return organization_service(repository).set_status(unit_id, request, identity.email)
    except OrganizationNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _members(store: Store, viewer_email: str) -> ConsoleMemberList:
    rows = store.list_users()
    members = [
        ConsoleMember.model_validate({
            "email": row["email"],
            "display_name": row.get("display_name"),
            "role": row["role"],
            "enabled": row["enabled"],
            # How they got in, not how they are allowed in: an account created by the
            # bootstrap command has a password, and one provisioned on first Microsoft
            # sign-in never does. Showing it is what makes an empty password column
            # explicable rather than alarming.
            "sign_in": "password" if row.get("has_password") else "microsoft",
            "created_at": row["created_at"],
            "last_login_at": row.get("last_login_at"),
            "is_self": str(row["email"]).casefold() == viewer_email.casefold(),
        })
        for row in rows
    ]
    return ConsoleMemberList(
        members=members,
        owner_count=sum(1 for item in members if item.role == "owner" and item.enabled),
    )


@router.get("/members", response_model=ConsoleMemberList)
def list_console_members(store: Store, identity: CurrentSession) -> ConsoleMemberList:
    return _members(store, identity.email)


@router.put("/members/{email}/role", response_model=ConsoleMemberList)
def set_console_member_role(
    email: str,
    request: ConsoleMemberRoleUpdate,
    store: Store,
    identity: OwnerSession,
) -> ConsoleMemberList:
    try:
        updated = store.set_user_role(email, request.role, identity.email)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if updated is None:
        raise HTTPException(status_code=404, detail=f"No account for {email}")
    return _members(store, identity.email)


@router.put("/members/{email}/status", response_model=ConsoleMemberList)
def set_console_member_status(
    email: str,
    request: ConsoleMemberStatusUpdate,
    store: Store,
    identity: OwnerSession,
) -> ConsoleMemberList:
    try:
        updated = store.set_user_enabled(email, request.enabled, identity.email)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if updated is None:
        raise HTTPException(status_code=404, detail=f"No account for {email}")
    return _members(store, identity.email)
