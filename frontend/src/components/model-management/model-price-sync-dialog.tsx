import { ArrowRight, Check, ChevronDown, CircleHelp, LoaderCircle, RefreshCw, TriangleAlert, X } from "lucide-react"
import { useState } from "react"

import { dataSource } from "../../data-sources/apim/api"
import type { ModelRegistry, PriceSyncDetail, PriceSyncResponse, StoredContextPriceTier } from "../../data-sources/apim/types"
import { ModelBrandLogo } from "../brand-logos"
import { Button } from "../ui/button"
import { Chip } from "../ui/chip"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { RegistryNoticeBanner } from "../ui/registry-notice-banner"
import { ResizableTable } from "../ui/resizable-table"
import { discountedRate, priceSyncChangeCounts, priceSyncChangeLabels, priceSyncContextRows, priceSyncDetailForDisplay } from "./model-edit-form"
import { ModelPriceText } from "./model-price-source"

const outcomes: Record<string, string> = {
  updated: "可更新", unchanged: "未变化", unmapped: "未匹配", stale: "来源不可用",
  review_needed: "待复核", superseded: "配置已变化", ok: "已同步",
}

const priceBuckets = ["input", "output", "cached", "cache_write"] as const

function RateComparison({ before, after, available }: {
  before: number | null | undefined
  after: number | null | undefined
  available: boolean
}) {
  const format = (value: number | null | undefined) => value == null ? "-"
    : value.toLocaleString("en-US", { maximumFractionDigits: 8 })
  if (available && before === after) return <span className="price-sync-rate-single" data-no-localize>{format(after)}</span>
  return <span className="price-sync-rate-pair"
    data-changed={available && before !== after || undefined} data-no-localize>
    <span className="price-sync-rate-before">{format(before)}</span>
    <ArrowRight size={11} aria-hidden="true" />
    <span className="price-sync-rate-after">{format(after)}</span>
  </span>
}

function contextRates(tier: StoredContextPriceTier, percent: number | null): NonNullable<PriceSyncDetail["current_prices"]> {
  const prices = tier.list_prices
  return Object.fromEntries(priceBuckets.map(bucket => [bucket,
    prices[bucket] == null ? null : discountedRate(prices[bucket], percent),
  ])) as NonNullable<PriceSyncDetail["current_prices"]>
}

function tierRange(tier: StoredContextPriceTier): string {
  return tier.max_input_tokens === null ? `>= ${tier.min_input_tokens.toLocaleString("en-US")}`
    : `${tier.min_input_tokens.toLocaleString("en-US")} - ${tier.max_input_tokens.toLocaleString("en-US")}`
}

export function ModelPriceSyncDialog({ onComplete }: { onComplete?: (registry: ModelRegistry) => void }) {
  const [open, setOpen] = useState(false)
  const [result, setResult] = useState<PriceSyncResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const details = (result?.details ?? []).map(detail => priceSyncDetailForDisplay(
    detail, result?.registry.models.find(model => model.id === detail.model_id),
  ))
  const changes = priceSyncChangeCounts(details)

  const execute = async (confirm = false) => {
    setBusy(true)
    setError(null)
    try {
      const response = await dataSource.syncPrices(undefined, confirm
        ? { confirmation_digest: result!.confirmation_digest! } : { dry_run: true })
      setResult(response)
      if (!response.dry_run) onComplete?.(response.registry)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
      if (confirm) setResult(null)
    } finally { setBusy(false) }
  }

  return <>
    <button type="button" title="预览价格同步" onClick={() => {
      setOpen(true); setResult(null); void execute()
    }}><RefreshCw size={14} />同步定价</button>
    <Dialog open={open} onOpenChange={value => { if (!busy) setOpen(value) }}>
      <DialogContent className="registry-editor-dialog price-sync-dialog" finalFocus={false}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>{result && !result.dry_run ? "价格同步结果" : "价格同步预览"}</DialogTitle>
          <DialogDescription data-no-localize>USD / 1M Tokens</DialogDescription>
        </DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body simple-model-body price-sync-body" aria-busy={busy}>
          {busy && <p className="price-sync-loading" role="status"><LoaderCircle className="spin" size={16} />正在读取报价</p>}
          {error && <RegistryNoticeBanner notice={{ ok: false, text: error }} />}
          {result && <>
            <dl className="price-sync-summary">
              <div data-tone="updated"><dt><ModelPriceText>目录价变化</ModelPriceText></dt><dd>{changes.catalog}</dd></div>
              <div data-tone="review"><dt><ModelPriceText>规则变化</ModelPriceText></dt><dd>{changes.rules}</dd></div>
              <div><dt>未变化</dt><dd>{changes.unchanged}</dd></div>
              {changes.unknown > 0 && <div><dt><ModelPriceText>目录基准未知</ModelPriceText></dt><dd>{changes.unknown}</dd></div>}
              <div><dt>手工价已跳过</dt><dd>{result.skipped_manual ?? 0}</dd></div>
              <div data-tone="review"><dt>待复核</dt><dd>{result.review_needed}</dd></div>
              <div data-tone="unavailable"><dt>未匹配或读取失败</dt><dd>{result.unmapped + result.stale + result.superseded}</dd></div>
            </dl>
            <div className="price-sync-table-scroll">
            <ResizableTable className="price-sync-table">
              <colgroup><col className="price-sync-model-column" /><col className="price-sync-tier-column" /><col /><col /><col /><col /><col className="price-sync-result-column" /></colgroup>
              <thead><tr><th>模型</th><th><ModelPriceText>计价档位</ModelPriceText></th><th>输入</th><th>输出</th><th>缓存读取</th><th>缓存写入</th><th>结果</th></tr></thead>
              {details.map(detail => {
                const outcome = detail.outcome ?? detail.status
                const model = result.registry.models.find(item => item.id === detail.model_id)
                const labels = priceSyncChangeLabels(detail, model, !result.dry_run && outcome === "updated")
                const contextRows = priceSyncContextRows(detail)
                const originTier = contextRows[0]?.current ?? contextRows[0]?.proposed
                const origin = originTier?.input_range_source?.origin
                const rows = contextRows.length ? contextRows : [{ key: "flat", current: undefined, proposed: undefined }]
                const status = <Chip className="price-sync-outcome" data-outcome={outcome}>
                  {outcome === "review_needed" && <TriangleAlert size={12} aria-hidden="true" />}
                  <span><ModelPriceText>{labels.length ? [
                    ...labels, ...(outcome === "review_needed" ? ["待复核"] : []),
                    ...(!result.dry_run && outcome === "updated" ? ["已应用"] : []),
                  ].join(" · ") : !result.dry_run && outcome === "updated" ? "已同步" : outcomes[outcome] ?? detail.status}</ModelPriceText></span>
                </Chip>
                return <tbody key={detail.model_id} data-model-key={detail.model_key} data-outcome={outcome}>
                  {rows.map((pair, index) => {
                    const tier = pair.current ?? pair.proposed
                    const percent = model?.effective_discount_percent ?? null
                    const before = contextRows.length ? pair.current ? contextRates(pair.current, percent) : null : detail.current_prices
                    const after = contextRows.length ? pair.proposed ? contextRates(pair.proposed, percent) : null : detail.proposed_prices
                    const changedRange = pair.current && pair.proposed && (
                      pair.current.min_input_tokens !== pair.proposed.min_input_tokens
                      || pair.current.max_input_tokens !== pair.proposed.max_input_tokens)
                    return <tr key={pair.key} className="price-sync-data-row" data-tier-key={pair.key}>
                      {index === 0 && <th className="price-sync-model-cell" scope="rowgroup" rowSpan={rows.length}>
                        <div className="price-sync-model-identity">
                        <span className="price-sync-model-icon" aria-hidden="true"><ModelBrandLogo
                          familyKey={model?.family_key}
                          identity={`${model?.upstream_model_id ?? ""} ${model?.display_name ?? detail.model_key} ${detail.model_key}`}
                          providerName={result.registry.providers.find(item => item.id === model?.provider_id)?.name ?? ""}
                          size={16} /></span>
                        <div className="price-sync-model-copy">
                        <b data-no-localize title={model?.display_name ?? detail.model_key}>{model?.display_name ?? detail.model_key}</b>
                        <code data-no-localize title={detail.model_key}>{detail.model_key}</code>
                        {originTier && <small title={originTier.input_range_source?.note ?? undefined}>
                          {origin === "official" ? "官方阈值" : origin === "custom" ? "自定义阈值"
                            : origin === "reference" ? "参考阈值" : "阈值来源未验证"}
                        </small>}
                        </div>
                        </div>
                      </th>}
                      <th className="price-sync-tier-cell" scope="row"><b>{tier ? tier.label : <ModelPriceText>单档报价</ModelPriceText>}</b>
                        {tier && <>
                        <span className="price-sync-tier-range" data-changed={changedRange || undefined}>
                          <span data-no-localize>{tierRange(tier)}</span>
                          {changedRange && <><ArrowRight size={10} aria-hidden="true" /><span data-no-localize>{tierRange(pair.proposed!)}</span></>}
                          <span>输入 Tokens</span>
                        </span>
                        {!pair.current && <small>新增档位</small>}
                        {!pair.proposed && <small>{detail.proposed_context_price_tiers == null ? "暂无报价" : "移除档位"}</small>}
                        </>}
                      </th>
                      {priceBuckets.map(bucket => <td key={bucket}><RateComparison
                        before={before?.[bucket]} after={after?.[bucket]} available={after != null} /></td>)}
                      {index === 0 && <td className="price-sync-result-cell" rowSpan={rows.length}>
                        {detail.message ? <details className="price-sync-status-details">
                          <summary>{status}<ChevronDown size={11} aria-hidden="true" /></summary>
                          <div className="price-sync-message"><ModelPriceText>{detail.message}</ModelPriceText></div>
                        </details> : status}
                      </td>}
                    </tr>
                  })}
                </tbody>
              })}
            </ResizableTable>
            </div>
            {!result.details.length && <div className="price-sync-empty"><CircleHelp size={20} /><p>没有需要同步的目录价格。</p></div>}
          </>}
        </div>
        <DialogFooter className="registry-editor-footer price-sync-footer">
          <span className="price-sync-legend"><ModelPriceText>原报价</ModelPriceText><ArrowRight size={12} aria-hidden="true" /><ModelPriceText>新报价（同一折扣）</ModelPriceText></span>
          <Button type="button" variant="outline" disabled={busy} onClick={() => void execute()}><RefreshCw size={14} />重新预览</Button>
          {result?.dry_run && <Button type="button" disabled={busy || !result.confirmation_digest || result.considered === 0}
            onClick={() => void execute(true)}><Check size={14} />确认同步</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </>
}