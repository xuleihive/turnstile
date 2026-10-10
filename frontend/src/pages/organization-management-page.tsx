import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Building2, ChevronLeft, ChevronRight, Ellipsis, FolderTree, Link2, PanelLeft,
  Pencil, Plus, RefreshCw, Save, Shield, UserRound, Users,
} from "lucide-react"
import {
  directoryApi, type DirectoryCapabilities, type DirectoryOrganization,
  type AdministratorScopeKind, type DirectoryPerson, type DirectoryUnit,
} from "../data-sources/apim/api/organization-management"
import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { Textarea } from "../components/ui/textarea"
import { Checkbox } from "../components/ui/checkbox"
import { Switch } from "../components/ui/switch"
import { Alert, AlertDescription } from "../components/ui/alert"
import { Badge } from "../components/ui/badge"
import { Empty, EmptyContent, EmptyTitle } from "../components/ui/empty"
import { Field, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "../components/ui/field"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "../components/ui/dialog"
import { confirmDirectoryLeave, DirectorySelect as MenuSelect, useDirectoryLeaveGuard } from "../components/directory-controls"
import { DirectorySyncPanel } from "./directory-sync-panel"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuTrigger,
} from "../components/ui/dropdown-menu"
import { ResizableTable } from "../components/ui/resizable-table"
import { cn } from "../lib/utils"
import { useAuth } from "../providers/auth-provider"
import { useTimezone } from "../providers/timezone-provider"
import { getIntlLocale, useLocale } from "../locales/index"
import { FINOPS_NAVIGATE_EVENT } from "../lib/navigation"
import { menuPermissionGroupNames, type MenuAdministratorScope, type MenuPermissionGroup } from "../api/auth"
import {
  directoryOrganizationSelection, directoryUnitSelection, managedDirectoryStatus,
  sortDirectoryNodes, visibleDirectoryOrganizations, visibleDirectoryUnits, type ManagedDirectoryStatus,
} from "../lib/directory-visibility"

const statusNames: Record<ManagedDirectoryStatus, string> = { active: "启用", archived: "已归档" }
const peopleColumnMinWidths = [160, 160, 200, 160, 80, 100, 60] as const
const actionNames: Record<string, string> = {
  created: "新增", updated: "编辑", account_linked: "关联账号", status_changed: "状态变更",
  teams_updated: "团队成员变更", administrators_updated: "管理员变更", scheduled: "计划调岗",
  menu_administrators_updated: "菜单管理员变更",
}
type Editor = {
  kind: "organization" | "department" | "team" | "person"
  row?: DirectoryOrganization | DirectoryUnit | DirectoryPerson
}

function FormInput({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return <Field><FieldLabel htmlFor={id}>{label}</FieldLabel>{children}</Field>
}

function MenuGroupTags({ groups, scopes = [] }: {
  groups: MenuPermissionGroup[]; scopes?: MenuAdministratorScope[]
}) {
  const admins = groups.filter((group) => group !== "user")
  return <div className="directory-permission-tags">
    {(admins.length ? admins : ["user"] as MenuPermissionGroup[]).map((group) =>
      <Badge key={group} variant="outline"
        title={scopes.filter((scope) => `${scope.scope_kind}_admin` === group).map((scope) => scope.scope_name).join("、") || undefined}>
        {menuPermissionGroupNames[group]}
      </Badge>)}
  </div>
}

function DirectoryEditor({ editor, organizationId, departmentId, units, onClose, onSaved }: {
  editor: Editor; organizationId: string; departmentId: string; units: DirectoryUnit[]
  onClose: () => void; onSaved: (id: string) => void
}) {
  const row = editor.row
  const person = editor.kind === "person" ? row as DirectoryPerson | undefined : undefined
  const entity = editor.kind !== "person" ? row as DirectoryOrganization | DirectoryUnit | undefined : undefined
  const [name, setName] = useState(person?.display_name ?? entity?.name ?? "")
  const [code, setCode] = useState(entity?.code ?? "")
  const [email, setEmail] = useState(person?.contact_email ?? (
    entity && "contact_email" in entity ? entity.contact_email ?? "" : ""
  ))
  const [password, setPassword] = useState("")
  const [primaryDepartmentId, setPrimaryDepartmentId] = useState("")
  const [employee, setEmployee] = useState(person?.employee_number ?? "")
  const menuGroups = person?.scope_menu_permission_groups ?? ["user"]
  const [description, setDescription] = useState(entity?.description ?? "")
  const [status, setStatus] = useState<ManagedDirectoryStatus>(managedDirectoryStatus(entity?.status ?? "active"))
  const [dirty, setDirty] = useState(false)
  useDirectoryLeaveGuard(dirty)
  const key = useMemo(() => crypto.randomUUID(), [])
  const label = { organization: "组织", department: "部门", team: "团队", person: "人员" }[editor.kind]
  const mutation = useMutation({
    mutationFn: async () => {
      if (editor.kind === "person") {
        if (person) return directoryApi.updatePerson(person.id, {
          display_name: name, contact_email: email || null, employee_number: employee || null,
          expected_revision: person.revision,
        })
        return directoryApi.createPerson({
          organization_id: organizationId, display_name: name, email, password,
          department_id: primaryDepartmentId,
          employee_number: employee || null,
        }, key)
      }
      const value = { code, name, description, status, expected_revision: entity?.revision }
      if (editor.kind === "organization") {
        return entity ? directoryApi.updateOrganization(entity.id, { ...value, contact_email: email || null })
          : directoryApi.createOrganization({ ...value, contact_email: email || null }, key)
      }
      const unitValue = {
        ...value, kind: editor.kind,
        parent_unit_id: editor.kind === "team"
          ? (entity as DirectoryUnit | undefined)?.parent_unit_id ?? departmentId : null,
      }
      return entity ? directoryApi.updateUnit(entity.id, unitValue)
        : directoryApi.createUnit(organizationId, unitValue, key)
    },
    onSuccess: (saved) => onSaved(saved.id),
  })
  const change = (setter: (value: string) => void) => (value: string) => {
    setter(value); setDirty(true)
  }
  const close = () => {
    if (!mutation.isPending && (!dirty || window.confirm("放弃未保存的更改？"))) onClose()
  }
  const submit = (event: FormEvent) => { event.preventDefault(); mutation.mutate() }
  return <Dialog open onOpenChange={(open) => { if (!open) close() }}>
    <DialogContent className="directory-dialog">
      <DialogHeader><DialogTitle>{entity || person ? "编辑" : "新增"}{label}</DialogTitle>
        <DialogDescription>{person?.externally_managed ? "Entra ID" : label}</DialogDescription>
      </DialogHeader>
      <form onSubmit={submit}>
        <FieldGroup>
          <FormInput id="directory-name" label="名称">
            <Input id="directory-name" autoFocus value={name} required maxLength={160}
              disabled={person?.externally_managed} onChange={(event) => change(setName)(event.target.value)} />
          </FormInput>
          {editor.kind !== "person" && <FormInput id="directory-code" label="编码">
            <Input id="directory-code" value={code} required maxLength={64} pattern={"[a-zA-Z0-9][a-zA-Z0-9_\\-]*"}
              onChange={(event) => change(setCode)(event.target.value)} />
          </FormInput>}
          {(editor.kind === "person" || editor.kind === "organization") && <FormInput id="directory-email" label={editor.kind === "person" && !person ? "邮箱" : "联系邮箱"}>
            <Input id="directory-email" type="email" value={email} required={editor.kind === "person" && !person} maxLength={255}
              disabled={person?.externally_managed} onChange={(event) => change(setEmail)(event.target.value)} />
          </FormInput>}
          {editor.kind === "person" && !person && <FormInput id="directory-password" label="登录密码">
            <Input id="directory-password" type="password" autoComplete="new-password" value={password}
              required minLength={12} maxLength={256} onChange={(event) => change(setPassword)(event.target.value)} />
          </FormInput>}
          {editor.kind === "person" && !person && <Field><FieldLabel>归属部门</FieldLabel>
            <MenuSelect label="归属部门" value={primaryDepartmentId} required
              items={units.filter((unit) => unit.kind === "department" && unit.organization_id === organizationId && unit.status === "active")
                .map((unit) => ({ value: unit.id, label: unit.name }))}
              onChange={(value) => { setPrimaryDepartmentId(value); setDirty(true) }} />
          </Field>}
          {editor.kind === "person" ? <>
            <FormInput id="directory-employee" label="员工号"><Input id="directory-employee" value={employee}
              maxLength={64} onChange={(event) => change(setEmployee)(event.target.value)} /></FormInput>
            <Field><FieldLabel>菜单权限组</FieldLabel>
              <MenuGroupTags groups={menuGroups} scopes={person?.scope_menu_administrator_scopes} />
            </Field>
          </> : <>
            <FormInput id="directory-description" label="描述"><Textarea id="directory-description"
              value={description} maxLength={2000} onChange={(event) => change(setDescription)(event.target.value)} /></FormInput>
            {entity && <Field><FieldLabel>状态</FieldLabel><MenuSelect label="状态" value={status}
              items={Object.entries(statusNames).map(([value, label]) => ({ value, label }))}
              onChange={(value) => { setStatus(value as ManagedDirectoryStatus); setDirty(true) }} /></Field>}
          </>}
        </FieldGroup>
        {mutation.error && <Alert><AlertDescription>{mutation.error.message}</AlertDescription></Alert>}
        <DialogFooter><Button type="button" variant="outline" onClick={close} disabled={mutation.isPending}>取消</Button>
          <Button type="submit" disabled={mutation.isPending || (Boolean(row) && !dirty)
            || (editor.kind === "person" && !person && !primaryDepartmentId)}>
            {mutation.isPending ? <RefreshCw className="spin" data-icon="inline-start" /> : <Save data-icon="inline-start" />}保存
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function PersonActionDialog({ person, action, units, onClose, onSaved }: {
  person: DirectoryPerson; action: "link" | "teams" | "status"
  units: DirectoryUnit[]; onClose: () => void; onSaved: () => void
}) {
  const [accountId, setAccountId] = useState(person.app_user_id ?? "")
  const [accountQuery, setAccountQuery] = useState(person.account_email ?? "")
  const manualTeams = person.manual_team_ids ?? (person.externally_managed ? [] : person.team_ids)
  const sourceTeams = person.source_team_ids ?? (person.externally_managed ? person.team_ids : [])
  const [teamIds, setTeamIds] = useState(manualTeams)
  const [disableAccount, setDisableAccount] = useState(false)
  const [reason, setReason] = useState("")
  useDirectoryLeaveGuard(Boolean(reason || disableAccount || accountId !== (person.app_user_id ?? "")
    || [...teamIds].sort().join() !== [...manualTeams].sort().join()))
  const accounts = useQuery({
    queryKey: ["directory", "accounts", accountQuery],
    queryFn: () => directoryApi.accounts(accountQuery), enabled: action === "link",
  })
  const statusPreview = useQuery({
    queryKey: ["directory", "status-preview", person.id], queryFn: () => directoryApi.statusPreview(person.id),
    enabled: action === "status",
  })
  const save = useMutation({
    mutationFn: async () => {
      const common = { expected_revision: person.revision, reason }
      if (action === "link") return directoryApi.linkAccount(person.id, { ...common, app_user_id: accountId })
      if (action === "teams") return directoryApi.setTeams(person.id, { ...common, team_ids: teamIds })
      if (action === "status") return directoryApi.setStatus(person.id, {
        ...common, status: person.status === "active" ? "archived" : "active", disable_account: disableAccount,
      })
    },
    onSuccess: onSaved,
  })
  const label = { link: "关联账号", teams: "团队成员", status: "人员状态" }[action]
  const teams = units.filter((unit) => unit.kind === "team" && unit.organization_id === person.organization_id && unit.status === "active")
  const allowed = action === "link" ? Boolean(accountId)
    : action === "status" ? Boolean(statusPreview.data) : true
  return <Dialog open onOpenChange={(open) => { if (!open && !save.isPending) onClose() }}>
    <DialogContent className="directory-dialog">
      <DialogHeader><DialogTitle>{label}</DialogTitle><DialogDescription data-no-localize>{person.display_name}</DialogDescription></DialogHeader>
      <FieldGroup>
        {action === "link" && <FormInput id="directory-account-search" label="搜索登录账号">
          <Input id="directory-account-search" value={accountQuery} maxLength={200}
            onChange={(event) => { setAccountQuery(event.target.value); setAccountId("") }} />
        </FormInput>}
        {action === "link" && <Field><FieldLabel>登录账号</FieldLabel><MenuSelect label="登录账号" value={accountId}
          items={(accounts.data ?? []).map((account) => ({ value: account.id, label: `${account.display_name ?? account.email} · ${account.email}` }))}
          onChange={setAccountId} /></Field>}
        {action === "teams" && <FieldSet><FieldLegend>团队</FieldLegend><div className="directory-checkboxes directory-team-options">
          {teams.map((team) => <label key={team.id}><Checkbox
            checked={teamIds.includes(team.id) || sourceTeams.includes(team.id)}
            disabled={sourceTeams.includes(team.id)}
            onCheckedChange={(checked) => setTeamIds((current) => checked ? [...current, team.id] : current.filter((id) => id !== team.id))} />
            <span className="directory-team-name" data-no-localize>{team.name}</span>
            {sourceTeams.includes(team.id) && <Badge variant="outline">Entra ID</Badge>}</label>)}
          {teams.length === 0 && <span>暂无团队</span>}
        </div></FieldSet>}
        {action === "status" && <>
          <dl className="directory-status-facts">
            <div><dt>人员状态</dt><dd><Badge>{statusNames[managedDirectoryStatus(person.status)]}</Badge></dd></div>
            <div><dt>变更后</dt><dd><Badge>{person.status === "active" ? "已归档" : "启用"}</Badge></dd></div>
            <div><dt>历史预算</dt><dd>{statusPreview.data?.budgets.length ?? "--"}</dd></div>
            <div><dt>网关归属</dt><dd><Badge variant="outline">待同步</Badge></dd></div>
          </dl>
          {person.status === "active" && person.app_user_id && <label className="directory-checkbox-line">
            <Checkbox checked={disableAccount} onCheckedChange={(checked) => setDisableAccount(Boolean(checked))} />同时停用登录账号
          </label>}
        </>}
        <FormInput id="directory-action-reason" label="变更原因"><Textarea id="directory-action-reason" value={reason}
          maxLength={2000} onChange={(event) => setReason(event.target.value)} /></FormInput>
      </FieldGroup>
      {(save.error || accounts.error || statusPreview.error) && <Alert><AlertDescription>
        {(save.error || accounts.error || statusPreview.error)?.message}
      </AlertDescription></Alert>}
      <DialogFooter><Button type="button" variant="outline" disabled={save.isPending} onClick={onClose}>取消</Button>
        <Button type="button" disabled={!allowed || save.isPending} onClick={() => save.mutate()}><Save data-icon="inline-start" />确认</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
}

function AddMembersDialog({ unit, scopeKey, onClose, onSaved }: {
  unit: DirectoryUnit; scopeKey: string; onClose: () => void; onSaved: () => void
}) {
  const [query, setQuery] = useState("")
  const [cursor, setCursor] = useState("")
  const [previous, setPrevious] = useState<string[]>([])
  const [selection, setSelection] = useState<string[]>([])
  useDirectoryLeaveGuard(selection.length > 0)
  const people = useQuery({
    queryKey: ["directory", "member-candidates", unit.id, query, cursor, scopeKey],
    queryFn: () => directoryApi.people({ available_unit_id: unit.id, query, cursor }),
  })
  const save = useMutation({
    mutationFn: () => directoryApi.addMembers(unit.id, {
      person_ids: selection, expected_revision: unit.revision,
    }),
    onSuccess: onSaved,
  })
  return <Dialog open onOpenChange={(open) => { if (!open && !save.isPending) onClose() }}>
    <DialogContent className="directory-dialog">
      <DialogHeader><DialogTitle>添加成员</DialogTitle><DialogDescription data-no-localize>{unit.name}</DialogDescription></DialogHeader>
      <Input aria-label="搜索可添加人员" placeholder="搜索人员" value={query}
        onChange={(event) => { setQuery(event.target.value); setCursor(""); setPrevious([]) }} />
      <FieldSet><FieldLegend>组织成员</FieldLegend><div className="directory-checkboxes">
        {(people.data?.items ?? []).map((person) => <label key={person.id}>
          <Checkbox checked={selection.includes(person.id)} disabled={save.isPending}
            onCheckedChange={(checked) => setSelection((current) => checked
              ? [...current, person.id] : current.filter((id) => id !== person.id))} />
          <span data-no-localize>{person.display_name}</span><small data-no-localize>{person.contact_email ?? person.governance_user_id}</small>
        </label>)}
      </div></FieldSet>
      {people.isPending ? <Empty><EmptyTitle>正在加载</EmptyTitle></Empty>
        : people.data?.total === 0 && <Empty><EmptyTitle>暂无可添加人员</EmptyTitle></Empty>}
      {(people.error || save.error) && <Alert><AlertDescription>{(people.error || save.error)?.message}</AlertDescription></Alert>}
      <div className="directory-pagination">
        <Button variant="outline" size="icon-sm" aria-label="上一页" title="上一页" disabled={!previous.length}
          onClick={() => { setCursor(previous.at(-1) ?? ""); setPrevious((values) => values.slice(0, -1)) }}><ChevronLeft /></Button>
        <Button variant="outline" size="icon-sm" aria-label="下一页" title="下一页" disabled={!people.data?.next_cursor}
          onClick={() => { setPrevious((values) => [...values, cursor]); setCursor(people.data?.next_cursor ?? "") }}><ChevronRight /></Button>
      </div>
      <DialogFooter><Button variant="outline" disabled={save.isPending} onClick={onClose}>取消</Button>
        <Button disabled={!selection.length || save.isPending} onClick={() => save.mutate()}><Plus data-icon="inline-start" />添加</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
}

function ScopeAdministrators({ scope, kind, owner, scopeKey, onSaved }: {
  scope: DirectoryOrganization | DirectoryUnit; kind: AdministratorScopeKind
  owner: boolean; scopeKey: string; onSaved: () => void
}) {
  const organizationId = kind === "organization" ? scope.id : (scope as DirectoryUnit).organization_id
  const departmentId = kind === "department" ? scope.id : kind === "team" ? (scope as DirectoryUnit).parent_unit_id ?? "" : ""
  const title = menuPermissionGroupNames[`${kind}_admin` as MenuPermissionGroup]
  const existing = useQuery({
    queryKey: ["directory", "administrators", kind, scope.id, scopeKey],
    queryFn: () => directoryApi.menuAdministrators(kind, scope.id),
  })
  const [selection, setSelection] = useState<string[] | null>(null)
  const [query, setQuery] = useState("")
  const [cursor, setCursor] = useState("")
  const [previous, setPrevious] = useState<string[]>([])
  const people = useQuery({
    queryKey: ["directory", "administrator-candidates", kind, scope.id, query, cursor, scopeKey],
    queryFn: () => directoryApi.people({
      organization_id: organizationId, ...(kind === "department" && departmentId ? { department_id: departmentId } : {}),
      ...(kind === "team" ? { team_id: scope.id } : {}), status: "active", query, cursor,
    }),
  })
  useDirectoryLeaveGuard(selection !== null)
  const selected = selection ?? existing.data?.map((row) => row.app_user_id) ?? []
  const save = useMutation({
    mutationFn: () => {
      const value = { account_ids: selected, expected_revision: scope.revision }
      return directoryApi.setMenuAdministrators(kind, scope.id, value)
    },
    onSuccess: () => { setSelection(null); onSaved() },
  })
  const candidates = (people.data?.items ?? []).filter((person) => person.app_user_id && person.account_enabled
    && !person.manual_disabled && !person.source_disabled)
  return <div className="directory-admins">
    <Input aria-label="搜索管理员候选人" placeholder="搜索人员" value={query}
      onChange={(event) => { setQuery(event.target.value); setCursor(""); setPrevious([]) }} />
    {existing.error && <Alert><AlertDescription>{existing.error.message}</AlertDescription></Alert>}
    <FieldSet><FieldLegend>{title}</FieldLegend><div className="directory-checkboxes">
      {candidates.map((person) => <label key={person.id}><Checkbox
        checked={selected.includes(person.app_user_id!)} disabled={!owner || existing.isPending || Boolean(existing.error)}
        onCheckedChange={(checked) => setSelection(checked ? [...selected, person.app_user_id!] : selected.filter((id) => id !== person.app_user_id))} />
        <span data-no-localize>{person.display_name}</span><small data-no-localize>{person.account_email}</small>
      </label>)}
    </div></FieldSet>
    {(existing.data ?? []).filter((admin) => !candidates.some((person) => person.app_user_id === admin.app_user_id)).map((admin) =>
      <label key={admin.id} className="directory-existing-admin">
        <Checkbox checked={selected.includes(admin.app_user_id)} disabled={!owner || existing.isPending || Boolean(existing.error)}
          onCheckedChange={(checked) => setSelection(checked ? [...selected, admin.app_user_id] : selected.filter((id) => id !== admin.app_user_id))} />
        <Shield /><span data-no-localize>{admin.display_name ?? admin.email}</span><small data-no-localize>{admin.email}</small></label>,
    )}
    {people.isPending ? <Empty><EmptyTitle>正在加载</EmptyTitle></Empty>
      : candidates.length === 0 && <Empty><EmptyTitle>暂无已关联账号的人员</EmptyTitle></Empty>}
    {people.error && <Alert><AlertDescription>{people.error.message}</AlertDescription></Alert>}
    <div className="directory-pagination">
      <Button variant="outline" size="icon-sm" aria-label="上一页" title="上一页" disabled={!previous.length}
        onClick={() => { setCursor(previous.at(-1) ?? ""); setPrevious((values) => values.slice(0, -1)) }}><ChevronLeft /></Button>
      <Button variant="outline" size="icon-sm" aria-label="下一页" title="下一页" disabled={!people.data?.next_cursor}
        onClick={() => { setPrevious((values) => [...values, cursor]); setCursor(people.data?.next_cursor ?? "") }}><ChevronRight /></Button>
    </div>
    {save.error && <Alert><AlertDescription>{save.error.message}</AlertDescription></Alert>}
    {owner && <Button disabled={selection === null || save.isPending || existing.isPending || Boolean(existing.error)} onClick={() => save.mutate()}>
      <Save data-icon="inline-start" />保存管理员
    </Button>}
  </div>
}

export function OrganizationManagementPage({ onToggleSidebar, capabilities }: {
  onToggleSidebar: () => void; capabilities: DirectoryCapabilities | undefined
}) {
  const { user, refreshProfile } = useAuth()
  const { locale } = useLocale()
  const { timezone } = useTimezone()
  const queryClient = useQueryClient()
  const [organizationId, setOrganizationId] = useState(() => new URLSearchParams(location.search).get("directory_org") ?? "")
  const [unitId, setUnitId] = useState(() => new URLSearchParams(location.search).get("directory_unit") ?? "")
  const [query, setQuery] = useState("")
  const [debouncedQuery, setDebouncedQuery] = useState("")
  const [status, setStatus] = useState<ManagedDirectoryStatus | "all">("all")
  const [hideArchived, setHideArchived] = useState(true)
  const [cursor, setCursor] = useState("")
  const [previousCursors, setPreviousCursors] = useState<string[]>([])
  const [editor, setEditor] = useState<Editor | null>(null)
  const [action, setAction] = useState<{ person: DirectoryPerson; action: "link" | "teams" | "status" } | null>(null)
  const [addingMembers, setAddingMembers] = useState(false)
  const [tab, setTab] = useState("people")
  const canRead = capabilities?.can_read === true
  const owner = capabilities?.can_manage_organizations === true
  const scope = [user?.id, capabilities?.permission_revision]
  const organizations = useQuery({
    queryKey: ["directory", "organizations", ...scope], queryFn: directoryApi.organizations,
    enabled: canRead,
  })
  const sortedOrganizations = useMemo(() => sortDirectoryNodes(organizations.data ?? [], locale),
    [organizations.data, locale])
  const visibleOrganizations = visibleDirectoryOrganizations(sortedOrganizations, hideArchived)
  const effectiveOrgId = directoryOrganizationSelection(sortedOrganizations, organizationId, hideArchived)
  const organization = organizations.data?.find((row) => row.id === effectiveOrgId)
  const units = useQuery({
    queryKey: ["directory", "units", effectiveOrgId, ...scope],
    queryFn: () => directoryApi.units(effectiveOrgId), enabled: canRead && Boolean(organization),
  })
  const sortedUnits = useMemo(() => sortDirectoryNodes(units.data ?? [], locale), [units.data, locale])
  const visibleUnits = visibleDirectoryUnits(sortedUnits, hideArchived)
  const effectiveUnitId = effectiveOrgId !== organizationId && organizationId ? ""
    : directoryUnitSelection(units.data ?? [], unitId, hideArchived)
  const selectedUnit = visibleUnits.find((row) => row.id === effectiveUnitId)
  const departmentId = selectedUnit?.kind === "department" ? selectedUnit.id : selectedUnit?.parent_unit_id ?? ""
  const department = units.data?.find((row) => row.id === departmentId)
  const teamId = selectedUnit?.kind === "team" ? selectedUnit.id : undefined
  const people = useQuery({
    queryKey: ["directory", "people", effectiveOrgId, departmentId, teamId, debouncedQuery, status, cursor, ...scope],
    queryFn: () => directoryApi.people({
      organization_id: effectiveOrgId,
      ...(!teamId && departmentId ? { department_id: departmentId } : {}),
      ...(teamId ? { team_id: teamId } : {}), query: debouncedQuery,
      ...(status !== "all" ? { status } : {}), ...(cursor ? { cursor } : {}),
    }),
    enabled: canRead && Boolean(organization) && (!effectiveUnitId || Boolean(selectedUnit)) && tab === "people",
  })
  const audit = useQuery({
    queryKey: ["directory", "audit", departmentId, ...scope],
    queryFn: () => directoryApi.audit(departmentId || undefined),
    enabled: canRead && tab === "audit",
  })
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 250)
    return () => clearTimeout(timer)
  }, [query])
  useEffect(() => { setCursor(""); setPreviousCursors([]) }, [unitId, effectiveOrgId, debouncedQuery, status])
  useEffect(() => {
    if (!organizations.data) return
    if (organizationId && effectiveOrgId !== organizationId) {
      setOrganizationId(effectiveOrgId); setUnitId(""); setTab("people")
    } else if (units.data && effectiveUnitId !== unitId) {
      setUnitId(effectiveUnitId); setTab("people")
    }
  }, [organizations.data, units.data, organizationId, effectiveOrgId, unitId, effectiveUnitId])
  useEffect(() => {
    const url = new URL(location.href)
    if (organizationId) url.searchParams.set("directory_org", organizationId); else url.searchParams.delete("directory_org")
    if (unitId) url.searchParams.set("directory_unit", unitId); else url.searchParams.delete("directory_unit")
    history.replaceState(null, "", url)
    window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT))
  }, [organizationId, unitId])
  const changed = async () => {
    setEditor(null); setAction(null); setAddingMembers(false)
    await refreshProfile()
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["directory"] }),
      queryClient.invalidateQueries({ queryKey: ["reference", "enterprise-entities"] }),
      queryClient.invalidateQueries({ queryKey: ["reference", "enterprise-query-entities"] }),
      queryClient.invalidateQueries({ queryKey: ["finops"] }),
      queryClient.invalidateQueries({ queryKey: ["user-settings"] }),
    ])
  }
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["directory"] })
  const localCapabilities = capabilities?.department_capabilities?.[departmentId] ?? []
  const editPeople = owner || localCapabilities.includes("directory.edit_people")
  const editTeams = owner || localCapabilities.includes("directory.edit_teams")
  const selectOrganization = (id: string) => {
    if (confirmDirectoryLeave()) { setOrganizationId(id); setUnitId(""); setTab("people") }
  }
  const selectUnit = (id: string) => { if (confirmDirectoryLeave()) { setUnitId(id); setTab("people") } }
  const error = organizations.error || units.error || people.error || audit.error
  const missing = organizations.data && organizationId && !organization
    || units.data && effectiveUnitId && !selectedUnit
  return <div className="finops-workspace directory-workspace">
    <header className="finops-header"><div>
      <Button variant="ghost" size="icon-sm" className="finops-sidebar-trigger" aria-label="切换导航栏" title="切换导航栏" onClick={onToggleSidebar}><PanelLeft /></Button>
      <span className="finops-header-icon"><FolderTree size={17} aria-hidden="true" /></span><h1>组织管理</h1>
    </div><Button variant="ghost" size="icon-sm" aria-label="刷新组织信息" title="刷新组织信息" onClick={() => void refresh()}><RefreshCw /></Button></header>
    {!capabilities ? <Empty><EmptyTitle>正在加载</EmptyTitle></Empty> : !canRead ? <Empty><EmptyTitle>无组织管理权限</EmptyTitle></Empty>
      : <div className="directory-layout">
        <aside className="directory-tree" aria-label="组织目录">
          <div className="directory-tree-heading"><strong>组织目录</strong>{owner && <Button variant="ghost" size="icon-sm"
            aria-label="新增组织" title="新增组织" onClick={() => setEditor({ kind: "organization" })}><Plus /></Button>}</div>
          <label className="directory-tree-filter"><span id="directory-hide-archived-label">隐藏已归档</span><Switch
            aria-label="隐藏已归档" aria-labelledby="directory-hide-archived-label"
            checked={hideArchived} onCheckedChange={(checked) => {
              if (confirmDirectoryLeave()) setHideArchived(Boolean(checked))
            }} /></label>
          {visibleOrganizations.map((org) => <div key={org.id}>
            <button type="button" className={cn("directory-tree-node", effectiveOrgId === org.id && !effectiveUnitId && "selected")}
              onClick={() => selectOrganization(org.id)} title={org.name}><Building2 /><span data-no-localize>{org.name}</span>
              {org.status !== "active" && <Badge variant="outline">{statusNames[managedDirectoryStatus(org.status)]}</Badge>}
            </button>
            {effectiveOrgId === org.id && visibleUnits.filter((unit) => unit.kind === "department").map((unit) => <div key={unit.id}>
              <button type="button" className={cn("directory-tree-node directory-tree-department", effectiveUnitId === unit.id && "selected")}
                onClick={() => selectUnit(unit.id)} title={unit.name}><Users /><span data-no-localize>{unit.name}</span>
                {unit.status !== "active" && <Badge variant="outline">{statusNames[managedDirectoryStatus(unit.status)]}</Badge>}
              </button>
              {visibleUnits.filter((team) => team.parent_unit_id === unit.id).map((team) => <button type="button" key={team.id}
                className={cn("directory-tree-node directory-tree-team", effectiveUnitId === team.id && "selected")}
                onClick={() => selectUnit(team.id)} title={team.name}><UserRound /><span data-no-localize>{team.name}</span>
                {team.status !== "active" && <Badge variant="outline">{statusNames[managedDirectoryStatus(team.status)]}</Badge>}
              </button>)}
            </div>)}
          </div>)}
          {organizations.data && visibleOrganizations.length === 0 && <Empty><EmptyTitle>暂无组织</EmptyTitle></Empty>}
        </aside>
        <main className="directory-main">
          {error && <Alert><AlertDescription>{error.message}</AlertDescription><Button variant="outline" size="sm" onClick={() => void refresh()}>重试</Button></Alert>}
          {missing ? <Empty><EmptyTitle>当前组织或部门不可访问</EmptyTitle></Empty> : !organization ? <Empty>
            <EmptyTitle>{organizations.isPending ? "正在加载" : "暂无组织"}</EmptyTitle>
            {owner && <EmptyContent><Button onClick={() => setEditor({ kind: "organization" })}><Plus data-icon="inline-start" />创建组织</Button></EmptyContent>}
          </Empty> : <>
            <div className="directory-context-heading"><div><h2 data-no-localize>{selectedUnit?.name ?? organization.name}</h2>
              <span>{selectedUnit?.kind === "team" ? "团队" : selectedUnit ? "部门" : "组织"}</span>
              <Badge variant="outline">{statusNames[managedDirectoryStatus(selectedUnit?.status ?? organization.status)]}</Badge>
              <small data-no-localize>{selectedUnit?.code ?? organization.code}</small>
            </div><div>
              {(owner || selectedUnit?.kind === "team" && editTeams) && <Button variant="outline" size="sm" onClick={() => setEditor({
                kind: selectedUnit?.kind ?? "organization", row: selectedUnit ?? organization,
              })}><Pencil data-icon="inline-start" />编辑</Button>}
              {owner && !selectedUnit && <Button variant="outline" size="sm" onClick={() => setEditor({ kind: "department" })}><Plus data-icon="inline-start" />新增部门</Button>}
              {selectedUnit?.kind === "department" && editTeams &&
                <Button variant="outline" size="sm" onClick={() => setEditor({ kind: "team" })}><Plus data-icon="inline-start" />新增团队</Button>}
            </div></div>
            <Tabs value={tab} onValueChange={(value) => { if (confirmDirectoryLeave()) setTab(String(value)) }}>
              <TabsList><TabsTrigger value="people">人员</TabsTrigger>
                {owner && <TabsTrigger value="administrators">管理员</TabsTrigger>}
                <TabsTrigger value="audit">变更记录</TabsTrigger>
                {owner && <TabsTrigger value="sync">Entra ID</TabsTrigger>}
              </TabsList>
              <TabsContent value="people">
                <div className="directory-toolbar"><Input aria-label="搜索人员" placeholder="搜索人员" value={query}
                  onChange={(event) => setQuery(event.target.value)} />
                  <MenuSelect label="人员状态" value={status} items={[{ value: "all", label: "全部状态" },
                    ...Object.entries(statusNames).map(([value, label]) => ({ value, label }))]}
                    onChange={(value) => setStatus(value as ManagedDirectoryStatus | "all")} />
                  <span className="directory-count">{people.data?.total ?? "--"} 人</span>
                  {!selectedUnit && owner && <Button variant="outline" size="sm" onClick={() => setEditor({ kind: "person" })}><Plus data-icon="inline-start" />新增人员</Button>}
                  {selectedUnit && editPeople && <Button variant="outline" size="sm" onClick={() => setAddingMembers(true)}><Plus data-icon="inline-start" />添加成员</Button>}
                </div>
                <div className="directory-table-scroll"><ResizableTable className="directory-table directory-people-table" minWidths={peopleColumnMinWidths}>
                  <caption className="sr-only">组织人员</caption><thead><tr>
                    <th><span>人员</span></th><th><span>部门 / 团队</span></th><th><span>菜单权限组</span></th>
                    <th><span>账号</span></th><th><span>来源</span></th><th><span>状态</span></th><th><span>操作</span></th>
                  </tr></thead>
                  <tbody>{(people.data?.items ?? []).map((person) => <tr key={person.id}>
                    <td><strong data-no-localize>{person.display_name}</strong><small data-no-localize>{person.governance_user_id}</small>
                      {person.employee_number && <small data-no-localize>{person.employee_number}</small>}</td>
                    <td><span data-no-localize>{person.department_name ?? "--"}</span><small data-no-localize>{person.team_ids.map((id) => units.data?.find((unit) => unit.id === id)?.name ?? id).join("、") || "--"}</small></td>
                    <td><MenuGroupTags groups={person.scope_menu_permission_groups ?? ["user"]}
                      scopes={person.scope_menu_administrator_scopes} /></td>
                    <td><span data-no-localize>{person.account_email ?? "--"}</span><small>{person.app_user_id ? person.account_enabled ? "启用" : "停用" : "未关联"}</small></td>
                    <td><Badge variant="outline">{person.externally_managed ? "Entra ID" : "本地"}</Badge></td>
                    <td><Badge variant={person.status === "active" && !person.source_disabled ? "secondary" : "outline"}>
                      {person.source_disabled ? "来源已停用" : statusNames[managedDirectoryStatus(person.status)]}</Badge></td>
                    <td>{(owner || (person.department_id && capabilities?.department_capabilities?.[person.department_id]?.includes("directory.edit_people"))) && <DropdownMenu><DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" className="directory-person-trigger"
                      aria-label={`操作 ${person.display_name}`} title="人员操作" />}><Ellipsis /></DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="directory-person-menu"><DropdownMenuGroup>
                        <DropdownMenuItem onClick={() => setEditor({ kind: "person", row: person })}><Pencil />编辑</DropdownMenuItem>
                        <DropdownMenuItem onClick={() => setAction({ person, action: "teams" })}><Users />团队成员</DropdownMenuItem>
                        {owner && <>
                          <DropdownMenuItem onClick={() => setAction({ person, action: "link" })}><Link2 />关联账号</DropdownMenuItem>
                          <DropdownMenuItem onClick={() => setAction({ person, action: "status" })}><Shield />人员状态</DropdownMenuItem>
                        </>}
                      </DropdownMenuGroup></DropdownMenuContent>
                    </DropdownMenu>}</td>
                  </tr>)}</tbody>
                </ResizableTable></div>
                {people.isPending && <Empty><EmptyTitle>正在加载</EmptyTitle></Empty>}
                {people.data?.total === 0 && <Empty><EmptyTitle>暂无人员</EmptyTitle></Empty>}
                <div className="directory-pagination"><span>{cursor ? Number(cursor) + 1 : people.data?.total ? 1 : 0} - {Number(cursor || 0) + (people.data?.items.length ?? 0)}</span>
                  <Button variant="outline" size="icon-sm" aria-label="上一页" title="上一页" disabled={previousCursors.length === 0}
                    onClick={() => { setCursor(previousCursors.at(-1) ?? ""); setPreviousCursors((current) => current.slice(0, -1)) }}><ChevronLeft /></Button>
                  <Button variant="outline" size="icon-sm" aria-label="下一页" title="下一页" disabled={!people.data?.next_cursor}
                    onClick={() => { setPreviousCursors((current) => [...current, cursor]); setCursor(people.data?.next_cursor ?? "") }}><ChevronRight /></Button>
                </div>
              </TabsContent>
              {owner && <TabsContent value="administrators"><ScopeAdministrators key={selectedUnit?.id ?? organization.id}
                scope={selectedUnit ?? organization} kind={selectedUnit?.kind ?? "organization"}
                scopeKey={scope.join(":")} owner={owner} onSaved={() => void changed()} /></TabsContent>}
              {owner && tab === "sync" && <TabsContent value="sync"><DirectorySyncPanel
                key={effectiveOrgId} organization={organization} units={units.data ?? []}
                capabilities={capabilities} onSaved={changed} /></TabsContent>}
              <TabsContent value="audit"><div className="directory-audit-list">{(audit.data ?? []).map((event) => <div key={event.id}>
                <span>{actionNames[event.action] ?? event.action}</span><time data-no-localize>{new Intl.DateTimeFormat(getIntlLocale(), {
                  dateStyle: "medium", timeStyle: "short", timeZone: timezone,
                }).format(new Date(event.changed_at))}</time><strong data-no-localize>{event.actor_label}</strong>
                <small>{event.reason || event.source}</small>
              </div>)}</div>{audit.data?.length === 0 && <Empty><EmptyTitle>暂无变更记录</EmptyTitle></Empty>}</TabsContent>
            </Tabs>
          </>}
        </main>
      </div>}
    {editor && <DirectoryEditor key={`${editor.kind}-${editor.row?.id ?? "new"}`} editor={editor}
      organizationId={effectiveOrgId} departmentId={departmentId} units={units.data ?? []}
      onClose={() => setEditor(null)} onSaved={(id) => {
        if (editor.kind === "organization" && !editor.row) { setOrganizationId(id); setUnitId("") }
        if (editor.kind === "department" && !editor.row) setUnitId(id)
        void changed()
      }} />}
    {action && <PersonActionDialog key={`${action.person.id}-${action.action}`} {...action}
      units={units.data ?? []} onClose={() => setAction(null)} onSaved={() => void changed()} />}
    {addingMembers && selectedUnit && <AddMembersDialog key={selectedUnit.id} unit={selectedUnit}
      scopeKey={scope.join(":")} onClose={() => setAddingMembers(false)} onSaved={() => void changed()} />}
  </div>
}
