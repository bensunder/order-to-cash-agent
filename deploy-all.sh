#!/usr/bin/env bash
set -euo pipefail

: "${FOUNDRY_API_KEY:?Set FOUNDRY_API_KEY env var first}"
: "${PG_ADMIN_PASSWORD:?Set PG_ADMIN_PASSWORD env var first}"

RG=rg-o2c-demo
LOCATION=eastus
SUFFIX=o2c01
FOUNDRY_ENDPOINT="https://benjmainsunder-3891-resource.services.ai.azure.com"

echo "== 0. Azure login check =="
az account show -o table

echo "== 1. Resource group =="
az group create -n "$RG" -l "$LOCATION" -o none

echo "== 2. Deploy infrastructure (long pole: AKS + APIM, be patient) =="
az deployment group create \
  -g "$RG" -f infra/main.bicep -p infra/main.parameters.json \
  -p nameSuffix="$SUFFIX" -p foundryApiKey="$FOUNDRY_API_KEY" \
  -p postgresAdminPassword="$PG_ADMIN_PASSWORD" -o none

echo "== 3. Capture outputs =="
o() { az deployment group show -g "$RG" -n main --query "properties.outputs.$1.value" -o tsv; }
COSMOS_ENDPOINT=$(o cosmosEndpoint)
SEARCH_ENDPOINT=$(o searchEndpoint)
ACR_LOGIN_SERVER=$(o acrLoginServer)
AKS_NAME=$(o aksName)
APPINSIGHTS_CONN=$(o appInsightsConnectionString)
POSTGRES_CONN=$(o postgresConnectionString)

COSMOS_KEY=$(az cosmosdb keys list -g "$RG" -n "cosmos-o2c-$SUFFIX" --query primaryMasterKey -o tsv)
SEARCH_KEY=$(az search admin-key show -g "$RG" --service-name "srch-o2c-$SUFFIX" --query primaryKey -o tsv)
SERVICEBUS_CONNECTION=$(az servicebus namespace authorization-rule keys list \
  -g "$RG" --namespace-name "sb-o2c-$SUFFIX" --name RootManageSharedAccessKey \
  --query primaryConnectionString -o tsv)

echo "== 4. Create AI Search index + load sample contract/SOP docs =="
curl -s -X PUT "$SEARCH_ENDPOINT/indexes/contracts-sops?api-version=2024-07-01" \
  -H "api-key: $SEARCH_KEY" -H "Content-Type: application/json" \
  -d '{"name":"contracts-sops","fields":[
        {"name":"id","type":"Edm.String","key":true},
        {"name":"content","type":"Edm.String","searchable":true}],
      "semantic":{"configurations":[{"name":"default",
        "prioritizedFields":{"prioritizedContentFields":[{"fieldName":"content"}]}}]}}' -o /dev/null

curl -s -X POST "$SEARCH_ENDPOINT/indexes/contracts-sops/docs/index?api-version=2024-07-01" \
  -H "api-key: $SEARCH_KEY" -H "Content-Type: application/json" \
  -d '{"value":[
    {"@search.action":"upload","id":"1","content":"Shipping delay policy: customers whose orders are delayed more than 3 business days are eligible for a cross-ship of an equivalent part from any warehouse with available stock, at no additional cost."},
    {"@search.action":"upload","id":"2","content":"Billing credit policy: shipments delayed more than 5 days qualify for a 10 percent billing credit on the affected line item, applied automatically upon resolution."},
    {"@search.action":"upload","id":"3","content":"Cross-ship authorization: agents may authorize a cross-ship without manager approval if the replacement part is available in any warehouse with at least 1 unit of stock."}
  ]}' -o /dev/null

echo "== 5. Build both container images in ACR (no local Docker needed) =="
ACR_NAME="${ACR_LOGIN_SERVER%%.*}"
az acr build --registry "$ACR_NAME" --image o2c-agent:latest ./agent
az acr build --registry "$ACR_NAME" --image erp-crm-service:latest ./erp-crm-service

echo "== 6. AKS credentials =="
az aks install-cli 2>/dev/null || true
az aks get-credentials -g "$RG" -n "$AKS_NAME" --overwrite-existing

echo "== 7. Deploy ERP/CRM microservice to AKS first (agent depends on its URL) =="
cat > /tmp/erp-crm-deployment.yaml << YAML_EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: erp-crm-service
spec:
  replicas: 2
  selector:
    matchLabels: { app: erp-crm-service }
  template:
    metadata:
      labels: { app: erp-crm-service }
    spec:
      containers:
        - name: erp-crm-service
          image: ${ACR_LOGIN_SERVER}/erp-crm-service:latest
          ports: [{ containerPort: 8080 }]
          envFrom: [{ secretRef: { name: erp-crm-secrets } }]
          readinessProbe:
            httpGet: { path: /healthz, port: 8080 }
            initialDelaySeconds: 5
            periodSeconds: 10
---
apiVersion: v1
kind: Secret
metadata:
  name: erp-crm-secrets
type: Opaque
stringData:
  COSMOS_ENDPOINT: "${COSMOS_ENDPOINT}"
  COSMOS_KEY: "${COSMOS_KEY}"
  SERVICEBUS_CONNECTION: "${SERVICEBUS_CONNECTION}"
---
apiVersion: v1
kind: Service
metadata:
  name: erp-crm-service-svc
spec:
  selector: { app: erp-crm-service }
  ports: [{ port: 80, targetPort: 8080 }]
  type: LoadBalancer
YAML_EOF
kubectl apply -f /tmp/erp-crm-deployment.yaml

echo "Waiting for erp-crm-service public IP..."
for i in $(seq 1 30); do
  ERP_LB_IP=$(kubectl get svc erp-crm-service-svc -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  [ -n "$ERP_LB_IP" ] && break
  sleep 10
done
echo "ERP/CRM service reachable at: http://$ERP_LB_IP"
FUNCTIONS_BASE_URL="http://${ERP_LB_IP}"
FUNCTIONS_KEY=""   # no key needed on this demo endpoint — add auth before real use

echo "== 8. Deploy the agent, pointing at the erp-crm-service =="
cat > /tmp/o2c-deployment.yaml << YAML_EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: o2c-agent
spec:
  replicas: 2
  selector:
    matchLabels: { app: o2c-agent }
  template:
    metadata:
      labels: { app: o2c-agent }
    spec:
      containers:
        - name: o2c-agent
          image: ${ACR_LOGIN_SERVER}/o2c-agent:latest
          ports: [{ containerPort: 8080 }]
          envFrom: [{ secretRef: { name: o2c-agent-secrets } }]
          readinessProbe:
            httpGet: { path: /healthz, port: 8080 }
            initialDelaySeconds: 5
            periodSeconds: 10
---
apiVersion: v1
kind: Secret
metadata:
  name: o2c-agent-secrets
type: Opaque
stringData:
  FOUNDRY_ENDPOINT: "${FOUNDRY_ENDPOINT}"
  FOUNDRY_API_KEY: "${FOUNDRY_API_KEY}"
  MODEL_DEPLOYMENT_NAME: "gpt-5-mini"
  SEARCH_ENDPOINT: "${SEARCH_ENDPOINT}"
  SEARCH_KEY: "${SEARCH_KEY}"
  SEARCH_INDEX_NAME: "contracts-sops"
  COSMOS_ENDPOINT: "${COSMOS_ENDPOINT}"
  COSMOS_KEY: "${COSMOS_KEY}"
  FUNCTIONS_BASE_URL: "${FUNCTIONS_BASE_URL}"
  FUNCTIONS_KEY: "${FUNCTIONS_KEY}"
  APPLICATIONINSIGHTS_CONNECTION_STRING: "${APPINSIGHTS_CONN}"
  POSTGRES_CONN_STRING: "${POSTGRES_CONN}"
YAML_EOF

cat > /tmp/o2c-service.yaml << 'YAML_EOF'
apiVersion: v1
kind: Service
metadata:
  name: o2c-agent-svc
spec:
  selector: { app: o2c-agent }
  ports: [{ port: 80, targetPort: 8080 }]
  type: LoadBalancer
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: o2c-agent-hpa
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: o2c-agent }
  minReplicas: 2
  maxReplicas: 6
  metrics:
    - type: Resource
      resource: { name: cpu, target: { type: Utilization, averageUtilization: 70 } }
YAML_EOF

kubectl apply -f /tmp/o2c-deployment.yaml
kubectl apply -f /tmp/o2c-service.yaml

echo "Waiting for agent public IP..."
for i in $(seq 1 30); do
  LB_IP=$(kubectl get svc o2c-agent-svc -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  [ -n "$LB_IP" ] && break
  sleep 10
done
echo "Agent reachable at: http://$LB_IP"

echo "== 9. Wire Event Grid trigger to the erp-crm-service webhook endpoint =="
TOPIC_ID=$(az eventgrid topic show -g "$RG" -n "evgt-o2c-$SUFFIX" --query id -o tsv)
az eventgrid event-subscription create --name case-created-sub \
  --source-resource-id "$TOPIC_ID" \
  --endpoint "http://${ERP_LB_IP}/api/events" --endpoint-type webhook -o none

echo ""
echo "===================================================================="
echo "DONE."
echo "Agent:         http://$LB_IP/invoke"
echo "ERP/CRM svc:   http://$ERP_LB_IP/healthz"
echo ""
echo "Demo call:"
echo "curl -X POST http://$LB_IP/invoke -H 'Content-Type: application/json' \\"
echo "  -d '{\"customer_id\":\"cust-42\",\"query\":\"My shipment is delayed, do you have PART-99X anywhere?\"}'"
echo ""
echo "Simulate a proactive event (no chat involved):"
echo "az eventgrid event send --topic-endpoint \$(az eventgrid topic show -g $RG -n evgt-o2c-$SUFFIX --query endpoint -o tsv) \\"
echo "  --topic-key \$(az eventgrid topic key list -g $RG -n evgt-o2c-$SUFFIX --query key1 -o tsv) \\"
echo "  --events '[{\"id\":\"1\",\"eventType\":\"CaseCreated\",\"subject\":\"case/CRM-1082\",\"data\":{\"case_id\":\"CRM-1082\",\"customer_id\":\"cust-42\"},\"dataVersion\":\"1.0\"}]'"
echo "===================================================================="
