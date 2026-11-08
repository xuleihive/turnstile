import { defineConfig, loadEnv, type ProxyOptions } from "vite"
import react from "@vitejs/plugin-react"

// The dev server never talks to a model provider directly. It proxies every API
// call to the FastAPI backend, which owns credentials and gateway routing.
// The tracked frontend/.env.local targets the shared test API. Override API_PROXY_TARGET
// explicitly for a local backend; a missing value falls back to localhost.
const DEFAULT_API_TARGET = "http://127.0.0.1:8000"

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "")
  const apiTarget = env.API_PROXY_TARGET || DEFAULT_API_TARGET
  const apiOrigin = new URL(apiTarget).origin
  const localCookieName = env.API_PROXY_COOKIE_NAME
  const sessionCookieName = "turnstile_session"
  const apiProxy: ProxyOptions = {
    target: apiTarget,
    changeOrigin: true,
    cookieDomainRewrite: "",
    bypass(request) {
      if (["GET", "HEAD", "OPTIONS"].includes(request.method ?? "GET")) return
      const { origin, host } = request.headers
      const fetchSite = request.headers["sec-fetch-site"]
      const protocol = "encrypted" in request.socket && request.socket.encrypted ? "https" : "http"
      if (!host || origin !== `${protocol}://${host}`) return
      if (fetchSite && fetchSite !== "same-origin") return
      request.headers.origin = apiOrigin
    },
    configure(proxy) {
      if (!localCookieName) return
      proxy.on("proxyReq", (proxyRequest, request) => {
        const cookies = (request.headers.cookie ?? "").split(";")
          .map(cookie => cookie.trim())
          .filter(cookie => cookie && !cookie.startsWith(`${sessionCookieName}=`))
          .map(cookie => cookie.startsWith(`${localCookieName}=`)
            ? `${sessionCookieName}${cookie.slice(localCookieName.length)}`
            : cookie)
        if (cookies.length) proxyRequest.setHeader("cookie", cookies.join("; "))
        else proxyRequest.removeHeader("cookie")
      })
      proxy.on("proxyRes", response => {
        const cookies = response.headers["set-cookie"]
        if (cookies) response.headers["set-cookie"] = cookies.map(cookie =>
          cookie.startsWith(`${sessionCookieName}=`)
            ? `${localCookieName}${cookie.slice(sessionCookieName.length)}`
            : cookie)
      })
    },
  }

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        // `cookieDomainRewrite` is what lets the session cookie survive the hop. Without
        // it the backend's Set-Cookie carries whatever host it saw -- and against a cloud
        // target that is an azurewebsites.net domain the browser will not store for
        // localhost, so sign-in succeeds and every following request is anonymous.
        "/api": apiProxy,
        "/health": { target: apiTarget, changeOrigin: true, cookieDomainRewrite: "" },
      },
    },
  }
})