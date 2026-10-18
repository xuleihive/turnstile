targetScope = 'resourceGroup'

param location string
param virtualNetworkName string
@allowed(['Developer', 'StandardV2'])
param apimSkuName string
param apimNetworkSecurityGroupName string

resource apimNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: apimNetworkSecurityGroupName
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

// One owner for the complete subnet collection; reruns must not erase sibling subnets.
resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: virtualNetworkName
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.42.0.0/16'] }
    subnets: [
      {
        name: 'snet-flex-telemetry'
        properties: {
          addressPrefix: '10.42.3.0/27'
          delegations: [{ name: 'flex-consumption-delegation', properties: { serviceName: 'Microsoft.App/environments' } }]
        }
      }
      {
        name: 'snet-flex-control'
        properties: {
          addressPrefix: '10.42.3.32/27'
          delegations: [{ name: 'flex-consumption-delegation', properties: { serviceName: 'Microsoft.App/environments' } }]
        }
      }
      {
        name: 'snet-private-endpoints'
        properties: { addressPrefix: '10.42.2.0/24', privateEndpointNetworkPolicies: 'Disabled' }
      }
      {
        name: 'snet-api'
        properties: {
          addressPrefix: '10.42.3.64/27'
          delegations: [{ name: 'app-service-delegation', properties: { serviceName: 'Microsoft.Web/serverFarms' } }]
        }
      }
      {
        name: 'snet-apim-ledger'
        properties: {
          addressPrefix: '10.42.4.0/24'
          networkSecurityGroup: { id: apimNsg.id }
          delegations: apimSkuName == 'StandardV2' ? [
            { name: 'apim-outbound-delegation', properties: { serviceName: 'Microsoft.Web/serverFarms' } }
          ] : []
        }
      }
    ]
  }
}

output apimSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', network.name, 'snet-apim-ledger')
