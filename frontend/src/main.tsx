import { StrictMode } from "react"
import { createRoot, type Root } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { App } from "./app"
import { ApiError } from "./data-sources/apim/api"
import { LoginPage } from "./pages/login-page"
import { ClientAuthorizationPage } from "./pages/client-authorization-page"
import { LocaleProvider } from "./locales/index"
import { AuthProvider, useAuth } from "./providers/auth-provider"
import { ThemeProvider } from "./providers/theme-provider"
import { TimezoneProvider } from "./providers/timezone-provider"
import { DataRefreshProvider } from "./providers/data-refresh-provider"
import "@xyflow/react/dist/style.css"
import "./styles.css"

/** Local development only: move the whole page to `localhost` before anything else runs.
 *
 *  Entra compares redirect URIs as exact strings and does not treat `127.0.0.1` and
 *  `localhost` as the same host, so opening the app on the wrong one fails the Microsoft
 *  sign-in with AADSTS50011. Registering both addresses would also work, but that is a
 *  portal change every developer has to know about, and forgetting it produces an error
 *  that says nothing about which of the two you happened to type.
 *
 *  It has to be a full navigation rather than just overriding MSAL's redirectUri: the PKCE
 *  verifier and the state MSAL writes before leaving live in this origin's sessionStorage,
 *  so coming back on a different origin would leave them unreadable and fail anyway.
 *  Doing it here, before `createRoot`, means nothing has been fetched or stored yet.
 *
 *  `replace` rather than `assign` so the back button does not bounce between the two. */
if (window.location.hostname === "127.0.0.1") {
  window.location.replace(
    `${window.location.protocol}//localhost:${window.location.port}${window.location.pathname}${window.location.search}${window.location.hash}`,
  )
}

const queryClient: QueryClient = import.meta.hot?.data.queryClient ?? new QueryClient({
  defaultOptions: {
    queries: {
      gcTime: 30 * 60_000,
      refetchOnWindowFocus: false,
      refetchOnMount: "always",
      // One retry, but only for failures that could plausibly go away. A 4xx is the
      // server's considered answer -- retrying a 404 from an API older than this UI just
      // doubles how long the page sits on a spinner before saying so. Measured against a
      // cold App Service: 25 seconds of "loading" for an endpoint that was never there.
      retry: (failureCount, error) =>
        failureCount < 1
        && !(error instanceof ApiError && error.status >= 400 && error.status < 500),
    },
  },
})

function pendingClientAuthorization(): string | null {
  const supplied = new URLSearchParams(window.location.search).get("client_authorization")
  try {
    if (supplied && /^[A-Z2-9]{4}-[A-Z2-9]{4}$/.test(supplied)) {
      sessionStorage.setItem("turnstile-client-authorization", JSON.stringify({ code: supplied, until: Date.now() + 300_000 }))
      return supplied
    }
    const pending = JSON.parse(sessionStorage.getItem("turnstile-client-authorization") ?? "null") as { code: string; until: number } | null
    if (pending && pending.until > Date.now() && /^[A-Z2-9]{4}-[A-Z2-9]{4}$/.test(pending.code)) return pending.code
    sessionStorage.removeItem("turnstile-client-authorization")
  } catch { /* Login still works when browser storage is unavailable. */ }
  return null
}
const clientAuthorization = pendingClientAuthorization()

/** Nothing behind this renders until the session is known.
 *
 *  It gates on mount rather than per route because every route in this product reads
 *  organisation-wide spend; there is no page that is meaningfully public. Rendering the
 *  shell first and hiding it afterwards would fire the whole prefetch chain as an
 *  anonymous caller, so the gate has to sit above `App`, not inside it.
 *
 *  `checking` used to render nothing, on the reasoning that it lasts one tick. That was
 *  true only for the plain case. Returning from Microsoft it covers MSAL initialising, the
 *  redirect being processed, a round trip to exchange the token and the server fetching
 *  Microsoft's signing keys -- measured at about five seconds of blank page, which reads
 *  as a crash rather than as progress. */
function AuthGate({ children }: { children: React.ReactNode }) {
  const { status } = useAuth()
  if (status === "checking") {
    return (
      <div className="login-shell">
        <div className="login-skeleton" role="status" aria-busy="true">
          <i className="sk-mark" />
          <i className="sk-title" />
          <i className="sk-subtitle" />
          <i className="sk-button" />
          <i className="sk-field" />
        </div>
      </div>
    )
  }
  if (status === "anonymous") return <LoginPage />
  return <>{children}</>
}

const root: Root = import.meta.hot?.data.root ?? createRoot(document.getElementById("root")!)
if (import.meta.hot) {
  import.meta.hot.data.root = root
  import.meta.hot.data.queryClient = queryClient
}

root.render(
  <StrictMode>
    <LocaleProvider>
      <TimezoneProvider>
        <ThemeProvider>
          <QueryClientProvider client={queryClient}>
            <AuthProvider>
              <AuthGate>
                <DataRefreshProvider>
                  {clientAuthorization ? <ClientAuthorizationPage code={clientAuthorization} /> : <App />}
                </DataRefreshProvider>
              </AuthGate>
            </AuthProvider>
          </QueryClientProvider>
        </ThemeProvider>
      </TimezoneProvider>
    </LocaleProvider>
  </StrictMode>,
)
