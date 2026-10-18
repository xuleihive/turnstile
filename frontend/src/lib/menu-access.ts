import type { AuthUser } from "../api/auth"

/** Navigation only; API data scopes and APIM admission remain independently enforced. */
export function canAccessMenu(user: AuthUser | null, page: string): boolean {
  if (!user) return false
  if (user.role === "owner") return true
  return (user.menu_permissions ?? ["user-settings", "finops-invoke"]).includes(page)
}
