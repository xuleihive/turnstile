import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react"
import { createPortal } from "react-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Activity,
  AlertTriangle,
  AppWindow,
  Bot,
  Camera,
  CheckCircle2,
  ChevronRight,
  Edit3,
  Gauge,
  KeyRound,
  Layers3,
  Plus,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  UserRound,
  WalletCards,
  X,
} from "lucide-react"

import { Button } from "../components/ui/button"
import { Checkbox } from "../components/ui/checkbox"
import { ExpandableSearch } from "../components/ui/expandable-search"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../components/ui/dialog"
import { Input } from "../components/ui/input"
import { Progress } from "../components/ui/progress"
import { ResizableGridTable } from "../components/ui/resizable-table"
import { Textarea } from "../components/ui/textarea"
import { useResizablePane } from "../components/ui/use-resizable-pane"
import { ApplicationUsageActivityCard } from "../components/finops/application-usage-activity-card"
import { ApplicationSubscriptionCard } from "../components/finops/application-subscription-card"
import { ApplicationCreateDialog } from "../components/applications/application-create-dialog"
import { APPLICATION_OPERATION_PARAMETER, applicationOperationIdFromUrl, applicationOperationTerminal, applicationProvisionStage } from "../components/applications/application-create-form"
import { ApiError, dataSource } from "../data-sources/apim/api"
import { finopsKeys, finopsQueries } from "../data-sources/apim/queries"
import type {
  GatewayApplicationAuditEvent,
  GatewayApplicationDetail,
  GatewayApplicationStatus,
  GatewayApplicationSummary,
  GatewayApplicationType,
  GatewayReleaseOperation,
  GatewayReleaseOperationAccepted,
  ManagedModel,
} from "../data-sources/apim/types"
import { FINOPS_NAVIGATE_EVENT } from "../lib/navigation"
import { getIntlLocale } from "../locales"
import { useAuth } from "../providers/auth-provider"
import { useTimezone } from "../providers/timezone-provider"

type ApplicationFilter = "all" | "active" | "attention" | "system"
type SubscriptionCategory = "applications" | "agents"

const APPLICATION_TABLE_COLUMN_MIN_WIDTHS = [180, 130, 120, 160, 170, 80] as const

const OWNER_SOURCE_LABELS: Record<string, string> = {
  apim: "来自 APIM",
  derived: "按名称推导",
  manual: "人工指定",
}

const statusLabels: Record<GatewayApplicationStatus, string> = {
  active: "活动",
  suspended: "已暂停",
  retired: "已停用",
}

const typeLabels: Record<GatewayApplicationType, string> = {
  service: "服务",
  agent: "智能体",
  delegated_user: "委托用户",
  system: "系统",
}

const auditLabels: Record<GatewayApplicationAuditEvent["operation"], string> = {
  created: "创建应用",
  adopted: "接管订阅",
  updated: "更新应用",
  budget_updated: "更新额度",
  models_updated: "更新模型访问",
  subscription_synced: "同步订阅",
  suspended: "暂停应用",
  resumed: "恢复应用",
  retired: "停用应用",
}

const applicationAvatarTypes = new Set(["image/png", "image/jpeg", "image/webp"])
const applicationAvatarMaxBytes = 64 * 1024
const applicationAvatarMaxSourceBytes = 10 * 1024 * 1024

function dataUrlByteLength(value: string) {
  const encoded = value.slice(value.indexOf(",") + 1)
  const padding = encoded.endsWith("==") ? 2 : encoded.endsWith("=") ? 1 : 0
  return Math.floor(encoded.length * 3 / 4) - padding
}

function loadAvatarImage(file: File) {
  return new Promise<HTMLImageElement>((resolve, reject) => {
    const objectUrl = URL.createObjectURL(file)
    const image = new Image()
    image.onload = () => {
      URL.revokeObjectURL(objectUrl)
      resolve(image)
    }
    image.onerror = () => {
      URL.revokeObjectURL(objectUrl)
      reject(new Error("无法读取这张图片。"))
    }
    image.src = objectUrl
  })
}

async function prepareApplicationAvatar(file: File) {
  if (!applicationAvatarTypes.has(file.type)) throw new Error("请选择 PNG、JPEG 或 WebP 图片。")
  if (file.size > applicationAvatarMaxSourceBytes) throw new Error("原始图片不能超过 10 MB。")
  const image = await loadAvatarImage(file)
  const sourceSize = Math.min(image.naturalWidth, image.naturalHeight)
  if (!sourceSize) throw new Error("图片尺寸无效。")
  const sourceX = (image.naturalWidth - sourceSize) / 2
  const sourceY = (image.naturalHeight - sourceSize) / 2
  const webpProbe = document.createElement("canvas").toDataURL("image/webp")
  const outputType = webpProbe.startsWith("data:image/webp") ? "image/webp" : "image/jpeg"
  for (const outputSize of [192, 160, 128]) {
    const canvas = document.createElement("canvas")
    canvas.width = outputSize
    canvas.height = outputSize
    const context = canvas.getContext("2d")
    if (!context) throw new Error("浏览器无法处理这张图片。")
    context.imageSmoothingEnabled = true
    context.imageSmoothingQuality = "high"
    if (outputType === "image/jpeg") {
      context.fillStyle = "#ffffff"
      context.fillRect(0, 0, outputSize, outputSize)
    }
    context.drawImage(image, sourceX, sourceY, sourceSize, sourceSize, 0, 0, outputSize, outputSize)
    for (const quality of [0.82, 0.68, 0.54, 0.4]) {
      const dataUrl = canvas.toDataURL(outputType, quality)
      if (dataUrlByteLength(dataUrl) <= applicationAvatarMaxBytes) return dataUrl
    }
  }
  throw new Error("压缩后的头像仍然过大，请选择更简单的图片。")
}

function applicationFromUrl() {
  return new URL(window.location.href).searchParams.get("application")
}

function subscriptionCategoryFromUrl(): SubscriptionCategory {
  return new URL(window.location.href).searchParams.get("consumer") === "agents" ? "agents" : "applications"
}

// Arriving from the organization screen, where the channel count on a department is a link.
// Kept in the URL rather than in component state so the link is shareable and survives a
// reload -- the operator who sent it is usually asking a colleague to look at the same list.
function departmentFromUrl(): string | null {
  return new URL(window.location.href).searchParams.get("department")
}

function applicationRouteHref(applicationId: string | null, category = subscriptionCategoryFromUrl()) {
  const url = new URL(window.location.href)
  url.searchParams.set("page", "applications")
  url.searchParams.set("consumer", category)
  if (applicationId) url.searchParams.set("application", applicationId)
  else url.searchParams.delete("application")
  return `${url.pathname}${url.search}`
}

function openApplicationRoute(applicationId: string | null, category = subscriptionCategoryFromUrl()) {
  return (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    if (event.button !== 0) return
    event.preventDefault()
    window.history.pushState(null, "", applicationRouteHref(applicationId, category))
    window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT))
  }
}

function formatCount(value: number) {
  return new Intl.NumberFormat(getIntlLocale(), { notation: "compact", maximumFractionDigits: 1 }).format(value)
}

function formatFullCount(value: number) {
  return new Intl.NumberFormat(getIntlLocale()).format(value)
}

function formatCost(value: number) {
  return new Intl.NumberFormat(getIntlLocale(), {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: value > 0 && value < 1 ? 4 : 2,
    maximumFractionDigits: value > 0 && value < 1 ? 4 : 2,
  }).format(value)
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

function ApplicationStatus({ application }: { application: GatewayApplicationSummary }) {
  const state = application.stale_subscription_count > 0 ? "stale" : application.status
  const label = state === "stale" ? "范围失联" : statusLabels[application.status]
  const health = state === "active" ? "available" : state === "retired" ? "unavailable" : state
  return <span className={`registry-health application-health ${health}`} title={label}><i />{label}</span>
}

function ApplicationAvatar({
  application,
  size = "inventory",
  showStatus = false,
}: {
  application: GatewayApplicationSummary
  size?: "inventory" | "detail"
  showStatus?: boolean
}) {
  const [imageFailed, setImageFailed] = useState(false)
  useEffect(() => setImageFailed(false), [application.avatar_url])
  const ConsumerIcon = application.application_type === "agent" ? Bot : AppWindow
  const state = application.stale_subscription_count > 0 ? "stale" : application.status
  return <span className={`application-avatar ${size}`} aria-hidden="true">
    {application.avatar_url && !imageFailed
      ? <img src={application.avatar_url} alt="" onError={() => setImageFailed(true)} />
      : <ConsumerIcon size={size === "detail" ? 19 : 15} />}
    {showStatus && <i className={state} />}
  </span>
}

function ApplicationAvatarEditor({ application, canManage, writeAvailable }: {
  application: GatewayApplicationDetail
  canManage: boolean
  writeAvailable: boolean
}) {
  const queryClient = useQueryClient()
  const inputRef = useRef<HTMLInputElement>(null)
  const [processing, setProcessing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mutation = useMutation({
    mutationFn: (avatarDataUrl: string | null) => dataSource.updateGatewayApplicationAvatar(
      application.id,
      { avatar_data_url: avatarDataUrl },
    ),
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayApplication(application.id) })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayApplications })
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : "无法更新头像。"),
  })
  const busy = processing || mutation.isPending
  const chooseAvatar = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ""
    if (!file) return
    setError(null)
    setProcessing(true)
    try {
      mutation.mutate(await prepareApplicationAvatar(file))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法处理头像。")
    } finally {
      setProcessing(false)
    }
  }
  if (!canManage) return <ApplicationAvatar application={application} size="detail" />
  if (!writeAvailable) return <div className="application-avatar-editor">
    <button type="button" className="application-avatar-trigger unavailable" disabled aria-label="更换头像" title="后端尚未发布编辑能力">
      <ApplicationAvatar application={application} size="detail" />
      <span className="application-avatar-overlay"><Camera size={15} /></span>
    </button>
  </div>
  return <div className="application-avatar-editor">
    <button type="button" className="application-avatar-trigger" disabled={busy} onClick={() => inputRef.current?.click()} aria-label="更换头像" title="更换头像">
      <ApplicationAvatar application={application} size="detail" />
      <span className="application-avatar-overlay">{busy ? <RefreshCw className="spin" size={15} /> : <Camera size={15} />}</span>
    </button>
    {application.avatar_url && <button type="button" className="application-avatar-remove" disabled={busy} onClick={() => mutation.mutate(null)} aria-label="移除头像" title="移除头像"><Trash2 size={11} /></button>}
    {error && <span className="application-avatar-error" role="alert" title={error}><AlertTriangle size={11} /></span>}
    <input ref={inputRef} type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => void chooseAvatar(event)} tabIndex={-1} aria-hidden="true" />
  </div>
}

function ApplicationBudgetDialog({ application, open, writeAvailable, onOpenChange }: {
  application: GatewayApplicationDetail
  open: boolean
  writeAvailable: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const budget = application.budget
  const [tokenLimit, setTokenLimit] = useState(String(budget?.token_limit ?? 100_000))
  const [tokensPerMinute, setTokensPerMinute] = useState(String(budget?.tokens_per_minute ?? 100_000))
  const [warningThreshold, setWarningThreshold] = useState(String(budget?.warning_threshold_percent ?? 80))
  const [enforce, setEnforce] = useState(budget?.enforce ?? true)
  const mutation = useMutation({
    mutationFn: () => dataSource.updateGatewayApplicationBudget(application.id, {
      token_limit: Number(tokenLimit),
      tokens_per_minute: Number(tokensPerMinute),
      enforce,
      warning_threshold_percent: Number(warningThreshold),
    }),
    onSuccess: (value) => {
      queryClient.setQueryData(finopsKeys.gatewayApplication(application.id), value)
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayApplications })
      onOpenChange(false)
    },
  })
  const valid = Number(tokenLimit) > 0
    && Number.isInteger(Number(tokenLimit))
    && Number(tokensPerMinute) > 0
    && Number.isInteger(Number(tokensPerMinute))
    && Number(warningThreshold) >= 1
    && Number(warningThreshold) <= 100
    && Number.isInteger(Number(warningThreshold))
  return <Dialog open={open} onOpenChange={(next) => { if (!mutation.isPending) onOpenChange(next) }}>
    <DialogContent className="registry-editor-dialog application-governance-dialog application-budget-dialog" finalFocus={false}>
      <form className="registry-editor" onSubmit={(event) => { event.preventDefault(); if (valid) mutation.mutate() }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>编辑应用额度</DialogTitle><DialogDescription>{application.display_name}</DialogDescription></DialogHeader>
        <button type="button" className="registry-editor-close" onClick={() => onOpenChange(false)} disabled={mutation.isPending} aria-label="关闭"><X size={16} /></button>
        <div className="registry-editor-body application-governance-form application-budget-form">
          {!writeAvailable && <div className="application-governance-unavailable"><AlertTriangle size={14} />此环境尚未发布编辑 API，部署新后端后可保存。</div>}
          <div className="form-grid"><label className="registry-field"><span className="registry-field-label">月度 Token 上限</span><Input type="number" min="1" step="1" inputMode="numeric" value={tokenLimit} disabled={!writeAvailable || mutation.isPending} onChange={(event) => setTokenLimit(event.target.value)} /></label><label className="registry-field"><span className="registry-field-label">每分钟 Token 上限</span><Input type="number" min="1" step="1" inputMode="numeric" value={tokensPerMinute} disabled={!writeAvailable || mutation.isPending} onChange={(event) => setTokensPerMinute(event.target.value)} /></label></div>
          <div className="form-grid"><label className="registry-field"><span className="registry-field-label">预警阈值（%）</span><Input type="number" min="1" max="100" step="1" inputMode="numeric" value={warningThreshold} disabled={!writeAvailable || mutation.isPending} onChange={(event) => setWarningThreshold(event.target.value)} /></label><label className="application-governance-toggle"><Checkbox checked={enforce} disabled={!writeAvailable || mutation.isPending} onCheckedChange={(checked) => setEnforce(checked === true)} /><span><b>超额时阻断</b><small>关闭后仅记录，不拒绝调用</small></span></label></div>
          {mutation.error && <div className="registry-error">{String(mutation.error)}</div>}
        </div>
        <DialogFooter className="registry-editor-footer"><Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={mutation.isPending}>取消</Button><Button type="submit" disabled={!writeAvailable || !valid || mutation.isPending}>{mutation.isPending ? <RefreshCw className="spin" size={14} /> : null}{mutation.isPending ? "正在保存" : "保存额度"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function ApplicationDepartmentDialog({ application, open, writeAvailable, onOpenChange }: {
  application: GatewayApplicationDetail
  open: boolean
  writeAvailable: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const entities = useQuery(finopsQueries.entities())
  const departments = entities.data?.departments ?? []
  const [departmentId, setDepartmentId] = useState(application.department_id ?? "")
  const [ownerId, setOwnerId] = useState(application.owner_id ?? "")
  const ownerChanged = (ownerId.trim() || null) !== (application.owner_id ?? null)
  const mutation = useMutation({
    mutationFn: async () => {
      // The owner goes first: it is the one that can be refused, and applying the department
      // before a rejected owner would leave the dialog open on a half-saved change.
      if (ownerChanged) {
        await dataSource.updateGatewayApplicationOwner(application.id, {
          owner_id: ownerId.trim() || null,
        })
      }
      return dataSource.updateGatewayApplicationDepartment(application.id, {
        department_id: departmentId || null,
      })
    },
    onSuccess: (value) => {
      queryClient.setQueryData(finopsKeys.gatewayApplication(application.id), value)
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayApplications })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.organizationDirectory })
      onOpenChange(false)
    },
  })
  return <Dialog open={open} onOpenChange={(next) => { if (!mutation.isPending) onOpenChange(next) }}>
    <DialogContent className="registry-editor-dialog application-governance-dialog" finalFocus={false}>
      <form className="registry-editor" onSubmit={(event) => { event.preventDefault(); mutation.mutate() }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>编辑归属</DialogTitle><DialogDescription>{application.display_name}</DialogDescription></DialogHeader>
        <button type="button" className="registry-editor-close" onClick={() => onOpenChange(false)} disabled={mutation.isPending} aria-label="关闭"><X size={16} /></button>
        <div className="registry-editor-body application-governance-form">
          {!writeAvailable && <div className="application-governance-unavailable"><AlertTriangle size={14} />此环境尚未发布编辑 API，部署新后端后可保存。</div>}
          <label className="registry-editor-field">
            <span>所属部门</span>
            <select value={departmentId} disabled={mutation.isPending || !departments.length} onChange={(event) => setDepartmentId(event.target.value)}>
              <option value="">未归属</option>
              {departments.map((department) => <option key={department.id} value={department.id}>{department.name}</option>)}
            </select>
            <small>这个订阅的用量计入该部门的预算，从下一次调用开始生效。</small>
          </label>
          <label className="registry-editor-field">
            <span>归属人</span>
            <Input value={ownerId} disabled={mutation.isPending} placeholder="name@company.com"
              onChange={(event) => setOwnerId(event.target.value)} />
            <small>填了才能给这个人单独配额度。必须是邮箱。</small>
            {(application.person_group_size ?? 1) > 1 && <small className="application-owner-hint">
              <AlertTriangle size={12} />
              {`还有 ${(application.person_group_size ?? 1) - 1} 个订阅去掉用途后缀后同名，可能是同一个人。两个订阅各自算各自的额度。`}
            </small>}
          </label>
          {mutation.error && <div className="registry-error">{String(mutation.error)}</div>}
        </div>
        <DialogFooter className="registry-editor-footer"><Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={mutation.isPending}>取消</Button><Button type="submit" disabled={!writeAvailable || mutation.isPending}>{mutation.isPending ? <RefreshCw className="spin" size={14} /> : null}{mutation.isPending ? "正在保存" : "保存"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function ApplicationModelAccessDialog({ application, models, open, writeAvailable, onOpenChange }: {
  application: GatewayApplicationDetail
  models: ManagedModel[]
  open: boolean
  writeAvailable: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [mode, setMode] = useState<"unrestricted" | "restricted">(application.model_policy_configured ? "restricted" : "unrestricted")
  const [selected, setSelected] = useState(() => new Set(application.allowed_model_ids))
  const [search, setSearch] = useState("")
  const enabledModels = models.filter((model) => model.enabled)
  const visibleModels = enabledModels.filter((model) => `${model.display_name} ${model.model_key}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  const mutation = useMutation({
    mutationFn: () => dataSource.updateGatewayApplicationModelAccess(application.id, {
      mode,
      model_ids: mode === "restricted" ? [...selected] : [],
    }),
    onSuccess: (value) => {
      queryClient.setQueryData(finopsKeys.gatewayApplication(application.id), value)
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayApplications })
      onOpenChange(false)
    },
  })
  const toggleModel = (modelId: string, checked: boolean) => setSelected((current) => {
    const next = new Set(current)
    if (checked) next.add(modelId)
    else next.delete(modelId)
    return next
  })
  return <Dialog open={open} onOpenChange={(next) => { if (!mutation.isPending) onOpenChange(next) }}>
    <DialogContent className="registry-editor-dialog application-governance-dialog" finalFocus={false}>
      <form className="registry-editor" onSubmit={(event) => { event.preventDefault(); mutation.mutate() }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>编辑模型访问</DialogTitle><DialogDescription>{application.display_name}</DialogDescription></DialogHeader>
        <button type="button" className="registry-editor-close" onClick={() => onOpenChange(false)} disabled={mutation.isPending} aria-label="关闭"><X size={16} /></button>
        <div className="registry-editor-body application-governance-form">
          {!writeAvailable && <div className="application-governance-unavailable"><AlertTriangle size={14} />此环境尚未发布编辑 API，部署新后端后可保存。</div>}
          <div className="application-access-mode" role="group" aria-label="模型访问模式"><button type="button" disabled={!writeAvailable || mutation.isPending} className={mode === "unrestricted" ? "active" : ""} aria-pressed={mode === "unrestricted"} onClick={() => setMode("unrestricted")}>全部已发布模型</button><button type="button" disabled={!writeAvailable || mutation.isPending} className={mode === "restricted" ? "active" : ""} aria-pressed={mode === "restricted"} onClick={() => setMode("restricted")}>仅所选模型</button></div>
          {mode === "restricted" && <><label className="smh-search application-model-search"><Search size={14} /><input value={search} disabled={mutation.isPending} onChange={(event) => setSearch(event.target.value)} placeholder="搜索模型..." aria-label="搜索模型" /></label><div className="application-model-options">{visibleModels.map((model) => <label key={model.id}><Checkbox checked={selected.has(model.id)} disabled={mutation.isPending} onCheckedChange={(checked) => toggleModel(model.id, checked === true)} /><span><b data-no-localize>{model.display_name}</b><small data-no-localize>{model.model_key} · {model.runtime_name}</small></span></label>)}{!visibleModels.length && <div className="application-list-empty">没有匹配的模型</div>}</div></>}
          {mutation.error && <div className="registry-error">{String(mutation.error)}</div>}
        </div>
        <DialogFooter className="registry-editor-footer"><Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={mutation.isPending}>取消</Button><Button type="submit" disabled={!writeAvailable || mutation.isPending}>{mutation.isPending ? <RefreshCw className="spin" size={14} /> : null}{mutation.isPending ? "正在保存" : "保存模型访问"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function ApplicationInventoryRow({ application, category, timezone, selected, canSelect, onToggle }: {
  application: GatewayApplicationSummary
  category: SubscriptionCategory
  timezone: string
  selected?: boolean
  canSelect?: boolean
  onToggle?: (checked: boolean) => void
}) {
  const usage = application.budget?.usage_percent ?? 0
  return <a
    className="model-list-row application-inventory-row"
    role="row"
    href={applicationRouteHref(application.id, category)}
    onClick={openApplicationRoute(application.id, category)}
  >
    <div className="model-primary-cell application-primary-cell">
      {canSelect && <span className="application-row-select"
        onClick={(event) => { event.preventDefault(); event.stopPropagation() }}>
        <Checkbox checked={selected === true}
          onCheckedChange={(checked) => onToggle?.(checked === true)}
          aria-label={`选择 ${application.display_name}`} />
      </span>}
      <ApplicationAvatar application={application} showStatus />
      <div><b title={application.display_name}>{application.display_name}</b><code data-no-localize>{application.slug}</code></div>
      {application.system_managed && <em>系统</em>}
    </div>
    <div className="model-runtime-cell"><b>{typeLabels[application.application_type]}</b><span>{application.system_managed ? "系统管理" : "消费对象"} · {application.active_subscription_count} / {application.subscription_count} 个订阅</span></div>
    <div className="model-runtime-cell application-department-cell">{application.department_name
      ? <b data-no-localize>{application.department_name}</b>
      : <em>未归属</em>}</div>
    <div className="model-runtime-cell application-owner-cell">{application.owner_id
      ? <><b data-no-localize title={application.owner_id}>{application.owner_id}</b>
        <span>{OWNER_SOURCE_LABELS[application.owner_source ?? "manual"]}</span></>
      : <><em>未指定</em>
        {(application.person_group_size ?? 1) > 1 && <span title="订阅名去掉用途后缀后相同">
          {`与另 ${(application.person_group_size ?? 1) - 1} 把钥匙同名`}</span>}</>}</div>
    <div className="model-pricing-cell" title={`已使用 ${usage.toFixed(2)}%`}><b>{formatFullCount(application.usage.total_tokens)} Token · {usage.toFixed(2)}%</b><span>{`${application.usage.request_count} 个请求`} · {formatTimestamp(application.usage.last_request_at, timezone)}</span></div>
    <div className="model-status-cell"><ApplicationStatus application={application} /></div>
  </a>
}

function ApplicationSyncStatus({ operation }: { operation: GatewayReleaseOperation }) {
  const terminal = ["succeeded", "failed", "restored"].includes(operation.status)
  const unavailable = operation.worker_available === false && !terminal
  const title = unavailable ? "同步未启动"
    : operation.status === "failed" ? "APIM 应用同步失败"
    : operation.status === "succeeded" ? "APIM 应用已同步"
    : operation.status === "queued" ? "APIM 应用同步已排队"
    : "正在读取 APIM 应用"
  const count = typeof operation.checkpoint.application_count === "number"
    ? operation.checkpoint.application_count
    : null
  const message = unavailable ? operation.worker_unavailable_reason ?? "Release Worker 未部署。"
    : operation.error_message ?? (count == null ? "等待只读库存刷新完成。" : `${count} 个应用已更新。`)
  return <div className={`application-sync-status ${unavailable ? "unavailable" : operation.status}`} role="status" aria-live="polite">
    {unavailable || operation.status === "failed" ? <AlertTriangle size={15} /> : terminal ? <CheckCircle2 size={15} /> : <RefreshCw className="spin" size={15} />}
    <div><b>{title}</b><span>{message}</span></div>
  </div>
}

function ApplicationProvisionStatus({ operation }: { operation: GatewayReleaseOperation }) {
  const terminal = applicationOperationTerminal(operation)
  const unavailable = operation.worker_available === false && !terminal
  const title = applicationProvisionStage(operation)
  const message = unavailable ? operation.worker_unavailable_reason ?? "Release Worker 未部署。"
    : operation.error_message ?? (operation.status === "succeeded" ? "订阅已启用。" : "等待后台创建完成。")
  return <div className={`application-sync-status ${unavailable ? "unavailable" : operation.status}`} role="status" aria-live="polite">
    {unavailable || operation.status === "failed" ? <AlertTriangle size={15} /> : terminal ? <CheckCircle2 size={15} /> : <RefreshCw className="spin" size={15} />}
    <div><b>{title}</b><span>{message}</span></div>
  </div>
}

function Metric({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return <div className="application-metric"><span>{label}</span><b>{value}</b>{detail && <small>{detail}</small>}</div>
}

function ApplicationUsersCard({ application }: { application: GatewayApplicationDetail }) {
  const { timezone } = useTimezone()
  const users = application.users ?? []
  const userCount = application.user_count ?? users.length
  const visibleUsers = users.slice(0, 10)
  const actorLabel = { person: "人员", service: "服务身份", system: "系统身份" }
  return <section className="application-card application-users-card">
    <header className="application-card-head"><div><UserRound size={14} /><h2>使用者用量</h2></div><span>本月 · {userCount} 个</span></header>
    {visibleUsers.length ? <div className="application-user-list">{visibleUsers.map((user) => {
      const Icon = user.actor_type === "person" ? UserRound : user.actor_type === "system" ? ShieldCheck : Bot
      return <div key={`${user.actor_type}:${user.user_id}`} title={`最近请求 ${formatTimestamp(user.last_request_at, timezone)}`}>
        <span className={`application-user-icon ${user.actor_type}`}><Icon size={13} /></span>
        <span className="application-user-copy"><b title={user.display_name} data-no-localize>{user.display_name}</b><small><span>{actorLabel[user.actor_type]}</span><i>·</i><span>{user.request_count} 次调用</span>{user.denied_request_count > 0 && <em>{user.denied_request_count} 次拒绝</em>}</small></span>
        <span className="application-user-usage"><b>{formatCount(user.total_tokens)}</b><small>{formatCost(user.estimated_cost)}</small></span>
      </div>
    })}</div> : <div className="application-card-empty"><UserRound size={18} />本月暂无已归因使用者</div>}
    {userCount > visibleUsers.length && <footer className="application-user-overflow">另有 {userCount - visibleUsers.length} 个使用者</footer>}
  </section>
}

function ApplicationDetailView({
  application,
  loading,
  modelNames,
  canEditAvatar,
  canManage,
  governanceWriteAvailable,
  models,
}: {
  application: GatewayApplicationDetail | undefined
  loading: boolean
  modelNames: Map<string, string>
  canEditAvatar: boolean
  canManage: boolean
  governanceWriteAvailable: boolean
  models: ManagedModel[]
}) {
  const [budgetEditorOpen, setBudgetEditorOpen] = useState(false)
  const [modelEditorOpen, setModelEditorOpen] = useState(false)
  const [departmentEditorOpen, setDepartmentEditorOpen] = useState(false)
  const { timezone } = useTimezone()
  if (loading) return <div className="application-detail-state"><RefreshCw className="spin" size={20} />加载应用详情</div>
  if (!application) return <div className="application-detail-state"><AppWindow size={24} />选择一个应用</div>
  const isAgent = application.application_type === "agent"
  const consumer = isAgent ? "智能体" : "应用"
  const quotaLabel = isAgent ? "智能体额度" : "应用额度"
  const budget = application.budget
  const modelLabels = application.allowed_model_ids.map((id) => modelNames.get(id) ?? id)
  const showUserUsage = application.user_count > 1
    || application.users.some((user) => user.actor_type === "person")
  const showSubscriptionDetails = canManage || application.subscriptions.length > 1
    || application.subscriptions.some((subscription) => subscription.state !== "active" || !subscription.scope_exists)
  return <div className="application-detail-scroll">
    <div className="application-detail-grid">
      <div className="application-detail-main">
        <section className="application-summary-card">
          <header className="application-detail-head">
            <ApplicationAvatarEditor application={application} canManage={canManage} writeAvailable={canEditAvatar} />
            <div className="application-identity">
              <div><h2 title={application.display_name}>{application.display_name}</h2><ApplicationStatus application={application} /></div>
              <p><span>{typeLabels[application.application_type]}</span><i>·</i><code data-no-localize>{application.slug}</code></p>
            </div>
          </header>
          <dl className="application-facts">
            <div><dt>APIM 订阅</dt><dd>{application.subscription_count}</dd></div>
            <div><dt>活动订阅</dt><dd>{application.active_subscription_count}</dd></div>
            <div><dt>最近请求</dt><dd>{formatTimestamp(application.usage.last_request_at, timezone)}</dd></div>
            <div><dt>最近同步</dt><dd>{formatTimestamp(application.subscriptions[0]?.last_synced_at ?? null, timezone)}</dd></div>
          </dl>
        </section>

        <section className="application-card application-usage-card">
          <header className="application-card-head"><div><Activity size={14} /><h2>本月用量</h2></div><span>{application.usage.request_count} 个请求</span></header>
          <div className="application-metric-grid">
            <Metric label="总 Tokens" value={formatFullCount(application.usage.total_tokens)} />
            <Metric label="输入" value={formatCount(application.usage.input_tokens)} />
            <Metric label="缓存读取" value={formatCount(application.usage.cached_tokens)} />
            <Metric label="输出" value={formatCount(application.usage.output_tokens)} />
            <Metric label="拒绝请求" value={formatFullCount(application.usage.denied_request_count)} />
            <Metric label="估算成本" value={formatCost(application.usage.estimated_cost)} />
          </div>
        </section>

        <ApplicationUsageActivityCard applicationId={application.id} />

        <section className="application-card application-budget-card">
          <header className="application-card-head"><div><WalletCards size={14} /><h2>{quotaLabel}</h2></div><div className="application-card-actions">{budget && <span className={budget.enforce ? "enforced" : "audit"}>{budget.enforce ? "阻断" : "仅记录"}</span>}{canManage && <Button type="button" variant="ghost" size="icon-sm" onClick={() => setBudgetEditorOpen(true)} aria-label="编辑应用额度" title="编辑应用额度"><Edit3 size={14} /></Button>}</div></header>
          {budget ? <div className="application-budget-body">
            <div className="application-budget-line"><span>月度 Tokens</span><b>{formatFullCount(budget.used_tokens)} <i>/</i> {formatFullCount(budget.token_limit)}</b></div>
            <Progress className="application-budget-progress" value={Math.min(budget.usage_percent, 100)} aria-label={`应用额度已使用 ${budget.usage_percent.toFixed(2)}%`} />
            <div className="application-budget-stats">
              <div><span>剩余</span><b>{formatFullCount(budget.remaining_tokens)}</b></div>
              <div><span>已使用</span><b>{budget.usage_percent.toFixed(2)}%</b></div>
              <div><span>TPM</span><b>{formatFullCount(budget.tokens_per_minute)}</b></div>
              <div><span>预警</span><b>{budget.warning_threshold_percent}%</b></div>
              <div><span>预留占用</span><b>{budget.pending_reserved_tokens == null ? "—" : formatFullCount(budget.pending_reserved_tokens)}</b></div>
              <div><span>上限占用</span><b>{budget.finalized_upper_bound_tokens == null ? "—" : formatFullCount(budget.finalized_upper_bound_tokens)}</b></div>
              <div><span>可用额度</span><b>{budget.available_tokens == null ? "—" : formatFullCount(budget.available_tokens)}</b></div>
              <div><span>待处理请求</span><b>{budget.pending_reservation_count == null ? "—" : formatFullCount(budget.pending_reservation_count)}</b></div>
            </div>
            <div className="application-ledger-snapshot"><span>账本快照</span><time dateTime={budget.ledger_snapshot_at ?? undefined}>{budget.ledger_snapshot_at ? formatTimestamp(budget.ledger_snapshot_at, timezone) : "未同步"}</time></div>
            {budget.stale_reservation_count != null && budget.stale_reservation_count > 0 && <div className="application-ledger-snapshot"><span>延迟结算</span><b>{formatFullCount(budget.stale_reservation_count)}</b></div>}
          </div> : <div className="application-card-empty"><WalletCards size={18} />未配置本月额度</div>}
        </section>

        <section className="application-card application-model-card">
          <header className="application-card-head"><div><Layers3 size={14} /><h2>模型访问</h2></div><div className="application-card-actions"><span className={!application.model_policy_configured ? "unrestricted" : modelLabels.length ? "configured" : "denied"}>{!application.model_policy_configured ? "未限制" : modelLabels.length ? `${modelLabels.length} 个模型` : "拒绝全部"}</span>{canManage && <Button type="button" variant="ghost" size="icon-sm" onClick={() => setModelEditorOpen(true)} aria-label="编辑模型访问" title="编辑模型访问"><Edit3 size={14} /></Button>}</div></header>
          {!application.model_policy_configured ? <div className="application-card-empty"><ShieldCheck size={18} />可使用已发布模型</div>
            : modelLabels.length ? <div className="application-model-list">{modelLabels.map((name) => <span key={name} title={name}>{name}</span>)}</div>
            : <div className="application-card-empty warning"><ShieldAlert size={18} />没有已分配模型</div>}
        </section>
      </div>

      <aside className="application-detail-rail">
        {showUserUsage && <ApplicationUsersCard application={application} />}
        {showSubscriptionDetails && <ApplicationSubscriptionCard application={application} canManage={canManage} />}

        <section className="application-card application-governance-card">
          <header className="application-card-head"><div><Gauge size={14} /><h2>归因状态</h2></div>{canManage && !application.system_managed && <div className="application-card-actions"><Button type="button" variant="ghost" size="icon-sm" onClick={() => setDepartmentEditorOpen(true)} aria-label="编辑所属部门" title="编辑所属部门"><Edit3 size={14} /></Button></div>}</header>
          <dl>
            <div><dt>所属部门</dt><dd>{application.department_name ?? (application.department_id ?? "未归属")}</dd></div>
            <div><dt>归属人</dt><dd>{application.owner_id
              ? `${application.owner_id}（${OWNER_SOURCE_LABELS[application.owner_source ?? "manual"]}）`
              : "未指定"}</dd></div>
            <div><dt>{consumer}</dt><dd>已绑定</dd></div>
            <div><dt>调用身份</dt><dd>{isAgent ? "智能体" : application.application_type === "delegated_user" ? "应用 + 人员" : application.system_managed ? "系统" : "服务"}</dd></div>
            <div><dt>额度账本</dt><dd>{budget?.ledger_snapshot_at ? "已投影" : budget ? "未同步" : "未配置"}</dd></div>
            <div><dt>密钥</dt><dd>{application.key_management_available && canManage ? "按需读取" : "不存储"}</dd></div>
          </dl>
        </section>

        <section className="application-card application-audit-card">
          <header className="application-card-head"><div><ShieldCheck size={14} /><h2>审计</h2></div><span>{application.audit.length}</span></header>
          {application.audit.length ? <div className="application-timeline">{application.audit.map((event) => <div key={event.id}><i /><span><b>{isAgent ? auditLabels[event.operation].replace("应用", "智能体") : auditLabels[event.operation]}</b><small><time>{formatTimestamp(event.created_at, timezone)}</time><span title={event.actor}>· {event.actor}</span></small></span></div>)}</div>
            : <div className="application-card-empty">没有审计记录</div>}
        </section>
      </aside>
    </div>
    {budgetEditorOpen && <ApplicationBudgetDialog application={application} open={budgetEditorOpen} writeAvailable={governanceWriteAvailable} onOpenChange={setBudgetEditorOpen} />}
    {modelEditorOpen && <ApplicationModelAccessDialog application={application} models={models} open={modelEditorOpen} writeAvailable={governanceWriteAvailable} onOpenChange={setModelEditorOpen} />}
    {departmentEditorOpen && <ApplicationDepartmentDialog application={application} open={departmentEditorOpen} writeAvailable={governanceWriteAvailable} onOpenChange={setDepartmentEditorOpen} />}
  </div>
}

function BulkDepartmentBar({ selected, onClear }: {
  selected: Set<string>
  onClear: () => void
}) {
  const queryClient = useQueryClient()
  const entities = useQuery(finopsQueries.entities())
  const [departmentId, setDepartmentId] = useState("")
  const [result, setResult] = useState<{ updated: number } | null>(null)
  const mutation = useMutation({
    mutationFn: () => dataSource.updateGatewayApplicationDepartmentBulk({
      application_ids: [...selected],
      department_id: departmentId || null,
    }),
    onSuccess: (value) => {
      setResult(value)
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayApplications })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.organizationDirectory })
      onClear()
    },
  })
  return <div className="application-bulk-bar">
    <span>{`已选 ${selected.size} 个订阅`}</span>
    <label>
      <span>归到部门</span>
      <select value={departmentId} disabled={mutation.isPending}
        onChange={(event) => setDepartmentId(event.target.value)}>
        <option value="">未归属</option>
        {(entities.data?.departments ?? []).map((department) =>
          <option key={department.id} value={department.id}>{department.name}</option>)}
      </select>
    </label>
    <Button type="button" disabled={mutation.isPending || !selected.size}
      onClick={() => { setResult(null); mutation.mutate() }}>
      {mutation.isPending ? <RefreshCw className="spin" size={14} /> : null}应用
    </Button>
    <Button type="button" variant="ghost" size="sm" disabled={mutation.isPending} onClick={onClear}>
      取消选择
    </Button>
    {mutation.error && <span className="registry-error">{String(mutation.error)}</span>}
    {result && <span className="application-bulk-result">{`已更新 ${result.updated} 个订阅`}</span>}
  </div>
}

export function ApplicationsPage() {
  const { user } = useAuth()
  const canManage = user?.role === "owner"
  const { timezone } = useTimezone()
  const queryClient = useQueryClient()
  const [mobileActionsTarget, setMobileActionsTarget] = useState<HTMLElement | null>(null)
  const {
    width: navigationWidth,
    minWidth: navigationMinWidth,
    maxWidth: navigationMaxWidth,
    startResize: startNavigationResize,
    resizeWithKeyboard: resizeNavigationWithKeyboard,
    resetWidth: resetNavigationWidth,
  } = useResizablePane({
    storageKey: "turnstile_subscriptions_navigation_width",
    defaultWidth: 276,
    minWidth: 236,
    maxWidth: 380,
  })
  const [applicationId, setApplicationId] = useState<string | null>(applicationFromUrl)
  const [category, setCategory] = useState<SubscriptionCategory>(subscriptionCategoryFromUrl)
  const [department, setDepartment] = useState<string | null>(departmentFromUrl)
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const departmentOptions = useQuery(finopsQueries.entities()).data?.departments
  const applications = useQuery({
    ...finopsQueries.gatewayApplications(),
    enabled: !applicationId,
  })
  const registry = useQuery(finopsQueries.registry())
  const [search, setSearch] = useState("")
  const [filter, setFilter] = useState<ApplicationFilter>("all")
  const [activeOperationId, setActiveOperationId] = useState<string | null>(() => applicationOperationIdFromUrl(window.location.href))
  const [createOpen, setCreateOpen] = useState(false)
  const detail = useQuery(finopsQueries.gatewayApplication(applicationId))
  const items = applications.data?.items ?? []
  const applicationItems = useMemo(() => items.filter((item) => item.application_type !== "agent"), [items])
  const agentItems = useMemo(() => items.filter((item) => item.application_type === "agent"), [items])
  const categoryItems = category === "agents" ? agentItems : applicationItems
  const visible = useMemo(() => {
    const normalized = search.trim().toLocaleLowerCase()
    return categoryItems.filter((application) => {
      const filterMatch = filter === "all"
        || (filter === "active" && application.status === "active" && application.stale_subscription_count === 0)
        || (filter === "attention" && (application.status !== "active" || application.stale_subscription_count > 0))
        || (filter === "system" && application.system_managed)
      const searchMatch = !normalized || `${application.display_name} ${application.slug} ${application.department_id ?? ""} ${application.department_name ?? ""}`.toLocaleLowerCase().includes(normalized)
      const departmentMatch = !department
        || (department === "unassigned" ? !application.department_id : application.department_id === department)
      return filterMatch && searchMatch && departmentMatch
    })
  }, [categoryItems, filter, search, department])
  // Resolved from the rows themselves rather than by fetching the directory again: the list
  // already carries every channel's department name, and a filter that has no matching row
  // has nothing to label anyway.
  const departmentLabel = useMemo(() => {
    if (!department) return ""
    if (department === "unassigned") return "未归属"
    return categoryItems.find((item) => item.department_id === department)?.department_name
      ?? department
  }, [categoryItems, department])
  useEffect(() => {
    setMobileActionsTarget(document.getElementById("mobile-topbar-end-actions"))
  }, [])
  useEffect(() => {
    const sync = () => {
      setApplicationId(applicationFromUrl())
      setCategory(subscriptionCategoryFromUrl())
      setDepartment(departmentFromUrl())
      setActiveOperationId(applicationOperationIdFromUrl(window.location.href))
    }
    window.addEventListener("popstate", sync)
    window.addEventListener(FINOPS_NAVIGATE_EVENT, sync)
    return () => {
      window.removeEventListener("popstate", sync)
      window.removeEventListener(FINOPS_NAVIGATE_EVENT, sync)
    }
  }, [])
  useEffect(() => {
    setFilter("all")
    setSearch("")
  }, [category])
  const operation = useQuery(finopsQueries.gatewayReleaseOperation(activeOperationId))
  const operationTerminal = applicationOperationTerminal(operation.data)
  useEffect(() => {
    if (!operationTerminal) return
    void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayApplications })
  }, [operationTerminal, operation.data?.id, queryClient])
  const acceptOperation = (accepted: GatewayReleaseOperationAccepted) => {
    setActiveOperationId(accepted.operation.id)
    const url = new URL(window.location.href)
    url.searchParams.set(APPLICATION_OPERATION_PARAMETER, accepted.operation.id)
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`)
  }
  const syncMutation = useMutation({
    mutationFn: (gatewayId: string) => dataSource.syncGatewayApplications(gatewayId),
    onSuccess: acceptOperation,
  })
  const gatewayId = items[0]?.gateway_profile_id
    ?? registry.data?.gateways.find((gateway) => gateway.implementation === "apim")?.id
    ?? null
  const busy = syncMutation.isPending || Boolean(activeOperationId && !operationTerminal && !operation.isError)
  const modelNames = useMemo(() => new Map((registry.data?.models ?? []).map((model) => [model.id, model.display_name])), [registry.data?.models])
  const apiMissing = applications.error instanceof ApiError && applications.error.status === 404
  const consumer = category === "agents" ? "智能体" : "应用"
  const consumerPlural = category === "agents" ? "智能体" : "应用"
  const ConsumerIcon = category === "agents" ? Bot : AppWindow
  const filters: Array<{ id: ApplicationFilter; label: string; count: number }> = [
    { id: "all", label: "全部", count: categoryItems.length },
    { id: "active", label: "活动", count: categoryItems.filter((item) => item.status === "active" && item.stale_subscription_count === 0).length },
    { id: "attention", label: "需关注", count: categoryItems.filter((item) => item.status !== "active" || item.stale_subscription_count > 0).length },
    { id: "system", label: "系统", count: categoryItems.filter((item) => item.system_managed).length },
  ]
  const categoryCards = [
    { id: "applications" as const, label: "应用", icon: AppWindow, items: applicationItems },
    { id: "agents" as const, label: "智能体", icon: Bot, items: agentItems },
  ].map((card) => ({
    ...card,
    activeCount: card.items.filter((item) => item.status === "active" && item.stale_subscription_count === 0).length,
    attentionCount: card.items.filter((item) => item.status !== "active" || item.stale_subscription_count > 0).length,
  }))

  if (applicationId) {
    const detailCategory = detail.data?.application_type === "agent" ? "agents" : category
    const detailConsumer = detailCategory === "agents" ? "智能体" : "应用"
    const DetailIcon = detailCategory === "agents" ? Bot : AppWindow
    return <div className="application-detail-workspace">
    <header className="application-detail-breadcrumb"><a href={applicationRouteHref(null, detailCategory)} onClick={openApplicationRoute(null, detailCategory)}>订阅对象</a><ChevronRight size={13} /><a href={applicationRouteHref(null, detailCategory)} onClick={openApplicationRoute(null, detailCategory)}>{detailCategory === "agents" ? "智能体" : "应用"}</a><ChevronRight size={13} /><code title={detail.data?.display_name}>{detail.data?.display_name ?? applicationId}</code></header>
    {detail.error ? <div className="smh-empty application-detail-error"><DetailIcon size={28} /><b>{detailCategory === "agents" ? "无法加载智能体详情" : "无法加载应用详情"}</b><span>{String(detail.error)}</span><a className="ghost-button" href={applicationRouteHref(null, detailCategory)} onClick={openApplicationRoute(null, detailCategory)}>返回{detailCategory === "agents" ? "智能体" : "应用"}</a></div>
      : <main className="application-detail"><ApplicationDetailView application={detail.data} loading={detail.isLoading} modelNames={modelNames} canEditAvatar={detail.data?.avatar_url !== undefined} canManage={user?.role === "owner"} governanceWriteAvailable={detail.data?.governance_write_available === true} models={registry.data?.models ?? []} /></main>}
  </div>
  }

  return <div className="smh-workspace applications-workspace">
    {user?.role === "owner" && mobileActionsTarget && createPortal(<div className="subscriptions-mobile-topbar-actions">
      <Button type="button" variant="ghost" size="icon-sm" disabled={!gatewayId || !applications.data?.sync_available || busy} onClick={() => gatewayId && syncMutation.mutate(gatewayId)} aria-label="同步 APIM" title={applications.data?.sync_available ? "从 APIM 刷新订阅库存" : applications.data?.sync_unavailable_reason ?? "发布工作进程未部署"}>{busy ? <RefreshCw className="spin" size={15} /> : <RefreshCw size={15} />}</Button>
      <Button type="button" variant="ghost" size="icon-sm" disabled={busy} onClick={() => setCreateOpen(true)} aria-label={`添加${consumer}`} title={`添加${consumer}`}><Plus size={16} /></Button>
    </div>, mobileActionsTarget)}
    <header className="smh-page-header applications-header">
      <div><span className="smh-header-icon"><KeyRound size={17} /></span><h1>订阅对象</h1><span>{applications.isLoading ? "—" : items.length}</span></div>
      <div className="smh-header-actions">
        {user?.role === "owner" ? <><Button variant="outline" disabled={!gatewayId || !applications.data?.sync_available || busy} onClick={() => gatewayId && syncMutation.mutate(gatewayId)} title={applications.data?.sync_available ? "从 APIM 刷新订阅库存" : applications.data?.sync_unavailable_reason ?? "发布工作进程未部署"}>{busy ? <RefreshCw className="spin" size={13} /> : <RefreshCw size={13} />}同步 APIM</Button><Button disabled={busy} onClick={() => setCreateOpen(true)} title={`添加${consumer}`}><Plus size={14} />添加{consumer}</Button></> : <span className="applications-readonly"><ShieldAlert size={13} />只读</span>}
      </div>
    </header>
    {syncMutation.error && <div className="application-error-band" role="alert"><AlertTriangle size={14} /><span>{String(syncMutation.error)}</span></div>}
    {operation.data?.operation_kind === "application_sync" && <ApplicationSyncStatus operation={operation.data} />}
    {operation.data?.operation_kind === "application_provision" && <ApplicationProvisionStatus operation={operation.data} />}
    <div className="subscriptions-layout" style={{ "--subscriptions-nav-width": `${navigationWidth}px` } as CSSProperties}>
      <nav className="subscriptions-local-nav" aria-label="订阅对象类别">
        <header className="subscriptions-local-heading"><span>订阅对象</span><small>{categoryCards.length}</small></header>
        <div className="subscriptions-category-list">
          {categoryCards.map((card) => {
            const CategoryIcon = card.icon
            const status = card.attentionCount > 0 ? "attention" : card.activeCount > 0 ? "active" : "empty"
            return <a key={card.id} className={`subscriptions-category-card ${category === card.id ? "active" : ""}`} aria-current={category === card.id ? "page" : undefined} href={applicationRouteHref(null, card.id)} onClick={openApplicationRoute(null, card.id)}>
              <span className={`subscriptions-category-icon ${card.id}`}><CategoryIcon size={17} /><i className={status} /></span>
              <span className="subscriptions-category-copy"><b>{card.label}</b><small><span>{card.activeCount}</span><span>活动</span>{card.attentionCount > 0 && <><i>·</i><em>{card.attentionCount}</em><span>需关注</span></>}</small></span>
              <strong data-no-localize>{card.items.length}</strong>
            </a>
          })}
        </div>
      </nav>
      <button className="runtime-pane-handle subscriptions-pane-handle" type="button" role="separator" aria-label="调整订阅对象导航宽度" aria-orientation="vertical" aria-valuemin={navigationMinWidth} aria-valuemax={navigationMaxWidth} aria-valuenow={navigationWidth} title="拖动调整宽度，双击复位" onPointerDown={startNavigationResize} onKeyDown={resizeNavigationWithKeyboard} onDoubleClick={resetNavigationWidth} />
      <section className="subscriptions-content" aria-label={`${consumer}订阅`}>
      {applications.isLoading ? <div className="registry-loading"><RefreshCw className="spin" />加载订阅对象</div>
      : applications.error ? <div className="smh-empty application-api-state"><KeyRound size={28} /><b>{apiMissing ? "当前环境尚未部署订阅对象 API" : "无法加载订阅对象"}</b><span>{apiMissing ? "发布订阅对象功能后，此处将显示库存。" : String(applications.error)}</span></div>
      : <div className="application-inventory">
        <div className="application-inventory-toolbar">
          <div className="application-filters" role="group" aria-label={`${consumer}状态筛选`}>{filters.filter((item) => item.id === "all" || item.count > 0).map((item) => <button type="button" key={item.id} className={filter === item.id ? "active" : ""} aria-pressed={filter === item.id} onClick={() => setFilter(item.id)}><span>{item.label}</span><small>{item.count}</small></button>)}</div>
          <ExpandableSearch key={category} value={search} onChange={setSearch} placeholder={`搜索${consumer}或订阅 ID...`} ariaLabel={`搜索${consumer}`} />
        </div>
        {canManage && selected.size > 0 && <BulkDepartmentBar selected={selected}
          onClear={() => setSelected(new Set())} />}
        {canManage && <div className="application-department-filter">
          <label><span>部门</span>
            <select value={department ?? ""} onChange={(event) => {
              const value = event.target.value
              const url = new URL(window.location.href)
              if (value) url.searchParams.set("department", value)
              else url.searchParams.delete("department")
              window.history.pushState(null, "", `${url.pathname}${url.search}`)
              setDepartment(value || null)
              setSelected(new Set())
            }}>
              <option value="">全部部门</option>
              <option value="unassigned">未归属</option>
              {(departmentOptions ?? []).map((item) =>
                <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          </label>
          {!!visible.length && <Button type="button" variant="outline" size="sm"
            onClick={() => setSelected(new Set(visible.filter((item) => !item.system_managed).map((item) => item.id)))}>
            {`全选当前 ${visible.length} 个`}
          </Button>}
        </div>}
        {department && <div className="application-department-filter">
          <span>只看部门：<b data-no-localize>{departmentLabel}</b></span>
          <Button type="button" variant="ghost" size="sm" onClick={() => {
            const url = new URL(window.location.href)
            url.searchParams.delete("department")
            window.history.pushState(null, "", `${url.pathname}${url.search}`)
            setDepartment(null)
          }}><X size={13} />显示全部</Button>
        </div>}
        <ResizableGridTable className="model-table application-model-table" role="table" aria-label={`${consumer}列表`} headerSelector=".application-table-head" minWidths={APPLICATION_TABLE_COLUMN_MIN_WIDTHS} columnGap={12} horizontalPadding={32}>
          <div className="model-table-head application-table-head" role="row">
            {[consumer, "类型 / 订阅", "部门", "归属人", "本月用量 / 最近请求", "状态"].map((label) => <span className="model-table-heading" role="columnheader" aria-label={label} key={label}><span>{label}</span></span>)}
          </div>
          <div className="model-table-body application-inventory-list" role="rowgroup">
          {visible.map((application) => <ApplicationInventoryRow application={application} category={category} timezone={timezone} key={application.id}
            selected={selected.has(application.id)} canSelect={canManage && !application.system_managed}
            onToggle={(checked) => setSelected((current) => {
              const next = new Set(current)
              if (checked) next.add(application.id)
              else next.delete(application.id)
              return next
            })} />)}
          {!visible.length && <div className="application-list-empty"><ConsumerIcon size={18} /><span>{categoryItems.length ? `没有匹配的${consumer}` : `没有已发现的${consumer}`}</span></div>}
          </div>
        </ResizableGridTable>
        <footer className="model-table-footer"><span>{visible.length} 个{consumerPlural}</span></footer>
      </div>}
      </section>
    </div>
    {createOpen && user?.role === "owner" && <ApplicationCreateDialog
      gateways={registry.data?.gateways ?? []} inventory={applications.data}
      inventoryError={applications.isError || registry.isError}
      applicationType={category === "agents" ? "agent" : "service"}
      operation={operation.data} operationError={operation.error ? String(operation.error) : null}
      refreshing={applications.isFetching || registry.isFetching || operation.isFetching}
      onRefresh={() => { void applications.refetch(); void registry.refetch(); if (activeOperationId) void operation.refetch() }}
      onClose={() => setCreateOpen(false)} onAccepted={acceptOperation}
      onOpenApplication={(id) => {
        setCreateOpen(false)
        window.history.pushState(null, "", applicationRouteHref(id, category))
        window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT))
      }}
    />}
  </div>
}
