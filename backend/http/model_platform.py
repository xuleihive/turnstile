from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from turnstile_core.config import get_settings
from turnstile_core.domain.control_plane import (
    GatewayBackendPoolConfig,
    GatewayBackendPoolWrite,
    GatewayCredentialRotation,
    GatewayPublicationCreate,
    GatewayPublicationList,
    GatewayPublicationRequestAccepted,
    GatewayPublicationRetry,
    GatewayPublicationView,
    GatewayReconcileRequest,
    GatewayReleaseDetail,
    GatewayReleaseDiff,
    GatewayReleaseIntegrity,
    GatewayReleaseList,
    GatewayReleaseOperation,
    GatewayReleaseOperationAccepted,
    GatewayReleaseProtection,
    GatewayReleaseProtectionWrite,
    GatewayReleaseRollbackPreview,
    GatewayReleaseRollbackRequest,
)
from turnstile_core.domain.enterprise import (
    configured_invocation_tester,
    governance_directory,
    merge_application_owners,
)
from turnstile_core.domain.images import ImageInvocationRequest, ImageInvocationResponse
from turnstile_core.domain.runtime_models import (
    DatabricksConnectionAdopt,
    GatewayProfileWrite,
    ManagedModelWrite,
    ModelConnectionCreate,
    ModelConnectionUpdate,
    ModelInvocationRequest,
    ModelInvocationResponse,
    PriceCatalogModelsResponse,
    PriceCatalogOptionsResponse,
    PriceSyncRequest,
    PriceSyncResponse,
    ProviderWrite,
    RegistryResponse,
    RuntimeHealth,
    RuntimeWrite,
    TrafficGenerationPlan,
    TrafficGenerationRequest,
    TrafficGenerationResult,
)
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    ControlPlaneNotFoundError,
    ControlPlaneUnavailableError,
)

from ..services.traffic import TrafficGenerator
from .dependencies import Repository
from .publication_auth import PublicationOwner
from .service_dependencies import (
    Authorization,
    ControlPlaneService,
    RuntimeService,
)
from .session import (
    Config,
    CurrentSession,
    OwnerSession,
    require_allowed_write_origin,
    require_authenticated_session,
)

logger = logging.getLogger(__name__)
InvocationRequest = TypeVar("InvocationRequest", ModelInvocationRequest, ImageInvocationRequest)

class ModelPlatformRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def credential_safe_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as error:
                detail = [
                    {key: issue[key] for key in ("type", "loc", "msg") if key in issue}
                    for issue in error.errors()
                ]
                raise HTTPException(status_code=422, detail=detail) from None

        return credential_safe_handler


protected_router = APIRouter(
    route_class=ModelPlatformRoute,
    dependencies=[
        Depends(require_authenticated_session),
        Depends(require_allowed_write_origin),
    ]
)
publication_router = APIRouter(route_class=ModelPlatformRoute)


@protected_router.get("/api/v1/model-management", response_model=RegistryResponse)
def get_model_registry(
    service: RuntimeService,
    identity: CurrentSession,
    authorization: Authorization = None,
) -> RegistryResponse:
    service.authorize(identity.role, authorization, manage=False)
    return service.registry()


@protected_router.post("/api/v1/model-management/gateways", response_model=RegistryResponse)
def create_gateway_profile(
    write: GatewayProfileWrite,
    service: RuntimeService,
    identity: OwnerSession,
    authorization: Authorization = None,
) -> RegistryResponse:
    service.authorize(identity.role, authorization, manage=True)
    return service.save_gateway(write)


@protected_router.put(
    "/api/v1/model-management/gateways/{item_id}", response_model=RegistryResponse
)
def update_gateway_profile(
    item_id: UUID,
    write: GatewayProfileWrite,
    service: RuntimeService,
    identity: OwnerSession,
    authorization: Authorization = None,
) -> RegistryResponse:
    service.authorize(identity.role, authorization, manage=True)
    return service.save_gateway(write, item_id)


@protected_router.delete(
    "/api/v1/model-management/gateways/{item_id}",
    response_model=RegistryResponse,
)
def delete_gateway_profile(
    item_id: UUID,
    service: RuntimeService,
    identity: OwnerSession,
) -> RegistryResponse:
    del identity
    return service.delete_gateway(item_id)


@protected_router.post("/api/v1/model-management/providers", response_model=RegistryResponse)
def create_provider(
    write: ProviderWrite,
    service: RuntimeService,
    identity: OwnerSession,
    authorization: Authorization = None,
) -> RegistryResponse:
    service.authorize(identity.role, authorization, manage=True)
    return service.save_provider(write)


@protected_router.put(
    "/api/v1/model-management/providers/{item_id}", response_model=RegistryResponse
)
def update_provider(
    item_id: UUID,
    write: ProviderWrite,
    service: RuntimeService,
    identity: OwnerSession,
    authorization: Authorization = None,
) -> RegistryResponse:
    service.authorize(identity.role, authorization, manage=True)
    return service.save_provider(write, item_id)


@protected_router.post("/api/v1/model-management/runtimes", response_model=RegistryResponse)
def create_runtime(
    write: RuntimeWrite,
    service: RuntimeService,
    identity: OwnerSession,
    authorization: Authorization = None,
) -> RegistryResponse:
    service.authorize(identity.role, authorization, manage=True)
    return service.save_runtime(write)


@protected_router.post("/api/v1/model-management/connections", response_model=RegistryResponse)
def create_model_connection(
    write: ModelConnectionCreate,
    service: RuntimeService,
    identity: OwnerSession,
) -> RegistryResponse:
    del identity
    return service.save_connection(write)


@protected_router.put(
    "/api/v1/model-management/connections/{runtime_id}",
    response_model=RegistryResponse,
)
def update_model_connection(
    runtime_id: UUID,
    write: ModelConnectionUpdate,
    service: RuntimeService,
    identity: OwnerSession,
) -> RegistryResponse:
    del identity
    return service.update_connection(runtime_id, write)


@protected_router.post(
    "/api/v1/model-management/connections/{runtime_id}/adopt",
    response_model=GatewayPublicationRequestAccepted,
    status_code=202,
)
def adopt_databricks_connection(
    runtime_id: UUID,
    write: DatabricksConnectionAdopt,
    service: ControlPlaneService,
    identity: OwnerSession,
) -> GatewayPublicationRequestAccepted:
    try:
        publication = service.adopt_databricks_connection(
            runtime_id, str(write.workspace_url), identity.email,
        )
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return GatewayPublicationRequestAccepted(
        publication=GatewayPublicationView.from_publication(publication),
        status_url=f"/api/v1/model-management/publications/{publication.id}",
    )


@protected_router.delete(
    "/api/v1/model-management/connections/{runtime_id}",
    response_model=RegistryResponse,
)
def delete_model_connection(
    runtime_id: UUID,
    service: RuntimeService,
    identity: OwnerSession,
) -> RegistryResponse:
    del identity
    return service.delete_connection(runtime_id)


@protected_router.put(
    "/api/v1/model-management/runtimes/{item_id}", response_model=RegistryResponse
)
def update_runtime(
    item_id: UUID,
    write: RuntimeWrite,
    service: RuntimeService,
    identity: OwnerSession,
    authorization: Authorization = None,
) -> RegistryResponse:
    service.authorize(identity.role, authorization, manage=True)
    return service.save_runtime(write, item_id)


@protected_router.get(
    "/api/v1/model-management/price-catalog/models",
    response_model=PriceCatalogModelsResponse,
)
def search_price_catalog_models(
    service: RuntimeService,
    identity: CurrentSession,
    q: Annotated[str, Query(max_length=120)] = "",
    authorization: Authorization = None,
) -> PriceCatalogModelsResponse:
    # Reading a vendor's published price list changes nothing and reveals nothing the vendor
    # does not print on its own website, so this is gated like the registry read it sits beside
    # rather than like the write it leads to.
    service.authorize(identity.role, authorization, manage=False)
    return service.price_catalog_models(q)


@protected_router.get(
    "/api/v1/model-management/price-catalog/options",
    response_model=PriceCatalogOptionsResponse,
)
def price_catalog_options(
    service: RuntimeService,
    identity: CurrentSession,
    model: Annotated[str, Query(max_length=300)],
    authorization: Authorization = None,
) -> PriceCatalogOptionsResponse:
    service.authorize(identity.role, authorization, manage=False)
    return service.price_catalog_options(model)


@protected_router.post(
    "/api/v1/model-management/price-sync",
    response_model=PriceSyncResponse,
)
def sync_model_prices(
    service: RuntimeService,
    identity: OwnerSession,
    request: PriceSyncRequest | None = None,
    authorization: Authorization = None,
) -> PriceSyncResponse:
    # Gated exactly like editing one model's rate by hand: `update_model` already requires an
    # owner session and skips the management credential for a price-only change, and repricing
    # in bulk is the same act at a larger scale. One gate, stated once.
    service.authorize(identity.role, authorization, manage=False)
    return service.sync_prices(request.model_ids if request else None)


@protected_router.post("/api/v1/model-management/models", response_model=RegistryResponse)
def create_model(
    write: ManagedModelWrite,
    service: RuntimeService,
    identity: OwnerSession,
    authorization: Authorization = None,
) -> RegistryResponse:
    service.authorize(identity.role, authorization, manage=True)
    return service.save_model(write)


@protected_router.put(
    "/api/v1/model-management/models/{item_id}",
    response_model=RegistryResponse,
)
def update_model(
    item_id: UUID,
    write: ManagedModelWrite,
    service: RuntimeService,
    identity: OwnerSession,
    authorization: Authorization = None,
) -> RegistryResponse:
    service.authorize_model_update(item_id, write, identity.role, authorization)
    return service.save_model(write, item_id)


@publication_router.delete(
    "/api/v1/model-management/models/{item_id}",
    response_model=GatewayPublicationRequestAccepted,
    status_code=202,
)
def delete_model(
    item_id: UUID,
    service: ControlPlaneService,
    owner_email: PublicationOwner,
) -> GatewayPublicationRequestAccepted:
    try:
        publication = service.remove_model(item_id, owner_email)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return GatewayPublicationRequestAccepted(
        publication=GatewayPublicationView.from_publication(publication),
        status_url=f"/api/v1/model-management/publications/{publication.id}",
    )


@publication_router.put(
    "/api/v1/model-management/models/{item_id}/backend-pool",
    response_model=GatewayPublicationRequestAccepted,
    status_code=202,
)
def configure_model_backend_pool(
    item_id: UUID,
    write: GatewayBackendPoolWrite,
    service: ControlPlaneService,
    owner_email: PublicationOwner,
) -> GatewayPublicationRequestAccepted:
    try:
        publication = service.configure_model_backend_pool(
            item_id,
            write,
            owner_email,
        )
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return GatewayPublicationRequestAccepted(
        publication=GatewayPublicationView.from_publication(publication),
        status_url=f"/api/v1/model-management/publications/{publication.id}",
    )


@protected_router.get(
    "/api/v1/model-management/models/{item_id}/backend-pool",
    response_model=GatewayBackendPoolConfig,
)
def get_model_backend_pool(
    item_id: UUID,
    service: ControlPlaneService,
    identity: CurrentSession,
) -> GatewayBackendPoolConfig:
    del identity
    try:
        return service.model_backend_pool(item_id)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@publication_router.delete(
    "/api/v1/model-management/models/{item_id}/backend-pool",
    response_model=GatewayPublicationRequestAccepted,
    status_code=202,
)
def remove_model_backend_pool(
    item_id: UUID,
    service: ControlPlaneService,
    owner_email: PublicationOwner,
) -> GatewayPublicationRequestAccepted:
    try:
        publication = service.remove_model_backend_pool(item_id, owner_email)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return GatewayPublicationRequestAccepted(
        publication=GatewayPublicationView.from_publication(publication),
        status_url=f"/api/v1/model-management/publications/{publication.id}",
    )


@publication_router.post(
    "/api/v1/model-management/publications",
    response_model=GatewayPublicationRequestAccepted,
    status_code=202,
)
def create_gateway_publication(
    write: GatewayPublicationCreate,
    service: ControlPlaneService,
    owner_email: PublicationOwner,
) -> GatewayPublicationRequestAccepted:
    try:
        publication = service.publish(write, owner_email)
    except ControlPlaneUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return GatewayPublicationRequestAccepted(
        publication=GatewayPublicationView.from_publication(publication),
        status_url=f"/api/v1/model-management/publications/{publication.id}",
    )


@publication_router.post(
    "/api/v1/model-management/gateways/{gateway_profile_id}/reconcile",
    response_model=GatewayPublicationRequestAccepted,
    status_code=202,
)
def reconcile_gateway_routes(
    gateway_profile_id: UUID,
    service: ControlPlaneService,
    owner_email: PublicationOwner,
    write: GatewayReconcileRequest | None = None,
) -> GatewayPublicationRequestAccepted:
    try:
        publication = service.reconcile_gateway(
            gateway_profile_id, owner_email, write.image_configurations if write else None
        )
    except ControlPlaneUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return GatewayPublicationRequestAccepted(
        publication=GatewayPublicationView.from_publication(publication),
        status_url=f"/api/v1/model-management/publications/{publication.id}",
    )


@publication_router.get(
    "/api/v1/model-management/publications",
    response_model=GatewayPublicationList,
)
def list_gateway_publications(
    service: ControlPlaneService,
    owner_email: PublicationOwner,
    gateway_profile_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> GatewayPublicationList:
    del owner_email
    return GatewayPublicationList(
        items=[
            GatewayPublicationView.from_publication(publication)
            for publication in service.publications(gateway_profile_id, limit)
        ]
    )


@publication_router.get(
    "/api/v1/model-management/publications/{publication_id}",
    response_model=GatewayPublicationView,
)
def get_gateway_publication(
    publication_id: UUID,
    service: ControlPlaneService,
    owner_email: PublicationOwner,
) -> GatewayPublicationView:
    del owner_email
    try:
        return GatewayPublicationView.from_publication(service.publication(publication_id))
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@protected_router.get(
    "/api/v1/model-management/releases",
    response_model=GatewayReleaseList,
)
def list_gateway_releases(
    service: ControlPlaneService,
    identity: CurrentSession,
    gateway_profile_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> GatewayReleaseList:
    del identity
    return service.releases(gateway_profile_id, limit)


@protected_router.get(
    "/api/v1/model-management/releases/{release_id}",
    response_model=GatewayReleaseDetail,
)
def get_gateway_release(
    release_id: UUID,
    service: ControlPlaneService,
    identity: CurrentSession,
) -> GatewayReleaseDetail:
    del identity
    try:
        return service.release(release_id)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@protected_router.get(
    "/api/v1/model-management/releases/{release_id}/diff",
    response_model=GatewayReleaseDiff,
)
def get_gateway_release_diff(
    release_id: UUID,
    service: ControlPlaneService,
    identity: CurrentSession,
    against_release_id: UUID | None = None,
) -> GatewayReleaseDiff:
    del identity
    try:
        return service.release_diff(release_id, against_release_id)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@protected_router.get(
    "/api/v1/model-management/releases/{release_id}/integrity",
    response_model=GatewayReleaseIntegrity,
)
def get_gateway_release_integrity(
    release_id: UUID,
    service: ControlPlaneService,
    identity: CurrentSession,
) -> GatewayReleaseIntegrity:
    del identity
    try:
        return service.release_integrity(release_id)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@protected_router.put(
    "/api/v1/model-management/releases/{release_id}/protection",
    response_model=GatewayReleaseProtection,
)
def protect_gateway_release(
    release_id: UUID,
    write: GatewayReleaseProtectionWrite,
    service: ControlPlaneService,
    identity: OwnerSession,
) -> GatewayReleaseProtection:
    try:
        protection = service.protect_release(release_id, write, identity.email)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if protection is None:
        raise HTTPException(
            status_code=422,
            detail="Use DELETE to remove release protection",
        )
    return protection


@protected_router.delete(
    "/api/v1/model-management/releases/{release_id}/protection",
    status_code=204,
)
def unprotect_gateway_release(
    release_id: UUID,
    service: ControlPlaneService,
    identity: OwnerSession,
) -> None:
    try:
        service.protect_release(
            release_id,
            GatewayReleaseProtectionWrite(pinned=False),
            identity.email,
        )
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@protected_router.post(
    "/api/v1/model-management/releases/{release_id}/integrity-checks",
    response_model=GatewayReleaseOperationAccepted,
    status_code=202,
)
def request_gateway_release_integrity_check(
    release_id: UUID,
    service: ControlPlaneService,
    identity: OwnerSession,
) -> GatewayReleaseOperationAccepted:
    try:
        return service.request_integrity_check(release_id, identity.email)
    except ControlPlaneUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@protected_router.get(
    "/api/v1/model-management/releases/{release_id}/rollback-preview",
    response_model=GatewayReleaseRollbackPreview,
)
def preview_gateway_release_rollback(
    release_id: UUID,
    service: ControlPlaneService,
    identity: CurrentSession,
) -> GatewayReleaseRollbackPreview:
    del identity
    try:
        return service.rollback_preview(release_id)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@protected_router.post(
    "/api/v1/model-management/releases/{release_id}/rollback",
    response_model=GatewayReleaseOperationAccepted,
    status_code=202,
)
def request_gateway_release_rollback(
    release_id: UUID,
    write: GatewayReleaseRollbackRequest,
    service: ControlPlaneService,
    identity: OwnerSession,
) -> GatewayReleaseOperationAccepted:
    try:
        return service.request_rollback(release_id, write, identity.email)
    except ControlPlaneUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@protected_router.get(
    "/api/v1/model-management/release-operations/{operation_id}",
    response_model=GatewayReleaseOperation,
)
def get_gateway_release_operation(
    operation_id: UUID,
    service: ControlPlaneService,
    identity: CurrentSession,
) -> GatewayReleaseOperation:
    del identity
    try:
        return service.release_operation(operation_id)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@protected_router.post(
    "/api/v1/model-management/gateways/{gateway_profile_id}/gc-plans",
    response_model=GatewayReleaseOperationAccepted,
    status_code=202,
)
def request_gateway_release_gc_plan(
    gateway_profile_id: UUID,
    service: ControlPlaneService,
    identity: OwnerSession,
) -> GatewayReleaseOperationAccepted:
    try:
        return service.request_gc_plan(gateway_profile_id, identity.email)
    except ControlPlaneUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@publication_router.delete(
    "/api/v1/model-management/publications/{publication_id}",
    response_model=GatewayPublicationView,
)
def cancel_gateway_publication_authorization(
    publication_id: UUID,
    service: ControlPlaneService,
    owner_email: PublicationOwner,
) -> GatewayPublicationView:
    try:
        publication = service.cancel_authorization(publication_id, owner_email)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return GatewayPublicationView.from_publication(publication)


@publication_router.post(
    "/api/v1/model-management/publications/{publication_id}/retry",
    response_model=GatewayPublicationView,
)
def retry_gateway_publication(
    publication_id: UUID,
    write: GatewayPublicationRetry,
    service: ControlPlaneService,
    owner_email: PublicationOwner,
) -> GatewayPublicationView:
    try:
        publication = service.retry(publication_id, write, owner_email)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return GatewayPublicationView.from_publication(publication)


@publication_router.post(
    "/api/v1/model-management/publications/{publication_id}/authorization/resume",
    response_model=GatewayPublicationView,
)
def resume_gateway_publication_authorization(
    publication_id: UUID,
    service: ControlPlaneService,
    owner_email: PublicationOwner,
) -> GatewayPublicationView:
    try:
        publication = service.resume_authorization(publication_id, owner_email)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return GatewayPublicationView.from_publication(publication)


@publication_router.post(
    "/api/v1/model-management/gateways/{gateway_profile_id}/credentials/rotate",
    response_model=GatewayPublicationView,
    status_code=202,
)
def rotate_gateway_credential(
    gateway_profile_id: UUID,
    write: GatewayCredentialRotation,
    service: ControlPlaneService,
    owner_email: PublicationOwner,
) -> GatewayPublicationView:
    try:
        publication = service.rotate_credential(gateway_profile_id, write, owner_email)
    except ControlPlaneConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return GatewayPublicationView.from_publication(publication)


@protected_router.post(
    "/api/v1/model-management/runtimes/{runtime_id}/check",
    response_model=RuntimeHealth,
)
def check_runtime(
    runtime_id: UUID,
    service: RuntimeService,
    identity: OwnerSession,
    authorization: Authorization = None,
) -> RuntimeHealth:
    service.authorize(identity.role, authorization, manage=True)
    return service.check_runtime(runtime_id)


@protected_router.post("/api/v1/model-gateway/invoke", response_model=ModelInvocationResponse)
def invoke_model(
    request: ModelInvocationRequest,
    service: RuntimeService,
    identity: CurrentSession,
    repository: Repository,
    settings: Config,
) -> ModelInvocationResponse:
    return service.invoke(_bind_invocation_identity(request, identity, repository, settings))


@protected_router.post(
    "/api/v1/model-gateway/images/generations", response_model=ImageInvocationResponse
)
def generate_image(
    request: ImageInvocationRequest,
    service: RuntimeService,
    identity: CurrentSession,
    repository: Repository,
    settings: Config,
    response: Response,
) -> ImageInvocationResponse:
    request = _bind_invocation_identity(request, identity, repository, settings)
    response.headers["Cache-Control"] = "no-store"
    return service.generate_image(request, role=identity.role)


def _bind_invocation_identity(
    request: InvocationRequest,
    identity: CurrentSession,
    repository: Repository,
    settings: Config,
) -> InvocationRequest:
    if identity.role != "owner":
        requested_user_id = request.metadata.user_id.casefold()
        if requested_user_id == identity.email.casefold():
            effective_user_id = identity.email
            effective_user_name = identity.name or identity.email
        else:
            catalog = merge_application_owners(
                governance_directory(
                    repository.observed_users(),
                    include_seeded_people=settings.seed_demo_directory,
                    units=repository.org_units(),
                ),
                repository.application_owners(),
            )
            tester = configured_invocation_tester(
                catalog,
                settings.delegated_invocation_tester_ids,
                requested_user_id,
            )
            if tester is None:
                raise HTTPException(
                    status_code=403,
                    detail="Invocation identity must be the signed-in user or a configured tester",
                )
            if tester.parent_id != request.metadata.department_id:
                raise HTTPException(
                    status_code=422,
                    detail="Invocation tester is not assigned to the selected department",
                )
            logger.info(
                "Delegated tester invocation actor=%s tester=%s",
                identity.email,
                tester.id,
            )
            effective_user_id = tester.id
            effective_user_name = tester.name
        request = request.model_copy(
            update={
                "metadata": request.metadata.model_copy(
                    update={"user_id": effective_user_id, "user": effective_user_name}
                )
            }
        )
    return request


@protected_router.post("/api/v1/traffic/plan", response_model=TrafficGenerationPlan)
def plan_enterprise_traffic(
    request: TrafficGenerationRequest,
    service: RuntimeService,
    identity: OwnerSession,
    authorization: Authorization = None,
) -> TrafficGenerationPlan:
    service.authorize(identity.role, authorization, manage=True)
    plan_request = request.model_copy(update={"dry_run": True})
    return TrafficGenerator(service, get_settings()).plan(plan_request)


@protected_router.post("/api/v1/traffic/execute", response_model=TrafficGenerationResult)
def execute_enterprise_traffic(
    request: TrafficGenerationRequest,
    service: RuntimeService,
    identity: OwnerSession,
    authorization: Authorization = None,
) -> TrafficGenerationResult:
    service.authorize(identity.role, authorization, manage=True)
    return TrafficGenerator(service, get_settings()).execute(request, identity.role)
