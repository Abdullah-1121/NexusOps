"""Feature 3 acceptance tests (spec §3): state ordering, D-6 manual_review,
D-4 escalation rule, and gate behavior via the checkpointer (Pattern B)."""

import asyncio

import pytest

from langgraph.types import Command

from app.models import ModelError
from app.state_machine import (_failure_reason, build_graph, make_classify_node,
                               route_escalation)

ALERT = {
    "incident_id": "11111111-1111-1111-1111-111111111111",
    "occurred_at": "2026-09-08T12:00:00Z",
    "source": "prometheus",
    "severity_hint": "info",
    "service": "auth",
    "status_code": 0,
    "message": "p99 spike",
}

PLAN = {
    "severity": "critical",
    "affected_service": "auth",
    "root_cause_hypothesis": "db overload",
    "reasoning": "Evidence log-1 shows a saturated connection pool during the "
                 "incident window, which matches the db-overload hypothesis; "
                 "rolling back the deploy directly relieves the checkouts.",
    "confidence": 0.9,
    "remediation_steps": [
        {"action": "rollback", "target": "abc123", "reason": "bad deploy"}
    ],
    "requires_approval": True,
    "evidence": ["log-1"],
}


async def _fake_classify(messages, model_env, schema):
    return {
        "severity": "info",
        "affected_service": "auth",
        "triage_confidence": 0.95,
        "ambiguous": False,
    }


class Boom:
    """Injected model fn that always fails -> D-6 manual_review path."""

    async def __call__(self, messages, model_env, schema):
        raise RuntimeError("provider down")


async def _fake_rca(messages, model_env, schema):
    return PLAN


async def _fake_evidence(incident: dict) -> list:
    return [{"timestamp": "2026-09-08T11:59:00Z", "level": "error", "message": "boom"}]


async def _spy_rollback(sha: str):
    calls.append(("rollback", sha))
    return {"status": "performed"}


calls = []


STATE = {"incident_id": ALERT["incident_id"], "alert": ALERT}


async def _drive(graph, alert=ALERT, decision="approve"):
    """Drive one incident through classify..gate, then resume with decision."""
    config = {"configurable": {"thread_id": alert["incident_id"]}}
    await graph.ainvoke({"incident_id": alert["incident_id"], "alert": alert}, config)  # runs to interrupt
    out = await graph.ainvoke(
        Command(resume={"decision": decision, "actor": "ops"}),
        config,
    )
    return out


def _run_incident(graph, alert=ALERT, decision="approve"):
    return asyncio.run(_drive(graph, alert, decision))


def test_full_walk_approve_terminates_and_rolls_back(monkeypatch):
    graph = build_graph(classify=_fake_classify, rca=_fake_rca, gather=_fake_evidence, rollback_tool=_spy_rollback)
    out = _run_incident(graph, decision="approve")
    assert out["terminal"] == "resolved"
    assert any(c[0] == "rollback" for c in calls)


def test_rca_node_model_env_honors_operator_config(monkeypatch):
    """T-7.11 (2026-09-28): the consuming node's model choice follows
    NEXUSOPS_RCA_MODEL. Default keeps D-4 — the frontier deep-dives escalated
    incidents — and the override `slm` drops the frontier out of the WHOLE
    pipeline, even on escalated incidents. This asserts the env the node
    actually hands the model fn; serve._inspect asserts the stamp matches it
    (one env, both sides)."""
    from app.state_machine import make_rca_node

    seen = {}

    async def spy_rca(messages, model_env, schema):
        seen["model_env"] = model_env
        return PLAN

    async def run(escalated):
        node = make_rca_node(spy_rca)
        return await node({**STATE, "escalated": escalated})

    # default, escalated -> frontier deep-dive (D-4 preserved)
    monkeypatch.delenv("NEXUSOPS_RCA_MODEL", raising=False)
    asyncio.run(run(True))
    assert seen["model_env"] == "NEXUSOPS_FRONTIER_MODEL"

    # NEXUSOPS_RCA_MODEL=slm -> SLM even on an escalated incident
    monkeypatch.setenv("NEXUSOPS_RCA_MODEL", "slm")
    asyncio.run(run(True))
    assert seen["model_env"] == "NEXUSOPS_SLM_MODEL"


def test_reject_never_reaches_rollback():
    calls.clear()
    graph = build_graph(classify=_fake_classify, rca=_fake_rca, gather=_fake_evidence, rollback_tool=_spy_rollback)
    out = _run_incident(graph, decision="reject")
    assert out["terminal"] == "rejected"
    assert calls == []  # FR-5/NG-1: no remediation on reject


def test_invalid_gate_decision_fails_loud():
    graph = build_graph(classify=_fake_classify, rca=_fake_rca, gather=_fake_evidence, rollback_tool=_spy_rollback)
    cfg = {"configurable": {"thread_id": "id-invalid"}}
    with pytest.raises(ValueError, match="invalid"):
        asyncio.run(_drive(graph, decision="maybe"))


def test_ai_down_fixture_routes_to_manual_review():
    graph = build_graph(classify=Boom(), rca=Boom(), gather=_fake_evidence, rollback_tool=_spy_rollback)

    async def run():
        return await graph.ainvoke(
            {"incident_id": "id-2", "alert": ALERT},
            {"configurable": {"thread_id": "id-2"}},
        )

    out = asyncio.run(run())
    # classify failed -> manual_review, never reaches gate
    assert "manual_review_reason" in out
    assert out.get("terminal") == "manual_review"


@pytest.mark.parametrize(
    "alert,expected",
    [
        ({"severity_hint": "critical", "triage_confidence": 0.9, "ambiguous": False}, "escalate"),
        ({"severity_hint": "info", "triage_confidence": 0.5, "ambiguous": False}, "escalate"),
        ({"severity_hint": "info", "triage_confidence": 0.9, "ambiguous": True}, "escalate"),
        ({"severity_hint": "info", "triage_confidence": 0.9, "ambiguous": False}, "evidence"),
    ],
)
def test_escalation_rule_matches_d4(alert, expected):
    class S(dict):
        pass
    state = {"alert": alert, **{k: v for k, v in alert.items() if k in ("triage_confidence", "ambiguous")}}
    assert route_escalation(state) == expected

def test_rca_node_locks_gate_invariant():
    # NG-1 gate lock (2026-09-21): the human approval gate is a SYSTEM
    # invariant — a model emitting requires_approval=false must not be able to
    # exempt its own plan at the choke point where plans enter the system.
    from app.state_machine import make_rca_node

    rogue_plan = {k: v for k, v in PLAN.items()}
    rogue_plan["requires_approval"] = False

    async def rogue_rca(messages, model_env, schema):
        return {k: v for k, v in rogue_plan.items()}

    async def run():
        node = make_rca_node(rogue_rca)
        out = await node(STATE)
        return out

    out = asyncio.run(run())
    assert out["plan"]["requires_approval"] is True  # system rule wins
    # everything else about the model's plan is preserved
    assert out["plan"]["root_cause_hypothesis"] == PLAN["root_cause_hypothesis"]


# --- D-15: readable, classified manual_review reasons (2026-09-28) -------------


def test_failure_reason_never_empty_and_kind_tagged():
    # a Message whose str() is empty must still name the class (the fallback
    # that fixes the live "bare model error" report)
    msg = ModelError("")
    assert str(msg) == ""
    reason = _failure_reason("RCA", msg)
    assert reason != "RCA failed (unknown): "
    assert "ModelError" in reason
    assert reason.startswith("RCA failed (unknown): ")


def test_failure_reason_carries_kind_and_detail():
    reason = _failure_reason(
        "SLM classify", ModelError("hit its daily quota cap", kind="quota")
    )
    assert reason == "SLM classify failed (quota): hit its daily quota cap"


def test_classify_failure_reason_is_a_readable_sentence_not_a_repr():
    async def run():
        class Boom():
            async def __call__(self, messages, model_env, schema):
                raise ModelError("exceeded quota", kind="quota")
        n = make_classify_node(Boom())
        return await n({"incident_id": "id-3", "alert": ALERT})

    out = asyncio.run(run())
    assert out["manual_review_reason"] == "SLM classify failed (quota): exceeded quota"
    assert "ModelError(" not in out["manual_review_reason"]  # never an exception repr
