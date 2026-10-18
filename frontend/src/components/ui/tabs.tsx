import { Tabs as TabsPrimitive } from "@base-ui/react/tabs"
import { cn } from "../../lib/utils"

export const Tabs = TabsPrimitive.Root
export function TabsList({ className, ...props }: TabsPrimitive.List.Props) {
  return <TabsPrimitive.List data-slot="tabs-list" className={cn("ui-tabs-list", className)} {...props} />
}
export function TabsTrigger({ className, ...props }: TabsPrimitive.Tab.Props) {
  return <TabsPrimitive.Tab data-slot="tabs-trigger" className={cn("ui-tabs-trigger", className)} {...props} />
}
export function TabsContent({ className, ...props }: TabsPrimitive.Panel.Props) {
  return <TabsPrimitive.Panel data-slot="tabs-content" className={cn("ui-tabs-content", className)} {...props} />
}
