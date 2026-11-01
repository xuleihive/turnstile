import { useEffect, useId, useState } from "react"
import { Download, Image as ImageIcon, Maximize2, RefreshCw } from "lucide-react"

import { Button } from "../../../components/ui/button"
import { Input } from "../../../components/ui/input"
import { generatedImageBlob, imageDownloadFilename } from "../image-generation"
import type { ImageGenerationOptions, ImageInvocationResponse } from "../types"
import { compact, formatLatency, PanelTitle } from "./dashboard-shared"
import "./image-generation.css"

// Microsoft Foundry's documented values; other provider values can still be typed.
const IMAGE_OPTION_FIELDS = [
  { key: "size", label: "图像尺寸", suggestions: ["1024x1024", "1024x1536", "1536x1024"] },
  { key: "quality", label: "图像质量", suggestions: ["low", "medium", "high"] },
  { key: "output_format", label: "文件格式", suggestions: ["png", "jpeg"] },
] as const

export function ImageOptions({ value, onChange, disabled }: {
  value: ImageGenerationOptions
  onChange: (value: ImageGenerationOptions) => void
  disabled: boolean
}) {
  const id = useId()
  return <div className="invoke-image-options">
    {IMAGE_OPTION_FIELDS.map(({ key, label, suggestions }) => <label key={key} className="registry-field invoke-select"><span>{label}</span>
      <Input list={`${id}-${key}`} value={value[key] ?? ""} disabled={disabled} data-no-localize onChange={event => onChange({ ...value, [key]: event.target.value || undefined })} />
      <datalist id={`${id}-${key}`} data-no-localize>{suggestions.map(option => <option key={option} value={option} />)}</datalist>
    </label>)}
  </div>
}

export function ImageGenerationResult({ result, pending, available }: {
  result: ImageInvocationResponse | null
  pending: boolean
  available: boolean
}) {
  const [source, setSource] = useState<string | null>(null)
  const [invalid, setInvalid] = useState(false)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    setInvalid(false)
    setLoaded(false)
    setSource(null)
    if (!result) return
    try {
      const url = URL.createObjectURL(generatedImageBlob(result))
      setSource(url)
      return () => URL.revokeObjectURL(url)
    } catch {
      setInvalid(true)
    }
  }, [result])

  const actionsUnavailable = !result || !source || !loaded || invalid || pending
  const download = () => {
    if (!result || !source || actionsUnavailable) return
    const anchor = document.createElement("a")
    anchor.href = source
    anchor.download = imageDownloadFilename(result)
    anchor.click()
  }

  const header = <PanelTitle title="调用结果" action={<>
    <Button variant="ghost" size="icon-sm" className="rounded-full" title="查看原图" aria-label="查看原图" disabled={actionsUnavailable} onClick={() => {
      if (source && !actionsUnavailable) window.open(source, "_blank", "noopener,noreferrer")
    }}><Maximize2 size={14} /></Button>
    <Button variant="ghost" size="icon-sm" className="rounded-full" title="下载图片" aria-label="下载图片" disabled={actionsUnavailable} onClick={download}><Download size={14} /></Button>
  </>} />

  if (!result) return <>{header}<div className="invoke-image-state" role="status" aria-busy={pending}>
    {pending ? <RefreshCw className="spin" size={24} /> : <ImageIcon size={28} />}
    <span>{pending ? "正在生成图片" : available ? "尚未生成图片" : "后端尚未启用图像生成"}</span>
  </div></>

  const [imageWidth, imageHeight] = result.size.split("x").map(Number)
  return <>{header}<div className="invoke-response invoke-image-response">
    <figure className="invoke-image-viewport" aria-busy={!loaded && !invalid}>
      {invalid ? <div className="invoke-error" role="alert">图片数据不可用</div> : source ? <img src={source} alt="生成的图像" onLoad={event => {
        if (event.currentTarget.naturalWidth !== imageWidth || event.currentTarget.naturalHeight !== imageHeight) setInvalid(true)
        else setLoaded(true)
      }} onError={() => setInvalid(true)} /> : <RefreshCw className="spin" size={24} />}
    </figure>
    <dl className="invoke-result-facts">
      <div className="invoke-image-identity"><dt>模型</dt><dd title={result.model}>{result.model}</dd></div>
      <div className="invoke-image-identity"><dt>运行时</dt><dd title={result.runtime}>{result.runtime}</dd></div>
      <div><dt>图像尺寸</dt><dd>{result.size}</dd></div>
      <div><dt>图像质量</dt><dd>{result.quality ?? "—"}</dd></div>
      <div><dt>文件格式</dt><dd>{result.output_format.toUpperCase()}</dd></div>
      <div><dt>延迟</dt><dd>{formatLatency(result.latency_ms)}</dd></div>
      <div><dt>文字输入 Token</dt><dd>{result.usage ? compact.format(result.usage.input_tokens + result.usage.cached_tokens) : "未测量"}</dd></div>
      <div><dt>缓存文字 Token</dt><dd>{result.usage ? compact.format(result.usage.cached_tokens) : "未测量"}</dd></div>
      <div><dt>图像输出 Token</dt><dd>{result.usage ? compact.format(result.usage.output_tokens) : "未测量"}</dd></div>
      <div><dt>估算成本</dt><dd>{result.estimated_cost == null ? "未计价" : `$${result.estimated_cost.toFixed(6)}`}</dd></div>
    </dl>
    <dl className="invoke-result-ids">
      <div><dt>Request ID</dt><dd>{result.request_id}</dd></div>
      <div><dt>Correlation ID</dt><dd>{result.correlation_id}</dd></div>
    </dl>
  </div></>
}