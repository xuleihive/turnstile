import type {
  AnomalyRule,
  AnomalyRuleWrite,
  AuditFinding,
  AuditStatus,
  BudgetScopeType,
  DistributionDimension,
  DistributionResponse,
  EnterpriseEntityCatalog,
  ExecutiveOverview,
  GatewayPublication,
  GatewayPublicationAccepted,
  GatewayPublicationCreate,
  GatewayPublicationList,
  GatewayReleaseDetail,
  GatewayReleaseDiff,
  GatewayReleaseIntegrity,
  GatewayReleaseList,
  GatewayReleaseOperation,
  GatewayReleaseOperationAccepted,
  GatewayReleaseProtection,
  GatewayReleaseProtectionWrite,
  GatewayReleaseRollbackPreview,
  GatewayBackendPoolConfig,
  GatewayBackendPoolWrite,
  GatewayApplicationAvatar,
  GatewayApplicationAvatarUpdate,
  GatewayApplicationBudgetUpdate,
  GatewayApplicationDetail,
  GatewayApplicationList,
  GatewayApplicationModelAccessUpdate,
  GatewayApplicationDepartmentUpdate,
  ConsoleMemberList,
  OrganizationDirectory,
  GatewayApplicationSubscriptionCreate,
  GatewayApplicationSubscriptionKeyKind,
  GatewayApplicationSubscriptionKeySecret,
  GatewayApplicationSubscriptionProvisionAccepted,
  MetricTotals,
  ModelConnectionCreate,
  ModelConnectionUpdate,
  ModelInvocationResponse,
  ImageInvocationRequest,
  ImageInvocationResponse,
  ModelRegistry,
  PriceCatalogModelsResponse,
  PriceCatalogOptionsResponse,
  PriceSyncResponse,
  OptimizationEvent,
  PeopleBudgetFilter,
  RunDetail,
  RuntimeHealth,
  TokenBudgetResponse,
  EnforcementMode,
  TokenBudgetBulkResult,
  TokenBudgetBulkWrite,
  TokenBudgetPeopleResponse,
  TokenBudgetWrite,
  TrafficGenerationPlan,
  TrafficGenerationRequest,
  TrafficGenerationResult,
  TrendApiResponse,
  TrendDimension,
  TrendMetric,
  UsageAnomaly,
  UsageFilters,
  UsageRequestDetail,
  UsageRequestSummary,
} from "./types"
import { getIntlLocale } from "../../locales/index"
import { ApiError, request, writeJson } from "../../api/client"
import { isUnsupportedOpenAICompatibleDetails } from "../../components/model-management/openai-compatible"

export { ApiError } from "../../api/client"

const DAY_MS = 24 * 60 * 60 * 1000

async function requireOpenAICompatibleApi<Value>(operation: Promise<Value>): Promise<Value> {
  try {
    return await operation
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 422) throw error
    const payloadStart = error.message.indexOf("{")
    if (payloadStart < 0) throw error
    let unsupported = false
    try {
      unsupported = isUnsupportedOpenAICompatibleDetails(JSON.parse(error.message.slice(payloadStart)))
    } catch {
      throw error
    }
    if (!unsupported) throw error
    throw new ApiError(422, "当前 API 尚未部署 OpenAI-compatible 连接支持。表单内容已保留，请在后端更新后重试。")
  }
}

export const usageWindow = (days: number, now = Date.now()): UsageFilters => {
  const to = new Date(now)
  const from = new Date(now - days * DAY_MS)
  return { from: from.toISOString(), to: to.toISOString() }
}

const params = (filters: UsageFilters, extra: Record<string, string | number> = {}) => {
  const values = new URLSearchParams({ from: filters.from, to: filters.to })
  for (const [key, value] of Object.entries({ ...filters, ...extra })) {
    if (key === "from" || key === "to" || value === undefined || value === "") continue
    // A repeated key, not a joined string: the API reads these as a list, and a runtime
    // display name can legitimately contain a comma.
    if (Array.isArray(value)) {
      for (const item of value) values.append(key, String(item))
      continue
    }
    values.set(key, String(value))
  }
  return values.toString()
}

const saveRegistry = <T,>(path: string, value: T, id?: string) =>
  writeJson<ModelRegistry>(`${path}${id ? `/${id}` : ""}`, value, id ? "PUT" : "POST")

// TODO: 疑似废弃补丁，待确认：仅在所有受支持环境均已应用 Cloud Code 清理迁移，且
// Registry API 不再可能返回这些历史 ID 后删除。
const LEGACY_CLOUD_CODE_PROVIDER_ID = "20000000-0000-4000-8000-000000000002"
const LEGACY_CLOUD_CODE_RUNTIME_ID = "30000000-0000-4000-8000-000000000002"
const RETIRED_LITELLM_GATEWAY_ID = "10000000-0000-4000-8000-000000000002"

function normalizeRegistry(registry: ModelRegistry): ModelRegistry {
  const runtimes = registry.runtimes.filter(
    (runtime) => runtime.id !== LEGACY_CLOUD_CODE_RUNTIME_ID
      && String(runtime.runtime_kind) !== "copilot_cli"
      && String(runtime.brand_key) !== "github",
  )
  const runtimeIds = new Set(runtimes.map((runtime) => runtime.id))
  return {
    backend_pool_session_affinity_supported: registry.backend_pool_session_affinity_supported === true,
    databricks_connections_supported: registry.databricks_connections_supported === true,
    databricks_oauth_supported: registry.databricks_oauth_supported === true,
    image_generation_supported: registry.image_generation_supported === true,
    image_configuration_defaults: registry.image_configuration_defaults,
    image_configuration_schema_version: registry.image_configuration_schema_version,
    gateways: registry.gateways.filter(
      (gateway) => gateway.id !== RETIRED_LITELLM_GATEWAY_ID,
    ),
    providers: registry.providers
      .filter((provider) => provider.id !== LEGACY_CLOUD_CODE_PROVIDER_ID
        && String(provider.provider_kind) !== "github"
        && String(provider.brand_key) !== "github")
      .map((provider) => ({
        ...provider,
        brand_key: provider.brand_key ?? "generic",
      })),
    runtimes: runtimes.map((runtime) => ({
      ...runtime,
      brand_key: runtime.brand_key ?? "generic",
    })),
    models: registry.models
      .filter((model) => runtimeIds.has(model.runtime_id)
        && String(model.family_key) !== "copilot")
      .map((model) => ({
        ...model,
        family_key: model.family_key ?? "generic",
        upstream_model_id: model.upstream_model_id ?? model.model_key,
        assignment_required: model.assignment_required ?? false,
        publication_id: model.publication_id ?? null,
      })),
  }
}

const normalizeGatewayPublication = (publication: GatewayPublication): GatewayPublication => ({
  ...publication,
  publication_kind: publication.publication_kind ?? "model_add",
  authorization: publication.authorization ?? null,
  retry_requires_credential: publication.retry_requires_credential ?? false,
  retry_can_authorize_image_probes: publication.retry_can_authorize_image_probes === true,
})

const normalizeGatewayPublicationAccepted = (
  accepted: GatewayPublicationAccepted,
): GatewayPublicationAccepted => ({
  ...accepted,
  publication: normalizeGatewayPublication(accepted.publication),
})

const normalizeEnterpriseUsers = (catalog: EnterpriseEntityCatalog): EnterpriseEntityCatalog => ({
  ...catalog,
  users: catalog.users.map((user) => {
    if (user.id.includes("@")) return user
    const legacy = /^user-(\d{2})$/.exec(user.id)
    if (!legacy) return user
    const email = `test.user${legacy[1]}@contoso.com`
    return { ...user, id: email, name: email }
  }),
})

const pivotTrend = (response: TrendApiResponse, metric: TrendMetric) => {
  const rows = new Map<string, Record<string, string | number>>()
  for (const point of response.points) {
    const day = new Intl.DateTimeFormat(getIntlLocale(), { month: "2-digit", day: "2-digit" }).format(new Date(point.bucket_start))
    const row = rows.get(point.bucket_start) ?? { day }
    row[point.label] = point.totals[metric]
    rows.set(point.bucket_start, row)
  }
  return [...rows.values()]
}

export const dataSource = {
  mode: "api" as const,
  defaultFilters: () => usageWindow(30),
  entities: () => request<EnterpriseEntityCatalog>("/api/v1/enterprise/entities").then(normalizeEnterpriseUsers),
  executiveOverview: (filters: UsageFilters) => request<ExecutiveOverview>(
    `/api/v1/observability/executive-overview?${params(filters)}`,
  ),
  distribution: (
    filters: UsageFilters,
    dimension: DistributionDimension,
    limit = 20,
    splitBy?: DistributionDimension,
  ) =>
    request<DistributionResponse>(
      `/api/v1/observability/distribution?${params(filters, { dimension, limit, ...(splitBy ? { split_by: splitBy } : {}) })}`,
    ),
  trendPoints: (
    filters: UsageFilters,
    groupBy: TrendDimension,
    interval: "hour" | "day" | "week" = "day",
    timezone = "UTC",
  ) => request<TrendApiResponse>(
    `/api/v1/observability/trends?${params(filters, { group_by: groupBy, interval, timezone })}`,
  ),
  trends: (groupBy: TrendDimension, metric: TrendMetric, filters = usageWindow(14)) =>
    dataSource.trendPoints(filters, groupBy).then((response) => pivotTrend(response, metric)),
  requests: (filters: UsageFilters, limit = 50) => request<{ items: UsageRequestSummary[] }>(
    `/api/v1/observability/requests?${params(filters, { limit })}`,
  ).then((value) => value.items),
  requestDetail: (requestId: string) => request<UsageRequestDetail>(
    `/api/v1/observability/requests/${encodeURIComponent(requestId)}`,
  ),
  anomalies: (filters: UsageFilters, limit = 50) => request<{ items: UsageAnomaly[] }>(
    `/api/v1/observability/anomalies?${params(filters, { limit })}`,
  ).then((value) => value.items),
  anomalyRules: () => request<{ items: AnomalyRule[] }>("/api/v1/anomaly-rules").then((value) => value.items),
  createAnomalyRule: (value: AnomalyRuleWrite) => writeJson<AnomalyRule>("/api/v1/anomaly-rules", value),
  updateAnomalyRule: (id: string, value: AnomalyRuleWrite) => writeJson<AnomalyRule>(`/api/v1/anomaly-rules/${encodeURIComponent(id)}`, value, "PUT"),
  deleteAnomalyRule: (id: string) => request<void>(`/api/v1/anomaly-rules/${encodeURIComponent(id)}`, { method: "DELETE" }),
  budgets: (period: string) => request<TokenBudgetResponse>(
    `/api/v1/budgets?period=${encodeURIComponent(period)}`,
  ).then((response) => {
    if (!Number.isInteger(response.risk_count) || !Array.isArray(response.risk_items)) {
      throw new ApiError(
        502,
        "Budget API contract mismatch: risk summary fields are missing",
      )
    }
    return response
  }),
  peopleBudgets: (
    period: string,
    departmentId: string,
    query: string,
    status: PeopleBudgetFilter,
    offset: number,
    limit: number,
  ) => request<TokenBudgetPeopleResponse>(
    `/api/v1/budgets/users?${new URLSearchParams({
      period,
      department_id: departmentId,
      query,
      status,
      offset: String(offset),
      limit: String(limit),
    })}`,
  ),
  bulkSavePeopleBudgets: (period: string, value: TokenBudgetBulkWrite) =>
    writeJson<TokenBudgetBulkResult>(
      `/api/v1/budgets/users/bulk?period=${encodeURIComponent(period)}`,
      value,
    ),
  saveBudget: (
    period: string,
    scopeType: BudgetScopeType,
    scopeId: string,
    value: TokenBudgetWrite,
  ) => writeJson<TokenBudgetResponse>(
    `/api/v1/budgets/${scopeType}/${encodeURIComponent(scopeId)}?period=${encodeURIComponent(period)}`,
    value,
    "PUT",
  ),
  deleteBudget: (period: string, scopeType: BudgetScopeType, scopeId: string) =>
    request<TokenBudgetResponse>(
      `/api/v1/budgets/${scopeType}/${encodeURIComponent(scopeId)}?period=${encodeURIComponent(period)}`,
      { method: "DELETE" },
    ),
  saveDepartmentEnforcement: (period: string, departmentId: string, mode: EnforcementMode) =>
    writeJson<TokenBudgetResponse>(
      `/api/v1/budgets/enforcement/${encodeURIComponent(departmentId)}?period=${encodeURIComponent(period)}`,
      { mode },
      "PUT",
    ),
  overview: () => request<{
    timezone: string
    top_workflows: Array<{ workflow: string; totals: MetricTotals; share_percent: number }>
  }>("/api/v1/observability/overview?timezone=Asia%2FShanghai"),
  runs: () => {
    const filters = usageWindow(30)
    return request<{ items: Array<{ run_id: string }> }>(
      `/api/v1/observability/runs?${params(filters)}`,
    ).then((value) => Promise.all(value.items.map((run) => request<RunDetail>(
      `/api/v1/observability/runs/${encodeURIComponent(run.run_id)}`,
    ))))
  },
  findings: () => {
    const filters = usageWindow(30)
    return request<{ items: AuditFinding[] }>(
      `/api/v1/observability/audit-findings?${params(filters)}`,
    ).then((value) => value.items)
  },
  optimizations: () => request<{ items: OptimizationEvent[] }>(
    "/api/v1/observability/optimization-events",
  ).then((value) => value.items),
  updateFinding: (id: string, status: AuditStatus) => writeJson<AuditFinding>(
    `/api/v1/observability/audit-findings/${id}`, { status }, "PATCH",
  ),
  createOptimization: (value: {
    workflow: string; label: string; notes: string; occurred_at: string; comparison_window_days: number
  }) => writeJson<OptimizationEvent>("/api/v1/observability/optimization-events", value),
  registry: () => request<ModelRegistry>("/api/v1/model-management").then(normalizeRegistry),
  saveGateway: (value: Record<string, unknown>, id?: string) => saveRegistry("/api/v1/model-management/gateways", value, id),
  deleteGateway: (id: string) => request<ModelRegistry>(
    `/api/v1/model-management/gateways/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  ).then(normalizeRegistry),
  saveProvider: (value: Record<string, unknown>, id?: string) => saveRegistry("/api/v1/model-management/providers", value, id),
  saveRuntime: (value: Record<string, unknown>, id?: string) => saveRegistry("/api/v1/model-management/runtimes", value, id),
  saveConnection: (value: ModelConnectionCreate) => requireOpenAICompatibleApi(
    writeJson<ModelRegistry>("/api/v1/model-management/connections", value),
  ).then(normalizeRegistry),
  updateConnection: (id: string, value: ModelConnectionUpdate) => writeJson<ModelRegistry>(
    `/api/v1/model-management/connections/${encodeURIComponent(id)}`,
    value,
    "PUT",
  ).then(normalizeRegistry),
  adoptDatabricksConnection: (id: string, workspaceUrl: string) => writeJson<GatewayPublicationAccepted>(
    `/api/v1/model-management/connections/${encodeURIComponent(id)}/adopt`,
    { workspace_url: workspaceUrl },
  ).then(normalizeGatewayPublicationAccepted),
  deleteConnection: (id: string) => request<ModelRegistry>(
    `/api/v1/model-management/connections/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  ).then(normalizeRegistry),
  saveModel: (value: Record<string, unknown>, id?: string) => saveRegistry("/api/v1/model-management/models", value, id),
  priceCatalogModels: (query: string) => request<PriceCatalogModelsResponse>(
    `/api/v1/model-management/price-catalog/models?q=${encodeURIComponent(query)}`,
  ),
  priceCatalogOptions: (modelKey: string) => request<PriceCatalogOptionsResponse>(
    `/api/v1/model-management/price-catalog/options?model=${encodeURIComponent(modelKey)}`,
  ),
  // Omitting the ids syncs everything that follows a list price. Passing them is how the
  // price table's row selection turns into "sync just these".
  syncPrices: (modelIds?: string[]) => writeJson<PriceSyncResponse>(
    "/api/v1/model-management/price-sync",
    { model_ids: modelIds ?? null },
  ).then((result) => ({ ...result, registry: normalizeRegistry(result.registry) })),
  deleteModel: (id: string) => request<GatewayPublicationAccepted>(
    `/api/v1/model-management/models/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  ).then(normalizeGatewayPublicationAccepted),
  modelBackendPool: (id: string) => request<GatewayBackendPoolConfig>(
    `/api/v1/model-management/models/${encodeURIComponent(id)}/backend-pool`,
  ).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 404) return null
    throw error
  }),
  saveModelBackendPool: (id: string, value: GatewayBackendPoolWrite) =>
    writeJson<GatewayPublicationAccepted>(
      `/api/v1/model-management/models/${encodeURIComponent(id)}/backend-pool`,
      value,
      "PUT",
    ).then(normalizeGatewayPublicationAccepted),
  deleteModelBackendPool: (id: string) => request<GatewayPublicationAccepted>(
    `/api/v1/model-management/models/${encodeURIComponent(id)}/backend-pool`,
    { method: "DELETE" },
  ).then(normalizeGatewayPublicationAccepted),
  publishModel: (value: GatewayPublicationCreate) => requireOpenAICompatibleApi(
    writeJson<GatewayPublicationAccepted>("/api/v1/model-management/publications", value),
  ).then(normalizeGatewayPublicationAccepted),
  gatewayPublication: (id: string) => request<GatewayPublication>(
    `/api/v1/model-management/publications/${encodeURIComponent(id)}`,
  ).then(normalizeGatewayPublication),
  gatewayPublications: () => request<GatewayPublicationList>(
    "/api/v1/model-management/publications?limit=20",
  ).then((value) => ({
    ...value,
    items: value.items.map(normalizeGatewayPublication),
  })),
  gatewayReleases: () => request<GatewayReleaseList>(
    "/api/v1/model-management/releases?limit=100",
  ),
  gatewayRelease: (id: string) => request<GatewayReleaseDetail>(
    `/api/v1/model-management/releases/${encodeURIComponent(id)}`,
  ),
  gatewayReleaseDiff: (id: string, againstId?: string) => request<GatewayReleaseDiff>(
    `/api/v1/model-management/releases/${encodeURIComponent(id)}/diff${againstId ? `?against_release_id=${encodeURIComponent(againstId)}` : ""}`,
  ),
  gatewayReleaseIntegrity: (id: string) => request<GatewayReleaseIntegrity>(
    `/api/v1/model-management/releases/${encodeURIComponent(id)}/integrity`,
  ),
  protectGatewayRelease: (id: string, value: GatewayReleaseProtectionWrite) =>
    writeJson<GatewayReleaseProtection>(
      `/api/v1/model-management/releases/${encodeURIComponent(id)}/protection`,
      value,
      "PUT",
    ),
  unprotectGatewayRelease: (id: string) => request<void>(
    `/api/v1/model-management/releases/${encodeURIComponent(id)}/protection`,
    { method: "DELETE" },
  ),
  requestGatewayReleaseIntegrity: (id: string) =>
    request<GatewayReleaseOperationAccepted>(
      `/api/v1/model-management/releases/${encodeURIComponent(id)}/integrity-checks`,
      { method: "POST" },
    ),
  gatewayReleaseRollbackPreview: (id: string) => request<GatewayReleaseRollbackPreview>(
    `/api/v1/model-management/releases/${encodeURIComponent(id)}/rollback-preview`,
  ),
  requestGatewayReleaseRollback: (id: string, confirmationSha256: string) =>
    writeJson<GatewayReleaseOperationAccepted>(
      `/api/v1/model-management/releases/${encodeURIComponent(id)}/rollback`,
      { confirmation_sha256: confirmationSha256 },
    ),
  gatewayReleaseOperation: (id: string) => request<GatewayReleaseOperation>(
    `/api/v1/model-management/release-operations/${encodeURIComponent(id)}`,
  ),
  requestGatewayReleaseGcPlan: (gatewayId: string) =>
    request<GatewayReleaseOperationAccepted>(
      `/api/v1/model-management/gateways/${encodeURIComponent(gatewayId)}/gc-plans`,
      { method: "POST" },
    ),
  gatewayApplications: () => request<GatewayApplicationList>(
    "/api/v1/application-access/applications",
  ),
  gatewayApplication: (id: string) => request<GatewayApplicationDetail>(
    `/api/v1/application-access/applications/${encodeURIComponent(id)}`,
  ),
  gatewayApplicationUsageActivity: (
    id: string,
    filters: UsageFilters,
    interval: "day" | "week",
    timezone: string,
  ) => request<TrendApiResponse>(
    `/api/v1/application-access/applications/${encodeURIComponent(id)}/usage-activity?${params(filters, { interval, timezone })}`,
  ),
  updateGatewayApplicationAvatar: (
    id: string,
    value: GatewayApplicationAvatarUpdate,
  ) => writeJson<GatewayApplicationAvatar>(
    `/api/v1/application-access/applications/${encodeURIComponent(id)}/avatar`,
    value,
    "PUT",
  ),
  updateGatewayApplicationBudget: (
    id: string,
    value: GatewayApplicationBudgetUpdate,
  ) => writeJson<GatewayApplicationDetail>(
    `/api/v1/application-access/applications/${encodeURIComponent(id)}/budget`,
    value,
    "PUT",
  ),
  updateGatewayApplicationModelAccess: (
    id: string,
    value: GatewayApplicationModelAccessUpdate,
  ) => writeJson<GatewayApplicationDetail>(
    `/api/v1/application-access/applications/${encodeURIComponent(id)}/model-access`,
    value,
    "PUT",
  ),
  updateGatewayApplicationDepartment: (
    id: string,
    value: GatewayApplicationDepartmentUpdate,
  ) => writeJson<GatewayApplicationDetail>(
    `/api/v1/application-access/applications/${encodeURIComponent(id)}/department`,
    value,
    "PUT",
  ),
  updateGatewayApplicationOwner: (
    id: string,
    value: { owner_id: string | null },
  ) => writeJson<GatewayApplicationDetail>(
    `/api/v1/application-access/applications/${encodeURIComponent(id)}/owner`,
    value,
    "PUT",
  ),
  updateGatewayApplicationDepartmentBulk: (value: {
    application_ids: string[]
    department_id: string | null
  }) => writeJson<{ updated: number; unchanged: number }>(
    "/api/v1/application-access/applications/bulk-department",
    value,
    "PUT",
  ),
  organizationDirectory: () =>
    request<OrganizationDirectory>("/api/v1/organization/directory"),
  createDepartment: (value: { id: string; display_name: string }) =>
    writeJson<OrganizationDirectory>("/api/v1/organization/departments", value, "POST"),
  renameOrgUnit: (unitId: string, displayName: string) =>
    writeJson<OrganizationDirectory>(
      `/api/v1/organization/units/${encodeURIComponent(unitId)}/name`,
      { display_name: displayName },
      "PUT",
    ),
  setOrgUnitStatus: (unitId: string, status: "active" | "retired") =>
    writeJson<OrganizationDirectory>(
      `/api/v1/organization/units/${encodeURIComponent(unitId)}/status`,
      { status },
      "PUT",
    ),
  consoleMembers: () => request<ConsoleMemberList>("/api/v1/organization/members"),
  setConsoleMemberRole: (email: string, role: "owner" | "member") =>
    writeJson<ConsoleMemberList>(
      `/api/v1/organization/members/${encodeURIComponent(email)}/role`,
      { role },
      "PUT",
    ),
  setConsoleMemberEnabled: (email: string, enabled: boolean) =>
    writeJson<ConsoleMemberList>(
      `/api/v1/organization/members/${encodeURIComponent(email)}/status`,
      { enabled },
      "PUT",
    ),
  syncGatewayApplications: (gatewayId: string) =>
    request<GatewayReleaseOperationAccepted>(
      `/api/v1/application-access/gateways/${encodeURIComponent(gatewayId)}/sync`,
      { method: "POST" },
    ),
  provisionGatewayApplicationSubscription: (
    gatewayId: string,
    value: GatewayApplicationSubscriptionCreate,
  ) => writeJson<GatewayApplicationSubscriptionProvisionAccepted>(
    `/api/v1/application-access/gateways/${encodeURIComponent(gatewayId)}/subscriptions`,
    value,
  ),
  revealGatewayApplicationSubscriptionKey: (
    applicationId: string,
    applicationSubscriptionId: string,
    keyKind: GatewayApplicationSubscriptionKeyKind,
  ) => writeJson<GatewayApplicationSubscriptionKeySecret>(
    `/api/v1/application-access/applications/${encodeURIComponent(applicationId)}`
      + `/subscriptions/${encodeURIComponent(applicationSubscriptionId)}`
      + `/keys/${keyKind}/reveal`,
    {},
  ),
  rotateGatewayApplicationSubscriptionKey: (
    applicationId: string,
    applicationSubscriptionId: string,
    keyKind: GatewayApplicationSubscriptionKeyKind,
    confirmation: string,
  ) => writeJson<void>(
    `/api/v1/application-access/applications/${encodeURIComponent(applicationId)}`
      + `/subscriptions/${encodeURIComponent(applicationSubscriptionId)}`
      + `/keys/${keyKind}/rotate`,
    { confirmation },
  ),
  retryGatewayPublication: (id: string, credential?: string, authorizeImageProbes = false, kind: "api_key" | "oauth_m2m" = "api_key") => writeJson<GatewayPublication>(
    `/api/v1/model-management/publications/${encodeURIComponent(id)}/retry`,
    {
      ...(credential ? kind === "oauth_m2m" ? { oauth_client_secret: credential } : { api_key: credential } : {}),
      ...(authorizeImageProbes === true ? { authorize_image_probes: true } : {}),
    },
  ).then(normalizeGatewayPublication),
  resumeGatewayPublicationAuthorization: (id: string) => writeJson<GatewayPublication>(
    `/api/v1/model-management/publications/${encodeURIComponent(id)}/authorization/resume`,
    {},
  ).then(normalizeGatewayPublication),
  rotateGatewayCredential: (gatewayId: string, modelKey: string, credential: string, kind: "api_key" | "oauth_m2m" = "api_key") =>
    writeJson<GatewayPublication>(
      `/api/v1/model-management/gateways/${encodeURIComponent(gatewayId)}/credentials/rotate`,
      { model_key: modelKey, ...(kind === "oauth_m2m" ? { oauth_client_secret: credential } : { api_key: credential }) },
    ).then(normalizeGatewayPublication),
  checkRuntime: (id: string) => request<RuntimeHealth>(
    `/api/v1/model-management/runtimes/${id}/check`, { method: "POST" },
  ),
  invokeModel: (value: Record<string, unknown>) => writeJson<ModelInvocationResponse>(
    "/api/v1/model-gateway/invoke", value,
  ),
  generateImage: (value: ImageInvocationRequest) => writeJson<ImageInvocationResponse>(
    "/api/v1/model-gateway/images/generations", value,
  ),
  planTraffic: (value: TrafficGenerationRequest) => writeJson<TrafficGenerationPlan>(
    "/api/v1/traffic/plan", value,
  ),
  executeTraffic: (value: TrafficGenerationRequest) => writeJson<TrafficGenerationResult>(
    "/api/v1/traffic/execute", value,
  ),
}