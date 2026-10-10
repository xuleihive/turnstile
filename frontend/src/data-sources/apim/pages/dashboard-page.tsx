import { useEffect, useMemo, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Activity,
  Archive,
  AlertTriangle,
  Bot,
  ChartPie,
  Building2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  CircleDollarSign,
  Clock3,
  LayoutDashboard,
  LineChart,
  Network,
  PanelLeft,
  RefreshCw,
  Search,
  ShieldAlert,
  Timer,
  UserRound,
  Users,
  Workflow,
  X,
  Zap,
} from "lucide-react"
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../../components/ui/select"
import { Button } from "../../../components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../../components/ui/dialog"
import { UsageActivityCard } from "../../../components/finops/usage-activity-card"
import { ChartSeriesLegend, useSeriesToggle } from "../../../components/finops/chart-legend"
import { CategoryAxisTick, categoryAxisWidth, categoryTickGutter, useChartWidthKey } from "../../../components/finops/category-axis"
import { ResizableGridTable } from "../../../components/ui/resizable-table"
import { FinOpsChartTooltip } from "../../../components/finops/chart-tooltip"
import { FilterMenuField, MultiSelectFilterField } from "../../../components/finops/filter-menu-field"
import {
  reconcileUsageScope, usageArchiveFilter, usageArchiveSelection, usageFacetOptions, usageSelection,
} from "../../../lib/usage-filter-scope"
import { AnomalyRuleManagement } from "./anomaly-rule-management"
import { AgentInvocation } from "./dashboard-invocation"
import {
  compact,
  currency,
  decimal,
  EmptyState,
  ErrorState,
  formatLatency,
  LoadingState,
  PanelTitle,
} from "./dashboard-shared"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "../../../components/ui/dropdown-menu"
import { usageWindow } from "../api"
import { getIntlLocale, useLocale, type LocalePreference } from "../../../locales/index"
import { priceRequests } from "../../../lib/pricing"
import { finopsKeys, finopsQueries, trendDimensionForFilters } from "../queries"
import type {
  AnomalyRule,
  DistributionDimension,
  DistributionItem,
  EnterpriseEntityCatalog,
  ExecutiveTotals,
  ManagedModel,
  ModelRegistry,
  TrafficGenerationRequest,
  TrendApiResponse,
  TrendDimension,
  TrendMetric,
  UsageFilters,
  UsageDirectoryStatus,
  UsageAnomaly,
  UsageRequestDetail,
  UsageRequestSummary,
} from "../types"

type AnalyticsTab = "overview" | "analytics" | "trends" | "governance" | "requests" | "invoke"
export type FinOpsScope = Omit<UsageFilters, "from" | "to">
type RankingLimit = 5 | 10 | 20

const rankingLimits: RankingLimit[] = [5, 10, 20]

const pageHeaders: Record<AnalyticsTab, { title: string; icon: typeof Activity }> = {
  overview: { title: "管理总览", icon: LayoutDashboard },
  analytics: { title: "用量分布", icon: ChartPie },
  trends: { title: "使用趋势", icon: LineChart },
  governance: { title: "异常治理", icon: ShieldAlert },
  requests: { title: "请求追踪", icon: Activity },
  invoke: { title: "调用测试", icon: Zap },
}

// Mirrors the backend RECONCILIATION_LOOKBACK_HOURS setting: past this age the gateway telemetry is
// no longer scanned, so an unmeasured request will never receive its real token counts.
const RECONCILIATION_LOOKBACK_HOURS = 24

// A request whose usage was never measured carries placeholder zeros until reconciliation backfills
// the real counts, so those zeros must never be rendered as if they were observed values.
function usageState(unmeasured: boolean, timestamp: string) {
  if (!unmeasured) return "measured" as const
  const withinLookback = Date.now() - new Date(timestamp).getTime() < RECONCILIATION_LOOKBACK_HOURS * 3_600_000
  return withinLookback ? ("pending" as const) : ("unavailable" as const)
}

export function FinOpsDashboard({
  initialTab = "overview",
  onToggleSidebar,
  days,
  scope,
  onDaysChange,
  onScopeChange,
  onOpenRequest,
}: {
  initialTab?: AnalyticsTab
  onToggleSidebar: () => void
  days: number
  scope: FinOpsScope
  onDaysChange: (days: number) => void
  onScopeChange: (scope: FinOpsScope) => void
  onOpenRequest: (requestId: string) => void
}) {
  const queryClient = useQueryClient()
  const [isRefreshing, setIsRefreshing] = useState(false)
  const tab = initialTab
  const header = pageHeaders[tab]
  const HeaderIcon = header.icon
  const registryQuery = useQuery(finopsQueries.registry())
  const managedScope = useMemo(() => ({
    ...scope, directory_status: usageArchiveSelection(scope.directory_status),
  }), [scope])
  const scopeFilters = useMemo<UsageFilters>(() => ({
    ...usageWindow(days), ...managedScope,
    directory_status: usageArchiveFilter(managedScope.directory_status),
  }), [days, managedScope])
  const activeEntities = useQuery({ ...finopsQueries.entities(), enabled: tab === "invoke" })
  const historyEntities = useQuery({ ...finopsQueries.queryEntities(scopeFilters), enabled: tab !== "invoke" })
  const effectiveScope = useMemo(() => historyEntities.data
    ? reconcileUsageScope(managedScope, historyEntities.data) : managedScope, [managedScope, historyEntities.data])
  const filters = useMemo(() => ({
    ...scopeFilters, ...effectiveScope,
    directory_status: usageArchiveFilter(effectiveScope.directory_status),
  }), [scopeFilters, effectiveScope])
  const entityQuery = tab === "invoke" ? activeEntities : historyEntities
  const entities = entityQuery.data
  const unpricedModels = registryQuery.data?.models.filter((model) => model.enabled && (model.input_cost_per_million == null || model.output_cost_per_million == null)) ?? []
  const costAvailable = registryQuery.data != null && unpricedModels.length === 0
  const facets = historyEntities.data ? usageFacetOptions(historyEntities.data, filters) : null
  const updateScope = (patch: Partial<FinOpsScope>) => onScopeChange({ ...effectiveScope, ...patch })
  const directoryStatuses = managedScope.directory_status
  const activeFilterCount = Object.entries(effectiveScope).filter(([key, value]) =>
    key !== "directory_status" && (Array.isArray(value) ? value.length > 0 : Boolean(value))).length
    + (days === 30 ? 0 : 1)
    + (directoryStatuses.length === 1 && directoryStatuses[0] === "active" ? 0 : 1)
  const refreshCurrentPage = async () => {
    setIsRefreshing(true)
    try {
      await Promise.all([
        queryClient.refetchQueries({ queryKey: finopsKeys.all, type: "active" }),
        queryClient.refetchQueries({ queryKey: finopsKeys.entities, type: "active" }),
        queryClient.refetchQueries({ queryKey: finopsKeys.queryEntities(filters), type: "active" }),
        queryClient.refetchQueries({ queryKey: finopsKeys.registry, type: "active" }),
      ])
    } finally {
      setIsRefreshing(false)
    }
  }
  return <div className="finops-workspace">
    <header className="finops-header">
      <div><Button variant="ghost" size="icon-sm" className="finops-sidebar-trigger" aria-label="切换导航栏" title="切换导航栏" onClick={onToggleSidebar}><PanelLeft size={16} /></Button><span className="finops-header-icon"><HeaderIcon size={17} /></span><h1>{header.title}</h1></div>
      <Button variant="ghost" size="icon-sm" className="finops-header-refresh" aria-label="刷新当前页面数据" title="刷新当前页面数据" disabled={isRefreshing} onClick={() => void refreshCurrentPage()}><RefreshCw className={isRefreshing ? "spin" : undefined} size={15} /></Button>
    </header>
    {tab !== "invoke" && <div className="finops-filterbar">
      <FilterMenuField label="时间范围" icon={Clock3} value={String(days)} active={days !== 30} allowAll={false} options={[{ value: "7", label: "近 7 天" }, { value: "30", label: "近 30 天" }, { value: "90", label: "近 90 天" }]} onChange={(next) => next && onDaysChange(Number(next))} />
      <MultiSelectFilterField label="归档状态" icon={Archive} value={directoryStatuses}
        translateValues active={directoryStatuses.length !== 1 || directoryStatuses[0] !== "active"}
        options={[{ value: "active", label: "启用" }, { value: "archived", label: "已归档" }]}
        onChange={next => updateScope({
          directory_status: next as UsageDirectoryStatus[], organization_id: undefined,
          department_id: undefined, agent_id: undefined, user_id: undefined,
        })} />
      {facets && <>
        <MultiSelectFilterField label="组织" icon={Building2} value={usageSelection(effectiveScope.organization_id)} options={facets.organizations.map((item) => ({ value: item.id, label: item.name }))} onChange={(organization_id) => updateScope({ organization_id, department_id: undefined, agent_id: undefined, user_id: undefined })} />
        <MultiSelectFilterField label="部门" icon={Users} value={usageSelection(effectiveScope.department_id)} options={facets.departments.map((item) => ({ value: item.id, label: item.name }))} onChange={(department_id) => updateScope({ department_id, agent_id: undefined, user_id: undefined })} />
        {/* No project filter. It is the one dimension no token carries -- the APIM policy
            writes it as `unattributed` outright -- so 98.8% of production tokens have
            none, and a control that narrows to 1.2% of the spend is a trap rather than a
            tool. `project_id` stays in the API and the schema for callers that do send
            it. */}
        <MultiSelectFilterField label="智能体" icon={Bot} value={usageSelection(effectiveScope.agent_id)} options={facets.agents.map((item) => ({ value: item.id, label: item.name }))} onChange={(agent_id) => updateScope({ agent_id })} />
        <MultiSelectFilterField label="人员" icon={UserRound} value={usageSelection(effectiveScope.user_id)} options={facets.users.map((item) => ({ value: item.id, label: item.name }))} onChange={(user_id) => updateScope({ user_id })} />
      </>}
      {activeFilterCount > 0 && <Button variant="ghost" size="sm" className="finops-clear" onClick={() => { onDaysChange(30); onScopeChange({}) }}><X size={14} />重置</Button>}
    </div>}
    <div className="finops-scroll-region">
      {tab !== "invoke" && unpricedModels.length > 0 && <PricingCoverageNotice modelKeys={unpricedModels.map((model) => model.model_key)} />}
      <div className="finops-content">
        {(entityQuery.isLoading || registryQuery.isLoading) && <LoadingState label={tab === "invoke" ? "正在加载调用配置" : undefined} />}
        {(entityQuery.error || registryQuery.error) && <ErrorState error={entityQuery.error ?? registryQuery.error} />}
        {entities && registryQuery.data && tab === "overview" && <ExecutiveOverview filters={filters} costAvailable={costAvailable} />}
        {entities && registryQuery.data && tab === "analytics" && <ModelUsage filters={filters} costAvailable={costAvailable} />}
        {entities && tab === "trends" && <UsageTrends filters={filters} />}
        {entities && registryQuery.data && tab === "governance" && <Governance filters={filters} entities={entities} models={registryQuery.data.models} onOpenRequest={onOpenRequest} />}
        {entities && registryQuery.data && tab === "requests" && <RequestTrace key={JSON.stringify(effectiveScope)} filters={filters} costAvailable={costAvailable} models={registryQuery.data.models} />}
        {entities && tab === "invoke" && <AgentInvocation entities={entities} />}
      </div>
    </div>
  </div>
}

function PricingCoverageNotice({ modelKeys }: { modelKeys: string[] }) {
  const openModelPricing = () => {
    const url = new URL(window.location.href)
    url.searchParams.set("page", "models")
    url.searchParams.delete("cacheBust")
    window.location.assign(url)
  }
  return <div className="finops-pricing-warning" role="status">
    <CircleAlert size={18} />
    <div><p><b>成本尚未计价</b><span>{`${modelKeys.length} 个启用模型缺少输入或输出单价；相关 Token 尚未计入费用统计。`}</span></p><code>{modelKeys.join(" · ")}</code></div>
    <button type="button" onClick={openModelPricing}>设置自定义价格</button>
  </div>
}

function ExecutiveOverview({ filters, costAvailable }: { filters: UsageFilters; costAvailable: boolean }) {
  const overview = useQuery(finopsQueries.executiveOverview(filters))
  // The server rule engine evaluates over the complete filtered data set. Evaluating the
  // client-side rules here instead only ever saw the capped request page, so a threshold
  // could be breached across the window and still not fire.
  const anomalies = useQuery(finopsQueries.anomalies(filters))
  const departments = useQuery(finopsQueries.distribution(filters, "department"))
  const models = useQuery(finopsQueries.distribution(filters, "model"))
  const agents = useQuery(finopsQueries.distribution(filters, "agent"))
  const users = useQuery(finopsQueries.distribution(filters, "user"))
  const runtimes = useQuery(finopsQueries.distribution(filters, "runtime"))
  if (overview.isLoading || anomalies.isLoading || departments.isLoading || models.isLoading || agents.isLoading || users.isLoading || runtimes.isLoading) return <LoadingState />
  if (overview.error || anomalies.error || departments.error || models.error || agents.error || users.error || runtimes.error) return <ErrorState error={overview.error ?? anomalies.error ?? departments.error ?? models.error ?? agents.error ?? users.error ?? runtimes.error} />
  if (!overview.data) return null
  const totals = overview.data.totals
  // The server value, not a sum over the 200-row request page. Summing the page made a
  // filtered subset able to exceed the unfiltered total -- selecting one channel scoped the
  // list under the cap and therefore counted every row, while "all" counted 200 of 442.
  // Missing half the rows is a far larger error than the unpriced rows the client-side
  // registry fallback used to patch, and the pricing warning card already surfaces those.
  const estimatedCost = totals.estimated_cost
  const signals = (anomalies.data ?? []).slice(0, 5)
  return <div className="finops-grid">
    <section className="finops-kpis span-3">
      <Kpi label="总 Token" value={compact.format(totals.total_tokens)} change={overview.data.changes_percent.total_tokens} icon={Zap} />
      <Kpi label="总调用次数" value={compact.format(totals.total_requests)} change={overview.data.changes_percent.total_requests} icon={Activity} />
      <Kpi label="估算成本" value={!costAvailable ? "未计价" : currency.format(estimatedCost)} change={null} note={costAvailable ? "按入库时单价结算" : undefined} icon={CircleDollarSign} />
      <Kpi label="平均延迟" value={formatLatency(totals.average_latency_ms)} change={overview.data.changes_percent.average_latency_ms} icon={Clock3} />
      <Kpi label="错误率" value={`${totals.error_rate.toFixed(1)}%`} change={overview.data.changes_percent.error_rate} icon={ShieldAlert} />
    </section>
    <UsageActivityCard filters={filters} costAvailable={costAvailable} />
    <DistributionRankingPanel className="span-2" title="运行时用量结构" meta={`${runtimes.data?.items.length ?? 0} 个运行时`} items={runtimes.data?.items ?? []} costAvailable={costAvailable} metricMode="auto" emptyTitle="暂无运行时数据" emptyDetail="当前范围暂无用量数据。" />
    <section className="finops-panel"><PanelTitle title="重点治理信号" meta={`${signals.length} 项`} />{signals.length ? <div className="anomaly-list">{signals.map((item) => <div key={item.id}><span className={`anomaly-dot ${item.severity}`} /><div><b>{item.title}</b><p>{item.description}</p></div><strong>{item.actual_value.toFixed(1)}</strong></div>)}</div> : <EmptyState title="暂无异常" detail="当前范围未触发治理阈值。" />}</section>
    <DistributionRankingPanel className="overview-distribution-panel" title="部门用量结构" meta={`${departments.data?.items.length ?? 0} 个部门`} items={departments.data?.items ?? []} costAvailable={costAvailable} metricMode="auto" emptyTitle="暂无部门数据" emptyDetail="当前范围暂无用量数据。" />
    <DistributionRankingPanel className="overview-distribution-panel" title="模型用量结构" meta={`${new Set(models.data?.items.map((item) => item.name) ?? []).size} 个模型`} items={models.data?.items ?? []} costAvailable={costAvailable} metricMode="auto" emptyTitle="暂无模型数据" emptyDetail="当前范围暂无用量数据。" />
    <section className="finops-panel"><PanelTitle title="请求健康度" meta={`P95 ${formatLatency(totals.p95_latency_ms)}`} /><RequestHealthChart totals={totals} /></section>
    <div className="overview-ranking-grid span-3">
      <DistributionRankingPanel title="智能体 Token 排行" meta={`${agents.data?.items.length ?? 0} 个智能体`} items={agents.data?.items ?? []} costAvailable={costAvailable} metricMode="tokens" color="var(--chart-1)" emptyTitle="暂无智能体数据" emptyDetail="当前范围暂无用量数据。" />
      <DistributionRankingPanel title="人员 Token 排行" meta={`${users.data?.items.length ?? 0} 人`} items={users.data?.items ?? []} costAvailable={costAvailable} metricMode="tokens" color="var(--success)" emptyTitle="暂无人员数据" emptyDetail="当前范围暂无用量数据。" />
      <DistributionRankingPanel title="智能体调用次数排行" meta={`${agents.data?.items.length ?? 0} 个智能体`} items={agents.data?.items ?? []} costAvailable={costAvailable} metricMode="requests" color="var(--warning)" emptyTitle="暂无智能体数据" emptyDetail="当前范围暂无调用数据。" />
    </div>
  </div>
}

function Kpi({ label, value, change, note, icon: Icon }: { label: string; value: string; change: number | null; note?: string; icon: typeof Activity }) {
  return <div><span className="finops-kpi-icon"><Icon size={16} /></span><label>{label}</label><strong>{value}</strong><small className={change != null && change > 0 ? "up" : ""}>{note ?? (change == null ? "无上期数据" : `${change > 0 ? "+" : ""}${change.toFixed(1)}% 较上期`)}</small></div>
}

function RankingLimitSelect({ value, onChange, label }: { value: RankingLimit; onChange: (value: RankingLimit) => void; label: string }) {
  const { locale } = useLocale()
  const displayCountLabel = rankingDisplayCountLabel(locale)
  return <Select value={String(value)} onValueChange={(next) => next && onChange(Number(next) as RankingLimit)}>
    <SelectTrigger size="sm" className="ranking-limit-trigger" aria-label={`${label} · ${displayCountLabel}`}><SelectValue>{rankingLimitLabel(locale, value)}</SelectValue></SelectTrigger>
    <SelectContent align="end" alignItemWithTrigger={false}>{rankingLimits.map((limit) => <SelectItem key={limit} value={String(limit)}>{rankingLimitLabel(locale, limit)}</SelectItem>)}</SelectContent>
  </Select>
}

function rankingLimitLabel(locale: LocalePreference, limit: RankingLimit) {
  if (locale === "en") return `Top ${limit}`
  if (locale === "ko") return `상위 ${limit}`
  if (locale === "ja") return `上位 ${limit}`
  return `前 ${limit}`
}

function rankingDisplayCountLabel(locale: LocalePreference) {
  if (locale === "en") return "Items shown"
  if (locale === "ko") return "표시 개수"
  if (locale === "ja") return "表示件数"
  if (locale === "zh-TW") return "顯示數量"
  return "显示数量"
}

/* Both charts read the server's window aggregate. They used to count a capped 200-row page,
   which silently drew a histogram of the most recent 200 requests under a full-window label. */
function RequestHealthChart({ totals }: { totals: ExecutiveTotals | undefined }) {
  const data = [
    { name: "成功", value: totals?.success_requests ?? 0, color: "var(--chart-1)" },
    { name: "客户端错误", value: totals?.client_error_requests ?? 0, color: "var(--warning)" },
    { name: "网关错误", value: totals?.server_error_requests ?? 0, color: "var(--destructive)" },
  ].filter((item) => item.value > 0)
  if (data.length === 0) return <EmptyState title="暂无请求" detail="当前范围暂无调用数据。" />
  return <div className="request-health-chart finops-chart-surface">
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
        <CartesianGrid vertical={false} stroke="var(--border)" />
        <XAxis dataKey="name" tickLine={false} axisLine={false} tickMargin={8} />
        <YAxis tickLine={false} axisLine={false} tickMargin={8} width={40} allowDecimals={false} />
        <Tooltip cursor={false} content={<FinOpsChartTooltip nameFormatter={() => "请求数"} valueFormatter={(value) => `${Number(value).toLocaleString(getIntlLocale())} 次`} />} />
        <Bar dataKey="value" radius={[3, 3, 0, 0]} maxBarSize={56} isAnimationActive={false}>
          {data.map((item) => <Cell key={item.name} fill={item.color} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  </div>
}

function RequestVolumeTrendChart({ rows }: { rows: Array<{ day: string; calls: number }> }) {
  if (!rows.length) return <EmptyState title="暂无调用趋势" detail="当前范围暂无请求记录。" />
  return <div className="request-health-chart finops-chart-surface">
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={rows} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
        <CartesianGrid vertical={false} stroke="var(--border)" />
        <XAxis dataKey="day" tickLine={false} axisLine={false} tickMargin={8} interval="preserveStartEnd" />
        <YAxis tickLine={false} axisLine={false} tickMargin={8} width={40} allowDecimals={false} tickFormatter={(value) => compact.format(Number(value))} />
        <Tooltip cursor={false} content={<FinOpsChartTooltip nameFormatter={() => "调用次数"} valueFormatter={(value) => `${Number(value).toLocaleString(getIntlLocale())} 次`} />} />
        <Line dataKey="calls" type="linear" stroke="var(--chart-1)" strokeWidth={2} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />
      </ComposedChart>
    </ResponsiveContainer>
  </div>
}

/* Per-band colour, the same way the status chart beside it colours its outcomes, and for
   the same reason: one flat fill made the four bands look like four readings of one thing.

   What the colour encodes is the threshold, not the order -- the axis already spells the
   order out, so repeating it would add nothing, while "which of these is a problem" is a
   judgement the labels do not carry. This panel's own meta counts `≥ 2 s` as slow and the
   product ships an anomaly rule at the same line, so 2 s is where brand gives way to
   warning. The two bands under it differ by intensity rather than hue because they are
   both inside target; the tint is derived from --chart-1 exactly as the activity heatmap
   derives its levels, since the palette no longer holds a lighter blue as a token.

   That mix must go to `transparent`, never to `var(--background)`: oklch interpolates hue,
   the background is achromatic and records hue 0, so mixing blue toward it travels through
   purple and lands on #b9a2de. Measured, not guessed -- it reads as a fifth unrelated
   series rather than as a lighter blue.

   75% is the lightest step that still clears 3:1 against the card in BOTH themes
   (#5995d9, 3.12 light / 4.54 dark) while staying ΔE 20.5 away from the full brand blue.
   55% looked right and measured 2.24, i.e. below the floor the rest of the palette holds
   to. Anything above 80% buys contrast by collapsing the gap to the neighbouring bar. */
function RequestLatencyChart({ totals, percentage = false }: { totals: ExecutiveTotals | undefined; percentage?: boolean }) {
  const counts = [
    { name: "< 1 秒", value: totals?.latency_under_1s ?? 0, color: "var(--chart-1)" },
    { name: "1–2 秒", value: totals?.latency_1_to_2s ?? 0, color: "color-mix(in oklch, var(--chart-1) 75%, transparent)" },
    { name: "2–5 秒", value: totals?.latency_2_to_5s ?? 0, color: "var(--warning)" },
    { name: "≥ 5 秒", value: totals?.latency_over_5s ?? 0, color: "var(--destructive)" },
  ]
  const total = counts.reduce((sum, item) => sum + item.value, 0)
  const data = percentage
    ? counts.map((item) => ({ ...item, value: total ? item.value / total * 100 : 0 }))
    : counts
  return <div className="request-health-chart finops-chart-surface">
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
        <CartesianGrid vertical={false} stroke="var(--border)" />
        <XAxis dataKey="name" tickLine={false} axisLine={false} tickMargin={8} />
        <YAxis tickLine={false} axisLine={false} tickMargin={8} width={40} allowDecimals={!percentage} domain={percentage ? [0, 100] : undefined} tickFormatter={percentage ? (value) => `${Number(value)}%` : undefined} />
        <Tooltip cursor={false} content={<FinOpsChartTooltip nameFormatter={() => percentage ? "请求占比" : "请求数"} valueFormatter={(value) => percentage ? `${decimal.format(Number(value))}%` : `${Number(value).toLocaleString(getIntlLocale())} 次`} />} />
        <Bar dataKey="value" radius={[3, 3, 0, 0]} maxBarSize={56} isAnimationActive={false}>
          {data.map((item) => <Cell key={item.name} fill={item.color} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  </div>
}

type DistributionRankingItem = { id: string; name: string; total_tokens: number; cache_read_tokens?: number; total_requests: number; estimated_cost: number; share_percent: number }

function tokenRankingTooltip(value: string | number, payload?: unknown) {
  const row = payload as DistributionRankingItem | undefined
  const total = Number(value).toLocaleString(getIntlLocale())
  const cacheRead = Number(row?.cache_read_tokens ?? 0).toLocaleString(getIntlLocale())
  return `${total} tokens，其中缓存读取 ${cacheRead} tokens`
}

function DistributionRankingPanel({ title, meta, items, costAvailable, metricMode = "tokens", color, className, emptyTitle, emptyDetail }: {
  title: string
  meta: string
  items: DistributionRankingItem[]
  costAvailable: boolean
  metricMode?: "auto" | "tokens" | "requests"
  color?: string
  className?: string
  emptyTitle: string
  emptyDetail: string
}) {
  const [limit, setLimit] = useState<RankingLimit>(5)
  return <section className={`finops-panel${className ? ` ${className}` : ""}`}>
    <PanelTitle title={title} meta={meta} action={<RankingLimitSelect value={limit} onChange={setLimit} label={title} />} />
    {items.length ? <DistributionBars items={items} costAvailable={costAvailable} metricMode={metricMode} color={color} limit={limit} /> : <EmptyState title={emptyTitle} detail={emptyDetail} />}
  </section>
}

function ModelMixPanel({ title, meta, className, rows, series, metric, emptyTitle, emptyDetail }: {
  title: string
  meta: string
  className?: string
  rows: ModelMixRow[]
  series: ModelSeries[]
  metric: ModelMixMetric
  emptyTitle: string
  emptyDetail: string
}) {
  const [limit, setLimit] = useState<RankingLimit>(5)
  return <section className={`finops-panel${className ? ` ${className}` : ""}`}>
    <PanelTitle title={title} meta={meta} action={rows.length ? <RankingLimitSelect value={limit} onChange={setLimit} label={title} /> : undefined} />
    {rows.length ? <ModelMixChart rows={rows.slice(0, limit)} series={series} metric={metric} /> : <EmptyState title={emptyTitle} detail={emptyDetail} />}
  </section>
}

function MetricRankingPanel({ title, meta, className, rows, valueLabel, valueFormatter, color, domain, sortAscending, emptyTitle, emptyDetail }: {
  title: string
  meta: string
  className?: string
  rows: Array<{ name: string; value: number }>
  valueLabel: string
  valueFormatter: (value: number) => string
  color: string
  domain?: [number, number]
  sortAscending?: boolean
  emptyTitle?: string
  emptyDetail?: string
}) {
  const [limit, setLimit] = useState<RankingLimit>(5)
  const showEmpty = rows.length === 0 && emptyTitle !== undefined
  return <section className={`finops-panel${className ? ` ${className}` : ""}`}>
    <PanelTitle title={title} meta={meta} action={showEmpty ? undefined : <RankingLimitSelect value={limit} onChange={setLimit} label={title} />} />
    {showEmpty
      ? <EmptyState title={emptyTitle} detail={emptyDetail ?? ""} />
      : <HorizontalMetricChart rows={rows} limit={limit} valueLabel={valueLabel} valueFormatter={valueFormatter} color={color} domain={domain} sortAscending={sortAscending} />}
  </section>
}

function DistributionBars({ items, costAvailable, metricMode = "auto", color, limit = 5 }: { items: DistributionRankingItem[]; costAvailable: boolean; metricMode?: "auto" | "tokens" | "requests"; color?: string; limit?: RankingLimit }) {
  const grouped = new Map<string, { id: string; name: string; total_tokens: number; cache_read_tokens: number; total_requests: number; estimated_cost: number; share_percent: number }>()
  for (const item of items) {
    const current = grouped.get(item.name) ?? { ...item, id: item.name, total_tokens: 0, cache_read_tokens: 0, total_requests: 0, estimated_cost: 0, share_percent: 0 }
    current.total_tokens += item.total_tokens
    current.cache_read_tokens += item.cache_read_tokens ?? 0
    current.total_requests += item.total_requests
    current.estimated_cost += item.estimated_cost
    current.share_percent += item.share_percent
    grouped.set(item.name, current)
  }
  const merged = [...grouped.values()]
  const useRequests = metricMode === "requests"
  const useCost = metricMode === "auto" && costAvailable && merged.some((item) => item.estimated_cost > 0)
  const metric = useRequests ? "total_requests" : useCost ? "estimated_cost" : "total_tokens"
  const rows = merged.sort((left, right) => right[metric] - left[metric]).slice(0, limit)
  const [chartRef, chartWidth] = useChartWidthKey()
  const labelWidth = categoryAxisWidth(rows.map((item) => item.name), chartWidth)
  return <div ref={chartRef} className="distribution-chart finops-chart-surface" style={{ minHeight: Math.max(260, rows.length * 34 + 48) }}>
    <ResponsiveContainer key={chartWidth} width="100%" height="100%">
      <BarChart data={rows} layout="vertical" barCategoryGap="22%" margin={{ left: 0, right: 16, top: 4, bottom: 0 }}>
        <CartesianGrid horizontal={false} stroke="var(--border)" />
        <XAxis type="number" height={30} tickLine={false} axisLine={false} tickMargin={8} allowDecimals={!useRequests} tickFormatter={(value) => useCost ? currency.format(Number(value)) : compact.format(Number(value))} />
        <YAxis type="category" dataKey="name" interval={0} tick={<CategoryAxisTick maxWidth={labelWidth - categoryTickGutter} />} tickLine={false} axisLine={false} tickMargin={8} width={labelWidth} />
        <Tooltip
          cursor={false}
          content={<FinOpsChartTooltip
            nameFormatter={() => useRequests ? "调用次数" : useCost ? "费用" : "Token"}
            valueFormatter={(value, _name, payload) => useRequests
              ? `${Number(value).toLocaleString(getIntlLocale())} 次`
              : useCost
                ? currency.format(Number(value))
                : tokenRankingTooltip(value, payload)}
          />}
        />
        <Bar dataKey={metric} fill={color ?? (useRequests ? "var(--chart-2)" : "var(--chart-1)")} radius={[0, 3, 3, 0]} isAnimationActive={false} maxBarSize={26} />
      </BarChart>
    </ResponsiveContainer>
  </div>
}

type ModelMixMetric = "tokens" | "requests"
type ModelSeries = { key: string; label: string; color: string }
type ModelMixRow = { id: string; name: string; total: number; [key: string]: string | number }

/* The four helpers below read the server's ranking instead of raw rows, so every figure
   covers the whole window rather than the most recent 200 requests. `DistributionItem`
   carries the window metrics, and `breakdown` carries the second dimension when the caller
   asked for one, so a stacked chart no longer has to be assembled in the browser. */
function modelSeriesFromDistribution(items: DistributionItem[]): ModelSeries[] {
  return items.map((item, index) => ({
    key: `model_${index}`,
    label: item.name,
    color: `var(--chart-${index % 5 + 1})`,
  }))
}

function modelMixFromDistribution(
  items: DistributionItem[],
  metric: ModelMixMetric,
  series: ModelSeries[],
): ModelMixRow[] {
  const keyByLabel = new Map(series.map((item) => [item.label, item.key]))
  return items
    .map((item) => {
      const row: ModelMixRow = { id: item.id, name: item.name, total: 0 }
      for (const child of item.breakdown) {
        const key = keyByLabel.get(child.name)
        if (!key) continue
        const value = metric === "tokens" ? child.total_tokens : child.total_requests
        row[key] = Number(row[key] ?? 0) + value
        row.total += value
      }
      return row
    })
    .sort((left, right) => right.total - left.total)
}

function statsFromDistribution(items: DistributionItem[]): ModelStat[] {
  return items.map((item) => ({
    name: item.name,
    calls: item.total_requests,
    totalTokens: item.total_tokens,
    averageLatencyMs: item.average_latency_ms,
    successRate: item.total_requests ? item.success_requests / item.total_requests * 100 : 0,
  }))
}

function summarizeDistribution(items: DistributionItem[], metric: ModelMixMetric) {
  const value = (item: DistributionItem) =>
    metric === "tokens" ? item.total_tokens : item.total_requests
  const total = items.reduce((sum, item) => sum + value(item), 0)
  const dominant = [...items].sort((left, right) => value(right) - value(left))[0]
  if (!dominant || total === 0) return "暂无模型用量"
  const unit = metric === "tokens" ? "Token" : "调用"
  return `主导模型 ${dominant.name} · ${(value(dominant) / total * 100).toFixed(1)}% ${unit}`
}

function ModelMixChart({ rows, series, metric }: { rows: ModelMixRow[]; series: ModelSeries[]; metric: ModelMixMetric }) {
  const [chartRef, chartWidth] = useChartWidthKey()
  const { hiddenSeries, toggleSeries } = useSeriesToggle()
  const visibleSeries = series.filter((item) => !hiddenSeries.includes(item.key))
  const labelWidth = categoryAxisWidth(rows.map((item) => item.name), chartWidth)
  return <div ref={chartRef} className="model-mix-chart finops-chart-surface" style={{ minHeight: Math.max(260, rows.length * 34 + 82) }}>
    <ChartSeriesLegend className="model-mix-legend" items={series} hiddenSeries={hiddenSeries} onToggle={toggleSeries} />
    <div className="model-mix-canvas">
      <ResponsiveContainer key={chartWidth} width="100%" height="100%">
        <BarChart data={rows} layout="vertical" barCategoryGap="22%" margin={{ left: 0, right: 16, top: 4, bottom: 0 }}>
          <CartesianGrid horizontal={false} stroke="var(--border)" />
          <XAxis type="number" tickLine={false} axisLine={false} tickMargin={8} allowDecimals={metric === "tokens"} tickFormatter={(value) => metric === "tokens" ? compact.format(Number(value)) : String(value)} />
          <YAxis type="category" dataKey="name" interval={0} tick={<CategoryAxisTick maxWidth={labelWidth - categoryTickGutter} />} tickLine={false} axisLine={false} tickMargin={8} width={labelWidth} />
          <Tooltip cursor={false} content={<FinOpsChartTooltip valueFormatter={(value) => metric === "tokens" ? `${Number(value).toLocaleString(getIntlLocale())} tokens` : `${Number(value).toLocaleString(getIntlLocale())} 次`} />} />
          {visibleSeries.map((item, index) => <Bar key={item.key} dataKey={item.key} name={item.label} stackId="models" fill={item.color} radius={index === visibleSeries.length - 1 ? [0, 3, 3, 0] : 0} isAnimationActive={false} maxBarSize={26} />)}
        </BarChart>
      </ResponsiveContainer>
    </div>
  </div>
}

/* The cross-tab dimension is a control, not a page.
 *
 * These three used to be three hardcoded panels spread across two nav items, chosen for
 * you by whatever the toolbar happened to be scoped to -- so a page titled "departments
 * and projects" showed agents and people once you drilled into a project, and four of its
 * five panels were identical to the neighbouring page. They are one chart with a
 * different grouping key, so the key belongs in the reader's hands.
 *
 * Every label is a complete literal rather than a phrase built from `label`, because the
 * locale provider walks text nodes and can only translate a sentence it can see whole. */
const mixDimensions = [
  {
    id: "department" as const, label: "部门", unit: "个部门",
    rankingTitle: "部门 Token 排行", mixTitle: "部门模型分布",
    heading: "部门 × 模型使用结构",
    emptyTitle: "暂无部门数据", mixEmptyTitle: "暂无部门模型数据",
  },
  {
    id: "agent" as const, label: "智能体", unit: "个智能体",
    rankingTitle: "智能体 Token 排行", mixTitle: "智能体模型分布",
    heading: "智能体 × 模型使用结构",
    emptyTitle: "暂无智能体数据", mixEmptyTitle: "暂无智能体模型数据",
  },
  {
    id: "user" as const, label: "人员", unit: "人",
    rankingTitle: "人员 Token 排行", mixTitle: "人员模型分布",
    heading: "人员 × 模型使用结构",
    emptyTitle: "暂无人员数据", mixEmptyTitle: "暂无人员模型数据",
  },
]

type MixDimension = (typeof mixDimensions)[number]["id"]

function ModelUsage({ filters, costAvailable }: { filters: UsageFilters; costAvailable: boolean }) {
  const [modelMetric, setModelMetric] = useState<ModelMixMetric>("tokens")
  const [dimension, setDimension] = useState<MixDimension>("department")
  const current = mixDimensions.find((item) => item.id === dimension) ?? mixDimensions[0]
  // One split query feeds both the ranking and the stacked chart, so changing the
  // dimension costs a single request and nothing the second time.
  const grouped = useQuery(finopsQueries.distribution(filters, dimension, "model"))
  const models = useQuery(finopsQueries.distribution(filters, "model"))
  const overview = useQuery(finopsQueries.executiveOverview(filters))
  if (grouped.isLoading || models.isLoading || overview.isLoading) return <LoadingState />
  if (grouped.error || models.error || overview.error) return <ErrorState error={grouped.error ?? models.error ?? overview.error} />
  const totals = overview.data?.totals
  const modelSeries = modelSeriesFromDistribution(models.data?.items ?? [])
  const mixRows = modelMixFromDistribution(grouped.data?.items ?? [], modelMetric, modelSeries)
  const modelStats = statsFromDistribution(models.data?.items ?? [])
  const modelSummary = summarizeDistribution(models.data?.items ?? [], modelMetric)
  const totalTokens = totals?.total_tokens ?? 0
  const totalRequests = totals?.total_requests ?? 0
  const averageLatency = totals?.average_latency_ms ?? 0
  const successRate = totalRequests ? (totals?.success_requests ?? 0) / totalRequests * 100 : 0
  return <div className="finops-grid">
    {/* The KPIs describe the whole window rather than the selected dimension, so
        switching that dimension does not silently change what the numbers above the
        fold mean. */}
    <section className="finops-kpis span-3 analytics-kpis">
      <Kpi label="使用模型" value={String(modelSeries.length)} change={null} note="有调用记录" icon={Zap} />
      <Kpi label="Token 总量" value={compact.format(totalTokens)} change={null} note="当前筛选范围" icon={Bot} />
      <Kpi label="调用次数" value={compact.format(totalRequests)} change={null} note={`成功率 ${successRate.toFixed(1)}%`} icon={Activity} />
      <Kpi label="平均 Token / 次" value={totalRequests ? compact.format(totalTokens / totalRequests) : "0"} change={null} note={`${compact.format(totalTokens)} Token`} icon={Workflow} />
      <Kpi label="P95 延迟" value={formatLatency(totals?.p95_latency_ms ?? 0)} change={null} note={`平均 ${formatLatency(averageLatency)}`} icon={Clock3} />
    </section>
    {/* Models take the wide column because they are the one axis every other panel here
        shares -- the cross-tab and both quality charts are model-keyed -- so this column
        stays put while the one beside it changes with the dimension switch. */}
    <DistributionRankingPanel className="span-2" title="模型 Token 排行" meta={`${new Set(models.data?.items.map((item) => item.name) ?? []).size} 个模型`} items={models.data?.items ?? []} costAvailable={costAvailable} emptyTitle="暂无模型数据" emptyDetail="当前筛选范围暂无用量数据。" />
    <DistributionRankingPanel title={current.rankingTitle} meta={`${grouped.data?.items.length ?? 0} ${current.unit}`} items={grouped.data?.items ?? []} costAvailable={costAvailable} emptyTitle={current.emptyTitle} emptyDetail="当前筛选范围暂无用量数据。" />
    <div className="model-mix-heading span-3">
      <div><h2>{current.heading}</h2><span>{modelSummary}</span></div>
      <div className="model-mix-controls">
        <div className="usage-metric-segment" aria-label="交叉维度">
          {mixDimensions.map((item) => <button
            key={item.id}
            type="button"
            className={dimension === item.id ? "active" : ""}
            aria-pressed={dimension === item.id}
            onClick={() => setDimension(item.id)}
          >{item.label}</button>)}
        </div>
        <div className="usage-metric-segment" aria-label="模型分布指标">
          <button type="button" className={modelMetric === "tokens" ? "active" : ""} onClick={() => setModelMetric("tokens")}>Token</button>
          <button type="button" className={modelMetric === "requests" ? "active" : ""} onClick={() => setModelMetric("requests")}>调用次数</button>
        </div>
      </div>
    </div>
    <ModelMixPanel className="span-2" title={current.mixTitle} meta={`${mixRows.length} ${current.unit}`} rows={mixRows} series={modelSeries} metric={modelMetric} emptyTitle={current.mixEmptyTitle} emptyDetail="当前筛选范围暂无用量数据。" />
    <div className="analytics-quality-stack">
      <MetricRankingPanel title="模型平均延迟" meta={`整体 ${formatLatency(averageLatency)}`} rows={modelStats.map((item) => ({ name: item.name, value: item.averageLatencyMs }))} valueLabel="平均延迟" valueFormatter={(value) => formatLatency(value)} color="var(--chart-2)" />
      <MetricRankingPanel title="模型成功率" meta={`整体 ${successRate.toFixed(1)}%`} rows={modelStats.map((item) => ({ name: item.name, value: item.successRate }))} valueLabel="成功率" valueFormatter={(value) => `${value.toFixed(1)}%`} color="var(--success)" domain={[0, 100]} />
    </div>
  </div>
}

type ModelStat = { name: string; calls: number; totalTokens: number; averageLatencyMs: number; successRate: number }

function HorizontalMetricChart({ rows, limit = 5, valueLabel, valueFormatter, color, domain, sortAscending = false }: {
  rows: Array<{ name: string; value: number }>
  limit?: RankingLimit
  valueLabel: string
  valueFormatter: (value: number) => string
  color: string
  domain?: [number, number]
  sortAscending?: boolean
}) {
  const sortedRows = [...rows].sort((left, right) => sortAscending ? left.value - right.value : right.value - left.value).slice(0, limit)
  const [chartRef, chartWidth] = useChartWidthKey()
  const labelWidth = categoryAxisWidth(sortedRows.map((item) => item.name), chartWidth)
  return <div ref={chartRef} className="model-quality-chart finops-chart-surface" style={{ minHeight: Math.max(174, sortedRows.length * 30 + 56) }}>
    <ResponsiveContainer key={chartWidth} width="100%" height="100%">
      <BarChart data={sortedRows} layout="vertical" barCategoryGap="22%" margin={{ left: 0, right: 12, top: 4, bottom: 0 }}>
        <CartesianGrid horizontal={false} stroke="var(--border)" />
        <XAxis type="number" domain={domain} tickLine={false} axisLine={false} tickMargin={8} tickFormatter={(value) => valueFormatter(Number(value))} />
        <YAxis type="category" dataKey="name" interval={0} tick={<CategoryAxisTick maxWidth={labelWidth - categoryTickGutter} />} tickLine={false} axisLine={false} tickMargin={8} width={labelWidth} />
        <Tooltip cursor={false} content={<FinOpsChartTooltip nameFormatter={() => valueLabel} valueFormatter={(value) => valueFormatter(Number(value))} />} />
        <Bar dataKey="value" fill={color} radius={[0, 3, 3, 0]} isAnimationActive={false} maxBarSize={20} />
      </BarChart>
    </ResponsiveContainer>
  </div>
}

type TrendInterval = "hour" | "day" | "week"
type TrendPoint = TrendApiResponse["points"][number]
type TrendContribution = { name: string; value: number; share: number }
type RequestQualityPoint = { day: string; calls: number; p95Latency: number }
type GovernanceRiskPoint = { day: string; errorRate: number; p95Latency: number }

const trendDimensionLabels: Record<TrendDimension, string> = {
  none: "全部",
  organization: "组织",
  department: "部门",
  project: "项目",
  agent: "智能体",
  model: "模型",
  user: "用户",
  runtime: "运行时",
  workflow: "工作流",
  team: "团队",
}

const trendMetricLabels: Record<TrendMetric, string> = {
  total_tokens: "Token",
  calls: "调用次数",
  et: "ET",
}

const trendIntervalLabels: Record<TrendInterval, string> = {
  hour: "小时",
  day: "每日",
  week: "每周",
}

function UsageTrends({ filters }: { filters: UsageFilters }) {
  const [metric, setMetric] = useState<TrendMetric>("total_tokens")
  const [interval, setInterval] = useState<TrendInterval>("hour")
  const [contributionLimit, setContributionLimit] = useState<RankingLimit>(5)
  const { hiddenSeries: hiddenTrendSeries, toggleSeries: toggleTrendSeries } = useSeriesToggle()
  const dimension = trendDimensionForFilters(filters)
  const query = useQuery(finopsQueries.trends(filters, dimension, interval))
  // Success rate, tail latency and the quality chart describe the whole window, so they read
  // server aggregates. `none` collapses each bucket to one point because a percentile cannot
  // be merged across the comparison series the main chart is grouped by.
  const overview = useQuery(finopsQueries.executiveOverview(filters))
  const qualityBuckets = useQuery(finopsQueries.trends(filters, "none", interval))
  if (query.isLoading || overview.isLoading || qualityBuckets.isLoading) return <LoadingState />
  if (query.error || overview.error || qualityBuckets.error) return <ErrorState error={query.error ?? overview.error ?? qualityBuckets.error} />

  const points = query.data?.points ?? []
  if (!points.length) return <section className="finops-panel"><PanelTitle title="用量趋势" /><EmptyState title="暂无趋势数据" detail="当前范围暂无用量数据。" /></section>

  const totals = overview.data?.totals
  const contributions = summarizeTrendContributions(points, metric)
  const leadingSeries = contributions.slice(0, 5).map((item) => item.name)
  const series = contributions.length > leadingSeries.length ? [...leadingSeries, "其他"] : leadingSeries
  const visibleTrendSeries = series
    .map((key, index) => ({ key, color: trendSeriesColor(key, index) }))
    .filter((entry) => !hiddenTrendSeries.includes(entry.key))
  const data = pivotPoints(points, metric, interval, new Set(leadingSeries))
  const buckets = summarizeTrendBuckets(points, metric)
  const peak = buckets.reduce((current, item) => item.value > current.value ? item : current, buckets[0])
  const total = contributions.reduce((sum, item) => sum + item.value, 0)
  const topContributor = contributions[0]
  const divisor = metric === "calls" ? buckets.length : (totals?.total_requests ?? 0)
  const average = divisor ? total / divisor : 0
  const successRate = totals?.total_requests
    ? totals.success_requests / totals.total_requests * 100
    : 0
  const p95Latency = totals?.p95_latency_ms ?? 0
  const qualityData: RequestQualityPoint[] = (qualityBuckets.data?.points ?? []).map((point) => ({
    day: formatTrendBucket(point.bucket_start, interval),
    calls: point.totals.calls,
    p95Latency: point.totals.p95_latency_ms,
  }))
  const averageLabel = metric === "calls" ? "平均调用 / 时段" : metric === "total_tokens" ? "平均 Token / 次" : "平均 ET / 次"
  return <div className="finops-grid trends-dashboard">
    <div className="trend-toolbar span-3" aria-label="趋势视图控制">
      <span className="trend-toolbar-label">趋势视图</span>
      <div className="trend-toolbar-controls">
        <div className="trend-control-group">
          <span>指标</span>
          <div className="trend-segment" role="group" aria-label="趋势指标">
            {(["total_tokens", "calls", "et"] as TrendMetric[]).map((value) => <button key={value} type="button" className={metric === value ? "active" : ""} aria-pressed={metric === value} onClick={() => setMetric(value)}>{trendMetricLabels[value]}</button>)}
          </div>
        </div>
        <div className="trend-control-group">
          <span>粒度</span>
          <div className="trend-segment" role="group" aria-label="趋势粒度">
            {(["hour", "day", "week"] as TrendInterval[]).map((value) => <button key={value} type="button" className={interval === value ? "active" : ""} aria-pressed={interval === value} onClick={() => setInterval(value)}>{trendIntervalLabels[value]}</button>)}
          </div>
        </div>
      </div>
    </div>
    <section className="finops-kpis span-3 trends-kpis">
      <Kpi label={`总${trendMetricLabels[metric]}`} value={formatTrendMetric(metric, total)} change={null} note={`${(totals?.total_requests ?? 0).toLocaleString(getIntlLocale())} 次调用`} icon={Activity} />
      <Kpi label={averageLabel} value={formatTrendMetric(metric, average)} change={null} note={metric === "calls" ? `${buckets.length} 个活跃时段` : "当前筛选范围"} icon={Zap} />
      <Kpi label="峰值时段" value={formatTrendBucket(peak.bucket, interval)} change={null} note={formatTrendMetric(metric, peak.value)} icon={Clock3} />
      <Kpi label={`主要${trendDimensionLabels[dimension]}`} value={topContributor.name} change={null} note={`${decimal.format(topContributor.share)}% 用量占比`} icon={Workflow} />
      <Kpi label="请求成功率" value={`${decimal.format(successRate)}%`} change={null} note={`P95 ${formatLatency(p95Latency)}`} icon={ShieldAlert} />
    </section>
    <section className="finops-panel span-3 trend-main-panel">
      <PanelTitle title="用量趋势" meta={`按${trendDimensionLabels[dimension]}对比`} />
      <ChartSeriesLegend
        className="trend-series-legend"
        items={series.map((key, index) => ({ key, label: key, color: trendSeriesColor(key, index) }))}
        hiddenSeries={hiddenTrendSeries}
        onToggle={toggleTrendSeries}
      />
      <div className="trend-main-chart finops-chart-surface">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--border)" />
            <XAxis dataKey="day" axisLine={false} tickLine={false} tickMargin={8} interval="preserveStartEnd" />
            <YAxis axisLine={false} tickLine={false} tickMargin={8} width={56} tickFormatter={(value) => compact.format(Number(value))} />
            <Tooltip cursor={false} content={<FinOpsChartTooltip valueFormatter={(value) => formatTrendMetric(metric, Number(value), true)} />} />
            {visibleTrendSeries.map((entry, index) => <Bar key={entry.key} dataKey={entry.key} stackId="usage" fill={entry.color} isAnimationActive={false} maxBarSize={54} radius={index === visibleTrendSeries.length - 1 ? [3, 3, 0, 0] : 0} />)}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </section>
    <section className="finops-panel trend-contribution-panel">
      <PanelTitle title={`${trendDimensionLabels[dimension]}贡献排行`} meta={`${contributions.length} 项`} action={<RankingLimitSelect value={contributionLimit} onChange={setContributionLimit} label={`${trendDimensionLabels[dimension]}贡献排行`} />} />
      <TrendContributionChart rows={contributions} metric={metric} limit={contributionLimit} />
    </section>
    <section className="finops-panel span-2 trend-quality-panel">
      <PanelTitle title="请求质量趋势" meta={`整体成功率 ${decimal.format(successRate)}%`} />
      {qualityData.length ? <RequestQualityTrendChart rows={qualityData} /> : <EmptyState title="暂无请求质量数据" detail="当前范围暂无请求记录。" />}
    </section>
  </div>
}

function summarizeTrendContributions(points: TrendPoint[], metric: TrendMetric): TrendContribution[] {
  const totals = new Map<string, number>()
  for (const point of points) totals.set(point.label, (totals.get(point.label) ?? 0) + point.totals[metric])
  const total = [...totals.values()].reduce((sum, value) => sum + value, 0)
  return [...totals.entries()]
    .map(([name, value]) => ({ name, value, share: total ? value / total * 100 : 0 }))
    .sort((left, right) => right.value - left.value)
}

function summarizeTrendBuckets(points: TrendPoint[], metric: TrendMetric) {
  const totals = new Map<string, number>()
  for (const point of points) totals.set(point.bucket_start, (totals.get(point.bucket_start) ?? 0) + point.totals[metric])
  return [...totals.entries()].map(([bucket, value]) => ({ bucket, value })).sort((left, right) => left.bucket.localeCompare(right.bucket))
}

function pivotPoints(points: TrendPoint[], metric: TrendMetric, interval: TrendInterval, leadingSeries: Set<string>) {
  const rows = new Map<string, Record<string, string | number>>()
  for (const point of points) {
    const key = leadingSeries.has(point.label) ? point.label : "其他"
    const row = rows.get(point.bucket_start) ?? { day: formatTrendBucket(point.bucket_start, interval) }
    row[key] = Number(row[key] ?? 0) + point.totals[metric]
    rows.set(point.bucket_start, row)
  }
  return [...rows.values()]
}

function formatTrendMetric(metric: TrendMetric, value: number, withUnit = false) {
  if (metric === "et") return decimal.format(value)
  if (metric === "calls") return withUnit ? `${Math.round(value).toLocaleString(getIntlLocale())} 次` : compact.format(value)
  return withUnit ? `${Math.round(value).toLocaleString(getIntlLocale())} tokens` : compact.format(value)
}

function formatTrendBucket(timestamp: string, interval: TrendInterval) {
  const date = new Date(timestamp)
  const locale = getIntlLocale()
  if (interval === "hour") return date.toLocaleString(locale, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    ...(locale === "en-US" ? { minute: "2-digit" as const } : {}),
    hour12: false,
    timeZone: "UTC",
  })
  return date.toLocaleDateString(getIntlLocale(), { month: "2-digit", day: "2-digit", timeZone: "UTC" })
}

function trendSeriesColor(key: string, index: number) {
  return key === "其他" ? "var(--muted-foreground)" : `var(--chart-${(index % 5) + 1})`
}

function TrendContributionChart({ rows, metric, limit }: { rows: TrendContribution[]; metric: TrendMetric; limit: RankingLimit }) {
  const data = rows.slice(0, limit)
  const [chartRef, chartWidth] = useChartWidthKey()
  const labelWidth = categoryAxisWidth(data.map((item) => item.name), chartWidth)
  return <div ref={chartRef} className="trend-contribution-chart finops-chart-surface" style={{ minHeight: Math.max(280, data.length * 32 + 56) }}>
    <ResponsiveContainer key={chartWidth} width="100%" height="100%">
      <BarChart data={data} layout="vertical" barCategoryGap="22%" margin={{ left: 0, right: 12, top: 8, bottom: 0 }}>
        <CartesianGrid horizontal={false} stroke="var(--border)" />
        <XAxis type="number" axisLine={false} tickLine={false} tickMargin={8} tickFormatter={(value) => compact.format(Number(value))} />
        <YAxis type="category" dataKey="name" interval={0} tick={<CategoryAxisTick maxWidth={labelWidth - categoryTickGutter} />} axisLine={false} tickLine={false} tickMargin={8} width={labelWidth} />
        <Tooltip cursor={false} content={<FinOpsChartTooltip nameFormatter={() => trendMetricLabels[metric]} valueFormatter={(value) => formatTrendMetric(metric, Number(value), true)} />} />
        <Bar dataKey="value" fill="var(--chart-1)" radius={[0, 3, 3, 0]} maxBarSize={22} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  </div>
}

function RequestQualityTrendChart({ rows }: { rows: RequestQualityPoint[] }) {
  const { hiddenSeries, toggleSeries, isHidden } = useSeriesToggle()
  const showCalls = !isHidden("calls")
  const showLatency = !isHidden("latency")
  return <div className="trend-quality-chart finops-chart-surface">
    <ChartSeriesLegend
      className="trend-quality-legend"
      items={[{ key: "calls", label: "调用次数", markClassName: "calls" }, { key: "latency", label: "P95 时延", markClassName: "latency" }]}
      hiddenSeries={hiddenSeries}
      onToggle={toggleSeries}
    />
    <div className="trend-quality-canvas">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={rows} margin={{ left: 0, right: 4, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey="day" axisLine={false} tickLine={false} tickMargin={8} interval="preserveStartEnd" />
          {showCalls && <YAxis yAxisId="calls" axisLine={false} tickLine={false} tickMargin={8} width={40} allowDecimals={false} />}
          {showLatency && <YAxis yAxisId="latency" orientation="right" axisLine={false} tickLine={false} tickMargin={8} width={56} tickFormatter={(value) => formatLatency(Number(value))} />}
          <Tooltip cursor={false} content={<FinOpsChartTooltip valueFormatter={(value, name) => name === "P95 时延" ? formatLatency(Number(value)) : `${Number(value).toLocaleString(getIntlLocale())} 次`} />} />
          {showCalls && <Bar yAxisId="calls" dataKey="calls" name="调用次数" fill="var(--chart-1)" radius={[3, 3, 0, 0]} maxBarSize={42} isAnimationActive={false} />}
          {/* linear, not monotone: a percentile is measured per bucket and a curve between
              two of them is invented. Same rule as the usage activity chart. */}
          {showLatency && <Line yAxisId="latency" dataKey="p95Latency" name="P95 时延" type="linear" stroke="var(--chart-2)" strokeWidth={2} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  </div>
}

function GovernanceRiskTrendChart({ rows }: { rows: GovernanceRiskPoint[] }) {
  const { hiddenSeries, toggleSeries, isHidden } = useSeriesToggle()
  const showErrors = !isHidden("errors")
  const showLatency = !isHidden("latency")
  return <div className="governance-risk-chart finops-chart-surface">
    <ChartSeriesLegend
      className="governance-chart-legend"
      items={[{ key: "errors", label: "错误率", markClassName: "errors" }, { key: "latency", label: "P95 时延", markClassName: "latency" }]}
      hiddenSeries={hiddenSeries}
      onToggle={toggleSeries}
    />
    <div className="governance-risk-canvas">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={rows} margin={{ left: 0, right: 4, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey="day" axisLine={false} tickLine={false} tickMargin={8} interval="preserveStartEnd" />
          {showErrors && <YAxis yAxisId="errors" axisLine={false} tickLine={false} tickMargin={8} width={44} tickFormatter={(value) => `${decimal.format(Number(value))}%`} />}
          {showLatency && <YAxis yAxisId="latency" orientation="right" axisLine={false} tickLine={false} tickMargin={8} width={56} tickFormatter={(value) => formatLatency(Number(value))} />}
          <Tooltip cursor={false} content={<FinOpsChartTooltip valueFormatter={(value, name) => name === "P95 时延" ? formatLatency(Number(value)) : `${decimal.format(Number(value))}%`} />} />
          {showErrors && <Bar yAxisId="errors" dataKey="errorRate" name="错误率" fill="var(--destructive)" radius={[3, 3, 0, 0]} maxBarSize={42} isAnimationActive={false} />}
          {showLatency && <Line yAxisId="latency" dataKey="p95Latency" name="P95 时延" type="linear" stroke="var(--chart-2)" strokeWidth={2} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  </div>
}

function Governance({ filters, entities, models, onOpenRequest }: { filters: UsageFilters; entities: EnterpriseEntityCatalog; models: ManagedModel[]; onOpenRequest: (requestId: string) => void }) {
  const rules = useQuery(finopsQueries.anomalyRules())
  // Every figure below describes the whole window, so each comes from a server aggregate:
  // totals for the KPIs and the two histograms, the model distribution for per-model success
  // and failure counts, and an ungrouped daily trend for the risk chart. The request page is
  // still fetched, but only so the signal dialog can show the actual rows behind a signal.
  const overview = useQuery(finopsQueries.executiveOverview(filters))
  const anomalies = useQuery(finopsQueries.anomalies(filters))
  const modelUsage = useQuery(finopsQueries.distribution(filters, "model"))
  const riskBuckets = useQuery(finopsQueries.trends(filters, "none", "day"))
  const requests = useQuery(finopsQueries.requests(filters))
  const [selectedSignal, setSelectedSignal] = useState<UsageAnomaly | null>(null)
  if (rules.isLoading || overview.isLoading || anomalies.isLoading || modelUsage.isLoading || riskBuckets.isLoading || requests.isLoading) return <LoadingState />
  if (rules.error || overview.error || anomalies.error || modelUsage.error || riskBuckets.error || requests.error) return <ErrorState error={rules.error ?? overview.error ?? anomalies.error ?? modelUsage.error ?? riskBuckets.error ?? requests.error} />
  const requestRows = priceRequests(requests.data ?? [], models)
  const totals = overview.data?.totals
  const signals = anomalies.data ?? []
  const modelRows = modelUsage.data?.items ?? []
  const failedRequests = (totals?.client_error_requests ?? 0) + (totals?.server_error_requests ?? 0)
  const slowRequests = (totals?.latency_2_to_5s ?? 0) + (totals?.latency_over_5s ?? 0)
  const criticalSignals = signals.filter((item) => item.severity === "critical").length
  const warningSignals = signals.filter((item) => item.severity === "warning").length
  const errorRate = totals?.error_rate ?? 0
  const p95Latency = totals?.p95_latency_ms ?? 0
  const modelFailures = modelRows.map((item) => ({
    name: item.name,
    value: item.client_error_requests + item.server_error_requests,
  }))
  const affectedModels = modelFailures.filter((item) => item.value > 0).length
  const modelStats = modelRows.map((item) => ({
    name: item.name,
    successRate: item.total_requests ? item.success_requests / item.total_requests * 100 : 0,
  }))
  const failureSources = modelFailures
    .filter((item) => item.value > 0)
    .sort((left, right) => right.value - left.value)
  const riskTrend: GovernanceRiskPoint[] = (riskBuckets.data?.points ?? []).map((point) => ({
    day: new Date(point.bucket_start).toLocaleDateString(getIntlLocale(), { month: "numeric", day: "numeric" }),
    errorRate: point.totals.calls ? point.totals.failed_calls / point.totals.calls * 100 : 0,
    p95Latency: point.totals.p95_latency_ms,
  }))
  return <div className="finops-grid governance-dashboard">
    <section className="finops-kpis span-3 governance-kpis">
      <Kpi label="活跃信号" value={String(signals.length)} change={null} note={`${criticalSignals} 严重 · ${warningSignals} 警告`} icon={ShieldAlert} />
      <Kpi label="严重异常" value={String(criticalSignals)} change={null} note="需要优先处理" icon={CircleAlert} />
      <Kpi label="失败请求" value={String(failedRequests)} change={null} note={`错误率 ${decimal.format(errorRate)}%`} icon={AlertTriangle} />
      <Kpi label="慢请求" value={String(slowRequests)} change={null} note="响应时间 ≥ 2 s" icon={Clock3} />
      <Kpi label="受影响模型" value={String(affectedModels)} change={null} note={`P95 ${formatLatency(p95Latency)}`} icon={Bot} />
    </section>
    <AnomalyRuleManagement rules={rules.data ?? []} entities={entities} models={models} />
    <section className="finops-panel governance-chart-panel"><PanelTitle title="请求状态" meta={`${failedRequests} 次失败`} /><RequestHealthChart totals={totals} /></section>
    <section className="finops-panel governance-chart-panel"><PanelTitle title="延迟分布" meta={`${slowRequests} 次 ≥ 2 s`} /><RequestLatencyChart totals={totals} /></section>
    <MetricRankingPanel className="governance-chart-panel" title="模型成功率" meta={`${modelStats.length} 个模型`} rows={modelStats.map((item) => ({ name: item.name, value: item.successRate }))} valueLabel="成功率" valueFormatter={(value) => `${decimal.format(value)}%`} color="var(--success)" domain={[0, 100]} />
    <section className="finops-panel span-2"><PanelTitle title="风险变化" meta="错误率 · P95 时延" />{riskTrend.length ? <GovernanceRiskTrendChart rows={riskTrend} /> : <EmptyState title="暂无风险趋势" detail="当前范围暂无请求数据。" />}</section>
    <MetricRankingPanel className="governance-chart-panel" title="失败来源" meta={`${affectedModels} 个模型`} rows={failureSources} valueLabel="失败请求" valueFormatter={(value) => `${value.toLocaleString(getIntlLocale())} 次`} color="var(--destructive)" emptyTitle="暂无失败请求" emptyDetail="当前范围未发现失败调用。" />
    <section className="finops-panel span-3 governance-signals"><PanelTitle title="触发信号明细" meta={`${signals.length} 项 · ${criticalSignals} 严重 · ${warningSignals} 警告`} /><GovernanceSignalTable signals={signals} onSelect={setSelectedSignal} /></section>
    <GovernanceSignalDialog signal={selectedSignal} rule={(rules.data ?? []).find((rule) => rule.id === selectedSignal?.rule_id)} requests={requestRows} onClose={() => setSelectedSignal(null)} onOpenRequest={onOpenRequest} />
  </div>
}

const SIGNALS_PER_PAGE = 10

/** The triggered-signal table, paged.
 *
 *  Paging is client-side because the whole list already is: `/anomalies` is capped at 100
 *  rows and the page holds all of them, so asking the server again would buy nothing and
 *  cost a round trip. This is the opposite of the request list, where the cap is the
 *  problem rather than the page size.
 */
function GovernanceSignalTable({ signals, onSelect }: {
  signals: UsageAnomaly[]
  onSelect: (signal: UsageAnomaly) => void
}) {
  const [page, setPage] = useState(0)
  const pageCount = Math.max(1, Math.ceil(signals.length / SIGNALS_PER_PAGE))
  // Changing a filter can shrink the list under the current page, which would otherwise
  // leave the reader on a page that no longer exists, looking at nothing.
  const current = Math.min(page, pageCount - 1)
  useEffect(() => { if (page !== current) setPage(current) }, [page, current])

  if (!signals.length) {
    return <EmptyState title="暂无异常" detail="当前筛选范围未触发治理规则。" />
  }
  const start = current * SIGNALS_PER_PAGE
  const rows = signals.slice(start, start + SIGNALS_PER_PAGE)
  return <>
    <ResizableGridTable className="governance-table" role="table" aria-label="治理信号" headerSelector=".governance-head" minWidths={[220, 120, 90, 90, 140, 24]} columnGap={14} horizontalPadding={36}>
      <div className="governance-head" role="row"><span><span>信号</span></span><span><span>维度</span></span><span><span>实际值</span></span><span><span>阈值</span></span><span><span>时间</span></span><span /></div>
      {rows.map((item) => <button type="button" className="governance-signal-row" key={item.id} title={`查看信号详情：${item.title}`} aria-label={`查看信号详情：${item.title}，${item.dimension_name}`} onClick={() => onSelect(item)}><span><i className={item.severity} /><b>{item.title}</b><small>{item.description}</small></span><span>{item.dimension_name}</span><span>{item.actual_value.toFixed(2)}</span><span>{item.threshold_value.toFixed(2)}</span><span>{new Date(item.detected_at).toLocaleString(getIntlLocale())}</span><ChevronRight aria-hidden="true" size={16} /></button>)}
    </ResizableGridTable>
    {pageCount > 1 && <div className="people-table-footer">
      {/* Controls on the left, count on the right: the assistant FAB is anchored to the
          bottom-right corner and was overlapping the next-page button. */}
      <div>
        <Button type="button" variant="outline" size="icon-sm" aria-label="上一页" disabled={current === 0} onClick={() => setPage(current - 1)}><ChevronLeft size={14} /></Button>
        <span>{current + 1} / {pageCount}</span>
        <Button type="button" variant="outline" size="icon-sm" aria-label="下一页" disabled={current >= pageCount - 1} onClick={() => setPage(current + 1)}><ChevronRight size={14} /></Button>
      </div>
      {/* One template literal, not JSX interpolation: the locale provider walks text
          nodes, so a sentence split across nodes can only ever be half-translated. */}
      <div><span>{`显示 ${(start + 1).toLocaleString(getIntlLocale())}–${(start + rows.length).toLocaleString(getIntlLocale())}，共 ${signals.length.toLocaleString(getIntlLocale())} 项`}</span></div>
    </div>}
  </>
}

function signalScopeRequests(rule: AnomalyRule | undefined, requests: UsageRequestSummary[]) {
  if (!rule || rule.scope_type === "global" || !rule.scope_id) return requests
  if (rule.scope_type === "organization") return requests.filter((request) => request.organization_id === rule.scope_id)
  if (rule.scope_type === "department") return requests.filter((request) => request.department_id === rule.scope_id)
  if (rule.scope_type === "project") return requests.filter((request) => request.project_id === rule.scope_id)
  if (rule.scope_type === "agent") return requests.filter((request) => request.agent_id === rule.scope_id)
  if (rule.scope_type === "model") return requests.filter((request) => request.model_id === rule.scope_id)
  return requests.filter((request) => request.user_id === rule.scope_id)
}

const signalMetricLabels: Record<AnomalyRule["metric"], string> = {
  error_rate_percent: "错误率",
  request_latency_ms: "请求延迟",
  request_cost_usd: "单次请求成本",
  agent_request_count: "智能体调用量",
}

function GovernanceSignalDialog({ signal, rule, requests, onClose, onOpenRequest }: {
  signal: UsageAnomaly | null
  rule: AnomalyRule | undefined
  requests: UsageRequestSummary[]
  onClose: () => void
  onOpenRequest: (requestId: string) => void
}) {
  const scopedRequests = signalScopeRequests(rule, requests)
  const linkedRequest = signal?.request_id ? requests.find((request) => request.request_id === signal.request_id) : undefined
  const failedRequests = scopedRequests.filter((request) => request.status_code >= 400).length
  return <Dialog open={signal != null} onOpenChange={(open) => { if (!open) onClose() }}>
    {signal && <DialogContent className="registry-editor-dialog governance-signal-dialog" finalFocus={false}>
      <div className="registry-editor">
        <DialogHeader className="registry-editor-header">
          <DialogTitle>信号详情</DialogTitle>
          <DialogDescription>{signal.title}</DialogDescription>
        </DialogHeader>
        <div className="registry-editor-body governance-signal-dialog-body">
          <div className="governance-signal-summary">
            <span data-severity={signal.severity}>{signal.severity === "critical" ? "严重" : signal.severity === "warning" ? "警告" : "信息"}</span>
            <div><small>实际值</small><strong>{signal.actual_value.toFixed(2)}</strong></div>
            <div><small>阈值</small><strong>{signal.threshold_value.toFixed(2)}</strong></div>
          </div>
          <dl className="governance-signal-facts">
            <div><dt>触发规则</dt><dd>{rule?.name ?? signal.title}</dd></div>
            <div><dt>信号类型</dt><dd>{rule ? signalMetricLabels[rule.metric] : signal.dimension}</dd></div>
            <div><dt>维度</dt><dd>{signal.dimension_name}</dd></div>
            <div><dt>触发时间</dt><dd>{new Date(signal.detected_at).toLocaleString(getIntlLocale())}</dd></div>
            <div><dt>样本数</dt><dd>{scopedRequests.length.toLocaleString(getIntlLocale())}</dd></div>
            <div><dt>最小样本</dt><dd>{rule?.minimum_sample_size.toLocaleString(getIntlLocale()) ?? "--"}</dd></div>
          </dl>
          <section className="governance-signal-evidence">
            <h3>行为证据</h3>
            <p>{linkedRequest ? "该信号由单个请求触发，可查看完整请求链路、Token 和错误信息。" : "该信号由当前筛选范围内的聚合样本触发。"}</p>
            {linkedRequest ? <dl>
              <div><dt>请求状态</dt><dd>HTTP {linkedRequest.status_code}</dd></div>
              <div><dt>模型</dt><dd>{linkedRequest.model_name}</dd></div>
              <div><dt>人员</dt><dd>{linkedRequest.user_name}</dd></div>
              <div><dt>Token</dt><dd>{linkedRequest.total_tokens.toLocaleString(getIntlLocale())}</dd></div>
              <div><dt>请求延迟</dt><dd>{formatLatency(linkedRequest.latency_ms)}</dd></div>
              <div><dt>Request ID</dt><dd><code>{linkedRequest.request_id}</code></dd></div>
            </dl> : <dl>
              <div><dt>请求数</dt><dd>{scopedRequests.length.toLocaleString(getIntlLocale())}</dd></div>
              <div><dt>失败请求</dt><dd>{failedRequests.toLocaleString(getIntlLocale())}</dd></div>
              <div><dt>作用范围</dt><dd>{rule?.scope_type === "global" ? "全部范围" : signal.dimension_name}</dd></div>
              <div><dt>阈值模式</dt><dd>{rule?.threshold_mode === "percentile" ? "动态百分位" : "固定阈值"}</dd></div>
            </dl>}
          </section>
        </div>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <DialogFooter className="registry-editor-footer">
          <DialogClose render={<Button type="button" variant="outline" />}>关闭</DialogClose>
          {signal.request_id && <Button type="button" onClick={() => onOpenRequest(signal.request_id!)}><Activity size={14} />查看请求追踪</Button>}
        </DialogFooter>
      </div>
    </DialogContent>}
  </Dialog>
}

function RequestTrace({
  filters,
  costAvailable,
  models,
}: {
  filters: UsageFilters;
  costAvailable: boolean;
  models: ManagedModel[];
}) {
  const initialRequestId = new URLSearchParams(window.location.search).get("request")
  const [selected, setSelected] = useState<string | null>(initialRequestId);
  const [searchText, setSearchText] = useState(initialRequestId ?? "");
  const [statusFilter, setStatusFilter] = useState<"all" | "success" | "error">("all");
  const query = useQuery(finopsQueries.requests(filters))
  // The header describes the whole window, so it reads the server aggregate. The list below
  // is a page of recent requests and legitimately uses the rows.
  const overview = useQuery(finopsQueries.executiveOverview(filters))
  const volumeTrend = useQuery(finopsQueries.trends(filters, "none", "day"))
  const detail = useQuery(finopsQueries.requestDetail(selected ?? ""))
  const firstRequestId = query.data?.[0]?.correlation_id ?? null
  const selectedCorrelationId = detail.data?.correlation_id ?? selected
  useEffect(() => {
    if (!selected && firstRequestId) setSelected(firstRequestId)
  }, [firstRequestId, selected])
  if (query.isLoading || overview.isLoading || volumeTrend.isLoading) return <LoadingState />;
  if (query.error || overview.error || volumeTrend.error) return <ErrorState error={query.error ?? overview.error ?? volumeTrend.error} />;
  if (!query.data?.length)
    return (
      <EmptyState
        title="暂无请求记录"
        detail="调用完成后可在此按 requestId 查询。"
      />
    );
  const requestRows = priceRequests(query.data, models);
  const attemptsByRequest = new Map<string, UsageRequestSummary[]>();
  for (const item of requestRows) {
    const attempts = attemptsByRequest.get(item.request_id) ?? [];
    attempts.push(item);
    attemptsByRequest.set(item.request_id, attempts);
  }
  const attemptPositions = new Map<string, { index: number; total: number }>();
  for (const attempts of attemptsByRequest.values()) {
    attempts.sort((left, right) => Date.parse(left.timestamp) - Date.parse(right.timestamp)
      || left.correlation_id.localeCompare(right.correlation_id));
    attempts.forEach((item, index) => {
      attemptPositions.set(item.correlation_id, { index: index + 1, total: attempts.length });
    });
  }
  const normalizedSearch = searchText.trim().toLocaleLowerCase();
  const visibleRows = requestRows.filter((item) => {
    const matchesStatus = statusFilter === "all"
      || (statusFilter === "success" ? item.status_code < 400 : item.status_code >= 400);
    const matchesSearch = !normalizedSearch || [
      item.request_id,
      item.correlation_id,
      item.agent_name,
      item.model_name,
      item.user_name,
      item.project_name,
    ].some((value) => value?.toLocaleLowerCase().includes(normalizedSearch));
    return matchesStatus && matchesSearch;
  });
  const totals = overview.data?.totals;
  const windowRequests = totals?.total_requests ?? 0;
  const clientErrors = totals?.client_error_requests ?? 0;
  const serverErrors = totals?.server_error_requests ?? 0;
  const failedRequests = clientErrors + serverErrors;
  const slowRequests = totals?.latency_over_5s ?? 0;
  const slowRequestPercent = windowRequests ? (slowRequests / windowRequests) * 100 : 0;
  const volumeRows = (volumeTrend.data?.points ?? []).map((point) => ({
    day: formatTrendBucket(point.bucket_start, "day"),
    calls: point.totals.calls,
  }));
  const pricedDetail = detail.data
    ? priceRequests([detail.data], models)[0]
    : null;
  return (
    <div className="finops-grid">
      <section className="finops-kpis span-3 trace-kpis" aria-label="请求摘要">
        <Kpi label="P50 时延" value={formatLatency(totals?.p50_latency_ms ?? 0)} change={null} note="典型响应时间" icon={Clock3} />
        <Kpi label="P95 时延" value={formatLatency(totals?.p95_latency_ms ?? 0)} change={null} note="长尾响应时间" icon={Timer} />
        <Kpi label="失败请求" value={failedRequests.toLocaleString(getIntlLocale())} change={null} note={`4xx ${clientErrors.toLocaleString(getIntlLocale())} · 5xx ${serverErrors.toLocaleString(getIntlLocale())}`} icon={ShieldAlert} />
        <Kpi label="≥5 秒请求" value={slowRequests.toLocaleString(getIntlLocale())} change={null} note={`${decimal.format(slowRequestPercent)}%`} icon={AlertTriangle} />
      </section>
      <section className="finops-panel trace-insight-panel span-2">
        <PanelTitle title="调用趋势" meta="每日" />
        <RequestVolumeTrendChart rows={volumeRows} />
      </section>
      <section className="finops-panel trace-insight-panel">
        <PanelTitle title="时延占比分布" meta={`P99 ${formatLatency(totals?.p99_latency_ms ?? 0)}`} />
        <RequestLatencyChart totals={totals} percentage />
      </section>
      <div className="trace-layout span-3">
        <section className="finops-panel trace-list">
          <PanelTitle title="请求记录" meta={`${visibleRows.length} / ${requestRows.length} 条`} />
          <div className="trace-list-tools">
            <label>
              <Search size={14} />
              <input
                type="search"
                value={searchText}
                onChange={(event) => setSearchText(event.target.value)}
                placeholder="搜索 ID、智能体、模型或人员"
                aria-label="搜索请求"
              />
            </label>
            <div className="trace-status-filter" aria-label="请求状态筛选">
              {(["all", "success", "error"] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  className={statusFilter === value ? "active" : ""}
                  onClick={() => setStatusFilter(value)}
                >
                  {value === "all" ? "全部" : value === "success" ? "成功" : "错误"}
                </button>
              ))}
            </div>
          </div>
          {visibleRows.map((item) => (
            <TraceRow
              key={item.correlation_id}
              item={item}
              active={selectedCorrelationId === item.correlation_id}
              onClick={() => setSelected(item.correlation_id)}
              costAvailable={costAvailable}
              attempt={attemptPositions.get(item.correlation_id)}
            />
          ))}
          {!visibleRows.length && <div className="trace-no-results">没有符合条件的请求</div>}
        </section>
        <section className="finops-panel trace-detail">
          <PanelTitle title="请求详情" />
          {!selected ? (
            <EmptyState
              title="选择一条请求"
              detail="查看业务归因、Token、状态和 APIM correlationId。"
            />
          ) : detail.isLoading ? (
            <LoadingState />
          ) : detail.error ? (
            <ErrorState error={detail.error} />
          ) : (
            pricedDetail && (
              <RequestTraceDetail request={pricedDetail} costAvailable={costAvailable} />
            )
          )}
        </section>
      </div>
    </div>
  );
}

// Only the degraded outcomes are listed. `ok` means the ledger answered and `null` means the
// request never reached admission at all (subscription callers, pre-rollout rows); neither is
// worth an alert, and rendering one for them would train people to ignore the real ones.
// Only the degraded outcomes are listed, on the same rule as the budget markers: 'ok',
// 'policy_unconfigured' and null are the normal states and rendering a bar for them would
// train people to ignore the ones that matter.
const modelAdmissionWarnings: Record<string, { title: string; detail: string; tone: string }> = {
  denied: {
    title: "模型未授权",
    detail: "该人员的模型访问策略不包含此模型，网关在调用模型前就拒绝了请求。未产生任何 Token 与费用。",
    tone: "blocked",
  },
  ledger_unavailable: {
    title: "未经模型校验",
    detail: "网关读取策略账本失败，本次调用按放行处理。用量照常入账，但当时的模型授权判断未生效。",
    tone: "warning",
  },
  model_unattributed: {
    title: "模型无法识别",
    detail: "本次调用未携带可识别的模型标识，模型访问策略无法比对，已按放行处理。",
    tone: "warning",
  },
};

const budgetAdmissionWarnings: Record<string, { title: string; detail: string; tone: string }> = {
  denied: {
    title: "预算拦截",
    detail: "该人员本月额度已用尽，网关在调用模型前就拒绝了请求。未产生任何 Token 与费用。",
    tone: "blocked",
  },
  ledger_unavailable: {
    title: "未经预算校验",
    detail: "网关读取预算账本失败，本次调用按放行处理。用量照常入账，但当时的额度判断未生效。",
    tone: "warning",
  },
  ledger_write_failed: {
    title: "预留未写入",
    detail: "预算校验通过，但本次占用未写回账本。下一次同步前，该人员的额度会被少算这一笔。",
    tone: "warning",
  },
};

function RequestTraceDetail({
  request,
  costAvailable,
}: {
  request: UsageRequestDetail;
  costAvailable: boolean;
}) {
  const errorMessage = request.ingest_source === "policy"
    ? request.error_message === "API policy denied: Model is not assigned to this user"
      ? "该人员无权使用此模型"
      : request.error_message === "API policy denied: Role cannot use this runtime or model"
        ? "当前角色无权使用此运行时或模型"
        : request.error_message
    : request.error_message;
  const tokenTotal = Math.max(1, request.prompt_tokens + request.cached_tokens + request.completion_tokens);
  // Cache writes bill far above reads, so two requests with identical token counts can differ in
  // cost by an order of magnitude. The split is only shown once a write was actually reported.
  const cacheWrite = request.cache_write_tokens ?? 0;
  const cacheRead = Math.max(request.cached_tokens - cacheWrite, 0);
  // Both providers are normalized to "input excludes everything under cache_control", so this
  // number is routinely a handful of tokens while the real prompt sits in the cache buckets.
  // Labelling it plainly "输入" reads as a bug to anyone inspecting a cached request.
  const tokenSegments = cacheWrite > 0
    ? [
      { label: "未缓存输入", value: request.prompt_tokens, className: "prompt" },
      { label: "缓存读取", value: cacheRead, className: "cached" },
      { label: "缓存写入", value: cacheWrite, className: "cache-write" },
      { label: "输出", value: request.completion_tokens, className: "completion" },
    ]
    : [
      { label: "未缓存输入", value: request.prompt_tokens, className: "prompt" },
      { label: "缓存读取", value: cacheRead, className: "cached" },
      { label: "输出", value: request.completion_tokens, className: "completion" },
    ];
  // Gateway-log reconciliation measures input/output but cannot measure streamed cache usage.
  // Keep those known buckets visible without presenting the legacy numeric cache zero as fact.
  const inputOutputMeasured = !request.estimated
    || Boolean(request.reconciled_at)
    || request.status_code >= 400;
  const cacheUsageUnmeasured = request.ingest_error === "stream_cache_usage_unavailable";
  const state = usageState(!inputOutputMeasured, request.timestamp);
  const usagePlaceholder = state === "pending" ? "待对账" : "不可用";
  const tokenValue = (item: (typeof tokenSegments)[number]) => {
    const cacheBucket = item.className === "cached" || item.className === "cache-write";
    if (!inputOutputMeasured) return usagePlaceholder;
    if (cacheBucket && cacheUsageUnmeasured) return "未实测";
    return item.value.toLocaleString(getIntlLocale());
  };
  const traceSteps = request.ingest_source === "policy"
    ? [request.request_source, "Cloud API 策略"]
    : [request.request_source, request.provider, request.runtime, request.ingest_source].filter(Boolean);
  // The gateway admission check fails open so a ledger outage cannot stop employees working.
  // That trade-off is only acceptable while it stays visible: a degraded request must not look
  // identical to one that was actually checked against a budget.
  const admissionWarning = budgetAdmissionWarnings[request.budget_admission ?? ""];
  const modelWarning = modelAdmissionWarnings[request.model_admission ?? ""];
  return <div className="trace-detail-content">
    <div className="trace-detail-hero">
      <span className={request.status_code >= 400 ? "failed" : "success"}>{request.status_code}</span>
      <div><b>{request.model_name}</b><small>{new Date(request.timestamp).toLocaleString(getIntlLocale())}</small></div>
      <strong>{formatLatency(request.latency_ms)}</strong>
    </div>
    {errorMessage && <div className="trace-error"><CircleAlert size={15} /><div><b>{request.ingest_source === "policy" ? "策略拒绝" : "请求失败"}</b><span>{errorMessage}</span></div></div>}
    {admissionWarning && <div className="trace-error" data-tone={admissionWarning.tone}><CircleAlert size={15} /><div><b>{admissionWarning.title}</b><span>{admissionWarning.detail}</span></div></div>}
    {modelWarning && <div className="trace-error" data-tone={modelWarning.tone}><CircleAlert size={15} /><div><b>{modelWarning.title}</b><span>{modelWarning.detail}</span></div></div>}
    <section className="trace-detail-section">
      <h3>处理链路</h3>
      <div className="trace-path">
        {traceSteps.map((step, index) => <span key={`${step}-${index}`}>{step}</span>)}
      </div>
    </section>
    <section className="trace-detail-section">
      <div className="trace-section-heading"><h3>Token 构成</h3><strong>{inputOutputMeasured ? `${cacheUsageUnmeasured ? "≥ " : ""}${request.total_tokens.toLocaleString(getIntlLocale())} tokens` : state === "pending" ? "等待对账回填" : "用量未实测"}</strong></div>
      <div className="trace-token-bar" aria-label="Token 构成">
        {tokenSegments.filter((item) => item.value > 0).map((item) => <i key={item.label} className={item.className} style={{ width: `${(item.value / tokenTotal) * 100}%` }} />)}
      </div>
      <dl className={`trace-token-facts columns-${tokenSegments.length}`}>
        {tokenSegments.map((item) => <div className={`trace-token-fact ${item.className}`} key={item.label}><dt>{item.label}</dt><dd>{tokenValue(item)}</dd></div>)}
        <div className="trace-token-cost"><dt>估算费用</dt><dd>{!inputOutputMeasured ? usagePlaceholder : costAvailable ? `${cacheUsageUnmeasured ? "≥ " : ""}${currency.format(request.estimated_cost)}` : "未计价"}</dd></div>
      </dl>
      {cacheUsageUnmeasured && <p className="trace-token-note"><CircleAlert size={13} />流式缓存用量未实测；当前 Token 总量与费用均为下限。</p>}
    </section>
    <TraceFacts title="业务归因" facts={[
      ["组织", request.organization_name],
      ["部门", request.department_name],
      ["项目", request.project_name],
      ["智能体", request.agent_name],
      ["人员", request.user_name],
      ["工作流", request.workflow],
    ]} />
    <TraceFacts title="追踪标识" mono facts={[
      ["Request ID", request.request_id],
      ["Correlation ID", request.correlation_id],
      ["Run ID", request.run_id],
      ["Turn", String(request.turn_index)],
    ]} />
  </div>
}

function TraceFacts({ title, facts, mono = false }: { title: string; facts: Array<[string, string]>; mono?: boolean }) {
  return <section className="trace-detail-section">
    <h3>{title}</h3>
    <dl className={`trace-facts ${mono ? "mono" : ""}`}>
      {facts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value || "—"}</dd></div>)}
    </dl>
  </section>
}

function TraceRow({
  item,
  active,
  onClick,
  costAvailable,
  attempt,
}: {
  item: UsageRequestSummary;
  active: boolean;
  onClick: () => void;
  costAvailable: boolean;
  attempt: { index: number; total: number } | undefined;
}) {
  // The list contract carries no `estimated` flag, but a successful call never truly consumes zero
  // tokens: those zeros identify a row that is still waiting for reconciliation.
  const state = usageState(item.status_code < 400 && item.total_tokens === 0, item.timestamp);
  const placeholder = state === "pending" ? "待对账" : "不可用";
  return (
    <button className={`trace-row ${active ? "active" : ""}`} onClick={onClick}>
      <span className={`trace-row-status ${item.status_code >= 400 ? "failed" : "success"}`}>
        {item.status_code}
      </span>
      <div>
        <b>{item.model_name}</b>
        <code title={`Correlation ID: ${item.correlation_id}`}>{attempt && attempt.total > 1 ? `#${attempt.index}/${attempt.total} · ${item.request_id}` : item.request_id}</code>
      </div>
      <small>{state === "measured" ? `${compact.format(item.total_tokens)} tokens` : placeholder}</small>
      <strong>
        {state !== "measured" ? placeholder : costAvailable ? currency.format(item.estimated_cost) : "未计价"}
      </strong>
    </button>
  );
}
