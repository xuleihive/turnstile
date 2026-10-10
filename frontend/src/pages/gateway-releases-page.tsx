import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  Box,
  CheckCircle2,
  CircleDashed,
  History,
  KeyRound,
  Layers3,
  ListFilter,
  Pin,
  PinOff,
  RefreshCw,
  RotateCcw,
  ScanSearch,
  Search,
  ShieldAlert,
  ShieldCheck,
  X,
  XCircle,
} from "lucide-react"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../components/ui/alert-dialog"
import { Button } from "../components/ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu"
import { useIsMobile } from "../hooks/use-mobile"
import { ApiError, dataSource } from "../data-sources/apim/api"
import { finopsKeys, finopsQueries } from "../data-sources/apim/queries"
import type {
  GatewayReleaseChangeSet,
  GatewayReleaseDetail,
  GatewayReleaseRole,
  GatewayReleaseOperation,
  GatewayReleaseSummary,
} from "../data-sources/apim/types"
import { useResizablePane } from "../components/ui/use-resizable-pane"
import { useTimezone } from "../providers/timezone-provider"
import { getIntlLocale } from "../locales"
import { useAuth } from "../providers/auth-provider"

const RELEASE_PANE_KEY = "turnstile_gateway_release_pane_width"
const RELEASE_PANE_DEFAULT = 336
const RELEASE_PANE_MIN = 272
const RELEASE_PANE_MAX = 460

type ReleaseFilter = "all" | GatewayReleaseRole

const roleLabels: Record<GatewayReleaseRole, string> = {
  current: "当前版本",
  immediate_rollback: "立即回滚版本",
  pinned: "已固定",
  superseded: "历史版本",
  failed: "发布失败",
  expired: "已过期",
  in_progress: "未完成",
  rolled_back: "已回退",
}

const releaseIndicatorRoles = new Set<GatewayReleaseRole>([
  "current",
  "immediate_rollback",
  "pinned",
  "failed",
  "in_progress",
  "rolled_back",
  "expired",
])

const publicationKindLabels: Record<GatewayReleaseSummary["publication_kind"], string> = {
  model_add: "添加模型",
  model_remove: "移除模型",
  credential_rotation: "轮换凭据",
  route_reconcile: "路由协调",
}

const auditStatusLabels: Record<string, string> = {
  queued: "已排队",
  validating: "验证配置",
  provisioning: "配置资源",
  building_revision: "构建 Revision",
  verifying: "验证 Revision",
  awaiting_authorization: "等待授权",
  promoting: "提升 Revision",
  active: "已生效",
  failed: "失败",
  superseded: "已取代",
  rolling_back: "回退中",
  rolled_back: "已回退",
  validating_dependencies: "验证依赖",
  preflight_probing: "候选探测",
  verifying_readback: "确认 Revision",
  post_promotion_probing: "提升后探测",
  restoring: "恢复先前版本",
  succeeded: "操作完成",
  restored: "已自动恢复",
}

function releaseFromUrl() {
  return new URL(window.location.href).searchParams.get("release")
}

function setReleaseInUrl(releaseId: string | null) {
  const url = new URL(window.location.href)
  if (releaseId) url.searchParams.set("release", releaseId)
  else url.searchParams.delete("release")
  window.history.replaceState(null, "", url)
}

function formatTimestamp(value: string | null, timezone: string) {
  if (!value) return "—"
  return new Date(value).toLocaleString(getIntlLocale(), {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: timezone,
  })
}

function formatAuditTimestamp(value: string, timezone: string) {
  return new Date(value).toLocaleString(getIntlLocale(), {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: timezone,
  })
}

function shortHash(value: string | null) {
  return value ? `${value.slice(0, 10)}…${value.slice(-6)}` : "—"
}

function auditActorLabel(actor: string) {
  return /^[a-f0-9]{64}$/i.test(actor) ? null : actor
}

function changeCount(changes: GatewayReleaseChangeSet) {
  return Object.values(changes).reduce((total, values) => total + values.length, 0)
}

function ReleaseRole({ role, status, hideSuperseded = false }: { role: GatewayReleaseRole; status: GatewayReleaseSummary["status"]; hideSuperseded?: boolean }) {
  if (hideSuperseded && role === "superseded") return null
  const progressing = role === "in_progress" && !["queued", "awaiting_authorization"].includes(status)
  const Icon = role === "current" ? CheckCircle2
    : role === "immediate_rollback" ? RotateCcw
    : role === "failed" ? XCircle
    : role === "expired" ? XCircle
    : role === "pinned" ? Pin
    : progressing ? RefreshCw
    : CircleDashed
  const label = role === "in_progress" ? auditStatusLabels[status] ?? status : roleLabels[role]
  return <span className={`gateway-release-role ${role} ${status}`}><Icon className={progressing ? "spin" : ""} size={12} />{label}</span>
}

function ReleaseStatusDot({ role, labelled = false, reserve = false }: {
  role: GatewayReleaseRole
  labelled?: boolean
  reserve?: boolean
}) {
  if (!releaseIndicatorRoles.has(role)) {
    return reserve ? <i className="gateway-release-status-dot empty" aria-hidden="true" /> : null
  }
  const className = `gateway-release-status-dot ${role}`
  return labelled
    ? <i className={className} role="img" aria-label={roleLabels[role]} title={roleLabels[role]} />
    : <i className={className} aria-hidden="true" />
}

function ReleaseListRow({ release, selected, onSelect }: {
  release: GatewayReleaseSummary
  selected: boolean
  onSelect: () => void
}) {
  return <button type="button" className={`gateway-release-list-row ${selected ? "active" : ""}`} onClick={onSelect}>
    <span className="gateway-release-list-leading"><History size={15} /><ReleaseStatusDot role={release.role} labelled /></span>
    <span className="gateway-release-list-copy">
      <span><b>Generation {release.generation}</b></span>
      <small title={`${publicationKindLabels[release.publication_kind]} · ${release.display_name}`}><span>{publicationKindLabels[release.publication_kind]}</span><i>·</i><span>{release.display_name}</span></small>
    </span>
  </button>
}

function ChangeColumn({ title, added, removed, changed }: {
  title: string
  added: string[]
  removed: string[]
  changed: string[]
}) {
  const empty = !added.length && !removed.length && !changed.length
  return <div className="gateway-release-change-column">
    <h3>{title}</h3>
    {empty ? <p>没有语义变更</p> : <div className="gateway-release-change-list">
      {added.map((value) => <span className="added" key={`added-${value}`}><b>已新增</b><code title={value} data-no-localize>{value}</code></span>)}
      {removed.map((value) => <span className="removed" key={`removed-${value}`}><b>已移除</b><code title={value} data-no-localize>{value}</code></span>)}
      {changed.map((value) => <span className="changed" key={`changed-${value}`}><b>已变更</b><code title={value} data-no-localize>{value}</code></span>)}
    </div>}
  </div>
}

function DependencyList({ icon: Icon, title, values }: {
  icon: typeof Box
  title: string
  values: string[]
}) {
  return <div className="gateway-release-dependency-list">
    <h3><Icon size={14} />{title}<span>{values.length}</span></h3>
    {values.length ? <div>{values.map((value) => <code title={value} key={value}>{value}</code>)}</div> : <p>没有记录的资源</p>}
  </div>
}

function GatewayCleanupStatus({ operation }: { operation: GatewayReleaseOperation }) {
  const unavailable = !operation.worker_available && !["succeeded", "failed", "restored"].includes(operation.status)
  const terminal = ["succeeded", "failed", "restored"].includes(operation.status)
  const title = unavailable ? "扫描未启动"
    : operation.status === "failed" ? "闲置资源扫描失败"
    : operation.status === "succeeded" ? "闲置资源扫描完成"
    : "正在扫描闲置 APIM 资源"
  const message = unavailable ? "Release Worker 未部署，扫描未被处理。"
    : operation.error_message ?? (terminal ? "已完成整个 Gateway 的 APIM 引用检查。"
      : operation.status === "queued" ? "等待 Release Worker 开始扫描。"
      : "正在读取 APIM 配置并构建完整引用图。")
  return <div className={`gateway-release-gateway-operation ${unavailable ? "unavailable" : operation.status}`} role="status" aria-live="polite">
    {unavailable || operation.status === "failed" || operation.status === "restored" ? <AlertTriangle size={15} /> : terminal ? <CheckCircle2 size={15} /> : <RefreshCw className="spin" size={15} />}
    <div><b>{title}</b><span>{message}</span></div>
    {operation.gc_plan && <small><b data-no-localize>{operation.gc_plan.candidates.length}</b> 个清理候选；未删除资源</small>}
  </div>
}

function ReleaseDetail({ release, loading, canManage, operationsEnabled, busy, operation, onPin, onCheck, onRollback, showMobileActions }: {
  release: GatewayReleaseDetail | undefined
  loading: boolean
  canManage: boolean
  operationsEnabled: boolean
  busy: boolean
  operation: GatewayReleaseOperation | undefined
  onPin: () => void
  onCheck: () => void
  onRollback: () => void
  showMobileActions: boolean
}) {
  const { timezone } = useTimezone()
  if (loading) return <div className="gateway-release-detail-state"><RefreshCw className="spin" size={20} />加载网关发布详情</div>
  if (!release) return <div className="gateway-release-detail-state"><History size={24} />选择一个网关发布版本</div>
  const changes = release.diff_from_base.changes
  const isCurrentRelease = release.role === "current"
  const liveStatus = release.dependencies.live_status
  const readinessTitle = isCurrentRelease ? "线上配置状态" : "回滚就绪状态"
  const readinessStatus = isCurrentRelease
    ? liveStatus === "healthy" ? "一致" : liveStatus === "not_checked" ? "未检查" : "发现漂移"
    : liveStatus === "healthy" ? release.rollback_eligible ? "可回滚" : "受阻"
    : liveStatus === "not_checked" ? "需要检查" : "不可回滚"
  const readinessMessage = isCurrentRelease
    ? liveStatus === "healthy" ? "当前 APIM 配置与此 Release 的记录一致。"
    : liveStatus === "not_checked" ? "运行检查以比较当前 APIM 配置与此 Release 的记录。"
    : "当前 APIM 配置与此 Release 的记录不同。当前流量不一定受影响，但需要协调配置漂移。"
    : liveStatus === "healthy" ? release.rollback_eligible
      ? "当前 Azure 资源与此 Release 的记录一致，可以作为回滚目标。"
      : "当前 Azure 资源与此 Release 的记录一致，但仍有其他条件阻止回滚。"
    : liveStatus === "not_checked" ? "使用此 Release 回滚前，先运行 Azure 实时检查。"
    : "当前 Azure 资源与此 Release 的记录不同，修复差异前不能安全回滚。"
  const issueLabel = isCurrentRelease ? "配置差异" : "回滚阻塞项"
  const renderReleaseActions = (variant: "mobile" | "rail") => canManage ? <div className={`gateway-release-actions gateway-release-actions-${variant}`} role="group" aria-label="网关发布操作">
    <button type="button" className="gateway-release-action" disabled={busy} onClick={onPin} title={release.pinned ? "取消固定" : "固定此版本"}>{release.pinned ? <PinOff size={13} /> : <Pin size={13} />}<span>{release.pinned ? "取消固定" : "固定"}</span></button>
    <button type="button" className="gateway-release-action" disabled={busy || !operationsEnabled} onClick={onCheck} title={operationsEnabled ? "重新检查 Azure 依赖" : "Release Worker 未部署"}><ShieldCheck size={13} /><span>检查依赖</span></button>
    {release.role !== "current" && <button type="button" className="gateway-release-action rollback" disabled={busy || !operationsEnabled} onClick={onRollback} title={operationsEnabled ? "预览并请求回滚" : "Release Worker 未部署"}><RotateCcw size={13} /><span>回滚</span></button>}
  </div> : null
  return <div className="gateway-release-detail-scroll">
    <div className="gateway-release-detail-grid">
      <div className="gateway-release-detail-main">
        <section className="gateway-release-summary-card">
          <header className="gateway-release-detail-head">
            <div className="gateway-release-identity">
              <div className="gateway-release-identity-line"><h2>Generation {release.generation}</h2><ReleaseRole role={release.role} status={release.status} /></div>
            </div>
          </header>
          <dl className="gateway-release-facts">
            <div><dt>APIM REVISION</dt><dd>{release.apim_revision ? <code data-no-localize>{release.apim_revision}</code> : <span>尚未创建</span>}</dd></div>
            <div><dt>发布类型</dt><dd>{publicationKindLabels[release.publication_kind]}</dd></div>
            <div><dt>{release.completed_at ? "完成时间" : "创建时间"}</dt><dd>{formatTimestamp(release.completed_at ?? release.created_at, timezone)}</dd></div>
            <div><dt>操作者</dt><dd>{release.created_by}</dd></div>
          </dl>
          {showMobileActions && renderReleaseActions("mobile")}
          {release.status === "queued" && <div className="gateway-release-operation queued" role="status">
            <CircleDashed size={15} />
            <div><b>已排队，尚未开始</b><span>正在等待 Publication Worker 处理。</span></div>
          </div>}
          {operation && <div className={`gateway-release-operation ${!operation.worker_available && !["succeeded", "failed", "restored"].includes(operation.status) ? "unavailable" : operation.status}`} role="status" aria-live="polite">
            {!operation.worker_available && !["succeeded", "failed", "restored"].includes(operation.status) ? <AlertTriangle size={15} /> : operation.status === "failed" || operation.status === "restored" ? <AlertTriangle size={15} /> : operation.status === "succeeded" ? <CheckCircle2 size={15} /> : <RefreshCw className="spin" size={15} />}
            <div>{!operation.worker_available && !["succeeded", "failed", "restored"].includes(operation.status) ? <><b>操作未启动</b><span>Release Worker 未部署，操作未被处理。</span></> : <><b>{auditStatusLabels[operation.status] ?? operation.status}</b>{operation.error_message ? <span>{operation.error_message}</span> : <span>操作 <code data-no-localize>{operation.id.slice(0, 8)}</code> 正在由控制平面处理</span>}</>}</div>
          </div>}
        </section>

        <section className="gateway-release-card gateway-release-changes-card">
          <header className="gateway-release-card-head"><div><h2>相对基础版本的变更</h2></div><span data-no-localize>{changeCount(changes)}</span></header>
          <div className="gateway-release-change-grid">
            <ChangeColumn title="模型" added={changes.added_models} removed={changes.removed_models} changed={changes.changed_models} />
            <ChangeColumn title="负载均衡" added={changes.added_backend_pools} removed={changes.removed_backend_pools} changed={changes.changed_backend_pools} />
          </div>
        </section>

        <section className="gateway-release-card gateway-release-dependency-card">
          <header className="gateway-release-card-head" data-state={liveStatus}><div><h2>{readinessTitle}</h2></div><span>{readinessStatus}</span></header>
          <div className={`gateway-release-integrity ${liveStatus}`}><p>{readinessMessage}</p></div>
          {release.dependencies.issues.length > 0 && <details className="gateway-release-disclosure">
            <summary><span>{issueLabel}</span><small>{release.dependencies.issues.length}</small></summary>
            <div className="gateway-release-integrity-issues">{release.dependencies.issues.map((issue) => <code key={issue}>{issue}</code>)}</div>
          </details>}
          <details className="gateway-release-disclosure">
            <summary><span>技术证据</span></summary>
            <div className="gateway-release-evidence">
              <dl className="gateway-release-hashes">
                <div><dt>BASE POLICY SHA</dt><dd><code title={release.dependencies.parent_policy_sha256 ?? undefined}>{shortHash(release.dependencies.parent_policy_sha256)}</code></dd></div>
                <div><dt>COMPILED POLICY SHA</dt><dd><code title={release.dependencies.compiled_policy_sha256 ?? undefined}>{shortHash(release.dependencies.compiled_policy_sha256)}</code></dd></div>
                <div><dt>DESIRED SPEC SHA</dt><dd><code title={release.desired_spec_sha256}>{shortHash(release.desired_spec_sha256)}</code></dd></div>
              </dl>
              <div className="gateway-release-dependency-grid">
                <DependencyList icon={Box} title="Backends" values={release.dependencies.backends} />
                <DependencyList icon={Layers3} title="Backend Pools" values={release.dependencies.backend_pools} />
                <DependencyList icon={KeyRound} title="Named Values" values={release.dependencies.named_values} />
                {Boolean(release.dependencies.oauth_credentials?.length) && <DependencyList icon={KeyRound} title="OAuth Credentials" values={release.dependencies.oauth_credentials ?? []} />}
              </div>
            </div>
          </details>
        </section>
      </div>

      <aside className="gateway-release-detail-rail">
        {canManage && !showMobileActions && <section className="gateway-release-card gateway-release-actions-card">
          <header className="gateway-release-card-head"><div><h2>操作</h2></div></header>
          {renderReleaseActions("rail")}
        </section>}
        <section className="gateway-release-card gateway-release-audit-card">
          <header className="gateway-release-card-head"><div><h2>发布审计</h2></div><span>{release.audit.length}</span></header>
          <div className="gateway-release-timeline">{release.audit.map((event) => {
            const actor = auditActorLabel(event.actor)
            return <div key={event.id}><i /><span><b>{auditStatusLabels[event.to_status] ?? event.to_status}</b><small><time>{formatAuditTimestamp(event.created_at, timezone)}</time>{actor && <span title={event.actor}>· {actor}</span>}</small></span></div>
          })}</div>
        </section>
      </aside>
    </div>
  </div>
}

export function GatewayReleasesPage({
  releaseDrawerOpen,
  onReleaseDrawerOpenChange,
}: {
  releaseDrawerOpen: boolean
  onReleaseDrawerOpenChange: (open: boolean) => void
}) {
  const { user } = useAuth()
  const isMobile = useIsMobile()
  const releaseDrawerPortalRef = useRef<HTMLDivElement>(null)
  const queryClient = useQueryClient()
  const releases = useQuery(finopsQueries.gatewayReleases())
  const [selectedId, setSelectedId] = useState<string | null>(releaseFromUrl)
  const [search, setSearch] = useState("")
  const [filter, setFilter] = useState<ReleaseFilter>("all")
  const [rollbackId, setRollbackId] = useState<string | null>(null)
  const [activeOperationId, setActiveOperationId] = useState<string | null>(null)
  const {
    width,
    minWidth,
    maxWidth,
    startResize,
    resizeWithKeyboard,
    resetWidth,
  } = useResizablePane({
    storageKey: RELEASE_PANE_KEY,
    defaultWidth: RELEASE_PANE_DEFAULT,
    minWidth: RELEASE_PANE_MIN,
    maxWidth: RELEASE_PANE_MAX,
  })
  const items = releases.data?.items ?? []
  const visible = useMemo(() => {
    const normalized = search.trim().toLocaleLowerCase()
    return items.filter((release) => (filter === "all" || release.role === filter)
      && (!normalized || `${release.apim_revision ?? ""} ${release.display_name} ${release.model_key} ${release.created_by}`.toLocaleLowerCase().includes(normalized)))
  }, [filter, items, search])
  useEffect(() => {
    if (selectedId && items.some((release) => release.id === selectedId)) return
    const next = items.find((release) => release.role === "current")?.id ?? items[0]?.id ?? null
    setSelectedId(next)
    setReleaseInUrl(next)
  }, [items, selectedId])
  useEffect(() => {
    const sync = () => setSelectedId(releaseFromUrl())
    window.addEventListener("popstate", sync)
    return () => window.removeEventListener("popstate", sync)
  }, [])
  useEffect(() => {
    onReleaseDrawerOpenChange(false)
  }, [isMobile, onReleaseDrawerOpenChange])
  const detail = useQuery(finopsQueries.gatewayRelease(selectedId))
  const rollbackPreview = useQuery(finopsQueries.gatewayReleaseRollbackPreview(rollbackId, Boolean(rollbackId)))
  const operation = useQuery(finopsQueries.gatewayReleaseOperation(activeOperationId))
  const refreshReleaseState = () => {
    void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayReleases })
    if (selectedId) {
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayRelease(selectedId) })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayReleaseIntegrity(selectedId) })
    }
  }
  const protectionMutation = useMutation({
    mutationFn: async (release: GatewayReleaseDetail) => {
      if (release.pinned) await dataSource.unprotectGatewayRelease(release.id)
      else await dataSource.protectGatewayRelease(release.id, { pinned: true })
    },
    onSuccess: refreshReleaseState,
  })
  const integrityMutation = useMutation({
    mutationFn: (id: string) => dataSource.requestGatewayReleaseIntegrity(id),
    onSuccess: (accepted) => setActiveOperationId(accepted.operation.id),
  })
  const rollbackMutation = useMutation({
    mutationFn: ({ id, confirmation }: { id: string; confirmation: string }) => dataSource.requestGatewayReleaseRollback(id, confirmation),
    onSuccess: (accepted) => {
      setRollbackId(null)
      setActiveOperationId(accepted.operation.id)
    },
  })
  const gcMutation = useMutation({
    mutationFn: (gatewayId: string) => dataSource.requestGatewayReleaseGcPlan(gatewayId),
    onSuccess: (accepted) => setActiveOperationId(accepted.operation.id),
  })
  const operationsEnabled = releases.data?.operations_enabled === true
  const operationTerminal = operation.data && ["succeeded", "failed", "restored"].includes(operation.data.status)
  const gatewayOperation = operation.data?.operation_kind === "gc_plan" ? operation.data : undefined
  const releaseOperation = operation.data?.operation_kind !== "gc_plan" && operation.data?.target_release_id === selectedId ? operation.data : undefined
  useEffect(() => {
    if (operationTerminal) refreshReleaseState()
  }, [operationTerminal])
  const chooseRelease = (id: string, closeDrawer = false) => {
    setSelectedId(id)
    setReleaseInUrl(id)
    if (closeDrawer) onReleaseDrawerOpenChange(false)
  }
  const apiMissing = releases.error instanceof ApiError && releases.error.status === 404
  const busy = protectionMutation.isPending || integrityMutation.isPending || rollbackMutation.isPending || gcMutation.isPending || Boolean(operation.data?.worker_available && !operationTerminal)
  const releaseActionError = protectionMutation.error ?? integrityMutation.error ?? rollbackMutation.error
  const primaryFilters: ReleaseFilter[] = ["all", "current"]
  const secondaryFilters: GatewayReleaseRole[] = ["immediate_rollback", "in_progress", "pinned", "failed", "rolled_back", "expired", "superseded"]
  const releaseListContent = (closeOnSelect: boolean) => <>
    <div className="gateway-release-list-controls">
      <label className="smh-search"><Search size={14} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索 Revision、模型或操作者..." aria-label="搜索网关发布" /></label>
      <div className="gateway-release-filters" role="group" aria-label="网关发布状态筛选">
        {primaryFilters.filter((value) => value === "all" || items.some((release) => release.role === value)).map((value) => <button type="button" key={value} className={filter === value ? "active" : ""} aria-pressed={filter === value} onClick={() => setFilter(value)}>{value !== "all" && <ReleaseStatusDot role={value} />}<span>{value === "all" ? "全部" : roleLabels[value]}</span><small>{value === "all" ? items.length : items.filter((release) => release.role === value).length}</small></button>)}
        <DropdownMenu>
          <DropdownMenuTrigger render={<button type="button" className={filter !== "all" && filter !== "current" ? "gateway-release-filter-more active" : "gateway-release-filter-more"} aria-label="更多状态" title="更多状态" />}><ListFilter size={13} /></DropdownMenuTrigger>
          <DropdownMenuContent portalContainer={closeOnSelect ? releaseDrawerPortalRef : undefined} align="start">
            {secondaryFilters.filter((value) => items.some((release) => release.role === value)).map((value) => <DropdownMenuItem key={value} onClick={() => setFilter(value)}><ReleaseStatusDot role={value} reserve /><span>{roleLabels[value]}</span><small className="gateway-release-filter-count">{items.filter((release) => release.role === value).length}</small></DropdownMenuItem>)}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
    <div className="gateway-release-list" role="list" aria-label="网关发布列表">
      <div className="gateway-release-list-section-label"><span>网关发布</span><i /></div>
      {visible.map((release) => <div role="listitem" key={release.id}><ReleaseListRow release={release} selected={release.id === selectedId} onSelect={() => chooseRelease(release.id, closeOnSelect)} /></div>)}
      {!visible.length && <div className="gateway-release-list-empty">没有匹配的网关发布</div>}
    </div>
    <footer>{`${visible.length} 个网关发布`}</footer>
  </>

  return <div className="smh-workspace gateway-releases-workspace">
    <header className="smh-page-header gateway-releases-header">
      <div><span className="smh-header-icon"><History size={17} /></span><h1>网关发布</h1><span>{releases.isLoading ? "—" : items.length}</span></div>
      <div className="smh-header-actions">
        {releases.data && !operationsEnabled && <span className="gateway-release-worker-state" title="检查依赖、回滚和清理计划暂不可用。"><AlertTriangle size={13} />Release Worker 未部署</span>}
        {user?.role === "owner" && detail.data ? <Button variant="outline" disabled={busy || !operationsEnabled} onClick={() => gcMutation.mutate(detail.data!.gateway_profile_id)} title={operationsEnabled ? "扫描整个 Gateway 的闲置 APIM 资源" : "Release Worker 未部署"}><ScanSearch size={13} />扫描闲置资源</Button> : <span className="gateway-releases-readonly"><ShieldAlert size={13} />只读</span>}
      </div>
    </header>
    {isMobile && user?.role === "owner" && detail.data && <div className="gateway-release-mobile-maintenance">
      <Button variant="outline" disabled={busy || !operationsEnabled} onClick={() => gcMutation.mutate(detail.data!.gateway_profile_id)} title={operationsEnabled ? "扫描整个 Gateway 的闲置 APIM 资源" : "Release Worker 未部署"}><ScanSearch size={13} />扫描闲置资源</Button>
    </div>}
    {gcMutation.error && <div className="gateway-release-gateway-error" role="alert"><AlertTriangle size={14} /><span>{String(gcMutation.error)}</span></div>}
    {gatewayOperation && <GatewayCleanupStatus operation={gatewayOperation} />}
    {releases.isLoading ? <div className="registry-loading"><RefreshCw className="spin" />加载网关发布</div>
      : releases.error ? <div className="registry-error">{apiMissing ? "当前环境尚未部署网关发布 API。" : `无法加载网关发布：${String(releases.error)}`}</div>
      : !items.length ? <div className="smh-empty"><History size={28} /><b>还没有网关发布记录</b><span>模型或后端池首次发布后会显示在这里。</span></div>
      : <div className="gateway-release-layout" style={{ "--gateway-release-pane-width": `${width}px` } as React.CSSProperties}>
        {!isMobile && <aside className="gateway-release-list-pane">{releaseListContent(false)}</aside>}
        <button className="runtime-pane-handle" type="button" role="separator" aria-label="调整网关发布列表宽度" aria-orientation="vertical" aria-valuemin={minWidth} aria-valuemax={maxWidth} aria-valuenow={width} onPointerDown={startResize} onKeyDown={resizeWithKeyboard} onDoubleClick={resetWidth} />
        <main className="gateway-release-detail">
          {releaseActionError && <div className="gateway-release-action-error" role="alert"><AlertTriangle size={14} /><span>{String(releaseActionError)}</span></div>}
          <ReleaseDetail release={detail.data} loading={detail.isLoading} canManage={user?.role === "owner"} operationsEnabled={operationsEnabled} busy={busy} operation={releaseOperation} onPin={() => detail.data && protectionMutation.mutate(detail.data)} onCheck={() => detail.data && integrityMutation.mutate(detail.data.id)} onRollback={() => detail.data && setRollbackId(detail.data.id)} showMobileActions={isMobile} />
        </main>
      </div>}
    {isMobile && releases.data && items.length > 0 && <Dialog open={releaseDrawerOpen} onOpenChange={onReleaseDrawerOpenChange}>
      <DialogContent className="apim-native-route-drawer gateway-release-drawer" finalFocus={false}>
        <DialogHeader className="apim-native-route-drawer-header"><DialogTitle>网关发布</DialogTitle><DialogDescription className="sr-only">选择一个网关发布版本</DialogDescription><DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="apim-native-route-drawer-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose></DialogHeader>
        <aside className="gateway-release-list-pane gateway-release-drawer-list">{releaseListContent(true)}</aside>
        <div ref={releaseDrawerPortalRef} className="gateway-release-drawer-portals" />
      </DialogContent>
    </Dialog>}
    <AlertDialog open={Boolean(rollbackId)} onOpenChange={(open) => { if (!open && !rollbackMutation.isPending) setRollbackId(null) }}>
      <AlertDialogContent className="gateway-release-rollback-dialog">
        <AlertDialogHeader>
          <div className="gateway-release-rollback-heading"><span><RotateCcw size={18} /></span><div><AlertDialogTitle>确认网关回滚</AlertDialogTitle><AlertDialogDescription>控制平面会重新检查依赖、探测候选 Revision、提升并再次探测；失败时自动恢复当前版本。</AlertDialogDescription></div></div>
        </AlertDialogHeader>
        {rollbackPreview.isLoading ? <div className="gateway-release-rollback-state"><RefreshCw className="spin" size={15} />生成语义预览</div>
          : rollbackPreview.error ? <div className="gateway-release-rollback-state error"><AlertTriangle size={15} />{String(rollbackPreview.error)}</div>
          : rollbackPreview.data && <div className="gateway-release-rollback-preview">
            <dl><div><dt>目标版本</dt><dd><code>{rollbackPreview.data.target_release_id.slice(0, 8)}</code></dd></div><div><dt>当前版本</dt><dd><code>{rollbackPreview.data.current_release_id.slice(0, 8)}</code></dd></div><div><dt>语义变更</dt><dd>{changeCount(rollbackPreview.data.changes)} 项</dd></div></dl>
            {rollbackPreview.data.rollback_blockers.length > 0 && <div className="gateway-release-rollback-blockers">{rollbackPreview.data.rollback_blockers.map((blocker) => <code key={blocker}>{blocker}</code>)}</div>}
          </div>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={rollbackMutation.isPending}>取消</AlertDialogCancel>
          <AlertDialogAction className="gateway-release-rollback-confirm" disabled={!rollbackPreview.data?.rollback_eligible || rollbackPreview.isFetching || rollbackMutation.isPending} onClick={(event) => { event.preventDefault(); if (rollbackId && rollbackPreview.data?.rollback_eligible && !rollbackPreview.isFetching) rollbackMutation.mutate({ id: rollbackId, confirmation: rollbackPreview.data.confirmation_sha256 }) }}>{rollbackMutation.isPending ? <><RefreshCw className="spin" size={13} />正在排队</> : <><RotateCcw size={13} />确认回滚</>}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
}
