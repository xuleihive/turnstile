from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from turnstile_core.domain.directory import (
    AccountLinkWrite,
    AdministratorScopeKind,
    AdministratorsWrite,
    DirectoryError,
    DirectoryPage,
    DirectoryPrincipal,
    OrganizationPersonCreate,
    OrganizationWrite,
    PersonStatusWrite,
    PersonWrite,
    TeamsWrite,
    UnitMembersWrite,
    UnitWrite,
)
from turnstile_core.services.directory_catalog import directory_store
from turnstile_core.services.directory_service import DirectoryService, json_value

from .session import Config, CurrentSession, require_allowed_write_origin

router = APIRouter(
    prefix="/api/v1/organization-management",
    tags=["Organization management"],
    dependencies=[Depends(require_allowed_write_origin)],
)


def directory_service(settings: Config) -> DirectoryService:
    if not settings.organization_management_enabled:
        raise HTTPException(status_code=404, detail="Organization management is not enabled")
    if not settings.database_url or settings.directory_source != "database":
        raise HTTPException(status_code=503, detail="Persistent directory is not configured")
    store = directory_store(settings.database_url)
    store.require_ready()
    return DirectoryService(store)


Service = Annotated[DirectoryService, Depends(directory_service)]


def directory_principal(identity: CurrentSession, service: Service) -> DirectoryPrincipal:
    principal = service.store.principal(UUID(identity.id), identity.email, identity.role)
    if not principal.owner and not principal.department_ids:
        raise HTTPException(status_code=403, detail="Directory management access is not granted")
    return principal


Principal = Annotated[DirectoryPrincipal, Depends(directory_principal)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]


@router.get("/capabilities")
def capabilities(
    identity: CurrentSession,
    settings: Config,
    response: Response,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if not settings.organization_management_enabled or not settings.database_url:
        return {
            "available": False,
            "can_read": False,
            "can_manage_organizations": False,
            "can_manage_connections": False,
            "capabilities": [],
            "department_ids": [],
            "permission_revision": 0,
            "directory_version": 0,
            "protocol_version": 1,
        }
    store = directory_store(settings.database_url)
    principal = store.principal(UUID(identity.id), identity.email, identity.role)
    return {
        "available": True,
        **DirectoryService(store).capabilities(principal),
        "sync_available": settings.directory_sync_enabled,
        "identity_projection_available": settings.directory_identity_projection_enabled,
        "managed_identity_tenant_id": (
            settings.directory_managed_identity_tenant_id if principal.owner else None
        ),
        "deployment_cloud": settings.directory_deployment_cloud,
    }


@router.get("/organizations")
def organizations(service: Service, principal: Principal) -> list[dict[str, Any]]:
    return json_value(service.organizations(principal))


@router.get("/organizations/{organization_id}")
def organization(
    organization_id: str,
    service: Service,
    principal: Principal,
) -> dict[str, Any]:
    row = next(
        (row for row in service.organizations(principal) if row["id"] == organization_id),
        None,
    )
    if row is None:
        raise DirectoryError("scope_not_found", "Organization not found", 404)
    return json_value(row)


@router.post("/organizations", status_code=201)
def create_organization(
    write: OrganizationWrite,
    service: Service,
    principal: Principal,
    key: IdempotencyKey = None,
) -> dict[str, Any]:
    return service.create_organization(principal, write, key)


@router.patch("/organizations/{organization_id}")
def update_organization(
    organization_id: str,
    write: OrganizationWrite,
    service: Service,
    principal: Principal,
) -> dict[str, Any]:
    return service.update_organization(principal, organization_id, write)


@router.get("/organizations/{organization_id}/units")
def units(
    organization_id: str,
    service: Service,
    principal: Principal,
) -> list[dict[str, Any]]:
    return json_value(service.units(principal, organization_id))


@router.post("/organizations/{organization_id}/units", status_code=201)
def create_unit(
    organization_id: str,
    write: UnitWrite,
    service: Service,
    principal: Principal,
    key: IdempotencyKey = None,
) -> dict[str, Any]:
    return service.create_unit(principal, organization_id, write, key)


@router.patch("/units/{unit_id}")
def update_unit(
    unit_id: str,
    write: UnitWrite,
    service: Service,
    principal: Principal,
) -> dict[str, Any]:
    return service.update_unit(principal, unit_id, write)


@router.get("/people", response_model=DirectoryPage)
def people(
    service: Service,
    principal: Principal,
    department_id: Annotated[str | None, Query(max_length=255)] = None,
    team_id: Annotated[str | None, Query(max_length=255)] = None,
    organization_id: Annotated[str | None, Query(max_length=255)] = None,
    query: Annotated[str, Query(max_length=200)] = "",
    status: Literal["active", "inactive", "archived"] | None = None,
    cursor: Annotated[str | None, Query(pattern=r"^\d{1,9}$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    available_unit_id: Annotated[str | None, Query(max_length=255)] = None,
) -> DirectoryPage:
    return service.people(
        principal,
        department_id=department_id,
        query=query,
        status=status,
        team_id=team_id,
        organization_id=organization_id,
        offset=int(cursor or 0),
        limit=limit,
        available_unit_id=available_unit_id,
    )


@router.get("/people/{person_id}")
def person(
    person_id: UUID,
    service: Service,
    principal: Principal,
) -> dict[str, Any]:
    with service.store.connection() as connection:
        row = service._person(connection, person_id)
    if (
        principal.data_scope is not None
        and row["governance_user_id"] not in principal.data_scope.user_ids
    ):
        raise HTTPException(status_code=404, detail="Person not found")
    if principal.data_scope is None:
        principal.require("directory.read", row["department_id"])
    return json_value(row)


@router.post("/people", status_code=201)
def create_person(
    write: OrganizationPersonCreate,
    service: Service,
    principal: Principal,
    key: IdempotencyKey = None,
) -> dict[str, Any]:
    return service.create_organization_person(principal, write, key)


@router.post("/units/{unit_id}/members")
def add_unit_members(
    unit_id: str, write: UnitMembersWrite, service: Service, principal: Principal,
) -> dict[str, Any]:
    return service.add_unit_members(principal, unit_id, write)


@router.patch("/people/{person_id}")
def update_person(
    person_id: UUID,
    write: PersonWrite,
    service: Service,
    principal: Principal,
) -> dict[str, Any]:
    return service.update_person(principal, person_id, write)


@router.put("/people/{person_id}/teams")
def set_teams(
    person_id: UUID,
    write: TeamsWrite,
    service: Service,
    principal: Principal,
) -> dict[str, Any]:
    return service.set_teams(principal, person_id, write)


@router.get("/accounts")
def accounts(
    service: Service,
    principal: Principal,
    query: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict[str, Any]]:
    principal.require_owner()
    with service.store.connection() as connection:
        rows = connection.execute(
            """SELECT id,email,display_name,role,enabled FROM app_user
               WHERE enabled AND (email ILIKE %s OR display_name ILIKE %s)
               ORDER BY email LIMIT %s""",
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()
    return json_value(rows)


@router.post("/people/{person_id}/account-link")
def link_account(
    person_id: UUID,
    write: AccountLinkWrite,
    service: Service,
    principal: Principal,
) -> dict[str, Any]:
    return service.link_account(principal, person_id, write)


@router.get("/departments/{department_id}/administrators")
def administrators(
    department_id: str,
    service: Service,
    principal: Principal,
) -> list[dict[str, Any]]:
    return service.administrators(principal, department_id)


@router.put("/departments/{department_id}/administrators")
def set_administrators(
    department_id: str,
    write: AdministratorsWrite,
    service: Service,
    principal: Principal,
) -> dict[str, Any]:
    return service.set_administrators(principal, department_id, write)


@router.get("/menu-administrators/{scope_kind}/{scope_id}")
def menu_administrators(
    scope_kind: AdministratorScopeKind, scope_id: str, service: Service, principal: Principal,
) -> list[dict[str, Any]]:
    return service.menu_administrators(principal, scope_kind, scope_id)


@router.put("/menu-administrators/{scope_kind}/{scope_id}")
def set_menu_administrators(
    scope_kind: AdministratorScopeKind, scope_id: str, write: AdministratorsWrite,
    service: Service, principal: Principal,
) -> dict[str, Any]:
    return service.set_menu_administrators(principal, scope_kind, scope_id, write)


@router.post("/people/{person_id}/status-preview")
def status_preview(
    person_id: UUID,
    service: Service,
    principal: Principal,
) -> dict[str, Any]:
    return service.status_preview(principal, person_id)


@router.post("/people/{person_id}/status-changes")
def set_status(
    person_id: UUID,
    write: PersonStatusWrite,
    service: Service,
    principal: Principal,
) -> dict[str, Any]:
    return service.set_status(principal, person_id, write)


@router.get("/audit")
def audit(
    service: Service,
    principal: Principal,
    department_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    return service.audit(principal, department_id=department_id, limit=limit)
