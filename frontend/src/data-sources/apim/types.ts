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
  organization_id?: string
  department_id?: string
  project_id?: string
  agent_id?: string
  model_id?: string
  user_id?: string
  runtime?: string[]
  status_code?: number
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
  request_id: string
  correlation_id: string
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
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  latency_ms: number
  status_code: number
  estimated_cost: number
  error_message: string | null
}

export type UsageRequestDetail = UsageRequestSummary & {
  provider: string
  workflow: string
  run_id: string
  turn_index: number
  cached_tokens: number
  cache_write_tokens: number
  ingest_error: string | null
  reconciled_at: string | null
  estimated: boolean
  ingest_source: "eventhub" | "backfill" | "gateway" | "policy"
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
export type BudgetStatus = "unallocated" | "healthy" | "warning" | "exceeded"
export type PeopleBudgetFilter = "all" | "assigned" | BudgetStatus

export type TokenBudgetWrite = {
  token_limit: number
  warning_threshold_percent: number
}

export type TokenBudgetItem = {
  scope_type: BudgetScopeType
  scope_id: string
  scope_name: string
  parent_scope_id: string | null
  token_limit: number | null
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
  warning_threshold_percent: number
  model_ids?: string[]
}

export type TokenBudgetBulkResult = {
  period: string
  department_id: string
  updated_count: number
  token_limit_per_user: number | null
  total_allocated: number | null
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
export type ModelVendorKey = "generic" | "kimi" | "deepseek" | "openai" | "anthropic"
export type ModelFamilyKey = "generic" | "claude" | "openai"

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
  // Percent of list price charged for models on this connection that follow a list price.
  // 90 means a 10% discount. null charges list price.
  price_discount_percent: number | null
  health_status: "unknown" | "available" | "unavailable"
  health_message: string | null
  last_checked_at: string | null
  created_at: string
  updated_at: string
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
  enabled: boolean
  is_default: boolean
  capabilities: Array<"chat" | "tools" | "vision" | "reasoning" | "streaming" | "embeddings" | "image_generation">
  image_profile?: ImageGenerationProfile | null
  context_window: number | null
  input_cost_per_million: number | null
  output_cost_per_million: number | null
  cached_cost_per_million: number | null
  cache_write_cost_per_million: number | null
  allowed_roles: string[]
  // Where the four rates above came from. "manual" means someone typed them and the price sync
  // leaves the row alone, which is what every model did before this existed.
  price_source: PriceSource
  price_reference: string | null
  // Percent of list price, overriding the connection's own figure. null inherits it.
  price_discount_percent: number | null
  // Resolved server-side from the model's figure or the connection's, so the UI never has to
  // work out which one won.
  effective_discount_percent: number | null
  list_input_cost_per_million: number | null
  list_output_cost_per_million: number | null
  list_cached_cost_per_million: number | null
  list_cache_write_cost_per_million: number | null
  price_synced_at: string | null
  price_sync_status: PriceSyncStatus | null
  price_sync_message: string | null
  created_at: string
  updated_at: string
}

export type PriceSource = "manual" | "azure_retail" | "anthropic"
export type PriceSyncStatus = "ok" | "unmapped" | "stale" | "review_needed"

/** One priceable model in a vendor's published list, named the way that vendor names it. */
export type PriceCatalogModel = {
  key: string
  label: string
  product: string
  source: PriceSource
}

export type PriceCatalogModelsResponse = {
  models: PriceCatalogModel[]
  unavailable: string[]
}

/**
 * How one model is priced under one deployment shape. `region_required` is false when the shape
 * charges one figure everywhere -- Global always does -- so there is nothing to pick.
 */
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
  status: PriceSyncStatus
  message: string | null
}

export type PriceSyncResponse = {
  considered: number
  updated: number
  unmapped: number
  review_needed: number
  stale: number
  details: PriceSyncDetail[]
  registry: ModelRegistry
}

export type ModelRegistry = {
  backend_pool_session_affinity_supported?: boolean
  databricks_connections_supported?: boolean
  databricks_oauth_supported?: boolean
  image_generation_supported?: boolean
  image_configuration_defaults?: ImageGenerationLimits | null
  image_configuration_schema_version?: number
  gateways: GatewayProfile[]
  providers: ModelProvider[]
  runtimes: ModelRuntime[]
  models: ManagedModel[]
}

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

export type GatewayPublicationCreate = {
  gateway_profile_id: string
  provider: {
    existing_id?: string
    template?: "amazon_bedrock" | "microsoft_foundry" | "openai_compatible" | "azure_databricks"
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
    operation?: "chat" | "image_generation"
    image_configuration?: ImageGenerationLimits | null
    deployment_name?: string
    model_key?: string
    display_name?: string
    upstream_model_id?: string
    context_window?: number | null
    input_cost_per_million?: number | null
    output_cost_per_million?: number | null
    cached_cost_per_million?: number | null
    cache_write_cost_per_million?: number | null
  }
}

export type GatewayPublication = {
  id: string
  gateway_profile_id: string
  generation: number
  publication_kind: "model_add" | "model_remove" | "credential_rotation" | "route_reconcile"
  model_key: string
  display_name: string
  status: GatewayPublicationStatus
  error_code: string | null
  error_message: string | null
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
  retry_can_authorize_image_probes?: boolean
  attempt_count: number
  created_by: string
  created_at: string
  started_at: string | null
  completed_at: string | null
  updated_at: string
}

export type GatewayPublicationAccepted = {
  publication: GatewayPublication
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
  operation_kind: "rollback" | "integrity_check" | "gc_plan" | "application_sync" | "application_provision"
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
  token_limit: number
  tokens_per_minute: number
  enforce: boolean
  warning_threshold_percent: number
  used_tokens: number
  remaining_tokens: number
  usage_percent: number
  updated_by: string
  updated_at: string
  pending_reserved_tokens?: number | null
  pending_reservation_count?: number | null
  finalized_upper_bound_tokens?: number | null
  finalized_upper_bound_count?: number | null
  stale_reservation_count?: number | null
  oldest_reservation_at?: string | null
  available_tokens?: number | null
  ledger_snapshot_at?: string | null
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

export type GatewayAttributionSource = "apim" | "derived" | "manual"

export type GatewayApplicationSummary = {
  id: string
  gateway_profile_id: string
  slug: string
  display_name: string
  description: string | null
  owner_id: string | null
  owner_source?: GatewayAttributionSource | null
  department_id: string | null
  department_name?: string | null
  department_source?: GatewayAttributionSource | null
  person_group?: string | null
  person_group_size?: number
  application_type: GatewayApplicationType
  status: GatewayApplicationStatus
  system_managed: boolean
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

export type GatewayApplicationDepartmentUpdate = {
  department_id: string | null
}

export type OrgUnitReferences = { budgets: number; usage_records: number; applications: number }

export type OrgUnit = {
  id: string
  unit_type: "organization" | "department"
  parent_id: string | null
  display_name: string
  status: "active" | "retired"
  references: OrgUnitReferences
  updated_by: string
  updated_at: string
}

export type OrganizationDirectory = {
  organization: OrgUnit | null
  departments: OrgUnit[]
}

export type ConsoleMember = {
  email: string
  display_name: string | null
  role: "owner" | "member"
  enabled: boolean
  sign_in: "password" | "microsoft"
  created_at: string
  last_login_at: string | null
  is_self: boolean
}

export type ConsoleMemberList = { members: ConsoleMember[]; owner_count: number }

export type GatewayApplicationAvatar = {
  avatar_url: string | null
  updated_at: string | null
}

export type GatewayApplicationBudgetUpdate = {
  token_limit: number
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
  provisioning_defaults?: { monthly_token_limit: number; tokens_per_minute: number } | null
  key_management_available: boolean
  key_management_unavailable_reason: string | null
}

export type GatewayApplicationSubscriptionCreate = {
  subscription_id: string
  display_name: string
  description?: string | null
  application_type?: "service" | "agent"
}

export type GatewayApplicationSubscriptionProvisionAccepted = GatewayReleaseOperationAccepted & {
  primary_key: string
}

export type RuntimeHealth = {
  runtime_id: string
  status: "unknown" | "available" | "unavailable"
  message: string
  checked_at: string
}

export type ModelInvocationResponse = {
  request_id: string
  correlation_id: string
  content: string
  provider: string
  runtime: string
  model: string
  gateway: "local-cli" | "apim" | "litellm" | "direct"
  latency_ms: number
  usage: {
    input_tokens: number
    cached_tokens: number
    cache_write_tokens: number
    output_tokens: number
    estimated: boolean
  } | null
  estimated_cost: number | null
}

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
  metadata: Record<string, string | number>
  prompt: string
  n: 1
  stream: false
}

export type ImageInvocationResponse = Pick<ModelInvocationResponse,
  "request_id" | "correlation_id" | "provider" | "runtime" | "model" | "gateway" | "latency_ms" | "usage" | "estimated_cost"
> & {
  size: string
  quality: string | null
  output_format: "png" | "jpeg" | "webp"
  data: Array<{ b64_json: string; media_type: "image/png" | "image/jpeg" | "image/webp" }>
}
