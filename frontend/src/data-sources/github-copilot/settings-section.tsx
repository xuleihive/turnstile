import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  Check,
  ChevronRight,
  ExternalLink,
  KeyRound,
  Link2,
  RefreshCw,
  Save,
} from "lucide-react"

import { CopilotLogo } from "../../components/brand-logos"
import { Button } from "../../components/ui/button"
import { Input } from "../../components/ui/input"
import { Switch } from "../../components/ui/switch"
import { useAuth } from "../../providers/auth-provider"
import { copilotApi } from "./api"
import { CopilotConnectButton } from "./connect-button"
import { copilotKeys, copilotQueries } from "./queries"

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

export function CopilotSettingsSection() {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const canManage = user?.role === "owner"
  // These reads initialize form drafts, unlike the same queries on analytics pages.
  const status = useQuery({ ...copilotQueries.status(), meta: { automaticRefresh: false } })
  const identities = useQuery({ ...copilotQueries.identities(Boolean(canManage)), meta: { automaticRefresh: false } })
  const oauth = useQuery({ ...copilotQueries.oauthConfig(Boolean(canManage)), meta: { automaticRefresh: false } })
  const [organization, setOrganization] = useState("")
  const [billingToken, setBillingToken] = useState("")
  const [billingWritesEnabled, setBillingWritesEnabled] = useState(false)
  const [oauthClientId, setOauthClientId] = useState("")
  const [oauthClientSecret, setOauthClientSecret] = useState("")
  const [loginDrafts, setLoginDrafts] = useState<Record<string, string>>({})
  useEffect(() => {
    const defaultConnection = status.data?.connections.find((item) => item.is_default)
      ?? status.data?.connections[0]
    if (defaultConnection) {
      setOrganization(defaultConnection.organization)
      setBillingWritesEnabled(defaultConnection.write_enabled)
    }
  }, [status.data])
  useEffect(() => {
    if (oauth.data?.client_id) setOauthClientId(oauth.data.client_id)
  }, [oauth.data])
  useEffect(() => {
    if (!identities.data) return
    setLoginDrafts(Object.fromEntries(
      identities.data.map((item) => [item.app_user_id, item.github_login ?? ""]),
    ))
  }, [identities.data])
  const saveIdentity = useMutation({
    mutationFn: ({ appUserId, githubLogin }: { appUserId: string; githubLogin: string }) =>
      copilotApi.saveIdentity(appUserId, githubLogin),
    onSuccess: (mapping) => {
      queryClient.setQueryData(
        copilotKeys.identities,
        (current: typeof identities.data) => current?.map((item) =>
          item.app_user_id === mapping.app_user_id ? mapping : item),
      )
      void queryClient.invalidateQueries({ queryKey: copilotKeys.status })
      void queryClient.invalidateQueries({ queryKey: copilotKeys.dashboard(null) })
    },
  })
  const saveOauth = useMutation({
    mutationFn: () => copilotApi.saveOAuthConfig({
      client_id: oauthClientId.trim(),
      ...(oauthClientSecret ? { client_secret: oauthClientSecret } : {}),
    }),
    onSuccess: (value) => {
      setOauthClientSecret("")
      queryClient.setQueryData(copilotKeys.oauthConfig, value)
      void queryClient.invalidateQueries({ queryKey: copilotKeys.status })
    },
  })
  const saveBillingConnection = useMutation({
    mutationFn: () => copilotApi.saveConnection({
      organization: organization.trim(),
      token: billingToken,
      write_enabled: billingWritesEnabled,
      set_default: true,
    }),
    onSuccess: () => {
      setBillingToken("")
      void queryClient.invalidateQueries({ queryKey: copilotKeys.all })
    },
  })

  if (status.isLoading) {
    return <div className="settings-section"><span className="settings-inline-loading"><RefreshCw className="spin" size={13} />正在读取 GitHub Copilot 配置</span></div>
  }
  if (status.error) {
    return <div className="settings-section"><p className="settings-warning"><AlertTriangle size={13} /><span>无法读取 GitHub Copilot 配置<br /><span data-no-localize>{errorText(status.error)}</span></span></p></div>
  }
  if (!canManage) {
    return <div className="settings-section copilot-member-settings">
      <div><h2>GitHub 身份</h2><p>你的 Copilot 用量仅按已关联的 GitHub login 查询。</p></div>
      <div className="copilot-link-status" data-linked={Boolean(status.data?.viewer_github_login) || undefined}>
        <Link2 size={16} />
        <span>{status.data?.viewer_github_login
          ? <><b>已关联</b><small data-no-localize>{status.data.viewer_github_login}</small></>
          : <><b>尚未关联</b><small>连接当前 GitHub 账号以读取你的 Copilot 数据。</small></>}</span>
        {!status.data?.viewer_github_login && status.data?.oauth_configured && <CopilotConnectButton />}
      </div>
    </div>
  }

  const oauthReady = oauthClientId.trim().length > 0
    && (oauth.data?.configured || oauthClientSecret.length > 0)
  return <>
    <div className="settings-section copilot-account-settings">
      <div>
        <h2>我的 GitHub 账号</h2>
        <p>通过 GitHub 登录并授权一次，将当前 Turnstile 账号关联到你的 GitHub login。</p>
      </div>
      <div className="copilot-account-card" data-linked={Boolean(status.data?.viewer_github_login) || undefined}>
        <span className="copilot-account-icon"><CopilotLogo size={20} /></span>
        <span>{status.data?.viewer_github_login
          ? <><b>已连接 GitHub</b><small data-no-localize>{status.data.viewer_github_login}</small></>
          : <><b>尚未连接 GitHub</b><small>{oauth.data?.configured ? "点击按钮后将在 GitHub 完成登录和授权。" : "此环境尚未启用 GitHub 登录。"}</small></>}</span>
        {!status.data?.viewer_github_login && oauth.data?.configured && <CopilotConnectButton />}
      </div>
      {oauth.isLoading && <span className="settings-inline-loading"><RefreshCw className="spin" size={13} />正在检查 GitHub 登录状态</span>}
      {oauth.error && <p className="copilot-form-error"><AlertTriangle size={14} />{errorText(oauth.error)}</p>}
      {oauth.data && !oauth.data.configured && <p className="copilot-login-unavailable"><AlertTriangle size={14} />需要 Owner 先完成下方的一次性管理员设置，之后所有用户都只需点击 GitHub 授权按钮。</p>}
      <details className="copilot-admin-disclosure">
        <summary><ChevronRight size={14} /><span><b>一次性管理员设置</b><small>仅 Owner 首次启用 GitHub 登录时填写，普通用户无需填写。</small></span></summary>
        <div className="copilot-admin-disclosure-body">
          {oauth.data && <div className="copilot-connection-form copilot-oauth-form">
            <label><span>Client ID</span><Input value={oauthClientId} maxLength={255} autoComplete="off" onChange={(event) => setOauthClientId(event.target.value)} /></label>
            <label><span>Client Secret</span><Input value={oauthClientSecret} type="password" autoComplete="new-password" placeholder={oauth.data.client_secret_hint ?? "GitHub OAuth secret"} onChange={(event) => setOauthClientSecret(event.target.value)} /></label>
            <label className="copilot-callback-field"><span>Authorization callback URL</span><Input readOnly value={oauth.data.effective_callback_url} onFocus={(event) => event.currentTarget.select()} /></label>
            <Button disabled={!oauthReady || saveOauth.isPending} onClick={() => saveOauth.mutate()}><Save size={14} />{saveOauth.isPending ? "正在保存" : "保存 OAuth 配置"}</Button>
          </div>}
          {oauth.data?.configured && <div className="copilot-admin-configured">
            <p className="copilot-form-success"><Check size={14} />GitHub 登录已启用</p>
            <small data-no-localize>{oauth.data.client_secret_hint}</small>
          </div>}
          {saveOauth.isSuccess && <p className="copilot-form-success"><Check size={14} />OAuth 配置已保存。</p>}
          {saveOauth.error && <p className="copilot-form-error"><AlertTriangle size={14} />{errorText(saveOauth.error)}</p>}
        </div>
      </details>
    </div>
    <div className="settings-section copilot-connection-settings">
      <div><h2>组织 Copilot 数据连接</h2><p>这一步与个人 GitHub 登录不同，用于读取组织的席位、用量、AI Credits 和预算。</p></div>
      {status.data?.connections.length ? <div className="copilot-connection-list">
        {status.data.connections.map((connection) => <div key={connection.id}>
          <span className="copilot-connection-logo"><CopilotLogo size={17} /></span>
          <span><b data-no-localize>{connection.display_name}</b><small data-no-localize>{connection.organization}</small></span>
          <span className="copilot-credential-state"><KeyRound size={13} />{connection.credential_hint ?? "已配置"}</span>
          <span className="copilot-write-state" data-enabled={connection.write_enabled || undefined}>{connection.write_enabled ? "允许受控写入" : "只读"}</span>
        </div>)}
      </div> : <div className="copilot-link-status"><CopilotLogo size={16} /><span><b>尚未连接组织数据</b><small>个人账号已关联，但还不能读取组织 Copilot 数据。</small></span></div>}
      <div className="copilot-connection-form">
        <label><span>GitHub 组织</span><Input value={organization} maxLength={39} autoComplete="off" onChange={(event) => setOrganization(event.target.value)} /></label>
        {!status.data?.configured && <CopilotConnectButton purpose="organization" organization={organization} />}
      </div>
      {!status.data?.configured && <small>点击后将在 GitHub 授权组织 Copilot 用量和 billing 读取权限，Turnstile 不再要求你粘贴第二个 Token。</small>}
      {status.data?.configured && <div className="copilot-billing-credential">
        <div>
          <h3>完整账单数据</h3>
          <p>GitHub 的 Enterprise Billing、AI Credits 和 Budgets API 仅接受 classic PAT。Turnstile 验证全部只读接口后才会加密替换凭据。</p>
        </div>
        <a href="https://github.com/settings/tokens/new?description=Turnstile%20Enterprise%20Billing&scopes=manage_billing%3Acopilot%2Cmanage_billing%3Aenterprise" target="_blank" rel="noreferrer">创建 Billing PAT<ExternalLink size={13} /></a>
        <div className="copilot-connection-form">
          <label><span>Billing PAT</span><Input value={billingToken} type="password" autoComplete="new-password" placeholder="ghp_..." onChange={(event) => setBillingToken(event.target.value)} /></label>
          <label className="copilot-write-toggle">
            <Switch checked={billingWritesEnabled} onCheckedChange={setBillingWritesEnabled} />
            <span><b>允许受控 GitHub 写入</b><small>仅当 Owner 在单次审批中再次选择同步时，才会更新预算或成本中心。默认关闭。</small></span>
          </label>
          <Button disabled={!organization.trim() || billingToken.length < 8 || saveBillingConnection.isPending} onClick={() => saveBillingConnection.mutate()}><KeyRound size={14} />{saveBillingConnection.isPending ? "正在验证" : "验证并加密保存"}</Button>
        </div>
        {saveBillingConnection.isSuccess && <p className="copilot-form-success"><Check size={14} />完整账单凭据已验证并加密保存。</p>}
        {saveBillingConnection.error && <p className="copilot-form-error"><AlertTriangle size={14} />{errorText(saveBillingConnection.error)}</p>}
      </div>}
    </div>
    <div className="settings-section copilot-identity-settings">
      <div><h2>GitHub 身份映射</h2><p>显式关联 Turnstile 用户和 GitHub login，成员只能读取自己的 Copilot 数据。</p></div>
      {identities.isLoading && <span className="settings-inline-loading"><RefreshCw className="spin" size={13} />正在读取用户</span>}
      {identities.error && <p className="settings-warning"><AlertTriangle size={13} /><span>无法读取身份映射<br /><span data-no-localize>{errorText(identities.error)}</span></span></p>}
      {identities.data && <div className="copilot-identity-list">
        {identities.data.map((item) => {
          const value = loginDrafts[item.app_user_id] ?? ""
          const unchanged = value.trim().toLowerCase() === (item.github_login ?? "")
          return <div key={item.app_user_id}>
            <span className="copilot-user-avatar">{(item.display_name ?? item.email).charAt(0).toUpperCase()}</span>
            <span><b>{item.display_name ?? item.email}</b>{item.display_name && <small>{item.email}</small>}</span>
            <Input value={value} maxLength={39} autoComplete="off" placeholder="GitHub login" aria-label={`${item.email} 的 GitHub login`} onChange={(event) => setLoginDrafts((current) => ({ ...current, [item.app_user_id]: event.target.value }))} />
            <Button variant="outline" size="sm" disabled={!value.trim() || unchanged || saveIdentity.isPending} onClick={() => saveIdentity.mutate({ appUserId: item.app_user_id, githubLogin: value.trim() })}>保存</Button>
          </div>
        })}
      </div>}
      {saveIdentity.error && <p className="copilot-form-error"><AlertTriangle size={14} />{errorText(saveIdentity.error)}</p>}
    </div>
  </>
}
