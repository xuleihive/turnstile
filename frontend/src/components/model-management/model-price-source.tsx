import { AlertTriangle, LoaderCircle, RefreshCw, Search } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import { dataSource } from "../../data-sources/apim/api"
import type {
  ManagedModel,
  PriceCatalogModel,
  PriceCatalogOption,
  PriceCatalogOptionsResponse,
} from "../../data-sources/apim/types"
import { Button } from "../ui/button"
import { Input } from "../ui/input"
import { FieldHelp } from "./field-help"
import { PublicModelPricing } from "./public-model-pricing"
import {
  applyCatalogOption,
  discountedRate,
  modelKeyFromReference,
  resolveDiscount,
  type ModelEditDraft,
} from "./model-edit-form"

const SOURCE_LABEL: Record<string, string> = {
  models_dev: "从公网同步定价",
  manual: "手工填写",
  azure_retail: "Azure 零售价",
  anthropic: "Anthropic 列表价",
}

const SYNC_STATUS_LABEL: Record<string, string> = {
  ok: "已同步",
  unmapped: "未映射",
  stale: "来源读取失败，保留上次单价",
  review_needed: "官方价变动较大，待复核",
}

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—"
  return `$${value.toFixed(value < 1 ? 4 : 2)}`
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
export function ModelPriceSourceFields({ model, draft, setDraft, busy, connectionDiscount }: {
  model: ManagedModel
  draft: ModelEditDraft
  setDraft: (updater: (current: ModelEditDraft) => ModelEditDraft) => void
  busy: boolean
  connectionDiscount: number | null
}) {
  const [query, setQuery] = useState(() => model.display_name.split("·")[0].trim())
  const [matches, setMatches] = useState<PriceCatalogModel[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [chosenModel, setChosenModel] = useState<PriceCatalogModel | null>(null)
  const [details, setDetails] = useState<PriceCatalogOptionsResponse | null>(null)
  const [loadingOptions, setLoadingOptions] = useState(false)
  // Set when someone asks to choose a different model. Without it the effect below sees an empty
  // picker and a stored reference and helpfully puts the old choice straight back, so the button
  // appears to do nothing.
  const [reselecting, setReselecting] = useState(false)
  const generation = useRef({ search: 0, options: 0 })
  const currentSource = useRef(draft.priceSource)
  currentSource.current = draft.priceSource
  const imageGeneration = model.capabilities.includes("image_generation")

  const following = draft.priceSource !== "manual" && draft.priceSource !== "models_dev"
  const discount = resolveDiscount(draft, connectionDiscount)
  const storedModelKey = draft.priceReference ? modelKeyFromReference(draft.priceReference) : null

  useEffect(() => {
    setMatches(null)
    setChosenModel(null)
    setDetails(null)
    setError(null)
    setSearching(false)
    setLoadingOptions(false)
    setReselecting(false)
    return () => {
      generation.current.search++
      generation.current.options++
    }
  }, [draft.priceSource, model.id])

  // Reopening the dialog on a model that already follows a list price should show what it
  // follows, not an empty picker.
  useEffect(() => {
    if (!following || chosenModel || !storedModelKey || details || reselecting
      || !storedModelKey.startsWith(`${draft.priceSource}:`)) return
    let cancelled = false
    const source = draft.priceSource
    setLoadingOptions(true)
    dataSource.priceCatalogOptions(storedModelKey)
      .then((response) => {
        if (cancelled || currentSource.current !== source
          || response.model_entry.source !== source) return
        setChosenModel(response.model_entry)
        setDetails(response)
      })
      .catch(() => undefined)
      .finally(() => { if (!cancelled) setLoadingOptions(false) })
    return () => { cancelled = true }
  }, [following, draft.priceSource, storedModelKey, chosenModel, details, reselecting])

  // A stored reference names one region, while an option covers every region charging alike, so
  // matching on the option's own reference alone would fail to recognise a saved choice that is
  // not the one region the group happens to be anchored at.
  const selectedOption = useMemo(() => {
    const found = details?.options.find((option) =>
      option.reference === draft.priceReference
      || Object.values(option.references_by_region ?? {}).includes(draft.priceReference ?? ""))
    if (!found) return null
    return found.reference === draft.priceReference
      ? found
      : { ...found, reference: draft.priceReference ?? found.reference }
  }, [details, draft.priceReference])
  // Sibling options for the same deployment are the regions worth choosing between; when a
  // deployment charges one figure everywhere there are no siblings and no question to ask.
  const regionPeers = useMemo(() => {
    if (!details || !selectedOption?.region_required) return []
    return details.options.filter((option) => option.deployment === selectedOption.deployment)
  }, [details, selectedOption])

  // Re-pricing on a discount change keeps the rates honest while the dialog is open; without it
  // the box would say 66% while the rates below still showed the old figures.
  useEffect(() => {
    if (!following || !selectedOption || !chosenModel || !details?.complete
      || chosenModel.source !== draft.priceSource) return
    setDraft((current) => current.priceSource === chosenModel.source
      ? applyCatalogOption(current, current.priceSource, selectedOption, discount.percent) : current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.discountPercent])

  const search = async () => {
    const source = draft.priceSource
    const token = ++generation.current.search
    const active = () => token === generation.current.search && currentSource.current === source
    setSearching(true)
    setError(null)
    try {
      const response = await dataSource.priceCatalogModels(query, source)
      if (!active()) return
      setMatches(response.models.filter(candidate => candidate.source === source))
      const unavailable = response.unavailable.filter(value => value === source)
      if (unavailable.length) {
        setError(`这些来源暂时读不到：${unavailable.join("、")}`)
      }
    } catch (failure) {
      if (active()) setError(failure instanceof Error ? failure.message : "价目表读取失败")
    } finally {
      if (active()) setSearching(false)
    }
  }

  const chooseModel = async (candidate: PriceCatalogModel) => {
    const source = draft.priceSource
    if (candidate.source !== source || !candidate.key.startsWith(`${source}:`)) return
    const token = ++generation.current.options
    const active = () => token === generation.current.options && currentSource.current === source
    generation.current.search++
    setSearching(false)
    setReselecting(false)
    setChosenModel(candidate)
    setDetails(null)
    setMatches(null)
    setLoadingOptions(true)
    setError(null)
    try {
      const response = await dataSource.priceCatalogOptions(candidate.key)
      if (!active() || response.model_entry.source !== source
        || response.model_entry.key !== candidate.key) return
      setDetails(response)
      const first = response.options[0]
      if (response.complete && first?.reference.startsWith(`${source}:`)) {
        setDraft((current) => current.priceSource === source
          ? applyCatalogOption(current, source, first, discount.percent) : current)
      }
    } catch (failure) {
      if (active()) setError(failure instanceof Error ? failure.message : "价目表读取失败")
    } finally {
      if (active()) setLoadingOptions(false)
    }
  }

  const chooseOption = (option: PriceCatalogOption) => {
    if (!chosenModel || !details?.complete || chosenModel.source !== draft.priceSource
      || !option.reference.startsWith(`${draft.priceSource}:`)) return
    setDraft((current) => current.priceSource === chosenModel.source
      ? applyCatalogOption(current, current.priceSource, option, discount.percent) : current)
  }

  return <div className="model-price-source">
    <div className="registry-field-label-row">
      <span className="registry-field-label">价格来源</span>
      <FieldHelp>
        {draft.priceSource === "models_dev"
          ? "公开参考价用于费用估算，不代表实际账单。"
          : "「跟随官方价」按 官方价 × 折扣 自动维护单价；折扣由你维护，不会被任何来源覆盖。"}
      </FieldHelp>
    </div>
    <div className="model-price-source-choice" role="radiogroup" aria-label="价格来源">
      {(imageGeneration ? ["models_dev", "manual"] as const
        : ["models_dev", "manual", "azure_retail", "anthropic"] as const).map((source) => (
        <label key={source} className="model-editor-checkbox">
          <input type="radio" name="price-source" value={source} disabled={busy}
            checked={draft.priceSource === source}
            onChange={() => {
              generation.current.search++
              generation.current.options++
              currentSource.current = source
              setDraft((current) => ({
                ...current, priceSource: source,
                priceReference: source === current.priceSource ? current.priceReference : "",
                priceEntryDigest: "", allowUnpriced: false,
              }))
            }} />
          <span>{SOURCE_LABEL[source]}</span>
        </label>
      ))}
    </div>
    {draft.priceSource === "models_dev" && <PublicModelPricing
      request={{ model_id: model.id, runtime_id: model.runtime_id,
        operation: imageGeneration ? "image_generation" : "chat" }}
      draft={draft} busy={busy}
      onChange={updater => setDraft(current => ({ ...current, ...updater(current) }))} />}
    {draft.priceSource === "models_dev" && <DiscountField
      draft={draft} busy={busy} connectionDiscount={connectionDiscount} discount={discount}
      setDraft={updater => setDraft(current => ({ ...current, ...updater(current) }))} />}

    {following && <div className="model-price-follow">
      <ModelStep
        busy={busy} query={query} setQuery={setQuery} searching={searching}
        matches={matches} chosen={chosenModel} onSearch={search} onChoose={chooseModel}
        onClear={() => {
          generation.current.options++
          generation.current.search++
          setLoadingOptions(false)
          setSearching(false)
          setReselecting(true)
          setChosenModel(null); setDetails(null); setMatches(null)
        }} />

      {error && <p className="publication-form-note" role="alert">{error}</p>}
      {loadingOptions && <p className="publication-form-note">
        <LoaderCircle className="spin" size={12} /> 正在读取该模型的价目…
      </p>}

      {details && <DeploymentStep
        details={details} selected={selectedOption}
        busy={busy || !details.complete} onChoose={chooseOption} />}

      {regionPeers.length > 1 && <RegionStep
        peers={regionPeers} selected={selectedOption}
        busy={busy || !details?.complete} onChoose={chooseOption} />}

      <DiscountField
        draft={draft} setDraft={updater => setDraft(current => ({ ...current, ...updater(current) }))}
        busy={busy || details?.complete === false}
        connectionDiscount={connectionDiscount} discount={discount} />

      <PriceArithmetic model={model} draft={draft} percent={discount.percent}
        option={details?.complete ? selectedOption : null} />

      {details && <CatalogNotices details={details} />}
    </div>}
  </div>
}

function ModelStep({ busy, query, setQuery, searching, matches, chosen, onSearch, onChoose,
  onClear }: {
  busy: boolean
  query: string
  setQuery: (value: string) => void
  searching: boolean
  matches: PriceCatalogModel[] | null
  chosen: PriceCatalogModel | null
  onSearch: () => void
  onChoose: (model: PriceCatalogModel) => void
  onClear: () => void
}) {
  return <div className="registry-field">
    <div className="registry-field-label-row">
      <span className="registry-field-label">官方价目里的模型</span>
      <FieldHelp>
        由你确认一次，之后同步按这个模型取值。按名称自动匹配会错，而且错了看不出来。
      </FieldHelp>
    </div>
    {chosen
      ? <div className="model-price-chosen">
          <b data-no-localize>{chosen.label}</b>
          <small data-no-localize>{chosen.product}</small>
          <button type="button" disabled={busy} onClick={onClear}>重新选择</button>
        </div>
      : <div className="model-price-search">
          <Input value={query} disabled={busy} placeholder="搜索模型，例如 gpt 5、opus、grok"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") { event.preventDefault(); onSearch() }
            }} />
          <Button type="button" variant="secondary" size="sm" disabled={busy || searching}
            onClick={onSearch}>
            {searching ? <LoaderCircle className="spin" size={14} /> : <Search size={14} />}
            搜索
          </Button>
        </div>}
    {!chosen && matches && matches.length === 0 && (
      <p className="publication-form-note">没有匹配的模型。</p>
    )}
    {!chosen && matches && matches.length > 0 && <ul className="model-price-results">
      {matches.map((candidate) => (
        <li key={candidate.key}>
          <button type="button" disabled={busy} onClick={() => onChoose(candidate)}>
            <span className="model-price-result-label" data-no-localize>{candidate.label}</span>
            <span className="model-price-result-detail" data-no-localize>{candidate.product}</span>
          </button>
        </li>
      ))}
    </ul>}
  </div>
}

function DeploymentStep({ details, selected, busy, onChoose }: {
  details: PriceCatalogOptionsResponse
  selected: PriceCatalogOption | null
  busy: boolean
  onChoose: (option: PriceCatalogOption) => void
}) {
  // One row per deployment, priced at its cheapest variant. The regions inside a deployment are
  // a second question, and only when they disagree.
  const shapes = new Map<string, PriceCatalogOption>()
  for (const option of details.options) {
    const existing = shapes.get(option.deployment)
    if (!existing || (option.input_per_million ?? 0) < (existing.input_per_million ?? 0)) {
      shapes.set(option.deployment, option)
    }
  }
  if (!shapes.size) return <p className="publication-form-note">该模型没有可用于对话调用的价目。</p>
  return <div className="registry-field">
    <div className="registry-field-label-row">
      <span className="registry-field-label">计价方式</span>
      <FieldHelp>对应你在 Foundry 上的部署形态。Global 在所有区域同价，无需选区域。</FieldHelp>
    </div>
    <div className="model-price-options" role="radiogroup" aria-label="计价方式">
      {[...shapes.values()].map((option) => (
        <label key={option.deployment}
          className={selected?.deployment === option.deployment ? "active" : ""}>
          <input type="radio" name="price-deployment" disabled={busy}
            checked={selected?.deployment === option.deployment}
            onChange={() => onChoose(option)} />
          <span className="model-price-option-name" data-no-localize>{option.deployment}</span>
          <span className="model-price-option-rate" data-no-localize>
            入 {money(option.input_per_million)} / 出 {money(option.output_per_million)}
          </span>
          <span className="model-price-option-note">
            {option.region_required ? "按区域不同" : "全区域同价"}
          </span>
        </label>
      ))}
    </div>
  </div>
}

function RegionStep({ peers, selected, busy, onChoose }: {
  peers: PriceCatalogOption[]
  selected: PriceCatalogOption | null
  busy: boolean
  onChoose: (option: PriceCatalogOption) => void
}) {
  return <div className="registry-field">
    <div className="registry-field-label-row">
      <span className="registry-field-label">区域</span>
      <FieldHelp>这种计价方式下各区域单价不同，请选你实际部署所在的区域。</FieldHelp>
    </div>
    <select className="model-price-region" disabled={busy}
      value={selected?.reference ?? ""}
      onChange={(event) => {
        const picked = event.target.value
        // Grouping regions that charge alike is a display decision. What gets stored is the
        // region the person actually chose, so that the day the vendor prices them apart this
        // model follows its own region rather than whichever one sorted first.
        const next = peers.find((option) =>
          option.reference === picked
          || Object.values(option.references_by_region ?? {}).includes(picked))
        if (next) onChoose({ ...next, reference: picked })
      }}>
      {peers.map((option) => (
        <optgroup key={option.reference}
          label={`${money(option.input_per_million)} / ${money(option.output_per_million)}`}>
          {option.regions.map((region) => (
            <option key={region}
              value={option.references_by_region?.[region] ?? option.reference}>{region}</option>
          ))}
        </optgroup>
      ))}
    </select>
    {selected && selected.regions.length > 1 && <p className="publication-form-note">
      {`与其他 ${selected.regions.length - 1} 个区域同价。`}
    </p>}
  </div>
}

export function DiscountField({ draft, setDraft, busy, connectionDiscount, discount }: {
  draft: Pick<ModelEditDraft, "discountPercent"> & Partial<Pick<ModelEditDraft, "priceSource">>
  setDraft: (updater: (current: Pick<ModelEditDraft, "discountPercent">) => Pick<ModelEditDraft, "discountPercent">) => void
  busy: boolean
  connectionDiscount: number | null
  discount: { percent: number | null; inherited: boolean }
}) {
  return <div className="registry-field">
    <div className="registry-field-label-row">
      <label htmlFor="model-discount" className="registry-field-label">折扣</label>
      <span className="model-editor-unit" data-no-localize>%</span>
      <FieldHelp>
        {draft.priceSource === "models_dev"
          ? "参考价乘以此百分比。留空继承连接折扣。"
          : "官方价乘以这个百分比得到实际单价。90 表示九折。留空则继承连接的折扣。"}
      </FieldHelp>
    </div>
    <Input id="model-discount" type="number" inputMode="decimal" min={0} max={100} step="any"
      disabled={busy} value={draft.discountPercent}
      placeholder={connectionDiscount === null
        ? "继承连接（未设折扣）" : `继承连接（${connectionDiscount}%）`}
      onChange={(event) =>
        setDraft((current) => ({ ...current, discountPercent: event.target.value }))} />
    <p className="publication-form-note">
      {discount.percent === null
        ? draft.priceSource === "models_dev" ? "当前按公开参考原价计费。" : "当前按官方价原价计费。"
        : discount.inherited
          ? `当前继承连接折扣 ${discount.percent}%。`
          : `当前使用本模型单独设置的 ${discount.percent}%。`}
    </p>
  </div>
}

function PriceArithmetic({ model, draft, percent, option }: {
  model: ManagedModel
  draft: ModelEditDraft
  percent: number | null
  option: PriceCatalogOption | null
}) {
  // A freshly picked option wins over the last sync, so the list price and the charged rate in
  // each row always come from the same place and the multiplication reads true.
  const list = option !== null ? {
    input: option.input_per_million,
    output: option.output_per_million,
    cached: option.cached_per_million,
    cacheWrite: option.cache_write_per_million,
  } : {
    input: model.list_input_cost_per_million,
    output: model.list_output_cost_per_million,
    cached: model.list_cached_cost_per_million,
    cacheWrite: model.list_cache_write_cost_per_million,
  }
  const charged = {
    input: draft.inputPrice ? Number(draft.inputPrice) : null,
    output: draft.outputPrice ? Number(draft.outputPrice) : null,
    cached: draft.cacheReadPrice ? Number(draft.cacheReadPrice) : null,
    cacheWrite: draft.cacheWritePrice ? Number(draft.cacheWritePrice) : null,
  }
  const rows: Array<[string, number | null, number | null]> = [
    ["输入", list.input, charged.input],
    ["输出", list.output, charged.output],
    ["缓存读取", list.cached, charged.cached],
    ["缓存写入", list.cacheWrite, charged.cacheWrite],
  ]
  return <div className="model-price-arithmetic">
    <div className="registry-field-label-row">
      <span className="registry-field-label">实际单价</span>
      <span className="model-editor-unit" data-no-localize>USD / 1M Tokens</span>
    </div>
    <div className="model-price-grid">
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
    </div>
    {option !== null && <p className="publication-form-note">
      保存后按此基准计费；下次同步会沿用它。
    </p>}
    {option === null && model.price_synced_at && <p className="publication-form-note">
      <RefreshCw size={12} /> 最近同步 {new Date(model.price_synced_at).toLocaleString()}
      {model.price_sync_status && model.price_sync_status !== "ok"
        && ` · ${SYNC_STATUS_LABEL[model.price_sync_status] ?? model.price_sync_status}`}
      {model.price_sync_message && ` · ${model.price_sync_message}`}
    </p>}
    {list.cacheWrite === null && <p className="publication-form-note">
      该来源未单独发布缓存写入价，按缓存读取单价计费。
    </p>}
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
    {details.other_meters.length > 0 && <details className="model-editor-advanced">
      <summary>
        <span>计的是别的东西的计量表</span>
        <span data-no-localize>{details.other_meters.length}</span>
      </summary>
      <div className="model-editor-advanced-body">
        <p className="publication-form-note">
          批量调用、微调、预留吞吐等，不是普通对话调用的单价，因此不作为选项。
        </p>
        {details.other_meters.map((name) => (
          <code key={name} data-no-localize>{name}</code>
        ))}
      </div>
    </details>}
  </>
}
