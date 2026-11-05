targetScope = 'resourceGroup'

@description('Principal ID of the API managed identity.')
param principalId string

var sandboxGroupDataOwnerRole = 'c24cf47c-5077-412d-a19c-45202126392c'

resource sandboxGroupPermission 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, principalId, sandboxGroupDataOwnerRole)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      sandboxGroupDataOwnerRole
    )
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
