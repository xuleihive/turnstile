import { useEffect, useLayoutEffect, useRef, useState } from "react"
import {
  AlertTriangle,
  ChevronRight,
  Loader2,
  SendHorizontal,
  Sparkles,
} from "lucide-react"

import type {
  AssistantReply,
  ChartSpec,
} from "./types"
import { ChartCard } from "../charts/chart-card"
import { translateForLocale, useLocale } from "../../locales/index"
import { AssistantMarkdown } from "./markdown"
import type { AssistantController } from "./use-assistant-controller"

/** The emoji sits outside the phrase because it is locale-neutral, exactly as SmartHive
 *  keeps its starter icons out of the translated `starter_prompts` catalog. */
const STARTERS = [
  { emoji: "📋", text: "本月 Token 消耗最高的 10 个人是谁？" },
  { emoji: "📈", text: "过去 7 天每个部门的调用量趋势如何？" },
  { emoji: "💰", text: "哪个模型的成本最高？" },
]

const COPILOT_STARTERS = [
  { emoji: "👥", text: "哪些 Copilot 成员最近最活跃？" },
  { emoji: "📈", text: "Copilot 采纳率和代码生成趋势如何？" },
  { emoji: "💳", text: "哪些成员的 Copilot 预算即将用完？" },
]

function ToolSteps({ reply }: { reply: AssistantReply }) {
  const [open, setOpen] = useState(false)
  if (reply.steps.length === 0) return null
  const failed = reply.steps.some((step) => step.status === "error")
  return <div className="assistant-steps">
    <button type="button" className="assistant-steps-trigger" onClick={() => setOpen(!open)}>
      <ChevronRight size={12} className={open ? "assistant-steps-caret open" : "assistant-steps-caret"} />
      <span>{failed ? "查询过程（含失败步骤）" : "查询过程"}</span>
      <em className="assistant-steps-count">{reply.steps.length}</em>
    </button>
    {open && <div className="assistant-steps-body">
      {reply.steps.map((step) => <div className="assistant-step" key={step.sequence}>
        {step.status === "error"
          ? <AlertTriangle size={11} className="assistant-step-fail" />
          : <span className="assistant-step-dot" />}
        <b>{step.tool}</b>
        <span>{step.summary}</span>
      </div>)}
      <p className="assistant-steps-note">
        所有数值均来自以上工具查询的真实结果，助手不会自行生成数据。
      </p>
    </div>}
  </div>
}

/**
 * The transcript and the composer, shared verbatim by the floating panel and the full
 * page so an answer cannot render one way in one surface and another way in the other.
 * Everything surface-specific -- the window chrome, the thread rail, the page header --
 * stays outside.
 */
export function AssistantConversation({ controller, onPin }: {
  controller: AssistantController
  onPin?: (target: { chart: ChartSpec; question: string }) => void
}) {
  const { exchanges, question, setQuestion, asking, submit, inputRef } = controller
  const { locale } = useLocale()
  const isCopilot = controller.source === "github-copilot"
  const starters = isCopilot ? COPILOT_STARTERS : STARTERS
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" })
  }, [exchanges])

  useLayoutEffect(() => {
    const input = inputRef.current
    if (!input) return
    input.style.height = "auto"
    input.style.height = `${Math.min(input.scrollHeight, 132)}px`
  }, [inputRef, question])

  return <>
    <div
      className="assistant-scroll"
      ref={scrollRef}
      onScroll={(event) => {
        // Written straight to the node: this drives one CSS mask and nothing else, so a
        // state update per scroll frame would re-render the whole thread for no gain.
        event.currentTarget.dataset.scrolled = event.currentTarget.scrollTop > 0 ? "true" : "false"
      }}
    >
      {exchanges.length === 0 && <div className="assistant-empty">
        <div className="assistant-empty-copy">
          <h3>{isCopilot ? "和你的 Copilot 数据对话" : "和你的用量数据对话"}</h3>
          {isCopilot
            ? <>
              <p>✨ 它了解你的 GitHub 组织、成员、席位与预算。</p>
              <p>让它分析采纳率、活动趋势和预算状态。</p>
            </>
            : <>
              <p>✨ 它了解你的组织——<b>部门、项目、智能体、模型。</b></p>
              <p>成本排名、用量趋势与预算分析</p>
            </>}
        </div>
        <div className="assistant-starters">
          {starters.map((starter) => <button
            type="button"
            key={starter.text}
            onClick={() => {
              void translateForLocale(starter.text, locale).then((translatedPrompt) => {
                setQuestion(translatedPrompt)
                inputRef.current?.focus()
              })
            }}
          ><span className="assistant-starter-emoji">{starter.emoji}</span><span>{starter.text}</span></button>)}
        </div>
      </div>}

      {exchanges.map((exchange) => <div className="assistant-exchange" key={exchange.id}>
        {/* Both the question and the answer are content, not product copy: one is typed
            by a person and the other is written by the model in the language it was
            asked to use. Running either through the phrase dictionary rewrites
            recognised fragments and leaves the rest, which reads as corruption. */}
        <div className="assistant-question" data-no-localize>{exchange.question}</div>
        {!exchange.reply && !exchange.error && <div className="assistant-thinking">
          <Loader2 size={13} className="assistant-spin" />
          <span>正在查询…</span>
        </div>}
        {exchange.error && <div className="assistant-error">
          <AlertTriangle size={13} />
          <span>{exchange.error}</span>
        </div>}
        {exchange.reply && <div className="assistant-answer">
          <ToolSteps reply={exchange.reply} />
          {/* The question is not rendered this way: it is typed into a plain textarea,
              not authored as markdown, so a person asking about `**` or a cost of
              `$5 * 3` would have their own words silently reformatted. */}
          {exchange.reply.message && <AssistantMarkdown>{exchange.reply.message}</AssistantMarkdown>}
          {exchange.reply.charts.map((chart) => <ChartCard
            key={chart.id}
            chart={chart}
            onPin={onPin ? () => onPin({ chart, question: exchange.question }) : undefined}
          />)}
          <div className="assistant-meta">
            {exchange.reply.model} · {exchange.reply.total_tokens.toLocaleString()} Token ·
            {" "}{(exchange.reply.latency_ms / 1000).toFixed(1)} s
          </div>
        </div>}
      </div>)}
    </div>

    <div className="assistant-composer">
      <textarea
        ref={inputRef}
        value={question}
        rows={1}
        placeholder={isCopilot
          ? "问一个关于 Copilot 用量或预算的问题…"
          : "问一个关于用量或成本的问题…"}
        onChange={(event) => setQuestion(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault()
            void submit()
          }
        }}
      />
      <div className="assistant-composer-foot">
        {/* Mirrors SmartHive's bottom-left agent chip. It is a label rather than a
            picker: this product has exactly one assistant, and the earlier identity
            dropdown here asked the person a question the application should not need
            to ask. */}
        <span className="assistant-agent-chip">
          <Sparkles size={12} />
          <span>FinOps助手</span>
        </span>
        <button
          type="button"
          className="assistant-send"
          onClick={() => void submit()}
          disabled={!question.trim() || asking}
          title="发送"
          aria-label="发送"
        >
          {asking
            ? <Loader2 size={14} className="assistant-spin" />
            : <SendHorizontal size={14} />}
        </button>
      </div>
    </div>
  </>
}
