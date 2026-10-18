targetScope = 'resourceGroup'

type DisabledUsageObserver = {
  mode: 'disabled'
  url: ''
  keyNamedValue: ''
  legacyFoundryUpstreamHost: ''
  legacyFoundryUpstreamBasePath: ''
  legacyDatabricksUpstreamHost: ''
  legacyDatabricksUpstreamBasePath: ''
}

type EnabledUsageObserver = {
  mode: 'enabled'
  @minLength(1)
  url: string
  @minLength(1)
  keyNamedValue: string
  @minLength(1)
  legacyFoundryUpstreamHost: string
  @minLength(1)
  legacyFoundryUpstreamBasePath: string
  @minLength(1)
  legacyDatabricksUpstreamHost: string
  @minLength(1)
  legacyDatabricksUpstreamBasePath: string
}

param apimName string
param apiId string = 'turnstile-llm'
param apiPath string
param productId string = 'finops-ai-consumers'
param dashboardSubscriptionId string = 'turnstile-dashboard'
param probeSubscriptionId string = 'turnstile-publisher-probe'
param appInsightsLoggerId string = 'finops-appinsights'
param eventHubLoggerId string = 'finops-token-usage-eventhub'
param diagnosticSettingName string = 'finops-llm-token-logs'
param eventHubNamespaceResourceId string
param eventHubNamespaceName string
param eventHubName string
@secure()
param apimSubscriptionKey string
@secure()
param apimProbeSubscriptionKey string
@description('Stable Registry gateway UUID emitted into trusted Event Hub attribution.')
param gatewayProfileId string = '10000000-0000-4000-8000-000000000001'
@minValue(1)
param tokensPerMinute int = 100000

// Azure primaryEndpoints.table includes a trailing slash; the policy appends its own.
var normalizedLedgerTableEndpoint = endsWith(trim(ledgerTableEndpoint), '/')
  ? substring(trim(ledgerTableEndpoint), 0, length(trim(ledgerTableEndpoint)) - 1)
  : trim(ledgerTableEndpoint)

@description('Preserve fixed backend IDs only during an explicitly reviewed legacy-model adoption window.')
param preserveLegacyProviderRouting bool = false

@description('Switch historical fallback routes only after a reconciled revision with managed-routing markers is active.')
param legacyObserverRoutingEnabled bool = false

@description('All-or-nothing transparent usage observer configuration.')
@discriminator('mode')
param usageObserver DisabledUsageObserver | EnabledUsageObserver = {
  mode: 'disabled'
  url: ''
  keyNamedValue: ''
  legacyFoundryUpstreamHost: ''
  legacyFoundryUpstreamBasePath: ''
  legacyDatabricksUpstreamHost: ''
  legacyDatabricksUpstreamBasePath: ''
}

@description('Entra tenant that issues employee tokens for interactive clients such as Claude Desktop.')
param employeeTenantId string

@description('Public client application ID used by employee desktop clients.')
param employeeClientId string

@description('Application ID of the model API that employee tokens are issued for.')
param employeeAudience string

@description('Per-minute burst ceiling for an employee. The monthly allowance lives in the Table Storage ledger because llm-token-limit cannot see cached tokens.')
@minValue(1)
param employeeTokensPerMinute int

@description('Table endpoint of the budget ledger, for example https://stfinopsledger.table.core.windows.net.')
param ledgerTableEndpoint string

@description('Ledger table name.')
param ledgerTableName string = 'TurnstileLedger'

param ledgerTableResourceId string
param routeTableResourceId string
param credentialVaultResourceId string
param platformGovernanceReaderPrincipalIds string[]

@description('Output ceiling reserved when a caller omits max_tokens. OpenAI allows the field to be omitted, which would otherwise leave the reservation unbounded below the real cost.')
@minValue(1)
param defaultMaxOutputTokens int = 4096

@description('Organization ID attributed to employee desktop traffic. Must equal the enterprise catalog organization id so the org/department/user rollup stays consistent.')
param employeeOrgId string

@description('Organization name attributed to employee desktop traffic.')
param employeeOrgName string

@description('Maps FinOps department IDs, emitted as Entra app roles, to their display names.')
param employeeDepartmentMap object

@description('Maps Entra client application IDs to the Agent name attributed to their traffic.')
param employeeClientMap object

@description('Refines the Agent name from the User-Agent, most specific entry first. Every token must be present for an entry to match; no match keeps the client-map name.')
param employeeSurfaceMap array

@description('Maps trusted APIM subscription IDs to Agent identities for subscription-key callers.')
param subscriptionAgentMap object

@description('Application Insights component that receives accurate LLM token metrics.')
param appInsightsName string

@description('Resource group holding the Application Insights component.')
param appInsightsResourceGroupName string

var policy = loadTextContent('../policies/foundry-finops-policy.xml')
var observerEnabled = usageObserver.mode == 'enabled'
var legacyObserverEnabled = preserveLegacyProviderRouting && observerEnabled && legacyObserverRoutingEnabled
var employeeTokenEnabled = !empty(trim(employeeClientId)) && !empty(trim(employeeAudience))
var disabledEmployeeApplicationId = '00000000-0000-0000-0000-000000000000'
var effectiveEmployeeClientId = employeeTokenEnabled ? trim(employeeClientId) : disabledEmployeeApplicationId
var effectiveEmployeeAudience = employeeTokenEnabled ? trim(employeeAudience) : disabledEmployeeApplicationId

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource platformGovernance 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'turnstile-platform-governance'
  properties: {
    displayName: 'turnstile-platform-governance'
    secret: false
    tags: [
      'turnstile-platform-configuration'
    ]
    value: string({
      management_tenant_id: tenant().tenantId
      event_hub_resource_id: '${eventHubNamespaceResourceId}/eventhubs/${eventHubName}'
      ledger_table_resource_id: ledgerTableResourceId
      route_table_resource_id: routeTableResourceId
      credential_vault_resource_id: credentialVaultResourceId
      employee_tenant_id: employeeTenantId
      employee_client_id: effectiveEmployeeClientId
      employee_audience: effectiveEmployeeAudience
      organization_id: employeeOrgId
      organization_name: employeeOrgName
      employee_tokens_per_minute: employeeTokensPerMinute
      default_max_output_tokens: defaultMaxOutputTokens
      department_map: employeeDepartmentMap
      client_map: employeeClientMap
      surface_map: employeeSurfaceMap
    })
  }
}

resource platformGovernanceReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in platformGovernanceReaderPrincipalIds: {
  name: guid(platformGovernance.id, principalId, 'platform-governance-reader')
  scope: platformGovernance
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}]

resource legacyFoundryObserverBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = if (legacyObserverEnabled) {
  parent: apim
  name: 'finops-legacy-foundry-observer'
  properties: {
    title: 'Legacy Foundry via Turnstile usage observer'
    description: 'Temporary observer bridge for the historical Foundry backend.'
    type: 'Single'
    protocol: 'http'
    url: '${trim(usageObserver.url)}${usageObserver.legacyFoundryUpstreamBasePath}'
    credentials: {
      header: {
        'x-adapter-key': [
          '{{${usageObserver.keyNamedValue}}}'
        ]
        'x-turnstile-upstream-host': [
          usageObserver.legacyFoundryUpstreamHost
        ]
      }
    }
    tls: {
      validateCertificateChain: true
      validateCertificateName: true
    }
  }
}

resource legacyDatabricksObserverBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = if (legacyObserverEnabled) {
  parent: apim
  name: 'finops-legacy-databricks-observer'
  properties: {
    title: 'Legacy Databricks via Turnstile usage observer'
    description: 'Temporary observer bridge for the historical Databricks backend.'
    type: 'Single'
    protocol: 'http'
    url: '${trim(usageObserver.url)}${usageObserver.legacyDatabricksUpstreamBasePath}'
    credentials: {
      header: {
        'x-adapter-key': [
          '{{${usageObserver.keyNamedValue}}}'
        ]
        'x-turnstile-upstream-host': [
          usageObserver.legacyDatabricksUpstreamHost
        ]
      }
    }
    tls: {
      validateCertificateChain: true
      validateCertificateName: true
    }
  }
}

var directLegacyProviderRouting = '<choose><when condition="@((bool)context.Variables[&quot;isAnthropicOperation&quot;])"><set-backend-service backend-id="finops-databricks-anthropic" /><authentication-managed-identity resource="2ff814a6-3304-4ab8-85cb-cd0e6f879c1d" /></when><otherwise><set-backend-service backend-id="finops-foundry-openai" /><authentication-managed-identity resource="https://cognitiveservices.azure.com" /></otherwise></choose>'
var observedLegacyProviderRouting = '<choose><when condition="@(!context.Variables.GetValueOrDefault&lt;bool&gt;(&quot;selectedRoutingManaged&quot;, context.Variables.GetValueOrDefault&lt;bool&gt;(&quot;selectedModelKnown&quot;, false)) &amp;&amp; (bool)context.Variables[&quot;isAnthropicOperation&quot;])"><set-variable name="usingLegacyUsageObserver" value="@(true)" /><set-backend-service backend-id="finops-legacy-databricks-observer" /><authentication-managed-identity resource="2ff814a6-3304-4ab8-85cb-cd0e6f879c1d" /></when><when condition="@(!context.Variables.GetValueOrDefault&lt;bool&gt;(&quot;selectedRoutingManaged&quot;, context.Variables.GetValueOrDefault&lt;bool&gt;(&quot;selectedModelKnown&quot;, false)))"><set-variable name="usingLegacyUsageObserver" value="@(true)" /><set-backend-service backend-id="finops-legacy-foundry-observer" /><authentication-managed-identity resource="https://cognitiveservices.azure.com" /></when></choose>'
var legacyProviderRouting = legacyObserverEnabled
  ? observedLegacyProviderRouting
  : directLegacyProviderRouting
var legacyOpenAiIncludeUsage = '<choose><when condition="@(!(bool)context.Variables[&quot;isAnthropicOperation&quot;] &amp;&amp; !context.Variables.GetValueOrDefault&lt;bool&gt;(&quot;isResponsesOperation&quot;, false))"><set-body>@{ var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true); if ((bool?)body[&quot;stream&quot;] ?? false) { var streamOptions = body[&quot;stream_options&quot;] as JObject ?? new JObject(); streamOptions[&quot;include_usage&quot;] = true; body[&quot;stream_options&quot;] = streamOptions; } return body.ToString(Newtonsoft.Json.Formatting.None); }</set-body></when></choose>'
var legacyObserverHeaders = legacyObserverEnabled
  ? '<choose><when condition="@(context.Variables.GetValueOrDefault&lt;bool&gt;(&quot;usingLegacyUsageObserver&quot;, false))"><set-header name="x-turnstile-correlation-id" exists-action="override"><value>@(context.RequestId.ToString())</value></set-header><set-header name="x-request-id" exists-action="override"><value>@((string)context.Variables[&quot;callerRequestId&quot;])</value></set-header><set-header name="x-org-name" exists-action="override"><value>@((string)context.Variables[&quot;telemetryOrganization&quot;])</value></set-header><set-header name="x-org-id" exists-action="override"><value>@((string)context.Variables[&quot;telemetryOrganizationId&quot;])</value></set-header><set-header name="x-department-name" exists-action="override"><value>@((string)context.Variables[&quot;telemetryDepartment&quot;])</value></set-header><set-header name="x-department-id" exists-action="override"><value>@((string)context.Variables[&quot;telemetryDepartmentId&quot;])</value></set-header><set-header name="x-project-name" exists-action="override"><value>@((string)context.Variables[&quot;telemetryProject&quot;])</value></set-header><set-header name="x-project-id" exists-action="override"><value>@((string)context.Variables[&quot;telemetryProjectId&quot;])</value></set-header><set-header name="x-agent-name" exists-action="override"><value>@((string)context.Variables[&quot;telemetryAgent&quot;])</value></set-header><set-header name="x-agent-id" exists-action="override"><value>@((string)context.Variables[&quot;telemetryAgentId&quot;])</value></set-header><set-header name="x-user-name" exists-action="override"><value>@((string)context.Variables[&quot;telemetryUser&quot;])</value></set-header><set-header name="x-user-id" exists-action="override"><value>@((string)context.Variables[&quot;telemetryUserId&quot;])</value></set-header><set-header name="x-hive-workflow" exists-action="override"><value>@((string)context.Variables[&quot;telemetryWorkflow&quot;])</value></set-header><set-header name="x-hive-run-id" exists-action="override"><value>@((string)context.Variables[&quot;telemetryRunId&quot;])</value></set-header><set-header name="x-hive-turn-index" exists-action="override"><value>@((string)context.Variables[&quot;telemetryTurnIndex&quot;])</value></set-header><set-header name="x-provider-name" exists-action="override"><value>@((string)context.Variables[&quot;providerName&quot;])</value></set-header><set-header name="x-hive-model" exists-action="override"><value>@((string)context.Variables[&quot;telemetryModel&quot;])</value></set-header><set-header name="x-model-id" exists-action="override"><value>@((string)context.Variables[&quot;telemetryModelId&quot;])</value></set-header><set-header name="x-hive-runtime" exists-action="override"><value>@((string)context.Variables[&quot;telemetryRuntime&quot;])</value></set-header><set-header name="x-turnstile-api-format" exists-action="override"><value>@((bool)context.Variables[&quot;isAnthropicOperation&quot;] ? &quot;anthropic_messages&quot; : &quot;openai_chat&quot;)</value></set-header><set-header name="x-request-source" exists-action="override"><value>@((string)context.Variables[&quot;telemetryRequestSource&quot;])</value></set-header>${legacyOpenAiIncludeUsage}</when></choose>'
  : ''
var policyWithObserverHeaders = replace(
  policy,
  '__USAGE_OBSERVER_HEADERS__',
  legacyObserverHeaders
)

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
  scope: resourceGroup(appInsightsResourceGroupName)
}

resource metricsLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: appInsightsLoggerId
  properties: {
    loggerType: 'applicationInsights'
    description: 'FinOps accurate LLM token metrics, including streamed responses.'
    isBuffered: true
    resourceId: appInsights.id
    credentials: {
      connectionString: appInsights.properties.ConnectionString
    }
  }
}

resource azureMonitorLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'azuremonitor'
  properties: {
    loggerType: 'azureMonitor'
    description: 'Turnstile gateway request and LLM diagnostics.'
    isBuffered: true
  }
}

resource llmDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: apim
  name: diagnosticSettingName
  properties: {
    workspaceId: appInsights.properties.WorkspaceResourceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'GatewayLlmLogs'
        enabled: true
      }
      // The gateway log is the ONLY channel that carries the caller's request headers, and
      // therefore the only way to tell Claude Desktop's surfaces apart -- Chat, Cowork and
      // Code share one app registration and one OAuth token cache, so the token's azp claim
      // is identical for all three. It also cannot be recovered retroactively: with this
      // category off the table is never written at all.
      {
        category: 'GatewayLogs'
        enabled: true
      }
    ]
  }
}

resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: eventHubLoggerId
  properties: {
    loggerType: 'azureEventHub'
    description: 'FinOps usage metadata only; prompt and completion bodies are prohibited.'
    isBuffered: true
    resourceId: eventHubNamespaceResourceId
    credentials: {
      endpointAddress: '${eventHubNamespaceName}.servicebus.windows.net'
      identityClientId: 'systemAssigned'
      name: eventHubName
    }
  }
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: apiId
  properties: {
    apiType: 'http'
    displayName: '${apiId} | Turnstile AI Gateway'
    description: 'Provider-neutral APIM model access, token controls, and metadata-only usage collection.'
    path: apiPath
    protocols: [
      'https'
    ]
    subscriptionRequired: false
    subscriptionKeyParameterNames: {
      header: 'Ocp-Apim-Subscription-Key'
      query: 'subscription-key'
    }
  }
}

resource product 'Microsoft.ApiManagement/service/products@2024-05-01' = {
  parent: apim
  name: productId
  properties: {
    displayName: '${productId} | Turnstile AI'
    description: 'Product-scoped access to governed model provider integrations.'
    approvalRequired: false
    subscriptionRequired: true
    state: 'published'
    subscriptionsLimit: 1000
  }
}

resource productApi 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = {
  parent: product
  name: api.name
}

resource applicationProduct 'Microsoft.ApiManagement/service/products@2024-05-01' = if (productId != 'turnstile-ai-consumers') {
  parent: apim
  name: 'turnstile-ai-consumers'
  properties: {
    displayName: 'turnstile-ai-consumers | Turnstile AI'
    description: 'Product-scoped access to governed model provider integrations.'
    approvalRequired: false
    subscriptionRequired: true
    state: 'published'
    subscriptionsLimit: 1000
  }
}

resource applicationProductApi 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = if (productId != 'turnstile-ai-consumers') {
  parent: applicationProduct
  name: api.name
}

resource proxyOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'chat-completions'
  properties: {
    displayName: 'OpenAI-compatible chat completions'
    method: 'POST'
    urlTemplate: '/chat/completions'
    templateParameters: []
    request: {
      queryParameters: []
      headers: []
      representations: [
        {
          contentType: 'application/json'
        }
      ]
    }
    responses: []
  }
}

resource imagesGenerationsOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'images-generations'
  properties: {
    displayName: 'Image generations'
    method: 'POST'
    urlTemplate: '/images/generations'
    templateParameters: []
    request: {
      queryParameters: []
      headers: []
      representations: [
        {
          contentType: 'application/json'
        }
      ]
    }
    responses: []
  }
  dependsOn: [
    apiPolicy
  ]
}

resource imagesGenerationsOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = {
  parent: imagesGenerationsOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/provider-neutral-images-policy.xml')
  }
}

resource responsesOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'responses'
  properties: {
    displayName: 'OpenAI Responses API'
    method: 'POST'
    urlTemplate: '/responses'
    templateParameters: []
    request: {
      queryParameters: []
      headers: []
      representations: [
        {
          contentType: 'application/json'
        }
      ]
    }
    responses: []
  }
  dependsOn: [
    apiPolicy
  ]
}

resource responsesCompactOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'responses-compact'
  properties: {
    displayName: 'OpenAI Responses compact'
    method: 'POST'
    urlTemplate: '/responses/compact'
    templateParameters: []
    request: {
      queryParameters: []
      headers: []
      representations: [
        {
          contentType: 'application/json'
        }
      ]
    }
    responses: []
  }
  dependsOn: [
    apiPolicy
  ]
}

resource anthropicMessagesOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'anthropic-messages'
  properties: {
    displayName: 'Anthropic Messages'
    method: 'POST'
    urlTemplate: '/v1/messages'
    templateParameters: []
    request: {
      queryParameters: []
      headers: []
      representations: [
        {
          contentType: 'application/json'
        }
      ]
    }
    responses: []
  }
}

// The Anthropic SDK calls this before a turn to size the context window. Without the operation
// APIM answered 404, which was 47% of all gateway traffic on a measured agent session -- and
// worse, an unmatched request leaves context.Operation.Id empty, so the policy's provider test
// fell through to microsoft_foundry and the dashboard blamed Foundry for Databricks-bound
// traffic. It is deliberately NOT part of isInferenceOperation: it burns no model tokens, so it
// takes no quota, emits no token metric and writes no usage row, the same treatment /v1/models
// gets.
resource anthropicCountTokensOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'anthropic-count-tokens'
  properties: {
    displayName: 'Anthropic Count Tokens'
    method: 'POST'
    urlTemplate: '/v1/messages/count_tokens'
    templateParameters: []
    request: {
      queryParameters: []
      headers: []
      representations: [
        {
          contentType: 'application/json'
        }
      ]
    }
    responses: []
  }
}

resource anthropicModelsOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {  parent: api
  name: 'anthropic-models'
  properties: {
    displayName: 'Anthropic Models'
    method: 'GET'
    urlTemplate: '/v1/models'
    templateParameters: []
    request: {
      queryParameters: []
      headers: []
      representations: []
    }
    responses: []
  }
}

resource anthropicModelsOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = if (!preserveLegacyProviderRouting) {
  parent: anthropicModelsOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/foundry-finops-models-policy.xml')
  }
  dependsOn: [
    apiPolicy
  ]
}

resource chatCompletionsOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = if (!preserveLegacyProviderRouting) {
  parent: proxyOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/provider-neutral-chat-policy.xml')
  }
  dependsOn: [
    apiPolicy
  ]
}

resource responsesOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = if (!preserveLegacyProviderRouting) {
  parent: responsesOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/provider-neutral-responses-policy.xml')
  }
  dependsOn: [
    apiPolicy
  ]
}

resource responsesCompactOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = if (!preserveLegacyProviderRouting) {
  parent: responsesCompactOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/provider-neutral-responses-policy.xml')
  }
  dependsOn: [
    apiPolicy
  ]
}

resource anthropicMessagesOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = if (!preserveLegacyProviderRouting) {
  parent: anthropicMessagesOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/provider-neutral-messages-policy.xml')
  }
  dependsOn: [
    apiPolicy
  ]
}

resource anthropicCountTokensOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = if (!preserveLegacyProviderRouting) {
  parent: anthropicCountTokensOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/provider-neutral-count-tokens-policy.xml')
  }
  dependsOn: [
    apiPolicy
  ]
}

resource apiMetricsDiagnostic 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: api
  name: 'applicationinsights'
  properties: {
    loggerId: metricsLogger.id
    alwaysLog: 'allErrors'
    metrics: true
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    verbosity: 'error'
  }
}

resource apiAzureMonitorDiagnostic 'Microsoft.ApiManagement/service/apis/diagnostics@2024-06-01-preview' = {
  parent: api
  name: 'azuremonitor'
  properties: {
    loggerId: azureMonitorLogger.id
    alwaysLog: 'allErrors'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    verbosity: 'information'
    largeLanguageModel: {
      logs: 'enabled'
    }
    // Enabling the GatewayLogs category is not enough on its own: RequestHeaders stays empty
    // until a header is explicitly whitelisted here. This is an allow-list of exactly one
    // header -- no request or response body is ever logged, so prompts stay out of the log
    // exactly as they stay out of the Event Hub payload.
    frontend: {
      request: {
        headers: [
          'User-Agent'
        ]
      }
    }
  }
}

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(
      replace(
      replace(
      replace(
      replace(
      replace(
        replace(
          replace(
            replace(
              replace(
                replace(
                  replace(
                    replace(
                      replace(
                        replace(
                          replace(
                            replace(
                              replace(
                                policyWithObserverHeaders,
                                '__LEGACY_PROVIDER_ROUTING__',
                                preserveLegacyProviderRouting ? legacyProviderRouting : ''
                              ),
                              '__EMPLOYEE_TOKEN_ENABLED__',
                              employeeTokenEnabled ? 'true' : 'false'
                            ),
                            '__LOGGER_ID__',
                            logger.name
                          ),
                          '__TOKENS_PER_MINUTE__',
                          string(tokensPerMinute)
                        ),
                        '__EMPLOYEE_TENANT_ID__',
                        employeeTenantId
                      ),
                      '__EMPLOYEE_CLIENT_ID__',
                      effectiveEmployeeClientId
                    ),
                    '__EMPLOYEE_AUDIENCE__',
                    effectiveEmployeeAudience
                  ),
                  '__EMPLOYEE_TOKENS_PER_MINUTE__',
                  string(employeeTokensPerMinute)
                ),
                '__LEDGER_TABLE_ENDPOINT__',
                normalizedLedgerTableEndpoint
              ),
              '__LEDGER_TABLE_NAME__',
              ledgerTableName
            ),
            '__EMPLOYEE_ORG_ID__',
            employeeOrgId
          ),
          '__EMPLOYEE_ORG_NAME__',
          employeeOrgName
        ),
        '__EMPLOYEE_DEPARTMENT_MAP__',
        replace(string(employeeDepartmentMap), '"', '\\&quot;')
      ),
      '__EMPLOYEE_CLIENT_MAP__',
      replace(string(employeeClientMap), '"', '\\&quot;')
      ),
      '__DEFAULT_MAX_OUTPUT_TOKENS__',
      string(defaultMaxOutputTokens)
      ),
      '__EMPLOYEE_SURFACE_MAP__',
      replace(string(employeeSurfaceMap), '"', '\\&quot;')
      ),
      '__SUBSCRIPTION_AGENT_MAP__',
      replace(string(subscriptionAgentMap), '"', '\\&quot;')
      ),
      '__GATEWAY_PROFILE_ID__',
      gatewayProfileId
    )
  }
  dependsOn: [
    proxyOperation
    anthropicMessagesOperation
  ]
}
resource subscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: dashboardSubscriptionId
  properties: {
    displayName: 'Turnstile Dashboard'
    scope: product.id
    state: 'active'
    allowTracing: false
    primaryKey: apimSubscriptionKey
  }
  dependsOn: [
    productApi
  ]
}


resource probeSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: probeSubscriptionId
  properties: {
    displayName: 'Turnstile Publisher Probe'
    scope: product.id
    state: 'active'
    allowTracing: false
    primaryKey: apimProbeSubscriptionKey
  }
  dependsOn: [
    productApi
  ]
}

output gatewayUrl string = apim.properties.gatewayUrl
output gatewayApiPath string = '${apim.properties.gatewayUrl}/${api.properties.path}'
