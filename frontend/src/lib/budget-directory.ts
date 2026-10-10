import type { DirectoryOrganization, DirectoryPerson, DirectoryUnit } from "../data-sources/apim/api/organization-management"
import type { PeopleBudgetFilter, TokenBudgetItem, TokenBudgetPeopleResponse } from "../data-sources/apim/types"
import { sortDirectoryNodes } from "./directory-visibility"

export type BudgetDirectoryScope = { type: "organization" | "department"; id: string }
export type BudgetDirectory = { organizations: DirectoryOrganization[]; units: DirectoryUnit[] }

export function budgetDirectoryItems(
  items: TokenBudgetItem[], directory: BudgetDirectory, hideArchived: boolean, locale: string,
) {
  const organizations = new Map(directory.organizations.map(row => [row.id, row]))
  const departments = new Map(directory.units.filter(row => row.kind === "department").map(row => [row.id, row]))
  const ranked = items.filter(row => row.scope_type !== "user").map(item => ({
    ...item, budgetStatus: item.status, id: `${item.scope_type}:${item.scope_id}`, name: item.scope_name,
    directoryStatus: (item.scope_type === "organization" ? organizations : departments).get(item.scope_id)?.status ?? "archived",
  }))
  return sortDirectoryNodes(ranked.map(row => ({ ...row, status: row.directoryStatus })), locale)
    .filter(row => !hideArchived || (row.directoryStatus === "active"
      && (row.scope_type === "organization" || organizations.get(row.parent_scope_id ?? "")?.status === "active")))
    .map(row => ({ ...row, status: row.budgetStatus }))
}

export function budgetScopeDepartments(items: TokenBudgetItem[], scope: BudgetDirectoryScope) {
  return items.filter(row => row.scope_type === "department"
    && (scope.type === "department" ? row.scope_id === scope.id : row.parent_scope_id === scope.id))
}

export function budgetScopeTeams(
  directory: BudgetDirectory, scope: BudgetDirectoryScope, hideArchived: boolean, locale: string,
) {
  const department = directory.units.find(row => row.id === scope.id && row.kind === "department")
  return sortDirectoryNodes(directory.units.filter(row => row.kind === "team"
    && (scope.type === "organization" ? row.organization_id === scope.id : row.parent_unit_id === department?.id)
    && (!hideArchived || row.status === "active")), locale)
}

export function filterBudgetPeople(
  items: TokenBudgetItem[], query: string, status: PeopleBudgetFilter,
  teamMembers: Set<string> | null, locale: string,
) {
  const search = query.trim().toLocaleLowerCase(locale)
  const names = new Intl.Collator(locale, { numeric: true, sensitivity: "base" })
  return items.filter(row => (!search || `${row.scope_name} ${row.scope_id}`.toLocaleLowerCase(locale).includes(search))
    && (status === "all" || (status === "assigned" ? row.token_limit != null : row.status === status))
    && (!teamMembers || teamMembers.has(row.scope_id)))
    .sort((a, b) => names.compare(a.scope_name, b.scope_name) || names.compare(a.scope_id, b.scope_id))
}

export function selectedBudgetDepartment(items: TokenBudgetItem[], selectedIds: Set<string>) {
  const selected = items.filter(row => selectedIds.has(row.scope_id))
  const departments = new Set(selected.map(row => row.parent_scope_id))
  return selected.length === selectedIds.size && departments.size === 1 ? selected[0]?.parent_scope_id ?? null : null
}

// Read every page before client-side organization/team filtering; never filter just one page.
export async function readDepartmentBudgetPeople(
  departmentIds: string[],
  read: (departmentId: string, offset: number, limit: number) => Promise<TokenBudgetPeopleResponse>,
) {
  const departments: TokenBudgetPeopleResponse[] = []
  for (let start = 0; start < departmentIds.length; start += 4) {
    const group = await Promise.all(departmentIds.slice(start, start + 4).map(async departmentId => {
      const first = await read(departmentId, 0, 200)
      const items = [...first.items]
      while (items.length < first.total) {
        const page = await read(departmentId, items.length, 200)
        if (page.total !== first.total || page.items.length === 0) throw new Error("人员预算列表已变更，请刷新")
        items.push(...page.items)
      }
      return { ...first, items }
    }))
    departments.push(...group)
  }
  return { departments, items: departments.flatMap(row => row.items) }
}

export async function readTeamBudgetMembers(
  read: (cursor?: string) => Promise<{ items: DirectoryPerson[]; next_cursor: string | null }>,
) {
  const members = new Set<string>()
  const cursors = new Set<string>()
  let cursor: string | undefined
  do {
    const page = await read(cursor)
    page.items.forEach(row => members.add(row.governance_user_id))
    cursor = page.next_cursor ?? undefined
    if (cursor) {
      if (cursors.has(cursor)) throw new Error("组织成员列表已变更，请刷新")
      cursors.add(cursor)
    }
  } while (cursor)
  return members
}
