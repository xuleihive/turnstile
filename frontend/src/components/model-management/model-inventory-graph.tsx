import { createContext, useContext, useEffect, useRef, useState } from "react"
import { Background, Handle, Panel, Position, ReactFlow, ReactFlowProvider, applyNodeChanges, useReactFlow, useViewport, type EdgeProps, type NodeProps } from "@xyflow/react"
import { Database, GitFork, List, Maximize2, Minus, Plus, Scan, X } from "lucide-react"
import type { ApimTargetTopologyList, ManagedModel, ModelRegistry, ModelRuntime } from "../../data-sources/apim/types"
import { layoutModelInventoryGraph, projectModelInventoryGraph, type InventoryGraphEdge, type InventoryGraphNode, type InventoryGraphProjection } from "../../lib/model-inventory-graph"
import { ApimLogo, FoundryLogo, MicrosoftLogo, ProviderBrandLogo, providerBrandFromMetadata, providerBrandLabel } from "../brand-logos"
import { Button } from "../ui/button"
import { ModelVendorLogo, modelVendorFromMetadata } from "./model-vendor-select"
import { modelDetailHref, navigateToModelDetailHref } from "./model-usage-detail"
import "../../styles/model-inventory-graph.css"

type Selection = { kind: "gateway" | "connection" | "model"; id: string; connectionId: string | null }
type GraphContextValue = { projection: InventoryGraphProjection; registry: ModelRegistry; selection: Selection | null }
const GraphContext = createContext<GraphContextValue | null>(null)
const handleStyle = { left: "50%", top: "50%", width: 0, height: 0, minWidth: 0, minHeight: 0, opacity: 0, border: 0, pointerEvents: "none" as const }

function GraphZoomReadout({ onReset }: { onReset: () => void }) {
  const { zoom } = useViewport()
  return <Button variant="ghost" size="sm" className="model-graph-zoom" aria-label="恢复可读缩放" title="恢复可读缩放" onClick={onReset}>{Math.round(zoom * 100)}%</Button>
}

function GraphHandles({ gateway = false, model = false }: { gateway?: boolean; model?: boolean }) {
  const style = gateway ? { ...handleStyle, left: 110, top: 46 } : handleStyle
  return <>
    {!model && <Handle id="source" type="source" position={Position.Left} style={style} isConnectable={false} />}
    {!gateway && <Handle id="target" type="target" position={Position.Left} style={style} isConnectable={false} />}
  </>
}

function runtimeAuthenticationLabel(runtime: ModelRuntime | null) {
  const strategy = runtime?.config.auth_strategy
  if (strategy === "managed_identity") return "Managed Identity"
  if (strategy === "named_value_api_key") return "API Key"
  if (strategy === "named_value_bearer") return "Bearer Token"
  if (strategy === "oauth_client_credentials") return "OAuth M2M"
  if (strategy === "none") return "无认证"
  return "未记录"
}

function graphRelated(data: InventoryGraphNode["data"], selection: Selection | null, projection: InventoryGraphProjection) {
  if (!selection) return true
  if (selection.kind === "gateway") return data.gatewayId === selection.id
  if (selection.kind === "connection") {
    const connection = projection.connections.find(item => item.id === selection.id)
    return data.connectionId === selection.id || data.kind === "gateway" && data.gatewayId === connection?.gatewayId
  }
  const connection = projection.connections.find(item => item.id === selection.connectionId)
  return data.kind === "model" ? data.modelId === selection.id && data.connectionId === selection.connectionId
    : data.connectionId === selection.connectionId || data.kind === "gateway" && data.gatewayId === connection?.gatewayId
}

function GatewayNode({ data }: NodeProps<InventoryGraphNode>) {
  const context = useContext(GraphContext)!
  const gateway = context.projection.gateways.find(item => item.id === data.gatewayId)
  if (!gateway) return null
  const models = context.projection.connections.filter(item => item.gatewayId === gateway.id).flatMap(item => item.models)
  const related = graphRelated(data, context.selection, context.projection)
  return <div className={`model-graph-gateway${related ? "" : " relation-muted"}`} data-gateway-id={gateway.entry.deployment.id}>
    <button type="button" className="model-graph-gateway-circle" aria-label={`查看网关 ${gateway.name}`} title={`${gateway.apimName} / ${gateway.name}`} aria-pressed={context.selection?.kind === "gateway" && context.selection.id === gateway.id}><ApimLogo size={44} /></button>
    <strong data-no-localize>{gateway.name}</strong><small data-no-localize>{gateway.apimName} · {new Set(models.map(model => model.id)).size}</small>
    <GraphHandles gateway />
  </div>
}

function ConnectionNode({ data }: NodeProps<InventoryGraphNode>) {
  const context = useContext(GraphContext)!
  const connection = context.projection.connections.find(item => item.id === data.connectionId)
  if (!connection) return null
  const brand = connection.runtime ? providerBrandFromMetadata(connection.runtime.brand_key,
    `${connection.runtime.name} ${connection.provider?.name ?? ""}`) : "generic"
  const related = graphRelated(data, context.selection, context.projection)
  const active = context.selection && graphRelated(data, context.selection, context.projection)
  return <div className={`model-graph-connection${related ? "" : " relation-muted"}`} data-tone={data.tone}>
    <svg width={data.radius * 2} height={data.radius * 2} viewBox={`0 0 ${data.radius * 2} ${data.radius * 2}`}>
      <circle className="model-graph-ring" cx={data.radius} cy={data.radius} r={data.radius} />
      {data.innerRadius > 0 && <circle className="model-graph-ring" cx={data.radius} cy={data.radius} r={data.innerRadius} />}
    </svg>
    <button type="button" className={`model-graph-connection-center${active ? " active" : ""}`} style={{ left: data.radius - 30, top: data.radius - 30 }}
      title={`${connection.name} · ${providerBrandLabel(brand)}`} aria-label={`查看连接 ${connection.name}`} aria-pressed={context.selection?.kind === "connection" && context.selection.id === connection.id}>
      <ProviderBrandLogo brand={brand} size={30} />
    </button>
    <small className="model-graph-connection-name" style={{ left: data.radius - 60, top: data.radius + 34 }} title={connection.name} data-no-localize>{connection.name}</small>
    <GraphHandles />
  </div>
}

function ModelNode({ data }: NodeProps<InventoryGraphNode>) {
  const context = useContext(GraphContext)!
  const connection = context.projection.connections.find(item => item.id === data.connectionId)
  const model = connection?.models.find(item => item.id === data.modelId)
  if (!connection || !model) return null
  const foundryRouter = connection.runtime?.runtime_kind === "foundry" && model.upstream_model_id === "model-router"
  const vendor = modelVendorFromMetadata(connection.runtime?.config, `${model.family_key} ${model.display_name} ${model.upstream_model_id ?? ""}`)
  const related = graphRelated(data, context.selection, context.projection)
  const active = context.selection?.kind === "model" && context.selection.id === model.id && context.selection.connectionId === connection.id
  return <div className="model-graph-model-content" data-tone={data.tone}>
    <button type="button" className={`model-graph-model${related ? "" : " relation-muted"}${active ? " selected" : ""}`}
      title={model.display_name} aria-label={`查看模型 ${model.display_name}`} aria-pressed={active} data-model-id={model.id}>
      {model.capabilities.includes("decision") ? <MicrosoftLogo size={18} /> : foundryRouter ? <FoundryLogo size={18} /> : <ModelVendorLogo value={vendor} size={18} />}<span data-no-localize>{model.display_name}</span><i data-enabled={model.enabled} />
    </button><GraphHandles model />
  </div>
}

function InventoryEdge({ id, sourceX, sourceY, targetX, targetY, data, source, target }: EdgeProps<InventoryGraphEdge>) {
  const context = useContext(GraphContext)!
  const dx = targetX - sourceX
  const dy = targetY - sourceY
  const length = Math.hypot(dx, dy)
  const sourceRadius = data?.gateway ? 46 : 30
  const targetRadius = data?.gateway ? 30 : 0
  const connectionId = data?.gateway ? target : source
  const connection = context.projection.connections.find(item => item.id === connectionId)
  const selection = context.selection
  const active = selection && (selection.kind === "gateway" ? connection?.gatewayId === selection.id
    : selection.kind === "connection" ? connectionId === selection.id
      : connectionId === selection.connectionId && (data?.gateway || target.endsWith(`:model:${selection.id}`)))
  return <line id={id} className={`model-graph-edge${data?.gateway ? " gateway" : ""}${active ? " active" : ""}${selection && !active ? " relation-muted" : ""}`}
    data-tone={data?.tone} x1={sourceX + dx / length * sourceRadius} y1={sourceY + dy / length * sourceRadius}
    x2={targetX - dx / length * targetRadius} y2={targetY - dy / length * targetRadius} />
}

const nodeTypes = { gateway: GatewayNode, connection: ConnectionNode, model: ModelNode }
const edgeTypes = { inventory: InventoryEdge }

function InventoryCanvas({ registry, models, topology }: { registry: ModelRegistry; models: ManagedModel[]; topology: ApimTargetTopologyList | null }) {
  const container = useRef<HTMLDivElement>(null)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [nodes, setNodes] = useState<InventoryGraphNode[]>([])
  const [edges, setEdges] = useState<InventoryGraphEdge[]>([])
  const [layoutVersion, setLayoutVersion] = useState(0)
  const previousSize = useRef({ width: 0, height: 0 })
  const layoutKeyRef = useRef("")
  const clickTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const { setViewport, getViewport, getNodes, zoomIn, zoomOut } = useReactFlow<InventoryGraphNode, InventoryGraphEdge>()
  const projection = projectModelInventoryGraph(registry, models, topology)
  const projectionRef = useRef(projection)
  projectionRef.current = projection
  const layoutKey = JSON.stringify(projection.connections.map(connection => ({ id: connection.id, tone: connection.tone, models: connection.models.map(model => [model.id, model.display_name]) })))
  const layoutReady = layoutKeyRef.current === layoutKey
  const duration = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 220
  const close = () => { if (clickTimer.current) clearTimeout(clickTimer.current); setSelection(null) }
  const initialFocus = (items: InventoryGraphNode[]) => {
    if ((container.current?.clientWidth ?? 0) > 760) return items
    const connections = items.filter(node => node.data.kind === "connection")
    const primary = connections.sort((left, right) =>
      items.filter(node => node.data.kind === "model" && node.parentId === right.id).length
      - items.filter(node => node.data.kind === "model" && node.parentId === left.id).length)[0]
    if (!primary) return items
    const radius = primary.data.radius
    const innerRadius = primary.data.innerRadius
    const members = items.filter(node => node.data.kind === "model" && node.parentId === primary.id
      && (!innerRadius || Math.hypot(node.position.x + Number(node.style?.width) / 2 - radius,
        node.position.y + 19 - radius) <= innerRadius + 1))
    return members.length ? members : [primary]
  }
  const focus = (items: InventoryGraphNode[], fraction = .8, animate = true) => {
    if (!container.current || !items.length) return
    const lookup = new Map([...nodes, ...items].map(node => [node.id, node]))
    const bounds = items.map(node => {
      let x = node.position.x
      let y = node.position.y
      let parent = node.parentId ? lookup.get(node.parentId) : undefined
      while (parent) { x += parent.position.x; y += parent.position.y; parent = parent.parentId ? lookup.get(parent.parentId) : undefined }
      return { x, y, width: Number(node.style?.width ?? node.measured?.width ?? node.width), height: Number(node.style?.height ?? node.measured?.height ?? node.height) }
    }).reduce((result, item) => ({
      left: Math.min(result.left, item.x), top: Math.min(result.top, item.y),
      right: Math.max(result.right, item.x + item.width), bottom: Math.max(result.bottom, item.y + item.height),
    }), { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity })
    const boundsWidth = bounds.right - bounds.left
    const boundsHeight = bounds.bottom - bounds.top
    const width = container.current.clientWidth
    const height = container.current.clientHeight
    const zoom = Math.min(width * fraction / boundsWidth, height * fraction / boundsHeight, 2.2)
    void setViewport({ x: width / 2 - (bounds.left + boundsWidth / 2) * zoom, y: height / 2 - (bounds.top + boundsHeight / 2) * zoom, zoom }, { duration: animate ? duration() : 0 })
  }
  const rebuild = () => {
    const element = container.current
    if (!element) return
    const canvas = document.createElement("canvas").getContext("2d")!
    canvas.font = `12px ${getComputedStyle(element).fontFamily}`
    const layout = layoutModelInventoryGraph(projectionRef.current, element.clientWidth, element.clientHeight, name => canvas.measureText(name).width)
    setNodes(layout.nodes)
    setEdges(layout.edges)
    setLayoutVersion(version => version + 1)
    close()
  }
  useEffect(() => {
    if (layoutKeyRef.current !== layoutKey) { layoutKeyRef.current = layoutKey; rebuild() }
  }, [layoutKey])
  useEffect(() => {
    if (!layoutVersion || !nodes.length) return
    const firstFrame = requestAnimationFrame(() => requestAnimationFrame(() => focus(initialFocus(nodes), .9, false)))
    return () => cancelAnimationFrame(firstFrame)
  }, [layoutVersion])
  useEffect(() => {
    const element = container.current!
    const observer = new ResizeObserver(() => {
      const width = element.clientWidth
      const height = element.clientHeight
      const resized = previousSize.current.width > 0 && previousSize.current.height > 0
        && (width !== previousSize.current.width || height !== previousSize.current.height)
      previousSize.current = { width, height }
      if (resized) rebuild()
    })
    observer.observe(element)
    return () => { observer.disconnect(); if (clickTimer.current) clearTimeout(clickTimer.current) }
  }, [])
  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => { if (event.key === "Escape") close() }
    window.addEventListener("keydown", handleKey)
    return () => window.removeEventListener("keydown", handleKey)
  }, [])
  const selectedConnection = projection.connections.find(item => item.id === (selection?.kind === "connection" ? selection.id : selection?.connectionId))
  const selectedModel = selection?.kind === "model" ? selectedConnection?.models.find(item => item.id === selection.id) : undefined
  const selectedGateway = projection.gateways.find(item => item.id === (selection?.kind === "gateway" ? selection.id : selectedConnection?.gatewayId))
  return <GraphContext value={{ projection, registry, selection }}><div className="model-inventory-graph" ref={container} data-testid="model-inventory-graph">
    <ReactFlow<InventoryGraphNode, InventoryGraphEdge> nodes={layoutReady ? nodes : []} edges={layoutReady ? edges : []} nodeTypes={nodeTypes} edgeTypes={edgeTypes} zIndexMode="manual"
      minZoom={.05} maxZoom={2.2} nodesConnectable={false} elementsSelectable={false} nodesFocusable={false} edgesFocusable={false}
      deleteKeyCode={null} selectNodesOnDrag={false} nodeDragThreshold={0} nodeClickDistance={5} panOnScroll zoomOnScroll={false} zoomOnDoubleClick={false}
      proOptions={{ hideAttribution: true }}
      onNodesChange={changes => setNodes(current => applyNodeChanges(changes, current))} onNodeDragStart={close}
      onNodeClick={(_event, node) => {
        if (clickTimer.current) clearTimeout(clickTimer.current)
        const next: Selection = { kind: node.data.kind, id: node.data.kind === "model" ? node.data.modelId! : node.id, connectionId: node.data.connectionId }
        clickTimer.current = setTimeout(() => setSelection(next), 250)
      }}
      onNodeDoubleClick={(_event, node) => {
        close()
        if (node.data.kind === "model") {
          navigateToModelDetailHref(modelDetailHref(node.data.modelId))
          return
        }
        const connectionId = node.data.connectionId
        const members = getNodes().filter(item => node.data.kind === "gateway" ? item.data.gatewayId === node.id : item.data.connectionId === connectionId)
        focus(members)
      }} onPaneClick={event => { close(); if (event.detail === 2) focus(getNodes(), .9) }}>
      <Background gap={20} size={1} color="var(--border)" />
      <Panel position="bottom-left" className="model-graph-controls">
        <Button variant="ghost" size="icon-sm" aria-label="缩小" title="缩小" onClick={() => void zoomOut()}><Minus size={14} /></Button>
        <GraphZoomReadout onReset={() => {
          const camera = getViewport()
          const element = container.current!
          const x = element.clientWidth / 2
          const y = element.clientHeight / 2
          void setViewport({ x: x - (x - camera.x) / camera.zoom, y: y - (y - camera.y) / camera.zoom, zoom: 1 }, { duration: duration() })
        }} />
        <Button variant="ghost" size="icon-sm" aria-label="放大" title="放大" onClick={() => void zoomIn()}><Plus size={14} /></Button>
        <Button variant="ghost" size="icon-sm" aria-label="适应画布" title="适应画布" onClick={() => { close(); focus(getNodes(), .9) }}><Maximize2 size={14} /></Button>
        <Button variant="ghost" size="icon-sm" aria-label="重新排布" title="重新排布" onClick={rebuild}><Scan size={15} /></Button>
      </Panel>
    </ReactFlow>
    {!models.length && <div className="smh-empty model-graph-empty"><List size={28} /><b>没有找到模型</b></div>}
    {selection && <aside className="model-graph-details" aria-label={selectedModel ? "模型详情" : selection.kind === "gateway" ? "网关详情" : "连接详情"}
      onKeyDown={event => { if (event.key === "Escape") close() }} tabIndex={-1}>
      <Button variant="ghost" size="icon-sm" className="model-graph-details-close" aria-label="关闭详情" title="关闭详情" onClick={close}><X size={15} /></Button>
      <header>{selectedModel ? <ModelVendorLogo value={modelVendorFromMetadata(selectedConnection?.runtime?.config, `${selectedModel.family_key} ${selectedModel.display_name}`)} size={24} />
        : selection.kind === "gateway" ? <ApimLogo size={28} /> : selectedConnection?.runtime
          ? <ProviderBrandLogo brand={providerBrandFromMetadata(selectedConnection.runtime.brand_key,
            `${selectedConnection.runtime.name} ${selectedConnection.provider?.name ?? ""}`)} size={24} />
          : <Database size={24} />}<h2 data-no-localize>{selectedModel?.display_name ?? (selection.kind === "gateway" ? selectedGateway?.name : selectedConnection?.name)}</h2></header>
      <dl>
        {selectedModel && <><div><dt>模型标识</dt><dd data-no-localize>{selectedModel.model_key}</dd></div><div><dt>上游模型</dt><dd data-no-localize>{selectedModel.upstream_model_id ?? "—"}</dd></div><div><dt>注册状态</dt><dd>{selectedModel.enabled ? "已启用" : "已停用"}</dd></div></>}
        {selection.kind !== "gateway" && <div><dt>Connection</dt><dd data-no-localize={Boolean(selectedConnection?.runtime)}>{selectedConnection?.name}</dd></div>}
        {selection.kind !== "gateway" && <div><dt>认证方式</dt><dd>{runtimeAuthenticationLabel(selectedConnection?.runtime ?? null)}</dd></div>}
        {selectedGateway ? <><div><dt>目标网关</dt><dd data-no-localize>{selectedGateway.name}</dd></div><div><dt>APIM 实例</dt><dd data-no-localize>{selectedGateway.apimName}</dd></div><div><dt>Gateway 类型</dt><dd>{selectedGateway.entry.deployment.kind === "managed" ? "Managed Gateway" : "Self-hosted Gateway"}</dd></div></>
          : <div><dt>目标网关</dt><dd>网关关系未记录</dd></div>}
        {!selectedModel && <div><dt>模型数量</dt><dd>{selection.kind === "gateway" ? new Set(projection.connections.filter(item => item.gatewayId === selectedGateway?.id).flatMap(item => item.models.map(model => model.id))).size : selectedConnection?.models.length}</dd></div>}
      </dl>
    </aside>}
  </div></GraphContext>
}

export function ModelInventoryGraph(props: { registry: ModelRegistry; models: ManagedModel[]; topology: ApimTargetTopologyList | null }) {
  return <ReactFlowProvider><InventoryCanvas {...props} /></ReactFlowProvider>
}