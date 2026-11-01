import type {
  ManagedModel,
  ModelRouterDefinition,
  ModelRouterNode,
  ModelRouterReasoningEffort,
} from "../../data-sources/apim/types"
import { Input } from "../ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select"
import { Textarea } from "../ui/textarea"

const DEFAULT_MAX_PROMPT_CHARS = 4_000

const REASONING_EFFORT_LABELS: Record<string, string> = {
  default: "默认（跟随模型）",
  none: "关闭思考（none · 最快）",
  low: "低强度（low）",
  medium: "中强度（medium）",
  high: "高强度（high）",
}

export function DecisionPromptEditor({
  node,
  models = [],
  onChange,
}: {
  definition?: ModelRouterDefinition
  node: ModelRouterNode
  models?: ManagedModel[]
  onChange: (node: ModelRouterNode) => void
}) {
  const routingModel = models.find((model) => model.id === node.routing_model_id) ?? null
  const isSystemOne = Boolean(routingModel?.capabilities.includes("decision"))

  return <div className="router-decision-prompt-editor">
    <label className="router-field"><span>{isSystemOne ? "判路由指令" : "判路由提示词"}</span>
      <Textarea
        rows={6}
        maxLength={20_000}
        placeholder={isSystemOne
          ? "留空使用内置判路由指令；候选模型的名称、单价和路由用途说明会自动作为 Choice 判据"
          : "留空使用内置提示词；{catalog} 会替换为当前授权候选目录"}
        value={node.decision_prompt ?? ""}
        onChange={(event) => {
          onChange({ ...node, decision_prompt: event.target.value || null })
        }}
      />
    </label>
    {!isSystemOne && <label className="router-field"><span>判路由推理强度</span>
      <Select
        value={node.decision_reasoning_effort ?? "default"}
        onValueChange={(value) => {
          const nextEffort = !value || value === "default"
            ? undefined
            : (value as ModelRouterReasoningEffort)
          const nextNode: ModelRouterNode = { ...node }
          if (nextEffort) {
            nextNode.decision_reasoning_effort = nextEffort
          } else {
            delete nextNode.decision_reasoning_effort
          }
          onChange(nextNode)
        }}
      >
        <SelectTrigger aria-label="选择判路由推理强度">
          <SelectValue>{REASONING_EFFORT_LABELS[node.decision_reasoning_effort ?? "default"]}</SelectValue>
        </SelectTrigger>
        <SelectContent align="end">
          <SelectItem value="default">{REASONING_EFFORT_LABELS.default}</SelectItem>
          <SelectItem value="none">{REASONING_EFFORT_LABELS.none}</SelectItem>
          <SelectItem value="low">{REASONING_EFFORT_LABELS.low}</SelectItem>
          <SelectItem value="medium">{REASONING_EFFORT_LABELS.medium}</SelectItem>
          <SelectItem value="high">{REASONING_EFFORT_LABELS.high}</SelectItem>
        </SelectContent>
      </Select>
    </label>}
    <label className="router-field"><span>最大判路由输入字符</span>
      <Input
        type="number"
        min={64}
        max={50_000}
        value={node.max_prompt_chars ?? DEFAULT_MAX_PROMPT_CHARS}
        onChange={(event) => {
          const value = Number(event.target.value)
          onChange({
            ...node,
            max_prompt_chars: Number.isFinite(value)
              ? Math.min(50_000, Math.max(64, value))
              : DEFAULT_MAX_PROMPT_CHARS,
          })
        }}
      />
    </label>
  </div>
}
