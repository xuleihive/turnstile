import type { ComponentProps } from "react"

import { cn } from "../../lib/utils"

export function Chip({ className, ...props }: ComponentProps<"span">) {
  return <span {...props} data-slot="chip" className={cn(
    "inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-background px-2 py-0.5 text-xs font-medium leading-4 whitespace-nowrap [&_svg]:shrink-0",
    className,
  )} />
}