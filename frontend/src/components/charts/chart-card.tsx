import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import { CircleHelp, Pin, PinOff, RotateCw } from "lucide-react"
import type { ReactNode } from "react"
import type { ChartSpec } from "../assistant/types"
import { getIntlLocale } from "../../locales/index"
import { FinOpsChartTooltip } from "../finops/chart-tooltip"
import { ResizableTable } from "../ui/resizable-table"

const SERIES_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
]

const compact = (value: number) =>
  new Intl.NumberFormat(getIntlLocale(), { notation: "compact", maximumFractionDigits: 1 })
    .format(value)

/** Currency needs more precision than a compact number: $0.03 must not render as $0. */
const formatValue = (value: number, unit: string) => {
  if (unit === "USD") return `$${value < 1 ? value.toPrecision(2) : value.toFixed(2)}`
  if (unit === "%") return `${value.toFixed(2)}%`
  if (unit === "ms") return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`
  return new Intl.NumberFormat(getIntlLocale()).format(value)
}

const bucketLabel = (value: string) => {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat(getIntlLocale(), { month: "2-digit", day: "2-digit" }).format(parsed)
}

const categoryTickFontSize = 11
const categoryTickInset = 4
let categoryTickMeasureContext: CanvasRenderingContext2D | null = null

const measureCategoryTick = (value: string) => {
  if (typeof document === "undefined") return value.length * 7
  if (!categoryTickMeasureContext) {
    const context = document.createElement("canvas").getContext("2d")
    if (context) context.font = `400 ${categoryTickFontSize}px ${getComputedStyle(document.body).fontFamily}`
    categoryTickMeasureContext = context
  }
  return categoryTickMeasureContext?.measureText(value).width ?? value.length * 7
}

const ellipsizeCategoryTick = (value: string, maxWidth: number) => {
  if (measureCategoryTick(value) <= maxWidth) return value
  const characters = [...value]
  let low = 0
  let high = characters.length
  while (low < high) {
    const middle = Math.ceil((low + high) / 2)
    if (measureCategoryTick(`${characters.slice(0, middle).join("")}…`) <= maxWidth) low = middle
    else high = middle - 1
  }
  return `${characters.slice(0, low).join("")}…`
}

function CategoryAxisTick({ x = 0, y = 0, payload }: {
  x?: number
  y?: number
  payload?: { value?: string | number }
}) {
  const value = String(payload?.value ?? "")
  const label = ellipsizeCategoryTick(value, Math.max(0, x - categoryTickInset))
  return <g>
    <title>{value}</title>
    <text
      x={x}
      y={y}
      dominantBaseline="central"
      textAnchor="end"
      fill="var(--muted-foreground)"
      fontSize={categoryTickFontSize}
    >{label}</text>
  </g>
}

function ChartBody({ chart, fill = false }: { chart: ChartSpec; fill?: boolean }) {
  if (chart.rows.length === 0) {
    return <div className="assistant-chart-empty">
      该范围内没有数据。可能是时间范围太窄，或筛选条件过严。
    </div>
  }

  if (chart.kind === "kpi") {
    return <div className="assistant-kpi-strip">
      {chart.rows.map((row) => <div className="assistant-kpi" key={String(row[chart.category_key])}>
        <span>{String(row[chart.category_key])}</span>
        <b>{formatValue(Number(row.value), String(row.unit ?? ""))}</b>
      </div>)}
    </div>
  }

  if (chart.kind === "table") {
    return <div className={fill ? "assistant-table-scroll fill" : "assistant-table-scroll"}>
      <ResizableTable className="assistant-table" minWidths={[120, ...chart.series.map(() => 90)]}>
        <thead>
          <tr>
            <th><span>{chart.category_label}</span></th>
            {chart.series.map((series) => <th key={series.key}><span>{series.label}</span></th>)}
          </tr>
        </thead>
        <tbody>
          {chart.rows.map((row) => <tr key={String(row[chart.category_key])}>
            <td>{String(row[chart.category_key])}</td>
            {chart.series.map((series) => <td key={series.key}>
              {formatValue(Number(row[series.key] ?? 0), chart.unit)}
            </td>)}
          </tr>)}
        </tbody>
      </ResizableTable>
    </div>
  }

  /* A fixed pixel height is right in the assistant panel, where the card is sized by its
     content. In a report whose cards are stretched to a shared row height, or dragged to
     an explicit one, it would pin the plot and leave the extra room blank. */
  const height = chart.kind === "bar" ? Math.max(160, chart.rows.length * 30 + 44) : 220
  const plotClass = fill ? "assistant-chart-plot fill" : "assistant-chart-plot"
  const plotStyle = fill ? undefined : { height }

  if (chart.kind === "bar") {
    return <div className={plotClass} style={plotStyle}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={chart.rows}
          layout="vertical"
          barCategoryGap="24%"
          margin={{ left: 0, right: 16, top: 4, bottom: 0 }}
        >
          <CartesianGrid horizontal={false} stroke="var(--border)" />
          <XAxis type="number" tickFormatter={compact} tick={{ fontSize: 11 }} height={26} />
          {/* interval=0 is required: Recharts thins category ticks by default, which
              silently drops labels and leaves unidentifiable bars. */}
          <YAxis
            type="category"
            dataKey={chart.category_key}
            width={116}
            interval={0}
            tick={<CategoryAxisTick />}
          />
          <Tooltip
            cursor={false}
            content={<FinOpsChartTooltip
              valueFormatter={(value) => formatValue(Number(value), chart.unit)}
            />}
          />
          {chart.series.map((series, index) => <Bar
            key={series.key}
            dataKey={series.key}
            name={series.label}
            fill={SERIES_COLORS[index % SERIES_COLORS.length]}
            radius={[0, 4, 4, 0]}
            maxBarSize={18}
            isAnimationActive={false}
          />)}
        </BarChart>
      </ResponsiveContainer>
    </div>
  }

  return <div className={plotClass} style={plotStyle}>
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={chart.rows} margin={{ left: 0, right: 12, top: 8, bottom: 0 }}>
        <CartesianGrid vertical={false} stroke="var(--border)" />
        <XAxis
          dataKey={chart.category_key}
          tickFormatter={bucketLabel}
          tick={{ fontSize: 11 }}
          height={26}
        />
        <YAxis tickFormatter={compact} tick={{ fontSize: 11 }} width={48} />
        <Tooltip
          cursor={false}
          content={<FinOpsChartTooltip
            labelFormatter={(value) => bucketLabel(String(value))}
            valueFormatter={(value) => formatValue(Number(value), chart.unit)}
          />}
        />
        {chart.series.map((series, index) => <Line
          key={series.key}
          // Straight segments; the points are bucketed measurements and a curve between them
          // is drawn, not observed. Matches the dashboard charts this mirrors.
          type="linear"
          dataKey={series.key}
          name={series.label}
          stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />)}
      </LineChart>
    </ResponsiveContainer>
  </div>
}

export function ChartCard({
  chart,
  originalQuestion,
  pinned = false,
  busy = false,
  fillHeight = false,
  dragHandle,
  onPin,
  onUnpin,
  onRefresh,
}: {
  chart: ChartSpec
  originalQuestion?: string
  pinned?: boolean
  busy?: boolean
  fillHeight?: boolean
  /** Rendered last in the action row. The card does not own reordering -- only the list
   *  that holds it knows what "next" means -- so the handle is passed in. */
  dragHandle?: ReactNode
  onPin?: () => void
  onUnpin?: () => void
  onRefresh?: () => void
}) {
  return <figure className={fillHeight ? "assistant-chart fill" : "assistant-chart"}>
    <div className="assistant-chart-head">
      <div className="assistant-chart-title">
        <b>{chart.title}</b>
        <span>{chart.time_range_label}</span>
      </div>
      <div className="assistant-chart-actions">
        {onRefresh && <button
          type="button"
          className="assistant-chart-action"
          onClick={onRefresh}
          disabled={busy}
          title="刷新数据"
          aria-label="刷新数据"
        ><RotateCw size={13} className={busy ? "assistant-spin" : undefined} /></button>}
        {originalQuestion && <button
          type="button"
          className="assistant-chart-action assistant-chart-action-question"
          /* What a person typed, not product copy. The phrase translator substitutes every
             fragment it recognises and leaves the rest, so an unprotected Chinese question
             came out of English mode with its nouns and its "7 days" swapped to English and
             the grammar still Chinese — the person's own words, half-rewritten. */
          data-no-localize
          aria-label={`Original question: ${originalQuestion}`}
        >
          <CircleHelp size={13} />
          <span className="assistant-chart-tooltip" role="tooltip">
            {`Original question: ${originalQuestion}`}
          </span>
        </button>}
        {onPin && !pinned && <button
          type="button"
          className="assistant-chart-action"
          onClick={onPin}
          disabled={busy}
          title="固定到导航栏"
          aria-label="固定到导航栏"
        ><Pin size={13} /></button>}
        {onUnpin && pinned && <button
          type="button"
          className="assistant-chart-action"
          onClick={onUnpin}
          disabled={busy}
          title="取消固定"
          aria-label="取消固定"
        ><PinOff size={13} /></button>}
        {dragHandle}
      </div>
    </div>
    <ChartBody chart={chart} fill={fillHeight} />
    {/* PRD 5.4: the basis, the unit and when the data was read are part of the chart,
        not commentary around it. A pinned copy is read months later by someone who
        never saw the question. */}
    <figcaption className="assistant-chart-basis">
      <span>{chart.basis}</span>
      <span>
        单位：{chart.unit || "—"} · 数据更新于 {new Intl.DateTimeFormat(getIntlLocale(), {
          dateStyle: "short",
          timeStyle: "short",
        }).format(new Date(chart.generated_at))}
      </span>
    </figcaption>
  </figure>
}
