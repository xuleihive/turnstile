import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  Box,
  CheckCircle2,
  ChevronRight,
  ChevronsDown,
  ChevronsUp,
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
  Send,
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
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu"
import { useIsMobile } from "../hooks/use-mobile"
import { ApiError, dataSource } from "../data-sources/apim/api"
import { finopsKeys, finopsQueries } from "../data-sources/apim/queries"
import type {
  GatewayReleaseChangeSet,
  GatewayReleaseDetail,
  GatewayReleaseRollbackPreview,
  GatewayReleaseRole,
  GatewayReleaseOperation,
  GatewayReleaseSummary,
  GatewayPublicationFailure,
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

const rollbackBlockerLabels: Record<string, string> = {
  model_release_not_gateway_rollback_target: "单模型发布不能作为网关配置回滚目标",
  gateway_program_mismatch: "目标版本的网关程序与当前安装程序不一致",
  release_is_current: "目标已是当前网关基线",
  release_was_not_promoted: "目标尚未完成发布",
  recorded_dependencies_incomplete: "目标依赖记录不完整",
  live_dependency_check_required: "尚未确认目标依赖一致性",
  release_marked_invalid_for_rollback: "目标已被标记为不可回滚",
}

const roleLabels: Record<GatewayReleaseRole, string> = {
  current: "当前基线",
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
  const statusLabel = releaseIndicatorRoles.has(release.role) ? roleLabels[release.role] : undefined
  return <button type="button" className={`gateway-release-list-row ${selected ? "active" : ""}`} onClick={onSelect}>
    <span className="gateway-release-list-leading" data-role={release.role} role={statusLabel ? "img" : undefined} aria-label={statusLabel} title={statusLabel}><History size={15} aria-hidden="true" /></span>
    <span className="gateway-release-list-copy">
      <span><b>{publicationKindLabels[release.publication_kind]} <span data-no-localize>#{release.generation}</span></b></span>
      <small title={release.display_name}><span data-no-localize>{release.display_name}</span></small>
    </span>
  </button>
}

function RollbackConfirmationHeader({ preview }: { preview: GatewayReleaseRollbackPreview | undefined }) {
  const modelRelease = preview?.rollback_blockers.includes("model_release_not_gateway_rollback_target")
  const programMismatch = preview?.rollback_blockers.includes("gateway_program_mismatch")
  const configurationRelease = Boolean(preview?.dependencies.route_configuration_sha256)
  const title = !preview ? "确认回滚" : modelRelease || programMismatch ? "无法回滚网关配置"
    : configurationRelease ? "确认路由配置回滚" : "确认网关版本回滚"
  const description = !preview ? "正在确认回滚范围与依赖状态。"
    : modelRelease ? rollbackBlockerLabels.model_release_not_gateway_rollback_target
    : programMismatch ? rollbackBlockerLabels.gateway_program_mismatch
    : configurationRelease ? "仅恢复路由配置，不修改网关 Policy 或切换 Revision。依赖检查与生效验证通过后完成回滚。"
    : "控制平面会重新检查依赖、探测候选 Revision、提升并再次探测；失败时自动恢复当前版本。"
  return <AlertDialogHeader>
    <div className="gateway-release-rollback-heading"><span><RotateCcw size={18} /></span><div><AlertDialogTitle>{title}</AlertDialogTitle><AlertDialogDescription>{description}</AlertDialogDescription></div></div>
  </AlertDialogHeader>
}

function ChangeColumn({ title, added, removed, changed, planned }: {
  title: string
  added: string[]
  removed: string[]
  changed: string[]
  planned: boolean
}) {
  const empty = !added.length && !removed.length && !changed.length
  return <div className="gateway-release-change-column">
    <h3>{title}</h3>
    {empty ? <p>没有语义变更</p> : <div className="gateway-release-change-list">
      {added.map((value) => <span className="added" key={`added-${value}`}><b>{planned ? "拟新增" : "已新增"}</b><code title={value} data-no-localize>{value}</code></span>)}
      {removed.map((value) => <span className="removed" key={`removed-${value}`}><b>{planned ? "拟移除" : "已移除"}</b><code title={value} data-no-localize>{value}</code></span>)}
      {changed.map((value) => <span className="changed" key={`changed-${value}`}><b>{planned ? "拟变更" : "已变更"}</b><code title={value} data-no-localize>{value}</code></span>)}
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

function PublicationFailureNotice({ failure, publicationId }: {
  failure: GatewayPublicationFailure
  publicationId: string
}) {
  const messages: Record<GatewayPublicationFailure["code"], string> = {
    publisher_permissions_missing: "Publisher 缺少此 APIM 的模型发布权限。请由部署管理员核对授权后再重试。",
    publisher_permissions_unverified: "无法核验 Publisher 的发布权限，当前发布步骤已停止。",
    publisher_authorization_failed: "Azure 拒绝了 Publisher 的 APIM 管理请求。请核对发布身份的授权。",
    parent_policy_changed: "APIM 父策略与排队时的快照不一致，请核对变化后再重试。",
    credential_source_missing: "APIM 凭据资源尚未创建，请重新提供 API Key 后重试。",
    deployment_not_found: "选定连接中不存在该部署，请使用实际部署名称而不是模型名称。",
    deployment_metadata_unavailable: "无法读取部署目录，请核对连接认证和目录权限。",
    upstream_request_failed: "模型验证请求失败，请核对部署名称、接口路径和 HTTP 状态。",
    publication_failed: "发布未完成，请使用任务 ID 查询诊断记录。",
  }
  return <div className="gateway-publication-failure" role="alert">
    <AlertTriangle size={16} aria-hidden="true" />
    <div>
      <b>发布失败原因</b>
      <p>{messages[failure.code] ?? messages.publication_failed}</p>
      <dl>
        {failure.stage && <div><dt>失败阶段</dt><dd>{auditStatusLabels[failure.stage] ?? failure.stage}</dd></div>}
        {failure.http_status && <div><dt>HTTP</dt><dd data-no-localize>{failure.http_status}</dd></div>}
        <div><dt>任务 ID</dt><dd><code data-no-localize>{publicationId}</code></dd></div>
        {failure.request_id && <div><dt>Azure Request ID</dt><dd><code data-no-localize>{failure.request_id}</code></dd></div>}
      </dl>
      {failure.missing_actions.length > 0 && <details><summary>缺少权限</summary><ul>{failure.missing_actions.map(action => <li key={action}><code data-no-localize>{action}</code></li>)}</ul></details>}
    </div>
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
  if (loading) return <div className="gateway-release-detail-state"><RefreshCw className="spin" size={20} />加载发布详情</div>
  if (!release) return <div className="gateway-release-detail-state"><History size={24} />选择一条发布记录</div>
  const changes = release.diff_from_base.changes
  const plannedChanges = release.status !== "active" && release.status !== "superseded"
    && !release.audit.some(event => event.to_status === "active")
  const failure = release.status === "failed" ? release.failure ?? {
    code: "publication_failed" as const,
    message: "",
    stage: [...release.audit].reverse().find(event => event.to_status === "failed")?.from_status ?? null,
    request_id: null,
    http_status: null,
    missing_actions: [],
  } : null
  const isCurrentRelease = release.role === "current"
  const isModelRelease = release.rollback_blockers.includes("model_release_not_gateway_rollback_target")
  const isConfigurationRelease = Boolean(release.dependencies.route_configuration_sha256)
  const liveStatus = release.dependencies.live_status
  const readinessTitle = isModelRelease ? "模型依赖状态" : isCurrentRelease ? "线上配置状态" : "回滚就绪状态"
  const readinessStatus = isCurrentRelease || isModelRelease
    ? liveStatus === "healthy" ? "一致" : liveStatus === "not_checked" ? "未检查" : "发现漂移"
    : liveStatus === "healthy" ? release.rollback_eligible ? "可回滚" : "受阻"
    : liveStatus === "not_checked" ? "需要检查" : "不可回滚"
  const readinessMessage = isModelRelease
    ? liveStatus === "healthy" ? "模型路由依赖与此发布记录一致。"
    : liveStatus === "not_checked" ? "模型路由依赖尚未检查。"
    : "模型路由依赖与此发布记录不一致。"
    : isCurrentRelease && isConfigurationRelease
    ? liveStatus === "healthy" ? "当前网关程序与 Storage 路由配置符合此发布记录。"
    : liveStatus === "not_checked" ? "当前网关程序与 Storage 路由配置尚未检查。"
    : "当前网关程序或 Storage 路由配置与此发布记录不一致。"
    : isCurrentRelease
    ? liveStatus === "healthy" ? "当前 APIM 配置与此 Release 的记录一致。"
    : liveStatus === "not_checked" ? "运行检查以比较当前 APIM 配置与此 Release 的记录。"
    : "当前 APIM 配置与此 Release 的记录不同。当前流量不一定受影响，但需要协调配置漂移。"
    : liveStatus === "healthy" ? release.rollback_eligible
      ? "当前 Azure 资源与此 Release 的记录一致，可以作为回滚目标。"
      : "当前 Azure 资源与此 Release 的记录一致，但仍有其他条件阻止回滚。"
    : liveStatus === "not_checked" ? "使用此 Release 回滚前，先运行 Azure 实时检查。"
    : "当前 Azure 资源与此 Release 的记录不同，修复差异前不能安全回滚。"
  const issueLabel = isModelRelease ? "模型依赖差异" : isCurrentRelease ? "配置差异" : "回滚阻塞项"
  const renderReleaseActions = (variant: "mobile" | "rail") => canManage ? <div className={`gateway-release-actions gateway-release-actions-${variant}`} role="group" aria-label="发布操作">
    <button type="button" className="gateway-release-action" disabled={busy} onClick={onPin} title={release.pinned ? "取消固定" : "保留此发布记录"}>{release.pinned ? <PinOff size={13} /> : <Pin size={13} />}<span>{release.pinned ? "取消固定" : "固定"}</span></button>
    <button type="button" className="gateway-release-action" disabled={busy || !operationsEnabled} onClick={onCheck} title={operationsEnabled ? "重新检查 Azure 依赖" : "Release Worker 未部署"}><ShieldCheck size={13} /><span>检查依赖</span></button>
    {!isModelRelease && release.role !== "current" && <button type="button" className="gateway-release-action rollback" disabled={busy || !operationsEnabled} onClick={onRollback} title={operationsEnabled ? "预览并请求回滚" : "Release Worker 未部署"}><RotateCcw size={13} /><span>{isConfigurationRelease ? "回滚配置" : "回滚版本"}</span></button>}
  </div> : null
  return <div className="gateway-release-detail-scroll">
    <div className="gateway-release-detail-grid">
      <div className="gateway-release-detail-main">
        <section className="gateway-release-summary-card">
          <header className="gateway-release-detail-head">
            <div className="gateway-release-identity">
              <div className="gateway-release-identity-line"><h2>{publicationKindLabels[release.publication_kind]} <span data-no-localize>#{release.generation}</span></h2><ReleaseRole role={release.role} status={release.status} hideSuperseded={isModelRelease} /></div>
            </div>
          </header>
          <dl className="gateway-release-facts">
            <div><dt>影响对象</dt><dd data-no-localize title={release.display_name}>{release.display_name}</dd></div>
            <div><dt>发布类型</dt><dd>{publicationKindLabels[release.publication_kind]}</dd></div>
            <div><dt>{release.completed_at ? "完成时间" : "创建时间"}</dt><dd>{formatTimestamp(release.completed_at ?? release.created_at, timezone)}</dd></div>
            <div><dt>操作者</dt><dd>{release.created_by}</dd></div>
            <div className="gateway-release-scope"><dt>回滚范围</dt><dd>{isModelRelease ? "单模型发布，非网关回滚快照" : isConfigurationRelease ? "网关路由配置" : "网关版本"}</dd></div>
          </dl>
          {showMobileActions && renderReleaseActions("mobile")}
          {failure && <PublicationFailureNotice failure={failure} publicationId={release.id} />}
          {release.status === "queued" && <div className="gateway-release-operation queued" role="status">
            <CircleDashed size={15} />
            <div><b>已排队，尚未开始</b><span>正在等待 Publication Worker 处理。</span></div>
          </div>}
          {operation && <div className={`gateway-release-operation ${!operation.worker_available && !["succeeded", "failed", "restored"].includes(operation.status) ? "unavailable" : operation.status}`} role="status" aria-live="polite">
            {!operation.worker_available && !["succeeded", "failed", "restored"].includes(operation.status) ? <AlertTriangle size={15} /> : operation.status === "failed" || operation.status === "restored" ? <AlertTriangle size={15} /> : operation.status === "succeeded" ? <CheckCircle2 size={15} /> : <RefreshCw className="spin" size={15} />}
            <div>{!operation.worker_available && !["succeeded", "failed", "restored"].includes(operation.status) ? <><b>操作未启动</b><span>Release Worker 未部署，操作未被处理。</span></> : <><b>{auditStatusLabels[operation.status] ?? operation.status}</b>{operation.error_message ? <span>{operation.error_message}</span> : <span>操作 <code data-no-localize>{operation.id.slice(0, 8)}</code> 正在由控制平面处理</span>}</>}</div>
          </div>}
        </section>

        <section className="gateway-release-card gateway-release-changes-card" data-state={plannedChanges ? "planned" : "applied"}>
          <header className="gateway-release-card-head"><div><h2>{plannedChanges ? "计划变更" : "相对基础版本的变更"}</h2>{plannedChanges && <span className="gateway-release-change-state">未确认生效</span>}</div><span data-no-localize>{changeCount(changes)}</span></header>
          <div className="gateway-release-change-grid">
            <ChangeColumn title="模型" added={changes.added_models} removed={changes.removed_models} changed={changes.changed_models} planned={plannedChanges} />
            <ChangeColumn title="负载均衡" added={changes.added_backend_pools} removed={changes.removed_backend_pools} changed={changes.changed_backend_pools} planned={plannedChanges} />
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
                <div><dt>APIM REVISION</dt><dd><code title={release.apim_revision ?? undefined} data-no-localize>{release.apim_revision ?? "—"}</code></dd></div>
                <div><dt>BASE POLICY SHA</dt><dd><code title={release.dependencies.parent_policy_sha256 ?? undefined}>{shortHash(release.dependencies.parent_policy_sha256)}</code></dd></div>
                <div><dt>COMPILED POLICY SHA</dt><dd><code title={release.dependencies.compiled_policy_sha256 ?? undefined}>{shortHash(release.dependencies.compiled_policy_sha256)}</code></dd></div>
                <div><dt>DESIRED SPEC SHA</dt><dd><code title={release.desired_spec_sha256}>{shortHash(release.desired_spec_sha256)}</code></dd></div>
                {release.dependencies.route_configuration_sha256 && <div><dt>ROUTE CONFIG SHA</dt><dd><code title={release.dependencies.route_configuration_sha256}>{shortHash(release.dependencies.route_configuration_sha256)}</code></dd></div>}
                {release.dependencies.route_head_sha256 && <div><dt>ROUTE HEAD SHA</dt><dd><code title={release.dependencies.route_head_sha256}>{shortHash(release.dependencies.route_head_sha256)}</code></dd></div>}
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
  const [collapsedGatewayIds, setCollapsedGatewayIds] = useState(() => new Set<string>())
  const [rollbackId, setRollbackId] = useState<string | null>(null)
  const [activeOperationId, setActiveOperationId] = useState<string | null>(null)
  const [readinessOpen, setReadinessOpen] = useState(false)
  const [readinessGateway, setReadinessGateway] = useState("")
  const [executionOpen, setExecutionOpen] = useState(false)
  const [executionApplication, setExecutionApplication] = useState("")
  const [executionModel, setExecutionModel] = useState("")
  const readinessMutation = useMutation({
    mutationFn: (gatewayId: string) => dataSource.checkBudgetDelegation(gatewayId),
    retry: false,
  })
  const verification = readinessMutation.data?.ready
    && readinessMutation.data.gateway_profile_id === readinessGateway
    && readinessMutation.data.protocol_mode === "paused"
    ? readinessMutation.data.verification : null
  const readinessRegistry = useQuery({
    ...finopsQueries.registry(),
    enabled: (readinessOpen || executionOpen) && user?.role === "owner",
  })
  const readinessOptions = (readinessRegistry.data?.gateways ?? [])
    .filter(gateway => gateway.enabled && gateway.implementation === "apim")
    .map(gateway => ({value: gateway.id, label: gateway.name}))
  const executionApplications = useQuery({
    ...finopsQueries.gatewayApplications(),
    enabled: executionOpen && user?.role === "owner",
  })
  const applicationOptions = (executionApplications.data?.items ?? [])
    .filter(application => application.id === verification?.application_id
      && application.gateway_profile_id === readinessGateway && application.status === "active" && !application.system_managed)
    .map(application => ({value: application.id, label: application.display_name}))
  const selectedApplication = executionApplications.data?.items.find(application => application.id === executionApplication)
  const modelOptions = (readinessRegistry.data?.models ?? [])
    .filter(model => verification?.model_ids.includes(model.id)
      && model.enabled && !model.capabilities?.includes("image_generation")
      && readinessRegistry.data?.runtimes.some(runtime => runtime.id === model.runtime_id && runtime.enabled && runtime.gateway_profile_id === readinessGateway)
      && selectedApplication && (!selectedApplication.model_policy_configured || selectedApplication.allowed_model_ids.includes(model.id)))
    .map(model => ({value: model.id, label: model.display_name}))
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
  const selectedGatewayId = items.find(release => release.id === selectedId)?.gateway_profile_id
  const visible = useMemo(() => {
    const normalized = search.trim().toLocaleLowerCase()
    return items.filter((release) => (filter === "all" || release.role === filter)
      && (!normalized || `${release.apim_revision ?? ""} ${release.display_name} ${release.model_key} ${release.created_by}`.toLocaleLowerCase().includes(normalized)))
  }, [filter, items, search])
  const releaseGroups = useMemo(() => {
    const groups = new Map<string, { gatewayId: string; gatewayName: string; releases: GatewayReleaseSummary[] }>()
    visible.forEach((release) => {
      const group = groups.get(release.gateway_profile_id)
      if (group) group.releases.push(release)
      else groups.set(release.gateway_profile_id, {
        gatewayId: release.gateway_profile_id,
        gatewayName: release.gateway_name,
        releases: [release],
      })
    })
    return [...groups.values()]
  }, [visible])
  const toggleGatewayGroup = (gatewayId: string) => {
    setCollapsedGatewayIds((current) => {
      const next = new Set(current)
      if (next.has(gatewayId)) next.delete(gatewayId)
      else next.add(gatewayId)
      return next
    })
  }
  const expandAllGatewayGroups = () => setCollapsedGatewayIds(new Set())
  const collapseAllGatewayGroups = () => setCollapsedGatewayIds(new Set(items.map((release) => release.gateway_profile_id)))
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
  const executionMutation = useMutation({
    mutationFn: ({ gatewayId, applicationId, modelId }: { gatewayId: string; applicationId: string; modelId: string }) =>
      dataSource.verifyBudgetExecution(gatewayId, applicationId, modelId),
    retry: false,
  })
  const canVerifyExecution = !executionMutation.isPending
    && applicationOptions.some(option => option.value === executionApplication)
    && modelOptions.some(option => option.value === executionModel)
  const verifyExecution = () => {
    if (!canVerifyExecution) return
    executionMutation.mutate({ gatewayId: readinessGateway, applicationId: executionApplication, modelId: executionModel })
  }
  const checkReadiness = () => {
    if (!selectedGatewayId) return
    setReadinessGateway(selectedGatewayId)
    setReadinessOpen(true)
    readinessMutation.reset()
  }
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
      <label className="smh-search"><Search size={14} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索 Revision、模型或操作者..." aria-label="搜索发布记录" /></label>
      <div className="gateway-release-filter-row">
        <div className="gateway-release-filters" role="group" aria-label="发布状态筛选">
          {primaryFilters.filter((value) => value === "all" || items.some((release) => release.role === value)).map((value) => <button type="button" key={value} className={filter === value ? "active" : ""} aria-pressed={filter === value} onClick={() => setFilter(value)}>{value !== "all" && <ReleaseStatusDot role={value} />}<span>{value === "all" ? "全部" : roleLabels[value]}</span><small>{value === "all" ? items.length : items.filter((release) => release.role === value).length}</small></button>)}
          <DropdownMenu>
            <DropdownMenuTrigger render={<button type="button" className={filter !== "all" && filter !== "current" ? "gateway-release-filter-more active" : "gateway-release-filter-more"} aria-label="更多状态" title="更多状态" />}><ListFilter size={13} /></DropdownMenuTrigger>
            <DropdownMenuContent portalContainer={closeOnSelect ? releaseDrawerPortalRef : undefined} align="start">
              {secondaryFilters.filter((value) => items.some((release) => release.role === value)).map((value) => <DropdownMenuCheckboxItem key={value} className="gateway-release-filter-item" checked={filter === value} closeOnClick onCheckedChange={(checked) => setFilter(checked ? value : "all")}><ReleaseStatusDot role={value} reserve /><span>{roleLabels[value]}</span><small className="gateway-release-filter-count">{items.filter((release) => release.role === value).length}</small></DropdownMenuCheckboxItem>)}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <div className="gateway-release-list-actions" role="group" aria-label="网关分组操作">
          <button type="button" className="gateway-release-filter-action" aria-label="全部展开" onClick={expandAllGatewayGroups}><ChevronsDown size={13} /></button>
          <button type="button" className="gateway-release-filter-action" aria-label="全部折叠" onClick={collapseAllGatewayGroups}><ChevronsUp size={13} /></button>
        </div>
      </div>
    </div>
    <div className="gateway-release-list" role="list" aria-label="发布列表">
      {releaseGroups.map((group) => {
        const collapsed = collapsedGatewayIds.has(group.gatewayId)
        const contentId = `gateway-release-group-${closeOnSelect ? "drawer" : "rail"}-${group.gatewayId}`
        return <section className="gateway-release-group" aria-label={group.gatewayName} key={group.gatewayId}>
          <button type="button" className="gateway-release-group-trigger" aria-expanded={!collapsed} aria-controls={contentId} onClick={() => toggleGatewayGroup(group.gatewayId)}>
            <ChevronRight className="gateway-release-group-caret" size={13} aria-hidden="true" />
            <span title={group.gatewayName} data-no-localize>{group.gatewayName}</span>
            <i aria-hidden="true" />
            <small>{group.releases.length}</small>
          </button>
          <div id={contentId} className="gateway-release-group-content" role="group" hidden={collapsed}>
            {group.releases.map((release) => <div role="listitem" key={release.id}><ReleaseListRow release={release} selected={release.id === selectedId} onSelect={() => chooseRelease(release.id, closeOnSelect)} /></div>)}
          </div>
        </section>
      })}
      {!visible.length && <div className="gateway-release-list-empty">没有匹配的发布记录</div>}
    </div>
    <footer>{visible.length} <span>条发布记录</span></footer>
  </>

  return <div className="smh-workspace gateway-releases-workspace">
    <header className="smh-page-header gateway-releases-header">
      <div><span className="smh-header-icon"><History size={17} /></span><h1>发布管理</h1><span>{releases.isLoading ? "—" : items.length}</span></div>
      <div className="smh-header-actions">
        {user?.role === "owner" && selectedGatewayId && <Button variant="outline" disabled={readinessMutation.isPending} onClick={checkReadiness}><ShieldCheck size={13} />验证预算委托</Button>}
        {releases.data && !operationsEnabled && <span className="gateway-release-worker-state" title="检查依赖、回滚和清理计划暂不可用。"><AlertTriangle size={13} />Release Worker 未部署</span>}
        {user?.role === "owner" && detail.data ? <Button variant="outline" disabled={busy || !operationsEnabled} onClick={() => gcMutation.mutate(detail.data!.gateway_profile_id)} title={operationsEnabled ? "扫描整个 Gateway 的闲置 APIM 资源" : "Release Worker 未部署"}><ScanSearch size={13} />扫描闲置资源</Button> : <span className="gateway-releases-readonly"><ShieldAlert size={13} />只读</span>}
      </div>
    </header>
    {isMobile && user?.role === "owner" && selectedGatewayId && <div className="gateway-release-mobile-maintenance">
      <Button variant="outline" disabled={readinessMutation.isPending} onClick={checkReadiness}><ShieldCheck size={13} />验证预算委托</Button>
      {detail.data && <Button variant="outline" disabled={busy || !operationsEnabled} onClick={() => gcMutation.mutate(detail.data!.gateway_profile_id)} title={operationsEnabled ? "扫描整个 Gateway 的闲置 APIM 资源" : "Release Worker 未部署"}><ScanSearch size={13} />扫描闲置资源</Button>}
    </div>}
    {gcMutation.error && <div className="gateway-release-gateway-error" role="alert"><AlertTriangle size={14} /><span>{String(gcMutation.error)}</span></div>}
    {gatewayOperation && <GatewayCleanupStatus operation={gatewayOperation} />}
    {releases.isLoading ? <div className="registry-loading"><RefreshCw className="spin" />加载发布记录</div>
      : releases.error ? <div className="registry-error">{apiMissing ? "当前环境尚未部署发布管理 API。" : <><span>无法加载发布记录：</span>{String(releases.error)}</>}</div>
      : !items.length ? <div className="smh-empty"><History size={28} /><b>还没有发布记录</b><span>模型或后端池首次发布后会显示在这里。</span></div>
      : <div className="gateway-release-layout" style={{ "--gateway-release-pane-width": `${width}px` } as React.CSSProperties}>
        {!isMobile && <aside className="gateway-release-list-pane">{releaseListContent(false)}</aside>}
        <button className="runtime-pane-handle" type="button" role="separator" aria-label="调整发布列表宽度" aria-orientation="vertical" aria-valuemin={minWidth} aria-valuemax={maxWidth} aria-valuenow={width} onPointerDown={startResize} onKeyDown={resizeWithKeyboard} onDoubleClick={resetWidth} />
        <main className="gateway-release-detail">
          {releaseActionError && <div className="gateway-release-action-error" role="alert"><AlertTriangle size={14} /><span>{String(releaseActionError)}</span></div>}
          <ReleaseDetail release={detail.data} loading={detail.isLoading} canManage={user?.role === "owner"} operationsEnabled={operationsEnabled} busy={busy} operation={releaseOperation} onPin={() => detail.data && protectionMutation.mutate(detail.data)} onCheck={() => detail.data && integrityMutation.mutate(detail.data.id)} onRollback={() => detail.data && setRollbackId(detail.data.id)} showMobileActions={isMobile} />
        </main>
      </div>}
    {isMobile && releases.data && items.length > 0 && <Dialog open={releaseDrawerOpen} onOpenChange={onReleaseDrawerOpenChange}>
      <DialogContent className="apim-native-route-drawer gateway-release-drawer" finalFocus={false}>
        <DialogHeader className="apim-native-route-drawer-header"><DialogTitle>发布管理</DialogTitle><DialogDescription className="sr-only">选择一条发布记录</DialogDescription><DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="apim-native-route-drawer-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose></DialogHeader>
        <aside className="gateway-release-list-pane gateway-release-drawer-list">{releaseListContent(true)}</aside>
        <div ref={releaseDrawerPortalRef} className="gateway-release-drawer-portals" />
      </DialogContent>
    </Dialog>}
    <Dialog open={readinessOpen} onOpenChange={setReadinessOpen}>
      <DialogContent className="gateway-budget-dialog">
        <DialogHeader>
          <DialogTitle>预算委托验证</DialogTitle>
          <DialogDescription className="sr-only">候选身份验证结果</DialogDescription>
        </DialogHeader>
        <label className="registry-field gateway-budget-target">
          <span className="registry-field-label">Gateway</span>
          <Select value={readinessOptions.some(option => option.value === readinessGateway) ? readinessGateway : null} items={readinessOptions} disabled={readinessMutation.isPending || !readinessOptions.length} onValueChange={value => {
            if (value) {
              setReadinessGateway(value)
              readinessMutation.reset()
            }
          }}>
            <SelectTrigger aria-label="Gateway" title={readinessOptions.find(option => option.value === readinessGateway)?.label}><SelectValue placeholder="选择 Gateway" /></SelectTrigger>
            <SelectContent align="start" alignItemWithTrigger={false}>{readinessOptions.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
          </Select>
        </label>
        {readinessRegistry.isLoading && <div className="gateway-budget-state" role="status"><RefreshCw className="spin" size={15} /><span>正在加载调用配置</span></div>}
        {readinessRegistry.error && <div className="gateway-budget-state error" role="alert"><AlertTriangle size={15} /><span>{String(readinessRegistry.error)}</span></div>}
        {readinessMutation.isPending ? <div className="gateway-budget-state" role="status"><RefreshCw className="spin" size={15} /><span>正在验证身份</span></div>
          : readinessMutation.error ? <div className="gateway-budget-state error" role="alert"><AlertTriangle size={15} /><span>{String(readinessMutation.error)}</span></div>
          : readinessMutation.data && <div className="gateway-budget-preview">
            <div className={`gateway-budget-state ${readinessMutation.data.ready ? "success" : "error"}`} role="status">{readinessMutation.data.ready ? <><CheckCircle2 size={15} /><span>{readinessMutation.data.protocol_mode === "paused" ? "围栏验证通过" : "身份验证通过"}</span></> : <><XCircle size={15} /><span>{readinessMutation.data.protocol_mode === "paused" ? "围栏验证未通过" : "身份验证未通过"}</span></>}</div>
            <p>HTTP 实际 / 预期</p>
            <dl>
              <div><dt>Revision</dt><dd><code>{readinessMutation.data.revision}</code></dd></div>
              <div><dt>Gateway</dt><dd><code>{readinessMutation.data.native_gateway_id}</code></dd></div>
              {readinessMutation.data.checks.map(check => <div key={`${check.native_gateway_id ?? ""}:${check.case}`}>
                <dt>{({valid_identity: "有效身份", wrong_audience: "错误受众", missing_person: "缺少人员身份", current_fenced: "当前路径围栏", candidate_fenced: "候选路径围栏"})[check.case]}</dt>
                <dd>{check.actual_status ?? "—"} / {check.expected_status}{check.expected_error_code && <><br /><code>{check.native_gateway_id}</code><br /><code>{check.actual_error_code ?? "—"}</code></>}</dd>
              </div>)}
              <div><dt>UTC</dt><dd>{new Date(readinessMutation.data.checked_at).toISOString()}</dd></div>
            </dl>
          </div>}
        <DialogFooter>
          <Button variant="outline" disabled={readinessMutation.isPending || !readinessOptions.some(option => option.value === readinessGateway)} onClick={() => readinessMutation.mutate(readinessGateway)}><ShieldCheck size={13} />验证预算委托</Button>
          <Button variant="outline" disabled={readinessMutation.isPending || !verification?.model_ids.length} title={!verification?.model_ids.length ? "仅限已验证的隔离候选" : undefined} onClick={() => {
            setReadinessOpen(false)
            setExecutionApplication("")
            setExecutionModel("")
            executionMutation.reset()
            setExecutionOpen(true)
          }}><Layers3 size={13} />联合账本验证</Button>
          <DialogClose render={<Button variant="outline" />}>关闭</DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={executionOpen} onOpenChange={open => { if (!executionMutation.isPending) setExecutionOpen(open) }}>
      <DialogContent className="gateway-budget-dialog">
        <DialogHeader><DialogTitle>联合账本验证</DialogTitle><DialogDescription className="sr-only">候选双作用域账本</DialogDescription></DialogHeader>
        <div className="gateway-budget-fields">
          <span className="gateway-budget-gateway">{readinessOptions.find(option => option.value === readinessGateway)?.label}</span>
          <label className="registry-field"><span className="registry-field-label">Application</span>
            <Select value={executionApplication || null} items={applicationOptions} disabled={executionMutation.isPending || executionApplications.isLoading || !applicationOptions.length} onValueChange={value => {
              setExecutionApplication(value ?? "")
              setExecutionModel("")
              executionMutation.reset()
            }}>
              <SelectTrigger aria-label="Application" title={selectedApplication?.display_name}><SelectValue placeholder={executionApplications.isLoading ? "正在加载应用" : applicationOptions.length ? "选择应用" : "暂无可用应用"} /></SelectTrigger>
              <SelectContent align="start" alignItemWithTrigger={false}>{applicationOptions.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
            </Select>
          </label>
          <label className="registry-field"><span className="registry-field-label">模型</span>
            <Select value={executionModel || null} items={modelOptions} disabled={executionMutation.isPending || !executionApplication || !modelOptions.length} onValueChange={value => {
              setExecutionModel(value ?? "")
              executionMutation.reset()
            }}>
              <SelectTrigger aria-label="模型" title={modelOptions.find(option => option.value === executionModel)?.label}><SelectValue placeholder={!executionApplication || modelOptions.length ? "选择模型" : "暂无可用模型"} /></SelectTrigger>
              <SelectContent align="start" alignItemWithTrigger={false}>{modelOptions.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
            </Select>
          </label>
          <div className="gateway-budget-limit"><span>最大输出 Tokens</span><strong>32</strong></div>
          {(executionApplications.error || readinessRegistry.error || executionMutation.error) && <div role="alert" className="gateway-budget-state error"><AlertTriangle size={15} /><span>{String(executionApplications.error || readinessRegistry.error || executionMutation.error)}</span></div>}
          {executionMutation.isPending && <div role="status" className="gateway-budget-state"><RefreshCw size={15} className="spin" /><span>正在验证账本</span></div>}
          {executionMutation.data && <div className="gateway-budget-results">
            <div role="status">HTTP {executionMutation.data.actual_status} · {executionMutation.data.settled ? "结算完成" : "尚未确认结算"}</div>
            <code>{executionMutation.data.request_id}</code>
            {executionMutation.data.scopes.map(scope => <section key={scope.before.scope.scope_type} className="gateway-budget-scope">
              <strong>{scope.before.scope.scope_type === "person" ? "Person" : "Application"}</strong>
              <code>{scope.before.scope.scope_id}</code>
              <dl>
                <div><dt>调用前已确认</dt><dd>{scope.before.confirmed_tokens}</dd></div>
                <div><dt>调用后已确认</dt><dd>{scope.after?.confirmed_tokens ?? "—"}</dd></div>
                <div><dt>待结算 Tokens</dt><dd>{scope.after?.active_reserved_tokens ?? "—"}</dd></div>
                <div><dt>实际 Tokens</dt><dd>{scope.attempt?.actual_tokens ?? "—"}</dd></div>
                <div><dt>状态</dt><dd>{scope.attempt?.state ?? "—"}</dd></div>
                <div><dt>账本版本</dt><dd>{scope.after?.version ?? "—"}</dd></div>
              </dl>
            </section>)}
          </div>}
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={!canVerifyExecution} onClick={verifyExecution}><Send size={13} />发送验证请求</Button>
          <DialogClose render={<Button variant="outline" disabled={executionMutation.isPending} />}>关闭</DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <AlertDialog open={Boolean(rollbackId)} onOpenChange={(open) => { if (!open && !rollbackMutation.isPending) setRollbackId(null) }}>
      <AlertDialogContent className="gateway-release-rollback-dialog">
        <RollbackConfirmationHeader preview={rollbackPreview.data} />
        {rollbackPreview.isLoading ? <div className="gateway-release-rollback-state"><RefreshCw className="spin" size={15} />生成语义预览</div>
          : rollbackPreview.error ? <div className="gateway-release-rollback-state error"><AlertTriangle size={15} />{String(rollbackPreview.error)}</div>
          : rollbackPreview.data && <div className="gateway-release-rollback-preview">
            <dl><div><dt>目标版本</dt><dd><code>{rollbackPreview.data.target_release_id.slice(0, 8)}</code></dd></div><div><dt>当前版本</dt><dd><code>{rollbackPreview.data.current_release_id.slice(0, 8)}</code></dd></div><div><dt>语义变更</dt><dd data-no-localize>{changeCount(rollbackPreview.data.changes).toLocaleString(getIntlLocale())}</dd></div></dl>
            {rollbackPreview.data.rollback_blockers.length > 0 && <div className="gateway-release-rollback-blockers">{rollbackPreview.data.rollback_blockers.map((blocker) => rollbackBlockerLabels[blocker] ? <span key={blocker}>{rollbackBlockerLabels[blocker]}</span> : <code key={blocker}>{blocker}</code>)}</div>}
          </div>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={rollbackMutation.isPending}>取消</AlertDialogCancel>
          <AlertDialogAction variant="destructive" className="gateway-release-rollback-confirm" disabled={!rollbackPreview.data?.rollback_eligible || rollbackPreview.isFetching || rollbackMutation.isPending} onClick={(event) => { event.preventDefault(); if (rollbackId && rollbackPreview.data?.rollback_eligible && !rollbackPreview.isFetching) rollbackMutation.mutate({ id: rollbackId, confirmation: rollbackPreview.data.confirmation_sha256 }) }}>{rollbackMutation.isPending ? <><RefreshCw className="spin" size={13} />正在排队</> : <><RotateCcw size={13} />确认回滚</>}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
}
