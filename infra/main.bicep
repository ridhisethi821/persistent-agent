targetScope = 'resourceGroup'

@description('Azure region for the API infrastructure.')
param location string = resourceGroup().location

var suffix = uniqueString(subscription().id, resourceGroup().id)
var storageName = 'pa${suffix}'
var registryName = 'paacr${suffix}'
var identityName = 'persistent-agent-api'
var logName = 'pa-logs-${suffix}'
var environmentName = 'pa-env-${suffix}'

// Managed identity used by the API Container App.
resource apiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

// Storage account for workspaces and Claude transcripts.
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource workspaceContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'workspaces'
  properties: {
    publicAccess: 'None'
  }
}

resource transcriptContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'claude-transcripts'
  properties: {
    publicAccess: 'None'
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource sessionsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'AgentSessions'
}

resource capacityTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'ExecutionCapacity'
}

// Managed identity data-plane permissions.
var blobDataContributorRole = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var tableDataContributorRole = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'

resource blobPermission 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, apiIdentity.name, blobDataContributorRole)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataContributorRole)
    principalId: apiIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource tablePermission 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, apiIdentity.name, tableDataContributorRole)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', tableDataContributorRole)
    principalId: apiIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Container Registry for the application image.
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

var acrPullRole = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registryPullPermission 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, apiIdentity.name, acrPullRole)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRole)
    principalId: apiIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Key Vault for application secrets.
var keyVaultName = 'pa-kv-${suffix}'
var keyVaultSecretsUserRole = '4633458b-17de-408a-b874-0445c86b69e6'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    sku: {
      family: 'A'
      name: 'standard'
    }
    accessPolicies: []
  }
}

resource keyVaultSecretsPermission 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, apiIdentity.name, keyVaultSecretsUserRole)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      keyVaultSecretsUserRole
    )
    principalId: apiIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Centralized application logs.
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    features: {
      searchVersion: 1
    }
  }
}

// Consumption-based Container Apps environment.
resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

output storageAccountName string = storage.name
output storageAccountUrl string = concat('https', '://', storage.name, '.blob.', environment().suffixes.storage)
output tablesEndpoint string = concat('https', '://', storage.name, '.table.', environment().suffixes.storage)
output workspaceContainerName string = workspaceContainer.name
output transcriptContainerName string = transcriptContainer.name
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output apiIdentityName string = apiIdentity.name
output apiIdentityClientId string = apiIdentity.properties.clientId
output apiIdentityPrincipalId string = apiIdentity.properties.principalId
output containerAppsEnvironmentName string = containerAppsEnvironment.name
output logAnalyticsWorkspaceName string = logAnalytics.name


// Grant the API identity Sandbox Group management access in slot-d-rg1.
module sandboxPermissionsRg1 'sandbox-permissions.bicep' = {
  name: 'sandbox-permissions-rg1'
  scope: resourceGroup(subscription().subscriptionId, 'slot-d-rg1')
  params: {
    principalId: apiIdentity.properties.principalId
  }
}





output keyVaultName string = keyVault.name
