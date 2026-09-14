"""
FastAPI wrapper around the LangGraph agent. This is what runs in the AKS
pod and sits behind API Management.

Run locally:
  uvicorn app:app --host 0.0.0.0 --port 8080
"""
import logging
import os
import uuid

from azure.monitor.opentelemetry import configure_azure_monitor
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from graph import compiled_graph

APPINSIGHTS_CONNECTION_STRING = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
if APPINSIGHTS_CONNECTION_STRING:
    configure_azure_monitor(connection_string=APPINSIGHTS_CONNECTION_STRING)

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Order-to-cash exception handling agent")

# Demo-only: allow any origin so a static HTML front end can call this
# directly from a browser. Scope this to the actual front-end's origin
# before this goes anywhere near production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class CaseRequest(BaseModel):
    customer_id: str
    query: str
    case_id: str | None = None


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/invoke")
def invoke(req: CaseRequest):
    # case_id doubles as the LangGraph thread_id: a follow-up call with the
    # same case_id resumes from the checkpointed state for this case
    # (short-term/session memory) instead of starting the graph cold.
    case_id = req.case_id or str(uuid.uuid4())
    result = compiled_graph.invoke(
        {
            "case_id": case_id,
            "customer_id": req.customer_id,
            "query": req.query,
        },
        config={"configurable": {"thread_id": case_id}},
    )
    return {
        "case_id": case_id,
        "outcome": result.get("outcome"),
        "plan": result.get("plan"),
        "tool_results": result.get("tool_results"),
        "judge_result": result.get("judge_result"),
    }
