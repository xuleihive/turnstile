import type { GatewayApplicationList, GatewayApplicationSubscriptionCreate, GatewayApplicationSummary, GatewayProfile, GatewayReleaseOperation } from "../../data-sources/apim/types"

export const APPLICATION_OPERATION_PARAMETER = "applicationOperation"
export const APPLICATION_TERMINAL_STATUSES = ["succeeded", "failed", "restored"] as const

export function applicationOperationTerminal(operation: GatewayReleaseOperation | undefined) {
  return Boolean(operation && APPLICATION_TERMINAL_STATUSES.some((status) => status === operation.status))
}

export function applicationRetryDetail(operation: GatewayReleaseOperation | undefined) {
  const message = operation?.error_message
  if (!message || applicationOperationTerminal(operation)) return null
  const prefix = "Application data-plane authentication: "
  if (message.startsWith(prefix)) {
    try {
      const diagnostic: unknown = JSON.parse(message.slice(prefix.length))
      if (diagnostic && typeof diagnostic === "object" && "category" in diagnostic && diagnostic.category === "subscription_not_ready" && "status_code" in diagnostic && diagnostic.status_code === 401) return null
    } catch {}
  }
  return message
}

export function applicationOperationIdFromUrl(href: string) {
  const value = new URL(href).searchParams.get(APPLICATION_OPERATION_PARAMETER)
  return value && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value) ? value : null
}

export function applicationSubscriptionIdFromName(value: string) {
  return value.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 127).replace(/-+$/, "")
}

export function applicationProvisionBlocker(inventory: GatewayApplicationList | undefined) {
  if (!inventory) return "正在读取创建能力。"
  if (inventory.provisioning_available) return inventory.provisioning_options_supported
    ? null : "当前后端尚未支持创建时设置预算和标签。"
  const reason = inventory.provisioning_unavailable_reason
  if (reason === "Application subscription provisioning is not deployed.") return "创建服务尚未部署或启用。"
  if (reason === "Application subscription provisioning is not enabled in this environment.") return "此环境的订阅创建开关处于关闭状态。"
  if (reason === "Gateway Release Worker is not enabled.") return "后台 Release Worker 尚未启用。"
  if (reason === "APIM management target is not configured.") return "尚未配置 APIM 管理目标。"
  if (reason === "Credential encryption is unavailable.") return "后台密钥加密尚未就绪。"
  return reason ?? "当前后端尚未支持订阅创建，请部署并启用创建功能。"
}

export function applicationCreateError(
  gateway: GatewayProfile | undefined,
  value: GatewayApplicationSubscriptionCreate,
  inventory: GatewayApplicationSummary[],
) {
  if (!gateway?.enabled || gateway.implementation !== "apim") return "请选择可用的 APIM 网关。"
  if (!value.display_name.trim() || value.display_name.trim().length > 100) return "名称须为 1–100 个字符。"
  if (!/^[a-z0-9][a-z0-9-]{0,126}$/.test(value.subscription_id)) return "订阅 ID 只能包含小写字母、数字和连字符。"
  if (["master", "turnstile-dashboard", "turnstile-publisher-probe"].includes(value.subscription_id)) return "此订阅 ID 为系统保留，请使用其他 ID。"
  if ((value.description?.length ?? 0) > 1000) return "说明不能超过 1000 个字符。"
  if (inventory.some((item) => item.gateway_profile_id === gateway.id && item.slug.toLowerCase() === value.subscription_id)) return "此网关中已存在同名订阅 ID，请使用其他 ID。"
  if (value.budget_mode === "usd") {
    if (value.token_limit != null || !/^\d{1,16}(\.\d{1,2})?$/.test(value.usd_limit ?? "") || Number(value.usd_limit) <= 0) return "美元预算须为大于零的金额，最多两位小数。"
  } else if (value.usd_limit != null || (value.token_limit != null && (!Number.isSafeInteger(value.token_limit) || value.token_limit <= 0))) return "Token 预算须为大于零的整数。"
  if (value.tokens_per_minute != null && (!Number.isInteger(value.tokens_per_minute) || value.tokens_per_minute <= 0 || value.tokens_per_minute > 2_147_483_647)) return "TPM 须为 1 到 2147483647 的整数。"
  const tags = Object.entries(value.tags ?? {})
  if (tags.length > 32 || tags.some(([key, tag]) => !key.trim() || key.length > 128 || tag.length > 256)) return "标签最多 32 个，键须为 1 到 128 个字符，值最多 256 个字符。"
  return null
}

export function applicationTagsError(tags: Array<{ key: string; value: string }>) {
  const keys = tags.map(tag => tag.key.trim())
  if (keys.some(key => !key)) return "请填写标签键。"
  if (new Set(keys).size !== keys.length) return "标签键不能重复。"
  return null
}

export function provisionedApplicationId(operation: GatewayReleaseOperation | undefined) {
  if (operation?.operation_kind !== "application_provision" || operation.status !== "succeeded") return null
  const value = operation.checkpoint.application_id
  return typeof value === "string" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value) ? value : null
}

export function applicationProvisionStage(operation: GatewayReleaseOperation | undefined) {
  if (!operation) return "正在读取创建进度"
  if (operation.status === "failed" || operation.status === "restored") return "创建失败"
  if (operation.status === "succeeded") return "订阅已启用"
  if (operation.worker_available === false) return "创建未启动"
  if (operation.status === "queued") return "创建已排队"
  if (operation.status === "validating_dependencies") return "正在创建 APIM 订阅"
  if (operation.status === "promoting") return "正在配置订阅预算"
  return "正在验证订阅与预算"
}