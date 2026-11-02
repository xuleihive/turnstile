import {
  type CSSProperties,
  useEffect,
  useMemo,
  useState,
} from "react"
import { createPortal } from "react-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertCircle,
  Blocks,
  Building2,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Copy,
  FileSliders,
  GitBranch,
  MoreHorizontal,
  Pencil,
  Plus,
  PowerOff,
  RefreshCw,
  Save,
  Search,
  Send,
  Trash2,
  Users,
  Workflow,
  X,
} from "lucide-react"

import { RouterCanvas } from "../components/model-router/router-canvas"
import { RouterConfigForm } from "../components/model-router/router-config-form"
import {
  ROUTER_NATIVE_FALLBACK_NOTICE,
  cloneDefinition,
  copyName,
  createStarterDefinition,
  normalizeRouterDefinition,
  resolveRouterDisplayModels,
} from "../components/model-router/router-definition"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../components/ui/alert-dialog"
import { Button } from "../components/ui/button"
import { ButtonGroup } from "../components/ui/button-group"
import { Checkbox } from "../components/ui/checkbox"
import {
  Dialog,
  DialogClose,
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
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu"
import { Input } from "../components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../components/ui/select"
import { Textarea } from "../components/ui/textarea"
import { useResizablePane } from "../components/ui/use-resizable-pane"
import { dataSource } from "../data-sources/apim/api"
import { finopsKeys, finopsQueries } from "../data-sources/apim/queries"
import type {
  ManagedModel,
  ModelRouter,
  ModelRouterBulkAssignmentWrite,
  ModelRouterList,
  ModelRouterModelReference,
  ModelRouterPersonAccessItem,
  ModelRouterStatus,
  ModelRouterValidation,
  ModelRouterWrite,
} from "../data-sources/apim/types"
import { getIntlLocale, useLocale } from "../locales/index"
import { useIsMobile } from "../hooks/use-mobile"
import { useAuth } from "../providers/auth-provider"


type RouterMode = "canvas" | "config"
type RouterFilter = "all" | ModelRouterStatus

const ROUTER_CONFIG_MODE_ENABLED = false
const ROUTER_PANE_WIDTH_KEY = "turnstile_model_router_pane_width"
const ROUTER_PANE_WIDTH_DEFAULT = 304
const ROUTER_PANE_WIDTH_MIN = 240
const ROUTER_PANE_WIDTH_MAX = 420

const STATUS_LABELS: Record<ModelRouterStatus, string> = {
  draft: "草稿",
  published: "已发布",
  disabled: "已停用",
}

const ROUTER_VALIDATION_MESSAGES: Record<string, string> = {
  "One or more candidate models are disabled or unavailable": "一个或多个候选模型已停用或不可用",
  "One or more candidate models are disabled or not assigned to this user": "一个或多个候选模型已停用或未分配给当前用户",
  "A model router with this name already exists": "同名模型路由器已存在",
  "Only a published model router can be assigned": "仅已发布的模型路由器可分配权限",
  "Only a published model router can be disabled": "仅已发布的模型路由器可停用",
  "The Strategy has no callable candidate models": "该策略没有可调用的候选模型",
}

function formatRouterValidationMessage(message: string) {
  return ROUTER_VALIDATION_MESSAGES[message] ?? message
}

function routerIdFromUrl() {
  return new URL(window.location.href).searchParams.get("router")
}

function writeRouterUrl(routerId: string | null, push = false) {
  const url = new URL(window.location.href)
  if (routerId) url.searchParams.set("router", routerId)
  else url.searchParams.delete("router")
  window.history[push ? "pushState" : "replaceState"](null, "", `${url.pathname}${url.search}${url.hash}`)
}

function writeFromRouter(router: ModelRouter): ModelRouterWrite {
  return {
    name: router.name,
    description: router.description,
    definition: normalizeRouterDefinition(router.definition),
  }
}

function serializedWrite(value: ModelRouterWrite) {
  return JSON.stringify(value)
}

function relativeUpdatedAt(value: string) {
  const date = new Date(value)
  const elapsedMinutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60_000))
  const locale = getIntlLocale()
  if (elapsedMinutes < 1) return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(0, "second")
  if (elapsedMinutes < 60) return new Intl.RelativeTimeFormat(locale, { numeric: "always" }).format(-elapsedMinutes, "minute")
  const hours = Math.floor(elapsedMinutes / 60)
  if (hours < 24) return new Intl.RelativeTimeFormat(locale, { numeric: "always" }).format(-hours, "hour")
  return new Intl.DateTimeFormat(locale, { month: "short", day: "numeric" }).format(date)
}

function RouterCreateDialog({
  open,
  models,
  busy,
  error,
  onOpenChange,
  onCreate,
}: {
  open: boolean
  models: ManagedModel[]
  busy: boolean
  error: string | null
  onOpenChange: (open: boolean) => void
  onCreate: (value: ModelRouterWrite) => void
}) {
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [modelIds, setModelIds] = useState<Set<string>>(() => new Set())
  const [search, setSearch] = useState("")
  useEffect(() => {
    if (!open) return
    setName("")
    setDescription("")
    setModelIds(new Set())
    setSearch("")
  }, [open])
  const candidateModels = models.filter((model) => !model.capabilities.includes("decision"))
  const visibleModels = candidateModels.filter((model) =>
    `${model.display_name} ${model.model_key} ${model.provider_name}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()),
  )
  return <Dialog open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next) }}>
    <DialogContent className="registry-editor-dialog router-create-dialog" finalFocus={false}>
      <form className="registry-editor router-create-form" onSubmit={(event) => {
        event.preventDefault()
        if (!name.trim()) return
        onCreate({ name: name.trim(), description: description.trim(), definition: createStarterDefinition([...modelIds]) })
      }}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>新建模型路由器</DialogTitle>
          <DialogDescription>定义用途，并选择首批候选模型。</DialogDescription>
        </DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body router-create-body">
          <div className="router-create-basics">
            <label className="registry-field"><span className="registry-field-label">名称</span><Input autoFocus required maxLength={120} placeholder="例如：客服问答路由" value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label className="registry-field"><span className="registry-field-label">描述</span><Textarea rows={2} maxLength={500} placeholder="说明业务场景与路由目标" value={description} onChange={(event) => setDescription(event.target.value)} /></label>
          </div>
          <div className="router-create-models">
            <div className="router-create-models-head"><b>初始候选模型</b><small>{modelIds.size} / {candidateModels.length} 已选</small></div>
            <div className="smh-search router-create-search"><Search size={14} /><input aria-label="搜索候选模型" placeholder="搜索模型或提供方..." value={search} onChange={(event) => setSearch(event.target.value)} /></div>
            <div className="router-create-model-list">{visibleModels.map((model) => <label key={model.id}><Checkbox checked={modelIds.has(model.id)} onCheckedChange={(checked) => setModelIds((current) => { const next = new Set(current); if (checked === true) next.add(model.id); else next.delete(model.id); return next })} /><span><b>{model.display_name}</b><small>{model.provider_name} · {model.capabilities.join(" / ")}</small></span></label>)}</div>
            {!visibleModels.length && <div className="router-config-empty">没有匹配的可用模型。</div>}
          </div>
          {error && <div className="registry-error">{error}</div>}
        </div>
        <DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose><Button type="submit" disabled={busy || !name.trim()}>{busy ? <RefreshCw className="spin" size={14} /> : <Plus size={14} />}<span>创建路由器</span></Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function RouterNameDialog({
  mode,
  router,
  initialValue,
  busy,
  error,
  onClose,
  onSubmit,
}: {
  mode: "rename" | "duplicate"
  router: ModelRouter
  initialValue: ModelRouterWrite
  busy: boolean
  error: string | null
  onClose: () => void
  onSubmit: (name: string, description: string) => void
}) {
  const { locale } = useLocale()
  const [name, setName] = useState(mode === "duplicate" ? copyName(router.name, locale) : initialValue.name)
  const [description, setDescription] = useState(initialValue.description)
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <DialogContent className="registry-editor-dialog simple-model-dialog" finalFocus={false}>
      <form className="registry-editor simple-model-form" onSubmit={(event) => { event.preventDefault(); if (name.trim()) onSubmit(name.trim(), description.trim()) }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>{mode === "duplicate" ? "复制路由器" : "编辑基础信息"}</DialogTitle><DialogDescription>{router.name}</DialogDescription></DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body simple-model-body">
          <label className="router-field"><span>名称</span><Input autoFocus maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></label>
          {mode === "rename" && <label className="router-field"><span>描述</span><Textarea rows={3} maxLength={500} value={description} onChange={(event) => setDescription(event.target.value)} /></label>}
          {error && <div className="registry-error">{error}</div>}
        </div>
        <DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose><Button type="submit" disabled={busy || !name.trim()}>{busy && <RefreshCw className="spin" size={14} />}{mode === "duplicate" ? "创建副本" : "保存"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function RouterAccessDialog({
  router,
  busy,
  error,
  onClose,
  onSave,
}: {
  router: ModelRouter
  busy: boolean
  error: string | null
  onClose: () => void
  onSave: (write: ModelRouterBulkAssignmentWrite) => void
}) {
  const [search, setSearch] = useState("")
  const [debouncedSearch, setDebouncedSearch] = useState("")
  const [selectedDeptId, setSelectedDeptId] = useState<string>("all")
  const [offset, setOffset] = useState(0)
  const [collapsedDepts, setCollapsedDepts] = useState<Set<string>>(new Set())
  const limit = 50

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    setOffset(0)
  }, [selectedDeptId, debouncedSearch])

  const people = useQuery(
    finopsQueries.modelRouterPeople(
      router.id,
      selectedDeptId,
      debouncedSearch,
      offset,
      limit,
    ),
  )

  const [allMatchingDraft, setAllMatchingDraft] = useState<{
    assigned: boolean
    departmentId: string
    query: string
  } | null>(null)
  const [deptDraft, setDeptDraft] = useState<
    Map<string, { assigned: boolean; query: string }>
  >(new Map())
  const [userDraft, setUserDraft] = useState<Map<string, boolean>>(new Map())

  const isPersonAssigned = (person: ModelRouterPersonAccessItem): boolean => {
    if (userDraft.has(person.id)) return userDraft.get(person.id)!
    if (person.department_id && deptDraft.has(person.department_id)) {
      return deptDraft.get(person.department_id)!.assigned
    }
    if (
      allMatchingDraft &&
      (allMatchingDraft.departmentId === "all" ||
        allMatchingDraft.departmentId === person.department_id)
    ) {
      return allMatchingDraft.assigned
    }
    return person.assigned
  }

  const departments = people.data?.departments ?? []
  const pageItems = people.data?.items ?? []
  const total = people.data?.total ?? 0
  const totalUsers = people.data?.total_users ?? 0

  const filteredGroups = useMemo(() => {
    const groupMap = new Map<
      string,
      { id: string; name: string; visibleUsers: ModelRouterPersonAccessItem[] }
    >()
    for (const person of pageItems) {
      const deptId = person.department_id ?? "__unassigned__"
      let group = groupMap.get(deptId)
      if (!group) {
        group = { id: deptId, name: person.department_name, visibleUsers: [] }
        groupMap.set(deptId, group)
      }
      group.visibleUsers.push(person)
    }
    return [...groupMap.values()]
  }, [pageItems])

  const effectiveAssignedCount = useMemo(() => {
    if (!people.data) return 0
    if (allMatchingDraft && allMatchingDraft.departmentId === "all" && !allMatchingDraft.query) {
      let base = allMatchingDraft.assigned ? totalUsers : 0
      for (const [deptId, draft] of deptDraft.entries()) {
        const summary = departments.find((dept) => dept.id === deptId)
        if (summary && draft.assigned !== allMatchingDraft.assigned) {
          base += draft.assigned ? summary.total_users : -summary.total_users
        }
      }
      for (const [, nextVal] of userDraft.entries()) {
        if (nextVal !== allMatchingDraft.assigned) {
          base += nextVal ? 1 : -1
        }
      }
      return Math.max(0, Math.min(totalUsers, base))
    }
    let count = 0
    for (const summary of departments) {
      if (deptDraft.has(summary.id)) {
        count += deptDraft.get(summary.id)!.assigned ? summary.total_users : 0
      } else if (
        allMatchingDraft &&
        allMatchingDraft.departmentId === summary.id &&
        !allMatchingDraft.query
      ) {
        count += allMatchingDraft.assigned ? summary.total_users : 0
      } else {
        count += summary.assigned_users
      }
    }
    for (const person of pageItems) {
      if (!userDraft.has(person.id)) continue
      const userVal = userDraft.get(person.id)!
      const deptVal =
        person.department_id && deptDraft.has(person.department_id)
          ? deptDraft.get(person.department_id)!.assigned
          : allMatchingDraft &&
              (allMatchingDraft.departmentId === "all" ||
                allMatchingDraft.departmentId === person.department_id)
            ? allMatchingDraft.assigned
            : person.assigned
      if (userVal !== deptVal) {
        count += userVal ? 1 : -1
      }
    }
    return Math.max(0, Math.min(totalUsers, count))
  }, [allMatchingDraft, departments, deptDraft, pageItems, people.data, totalUsers, userDraft])

  const hasPendingChanges = Boolean(
    allMatchingDraft !== null || deptDraft.size > 0 || userDraft.size > 0,
  )

  const togglePerson = (person: ModelRouterPersonAccessItem, nextAssigned: boolean) => {
    setUserDraft((current) => {
      const next = new Map(current)
      const baseline =
        person.department_id && deptDraft.has(person.department_id)
          ? deptDraft.get(person.department_id)!.assigned
          : allMatchingDraft &&
              (allMatchingDraft.departmentId === "all" ||
                allMatchingDraft.departmentId === person.department_id)
            ? allMatchingDraft.assigned
            : person.assigned
      if (nextAssigned === baseline && !deptDraft.has(person.department_id ?? "") && !allMatchingDraft) {
        next.delete(person.id)
      } else {
        next.set(person.id, nextAssigned)
      }
      return next
    })
  }

  const toggleEntireDept = (deptId: string, nextAssigned: boolean) => {
    setDeptDraft((current) => {
      const next = new Map(current)
      next.set(deptId, { assigned: nextAssigned, query: debouncedSearch })
      return next
    })
    setUserDraft((current) => {
      const next = new Map(current)
      for (const person of pageItems) {
        if (person.department_id === deptId) next.delete(person.id)
      }
      return next
    })
  }

  const applyBulkMatching = (nextAssigned: boolean) => {
    setAllMatchingDraft({
      assigned: nextAssigned,
      departmentId: selectedDeptId,
      query: debouncedSearch,
    })
    if (selectedDeptId === "all") {
      setDeptDraft(new Map())
    } else {
      setDeptDraft((current) => {
        const next = new Map(current)
        next.delete(selectedDeptId)
        return next
      })
    }
    setUserDraft(new Map())
  }

  const toggleDeptCollapse = (deptId: string) => {
    setCollapsedDepts((current) => {
      const next = new Set(current)
      if (next.has(deptId)) next.delete(deptId)
      else next.add(deptId)
      return next
    })
  }

  const allVisibleAssigned =
    pageItems.length > 0 && pageItems.every((person) => isPersonAssigned(person))
  const anyVisibleAssigned = pageItems.some((person) => isPersonAssigned(person))
  const pageStart = total === 0 ? 0 : offset + 1
  const pageEnd = Math.min(offset + limit, total)

  const submitSave = () => {
    onSave({
      all_matching_assigned: allMatchingDraft ? allMatchingDraft.assigned : null,
      department_id: allMatchingDraft ? allMatchingDraft.departmentId : null,
      query: allMatchingDraft ? allMatchingDraft.query || null : null,
      department_assignments: [...deptDraft.entries()].map(([department_id, item]) => ({
        department_id,
        assigned: item.assigned,
        query: item.query || null,
      })),
      changes: [...userDraft.entries()].map(([user_id, assigned]) => ({
        user_id,
        assigned,
      })),
    })
  }

  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <DialogContent className="registry-editor-dialog router-access-dialog" finalFocus={false}>
      <div className="registry-editor router-access-editor">
        <DialogHeader className="registry-editor-header"><DialogTitle>人员访问</DialogTitle><DialogDescription>{router.name} · 已发布 v{router.version}</DialogDescription></DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body router-access-body">
          <div className="router-access-toolbar">
            {departments.length > 0 && <Select value={selectedDeptId} onValueChange={(value) => value && setSelectedDeptId(value)}>
              <SelectTrigger aria-label="所属部门" className="router-access-dept-select">
                <SelectValue>{selectedDeptId === "all" ? "全部部门" : departments.find((dept) => dept.id === selectedDeptId)?.name ?? "选择部门"}</SelectValue>
              </SelectTrigger>
              <SelectContent align="start" alignItemWithTrigger={false}>
                <SelectItem value="all">全部部门</SelectItem>
                {departments.map((dept) => <SelectItem key={dept.id} value={dept.id}>{dept.name}</SelectItem>)}
              </SelectContent>
            </Select>}
            <label className="people-model-search router-access-search"><Search size={14} /><Input autoFocus value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索姓名或邮箱" aria-label="搜索路由器人员" /></label>
          </div>
          <div className="router-access-summary">
            <div className="router-access-summary-meta">
              <span>{effectiveAssignedCount.toLocaleString(getIntlLocale())} / {totalUsers.toLocaleString(getIntlLocale())} 已分配</span>
              <small>候选模型访问由保存时一并校验</small>
            </div>
            {!people.isLoading && pageItems.length > 0 && <div className="router-access-bulk-actions">
              <button type="button" disabled={busy || allVisibleAssigned} onClick={() => applyBulkMatching(true)}>全选</button>
              <button type="button" disabled={busy || !anyVisibleAssigned} onClick={() => applyBulkMatching(false)}>清空</button>
            </div>}
          </div>
          {people.isLoading ? <div className="router-access-state"><RefreshCw className="spin" size={15} />正在加载人员访问</div> : <div className="people-model-select-list router-access-list" role="group" aria-label="模型路由器人员访问">
            {filteredGroups.map((group) => {
              const summary = departments.find((dept) => dept.id === group.id)
              const visibleAssigned = group.visibleUsers.filter((person) => isPersonAssigned(person)).length
              const deptTotal = !debouncedSearch && summary ? summary.total_users : group.visibleUsers.length
              const deptAssignedCount = !debouncedSearch && summary
                ? deptDraft.has(group.id)
                  ? deptDraft.get(group.id)!.assigned ? summary.total_users : 0
                  : allMatchingDraft && (allMatchingDraft.departmentId === "all" || allMatchingDraft.departmentId === group.id)
                    ? allMatchingDraft.assigned ? summary.total_users : 0
                    : Math.max(
                        0,
                        Math.min(
                          summary.total_users,
                          summary.assigned_users +
                            group.visibleUsers.reduce(
                              (delta, person) =>
                                userDraft.has(person.id) && userDraft.get(person.id) !== person.assigned
                                  ? delta + (userDraft.get(person.id) ? 1 : -1)
                                  : delta,
                              0,
                            ),
                        ),
                      )
                : visibleAssigned
              const allDeptAssigned = deptTotal > 0 && deptAssignedCount === deptTotal
              const someDeptAssigned = deptAssignedCount > 0 && !allDeptAssigned
              const collapsed = collapsedDepts.has(group.id) && !debouncedSearch
              return <div className="router-access-dept-group" key={group.id}>
                <div className="router-access-dept-header">
                  <Checkbox
                    checked={allDeptAssigned}
                    indeterminate={someDeptAssigned}
                    disabled={busy}
                    aria-label={`${group.name} · 选择整个部门`}
                    onCheckedChange={(checked) => toggleEntireDept(group.id, checked === true)}
                  />
                  <button type="button" className="router-access-dept-toggle" onClick={() => toggleDeptCollapse(group.id)} aria-expanded={!collapsed}>
                    <Building2 size={13} />
                    <b>{group.name}</b>
                    <small>{deptAssignedCount.toLocaleString(getIntlLocale())} / {deptTotal.toLocaleString(getIntlLocale())} 已分配</small>
                    <ChevronDown className={collapsed ? "collapsed" : ""} size={14} />
                  </button>
                </div>
                {!collapsed && <div className="router-access-dept-members">
                  {group.visibleUsers.map((person) => <label className="people-model-option" key={person.id}><Checkbox checked={isPersonAssigned(person)} disabled={busy} onCheckedChange={(checked) => togglePerson(person, checked === true)} /><span><b>{person.name}</b><small>{person.id}</small></span></label>)}
                </div>}
              </div>
            })}
            {!pageItems.length && <div className="people-model-empty">没有匹配的人员</div>}
          </div>}
          {total > limit && <div className="router-access-pagination">
            <span>{`显示 ${pageStart.toLocaleString(getIntlLocale())}–${pageEnd.toLocaleString(getIntlLocale())}，共 ${total.toLocaleString(getIntlLocale())} 人`}</span>
            <div>
              <Button type="button" variant="outline" size="icon-sm" aria-label="上一页" disabled={busy || offset === 0 || people.isFetching} onClick={() => setOffset(Math.max(0, offset - limit))}><ChevronLeft size={14} /></Button>
              <Button type="button" variant="outline" size="icon-sm" aria-label="下一页" disabled={busy || offset + limit >= total || people.isFetching} onClick={() => setOffset(offset + limit)}><ChevronRight size={14} /></Button>
            </div>
          </div>}
          {(error || people.error) && <div className="registry-error">{error ?? String(people.error)}</div>}
        </div>
        <DialogFooter className="registry-editor-footer">
          <DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose>
          <Button type="button" disabled={busy || !hasPendingChanges} onClick={submitSave}>{busy && <RefreshCw className="spin" size={14} />}保存</Button>
        </DialogFooter>
      </div>
    </DialogContent>
  </Dialog>
}

function RouterListPane({
  className = "",
  routers,
  selectedId,
  search,
  filter,
  canManage,
  busy,
  dirty,
  onSearchChange,
  onFilterChange,
  onSelect,
  onRename,
  onDuplicate,
  onAccess,
  onDisable,
  onDelete,
}: {
  className?: string
  routers: ModelRouter[]
  selectedId: string | null
  search: string
  filter: RouterFilter
  canManage: boolean
  busy: boolean
  dirty: boolean
  onSearchChange: (value: string) => void
  onFilterChange: (value: RouterFilter) => void
  onSelect: (routerId: string) => void
  onRename: (router: ModelRouter) => void
  onDuplicate: (router: ModelRouter) => void
  onAccess: (router: ModelRouter) => void
  onDisable: (router: ModelRouter) => void
  onDelete: (router: ModelRouter) => void
}) {
  const counts = {
    all: routers.length,
    draft: routers.filter((router) => router.status === "draft").length,
    published: routers.filter((router) => router.status === "published").length,
    disabled: routers.filter((router) => router.status === "disabled").length,
  }
  const matches = routers.filter((router) => {
    const searchMatch = `${router.name} ${router.description}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())
    return searchMatch && (filter === "all" || router.status === filter)
  })
  return <aside className={`router-list-pane ${className}`}>
    <div className="router-list-controls">
      <div className="smh-search"><Search size={15} /><input aria-label="搜索模型路由器" placeholder="搜索模型路由器..." value={search} onChange={(event) => onSearchChange(event.target.value)} /></div>
      <div className="router-status-filters">
        {(["all", "draft", "published", "disabled"] as RouterFilter[])
          .filter((value) => value === "all" || counts[value] > 0)
          .map((value) => <button type="button" className={filter === value ? "active" : ""} aria-pressed={filter === value} key={value} onClick={() => onFilterChange(value)}>{value !== "all" && <i data-status={value} />}<span>{value === "all" ? "全部" : STATUS_LABELS[value]}</span><small>{counts[value]}</small></button>)}
      </div>
    </div>
    <div className="router-list-scroll">
      <div className="router-list-section-label"><span>模型路由器</span><i /></div>
      {matches.map((router) => <div className={`router-list-row ${router.id === selectedId ? "active" : ""}`} key={router.id}>
        <button type="button" className="router-list-main" onClick={() => onSelect(router.id)}>
          <span className="router-list-icon"><GitBranch size={15} /><i data-status={router.status} /></span>
          <span className="router-list-copy"><b>{router.name}</b><small><span>{STATUS_LABELS[router.status]}</span>{router.version > 0 && <> · v{router.version}</>}{router.status === "published" && router.has_unpublished_changes && <> · <span>未发布</span></>} · <span data-no-localize>{relativeUpdatedAt(router.updated_at)}</span></small></span>
        </button>
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button type="button" variant="ghost" size="icon-sm" className="router-list-menu" aria-label={`${router.name} 操作`} title="更多操作" disabled={busy} />}><MoreHorizontal size={15} /></DropdownMenuTrigger>
          <DropdownMenuContent className="router-list-menu-content" align="end" side="right">
            <DropdownMenuItem onClick={() => onRename(router)}><Pencil size={14} />编辑基础信息</DropdownMenuItem>
            <DropdownMenuItem disabled={dirty && router.id === selectedId} onClick={() => onDuplicate(router)}><Copy size={14} />复制</DropdownMenuItem>
            {canManage && router.status === "published" && <DropdownMenuItem onClick={() => onAccess(router)}><Users size={14} />人员访问</DropdownMenuItem>}
            {router.status === "published" && <DropdownMenuItem disabled={dirty && router.id === selectedId} onClick={() => onDisable(router)}><PowerOff size={14} />停用</DropdownMenuItem>}
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onClick={() => onDelete(router)}><Trash2 size={14} />删除</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>)}
      {!matches.length && <div className="router-list-empty"><Search size={22} /><span>没有匹配的路由器</span></div>}
    </div>
    <footer>{`共 ${matches.length} 个路由器`}</footer>
  </aside>
}

function RouterEditor({
  router,
  value,
  models,
  displayModels,
  mode,
  validation,
  selectedNodeId,
  dirty,
  busy,
  onModeChange,
  onSelectedNodeChange,
  onChange,
  onSave,
  onValidate,
  onPublish,
}: {
  router: ModelRouter
  value: ModelRouterWrite
  models: ManagedModel[]
  displayModels: ModelRouterModelReference[]
  mode: RouterMode
  validation: ModelRouterValidation | null
  selectedNodeId: string | null
  dirty: boolean
  busy: boolean
  onModeChange: (mode: RouterMode) => void
  onSelectedNodeChange: (nodeId: string | null) => void
  onChange: (value: ModelRouterWrite) => void
  onSave: () => void
  onValidate: () => void
  onPublish: () => void
}) {
  const effectiveMode: RouterMode = ROUTER_CONFIG_MODE_ENABLED ? mode : "canvas"
  // Mirrors the APIM gateway check that rejects any node with Fallback triggers.
  const nativeFallback = value.definition.nodes.some((node) => node.fallback_on.length > 0)
  return <section className="router-editor-pane">
    <header className="router-editor-head">
      <div className="router-editor-identity"><span><GitBranch size={18} /></span><div><div><h2>{value.name || "未命名路由器"}</h2><i data-status={router.status}>{STATUS_LABELS[router.status]}</i>{router.version > 0 && <em>v{router.version}</em>}{dirty && <strong>未保存</strong>}{!dirty && router.status === "published" && router.has_unpublished_changes && <strong>未发布</strong>}</div></div></div>
      <div className="router-editor-actions">
        <Button className="router-action-secondary" type="button" variant="outline" disabled={busy || !dirty} onClick={onSave}>{busy ? <RefreshCw className="spin" size={14} /> : <Save size={14} />}<span>保存草稿</span></Button>
        <Button className="router-action-secondary" type="button" variant="outline" disabled={busy} onClick={onValidate}><CheckCircle2 size={14} />校验</Button>
        <Button className="router-action-primary" type="button" disabled={busy || validation?.valid === false} onClick={onPublish}><Send size={14} />发布</Button>
      </div>
    </header>
    {ROUTER_CONFIG_MODE_ENABLED && <div className="router-editor-view-row">
      <ButtonGroup className="usage-metric-segment router-mode-switch" aria-label="路由器编辑模式">
        <button type="button" className={mode === "canvas" ? "active" : ""} aria-pressed={mode === "canvas"} onClick={() => onModeChange("canvas")}><Blocks size={14} />画布</button>
        <button type="button" className={mode === "config" ? "active" : ""} aria-pressed={mode === "config"} onClick={() => onModeChange("config")}><FileSliders size={14} />常规配置</button>
      </ButtonGroup>
    </div>}
    {validation && <div className={`router-validation-banner ${validation.valid ? "valid" : "invalid"}`}>
      <div className="router-validation-banner-head">
        {validation.valid ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
        <span>{validation.valid ? "配置校验通过，可以发布。" : `发现 ${validation.issues.length} 个配置问题。`}</span>
        {!validation.valid && ROUTER_CONFIG_MODE_ENABLED && <button type="button" onClick={() => onModeChange("config")}>查看问题</button>}
      </div>
      {!validation.valid && validation.issues.length > 0 && <div className="router-validation-banner-issues">
        {validation.issues.map((issue, index) => {
          const targetNodeId = issue.node_id ?? (
            issue.code === "candidate_unavailable"
              ? value.definition.nodes.find((node) =>
                node.candidates.some((cand) => !models.some((m) => m.id === cand.model_id))
                || Boolean(node.routing_model_id && !models.some((m) => m.id === node.routing_model_id)),
              )?.id ?? null
              : null
          )
          return <button
            type="button"
            className="router-validation-banner-issue"
            key={`${issue.code}-${targetNodeId ?? index}`}
            disabled={!targetNodeId}
            onClick={() => { if (targetNodeId) onSelectedNodeChange(targetNodeId) }}
          >
            <span>{formatRouterValidationMessage(issue.message)}</span>
            {targetNodeId && <small>定位节点</small>}
          </button>
        })}
      </div>}
      {nativeFallback && <div className="router-validation-banner-warning">{ROUTER_NATIVE_FALLBACK_NOTICE}</div>}
    </div>}
    {effectiveMode === "canvas" ? <RouterCanvas key={router.id} definition={value.definition} models={models} displayModels={displayModels} routerName={value.name} routerDescription={value.description} selectedNodeId={selectedNodeId} onSelectedNodeChange={onSelectedNodeChange} onChange={(definition) => onChange({ ...value, definition })} onMetadataChange={(name, description) => onChange({ ...value, name, description })} /> : <RouterConfigForm value={value} models={models} displayModels={displayModels} validation={validation} onChange={onChange} onIssueSelect={(nodeId) => { onSelectedNodeChange(nodeId); onModeChange("canvas") }} />}
  </section>
}

export function ModelRouterPage({
  routerDrawerOpen,
  onRouterDrawerOpenChange,
}: {
  routerDrawerOpen: boolean
  onRouterDrawerOpenChange: (open: boolean) => void
}) {
  const client = useQueryClient()
  const { user } = useAuth()
  const isMobile = useIsMobile()
  const registry = useQuery(finopsQueries.registry())
  const routers = useQuery(finopsQueries.modelRouters())
  const catalog = useQuery(finopsQueries.modelRouterCatalog())
  const [selectedId, setSelectedId] = useState<string | null>(routerIdFromUrl)
  const [draft, setDraft] = useState<ModelRouterWrite | null>(null)
  const [savedDraftSignature, setSavedDraftSignature] = useState<string | null>(null)
  const [mode, setMode] = useState<RouterMode>("canvas")
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [validation, setValidation] = useState<ModelRouterValidation | null>(null)
  const [search, setSearch] = useState("")
  const [filter, setFilter] = useState<RouterFilter>("all")
  const {
    width: paneWidth,
    minWidth: paneMinWidth,
    maxWidth: paneMaxWidth,
    startResize: startPaneResize,
    resizeWithKeyboard: resizePaneWithKeyboard,
    resetWidth: resetPaneWidth,
  } = useResizablePane({
    storageKey: ROUTER_PANE_WIDTH_KEY,
    defaultWidth: ROUTER_PANE_WIDTH_DEFAULT,
    minWidth: ROUTER_PANE_WIDTH_MIN,
    maxWidth: ROUTER_PANE_WIDTH_MAX,
  })
  const [createOpen, setCreateOpen] = useState(false)
  const [mobileActionsTarget, setMobileActionsTarget] = useState<HTMLElement | null>(null)
  const [nameAction, setNameAction] = useState<{ mode: "rename" | "duplicate"; router: ModelRouter } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ModelRouter | null>(null)
  const [disableTarget, setDisableTarget] = useState<ModelRouter | null>(null)
  const [accessTarget, setAccessTarget] = useState<ModelRouter | null>(null)
  const [pendingSelection, setPendingSelection] = useState<string | null>(null)

  const routerItems = routers.data?.items ?? []
  const selectedRouter = routerItems.find((router) => router.id === selectedId) ?? null
  const allowedIds = useMemo(() => new Set(catalog.data?.model_ids ?? []), [catalog.data?.model_ids])
  const availableModels = useMemo(
    () => (registry.data?.models ?? []).filter((model) => model.enabled && (allowedIds.has(model.id) || model.capabilities.includes("decision"))),
    [allowedIds, registry.data?.models],
  )
  const displayModels = useMemo(
    () => resolveRouterDisplayModels(registry.data?.models ?? [], routers.data?.model_references),
    [registry.data?.models, routers.data?.model_references],
  )
  const dirty = Boolean(draft && savedDraftSignature && serializedWrite(draft) !== savedDraftSignature)

  useEffect(() => {
    const syncRoute = () => setSelectedId(routerIdFromUrl())
    window.addEventListener("popstate", syncRoute)
    return () => window.removeEventListener("popstate", syncRoute)
  }, [])

  useEffect(() => {
    onRouterDrawerOpenChange(false)
  }, [isMobile, onRouterDrawerOpenChange])

  useEffect(() => {
    setMobileActionsTarget(document.getElementById("mobile-topbar-end-actions"))
  }, [])

  useEffect(() => {
    if (!routerItems.length) {
      if (selectedId) {
        setSelectedId(null)
        writeRouterUrl(null)
      }
      return
    }
    if (selectedId && routerItems.some((router) => router.id === selectedId)) return
    setSelectedId(routerItems[0].id)
    writeRouterUrl(routerItems[0].id)
  }, [routerItems, selectedId])

  useEffect(() => {
    if (!selectedRouter) {
      setDraft(null)
      setSavedDraftSignature(null)
      return
    }
    const nextDraft = writeFromRouter(selectedRouter)
    setDraft(nextDraft)
    setSavedDraftSignature(serializedWrite(nextDraft))
    setValidation(null)
    setSelectedNodeId(null)
  }, [
    selectedRouter?.id,
    selectedRouter?.updated_at,
  ])

  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener("beforeunload", warn)
    return () => window.removeEventListener("beforeunload", warn)
  }, [dirty])

  const setRouter = (router: ModelRouter) => {
    client.setQueryData<ModelRouterList>(finopsKeys.modelRouters, (current) => ({
      ...current,
      items: [router, ...(current?.items ?? []).filter((item) => item.id !== router.id)]
        .sort((left, right) => right.updated_at.localeCompare(left.updated_at)),
    }))
    client.setQueryData(finopsKeys.modelRouter(router.id), router)
  }

  const create = useMutation({
    mutationFn: (value: ModelRouterWrite) => dataSource.saveModelRouter(value),
    onSuccess: (router) => {
      setRouter(router)
      setSelectedId(router.id)
      writeRouterUrl(router.id, true)
      setCreateOpen(false)
    },
  })
  const save = useMutation({
    mutationFn: ({ id, value }: { id: string; value: ModelRouterWrite }) => dataSource.saveModelRouter(value, id),
    onSuccess: setRouter,
  })
  const duplicate = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => dataSource.duplicateModelRouter(id, name),
    onSuccess: (router) => {
      setRouter(router)
      setSelectedId(router.id)
      writeRouterUrl(router.id, true)
      setNameAction(null)
    },
  })
  const validate = useMutation({ mutationFn: dataSource.validateModelRouter, onSuccess: setValidation })
  const refreshAssignedRouters = () => client.invalidateQueries({ queryKey: ["model-platform", "assigned-model-routers"] })
  const publish = useMutation({ mutationFn: dataSource.publishModelRouter, onSuccess: (router) => { setRouter(router); setValidation({ valid: true, issues: [] }); void refreshAssignedRouters() } })
  const disable = useMutation({ mutationFn: dataSource.disableModelRouter, onSuccess: (router) => { setRouter(router); setDisableTarget(null); void refreshAssignedRouters() } })
  const remove = useMutation({
    mutationFn: dataSource.deleteModelRouter,
    onSuccess: (_result, id) => {
      const remaining = routerItems.filter((router) => router.id !== id)
      client.setQueryData<ModelRouterList>(finopsKeys.modelRouters, current => ({ ...current, items: remaining }))
      const nextId = remaining[0]?.id ?? null
      setSelectedId(nextId)
      writeRouterUrl(nextId)
      setDeleteTarget(null)
      void refreshAssignedRouters()
    },
  })
  const assignment = useMutation({
    mutationFn: ({
      routerId,
      write,
    }: {
      routerId: string
      write: ModelRouterBulkAssignmentWrite
    }) => dataSource.saveModelRouterAssignments(routerId, write),
    onSuccess: (_result, { routerId }) => {
      void client.invalidateQueries({
        queryKey: ["model-platform", "model-router-people", routerId],
      })
      void client.invalidateQueries({
        queryKey: ["model-platform", "model-router-assignees", routerId],
      })
      void refreshAssignedRouters()
      setAccessTarget(null)
    },
  })
  const busy = create.isPending || save.isPending || duplicate.isPending || validate.isPending || publish.isPending || disable.isPending || remove.isPending
  const actionError = create.error ?? save.error ?? duplicate.error ?? validate.error ?? publish.error ?? disable.error ?? remove.error

  const chooseRouter = (routerId: string) => {
    onRouterDrawerOpenChange(false)
    if (routerId === selectedId) return
    if (dirty) {
      setPendingSelection(routerId)
      return
    }
    setSelectedId(routerId)
    writeRouterUrl(routerId, true)
  }
  const persistDraft = async () => {
    if (!selectedRouter || !draft) return selectedRouter
    if (!dirty) return selectedRouter
    return save.mutateAsync({ id: selectedRouter.id, value: draft })
  }
  const runValidation = async () => {
    const saved = await persistDraft()
    if (!saved) return null
    return validate.mutateAsync(saved.id)
  }
  const runPublish = async () => {
    const result = await runValidation()
    if (!result?.valid || !selectedRouter) return
    await publish.mutateAsync(selectedRouter.id)
  }
  const routerList = <RouterListPane className={isMobile ? "apim-native-route-drawer-list" : undefined} routers={routerItems} selectedId={selectedId} search={search} filter={filter} canManage={user?.role === "owner"} busy={busy} dirty={dirty} onSearchChange={setSearch} onFilterChange={setFilter} onSelect={chooseRouter} onRename={(router) => setNameAction({ mode: "rename", router })} onDuplicate={(router) => setNameAction({ mode: "duplicate", router })} onAccess={(router) => { assignment.reset(); setAccessTarget(router) }} onDisable={setDisableTarget} onDelete={setDeleteTarget} />

  if (registry.isLoading || routers.isLoading || catalog.isLoading) {
    return <div className="registry-loading"><RefreshCw className="spin" />加载模型路由器</div>
  }
  if (registry.error || !registry.data) {
    return <div className="registry-error">无法加载模型管理 API：{String(registry.error)}</div>
  }
  if (routers.error || catalog.error) {
    return <div className="smh-workspace model-router-workspace"><header className="smh-page-header"><div><span className="smh-header-icon"><Workflow size={17} /></span><h1>模型智能路由</h1></div></header><div className="smh-empty"><AlertCircle size={28} /><b>无法加载模型路由服务</b><span>{String(routers.error ?? catalog.error)}</span></div></div>
  }

  return <div className="smh-workspace model-router-workspace">
    {isMobile && mobileActionsTarget && createPortal(<Button type="button" variant="ghost" size="icon-sm" className="settings-mobile-topbar-end-action" aria-label="新建路由器" title="新建路由器" onClick={() => setCreateOpen(true)}><Plus size={16} /></Button>, mobileActionsTarget)}
    <header className="smh-page-header">
      <div><span className="smh-header-icon"><Workflow size={17} /></span><h1>模型智能路由</h1><span>{routerItems.length}</span></div>
      <div className="smh-header-actions"><Button aria-label="新建路由器" onClick={() => setCreateOpen(true)}><Plus size={14} />新建路由器</Button></div>
    </header>
    {actionError && <div className="smh-notice registry-error"><AlertCircle size={15} /><div className="smh-notice-copy"><b>操作失败</b><span>{String(actionError)}</span></div></div>}
    <div className="router-split" style={{ "--router-pane-width": `${paneWidth}px` } as CSSProperties}>
      {!isMobile && routerList}
      {!isMobile && <button
        className="runtime-pane-handle"
        type="button"
        role="separator"
        aria-label="调整模型路由器列表宽度"
        aria-orientation="vertical"
        aria-valuemin={paneMinWidth}
        aria-valuemax={paneMaxWidth}
        aria-valuenow={paneWidth}
        title="拖动调整宽度，双击复位"
        onPointerDown={startPaneResize}
        onKeyDown={resizePaneWithKeyboard}
        onDoubleClick={resetPaneWidth}
      />}
      {selectedRouter && draft ? <RouterEditor router={selectedRouter} value={draft} models={availableModels} displayModels={displayModels} mode={mode} validation={validation} selectedNodeId={selectedNodeId} dirty={dirty} busy={busy} onModeChange={setMode} onSelectedNodeChange={setSelectedNodeId} onChange={(value) => { setDraft(value); setValidation(null) }} onSave={() => void persistDraft()} onValidate={() => void runValidation()} onPublish={() => void runPublish()} /> : <section className="router-editor-pane"><div className="smh-empty router-empty-state"><GitBranch size={32} /><b>创建第一个模型路由器</b><span>从可用模型中选择候选池，并在画布上连接条件、优先级和 Fallback。</span><Button className="router-new-button" onClick={() => setCreateOpen(true)}><Plus size={14} />新建路由器</Button></div></section>}
    </div>

    {isMobile && <Dialog open={routerDrawerOpen} onOpenChange={onRouterDrawerOpenChange}>
      <DialogContent className="apim-native-route-drawer model-router-drawer" finalFocus={false}>
        <DialogHeader className="apim-native-route-drawer-header"><DialogTitle>模型路由器</DialogTitle><DialogDescription className="sr-only">{selectedRouter?.name ?? "模型路由器"}</DialogDescription><DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="apim-native-route-drawer-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose></DialogHeader>
        {routerList}
      </DialogContent>
    </Dialog>}
    <RouterCreateDialog open={createOpen} models={availableModels} busy={create.isPending} error={create.error ? String(create.error) : null} onOpenChange={setCreateOpen} onCreate={(value) => create.mutate(value)} />
    {user?.role === "owner" && accessTarget && <RouterAccessDialog router={accessTarget} busy={assignment.isPending} error={assignment.error ? String(assignment.error) : null} onClose={() => setAccessTarget(null)} onSave={(write) => assignment.mutate({ routerId: accessTarget.id, write })} />}
    {nameAction && <RouterNameDialog mode={nameAction.mode} router={nameAction.router} initialValue={nameAction.router.id === selectedRouter?.id && draft ? draft : writeFromRouter(nameAction.router)} busy={save.isPending || duplicate.isPending} error={(nameAction.mode === "duplicate" ? duplicate.error : save.error) ? String(nameAction.mode === "duplicate" ? duplicate.error : save.error) : null} onClose={() => setNameAction(null)} onSubmit={(name, description) => {
      if (nameAction.mode === "duplicate") duplicate.mutate({ id: nameAction.router.id, name })
      else {
        const current = nameAction.router.id === selectedRouter?.id && draft ? draft : writeFromRouter(nameAction.router)
        save.mutate({ id: nameAction.router.id, value: { ...current, name, description } }, { onSuccess: () => setNameAction(null) })
      }
    }} />}
    <AlertDialog open={Boolean(deleteTarget)} onOpenChange={(open) => { if (!open && !remove.isPending) setDeleteTarget(null) }}>
      <AlertDialogContent className="model-delete-dialog router-confirm-dialog" data-tone="destructive"><div className="model-delete-body"><AlertDialogHeader><AlertDialogTitle>删除模型路由器</AlertDialogTitle><AlertDialogDescription>{deleteTarget ? `将删除“${deleteTarget.name}”及其当前草稿，并从路由器列表中移除。已发布版本记录会保留用于审计。` : ""}</AlertDialogDescription></AlertDialogHeader></div><AlertDialogFooter className="model-delete-footer"><AlertDialogCancel disabled={remove.isPending}>取消</AlertDialogCancel><AlertDialogAction variant="destructive" disabled={remove.isPending} onClick={() => deleteTarget && remove.mutate(deleteTarget.id)}>{remove.isPending ? "正在删除" : "删除路由器"}</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
    </AlertDialog>
    <AlertDialog open={Boolean(disableTarget)} onOpenChange={(open) => { if (!open && !disable.isPending) setDisableTarget(null) }}>
      <AlertDialogContent className="model-delete-dialog router-confirm-dialog" data-tone="neutral"><div className="model-delete-body"><AlertDialogHeader><AlertDialogTitle>停用模型路由器</AlertDialogTitle><AlertDialogDescription>{disableTarget ? `停用“${disableTarget.name}”后，其已发布版本将不再作为可用路由。草稿与版本记录会保留。` : ""}</AlertDialogDescription></AlertDialogHeader></div><AlertDialogFooter className="model-delete-footer"><AlertDialogCancel disabled={disable.isPending}>取消</AlertDialogCancel><AlertDialogAction disabled={disable.isPending} onClick={() => disableTarget && disable.mutate(disableTarget.id)}>停用</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
    </AlertDialog>
    <AlertDialog open={Boolean(pendingSelection)} onOpenChange={(open) => { if (!open) setPendingSelection(null) }}>
      <AlertDialogContent className="model-delete-dialog router-confirm-dialog" data-tone="destructive"><div className="model-delete-body"><AlertDialogHeader><AlertDialogTitle>放弃未保存的修改？</AlertDialogTitle><AlertDialogDescription>当前画布有尚未保存的更改。切换路由器将丢失这些更改。</AlertDialogDescription></AlertDialogHeader></div><AlertDialogFooter className="model-delete-footer"><AlertDialogCancel>继续编辑</AlertDialogCancel><AlertDialogAction variant="destructive" onClick={() => { if (!pendingSelection) return; setSelectedId(pendingSelection); writeRouterUrl(pendingSelection, true); setPendingSelection(null) }}>放弃并切换</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
    </AlertDialog>
  </div>
}