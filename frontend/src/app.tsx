import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Activity,
  AlertTriangle,
  AppWindow,
  ArrowDownRight,
  ArrowUpRight,
  Bot,
  Building2,
  ChartPie,
  CalendarDays,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDollarSign,
  Cpu,
  FileSpreadsheet,
  History,
  KeyRound,
  LayoutDashboard,
  List,
  LineChart,
  Network,
  PanelLeft,
  PanelLeftClose,
  Pin,
  Plus,
  Search,
  Settings,
  ShieldAlert,
  Sparkles,
  SquarePen,
  UserRoundX,
  UsersRound,
  WalletCards,
  X,
  Zap, LogOut,} from "lucide-react";
import { TurnstileMark } from "./components/turnstile-logo";
import { ApimLogo, CopilotLogo } from "./components/brand-logos";
import { FINOPS_NAVIGATE_EVENT } from "./lib/navigation";
import { useAuth } from "./providers/auth-provider";
import { dataSource, usageWindow } from "./data-sources/apim/api";
import {
  finopsQueries,
} from "./data-sources/apim/queries";
import { assistantApi } from "./components/assistant/api";
import { pinnedChartsKey, pinnedChartsQuery } from "./components/assistant/queries";
import type { PinnedReport } from "./components/assistant/types";
import { AssistantPanel } from "./components/assistant/assistant-panel";
import { AssistantPage } from "./pages/assistant-page";
import { PinnedReportPage } from "./pages/pinned-report-page";
import { Button } from "./components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "./components/ui/dropdown-menu";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./components/ui/select";
import { SearchCommand } from "./components/search-command";
import {
  apimPages,
  defaultApimFilters,
  normalizeApimPage,
  prefetchApimPage,
  renderApimPage,
} from "./data-sources/apim/source";
import type { FinOpsScope } from "./data-sources/apim/pages/dashboard-page";
import {
  githubCopilotOwnsPage,
  githubCopilotPages,
  normalizeGithubCopilotPage,
  prefetchGithubCopilotPage,
  renderGithubCopilotPage,
} from "./data-sources/github-copilot/source";
import { useIsMobile } from "./hooks/use-mobile";
import { getIntlLocale } from "./locales/index";
import { ModelManagementPage } from "./pages/model-management-page";
import { ApimNativeRoutesPage } from "./pages/apim-native-routes-page";
import { GatewayReleasesPage } from "./pages/gateway-releases-page";
import { ApplicationsPage } from "./pages/applications-page";
import { OrganizationPage } from "./pages/organization-page";
import { SettingsPage } from "./pages/settings-page";
import type {
  AuditFinding,
  AuditStatus,
  MetricTotals,
  OptimizationEvent,
  RunDetail,
  UsageFilters,
} from "./data-sources/apim/types";

type Page =
  | "settings"
  | "models"
  | "apim-native-routes"
  | "gateway-releases"
  | "applications"
  | "organization"
  | "budgets"
  | "assistant"
  | "pinned-report"
  | "finops-overview"
  | "finops-analytics"
  | "finops-trends"
  | "finops-governance"
  | "finops-requests"
  | "finops-invoke"
  | "copilot-cost-centers"
  | "copilot-unassigned-users"
  | "copilot-enterprise-teams"
  | "copilot-requests";
type DataSource = "apim" | "github-copilot";

function normalizePageForSource(source: DataSource, page: Page): Page {
  return (source === "github-copilot"
    ? normalizeGithubCopilotPage(page)
    : normalizeApimPage(page)) as Page;
}

const DATA_SOURCE_STORAGE_KEY = "turnstile_data_source";

function dataSourceFromStorage(): DataSource {
  const queryValue = new URLSearchParams(window.location.search).get("source");
  if (queryValue === "github-copilot") return "github-copilot";
  if (queryValue === "apim") return "apim";
  return localStorage.getItem(DATA_SOURCE_STORAGE_KEY) === "github-copilot"
    ? "github-copilot"
    : "apim";
}
const pageIds: Page[] = [
  "settings",
  "models",
  "apim-native-routes",
  "gateway-releases",
  "applications",
  "organization",
  "budgets",
  "assistant",
  "pinned-report",
  "finops-overview",
  "finops-analytics",
  "finops-trends",
  "finops-governance",
  "finops-requests",
  "finops-invoke",
  "copilot-cost-centers",
  "copilot-unassigned-users",
  "copilot-enterprise-teams",
  "copilot-requests",
];
function pageFromUrl(): Page {
  const value = new URLSearchParams(window.location.search).get("page");
  // TODO: 疑似废弃补丁，待确认：仅在旧 page 参数不再出现在受支持书签或入口后删除。
  const legacy: Record<string, Page> = {
    overview: "finops-overview",
    trends: "finops-trends",
    runs: "finops-requests",
    audit: "finops-governance",
    optimization: "finops-analytics",
    // The department/project page was merged into model usage, which now carries its
    // cross-tabs as a dimension. Existing bookmarks land there instead of on the default.
    "finops-breakdown": "finops-analytics",
  };
  const resolved = legacy[value ?? ""] ?? value;
  return resolved && pageIds.includes(resolved as Page)
    ? (resolved as Page)
    : "finops-overview";
}

const SIDEBAR_WIDTH_DEFAULT = 256;
const SIDEBAR_WIDTH_MIN = 200;
const SIDEBAR_WIDTH_MAX = 360;
const SIDEBAR_COOKIE_NAME = "sidebar_state";
const SIDEBAR_COOKIE_MAX_AGE = 60 * 60 * 24 * 7;
const SIDEBAR_WIDTH_STORAGE_KEY = "sidebar_width";

function clampSidebarWidth(width: number) {
  return Math.max(SIDEBAR_WIDTH_MIN, Math.min(SIDEBAR_WIDTH_MAX, width));
}

function sidebarOpenFromCookie() {
  const value = document.cookie
    .split("; ")
    .find((entry) => entry.startsWith(`${SIDEBAR_COOKIE_NAME}=`))
    ?.split("=")[1];
  return value !== "false";
}

function sidebarWidthFromStorage() {
  const stored = localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY);
  if (!stored) return SIDEBAR_WIDTH_DEFAULT;
  const parsed = Number(stored);
  return Number.isFinite(parsed)
    ? clampSidebarWidth(parsed)
    : SIDEBAR_WIDTH_DEFAULT;
}
const formatNumber = (value: number) => new Intl.NumberFormat(getIntlLocale(), {
  notation: "compact",
  maximumFractionDigits: 1,
}).format(value);
const formatPercent = (value: number | null) =>
  value == null ? "--" : `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;

function Card({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <section className={`card ${className}`}>{children}</section>;
}

function CardHeader({
  title,
  meta,
  action,
}: {
  title: string;
  meta?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="card-header">
      <div>
        <h2>{title}</h2>
        {meta && <p>{meta}</p>}
      </div>
      {action}
    </div>
  );
}

function MetricCard({
  label,
  value,
  detail,
  change,
  icon: Icon,
}: {
  label: string;
  value: string;
  detail: string;
  change: number;
  icon: typeof Activity;
}) {
  const improved = change < 0;
  return (
    <Card className="metric-card">
      <div className="metric-top">
        <span className="metric-icon">
          <Icon size={17} />
        </span>
        <span className={`delta ${improved ? "good" : "bad"}`}>
          {improved ? <ArrowDownRight size={14} /> : <ArrowUpRight size={14} />}
          {Math.abs(change)}%
        </span>
      </div>
      <p className="metric-label">{label}</p>
      <strong className="metric-value">{value}</strong>
      <p className="metric-detail">{detail}</p>
    </Card>
  );
}

function Segment<T extends string>({
  values,
  value,
  onChange,
}: {
  values: Array<{ value: T; label: string }>;
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div className="segment">
      {values.map((item) => (
        <button
          key={item.value}
          className={value === item.value ? "active" : ""}
          onClick={() => onChange(item.value)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

const tooltipStyle = {
  border: "1px solid var(--border)",
  borderRadius: 8,
  background: "var(--popover)",
  color: "var(--foreground)",
  fontSize: 12,
};

function TrendsPage() {
  type TrendDimension =
    | "organization"
    | "department"
    | "project"
    | "agent"
    | "user"
    | "model"
    | "runtime"
    | "workflow"
    | "team";
  type TrendMetric = "et" | "total_tokens" | "calls";
  const [group, setGroup] = useState<TrendDimension>("project");
  const [metric, setMetric] = useState<TrendMetric>("et");
  const { data } = useQuery({
    queryKey: ["trends", group, metric],
    queryFn: () => dataSource.trends(group, metric),
  });
  if (!data) return <Loading />;
  const seriesKeys = [
    ...new Set(
      data.flatMap((row) => Object.keys(row).filter((key) => key !== "day")),
    ),
  ];
  const metricLabel =
    metric === "et"
      ? "ET 成本"
      : metric === "total_tokens"
        ? "Token"
        : "调用次数";
  return (
    <div className="page-grid">
      <Card className="span-3">
        <CardHeader
          title={`${metricLabel}时间趋势`}
          meta="按业务与模型维度聚合 Token、调用和成本"
          action={
            <div className="trend-controls">
              <div className="dimension-picker">
                <span>指标</span>
                <Select value={metric} onValueChange={(value) => value && setMetric(value as TrendMetric)}>
                  <SelectTrigger className="dimension-select-trigger" aria-label="指标"><SelectValue>{metric === "et" ? "ET 成本" : metric === "total_tokens" ? "Token" : "调用次数"}</SelectValue></SelectTrigger>
                  <SelectContent align="start" alignItemWithTrigger={false}>
                    <SelectItem value="et">ET 成本</SelectItem>
                    <SelectItem value="total_tokens">Token</SelectItem>
                    <SelectItem value="calls">调用次数</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="dimension-picker">
                <span>统计维度</span>
                <Select value={group} onValueChange={(value) => value && setGroup(value as TrendDimension)}>
                  <SelectTrigger className="dimension-select-trigger" aria-label="统计维度"><SelectValue>{group === "project" ? "按项目" : group === "agent" ? "按智能体" : group === "model" ? "按模型" : group === "department" ? "按部门" : group === "user" ? "按用户" : group === "organization" ? "按组织" : group === "runtime" ? "按运行时" : group === "workflow" ? "按 Workflow" : "按团队"}</SelectValue></SelectTrigger>
                  <SelectContent align="start" alignItemWithTrigger={false}>
                    <SelectItem value="project">按项目</SelectItem>
                    <SelectItem value="agent">按智能体</SelectItem>
                    <SelectItem value="model">按模型</SelectItem>
                    <SelectItem value="department">按部门</SelectItem>
                    <SelectItem value="user">按用户</SelectItem>
                    <SelectItem value="organization">按组织</SelectItem>
                    <SelectItem value="runtime">按运行时</SelectItem>
                    <SelectItem value="workflow">按 Workflow</SelectItem>
                    <SelectItem value="team">按团队</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
          }
        />
        <div className="chart-wrap chart-tall">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data}>
              <CartesianGrid vertical={false} stroke="var(--border)" />
              <XAxis
                dataKey="day"
                tick={{ fill: "var(--muted-foreground)" }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tickFormatter={formatNumber}
                tick={{ fill: "var(--muted-foreground)" }}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip
                contentStyle={tooltipStyle}
                formatter={(value) => formatNumber(Number(value))}
              />
              <Legend />
              {seriesKeys.map((key, index) => (
                <Bar
                  key={key}
                  dataKey={key}
                  stackId="metric"
                  fill={`var(--chart-${(index % 5) + 1})`}
                  radius={index === seriesKeys.length - 1 ? [3, 3, 0, 0] : 0}
                />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>
      <Card className="span-2">
        <CardHeader title="维度洞察" meta="当前筛选区间" />
        <div className="insight-list">
          <div>
            <span className="rank">01</span>
            <div>
              <b>commerce-platform</b>
              <p>ET 占比最高，异常 run 拉高近 3 日均值</p>
            </div>
            <strong>42.8%</strong>
          </div>
          <div>
            <span className="rank">02</span>
            <div>
              <b>developer-experience</b>
              <p>缓存利用率最高，输出 token 保持稳定</p>
            </div>
            <strong>31.4%</strong>
          </div>
          <div>
            <span className="rank">03</span>
            <div>
              <b>unattributed</b>
              <p>连续 8 日下降，仍需补齐 CI 服务账号</p>
            </div>
            <strong>7.8%</strong>
          </div>
        </div>
      </Card>
      <Card>
        <CardHeader title="模型结构" meta="ET 贡献" />
        <div className="model-bars">
          {[
            ["Claude Sonnet 4", 46],
            ["GPT-4.1 mini", 31],
            ["GPT-4.1", 15],
            ["其他", 8],
          ].map(([label, percent]) => (
            <div key={label}>
              <span>{label}</span>
              <b>{percent}%</b>
              <i>
                <em style={{ width: `${percent}%` }} />
              </i>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

function RunsPage() {
  const { data } = useQuery({ queryKey: ["runs"], queryFn: dataSource.runs });
  const [selected, setSelected] = useState(1);
  if (!data) return <Loading />;
  const run = data[selected] ?? data[0];
  const maxTokens = Math.max(
    ...run.turns.map(
      (turn) => turn.input_tokens + turn.cached_tokens + turn.output_tokens,
    ),
  );
  return (
    <div className="runs-layout">
      <Card className="run-list">
        <CardHeader title="近期 Runs" meta="选择任务查看逐轮瀑布" />
        {data.map((item, index) => (
          <button
            key={item.run_id}
            className={`run-option ${index === selected ? "selected" : ""}`}
            onClick={() => setSelected(index)}
          >
            <span
              className={`run-state ${item.turn_count > 10 ? "critical" : "healthy"}`}
            >
              <Activity size={15} />
            </span>
            <span>
              <b>{item.workflow}</b>
              <small>{item.run_id}</small>
            </span>
            <em>{item.turn_count} 轮</em>
          </button>
        ))}
      </Card>
      <div className="run-main">
        <Card>
          <div className="run-summary">
            <div>
              <span
                className={`severity-pill ${run.turn_count > 10 ? "critical" : "healthy"}`}
              >
                {run.turn_count > 10 ? "异常循环" : "运行正常"}
              </span>
              <h2>{run.workflow}</h2>
              <p>
                {run.run_id} · {run.agent} · {run.models.join(", ")}
              </p>
            </div>
            <div className="run-kpis">
              <span>
                <b>{run.turn_count}</b>轮次
              </span>
              <span>
                <b>{formatNumber(run.totals.et)}</b>ET
              </span>
              <span>
                <b>{(run.latency_ms / 1000).toFixed(1)}s</b>耗时
              </span>
            </div>
          </div>
        </Card>
        <Card>
          <CardHeader
            title="轮次瀑布"
            meta="每轮 input / cache / output 与端到端耗时"
          />
          <div className="waterfall-legend">
            <span>
              <i className="input" />
              Input
            </span>
            <span>
              <i className="cache" />
              Cache
            </span>
            <span>
              <i className="output" />
              Output
            </span>
            <span className="latency-legend">右侧为耗时</span>
          </div>
          <div className="waterfall">
            {run.turns.map((turn) => (
              <div
                className={`turn-row ${turn.estimated ? "estimated-row" : ""}`}
                key={turn.usage_id}
              >
                <span className="turn-index">
                  {String(turn.turn_index).padStart(2, "0")}
                </span>
                <div
                  className="turn-bar"
                  style={{
                    width: `${Math.max(18, ((turn.input_tokens + turn.cached_tokens + turn.output_tokens) / maxTokens) * 100)}%`,
                  }}
                >
                  <i className="input" style={{ flex: turn.input_tokens }} />
                  <i className="cache" style={{ flex: turn.cached_tokens }} />
                  <i className="output" style={{ flex: turn.output_tokens }} />
                </div>
                <span className="turn-tokens">
                  {formatNumber(
                    turn.input_tokens + turn.cached_tokens + turn.output_tokens,
                  )}
                </span>
                <span className="turn-latency">
                  {(turn.latency_ms / 1000).toFixed(1)}s
                </span>
                {turn.estimated && <span className="estimate-tag">估算</span>}
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: AuditStatus }) {
  const labels: Record<AuditStatus, string> = {
    new: "新建",
    acknowledged: "已确认",
    resolved: "已修复",
    ignored: "已忽略",
  };
  return <span className={`status-badge ${status}`}>{labels[status]}</span>;
}

function AuditPage() {
  const client = useQueryClient();
  const { data } = useQuery({
    queryKey: ["findings"],
    queryFn: dataSource.findings,
  });
  const mutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: AuditStatus }) =>
      dataSource.updateFinding(id, status),
    onSuccess: () => client.invalidateQueries({ queryKey: ["findings"] }),
  });
  if (!data) return <Loading />;
  return (
    <div className="page-grid">
      <div className="metric-grid span-3">
        <Card className="audit-stat">
          <strong>{data.filter((item) => item.status === "new").length}</strong>
          <span>待处理</span>
        </Card>
        <Card className="audit-stat">
          <strong>
            {data.filter((item) => item.severity === "critical").length}
          </strong>
          <span>严重异常</span>
        </Card>
        <Card className="audit-stat">
          <strong>92%</strong>
          <span>24h 内确认</span>
        </Card>
        <Card className="audit-stat">
          <strong>09:00</strong>
          <span>每日送达</span>
        </Card>
      </div>
      <Card className="span-3">
        <CardHeader
          title="异常审计"
          meta="规则逻辑留待 FR-4，当前支持查看与状态流转"
        />
        <div className="finding-list">
          {data.map((finding) => (
            <FindingRow
              key={finding.id}
              finding={finding}
              onStatus={(status) => mutation.mutate({ id: finding.id, status })}
            />
          ))}
        </div>
      </Card>
    </div>
  );
}

function FindingRow({
  finding,
  onStatus,
}: {
  finding: AuditFinding;
  onStatus: (status: AuditStatus) => void;
}) {
  return (
    <article className="finding-row">
      <span className={`finding-icon ${finding.severity}`}>
        <AlertTriangle size={17} />
      </span>
      <div className="finding-body">
        <div>
          <b>{finding.workflow}</b>
          <span className={`severity-text ${finding.severity}`}>
            {finding.severity}
          </span>
        </div>
        <h3>{finding.rule_id}</h3>
        <p>
          {Object.entries(finding.evidence)
            .map(([key, value]) => `${key}: ${value}`)
            .join(" · ")}
        </p>
        {finding.suggestion && <small>{finding.suggestion}</small>}
      </div>
      <div className="finding-actions">
        <StatusBadge status={finding.status} />
        <Select value={finding.status} onValueChange={(value) => value && onStatus(value as AuditStatus)}>
          <SelectTrigger className="finding-status-select" aria-label="更新审计状态"><SelectValue>{finding.status === "new" ? "新建" : finding.status === "acknowledged" ? "确认" : finding.status === "resolved" ? "已修复" : "忽略"}</SelectValue></SelectTrigger>
          <SelectContent align="end" alignItemWithTrigger={false}>
            <SelectItem value="new">新建</SelectItem>
            <SelectItem value="acknowledged">确认</SelectItem>
            <SelectItem value="resolved">已修复</SelectItem>
            <SelectItem value="ignored">忽略</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </article>
  );
}

function OptimizationPage() {
  const client = useQueryClient();
  const { data } = useQuery({
    queryKey: ["optimizations"],
    queryFn: dataSource.optimizations,
  });
  const [events, setEvents] = useState<OptimizationEvent[]>([]);
  const [open, setOpen] = useState(false);
  const mutation = useMutation({
    mutationFn: dataSource.createOptimization,
    onSuccess: (created) => {
      setEvents((current) => [created, ...current]);
      client.invalidateQueries({ queryKey: ["optimizations"] });
      setOpen(false);
    },
  });
  if (!data) return <Loading />;
  const all = [
    ...new Map([...events, ...data].map((item) => [item.id, item])).values(),
  ];
  const addEvent = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    mutation.mutate({
      workflow: String(form.get("workflow")),
      label: String(form.get("label")),
      notes: String(form.get("notes")),
      occurred_at: new Date().toISOString(),
      comparison_window_days: 7,
    });
  };
  return (
    <div className="page-grid">
      <Card className="span-3 optimization-hero">
        <div>
          <span className="eyebrow">OPTIMIZATION EVIDENCE</span>
          <h2>把每次优化变成可复现的数字</h2>
          <p>
            以事件时间为界，自动比较同一 workflow 前后等长窗口的 ET 与 Token。
          </p>
        </div>
        <button className="primary-button" onClick={() => setOpen(true)}>
          <Sparkles size={16} />
          标记优化事件
        </button>
      </Card>
      {all.map((item) => (
        <Card key={item.id} className="optimization-card">
          <div className="optimization-title">
            <span>
              <CheckCircle2 size={18} />
            </span>
            <div>
              <h3>{item.label}</h3>
              <p>
                {item.workflow} · {item.comparison.window_days} 天窗口
              </p>
            </div>
          </div>
          <div className="reduction">
            <strong>{formatPercent(item.comparison.et_change_percent)}</strong>
            <span>ET 变化</span>
          </div>
          <div className="before-after">
            <div>
              <span>优化前</span>
              <b>{formatNumber(item.comparison.before.et)} ET</b>
              <i>
                <em style={{ width: "100%" }} />
              </i>
            </div>
            <ArrowDownRight size={18} />
            <div>
              <span>优化后</span>
              <b>{formatNumber(item.comparison.after.et)} ET</b>
              <i>
                <em
                  style={{
                    width: `${Math.max(12, 100 + (item.comparison.et_change_percent ?? 0))}%`,
                  }}
                />
              </i>
            </div>
          </div>
          <p className="optimization-note">{item.notes}</p>
        </Card>
      ))}
      {open && (
        <div className="modal-backdrop" onMouseDown={() => setOpen(false)}>
          <form
            className="modal"
            onSubmit={addEvent}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="modal-head">
              <div>
                <h2>标记优化事件</h2>
                <p>系统将按事件时间计算前后窗口。</p>
              </div>
              <button
                type="button"
                className="icon-button"
                onClick={() => setOpen(false)}
                aria-label="关闭"
              >
                <X size={18} />
              </button>
            </div>
            <label>
              Workflow
              <input
                name="workflow"
                required
                defaultValue="release-regression-check"
              />
            </label>
            <label>
              事件名称
              <input name="label" required placeholder="例如：缩减工具上下文" />
            </label>
            <label>
              备注
              <textarea name="notes" rows={3} placeholder="记录具体改动" />
            </label>
            <div className="modal-actions">
              <button
                type="button"
                className="ghost-button"
                onClick={() => setOpen(false)}
              >
                取消
              </button>
              <button className="primary-button">保存并计算</button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}

function Loading() {
  return (
    <div className="loading">
      <Activity className="spin" />
      加载观测数据
    </div>
  );
}

export function App() {
  const queryClient = useQueryClient();
  const { user, photo, signOut } = useAuth();
  const assistantOwner = user?.email ?? null;
  const [selectedDataSource, setSelectedDataSource] = useState<DataSource>(dataSourceFromStorage);
  const [page, setPage] = useState<Page>(() =>
    normalizePageForSource(selectedDataSource, pageFromUrl()));
  const routedPage = normalizePageForSource(selectedDataSource, page);

  useEffect(() => {
    const syncPageFromUrl = () => {
      const nextSource = dataSourceFromStorage();
      const requestedPage = pageFromUrl();
      const nextPage = normalizePageForSource(nextSource, requestedPage);
      setSelectedDataSource(nextSource);
      setPage(nextPage);
      if (nextPage !== requestedPage) {
        const url = new URL(window.location.href);
        url.searchParams.set("page", nextPage);
        window.history.replaceState(null, "", url);
      }
    };
    window.addEventListener("popstate", syncPageFromUrl);
    window.addEventListener(FINOPS_NAVIGATE_EVENT, syncPageFromUrl);
    return () => {
      window.removeEventListener("popstate", syncPageFromUrl);
      window.removeEventListener(FINOPS_NAVIGATE_EVENT, syncPageFromUrl);
    };
  }, []);

  useEffect(() => {
    const url = new URL(window.location.href);
    localStorage.setItem(DATA_SOURCE_STORAGE_KEY, selectedDataSource);
    if (routedPage !== page) setPage(routedPage);
    if (
      url.searchParams.get("source") === selectedDataSource
      && url.searchParams.get("page") === routedPage
    ) return;
    url.searchParams.set("source", selectedDataSource);
    url.searchParams.set("page", routedPage);
    window.history.replaceState(null, "", url);
  }, [page, routedPage, selectedDataSource]);

  const [searchOpen, setSearchOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(sidebarOpenFromCookie);
  const [sidebarOpenMobile, setSidebarOpenMobile] = useState(false);
  const [nativeRouteDrawerOpen, setNativeRouteDrawerOpen] = useState(false);
  const [gatewayReleaseDrawerOpen, setGatewayReleaseDrawerOpen] = useState(false);
  const [nativeRouteAddOpen, setNativeRouteAddOpen] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(sidebarWidthFromStorage);
  const [finopsDays, setFinopsDays] = useState(30);
  const [finopsScope, setFinopsScope] = useState<FinOpsScope>({});
  const isMobile = useIsMobile();
  const currentFinopsFilters = useMemo<UsageFilters>(
    () => defaultApimFilters(finopsDays, finopsScope),
    [finopsDays, finopsScope],
  );
  const sidebarFilters = useMemo(() => usageWindow(30), []);
  const anomalyQuery = useQuery({
    ...finopsQueries.anomalies(sidebarFilters),
    enabled: selectedDataSource === "apim",
    refetchInterval: 5 * 60_000,
  });
  const anomalyCount = anomalyQuery.data?.length ?? 0;
  const [pinnedChartId, setPinnedChartId] = useState(
    () => new URLSearchParams(window.location.search).get("chart"),
  );
  const [assistantConversationId, setAssistantConversationId] = useState(
    () => new URLSearchParams(window.location.search).get("conversation"),
  );
  useEffect(() => {
    if (page === "apim-native-routes") return;
    setNativeRouteDrawerOpen(false);
    setNativeRouteAddOpen(false);
  }, [page]);
  useEffect(() => {
    if (page === "gateway-releases") return;
    setGatewayReleaseDrawerOpen(false);
  }, [page]);
  const pinnedCharts = useQuery({
    ...pinnedChartsQuery(assistantOwner),
    enabled: selectedDataSource === "apim" && Boolean(assistantOwner),
  });
  // Open by default, matching SmartHive's `<Collapsible defaultOpen>`. Not persisted for
  // the same reason it is not there: the group is small and re-expanding is one click,
  // whereas a remembered collapse can hide reports a person forgot they had.
  const [pinnedOpen, setPinnedOpen] = useState(true);
  const prefetchNavigation = (nextPage: Page) => {
    if (selectedDataSource === "github-copilot") {
      prefetchGithubCopilotPage(queryClient, nextPage);
      return;
    }
    prefetchApimPage(queryClient, nextPage, currentFinopsFilters);
  };
  const sidebarResize = useRef<{
    startX: number;
    startWidth: number;
    currentWidth: number;
    cleanup: () => void;
  } | null>(null);
  const sidebarRailDragged = useRef(false);
  const sidebarDrag = useRef<{
    pointerId: number;
    startY: number;
    startScrollTop: number;
    moved: boolean;
  } | null>(null);
  const suppressSidebarClick = useRef(false);

  const persistSidebarWidth = (width: number) => {
    localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(Math.round(width)));
  };

  const selectDataSource = (next: DataSource) => {
    setSelectedDataSource(next);
    setAssistantConversationId(null);
    localStorage.setItem(DATA_SOURCE_STORAGE_KEY, next);
    setFinopsScope({});
    const nextPage = normalizePageForSource(next, page);
    if (nextPage !== page) setPage(nextPage);
    const url = new URL(window.location.href);
    url.searchParams.set("source", next);
    url.searchParams.set("page", nextPage);
    url.searchParams.delete("runtime");
    url.searchParams.delete("request");
    url.searchParams.delete("chart");
    url.searchParams.delete("conversation");
    url.searchParams.delete("application");
    window.history.replaceState(null, "", url);
    window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT));
  };

  const toggleSidebar = () => {
    if (isMobile) {
      setSidebarOpenMobile((open) => !open);
      return;
    }
    setSidebarOpen((open) => {
      const next = !open;
      document.cookie = `${SIDEBAR_COOKIE_NAME}=${next}; Path=/; Max-Age=${SIDEBAR_COOKIE_MAX_AGE}; SameSite=Lax`;
      return next;
    });
  };

  const startSidebarResize = (event: React.MouseEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    sidebarRailDragged.current = false;
    sidebarResize.current?.cleanup();
    const resize = {
      startX: event.clientX,
      startWidth: sidebarWidth,
      currentWidth: sidebarWidth,
      cleanup: () => {},
    };
    const move = (moveEvent: MouseEvent) => {
      if (moveEvent.clientX === resize.startX) return;
      sidebarRailDragged.current = true;
      const next = clampSidebarWidth(
        resize.startWidth + moveEvent.clientX - resize.startX,
      );
      resize.currentWidth = next;
      setSidebarWidth(next);
      moveEvent.preventDefault();
    };
    const finish = () => {
      persistSidebarWidth(resize.currentWidth);
      resize.cleanup();
      sidebarResize.current = null;
      document.body.classList.remove("is-resizing-sidebar");
    };
    resize.cleanup = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", finish);
    };
    sidebarResize.current = resize;
    document.addEventListener("mousemove", move, { passive: false });
    document.addEventListener("mouseup", finish);
    document.body.classList.add("is-resizing-sidebar");
  };

  const handleSidebarRailClick = () => {
    if (!sidebarRailDragged.current) toggleSidebar();
  };

  const startSidebarDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.pointerType !== "mouse" || event.button !== 0) return;
    sidebarDrag.current = {
      pointerId: event.pointerId,
      startY: event.clientY,
      startScrollTop: event.currentTarget.scrollTop,
      moved: false,
    };
  };

  const moveSidebarDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = sidebarDrag.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const delta = event.clientY - drag.startY;
    if (!drag.moved && Math.abs(delta) < 4) return;
    if (!drag.moved) {
      event.currentTarget.setPointerCapture(event.pointerId);
    }
    drag.moved = true;
    event.currentTarget.classList.add("is-dragging");
    event.currentTarget.scrollTop = drag.startScrollTop - delta;
    event.preventDefault();
  };

  const finishSidebarDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = sidebarDrag.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    event.currentTarget.classList.remove("is-dragging");
    suppressSidebarClick.current = drag.moved;
    sidebarDrag.current = null;
  };
  const navigatePage = (next: Page, requestId?: string) => {
    setPage(next);
    const url = new URL(window.location.href);
    url.searchParams.set("page", next);
    url.searchParams.delete("runtime");
    url.searchParams.delete("tab");
    url.searchParams.delete("router");
    url.searchParams.delete("application");
    if (next !== "pinned-report") url.searchParams.delete("chart");
    // The open thread belongs to the assistant page alone. Leaving it in the URL would
    // put a stale conversation back on screen the next time that page is opened from
    // the nav, which reads as the nav entry having its own memory.
    if (next !== "assistant") url.searchParams.delete("conversation");
    if (requestId) url.searchParams.set("request", requestId);
    else url.searchParams.delete("request");
    window.history.replaceState(null, "", url);
    window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT));
  };
  /** Keeps `?conversation=` in step with the thread the assistant page has open, so a
   *  thread survives a refresh and can be linked to. */
  const setAssistantConversation = (id: string | undefined) => {
    setAssistantConversationId(id ?? null);
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("conversation", id);
    else url.searchParams.delete("conversation");
    window.history.replaceState(null, "", url);
  };
  /** The floating panel's handover: it closes itself and this opens the same thread on
   *  the full page, which is why the two surfaces need no shared live state. */
  const openAssistantPage = (conversationId?: string) => {
    setAssistantConversationId(conversationId ?? null);
    setPage("assistant");
    const url = new URL(window.location.href);
    url.searchParams.set("page", "assistant");
    if (conversationId) url.searchParams.set("conversation", conversationId);
    else url.searchParams.delete("conversation");
    window.history.replaceState(null, "", url);
  };
  const openPinnedReport = (chartId: string) => {
    setPinnedChartId(chartId);
    setPage("pinned-report");
    const url = new URL(window.location.href);
    url.searchParams.set("page", "pinned-report");
    url.searchParams.set("chart", chartId);
    window.history.replaceState(null, "", url);
  };
  const forgetPinnedReport = (id: string) => {
    // Removing the row we just deleted is exact, so invalidating and paying for a round
    // trip to be told the same thing would only add a spinner to a settled outcome.
    queryClient.setQueryData(pinnedChartsKey(assistantOwner ?? ""), (current: PinnedReport[] | undefined) =>
      current?.filter((item) => item.id !== id));
    // Deleting the report that is currently open would leave the page describing its
    // own absence, so the reader is moved off it rather than left staring at that.
    if (page === "pinned-report" && pinnedChartId === id) navigatePage("finops-overview");
  };
  const deletePinnedReport = useMutation({
    mutationFn: (id: string) => assistantApi.unpin(id),
    onSuccess: (_result, id) => forgetPinnedReport(id),
  });
  useEffect(() => {
    const handleCreateInvocationShortcut = (event: KeyboardEvent) => {
      if (selectedDataSource !== "apim") return;
      if (event.key.toLowerCase() !== "c" || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.target instanceof HTMLElement && event.target.closest("input, textarea, select, [contenteditable='true']")) return;
      event.preventDefault();
      navigatePage("finops-invoke");
    };
    document.addEventListener("keydown", handleCreateInvocationShortcut);
    return () => document.removeEventListener("keydown", handleCreateInvocationShortcut);
  }, [selectedDataSource]);
  const sourcePages = selectedDataSource === "apim" ? apimPages : githubCopilotPages;
  const pageInfo = sourcePages.find((item) => item.id === page) ?? {
    id: page,
    label:
      page === "apim-native-routes"
        ? "APIM 后端池"
        : page === "gateway-releases"
          ? "网关发布"
        : page === "applications"
          ? "订阅"
        : page === "settings"
          ? "设置"
          : "模型管理",
    icon:
      page === "apim-native-routes"
        ? Network
        : page === "gateway-releases"
          ? History
        : page === "applications"
          ? KeyRound
        : page === "settings"
          ? Settings
          : Cpu,
  };
  const finopsPage = page.startsWith("finops-");
  // Full-bleed pages: they draw their own header and own their scrolling, so the host
  // must not add the 24px page padding the card-based routes need. Leaving the assistant
  // out of this put a 24px inset around its whole two-pane surface, which pushed the
  // thread rail's border away from the card wall it is supposed to sit against.
  const workspacePage = finopsPage || page === "models" || page === "budgets" || page.startsWith("copilot-") || page === "pinned-report" || page === "assistant";  type NavGroup = {
    label: string;
    items: Array<{
      label: string;
      icon: typeof Activity;
      page: Page;
    }>;
  };
  const navGroups: NavGroup[] = [
    {
      label: "AI 用量治理",
      items: sourcePages.map((item) => ({ label: item.label, icon: item.icon, page: item.id })),
    },
  ];
  if (selectedDataSource === "apim") {
    navGroups.push({
      label: "组织管理",
      items: [
        { label: "组织与部门", icon: Building2, page: "organization" },
      ],
    });
    navGroups.push({
      label: "模型平台",
      items: [
        { label: "模型管理", icon: Cpu, page: "models" },
        { label: "APIM 后端池", icon: Network, page: "apim-native-routes" },
        { label: "网关发布", icon: History, page: "gateway-releases" },
        { label: "订阅", icon: KeyRound, page: "applications" },
      ],
    });
  }
  navGroups.push({
    label: "系统管理",
    items: [
      { label: "设置", icon: Settings, page: "settings" },
    ],
  });
  // The invocation console is deliberately absent from the sidebar: the header already
  // has 新建调用 with a keyboard shortcut, and two rows pointing at one page is clutter.
  // It stays in the palette because dropping the nav entry would otherwise make a page
  // that still exists unsearchable -- a silent loss the de-duplication never asked for.
  const searchablePages = [
    // The assistant is listed explicitly for the same reason the invocation console is:
    // this array is derived from navGroups, and both of those live outside them, so
    // neither would otherwise be findable in the palette.
    { id: "assistant" as Page, label: "FinOps Assistant", icon: Sparkles },
    ...navGroups.flatMap((group) => group.items.map((item) => ({ id: item.page, label: item.label, icon: item.icon }))),
    ...(selectedDataSource === "apim"
      ? [{ id: "finops-invoke" as Page, label: "调用测试", icon: Zap }]
      : []),
  ];
  return (
    <div
      className="app-shell"
      data-sidebar-state={sidebarOpen ? "expanded" : "collapsed"}
      style={{
        "--sidebar-width": `${sidebarWidth}px`,
        "--sidebar-layout-width": sidebarOpen ? `${sidebarWidth}px` : "0px",
      } as CSSProperties}
    >
      {sidebarOpenMobile && (
        <button
          type="button"
          className="sidebar-mobile-overlay"
          aria-label="关闭导航栏"
          onClick={() => setSidebarOpenMobile(false)}
        />
      )}
      <div
        className="sidebar-peer"
        data-state={sidebarOpen ? "expanded" : "collapsed"}
        data-collapsible={sidebarOpen ? "" : "offcanvas"}
      >
        <div className="sidebar-gap" data-slot="sidebar-gap" />
        <aside
          className={sidebarOpenMobile ? "open" : ""}
          data-state={sidebarOpen ? "expanded" : "collapsed"}
          data-collapsible={sidebarOpen ? "" : "offcanvas"}
          data-slot="sidebar-container"
        >
        <div className="sidebar-inner" data-sidebar="sidebar">
          <div className="sidebar-header" data-sidebar="header">
          <ul className="sidebar-menu">
            <li className="sidebar-menu-item workspace-menu-item">
              <div className="workspace-switcher">
                <span className="workspace-icon-slot" aria-hidden="true"><span><TurnstileMark size={16} /></span></span>
                <b data-no-localize>Turnstile</b>
              </div>
              <DropdownMenu>
                <DropdownMenuTrigger className="workspace-source-switcher" aria-label="切换数据源">
                  <span className="workspace-source-logo" aria-hidden="true">
                    {selectedDataSource === "apim" ? <ApimLogo size={17} /> : <CopilotLogo size={17} />}
                  </span>
                  <ChevronDown className="workspace-switcher-chevron" size={14} />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" sideOffset={6} className="workspace-source-menu">
                  <DropdownMenuItem onClick={() => selectDataSource("apim")}>
                    <ApimLogo size={17} />
                    <span>APIM</span>
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={() => selectDataSource("github-copilot")}>
                    <CopilotLogo size={17} />
                    <span>GitHub Copilot</span>
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <button
                className="icon-button sidebar-close"
                aria-label="关闭导航栏"
                onClick={() => setSidebarOpenMobile(false)}
              >
                <PanelLeftClose size={16} />
              </button>
            </li>
          </ul>
          <ul className="sidebar-menu sidebar-search-menu">
            <li className="sidebar-menu-item">
              <button className="sidebar-menu-button sidebar-search-trigger" type="button" onClick={() => { setSearchOpen(true); setSidebarOpenMobile(false); }}>
                <Search size={16} />
                <span>搜索...</span>
                <kbd><span>⌘</span>K</kbd>
              </button>
            </li>
            {selectedDataSource === "apim" && <li className="sidebar-menu-item">
              <button
                className="sidebar-menu-button sidebar-quick-action"
                type="button"
                onClick={() => {
                  navigatePage("finops-invoke");
                  setSidebarOpenMobile(false);
                }}
              >
                <SquarePen size={16} />
                <span>新建调用</span>
                <kbd>C</kbd>
              </button>
            </li>}
          </ul>
          </div>
          <div
            className="sidebar-scroll sidebar-content"
            aria-label="主导航"
            onPointerDown={startSidebarDrag}
            onPointerMove={moveSidebarDrag}
            onPointerUp={finishSidebarDrag}
            onPointerCancel={finishSidebarDrag}
            onClickCapture={(event) => {
              if (!suppressSidebarClick.current) return;
              event.preventDefault();
              event.stopPropagation();
              suppressSidebarClick.current = false;
            }}
          >
            <nav>
                {/* SmartHive's first SidebarGroup: primary destinations, no group label, above
                  the reports group. Chat lives there rather than inside a labelled section
                  because it is a place you go, not a report you read -- and a label would
                  imply a category that has one member. */}
              <div className="nav-group sidebar-group" key="assistant">
                <div className="sidebar-group-content">
                  <ul className="sidebar-menu sidebar-nav-menu">
                    <li className="sidebar-menu-item">
                      <button
                        className={`sidebar-menu-button ${page === "assistant" ? "active" : ""}`}
                        onClick={() => {
                          navigatePage("assistant");
                          setSidebarOpenMobile(false);
                        }}
                      >
                        <Sparkles size={16} />
                        <span>FinOps Assistant</span>
                      </button>
                    </li>
                  </ul>
                </div>
              </div>
              {selectedDataSource === "apim" && pinnedCharts.data && pinnedCharts.data.length > 0 && (
                <div className="nav-group sidebar-group pinned-group" key="pinned">
                    {/* Mirrors SmartHive's pinned group interaction, but names this report-only
                      collection by its resource. The label itself is the trigger, the caret
                      turns on open, and the count only fades in on hover. */}
                  <button
                    type="button"
                    className="nav-label pinned-group-trigger"
                    aria-expanded={pinnedOpen}
                    onClick={() => setPinnedOpen((open) => !open)}
                  >
                    <span>报表</span>
                    <ChevronRight size={12} className="pinned-group-caret" />
                    <span className="pinned-group-count">{pinnedCharts.data.length}</span>
                  </button>
                  {pinnedOpen && (
                  <div className="sidebar-group-content">
                    <ul className="sidebar-menu sidebar-nav-menu">
                      {pinnedCharts.data.map((item) => (
                        <li className="sidebar-menu-item" key={item.id}>
                          <button
                            className={`sidebar-menu-button ${page === "pinned-report" && pinnedChartId === item.id ? "active" : ""}`}
                            title={item.title}
                            /* Both the label and the tooltip are the reader's own report
                               name, so nothing inside this button may be translated. */
                            data-no-localize
                            onClick={() => {
                              openPinnedReport(item.id);
                              setSidebarOpenMobile(false);
                            }}
                          >
                            <Pin size={16} />
                            <span>{item.title}</span>
                          </button>
                          {/* Only past one: a "1" on every single-chart report is noise. */}
                          {item.charts.length > 1 && (
                            <span className="sidebar-menu-badge">{item.charts.length}</span>
                          )}
                          {/* SmartHive's pin rows close with a small X rather than a bin,
                              revealed on hover. It shares the slot with the count, which
                              yields while it is shown: two boxes pinned to the same right
                              edge would otherwise sit on top of each other. */}
                          {item.can_manage && <button
                            type="button"
                            className="sidebar-menu-action"
                            title="删除报表"
                            aria-label="删除报表"
                            disabled={deletePinnedReport.isPending}
                            onClick={() => deletePinnedReport.mutate(item.id)}
                          ><X size={12} /></button>}
                        </li>
                      ))}
                    </ul>
                  </div>
                  )}
                </div>
              )}
              {navGroups.map((group) => (
                <div className="nav-group sidebar-group" key={group.label}>
                  <span className="nav-label">{group.label}</span>
                  <div className="sidebar-group-content">
                    <ul className="sidebar-menu sidebar-nav-menu">
                      {group.items.map((item) => (
                        <li className="sidebar-menu-item" key={item.label}>
                          <button
                            className={`sidebar-menu-button ${item.page === page ? "active" : ""}`}
                            onMouseEnter={() => prefetchNavigation(item.page)}
                            onFocus={() => prefetchNavigation(item.page)}
                            onClick={() => {
                              navigatePage(item.page);
                              setSidebarOpenMobile(false);
                            }}
                          >
                            <item.icon size={16} />
                            <span>{item.label}</span>
                            {item.page === "finops-governance" && anomalyCount > 0 && <em>{anomalyCount}</em>}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              ))}
            </nav>
          </div>
          <div className="sidebar-footer" data-sidebar="footer">
            <div className="sidebar-footer-actions">
              {/* Who is signed in, and the only way out. The sign-in method is shown as a
                  small mark on the avatar rather than as text: on a shared screen the
                  difference between a real Microsoft identity and a temporary demo account
                  is worth seeing at a glance, and it costs no room. */}
              {user && (
                <DropdownMenu>
                  <DropdownMenuTrigger className="sidebar-account" title={user.email}>
                    {photo ? (
                      <img className="sidebar-avatar" src={photo} alt="" />
                    ) : (
                      <span className="sidebar-avatar" aria-hidden="true">
                        {(user.name ?? user.email).trim().charAt(0).toUpperCase() || "?"}
                      </span>
                    )}
                    <span className="sidebar-account-text">
                      {/* A password account has no name to show, so the email is the
                          identity rather than a subtitle under an invented one. */}
                      <b>{user.name ?? user.email}</b>
                      {user.name && <small>{user.email}</small>}
                    </span>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start" side="top" sideOffset={8} className="sidebar-help-menu">
                    <DropdownMenuItem onClick={() => void signOut()}>
                      <LogOut size={16} />
                      <span>退出登录</span>
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
              {/* The help button that used to sit here offered Executive Overview and
                  Settings -- both of them rows in the navigation a few pixels above. An
                  affordance labelled "help" that only repeats the menu teaches people
                  that it is not worth opening. */}
            </div>
          </div>
        </div>
        <button
          type="button"
          className="sidebar-resize-rail"
          data-sidebar="rail"
          data-slot="sidebar-rail"
          aria-label="切换导航栏"
          title="切换导航栏"
          tabIndex={-1}
          onClick={handleSidebarRailClick}
          onMouseDown={startSidebarResize}
        />
        </aside>
      </div>
      <SearchCommand
        open={searchOpen}
        onOpenChange={setSearchOpen}
        items={searchablePages}
        onSelect={(id) => navigatePage(id as Page)}
      />
      <main>
        {!workspacePage && (
          <header className="topbar settings-mobile-topbar">
            <div className="settings-mobile-topbar-actions">
              <Button
                variant="ghost"
                size="icon-sm"
                className="menu-button"
                aria-label="切换导航栏"
                title="切换导航栏"
                onClick={toggleSidebar}
              >
                <PanelLeft size={16} />
              </Button>
              {selectedDataSource === "apim" && page === "apim-native-routes" && <Button type="button" variant="ghost" size="icon-sm" aria-label="打开 APIM 后端池列表" title="打开 APIM 后端池列表" onClick={() => setNativeRouteDrawerOpen(true)}><List size={16} /></Button>}
              {selectedDataSource === "apim" && page === "gateway-releases" && <Button type="button" variant="ghost" size="icon-sm" aria-label="打开网关发布列表" title="打开网关发布列表" onClick={() => setGatewayReleaseDrawerOpen(true)}><List size={16} /></Button>}
            </div>
            <strong>{pageInfo.label}</strong>
            <div id="mobile-topbar-end-actions" className="settings-mobile-topbar-end-actions">
              {selectedDataSource === "apim" && page === "apim-native-routes" && user?.role === "owner" && <Button type="button" variant="ghost" size="icon-sm" className="settings-mobile-topbar-end-action" aria-label="添加后端池" title="添加后端池" onClick={() => setNativeRouteAddOpen(true)}><Plus size={16} /></Button>}
            </div>
          </header>
        )}
        <div
          className={`page ${page === "settings" ? "settings-host" : ""} ${page === "models" || page === "apim-native-routes" || page === "gateway-releases" || page === "applications" ? "registry-workspace-host" : ""} ${workspacePage ? "finops-workspace-host" : ""}`}
        >
          {page === "settings" && <SettingsPage dataSource={selectedDataSource} />}
          {selectedDataSource === "apim" && page === "models" && <ModelManagementPage onToggleSidebar={toggleSidebar} />}
          {selectedDataSource === "apim" && page === "apim-native-routes" && <ApimNativeRoutesPage routeDrawerOpen={nativeRouteDrawerOpen} onRouteDrawerOpenChange={setNativeRouteDrawerOpen} addOpen={nativeRouteAddOpen} onAddOpenChange={setNativeRouteAddOpen} />}
          {selectedDataSource === "apim" && page === "gateway-releases" && <GatewayReleasesPage releaseDrawerOpen={gatewayReleaseDrawerOpen} onReleaseDrawerOpenChange={setGatewayReleaseDrawerOpen} />}
          {selectedDataSource === "apim" && page === "applications" && <ApplicationsPage />}
          {selectedDataSource === "apim" && page === "organization" && <OrganizationPage />}
          {page === "assistant" && (
            <AssistantPage
              key={selectedDataSource}
              source={selectedDataSource}
              conversationId={assistantConversationId}
              onConversationChange={setAssistantConversation}
              onToggleSidebar={toggleSidebar}
            />
          )}
          {selectedDataSource === "apim" && page === "pinned-report" && pinnedChartId && (
            <PinnedReportPage chartId={pinnedChartId} onToggleSidebar={toggleSidebar} onDeleted={forgetPinnedReport} />
          )}
          {selectedDataSource === "apim" && renderApimPage({ page, onToggleSidebar: toggleSidebar, days: finopsDays, scope: finopsScope, onDaysChange: setFinopsDays, onScopeChange: setFinopsScope, onOpenRequest: (requestId) => navigatePage("finops-requests", requestId) })}
          {selectedDataSource === "github-copilot" && githubCopilotOwnsPage(page) && renderGithubCopilotPage(page, toggleSidebar)}
        </div>
      </main>
      {/* SmartHive's `isFloatingChatRouteSuppressed`, and for its reason: the full page
          already owns this conversation, so a floating copy of it would be duplication
          the reader has to reconcile. */}
      {page !== "assistant" && (
        <AssistantPanel
          key={selectedDataSource}
          source={selectedDataSource}
          onOpenFullPage={openAssistantPage}
        />
      )}
    </div>
  );
}
