from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .models import (
    EnterpriseCatalogEntity,
    EnterpriseCatalogResponse,
    EnterpriseCatalogWrite,
    EnterpriseEntity,
    EnterpriseEntityCatalog,
)

ORGANIZATION_ID = "org-contoso-global"
DEFAULT_APPLICATION_USER_DEPARTMENT_ID = "department-platform"


def catalog_write_problems(write: EnterpriseCatalogWrite) -> list[str]:
    """Structural errors a schema cannot express. Empty means the catalog can be stored."""
    problems: list[str] = []
    organization_ids = [item.id for item in write.organizations]
    department_ids = [item.id for item in write.departments]
    for kind, ids in (("organization", organization_ids), ("department", department_ids)):
        seen: set[str] = set()
        for id_ in ids:
            if id_ in seen:
                problems.append(f"Duplicate {kind} id: {id_}")
            seen.add(id_)
    known_organizations = set(organization_ids)
    for department in write.departments:
        if department.parent_id not in known_organizations:
            problems.append(
                f"Department {department.id} names parent {department.parent_id}, "
                "which is not an organization in this catalog"
            )
    if write.default_department_id and write.default_department_id not in set(department_ids):
        problems.append(
            f"Default department {write.default_department_id} is not a department in this catalog"
        )
    for entity in [*write.organizations, *write.departments]:
        for key in entity.attributes:
            if not key or len(key) > 64:
                problems.append(f"{entity.id}: attribute names must be 1 to 64 characters")
    return problems


def catalog_rows(write: EnterpriseCatalogWrite) -> list[dict[str, Any]]:
    """The rows a write stores, in document order so the catalog reads back as written."""
    rows: list[dict[str, Any]] = []
    for position, organization in enumerate(write.organizations):
        rows.append(
            {
                "entity_type": "organization",
                "entity_id": organization.id,
                "name": organization.name,
                "parent_id": None,
                "is_default": False,
                "external_ref": organization.external_ref,
                "attributes": dict(organization.attributes),
                "position": position,
            }
        )
    for position, department in enumerate(write.departments):
        rows.append(
            {
                "entity_type": "department",
                "entity_id": department.id,
                "name": department.name,
                "parent_id": department.parent_id,
                "is_default": department.id == write.default_department_id,
                "external_ref": department.external_ref,
                "attributes": dict(department.attributes),
                "position": position,
            }
        )
    return rows


def configured_catalog(rows: Iterable[Mapping[str, Any]]) -> EnterpriseEntityCatalog | None:
    """The stored catalog, or None when nothing has been configured.

    Projects, agents and people are not part of it: people are discovered from gateway
    usage and Owner accounts, as with the seeded catalog, and a configured catalog does not
    invent projects or agents that nobody defined.
    """
    ordered = sorted(rows, key=lambda row: (str(row["entity_type"]), int(row["position"])))
    organizations = [
        EnterpriseEntity(id=str(row["entity_id"]), name=str(row["name"]))
        for row in ordered
        if row["entity_type"] == "organization"
    ]
    if not organizations:
        return None
    departments = [
        EnterpriseEntity(
            id=str(row["entity_id"]), name=str(row["name"]), parent_id=str(row["parent_id"])
        )
        for row in ordered
        if row["entity_type"] == "department"
    ]
    default = next(
        (str(row["entity_id"]) for row in ordered if row.get("is_default")),
        departments[0].id if departments else None,
    )
    return EnterpriseEntityCatalog(
        organizations=organizations,
        departments=departments,
        projects=[],
        agents=[],
        users=[],
        default_department_id=default,
    )


def resolve_enterprise_catalog(rows: Iterable[Mapping[str, Any]]) -> EnterpriseEntityCatalog:
    """The configured catalog when there is one, otherwise the seeded demonstration."""
    return configured_catalog(rows) or enterprise_catalog()


def default_department_id(catalog: EnterpriseEntityCatalog) -> str | None:
    if catalog.default_department_id:
        return catalog.default_department_id
    ids = {item.id for item in catalog.departments}
    if DEFAULT_APPLICATION_USER_DEPARTMENT_ID in ids:
        return DEFAULT_APPLICATION_USER_DEPARTMENT_ID
    return catalog.departments[0].id if catalog.departments else None


def catalog_response(rows: Iterable[Mapping[str, Any]]) -> EnterpriseCatalogResponse:
    stored = list(rows)
    if not any(row["entity_type"] == "organization" for row in stored):
        seeded = enterprise_catalog()
        return EnterpriseCatalogResponse(
            source="seeded",
            organizations=[
                EnterpriseCatalogEntity(id=item.id, name=item.name) for item in seeded.organizations
            ],
            departments=[
                EnterpriseCatalogEntity(id=item.id, name=item.name, parent_id=item.parent_id)
                for item in seeded.departments
            ],
            default_department_id=default_department_id(seeded),
        )
    ordered = sorted(stored, key=lambda row: (str(row["entity_type"]), int(row["position"])))
    entities = {
        kind: [
            EnterpriseCatalogEntity(
                id=str(row["entity_id"]),
                name=str(row["name"]),
                parent_id=row.get("parent_id"),
                external_ref=row.get("external_ref"),
                attributes=dict(row.get("attributes") or {}),
            )
            for row in ordered
            if row["entity_type"] == kind
        ]
        for kind in ("organization", "department")
    }
    dated = [row for row in stored if row.get("updated_at")]
    latest: Mapping[str, Any] = max(dated, key=lambda row: row["updated_at"]) if dated else {}
    return EnterpriseCatalogResponse(
        source="configured",
        organizations=entities["organization"],
        departments=entities["department"],
        default_department_id=next(
            (str(row["entity_id"]) for row in ordered if row.get("is_default")),
            entities["department"][0].id if entities["department"] else None,
        ),
        updated_at=latest.get("updated_at"),
        updated_by=latest.get("updated_by"),
    )


def configured_invocation_testers(
    catalog: EnterpriseEntityCatalog, tester_ids: Iterable[str]
) -> list[EnterpriseEntity]:
    users = {user.id.casefold(): user for user in catalog.users}
    return [users[user_id.casefold()] for user_id in tester_ids if user_id.casefold() in users]


def configured_invocation_tester(
    catalog: EnterpriseEntityCatalog, tester_ids: Iterable[str], user_id: str
) -> EnterpriseEntity | None:
    configured = {
        tester.id.casefold(): tester
        for tester in configured_invocation_testers(catalog, tester_ids)
    }
    return configured.get(user_id.casefold())


def merge_application_owners(
    catalog: EnterpriseEntityCatalog, users: Iterable[Mapping[str, Any]]
) -> EnterpriseEntityCatalog:
    """Add Owner accounts before they generate gateway traffic."""
    department = default_department_id(catalog)
    if department is None:
        # A catalog with no departments has nowhere to list a person.
        return catalog
    existing = {item.id for item in catalog.users}
    discovered: list[EnterpriseEntity] = []
    for row in users:
        if row.get("role") != "owner":
            continue
        user_id = str(row.get("email") or "").strip().lower()
        if "@" not in user_id or user_id in existing:
            continue
        existing.add(user_id)
        discovered.append(
            EnterpriseEntity(
                id=user_id,
                name=str(row.get("display_name") or user_id).strip() or user_id,
                parent_id=department,
            )
        )
    if not discovered:
        return catalog
    return catalog.model_copy(
        update={"users": [*catalog.users, *sorted(discovered, key=lambda item: item.id)]}
    )


def merge_observed_users(
    catalog: EnterpriseEntityCatalog, observed: Iterable[Mapping[str, Any]]
) -> EnterpriseEntityCatalog:
    """Add people who actually called the gateway to the seeded catalog.

    Without this the catalog is a fixed demo list, so a real employee can never be
    given a budget: the budget API validates a user scope against the catalog and
    rejects anyone missing from it. Governance would then apply only to identities
    that generate no traffic.

    A discovered person must resolve to a known department. The budget hierarchy is
    organization -> department -> user, so an unattributed person has nowhere to hang
    and could not be allocated against a parent limit even if they were listed.

    They must also carry an email-shaped id. The gateway derives `x-user-id` from
    `preferred_username`/`upn`/`email`, so every genuine identity has one; ids without
    an `@` are pre-email historical rows whose people are already in the seeded list,
    and admitting them would list the same person twice.

    That same rule is what keeps machine identities out. The runtime health probe used
    to borrow an employee, which made a runtime's verdict depend on that employee's
    budget and model policy. It now calls as `system-runtime-health-check`, and because
    that id has no `@` it can never be listed as a person, never be given a budget and
    never be given a model policy for the check to trip over.
    """
    known_departments = {item.id for item in catalog.departments}
    existing = {item.id for item in catalog.users}
    discovered: list[EnterpriseEntity] = []
    for row in observed:
        user_id = (row.get("user_id") or "").strip()
        department_id = (row.get("department_id") or "").strip()
        if "@" not in user_id or user_id in existing or department_id not in known_departments:
            continue
        existing.add(user_id)
        discovered.append(
            EnterpriseEntity(
                id=user_id,
                name=(row.get("user_ref") or user_id).strip() or user_id,
                parent_id=department_id,
            )
        )
    if not discovered:
        return catalog
    return catalog.model_copy(
        update={"users": [*catalog.users, *sorted(discovered, key=lambda item: item.id)]}
    )


def enterprise_catalog() -> EnterpriseEntityCatalog:
    departments = [
        ("department-platform", "AI Platform"),
        ("department-commerce", "Commerce"),
        ("department-finance", "Finance"),
        ("department-support", "Customer Support"),
        ("department-security", "Security"),
    ]
    projects = [
        ("project-finops", "Model FinOps", "department-platform"),
        ("project-runtime", "Agent Runtime", "department-platform"),
        ("project-catalog", "Catalog Intelligence", "department-commerce"),
        ("project-checkout", "Checkout Assistant", "department-commerce"),
        ("project-close", "Month-end Close", "department-finance"),
        ("project-forecast", "Financial Forecast", "department-finance"),
        ("project-triage", "Support Triage", "department-support"),
        ("project-knowledge", "Support Knowledge", "department-support"),
        ("project-threat", "Threat Analysis", "department-security"),
        ("project-compliance", "AI Compliance", "department-security"),
    ]
    agents = [
        ("agent-delivery", "Delivery Engineer", "project-finops"),
        (
            "agent-chatgpt-desktop-codex",
            "ChatGPT Desktop Codex",
            "project-finops",
        ),
        ("agent-architect", "Product Architect", "project-runtime"),
        ("agent-catalog", "Catalog Curator", "project-catalog"),
        ("agent-checkout", "Checkout Copilot", "project-checkout"),
        ("agent-close", "Close Analyst", "project-close"),
        ("agent-forecast", "Forecast Analyst", "project-forecast"),
        ("agent-triage", "Support Triage", "project-triage"),
        ("agent-knowledge", "Knowledge Editor", "project-knowledge"),
        ("agent-threat", "Threat Hunter", "project-threat"),
        ("agent-compliance", "Compliance Reviewer", "project-compliance"),
    ]
    users = [
        EnterpriseEntity(
            id=f"test.user{index:02d}@contoso.com",
            name=f"test.user{index:02d}@contoso.com",
            parent_id=departments[(index - 1) % len(departments)][0],
        )
        for index in range(1, 21)
    ]
    return EnterpriseEntityCatalog(
        organizations=[EnterpriseEntity(id=ORGANIZATION_ID, name="Contoso Global")],
        departments=[
            EnterpriseEntity(id=id_, name=name, parent_id=ORGANIZATION_ID)
            for id_, name in departments
        ],
        projects=[
            EnterpriseEntity(id=id_, name=name, parent_id=parent_id)
            for id_, name, parent_id in projects
        ],
        agents=[
            EnterpriseEntity(id=id_, name=name, parent_id=parent_id)
            for id_, name, parent_id in agents
        ],
        users=users,
    )
