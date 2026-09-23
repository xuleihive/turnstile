"""The organization catalog: the organizations and departments budgets hang on.

Until one is written, Turnstile uses its seeded demonstration catalog. Writing a catalog
replaces it as a whole -- a directory sync or an Owner sends the complete set -- because a
partial update is how a department silently loses its parent.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from turnstile_core.domain.enterprise import catalog_response, catalog_rows, catalog_write_problems
from turnstile_core.domain.models import EnterpriseCatalogResponse, EnterpriseCatalogWrite

from .dependencies import Repository
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


@router.get("/api/v1/enterprise-catalog", response_model=EnterpriseCatalogResponse)
def get_enterprise_catalog(
    repository: Repository, identity: CurrentSession
) -> EnterpriseCatalogResponse:
    return catalog_response(repository.enterprise_entities())


@router.put("/api/v1/enterprise-catalog", response_model=EnterpriseCatalogResponse)
def put_enterprise_catalog(
    write: EnterpriseCatalogWrite, repository: Repository, identity: OwnerSession
) -> EnterpriseCatalogResponse:
    problems = catalog_write_problems(write)
    if problems:
        raise HTTPException(status_code=422, detail=problems)
    repository.replace_enterprise_entities(catalog_rows(write), identity.email)
    return catalog_response(repository.enterprise_entities())


@router.delete("/api/v1/enterprise-catalog", response_model=EnterpriseCatalogResponse)
def delete_enterprise_catalog(
    repository: Repository, identity: OwnerSession
) -> EnterpriseCatalogResponse:
    """Return to the seeded catalog."""
    repository.replace_enterprise_entities([], identity.email)
    return catalog_response(repository.enterprise_entities())
