import { request, writeJson } from "./client"
import type { DownloadPermission, MenuPermissionGroup } from "./auth"

export type PermissionConfiguration = {
  revision: number
  groups: Record<MenuPermissionGroup, string[]>
  download_groups: Record<MenuPermissionGroup, DownloadPermission[]>
  download_policy_available?: boolean
  download_catalog: Array<{ id: DownloadPermission; label: string }>
  updated_by: string
  updated_at: string
  catalog: Array<{
    id: string; module: string; label: string; source: string
    owner_only: boolean; required: boolean
  }>
}

export const permissionApi = {
  read: () => request<PermissionConfiguration>("/api/v1/permission-management"),
  save: (value: Pick<PermissionConfiguration, "groups"> & {
    download_groups?: PermissionConfiguration["download_groups"]; expected_revision: number
  }) =>
    writeJson<PermissionConfiguration>("/api/v1/permission-management", value, "PUT"),
}
