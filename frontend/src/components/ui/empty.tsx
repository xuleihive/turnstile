import type { ComponentProps } from "react"
import { cn } from "../../lib/utils"

export function Empty({ className, ...props }: ComponentProps<"div">) {
  return <div data-slot="empty" className={cn("ui-empty", className)} {...props} />
}
export function EmptyTitle(props: ComponentProps<"h3">) {
  return <h3 data-slot="empty-title" {...props} />
}
export function EmptyContent(props: ComponentProps<"div">) {
  return <div data-slot="empty-content" {...props} />
}
