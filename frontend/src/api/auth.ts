import { apiUrl, ApiError } from "./client"

export type SignInMethod = "password" | "entra"

export type AuthUser = {
  id: string
  name: string | null
  email: string
  role: "owner" | "member"
  method: SignInMethod
  avatar_url?: string | null
  /** Absent only while a newly built frontend is talking to a pre-expiry-field API. */
  session_expires_at?: string
}

async function readProfile(): Promise<AuthUser | null> {
  const response = await fetch(apiUrl("/api/v1/auth/me"), { credentials: "include", cache: "no-store" })
  if (response.status === 401) return null
  if (!response.ok) throw new ApiError(response.status, "无法读取账号信息。")
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
  return (await response.json()) as AuthUser
}

export const authApi = {
  profile: readProfile,
  avatar: async (url: string): Promise<Blob | null> => {
    const response = await fetch(apiUrl(url), { credentials: "include", cache: "no-store" })
    return response.ok ? response.blob() : null
  },
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
