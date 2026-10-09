import { type AuthUser } from "./auth"
import { apiUrl, ApiError, SESSION_EXPIRED_EVENT } from "./client"

export type PersonalAvatar = { avatar_url: string | null; updated_at: string | null }
export type AccountInformation = {
  account_id: string
  permission_groups: Array<{ key: "owner" | "member"; name: string; source: "turnstile" }>
  organization: { id: string; name: string } | null
  department: { id: string; name: string } | null
  membership_source: "catalog" | "observed_usage" | "owner_default" | "unlinked"
  editable: { avatar: boolean; display_name: boolean; password: boolean }
  generated_at: string
}
export type PersonalBudget = {
  period: string; period_start: string; period_end: string; min_period: string; max_period: string
  token_limit: number | null; used_tokens: number; remaining_tokens: number | null
  warning_threshold_percent: number | null; usage_percent: number | null
  forecast_tokens: number; forecast_percent: number | null
  status: "unallocated" | "healthy" | "warning" | "exceeded"
  enforcement_mode: "audit" | "block" | null
  enforcement_department_id: string | null
  basis: "budget_evidence"; generated_at: string
}
export type PersonalModelList = {
  policy_state: "unconfigured" | "assigned" | "deny_all"
  items: Array<{
    id: string; name: string; model_key: string; provider_name: string
    context_window: number | null; capabilities: string[]
    access_source: "explicit" | "legacy_compatibility"
    runtime_status: "available" | "unavailable" | "unknown"
  }>
  generated_at: string
}
export type PersonalUsageTotals = {
  requests: number; input_tokens: number; output_tokens: number
  cache_read_tokens: number; cache_write_tokens: number; total_tokens: number
  error_rate: number | null; average_latency_ms: number | null
  estimated_cost_usd: number | null; cost_state: "priced" | "partial" | "unpriced"
}
export type PersonalUsage = {
  period: string; from: string; to: string; interval: "hour" | "day"; timezone: string
  usage_domain: "apim"; totals: PersonalUsageTotals
  points: Array<{ bucket_start: string; totals: PersonalUsageTotals }>
  models: Array<{ model_id: string; model_name: string; totals: PersonalUsageTotals }>
  generated_at: string; last_observed_at: string | null; demo: boolean
}

async function personalRequest<T>(path: string, value?: unknown, method = "GET"): Promise<T> {
  const response = await fetch(apiUrl(`/api/v1/user-settings/me/${path}`), {
    method, credentials: "include", cache: "no-store",
    ...(value === undefined ? {} : {
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
    }),
  })
  if (response.status === 401) window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
  if (!response.ok) {
    const body = await response.json().catch(() => null) as {
      detail?: string | Array<{ msg: string }>
    } | null
    const detail = Array.isArray(body?.detail)
      ? body.detail.map((item) => item.msg).join(" ")
      : body?.detail
    throw new ApiError(response.status, detail || "无法读取账号信息。")
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>
}

export const userSettingsApi = {
  account: () => personalRequest<AccountInformation>("account"),
  budget: (period: string) => personalRequest<PersonalBudget>(`budget?${new URLSearchParams({ period })}`),
  models: () => personalRequest<PersonalModelList>("models"),
  usage: (period: string, interval: "hour" | "day", timezone: string) =>
    personalRequest<PersonalUsage>(`usage?${new URLSearchParams({ period, interval, timezone })}`),
  updateName: (display_name: string | null) =>
    personalRequest<AuthUser>("profile", { display_name }, "PATCH"),
  updateAvatar: (avatar_data_url: string | null) =>
    personalRequest<PersonalAvatar>("avatar", { avatar_data_url }, "PUT"),
  changePassword: (current_password: string, new_password: string, confirm_password: string) =>
    personalRequest<void>("password", { current_password, new_password, confirm_password }, "POST"),
}
