targetScope = 'resourceGroup'

param location string
param postgresLocation string
param resourcePrefix string
param suffix string
param postgresAdministratorLogin string
@secure()
param postgresAdministratorPassword string
param postgresSkuName string
param postgresTier string
param provisionPostgres bool = true
@secure()
param credentialEncryptionKey string
@secure()
param managementApiKey string
@secure()
param apimSubscriptionKey string
@secure()
param apimProbeSubscriptionKey string
param apimPrincipalId string
param apimResourceGroupName string
param apimName string
param apimApiId string = 'turnstile-llm'
param apimProductId string = 'finops-ai-consumers'
param dashboardSubscriptionId string = 'turnstile-dashboard'
param probeSubscriptionId string = 'turnstile-publisher-probe'
param apimGatewayUrl string
param ledgerTableName string
param gatewayReleaseWorkerEnabled bool = false
param gatewayApplicationKeyManagementEnabled bool = false
param gatewayApplicationProvisioningEnabled bool = false
param databricksOAuthEnabled bool = false
@minValue(1)
param gatewayApplicationDefaultMonthlyTokenLimit int = 100000
@minValue(1)
param gatewayApplicationDefaultTokensPerMinute int = 100000
param entraClientId string = ''
param entraAllowedEmailDomains array = []
param bootstrapOwnerEmail string
@secure()
param bootstrapOwnerPasswordHash string

var storageName = 'st${resourcePrefix}${take(suffix, 10)}'
var ledgerStorageName = 'st${resourcePrefix}ledger${take(suffix, 4)}'
var postgresServerName = 'pg-${resourcePrefix}-${suffix}'
var databaseName = 'turnstile'
var eventHubNamespaceName = 'eh-${resourcePrefix}-${suffix}'
var eventHubName = 'token-usage'
var workspaceName = 'log-${resourcePrefix}-${suffix}'
var appInsightsName = 'appi-${resourcePrefix}-${suffix}'
var keyVaultName = 'kv-${resourcePrefix}-${take(suffix, 8)}'
var planName = 'plan-${resourcePrefix}-${suffix}'
var telemetryPlanName = 'plan-${resourcePrefix}-telemetry-${suffix}'
var apiName = 'api-${resourcePrefix}-${suffix}'
var functionName = 'func-${resourcePrefix}-telemetry-${suffix}'
var virtualNetworkName = 'vnet-${resourcePrefix}-${suffix}'
var telemetryFunctionSubnetName = 'snet-flex-telemetry'
var controlFunctionSubnetName = 'snet-flex-control'
var privateEndpointSubnetName = 'snet-private-endpoints'
var apiSubnetName = 'snet-api'
var telemetryDeploymentContainerName = 'deploy-telemetry'
var blobPrivateDnsZoneName = 'privatelink.blob.${environment().suffixes.storage}'
var queuePrivateDnsZoneName = 'privatelink.queue.${environment().suffixes.storage}'
var tablePrivateDnsZoneName = 'privatelink.table.${environment().suffixes.storage}'
var postgresConnectionString = 'postgresql://${postgresAdministratorLogin}:${postgresAdministratorPassword}@${postgresServerName}.postgres.database.azure.com:5432/${databaseName}?sslmode=require'
var logAnalyticsReaderRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '73c42c96-874c-492b-b04d-ab87d138a893'
)
var tableDataContributorRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
)

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    RetentionInDays: 30
    DisableIpMasking: false
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
      ipRules: []
      virtualNetworkRules: []
    }
    accessPolicies: []
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = if (provisionPostgres) {
  name: postgresServerName
  location: postgresLocation
  sku: {
    name: postgresSkuName
    tier: postgresTier
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdministratorLogin
    administratorLoginPassword: postgresAdministratorPassword
    availabilityZone: '1'
    storage: {
      storageSizeGB: 32
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = if (provisionPostgres) {
  parent: postgres
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (provisionPostgres) {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource databaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'database-url'
  properties: {
    value: postgresConnectionString
  }
}

resource credentialKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'credential-encryption-key'
  properties: {
    value: credentialEncryptionKey
  }
}

resource managementKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'management-api-key'
  properties: {
    value: managementApiKey
  }
}

resource apimKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'apim-subscription-key'
  properties: {
    value: apimSubscriptionKey
  }
}

resource apimProbeKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'apim-probe-subscription-key'
  properties: {
    value: apimProbeSubscriptionKey
  }
}

resource eventHubNamespace 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: eventHubNamespaceName
  location: location
  sku: {
    name: 'Standard'
    tier: 'Standard'
    capacity: 1
  }
  properties: {
    isAutoInflateEnabled: true
    maximumThroughputUnits: 2
    publicNetworkAccess: 'Enabled'
  }
}

resource usageEventHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: eventHubNamespace
  name: eventHubName
  properties: {
    messageRetentionInDays: 7
    partitionCount: 4
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
    }
  }
}

resource functionDeploymentBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource functionDeploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: functionDeploymentBlobService
  name: telemetryDeploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource ledgerStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: ledgerStorageName
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
      defaultAction: 'Deny'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource ledgerTableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: ledgerStorage
  name: 'default'
}

resource ledgerTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: ledgerTableService
  name: ledgerTableName
}

var ledgerTableEndpoint = ledgerStorage.properties.primaryEndpoints.table

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: virtualNetworkName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.42.0.0/16'
      ]
    }
  }
}

resource telemetryFunctionSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: virtualNetwork
  name: telemetryFunctionSubnetName
  properties: {
    addressPrefix: '10.42.3.0/27'
    delegations: [
      {
        name: 'flex-consumption-delegation'
        properties: {
          serviceName: 'Microsoft.App/environments'
        }
      }
    ]
  }
  dependsOn: [
    virtualNetwork
  ]
}

resource controlFunctionSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: virtualNetwork
  name: controlFunctionSubnetName
  properties: {
    addressPrefix: '10.42.3.32/27'
    delegations: [
      {
        name: 'flex-consumption-delegation'
        properties: {
          serviceName: 'Microsoft.App/environments'
        }
      }
    ]
  }
  dependsOn: [
    telemetryFunctionSubnet
  ]
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: virtualNetwork
  name: privateEndpointSubnetName
  properties: {
    addressPrefix: '10.42.2.0/24'
    privateEndpointNetworkPolicies: 'Disabled'
  }
  dependsOn: [
    controlFunctionSubnet
  ]
}

resource apiSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: virtualNetwork
  name: apiSubnetName
  properties: {
    addressPrefix: '10.42.3.64/27'
    delegations: [
      {
        name: 'app-service-delegation'
        properties: {
          serviceName: 'Microsoft.Web/serverFarms'
        }
      }
    ]
  }
  dependsOn: [
    privateEndpointSubnet
  ]
}

resource blobPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: blobPrivateDnsZoneName
  location: 'global'
}

resource blobPrivateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: blobPrivateDnsZone
  name: 'finops-vnet'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource queuePrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: queuePrivateDnsZoneName
  location: 'global'
}

resource queuePrivateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: queuePrivateDnsZone
  name: 'finops-vnet'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource tablePrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: tablePrivateDnsZoneName
  location: 'global'
}

resource tablePrivateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: tablePrivateDnsZone
  name: 'finops-vnet'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource storageBlobPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${storageName}-blob'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'storage-blob'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [
            'blob'
          ]
        }
      }
    ]
  }
}

resource storageBlobPrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: storageBlobPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: blobPrivateDnsZone.id
        }
      }
    ]
  }
}

resource storageQueuePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${storageName}-queue'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'storage-queue'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [
            'queue'
          ]
        }
      }
    ]
  }
}

resource storageQueuePrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: storageQueuePrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'queue'
        properties: {
          privateDnsZoneId: queuePrivateDnsZone.id
        }
      }
    ]
  }
}

resource storageTablePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${storageName}-table'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'storage-table'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [
            'table'
          ]
        }
      }
    ]
  }
}

resource storageTablePrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: storageTablePrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'table'
        properties: {
          privateDnsZoneId: tablePrivateDnsZone.id
        }
      }
    ]
  }
}

resource ledgerTablePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${ledgerStorageName}-table'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'ledger-table'
        properties: {
          privateLinkServiceId: ledgerStorage.id
          groupIds: [
            'table'
          ]
        }
      }
    ]
  }
}

resource ledgerTablePrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: ledgerTablePrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'table'
        properties: {
          privateDnsZoneId: tablePrivateDnsZone.id
        }
      }
    ]
  }
}

module keyVaultPrivateEndpoint 'key-vault-private-endpoint.bicep' = {
  name: 'finops-key-vault-private-endpoint'
  params: {
    location: location
    keyVaultName: keyVault.name
    virtualNetworkName: virtualNetwork.name
    privateEndpointSubnetName: privateEndpointSubnet.name
  }
}

resource apiPlan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: planName
  location: location
  sku: {
    name: 'B1'
    tier: 'Basic'
    capacity: 1
  }
  kind: 'linux'
  properties: {
    reserved: true
  }
}

resource telemetryPlan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: telemetryPlanName
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

resource api 'Microsoft.Web/sites@2024-11-01' = {
  name: apiName
  location: location
  kind: 'app,linux'
  tags: {
    SecurityControl: 'Ignore'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: apiPlan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      alwaysOn: true
      healthCheckPath: '/health'
      appCommandLine: 'python -m backend.migrate && python -m backend.bootstrap && python -m uvicorn backend.api:app --host 0.0.0.0 --port 8000'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'false' }
        { name: 'ENABLE_ORYX_BUILD', value: 'false' }
        { name: 'WEBSITE_RUN_FROM_PACKAGE', value: '1' }
        { name: 'PYTHONPATH', value: '/home/site/wwwroot:/home/site/wwwroot/.python_packages/lib/site-packages' }
        { name: 'WEBSITES_PORT', value: '8000' }
        { name: 'WEBSITES_CONTAINER_START_TIME_LIMIT', value: '1800' }
        { name: 'DATABASE_URL', value: postgresConnectionString }
        { name: 'DATA_BACKEND', value: 'postgresql' }
        { name: 'CREDENTIAL_ENCRYPTION_KEY', value: credentialEncryptionKey }
        { name: 'MANAGEMENT_API_KEY', value: managementApiKey }
        { name: 'APIM_PRINCIPAL_ID', value: apimPrincipalId }
        { name: 'DATABRICKS_OAUTH_ENABLED', value: string(databricksOAuthEnabled) }
        { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
        { name: 'APIM_RESOURCE_GROUP', value: apimResourceGroupName }
        { name: 'APIM_SERVICE_NAME', value: apimName }
        { name: 'APIM_API_ID', value: apimApiId }
        { name: 'APIM_PRODUCT_ID', value: apimProductId }
        { name: 'APIM_GATEWAY_URL', value: apimGatewayUrl }
        { name: 'APIM_DASHBOARD_SUBSCRIPTION_ID', value: dashboardSubscriptionId }
        { name: 'APIM_DASHBOARD_SUBSCRIPTION_KEY', value: apimSubscriptionKey }
        { name: 'APIM_PROBE_SUBSCRIPTION_ID', value: probeSubscriptionId }
        { name: 'GATEWAY_RELEASE_WORKER_ENABLED', value: string(gatewayReleaseWorkerEnabled) }
        { name: 'GATEWAY_APPLICATION_PROVISIONING_ENABLED', value: string(gatewayApplicationProvisioningEnabled) }
        { name: 'GATEWAY_APPLICATION_DEFAULT_MONTHLY_TOKEN_LIMIT', value: string(gatewayApplicationDefaultMonthlyTokenLimit) }
        { name: 'GATEWAY_APPLICATION_DEFAULT_TOKENS_PER_MINUTE', value: string(gatewayApplicationDefaultTokensPerMinute) }
        { name: 'LEDGER_SYNC_ENABLED', value: 'true' }
        { name: 'LEDGER_TABLE_ENDPOINT', value: ledgerTableEndpoint }
        { name: 'LEDGER_TABLE_NAME', value: ledgerTableName }
        { name: 'GATEWAY_APPLICATION_KEY_MANAGEMENT_ENABLED', value: string(gatewayApplicationKeyManagementEnabled) }
        { name: 'ENTRA_CLIENT_ID', value: entraClientId }
        { name: 'ENTRA_ALLOWED_EMAIL_DOMAINS', value: string(entraAllowedEmailDomains) }
        { name: 'BOOTSTRAP_OWNER_EMAIL', value: bootstrapOwnerEmail }
        { name: 'BOOTSTRAP_OWNER_PASSWORD_HASH', value: bootstrapOwnerPasswordHash }
        { name: 'PRODUCTION', value: 'true' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'TRAFFIC_GENERATION_BUDGET_USD', value: '20' }
        { name: 'WEB_DIST_DIR', value: 'frontend/dist' }
        { name: 'MIGRATIONS_DIR', value: 'migrations' }
      ]
    }
  }
}

resource apiVnetIntegration 'Microsoft.Web/sites/networkConfig@2024-11-01' = {
  parent: api
  name: 'virtualNetwork'
  properties: {
    subnetResourceId: apiSubnet.id
    swiftSupported: true
  }
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
    serverFarmId: telemetryPlan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${functionDeploymentContainer.name}'
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
        { name: 'EVENT_HUB_NAME', value: eventHubName }
        { name: 'EVENT_HUB_CONNECTION__fullyQualifiedNamespace', value: '${eventHubNamespace.name}.servicebus.windows.net' }
        { name: 'EVENT_HUB_CONNECTION__credential', value: 'managedidentity' }
        { name: 'DATABASE_URL', value: postgresConnectionString }
        { name: 'DATA_BACKEND', value: 'postgresql' }
        { name: 'PRODUCTION', value: 'true' }
        { name: 'LOG_ANALYTICS_WORKSPACE_ID', value: workspace.properties.customerId }
        { name: 'APIM_API_ID', value: apimApiId }
        { name: 'CACHE_READ_BACKFILL_HOURS', value: '720' }
        { name: 'CACHE_READ_OVERLAP_HOURS', value: '24' }
        { name: 'LEDGER_SYNC_ENABLED', value: 'true' }
        { name: 'LEDGER_TABLE_ENDPOINT', value: ledgerTableEndpoint }
        { name: 'LEDGER_TABLE_NAME', value: ledgerTableName }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
      ]
    }
  }
}

resource functionVnetIntegration 'Microsoft.Web/sites/networkConfig@2024-11-01' = {
  parent: functionApp
  name: 'virtualNetwork'
  properties: {
    subnetResourceId: telemetryFunctionSubnet.id
    swiftSupported: true
  }
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

resource apiKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, api.id, 'key-vault-secrets-user')
  scope: keyVault
  properties: {
    principalId: api.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}

resource functionKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, functionApp.id, 'key-vault-secrets-user')
  scope: keyVault
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}

resource functionEventHubReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(usageEventHub.id, functionApp.id, 'event-hubs-data-receiver')
  scope: usageEventHub
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a638d3c7-ab3a-418d-83e6-5f17a39d4fde')
  }
}

resource functionLogAnalyticsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(workspace.id, functionApp.id, 'log-analytics-reader')
  scope: workspace
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: logAnalyticsReaderRoleDefinitionId
  }
}

resource apiLedgerContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(ledgerTable.id, api.id, 'table-data-contributor')
  scope: ledgerTable
  properties: {
    principalId: api.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: tableDataContributorRoleDefinitionId
  }
}

resource telemetryLedgerContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(ledgerTable.id, functionApp.id, 'table-data-contributor')
  scope: ledgerTable
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: tableDataContributorRoleDefinitionId
  }
}

resource apimLedgerContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(ledgerTable.id, apimPrincipalId, 'table-data-contributor')
  scope: ledgerTable
  properties: {
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: tableDataContributorRoleDefinitionId
  }
}

resource apimEventHubSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(usageEventHub.id, apimPrincipalId, 'event-hubs-data-sender')
  scope: usageEventHub
  properties: {
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2b629674-e913-4c01-ae53-ef4638d8f975')
  }
}

output postgresServerName string = postgresServerName
output postgresFqdn string = '${postgresServerName}.postgres.database.azure.com'
output databaseName string = databaseName
output eventHubNamespaceResourceId string = eventHubNamespace.id
output eventHubNamespaceName string = eventHubNamespace.name
output eventHubName string = usageEventHub.name
output applicationInsightsName string = appInsights.name
output applicationInsightsConnectionString string = appInsights.properties.ConnectionString
output ledgerStorageName string = ledgerStorage.name
output ledgerTableEndpoint string = ledgerTableEndpoint
output keyVaultName string = keyVault.name
output databaseUrlSecretUri string = databaseUrlSecret.properties.secretUri
output apimSubscriptionKeySecretUri string = apimKeySecret.properties.secretUri
output apimProbeSubscriptionKeySecretUri string = apimProbeKeySecret.properties.secretUri
output credentialEncryptionKeySecretUri string = credentialKeySecret.properties.secretUri
output appServicePlanName string = apiPlan.name
output telemetryFunctionPlanName string = telemetryPlan.name
output telemetryDeploymentContainerName string = functionDeploymentContainer.name
output apiName string = api.name
output apiUrl string = 'https://${api.properties.defaultHostName}'
output apiPrincipalId string = api.identity.principalId
output functionName string = functionApp.name
