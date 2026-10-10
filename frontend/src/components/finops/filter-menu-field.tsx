import type { LucideIcon } from "lucide-react"

import {
  Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue,
} from "../ui/select"
import { usageSelection } from "../../lib/usage-filter-scope"

const ALL_OPTION = "__all__"

export function FilterMenuField({
  label,
  icon: Icon,
  value,
  options,
  onChange,
  allowAll = true,
  active,
}: {
  label: string
  icon: LucideIcon
  value?: string
  options: Array<{ value: string; label: string }>
  onChange: (value: string | undefined) => void
  allowAll?: boolean
  active?: boolean
}) {
  const items = allowAll ? [{ value: ALL_OPTION, label: "全部" }, ...options] : options
  return <Select items={items} value={value ?? ALL_OPTION}
    onValueChange={next => onChange(next === ALL_OPTION || next === null ? undefined : next)}>
    <SelectTrigger size="sm" aria-label={label} className="finops-filter-select"
      data-active={(active ?? Boolean(value)) || undefined}>
      <Icon aria-hidden="true" /><SelectValue />
    </SelectTrigger>
    <SelectContent align="start" alignItemWithTrigger={false} className="finops-filter-select-options">
      <SelectGroup><SelectLabel>{label}</SelectLabel>
        {items.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}
      </SelectGroup>
    </SelectContent>
  </Select>
}

export function MultiSelectFilterField({
  label, icon: Icon, value, options, onChange, active, translateValues = false,
}: {
  label: string
  icon: LucideIcon
  value: string[]
  options: Array<{ value: string; label: string }>
  onChange: (value: string[]) => void
  active?: boolean
  translateValues?: boolean
}) {
  const items = [{ value: ALL_OPTION, label: "全部" }, ...options]
  const selected = options.filter(row => value.includes(row.value))
  const text = selected.length ? selected.map(row => row.label).join(", ") : "全部"
  return <Select multiple items={items} value={value.length ? value : [ALL_OPTION]}
    onValueChange={next => onChange(next.includes(ALL_OPTION) && value.length > 0
      ? [] : usageSelection(next.filter(item => item !== ALL_OPTION)))}>
    <SelectTrigger size="sm" aria-label={label} title={text} className="finops-filter-select"
      data-active={(active ?? value.length > 0) || undefined}>
      <Icon aria-hidden="true" />
      <span className="finops-filter-label">{label}</span>
      <SelectValue data-no-localize={!translateValues && selected.length > 0 || undefined}>
        {selected.length > 1 ? `${selected[0].label} +${selected.length - 1}` : text}
      </SelectValue>
    </SelectTrigger>
    <SelectContent align="start" alignItemWithTrigger={false} className="finops-filter-select-options">
      <SelectGroup><SelectLabel>{label}</SelectLabel>
        {items.map(option => <SelectItem key={option.value} value={option.value}
          title={option.label} data-no-localize={!translateValues && option.value !== ALL_OPTION || undefined}>
          {option.label}
        </SelectItem>)}
      </SelectGroup>
    </SelectContent>
  </Select>
}
