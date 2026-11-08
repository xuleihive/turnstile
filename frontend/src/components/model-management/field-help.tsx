import { Tooltip } from "@base-ui/react/tooltip"
import { CircleHelp } from "lucide-react"
import { useState, type ReactNode } from "react"

export function FieldHelp({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false)

  return <Tooltip.Provider delay={200} closeDelay={100}>
    <Tooltip.Root open={open} onOpenChange={setOpen}>
      <Tooltip.Trigger type="button" closeOnClick={false} className="field-help-trigger" aria-label="查看说明" onClick={() => setOpen(true)}>
        <CircleHelp size={13} aria-hidden="true" />
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Positioner side="top" sideOffset={6} className="field-help-positioner">
          <Tooltip.Popup className="field-help-popup">{children}</Tooltip.Popup>
        </Tooltip.Positioner>
      </Tooltip.Portal>
    </Tooltip.Root>
  </Tooltip.Provider>
}