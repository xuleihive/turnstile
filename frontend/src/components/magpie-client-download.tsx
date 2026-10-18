import { useEffect, useState } from "react"
import { Download } from "lucide-react"
import { buttonVariants } from "./ui/button"
import { detectMagpieDownload, magpieDownload, MAGPIE_DOWNLOAD_PAGE } from "../lib/magpie-download"

export function MagpieClientDownload() {
  const [download, setDownload] = useState(() => magpieDownload(typeof navigator === "undefined" ? undefined : navigator))
  useEffect(() => {
    let active = true
    void detectMagpieDownload(navigator).then(value => { if (active) setDownload(value) })
    return () => { active = false }
  }, [])
  return <div className="client-downloads-client-actions">
    <a className={buttonVariants({ variant: "default", className: "client-downloads-client-button" })}
      href={download.href} target="_blank" rel="noopener noreferrer" title="从客户端官网下载；在新标签页打开">
      <Download size={16} aria-hidden="true" /><span>{download.label}</span>
    </a>
    <a className="client-downloads-other-versions" href={MAGPIE_DOWNLOAD_PAGE} target="_blank" rel="noopener noreferrer"
      title="在客户端官网选择系统和处理器版本；在新标签页打开">其他版本</a>
  </div>
}
