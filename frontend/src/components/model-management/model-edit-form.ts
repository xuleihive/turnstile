import type { ManagedModel, PriceCatalogOption, PriceSource, PriceSyncResponse } from "../../data-sources/apim/types"

export function pricingSyncSummary(result: PriceSyncResponse): string {
  return `全部 ${result.total} 个模型 · 已更新 ${result.updated} · 无变化 ${result.unchanged} · 手动跳过 ${result.skipped_manual}`
}

export type ModelEditDraft = {
  displayName: string
  contextWindow: string
  contextOrigin?: "user" | "catalog"
  inputPrice: string
  outputPrice: string
  cacheReadPrice: string
  cacheWritePrice: string
  allowedRoles: string[]
  enabled: boolean
  isDefault: boolean
  // "manual" keeps the four fields above exactly as they have always behaved. The other sources
  // derive them from a published list price, so the fields become read-out rather than input.
  priceSource: PriceSource
  priceReference: string
  // Blank inherits the connection's discount. Kept as a string so an empty box stays empty
  // instead of becoming a zero.
  discountPercent: string
  priceEntryDigest: string
  allowUnpriced: boolean
}

export type ModelEditError =
  | "display_name"
  | "context_window"
  | "prices"
  | "default_disabled"
  | "price_reference"
  | "discount"

export function createModelEditDraft(model: ManagedModel): ModelEditDraft {
  return {
    displayName: model.display_name,
    contextWindow: model.context_window?.toString() ?? "",
    inputPrice: model.input_cost_per_million?.toString() ?? "",
    outputPrice: model.output_cost_per_million?.toString() ?? "",
    cacheReadPrice: model.cached_cost_per_million?.toString() ?? "",
    cacheWritePrice: model.cache_write_cost_per_million?.toString() ?? "",
    allowedRoles: [...model.allowed_roles],
    enabled: model.enabled,
    isDefault: model.is_default,
    priceSource: model.price_source_configured === false ? "models_dev" : model.price_source ?? "models_dev",
    priceReference: model.price_reference ?? "",
    discountPercent: model.price_discount_percent?.toString() ?? "",
    priceEntryDigest: "",
    allowUnpriced: false,
  }
}

export function modelEditRoleOptions(model: ManagedModel): string[] {
  return [...new Set(["owner", "member", ...model.allowed_roles])]
}

export function toggleModelRole(roles: string[], role: string, checked: boolean): string[] {
  return checked ? [...new Set([...roles, role])] : roles.filter((value) => value !== role)
}

export function setModelEditEnabled(draft: ModelEditDraft, enabled: boolean): ModelEditDraft {
  return { ...draft, enabled, isDefault: enabled && draft.isDefault }
}

export function setModelEditDefault(draft: ModelEditDraft, isDefault: boolean): ModelEditDraft {
  return { ...draft, isDefault, enabled: isDefault || draft.enabled }
}

/** Switching back to manual keeps the rates that were showing, so nothing silently blanks. */
export function setModelEditPriceSource(
  draft: ModelEditDraft,
  priceSource: PriceSource,
): ModelEditDraft {
  if (priceSource === "manual") return { ...draft, priceSource }
  return { ...draft, priceSource }
}

/** Picking a deployment fills the rates in so the dialog shows the arithmetic immediately. */
export function applyCatalogOption(
  draft: ModelEditDraft,
  source: PriceSource,
  option: PriceCatalogOption,
  effectiveDiscountPercent: number | null,
): ModelEditDraft {
  const rate = (value: number | null) =>
    value === null ? "" : discountedRate(value, effectiveDiscountPercent).toString()
  return {
    ...draft,
    priceSource: source,
    priceReference: option.reference,
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
  draft: Pick<ModelEditDraft, "discountPercent">,
  connectionPercent: number | null,
): { percent: number | null; inherited: boolean } {
  const own = draft.discountPercent.trim()
  if (own) return { percent: Number(own), inherited: false }
  return { percent: connectionPercent, inherited: true }
}

export function modelEditHasChanges(initial: ModelEditDraft, draft: ModelEditDraft): boolean {
  const comparable = (value: ModelEditDraft) => {
    const { contextOrigin: _contextOrigin, ...fields } = value
    return { ...fields, allowedRoles: [...value.allowedRoles].sort() }
  }
  return JSON.stringify(comparable(initial)) !== JSON.stringify(comparable(draft))
}

export function validateModelEdit(draft: ModelEditDraft): ModelEditError | null {
  const name = draft.displayName.trim()
  if (!name || name.length > 255) return "display_name"
  if (draft.contextWindow.trim()
    && (!Number.isSafeInteger(Number(draft.contextWindow)) || Number(draft.contextWindow) < 1)) {
    return "context_window"
  }
  const prices = [draft.inputPrice, draft.outputPrice, draft.cacheReadPrice, draft.cacheWritePrice]
  if (prices.some((value) => value.trim() && (!Number.isFinite(Number(value)) || Number(value) < 0))) {
    return "prices"
  }
  if (draft.priceSource !== "manual" && !draft.priceReference.trim()
    && !(draft.priceSource === "models_dev" && draft.allowUnpriced)) return "price_reference"
  const discount = draft.discountPercent.trim()
  if (discount && (!Number.isFinite(Number(discount)) || Number(discount) <= 0 || Number(discount) > 100)) {
    return "discount"
  }
  if (draft.isDefault && !draft.enabled) return "default_disabled"
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
    input_cost_per_million: optionalNumber(draft.inputPrice),
    output_cost_per_million: optionalNumber(draft.outputPrice),
    cached_cost_per_million: optionalNumber(draft.cacheReadPrice),
    cache_write_cost_per_million: optionalNumber(draft.cacheWritePrice),
    allowed_roles: [...draft.allowedRoles],
    enabled: draft.enabled,
    is_default: draft.isDefault,
    price_source: draft.priceSource,
    price_reference: draft.priceSource === "manual" ? null : draft.priceReference.trim(),
    price_discount_percent: optionalNumber(draft.discountPercent),
    ...(draft.priceSource === "models_dev" ? {
      price_entry_digest: draft.priceEntryDigest || null,
      allow_unpriced: draft.allowUnpriced,
    } : {}),
  }
}
