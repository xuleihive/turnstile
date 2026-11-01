const baseUrl = import.meta.env.VITE_API_BASE_URL ?? ""

export const apiUrl = (path: string) => `${baseUrl}${path}`

/** An HTTP failure, with the status kept rather than flattened into the message.
 *
 * The status decides whether retrying is useful: a 404 or 409 will not become a 200 by
 * asking again, so retry policies should not have to parse a human-readable message. */
export class ApiError extends Error {
  constructor(readonly status: number, message: string, readonly detail?: string) {
    super(message)
    this.name = "ApiError"
  }
}

export const SESSION_EXPIRED_EVENT = "turnstile:session-expired"

async function responseErrorDetail(response: Response, path: string) {
  const text = await response.text()
  if (!text) return path
  try {
    const body = JSON.parse(text) as { detail?: unknown }
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail
  } catch {}
  return text
}

async function applicationSessionExpired() {
  try {
    const profile = await fetch(apiUrl("/api/v1/auth/me"), {
      credentials: "include",
      cache: "no-store",
    })
    return profile.status === 401
  } catch {
    // A network outage is not evidence that the session expired.
    return false
  }
}

export const request = async <T,>(
  path: string,
  init?: RequestInit,
): Promise<T> => {
  const response = await fetch(apiUrl(path), {
    credentials: "include",
    cache: "no-store",
    ...init,
    headers: { ...init?.headers },
  })
  if (response.status === 401 && await applicationSessionExpired()) {
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
  }
  if (!response.ok) {
    const detail = await responseErrorDetail(response, path)
    throw new ApiError(response.status, `API ${response.status}: ${detail}`, detail)
  }
  if (response.status === 204) return undefined as T
  const contentType = response.headers.get("content-type") ?? ""
  if (!contentType.includes("application/json")) {
    throw new ApiError(
      response.status,
      `API ${response.status} returned ${contentType || "an unknown content type"} for ${path}`,
    )
  }
  return response.json() as Promise<T>
}

export async function requestSensitiveJson<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init, credentials: "include", cache: "no-store", redirect: "error",
  })
  if (!response.ok || !response.headers.get("cache-control")?.includes("no-store")
    || !response.headers.get("content-type")?.includes("application/json")) {
    await response.body?.cancel()
    throw new ApiError(response.status, "Sensitive configuration unavailable")
  }
  try { return await response.json() as T }
  catch { throw new ApiError(response.status, "Sensitive configuration unavailable") }
}

export const writeJson = <T,>(path: string, value: unknown, method = "POST") => request<T>(path, {
  method,
  headers: { "content-type": "application/json" },
  body: JSON.stringify(value),
})