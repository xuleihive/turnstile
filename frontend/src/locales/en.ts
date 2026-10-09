import { ENGLISH_CORE_PHRASES } from "./en/phrases-core"
import { ENGLISH_COPILOT_PHRASES } from "./en/phrases-copilot"
import { ENGLISH_AUTH_PHRASES } from "./en/phrases-auth"
import { USER_SETTINGS_PHRASES } from "./en/phrases-user-settings"
import { IMAGE_GENERATION_PHRASES } from "./en/phrases-image-generation"
import { DYNAMIC_RULES } from "./en/dynamic-rules"

const ENGLISH_PHRASES: Record<string, string> = {
  ...ENGLISH_CORE_PHRASES,
  ...ENGLISH_COPILOT_PHRASES,
  ...ENGLISH_AUTH_PHRASES,
  ...USER_SETTINGS_PHRASES,
  ...IMAGE_GENERATION_PHRASES,
}

const orderedPhrases = Object.entries(ENGLISH_PHRASES).sort(([left], [right]) => right.length - left.length)

export function translateToEnglish(input: string) {
  const whitespace = input.match(/^(\s*)([\s\S]*?)(\s*)$/)
  if (!whitespace) return input
  const [, before, content, after] = whitespace
  const exact = ENGLISH_PHRASES[content]
  if (exact != null) return `${before}${exact}${after}`

  let translated = content
  for (const [pattern, replacement] of DYNAMIC_RULES) translated = translated.replace(pattern, replacement)
  for (const [source, target] of orderedPhrases) translated = translated.replaceAll(source, target)
  translated = translated
    .replaceAll("（", " (")
    .replaceAll("）", ")")
    .replaceAll("，", ", ")
    .replaceAll("。", ".")
    .replaceAll("：", ": ")
    .replaceAll("、", ", ")
  return `${before}${translated}${after}`
}

const singularFragments: Record<string, string> = {
  connections: "connection",
  runtimes: "runtime",
  models: "model",
  departments: "department",
  projects: "project",
  Agents: "Agent",
  calls: "call",
  failures: "failure",
  errors: "error",
  items: "item",
  records: "record",
  turns: "turn",
  days: "day",
  people: "person",
}

const countableLabels: Record<string, string> = {
  Model: "Models",
  Provider: "Providers",
  Gateway: "Gateways",
  Runtime: "Runtimes",
}

function previousTextValue(text: Text) {
  let previous = text.previousSibling
  while (previous && !(previous.textContent ?? "").trim()) previous = previous.previousSibling
  return (previous?.textContent ?? "").trim()
}

export function correctEnglishPlurals(root: HTMLElement) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let current: Node | null
  while ((current = walker.nextNode())) {
    const text = current as Text
    const fragment = text.data.trim()
    const singular = singularFragments[fragment]
    const count = previousTextValue(text).replace(/,/g, "")
    if (singular && count === "1") text.data = text.data.replace(fragment, singular)
    const plural = countableLabels[fragment]
    if (plural && /^\d+$/.test(count) && count !== "1") text.data = text.data.replace(fragment, plural)
  }
}
