import { ChartNoAxesColumn, ChartSpline, Grid3X3 } from "lucide-react"

import "./chart-mode-toggle.css"

export type ChartMode = "bar" | "line" | "heatmap"

const chartModes: Array<{ value: ChartMode; label: string; icon: typeof ChartNoAxesColumn }> = [
  { value: "bar", label: "柱状图", icon: ChartNoAxesColumn },
  { value: "line", label: "折线图", icon: ChartSpline },
  { value: "heatmap", label: "热力图", icon: Grid3X3 },
]

export function ChartModeToggle({ value, onChange, modes }: {
  value: ChartMode
  onChange: (value: ChartMode) => void
  modes?: readonly ChartMode[]
}) {
  return <div className="chart-mode-toggle" role="group" aria-label="图表类型">
    {chartModes.filter((item) => !modes || modes.includes(item.value)).map((item) => {
      const Icon = item.icon
      return <button
        key={item.value}
        type="button"
        className={value === item.value ? "active" : ""}
        aria-label={item.label}
        title={item.label}
        aria-pressed={value === item.value}
        onClick={() => onChange(item.value)}
      ><Icon size={14} /></button>
    })}
  </div>
}
