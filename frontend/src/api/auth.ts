import { apiUrl } from "./client"

export type SignInMethod = "password" | "entra"

export type AuthUser = {
  id: string
  name: string | null
  email: string
  role: string
  method: SignInMethod
  /** Effective permissions from /auth/me, used only to shape navigation. */
  permissions?: string[]
  /** Absent only while a newly built frontend is talking to a pre-expiry-field API. */
  session_expires_at?: string
}

async function readProfile(): Promise<AuthUser | null> {
  const response = await fetch(apiUrl("/api/v1/auth/me"), { credentials: "include" })
  if (!response.ok) return null
  return (await response.json()) as AuthUser
}

async function postIdentity(path: string, body: unknown): Promise<AuthUser> {
  const response = await fetch(apiUrl(path), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const detail = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(detail?.detail || "登录失败，请重试。")
  }
  const signedIn = (await response.json()) as AuthUser
  // Fetch the effective permissions before shaping management navigation. Every backend
  // route still checks authorization independently.
  return (await readProfile()) ?? signedIn
}

export const authApi = {
  profile: readProfile,
  signInWithPassword: (email: string, password: string) =>
    postIdentity("/api/v1/auth/login", { email, password }),
  exchangeEntraToken: (idToken: string) =>
    postIdentity("/api/v1/auth/entra", { id_token: idToken }),
  signOut: async () => {
    await fetch(apiUrl("/api/v1/auth/logout"), {
      method: "POST",
      credentials: "include",
    })
  },
}
