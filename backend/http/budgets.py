from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from turnstile_core.config import get_settings
from turnstile_core.domain.budget_delegation import BudgetCapabilities
from turnstile_core.domain.ledger import (
    BudgetReservationResolution,
    BudgetReservationResolutionWrite,
    LedgerAttempt,
    LedgerProtocolConflict,
    LedgerScope,
)
from turnstile_core.domain.models import (
    DepartmentEnforcementWrite,
    PeopleBudgetFilter,
    TokenBudgetBulkResult,
    TokenBudgetBulkWrite,
    TokenBudgetPeopleResponse,
    TokenBudgetResponse,
    TokenBudgetWrite,
)

from ..services.budget_readiness import budget_capabilities
from ..services.budget_service import BudgetConflictError, BudgetNotFoundError
from .dependencies import Repository
from .service_dependencies import BudgetReservationServiceDependency, TokenBudgetServiceDependency
from .session import (
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


@router.get("/api/v1/budgets/capabilities", response_model=BudgetCapabilities)
def get_budget_capabilities(
    identity: CurrentSession, repository: Repository, response: Response,
) -> BudgetCapabilities:
    if identity.directory_department_ids == ():
        raise HTTPException(status_code=403, detail="Budget scope access is not granted")
    response.headers["Cache-Control"] = "no-store"
    return budget_capabilities(repository, get_settings())


@router.get("/api/v1/budgets/reservations", response_model=list[LedgerAttempt])
def review_budget_reservations(
    identity: OwnerSession,
    service: BudgetReservationServiceDependency,
    scope_type: Literal["person", "application"],
    scope_id: Annotated[str, Query(min_length=1, max_length=255)],
    period: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=20)] = 20,
    include_resolved: bool = False,
) -> list[LedgerAttempt]:
    del identity
    try:
        scope = LedgerScope(
            scope_type=scope_type, scope_id=scope_id,
            period_start=date.fromisoformat(period + "-01"),
        )
        return service.review(scope, limit=limit, include_resolved=include_resolved)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid reservation scope") from error


@router.post(
    "/api/v1/budgets/reservations/resolve",
    response_model=BudgetReservationResolution,
    status_code=202,
)
def approve_budget_reservation_resolution(
    write: BudgetReservationResolutionWrite,
    identity: OwnerSession,
    service: BudgetReservationServiceDependency,
) -> BudgetReservationResolution:
    try:
        return service.approve(write, identity.email)
    except LedgerProtocolConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/v1/budgets", response_model=TokenBudgetResponse)
def get_token_budgets(
    service: TokenBudgetServiceDependency,
    identity: CurrentSession,
    period: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    include_users: bool = False,
) -> TokenBudgetResponse:
    if identity.directory_department_ids == ():
        raise HTTPException(status_code=403, detail="Budget scope access is not granted")
    return service.overview(
        period,
        include_users=include_users,
        allowed_department_ids=identity.directory_department_ids,
        data_scope=identity.data_scope,
    )


@router.get("/api/v1/budgets/users", response_model=TokenBudgetPeopleResponse)
def get_people_budgets(
    service: TokenBudgetServiceDependency,
    identity: CurrentSession,
    period: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department_id: str,
    query: Annotated[str | None, Query(max_length=200)] = None,
    status: PeopleBudgetFilter = "all",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> TokenBudgetPeopleResponse:
    if (
        identity.directory_department_ids is not None
        and department_id not in identity.directory_department_ids
    ):
        raise HTTPException(status_code=404, detail="Department budget scope not found")
    try:
        return service.people(
            period, department_id, query, status, offset, limit, data_scope=identity.data_scope,
        )
    except BudgetNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/api/v1/budgets/users/bulk", response_model=TokenBudgetBulkResult)
def bulk_save_people_budgets(
    write: TokenBudgetBulkWrite,
    service: TokenBudgetServiceDependency,
    identity: OwnerSession,
    period: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> TokenBudgetBulkResult:
    try:
        return service.bulk_save_people(period, write, identity.email)
    except BudgetNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BudgetConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.put(
    "/api/v1/budgets/enforcement/{department_id}",
    response_model=TokenBudgetResponse,
)
def save_department_enforcement(
    department_id: str,
    write: DepartmentEnforcementWrite,
    service: TokenBudgetServiceDependency,
    identity: OwnerSession,
    period: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> TokenBudgetResponse:
    try:
        return service.set_enforcement(period, department_id, write, identity.email)
    except BudgetNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


# Declared after the literal /enforcement route so FastAPI cannot treat
# "enforcement" as a dynamic scope_type value.
@router.put("/api/v1/budgets/{scope_type}/{scope_id}", response_model=TokenBudgetResponse)
def save_token_budget(
    scope_type: Literal["organization", "department", "user"],
    scope_id: str,
    write: TokenBudgetWrite,
    service: TokenBudgetServiceDependency,
    identity: OwnerSession,
    period: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> TokenBudgetResponse:
    try:
        return service.save(period, scope_type, scope_id, write, identity.email)
    except BudgetNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BudgetConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.delete("/api/v1/budgets/{scope_type}/{scope_id}", response_model=TokenBudgetResponse)
def delete_token_budget(
    scope_type: Literal["organization", "department", "user"],
    scope_id: str,
    service: TokenBudgetServiceDependency,
    identity: OwnerSession,
    period: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> TokenBudgetResponse:
    try:
        return service.remove(period, scope_type, scope_id, identity.email)
    except BudgetNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except BudgetConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
