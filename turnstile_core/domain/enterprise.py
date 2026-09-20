from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .models import EnterpriseEntity, EnterpriseEntityCatalog

ORGANIZATION_ID = "org-contoso-global"
DEFAULT_APPLICATION_USER_DEPARTMENT_ID = "department-platform"


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
    existing = {item.id for item in catalog.users}
    known = [item.id for item in catalog.departments]
    # The department an Owner is parked under is configurable now that departments are, so
    # the constant can no longer be assumed present. Parking them under a department this
    # install has retired would hide them from the budget tree, which is the one place an
    # administrator account has to be visible.
    parent = (
        DEFAULT_APPLICATION_USER_DEPARTMENT_ID
        if DEFAULT_APPLICATION_USER_DEPARTMENT_ID in known
        else next(iter(known), None)
    )
    if parent is None:
        return catalog
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
                parent_id=parent,
            )
        )
    if not discovered:
        return catalog
    return catalog.model_copy(
        update={"users": [*catalog.users, *sorted(discovered, key=lambda item: item.id)]}
    )


def merge_observed_users(
    catalog: EnterpriseEntityCatalog,
    observed: Iterable[Mapping[str, Any]],
    *,
    excluded_ids: frozenset[str] = frozenset(),
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

    `excluded_ids` is the same idea for identities that are email-shaped but still not
    people here -- the seeded fixtures on an install that has disowned them. One test call
    made as `test.user01@contoso.com` is enough to put that name back in the org chart,
    where it reads as an employee rather than as the acceptance check it was.
    """
    known_departments = {item.id for item in catalog.departments}
    existing = {item.id for item in catalog.users}
    disowned = {item.casefold() for item in excluded_ids}
    discovered: list[EnterpriseEntity] = []
    for row in observed:
        user_id = (row.get("user_id") or "").strip()
        department_id = (row.get("department_id") or "").strip()
        if "@" not in user_id or user_id in existing or department_id not in known_departments:
            continue
        if user_id.casefold() in disowned:
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


def organization_structure(
    units: Iterable[Mapping[str, Any]],
) -> tuple[list[EnterpriseEntity], list[EnterpriseEntity]]:
    """Split stored org units into the organizations and the departments, active only.

    Retired units are dropped rather than marked, because every caller of this asks the same
    question -- what may something be attributed to now -- and a retired department that still
    appears in a picker is a department that is still being chosen.

    Their history survives regardless: budgets, usage and channels reference the id as text, so
    a retired department keeps every row that ever pointed at it. What it loses is the ability
    to collect new ones.
    """
    organizations: list[EnterpriseEntity] = []
    departments: list[EnterpriseEntity] = []
    for row in units:
        if str(row.get("status") or "active") != "active":
            continue
        entity = EnterpriseEntity(
            id=str(row["id"]),
            name=str(row.get("display_name") or row["id"]),
            parent_id=str(row["parent_id"]) if row.get("parent_id") else None,
        )
        if str(row.get("unit_type")) == "organization":
            organizations.append(entity)
        else:
            departments.append(entity)
    return (
        sorted(organizations, key=lambda item: item.name),
        sorted(departments, key=lambda item: item.name),
    )


def _with_structure(
    catalog: EnterpriseEntityCatalog, units: Iterable[Mapping[str, Any]] | None
) -> EnterpriseEntityCatalog:
    """Replace the seeded organization and departments with the ones this install stores.

    `units=None` keeps the seeded structure, which is what the traffic generator and every
    test that predates the table want. Once a deployment has the table, its rows win.

    Projects are re-parented by filtering rather than by reassignment: a project whose
    department has been retired drops out with it. Moving it to a surviving department would
    invent an attribution nobody asked for, and projects are seeded scaffolding that no real
    install populates -- the customer's traffic reports `project_id: unattributed` for every
    call.
    """
    if units is None:
        return catalog
    organizations, departments = organization_structure(units)
    known = {item.id for item in departments}
    projects = [item for item in catalog.projects if item.parent_id in known]
    surviving = {item.id for item in projects}
    return catalog.model_copy(
        update={
            "organizations": organizations,
            "departments": departments,
            "projects": projects,
            "agents": [item for item in catalog.agents if item.parent_id in surviving],
            "users": [item for item in catalog.users if item.parent_id in known],
        }
    )


def merge_subscription_owners(
    catalog: EnterpriseEntityCatalog,
    applications: Iterable[Mapping[str, Any]],
) -> EnterpriseEntityCatalog:
    """Add the people named as holding a gateway subscription.

    Usage attributed through a subscription never writes a person into `token_usage.user_id` --
    that column is immutable per correlation, so it keeps saying `unattributed` -- which means a
    holder would never appear in the roster built from observed traffic, and could therefore
    never be given a budget however much their key spent. Naming someone as the holder of a key
    is the same statement as calling the gateway as them, and has to put them in the same list.

    The same two rules as a discovered person: an email-shaped id, because the budget hierarchy
    refuses anything else and the `@` is what keeps machine identities out; and a department that
    exists, because organization -> department -> user is where a person hangs.
    """
    known_departments = {item.id for item in catalog.departments}
    existing = {item.id.casefold() for item in catalog.users}
    discovered: list[EnterpriseEntity] = []
    for row in applications:
        if row.get("system_managed"):
            continue
        owner_id = (row.get("owner_id") or "").strip()
        department_id = (row.get("department_id") or "").strip()
        if "@" not in owner_id or department_id not in known_departments:
            continue
        if owner_id.casefold() in existing:
            continue
        existing.add(owner_id.casefold())
        discovered.append(
            EnterpriseEntity(id=owner_id, name=owner_id, parent_id=department_id)
        )
    if not discovered:
        return catalog
    return catalog.model_copy(
        update={"users": sorted([*catalog.users, *discovered], key=lambda item: item.id)}
    )


def governance_directory(
    observed: Iterable[Mapping[str, Any]],
    *,
    include_seeded_people: bool,
    units: Iterable[Mapping[str, Any]] | None = None,
    applications: Iterable[Mapping[str, Any]] | None = None,
) -> EnterpriseEntityCatalog:
    """The catalog an administrator reads: the org structure and the people in it.

    `enterprise_catalog()` carries twenty fixture people so the traffic generator has
    somewhere to attribute generated calls that is deliberately not a real employee. That is
    the right call for generated traffic and a poor first impression for a real deployment:
    the budget page opens on `test.user01@contoso.com` through `test.user20@contoso.com`, none
    of whom exist at the customer, none of whom can be deleted -- they are generated on every
    request -- and all of whom stand between the operator and the people they came to allocate.

    With the fixtures disowned the roster starts empty and fills from the people who have
    actually used the gateway, which is how real employees have always arrived. The departments
    stay either way: a discovered person has to resolve to a known department to hang in the
    budget hierarchy.

    Disowning them also means disowning their traffic. A single acceptance check made as
    `test.user01@contoso.com` -- a 403 denial, no tokens, no cost -- was enough to put that
    name back on the budget page as though it were an employee. If this install says the
    fixtures are not its people, a call from one does not make them one.

    The flag and the stored structure are both passed in rather than read here because the
    domain layer imports neither configuration nor persistence -- see the layering test.
    """
    catalog = _with_structure(enterprise_catalog(), units)
    if include_seeded_people:
        roster = merge_observed_users(catalog, observed)
    else:
        roster = merge_observed_users(
            catalog.model_copy(update={"users": []}),
            observed,
            excluded_ids=frozenset(user.id for user in catalog.users),
        )
    if applications is None:
        return roster
    return merge_subscription_owners(roster, applications)


def governance_departments(
    units: Iterable[Mapping[str, Any]] | None = None,
) -> list[EnterpriseEntity]:
    """The departments an administrator may attribute something to.

    Separate from `governance_directory` because the seeded-people flag does not reach
    departments -- both modes carry the same set -- so a caller that only needs the
    structure should not have to read the traffic table to get it, nor reach into
    `enterprise_catalog()` and pick up the fixture people on the way past.
    """
    if units is None:
        return enterprise_catalog().departments
    return organization_structure(units)[1]


def enterprise_catalog() -> EnterpriseEntityCatalog:
    """Everything seeded, fixture people included.

    Only the traffic generator should take the people from here. Anything an administrator
    reads goes through `governance_directory()`.
    """
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
