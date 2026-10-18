"""Service entry point for the shared persistence-level directory reader."""

from __future__ import annotations

from datetime import datetime

from ..config import Settings, get_settings
from ..domain.models import EnterpriseEntityCatalog
from ..persistence.directory_catalog import catalog_for as _catalog_for
from ..persistence.directory_store import directory_store as directory_store
from ..persistence.repository_contract import QueryRepository


def catalog_for(
    repository: QueryRepository,
    settings: Settings | None = None,
    *,
    at_time: datetime | None = None,
    include_inactive: bool = False,
    allowed_department_ids: tuple[str, ...] | None = None,
) -> EnterpriseEntityCatalog:
    return _catalog_for(
        repository,
        settings or get_settings(),
        at_time=at_time,
        include_inactive=include_inactive,
        allowed_department_ids=allowed_department_ids,
    )
