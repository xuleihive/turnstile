import { type FormEvent, useEffect, useRef, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Download, PanelLeft, RefreshCw, ShieldCheck } from "lucide-react"
import { clientDownloadsApi } from "../api/client-downloads"
import { canDownloadPlugin } from "../api/auth"
import { Button } from "../components/ui/button"
import { Badge } from "../components/ui/badge"
import { MagpieClientDownload } from "../components/magpie-client-download"
import { useAuth } from "../providers/auth-provider"

export function ClientDownloadsPage({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const { user } = useAuth()
  const canDownloadUser = canDownloadPlugin(user, "user-plugin")
  const canDownloadSubscription = canDownloadPlugin(user, "subscription-plugin")
  const query = useQuery({
    queryKey: ["client-downloads", user?.id, user?.directory_permission_revision],
    queryFn: ({ signal }) => clientDownloadsApi.options(signal), enabled: canDownloadSubscription, staleTime: 60_000,
    refetchOnMount: "always",
  })
  const personal = useQuery({
    queryKey: ["client-user-options", user?.id, user?.directory_permission_revision],
    queryFn: ({ signal }) => clientDownloadsApi.userOptions(signal),
    enabled: canDownloadUser, staleTime: 60_000, refetchOnMount: "always",
  })
  // Publish the permitted catalogs together, after this visit's fresh reads, not cached placeholders.
  const catalogLoading = (canDownloadUser && (personal.isPending || personal.isFetchedAfterMount === false))
    || (canDownloadSubscription && (query.isPending || query.isFetchedAfterMount === false))
  const [subscription, setSubscription] = useState("")
  const [keyKind, setKeyKind] = useState<"primary" | "secondary">("primary")
  const [acknowledged, setAcknowledged] = useState(false)
  const [userAcknowledged, setUserAcknowledged] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const activeDownload = useRef<AbortController | null>(null)
  useEffect(() => {
    setBusy(false); setError(""); setNotice("")
    return () => { activeDownload.current?.abort(); activeDownload.current = null }
  }, [user?.id, canDownloadUser, canDownloadSubscription])
  const items = query.data?.items ?? []
  const selected = items.find((item) => item.subscription_id === subscription) ?? items[0]
  async function downloadUser() {
    if (!canDownloadUser || !personal.data?.enabled || !personal.data.budget_configured
      || activeDownload.current || !userAcknowledged) return
    const controller = new AbortController()
    activeDownload.current = controller
    setBusy(true); setError(""); setNotice("")
    try {
      const { blob, filename } = await clientDownloadsApi.downloadUser(controller.signal)
      if (controller.signal.aborted) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = url; link.download = filename
      document.body.append(link); link.click(); link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      setNotice("用户版已下载。安装后自动导入授权和默认订阅；需要更新模型时点击「刷新模型」。")
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "下载失败，请重试。")
    } finally {
      if (activeDownload.current === controller) {
        activeDownload.current = null
        if (!controller.signal.aborted) setBusy(false)
      }
    }
  }
  async function download(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canDownloadSubscription || !selected || activeDownload.current || !acknowledged) return
    const controller = new AbortController()
    activeDownload.current = controller
    setBusy(true); setError(""); setNotice("")
    try {
      const { blob, filename } = await clientDownloadsApi.download(selected, keyKind, controller.signal)
      if (controller.signal.aborted) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = url; link.download = filename
      document.body.append(link); link.click(); link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      setNotice("插件已下载，请解压后在客户端中导入文件夹。")
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "下载失败，请重试。")
    } finally {
      if (activeDownload.current === controller) {
        activeDownload.current = null
        if (!controller.signal.aborted) setBusy(false)
      }
    }
  }
  return <div className="finops-workspace client-downloads-workspace">
    <header className="finops-header"><div>
      <Button variant="ghost" size="icon-sm" className="finops-sidebar-trigger" onClick={onToggleSidebar} aria-label="切换导航栏"><PanelLeft size={16} /></Button>
      <span className="finops-header-icon"><Download size={17} /></span><h1>下载客户端</h1>
    </div><MagpieClientDownload /></header>
    <div className="finops-scroll-region"><div className="client-downloads-content">
      {!canDownloadUser && !canDownloadSubscription && <p role="status">当前角色没有插件下载权限。</p>}
      <div className="client-downloads-panels" aria-busy={catalogLoading}>
      {catalogLoading && <>
        {[canDownloadUser && "AIGateway-Proxy 用户版", canDownloadSubscription && "AIGateway-Proxy 订阅版"].filter(Boolean).map((title) =>
          <section className="client-downloads-card client-downloads-loading" key={String(title)}>
            <h2>{title}</h2><p role="status">正在加载</p>
            <div className="client-downloads-skeleton" aria-hidden="true"><span /><span /><span /><span /></div>
          </section>)}
      </>}
      {!catalogLoading && <>
      {canDownloadUser && <section className="client-downloads-card">
        <div className="client-downloads-heading"><span className="client-downloads-edition-icon"><ShieldCheck size={18} aria-hidden="true" /></span><div><h2>AIGateway-Proxy 用户版</h2><p>使用个人授权和默认订阅，安装后即可使用。</p></div><Badge variant="outline" data-no-localize>{personal.data?.plugin_version ? `v${personal.data.plugin_version}` : "—"}</Badge></div>
        {personal.data?.budget_configured && <div className="client-downloads-body">
          <dl className="client-downloads-facts">
            <div><dt>当前用户</dt><dd data-no-localize>{personal.data?.user_name ?? user?.name ?? user?.email}</dd></div>
            {personal.data?.default_subscription && <div><dt>平台默认订阅</dt><dd data-no-localize>{personal.data.default_subscription.application_name} / {personal.data.default_subscription.subscription_name}</dd></div>}
          </dl>
          <section className="client-downloads-section"><h3>安装与使用</h3>
            <ol className="client-user-steps"><li>解压安装包，在客户端「插件」页面添加文件夹。</li><li>首次安装自动导入授权、默认订阅和模型，无需再次选择订阅。</li><li>点击「刷新模型」更新模型目录；点击「重新登录」更新授权及默认订阅。</li></ol>
            <p className="client-downloads-help">解压目录与 ZIP 文件名一致（不含 .zip）。</p>
          </section>
          <section className="client-downloads-section"><h3>文生图接入</h3><p className="client-downloads-help">文生图通过配套 MCP 工具使用；首次需登记插件生成的 images-mcp.json，不会自动改写 Agent 配置。</p></section>
        </div>}
        {personal.isError && <p role="alert">{personal.error.message}</p>}
        {personal.data && !personal.data.enabled && !personal.data.unavailable_reason && <p role="status">当前平台暂未启用用户版。</p>}
        {personal.data?.unavailable_reason && <p role="alert">{personal.data.unavailable_reason}</p>}
        {personal.data?.budget_configured && <div className="client-downloads-footer">
        <div className="client-downloads-warning" id="client-user-secret-warning"><ShieldCheck size={18} /><p>用户版下载包包含个人登录凭据，不包含 APIM 密钥。请勿分享、上传或提交到仓库。</p></div>
        <label className="client-downloads-consent"><input type="checkbox" checked={userAcknowledged} disabled={busy} aria-describedby="client-user-secret-warning" onChange={event => setUserAcknowledged(event.target.checked)} /><span>我已了解用户版下载包包含个人登录凭据</span></label>
        <Button className="client-downloads-download-button" type="button" onClick={() => void downloadUser()} disabled={busy || !personal.data?.enabled || personal.isError || !userAcknowledged}><Download data-icon="inline-start" />{busy ? "正在生成安装包" : "下载用户版插件"}</Button>
        </div>}
      </section>}
      {canDownloadSubscription && <section className="client-downloads-card">
        <div className="client-downloads-heading"><span className="client-downloads-edition-icon"><Download size={18} aria-hidden="true" /></span><div><h2>AIGateway-Proxy 订阅版</h2><p>使用指定应用订阅与密钥，导入已授权模型。</p></div><Badge variant="outline" data-no-localize>v{query.data?.plugin_version ?? "..."}</Badge></div>
        {query.isPending ? <p role="status">正在加载</p> : query.isError ? <div role="alert"><p>{query.error.message}</p><Button variant="outline" onClick={() => void query.refetch()}><RefreshCw size={16} />重试</Button></div>
          : !items.length ? <p role="status">没有可下载的应用订阅。请联系管理员配置本人应用订阅。</p>
          : <form className="client-downloads-form" action="/api/v1/client-downloads/magpie" method="post" onSubmit={(event) => void download(event)}>
            <div className="client-downloads-body">
            <div className="client-downloads-fields">
              <label htmlFor="client-subscription">应用订阅<select id="client-subscription" name="subscription_id" value={selected?.subscription_id ?? ""} title={selected ? `${selected.application_name} / ${selected.subscription_name}` : undefined} disabled={busy} onChange={(event) => { setSubscription(event.target.value); setAcknowledged(false); setNotice(""); setError("") }} required>
                {items.map((item) => <option data-no-localize key={item.subscription_id} value={item.subscription_id}>{item.application_name} / {item.subscription_name}</option>)}
              </select></label>
              <label htmlFor="client-gateway">平台网关<select id="client-gateway" name="gateway_id" value={selected?.gateway_id ?? ""} title={selected?.gateway_name} disabled={busy} required onChange={() => {}}>
                {selected && <option data-no-localize value={selected.gateway_id}>{selected.gateway_name}</option>}
              </select></label>
              <label htmlFor="client-key-kind">订阅密钥<select id="client-key-kind" name="key_kind" value={keyKind} disabled={busy} onChange={(event) => { setKeyKind(event.target.value as "primary" | "secondary"); setAcknowledged(false) }}><option value="primary">主密钥</option><option value="secondary">备用密钥</option></select></label>
            </div>
            <p className="client-downloads-endpoint" data-no-localize>{selected?.base_url}</p>
            <section className="client-downloads-section"><h3>可用模型</h3>
            {selected?.models.length ? <ul className="client-downloads-models">{selected.models.map((model) => <li key={model.id}><span data-no-localize>{model.name}</span><small data-no-localize>{model.protocol}</small></li>)}</ul> : <p role="status">该订阅没有已发布且获授权的模型。</p>}
            </section>
            </div>
            <div className="client-downloads-footer">
            <div className="client-downloads-warning" id="client-secret-warning"><ShieldCheck size={18} /><p>下载包包含订阅密钥。请妥善保管，不要上传到公共仓库或分享给他人。</p></div>
            <label className="client-downloads-consent"><input type="checkbox" name="acknowledged" required checked={acknowledged} disabled={busy} aria-describedby="client-secret-warning" onChange={(event) => setAcknowledged(event.target.checked)} /><span>我已了解下载包包含密钥</span></label>
            {!query.data?.key_management_available && <p role="alert">暂时无法读取订阅密钥，请联系管理员。</p>}
            <Button className="client-downloads-download-button" type="submit" disabled={busy || !acknowledged || !query.data?.key_management_available || !selected?.models.length}><Download data-icon="inline-start" />{busy ? "正在生成安装包" : "下载订阅版插件"}</Button>
            </div>
          </form>}
      </section>}
      </>}
      </div>
      {error && <p role="alert" className="client-downloads-error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
    </div></div>
  </div>
}
