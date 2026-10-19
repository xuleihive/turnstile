import type {
  ApimOnboardingOperations,
  ApimOnboardingPlan,
  ApimOnboardingRequest,
  AnomalyRule,
  AnomalyRuleWrite,
  AuditFinding,
  AuditStatus,
  ApimTargetTopologyList,
  BudgetDelegationReadiness,
  BudgetExecutionVerification,
  BudgetScopeType,
  DistributionDimension,
  DistributionResponse,
  EnterpriseEntityCatalog,
  ExecutiveOverview,
  GatewayPublication,
  GatewayPublicationAccepted,
  GatewayPublicationCreate,
  GatewayPublicationList,
  GatewayProgramUpgradePreview,
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
  GatewayApplicationSubscriptionCreate,
  GatewayApplicationSubscriptionKeyKind,
  GatewayApplicationSubscriptionKeySecret,
  GatewayApplicationSubscriptionProvisionAccepted,
  MetricTotals,
  ModelAvailabilityAccepted,
  ModelAvailabilityCommand,
  ModelAvailabilityList,
  ModelConnectionCreate,
  ModelConnectionUpdate,
  ModelInvocationResponse,
  DecisionInvocationRequest,
  DecisionInvocationResponse,
  ImageInvocationRequest,
  ImageInvocationResponse,
  ModelRegistry,
  PriceCatalogModel,
  PricePreviewRequest,
  PricePreviewResponse,
  MultiTargetPublicationAccepted,
  MultiTargetPublicationCreate,
  MultiTargetRollout,
  MultiTargetRolloutList,








  OptimizationEvent,
  PeopleBudgetFilter,
  RunDetail,
  RuntimeCheckPlan,
  RuntimeCheckSelection,
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
  UsageRequestPage,
  UsageRequestSummary,
  PriceCatalogModelsResponse,
  PriceCatalogOptionsResponse,
  PriceSyncResponse,
} from "./types"
import { getIntlLocale } from "../../locales/index"
import { ApiError, request, requestSensitiveJson, writeJson } from "../../api/client"

export { ApiError } from "../../api/client"

export async function requestGatewayBootstrap(
  deploymentId: string, value: import("./types").SelfHostedGatewayBootstrapRequest,
  signal: AbortSignal,
): Promise<import("./types").SelfHostedGatewayBootstrap> {
  return requestSensitiveJson(`/api/v1/model-management/self-hosted-gateways/${encodeURIComponent(deploymentId)}/bootstrap`, {
    method: "POST", signal,
    headers: { "content-type": "application/json" }, body: JSON.stringify(value),
  })
}

const DAY_MS = 24 * 60 * 60 * 1000

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
    .then(normalizeRegistry)

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
    ...registry,
    gateway_name_edit_supported: registry.gateway_name_edit_supported === true,
    context_pricing_supported: registry.context_pricing_supported === true,
    context_price_rules_supported: registry.context_price_rules_supported === true,
    backend_pool_session_affinity_supported: registry.backend_pool_session_affinity_supported === true,
    image_generation_supported: registry.image_generation_supported === true,
    apim_target_topology_available: registry.apim_target_topology_available === true,
    apim_target_topology: registry.apim_target_topology_available === true
      ? registry.apim_target_topology
        ? normalizeApimTargetTopologies(registry.apim_target_topology)
        : registry.apim_target_topology
      : null,
    self_hosted_gateway_enabled: registry.self_hosted_gateway_enabled === true,
    multi_apim_target_rollout_enabled: registry.multi_apim_target_rollout_enabled === true,
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
      price_discount_percent: runtime.price_discount_percent ?? null,
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
        price_source: model.price_source ?? "manual",
        price_reference: model.price_reference ?? null,
        price_discount_percent: model.price_discount_percent ?? null,
        effective_discount_percent: model.effective_discount_percent ?? null,
        list_input_cost_per_million: model.list_input_cost_per_million ?? null,
        list_output_cost_per_million: model.list_output_cost_per_million ?? null,
        list_cached_cost_per_million: model.list_cached_cost_per_million ?? null,
        list_cache_write_cost_per_million: model.list_cache_write_cost_per_million ?? null,
        price_synced_at: model.price_synced_at ?? null,
        price_sync_status: model.price_sync_status ?? null,
        price_sync_message: model.price_sync_message ?? null,
        pending_list_price: model.pending_list_price ?? null,
        context_price_references: model.context_price_references ?? [],
        context_price_tiers: model.context_price_tiers ?? [],
        context_price_rule: model.context_price_rule ?? null,
        pending_context_price_tiers: model.pending_context_price_tiers ?? null,
      })),
  }
}

function normalizeApimTargetTopologies(
  topology: ApimTargetTopologyList,
): ApimTargetTopologyList {
  return {
    ...topology,
    resource_lifecycle_available: topology.resource_lifecycle_available ?? false,
    items: topology.items.map((item) => {
      const configurationEndpoint = item.configuration_endpoint
        ?? `https://${item.target.service_name}.configuration.azure-api.net`
      const managed = item.deployments.find(
        ({ deployment }) => deployment.kind === "managed",
      )
      const publishedModelsAvailable = Array.isArray(item.published_models)
      const publishedModels = item.published_models ?? []
      return {
        ...item,
        deployments: item.deployments.map((entry) => ({
          ...entry,
          configuration_endpoint: entry.configuration_endpoint ?? configurationEndpoint,
          published_models_available: Array.isArray(entry.published_models),
          published_models: entry.published_models ?? [],
        })),
        management_endpoint: item.management_endpoint
          ?? `https://management.azure.com${item.target.arm_resource_id}`,
        gateway_endpoint: item.gateway_endpoint ?? managed?.deployment.endpoint_url ?? null,
        configuration_endpoint: configurationEndpoint,
        connection_state: item.connection_state ?? (
          item.target.drift_state === "in_sync"
            ? "connected"
            : item.target.drift_state === "drifted" ? "unavailable" : "unknown"
        ),
        // Count inventory, including discovered entries awaiting an endpoint.
        // Older APIs counted only configured endpoints; the complete inventory
        // is the same source of truth used by both hierarchy and flat list.
        self_hosted_gateway_count: item.deployments.filter(({ deployment }) => (
          deployment.kind === "self_hosted" && !deployment.retired_at
        )).length,
        published_model_count: item.published_model_count ?? publishedModels.length,
        published_models: publishedModels,
        published_models_available: publishedModelsAvailable,
      }
    }),
  }
}







function isUnsupportedOpenAICompatibleSchema(error: unknown) {
  if (!(error instanceof ApiError) || error.status !== 422) return false
  const payloadStart = error.message.indexOf("{")
  if (payloadStart < 0) return false
  try {
    const payload = JSON.parse(error.message.slice(payloadStart)) as {
      detail?: Array<{
        type?: string
        loc?: unknown[]
        input?: unknown
      }>
    }
    const issues = payload.detail ?? []
    const rejectsProvider = issues.some((issue) =>
      issue.type === "literal_error"
      && issue.input === "openai_compatible"
      && issue.loc?.at(-2) === "provider"
      && issue.loc?.at(-1) === "template",
    )
    const rejectsBaseUrl = issues.some((issue) =>
      issue.type === "extra_forbidden"
      && issue.loc?.at(-1) === "openai_base_url",
    )
    return rejectsProvider && rejectsBaseUrl
  } catch {
    return false
  }
}

async function requireOpenAICompatibleApi<T>(operation: Promise<T>) {
  try {
    return await operation
  } catch (error) {
    if (!isUnsupportedOpenAICompatibleSchema(error)) throw error
    throw new ApiError(
      422,
      "当前 API 尚未部署 OpenAI-compatible 连接支持。表单内容已保留，请在后端更新后重试。",
    )
  }
}



const normalizeGatewayPublication = (publication: GatewayPublication): GatewayPublication => ({
  ...publication,
  publication_kind: publication.publication_kind ?? "model_add",
  authorization: publication.authorization ?? null,
  retry_requires_credential: publication.retry_requires_credential ?? false,
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
  requestPage: (filters: UsageFilters, cursor?: string) => request<UsageRequestPage>(
    `/api/v1/observability/requests?${params(filters, { limit: 200, ...(cursor ? { cursor } : {}) })}`,
  ),
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
  budgets: (period: string, includeUsers = false) => request<TokenBudgetResponse>(
    `/api/v1/budgets?period=${encodeURIComponent(period)}${includeUsers ? "&include_users=true" : ""}`,
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
  registry: async (): Promise<ModelRegistry> => {
    const registry = normalizeRegistry(await request<ModelRegistry>("/api/v1/model-management"))
    if (registry.apim_target_topology_available && !registry.apim_target_topology) {
      registry.apim_target_topology = await dataSource.apimTargetTopologies()
    }
    return registry
  },
  apimOnboardingOperations: () => request<ApimOnboardingOperations>(
    "/api/v1/model-management/apim-onboarding",
  ),
  previewApimOnboarding: (value: ApimOnboardingRequest) => writeJson<ApimOnboardingPlan>(
    "/api/v1/model-management/apim-onboarding/preview", value,
  ),
  startApimOnboarding: (value: { request: ApimOnboardingRequest; confirmation_sha256: string }) =>
    writeJson<GatewayReleaseOperationAccepted>("/api/v1/model-management/apim-onboarding", value),
  retryApimOnboarding: (id: string) => request<GatewayReleaseOperationAccepted>(
    `/api/v1/model-management/apim-onboarding/${encodeURIComponent(id)}/retry`, { method: "POST" },
  ),













  saveGateway: (value: Record<string, unknown>, id?: string) => saveRegistry("/api/v1/model-management/gateways", value, id),
  renameGateway: (id: string, name: string) => request<ModelRegistry | undefined>(
    `/api/v1/model-management/gateways/${encodeURIComponent(id)}/name`,
    { method: "PATCH", headers: { "content-type": "application/json", Prefer: "return=minimal" }, body: JSON.stringify({ name }) },
  ).then(registry => registry ? normalizeRegistry(registry) : undefined),
  checkBudgetDelegation: (gatewayId: string) => writeJson<BudgetDelegationReadiness>(
    `/api/v1/model-management/gateways/${encodeURIComponent(gatewayId)}/budget-readiness`,
    {},
  ),
  verifyBudgetExecution: (gatewayId: string, applicationId: string, modelId: string) => writeJson<BudgetExecutionVerification>(
    `/api/v1/model-management/gateways/${encodeURIComponent(gatewayId)}/budget-execution-verification`,
    {application_id: applicationId, model_id: modelId},
  ),
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
  modelAvailability: () => request<ModelAvailabilityList>(
    "/api/v1/model-management/model-availability", { cache: "no-store" },
  ),
  changeModelAvailability: (id: string, value: ModelAvailabilityCommand) => writeJson<ModelAvailabilityAccepted>(
    `/api/v1/model-management/models/${encodeURIComponent(id)}/availability`, value, "PUT",
  ),
  retryModelAvailability: (operationId: string, revision: number) => writeJson<ModelAvailabilityAccepted>(
    `/api/v1/model-management/model-availability/operations/${encodeURIComponent(operationId)}/retry`,
    { expected_revision: revision },
  ),
  // Omitting the ids syncs everything that follows a list price. Passing them is how the
  // price table's row selection turns into "sync just these".
  pricePreview: (value: PricePreviewRequest) => writeJson<PricePreviewResponse>(
    "/api/v1/model-management/price-preview", value,
  ),
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
    writeJson<GatewayPublicationAccepted>(
      "/api/v1/model-management/publications",
      value,
    ),
  ).then(normalizeGatewayPublicationAccepted),
  archiveFailedPublication: (id: string) => request<void>(
    `/api/v1/model-management/publications/${encodeURIComponent(id)}/archive`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
  ),
  publishMultiTargetModel: (value: MultiTargetPublicationCreate) =>
    writeJson<MultiTargetPublicationAccepted>(
      "/api/v1/model-management/rollouts",
      value,
    ),
  multiTargetRollouts: () => request<MultiTargetRolloutList>(
    "/api/v1/model-management/rollouts?limit=50",
  ),
  multiTargetRollout: (id: string) => request<MultiTargetRollout>(
    `/api/v1/model-management/rollouts/${encodeURIComponent(id)}`,
  ),
  apimTargetTopologies: () => request<ApimTargetTopologyList>(
    "/api/v1/model-management/apim-targets",
  ).then(normalizeApimTargetTopologies),
  addApimTarget: (value: import("./types").ApimTargetWrite) =>
    writeJson<import("./types").ApimTargetTopology>(
      "/api/v1/model-management/apim-targets",
      value,
    ),
  previewApimTargetRemoval: (gatewayProfileId: string) =>
    request<import("./types").ApimTargetRemovalPreview>(
      `/api/v1/model-management/apim-targets/${encodeURIComponent(gatewayProfileId)}/removal-preview`,
    ),
  removeApimTarget: (gatewayProfileId: string) =>
    request<import("./types").ApimTargetRemovalPreview>(
      `/api/v1/model-management/apim-targets/${encodeURIComponent(gatewayProfileId)}`,
      { method: "DELETE" },
    ),
  addSelfHostedGateway: (
    gatewayProfileId: string,
    value: import("./types").SelfHostedGatewayWrite,
  ) => writeJson<import("./types").ApimTargetTopology["deployments"][number]>(
    `/api/v1/model-management/apim-targets/${encodeURIComponent(gatewayProfileId)}/self-hosted-gateways`,
    value,
  ),
  selfHostedGatewayProvisions: () => request<import("./types").SelfHostedGatewayProvisionOperations>(
    "/api/v1/model-management/self-hosted-gateway-provisions",
  ),
  createSelfHostedGateway: (targetId: string, value: import("./types").SelfHostedGatewayCreate, requestId: string) => request<GatewayReleaseOperationAccepted>(
    `/api/v1/model-management/apim-targets/${encodeURIComponent(targetId)}/self-hosted-gateway-provisions`,
    { method: "POST", headers: { "content-type": "application/json", "Idempotency-Key": requestId }, body: JSON.stringify(value) },
  ),
  retrySelfHostedGatewayProvision: (id: string) => request<GatewayReleaseOperationAccepted>(
    `/api/v1/model-management/self-hosted-gateway-provisions/${encodeURIComponent(id)}/retry`, { method: "POST" },
  ),
  renameSelfHostedGateway: (deploymentId: string, displayName: string) => request<void>(
    `/api/v1/model-management/self-hosted-gateways/${encodeURIComponent(deploymentId)}/name`,
    { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ display_name: displayName }) },
  ),
  validateSelfHostedGateway: (deploymentId: string) =>
    writeJson<import("./types").ApimTargetTopology["deployments"][number]>(
      `/api/v1/model-management/self-hosted-gateways/${encodeURIComponent(deploymentId)}/validations`,
      {},
    ),
  changeSelfHostedGatewayAssociation: (
    deploymentId: string,
    value: import("./types").SelfHostedGatewayAssociationChange,
  ) => writeJson<import("./types").ApimTargetTopology["deployments"][number]>(
    `/api/v1/model-management/self-hosted-gateways/${encodeURIComponent(deploymentId)}/apim-association`,
    value,
    "PUT",
  ),
  previewSelfHostedGatewayRemoval: (deploymentId: string) =>
    request<import("./types").SelfHostedGatewayRemovalPreview>(
      `/api/v1/model-management/self-hosted-gateways/${encodeURIComponent(deploymentId)}/removal-preview`,
    ),
  removeSelfHostedGateway: (deploymentId: string) =>
    request<import("./types").SelfHostedGatewayRemovalPreview>(
      `/api/v1/model-management/self-hosted-gateways/${encodeURIComponent(deploymentId)}`,
      { method: "DELETE" },
    ),
  requestApimTargetInventorySync: (gatewayProfileId: string) =>
    request<GatewayReleaseOperationAccepted>(
      `/api/v1/model-management/apim-targets/${encodeURIComponent(gatewayProfileId)}/inventory-syncs`,
      { method: "POST" },
    ),
  gatewayPublication: (id: string) => request<GatewayPublication>(
    `/api/v1/model-management/publications/${encodeURIComponent(id)}`,
  ).then(normalizeGatewayPublication),
  gatewayProgramUpgradePreview: (gatewayProfileId: string) => request<GatewayProgramUpgradePreview>(
    `/api/v1/model-management/gateways/${encodeURIComponent(gatewayProfileId)}/program-upgrades/preview`,
  ),
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
  updateGatewayApplicationOrganization: (id: string, organizationId: string | null, departmentId?: string | null, personId?: string | null) =>
    writeJson<{ organization_id: string | null; organization_name: string | null; department_id?: string | null; department_name?: string | null; person_id?: string | null; person_name?: string | null; projected: boolean }>(
      `/api/v1/application-access/applications/${encodeURIComponent(id)}/organization`,
      { organization_id: organizationId, ...(departmentId !== undefined ? { department_id: departmentId } : {}), ...(personId !== undefined ? { person_id: personId } : {}) }, "PUT",
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
  retryGatewayPublication: (id: string, credential?: string, kind: "api_key" | "oauth_m2m" = "api_key") => writeJson<GatewayPublication>(
    `/api/v1/model-management/publications/${encodeURIComponent(id)}/retry`,
    credential ? kind === "oauth_m2m" ? { oauth_client_secret: credential } : { api_key: credential } : {},
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
  runtimeCheckPlan: (id: string) => request<RuntimeCheckPlan>(
    `/api/v1/model-management/runtimes/${encodeURIComponent(id)}/check`,
  ),
  checkRuntime: (id: string, selection: RuntimeCheckSelection) => writeJson<RuntimeHealth>(
    `/api/v1/model-management/runtimes/${encodeURIComponent(id)}/check`, selection,
  ),
  invokeModel: (value: Record<string, unknown>) => writeJson<ModelInvocationResponse>(
    "/api/v1/model-gateway/invoke", value,
  ),
  invokeDecision: (value: DecisionInvocationRequest) => writeJson<DecisionInvocationResponse>(
    "/api/v1/model-gateway/decision", value,
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
  priceCatalogModels: (query: string, source: PriceCatalogModel["source"]) => request<PriceCatalogModelsResponse>(
    `/api/v1/model-management/price-catalog/models?q=${encodeURIComponent(query)}&source=${encodeURIComponent(source)}`,
  ),
  priceCatalogOptions: (modelKey: string) => request<PriceCatalogOptionsResponse>(
    `/api/v1/model-management/price-catalog/options?model=${encodeURIComponent(modelKey)}`,
  ),
  syncPrices: (modelIds?: string[]) => writeJson<PriceSyncResponse>(
    "/api/v1/model-management/price-sync",
    { model_ids: modelIds ?? null },
  ).then((result) => ({ ...result, registry: normalizeRegistry(result.registry) }))
}
