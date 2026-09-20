import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  Building2,
  CalendarDays,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleGauge,
  CircleSlash2,
  Eye,
  MoreHorizontal,
  PanelLeft,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Search,
  Trash2,
  UserRound,
  Users,
  WalletCards,
  X,
} from "lucide-react"

import { Button } from "../../../components/ui/button"
import { Checkbox } from "../../../components/ui/checkbox"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../../../components/ui/dropdown-menu"
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
import { Popover, PopoverContent, PopoverTrigger } from "../../../components/ui/popover"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../../components/ui/select"
import { MonthPicker } from "../../../components/finops/month-picker"
import { Switch } from "../../../components/ui/switch"
import { ResizableGridTable } from "../../../components/ui/resizable-table"
import { dataSource } from "../api"
import { getIntlLocale, useLocale, type LocalePreference } from "../../../locales/index"
import { finopsKeys, finopsQueries } from "../queries"
import { useAuth } from "../../../providers/auth-provider"
import type {
  BudgetScopeType,
  DepartmentEnforcement,
  EnforcementMode,
  ManagedModel,
  PeopleBudgetFilter,
  TokenBudgetBulkWrite,
  TokenBudgetItem,
  TokenBudgetPeopleResponse,
  TokenBudgetResponse,
  TokenBudgetWrite,
} from "../types"

const scopeLabels: Record<BudgetScopeType, string> = {
  organization: "组织",
  department: "部门",
  user: "人员",
}

const statusLabels: Record<TokenBudgetItem["status"], string> = {
  unallocated: "未分配",
  healthy: "正常",
  warning: "预警",
  exceeded: "已超额",
}

const currentPeriod = () => new Date().toISOString().slice(0, 7)

/** The selectable window, expressed as bounds rather than a list of options: the picker
 *  renders a year at a time and only needs to know where to stop. Same span as the flat
 *  list it replaced — a year back for review, a year forward for planning. */
function periodBoundsFor(period: string) {
  const current = new Date(`${period}-01T00:00:00Z`)
  const shifted = (months: number) => new Date(Date.UTC(
    current.getUTCFullYear(), current.getUTCMonth() + months, 1,
  )).toISOString().slice(0, 7)
  return { min: shifted(-12), max: shifted(12) }
}

function queryError(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

function formatTokens(value: number) {
  return new Intl.NumberFormat(getIntlLocale(), {
    notation: value >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value)
}

function formatFullTokens(value: number) {
  return new Intl.NumberFormat(getIntlLocale()).format(value)
}

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat(getIntlLocale(), {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value))
}

function sumLimits(items: TokenBudgetItem[]) {
  return items.reduce((total, item) => total + (item.token_limit ?? 0), 0)
}

function BudgetKpis({ data }: { data: TokenBudgetResponse }) {
  const organizations = data.items.filter((item) => item.scope_type === "organization")
  const allocatedBudget = sumLimits(organizations)
  const usedTokens = organizations.reduce((total, item) => total + item.used_tokens, 0)
  const remainingTokens = allocatedBudget - usedTokens
  const assigned = data.items.filter((item) => item.token_limit != null).length
  const atRisk = data.risk_count
  const forecastTokens = organizations.reduce(
    (total, item) => total + item.forecast_tokens,
    0,
  )
  const kpis = [
    {
      label: "组织总预算",
      value: allocatedBudget ? formatTokens(allocatedBudget) : "未分配",
      detail: `${organizations.filter((item) => item.token_limit != null).length} 个组织已配置`,
      icon: WalletCards,
    },
    {
      label: "本月已使用",
      value: formatTokens(usedTokens),
      detail: allocatedBudget
        ? `${Math.round((usedTokens / allocatedBudget) * 1000) / 10}% 预算消耗`
        : "等待组织预算",
      icon: CircleGauge,
    },
    {
      label: "预算余额",
      value: allocatedBudget ? formatTokens(remainingTokens) : "--",
      detail: remainingTokens < 0 ? "已超过组织预算" : "本周期剩余额度",
      icon: CheckCircle2,
    },
    {
      label: "月底预测",
      value: formatTokens(forecastTokens),
      detail: allocatedBudget
        ? `${Math.round((forecastTokens / allocatedBudget) * 1000) / 10}% 预计使用率`
        : "按当前消耗速度",
      icon: CalendarDays,
    },
    {
      label: "预算覆盖",
      value: `${assigned}/${data.items.length}`,
      detail: atRisk ? `${atRisk} 个范围需要关注` : "当前无超额风险",
      icon: atRisk ? AlertTriangle : CheckCircle2,
    },
  ]
  return <section className="finops-kpis budget-kpis">
    {kpis.map(({ label, value, detail, icon: Icon }) => <div key={label}>
      <span className="finops-kpi-icon"><Icon size={14} /></span>
      <label>{label}</label>
      <strong>{value}</strong>
      <small className={detail.includes("超过") || detail.includes("关注") ? "up" : ""}>{detail}</small>
    </div>)}
  </section>
}

type BudgetConstraint = {
  minimum: number
  maximum: number | null
  childAllocated: number
  parentName: string | null
  parentLimit: number | null
}

function budgetConstraint(item: TokenBudgetItem, items: TokenBudgetItem[]): BudgetConstraint {
  const childType = item.scope_type === "organization"
    ? "department"
    : item.scope_type === "department"
      ? "user"
      : null
  const childAllocated = childType
    ? sumLimits(items.filter(
        (candidate) => candidate.scope_type === childType && candidate.parent_scope_id === item.scope_id,
      ))
    : 0
  if (item.scope_type === "organization") {
    return { minimum: Math.max(1, childAllocated), maximum: null, childAllocated, parentName: null, parentLimit: null }
  }
  const parentType = item.scope_type === "department" ? "organization" : "department"
  const parent = items.find(
    (candidate) => candidate.scope_type === parentType && candidate.scope_id === item.parent_scope_id,
  )
  const siblingAllocated = sumLimits(items.filter(
    (candidate) => candidate.scope_type === item.scope_type
      && candidate.parent_scope_id === item.parent_scope_id
      && candidate.scope_id !== item.scope_id,
  ))
  const maximum = parent?.token_limit == null ? null : parent.token_limit - siblingAllocated
  return {
    minimum: Math.max(1, childAllocated),
    maximum,
    childAllocated,
    parentName: parent?.scope_name ?? null,
    parentLimit: parent?.token_limit ?? null,
  }
}

function BudgetEditor({
  item,
  items,
  busy,
  error,
  onClose,
  onSave,
  onDelete,
}: {
  item: TokenBudgetItem
  items: TokenBudgetItem[]
  busy: boolean
  error: string | null
  onClose: () => void
  onSave: (value: TokenBudgetWrite) => void
  onDelete: () => void
}) {
  const constraint = budgetConstraint(item, items)
  const [tokenLimit, setTokenLimit] = useState(String(item.token_limit ?? ""))
  const [warningThreshold, setWarningThreshold] = useState(
    String(item.warning_threshold_percent),
  )
  const [formError, setFormError] = useState<string | null>(null)
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const parsedLimit = Number(tokenLimit)
    const parsedThreshold = Number(warningThreshold)
    if (!Number.isSafeInteger(parsedLimit) || parsedLimit < constraint.minimum) {
      setFormError(`Token 预算不能低于 ${formatFullTokens(constraint.minimum)}`)
      return
    }
    if (constraint.maximum != null && parsedLimit > constraint.maximum) {
      setFormError(`当前层级最多可分配 ${formatFullTokens(constraint.maximum)} Token`)
      return
    }
    if (!Number.isInteger(parsedThreshold) || parsedThreshold < 1 || parsedThreshold > 100) {
      setFormError("预警阈值必须在 1% 到 100% 之间")
      return
    }
    onSave({ token_limit: parsedLimit, warning_threshold_percent: parsedThreshold })
  }
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <DialogContent className="registry-editor-dialog budget-editor-dialog" finalFocus={false}>
      <form className="registry-editor" onSubmit={submit}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>{item.token_limit == null ? "分配 Token 预算" : "调整 Token 预算"}</DialogTitle>
          <DialogDescription>{scopeLabels[item.scope_type]} · {item.scope_name}</DialogDescription>
        </DialogHeader>
        <div className="registry-editor-body budget-editor-body">
          <div className="budget-editor-context">
            {constraint.parentName && <div><span>上级范围</span><strong>{constraint.parentName}</strong><small>{constraint.parentLimit == null ? "尚未分配预算" : `${formatFullTokens(constraint.parentLimit)} Token`}</small></div>}
            <div><span>已使用</span><strong>{formatFullTokens(item.used_tokens)}</strong><small>本自然月实际 Token</small></div>
            {item.scope_type !== "user" && <div><span>下级已分配</span><strong>{formatFullTokens(constraint.childAllocated)}</strong><small>调整后不可低于此值</small></div>}
          </div>
          <label className="registry-field">
            <span className="registry-field-label">Token 预算</span>
            <Input
              name="token_limit"
              type="number"
              min={constraint.minimum}
              max={constraint.maximum ?? undefined}
              step="1"
              inputMode="numeric"
              value={tokenLimit}
              onChange={(event) => setTokenLimit(event.target.value)}
              placeholder="输入本月 Token 额度"
              required
              autoFocus
            />
            <small>{constraint.maximum == null
              ? `最低 ${formatFullTokens(constraint.minimum)} Token`
              : `可分配范围 ${formatFullTokens(constraint.minimum)} - ${formatFullTokens(Math.max(constraint.minimum, constraint.maximum))} Token`}</small>
          </label>
          <label className="registry-field">
            <span className="registry-field-label">预警阈值</span>
            <div className="budget-threshold-input"><Input
              name="warning_threshold_percent"
              type="number"
              min="1"
              max="100"
              step="1"
              inputMode="numeric"
              value={warningThreshold}
              onChange={(event) => setWarningThreshold(event.target.value)}
              required
            /><span>%</span></div>
            <small>实际使用或月底预测达到阈值时进入预警状态</small>
          </label>
          {(formError || error) && <div className="registry-error">{formError ?? error}</div>}
        </div>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <DialogFooter className="registry-editor-footer budget-editor-footer">
          <div>{item.token_limit != null && <Button type="button" variant="destructive" disabled={busy || constraint.childAllocated > 0} onClick={onDelete} title={constraint.childAllocated > 0 ? "请先移除下级预算" : "移除预算"}><Trash2 size={14} />移除预算</Button>}</div>
          <div><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose><Button type="submit" disabled={busy || (item.scope_type !== "organization" && constraint.parentLimit == null)}><Save size={14} />保存预算</Button></div>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function BudgetProgress({ item }: { item: TokenBudgetItem }) {
  const progress = Math.min(100, Math.max(0, item.usage_percent ?? 0))
  return <div className="budget-progress-cell" data-label="已使用">
    <div><strong>{formatFullTokens(item.used_tokens)}</strong><span>{item.usage_percent == null ? "--" : `${item.usage_percent}%`}</span></div>
    <span className="budget-progress-track" data-status={item.status} role="progressbar" aria-label={`${item.scope_name} 已使用`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={item.usage_percent == null ? undefined : progress}><i style={{ width: `${progress}%` }} /></span>
  </div>
}

function BudgetRow({
  item,
  canManage,
  depth,
  expanded,
  hasChildren,
  onToggle,
  onEdit,
  onManagePeople,
  parentAllocated,
  enforcement,
  enforcementBusy,
  onToggleEnforcement,
}: {
  item: TokenBudgetItem
  canManage: boolean
  depth: number
  expanded: boolean
  hasChildren: boolean
  onToggle: () => void
  onEdit: () => void
  onManagePeople?: () => void
  parentAllocated: boolean
  enforcement?: DepartmentEnforcement
  enforcementBusy?: boolean
  onToggleEnforcement?: (mode: EnforcementMode) => void
}) {
  const ScopeIcon = item.scope_type === "organization"
    ? Building2
    : item.scope_type === "department"
      ? Users
      : UserRound
  const editDisabled = item.scope_type !== "organization" && !parentAllocated
  return <div className="budget-table-row" data-depth={depth} data-status={item.status}>
    <div className="budget-scope-cell" style={{ "--budget-depth": depth } as React.CSSProperties}>
      {hasChildren
        ? <button type="button" className="budget-tree-toggle" onClick={onToggle} aria-label={expanded ? "折叠" : "展开"}>{expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</button>
        : <span className="budget-tree-spacer" />}
      <span className="budget-scope-icon"><ScopeIcon size={14} /></span>
      {/* A scope name is the customer's own text -- their organization, their department, a
          person's address. The runtime translator walks text nodes and would translate it:
          a department named 生物信息 rendered as "生物Info" in English until this was marked. */}
      <span><strong data-no-localize>{item.scope_name}</strong><small>{scopeLabels[item.scope_type]}</small></span>
    </div>
    <div className="budget-number-cell" data-label="预算"><strong>{item.token_limit == null ? "--" : formatFullTokens(item.token_limit)}</strong><small>{item.token_limit == null ? "尚未分配" : `预警 ${item.warning_threshold_percent}%`}</small></div>
    <BudgetProgress item={item} />
    <div className="budget-number-cell" data-label="剩余"><strong>{item.remaining_tokens == null ? "--" : formatFullTokens(item.remaining_tokens)}</strong><small>Token</small></div>
    <div className="budget-number-cell" data-label="月底预测"><strong>{formatFullTokens(item.forecast_tokens)}</strong><small>{item.forecast_percent == null ? "未设置预算" : `${item.forecast_percent}% 预计使用`}</small></div>
    <div className="budget-status-cell"><span data-status={item.status}>{statusLabels[item.status]}</span></div>
    <div className="budget-enforce-cell" data-label="拦截">
      {enforcement
        ? canManage ? <Switch
          checked={enforcement.mode === "block"}
          disabled={enforcementBusy}
          aria-label={`${item.scope_name} ${enforcement.mode === "block" ? "改为仅告警" : "改为拦截"}`}
          title={enforcement.mode === "block" ? "拦截中：额度用尽的人员会被网关拒绝" : "仅告警：完整记账，不拦截"}
          onCheckedChange={(checked: boolean) => onToggleEnforcement?.(checked ? "block" : "audit")}
        /> : <span
          className="budget-enforcement-state"
          data-mode={enforcement.mode}
          title={enforcement.mode === "block" ? "拦截中：额度用尽的人员会被网关拒绝" : "仅告警：完整记账，不拦截"}
        >
          {enforcement.mode === "block" ? <CircleSlash2 aria-hidden="true" /> : <Eye aria-hidden="true" />}
          <span>{enforcement.mode === "block" ? "拦截" : "仅告警"}</span>
        </span>
        : <span className="budget-enforce-na">--</span>}
    </div>
    <div className="budget-row-actions">
      {canManage && <DropdownMenu>
        <DropdownMenuTrigger render={<Button type="button" variant="ghost" size="icon-sm" className="budget-row-menu-trigger" aria-label={`${item.scope_name} 操作`} title="更多操作" />}>
          <MoreHorizontal size={15} />
          <span className="sr-only">更多操作</span>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" sideOffset={4} className="budget-row-menu-content">
          {onManagePeople && <DropdownMenuItem disabled={item.token_limit == null} onClick={onManagePeople}><Users size={14} />管理人员预算</DropdownMenuItem>}
          <DropdownMenuItem disabled={editDisabled} onClick={onEdit}>{item.token_limit == null ? <Plus size={14} /> : <Pencil size={14} />}{item.token_limit == null ? "分配预算" : `编辑${scopeLabels[item.scope_type]}预算`}</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>}
    </div>
  </div>
}

function BudgetTable({
  items,
  canManage,
  onEdit,
  onManagePeople,
  enforcement,
  enforcementBusy,
  onToggleEnforcement,
}: {
  items: TokenBudgetItem[]
  canManage: boolean
  onEdit: (item: TokenBudgetItem) => void
  onManagePeople: (departmentId: string) => void
  enforcement: DepartmentEnforcement[]
  enforcementBusy: boolean
  onToggleEnforcement: (departmentId: string, mode: EnforcementMode) => void
}) {
  const enforcementByDepartment = useMemo(
    () => new Map(enforcement.map((row) => [row.department_id, row])),
    [enforcement],
  )
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const byKey = useMemo(
    () => new Map(items.map((item) => [`${item.scope_type}:${item.scope_id}`, item])),
    [items],
  )
  const rows = useMemo(() => {
    const result: Array<{ item: TokenBudgetItem; depth: number; hasChildren: boolean; parentAllocated: boolean }> = []
    const organizations = items.filter((item) => item.scope_type === "organization")
    for (const organization of organizations) {
      const departments = items.filter(
        (item) => item.scope_type === "department" && item.parent_scope_id === organization.scope_id,
      )
      result.push({ item: organization, depth: 0, hasChildren: departments.length > 0, parentAllocated: true })
      if (collapsed.has(organization.scope_id)) continue
      for (const department of departments) {
        result.push({ item: department, depth: 1, hasChildren: false, parentAllocated: organization.token_limit != null })
      }
    }
    return result
  }, [collapsed, items])
  const toggle = (scopeId: string) => setCollapsed((current) => {
    const next = new Set(current)
    if (next.has(scopeId)) next.delete(scopeId)
    else next.add(scopeId)
    return next
  })
  const assignedCount = items.filter((item) => item.token_limit != null).length
  return <div className="budget-allocation-section">
    <div className="budget-section-heading">
      <div><h2>层级预算分配</h2><p>组织额度约束部门，部门额度约束人员</p></div>
      <div className="budget-section-summary">
        <span><strong>{assignedCount}</strong><small>已配置</small></span>
        <i />
        <span><strong>{items.length - assignedCount}</strong><small>待分配</small></span>
      </div>
    </div>
    <section className="finops-panel budget-allocation-panel">
      <div className="budget-table-scroll">
        <ResizableGridTable className="budget-table" role="table" aria-label="Token 预算层级" headerSelector=".budget-table-head" minWidths={[180, 100, 140, 100, 110, 94, 88, 52]}>
          <div className="budget-table-head" role="row"><span>范围</span><span>预算</span><span>已使用</span><span>剩余</span><span>月底预测</span><span>状态</span><span title="开启后额度用尽的人员会被网关拒绝">拦截</span><span /></div>
          {rows.map(({ item, depth, hasChildren, parentAllocated }) => <BudgetRow
            key={`${item.scope_type}:${item.scope_id}`}
            item={item}
            canManage={canManage}
            depth={depth}
            expanded={!collapsed.has(item.scope_id)}
            hasChildren={hasChildren}
            parentAllocated={parentAllocated}
            onToggle={() => toggle(item.scope_id)}
            onEdit={() => onEdit(byKey.get(`${item.scope_type}:${item.scope_id}`) ?? item)}
            onManagePeople={item.scope_type === "department" ? () => onManagePeople(item.scope_id) : undefined}
            enforcement={item.scope_type === "department" ? enforcementByDepartment.get(item.scope_id) : undefined}
            enforcementBusy={enforcementBusy}
            onToggleEnforcement={item.scope_type === "department"
              ? (mode) => onToggleEnforcement(item.scope_id, mode)
              : undefined}
          />)}
        </ResizableGridTable>
      </div>
    </section>
  </div>
}

function BulkPeopleEditor({
  people,
  models,
  selection,
  selectedIds,
  query,
  status,
  busy,
  error,
  onClose,
  onSave,
}: {
  people: TokenBudgetPeopleResponse
  models: ManagedModel[]
  selection: "ids" | "all_matching"
  selectedIds: string[]
  query: string
  status: PeopleBudgetFilter
  busy: boolean
  error: string | null
  onClose: () => void
  onSave: (write: TokenBudgetBulkWrite) => void
}) {
  const targetCount = selection === "all_matching" ? people.total : selectedIds.length
  const singlePerson = selection === "ids" && selectedIds.length === 1
    ? people.items.find((item) => item.scope_id === selectedIds[0])
    : undefined
  const [updateBudget, setUpdateBudget] = useState(
    singlePerson ? singlePerson.token_limit != null : true,
  )
  const [updateModels, setUpdateModels] = useState(
    singlePerson?.model_policy_configured ?? false,
  )
  const [mode, setMode] = useState<"fixed" | "equal_remaining">("fixed")
  const [tokenLimit, setTokenLimit] = useState(
    singlePerson?.token_limit == null ? "" : String(singlePerson.token_limit),
  )
  const [threshold, setThreshold] = useState(
    String(singlePerson?.warning_threshold_percent ?? 80),
  )
  const [modelIds, setModelIds] = useState<Set<string>>(
    new Set(singlePerson?.allowed_model_ids ?? []),
  )
  const [formError, setFormError] = useState<string | null>(null)
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!updateBudget && !updateModels) {
      setFormError("请选择要更新的预算或模型访问")
      return
    }
    const parsedThreshold = Number(threshold)
    const parsedLimit = Number(tokenLimit)
    if (updateBudget && (!Number.isInteger(parsedThreshold) || parsedThreshold < 1 || parsedThreshold > 100)) {
      setFormError("预警阈值必须在 1% 到 100% 之间")
      return
    }
    if (updateBudget && mode === "fixed" && (!Number.isSafeInteger(parsedLimit) || parsedLimit < 1)) {
      setFormError("请输入大于 0 的每人 Token 额度")
      return
    }
    onSave({
      department_id: people.department_id,
      selection,
      user_ids: selection === "ids" ? selectedIds : [],
      query: selection === "all_matching" ? query : undefined,
      status,
      allocation_mode: updateBudget ? mode : "preserve",
      token_limit: updateBudget && mode === "fixed" ? parsedLimit : undefined,
      warning_threshold_percent: parsedThreshold,
      model_ids: updateModels ? [...modelIds] : undefined,
    })
  }
  const projected = updateBudget && mode === "fixed" && Number(tokenLimit) > 0
    ? Number(tokenLimit) * targetCount
    : null
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <DialogContent className="registry-editor-dialog people-bulk-dialog" finalFocus={false}>
      <form className="registry-editor" onSubmit={submit}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>人员预算与模型访问</DialogTitle>
          <DialogDescription>{people.department_name} · {targetCount.toLocaleString(getIntlLocale())} 人</DialogDescription>
        </DialogHeader>
        <div className="registry-editor-body people-bulk-body">
          <div className="people-bulk-summary">
            <div><span>部门预算</span><strong>{people.department_token_limit == null ? "--" : formatFullTokens(people.department_token_limit)}</strong></div>
            <div><span>已分配</span><strong>{formatFullTokens(people.department_allocated_tokens)}</strong></div>
            <div><span>当前可用</span><strong>{people.department_available_tokens == null ? "--" : formatFullTokens(people.department_available_tokens)}</strong></div>
          </div>
          <label className="people-update-toggle"><Checkbox checked={updateBudget} onCheckedChange={(checked) => setUpdateBudget(checked === true)} /><span><b>更新月度预算</b><small>按当前预算周期保存 Token 额度</small></span></label>
          {updateBudget && <div className="registry-field">
            <span className="registry-field-label">分配方式</span>
            <div className="people-allocation-modes">
              <button type="button" className={mode === "fixed" ? "active" : ""} onClick={() => setMode("fixed")}>每人固定额度</button>
              <button type="button" className={mode === "equal_remaining" ? "active" : ""} onClick={() => setMode("equal_remaining")}>均分可用额度</button>
            </div>
          </div>}
          {updateBudget && mode === "fixed" && <label className="registry-field">
            <span className="registry-field-label">每人 Token 预算</span>
            <Input type="number" min="1" step="1" inputMode="numeric" value={tokenLimit} onChange={(event) => setTokenLimit(event.target.value)} placeholder="例如 50,000" autoFocus />
            <small>{projected == null ? "为所有选中人员设置相同额度" : `本次合计 ${formatFullTokens(projected)} Token`}</small>
          </label>}
          {updateBudget && mode === "equal_remaining" && <div className="people-equal-notice">{`系统会扣除未选中人员的现有额度，再将可用预算平均分配给这 ${targetCount.toLocaleString(getIntlLocale())} 人。`}</div>}
          {updateBudget && <label className="registry-field">
            <span className="registry-field-label">预警阈值</span>
            <div className="budget-threshold-input"><Input type="number" min="1" max="100" step="1" inputMode="numeric" value={threshold} onChange={(event) => setThreshold(event.target.value)} /><span>%</span></div>
          </label>}
          <label className="people-update-toggle"><Checkbox checked={updateModels} disabled={models.length === 0} onCheckedChange={(checked) => setUpdateModels(checked === true)} /><span><b>更新模型访问</b><small>长期有效，不随预算月份变化</small></span></label>
          {updateModels && <ModelAccessPicker models={models} selectedIds={modelIds} onChange={setModelIds} />}
          {(formError || error) && <div className="registry-error">{formError ?? error}</div>}
        </div>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose><Button type="submit" disabled={busy || targetCount === 0}><Save size={14} />保存分配</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function ModelAccessPicker({
  models,
  selectedIds,
  onChange,
}: {
  models: ManagedModel[]
  selectedIds: Set<string>
  onChange: (ids: Set<string>) => void
}) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState("")
  const normalizedSearch = search.trim().toLocaleLowerCase()
  const filteredModels = models.filter((model) => !normalizedSearch || `${model.display_name} ${model.model_key} ${model.provider_name}`.toLocaleLowerCase().includes(normalizedSearch))
  const selectedModel = selectedIds.size === 1
    ? models.find((model) => selectedIds.has(model.id))
    : undefined
  const triggerLabel = selectedIds.size === 0
    ? "禁止全部模型"
    : selectedModel?.display_name ?? `已选 ${selectedIds.size.toLocaleString(getIntlLocale())} 个模型`
  const toggleModel = (modelId: string, checked: boolean) => {
    const next = new Set(selectedIds)
    if (checked) next.add(modelId)
    else next.delete(modelId)
    onChange(next)
  }
  return <div className="registry-field people-model-access-field">
    <span className="registry-field-label">允许使用的模型</span>
    <Popover open={open} onOpenChange={(next) => { setOpen(next); if (!next) setSearch("") }}>
      <PopoverTrigger className="people-model-select-trigger" aria-label="选择允许模型">
        <span><b>{triggerLabel}</b><small>{selectedIds.size === 0 ? "明确禁止所选人员调用任何模型" : "长期有效"}</small></span>
        <ChevronDown size={14} />
      </PopoverTrigger>
      <PopoverContent className="people-model-select-content" align="start">
        <label className="people-model-search"><Search size={14} /><Input autoFocus value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索模型或提供方" aria-label="搜索允许模型" /></label>
        <div className="people-model-select-actions"><span>{selectedIds.size.toLocaleString(getIntlLocale())} / {models.length.toLocaleString(getIntlLocale())} 已选</span><button type="button" onClick={() => onChange(new Set(models.map((model) => model.id)))}>全选可用</button><button type="button" onClick={() => onChange(new Set())}>清空</button></div>
        <div className="people-model-select-list" role="group" aria-label="允许模型选项">
          {filteredModels.map((model) => <div className="people-model-option" key={model.id} onClick={(event) => { if (!(event.target as HTMLElement).closest('[data-slot="checkbox"]')) toggleModel(model.id, !selectedIds.has(model.id)) }}><Checkbox checked={selectedIds.has(model.id)} onCheckedChange={(checked) => toggleModel(model.id, checked === true)} /><span><b>{model.display_name}</b><small>{model.provider_name} · {model.model_key}</small></span></div>)}
          {filteredModels.length === 0 && <div className="people-model-empty">没有匹配的模型</div>}
        </div>
      </PopoverContent>
    </Popover>
  </div>
}

function PeopleBudgetWorkspace({
  period,
  canManage,
  departments,
  models,
  departmentId,
  onDepartmentChange,
}: {
  period: string
  canManage: boolean
  departments: TokenBudgetItem[]
  models: ManagedModel[]
  departmentId: string
  onDepartmentChange: (departmentId: string) => void
}) {
  const queryClient = useQueryClient()
  const [search, setSearch] = useState("")
  const [debouncedSearch, setDebouncedSearch] = useState("")
  const [status, setStatus] = useState<PeopleBudgetFilter>("all")
  const [offset, setOffset] = useState(0)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [allMatching, setAllMatching] = useState(false)
  const [bulkOpen, setBulkOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const limit = 50
  const people = useQuery(finopsQueries.peopleBudgets(
    period,
    departmentId,
    debouncedSearch,
    status,
    offset,
    limit,
  ))
  const bulk = useMutation({
    mutationFn: (write: TokenBudgetBulkWrite) => dataSource.bulkSavePeopleBudgets(period, write),
    onSuccess: (result) => {
      setBulkOpen(false)
      setSelectedIds(new Set())
      setAllMatching(false)
      const changes = [
        result.token_limit_per_user != null ? "预算" : null,
        result.model_policy_updated_count > 0 ? "模型访问" : null,
      ].filter(Boolean).join("和")
      setNotice(`已为 ${result.updated_count.toLocaleString(getIntlLocale())} 人更新${changes}`)
      void queryClient.invalidateQueries({ queryKey: ["finops", "budgets", "people", period, departmentId] })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.budgets(period) })
    },
  })
  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedSearch(search.trim()), 250)
    return () => window.clearTimeout(timeout)
  }, [search])
  useEffect(() => {
    setOffset(0)
    setSelectedIds(new Set())
    setAllMatching(false)
    setNotice(null)
    bulk.reset()
  }, [departmentId, debouncedSearch, status])
  const pageItems = people.data?.items ?? []
  const pageIds = pageItems.map((item) => item.scope_id)
  const allPageSelected = pageIds.length > 0 && pageIds.every((id) => selectedIds.has(id))
  const somePageSelected = pageIds.some((id) => selectedIds.has(id))
  const selectedCount = allMatching ? people.data?.total ?? 0 : selectedIds.size
  const setPageSelected = (checked: boolean) => {
    setAllMatching(false)
    setSelectedIds((current) => {
      const next = new Set(current)
      for (const id of pageIds) {
        if (checked) next.add(id)
        else next.delete(id)
      }
      return next
    })
  }
  const togglePerson = (id: string, checked: boolean) => {
    setAllMatching(false)
    setSelectedIds((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }
  const total = people.data?.total ?? 0
  const pageStart = total === 0 ? 0 : offset + 1
  const pageEnd = Math.min(offset + limit, total)
  return <section className="finops-panel people-budget-panel">
    <div className="people-budget-heading">
      <div><h2>人员预算与模型</h2><p>月度 Token 额度与长期模型访问分开管理；列表按需加载。</p></div>
      <div className="people-budget-summary">{people.data && <><span>{`${people.data.assigned_count.toLocaleString(getIntlLocale())} 预算已配置`}</span><i /><span>{`${people.data.model_configured_count.toLocaleString(getIntlLocale())} 模型已配置`}</span>{people.data.risk_count > 0 && <><i /><span className="risk">{`${people.data.risk_count.toLocaleString(getIntlLocale())} 风险`}</span></>}</>}</div>
    </div>
    <div className="people-budget-toolbar">
      <Select value={departmentId} onValueChange={(value) => value && onDepartmentChange(value)}>
        <SelectTrigger aria-label="人员预算部门" className="people-department-trigger"><SelectValue>{departments.find((item) => item.scope_id === departmentId)?.scope_name ?? "选择部门"}</SelectValue></SelectTrigger>
        <SelectContent align="start" alignItemWithTrigger={false}>{departments.map((item) => <SelectItem key={item.scope_id} value={item.scope_id}>{item.scope_name}</SelectItem>)}</SelectContent>
      </Select>
      <label className="people-search"><Search size={14} /><Input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索姓名或邮箱" aria-label="搜索人员" /></label>
      <Select value={status} onValueChange={(value) => value && setStatus(value as PeopleBudgetFilter)}>
        <SelectTrigger aria-label="人员预算状态" className="people-status-trigger"><SelectValue>{status === "all" ? "全部状态" : status === "assigned" ? "已配置" : statusLabels[status]}</SelectValue></SelectTrigger>
        <SelectContent align="start" alignItemWithTrigger={false}><SelectItem value="all">全部状态</SelectItem><SelectItem value="assigned">已配置</SelectItem><SelectItem value="unallocated">未分配</SelectItem><SelectItem value="healthy">正常</SelectItem><SelectItem value="warning">预警</SelectItem><SelectItem value="exceeded">已超额</SelectItem></SelectContent>
      </Select>
      {canManage && <Button type="button" disabled={selectedCount === 0 || people.isFetching} onClick={() => { bulk.reset(); setBulkOpen(true) }}><Users size={14} />批量分配{selectedCount > 0 ? ` (${selectedCount.toLocaleString(getIntlLocale())})` : ""}</Button>}
    </div>
    {notice && <div className="people-budget-notice"><CheckCircle2 size={14} />{notice}<button type="button" aria-label="关闭" onClick={() => setNotice(null)}><X size={13} /></button></div>}
    {people.error && <div className="finops-state error"><AlertTriangle size={18} /><b>人员预算加载失败</b><span>{queryError(people.error)}</span></div>}
    {!people.error && <div className="people-table-scroll"><ResizableGridTable className="people-table" role="table" aria-label="人员预算列表" headerSelector=".people-table-head" minWidths={[32, 180, 160, 100, 90, 90, 80, 42]} horizontalPadding={24}>
      <div className="people-table-head" role="row"><span>{canManage && <Checkbox checked={allPageSelected} indeterminate={!allPageSelected && somePageSelected} onCheckedChange={(checked) => setPageSelected(checked === true)} aria-label="选择当前页" />}</span><span className="people-name-heading">人员</span><span className="people-data-heading">模型访问</span><span className="people-data-heading">预算</span><span className="people-data-heading">已使用</span><span className="people-data-heading">剩余</span><span className="people-status-heading">状态</span><span /></div>
      {people.isLoading && <div className="people-table-state"><RefreshCw className="spin" size={16} />正在加载人员</div>}
      {!people.isLoading && pageItems.length === 0 && <div className="people-table-state"><UserRound size={18} />没有符合条件的人员</div>}
      {!people.isLoading && pageItems.map((item) => <div className="people-table-row" role="row" key={item.scope_id}>
        {canManage ? <Checkbox checked={selectedIds.has(item.scope_id) || allMatching} disabled={allMatching} onCheckedChange={(checked) => togglePerson(item.scope_id, checked === true)} aria-label={`选择 ${item.scope_name}`} /> : <span />}
        <div className="people-name-cell"><strong data-no-localize title={item.scope_name}>{item.scope_name}</strong>{item.scope_id !== item.scope_name && <small data-no-localize>{item.scope_id}</small>}</div>
        <div className="people-model-cell">{item.model_policy_configured
          ? item.allowed_model_ids.length > 0
            ? <><strong title={item.allowed_model_ids.map((id) => models.find((model) => model.id === id)?.display_name ?? id).join("、")}>{models.find((model) => model.id === item.allowed_model_ids[0])?.display_name ?? "已分配模型"}</strong><small>{item.allowed_model_ids.length > 1 ? `另有 ${item.allowed_model_ids.length - 1} 个` : "长期有效"}</small></>
            : <><strong>禁止全部</strong><small>已配置策略</small></>
          : <><strong>未配置</strong><small>兼容现有权限</small></>}</div>
        <div><strong>{item.token_limit == null ? "--" : formatFullTokens(item.token_limit)}</strong><small>{item.token_limit == null ? "未分配" : `预警 ${item.warning_threshold_percent}%`}</small></div>
        <div><strong>{formatFullTokens(item.used_tokens)}</strong><small>{item.usage_percent == null ? "--" : `${item.usage_percent}%`}</small></div>
        <div><strong>{item.remaining_tokens == null ? "--" : formatFullTokens(item.remaining_tokens)}</strong><small>Token</small></div>
        <div className="budget-status-cell"><span data-status={item.status}>{statusLabels[item.status]}</span></div>
        {canManage ? <Button type="button" variant="ghost" size="icon-sm" className="people-budget-edit-button" title="设置人员预算与模型" onClick={() => { setSelectedIds(new Set([item.scope_id])); setAllMatching(false); bulk.reset(); setBulkOpen(true) }}><Pencil size={14} /></Button> : <span />}
      </div>)}
    </ResizableGridTable></div>}
    {people.data && <div className="people-table-footer">
      <div><span>{`显示 ${pageStart.toLocaleString(getIntlLocale())}–${pageEnd.toLocaleString(getIntlLocale())}，共 ${total.toLocaleString(getIntlLocale())} 人`}</span>{canManage && allPageSelected && total > pageItems.length && !allMatching && <button type="button" onClick={() => { setAllMatching(true); setSelectedIds(new Set()) }}>{`选择全部 ${total.toLocaleString(getIntlLocale())} 个结果`}</button>}{canManage && allMatching && <><b>已选择全部匹配结果</b><button type="button" onClick={() => setAllMatching(false)}>清除选择</button></>}</div>
      <div><Button type="button" variant="outline" size="icon-sm" aria-label="上一页" disabled={offset === 0 || people.isFetching} onClick={() => setOffset(Math.max(0, offset - limit))}><ChevronLeft size={14} /></Button><Button type="button" variant="outline" size="icon-sm" aria-label="下一页" disabled={offset + limit >= total || people.isFetching} onClick={() => setOffset(offset + limit)}><ChevronRight size={14} /></Button></div>
    </div>}
    {canManage && bulkOpen && people.data && <BulkPeopleEditor people={people.data} models={models} selection={allMatching ? "all_matching" : "ids"} selectedIds={[...selectedIds]} query={debouncedSearch} status={status} busy={bulk.isPending} error={bulk.error ? queryError(bulk.error) : null} onClose={() => { if (!bulk.isPending) setBulkOpen(false) }} onSave={(write) => bulk.mutate(write)} />}
  </section>
}

type BudgetListLimit = 5 | 10 | 25

const budgetListLimits: BudgetListLimit[] = [5, 10, 25]

function budgetListLimitLabel(locale: LocalePreference, limit: BudgetListLimit) {
  if (locale === "en") return `Top ${limit}`
  if (locale === "ko") return `상위 ${limit}`
  if (locale === "ja") return `上位 ${limit}`
  return `前 ${limit}`
}

function budgetListCountLabel(locale: LocalePreference) {
  if (locale === "en") return "Items shown"
  if (locale === "ko") return "표시 개수"
  if (locale === "ja") return "表示件数"
  if (locale === "zh-TW") return "顯示數量"
  return "显示数量"
}

function BudgetListLimitSelect({ value, onChange, label }: { value: BudgetListLimit; onChange: (value: BudgetListLimit) => void; label: string }) {
  const { locale } = useLocale()
  return <Select value={String(value)} onValueChange={(next) => next && onChange(Number(next) as BudgetListLimit)}>
    <SelectTrigger size="sm" className="ranking-limit-trigger" aria-label={`${label} · ${budgetListCountLabel(locale)}`}><SelectValue>{budgetListLimitLabel(locale, value)}</SelectValue></SelectTrigger>
    <SelectContent align="end" alignItemWithTrigger={false}>{budgetListLimits.map((limit) => <SelectItem key={limit} value={String(limit)}>{budgetListLimitLabel(locale, limit)}</SelectItem>)}</SelectContent>
  </Select>
}

function BudgetRiskPanel({ data }: { data: TokenBudgetResponse }) {
  const [limit, setLimit] = useState<BudgetListLimit>(5)
  const risks = data.risk_items
  return <section className="finops-panel budget-risk-panel">
    <div className="finops-panel-title"><h2>预算风险</h2><div className="finops-panel-title-actions"><span className="finops-panel-title-meta">{`${data.risk_count} 个信号`}</span><BudgetListLimitSelect value={limit} onChange={setLimit} label="预算风险" /></div></div>
    {risks.length === 0
      ? <div className="budget-empty-state"><CheckCircle2 size={20} /><strong>当前无预算风险</strong><span>达到预警阈值或预计超额的范围会出现在这里。</span></div>
      : <div className="budget-risk-list">{risks.slice(0, limit).map((item) => <div key={`${item.scope_type}:${item.scope_id}`}>
          <span data-status={item.status}>{item.status === "exceeded" ? <CircleSlash2 size={14} /> : <AlertTriangle size={14} />}</span>
          <div><strong>{item.scope_name}</strong><small>{scopeLabels[item.scope_type]} · 已使用 {item.usage_percent ?? 0}%</small></div>
          <div className="budget-risk-forecast"><small>月底预测</small><b>{item.forecast_percent ?? 0}%</b></div>
        </div>)}</div>}
  </section>
}

function modelAccessValue(
  modelIds: string[],
  models: ManagedModel[],
  emptyLabel: string,
  recordedNames: string[] = [],
) {
  if (modelIds.length === 0) return emptyLabel
  const enabledIds = new Set(
    models.filter((model) => model.enabled).map((model) => model.id),
  )
  const isAllEnabled = enabledIds.size === modelIds.length
    && modelIds.every((modelId) => enabledIds.has(modelId))
  if (isAllEnabled) return `全部 ${modelIds.length} 个模型`
  const names = modelIds.map((modelId, index) => (
    models.find((model) => model.id === modelId)?.display_name
    ?? recordedNames[index]
    ?? modelId
  ))
  return names.length <= 2 ? names.join("、") : `${names.length} 个模型`
}

function BudgetHistory({ data, models }: { data: TokenBudgetResponse; models: ManagedModel[] }) {
  const [limit, setLimit] = useState<BudgetListLimit>(5)
  const history = data.history.filter((event) => (
    event.event_type !== "budget"
    || event.action !== "updated"
    || event.previous_token_limit !== event.new_token_limit
    || event.previous_warning_threshold_percent !== event.new_warning_threshold_percent
  ))
  return <section className="finops-panel budget-history-panel">
    <div className="finops-panel-title"><h2>预算与模型变更</h2><div className="finops-panel-title-actions"><span className="finops-panel-title-meta">最近 {history.length} 条</span><BudgetListLimitSelect value={limit} onChange={setLimit} label="预算与模型变更" /></div></div>
    {history.length === 0
      ? <div className="budget-empty-state"><CalendarDays size={20} /><strong>本月暂无变更</strong><span>预算和模型访问调整会记录在这里。</span></div>
      : <div className="budget-history-list">{history.slice(0, limit).map((event) => {
          if (event.event_type === "enforcement") {
            const label = (mode: EnforcementMode | null) =>
              mode === "block" ? "拦截" : mode === "audit" ? "仅告警" : "未配置"
            return <div key={event.id}>
              <span data-action={`enforcement_${event.new_mode}`}>{label(event.new_mode)}</span>
              <div><strong>{event.department_name}</strong><small>拦截开关 · {event.changed_by}</small></div>
              <p>{label(event.previous_mode)} → {label(event.new_mode)}</p>
              <time>{formatDateTime(event.changed_at)}</time>
            </div>
          }
          if (event.event_type === "model_access") {
            const previous = modelAccessValue(event.previous_model_ids, models, "0 个模型", event.previous_model_names)
            const next = modelAccessValue(event.new_model_ids, models, "禁止全部", event.new_model_names)
            return <div key={event.id}>
              <span data-action="model_access">模型</span>
              <div><strong>{event.user_name}</strong><small>模型访问 · {event.changed_by}</small></div>
              <p title={`${previous} → ${next}`}>{previous} → {next}</p>
              <time>{formatDateTime(event.changed_at)}</time>
            </div>
          }
          const tokenChanged = event.previous_token_limit !== event.new_token_limit
          const thresholdChanged = event.previous_warning_threshold_percent !== event.new_warning_threshold_percent
          return <div key={event.id}>
            <span data-action={event.action}>Token</span>
            <div><strong>{event.scope_name}</strong><small>{scopeLabels[event.scope_type]} · {event.changed_by}</small></div>
            <p>{(tokenChanged || event.action !== "updated") && (event.action === "removed"
              ? `${formatFullTokens(event.previous_token_limit ?? 0)} → 未分配`
              : `${event.previous_token_limit == null ? "未分配" : formatFullTokens(event.previous_token_limit)} → ${formatFullTokens(event.new_token_limit ?? 0)}`)}
              {event.action === "updated" && thresholdChanged && <>{tokenChanged && " · "}<span>预警阈值</span> {event.previous_warning_threshold_percent}% → {event.new_warning_threshold_percent}%</>}</p>
            <time>{formatDateTime(event.changed_at)}</time>
          </div>
        })}</div>}
  </section>
}

export function BudgetManagementPage({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const canManage = user?.role === "owner"
  const [period, setPeriod] = useState(currentPeriod)
  const [editing, setEditing] = useState<TokenBudgetItem | null>(null)
  const [peopleDepartmentId, setPeopleDepartmentId] = useState("")
  const periodBounds = useMemo(() => periodBoundsFor(currentPeriod()), [])
  const query = useQuery(finopsQueries.budgets(period))
  const registry = useQuery(finopsQueries.registry())
  const save = useMutation({
    mutationFn: ({ item, value }: { item: TokenBudgetItem; value: TokenBudgetWrite }) =>
      dataSource.saveBudget(period, item.scope_type, item.scope_id, value),
    onSuccess: (data) => {
      queryClient.setQueryData(finopsKeys.budgets(period), data)
      setEditing(null)
    },
  })
  const remove = useMutation({
    mutationFn: (item: TokenBudgetItem) =>
      dataSource.deleteBudget(period, item.scope_type, item.scope_id),
    onSuccess: (data) => {
      queryClient.setQueryData(finopsKeys.budgets(period), data)
      setEditing(null)
    },
  })
  const enforcement = useMutation({
    mutationFn: ({ departmentId, mode }: { departmentId: string; mode: EnforcementMode }) =>
      dataSource.saveDepartmentEnforcement(period, departmentId, mode),
    onSuccess: (data) => { queryClient.setQueryData(finopsKeys.budgets(period), data) },
  })
  const busy = save.isPending || remove.isPending
  const mutationError = save.error ?? remove.error ?? enforcement.error
  const refreshBudgets = () => {
    if (!query.isFetching && !busy) {
      void queryClient.refetchQueries({ queryKey: ["finops", "budgets"], type: "active" })
    }
  }
  const changePeriod = (value: string) => {
    if (!value) return
    setPeriod(value)
    setEditing(null)
    save.reset()
    remove.reset()
  }
  const departments = query.data?.items.filter((item) => item.scope_type === "department") ?? []
  const enabledModels = registry.data?.models.filter((model) => model.enabled) ?? []
  // The badge used to read "soft budget, alerts only" unconditionally. That stopped being
  // true the moment enforcement became a per-department mode: a department set to Block has
  // its over-allowance people refused, so a static claim contradicted the very column that
  // sits under it. It now reports the real, mixed state instead.
  const enforcementRows = query.data?.enforcement ?? []
  const blocking = enforcementRows.filter((row) => row.mode === "block").length
  const enforcementLabel = blocking === 0
    ? "软预算 · 仅告警"
    : blocking === enforcementRows.length
      ? "硬预算 · 超额拦截"
      : `${blocking}/${enforcementRows.length} 个部门拦截`
  const activePeopleDepartmentId = peopleDepartmentId || departments[0]?.scope_id || ""
  const managePeople = (departmentId: string) => {
    setPeopleDepartmentId(departmentId)
    requestAnimationFrame(() => document.querySelector(".people-budget-panel")?.scrollIntoView({ behavior: "smooth", block: "start" }))
  }
  return <div className="finops-workspace budget-workspace">
    <header className="finops-header">
      <div><Button variant="ghost" size="icon-sm" className="finops-sidebar-trigger" aria-label="切换导航栏" title="切换导航栏" onClick={onToggleSidebar}><PanelLeft size={16} /></Button><span className="finops-header-icon"><WalletCards size={17} /></span><h1>预算管理</h1></div>
      <Button variant="ghost" size="icon-sm" className="finops-header-refresh" aria-label="刷新预算数据" title="刷新预算数据" disabled={query.isFetching || busy} onClick={refreshBudgets}><RefreshCw className={query.isFetching ? "spin" : undefined} size={15} /></Button>
    </header>
    <div className="finops-filterbar budget-filterbar">
      <div className="budget-period-control"><CalendarDays size={14} /><MonthPicker value={period} min={periodBounds.min} max={periodBounds.max} onChange={changePeriod} label="预算周期" ariaLabel="预算周期" /></div>
      {query.data && <span className="budget-mode-badge" data-blocking={blocking > 0 || undefined}>{enforcementLabel}</span>}
    </div>
    <div className="finops-scroll-region">
      <div className="finops-content budget-content">
        {query.isLoading && <div className="finops-state"><RefreshCw className="spin" size={18} />正在加载预算</div>}
        {query.error && <div className="finops-state error"><AlertTriangle size={18} /><b>预算接口不可用</b><span>{queryError(query.error)}</span></div>}
        {query.data && <>
          <BudgetKpis data={query.data} />
          <BudgetTable
            items={query.data.items}
            canManage={canManage}
            onEdit={(item) => { save.reset(); remove.reset(); setEditing(item) }}
            onManagePeople={managePeople}
            enforcement={query.data.enforcement}
            enforcementBusy={enforcement.isPending}
            onToggleEnforcement={(departmentId, mode) => enforcement.mutate({ departmentId, mode })}
          />
          {activePeopleDepartmentId && <PeopleBudgetWorkspace period={period} canManage={canManage} departments={departments} models={enabledModels} departmentId={activePeopleDepartmentId} onDepartmentChange={setPeopleDepartmentId} />}
          <div className="budget-secondary-grid">
            <BudgetRiskPanel data={query.data} />
            <BudgetHistory data={query.data} models={registry.data?.models ?? []} />
          </div>
        </>}
      </div>
    </div>
    {canManage && editing && query.data && <BudgetEditor
      key={`${period}:${editing.scope_type}:${editing.scope_id}`}
      item={editing}
      items={query.data.items}
      busy={busy}
      error={mutationError ? queryError(mutationError) : null}
      onClose={() => { if (!busy) setEditing(null) }}
      onSave={(value) => save.mutate({ item: editing, value })}
      onDelete={() => remove.mutate(editing)}
    />}
  </div>
}
