import {
  type CSSProperties,
  type HTMLAttributes,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type TableHTMLAttributes,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react"
import { createPortal } from "react-dom"

import { DEFAULT_MIN_COLUMN_WIDTH, preserveColumnWidths } from "./column-widths"

const MAX_COLUMN_WIDTH = 1600
const RESIZE_THRESHOLD = 4

type ResizeState = {
  activeColumn: number | null
  enabled: boolean
  overlay: ReactNode
  portals: ReactNode
  widths: number[] | null
}

function useResizableColumns({
  rootRef,
  headerSelector,
  minWidths = [],
  enabled,
  resizeAtAllWidths = false,
}: {
  rootRef: React.RefObject<HTMLElement | null>
  headerSelector: string
  minWidths?: readonly number[]
  enabled: boolean
  resizeAtAllWidths?: boolean
}): ResizeState {
  const [headerCells, setHeaderCells] = useState<HTMLElement[]>([])
  const [portalContainers, setPortalContainers] = useState<Map<HTMLElement, HTMLSpanElement>>(() => new Map())
  const [widths, setWidths] = useState<number[] | null>(null)
  const [activeColumn, setActiveColumn] = useState<number | null>(null)
  const defaultWidthsRef = useRef<number[] | null>(null)
  const lastVisibleWidthsRef = useRef<number[] | null>(null)
  const cleanupDragRef = useRef<(() => void) | null>(null)

  useLayoutEffect(() => {
    const header = rootRef.current?.querySelector<HTMLElement>(headerSelector)
    const nextCells = header ? Array.from(header.children).filter((cell): cell is HTMLElement => cell instanceof HTMLElement) : []
    const cellsChanged = headerCells.length !== nextCells.length
      || headerCells.some((cell, index) => cell !== nextCells[index])
    if (cellsChanged) cleanupDragRef.current?.()
    setHeaderCells((current) => current.length === nextCells.length && current.every((cell, index) => cell === nextCells[index])
      ? current
      : nextCells)
    if ((cellsChanged && headerCells.length > 0) || (widths && widths.length !== nextCells.length)) {
      setWidths(null)
      defaultWidthsRef.current = null
      lastVisibleWidthsRef.current = null
    }
  })

  useLayoutEffect(() => {
    const containers = new Map<HTMLElement, HTMLSpanElement>()
    const restorations = headerCells.map((cell) => {
      const previousPosition = cell.style.position
      const previousRole = cell.getAttribute("role")
      if (getComputedStyle(cell).position === "static") cell.style.position = "relative"
      if (cell.tagName !== "TH" && !previousRole) cell.setAttribute("role", "columnheader")
      cell.classList.add("resizable-column-header")
      const container = document.createElement("span")
      container.style.display = "contents"
      cell.appendChild(container)
      containers.set(cell, container)
      return () => {
        container.remove()
        cell.classList.remove("resizable-column-header")
        cell.style.position = previousPosition
        if (previousRole) cell.setAttribute("role", previousRole)
        else cell.removeAttribute("role")
      }
    })
    setPortalContainers(containers)
    return () => restorations.forEach((restore) => restore())
  }, [headerCells])

  useLayoutEffect(() => {
    const rememberVisibleWidths = () => {
      lastVisibleWidthsRef.current = preserveColumnWidths(
        headerCells.map((cell) => cell.getBoundingClientRect().width),
        lastVisibleWidthsRef.current,
        minWidths,
      )
    }
    rememberVisibleWidths()
    const observer = new ResizeObserver(rememberVisibleWidths)
    headerCells.forEach((cell) => observer.observe(cell))
    return () => observer.disconnect()
  }, [headerCells, minWidths])

  useLayoutEffect(() => () => cleanupDragRef.current?.(), [])
  useLayoutEffect(() => {
    if (enabled) return
    cleanupDragRef.current?.()
    setActiveColumn(null)
  }, [enabled])

  const measureWidths = () => preserveColumnWidths(
    headerCells.map((cell) => cell.getBoundingClientRect().width),
    widths ?? lastVisibleWidthsRef.current,
    minWidths,
  )
  const clampWidth = (index: number, width: number) => Math.min(
    MAX_COLUMN_WIDTH,
    Math.max(minWidths[index] ?? DEFAULT_MIN_COLUMN_WIDTH, Math.round(width)),
  )
  const rememberDefaults = (measured: number[]) => {
    if (!defaultWidthsRef.current || defaultWidthsRef.current.length !== measured.length) {
      defaultWidthsRef.current = [...measured]
    }
  }
  const stopActiveDrag = () => {
    cleanupDragRef.current?.()
    cleanupDragRef.current = null
  }
  const beginResize = (index: number, event: ReactPointerEvent<HTMLSpanElement>) => {
    const measured = measureWidths()
    if (measured.length !== headerCells.length || !measured.length) return

    event.preventDefault()
    event.stopPropagation()
    stopActiveDrag()
    rememberDefaults(measured)

    const startX = event.clientX
    const startWidth = measured[index]
    const handle = event.currentTarget
    const { pointerId } = event
    let dragging = false

    const handlePointerMove = (pointerEvent: PointerEvent) => {
      const delta = pointerEvent.clientX - startX
      if (!dragging && Math.abs(delta) < RESIZE_THRESHOLD) return
      if (!dragging) setActiveColumn(index)
      dragging = true
      const nextWidths = [...measured]
      nextWidths[index] = clampWidth(index, startWidth + delta)
      setWidths(nextWidths)
    }
    const stopResize = () => {
      window.removeEventListener("pointermove", handlePointerMove)
      window.removeEventListener("pointerup", stopResize)
      window.removeEventListener("pointercancel", stopResize)
      window.removeEventListener("blur", stopResize)
      handle.removeEventListener("lostpointercapture", stopResize)
      if (handle.hasPointerCapture?.(pointerId)) handle.releasePointerCapture?.(pointerId)
      setActiveColumn(null)
      cleanupDragRef.current = null
    }

    cleanupDragRef.current = stopResize
    window.addEventListener("pointermove", handlePointerMove)
    window.addEventListener("pointerup", stopResize)
    window.addEventListener("pointercancel", stopResize)
    window.addEventListener("blur", stopResize)
    handle.addEventListener("lostpointercapture", stopResize)
    if (event.isTrusted) handle.setPointerCapture?.(pointerId)
  }
  const resetColumn = (index: number) => {
    setWidths((current) => {
      const defaultWidths = defaultWidthsRef.current
      if (!current || !defaultWidths || defaultWidths[index] == null) return current
      const nextWidths = [...current]
      nextWidths[index] = clampWidth(index, defaultWidths[index])
      return nextWidths
    })
  }
  const resizeWithKeyboard = (index: number, event: ReactKeyboardEvent<HTMLSpanElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault()
      event.stopPropagation()
      resetColumn(index)
      return
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return
    const measured = measureWidths()
    if (measured.length !== headerCells.length || !measured.length) return

    event.preventDefault()
    event.stopPropagation()
    rememberDefaults(measured)
    const step = event.shiftKey ? 20 : 8
    measured[index] = clampWidth(index, measured[index] + (event.key === "ArrowRight" ? step : -step))
    setWidths(measured)
  }

  const portals = enabled ? headerCells.map((cell, index) => {
    const label = cell.textContent?.trim() || cell.getAttribute("aria-label")?.trim()
    const container = portalContainers.get(cell)
    if (!container || container.parentElement !== cell) return null
    return createPortal(
      <span
        className="table-column-resizer"
        role="separator"
        aria-label={`调整列宽：${label || index + 1}`}
        aria-orientation="vertical"
        aria-valuemin={minWidths[index] ?? DEFAULT_MIN_COLUMN_WIDTH}
        aria-valuemax={MAX_COLUMN_WIDTH}
        aria-valuenow={clampWidth(index, widths?.[index] ?? cell.getBoundingClientRect().width)}
        aria-keyshortcuts="ArrowLeft ArrowRight Enter Space"
        data-active={activeColumn === index || undefined}
        data-resize-at-all-widths={resizeAtAllWidths || undefined}
        tabIndex={0}
        onPointerDown={(event) => beginResize(index, event)}
        onDoubleClick={(event) => {
          event.preventDefault()
          event.stopPropagation()
          resetColumn(index)
        }}
        onKeyDown={(event) => resizeWithKeyboard(index, event)}
      />,
      container,
      `column-resizer-${index}`,
    )
  }) : null

  return {
    activeColumn,
    enabled,
    overlay: activeColumn === null ? null : <div className="table-column-resize-overlay" data-resize-at-all-widths={resizeAtAllWidths || undefined} aria-hidden="true" />,
    portals,
    widths: widths?.map((width, index) => clampWidth(index, width)) ?? null,
  }
}

function useDesktopColumnResize(resizeAtAllWidths = false) {
  const [enabled, setEnabled] = useState(() => typeof window === "undefined"
    || window.matchMedia("(min-width: 761px)").matches)

  useEffect(() => {
    const media = window.matchMedia("(min-width: 761px)")
    const update = () => setEnabled(media.matches)
    update()
    media.addEventListener("change", update)
    window.addEventListener("resize", update)
    return () => {
      media.removeEventListener("change", update)
      window.removeEventListener("resize", update)
    }
  }, [])

  return resizeAtAllWidths || enabled
}

type ResizableGridTableProps = HTMLAttributes<HTMLDivElement> & {
  children: ReactNode
  columnGap?: number
  headerSelector: string
  horizontalPadding?: number
  minWidths?: readonly number[]
  resizeAtAllWidths?: boolean
}

export function ResizableGridTable({
  children,
  columnGap = 0,
  headerSelector,
  horizontalPadding = 0,
  className,
  minWidths,
  resizeAtAllWidths = false,
  style,
  ...props
}: ResizableGridTableProps) {
  const rootRef = useRef<HTMLDivElement>(null)
  const enabled = useDesktopColumnResize(resizeAtAllWidths)
  const resize = useResizableColumns({ rootRef, headerSelector, minWidths, enabled, resizeAtAllWidths })
  const activeWidths = resize.enabled ? resize.widths : null
  const contentWidth = activeWidths
    ? activeWidths.reduce((total, width) => total + width, horizontalPadding + columnGap * Math.max(0, activeWidths.length - 1))
    : null
  const resizeStyle = activeWidths
    ? {
        "--resizable-table-columns": activeWidths.map((width) => `${width}px`).join(" "),
        "--resizable-table-width": `${contentWidth}px`,
        ...Object.fromEntries(activeWidths.map((width, index) => [`--resizable-column-${index + 1}`, `${width}px`])),
      } as CSSProperties
    : undefined

  return <>
    <div {...props} ref={rootRef} className={["resizable-grid-table", className].filter(Boolean).join(" ")} style={{ ...style, ...resizeStyle }}>
      {children}
      {resize.portals}
    </div>
    {resize.overlay}
  </>
}

type ResizableTableProps = TableHTMLAttributes<HTMLTableElement> & {
  children: ReactNode
  minWidths?: readonly number[]
  resizable?: boolean
}

export function ResizableTable({ children, minWidths, style, resizable = true, ...props }: ResizableTableProps) {
  const tableRef = useRef<HTMLTableElement>(null)
  const enabled = useDesktopColumnResize()
  const resize = useResizableColumns({ rootRef: tableRef, headerSelector: "thead > tr", minWidths, enabled: enabled && resizable })
  const activeWidths = resize.enabled ? resize.widths : null
  const contentWidth = activeWidths?.reduce((total, width) => total + width, 0)

  return <>
    <table
      {...props}
      ref={tableRef}
      className={["resizable-native-table", props.className].filter(Boolean).join(" ")}
      data-resized={activeWidths ? "true" : undefined}
      style={{ ...style, ...(contentWidth ? { width: `max(100%, ${contentWidth}px)` } : undefined) }}
    >
      {activeWidths && <colgroup>{activeWidths.map((width, index) => <col key={index} style={{ width }} />)}</colgroup>}
      {children}
      {resize.portals}
    </table>
    {resize.overlay}
  </>
}