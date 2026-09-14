"""
Order-to-cash exception handling agent — Azure Functions layer.

Two of these are HONESTLY SIMULATED interfaces standing in for SAP and
Salesforce (check_inventory, update_crm_case) — no real ERP/CRM system is
behind them. Everything else (Cosmos DB writes, Service Bus publish, Event
Grid trigger, Application Insights logging) is real, live Azure service
usage, not mocked.

Deploy:
  func azure functionapp publish func-o2c-<suffix>
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import azure.functions as func
from azure.cosmos import CosmosClient
from azure.servicebus import ServiceBusClient, ServiceBusMessage

app = func.FunctionApp()

COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT")
COSMOS_KEY = os.environ.get("COSMOS_KEY")
SERVICEBUS_CONNECTION = os.environ.get("SERVICEBUS_CONNECTION")

_cosmos_client = None
_cases_container = None


def _get_cases_container():
    global _cosmos_client, _cases_container
    if _cases_container is None:
        _cosmos_client = CosmosClient(COSMOS_ENDPOINT, COSMOS_KEY)
        db = _cosmos_client.get_database_client("ordertocash")
        _cases_container = db.get_container_client("cases")
    return _cases_container


# ---------------------------------------------------------------------------
# SIMULATED SAP inventory/logistics interface.
# Real production version: outbound OData/REST call to SAP S/4HANA
# Material Availability / ATP check, same request/response shape.
# ---------------------------------------------------------------------------
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


@app.route(route="check_inventory", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def check_inventory(req: func.HttpRequest) -> func.HttpResponse:
    body = req.get_json()
    part_number = body.get("part_number", "")
    logging.info("check_inventory called for part_number=%s", part_number)

    results = _MOCK_WAREHOUSE_INVENTORY.get(part_number, [])
    best = max(results, key=lambda r: r["available_qty"], default=None)

    return func.HttpResponse(
        json.dumps({
            "part_number": part_number,
            "locations": results,
            "recommended_warehouse": best["warehouse"] if best else None,
            "source": "SIMULATED_SAP_INTERFACE",
        }),
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# SIMULATED Salesforce case write-back. Real version: Salesforce REST/Bulk
# API PATCH on the Case object. Publishes to Service Bus for reliable,
# retryable delivery instead of writing synchronously.
# ---------------------------------------------------------------------------
@app.route(route="update_crm_case", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def update_crm_case(req: func.HttpRequest) -> func.HttpResponse:
    body = req.get_json()
    case_id = body.get("case_id")
    notes = body.get("notes", "")
    resolution = body.get("resolution", {})

    logging.info("update_crm_case queued for case_id=%s", case_id)

    sb_client = ServiceBusClient.from_connection_string(SERVICEBUS_CONNECTION)
    with sb_client:
        sender = sb_client.get_queue_sender(queue_name="crm-erp-writebacks")
        with sender:
            msg = ServiceBusMessage(json.dumps({
                "case_id": case_id,
                "notes": notes,
                "resolution": resolution,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }))
            sender.send_messages(msg)

    container = _get_cases_container()
    container.upsert_item({
        "id": case_id,
        "customerId": body.get("customer_id", "unknown"),
        "status": "resolution_queued",
        "notes": notes,
        "resolution": resolution,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

    return func.HttpResponse(
        json.dumps({
            "case_id": case_id,
            "status": "queued_for_crm_write",
            "source": "SIMULATED_SALESFORCE_INTERFACE",
        }),
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# REAL Event Grid trigger. This is the proactive, event-driven entry point —
# a case-created (or delay-detected) event lands here instead of requiring
# a human to open a chat window.
# ---------------------------------------------------------------------------
@app.event_grid_trigger(arg_name="event")
def on_case_event(event: func.EventGridEvent):
    data = event.get_json()
    logging.info("Event Grid case event received: %s", data)

    case_id = data.get("case_id", str(uuid.uuid4()))
    container = _get_cases_container()
    container.upsert_item({
        "id": case_id,
        "customerId": data.get("customer_id", "unknown"),
        "status": "intake_received",
        "raw_event": data,
        "received_at": datetime.now(timezone.utc).isoformat(),
    })
    # In the full flow, this is where the agent's /invoke endpoint gets
    # called (HTTP POST to the AKS-hosted service, or a Service Bus message
    # that the agent pod consumes) to kick off the graph run for this case.
