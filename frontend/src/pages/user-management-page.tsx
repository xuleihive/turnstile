import { useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Building2, Check, CircleAlert, Edit3, Plus, Search, ShieldCheck, UserRound, UsersRound, X } from "lucide-react"

import { Button } from "../components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../components/ui/dialog"
import { Input } from "../components/ui/input"
import { ResizableTable } from "../components/ui/resizable-table"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select"
import { ApiError } from "../api/client"
import {
  userManagementApi,
  userManagementKeys,
  type AccessRole,
  type AdminUser,
  type AdminUserCreate,
  type AdminUserPatch,
  type OrganizationUnit,
  type OrganizationUnitCreate,
  type OrganizationUnitPatch,
} from "../data-sources/apim/user-management"
import { useAuth } from "../providers/auth-provider"

type AdminTab = "users" | "organizations" | "roles"
const NO_DEPARTMENT = "__none__"

function errorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 403) {
    return "当前账号没有此操作所需的权限。请联系管理员检查角色权限。"
  }
  return error instanceof Error ? error.message : String(error)
}

function ErrorNotice({ error }: { error: unknown }) {
  return <div className="user-management-error" role="alert"><CircleAlert size={16} /><span>{errorMessage(error)}</span></div>
}

function LoadingNotice({ children = "正在加载" }: { children?: string }) {
  return <div className="user-management-state" role="status">{children}…</div>
}

function EmptyNotice({ children }: { children: string }) {
  return <div className="user-management-state"><Search size={18} />{children}</div>
}

function UserEditor({
  user,
  roles,
  units,
  busy,
  error,
  onClose,
  onSave,
}: {
  user: AdminUser | null
  roles: AccessRole[]
  units: OrganizationUnit[]
  busy: boolean
  error: unknown
  onClose: () => void
  onSave: (value: AdminUserCreate | AdminUserPatch) => void
}) {
  const [email, setEmail] = useState(user?.email ?? "")
  const [name, setName] = useState(user?.display_name ?? "")
  const [role, setRole] = useState(user?.role ?? "member")
  const [department, setDepartment] = useState(user?.organization_unit_id ?? NO_DEPARTMENT)
  const [enabled, setEnabled] = useState(user?.enabled ?? true)
  const [createPassword, setCreatePassword] = useState(false)
  const [password, setPassword] = useState("")
  const [validation, setValidation] = useState<string | null>(null)

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!user && createPassword && (password.length < 12 || password.length > 256)) {
      setValidation("初始密码长度必须为 12 到 256 个字符。")
      return
    }
    setValidation(null)
    if (user) {
      onSave({
        display_name: name.trim() || null,
        role,
        enabled,
        department_id: department === NO_DEPARTMENT ? null : department,
      })
      return
    }
    const value: AdminUserCreate = {
      email: email.trim(),
      display_name: name.trim() || undefined,
      role,
      department_id: department === NO_DEPARTMENT ? undefined : department,
    }
    if (createPassword) value.initial_password = password
    onSave(value)
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
      <DialogContent className="user-management-dialog">
        <DialogHeader>
          <DialogTitle>{user ? "编辑用户" : "添加用户"}</DialogTitle>
          <DialogDescription>{user ? "调整用户的角色、状态和所属部门。" : "默认预建 Entra 目录项；如需本地密码登录，可另外设置一次初始密码。"}</DialogDescription>
        </DialogHeader>
        <form className="user-management-form" onSubmit={submit}>
          <label><span>邮箱</span><Input type="email" required maxLength={320} disabled={Boolean(user) || busy} value={email} onChange={(event) => setEmail(event.target.value)} /></label>
          <label><span>显示名称</span><Input maxLength={160} value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label><span>角色</span>
            <Select value={role} onValueChange={(value) => { if (value) setRole(value) }}>
              <SelectTrigger aria-label="用户角色"><SelectValue /></SelectTrigger>
              <SelectContent>{roles.map((item) => <SelectItem key={item.code} value={item.code}>{item.display_name} · {item.code}</SelectItem>)}</SelectContent>
            </Select>
          </label>
          <label><span>组织部门</span>
            <Select value={department} onValueChange={(value) => setDepartment(value ?? NO_DEPARTMENT)}>
              <SelectTrigger aria-label="用户所属部门"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_DEPARTMENT}>不指定部门</SelectItem>
                {units.filter((item) => item.enabled).map((item) => <SelectItem key={item.id} value={item.id}>{item.display_name}</SelectItem>)}
              </SelectContent>
            </Select>
          </label>
          {user ? <label className="user-management-check"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /><span>允许此用户登录</span></label> : (
            <div className="user-management-password-option">
              <label className="user-management-check"><input type="checkbox" checked={createPassword} onChange={(event) => setCreatePassword(event.target.checked)} /><span>同时设置本地初始密码</span></label>
              {createPassword && <label><span>初始密码（12–256 位）</span><Input type="password" autoComplete="new-password" minLength={12} maxLength={256} required value={password} onChange={(event) => setPassword(event.target.value)} /></label>}
              <small>不设置密码时，此目录项可由匹配邮箱的 Entra 用户登录。密码只在创建时提交，不会在列表或审计记录中显示。</small>
              {createPassword && <small>密码由管理员设置后，请通过组织批准的安全渠道交付给用户。</small>}
            </div>
          )}
          {validation && <div className="user-management-error" role="alert">{validation}</div>}
          {error !== null && error !== undefined && <ErrorNotice error={error} />}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={busy} onClick={onClose}>取消</Button>
            <Button type="submit" disabled={busy || (roles.length === 0)}>{busy ? "正在保存" : "保存"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function UsersTab() {
  const queryClient = useQueryClient()
  const { can } = useAuth()
  const [searchInput, setSearchInput] = useState("")
  const [search, setSearch] = useState("")
  const [editing, setEditing] = useState<AdminUser | null | undefined>(undefined)
  const users = useQuery({ queryKey: userManagementKeys.users(search), queryFn: () => userManagementApi.listUsers(search), retry: false })
  const roles = useQuery({ queryKey: userManagementKeys.roles, queryFn: userManagementApi.listRoles, retry: false })
  const units = useQuery({ queryKey: userManagementKeys.organizationUnits, queryFn: userManagementApi.listOrganizationUnits, retry: false })
  const save = useMutation({
    mutationFn: (value: AdminUserCreate | AdminUserPatch) => editing
      ? userManagementApi.updateUser(editing.id, value as AdminUserPatch)
      : userManagementApi.createUser(value as AdminUserCreate),
    onSuccess: () => {
      setEditing(undefined)
      void queryClient.invalidateQueries({ queryKey: ["admin-users"] })
    },
  })
  const toggle = useMutation({
    mutationFn: (user: AdminUser) => userManagementApi.updateUser(user.id, { enabled: !user.enabled }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["admin-users"] }),
  })

  function submitSearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSearch(searchInput.trim())
  }

  return (
    <section className="user-management-panel">
      <header className="user-management-toolbar">
        <form onSubmit={submitSearch} className="user-management-search"><Input value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="搜索姓名或邮箱" aria-label="搜索用户" /><Button type="submit" variant="outline"><Search size={15} />搜索</Button></form>
        {can("users.manage") && <Button type="button" onClick={() => { save.reset(); setEditing(null) }}><Plus size={15} />添加用户</Button>}
      </header>
      {(users.isLoading || roles.isLoading || units.isLoading) && <LoadingNotice>正在加载用户与权限资料</LoadingNotice>}
      {users.isError && <ErrorNotice error={users.error} />}
      {roles.isError && <ErrorNotice error={roles.error} />}
      {units.isError && <ErrorNotice error={units.error} />}
      {toggle.isError && <ErrorNotice error={toggle.error} />}
      {users.isSuccess && users.data.length === 0 && <EmptyNotice>没有匹配的用户。添加用户可预建 Entra 目录项或创建本地密码账号。</EmptyNotice>}
      {users.isSuccess && users.data.length > 0 && (
        <div className="user-management-table-wrap"><ResizableTable className="user-management-table" minWidths={[220, 90, 150, 90, 160, 230]}>
          <thead><tr><th>用户</th><th>角色</th><th>组织部门</th><th>状态</th><th>最近登录</th><th>操作</th></tr></thead>
          <tbody>{users.data.map((item) => <tr key={item.id}>
            <td><span>{item.display_name || item.email}</span><small>{item.email}</small></td>
            <td><code>{item.role}</code></td>
            <td>{item.organization_unit_name || "未指定"}</td>
            <td><span className={`user-management-status ${item.enabled ? "enabled" : "disabled"}`}>{item.enabled ? "启用" : "停用"}</span></td>
            <td>{item.last_login_at ? new Date(item.last_login_at).toLocaleString() : "从未登录"}</td>
            <td><div className="user-management-row-actions">{can("users.manage") && <><Button type="button" size="sm" variant="outline" disabled={save.isPending || toggle.isPending} onClick={() => { save.reset(); setEditing(item) }}><Edit3 size={13} />编辑</Button><Button type="button" size="sm" variant="outline" disabled={toggle.isPending} onClick={() => toggle.mutate(item)}>{item.enabled ? <X size={13} /> : <Check size={13} />}{item.enabled ? "停用" : "启用"}</Button></>}</div></td>
          </tr>)}</tbody>
        </ResizableTable></div>
      )}
      {editing !== undefined && roles.data && units.data && <UserEditor key={editing?.id ?? "create"} user={editing} roles={roles.data} units={units.data} busy={save.isPending} error={save.error} onClose={() => { save.reset(); setEditing(undefined) }} onSave={(value) => save.mutate(value)} />}
    </section>
  )
}

function UnitEditor({
  unit,
  units,
  busy,
  error,
  onClose,
  onSave,
}: {
  unit: OrganizationUnit | null
  units: OrganizationUnit[]
  busy: boolean
  error: unknown
  onClose: () => void
  onSave: (value: OrganizationUnitCreate | OrganizationUnitPatch) => void
}) {
  const [code, setCode] = useState("")
  const [name, setName] = useState(unit?.display_name ?? "")
  const rootUnit = units.find((item) => item.parent_id === null)
  const [parentId, setParentId] = useState(unit?.parent_id ?? rootUnit?.id ?? NO_DEPARTMENT)

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    onSave(unit
      ? { display_name: name.trim() }
      : { code: code.trim(), display_name: name.trim(), parent_id: parentId === NO_DEPARTMENT ? undefined : parentId })
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
      <DialogContent className="user-management-dialog">
        <DialogHeader><DialogTitle>{unit ? "编辑部门" : "新建部门"}</DialogTitle><DialogDescription>部门用于组织用户与用量归属。仍有成员或子部门时，后端会阻止删除。</DialogDescription></DialogHeader>
        <form className="user-management-form" onSubmit={submit}>
          {!unit && <label><span>部门代码</span><Input required maxLength={100} value={code} onChange={(event) => setCode(event.target.value)} /></label>}
          <label><span>部门名称</span><Input required maxLength={160} value={name} onChange={(event) => setName(event.target.value)} /></label>
          {!unit && <label><span>上级部门</span><Select value={parentId} onValueChange={(value) => setParentId(value ?? NO_DEPARTMENT)}><SelectTrigger aria-label="上级部门"><SelectValue /></SelectTrigger><SelectContent>{units.filter((item) => item.enabled).map((item) => <SelectItem key={item.id} value={item.id}>{item.parent_id === null ? "企业根节点" : item.display_name}</SelectItem>)}</SelectContent></Select></label>}
          {error !== null && error !== undefined && <ErrorNotice error={error} />}
          <DialogFooter><Button type="button" variant="outline" disabled={busy} onClick={onClose}>取消</Button><Button type="submit" disabled={busy}>{busy ? "正在保存" : "保存"}</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function unitDepth(unit: OrganizationUnit, byId: Map<string, OrganizationUnit>) {
  let depth = 0
  let parentId = unit.parent_id
  const seen = new Set<string>([unit.id])
  while (parentId && !seen.has(parentId)) {
    seen.add(parentId)
    const parent = byId.get(parentId)
    if (!parent) break
    depth += 1
    parentId = parent.parent_id
  }
  return depth
}

function OrganizationsTab() {
  const queryClient = useQueryClient()
  const { can } = useAuth()
  const [editing, setEditing] = useState<OrganizationUnit | null | undefined>(undefined)
  const units = useQuery({ queryKey: userManagementKeys.organizationUnits, queryFn: userManagementApi.listOrganizationUnits, retry: false })
  const save = useMutation({
    mutationFn: (value: OrganizationUnitCreate | OrganizationUnitPatch) => editing
      ? userManagementApi.updateOrganizationUnit(editing.id, value as OrganizationUnitPatch)
      : userManagementApi.createOrganizationUnit(value as OrganizationUnitCreate),
    onSuccess: () => {
      setEditing(undefined)
      void queryClient.invalidateQueries({ queryKey: userManagementKeys.organizationUnits })
    },
  })
  const toggle = useMutation({
    mutationFn: (unit: OrganizationUnit) => userManagementApi.updateOrganizationUnit(unit.id, { enabled: !unit.enabled }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: userManagementKeys.organizationUnits }),
  })
  const remove = useMutation({
    mutationFn: (unit: OrganizationUnit) => userManagementApi.deleteOrganizationUnit(unit.id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: userManagementKeys.organizationUnits }),
  })
  const byId = useMemo(() => new Map((units.data ?? []).map((item) => [item.id, item])), [units.data])

  return (
    <section className="user-management-panel">
      <header className="user-management-toolbar"><p>部门构成单企业组织树；根节点由系统维护。</p>{can("org.manage") && <Button type="button" onClick={() => { save.reset(); setEditing(null) }}><Plus size={15} />新建部门</Button>}</header>
      {units.isLoading && <LoadingNotice>正在加载组织部门</LoadingNotice>}
      {units.isError && <ErrorNotice error={units.error} />}
      {save.isError && <ErrorNotice error={save.error} />}
      {toggle.isError && <ErrorNotice error={toggle.error} />}
      {remove.isError && <ErrorNotice error={remove.error} />}
      {units.isSuccess && units.data.length === 0 && <EmptyNotice>尚未创建部门。</EmptyNotice>}
      {units.isSuccess && units.data.length > 0 && <div className="user-management-unit-list">
        {units.data.map((item) => <div className="user-management-unit-row" key={item.id} style={{ "--unit-depth": unitDepth(item, byId) } as React.CSSProperties}>
          <div className="user-management-unit-name"><Building2 size={15} /><span>{item.display_name}</span><code>{item.code}</code>{item.parent_id === null && <small>根节点</small>}</div>
          <span className={`user-management-status ${item.enabled ? "enabled" : "disabled"}`}>{item.enabled ? "启用" : "停用"}</span>
          {can("org.manage") && <div className="user-management-row-actions"><Button type="button" size="sm" variant="outline" disabled={item.parent_id === null || save.isPending} onClick={() => { save.reset(); setEditing(item) }}><Edit3 size={13} />编辑</Button>{item.parent_id !== null && <><Button type="button" size="sm" variant="outline" disabled={toggle.isPending} onClick={() => toggle.mutate(item)}>{item.enabled ? "停用" : "启用"}</Button><Button type="button" size="sm" variant="destructive" disabled={remove.isPending} onClick={() => { if (window.confirm(`确定删除部门“${item.display_name}”？仍有成员或子部门时系统会拒绝删除。`)) remove.mutate(item) }}>删除</Button></>}</div>}
        </div>)}
      </div>}
      {editing !== undefined && units.data && <UnitEditor key={editing?.id ?? "create"} unit={editing} units={units.data} busy={save.isPending} error={save.error} onClose={() => { save.reset(); setEditing(undefined) }} onSave={(value) => save.mutate(value)} />}
    </section>
  )
}

function RolesTab() {
  const roles = useQuery({ queryKey: userManagementKeys.roles, queryFn: userManagementApi.listRoles, retry: false })
  return (
    <section className="user-management-panel">
      <div className="user-management-readonly-note"><ShieldCheck size={16} /><span>角色权限为只读。系统内置角色不可删除或编辑；扩展角色管理将在后续版本提供。</span></div>
      {roles.isLoading && <LoadingNotice>正在加载角色权限</LoadingNotice>}
      {roles.isError && <ErrorNotice error={roles.error} />}
      {roles.isSuccess && roles.data.map((role) => <article className="user-management-role-card" key={role.code}>
        <header><div><h3>{role.display_name}</h3><code>{role.code}</code></div><span>{role.is_builtin ? "内置角色" : "自定义角色"}</span></header>
        <p>{role.permissions.length} 项权限</p>
        <div className="user-management-permissions">{role.permissions.map((permission) => <code key={permission}>{permission}</code>)}</div>
      </article>)}
    </section>
  )
}

export function UserManagementPage() {
  const { can } = useAuth()
  const [tab, setTab] = useState<AdminTab>("users")
  if (!can("users.read")) {
    return <div className="user-management-state error" role="alert">当前账号没有用户与组织管理权限。</div>
  }
  const tabs: Array<{ id: AdminTab; label: string; icon: typeof UserRound }> = [
    { id: "users", label: "用户", icon: UserRound },
    { id: "organizations", label: "组织部门", icon: Building2 },
    { id: "roles", label: "角色权限", icon: ShieldCheck },
  ]
  return (
    <section className="user-management-page">
      <header className="user-management-heading"><div className="user-management-title-icon"><UsersRound size={19} /></div><div><h1>用户与组织</h1><p>管理单企业用户目录、组织部门和内置角色权限。用户 Key 与计费归属将在后续阶段接入。</p></div></header>
      <nav className="user-management-tabs" aria-label="用户管理页面">
        {tabs.map((item) => <button type="button" key={item.id} className={tab === item.id ? "active" : ""} aria-current={tab === item.id ? "page" : undefined} onClick={() => setTab(item.id)}><item.icon size={15} />{item.label}</button>)}
      </nav>
      {tab === "users" && <UsersTab />}
      {tab === "organizations" && <OrganizationsTab />}
      {tab === "roles" && <RolesTab />}
    </section>
  )
}
