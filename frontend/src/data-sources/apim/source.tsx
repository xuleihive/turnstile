import type { QueryClient } from "@tanstack/react-query"
import {
  Activity,
  ChartPie,
  LayoutDashboard,
  LineChart,
  ShieldAlert,
  WalletCards,
  type LucideIcon,
} from "lucide-react"
import type { ReactNode } from "react"

import { usageWindow } from "./api"
import { prefetchPage, type PrefetchablePage } from "./queries"
import type { UsageFilters } from "./types"
import { BudgetManagementPage } from "./pages/budget-page"
import { FinOpsDashboard, type FinOpsScope } from "./pages/dashboard-page"

export const APIM_SOURCE_ID = "apim" as const

export const apimPages = [
  { id: "finops-overview", label: "管理总览", icon: LayoutDashboard },
  { id: "budgets", label: "预算管理", icon: WalletCards },
  { id: "finops-analytics", label: "用量分布", icon: ChartPie },
  { id: "finops-trends", label: "使用趋势", icon: LineChart },
  { id: "finops-governance", label: "异常治理", icon: ShieldAlert },
  { id: "finops-requests", label: "请求追踪", icon: Activity },
] as const satisfies ReadonlyArray<{ id: string; label: string; icon: LucideIcon }>

const apimPageIds = new Set([
  ...apimPages.map((item) => item.id),
  "assistant",
  "finops-invoke",
  "apim-native-routes",
  "applications",
  "gateway-releases",
  "models",
  "organization",
  "pinned-report",
  "settings",
])

export function normalizeApimPage(page: string) {
  return apimPageIds.has(page) ? page : "finops-overview"
}

export const apimPrefetchablePages = new Set<PrefetchablePage>([
  "finops-overview",
  "finops-analytics",
  "finops-trends",
  "finops-governance",
  "finops-requests",
  "finops-invoke",
  "budgets",
  "apim-native-routes",
  "applications",
  "models",
])

export function prefetchApimPage(
  queryClient: QueryClient,
  page: string,
  filters: UsageFilters,
) {
  if (apimPrefetchablePages.has(page as PrefetchablePage)) {
    prefetchPage(queryClient, page as PrefetchablePage, filters)
  }
}

export function defaultApimFilters(days = 30, scope: FinOpsScope = {}): UsageFilters {
  return { ...usageWindow(days), ...scope }
}

export function renderApimPage({
  page,
  onToggleSidebar,
  days,
  scope,
  onDaysChange,
  onScopeChange,
  onOpenRequest,
}: {
  page: string
  onToggleSidebar: () => void
  days: number
  scope: FinOpsScope
  onDaysChange: (days: number) => void
  onScopeChange: (scope: FinOpsScope) => void
  onOpenRequest: (requestId: string) => void
}): ReactNode {
  if (page === "budgets") {
    return <BudgetManagementPage onToggleSidebar={onToggleSidebar} />
  }
  if (!page.startsWith("finops-")) return null
  return <FinOpsDashboard
    initialTab={page.replace("finops-", "") as "overview" | "analytics" | "trends" | "governance" | "requests" | "invoke"}
    onToggleSidebar={onToggleSidebar}
    days={days}
    scope={scope}
    onDaysChange={onDaysChange}
    onScopeChange={onScopeChange}
    onOpenRequest={onOpenRequest}
  />
}
