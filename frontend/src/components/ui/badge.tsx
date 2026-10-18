import type { ComponentProps } from "react"
import { cn } from "../../lib/utils"

export function Badge({ className, variant = "secondary", ...props }: ComponentProps<"span"> & {
  variant?: "default" | "secondary" | "outline" | "destructive"
}) {
  return <span data-slot="badge" data-variant={variant} className={cn("ui-badge", className)} {...props} />
}
