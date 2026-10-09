import { useEffect, useRef, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, Circle, Copy, Eye, EyeOff, LoaderCircle, Plug, Rocket, ShieldCheck, TriangleAlert, X } from "lucide-react"

import { dataSource } from "../../data-sources/apim/api"
import { finopsKeys, finopsQueries } from "../../data-sources/apim/queries"
import type {
  GatewayPublication,
  GatewayPublicationCreate,
  GatewayPublicationStatus,
  ModelRegistry,
  ModelRuntime,
} from "../../data-sources/apim/types"
import {
  GatewayBrandLogo,
  ProviderBrandLogo,
  gatewayBrandFromIdentity,
  providerBrandFromMetadata,
} from "../brand-logos"
import { Button } from "../ui/button"
import { Checkbox } from "../ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog"
import { Input } from "../ui/input"
import { Progress } from "../ui/progress"
import { emptyPricingDraft, PublicModelPricing } from "./public-model-pricing"
import { DiscountField } from "./model-price-source"
import { resolveDiscount } from "./model-edit-form"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select"
import {
  preferredPublicationConnection,
  publicationConnectionAuth,
  publicationConnectionEndpoint,
  publicationConnectionNeedsCredential,
  publicationConnections,
  publicationConnectionTarget,
} from "./model-publication-connections"
import { FieldHelp } from "./field-help"
import { ModelVendorLogo } from "./model-vendor-select"
import {
  displayNameFromProviderId,
  modelVendorFromMetadata,
  modelVendorLabel,
  publicModelKeyFromProviderId,
} from "./openai-compatible"

const TERMINAL_STATUSES: GatewayPublicationStatus[] = [
  "active",
  "failed",
  "rolled_back",
  "superseded",
]

const PUBLICATION_STAGE_STATUSES: GatewayPublicationStatus[] = [
  "validating",
  "provisioning",
  "building_revision",
  "verifying",
  "promoting",
  "active",
]

function publicationStageLabels() {
  return [
    "验证配置",
    "准备后端资源",
    "构建 APIM Revision",
    "验证候选 Revision",
    "切换 APIM Revision",
    "完成",
  ]
}

function publicationStageIndex(status: GatewayPublicationStatus) {
  if (status === "queued") return 0
  if (status === "awaiting_authorization") return 3
  const index = PUBLICATION_STAGE_STATUSES.indexOf(status)
  return index < 0 ? null : index
}

function publicationTitle(kind: GatewayPublication["publication_kind"]) {
  if (kind === "model_remove") return "移除模型"
  if (kind === "credential_rotation") return "更新 API Key"
  if (kind === "route_reconcile") return "刷新网关路由"
  return "发布模型"
}

function PublicationProgress({ publication }: { publication: GatewayPublication }) {
  const stageIndex = publicationStageIndex(publication.status)
  const active = publication.status === "active"
  const completed = active || publication.status === "superseded"
  const failed = publication.status === "failed" || publication.status === "rolled_back"
  const paused = publication.status === "awaiting_authorization"
  const labels = publicationStageLabels()

  const progress = completed ? 100 : stageIndex === null ? 0 : ((stageIndex + 1) / labels.length) * 100

  return <section className="publication-progress" aria-label="模型发布配置进度">
    <div className="publication-progress-head">
      <span>发布进度</span>
      <b>{completed ? "6 / 6" : stageIndex === null ? "—" : `${stageIndex + 1} / 6`}</b>
    </div>
    {stageIndex !== null || completed ? <>
    <Progress value={progress} />
    <ol className="publication-stage-list">
      {labels.map((label, index) => {
        const complete = completed || (stageIndex !== null && index < stageIndex)
        const current = !completed && !failed && stageIndex === index
        return <li key={label} data-state={complete ? "complete" : current ? paused ? "paused" : "current" : failed && stageIndex === index ? "failed" : "pending"}>
          <span className="publication-stage-mark" aria-hidden="true">
            {complete ? <Check size={12} /> : current ? paused ? <ShieldCheck size={12} /> : <LoaderCircle className="spin" size={12} /> : failed && stageIndex === index ? <TriangleAlert size={12} /> : <Circle size={8} />}
          </span>
          <span>{label}</span>
        </li>
      })}
    </ol>
    </> : <p className="publication-progress-unavailable">发布未完成，当前 Revision 未受影响。</p>}
  </section>
}

function numberOrNull(value: string) {
  const text = value.trim()
  return text ? Number(text) : null
}

function optionalNumberError(values: string[]) {
  const numeric = values.filter((value) => value.trim()).map(Number)
  return numeric.some((value) => !Number.isFinite(value) || value < 0)
    ? "上下文窗口和价格必须是非负数字。"
    : null
}

function runtimeRegion(runtime: ModelRuntime) {
  if (typeof runtime.config.region === "string" && runtime.config.region.trim()) {
    return runtime.config.region.trim()
  }
  if (typeof runtime.config.backend_url !== "string") return null
  try {
    const hostname = new URL(runtime.config.backend_url).hostname
    const match = hostname.match(/^bedrock-runtime\.([^.]+)\.amazonaws\.com$/i)
    return match?.[1] ?? null
  } catch {
    return null
  }
}

function runtimeChoiceLabel(runtime: ModelRuntime) {
  const region = runtimeRegion(runtime)
  return region ? `${runtime.name} · ${region}` : runtime.name
}

export function publicationStatusLabel(status: GatewayPublicationStatus) {
  return {
    queued: "等待发布",
    validating: "正在验证配置",
    provisioning: "正在准备后端",
    building_revision: "正在构建 APIM Revision",
    verifying: "正在验证模型",
    awaiting_authorization: "等待 Foundry 授权",
    promoting: "正在切换 APIM Revision",
    active: "模型已发布",
    failed: "发布失败",
    superseded: "已被后续发布替代",
    rolling_back: "正在回滚",
    rolled_back: "已回滚",
  }[status]
}

export function ModelPublicationDialog({
  registry,
  publicationId,
  onPublicationQueued,
  onManageConnections,
  onClose,
}: {
  registry: ModelRegistry
  publicationId: string | null
  onPublicationQueued: (publicationId: string) => void
  onManageConnections: () => void
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const apimGateways = registry.gateways.filter(
    (gateway) => gateway.implementation === "apim" && gateway.enabled,
  )
  const [gatewayId, setGatewayId] = useState(
    apimGateways.find((gateway) => gateway.is_default)?.id
      ?? apimGateways[0]?.id
      ?? "",
  )
  const selectedGateway = apimGateways.find((gateway) => gateway.id === gatewayId)
  const apimRuntimes = publicationConnections(registry, gatewayId)
  const [runtimeId, setRuntimeId] = useState(
    preferredPublicationConnection(apimRuntimes)?.id ?? "",
  )
  const selectedRuntime = apimRuntimes.find((runtime) => runtime.id === runtimeId)
  const selectedProvider = registry.providers.find((provider) => provider.id === selectedRuntime?.provider_id)
  const foundry = selectedRuntime?.brand_key === "microsoft_foundry"
  const selectedRuntimeUsesOAuth = selectedRuntime?.config.auth_strategy === "oauth_client_credentials"
  const openaiCompatible = selectedProvider?.provider_kind === "openai_compatible"
  const selectedVendor = selectedRuntime && openaiCompatible
    ? modelVendorFromMetadata(selectedRuntime.config, selectedRuntime.name)
    : null
  const selectedRuntimeNeedsCredential = selectedRuntime
    ? publicationConnectionNeedsCredential(selectedRuntime)
    : false
  const connectionEndpoint = selectedRuntime ? publicationConnectionEndpoint(selectedRuntime) : null
  const connectionAuthLabel = {
    managed_identity: "托管身份",
    api_key: "API Key",
    oauth_m2m: "OAuth M2M",
    connection: "由连接管理",
  }[selectedRuntime ? publicationConnectionAuth(selectedRuntime, selectedProvider) : "connection"]

  const [providerApiKey, setProviderApiKey] = useState("")
  const [imageProbeAuthorization, setImageProbeAuthorization] = useState<string | null>(null)
  const [foundryDeployment, setFoundryDeployment] = useState("")
  const [modelOperation, setModelOperation] = useState<"chat" | "image_generation">("chat")
  const imageGeneration = foundry && modelOperation === "image_generation"
  const imageConfigurationSupported = registry.image_configuration_schema_version === 4
  const [keyRevealed, setKeyRevealed] = useState(false)
  const [modelKey, setModelKey] = useState("")
  const [displayName, setDisplayName] = useState("")
  const [upstreamModelId, setUpstreamModelId] = useState("")
  const [pricingDraft, setPricingDraft] = useState(emptyPricingDraft)
  const [pricingOpen, setPricingOpen] = useState(true)
  const { contextWindow, inputPrice, outputPrice, cacheReadPrice, cacheWritePrice } = pricingDraft
  const setContextWindow = (value: string) => setPricingDraft(current => ({ ...current, contextWindow: value }))
  const setInputPrice = (value: string) => setPricingDraft(current => ({ ...current, inputPrice: value }))
  const setOutputPrice = (value: string) => setPricingDraft(current => ({ ...current, outputPrice: value }))
  const setCacheReadPrice = (value: string) => setPricingDraft(current => ({ ...current, cacheReadPrice: value }))
  const setCacheWritePrice = (value: string) => setPricingDraft(current => ({ ...current, cacheWritePrice: value }))
  const followingPublicPrice = pricingDraft.priceSource === "models_dev"
  useEffect(() => {
    setPricingDraft(current => ({ ...current, priceReference: "", priceEntryDigest: "", allowUnpriced: false }))
  }, [runtimeId, foundryDeployment, upstreamModelId, modelKey, modelOperation])
  const [formError, setFormError] = useState<string | null>(null)
  const [publishError, setPublishError] = useState<string | null>(null)
  const [publishing, setPublishing] = useState(false)
  const activationNotified = useRef(false)
  const requiredCredentialMissing = selectedRuntimeNeedsCredential && !providerApiKey.trim()
  const effectiveModelKey = openaiCompatible ? publicModelKeyFromProviderId(upstreamModelId) : modelKey.trim()
  const effectiveDisplayName = openaiCompatible ? displayNameFromProviderId(upstreamModelId) : displayName.trim()
  const priceValues = imageGeneration ? [inputPrice, outputPrice, cacheReadPrice] : [inputPrice, outputPrice, cacheReadPrice, cacheWritePrice]
  const pricingStatus = priceValues.every(value => value.trim()) ? "价格已填写"
    : priceValues.some(value => value.trim()) ? "价格部分填写" : "价格未填写"
  const connectionLogo = (runtime: ModelRuntime) => registry.providers.find(provider => provider.id === runtime.provider_id)?.provider_kind === "openai_compatible"
    ? <ModelVendorLogo value={modelVendorFromMetadata(runtime.config, `${runtime.name} ${runtime.provider_name}`)} size={15} />
    : <ProviderBrandLogo brand={providerBrandFromMetadata(runtime.brand_key, runtime.provider_name)} size={15} />

  const publication = useQuery(finopsQueries.gatewayPublication(publicationId))
  const oauthCredential = publication.data
    ? publication.data.credential_kind === "oauth_m2m" : selectedRuntimeUsesOAuth
  const credentialLabel = oauthCredential ? "OAuth Client Secret" : "Provider API Key"
  const authorization = publication.data?.authorization

  useEffect(() => {
    if (publication.data?.status !== "active" || activationNotified.current) return
    activationNotified.current = true
    void queryClient.invalidateQueries({ queryKey: finopsKeys.registry })
  }, [publication.data?.status, queryClient])

  const clearConnectionInputs = () => {
    setProviderApiKey("")
    setFoundryDeployment("")
    setModelOperation("chat")
    setKeyRevealed(false)
    setModelKey("")
    setDisplayName("")
    setUpstreamModelId("")
    setContextWindow("")
    setInputPrice("")
    setOutputPrice("")
    setCacheReadPrice("")
    setCacheWritePrice("")
    setFormError(null)
    setPublishError(null)
  }

  const chooseGateway = (id: string | null) => {
    if (publishing || !id || id === gatewayId || !apimGateways.some((gateway) => gateway.id === id)) return
    const nextConnections = publicationConnections(registry, id)
    setGatewayId(id)
    setRuntimeId(preferredPublicationConnection(nextConnections, runtimeId)?.id ?? "")
    clearConnectionInputs()
  }

  const chooseRuntime = (id: string | null) => {
    if (publishing || !id || id === runtimeId || !apimRuntimes.some((runtime) => runtime.id === id)) return
    setRuntimeId(id)
    clearConnectionInputs()
  }

  const validate = () => {
    if (followingPublicPrice && !pricingDraft.priceReference
      && (imageGeneration || !pricingDraft.allowUnpriced)) {
      return "请同步定价或明确确认暂不计价。"
    }
    if (!selectedGateway) return "没有可用的 Azure API Management 网关。"
    if (!selectedRuntime || !selectedProvider) return "请选择当前网关下的可用连接。"
    if (requiredCredentialMissing) return selectedRuntimeUsesOAuth
      ? "请输入该连接首次发布所需的 OAuth Secret。" : "请输入该连接首次发布所需的一次性 API Key。"
    if (foundry) {
      if (!foundryDeployment.trim()) return "请输入已有的 Foundry Deployment Name。"
      if (imageGeneration && registry.image_generation_supported !== true) return "后端尚未启用图像生成"
      if (imageGeneration && !imageConfigurationSupported) return "后端尚未支持图像参数透传"
      if (imageGeneration && [inputPrice, cacheReadPrice, outputPrice].some(value => !value.trim())) {
        return "请填写文字输入、缓存文字和图像输出单价。"
      }
      return optionalNumberError([
        contextWindow, inputPrice, outputPrice, cacheReadPrice, cacheWritePrice,
      ])
    }
    if (!/^[a-zA-Z0-9._:-]+$/.test(effectiveModelKey) || effectiveModelKey.length > 255) return "模型 Key 格式无效。"
    if (!effectiveDisplayName || effectiveDisplayName.length > 255) return "请输入显示名称。"
    if (!upstreamModelId.trim()) return "请输入上游模型 ID。"
    return optionalNumberError([
      contextWindow, inputPrice, outputPrice, cacheReadPrice, cacheWritePrice,
    ])
  }

  const submit = async () => {
    if (publishing) return
    const error = validate()
    setFormError(error)
    setPublishError(null)
    if (error || !selectedGateway || !selectedRuntime || !selectedProvider) return
    setPublishing(true)
    try {
      const request: GatewayPublicationCreate = {
        ...publicationConnectionTarget(registry, selectedGateway.id, selectedRuntime.id, providerApiKey),
        model: {
          operation: imageGeneration ? "image_generation" : undefined,
          deployment_name: foundry ? foundryDeployment.trim() : undefined,
          model_key: foundry ? undefined : effectiveModelKey,
          display_name: foundry ? undefined : effectiveDisplayName,
          upstream_model_id: foundry ? undefined : upstreamModelId.trim(),
          context_window: imageGeneration ? null : numberOrNull(contextWindow),
          input_cost_per_million: numberOrNull(inputPrice),
          output_cost_per_million: numberOrNull(outputPrice),
          cached_cost_per_million: numberOrNull(cacheReadPrice),
          cache_write_cost_per_million: imageGeneration ? null : numberOrNull(cacheWritePrice),
          price_source: pricingDraft.priceSource,
          price_reference: followingPublicPrice ? pricingDraft.priceReference || null : null,
          price_entry_digest: followingPublicPrice ? pricingDraft.priceEntryDigest || null : null,
          allow_unpriced: pricingDraft.allowUnpriced,
          price_discount_percent: pricingDraft.discountPercent.trim() ? Number(pricingDraft.discountPercent) : null,
        },
      }
      const accepted = await dataSource.publishModel(request)
      setProviderApiKey("")
      setKeyRevealed(false)
      queryClient.setQueryData(
        finopsKeys.gatewayPublication(accepted.publication.id),
        accepted.publication,
      )
      onPublicationQueued(accepted.publication.id)
    } catch (requestError) {
      setPublishError(String(requestError))
    } finally {
      setPublishing(false)
    }
  }

  const status = publication.data?.status
  const terminal = status ? TERMINAL_STATUSES.includes(status) : false
  const failed = status === "failed"
  const awaitingAuthorization = status === "awaiting_authorization"
  const active = status === "active"
  const superseded = status === "superseded"
  const rolledBack = status === "rolled_back"
  const dialogTitle = publication.data
    ? publicationTitle(publication.data.publication_kind)
    : publicationId
      ? "发布详情"
      : "添加模型"

  const retry = async () => {
    if (!publicationId) return
    const retryRequiresCredential = publication.data?.retry_requires_credential ?? false
    if (retryRequiresCredential && !providerApiKey.trim()) return
    setPublishing(true)
    setPublishError(null)
    try {
      const queued = await dataSource.retryGatewayPublication(
        publicationId,
        retryRequiresCredential ? providerApiKey.trim() : undefined,
        publication.data?.retry_can_authorize_image_probes === true && imageProbeAuthorization === publicationId,
        oauthCredential ? "oauth_m2m" : "api_key",
      )
      setProviderApiKey("")
      setKeyRevealed(false)
      queryClient.setQueryData(
        finopsKeys.gatewayPublication(publicationId),
        queued,
      )
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayPublications })
      onPublicationQueued(publicationId)
    } catch (requestError) {
      setPublishError(String(requestError))
    } finally {
      setImageProbeAuthorization(null)
      setPublishing(false)
    }
  }

  const resumeAuthorization = async () => {
    if (!publicationId) return
    setPublishing(true)
    setPublishError(null)
    try {
      const verifying = await dataSource.resumeGatewayPublicationAuthorization(publicationId)
      queryClient.setQueryData(finopsKeys.gatewayPublication(publicationId), verifying)
      onPublicationQueued(publicationId)
    } catch (requestError) {
      setPublishError(String(requestError))
    } finally {
      setPublishing(false)
    }
  }

  const copyAuthorization = async () => {
    const authorization = publication.data?.authorization
    if (!authorization) return
    try {
      await navigator.clipboard.writeText([
        authorization.kind === "databricks_oauth"
          ? `Databricks Client ID: ${authorization.client_id}`
          : `APIM Object ID: ${authorization.principal_id}`,
        `Role: ${authorization.role_name}${authorization.kind === "azure_rbac" ? ` (${authorization.role_id})` : ""}`,
        `Resource: ${authorization.resource_endpoint}`,
      ].join("\n"))
    } catch (error) {
      setPublishError(String(error))
    }
  }

  const close = () => { if (!publishing) onClose() }

  return <Dialog open onOpenChange={(open) => { if (!open) close() }}>
    <DialogContent className="registry-editor-dialog simple-model-dialog publication-connection-dialog" finalFocus={false}>
      <form className="registry-editor simple-model-form" aria-busy={publishing} onSubmit={(event) => {
        event.preventDefault()
        if (!publicationId) void submit()
      }}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>{dialogTitle}</DialogTitle>
          {!publicationId && <DialogDescription>选择已有连接，将模型或部署发布到所选网关。</DialogDescription>}
          {publication.data && <DialogDescription data-no-localize>{`${publication.data.display_name} · ${publication.data.model_key}`}</DialogDescription>}
        </DialogHeader>
        <button type="button" className="registry-editor-close" onClick={close} disabled={publishing} aria-label="关闭"><X size={16} /></button>

        <div className="registry-editor-body simple-model-body">
          {publicationId && publication.isPending && <div className="publication-detail-loading"><LoaderCircle className="spin" size={16} />加载发布详情</div>}
          {publicationId && publication.isError && <div className="publication-detail-error">
            <div className="registry-error">无法加载发布详情：{String(publication.error)}</div>
            <Button type="button" variant="outline" onClick={() => void publication.refetch()}>重新加载</Button>
          </div>}
          {publication.data && <>
            <PublicationProgress publication={publication.data} />
            {!superseded && <div className={`publication-safety-note ${failed || rolledBack ? "failed" : awaitingAuthorization ? "warning" : active ? "active" : "running"}`}>
              {failed || rolledBack ? <TriangleAlert size={15} /> : awaitingAuthorization ? <ShieldCheck size={15} /> : active ? <Check size={15} /> : <ShieldCheck size={15} />}
              <div>
                <span className="publication-safety-title"><b>{failed || rolledBack ? "当前模型继续正常服务" : awaitingAuthorization ? "当前模型继续正常服务" : active ? publication.data.publication_kind === "model_remove" ? "模型已从 Turnstile 移除" : "发布完成" : "现有模型继续使用当前 Revision"}</b>{!failed && <FieldHelp>{rolledBack ? "当前模型继续正常服务" : awaitingAuthorization ? "目标提供方授权就绪后继续验证，未通过前不会切换当前 Revision。" : active ? publication.data.publication_kind === "model_remove" ? "历史用量与上游模型保留。" : "模型已发布，分配人员后即可使用" : "发布过程不会改变当前 Revision，验证通过后才切换。"}</FieldHelp>}</span>
                {failed && <span className="publication-safety-error">{publication.data.error_message ?? "发布未完成，请使用发布 ID 查看服务日志。"}</span>}
              </div>
            </div>}
          </>}

          {awaitingAuthorization && authorization && <div className="simple-model-section simple-connection-section">
            <div className="simple-section-title"><b>{authorization.kind === "azure_rbac" ? "授权 APIM 访问 Foundry 资源" : "授权 Databricks 模型访问"}</b><FieldHelp>{authorization.kind === "azure_rbac" ? "请在该 Foundry 资源的 Access control (IAM) 中，将以下 APIM Managed Identity 添加为 Cognitive Services User。Turnstile 不需要 Foundry API Key。" : authorization.kind === "databricks_oauth" ? "目标 Databricks 服务主体需要 Workspace 访问和模型查询权限。此处不是 Azure RBAC 授权。" : "在同租户 Databricks Workspace 中添加 APIM 的 Entra 服务主体并授予模型查询权限。下列 Object ID 用于查找 APIM 身份，不是 Databricks 所需的 Application (Client) ID。"}</FieldHelp></div>
            <dl className="simple-authorization-list">
              <div><dt>{authorization.kind === "databricks_oauth" ? "Databricks Client ID" : "APIM Object ID"}</dt><dd>{authorization.kind === "databricks_oauth" ? authorization.client_id : authorization.principal_id}</dd></div>
              <div><dt>Role</dt><dd>{authorization.role_name}{authorization.kind === "azure_rbac" ? ` · ${authorization.role_id}` : ""}</dd></div>
              <div><dt>{authorization.kind === "azure_rbac" ? "Foundry 资源" : "Workspace"}</dt><dd>{authorization.resource_endpoint}</dd></div>
            </dl>
            <Button type="button" variant="outline" onClick={() => void copyAuthorization()}><Copy size={14} />复制授权信息</Button>
          </div>}

          {failed && publication.data?.retry_can_authorize_image_probes === true && <div className="simple-model-section simple-connection-section">
            <label className="model-editor-checkbox">
              <Checkbox id="authorize-image-probes" checked={imageProbeAuthorization === publicationId}
                disabled={publishing} onCheckedChange={checked => setImageProbeAuthorization(checked === true ? publicationId : null)} />
              <span>授权本次重试新增付费图像探针</span>
            </label>
            <p className="publication-form-note">此前图像请求可能已产生费用。确认后，每个待验证图像模型最多新增一次探针；已验证的相同请求不会重放。</p>
          </div>}
          {failed && publication.data?.retry_requires_credential && <div className="simple-model-section simple-connection-section">
            <div className="simple-section-title"><b>重新发布</b></div>
            <div className="registry-field"><span className="registry-field-label-row"><label className="registry-field-label" htmlFor="retry-api-key">{credentialLabel}</label><FieldHelp>{oauthCredential ? "请输入新的 Databricks OAuth Secret。" : "请输入新的 API Key 后重新发布同一模型。"}</FieldHelp></span><div className="login-password"><Input id="retry-api-key" type={keyRevealed ? "text" : "password"} autoComplete="off" value={providerApiKey} onChange={(event) => setProviderApiKey(event.target.value)} disabled={publishing} /><button type="button" className="login-reveal" onClick={() => setKeyRevealed((value) => !value)} aria-label={keyRevealed ? "隐藏凭据" : "显示凭据"} aria-pressed={keyRevealed} disabled={publishing}>{keyRevealed ? <EyeOff size={15} /> : <Eye size={15} />}</button></div></div>
          </div>}

          {!publicationId && <>
          <section className="simple-model-section publication-target" aria-label="发布目标">
            {apimGateways.length > 1 ? <div className="registry-field">
                <span className="registry-field-label">目标网关</span>
                <Select value={selectedGateway?.id ?? ""} onValueChange={chooseGateway} disabled={publishing}>
                  <SelectTrigger className="registry-select-trigger" aria-label="目标网关">
                    <SelectValue>{selectedGateway ? <span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity(`${selectedGateway.name} ${selectedGateway.implementation}`)} size={15} /><span data-no-localize>{selectedGateway.name}</span></span> : "选择目标网关"}</SelectValue>
                  </SelectTrigger>
                  <SelectContent align="start" alignItemWithTrigger={false}>
                    {apimGateways.map((gateway) => <SelectItem key={gateway.id} value={gateway.id}><span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity(`${gateway.name} ${gateway.implementation}`)} size={15} /><span>{gateway.name}</span></span></SelectItem>)}
                  </SelectContent>
                </Select>
            </div> : selectedGateway ? <div className="publication-target-summary">
              <span className="registry-field-label">目标网关</span>
              <span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity(`${selectedGateway.name} ${selectedGateway.implementation}`)} size={15} /><span data-no-localize>{selectedGateway.name}</span></span>
            </div> : <div className="registry-error" role="alert">没有可用的 Azure API Management 网关。</div>}
          </section>

          {selectedGateway && <>
          <section className="simple-model-section" aria-labelledby="publication-connection-heading">
            <div className="publication-section-heading">
              <span className="simple-section-title"><b id="publication-connection-heading">连接</b><FieldHelp>连接包含服务端点和认证方式。切换网关或连接会清空未提交的模型与凭据。</FieldHelp></span>
              <Button type="button" variant="ghost" size="xs" className="publication-connection-action" onClick={onManageConnections} disabled={publishing}>管理连接</Button>
            </div>
            {apimRuntimes.length > 0 ? <div className="registry-field">
                <Select value={selectedRuntime?.id ?? ""} onValueChange={chooseRuntime} disabled={publishing}>
                  <SelectTrigger className="registry-select-trigger" aria-label="连接">
                    <SelectValue>{selectedRuntime ? <span className="registry-option">
                      {connectionLogo(selectedRuntime)}
                      <span data-no-localize>{runtimeChoiceLabel(selectedRuntime)}</span>
                    </span> : "选择连接"}</SelectValue>
                  </SelectTrigger>
                  <SelectContent align="start" alignItemWithTrigger={false}>
                    {apimRuntimes.map((runtime) => <SelectItem key={runtime.id} value={runtime.id}>
                      <span className="registry-option">
                        {connectionLogo(runtime)}
                        <span data-no-localize>{runtimeChoiceLabel(runtime)}</span>
                      </span>
                    </SelectItem>)}
                  </SelectContent>
                </Select>
                {!selectedRuntime && <div className="registry-error" role="alert">所选连接已不可用，请重新选择。</div>}
            </div> : <p className="publication-form-note" role="status">此网关没有可用连接。请先到连接管理添加并启用连接。</p>}
            {selectedRuntime && selectedProvider && <dl className="publication-connection-summary" aria-label="连接信息">
              <div><dt>接入类型</dt><dd className="registry-option">{openaiCompatible ? <Plug size={15} aria-hidden="true" /> : <ProviderBrandLogo brand={providerBrandFromMetadata(selectedRuntime.brand_key, selectedProvider.name)} size={15} />}<span data-no-localize>{openaiCompatible ? "OpenAI-compatible API" : selectedProvider.name}</span></dd></div>
              <div><dt>认证方式</dt><dd>{connectionAuthLabel}</dd></div>
              {selectedVendor && <div><dt>API 服务商</dt><dd className="registry-option"><ModelVendorLogo value={selectedVendor} size={15} /><span>{modelVendorLabel(selectedVendor)}</span></dd></div>}
              {connectionEndpoint && <div className="publication-connection-endpoint"><dt>Endpoint</dt><dd data-no-localize>{connectionEndpoint}</dd></div>}
            </dl>}
          </section>

          {selectedRuntime && selectedProvider && <>
            <section className="simple-model-section simple-connection-section" aria-labelledby="publication-model-heading">
              <div className="simple-section-title"><b id="publication-model-heading">模型</b></div>
              {foundry ? <div className="simple-model-grid publication-model-identity-grid"><div className="registry-field">
                <span className="registry-field-label">模型用途</span>
                <Select value={modelOperation} disabled={publishing} onValueChange={operation => {
                  if (operation !== "chat" && operation !== "image_generation") return
                  setModelOperation(operation)
                  setContextWindow("")
                  setInputPrice("")
                  setOutputPrice("")
                  setCacheReadPrice("")
                  setCacheWritePrice("")
                }}>
                  <SelectTrigger className="registry-select-trigger" aria-label="模型用途"><SelectValue>{imageGeneration ? "文生图" : "对话"}</SelectValue></SelectTrigger>
                  <SelectContent align="start" alignItemWithTrigger={false}>
                    <SelectItem value="chat">对话</SelectItem>
                    <SelectItem value="image_generation" disabled={registry.image_generation_supported !== true || !imageConfigurationSupported}>文生图</SelectItem>
                  </SelectContent>
                </Select>
              </div><div className="registry-field">
                <span className="registry-field-label-row"><label className="registry-field-label" htmlFor="foundry-deployment">部署名称</label><FieldHelp>填写此 Foundry Project 中已有的部署名称。Turnstile 不会创建上游部署。</FieldHelp></span>
                <Input id="foundry-deployment" value={foundryDeployment} onChange={(event) => setFoundryDeployment(event.target.value)} disabled={publishing} />
              </div></div> : openaiCompatible ? <>
                <div className="registry-field"><span className="registry-field-label-row"><label className="registry-field-label" htmlFor="provider-model-id">上游模型 ID</label><FieldHelp>供应商 API 在 model 字段中要求的真实模型 ID。Turnstile 会自动生成公开别名和显示名称。</FieldHelp></span>
                  <Input id="provider-model-id" value={upstreamModelId} onChange={(event) => setUpstreamModelId(event.target.value)} disabled={publishing} maxLength={500} />
                </div>
                {upstreamModelId.trim() && <dl className="publication-connection-summary">
                  <div><dt>模型别名</dt><dd data-no-localize><code>{effectiveModelKey || "-"}</code></dd></div>
                  <div><dt>显示名称</dt><dd data-no-localize>{effectiveDisplayName || "-"}</dd></div>
                </dl>}
              </> : <>
                <div className="form-grid">
                  <label className="registry-field"><span className="registry-field-label">模型 Key</span><Input value={modelKey} onChange={(event) => setModelKey(event.target.value)} disabled={publishing} /></label>
                  <label className="registry-field"><span className="registry-field-label">显示名称</span><Input value={displayName} onChange={(event) => setDisplayName(event.target.value)} disabled={publishing} /></label>
                </div>
                <label className="registry-field"><span className="registry-field-label">上游模型 ID</span><Input value={upstreamModelId} onChange={(event) => setUpstreamModelId(event.target.value)} disabled={publishing} /></label>
              </>}
            </section>
            {imageGeneration && !imageConfigurationSupported && <div className="registry-error" role="alert">后端尚未支持图像参数透传</div>}
            <details className="simple-pricing" open={pricingOpen}
              onToggle={event => setPricingOpen(event.currentTarget.open)}>
              <summary><span>价格与限制</span><span className="publication-pricing-status">{pricingStatus}</span></summary>
              <div className="simple-pricing-fields">
                <div className="model-price-source">
                  <span className="registry-field-label">价格来源</span>
                  <div className="model-price-source-choice" role="radiogroup" aria-label="价格来源">
                    {(["models_dev", "manual"] as const).map(source => <label className="model-editor-checkbox" key={source}>
                      <input type="radio" name="publication-price-source" value={source}
                        checked={pricingDraft.priceSource === source} disabled={publishing}
                        onChange={() => setPricingDraft(current => ({
                          ...current, priceSource: source, priceReference: "", priceEntryDigest: "", allowUnpriced: false,
                        }))} />
                      <span>{source === "models_dev" ? "从公网同步定价" : "手工填写"}</span>
                    </label>)}
                  </div>
                  {followingPublicPrice && <PublicModelPricing
                    request={{ runtime_id: runtimeId, operation: imageGeneration ? "image_generation" : "chat",
                      deployment_name: foundry ? foundryDeployment : undefined,
                      upstream_model_id: foundry ? undefined : upstreamModelId,
                      model_key: foundry ? undefined : effectiveModelKey,
                      display_name: foundry ? undefined : effectiveDisplayName }}
                    draft={pricingDraft} onChange={setPricingDraft} busy={publishing} />}
                  {followingPublicPrice && <DiscountField draft={pricingDraft}
                    busy={publishing} connectionDiscount={selectedRuntime?.price_discount_percent ?? null}
                    discount={resolveDiscount(pricingDraft, selectedRuntime?.price_discount_percent ?? null)}
                    setDraft={updater => setPricingDraft(current => ({ ...current, ...updater(current) }))} />}
                </div>
                <div className={imageGeneration ? "form-grid" : "form-grid three"}>
                  {!imageGeneration && <label className="registry-field"><span className="registry-field-label">上下文窗口</span><Input id="publication-context-window" type="number" min="1" value={contextWindow} onChange={(event) => setPricingDraft(current => ({ ...current, contextWindow: event.target.value, contextOrigin: "user" }))} disabled={publishing} /></label>}
                  <label className="registry-field"><span className="registry-field-label">{imageGeneration ? "文字输入 $/M" : "输入 $/M"}</span><Input id="publication-input-price" type="number" min="0" step="0.000001" value={inputPrice} onChange={(event) => setInputPrice(event.target.value)} disabled={publishing || followingPublicPrice} required={imageGeneration} /></label>
                  <label className="registry-field"><span className="registry-field-label">{imageGeneration ? "图像输出 $/M" : "输出 $/M"}</span><Input id="publication-output-price" type="number" min="0" step="0.000001" value={outputPrice} onChange={(event) => setOutputPrice(event.target.value)} disabled={publishing || followingPublicPrice} required={imageGeneration} /></label>
                </div>
                <div className="form-grid">
                  <label className="registry-field"><span className="registry-field-label">{imageGeneration ? "缓存文字输入 $/M" : "缓存读取 $/M"}</span><Input id="publication-cache-read-price" type="number" min="0" step="0.000001" value={cacheReadPrice} onChange={(event) => setCacheReadPrice(event.target.value)} disabled={publishing || followingPublicPrice} required={imageGeneration} /></label>
                  {!imageGeneration && <label className="registry-field"><span className="registry-field-label">缓存写入 $/M</span><Input id="publication-cache-write-price" type="number" min="0" step="0.000001" value={cacheWritePrice} onChange={(event) => setCacheWritePrice(event.target.value)} disabled={publishing || followingPublicPrice} /></label>}
                </div>
              </div>
            </details>
            {selectedRuntimeNeedsCredential && <section className="simple-model-section simple-connection-section" aria-labelledby="publication-credential-heading">
              <div className="simple-section-title"><b id="publication-credential-heading">首次发布凭据</b></div>
              <p className="publication-form-note">{selectedRuntimeUsesOAuth ? "仅补充已有连接首次发布所需的 Databricks OAuth Secret。" : "仅补充已有连接首次发布所需的 API Key，不会创建新连接。"}</p>
              <div className="registry-field">
                <span className="registry-field-label-row">
                  <label className="registry-field-label" htmlFor="existing-provider-api-key">{selectedRuntimeUsesOAuth ? credentialLabel : "一次性 API Key"}</label>
                  <FieldHelp>{selectedRuntimeUsesOAuth ? "仅首次发布需要。Secret 写入 APIM Credential Manager 并验证后即从临时记录中清除；不是 PAT 或 Entra 应用密钥。" : "仅首次发布需要。Key 写入 APIM Secret Named Value 后即从 Turnstile 临时记录中清除；后续模型无需重复提供。"}</FieldHelp>
                </span>
                <div className="login-password">
                  <Input id="existing-provider-api-key" type={keyRevealed ? "text" : "password"} autoComplete="off" value={providerApiKey} onChange={(event) => setProviderApiKey(event.target.value)} disabled={publishing} />
                  <button type="button" className="login-reveal" onClick={() => setKeyRevealed((value) => !value)} aria-label={keyRevealed ? "隐藏 API Key" : "显示 API Key"} aria-pressed={keyRevealed} disabled={publishing}>{keyRevealed ? <EyeOff size={15} /> : <Eye size={15} />}</button>
                </div>
              </div>
            </section>}
          </>}
          </>}
          </>}

          {(formError || publishError) && <div className="registry-error" role="alert">{formError ?? publishError}</div>}
        </div>

        <DialogFooter className="registry-editor-footer">
          <Button type="button" variant="outline" className="publication-dismiss" onClick={close} disabled={publishing}>{publicationId ? "关闭" : "取消"}</Button>
          {failed && <Button type="button" onClick={() => void retry()} disabled={publishing || Boolean(publication.data?.retry_requires_credential && !providerApiKey.trim())}><Rocket size={14} />重新发布</Button>}
          {awaitingAuthorization && <Button type="button" onClick={() => void resumeAuthorization()} disabled={publishing}><ShieldCheck size={14} />我已授权，重新验证</Button>}
          {!publicationId && <Button type="submit" disabled={publishing || !selectedRuntime || !selectedProvider || requiredCredentialMissing}><Rocket size={14} />{publishing ? "正在提交" : "发布模型"}</Button>}
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}
