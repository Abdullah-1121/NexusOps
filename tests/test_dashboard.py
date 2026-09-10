"""Feature 4 acceptance tests (spec §5). Pattern A: EventBus + FastAPI WS +
catch-up replay + decision-up seam to the parked gate (via make_resume_incident)."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.bus import EventBus
from app.dashboard import create_dashboard_app, make_resume_incident
from app.state_machine import build_graph

ALERT = {
    "incident_id": "99999999-9999-9999-9999-999999999999",
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


async def _fake_rca(messages, model_env, schema):
    return PLAN


async def _fake_evidence(incident: dict) -> list:
    return [{"timestamp": "2026-09-08T11:59:00Z", "level": "error", "message": "boom"}]


def _client(bus: EventBus, resume_incident) -> TestClient:
    return TestClient(create_dashboard_app(bus, resume_incident))


def test_live_events_stream_in_order_over_socket():
    bus = EventBus()
    seen = []
    client = _client(bus, seen.append)
    with client.websocket_connect("/ws") as ws:
        bus.publish("inc-1", "ingest", source="prometheus")
        bus.publish("inc-1", "classified", severity="info")
        bus.publish("inc-1", "gate_open")
        for _ in range(3):
            seen.append(ws.receive_json())
    assert [e["type"] for e in seen] == ["ingest", "classified", "gate_open"]
    assert [e["seq"] for e in seen] == [1, 2, 3]
    assert all(e["incident_id"] == "inc-1" for e in seen)


def test_reconnect_replays_missed_events_for_in_flight_incident():
    bus = EventBus()
    client = _client(bus, lambda *_: None)
    bus.publish("inc-1", "ingest", ts="2026-09-08T12:00:00Z")
    bus.publish("inc-1", "classified", severity="info")
    # client connects AFTER events already happened -> catch-up replay
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        second = ws.receive_json()
        assert [first["type"], second["type"]] == ["ingest", "classified"]
        # then live events follow without duplication
        bus.publish("inc-1", "escalating")
        assert ws.receive_json()["type"] == "escalating"
    # disconnect, two more events, reconnect -> full replay again
    bus.publish("inc-1", "plan")
    bus.publish("inc-1", "gate_open")
    with client.websocket_connect("/ws") as ws:
        types = [ws.receive_json()["type"] for _ in range(5)]
    assert types == ["ingest", "classified", "escalating", "plan", "gate_open"]


def test_catch_up_is_per_incident_ordered():
    bus = EventBus()
    client = _client(bus, lambda *_: None)
    bus.publish("a", "ingest")
    bus.publish("b", "ingest")
    bus.publish("a", "classified")
    with client.websocket_connect("/ws") as ws:
        got = [ws.receive_json() for _ in range(3)]
    per_incident = {}
    for e in got:
        per_incident.setdefault(e["incident_id"], []).append(e["type"])
    assert per_incident == {"a": ["ingest", "classified"], "b": ["ingest"]}
    assert [e["seq"] for e in got if e["incident_id"] == "a"] == [1, 2]


def test_decision_over_socket_delivers_exact_sec54_body_to_gate():
    bus = EventBus()
    delivered = []

    async def resume(incident_id, body):
        delivered.append((incident_id, body))

    client = _client(bus, resume)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"incident_id": "inc-9", "decision": "approve", "actor": "ops"})
        event = ws.receive_json()
    assert delivered == [("inc-9", {"decision": "approve", "actor": "ops"})]
    assert (event["type"], event["decision"], event["actor"]) == ("decision", "approve", "ops")


def test_invalid_decision_frame_gets_loud_error_and_socket_survives():
    async def noop(incident_id, body):
        return None

    bus = EventBus()
    client = _client(bus, noop)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"decision": "approve"})  # missing incident_id, actor
        err = ws.receive_json()
        assert err["type"] == "error"
        # socket still alive: a valid frame works after the error
        ws.send_json({"incident_id": "inc-9", "decision": "approve", "actor": "ops"})
        assert ws.receive_json()["type"] == "decision"


def test_resume_failure_returns_error_frame():
    bus = EventBus()

    async def boom(incident_id, body):
        raise ValueError("invalid decision")

    client = _client(bus, boom)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"incident_id": "inc-9", "decision": "nonsense", "actor": "ops"})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["error"] == "resume failed for inc-9"


def test_history_is_bounded_incidents_fifo_prune():
    bus = EventBus(max_incidents=2, history_size=10)
    bus.publish("a", "ingest")
    bus.publish("b", "ingest")
    bus.publish("c", "ingest")  # evicts "a"
    client = _client(bus, lambda *_: None)
    with client.websocket_connect("/ws") as ws:
        got = [ws.receive_json()["incident_id"] for _ in range(2)]
    assert got == ["b", "c"]
    assert "a" not in bus._seq


def test_make_resume_incident_unblocks_gate_same_loop():
    """Composition: make_resume_incident performs the exact Command(resume)
    call Feature 3's suite proves unblocks the gate. Parked and resumed inside
    one loop so the MemorySaver lock never crosses event loops."""
    rollback_calls = []

    async def _spy_rollback(sha: str):
        rollback_calls.append(sha)
        return {"status": "performed"}

    graph = build_graph(
        classify=_fake_classify, rca=_fake_rca, gather=_fake_evidence, rollback_tool=_spy_rollback
    )
    resume = make_resume_incident(graph)
    config = {"configurable": {"thread_id": ALERT["incident_id"]}}

    async def run():
        await graph.ainvoke({"incident_id": ALERT["incident_id"], "alert": ALERT}, config)
        await resume(ALERT["incident_id"], {"decision": "approve", "actor": "ops"})
        return graph.get_state(config)

    state = asyncio.run(run())
    assert state.values.get("terminal") == "resolved"
    assert rollback_calls == ["abc123"]