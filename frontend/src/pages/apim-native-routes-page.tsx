import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react"
import { useQueries, useQuery } from "@tanstack/react-query"
import { Network, Plus, RefreshCw, Search, X } from "lucide-react"

import {
  ApimNativeRouteCreateDialog,
  type NativeRouteModelOption,
} from "../components/model-management/apim-native-route-create-dialog"
import { DeploymentResilienceEditor } from "../components/model-management/deployment-resilience-editor"
import {
  compatibleNativeRouteRuntimes,
  equivalentNativeRouteRuntimes,
  hasNativeRouteCounterpart,
  nativeRouteUpstreamIdentity,
} from "../components/model-management/apim-native-route-eligibility"
import { finopsQueries } from "../data-sources/apim/queries"
import { useIsMobile } from "../hooks/use-mobile"
import { FINOPS_NAVIGATE_EVENT } from "../lib/navigation"
import { Button } from "../components/ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../components/ui/dialog"
import { useResizablePane } from "../components/ui/use-resizable-pane"
import { useAuth } from "../providers/auth-provider"

const NATIVE_ROUTE_PANE_WIDTH_KEY = "turnstile_apim_native_route_pane_width"
const NATIVE_ROUTE_PANE_WIDTH_DEFAULT = 304
const NATIVE_ROUTE_PANE_WIDTH_MIN = 240
const NATIVE_ROUTE_PANE_WIDTH_MAX = 420

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
  const { user } = useAuth()
  const isMobile = useIsMobile()
  const registry = useQuery(finopsQueries.registry())
  const [search, setSearch] = useState("")
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null)
  const [pendingCreatedModelId, setPendingCreatedModelId] = useState<string | null>(null)
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
  const eligibleRoutes = useMemo(() => {
    if (!registry.data) return []
    return registry.data.models
      .filter((model) => model.enabled && hasNativeRouteCounterpart(model, registry.data!))
      .sort((left, right) => left.model_key.localeCompare(right.model_key))
  }, [registry.data])
  const poolQueries = useQueries({
    queries: eligibleRoutes.map((model) => finopsQueries.modelBackendPool(model.id)),
  })
  const poolQueryByModelId = new Map(
    eligibleRoutes.map((model, index) => [model.id, poolQueries[index]] as const),
  )
  const activeRoutes = eligibleRoutes.filter(
    (model) => Boolean(poolQueryByModelId.get(model.id)?.data),
  )
  const routeOptions: NativeRouteModelOption[] = (registry.data?.models ?? [])
    .filter((model) => model.enabled)
    .map((model) => {
      const equivalentRuntimes = registry.data
        ? equivalentNativeRouteRuntimes(model, registry.data)
        : []
      const compatibleRuntimes = registry.data
        ? compatibleNativeRouteRuntimes(model, registry.data)
        : []
      const equivalentRuntimeIds = new Set(equivalentRuntimes.map((runtime) => runtime.id))
      const missingDeploymentRuntimes = compatibleRuntimes.filter(
        (runtime) => !equivalentRuntimeIds.has(runtime.id),
      )
      const poolQuery = poolQueryByModelId.get(model.id)
      const replicaOwner = activeRoutes.find((route) =>
        route.id !== model.id
        && nativeRouteUpstreamIdentity(route) === nativeRouteUpstreamIdentity(model)
        && poolQueryByModelId.get(route.id)?.data?.members.some(
          (member) => member.runtime_id === model.runtime_id,
        ),
      )
      const state = replicaOwner
        ? "replica"
        : equivalentRuntimes.length < 2
          ? "ineligible"
        : poolQuery?.isPending
          ? "loading"
          : poolQuery?.isError
            ? "error"
            : poolQuery?.data
              ? "active"
              : "available"
      return {
        model,
        upstreamDeployment: nativeRouteUpstreamIdentity(model),
        equivalentRuntimeNames: equivalentRuntimes.map((runtime) => runtime.name),
        missingDeploymentRuntimeNames: missingDeploymentRuntimes.map((runtime) => runtime.name),
        replicaOwnerName: replicaOwner?.display_name ?? null,
        state,
      }
    })
  const routeStateLoading = registry.isLoading || poolQueries.some((query) => query.isPending)
  const routeStateError = registry.error ?? poolQueries.find((query) => query.error)?.error
  const routes = useMemo(() => {
    const normalized = search.trim().toLocaleLowerCase()
    return activeRoutes
      .filter((model) => !normalized || `${model.display_name} ${model.model_key} ${model.runtime_name}`.toLocaleLowerCase().includes(normalized))
  }, [activeRoutes, search])

  const handleRouteCreated = useCallback((modelId: string) => {
    onAddOpenChange(false)
    setSearch("")
    setPendingCreatedModelId(modelId)
  }, [onAddOpenChange])

  useEffect(() => {
    if (!pendingCreatedModelId || !activeRoutes.some((model) => model.id === pendingCreatedModelId)) return
    setSelectedModelId(pendingCreatedModelId)
    setPendingCreatedModelId(null)
  }, [activeRoutes, pendingCreatedModelId])

  useEffect(() => {
    if (activeRoutes.some((model) => model.id === selectedModelId)) return
    setSelectedModelId(activeRoutes[0]?.id ?? null)
  }, [activeRoutes, selectedModelId])

  useEffect(() => {
    onRouteDrawerOpenChange(false)
  }, [isMobile, onRouteDrawerOpenChange])

  const selectedModel = activeRoutes.find((model) => model.id === selectedModelId) ?? null
  const runtimeNames = selectedModel && registry.data
    ? equivalentNativeRouteRuntimes(selectedModel, registry.data).map((runtime) => runtime.name)
    : []
  const selectedPool = selectedModel
    ? poolQueryByModelId.get(selectedModel.id)?.data
    : null
  const memberCount = selectedPool?.members.length ?? runtimeNames.length

  const navigateToModelPlatform = useCallback((tab: "models" | "connections") => {
    const url = new URL(window.location.href)
    url.searchParams.set("page", "models")
    url.searchParams.set("tab", tab)
    url.searchParams.delete("runtime")
    window.history.pushState(null, "", url)
    window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT))
  }, [])

  const routeListContent = (closeOnSelect: boolean) => <>
    <div className="gateway-list-controls apim-native-route-list-controls">
      <label className="smh-search"><Search size={14} /><input value={search} disabled={routeStateLoading} onChange={(event) => setSearch(event.target.value)} placeholder="搜索后端池..." aria-label="搜索负载均衡" /></label>
    </div>
    <div className="gateway-list-scroll" role="list" aria-label="负载均衡列表">
      {routeStateLoading ? <div className="apim-native-route-drawer-state"><RefreshCw className="spin" size={18} />加载负载均衡列表</div> : routeStateError ? <div className="apim-native-route-drawer-state error">无法加载负载均衡</div> : <>{routes.map((model) => {
        const runtimes = equivalentNativeRouteRuntimes(model, registry.data!)
        return <div role="listitem" className={`gateway-list-row ${model.id === selectedModelId ? "active" : ""}`} key={model.id}>
          <button type="button" className="gateway-list-main" onClick={() => {
            setSelectedModelId(model.id)
            if (closeOnSelect) onRouteDrawerOpenChange(false)
          }}>
            <span className="gateway-list-icon"><Network size={15} /></span>
            <span className="gateway-list-copy"><b data-no-localize title={model.display_name}>{model.display_name}</b><small><code data-no-localize title={model.model_key}>{model.model_key}</code><i>·</i><span>{runtimes.length}</span><span>等价 Runtime</span></small></span>
          </button>
        </div>
      })}
      {!routes.length && <div className="smh-empty"><Network size={28} /><b>{activeRoutes.length ? "没有匹配的后端池" : "还没有后端池"}</b><span>{activeRoutes.length ? "尝试调整搜索条件。" : "从已有模型中选择并配置 APIM 后端池。"}</span></div>}
      </>}
    </div>
  </>

  return <div className="smh-workspace apim-native-routes-workspace">
    <header className="smh-page-header">
      <div><span className="smh-header-icon"><Network size={17} /></span><h1>负载均衡</h1><span>{routeStateLoading ? "—" : activeRoutes.length}</span></div>
      {user?.role === "owner" && <div className="smh-header-actions"><button type="button" disabled={!registry.data || routeStateLoading || Boolean(routeStateError)} onClick={() => onAddOpenChange(true)}><Plus size={14} />添加后端池</button></div>}
    </header>
    {routeStateLoading ? <div className="registry-loading"><RefreshCw className="spin" />加载负载均衡列表</div> : routeStateError || !registry.data ? <div className="registry-error">无法加载负载均衡：{String(routeStateError)}</div> : <div className="gateway-split apim-native-routes-layout" style={{ "--gateway-pane-width": `${paneWidth}px` } as CSSProperties}>
      {!isMobile && <aside className="gateway-list-pane apim-native-route-list">{routeListContent(false)}</aside>}
      <button
        className="runtime-pane-handle"
        type="button"
        role="separator"
        aria-label="调整负载均衡列表宽度"
        aria-orientation="vertical"
        aria-valuemin={paneMinWidth}
        aria-valuemax={paneMaxWidth}
        aria-valuenow={paneWidth}
        title="拖动调整宽度，双击复位"
        onPointerDown={startPaneResize}
        onKeyDown={resizePaneWithKeyboard}
        onDoubleClick={resetPaneWidth}
      />
      <section className="gateway-editor-pane apim-native-route-detail">
        {selectedModel ? <DeploymentResilienceEditor
          model={selectedModel}
          renderFrame={(editor, controls) => <>
            <header className="apim-native-route-toolbar">
              <b>后端池</b>
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
                </div>
              </div>
            </div>
          </>}
        /> : <div className="smh-empty"><Network size={28} /><b>选择一个后端池</b><span>配置等价部署的负载均衡与故障转移。</span></div>}
      </section>
    </div>}
    {isMobile && registry.data && <Dialog open={routeDrawerOpen} onOpenChange={onRouteDrawerOpenChange}>
      <DialogContent className="apim-native-route-drawer" finalFocus={false}>
        <DialogHeader className="apim-native-route-drawer-header"><DialogTitle>负载均衡</DialogTitle><DialogDescription className="sr-only">选择一个后端池</DialogDescription><DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="apim-native-route-drawer-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose></DialogHeader>
        <aside className="gateway-list-pane apim-native-route-list apim-native-route-drawer-list">{routeListContent(true)}</aside>
      </DialogContent>
    </Dialog>}
    {registry.data && <ApimNativeRouteCreateDialog
      open={addOpen}
      options={routeOptions}
      onClose={() => onAddOpenChange(false)}
      onCreated={handleRouteCreated}
      onManageModels={() => navigateToModelPlatform("models")}
      onManageRuntimes={() => navigateToModelPlatform("connections")}
    />}
  </div>
}
