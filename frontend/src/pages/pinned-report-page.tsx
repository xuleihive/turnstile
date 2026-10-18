import { useRef, useState } from "react"
import type { PointerEvent as ReactPointerEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, GripVertical, Loader2, PanelLeft, Pin, RefreshCw } from "lucide-react"
import { assistantApi } from "../components/assistant/api"
import { useLocale } from "../locales/index"
import { usePinnedCardResize } from "../components/reports/use-pinned-card-resize"
import { pinnedChartsKey, pinnedChartsQuery } from "../components/assistant/queries"
import { useAuth } from "../providers/auth-provider"
import { useTimezone } from "../providers/timezone-provider"
import type { PinnedReport, PinnedReportLayout } from "../components/assistant/types"
import { Button } from "../components/ui/button"
import { Switch } from "../components/ui/switch"
import { ChartCard } from "../components/charts/chart-card"

/** The existing saved report viewer, embedded in the report center. */
export function PinnedReportPage({
  chartId,
  onToggleSidebar,
  onDeleted,
  embedded = false,
}: {
  chartId: string
  onToggleSidebar: () => void
  onDeleted: (id: string) => void
  embedded?: boolean
}) {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const owner = user?.email ?? ""
  const { timezone } = useTimezone()
  const { locale } = useLocale()
  const [layout, setLayout] = useState<"list" | "grid" | "mixed">("mixed")
  const [refreshedAt, setRefreshedAt] = useState<string | null>(null)
  const gridRef = useRef<HTMLDivElement | null>(null)
  const layoutWriteVersion = useRef(0)
  const [dragging, setDragging] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<string | null>(null)
  // The drop target is read when the pointer is released, by which time the state value
  // captured in the listener's closure is stale. The ref is what the release reads.
  const dropTargetRef = useRef<string | null>(null)
  const pins = useQuery(pinnedChartsQuery(owner))
  const pinned = pins.data?.find((item) => item.id === chartId)

  // `null` means "not editing". One value rather than a draft string plus an open flag,
  // which can disagree about whether the input is on screen.
  const [draft, setDraft] = useState<string | null>(null)
  // Escape cancels, and it has to say so through a ref: `blur()` fires synchronously, so
  // a `setDraft(null)` scheduled a line earlier is not yet visible to the blur handler
  // and it would commit the very edit the reader just abandoned.
  const cancelRename = useRef(false)

  const replaceReport = (updated: PinnedReport) => {
    queryClient.setQueryData(pinnedChartsKey(owner), (current: typeof pins.data) =>
      current?.map((item) => item.id === updated.id ? updated : item))
  }

  const saveLayout = useMutation({
    mutationFn: ({ next }: {
      next: PinnedReportLayout
      previous: PinnedReport
      version: number
    }) => assistantApi.saveLayout(chartId, next),
    scope: { id: `pinned-report-layout-${chartId}` },
    onSuccess: (updated, { version }) => {
      if (version === layoutWriteVersion.current) replaceReport(updated)
    },
    onError: (_error, { previous, version }) => {
      if (version === layoutWriteVersion.current) replaceReport(previous)
    },
  })

  const commitLayout = (next: PinnedReportLayout) => {
    if (!pinned?.can_manage) return
    const previous = pinned
    const version = ++layoutWriteVersion.current
    replaceReport({ ...pinned, layout: next })
    saveLayout.mutate({ next, previous, version })
  }

  const { canResize, startResize, reset, styleFor, isResized } = usePinnedCardResize(
    gridRef,
    pinned?.layout,
    commitLayout,
  )

  const rename = useMutation({
    // The endpoint replaces both fields and `description` defaults to empty, so sending
    // the title on its own would silently erase the description.
    mutationFn: (title: string) =>
      assistantApi.renamePinned(chartId, title, pinned?.description ?? ""),
    // Writing to the shared cache also renames the directory entry, which is the same
    // report under a different name and would otherwise disagree with the heading.
    onSuccess: replaceReport,
  })

  const visibility = useMutation({
    mutationFn: (value: "private" | "public") =>
      assistantApi.setVisibility(chartId, value),
    onSuccess: replaceReport,
  })

  const commitTitle = () => {
    if (cancelRename.current) {
      cancelRename.current = false
      setDraft(null)
      return
    }
    const next = draft?.trim() ?? ""
    setDraft(null)
    // An empty name is rejected by the server, and an unchanged one is not a write.
    if (!pinned || !next || next === pinned.title) return
    rename.mutate(next)
  }

  const refresh = useMutation({
    mutationFn: () => assistantApi.refreshPinned(chartId, timezone, locale),
    onSuccess: (updated) => {
      replaceReport(updated)
      setRefreshedAt(updated.charts[0]?.chart.generated_at ?? null)
    },
  })

  const unpin = useMutation({
    mutationFn: () => assistantApi.unpin(chartId),
    // This used to assign `window.location.search`, which is a full document load: the
    // JS context was destroyed and the app re-bootstrapped, re-downloading its modules
    // and re-fetching 12 endpoints to show a page it already had in cache. It also
    // discarded the list refetch it had just paid for one second earlier. The owner
    // drops the row from the cache and navigates in place instead.
    onSuccess: () => onDeleted(chartId),
  })

  // Removing a chart and removing the report are different acts, so the last chart's
  // button deletes the whole report rather than failing the server's 409.
  const removeChart = useMutation({
    mutationFn: (id: string) =>
      assistantApi.removeChart(chartId, id),
    onSuccess: replaceReport,
  })

  const busy = refresh.isPending || unpin.isPending || removeChart.isPending
    || visibility.isPending

  const reorder = useMutation({
    mutationFn: ({ order }: { order: string[]; previous: PinnedReport }) =>
      assistantApi.reorderCharts(chartId, order),
    onSuccess: replaceReport,
    // Roll back from the snapshot the drag took, not from a refetch. Invalidating was
    // measured not to refetch here at all, so the rejected order stayed on screen under a
    // message claiming it had been restored. It is also the wrong mechanism: if the
    // server is unreachable the refetch fails too, and the wrong order survives.
    onError: (_error, { previous }) => replaceReport(previous),
  })

  /** Move `sourceId` to where `targetId` currently sits, then persist the whole order.
   *
   * Reordering is pointer-driven rather than HTML5 drag-and-drop. `setDragImage` only
   * takes a snapshot at dragstart, so the card itself never moved -- the cursor towed a
   * frozen bitmap while the real card sat still, which reads as a broken gesture.
   */
  const moveChart = (sourceId: string, targetId: string) => {
    if (!pinned || sourceId === targetId) return
    const order = pinned.charts.map((chart) => chart.id)
    const from = order.indexOf(sourceId)
    const to = order.indexOf(targetId)
    if (from < 0 || to < 0) return
    order.splice(to, 0, ...order.splice(from, 1))
    // Applied to the cache first so the card lands under the cursor immediately; the
    // request confirms it. Waiting for the round trip would make a drop feel dropped.
    replaceReport({
      ...pinned,
      charts: order.map((id) => pinned.charts.find((chart) => chart.id === id)!),
    })
    reorder.mutate({ order, previous: pinned })
  }

  /**
   * Drag a card by its grip.
   *
   * The card's `transform` is written straight onto the node instead of through state:
   * a pointermove fires dozens of times a second and each React render would rebuild the
   * chart inside the card. Only the drop target goes through state, and only when it
   * actually changes.
   *
   * The card keeps its grid cell while it moves, so the layout underneath stays still and
   * the ring on the card being pointed at is what says where it will land.
   */
  const startReorder = (event: ReactPointerEvent, id: string) => {
    const card = (event.currentTarget as HTMLElement).closest<HTMLElement>(".pinned-report-item")
    if (!card) return
    event.preventDefault()

    const startX = event.clientX
    const startY = event.clientY
    // Without this the card sits under the cursor and hit-testing only ever finds itself.
    card.style.pointerEvents = "none"
    card.style.zIndex = "30"
    setDragging(id)

    const onMove = (moved: globalThis.PointerEvent) => {
      card.style.transform =
        `translate(${moved.clientX - startX}px, ${moved.clientY - startY}px)`
      const under = document
        .elementFromPoint(moved.clientX, moved.clientY)
        ?.closest<HTMLElement>(".pinned-report-item")
      const target = under?.dataset.cardId ?? null
      if (target !== dropTargetRef.current) {
        dropTargetRef.current = target
        setDropTarget(target)
      }
    }

    const onUp = () => {
      document.removeEventListener("pointermove", onMove)
      document.removeEventListener("pointerup", onUp)
      card.style.transform = ""
      card.style.pointerEvents = ""
      card.style.zIndex = ""
      document.body.style.cursor = ""
      document.body.style.userSelect = ""
      const target = dropTargetRef.current
      dropTargetRef.current = null
      setDropTarget(null)
      setDragging(null)
      if (target) moveChart(id, target)
    }

    document.addEventListener("pointermove", onMove)
    document.addEventListener("pointerup", onUp)
    document.body.style.cursor = "grabbing"
    document.body.style.userSelect = "none"
  }

  /**
   * Whether this card needs twice the default width.
   *
   * Tables used to be pinned to the full row, which gave a two-column MODEL/REQUESTS table
   * the entire 1481px while a dense one got the same. Width is a property of the content,
   * so it is answered from the column count: category column + one per series. The
   * stylesheet turns the flag into a span appropriate to the current container width,
   * because "twice the default" is 12 columns on a two-up row and 6 on a four-up one.
   */
  const needsWidth = (chart: PinnedReport["charts"][number]["chart"]) =>
    chart.kind === "table" && chart.series.length + 1 >= 5

  return <div className="finops-workspace pinned-report-workspace" data-embedded={embedded || undefined}>
    <header className="finops-header">
      <div>
        {!embedded && <Button
          variant="ghost"
          size="icon-sm"
          className="finops-sidebar-trigger"
          aria-label="切换导航栏"
          title="切换导航栏"
          onClick={onToggleSidebar}
        ><PanelLeft size={16} /></Button>}
        {!embedded && <span className="finops-header-icon"><Pin size={17} /></span>}
        {/* The title is the report's identity, so it is edited where it is read rather
            than behind a dialog for one field. Only a loaded report is editable: there is
            nothing to name while the page is still fetching, or once it is gone. */}
        <h1>
          {!pinned && "固定报表"}
          {pinned && draft === null && pinned.can_manage && <button
            type="button"
            className="pinned-title-button"
            title="重命名报表"
            onClick={() => setDraft(pinned.title)}
          >{/* The name is the reader's own words, so it is exempt from translation: the
                provider substitutes dictionary phrases inside any text node it walks, and
                a Chinese name read in English came out as "DepartmentRequests量趋势".
                The marker sits on the span rather than the button so the button's own
                title attribute, which is product copy, still translates. */}
            <span data-no-localize>{pinned.title}</span>
          </button>}
          {pinned && draft === null && !pinned.can_manage && <span data-no-localize>
            {pinned.title}
          </span>}
          {pinned && draft !== null && pinned.can_manage && <input
            className="pinned-title-input"
            aria-label="报表名称"
            value={draft}
            maxLength={120}
            autoFocus
            onFocus={(event) => event.currentTarget.select()}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              // Both keys leave through blur so there is a single commit path.
              if (event.key === "Enter") { event.preventDefault(); event.currentTarget.blur() }
              if (event.key === "Escape") { cancelRename.current = true; event.currentTarget.blur() }
            }}
            onBlur={commitTitle}
          />}
        </h1>
      </div>
      <div className="pinned-report-actions">
        {pinned?.can_manage && <label className="pinned-visibility-control">
          <span>公开</span>
          <Switch
            checked={pinned.visibility === "public"}
            disabled={visibility.isPending}
            aria-label="公开报表"
            onCheckedChange={(checked) => visibility.mutate(checked ? "public" : "private")}
          />
        </label>}
        <div className="pinned-layout-switcher" role="group" aria-label="报表布局">
          <button
            type="button"
            className={layout === "list" ? "active" : ""}
            aria-pressed={layout === "list"}
            onClick={() => setLayout("list")}
          >列表</button>
          <button
            type="button"
            className={layout === "grid" ? "active" : ""}
            aria-pressed={layout === "grid"}
            onClick={() => setLayout("grid")}
          >网格</button>
          <button
            type="button"
            className={layout === "mixed" ? "active" : ""}
            aria-pressed={layout === "mixed"}
            onClick={() => setLayout("mixed")}
          >混排</button>
        </div>
        {pinned?.can_manage && <Button
          variant="ghost"
          size="icon-sm"
          className="finops-header-refresh"
          aria-label="刷新数据"
          title="刷新数据"
          disabled={refresh.isPending || !pinned}
          onClick={() => refresh.mutate()}
        ><RefreshCw className={refresh.isPending ? "spin" : undefined} size={15} /></Button>}
      </div>
    </header>

    <div className="pinned-report-body">
      <div className={`pinned-report-content pinned-layout-${layout}`} ref={gridRef}>
        {pins.isLoading && <div className="assistant-thinking">
          <Loader2 size={13} className="assistant-spin" /><span>加载报表…</span>
        </div>}

        {!pins.isLoading && !pinned && <div className="assistant-chart-empty">
          这个固定报表不存在，或者已经被取消固定。
        </div>}

        {pinned && <>
          {pinned.description && <p className="pinned-report-description">{pinned.description}</p>}
          {/* The heading still shows the old name, because the cache is only written on
              success. Saying so turns a change that looks lost into one that was refused. */}
          {rename.isError && <div className="assistant-error">
            <AlertTriangle size={13} />
            <span>重命名失败，报表名称没有改动。</span>
          </div>}
          {refresh.isError && <div className="assistant-error">
            <AlertTriangle size={13} />
            <span>刷新失败，下面显示的是上一次成功取到的数据。</span>
          </div>}
          {visibility.isError && <div className="assistant-error">
            <AlertTriangle size={13} /><span>公开设置保存失败</span>
          </div>}
          {unpin.isError && <div className="assistant-error">
            <AlertTriangle size={13} /><span>删除报表失败</span>
          </div>}
          {/* A failed reorder puts the cards back, which on its own looks like the drag
              simply did not take. Saying so is the difference between a bug and a
              refusal the reader can act on. */}
          {reorder.isError && <div className="assistant-error">
            <AlertTriangle size={13} />
            <span>顺序没能保存，已恢复为原来的顺序。</span>
          </div>}
          {saveLayout.isError && <div className="assistant-error">
            <AlertTriangle size={13} />
            <span>布局没能保存，已恢复为上一次保存的尺寸。</span>
          </div>}
          {pinned.charts.map((item, index) => <div
            key={item.id}
            className={`pinned-report-item pinned-item-${item.chart.kind} pinned-item-${layout}`}
            data-index={index}
            data-card-id={item.id}
            data-wide={needsWidth(item.chart) ? "" : undefined}
            data-resized={isResized(item.id) || undefined}
            data-dragging={dragging === item.id || undefined}
            data-drop-target={dropTarget === item.id && dragging !== item.id || undefined}
            style={styleFor(item.id)}
          >
            <ChartCard
              chart={item.chart}
              originalQuestion={item.original_question}
              pinned
              busy={busy}
              fillHeight
              dragHandle={pinned.can_manage && pinned.charts.length > 1 ? <span
                className="assistant-chart-action pinned-card-drag"
                role="button"
                tabIndex={-1}
                title="拖动调整卡片顺序"
                aria-label="拖动调整卡片顺序"
                onPointerDown={(event) => startReorder(event, item.id)}
              ><GripVertical size={13} /></span> : undefined}
              onUnpin={pinned.can_manage ? () => {
                if (pinned.charts.length === 1) unpin.mutate()
                else removeChart.mutate(item.id)
              } : undefined}
            />
            {/* Width is only meaningful once the grid has more than one track, so the
                handle is absent in a single column rather than present and inert. */}
            {pinned.can_manage && canResize && <span
              className="pinned-card-resize right"
              title="拖动调整宽度，双击恢复默认"
              onPointerDown={(event) => startResize(event, item.id, "width")}
              onDoubleClick={() => reset(item.id)}
            />}
            {pinned.can_manage && canResize && <span
              className="pinned-card-resize bottom"
              title="拖动调整高度，双击恢复默认"
              onPointerDown={(event) => startResize(event, item.id, "height")}
              onDoubleClick={() => reset(item.id)}
            />}
            {pinned.can_manage && canResize && <span
              className="pinned-card-resize corner"
              title="拖动调整宽高，双击恢复默认"
              onPointerDown={(event) => startResize(event, item.id, "both")}
              onDoubleClick={() => reset(item.id)}
            />}
          </div>)}
          {refreshedAt && <p className="pinned-report-question">本次已按原始查询条件重新取数。</p>}
        </>}
      </div>
    </div>
  </div>
}
