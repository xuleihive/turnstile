import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { ChevronRight, Code2, LoaderCircle, Network, Plus, RefreshCw, Search, X } from "lucide-react"

import { ClientSampleCode } from "../components/finops/application-client-quickstart"
import {
  ApimNativeRouteCreateDialog,
  type NativeRouteModelOption,
} from "../components/model-management/apim-native-route-create-dialog"
import {
  DeploymentResilienceEditor,
  PENDING_SUBMISSIONS_STORAGE_KEY,
  readPendingBackendPoolSubmissions,
  removePendingBackendPoolSubmission,
  subscribePendingBackendPoolSubmissions,
  upsertPendingBackendPoolSubmission,
  type PendingBackendPoolSubmission,
} from "../components/model-management/deployment-resilience-editor"
import { finopsKeys, finopsQueries } from "../data-sources/apim/queries"
import type {
  GatewayBackendPoolSummary,
  GatewayPublication,
  GatewayPublicationStatus,
} from "../data-sources/apim/types"
import { useIsMobile } from "../hooks/use-mobile"
import {
  clientSampleFor,
  modelSampleOperations,
  type ClientSampleOperation,
} from "../lib/application-client-samples"
import { Button } from "../components/ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select"
import { useResizablePane } from "../components/ui/use-resizable-pane"
import { useAuth } from "../providers/auth-provider"

const NATIVE_ROUTE_PANE_WIDTH_KEY = "turnstile_apim_native_route_pane_width"
const NATIVE_ROUTE_PANE_WIDTH_DEFAULT = 304
const NATIVE_ROUTE_PANE_WIDTH_MIN = 240
const NATIVE_ROUTE_PANE_WIDTH_MAX = 420
const SELECTED_MODEL_STORAGE_KEY = "turnstile_apim_native_route_selected_model_id"

const TERMINAL_PENDING_STATUSES = new Set<GatewayPublicationStatus>([
  "active",
  "failed",
  "awaiting_authorization",
  "rolled_back",
  "superseded",
])

let cachedSelectedModelId: string | null = null

function readSelectedModelId(): string | null {
  if (cachedSelectedModelId) return cachedSelectedModelId
  try {
    cachedSelectedModelId = sessionStorage.getItem(SELECTED_MODEL_STORAGE_KEY)
  } catch {
    cachedSelectedModelId = null
  }
  return cachedSelectedModelId
}

function writeSelectedModelId(modelId: string | null) {
  cachedSelectedModelId = modelId
  try {
    if (modelId) sessionStorage.setItem(SELECTED_MODEL_STORAGE_KEY, modelId)
    else sessionStorage.removeItem(SELECTED_MODEL_STORAGE_KEY)
  } catch {
    // Ignore storage write failures
  }
}

function PendingPublicationWatcher({
  submission,
  hasPool,
  onPublicationChange,
  onSettled,
}: {
  submission: PendingBackendPoolSubmission
  hasPool: boolean
  onPublicationChange: (modelId: string, publication: GatewayPublication) => void
  onSettled: (modelId: string, action: "configure" | "remove") => void
}) {
  const queryClient = useQueryClient()
  const publication = useQuery(finopsQueries.gatewayPublication(submission.publication.id))
  const refreshingRef = useRef<string | null>(null)
  const latest = publication.data ?? submission.publication
  const action = submission.action ?? "configure"

  useEffect(() => {
    if (!publication.data) return
    if (
      publication.data.status !== submission.publication.status
      || publication.data.error_message !== submission.publication.error_message
    ) {
      onPublicationChange(submission.modelId, publication.data)
    }
  }, [
    onPublicationChange,
    publication.data,
    submission.modelId,
    submission.publication.error_message,
    submission.publication.status,
  ])

  useEffect(() => {
    if (latest.status !== "active") return
    if (action === "remove") {
      queryClient.setQueryData<GatewayBackendPoolSummary[]>(
        finopsKeys.modelBackendPools,
        (current) => current?.filter((item) => item.model_id !== submission.modelId),
      )
      queryClient.setQueryData(finopsKeys.modelBackendPool(submission.modelId), null)
    }
    if (refreshingRef.current !== latest.id) {
      refreshingRef.current = latest.id
      void Promise.all([
        ...(action === "remove"
          ? []
          : [queryClient.invalidateQueries({ queryKey: finopsKeys.modelBackendPool(submission.modelId) })]),
        queryClient.invalidateQueries({ queryKey: finopsKeys.modelBackendPools }),
        queryClient.invalidateQueries({ queryKey: finopsKeys.modelBackendPoolCandidates(submission.modelId) }),
        queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayPublications }),
      ]).then(() => {
        onSettled(submission.modelId, action)
      })
      if (action === "remove") {
        onSettled(submission.modelId, action)
      }
      return
    }
    const expectedHasPool = action !== "remove"
    if (hasPool === expectedHasPool) {
      onSettled(submission.modelId, action)
    }
  }, [action, hasPool, latest.id, latest.status, onSettled, queryClient, submission.modelId])

  return null
}

export function ApimNativeRoutesPage({
  routeDrawerOpen,
  onRouteDrawerOpenChange,
  addOpen,
  onAddOpenChange,
}: {
  routeDrawerOpen: boolean
  onRouteDrawerOpenChange: (open: boolean) => void
  addOpen: boolean
  onAddOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const isMobile = useIsMobile()
  const registry = useQuery(finopsQueries.registry())
  const pools = useQuery(finopsQueries.modelBackendPools())
  const [search, setSearch] = useState("")
  const [selectedModelId, setSelectedModelIdState] = useState<string | null>(readSelectedModelId)
  const [pendingCreatedModelId, setPendingCreatedModelId] = useState<string | null>(null)
  const [pendingSubmissions, setPendingSubmissions] = useState<Record<string, PendingBackendPoolSubmission>>(
    readPendingBackendPoolSubmissions,
  )
  useEffect(() => subscribePendingBackendPoolSubmissions(() => {
    setPendingSubmissions(readPendingBackendPoolSubmissions())
  }), [])
  const selectModel = useCallback((modelId: string | null) => {
    writeSelectedModelId(modelId)
    setSelectedModelIdState(modelId)
  }, [])
  const {
    width: paneWidth,
    minWidth: paneMinWidth,
    maxWidth: paneMaxWidth,
    startResize: startPaneResize,
    resizeWithKeyboard: resizePaneWithKeyboard,
    resetWidth: resetPaneWidth,
  } = useResizablePane({
    storageKey: NATIVE_ROUTE_PANE_WIDTH_KEY,
    defaultWidth: NATIVE_ROUTE_PANE_WIDTH_DEFAULT,
    minWidth: NATIVE_ROUTE_PANE_WIDTH_MIN,
    maxWidth: NATIVE_ROUTE_PANE_WIDTH_MAX,
  })
  const managedModels = useMemo(() => {
    if (!registry.data) return []
    return registry.data.models.filter((model) => model.enabled
      && !model.capabilities.includes("image_generation") && !model.capabilities.includes("decision")
      && registry.data!.runtimes.some(runtime => runtime.id === model.runtime_id
        && runtime.enabled && runtime.config.control_plane_managed === true
        && registry.data!.gateways.some(gateway => gateway.id === runtime.gateway_profile_id
          && gateway.implementation === "apim")))
      .sort((left, right) => left.model_key.localeCompare(right.model_key))
  }, [registry.data])
  const poolByModelId = new Map((pools.data ?? []).map(item => [item.model_id, item.pool] as const))
  const isRemovedModel = (modelId: string) =>
    pendingSubmissions[modelId]?.action === "remove"
    && pendingSubmissions[modelId]?.publication.status === "active"
  const activeRoutes = managedModels.filter(
    model => !isRemovedModel(model.id) && (
      poolByModelId.has(model.id)
      || Boolean(pendingSubmissions[model.id] && pendingSubmissions[model.id].action !== "remove")
    ),
  )
  const routeOptions: NativeRouteModelOption[] = managedModels.map(model => ({
    model,
    state: !isRemovedModel(model.id) && (
      poolByModelId.has(model.id)
      || Boolean(pendingSubmissions[model.id] && pendingSubmissions[model.id].action !== "remove")
    )
      ? "active"
      : "available",
  }))
  const routeStateLoading = registry.isLoading || pools.isLoading
  const routeStateError = registry.error ?? pools.error
  const routes = useMemo(() => {
    const normalized = search.trim().toLocaleLowerCase()
    return activeRoutes.filter(model => !normalized
      || `${model.display_name} ${model.model_key} ${model.runtime_name}`.toLocaleLowerCase().includes(normalized))
  }, [activeRoutes, search])

  const handleRouteCreated = useCallback((modelId: string) => {
    onAddOpenChange(false)
    setSearch("")
    selectModel(modelId)
    setPendingCreatedModelId(modelId)
  }, [onAddOpenChange, selectModel])

  const handleRouteSubmitted = useCallback((submission: PendingBackendPoolSubmission) => {
    onAddOpenChange(false)
    setSearch("")
    upsertPendingBackendPoolSubmission(submission)
    selectModel(submission.modelId)
    setPendingCreatedModelId(submission.modelId)
  }, [onAddOpenChange, selectModel])

  const handlePendingPublicationChange = useCallback((modelId: string, publication: GatewayPublication) => {
    const existing = readPendingBackendPoolSubmissions()[modelId]
    if (!existing) return
    upsertPendingBackendPoolSubmission({
      ...existing,
      publication,
    })
  }, [])

  const handlePendingSettled = useCallback((modelId: string, action: "configure" | "remove") => {
    const freshPools = queryClient.getQueryData<GatewayBackendPoolSummary[]>(finopsKeys.modelBackendPools)
    const existsInFresh = freshPools?.some((item) => item.model_id === modelId) ?? false
    if (action === "remove" ? existsInFresh : !existsInFresh) return
    removePendingBackendPoolSubmission(modelId)
  }, [queryClient])

  useEffect(() => {
    if (!pendingCreatedModelId || !activeRoutes.some((model) => model.id === pendingCreatedModelId)) return
    selectModel(pendingCreatedModelId)
    setPendingCreatedModelId(null)
  }, [activeRoutes, pendingCreatedModelId, selectModel])

  useEffect(() => {
    if (activeRoutes.some((model) => model.id === selectedModelId)) return
    selectModel(activeRoutes[0]?.id ?? null)
  }, [activeRoutes, selectModel, selectedModelId])

  useEffect(() => {
    onRouteDrawerOpenChange(false)
  }, [isMobile, onRouteDrawerOpenChange])

  const selectedModel = activeRoutes.find((model) => model.id === selectedModelId)
    ?? activeRoutes[0]
    ?? null
  const selectedPool = selectedModel
    ? poolByModelId.get(selectedModel.id)
    : null
  const selectedPending = selectedModel
    ? pendingSubmissions[selectedModel.id] ?? null
    : null
  const memberCount = (selectedPending && selectedPending.action !== "remove"
    ? selectedPending.draft.members.length
    : undefined) ?? selectedPool?.members.length ?? selectedPending?.draft.members.length ?? 0
  const selectedRuntime = selectedModel
    ? registry.data?.runtimes.find((runtime) => runtime.id === selectedModel.runtime_id)
    : undefined
  const selectedGateway = selectedRuntime
    ? registry.data?.gateways.find((gateway) => gateway.id === selectedRuntime.gateway_profile_id)
    : undefined
  const selectedTarget = selectedGateway
    ? registry.data?.apim_target_topology?.items.find(
      (item) => item.target.gateway_profile_id === selectedGateway.id,
    )
    : undefined
  const baseUrl = (selectedTarget?.gateway_endpoint ?? selectedGateway?.base_url ?? "").replace(/\/+$/, "")
  const [selectedOperation, setSelectedOperation] = useState<ClientSampleOperation>("chat_completions")
  const { defaultOperation, operations } = useMemo(
    () => selectedModel
      ? modelSampleOperations(selectedModel, selectedRuntime)
      : { defaultOperation: "chat_completions" as const, operations: [] },
    [selectedModel, selectedRuntime],
  )
  const operation = operations.find((item) => item.id === selectedOperation)?.id ?? defaultOperation

  const routeListContent = (closeOnSelect: boolean) => <>
    <div className="router-list-controls apim-native-route-list-controls">
      <label className="smh-search"><Search size={14} /><input value={search} disabled={routeStateLoading} onChange={(event) => setSearch(event.target.value)} placeholder="搜索后端池..." aria-label="搜索 APIM 后端池" /></label>
    </div>
    <div className="router-list-scroll" role="list" aria-label="APIM 后端池列表">
      {routeStateLoading ? <div className="apim-native-route-drawer-state"><RefreshCw className="spin" size={18} />加载 APIM 后端池列表</div> : routeStateError ? <div className="apim-native-route-drawer-state error">无法加载 APIM 后端池</div> : <>{routes.map((model) => {
        const pending = pendingSubmissions[model.id]
        const pendingStatus = pending?.publication.status ?? null
        const pendingRunning = Boolean(pending && pendingStatus && !TERMINAL_PENDING_STATUSES.has(pendingStatus))
        const pendingFailed = pendingStatus === "failed" || pendingStatus === "rolled_back"
        const members = pending && pending.action !== "remove"
          ? pending.draft.members
          : poolByModelId.get(model.id)?.members ?? pending?.draft.members ?? []
        return <div role="listitem" className={`router-list-row ${model.id === selectedModelId ? "active" : ""}`} key={model.id}>
          <button type="button" className="router-list-main" onClick={() => {
            selectModel(model.id)
            if (closeOnSelect) onRouteDrawerOpenChange(false)
          }}>
            <span className="router-list-icon">{pendingRunning ? <LoaderCircle className="spin" size={15} /> : <Network size={15} />}</span>
            <span className="router-list-copy"><b data-no-localize title={model.display_name}>{model.display_name}</b><small><code data-no-localize title={model.model_key}>{model.model_key}</code><i>·</i><span>{members.length}</span><span>个部署</span>{(pendingRunning || pendingFailed) && <><i>·</i><span>{pendingRunning ? "正在发布" : "发布失败"}</span></>}</small></span>
          </button>
        </div>
      })}
      {!routes.length && <div className="smh-empty"><Network size={28} /><b>{activeRoutes.length ? "没有匹配的后端池" : "还没有后端池"}</b><span>{activeRoutes.length ? "尝试调整搜索条件。" : "从已有模型中选择并配置 APIM 后端池。"}</span></div>}
      </>}
    </div>
  </>

  return <div className="smh-workspace apim-native-routes-workspace">
    {Object.values(pendingSubmissions).map((submission) => <PendingPublicationWatcher
      key={`${submission.modelId}:${submission.publication.id}`}
      submission={submission}
      hasPool={poolByModelId.has(submission.modelId)}
      onPublicationChange={handlePendingPublicationChange}
      onSettled={handlePendingSettled}
    />)}
    <header className="smh-page-header">
      <div><span className="smh-header-icon"><Network size={17} /></span><h1>模型负载均衡</h1><span>{routeStateLoading ? "—" : activeRoutes.length}</span></div>
      {user?.role === "owner" && <div className="smh-header-actions"><button type="button" disabled={!registry.data || routeStateLoading || Boolean(routeStateError)} onClick={() => onAddOpenChange(true)}><Plus size={14} />添加后端池</button></div>}
    </header>
    {routeStateLoading ? <div className="registry-loading"><RefreshCw className="spin" />加载 APIM 后端池列表</div> : routeStateError || !registry.data ? <div className="registry-error">无法加载 APIM 后端池：{String(routeStateError)}</div> : <div className="router-split apim-native-routes-layout" style={{ "--router-pane-width": `${paneWidth}px` } as CSSProperties}>
      {!isMobile && <aside className="router-list-pane apim-native-route-list">{routeListContent(false)}</aside>}
      <button
        className="runtime-pane-handle"
        type="button"
        role="separator"
        aria-label="调整 APIM 后端池列表宽度"
        aria-orientation="vertical"
        aria-valuemin={paneMinWidth}
        aria-valuemax={paneMaxWidth}
        aria-valuenow={paneWidth}
        title="拖动调整宽度，双击复位"
        onPointerDown={startPaneResize}
        onKeyDown={resizePaneWithKeyboard}
        onDoubleClick={resetPaneWidth}
      />
      <section className="router-editor-pane apim-native-route-detail">
        {selectedModel ? <DeploymentResilienceEditor
          key={selectedModel.id}
          model={selectedModel}
          initialDraft={selectedPending && selectedPending.action !== "remove" ? selectedPending.draft : null}
          initialPublicationId={selectedPending?.publication.id ?? null}
          initialPublicationAction={selectedPending?.action ?? null}
          onSubmitted={handleRouteSubmitted}
          renderFrame={(editor, controls) => <>
            <header className="apim-native-route-toolbar">
              <b data-no-localize title={selectedModel.display_name}>{selectedModel.display_name}</b>
              <div className="apim-native-route-toolbar-actions">{controls}</div>
            </header>
            <div className="runtime-detail-scroll apim-native-route-scroll">
              <div className="runtime-detail-grid apim-native-route-detail-grid">
                <div className="runtime-detail-main apim-native-route-main">
                  <section className="runtime-detail-hero apim-native-route-hero">
                    <dl className="runtime-detail-facts apim-native-route-facts">
                      <div className="runtime-detail-fact"><dt>数据面</dt><dd>Azure API Management</dd></div>
                      <div className="runtime-detail-fact"><dt>上游 Deployment</dt><dd data-no-localize>{selectedModel.upstream_model_id ?? selectedModel.model_key}</dd></div>
                      <div className="runtime-detail-fact"><dt>成员</dt><dd>{memberCount}</dd></div>
                      <div className="runtime-detail-fact"><dt>作用范围</dt><dd>模型级 · 直调及所有 Router</dd></div>
                    </dl>
                  </section>
                  <section className="runtime-detail-sidecard apim-native-route-config-card">{editor}</section>
                  <details className="application-card application-quickstart-card apim-native-route-quickstart-card">
                    <summary className="application-card-head"><div><Code2 size={14} /><h2>客户端接入</h2></div><span><span>示例代码</span><ChevronRight size={14} aria-hidden="true" /></span></summary>
                    <p className="apim-native-route-quickstart-note">
                      <span>客户端调用零改动，继续使用模型标识</span>{" "}
                      <code data-no-localize>{selectedModel.model_key}</code>
                    </p>
                    {baseUrl ? <>
                      <div className="application-quickstart-controls apim-native-route-quickstart-controls">
                        <label><span>接口</span><Select value={operation} items={operations.map((item) => ({ value: item.id, label: `${item.label} · ${item.path}` }))} onValueChange={(value) => { if (value) setSelectedOperation(value) }}>
                          <SelectTrigger className="application-quickstart-model-select" aria-label="示例接口"><SelectValue /></SelectTrigger>
                          <SelectContent>{operations.map((item) => <SelectItem key={item.id} value={item.id}>{item.label} · {item.path}</SelectItem>)}</SelectContent>
                        </Select></label>
                      </div>
                      <ClientSampleCode endpoint={baseUrl} sampleForLanguage={language => clientSampleFor(language, baseUrl, selectedModel.model_key, operation)} />
                    </> : <div className="application-card-empty"><Code2 size={17} />该 APIM 尚未提供可用的 API Base URL。</div>}
                  </details>
                </div>
              </div>
            </div>
          </>}
        /> : <div className="smh-empty"><Network size={28} /><b>选择一个后端池</b><span>配置等价部署的负载均衡与故障转移。</span></div>}
      </section>
    </div>}
    {isMobile && registry.data && <Dialog open={routeDrawerOpen} onOpenChange={onRouteDrawerOpenChange}>
      <DialogContent className="apim-native-route-drawer" finalFocus={false}>
        <DialogHeader className="apim-native-route-drawer-header"><DialogTitle>模型负载均衡</DialogTitle><DialogDescription className="sr-only">选择一个后端池</DialogDescription><DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="apim-native-route-drawer-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose></DialogHeader>
        <aside className="router-list-pane apim-native-route-list apim-native-route-drawer-list">{routeListContent(true)}</aside>
      </DialogContent>
    </Dialog>}
    {registry.data && <ApimNativeRouteCreateDialog
      open={addOpen}
      options={routeOptions}
      onClose={() => onAddOpenChange(false)}
      onCreated={handleRouteCreated}
      onSubmitted={handleRouteSubmitted}
    />}
  </div>
}
