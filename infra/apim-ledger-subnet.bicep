targetScope = 'subscription'
param resourceGroupName string
param location string
param virtualNetworkName string
param apimSubnetName string
param apimSubnetAddressPrefix string
param networkSecurityGroupName string
param apimSkuName string

module subnet 'modules/apim-ledger-subnet.bicep' = {
  name: 'apim-ledger-subnet-hotfix'
  scope: resourceGroup(resourceGroupName)
  params: {
    location: location
    virtualNetworkName: virtualNetworkName
    apimSubnetName: apimSubnetName
    apimSubnetAddressPrefix: apimSubnetAddressPrefix
    networkSecurityGroupName: networkSecurityGroupName
    apimSkuName: apimSkuName
  }
}
