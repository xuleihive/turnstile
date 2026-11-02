import { useEffect, useState } from "react"
import {
  ChevronDown,
  Loader2,
  Maximize2,
  MessageCircle,
  Minimize2,
  Minus,
  PanelRight,
  PictureInPicture2,
  Plus,
  SquareArrowOutUpRight,
  Trash2,
} from "lucide-react"
import { useLocale } from "../../locales/index"
import { Popover, PopoverContent, PopoverTrigger } from "../ui/popover"
import { AssistantConversation, ConversationDeleteDialog } from "./assistant-conversation"
import { formatTimeAgo } from "../../lib/time"
import { PinDialog } from "./pin-dialog"
import { useAssistantController } from "./use-assistant-controller"
import { useAssistantResize } from "./use-assistant-resize"
import type { AssistantSource } from "./types"

export function AssistantPanel({
  onOpenFullPage,
  source,
}: {
  onOpenFullPage: (conversationId?: string) => void
  source: AssistantSource
}) {
  const [open, setOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const { locale } = useLocale()
  // Only fetched while the dropdown is open: the panel is opt-in and the list is stale
  // the moment any answer arrives, so keeping it warm buys nothing.
  const c = useAssistantController({
    conversationsEnabled: open && historyOpen,
    source,
  })
  const { style, handles, dragging, atMax, toggleExpand, startDrag, docked, dockable, toggleDock } =
    useAssistantResize()

  useEffect(() => {
    if (open) c.inputRef.current?.focus()
  }, [open, c.inputRef])

  if (!open) {
    return <button
      type="button"
      className="assistant-fab"
      onClick={() => setOpen(true)}
      title="AI Assistant"
      aria-label="打开 AI Assistant"
    >
      <MessageCircle size={19} />
    </button>
  }

  // The model's name for the thread when it has one, the first question until then, and
  // product copy for an empty panel. The middle case is what the reader sees for the few
  // seconds between the answer landing and the title call returning.
  const currentTitle = c.conversationTitle || c.exchanges[0]?.question.trim() || "新对话"
  return <>
    <section
      className="assistant-window"
      data-mode={docked ? "docked" : "floating"}
      aria-label="AI Assistant"
      style={style}
    >
      {/* Left edge, top edge and top-left corner, matching SmartHive's ChatResizeHandles.
          The panel is anchored bottom-right, so dragging away from that corner grows it.
          Docked it keeps only the left edge, and mobile none at all.

          SmartHive leaves these invisible, which makes a resizable edge undiscoverable --
          nothing on screen says the panel can be dragged. They carry the same rail the
          sidebar already uses: hidden until the edge is approached, brand-coloured on
          hover, and held lit for the whole drag via data-dragging. */}
      {handles.map((direction) => <div
        key={direction}
        aria-hidden
        className={`assistant-resize ${direction}`}
        data-dragging={dragging === direction ? "" : undefined}
        onPointerDown={(event) => startDrag(event, direction)}
      />)}

      <header className="assistant-head">
        <div className="assistant-head-left">
          <button type="button" className="assistant-head-action" onClick={c.reset} title="新对话">
            <Plus size={16} />
          </button>
          {/* SmartHive's SessionDropdown: the current thread's name is the trigger, and
              opening it lists the rest. A separate "history" icon would leave nothing
              naming the conversation you are actually in. */}
          <Popover open={historyOpen} onOpenChange={setHistoryOpen}>
            <PopoverTrigger className="assistant-session-trigger" title="历史对话">
              {/* The opt-out is conditional because this one element carries two kinds of
                  content. A stored title is what a person typed and must survive
                  untranslated; the placeholder is product copy and has to translate like
                  everything else. Marking it unconditionally froze "新对话" in Chinese on
                  an otherwise English panel. */}
              <span
                className="assistant-session-title"
                data-no-localize={c.exchanges.length > 0 ? "" : undefined}
              >
                {currentTitle}
              </span>
              <ChevronDown size={13} />
            </PopoverTrigger>
            <PopoverContent className="assistant-history">
              <p className="assistant-history-label">历史对话</p>
              {c.conversations.isPending && <p className="assistant-history-empty">正在载入…</p>}
              {c.conversations.isError && <p className="assistant-history-empty">无法载入历史对话。</p>}
              {c.conversations.data?.length === 0 && (
                <p className="assistant-history-empty">还没有历史对话。</p>
              )}
              {c.conversations.data?.map((item) => <div
                key={item.id}
                className="assistant-history-row"
                data-current={item.id === c.conversationId || undefined}
              >
                <button
                  type="button"
                  className="assistant-history-open"
                  onClick={() => { setHistoryOpen(false); void c.openConversation(item) }}
                  disabled={c.loadingConversation != null}
                >
                  <span className="assistant-history-title" data-no-localize>{item.title}</span>
                  <span className="assistant-history-meta" data-no-localize>
                    {c.loadingConversation === item.id
                      ? <Loader2 size={11} className="assistant-spin" />
                      : formatTimeAgo(item.updated_at, locale)}
                  </span>
                </button>
                <button
                  type="button"
                  className="assistant-history-delete"
                  title="删除对话"
                  aria-label="删除对话"
                  disabled={c.removeConversation.isPending}
                  onClick={() => { setHistoryOpen(false); c.setDeleteTarget(item.id) }}
                ><Trash2 size={12} /></button>
              </div>)}
            </PopoverContent>
          </Popover>
        </div>
        <div className="assistant-head-right">
          {/* The handover to the full page. Disabled until a conversation exists, because
              the two surfaces share the server rather than live state: an unsent draft has
              nothing to hand over and the page would open blank, which reads as the button
              having lost the thread. */}
          <button
            type="button"
            className="assistant-head-action"
            onClick={() => { setOpen(false); onOpenFullPage(c.conversationId) }}
            disabled={!c.conversationId}
            title="在整页中打开"
            aria-label="在整页中打开"
          >
            <SquareArrowOutUpRight size={14} />
          </button>
          {dockable && <button
            type="button"
            className="assistant-head-action"
            onClick={toggleDock}
            title={docked ? "恢复浮动" : "停靠到右侧"}
            aria-label={docked ? "恢复浮动" : "停靠到右侧"}
            aria-pressed={docked}
          >
            {docked ? <PictureInPicture2 size={16} /> : <PanelRight size={16} />}
          </button>}
          {/* Expand means "fill the space available", which a docked panel already does on
              the only axis it owns; widening it further would just crush the dashboard it
              was docked next to in order to read.

              14px rather than the 16px its neighbours take: the corner-to-corner arrows
              fill their viewBox, while Plus, Minus and PanelRight all carry slack inside
              theirs, so equal nominal sizes do not read as equal. */}
          {!docked && <button
            type="button"
            className="assistant-head-action"
            onClick={toggleExpand}
            title={atMax ? "还原" : "展开"}
            aria-label={atMax ? "还原" : "展开"}
          >
            {atMax ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>}
          <button
            type="button"
            className="assistant-head-action"
            onClick={() => setOpen(false)}
            title="最小化"
            aria-label="最小化"
          >
            <Minus size={16} />
          </button>
        </div>
      </header>

      <AssistantConversation
        controller={c}
        onPin={source === "apim" ? c.setPinTarget : undefined}
      />
    </section>

    <ConversationDeleteDialog controller={c} />
    {source === "apim" && c.pinTarget && <PinDialog
      chart={c.pinTarget.chart}
      question={c.pinTarget.question}
      reports={c.pinnedReports.data ?? []}
      busy={c.pin.isPending}
      onClose={() => c.setPinTarget(null)}
      onConfirm={async (target) => {
        await c.pin.mutateAsync({
          ...target,
          question: c.pinTarget!.question,
          chart: c.pinTarget!.chart,
        })
        c.setPinTarget(null)
      }}
    />}
  </>
}
