import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react"
import { createPortal } from "react-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Activity,
  AlertTriangle,
  Bot,
  AppWindow,
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
import { RegistryNoticeBanner } from "../components/ui/registry-notice-banner"
import { ApimLogo } from "../components/brand-logos"
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu"
import { Input } from "../components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select"
import { Progress } from "../components/ui/progress"
import { ResizableGridTable } from "../components/ui/resizable-table"
import { Textarea } from "../components/ui/textarea"
import { useResizablePane } from "../components/ui/use-resizable-pane"
import { ApplicationUsageActivityCard } from "../components/finops/application-usage-activity-card"
import { ApplicationSubscriptionCard } from "../components/finops/application-subscription-card"
import { ApplicationClientQuickstart } from "../components/finops/application-client-quickstart"
import { ApplicationCreateDialog } from "../components/applications/application-create-dialog"
import {
  APPLICATION_OPERATION_PARAMETER,
  applicationOperationIdFromUrl,
  applicationOperationTerminal,
  applicationProvisionStage,
  provisionedApplicationId,
} from "../components/applications/application-create-form"
import { ApiError, dataSource } from "../data-sources/apim/api"
import { applicationBudgetPending, finopsKeys, finopsQueries, storeGatewayApplicationBudget } from "../data-sources/apim/queries"
import { apimGatewayProfiles } from "../lib/gateway-topology"
import type {
  BudgetMode,
  GatewayApplicationAuditEvent,
  GatewayApplicationDetail,
  GatewayApplicationStatus,
  GatewayApplicationSummary,
  GatewayReleaseOperation,
  GatewayReleaseOperationAccepted,
  ManagedModel,
  ModelRegistry,
} from "../data-sources/apim/types"
import { FINOPS_NAVIGATE_EVENT } from "../lib/navigation"
import { getIntlLocale } from "../locales"
import { useAuth } from "../providers/auth-provider"
import { useTimezone } from "../providers/timezone-provider"

type ApplicationFilter = "all" | "active" | "attention" | "system"

const APPLICATION_TABLE_COLUMN_MIN_WIDTHS = [180, 130, 180, 100] as const

const statusLabels: Record<GatewayApplicationStatus, string> = {
  active: "活动",
  suspended: "已暂停",
  retired: "已停用",
}

const auditLabels: Record<GatewayApplicationAuditEvent["operation"], string> = {
  created: "创建订阅",
  adopted: "接管订阅",
  updated: "更新订阅",
  budget_updated: "更新额度",
  models_updated: "更新模型访问",
  subscription_synced: "同步订阅",
  suspended: "暂停订阅",
  resumed: "恢复订阅",
  retired: "停用订阅",
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

function subscriptionApimFromUrl() {
  return new URL(window.location.href).searchParams.get("apim")
}

function applicationRouteHref(applicationId: string | null, apimId = subscriptionApimFromUrl()) {
  const url = new URL(window.location.href)
  url.searchParams.set("page", "applications")
  url.searchParams.set("consumer", "all")
  if (apimId) url.searchParams.set("apim", apimId)
  else url.searchParams.delete("apim")
  if (applicationId) url.searchParams.set("application", applicationId)
  else url.searchParams.delete("application")
  return `${url.pathname}${url.search}`
}

function openApplicationRoute(applicationId: string | null, apimId = subscriptionApimFromUrl()) {
  return (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    if (event.button !== 0) return
    event.preventDefault()
    window.history.pushState(null, "", applicationRouteHref(applicationId, apimId))
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
  const state = application.stale_subscription_count > 0 ? "stale" : application.status
  return <span className={`application-avatar ${size}`} aria-hidden="true">
    {application.avatar_url && !imageFailed
      ? <img src={application.avatar_url} alt="" onError={() => setImageFailed(true)} />
      : <KeyRound size={size === "detail" ? 19 : 15} />}
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
  const [budgetMode, setBudgetMode] = useState<BudgetMode>(budget?.budget_mode ?? "tokens")
  const [tokenLimit, setTokenLimit] = useState(String(budget?.token_limit ?? 100_000))
  const [usdLimit, setUsdLimit] = useState(budget?.usd_limit ?? "")
  const [tokensPerMinute, setTokensPerMinute] = useState(String(budget?.tokens_per_minute ?? 100_000))
  const [warningThreshold, setWarningThreshold] = useState(String(budget?.warning_threshold_percent ?? 80))
  const [enforce, setEnforce] = useState(budget?.enforce ?? true)
  const mutation = useMutation({
    mutationFn: () => dataSource.updateGatewayApplicationBudget(application.id, {
      ...(budgetMode === "usd"
        ? { budget_mode: "usd" as const, token_limit: null, usd_limit: usdLimit }
        : { token_limit: Number(tokenLimit) }),
      tokens_per_minute: Number(tokensPerMinute),
      enforce,
      warning_threshold_percent: Number(warningThreshold),
    }),
    retry: false,
    onSuccess: async (value) => {
      await storeGatewayApplicationBudget(queryClient, value)
      onOpenChange(false)
    },
  })
  const valid = (budgetMode === "usd"
    ? /^\d{1,16}(\.\d{1,14})?$/.test(usdLimit) && Number(usdLimit) > 0
    : Number(tokenLimit) > 0 && Number.isSafeInteger(Number(tokenLimit)))
    && Number(tokensPerMinute) > 0
    && Number.isSafeInteger(Number(tokensPerMinute))
    && Number(tokensPerMinute) <= 2_147_483_647
    && Number(warningThreshold) >= 1
    && Number(warningThreshold) <= 100
    && Number.isInteger(Number(warningThreshold))
  return <Dialog open={open} onOpenChange={(next) => { if (!mutation.isPending) onOpenChange(next) }}>
    <DialogContent className="registry-editor-dialog application-governance-dialog application-budget-dialog" finalFocus={false}>
      <form className="registry-editor" onSubmit={(event) => { event.preventDefault(); if (writeAvailable && valid && !mutation.isPending) mutation.mutate() }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>编辑订阅额度</DialogTitle><DialogDescription>{application.display_name}</DialogDescription></DialogHeader>
        <button type="button" className="registry-editor-close" onClick={() => onOpenChange(false)} disabled={mutation.isPending} aria-label="关闭"><X size={16} /></button>
        <div className="registry-editor-body application-governance-form application-budget-form">
          {!writeAvailable && <div className="application-governance-unavailable"><AlertTriangle size={14} />此环境尚未发布编辑 API，部署新后端后可保存。</div>}
          <div className="registry-field"><span className="registry-field-label">预算单位</span><Select value={budgetMode} items={[{ value: "tokens", label: "Token" }, { value: "usd", label: "USD" }]} disabled={!writeAvailable || mutation.isPending} onValueChange={(value) => {
            setBudgetMode(value as BudgetMode)
            setTokenLimit("")
            setUsdLimit("")
          }}><SelectTrigger aria-label="预算单位"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="tokens">Token</SelectItem><SelectItem value="usd">USD</SelectItem></SelectContent></Select></div>
          <div className="form-grid"><label className="registry-field"><span className="registry-field-label">{budgetMode === "usd" ? "月度金额上限（USD）" : "月度 Token 上限"}</span><Input name={budgetMode === "usd" ? "usd_limit" : "token_limit"} type={budgetMode === "usd" ? "text" : "number"} min={budgetMode === "usd" ? undefined : "1"} step={budgetMode === "usd" ? undefined : "1"} inputMode={budgetMode === "usd" ? "decimal" : "numeric"} value={budgetMode === "usd" ? usdLimit : tokenLimit} disabled={!writeAvailable || mutation.isPending} onChange={(event) => budgetMode === "usd" ? setUsdLimit(event.target.value) : setTokenLimit(event.target.value)} /></label><label className="registry-field"><span className="registry-field-label">每分钟 Token 上限</span><Input type="number" min="1" step="1" inputMode="numeric" value={tokensPerMinute} disabled={!writeAvailable || mutation.isPending} onChange={(event) => setTokensPerMinute(event.target.value)} /></label></div>
          <div className="form-grid"><label className="registry-field"><span className="registry-field-label">预警阈值（%）</span><Input type="number" min="1" max="100" step="1" inputMode="numeric" value={warningThreshold} disabled={!writeAvailable || mutation.isPending} onChange={(event) => setWarningThreshold(event.target.value)} /></label><label className="application-governance-toggle"><Checkbox checked={enforce} disabled={!writeAvailable || mutation.isPending} onCheckedChange={(checked) => setEnforce(checked === true)} /><span><b>超额时阻断</b><small>关闭后仅记录，不拒绝调用</small></span></label></div>
          {mutation.error && <div className="registry-error">{String(mutation.error)}</div>}
        </div>
        <DialogFooter className="registry-editor-footer"><Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={mutation.isPending}>取消</Button><Button type="submit" disabled={!writeAvailable || !valid || mutation.isPending}>{mutation.isPending ? <RefreshCw className="spin" size={14} /> : null}{mutation.isPending ? "正在保存" : "保存额度"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function ApplicationOrganizationDialog({ application, onClose, onSaved }: {
  application: GatewayApplicationDetail
  onClose: () => void
  onSaved: (projected: boolean) => void
}) {
  const queryClient = useQueryClient()
  const entities = useQuery(finopsQueries.entities())
  const [organizationId, setOrganizationId] = useState(application.organization_id ?? "")
  const [departmentId, setDepartmentId] = useState(application.department_id ?? "")
  const [personId, setPersonId] = useState(application.person_id ?? "")
  const organizations = entities.data?.organizations ?? []
  const departments = (entities.data?.departments ?? []).filter(item => item.parent_id === organizationId)
  const departmentSupported = application.department_assignment_supported === true
  const personSupported = application.person_assignment_supported === true
  const people = (entities.data?.users ?? []).filter(item => item.parent_id === departmentId)
  const valid = (!organizationId || organizations.some((item) => item.id === organizationId))
    && (!departmentSupported || !departmentId || departments.some(item => item.id === departmentId))
    && (!personSupported || !personId || people.some(item => item.id === personId))
  const unchanged = organizationId === (application.organization_id ?? "")
    && (!departmentSupported || departmentId === (application.department_id ?? ""))
    && (!personSupported || personId === (application.person_id ?? ""))
  const mutation = useMutation({
    mutationFn: () => personSupported
      ? dataSource.updateGatewayApplicationOrganization(application.id, organizationId || null, departmentId || null, personId || null)
      : departmentSupported
      ? dataSource.updateGatewayApplicationOrganization(application.id, organizationId || null, departmentId || null)
      : dataSource.updateGatewayApplicationOrganization(application.id, organizationId || null),
    retry: false,
    onSuccess: async (value) => {
      await queryClient.cancelQueries({ queryKey: finopsKeys.gatewayApplication(application.id) })
      await queryClient.cancelQueries({ queryKey: finopsKeys.gatewayApplications })
      queryClient.setQueryData<GatewayApplicationDetail>(finopsKeys.gatewayApplication(application.id),
        current => current ? { ...current, organization_id: value.organization_id, organization_name: value.organization_name,
          ...(departmentSupported ? { department_id: value.department_id ?? null, department_name: value.department_name ?? null } : {}) } : current)
      if (personSupported) queryClient.setQueryData<GatewayApplicationDetail>(finopsKeys.gatewayApplication(application.id),
        current => current ? { ...current, person_id: value.person_id ?? null, person_name: value.person_name ?? null } : current)
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayApplications })
      onSaved(value.projected)
      onClose()
    },
  })
  return <Dialog open onOpenChange={(open) => { if (!open && !mutation.isPending) onClose() }}>
    <DialogContent className="registry-editor-dialog application-governance-dialog" finalFocus={false}>
      <form className="registry-editor" onSubmit={(event) => { event.preventDefault(); if (valid && !entities.isPending && !entities.isError && !mutation.isPending) mutation.mutate() }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>{personSupported ? "编辑订阅归属" : departmentSupported ? "编辑组织与部门" : "编辑所属组织"}</DialogTitle><DialogDescription>{application.display_name}</DialogDescription></DialogHeader>
        <button type="button" className="registry-editor-close" onClick={onClose} disabled={mutation.isPending} aria-label="关闭"><X size={16} /></button>
        <div className="registry-editor-body application-governance-form">
          <div className="registry-field"><span className="registry-field-label">所属组织</span>
            <Select value={organizationId} items={[{ value: "", label: "未分配" }, ...organizations.map(item => ({ value: item.id, label: item.name }))]} disabled={entities.isPending || entities.isError || mutation.isPending} onValueChange={value => {
              if ((value ?? "") !== organizationId) { setDepartmentId(""); setPersonId("") }
              setOrganizationId(value ?? "")
            }}>
              <SelectTrigger aria-label="所属组织"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="">未分配</SelectItem>{organizations.map(item => <SelectItem key={item.id} value={item.id}><span data-no-localize>{item.name}</span></SelectItem>)}</SelectContent>
            </Select>
          </div>
          {departmentSupported && <div className="registry-field"><span className="registry-field-label">所属部门</span>
            <Select value={departmentId} items={[{ value: "", label: "未分配" }, ...departments.map(item => ({ value: item.id, label: item.name }))]} disabled={!organizationId || entities.isPending || entities.isError || mutation.isPending} onValueChange={value => {
              if ((value ?? "") !== departmentId) setPersonId("")
              setDepartmentId(value ?? "")
            }}>
              <SelectTrigger aria-label="所属部门"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="">未分配</SelectItem>{departments.map(item => <SelectItem key={item.id} value={item.id}><span data-no-localize>{item.name}</span></SelectItem>)}</SelectContent>
            </Select>
          </div>}
          {personSupported && <div className="registry-field"><span className="registry-field-label">归属人员</span>
            <Select value={personId} items={[{ value: "", label: "未分配" }, ...people.map(item => ({ value: item.id, label: item.name }))]} disabled={!departmentId || entities.isPending || entities.isError || mutation.isPending} onValueChange={value => setPersonId(value ?? "")}>
              <SelectTrigger aria-label="归属人员"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="">未分配</SelectItem>{people.map(item => <SelectItem key={item.id} value={item.id}><span data-no-localize>{item.name}</span></SelectItem>)}</SelectContent>
            </Select>
          </div>}
          {entities.isError && <div className="registry-error">{String(entities.error)}</div>}
          {mutation.error && <div className="registry-error">{String(mutation.error)}</div>}
        </div>
        <DialogFooter className="registry-editor-footer"><Button type="button" variant="outline" onClick={onClose} disabled={mutation.isPending}>取消</Button><Button type="submit" disabled={!valid || entities.isPending || entities.isError || mutation.isPending || unchanged}>{mutation.isPending && <RefreshCw className="spin" size={14} />}{mutation.isPending ? "正在保存" : "保存"}</Button></DialogFooter>
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

function ApplicationInventoryRow({ application, timezone }: {
  application: GatewayApplicationSummary
  timezone: string
}) {
  const usage = application.budget?.usage_percent ?? 0
  const budgetPending = applicationBudgetPending(application.budget)
  return <a
    className="model-list-row application-inventory-row"
    role="row"
    data-application-id={application.id}
    data-apim-id={application.gateway_profile_id}
    href={applicationRouteHref(application.id)}
    onClick={openApplicationRoute(application.id)}
  >
    <div className="model-primary-cell application-primary-cell">
      <ApplicationAvatar application={application} showStatus />
      <div><b title={application.display_name}>{application.display_name}</b><code data-no-localize>{application.slug}</code></div>
      {application.system_managed && <em>系统</em>}
    </div>
    <div className="model-runtime-cell"><b>{application.system_managed ? "系统管理" : "消费对象"}</b><span>{application.active_subscription_count} / {application.subscription_count} 个订阅</span></div>
    <div className="model-pricing-cell" title={budgetPending ? "预算修改等待应用" : `已使用 ${usage.toFixed(2)}%`}><b>{budgetPending ? <span className="application-budget-pending" role="status"><RefreshCw className="spin" size={12} /><span>预算修改等待应用</span></span> : <>{formatFullCount(application.usage.total_tokens)} Token · {usage.toFixed(2)}%</>}</b><span>{`${application.usage.request_count} 个请求`} · {formatTimestamp(application.usage.last_request_at, timezone)}</span></div>
    <div className="model-status-cell"><ApplicationStatus application={application} /></div>
  </a>
}

function ApplicationSyncButton({ targets, busy, disabled, title, compact = false, onSync }: {
  targets: Array<{ id: string; name: string }>
  busy: boolean
  disabled: boolean
  title: string
  compact?: boolean
  onSync: (gatewayId: string) => void
}) {
  const content = <><RefreshCw className={busy ? "spin" : undefined} size={compact ? 15 : 13} />{!compact && "同步 APIM"}</>
  const button = <Button type="button" variant={compact ? "ghost" : "outline"} size={compact ? "icon-sm" : "default"} disabled={disabled || busy || targets.length === 0} aria-label="同步 APIM" title={title}>{content}</Button>
  if (targets.length < 2) return <Button type="button" variant={compact ? "ghost" : "outline"} size={compact ? "icon-sm" : "default"} disabled={disabled || busy || targets.length === 0} aria-label="同步 APIM" title={title} onClick={() => targets[0] && onSync(targets[0].id)}>{content}</Button>
  return <DropdownMenu>
    <DropdownMenuTrigger render={button} />
    <DropdownMenuContent align="end">
      {targets.map(target => <DropdownMenuItem key={target.id} disabled={disabled || busy} onClick={() => onSync(target.id)}><RefreshCw size={14} /><span>{target.name}</span></DropdownMenuItem>)}
    </DropdownMenuContent>
  </DropdownMenu>
}

function ApplicationSyncStatus({ operation }: { operation: GatewayReleaseOperation }) {
  const terminal = ["succeeded", "failed", "restored"].includes(operation.status)
  const unavailable = operation.worker_available === false && !terminal
  const title = unavailable ? "同步未启动"
    : operation.status === "failed" ? "APIM 订阅同步失败"
    : operation.status === "succeeded" ? "APIM 订阅已同步"
    : operation.status === "queued" ? "APIM 订阅同步已排队"
    : "正在读取 APIM 订阅"
  const count = typeof operation.checkpoint.application_count === "number"
    ? operation.checkpoint.application_count
    : null
  const message = unavailable ? operation.worker_unavailable_reason ?? "Release Worker 未部署。"
    : operation.error_message ?? (count == null ? "等待只读库存刷新完成。" : `${count} 个订阅已更新。`)
  return <div className={`application-sync-status ${unavailable ? "unavailable" : operation.status}`} role="status" aria-live="polite">
    {unavailable || operation.status === "failed" ? <AlertTriangle size={15} /> : terminal ? <CheckCircle2 size={15} /> : <RefreshCw className="spin" size={15} />}
    <div><b>{title}</b><span>{message}</span></div>
  </div>
}

function ApplicationProvisionStatus({ operation, onOpen }: { operation: GatewayReleaseOperation; onOpen: (id: string) => void }) {
  const terminal = applicationOperationTerminal(operation)
  const unavailable = operation.worker_available === false && !terminal
  const title = applicationProvisionStage(operation)
  const message = unavailable ? operation.worker_unavailable_reason ?? "Release Worker 未部署。"
    : operation.error_message ?? (operation.status === "succeeded" ? "订阅已启用。" : "等待后台创建完成。")
  const createdId = provisionedApplicationId(operation)
  return <div className={`application-sync-status ${unavailable ? "unavailable" : operation.status}`} role="status" aria-live="polite">
    {unavailable || operation.status === "failed" ? <AlertTriangle size={15} /> : terminal ? <CheckCircle2 size={15} /> : <RefreshCw className="spin" size={15} />}
    <div><b>{title}</b><span>{message}</span></div>
    {createdId && <Button type="button" variant="outline" size="sm" onClick={() => onOpen(createdId)}>打开订阅详情</Button>}
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
  refreshing,
  refreshError,
  onRefresh,
  modelNames,
  canEditAvatar,
  canManage,
  governanceWriteAvailable,
  models,
  registry,
}: {
  application: GatewayApplicationDetail | undefined
  loading: boolean
  refreshing: boolean
  refreshError: boolean
  onRefresh: () => void
  modelNames: Map<string, string>
  canEditAvatar: boolean
  canManage: boolean
  governanceWriteAvailable: boolean
  models: ManagedModel[]
  registry: ModelRegistry | undefined
}) {
  const [budgetEditorOpen, setBudgetEditorOpen] = useState(false)
  const [modelEditorOpen, setModelEditorOpen] = useState(false)
  const [organizationEditorOpen, setOrganizationEditorOpen] = useState(false)
  const [organizationPending, setOrganizationPending] = useState(false)
  const { timezone } = useTimezone()
  if (loading) return <div className="application-detail-state"><RefreshCw className="spin" size={20} />加载订阅详情</div>
  if (!application) return <div className="application-detail-state"><KeyRound size={24} />选择一个订阅</div>
  const isAgent = application.application_type === "agent"
  const budget = application.budget
  const budgetPending = applicationBudgetPending(budget)
  const budgetUpdating = refreshing || budgetPending
  const pendingTokens = budget?.pending_reserved_tokens
  const usdBudget = budget?.budget_mode === "usd"
  const usdVerified = budget?.usd_cost_available !== false && budget?.ledger_status !== "unavailable" && budget?.ledger_status !== "prepared"
  const budgetMoney = (value: string | null | undefined) => value == null ? "--" : new Intl.NumberFormat(getIntlLocale(), {
    style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 14,
  }).format(Number(value))
  const enforcedUsagePercent = usdBudget
    ? usdVerified && budget?.usd_limit && budget.remaining_usd != null ? Math.max(0, (1 - Number(budget.remaining_usd) / Number(budget.usd_limit)) * 100) : 0
    : budget && budget.token_limit != null && budget.token_limit > 0 && budget.available_tokens != null
    ? Math.max(0, (budget.token_limit - budget.available_tokens) / budget.token_limit * 100)
    : budget?.usage_percent ?? 0
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
              <p><span>{application.system_managed ? "系统订阅" : "APIM 订阅"}</span><i>·</i><code data-no-localize>{application.slug}</code></p>
            </div>
          </header>
          <dl className="application-facts">
            <div><dt>所属组织</dt><dd className="application-organization-value"><span data-no-localize={application.organization_name ? true : undefined}>{application.organization_name ?? (application.organization_assignment_supported ? "未分配" : "--")}</span>{canManage && governanceWriteAvailable && application.organization_assignment_supported && <Button type="button" className="application-ownership-edit" variant="ghost" size="icon-sm" onClick={() => setOrganizationEditorOpen(true)} aria-label="编辑所属组织" title="编辑所属组织"><Edit3 size={14} /></Button>}</dd></div>
            <div><dt>所属部门</dt><dd className="application-organization-value"><span data-no-localize={application.department_name ? true : undefined}>{application.department_name ?? (application.department_assignment_supported ? "未分配" : "--")}</span>{canManage && governanceWriteAvailable && <Button type="button" className="application-ownership-edit" variant="ghost" size="icon-sm" onClick={() => setOrganizationEditorOpen(true)} disabled={!application.department_assignment_supported} aria-label="编辑所属部门" title={application.department_assignment_supported ? "编辑所属部门" : "部门设置尚未部署"}><Edit3 size={14} /></Button>}</dd></div>
            <div><dt>归属人员</dt><dd className="application-organization-value"><span data-no-localize={application.person_name ? true : undefined}>{application.person_name ?? (application.person_assignment_supported ? "未分配" : "--")}</span>{canManage && governanceWriteAvailable && <Button type="button" className="application-ownership-edit" variant="ghost" size="icon-sm" onClick={() => setOrganizationEditorOpen(true)} disabled={!application.person_assignment_supported} aria-label="编辑归属人员" title={application.person_assignment_supported ? "编辑归属人员" : "人员设置尚未部署"}><Edit3 size={14} /></Button>}</dd></div>
            <div><dt>关联订阅</dt><dd><span data-no-localize>{application.active_subscription_count} / {application.subscription_count}</span> <span>有效订阅</span></dd></div>
            <div><dt>最近请求</dt><dd>{formatTimestamp(application.usage.last_request_at, timezone)}</dd></div>
            <div><dt>最近同步</dt><dd>{formatTimestamp(application.subscriptions[0]?.last_synced_at ?? null, timezone)}</dd></div>
          </dl>
          {application.tags && Object.keys(application.tags).some(key => key !== "organization_id") && <dl className="application-tags" aria-label="标签">{Object.entries(application.tags).filter(([key]) => key !== "organization_id").map(([key, value]) => <div key={key} data-no-localize><dt>{key}</dt><dd>{value}</dd></div>)}</dl>}
          {organizationPending && <div className="application-governance-unavailable" role="status">{application.department_assignment_supported ? "组织与部门归属已保存，等待同步" : "组织归属已保存，等待同步"}</div>}
        </section>
        {organizationEditorOpen && <ApplicationOrganizationDialog key={application.id} application={application} onClose={() => setOrganizationEditorOpen(false)} onSaved={projected => setOrganizationPending(!projected)} />}

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

        <ApplicationClientQuickstart application={application} registry={registry} models={models} />

        <section className="application-card application-budget-card" aria-busy={budgetUpdating && !refreshError}>
          <header className="application-card-head"><div><WalletCards size={14} /><h2>订阅额度</h2></div><div className="application-card-actions">{budget && !budgetUpdating && !refreshError && <span className={budget.enforce ? "enforced" : "audit"}>{budget.enforce ? "阻断" : "仅记录"}</span>}{canManage && <Button type="button" variant="ghost" size="icon-sm" disabled={budgetUpdating || refreshError} onClick={() => setBudgetEditorOpen(true)} aria-label="编辑订阅额度" title="编辑订阅额度"><Edit3 size={14} /></Button>}</div></header>
          {refreshError ? <div className="application-budget-state error" role="alert"><AlertTriangle size={16} /><span>无法刷新额度，请重试。</span><Button type="button" variant="ghost" size="icon-sm" disabled={refreshing} onClick={onRefresh} aria-label="刷新状态" title="刷新状态"><RefreshCw className={refreshing ? "spin" : undefined} size={14} /></Button></div>
            : budgetUpdating ? <div className="application-budget-state" role="status"><RefreshCw className="spin" size={16} /><span>{budgetPending ? "预算修改等待应用" : "正在刷新额度"}</span></div>
            : budget ? <div className="application-budget-body">
            <div className="application-budget-line"><span>APIM 实际可用</span><b>{usdBudget ? usdVerified ? budgetMoney(budget.remaining_usd) : "--" : budget.available_tokens == null ? "—" : formatFullCount(budget.available_tokens)} <i>/</i> {usdBudget ? budgetMoney(budget.usd_limit) : budget.token_limit == null ? "--" : formatFullCount(budget.token_limit)}</b></div>
            <Progress className="application-budget-progress" value={Math.min(enforcedUsagePercent, 100)} aria-label={`APIM 额度已占用 ${enforcedUsagePercent.toFixed(2)}%`} />
            <div className="application-budget-stats">
              <div><span>已确认用量</span><b>{usdBudget ? usdVerified ? budgetMoney(budget.used_usd) : "--" : budget.ledger_status === "unavailable" ? "—" : formatFullCount(budget.used_tokens)}</b></div>
              <div><span>待结算预留</span><b>{usdBudget ? budgetMoney(budget.pending_reserved_usd) : pendingTokens == null ? "—" : formatFullCount(pendingTokens)}</b></div>
              <div><span>保守终态上界</span><b>{usdBudget ? budgetMoney(budget.finalized_upper_bound_usd) : budget.finalized_upper_bound_tokens == null ? "—" : formatFullCount(budget.finalized_upper_bound_tokens)}</b></div>
              <div><span>APIM 可用余额</span><b>{usdBudget ? usdVerified ? budgetMoney(budget.remaining_usd) : "--" : budget.available_tokens == null ? "—" : formatFullCount(budget.available_tokens)}</b></div>
              <div><span>预留数量</span><b>{budget.pending_reservation_count == null ? "—" : formatFullCount(budget.pending_reservation_count)}</b></div>
              <div><span>上界终态数量</span><b>{budget.finalized_upper_bound_count == null ? "—" : formatFullCount(budget.finalized_upper_bound_count)}</b></div>
              <div><span>TPM</span><b>{formatFullCount(budget.tokens_per_minute)}</b></div>
              <div><span>已投影 TPM</span><b>{budget.projected_tokens_per_minute == null ? "—" : formatFullCount(budget.projected_tokens_per_minute)}</b></div>
              <div><span>TPM 同步</span><b>{budget.tpm_sync_status === "projected" ? "已投影" : budget.tpm_sync_status === "pending" ? "等待同步" : "状态未知"}</b></div>
              <div><span>预警</span><b>{budget.warning_threshold_percent}%</b></div>
            </div>
            <div className={`application-budget-ledger ${budget.stale_reservation_count ? "warning" : ""}`}>
              <span>{budget.ledger_status === "unavailable" ? "执行账本暂不可用，余额未知" : budget.ledger_status === "prepared" ? "执行账本尚未启用" : budget.ledger_status === "current" && budget.ledger_read_at ? <>执行账本 <time>{formatTimestamp(budget.ledger_read_at, timezone)}</time></> : budget.ledger_snapshot_at ? <>账本快照 <time>{formatTimestamp(budget.ledger_snapshot_at, timezone)}</time></> : "等待账本快照"}</span>
              {budget.policy_pending ? <span>预算修改等待应用</span> : null}
              {budget.ledger_balance_tokens != null && budget.ledger_balance_tokens < 0 ? <span>已超额占用 <b>{formatFullCount(-budget.ledger_balance_tokens)}</b> Tokens</span> : null}
              {budget.pending_reservation_count ? <span>{budget.stale_reservation_count ? <><b>{budget.stale_reservation_count}</b> 条等待恢复</> : "预留均在结算窗口内"}{budget.oldest_reservation_at ? <> · 最早 <time>{formatTimestamp(budget.oldest_reservation_at, timezone)}</time></> : null}</span> : budget.pending_reservation_count === 0 && budget.ledger_snapshot_at ? <span>无待结算预留</span> : null}
              {budget.finalized_upper_bound_count ? <span><b>{budget.finalized_upper_bound_count}</b> 条无完整遥测，继续按请求上界计费</span> : null}
            </div>
            {application.budget_projection && <div className="application-ledger-snapshot" role="status" aria-live="polite">
              <span>{application.budget_projection.status === "projected" ? "额度配置已同步至网关账本" : application.budget_projection.status === "pending" ? "额度已保存，等待网关账本同步" : "无法确认网关账本同步状态"}</span>
              <b>{application.budget_projection.tokens_per_minute == null ? "—" : `${formatFullCount(application.budget_projection.tokens_per_minute)} TPM`}</b>
            </div>}
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
          <header className="application-card-head"><div><Gauge size={14} /><h2>归因状态</h2></div></header>
          <dl>
            <div><dt>订阅</dt><dd>已绑定</dd></div>
            <div><dt>调用身份</dt><dd>{isAgent ? "智能体" : application.application_type === "delegated_user" ? "应用 + 人员" : application.system_managed ? "系统" : "服务"}</dd></div>
            <div><dt>额度账本</dt><dd>{budget ? "已投影" : "未配置"}</dd></div>
            <div><dt>密钥</dt><dd>{application.key_management_available && canManage ? "按需读取" : "不存储"}</dd></div>
          </dl>
        </section>

        <section className="application-card application-audit-card">
          <header className="application-card-head"><div><ShieldCheck size={14} /><h2>审计</h2></div><span>{application.audit.length}</span></header>
          {application.audit.length ? <div className="application-timeline">{application.audit.map((event) => <div key={event.id}><i /><span><b>{auditLabels[event.operation]}</b><small><time>{formatTimestamp(event.created_at, timezone)}</time><span title={event.actor}>· {event.actor}</span></small></span></div>)}</div>
            : <div className="application-card-empty">没有审计记录</div>}
        </section>
      </aside>
    </div>
    {budgetEditorOpen && <ApplicationBudgetDialog application={application} open={budgetEditorOpen} writeAvailable={governanceWriteAvailable} onOpenChange={setBudgetEditorOpen} />}
    {modelEditorOpen && <ApplicationModelAccessDialog application={application} models={models} open={modelEditorOpen} writeAvailable={governanceWriteAvailable} onOpenChange={setModelEditorOpen} />}
  </div>
}

export function ApplicationsPage() {
  const { user } = useAuth()
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
  const [selectedApimId, setSelectedApimId] = useState<string | null>(subscriptionApimFromUrl)
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
  const targetItems = items.filter(item => !selectedApimId || item.gateway_profile_id === selectedApimId)
  const visible = useMemo(() => {
    const normalized = search.trim().toLocaleLowerCase()
    return targetItems.filter((application) => {
      const filterMatch = filter === "all"
        || (filter === "active" && application.status === "active" && application.stale_subscription_count === 0)
        || (filter === "attention" && (application.status !== "active" || application.stale_subscription_count > 0))
        || (filter === "system" && application.system_managed)
      const searchMatch = !normalized || `${application.display_name} ${application.slug} ${application.owner_id ?? ""} ${application.department_id ?? ""}`.toLocaleLowerCase().includes(normalized)
      return filterMatch && searchMatch
    })
  }, [targetItems, filter, search])
  useEffect(() => {
    setMobileActionsTarget(document.getElementById("mobile-topbar-end-actions"))
  }, [])
  useEffect(() => {
    const sync = () => {
      setApplicationId(applicationFromUrl())
      setSelectedApimId(subscriptionApimFromUrl())
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
  }, [selectedApimId])
  const operation = useQuery(finopsQueries.gatewayReleaseOperation(activeOperationId))
  const operationTerminal = applicationOperationTerminal(operation.data)
  const createdApplicationId = provisionedApplicationId(operation.data)
  useEffect(() => {
    if (!operationTerminal) return
    void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayApplications })
    const createdId = provisionedApplicationId(operation.data)
    if (createdId) void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayApplication(createdId) })
  }, [operationTerminal, operation.data?.id, queryClient])
  const trackOperation = (accepted: GatewayReleaseOperationAccepted) => {
    queryClient.setQueryData(finopsKeys.gatewayReleaseOperation(accepted.operation.id), accepted.operation)
    setActiveOperationId(accepted.operation.id)
    const url = new URL(window.location.href)
    url.searchParams.set(APPLICATION_OPERATION_PARAMETER, accepted.operation.id)
    window.history.replaceState(null, "", url)
  }
  const dismissOperationNotice = () => {
    const url = new URL(window.location.href)
    url.searchParams.delete(APPLICATION_OPERATION_PARAMETER)
    window.history.replaceState(null, "", url)
    setActiveOperationId(null)
  }
  const openCreatedApplication = (id: string) => {
    dismissOperationNotice()
    setCreateOpen(false)
    window.history.pushState(null, "", applicationRouteHref(id))
    window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT))
  }
  const refreshCreation = () => {
    void applications.refetch()
    void registry.refetch()
    if (activeOperationId) void operation.refetch()
  }
  const syncMutation = useMutation({
    mutationFn: (gatewayId: string) => dataSource.syncGatewayApplications(gatewayId),
    onSuccess: trackOperation,
  })
  const topology = registry.data?.apim_target_topology ?? null
  const topologyAvailable = registry.data?.apim_target_topology_available === true
  const registeredApimGateways = apimGatewayProfiles(registry.data?.gateways ?? [], topology, topologyAvailable)
  const syncTargets = registeredApimGateways.filter(gateway => gateway.enabled && gateway.implementation === "apim")
  const syncTitle = applications.data?.sync_available ? "从 APIM 刷新订阅库存" : applications.data?.sync_unavailable_reason ?? "发布工作进程未部署"
  const gatewayId = registeredApimGateways.find((gateway) => gateway.enabled && gateway.implementation === "apim" && gateway.is_default)?.id
    ?? registeredApimGateways.find((gateway) => gateway.enabled && gateway.implementation === "apim")?.id
    ?? null
  const busy = syncMutation.isPending || Boolean(operation.data?.worker_available && !operationTerminal)
  const modelNames = useMemo(() => new Map((registry.data?.models ?? []).map((model) => [model.id, model.display_name])), [registry.data?.models])
  const apiMissing = applications.error instanceof ApiError && applications.error.status === 404
  const filters: Array<{ id: ApplicationFilter; label: string; count: number }> = [
    { id: "all", label: "全部", count: targetItems.length },
    { id: "active", label: "活动", count: targetItems.filter((item) => item.status === "active" && item.stale_subscription_count === 0).length },
    { id: "attention", label: "需关注", count: targetItems.filter((item) => item.status !== "active" || item.stale_subscription_count > 0).length },
    { id: "system", label: "系统", count: targetItems.filter((item) => item.system_managed).length },
  ]
  const apimInstances = registeredApimGateways.filter(gateway => gateway.implementation === "apim")
  const apimCards = [
    { id: null, name: "全部", items },
    ...apimInstances.map(gateway => ({ id: gateway.id, name: gateway.name, items: items.filter(item => item.gateway_profile_id === gateway.id) })),
  ]

  if (applicationId) {
    const detailCategory = detail.data?.application_type === "agent" ? "agents" : "applications"
    const DetailIcon = detailCategory === "agents" ? Bot : AppWindow
    return <div className="application-detail-workspace">
    <header className="smh-page-header application-detail-breadcrumb"><div>
      <span className="smh-header-icon"><DetailIcon size={17} aria-hidden="true" /></span>
      <nav className="application-breadcrumb-trail" aria-label="订阅管理"><a href={applicationRouteHref(null, detailCategory)} onClick={openApplicationRoute(null, detailCategory)}>订阅管理</a><ChevronRight size={13} /><a href={applicationRouteHref(null, detailCategory)} onClick={openApplicationRoute(null, detailCategory)}>{detailCategory === "agents" ? "智能体" : "应用"}</a><ChevronRight size={13} /><span className="application-breadcrumb-current" aria-current="page" title={detail.data?.display_name}>{detail.data?.display_name ?? applicationId}</span></nav>
    </div></header>
    {detail.error && !detail.data ? <div className="smh-empty application-detail-error"><DetailIcon size={28} /><b>{detailCategory === "agents" ? "无法加载智能体详情" : "无法加载应用详情"}</b><span>{String(detail.error)}</span><a className="ghost-button" href={applicationRouteHref(null, detailCategory)} onClick={openApplicationRoute(null, detailCategory)}>返回{detailCategory === "agents" ? "智能体" : "应用"}</a></div>
      : <main className="application-detail"><ApplicationDetailView application={detail.data} loading={detail.isLoading} refreshing={detail.isFetching} refreshError={detail.isRefetchError} onRefresh={() => void detail.refetch()} registry={registry.data} modelNames={modelNames} canEditAvatar={detail.data?.avatar_url !== undefined} canManage={user?.role === "owner"} governanceWriteAvailable={detail.data?.governance_write_available === true} models={registry.data?.models ?? []} /></main>}
  </div>
  }

  return <div className="smh-workspace applications-workspace">
    {user?.role === "owner" && mobileActionsTarget && createPortal(<div className="subscriptions-mobile-topbar-actions">
      <ApplicationSyncButton targets={syncTargets} busy={busy} disabled={!applications.data?.sync_available} title={syncTitle} compact onSync={targetId => syncMutation.mutate(targetId)} />
      <Button type="button" variant="ghost" size="icon-sm" disabled={busy || applications.isLoading || registry.isLoading} onClick={() => setCreateOpen(true)} aria-label="添加订阅" title="添加订阅"><Plus size={16} /></Button>
    </div>, mobileActionsTarget)}
    <header className="smh-page-header applications-header">
      <div><span className="smh-header-icon"><KeyRound size={17} /></span><h1>订阅管理</h1><span>{applications.isLoading ? "—" : items.length}</span></div>
      <div className="smh-header-actions">
        {user?.role === "owner" ? <><ApplicationSyncButton targets={syncTargets} busy={busy} disabled={!applications.data?.sync_available} title={syncTitle} onSync={targetId => syncMutation.mutate(targetId)} /><Button disabled={busy || applications.isLoading || registry.isLoading} onClick={() => setCreateOpen(true)}><Plus size={14} />添加订阅</Button></> : <span className="applications-readonly"><ShieldAlert size={13} />只读</span>}
      </div>
    </header>
    {syncMutation.error && <div className="application-error-band" role="alert"><AlertTriangle size={14} /><span>{String(syncMutation.error)}</span></div>}
    {operation.data?.operation_kind === "application_sync" && <ApplicationSyncStatus operation={operation.data} />}
    {operation.data?.operation_kind === "application_provision" && (operation.data.status === "succeeded"
      ? <RegistryNoticeBanner notice={{ text: "订阅已启用", ok: true, actionLabel: createdApplicationId ? "查看详情" : undefined }} onDismiss={dismissOperationNotice} onAction={createdApplicationId ? () => openCreatedApplication(createdApplicationId) : undefined} />
      : <ApplicationProvisionStatus operation={operation.data} onOpen={openCreatedApplication} />)}
    {activeOperationId && operation.error && <div className="application-error-band" role="alert"><span>无法读取创建或同步进度。</span><Button type="button" variant="outline" size="sm" onClick={() => void operation.refetch()}>重新检查</Button></div>}
    <div className="subscriptions-layout" style={{ "--subscriptions-nav-width": `${navigationWidth}px` } as CSSProperties}>
      <nav className="subscriptions-local-nav" aria-label="APIM 实例">
        <header className="subscriptions-local-heading"><span>APIM 实例</span><small>{apimInstances.length}</small></header>
        <div className="subscriptions-category-list">
          {apimCards.map((card) => {
            return <a key={card.id ?? "all"} data-apim-id={card.id ?? "all"} className={`subscriptions-category-card ${selectedApimId === card.id ? "active" : ""}`} aria-current={selectedApimId === card.id ? "page" : undefined} href={applicationRouteHref(null, card.id)} onClick={openApplicationRoute(null, card.id)}>
              <span className="subscriptions-category-icon">{card.id ? <ApimLogo size={15} /> : <Layers3 size={15} />}</span>
              <span className="subscriptions-category-copy"><b data-no-localize={card.id ? true : undefined} title={card.name}>{card.name}</b><small>{applications.isLoading ? "—" : `${card.items.reduce((total, item) => total + item.subscription_count, 0)} 个订阅`}</small></span>
              <strong data-no-localize>{applications.isLoading ? "—" : card.items.length}</strong>
            </a>
          })}
        </div>
      </nav>
      <button className="runtime-pane-handle subscriptions-pane-handle" type="button" role="separator" aria-label="调整订阅对象导航宽度" aria-orientation="vertical" aria-valuemin={navigationMinWidth} aria-valuemax={navigationMaxWidth} aria-valuenow={navigationWidth} title="拖动调整宽度，双击复位" onPointerDown={startNavigationResize} onKeyDown={resizeNavigationWithKeyboard} onDoubleClick={resetNavigationWidth} />
      <section className="subscriptions-content" aria-label="订阅">
      {applications.isLoading ? <div className="registry-loading"><RefreshCw className="spin" />加载订阅对象</div>
      : applications.error ? <div className="smh-empty application-api-state"><KeyRound size={28} /><b>{apiMissing ? "当前环境尚未部署订阅对象 API" : "无法加载订阅对象"}</b><span>{apiMissing ? "发布订阅对象功能后，此处将显示库存。" : String(applications.error)}</span></div>
      : <div className="application-inventory">
        <div className="application-inventory-toolbar">
          <div className="application-filters" role="group" aria-label="订阅状态筛选">{filters.filter((item) => item.id === "all" || item.count > 0).map((item) => <button type="button" key={item.id} className={filter === item.id ? "active" : ""} aria-pressed={filter === item.id} onClick={() => setFilter(item.id)}><span>{item.label}</span><small>{item.count}</small></button>)}</div>
          <ExpandableSearch value={search} onChange={setSearch} placeholder="搜索名称或订阅 ID..." ariaLabel="搜索订阅" />
        </div>
        <ResizableGridTable className="model-table application-model-table" role="table" aria-label="订阅列表" headerSelector=".application-table-head" minWidths={APPLICATION_TABLE_COLUMN_MIN_WIDTHS} columnGap={12} horizontalPadding={32}>
          <div className="model-table-head application-table-head" role="row">
            {["订阅", "APIM 订阅", "本月用量 / 最近请求", "状态"].map((label) => <span className="model-table-heading" role="columnheader" aria-label={label} key={label}><span>{label}</span></span>)}
          </div>
          <div className="model-table-body application-inventory-list" role="rowgroup">
          {visible.map((application) => <ApplicationInventoryRow application={application} timezone={timezone} key={application.id} />)}
          {!visible.length && <div className="application-list-empty"><KeyRound size={18} /><span>{targetItems.length ? "没有匹配的订阅" : "没有已发现的订阅"}</span></div>}
          </div>
        </ResizableGridTable>
        <footer className="model-table-footer"><span>{visible.length} 个订阅</span></footer>
      </div>}
      </section>
    </div>
    {createOpen && user?.role === "owner" && <ApplicationCreateDialog
      gateways={registeredApimGateways}
      inventory={applications.data}
      inventoryError={applications.isError || registry.isError}
      applicationType="service"
      operation={operation.data}
      operationError={operation.error ? String(operation.error) : null}
      refreshing={applications.isFetching || operation.isFetching || registry.isFetching}
      onRefresh={refreshCreation}
      onClose={() => setCreateOpen(false)}
      onAccepted={trackOperation}
      onOpenApplication={openCreatedApplication}
    />}
  </div>
}
