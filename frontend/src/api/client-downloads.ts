import { apiUrl, ApiError, request, SESSION_EXPIRED_EVENT } from "./client"

export type ClientSubscription = {
  application_id: string; application_name: string; subscription_id: string; subscription_name: string
  gateway_id: string; gateway_name: string; base_url: string
  models: Array<{ id: string; name: string; protocol: string; context_window: number | null; capabilities: string[] }>
}
export type ClientDownloadOptions = {
  items: ClientSubscription[]; key_management_available: boolean; unavailable_reason: string | null; plugin_version: string
}
export type ClientPluginDownload = { blob: Blob; filename: string }

function downloadFilename(response: Response, prefix: string, fallback: string): string {
  const disposition = response.headers.get("content-disposition") ?? ""
  const encoded = /(?:^|;)\s*filename\*=UTF-8'[^']*'([^;]+)/i.exec(disposition)?.[1]
  const plain = /(?:^|;)\s*filename="([^"]+)"/i.exec(disposition)?.[1]
  let decoded: string | undefined
  try { if (encoded) decoded = decodeURIComponent(encoded.trim()) } catch { /* Use ASCII fallback. */ }
  for (const name of [decoded, plain]) {
    if (name?.startsWith(`${prefix}-`) && name.endsWith(".zip") && name.length <= 180
      && !/[\p{C}/\\:*?"<>|]/u.test(name)) return name
  }
  return `${prefix}-${fallback}.zip`
}

export const clientDownloadsApi = {
  userOptions: (signal?: AbortSignal) => request<{ enabled: boolean; budget_configured: boolean; plugin_version: string; user_name: string; default_subscription?: ClientSubscription; unavailable_reason?: string }>("/api/v1/client-downloads/user-options", { signal }),
  async downloadUser(signal?: AbortSignal): Promise<ClientPluginDownload> {
    const response = await fetch(apiUrl("/api/v1/client-downloads/magpie-user"), {
      method: "POST", credentials: "include", cache: "no-store", signal,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ acknowledged: true }),
    })
    if (response.status === 401) window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
    if (!response.ok) {
      const body = await response.json().catch(() => null) as { detail?: string } | null
      throw new ApiError(response.status, typeof body?.detail === "string" ? body.detail : "用户版下载失败，请重试。")
    }
    if (!response.headers.get("content-type")?.includes("application/zip")) throw new Error("下载失败，请重试。")
    return { blob: await response.blob(), filename: downloadFilename(response, "aigateway-proxy-user", "user") }
  },
  options: (signal?: AbortSignal) => request<ClientDownloadOptions>("/api/v1/client-downloads/options", { signal }),
  async download(item: ClientSubscription, keyKind: "primary" | "secondary", signal?: AbortSignal): Promise<ClientPluginDownload> {
    const response = await fetch(apiUrl("/api/v1/client-downloads/magpie"), {
      method: "POST", credentials: "include", cache: "no-store", signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ application_id: item.application_id, subscription_id: item.subscription_id,
        gateway_id: item.gateway_id, key_kind: keyKind }),
    })
    if (response.status === 401) window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
    if (!response.ok) {
      const body = await response.json().catch(() => null) as { detail?: string } | null
      throw new ApiError(response.status, typeof body?.detail === "string" ? body.detail : "下载失败，请重试。")
    }
    if (!response.headers.get("content-type")?.includes("application/zip")) throw new Error("下载失败，请重试。")
    return { blob: await response.blob(), filename: downloadFilename(response, "aigateway-proxy", "subscription") }
  },
}
