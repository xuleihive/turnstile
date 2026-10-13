import { request, requestBlob } from "../../api/client"

export type SecurityAuditResult = "success" | "failure" | "denied"

export type SecurityAuditEvent = {
  id: string
  created_at: string
  actor_user_id: string | null
  actor_email: string | null
  actor_role: string | null
  action: string
  resource_type: string
  resource_id: string | null
  result: SecurityAuditResult
  request_id: string | null
  correlation_id: string | null
  client_ip: string | null
  user_agent: string | null
  before_data: Record<string, unknown>
  after_data: Record<string, unknown>
}

export type SecurityAuditEventPage = {
  items: SecurityAuditEvent[]
  next_cursor: string | null
}

export type SecurityAuditFilters = {
  from?: string
  to?: string
  actor_id?: string
  action?: string
  result?: SecurityAuditResult
}

function queryString(filters: SecurityAuditFilters, extras: Record<string, string> = {}) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries({ ...filters, ...extras })) {
    if (value) params.set(key, value)
  }
  return params.toString()
}

export function listSecurityAuditEvents(
  filters: SecurityAuditFilters,
  cursor: string | null,
) {
  const query = queryString(filters, { limit: "100", ...(cursor ? { cursor } : {}) })
  return request<SecurityAuditEventPage>(`/api/v1/security/audit-events?${query}`)
}

export function exportSecurityAuditEvents(filters: SecurityAuditFilters) {
  const query = queryString(filters, { limit: "10000" })
  return requestBlob(`/api/v1/security/audit-events/export?${query}`)
}

export function downloadSecurityAuditCsv(blob: Blob) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = "turnstile_security_audit.csv"
  anchor.click()
  URL.revokeObjectURL(url)
}
