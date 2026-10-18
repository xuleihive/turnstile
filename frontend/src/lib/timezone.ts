type TimezoneGrain = "hour" | "day"

export type TimezonePreference = "browser" | "UTC"

const COOKIE_NAME = "finops_timezone"
const COOKIE_MAX_AGE = 60 * 60 * 24 * 365

export function readTimezonePreference(): TimezonePreference {
  const value = document.cookie
    .split("; ")
    .find((entry) => entry.startsWith(`${COOKIE_NAME}=`))
    ?.split("=")[1]
  return value === "UTC" ? "UTC" : "browser"
}

export function resolvedBrowserTimezone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"
}

export function resolveTimezone() {
  return readTimezonePreference() === "UTC" ? "UTC" : resolvedBrowserTimezone()
}

export function persistTimezonePreference(preference: TimezonePreference) {
  document.cookie = `${COOKIE_NAME}=${preference}; Max-Age=${COOKIE_MAX_AGE}; Path=/; SameSite=Lax`
}

const formatterCache = new Map<string, Intl.DateTimeFormat>()

function bucketFormatter(timezone: string, grain: TimezoneGrain) {
  const key = `${timezone}:${grain}`
  const cached = formatterCache.get(key)
  if (cached) return cached
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(grain === "hour" ? { hour: "2-digit" as const, hourCycle: "h23" as const } : {}),
  })
  formatterCache.set(key, formatter)
  return formatter
}

export function timezoneBucketKey(value: string | Date, grain: TimezoneGrain, timezone: string) {
  const parts = Object.fromEntries(
    bucketFormatter(timezone, grain)
      .formatToParts(typeof value === "string" ? new Date(value) : value)
      .map((part) => [part.type, part.value]),
  )
  const date = `${parts.year}-${parts.month}-${parts.day}`
  return grain === "hour" ? `${date}T${parts.hour}` : date
}

export function formatUsageBucket(
  timestamp: string,
  interval: "hour" | "day" | "week",
  timezone: string,
  locale: string,
) {
  const date = new Date(timestamp)
  if (interval === "hour") return date.toLocaleString(locale, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    ...(locale === "en-US" ? { minute: "2-digit" as const } : {}),
    hourCycle: "h23",
    timeZone: timezone,
  })
  return date.toLocaleDateString(locale, {
    month: "2-digit", day: "2-digit", timeZone: timezone,
  })
}

export function formatUsageTimestamp(timestamp: string, timezone: string, locale: string) {
  return new Date(timestamp).toLocaleString(locale, { timeZone: timezone })
}
