from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from turnstile_core.config import get_settings
from turnstile_core.security import CredentialCipher

from .data_sources.github_copilot.router import router as github_copilot_router
from .http.application_access import router as application_access_router
from .http.assistant import router as assistant_router
from .http.assistant import title_router as assistant_title_router
from .http.authentication import get_entra_verifier as _get_entra_verifier
from .http.authentication import router as authentication_router
from .http.budgets import router as budgets_router
from .http.dependencies import get_repository
from .http.governance import router as governance_router
from .http.model_platform import (
    protected_router as model_platform_protected_router,
)
from .http.model_platform import (
    publication_router as model_platform_publication_router,
)
from .http.observability import router as observability_router
from .http.service_dependencies import (
    application_access_service as _application_access_service,
)
from .http.service_dependencies import (
    assistant_service as _assistant_service,
)
from .http.service_dependencies import (
    control_plane_service as _control_plane_service,
)
from .http.service_dependencies import (
    runtime_service as _runtime_service,
)
from .http.session import require_allowed_write_origin, require_authenticated_session
from .http.static_files import validate_production_web_dist

logger = logging.getLogger(__name__)

assistant_service = _assistant_service
application_access_service = _application_access_service
control_plane_service = _control_plane_service
get_entra_verifier = _get_entra_verifier
runtime_service = _runtime_service


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    # Fail fast on misconfiguration. Without this, a deployment missing
    # CREDENTIAL_ENCRYPTION_KEY starts healthy and only breaks on the first
    # request that touches the model registry, surfacing as an opaque 500.
    settings = get_settings()
    validate_production_web_dist(settings)
    get_repository()
    if settings.production:
        CredentialCipher.from_settings(settings)
    yield


app = FastAPI(title="Token Observability API", version="0.1.0", lifespan=lifespan)
protected = APIRouter(
    dependencies=[
        Depends(require_authenticated_session),
        Depends(require_allowed_write_origin),
    ]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


protected.routes.extend(assistant_router.routes)
protected.routes.extend(governance_router.routes)
protected.routes.extend(budgets_router.routes)
protected.routes.extend(model_platform_protected_router.routes)
protected.routes.extend(observability_router.routes)
protected.routes.extend(application_access_router.routes)
app.include_router(protected)
app.include_router(assistant_title_router)
app.include_router(model_platform_publication_router)
app.include_router(github_copilot_router)
app.include_router(authentication_router)


# Registered after every real API route and before the single-page-application
# fallback below, so a typo in an /api path always answers JSON. This must not be
# folded into the fallback: that one only exists once the frontend has been
# built, which would make the guarantee depend on a build artifact being present.
@app.get("/api/{unknown_path:path}", include_in_schema=False)
def unknown_api_route(unknown_path: str) -> None:
    raise HTTPException(status_code=404, detail="API route not found")


web_dist = get_settings().web_dist_dir
if web_dist.is_dir():
    assets_dir = web_dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="web-assets")

    @app.get("/{client_path:path}", include_in_schema=False)
    def web_application(client_path: str) -> FileResponse:
        candidate = (web_dist / client_path).resolve()
        if candidate.is_file() and web_dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(web_dist / "index.html")
