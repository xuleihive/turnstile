"""One authority for legacy and persistent enterprise-directory readers."""

from __future__ import annotations

from datetime import datetime

from ..config import Settings, get_settings
from ..domain.enterprise import enterprise_catalog, merge_application_owners, merge_observed_users
from ..domain.models import EnterpriseEntityCatalog
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
    settings = settings or get_settings()
    if settings.directory_source == "database":
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is required for the persistent directory")
        return directory_store(settings.database_url).catalog(
            at_time=at_time,
            include_inactive=include_inactive,
            department_ids=allowed_department_ids,
        )
    if settings.database_url:
        state = directory_store(settings.database_url).state_if_present()
        if state is not None and state["source"] != "legacy":
            raise RuntimeError("Directory authority changed; configure DIRECTORY_SOURCE=database")
    return merge_application_owners(
        merge_observed_users(enterprise_catalog(), repository.observed_users()),
        repository.application_owners(),
    )
