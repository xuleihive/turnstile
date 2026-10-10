import { useEffect, useRef, useState } from "react"
import { ArrowLeft, Loader2, PanelLeft, Plus, Sparkles, Trash2 } from "lucide-react"

import { Button } from "../components/ui/button"
import { AssistantConversation } from "../components/assistant/assistant-conversation"
import { PinDialog } from "../components/assistant/pin-dialog"
import { useAssistantController } from "../components/assistant/use-assistant-controller"
import { useIsMobile } from "../hooks/use-mobile"
import { useLocale } from "../locales/index"
import { formatTimeAgo } from "../lib/time"
import type { AssistantSource } from "../components/assistant/types"

/**
 * The assistant's full-page surface, mirroring SmartHive's ChatPage: a thread rail beside
 * the conversation, sharing every piece of behaviour with the floating panel through
 * `useAssistantController` so the two cannot drift.
 *
 * One deliberate difference from SmartHive. Its right pane shows "select a conversation"
 * when nothing is open, because a chat there cannot start until an agent is picked. This
 * product has exactly one assistant, so there is nothing to pick and the empty state is
 * already an invitation to ask -- the conversation pane is therefore always live, and
 * opening the page with no thread selected is simply a new one.
 */
export function AssistantPage({
  source,
  conversationId,
  onConversationChange,
  onToggleSidebar,
}: {
  source: AssistantSource
  conversationId: string | null
  onConversationChange: (id: string | undefined) => void
  onToggleSidebar: () => void
}) {
  const { locale } = useLocale()
  const isMobile = useIsMobile()
  const c = useAssistantController({ conversationsEnabled: true, source })
  // Mobile shows either the rail or the conversation, never both. This is the "the reader
  // asked for a new chat but no conversation exists yet" case, which would otherwise drop
  // them back to the list with no way to compose.
  const [composingNew, setComposingNew] = useState(false)
  // What this surface last pushed to, or accepted from, the URL. Without it the two
  // effects below fight on mount: the store→URL one fires with the pre-load value and
  // strips the id the URL→store one is still applying, which breaks deep links.
  const settled = useRef<string | null | undefined>(undefined)

  // URL → controller: deep link, refresh, back/forward, and the panel's handover.
  useEffect(() => {
    if (settled.current === conversationId) return
    settled.current = conversationId
    if (conversationId) void c.openConversation({ id: conversationId, title: conversationId })
    else c.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- react to the URL only
  }, [conversationId])

  // controller → URL: selecting a thread, and the id a brand-new conversation gets back
  // from its first answer.
  useEffect(() => {
    const current = c.conversationId ?? null
    if (settled.current === current) return
    settled.current = current
    onConversationChange(c.conversationId)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- react to the controller only
  }, [c.conversationId])

  const startNew = () => {
    c.reset()
    setComposingNew(true)
  }

  const rail = <aside className="assistant-rail">
    {/* Label only. SmartHive puts its new-chat button here because that column header is
        its page header; ours already has one spanning the full width, and two controls
        starting the same thing is the clutter the invocation console was kept out of the
        sidebar to avoid. */}
    <div className="assistant-rail-head"><span>历史对话</span></div>
    <div className="assistant-rail-list">
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
          onClick={() => { setComposingNew(false); void c.openConversation(item) }}
          disabled={c.loadingConversation != null}
        >
          {/* A stored title is what a person or the model wrote in the language of that
              conversation, so it is content rather than product copy and must not be run
              through the phrase dictionary. */}
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
          onClick={() => c.removeConversation.mutate(item.id)}
        ><Trash2 size={12} /></button>
      </div>)}
    </div>
  </aside>

  const conversation = <section className="assistant-page-thread">
    <AssistantConversation
      controller={c}
      onPin={source === "apim" ? c.setPinTarget : undefined}
    />
  </section>

  const showThread = !isMobile || !!c.conversationId || composingNew
  return <div className="finops-workspace assistant-workspace">
    <header className="finops-header">
      <div>
        <Button
          variant="ghost"
          size="icon-sm"
          className="finops-sidebar-trigger"
          aria-label="切换导航栏"
          title="切换导航栏"
          onClick={onToggleSidebar}
        ><PanelLeft size={16} /></Button>
        <span className="finops-header-icon"><Sparkles size={17} /></span>
        <h1>FinOps助手</h1>
      </div>
      <Button variant="outline" size="sm" className="assistant-new-chat" onClick={startNew}>
        <Plus size={15} />新对话
      </Button>
    </header>

    <div className="assistant-page-body">
      {isMobile && showThread && <div className="assistant-page-back">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => { c.reset(); setComposingNew(false) }}
        ><ArrowLeft size={15} />历史对话</Button>
      </div>}
      {(!isMobile || !showThread) && rail}
      {showThread && conversation}
    </div>

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
  </div>
}
