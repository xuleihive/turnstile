import type {
  UsageDirectoryStatus, UsageFilters, UsageQueryEntity, UsageQueryEntityCatalog,
} from "../data-sources/apim/types"

export function usageArchiveSelection(statuses: UsageDirectoryStatus[] | undefined): UsageDirectoryStatus[] {
  if (statuses?.length === 0) return []
  const selected = [...new Set(statuses?.filter(status => status === "active" || status === "archived") ?? [])]
  return selected.length ? selected.sort() : ["active"]
}

export function usageArchiveFilter(statuses: UsageDirectoryStatus[] | undefined): UsageDirectoryStatus[] {
  const selected = usageArchiveSelection(statuses)
  return selected.length ? selected : ["active", "archived"]
}

export function usageSelection(value: string | string[] | undefined): string[] {
  return [...new Set(typeof value === "string" ? [value] : value ?? [])].sort()
}

export function usageFacetWindow(filters: UsageFilters): UsageFilters {
  return { from: filters.from, to: filters.to, directory_status: filters.directory_status }
}

function descendants(items: UsageQueryEntity[], parentIds: string[]) {
  return parentIds.length === 0 ? items : items.filter(row =>
    row.parent_ids.some(id => parentIds.includes(id)),
  )
}

export function usageFacetOptions(catalog: UsageQueryEntityCatalog, scope: UsageFilters) {
  const organizations = usageSelection(scope.organization_id)
  const departments = descendants(catalog.departments, organizations)
  const departmentIds = usageSelection(scope.department_id)
  const scopedDepartmentIds = departmentIds.length ? departmentIds
    : organizations.length ? departments.map(row => row.id) : []
  const projects = descendants(catalog.projects, scopedDepartmentIds)
  const constrained = organizations.length > 0 || departmentIds.length > 0
  return {
    organizations: catalog.organizations,
    departments,
    agents: constrained ? catalog.agents.filter(row =>
      projects.some(project => row.parent_ids.includes(project.id))) : catalog.agents,
    users: constrained ? catalog.users.filter(row =>
      row.parent_ids.some(id => scopedDepartmentIds.includes(id))) : catalog.users,
  }
}

export function reconcileUsageScope(
  scope: Omit<UsageFilters, "from" | "to">, catalog: UsageQueryEntityCatalog,
): Omit<UsageFilters, "from" | "to"> {
  const next = { ...scope }
  const retain = (value: string | string[] | undefined, items: UsageQueryEntity[]) => {
    const valid = new Set(items.map(row => row.id))
    const selected = usageSelection(value).filter(id => valid.has(id))
    return selected.length ? selected : undefined
  }
  next.organization_id = retain(next.organization_id, catalog.organizations)
  const options = usageFacetOptions(catalog, { from: "", to: "", ...next })
  next.department_id = retain(next.department_id, options.departments)
  const children = usageFacetOptions(catalog, { from: "", to: "", ...next })
  next.agent_id = retain(next.agent_id, children.agents)
  next.user_id = retain(next.user_id, children.users)
  return next
}
