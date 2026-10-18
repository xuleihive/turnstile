from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.assistant_models import (
    AssistantAskRequest,
    AssistantReply,
    Conversation,
    ConversationList,
    ConversationRename,
    ConversationSummary,
    ConversationTitleRequest,
)
from turnstile_core.security import CredentialCipher

from ...http.dependencies import Repository
from ...http.session import (
    Config,
    CurrentSession,
    OwnerSession,
    require_allowed_write_origin,
    require_authenticated_session,
    require_unscoped_read,
)
from ...services.runtime_service import ModelRuntimeService
from .assistant import CopilotAssistantService
from .client import GitHubCopilotApiError
from .contracts import (
    CopilotBudgetRequest,
    CopilotBudgetRequestCreate,
    CopilotBudgetRequestList,
    CopilotBudgetRequestReview,
    CopilotConnectionSummary,
    CopilotConnectionWrite,
    CopilotCostCenterOption,
    CopilotCostCenterRequest,
    CopilotCostCenterRequestCreate,
    CopilotCostCenterRequestList,
    CopilotCostCenterRequestReview,
    CopilotDashboard,
    CopilotGovernance,
    CopilotIdentityMapping,
    CopilotIdentityWrite,
    CopilotImportedUsage,
    CopilotOAuthConfigSummary,
    CopilotOAuthConfigWrite,
    CopilotStatus,
    CopilotUsageImportKind,
    CopilotUsageImportSummary,
)
from .csv_import import MAX_CSV_BYTES, CopilotCsvError, parse_copilot_csv
from .service import (
    CopilotIdentityRequiredError,
    CopilotNotConfiguredError,
    CopilotNotFoundError,
    CopilotPermissionError,
    CopilotService,
)
from .store import CopilotStore, CopilotStoreConflictError

logger = logging.getLogger(__name__)

router = APIRouter()
protected = APIRouter(
    dependencies=[
        Depends(require_authenticated_session),
        Depends(require_unscoped_read),
        Depends(require_allowed_write_origin),
    ]
)


@lru_cache
def get_copilot_store() -> CopilotStore:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for GitHub Copilot billing")
    return CopilotStore(settings.database_url)


CopilotStoreDependency = Annotated[CopilotStore, Depends(get_copilot_store)]


def copilot_service(store: CopilotStoreDependency) -> CopilotService:
    return CopilotService(store, CredentialCipher.from_settings(get_settings()))


CopilotServiceDependency = Annotated[CopilotService, Depends(copilot_service)]


def copilot_assistant_service(
    repository: Repository,
    copilot: CopilotServiceDependency,
) -> CopilotAssistantService:
    settings = get_settings()
    return CopilotAssistantService(
        repository,
        ModelRuntimeService(repository, settings),
        copilot,
    )


CopilotAssistantDependency = Annotated[
    CopilotAssistantService,
    Depends(copilot_assistant_service),
]


def request_origin(request: Request) -> str:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0]
    host = (
        request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    ).split(",")[0]
    if proto.strip() not in {"http", "https"} or any(character in host for character in "/\\?#@"):
        raise HTTPException(status_code=400, detail="Invalid request origin")
    return f"{proto.strip()}://{host.strip()}"


def oauth_return_origin(
    candidate: str | None,
    callback_origin: str,
    settings: Settings,
) -> str:
    value = (candidate or callback_origin).rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=400, detail="Invalid OAuth return origin")
    allowed = {callback_origin, *(origin.rstrip("/") for origin in settings.cors_origins)}
    if value not in allowed:
        raise HTTPException(status_code=400, detail="OAuth return origin is not allowed")
    return value


def copilot_http_error(error: Exception) -> HTTPException:
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, CopilotNotConfiguredError):
        return HTTPException(status_code=503, detail=str(error))
    if isinstance(error, CopilotIdentityRequiredError | CopilotStoreConflictError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, CopilotNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, CopilotPermissionError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, GitHubCopilotApiError):
        status = 503 if error.status_code == 503 else 502
        return HTTPException(status_code=status, detail=str(error))
    return HTTPException(status_code=500, detail="GitHub Copilot request failed")


@protected.get("/api/v1/copilot/status", response_model=CopilotStatus)
def get_copilot_status(
    request: Request,
    service: CopilotServiceDependency,
    identity: CurrentSession,
) -> CopilotStatus:
    return service.status(UUID(identity.id), identity.role, request_origin(request))


@protected.get(
    "/api/v1/copilot/oauth/config",
    response_model=CopilotOAuthConfigSummary,
)
def get_copilot_oauth_config(
    request: Request,
    service: CopilotServiceDependency,
    identity: OwnerSession,
) -> CopilotOAuthConfigSummary:
    del identity
    return service.oauth_config(request_origin(request))


@protected.put(
    "/api/v1/copilot/oauth/config",
    response_model=CopilotOAuthConfigSummary,
)
def save_copilot_oauth_config(
    request: Request,
    write: CopilotOAuthConfigWrite,
    service: CopilotServiceDependency,
    identity: OwnerSession,
) -> CopilotOAuthConfigSummary:
    try:
        return service.save_oauth_config(write, request_origin(request), identity.email)
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.get("/api/v1/copilot/oauth/login")
def start_copilot_oauth(
    request: Request,
    service: CopilotServiceDependency,
    identity: CurrentSession,
    settings: Config,
    purpose: Literal["identity", "organization"] = "identity",
    organization: Annotated[str | None, Query(max_length=39)] = None,
    return_origin: Annotated[str | None, Query(max_length=1000)] = None,
    return_path: Annotated[str, Query(max_length=1000)] = (
        "/?page=finops-overview&source=github-copilot"
    ),
) -> RedirectResponse:
    try:
        callback_origin = request_origin(request)
        normalized_organization = organization.strip().lower() if organization else None
        if purpose == "organization" and identity.role != "owner":
            raise CopilotPermissionError("Owner role is required to connect organization data")
        if purpose == "organization" and not normalized_organization:
            raise ValueError("A GitHub organization is required")
        url = service.oauth_authorize_url(
            UUID(identity.id),
            callback_origin,
            oauth_return_origin(return_origin, callback_origin, settings),
            return_path,
            purpose,
            normalized_organization,
        )
        return RedirectResponse(url=url, status_code=302)
    except Exception as error:
        raise copilot_http_error(error) from error


@router.get("/api/v1/copilot/oauth/callback")
async def complete_copilot_oauth(
    request: Request,
    service: CopilotServiceDependency,
    code: str = "",
    state: str = "",
    error: str = "",
) -> RedirectResponse:
    if error or not code or not state:
        return RedirectResponse(
            url="/?page=finops-overview&source=github-copilot&github_error=oauth_cancelled",
            status_code=302,
        )
    try:
        return_path = await service.complete_oauth_link(
            origin=request_origin(request),
            state=state,
            code=code,
        )
        return RedirectResponse(url=return_path, status_code=302)
    except CopilotPermissionError as permission_error:
        logger.warning("GitHub OAuth callback rejected: %s", permission_error)
        return RedirectResponse(
            url=("/?page=finops-overview&source=github-copilot&github_error=permission_denied"),
            status_code=302,
        )
    except GitHubCopilotApiError as api_error:
        logger.warning(
            "GitHub OAuth callback API failure: status=%s message=%s",
            api_error.status_code,
            api_error,
        )
        return RedirectResponse(
            url=(
                "/?page=finops-overview&source=github-copilot"
                f"&github_error=github_api_{api_error.status_code}"
            ),
            status_code=302,
        )
    except Exception:
        logger.exception("GitHub OAuth callback failed")
        return RedirectResponse(
            url="/?page=finops-overview&source=github-copilot&github_error=link_failed",
            status_code=302,
        )


@protected.put(
    "/api/v1/copilot/connections",
    response_model=CopilotConnectionSummary,
)
async def save_copilot_connection(
    write: CopilotConnectionWrite,
    service: CopilotServiceDependency,
    identity: OwnerSession,
) -> CopilotConnectionSummary:
    try:
        return await service.save_connection(write, identity.email)
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.get("/api/v1/copilot/dashboard", response_model=CopilotDashboard)
async def get_copilot_dashboard(
    service: CopilotServiceDependency,
    identity: CurrentSession,
    organization: Annotated[str | None, Query(max_length=39)] = None,
) -> CopilotDashboard:
    try:
        return await service.dashboard(
            user_id=UUID(identity.id),
            role=identity.role,
            organization=organization,
        )
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.get("/api/v1/copilot/governance", response_model=CopilotGovernance)
async def get_copilot_governance(
    service: CopilotServiceDependency,
    identity: OwnerSession,
    organization: Annotated[str | None, Query(max_length=39)] = None,
) -> CopilotGovernance:
    try:
        return await service.governance(role=identity.role, organization=organization)
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.post(
    "/api/v1/copilot/usage-imports",
    response_model=CopilotUsageImportSummary,
    status_code=201,
)
async def import_copilot_usage_csv(
    request: Request,
    service: CopilotServiceDependency,
    identity: OwnerSession,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    organization: Annotated[str | None, Query(max_length=39)] = None,
) -> CopilotUsageImportSummary:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"text/csv", "application/csv", "application/vnd.ms-excel"}:
        raise HTTPException(status_code=415, detail="A CSV content type is required")
    declared = request.headers.get("content-length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from error
        if declared_size > MAX_CSV_BYTES:
            raise HTTPException(status_code=413, detail="CSV file exceeds the 5 MB limit")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_CSV_BYTES:
            raise HTTPException(status_code=413, detail="CSV file exceeds the 5 MB limit")
        content.extend(chunk)
    try:
        parsed = parse_copilot_csv(bytes(content))
        return service.save_usage_import(
            parsed,
            filename=filename,
            file_size_bytes=len(content),
            user_id=UUID(identity.id),
            user_email=identity.email,
            organization=organization,
        )
    except CopilotCsvError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.get(
    "/api/v1/copilot/usage-imports",
    response_model=list[CopilotUsageImportSummary],
)
def list_copilot_usage_imports(
    service: CopilotServiceDependency,
    identity: OwnerSession,
    source_kind: CopilotUsageImportKind,
    organization: Annotated[str | None, Query(max_length=39)] = None,
) -> list[CopilotUsageImportSummary]:
    del identity
    return service.usage_imports(source_kind, organization)


@protected.get(
    "/api/v1/copilot/imported-usage",
    response_model=CopilotImportedUsage,
)
def get_copilot_imported_usage(
    service: CopilotServiceDependency,
    identity: OwnerSession,
    source_kind: CopilotUsageImportKind,
    organization: Annotated[str | None, Query(max_length=39)] = None,
) -> CopilotImportedUsage:
    del identity
    return service.imported_usage(source_kind, organization)


@protected.get(
    "/api/v1/copilot/cost-centers/options",
    response_model=list[CopilotCostCenterOption],
)
async def get_copilot_cost_center_options(
    service: CopilotServiceDependency,
    identity: CurrentSession,
    organization: Annotated[str | None, Query(max_length=39)] = None,
) -> list[CopilotCostCenterOption]:
    try:
        return await service.cost_center_options(
            role=identity.role,
            organization=organization,
        )
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.get(
    "/api/v1/copilot/cost-center-requests",
    response_model=CopilotCostCenterRequestList,
)
def list_copilot_cost_center_requests(
    service: CopilotServiceDependency,
    identity: CurrentSession,
) -> CopilotCostCenterRequestList:
    return service.cost_center_requests(UUID(identity.id), identity.role)


@protected.post(
    "/api/v1/copilot/cost-center-requests",
    response_model=CopilotCostCenterRequest,
    status_code=201,
)
async def create_copilot_cost_center_request(
    write: CopilotCostCenterRequestCreate,
    service: CopilotServiceDependency,
    identity: CurrentSession,
) -> CopilotCostCenterRequest:
    try:
        return await service.create_cost_center_request(
            write,
            user_id=UUID(identity.id),
            user_email=identity.email,
            user_display_name=identity.name,
            role=identity.role,
        )
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.post(
    "/api/v1/copilot/cost-center-requests/{request_id}/review",
    response_model=CopilotCostCenterRequest,
)
async def review_copilot_cost_center_request(
    request_id: UUID,
    write: CopilotCostCenterRequestReview,
    service: CopilotServiceDependency,
    identity: OwnerSession,
) -> CopilotCostCenterRequest:
    try:
        return await service.review_cost_center_request(
            request_id,
            write,
            actor=identity.email,
        )
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.get(
    "/api/v1/copilot/identities",
    response_model=list[CopilotIdentityMapping],
)
def list_copilot_identities(
    service: CopilotServiceDependency,
    identity: OwnerSession,
) -> list[CopilotIdentityMapping]:
    del identity
    return service.identities()


@protected.put(
    "/api/v1/copilot/identities/{app_user_id}",
    response_model=CopilotIdentityMapping,
)
def save_copilot_identity(
    app_user_id: UUID,
    write: CopilotIdentityWrite,
    service: CopilotServiceDependency,
    identity: OwnerSession,
) -> CopilotIdentityMapping:
    try:
        return service.save_identity(app_user_id, write.github_login, identity.email)
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.get(
    "/api/v1/copilot/budget-requests",
    response_model=CopilotBudgetRequestList,
)
def list_copilot_budget_requests(
    service: CopilotServiceDependency,
    identity: CurrentSession,
) -> CopilotBudgetRequestList:
    return service.budget_requests(UUID(identity.id), identity.role)


@protected.post(
    "/api/v1/copilot/budget-requests",
    response_model=CopilotBudgetRequest,
    status_code=201,
)
def create_copilot_budget_request(
    write: CopilotBudgetRequestCreate,
    service: CopilotServiceDependency,
    identity: CurrentSession,
) -> CopilotBudgetRequest:
    try:
        return service.create_budget_request(
            write,
            user_id=UUID(identity.id),
            user_email=identity.email,
            user_display_name=identity.name,
            role=identity.role,
        )
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.post(
    "/api/v1/copilot/budget-requests/{request_id}/review",
    response_model=CopilotBudgetRequest,
)
async def review_copilot_budget_request(
    request_id: UUID,
    write: CopilotBudgetRequestReview,
    service: CopilotServiceDependency,
    identity: OwnerSession,
) -> CopilotBudgetRequest:
    try:
        return await service.review_budget_request(
            request_id,
            write,
            actor=identity.email,
        )
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.post("/api/v1/copilot/assistant/ask", response_model=AssistantReply)
async def ask_copilot_assistant(
    request: AssistantAskRequest,
    service: CopilotAssistantDependency,
    identity: CurrentSession,
) -> AssistantReply:
    try:
        return await service.ask(
            request,
            user_uuid=UUID(identity.id),
            user_email=identity.email,
            user_name=identity.name or identity.email,
            role=identity.role,
        )
    except Exception as error:
        raise copilot_http_error(error) from error


@protected.get(
    "/api/v1/copilot/assistant/conversations",
    response_model=ConversationList,
)
def list_copilot_conversations(
    service: CopilotAssistantDependency,
    identity: CurrentSession,
) -> ConversationList:
    return ConversationList(items=service.list_conversations(identity.email))


@protected.get(
    "/api/v1/copilot/assistant/conversations/{conversation_id}",
    response_model=Conversation,
)
def get_copilot_conversation(
    conversation_id: UUID,
    service: CopilotAssistantDependency,
    identity: CurrentSession,
) -> Conversation:
    return service.get_conversation(conversation_id, identity.email)


@protected.patch(
    "/api/v1/copilot/assistant/conversations/{conversation_id}",
    response_model=Conversation,
)
def rename_copilot_conversation(
    conversation_id: UUID,
    rename: ConversationRename,
    service: CopilotAssistantDependency,
    identity: CurrentSession,
) -> Conversation:
    return service.rename_conversation(conversation_id, identity.email, rename.title)


@protected.post(
    "/api/v1/copilot/assistant/conversations/{conversation_id}/title",
    response_model=ConversationSummary,
)
def title_copilot_conversation(
    conversation_id: UUID,
    request: ConversationTitleRequest,
    service: CopilotAssistantDependency,
    identity: CurrentSession,
) -> ConversationSummary:
    return service.title_conversation(conversation_id, identity.email, request.locale)


@protected.delete(
    "/api/v1/copilot/assistant/conversations/{conversation_id}",
    status_code=204,
)
def delete_copilot_conversation(
    conversation_id: UUID,
    service: CopilotAssistantDependency,
    identity: CurrentSession,
) -> None:
    service.delete_conversation(conversation_id, identity.email)


router.include_router(protected)
