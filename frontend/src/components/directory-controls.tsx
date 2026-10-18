import { useEffect } from "react"
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "./ui/select"
import { DIRECTORY_BEFORE_LEAVE_EVENT } from "../lib/navigation"

export function confirmDirectoryLeave() {
  return window.dispatchEvent(new Event(DIRECTORY_BEFORE_LEAVE_EVENT, { cancelable: true }))
}

export function useDirectoryLeaveGuard(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return
    const guard = (event: Event) => {
      if (!window.confirm("放弃未保存的更改？")) event.preventDefault()
    }
    const unload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = "" }
    window.addEventListener(DIRECTORY_BEFORE_LEAVE_EVENT, guard)
    window.addEventListener("beforeunload", unload)
    return () => {
      window.removeEventListener(DIRECTORY_BEFORE_LEAVE_EVENT, guard)
      window.removeEventListener("beforeunload", unload)
    }
  }, [dirty])
}

export function DirectorySelect({ label, value, items, onChange, disabled = false, required = false }: {
  label: string; value: string; items: Array<{ value: string; label: string }>
  onChange: (value: string) => void; disabled?: boolean; required?: boolean
}) {
  const selectedLabel = items.find((item) => item.value === value)?.label ?? label
  return <Select items={items} value={value} onValueChange={(next) => next && onChange(next)} disabled={disabled} required={required}>
    <SelectTrigger aria-label={label} aria-required={required || undefined} title={selectedLabel}>
      <SelectValue>{selectedLabel}</SelectValue>
    </SelectTrigger>
    <SelectContent className="directory-select-content" alignItemWithTrigger={false}><SelectGroup>
      {items.map((item) => <SelectItem key={item.value} value={item.value} title={item.label}>{item.label}</SelectItem>)}
    </SelectGroup></SelectContent>
  </Select>
}
