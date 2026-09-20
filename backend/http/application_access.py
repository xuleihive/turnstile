from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from turnstile_core.domain.application_access import (
    GatewayApplicationAvatar,
    GatewayApplicationAvatarUpdate,
    GatewayApplicationBudgetUpdate,
    GatewayApplicationBulkDepartment,
    GatewayApplicationBulkDepartmentResult,
    GatewayApplicationDepartmentUpdate,
    GatewayApplicationDetail,
    GatewayApplicationList,
    GatewayApplicationModelAccessUpdate,
    GatewayApplicationOwnerUpdate,
    GatewayApplicationSubscriptionCreate,
    GatewayApplicationSubscriptionKeyRotation,
    GatewayApplicationSubscriptionKeySecret,
)
from turnstile_core.domain.control_plane import (
    GatewayApplicationSubscriptionProvisionAccepted,
    GatewayReleaseOperationAccepted,
)
from turnstile_core.domain.models import TrendResponse
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    ControlPlaneNotFoundError,
    ControlPlaneUnavailableError,
)

from .service_dependencies import ApplicationAccessServiceDependency, ControlPlaneService
from .session import (
    CurrentSession,
    OwnerSession,
    require_allowed_write_origin,
    require_authenticated_session,
)

router = APIRouter(
    prefix="/api/v1/application-access",
    tags=["Application Access"],
    dependencies=[
        Depends(require_authenticated_session),
        Depends(require_allowed_write_origin),
    ],
)


@router.get("/applications", response_model=GatewayApplicationList)
def list_gateway_applications(
    service: ApplicationAccessServiceDependency,
    identity: CurrentSession,
) -> GatewayApplicationList:
    del identity
    return service.applications()


@router.get("/applications/{application_id}", response_model=GatewayApplicationDetail)
def get_gateway_application(
    application_id: UUID,
    service: ApplicationAccessServiceDependency,
    identity: CurrentSession,
) -> GatewayApplicationDetail:
    del identity
    try:
        return service.application(application_id)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get(
    "/applications/{application_id}/usage-activity",
    response_model=TrendResponse,
)
def get_gateway_application_usage_activity(
    application_id: UUID,
    service: ApplicationAccessServiceDependency,
    identity: CurrentSession,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    interval: Literal["day", "week"],
    timezone: str = "UTC",
) -> TrendResponse:
    del identity
    if to <= from_:
        raise HTTPException(status_code=422, detail="to must be after from")
    try:
        return service.application_usage_activity(
            application_id,
            from_,
            to,
            interval,
            timezone,
        )
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _disable_secret_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post(
    "/applications/{application_id}/subscriptions/"
    "{application_subscription_id}/keys/{key_kind}/reveal",
    response_model=GatewayApplicationSubscriptionKeySecret,
)
def reveal_gateway_application_subscription_key(
    application_id: UUID,
    application_subscription_id: UUID,
    key_kind: Literal["primary", "secondary"],
    response: Response,
    service: ApplicationAccessServiceDependency,
    identity: OwnerSession,
) -> GatewayApplicationSubscriptionKeySecret:
    del identity
    _disable_secret_caching(response)
    try:
        return service.reveal_application_subscription_key(
            application_id, application_subscription_id, key_kind
        )
    except ControlPlaneUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/applications/{application_id}/subscriptions/"
    "{application_subscription_id}/keys/{key_kind}/rotate",
    status_code=204,
)
def rotate_gateway_application_subscription_key(
    application_id: UUID,
    application_subscription_id: UUID,
    key_kind: Literal["primary", "secondary"],
    request: GatewayApplicationSubscriptionKeyRotation,
    response: Response,
    service: ApplicationAccessServiceDependency,
    identity: OwnerSession,
) -> None:
    del identity
    _disable_secret_caching(response)
    try:
        service.rotate_application_subscription_key(
            application_id,
            application_subscription_id,
            key_kind,
            request,
        )
    except ControlPlaneUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/applications/{application_id}/avatar", response_class=Response)
def get_gateway_application_avatar(
    application_id: UUID,
    service: ApplicationAccessServiceDependency,
    identity: CurrentSession,
) -> Response:
    del identity
    try:
        media_type, image_bytes = service.application_avatar(application_id)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        content=image_bytes,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put(
    "/applications/{application_id}/avatar",
    response_model=GatewayApplicationAvatar,
)
def update_gateway_application_avatar(
    application_id: UUID,
    request: GatewayApplicationAvatarUpdate,
    service: ApplicationAccessServiceDependency,
    identity: OwnerSession,
) -> GatewayApplicationAvatar:
    try:
        return service.update_application_avatar(application_id, request, identity.email)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put(
    "/applications/{application_id}/budget",
    response_model=GatewayApplicationDetail,
)
def update_gateway_application_budget(
    application_id: UUID,
    request: GatewayApplicationBudgetUpdate,
    service: ApplicationAccessServiceDependency,
    identity: OwnerSession,
) -> GatewayApplicationDetail:
    try:
        return service.update_application_budget(application_id, request, identity.email)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put(
    "/applications/{application_id}/model-access",
    response_model=GatewayApplicationDetail,
)
def update_gateway_application_model_access(
    application_id: UUID,
    request: GatewayApplicationModelAccessUpdate,
    service: ApplicationAccessServiceDependency,
    identity: OwnerSession,
) -> GatewayApplicationDetail:
    try:
        return service.update_application_model_access(
            application_id, request, identity.email
        )
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put(
    "/applications/{application_id}/department",
    response_model=GatewayApplicationDetail,
)
def update_gateway_application_department(
    application_id: UUID,
    request: GatewayApplicationDepartmentUpdate,
    service: ApplicationAccessServiceDependency,
    identity: OwnerSession,
) -> GatewayApplicationDetail:
    try:
        return service.update_application_department(
            application_id, request, identity.email
        )
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put(
    "/applications/{application_id}/owner",
    response_model=GatewayApplicationDetail,
)
def update_gateway_application_owner(
    application_id: UUID,
    request: GatewayApplicationOwnerUpdate,
    service: ApplicationAccessServiceDependency,
    identity: OwnerSession,
) -> GatewayApplicationDetail:
    try:
        return service.update_application_owner(application_id, request, identity.email)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put(
    "/applications/bulk-department",
    response_model=GatewayApplicationBulkDepartmentResult,
)
def update_gateway_application_department_bulk(
    request: GatewayApplicationBulkDepartment,
    service: ApplicationAccessServiceDependency,
    identity: OwnerSession,
) -> GatewayApplicationBulkDepartmentResult:
    try:
        return service.update_application_department_bulk(request, identity.email)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/gateways/{gateway_profile_id}/sync",
    response_model=GatewayReleaseOperationAccepted,
    status_code=202,
)
def sync_gateway_applications(
    gateway_profile_id: UUID,
    service: ControlPlaneService,
    identity: OwnerSession,
) -> GatewayReleaseOperationAccepted:
    try:
        return service.request_application_sync(gateway_profile_id, identity.email)
    except ControlPlaneUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/gateways/{gateway_profile_id}/subscriptions",
    response_model=GatewayApplicationSubscriptionProvisionAccepted,
    status_code=202,
)
def provision_gateway_application_subscription(
    gateway_profile_id: UUID,
    request: GatewayApplicationSubscriptionCreate,
    response: Response,
    service: ControlPlaneService,
    identity: OwnerSession,
) -> GatewayApplicationSubscriptionProvisionAccepted:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        return service.request_application_subscription_provision(
            gateway_profile_id, request, identity.email
        )
    except ControlPlaneUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error