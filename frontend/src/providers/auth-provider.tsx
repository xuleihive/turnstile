import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import {
  PublicClientApplication,
  type Configuration,
} from "@azure/msal-browser"
import { authApi, type AuthUser, type SignInMethod } from "../api/auth"
import { SESSION_EXPIRED_EVENT } from "../api/client"

export type { AuthUser, SignInMethod } from "../api/auth"

/** How the person proved who they are. Kept on the profile because the UI says so out
 *  loud -- an operator looking at a shared screen should be able to tell a real Microsoft
 *  identity from a temporary demo account without opening anything. */

type AuthStatus = "checking" | "authenticated" | "anonymous"

type AuthContextValue = {
  status: AuthStatus
  user: AuthUser | null
  /** The person's Entra profile photo as a data URL, or null. Separate from `AuthUser`
   *  because it does not come from this app's server -- it is fetched from Microsoft
   *  Graph in the browser, after sign-in, and only for the Microsoft path. */
  photo: string | null
  /** Why the last Microsoft sign-in was refused, for the sign-in page to show. */
  entraError: string | null
  signInWithPassword: (email: string, password: string) => Promise<void>
  signInWithEntra: () => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

/** The client id is a public identifier -- it travels in every authorize URL.
 *
 *  There is no tenant id here on purpose. The authority is `/organizations`, copied from
 *  GBBAIP's authConfig.js, which admits any work or school account from any tenant. Pinning
 *  it to the tenant that owns the app registration is what produced "Selected user account
 *  does not exist in tenant 'Default Directory'": the registration and the people signing
 *  in live in different tenants, which is the normal case here rather than a mistake.
 *
 *  What keeps this from meaning "anyone with a Microsoft work account" is the backend: it
 *  verifies the signature, requires `aud` to be this client, requires the issuer to match
 *  the token's own tenant, and then admits only the configured mail domains. That last
 *  check is the actual authorization gate, which is why it is an exact domain comparison
 *  and not a substring.
 *
 *  This is Turnstile's own registration. It replaced a borrowed one whose display name was
 *  another product's, which every consent screen showed -- the name on that screen belongs
 *  to the registration, not to the frontend using it, so the only fix was a registration of
 *  our own. Keep it in step with `entra_client_id` on the backend: that is what `aud` is
 *  checked against, and a mismatch fails every Microsoft sign-in. */
const ENTRA_CLIENT_ID = import.meta.env.VITE_ENTRA_CLIENT_ID?.trim() ?? ""
/** Optional. Set for a single-tenant registration, where `/organizations` is refused
 *  (AADSTS50194) and a guest must sign in through the resource tenant. The backend then
 *  pins the same tenant with ENTRA_TENANT_IDS, so the two stay one decision. */
const ENTRA_TENANT_ID = import.meta.env.VITE_ENTRA_TENANT_ID?.trim() ?? ""

const msalConfig: Configuration = {
  auth: {
    clientId: ENTRA_CLIENT_ID || "00000000-0000-0000-0000-000000000000",
    authority: `https://login.microsoftonline.com/${ENTRA_TENANT_ID || "organizations"}`,
    // `window.location.origin` rather than a constant so one registration covers
    // localhost, test and production. Each origin still has to be listed in the app
    // registration, but the code does not need to know which one it is running as.
    redirectUri: window.location.origin,
    // Stay on the redirect URI once the response is processed instead of navigating back
    // to wherever sign-in was started. GBBAIP sets this too. The default `true` costs a
    // second navigation and, with this app's query-string routing, would restore a page
    // the person cannot see yet -- the session is established milliseconds later.
    navigateToLoginRequestUrl: false,
  },
  cache: {
    // The MSAL token never leaves this tab: it is posted straight to /auth/entra and the
    // session that comes back lives in an httpOnly cookie. sessionStorage rather than
    // localStorage keeps that short-lived artefact from outliving the tab that made it.
    cacheLocation: "sessionStorage",
    storeAuthStateInCookie: false,
  },
}

const msal = new PublicClientApplication(msalConfig)
let msalReady: Promise<void> | null = null

function initialiseMsal(): Promise<void> {
  // MSAL v4 requires initialize() before any other call, and it must happen exactly once.
  msalReady ??= msal.initialize()
  return msalReady
}

/** Resolved once per page load, at module scope, and shared by every caller.
 *
 *  React StrictMode runs effects twice in development, and `handleRedirectPromise` cannot
 *  be called concurrently: MSAL answers the first call and leaves the second pending
 *  forever. Since the first effect's cleanup has already set its `cancelled` flag by then,
 *  nothing ever calls setState and the page sits on the skeleton indefinitely -- which is
 *  exactly what it did. Memoising here also stops the redirect being exchanged twice; the
 *  server log showed two `POST /auth/entra` and therefore two sessions per sign-in. */
let identity: Promise<AuthUser | null> | null = null

/** Why the returning Microsoft token was refused, if it was.
 *
 *  Module scope alongside `identity` because it is set inside that one resolution, and
 *  reset by the same full-page redirect that resets it. Without this the 401 from
 *  `/auth/entra` was swallowed by the catch below and the person landed back on the
 *  sign-in page with nothing to read -- having just authenticated successfully at
 *  Microsoft. The most likely cause is a work account outside the allowed mail domains,
 *  which `/organizations` lets sign in and the backend then refuses; "nothing happened"
 *  is the one response that makes them retry forever. */
let entraRejection: string | null = null

function resolveIdentity(): Promise<AuthUser | null> {
  // Order matters and is the whole of the redirect flow. Coming back from Microsoft the
  // result is in the URL, not in a promise the click site is still awaiting -- that call
  // site navigated away and no longer exists. So `handleRedirectPromise` runs first, and
  // only if it yields nothing do we fall through to asking the server who we are.
  // Checking the session first would leave the returning token unread and drop the person
  // back on the sign-in page having just signed in.
  identity ??= (async () => {
    await initialiseMsal()
    const redirect = await msal.handleRedirectPromise().catch(() => null)
    if (redirect?.idToken) {
      try {
        return await authApi.exchangeEntraToken(redirect.idToken)
      } catch (error) {
        // Kept rather than rethrown: this is not signed in, which is an ordinary state,
        // but it is signed in *and refused*, which the person needs told.
        entraRejection = error instanceof Error ? error.message : "Microsoft 登录失败，请重试。"
        return null
      }
    }
    return await authApi.profile()
  })().catch(() => null)
  return identity
}

/** Reading the signed-in person's own profile, which is what `/me/photo` needs.
 *
 *  Requested at sign-in so consent is settled there rather than surfacing later as a
 *  silent failure: `acquireTokenSilent` cannot prompt, so a scope that was never consented
 *  to just fails, and the avatar would quietly never appear with nothing to explain it. */
const GRAPH_SCOPES = ["User.Read"]

/** Same size GBBAIP asks for. Graph resizes server-side, so this is a fraction of the
 *  full-resolution photo -- typically a few KB against a few hundred. */
const GRAPH_PHOTO_URL = "https://graph.microsoft.com/v1.0/me/photos/96x96/$value"

/** Cached in sessionStorage rather than refetched per mount, and sessionStorage
 *  specifically because that is where MSAL keeps the account this photo belongs to. The
 *  two expire together, so there is no window where a stale face outlives the identity it
 *  came from -- which localStorage would create on a shared machine. */
const PHOTO_CACHE_KEY = "finops_entra_photo"
/** Distinguishes "asked, and this person has no photo" from "not asked yet". Without it
 *  every load pays a Graph round trip to be told 404 again. */
const PHOTO_NONE = "none"

function readBlobAsDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(blob)
  })
}

/** The real avatar, from the directory rather than a generated initial.
 *
 *  A data URL rather than `URL.createObjectURL`: an object URL is a handle that has to be
 *  revoked or it leaks, and it cannot be cached across a reload. The photo is a few KB, so
 *  inlining it costs less than the bookkeeping.
 *
 *  Returns null for every failure -- no account, no consent, no photo set, Graph
 *  unreachable. None of them are conditions the person can act on, and the initial-letter
 *  avatar is a complete answer in all of them. */
async function fetchEntraPhoto(): Promise<string | null> {
  const cached = sessionStorage.getItem(PHOTO_CACHE_KEY)
  if (cached) return cached === PHOTO_NONE ? null : cached

  await initialiseMsal()
  const account = msal.getAllAccounts()[0]
  // No MSAL account means this tab was opened fresh against a still-valid session cookie:
  // the app's own session outlives MSAL's sessionStorage. Signed in, but with nothing to
  // call Graph with, so the letter stands in until the next Microsoft sign-in.
  if (!account) return null

  // A different token from the ID token that established the session. That one identifies
  // the person to this app; this one authorises this app to read Graph on their behalf.
  // Sending the ID token to Graph would be rejected, and sending this one to our own
  // backend would be a category error.
  const auth = await msal.acquireTokenSilent({ scopes: GRAPH_SCOPES, account }).catch(() => null)
  if (!auth) return null

  const response = await fetch(GRAPH_PHOTO_URL, {
    headers: { Authorization: `Bearer ${auth.accessToken}` },
  }).catch(() => null)
  // 404 is the ordinary answer for somebody who has never set a photo, not an error.
  if (!response?.ok) {
    sessionStorage.setItem(PHOTO_CACHE_KEY, PHOTO_NONE)
    return null
  }

  const dataUrl = await readBlobAsDataUrl(await response.blob()).catch(() => null)
  if (dataUrl) sessionStorage.setItem(PHOTO_CACHE_KEY, dataUrl)
  return dataUrl
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<AuthStatus>("checking")
  const [user, setUser] = useState<AuthUser | null>(null)
  const [photo, setPhoto] = useState<string | null>(null)
  const [entraError, setEntraError] = useState<string | null>(null)

  const expireLocalSession = useCallback(() => {
    queryClient.clear()
    setUser(null)
    setStatus("anonymous")
    setPhoto(null)
    setEntraError(null)
    sessionStorage.removeItem(PHOTO_CACHE_KEY)
    identity = null
  }, [queryClient])

  useEffect(() => {
    let cancelled = false
    void resolveIdentity().then((profile) => {
      if (cancelled) return
      setUser(profile)
      setStatus(profile ? "authenticated" : "anonymous")
      setEntraError(entraRejection)
    })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    window.addEventListener(SESSION_EXPIRED_EVENT, expireLocalSession)
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, expireLocalSession)
  }, [expireLocalSession])

  useEffect(() => {
    if (!user?.session_expires_at) return
    const remaining = Date.parse(user.session_expires_at) - Date.now()
    if (!Number.isFinite(remaining) || remaining <= 0) {
      expireLocalSession()
      return
    }
    let timer: number | undefined
    const expireWhenDue = () => {
      const nextRemaining = Date.parse(user.session_expires_at!) - Date.now()
      if (nextRemaining <= 0) {
        expireLocalSession()
        return
      }
      // Browsers clamp delays above a signed 32-bit millisecond value. Settings permits
      // a 30-day session, so long lifetimes are scheduled in safe chunks.
      timer = window.setTimeout(expireWhenDue, Math.min(nextRemaining, 2_147_483_647))
    }
    timer = window.setTimeout(expireWhenDue, Math.min(remaining, 2_147_483_647))
    return () => {
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [expireLocalSession, user])

  // Deliberately after the identity resolves rather than part of it. The photo is
  // decoration; making the dashboard wait on a Graph round trip to render would trade a
  // visible delay for an avatar nobody is waiting to see. It appears a moment later.
  useEffect(() => {
    if (user?.method !== "entra") return
    let cancelled = false
    void fetchEntraPhoto()
      .catch(() => null)
      .then((found) => {
        if (!cancelled) setPhoto(found)
      })
    return () => {
      cancelled = true
    }
  }, [user?.method])

  const adopt = useCallback((next: AuthUser) => {
    // Query data contains organisation-wide usage and is not keyed by reader. A second
    // account in the same tab must never inherit the previous account's in-memory view.
    queryClient.clear()
    setUser(next)
    setStatus("authenticated")
  }, [queryClient])

  const signInWithPassword = useCallback(
    async (email: string, password: string) => {
      adopt(await authApi.signInWithPassword(email, password))
    },
    [adopt],
  )

  const signInWithEntra = useCallback(async () => {
    if (!ENTRA_CLIENT_ID) {
      const message = "Microsoft sign-in is not configured for this deployment."
      setEntraError(message)
      throw new Error(message)
    }
    await initialiseMsal()
    // A full-page redirect, matching GBBAIP. Not `loginPopup`: a popup is blocked by
    // default in Safari and by common enterprise policy, and it fails in a way the person
    // cannot act on -- nothing appears and there is no error to read. The redirect costs a
    // page load and always works.
    //
    // Nothing follows this call. The browser leaves; the result is picked up by
    // `handleRedirectPromise` in the effect above when it comes back.
    // `User.Read` rides along with the sign-in scopes so consent for the photo is granted
    // in the same prompt. Asking for it later, at the point of use, would mean
    // `acquireTokenSilent` failing on an unconsented scope with no way to recover silently.
    await msal.loginRedirect({ scopes: ["openid", "profile", "email", ...GRAPH_SCOPES] })
  }, [])

  const signOut = useCallback(async () => {
    // The server session goes first: it is the thing that actually grants access, and the
    // local state is only a picture of it. Clearing the picture first would leave a live
    // session behind if the request then failed.
    await authApi.signOut().catch(() => undefined)
    // MSAL's own cache goes too, so the next Microsoft sign-in asks which account to use
    // instead of silently reusing the last one on a shared machine.
    await initialiseMsal()
      .then(() => msal.clearCache())
      .catch(() => undefined)
    expireLocalSession()
  }, [expireLocalSession])

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, photo, entraError, signInWithPassword, signInWithEntra, signOut }),
    [status, user, photo, entraError, signInWithPassword, signInWithEntra, signOut],
  )

  return <AuthContext value={value}>{children}</AuthContext>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error("useAuth must be used inside AuthProvider")
  return context
}
