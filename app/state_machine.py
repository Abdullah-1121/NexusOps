"""NexusOps — Feature 3: LangGraph state machine (Pattern B: thread-per-incident).

Each incident runs as one graph under `thread_id=<incident_id>`: the
MemorySaver checkpointer owns per-incident state isolation (NFR-3) and the
paused state at the gate (D-3). The gate is a real `interrupt()`: graph
freezes, persists, resumes on the same thread when the §5.4 decision arrives.

Dependency injection principle: classify and RCA are *callables* passed in,
because the AI-down test (D-6) must simulate provider failure without a
network. The default wiring (build_default) uses real OpenRouter via
`app.models` and real MCP evidence via `app.evidence`.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Literal, Optional, TypedDict

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt

from app.evidence import EvidenceFn, gather_evidence
from app.models import CONFIDENCE_SCHEMA, RCA_SCHEMA, complete_json

ModelFn = Callable[[list[dict], str, dict], Any]

Terminal = Literal["resolved", "rejected", "manual_review"]


class IncidentState(TypedDict, total=False):
    incident_id: str
    alert: dict
    severity: Optional[str]
    affected_service: Optional[str]
    triage_confidence: Optional[float]
    ambiguous: Optional[bool]
    escalated: bool
    evidence: list
    plan: dict
    decision: dict
    manual_review_reason: str
    rollback_result: dict
    terminal: str


def _model_failure(reason: str) -> dict:
    return {"manual_review_reason": reason}


def make_classify_node(classify: ModelFn) -> Callable:
    async def classify_node(state: IncidentState) -> dict:
        try:
            result = await classify(
                [
                    {
                        "role": "user",
                        "content": (
                            "You are an SRE triage classifier. Classify this alert "
                            "into severity (critical|warning|info), affected_service, "
                            "triage_confidence (0..1), ambiguous (bool). "
                            f"QUERY: {json.dumps(state['alert'])}"
                        ),
                    }
                ],
                "NEXUSOPS_SLM_MODEL",
                CONFIDENCE_SCHEMA,
            )
        except Exception as exc:  # D-6: model error -> manual_review, loud
            return _model_failure(f"SLM unavailable: {exc!r}")
        return {
            "severity": result["severity"],
            "affected_service": result["affected_service"],
            "triage_confidence": result["triage_confidence"],
            "ambiguous": result["ambiguous"],
        }

    return classify_node


def make_escalate_node() -> Callable:
    def escalate_node(state: IncidentState) -> dict:
        return {"escalated": True}

    return escalate_node


def route_escalation(state: IncidentState) -> Literal["escalate", "evidence"]:
    """D-4 deterministic rule: critical hint OR low confidence OR ambiguous."""
    if state.get("manual_review_reason"):
        return "manual_review"
    if state["alert"].get("severity_hint") == "critical":
        return "escalate"
    if (state.get("triage_confidence") or 0.0) < 0.6:
        return "escalate"
    if state.get("ambiguous"):
        return "escalate"
    return "evidence"


def make_evidence_node(gather: EvidenceFn) -> Callable:
    async def evidence_node(state: IncidentState) -> dict:
        try:
            return {"evidence": await gather(state["alert"])}
        except Exception as exc:  # tool/gather failure: loud, into evidence
            return {"evidence": [{"error": repr(exc)}], "manual_review_reason": "evidence gathering failed"}

    return evidence_node


def make_rca_node(rca: ModelFn) -> Callable:
    async def rca_node(state: IncidentState) -> dict:
        model_env = "NEXUSOPS_FRONTIER_MODEL" if state.get("escalated") else "NEXUSOPS_SLM_MODEL"
        try:
            plan = await rca(
                [
                    {
                        "role": "user",
                        "content": (
                            "Produce an RFC3339-aware remediation plan (strict JSON per schema) "
                            "for this incident + evidence. Incident: "
                            f"{json.dumps(state['alert'])} Evidence: {json.dumps(state['evidence'])}"
                        ),
                    }
                ],
                model_env,
                RCA_SCHEMA,
            )
        except Exception as exc:  # D-6
            return _model_failure(f"{'FF' if state.get('escalated') else 'SLM'} RCA unavailable: {exc!r}")
        return {"plan": plan}

    return rca_node


def make_gate_node(rollback_tool: Callable) -> Callable:
    def gate_node(state: IncidentState) -> dict:
        # interrupt: freezes graph, checkpoints state, waits for §5.4 resume.
        decision = interrupt({"incident_id": state["incident_id"], "plan": state.get("plan")})
        # §5.4 strict: decision must be exactly approve|reject. Anything else is
        # an input error — fail loud, never silently route to either branch.
        if not isinstance(decision, dict) or decision.get("decision") not in ("approve", "reject"):
            raise ValueError(f"invalid §5.4 gate decision: {decision!r}")
        return {"decision": decision}

    return gate_node


def route_after_gate(state: IncidentState) -> Literal["approve", "reject"]:
    return "approve" if state.get("decision", {}).get("decision") == "approve" else "reject"


def make_rollback_node(rollback_tool: Callable) -> Callable:
    async def rollback_node(state: IncidentState) -> dict:
        if state.get("decision", {}).get("decision") == "approve":
            sha = _rollback_target(state.get("plan", {}))
            if sha:
                result = await rollback_tool(sha)
                return {"rollback_result": result}
        return {}

    return rollback_node


def _rollback_target(plan: dict) -> str | None:
    for step in plan.get("remediation_steps", []):
        if step.get("action") == "rollback":
            return step.get("target")
    return None


# --- real adapters (default wiring) -----------------------------------------

async def _openrouter(messages, model_env, schema):
    return await complete_json(messages, model_env=model_env, timeout_s=20.0, json_schema=schema)


def build_graph(
    classify: ModelFn = _openrouter,
    rca: ModelFn = _openrouter,
    gather: EvidenceFn = gather_evidence,
    rollback_tool: Callable | None = None,
):
    """Compile the per-incident state machine with MemorySaver checkpointer."""
    from app.mcp_server import approve_rollback, trigger_github_rollback

    async def _mock_rollback(sha: str):
        approve_rollback(sha)
        return trigger_github_rollback(sha)

    rollback = rollback_tool or _mock_rollback

    g = StateGraph(IncidentState)
    g.add_node("classify", make_classify_node(classify))
    g.add_node("escalate", make_escalate_node())
    g.add_node("evidence", make_evidence_node(gather))
    g.add_node("rca", make_rca_node(rca))
    g.add_node("gate", make_gate_node(rollback))
    g.add_node("rollback", make_rollback_node(rollback))
    g.add_node("resolved", lambda s: {"terminal": "resolved"})
    g.add_node("rejected", lambda s: {"terminal": "rejected"})
    g.add_node("manual_review", lambda s: {"terminal": "manual_review"})

    g.set_entry_point("classify")
    g.add_conditional_edges(
        "classify", route_escalation, {"escalate": "escalate", "evidence": "evidence", "manual_review": "manual_review"}
    )
    g.add_edge("escalate", "evidence")
    g.add_edge("evidence", "rca")
    g.add_conditional_edges("rca", lambda s: "gate" if s.get("manual_review_reason") is None else "manual_review")
    g.add_conditional_edges(
        "gate", route_after_gate, {"approve": "rollback", "reject": "rejected"}
    )
    g.add_edge("rollback", "resolved")
    return g.compile(checkpointer=MemorySaver())