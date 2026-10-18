from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from turnstile_core.domain.models import AnomalyRule, AnomalyRuleListResponse, AnomalyRuleWrite
from turnstile_core.services.directory_catalog import catalog_for

from ..services.anomaly_service import AnomalyRuleConflictError, AnomalyRuleNotFoundError
from .dependencies import Repository
from .service_dependencies import AnomalyRuleServiceDependency
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


@router.get("/api/v1/anomaly-rules", response_model=AnomalyRuleListResponse)
def get_anomaly_rules(
    service: AnomalyRuleServiceDependency,
    identity: CurrentSession,
    repository: Repository,
    settings: Config,
) -> AnomalyRuleListResponse:
    result = service.list()
    if identity.data_scope is not None:
        data_scope = identity.data_scope
        catalog = data_scope.filter_catalog(catalog_for(repository, settings))
        people = {person.id for person in catalog.users}
        return result.model_copy(update={"items": [
            rule for rule in result.items
            if (rule.scope_type == "department" and rule.scope_id in data_scope.department_ids)
            or (rule.scope_type == "user" and rule.scope_id in people)
        ]})
    if identity.directory_department_ids is None:
        return result
    scope = identity.directory_department_ids
    catalog = catalog_for(repository, settings, allowed_department_ids=scope)
    people = {person.id for person in catalog.users if person.parent_id in scope}
    return result.model_copy(
        update={
            "items": [
                rule
                for rule in result.items
                if (rule.scope_type == "department" and rule.scope_id in scope)
                or (rule.scope_type == "user" and rule.scope_id in people)
            ]
        }
    )


@router.post("/api/v1/anomaly-rules", response_model=AnomalyRule, status_code=201)
def create_anomaly_rule(
    write: AnomalyRuleWrite,
    service: AnomalyRuleServiceDependency,
    identity: OwnerSession,
) -> AnomalyRule:
    try:
        return service.create(write, identity.email)
    except AnomalyRuleConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.put("/api/v1/anomaly-rules/{rule_id}", response_model=AnomalyRule)
def update_anomaly_rule(
    rule_id: UUID,
    write: AnomalyRuleWrite,
    service: AnomalyRuleServiceDependency,
    identity: OwnerSession,
) -> AnomalyRule:
    try:
        return service.update(rule_id, write, identity.email)
    except AnomalyRuleConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except AnomalyRuleNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete("/api/v1/anomaly-rules/{rule_id}", status_code=204)
def delete_anomaly_rule(
    rule_id: UUID,
    service: AnomalyRuleServiceDependency,
    identity: OwnerSession,
) -> None:
    try:
        service.remove(rule_id)
    except AnomalyRuleNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
