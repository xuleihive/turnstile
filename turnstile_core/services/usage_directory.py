"""Read-only directory state for usage facets; historical attribution stays immutable."""

from __future__ import annotations

from ..domain.models import (
    EnterpriseEntityCatalog,
    UsageQueryEntity,
    UsageQueryEntityCatalog,
)
from ..persistence.repository_support import (
    USAGE_DIRECTORY_STATUSES,
    UsageDirectoryScope,
    UsageDirectoryStatus,
)

CATALOG_KEYS = ("organizations", "departments", "projects", "agents", "users")


def scoped_usage_catalog(
    catalog: EnterpriseEntityCatalog, department_ids: tuple[str, ...] | None,
) -> EnterpriseEntityCatalog:
    if department_ids is None:
        return catalog
    allowed = set(department_ids)
    departments = [row for row in catalog.departments if row.id in allowed]
    organization_ids = {row.parent_id for row in departments}
    projects = [row for row in catalog.projects if row.parent_id in allowed]
    project_ids = {row.id for row in projects}
    return catalog.model_copy(update={
        "organizations": [row for row in catalog.organizations if row.id in organization_ids],
        "departments": departments,
        "projects": projects,
        "agents": [row for row in catalog.agents if row.parent_id in project_ids],
        "users": [row for row in catalog.users if row.parent_id in allowed],
        "invocation_testers": [],
    })


def usage_directory_scope(
    current: EnterpriseEntityCatalog,
    complete: EnterpriseEntityCatalog,
    statuses: tuple[UsageDirectoryStatus, ...],
) -> UsageDirectoryScope | None:
    if set(statuses) == set(USAGE_DIRECTORY_STATUSES):
        return None
    active_organizations = tuple(row.id for row in current.organizations)
    active_departments = {
        row.id: row.parent_id for row in current.departments if row.parent_id is not None
    }
    active_users = {row.id for row in current.users}
    return UsageDirectoryScope(
        statuses=statuses,
        active_organizations=active_organizations,
        archived_organizations=tuple(
            row.id for row in complete.organizations if row.id not in active_organizations
        ),
        active_departments=active_departments,
        archived_departments=tuple(
            row.id for row in complete.departments if row.id not in active_departments
        ),
        archived_users=tuple(row.id for row in complete.users if row.id not in active_users),
    )


def usage_query_catalog(
    current: EnterpriseEntityCatalog,
    complete: EnterpriseEntityCatalog,
    historical: UsageQueryEntityCatalog,
    statuses: tuple[UsageDirectoryStatus, ...],
) -> UsageQueryEntityCatalog:
    groups: dict[str, list[UsageQueryEntity]] = {}
    status_by_id: dict[str, dict[str, UsageDirectoryStatus]] = {}
    parent_group = {
        "departments": "organizations", "projects": "departments",
        "agents": "projects", "users": "departments",
    }
    for key in CATALOG_KEYS:
        known = {row.id: row for row in getattr(complete, key)}
        active_ids = {row.id for row in getattr(current, key)}
        observed = {row.id: row for row in getattr(historical, key)}
        # Current names win; usage rows and their original billing identities are never rewritten.
        merged = {**observed, **known}
        states: dict[str, UsageDirectoryStatus] = {}
        result: list[UsageQueryEntity] = []
        for identity, row in merged.items():
            parents = sorted({
                item.parent_id for item in (row, observed.get(identity))
                if item is not None and item.parent_id is not None
            } | set(observed[identity].parent_ids if identity in observed else []))
            if identity == "unattributed":
                status: UsageDirectoryStatus = "unattributed"
            elif identity in known:
                status = "active" if identity in active_ids else "archived"
            else:
                status = status_by_id.get(parent_group.get(key, ""), {}).get(
                    row.parent_id or "", "historical",
                )
            states[identity] = status
            # Observed rows already satisfy the request's status/permission constraints.
            # A moved person can be active today but have archived-scope usage in this window.
            if status in statuses or identity in observed:
                result.append(UsageQueryEntity(
                    id=identity, name=row.name, parent_id=row.parent_id, parent_ids=parents,
                    directory_status=status,
                    source="directory" if identity in known else "historical",
                ))
        status_by_id[key] = states
        groups[key] = sorted(result, key=lambda item: (item.name.casefold(), item.id))
    # Keep current ancestors available for archived children without relabeling their state.
    for key in reversed(CATALOG_KEYS):
        parent_key = parent_group.get(key)
        if not parent_key:
            continue
        required = {parent for row in groups[key] for parent in row.parent_ids}
        present = {row.id for row in groups[parent_key]}
        available = {
            row.id: row for row in (
                *getattr(historical, parent_key), *getattr(complete, parent_key),
            )
        }
        for identity in sorted(required - present):
            row = available.get(identity)
            if row is not None:
                groups[parent_key].append(UsageQueryEntity(
                    id=row.id, name=row.name, parent_id=row.parent_id,
                    parent_ids=[row.parent_id] if row.parent_id else [],
                    directory_status=status_by_id[parent_key][row.id],
                    source="directory" if any(
                        item.id == identity for item in getattr(complete, parent_key)
                    ) else "historical",
                ))
        groups[parent_key].sort(key=lambda item: (item.name.casefold(), item.id))
    return UsageQueryEntityCatalog.model_validate(groups)
