"""
ERP/CRM connector microservice — runs on AKS as a second deployment
alongside the agent, instead of Azure Functions.

Why AKS instead of Functions: this subscription has 0 App Service Plan
quota (Microsoft.Web), confirmed via SubscriptionIsOverQuotaForSku on both
Y1 and B1 SKUs, and via `az vm list-usage` showing 10 real regional vCPUs
available — the block is specific to Microsoft.Web, not general compute.
Running this as a container on the already-working AKS cluster sidesteps
that specific quota wall entirely and uses a different resource provider.

check_inventory and update_crm_case are SIMULATED SAP/Salesforce
interfaces — no real ERP/CRM system behind them, same as before.
Cosmos DB and Service Bus calls are real.
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from azure.cosmos import CosmosClient
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from fastapi import FastAPI, Request
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="ERP/CRM connector service")

COSMOS_ENDPOINT = os.environ["COSMOS_ENDPOINT"]
COSMOS_KEY = os.environ["COSMOS_KEY"]
SERVICEBUS_CONNECTION = os.environ["SERVICEBUS_CONNECTION"]

cosmos_client = CosmosClient(COSMOS_ENDPOINT, COSMOS_KEY)
cases_container = cosmos_client.get_database_client("ordertocash").get_container_client("cases")

_MOCK_WAREHOUSE_INVENTORY = {
    "PART-99X": [
        {"warehouse": "Austin-WH2", "available_qty": 14, "eta_days": 0},
        {"warehouse": "Dallas-WH1", "available_qty": 3, "eta_days": 1},
    ],
    "PART-55A": [
        {"warehouse": "Austin-WH2", "available_qty": 0, "eta_days": 5},
        {"warehouse": "Reno-WH3", "available_qty": 22, "eta_days": 2},
    ],
}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


class InventoryRequest(BaseModel):
    part_number: str


@app.post("/api/check_inventory")
def check_inventory(req: InventoryRequest):
    logging.info("check_inventory called for part_number=%s", req.part_number)
    results = _MOCK_WAREHOUSE_INVENTORY.get(req.part_number, [])
    best = max(results, key=lambda r: r["available_qty"], default=None)
    return {
        "part_number": req.part_number,
        "locations": results,
        "recommended_warehouse": best["warehouse"] if best else None,
        "source": "SIMULATED_SAP_INTERFACE",
    }


class CrmUpdateRequest(BaseModel):
    case_id: str
    customer_id: str = "unknown"
    notes: str = ""
    resolution: dict = {}


@app.post("/api/update_crm_case")
def update_crm_case(req: CrmUpdateRequest):
    logging.info("update_crm_case queued for case_id=%s", req.case_id)

    sb_client = ServiceBusClient.from_connection_string(SERVICEBUS_CONNECTION)
    with sb_client:
        sender = sb_client.get_queue_sender(queue_name="crm-erp-writebacks")
        with sender:
            msg = ServiceBusMessage(json.dumps({
                "case_id": req.case_id,
                "notes": req.notes,
                "resolution": req.resolution,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }))
            sender.send_messages(msg)

    cases_container.upsert_item({
        "id": req.case_id,
        "customerId": req.customer_id,
        "status": "resolution_queued",
        "notes": req.notes,
        "resolution": req.resolution,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

    return {
        "case_id": req.case_id,
        "status": "queued_for_crm_write",
        "source": "SIMULATED_SALESFORCE_INTERFACE",
    }


@app.post("/api/events")
async def on_case_event(request: Request):
    """
    Event Grid webhook endpoint (real, event-driven entry point). Handles
    the Event Grid subscription validation handshake, then processes
    case-created / delay-detected events proactively — no human required.
    """
    body = await request.json()
    events = body if isinstance(body, list) else [body]

    # Event Grid subscription validation handshake
    for event in events:
        if event.get("eventType") == "Microsoft.EventGrid.SubscriptionValidationEvent":
            code = event["data"]["validationCode"]
            return {"validationResponse": code}

    for event in events:
        data = event.get("data", {})
        case_id = data.get("case_id", str(uuid.uuid4()))
        cases_container.upsert_item({
            "id": case_id,
            "customerId": data.get("customer_id", "unknown"),
            "status": "intake_received",
            "raw_event": data,
            "received_at": datetime.now(timezone.utc).isoformat(),
        })
        logging.info("Event Grid case event received and recorded: %s", case_id)

    return {"status": "accepted"}
