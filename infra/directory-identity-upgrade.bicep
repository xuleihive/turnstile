targetScope = 'subscription'

param apimResourceGroupName string
param apimName string
param apiId string
param sourceRevision string
param revision string

@allowed(['prepare', 'promote'])
param stage string
param createRevision bool = false

@secure()
param apiProperties object = {}
@secure()
param parentPolicy string = ''

module upgrade 'modules/apim-upgrade.bicep' = {
  name: 'apim-directory-identity-${uniqueString(apimName, apiId, revision, stage)}'
  scope: resourceGroup(apimResourceGroupName)
  params: {
    apimName: apimName
    apiId: apiId
    sourceRevision: sourceRevision
    revision: revision
    stage: stage
    createRevision: createRevision
    apiProperties: apiProperties
    parentPolicy: parentPolicy
    initializeImagePolicy: false
    imageOperationProperties: {}
  }
}
