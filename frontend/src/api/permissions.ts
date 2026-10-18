import { request, writeJson } from "./client"
import type { MenuPermissionGroup } from "./auth"

export type PermissionConfiguration = {
  revision: number
  groups: Record<MenuPermissionGroup, string[]>
  updated_by: string
  updated_at: string
  catalog: Array<{
    id: string; module: string; label: string; source: string
    owner_only: boolean; required: boolean
  }>
}

export const permissionApi = {
  read: () => request<PermissionConfiguration>("/api/v1/permission-management"),
  save: (value: Pick<PermissionConfiguration, "groups"> & { expected_revision: number }) =>
    writeJson<PermissionConfiguration>("/api/v1/permission-management", value, "PUT"),
}
