import type { GatewayApplicationList, GatewayApplicationSubscriptionCreate, GatewayApplicationSummary, GatewayProfile, GatewayReleaseOperation } from "../../data-sources/apim/types"

export const APPLICATION_OPERATION_PARAMETER = "applicationOperation"

export function applicationOperationTerminal(operation: GatewayReleaseOperation | undefined) {
  return Boolean(operation && ["succeeded", "failed", "restored"].includes(operation.status))
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
  if (inventory.provisioning_available) return null
  const reason = inventory.provisioning_unavailable_reason
  if (reason === "Application provisioning requires its worker, ledger, encryption and APIM target.") return "创建所需的 Worker、账本、加密或 APIM 配置尚未就绪。"
  if (reason === "Application subscription provisioning is not deployed.") return "创建服务尚未部署或启用。"
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
  if (value.subscription_id === "master") return "此订阅 ID 为系统保留，请使用其他 ID。"
  if ((value.description?.length ?? 0) > 1000) return "说明不能超过 1000 个字符。"
  if (inventory.some((item) => item.gateway_profile_id === gateway.id && item.slug.toLowerCase() === value.subscription_id)) return "此网关中已存在同名订阅 ID，请使用其他 ID。"
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
  if (operation.status === "promoting") return "正在配置应用和预算"
  return "正在验证订阅与预算"
}