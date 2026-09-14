# Order-to-cash exception handling agent — deploy runbook

Honesty note, kept from the design phase: `check_inventory` and
`update_crm_case` in `functions/function_app.py` are simulated SAP/Salesforce
interfaces (mock data, no real ERP/CRM behind them) — clearly labeled in
the code and the response payloads with `"source": "SIMULATED_..."`.
Every other integration here (Cosmos DB, Azure AI Search, Event Grid,
Service Bus, AKS, API Management, Application Insights, Azure OpenAI via
Foundry) is real Azure service usage, wired to actually run.

## 0. Prerequisites
```bash
az login
az account set --subscription <sub-id>
az group create -n rg-o2c-demo -l eastus
```

## 1. Provision everything (kick this off first — AKS/APIM take the longest)
```bash
cd infra
az deployment group create \
  -g rg-o2c-demo \
  -f main.bicep \
  -p main.parameters.json \
  -p foundryApiKey=<your-foundry-api-key>
```
This provisions: Cosmos DB, Azure AI Search, Event Grid topic, Service Bus
namespace + queue, storage + Function App, Container Registry, AKS, API
Management (Consumption tier — provisions in minutes, not the 30-45 min a
Developer-tier instance would take), Log Analytics + Application Insights.

Capture the outputs — you'll need them for the next steps:
```bash
az deployment group show -g rg-o2c-demo -n main --query properties.outputs
```

## 2. Load a few documents into Azure AI Search
Create the `contracts-sops` index (via the Azure Portal's "Import data"
wizard, or the `azure-search-documents` SDK) and upload a handful of
sample contract-terms / SOP documents — this is what `context_assembly_node`
retrieves against. A handful of paragraphs is enough for a demo.

## 3. Deploy the Functions (ERP/CRM stand-ins)
```bash
cd ../functions
func azure functionapp publish func-o2c-o2c01
```
Grab a function key from the portal (Function App → App keys) for
`FUNCTIONS_KEY` in the next step.

## 4. Build and push the agent image
```bash
cd ../agent
az acr build --registry acro2co2c01 --image o2c-agent:latest .
```

## 5. Deploy the agent to AKS
```bash
az aks get-credentials -g rg-o2c-demo -n aks-o2c-o2c01
# Edit k8s/deployment.yaml: replace <ACR_LOGIN_SERVER> and every REPLACE_ME
# secret value with the real outputs from steps 1 and 3.
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl get pods -w   # confirm it comes up healthy
```

## 6. Front it with API Management
```bash
cd ../apim
# Edit policy.xml: replace <AKS_INTERNAL_LB_IP_OR_HOSTNAME> with the
# service's internal address (kubectl get svc o2c-agent-svc).
az apim api import -g rg-o2c-demo --service-name apim-o2c-o2c01 \
  --path o2c --specification-path openapi.yaml --specification-format OpenApi
az apim api policy create -g rg-o2c-demo --service-name apim-o2c-o2c01 \
  --api-id <api-id> --policy-file policy.xml
```

## 7. Wire the Event Grid trigger
```bash
az eventgrid event-subscription create \
  --name case-created-sub \
  --source-resource-id $(az eventgrid topic show -g rg-o2c-demo -n evgt-o2c-o2c01 --query id -o tsv) \
  --endpoint $(az functionapp function show -g rg-o2c-demo -n func-o2c-o2c01 \
    --function-name on_case_event --query invokeUrlTemplate -o tsv) \
  --endpoint-type azurefunction
```

## 8. Demo it end to end
```bash
# Simulate a case-created event landing from Salesforce:
az eventgrid event send --topic-endpoint <eventGridTopicEndpoint> \
  --topic-key <topic-key> \
  --events '[{"id":"1","eventType":"CaseCreated","subject":"case/CRM-1082",
             "data":{"case_id":"CRM-1082","customer_id":"cust-42",
                     "query":"My shipment is delayed, do you have PART-99X anywhere?"},
             "dataVersion":"1.0"}]'

# Or call the agent directly through APIM:
curl -X POST https://apim-o2c-o2c01.azure-api.net/o2c/invoke \
  -H "Ocp-Apim-Subscription-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"customer_id":"cust-42","query":"My shipment is delayed, do you have PART-99X anywhere?"}'
```
Then pull up the live trace in Application Insights (Transaction Search)
to show the end-to-end span across the Function call, the Azure OpenAI
call, and the Cosmos writes.

## What's genuinely live vs. what's a stand-in
| Service | Status |
|---|---|
| Azure OpenAI / AI Foundry | Real — calls your existing `benjmainsunder-3891` model deployment |
| Azure AI Search | Real — real index, real semantic retrieval |
| Cosmos DB | Real — episodic memory + case ledger |
| Azure Functions | Real Functions, simulated SAP/Salesforce payloads inside them |
| Event Grid | Real — real topic, real subscription, real trigger |
| Service Bus | Real — real queue with DLQ, used for reliable CRM write-back |
| AKS | Real cluster running the real agent container |
| API Management | Real Consumption-tier instance, real policy, real routing |
| Application Insights | Real — live distributed trace across the whole call |
| Postgres (session memory) | Real — LangGraph checkpointer backed by a real Flexible Server, shared correctly across all AKS replicas |

## Memory tiers — what's real
- **Episodic** (Cosmos `episodicMemory` container): read at the start of every case, written back on resolution. Persists *across* cases for a customer.
- **Semantic** (Azure AI Search): retrieved every case, grounds the orchestrator's plan in real contract/SOP content.
- **Session/short-term** (Postgres via LangGraph checkpointer, `graph.py`): persists *within* one case's `thread_id` (= `case_id`), so a follow-up call on the same case resumes with full prior state instead of starting cold. Required to be Postgres-backed, not `MemorySaver`, once you run more than one AKS replica — in-memory checkpoints are per-pod and will silently disagree across replicas.
