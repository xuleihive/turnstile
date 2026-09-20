import { queryOptions, type QueryClient } from "@tanstack/react-query"

import { cachePolicies } from "../../api/cache-policies"
import { resolveTimezone } from "../../lib/timezone"
import { dataSource, usageWindow } from "./api"
import type {
  DistributionDimension,
  PeopleBudgetFilter,
  TrendDimension,
  UsageFilters,
} from "./types"

const DAY_MS = 24 * 60 * 60 * 1000

type UsageScope = {
  days: number
  organizationId: string | null
  departmentId: string | null
  projectId: string | null
  agentId: string | null
  modelId: string | null
  userId: string | null
  runtime: string[] | null
  statusCode: number | null
}

function scopeFromFilters(filters: UsageFilters): UsageScope {
  const duration = Date.parse(filters.to) - Date.parse(filters.from)
  return {
    days: Math.max(1, Math.round(duration / DAY_MS)),
    organizationId: filters.organization_id ?? null,
    departmentId: filters.department_id ?? null,
    projectId: filters.project_id ?? null,
    agentId: filters.agent_id ?? null,
    modelId: filters.model_id ?? null,
    userId: filters.user_id ?? null,
    // Sorted so two selections of the same channel share one cache entry regardless of the
    // order the registry happened to return its runtimes in.
    runtime: filters.runtime?.length ? [...filters.runtime].sort() : null,
    statusCode: filters.status_code ?? null,
  }
}

function filtersFromScope(scope: UsageScope): UsageFilters {
  return {
    ...usageWindow(scope.days),
    organization_id: scope.organizationId ?? undefined,
    department_id: scope.departmentId ?? undefined,
    project_id: scope.projectId ?? undefined,
    agent_id: scope.agentId ?? undefined,
    model_id: scope.modelId ?? undefined,
    user_id: scope.userId ?? undefined,
    runtime: scope.runtime ?? undefined,
    status_code: scope.statusCode ?? undefined,
  }
}

export function withWindowDays(filters: UsageFilters, days: number): UsageFilters {
  const scope = scopeFromFilters(filters)
  return filtersFromScope({ ...scope, days })
}

// Project is deliberately absent from every comparison below. It is the one attribution
// field no token carries -- the APIM policy writes `x-project-id: unattributed` outright
// -- so it can only be asserted per request by the caller. Measured on 90 days of
// production traffic, 98.8% of tokens have no project, so ranking by it ranks 1.2% of the
// spend. The filter and the column stay in the data path for customers who do send it.
export function trendDimensionForFilters(filters: UsageFilters): TrendDimension {
  if (filters.agent_id || filters.user_id) return "model"
  if (filters.department_id) return "agent"
  return "department"
}

export const finopsKeys = {
  all: ["finops"] as const,
  entities: ["reference", "enterprise-entities"] as const,
  organizationDirectory: ["reference", "organization-directory"] as const,
  consoleMembers: ["reference", "console-members"] as const,
  registry: ["reference", "model-registry"] as const,
  modelBackendPool: (id: string) => ["model-platform", "model-backend-pool", id] as const,
  gatewayPublications: ["control-plane", "gateway-publications"] as const,
  gatewayPublication: (id: string) => ["control-plane", "gateway-publication", id] as const,
  gatewayReleases: ["control-plane", "gateway-releases"] as const,
  gatewayRelease: (id: string) => ["control-plane", "gateway-release", id] as const,
  gatewayReleaseDiff: (id: string, againstId: string | null) => ["control-plane", "gateway-release-diff", id, againstId] as const,
  gatewayReleaseIntegrity: (id: string) => ["control-plane", "gateway-release-integrity", id] as const,
  gatewayReleaseRollbackPreview: (id: string) => ["control-plane", "gateway-release-rollback-preview", id] as const,
  gatewayReleaseOperation: (id: string) => ["control-plane", "gateway-release-operation", id] as const,
  gatewayApplications: ["application-access", "applications"] as const,
  gatewayApplication: (id: string) => ["application-access", "application", id] as const,
  gatewayApplicationUsageActivity: (
    id: string,
    scope: UsageScope,
    interval: "day" | "week",
    timezone: string,
  ) => ["application-access", "application", id, "usage-activity", interval, timezone, scope] as const,
  executiveOverview: (scope: UsageScope) => ["finops", "executive-overview", scope] as const,
  distribution: (scope: UsageScope, dimension: DistributionDimension, splitBy?: DistributionDimension) =>
    ["finops", "distribution", dimension, splitBy ?? null, scope] as const,
  requests: (scope: UsageScope) => ["finops", "requests", scope] as const,
  // The server buckets by timezone, so the zone is part of the response identity rather than a
  // display option that could be reapplied to a cached UTC result.
  usageActivity: (scope: UsageScope, interval: "hour" | "day" | "week", timezone: string) =>
    ["finops", "usage-activity", interval, timezone, scope] as const,
  anomalies: (scope: UsageScope) => ["finops", "anomalies", scope] as const,
  anomalyRules: ["finops", "anomaly-rules"] as const,
  trends: (scope: UsageScope, groupBy: TrendDimension, interval: "hour" | "day" | "week") => ["finops", "trends", groupBy, interval, scope] as const,
  requestDetail: (requestId: string) => ["finops", "request-detail", requestId] as const,
  budgets: (period: string) => ["finops", "budgets", period] as const,
  peopleBudgets: (
    period: string,
    departmentId: string,
    query: string,
    status: PeopleBudgetFilter,
    offset: number,
    limit: number,
  ) => ["finops", "budgets", "people", period, departmentId, query, status, offset, limit] as const,
}

export const finopsQueries = {
  entities: () => queryOptions({
    queryKey: finopsKeys.entities,
    queryFn: dataSource.entities,
    ...cachePolicies.reference,
  }),
  organizationDirectory: () => queryOptions({
    queryKey: finopsKeys.organizationDirectory,
    queryFn: dataSource.organizationDirectory,
    ...cachePolicies.reference,
  }),
  consoleMembers: () => queryOptions({
    queryKey: finopsKeys.consoleMembers,
    queryFn: dataSource.consoleMembers,
    ...cachePolicies.reference,
  }),
  registry: () => queryOptions({
    queryKey: finopsKeys.registry,
    queryFn: dataSource.registry,
    ...cachePolicies.aggregate,
  }),
  modelBackendPool: (id: string | null) => queryOptions({
    queryKey: finopsKeys.modelBackendPool(id ?? ""),
    queryFn: () => dataSource.modelBackendPool(id!),
    enabled: Boolean(id),
    ...cachePolicies.aggregate,
  }),
  gatewayPublication: (id: string | null) => queryOptions({
    queryKey: finopsKeys.gatewayPublication(id ?? ""),
    queryFn: () => dataSource.gatewayPublication(id!),
    enabled: Boolean(id),
    refetchIntervalInBackground: true,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status && !["active", "failed", "awaiting_authorization", "rolled_back", "superseded"].includes(status)
        ? 2_000
        : false
    },
  }),
    gatewayPublications: () => queryOptions({
      queryKey: finopsKeys.gatewayPublications,
      queryFn: dataSource.gatewayPublications,
      refetchInterval: (query) => query.state.data?.items.some(
        (item) => !["active", "failed", "awaiting_authorization", "rolled_back", "superseded"].includes(item.status),
      ) ? 2_000 : false,
      ...cachePolicies.aggregate,
    }),
  gatewayReleases: () => queryOptions({
    queryKey: finopsKeys.gatewayReleases,
    queryFn: dataSource.gatewayReleases,
    ...cachePolicies.aggregate,
    refetchIntervalInBackground: true,
    refetchInterval: (query) => query.state.data?.items.some(
      (item) => item.role === "in_progress",
    ) ? 2_000 : false,
  }),
  gatewayRelease: (id: string | null) => queryOptions({
    queryKey: finopsKeys.gatewayRelease(id ?? ""),
    queryFn: () => dataSource.gatewayRelease(id!),
    enabled: Boolean(id),
    ...cachePolicies.aggregate,
    refetchIntervalInBackground: true,
    refetchInterval: (query) => query.state.data?.role === "in_progress" ? 2_000 : false,
  }),
  gatewayReleaseDiff: (id: string | null, againstId: string | null = null) => queryOptions({
    queryKey: finopsKeys.gatewayReleaseDiff(id ?? "", againstId),
    queryFn: () => dataSource.gatewayReleaseDiff(id!, againstId ?? undefined),
    enabled: Boolean(id),
    ...cachePolicies.aggregate,
  }),
  gatewayReleaseIntegrity: (id: string | null) => queryOptions({
    queryKey: finopsKeys.gatewayReleaseIntegrity(id ?? ""),
    queryFn: () => dataSource.gatewayReleaseIntegrity(id!),
    enabled: Boolean(id),
    ...cachePolicies.aggregate,
  }),
  gatewayReleaseRollbackPreview: (id: string | null, enabled = true) => queryOptions({
    queryKey: finopsKeys.gatewayReleaseRollbackPreview(id ?? ""),
    queryFn: () => dataSource.gatewayReleaseRollbackPreview(id!),
    enabled: Boolean(id) && enabled,
    staleTime: 0,
  }),
  gatewayReleaseOperation: (id: string | null) => queryOptions({
    queryKey: finopsKeys.gatewayReleaseOperation(id ?? ""),
    queryFn: () => dataSource.gatewayReleaseOperation(id!),
    enabled: Boolean(id),
    refetchIntervalInBackground: true,
    refetchInterval: (query) => {
      if (query.state.data?.worker_available === false) return false
      const status = query.state.data?.status
      return status && !["succeeded", "failed", "restored"].includes(status)
        ? 2_000
        : false
    },
  }),
  gatewayApplications: () => queryOptions({
    queryKey: finopsKeys.gatewayApplications,
    queryFn: dataSource.gatewayApplications,
    ...cachePolicies.aggregate,
  }),
  gatewayApplication: (id: string | null) => queryOptions({
    queryKey: finopsKeys.gatewayApplication(id ?? ""),
    queryFn: () => dataSource.gatewayApplication(id!),
    enabled: Boolean(id),
    ...cachePolicies.aggregate,
  }),
  gatewayApplicationUsageActivity: (
    id: string,
    filters: UsageFilters,
    interval: "day" | "week",
    timezone: string,
  ) => {
    const scope = scopeFromFilters(filters)
    return queryOptions({
      queryKey: finopsKeys.gatewayApplicationUsageActivity(id, scope, interval, timezone),
      queryFn: () => dataSource.gatewayApplicationUsageActivity(
        id,
        filtersFromScope(scope),
        interval,
        timezone,
      ),
      ...cachePolicies.aggregate,
    })
  },
  executiveOverview: (filters: UsageFilters) => {
    const scope = scopeFromFilters(filters)
    return queryOptions({
      queryKey: finopsKeys.executiveOverview(scope),
      queryFn: () => dataSource.executiveOverview(filtersFromScope(scope)),
      ...cachePolicies.aggregate,
    })
  },
  distribution: (filters: UsageFilters, dimension: DistributionDimension, splitBy?: DistributionDimension) => {
    const scope = scopeFromFilters(filters)
    return queryOptions({
      queryKey: finopsKeys.distribution(scope, dimension, splitBy),
      queryFn: () => dataSource.distribution(filtersFromScope(scope), dimension, 30, splitBy),
      ...cachePolicies.aggregate,
    })
  },
  requests: (filters: UsageFilters) => {
    const scope = scopeFromFilters(filters)
    return queryOptions({
      queryKey: finopsKeys.requests(scope),
      queryFn: () => dataSource.requests(filtersFromScope(scope), 200),
      ...cachePolicies.requestList,
    })
  },
  anomalies: (filters: UsageFilters) => {
    const scope = scopeFromFilters(filters)
    return queryOptions({
      queryKey: finopsKeys.anomalies(scope),
      queryFn: () => dataSource.anomalies(filtersFromScope(scope), 100),
      ...cachePolicies.aggregate,
    })
  },
  anomalyRules: () => queryOptions({
    queryKey: finopsKeys.anomalyRules,
    queryFn: dataSource.anomalyRules,
    ...cachePolicies.aggregate,
  }),
  // The usage-over-time chart reads a server-side aggregate rather than the capped request page.
  // One global point per bucket lets the backend merge low-cardinality streamed Cache Read
  // metrics without pretending they can be attributed to a model or another business scope.
  usageActivity: (
    filters: UsageFilters,
    interval: "hour" | "day" | "week",
    timezone: string,
  ) => {
    const scope = scopeFromFilters(filters)
    return queryOptions({
      queryKey: finopsKeys.usageActivity(scope, interval, timezone),
      queryFn: () => dataSource.trendPoints(filtersFromScope(scope), "none", interval, timezone),
      ...cachePolicies.aggregate,
    })
  },
  trends: (
    filters: UsageFilters,
    groupBy: TrendDimension,
    interval: "hour" | "day" | "week",
  ) => {
    const scope = scopeFromFilters(filters)
    return queryOptions({
      queryKey: finopsKeys.trends(scope, groupBy, interval),
      queryFn: () => dataSource.trendPoints(filtersFromScope(scope), groupBy, interval),
      ...cachePolicies.aggregate,
    })
  },
  requestDetail: (requestId: string) => queryOptions({
    queryKey: finopsKeys.requestDetail(requestId),
    queryFn: () => dataSource.requestDetail(requestId),
    enabled: requestId.length > 0,
    ...cachePolicies.requestDetail,
  }),
  budgets: (period: string) => queryOptions({
    queryKey: finopsKeys.budgets(period),
    queryFn: () => dataSource.budgets(period),
    ...cachePolicies.aggregate,
  }),
  peopleBudgets: (
    period: string,
    departmentId: string,
    query: string,
    status: PeopleBudgetFilter,
    offset: number,
    limit: number,
  ) => queryOptions({
    queryKey: finopsKeys.peopleBudgets(
      period,
      departmentId,
      query,
      status,
      offset,
      limit,
    ),
    queryFn: () => dataSource.peopleBudgets(
      period,
      departmentId,
      query,
      status,
      offset,
      limit,
    ),
    enabled: departmentId.length > 0,
    ...cachePolicies.aggregate,
  }),
}

export type PrefetchablePage =
  | "finops-overview"
  | "finops-analytics"
  | "finops-trends"
  | "finops-governance"
  | "finops-requests"
  | "finops-invoke"
  | "budgets"
  | "apim-native-routes"
  | "applications"
  | "models"

export function prefetchPage(
  queryClient: QueryClient,
  page: PrefetchablePage,
  filters: UsageFilters,
) {
  void queryClient.prefetchQuery(finopsQueries.entities())
  void queryClient.prefetchQuery(finopsQueries.registry())


  if (page === "applications") {
    void queryClient.prefetchQuery(finopsQueries.gatewayApplications())
  }

  if (page === "finops-overview") {
    void queryClient.prefetchQuery(finopsQueries.executiveOverview(filters))
    void queryClient.prefetchQuery(finopsQueries.anomalyRules())
    void queryClient.prefetchQuery(finopsQueries.distribution(filters, "department"))
    void queryClient.prefetchQuery(finopsQueries.distribution(filters, "model"))
    void queryClient.prefetchQuery(finopsQueries.distribution(filters, "agent"))
    void queryClient.prefetchQuery(finopsQueries.distribution(filters, "user"))
    void queryClient.prefetchQuery(finopsQueries.distribution(filters, "runtime"))
    void queryClient.prefetchQuery(finopsQueries.requests(filters))
    void queryClient.prefetchQuery(finopsQueries.usageActivity(filters, "hour", resolveTimezone()))
  } else if (page === "finops-analytics") {
    // The page opens on the department cross-tab, and one split query serves both the
    // ranking and the stacked chart.
    void queryClient.prefetchQuery(finopsQueries.distribution(filters, "department", "model"))
    void queryClient.prefetchQuery(finopsQueries.distribution(filters, "model"))
    void queryClient.prefetchQuery(finopsQueries.requests(filters))
  } else if (page === "finops-trends") {
    void queryClient.prefetchQuery(finopsQueries.trends(filters, trendDimensionForFilters(filters), "hour"))
    void queryClient.prefetchQuery(finopsQueries.requests(filters))
  } else if (page === "finops-governance") {
    void queryClient.prefetchQuery(finopsQueries.anomalyRules())
    void queryClient.prefetchQuery(finopsQueries.requests(filters))
  } else if (page === "finops-requests") {
    void queryClient.prefetchQuery(finopsQueries.requests(filters))
  } else if (page === "budgets") {
    void queryClient.prefetchQuery(finopsQueries.budgets(new Date().toISOString().slice(0, 7)))
  }
}

export function invalidateFinOps(queryClient: QueryClient) {
  return queryClient.invalidateQueries({ queryKey: finopsKeys.all })
}
