export const MAGPIE_DOWNLOAD_PAGE = "https://usemagpie.ai/zh/#get"

type BrowserInfo = {
  userAgent: string
  maxTouchPoints?: number
  userAgentData?: {
    platform?: string
    mobile?: boolean
    getHighEntropyValues?: (hints: string[]) => Promise<{ architecture?: string }>
  }
}

export type MagpieDownload = { href: string; label: string }

export function magpieDownload(browser?: BrowserInfo, architecture?: string): MagpieDownload {
  const fallback = { href: MAGPIE_DOWNLOAD_PAGE, label: "下载客户端" }
  if (!browser) return fallback
  const ua = browser.userAgent
  if (browser.userAgentData?.mobile || /Android|iPhone|iPad|iPod/i.test(ua)
    || (/Macintosh/i.test(ua) && (browser.maxTouchPoints ?? 0) > 1)) return fallback
  const platform = browser.userAgentData?.platform ?? ""
  const os = /Windows/i.test(platform || ua) ? "windows"
    : /macOS|Macintosh|Mac OS X/i.test(platform || ua) ? "mac"
    : /Linux|X11|CrOS/i.test(platform || ua) ? "linux" : null
  if (!os) return fallback
  const arm = architecture === "arm" || (!architecture && /aarch64|arm64|armv8/i.test(ua))
  // Apple Silicon browsers also report "Intel Mac"; never select an Intel build
  // from that UA or send an unknown Intel Mac to the default ARM-only installer.
  const name = os === "mac" ? "macOS" : os === "windows" ? "Windows" : "Linux"
  const label = `下载 ${name} 客户端`
  if (os === "mac" && architecture !== "arm" && architecture !== "x86") {
    return { href: MAGPIE_DOWNLOAD_PAGE, label }
  }
  const path = os === "mac" ? architecture === "x86" ? "download/mac-intel"
    : "download/mac-arm64"
    : `download/${os}-${arm ? "arm64" : "amd64"}`
  return { href: `https://usemagpie.ai/${path}`, label }
}

export async function detectMagpieDownload(browser?: BrowserInfo): Promise<MagpieDownload> {
  try {
    const hints = await browser?.userAgentData?.getHighEntropyValues?.(["architecture"])
    return magpieDownload(browser, hints?.architecture)
  } catch {
    return magpieDownload(browser)
  }
}
