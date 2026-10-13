import { request, writeJson } from "../../api/client"

export type AdminUser = {
  id: string
  email: string
  display_name: string | null
  role: string
  enabled: boolean
  organization_unit_id: string | null
  organization_unit_name: string | null
  created_at: string | null
  last_login_at: string | null
}

export type AccessRole = {
  code: string
  display_name: string
  is_builtin: boolean
  permissions: string[]
}

export type OrganizationUnit = {
  id: string
  parent_id: string | null
  code: string
  display_name: string
  enabled: boolean
  created_at: string | null
}

export type AdminUserCreate = {
  email: string
  display_name?: string
  role: string
  department_id?: string
  initial_password?: string
}

export type AdminUserPatch = {
  display_name?: string | null
  role?: string
  enabled?: boolean
  department_id?: string | null
}

export type OrganizationUnitCreate = {
  parent_id?: string | null
  code: string
  display_name: string
}

export type OrganizationUnitPatch = {
  display_name?: string
  enabled?: boolean
}

export const userManagementKeys = {
  users: (search: string) => ["admin-users", search] as const,
  roles: ["admin-roles"] as const,
  organizationUnits: ["admin-organization-units"] as const,
}

export const userManagementApi = {
  listUsers(search: string) {
    const params = new URLSearchParams()
    if (search.trim()) params.set("search", search.trim())
    const suffix = params.size ? `?${params}` : ""
    return request<AdminUser[]>(`/api/v1/admin/users${suffix}`)
  },
  listRoles() {
    return request<AccessRole[]>("/api/v1/admin/roles")
  },
  listOrganizationUnits() {
    return request<OrganizationUnit[]>("/api/v1/admin/organization-units")
  },
  createUser(value: AdminUserCreate) {
    return writeJson<AdminUser>("/api/v1/admin/users", value)
  },
  updateUser(id: string, value: AdminUserPatch) {
    return writeJson<AdminUser>(`/api/v1/admin/users/${encodeURIComponent(id)}`, value, "PATCH")
  },
  createOrganizationUnit(value: OrganizationUnitCreate) {
    return writeJson<OrganizationUnit>("/api/v1/admin/organization-units", value)
  },
  updateOrganizationUnit(id: string, value: OrganizationUnitPatch) {
    return writeJson<OrganizationUnit>(
      `/api/v1/admin/organization-units/${encodeURIComponent(id)}`,
      value,
      "PATCH",
    )
  },
  deleteOrganizationUnit(id: string) {
    return request<void>(`/api/v1/admin/organization-units/${encodeURIComponent(id)}`, {
      method: "DELETE",
    })
  },
}
