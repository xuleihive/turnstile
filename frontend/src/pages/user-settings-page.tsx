import { useEffect, useMemo, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Building2, CalendarDays, PanelLeft, RefreshCw, Search, Shield, UserRound, Users, WalletCards } from "lucide-react"
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"

import { userSettingsApi, type PersonalBudget, type PersonalUsage, type PersonalUsageTotals } from "../api/user-settings"
import { ProfileEditor } from "../components/user-settings/profile-editor"
import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { ResizableTable } from "../components/ui/resizable-table"
import { MonthPicker } from "../components/finops/month-picker"
import { ChartModeToggle } from "../components/finops/chart-mode-toggle"
import { FinOpsChartTooltip } from "../components/finops/chart-tooltip"
import { ChartSeriesLegend } from "../components/finops/chart-legend"
import { getIntlLocale } from "../locales/index"
import { useAuth } from "../providers/auth-provider"
import { useTimezone } from "../providers/timezone-provider"

const labels = { healthy: "正常", warning: "预警", exceeded: "已超额", unallocated: "未分配" }
const membershipSources = { catalog: "目录配置", observed_usage: "用量归属", owner_default: "平台默认归属", unlinked: "未关联" }
const tokens = (value: number | null) => value == null ? "--" : new Intl.NumberFormat(getIntlLocale()).format(value)
const compact = (value: number | null) => value == null ? "--" : new Intl.NumberFormat(getIntlLocale(), { notation: "compact", maximumFractionDigits: 1 }).format(value)
const cost = (value: number | null) => value == null ? "--" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 4 }).format(value)
const series = [
  { key: "input_tokens", label: "输入", color: "var(--chart-4)" },
  { key: "output_tokens", label: "输出", color: "var(--chart-2)" },
  { key: "cache_read_tokens", label: "缓存读取", color: "var(--chart-1)" },
  { key: "cache_write_tokens", label: "缓存写入", color: "var(--chart-cache-write)" },
]

function DataState({ loading, error, onRetry, children, updatedAt }: {
  loading: boolean; error: Error | null; onRetry: () => void; children: React.ReactNode; updatedAt?: string
}) {
  const { timezone } = useTimezone()
  if (loading) return <div className="user-settings-state" role="status"><RefreshCw size={16} className="spin" />正在加载</div>
  return <>
    {error && <div className="user-settings-state user-settings-error" role="alert"><AlertTriangle size={16} /><span>{error.message}</span>{updatedAt && <span>显示上次数据</span>}<Button variant="outline" size="sm" onClick={onRetry}>重试</Button></div>}
    {(!error || updatedAt) && children}
    {updatedAt && <div className="user-settings-data-time"><span>数据时间</span><time data-no-localize dateTime={updatedAt}>{new Intl.DateTimeFormat(getIntlLocale(), { dateStyle: "medium", timeStyle: "short", timeZone: timezone }).format(new Date(updatedAt))}</time></div>}
  </>
}

function BudgetSummary({ budget }: { budget: PersonalBudget }) {
  const kpis = [
    ["月度预算", budget.token_limit == null ? "未分配" : compact(budget.token_limit)],
    ["已使用", compact(budget.used_tokens)],
    ["剩余额度", compact(budget.remaining_tokens)],
    ["月底预测", compact(budget.forecast_tokens)],
    ["使用率", budget.usage_percent == null ? "--" : `${budget.usage_percent}%`],
  ]
  return <>
    <div className="user-settings-kpis">{kpis.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong><small>{label === "使用率" ? "" : "Token"}</small></div>)}</div>
    <div className="user-settings-budget-status">
      <div><WalletCards size={16} /><strong>{tokens(budget.used_tokens)}</strong><span>已使用</span><span className="user-settings-status" data-status={budget.status}>{labels[budget.status]}</span></div>
      {budget.token_limit != null && <div className="budget-progress-track" data-status={budget.status} role="progressbar" aria-label="预算使用率" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.max(0, Math.min(100, budget.usage_percent ?? 0))}><i style={{ width: `${Math.max(0, Math.min(100, budget.usage_percent ?? 0))}%` }} /></div>}
      <div className="user-settings-budget-foot"><span>{tokens(budget.token_limit)}</span><span>Token</span><span title={budget.enforcement_department_id ?? undefined}>{budget.enforcement_mode === "block" ? "超额拦截" : budget.enforcement_mode === "audit" ? "仅告警" : "未关联"}</span></div>
    </div>
  </>
}

function UsageChart({ usage }: { usage: PersonalUsage }) {
  const [shape, setShape] = useState<"bar" | "line">("bar")
  const [metric, setMetric] = useState<"tokens" | "cost">("tokens")
  const [hidden, setHidden] = useState<string[]>([])
  const activeSeries = series.filter((item) => !hidden.includes(item.key))
  const rows = useMemo(() => usage.points.map((point) => ({
    date: point.bucket_start, ...point.totals,
  })), [usage.points])
  const bucketLabel = (value: unknown) => new Intl.DateTimeFormat(getIntlLocale(), {
    timeZone: usage.timezone, month: "numeric", day: "numeric",
    ...(usage.interval === "hour" ? { hour: "2-digit" as const } : {}),
  }).format(new Date(String(value)))
  const axes = <>
    <CartesianGrid vertical={false} stroke="var(--border)" />
    <XAxis dataKey="date" tickFormatter={bucketLabel} tickLine={false} axisLine={false} interval="preserveStartEnd" />
    <YAxis width={56} tickLine={false} axisLine={false} tickFormatter={(value) => metric === "cost" ? `$${value}` : compact(Number(value))} />
    <Tooltip content={<FinOpsChartTooltip labelFormatter={bucketLabel} nameFormatter={(key) => series.find((item) => item.key === key)?.label ?? "费用估算"} valueFormatter={(value) => metric === "cost" ? cost(Number(value)) : tokens(Number(value))} />} />
  </>
  return <div className="user-settings-usage-chart">
    <div className="user-settings-chart-toolbar"><ChartModeToggle value={shape} modes={["bar", "line"]} onChange={(mode) => { if (mode !== "heatmap") setShape(mode) }} />
      <div className="usage-metric-segment" role="group" aria-label="图表指标">
        <button type="button" aria-pressed={metric === "tokens"} className={metric === "tokens" ? "active" : ""} onClick={() => setMetric("tokens")}>Token</button>
        <button type="button" aria-pressed={metric === "cost"} className={metric === "cost" ? "active" : ""} onClick={() => setMetric("cost")}>费用</button>
      </div>
      {metric === "tokens" && <ChartSeriesLegend className="usage-chart-legend" items={series} hiddenSeries={hidden} onToggle={(key) => setHidden((current) => current.includes(key) ? current.filter((item) => item !== key) : [...current, key])} />}
    </div>
    <div className="user-settings-chart-surface">
      {usage.points.length === 0 ? <div className="user-settings-state">本月暂无使用记录</div>
        : metric === "cost" && usage.totals.cost_state === "unpriced" ? <div className="user-settings-state">成本尚未计价</div>
        : <ResponsiveContainer width="100%" height="100%">
          {shape === "bar" ? <BarChart data={rows} margin={{ top: 12, right: 16, bottom: 12 }}>{axes}
            {metric === "cost" ? <Bar dataKey="estimated_cost_usd" fill="var(--chart-1)" isAnimationActive={false} />
              : activeSeries.map((item) => <Bar key={item.key} dataKey={item.key} stackId="tokens" fill={item.color} isAnimationActive={false} />)}
          </BarChart> : <LineChart data={rows} margin={{ top: 12, right: 16, bottom: 12 }}>{axes}
            {(metric === "cost" ? [{ key: "estimated_cost_usd", color: "var(--chart-1)" }] : activeSeries).map((item) => <Line key={item.key} type="linear" dataKey={item.key} stroke={item.color} strokeWidth={2} dot={rows.length === 1} isAnimationActive={false} />)}
          </LineChart>}
        </ResponsiveContainer>}
    </div>
  </div>
}

function UsageSummary({ usage }: { usage: PersonalUsage }) {
  const totals = usage.totals
  const kpis = [
    ["请求数", tokens(totals.requests)], ["总 Token", compact(totals.total_tokens)],
    ["错误率", totals.error_rate == null ? "--" : `${totals.error_rate.toFixed(1)}%`],
    ["费用估算", cost(totals.estimated_cost_usd)],
  ]
  const value = (row: PersonalUsageTotals, key: keyof PersonalUsageTotals) => tokens(row[key] as number)
  return <>
    {usage.demo && <span className="user-settings-status">演示数据</span>}
    <div className="user-settings-kpis usage-kpis">{kpis.map(([label, number]) => <div key={label}><span>{label}</span><strong>{number}</strong></div>)}</div>
    {totals.cost_state === "partial" && <p className="user-settings-meta">部分用量尚未计价</p>}
    <UsageChart usage={usage} />
    <div className="user-settings-table-scroll"><ResizableTable className="user-settings-table">
      <caption className="sr-only">模型使用汇总</caption>
      <thead><tr><th><span>模型</span></th><th><span>请求数</span></th>{series.map((item) => <th key={item.key}><span>{item.label}</span></th>)}<th><span>总 Token</span></th><th><span>费用估算</span></th></tr></thead>
      <tbody>{usage.models.map((row) => <tr key={row.model_id}>
        <td data-no-localize title={row.model_name}>{row.model_name}</td><td>{tokens(row.totals.requests)}</td>
        {series.map((item) => <td key={item.key}>{value(row.totals, item.key as keyof PersonalUsageTotals)}</td>)}
        <td>{tokens(row.totals.total_tokens)}</td><td>{cost(row.totals.estimated_cost_usd)}</td>
      </tr>)}</tbody>
    </ResizableTable></div>
  </>
}

export function UserSettingsPage({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const { user, refreshProfile } = useAuth()
  const { timezone } = useTimezone()
  const client = useQueryClient()
  const [period, setPeriod] = useState(() => new Date().toISOString().slice(0, 7))
  const [interval, setInterval] = useState<"day" | "hour">("day")
  const [search, setSearch] = useState("")
  const [profileError, setProfileError] = useState<string | null>(null)
  const prefix = ["user-settings", user?.id] as const
  const account = useQuery({ queryKey: [...prefix, "account"], queryFn: userSettingsApi.account, staleTime: 30_000 })
  const budget = useQuery({ queryKey: [...prefix, "budget", period], queryFn: () => userSettingsApi.budget(period), staleTime: 30_000 })
  const models = useQuery({ queryKey: [...prefix, "models"], queryFn: userSettingsApi.models, staleTime: 30_000 })
  const usage = useQuery({ queryKey: [...prefix, "usage", period, interval, timezone], queryFn: () => userSettingsApi.usage(period, interval, timezone), staleTime: 30_000 })
  useEffect(() => {
    let cancelled = false
    void refreshProfile().catch((error: unknown) => {
      if (!cancelled) setProfileError(error instanceof Error ? error.message : "无法读取账号信息。")
    })
    return () => { cancelled = true }
  }, [refreshProfile])
  const busy = account.isFetching || budget.isFetching || models.isFetching || usage.isFetching
  const filtered = models.data?.items.filter((model) =>
    `${model.name} ${model.model_key}`.toLowerCase().includes(search.trim().toLowerCase())) ?? []
  const max = new Date().toISOString().slice(0, 7)
  const oldest = new Date(`${max}-01T00:00:00Z`)
  oldest.setUTCMonth(oldest.getUTCMonth() - 11)
  return <div className="finops-workspace user-settings-workspace">
    <header className="finops-header"><div>
      <Button variant="ghost" size="icon-sm" className="finops-sidebar-trigger" title="切换导航栏" aria-label="切换导航栏" onClick={onToggleSidebar}><PanelLeft size={16} /></Button>
      <span className="finops-header-icon"><UserRound size={17} /></span><h1>用户设置</h1>
    </div><Button variant="ghost" size="icon-sm" title="刷新账号信息" aria-label="刷新账号信息" disabled={busy} onClick={() => {
      void client.invalidateQueries({ queryKey: prefix })
      void refreshProfile().then(() => setProfileError(null)).catch((error: unknown) => setProfileError(error instanceof Error ? error.message : "无法读取账号信息。"))
    }}><RefreshCw size={15} className={busy ? "spin" : undefined} /></Button></header>
    <div className="finops-scroll-region"><div className="user-settings-content">
      <ProfileEditor />
      {profileError && <p className="user-settings-error" role="alert">{profileError}</p>}
      <section className="user-settings-section"><h2>账号信息</h2>
        <DataState loading={account.isLoading} error={account.error} onRetry={() => void account.refetch()} updatedAt={account.data?.generated_at}>
          {account.data && <dl className="user-settings-facts">
            <div><dt><Shield size={14} />平台权限组</dt><dd>{account.data.permission_groups.map((group) => group.name).join(", ")}</dd></div>
            <div><dt><Building2 size={14} />所属组织</dt><dd>{account.data.organization ? <span data-no-localize>{account.data.organization.name}</span> : "未关联"}</dd></div>
            <div><dt><Users size={14} />所属部门</dt><dd>{account.data.department ? <><span data-no-localize>{account.data.department.name}</span><small>{membershipSources[account.data.membership_source]}</small></> : "未关联"}</dd></div>
          </dl>}
        </DataState>
      </section>
      <section className="user-settings-section"><div className="user-settings-section-heading"><h2>预算使用情况</h2>
        <div className="budget-period-control"><CalendarDays size={14} /><MonthPicker value={period} min={budget.data?.min_period ?? oldest.toISOString().slice(0, 7)} max={budget.data?.max_period ?? max} onChange={setPeriod} label="预算周期" ariaLabel="预算周期" /></div>
        <span className="user-settings-status">APIM</span>
      </div><DataState loading={budget.isLoading} error={budget.error} onRetry={() => void budget.refetch()} updatedAt={budget.data?.generated_at}>
        {budget.data && <BudgetSummary budget={budget.data} />}
      </DataState></section>
      <section className="user-settings-section"><div className="user-settings-section-heading"><h2>可用模型</h2>
        <label className="user-settings-search"><Search size={14} /><Input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索模型" aria-label="搜索模型" /></label>
      </div><DataState loading={models.isLoading} error={models.error} onRetry={() => void models.refetch()} updatedAt={models.data?.generated_at}>
        <div className="user-settings-table-scroll"><ResizableTable className="user-settings-table models-table"><caption className="sr-only">可用模型</caption>
          <thead><tr>{["模型", "模型标识", "提供方", "上下文窗口", "授权来源", "状态"].map((label) => <th key={label}><span>{label}</span></th>)}</tr></thead>
          <tbody>{filtered.map((model) => <tr key={model.id}>
            <td data-no-localize title={model.name}>{model.name}</td><td data-no-localize><code>{model.model_key}</code></td><td data-no-localize>{model.provider_name}</td><td>{tokens(model.context_window)}</td>
            <td>{model.access_source === "explicit" ? "显式授权" : "兼容现有权限"}</td>
            <td><span className="user-settings-status" data-status={model.runtime_status === "available" ? "healthy" : model.runtime_status === "unavailable" ? "warning" : undefined}>{model.runtime_status === "available" ? "可用" : model.runtime_status === "unavailable" ? "暂不可用" : "未检查"}</span></td>
          </tr>)}</tbody></ResizableTable></div>
        {!filtered.length && <div className="user-settings-state">{models.data?.policy_state === "deny_all" ? "未获模型授权" : "没有匹配的模型"}</div>}
      </DataState></section>
      <section className="user-settings-section"><div className="user-settings-section-heading"><h2>使用统计</h2>
        <div className="usage-metric-segment" role="group" aria-label="图表粒度">
          <button type="button" className={interval === "day" ? "active" : ""} aria-pressed={interval === "day"} onClick={() => setInterval("day")}>每日</button>
          <button type="button" className={interval === "hour" ? "active" : ""} aria-pressed={interval === "hour"} onClick={() => setInterval("hour")}>小时</button>
        </div>
      </div><DataState loading={usage.isLoading} error={usage.error} onRetry={() => void usage.refetch()} updatedAt={usage.data?.generated_at}>
        {usage.data && <UsageSummary usage={usage.data} />}
      </DataState></section>
    </div></div>
  </div>
}
