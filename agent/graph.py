"""
Order-to-cash exception handling agent — LangGraph orchestration.

Real, live Azure integrations in this file:
  - Azure OpenAI / Azure AI Foundry (chat completions, via langchain-openai)
  - Azure AI Search (semantic retrieval of contract terms / SOPs)
  - Cosmos DB (session read-through + episodic memory write-back)
  - HTTP calls to the Azure Functions ERP/CRM stand-ins

Runs as a container on AKS, fronted by API Management.
"""
import json
import os
from typing import Any, Dict, List, TypedDict

import requests
from azure.cosmos import CosmosClient
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from langchain_openai import AzureChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

try:
    from langgraph.checkpoint.postgres import PostgresSaver
except ImportError:
    PostgresSaver = None

FOUNDRY_ENDPOINT = os.environ["FOUNDRY_ENDPOINT"]
FOUNDRY_API_KEY = os.environ["FOUNDRY_API_KEY"]
MODEL_DEPLOYMENT = os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-5-mini")

SEARCH_ENDPOINT = os.environ["SEARCH_ENDPOINT"]
SEARCH_KEY = os.environ["SEARCH_KEY"]
SEARCH_INDEX = os.environ.get("SEARCH_INDEX_NAME", "contracts-sops")

COSMOS_ENDPOINT = os.environ["COSMOS_ENDPOINT"]
COSMOS_KEY = os.environ["COSMOS_KEY"]

FUNCTIONS_BASE_URL = os.environ["FUNCTIONS_BASE_URL"]
FUNCTIONS_KEY = os.environ["FUNCTIONS_KEY"]

MAX_JUDGE_RETRIES = 3

# ---------------------------------------------------------------------------
# Short-term / session memory (the tier that was previously missing).
#
# This is turn-by-turn state WITHIN one case's conversation — distinct from
# the episodic Cosmos DB memory above, which persists ACROSS cases for a
# customer. LangGraph's checkpointer gives every node access to prior state
# for the same thread_id (we use case_id as the thread_id), so a follow-up
# message on the same case resumes with full context instead of starting
# the graph cold.
#
# POSTGRES_CONN_STRING set -> durable checkpointer, survives pod restarts
# and is shared across all AKS replicas (required once you run more than
# one pod, since in-memory state is per-pod and would silently disagree
# depending which replica handles the next request).
# Not set -> falls back to MemorySaver (in-process only) for local/dev use.
# ---------------------------------------------------------------------------
POSTGRES_CONN_STRING = os.environ.get("POSTGRES_CONN_STRING")


def _build_checkpointer():
    if POSTGRES_CONN_STRING and PostgresSaver is not None:
        saver_cm = PostgresSaver.from_conn_string(POSTGRES_CONN_STRING)
        saver = saver_cm.__enter__()
        saver.setup()
        return saver
    return MemorySaver()

llm = AzureChatOpenAI(
    azure_endpoint=FOUNDRY_ENDPOINT,
    api_key=FOUNDRY_API_KEY,
    azure_deployment=MODEL_DEPLOYMENT,
    api_version="2024-10-21",
    # gpt-5-mini only supports the default temperature (1). Must be passed
    # explicitly — langchain_openai's own default is 0.7, not the model's
    # default, so simply omitting the parameter still sends an unsupported
    # value.
    temperature=1,
)

search_client = SearchClient(
    endpoint=SEARCH_ENDPOINT,
    index_name=SEARCH_INDEX,
    credential=AzureKeyCredential(SEARCH_KEY),
)

cosmos_client = CosmosClient(COSMOS_ENDPOINT, COSMOS_KEY)
cosmos_db = cosmos_client.get_database_client("ordertocash")
episodic_container = cosmos_db.get_container_client("episodicMemory")
cases_container = cosmos_db.get_container_client("cases")


class AgentState(TypedDict):
    case_id: str
    customer_id: str
    query: str
    episodic_context: Dict[str, Any]
    semantic_context: List[str]
    plan: Dict[str, Any]
    tool_results: Dict[str, Any]
    judge_result: Dict[str, Any]
    retry_count: int
    outcome: str


def intake_node(state: AgentState) -> Dict[str, Any]:
    """Normalizes the incoming event/case into canonical state."""
    cases_container.upsert_item({
        "id": state["case_id"],
        "customerId": state["customer_id"],
        "status": "processing",
        "query": state["query"],
    })
    return {"retry_count": 0}


def context_assembly_node(state: AgentState) -> Dict[str, Any]:
    """Pulls episodic memory (Cosmos) and semantic memory (Azure AI Search) in parallel."""
    episodic_context = {}
    try:
        item = episodic_container.read_item(
            item=state["customer_id"], partition_key=state["customer_id"]
        )
        episodic_context = item
    except Exception:
        episodic_context = {"note": "no prior episodic record for this customer"}

    search_results = search_client.search(
        search_text=state["query"], top=3, query_type="semantic",
        semantic_configuration_name="default"
    )
    semantic_context = [doc.get("content", "") for doc in search_results]

    return {
        "episodic_context": episodic_context,
        "semantic_context": semantic_context,
    }


def orchestrator_node(state: AgentState) -> Dict[str, Any]:
    """Real LLM call decides the plan — which tools, what parameters."""
    prompt = f"""You are an order-to-cash exception handling agent.

Customer request: {state['query']}

Customer history/preferences: {json.dumps(state['episodic_context'])}

Relevant contract terms / SOPs:
{chr(10).join(state['semantic_context'])}

Return a JSON plan with keys: action ("cross_ship", "billing_adjustment", or "escalate"),
part_number (if applicable), reasoning (short).
Respond with JSON only.
"""
    response = llm.invoke(prompt)
    try:
        plan = json.loads(response.content)
    except json.JSONDecodeError:
        plan = {"action": "escalate", "reasoning": "model did not return valid JSON"}
    return {"plan": plan}


def tool_execution_node(state: AgentState) -> Dict[str, Any]:
    """Calls the real Function endpoints (SAP/CRM stand-ins) based on the plan."""
    plan = state["plan"]
    tool_results = {}

    if plan.get("action") == "cross_ship" and plan.get("part_number"):
        resp = requests.post(
            f"{FUNCTIONS_BASE_URL}/api/check_inventory",
            params={"code": FUNCTIONS_KEY},
            json={"part_number": plan["part_number"]},
            timeout=10,
        )
        tool_results["inventory"] = resp.json()

    return {"tool_results": tool_results}


def judge_node(state: AgentState) -> Dict[str, Any]:
    """Schema + business-rule check before anything writes to a system of record."""
    plan = state["plan"]
    tool_results = state["tool_results"]

    if plan.get("action") == "cross_ship":
        inventory = tool_results.get("inventory", {})
        if not inventory.get("recommended_warehouse"):
            return {"judge_result": {"pass": False, "reason": "no warehouse has stock"}}
        return {"judge_result": {"pass": True, "confidence": 0.9}}

    if plan.get("action") == "escalate":
        return {"judge_result": {"pass": True, "confidence": 1.0}}

    return {"judge_result": {"pass": False, "reason": "unrecognized action"}}


def commit_or_escalate_node(state: AgentState) -> Dict[str, Any]:
    """Writes the resolution back via the CRM Function, and updates episodic memory."""
    plan = state["plan"]
    judge = state["judge_result"]

    if judge.get("pass") and plan.get("action") != "escalate":
        resolution = {
            "action": plan.get("action"),
            "details": state["tool_results"],
        }
        requests.post(
            f"{FUNCTIONS_BASE_URL}/api/update_crm_case",
            params={"code": FUNCTIONS_KEY},
            json={
                "case_id": state["case_id"],
                "customer_id": state["customer_id"],
                "notes": f"Automated resolution: {plan.get('reasoning', '')}",
                "resolution": resolution,
            },
            timeout=10,
        )
        episodic_container.upsert_item({
            "id": state["customer_id"],
            "customerId": state["customer_id"],
            "last_resolution": resolution,
        })
        outcome = "resolved"
    else:
        outcome = "escalated"

    cases_container.upsert_item({
        "id": state["case_id"],
        "customerId": state["customer_id"],
        "status": outcome,
    })
    return {"outcome": outcome}


def route_after_judge(state: AgentState) -> str:
    if state["judge_result"].get("pass"):
        return "commit_or_escalate"
    if state["retry_count"] >= MAX_JUDGE_RETRIES:
        return "commit_or_escalate"  # will fall through to escalate
    return "orchestrator"


def build_graph():
    workflow = StateGraph(AgentState)
    workflow.add_node("intake", intake_node)
    workflow.add_node("context_assembly", context_assembly_node)
    workflow.add_node("orchestrator", orchestrator_node)
    workflow.add_node("tool_execution", tool_execution_node)
    workflow.add_node("judge", judge_node)
    workflow.add_node("commit_or_escalate", commit_or_escalate_node)

    workflow.add_edge(START, "intake")
    workflow.add_edge("intake", "context_assembly")
    workflow.add_edge("context_assembly", "orchestrator")
    workflow.add_edge("orchestrator", "tool_execution")
    workflow.add_edge("tool_execution", "judge")
    workflow.add_conditional_edges("judge", route_after_judge, {
        "orchestrator": "orchestrator",
        "commit_or_escalate": "commit_or_escalate",
    })
    workflow.add_edge("commit_or_escalate", END)

    return workflow.compile(checkpointer=_build_checkpointer())


compiled_graph = build_graph()
