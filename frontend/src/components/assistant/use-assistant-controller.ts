import { useCallback, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { assistantApi } from "./api"
import { useLocale } from "../../locales/index"
import {
  conversationKey,
  conversationQuery,
  conversationsKey,
  conversationsQuery,
  pinnedChartsKey,
  pinnedChartsQuery,
} from "./queries"
import { useAuth } from "../../providers/auth-provider"
import { useTimezone } from "../../providers/timezone-provider"
import type {
  AssistantReply,
  AssistantSource,
  AssistantTurn,
  ChartSpec,
  ConversationSummary,
} from "./types"

export type Exchange = {
  id: string
  question: string
  reply: AssistantReply | null
  error: string | null
}

/** Only the text turns go back to the model. Charts are rebuilt by tools, never replayed
 *  through the context window, which keeps a long conversation from growing unboundedly. */
function toHistory(exchanges: Exchange[]): AssistantTurn[] {
  return exchanges.flatMap((exchange) => {
    const turns: AssistantTurn[] = [{ role: "user", content: exchange.question }]
    if (exchange.reply?.message) {
      turns.push({ role: "assistant", content: exchange.reply.message })
    }
    return turns
  }).slice(-12)
}

/**
 * Everything the assistant does, with no opinion about where it is drawn.
 *
 * It exists because there are two surfaces -- the floating panel and the full page -- and
 * SmartHive's chat module is built the same way: `useChatController` is shared and only the
 * chrome differs. Duplicating this logic per surface is the failure that matters, because
 * the two copies would drift on exactly the parts that are subtle here: which questions
 * trigger naming, what is invalidated after an answer, and what happens to the transcript
 * when the conversation being read is deleted.
 *
 * The two surfaces do NOT share live state, unlike SmartHive, which keeps one
 * `activeSessionId` in a global store. Ours hand over through the URL and the server
 * instead: the panel links to `?page=assistant&conversation=<id>` and the page loads it
 * back. That works because the server is already the source of truth for a conversation,
 * and it avoids introducing a global store for one feature. The cost is that an unsent
 * draft does not travel, which is why the panel's link is disabled until a conversation
 * exists to hand over.
 */
export function useAssistantController({
  conversationsEnabled,
  source,
}: {
  conversationsEnabled: boolean
  source: AssistantSource
}) {
  const [question, setQuestion] = useState("")
  const [exchanges, setExchanges] = useState<Exchange[]>([])
  const [conversationId, setConversationId] = useState<string | undefined>()
  // The server's name for the open thread, once it has one. Kept beside the transcript
  // rather than read from the conversations query because that query may not be fetched
  // while the title is on screen.
  const [conversationTitle, setConversationTitle] = useState<string | undefined>()
  const [loadingConversation, setLoadingConversation] = useState<string | null>(null)
  const [pinTarget, setPinTarget] = useState<{ chart: ChartSpec; question: string } | null>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const owner = user?.email ?? ""
  const { timezone } = useTimezone()
  const { locale } = useLocale()

  // The report center and pin dialog share the cache; a closed assistant does not fetch it.
  const pinnedReports = useQuery({
    ...pinnedChartsQuery(source === "apim" ? owner : null),
    enabled: source === "apim" && Boolean(owner && pinTarget),
  })
  const conversations = useQuery({
    ...conversationsQuery(source, owner),
    enabled: conversationsEnabled,
  })

  const ask = useMutation({
    mutationFn: (value: string) => assistantApi.ask(source, {
      question: value,
      history: toHistory(exchanges),
      conversation_id: conversationId,
      timezone,
      locale,
    }),
  })

  // A conversation is stored under a placeholder title -- its first question, truncated.
  // This asks the server to replace it with one the model wrote. Failure is silent on
  // purpose: the placeholder is a correct if dull label, and an error toast over a
  // working answer would be a worse outcome than a plain name.
  const nameConversation = useMutation({
    mutationFn: (id: string) => assistantApi.titleConversation(source, id, locale),
    onSuccess: (summary) => {
      setConversationTitle(summary.title)
      void queryClient.invalidateQueries({ queryKey: conversationsKey(source, owner) })
    },
  })

  const pin = useMutation({
    mutationFn: (value: {
      reportId?: string
      title?: string
      description?: string
      question: string
      chart: ChartSpec
    }) =>
      assistantApi.pin({
        report_id: value.reportId,
        title: value.title,
        description: value.description,
        original_question: value.question,
        chart: value.chart,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: pinnedChartsKey(owner) })
    },
  })

  const reset = useCallback(() => {
    setExchanges([])
    setConversationId(undefined)
    setConversationTitle(undefined)
    setQuestion("")
    inputRef.current?.focus()
  }, [])

  const submit = async () => {
    const value = question.trim()
    if (!value || ask.isPending) return
    const id = crypto.randomUUID()
    // Captured before the optimistic append, so this is "was the thread empty", which is
    // what decides whether it still needs naming.
    const isFirstQuestion = exchanges.length === 0
    setQuestion("")
    setExchanges((current) => [...current, { id, question: value, reply: null, error: null }])
    try {
      const reply = await ask.mutateAsync(value)
      setConversationId(reply.conversation_id)
      setExchanges((current) => current.map(
        (item) => item.id === id ? { ...item, reply } : item,
      ))
      // The answer just created or extended a conversation, so the list is stale the
      // moment it returns. The transcript itself is dropped rather than updated: the
      // surface already holds it, and refetching it on reopen is one cached read.
      void queryClient.invalidateQueries({ queryKey: conversationsKey(source, owner) })
      queryClient.removeQueries({
        queryKey: conversationKey(source, owner, reply.conversation_id),
      })
      // Deliberately after the answer is on screen and deliberately not awaited: naming
      // the thread is worth a second model call, but not a second of the reader's wait.
      if (isFirstQuestion) nameConversation.mutate(reply.conversation_id)
      return reply
    } catch (error) {
      setExchanges((current) => current.map(
        (item) => item.id === id
          ? { ...item, error: error instanceof Error ? error.message : "请求失败" }
          : item,
      ))
      return null
    }
  }

  /** Load a stored conversation into whichever surface is showing.
   *
   *  The transcript replaces the current one wholesale, exactly as switching sessions
   *  does in SmartHive. The stored replies are rendered as they were saved rather than
   *  re-run: re-running would spend tokens to redraw charts the person has already seen.
   */
  const openConversation = useCallback(async (summary: Pick<ConversationSummary, "id" | "title">) => {
    setLoadingConversation(summary.id)
    try {
      const conversation = await queryClient.fetchQuery(
        conversationQuery(source, owner, summary.id),
      )
      setConversationId(conversation.id)
      setConversationTitle(conversation.title)
      setExchanges(conversation.exchanges.map((exchange) => ({
        id: `${conversation.id}-${exchange.position}`,
        question: exchange.question,
        reply: exchange.reply,
        error: null,
      })))
      setQuestion("")
    } catch (error) {
      setExchanges([{
        id: crypto.randomUUID(),
        question: summary.title,
        reply: null,
        error: error instanceof Error ? error.message : "无法打开该对话",
      }])
    } finally {
      setLoadingConversation(null)
    }
  }, [owner, queryClient, source])

  const removeConversation = useMutation({
    mutationFn: (id: string) => assistantApi.deleteConversation(source, id),
    onSuccess: (_result, id) => {
      void queryClient.invalidateQueries({ queryKey: conversationsKey(source, owner) })
      queryClient.removeQueries({ queryKey: conversationKey(source, owner, id) })
      // Deleting the thread you are reading has to clear it too, or the surface keeps
      // showing a transcript that no longer exists and the next question silently
      // starts a new one anyway.
      if (id === conversationId) reset()
    },
  })

  return {
    source,
    question, setQuestion,
    exchanges,
    conversationId, conversationTitle,
    asking: ask.isPending,
    submit, reset,
    conversations,
    openConversation, loadingConversation,
    removeConversation,
    pin, pinTarget, setPinTarget, pinnedReports,
    inputRef,
  }
}

export type AssistantController = ReturnType<typeof useAssistantController>
