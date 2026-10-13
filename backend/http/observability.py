from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from backend.services.usage_export import build_usage_report_archive, usage_report_filename
from turnstile_core.domain.access_control import AccessControlError
from turnstile_core.domain.enterprise import (
    configured_invocation_testers,
    enterprise_catalog,
    merge_application_owners,
    merge_observed_users,
)
from turnstile_core.domain.models import (
    AuditFinding,
    AuditFindingListResponse,
    AuditFindingUpdate,
    DistributionResponse,
    EnterpriseEntity,
    EnterpriseEntityCatalog,
    ExecutiveOverviewResponse,
    OptimizationEvent,
    OptimizationEventCreate,
    OptimizationEventListResponse,
    OverviewResponse,
    PageInfo,
    RunDetail,
    RunListResponse,
    RunSummary,
    TrendResponse,
    UsageAnomaly,
    UsageAnomalyListResponse,
    UsageRequestDetail,
    UsageRequestListResponse,
    UsageRequestSummary,
)
from turnstile_core.domain.security_audit import AuditEventWrite
from turnstile_core.domain.usage_export import (
    UsageExportJobCreate,
    UsageExportJobError,
    UsageExportJobList,
    UsageExportJobResponse,
)
from turnstile_core.integrations.usage_export_blob import PrivateUsageExportBlob
from turnstile_core.persistence.export_jobs import ExportJobStore
from turnstile_core.persistence.repository import UsageFilters
from turnstile_core.services.access_control import scope_usage_filters
from turnstile_core.services.security_audit import SecurityAuditService
from turnstile_core.services.usage_export_jobs import UsageExportJobService

from .dependencies import Repository
from .security_audit_support import (
    OptionalAuditService,
    audit_request_context,
    record_audit_best_effort,
)
from .session import (
    Config,
    CurrentSession,
    OwnerSession,
    require_allowed_write_origin,
    require_authenticated_session,
)

router = APIRouter(
    dependencies=[
        Depends(require_authenticated_session),
        Depends(require_allowed_write_origin),
    ]
)

logger = logging.getLogger(__name__)
LEGACY_EXPORT_MAX_RANGE_DAYS = 31
LEGACY_EXPORT_MAX_ROWS = 10_000


def usage_filters(
    organization_id: str | None = None,
    department_id: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    model_id: str | None = None,
    user_id: str | None = None,
    runtime: Annotated[list[str] | None, Query()] = None,
    status_code: Annotated[int | None, Query(ge=100, le=599)] = None,
) -> UsageFilters:
    return UsageFilters(
        organization_id=organization_id,
        department_id=department_id,
        project_id=project_id,
        agent_id=agent_id,
        model_id=model_id,
        user_id=user_id,
        runtime=tuple(runtime) if runtime else None,
        status_code=status_code,
    )


UsageFilterSet = Annotated[UsageFilters, Depends(usage_filters)]


@lru_cache(maxsize=1)
def _export_job_store() -> ExportJobStore:
    from turnstile_core.config import get_settings

    settings = get_settings()
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Usage export jobs require PostgreSQL")
    return ExportJobStore(settings.database_url)


def get_usage_export_job_store() -> ExportJobStore:
    return _export_job_store()


ExportJobStoreDependency = Annotated[
    ExportJobStore, Depends(get_usage_export_job_store)
]


@lru_cache(maxsize=1)
def get_usage_export_blob() -> PrivateUsageExportBlob:
    from turnstile_core.config import get_settings

    endpoint = get_settings().usage_export_blob_service_url
    if not endpoint:
        raise HTTPException(
            status_code=503, detail="Usage export storage is temporarily unavailable"
        )
    try:
        return PrivateUsageExportBlob(endpoint)
    except Exception as error:
        logger.warning(
            "Usage export storage client unavailable: error_type=%s",
            type(error).__name__,
        )
        raise HTTPException(
            status_code=503, detail="Usage export storage is temporarily unavailable"
        ) from None


UsageExportBlobDependency = Annotated[
    PrivateUsageExportBlob, Depends(get_usage_export_blob)
]


def get_usage_export_job_service(
    store: ExportJobStoreDependency,
    settings: Config,
) -> UsageExportJobService:
    return UsageExportJobService(
        store,
        max_range_days=settings.usage_export_max_range_days,
        max_active_per_user=settings.usage_export_max_active_per_user,
        retention_days=settings.usage_export_retention_days,
    )


ExportJobServiceDependency = Annotated[
    UsageExportJobService, Depends(get_usage_export_job_service)
]


def _raise_export_job_error(error: UsageExportJobError) -> NoReturn:
    raise HTTPException(status_code=error.status_code, detail=error.detail) from error


def _audit_export_job_created(
    service: SecurityAuditService | None,
    request: Request,
    identity: CurrentSession,
    settings: Config,
    job: UsageExportJobResponse,
) -> None:
    context = audit_request_context(request, settings)
    record_audit_best_effort(
        service,
        AuditEventWrite(
            action="usage_export.created",
            result="success",
            resource_type="usage_export_job",
            resource_id=str(job.id),
            actor_id=identity.id,
            actor_email=identity.email,
            actor_role=identity.role,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            client_ip=context.client_ip,
            user_agent=context.user_agent,
            after={
                "job_id": str(job.id),
                "status": job.status,
                "row_count": job.row_count,
            },
        ),
    )


def _scope_filters(identity: CurrentSession, filters: UsageFilters) -> UsageFilters:
    try:
        return scope_usage_filters(identity, filters)
    except AccessControlError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error


@router.get("/api/v1/enterprise/entities", response_model=EnterpriseEntityCatalog)
def get_enterprise_entities(
    repository: Repository, settings: Config, identity: CurrentSession
) -> EnterpriseEntityCatalog:
    if not identity.permissions.intersection({"platform.manage", "users.read"}):
        if not identity.permissions.intersection({"usage.read.own", "usage.read.all"}):
            raise HTTPException(status_code=403, detail="Permission denied")
        catalog = enterprise_catalog()
        return catalog.model_copy(
            update={
                "organizations": [],
                "departments": [],
                "projects": [],
                "agents": [],
                "users": [
                    EnterpriseEntity(
                        id=identity.id,
                        name=identity.name or identity.email,
                    )
                ],
                "invocation_testers": [],
            }
        )

    catalog = merge_application_owners(
        merge_observed_users(enterprise_catalog(), repository.observed_users()),
        repository.application_owners(),
    )
    return catalog.model_copy(
        update={
            "invocation_testers": configured_invocation_testers(
                catalog, settings.delegated_invocation_tester_ids
            )
        }
    )


@router.get(
    "/api/v1/observability/executive-overview",
    response_model=ExecutiveOverviewResponse,
)
def get_executive_overview(
    repository: Repository,
    filters: UsageFilterSet,
    identity: CurrentSession,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
) -> ExecutiveOverviewResponse:
    if to <= from_:
        raise HTTPException(status_code=422, detail="to must be after from")
    return ExecutiveOverviewResponse.model_validate(
        repository.executive_overview(from_, to, _scope_filters(identity, filters))
    )


@router.get("/api/v1/observability/distribution", response_model=DistributionResponse)
def get_distribution(
    repository: Repository,
    filters: UsageFilterSet,
    identity: CurrentSession,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    dimension: Literal[
        "organization", "department", "project", "agent", "model", "user", "runtime"
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    split_by: Literal[
        "organization", "department", "project", "agent", "model", "user", "runtime"
    ]
    | None = None,
) -> DistributionResponse:
    return DistributionResponse.model_validate(
        {
            "from": from_,
            "to": to,
            "dimension": dimension,
            "items": repository.distribution(
                from_, to, dimension, _scope_filters(identity, filters), limit, split_by
            ),
        }
    )


@router.get("/api/v1/observability/requests", response_model=UsageRequestListResponse)
def list_usage_requests(
    repository: Repository,
    filters: UsageFilterSet,
    identity: CurrentSession,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> UsageRequestListResponse:
    return UsageRequestListResponse(
        items=[
            UsageRequestSummary.model_validate(row)
            for row in repository.list_usage_requests(
                from_, to, _scope_filters(identity, filters), limit
            )
        ],
        page=PageInfo(next_cursor=None),
    )


@router.get("/api/v1/observability/requests/export", response_class=StreamingResponse)
def export_usage_requests(
    repository: Repository,
    filters: UsageFilterSet,
    identity: CurrentSession,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    request: Request,
    settings: Config,
    audit_service: OptionalAuditService,
) -> StreamingResponse:
    if to <= from_:
        raise HTTPException(status_code=422, detail="to must be after from")

    if (to - from_).total_seconds() > LEGACY_EXPORT_MAX_RANGE_DAYS * 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Synchronous exports are limited to 31 days; submit an export job",
        )

    scoped_filters = _scope_filters(identity, filters)
    overview = repository.executive_overview(from_, to, scoped_filters)
    if int(overview["totals"]["total_requests"]) > LEGACY_EXPORT_MAX_ROWS:
        raise HTTPException(
            status_code=413,
            detail="Synchronous exports are limited to 10,000 rows; submit an export job",
        )

    archive = build_usage_report_archive(
        repository, from_, to, scoped_filters
    )
    audit_context = audit_request_context(request, settings)
    record_audit_best_effort(
        audit_service,
        AuditEventWrite(
            action="usage_export.legacy_exported",
            result="success",
            resource_type="usage_export",
            actor_id=identity.id,
            actor_email=identity.email,
            actor_role=identity.role,
            request_id=audit_context.request_id,
            correlation_id=audit_context.correlation_id,
            client_ip=audit_context.client_ip,
            user_agent=audit_context.user_agent,
            after={
                "from": from_.isoformat(),
                "to": to.isoformat(),
                "row_count": int(overview["totals"]["total_requests"]),
            },
        ),
    )

    def contents() -> Iterator[bytes]:
        try:
            while chunk := archive.read(64 * 1024):
                yield chunk
        finally:
            archive.close()

    return StreamingResponse(
        contents(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{usage_report_filename(from_, to)}"',
            "Cache-Control": "private, no-store",
        },
    )


def _audit_usage_export_download(
    service: SecurityAuditService | None,
    request: Request,
    identity: CurrentSession,
    settings: Config,
    job_id: UUID,
    *,
    result: Literal["success", "failure"],
    row_count: int | None = None,
    reason_code: str | None = None,
) -> None:
    context = audit_request_context(request, settings)
    after: dict[str, object] = {"export_job_id": str(job_id)}
    if row_count is not None:
        after["row_count"] = row_count
    if reason_code is not None:
        after["reason_code"] = reason_code
    record_audit_best_effort(
        service,
        AuditEventWrite(
            action="usage_export.downloaded",
            result=result,
            resource_type="usage_export_job",
            resource_id=str(job_id),
            actor_id=identity.id,
            actor_email=identity.email,
            actor_role=identity.role,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            client_ip=context.client_ip,
            user_agent=context.user_agent,
            after=after,
        ),
    )


@router.get(
    "/api/v1/observability/export-jobs/{job_id}/download",
    response_class=StreamingResponse,
)
def download_usage_export_job(
    job_id: UUID,
    identity: CurrentSession,
    service: ExportJobServiceDependency,
    blob: UsageExportBlobDependency,
    request: Request,
    settings: Config,
    audit_service: OptionalAuditService,
) -> StreamingResponse:
    try:
        blob_name, row_count = service.get_download_target(identity, job_id)
    except UsageExportJobError as error:
        _audit_usage_export_download(
            audit_service,
            request,
            identity,
            settings,
            job_id,
            result="failure",
            reason_code=("expired" if error.status_code == 410 else "job_unavailable"),
        )
        _raise_export_job_error(error)

    chunks = iter(blob.download_chunks(blob_name))
    try:
        first_chunk = next(chunks)
    except Exception as error:
        logger.warning(
            "Usage export download failed: job=%s error_type=%s",
            job_id,
            type(error).__name__,
        )
        _audit_usage_export_download(
            audit_service,
            request,
            identity,
            settings,
            job_id,
            result="failure",
            reason_code="blob_unavailable",
        )
        raise HTTPException(
            status_code=503, detail="Usage export download is temporarily unavailable"
        ) from None

    def contents() -> Iterator[bytes]:
        completed = False
        try:
            yield first_chunk
            yield from chunks
            completed = True
        except Exception as error:
            logger.warning(
                "Usage export stream interrupted: job=%s error_type=%s",
                job_id,
                type(error).__name__,
            )
            return
        finally:
            _audit_usage_export_download(
                audit_service,
                request,
                identity,
                settings,
                job_id,
                result="success" if completed else "failure",
                row_count=row_count,
                reason_code=None if completed else "blob_unavailable",
            )

    return StreamingResponse(
        contents(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="turnstile_usage_report_{job_id}.zip"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post(
    "/api/v1/observability/export-jobs",
    response_model=UsageExportJobResponse,
    status_code=202,
)
def create_usage_export_job(
    body: UsageExportJobCreate,
    identity: CurrentSession,
    service: ExportJobServiceDependency,
    request: Request,
    response: Response,
    settings: Config,
    audit_service: OptionalAuditService,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
    ],
) -> UsageExportJobResponse:
    try:
        job = service.create_job(
            identity,
            from_=body.from_,
            to=body.to,
            filters=body.filters.to_usage_filters(),
            idempotency_key=idempotency_key,
        )
    except UsageExportJobError as error:
        _raise_export_job_error(error)
    response.headers["Location"] = job.status_url
    response.headers["Cache-Control"] = "private, no-store"
    _audit_export_job_created(audit_service, request, identity, settings, job)
    return job


@router.get(
    "/api/v1/observability/export-jobs", response_model=UsageExportJobList
)
def list_usage_export_jobs(
    identity: CurrentSession,
    service: ExportJobServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> UsageExportJobList:
    try:
        return UsageExportJobList(items=service.list_jobs(identity, limit=limit))
    except UsageExportJobError as error:
        _raise_export_job_error(error)


@router.get(
    "/api/v1/observability/export-jobs/{job_id}",
    response_model=UsageExportJobResponse,
)
def get_usage_export_job(
    job_id: UUID,
    identity: CurrentSession,
    service: ExportJobServiceDependency,
) -> UsageExportJobResponse:
    try:
        return service.get_job(identity, job_id)
    except UsageExportJobError as error:
        _raise_export_job_error(error)


@router.get("/api/v1/observability/requests/{request_id}", response_model=UsageRequestDetail)
def get_usage_request(
    request_id: str, repository: Repository, identity: CurrentSession
) -> UsageRequestDetail:
    row = repository.get_usage_request(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Usage request not found")
    if not identity.permissions.intersection({"platform.manage", "usage.read.all"}):
        if "usage.read.own" not in identity.permissions:
            raise HTTPException(status_code=403, detail="Permission denied")
        if str(row.get("user_id") or "").strip().lower() not in {
            identity.id.strip().lower(),
            identity.email.strip().lower(),
        }:
            raise HTTPException(status_code=404, detail="Usage request not found")
    return UsageRequestDetail.model_validate(row)


@router.get("/api/v1/observability/anomalies", response_model=UsageAnomalyListResponse)
def list_usage_anomalies(
    repository: Repository,
    filters: UsageFilterSet,
    identity: CurrentSession,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> UsageAnomalyListResponse:
    return UsageAnomalyListResponse(
        items=[
            UsageAnomaly.model_validate(row)
            for row in repository.list_usage_anomalies(
                from_, to, _scope_filters(identity, filters), limit
            )
        ]
    )


@router.get("/api/v1/observability/overview", response_model=OverviewResponse)
def get_overview(
    repository: Repository, identity: OwnerSession, timezone: str = "UTC"
) -> dict[str, object]:
    return repository.overview(timezone)


@router.get("/api/v1/observability/trends", response_model=TrendResponse)
def get_trends(
    repository: Repository,
    filters: UsageFilterSet,
    identity: CurrentSession,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    interval: Literal["hour", "day", "week"],
    group_by: Literal[
        "organization",
        "department",
        "project",
        "agent",
        "user",
        "model",
        "runtime",
        "workflow",
        "team",
        "none",
    ],
    timezone: str = "UTC",
) -> dict[str, object]:
    return repository.trends(
        from_, to, interval, group_by, timezone, _scope_filters(identity, filters)
    )


@router.get("/api/v1/observability/runs", response_model=RunListResponse)
def list_runs(
    repository: Repository,
    identity: OwnerSession,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RunListResponse:
    rows = repository.list_runs(from_, to, limit)
    summaries = [{key: value for key, value in row.items() if key != "turns"} for row in rows]
    return RunListResponse(
        items=[RunSummary.model_validate(row) for row in summaries],
        page=PageInfo(next_cursor=None),
    )


@router.get("/api/v1/observability/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, repository: Repository, identity: OwnerSession) -> RunDetail:
    row = repository.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunDetail.model_validate(row)


@router.get("/api/v1/observability/audit-findings", response_model=AuditFindingListResponse)
def list_findings(
    repository: Repository,
    identity: OwnerSession,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditFindingListResponse:
    rows = repository.list_findings(from_, to, limit)
    return AuditFindingListResponse(
        items=[AuditFinding.model_validate(row) for row in rows], page=PageInfo(next_cursor=None)
    )


@router.patch("/api/v1/observability/audit-findings/{finding_id}", response_model=AuditFinding)
def update_finding(
    finding_id: UUID,
    update: AuditFindingUpdate,
    repository: Repository,
    identity: OwnerSession,
) -> AuditFinding:
    row = repository.update_finding(finding_id, update)
    if row is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return AuditFinding.model_validate(row)


@router.get(
    "/api/v1/observability/optimization-events",
    response_model=OptimizationEventListResponse,
)
def list_optimizations(
    repository: Repository,
    identity: OwnerSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> OptimizationEventListResponse:
    rows = repository.list_optimizations(limit)
    return OptimizationEventListResponse(
        items=[OptimizationEvent.model_validate(row) for row in rows],
        page=PageInfo(next_cursor=None),
    )


@router.post(
    "/api/v1/observability/optimization-events", response_model=OptimizationEvent, status_code=201
)
def create_optimization(
    create: OptimizationEventCreate,
    repository: Repository,
    identity: OwnerSession,
) -> OptimizationEvent:
    return OptimizationEvent.model_validate(repository.create_optimization(create))
