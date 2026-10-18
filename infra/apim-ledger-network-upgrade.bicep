targetScope = 'subscription'

param resourceGroupName string
param location string
param virtualNetworkName string
param apimName string
param apimSubnetName string = 'snet-apim-ledger'
param apimSubnetAddressPrefix string = '10.42.4.0/24'
param networkSecurityGroupName string

@description('Existing classic APIM SKU and capacity; never upgrades the pricing tier.')
param apimSku object
param apimTags object
param apimIdentity object
@secure()
@description('Current writable APIM properties, captured immediately before preview.')
param currentApimProperties object

module network 'modules/apim-ledger-network-upgrade.bicep' = {
  name: 'apim-ledger-private-access'
  scope: resourceGroup(resourceGroupName)
  params: {
    location: location
    virtualNetworkName: virtualNetworkName
    apimName: apimName
    apimSubnetName: apimSubnetName
    apimSubnetAddressPrefix: apimSubnetAddressPrefix
    networkSecurityGroupName: networkSecurityGroupName
    apimSku: apimSku
    apimTags: apimTags
    apimIdentity: apimIdentity
    currentApimProperties: currentApimProperties
  }
}
