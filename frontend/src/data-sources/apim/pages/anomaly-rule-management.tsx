import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Pencil, Plus, Save, Trash2, X } from "lucide-react"

import { Button } from "../../../components/ui/button"
import { Checkbox } from "../../../components/ui/checkbox"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../../components/ui/dialog"
import { Input } from "../../../components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../../components/ui/select"
import { Switch } from "../../../components/ui/switch"
import { Textarea } from "../../../components/ui/textarea"
import { ResizableGridTable } from "../../../components/ui/resizable-table"
import { dataSource } from "../api"
import { finopsKeys } from "../queries"
import { useAuth } from "../../../providers/auth-provider"
import type {
  AnomalyRule,
  AnomalyRuleMetric,
  AnomalyRuleWrite,
  AnomalyScopeType,
  AnomalySeverity,
  AnomalyThresholdMode,
  EnterpriseEntityCatalog,
  ManagedModel,
} from "../types"

const metricLabels: Record<AnomalyRuleMetric, string> = {
  error_rate_percent: "错误率",
  request_latency_ms: "请求延迟",
  request_cost_usd: "单次请求成本",
  agent_request_count: "智能体调用量",
}

const scopeLabels: Record<AnomalyScopeType, string> = {
  global: "全部范围",
  organization: "组织",
  department: "部门",
  project: "项目",
  agent: "智能体",
  model: "模型",
  user: "人员",
}

const metricDefaults: Record<AnomalyRuleMetric, {
  thresholdMode: AnomalyThresholdMode
  thresholdValue: number
  minimumSampleSize: number
  severity: AnomalySeverity
}> = {
  error_rate_percent: {
    thresholdMode: "absolute",
    thresholdValue: 5,
    minimumSampleSize: 20,
    severity: "critical",
  },
  request_latency_ms: {
    thresholdMode: "absolute",
    thresholdValue: 2_000,
    minimumSampleSize: 1,
    severity: "warning",
  },
  request_cost_usd: {
    thresholdMode: "percentile",
    thresholdValue: 99,
    minimumSampleSize: 2,
    severity: "warning",
  },
  agent_request_count: {
    thresholdMode: "percentile",
    thresholdValue: 99,
    minimumSampleSize: 2,
    severity: "warning",
  },
}

function defaultThreshold(metric: AnomalyRuleMetric, mode: AnomalyThresholdMode) {
  if (mode === "percentile") return 99
  return {
    error_rate_percent: 5,
    request_latency_ms: 2_000,
    request_cost_usd: 0.01,
    agent_request_count: 10,
  }[metric]
}

function thresholdLabel(metric: AnomalyRuleMetric, mode: AnomalyThresholdMode) {
  if (mode === "percentile") return "百分位（1–100）"
  return {
    error_rate_percent: "触发阈值（%）",
    request_latency_ms: "触发阈值（ms）",
    request_cost_usd: "触发阈值（USD）",
    agent_request_count: "触发阈值（次）",
  }[metric]
}

function thresholdStep(metric: AnomalyRuleMetric, mode: AnomalyThresholdMode) {
  if (mode === "percentile") return "1"
  return {
    error_rate_percent: "0.1",
    request_latency_ms: "1",
    request_cost_usd: "0.000001",
    agent_request_count: "1",
  }[metric]
}

function thresholdMinimum(metric: AnomalyRuleMetric, mode: AnomalyThresholdMode) {
  if (mode === "percentile") return "1"
  return {
    error_rate_percent: "0.1",
    request_latency_ms: "1",
    request_cost_usd: "0.000001",
    agent_request_count: "1",
  }[metric]
}

function toWrite(rule: AnomalyRule): AnomalyRuleWrite {
  const { name, description, metric, threshold_mode, threshold_value,
    minimum_sample_size, severity, scope_type, scope_id, enabled } = rule
  return { name, description, metric, threshold_mode, threshold_value,
    minimum_sample_size, severity, scope_type, scope_id, enabled }
}

function supportsPercentile(metric: AnomalyRuleMetric) {
  return metric === "request_cost_usd" || metric === "agent_request_count"
}

function conditionLabel(rule: AnomalyRule) {
  if (rule.threshold_mode === "percentile") return `P${rule.threshold_value}`
  if (rule.metric === "error_rate_percent") return `≥ ${rule.threshold_value}%`
  if (rule.metric === "request_latency_ms") {
    return rule.threshold_value >= 1_000
      ? `≥ ${rule.threshold_value / 1_000} s`
      : `≥ ${rule.threshold_value} ms`
  }
  if (rule.metric === "request_cost_usd") return `≥ $${rule.threshold_value}`
  return `≥ ${rule.threshold_value} 次`
}

function sampleLabel(rule: AnomalyRule) {
  return rule.metric === "agent_request_count"
    ? `至少 ${rule.minimum_sample_size} 个智能体`
    : `至少 ${rule.minimum_sample_size} 次请求`
}

function scopeOptions(
  type: AnomalyScopeType,
  entities: EnterpriseEntityCatalog,
  models: ManagedModel[],
) {
  if (type === "global") return []
  if (type === "model") {
    return models.map((model) => ({ value: model.id, label: model.display_name }))
  }
  const key = `${type === "user" ? "user" : type}s` as
    | "organizations" | "departments" | "projects" | "agents" | "users"
  return entities[key].map((item) => ({ value: item.id, label: item.name }))
}

function scopeName(
  rule: AnomalyRule,
  entities: EnterpriseEntityCatalog,
  models: ManagedModel[],
) {
  if (rule.scope_type === "global") return scopeLabels.global
  return scopeOptions(rule.scope_type, entities, models)
    .find((option) => option.value === rule.scope_id)?.label ?? rule.scope_id ?? "未选择"
}

function queryError(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

function AnomalyRuleEditor({
  rule, entities, models, busy, error, onClose, onSave, onDelete,
}: {
  rule: AnomalyRule | null
  entities: EnterpriseEntityCatalog
  models: ManagedModel[]
  busy: boolean
  error: string | null
  onClose: () => void
  onSave: (write: AnomalyRuleWrite) => void
  onDelete: () => void
}) {
  const initialMetric = rule?.metric ?? "request_latency_ms"
  const initialDefaults = metricDefaults[initialMetric]
  const [name, setName] = useState(rule?.name ?? "")
  const [description, setDescription] = useState(rule?.description ?? "")
  const [metric, setMetric] = useState<AnomalyRuleMetric>(initialMetric)
  const [thresholdMode, setThresholdMode] = useState<AnomalyThresholdMode>(rule?.threshold_mode ?? initialDefaults.thresholdMode)
  const [thresholdValue, setThresholdValue] = useState(String(rule?.threshold_value ?? initialDefaults.thresholdValue))
  const [minimumSampleSize, setMinimumSampleSize] = useState(String(rule?.minimum_sample_size ?? initialDefaults.minimumSampleSize))
  const [severity, setSeverity] = useState<AnomalySeverity>(rule?.severity ?? initialDefaults.severity)
  const [scopeType, setScopeType] = useState<AnomalyScopeType>(rule?.scope_type ?? "global")
  const [scopeId, setScopeId] = useState(rule?.scope_id ?? "")
  const [enabled, setEnabled] = useState(rule?.enabled ?? true)
  const [formError, setFormError] = useState<string | null>(null)
  const options = scopeOptions(scopeType, entities, models)

  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const threshold = Number(thresholdValue)
    const sample = Number(minimumSampleSize)
    if (!name.trim()) return setFormError("请输入规则名称")
    if (!Number.isFinite(threshold) || threshold <= 0) return setFormError("阈值必须大于 0")
    if (thresholdMode === "percentile" && threshold > 100) return setFormError("百分位必须在 1 到 100 之间")
    if (!Number.isInteger(sample) || sample < 1) return setFormError("最小样本必须是大于 0 的整数")
    if (scopeType !== "global" && !scopeId) return setFormError("请选择规则作用范围")
    onSave({
      name: name.trim(),
      description: description.trim(),
      metric,
      threshold_mode: thresholdMode,
      threshold_value: threshold,
      minimum_sample_size: sample,
      severity,
      scope_type: scopeType,
      scope_id: scopeType === "global" ? null : scopeId,
      enabled,
    })
  }

  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <DialogContent className="registry-editor-dialog anomaly-rule-editor-dialog" finalFocus={false}>
      <form className="registry-editor" onSubmit={submit}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>{rule ? "编辑异常规则" : "新增异常规则"}</DialogTitle>
          <DialogDescription>定义当前时间和业务筛选范围内需要触发的治理信号。</DialogDescription>
        </DialogHeader>
        <div className="registry-editor-body anomaly-rule-editor-body">
          <label className="registry-field anomaly-rule-name-field">
            <span className="registry-field-label">规则名称</span>
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：生产请求延迟超过 2 秒" autoFocus required />
          </label>
          <div className="anomaly-rule-form-grid">
            <label className="registry-field">
              <span className="registry-field-label">监控指标</span>
              <Select value={metric} onValueChange={(value) => {
                if (!value) return
                const next = value as AnomalyRuleMetric
                const defaults = metricDefaults[next]
                setMetric(next)
                setThresholdMode(defaults.thresholdMode)
                setThresholdValue(String(defaults.thresholdValue))
                setMinimumSampleSize(String(defaults.minimumSampleSize))
                setSeverity(defaults.severity)
                setFormError(null)
              }}>
                <SelectTrigger aria-label="监控指标" className="registry-select-trigger"><SelectValue>{metricLabels[metric]}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>{Object.entries(metricLabels).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent>
              </Select>
            </label>
            <label className="registry-field">
              <span className="registry-field-label">阈值模式</span>
              <Select value={thresholdMode} onValueChange={(value) => {
                if (!value) return
                const next = value as AnomalyThresholdMode
                setThresholdMode(next)
                setThresholdValue(String(defaultThreshold(metric, next)))
                setFormError(null)
              }}>
                <SelectTrigger aria-label="阈值模式" className="registry-select-trigger"><SelectValue>{thresholdMode === "absolute" ? "固定阈值" : "动态百分位"}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}><SelectItem value="absolute">固定阈值</SelectItem>{supportsPercentile(metric) && <SelectItem value="percentile">动态百分位</SelectItem>}</SelectContent>
              </Select>
            </label>
            <label className="registry-field">
              <span className="registry-field-label">{thresholdLabel(metric, thresholdMode)}</span>
              <Input type="number" min={thresholdMinimum(metric, thresholdMode)} max={thresholdMode === "percentile" || metric === "error_rate_percent" ? 100 : undefined} step={thresholdStep(metric, thresholdMode)} value={thresholdValue} onChange={(event) => setThresholdValue(event.target.value)} required />
            </label>
            <label className="registry-field">
              <span className="registry-field-label">最小样本</span>
              <Input type="number" min="1" step="1" value={minimumSampleSize} onChange={(event) => setMinimumSampleSize(event.target.value)} required />
            </label>
            <label className="registry-field">
              <span className="registry-field-label">严重级别</span>
              <Select value={severity} onValueChange={(value) => value && setSeverity(value as AnomalySeverity)}>
                <SelectTrigger aria-label="严重级别" className="registry-select-trigger"><SelectValue>{severity === "critical" ? "严重" : "警告"}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}><SelectItem value="warning">警告</SelectItem><SelectItem value="critical">严重</SelectItem></SelectContent>
              </Select>
            </label>
            <label className="registry-field">
              <span className="registry-field-label">作用范围</span>
              <Select value={scopeType} onValueChange={(value) => {
                if (!value) return
                setScopeType(value as AnomalyScopeType)
                setScopeId("")
              }}>
                <SelectTrigger aria-label="作用范围" className="registry-select-trigger"><SelectValue>{scopeLabels[scopeType]}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>{Object.entries(scopeLabels).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent>
              </Select>
            </label>
            {scopeType !== "global" && <label className="registry-field anomaly-rule-scope-target">
              <span className="registry-field-label">范围对象</span>
              <Select value={scopeId || undefined} onValueChange={(value) => value && setScopeId(value)}>
                <SelectTrigger aria-label="范围对象" className="registry-select-trigger"><SelectValue>{options.find((option) => option.value === scopeId)?.label ?? "选择对象"}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>{options.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
              </Select>
            </label>}
          </div>
          <label className="registry-field">
            <span className="registry-field-label">说明</span>
            <Textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="触发后向治理人员说明风险和处置重点" />
          </label>
          <div className="registry-checkbox-field"><Checkbox id="anomaly-rule-enabled" checked={enabled} onCheckedChange={(checked) => setEnabled(checked === true)} /><label htmlFor="anomaly-rule-enabled">启用规则</label></div>
          {(formError || error) && <div className="registry-error">{formError ?? error}</div>}
        </div>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <DialogFooter className="registry-editor-footer budget-editor-footer anomaly-rule-editor-footer">
          <div>{rule && <Button type="button" variant="destructive" className="anomaly-rule-delete" disabled={busy} onClick={onDelete}><Trash2 size={14} />删除规则</Button>}</div>
          <div><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose><Button type="submit" disabled={busy}><Save size={14} />保存规则</Button></div>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

export function AnomalyRuleManagement({ rules, entities, models }: {
  rules: AnomalyRule[]
  entities: EnterpriseEntityCatalog
  models: ManagedModel[]
}) {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const canManage = user?.role === "owner"
  const [editing, setEditing] = useState<AnomalyRule | null | undefined>(undefined)
  const syncRule = (rule: AnomalyRule) => {
    queryClient.setQueryData<AnomalyRule[]>(finopsKeys.anomalyRules, (current = []) => {
      const exists = current.some((item) => item.id === rule.id)
      return exists
        ? current.map((item) => item.id === rule.id ? rule : item)
        : [...current, rule]
    })
    void queryClient.invalidateQueries({ queryKey: ["finops", "anomalies"] })
  }
  const save = useMutation({
    mutationFn: ({ id, write }: { id?: string; write: AnomalyRuleWrite; close: boolean }) => id ? dataSource.updateAnomalyRule(id, write) : dataSource.createAnomalyRule(write),
    onSuccess: (rule, variables) => {
      syncRule(rule)
      if (variables.close) setEditing(undefined)
    },
  })
  const remove = useMutation({
    mutationFn: (id: string) => dataSource.deleteAnomalyRule(id),
    onSuccess: (_, id) => {
      queryClient.setQueryData<AnomalyRule[]>(finopsKeys.anomalyRules, (current = []) => current.filter((item) => item.id !== id))
      void queryClient.invalidateQueries({ queryKey: ["finops", "anomalies"] })
      setEditing(undefined)
    },
  })
  const busy = save.isPending || remove.isPending
  const enabledCount = rules.filter((rule) => rule.enabled).length
  return <section className="finops-panel span-3 anomaly-rule-panel">
    <header className="anomaly-rule-heading">
      <div><h2>异常规则</h2><p>定义阈值、严重级别和业务范围；规则按当前时间筛选窗口评估。</p></div>
      <div><span>{enabledCount} / {rules.length} 已启用</span>{canManage && <Button type="button" onClick={() => { save.reset(); remove.reset(); setEditing(null) }}><Plus size={14} />新增规则</Button>}</div>
    </header>
    {(save.error || remove.error) && editing === undefined && <div className="anomaly-rule-error"><AlertTriangle size={14} />{queryError(save.error ?? remove.error)}</div>}
    <div className="anomaly-rule-scroll">
      <ResizableGridTable className="anomaly-rule-list" role="table" aria-label="异常规则列表" headerSelector=".anomaly-rule-list-head" minWidths={[180, 120, 140, 72, 64, 42]} columnGap={10} horizontalPadding={28}>
        <div className="anomaly-rule-list-head" role="row"><span><span>规则</span></span><span><span>条件</span></span><span><span>作用范围</span></span><span><span>级别</span></span><span><span>状态</span></span><span /></div>
        {rules.map((rule) => <div className="anomaly-rule-row" role="row" key={rule.id} data-enabled={rule.enabled}>
          <div className="anomaly-rule-primary"><i data-severity={rule.severity} /><span><strong>{rule.name}</strong><small>{metricLabels[rule.metric]} · {sampleLabel(rule)}</small></span></div>
          <div><strong>{conditionLabel(rule)}</strong><small>{rule.threshold_mode === "percentile" ? "随当前窗口动态计算" : "固定阈值"}</small></div>
          <div><strong>{scopeLabels[rule.scope_type]}</strong><small title={scopeName(rule, entities, models)}>{scopeName(rule, entities, models)}</small></div>
          <div><span className="anomaly-rule-severity" data-severity={rule.severity}>{rule.severity === "critical" ? "严重" : "警告"}</span></div>
          <div className="anomaly-rule-switch-cell"><Switch checked={rule.enabled} disabled={busy || !canManage} aria-label={`${rule.name} ${rule.enabled ? "停用" : "启用"}`} onCheckedChange={(checked) => save.mutate({ id: rule.id, write: { ...toWrite(rule), enabled: checked }, close: false })} /></div>
          {canManage ? <Button type="button" variant="ghost" size="icon-sm" className="anomaly-rule-edit" aria-label={`编辑 ${rule.name}`} title="编辑规则" disabled={busy} onClick={() => { save.reset(); remove.reset(); setEditing(rule) }}><Pencil size={14} /></Button> : <span />}
        </div>)}
      </ResizableGridTable>
    </div>
    {canManage && editing !== undefined && <AnomalyRuleEditor
      key={editing?.id ?? "new-rule"}
      rule={editing}
      entities={entities}
      models={models}
      busy={busy}
      error={save.error || remove.error ? queryError(save.error ?? remove.error) : null}
      onClose={() => setEditing(undefined)}
      onSave={(write) => save.mutate({ id: editing?.id, write, close: true })}
      onDelete={() => editing && remove.mutate(editing.id)}
    />}
  </section>
}
