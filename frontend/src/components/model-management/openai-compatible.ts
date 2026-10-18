import type { ModelVendorKey } from "../../data-sources/apim/types"

export const MODEL_VENDOR_OPTIONS: ReadonlyArray<{ value: ModelVendorKey; label: string }> = [
  { value: "kimi", label: "Kimi" },
  { value: "deepseek", label: "DeepSeek" },
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "generic", label: "其他服务商" },
]

export function isOpenAICompatibleBaseUrl(value: string): boolean {
  try {
    const endpoint = new URL(value.trim())
    return endpoint.protocol === "https:"
      && Boolean(endpoint.hostname)
      && !endpoint.username
      && !endpoint.password
      && !endpoint.search
      && !endpoint.hash
  } catch {
    return false
  }
}

export function publicModelKeyFromProviderId(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9._:-]+/g, "-").replace(/^-+|-+$/g, "")
}

export function displayNameFromProviderId(value: string): string {
  const leaf = value.trim().split("/").at(-1) ?? ""
  const words = leaf.replace(/[-_]+/g, " ").trim()
  return words.replace(/(^|\s)([a-z])/g, (_match, space: string, letter: string) =>
    `${space}${letter.toUpperCase()}`)
    .replace(/^Deepseek\b/i, "DeepSeek")
    .replace(/^Openai\b/i, "OpenAI")
    .replace(/^Gpt\b/i, "GPT")
    .replace(/^Kimi\b/i, "Kimi")
}

export function modelVendorLabel(value: ModelVendorKey): string {
  return MODEL_VENDOR_OPTIONS.find((option) => option.value === value)?.label ?? "其他服务商"
}

export function modelVendorFromMetadata(
  config: Record<string, unknown> | undefined,
  identity = "",
): ModelVendorKey {
  const configured = MODEL_VENDOR_OPTIONS.find((option) => option.value === config?.model_vendor)
  if (configured) return configured.value
  const normalized = identity.toLowerCase().replaceAll("openai-compatible", "")
  if (normalized.includes("deepseek")) return "deepseek"
  if (normalized.includes("kimi") || normalized.includes("moonshot")) return "kimi"
  if (normalized.includes("anthropic") || normalized.includes("claude")) return "anthropic"
  if (normalized.includes("openai") || normalized.includes("gpt")) return "openai"
  return "generic"
}

export function isUnsupportedOpenAICompatibleDetails(value: unknown): boolean {
  if (!value || typeof value !== "object" || !("detail" in value) || !Array.isArray(value.detail)) {
    return false
  }
  const issues: unknown[] = value.detail
  const details = issues.filter((item): item is { type?: unknown; loc?: unknown; input?: unknown } =>
    item !== null && typeof item === "object")
  const providerRejected = details.some((issue) => issue.type === "literal_error"
    && issue.input === "openai_compatible"
    && Array.isArray(issue.loc)
    && issue.loc.at(-2) === "provider"
    && issue.loc.at(-1) === "template")
  const endpointRejected = details.some((issue) => issue.type === "extra_forbidden"
    && Array.isArray(issue.loc)
    && issue.loc.at(-1) === "openai_base_url")
  return providerRejected && endpointRejected
}
