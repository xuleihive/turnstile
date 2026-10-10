import { defineConfig, loadEnv } from "vite"
import react from "@vitejs/plugin-react"

// The dev server never talks to a model provider directly. It proxies every API
// call to the FastAPI backend, which owns credentials and gateway routing.
// The tracked frontend/.env.local targets the shared test API. Override API_PROXY_TARGET
// explicitly for a local backend; a missing value falls back to localhost.
const DEFAULT_API_TARGET = "http://127.0.0.1:8000"

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "")
  const apiTarget = env.API_PROXY_TARGET || DEFAULT_API_TARGET

  return {
    plugins: [react()],
    server: {
      port: 5173,
      watch: { usePolling: env.CHOKIDAR_USEPOLLING === "1" },
      proxy: {
        // `cookieDomainRewrite` is what lets the session cookie survive the hop. Without
        // it the backend's Set-Cookie carries whatever host it saw -- and against a cloud
        // target that is an azurewebsites.net domain the browser will not store for
        // localhost, so sign-in succeeds and every following request is anonymous.
        "/api": { target: apiTarget, changeOrigin: true, cookieDomainRewrite: "" },
        "/health": { target: apiTarget, changeOrigin: true, cookieDomainRewrite: "" },
      },
    },
  }
})
