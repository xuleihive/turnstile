import type {
  DirectoryOrganization, DirectoryStatus, DirectoryUnit,
} from "../data-sources/apim/api/organization-management"

export type ManagedDirectoryStatus = "active" | "archived"

export function managedDirectoryStatus(status: DirectoryStatus): ManagedDirectoryStatus {
  return status === "active" ? "active" : "archived"
}

export function sortDirectoryNodes<T extends { id: string; name: string; status: DirectoryStatus }>(
  nodes: readonly T[], locale: string,
): T[] {
  const names = new Intl.Collator(locale, { numeric: true, sensitivity: "base" })
  return [...nodes].sort((left, right) =>
    Number(right.status === "active") - Number(left.status === "active")
    || names.compare(left.name, right.name)
    || (left.id < right.id ? -1 : left.id > right.id ? 1 : 0))
}

export function visibleDirectoryOrganizations(
  organizations: DirectoryOrganization[], hideArchived: boolean,
) {
  return hideArchived ? organizations.filter((row) => row.status === "active") : organizations
}

export function visibleDirectoryUnits(units: DirectoryUnit[], hideArchived: boolean) {
  if (!hideArchived) return units
  const departments = new Set(units.filter((row) =>
    row.kind === "department" && row.status === "active").map((row) => row.id))
  return units.filter((row) => row.status === "active"
    && (row.kind === "department" || departments.has(row.parent_unit_id ?? "")))
}

export function directoryOrganizationSelection(
  organizations: DirectoryOrganization[], id: string, hideArchived: boolean,
) {
  const visible = visibleDirectoryOrganizations(organizations, hideArchived)
  const selected = organizations.find((row) => row.id === id)
  if (!id || (selected && hideArchived && selected.status !== "active")) return visible[0]?.id ?? ""
  return id
}

export function directoryUnitSelection(units: DirectoryUnit[], id: string, hideArchived: boolean) {
  if (!hideArchived || !id) return id
  const selected = units.find((row) => row.id === id)
  // Unknown IDs remain an access error, not a silent fallback into another scope.
  if (!selected || visibleDirectoryUnits(units, true).some((row) => row.id === id)) return id
  const parent = units.find((row) => row.id === selected.parent_unit_id)
  return parent?.kind === "department" && parent.status === "active" ? parent.id : ""
}
