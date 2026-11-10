import type {
  ManagedModel,
  ModelRegistry,
  ModelRuntime,
} from "../../data-sources/apim/types"

function configValue(runtime: ModelRuntime, key: string, fallback: unknown = null) {
  return runtime.config[key] ?? fallback
}

function apiFormat(runtime: ModelRuntime) {
  const configured = configValue(runtime, "api_format", "")
  return configured === "anthropic_messages"
    || runtime.brand_key === "amazon_bedrock"
    || runtime.brand_key === "azure_databricks"
    ? "anthropic_messages"
    : "openai_chat"
}

function authCompatible(runtime: ModelRuntime, primary: ModelRuntime) {
  const primaryAuth = String(configValue(primary, "auth_strategy", "none"))
  const runtimeAuth = String(configValue(runtime, "auth_strategy", "none"))
  const namedAuth = runtimeAuth === "named_value_api_key"
    || runtimeAuth === "named_value_bearer"
  if (namedAuth && (
    !configValue(runtime, "named_value_name")
    || configValue(runtime, "credential_provisioned", false) !== true
  )) return false
  if (primaryAuth === "managed_identity") {
    return runtimeAuth === "named_value_api_key"
      || (runtimeAuth === "managed_identity"
        && configValue(runtime, "managed_identity_resource")
          === configValue(primary, "managed_identity_resource"))
  }
  return runtimeAuth === primaryAuth
}

function runtimeCompatibilityIssue(runtime: ModelRuntime, primary: ModelRuntime) {
  if (!runtime.enabled) return "连接已停用"
  if (runtime.provider_id !== primary.provider_id) return "提供方不一致"
  if (runtime.gateway_profile_id !== primary.gateway_profile_id) return "Gateway 不一致"
  if (runtime.config.control_plane_managed !== true
    || primary.config.control_plane_managed !== true) return "连接未受管"
  if (!runtime.config.backend_url || !runtime.config.backend_path
    || !primary.config.backend_url || !primary.config.backend_path) return "后端地址或路径缺失"
  if (apiFormat(runtime) !== apiFormat(primary)) return "API 协议不一致"
  if (configValue(runtime, "backend_path") !== configValue(primary, "backend_path")) return "后端路径不一致"
  if (!authCompatible(runtime, primary)) return "认证配置不兼容"
  if (configValue(runtime, "streaming_mode", "native")
    !== configValue(primary, "streaming_mode", "native")) return "流式模式不一致"
  return null
}

export function nativeRouteManagedModels(registry: ModelRegistry) {
  return registry.models.filter(model => model.enabled
    && !model.capabilities.includes("image_generation") && !model.capabilities.includes("decision")
    && registry.runtimes.some(runtime => runtime.id === model.runtime_id
      && runtime.enabled && runtime.config.control_plane_managed === true
      && registry.gateways.some(gateway => gateway.id === runtime.gateway_profile_id
        && gateway.implementation === "apim")))
}

export function nativeRouteUpstreamIdentity(model: ManagedModel) {
  return (model.upstream_model_id ?? model.model_key).trim().toLocaleLowerCase()
}

export function compatibleNativeRouteRuntimes(
  model: ManagedModel,
  registry: ModelRegistry,
) {
  if (model.capabilities.includes("image_generation")) return []
  const primary = registry.runtimes.find((runtime) => runtime.id === model.runtime_id)
  if (!primary) return []
  return registry.runtimes.filter((runtime) => runtimeCompatibilityIssue(runtime, primary) === null)
}

export function nativeRouteRuntimeIssue(
  model: ManagedModel,
  runtime: ModelRuntime,
  registry: ModelRegistry,
) {
  if (model.capabilities.includes("image_generation")) return "此模型不支持 APIM 后端池"
  const primary = registry.runtimes.find((candidate) => candidate.id === model.runtime_id)
  if (!primary) return "主部署连接不可用"
  const issue = runtimeCompatibilityIssue(runtime, primary)
  if (issue) return issue
  const upstream = nativeRouteUpstreamIdentity(model)
  return registry.models.some((candidate) => candidate.enabled
    && candidate.provider_id === model.provider_id
    && candidate.runtime_id === runtime.id
    && nativeRouteUpstreamIdentity(candidate) === upstream)
    ? null : "缺少同名 Deployment"
}

export function equivalentNativeRouteRuntimes(
  model: ManagedModel,
  registry: ModelRegistry,
) {
  const primary = registry.runtimes.find((runtime) => runtime.id === model.runtime_id)
  if (!primary) return []
  const upstream = nativeRouteUpstreamIdentity(model)
  const runtimeIdsWithDeployment = new Set(
    registry.models
      .filter((candidate) =>
        candidate.enabled
        && candidate.provider_id === model.provider_id
        && nativeRouteUpstreamIdentity(candidate) === upstream
      )
      .map((candidate) => candidate.runtime_id),
  )
  return compatibleNativeRouteRuntimes(model, registry).filter((runtime) =>
    runtimeIdsWithDeployment.has(runtime.id)
  )
}

export function hasNativeRouteCounterpart(
  model: ManagedModel,
  registry: ModelRegistry,
) {
  return equivalentNativeRouteRuntimes(model, registry)
    .some((runtime) => runtime.id !== model.runtime_id)
}

export function previewNativeRouteDeployments(model: ManagedModel, registry: ModelRegistry) {
  if (!model.enabled || !model.capabilities.includes("chat")
    || model.capabilities.some(capability => capability === "image_generation" || capability === "decision")) return []
  const primary = registry.runtimes.find(runtime => runtime.id === model.runtime_id)
  if (!primary) return []
  const upstream = nativeRouteUpstreamIdentity(model)
  const numberedReplica = (base: string, candidate: string) => candidate.startsWith(`${base}-`)
    && /^[0-9]+$/.test(candidate.slice(base.length + 1))
  const seen = new Set<string>()
  return registry.models.filter(candidate => {
    if (!candidate.enabled || candidate.provider_id !== model.provider_id
      || (candidate.family_key ?? "generic") !== (model.family_key ?? "generic")
      || !model.capabilities.every(capability => candidate.capabilities.includes(capability))
      || candidate.capabilities.some(capability => capability === "image_generation" || capability === "decision")) return false
    const runtime = registry.runtimes.find(item => item.id === candidate.runtime_id)
    if (!runtime || runtimeCompatibilityIssue(runtime, primary) !== null) return false
    const deployment = nativeRouteUpstreamIdentity(candidate)
    if (deployment !== upstream && (primary.brand_key !== "microsoft_foundry"
      || runtime.brand_key !== "microsoft_foundry"
      || !numberedReplica(upstream, deployment) && !numberedReplica(deployment, upstream))) return false
    const identity = `${candidate.runtime_id}:${deployment}`
    if (seen.has(identity)) return false
    seen.add(identity)
    return true
  })
}
