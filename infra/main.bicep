// Order-to-cash exception handling agent — full infrastructure
// Deploy: az deployment group create -g <rg> -f main.bicep -p main.parameters.json

@description('Short unique suffix, e.g. o2c01')
param nameSuffix string

@description('Azure region')
param location string = resourceGroup().location

@description('Existing Azure AI Foundry / Azure OpenAI endpoint to reuse (e.g. https://benjmainsunder-3891-resource.services.ai.azure.com)')
param foundryEndpoint string

@description('Existing Foundry/OpenAI API key')
@secure()
param foundryApiKey string

@description('Model deployment name to call for chat completions')
param modelDeploymentName string = 'gpt-5-mini'

@description('Admin password for the session-memory Postgres server')
@secure()
param postgresAdminPassword string

var tags = {
  project: 'order-to-cash-agent'
  env: 'demo'
}

// ---------- Observability ----------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-o2c-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-o2c-${nameSuffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ---------- Cosmos DB (episodic memory + case ledger) ----------
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: 'cosmos-o2c-${nameSuffix}'
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    capabilities: [ { name: 'EnableServerless' } ]
    locations: [ { locationName: location, failoverPriority: 0 } ]
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmos
  name: 'ordertocash'
  properties: { resource: { id: 'ordertocash' } }
}

resource cosmosCases 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: cosmosDb
  name: 'cases'
  properties: {
    resource: {
      id: 'cases'
      partitionKey: { paths: [ '/customerId' ], kind: 'Hash' }
    }
  }
}

resource cosmosEpisodic 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: cosmosDb
  name: 'episodicMemory'
  properties: {
    resource: {
      id: 'episodicMemory'
      partitionKey: { paths: [ '/customerId' ], kind: 'Hash' }
    }
  }
}

// ---------- Postgres (session/short-term memory: LangGraph checkpointer) ----------
resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: 'pg-o2c-${nameSuffix}'
  location: location
  tags: tags
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: 'o2cadmin'
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
  }
}

resource postgresDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-12-01-preview' = {
  parent: postgres
  name: 'agent_checkpoints'
}

resource postgresFirewallAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// ---------- Azure AI Search (semantic memory: contracts, SOPs) ----------
resource search 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: 'srch-o2c-${nameSuffix}'
  location: location
  tags: tags
  sku: { name: 'basic' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
  }
}

// ---------- Event Grid (case-created trigger) ----------
resource eventGridTopic 'Microsoft.EventGrid/topics@2023-12-15-preview' = {
  name: 'evgt-o2c-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    inputSchema: 'EventGridSchema'
  }
}

// ---------- Service Bus (reliable CRM/ERP write-back with DLQ) ----------
resource serviceBus 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: 'sb-o2c-${nameSuffix}'
  location: location
  tags: tags
  sku: { name: 'Standard', tier: 'Standard' }
}

resource sbQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: serviceBus
  name: 'crm-erp-writebacks'
  properties: {
    maxDeliveryCount: 5
    deadLetteringOnMessageExpiration: true
    defaultMessageTimeToLive: 'P1D'
  }
}

// ---------- Storage + Functions (ERP/CRM connector stand-ins) ----------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'sto2c${nameSuffix}'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
}

resource funcPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'plan-o2c-${nameSuffix}'
  location: location
  tags: tags
  // Y1 (Consumption) draws from a separate quota that many trial/sandbox
  // subscriptions have at 0. B1 (Basic) uses standard App Service quota,
  // which is almost always available. Costs a small hourly rate instead
  // of being pay-per-execution — fine for a demo, revisit for real scale.
  sku: { name: 'B1', tier: 'Basic' }
  kind: 'linux'
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: 'func-o2c-${nameSuffix}'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  properties: {
    serverFarmId: funcPlan.id
    siteConfig: {
      linuxFxVersion: 'Python|3.12'
      alwaysOn: true
      appSettings: [
        { name: 'AzureWebJobsStorage', value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=core.windows.net' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
        { name: 'COSMOS_KEY', value: cosmos.listKeys().primaryMasterKey }
        { name: 'EVENTGRID_TOPIC_ENDPOINT', value: eventGridTopic.properties.endpoint }
        { name: 'SERVICEBUS_CONNECTION', value: listKeys('${serviceBus.id}/AuthorizationRules/RootManageSharedAccessKey', '2022-10-01-preview').primaryConnectionString }
      ]
    }
    httpsOnly: true
  }
}

// ---------- Container Registry (agent image for AKS) ----------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: 'acro2c${nameSuffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: true }
}

// ---------- AKS (orchestrator runtime at scale) ----------
resource aks 'Microsoft.ContainerService/managedClusters@2024-03-02-preview' = {
  name: 'aks-o2c-${nameSuffix}'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    dnsPrefix: 'o2c${nameSuffix}'
    agentPoolProfiles: [
      {
        name: 'systempool'
        count: 2
        vmSize: 'Standard_D2s_v5'
        mode: 'System'
        osType: 'Linux'
      }
    ]
    addonProfiles: {
      omsagent: {
        enabled: true
        config: { logAnalyticsWorkspaceResourceID: logAnalytics.id }
      }
    }
  }
}

resource aksAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aks.id, acr.id, 'AcrPull')
  scope: acr
  properties: {
    principalId: aks.properties.identityProfile.kubeletidentity.objectId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalType: 'ServicePrincipal'
  }
}

// ---------- API Management (Consumption tier — provisions in minutes, fronts agent + functions) ----------
resource apim 'Microsoft.ApiManagement/service@2023-05-01-preview' = {
  name: 'apim-o2c-${nameSuffix}'
  location: location
  tags: tags
  sku: { name: 'Consumption', capacity: 0 }
  properties: {
    publisherName: 'Order-to-Cash Agent Demo'
    publisherEmail: 'admin@example.com'
  }
}

// ---------- Outputs ----------
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output functionAppName string = functionApp.name
output functionAppHostname string = functionApp.properties.defaultHostName
output eventGridTopicEndpoint string = eventGridTopic.properties.endpoint
output serviceBusNamespace string = serviceBus.name
output acrLoginServer string = acr.properties.loginServer
output aksName string = aks.name
output apimGatewayUrl string = apim.properties.gatewayUrl
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output postgresConnectionString string = 'postgresql://o2cadmin:${postgresAdminPassword}@${postgres.properties.fullyQualifiedDomainName}:5432/agent_checkpoints?sslmode=require'
