import { createContext, useContext, useEffect, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"

import {
  DATA_REFRESH_STORAGE_KEY, dataRefreshLabel, normalizeDataRefreshMinutes,
  persistDataRefreshMinutes, readDataRefreshMinutes, refreshActiveDataQueries,
  startDataRefresh, type DataRefreshMinutes,
} from "../lib/data-refresh"

type DataRefreshContextValue = {
  minutes: DataRefreshMinutes
  label: string
  persisted: boolean
  setMinutes: (minutes: DataRefreshMinutes) => void
}

const DataRefreshContext = createContext<DataRefreshContextValue | null>(null)

export function DataRefreshProvider({ children }: { children: React.ReactNode }) {
  const client = useQueryClient()
  const [minutes, setMinutesState] = useState(readDataRefreshMinutes)
  const [persisted, setPersisted] = useState(true)

  useEffect(() => startDataRefresh(minutes * 60_000, () => {
    void refreshActiveDataQueries(client)
  }, {
    now: Date.now,
    visible: () => document.visibilityState !== "hidden",
    schedule: (callback, delay) => {
      const timer = window.setTimeout(callback, delay)
      return () => window.clearTimeout(timer)
    },
    subscribe: (callback) => {
      document.addEventListener("visibilitychange", callback)
      window.addEventListener("focus", callback)
      return () => {
        document.removeEventListener("visibilitychange", callback)
        window.removeEventListener("focus", callback)
      }
    },
  }), [client, minutes])

  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key !== DATA_REFRESH_STORAGE_KEY && event.key !== null) return
      setMinutesState(normalizeDataRefreshMinutes(event.newValue))
      setPersisted(true)
    }
    window.addEventListener("storage", sync)
    return () => window.removeEventListener("storage", sync)
  }, [])

  const setMinutes = (next: DataRefreshMinutes) => {
    const normalized = normalizeDataRefreshMinutes(next)
    setPersisted(persistDataRefreshMinutes(normalized))
    setMinutesState(normalized)
    if (normalized !== minutes) void refreshActiveDataQueries(client)
  }

  return <DataRefreshContext.Provider value={{ minutes, label: dataRefreshLabel(minutes), persisted, setMinutes }}>
    {children}
  </DataRefreshContext.Provider>
}

export function useDataRefresh() {
  const context = useContext(DataRefreshContext)
  if (!context) throw new Error("useDataRefresh must be used within DataRefreshProvider")
  return context
}
