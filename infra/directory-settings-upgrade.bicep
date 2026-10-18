targetScope = 'resourceGroup'

@description('Exact existing API Web App name in this resource group.')
param apiName string

@description('Exact existing Telemetry Function App name in this resource group.')
param telemetryFunctionName string

@description('Exact existing Control-plane Function App name in this resource group.')
param controlPlaneFunctionName string

@allowed(['legacy', 'database'])
param directorySource string = 'legacy'

@description('Matching SHA-256 from all three mounted directory-runtime-manifest.json files.')
@minLength(64)
@maxLength(64)
param expectedCoreDigest string

param organizationManagementEnabled bool = false
param synchronizationEnabled bool = false
param identityProjectionEnabled bool = false
param managedIdentityTenantId string = ''

@allowed(['public', 'usgov', 'china'])
param deploymentCloud string = 'public'

@secure()
param currentApiSettings object

@secure()
param currentTelemetrySettings object

@secure()
param currentControlPlaneSettings object

resource api 'Microsoft.Web/sites@2024-11-01' existing = {
  name: apiName
}

resource telemetry 'Microsoft.Web/sites@2024-11-01' existing = {
  name: telemetryFunctionName
}

resource controlPlane 'Microsoft.Web/sites@2024-11-01' existing = {
  name: controlPlaneFunctionName
}

var shared = {
  DIRECTORY_SOURCE: directorySource
  DIRECTORY_EXPECTED_CORE_DIGEST: expectedCoreDigest
  DIRECTORY_PROTOCOL_VERSION: '1'
  DIRECTORY_EMPTY_INITIALIZATION_ALLOWED: 'false'
  DIRECTORY_SYNC_ENABLED: string(synchronizationEnabled)
  DIRECTORY_IDENTITY_PROJECTION_ENABLED: string(identityProjectionEnabled)
  DIRECTORY_MANAGED_IDENTITY_TENANT_ID: managedIdentityTenantId
  DIRECTORY_DEPLOYMENT_CLOUD: deploymentCloud
}

resource apiSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: api
  name: 'appsettings'
  properties: union(currentApiSettings, shared, {
    ORGANIZATION_MANAGEMENT_ENABLED: string(organizationManagementEnabled)
  })
}

resource telemetrySettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: telemetry
  name: 'appsettings'
  properties: union(currentTelemetrySettings, shared)
}

resource controlPlaneSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: controlPlane
  name: 'appsettings'
  properties: union(currentControlPlaneSettings, shared)
}

output apiName string = api.name
output telemetryFunctionName string = telemetry.name
output controlPlaneFunctionName string = controlPlane.name
