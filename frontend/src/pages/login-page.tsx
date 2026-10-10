import { useState, type FormEvent } from "react"
import { Eye, EyeOff, KeyRound, LoaderCircle } from "lucide-react"

import { Button } from "../components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "../components/ui/card"
import { Input } from "../components/ui/input"
import { Label } from "../components/ui/label"
import { TurnstileMark } from "../components/turnstile-logo"
import { useAuth } from "../providers/auth-provider"

/** Microsoft's four-square mark. Copied from the brand guidelines rather than approximated,
 *  and deliberately not recoloured: the squares are the one part of a Microsoft sign-in
 *  button that must stay literal, so it is not wired to any theme token. */
function MicrosoftMark({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 21 21" aria-hidden="true">
      <rect x="1" y="1" width="9" height="9" fill="#f25022" />
      <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
      <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
      <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
    </svg>
  )
}

export function LoginPage() {
  const { signInWithPassword, signInWithEntra, entraError, loginNotice } = useAuth()
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [revealed, setRevealed] = useState(false)
  const [pending, setPending] = useState<"password" | "entra" | null>(null)
  // Seeded from the redirect that just came back, so an account Microsoft authenticated
  // and this application then refused says so, instead of silently landing here again.
  const [error, setError] = useState(entraError ?? "")

  const busy = pending !== null

  async function handlePassword(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    setError("")
    setPending("password")
    try {
      await signInWithPassword(email.trim(), password)
    } catch {
      setError("邮箱或密码不正确，请重新输入。")
      setPending(null)
    }
  }

  async function handleEntra() {
    if (busy) return
    setError("")
    setPending("entra")
    try {
      await signInWithEntra()
    } catch {
      setError("Microsoft 登录未能完成，请重试。")
      setPending(null)
    }
  }

  return (
    <div className="login-shell">
      <Card className="login-card">
        <CardHeader className="login-head">
          <span className="login-mark" aria-hidden="true"><TurnstileMark size={24} /></span>
          <CardTitle className="login-title">登录 Turnstile</CardTitle>
          <CardDescription className="login-subtitle">
            Token 用量与成本治理
          </CardDescription>
        </CardHeader>

        <CardContent className="login-body">
          {loginNotice && <p className="login-notice" role="status">{loginNotice}</p>}
          {/* Microsoft first: it is the real identity path, and the password form below it is
              a temporary account for people trying the product out. Order is the only thing
              on this page that says which one is which. */}
          <Button
            type="button"
            variant="outline"
            className="login-entra"
            onClick={handleEntra}
            disabled={busy}
          >
            {pending === "entra"
              ? <><LoaderCircle className="login-spin" size={16} />正在跳转…</>
              : <><MicrosoftMark size={16} />使用 Microsoft 账户登录</>}
          </Button>
          {/* Microsoft's branding guidance explicitly asks for this line beside the button,
              so a reader can tell whether the account they hold is the one that works. */}
          <p className="login-hint">适用于你的工作或学校账户。</p>

          <div className="login-divider" aria-hidden="true"><span>或</span></div>

          <form id="login-form" className="login-form" onSubmit={handlePassword}>
            <div className="login-field">
              <Label htmlFor="login-email">邮箱地址</Label>
              <Input
                id="login-email"
                type="email"
                autoComplete="username"
                placeholder="you@contoso.com"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                disabled={busy}
                required
              />
            </div>

            <div className="login-field">
              <Label htmlFor="login-password">密码</Label>
              {/* The reveal control sits inside the field rather than beside it so the
                  input keeps the full column width; the button is 30px inside a 32px
                  field, which is why the wrapper owns the border instead of the input. */}
              <div className="login-password">
                <Input
                  id="login-password"
                  type={revealed ? "text" : "password"}
                  autoComplete="current-password"
                  placeholder="请输入密码"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  disabled={busy}
                  required
                />
                <button
                  type="button"
                  className="login-reveal"
                  onClick={() => setRevealed((value) => !value)}
                  aria-label={revealed ? "隐藏密码" : "显示密码"}
                  aria-pressed={revealed}
                  disabled={busy}
                >
                  {revealed ? <EyeOff size={15} /> : <Eye size={15} />}
                </button>
              </div>
            </div>

            {error && <p className="login-error" role="alert">{error}</p>}
          </form>
        </CardContent>

        <CardFooter className="login-actions">
          <Button
            type="submit"
            form="login-form"
            className="login-submit"
            disabled={busy || !email || !password}
          >
            {pending === "password"
              ? <><LoaderCircle className="login-spin" size={16} />正在登录…</>
              : <><KeyRound size={16} />登录</>}
          </Button>

        </CardFooter>
      </Card>
    </div>
  )
}
