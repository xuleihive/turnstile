from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from turnstile_core.domain.directory import (
    ConnectionWrite,
    ExternalBindingWrite,
    GroupMappingsWrite,
    SyncApplyWrite,
    SyncJobWrite,
)
from turnstile_core.integrations.entra_directory import GraphDirectoryClient
from turnstile_core.services.directory_connections import DirectoryConnectionService

from .organization_management import IdempotencyKey, Principal, Service
from .session import Config, require_allowed_write_origin

router = APIRouter(
    prefix="/api/v1/organization-management",
    tags=["Directory synchronization"],
    dependencies=[Depends(require_allowed_write_origin)],
)


def connection_service(service: Service, settings: Config) -> DirectoryConnectionService:
    def graph_factory(connection: dict[str, Any]) -> GraphDirectoryClient:
        return GraphDirectoryClient(
            connection,
            managed_identity_tenant_id=settings.directory_managed_identity_tenant_id,
            deployment_cloud=settings.directory_deployment_cloud,
        )

    return DirectoryConnectionService(
        service,
        graph_factory,
        sync_enabled=settings.directory_sync_enabled,
        deployment_cloud=settings.directory_deployment_cloud,
    )


Connections = Annotated[DirectoryConnectionService, Depends(connection_service)]


@router.get("/connections")
def connections(service: Connections, principal: Principal) -> list[dict[str, Any]]:
    return service.connections(principal)


@router.post("/connections", status_code=201)
def create_connection(
    write: ConnectionWrite,
    service: Connections,
    principal: Principal,
    key: IdempotencyKey = None,
) -> dict[str, Any]:
    return service.create(principal, write, key)


@router.patch("/connections/{connection_id}")
def update_connection(
    connection_id: UUID,
    write: ConnectionWrite,
    service: Connections,
    principal: Principal,
) -> dict[str, Any]:
    return service.update(principal, connection_id, write)


@router.post("/connections/{connection_id}/validate")
def validate_connection(
    connection_id: UUID,
    service: Connections,
    principal: Principal,
) -> dict[str, Any]:
    return service.validate(principal, connection_id)


@router.get("/connections/{connection_id}/group-mappings")
def group_mappings(
    connection_id: UUID,
    service: Connections,
    principal: Principal,
) -> list[dict[str, Any]]:
    return service.mappings(principal, connection_id)


@router.put("/connections/{connection_id}/group-mappings")
def set_group_mappings(
    connection_id: UUID,
    write: GroupMappingsWrite,
    service: Connections,
    principal: Principal,
) -> dict[str, Any]:
    return service.set_mappings(principal, connection_id, write)


@router.post("/connections/{connection_id}/sync-jobs", status_code=202)
def create_sync_job(
    connection_id: UUID,
    write: SyncJobWrite,
    service: Connections,
    principal: Principal,
    key: IdempotencyKey = None,
) -> dict[str, Any]:
    if key is None:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    return service.create_job(principal, connection_id, write, key)


@router.get("/sync-jobs")
def sync_jobs(
    service: Connections,
    principal: Principal,
    connection_id: UUID | None = None,
) -> list[dict[str, Any]]:
    return service.jobs(principal, connection_id)


@router.post("/connections/{connection_id}/users/{object_id}/binding")
def bind_external_person(
    connection_id: UUID,
    object_id: UUID,
    write: ExternalBindingWrite,
    service: Connections,
    principal: Principal,
) -> dict[str, Any]:
    return service.bind_person(principal, connection_id, object_id, write)


@router.get("/sync-jobs/{job_id}")
def sync_job(job_id: UUID, service: Connections, principal: Principal) -> dict[str, Any]:
    return service.job(principal, job_id)


@router.get("/sync-jobs/{job_id}/changes")
def sync_changes(
    job_id: UUID,
    service: Connections,
    principal: Principal,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return service.changes(principal, job_id, offset=offset, limit=limit)


@router.post("/sync-jobs/{job_id}/apply", status_code=202)
def apply_sync_job(
    job_id: UUID,
    write: SyncApplyWrite,
    service: Connections,
    principal: Principal,
) -> dict[str, Any]:
    return service.apply_job(principal, job_id, write)


@router.post("/sync-jobs/{job_id}/cancel")
def cancel_sync_job(
    job_id: UUID,
    service: Connections,
    principal: Principal,
) -> dict[str, Any]:
    return service.cancel(principal, job_id)
