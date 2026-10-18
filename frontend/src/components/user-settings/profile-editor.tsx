import { useEffect, useRef, useState, type FormEvent } from "react"
import { Camera, Check, Eye, EyeOff, KeyRound, RefreshCw, Save, Trash2, X } from "lucide-react"

import { userSettingsApi } from "../../api/user-settings"
import { ApiError } from "../../api/client"
import { useAuth, type AuthUser } from "../../providers/auth-provider"
import { prepareAvatar } from "../../lib/avatar"
import { Button } from "../ui/button"
import { Input } from "../ui/input"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../ui/dialog"

const message = (error: unknown) => error instanceof Error ? error.message : "操作失败，请重试。"

function PasswordDialog({ onClose }: { onClose: () => void }) {
  const { endSession } = useAuth()
  const [current, setCurrent] = useState("")
  const [next, setNext] = useState("")
  const [confirmation, setConfirmation] = useState("")
  const [revealed, setRevealed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [errorField, setErrorField] = useState<string | null>(null)
  async function submit(event: FormEvent) {
    event.preventDefault()
    event.stopPropagation()
    if (busy) return
    if (next !== confirmation) { setError("两次输入的新密码不一致。"); setErrorField("confirm-password"); return }
    if (next === current) { setError("新密码不能与当前密码相同。"); setErrorField("new-password"); return }
    setBusy(true)
    setError(null)
    setErrorField(null)
    try {
      await userSettingsApi.changePassword(current, next, confirmation)
      setCurrent(""); setNext(""); setConfirmation("")
      endSession("密码已更新，请重新登录。")
    } catch (reason) {
      if (reason instanceof TypeError) {
        endSession("密码更新结果未确认，请重新登录。")
        return
      }
      setError(message(reason))
      setErrorField(reason instanceof ApiError && reason.status === 400 ? "current-password" : null)
      setBusy(false)
    }
  }
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <DialogContent className="user-settings-dialog">
      <form onSubmit={submit}>
        <div className="user-settings-dialog-heading">
          <DialogTitle>修改密码</DialogTitle>
          <Button type="button" variant="ghost" size="icon" title="关闭" aria-label="关闭" disabled={busy} onClick={onClose}><X size={16} /></Button>
        </div>
        <DialogDescription className="sr-only">修改密码</DialogDescription>
        <div className="user-settings-password-fields">
          {[
            { id: "current-password", label: "当前密码", value: current, setter: setCurrent, min: 1, auto: "current-password" },
            { id: "new-password", label: "新密码", value: next, setter: setNext, min: 12, auto: "new-password" },
            { id: "confirm-password", label: "确认新密码", value: confirmation, setter: setConfirmation, min: 12, auto: "new-password" },
          ].map((field) => <label key={field.id} htmlFor={field.id}>
            <span>{field.label}</span>
            <Input data-no-localize id={field.id} type={revealed ? "text" : "password"} autoComplete={field.auto}
              minLength={field.min} maxLength={256} value={field.value} required
              disabled={busy} aria-invalid={errorField === field.id} aria-describedby={error ? "password-error" : undefined}
              onChange={(event) => field.setter(event.target.value)} />
          </label>)}
          {error && <p id="password-error" className="user-settings-error" role="alert">{error}</p>}
        </div>
        <div className="user-settings-dialog-actions">
          <Button type="button" variant="ghost" size="icon" title={revealed ? "隐藏密码" : "显示密码"} aria-label={revealed ? "隐藏密码" : "显示密码"} onClick={() => setRevealed(!revealed)}>{revealed ? <EyeOff size={16} /> : <Eye size={16} />}</Button>
          <div><Button type="button" variant="outline" disabled={busy} onClick={onClose}>取消</Button>
            <Button type="submit" disabled={busy}>{busy ? <RefreshCw className="spin" size={14} /> : <Save size={14} />}保存</Button></div>
        </div>
      </form>
    </DialogContent>
  </Dialog>
}

function AvatarEditor({ user }: { user: AuthUser }) {
  const { photo, updateAvatar } = useAuth()
  const input = useRef<HTMLInputElement>(null)
  const mounted = useRef(true)
  const [preview, setPreview] = useState<string | null>(null)
  const [processing, setProcessing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const editable = user.method === "password"
  async function choose(file: File) {
    setProcessing(true)
    setError(null)
    try {
      const prepared = await prepareAvatar(file)
      if (mounted.current) setPreview(prepared)
    } catch (reason) {
      if (mounted.current) setError(message(reason))
    } finally {
      if (mounted.current) setProcessing(false)
    }
  }
  async function save(value: string | null) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await userSettingsApi.updateAvatar(value)
      if (mounted.current) {
        updateAvatar(result.avatar_url)
        setPreview(null)
      }
    } catch (reason) {
      if (mounted.current) setError(message(reason))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }
  const image = <span className="user-settings-avatar">
    {photo ? <img src={photo} alt="" /> : <span>{(user.name ?? user.email).trim().charAt(0).toUpperCase()}</span>}
  </span>
  return <div className="user-settings-avatar-editor">
    {editable ? <Button className="user-settings-avatar-trigger" type="button" variant="ghost" title="更换头像" aria-label="更换头像" disabled={busy || processing} onClick={() => input.current?.click()}>
      {image}<span className="user-settings-avatar-action">{processing ? <RefreshCw className="spin" size={14} /> : <Camera size={14} />}</span>
    </Button> : image}
    {editable && user.avatar_url && <Button type="button" variant="ghost" size="icon-sm" title="移除头像" aria-label="移除头像" disabled={busy || processing} onClick={() => void save(null)}><Trash2 size={14} /></Button>}
    <input ref={input} type="file" hidden accept="image/png,image/jpeg,image/webp" onChange={(event) => {
      const file = event.target.files?.[0]
      event.target.value = ""
      if (file) void choose(file)
    }} />
    {error && !preview && <span className="user-settings-error" role="alert">{error}</span>}
    {preview && <Dialog open onOpenChange={(open) => { if (!open && !busy) setPreview(null) }}>
      <DialogContent className="user-settings-dialog">
        <div className="user-settings-dialog-heading"><DialogTitle>更换头像</DialogTitle>
          <Button type="button" variant="ghost" size="icon" title="关闭" aria-label="关闭" disabled={busy} onClick={() => setPreview(null)}><X size={16} /></Button></div>
        <DialogDescription className="sr-only">头像预览</DialogDescription>
        <div className="user-settings-avatar-preview"><img src={preview} alt="头像预览" /></div>
        {error && <p className="user-settings-error" role="alert">{error}</p>}
        <div className="user-settings-dialog-actions"><span />
          <div><Button variant="outline" disabled={busy} onClick={() => setPreview(null)}>取消</Button>
            <Button disabled={busy} onClick={() => void save(preview)}>{busy ? <RefreshCw className="spin" size={14} /> : <Save size={14} />}保存</Button></div>
        </div>
      </DialogContent>
    </Dialog>}
  </div>
}

function NameEditor({ user }: { user: AuthUser }) {
  const { updateProfile } = useAuth()
  const [name, setName] = useState(user.name ?? "")
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [passwordOpen, setPasswordOpen] = useState(false)
  useEffect(() => { setName(user.name ?? "") }, [user.name])
  const changed = (name.trim() || null) !== user.name
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!changed || busy) return
    setBusy(true); setNotice(null); setError(null)
    try {
      updateProfile(await userSettingsApi.updateName(name.trim() || null), true)
      setNotice("显示名称已更新。")
    } catch (reason) { setError(message(reason)) }
    finally { setBusy(false) }
  }
  return <><form className="user-settings-name-form" onSubmit={submit}>
    <label htmlFor="display-name"><span>显示名称</span>
      <Input data-no-localize id="display-name" value={name} maxLength={160} disabled={busy}
        aria-invalid={Boolean(error)} aria-describedby={error ? "name-error" : undefined}
        onChange={(event) => { setName(event.target.value); setNotice(null) }} /></label>
    <div className="user-settings-profile-actions">
      <Button type="button" variant="outline" disabled={busy || !changed} onClick={() => { setName(user.name ?? ""); setError(null) }}>取消</Button>
      <Button type="submit" disabled={busy || !changed}>{busy ? <RefreshCw className="spin" size={14} /> : <Save size={14} />}保存</Button>
      <Button type="button" variant="outline" onClick={() => setPasswordOpen(true)}><KeyRound size={14} />修改密码</Button>
    </div>
    {notice && <span className="user-settings-notice" role="status"><Check size={14} />{notice}</span>}
    {error && <span id="name-error" className="user-settings-error" role="alert">{error}</span>}
  </form>{passwordOpen && <PasswordDialog onClose={() => setPasswordOpen(false)} />}</>
}

export function ProfileEditor() {
  const { user } = useAuth()
  if (!user) return null
  return <section className="user-settings-section">
    <h2>个人资料</h2>
    <div className="user-settings-profile">
      <AvatarEditor key={`${user.id}:${user.method}`} user={user} />
      <div className="user-settings-identity">
        <strong data-no-localize>{user.name ?? user.email}</strong>
        <span className="user-settings-account-name"><span>账号名称</span><span data-no-localize>{user.email}</span></span>
        <span className="user-settings-method">{user.method === "password" ? "账号密码" : "Microsoft 账号"}</span>
      </div>
    </div>
    {user.method === "password" ? <NameEditor user={user} /> : <dl className="user-settings-facts"><div><dt>显示名称</dt><dd data-no-localize>{user.name ?? "--"}</dd></div><div><dt>登录方式</dt><dd>Microsoft 管理</dd></div></dl>}
  </section>
}
