import { graphlib, layout } from "@dagrejs/dagre"

import type {
  ManagedModel,
  ModelRouter,
  ModelRouterDefinition,
  ModelRouterEdge,
  ModelRouterModelReference,
  ModelRouterNode,
  ModelRouterNodeType,
  ModelRouterStrategyModel,
} from "../../data-sources/apim/types"
import type { LocalePreference } from "../../locales/index"

export type RouterLayoutDirection = "horizontal" | "vertical"
export const ROUTER_GRAPH_PRESENTATION_VERSION = 6

const COPY_SUFFIX: Record<LocalePreference, string> = {
  "zh-CN": "副本",
  "zh-TW": "副本",
  en: "copy",
  ja: "コピー",
  ko: "사본",
}

/** A prefilled name is stored data, which the page translator never rewrites. */
export function copyName(name: string, locale: LocalePreference) {
  return `${name} ${COPY_SUFFIX[locale]}`
}

export const ROUTER_NODE_WIDTH = 240
export const ROUTER_NODE_HEIGHT = 88
export const ROUTER_CONDITION_NODE_HEIGHT = 96
export const ROUTER_DETAIL_NODE_HEIGHT = 88
export const ROUTER_CANDIDATE_NODE_WIDTH = 300
export const ROUTER_CANDIDATE_NODE_MIN_HEIGHT = 134
export const ROUTER_CANDIDATE_ROW_HEIGHT = 44
export const ROUTER_DEFAULT_ATTEMPT_TIMEOUT_MS = 30_000
// Condition branch labels the router compiler reads as the match outcome.
export const MATCH_BRANCH_LABELS = ["match", "matched", "true", "yes", "匹配"]
export const ROUTER_NATIVE_FALLBACK_NOTICE = "经 APIM 网关调用的 Router 不支持 Fallback，包含 Fallback 时会返回 503；Fallback 只在平台内调用时生效。"

export function resolveRouterDisplayModels(
  models: ReadonlyArray<Pick<ManagedModel,
    "id" | "model_key" | "display_name" | "provider_name" | "family_key" | "enabled"
  >>,
  references: readonly ModelRouterModelReference[] = [],
): ModelRouterModelReference[] {
  const resolved = new Map(references.map(model => [model.id, model]))
  for (const model of models) {
    resolved.set(model.id, {
      id: model.id, model_key: model.model_key, display_name: model.display_name,
      provider_name: model.provider_name, family_key: model.family_key,
      status: model.enabled ? "available" : "disabled",
    })
  }
  return [...resolved.values()]
}

export function routerModelStatusLabel(status?: ModelRouterModelReference["status"]) {
  return status === "available" ? "" : status === "removed" ? "已移除"
    : status === "disabled" ? "已停用" : "已不可用的模型"
}

export function routerNodeDimensions(
  node: Pick<ModelRouterNode, "type" | "candidates">,
) {
  if (node.type !== "candidate_models") {
    return {
      width: ROUTER_NODE_WIDTH,
      height: node.type === "condition"
        ? ROUTER_CONDITION_NODE_HEIGHT
        : node.type === "strategy" || node.type === "fallback"
          ? ROUTER_DETAIL_NODE_HEIGHT
          : ROUTER_NODE_HEIGHT,
    }
  }
  return {
    width: ROUTER_CANDIDATE_NODE_WIDTH,
    height: Math.max(
      ROUTER_CANDIDATE_NODE_MIN_HEIGHT,
      90 + node.candidates.length * ROUTER_CANDIDATE_ROW_HEIGHT,
    ),
  }
}

export const ROUTER_NODE_LABELS: Record<ModelRouterNodeType, string> = {
  start: "Start",
  condition: "Condition",
  candidate_models: "Candidate Models",
  strategy: "Strategy",
  priority: "Priority",
  fallback: "Fallback",
  output: "Output",
}

export function cloneDefinition(definition: ModelRouterDefinition): ModelRouterDefinition {
  return structuredClone(definition)
}

export function createRouterNode(
  type: ModelRouterNodeType,
  position = { x: 120, y: 120 },
): ModelRouterNode {
  return {
    id: `node-${crypto.randomUUID()}`,
    type,
    label: ROUTER_NODE_LABELS[type],
    position,
    condition: type === "condition"
      ? { field: "task_type", operator: "equals", value: "general" }
      : null,
    candidates: [],
    strategy_type: type === "strategy" ? "priority" : null,
    model_configs: [],
    routing_model_id: null,
    decision_prompt: null,
    max_prompt_chars: 4_000,
    fallback_on: type === "fallback" ? ["error", "timeout", "rate_limit"] : [],
    timeout_ms: type === "strategy" ? ROUTER_DEFAULT_ATTEMPT_TIMEOUT_MS : null,
  }
}

export function createRouterEdge(source: string, target: string, label = ""): ModelRouterEdge {
  return {
    id: `edge-${crypto.randomUUID()}`,
    source,
    target,
    source_handle: null,
    label,
  }
}

export function normalizeRouterDefinition(
  definition: ModelRouterDefinition,
): ModelRouterDefinition {
  const next = cloneDefinition(definition)
  // TODO: 疑似废弃补丁，待确认：仅在 API 与所有持久化 Router 都不再返回 schema v1
  // candidate priority/weight 字段后，删除 legacyVersion 分支与节点合成逻辑。
  const legacyVersion = Number(
    (definition as ModelRouterDefinition & { schema_version?: number }).schema_version ?? 1,
  )
  const synthesizedStrategyIds = new Set<string>()
  for (const condition of next.nodes.filter((node) => node.type === "condition")) {
    const incoming = next.edges.filter((edge) => edge.target === condition.id)
    const outgoing = next.edges.filter((edge) => edge.source === condition.id)
    const labels = new Set(outgoing.map((edge) => edge.label.trim().toLocaleLowerCase()))
    const hasElse = [...labels].some((label) => ["else", "default", "false", "no", "未匹配"].includes(label))
    // An authored Match branch is an incomplete condition, not a legacy pass-through.
    const hasMatch = [...labels].some((label) => MATCH_BRANCH_LABELS.includes(label))
    if (incoming.length !== 1 || outgoing.length !== 1 || hasElse || hasMatch) continue
    incoming[0].target = outgoing[0].target
    incoming[0].label = ""
    next.edges = next.edges.filter((edge) => edge.id !== outgoing[0].id)
    next.nodes = next.nodes.filter((node) => node.id !== condition.id)
  }
  for (const node of next.nodes) {
    node.decision_prompt ??= null
    node.max_prompt_chars ??= 4_000
    if (node.type !== "priority") continue
    node.type = "strategy"
    node.label = "Strategy"
    node.strategy_type = "priority"
    node.model_configs ??= []
    node.routing_model_id = null
  }

  const usedNodeIds = new Set(next.nodes.map((node) => node.id))
  const usedEdgeIds = new Set(next.edges.map((edge) => edge.id))
  const uniqueId = (base: string, used: Set<string>) => {
    let candidate = base.slice(0, 64)
    let suffix = 2
    while (used.has(candidate)) {
      const ending = `-${suffix}`
      candidate = `${base.slice(0, 64 - ending.length)}${ending}`
      suffix += 1
    }
    used.add(candidate)
    return candidate
  }
  let nodesById = new Map(next.nodes.map((node) => [node.id, node]))
  for (const candidatePool of next.nodes.filter(
    (node) => node.type === "candidate_models",
  )) {
    const legacyCandidates = candidatePool.candidates as Array<
      ModelRouterStrategyModel & { enabled?: boolean }
    >
    const poolCandidates = legacyCandidates.filter(
      (candidate) => candidate.enabled !== false,
    )
    candidatePool.candidates = poolCandidates.map((candidate) => ({
      model_id: candidate.model_id,
    }))
    const outgoing = next.edges.filter((edge) => edge.source === candidatePool.id)
    let strategy = outgoing
      .map((edge) => nodesById.get(edge.target))
      .find((node) => node?.type === "strategy")
    const policyEdges = outgoing.filter((edge) => {
      const targetType = nodesById.get(edge.target)?.type
      return targetType === "output" || targetType === "fallback"
    })
    if (!strategy && policyEdges.length) {
      strategy = createRouterNode("strategy", {
        x: candidatePool.position.x + ROUTER_CANDIDATE_NODE_WIDTH + 80,
        y: candidatePool.position.y,
      })
      strategy.id = uniqueId(`${candidatePool.id}-strategy`, usedNodeIds)
      next.nodes.push(strategy)
      nodesById.set(strategy.id, strategy)
      synthesizedStrategyIds.add(strategy.id)
      next.edges.push({
        ...createRouterEdge(candidatePool.id, strategy.id),
        id: uniqueId(`${candidatePool.id}-strategy`, usedEdgeIds),
      })
    }
    if (!strategy) continue
    strategy.model_configs ??= []
    const existingConfigs = new Map(
      strategy.model_configs.map((config) => [config.model_id, config]),
    )
    strategy.model_configs = poolCandidates.map((candidate, index) =>
      existingConfigs.get(candidate.model_id) ?? {
        model_id: candidate.model_id,
        priority: legacyVersion === 1 ? candidate.priority ?? index + 1 : index + 1,
        weight: legacyVersion === 1 ? candidate.weight ?? 100 : 100,
        max_latency_ms: legacyVersion === 1 ? candidate.max_latency_ms ?? null : null,
        max_cost_per_million: legacyVersion === 1
          ? candidate.max_cost_per_million ?? null
          : null,
        description: legacyVersion === 1 ? candidate.description ?? "" : "",
      },
    )
    for (const edge of policyEdges) {
      edge.source = strategy.id
      edge.source_handle = nodesById.get(edge.target)?.type === "fallback"
        ? "branch-exception"
        : "branch-normal"
      edge.label = nodesById.get(edge.target)?.type === "fallback"
        ? "异常策略"
        : "正常输出"
    }
  }

  nodesById = new Map(next.nodes.map((node) => [node.id, node]))
  for (const fallback of next.nodes.filter((node) => node.type === "fallback")) {
    const policyEdges = next.edges.filter(
      (edge) => edge.target === fallback.id
        && nodesById.get(edge.source)?.type === "strategy",
    )
    if (!policyEdges.length) continue
    for (const policyEdge of policyEdges) {
      const strategy = nodesById.get(policyEdge.source)
      if (strategy && strategy.timeout_ms == null && fallback.timeout_ms != null) {
        strategy.timeout_ms = fallback.timeout_ms
      }
      policyEdge.label = "异常策略"
    }
    fallback.timeout_ms = null
    const legacyOutputs = next.edges.filter((edge) => edge.source === fallback.id)
    next.edges = next.edges.filter((edge) => edge.source !== fallback.id)
    for (const legacyOutput of legacyOutputs) {
      let migratedEdgeCount = 0
      for (const policyEdge of policyEdges) {
        if (next.edges.some(
          (edge) => edge.source === policyEdge.source
            && edge.target === legacyOutput.target,
        )) continue
        next.edges.push({
          ...legacyOutput,
          id: migratedEdgeCount === 0
            ? legacyOutput.id
            : uniqueId(`${policyEdge.source}-${legacyOutput.target}`, usedEdgeIds),
          source: policyEdge.source,
          source_handle: "branch-normal",
          label: "正常输出",
        })
        migratedEdgeCount += 1
      }
    }
    const policyEdge = policyEdges[0]
    if (legacyOutputs.length || synthesizedStrategyIds.has(policyEdge.source)) {
      const strategy = nodesById.get(policyEdge.source)
      const normalOutputEdge = next.edges.find(
        (edge) => edge.source === policyEdge.source
          && nodesById.get(edge.target)?.type === "output",
      )
      const output = normalOutputEdge
        ? nodesById.get(normalOutputEdge.target)
        : null
      if (!strategy) continue
      const horizontal = !output
        || Math.abs(output.position.x - strategy.position.x)
          >= Math.abs(output.position.y - strategy.position.y)
      const strategyDimensions = routerNodeDimensions(strategy)
      const outputDimensions = output ? routerNodeDimensions(output) : null
      const branchOffset = 96
      if (horizontal) {
        const branchX = output?.position.x
          ?? strategy.position.x + strategyDimensions.width + 112
        if (output && outputDimensions) {
          output.position = {
            x: branchX,
            y: strategy.position.y - branchOffset,
          }
        }
        fallback.position = {
          x: branchX,
          y: strategy.position.y + branchOffset,
        }
      } else {
        const branchY = output?.position.y
          ?? strategy.position.y + strategyDimensions.height + 112
        if (output && outputDimensions) {
          output.position = {
            x: strategy.position.x - branchOffset,
            y: branchY,
          }
        }
        fallback.position = {
          x: strategy.position.x + branchOffset,
          y: branchY,
        }
      }
    }
  }
  next.schema_version = 2
  for (const strategy of next.nodes.filter((node) => node.type === "strategy")) {
    strategy.model_configs ??= []
  }
  return next
}

export function syncStrategyModelConfigs(
  definition: ModelRouterDefinition,
): ModelRouterDefinition {
  const next = cloneDefinition(definition)
  const nodesById = new Map(next.nodes.map((node) => [node.id, node]))
  for (const strategy of next.nodes.filter((node) => node.type === "strategy")) {
    const candidateIds = next.edges
      .filter((edge) => edge.target === strategy.id)
      .map((edge) => nodesById.get(edge.source))
      .filter((node): node is ModelRouterNode => node?.type === "candidate_models")
      .flatMap((node) => node.candidates.map((candidate) => candidate.model_id))
    const uniqueCandidateIds = [...new Set(candidateIds)]
    const configs = new Map(
      strategy.model_configs.map((config) => [config.model_id, config]),
    )
    const ordered = [...strategy.model_configs]
      .filter((config) => uniqueCandidateIds.includes(config.model_id))
      .sort((left, right) => left.priority - right.priority)
    for (const modelId of uniqueCandidateIds) {
      if (configs.has(modelId)) continue
      ordered.push({
        model_id: modelId,
        priority: ordered.length + 1,
        weight: 100,
        max_latency_ms: null,
        max_cost_per_million: null,
        description: "",
      })
    }
    strategy.model_configs = ordered.map((config, index) => ({
      ...config,
      priority: index + 1,
    }))
  }
  return next
}

export function createStarterDefinition(modelIds: string[]): ModelRouterDefinition {
  const start = createRouterNode("start", { x: 40, y: 180 })
  const candidates = createRouterNode("candidate_models", { x: 320, y: 180 })
  candidates.candidates = modelIds.map((modelId) => ({ model_id: modelId }))
  const output = createRouterNode("output", { x: 920, y: 180 })
  const strategy = createRouterNode("strategy", { x: 660, y: 180 })
  strategy.model_configs = modelIds.map((modelId, index) => ({
    model_id: modelId,
    priority: index + 1,
    weight: Math.max(1, Math.floor(100 / Math.max(modelIds.length, 1))),
    max_latency_ms: null,
    max_cost_per_million: null,
    description: "",
  }))
  // Routers called through APIM reject Fallback nodes, so new routers start without one.
  const nodes = [start, candidates, strategy, output]
  const outputEdge = createRouterEdge(strategy.id, output.id, "正常输出")
  outputEdge.source_handle = "branch-normal"
  return {
    schema_version: 2,
    presentation_version: ROUTER_GRAPH_PRESENTATION_VERSION,
    default_model_id: modelIds[0] ?? null,
    nodes,
    edges: [
      createRouterEdge(start.id, candidates.id),
      createRouterEdge(candidates.id, strategy.id),
      outputEdge,
    ],
  }
}

export function inferLayoutDirection(
  definition: ModelRouterDefinition,
): RouterLayoutDirection {
  const start = definition.nodes.find((node) => node.type === "start")
  const output = definition.nodes.find((node) => node.type === "output")
  if (!start || !output) return "horizontal"
  return Math.abs(output.position.y - start.position.y)
    > Math.abs(output.position.x - start.position.x)
    ? "vertical"
    : "horizontal"
}

export function autoLayoutDefinition(
  definition: ModelRouterDefinition,
  direction: RouterLayoutDirection = "horizontal",
): ModelRouterDefinition {
  const normalized = normalizeRouterDefinition(definition)
  const graph = new graphlib.Graph({ multigraph: true })
    .setDefaultEdgeLabel(() => ({}))
    .setGraph({
      rankdir: direction === "vertical" ? "TB" : "LR",
      align: "UL",
      ranker: "network-simplex",
      nodesep: direction === "vertical" ? 64 : 56,
      ranksep: direction === "vertical" ? 144 : 80,
      marginx: 40,
      marginy: 40,
    })
  for (const node of normalized.nodes) {
    graph.setNode(node.id, routerNodeDimensions(node))
  }
  for (const edge of normalized.edges) {
    if (graph.hasNode(edge.source) && graph.hasNode(edge.target)) {
      graph.setEdge(edge.source, edge.target, {}, edge.id)
    }
  }
  try {
    layout(graph)
  } catch {
    return cloneDefinition(definition)
  }
  const laidOut = {
    ...cloneDefinition(normalized),
    nodes: normalized.nodes.map((node) => {
      const position = graph.node(node.id)
      if (!position) return { ...node }
      const { width, height } = routerNodeDimensions(node)
      return {
        ...structuredClone(node),
        position: {
          x: Math.round(position.x - width / 2),
          y: Math.round(position.y - height / 2),
        },
      }
    }),
  }
  const arranged = cloneDefinition(laidOut)
  const arrangedNodes = new Map(arranged.nodes.map((node) => [node.id, node]))
  const branchRank = (node: ModelRouterNode) => {
    if (node.type === "output") return 0
    if (node.type === "fallback") return 2
    return 1
  }
  for (const source of arranged.nodes) {
    const targets = arranged.edges
      .filter((edge) => edge.source === source.id)
      .map((edge) => arrangedNodes.get(edge.target))
      .filter((node): node is ModelRouterNode => Boolean(node))
      .sort((left, right) => branchRank(left) - branchRank(right))
    if (targets.length < 2) continue
    const sourceDimensions = routerNodeDimensions(source)
    const gap = 72
    if (direction === "horizontal") {
      const branchX = Math.max(
        source.position.x + sourceDimensions.width + 80,
        ...targets.map((target) => target.position.x),
      )
      const totalHeight = targets.reduce(
        (total, target) => total + routerNodeDimensions(target).height,
        gap * (targets.length - 1),
      )
      let branchY = source.position.y + sourceDimensions.height / 2 - totalHeight / 2
      for (const target of targets) {
        target.position = { x: branchX, y: Math.round(branchY) }
        branchY += routerNodeDimensions(target).height + gap
      }
    } else {
      const branchY = Math.max(
        source.position.y + sourceDimensions.height + 80,
        ...targets.map((target) => target.position.y),
      )
      const totalWidth = targets.reduce(
        (total, target) => total + routerNodeDimensions(target).width,
        gap * (targets.length - 1),
      )
      let branchX = source.position.x + sourceDimensions.width / 2 - totalWidth / 2
      for (const target of targets) {
        target.position = { x: Math.round(branchX), y: branchY }
        branchX += routerNodeDimensions(target).width + gap
      }
    }
  }
  return normalizeRouterDefinition(arranged)
}

/** Published Routers whose saved graph can send requests to the model. */
export function publishedRoutersUsingModel(
  routers: ReadonlyArray<Pick<ModelRouter, "name" | "status" | "definition">>,
  modelId: string,
): string[] {
  return routers.filter((router) => router.status === "published"
    && router.definition.nodes.some((node) => node.routing_model_id === modelId
      || node.candidates.some((candidate) => candidate.model_id === modelId)
      || node.model_configs.some((config) => config.model_id === modelId)))
    .map((router) => router.name)
}

export function enabledCandidateModelIds(definition: ModelRouterDefinition): string[] {
  return [...new Set(definition.nodes.flatMap((node) =>
    node.candidates.map((candidate) => candidate.model_id),
  ))]
}
