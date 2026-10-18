export type MetricTotals = {
  et: number
  total_tokens: number
  input_tokens: number
  cached_tokens: number
  cache_read_tokens?: number
  cache_write_tokens?: number
  output_tokens: number
  calls: number
  estimated_cost: number
  p95_latency_ms: number
  failed_calls: number
}

export type UsageFilters = {
  from: string
  to: string
  organization_id?: string | string[]
  department_id?: string | string[]
  project_id?: string | string[]
  agent_id?: string | string[]
  model_id?: string | string[]
  user_id?: string | string[]
  runtime?: string[]
  status_code?: number
  apim_target_id?: string
  gateway_deployment_id?: string

  directory_status?: UsageDirectoryStatus[]
}

export type ExecutiveTotals = {
  total_tokens: number
  cache_read_tokens?: number
  total_requests: number
  estimated_cost: number
  average_latency_ms: number
  error_rate: number
  // Tail latency and the failure/latency split, aggregated server-side over the whole
  // window. These used to be derived in the browser from a capped 200-row page.
  p50_latency_ms: number
  p95_latency_ms: number
  p99_latency_ms: number
  success_requests: number
  client_error_requests: number
  server_error_requests: number
  latency_under_1s: number
  latency_1_to_2s: number
  latency_2_to_5s: number
  latency_over_5s: number
}

export type ExecutiveOverview = {
  from: string
  to: string
  previous_from: string
  generated_at: string
  totals: ExecutiveTotals
  changes_percent: Record<keyof ExecutiveTotals, number | null>
}

export type DistributionDimension = "organization" | "department" | "project" | "agent" | "model" | "user" | "runtime"

export type DistributionBreakdown = {
  id: string
  name: string
  total_tokens: number
  total_requests: number
}

export type DistributionItem = ExecutiveTotals & {
  id: string
  name: string
  share_percent: number
  // Present only when the request asked for a second dimension via `split_by`.
  breakdown: DistributionBreakdown[]
}

export type DistributionResponse = {
  from: string
  to: string
  dimension: DistributionDimension
  items: DistributionItem[]
}

export type TrendMetric = "et" | "total_tokens" | "calls"
// "none" collapses every row of a bucket into one point. A percentile cannot be merged
// across groups, so a chart of the window's own tail latency has to ask for it ungrouped.
export type TrendDimension = DistributionDimension | "workflow" | "team" | "none"

export type TrendApiResponse = {
  from: string
  to: string
  timezone: string
  interval: "hour" | "day" | "week"
  group_by: TrendDimension
  points: Array<{
    bucket_start: string
    key: string
    label: string
    totals: MetricTotals
  }>
}

export type UsageRequestSummary = {
  /** Caller-visible logical identity; retries may reuse it. */
  request_id: string
  /** Unique APIM attempt identity and exact-detail selector. */
  correlation_id: string | null
  trace_id?: string | null
  usage_available?: boolean
  platform_outcome?: "dispatched" | "succeeded" | "failed" | null
  gateway_status_code?: number | null
  upstream_request_id?: string | null
  timestamp: string
  organization_id: string
  organization_name: string
  department_id: string
  department_name: string
  project_id: string
  project_name: string
  agent_id: string
  agent_name: string
  user_id: string
  user_name: string
  model_id: string
  model_name: string
  request_source: string
  runtime: string
  apim_target_id: string | null
  gateway_deployment_id: string | null
  native_gateway_id: string | null
  native_gateway_instance_id: string | null
  gateway_is_managed: boolean | null
  apim_region: string | null
  api_revision: string | null
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  latency_ms: number
  status_code: number | null
  estimated_cost: number
  cost_available?: boolean | null
  error_message: string | null



  selected_model_id: string | null
  selected_model_name: string | null
}

export type UsageRequestPage = {
  items: UsageRequestSummary[]
  page: { next_cursor: string | null }
}

export type UsageRequestDetail = UsageRequestSummary & {
  provider: string
  workflow: string
  run_id: string
  turn_index: number
  cached_tokens: number
  cache_write_tokens: number
  input_price_per_million?: number | null
  cached_price_per_million?: number | null
  cache_write_price_per_million?: number | null
  output_price_per_million?: number | null
  price_context_tier?: string | null
  price_context_snapshot?: {
    context_tiers?: Pick<StoredContextPriceTier, "key" | "min_input_tokens" | "max_input_tokens" | "input_range_source">[]
  } | null
  ingest_error: string | null
  reconciled_at: string | null
  estimated: boolean
  ingest_source: "eventhub" | "backfill" | "gateway" | "policy" | "platform"
  budget_admission: string | null
  model_admission: string | null

}

export type UsageAnomaly = {
  id: string
  detected_at: string
  rule_id: string
  severity: "info" | "warning" | "critical"
  title: string
  description: string
  dimension: string
  dimension_id: string
  dimension_name: string
  request_id: string | null
  actual_value: number
  threshold_value: number
}

export type AnomalyRuleMetric = "error_rate_percent" | "request_latency_ms" | "request_cost_usd" | "agent_request_count"
export type AnomalyThresholdMode = "absolute" | "percentile"
export type AnomalySeverity = "warning" | "critical"
export type AnomalyScopeType = "global" | "organization" | "department" | "project" | "agent" | "model" | "user"

export type AnomalyRuleWrite = {
  name: string
  description: string
  metric: AnomalyRuleMetric
  threshold_mode: AnomalyThresholdMode
  threshold_value: number
  minimum_sample_size: number
  severity: AnomalySeverity
  scope_type: AnomalyScopeType
  scope_id: string | null
  enabled: boolean
}

export type AnomalyRule = AnomalyRuleWrite & {
  id: string
  created_at: string
  updated_at: string
  updated_by: string
}

export type EnterpriseEntity = { id: string; name: string; parent_id: string | null }
export type EnterpriseEntityCatalog = {
  organizations: EnterpriseEntity[]
  departments: EnterpriseEntity[]
  projects: EnterpriseEntity[]
  agents: EnterpriseEntity[]
  users: EnterpriseEntity[]
  invocation_testers?: EnterpriseEntity[]
}

export type BudgetScopeType = "organization" | "department" | "user"
export type BudgetMode = "tokens" | "usd"
export type BudgetStatus = "unallocated" | "healthy" | "warning" | "exceeded" | "unverified"
export type PeopleBudgetFilter = "all" | "assigned" | BudgetStatus

export type TokenBudgetWrite = {
  token_limit: number | null
  budget_mode?: BudgetMode
  usd_limit?: string | null
  warning_threshold_percent: number
}

export type TokenBudgetItem = {
  scope_type: BudgetScopeType
  scope_id: string
  scope_name: string
  parent_scope_id: string | null
  token_limit: number | null
  budget_mode?: BudgetMode
  usd_limit?: string | null
  used_usd?: string | null
  remaining_usd?: string | null
  forecast_usd?: string | null
  usd_cost_available?: boolean | null
  unpriced_usage_count?: number
  warning_threshold_percent: number
  used_tokens: number
  remaining_tokens: number | null
  usage_percent: number | null
  forecast_tokens: number
  forecast_percent: number | null
  status: BudgetStatus
  updated_at: string | null
  updated_by: string | null
  model_policy_configured: boolean
  allowed_model_ids: string[]
}

export type TokenBudgetAuditEvent = {
  event_type: "budget"
  id: string
  period_start: string
  scope_type: BudgetScopeType
  scope_id: string
  scope_name: string
  action: "assigned" | "updated" | "removed"
  previous_token_limit: number | null
  new_token_limit: number | null
  previous_budget_mode?: BudgetMode | null
  new_budget_mode?: BudgetMode | null
  previous_usd_limit?: string | null
  new_usd_limit?: string | null
  previous_warning_threshold_percent: number | null
  new_warning_threshold_percent: number | null
  changed_at: string
  changed_by: string
}

export type UserModelAccessAuditEvent = {
  event_type: "model_access"
  id: string
  user_id: string
  user_name: string
  previous_model_ids: string[]
  new_model_ids: string[]
  previous_model_names: string[]
  new_model_names: string[]
  changed_at: string
  changed_by: string
}

export type TokenBudgetResponse = {
  period: string
  period_start: string
  period_end: string
  generated_at: string
  items: TokenBudgetItem[]
  risk_count: number
  risk_items: TokenBudgetRiskItem[]
  history: Array<
    TokenBudgetAuditEvent | UserModelAccessAuditEvent | DepartmentEnforcementAuditEvent
  >
  enforcement: DepartmentEnforcement[]

  coverage?: "global" | "department"
}

export type TokenBudgetRiskItem = {
  scope_type: BudgetScopeType
  scope_id: string
  scope_name: string
  status: "warning" | "exceeded"
  usage_percent: number
  forecast_percent: number
}

export type EnforcementMode = "block" | "audit"

export type DepartmentEnforcement = {
  department_id: string
  department_name: string
  mode: EnforcementMode
  updated_at: string | null
  updated_by: string | null
}

export type DepartmentEnforcementAuditEvent = {
  event_type: "enforcement"
  id: string
  department_id: string
  department_name: string
  previous_mode: EnforcementMode | null
  new_mode: EnforcementMode
  changed_at: string
  changed_by: string
}

export type TokenBudgetPeopleResponse = {
  period: string
  department_id: string
  department_name: string
  department_token_limit: number | null
  department_allocated_tokens: number
  department_available_tokens: number | null
  department_usd_limit?: string | null
  department_allocated_usd?: string | null
  department_available_usd?: string | null
  total: number
  offset: number
  limit: number
  assigned_count: number
  unallocated_count: number
  risk_count: number
  model_configured_count: number
  items: TokenBudgetItem[]
}

export type TokenBudgetBulkWrite = {
  department_id: string
  selection: "ids" | "all_matching"
  user_ids: string[]
  query?: string
  status: PeopleBudgetFilter
  allocation_mode: "fixed" | "equal_remaining" | "preserve"
  token_limit?: number
  budget_mode?: BudgetMode
  usd_limit?: string
  warning_threshold_percent: number
  model_ids?: string[]
}

export type TokenBudgetBulkResult = {
  period: string
  department_id: string
  updated_count: number
  token_limit_per_user: number | null
  total_allocated: number | null
  usd_limit_per_user?: string | null
  total_allocated_usd?: string | null
  model_policy_updated_count: number
}

export type TrafficGenerationRequest = {
  model_ids: string[]
  requests_per_model: number
  max_output_tokens: number
  budget_usd: number
  dry_run: boolean
}

export type TrafficGenerationPlan = {
  dry_run: boolean
  request_count: number
  budget_usd: number
  conservative_cost_ceiling: number
  items: Array<{
    sequence: number
    model_id: string
    model_name: string
    department_name: string
    project_name: string
    agent_name: string
    user_name: string
    conservative_cost_ceiling: number
  }>
}

export type TrafficGenerationResult = {
  planned_requests: number
  completed_requests: number
  successful_requests: number
  failed_requests: number
  total_estimated_cost: number
  stopped_reason: string | null
  items: Array<{
    sequence: number
    model_id: string
    request_id: string | null
    correlation_id: string | null
    status_code: number
    estimated_cost: number
    error_message: string | null
  }>
}

export type RunTurn = {
  usage_id: string
  turn_index: number
  ts: string
  model: string
  input_tokens: number
  cached_tokens: number
  output_tokens: number
  et: number
  et_coeff_m: number
  latency_ms: number
  status: string
  estimated: boolean
  ingest_source: "eventhub" | "backfill" | "gateway"
}

export type RunDetail = {
  run_id: string
  started_at: string
  ended_at: string
  team: string
  user: string
  agent: string
  workflow: string
  provider: string
  models: string[]
  turn_count: number
  totals: MetricTotals
  latency_ms: number
  estimated: boolean
  turns: RunTurn[]
}

export type AuditStatus = "new" | "acknowledged" | "resolved" | "ignored"
export type AuditFinding = {
  id: string
  created_at: string
  updated_at: string
  rule_id: string
  severity: "info" | "warning" | "critical"
  workflow: string
  run_id: string | null
  evidence: Record<string, number | string>
  status: AuditStatus
  assignee: string | null
  suggestion: string | null
  resolution_note: string | null
}

export type OptimizationEvent = {
  id: string
  created_at: string
  workflow: string
  occurred_at: string
  label: string
  notes: string | null
  comparison: {
    window_days: number
    before: MetricTotals
    after: MetricTotals
    et_change_percent: number | null
    token_change_percent: number | null
  }
}

export type GatewayKind = "apim" | "litellm" | "direct"
export type RuntimeKind = "foundry" | "openai_compatible"
export type AuthType = "none" | "api_key" | "bearer" | "azure_ad"
export type BrandKey = "generic" | "amazon_bedrock" | "anthropic" | "azure_databricks" | "microsoft" | "microsoft_foundry" | "openai"
export type ModelFamilyKey = "generic" | "claude" | "openai"
export type ModelVendorKey = "generic" | "kimi" | "deepseek" | "openai" | "anthropic"

export type GatewayProfile = {
  id: string
  name: string
  implementation: GatewayKind
  base_url: string | null
  auth_type: AuthType
  enabled: boolean
  is_default: boolean
  config: Record<string, unknown>
  credential_configured: boolean
  credential_hint: string | null
  created_at: string
  updated_at: string
}

export type CapabilitySupport = "unknown" | "supported" | "unsupported"
export type GatewayReleaseGate = "required" | "optional"

export type GatewayPublishedModelSummary = {
  id: string
  model_key: string
  display_name: string
}

export type ApimOnboardingRequest = {
  tenant_id: string
  resource_id: string
  name?: string | null
}

export type ApimOnboardingPlan = {
  request: ApimOnboardingRequest
  gateway_profile_id: string
  service_name: string
  service_tier: string
  location: string
  managed_gateway_origin: string
  api_id: string
  api_path: string
  dashboard_subscription_id: string
  probe_subscription_id: string
  initial_monthly_token_limit: number
  initial_tokens_per_minute: number
  fixed_program_sha256?: string | null
  ready: boolean
  blockers: string[]
  steps: string[]
  confirmation_sha256: string
}

export type ApimOnboardingOperations = {
  available: boolean
  unavailable_reason: string | null
  items: GatewayReleaseOperation[]
}

export type ApimTargetWrite = {
  gateway_profile_id?: string
  name: string
  management_endpoint: string
  tenant_id: string
  subscription_id: string
  resource_group: string
  service_name: string
  api_id: string
  gateway_endpoint: string
  management_auth_profile_ref: "system_managed_identity"
  dashboard_subscription_id: string
  probe_subscription_id: string
}

export type SelfHostedGatewayWrite = {
  native_gateway_id: string
  display_name: string
  endpoint_url: string
  location?: string | null
  hosting_mode: "aks" | "kubernetes" | "arc" | "container_apps" | "docker" | "other"
  release_gate: GatewayReleaseGate
}

export type SelfHostedGatewayCreate = Omit<SelfHostedGatewayWrite, "native_gateway_id"> & {
  endpoint_control_confirmed: boolean
}

export type SelfHostedGatewayBootstrapRequest = {
  endpoint_control_confirmed: boolean
  expires_in_hours: number
  trusted_reverse_proxy: boolean
}

export type SelfHostedGatewayBootstrap = {
  deployment_id: string
  native_gateway_id: string
  configuration_endpoint: string
  expires_at: string
  image: string
  environment_file_name: string
  environment_file: string
  docker_command: string
  renewal_command: string
}

export type SelfHostedGatewayProvisionOperations = {
  available: boolean
  unavailable_reason: string | null
  items: GatewayReleaseOperation[]
}

export type SelfHostedGatewayAssociationChange = SelfHostedGatewayWrite & {
  apim_target_id: string
}

export type SelfHostedGatewayRemovalPreview = {
  deployment_id: string
  display_name: string
  model_count: number
  models: GatewayPublishedModelSummary[]
  removal_allowed: boolean
  blockers: string[]
}

export type ApimTargetRemovalPreview = {
  gateway_profile_id: string
  service_name: string
  self_hosted_gateway_count: number
  runtime_count: number
  model_count: number
  self_hosted_gateways: Array<{
    id: string
    native_gateway_id: string
    display_name: string
  }>
  models: GatewayPublishedModelSummary[]
  removal_allowed: boolean
  blockers: string[]
}

export type ApimTargetTopology = {
  target: {
    schema_version: 1
    gateway_profile_id: string
    arm_resource_id: string
    tenant_id: string
    subscription_id: string
    resource_group: string
    service_name: string
    api_id: string
    api_path: string
    service_tier: string
    tier_family: "classic" | "v2"
    service_location: string
    principal_id: string | null
    ownership_mode: "turnstile_managed" | "adopted" | "read_only"
    capabilities: {
      schema_version: 1
      self_hosted_gateway: CapabilitySupport
      backend_pools: CapabilitySupport
      managed_identity_policy: CapabilitySupport
      event_hub_logger: CapabilitySupport
      response_observer?: CapabilitySupport
      table_ledger: CapabilitySupport
      responses: CapabilitySupport
      anthropic_messages: CapabilitySupport
      source_api_version: string | null
      observed_at: string | null
    }
    last_inventory_sync_at: string | null
    last_validated_at: string | null
    drift_state: "unknown" | "in_sync" | "drifted"
  }
  deployments: Array<{
    deployment: {
      schema_version: 1
      id: string
      apim_target_id: string
      native_gateway_id: string
      kind: "managed" | "self_hosted"
      display_name: string
      location: string | null
      endpoint_url: string | null
      hosting_mode: "azure_managed" | "aks" | "kubernetes" | "arc" | "container_apps" | "docker" | "other"
      lifecycle_ownership: "customer" | "turnstile_iac" | "external"
      release_gate: GatewayReleaseGate
      declared_state: "unknown" | "present" | "absent" | "unsupported"
      reachability_state: "unknown" | "reachable" | "unreachable"
      observation_state: "unknown" | "observed" | "stale"
      runtime_version_support: CapabilitySupport
      rate_limit_synchronization: CapabilitySupport
      last_arm_sync_at: string | null
      last_probe_at: string | null
      last_observed_at: string | null
      retired_at: string | null
      provisioning_operation_id?: string | null
    }
    inventory: {
      deployment_id: string
      associated_api_ids: string[]
      source_api_version: string
      synced_at: string
    } | null
    configuration_endpoint: string
    published_models: GatewayPublishedModelSummary[]
    published_models_available?: boolean
  }>
  management_endpoint: string
  gateway_endpoint: string | null
  configuration_endpoint: string
  connection_state: "unknown" | "connected" | "unavailable"
  self_hosted_gateway_count: number
  published_model_count: number
  published_models: GatewayPublishedModelSummary[]
  published_models_available?: boolean
}

export type ApimTargetTopologyList = {
  items: ApimTargetTopology[]
  total: number
  inventory_sync_available: boolean
  inventory_sync_unavailable_reason: string | null
  resource_lifecycle_available?: boolean
  onboarding_available?: boolean
  self_hosted_gateway_name_edit_supported?: boolean
  self_hosted_gateway_provisioning_available?: boolean
}

export type ModelProvider = {
  id: string
  name: string
  provider_kind: "anthropic" | "microsoft_foundry" | "openai_compatible"
  endpoint_url: string | null
  auth_type: AuthType
  enabled: boolean
  brand_key: BrandKey
  config: Record<string, unknown>
  credential_configured: boolean
  credential_hint: string | null
  created_at: string
  updated_at: string
}

export type ModelRuntime = {
  id: string
  provider_id: string
  provider_name: string
  gateway_profile_id: string | null
  gateway_name: string | null
  name: string
  runtime_kind: RuntimeKind
  enabled: boolean
  is_default: boolean
  brand_key: BrandKey
  config: Record<string, unknown>
  allowed_roles: string[]
  health_status: "unknown" | "available" | "unavailable"
  health_message: string | null
  last_checked_at: string | null
  diagnostic?: RuntimeHealth | null
  created_at: string
  updated_at: string
  price_discount_percent: number | null
}

export type ManagedModel = {
  id: string
  provider_id: string
  provider_name: string
  runtime_id: string
  runtime_name: string
  model_key: string
  display_name: string
  family_key: ModelFamilyKey
  upstream_model_id: string | null
  assignment_required: boolean
  publication_id: string | null
  image_profile?: ImageGenerationProfile | null
  enabled: boolean
  is_default: boolean
  capabilities: Array<"chat" | "tools" | "vision" | "reasoning" | "streaming" | "embeddings" | "image_generation" | "decision">
  context_window: number | null
  input_cost_per_million: number | null
  output_cost_per_million: number | null
  cached_cost_per_million: number | null
  cache_write_cost_per_million: number | null
  allowed_roles: string[]
  created_at: string
  updated_at: string
  price_source: PriceSource
  price_reference: string | null
  price_discount_percent: number | null
  effective_discount_percent: number | null
  list_input_cost_per_million: number | null
  list_output_cost_per_million: number | null
  list_cached_cost_per_million: number | null
  list_cache_write_cost_per_million: number | null
  price_synced_at: string | null
  price_sync_status: PriceSyncStatus | null
  price_sync_message: string | null
  pending_list_price?: PendingListPrice | null
  context_price_references?: string[]
  context_price_rule?: ContextPriceRule | null
  context_price_tiers?: StoredContextPriceTier[]
  pending_context_price_tiers?: StoredContextPriceTier[] | null

  price_source_configured?: boolean
}

export type ModelRegistry = {
    failed_publication_archive_supported?: boolean
  gateways: GatewayProfile[]
  providers: ModelProvider[]
  runtimes: ModelRuntime[]
  models: ManagedModel[]
  gateway_name_edit_supported?: boolean
  runtime_diagnostics_supported?: boolean
  backend_pool_session_affinity_supported?: boolean
  databricks_connections_supported?: boolean
  databricks_oauth_supported?: boolean
  image_generation_supported?: boolean
  decision_supported?: boolean
  image_configuration_defaults?: ImageGenerationLimits | null
  image_configuration_schema_version?: number
  apim_target_topology_available?: boolean
  apim_target_topology?: ApimTargetTopologyList | null
  explicit_gateway_selection_supported?: boolean
  self_hosted_gateway_enabled?: boolean
  multi_apim_target_rollout_enabled?: boolean
  context_pricing_supported?: boolean
  context_price_rules_supported?: boolean
}

export type ModelAvailabilityStatus = "pending" | "projected" | "partial" | "succeeded" | "failed" | "unknown"

export type ModelAvailabilityTarget = {
  target_id: string
  inventory_digest: string
  policy_digest: string | null
  status: "pending" | "verified" | "failed" | "unknown"
  observed_revision: number | null
  observed_enabled: boolean | null
  verified_at: string | null
  reason: string | null
}

export type ModelAvailabilityOperation = {
  id: string
  model_id: string
  idempotency_key: string
  expected_revision: number
  desired_revision: number
  desired_enabled: boolean
  created_by: string
  created_at: string
  updated_at: string
  status: ModelAvailabilityStatus
  targets: ModelAvailabilityTarget[]
  projected_revision: number | null
  projected_at: string | null
  completed_at: string | null
  error_code: string | null
}

export type ModelAvailabilityReadback = {
  model_id: string
  desired_enabled: boolean
  desired_revision: number
  effective_enabled: boolean | null
  effective_revision: number | null
  verified_at: string | null
  status: ModelAvailabilityStatus
  command_ready: boolean
  reason: string | null
  operation: ModelAvailabilityOperation | null
}

export type ModelAvailabilityCommand = {
  enabled: boolean
  expected_revision: number
  idempotency_key: string
}

export type ModelAvailabilityList = { items: ModelAvailabilityReadback[] }
export type ModelAvailabilityAccepted = { operation: ModelAvailabilityOperation; status_url: string }



































export type ModelConnectionCreate = {
  gateway_profile_id: string
  provider: {
    existing_id?: string
    template?: "amazon_bedrock" | "microsoft_foundry" | "openai_compatible" | "azure_databricks"
  }
  auth_mode?: "managed_identity" | "api_key" | "oauth_m2m"
  foundry_project_endpoint?: string
  foundry_inference_endpoint?: string
  bedrock_runtime_url?: string
  openai_base_url?: string
  databricks_workspace_url?: string
  oauth_client_id?: string
  model_vendor?: ModelVendorKey
}

export type ModelConnectionUpdate = {
  name: string
  enabled: boolean
  is_default: boolean
  price_discount_percent: number | null
}

export type GatewayPublicationStatus = "queued" | "validating" | "provisioning" | "building_revision" | "verifying" | "awaiting_authorization" | "promoting" | "active" | "failed" | "superseded" | "rolling_back" | "rolled_back"

export type GatewayDeploymentScope =
  | { mode: "all"; deployment_ids?: [] }
  | { mode: "selected"; deployment_ids: string[] }

export type GatewayPublicationCreate = {
  gateway_profile_id: string
  provider: {
    existing_id?: string
    template?: "amazon_bedrock" | "microsoft_foundry" | "openai_compatible"
  }
  runtime: {
    existing_id?: string
    bedrock_runtime_url?: string
    foundry_project_endpoint?: string
    foundry_inference_endpoint?: string
    openai_base_url?: string
    model_vendor?: ModelVendorKey
    api_key?: string
    oauth_client_secret?: string
  }
  model: {
    operation?: "chat" | "image_generation" | "decision"
    image_configuration?: ImageGenerationLimits
    deployment_name?: string
    model_key?: string
    display_name?: string
    upstream_model_id?: string
    context_window?: number | null
    input_cost_per_million?: number | null
    output_cost_per_million?: number | null
    cached_cost_per_million?: number | null
    cache_write_cost_per_million?: number | null
    price_source?: PriceSource
    price_reference?: string | null
    price_discount_percent?: number | null
    context_price_references?: string[]
    context_price_rule?: ContextPriceRule

  allow_unpriced?: boolean

  price_entry_digest?: string | null
}
  deployment_scope?: GatewayDeploymentScope
}

export type GatewayPublicationFailure = {
  code: "publisher_permissions_missing" | "publisher_permissions_unverified" | "publisher_authorization_failed" | "parent_policy_changed" | "credential_source_missing" | "publication_failed" | "deployment_not_found" | "deployment_metadata_unavailable" | "upstream_request_failed"
  message: string
  stage: GatewayPublicationStatus | null
  request_id: string | null
  http_status: number | null
  missing_actions: string[]
}

export type GatewayPublication = {
  id: string
  configuration_only?: boolean
  gateway_profile_id: string
  target_deployments?: Array<{
    deployment_id: string
    native_gateway_id: string
    kind: "managed" | "self_hosted"
    endpoint_url: string
    release_gate: GatewayReleaseGate
  }> | null
  generation: number
  publication_kind: "model_add" | "model_remove" | "credential_rotation" | "route_reconcile"
  model_key: string
  display_name: string
  status: GatewayPublicationStatus
  error_code: string | null
  error_message: string | null
  failure?: GatewayPublicationFailure | null
  authorization: {
    kind: "azure_rbac"
    principal_id: string
    resource_endpoint: string
    role_id: string
    role_name: string
  } | {
    kind: "databricks_workspace"
    principal_id: string
    resource_endpoint: string
    role_name: "CAN_QUERY"
  } | {
    kind: "databricks_oauth"
    client_id: string
    resource_endpoint: string
    role_name: "CAN_QUERY"
  } | null
  retry_requires_credential: boolean
  credential_kind?: "api_key" | "oauth_m2m" | null
  attempt_count: number
  created_by: string
  created_at: string
  started_at: string | null
  completed_at: string | null
  updated_at: string

  retry_can_authorize_image_probes?: boolean
}

export type GatewayProgramUpgradePreview = {
  previous_publication_id: string
  previous_installation_sha256: string
  installation: { model_additions_enabled?: boolean; native_gateway_scope?: boolean; decision_enabled?: boolean; supported_profiles?: string[] }
  current_installation?: GatewayProgramUpgradePreview["installation"]
  current_installation_sha256?: string
  registration: {
    previous_publication_id: string
    previous_installation_sha256: string
    installation: { model_additions_enabled?: boolean; native_gateway_scope?: boolean; decision_enabled?: boolean; supported_profiles?: string[] }
  } | null
}

export type GatewayPublicationAccepted = {
  publication: GatewayPublication
  status_url: string
}

export type MultiTargetPublicationCreate = {
  children: Array<{
    publication: GatewayPublicationCreate
    required?: boolean
  }>
}

export type TargetRolloutChildStatus = "queued" | "in_progress" | "succeeded" | "failed" | "rolling_back" | "rolled_back"
export type TargetRolloutStatus = "queued" | "in_progress" | "succeeded" | "partial" | "failed" | "rolling_back" | "rolled_back"

export type MultiTargetRollout = {
  schema_version: 1
  id: string
  kind: "model_add" | "model_remove" | "credential_rotation" | "route_reconcile"
  logical_model_id: string | null
  status: TargetRolloutStatus
  children: Array<{
    apim_target_id: string
    publication_id: string
    required: boolean
    status: TargetRolloutChildStatus
  }>
  created_by: string
  created_at: string
}

export type MultiTargetRolloutList = {
  items: MultiTargetRollout[]
}

export type MultiTargetPublicationAccepted = {
  rollout: MultiTargetRollout
  publications: GatewayPublication[]
  status_url: string
}

export type GatewayRateLimitCircuitBreaker = {
  failure_count: 1
  interval_seconds: 60
  trip_duration_seconds: 60
  accept_retry_after: true
  status_code_ranges: Array<{
    minimum: number
    maximum: number
  }>
  error_reasons: Array<"BackendConnectionFailure" | "Timeout">
}

export type GatewayRateLimitResilience = {
  max_attempts_per_request: 2
  retry_interval_seconds: 1
  first_fast_retry: true
  backend_timeout_seconds: 120
  circuit_breaker: GatewayRateLimitCircuitBreaker
}

export type GatewayBackendPoolMember = {
  runtime_id: string
  runtime_name: string
  backend_url: string
  auth_strategy: "none" | "managed_identity" | "named_value_bearer" | "named_value_api_key"
  named_value_name: string | null
  priority: number
  weight: number
}

export type GatewayBackendPoolConfig = {
  schema_version: 1
  members: GatewayBackendPoolMember[]
  rate_limit: GatewayRateLimitResilience
  session_affinity?: boolean
}

export type GatewayBackendPoolWrite = {
  members: Array<Pick<GatewayBackendPoolMember, "runtime_id" | "priority" | "weight">>
  rate_limit: GatewayRateLimitResilience
  session_affinity?: boolean
}

export type GatewayPublicationList = {
  items: GatewayPublication[]
}

export type GatewayReleaseRole = "current" | "immediate_rollback" | "pinned" | "superseded" | "failed" | "expired" | "in_progress" | "rolled_back"
export type GatewayReleaseIntegrityStatus = "not_checked" | "healthy" | "missing" | "mismatched"
export type GatewayReleaseOperationStatus = "queued" | "validating_dependencies" | "preflight_probing" | "promoting" | "verifying_readback" | "post_promotion_probing" | "restoring" | "succeeded" | "failed" | "restored"

export type GatewayReleaseChangeSet = {
  added_models: string[]
  removed_models: string[]
  changed_models: string[]
  added_backend_pools: string[]
  removed_backend_pools: string[]
  changed_backend_pools: string[]
}

export type GatewayReleaseDependencies = {
  apim_revision: string | null
  parent_policy_sha256: string | null
  compiled_policy_sha256: string | null
  route_configuration_sha256?: string | null
  route_head_sha256?: string | null
  backends: string[]
  backend_pools: string[]
  named_values: string[]
  oauth_credentials?: string[]
  recorded_complete: boolean
  live_status: GatewayReleaseIntegrityStatus
  live_checked_at: string | null
  issues: string[]
}

export type GatewayReleaseRetentionPolicy = {
  retained_count: number
  retained_days: number
  failed_retained_days: number
  protected_labels: string[]
}

export type GatewayReleaseProtectionWrite = {
  pinned?: boolean
  protected_label?: string | null
  retain_until?: string | null
}

export type GatewayReleaseProtection = {
  publication_id: string
  pinned: boolean
  protected_label: string | null
  retain_until: string | null
  updated_by: string
  updated_at: string
}

export type GatewayReleaseProtectionAuditEvent = {
  id: string
  publication_id: string
  pinned: boolean
  protected_label: string | null
  retain_until: string | null
  actor: string
  created_at: string
}

export type GatewayReleaseAuditEvent = {
  id: string
  publication_id: string
  from_status: GatewayPublicationStatus | null
  to_status: GatewayPublicationStatus
  actor: string
  detail: Record<string, unknown>
  created_at: string
}

export type GatewayReleaseSummary = {
  id: string
  gateway_profile_id: string
  gateway_name: string
  generation: number
  role: GatewayReleaseRole
  publication_kind: GatewayPublication["publication_kind"]
  model_key: string
  display_name: string
  status: GatewayPublicationStatus
  failure?: GatewayPublicationFailure | null
  apim_revision: string | null
  policy_sha256: string | null
  desired_spec_sha256: string
  change_set: GatewayReleaseChangeSet
  dependency_count: number
  recorded_dependencies_complete: boolean
  live_integrity_status: GatewayReleaseIntegrityStatus
  rollback_eligible: boolean
  rollback_blockers: string[]
  pinned: boolean
  protected_label: string | null
  retain_until: string | null
  retention_eligible: boolean
  retention_reasons: string[]
  created_by: string
  created_at: string
  completed_at: string | null
}

export type GatewayReleaseList = {
  items: GatewayReleaseSummary[]
  retention_policy: GatewayReleaseRetentionPolicy | null
  operations_enabled: boolean
  operations_disabled_reason: string | null
}

export type GatewayReleaseDiff = {
  release_id: string
  against_release_id: string | null
  changes: GatewayReleaseChangeSet
}

export type GatewayReleaseIntegrity = {
  release_id: string
  dependencies: GatewayReleaseDependencies
  rollback_eligible: boolean
  rollback_blockers: string[]
}

export type GatewayReleaseRollbackPreview = {
  target_release_id: string
  current_release_id: string
  changes: GatewayReleaseChangeSet
  dependencies: GatewayReleaseDependencies
  rollback_eligible: boolean
  rollback_blockers: string[]
  confirmation_sha256: string
}

export type GatewayReleaseOperationAuditEvent = {
  id: string
  operation_id: string
  from_status: GatewayReleaseOperationStatus | null
  to_status: GatewayReleaseOperationStatus
  actor: string
  detail: Record<string, unknown>
  created_at: string
}

export type GatewayReleaseGcCandidate = {
  resource_type: string
  resource_id: string
  reasons: string[]
}

export type GatewayReleaseGcPlan = {
  id: string
  operation_id: string
  gateway_profile_id: string
  retained_release_ids: string[]
  current_non_release_references: Record<string, unknown>
  candidates: GatewayReleaseGcCandidate[]
  reference_graph_sha256: string
  created_by: string
  created_at: string
}

export type GatewayReleaseOperation = {
  id: string
  gateway_profile_id: string
  operation_kind: "rollback" | "integrity_check" | "gc_plan" | "application_sync" | "application_provision" | "target_inventory_sync" | "apim_onboarding" | "self_hosted_gateway_provision"
  target_release_id: string | null
  prior_release_id: string | null
  status: GatewayReleaseOperationStatus
  semantic_preview: Record<string, unknown>
  checkpoint: Record<string, unknown>
  error_code: string | null
  error_message: string | null
  attempt_count: number
  created_by: string
  created_at: string
  started_at: string | null
  completed_at: string | null
  updated_at: string
  worker_available: boolean
  worker_unavailable_reason: string | null
  audit: GatewayReleaseOperationAuditEvent[]
  gc_plan: GatewayReleaseGcPlan | null
}

export type GatewayReleaseOperationAccepted = {
  operation: GatewayReleaseOperation
  status_url: string
}

export type GatewayReleaseDetail = GatewayReleaseSummary & {
  base_release_id: string | null
  diff_from_base: GatewayReleaseDiff
  dependencies: GatewayReleaseDependencies
  audit: GatewayReleaseAuditEvent[]
  protection_audit: GatewayReleaseProtectionAuditEvent[]
}

export type GatewayApplicationType = "service" | "agent" | "delegated_user" | "system"
export type GatewayApplicationStatus = "active" | "suspended" | "retired"

export type GatewayApplicationUsage = {
  request_count: number
  denied_request_count: number
  input_tokens: number
  cached_tokens: number
  cache_write_tokens: number
  output_tokens: number
  total_tokens: number
  estimated_cost: number
  last_request_at: string | null
}

export type GatewayApplicationBudget = {
  period_start: string
  token_limit: number | null
  budget_mode?: BudgetMode
  usd_limit?: string | null
  used_usd?: string | null
  remaining_usd?: string | null
  pending_reserved_usd?: string | null
  finalized_upper_bound_usd?: string | null
  usd_cost_available?: boolean | null
  tokens_per_minute: number
  enforce: boolean
  warning_threshold_percent: number
  used_tokens: number
  remaining_tokens: number | null
  usage_percent: number
  pending_reserved_tokens: number | null
  finalized_upper_bound_tokens: number | null
  finalized_upper_bound_count: number | null
  pending_reservation_count: number | null
  stale_reservation_count: number | null
  oldest_reservation_at: string | null
  available_tokens: number | null
  ledger_snapshot_at: string | null
  ledger_source?: "snapshot" | "execution"
  ledger_status?: "snapshot" | "current" | "unavailable" | "prepared"
  ledger_read_at?: string | null
  ledger_version?: number | null
  ledger_balance_tokens?: number | null
  policy_pending?: boolean
  projected_tokens_per_minute?: number | null
  tpm_sync_status?: "projected" | "pending" | "unavailable"
  updated_by: string
  updated_at: string
}

export type GatewayApplicationUserUsage = {
  user_id: string
  display_name: string
  actor_type: "person" | "service" | "system"
  person_id: string | null
  request_count: number
  denied_request_count: number
  total_tokens: number
  estimated_cost: number
  last_request_at: string
}

export type GatewayApplicationSummary = {
  organization_id?: string | null
  organization_name?: string | null
  organization_assignment_supported?: boolean
  department_name?: string | null
  department_assignment_supported?: boolean
  person_id?: string | null
  person_name?: string | null
  person_assignment_supported?: boolean
  id: string
  gateway_profile_id: string
  slug: string
  display_name: string
  description: string | null
  owner_id: string | null
  department_id: string | null
  application_type: GatewayApplicationType
  status: GatewayApplicationStatus
  system_managed: boolean
  tags?: Record<string, string>
  avatar_url?: string | null
  governance_write_available?: boolean
  subscription_count: number
  active_subscription_count: number
  stale_subscription_count: number
  model_policy_configured: boolean
  allowed_model_ids: string[]
  budget: GatewayApplicationBudget | null
  usage: GatewayApplicationUsage
  created_by: string
  created_at: string
  updated_by: string
  updated_at: string
}

export type GatewayApplicationAvatarUpdate = {
  avatar_data_url: string | null
}

export type GatewayApplicationAvatar = {
  avatar_url: string | null
  updated_at: string | null
}

export type GatewayApplicationBudgetUpdate = {
  token_limit: number | null
  budget_mode?: BudgetMode
  usd_limit?: string | null
  tokens_per_minute: number
  enforce: boolean
  warning_threshold_percent: number
}

export type GatewayApplicationModelAccessUpdate = {
  mode: "unrestricted" | "restricted"
  model_ids: string[]
}

export type GatewayApplicationSubscription = {
  id: string
  application_id: string
  gateway_profile_id: string
  apim_subscription_id: string
  display_name: string
  scope_type: "product" | "api" | "service"
  scope_id: string
  state: "active" | "suspended" | "cancelled"
  scope_exists: boolean
  source: "bicep" | "discovered" | "managed"
  discovered_at: string
  last_synced_at: string
}

export type GatewayApplicationSubscriptionKeyKind = "primary" | "secondary"

export type GatewayApplicationSubscriptionKeySecret = {
  key_kind: GatewayApplicationSubscriptionKeyKind
  value: string
}

export type GatewayApplicationAuditEvent = {
  id: string
  application_id: string
  operation: "created" | "adopted" | "updated" | "budget_updated" | "models_updated" | "subscription_synced" | "suspended" | "resumed" | "retired"
  before_state: Record<string, unknown> | null
  after_state: Record<string, unknown> | null
  actor: string
  created_at: string
}

export type GatewayApplicationDetail = GatewayApplicationSummary & {
  subscriptions: GatewayApplicationSubscription[]
  users: GatewayApplicationUserUsage[]
  user_count: number
  audit: GatewayApplicationAuditEvent[]
  key_management_available: boolean
  key_management_unavailable_reason: string | null
}

export type GatewayApplicationList = {
  items: GatewayApplicationSummary[]
  period_start: string
  total: number
  active: number
  suspended: number
  retired: number
  stale_subscriptions: number
  sync_available: boolean
  sync_unavailable_reason: string | null
  provisioning_available: boolean
  provisioning_unavailable_reason: string | null
  provisioning_options_supported?: boolean
  provisioning_defaults?: {
    monthly_token_limit: number
    tokens_per_minute: number
  } | null
  key_management_available: boolean
  key_management_unavailable_reason: string | null
}

export type GatewayApplicationSubscriptionCreate = {
  subscription_id: string
  display_name: string
  description?: string | null
  application_type?: "service" | "agent"
  budget_mode?: BudgetMode
  token_limit?: number | null
  usd_limit?: string | null
  tokens_per_minute?: number
  tags?: Record<string, string>
}

export type GatewayApplicationSubscriptionProvisionAccepted = GatewayReleaseOperationAccepted & {
  primary_key: string
}

export type BudgetDelegationReadiness = {
  gateway_profile_id: string
  native_gateway_id: string
  revision: string
  checked_at: string
  ready: boolean
  protocol_mode?: "prepared" | "paused" | "active"
  verification?: { application_id: string; model_ids: string[] } | null
  checks: Array<{
    case: "valid_identity" | "wrong_audience" | "missing_person" | "current_fenced" | "candidate_fenced"
    expected_status: number
    actual_status: number | null
    native_gateway_id?: string | null
    expected_error_code?: string | null
    actual_error_code?: string | null
  }>
}

export type BudgetExecutionSummary = {
  scope: { scope_type: "person" | "application"; scope_id: string; period_start: string }
  epoch: string
  version: number
  confirmed_tokens: number
  active_reserved_tokens: number
  upper_bound_tokens: number
  updated_at: string
}

export type BudgetExecutionVerification = {
  gateway_profile_id: string
  native_gateway_id: string
  revision: string
  checked_at: string
  request_id: string
  actual_status: number
  invocation: ModelInvocationResponse | null
  settled: boolean
  scopes: Array<{
    before: BudgetExecutionSummary
    after: BudgetExecutionSummary | null
    attempt: { state: string; actual_tokens: number | null; reserved_tokens: number } | null
  }>
}

export type RuntimeCheckSelection = {
  model_id: string
  gateway_deployment_id: string
}

export type RuntimeCheckOption = RuntimeCheckSelection & {
  model_key: string
  model_name: string
  gateway_profile_id: string
  gateway_name: string
  native_gateway_id: string
}

export type RuntimeDiagnosticAccess = {
  status: "ready" | "missing_permissions" | "unverified"
  reason: "permission_missing" | "permission_unverified" | "identity_unavailable" | "tenant_mismatch" | "probe_subscription_unconfigured" | null
  scope: string
  api_principal_id: string | null
  tenant_id: string
  subscription_id: string
  resource_group: string
  apim_name: string
  probe_subscription_id: string
  required_actions: string[]
  missing_actions: string[]
  checked_at: string
  template_path: "infra/modules/runtime-diagnostic-rbac.bicep"
}

export type RuntimeCheckPlan = {
  runtime_id: string
  options: RuntimeCheckOption[]
  unavailable_reason: string | null
  access?: RuntimeDiagnosticAccess | null
  max_output_tokens: 64
  request_limit: 1
}

export type RuntimeHealth = {
  runtime_id: string
  status: "unknown" | "available" | "unavailable"
  message: string
  checked_at: string
  check_kind?: "legacy" | "model_inference"
  outcome?: "passed" | "blocked" | "configuration_error" | "upstream_error" | "inconclusive" | null
  selection?: RuntimeCheckOption | null
  request_id?: string | null
  correlation_id?: string | null
  http_status?: number | null
  latency_ms?: number | null
  error_code?: string | null
  valid_until?: string | null
  configuration_fingerprint?: string | null
  configuration_status?: "current" | "changed" | "unverified"
  configuration_checked_at?: string | null
}

export type ModelInvocationResponse = {
  request_id: string
  correlation_id: string | null
  upstream_request_id?: string | null
  content: string
  provider: string
  runtime: string
  model: string
  gateway: "local-cli" | "apim" | "litellm" | "direct"
  gateway_profile_id?: string | null
  gateway_deployment_id?: string | null
  native_gateway_id?: string | null
  latency_ms: number
  usage: {
    input_tokens: number
    cached_tokens: number
    cache_write_tokens: number
    output_tokens: number
    estimated: boolean
  } | null
  estimated_cost: number | null



  selected_model_id: string | null
  selected_model_name: string | null

}

export type DecisionQuestion =
  | { type: "noul"; instructions: string; criteria?: { true?: string; false?: string } }
  | { type: "choice"; instructions: string; criteria: Record<string, string> }
  | { type: "score"; instructions: string; criteria: string[] }

export type DecisionInvocationRequest = {
  model_id: string
  runtime_id?: string
  gateway_profile_id?: string
  gateway_deployment_id?: string
  metadata: Record<string, string | number>
  state: unknown
  questions: Record<string, DecisionQuestion>
}

export type DecisionInvocationResponse = Pick<ModelInvocationResponse,
  "request_id" | "correlation_id" | "upstream_request_id" | "provider" | "runtime" | "model" | "gateway" | "latency_ms" | "usage" | "estimated_cost" | "gateway_profile_id" | "gateway_deployment_id" | "native_gateway_id"
> & { answers: Record<string, unknown> }

export type ImageGenerationLimits = {
  max_request_bytes: number
  max_response_bytes: number
  output_reservation_tokens: number
  timeout_seconds: number
}

export type ImageGenerationProfile = ImageGenerationLimits & {
  id: string
  version: number
  display_name: string
  provider_model: string
}

export type ImageGenerationOptions = {
  size?: string
  quality?: string
  output_format?: string
}

export type ImageInvocationRequest = ImageGenerationOptions & {
  model_id: string
  runtime_id?: string
  gateway_profile_id?: string
  gateway_deployment_id?: string
  metadata: Record<string, string | number>
  prompt: string
  n: 1
  stream: false
}

export type ImageInvocationResponse = Pick<ModelInvocationResponse,
  "request_id" | "correlation_id" | "provider" | "runtime" | "model" | "gateway" | "latency_ms" | "usage" | "estimated_cost" | "gateway_profile_id" | "gateway_deployment_id" | "native_gateway_id"
> & {
  size: string
  quality: string | null
  output_format: "png" | "jpeg" | "webp"
  data: Array<{ b64_json: string; media_type: "image/png" | "image/jpeg" | "image/webp" }>
}

export type PriceSource = "models_dev" | "manual" | "azure_retail" | "anthropic"

export type PriceSyncStatus = "ok" | "unmapped" | "stale" | "review_needed" | "superseded" | "ambiguous" | "unsupported" | "deferred"

export type PendingListPrice = {
  input: number | null
  output: number | null
  cached: number | null
  cache_write: number | null
}

export type PriceCatalogContextTier = {
  group_key: string
  model_label: string
  service_tier: string
  key: string
  label: string
  order: number
  input_range: { min_input_tokens: number; max_input_tokens: number | null } | null
  input_range_source?: {
    rule_id: string
    url: string | null
    revision: string
    verified_on: string | null
    origin?: "official" | "reference" | "custom"
    configured_at?: string | null
    note?: string | null
  } | null
}

export type ContextPriceRule = {
  short_context_max_tokens: number
  note?: string
  source_url?: string | null
}

export type StoredContextPriceTier = {
  key: string
  label: string
  reference: string
  min_input_tokens: number
  max_input_tokens: number | null
  input_price_per_million: number
  cached_price_per_million: number
  cache_write_price_per_million: number
  output_price_per_million: number
  list_prices: { input: number | null; output: number | null; cached: number | null; cache_write: number | null }
  input_range_source?: PriceCatalogContextTier["input_range_source"]
  model_key: string
  model_label: string
  product: string
  group_key: string
  service_tier: string
  deployment: string
  regions: string[]
}

export type PriceCatalogModel = {
  key: string
  label: string
  product: string
  source: PriceSource
  context_tier?: PriceCatalogContextTier | null
  text_to_image?: boolean
}

export type PriceCatalogModelsResponse = {
  models: PriceCatalogModel[]
  unavailable: string[]
}

export type PriceCatalogOption = {
  reference: string
  deployment: string
  regions: string[]
  // Per-region references. Regions are grouped for display when they charge alike; the choice
  // that gets stored is still the specific region.
  references_by_region?: Record<string, string>
  region_required: boolean
  input_per_million: number | null
  output_per_million: number | null
  cached_per_million: number | null
  cache_write_per_million: number | null
}

export type PriceCatalogOptionsResponse = {
  model_entry: PriceCatalogModel
  options: PriceCatalogOption[]
  complete: boolean
  unreadable: string[]
  other_meters: string[]
  note: string | null
}

export type PriceSyncDetail = {
  model_id: string
  model_key: string
  status: PriceSyncStatus | null
  message: string | null

  outcome: string | null
}

export type PriceSyncResponse = {
  considered: number
  updated: number
  unmapped: number
  review_needed: number
  stale: number
  superseded: number
  details: PriceSyncDetail[]
  registry: ModelRegistry

  partial_fields: number

  deferred: number

  unsupported: number

  ambiguous: number

  skipped_manual: number

  unchanged: number

  total: number
}

export type PricePreviewResponse = {
  status: "matched" | "unmapped" | "ambiguous" | "stale" | "unsupported" | "deferred"
  match: PriceMatch | null
  context_window: number | null
  effective_prices: { input: number | null; output: number | null; cached: number | null; cache_write: number | null } | null
  entry_digest: string | null
  warnings: string[]
  candidates: PriceMatch[]
}

export type PricePreviewRequest = {
  model_id?: string
  runtime_id?: string
  price_source?: PriceSource
  deployment_name?: string
  upstream_model_id?: string
  display_name?: string
  model_key?: string
  operation?: "chat" | "image_generation"
  price_reference?: string | null
  price_discount_percent?: number | null
  refresh?: boolean
  rematch?: boolean
}

export type PriceMatch = {
  reference: string
  provider_id: string
  model_id: string
  name: string
  method: "exact" | "ai" | "manual" | "stored"
  price_basis: "deployment" | "origin_reference"
}

export type UsageQueryEntityCatalog = {
  organizations: UsageQueryEntity[]
  departments: UsageQueryEntity[]
  projects: UsageQueryEntity[]
  agents: UsageQueryEntity[]
  users: UsageQueryEntity[]
  invocation_testers: EnterpriseEntity[]
}

export type UsageQueryEntity = EnterpriseEntity & {
  directory_status: UsageDirectoryStatus
  source: "directory" | "historical"
  parent_ids: string[]
}

export type UsageDirectoryStatus = "active" | "archived" | "historical" | "unattributed"
