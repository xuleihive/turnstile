import type { QueryClient } from "@tanstack/react-query"
import {
  Activity,
  Building2,
  ChartPie,
  FileSpreadsheet,
  LayoutDashboard,
  UserRoundX,
  UsersRound,
  WalletCards,
  type LucideIcon,
} from "lucide-react"
import type { ReactNode } from "react"

import "./styles.css"

import { copilotKeys, copilotQueries } from "./queries"
import type { CopilotStatus } from "./types"
import { CopilotBudgetPage } from "./pages/budget-page"
import { CopilotDashboardPage } from "./pages/dashboard-page"
import { CopilotGovernancePage } from "./pages/governance-page"

export const GITHUB_COPILOT_SOURCE_ID = "github-copilot" as const

export const githubCopilotPages = [
  { id: "finops-overview", label: "使用指标", icon: LayoutDashboard },
  { id: "finops-analytics", label: "AI 用量", icon: ChartPie },
  { id: "finops-trends", label: "使用报告", icon: FileSpreadsheet },
  { id: "copilot-cost-centers", label: "成本中心", icon: Building2 },
  { id: "copilot-unassigned-users", label: "未分配用户", icon: UserRoundX },
  { id: "copilot-enterprise-teams", label: "企业团队", icon: UsersRound },
  { id: "budgets", label: "预算", icon: WalletCards },
  { id: "copilot-requests", label: "请求审批", icon: Activity },
] as const satisfies ReadonlyArray<{ id: string; label: string; icon: LucideIcon }>

const githubCopilotPageIds = new Set([
  ...githubCopilotPages.map((item) => item.id),
  "assistant",
  "settings",
])

export function githubCopilotOwnsPage(page: string) {
  return githubCopilotPageIds.has(page)
}

export function normalizeGithubCopilotPage(page: string) {
  return githubCopilotOwnsPage(page) ? page : "finops-overview"
}

export function prefetchGithubCopilotPage(queryClient: QueryClient, page: string) {
  void queryClient.prefetchQuery(copilotQueries.status())
  const status = queryClient.getQueryData<CopilotStatus>(copilotKeys.status)
  const canRead = status?.configured === true
    && (status.viewer_role === "owner" || Boolean(status.viewer_github_login))
  if (page === "copilot-requests" && canRead) {
    void queryClient.prefetchQuery(copilotQueries.budgetRequests())
  } else if (
    canRead
    && status?.viewer_role === "owner"
    && [
      "budgets",
      "copilot-cost-centers",
      "copilot-unassigned-users",
      "copilot-enterprise-teams",
    ].includes(page)
  ) {
    void queryClient.prefetchQuery(copilotQueries.governance())
  } else if (page.startsWith("finops-") && canRead) {
    void queryClient.prefetchQuery(copilotQueries.dashboard())
  }
}

export function renderGithubCopilotPage(page: string, onToggleSidebar: () => void): ReactNode {
  if (page === "budgets") {
    return <CopilotGovernancePage key={page} view="budgets" onToggleSidebar={onToggleSidebar} />
  }
  if (page === "copilot-cost-centers") {
    return <CopilotGovernancePage key={page} view="cost-centers" onToggleSidebar={onToggleSidebar} />
  }
  if (page === "copilot-unassigned-users") {
    return <CopilotGovernancePage key={page} view="unassigned" onToggleSidebar={onToggleSidebar} />
  }
  if (page === "copilot-enterprise-teams") {
    return <CopilotGovernancePage key={page} view="teams" onToggleSidebar={onToggleSidebar} />
  }
  if (page === "copilot-requests") {
    return <CopilotBudgetPage onToggleSidebar={onToggleSidebar} />
  }
  if (page.startsWith("finops-")) {
    return <CopilotDashboardPage
      key={page}
      initialTab={page === "finops-analytics" ? "analytics" : page === "finops-trends" ? "trends" : "overview"}
      onToggleSidebar={onToggleSidebar}
    />
  }
  return null
}
