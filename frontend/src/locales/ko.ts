import { KOREAN_CORE_PHRASES } from "./ko/phrases-core"
import { USER_SETTINGS_PHRASES } from "./ko/phrases-user-settings"
import { KOREAN_COPILOT_PHRASES } from "./ko/phrases-copilot"
import { KOREAN_AUTH_PHRASES } from "./ko/phrases-auth"
import { IMAGE_GENERATION_PHRASES } from "./ko/phrases-image-generation"
import { ORGANIZATION_PHRASES } from "./ko/phrases-organization"
import { DYNAMIC_RULES } from "./ko/dynamic-rules"

const KOREAN_PHRASES: Record<string, string> = {
  ...KOREAN_CORE_PHRASES,
  ...USER_SETTINGS_PHRASES,
  ...KOREAN_COPILOT_PHRASES,
  ...KOREAN_AUTH_PHRASES,
  ...IMAGE_GENERATION_PHRASES,
  ...ORGANIZATION_PHRASES,
}

const orderedPhrases = Object.entries(KOREAN_PHRASES).sort(([left], [right]) => right.length - left.length)

export function translateToKorean(input: string): string {
  const whitespace = input.match(/^(\s*)([\s\S]*?)(\s*)$/)
  if (!whitespace) return input
  const [, before, content, after] = whitespace

  const exact = KOREAN_PHRASES[content]
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
