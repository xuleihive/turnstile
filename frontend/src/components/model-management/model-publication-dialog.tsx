import { useEffect, useRef, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, Circle, Copy, Eye, EyeOff, LoaderCircle, Plug, RefreshCw, Rocket, Server, ShieldCheck, TriangleAlert, X } from "lucide-react"

import { dataSource } from "../../data-sources/apim/api"
import { finopsKeys, finopsQueries } from "../../data-sources/apim/queries"
import { translateForLocale, useLocale } from "../../locales/index"
import type {
  ApimTargetTopologyList,
  GatewayPublication,
  GatewayPublicationCreate,
  GatewayPublicationStatus,
  ModelProvider,
  ModelRegistry,
  ModelRuntime,
} from "../../data-sources/apim/types"
import {
  apimGatewayProfiles,
  gatewayPublicationChoices,
  publicationTargetDetails,
  resolveGatewaySelection,
  type GatewaySelection,
} from "../../lib/gateway-topology"
import {
  GatewayBrandLogo,
  ManagedGatewayLogo,
  ProviderBrandLogo,
  gatewayBrandFromIdentity,
  providerBrandFromMetadata,
} from "../brand-logos"
import { Button } from "../ui/button"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select"
import { FieldHelp } from "./field-help"
import { contextPriceRulePayload, createModelPriceDraft, validateModelPricing } from "./model-edit-form"
import { ModelPriceSourceFields, ModelPriceText } from "./model-price-source"
import {
  preferredPublicationConnection,
  publicationConnectionAuth,
  publicationConnectionEndpoint,
  publicationConnections,
} from "./model-publication-connections"
import {
  ModelVendorLogo,
  modelVendorFromMetadata,
  modelVendorLabel,
} from "./model-vendor-select"

const TERMINAL_STATUSES: GatewayPublicationStatus[] = [
  "active",
  "failed",
  "rolled_back",
  "superseded",
]

const PUBLICATION_STAGE_STATUSES: GatewayPublicationStatus[] = [
  "validating",
  "provisioning",
  "verifying",
  "promoting",
  "active",
]

function publicationStageLabels(configurationOnly = false) {
  return [
    "验证配置",
    "准备模型路由",
    configurationOnly ? "验证路由配置" : "验证模型",
    "启用发布",
    "完成",
  ]
}

function publicationStageIndex(status: GatewayPublicationStatus) {
  if (status === "queued") return 0
  if (status === "building_revision") return 1
  if (status === "awaiting_authorization") return 2
  const index = PUBLICATION_STAGE_STATUSES.indexOf(status)
  return index < 0 ? null : index
}

function publicationTitle(kind: GatewayPublication["publication_kind"]) {
  if (kind === "model_remove") return "移除模型"
  if (kind === "credential_rotation") return "更新凭据"
  if (kind === "route_reconcile") return "刷新网关路由"
  return "发布模型"
}

export function PublicationTargetDetails({ publication, registry, topology }: {
  publication: GatewayPublication
  registry: Pick<ModelRegistry, "gateways">
  topology: ApimTargetTopologyList | null
}) {
  const target = publicationTargetDetails(publication, registry.gateways, topology)
  return <dl className="publication-target-details">
    <div><dt>目标 Gateway</dt><dd>{target.gateways.length ? target.gateways.map(gateway => <div key={gateway.id} data-deployment-id={gateway.id}>
      <span data-no-localize>{gateway.name}</span><small>{gateway.kind === "managed" ? "Managed" : "Self-hosted"}</small>
      {gateway.name !== gateway.nativeId && <code data-no-localize>{gateway.nativeId}</code>}
    </div>) : <span>本次任务未提供具体 Gateway 信息</span>}</dd></div>
    <div><dt>所属 APIM</dt><dd><span data-no-localize>{target.apimName}</span>{target.serviceName && target.serviceName !== target.apimName && <code data-no-localize>{target.serviceName}</code>}</dd></div>
  </dl>
}

function PublicationProgress({ publication }: { publication: GatewayPublication }) {
  const stageIndex = publicationStageIndex(publication.status)
  const active = publication.status === "active"
  const completed = active || publication.status === "superseded"
  const failed = publication.status === "failed" || publication.status === "rolled_back"
  const paused = publication.status === "awaiting_authorization"
  const removing = publication.publication_kind === "model_remove"
  const labels = publicationStageLabels(publication.configuration_only === true)
  const started = Date.parse(publication.created_at)
  const finished = publication.completed_at ? Date.parse(publication.completed_at) : Date.now()
  const elapsed = Number.isFinite(started) && Number.isFinite(finished)
    ? Math.max(0, Math.floor((finished - started) / 1000)) : null

  const progress = completed ? 100 : stageIndex === null ? 0 : ((stageIndex + 1) / labels.length) * 100

  return <section className="publication-progress" data-kind={publication.publication_kind} aria-label={removing ? "模型移除进度" : "模型发布配置进度"}>
    <div className="publication-progress-head">
      <span>{removing ? "移除进度" : "发布进度"}</span>
      {elapsed !== null && <span><span>耗时</span> <span data-no-localize>{Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, "0")}</span></span>}
      <b>{removing ? publicationStatusLabel(publication.status, false, "model_remove") : completed ? `${labels.length} / ${labels.length}` : stageIndex === null ? "—" : `${stageIndex + 1} / ${labels.length}`}</b>
    </div>
    {!removing && (stageIndex !== null || completed) ? <>
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
    </> : <p className="publication-progress-unavailable">{removing ? completed ? "模型已从 Turnstile 移除" : "移除尚未完成" : "发布未完成，生效状态未确认。"}</p>}
  </section>
}

function ConnectionTypeLogo({ runtime, provider }: { runtime: ModelRuntime; provider: ModelProvider }) {
  return provider.provider_kind === "openai_compatible"
    ? <Plug size={15} aria-hidden="true" />
    : <ProviderBrandLogo brand={providerBrandFromMetadata(runtime.brand_key, provider.name)} size={15} />
}

function ConnectionLogo({ runtime, providers }: { runtime: ModelRuntime; providers: ModelProvider[] }) {
  const provider = providers.find((item) => item.id === runtime.provider_id)
  return provider?.provider_kind === "openai_compatible"
    ? <ModelVendorLogo value={modelVendorFromMetadata(runtime.config, `${runtime.name} ${runtime.provider_name}`)} size={15} />
    : <ProviderBrandLogo brand={providerBrandFromMetadata(runtime.brand_key, runtime.provider_name)} size={15} />
}

function numberOrNull(value: string) {
  const text = value.trim()
  return text ? Number(text) : null
}

function publicModelKeyFromProviderId(value: string) {
  return value.trim().toLowerCase()
    .replace(/[^a-z0-9._:-]+/g, "-")
    .replace(/^-+|-+$/g, "")
}

function displayNameFromProviderId(value: string) {
  const leaf = value.trim().split("/").at(-1) ?? ""
  const words = leaf.replace(/[-_]+/g, " ").trim()
  const title = words.replace(/(^|\s)([a-z])/g, (_match, space: string, letter: string) => `${space}${letter.toUpperCase()}`)
  return title
    .replace(/^Deepseek\b/i, "DeepSeek")
    .replace(/^Openai\b/i, "OpenAI")
    .replace(/^Gpt\b/i, "GPT")
    .replace(/^Kimi\b/i, "Kimi")
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

function optionalNumberError(values: string[]) {
  const numeric = values.filter((value) => value.trim()).map(Number)
  return numeric.some((value) => !Number.isFinite(value) || value < 0)
    ? "上下文窗口和价格必须是非负数字。"
    : null
}

function publicationErrorMessage(message: string): string {
  const response = message.match(/^(?:(?:ApiError|Error):\s*)?API (\d{3}):\s*([\s\S]*)$/)
  if (!response) return message
  let payload: { detail?: unknown }
  try {
    const parsed: unknown = JSON.parse(response[2])
    if (typeof parsed !== "object" || parsed === null) return message
    payload = parsed
  } catch {
    return message
  }
  if (typeof payload.detail === "string") return payload.detail
  if (!Array.isArray(payload.detail)) return message
  const issues = payload.detail.filter((issue): issue is { type?: unknown; loc?: unknown; msg?: unknown } =>
    typeof issue === "object" && issue !== null)
  const pricingFields = ["price_source", "price_reference", "price_discount_percent", "context_price_references", "context_price_rule"]
  if (response[1] === "422" && issues.length > 0 && issues.every(issue =>
    issue.type === "extra_forbidden" && Array.isArray(issue.loc) && issue.loc.length === 3
    && issue.loc[0] === "body" && issue.loc[1] === "model" && pricingFields.includes(issue.loc[2]))) {
    return "前后端计价接口版本不一致，请更新后端后再发布。"
  }
  return issues.map(issue => {
    if (typeof issue.msg !== "string") return ""
    const location = Array.isArray(issue.loc)
      ? issue.loc.filter(part => (typeof part === "string" || typeof part === "number") && part !== "body").join(".")
      : ""
    return location ? `${location}: ${issue.msg}` : issue.msg
  }).filter(Boolean).join("\n") || message
}

function PublicationErrorNotice({ message }: { message: string }) {
  const { locale } = useLocale()
  const source = publicationErrorMessage(message)
  const [translated, setTranslated] = useState<{ source: string; locale: string; text: string } | null>(null)
  useEffect(() => {
    let active = true
    void translateForLocale(source, locale).then(text => {
      if (active) setTranslated({ source, locale, text })
    }).catch(() => undefined)
    return () => { active = false }
  }, [source, locale])
  return <div className="registry-error publication-form-error" role="alert">
    <TriangleAlert size={15} aria-hidden="true" />
    <div data-no-localize>{translated?.source === source && translated.locale === locale ? translated.text : source}</div>
  </div>
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

export function publicationStatusLabel(status: GatewayPublicationStatus, configurationOnly = false, kind?: GatewayPublication["publication_kind"]) {
  if (kind === "model_remove") return {
    queued: "等待移除",
    validating: "正在移除模型",
    provisioning: "正在移除模型",
    building_revision: "正在移除模型",
    verifying: "正在移除模型",
    awaiting_authorization: "等待授权",
    promoting: "正在移除模型",
    active: "模型已移除",
    failed: "移除失败",
    superseded: "移除任务已被替代",
    rolling_back: "正在回滚移除",
    rolled_back: "移除已回退",
  }[status]
  return {
    queued: "等待发布",
    validating: "正在验证配置",
    provisioning: "正在准备模型路由",
    building_revision: "正在准备模型路由",
    verifying: configurationOnly ? "正在验证路由配置" : "正在验证模型",
    awaiting_authorization: "等待提供方授权",
    promoting: "正在启用发布",
    active: "模型已发布",
    failed: "发布失败",
    superseded: "已被后续发布替代",
    rolling_back: "正在回滚",
    rolled_back: "已回滚",
  }[status]
}

export function ModelPublicationDialog({
  registry,
  topology = null,
  topologyAvailable = registry.apim_target_topology_available === true,
  topologyLoading = false,
  topologyError = null,
  publicationId,
  onPublicationQueued,
  onManageConnections,
  onClose,
  autoCloseOnSuccess = true,
}: {
  registry: ModelRegistry
  topology?: ApimTargetTopologyList | null
  topologyAvailable?: boolean
  topologyLoading?: boolean
  topologyError?: string | null
  publicationId: string | null
  onPublicationQueued: (publicationId: string) => void
  onManageConnections: () => void
  onClose: () => void
  autoCloseOnSuccess?: boolean
}) {
  const queryClient = useQueryClient()
  const apimGateways = apimGatewayProfiles(registry.gateways, topology, topologyAvailable)
  const gatewayChoices = gatewayPublicationChoices(registry.gateways, topology, topologyAvailable)
    .filter((choice) => choice.kind === "managed" || registry.self_hosted_gateway_enabled === true)
  const [gatewaySelection, setGatewaySelection] = useState<GatewaySelection>({ targetId: null, deploymentId: null })
  const { targetId: gatewayId, choice: selectedGatewayApi, targetChoices } = resolveGatewaySelection(
    apimGateways, gatewayChoices, gatewaySelection,
  )
  const topologyBlocked = topologyAvailable && (topologyLoading || Boolean(topologyError) || !topology || !selectedGatewayApi?.selectable)
  const selectedGateway = apimGateways.find((gateway) => gateway.id === gatewayId)
  const apimRuntimes = publicationConnections(registry, gatewayId)
  const [runtimeId, setRuntimeId] = useState<string | null>(null)
  const selectedRuntime = runtimeId === null
    ? preferredPublicationConnection(apimRuntimes)
    : apimRuntimes.find((runtime) => runtime.id === runtimeId)
  const selectedProvider = registry.providers.find((provider) => provider.id === selectedRuntime?.provider_id)
  const foundry = selectedRuntime?.brand_key === "microsoft_foundry"
  const openAICompatible = selectedProvider?.provider_kind === "openai_compatible"
  const selectedRuntimeUsesOAuth = selectedRuntime?.config.auth_strategy === "oauth_client_credentials"
  const connectionTypeLabel = openAICompatible ? "OpenAI-compatible API" : selectedProvider?.name
  const selectedRuntimeNeedsCredential = selectedRuntime?.config.credential_provisioned === false
    && ["named_value_bearer", "named_value_api_key", "oauth_client_credentials"].includes(
      String(selectedRuntime.config.auth_strategy ?? ""),
    )

  const [providerApiKey, setProviderApiKey] = useState("")
  const [foundryDeployment, setFoundryDeployment] = useState("")
  const [modelOperation, setModelOperation] = useState<"chat" | "image_generation" | "decision">("chat")
  const imageGeneration = foundry && modelOperation === "image_generation"
  const decision = foundry && modelOperation === "decision"
  const imageConfigurationSupported = registry.image_configuration_schema_version === 4
  const [keyRevealed, setKeyRevealed] = useState(false)
  const [modelKey, setModelKey] = useState("")
  const [displayName, setDisplayName] = useState("")
  const [upstreamModelId, setUpstreamModelId] = useState("")
  const [pricing, setPricing] = useState(() => createModelPriceDraft(registry.context_pricing_supported === true, registry.context_price_rules_supported === true))
  const contextWindow = pricing.contextWindow ?? ""
  const setContextWindow = (value: string) => setPricing(current => ({ ...current, contextWindow: value, contextOrigin: "user" }))
  const { inputPrice, outputPrice, cacheReadPrice, cacheWritePrice } = pricing
  useEffect(() => {
    setPricing(current => current.priceSource === "models_dev" ? {
      ...current, priceReference: "", priceEntryDigest: "", allowUnpriced: false,
      inputPrice: "", outputPrice: "", cacheReadPrice: "", cacheWritePrice: "",
      contextWindow: current.contextOrigin === "catalog" ? "" : current.contextWindow,
    } : current)
  }, [runtimeId, foundryDeployment, upstreamModelId, modelKey, modelOperation])
  const [formError, setFormError] = useState<string | null>(null)
  const [publishError, setPublishError] = useState<string | null>(null)
  const [publishing, setPublishing] = useState(false)
  const activationNotified = useRef(false)
  const effectiveModelKey = openAICompatible
    ? publicModelKeyFromProviderId(upstreamModelId)
    : modelKey.trim()
  const effectiveDisplayName = openAICompatible
    ? displayNameFromProviderId(upstreamModelId)
    : displayName.trim()
  const selectedRuntimeVendor = selectedRuntime && openAICompatible
    ? modelVendorFromMetadata(
        selectedRuntime.config,
        `${selectedRuntime.name} ${selectedRuntime.provider_name}`,
      )
    : null
  const connectionEndpoint = selectedRuntime ? publicationConnectionEndpoint(selectedRuntime) : null
  const connectionAuthLabel = {
    managed_identity: "托管身份",
    api_key: "API Key",
    oauth_m2m: "OAuth M2M",
    connection: "由连接管理",
  }[selectedRuntime ? publicationConnectionAuth(selectedRuntime, selectedProvider) : "connection"]
  const priceValues = decision ? [inputPrice, outputPrice] : imageGeneration ? [inputPrice, outputPrice, cacheReadPrice] : [inputPrice, outputPrice, cacheReadPrice, cacheWritePrice]
  const pricingStatus = pricing.contextPricingPreview ? pricing.contextPricingSupported ? "分档价格" : "分档价格预览"
    : pricing.priceSource === "models_dev" ? pricing.priceReference ? "价格已填写" : "价格未填写"
    : pricing.priceSource !== "manual"
    ? pricing.priceReference ? "官方价已选择" : "请选择官方价"
    : priceValues.every((value) => value.trim())
    ? "价格已填写"
    : priceValues.some((value) => value.trim()) ? "价格部分填写" : "价格未填写"
  const requiredCredentialMissing = selectedRuntimeNeedsCredential && !providerApiKey.trim()

  const publication = useQuery(finopsQueries.gatewayPublication(publicationId))
  const removing = publication.data?.publication_kind === "model_remove"
  const programPreview = useQuery(finopsQueries.gatewayProgramUpgradePreview(
    publicationId ? null : gatewayId,
  ))
  const program = programPreview.data
  const registration = program?.registration
  const currentProgram = program?.current_installation
  const programReady = !programPreview.isError
    && Boolean(program?.previous_publication_id && program.previous_installation_sha256)
    && (currentProgram
      ? currentProgram.model_additions_enabled === true
      : registration
      ? registration.installation?.model_additions_enabled === true
        && registration.previous_publication_id === program?.previous_publication_id
        && registration.previous_installation_sha256 === program?.previous_installation_sha256
      : program?.installation?.model_additions_enabled === true)
  const programPending = Boolean(selectedGateway && !publicationId
    && (programPreview.isPending || programPreview.isFetching))
  const installedProgram = currentProgram ?? registration?.installation ?? program?.installation
  const decisionSupported = registry.decision_supported === true
    && installedProgram?.decision_enabled === true
    && installedProgram.supported_profiles?.includes("microsoft-decision") === true
  const programError = selectedGateway && !publicationId && !programPending && !programReady
    ? programPreview.isError ? "无法验证网关发布状态。" : "网关尚未准备好单模型发布。"
    : null
  const oauthCredential = publication.data
    ? publication.data.credential_kind === "oauth_m2m" : selectedRuntimeUsesOAuth
  const credentialLabel = oauthCredential ? "OAuth Client Secret" : "Provider API Key"
  const authorization = publication.data?.authorization

  useEffect(() => {
    if (publication.data?.status !== "active" || activationNotified.current) return
    activationNotified.current = true
    void queryClient.invalidateQueries({ queryKey: finopsKeys.registry })
    if (autoCloseOnSuccess) onClose()
  }, [autoCloseOnSuccess, onClose, publication.data?.status, queryClient])

  const clearConnectionInputs = () => {
    setProviderApiKey("")
    setFoundryDeployment("")
    setModelOperation("chat")
    setKeyRevealed(false)
    setModelKey("")
    setDisplayName("")
    setUpstreamModelId("")
    setContextWindow("")
    setPricing(createModelPriceDraft(registry.context_pricing_supported === true, registry.context_price_rules_supported === true))
    setFormError(null)
    setPublishError(null)
  }

  const chooseGateway = (id: string | null) => {
    if (!id || id === gatewayId || !apimGateways.some((gateway) => gateway.id === id)) return
    const nextConnections = publicationConnections(registry, id)
    setGatewaySelection({ targetId: id, deploymentId: null })
    setRuntimeId(preferredPublicationConnection(nextConnections, runtimeId ?? undefined)?.id ?? "")
    clearConnectionInputs()
  }

  const chooseGatewayApi = (value: string | null) => {
    if (!value || value === selectedGatewayApi?.value) return
    const choice = gatewayChoices.find((item) => item.value === value)
    if (!choice?.selectable || choice.targetId !== gatewayId) return
    clearConnectionInputs()
    setGatewaySelection({ targetId: choice.targetId, deploymentId: choice.deploymentId })
  }

  const chooseRuntime = (id: string | null) => {
    if (!id || id === runtimeId || !selectedGateway) return
    if (!apimRuntimes.some((runtime) => runtime.id === id)) return
    setRuntimeId(id)
    clearConnectionInputs()
  }

  const validate = () => {
    if (topologyBlocked) return topologyError ?? selectedGatewayApi?.reason ?? "请选择可用的 Gateway API。"
    if (!selectedGateway) return "没有可用的 Azure API Management 网关。"
    if (programPending) return "正在验证网关发布状态"
    if (!programReady) return programError ?? "网关尚未准备好单模型发布。"
    if (decision && !decisionSupported) return "所选网关尚未启用 Decision 接口。"
    if (!selectedRuntime || !selectedProvider) return "请选择当前网关下的可用连接。"
    const priceError = validateModelPricing(pricing)
    if (priceError) return {
      prices: "价格必须是非负数字。",
      price_reference: "请选择官方价目中的模型与计价方式。",
      context_pricing_preview: "当前后端尚不支持分档计费，暂不可保存或发布。",
      context_pricing_incomplete: "分档价格或阈值不完整，不能保存。",
      context_price_rule: "阈值须为正整数。",
      context_price_rule_preview: "当前后端尚不支持自定义阈值，暂不可保存或发布。",
      discount: "折扣必须大于 0 且不超过 100%。",
    }[priceError]
    const numericError = optionalNumberError([
      contextWindow, inputPrice, outputPrice, cacheReadPrice, cacheWritePrice,
    ])
    if (numericError) return numericError
    if (requiredCredentialMissing) {
      return selectedRuntimeUsesOAuth ? "请输入该连接首次发布所需的 OAuth Secret。" : "请输入该连接首次发布所需的一次性 API Key。"
    }
    if (foundry) {
      if (!foundryDeployment.trim()) return "请输入已有的 Foundry Deployment Name。"
      if (imageGeneration && registry.image_generation_supported !== true) return "后端尚未启用图像生成"
      if (imageGeneration && !imageConfigurationSupported) return "后端尚未支持图像参数透传"
      if (imageGeneration && priceValues.some(value => !value.trim())) return "请填写文字输入、缓存文字和图像输出单价。"
      return null
    }
    if (!upstreamModelId.trim()) return "请输入上游模型 ID。"
    if (!/^[a-zA-Z0-9._:-]+$/.test(effectiveModelKey) || effectiveModelKey.length > 255) return "模型别名格式无效。"
    if (!effectiveDisplayName) return "请输入显示名称。"
    return null
  }

  const submit = async () => {
    if (publishing) return
    const error = validate()
    setFormError(error)
    setPublishError(null)
    if (error || !selectedGateway || !selectedRuntime || !selectedProvider) return

    const request: GatewayPublicationCreate = {
      gateway_profile_id: selectedGateway.id,
      deployment_scope: topologyAvailable && registry.self_hosted_gateway_enabled === true && selectedGatewayApi?.deploymentId
        ? { mode: "selected", deployment_ids: [selectedGatewayApi.deploymentId] }
        : undefined,
      provider: { existing_id: selectedRuntime.provider_id },
      runtime: {
        existing_id: selectedRuntime.id,
        api_key: selectedRuntimeNeedsCredential && !selectedRuntimeUsesOAuth ? providerApiKey.trim() : undefined,
        oauth_client_secret: selectedRuntimeNeedsCredential && selectedRuntimeUsesOAuth ? providerApiKey.trim() : undefined,
      },
      model: {
        operation: decision ? "decision" : imageGeneration ? "image_generation" : undefined,
        deployment_name: foundry ? foundryDeployment.trim() : undefined,
        model_key: foundry ? undefined : effectiveModelKey,
        display_name: foundry ? undefined : effectiveDisplayName,
        upstream_model_id: foundry ? undefined : upstreamModelId.trim(),
        context_window: imageGeneration ? null : numberOrNull(contextWindow),
        input_cost_per_million: pricing.contextPricingPreview ? null : numberOrNull(inputPrice),
        output_cost_per_million: pricing.contextPricingPreview ? null : numberOrNull(outputPrice),
        cached_cost_per_million: decision || pricing.contextPricingPreview ? null : numberOrNull(cacheReadPrice),
        cache_write_cost_per_million: decision || imageGeneration || pricing.contextPricingPreview ? null : numberOrNull(cacheWritePrice),
        ...(pricing.priceSource === "models_dev" ? {
          price_entry_digest: pricing.priceEntryDigest || null,
          allow_unpriced: pricing.allowUnpriced,
        } : {}),
        ...(pricing.contextPricingPreview ? {
          context_price_references: pricing.contextPriceReferences,
          ...(contextPriceRulePayload(pricing) ? { context_price_rule: contextPriceRulePayload(pricing) } : {}),
        } : {}),
        ...(pricing.priceSource !== "manual" || pricing.discountPercent.trim() ? {
          price_source: pricing.priceSource,
          price_reference: pricing.priceSource === "manual" || pricing.contextPricingPreview ? null : pricing.priceReference.trim() || null,
          price_discount_percent: numberOrNull(pricing.discountPercent),
        } : {}),
      },
    }
    setPublishing(true)
    try {
      const accepted = await dataSource.publishModel(request)
      setProviderApiKey("")
      setKeyRevealed(false)
      queryClient.setQueryData(
        finopsKeys.gatewayPublication(accepted.publication.id),
        accepted.publication,
      )
      onPublicationQueued(accepted.publication.id)
    } catch (requestError) {
      setPublishError(errorMessage(requestError))
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
    const removalModel = removing ? registry.models.find(model => model.model_key === publication.data?.model_key
      && registry.runtimes.some(runtime => runtime.id === model.runtime_id
        && runtime.gateway_profile_id === publication.data?.gateway_profile_id)) : null
    if (removing && !removalModel) {
      setPublishError("移除目标已变化，请刷新模型列表。")
      return
    }
    const retryRequiresCredential = !removing && (publication.data?.retry_requires_credential ?? false)
    if (retryRequiresCredential && !providerApiKey.trim()) return
    setPublishing(true)
    setPublishError(null)
    try {
      const queued = removing && removalModel
        ? (await dataSource.deleteModel(removalModel.id)).publication
        : await dataSource.retryGatewayPublication(
        publicationId,
        retryRequiresCredential ? providerApiKey.trim() : undefined,
        oauthCredential ? "oauth_m2m" : "api_key",
      )
      setProviderApiKey("")
      setKeyRevealed(false)
      queryClient.setQueryData(
        finopsKeys.gatewayPublication(queued.id),
        queued,
      )
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayPublications })
      onPublicationQueued(queued.id)
    } catch (requestError) {
      setPublishError(errorMessage(requestError))
    } finally {
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
      setPublishError(errorMessage(requestError))
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
      setPublishError(errorMessage(error))
    }
  }

  return <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
    <DialogContent className="registry-editor-dialog simple-model-dialog" finalFocus={false}>
      <div className="registry-editor simple-model-form">
        <DialogHeader className="registry-editor-header">
          <DialogTitle>{dialogTitle}</DialogTitle>
          {!publicationId && <DialogDescription>选择已有连接，将模型或部署发布到所选网关。</DialogDescription>}
          {publication.data && <DialogDescription data-no-localize>{`${publication.data.display_name} · ${publication.data.model_key}`}</DialogDescription>}
        </DialogHeader>
        <button type="button" className="registry-editor-close" onClick={onClose} aria-label="关闭"><X size={16} /></button>

        <div className="registry-editor-body simple-model-body">
          {publicationId && publication.isPending && <div className="publication-detail-loading"><LoaderCircle className="spin" size={16} />加载发布详情</div>}
          {publicationId && publication.isError && <div className="publication-detail-error">
            <div className="registry-error">无法加载发布详情：{String(publication.error)}</div>
            <Button type="button" variant="outline" onClick={() => void publication.refetch()}>重新加载</Button>
          </div>}
          {publication.data && <>
            <PublicationTargetDetails publication={publication.data} registry={registry} topology={topology} />
            <PublicationProgress publication={publication.data} />
            {!superseded && <div className={`publication-safety-note ${failed || rolledBack ? "failed" : awaitingAuthorization ? "warning" : active ? "active" : "running"}`}>
              {failed || rolledBack ? <TriangleAlert size={15} /> : awaitingAuthorization ? <ShieldCheck size={15} /> : active ? <Check size={15} /> : <ShieldCheck size={15} />}
              <div>
                <span className="publication-safety-title"><b>{failed ? removing ? "移除未完成" : "发布未完成" : rolledBack ? "已回退" : awaitingAuthorization ? "等待授权" : active ? removing ? "模型已从 Turnstile 移除" : "发布完成" : removing ? "移除尚未完成" : "当前配置仍在生效"}</b>{!failed && !rolledBack && <FieldHelp>{removing ? "历史用量与上游模型保留。" : awaitingAuthorization ? "目标提供方授权就绪后继续验证，验证通过前不启用本次发布。" : active ? "模型已发布，分配人员后即可使用" : "验证通过后启用本次发布。"}</FieldHelp>}</span>
                {failed && <span className="publication-safety-error">{publication.data.failure?.code === "credential_source_missing" ? "APIM 凭据资源尚未创建，请重新提供 API Key 后重试。" : publication.data.failure?.message ?? publication.data.error_message ?? (removing ? "移除未完成，生效状态未确认。" : "发布未完成，请使用发布 ID 查看服务日志。")}</span>}
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

          {failed && !removing && publication.data?.retry_requires_credential && <div className="simple-model-section simple-connection-section">
            <div className="simple-section-title"><b>重新发布</b></div>
            <div className="registry-field"><span className="registry-field-label-row"><label className="registry-field-label" htmlFor="retry-api-key">{credentialLabel}</label><FieldHelp>{oauthCredential ? "请输入新的 Databricks OAuth Secret。" : "请输入新的 API Key 后重新发布同一模型。"}</FieldHelp></span><div className="login-password"><Input id="retry-api-key" type={keyRevealed ? "text" : "password"} autoComplete="off" value={providerApiKey} onChange={(event) => setProviderApiKey(event.target.value)} /><button type="button" className="login-reveal" onClick={() => setKeyRevealed((value) => !value)} aria-label={keyRevealed ? "隐藏凭据" : "显示凭据"} aria-pressed={keyRevealed}>{keyRevealed ? <EyeOff size={15} /> : <Eye size={15} />}</button></div></div>
          </div>}

          {!publicationId && <>
          <div className="simple-model-section publication-target">
            {topologyAvailable ? <div className="form-grid">
            <div className="registry-field">
              <span className="registry-field-label">APIM 实例</span>
              <Select value={selectedGateway?.id ?? null} items={apimGateways.map((gateway) => ({ value: gateway.id, label: gateway.name }))} onValueChange={chooseGateway} disabled={publishing || topologyLoading || Boolean(topologyError)}>
                <SelectTrigger className="registry-select-trigger" aria-label="APIM 实例">
                  <SelectValue>{selectedGateway ? <span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity("Azure API Management")} size={15} /><span data-no-localize>{selectedGateway.name}</span></span> : "选择 APIM 实例"}</SelectValue>
                </SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {apimGateways.map((gateway) => <SelectItem key={gateway.id} value={gateway.id}><span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity("Azure API Management")} size={15} /><span data-no-localize>{gateway.name}</span></span></SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="registry-field">
              <span className="registry-field-label">Gateway</span>
              <Select value={selectedGatewayApi?.value ?? null} items={targetChoices.map((choice) => ({ value: choice.value, label: choice.name }))} onValueChange={chooseGatewayApi} disabled={publishing || topologyLoading || Boolean(topologyError) || !selectedGateway}>
                <SelectTrigger className="registry-select-trigger" aria-label="Gateway API">
                  <SelectValue>{selectedGatewayApi ? <span className="gateway-api-select-value">
                    {selectedGatewayApi.kind === "self_hosted" ? <Server size={15} /> : <ManagedGatewayLogo size={15} />}
                    <span><b data-no-localize>{selectedGatewayApi.name}</b>{selectedGatewayApi.kind === "self_hosted" && <small>Self-hosted Gateway</small>}</span>
                  </span> : "选择 Gateway API"}</SelectValue>
                </SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {targetChoices.map((choice) => <SelectItem key={choice.value} value={choice.value} disabled={!choice.selectable} title={choice.reason ?? choice.endpoint ?? undefined}>
                    <span className="gateway-api-select-option">
                      {choice.kind === "self_hosted" ? <Server size={15} /> : <ManagedGatewayLogo size={15} />}
                      <span><b data-no-localize>{choice.name}</b>{choice.reason ? <small>{choice.reason}</small> : choice.kind === "self_hosted" && <small>Self-hosted Gateway</small>}</span>
                    </span>
                  </SelectItem>)}
                </SelectContent>
              </Select>
              {topologyLoading && <div className="simple-target-state" role="status"><LoaderCircle size={15} className="spin" /><span>加载资源拓扑</span></div>}
              {!topologyLoading && topologyBlocked && <div className="registry-error" role="alert">{topologyError ?? selectedGatewayApi?.reason ?? "请选择可用的 Gateway API。"}</div>}
            </div>
            </div> : apimGateways.length > 1 ? <div className="registry-field">
              <span className="registry-field-label">目标网关</span>
              <Select value={gatewayId} items={apimGateways.map((gateway) => ({ value: gateway.id, label: gateway.name }))} onValueChange={chooseGateway} disabled={publishing}>
                <SelectTrigger className="registry-select-trigger" aria-label="目标网关">
                  <SelectValue>{selectedGateway ? <span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity(`${selectedGateway.name} ${selectedGateway.implementation}`)} size={15} /><span>{selectedGateway.name}</span></span> : "选择目标网关"}</SelectValue>
                </SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {apimGateways.map((gateway) => <SelectItem key={gateway.id} value={gateway.id}><span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity(`${gateway.name} ${gateway.implementation}`)} size={15} /><span>{gateway.name}</span></span></SelectItem>)}
                </SelectContent>
              </Select>
            </div> : selectedGateway ? <div className="publication-target-summary">
              <span className="registry-field-label">目标网关</span>
              <span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity(`${selectedGateway.name} ${selectedGateway.implementation}`)} size={15} /><span data-no-localize>{selectedGateway.name}</span></span>
            </div> : <div className="registry-error" role="alert">没有可用的 Azure API Management 网关。</div>}
            {programPending && <div className="simple-target-state" role="status"><LoaderCircle size={15} className="spin" /><span>正在验证网关发布状态</span></div>}
          </div>

          {selectedGateway && <>
          <section className="simple-model-section" aria-labelledby="publication-connection-heading">
            <div className="publication-section-heading">
              <span className="simple-section-title"><b id="publication-connection-heading">连接</b><FieldHelp>连接包含服务端点和认证方式。切换网关或连接会清空未提交的模型与凭据。</FieldHelp></span>
              <Button type="button" variant="ghost" size="xs" className="publication-connection-action" disabled={publishing} onClick={onManageConnections}>管理连接</Button>
            </div>

            {apimRuntimes.length > 0 ? <div className="registry-field">
              <Select value={selectedRuntime?.id ?? null} items={apimRuntimes.map((runtime) => ({ value: runtime.id, label: runtimeChoiceLabel(runtime) }))} onValueChange={chooseRuntime} disabled={publishing}>
                <SelectTrigger className="registry-select-trigger publication-connection-select" aria-label="连接">
                  <SelectValue>{selectedRuntime ? <span className="registry-option"><ConnectionLogo runtime={selectedRuntime} providers={registry.providers} /><span data-no-localize>{runtimeChoiceLabel(selectedRuntime)}</span></span> : "选择连接"}</SelectValue>
                </SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {apimRuntimes.map((runtime) => <SelectItem key={runtime.id} value={runtime.id}><span className="registry-option"><ConnectionLogo runtime={runtime} providers={registry.providers} /><span data-no-localize>{runtimeChoiceLabel(runtime)}</span></span></SelectItem>)}
                </SelectContent>
              </Select>
              {!selectedRuntime && <div className="registry-error" role="alert">所选连接已不可用，请重新选择。</div>}
            </div> : <p className="publication-form-note" role="status">此网关没有可用连接。请先到连接管理添加并启用连接。</p>}

            {selectedRuntime && selectedProvider && <dl className="publication-connection-summary">
              <div><dt>接入类型</dt><dd className="registry-option"><ConnectionTypeLogo runtime={selectedRuntime} provider={selectedProvider} /><span>{connectionTypeLabel}</span></dd></div>
              <div><dt>认证方式</dt><dd>{connectionAuthLabel}</dd></div>
              {selectedRuntimeVendor && <div><dt>API 服务商</dt><dd className="registry-option"><ModelVendorLogo value={selectedRuntimeVendor} size={15} /><span>{modelVendorLabel(selectedRuntimeVendor)}</span></dd></div>}
              {connectionEndpoint && <div className="publication-connection-endpoint"><dt>Endpoint</dt><dd data-no-localize>{connectionEndpoint}</dd></div>}
            </dl>}

          </section>

          {selectedRuntime && selectedProvider && <>
          <section className="simple-model-section simple-connection-section" aria-labelledby="publication-model-heading">
            <div className="simple-section-title"><b id="publication-model-heading">模型</b></div>
            {foundry ? <div className="simple-model-grid publication-model-identity-grid">
            <div className="registry-field">
              <span className="registry-field-label">模型用途</span>
              <Select value={modelOperation} disabled={publishing} onValueChange={operation => {
                if (operation !== "chat" && operation !== "image_generation" && operation !== "decision") return
                setModelOperation(operation)
                setContextWindow("")
                setPricing(createModelPriceDraft(registry.context_pricing_supported === true, registry.context_price_rules_supported === true))
              }}>
                <SelectTrigger className="registry-select-trigger" aria-label="模型用途"><SelectValue>{decision ? "决策（Decision）" : imageGeneration ? "文生图" : "对话"}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  <SelectItem value="chat">对话</SelectItem>
                  <SelectItem value="image_generation" disabled={registry.image_generation_supported !== true}>文生图</SelectItem>
                  <SelectItem value="decision">决策（Decision）</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="registry-field">
              <span className="registry-field-label-row"><label className="registry-field-label" htmlFor="foundry-deployment">部署名称</label><FieldHelp>填写此 Foundry Project 中已有的部署名称。Turnstile 不会创建上游部署。</FieldHelp></span>
              <Input id="foundry-deployment" value={foundryDeployment} onChange={(event) => setFoundryDeployment(event.target.value)} disabled={publishing} />
            </div>
            </div> : openAICompatible ? <div className="registry-field">
              <span className="registry-field-label-row"><label className="registry-field-label" htmlFor="provider-model-id">上游模型 ID</label><FieldHelp>供应商 API 在 model 字段中要求的真实模型 ID，例如 kimi-k2.6 或 deepseek-chat。Turnstile 会自动生成公开别名和显示名称。</FieldHelp></span>
              <Input id="provider-model-id" value={upstreamModelId} onChange={(event) => setUpstreamModelId(event.target.value)} disabled={publishing} placeholder={selectedRuntimeVendor === "kimi" ? "kimi-k2.6" : selectedRuntimeVendor === "deepseek" ? "deepseek-chat" : "provider-model-id"} />
            </div> : <>
              <label className="registry-field"><span className="registry-field-label">{selectedRuntime.brand_key === "amazon_bedrock" ? "Model / Inference Profile ID" : "上游模型 ID"}</span><Input value={upstreamModelId} onChange={(event) => setUpstreamModelId(event.target.value)} disabled={publishing} placeholder={selectedRuntime.brand_key === "azure_databricks" ? "databricks-claude-sonnet-5" : "au.anthropic.claude-sonnet-4-6"} /></label>
              <div className="form-grid"><label className="registry-field"><span className="registry-field-label">模型别名</span><Input value={modelKey} onChange={(event) => setModelKey(event.target.value)} disabled={publishing} placeholder="claude-sonnet" /></label><label className="registry-field"><span className="registry-field-label">显示名称</span><Input value={displayName} onChange={(event) => setDisplayName(event.target.value)} disabled={publishing} placeholder="Claude Sonnet" /></label></div>
            </>}
          </section>

          {decision && !programPending && !decisionSupported && <div className="registry-error" role="alert">所选网关尚未启用 Decision 接口。</div>}
          {imageGeneration && !imageConfigurationSupported && <div className="registry-error" role="alert">后端尚未支持图像参数透传</div>}
          <section className="simple-model-section simple-pricing" aria-labelledby="publication-pricing-heading">
            <div className="publication-section-heading">
              <span className="simple-section-title"><b id="publication-pricing-heading">价格与限制</b></span>
              <span className="publication-pricing-status"><ModelPriceText>{pricingStatus}</ModelPriceText></span>
            </div>
            <div className="simple-pricing-fields">
              <ModelPriceSourceFields
                key={`${gatewayId}:${selectedRuntime.id}:${modelOperation}`}
                modelName={foundry ? foundryDeployment : effectiveDisplayName || upstreamModelId}
                draft={pricing} setDraft={setPricing} busy={publishing}
                showAnthropic={!decision && !imageGeneration}
                imageGeneration={imageGeneration}
                publicRequest={{ runtime_id: selectedRuntime.id, operation: imageGeneration ? "image_generation" : decision ? "decision" : "chat",
                  deployment_name: foundry ? foundryDeployment : undefined,
                  upstream_model_id: foundry ? undefined : upstreamModelId,
                  model_key: foundry ? undefined : effectiveModelKey,
                  display_name: foundry ? undefined : effectiveDisplayName }}
                connectionDiscount={selectedRuntime.price_discount_percent ?? null} />
              {!imageGeneration && (!decision || pricing.priceSource !== "manual") && <div className="form-grid">
                <label className="registry-field"><span className="registry-field-label">上下文窗口</span><Input id="publication-context-window" type="number" min="1" value={contextWindow} onChange={(event) => setContextWindow(event.target.value)} disabled={publishing} /></label>
              </div>}
              {pricing.priceSource === "manual" && <>
              <div className={decision ? "publication-decision-price-fields" : "form-grid"}>
                <label className="registry-field"><span className="registry-field-label">{imageGeneration ? "文字输入 $/M" : "输入 $/M"}</span><Input id="publication-input-price" type="number" min="0" step="0.000001" value={inputPrice} onChange={(event) => setPricing(current => ({ ...current, inputPrice: event.target.value }))} disabled={publishing} required={imageGeneration} /></label>
                <label className="registry-field"><span className="registry-field-label">{imageGeneration ? "图像输出 $/M" : "输出 $/M"}</span><Input id="publication-output-price" type="number" min="0" step="0.000001" value={outputPrice} onChange={(event) => setPricing(current => ({ ...current, outputPrice: event.target.value }))} disabled={publishing} required={imageGeneration} /></label>
                {decision && <label className="registry-field"><span className="registry-field-label">上下文窗口</span><Input id="publication-context-window" type="number" min="1" value={contextWindow} onChange={(event) => setContextWindow(event.target.value)} disabled={publishing} /></label>}
              </div>
              {!decision && <div className="form-grid">
                <label className="registry-field"><span className="registry-field-label">{imageGeneration ? "缓存文字输入 $/M" : "缓存读取 $/M"}</span><Input id="publication-cache-read-price" type="number" min="0" step="0.000001" value={cacheReadPrice} onChange={(event) => setPricing(current => ({ ...current, cacheReadPrice: event.target.value }))} disabled={publishing} required={imageGeneration} /></label>
                {!imageGeneration && <label className="registry-field"><span className="registry-field-label">缓存写入 $/M</span><Input id="publication-cache-write-price" type="number" min="0" step="0.000001" value={cacheWritePrice} onChange={(event) => setPricing(current => ({ ...current, cacheWritePrice: event.target.value }))} disabled={publishing} /></label>}
              </div>}
              </>}
            </div>
          </section>
          {selectedRuntimeNeedsCredential && <section className="simple-model-section simple-connection-section" aria-labelledby="publication-credential-heading">
            <div className="simple-section-title"><b id="publication-credential-heading">首次发布凭据</b></div>
            <p className="publication-form-note">{selectedRuntimeUsesOAuth ? "仅补充已有连接首次发布所需的 Databricks OAuth Secret。" : "仅补充已有连接首次发布所需的 API Key，不会创建新连接。"}</p>
            <div className="registry-field">
              <span className="registry-field-label-row"><label className="registry-field-label" htmlFor="existing-connection-api-key">{credentialLabel}</label><FieldHelp>{selectedRuntimeUsesOAuth ? "仅首次发布需要。Secret 写入 APIM Credential Manager 并验证后即从临时记录中清除；不是 PAT 或 Entra 应用密钥。" : "仅首次发布需要。Key 写入 APIM Secret Named Value 后即从 Turnstile 临时记录中清除；后续模型无需重复提供。"}</FieldHelp></span>
              <div className="login-password"><Input id="existing-connection-api-key" type={keyRevealed ? "text" : "password"} autoComplete="off" value={providerApiKey} onChange={(event) => setProviderApiKey(event.target.value)} disabled={publishing} /><button type="button" className="login-reveal" onClick={() => setKeyRevealed((value) => !value)} aria-label={keyRevealed ? "隐藏 API Key" : "显示 API Key"} aria-pressed={keyRevealed} disabled={publishing}>{keyRevealed ? <EyeOff size={15} /> : <Eye size={15} />}</button></div>
            </div>
          </section>}
          </>}
          </>}
          </>}

        </div>

        {(formError || publishError || programError) && <PublicationErrorNotice message={formError ?? publishError ?? programError ?? ""} />}

        <DialogFooter className="registry-editor-footer">
          <Button type="button" variant="outline" className="publication-dismiss" onClick={onClose}>{publicationId ? "关闭" : "取消"}</Button>
          {failed && <Button type="button" onClick={() => void retry()} disabled={publishing || Boolean(!removing && publication.data?.retry_requires_credential && !providerApiKey.trim())}>{removing ? <RefreshCw size={14} /> : <Rocket size={14} />}{removing ? "重试移除" : "重新发布"}</Button>}
          {awaitingAuthorization && <Button type="button" onClick={() => void resumeAuthorization()} disabled={publishing}><ShieldCheck size={14} />我已授权，重新验证</Button>}
          {!publicationId && <Button type="button" onClick={() => void submit()} disabled={(decision && !decisionSupported) || publishing || programPending || !programReady || topologyBlocked || requiredCredentialMissing || !selectedGateway || !selectedRuntime || !selectedProvider || (imageGeneration && !imageConfigurationSupported) || (pricing.contextPricingPreview === true && !!validateModelPricing(pricing))}><Rocket size={14} />{publishing ? "正在提交" : "发布模型"}</Button>}
        </DialogFooter>
      </div>
    </DialogContent>
  </Dialog>
}
