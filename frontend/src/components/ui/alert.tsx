import type { ComponentProps } from "react"
import { cn } from "../../lib/utils"

export function Alert({ className, ...props }: ComponentProps<"div">) {
  return <div role="alert" data-slot="alert" className={cn("ui-alert", className)} {...props} />
}

export function AlertTitle({ className, ...props }: ComponentProps<"div">) {
  return <div data-slot="alert-title" className={cn(className)} {...props} />
}

export function AlertDescription({ className, ...props }: ComponentProps<"div">) {
  return <div data-slot="alert-description" className={cn(className)} {...props} />
}
