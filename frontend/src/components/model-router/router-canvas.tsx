import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react"
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  applyEdgeChanges,
  applyNodeChanges,
  useReactFlow,
  useUpdateNodeInternals,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react"
import {
  AlignHorizontalDistributeCenter,
  ArrowDown,
  ArrowRight,
  CircleDot,
  Copy,
  Cpu,
  GitBranch,
  Grid3X3,
  ListOrdered,
  Lock,
  Maximize2,
  Play,
  Redo2,
  RotateCcw,
  Search,
  Settings2,
  Trash2,
  Undo2,
  Unlock,
  X,
} from "lucide-react"
import { useIsMobile } from "../../hooks/use-mobile"
import { useLocale } from "../../locales/index"
import { ModelBrandLogo } from "../brand-logos"
import { Button } from "../ui/button"
import { Checkbox } from "../ui/checkbox"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog"
import { Input } from "../ui/input"
import { Textarea } from "../ui/textarea"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select"
import type {
  ManagedModel,
  ModelRouterDefinition,
  ModelRouterFallbackEvent,
  ModelRouterModelReference,
  ModelRouterNode,
  ModelRouterNodeType,
  ModelRouterStrategyType,
} from "../../data-sources/apim/types"
import {
  MATCH_BRANCH_LABELS,
  ROUTER_NATIVE_FALLBACK_NOTICE,
  ROUTER_NODE_LABELS,
  autoLayoutDefinition,
  cloneDefinition,
  copyName,
  createRouterEdge,
  createRouterNode,
  enabledCandidateModelIds,
  inferLayoutDirection,
  routerNodeDimensions,
  routerModelStatusLabel,
  syncStrategyModelConfigs,
  type RouterLayoutDirection,
} from "./router-definition"
import { StrategyModelEditor } from "./strategy-model-editor"
import { DecisionPromptEditor } from "./decision-prompt-editor"
import { RouterEdge } from "./router-edge"


const NODE_MIME = "application/turnstile-model-router-node"
const PROPERTIES_WIDTH_KEY = "turnstile_model_router_properties_width"
const PROPERTIES_WIDTH_DEFAULT = 312
const PROPERTIES_WIDTH_MIN = 260
const PROPERTIES_WIDTH_MAX = 420
const ROUTER_CANVAS_MIN_ZOOM = 0.1
const ROUTER_CANVAS_FIT_OPTIONS = {
  padding: 0.12,
  minZoom: ROUTER_CANVAS_MIN_ZOOM,
  maxZoom: 1.1,
} as const
function clampPropertiesWidth(value: number) {
  return Math.max(PROPERTIES_WIDTH_MIN, Math.min(PROPERTIES_WIDTH_MAX, value))
}

function propertiesWidthFromStorage() {
  const stored = Number(localStorage.getItem(PROPERTIES_WIDTH_KEY))
  return Number.isFinite(stored) && stored > 0
    ? clampPropertiesWidth(stored)
    : PROPERTIES_WIDTH_DEFAULT
}
const PALETTE_TYPES: ModelRouterNodeType[] = [
  "start",
  "condition",
  "candidate_models",
  "strategy",
  "fallback",
  "output",
]

const NODE_ICONS = {
  start: Play,
  condition: GitBranch,
  candidate_models: Cpu,
  strategy: ListOrdered,
  priority: ListOrdered,
  fallback: RotateCcw,
  output: ArrowRight,
} satisfies Record<ModelRouterNodeType, typeof Play>

type RouterFlowData = {
  routerNode: ModelRouterNode
  summary: string
  layoutDirection: RouterLayoutDirection
  outgoingHandles: Array<{
    id: string
    tone: "normal" | "exception" | "neutral"
  }>
  candidateModels: Array<{
    candidate: { model_id: string }
    model: ModelRouterModelReference | null
  }>
} & Record<string, unknown>

type RouteFlowNode = Node<RouterFlowData, "routerNode">
type RouterFlowNode = RouteFlowNode
type RouterFlowEdge = Edge<Record<string, unknown>, "routerEdge">

function outgoingHandleId(
  source: ModelRouterNode,
  target: ModelRouterNode | undefined,
  index: number,
  count: number,
) {
  if (count <= 1) return "route-source"
  if (source.type === "strategy" && target?.type === "output") return "branch-normal"
  if (target?.type === "fallback") return "branch-exception"
  return `branch-${index + 1}`
}

function branchTone(target: ModelRouterNode | undefined) {
  if (target?.type === "output") return "normal" as const
  if (target?.type === "fallback") return "exception" as const
  return "neutral" as const
}

function outgoingEdges(definition: ModelRouterDefinition, sourceId: string) {
  return definition.edges
    .filter((edge) => edge.source === sourceId)
    .map((edge, index) => ({ edge, index }))
    .sort((left, right) => {
      const nodesById = new Map(definition.nodes.map((node) => [node.id, node]))
      const rank = (targetId: string) => {
        const type = nodesById.get(targetId)?.type
        if (type === "output") return 0
        if (type === "fallback") return 2
        return 1
      }
      return rank(left.edge.target) - rank(right.edge.target) || left.index - right.index
    })
    .map(({ edge }) => edge)
}

function nodeSummary(node: ModelRouterNode, modelsById: Map<string, ModelRouterModelReference>) {
  if (node.type === "start") return "接收请求上下文"
  if (node.type === "condition") {
    const condition = node.condition
    if (!condition) return "尚未配置判断条件"
    const value = condition.operator === "exists" ? "" : ` ${String(condition.value ?? "")}`
    return `${condition.field} · ${condition.operator}${value}`
  }
  if (node.type === "candidate_models") {
    if (!node.candidates.length) return "尚未选择模型"
    const first = modelsById.get(node.candidates[0].model_id)?.display_name ?? "已不可用的模型"
    return node.candidates.length === 1 ? first : `${first} 等 ${node.candidates.length} 个模型`
  }
  if (node.type === "strategy") {
    if (node.strategy_type === "weighted") return "按权重选择候选模型"
    if (node.strategy_type === "model_based") return "由路由模型选择候选模型"
    return "按优先级依次尝试候选模型"
  }
  if (node.type === "priority") return "按优先级与权重选择"
  if (node.type === "fallback") {
    return node.fallback_on.length ? node.fallback_on.join(" · ") : "尚未配置回退条件"
  }
  return "调用选中模型并返回响应"
}

function RouterNodeCard({ data, selected }: NodeProps<RouteFlowNode>) {
  const Icon = NODE_ICONS[data.routerNode.type]
  const vertical = data.layoutDirection === "vertical"
  const candidateModels = data.candidateModels ?? []
  const showTypeBadge = data.routerNode.label !== ROUTER_NODE_LABELS[data.routerNode.type]
  return <article className={`router-flow-node ${selected ? "selected" : ""}`} data-node-type={data.routerNode.type}>
    {data.routerNode.type !== "start" && <Handle id="route-target" type="target" position={vertical ? Position.Top : Position.Left} />}
    <header className="router-node-heading">
      <span className="router-node-icon"><Icon size={16} /></span>
      <b>{data.routerNode.label}</b>
      {showTypeBadge && <small>{ROUTER_NODE_LABELS[data.routerNode.type]}</small>}
    </header>
    <div className="router-node-content">
      {data.routerNode.type !== "condition" && data.routerNode.type !== "fallback" && <p>{data.summary}</p>}
      {data.routerNode.type === "condition" && data.routerNode.condition && <code>{data.routerNode.condition.field} {data.routerNode.condition.operator} {String(data.routerNode.condition.value ?? "")}</code>}
      {data.routerNode.type === "fallback" && <div className="router-node-event-list">{data.routerNode.fallback_on.map((event) => <span key={event}>{event}</span>)}</div>}
      {data.routerNode.type === "candidate_models" && <div className="router-node-candidate-list">
        {candidateModels.map(({ candidate, model }) => <div className="router-node-candidate-item" key={candidate.model_id} data-model-id={candidate.model_id} data-model-status={model?.status ?? "unavailable"} title={candidate.model_id}>
          <span className="router-model-logo"><ModelBrandLogo familyKey={model?.family_key} identity={`${model?.display_name ?? ""} ${model?.model_key ?? ""}`} providerName={model?.provider_name ?? ""} size={15} /></span>
          <span><b>{model?.display_name ?? "已不可用的模型"}</b><small>{model?.provider_name}{model?.provider_name && model.status !== "available" ? " · " : null}{model ? routerModelStatusLabel(model.status) : "候选模型"}</small></span>
        </div>)}
        {candidateModels.length === 0 && <span className="router-node-candidate-empty">尚未选择模型</span>}
      </div>}
    </div>
    {data.outgoingHandles.map((handle, index) => <Handle
      id={handle.id}
      key={handle.id}
      type="source"
      position={vertical ? Position.Bottom : Position.Right}
      className={`router-branch-handle router-branch-${handle.tone}`}
      style={data.outgoingHandles.length > 1
        ? vertical
          ? { left: `${((index + 1) / (data.outgoingHandles.length + 1)) * 100}%` }
          : { top: `${((index + 1) / (data.outgoingHandles.length + 1)) * 100}%` }
        : undefined}
    />)}
  </article>
}

const nodeTypes = { routerNode: RouterNodeCard }
const edgeTypes = { routerEdge: RouterEdge }

function flowNodes(
  definition: ModelRouterDefinition,
  selectedNodeId: string | null,
  modelsById: Map<string, ModelRouterModelReference>,
  layoutDirection: RouterLayoutDirection,
): RouterFlowNode[] {
  const nodesById = new Map(definition.nodes.map((node) => [node.id, node]))
  return definition.nodes.map((node) => {
    const outgoing = outgoingEdges(definition, node.id)
    const terminal = node.type === "output" || node.type === "fallback"
    const handleEdges = terminal
      ? []
      : outgoing.length
        ? outgoing
        : [{ target: "" }]
    const summary = nodeSummary(node, modelsById)
    const { width } = routerNodeDimensions(node)
    return {
      id: node.id,
      type: "routerNode",
      position: node.position,
      width,
      sourcePosition: layoutDirection === "vertical" ? Position.Bottom : Position.Right,
      targetPosition: layoutDirection === "vertical" ? Position.Top : Position.Left,
      selected: node.id === selectedNodeId,
      ariaLabel: `${node.label}: ${summary}`,
      data: {
        routerNode: node,
        summary,
        layoutDirection,
        outgoingHandles: handleEdges.map((edge, index) => ({
          id: outgoingHandleId(node, nodesById.get(edge.target), index, handleEdges.length),
          tone: branchTone(nodesById.get(edge.target)),
        })),
        candidateModels: node.candidates.map((candidate) => ({
          candidate,
          model: modelsById.get(candidate.model_id) ?? null,
        })),
      },
    }
  })
}

function flowEdges(
  definition: ModelRouterDefinition,
  selectedEdgeId: string | null,
): RouterFlowEdge[] {
  const nodesById = new Map(definition.nodes.map((node) => [node.id, node]))
  return definition.edges.map((edge) => {
    const source = nodesById.get(edge.source)
    const target = nodesById.get(edge.target)
    const outgoing = outgoingEdges(definition, edge.source)
    const branchIndex = outgoing.findIndex((item) => item.id === edge.id)
    const branching = outgoing.length > 1
    const tone = branchTone(target)
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: source
        ? outgoingHandleId(source, target, branchIndex, outgoing.length)
        : "route-source",
      targetHandle: "route-target",
      label: edge.label || undefined,
      type: "routerEdge",
      selected: edge.id === selectedEdgeId,
      className: branching
        ? `router-branch-edge router-branch-${tone}`
        : "router-route-edge",
      data: { tone },
      markerEnd: { type: MarkerType.ArrowClosed },
    }
  })
}

function numberOrNull(value: string): number | null {
  if (!value.trim()) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function RouterCanvasSurface({
  definition,
  models,
  selectedNodeId,
  onSelectedNodeChange,
  onChange,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  onOpenProperties,
}: {
  definition: ModelRouterDefinition
  models: ModelRouterModelReference[]
  selectedNodeId: string | null
  onSelectedNodeChange: (nodeId: string | null) => void
  onChange: (definition: ModelRouterDefinition) => void
  canUndo: boolean
  canRedo: boolean
  onUndo: () => void
  onRedo: () => void
  onOpenProperties?: () => void
}) {
  const { fitView, screenToFlowPosition } = useReactFlow<RouterFlowNode, RouterFlowEdge>()
  const updateNodeInternals = useUpdateNodeInternals()
  const fittedSignature = useRef("")
  const pendingFitSignature = useRef("")
  const modelsById = useMemo(() => new Map(models.map((model) => [model.id, model])), [models])
  const layoutDirection = inferLayoutDirection(definition)
  const projectedNodes = useMemo(
    () => flowNodes(definition, selectedNodeId, modelsById, layoutDirection),
    [definition, layoutDirection, modelsById, selectedNodeId],
  )
  const [canvasNodes, setCanvasNodesState] = useState<RouterFlowNode[]>(projectedNodes)
  const [isDragging, setIsDragging] = useState(false)
  const [isLocked, setIsLocked] = useState(false)
  const [showGrid, setShowGrid] = useState(true)
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const canvasNodesRef = useRef<RouterFlowNode[]>(projectedNodes)
  const draggingRef = useRef(false)
  const dragFrameRef = useRef<number | null>(null)
  const pendingDragChangesRef = useRef<NodeChange<RouterFlowNode>[]>([])
  const setCanvasNodes = useCallback((next: RouterFlowNode[]) => {
    canvasNodesRef.current = next
    setCanvasNodesState(next)
  }, [])
  const edges = useMemo(
    // React Flow deletes selected edges even when the canvas is locked.
    () => flowEdges(definition, isLocked ? null : selectedEdgeId),
    [definition, isLocked, selectedEdgeId],
  )
  const nodeSignature = `${layoutDirection}|${definition.nodes.map((node) => `${node.id}:${node.candidates.map((candidate) => candidate.model_id).join(",")}`).sort().join("|")}`

  useEffect(() => {
    if (!draggingRef.current) setCanvasNodes(projectedNodes)
  }, [projectedNodes, setCanvasNodes])

  useEffect(() => () => {
    if (dragFrameRef.current !== null) cancelAnimationFrame(dragFrameRef.current)
  }, [])

  useEffect(() => {
    if (fittedSignature.current === nodeSignature || pendingFitSignature.current === nodeSignature) return
    pendingFitSignature.current = nodeSignature
    requestAnimationFrame(() => {
      updateNodeInternals(canvasNodesRef.current.map((node) => node.id))
      const fitWhenReady = (attempt: number) => {
        void fitView(ROUTER_CANVAS_FIT_OPTIONS).then((didFit) => {
          if (didFit) {
            fittedSignature.current = nodeSignature
            pendingFitSignature.current = ""
          } else if (attempt < 12) {
            requestAnimationFrame(() => fitWhenReady(attempt + 1))
          } else {
            pendingFitSignature.current = ""
          }
        })
      }
      requestAnimationFrame(() => fitWhenReady(0))
    })
  }, [canvasNodes, fitView, nodeSignature, updateNodeInternals])

  const addNode = (type: ModelRouterNodeType, position?: { x: number; y: number }) => {
    const next = createRouterNode(type, position ?? { x: 120 + definition.nodes.length * 24, y: 120 })
    onChange({ ...cloneDefinition(definition), nodes: [...definition.nodes, next] })
    onSelectedNodeChange(next.id)
  }

  const commitCanvasNodes = useCallback((nextNodes: RouterFlowNode[]) => {
    const routeNodes = nextNodes.filter((node) => node.type === "routerNode")
    const positions = new Map(routeNodes.map((node) => [node.id, node.position]))
    const retainedIds = new Set(routeNodes.map((node) => node.id))
    const nextDefinition = cloneDefinition(definition)
    nextDefinition.nodes = nextDefinition.nodes
      .filter((node) => retainedIds.has(node.id))
      .map((node) => ({ ...node, position: positions.get(node.id) ?? node.position }))
    nextDefinition.edges = nextDefinition.edges.filter(
      (edge) => retainedIds.has(edge.source) && retainedIds.has(edge.target),
    )
    if (selectedNodeId && !retainedIds.has(selectedNodeId)) onSelectedNodeChange(null)
    onChange(nextDefinition)
  }, [definition, onChange, onSelectedNodeChange, selectedNodeId])

  const handleNodesChange = useCallback((changes: NodeChange<RouterFlowNode>[]) => {
    const removals = changes.filter((change) => change.type === "remove")
    if (removals.length) {
      const nextNodes = applyNodeChanges(removals, canvasNodesRef.current)
      setCanvasNodes(nextNodes)
      commitCanvasNodes(nextNodes)
      return
    }
    const positionChanges = changes.filter((change) => change.type === "position")
    if (!positionChanges.length) return
    pendingDragChangesRef.current.push(...positionChanges)
    if (dragFrameRef.current !== null) return
    dragFrameRef.current = requestAnimationFrame(() => {
      const pending = pendingDragChangesRef.current
      pendingDragChangesRef.current = []
      dragFrameRef.current = null
      setCanvasNodes(applyNodeChanges(pending, canvasNodesRef.current))
    })
  }, [commitCanvasNodes, setCanvasNodes])

  const handleNodeDragStart = useCallback(() => {
    draggingRef.current = true
    setIsDragging(true)
  }, [])

  const handleNodeDragStop = useCallback((_event: unknown, draggedNode: RouterFlowNode) => {
    if (dragFrameRef.current !== null) {
      cancelAnimationFrame(dragFrameRef.current)
      dragFrameRef.current = null
    }
    const pending = pendingDragChangesRef.current
    pendingDragChangesRef.current = []
    let nextNodes = pending.length
      ? applyNodeChanges(pending, canvasNodesRef.current)
      : canvasNodesRef.current
    nextNodes = nextNodes.map((node) => node.id === draggedNode.id
      ? { ...node, position: draggedNode.position }
      : node)
    setCanvasNodes(nextNodes)
    draggingRef.current = false
    setIsDragging(false)
    commitCanvasNodes(nextNodes)
  }, [commitCanvasNodes, setCanvasNodes])

  const handleEdgesChange = (changes: EdgeChange<RouterFlowEdge>[]) => {
    const meaningful = changes.filter((change) => change.type === "remove")
    if (!meaningful.length) return
    const retained = new Set(applyEdgeChanges(meaningful, edges).map((edge) => edge.id))
    onChange({
      ...cloneDefinition(definition),
      edges: definition.edges.filter((edge) => retained.has(edge.id)),
    })
  }

  const handleConnect = (connection: Connection) => {
    if (!connection.source || !connection.target || connection.source === connection.target) return
    if (definition.edges.some((edge) => edge.source === connection.source && edge.target === connection.target)) return
    const sourceNode = definition.nodes.find((node) => node.id === connection.source)
    const existingOutgoing = definition.edges.filter((edge) => edge.source === connection.source)
    let defaultLabel = ""
    if (sourceNode?.type === "condition") {
      const hasMatch = existingOutgoing.some((edge) => MATCH_BRANCH_LABELS.includes(edge.label.trim().toLocaleLowerCase()))
      defaultLabel = hasMatch ? "Else" : "Match"
    }
    const edge = createRouterEdge(connection.source, connection.target, defaultLabel)
    edge.source_handle = connection.sourceHandle
    const next = { ...cloneDefinition(definition), edges: [...definition.edges, edge] }
    // A Strategy newly fed by a candidate pool stores the candidate rows it displays.
    const feedsStrategy = sourceNode?.type === "candidate_models"
      && definition.nodes.find((node) => node.id === connection.target)?.type === "strategy"
    onChange(feedsStrategy ? syncStrategyModelConfigs(next) : next)
  }

  const applyLayout = (direction: RouterLayoutDirection) => {
    onChange(autoLayoutDefinition(definition, direction))
    requestAnimationFrame(() => {
      updateNodeInternals(definition.nodes.map((node) => node.id))
      requestAnimationFrame(() => void fitView({ ...ROUTER_CANVAS_FIT_OPTIONS, maxZoom: 1.2, duration: 240 }))
    })
  }

  return <div className="router-canvas-stage">
    <div className="router-node-palette" role="toolbar" aria-label="路由节点工具栏">
      {PALETTE_TYPES.map((type) => {
        const Icon = NODE_ICONS[type]
        return <button
          className="router-palette-node"
          type="button"
          draggable
          key={type}
          title={`拖拽或点击添加 ${ROUTER_NODE_LABELS[type]}`}
          onDragStart={(event) => {
            event.dataTransfer.setData(NODE_MIME, type)
            event.dataTransfer.effectAllowed = "move"
          }}
          onClick={() => addNode(type)}
        ><Icon size={14} /><span>{ROUTER_NODE_LABELS[type]}</span></button>
      })}
      {onOpenProperties && <Button className="router-settings-trigger" type="button" variant="ghost" size="icon-sm" aria-label="路由设置" title="路由设置" onClick={onOpenProperties}><Settings2 size={14} /></Button>}
    </div>
    <ReactFlow<RouterFlowNode, RouterFlowEdge>
      nodes={canvasNodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      fitViewOptions={ROUTER_CANVAS_FIT_OPTIONS}
      minZoom={ROUTER_CANVAS_MIN_ZOOM}
      maxZoom={1.8}
      snapToGrid
      snapGrid={[16, 16]}
      onlyRenderVisibleElements={false}
      nodesDraggable={!isLocked}
      nodesConnectable={!isLocked}
      elementsSelectable={!isLocked && !isDragging}
      nodesFocusable={!isLocked && !isDragging}
      edgesFocusable={!isLocked && !isDragging}
      deleteKeyCode={["Backspace", "Delete"]}
      onNodesChange={handleNodesChange}
      onNodeDragStart={handleNodeDragStart}
      onNodeDragStop={handleNodeDragStop}
      onEdgesChange={handleEdgesChange}
      onConnect={handleConnect}
      onNodeClick={(_event, node) => { setSelectedEdgeId(null); onSelectedNodeChange(node.id) }}
      onEdgeClick={(_event, edge) => { if (isLocked) return; setSelectedEdgeId(edge.id); onSelectedNodeChange(null) }}
      onPaneClick={() => { setSelectedEdgeId(null); onSelectedNodeChange(null) }}
      onDragOver={(event) => {
        event.preventDefault()
        event.dataTransfer.dropEffect = "move"
      }}
      onDrop={(event) => {
        event.preventDefault()
        const type = event.dataTransfer.getData(NODE_MIME) as ModelRouterNodeType
        if (!PALETTE_TYPES.includes(type)) return
        addNode(type, screenToFlowPosition({ x: event.clientX, y: event.clientY }))
      }}
      defaultEdgeOptions={{ type: "routerEdge", markerEnd: { type: MarkerType.ArrowClosed } }}
      connectionLineStyle={{ stroke: "var(--brand)", strokeWidth: 1.5 }}
      proOptions={{ hideAttribution: true }}
      className={isDragging ? "is-dragging" : isLocked ? "is-locked" : undefined}
    >
      {showGrid && <Background gap={20} size={1} />}
      <Panel className="router-canvas-toolbar" position="top-right">
        <span className="router-toolbar-group">
          <button type="button" disabled={!canUndo || isLocked} onClick={onUndo} aria-label="撤销" title="撤销"><Undo2 size={14} /></button>
          <button type="button" disabled={!canRedo || isLocked} onClick={onRedo} aria-label="重做" title="重做"><Redo2 size={14} /></button>
        </span>
        <i />
        <span className="router-toolbar-group">
          <button type="button" className={layoutDirection === "horizontal" ? "active" : ""} disabled={isLocked} aria-label="从左到右排列" aria-pressed={layoutDirection === "horizontal"} onClick={() => applyLayout("horizontal")} title="从左到右排列"><ArrowRight size={14} /></button>
          <button type="button" className={layoutDirection === "vertical" ? "active" : ""} disabled={isLocked} aria-label="从上到下排列" aria-pressed={layoutDirection === "vertical"} onClick={() => applyLayout("vertical")} title="从上到下排列"><ArrowDown size={14} /></button>
          <button type="button" disabled={isLocked} onClick={() => applyLayout(layoutDirection)} aria-label="按当前方向自动布局" title="按当前方向自动布局"><AlignHorizontalDistributeCenter size={14} /></button>
        </span>
        <i />
        <span className="router-toolbar-group">
          <button type="button" onClick={() => void fitView({ ...ROUTER_CANVAS_FIT_OPTIONS, duration: 220 })} aria-label="适应画布" title="适应画布"><Maximize2 size={14} /></button>
          <button type="button" className={showGrid ? "active" : ""} aria-label="显示画布网格" aria-pressed={showGrid} onClick={() => setShowGrid((current) => !current)} title="显示画布网格"><Grid3X3 size={14} /></button>
          <button type="button" className={isLocked ? "warning" : ""} aria-label={isLocked ? "解锁画布" : "锁定画布"} aria-pressed={isLocked} onClick={() => setIsLocked((current) => !current)} title={isLocked ? "解锁画布" : "锁定画布"}>{isLocked ? <Lock size={14} /> : <Unlock size={14} />}</button>
        </span>
      </Panel>
      <Controls showInteractive={false} fitViewOptions={ROUTER_CANVAS_FIT_OPTIONS} />
    </ReactFlow>
  </div>
}

function CandidateEditor({
  node,
  models,
  displayModels = [],
  onChange,
}: {
  node: ModelRouterNode
  models: ManagedModel[]
  displayModels?: ModelRouterModelReference[]
  onChange: (node: ModelRouterNode) => void
}) {
  const [search, setSearch] = useState("")
  const candidatePoolModels = models.filter((model) => !model.capabilities.includes("decision"))
  const activeIds = new Set(candidatePoolModels.map((model) => model.id))
  const displayById = new Map(displayModels.map((model) => [model.id, model]))
  const unavailableSelectedModels = node.candidates
    .filter((candidate) => !activeIds.has(candidate.model_id))
    .map((candidate) => {
      const ref = displayById.get(candidate.model_id)
      return {
        id: candidate.model_id,
        display_name: ref?.display_name ?? candidate.model_id,
        model_key: ref?.model_key ?? candidate.model_id,
        provider_name: ref ? `${ref.provider_name} · 不可用` : "已移除 · 不可用",
        family_key: ref?.family_key ?? null,
      }
    })
  const allPickerModels = [...unavailableSelectedModels, ...candidatePoolModels]
  const candidatesById = new Map(node.candidates.map((candidate) => [candidate.model_id, candidate]))
  const normalizedSearch = search.trim().toLocaleLowerCase()
  const visibleModels = allPickerModels.filter((model) => !normalizedSearch
    || `${model.display_name} ${model.model_key} ${model.provider_name}`.toLocaleLowerCase().includes(normalizedSearch))
  const selectedCount = node.candidates.length
  return <div className="router-candidate-editor">
    <div className="router-candidate-heading"><span className="router-properties-label">候选模型</span><small>{selectedCount} / {candidatePoolModels.length} 已选</small></div>
    <label className="smh-search router-candidate-search"><Search size={13} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索模型或提供方..." aria-label="搜索候选模型" /></label>
    <div className="router-model-picker">
      {visibleModels.map((model) => {
        const candidate = candidatesById.get(model.id)
        return <div className={`router-model-option ${candidate ? "selected" : ""}`} key={model.id}>
          <label>
            <Checkbox
              checked={Boolean(candidate)}
              onCheckedChange={(checked) => {
                if (checked === true && !candidate) {
                  onChange({
                    ...node,
                    candidates: [...node.candidates, { model_id: model.id }],
                  })
                } else if (checked !== true && candidate) {
                  onChange({
                    ...node,
                    candidates: node.candidates.filter((item) => item.model_id !== model.id),
                  })
                }
              }}
            />
            <span className="router-model-logo"><ModelBrandLogo familyKey={model.family_key} identity={`${model.display_name} ${model.model_key}`} providerName={model.provider_name} size={14} /></span>
            <span className="router-model-copy"><b>{model.display_name}</b><small>{model.provider_name}</small></span>
          </label>
        </div>
      })}
      {!visibleModels.length && <p className="router-properties-empty">{allPickerModels.length ? "没有匹配的可用模型。" : "当前用户没有可用于路由的模型。"}</p>}
    </div>
  </div>
}

function RouterProperties({
  definition,
  models,
  displayModels,
  routerName,
  routerDescription,
  selectedNodeId,
  onSelectedNodeChange,
  onChange,
  onMetadataChange,
}: {
  definition: ModelRouterDefinition
  models: ManagedModel[]
  displayModels: ModelRouterModelReference[]
  routerName: string
  routerDescription: string
  selectedNodeId: string | null
  onSelectedNodeChange: (nodeId: string | null) => void
  onChange: (definition: ModelRouterDefinition) => void
  onMetadataChange: (name: string, description: string) => void
}) {
  const { locale } = useLocale()
  const selected = definition.nodes.find((node) => node.id === selectedNodeId) ?? null
  const updateNode = (node: ModelRouterNode) => {
    const next = cloneDefinition(definition)
    next.nodes = next.nodes.map((item) => item.id === node.id ? node : item)
    const enabledIds = enabledCandidateModelIds(next)
    if (!next.default_model_id || !enabledIds.includes(next.default_model_id)) {
      next.default_model_id = enabledIds[0] ?? null
    }
    onChange(node.type === "candidate_models" ? syncStrategyModelConfigs(next) : next)
  }
  const duplicateNode = () => {
    if (!selected) return
    const copy = structuredClone(selected)
    copy.id = `node-${crypto.randomUUID()}`
    copy.label = copyName(selected.label, locale)
    copy.position = { x: selected.position.x + 40, y: selected.position.y + 40 }
    onChange({ ...cloneDefinition(definition), nodes: [...definition.nodes, copy] })
    onSelectedNodeChange(copy.id)
  }
  const deleteNode = () => {
    if (!selected) return
    onChange({
      ...cloneDefinition(definition),
      nodes: definition.nodes.filter((node) => node.id !== selected.id),
      edges: definition.edges.filter((edge) => edge.source !== selected.id && edge.target !== selected.id),
    })
    onSelectedNodeChange(null)
  }
  const candidateIds = enabledCandidateModelIds(definition)

  if (!selected) {
    return <aside className="router-properties">
      <header><div><CircleDot size={15} /><b>路由设置</b></div><span>全局</span></header>
      <div className="router-properties-body">
        <label className="router-field"><span>标题</span><Input value={routerName} maxLength={120} onChange={(event) => onMetadataChange(event.target.value, routerDescription)} /></label>
        <label className="router-field"><span>描述</span><Textarea value={routerDescription} maxLength={500} rows={4} onChange={(event) => onMetadataChange(routerName, event.target.value)} /></label>
        <dl className="router-definition-facts">
          <div><dt>节点</dt><dd>{definition.nodes.length}</dd></div>
          <div><dt>连线</dt><dd>{definition.edges.length}</dd></div>
          <div><dt>候选模型</dt><dd>{candidateIds.length}</dd></div>
        </dl>
      </div>
    </aside>
  }

  const Icon = NODE_ICONS[selected.type]
  const routingModel = displayModels.find((model) => model.id === selected.routing_model_id)
  return <aside className="router-properties">
    <header><div><Icon size={15} /><b>节点属性</b></div><span>{ROUTER_NODE_LABELS[selected.type]}</span></header>
    <div className="router-properties-body">
      <label className="router-field"><span>名称</span><Input value={selected.label} maxLength={120} onChange={(event) => updateNode({ ...selected, label: event.target.value })} /></label>
      {selected.type === "condition" && <>
        <label className="router-field"><span>判断字段</span>
          <Select value={selected.condition?.field ?? "task_type"} onValueChange={(value) => updateNode({ ...selected, condition: { field: value ?? "task_type", operator: selected.condition?.operator ?? "equals", value: selected.condition?.value ?? "" } })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent align="end">
              <SelectItem value="task_type">任务类型</SelectItem>
              <SelectItem value="request_parameter">请求参数</SelectItem>
              <SelectItem value="prompt_length">问题长度</SelectItem>
              <SelectItem value="user_id">用户标识</SelectItem>
              <SelectItem value="required_capability">所需能力</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label className="router-field"><span>操作符</span>
          <Select value={selected.condition?.operator ?? "equals"} onValueChange={(value) => updateNode({ ...selected, condition: { field: selected.condition?.field ?? "task_type", operator: value ?? "equals", value: selected.condition?.value ?? "" } })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent align="end">
              <SelectItem value="equals">等于</SelectItem>
              <SelectItem value="not_equals">不等于</SelectItem>
              <SelectItem value="contains">包含</SelectItem>
              <SelectItem value="matches_any">包含任一关键词</SelectItem>
              <SelectItem value="greater_than">大于</SelectItem>
              <SelectItem value="greater_than_or_equal">大于或等于</SelectItem>
              <SelectItem value="less_than">小于</SelectItem>
              <SelectItem value="less_than_or_equal">小于或等于</SelectItem>
              <SelectItem value="exists">存在</SelectItem>
            </SelectContent>
          </Select>
        </label>
        {selected.condition?.operator !== "exists" && <label className="router-field"><span>比较值</span><Input placeholder={selected.condition?.operator === "matches_any" ? "多个关键词用逗号分隔，如：证明, 算法, theorem" : undefined} value={String(selected.condition?.value ?? "")} onChange={(event) => updateNode({ ...selected, condition: { field: selected.condition?.field ?? "task_type", operator: selected.condition?.operator ?? "equals", value: event.target.value } })} /></label>}
      </>}
      {selected.type === "candidate_models" && <CandidateEditor node={selected} models={models} displayModels={displayModels} onChange={updateNode} />}
      {selected.type === "strategy" && <>
        <label className="router-field"><span>选择策略</span>
          <Select value={selected.strategy_type ?? "priority"} onValueChange={(value) => updateNode({ ...selected, strategy_type: (value ?? "priority") as ModelRouterStrategyType, routing_model_id: value === "model_based" ? selected.routing_model_id ?? models.find((model) => model.capabilities.includes("decision"))?.id ?? candidateIds[0] ?? null : null })}>
            <SelectTrigger aria-label="选择策略"><SelectValue>{{ priority: "优先级顺序", weighted: "加权选择", model_based: "模型判路由" }[selected.strategy_type ?? "priority"]}</SelectValue></SelectTrigger>
            <SelectContent align="end">
              <SelectItem value="priority">优先级顺序</SelectItem>
              <SelectItem value="weighted">加权选择</SelectItem>
              <SelectItem value="model_based">模型判路由</SelectItem>
            </SelectContent>
          </Select>
        </label>
        {selected.strategy_type === "model_based" && <label className="router-field"><span>Routing Model</span>
          <Select value={selected.routing_model_id ?? ""} onValueChange={(value) => updateNode({ ...selected, routing_model_id: value ?? null })}>
            <SelectTrigger aria-label="选择判路由模型" title={selected.routing_model_id ?? undefined}>
              <SelectValue>{selected.routing_model_id ? <span className="registry-option">
                <ModelBrandLogo familyKey={routingModel?.family_key} identity={`${routingModel?.display_name ?? ""} ${routingModel?.model_key ?? ""}`} providerName={routingModel?.provider_name ?? ""} size={15} />
                <span>{routingModel?.display_name ?? "已不可用的模型"}</span>
                {routingModel && routingModel.status !== "available" && <small>{routerModelStatusLabel(routingModel.status)}</small>}
              </span> : "选择判路由模型"}</SelectValue>
            </SelectTrigger>
            <SelectContent align="end">
              {models.map((model) => <SelectItem key={model.id} value={model.id}><span className="registry-option"><ModelBrandLogo familyKey={model.family_key} identity={`${model.display_name} ${model.model_key}`} providerName={model.provider_name} size={15} /><span>{model.display_name}</span></span></SelectItem>)}
            </SelectContent>
          </Select>
        </label>}
        {selected.strategy_type === "model_based" && <DecisionPromptEditor definition={definition} node={selected} models={models} onChange={updateNode} />}
        <label className="router-field"><span>候选调用超时 ms</span><Input type="number" min={1} value={selected.timeout_ms ?? ""} onChange={(event) => updateNode({ ...selected, timeout_ms: numberOrNull(event.target.value) })} /></label>
        <div className="router-properties-empty">{selected.strategy_type === "weighted" ? "按候选模型权重稳定选择首选模型；失败时按权重顺序继续。" : selected.strategy_type === "model_based" ? "Routing Model 从当前候选池选择最合适的模型；判路由失败时按优先级继续。" : "通过下方排序定义模型优先级；失败时按顺序继续。"}</div>
        <StrategyModelEditor definition={definition} node={selected} models={displayModels} onChange={updateNode} compact />
      </>}
      {selected.type === "fallback" && <>
        <div className="router-field"><span>触发条件</span><div className="router-fallback-events">
          {(["error", "timeout", "rate_limit"] as const).map((event) => <label key={event}><Checkbox checked={selected.fallback_on.includes(event)} onCheckedChange={(checked) => updateNode({ ...selected, fallback_on: checked === true ? [...selected.fallback_on, event] : selected.fallback_on.filter((value) => value !== event) })} /><span>{{ error: "调用失败", timeout: "超时", rate_limit: "限流" }[event]}</span></label>)}
          {selected.fallback_on.includes("quality") && <label><Checkbox checked onCheckedChange={() => updateNode({ ...selected, fallback_on: selected.fallback_on.filter((value) => value !== "quality") })} /><span>质量不足（未支持，请移除）</span></label>}
        </div></div>
        <div className="router-properties-empty">{ROUTER_NATIVE_FALLBACK_NOTICE}</div>
      </>}
      <div className="router-node-actions">
        <Button className="router-node-action-secondary" type="button" variant="outline" onClick={duplicateNode}><Copy size={14} />复制</Button>
        <Button className="router-node-action-destructive" type="button" variant="destructive" onClick={deleteNode}><Trash2 size={14} />删除</Button>
      </div>
    </div>
  </aside>
}

export function RouterCanvas({
  definition,
  models,
  displayModels,
  routerName,
  routerDescription,
  selectedNodeId,
  onSelectedNodeChange,
  onChange,
  onMetadataChange,
}: {
  definition: ModelRouterDefinition
  models: ManagedModel[]
  displayModels: ModelRouterModelReference[]
  routerName: string
  routerDescription: string
  selectedNodeId: string | null
  onSelectedNodeChange: (nodeId: string | null) => void
  onChange: (definition: ModelRouterDefinition) => void
  onMetadataChange: (name: string, description: string) => void
}) {
  const isMobile = useIsMobile()
  const [propertiesOpen, setPropertiesOpen] = useState(false)
  const [propertiesWidth, setPropertiesWidth] = useState(propertiesWidthFromStorage)
  const definitionRef = useRef(definition)
  const historyRef = useRef<ModelRouterDefinition[]>([])
  const futureRef = useRef<ModelRouterDefinition[]>([])
  const [historyRevision, setHistoryRevision] = useState(0)
  useEffect(() => {
    setPropertiesOpen(false)
  }, [isMobile])
  const selectNode = useCallback((nodeId: string | null) => {
    onSelectedNodeChange(nodeId)
    if (isMobile && nodeId) setPropertiesOpen(true)
  }, [isMobile, onSelectedNodeChange])
  useEffect(() => {
    if (JSON.stringify(definitionRef.current) === JSON.stringify(definition)) return
    definitionRef.current = definition
    historyRef.current = []
    futureRef.current = []
    setHistoryRevision((revision) => revision + 1)
  }, [definition])
  const applyDefinition = useCallback((next: ModelRouterDefinition) => {
    const current = definitionRef.current
    if (JSON.stringify(current) === JSON.stringify(next)) return
    historyRef.current = [...historyRef.current, cloneDefinition(current)].slice(-50)
    futureRef.current = []
    definitionRef.current = next
    setHistoryRevision((revision) => revision + 1)
    onChange(next)
  }, [onChange])
  const undo = useCallback(() => {
    const previous = historyRef.current.pop()
    if (!previous) return
    futureRef.current = [cloneDefinition(definitionRef.current), ...futureRef.current].slice(0, 50)
    definitionRef.current = previous
    setHistoryRevision((revision) => revision + 1)
    onChange(previous)
  }, [onChange])
  const redo = useCallback(() => {
    const next = futureRef.current.shift()
    if (!next) return
    historyRef.current = [...historyRef.current, cloneDefinition(definitionRef.current)].slice(-50)
    definitionRef.current = next
    setHistoryRevision((revision) => revision + 1)
    onChange(next)
  }, [onChange])
  void historyRevision
  const savePropertiesWidth = (value: number) => {
    const next = clampPropertiesWidth(value)
    setPropertiesWidth(next)
    localStorage.setItem(PROPERTIES_WIDTH_KEY, String(next))
  }
  const startPropertiesResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    const startX = event.clientX
    const startWidth = propertiesWidth
    const handle = event.currentTarget
    const pointerId = event.pointerId
    let nextWidth = propertiesWidth
    const move = (moveEvent: PointerEvent) => {
      nextWidth = clampPropertiesWidth(startWidth + startX - moveEvent.clientX)
      setPropertiesWidth(nextWidth)
    }
    const finish = () => {
      window.removeEventListener("pointermove", move)
      window.removeEventListener("pointerup", finish)
      window.removeEventListener("pointercancel", finish)
      window.removeEventListener("blur", finish)
      handle.removeEventListener("lostpointercapture", finish)
      if (handle.hasPointerCapture?.(pointerId)) handle.releasePointerCapture?.(pointerId)
      document.body.classList.remove("is-resizing-runtime-pane")
      localStorage.setItem(PROPERTIES_WIDTH_KEY, String(nextWidth))
    }
    window.addEventListener("pointermove", move)
    window.addEventListener("pointerup", finish)
    window.addEventListener("pointercancel", finish)
    window.addEventListener("blur", finish)
    handle.addEventListener("lostpointercapture", finish)
    if (event.isTrusted) handle.setPointerCapture?.(pointerId)
    document.body.classList.add("is-resizing-runtime-pane")
  }
  const resizePropertiesWithKeyboard = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    const step = event.shiftKey ? 32 : 16
    if (event.key === "ArrowLeft") {
      event.preventDefault()
      savePropertiesWidth(propertiesWidth + step)
    } else if (event.key === "ArrowRight") {
      event.preventDefault()
      savePropertiesWidth(propertiesWidth - step)
    } else if (event.key === "Home") {
      event.preventDefault()
      savePropertiesWidth(PROPERTIES_WIDTH_MIN)
    } else if (event.key === "End") {
      event.preventDefault()
      savePropertiesWidth(PROPERTIES_WIDTH_MAX)
    }
  }
  const properties = <RouterProperties
    definition={definition}
    models={models}
    displayModels={displayModels}
    routerName={routerName}
    routerDescription={routerDescription}
    selectedNodeId={selectedNodeId}
    onSelectedNodeChange={onSelectedNodeChange}
    onChange={applyDefinition}
    onMetadataChange={onMetadataChange}
  />
  return <div className="router-canvas-layout" style={{ "--router-properties-width": `${propertiesWidth}px` } as CSSProperties}>
    <ReactFlowProvider>
      <RouterCanvasSurface
        definition={definition}
        models={displayModels}
        selectedNodeId={selectedNodeId}
        onSelectedNodeChange={selectNode}
        onChange={applyDefinition}
        canUndo={historyRef.current.length > 0}
        canRedo={futureRef.current.length > 0}
        onUndo={undo}
        onRedo={redo}
        onOpenProperties={isMobile ? () => {
          onSelectedNodeChange(null)
          setPropertiesOpen(true)
        } : undefined}
      />
    </ReactFlowProvider>
    {!isMobile && <button
      className="router-properties-handle"
      type="button"
      role="separator"
      aria-label="调整节点属性面板宽度"
      aria-orientation="vertical"
      aria-valuemin={PROPERTIES_WIDTH_MIN}
      aria-valuemax={PROPERTIES_WIDTH_MAX}
      aria-valuenow={propertiesWidth}
      title="拖动调整宽度，双击复位"
      onPointerDown={startPropertiesResize}
      onKeyDown={resizePropertiesWithKeyboard}
      onDoubleClick={() => savePropertiesWidth(PROPERTIES_WIDTH_DEFAULT)}
    />}
    {!isMobile && properties}
    {isMobile && <Dialog open={propertiesOpen} onOpenChange={setPropertiesOpen}>
      <DialogContent className="apim-native-route-drawer router-settings-drawer" finalFocus={false}>
        <DialogHeader className="apim-native-route-drawer-header"><DialogTitle>{selectedNodeId ? "节点属性" : "路由设置"}</DialogTitle><DialogDescription className="sr-only">{definition.nodes.find((node) => node.id === selectedNodeId)?.label ?? routerName}</DialogDescription><DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="apim-native-route-drawer-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose></DialogHeader>
        {properties}
      </DialogContent>
    </Dialog>}
  </div>
}
