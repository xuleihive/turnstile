from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

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
from turnstile_core.integrations.router_table_projection import (
    sync_apim_router_traces_if_configured,
)
from turnstile_core.persistence.repository import UsageFilters

from .dependencies import Repository
from .session import (
    Config,
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


def usage_filters(
    organization_id: str | None = None,
    department_id: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    model_id: str | None = None,
    user_id: str | None = None,
    runtime: Annotated[list[str] | None, Query()] = None,
    status_code: Annotated[int | None, Query(ge=100, le=599)] = None,
    apim_target_id: UUID | None = None,
    gateway_deployment_id: UUID | None = None,
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
        apim_target_id=apim_target_id,
        gateway_deployment_id=gateway_deployment_id,
    )


UsageFilterSet = Annotated[UsageFilters, Depends(usage_filters)]


@router.get("/api/v1/enterprise/entities", response_model=EnterpriseEntityCatalog)
def get_enterprise_entities(
    repository: Repository, settings: Config
) -> EnterpriseEntityCatalog:
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
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    compare_from: datetime | None = None,
    compare_to: datetime | None = None,
) -> ExecutiveOverviewResponse:
    if to <= from_:
        raise HTTPException(status_code=422, detail="to must be after from")
    previous_window = None
    if compare_from is not None or compare_to is not None:
        if compare_from is None or compare_to is None or compare_to <= compare_from:
            raise HTTPException(
                status_code=422, detail="compare_from and compare_to must form a window"
            )
        previous_window = (compare_from, compare_to)
    return ExecutiveOverviewResponse.model_validate(
        repository.executive_overview(from_, to, filters, previous_window)
    )


@router.get("/api/v1/observability/distribution", response_model=DistributionResponse)
def get_distribution(
    repository: Repository,
    filters: UsageFilterSet,
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
                from_, to, dimension, filters, limit, split_by
            ),
        }
    )


@router.get("/api/v1/observability/requests", response_model=UsageRequestListResponse)
def list_usage_requests(
    repository: Repository,
    filters: UsageFilterSet,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> UsageRequestListResponse:
    sync_apim_router_traces_if_configured(repository)
    after: tuple[datetime, str] | None = None
    if cursor:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            after = (datetime.fromisoformat(payload["timestamp"]), str(payload["trace_id"]))
        except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail="Invalid request cursor") from error
    rows = repository.list_usage_requests(from_, to, filters, limit + 1, after)
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {"timestamp": last["timestamp"].isoformat(), "trace_id": last["trace_id"]},
                    separators=(",", ":"),
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return UsageRequestListResponse(
        items=[
            UsageRequestSummary.model_validate(row)
            for row in items
        ],
        page=PageInfo(next_cursor=next_cursor),
    )


@router.get("/api/v1/observability/requests/{request_id}", response_model=UsageRequestDetail)
def get_usage_request(request_id: str, repository: Repository) -> UsageRequestDetail:
    sync_apim_router_traces_if_configured(repository, request_id=request_id)
    row = repository.get_usage_request(request_id)
    if row is not None and row.get("router_id") is None and row.get("request_id"):
        sync_apim_router_traces_if_configured(
            repository, request_id=str(row["request_id"])
        )
        row = repository.get_usage_request(request_id)
    if row is None:
        sync_apim_router_traces_if_configured(
            repository, force=True, request_id=request_id
        )
        row = repository.get_usage_request(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Usage request not found")
    return UsageRequestDetail.model_validate(row)


@router.get("/api/v1/observability/anomalies", response_model=UsageAnomalyListResponse)
def list_usage_anomalies(
    repository: Repository,
    filters: UsageFilterSet,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> UsageAnomalyListResponse:
    return UsageAnomalyListResponse(
        items=[
            UsageAnomaly.model_validate(row)
            for row in repository.list_usage_anomalies(from_, to, filters, limit)
        ]
    )


@router.get("/api/v1/observability/overview", response_model=OverviewResponse)
def get_overview(repository: Repository, timezone: str = "UTC") -> dict[str, object]:
    return repository.overview(timezone)


@router.get("/api/v1/observability/trends", response_model=TrendResponse)
def get_trends(
    repository: Repository,
    filters: UsageFilterSet,
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
    return repository.trends(from_, to, interval, group_by, timezone, filters)


@router.get("/api/v1/observability/runs", response_model=RunListResponse)
def list_runs(
    repository: Repository,
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
def get_run(run_id: str, repository: Repository) -> RunDetail:
    row = repository.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunDetail.model_validate(row)


@router.get("/api/v1/observability/audit-findings", response_model=AuditFindingListResponse)
def list_findings(
    repository: Repository,
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
    repository: Repository, limit: Annotated[int, Query(ge=1, le=200)] = 50
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