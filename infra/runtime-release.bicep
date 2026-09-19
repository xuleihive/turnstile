targetScope = 'resourceGroup'

@description('Existing Turnstile API Web App name.')
param apiName string

@description('Existing Turnstile control-plane Function App name.')
param controlPlaneFunctionName string

@description('Published APIM API URL including the API path.')
param apimGatewayUrl string

@description('Server-side APIM subscription key used only by authenticated dashboard invocations.')
@secure()
param apimSubscriptionKey string

@description('Response observer App Service origin.')
param usageObserverUrl string

@description('Secret APIM Named Value containing the observer authentication key.')
param usageObserverKeyNamedValue string

param publicationWorkerEnabled bool = true
param releaseWorkerEnabled bool = true

@description('Microsoft Entra SPA application (client) id for dashboard sign-in. Empty leaves it off.')
param entraClientId string = ''

@description('Mail domains admitted by Microsoft sign-in. Empty leaves it off.')
param entraAllowedEmailDomains array = []

param applicationKeyManagementEnabled bool = false
param applicationProvisioningEnabled bool = false
param applicationDefaultMonthlyTokenLimit int = 100000
param applicationDefaultTokensPerMinute int = 100000
param databricksOAuthEnabled bool = false

@description('Existing API settings read immediately before deployment so unrelated values are preserved.')
@secure()
param currentApiSettings object

@description('Existing control-plane settings read immediately before deployment so unrelated values are preserved.')
@secure()
param currentControlPlaneSettings object

resource api 'Microsoft.Web/sites@2024-11-01' existing = {
  name: apiName
}

resource controlPlane 'Microsoft.Web/sites@2024-11-01' existing = {
  name: controlPlaneFunctionName
}

resource apiSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: api
  name: 'appsettings'
  // Every setting whose value comes from a deployment parameter belongs here rather than in
  // `data-plane.bicep`: the base template only runs on a first deployment, so anything it owns
  // is frozen at the value the environment was created with. `GATEWAY_RELEASE_WORKER_ENABLED`
  // was already written here; the rest of this list is its siblings, which were not.
  properties: union(currentApiSettings, {
    APIM_GATEWAY_URL: apimGatewayUrl
    APIM_DASHBOARD_SUBSCRIPTION_KEY: apimSubscriptionKey
    GATEWAY_RELEASE_WORKER_ENABLED: string(releaseWorkerEnabled)
    ENTRA_CLIENT_ID: entraClientId
    ENTRA_ALLOWED_EMAIL_DOMAINS: string(entraAllowedEmailDomains)
    GATEWAY_APPLICATION_KEY_MANAGEMENT_ENABLED: string(applicationKeyManagementEnabled)
    GATEWAY_APPLICATION_PROVISIONING_ENABLED: string(applicationProvisioningEnabled)
    GATEWAY_APPLICATION_DEFAULT_MONTHLY_TOKEN_LIMIT: string(applicationDefaultMonthlyTokenLimit)
    GATEWAY_APPLICATION_DEFAULT_TOKENS_PER_MINUTE: string(applicationDefaultTokensPerMinute)
    DATABRICKS_OAUTH_ENABLED: string(databricksOAuthEnabled)
  })
}

resource controlPlaneSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: controlPlane
  name: 'appsettings'
  properties: union(currentControlPlaneSettings, {
    CONTROL_PLANE_ENABLED: 'true'
    GATEWAY_PUBLICATION_WORKER_ENABLED: string(publicationWorkerEnabled)
    GATEWAY_RELEASE_WORKER_ENABLED: string(releaseWorkerEnabled)
    APIM_GATEWAY_URL: apimGatewayUrl
    APIM_USAGE_OBSERVER_URL: usageObserverUrl
    APIM_USAGE_OBSERVER_KEY_NAMED_VALUE: usageObserverKeyNamedValue
  })
}

output apiName string = api.name
output controlPlaneFunctionName string = controlPlane.name
