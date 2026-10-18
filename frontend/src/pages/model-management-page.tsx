import { useEffect, useId, useMemo, useRef, useState } from "react"
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query"
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import {
  Activity,
  Bot,
  Check,
  ChevronRight,
  CircleDashed,
  Cpu,
  Database,
  Edit3,
  Eye,
  EyeOff,
  KeyRound,
  List,
  MoreHorizontal,
  PanelLeft,
  Plus,
  Orbit,
  RefreshCw,
  Save,
  Search,
  Send,
  Server,
  ShieldCheck,
  Trash2,
  TriangleAlert,
  Wifi,
  WifiOff,
  X,
} from "lucide-react"

import { dataSource, usageWindow } from "../data-sources/apim/api"
import { RegistryNoticeBanner, type RegistryNotice as Notice } from "../components/ui/registry-notice-banner"
import { useIsMobile } from "../hooks/use-mobile"
import {
  ClaudeLogo,
  DeepSeekLogo,
  FoundryLogo,
  GatewayBrandLogo,
  KimiLogo,
  MicrosoftLogo,
  OpenAiLogo,
  ProviderBrandLogo,
  gatewayBrandFromIdentity,
  providerBrandFromIdentity,
  providerBrandFromMetadata,
  providerBrandLabel,
  type GatewayBrand,
  type ProviderBrand,
} from "../components/brand-logos"
import { FINOPS_NAVIGATE_EVENT } from "../lib/navigation"
import { apimGatewayEntries, apimGatewayProfiles, connectionTargetDetails, gatewayEntries, modelTargetDetails, publicationTargetDetails } from "../lib/gateway-topology"
import { emptyModelFilters, matchesModelFilters, modelFilterRecord, modelInventoryFamily, publicationFilterRecord } from "../lib/model-inventory-filters"
import { Button } from "../components/ui/button"
import { ButtonGroup } from "../components/ui/button-group"
import { Checkbox } from "../components/ui/checkbox"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from "../components/ui/alert-dialog"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../components/ui/dialog"
import { ActivityHeatmap } from "../components/finops/activity-heatmap"
import { aggregateActivity } from "../components/finops/usage-activity-card"
import { ChartSeriesLegend, useSeriesToggle } from "../components/finops/chart-legend"
import { FinOpsChartTooltip } from "../components/finops/chart-tooltip"
import { ExpandableSearch } from "../components/ui/expandable-search"
import { Input } from "../components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../components/ui/select"
import { Textarea } from "../components/ui/textarea"
import { ResizableGridTable } from "../components/ui/resizable-table"
import { getIntlLocale, useLocale } from "../locales/index"
import { formatTimeAgo } from "../lib/time"
import { hasModelPrices, priceRequests } from "../lib/pricing"
import { finopsKeys, finopsQueries, storeRegistry } from "../data-sources/apim/queries"
import { assistantSettingsKey } from "../components/assistant/queries"
import { useTimezone } from "../providers/timezone-provider"
import { useAuth } from "../providers/auth-provider"
import { pricingSyncSummary } from "../components/model-management/model-edit-form"
import type { PriceSyncResponse } from "../data-sources/apim/types"
import {
  ModelPublicationDialog,
  publicationStatusLabel,
} from "../components/model-management/model-publication-dialog"
import { ConnectionDialog } from "../components/model-management/connection-dialog"
import { ModelEditDialog } from "../components/model-management/model-edit-dialog"
import { RuntimeDiagnosticDialog, useRuntimeDiagnostics } from "../components/model-management/runtime-diagnostic-dialog"
import { runtimeDiagnostic, runtimeHealthDescription, runtimeHealthLabels, runtimeHealthState, runtimeWithTelemetryHealth, type RuntimeHealthState } from "../lib/runtime-health"
import { ModelAvailabilityDialog, ModelAvailabilityStatus, ModelAvailabilitySwitch, useModelAvailabilityCommand } from "../components/model-management/model-availability"
import { ModelInventoryFilterChips, ModelInventoryFilterMenu } from "../components/model-management/model-inventory-filter-menu"
import { ModelInventoryGraph } from "../components/model-management/model-inventory-graph"
import { ModelUsageDetail, modelDetailHref, openModelDetailLink } from "../components/model-management/model-usage-detail"
import {
  ModelVendorLogo,
  modelVendorFromMetadata,
  modelVendorLabel,
} from "../components/model-management/model-vendor-select"
import {
  ApimGatewayWorkspace,
  MODEL_MANAGEMENT_TABS,
  type ModelManagementTab,
} from "../components/model-management/apim-gateway-workspace"
import type {
  ApimTargetTopologyList,
  GatewayPublication,
  GatewayPublicationList,
  GatewayProfile,
  ManagedModel,
  ModelConnectionUpdate,
  ModelAvailabilityReadback,
  ModelProvider,
  ModelRegistry,
  ModelRuntime,
  RuntimeHealth,
  UsageRequestSummary,
} from "../data-sources/apim/types"

type RegistryKind = "runtime" | "model" | "provider" | "gateway"
type RegistryItem = ModelRuntime | ManagedModel | ModelProvider | GatewayProfile
type Tab = ModelManagementTab
type ModelTableTab = Tab

const MODEL_TABLE_COLUMN_MIN_WIDTHS = {
  models: [160, 120, 120, 160, 80, 125],
  connections: [180, 160, 180, 140],
  apim: [180, 120, 160, 80, 48],
  gateways: [180, 120, 160, 80, 48],
} as const

const MODEL_COMPACT_COLUMN_MIN_WIDTHS = [160, 120, 120, 160, 80, 63] as const
const MODEL_CONNECTION_COMPACT_COLUMN_MIN_WIDTHS = [180, 160, 180, 63] as const

const capabilityLabels: Record<string, string> = {
  chat: "对话",
  tools: "工具调用",
  vision: "视觉",
  reasoning: "推理",
  streaming: "流式",
  embeddings: "向量",
}

const compactNumber = { format: (value: number) => new Intl.NumberFormat(getIntlLocale(), { notation: "compact", maximumFractionDigits: 1 }).format(value) }
const runtimeCurrency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 4 })

function runtimePayload(runtime: ModelRuntime, patch: Partial<ModelRuntime> = {}) {
  const value = { ...runtime, ...patch }
  return {
    provider_id: value.provider_id,
    gateway_profile_id: value.gateway_profile_id,
    name: value.name,
    runtime_kind: value.runtime_kind,
    brand_key: value.brand_key,
    enabled: value.enabled,
    is_default: value.is_default,
    config: value.config,
    allowed_roles: value.allowed_roles,
    price_discount_percent: value.price_discount_percent ?? null,
  }
}

function gatewayPayload(gateway: GatewayProfile, patch: Partial<GatewayProfile> = {}) {
  const value = { ...gateway, ...patch }
  return {
    name: value.name,
    implementation: value.implementation,
    base_url: value.base_url,
    auth_type: value.auth_type,
    enabled: value.enabled,
    is_default: value.is_default,
    config: value.config,
  }
}

function modelManagementTabFromUrl(): Tab {
  const tab = new URL(window.location.href).searchParams.get("tab")
  return tab === "connections" || tab === "apim" || tab === "gateways"
    ? tab
    : "models"
}

export function ModelManagementPage({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const client = useQueryClient()
  const { user } = useAuth()
  const { data, isLoading, error } = useQuery({
    ...finopsQueries.registry(),
    refetchInterval: query => query.state.data?.runtimes.some(runtime => runtimeHealthState(runtime) === "checking") ? 2_000 : false,
  })
  const availability = useQuery({ ...finopsQueries.modelAvailability(), enabled: Boolean(user) })
  const [availabilityModelId, setAvailabilityModelId] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>(modelManagementTabFromUrl)
  const [editor, setEditor] = useState<{ kind: RegistryKind; item?: RegistryItem } | null>(null)
  const [consoleOpen, setConsoleOpen] = useState(false)
  const [publicationOpen, setPublicationOpen] = useState(() => Boolean(publicationIdFromUrl()))
  const [publicationId, setPublicationId] = useState<string | null>(publicationIdFromUrl)
  const [publicationDialogId, setPublicationDialogId] = useState<string | null>(publicationIdFromUrl)
  const [publicationAutoClose, setPublicationAutoClose] = useState(true)
  const [connectionOpen, setConnectionOpen] = useState(false)
  const [editingConnection, setEditingConnection] = useState<ModelRuntime | null>(null)
  const [diagnosticRuntime, setDiagnosticRuntime] = useState<ModelRuntime | null>(null)
  const [diagnosticRequestId, setDiagnosticRequestId] = useState<string | null>(null)
  const [diagnosticGatewayDeploymentId, setDiagnosticGatewayDeploymentId] = useState<string | null>(null)
  const [, refreshHealthAge] = useState(0)
  useEffect(() => {
    const timer = window.setInterval(() => refreshHealthAge(value => value + 1), 30_000)
    return () => window.clearInterval(timer)
  }, [])
  const [credentialModel, setCredentialModel] = useState<ManagedModel | null>(null)
  const [deletingModel, setDeletingModel] = useState<ManagedModel | null>(null)
  const [archivingPublication, setArchivingPublication] = useState<GatewayPublication | null>(null)
  const [deletingConnection, setDeletingConnection] = useState<ModelRuntime | null>(null)
  const [deletingGateway, setDeletingGateway] = useState<GatewayProfile | null>(null)
  const targetTopologies = useQuery(finopsQueries.apimTargetTopologies())
  const publications = useQuery({ ...finopsQueries.gatewayPublications(), enabled: user?.role === "owner" })
  const trackedPublication = useQuery({ ...finopsQueries.gatewayPublication(publicationId), enabled: user?.role === "owner" && Boolean(publicationId) })
  // The outcome travels with the text. It used to be a bare string rendered by a banner
  // that was hardcoded green with a check mark, so a failed inference check announced
  // itself as a success -- the words said "failed" and everything around them said "ok".
  const [notice, setNotice] = useState<Notice | null>(null)
  const [syncResult, setSyncResult] = useState<PriceSyncResponse | null>(null)
  const syncSummary = syncResult ? pricingSyncSummary(syncResult) : ""
  const syncPrices = useMutation({
    mutationFn: () => dataSource.syncPrices(),
    onSuccess: result => {
      client.setQueryData(finopsKeys.registry, result.registry)
      void client.invalidateQueries({ queryKey: finopsKeys.all })
      setSyncResult(result)
    },
    onError: failure => setNotice({ text: String(failure), ok: false }),
  })
  const [runtimeDetailId, setRuntimeDetailId] = useState(() => new URL(window.location.href).searchParams.get("runtime"))
  const [modelDetailId, setModelDetailId] = useState(() => new URL(window.location.href).searchParams.get("model"))
  const runtimeHealthWindow = useMemo(() => usageWindow(30), [])
  // One scoped page per runtime matches the detail view. Filtering the global 200-row page
  // would hide older probes for busy runtimes and recreate the list/detail disagreement.
  const runtimeHealthQueries = useQueries({
    queries: (data?.runtimes ?? []).map((runtime) => ({
      ...finopsQueries.requests({ ...runtimeHealthWindow, runtime: [runtime.name] }),
      enabled: tab === "connections" && !runtimeDetailId && runtime.enabled,
    })),
  })

  const showPublication = (id: string) => {
    const status = trackedPublication.data?.id === id
      ? trackedPublication.data.status
      : publications.data?.items.find(publication => publication.id === id)?.status
    setPublicationAutoClose(status !== "active")
    setPublicationId(id)
    setPublicationDialogId(id)
    setPublicationOpen(true)
    replacePublicationUrl(id)
  }

  const trackPublication = (id: string) => {
    setPublicationAutoClose(true)
    setPublicationId(id)
    setPublicationDialogId(id)
    setPublicationOpen(true)
    replacePublicationUrl(id)
  }

  const closePublication = () => {
    setPublicationDialogId(null)
    setPublicationOpen(false)
    replacePublicationUrl(null)
  }

  const openNewPublication = () => {
    setPublicationAutoClose(true)
    setPublicationDialogId(null)
    setPublicationOpen(true)
    replacePublicationUrl(null)
  }

  useEffect(() => {
    const syncRoute = () => {
      setRuntimeDetailId(new URL(window.location.href).searchParams.get("runtime"))
      setModelDetailId(new URL(window.location.href).searchParams.get("model"))
      setTab(modelManagementTabFromUrl())
    }
    window.addEventListener("popstate", syncRoute)
    window.addEventListener(FINOPS_NAVIGATE_EVENT, syncRoute)
    return () => {
      window.removeEventListener("popstate", syncRoute)
      window.removeEventListener(FINOPS_NAVIGATE_EVENT, syncRoute)
    }
  }, [])

  useEffect(() => {
    if (publicationId) return
    const recoverable = publications.data?.items.find((publication) =>
      !["active", "failed", "rolled_back", "superseded"].includes(publication.status),
    )
    if (recoverable) setPublicationId(recoverable.id)
  }, [publicationId, publications.data?.items])

  useEffect(() => {
    const publication = trackedPublication.data
    if (publication?.status === "active") {
      if (publication.publication_kind === "model_remove") {
        void client.cancelQueries({ queryKey: finopsKeys.registry })
        client.setQueryData<ModelRegistry>(finopsKeys.registry, current => current
          ? { ...current, models: current.models.filter(model => model.model_key !== publication.model_key) }
          : current)
      }
      void client.invalidateQueries({ queryKey: finopsKeys.registry })
      void client.invalidateQueries({ queryKey: finopsKeys.gatewayPublications })
      if (publication.publication_kind === "model_remove") {
        void client.invalidateQueries({ queryKey: ["finops", "budgets"] })
        void client.invalidateQueries({ queryKey: assistantSettingsKey })
      }
      setNotice(publication.publication_kind === "model_remove"
        ? {
            context: publication.display_name,
            text: "已从 Turnstile 和 APIM 移除",
            actionLabel: "查看详情",
            ok: true,
          }
        : {
            context: publication.display_name,
            text: publication.publication_kind === "credential_rotation"
              ? "凭据已更新"
              : "模型已发布，分配人员后即可使用",
            actionLabel: "查看详情",
            ok: true,
          })
    } else if (publication?.status === "failed") {
      setNotice(publication.publication_kind === "model_remove"
        ? { context: publication.display_name, text: "移除未完成，生效状态未确认。", actionLabel: "查看详情", ok: false }
        : {
            context: publication.display_name,
            text: "发布失败",
            detail: publication.failure?.message ?? "发布未完成，生效状态未确认。",
            actionLabel: "重新发布",
            ok: false,
          })
    } else if (publication) {
      setNotice(null)
    }
  }, [client, trackedPublication.data?.display_name, trackedPublication.data?.model_key, trackedPublication.data?.publication_kind, trackedPublication.data?.status, trackedPublication.data?.failure?.message])

  const save = useMutation({
    mutationFn: async ({ kind, id, value }: { kind: RegistryKind; id?: string; value: Record<string, unknown> }) => {
      if (kind === "runtime") return dataSource.saveRuntime(value, id)
      if (kind === "model") return dataSource.saveModel(value, id)
      if (kind === "provider") return dataSource.saveProvider(value, id)
      return dataSource.saveGateway(value, id)
    },
    onSuccess: async (registry) => {
      await storeRegistry(client, registry)
      setEditor(null)
      setNotice({ text: "配置已保存", ok: true })
    },
  })
  const health = useRuntimeDiagnostics({
    supported: data?.runtime_diagnostics_supported === true,
    onSuccess: (result, request) => {
      setNotice({ context: request.runtimeName, text: result.outcome === "passed" ? "所选模型检查通过" : "检查未确认连接可用", ok: result.outcome === "passed" })
    },
    onError: (error, request) => {
      setNotice({ context: request.runtimeName, text: "检查未确认连接可用", detail: errorMessage(error), ok: false })
    },
  })
  const saveConnection = useMutation({
    mutationFn: dataSource.saveConnection,
    onSuccess: async (registry) => {
      await storeRegistry(client, registry)
      setConnectionOpen(false)
      setEditingConnection(null)
      setNotice({ text: "连接已添加", detail: "添加首个模型后将通过候选 Revision 验证", ok: true })
    },
  })
  const adoptConnection = useMutation({
    mutationFn: ({ runtime, workspaceUrl }: { runtime: ModelRuntime; workspaceUrl: string }) => dataSource.adoptDatabricksConnection(runtime.id, workspaceUrl),
    onSuccess: (accepted) => {
      client.setQueryData(finopsKeys.gatewayPublication(accepted.publication.id), accepted.publication)
      setConnectionOpen(false)
      setEditingConnection(null)
      trackPublication(accepted.publication.id)
      void client.invalidateQueries({ queryKey: finopsKeys.gatewayPublications })
    },
  })
  const updateConnection = useMutation({
    mutationFn: ({ runtime, value }: { runtime: ModelRuntime; value: ModelConnectionUpdate }) => dataSource.updateConnection(runtime.id, value),
    onSuccess: async (registry) => {
      await storeRegistry(client, registry)
      setConnectionOpen(false)
      setEditingConnection(null)
      setNotice({ text: "连接已更新", ok: true })
    },
  })
  const deleteConnection = useMutation({
    mutationFn: (runtime: ModelRuntime) => dataSource.deleteConnection(runtime.id),
    onSuccess: async (registry, runtime) => {
      await storeRegistry(client, registry)
      setDeletingConnection(null)
      setNotice({ context: runtime.name, text: "连接已删除", ok: true })
    },
  })
  const deleteGateway = useMutation({
    mutationFn: (gateway: GatewayProfile) => dataSource.deleteGateway(gateway.id),
    onSuccess: async (registry, gateway) => {
      await storeRegistry(client, registry)
      setDeletingGateway(null)
      setNotice({ context: gateway.name, text: "网关记录已删除", ok: true })
    },
  })
  const rotationCredential = useRef("")
  const rotateCredential = useMutation({
    retry: false,
    mutationFn: (model: ManagedModel) => {
      const credential = rotationCredential.current
      rotationCredential.current = ""
      const runtime = data?.runtimes.find((item) => item.id === model.runtime_id)
      if (!runtime?.gateway_profile_id) throw new Error("模型没有可发布的 APIM 网关")
      if (!credential) throw new Error("请输入新的凭据。")
      return dataSource.rotateGatewayCredential(
        runtime.gateway_profile_id,
        model.model_key,
        credential,
        runtime.config.auth_strategy === "oauth_client_credentials" ? "oauth_m2m" : "api_key",
      )
    },
    onSuccess: (publication) => {
      setCredentialModel(null)
      showPublication(publication.id)
      client.setQueryData(finopsKeys.gatewayPublication(publication.id), publication)
      void client.invalidateQueries({ queryKey: finopsKeys.gatewayPublications })
    },
  })
  const archivePublication = useMutation({
    retry: false,
    mutationFn: (publication: GatewayPublication) => dataSource.archiveFailedPublication(publication.id),
    onSuccess: async (_result, publication) => {
      await client.cancelQueries({ queryKey: finopsKeys.gatewayPublications })
      client.setQueriesData<GatewayPublicationList>({ queryKey: finopsKeys.gatewayPublications }, current => current
        ? { ...current, items: current.items.filter(item => item.id !== publication.id) }
        : current)
      setArchivingPublication(null)
      if (publicationId === publication.id) {
        setPublicationId(null)
        closePublication()
      }
      setNotice({ text: "失败记录已从模型列表移除", ok: true })
      client.removeQueries({ queryKey: finopsKeys.gatewayPublication(publication.id), exact: true })
      void client.invalidateQueries({ queryKey: finopsKeys.gatewayPublications })
    },
  })
  const deleteModel = useMutation({
    mutationFn: (model: ManagedModel) => dataSource.deleteModel(model.id),
    onSuccess: (accepted) => {
      setDeletingModel(null)
      setPublicationId(accepted.publication.id)
      setPublicationDialogId(null)
      setPublicationOpen(false)
      replacePublicationUrl(null)
      client.setQueryData(
        finopsKeys.gatewayPublication(accepted.publication.id),
        accepted.publication,
      )
      void client.invalidateQueries({ queryKey: finopsKeys.gatewayPublications })
    },
  })

  if (isLoading) return <div className="registry-loading"><RefreshCw className="spin" />加载模型目录</div>
  if (error || !data) return <div className="registry-error">无法加载模型管理 API：{String(error)}</div>

  const effectiveRegistry = {
    ...data,
    runtimes: data.runtimes.map((runtime, index) => runtimeWithTelemetryHealth(
      runtime,
      runtimeHealthQueries[index]?.data ?? [],
    )),
  }
  const checkingRuntimeIds = new Set([
    ...health.attempts.filter(attempt => attempt.status === "pending").map(attempt => attempt.request.runtimeId),
    ...data.runtimes.filter(runtime => runtimeHealthState(runtime) === "checking").map(runtime => runtime.id),
  ])
  const diagnosticAttempt = health.attempts.find(attempt => attempt.request.id === diagnosticRequestId)
  const openRuntimeDiagnostic = (runtime: ModelRuntime, preferredGatewayDeploymentId?: string) => {
    const pending = health.attempts.find(attempt => attempt.status === "pending" && attempt.request.runtimeId === runtime.id)
    setDiagnosticRequestId(pending?.request.id ?? null)
    setDiagnosticGatewayDeploymentId(pending?.request.selection.gateway_deployment_id ?? preferredGatewayDeploymentId ?? null)
    setDiagnosticRuntime(runtime)
  }

  const updateRuntime = (runtime: ModelRuntime, patch: Partial<ModelConnectionUpdate>) =>
    updateConnection.mutate({ runtime, value: {
      name: patch.name ?? runtime.name,
      enabled: patch.enabled ?? runtime.enabled,
      is_default: patch.is_default ?? runtime.is_default,
      price_discount_percent: patch.price_discount_percent === undefined
        ? runtime.price_discount_percent ?? null : patch.price_discount_percent,
    } })
  const updateGateway = (gateway: GatewayProfile, patch: Partial<GatewayProfile>) =>
    save.mutate({ kind: "gateway", id: gateway.id, value: gatewayPayload(gateway, patch) })
  const removingModelKey = trackedPublication.data?.publication_kind === "model_remove"
    && !["active", "failed", "rolled_back", "superseded"].includes(trackedPublication.data.status)
    ? trackedPublication.data.model_key
    : null
  const modelPublications = trackedPublication.data
    ? [trackedPublication.data, ...(publications.data?.items ?? []).filter((item) => item.id !== trackedPublication.data?.id)]
    : publications.data?.items ?? []
  const openPublicationRetry = () => {
    if (!publicationId) return
    showPublication(publicationId)
  }

  const changeTab = (next: Tab) => {
    setTab(next)
    setModelDetailId(null)
    const url = new URL(window.location.href)
    url.searchParams.set("page", "models")
    url.searchParams.set("tab", next)
    url.searchParams.delete("runtime")
    url.searchParams.delete("model")
    window.history.replaceState(null, "", url)
  }

  const editingModel = editor?.kind === "model" ? editor.item as ManagedModel | undefined : undefined
  const availabilityByModel = new Map((availability.error ? [] : availability.data?.items ?? []).map((item) => [item.model_id, item]))
  const availabilityModel = data.models.find((model) => model.id === availabilityModelId)
  const overlays = <>
    {diagnosticRuntime && <RuntimeDiagnosticDialog
      key={diagnosticRuntime.id}
      runtime={data.runtimes.find(runtime => runtime.id === diagnosticRuntime.id) ?? diagnosticRuntime}
      supported={data.runtime_diagnostics_supported === true}
      busy={checkingRuntimeIds.has(diagnosticRuntime.id)}
      error={diagnosticAttempt?.error ? errorMessage(diagnosticAttempt.error) : null}
      result={diagnosticAttempt?.result ?? null}
      initialSelection={diagnosticAttempt?.request.selection}
      preferredGatewayDeploymentId={diagnosticGatewayDeploymentId}
      onCheck={selection => {
        setDiagnosticGatewayDeploymentId(selection.gateway_deployment_id)
        setDiagnosticRequestId(health.start(diagnosticRuntime, selection)?.id ?? null)
      }}
      onClose={() => { setDiagnosticRuntime(null); setDiagnosticRequestId(null); setDiagnosticGatewayDeploymentId(null) }}
    />}
    {availabilityModel && <ModelAvailabilityDialog model={availabilityModel} registry={data} readback={availabilityByModel.get(availabilityModel.id)} loading={availability.isPending} readError={Boolean(availability.error)} onRefresh={() => { void availability.refetch() }} onClose={() => setAvailabilityModelId(null)} />}
    {editingModel && <ModelEditDialog
      key={editingModel.id}
      registry={data}
      model={editingModel}
      availability={availabilityByModel.get(editingModel.id)}
      availabilityLoading={availability.isPending}
      busy={save.isPending}
      error={save.variables?.kind === "model" && save.variables.id === editingModel.id && save.error ? errorMessage(save.error) : null}
      onClose={() => { save.reset(); setEditor(null) }}
      onSave={(value) => save.mutate({ kind: "model", id: editingModel.id, value })}
    />}
    {editor && editor.kind !== "model" && <RegistryEditor registry={data} kind={editor.kind} item={editor.item} busy={save.isPending} onClose={() => setEditor(null)} onSave={(value) => save.mutate({ kind: editor.kind, id: editor.item?.id, value })} />}
    {consoleOpen && <GatewayConsole registry={data} onClose={() => setConsoleOpen(false)} />}
    {user?.role === "owner" && publicationOpen && <ModelPublicationDialog registry={data} topology={targetTopologies.data ?? null} topologyAvailable={data.apim_target_topology_available === true} topologyLoading={targetTopologies.isLoading} topologyError={targetTopologies.error ? String(targetTopologies.error) : null} publicationId={publicationDialogId} autoCloseOnSuccess={publicationAutoClose} onPublicationQueued={(id) => {
      trackPublication(id)
      void client.invalidateQueries({ queryKey: finopsKeys.gatewayPublications })
    }} onClose={closePublication} onManageConnections={() => {
      closePublication()
      setRuntimeDetailId(null)
      changeTab("connections")
    }} />}
    {connectionOpen && <ConnectionDialog registry={data} topology={targetTopologies.data ?? null} topologyLoading={targetTopologies.isLoading} topologyError={targetTopologies.error ? String(targetTopologies.error) : null} runtime={editingConnection ?? undefined} busy={saveConnection.isPending || updateConnection.isPending || adoptConnection.isPending} error={adoptConnection.error ? errorMessage(adoptConnection.error) : (editingConnection ? updateConnection.error : saveConnection.error) ? errorMessage(editingConnection ? updateConnection.error : saveConnection.error) : null} onClose={() => { if (!saveConnection.isPending && !updateConnection.isPending && !adoptConnection.isPending) { setConnectionOpen(false); setEditingConnection(null); adoptConnection.reset() } }} onCreate={(value) => saveConnection.mutate(value)} onUpdate={(value) => { if (editingConnection) updateConnection.mutate({ runtime: editingConnection, value }) }} onAdopt={(workspaceUrl) => { if (editingConnection) adoptConnection.mutate({ runtime: editingConnection, workspaceUrl }) }} />}
    {credentialModel && <CredentialRotationDialog model={credentialModel} oauth={data.runtimes.find((runtime) => runtime.id === credentialModel.runtime_id)?.config.auth_strategy === "oauth_client_credentials"} busy={rotateCredential.isPending} error={rotateCredential.error ? String(rotateCredential.error) : null} onClose={() => { rotationCredential.current = ""; setCredentialModel(null); rotateCredential.reset() }} onSave={(credential) => { rotationCredential.current = credential; rotateCredential.mutate(credentialModel) }} />}
    {deletingModel && <ModelDeleteDialog model={deletingModel} busy={deleteModel.isPending} error={deleteModel.error ? String(deleteModel.error) : null} onClose={() => { if (!deleteModel.isPending) setDeletingModel(null) }} onConfirm={() => deleteModel.mutate(deletingModel)} />}
    {archivingPublication && <PublicationArchiveDialog publication={archivingPublication} busy={archivePublication.isPending} error={archivePublication.error ? errorMessage(archivePublication.error) : null} onClose={() => { if (!archivePublication.isPending) { setArchivingPublication(null); archivePublication.reset() } }} onConfirm={() => archivePublication.mutate(archivingPublication)} />}
    {deletingConnection && <ConnectionDeleteDialog runtime={deletingConnection} busy={deleteConnection.isPending} error={deleteConnection.error ? String(deleteConnection.error) : null} onClose={() => { if (!deleteConnection.isPending) setDeletingConnection(null) }} onConfirm={() => deleteConnection.mutate(deletingConnection)} />}
    {deletingGateway && <GatewayDeleteDialog gateway={deletingGateway} busy={deleteGateway.isPending} error={deleteGateway.error ? String(deleteGateway.error) : null} onClose={() => { if (!deleteGateway.isPending) setDeletingGateway(null) }} onConfirm={() => deleteGateway.mutate(deletingGateway)} />}
  </>

  if (modelDetailId && tab === "models") {
    return <>
      <ModelUsageDetail modelId={modelDetailId} registry={data} onToggleSidebar={onToggleSidebar} />
      {overlays}
    </>
  }

  if (runtimeDetailId && tab === "connections") {
    const detailRuntime = data.runtimes.find((runtime) => runtime.id === runtimeDetailId)
    return <>
      <RuntimeDetailView
        runtime={detailRuntime}
        registry={data}
        busy={save.isPending}
        checking={checkingRuntimeIds.has(runtimeDetailId)}
        onEdit={(runtime) => { setEditingConnection(runtime); setConnectionOpen(true) }}
        onEditModel={(model) => setEditor({ kind: "model", item: model })}
        onCheck={openRuntimeDiagnostic}
        onToggle={(runtime) => updateRuntime(runtime, { enabled: !runtime.enabled })}
        onToggleSidebar={onToggleSidebar}
      />
      {overlays}
    </>
  }

  if ((tab === "apim" || tab === "gateways") && data.apim_target_topology_available === true) {
    return <>
      <ApimGatewayWorkspace
        mode={tab}
        registry={effectiveRegistry}
        topology={targetTopologies.data ?? null}
        loading={targetTopologies.isLoading}
        error={targetTopologies.error}
        onTabChange={changeTab}
        onToggleSidebar={onToggleSidebar}
      />
      {overlays}
    </>
  }

  return <>
    <ModelWorkspace
      registry={effectiveRegistry}
      tab={tab === "apim" ? "gateways" : tab}
      notice={notice}
      error={save.error}
      busy={save.isPending || deleteModel.isPending || deleteConnection.isPending || deleteGateway.isPending || archivePublication.isPending}
      checkingRuntimeIds={checkingRuntimeIds}
      diagnosticGatewayDeploymentId={diagnosticGatewayDeploymentId}
      diagnosticRuntimeId={diagnosticRuntime?.id ?? null}
      removingModelKey={removingModelKey}
      publications={modelPublications}
      topology={targetTopologies.data ?? null}
      topologyAvailable={data.apim_target_topology_available === true}
      availabilityByModel={availabilityByModel}
      availabilityLoading={availability.isPending}
      onOpenAvailability={setAvailabilityModelId}
      onTabChange={changeTab}
      onDismissNotice={() => setNotice(null)}
      onNoticeAction={openPublicationRetry}
      onToggleSidebar={onToggleSidebar}
      onAdd={(kind) => {
        if (kind === "model") {
          openNewPublication()
        } else if (kind === "runtime") {
          setEditingConnection(null)
          setConnectionOpen(true)
        } else {
          setEditor({ kind })
        }
      }}
      onEdit={(kind, item) => {
        if (kind === "runtime") {
          setEditingConnection(item as ModelRuntime)
          setConnectionOpen(true)
        } else {
          setEditor({ kind, item })
        }
      }}
      onRotateCredential={setCredentialModel}
      onDeleteModel={setDeletingModel}
      onOpenPublication={showPublication}
      onArchivePublication={user?.role === "owner" && data.failed_publication_archive_supported
        ? setArchivingPublication : undefined}
      onToggleRuntime={(runtime) => updateRuntime(runtime, { enabled: !runtime.enabled })}
      onCheckRuntime={openRuntimeDiagnostic}
      onDeleteConnection={setDeletingConnection}
      onToggleGateway={(gateway) => updateGateway(gateway, { enabled: !gateway.enabled })}
      onDefaultGateway={(gateway) => updateGateway(gateway, { is_default: true, enabled: true })}
      onDeleteGateway={setDeletingGateway}
      canSyncPrices={user?.role === "owner"}
      syncingPrices={syncPrices.isPending}
      onSyncPrices={() => syncPrices.mutate()}
    />
    {syncResult && <Dialog open onOpenChange={open => { if (!open) setSyncResult(null) }}>
      <DialogContent className="registry-editor-dialog">
        <div className="registry-editor">
          <DialogHeader className="registry-editor-header"><DialogTitle>定价同步结果</DialogTitle>
            <DialogDescription>{syncSummary}</DialogDescription>
          </DialogHeader>
          <div className="registry-editor-body"><div className="price-sync-results">
            {syncResult.details.map(detail => <div className="price-sync-result" key={detail.model_id}>
              <span data-no-localize>{detail.model_key}</span>
              <b>{({ updated: "已更新", unchanged: "无变化", skipped_manual: "手动跳过",
                unmapped: "未匹配", ambiguous: "待确认", stale: "来源失败",
                review_needed: "待复核", superseded: "计划作废",
                unsupported: "不支持", deferred: "未执行" } as Record<string, string>)[detail.outcome ?? ""] ?? detail.outcome}</b>
              {detail.message && <small>{detail.message}</small>}
              {detail.outcome !== "skipped_manual" && <Button type="button" variant="ghost" size="icon"
                title="编辑模型" aria-label="编辑模型" onClick={() => {
                  const model = syncResult.registry.models.find(model => model.id === detail.model_id)
                  setSyncResult(null)
                  if (model) setEditor({ kind: "model", item: model })
                }}><Edit3 size={14} /></Button>}
            </div>)}
          </div></div>
          <DialogFooter className="registry-editor-footer"><DialogClose>关闭</DialogClose></DialogFooter>
        </div>
      </DialogContent>
    </Dialog>}
    {overlays}
  </>
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

function formatRuntimeTimestamp(timestamp: string, timezone: string) {
  return new Date(timestamp).toLocaleString(getIntlLocale(), {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: timezone,
  })
}

function shortRuntimeId(id: string) {
  return id.length <= 10 ? id : `${id.slice(0, 6)}··${id.slice(-2)}`
}

function RuntimeDetailHealth({ state }: { state: RuntimeHealthState | "checking" }) {
  if (state === "checking") return <span className="runtime-health-badge checking"><RefreshCw className="spin" size={12} />正在检查</span>
  const label = runtimeHealthLabels[state]
  return <span className={`runtime-health-badge ${state}`}><i />{label}</span>
}

function RuntimeDetailFact({ label, children, mono = false, compact = false }: {
  label: string
  children: React.ReactNode
  mono?: boolean
  compact?: boolean
}) {
  return <div className={`runtime-detail-fact ${compact ? "compact" : ""}`}><dt>{label}</dt><dd className={mono ? "mono" : ""}>{children}</dd></div>
}

function runtimeMetricLabel(value: string) {
  return { input: "输入", output: "输出", cost: "估算费用" }[value] ?? value
}

function RuntimeDetailView({ runtime, registry, busy, checking, onEdit, onEditModel, onCheck, onToggle, onToggleSidebar }: {
  runtime: ModelRuntime | null | undefined
  registry: ModelRegistry
  busy: boolean
  checking: boolean
  onEdit: (runtime: ModelRuntime) => void
  onEditModel: (model: ManagedModel) => void
  onCheck: (runtime: ModelRuntime) => void
  onToggle: (runtime: ModelRuntime) => void
  onToggleSidebar: () => void
}) {
  const { timezone } = useTimezone()
  const [dimension, setDimension] = useState<"daily" | "weekly">("daily")
  const [days, setDays] = useState<7 | 30 | 90>(30)
  const [metric, setMetric] = useState<"cost" | "token" | "heatmap">("token")
  const { hiddenSeries, toggleSeries } = useSeriesToggle()
  const [showTechnicalDetails, setShowTechnicalDetails] = useState(false)
  // Every query here is scoped to the runtime on the server. Filtering a global page of
  // requests in the browser instead gave "the part of the last 200 calls that happened to
  // belong to this runtime", so the busier the other runtimes were, the shorter the window
  // silently became -- measured at 26 requests against a real 614, over 10 hours of a
  // 30-day label. The error direction is systematic under-reporting with no visible cue.
  const runtimeName = runtime?.name
  const usageFilters = useMemo(
    () => ({ ...usageWindow(days), runtime: runtimeName ? [runtimeName] : undefined }),
    [days, runtimeName],
  )
  const heatmapFilters = useMemo(
    () => ({ ...usageWindow(182), runtime: runtimeName ? [runtimeName] : undefined }),
    [runtimeName],
  )
  const overview = useQuery({ ...finopsQueries.executiveOverview(usageFilters), enabled: !!runtime })
  const activity = useQuery({
    ...finopsQueries.usageActivity(usageFilters, dimension === "daily" ? "day" : "week", timezone),
    enabled: !!runtime,
  })
  const agentUsage = useQuery({ ...finopsQueries.distribution(usageFilters, "agent"), enabled: !!runtime })
  // Row-level reads that no aggregate can answer: the newest health probe and the latest
  // call timestamp. Now that the query carries the runtime, all 200 rows are this runtime's.
  const usage = useQuery({ ...finopsQueries.requests(usageFilters), enabled: !!runtime })
  const heatmapActivity = useQuery({
    ...finopsQueries.usageActivity(heatmapFilters, "day", timezone),
    enabled: !!runtime && metric === "heatmap",
  })
  if (!runtime) return <div className="runtime-detail-workspace"><header className="runtime-detail-breadcrumb"><Button type="button" variant="ghost" size="icon-sm" className="model-mobile-sidebar-toggle" aria-label="切换导航栏" title="切换导航栏" onClick={onToggleSidebar}><PanelLeft size={16} /></Button><a href={runtimeRouteHref(null)} onClick={openRuntimeRoute(null)}>连接</a><ChevronRight size={13} /><span className="breadcrumb-note">未找到</span></header><div className="smh-empty"><Server size={28} /><b>连接不存在</b><a className="ghost-button" href={runtimeRouteHref(null)} onClick={openRuntimeRoute(null)}>返回连接</a></div></div>

  const models = registry.models.filter((model) => model.runtime_id === runtime.id)
  const enabledModelCount = models.filter((model) => model.enabled).length
  const missingPrices = models.filter(model => !hasModelPrices(model))
  const runtimeRequests = usage.data ?? []
  // The distribution is ranked by tokens, but this card counts calls, so it has to be
  // re-sorted on the field it shows -- otherwise the busiest caller lands mid-list. The
  // header still reports the full count, so the cut is visible rather than silent.
  const agentTotal = agentUsage.data?.items.length ?? 0
  const agents = (agentUsage.data?.items ?? [])
    .map((item) => ({ id: item.id, name: item.name, calls: item.total_requests }))
    .sort((left, right) => right.calls - left.calls)
    .slice(0, 10)
  const effectiveRuntime = runtimeWithTelemetryHealth(runtime, runtimeRequests)
  const latestRequestTimestamp = runtimeRequests.reduce<string | null>(
    (latest, request) => !latest || request.timestamp > latest ? request.timestamp : latest,
    null,
  )
  // Totals come from the server's aggregate over the whole window, not from the capped
  // request page, so they cannot understate a busy runtime.
  const totals = overview.data?.totals
  const chartData = aggregateActivity(activity.data?.points ?? [], dimension === "daily" ? "day" : "week", timezone)
  const heatmapDays = aggregateActivity(heatmapActivity.data?.points ?? [], "day", timezone)
    .map((row) => ({ date: row.date, value: row.total }))
  const costLabel = missingPrices.length > 0 ? "未计价"
    : (totals?.total_tokens ?? 0) > 0 && (totals?.estimated_cost ?? 0) === 0 ? "待回算"
    : runtimeCurrency.format(totals?.estimated_cost ?? 0)
  const healthState = checking ? "checking" : runtimeHealthState(effectiveRuntime)

  return <div className="runtime-detail-workspace">
    <header className="runtime-detail-breadcrumb">
      <Button type="button" variant="ghost" size="icon-sm" className="model-mobile-sidebar-toggle" aria-label="切换导航栏" title="切换导航栏" onClick={onToggleSidebar}><PanelLeft size={16} /></Button><a href={runtimeRouteHref(null)} onClick={openRuntimeRoute(null)}>连接</a><ChevronRight size={13} /><code title={runtime.name}>{runtime.name}</code>
    </header>
    <div className="runtime-detail-scroll">
      <div className="runtime-detail-grid">
        <div className="runtime-detail-main">
          <section className="runtime-detail-hero">
            <div className="runtime-detail-identity">
              <span className="runtime-detail-logo"><RuntimeLogo runtime={runtime} size={20} /></span>
              <div><h2 title={runtime.name}>{runtime.name}</h2><RuntimeDetailHealth state={healthState} /><small>{usage.isLoading ? "正在加载调用记录" : latestRequestTimestamp ? `最近调用 ${formatRuntimeTimestamp(latestRequestTimestamp, timezone)}` : "所选范围暂无调用"}</small></div>
            </div>
            <dl className="runtime-detail-facts">
              <RuntimeDetailFact label="提供方">{runtime.provider_name}</RuntimeDetailFact>
              <RuntimeDetailFact label="网关">{runtime.gateway_name ?? "—"}</RuntimeDetailFact>
              <RuntimeDetailFact label="启用模型">{enabledModelCount}</RuntimeDetailFact>
            </dl>
            <div className="runtime-technical-details">
              <button type="button" aria-expanded={showTechnicalDetails} onClick={() => setShowTechnicalDetails((current) => !current)}><ChevronRight size={12} />技术详情</button>
              {showTechnicalDetails && <dl>
                <RuntimeDetailFact label="RUNTIME ID" mono compact>{shortRuntimeId(runtime.id)}</RuntimeDetailFact>
              </dl>}
            </div>
          </section>

          <div className="runtime-detail-controls">
            <div><span>维度</span><div><button className={dimension === "daily" ? "active" : ""} onClick={() => setDimension("daily")}>按天</button><button className={dimension === "weekly" ? "active" : ""} onClick={() => setDimension("weekly")}>按周</button></div></div>
            <div><span>时间范围</span><div>{([7, 30, 90] as const).map((value) => <button key={value} className={days === value ? "active" : ""} onClick={() => setDays(value)}>{value}d</button>)}</div></div>
          </div>

          {missingPrices.length > 0 && <div className="runtime-price-warning"><span>!</span><div>{`有 ${missingPrices.length} 个模型没有维护价格，相关 token 未计入费用统计。`}<code>{missingPrices.map((model) => model.model_key).join("、")}</code></div><button onClick={() => onEditModel(missingPrices[0])}>设置自定义价格</button></div>}

          <section className="runtime-detail-kpis">
            <div><span>估算费用 · {days} 天</span><strong>{overview.isLoading ? "—" : costLabel}</strong></div>
            <div><span>请求 · {days} 天</span><strong>{overview.isLoading ? "—" : compactNumber.format(totals?.total_requests ?? 0)}</strong><small>成功率 {((100 - (totals?.error_rate ?? 0))).toFixed(1)}%</small></div>
            <div><span>TOKEN · {days} 天</span><strong>{overview.isLoading ? "—" : compactNumber.format(totals?.total_tokens ?? 0)}</strong><small>平均时延 {Math.round(totals?.average_latency_ms ?? 0).toLocaleString(getIntlLocale())} ms</small></div>
          </section>

          <section className="runtime-detail-chart">
            <div className="runtime-detail-chart-head">
              <div className="runtime-detail-chart-title">
                <h3>这个运行时在何时消耗</h3>
                <div className="runtime-detail-chart-metric-toggle" role="group" aria-label="图表指标"><button className={metric === "cost" ? "active" : ""} onClick={() => setMetric("cost")}>估算费用</button><button className={metric === "token" ? "active" : ""} onClick={() => setMetric("token")}>Token</button><button className={metric === "heatmap" ? "active" : ""} onClick={() => setMetric("heatmap")}>热力图</button></div>
              </div>
              {metric === "token" && <ChartSeriesLegend
                className="runtime-chart-legend"
                items={[{ key: "input", label: "输入" }, { key: "output", label: "输出" }]}
                hiddenSeries={hiddenSeries}
                onToggle={toggleSeries}
              />}
            </div>
            {metric === "heatmap" ? heatmapActivity.isLoading ? <div className="runtime-chart-state">正在加载用量数据</div>
              : heatmapActivity.error ? <div className="runtime-chart-state">用量数据加载失败</div>
              : <div className="runtime-detail-heatmap"><p className="usage-heatmap-caption">最近 26 周 · 每日 Token 强度（此处忽略上方时间范围）</p><ActivityHeatmap days={heatmapDays} metric="tokens" timezone={timezone} /></div>
              : activity.isLoading ? <div className="runtime-chart-state">正在加载用量数据</div>
              : activity.error ? <div className="runtime-chart-state">用量数据加载失败</div>
              : chartData.length === 0 ? <div className="runtime-chart-state">当前运行时在所选范围内暂无用量</div>
              : <div className="runtime-chart-canvas finops-chart-surface">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData} margin={{ left: 8, right: 8, top: 8, bottom: 0 }}>
                    <CartesianGrid vertical={false} stroke="var(--border)" />
                    <XAxis dataKey="label" axisLine={false} tickLine={false} tickMargin={8} />
                    <YAxis axisLine={false} tickLine={false} tickMargin={8} width={56} tickFormatter={(value) => metric === "cost" ? `$${Number(value).toFixed(2)}` : compactNumber.format(Number(value))} />
                    <Tooltip cursor={false} content={<FinOpsChartTooltip nameFormatter={runtimeMetricLabel} valueFormatter={(value) => metric === "cost" ? runtimeCurrency.format(Number(value)) : `${Number(value).toLocaleString(getIntlLocale())} tokens`} />} />
                    {metric === "cost" ? <Bar dataKey="cost" fill="var(--chart-1)" radius={[3, 3, 0, 0]} isAnimationActive={false} /> : <>
                      {!hiddenSeries.includes("input") && <Bar dataKey="input" stackId="tokens" fill="var(--chart-4)" radius={hiddenSeries.includes("output") ? [3, 3, 0, 0] : 0} isAnimationActive={false} />}
                      {!hiddenSeries.includes("output") && <Bar dataKey="output" stackId="tokens" fill="var(--chart-2)" radius={[3, 3, 0, 0]} isAnimationActive={false} />}
                    </>}
                  </BarChart>
                </ResponsiveContainer>
              </div>}
          </section>
        </div>

        <aside className="runtime-detail-rail">
          <section className="runtime-detail-sidecard serving-agents-card">
            <header><b>智能体用量</b><span>{days} 天 · {agentTotal} 个</span></header>
            {agents.length ? <div>{agents.map((agent) => <div className="serving-agent-row" key={agent.id}><span className="detail-agent-icon"><Bot size={12} /></span><span><b>{agent.name}</b><small><i />{agent.calls} 次调用</small></span></div>)}</div> : <p>所选时间范围内暂无智能体调用</p>}
          </section>
          <section className="runtime-detail-sidecard diagnostics-card">
            <header><b>诊断</b></header>
            <div className="diagnostics-content">
              <label>最近诊断</label><RuntimeDetailHealth state={checking ? "checking" : healthState} />
              <div className="diagnostic-divider" />
              <label>最近检查</label><p>{effectiveRuntime.last_checked_at ? formatRuntimeTimestamp(effectiveRuntime.last_checked_at, timezone) : "尚未检查"}</p>
              {effectiveRuntime.health_message && <><label>检查结果</label><p>{runtimeHealthDescription(effectiveRuntime)}</p></>}
              {runtimeDiagnostic(effectiveRuntime)?.correlation_id && <><label>Correlation ID</label><p data-no-localize>{runtimeDiagnostic(effectiveRuntime)?.correlation_id}</p></>}
              <div className="diagnostic-divider" />
              <button className="diagnostic-action" title="选择模型和 Gateway 后确认检查" disabled={busy} onClick={() => onCheck(runtime)}><RefreshCw className={checking ? "spin" : ""} size={14} />{checking ? "正在检查" : "连接诊断"}</button>
              <button className="diagnostic-action" disabled={busy || checking} onClick={() => onEdit(runtime)}><Edit3 size={14} />编辑连接</button>
              <button className="diagnostic-action destructive" disabled={busy || checking} onClick={() => onToggle(runtime)}><MoreHorizontal size={14} />{runtime.enabled ? "停用连接" : "启用连接"}</button>
            </div>
          </section>
        </aside>
      </div>
    </div>
  </div>
}

function RuntimeLogo({ runtime, size = 20 }: { runtime: ModelRuntime; size?: number }) {
  if (runtime.runtime_kind === "openai_compatible" && runtime.config.api_format === "openai_chat") {
    return <ModelVendorLogo value={modelVendorFromMetadata(
      runtime.config,
      `${runtime.name} ${runtime.provider_name ?? ""}`,
    )} size={size} />
  }
  const brand = providerBrandFromMetadata(
    runtime.brand_key,
    `${runtime.name} ${runtime.provider_name ?? ""} ${runtime.runtime_kind}`,
  )
  if (brand !== "generic") return <ProviderBrandLogo brand={brand} size={size} />
  return <Cpu size={size} />
}

function gatewayBrand(gateway: GatewayProfile): GatewayBrand {
  return gatewayBrandFromIdentity(`${gateway.name} ${gateway.implementation}`)
}

function RuntimeHealth({ state, checkedAt }: { state: RuntimeHealthState; checkedAt?: string }) {
  const { locale } = useLocale()
  if (state === "checking") return <span className="machine-health checking" role="status"><RefreshCw className="spin" size={13} />正在检查</span>
  const label = runtimeHealthLabels[state]
  const icon = state === "online" ? <Check size={13} />
    : ["offline", "blocked", "configuration", "changed"].includes(state) ? <TriangleAlert size={13} />
    : <CircleDashed size={13} />
  return <span className="runtime-health-summary"><span className={`machine-health ${state}`}>{icon}<span>{label}</span></span>
    {checkedAt && Number.isFinite(Date.parse(checkedAt)) && <time dateTime={checkedAt} title={checkedAt} data-no-localize>{formatTimeAgo(checkedAt, locale)}</time>}
  </span>
}

function ModelDeleteDialog({ model, busy, error, onClose, onConfirm }: {
  model: ManagedModel
  busy: boolean
  error: string | null
  onClose: () => void
  onConfirm: () => void
}) {
  return <AlertDialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <AlertDialogContent className="model-delete-dialog" onClick={(event) => event.stopPropagation()}>
      <div className="model-delete-body">
        <AlertDialogTitle>删除模型</AlertDialogTitle>
        <AlertDialogDescription>{`将从 Turnstile 删除“${model.display_name}”。历史用量和上游模型会保留。`}</AlertDialogDescription>
        {error && <div className="registry-error">{error}</div>}
      </div>
      <AlertDialogFooter className="model-delete-footer">
        <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
        <AlertDialogAction variant="destructive" disabled={busy} aria-busy={busy} onClick={onConfirm}>{busy && <RefreshCw className="spin" size={14} aria-hidden="true" />}{busy ? "正在提交" : "删除模型"}</AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>
}

function PublicationArchiveDialog({ publication, busy, error, onClose, onConfirm }: {
  publication: GatewayPublication
  busy: boolean
  error: string | null
  onClose: () => void
  onConfirm: () => void
}) {
  return <AlertDialog open onOpenChange={open => { if (!open && !busy) onClose() }}>
    <AlertDialogContent className="model-delete-dialog publication-archive-dialog">
      <div className="model-delete-body">
        <AlertDialogTitle>移除失败记录</AlertDialogTitle>
        <AlertDialogDescription>从模型列表移除这条失败记录？发布审计和计费记录会保留，不影响已发布模型。</AlertDialogDescription>
        <p data-no-localize>{publication.display_name}</p>
        {error && <div className="registry-error" role="alert">{error}</div>}
      </div>
      <AlertDialogFooter className="model-delete-footer">
        <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
        <AlertDialogAction variant="destructive" disabled={busy} aria-busy={busy} onClick={onConfirm}>{busy && <RefreshCw className="spin" size={14} aria-hidden="true" />}{busy ? "正在提交" : "移除失败记录"}</AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>
}

function ConnectionDeleteDialog({ runtime, busy, error, onClose, onConfirm }: {
  runtime: ModelRuntime
  busy: boolean
  error: string | null
  onClose: () => void
  onConfirm: () => void
}) {
  return <AlertDialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <AlertDialogContent className="model-delete-dialog" onClick={(event) => event.stopPropagation()}>
      <div className="model-delete-body">
        <AlertDialogTitle>删除连接</AlertDialogTitle>
        <AlertDialogDescription>{`将从 Turnstile 删除“${runtime.name}”。历史用量、publication 和旧 APIM 资源会保留。`}</AlertDialogDescription>
        {error && <div className="registry-error">{error}</div>}
      </div>
      <AlertDialogFooter className="model-delete-footer">
        <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
        <AlertDialogAction variant="destructive" disabled={busy} onClick={onConfirm}>{busy ? "正在提交" : "删除连接"}</AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>
}

function GatewayDeleteDialog({ gateway, busy, error, onClose, onConfirm }: {
  gateway: GatewayProfile
  busy: boolean
  error: string | null
  onClose: () => void
  onConfirm: () => void
}) {
  return <AlertDialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <AlertDialogContent className="model-delete-dialog" onClick={(event) => event.stopPropagation()}>
      <div className="model-delete-body">
        <AlertDialogTitle>删除网关记录</AlertDialogTitle>
        <AlertDialogDescription>{`将从 Turnstile Registry 删除“${gateway.name}”。关联的 APIM 云资源必须单独清理。`}</AlertDialogDescription>
        {error && <div className="registry-error">{error}</div>}
      </div>
      <AlertDialogFooter className="model-delete-footer">
        <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
        <AlertDialogAction variant="destructive" disabled={busy} onClick={onConfirm}>{busy ? "正在删除" : "删除网关记录"}</AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>
}

function CredentialRotationDialog({ model, oauth = false, busy, error, onClose, onSave }: {
  model: ManagedModel
  oauth?: boolean
  busy: boolean
  error: string | null
  onClose: () => void
  onSave: (apiKey: string) => void
}) {
  const [apiKey, setApiKey] = useState("")
  const [revealed, setRevealed] = useState(false)
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <DialogContent className="registry-editor-dialog simple-model-dialog" finalFocus={false}>
      <form className="registry-editor simple-model-form" onSubmit={(event) => { event.preventDefault(); if (apiKey.trim()) onSave(apiKey.trim()) }}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>{oauth ? "更新 OAuth Secret" : "更新 API Key"}</DialogTitle>
          <DialogDescription>{model.display_name}</DialogDescription>
        </DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body simple-model-body">
          <div className="registry-field"><label className="registry-field-label" htmlFor="rotation-api-key">{oauth ? "OAuth Client Secret" : "Provider API Key"}</label><div className="login-password"><Input id="rotation-api-key" type={revealed ? "text" : "password"} autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} disabled={busy} autoFocus /><button type="button" className="login-reveal" onClick={() => setRevealed((value) => !value)} aria-label={revealed ? "隐藏凭据" : "显示凭据"} aria-pressed={revealed} disabled={busy}>{revealed ? <EyeOff size={15} /> : <Eye size={15} />}</button></div><small>新凭据通过候选 APIM Revision 验证后才会切换。</small></div>
          {error && <div className="registry-error">{error}</div>}
        </div>
        <DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose><Button type="submit" disabled={busy || !apiKey.trim()}><KeyRound size={14} />更新凭据</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function ModelWorkspace({ registry, tab, notice, error, busy, checkingRuntimeIds, diagnosticGatewayDeploymentId, diagnosticRuntimeId, removingModelKey, publications, topology, topologyAvailable, availabilityByModel, availabilityLoading, onOpenAvailability, onTabChange, onDismissNotice, onNoticeAction, onToggleSidebar, onAdd, onEdit, onRotateCredential, onDeleteModel, onOpenPublication, onArchivePublication, onToggleRuntime, onCheckRuntime, onDeleteConnection, onToggleGateway, onDefaultGateway, onDeleteGateway, canSyncPrices, syncingPrices, onSyncPrices }: {
  registry: ModelRegistry
  tab: Tab
  notice: Notice | null
  error: Error | null
  busy: boolean
  checkingRuntimeIds: Set<string>
  diagnosticGatewayDeploymentId: string | null
  diagnosticRuntimeId: string | null
  removingModelKey: string | null
  publications: GatewayPublication[]
  topology: ApimTargetTopologyList | null
  topologyAvailable: boolean
  availabilityByModel: Map<string, ModelAvailabilityReadback>
  availabilityLoading: boolean
  onOpenAvailability: (modelId: string) => void
  onTabChange: (tab: Tab) => void
  onDismissNotice: () => void
  onNoticeAction: () => void
  onToggleSidebar: () => void
  onAdd: (kind: RegistryKind) => void
  onEdit: (kind: RegistryKind, item: RegistryItem) => void
  onRotateCredential: (model: ManagedModel) => void
  onDeleteModel: (model: ManagedModel) => void
  onOpenPublication: (publicationId: string) => void
  onArchivePublication?: (publication: GatewayPublication) => void
  onToggleRuntime: (runtime: ModelRuntime) => void
  onCheckRuntime: (runtime: ModelRuntime, gatewayDeploymentId?: string) => void
  onDeleteConnection: (runtime: ModelRuntime) => void
  onToggleGateway: (gateway: GatewayProfile) => void
  onDefaultGateway: (gateway: GatewayProfile) => void
  onDeleteGateway: (gateway: GatewayProfile) => void
  canSyncPrices: boolean
  syncingPrices: boolean
  onSyncPrices: () => void
}) {
  const activeTab: ModelTableTab = tab
  const compactLayout = useIsMobile(761)
  const columnMinWidths = activeTab === "models" && compactLayout
    ? MODEL_COMPACT_COLUMN_MIN_WIDTHS
    : activeTab === "connections" && compactLayout
      ? MODEL_CONNECTION_COMPACT_COLUMN_MIN_WIDTHS
      : MODEL_TABLE_COLUMN_MIN_WIDTHS[activeTab]
  const [search, setSearch] = useState("")
  const [modelFilters, setModelFilters] = useState(emptyModelFilters)
  const [modelView, setModelView] = useState<"list" | "graph">("list")
  const [providerFilter, setProviderFilter] = useState("all")
  const [connectionHealthFilter, setConnectionHealthFilter] = useState<"active" | RuntimeHealthState>("active")
  const connectionHealthState = (runtime: ModelRuntime) => checkingRuntimeIds.has(runtime.id) ? "checking" : runtimeHealthState(runtime)
  const [collapsedProviderIds, setCollapsedProviderIds] = useState<Set<string>>(() => new Set())
  const kind: RegistryKind = activeTab === "connections" ? "runtime" : activeTab.slice(0, -1) as RegistryKind
  const tabs = topologyAvailable
    ? MODEL_MANAGEMENT_TABS
    : MODEL_MANAGEMENT_TABS.filter((value) => value !== "apim")
  const matches = (text: string) => text.toLowerCase().includes(search.toLowerCase())
  const inventoryConnections = registry.runtimes
  const inventoryProviderIds = new Set(
    inventoryConnections.map((runtime) => runtime.provider_id),
  )
  const providers = registry.providers
    .filter((provider) => inventoryProviderIds.has(provider.id))
    .map((provider, index) => ({ provider, index }))
    .sort((left, right) => {
      const rank = (provider: ModelProvider) => provider.brand_key === "microsoft_foundry"
        ? 0
        : provider.brand_key === "amazon_bedrock"
          ? 2
          : 1
      return rank(left.provider) - rank(right.provider) || left.index - right.index
    })
    .map(({ provider }) => provider)
  const matchingProviderIds = new Set(providers
    .filter((provider) => matches(`${provider.name} ${provider.provider_kind} ${provider.endpoint_url ?? ""}`))
    .map((provider) => provider.id))
  const scopedConnections = inventoryConnections.filter((runtime) => connectionHealthFilter === "active"
    ? runtime.enabled
    : connectionHealthState(runtime) === connectionHealthFilter)
  const connections = scopedConnections
    .filter((runtime) => providerFilter === "all" || runtime.provider_id === providerFilter)
    .filter((runtime) => matchingProviderIds.has(runtime.provider_id)
      || matches(`${runtime.name} ${runtime.provider_name} ${runtime.gateway_name ?? ""} ${runtime.runtime_kind}`))
    .sort((left, right) => left.name.localeCompare(right.name))
  const connectionGroups = providers
    .map((provider) => {
      const providerConnections = scopedConnections.filter((runtime) => runtime.provider_id === provider.id)
      const providerRuntimeIds = new Set(providerConnections.map((runtime) => runtime.id))
      return {
        provider,
        connections: connections.filter((runtime) => runtime.provider_id === provider.id),
        connectionCount: providerConnections.length,
        modelCount: registry.models.filter((model) => providerRuntimeIds.has(model.runtime_id)).length,
      }
    })
    .filter((group) => group.connections.length)
  const toggleProviderGroup = (providerId: string) => setCollapsedProviderIds((current) => {
    const next = new Set(current)
    if (next.has(providerId)) next.delete(providerId)
    else next.add(providerId)
    return next
  })
  const gateways = registry.gateways.filter((gateway) => matches(`${gateway.name} ${gateway.implementation} ${gateway.base_url ?? ""}`))
  const deploymentCount = topologyAvailable ? gatewayEntries(topology).length : registry.gateways.length
  const gatewayCount = deploymentCount
  const count = activeTab === "connections"
    ? connections.length
    : activeTab === "gateways"
      ? gatewayCount
      : activeTab === "apim"
        ? topology?.total ?? 0
        : registry.models.length
  const connectionHealthCounts = {
    active: inventoryConnections.filter((runtime) => runtime.enabled).length,
    online: inventoryConnections.filter((runtime) => connectionHealthState(runtime) === "online").length,
    offline: inventoryConnections.filter((runtime) => connectionHealthState(runtime) === "offline").length,
    unchecked: inventoryConnections.filter((runtime) => connectionHealthState(runtime) === "unchecked").length,
    disabled: inventoryConnections.filter((runtime) => connectionHealthState(runtime) === "disabled").length,
    blocked: inventoryConnections.filter((runtime) => connectionHealthState(runtime) === "blocked").length,
    configuration: inventoryConnections.filter((runtime) => connectionHealthState(runtime) === "configuration").length,
    inconclusive: inventoryConnections.filter((runtime) => connectionHealthState(runtime) === "inconclusive").length,
    changed: inventoryConnections.filter((runtime) => connectionHealthState(runtime) === "changed").length,
    checking: inventoryConnections.filter((runtime) => connectionHealthState(runtime) === "checking").length,
  }
  const connectionHealthLabels = {
    active: "启用中",
    ...runtimeHealthLabels,
  }
  const addLabel = activeTab === "models" ? "模型" : activeTab === "connections" ? "连接" : activeTab === "apim" ? "APIM 实例" : "网关"
  const columnHeaders = [
    addLabel,
    ...(activeTab === "connections" ? [] : [activeTab === "models" ? "运行时" : "Gateway 类型"]),
    ...(activeTab === "models" ? ["目标网关"] : []),
    activeTab === "models" ? "上下文 / 价格" : activeTab === "connections" ? "目标网关" : "API / 端点",
    activeTab === "connections" ? "最近诊断" : "状态",
    "操作",
  ]
  const latestPublicationByModel = new Map<string, GatewayPublication>()
  for (const publication of [...publications].sort((left, right) => right.updated_at.localeCompare(left.updated_at))) {
    if (publication.publication_kind === "model_remove" && publication.status === "failed") continue
    const identity = `${publication.gateway_profile_id}:${publication.model_key}`
    if (!latestPublicationByModel.has(identity)) {
      latestPublicationByModel.set(identity, publication)
    }
  }
  const rowStatuses = new Set<GatewayPublication["status"]>([
    "queued", "validating", "provisioning", "building_revision", "verifying",
    "awaiting_authorization", "promoting", "failed", "rolling_back",
  ])
  const pendingModelPublications = [...latestPublicationByModel.values()].filter(
    (publication) => publication.publication_kind === "model_add"
      && rowStatuses.has(publication.status)
      && !registry.models.some((model) => model.model_key === publication.model_key
        && registry.runtimes.some(runtime => runtime.id === model.runtime_id && runtime.gateway_profile_id === publication.gateway_profile_id)),
  )
  const modelRecords = registry.models.map(model => {
    const runtime = registry.runtimes.find(item => item.id === model.runtime_id)
    const provider = registry.providers.find(item => item.id === model.provider_id)
    const record = modelFilterRecord(model, runtime, provider, topology, registry.gateways)
    const publication = latestPublicationByModel.get(`${runtime?.gateway_profile_id}:${model.model_key}`)
    if (publication && rowStatuses.has(publication.status)) record.status = publicationFilterRecord(publication).status
    return { model, record }
  })
  const pendingRecords = pendingModelPublications.map(publication => ({ publication, record: publicationFilterRecord(publication) }))
  const filterRecords = [...modelRecords, ...pendingRecords].map(item => item.record)
  const models = modelRecords.filter(item => matchesModelFilters(item.record, modelFilters, search)).map(item => item.model)
  const visiblePublications = pendingRecords.filter(item => matchesModelFilters(item.record, modelFilters, search)).map(item => item.publication)
  const filterGateways = (topology?.items ?? []).flatMap(apimGatewayEntries).map(({ deployment }) => ({
    id: deployment.id, name: deployment.display_name.trim() || deployment.native_gateway_id, targetId: deployment.apim_target_id,
  }))
  const filterApimInstances = apimGatewayProfiles(registry.gateways, topology, topologyAvailable).filter(profile => profile.implementation === "apim").map(profile => ({
    id: profile.id, name: profile.name.trim() || topology?.items.find(target => target.target.gateway_profile_id === profile.id)?.target.service_name || profile.id,
  }))

  return <div className="smh-workspace model-workspace">
    <header className="smh-page-header">
      <div><Button type="button" variant="ghost" size="icon-sm" className="model-mobile-sidebar-toggle" aria-label="切换导航栏" title="切换导航栏" onClick={onToggleSidebar}><PanelLeft size={16} /></Button><span className="smh-header-icon"><Cpu size={17} /></span><h1>模型管理</h1><span>{registry.models.length}</span></div>
      {activeTab !== "gateways" && <div className="smh-header-actions">
        {activeTab === "models" && canSyncPrices && <button type="button"
          title={`同步全部 ${registry.models.length} 个已保存模型的定价`}
          disabled={busy || syncingPrices} onClick={onSyncPrices}>
          <RefreshCw size={14} className={syncingPrices ? "spin" : undefined} />
          {syncingPrices ? "正在同步" : "同步定价"}
        </button>}
        <button onClick={() => onAdd(kind)}><Plus size={14} />添加{addLabel}</button>
      </div>}
    </header>
    <div className="model-toolbar">
      <ButtonGroup className="model-tabs usage-metric-segment" aria-label="模型管理类型">{tabs.map((value) => <button key={value} data-tab={value} className={activeTab === value ? "active" : ""} aria-pressed={activeTab === value} onClick={() => onTabChange(value)}>{value === "models" ? "模型" : value === "connections" ? "连接" : value === "apim" ? "APIM 实例" : "网关"}<span>{value === "connections" ? scopedConnections.length : value === "apim" ? topology?.total ?? "—" : value === "gateways" ? topologyAvailable && !topology ? "—" : gatewayCount : registry.models.length}</span></button>)}</ButtonGroup>
      <div className={`model-toolbar-filters ${activeTab === "models" ? "model-inventory-toolbar-filters" : ""}`}>
        {activeTab === "connections" && <Select value={connectionHealthFilter} onValueChange={(value) => setConnectionHealthFilter((value ?? "active") as "active" | RuntimeHealthState)}>
          <SelectTrigger className="connection-health-filter" aria-label="按诊断结果筛选"><SelectValue>{connectionHealthLabels[connectionHealthFilter]} · {connectionHealthCounts[connectionHealthFilter]}</SelectValue></SelectTrigger>
          <SelectContent align="end">
            {(Object.keys(connectionHealthLabels) as Array<keyof typeof connectionHealthLabels>)
              .filter((value) => value === "active" || connectionHealthCounts[value] > 0)
              .map((value) => <SelectItem key={value} value={value}><span className="connection-health-option"><i className={value} /><span>{connectionHealthLabels[value]}</span><small>{connectionHealthCounts[value]}</small></span></SelectItem>)}
          </SelectContent>
        </Select>}
        {activeTab === "connections" && <Select value={providerFilter} onValueChange={(value) => setProviderFilter(value ?? "all")}>
          <SelectTrigger className="connection-provider-filter" aria-label="按提供方筛选"><SelectValue>{providerFilter === "all"
            ? <span className="registry-option"><Database size={15} /><span>全部提供方</span></span>
            : (() => {
                const provider = registry.providers.find((item) => item.id === providerFilter)
                return provider ? <span className="registry-option"><ProviderInventoryLogo provider={provider} runtimes={inventoryConnections} size={15} /><span>{providerInventoryName(provider, inventoryConnections)}</span></span> : null
              })()}</SelectValue></SelectTrigger>
          <SelectContent align="end">
            <SelectItem value="all"><span className="registry-option"><Database size={15} /><span>全部提供方</span></span></SelectItem>
            {providers.map((provider) => <SelectItem key={provider.id} value={provider.id}><span className="registry-option"><ProviderInventoryLogo provider={provider} runtimes={inventoryConnections} size={15} /><span>{providerInventoryName(provider, inventoryConnections)}</span></span></SelectItem>)}
          </SelectContent>
        </Select>}
        <ExpandableSearch key={activeTab} value={search} onChange={setSearch} placeholder={`搜索${addLabel}...`} />
        {activeTab === "models" && <ButtonGroup className="usage-metric-segment model-view-switch" aria-label="模型视图">
          <button type="button" className={modelView === "list" ? "active" : ""} aria-label="列表视图" title="列表视图" aria-pressed={modelView === "list"} onClick={() => setModelView("list")}><List size={14} /></button>
          <button type="button" className={modelView === "graph" ? "active" : ""} aria-label="关系图" title="关系图" aria-pressed={modelView === "graph"} onClick={() => setModelView("graph")}><Orbit size={14} strokeWidth={1.5} /></button>
        </ButtonGroup>}
        {activeTab === "models" && <ModelInventoryFilterMenu filters={modelFilters} onChange={setModelFilters} connections={registry.runtimes} gateways={filterGateways} apimInstances={filterApimInstances} records={filterRecords} search={search} compact={compactLayout} />}
      </div>
    </div>
    {activeTab === "models" && <ModelInventoryFilterChips filters={modelFilters} onChange={setModelFilters} connections={registry.runtimes} gateways={filterGateways} apimInstances={filterApimInstances} />}
    {notice && <RegistryNoticeBanner notice={notice} onDismiss={onDismissNotice} onAction={onNoticeAction} />}
    {error && <div className="registry-error smh-notice infrastructure-error-notice" role="alert"><TriangleAlert size={14} aria-hidden="true" /><div className="smh-notice-copy">保存失败：{String(error)}</div></div>}
    {activeTab === "models" && modelView === "graph" && (models.length > 0 || visiblePublications.length === 0) && <ModelInventoryGraph registry={registry} models={models} topology={topology} />}
    {(activeTab !== "models" || modelView === "list" || visiblePublications.length > 0) && <ResizableGridTable className={`model-table ${activeTab}${activeTab === "models" && modelView === "graph" ? " model-graph-publications" : ""}`} role="table" aria-label={`${addLabel}列表`} headerSelector=".model-table-head" minWidths={columnMinWidths} columnGap={12} horizontalPadding={32} resizeAtAllWidths>
      <div className="model-table-head" role="row">
        {columnHeaders.map((label, index) => <span className="model-table-heading" role="columnheader" aria-label={label} key={label}>
          {index < columnHeaders.length - 1 && <span>{label}</span>}
        </span>)}
      </div>
      <div className="model-table-body">
        {activeTab === "models" && modelView === "list" && models.map((model) => {
          const provider = registry.providers.find((item) => item.id === model.provider_id)
          const runtime = registry.runtimes.find((item) => item.id === model.runtime_id)
          const gateway = registry.gateways.find((item) => item.id === runtime?.gateway_profile_id)
          const deleteDisabledReason = model.is_default
            ? "请先将其他模型设为默认模型"
            : gateway?.implementation !== "apim"
              ? "当前仅支持删除由 APIM 路由的模型"
              : null
          const latestPublication = latestPublicationByModel.get(`${runtime?.gateway_profile_id}:${model.model_key}`)
          const rowPublication = latestPublication && rowStatuses.has(latestPublication.status)
            ? latestPublication
            : null
          const managedCredential = runtime?.config.auth_strategy === "named_value_bearer"
            || runtime?.config.auth_strategy === "named_value_api_key"
            || runtime?.config.auth_strategy === "oauth_client_credentials"
              && registry.databricks_oauth_supported === true
          return <ModelRow key={model.id} model={model} runtime={runtime} target={modelTargetDetails(model, runtime, registry.gateways, topology)} availability={availabilityByModel.get(model.id)} availabilityLoading={availabilityLoading} onOpenAvailability={() => onOpenAvailability(model.id)} busy={busy} removing={removingModelKey === model.model_key} publication={rowPublication} canRotateCredential={Boolean(model.publication_id && managedCredential)} deleteDisabledReason={deleteDisabledReason} onOpenPublication={onOpenPublication} onEdit={() => onEdit("model", model)} onRotateCredential={() => onRotateCredential(model)} onDelete={() => onDeleteModel(model)} />
        })}
        {activeTab === "models" && visiblePublications.map((publication) => <PublicationModelRow key={publication.id} publication={publication} registry={registry} topology={topology} onOpen={() => onOpenPublication(publication.id)} onArchive={onArchivePublication} busy={busy} />)}
        {activeTab === "connections" && connectionGroups.map(({ provider, connections: providerConnections, connectionCount, modelCount }) => {
          const collapsed = collapsedProviderIds.has(provider.id)
          return <section className="connection-provider-group" role="rowgroup" aria-label={provider.name} key={provider.id}>
            <button className="connection-provider-heading" type="button" aria-expanded={!collapsed} onClick={() => toggleProviderGroup(provider.id)}>
              <span className="connection-provider-identity"><span className="model-list-icon" data-model-family={providerBrand(provider)}><ProviderInventoryLogo provider={provider} runtimes={providerConnections} /></span><span><b>{providerInventoryName(provider, providerConnections)}</b><small>{providerKindLabels[provider.provider_kind] ?? provider.provider_kind.replaceAll("_", " ")}</small></span></span>
              <span className="connection-provider-summary"><b>{connectionCount} 个连接</b><small>{modelCount} 个模型</small></span>
              <span className="model-status-cell"><span className="machine-health"><CircleDashed size={13} />{provider.enabled ? "已启用" : "已停用"}</span></span>
              <span className="connection-provider-chevron"><ChevronRight size={15} /></span>
            </button>
            {!collapsed && providerConnections.map((runtime) => <ConnectionRow key={runtime.id} runtime={runtime} models={registry.models.filter((model) => model.runtime_id === runtime.id)} profiles={registry.gateways} topology={topology} busy={busy} checking={checkingRuntimeIds.has(runtime.id)} checkingGatewayDeploymentId={diagnosticRuntimeId === runtime.id ? diagnosticGatewayDeploymentId : null} onEdit={() => onEdit("runtime", runtime)} onToggle={() => onToggleRuntime(runtime)} onCheck={(gatewayDeploymentId) => onCheckRuntime(runtime, gatewayDeploymentId)} onDelete={() => onDeleteConnection(runtime)} />)}
          </section>
        })}
        {activeTab === "gateways" && gateways.map((gateway) => {
          const runtimeCount = registry.runtimes.filter(
            (runtime) => runtime.gateway_profile_id === gateway.id,
          ).length
          return <GatewayCompatibilityRow
            key={gateway.id}
            gateway={gateway}
            runtimeCount={runtimeCount}
            busy={busy}
            onEdit={() => onEdit("gateway", gateway)}
            onToggle={() => onToggleGateway(gateway)}
            onDefault={() => onDefaultGateway(gateway)}
            onDelete={() => onDeleteGateway(gateway)}
          />
        })}
        {((activeTab === "models" && !models.length && !visiblePublications.length) || (activeTab === "connections" && !connectionGroups.length) || (activeTab === "gateways" && !gateways.length)) && <div className="smh-empty"><Search size={28} /><b>没有找到{addLabel}</b><span>尝试调整搜索条件。</span></div>}
      </div>
    </ResizableGridTable>}
    <footer className="model-table-footer" aria-live="polite">{activeTab === "gateways" ? `${deploymentCount} 个 Gateway API` : `共 ${activeTab === "models" ? models.length + visiblePublications.length : count} 个${addLabel}`}</footer>
  </div>
}

/** In-page navigation between the runtime list and a runtime's detail.
 *
 *  These two views are one React page reading `?runtime=`, but they were linked with plain
 *  anchors, so every click tore the document down and rebuilt it -- a full reload, a fresh
 *  bundle parse and a cold query cache, to move between two halves of the same screen.
 *
 *  The `href` stays: it is what makes the row middle-clickable and keeps the URL real. The
 *  handler only takes over the ordinary left click, and leaves modified clicks alone so
 *  "open in new tab" still works. `finops:navigate` is the event the page already listens
 *  for, so nothing new has to be wired up. */
function runtimeRouteHref(runtimeId: string | null) {
  const url = new URL(window.location.href)
  url.searchParams.set("page", "models")
  url.searchParams.set("tab", "connections")
  url.searchParams.delete("model")
  if (runtimeId) url.searchParams.set("runtime", runtimeId)
  else url.searchParams.delete("runtime")
  return `${url.pathname}${url.search}`
}

function openRuntimeRoute(runtimeId: string | null) {
  return (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    if (event.button !== 0) return
    event.preventDefault()
    window.history.pushState(null, "", runtimeRouteHref(runtimeId))
    window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT))
  }
}

function publicationIdFromUrl() {
  return new URL(window.location.href).searchParams.get("publication")
}

function replacePublicationUrl(publicationId: string | null) {
  const url = new URL(window.location.href)
  if (publicationId) url.searchParams.set("publication", publicationId)
  else url.searchParams.delete("publication")
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`)
}

function ModelPublicationStatus({ publication, onOpen }: { publication: GatewayPublication; onOpen: (publicationId: string) => void }) {
  const failed = publication.status === "failed" || publication.status === "rolled_back"
  return <button type="button" className={`model-publication-status ${failed ? "failed" : publication.status === "awaiting_authorization" ? "warning" : publication.publication_kind === "model_remove" ? "removing" : "running"}`} title={publicationStatusLabel(publication.status, publication.configuration_only === true, publication.publication_kind)} onClick={() => onOpen(publication.id)}>
    {failed ? <TriangleAlert size={12} /> : publication.status === "awaiting_authorization" ? <ShieldCheck size={12} /> : <RefreshCw className="spin" size={12} />}
    <span>{publicationStatusLabel(publication.status, publication.configuration_only === true, publication.publication_kind)}</span>
  </button>
}

export function ModelTargetGatewayCell({ target, emptyLabel = "目标未记录", unrecordedModelCount = 0 }: { target: ReturnType<typeof publicationTargetDetails>; emptyLabel?: string; unrecordedModelCount?: number }) {
  return <div className="model-target-cell" role="cell">
    {target.gateways.length ? target.gateways.map(gateway => <div key={gateway.id} data-deployment-id={gateway.id}>
      {gateway.kind === "managed" ? <b title={gateway.nativeId}>托管网关</b> : <b data-no-localize title={gateway.nativeId}>{gateway.name}</b>}
      <span title={target.serviceName ?? target.apimName}><span>{gateway.kind === "managed" ? "托管" : "自托管"}</span>{target.apimName && <> · <span data-no-localize>{target.apimName}</span></>}</span>
    </div>) : <div><b>{emptyLabel}</b><span data-no-localize>{target.apimName || "—"}</span></div>}
    {target.gateways.length > 0 && unrecordedModelCount > 0 && <div><b>网关未记录</b><span>{unrecordedModelCount} 个模型</span></div>}
  </div>
}

function PublicationModelRow({ publication, registry, topology, onOpen, onArchive, busy = false }: {
  publication: GatewayPublication
  registry: Pick<ModelRegistry, "gateways">
  topology: ApimTargetTopologyList | null
  onOpen: () => void
  onArchive?: (publication: GatewayPublication) => void
  busy?: boolean
}) {
  return <article className="model-list-row publication-model-row" role="row" data-publication-id={publication.id}>
    <div className="model-primary-cell"><span className="model-list-icon"><Cpu size={16} /></span><div><b data-no-localize>{publication.display_name}</b><code data-no-localize>{publication.model_key}</code></div></div>
    <div className="model-runtime-cell"><b>—</b></div>
    <ModelTargetGatewayCell target={publicationTargetDetails(publication, registry.gateways, topology)} />
    <div className="model-pricing-cell"><b>—</b><span>{publication.status === "failed" ? "发布未完成" : "发布完成后显示"}</span></div>
    <div className="model-status-cell"><ModelPublicationStatus publication={publication} onOpen={() => onOpen()} /></div>
    <div className="model-row-actions">
      <button title="查看详情" onClick={onOpen}><Eye size={14} /></button>
      {publication.status === "failed" && onArchive && <button title="移除失败记录" aria-label="移除失败记录" disabled={busy} onClick={() => onArchive(publication)}><Trash2 size={14} /></button>}
    </div>
  </article>
}

function ModelRow({ model, runtime, target, availability, availabilityLoading, onOpenAvailability, busy, removing, publication, canRotateCredential, deleteDisabledReason, onOpenPublication, onEdit, onRotateCredential, onDelete }: { model: ManagedModel; runtime: ModelRuntime | undefined; target: ReturnType<typeof modelTargetDetails>; availability?: ModelAvailabilityReadback; availabilityLoading?: boolean; onOpenAvailability: () => void; busy: boolean; removing: boolean; publication: GatewayPublication | null; canRotateCredential: boolean; deleteDisabledReason: string | null; onOpenPublication: (publicationId: string) => void; onEdit: () => void; onRotateCredential: () => void; onDelete: () => void }) {
  const availabilityAction = useModelAvailabilityCommand(model.id, availability)
  const family = modelFamily(model)
  const decision = model.capabilities.includes("decision")
  const foundryRouter = runtime?.runtime_kind === "foundry" && model.upstream_model_id === "model-router"
  const runtimeVendor = runtime?.runtime_kind === "openai_compatible" && runtime.config.api_format === "openai_chat"
    ? modelVendorFromMetadata(runtime.config, `${model.model_key} ${model.display_name} ${runtime.name}`)
    : null
  return <article className="model-list-row" role="row" data-model-id={model.id}>
    <div className="model-primary-cell"><span className="model-list-icon" data-model-family={runtimeVendor ?? family} title={decision ? "Microsoft" : foundryRouter ? "Microsoft Foundry" : runtimeVendor ? modelVendorLabel(runtimeVendor) : modelFamilyLabel(family)}>{decision ? <MicrosoftLogo size={16} /> : foundryRouter ? <FoundryLogo size={16} /> : runtimeVendor ? <ModelVendorLogo value={runtimeVendor} size={18} /> : <ModelFamilyLogo family={family} />}</span><div><a className="connection-name-link model-name-link" href={modelDetailHref(model.id)} onClick={openModelDetailLink}><b title={model.display_name}>{model.display_name}</b></a><code title={model.model_key}>{model.model_key}</code></div>{model.is_default && <em>默认</em>}</div>
    <div className="model-runtime-cell model-runtime-identity"><span className="model-list-icon" aria-hidden="true">{runtime ? <RuntimeLogo runtime={runtime} size={16} /> : <Cpu size={16} />}</span><div className="model-runtime-copy"><b title={model.runtime_name}>{model.runtime_name}</b><span>{runtimeVendor ? modelVendorLabel(runtimeVendor) : model.provider_name}</span></div></div>
    <ModelTargetGatewayCell target={target} />
    <div className="model-pricing-cell"><b>{model.context_window?.toLocaleString() ?? "—"}</b>{model.context_price_tiers?.length
      ? <span>分档价格 <span data-no-localize>({model.context_price_tiers.length})</span></span>
      : <span>输入 ${model.input_cost_per_million ?? "—"} · 缓存读 ${model.cached_cost_per_million ?? model.input_cost_per_million ?? "—"} · 缓存写 ${model.cache_write_cost_per_million ?? model.cached_cost_per_million ?? model.input_cost_per_million ?? "—"} · 输出 ${model.output_cost_per_million ?? "—"}</span>}</div>
    <div className="model-status-cell">{publication ? <ModelPublicationStatus publication={publication} onOpen={onOpenPublication} /> : removing ? <span className="model-removing-status"><RefreshCw className="spin" size={12} />正在删除</span> : <ModelAvailabilityStatus readback={availability} loading={availabilityLoading} pending={availabilityAction.submitting} requestedEnabled={availabilityAction.requestedEnabled} error={availabilityAction.error} onOpen={onOpenAvailability} />}</div>
  <div className="model-row-actions"><button className="model-edit-action" title="编辑" disabled={busy || removing || Boolean(publication && publication.status !== "failed" && publication.status !== "rolled_back")} onClick={onEdit}><Edit3 size={14} /></button><button className="model-delete-action" title={deleteDisabledReason ?? "删除模型"} disabled={busy || removing || availabilityAction.inFlight || Boolean(publication) || Boolean(deleteDisabledReason)} onClick={onDelete}><Trash2 size={14} /></button>{canRotateCredential && <button className="model-credential-action" title={runtime?.config.auth_strategy === "oauth_client_credentials" ? "更新 OAuth Secret" : "更新 API Key"} disabled={busy || removing || Boolean(publication)} onClick={onRotateCredential}><KeyRound size={14} /></button>}<ModelAvailabilitySwitch readback={availability} loading={availabilityLoading} registryEnabled={model.enabled} pending={availabilityAction.submitting} requestedEnabled={availabilityAction.requestedEnabled} disabled={busy || removing || Boolean(publication)} onChange={availabilityAction.change} /></div>
  </article>
}

type ModelFamily = "openai" | "claude" | "deepseek" | "kimi" | "generic"

function modelFamily(model: ManagedModel): ModelFamily {
  return modelInventoryFamily(model)
}

function modelFamilyLabel(family: ModelFamily) {
  return { openai: "OpenAI", claude: "Claude", deepseek: "DeepSeek", kimi: "Kimi", generic: "Model" }[family]
}

function ModelFamilyLogo({ family }: { family: ModelFamily }) {
  if (family === "openai") return <OpenAiLogo size={16} />
  if (family === "claude") return <ClaudeLogo size={16} />
  if (family === "deepseek") return <DeepSeekLogo size={18} />
  if (family === "kimi") return <KimiLogo size={18} />
  return <Cpu size={16} />
}

function providerBrand(provider: ModelProvider): ProviderBrand {
  return providerBrandFromMetadata(
    provider.brand_key,
    `${provider.name} ${provider.provider_kind}`,
  )
}

function providerInventoryVendor(
  provider: ModelProvider,
  runtimes: ModelRuntime[],
) {
  if (provider.provider_kind !== "openai_compatible") return null
  const runtime = runtimes.find((item) => item.provider_id === provider.id)
  return modelVendorFromMetadata(
    runtime?.config,
    `${provider.name} ${runtime?.name ?? ""}`,
  )
}

function providerInventoryName(provider: ModelProvider, runtimes: ModelRuntime[]) {
  const vendor = providerInventoryVendor(provider, runtimes)
  return vendor ? modelVendorLabel(vendor) : provider.name
}

function ProviderInventoryLogo({ provider, runtimes, size = 16 }: {
  provider: ModelProvider
  runtimes: ModelRuntime[]
  size?: number
}) {
  const vendor = providerInventoryVendor(provider, runtimes)
  return vendor
    ? <ModelVendorLogo value={vendor} size={size} />
    : <ProviderBrandLogo brand={providerBrand(provider)} size={size} />
}

function ConnectionRow({ runtime, models, profiles, topology, busy, checking, checkingGatewayDeploymentId, onEdit, onToggle, onCheck, onDelete }: { runtime: ModelRuntime; models: ManagedModel[]; profiles: GatewayProfile[]; topology: ApimTargetTopologyList | null; busy: boolean; checking: boolean; checkingGatewayDeploymentId: string | null; onEdit: () => void; onToggle: () => void; onCheck: (gatewayDeploymentId?: string) => void; onDelete: () => void }) {
  const awaitingFirstModel = runtime.config.control_plane_managed === true && models.length === 0
  const hasEnabledModel = models.some((model) => model.enabled)
  const deleteDisabledReason = models.length
    ? "请先删除此连接中的全部模型"
    : runtime.is_default
      ? "请先将其他模型设为默认模型"
      : runtime.config.control_plane_managed !== true
        ? "仅支持删除由 Turnstile 管理的连接"
        : null
  const target = connectionTargetDetails(runtime, models, profiles, topology)
  const hasGatewayChildren = target.gateways.length > 1
  const primaryGateway = target.gateways.find(gateway => gateway.kind === "managed") ?? target.gateways[0]
  const diagnostic = runtimeDiagnostic(runtime)
  const diagnosticMatches = !hasGatewayChildren || diagnostic?.selection?.gateway_deployment_id === primaryGateway.id
  const primaryChecking = checking && (!hasGatewayChildren || checkingGatewayDeploymentId === primaryGateway.id)
  const primaryState = primaryChecking ? "checking" : diagnosticMatches ? runtimeHealthState(runtime) : "unchecked"
  return <>
    <article className="model-list-row connection-list-row" role="row" data-runtime-id={runtime.id}>
      <div className="model-primary-cell connection-primary-cell"><span className="connection-branch" aria-hidden="true" /><div><a className="connection-name-link" href={runtimeRouteHref(runtime.id)} onClick={openRuntimeRoute(runtime.id)} title={runtime.name}><b>{runtime.name}</b></a><span>{models.length} 个模型</span></div>{runtime.is_default && <em>默认</em>}</div>
      <ConnectionTargetGatewayCell runtime={runtime} models={models} profiles={profiles} topology={topology} busy={busy} checking={checking} checkingGatewayDeploymentId={checkingGatewayDeploymentId} onCheck={onCheck} />
      <div className="model-status-cell" title={primaryChecking ? runtimeHealthLabels.checking : diagnosticMatches ? runtimeHealthDescription(runtime) : "尚无此 Gateway 的诊断结果"} aria-busy={primaryChecking || undefined}>{awaitingFirstModel && !checking ? <span className="connection-pending-status" role="status" aria-label="等待首次模型验证" title="等待首次模型验证"><span className="connection-pending-primary"><CircleDashed size={12} /><b>待验证</b></span><small>添加首个模型</small></span> : <RuntimeHealth state={primaryState} checkedAt={primaryChecking || !diagnosticMatches ? undefined : diagnostic?.checked_at} />}</div>
      <div className="model-row-actions"><button className="connection-health-action" title={primaryChecking ? "正在检查" : hasEnabledModel ? "连接诊断" : "添加启用模型后可检查"} disabled={busy || !runtime.enabled || !hasEnabledModel} onClick={() => onCheck(hasGatewayChildren ? primaryGateway.id : undefined)}><RefreshCw className={primaryChecking ? "spin" : ""} size={14} /></button><button className="model-edit-action" title="编辑" disabled={busy || checking} onClick={onEdit}><Edit3 size={14} /></button><button className="model-delete-action" title={deleteDisabledReason ?? "删除连接"} disabled={busy || checking || Boolean(deleteDisabledReason)} onClick={onDelete}><Trash2 size={14} /></button><Toggle checked={runtime.enabled} disabled={busy || checking} onChange={onToggle} /></div>
    </article>
    {hasGatewayChildren && target.gateways.filter(gateway => gateway.id !== primaryGateway.id).map(gateway => <ConnectionGatewayChildRow
      key={gateway.id}
      gateway={gateway}
      apimName={target.apimName}
      modelCount={target.gatewayModelCounts[gateway.id] ?? 0}
      runtime={runtime}
      checking={checking}
      checkingGatewayDeploymentId={checkingGatewayDeploymentId}
      disabled={busy || !runtime.enabled}
      onCheck={() => onCheck(gateway.id)}
    />)}
  </>
}

export function ConnectionTargetGatewayCell({ runtime, models, profiles, topology, busy, checking, checkingGatewayDeploymentId, onCheck }: {
  runtime: ModelRuntime
  models: readonly ManagedModel[]
  profiles: readonly GatewayProfile[]
  topology: ApimTargetTopologyList | null
  busy: boolean
  checking: boolean
  checkingGatewayDeploymentId: string | null
  onCheck: (gatewayDeploymentId?: string) => void
}) {
  if (!runtime.gateway_profile_id) return <div className="model-target-cell" role="cell"><b>提供方直连</b></div>
  const target = connectionTargetDetails(runtime, models, profiles, topology)
  if (target.gateways.length > 1) {
    const primaryGateway = target.gateways.find(gateway => gateway.kind === "managed") ?? target.gateways[0]
    return <ModelTargetGatewayCell target={{ ...target, gateways: [primaryGateway] }} />
  }
  return <ModelTargetGatewayCell target={target} emptyLabel={target.modelCount ? "网关未记录" : "尚未发布模型"} unrecordedModelCount={target.unrecordedModelCount} />
}

function ConnectionGatewayChildRow({ gateway, apimName, modelCount, runtime, checking, checkingGatewayDeploymentId, disabled, onCheck }: {
  gateway: ReturnType<typeof connectionTargetDetails>["gateways"][number]
  apimName: string
  modelCount: number
  runtime: ModelRuntime
  checking: boolean
  checkingGatewayDeploymentId: string | null
  disabled: boolean
  onCheck: () => void
}) {
  const diagnostic = runtimeDiagnostic(runtime)
  const diagnosticMatches = diagnostic?.selection?.gateway_deployment_id === gateway.id
  const state = checking && checkingGatewayDeploymentId === gateway.id
    ? "checking" : diagnosticMatches ? runtimeHealthState(runtime) : "unchecked"
  return <article className="model-list-row connection-list-row connection-gateway-child-row" role="row" data-parent-runtime-id={runtime.id} data-gateway-deployment-id={gateway.id}>
    <div role="cell" />
    <div className="model-target-cell connection-gateway-child-target" role="cell"><div className="connection-gateway-child-copy" data-deployment-id={gateway.id}><b data-no-localize title={gateway.nativeId}>{gateway.name}</b><span><span>{gateway.kind === "managed" ? "托管" : "自托管"}</span> · <span data-no-localize>{apimName}</span></span></div></div>
    <div className="model-status-cell" title={diagnosticMatches ? runtimeHealthDescription(runtime) : "尚无此 Gateway 的诊断结果"} aria-busy={checking && checkingGatewayDeploymentId === gateway.id || undefined}>
      <RuntimeHealth state={state} checkedAt={diagnosticMatches ? diagnostic?.checked_at : undefined} />
    </div>
    <div className="model-row-actions connection-gateway-child-actions">
      <button type="button" className="connection-gateway-diagnostic" title={`诊断 ${gateway.name}`} aria-label={`诊断 ${gateway.name}`} disabled={disabled || checking || modelCount === 0} onClick={onCheck}>
        <RefreshCw className={checking && checkingGatewayDeploymentId === gateway.id ? "spin" : ""} size={14} />
      </button>
    </div>
  </article>
}

function GatewayCompatibilityRow({ gateway, runtimeCount, busy, onEdit, onToggle, onDefault, onDelete }: {
  gateway: GatewayProfile
  runtimeCount: number
  busy: boolean
  onEdit: () => void
  onToggle: () => void
  onDefault: () => void
  onDelete: () => void
}) {
  const deleteDisabledReason = gateway.is_default
    ? "请先将其他 Managed APIM 设为默认"
    : runtimeCount > 0
      ? "请先移除该 Managed APIM 下的全部连接"
      : null
  return <article className="model-list-row gateway-api-row" role="row">
    <div className="model-primary-cell"><span className="model-list-icon" data-model-family={gatewayBrand(gateway)}><GatewayBrandLogo brand={gatewayBrand(gateway)} /></span><div><b title={gateway.name}>{gateway.name}</b><code>managed</code></div>{gateway.is_default && <em>默认</em>}</div>
    <div className="model-runtime-cell"><b>Managed APIM</b><span>Inventory unavailable</span></div>
    <div className="model-pricing-cell"><b>{gateway.base_url ?? "Endpoint 未配置"}</b><span>{`${runtimeCount} 个运行时 · API 关联未读取`}</span></div>
    <div className="model-status-cell"><GatewayReadiness state={gateway.enabled && gateway.base_url ? "ready" : "unavailable"} label={gateway.enabled && gateway.base_url ? "Configured" : "Unavailable"} detail="Managed APIM" /></div>
    <div className="model-row-actions gateway-api-actions">
      <button className="model-edit-action" title="编辑 Managed APIM" disabled={busy} onClick={onEdit}><Edit3 size={14} /></button>
      <button className="model-delete-action" title={deleteDisabledReason ?? "删除 Managed APIM"} disabled={busy || Boolean(deleteDisabledReason)} onClick={onDelete}><Trash2 size={14} /></button>
      {!gateway.is_default && <button className="connection-default-action" title="设为默认 Managed APIM" disabled={busy || !gateway.enabled} onClick={onDefault}><Check size={14} /></button>}
      <Toggle checked={gateway.enabled} disabled={busy} onChange={onToggle} />
    </div>
  </article>
}

function GatewayReadiness({ state, label, detail }: { state: "ready" | "warning" | "unavailable"; label: string; detail: string }) {
  return <span className={`gateway-readiness ${state}`} title={`${label} · ${detail}`}><span><i />{label}</span><small>{detail}</small></span>
}

function Health({ status, enabled }: { status: string; enabled: boolean }) {
  const actual = enabled ? status : "unavailable"
  return <span className={`registry-health ${actual}`}><i />{actual === "available" ? "可用" : actual === "unavailable" ? "不可用" : "未检查"}</span>
}

function Toggle({ checked, disabled, onChange }: { checked: boolean; disabled: boolean; onChange: () => void }) {
  return <button type="button" aria-label={checked ? "停用" : "启用"} aria-pressed={checked} className={`toggle ${checked ? "on" : ""}`} disabled={disabled} onClick={onChange}><i /></button>
}

const NONE_SELECT_VALUE = "__none__"

type RegistryOption = { value: string; label: string; icon?: React.ReactNode; disabled?: boolean }

const providerKindLabels: Record<string, string> = {
  anthropic: "Anthropic Messages",
  microsoft_foundry: "Microsoft Foundry",
  openai_compatible: "OpenAI 兼容",
}

const providerKindOptions: RegistryOption[] = Object.entries(providerKindLabels).map(([value, label]) => ({
  value,
  label,
  icon: <ProviderBrandLogo brand={providerBrandFromIdentity(`${value} ${label}`)} size={15} />,
}))

const gatewayImplementationOptions: RegistryOption[] = [
  { value: "apim", label: "Azure API Management" },
  { value: "litellm", label: "LiteLLM", disabled: true },
  { value: "direct", label: "Direct / Custom", disabled: true },
].map((option) => ({ ...option, icon: <GatewayBrandLogo brand={gatewayBrandFromIdentity(`${option.value} ${option.label}`)} size={15} /> }))

function RegistryOptionLabel({ option }: { option?: RegistryOption }) {
  if (!option) return null
  if (!option.icon) return <>{option.label}</>
  return <span className="registry-option">{option.icon}<span>{option.label}</span></span>
}

function RegistrySelectField({ label, name, defaultValue, options, disabled = false }: {
  label: string
  name: string
  defaultValue: string
  options: RegistryOption[]
  disabled?: boolean
}) {
  const [value, setValue] = useState(defaultValue)
  const selected = options.find((option) => option.value === value)
  return <div className="registry-field">
    <span className="registry-field-label">{label}</span>
    <Select name={name} value={value} disabled={disabled} onValueChange={(next) => next && setValue(next)}>
      <SelectTrigger aria-label={label} className="registry-select-trigger">
        <SelectValue><RegistryOptionLabel option={selected} /></SelectValue>
      </SelectTrigger>
      <SelectContent align="start" alignItemWithTrigger={false}>
        {options.map((option) => <SelectItem key={option.value} value={option.value} disabled={option.disabled}><RegistryOptionLabel option={option} /></SelectItem>)}
      </SelectContent>
    </Select>
  </div>
}

function RegistryInputField({ label, ...props }: React.ComponentProps<typeof Input> & { label: string }) {
  return <label className="registry-field"><span className="registry-field-label">{label}</span><Input {...props} /></label>
}

function RegistryCheckboxField({ id, name, label, defaultChecked }: { id: string; name: string; label: string; defaultChecked: boolean }) {
  return <div className="registry-checkbox-field"><Checkbox id={id} name={name} value="on" defaultChecked={defaultChecked} /><label htmlFor={id}>{label}</label></div>
}

function RegistryEditor({ registry, kind, item, busy, onClose, onSave }: { registry: ModelRegistry; kind: Exclude<RegistryKind, "model">; item?: RegistryItem; busy: boolean; onClose: () => void; onSave: (value: Record<string, unknown>) => void }) {
  const [formError, setFormError] = useState<string | null>(null)
  const runtime = kind === "runtime" ? item as ModelRuntime | undefined : undefined
  const provider = kind === "provider" ? item as ModelProvider | undefined : undefined
  const gateway = kind === "gateway" ? item as GatewayProfile | undefined : undefined
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const values = new FormData(event.currentTarget)
    try {
      const config = JSON.parse(String(values.get("config") || "{}")) as Record<string, unknown>
      if (kind === "runtime") onSave({ provider_id: values.get("provider_id"), gateway_profile_id: values.get("gateway_profile_id") === NONE_SELECT_VALUE ? null : values.get("gateway_profile_id"), name: values.get("name"), runtime_kind: values.get("runtime_kind"), brand_key: runtime?.brand_key ?? "generic", enabled: values.get("enabled") === "on", is_default: values.get("is_default") === "on", allowed_roles: split(values.get("allowed_roles")), config })
      else if (kind === "provider") onSave({ name: values.get("name"), provider_kind: values.get("provider_kind"), brand_key: provider?.brand_key ?? "generic", endpoint_url: values.get("endpoint_url") || null, auth_type: values.get("auth_type"), credential: values.get("credential") || undefined, enabled: values.get("enabled") === "on", config })
      else onSave({ name: values.get("name"), implementation: values.get("implementation"), base_url: values.get("base_url") || null, auth_type: values.get("auth_type"), credential: values.get("credential") || undefined, enabled: values.get("enabled") === "on", is_default: values.get("is_default") === "on", config })
    } catch (error) { setFormError(`参数 JSON 无效：${String(error)}`) }
  }
  const editorLabel = kind === "runtime" ? "连接" : kind === "provider" ? "提供方" : "网关"
  return <Dialog open onOpenChange={(open) => { if (!open) onClose() }}><DialogContent className="registry-editor-dialog" finalFocus={false}>
    <form className="registry-editor" onSubmit={submit}>
      <DialogHeader className="registry-editor-header"><DialogTitle>{`${item ? "编辑" : "新增"}${editorLabel}`}</DialogTitle><DialogDescription>配置会直接写入模型平台 Registry。</DialogDescription></DialogHeader>
      <div className="registry-editor-body">
        {kind === "runtime" && <RegistrySelectField label="提供方" name="provider_id" defaultValue={runtime?.provider_id ?? registry.providers[0]?.id ?? ""} options={registry.providers.map((value) => ({ value: value.id, label: value.name }))} />}
        {kind === "runtime" && <><RegistryInputField label="运行时名称" name="name" required defaultValue={runtime?.name} placeholder="例如 Foundry Production" /><div className="form-grid"><RegistrySelectField label="运行时类型" name="runtime_kind" defaultValue={runtime?.runtime_kind ?? "foundry"} options={[{ value: "foundry", label: "Microsoft Foundry" }, { value: "openai_compatible", label: "OpenAI Compatible" }]} /><RegistrySelectField label="网关" name="gateway_profile_id" defaultValue={runtime?.gateway_profile_id ?? NONE_SELECT_VALUE} options={[{ value: NONE_SELECT_VALUE, label: "提供方直连" }, ...registry.gateways.map((value) => ({ value: value.id, label: value.name }))]} /></div><RegistryInputField label="允许角色" name="allowed_roles" defaultValue={(runtime?.allowed_roles ?? ["owner", "admin", "member"]).join(", ")} /></>}
        {kind === "provider" && <><RegistryInputField label="提供方名称" name="name" required defaultValue={provider?.name} /><div className="form-grid"><RegistrySelectField label="接口协议" name="provider_kind" defaultValue={provider?.provider_kind ?? "microsoft_foundry"} options={providerKindOptions} /><AuthSelect value={provider?.auth_type} /></div><RegistryInputField label="Endpoint URL" name="endpoint_url" type="url" defaultValue={provider?.endpoint_url ?? ""} placeholder="https://..." /><CredentialField configured={provider?.credential_configured} hint={provider?.credential_hint} /></>}
        {kind === "gateway" && <><RegistryInputField label="网关名称" name="name" required defaultValue={gateway?.name} /><div className="form-grid"><RegistrySelectField label="实现" name="implementation" defaultValue={gateway?.implementation ?? "apim"} options={gatewayImplementationOptions} /><AuthSelect value={gateway?.auth_type} /></div><RegistryInputField label="Base URL" name="base_url" type="url" defaultValue={gateway?.base_url ?? ""} placeholder="https://gateway.example.com" /><CredentialField configured={gateway?.credential_configured} hint={gateway?.credential_hint} /></>}
        {(kind === "runtime" || kind === "provider" || kind === "gateway") && <label className="registry-field"><span className="registry-field-label">参数 JSON</span><Textarea name="config" rows={5} defaultValue={JSON.stringify(runtime?.config ?? provider?.config ?? gateway?.config ?? {}, null, 2)} spellCheck={false} /></label>}
        <div className="form-switches"><RegistryCheckboxField id="registry-enabled" name="enabled" label="启用" defaultChecked={runtime?.enabled ?? provider?.enabled ?? gateway?.enabled ?? true} />{(kind === "runtime" || kind === "gateway") && <RegistryCheckboxField id="registry-default" name="is_default" label="设为默认" defaultChecked={runtime?.is_default ?? gateway?.is_default ?? false} />}</div>
        {formError && <div className="registry-error">{formError}</div>}
      </div>
      <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
      <DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" />}>取消</DialogClose><Button type="submit" disabled={busy}><Save size={14} />保存配置</Button></DialogFooter>
    </form>
  </DialogContent></Dialog>
}

function AuthSelect({ value = "none" }: { value?: string }) { return <RegistrySelectField label="鉴权方式" name="auth_type" defaultValue={value} options={[{ value: "none", label: "无 / 本机登录" }, { value: "api_key", label: "API Key" }, { value: "bearer", label: "Bearer Token" }, { value: "azure_ad", label: "Azure AD" }]} /> }
function CredentialField({ configured, hint }: { configured?: boolean; hint?: string | null }) { return <RegistryInputField label="鉴权凭据" name="credential" type="password" autoComplete="new-password" placeholder={configured ? `已配置 ${hint ?? ""}，留空保持不变` : "输入后将加密保存"} /> }
function split(value: FormDataEntryValue | null) { return String(value ?? "").split(",").map((item) => item.trim()).filter(Boolean) }

function GatewayConsole({ registry, onClose }: { registry: ModelRegistry; onClose: () => void }) {
  const defaultRuntime = registry.runtimes.find((item) => item.is_default && item.enabled) ?? registry.runtimes.find((item) => item.enabled)
  const [runtimeId, setRuntimeId] = useState(defaultRuntime?.id ?? "")
  const availableModels = registry.models.filter((item) => item.runtime_id === runtimeId && item.enabled && !item.capabilities.includes("decision"))
  const defaultModel = availableModels.find((item) => item.is_default) ?? availableModels[0]
  const [modelId, setModelId] = useState(defaultModel?.id ?? "")
  const selectedRuntime = registry.runtimes.find((item) => item.id === runtimeId)
  const selectedModel = availableModels.find((item) => item.id === modelId) ?? defaultModel
  const updateRuntime = (nextRuntimeId: string | null) => {
    if (!nextRuntimeId) return
    setRuntimeId(nextRuntimeId)
    const nextModels = registry.models.filter((item) => item.runtime_id === nextRuntimeId && item.enabled && !item.capabilities.includes("decision"))
    setModelId((nextModels.find((item) => item.is_default) ?? nextModels[0])?.id ?? "")
  }
  const invoke = useMutation({ mutationFn: dataSource.invokeModel })
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const values = new FormData(event.currentTarget)
    const runtime = registry.runtimes.find((item) => item.id === runtimeId)
    const model = registry.models.find((item) => item.id === modelId)
    if (!runtime || !model) return
    invoke.mutate({
      runtime_id: runtime.id,
      model_id: model.id,
      metadata: {
        organization: values.get("organization"),
        department: values.get("department"),
        project: values.get("project"),
        agent: values.get("agent"),
        user: values.get("user"),
        workflow: values.get("workflow"),
        model: model.model_key,
        runtime: runtime.name,
        run_id: crypto.randomUUID(),
        turn_index: 1,
      },
      messages: [{ role: "user", content: values.get("prompt") }],
      stream: false,
    })
  }
  return <div className="modal-backdrop" onMouseDown={onClose}><form className="modal gateway-console" onSubmit={submit} onMouseDown={(event) => event.stopPropagation()}><div className="modal-head"><div><h2>统一网关调用测试</h2><p>请求会经过实际 Runtime Adapter，并写入完整归因 telemetry。</p></div><button type="button" className="icon-button" onClick={onClose} aria-label="关闭"><X size={16} /></button></div><div className="form-grid"><div className="gateway-select-field"><span>运行时</span><Select value={runtimeId} onValueChange={updateRuntime}><SelectTrigger aria-label="运行时" className="gateway-select-trigger"><SelectValue>{selectedRuntime?.name}{selectedRuntime?.is_default ? "（默认）" : ""}</SelectValue></SelectTrigger><SelectContent align="start" alignItemWithTrigger={false}>{registry.runtimes.filter((item) => item.enabled).map((runtime) => <SelectItem key={runtime.id} value={runtime.id}>{runtime.name}{runtime.is_default ? "（默认）" : ""}</SelectItem>)}</SelectContent></Select></div><div className="gateway-select-field"><span>模型</span><Select value={selectedModel?.id ?? ""} onValueChange={(value) => value && setModelId(value)}><SelectTrigger aria-label="模型" className="gateway-select-trigger"><SelectValue>{selectedModel?.display_name ?? "无可用模型"}</SelectValue></SelectTrigger><SelectContent align="start" alignItemWithTrigger={false}>{availableModels.map((model) => <SelectItem key={model.id} value={model.id}>{model.display_name}</SelectItem>)}</SelectContent></Select></div></div><div className="form-grid three"><label>组织<input name="organization" required defaultValue="contoso" /></label><label>部门<input name="department" required defaultValue="platform" /></label><label>项目<input name="project" required defaultValue="token-finops" /></label></div><div className="form-grid three"><label>智能体<input name="agent" required defaultValue="delivery-engineer" /></label><label>用户<input name="user" required defaultValue="operator" /></label><label>Workflow<input name="workflow" required defaultValue="runtime-smoke-test" /></label></div><label>Prompt<textarea name="prompt" rows={4} required defaultValue="Reply with exactly: RUNTIME_OK" /></label>{invoke.error && <div className="registry-error">调用失败：{String(invoke.error)}</div>}{invoke.data && <div className="gateway-result"><div><span>ADAPTER</span><strong>{invoke.data.gateway}</strong></div><div><span>MODEL</span><strong>{invoke.data.model}</strong></div><div><span>LATENCY</span><strong>{invoke.data.latency_ms}ms</strong></div><div><span>TOKENS</span><strong>{invoke.data.usage ? invoke.data.usage.input_tokens + invoke.data.usage.cached_tokens + invoke.data.usage.output_tokens : "--"}</strong></div><pre>{invoke.data.content}</pre></div>}<div className="modal-actions"><button type="button" className="ghost-button" onClick={onClose}>关闭</button><button className="primary-button" disabled={invoke.isPending || !selectedModel}><Send size={13} />{invoke.isPending ? "调用中..." : "发送请求"}</button></div></form></div>
}
