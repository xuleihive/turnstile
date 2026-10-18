import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { LockKeyhole, PanelLeft, RefreshCw, Save, Search, Undo2 } from "lucide-react"
import { permissionApi, type PermissionConfiguration } from "../api/permissions"
import { menuPermissionGroupNames, type MenuPermissionGroup } from "../api/auth"
import { Button } from "../components/ui/button"
import { Checkbox } from "../components/ui/checkbox"
import { Input } from "../components/ui/input"
import { Field, FieldGroup, FieldLabel } from "../components/ui/field"
import { Badge } from "../components/ui/badge"
import { Alert, AlertDescription } from "../components/ui/alert"
import { confirmDirectoryLeave, useDirectoryLeaveGuard } from "../components/directory-controls"
import { useAuth } from "../providers/auth-provider"
import { ResizableTable } from "../components/ui/resizable-table"

const roles: MenuPermissionGroup[] = ["organization_admin", "department_admin", "team_admin", "user"]
const scopes = ["所属组织", "所属部门", "所属团队", "本人"]

function PermissionEditor({ initial, onReload }: {
  initial: PermissionConfiguration; onReload: () => void
}) {
  const client = useQueryClient()
  const { refreshProfile } = useAuth()
  const [groups, setGroups] = useState(initial.groups)
  const [search, setSearch] = useState("")
  const dirty = JSON.stringify(groups) !== JSON.stringify(initial.groups)
  useDirectoryLeaveGuard(dirty)
  const save = useMutation({
    mutationFn: () => permissionApi.save({ groups, expected_revision: initial.revision }),
    onSuccess: async (value) => {
      client.setQueryData(["permission-configuration"], value)
      await refreshProfile()
    },
  })
  const entries = initial.catalog.filter((row) =>
    `${row.label} ${row.module}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  const modules = [...new Set(entries.map((row) => row.module))]
  const change = (role: MenuPermissionGroup, ids: string[], checked: boolean) => {
    setGroups((previous) => ({
      ...previous,
      [role]: initial.catalog.filter((row) => checked && ids.includes(row.id)
        || previous[role].includes(row.id) && (!ids.includes(row.id) || checked))
        .map((row) => row.id),
    }))
    save.reset()
  }
  return <>
    <div className="permission-toolbar">
      <FieldGroup><Field><FieldLabel htmlFor="permission-search" className="sr-only">搜索菜单</FieldLabel>
        <Input id="permission-search" value={search} onChange={(event) => setSearch(event.target.value)}
          placeholder="搜索菜单" />
      </Field></FieldGroup>
      <Search aria-hidden size={16} />
      <Button variant="ghost" size="icon-sm" aria-label="恢复已保存配置" title="恢复已保存配置"
        disabled={!dirty || save.isPending} onClick={() => { setGroups(initial.groups); save.reset() }}>
        <Undo2 />
      </Button>
      <Button variant="ghost" size="icon-sm" aria-label="重新加载" title="重新加载"
        disabled={save.isPending} onClick={() => { if (confirmDirectoryLeave()) onReload() }}>
        <RefreshCw />
      </Button>
    </div>
    {save.error && <Alert><AlertDescription>
      {save.error.message}
    </AlertDescription></Alert>}
    <div className="permission-matrix-scroll">
      <ResizableTable className="permission-matrix" minWidths={[180, 160, 160, 160, 160]}>
        <caption className="sr-only">菜单权限配置</caption>
        <thead><tr><th scope="col">菜单功能</th>{roles.map((role, index) =>
          <th scope="col" key={role}>{menuPermissionGroupNames[role]}
            <Badge variant="outline">{scopes[index]}</Badge>
          </th>)}</tr></thead>
        <tbody>{modules.map((module) => {
          const rows = entries.filter((row) => row.module === module)
          const ids = rows.filter((row) => !row.owner_only && !row.required).map((row) => row.id)
          return <ModuleRows key={module} module={module} rows={rows} roles={roles} groups={groups}
            ids={ids} disabled={save.isPending} change={change} />
        })}</tbody>
      </ResizableTable>
    </div>
    <div className="permission-save-actions">
      <Badge variant="secondary" role="status">{dirty ? "未保存" : "已保存"}</Badge>
      <Button size="sm" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
        <Save data-icon="inline-start" />保存
      </Button>
    </div>
  </>
}

function ModuleRows({ module, rows, roles: roleList, groups, ids, disabled, change }: {
  module: string; rows: PermissionConfiguration["catalog"]; roles: MenuPermissionGroup[]
  groups: PermissionConfiguration["groups"]; ids: string[]; disabled: boolean
  change: (role: MenuPermissionGroup, ids: string[], checked: boolean) => void
}) {
  return <>
    <tr className="permission-module"><th scope="row">{module}</th>{roleList.map((role) => {
      const count = ids.filter((id) => groups[role].includes(id)).length
      return <td key={role}><Checkbox checked={ids.length > 0 && count === ids.length}
        indeterminate={count > 0 && count < ids.length} disabled={disabled || !ids.length}
        aria-label={`${menuPermissionGroupNames[role]} ${module}`}
        onCheckedChange={(checked) => change(role, ids, checked)} /></td>
    })}</tr>
    {rows.map((row) => <tr key={row.id}><th scope="row">{row.label}</th>{roleList.map((role) =>
      <td key={role}>{row.owner_only ? <span className="permission-lock" title="仅平台管理员">
        <LockKeyhole size={16} aria-label="仅平台管理员" />
      </span> : <Checkbox
        checked={groups[role].includes(row.id)} disabled={disabled || row.required}
        aria-label={`${menuPermissionGroupNames[role]} ${row.label}`}
        onCheckedChange={(checked) => change(role, [row.id], checked)} />}</td>)}</tr>)}
  </>
}

export function PermissionManagementPage({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const query = useQuery({ queryKey: ["permission-configuration"], queryFn: permissionApi.read,
    staleTime: 0, refetchOnWindowFocus: false })
  return <section className="permission-workspace">
    <header className="permission-header">
      <Button variant="ghost" size="icon-sm" aria-label="切换侧边栏" title="切换侧边栏" onClick={onToggleSidebar}>
        <PanelLeft />
      </Button>
      <h1>权限管理</h1>
    </header>
    {query.isPending ? <div className="permission-loading" role="status">加载中</div> : query.error ?
      <Alert><AlertDescription>{query.error.message}</AlertDescription></Alert> :
      query.data && <PermissionEditor key={`${query.data.revision}-${query.dataUpdatedAt}`}
        initial={query.data} onReload={() => { void query.refetch() }} />}
  </section>
}
