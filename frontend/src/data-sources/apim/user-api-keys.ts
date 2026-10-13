import { request, requestBlob, writeJson } from "../../api/client"
import type { GatewayReleaseOperationAccepted, GatewayReleaseOperation } from "./types"

export type UserSubscription = {
  id: string
  application_id: string
  application_display_name: string
  application_type: "service" | "agent"
  apim_subscription_id: string
  display_name: string
  scope_id: string
  state: "active" | "suspended" | "cancelled"
  source: string
  created_at: string
}

export type UserSubscriptionList = {
  user_id: string
  email: string
  display_name: string | null
  role: string
  items: UserSubscription[]
}

export type ApiKeySecret = { key_kind: "primary" | "secondary"; value: string }

export type ApiKeyExportFilter = {
  user_ids?: string[]
  department_id?: string
  subscription_ids?: string[]
}

export const userApiKeyQueries = {
  subscriptions: (userId: string | null) => ["user-api-keys", "subscriptions", userId] as const,
  operation: (operationId: string | null) => ["user-api-keys", "operation", operationId] as const,
}

export const userApiKeysApi = {
  listMine() {
    return request<UserSubscriptionList>("/api/v1/application-access/me/subscriptions")
  },
  listUser(userId: string) {
    return request<UserSubscriptionList>(`/api/v1/admin/users/${encodeURIComponent(userId)}/subscriptions`)
  },
  createMine(value: { display_name: string; description?: string; application_type?: "service" | "agent" }) {
    return writeJson<GatewayReleaseOperationAccepted>("/api/v1/application-access/me/subscriptions", value)
  },
  createForUser(userId: string, value: { display_name: string; description?: string; application_type?: "service" | "agent" }) {
    return writeJson<GatewayReleaseOperationAccepted>(`/api/v1/admin/users/${encodeURIComponent(userId)}/subscriptions`, value)
  },
  operation(operationId: string) {
    return request<GatewayReleaseOperation>(`/api/v1/model-management/release-operations/${encodeURIComponent(operationId)}`)
  },
  reveal(applicationId: string, subscriptionId: string, kind: "primary" | "secondary") {
    return request<ApiKeySecret>(
      `/api/v1/application-access/applications/${encodeURIComponent(applicationId)}/subscriptions/${encodeURIComponent(subscriptionId)}/keys/${kind}/reveal`,
      { method: "POST" },
    )
  },
  rotate(applicationId: string, subscriptionId: string, kind: "primary" | "secondary", confirmation: string) {
    return writeJson<void>(
      `/api/v1/application-access/applications/${encodeURIComponent(applicationId)}/subscriptions/${encodeURIComponent(subscriptionId)}/keys/${kind}/rotate`,
      { confirmation },
    )
  },
  cancel(applicationId: string, subscriptionId: string, confirmation: string) {
    return writeJson<void>(
      `/api/v1/application-access/applications/${encodeURIComponent(applicationId)}/subscriptions/${encodeURIComponent(subscriptionId)}/cancel`,
      { confirmation },
    )
  },
  export(filter: ApiKeyExportFilter) {
    return requestBlob("/api/v1/admin/subscriptions/keys/export", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(filter),
    })
  },
}
