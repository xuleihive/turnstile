import { Check, LoaderCircle, RefreshCw, Search } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { dataSource } from "../../data-sources/apim/api"
import type { PricePreviewRequest, PricePreviewResponse } from "../../data-sources/apim/types"
import { Button } from "../ui/button"
import { Checkbox } from "../ui/checkbox"
import type { ModelEditDraft } from "./model-edit-form"

export type PricingDraft = Pick<ModelEditDraft,
  "priceSource" | "priceReference" | "priceEntryDigest" | "allowUnpriced" | "discountPercent" |
  "contextWindow" | "contextOrigin" | "inputPrice" | "outputPrice" | "cacheReadPrice" | "cacheWritePrice">

export function emptyPricingDraft(): PricingDraft {
  return {
    priceSource: "models_dev", priceReference: "", priceEntryDigest: "", allowUnpriced: false,
    discountPercent: "", contextWindow: "", inputPrice: "", outputPrice: "",
    cacheReadPrice: "", cacheWritePrice: "",
  }
}

export function PublicModelPricing({ request, draft, onChange, busy }: {
  request: PricePreviewRequest
  draft: PricingDraft
  onChange: (updater: (value: PricingDraft) => PricingDraft) => void
  busy: boolean
}) {
  const [preview, setPreview] = useState<PricePreviewResponse | null>(null)
  const [pendingContext, setPendingContext] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [action, setAction] = useState({ sequence: 0, refresh: false, rematch: false, reference: "" })
  const callbacks = useRef({ onChange, draft })
  callbacks.current = { onChange, draft }
  const generation = useRef(0)
  const initialContext = useRef(draft.contextWindow)
  const requestKey = JSON.stringify({
    ...request, price_source: "models_dev",
    price_reference: action.reference || (action.rematch ? null : draft.priceReference || null),
    price_discount_percent: draft.discountPercent.trim() ? Number(draft.discountPercent) : null,
    refresh: action.refresh, rematch: action.rematch && !action.reference,
  })

  useEffect(() => {
    const token = ++generation.current
    setPreview(null)
    setError(null)
    setLoading(false)
    const payload: PricePreviewRequest = JSON.parse(requestKey)
    if (!payload.model_id && !payload.deployment_name && !payload.upstream_model_id
      && !payload.model_key && !payload.display_name) return
    let cancelled = false
    const timer = setTimeout(async () => {
      setLoading(true)
      const startedContext = callbacks.current.draft.contextWindow
      try {
        const response = await dataSource.pricePreview(payload)
        if (cancelled || generation.current !== token) return
        setPreview(response)
        if (response.status !== "matched" || !response.match || !response.effective_prices) return
        const prices = response.effective_prices
        const observed = callbacks.current.draft
        const canFillContext = observed.contextOrigin !== "user"
          && observed.contextWindow === startedContext
          && (observed.contextWindow === ""
            || !payload.model_id
            || !payload.price_reference && observed.contextWindow === initialContext.current)
        if (!canFillContext && response.context_window !== null) {
          setPendingContext(response.context_window)
        }
        const proposedContext = canFillContext && response.context_window !== null
          ? response.context_window.toString() : observed.contextWindow
        if (canFillContext) initialContext.current = proposedContext
        callbacks.current.onChange((current) => {
          if (current.priceSource !== "models_dev") return current
          const fillContext = canFillContext && current.contextOrigin !== "user"
            && current.contextWindow === startedContext
          return {
            ...current,
            priceReference: response.match!.reference,
            priceEntryDigest: response.entry_digest ?? "",
            allowUnpriced: false,
            inputPrice: prices.input?.toString() ?? current.inputPrice,
            outputPrice: prices.output?.toString() ?? current.outputPrice,
            cacheReadPrice: prices.cached?.toString() ?? current.cacheReadPrice,
            cacheWritePrice: prices.cache_write?.toString() ?? current.cacheWritePrice,
            contextWindow: fillContext ? proposedContext : current.contextWindow,
            contextOrigin: fillContext ? "catalog" : current.contextOrigin,
          }
        })
      } catch (failure) {
        if (!cancelled) setError(failure instanceof Error ? failure.message : String(failure))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }, 500)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [requestKey, action.sequence])

  return <div className="model-price-follow" aria-busy={loading}>
    <div className="public-price-actions">
      <Button type="button" size="sm" variant="secondary" title="重新同步定价"
        disabled={busy || loading} onClick={() => setAction(current => ({
          sequence: current.sequence + 1, refresh: true, rematch: false, reference: "",
        }))}>
        {loading ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
        {loading ? "正在同步" : "同步定价"}
      </Button>
      <Button type="button" size="icon" variant="ghost" title="重新匹配模型"
        aria-label="重新匹配模型" disabled={busy || loading}
        onClick={() => setAction(current => ({
          sequence: current.sequence + 1, refresh: true, rematch: true, reference: "",
        }))}><Search size={14} /></Button>
    </div>
    {preview?.match && <div className="model-price-chosen">
      <b data-no-localize>{preview.match.name}</b>
      <small data-no-localize>{preview.match.provider_id} / {preview.match.model_id}</small>
      <small>{preview.match.method === "ai" ? "默认模型辅助匹配" : "目录匹配"}</small>
    </div>}
    {pendingContext !== null && pendingContext.toString() !== draft.contextWindow && (
      <div className="public-price-context">
        <span>目录上下文 <b data-no-localize>{pendingContext.toLocaleString()}</b></span>
        <Button type="button" variant="ghost" size="sm" disabled={busy}
          onClick={() => { onChange(current => ({ ...current, contextWindow: pendingContext.toString(), contextOrigin: "catalog" })); setPendingContext(null) }}>
          <Check size={14} />采用
        </Button>
      </div>
    )}
    {preview?.candidates.length ? <ul className="model-price-results">
      {preview.candidates.map(candidate => <li key={candidate.reference}>
        <button type="button" disabled={busy || loading} onClick={() => setAction(current => ({
          sequence: current.sequence + 1, refresh: false, rematch: false, reference: candidate.reference,
        }))}><span data-no-localize>{candidate.name}</span><small data-no-localize>{candidate.provider_id} / {candidate.model_id}</small></button>
      </li>)}
    </ul> : null}
    <div role="status">
      {error && <p className="publication-form-note" role="alert">{error}</p>}
      {preview?.warnings.map((warning, index) => <p className="publication-form-note" key={index}>{warning}</p>)}
    </div>
    {!draft.priceReference && request.operation !== "image_generation" && <label className="model-editor-checkbox">
      <Checkbox checked={draft.allowUnpriced} disabled={busy || loading}
        onCheckedChange={checked => onChange(current => ({ ...current, allowUnpriced: checked === true }))} />
      <span>暂不计价</span>
    </label>}
  </div>
}
