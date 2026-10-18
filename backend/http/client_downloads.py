from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response

from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    ControlPlaneNotFoundError,
    ControlPlaneUnavailableError,
)

from ..services.client_download_permissions import require_plugin_download
from ..services.client_downloads import (
    ClientDownloadOptions,
    ClientDownloadRequest,
    ClientDownloadService,
)
from ..services.download_filename import attachment_header
from .dependencies import Repository
from .service_dependencies import ApplicationAccessServiceDependency
from .session import Config, CurrentSession, require_allowed_write_origin

router = APIRouter(
    prefix="/api/v1/client-downloads", tags=["Client Downloads"],
    dependencies=[Depends(require_allowed_write_origin)],
)
HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


def client_download_service(
    repository: Repository, applications: ApplicationAccessServiceDependency, settings: Config,
) -> ClientDownloadService:
    return ClientDownloadService(repository, applications, settings)


Service = Annotated[ClientDownloadService, Depends(client_download_service)]


@router.get("/options", response_model=ClientDownloadOptions)
def options(
    identity: CurrentSession, service: Service, response: Response,
) -> ClientDownloadOptions:
    require_plugin_download(identity, "subscription-plugin")
    response.headers.update(HEADERS)
    return service.options(identity)


@router.post("/magpie", response_class=Response, responses={
    200: {"description": "Private magpie plugin ZIP including subscription credentials",
          "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}},
})
def download(identity: CurrentSession, service: Service, body: ClientDownloadRequest) -> Response:
    require_plugin_download(identity, "subscription-plugin")
    try:
        archive = service.download(identity, body)
    except ControlPlaneNotFoundError as error:
        raise HTTPException(404, "订阅已不存在。", headers=HEADERS) from error
    except ControlPlaneConflictError as error:
        raise HTTPException(409, "订阅当前无法导出。", headers=HEADERS) from error
    except ControlPlaneUnavailableError as error:
        raise HTTPException(503, "暂时无法读取订阅密钥，请稍后重试。", headers=HEADERS) from error
    return Response(archive.content, media_type="application/zip", headers={
        **HEADERS, "Content-Disposition": attachment_header(archive.filename),
        "Access-Control-Expose-Headers": "Content-Disposition",
    })
