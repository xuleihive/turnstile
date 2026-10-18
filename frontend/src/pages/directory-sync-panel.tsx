import { useEffect, useMemo, useState, type FormEvent } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Check, ChevronLeft, ChevronRight, Link2, Pencil, Plus, RefreshCw, Save, ShieldCheck, Trash2, X } from "lucide-react"
import {
  directoryApi, type DirectoryCapabilities, type DirectoryConnection, type DirectoryGroupMapping,
  type DirectoryOrganization, type DirectorySyncChange, type DirectorySyncJob, type DirectoryUnit,
} from "../data-sources/apim/api/organization-management"
import { confirmDirectoryLeave, DirectorySelect, useDirectoryLeaveGuard } from "../components/directory-controls"
import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { Checkbox } from "../components/ui/checkbox"
import { Field, FieldGroup, FieldLabel } from "../components/ui/field"
import { Alert, AlertDescription } from "../components/ui/alert"
import { Badge } from "../components/ui/badge"
import { Empty, EmptyTitle } from "../components/ui/empty"
import { ResizableTable } from "../components/ui/resizable-table"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "../components/ui/dialog"

const jobNames: Record<DirectorySyncJob["status"], string> = {
  queued: "已排队", running: "正在读取", awaiting_review: "待审核", applying: "正在应用",
  succeeded: "已完成", failed: "失败", cancelled: "已取消", conflicted: "版本冲突",
}
const decisionNames: Record<string, string> = {
  create: "新增", update: "更新", conflict: "冲突", ignored: "忽略", missing: "来源缺失",
}
const conflictNames: Record<string, string> = {
  explicit_person_binding_required: "需要明确关联人员",
  multiple_primary_departments: "存在多个主部门",
  governance_identity_missing: "缺少有效人员标识",
  department_change_requires_transfer: "需要先完成计划调岗",
  identity_owned_by_another_connection: "身份已属于其他连接",
  incomplete_profile: "来源资料不完整",
  inactive_mapping: "映射单位已停用",
}

function connectionValue(connection: DirectoryConnection) {
  return {
    organization_id: connection.organization_id, name: connection.name, cloud: connection.cloud,
    tenant_id: connection.tenant_id, client_id: connection.client_id,
    authentication_mode: connection.authentication_mode, scope: connection.scope,
    default_department_id: connection.default_department_id, include_guests: connection.include_guests,
    enabled: connection.enabled, sync_interval_minutes: connection.sync_interval_minutes,
    expected_revision: connection.revision,
  }
}

function ConnectionEditor({ organization, units, connection, capabilities, onClose, onSaved }: {
  organization: DirectoryOrganization; units: DirectoryUnit[]; connection?: DirectoryConnection
  capabilities: DirectoryCapabilities; onClose: () => void; onSaved: () => Promise<void>
}) {
  const [name, setName] = useState(connection?.name ?? "")
  const [cloud, setCloud] = useState(connection?.cloud ?? capabilities.deployment_cloud ?? "public")
  const [tenant, setTenant] = useState(connection?.tenant_id ?? capabilities.managed_identity_tenant_id ?? "")
  const [mode, setMode] = useState(connection?.authentication_mode ?? "managed_identity")
  const [clientId, setClientId] = useState(connection?.client_id ?? "")
  const [credential, setCredential] = useState("")
  const [scope, setScope] = useState(connection?.scope ?? "selected_groups")
  const [department, setDepartment] = useState(connection?.default_department_id ?? "")
  const [guests, setGuests] = useState(connection?.include_guests ?? false)
  const [interval, setInterval] = useState(connection?.sync_interval_minutes ?? 60)
  const [dirty, setDirty] = useState(false)
  useDirectoryLeaveGuard(dirty)
  const key = useMemo(() => crypto.randomUUID(), [])
  const save = useMutation({
    mutationFn: () => {
      const value = {
        organization_id: organization.id, name, cloud, tenant_id: tenant,
        authentication_mode: mode, client_id: mode === "key_vault_secret" ? clientId : null,
        ...(mode === "managed_identity" ? { credential_ref: null } : credential ? { credential_ref: credential } : {}),
        scope, default_department_id: scope === "all_users" ? department : null,
        include_guests: guests, enabled: false, sync_interval_minutes: interval,
        expected_revision: connection?.revision,
      }
      return connection ? directoryApi.updateConnection(connection.id, value) : directoryApi.createConnection(value, key)
    },
    onSuccess: async () => { setDirty(false); await onSaved(); onClose() },
  })
  const close = () => {
    if (!save.isPending && (!dirty || window.confirm("放弃未保存的更改？"))) onClose()
  }
  const submit = (event: FormEvent) => { event.preventDefault(); save.mutate() }
  return <Dialog open onOpenChange={(open) => { if (!open) close() }}>
    <DialogContent className="directory-dialog">
      <DialogHeader><DialogTitle>{connection ? "编辑目录连接" : "新增目录连接"}</DialogTitle>
        <DialogDescription data-no-localize>{organization.name}</DialogDescription></DialogHeader>
      <form onSubmit={submit} onChange={() => setDirty(true)}>
        <FieldGroup>
          <Field><FieldLabel htmlFor="sync-name">名称</FieldLabel><Input id="sync-name" autoFocus required maxLength={160}
            value={name} onChange={(event) => setName(event.target.value)} /></Field>
          <Field><FieldLabel>云环境</FieldLabel><DirectorySelect label="云环境" value={cloud}
            items={[{ value: "public", label: "Azure" }, { value: "usgov", label: "Azure Government" }, { value: "china", label: "Azure China" }]}
            onChange={(value) => { setCloud(value as typeof cloud); setDirty(true) }} /></Field>
          <Field><FieldLabel htmlFor="sync-tenant">Tenant ID</FieldLabel><Input id="sync-tenant" required value={tenant}
            maxLength={36} onChange={(event) => setTenant(event.target.value)} /></Field>
          <Field><FieldLabel>认证方式</FieldLabel><DirectorySelect label="认证方式" value={mode}
            items={[{ value: "managed_identity", label: "托管身份" }, { value: "key_vault_secret", label: "Key Vault" }]}
            onChange={(value) => { setMode(value as typeof mode); setDirty(true) }} /></Field>
          {mode === "key_vault_secret" && <>
            <Field><FieldLabel htmlFor="sync-client">Client ID</FieldLabel><Input id="sync-client" required maxLength={36}
              value={clientId} onChange={(event) => setClientId(event.target.value)} /></Field>
            <Field><FieldLabel htmlFor="sync-credential">Key Vault Secret URI</FieldLabel><Input id="sync-credential" type="url"
              required={!connection?.credential_configured} maxLength={512} value={credential}
              onChange={(event) => setCredential(event.target.value)} /></Field>
          </>}
          <Field><FieldLabel>同步范围</FieldLabel><DirectorySelect label="同步范围" value={scope}
            items={[{ value: "selected_groups", label: "指定 Groups" }, { value: "all_users", label: "全部人员" }]}
            onChange={(value) => { setScope(value as typeof scope); setDirty(true) }} /></Field>
          {scope === "all_users" && <Field><FieldLabel>默认部门</FieldLabel><DirectorySelect label="默认部门" value={department}
            items={units.filter((unit) => unit.kind === "department" && unit.status === "active").map((unit) => ({ value: unit.id, label: unit.name }))}
            onChange={(value) => { setDepartment(value); setDirty(true) }} /></Field>}
          <Field><FieldLabel htmlFor="sync-interval">同步间隔（分钟）</FieldLabel><Input id="sync-interval" type="number"
            min={15} max={1440} required value={interval} onChange={(event) => setInterval(Number(event.target.value))} /></Field>
          <label className="directory-checkbox-line"><Checkbox checked={guests}
            onCheckedChange={(value) => { setGuests(value); setDirty(true) }} />包含 Guest</label>
        </FieldGroup>
        {save.error && <Alert><AlertDescription>{save.error.message}</AlertDescription></Alert>}
        <DialogFooter><Button type="button" variant="outline" disabled={save.isPending} onClick={close}>取消</Button>
          <Button type="submit" disabled={save.isPending || (scope === "all_users" && !department)}>
            <Save data-icon="inline-start" />保存</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function MappingEditor({ connection, mappings, units, onSaved }: {
  connection: DirectoryConnection; mappings: DirectoryGroupMapping[]; units: DirectoryUnit[]
  onSaved: () => Promise<void>
}) {
  const [items, setItems] = useState(mappings)
  const [dirty, setDirty] = useState(false)
  useDirectoryLeaveGuard(dirty)
  const save = useMutation({
    mutationFn: () => directoryApi.setMappings(connection.id, items, connection.revision),
    onSuccess: async () => { setDirty(false); await onSaved() },
  })
  const update = (index: number, patch: Partial<DirectoryGroupMapping>) => {
    setItems((values) => values.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item))
    setDirty(true)
  }
  return <section className="directory-sync-section">
    <div className="directory-context-heading"><h3>Group 映射</h3><Button variant="outline" size="sm" onClick={() => {
      setItems((values) => [...values, { group_id: "", unit_id: "", membership_mode: "direct", enabled: true }]); setDirty(true)
    }}><Plus data-icon="inline-start" />新增映射</Button></div>
    <FieldGroup>{items.map((item, index) => <div key={index} className="directory-mapping-row">
      <Field><FieldLabel htmlFor={`sync-group-${index}`}>Group ID</FieldLabel>
        <Input id={`sync-group-${index}`} value={item.group_id} maxLength={36}
          onChange={(event) => update(index, { group_id: event.target.value })} /></Field>
      <Field><FieldLabel>目标部门或团队</FieldLabel><DirectorySelect label="目标部门或团队" value={item.unit_id}
        items={units.filter((unit) => unit.status === "active").map((unit) => ({
          value: unit.id, label: `${unit.kind === "department" ? "部门" : "团队"} · ${unit.name}`,
        }))} onChange={(unit_id) => update(index, { unit_id })} /></Field>
      <Field><FieldLabel>成员范围</FieldLabel><DirectorySelect label="成员范围" value={item.membership_mode}
        items={[{ value: "direct", label: "直接成员" }, { value: "transitive", label: "递归成员" }]}
        onChange={(membership_mode) => update(index, { membership_mode: membership_mode as "direct" | "transitive" })} /></Field>
      <label className="directory-checkbox-line"><Checkbox checked={item.enabled}
        onCheckedChange={(enabled) => update(index, { enabled })} />启用</label>
      <Button variant="ghost" size="icon-sm" aria-label="移除映射" title="移除映射" onClick={() => {
        setItems((values) => values.filter((_, itemIndex) => itemIndex !== index)); setDirty(true)
      }}><Trash2 /></Button>
    </div>)}</FieldGroup>
    {!items.length && <Empty><EmptyTitle>暂无映射</EmptyTitle></Empty>}
    {save.error && <Alert><AlertDescription>{save.error.message}</AlertDescription></Alert>}
    <Button disabled={!dirty || save.isPending || items.some((item) => !item.group_id || !item.unit_id)}
      onClick={() => save.mutate()}><Save data-icon="inline-start" />保存映射</Button>
  </section>
}

function BindingDialog({ connection, job, change, onSaved, onClose }: {
  connection: DirectoryConnection; job: DirectorySyncJob; change: DirectorySyncChange
  onSaved: () => Promise<void>; onClose: () => void
}) {
  const [query, setQuery] = useState(change.payload.profile?.mail ?? "")
  const [personId, setPersonId] = useState("")
  const [login, setLogin] = useState(false)
  const people = useQuery({
    queryKey: ["directory", "binding-candidates", connection.organization_id, query],
    queryFn: () => directoryApi.people({ organization_id: connection.organization_id, query }),
  })
  const person = people.data?.items.find((item) => item.id === personId)
  const save = useMutation({
    mutationFn: () => directoryApi.bindExternalPerson(connection.id, change.object_id, {
      person_id: personId, job_id: job.id, expected_revision: person?.revision, bind_login_identity: login,
    }),
    onSuccess: async () => { await onSaved(); onClose() },
  })
  return <Dialog open onOpenChange={(open) => { if (!open && !save.isPending) onClose() }}>
    <DialogContent className="directory-dialog"><DialogHeader><DialogTitle>关联来源人员</DialogTitle>
      <DialogDescription data-no-localize>{change.payload.profile?.displayName ?? change.object_id}</DialogDescription></DialogHeader>
      <FieldGroup>
        <Field><FieldLabel htmlFor="sync-person-search">搜索人员</FieldLabel><Input id="sync-person-search" value={query}
          maxLength={200} onChange={(event) => { setQuery(event.target.value); setPersonId(""); setLogin(false) }} /></Field>
        <Field><FieldLabel>本地人员</FieldLabel><DirectorySelect label="本地人员" value={personId}
          items={(people.data?.items ?? []).map((item) => ({ value: item.id, label: `${item.display_name} · ${item.governance_user_id}` }))}
          onChange={(id) => { setPersonId(id); setLogin(false) }} /></Field>
        <label className="directory-checkbox-line"><Checkbox checked={login} disabled={!person?.app_user_id}
          onCheckedChange={setLogin} />同时授权此 Entra 身份登录已关联账号</label>
      </FieldGroup>
      {(save.error || people.error) && <Alert><AlertDescription>{(save.error || people.error)?.message}</AlertDescription></Alert>}
      <DialogFooter><Button variant="outline" disabled={save.isPending} onClick={onClose}>取消</Button>
        <Button disabled={!person || save.isPending} onClick={() => save.mutate()}><Link2 data-icon="inline-start" />确认关联</Button></DialogFooter>
    </DialogContent>
  </Dialog>
}

function SyncReview({ connection, job, capabilities, onSaved }: {
  connection: DirectoryConnection; job: DirectorySyncJob; capabilities: DirectoryCapabilities
  onSaved: () => Promise<void>
}) {
  const [offset, setOffset] = useState(0)
  const [approveMissing, setApproveMissing] = useState(false)
  const [approveMass, setApproveMass] = useState(false)
  const [binding, setBinding] = useState<DirectorySyncChange | null>(null)
  const changes = useQuery({
    queryKey: ["directory", "sync-changes", job.id, job.status, offset],
    queryFn: () => directoryApi.syncChanges(job.id, offset),
  })
  const mutate = useMutation({
    mutationFn: (action: "apply" | "cancel") => action === "apply"
      ? directoryApi.applySync(job.id, capabilities.directory_version, approveMissing, approveMass)
      : directoryApi.cancelSync(job.id),
    onSuccess: onSaved,
  })
  const canApply = job.status === "awaiting_review" && !job.summary.conflicts
    && (!job.summary.missing || approveMissing) && (!job.summary.mass_change || approveMass)
  return <section className="directory-sync-section">
    <div className="directory-context-heading"><h3>同步差异</h3><Badge>{jobNames[job.status]}</Badge></div>
    <div className="directory-sync-summary">
      {(["create", "update", "conflicts", "ignored", "missing"] as const).map((key) =>
        <span key={key}>{decisionNames[key === "conflicts" ? "conflict" : key]} <strong>{job.summary[key] ?? 0}</strong></span>)}
    </div>
    {job.error_code && <Alert><AlertDescription>{job.error_code}</AlertDescription></Alert>}
    {(changes.error || mutate.error) && <Alert><AlertDescription>{(changes.error || mutate.error)?.message}</AlertDescription></Alert>}
    <div className="directory-table-scroll"><ResizableTable className="directory-table directory-sync-table">
      <thead><tr><th><span>人员</span></th><th><span>变更</span></th><th><span>目标部门</span></th><th><span>结果</span></th></tr></thead>
      <tbody>{(changes.data?.items ?? []).map((change) => <tr key={change.object_id}>
        <td data-no-localize>{change.payload.profile?.displayName ?? change.payload.person_id ?? change.object_id}
          <small>{change.payload.profile?.mail ?? change.payload.profile?.userPrincipalName ?? change.object_id}</small></td>
        <td>{decisionNames[change.decision] ?? change.decision}</td>
        <td data-no-localize>{change.payload.department_id ?? "--"}</td>
        <td>{change.conflict_code ? conflictNames[change.conflict_code] ?? change.conflict_code : "--"}
          {change.conflict_code === "explicit_person_binding_required" && job.status === "awaiting_review" &&
            <Button size="sm" variant="outline" onClick={() => setBinding(change)}><Link2 data-icon="inline-start" />关联人员</Button>}</td>
      </tr>)}</tbody>
    </ResizableTable></div>
    {changes.data?.total === 0 && <Empty><EmptyTitle>暂无差异</EmptyTitle></Empty>}
    <div className="directory-pagination">
      <Button size="icon-sm" variant="outline" aria-label="上一页" title="上一页" disabled={!offset}
        onClick={() => setOffset((value) => Math.max(0, value - 50))}><ChevronLeft /></Button>
      <Button size="icon-sm" variant="outline" aria-label="下一页" title="下一页" disabled={offset + 50 >= (changes.data?.total ?? 0)}
        onClick={() => setOffset((value) => value + 50)}><ChevronRight /></Button>
    </div>
    {job.status === "awaiting_review" && <FieldGroup>
      {Boolean(job.summary.missing) && <label className="directory-checkbox-line">
        <Checkbox checked={approveMissing} onCheckedChange={setApproveMissing} />批准来源缺失人员停用</label>}
      {job.summary.mass_change && <label className="directory-checkbox-line">
        <Checkbox checked={approveMass} onCheckedChange={setApproveMass} />批准超过 20% 的来源成员减少</label>}
    </FieldGroup>}
    <div className="directory-sync-actions">
      {job.status === "awaiting_review" && <Button disabled={!canApply || mutate.isPending} onClick={() => mutate.mutate("apply")}>
        <Check data-icon="inline-start" />应用已审核差异</Button>}
      {["queued", "running", "awaiting_review", "conflicted"].includes(job.status) &&
        <Button variant="outline" disabled={mutate.isPending} onClick={() => mutate.mutate("cancel")}><X data-icon="inline-start" />取消任务</Button>}
    </div>
    {binding && <BindingDialog connection={connection} job={job} change={binding} onSaved={onSaved} onClose={() => setBinding(null)} />}
  </section>
}

export function DirectorySyncPanel({ organization, units, capabilities, onSaved }: {
  organization: DirectoryOrganization; units: DirectoryUnit[]; capabilities: DirectoryCapabilities
  onSaved: () => Promise<void>
}) {
  const [selected, setSelected] = useState("")
  const [jobId, setJobId] = useState("")
  const [editor, setEditor] = useState<DirectoryConnection | "new" | null>(null)
  const [mappingReset, setMappingReset] = useState(0)
  const guardConfiguration = () => {
    if (!confirmDirectoryLeave()) return false
    setMappingReset((value) => value + 1)
    return true
  }
  const connections = useQuery({
    queryKey: ["directory", "connections", capabilities.permission_revision], queryFn: directoryApi.connections,
  })
  const available = (connections.data ?? []).filter((item) => item.organization_id === organization.id)
  const connection = available.find((item) => item.id === selected) ?? available[0]
  const mappings = useQuery({
    queryKey: ["directory", "mappings", connection?.id, connection?.revision],
    queryFn: () => directoryApi.mappings(connection!.id), enabled: Boolean(connection),
  })
  const jobs = useQuery({
    queryKey: ["directory", "sync-jobs", connection?.id],
    queryFn: () => directoryApi.syncJobs(connection!.id), enabled: Boolean(connection),
    refetchInterval: (query) => query.state.data?.some((job) => ["queued", "running", "applying"].includes(job.status)) ? 3000 : false,
  })
  const job = jobs.data?.find((item) => item.id === jobId) ?? jobs.data?.[0]
  const run = useMutation({
    mutationFn: async (action: "validate" | "toggle" | "full" | "delta"): Promise<unknown> => {
      if (!connection) throw new Error("请选择目录连接。")
      if (action === "validate") return directoryApi.validateConnection(connection.id)
      if (action === "toggle") return directoryApi.updateConnection(connection.id, {
        ...connectionValue(connection), enabled: !connection.enabled,
      })
      return directoryApi.sync(connection.id, action, crypto.randomUUID())
    },
    onSuccess: async () => { await onSaved(); await jobs.refetch() },
  })
  useEffect(() => { setJobId("") }, [connection?.id])
  const error = connections.error || mappings.error || jobs.error || run.error
  return <div className="directory-sync">
    <div className="directory-toolbar">
      <DirectorySelect label="目录连接" value={connection?.id ?? ""} items={available.map((item) => ({ value: item.id, label: item.name }))}
        onChange={(value) => { if (confirmDirectoryLeave()) setSelected(value) }} />
      <Button variant="outline" size="sm" onClick={() => { if (guardConfiguration()) setEditor("new") }}><Plus data-icon="inline-start" />新增目录连接</Button>
    </div>
    {error && <Alert><AlertDescription>{error.message}</AlertDescription></Alert>}
    {!connection ? <Empty><EmptyTitle>{connections.isPending ? "正在加载" : "暂无目录连接"}</EmptyTitle></Empty> : <>
      <div className="directory-sync-details"><div><strong data-no-localize>{connection.name}</strong>
        <Badge variant="outline">{connection.enabled ? "启用" : "停用"}</Badge>
        <Badge variant="outline">{connection.validated_revision === connection.revision ? "已验证" : "未验证"}</Badge></div>
        <span data-no-localize>{connection.tenant_id}</span>
        <small>{connection.cloud} · {connection.authentication_mode === "managed_identity" ? "托管身份" : "Key Vault"}</small>
      </div>
      <div className="directory-sync-actions">
        <Button variant="outline" size="sm" onClick={() => { if (guardConfiguration()) setEditor(connection) }}><Pencil data-icon="inline-start" />编辑</Button>
        <Button variant="outline" size="sm" disabled={run.isPending} onClick={() => { if (guardConfiguration()) run.mutate("validate") }}><ShieldCheck data-icon="inline-start" />验证连接</Button>
        <Button variant="outline" size="sm" disabled={run.isPending || (!connection.enabled && connection.validated_revision !== connection.revision)}
          onClick={() => { if (guardConfiguration()) run.mutate("toggle") }}><Check data-icon="inline-start" />{connection.enabled ? "停用连接" : "启用连接"}</Button>
        <Button size="sm" disabled={run.isPending || !connection.enabled || !capabilities.sync_available}
          onClick={() => { if (guardConfiguration()) run.mutate("full") }}><RefreshCw data-icon="inline-start" />全量预览</Button>
        <Button variant="outline" size="sm" disabled={run.isPending || !connection.enabled || !capabilities.sync_available}
          onClick={() => { if (guardConfiguration()) run.mutate("delta") }}><RefreshCw data-icon="inline-start" />增量预览</Button>
      </div>
      {connection.scope === "selected_groups" && mappings.data && <MappingEditor
        key={`${connection.id}:${connection.revision}:${mappingReset}`} connection={connection} mappings={mappings.data} units={units} onSaved={onSaved} />}
      <section className="directory-sync-section"><div className="directory-context-heading"><h3>同步任务</h3>
        <Button variant="ghost" size="icon-sm" aria-label="刷新同步任务" title="刷新同步任务" onClick={() => void jobs.refetch()}><RefreshCw /></Button></div>
        {(jobs.data ?? []).map((item) => <button type="button" key={item.id} className="directory-job-row"
          aria-pressed={job?.id === item.id} onClick={() => setJobId(item.id)}>
          <span>{item.mode === "full" ? "全量" : "增量"}</span><Badge>{jobNames[item.status]}</Badge>
          <time data-no-localize>{new Date(item.created_at).toLocaleString()}</time>
        </button>)}
        {jobs.data?.length === 0 && <Empty><EmptyTitle>暂无同步任务</EmptyTitle></Empty>}
      </section>
      {job && <SyncReview key={job.id} connection={connection} job={job} capabilities={capabilities}
        onSaved={async () => { await onSaved(); await jobs.refetch() }} />}
    </>}
    {editor && <ConnectionEditor organization={organization} units={units} capabilities={capabilities}
      connection={editor === "new" ? undefined : editor} onClose={() => setEditor(null)} onSaved={onSaved} />}
  </div>
}
