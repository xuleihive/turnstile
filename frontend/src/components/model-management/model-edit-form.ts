import contextRules from "../../../../turnstile_core/pricing/context-rules.json" with { type: "json" }
import type { ContextPriceRule, ManagedModel, PriceCatalogContextTier, PriceCatalogModel, PriceCatalogOption, PriceCatalogOptionsResponse, PriceSource, PriceSyncResponse } from "../../data-sources/apim/types"

export function pricingSyncSummary(result: PriceSyncResponse): string {
  return `全部 ${result.total} 个模型 · 已更新 ${result.updated} · 无变化 ${result.unchanged} · 手动跳过 ${result.skipped_manual}`
}

export function resolveContextPriceRule(
  model: PriceCatalogModel,
  descriptor: PriceCatalogContextTier,
  rules = contextRules.rules,
): PriceCatalogContextTier {
  const matching = rules.filter(rule => rule.basis === "request_input_tokens" && rule.matches.some(match =>
    match.source === model.source && match.product === model.product
    && match.group_key === descriptor.group_key && match.service_tier === descriptor.service_tier))
  if (matching.length !== 1) return descriptor
  const rule = matching[0]
  if (descriptor.input_range !== null) return descriptor.input_range_source?.rule_id === rule.id
    && rule.origin === "reference" ? {
      ...descriptor, input_range_source: { ...descriptor.input_range_source, origin: "reference" },
    } : descriptor
  const tier = rule.tiers.find(candidate => candidate.key === descriptor.key)
  return tier ? {
    ...descriptor,
    input_range: tier.input_range,
    input_range_source: { rule_id: rule.id, url: rule.source_url, revision: rule.source_revision,
      verified_on: rule.verified_on, origin: rule.origin === "official" ? "official" : "reference", note: rule.note },
  } : descriptor
}

type ModelContextTier = {
  key: string
  label: string
  order: number
  inputRange: PriceCatalogContextTier["input_range"]
  inputRangeSource: PriceCatalogContextTier["input_range_source"]
  model: PriceCatalogModel
}

export type ModelContextPriceTier = Omit<ModelContextTier, "model"> & { option: PriceCatalogOption }

export type ModelPriceCandidate = PriceCatalogModel & {
  contextPricing?: {
    tiers: ModelContextTier[]
  }
}

export type ModelPriceOption = PriceCatalogOption & {
  contextPricing?: {
    tiers: ModelContextPriceTier[]
    regionalReferences?: Record<string, string[]>
  }
}

export type ModelPriceOptions = Omit<PriceCatalogOptionsResponse, "model_entry" | "options"> & {
  model_entry: ModelPriceCandidate
  options: ModelPriceOption[]
}

function contextTierFromModel(model: PriceCatalogModel): PriceCatalogContextTier | null {
  if (model.context_tier) return model.context_tier
  const markers = [...model.label.matchAll(/\b(short|long)(?:co\b|[\s_-]+context\b)/gi)]
  if (markers.length !== 1) return null
  const marker = markers[0]
  const label = `${model.label.slice(0, marker.index)} ${model.label.slice(marker.index + marker[0].length)}`
    .replace(/\(\s*\)/g, "").replace(/\s+/g, " ").trim()
  if (!label) return null
  const short = marker[1].toLowerCase() === "short"
  return {
    group_key: label.toLowerCase(), model_label: label, service_tier: "",
    key: short ? "short" : "long", label: short ? "短上下文" : "长上下文",
    order: short ? 0 : 1, input_range: null,
  }
}

export function groupContextPriceModels(models: PriceCatalogModel[]): ModelPriceCandidate[] {
  const result: ModelPriceCandidate[] = []
  const groups = new Map<string, ModelPriceCandidate>()
  for (const model of models) {
    const discovered = contextTierFromModel(model)
    if (!discovered) {
      result.push(model)
      continue
    }
    const descriptor = resolveContextPriceRule(model, discovered)
    const groupKey = JSON.stringify([model.source, model.product, descriptor.group_key, descriptor.service_tier])
    let grouped = groups.get(groupKey)
    if (!grouped) {
      grouped = {
        ...model, label: descriptor.service_tier ? `${descriptor.model_label} · ${descriptor.service_tier}` : descriptor.model_label,
        contextPricing: { tiers: [] },
      }
      groups.set(groupKey, grouped)
      result.push(grouped)
    }
    const tiers = grouped.contextPricing!.tiers
    if (tiers.some(tier => tier.model.key === model.key)) continue
    tiers.push({ key: descriptor.key, label: descriptor.label, order: descriptor.order,
      inputRange: descriptor.input_range, inputRangeSource: descriptor.input_range_source, model })
    tiers.sort((left, right) => left.order - right.order || left.key.localeCompare(right.key))
    grouped.key = tiers[0].model.key
  }
  return result
}

export function pairContextPriceOptions(
  model: ModelPriceCandidate,
  responses: PriceCatalogOptionsResponse[],
): ModelPriceOptions {
  const candidates = model.contextPricing?.tiers
  if (!candidates || candidates.length < 2 || responses.length !== candidates.length
    || new Set(candidates.map(tier => tier.key)).size !== candidates.length
    || candidates.some((tier, index) => tier.model.key !== responses[index].model_entry.key
      || tier.model.source !== responses[index].model_entry.source
      || tier.model.product !== responses[index].model_entry.product)) {
    throw new Error("分档价目与所选模型不匹配。")
  }
  const tiers = candidates.map((tier, index) => {
    const details = responses[index].model_entry
    const metadata = contextTierFromModel(details)
    if (!metadata) return tier
    const grouped = contextTierFromModel(tier.model)
    if (!grouped || metadata.group_key !== grouped.group_key || metadata.service_tier !== grouped.service_tier
      || metadata.key !== tier.key || metadata.order !== tier.order) {
      throw new Error("分档价目与所选模型不匹配。")
    }
    const resolved = resolveContextPriceRule(details, metadata)
    return resolved.input_range === null ? tier : {
      ...tier, inputRange: resolved.input_range, inputRangeSource: resolved.input_range_source,
    }
  })
  const ranges = tiers.map(tier => tier.inputRange)
  if (ranges.some(range => range !== null && (!Number.isSafeInteger(range.min_input_tokens) || range.min_input_tokens < 0
    || (range.max_input_tokens !== null && (!Number.isSafeInteger(range.max_input_tokens)
      || range.max_input_tokens < range.min_input_tokens))))) {
    throw new Error("上下文计价区间无效。")
  }
  if (ranges.every(range => range !== null) && (ranges[0]!.min_input_tokens !== 0
    || ranges.at(-1)!.max_input_tokens !== null
    || ranges.slice(1).some((range, index) => ranges[index]!.max_input_tokens === null
      || range!.min_input_tokens !== ranges[index]!.max_input_tokens! + 1))) {
    throw new Error("上下文计价区间无效。")
  }
  const complete = responses.every(response => response.complete === true)
  const catalogs = complete ? responses.map(response => {
    const options = new Map<string, PriceCatalogOption>()
    for (const option of response.options) {
      if ([option.input_per_million, option.output_per_million]
        .some(rate => rate === null || !Number.isFinite(rate) || rate < 0)
        || [option.cached_per_million, option.cache_write_per_million]
          .some(rate => rate !== null && (!Number.isFinite(rate) || rate < 0))) continue
      for (const region of option.regions) {
        const key = JSON.stringify([option.deployment, region])
        const reference = option.references_by_region?.[region] ?? option.reference
        const regional = { ...option, reference }
        const previous = options.get(key)
        if (previous && (previous.reference !== reference
          || previous.input_per_million !== option.input_per_million
          || previous.output_per_million !== option.output_per_million
          || previous.cached_per_million !== option.cached_per_million
          || previous.cache_write_per_million !== option.cache_write_per_million)) {
          throw new Error("同一区域存在冲突的计价档位。")
        }
        options.set(key, regional)
      }
    }
    return options
  }) : []
  const groups = new Map<string, { deployment: string; tiers: ModelContextPriceTier[]; references: Record<string, string>; regionalReferences: Record<string, string[]> }>()
  for (const [key, first] of catalogs[0] ?? []) {
    const options = catalogs.map(catalog => catalog.get(key))
    if (options.some(option => !option)) continue
    const [, region] = JSON.parse(key) as [string, string]
    const signature = JSON.stringify([first.deployment, options.map(option => [
      option!.input_per_million, option!.output_per_million, option!.cached_per_million, option!.cache_write_per_million,
    ])])
    let group = groups.get(signature)
    if (!group) {
      group = {
        deployment: first.deployment,
        tiers: tiers.map((tier, index) => ({ key: tier.key, label: tier.label, order: tier.order,
          inputRange: tier.inputRange, inputRangeSource: tier.inputRangeSource, option: options[index]! })),
        references: {}, regionalReferences: {},
      }
      groups.set(signature, group)
    }
    group.references[region] = JSON.stringify([options.map(option => option!.reference), region])
    group.regionalReferences[region] = options.map(option => option!.reference)
  }
  const paired = [...groups.values()]
  return {
    model_entry: model,
    complete,
    options: paired.map(group => {
      const regions = Object.keys(group.references).sort()
      const regionRequired = paired.filter(candidate => candidate.deployment === group.deployment).length > 1
      return {
        ...group.tiers[0].option,
        reference: group.references[regions[0]],
        regions,
        region_required: regionRequired,
        references_by_region: group.references,
        contextPricing: { tiers: group.tiers, regionalReferences: group.regionalReferences },
      }
    }),
    unreadable: [...new Set(responses.flatMap(response => response.unreadable))],
    other_meters: [...new Set(responses.flatMap(response => response.other_meters))],
    note: responses.map(response => response.note).filter(Boolean).join("\n") || null,
  }
}

export type ModelPriceDraft = {
  contextWindow?: string
  contextOrigin?: "user" | "catalog"
  priceEntryDigest: string
  allowUnpriced: boolean
  inputPrice: string
  outputPrice: string
  cacheReadPrice: string
  cacheWritePrice: string
  // "manual" keeps the four fields above exactly as they have always behaved. The other sources
  // derive them from a published list price, so the fields become read-out rather than input.
  priceSource: PriceSource
  priceReference: string
  // Blank inherits the connection's discount. Kept as a string so an empty box stays empty
  // instead of becoming a zero.
  discountPercent: string
  contextPricingPreview?: boolean
  contextPricingSupported?: boolean
  contextPriceReferences?: string[]
  contextPriceTiers?: ModelContextPriceTier[]
  contextPriceRefresh?: boolean
  contextPriceRulesSupported?: boolean
  contextRuleMode?: "catalog" | "custom"
  contextThreshold?: string
  contextRuleNote?: string
  contextRuleUrl?: string
}

export type ModelEditDraft = ModelPriceDraft & {
  displayName: string
  contextWindow: string
  contextOrigin?: "user" | "catalog"
  allowedRoles: string[]
  isDefault: boolean
}

export type ModelEditError =
  | "display_name"
  | "context_window"
  | "prices"
  | "price_reference"
  | "discount"
  | "context_pricing_preview"
  | "context_pricing_incomplete"
  | "context_price_rule"
  | "context_price_rule_preview"

export function createModelPriceDraft(contextPricingSupported = false, contextPriceRulesSupported = false): ModelPriceDraft {
  return {
    inputPrice: "", outputPrice: "", cacheReadPrice: "", cacheWritePrice: "",
    priceSource: "models_dev", priceReference: "", discountPercent: "",
    priceEntryDigest: "", allowUnpriced: false,
    contextPricingPreview: false,
    contextPricingSupported, contextPriceReferences: [], contextPriceTiers: [], contextPriceRefresh: false,
    contextPriceRulesSupported, contextRuleMode: "catalog", contextThreshold: "", contextRuleNote: "", contextRuleUrl: "",
  }
}

export function createModelEditDraft(model: ManagedModel, contextPricingSupported = false, contextPriceRulesSupported = false): ModelEditDraft {
  const restored = restoreContextPrices(model)
  return {
    displayName: model.display_name,
    contextWindow: model.context_window?.toString() ?? "",
    inputPrice: model.input_cost_per_million?.toString() ?? "",
    outputPrice: model.output_cost_per_million?.toString() ?? "",
    cacheReadPrice: model.cached_cost_per_million?.toString() ?? "",
    cacheWritePrice: model.cache_write_cost_per_million?.toString() ?? "",
    allowedRoles: [...model.allowed_roles],
    isDefault: model.is_default,
    priceSource: model.price_source_configured === false ? "models_dev" : model.price_source ?? "models_dev",
    priceEntryDigest: "", allowUnpriced: false,
    priceReference: model.price_reference ?? "",
    discountPercent: model.price_discount_percent?.toString() ?? "",
    contextPricingPreview: !!restored,
    contextPricingSupported,
    contextPriceReferences: model.context_price_tiers?.map(tier => tier.reference) ?? [],
    contextPriceTiers: restored?.options[0].contextPricing?.tiers ?? [],
    contextPriceRefresh: false,
    contextPriceRulesSupported,
    contextRuleMode: model.context_price_rule ? "custom" : "catalog",
    contextThreshold: model.context_price_rule?.short_context_max_tokens.toString() ?? "",
    contextRuleNote: model.context_price_rule?.note ?? "",
    contextRuleUrl: model.context_price_rule?.source_url ?? "",
  }
}

export function resetContextPriceRule<Draft extends ModelPriceDraft>(draft: Draft): Draft {
  return { ...draft, contextRuleMode: "catalog", contextThreshold: "", contextRuleNote: "", contextRuleUrl: "" }
}

export function contextPriceRulePayload(draft: ModelPriceDraft): ContextPriceRule | undefined {
  return draft.contextPricingPreview && draft.contextRuleMode === "custom" ? {
    short_context_max_tokens: Number(draft.contextThreshold),
    note: draft.contextRuleNote?.trim() ?? "",
    source_url: draft.contextRuleUrl?.trim() || null,
  } : undefined
}

export function validContextPriceRule(draft: ModelPriceDraft): boolean {
  const rule = contextPriceRulePayload(draft)
  if (!rule || !draft.contextThreshold?.trim() || !Number.isSafeInteger(rule.short_context_max_tokens)
    || rule.short_context_max_tokens <= 0 || rule.short_context_max_tokens >= Number.MAX_SAFE_INTEGER
    || (rule.note?.length ?? 0) > 500) return false
  if (rule.source_url) {
    try {
      const url = new URL(rule.source_url)
      if (url.protocol !== "https:" || !url.hostname || url.username || url.password
        || /\s/.test(rule.source_url) || rule.source_url.length > 2000) return false
    } catch { return false }
  }
  return true
}

export function effectiveContextPriceTiers(draft: ModelPriceDraft): ModelContextPriceTier[] {
  const tiers = draft.contextPriceTiers ?? []
  if (draft.contextRuleMode !== "custom") return tiers
  const maximum = Number(draft.contextThreshold)
  const valid = !!draft.contextThreshold?.trim() && Number.isSafeInteger(maximum)
    && maximum > 0 && maximum < Number.MAX_SAFE_INTEGER
  return tiers.map(tier => ({
    ...tier, inputRangeSource: undefined,
    inputRange: valid && ["short", "long"].includes(tier.key) ? {
      min_input_tokens: tier.key === "short" ? 0 : maximum + 1,
      max_input_tokens: tier.key === "short" ? maximum : null,
    } : null,
  }))
}

export function modelEditRoleOptions(model: ManagedModel): string[] {
  return [...new Set(["owner", "member", ...model.allowed_roles])]
}

export function toggleModelRole(roles: string[], role: string, checked: boolean): string[] {
  return checked ? [...new Set([...roles, role])] : roles.filter((value) => value !== role)
}

export function setModelEditDefault(draft: ModelEditDraft, isDefault: boolean): ModelEditDraft {
  return { ...draft, isDefault }
}

/** Switching back to manual keeps the rates that were showing, so nothing silently blanks. */
export function setModelEditPriceSource(
  draft: ModelEditDraft,
  priceSource: PriceSource,
): ModelEditDraft {
  if (priceSource === "manual") return resetContextPriceRule({
    ...draft, priceSource, contextPricingPreview: false, contextPriceReferences: [],
    contextPriceTiers: [], contextPriceRefresh: false,
  })
  return { ...draft, priceSource }
}

/** Picking a deployment fills the rates in so the dialog shows the arithmetic immediately. */
export function applyCatalogOption<Draft extends ModelPriceDraft>(
  draft: Draft,
  source: PriceSource,
  option: ModelPriceOption,
  effectiveDiscountPercent: number | null,
): Draft {
  if (option.contextPricing) {
    const region = option.regions.find(candidate => option.references_by_region?.[candidate] === option.reference)
    const references = region ? option.contextPricing.regionalReferences?.[region] : undefined
    const selectedReferences = references ?? option.contextPricing.tiers.map(tier => tier.option.reference)
    const sameScope = draft.priceSource === source
      && JSON.stringify(draft.contextPriceReferences) === JSON.stringify(selectedReferences)
    return {
      ...(sameScope ? draft : resetContextPriceRule(draft)), priceSource: source, priceReference: "", contextPricingPreview: true,
      contextPriceReferences: selectedReferences,
      contextPriceTiers: option.contextPricing.tiers,
      contextPriceRefresh: true,
    }
  }
  const rate = (value: number | null) =>
    value === null ? "" : discountedRate(value, effectiveDiscountPercent).toString()
  return {
    ...resetContextPriceRule(draft),
    priceSource: source,
    priceReference: option.reference,
    contextPricingPreview: false,
    contextPriceReferences: [], contextPriceTiers: [], contextPriceRefresh: false,
    inputPrice: rate(option.input_per_million),
    outputPrice: rate(option.output_per_million),
    cacheReadPrice: rate(option.cached_per_million),
    cacheWritePrice: rate(option.cache_write_per_million),
  }
}

/**
 * The model key a stored reference came from, so reopening the dialog can show the chosen model
 * rather than an empty picker. References read
 * `<source>:<product>:<model>:<deployment>:<region-or-*>`; the first three parts are the key.
 */
export function modelKeyFromReference(reference: string): string | null {
  const parts = reference.split(":")
  if (parts[0] === "models_dev" && parts.length === 3) return reference
  return parts.length === 5 ? parts.slice(0, 3).join(":") : null
}

export function discountedRate(listPrice: number, percent: number | null): number {
  if (percent === null || percent === 100) return listPrice
  return Number((listPrice * (percent / 100)).toFixed(8))
}

/**
 * Which discount actually applies, given what the model overrides and what the connection sets.
 * Returned to the caller rather than resolved deep inside a component so the dialog can say
 * which of the two it used.
 */
export function resolveDiscount(
  draft: ModelPriceDraft,
  connectionPercent: number | null,
): { percent: number | null; inherited: boolean } {
  const own = draft.discountPercent.trim()
  if (own) return { percent: Number(own), inherited: false }
  return { percent: connectionPercent, inherited: true }
}

export function modelEditHasChanges(initial: ModelEditDraft, draft: ModelEditDraft): boolean {
  const comparable = (value: ModelEditDraft) => ({
    ...value, contextOrigin: undefined,
    contextPriceTiers: undefined, contextPricingSupported: undefined, contextPriceRulesSupported: undefined,
    allowedRoles: [...value.allowedRoles].sort(),
  })
  return JSON.stringify(comparable(initial)) !== JSON.stringify(comparable(draft))
}

export function validateModelEdit(draft: ModelEditDraft): ModelEditError | null {
  const name = draft.displayName.trim()
  if (!name || name.length > 255) return "display_name"
  if (draft.contextWindow.trim()
    && (!Number.isSafeInteger(Number(draft.contextWindow)) || Number(draft.contextWindow) < 1)) {
    return "context_window"
  }
  const priceError = validateModelPricing(draft)
  if (priceError) return priceError
  return null
}

export function validateModelPricing(draft: ModelPriceDraft): Exclude<ModelEditError, "display_name" | "context_window"> | null {
  const prices = [draft.inputPrice, draft.outputPrice, draft.cacheReadPrice, draft.cacheWritePrice]
  if (prices.some((value) => value.trim() && (!Number.isFinite(Number(value)) || Number(value) < 0))) {
    return "prices"
  }
  if (draft.priceSource !== "manual" && draft.contextPricingPreview) {
    if (!draft.contextPricingSupported) return "context_pricing_preview"
    if (draft.contextRuleMode === "custom") {
      if (!validContextPriceRule(draft)) return "context_price_rule"
      if (!draft.contextPriceRulesSupported) return "context_price_rule_preview"
      const keys = draft.contextPriceTiers?.map(tier => tier.key)
      if (keys?.length !== 2 || !keys.includes("short") || !keys.includes("long")) return "context_pricing_incomplete"
    }
    const tiers = effectiveContextPriceTiers(draft)
    const references = draft.contextPriceReferences ?? []
    if (tiers.length < 2 || references.length !== tiers.length || new Set(references).size !== references.length
      || references.some(reference => !reference.startsWith(`${draft.priceSource}:`))) return "context_pricing_incomplete"
    let minimum = 0
    for (const [index, tier] of tiers.entries()) {
      if (tier.inputRangeSource?.origin === "reference" && draft.contextPriceRefresh !== false) return "context_pricing_incomplete"
      const range = tier.inputRange
      if (!range || !Number.isSafeInteger(range.min_input_tokens) || range.min_input_tokens !== minimum
        || (range.max_input_tokens !== null && (!Number.isSafeInteger(range.max_input_tokens)
          || range.max_input_tokens < minimum))) return "context_pricing_incomplete"
      if ((range.max_input_tokens === null) !== (index === tiers.length - 1)) return "context_pricing_incomplete"
      minimum = range.max_input_tokens === null ? minimum : range.max_input_tokens + 1
    }
  } else if (draft.priceSource !== "manual" && !(draft.priceSource === "models_dev" && draft.allowUnpriced)
    && !draft.priceReference.trim().startsWith(`${draft.priceSource}:`)) return "price_reference"
  const discount = draft.discountPercent.trim()
  if (discount && (!Number.isFinite(Number(discount)) || Number(discount) <= 0 || Number(discount) > 100)) {
    return "discount"
  }
  return null
}

function optionalNumber(value: string): number | null {
  return value.trim() ? Number(value) : null
}

export function modelEditPayload(model: ManagedModel, draft: ModelEditDraft) {
  const error = validateModelEdit(draft)
  if (error) throw new Error(error)
  return {
    provider_id: model.provider_id,
    runtime_id: model.runtime_id,
    model_key: model.model_key,
    upstream_model_id: model.upstream_model_id,
    family_key: model.family_key,
    assignment_required: model.assignment_required,
    capabilities: [...model.capabilities],
    display_name: draft.displayName.trim(),
    context_window: optionalNumber(draft.contextWindow),
    input_cost_per_million: draft.contextPricingPreview ? null : optionalNumber(draft.inputPrice),
    output_cost_per_million: draft.contextPricingPreview ? null : optionalNumber(draft.outputPrice),
    cached_cost_per_million: draft.contextPricingPreview ? null : optionalNumber(draft.cacheReadPrice),
    cache_write_cost_per_million: draft.contextPricingPreview ? null : optionalNumber(draft.cacheWritePrice),
    allowed_roles: [...draft.allowedRoles],
    is_default: draft.isDefault,
    price_source: draft.priceSource,
    price_reference: draft.priceSource === "manual" || draft.contextPricingPreview ? null : draft.priceReference.trim(),
    price_discount_percent: optionalNumber(draft.discountPercent),
    ...(draft.priceSource === "models_dev" ? {
      price_entry_digest: draft.priceEntryDigest || null,
      allow_unpriced: draft.allowUnpriced,
    } : {}),
    ...(draft.contextPricingPreview ? {
      context_price_references: draft.contextPriceReferences,
      context_price_refresh: draft.contextPriceRefresh === true,
      ...(contextPriceRulePayload(draft) ? { context_price_rule: contextPriceRulePayload(draft) } : {}),
    } : {}),
  }
}

export function restoreContextPrices(model: ManagedModel): ModelPriceOptions | null {
  const saved = model.context_price_tiers
  if (!saved?.length) return null
  const responses: PriceCatalogOptionsResponse[] = saved.map((tier, order) => {
    const parts = tier.reference.split(":")
    const modelEntry: PriceCatalogModel = {
      key: tier.model_key || parts.slice(0, 3).join(":"), label: tier.model_label || parts[2],
      product: tier.product || parts[1], source: model.price_source,
      context_tier: { group_key: tier.group_key, model_label: tier.group_key, service_tier: tier.service_tier,
        key: tier.key, label: tier.label, order,
        input_range: { min_input_tokens: tier.min_input_tokens, max_input_tokens: tier.max_input_tokens },
        input_range_source: tier.input_range_source },
    }
    const regions = tier.regions.length ? tier.regions : [parts[4]]
    return {
      model_entry: modelEntry, complete: true, unreadable: [], other_meters: [], note: null,
      options: [{ reference: tier.reference, deployment: tier.deployment || parts[3], regions,
        region_required: parts[4] !== "*", references_by_region: Object.fromEntries(regions.map(region => [region, tier.reference])),
        input_per_million: tier.list_prices.input, output_per_million: tier.list_prices.output,
        cached_per_million: tier.list_prices.cached, cache_write_per_million: tier.list_prices.cache_write }],
    }
  })
  const [grouped] = groupContextPriceModels(responses.map(response => response.model_entry))
  return pairContextPriceOptions(grouped, responses)
}

export function toggleModelSelection<T extends string>(values: T[], value: T, checked: boolean): T[] {
  return checked ? values.includes(value) ? values : [...values, value] : values.filter((item) => item !== value)
}
