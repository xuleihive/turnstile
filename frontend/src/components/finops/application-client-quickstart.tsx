import { useEffect, useState } from "react"
import { Check, Code2, Copy, WrapText } from "lucide-react"
import hljs from "highlight.js/lib/core"
import bash from "highlight.js/lib/languages/bash"
import javascript from "highlight.js/lib/languages/javascript"
import python from "highlight.js/lib/languages/python"

import type {
  GatewayApplicationDetail,
  GatewayProfile,
  GatewayPublishedModelSummary,
  ManagedModel,
  ModelRegistry,
} from "../../data-sources/apim/types"
import {
  clientSampleFor,
  gatewayApiOperations,
  modelSampleOperations,
  operationSampleModels,
  type ClientSampleLanguage,
  type ClientSampleOperation,
} from "../../lib/application-client-samples"
import { Button } from "../ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select"

hljs.registerLanguage("bash", bash)
hljs.registerLanguage("javascript", javascript)
hljs.registerLanguage("python", python)

const languageOptions: Array<{ id: ClientSampleLanguage; label: string }> = [
  { id: "bash", label: "cURL" },
  { id: "python", label: "Python" },
  { id: "javascript", label: "JavaScript" },
]

export function ClientSampleCode({
  endpoint,
  sampleForLanguage,
}: {
  endpoint: string
  sampleForLanguage: (language: ClientSampleLanguage) => string
}) {
  const [language, setLanguage] = useState<ClientSampleLanguage>("bash")
  const [copyState, setCopyState] = useState<{ sample: string; error: boolean } | null>(null)
  const [wrapCode, setWrapCode] = useState(false)
  const sample = sampleForLanguage(language)
  const copied = copyState?.sample === sample && !copyState.error
  const copyError = copyState?.sample === sample && copyState.error
  const highlightedSample = sample ? hljs.highlight(sample, { language }).value : ""

  useEffect(() => {
    if (!copyState || copyState.error) return
    const timer = window.setTimeout(() => setCopyState(null), 2_000)
    return () => window.clearTimeout(timer)
  }, [copyState])

  const copySample = async () => {
    try {
      await navigator.clipboard.writeText(sample)
      setCopyState({ sample, error: false })
    } catch {
      setCopyState({ sample, error: true })
    }
  }

  return <>
    <div className="application-quickstart-endpoint"><span>API Base URL</span><code title={endpoint}>{endpoint}</code></div>
    <div className="application-quickstart-code-toolbar">
      <div className="application-quickstart-languages" role="group" aria-label="代码语言">
        {languageOptions.map((option) => <button type="button" key={option.id} className={language === option.id ? "active" : ""} aria-pressed={language === option.id} onClick={() => { setLanguage(option.id); setCopyState(null) }}>{option.label}</button>)}
      </div>
    </div>
    {sample ? <div className="application-quickstart-code-block">
      <pre className="application-quickstart-code" data-wrap={wrapCode}><code className={`hljs language-${language}`} dangerouslySetInnerHTML={{ __html: highlightedSample }} /></pre>
      <div className="application-quickstart-code-actions">
        <Button className="application-quickstart-wrap" type="button" variant="ghost" size="icon-sm" aria-label="自动换行" aria-pressed={wrapCode} title={wrapCode ? "取消自动换行" : "自动换行"} onClick={() => setWrapCode(!wrapCode)}><WrapText size={14} /></Button>
        <Button className="application-quickstart-copy" type="button" variant="ghost" size="icon-sm" aria-label="复制代码" title={copied ? "代码已复制" : "复制代码"} onClick={() => void copySample()}>{copied ? <Check size={14} /> : <Copy size={14} />}</Button>
      </div>
    </div> : <div className="application-quickstart-code-empty">选择接口后显示对应调用示例</div>}
    <footer className="application-quickstart-footer">
      <span>使用安全的环境变量 TURNSTILE_APIM_KEY 提供订阅密钥；请勿将密钥写入示例代码。</span>
    </footer>
    {copied && <span className="application-quickstart-status" role="status"><Check size={13} />代码已复制</span>}
    {copyError && <span className="application-quickstart-status error" role="alert">无法访问剪贴板，请手动复制。</span>}
  </>
}

export function ApplicationClientQuickstart({
  application,
  registry,
  models,
}: {
  application: GatewayApplicationDetail
  registry: ModelRegistry | undefined
  models: ManagedModel[]
}) {
  const target = registry?.apim_target_topology?.items.find((item) => (
    item.target.gateway_profile_id === application.gateway_profile_id
  ))
  const gateway: GatewayProfile | undefined = registry?.gateways.find((item) => (
    item.id === application.gateway_profile_id
  ))
  const endpoint = target?.gateway_endpoint ?? gateway?.base_url ?? null
  const publishedModels: GatewayPublishedModelSummary[] = target?.published_models ?? []
  const publishedModelsAvailable = target?.published_models_available === true
  const eligibleModels = publishedModels.flatMap((published) => {
    const model = models.find((candidate) => candidate.id === published.id)
    if (!model?.enabled) return []
    if (application.model_policy_configured && !application.allowed_model_ids.includes(model.id)) return []
    return [model]
  })
  const [selectedModelId, setSelectedModelId] = useState(eligibleModels[0]?.id ?? "")
  const [selectedOperation, setSelectedOperation] = useState<ClientSampleOperation>("chat_completions")
  const sampleModels = (operation: ClientSampleOperation) => operationSampleModels(eligibleModels, operation)
    .filter(model => operation === "models" || operation === "decision" || operation === "image_generations"
      || modelSampleOperations(model, registry?.runtimes.find(runtime => runtime.id === model.runtime_id))
        .operations.some(item => item.id === operation))
  const operations = gatewayApiOperations().filter(operation => operation.id === "models"
    || (operation.id !== "decision" || registry?.decision_supported === true) && sampleModels(operation.id).length > 0)
  const operation = operations.find(item => item.id === selectedOperation)?.id
    ?? operations.find(item => item.id !== "models")?.id ?? "models"
  const operationModels = sampleModels(operation)
  const selectedModel = operationModels.find(model => model.id === selectedModelId) ?? operationModels[0]
  const modelOptions = operationModels.map(model => ({ value: model.id, label: `${model.display_name} · ${model.model_key}` }))
  const baseUrl = endpoint?.replace(/\/+$/, "") ?? ""
  const hasActiveSubscription = application.subscriptions.some((subscription) => (
    subscription.state === "active" && subscription.scope_exists
  ))
  const ready = application.status === "active"
    && hasActiveSubscription
    && gateway?.enabled === true
    && Boolean(baseUrl)
    && publishedModelsAvailable
    && eligibleModels.length > 0

  return <section className="application-card application-quickstart-card">
    <header className="application-card-head"><div><Code2 size={14} /><h2>客户端接入</h2></div><span>示例代码</span></header>
    {ready ? <>
      <div className="application-quickstart-controls">
        <label><span>模型</span><Select value={selectedModel?.id ?? null} items={modelOptions} onValueChange={(value) => { if (value) setSelectedModelId(value) }}>
          <SelectTrigger className="application-quickstart-model-select" aria-label="示例模型"><SelectValue /></SelectTrigger>
          <SelectContent>{modelOptions.map((model) => <SelectItem key={model.value} value={model.value}>{model.label}</SelectItem>)}</SelectContent>
        </Select></label>
        <label><span>接口</span><Select value={operation} items={operations.map((operation) => ({ value: operation.id, label: operation.label }))} onValueChange={(value) => { if (value) setSelectedOperation(value) }}>
          <SelectTrigger className="application-quickstart-model-select" aria-label="示例接口"><SelectValue /></SelectTrigger>
          <SelectContent>{operations.map((operation) => <SelectItem key={operation.id} value={operation.id}>{operation.label} · {operation.path}</SelectItem>)}</SelectContent>
        </Select></label>
      </div>
      <ClientSampleCode endpoint={baseUrl} sampleForLanguage={language => selectedModel ? clientSampleFor(language, baseUrl, selectedModel.model_key, operation) : ""} />
    </> : <div className="application-card-empty"><Code2 size={17} />
      {application.status !== "active" ? "应用未启用，暂不能生成调用示例。"
        : !hasActiveSubscription ? "该应用没有可用的活动订阅。"
          : !gateway?.enabled ? "该应用关联的 APIM 未启用，暂不能生成调用示例。"
            : !baseUrl ? "该 APIM 尚未提供可用的 API Base URL。"
              : !publishedModelsAvailable ? "暂时无法确认该 APIM 已发布的模型。"
                : "该应用没有已发布且获准访问的模型。"}
    </div>}
  </section>
}