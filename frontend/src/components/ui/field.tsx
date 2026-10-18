import type { ComponentProps } from "react"
import { Field as FieldPrimitive } from "@base-ui/react/field"
import { cn } from "../../lib/utils"

export function FieldGroup({ className, ...props }: ComponentProps<"div">) {
  return <div data-slot="field-group" className={cn("form-field-group", className)} {...props} />
}

export function Field({ className, ...props }: FieldPrimitive.Root.Props) {
  return <FieldPrimitive.Root data-slot="field" className={cn("form-field", className)} {...props} />
}

export function FieldLabel({ className, ...props }: FieldPrimitive.Label.Props) {
  return <FieldPrimitive.Label data-slot="field-label" className={cn(className)} {...props} />
}

export function FieldError({ className, ...props }: FieldPrimitive.Error.Props) {
  return <FieldPrimitive.Error data-slot="field-error" className={cn(className)} {...props} />
}

export function FieldSet({ className, ...props }: ComponentProps<"fieldset">) {
  return <fieldset data-slot="field-set" className={cn("form-field-set", className)} {...props} />
}

export function FieldLegend({ className, ...props }: ComponentProps<"legend">) {
  return <legend data-slot="field-legend" className={cn(className)} {...props} />
}
