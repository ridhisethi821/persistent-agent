targetScope = 'resourceGroup'

@description('Fully qualified container image in Azure Container Registry.')
param containerImage string

@description('Azure Container Apps Sandbox endpoint.')
param sandboxEndpoint string

@description('Comma-separated Sandbox Group mappings: group-name:resource-group.')
param sandboxGroups string

@description('Versionless Key Vault secret URI for ANTHROPIC_API_KEY.')
param anthropicSecretUri string

@description('Existing storage account name.')
param storageAccountName string

@description('Existing Azure Container Registry name.')
param registryName string

@description('Existing API managed identity name.')
param apiIdentityName string

@description('Existing Container Apps environment name.')
param containerAppsEnvironmentName string

@description('Existing workspace Blob container name.')
param workspaceContainerName string = 'workspaces'

@description('Existing transcript Blob container name.')
param transcriptContainerName string = 'claude-transcripts'

param location string = resourceGroup().location

var suffix = uniqueString(subscription().id, resourceGroup().id)

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource apiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: apiIdentityName
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: containerAppsEnvironmentName
}

// Container App hosting the API and built frontend.

resource apiContainerApp 'Microsoft.App/containerApps@2024-03-01' = {
name: 'pa-api-${suffix}'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${apiIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: registry.properties.loginServer
          identity: apiIdentity.id
        }
      ]
      secrets: [
        {
          name: 'anthropic-api-key'
          keyVaultUrl: anthropicSecretUri
          identity: apiIdentity.id
        }
      ]
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
    }
    template: {
      containers: [
        {
          name: 'persistent-agent-api'
          image: containerImage
          resources: {
            cpu: 1
            memory: '2Gi'
          }
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 18
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              periodSeconds: 10
              failureThreshold: 3
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              periodSeconds: 30
              failureThreshold: 3
            }
          ]
          env: [
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
            { name: 'WORKSPACE_STORAGE_BACKEND', value: 'azure_blob' }
            { name: 'AZURE_STORAGE_ACCOUNT_URL', value: concat('https', '://', storage.name, '.blob.', environment().suffixes.storage) }
            { name: 'AZURE_STORAGE_CONTAINER', value: workspaceContainerName }
            { name: 'SESSION_STORE_BACKEND', value: 'azure_tables' }
            { name: 'AZURE_TABLES_ENDPOINT', value: concat('https', '://', storage.name, '.table.', environment().suffixes.storage) }
            { name: 'CLAUDE_TRANSCRIPT_STORE_BACKEND', value: 'azure_blob' }
            { name: 'CLAUDE_TRANSCRIPT_CONTAINER', value: transcriptContainerName }
            { name: 'EXECUTION_PROVIDER', value: 'azure_sandbox' }
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
            { name: 'AZURE_CLIENT_ID', value: apiIdentity.properties.clientId }
            { name: 'AZURE_SANDBOX_GROUPS', value: sandboxGroups }
            { name: 'AZURE_SANDBOX_ENDPOINT', value: sandboxEndpoint }
            { name: 'AZURE_SANDBOX_RESOURCE_GROUP', value: 'slot-d-rg1' }
            { name: 'AZURE_SANDBOX_DISK_ID', value: '71610387-0f84-475a-853c-228380078702' }
            { name: 'AZURE_SANDBOX_CPU', value: '3000m' }
            { name: 'AZURE_SANDBOX_MEMORY', value: '6144Mi' }
            { name: 'AZURE_SANDBOX_AUTO_SUSPEND_SECONDS', value: '300' }
            { name: 'SESSION_MAX_CONCURRENT', value: '50' }
            { name: 'SESSION_MEMORY_MB', value: '6144' }
            { name: 'SESSION_MAX_MEMORY_MB', value: '6144' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 10
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
}

output apiContainerAppName string = apiContainerApp.name
output apiContainerAppUrl string = apiContainerApp.properties.configuration.ingress.fqdn
