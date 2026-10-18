import type { Query, QueryClient } from "@tanstack/react-query"

export const DATA_REFRESH_STORAGE_KEY = "finops_data_refresh_minutes_v1"
export const DATA_REFRESH_OPTIONS = [
  { minutes: 1, label: "近实时（1分钟）" },
  { minutes: 5, label: "5分钟（推荐）" },
  { minutes: 15, label: "15分钟" },
  { minutes: 30, label: "30分钟" },
  { minutes: 60, label: "1小时" },
  { minutes: 0, label: "关闭定时刷新" },
] as const
export type DataRefreshMinutes = typeof DATA_REFRESH_OPTIONS[number]["minutes"]
export const DEFAULT_DATA_REFRESH_MINUTES: DataRefreshMinutes = 5

export function normalizeDataRefreshMinutes(value: unknown): DataRefreshMinutes {
  const minutes = typeof value === "string" && value.trim() !== "" ? Number(value) : value
  return DATA_REFRESH_OPTIONS.find(option => option.minutes === minutes)?.minutes
    ?? DEFAULT_DATA_REFRESH_MINUTES
}

export function readDataRefreshMinutes(): DataRefreshMinutes {
  try {
    return normalizeDataRefreshMinutes(localStorage.getItem(DATA_REFRESH_STORAGE_KEY))
  } catch {
    return DEFAULT_DATA_REFRESH_MINUTES
  }
}

export function persistDataRefreshMinutes(minutes: DataRefreshMinutes) {
  try {
    localStorage.setItem(DATA_REFRESH_STORAGE_KEY, String(minutes))
    return true
  } catch {
    return false
  }
}

export function dataRefreshLabel(minutes: DataRefreshMinutes) {
  return DATA_REFRESH_OPTIONS.find(option => option.minutes === minutes)!.label
}

const DATA_QUERY_ROOTS = new Set([
  "finops", "application-access", "control-plane", "copilot", "user-settings",
  "trends", "runs", "findings", "optimizations",
])
const DIRECTORY_DATA_KINDS = new Set(["organizations", "units", "people", "accounts", "audit"])
const RELEASE_DATA_KINDS = new Set([
  "gateway-publication", "gateway-publications", "gateway-release", "gateway-releases",
])

export function isAutomaticDataQuery(query: Query) {
  const [root, kind] = query.queryKey
  if (query.meta?.automaticRefresh === false || query.state.fetchStatus !== "idle") return false
  if (root === "assistant") return kind === "pinned-charts"
  // References used by another page keep their cache TTL. A primary inventory view opts in.
  if (root === "reference") return query.meta?.automaticRefresh === true
  if (root === "directory") return DIRECTORY_DATA_KINDS.has(String(kind))
  if (!DATA_QUERY_ROOTS.has(String(root))) return false
  // Security freshness and operational progress must not follow an optional display cadence.
  if (root === "control-plane") {
    if (!RELEASE_DATA_KINDS.has(String(kind))) return false
    const data = query.state.data as { status?: string; role?: string; items?: { status?: string; role?: string }[] } | undefined
    const rows = data?.items ?? (data ? [data] : [])
    const terminal = new Set(["active", "failed", "awaiting_authorization", "rolled_back", "superseded"])
    if (rows.some(row => row.role === "in_progress" || (row.status && !terminal.has(row.status)))) return false
  }
  return true
}

export function refreshActiveDataQueries(client: QueryClient) {
  if (client.isMutating()) return Promise.resolve()
  return client.refetchQueries(
    { type: "active", predicate: isAutomaticDataQuery },
    { cancelRefetch: false },
  )
}

export type RefreshEnvironment = {
  now: () => number
  visible: () => boolean
  schedule: (callback: () => void, delay: number) => () => void
  subscribe: (callback: () => void) => () => void
}

export function startDataRefresh(
  intervalMs: number,
  refresh: () => void,
  environment: RefreshEnvironment,
) {
  if (intervalMs <= 0) return () => {}
  let lastRefresh = environment.now()
  let cancelTimer = () => {}
  let stopped = false
  const schedule = () => {
    cancelTimer()
    if (stopped || !environment.visible()) return
    const remaining = Math.max(0, intervalMs - (environment.now() - lastRefresh))
    cancelTimer = environment.schedule(() => {
      if (!environment.visible()) return
      lastRefresh = environment.now()
      refresh()
      schedule()
    }, remaining)
  }
  const unsubscribe = environment.subscribe(schedule)
  schedule()
  return () => {
    stopped = true
    cancelTimer()
    unsubscribe()
  }
}
