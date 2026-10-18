targetScope = 'resourceGroup'
param location string
param virtualNetworkName string
param apimSubnetName string
param apimSubnetAddressPrefix string
param networkSecurityGroupName string
@allowed(['Developer', 'StandardV2'])
param apimSkuName string

resource network 'Microsoft.Network/virtualNetworks@2024-05-01' existing = { name: virtualNetworkName }
resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: networkSecurityGroupName
  location: location
  properties: {
    securityRules: apimSkuName == 'Developer' ? [
      {
        name: 'apim-public-gateway'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRanges: ['80', '443']
        }
      }
      {
        name: 'apim-control-plane'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'ApiManagement'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '3443'
        }
      }
    ] : []
  }
}
resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: network
  name: apimSubnetName
  properties: {
    addressPrefix: apimSubnetAddressPrefix
    networkSecurityGroup: { id: nsg.id }
    delegations: apimSkuName == 'StandardV2' ? [
      { name: 'apim-outbound-delegation', properties: { serviceName: 'Microsoft.Web/serverFarms' } }
    ] : []
  }
}
