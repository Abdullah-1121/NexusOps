"""Feature 7 acceptance (D-11): pipeline voice, human gate, serve composition.

Covers (no network — smoke fakes):
  * LiveDriver — full approve cycle event vocabulary + order + rollback-once
  * LiveDriver — reject cycle: no rollback event, never calls the tool (NG-1)
  * LiveDriver — honest outage: model-down rca/classify -> manual_review, NO
    gate (no interrupt means the driver never waits on a human)
  * GateAwaiter — first-decision-wins, already_decided, pending()
  * _make_resume — loud raise when nothing is awaiting
And (local Redis, DB 15 — integration, T-7.5):
  * serve app over TestClient + real worker: webhook -> ingress event -> full
    stage order on /ws -> human approve on the socket -> rollback -> done;
    a second decision is refused (already_decided) with no second rollback.
  * /api/fixtures leaks no ground truth; /api/status reports redis + depth.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

import pytest
import redis as redis_sync
from fastapi.testclient import TestClient

from app.benchmark import build_fixtures, smoke_classify, smoke_gather, smoke_rca
from app.bus import EventBus
from app.serve import GateAwaiter, LiveDriver, _make_resume, consume_loop, create_serve_app
from app.state_machine import build_graph

TEST_REDIS_URL = "redis://localhost:6379/15"  # dedicated test DB, away from db 0


@pytest.fixture(autouse=True)
def clean_test_db(monkeypatch):
    monkeypatch.setenv("NEXUSOPS_REDIS_URL", TEST_REDIS_URL)
    r = redis_sync.from_url(TEST_REDIS_URL)
    r.flushdb()
    r.close()


# --- helpers (deterministic smoke graph + recording rollback tool) -----------


def _smoke_graph(rollback_log: list):
    async def fake_rollback(sha: str) -> dict:
        rollback_log.append(sha)
        return {"tag": f"rollback-{sha[:10]}", "ref": "refs/tags/rollback-x"}

    return lambda: build_graph(
        classify=smoke_classify,
        gather=smoke_gather,
        rca=smoke_rca,
        rollback_tool=fake_rollback,
    )


def _fixture(expected: str):
    for f in build_fixtures():
        if f.delivered and f.ground["expected_decision"] == expected:
            return f
    raise AssertionError(f"no fixture with expected decision {expected!r}")


async def _drain_until(reader, target: str, timeout_s: float = 5.0) -> list[dict]:
    events = []
    while True:
        ev = await asyncio.wait_for(reader.get(), timeout=timeout_s)
        events.append(ev)
        if ev["type"] == target:
            return events


# --- T-7.1 / T-7.2: driver voice + wheel --------------------------------------------------


def test_driver_full_approve_cycle_events_and_rollback_once():
    rollback_log: list = []

    async def main():
        bus = EventBus()
        awaiter = GateAwaiter()
        driver = LiveDriver(bus, awaiter, make_graph=_smoke_graph(rollback_log), mode="real")
        reader = bus.subscribe()
        fixture = _fixture("approve")
        iid = fixture.alert["incident_id"]
        task = asyncio.create_task(driver.run_once(fixture.alert))
        try:
            evs = await _drain_until(reader, "gate_open")
            # Pre-create the future (idempotent — the driver awaits the same
            # one), then assert it is truly parked and nothing fired pre-gate.
            awaiter.wait(iid)
            assert awaiter.pending() == [iid]
            assert rollback_log == []  # NG-1: nothing fired before approval
            # B1 timing honesty: the operator's decision wait (simulated here)
            # must NEVER be inherited by the post-gate rollback stage.
            await asyncio.sleep(0.5)
            assert awaiter.resolve(iid, {"decision": "approve", "actor": "ops"}) is True
            evs += await _drain_until(reader, "done")
            await asyncio.wait_for(task, timeout=5.0)
            return evs
        finally:
            task.cancel()

    events = asyncio.run(main())
    kinds = [e["type"] for e in events]
    assert kinds == [
        "classified", "escalating", "tool_call", "plan", "gate_open",
        "rollback", "done",
    ]
    gate = next(e for e in events if e["type"] == "gate_open")
    assert gate["waiting"] is True and gate["plan"].get("requires_approval")
    rollback = next(e for e in events if e["type"] == "rollback")
    assert rollback["rollback_result"]["tag"].startswith("rollback-")
    done = events[-1]
    assert done["terminal"] == "resolved" and done["t_resolve_ms"] >= 0
    assert len(rollback_log) == 1

    # B1 (2026-09-28): honest provenance on every milestone — the operator must
    # be able to see which model ran, which runtime, and how long each stage
    # took. If any stamp is dropped, this fails loudly instead of the console
    # silently showing "—".
    classified = next(e for e in events if e["type"] == "classified")
    plan = next(e for e in events if e["type"] == "plan")
    tool_call = next(e for e in events if e["type"] == "tool_call")
    for ev in [classified, plan, tool_call, gate, rollback]:
        assert ev["mode"] == "real", "runtime label must reach every milestone"
        assert ev["stage_duration_ms"] is not None and ev["stage_duration_ms"] >= 0
    assert classified["model_env"] == (os.environ.get("NEXUSOPS_SLM_MODEL") or "slm")
    assert plan["model_env"] == (os.environ.get("NEXUSOPS_FRONTIER_MODEL") or "frontier")
    # The post-gate stage restarts the clock at resume: the 0.5 s human wait
    # above must NOT be claimed by the rollback stage (B1 timing honesty).
    assert rollback["stage_duration_ms"] < 200, (
        f"rollback stage inherited the decision wait: {rollback['stage_duration_ms']} ms"
    )
    # smoke_gather returns one row per tool, each tagged with its origin (B1).
    assert {r.get("_tool") for r in tool_call["evidence"]} == {
        "fetch_service_logs", "query_prometheus_metrics",
    }


def test_rca_operator_slm_forces_stamp_and_status(monkeypatch):
    """T-7.11 (2026-09-28): NEXUSOPS_RCA_MODEL=slm must be honored by BOTH
    sides that read it — the consuming graph node (state_machine) and the
    honest stamp (serve._inspect). The plan + escalating events must name the
    SLM, never the frontier, and /api/status must report the active choice.
    One env, both sides: the plan row can never name a model that didn't write
    the plan (same provenance contract as classify, B1)."""
    monkeypatch.setenv("NEXUSOPS_SLM_MODEL", "slm-probe")
    monkeypatch.setenv("NEXUSOPS_FRONTIER_MODEL", "ff-probe")
    monkeypatch.setenv("NEXUSOPS_RCA_MODEL", "slm")

    async def main():
        bus = EventBus()
        awaiter = GateAwaiter()
        driver = LiveDriver(bus, awaiter, make_graph=_smoke_graph([]))
        reader = bus.subscribe()
        fixture = _fixture("approve")  # severity_hint critical -> escalates
        iid = fixture.alert["incident_id"]
        task = asyncio.create_task(driver.run_once(fixture.alert))
        try:
            evs = await _drain_until(reader, "gate_open")
            awaiter.wait(iid)
            assert awaiter.resolve(iid, {"decision": "approve", "actor": "ops"}) is True
            evs += await _drain_until(reader, "done")
            await asyncio.wait_for(task, timeout=5.0)
            return evs
        finally:
            task.cancel()

    events = asyncio.run(main())
    plan = next(e for e in events if e["type"] == "plan")
    escalating = next(e for e in events if e["type"] == "escalating")
    assert plan["model_env"] == "slm-probe", "plan must be stamped SLM under --slm-rca"
    assert escalating["depth_model"] == "slm-probe", "depth model must reflect the override"

    # /api/status reports the choice the operator actually made.
    with TestClient(create_serve_app(run_worker=False)) as client:
        assert client.get("/api/status").json()["rca_model"] == "slm-probe"


def test_driver_rerun_of_same_incident_parks_fresh_no_stale_decision():
    """Postmortem 2026-09-23: a forced re-run of the same incident_id used to
    `await` the PREVIOUS run's already-done gate future, silently resuming with
    the stale approve decision — rollback fired with no human decision that run
    (NG-1). `GateAwaiter.reset` at run start + idempotent `wait` means run 2
    parks at a FRESH gate and waits for a NEW decision."""
    rollback_log: list = []

    async def main():
        bus = EventBus()
        awaiter = GateAwaiter()
        driver = LiveDriver(bus, awaiter, make_graph=_smoke_graph(rollback_log))
        reader = bus.subscribe()
        fixture = _fixture("approve")
        iid = fixture.alert["incident_id"]

        # run 1: approve, completes
        task = asyncio.create_task(driver.run_once(fixture.alert))
        try:
            await _drain_until(reader, "gate_open")
            awaiter.wait(iid)  # pre-create (idempotent) — settles the timing race
            assert awaiter.pending() == [iid]
            assert awaiter.resolve(iid, {"decision": "approve", "actor": "ops"}) is True
            await _drain_until(reader, "done")
            await asyncio.wait_for(task, timeout=5.0)
            assert len(rollback_log) == 1

            # run 2 (force re-run): must park again with a FRESH gate
            task = asyncio.create_task(driver.run_once(fixture.alert))
            try:
                await _drain_until(reader, "gate_open")
                awaiter.wait(iid)  # pre-create — same fresh future the driver awaits
                assert awaiter.pending() == [iid]  # genuinely parked, awaiting human
                assert len(rollback_log) == 1  # NG-1: nothing auto-fired yet
                assert awaiter.resolve(iid, {"decision": "approve", "actor": "ops2"}) is True
                await _drain_until(reader, "done")
                await asyncio.wait_for(task, timeout=5.0)
            finally:
                task.cancel()
        finally:
            task.cancel()

    asyncio.run(main())
    assert len(rollback_log) == 2  # exactly one rollback per APPROVED run


def test_driver_reject_cycle_never_calls_rollback():
    rollback_log: list = []

    async def main():
        bus = EventBus()
        awaiter = GateAwaiter()
        driver = LiveDriver(bus, awaiter, make_graph=_smoke_graph(rollback_log))
        reader = bus.subscribe()
        fixture = _fixture("reject")
        task = asyncio.create_task(driver.run_once(fixture.alert))
        try:
            evs = await _drain_until(reader, "gate_open")
            awaiter.wait(fixture.alert["incident_id"])
            assert awaiter.resolve(fixture.alert["incident_id"], {"decision": "reject", "actor": "ops"})
            evs += await _drain_until(reader, "done")
            await asyncio.wait_for(task, timeout=5.0)
            return evs
        finally:
            task.cancel()

    events = asyncio.run(main())
    kinds = [e["type"] for e in events]
    assert "rollback" not in kinds
    assert events[-1]["terminal"] == "rejected"
    assert rollback_log == []  # NG-1: rejected incident reaches no tool


def test_driver_honest_outage_no_gate_no_hang():
    async def failing_rca(messages, model_env, schema):
        raise RuntimeError("frontier down (503 high demand)")

    async def main():
        bus = EventBus()
        awaiter = GateAwaiter()
        graph = lambda: build_graph(
            classify=smoke_classify, gather=smoke_gather, rca=failing_rca
        )
        driver = LiveDriver(bus, awaiter, make_graph=graph)
        reader = bus.subscribe()
        fixture = _fixture("approve")  # critical -> escalates -> rca fails
        await asyncio.wait_for(driver.run_once(fixture.alert), timeout=5.0)  # no hang
        kinds = []
        while not reader.empty():
            kinds.append(reader.get_nowait()["type"])
        assert "gate_open" not in kinds  # D-6: no gate for a failed run
        assert "manual_review" in kinds
        assert kinds[-1] == "done"
        assert awaiter.pending() == []  # never waited on a human

    asyncio.run(main())


def test_driver_slm_down_manual_review_from_classify():
    async def failing_classify(messages, model_env, schema):
        raise RuntimeError("SLM unavailable")

    async def main():
        bus = EventBus()
        awaiter = GateAwaiter()
        graph = lambda: build_graph(
            classify=failing_classify, gather=smoke_gather, rca=smoke_rca
        )
        driver = LiveDriver(bus, awaiter, make_graph=graph)
        reader = bus.subscribe()
        fixture = _fixture("approve")
        await asyncio.wait_for(driver.run_once(fixture.alert), timeout=5.0)
        kinds = []
        while not reader.empty():
            kinds.append(reader.get_nowait()["type"])
        assert kinds[0] == "manual_review"      # loud reason, immediately
        assert kinds[-1] == "done"

    asyncio.run(main())


def test_gate_awaiter_first_decision_wins():
    awaiter = GateAwaiter()

    async def main():
        fut = awaiter.wait("inc-1")
        assert awaiter.resolve("inc-1", {"decision": "approve", "actor": "ops"}) is True
        assert await asyncio.wait_for(fut, timeout=1.0) == {"decision": "approve", "actor": "ops"}
        assert awaiter.resolve("inc-1", {"decision": "reject", "actor": "ops"}) is False  # already_decided
        assert awaiter.resolve("unknown-9", {"decision": "approve", "actor": "ops"}) is False  # not awaiting
        assert awaiter.pending() == []

    asyncio.run(main())


def test_consume_loop_idle_timeout_is_silent_and_graceful_stop_is_prompt(caplog):
    """Postmortems 2026-09-23: async redis-py brpop(timeout=5) RAISES
    TimeoutError on natural idle — it never returns None, so the worker's
    steady state is one TimeoutError per quiet boundary. That idle heartbeat
    must be SILENT (no 'worker fetch failed' warning, no phantom `?` incident),
    and the loop must re-check shutdown_event at every boundary so graceful
    stop always completes — the old generic except swallowed this TimeoutError
    and re-looped past the event, causing the observed `await task` hang /
    "SIGTERM ignored, needed SIGKILL" on two servers."""

    calls = 0

    class _IdleTimeout:
        """brpop that behaves like the real async client on an idle queue.

        Must include an await point (as a real socket read does): the consume
        loop otherwise spins CPU-bound, never yields, and the test can't set
        the shutdown event — a deadlock the real worker can't have."""

        async def brpop(self, keys, timeout=0):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.001)
            raise redis_sync.exceptions.TimeoutError("Timeout reading from localhost:6379")

    async def main():
        bus = EventBus()
        awaiter = GateAwaiter()
        driver = LiveDriver(bus, awaiter, make_graph=_smoke_graph([]))
        reader = bus.subscribe()
        stop = asyncio.Event()
        task = asyncio.create_task(consume_loop(_IdleTimeout(), driver, stop))
        try:
            await asyncio.sleep(0.08)  # several idle boundaries land
            assert calls >= 2  # heartbeat keeps beating, loop did not die
            assert reader.empty()  # no phantom "?" incident event
            # idle is NOT a failure: the warning branch stays silent
            assert not [
                r for r in caplog.records if "worker fetch failed" in r.getMessage()
            ], "every-5s idle TimeoutError must not log as a fetch failure"
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)  # graceful, no hang
        finally:
            if not task.done():
                task.cancel()

    with caplog.at_level(logging.WARNING):
        asyncio.run(main())


def test_make_resume_raises_when_nothing_awaiting():
    awaiter = GateAwaiter()
    resume = _make_resume(awaiter)

    async def main():
        with pytest.raises(ValueError):
            await resume("never-parked", {"decision": "approve", "actor": "ops"})

    asyncio.run(main())


# --- T-7.3: serve app composition + T-7.5 integration ------------------------------


def test_serve_fixtures_api_leaks_no_ground_truth():
    with TestClient(create_serve_app(run_worker=False)) as client:
        cats = client.get("/api/fixtures").json()
    assert cats, "catalog must not be empty"
    # D-16: the problem envelope (summary/context/status_code) rides the picker —
    # it is sender-provided alert data, NOT ground truth, so it is safe to
    # expose. Everything else stays out: the operator picks and decides blind.
    allowed = {"incident_id", "occurred_at", "source", "service", "message", "severity_hint",
               "status_code", "summary", "context"}
    for item in cats:
        assert set(item) == allowed
        assert "expected" not in item and "ground" not in item  # NG-1: operator decides
    # the flagship problem envelopes are actually there (a rich incident phase
    # requires the data to exist, not just the field to be allowed)
    assert cats[0]["summary"] and isinstance(cats[0]["context"], dict) and len(cats[0]["context"]) >= 1


def test_serve_status_reports_redis_and_queue():
    with TestClient(create_serve_app(run_worker=False)) as client:
        status = client.get("/api/status").json()
    assert status["redis"] == "ok"
    assert status["queue_depth"] == 0
    assert status["gates_waiting"] == []
    # T-7.11: the operator must see which model writes plans. Default is the
    # frontier (D-4); the slm override is covered by the driver-level test.
    assert status["rca_model"] == (os.environ.get("NEXUSOPS_FRONTIER_MODEL") or "frontier")


def test_serve_webhook_publishes_ingest_and_duplicate_is_quiet():
    bus = EventBus()
    with TestClient(create_serve_app(run_worker=False, bus=bus)) as client:
        alert = {"incident_id": str(uuid.uuid4()), "occurred_at": "2026-09-23T12:00:00Z",
                 "source": "prometheus", "service": "auth", "message": "5xx spike",
                 "severity_hint": "critical", "status_code": 500,
                 "summary": "Sign-in 5xx spike since 11:40Z.",
                 "context": {"affected_endpoints": ["/v1/signin"], "error_rate_pct": 6.8}}
        first = client.post("/webhook/incident", json=alert)
        second = client.post("/webhook/incident", json=alert)
    assert first.status_code == 202 and first.json()["duplicate"] is False
    assert second.status_code == 202 and second.json()["duplicate"] is True
    assert {"type", "incident_id", "seq", "source", "service", "message", "severity_hint"} <= set(
        bus._history[alert["incident_id"]][0]
    ) and bus._history[alert["incident_id"]][0]["type"] == "ingest"
    # D-16: the FULL alert envelope is published, not the old four fields —
    # the operator sees occurred_at, status_code, summary and context.
    ing = bus._history[alert["incident_id"]][0]
    assert ing["occurred_at"] == "2026-09-23T12:00:00Z"
    assert ing["status_code"] == 500
    assert ing["summary"] == alert["summary"]
    assert ing["context"] == alert["context"]
    # a duplicate publishes NO second `ingest` event (quiet, FR-1)
    assert len(bus._history[alert["incident_id"]]) == 1


def test_serve_webhook_force_bypasses_dedupe_for_console_rehearsal():
    """D:11 console rehearsal path (postmortem 2026-09-23): `force=1` must
    re-enqueue a fixture id the seen-set already knows, so "fire again" works
    in one session. The default (no force) stays dedupe-by-default (FR-1)."""
    bus = EventBus()
    with TestClient(create_serve_app(run_worker=False, bus=bus)) as client:
        alert = {"incident_id": "c-01", "occurred_at": "2026-09-23T12:00:00Z",
                 "source": "prometheus", "service": "db", "message": "pool exhausted",
                 "severity_hint": "critical"}
        first = client.post("/webhook/incident", json=alert)          # seeds seen-set
        forced = client.post("/webhook/incident?force=1", json=alert)  # re-run
        dup = client.post("/webhook/incident", json=alert)             # still deduped path
    assert first.json()["duplicate"] is False
    assert forced.json()["duplicate"] is False   # force: enqueued anyway
    assert dup.json()["duplicate"] is True       # default contract unchanged
    assert len(bus._history[alert["incident_id"]]) == 2  # ingest only for the two adds
    # the routinely-ingested alert got a second `ingest` (the re-run is real work)
    from app.ingest import QUEUE_KEY
    assert bus._history[alert["incident_id"]][1]["type"] == "ingest"


def test_full_live_cycle_via_worker_and_ws_human_approve():
    """The complete demo promise: webhook -> real pipeline -> live stages on the
    socket -> human approve -> rollback -> done; second decision refused."""
    log: list = []

    def make_graph():
        from app.state_machine import build_graph

        async def fake_rollback(sha: str) -> dict:
            log.append(sha)
            return {"tag": f"rollback-{sha[:10]}", "ref": "refs/tags/rollback-x"}

        return build_graph(classify=smoke_classify, gather=smoke_gather,
                           rca=smoke_rca, rollback_tool=fake_rollback)

    fixture = _fixture("approve")
    with TestClient(create_serve_app(run_worker=True, make_graph=make_graph)) as client:
        with client.websocket_connect("/ws") as ws:
            resp = client.post("/webhook/incident", json=fixture.alert)
            assert resp.status_code == 202

            kinds = []
            while "gate_open" not in kinds and len(kinds) < 8:
                ev = ws.receive_json()
                kinds.append(ev["type"])
            assert kinds[:5] == ["ingest", "classified", "escalating", "tool_call", "plan"]
            assert kinds[5] == "gate_open"

            ws.send_json({"incident_id": fixture.alert["incident_id"],
                          "decision": "approve", "actor": "ops"})
            tail = []
            while "done" not in tail and len(tail) < 8:
                tail.append(ws.receive_json()["type"])
            assert tail[:3] == ["decision", "rollback", "done"]
            assert len(log) == 1  # NG-1: exactly one rollback, post-approval

            # second decision: first wins -> loud error, no second rollback
            ws.send_json({"incident_id": fixture.alert["incident_id"],
                          "decision": "reject", "actor": "ops"})
            resp_events = []
            while "error" not in resp_events and len(resp_events) < 4:
                resp_events.append(ws.receive_json()["type"])
            assert "error" in resp_events  # already_decided surfaced loud
            assert len(log) == 1