from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends

from turnstile_core.domain.menu_permissions import PermissionPolicyWrite
from turnstile_core.services.directory_catalog import directory_store
from turnstile_core.services.permission_service import PermissionService

from .session import Config, OwnerSession, require_allowed_write_origin

router = APIRouter(
    prefix="/api/v1/permission-management",
    tags=["Permission Management"],
    dependencies=[Depends(require_allowed_write_origin)],
)


@router.get("")
def permission_configuration(identity: OwnerSession, settings: Config) -> dict[str, Any]:
    store = directory_store(settings.database_url or "")
    return PermissionService(store).read(
        store.principal(UUID(identity.id), identity.email, identity.role)
    )


@router.put("")
def save_permission_configuration(
    write: PermissionPolicyWrite,
    identity: OwnerSession,
    settings: Config,
) -> dict[str, Any]:
    store = directory_store(settings.database_url or "")
    return PermissionService(store).save(
        store.principal(UUID(identity.id), identity.email, identity.role),
        write,
    )
