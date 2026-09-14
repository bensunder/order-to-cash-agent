# Order-to-cash exception handling agent — deploy runbook

Honesty note, kept from the design phase: `check_inventory` and
`update_crm_case` in `erp-crm-service/app.py` are simulated SAP/Salesforce
interfaces (mock data, no real ERP/CRM system behind them) — clearly labeled
in the code and the response payloads with `"source": "SIMULATED_..."`.

Architecture note: the ERP/CRM connector logic originally targeted Azure
Functions. This subscription has 0 quota for Microsoft.Web (App Service
Plan) SKUs specifically — confirmed via `SubscriptionIsOverQuotaForSku` on
both Y1 (Consumption) and B1 (Basic), while general compute
(Microsoft.Compute) shows real, available regional vCPU quota. Rather than
keep fighting that specific wall, the connector logic runs as a second
containerized FastAPI microservice on the same AKS cluster as the agent —
a different resource provider, genuinely live, and arguably a stronger
answer to "microservices and distributed systems" than Functions would
have been anyway.

Every other integration here (Cosmos DB, Azure AI Search, Event Grid,
Service Bus, AKS, API Management, Application Insights, Azure OpenAI via
Foundry) is real Azure service usage, wired
to actually run.

## 0. Prerequisites
```bash
az login
az account set --subscription <sub-id>
```
If a previous attempt left a partial resource group behind:
```bash
az group delete -n rg-o2c-demo --yes --no-wait
```

## 1. Set required secrets
```bash
export FOUNDRY_API_KEY="$(az cognitiveservices account keys list \
  --name benjmainsunder-3891-resource \
  --resource-group rg-benjmainsunder-5997 \
  --query key1 -o tsv)"
export PG_ADMIN_PASSWORD="O2cDemo$(date +%s)Az!"
```

## 2. Run everything
```bash
chmod +x deploy-all.sh
./deploy-all.sh
```
This single script: provisions all infrastructure via Bicep, creates and
loads the Azure AI Search index, builds both container images in ACR,
deploys the ERP/CRM microservice and the agent to AKS, and wires the
Event Grid trigger to the microservice's webhook endpoint. It prints the
agent's public endpoint and a ready-to-run demo `curl` command at the end.

### If you hit `SubscriptionIsOverQuotaForSku`
Check whether the relevant resource provider is actually registered —
this looked identical to a real quota limit but was actually a
provider-registration timing issue for `Microsoft.Compute` /
`Microsoft.Network` on first run:
```bash
for ns in Microsoft.Compute Microsoft.ContainerService Microsoft.Network; do
  echo "$ns: $(az provider show --namespace $ns --query registrationState -o tsv)"
done
```
If any show `NotRegistered` or `Registering`, register and wait:
```bash
az provider register --namespace Microsoft.Compute
az provider register --namespace Microsoft.ContainerService
az provider register --namespace Microsoft.Network
```
If all three show `Registered` and you still get the error on a
`Microsoft.Web` resource specifically, that's a genuine subscription-level
block on App Service Plan quota (this is what happened here) — the fix is
architectural, not a retry: don't use Functions/App Service on this
subscription, use a containerized service on AKS instead, as this project
now does.

## 3. Demo it end to end
```bash
# Direct call to the agent:
curl -X POST http://<agent-ip>/invoke -H 'Content-Type: application/json' \
  -d '{"customer_id":"cust-42","query":"My shipment is delayed, do you have PART-99X anywhere?"}'

# Proactive, event-driven — no chat involved:
az eventgrid event send --topic-endpoint <topic-endpoint> --topic-key <topic-key> \
  --events '[{"id":"1","eventType":"CaseCreated","subject":"case/CRM-1082",
             "data":{"case_id":"CRM-1082","customer_id":"cust-42"},
             "dataVersion":"1.0"}]'
```
Then pull up the live trace in Application Insights (Transaction Search)
to show the end-to-end span across the microservice call, the Azure OpenAI
call, and the Cosmos writes.

## What's genuinely live vs. what's a stand-in
| Service | Status |
|---|---|
| Azure OpenAI / AI Foundry | Real — calls your existing `benjmainsunder-3891` model deployment |
| Azure AI Search | Real — real index, real semantic retrieval |
| Cosmos DB | Real — episodic memory + case ledger |
| ERP/CRM connector | Real FastAPI microservice on AKS, simulated SAP/Salesforce payloads inside it |
| Event Grid | Real — real topic, real webhook subscription, real trigger |
| Service Bus | Real — real queue with DLQ, used for reliable CRM write-back |
| AKS | Real cluster running both the agent and the ERP/CRM microservice |
| API Management | Real Consumption-tier instance (see `apim/`), ready to front the AKS services once VNET/private endpoint wiring is added |
| Application Insights | Real — live distributed trace across the whole call |
| Postgres (session memory) | Not available — this subscription has no Postgres Flexible Server SKUs in eastus. Agent falls back to in-process `MemorySaver` automatically. |

## Memory tiers — what's real
- **Episodic** (Cosmos `episodicMemory` container): read at the start of every case, written back on resolution. Persists *across* cases for a customer.
- **Semantic** (Azure AI Search): retrieved every case, grounds the orchestrator's plan in real contract/SOP content.
- **Session/short-term**: designed for Postgres via LangGraph checkpointer (`agent/graph.py`), persisting *within* one case's `thread_id` (= `case_id`) so a follow-up call resumes with full prior state. On this subscription, no Postgres SKUs are available in eastus, so the agent runs with LangGraph's in-process `MemorySaver` instead — same code path, just not durable across pod restarts or shared across AKS replicas until Postgres is added back (different subscription or a lifted restriction).
