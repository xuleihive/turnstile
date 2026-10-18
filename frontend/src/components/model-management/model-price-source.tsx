import { Combobox } from "@base-ui/react/combobox"
import { AlertTriangle, Check, ChevronDown, LoaderCircle, RefreshCw, Search } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import { dataSource } from "../../data-sources/apim/api"
import { translateForLocale, useLocale } from "../../locales/index"
import type {
  ManagedModel,
  PriceCatalogOptionsResponse,
  PriceSource,
  PriceSyncStatus,
  PricePreviewRequest,
} from "../../data-sources/apim/types"
import { ModelBrandLogo } from "../brand-logos"
import { Button } from "../ui/button"
import { ButtonGroup } from "../ui/button-group"
import { Input } from "../ui/input"
import { ResizableTable } from "../ui/resizable-table"
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from "../ui/select"
import { FieldHelp } from "./field-help"
import { PublicModelPricing } from "./public-model-pricing"
import {
  applyCatalogOption,
  contextPriceRulePayload,
  discountedRate,
  effectiveContextPriceTiers,
  groupContextPriceModels,
  modelKeyFromReference,
  pairContextPriceOptions,
  resolveDiscount,
  resetContextPriceRule,
  restoreContextPrices,
  validateModelPricing,
  type ModelPriceCandidate,
  type ModelPriceDraft,
  type ModelPriceOption,
  type ModelPriceOptions,
} from "./model-edit-form"

const SOURCE_LABEL: Record<string, string> = {
  models_dev: "从公网同步定价",
  manual: "手工填写",
  azure_retail: "Azure 零售价",
  anthropic: "Anthropic 列表价",
}

const SYNC_STATUS_LABEL: Record<PriceSyncStatus, string> = {
  ok: "已同步",
  unmapped: "未映射",
  stale: "来源读取失败，保留上次单价",
  review_needed: "官方价变动较大，待复核",
  superseded: "配置已更新，保留当前单价",
  ambiguous: "待确认",
  unsupported: "不支持",
  deferred: "未执行",
}

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—"
  return `$${value.toFixed(value < 1 ? 4 : 2)}`
}

async function loadModelPrices(candidate: ModelPriceCandidate): Promise<ModelPriceOptions> {
  if (!candidate.contextPricing) return dataSource.priceCatalogOptions(candidate.key)
  const { tiers } = candidate.contextPricing
  if (tiers.length < 2 || new Set(tiers.map(tier => tier.key)).size !== tiers.length) {
    throw new Error("未找到完整的分档价目。")
  }
  const prices = await Promise.all(tiers.map(tier => dataSource.priceCatalogOptions(tier.model.key)))
  const response = pairContextPriceOptions(candidate, prices)
  if (!response.complete || !response.options.length) throw new Error("未找到完整的分档价目。")
  return response
}

/**
 * Points a model at a published price, in the order a person actually decides: which model, then
 * how it is deployed, then -- only when it changes the answer -- which region.
 *
 * The shape follows the data rather than the API's row layout. One model's prices arrive as
 * dozens of near-identical meters, one per region, and a flat list of them is unreadable: gpt 5
 * pro alone returns 37 rows carrying two distinct prices, and those 37 rows crowded every other
 * model out of the results. Grouping first means Global -- which charges one figure across all
 * 24 to 28 regions it is sold in -- is a single choice with no region to pick, and a region is
 * asked for only where two regions genuinely disagree.
 */
export function ModelPriceSourceFields<Draft extends ModelPriceDraft>({ model, modelName = "", draft, setDraft, busy, connectionDiscount, showAnthropic = true, imageGeneration = false, publicRequest }: {
  model?: ManagedModel
  modelName?: string
  draft: Draft
  setDraft: (updater: (current: Draft) => Draft) => void
  busy: boolean
  connectionDiscount: number | null
  showAnthropic?: boolean
  imageGeneration?: boolean
  publicRequest?: PricePreviewRequest
}) {
  const restored = model ? restoreContextPrices(model) : null
  const [query, setQuery] = useState<string | null>(() => restored?.model_entry.label ?? null)
  const [matches, setMatches] = useState<ModelPriceCandidate[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [catalogOpen, setCatalogOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [chosenModel, setChosenModel] = useState<ModelPriceCandidate | null>(() => restored?.model_entry ?? null)
  const [details, setDetails] = useState<ModelPriceOptions | null>(() => restored)
  const [loadingOptions, setLoadingOptions] = useState(false)
  const [previewReference, setPreviewReference] = useState(() => restored?.options[0]?.reference ?? "")
  const requestVersion = useRef(0)
  const lastDiscount = useRef({ own: draft.discountPercent, connection: connectionDiscount })

  const following = draft.priceSource !== "manual" && draft.priceSource !== "models_dev"
  const discount = resolveDiscount(draft, connectionDiscount)
  const storedModelKey = draft.priceReference ? modelKeyFromReference(draft.priceReference) : null
  const searchQuery = query ?? (model?.display_name ?? modelName).split("·")[0].trim()

  useEffect(() => () => { requestVersion.current += 1 }, [])

  // Reopening the dialog on a model that already follows a list price should show what it
  // follows, not an empty picker.
  useEffect(() => {
    if (!following || chosenModel || !storedModelKey || details) return
    let cancelled = false
    setLoadingOptions(true)
    dataSource.priceCatalogOptions(storedModelKey)
      .then((response) => {
        if (cancelled) return
        setChosenModel(response.model_entry)
        setQuery(response.model_entry.label)
        setDetails(response)
      })
      .catch(() => undefined)
      .finally(() => { if (!cancelled) setLoadingOptions(false) })
    return () => { cancelled = true }
  }, [following, storedModelKey, chosenModel, details])

  // A stored reference names one region, while an option covers every region charging alike, so
  // matching on the option's own reference alone would fail to recognise a saved choice that is
  // not the one region the group happens to be anchored at.
  const selectedOption = useMemo(() => {
    const reference = chosenModel?.contextPricing ? previewReference : draft.priceReference
    const found = details?.options.find((option) =>
      option.reference === reference
      || Object.values(option.references_by_region ?? {}).includes(reference ?? ""))
    if (!found) return null
    return found.reference === reference
      ? found
      : { ...found, reference: reference ?? found.reference }
  }, [details, draft.priceReference, chosenModel, previewReference])
  // Sibling options for the same deployment are the regions worth choosing between; when a
  // deployment charges one figure everywhere there are no siblings and no question to ask.
  const regionPeers = useMemo(() => {
    if (!details || !selectedOption?.region_required) return []
    return details.options.filter((option) => option.deployment === selectedOption.deployment)
  }, [details, selectedOption])

  // Re-pricing on a discount change keeps the rates honest while the dialog is open; without it
  // the box would say 66% while the rates below still showed the old figures.
  useEffect(() => {
    const changed = lastDiscount.current.own !== draft.discountPercent || lastDiscount.current.connection !== connectionDiscount
    lastDiscount.current = { own: draft.discountPercent, connection: connectionDiscount }
    if (!changed) return
    if (!following || !selectedOption || !chosenModel || !details?.complete) return
    if (selectedOption.contextPricing) {
      setDraft(current => ({ ...current, contextPriceRefresh: true }))
      return
    }
    setDraft((current) =>
      applyCatalogOption(current, chosenModel.source, selectedOption, discount.percent))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.discountPercent, connectionDiscount])

  const clearSelection = () => {
    requestVersion.current += 1
    setChosenModel(null)
    setDetails(null)
    setMatches(null)
    setCatalogOpen(false)
    setLoadingOptions(false)
    setSearching(false)
    setError(null)
    setPreviewReference("")
    setDraft(current => ({ ...resetContextPriceRule(current), priceReference: "", contextPricingPreview: false,
      contextPriceReferences: [], contextPriceTiers: [], contextPriceRefresh: false }))
  }

  const changeQuery = (value: string) => {
    clearSelection()
    setQuery(value)
  }

  const search = async (text = searchQuery, source: PriceSource = draft.priceSource) => {
    if (source === "manual") return
    const version = ++requestVersion.current
    setSearching(true)
    setCatalogOpen(true)
    setError(null)
    try {
      const response = await dataSource.priceCatalogModels(text, source)
      if (version !== requestVersion.current) return
      setMatches(groupContextPriceModels(response.models.filter(candidate => candidate.source === source
        && (!imageGeneration || candidate.text_to_image === true))))
      if (response.unavailable.length) {
        setError(`这些来源暂时读不到：${response.unavailable.join("、")}`)
      }
    } catch (failure) {
      if (version !== requestVersion.current) return
      setError(failure instanceof Error ? failure.message : "价目表读取失败")
    } finally {
      if (version === requestVersion.current) setSearching(false)
    }
  }

  const chooseModel = async (candidate: ModelPriceCandidate) => {
    if (candidate.source !== draft.priceSource) return
    const version = ++requestVersion.current
    const previousRule = chosenModel?.key === candidate.key ? contextPriceRulePayload(draft) : undefined
    const previousReferences = chosenModel?.key === candidate.key ? draft.contextPriceReferences ?? [] : []
    const preserveSelection = !!candidate.contextPricing && previousReferences.length > 0
    setDraft(current => ({ ...(preserveSelection ? current : resetContextPriceRule(current)),
      priceReference: "", contextPricingPreview: !!candidate.contextPricing,
      contextPriceReferences: preserveSelection ? previousReferences : [],
      contextPriceTiers: [], contextPriceRefresh: false }))
    setPreviewReference("")
    setChosenModel(candidate)
    setQuery(candidate.label)
    setCatalogOpen(false)
    setDetails(null)
    setLoadingOptions(true)
    setError(null)
    try {
      const response = await loadModelPrices(candidate)
      if (version !== requestVersion.current) return
      if (response.model_entry.source !== candidate.source) return
      setDetails(response)
      const selected = preserveSelection ? response.options.map(option => {
        const region = Object.entries(option.contextPricing?.regionalReferences ?? {})
          .find(([, references]) => references.length === previousReferences.length
            && references.every(reference => previousReferences.includes(reference)))?.[0]
        return region === undefined ? undefined : {
          ...option, reference: option.references_by_region?.[region] ?? option.reference,
        }
      }).find(option => option !== undefined) : response.options[0]
      if (response.complete && selected) {
        if (selected.contextPricing) setPreviewReference(selected.reference)
        setDraft((current) => {
          const next = applyCatalogOption(current, candidate.source, selected, discount.percent)
          return preserveSelection && previousRule
            ? { ...next, contextRuleMode: "custom", contextThreshold: String(previousRule.short_context_max_tokens),
              contextRuleNote: previousRule.note, contextRuleUrl: previousRule.source_url ?? "" }
            : next
        })
      } else if (response.complete && preserveSelection) {
        setError("分档价目与所选模型不匹配。")
      }
    } catch (failure) {
      if (version !== requestVersion.current) return
      setError(failure instanceof Error ? failure.message : "价目表读取失败")
    } finally {
      if (version === requestVersion.current) setLoadingOptions(false)
    }
  }

  const chooseOption = (option: ModelPriceOption) => {
    if (!chosenModel || !details?.complete) return
    if (option.contextPricing) setPreviewReference(option.reference)
    setDraft((current) =>
      applyCatalogOption(current, chosenModel.source, option, discount.percent))
  }

  return <div className="model-price-source">
    <div className="model-price-source-row">
    <div className="registry-field-label-row">
      <span className="registry-field-label">价格来源</span>
      <FieldHelp>
        「跟随官方价」按 官方价 × 折扣 自动维护单价；折扣由你维护，不会被任何来源覆盖。
      </FieldHelp>
    </div>
    <ButtonGroup className="model-tabs usage-metric-segment model-price-source-choice" aria-label="价格来源"
      data-value={draft.priceSource}>
      {(["models_dev", "manual", "azure_retail", "anthropic"] as const).filter(source => showAnthropic || source !== "anthropic").map((source) => (
        <button key={source} type="button" disabled={busy} aria-label={SOURCE_LABEL[source]}
          className={draft.priceSource === source ? "active" : ""}
          title={SOURCE_LABEL[source]} aria-pressed={draft.priceSource === source}
          onClick={() => {
            if (source === draft.priceSource) return
            clearSelection()
            setDraft(current => ({ ...current, priceSource: source }))
            if (source !== "manual" && source !== "models_dev") {
              const text = !model && modelName.trim() ? modelName.trim() : searchQuery
              setQuery(text)
              if (text) void search(text, source)
            }
          }}>
          {source === "models_dev" || source === "manual" ? SOURCE_LABEL[source] : source === "azure_retail" ? "Azure" : "Anthropic"}
        </button>
      ))}
    </ButtonGroup>
    </div>

    {draft.priceSource === "models_dev" && <>
      <PublicModelPricing
        request={publicRequest ?? { model_id: model?.id, operation: imageGeneration ? "image_generation" : "chat" }}
        draft={{ ...draft, contextWindow: draft.contextWindow ?? "" }}
        onChange={updater => setDraft(current => ({
          ...current, ...updater({ ...current, contextWindow: current.contextWindow ?? "" }),
        }))}
        busy={busy} />
      <DiscountField draft={draft} setDraft={setDraft} busy={busy} connectionDiscount={connectionDiscount} />
    </>}

    {following && <div className="model-price-follow">
      <div className="model-price-catalog-fields">
        <ModelStep
          busy={busy} query={searchQuery} setQuery={changeQuery} searching={searching}
          open={catalogOpen} onOpenChange={setCatalogOpen}
          matches={matches} chosen={chosenModel} onSearch={search} onChoose={chooseModel} />

        {details && <DeploymentStep
          details={details} selected={selectedOption}
          busy={busy || !details.complete} onChoose={chooseOption} />}

        {regionPeers.length > 1 && <RegionStep
          peers={regionPeers} selected={selectedOption}
          busy={busy || !details?.complete} onChoose={chooseOption} />}
      </div>

      {error && <p className="publication-form-note" role="alert"><ModelPriceText>{error}</ModelPriceText></p>}
      {loadingOptions && <p className="publication-form-note model-price-loading" role="status">
        <LoaderCircle className="spin" size={12} aria-hidden="true" />
        <span>正在读取该模型的价目…</span>
      </p>}

      <DiscountField
        draft={draft} setDraft={setDraft} busy={busy || details?.complete === false}
        connectionDiscount={connectionDiscount} />

      <PriceArithmetic model={model} draft={draft} setDraft={setDraft} percent={discount.percent}
        imageGeneration={imageGeneration}
        busy={busy || loadingOptions || details?.complete === false}
        option={details?.complete ? selectedOption : null} otherMeters={details?.other_meters ?? []} />

      {draft.contextPricingPreview && validateModelPricing(draft) === "context_pricing_preview" && <p className="publication-form-note" role="status">
        <ModelPriceText>当前后端尚不支持分档计费，暂不可保存或发布。</ModelPriceText>
      </p>}
      {draft.contextPricingPreview && validateModelPricing(draft) === "context_pricing_incomplete" && <p className="publication-form-note" role="status">
        <ModelPriceText>分档价格或阈值不完整，不能保存。</ModelPriceText>
      </p>}
      {draft.contextPricingPreview && draft.contextRuleMode === "custom" && !draft.contextPriceRulesSupported && <p className="publication-form-note" role="status">
        <ModelPriceText>当前后端尚不支持自定义阈值，暂不可保存或发布。</ModelPriceText>
      </p>}
      {draft.contextPricingPreview && chosenModel && model?.context_price_tiers?.length ? <div className="registry-field-label-row">
        <Button type="button" variant="outline" size="sm" disabled={busy || loadingOptions}
          onClick={() => void chooseModel(chosenModel)}><RefreshCw size={12} /><ModelPriceText>重新读取分档价格</ModelPriceText></Button>
        {model.pending_context_price_tiers?.length ? <span className="publication-form-note"><ModelPriceText>新的分档价格待复核</ModelPriceText></span> : null}
      </div> : null}
      {details && <CatalogNotices details={details} />}
    </div>}
  </div>
}

function ModelStep({ busy, query, setQuery, searching, open, onOpenChange, matches, chosen, onSearch, onChoose }: {
  busy: boolean
  query: string
  setQuery: (value: string) => void
  searching: boolean
  open: boolean
  onOpenChange: (open: boolean) => void
  matches: ModelPriceCandidate[] | null
  chosen: ModelPriceCandidate | null
  onSearch: () => void
  onChoose: (model: ModelPriceCandidate) => void
}) {
  const input = useRef<HTMLInputElement | null>(null)
  const search = () => {
    input.current?.focus()
    onSearch()
  }
  const candidates = matches ?? (chosen ? [chosen] : [])

  return <div className="registry-field">
    <div className="registry-field-label-row">
      <label htmlFor="model-price-model" className="registry-field-label">模型</label>
      <FieldHelp>
        这是官方价目里的模型。由你确认一次，之后同步按这个模型取值。
      </FieldHelp>
    </div>
      <Combobox.Root<ModelPriceCandidate>
          items={candidates} filter={null} disabled={busy}
          inputValue={query} onInputValueChange={(value, event) => {
            if (event.reason === "input-change" || event.reason === "clear-press") setQuery(value)
          }}
          value={chosen} itemToStringLabel={candidate => candidate.label}
          isItemEqualToValue={(candidate, selected) => candidate.key === selected.key}
          openOnInputClick
          open={open && (searching || matches !== null || chosen !== null)} onOpenChange={onOpenChange}
          onValueChange={candidate => {
            if (!candidate || searching || busy) return
            onOpenChange(false)
            onChoose(candidate)
          }}>
          <div className="model-price-search">
            <Combobox.Input ref={input} id="model-price-model" render={<Input />}
              placeholder="搜索模型，例如 gpt 5、opus、grok" autoComplete="off"
              onKeyDown={event => {
                if (event.key === "Enter" && !event.nativeEvent.isComposing
                  && !event.currentTarget.getAttribute("aria-activedescendant")) {
                  event.preventDefault()
                  if (!searching && !busy) search()
                }
              }} />
            <div className="model-price-input-actions">
              <Button type="button" variant="ghost" size="icon-sm" disabled={busy || searching}
                aria-label="搜索" title="搜索" onClick={search}>
                {searching ? <LoaderCircle className="spin" size={14} /> : <Search size={14} />}
              </Button>
              <Combobox.Trigger render={<Button variant="ghost" size="icon-sm" />}
                aria-label="选择模型" title="选择模型" disabled={busy}
                onClick={() => {
                  if (!open && matches === null && !chosen && !searching) search()
                }}>
                <ChevronDown size={14} />
              </Combobox.Trigger>
            </div>
          </div>
          <Combobox.Portal>
            <Combobox.Positioner anchor={input} align="start" side="bottom" sideOffset={4}
              className="select-positioner">
              <Combobox.Popup data-slot="select-content" data-align-trigger={false}
                className="model-price-catalog-popup">
                {searching && <div data-slot="select-item" data-disabled role="status">
                  <span data-slot="select-item-text"><span className="registry-option">
                    <LoaderCircle className="spin" size={14} />正在读取该模型的价目…
                  </span></span>
                </div>}
                {!searching && matches?.length === 0 && <div data-slot="select-item" data-disabled role="status">
                  <span data-slot="select-item-text">没有匹配的模型。</span>
                </div>}
                <Combobox.List className="model-price-results" aria-label="官方价目里的模型">
                  {candidates.map(candidate => <Combobox.Item key={candidate.key} value={candidate}
                    disabled={searching} data-slot="select-item" title={`${candidate.label} · ${candidate.product}`}>
                    <span data-slot="select-item-text"><span className="registry-option">
                      <ModelBrandLogo identity={`${candidate.label} ${candidate.product}`} providerName={candidate.product} size={16} />
                      <span data-no-localize>{candidate.label} · {candidate.product}</span>
                    </span></span>
                    <Combobox.ItemIndicator data-slot="select-item-indicator"><Check size={14} /></Combobox.ItemIndicator>
                  </Combobox.Item>)}
                </Combobox.List>
              </Combobox.Popup>
            </Combobox.Positioner>
          </Combobox.Portal>
        </Combobox.Root>
  </div>
}

function DeploymentStep({ details, selected, busy, onChoose }: {
  details: ModelPriceOptions
  selected: ModelPriceOption | null
  busy: boolean
  onChoose: (option: ModelPriceOption) => void
}) {
  // One row per deployment, priced at its cheapest variant. The regions inside a deployment are
  // a second question, and only when they disagree.
  const shapes = new Map<string, ModelPriceOption>()
  for (const option of details.options) {
    const existing = shapes.get(option.deployment)
    if (!existing || (option.input_per_million ?? 0) < (existing.input_per_million ?? 0)) {
      shapes.set(option.deployment, option)
    }
  }
  if (!shapes.size) return <p className="publication-form-note">该模型没有可用于对话调用的价目。</p>
  const selectedLabel = selected
    ? selected.contextPricing ? selected.deployment
      : `${selected.deployment} · ${money(selected.input_per_million)} / ${money(selected.output_per_million)}`
    : ""
  return <div className="registry-field">
    <div className="registry-field-label-row">
      <span className="registry-field-label">计价方式</span>
      <FieldHelp>对应你在 Foundry 上的部署形态。Global 在所有区域同价，无需选区域。</FieldHelp>
    </div>
    <Select name="price-deployment"
      disabled={busy} value={selected?.deployment ?? ""}
      onValueChange={(deployment) => {
        if (!deployment || deployment === selected?.deployment) return
        const option = shapes.get(deployment)
        if (option) onChoose(option)
      }}>
      <SelectTrigger className="registry-select-trigger" aria-label="计价方式" title={selectedLabel}>
        <SelectValue><span data-no-localize>{selectedLabel}</span></SelectValue>
      </SelectTrigger>
      <SelectContent align="start" alignItemWithTrigger={false}>
        {[...shapes.values()].map(option => (
          <SelectItem key={option.deployment} value={option.deployment}>
            <span data-no-localize>{option.contextPricing ? option.deployment
              : `${option.deployment} · ${money(option.input_per_million)} / ${money(option.output_per_million)}`}</span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  </div>
}

function RegionStep({ peers, selected, busy, onChoose }: {
  peers: ModelPriceOption[]
  selected: ModelPriceOption | null
  busy: boolean
  onChoose: (option: ModelPriceOption) => void
}) {
  const selectedRegion = selected?.regions.find((region) =>
    (selected.references_by_region?.[region] ?? selected.reference) === selected.reference) ?? ""

  return <div className="registry-field">
      <div className="registry-field-label-row">
        <span className="registry-field-label">区域</span>
        <FieldHelp>这种计价方式下各区域单价不同，请选你实际部署所在的区域。</FieldHelp>
      </div>
      <Select name="price-region" disabled={busy} value={selected?.reference ?? ""}
        onValueChange={(picked) => {
          if (!picked || picked === selected?.reference) return
          // Grouping regions that charge alike is a display decision. What gets stored is the
          // region the person actually chose, so that the day the vendor prices them apart this
          // model follows its own region rather than whichever one sorted first.
          const next = peers.find((option) =>
            option.reference === picked
            || Object.values(option.references_by_region ?? {}).includes(picked))
          if (next) onChoose({ ...next, reference: picked })
        }}>
        <SelectTrigger className="registry-select-trigger" aria-label="区域">
          <SelectValue><span data-no-localize>{selectedRegion}</span></SelectValue>
        </SelectTrigger>
        <SelectContent align="start" alignItemWithTrigger={false}>
          {peers.map((option) => (
            <SelectGroup key={option.reference}>
              <SelectLabel>
                {option.contextPricing ? option.contextPricing.tiers.map((tier, index) => <span key={tier.key}>
                  {index > 0 && " · "}<ModelPriceText>{tier.label}</ModelPriceText>{" "}
                  <span data-no-localize>{money(tier.option.input_per_million)} / {money(tier.option.output_per_million)}</span>
                </span>) : <span data-no-localize>{money(option.input_per_million)} / {money(option.output_per_million)}</span>}
              </SelectLabel>
              {option.regions.map((region) => (
                <SelectItem key={region}
                  value={option.references_by_region?.[region] ?? option.reference}>
                  <span data-no-localize>{region}</span>
                </SelectItem>
              ))}
            </SelectGroup>
          ))}
        </SelectContent>
      </Select>
  </div>
}

function DiscountField<Draft extends ModelPriceDraft>({ draft, setDraft, busy, connectionDiscount }: {
  draft: Draft
  setDraft: (updater: (current: Draft) => Draft) => void
  busy: boolean
  connectionDiscount: number | null
}) {
  return <div className="registry-field">
    <div className="registry-field-label-row">
      <label htmlFor="model-discount" className="registry-field-label">折扣</label>
      <FieldHelp>
        官方价乘以这个百分比得到实际单价。90 表示九折。留空则继承连接的折扣。
      </FieldHelp>
    </div>
    <div className="model-price-discount-input">
    <Input id="model-discount" type="number" inputMode="decimal" min={0} max={100} step="any"
      disabled={busy} value={draft.discountPercent}
      placeholder={connectionDiscount === null
        ? "继承连接（未设折扣）" : `继承连接（${connectionDiscount}%）`}
      onChange={(event) =>
        setDraft((current) => ({ ...current, discountPercent: event.target.value }))} />
    <span className="model-editor-unit" data-no-localize>%</span>
    </div>
  </div>
}

export function ModelPriceText({ children }: { children: string }) {
  const { locale } = useLocale()
  const [translation, setTranslation] = useState<{ source: string; locale: string; text: string } | null>(null)
  useEffect(() => {
    let active = true
    void translateForLocale(children, locale).then(text => {
      if (active) setTranslation({ source: children, locale, text })
    }).catch(() => undefined)
    return () => { active = false }
  }, [children, locale])
  return <span data-no-localize>{translation?.source === children && translation.locale === locale ? translation.text : children}</span>
}

function ContextRuleFields<Draft extends ModelPriceDraft>({ draft, setDraft, busy }: {
  draft: Draft
  setDraft: (updater: (current: Draft) => Draft) => void
  busy: boolean
}) {
  const mode = draft.contextRuleMode ?? "catalog"
  const tiers = draft.contextPriceTiers ?? []
  const pair = tiers.length === 2 && tiers.some(tier => tier.key === "short") && tiers.some(tier => tier.key === "long")
  const referenceOnly = tiers.some(tier => tier.inputRangeSource?.origin === "reference")
  const maximum = Number(draft.contextThreshold)
  const invalidThreshold = !!draft.contextThreshold && (!Number.isSafeInteger(maximum) || maximum <= 0 || maximum >= Number.MAX_SAFE_INTEGER)
  const update = (fields: Partial<ModelPriceDraft>) => setDraft(current => ({ ...current, ...fields, contextPriceRefresh: true }))
  return <div className="model-price-rule">
    <div className="model-price-rule-row">
      <div className="model-price-rule-mode">
        <div className="registry-field-label-row">
          <span className="registry-field-label">阈值规则</span>
          <FieldHelp>{mode === "custom" ? "管理员自定义估算，非官方计费规则。" : "按单次请求的完整输入 Tokens 选档（含缓存输入），不含输出 Tokens；整次请求使用对应档位价格。"}</FieldHelp>
        </div>
        <ButtonGroup className="model-tabs usage-metric-segment model-price-rule-choice" aria-label="阈值来源" data-value={mode}>
          <button type="button" disabled={busy} className={mode === "catalog" ? "active" : ""} aria-pressed={mode === "catalog"}
            onClick={() => setDraft(current => ({ ...resetContextPriceRule(current), contextPriceRefresh: true }))}>目录规则</button>
          <button type="button" disabled={busy || !pair} className={mode === "custom" ? "active" : ""} aria-pressed={mode === "custom"}
            onClick={() => update({ contextRuleMode: "custom" })}>管理员自定义</button>
        </ButtonGroup>
      </div>
      {mode === "custom" && <div className="model-price-rule-fields">
        <label className="registry-field" htmlFor="model-context-threshold">
          <span className="registry-field-label-row"><span className="registry-field-label">短上下文上限</span><span className="model-editor-unit" data-no-localize>Tokens</span></span>
          <Input id="model-context-threshold" type="number" inputMode="numeric" min={1} max={9007199254740990} step={1}
            required disabled={busy} aria-invalid={invalidThreshold || undefined} value={draft.contextThreshold ?? ""}
            onChange={event => update({ contextThreshold: event.target.value, contextRuleNote: "", contextRuleUrl: "" })} />
        </label>
      </div>}
    </div>
    {mode !== "custom" && (referenceOnly || tiers.some(tier => !tier.inputRange)) ? <p className="publication-form-note" role="status">
      <ModelPriceText>{referenceOnly ? "目录只有参考说明，计费阈值尚未验证。" : "目录未提供上下文计费阈值。"}</ModelPriceText>
    </p> : null}
  </div>
}

function ContextPriceTable({ pricing, percent }: {
  pricing: NonNullable<ModelPriceOption["contextPricing"]>
  percent: number | null
}) {
  const rows = [
    ["input_per_million", "输入"],
    ["output_per_million", "输出"],
    ["cached_per_million", "缓存读取"],
    ["cache_write_per_million", "缓存写入"],
  ] as const
  return <div className="model-price-context-scroll">
    <ResizableTable resizable={false} className="model-price-context-table" style={{ minWidth: 120 + pricing.tiers.length * 184 }}>
    <colgroup><col className="model-price-context-item" />{pricing.tiers.flatMap(tier => [<col key={`${tier.key}-list`} />, <col key={`${tier.key}-charged`} />])}</colgroup>
    <thead>
      <tr>
        <th rowSpan={2} scope="col">计费项</th>
        {pricing.tiers.map(tier => {
          const range = tier.inputRange
          const minimum = range?.min_input_tokens.toLocaleString("en-US")
          const maximum = range?.max_input_tokens?.toLocaleString("en-US")
          const rangeLabel = !range ? null : range.max_input_tokens === null ? `≥ ${minimum}`
            : range.min_input_tokens === 0 ? `≤ ${maximum}` : `${minimum} - ${maximum}`
          return <th key={tier.key} colSpan={2} scope="colgroup">
            <ModelPriceText>{tier.label}</ModelPriceText>
            <small>{rangeLabel === null ? <ModelPriceText>阈值未提供</ModelPriceText>
              : <><span>输入</span>{" "}{tier.inputRangeSource?.url
                ? <a href={tier.inputRangeSource.url} target="_blank" rel="noopener noreferrer"
                  title={`${tier.inputRangeSource.url}\n${tier.inputRangeSource.verified_on}`} data-no-localize>{rangeLabel}</a>
                : <span data-no-localize>{rangeLabel}</span>}</>}</small>
            {tier.inputRangeSource?.origin === "reference" && <small><ModelPriceText>参考阈值</ModelPriceText></small>}
          </th>
        })}
      </tr>
      <tr>{pricing.tiers.flatMap(tier => [<th key={`${tier.key}-list`} scope="col">官方价</th>, <th key={`${tier.key}-charged`} scope="col">实际单价</th>])}</tr>
    </thead>
    <tbody>
      {rows.map(([bucket, label]) => <tr key={bucket} data-price-bucket={bucket}>
        <th scope="row">{label}</th>
        {pricing.tiers.flatMap(tier => {
          const rate = tier.option[bucket]
          const effectiveRate = rate ?? (bucket === "cache_write_per_million"
            ? tier.option.cached_per_million ?? tier.option.input_per_million
            : bucket === "cached_per_million" ? tier.option.input_per_million : null)
          return [
            <td key={`${tier.key}-list`} data-no-localize>{money(rate)}</td>,
            <td key={`${tier.key}-charged`} data-no-localize className="model-price-grid-actual">{money(effectiveRate === null ? null : discountedRate(effectiveRate, percent))}</td>,
          ]
        })}
      </tr>)}
    </tbody>
    </ResizableTable>
  </div>
}

function PriceArithmetic<Draft extends ModelPriceDraft>({ model, draft, setDraft, busy, percent, option, otherMeters, imageGeneration }: {
  model?: ManagedModel
  draft: Draft
  setDraft: (updater: (current: Draft) => Draft) => void
  busy: boolean
  percent: number | null
  option: ModelPriceOption | null
  otherMeters: string[]
  imageGeneration: boolean
}) {
  // A freshly picked option wins over the last sync, so the list price and the charged rate in
  // each row always come from the same place and the multiplication reads true.
  const list = option !== null ? {
    input: option.input_per_million,
    output: option.output_per_million,
    cached: option.cached_per_million,
    cacheWrite: option.cache_write_per_million,
  } : {
    input: model?.list_input_cost_per_million ?? null,
    output: model?.list_output_cost_per_million ?? null,
    cached: model?.list_cached_cost_per_million ?? null,
    cacheWrite: model?.list_cache_write_cost_per_million ?? null,
  }
  const charged = {
    input: draft.inputPrice ? Number(draft.inputPrice) : null,
    output: draft.outputPrice ? Number(draft.outputPrice) : null,
    cached: draft.cacheReadPrice ? Number(draft.cacheReadPrice) : null,
    cacheWrite: draft.cacheWritePrice ? Number(draft.cacheWritePrice) : null,
  }
  const rows: Array<[string, number | null, number | null]> = [
    [imageGeneration ? "文字输入" : "输入", list.input, charged.input],
    [imageGeneration ? "图像输出" : "输出", list.output, charged.output],
    [imageGeneration ? "缓存文字输入" : "缓存读取", list.cached, charged.cached],
  ]
  if (!imageGeneration) rows.push(["缓存写入", list.cacheWrite, charged.cacheWrite])
  return <div className="model-price-arithmetic">
    <div className="registry-field-label-row">
      <span className="registry-field-label">实际单价</span>
      <span className="model-editor-unit" data-no-localize>USD / 1M Tokens</span>
    </div>
    {draft.contextPricingPreview && <ContextRuleFields draft={draft} setDraft={setDraft} busy={busy} />}
    {option?.contextPricing ? <ContextPriceTable pricing={{ ...option.contextPricing, tiers: effectiveContextPriceTiers(draft) }} percent={percent} /> : <div className="model-price-grid">
      <div className="model-price-grid-head">
        <span>计费项</span><span>官方价</span><span>折扣</span><span>实际单价</span>
      </div>
      {rows.map(([label, listed, actual]) => (
        <div className="model-price-grid-row" key={label}>
          <span>{label}</span>
          <span data-no-localize>{money(listed)}</span>
          <span data-no-localize>{percent === null ? "—" : `${percent}%`}</span>
          <span data-no-localize className="model-price-grid-actual">
            {actual !== null
              ? money(actual)
              : listed !== null ? money(discountedRate(listed, percent)) : "—"}
          </span>
        </div>
      ))}
    </div>}
    {option !== null && !option.contextPricing && <p className="publication-form-note">
      保存后按此基准计费；下次同步会沿用它。
    </p>}
    {option === null && model?.price_synced_at && <p className="publication-form-note">
      <RefreshCw size={12} /> 最近同步 {new Date(model.price_synced_at).toLocaleString()}
      {model.price_sync_status && model.price_sync_status !== "ok"
        && ` · ${SYNC_STATUS_LABEL[model.price_sync_status] ?? model.price_sync_status}`}
      {model.price_sync_message && ` · ${model.price_sync_message}`}
    </p>}
    {!imageGeneration && !option?.contextPricing && list.cacheWrite === null && (option !== null || model?.price_synced_at) && <p className="publication-form-note">
      该来源未单独发布缓存写入价，按缓存读取单价计费。
    </p>}
    {otherMeters.length > 0 && <details className="model-editor-advanced">
      <summary>
        <span>计的是别的东西的计量表</span>
        <span data-no-localize>{otherMeters.length}</span>
      </summary>
      <div className="model-editor-advanced-body">
        <p className="publication-form-note">
          批量调用、微调、预留吞吐等，不是普通对话调用的单价，因此不作为选项。
        </p>
        {otherMeters.map((name) => (
          <code key={name} data-no-localize>{name}</code>
        ))}
      </div>
    </details>}
  </div>
}

function CatalogNotices({ details }: { details: PriceCatalogOptionsResponse }) {
  return <>
    {details.note && <p className="publication-form-note">{details.note}</p>}
    {details.unreadable.length > 0 && <details className="model-editor-advanced">
      {/* The count sits in its own untranslated element rather than inside the sentence. An
          interpolated sentence has to be matched by a dynamic rule, and a generic fragment rule
          earlier in the table -- `(\d+)\s*条` -> "$1 records" -- rewrites the digits first, so
          the whole-sentence rule never fires and the line renders half translated. */}
      <summary>
        <AlertTriangle size={12} />
        <span>未能识别的计量表</span>
        <span data-no-localize>{details.unreadable.length}</span>
      </summary>
      <div className="model-editor-advanced-body">
        <p className="publication-form-note">
          这些计量表提到了该模型，但名称写法本系统还不认识，因此没有纳入上面的选项。
          如果你要的价在里面，告诉我们即可补上。
        </p>
        {details.unreadable.map((name) => (
          <code key={name} data-no-localize>{name}</code>
        ))}
      </div>
    </details>}
  </>
}
