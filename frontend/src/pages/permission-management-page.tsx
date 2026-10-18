import { useId, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ChevronDown, LockKeyhole, PanelLeft, RefreshCw, Save, Search, Undo2 } from "lucide-react"
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

function RoleHeader({ downloads = false }: { downloads?: boolean }) {
  return <thead><tr><th scope="col">{downloads ? "下载功能" : "菜单功能"}</th>
    {roles.map((role, index) => <th scope="col" key={role}>{menuPermissionGroupNames[role]}
      {!downloads && <Badge variant="outline">{scopes[index]}</Badge>}
    </th>)}
  </tr></thead>
}

function PermissionEditor({ initial, onReload }: {
  initial: PermissionConfiguration; onReload: () => void
}) {
  const client = useQueryClient()
  const { refreshProfile } = useAuth()
  const [groups, setGroups] = useState(initial.groups)
  const [downloads, setDownloads] = useState(initial.download_groups)
  const [search, setSearch] = useState("")
  const dirty = JSON.stringify(groups) !== JSON.stringify(initial.groups)
    || JSON.stringify(downloads) !== JSON.stringify(initial.download_groups)
  useDirectoryLeaveGuard(dirty)
  const save = useMutation({
    mutationFn: () => permissionApi.save({ groups,
      ...(initial.download_policy_available === false ? {} : { download_groups: downloads }),
      expected_revision: initial.revision }),
    onSuccess: async (value) => {
      client.setQueryData(["permission-configuration"], value)
      await refreshProfile()
    },
  })
  const term = search.trim().toLocaleLowerCase()
  const entries = initial.catalog.filter((row) =>
    `${row.label} ${row.module}`.toLocaleLowerCase().includes(term))
  const modules = [...new Set(entries.map((row) => row.module))]
  const downloadRows = initial.download_catalog.filter((row) =>
    row.label.toLocaleLowerCase().includes(term))
    .map((row) => ({ ...row, module: "插件下载", source: "all", owner_only: false, required: false }))
  const change = (role: MenuPermissionGroup, ids: string[], checked: boolean) => {
    setGroups((previous) => ({ ...previous,
      [role]: initial.catalog.filter((row) => checked && ids.includes(row.id)
        || previous[role].includes(row.id) && (!ids.includes(row.id) || checked)).map((row) => row.id),
    }))
    save.reset()
  }
  const changeDownloads = (role: MenuPermissionGroup, ids: string[], checked: boolean) => {
    setDownloads((previous) => ({ ...previous,
      [role]: initial.download_catalog.filter((row) => checked && ids.includes(row.id)
        || previous[role].includes(row.id) && (!ids.includes(row.id) || checked)).map((row) => row.id),
    }))
    save.reset()
  }
  return <>
    <div className="permission-toolbar">
      <FieldGroup><Field><FieldLabel htmlFor="permission-search" className="sr-only">搜索权限</FieldLabel>
        <Input id="permission-search" value={search} onChange={(event) => setSearch(event.target.value)}
          placeholder="搜索权限" />
      </Field></FieldGroup>
      <Search aria-hidden size={16} />
      <Button variant="ghost" size="icon-sm" aria-label="恢复已保存配置" title="恢复已保存配置"
        disabled={!dirty || save.isPending} onClick={() => {
          setGroups(initial.groups); setDownloads(initial.download_groups); save.reset()
        }}><Undo2 /></Button>
      <Button variant="ghost" size="icon-sm" aria-label="重新加载" title="重新加载"
        disabled={save.isPending} onClick={() => { if (confirmDirectoryLeave()) onReload() }}>
        <RefreshCw />
      </Button>
    </div>
    {save.error && <Alert><AlertDescription>{save.error.message}</AlertDescription></Alert>}
    <div className="permission-matrix-scroll">
      <details className="permission-section" open>
        <summary><ChevronDown aria-hidden size={16} /><span>菜单管理</span>
          <Badge variant="secondary">{entries.length}</Badge></summary>
        <p className="permission-description">配置各角色可访问的菜单。数据范围仍由组织任命和成员关系决定。</p>
        <ResizableTable className="permission-matrix" minWidths={[180, 160, 160, 160, 160]}>
          <caption className="sr-only">菜单权限配置</caption>
          <RoleHeader />
          {modules.map((module) => {
            const rows = entries.filter((row) => row.module === module)
            const ids = rows.filter((row) => !row.owner_only && !row.required).map((row) => row.id)
            return <ModuleRows key={module} module={module} rows={rows} groups={groups} ids={ids}
              disabled={save.isPending} searching={Boolean(term)} change={change} />
          })}
        </ResizableTable>
      </details>
      <details className="permission-section" open>
        <summary><ChevronDown aria-hidden size={16} /><span>下载权限</span>
          <Badge variant="secondary">{downloadRows.length}</Badge></summary>
        <p className="permission-description">分别控制两版插件的可见性和下载。非平台管理员仅可导出本人拥有的应用订阅，不授予其他订阅密钥或模型权限。</p>
        {initial.download_policy_available === false && <p className="permission-description" role="status">下载权限管理需要启用数据库目录。</p>}
        <ResizableTable className="permission-matrix" minWidths={[180, 160, 160, 160, 160]}>
          <caption className="sr-only">插件下载权限配置</caption>
          <RoleHeader downloads />
          <ModuleRows module="插件下载" rows={downloadRows} groups={downloads}
            ids={downloadRows.map((row) => row.id)}
            disabled={save.isPending || initial.download_policy_available === false}
            searching={Boolean(term)} change={changeDownloads} />
        </ResizableTable>
      </details>
      <p className="permission-description">平台管理员始终具备全部权限。折叠和搜索不会修改已配置的权限。</p>
    </div>
    <div className="permission-save-actions">
      <Badge variant="secondary" role="status">{dirty ? "未保存" : "已保存"}</Badge>
      <Button size="sm" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
        <Save data-icon="inline-start" />保存
      </Button>
    </div>
  </>
}

function ModuleRows({ module, rows, groups, ids, disabled, searching, change }: {
  module: string; rows: PermissionConfiguration["catalog"]
  groups: PermissionConfiguration["groups"]; ids: string[]; disabled: boolean; searching: boolean
  change: (role: MenuPermissionGroup, ids: string[], checked: boolean) => void
}) {
  const [collapsed, setCollapsed] = useState(module !== "插件下载")
  const rowsId = useId()
  const expanded = searching || !collapsed
  return <>
    <tbody><tr className="permission-module"><th scope="row">
      <Button variant="ghost" size="sm" aria-expanded={expanded} aria-controls={rowsId}
        aria-label={`${expanded ? "折叠" : "展开"}${module}`} disabled={searching}
        onClick={() => setCollapsed((previous) => !previous)}>
        <ChevronDown data-icon="inline-start" className={expanded ? "" : "permission-chevron-closed"} />{module}
      </Button>
    </th>{roles.map((role) => {
      const count = ids.filter((id) => groups[role].includes(id)).length
      return <td key={role}><Checkbox checked={ids.length > 0 && count === ids.length}
        indeterminate={count > 0 && count < ids.length} disabled={disabled || !ids.length}
        aria-label={`${menuPermissionGroupNames[role]} ${module}`}
        onCheckedChange={(checked) => change(role, ids, checked)} /></td>
    })}</tr></tbody>
    <tbody id={rowsId} hidden={!expanded}>
      {rows.map((row) => <tr key={row.id}><th scope="row">{row.label}</th>{roles.map((role) =>
        <td key={role}>{row.owner_only ? <span className="permission-lock" title="仅平台管理员">
          <LockKeyhole size={16} aria-label="仅平台管理员" />
        </span> : <Checkbox checked={groups[role].includes(row.id)} disabled={disabled || row.required}
          aria-label={`${menuPermissionGroupNames[role]} ${row.label}`}
          onCheckedChange={(checked) => change(role, [row.id], checked)} />}</td>)}</tr>)}
    </tbody>
  </>
}

export function PermissionManagementPage({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const query = useQuery({ queryKey: ["permission-configuration"], queryFn: permissionApi.read,
    staleTime: 0, refetchOnWindowFocus: false,
    // The editor remounts on dataUpdatedAt; automatic reads would discard unsaved changes.
    meta: { automaticRefresh: false } })
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
