from __future__ import annotations

import csv
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import StringIO
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from turnstile_core.config import get_settings
from turnstile_core.domain.access_control import AccessControlError
from turnstile_core.domain.application_access import (
    GatewayApplicationAvatar,
    GatewayApplicationAvatarUpdate,
    GatewayApplicationBudgetUpdate,
    GatewayApplicationDetail,
    GatewayApplicationList,
    GatewayApplicationModelAccessUpdate,
    GatewayApplicationOwnerUpdate,
    GatewayApplicationSubscriptionCancel,
    GatewayApplicationSubscriptionCreate,
    GatewayApplicationSubscriptionKeyRotation,
    GatewayApplicationSubscriptionKeySecret,
    GatewaySubscriptionKeysExportFilter,
    GatewayUserSubscriptionCreate,
    GatewayUserSubscriptionEntry,
    GatewayUserSubscriptionList,
)
from turnstile_core.domain.control_plane import (
    GatewayApplicationSubscriptionProvisionAccepted,
    GatewayReleaseOperationAccepted,
)
from turnstile_core.domain.models import TrendResponse
from turnstile_core.domain.security_audit import AuditEventWrite
from turnstile_core.services.access_control import AccessControlService
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    ControlPlaneNotFoundError,
    ControlPlaneUnavailableError,
)

from .security_audit_support import (
    OptionalAuditService,
    audit_request_context,
    record_audit_best_effort,
)
from .service_dependencies import ApplicationAccessServiceDependency, ControlPlaneService
from .session import (
    OwnerSession,
    SessionIdentity,
    require_allowed_write_origin,
    require_any_permission,
    require_authenticated_session,
)
from .user_management import AccessService

router = APIRouter(
    prefix="/api/v1/application-access",
    tags=["Application Access"],
    dependencies=[
        Depends(require_authenticated_session),
        Depends(require_allowed_write_origin),
    ],
)
admin_key_router = APIRouter(prefix="/api/v1/admin", tags=["Application Access"])

KeyManager = Annotated[
    SessionIdentity,
    Depends(require_any_permission("keys.manage.own", "keys.manage.all")),
]

logger = logging.getLogger(__name__)
AllKeyManager = Annotated[
    SessionIdentity, Depends(require_any_permission("keys.manage.all"))
]
ApplicationReader = Annotated[
    SessionIdentity,
    Depends(require_any_permission("applications.read.all", "applications.read.own")),
]


def _audit_key_action(
    audit_service: OptionalAuditService,
    request: Request,
    identity: SessionIdentity,
    *,
    action: str,
    result: Literal["success", "failure", "denied"],
    resource_type: str,
    resource_id: str | None,
    after: dict[str, object] | None = None,
    correlation_id: str | None = None,
    fail_closed: bool = False,
) -> None:
    context = audit_request_context(request, get_settings())
    event = AuditEventWrite(
        action=action,
        result=result,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_id=identity.id,
        actor_email=identity.email,
        actor_role=identity.role,
        request_id=context.request_id,
        correlation_id=correlation_id or context.correlation_id,
        client_ip=context.client_ip,
        user_agent=context.user_agent,
        after=after or {},
    )
    if not fail_closed:
        record_audit_best_effort(audit_service, event)
        return
    if audit_service is None:
        raise HTTPException(
            status_code=503,
            detail="Key delivery is unavailable while security audit is unavailable.",
        )
    try:
        audit_service.record(event)
    except Exception as error:
        # Exception text may contain SQL parameters; only log the action and class.
        logger.warning(
            "Sensitive key audit persistence failed: action=%s error_type=%s",
            action,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Key delivery is unavailable while security audit is unavailable.",
        ) from None


def _subscription_list(
    service: ApplicationAccessServiceDependency,
    user: dict[str, object],
) -> GatewayUserSubscriptionList:
    owner_user_id = UUID(str(user["id"]))
    rows = service.user_subscription_rows(owner_user_id)
    return GatewayUserSubscriptionList(
        user_id=owner_user_id,
        email=str(user["email"]),
        display_name=(str(user["display_name"]) if user.get("display_name") else None),
        role=str(user["role"]),
        items=[
            GatewayUserSubscriptionEntry(
                id=UUID(str(row["id"])),
                application_id=UUID(str(row["application_id"])),
                application_display_name=str(row["application_display_name"]),
                application_type=cast(
                    Literal["service", "agent", "delegated_user", "system"],
                    row["application_type"],
                ),
                apim_subscription_id=str(row["apim_subscription_id"]),
                display_name=str(row["display_name"]),
                scope_id=str(row["scope_id"]),
                state=cast(Literal["active", "suspended", "cancelled"], row["state"]),
                source=cast(Literal["bicep", "discovered", "managed"], row["source"]),
                created_at=cast(datetime, row["created_at"]),
            )
            for row in rows
        ],
    )


def _directory_user(service: AccessControlService, user_id: UUID) -> dict[str, object]:
    try:
        return service.get_user(user_id)
    except AccessControlError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error


def _application_read_owner(identity: SessionIdentity) -> UUID | None:
    if "applications.read.all" in identity.permissions:
        return None
    if "applications.read.own" in identity.permissions:
        return UUID(identity.id)
    raise HTTPException(status_code=403, detail="Permission denied")


@router.get("/me/subscriptions", response_model=GatewayUserSubscriptionList)
def list_my_gateway_subscriptions(
    identity: KeyManager,
    service: ApplicationAccessServiceDependency,
    directory: AccessService,
) -> GatewayUserSubscriptionList:
    user = _directory_user(directory, UUID(identity.id))
    return _subscription_list(service, user)


@router.post(
    "/me/subscriptions",
    response_model=GatewayApplicationSubscriptionProvisionAccepted,
    status_code=202,
)
def create_my_gateway_subscription(
    request: GatewayUserSubscriptionCreate,
    response: Response,
    http_request: Request,
    identity: KeyManager,
    service: ControlPlaneService,
    directory: AccessService,
    audit_service: OptionalAuditService,
) -> GatewayApplicationSubscriptionProvisionAccepted:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    _audit_key_action(
        audit_service, http_request, identity, action="key.create.requested",
        result="success", resource_type="subscription", resource_id=None,
    )
    try:
        user = _directory_user(directory, UUID(identity.id))
        if not bool(user.get("enabled")):
            raise ControlPlaneConflictError("Disabled users cannot own API keys")
        accepted = service.request_user_application_subscription_provision(
            request, user, identity.email
        )
    except ControlPlaneUnavailableError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="subscription.create",
            result="failure", resource_type="subscription", resource_id=None,
        )
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (ControlPlaneNotFoundError, ControlPlaneConflictError) as error:
        _audit_key_action(
            audit_service, http_request, identity, action="subscription.create",
            result="failure", resource_type="subscription", resource_id=None,
        )
        status_code = 404 if isinstance(error, ControlPlaneNotFoundError) else 409
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    except HTTPException as error:
        _audit_key_action(
            audit_service, http_request, identity, action="subscription.create",
            result="failure", resource_type="subscription", resource_id=None,
        )
        raise error
    spec = accepted.operation.semantic_preview
    _audit_key_action(
        audit_service,
        http_request,
        identity,
        action="subscription.create",
        result="success",
        resource_type="subscription",
        resource_id=str(spec.get("apim_subscription_id") or ""),
        after={
            "subscription_id": str(spec.get("apim_subscription_id") or ""),
            "application_id": str(spec.get("application_id") or ""),
        },
        correlation_id=str(accepted.operation.id),
    )
    return accepted


@admin_key_router.get(
    "/users/{user_id}/subscriptions",
    response_model=GatewayUserSubscriptionList,
)
def list_user_gateway_subscriptions(
    user_id: UUID,
    _identity: AllKeyManager,
    service: ApplicationAccessServiceDependency,
    directory: AccessService,
) -> GatewayUserSubscriptionList:
    user = _directory_user(directory, user_id)
    return _subscription_list(service, user)


@admin_key_router.post(
    "/users/{user_id}/subscriptions",
    response_model=GatewayApplicationSubscriptionProvisionAccepted,
    status_code=202,
)
def create_user_gateway_subscription(
    user_id: UUID,
    request: GatewayUserSubscriptionCreate,
    response: Response,
    http_request: Request,
    identity: AllKeyManager,
    service: ControlPlaneService,
    directory: AccessService,
    audit_service: OptionalAuditService,
) -> GatewayApplicationSubscriptionProvisionAccepted:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    _audit_key_action(
        audit_service, http_request, identity, action="key.create.requested",
        result="success", resource_type="subscription", resource_id=None,
        after={"target_user_id": str(user_id)},
    )
    try:
        user = _directory_user(directory, user_id)
        if not bool(user.get("enabled")):
            raise ControlPlaneConflictError("Disabled users cannot own API keys")
        accepted = service.request_user_application_subscription_provision(
            request, user, identity.email
        )
    except ControlPlaneUnavailableError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="subscription.create",
            result="failure", resource_type="subscription", resource_id=None,
        )
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (ControlPlaneNotFoundError, ControlPlaneConflictError) as error:
        _audit_key_action(
            audit_service, http_request, identity, action="subscription.create",
            result="failure", resource_type="subscription", resource_id=None,
        )
        status_code = 404 if isinstance(error, ControlPlaneNotFoundError) else 409
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    except HTTPException as error:
        _audit_key_action(
            audit_service, http_request, identity, action="subscription.create",
            result="failure", resource_type="subscription", resource_id=None,
        )
        raise error
    spec = accepted.operation.semantic_preview
    _audit_key_action(
        audit_service,
        http_request,
        identity,
        action="subscription.create",
        result="success",
        resource_type="subscription",
        resource_id=str(spec.get("apim_subscription_id") or ""),
        after={
            "subscription_id": str(spec.get("apim_subscription_id") or ""),
            "application_id": str(spec.get("application_id") or ""),
            "id": str(user_id),
            "email": str(user["email"]),
            "role": str(user["role"]),
        },
        correlation_id=str(accepted.operation.id),
    )
    return accepted


@admin_key_router.post("/subscriptions/keys/export")
def export_gateway_subscription_keys(
    filters: GatewaySubscriptionKeysExportFilter,
    http_request: Request,
    identity: AllKeyManager,
    service: ApplicationAccessServiceDependency,
    directory: AccessService,
    audit_service: OptionalAuditService,
) -> StreamingResponse:
    _audit_key_action(
        audit_service,
        http_request,
        identity,
        action="key.export.requested",
        result="success",
        resource_type="subscription_export",
        resource_id=None,
        after={
            "user_count": len(filters.user_ids),
            "department_filtered": filters.department_id is not None,
            "subscription_count": len(filters.subscription_ids),
        },
        fail_closed=True,
    )
    try:
        candidates = service.subscription_export_rows()
        selected_user_ids = {str(user_id) for user_id in filters.user_ids}
        selected_subscription_ids = set(filters.subscription_ids)
        candidates = [
            row
            for row in candidates
            if (not selected_user_ids or row["user_id"] in selected_user_ids)
            and (
                filters.department_id is None
                or row["department_id"] == str(filters.department_id)
            )
            and (
                not selected_subscription_ids
                or row["subscription_id"] in selected_subscription_ids
            )
            and row["subscription_state"] != "cancelled"
        ]
        candidates.sort(key=lambda row: (str(row["user_id"]), str(row["subscription_id"])))
        if len(candidates) > 1000:
            raise HTTPException(
                status_code=422,
                detail="Key export is limited to 1000 APIM subscriptions; add filters",
            )

        rows: list[dict[str, object]] = []
        users: dict[str, dict[str, object]] = {}
        for candidate in candidates:
            user_id = str(candidate["user_id"])
            if user_id not in users:
                users[user_id] = _directory_user(directory, UUID(user_id))
            user = users[user_id]
            if not bool(user.get("enabled")):
                continue
            if (
                filters.department_id is not None
                and str(user.get("organization_unit_id") or "")
                != str(filters.department_id)
            ):
                continue
            rows.append({**candidate, "email": str(user["email"]), "role": str(user["role"])})

        if len(rows) > 1000:
            raise HTTPException(
                status_code=422,
                detail="Key export is limited to 1000 APIM subscriptions; add filters",
            )
        secrets: dict[str, tuple[str, str]] = {}
        if rows:
            with ThreadPoolExecutor(max_workers=min(8, len(rows))) as executor:
                futures = {
                    executor.submit(
                        service.subscription_keys_for_export, str(row["subscription_id"])
                    ): str(row["subscription_id"])
                    for row in rows
                }
                for future, subscription_id in futures.items():
                    secrets[subscription_id] = future.result()
    except HTTPException as error:
        _audit_key_action(
            audit_service,
            http_request,
            identity,
            action="key.export",
            result="failure",
            resource_type="subscription_export",
            resource_id=None,
        )
        raise error
    except ControlPlaneUnavailableError as error:
        _audit_key_action(
            audit_service,
            http_request,
            identity,
            action="key.export",
            result="failure",
            resource_type="subscription_export",
            resource_id=None,
        )
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneNotFoundError as error:
        _audit_key_action(
            audit_service,
            http_request,
            identity,
            action="key.export",
            result="failure",
            resource_type="subscription_export",
            resource_id=None,
        )
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        _audit_key_action(
            audit_service,
            http_request,
            identity,
            action="key.export",
            result="failure",
            resource_type="subscription_export",
            resource_id=None,
        )
        raise HTTPException(status_code=503, detail="APIM key export failed") from error

    output = StringIO(newline="")
    columns = (
        "user_id",
        "email",
        "role",
        "department_id",
        "application_id",
        "application_name",
        "application_type",
        "subscription_id",
        "subscription_state",
        "primary_key",
        "secondary_key",
        "created_at",
    )
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        primary_key, secondary_key = secrets[str(row["subscription_id"])]
        writer.writerow(
            {
                column: _export_csv_value(
                    primary_key if column == "primary_key" else
                    secondary_key if column == "secondary_key" else row.get(column)
                )
                for column in columns
            }
        )
    _audit_key_action(
        audit_service,
        http_request,
        identity,
        action="key.export",
        result="success",
        resource_type="subscription_export",
        resource_id=None,
        after={"subscription_count": len(rows)},
        fail_closed=True,
    )
    return StreamingResponse(
        iter(["\ufeff" + output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Content-Disposition": 'attachment; filename="turnstile-apim-keys.csv"',
        },
    )


def _export_csv_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    text = "" if value is None else str(value)
    if text.lstrip(" \t\r\n")[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


@router.put(
    "/applications/{application_id}/owner",
    response_model=GatewayApplicationDetail,
)
def update_gateway_application_owner(
    application_id: UUID,
    request: GatewayApplicationOwnerUpdate,
    http_request: Request,
    service: ApplicationAccessServiceDependency,
    directory: AccessService,
    identity: OwnerSession,
    audit_service: OptionalAuditService,
) -> GatewayApplicationDetail:
    user = _directory_user(directory, request.user_id) if request.user_id else None
    if user is not None and not bool(user.get("enabled")):
        raise HTTPException(status_code=409, detail="Disabled users cannot own applications")
    try:
        service.claim_application_owner(
            application_id,
            request.user_id,
            UUID(str(user["tenant_id"])) if user else None,
            UUID(str(user["organization_unit_id"]))
            if user and user.get("organization_unit_id")
            else None,
            str(user["email"]) if user else None,
            identity.email,
        )
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ControlPlaneConflictError, ControlPlaneUnavailableError) as error:
        raise HTTPException(
            status_code=409 if isinstance(error, ControlPlaneConflictError) else 503,
            detail=str(error),
        ) from error
    _audit_key_action(
        audit_service,
        http_request,
        identity,
        action="application.owner.change",
        result="success",
        resource_type="application",
        resource_id=str(application_id),
        after={
            "application_id": str(application_id),
            "id": str(request.user_id or ""),
            "email": str(user["email"]) if user else "",
        },
    )
    return service.application(application_id)


@router.get("/applications", response_model=GatewayApplicationList)
def list_gateway_applications(
    service: ApplicationAccessServiceDependency,
    identity: ApplicationReader,
) -> GatewayApplicationList:
    return service.applications(owner_user_id=_application_read_owner(identity))


@router.get("/applications/{application_id}", response_model=GatewayApplicationDetail)
def get_gateway_application(
    application_id: UUID,
    service: ApplicationAccessServiceDependency,
    identity: ApplicationReader,
    http_request: Request,
    audit_service: OptionalAuditService,
) -> GatewayApplicationDetail:
    owner_user_id = _application_read_owner(identity)
    try:
        return service.application(application_id, owner_user_id=owner_user_id)
    except ControlPlaneNotFoundError as error:
        if owner_user_id is not None:
            _audit_key_action(
                audit_service,
                http_request,
                identity,
                action="authorization.denied",
                result="denied",
                resource_type="application",
                resource_id=str(application_id),
                after={"permission": "applications.read.own", "view": "detail"},
            )
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get(
    "/applications/{application_id}/usage-activity",
    response_model=TrendResponse,
)
def get_gateway_application_usage_activity(
    application_id: UUID,
    service: ApplicationAccessServiceDependency,
    identity: ApplicationReader,
    http_request: Request,
    audit_service: OptionalAuditService,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    interval: Literal["day", "week"],
    timezone: str = "UTC",
) -> TrendResponse:
    owner_user_id = _application_read_owner(identity)
    if to <= from_:
        raise HTTPException(status_code=422, detail="to must be after from")
    try:
        return service.application_usage_activity(
            application_id,
            from_,
            to,
            interval,
            timezone,
            owner_user_id=owner_user_id,
        )
    except ControlPlaneNotFoundError as error:
        if owner_user_id is not None:
            _audit_key_action(
                audit_service,
                http_request,
                identity,
                action="authorization.denied",
                result="denied",
                resource_type="application",
                resource_id=str(application_id),
                after={"permission": "applications.read.own", "view": "usage_activity"},
            )
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _disable_secret_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _managed_apim_subscription_id(
    service: ApplicationAccessServiceDependency,
    application_id: UUID,
    application_subscription_id: UUID,
    identity: SessionIdentity,
    request: Request,
    audit_service: OptionalAuditService,
    *,
    action: str,
    key_kind: str | None = None,
    allow_cancelled: bool = False,
) -> str:
    try:
        return service.apim_subscription_id_for_key_management(
            application_id,
            application_subscription_id,
            actor_user_id=UUID(identity.id),
            can_manage_all="keys.manage.all" in identity.permissions,
            allow_cancelled=allow_cancelled,
        )
    except ControlPlaneNotFoundError as error:
        _audit_key_action(
            audit_service,
            request,
            identity,
            action=action,
            result="denied",
            resource_type="subscription",
            resource_id=str(application_subscription_id),
            after={"key_kind": key_kind} if key_kind else {},
        )
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        _audit_key_action(
            audit_service,
            request,
            identity,
            action=action,
            result="failure",
            resource_type="subscription",
            resource_id=str(application_subscription_id),
            after={"key_kind": key_kind} if key_kind else {},
        )
        raise HTTPException(status_code=409, detail=str(error)) from error


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
    http_request: Request,
    service: ApplicationAccessServiceDependency,
    identity: KeyManager,
    audit_service: OptionalAuditService,
) -> GatewayApplicationSubscriptionKeySecret:
    _disable_secret_caching(response)
    apim_subscription_id = _managed_apim_subscription_id(
        service,
        application_id,
        application_subscription_id,
        identity,
        http_request,
        audit_service,
        action="key.reveal",
        key_kind=key_kind,
    )
    _audit_key_action(
        audit_service, http_request, identity, action="key.reveal.requested",
        result="success", resource_type="subscription",
        resource_id=apim_subscription_id, after={"key_kind": key_kind},
        fail_closed=True,
    )
    try:
        secret = service.reveal_application_subscription_key(
            application_id,
            application_subscription_id,
            key_kind,
            actor_user_id=UUID(identity.id),
            can_manage_all="keys.manage.all" in identity.permissions,
        )
    except ControlPlaneUnavailableError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="key.reveal",
            result="failure", resource_type="subscription",
            resource_id=apim_subscription_id, after={"key_kind": key_kind},
        )
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneNotFoundError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="key.reveal",
            result="denied", resource_type="subscription",
            resource_id=apim_subscription_id, after={"key_kind": key_kind},
        )
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="key.reveal",
            result="failure", resource_type="subscription",
            resource_id=apim_subscription_id, after={"key_kind": key_kind},
        )
        raise HTTPException(status_code=409, detail=str(error)) from error
    _audit_key_action(
        audit_service, http_request, identity, action="key.reveal",
        result="success", resource_type="subscription",
        resource_id=apim_subscription_id, after={"key_kind": key_kind},
        fail_closed=True,
    )
    return secret


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
    http_request: Request,
    service: ApplicationAccessServiceDependency,
    identity: KeyManager,
    audit_service: OptionalAuditService,
) -> None:
    _disable_secret_caching(response)
    apim_subscription_id = _managed_apim_subscription_id(
        service,
        application_id,
        application_subscription_id,
        identity,
        http_request,
        audit_service,
        action="key.rotate",
        key_kind=key_kind,
    )
    _audit_key_action(
        audit_service, http_request, identity, action="key.rotate.requested",
        result="success", resource_type="subscription",
        resource_id=apim_subscription_id, after={"key_kind": key_kind},
    )
    try:
        service.rotate_application_subscription_key(
            application_id,
            application_subscription_id,
            key_kind,
            request,
            actor_user_id=UUID(identity.id),
            can_manage_all="keys.manage.all" in identity.permissions,
        )
    except ControlPlaneUnavailableError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="key.rotate",
            result="failure", resource_type="subscription",
            resource_id=apim_subscription_id, after={"key_kind": key_kind},
        )
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneNotFoundError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="key.rotate",
            result="denied", resource_type="subscription",
            resource_id=apim_subscription_id, after={"key_kind": key_kind},
        )
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="key.rotate",
            result="failure", resource_type="subscription",
            resource_id=apim_subscription_id, after={"key_kind": key_kind},
        )
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="key.rotate",
            result="failure", resource_type="subscription",
            resource_id=apim_subscription_id, after={"key_kind": key_kind},
        )
        raise HTTPException(status_code=422, detail=str(error)) from error
    _audit_key_action(
        audit_service, http_request, identity, action="key.rotate",
        result="success", resource_type="subscription",
        resource_id=apim_subscription_id, after={"key_kind": key_kind},
    )


@router.post(
    "/applications/{application_id}/subscriptions/"
    "{application_subscription_id}/cancel",
    status_code=204,
)
def cancel_gateway_application_subscription(
    application_id: UUID,
    application_subscription_id: UUID,
    request: GatewayApplicationSubscriptionCancel,
    response: Response,
    http_request: Request,
    service: ApplicationAccessServiceDependency,
    identity: KeyManager,
    audit_service: OptionalAuditService,
) -> None:
    _disable_secret_caching(response)
    apim_subscription_id = _managed_apim_subscription_id(
        service,
        application_id,
        application_subscription_id,
        identity,
        http_request,
        audit_service,
        action="key.cancel",
        allow_cancelled=True,
    )
    _audit_key_action(
        audit_service,
        http_request,
        identity,
        action="key.cancel.requested",
        result="success",
        resource_type="subscription",
        resource_id=apim_subscription_id,
        after={"application_id": str(application_id)},
    )
    try:
        service.cancel_application_subscription(
            application_id,
            application_subscription_id,
            request,
            identity.email,
            actor_user_id=UUID(identity.id),
            can_manage_all="keys.manage.all" in identity.permissions,
        )
    except ControlPlaneUnavailableError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="key.cancel",
            result="failure", resource_type="subscription",
            resource_id=apim_subscription_id,
        )
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneNotFoundError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="key.cancel",
            result="failure", resource_type="subscription",
            resource_id=apim_subscription_id,
        )
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="key.cancel",
            result="failure", resource_type="subscription",
            resource_id=apim_subscription_id,
        )
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        _audit_key_action(
            audit_service, http_request, identity, action="key.cancel",
            result="failure", resource_type="subscription",
            resource_id=apim_subscription_id,
        )
        raise HTTPException(status_code=422, detail=str(error)) from error
    _audit_key_action(
        audit_service,
        http_request,
        identity,
        action="key.cancel",
        result="success",
        resource_type="subscription",
        resource_id=apim_subscription_id,
        after={"application_id": str(application_id)},
    )


@router.get("/applications/{application_id}/avatar", response_class=Response)
def get_gateway_application_avatar(
    application_id: UUID,
    service: ApplicationAccessServiceDependency,
    identity: ApplicationReader,
    http_request: Request,
    audit_service: OptionalAuditService,
) -> Response:
    owner_user_id = _application_read_owner(identity)
    try:
        media_type, image_bytes = service.application_avatar(
            application_id, owner_user_id=owner_user_id
        )
    except ControlPlaneNotFoundError as error:
        if owner_user_id is not None:
            _audit_key_action(
                audit_service,
                http_request,
                identity,
                action="authorization.denied",
                result="denied",
                resource_type="application",
                resource_id=str(application_id),
                after={"permission": "applications.read.own", "view": "avatar"},
            )
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
