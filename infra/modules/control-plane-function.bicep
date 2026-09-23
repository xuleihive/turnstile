targetScope = 'resourceGroup'

param location string
param resourcePrefix string
param suffix string
param virtualNetworkName string = 'vnet-turnstile-${suffix}'
param functionSubnetName string = 'snet-flex-control'
param keyVaultName string
param databaseUrlSecretUri string
param apimProbeSubscriptionKeySecretUri string
param credentialEncryptionKeySecretUri string
param applicationInsightsConnectionString string
param apimResourceGroupName string
param apimName string
param apimApiId string = 'turnstile-llm'
param apimProductId string = 'finops-ai-consumers'
param dashboardSubscriptionId string = 'turnstile-dashboard'
param probeSubscriptionId string = 'turnstile-publisher-probe'
param chatCompletionsOperationId string = 'chat-completions'
param responsesOperationId string = 'responses'
param responsesCompactOperationId string = 'responses-compact'
param messagesOperationId string = 'anthropic-messages'
param countTokensOperationId string = 'anthropic-count-tokens'
param modelsOperationId string = 'anthropic-models'
param apimGatewayUrl string
param regressionModelKey string
param usageObserverUrl string = ''
param usageObserverKeyNamedValue string = ''
param subscriptionAgentMap object = {}
param publicationWorkerEnabled bool = false
param releaseWorkerEnabled bool = false
param applicationProvisioningEnabled bool = false
param databricksOAuthEnabled bool = false
@minValue(1)
param applicationDefaultMonthlyTokenLimit int = 100000
@minValue(1)
param applicationDefaultTokensPerMinute int = 100000
param ledgerStorageName string
param ledgerTableName string = 'TurnstileLedger'
param ledgerTableEndpoint string

var storageName = 'stturnstilecp${take(suffix, 11)}'
var planName = 'plan-${resourcePrefix}-control-${suffix}'
var functionName = 'func-${resourcePrefix}-control-${suffix}'
var deploymentContainerName = 'deploy-control-plane'
var observerConfigured = !empty(trim(usageObserverUrl)) && !empty(trim(usageObserverKeyNamedValue))
var effectivePublicationWorkerEnabled = publicationWorkerEnabled && observerConfigured
var effectiveReleaseWorkerEnabled = releaseWorkerEnabled && observerConfigured
var effectiveEnabled = effectivePublicationWorkerEnabled || effectiveReleaseWorkerEnabled

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: {
    SecurityControl: 'Ignore'
  }
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource deploymentBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: deploymentBlobService
  name: deploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: planName
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: virtualNetworkName
}

resource functionSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: virtualNetwork
  name: functionSubnetName
}

resource functionApp 'Microsoft.Web/sites@2024-11-01' = {
  name: functionName
  location: location
  kind: 'functionapp,linux'
  tags: {
    SecurityControl: 'Ignore'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    clientAffinityEnabled: false
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer.name}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 100
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsFeatureFlags', value: 'EnableWorkerIndexing' }
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__blobServiceUri', value: storage.properties.primaryEndpoints.blob }
        { name: 'AzureWebJobsStorage__queueServiceUri', value: storage.properties.primaryEndpoints.queue }
        { name: 'AzureWebJobsStorage__tableServiceUri', value: storage.properties.primaryEndpoints.table }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'DATABASE_URL', value: '@Microsoft.KeyVault(SecretUri=${databaseUrlSecretUri})' }
        { name: 'CREDENTIAL_ENCRYPTION_KEY', value: '@Microsoft.KeyVault(SecretUri=${credentialEncryptionKeySecretUri})' }
        { name: 'DATA_BACKEND', value: 'postgresql' }
        { name: 'PRODUCTION', value: 'true' }
        { name: 'CONTROL_PLANE_ENABLED', value: string(effectiveEnabled) }
        { name: 'GATEWAY_PUBLICATION_WORKER_ENABLED', value: string(effectivePublicationWorkerEnabled) }
        { name: 'GATEWAY_RELEASE_WORKER_ENABLED', value: string(effectiveReleaseWorkerEnabled) }
        { name: 'GATEWAY_APPLICATION_PROVISIONING_ENABLED', value: string(applicationProvisioningEnabled) }
        { name: 'DATABRICKS_OAUTH_ENABLED', value: string(databricksOAuthEnabled) }
        { name: 'GATEWAY_APPLICATION_DEFAULT_MONTHLY_TOKEN_LIMIT', value: string(applicationDefaultMonthlyTokenLimit) }
        { name: 'GATEWAY_APPLICATION_DEFAULT_TOKENS_PER_MINUTE', value: string(applicationDefaultTokensPerMinute) }
        { name: 'LEDGER_TABLE_ENDPOINT', value: ledgerTableEndpoint }
        { name: 'LEDGER_TABLE_NAME', value: ledgerTableName }
        { name: 'APIM_SUBSCRIPTION_AGENT_MAP', value: string(subscriptionAgentMap) }
        { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
        { name: 'APIM_RESOURCE_GROUP', value: apimResourceGroupName }
        { name: 'APIM_SERVICE_NAME', value: apimName }
        { name: 'APIM_API_ID', value: apimApiId }
        { name: 'APIM_PRODUCT_ID', value: apimProductId }
        { name: 'APIM_CHAT_COMPLETIONS_OPERATION_ID', value: chatCompletionsOperationId }
        { name: 'APIM_RESPONSES_OPERATION_ID', value: responsesOperationId }
        { name: 'APIM_RESPONSES_COMPACT_OPERATION_ID', value: responsesCompactOperationId }
        { name: 'APIM_MESSAGES_OPERATION_ID', value: messagesOperationId }
        { name: 'APIM_COUNT_TOKENS_OPERATION_ID', value: countTokensOperationId }
        { name: 'APIM_MODELS_OPERATION_ID', value: modelsOperationId }
        { name: 'APIM_GATEWAY_URL', value: apimGatewayUrl }
        { name: 'APIM_DASHBOARD_SUBSCRIPTION_ID', value: dashboardSubscriptionId }
        { name: 'APIM_PROBE_SUBSCRIPTION_KEY', value: '@Microsoft.KeyVault(SecretUri=${apimProbeSubscriptionKeySecretUri})' }
        { name: 'APIM_PROBE_SUBSCRIPTION_ID', value: probeSubscriptionId }
        { name: 'APIM_REGRESSION_MODEL_KEY', value: regressionModelKey }
        { name: 'APIM_USAGE_OBSERVER_URL', value: usageObserverUrl }
        { name: 'APIM_USAGE_OBSERVER_KEY_NAMED_VALUE', value: usageObserverKeyNamedValue }
        { name: 'CONTROL_PLANE_LEASE_SECONDS', value: '180' }
        { name: 'CONTROL_PLANE_MAX_ATTEMPTS', value: '30' }
        { name: 'CONTROL_PLANE_PARENT_POLICY_PATH', value: 'policies/foundry-finops-policy.xml' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: applicationInsightsConnectionString }
      ]
    }
  }
}

resource functionVnetIntegration 'Microsoft.Web/sites/networkConfig@2024-11-01' = {
  parent: functionApp
  name: 'virtualNetwork'
  properties: {
    subnetResourceId: functionSubnet.id
    swiftSupported: true
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource databaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: keyVault
  name: 'database-url'
}

resource credentialEncryptionKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: keyVault
  name: 'credential-encryption-key'
}

resource apimProbeSubscriptionKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: keyVault
  name: 'apim-probe-subscription-key'
}

resource functionStorageBlobOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, 'storage-blob-data-owner')
  scope: storage
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
  }
}

resource functionStorageQueueContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, 'storage-queue-data-contributor')
  scope: storage
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '974c5e8b-45b9-4653-ba55-5f855dd0fb88')
  }
}

resource functionStorageTableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, 'storage-table-data-contributor')
  scope: storage
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
  }
}

resource ledgerStorage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: ledgerStorageName
}

resource ledgerTableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' existing = {
  parent: ledgerStorage
  name: 'default'
}

resource ledgerTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' existing = {
  parent: ledgerTableService
  name: ledgerTableName
}

resource applicationLedgerContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (applicationProvisioningEnabled) {
  name: guid(ledgerTable.id, functionApp.id, 'application-provisioning-table-contributor')
  scope: ledgerTable
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
  }
}

resource functionDatabaseSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(databaseUrlSecret.id, functionApp.id, 'key-vault-secrets-user')
  scope: databaseUrlSecret
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}

resource functionCredentialSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(credentialEncryptionKeySecret.id, functionApp.id, 'key-vault-secrets-user')
  scope: credentialEncryptionKeySecret
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}

resource functionProbeSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apimProbeSubscriptionKeySecret.id, functionApp.id, 'key-vault-secrets-user')
  scope: apimProbeSubscriptionKeySecret
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}

output functionName string = functionApp.name
output principalId string = functionApp.identity.principalId
output storageName string = storage.name
output appServicePlanName string = plan.name
output deploymentContainerName string = deploymentContainer.name
