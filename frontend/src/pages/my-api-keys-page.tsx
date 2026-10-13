import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Clipboard, Download, Eye, KeyRound, Plus, RefreshCw, ShieldAlert, Trash2 } from "lucide-react"

import { ApiError } from "../api/client"
import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { ResizableTable } from "../components/ui/resizable-table"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select"
import { userManagementApi, type AdminUser } from "../data-sources/apim/user-management"
import { userApiKeyQueries, userApiKeysApi, type ApiKeySecret, type UserSubscription } from "../data-sources/apim/user-api-keys"
import { useAuth } from "../providers/auth-provider"

const ALL_USERS = "__all_users__"
const ALL_DEPARTMENTS = "__all_departments__"

function errorText(error: unknown) {
  if (error instanceof ApiError && error.status === 403) return "当前账号没有执行此操作的权限。"
  return error instanceof Error ? error.message : String(error)
}

function operationIsTerminal(status: string | undefined) {
  return status === "succeeded" || status === "failed" || status === "restored"
}

function requireConfirmation(subscription: UserSubscription, action: string) {
  return window.prompt(`确认${action}。请输入 APIM Subscription ID：\n${subscription.apim_subscription_id}`)
    === subscription.apim_subscription_id
}

export function MyApiKeysPage() {
  const { user, can } = useAuth()
  const queryClient = useQueryClient()
  const canManageAll = can("keys.manage.all")
  const canManageOwn = can("keys.manage.own")
  const [selectedUserId, setSelectedUserId] = useState(user?.id ?? "")
  const [selectedDepartmentId, setSelectedDepartmentId] = useState(ALL_DEPARTMENTS)
  const [displayName, setDisplayName] = useState("")
  const [applicationType, setApplicationType] = useState<"service" | "agent">("service")
  const [operationId, setOperationId] = useState<string | null>(null)
  const [operationTargetUserId, setOperationTargetUserId] = useState<string | null>(null)
  const [revealed, setRevealed] = useState<(ApiKeySecret & { subscriptionId: string }) | null>(null)
  const [revealing, setRevealing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const autoRevealAttempted = useRef<string | null>(null)
  const activeUserId = canManageAll
    ? selectedUserId === ALL_USERS ? "" : selectedUserId
    : (user?.id ?? "")

  useEffect(() => {
    if (user?.id && !selectedUserId) setSelectedUserId(user.id)
  }, [selectedUserId, user?.id])

  const users = useQuery({
    queryKey: ["admin-users", ""],
    queryFn: () => userManagementApi.listUsers(""),
    enabled: canManageAll,
    retry: false,
  })
  const departments = useQuery({
    queryKey: ["admin-organization-units"],
    queryFn: userManagementApi.listOrganizationUnits,
    enabled: canManageAll,
    retry: false,
  })
  const subscriptions = useQuery({
    queryKey: userApiKeyQueries.subscriptions(activeUserId),
    queryFn: () => canManageAll
      ? userApiKeysApi.listUser(activeUserId)
      : userApiKeysApi.listMine(),
    enabled: Boolean(activeUserId),
    retry: false,
  })
  const operation = useQuery({
    queryKey: userApiKeyQueries.operation(operationId),
    queryFn: () => userApiKeysApi.operation(operationId!),
    enabled: Boolean(operationId),
    refetchInterval: (query) => query.state.data?.worker_available === false
      || operationIsTerminal(query.state.data?.status) ? false : 2_000,
    refetchIntervalInBackground: true,
    retry: false,
  })

  const create = useMutation({
    mutationFn: () => {
      const name = displayName.trim()
      if (!name) throw new Error("请先填写 Key 名称。")
      const body = { display_name: name, application_type: applicationType }
      return canManageAll
        ? userApiKeysApi.createForUser(activeUserId, body)
        : userApiKeysApi.createMine(body)
    },
    onSuccess: (accepted) => {
      setActionError(null)
      setRevealed(null)
      autoRevealAttempted.current = null
      setOperationId(accepted.operation.id)
      setOperationTargetUserId(activeUserId)
      setDisplayName("")
    },
    onError: (error) => setActionError(errorText(error)),
  })
  async function revealKey(subscription: UserSubscription, kind: "primary" | "secondary") {
    setRevealing(true)
    setActionError(null)
    try {
      // Do not put secret responses into TanStack Query or mutation caches.
      const secret = await userApiKeysApi.reveal(subscription.application_id, subscription.id, kind)
      setRevealed({ ...secret, subscriptionId: subscription.apim_subscription_id })
    } catch (error) {
      setActionError(errorText(error))
    } finally {
      setRevealing(false)
    }
  }
  const rotate = useMutation({
    mutationFn: async ({ subscription, kind }: { subscription: UserSubscription; kind: "primary" | "secondary" }) => {
      if (!requireConfirmation(subscription, `轮换 ${kind} Key`)) throw new Error("确认内容不匹配，未执行轮换。")
      await userApiKeysApi.rotate(subscription.application_id, subscription.id, kind, subscription.apim_subscription_id)
      const secret = await userApiKeysApi.reveal(subscription.application_id, subscription.id, kind)
      // Keep only the displayed one-time value in component state, never in mutation data.
      setRevealed({ ...secret, subscriptionId: subscription.apim_subscription_id })
    },
    onSuccess: () => {
      setActionError(null)
      void queryClient.invalidateQueries({ queryKey: userApiKeyQueries.subscriptions(activeUserId) })
    },
    onError: (error) => setActionError(errorText(error)),
  })
  const cancel = useMutation({
    mutationFn: async (subscription: UserSubscription) => {
      if (!requireConfirmation(subscription, "停用此 Subscription")) throw new Error("确认内容不匹配，未执行停用。")
      return userApiKeysApi.cancel(subscription.application_id, subscription.id, subscription.apim_subscription_id)
    },
    onSuccess: () => {
      setRevealed(null)
      setActionError(null)
      void queryClient.invalidateQueries({ queryKey: userApiKeyQueries.subscriptions(activeUserId) })
    },
    onError: (error) => setActionError(errorText(error)),
  })
  const exportKeys = useMutation({
    mutationFn: async () => {
      const blob = await userApiKeysApi.export({
        user_ids: activeUserId ? [activeUserId] : [],
        ...(selectedDepartmentId !== ALL_DEPARTMENTS ? { department_id: selectedDepartmentId } : {}),
      })
      const url = URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = url
      link.download = "turnstile-apim-keys.csv"
      link.click()
      URL.revokeObjectURL(url)
    },
    onError: (error) => setActionError(errorText(error)),
  })

  useEffect(() => {
    const current = operation.data
    if (!operationId || current?.id !== operationId || current.status !== "succeeded") return
    void queryClient.invalidateQueries({ queryKey: userApiKeyQueries.subscriptions(operationTargetUserId) })
    if (autoRevealAttempted.current === operationId) return
    autoRevealAttempted.current = operationId
    const applicationId = current.semantic_preview.application_id
    const subscriptionRecordId = current.semantic_preview.application_subscription_id
    if (typeof applicationId !== "string" || typeof subscriptionRecordId !== "string") return
    const apimId = String(current.semantic_preview.apim_subscription_id ?? "")
    void userApiKeysApi.reveal(applicationId, subscriptionRecordId, "primary")
      .then((secret) => setRevealed({ ...secret, subscriptionId: apimId }))
      .catch((error: unknown) => setActionError(errorText(error)))
  }, [applicationType, displayName, operation.data, operationId, operationTargetUserId, queryClient])

  const terminal = operationIsTerminal(operation.data?.status)
  const provisioning = Boolean(operationId) && !terminal && !operation.isError
  const busy = create.isPending || provisioning || revealing || rotate.isPending || cancel.isPending || exportKeys.isPending
  const selectedUser = users.data?.find((item) => item.id === activeUserId)

  return (
    <div className="user-api-keys-page">
      <header className="user-api-keys-header">
        <div><span className="user-api-keys-icon"><KeyRound size={18} /></span><div><h1>我的 API Key</h1><p>Key 由 APIM 创建和校验；Turnstile 只按需揭示，不在本地保存 Key 值。</p></div></div>
        {canManageAll && <Button type="button" variant="outline" disabled={exportKeys.isPending} onClick={() => exportKeys.mutate()}><Download size={15} />{exportKeys.isPending ? "正在导出…" : "导出所选用户 Key"}</Button>}
      </header>

      {canManageAll && <section className="user-api-keys-admin card">
        <div><b>管理员管理</b><small>可为用户创建 Key，并导出 APIM 当前主、次 Key。导出文件只在本次响应中生成。</small></div>
        <label><span>选择用户</span><Select value={selectedUserId} onValueChange={(value) => { if (value) { setSelectedUserId(value); setRevealed(null) } }}>
          <SelectTrigger aria-label="选择要管理 Key 的用户"><SelectValue /></SelectTrigger><SelectContent><SelectItem value={ALL_USERS}>全部用户（仅用于导出）</SelectItem>{(users.data ?? []).map((item: AdminUser) => <SelectItem key={item.id} value={item.id}>{item.display_name || item.email} · {item.role}{item.enabled ? "" : "（已停用）"}</SelectItem>)}</SelectContent>
        </Select></label>
        <label><span>导出部门</span><Select value={selectedDepartmentId} onValueChange={(value) => { if (value) setSelectedDepartmentId(value) }}>
          <SelectTrigger aria-label="按部门筛选 Key 导出"><SelectValue /></SelectTrigger><SelectContent><SelectItem value={ALL_DEPARTMENTS}>全部部门</SelectItem>{(departments.data ?? []).filter((item) => item.enabled).map((item) => <SelectItem key={item.id} value={item.id}>{item.display_name}</SelectItem>)}</SelectContent>
        </Select></label>
        {users.isError && <span className="user-api-keys-error">{errorText(users.error)}</span>}
      </section>}

      <section className="user-api-keys-create card">
        <div className="user-api-keys-section-heading"><div><h2>创建 APIM Key</h2><p>{selectedUser?.email ?? user?.email ?? "当前用户"}</p><p>该用户的 Key 会关联到其 Turnstile 用量归属。</p></div></div>
        <form onSubmit={(event) => { event.preventDefault(); create.mutate() }}>
          <label><span>Key 名称</span><Input required maxLength={100} value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="例如：报表自动化服务" /></label>
          <label><span>用途类型</span><Select value={applicationType} onValueChange={(value) => { if (value === "service" || value === "agent") setApplicationType(value) }}><SelectTrigger aria-label="Key 用途类型"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="service">服务</SelectItem><SelectItem value="agent">智能体</SelectItem></SelectContent></Select></label>
          <Button type="submit" disabled={busy || !activeUserId || selectedUser?.enabled === false}><Plus size={15} />{create.isPending ? "正在创建" : "创建并获取 Primary Key"}</Button>
        </form>
      </section>

      {operationId && <section className={`user-api-key-operation ${operation.data?.status ?? "queued"}`} role="status">
        <RefreshCw size={15} className={!terminal && operation.data?.worker_available !== false ? "spin" : ""} />
        <span>{operation.isError ? errorText(operation.error) : operation.data?.error_message ?? `创建操作：${operation.data?.status ?? "等待控制平面处理"}`}</span>
        {operation.data?.worker_available === false && <small>发布工作进程未部署；操作仍在队列中，Primary Key 暂不可揭示。</small>}
      </section>}

      {revealed && <section className="user-api-key-secret" role="status">
        <div><b>{revealed.key_kind === "primary" ? "Primary" : "Secondary"} Key 已揭示</b><small>请立即复制到调用方的安全配置中。关闭或刷新页面后需再次通过 APIM 揭示。</small></div>
        <code data-no-localize>{revealed.value}</code>
        <div className="user-api-key-secret-actions"><Button type="button" variant="outline" onClick={() => void navigator.clipboard.writeText(revealed.value)}><Clipboard size={14} />复制</Button><Button type="button" variant="ghost" onClick={() => setRevealed(null)}>隐藏</Button></div>
      </section>}

      {(actionError || create.error || operation.data?.status === "failed") && <div className="user-api-keys-error" role="alert">{actionError ?? (create.error ? errorText(create.error) : operation.data?.error_message)}</div>}

      <section className="user-api-keys-list card">
        <div className="user-api-keys-section-heading"><div><h2>{canManageAll ? `${selectedUser?.display_name || selectedUser?.email || "所选用户"} 的 Key` : "已创建的 Key"}</h2><p>只显示 APIM Subscription 元数据；如需再次使用密钥，请显式揭示。</p></div><Button type="button" variant="outline" disabled={subscriptions.isFetching} onClick={() => void subscriptions.refetch()}><RefreshCw size={14} className={subscriptions.isFetching ? "spin" : ""} />刷新</Button></div>
        {subscriptions.isLoading && <p className="user-api-keys-empty">正在加载 Key 目录…</p>}
        {subscriptions.isError && <p className="user-api-keys-error">{errorText(subscriptions.error)}</p>}
        {subscriptions.data?.items.length === 0 && <p className="user-api-keys-empty">还没有 API Key。创建后，APIM 会异步配置对应的 Subscription。</p>}
        {!!subscriptions.data?.items.length && <div className="user-api-keys-table-wrap"><ResizableTable minWidths={[200, 240, 160, 160, 360]}><thead><tr><th>名称</th><th>APIM Subscription</th><th>类型与状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>
          {subscriptions.data.items.map((item) => <tr key={item.id}>
            <td><b>{item.display_name}</b><small>{item.application_display_name}</small></td>
            <td><code data-no-localize>{item.apim_subscription_id}</code></td>
            <td><span>{item.application_type === "agent" ? "智能体" : "服务"}</span><small>{item.state === "active" ? "活动" : item.state === "suspended" ? "已暂停" : "已停用"}</small></td>
            <td>{new Date(item.created_at).toLocaleString()}</td>
            <td><div className="user-api-keys-actions">
              <Button type="button" size="sm" variant="outline" disabled={busy || item.state !== "active"} onClick={() => void revealKey(item, "primary")}><Eye size={13} />揭示主 Key</Button>
              <Button type="button" size="sm" variant="outline" disabled={busy || item.state !== "active"} onClick={() => rotate.mutate({ subscription: item, kind: "secondary" })}><RefreshCw size={13} />轮换备用 Key</Button>
              {(canManageOwn || canManageAll) && <Button type="button" size="sm" variant="destructive" disabled={busy || item.state === "cancelled"} onClick={() => cancel.mutate(item)}><Trash2 size={13} />停用</Button>}
            </div></td>
          </tr>)}
        </tbody></ResizableTable></div>}
      </section>

      <aside className="user-api-keys-note"><ShieldAlert size={15} /><span>生产调用请使用 <code>Ocp-Apim-Subscription-Key</code> 请求头。轮换备用 Key 后先更新调用方，再轮换旧 Key，避免停机。</span></aside>
    </div>
  )
}
