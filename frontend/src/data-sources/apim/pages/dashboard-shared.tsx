import type { ReactNode } from "react"
import { AlertTriangle, RefreshCw, Search } from "lucide-react"

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../../components/ui/select"
import { getIntlLocale } from "../../../locales/index"
import type { EnterpriseEntity } from "../types"

const ALL_OPTION = "__all__"

export const compact = {
  format: (value: number) => new Intl.NumberFormat(getIntlLocale(), {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value),
}
export const currency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 4,
})
// Single requests cost fractions of a cent; match the invocation page's six decimals.
export const requestCost = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 6,
})
export const decimal = {
  format: (value: number) => new Intl.NumberFormat(getIntlLocale(), {
    maximumFractionDigits: 1,
  }).format(value),
}

export function formatLatency(milliseconds: number) {
  return milliseconds >= 1_000
    ? `${decimal.format(milliseconds / 1_000)} s`
    : `${Math.round(milliseconds)} ms`
}

export function queryError(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

export function LoadingState({ label = "正在加载用量数据" }: { label?: string }) {
  return <div className="finops-state"><RefreshCw className="spin" size={18} />{label}</div>
}

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="finops-state empty"><Search size={22} /><b>{title}</b><span>{detail}</span></div>
}

export function ErrorState({ error }: { error: unknown }) {
  return <div className="finops-state error"><AlertTriangle size={18} /><b>数据接口不可用</b><span>{queryError(error)}</span></div>
}

export function FilterSelect({ label, value, items, onChange, allowAll = true }: {
  label: string
  value?: string
  items: EnterpriseEntity[]
  onChange: (value: string | undefined) => void
  allowAll?: boolean
}) {
  const selected = items.find((item) => item.id === value)
  return <div className="finops-filter"><span>{label}</span><Select value={value ?? (allowAll ? ALL_OPTION : undefined)} onValueChange={(next) => onChange(next == null || next === ALL_OPTION ? undefined : next)}><SelectTrigger aria-label={label} title={selected?.name ?? (allowAll ? "全部" : undefined)}><SelectValue>{selected?.name ?? (allowAll ? "全部" : null)}</SelectValue></SelectTrigger><SelectContent align="start" alignItemWithTrigger={false}>{allowAll && <SelectItem value={ALL_OPTION}>全部</SelectItem>}{items.map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectContent></Select></div>
}

export function PanelTitle({ title, meta, action }: { title: string; meta?: string; action?: ReactNode }) {
  return <header className="finops-panel-title"><h2>{title}</h2>{(meta || action) && <div className="finops-panel-title-actions">{meta && <span className="finops-panel-title-meta">{meta}</span>}{action}</div>}</header>
}
