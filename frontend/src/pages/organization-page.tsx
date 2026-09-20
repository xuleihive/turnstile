import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Building2, Check, Copy, Edit3, Plus, RefreshCw, RotateCcw, ShieldAlert, Users, X } from "lucide-react"

import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { ResizableGridTable } from "../components/ui/resizable-table"
import { dataSource } from "../data-sources/apim/api"
import { finopsKeys, finopsQueries } from "../data-sources/apim/queries"
import type { ConsoleMemberList, OrganizationDirectory, OrgUnit } from "../data-sources/apim/types"
import { FINOPS_NAVIGATE_EVENT } from "../lib/navigation"
import { useAuth } from "../providers/auth-provider"

// Mirrors `suggested_unit_id` on the server: an id is proposed where the name allows one and
// withheld where it does not. A department named in Chinese has no slug to derive, and an
// invented `department-1` would be a permanent identifier that says nothing about what it
// identifies -- so the field is simply left for the administrator to fill.
function suggestedId(displayName: string): string {
  const slug = displayName.trim().toLocaleLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")
  return slug ? `department-${slug}`.slice(0, 63).replace(/-$/, "") : ""
}

function channelsHref(departmentId: string) {
  const url = new URL(window.location.href)
  url.searchParams.set("page", "applications")
  url.searchParams.set("consumer", "applications")
  url.searchParams.set("department", departmentId)
  url.searchParams.delete("application")
  return `${url.pathname}${url.search}`
}

function openChannels(departmentId: string) {
  return (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey) return
    if (event.button !== 0) return
    event.preventDefault()
    window.history.pushState(null, "", channelsHref(departmentId))
    window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT))
  }
}

function useDirectoryMutation(onDone?: () => void) {
  const queryClient = useQueryClient()
  return {
    onSuccess: (value: OrganizationDirectory) => {
      queryClient.setQueryData(finopsKeys.organizationDirectory, value)
      void queryClient.invalidateQueries({ queryKey: finopsKeys.entities })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayApplications })
      onDone?.()
    },
  }
}

function RenameField({ unit, canManage }: { unit: OrgUnit; canManage: boolean }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(unit.display_name)
  const mutation = useMutation({
    mutationFn: () => dataSource.renameOrgUnit(unit.id, draft.trim()),
    ...useDirectoryMutation(() => setEditing(false)),
  })
  if (!editing) {
    return <span className="org-unit-name">
      <b data-no-localize>{unit.display_name}</b>
      {canManage && <Button type="button" variant="ghost" size="icon-sm" aria-label="重命名" title="重命名"
        onClick={() => { setDraft(unit.display_name); setEditing(true) }}><Edit3 size={13} /></Button>}
    </span>
  }
  return <form className="org-unit-rename" onSubmit={(event) => { event.preventDefault(); mutation.mutate() }}>
    <Input value={draft} autoFocus disabled={mutation.isPending} onChange={(event) => setDraft(event.target.value)} />
    <Button type="submit" size="icon-sm" disabled={mutation.isPending || !draft.trim()} aria-label="保存">
      {mutation.isPending ? <RefreshCw className="spin" size={13} /> : <Check size={13} />}
    </Button>
    <Button type="button" variant="ghost" size="icon-sm" disabled={mutation.isPending}
      onClick={() => setEditing(false)} aria-label="取消"><X size={13} /></Button>
  </form>
}

function DepartmentRow({ unit, canManage }: { unit: OrgUnit; canManage: boolean }) {
  const retired = unit.status === "retired"
  const mutation = useMutation({
    mutationFn: () => dataSource.setOrgUnitStatus(unit.id, retired ? "active" : "retired"),
    ...useDirectoryMutation(),
  })
  const { budgets, usage_records: usage, applications } = unit.references
  return <div className="org-row" role="row" data-retired={retired || undefined}>
    <span role="cell"><RenameField unit={unit} canManage={canManage} /></span>
    <span role="cell"><code data-no-localize>{unit.id}</code></span>
    <span role="cell">{retired ? "已停用" : "使用中"}</span>
    <span role="cell" className="org-unit-references">
      <span title="预算">{budgets}</span>
      <span title="用量记录">{usage}</span>
      {/* The count was the only place a department's channels were visible, and it was a
          number with no way through to what it counted. */}
      <a title="查看这个部门下的订阅" href={channelsHref(unit.id)}
        onClick={openChannels(unit.id)}>{applications}</a>
    </span>
    <span role="cell">
      {canManage && <Button type="button" variant="outline" size="sm" disabled={mutation.isPending}
        onClick={() => mutation.mutate()}>
        {mutation.isPending ? <RefreshCw className="spin" size={13} /> : retired ? <RotateCcw size={13} /> : <X size={13} />}
        {retired ? "恢复使用" : "停用"}
      </Button>}
    </span>
  </div>
}

function CreateDepartment() {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const [id, setId] = useState("")
  const [touchedId, setTouchedId] = useState(false)
  const effectiveId = touchedId ? id : suggestedId(name)
  const mutation = useMutation({
    mutationFn: () => dataSource.createDepartment({ id: effectiveId.trim(), display_name: name.trim() }),
    ...useDirectoryMutation(() => { setOpen(false); setName(""); setId(""); setTouchedId(false) }),
  })
  if (!open) {
    return <Button type="button" onClick={() => setOpen(true)}><Plus size={14} />新建部门</Button>
  }
  return <form className="org-create-form" onSubmit={(event) => { event.preventDefault(); mutation.mutate() }}>
    <label><span>部门名称</span>
      <Input value={name} autoFocus disabled={mutation.isPending} onChange={(event) => setName(event.target.value)} />
    </label>
    <label><span>部门标识</span>
      <Input value={effectiveId} disabled={mutation.isPending} placeholder="department-xxx"
        onChange={(event) => { setTouchedId(true); setId(event.target.value) }} />
      <small>标识建好后不能再改，名称可以随时改。</small>
    </label>
    {mutation.error && <div className="registry-error">{String(mutation.error)}</div>}
    <div className="org-create-actions">
      <Button type="button" variant="outline" disabled={mutation.isPending} onClick={() => setOpen(false)}>取消</Button>
      <Button type="submit" disabled={mutation.isPending || !name.trim() || !effectiveId.trim()}>
        {mutation.isPending ? <RefreshCw className="spin" size={14} /> : null}创建
      </Button>
    </div>
  </form>
}

function MembersCard({ canManage }: { canManage: boolean }) {
  const queryClient = useQueryClient()
  const members = useQuery(finopsQueries.consoleMembers())
  const onSuccess = (value: ConsoleMemberList) => {
    queryClient.setQueryData(finopsKeys.consoleMembers, value)
  }
  const role = useMutation({
    mutationFn: (input: { email: string; role: "owner" | "member" }) =>
      dataSource.setConsoleMemberRole(input.email, input.role),
    onSuccess,
  })
  const enabled = useMutation({
    mutationFn: (input: { email: string; enabled: boolean }) =>
      dataSource.setConsoleMemberEnabled(input.email, input.enabled),
    onSuccess,
  })
  const rows = members.data?.members ?? []
  const busy = role.isPending || enabled.isPending
  const error = role.error ?? enabled.error
  return <section className="org-card">
    <header><h2><Users size={15} />控制台账号</h2></header>
    <p className="org-card-note">用 Microsoft 登录的人会在第一次登录时自动出现在这里，角色是成员。成员能看，Owner 能改。</p>
    <ResizableGridTable className="org-table org-member-table" role="table" aria-label="控制台账号"
      headerSelector=".org-table-head" minWidths={[260, 110, 110, 160, 180]} columnGap={12}>
      <div className="org-table-head" role="row">
        {["账号", "登录方式", "角色", "最近登录", ""].map((label, index) =>
          <span className="org-table-heading" role="columnheader" key={label || index}><span>{label}</span></span>)}
      </div>
      <div role="rowgroup">
        {rows.map((member) => <div className="org-row" role="row" key={member.email}
          data-retired={!member.enabled || undefined}>
          <span role="cell"><b data-no-localize>{member.email}</b>{member.is_self && <small> （你自己）</small>}</span>
          <span role="cell">{member.sign_in === "password" ? "密码" : "Microsoft"}</span>
          <span role="cell">{member.role === "owner" ? "Owner" : "成员"}</span>
          <span role="cell" data-no-localize>{member.last_login_at ? member.last_login_at.slice(0, 16).replace("T", " ") : "—"}</span>
          <span role="cell" className="org-member-actions">
            {canManage && <>
              <Button type="button" variant="outline" size="sm" disabled={busy}
                onClick={() => role.mutate({ email: member.email, role: member.role === "owner" ? "member" : "owner" })}>
                {member.role === "owner" ? "降为成员" : "设为 Owner"}
              </Button>
              <Button type="button" variant="ghost" size="sm" disabled={busy}
                onClick={() => enabled.mutate({ email: member.email, enabled: !member.enabled })}>
                {member.enabled ? "停用" : "启用"}
              </Button>
            </>}
          </span>
        </div>)}
      </div>
    </ResizableGridTable>
    {error && <div className="registry-error">{String(error)}</div>}
    <p className="org-card-note">最后一个 Owner 不能被降级或停用，自己也不能撤自己的 Owner —— 这两件事出错只能上机器用命令行救。</p>
  </section>
}

export function OrganizationPage() {
  const { user } = useAuth()
  const canManage = user?.role === "owner"
  const directory = useQuery(finopsQueries.organizationDirectory())
  if (directory.isLoading) {
    return <main className="org-page"><div className="org-loading"><RefreshCw className="spin" size={18} /></div></main>
  }
  const data = directory.data
  if (!data) {
    return <main className="org-page"><div className="org-loading">读取组织结构失败</div></main>
  }
  return <main className="org-page">
    {!canManage && <div className="org-readonly"><ShieldAlert size={14} />你当前是成员，只能查看。要修改，请让一位 Owner 在下方「控制台账号」里把你设为 Owner。</div>}
    <section className="org-card">
      <header><h2><Building2 size={15} />组织</h2></header>
      {data.organization
        ? <div className="org-organization">
            <RenameField unit={data.organization} canManage={canManage} />
            <code data-no-localize>{data.organization.id}</code>
          </div>
        : <p className="org-card-note">这个部署还没有组织。</p>}
      <p className="org-card-note">改名是安全的，不影响已有的预算和用量。</p>
    </section>

    <section className="org-card">
      <header>
        <h2>部门</h2>
        {canManage && <CreateDepartment />}
      </header>
      <ResizableGridTable className="org-table org-department-table" role="table" aria-label="部门列表"
        headerSelector=".org-table-head" minWidths={[200, 220, 90, 140, 120]} columnGap={12}>
        <div className="org-table-head" role="row">
          {["名称", "标识", "状态", "预算 / 用量 / 订阅", ""].map((label, index) =>
            <span className="org-table-heading" role="columnheader" key={label || index}><span>{label}</span></span>)}
        </div>
        <div role="rowgroup">
          {data.departments.map((unit) => <DepartmentRow key={unit.id} unit={unit} canManage={canManage} />)}
        </div>
      </ResizableGridTable>
      <p className="org-card-note">部门不能删除，只能停用。停用后不再出现在任何可选项里，但已经记在它名下的预算和用量原样保留。</p>
    </section>

    <MembersCard canManage={canManage} />
  </main>
}
