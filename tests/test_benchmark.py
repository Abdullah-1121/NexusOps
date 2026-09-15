"""Feature 5 acceptance tests (spec §3 + §6): B-1 fixtures, B-2 judge,
B-3 report, B-4 gate, NFR-1 P95, NFR-2 strict plans, and the deferred
queue consumer (the Feature-3 add-when arrived)."""

import asyncio
import json

import pytest

from app import benchmark as bench
from app.models import ModelError

ALERT_FIELDS = ("severity", "affected_service", "root_cause_hypothesis", "confidence",
                "remediation_steps", "requires_approval", "evidence")


def _plan(ground: dict) -> dict:
    return {
        "severity": ground["severity"],
        "affected_service": "auth",
        "root_cause_hypothesis": ground["root_cause_hypothesis"],
        "confidence": 0.9,
        "remediation_steps": [
            {"action": "rollback", "target": "x", "reason": ground["root_cause_hypothesis"]}
        ],
        "requires_approval": True,
        "evidence": ["ev-1"],
    }


def _graph(rollback):
    return bench.build_graph(
        classify=bench.smoke_classify,
        rca=bench.smoke_rca,
        gather=bench.smoke_gather,
        rollback_tool=rollback,
    )


def _fake_judge(result):
    """Injected judge mirroring smoke_judge contract for report tests."""
    return bench.smoke_judge(
        [{"role": "user", "content": json.dumps(
            {"ground_truth": {"severity": result.severity,
                              "root_cause": result.fixture.ground["root_cause_hypothesis"],
                              "requires_gate": True},
             "produced": {"severity": result.severity, "plan": result.plan,
                          "terminal": result.terminal, "escalated": False,
                          "manual_review_reason": None, "decision": result.decision,
                          "nfr2_plan_ok": result.plan_ok}})}],
        "", None,
    )


# --- B-1 ---------------------------------------------------------------------


def test_fixtures_cover_b1_classes():
    fixtures = bench.build_fixtures()
    assert len(fixtures) == 30
    dup = [f for f in fixtures if not f.delivered]
    assert len(dup) == 1 and dup[0].alert["incident_id"] == "dup-01"
    ids = {f.alert["incident_id"] for f in fixtures}
    assert len(ids) == 30
    hints = {}
    for f in fixtures:
        if f.delivered:
            hints[f.alert["severity_hint"]] = hints.get(f.alert["severity_hint"], 0) + 1
    assert hints["critical"] >= 6 and hints["warning"] >= 8 and hints["info"] >= 6
    assert any(i.startswith("a-") for i in ids)  # ambiguous escalation class
    assert any(i.startswith("m-") for i in ids)  # malformed-log-source class
    assert any(f.ground["expected_decision"] == "reject" for f in fixtures)
    assert all(set(f.ground) == {"severity", "root_cause_hypothesis", "requires_gate", "expected_decision"}
               for f in fixtures)


# --- worker: approve / reject / ai-down / consume ----------------------------


def test_drive_incident_approve_reaches_resolved_and_rolls_back():
    calls = []

    async def rollback(sha):
        calls.append(sha)
        return {"status": "performed"}

    fixtures = bench.build_fixtures()
    f = next(x for x in fixtures if x.ground["expected_decision"] == "approve")
    res = asyncio.run(bench.drive_incident(_graph(rollback), f.alert, lambda g: g["expected_decision"], f))
    assert res.terminal == "resolved"
    assert res.t_gate_ms is not None
    assert res.decision["decision"] == "approve"
    assert calls == [res.plan["remediation_steps"][0]["target"]]


def test_drive_incident_reject_never_rolls_back():
    calls = []

    async def rollback(sha):
        calls.append(sha)
        return {"status": "performed"}

    fixtures = bench.build_fixtures()
    f = next(x for x in fixtures if x.ground["expected_decision"] == "reject")
    res = asyncio.run(bench.drive_incident(_graph(rollback), f.alert, lambda g: g["expected_decision"], f))
    assert res.terminal == "rejected"
    assert res.decision["decision"] == "reject"
    assert calls == []  # NG-1: no remediation on reject


def test_ai_down_routes_to_manual_review_and_never_gates():
    class Boom:
        async def __call__(self, messages, model_env, schema):
            raise RuntimeError("provider down")

    fixtures = bench.build_fixtures()
    f = next(x for x in fixtures if x.delivered)
    g = bench.build_graph(classify=Boom(), rca=Boom(), gather=bench.smoke_gather,
                          rollback_tool=lambda sha: {"status": "performed"})
    res = asyncio.run(bench.drive_incident(g, f.alert, lambda g_: "approve", f))
    assert res.manual_review_reason
    assert res.t_gate_ms is None  # D-6: never reaches the gate
    assert res.plan_ok is False
    assert bench.build_report([res], fixtures, bench.MetricSink(), [], mode="test")["all_pass"] is False


def test_consume_loop_drives_through_queue_seam():
    fixtures = bench.build_fixtures()
    alerts = [f.alert for f in fixtures if f.delivered][:3]
    idx = [0]

    async def pop():
        if idx[0] >= len(alerts):
            return None
        a = alerts[idx[0]]
        idx[0] += 1
        return a

    async def _rb(sha):
        return {"status": "performed"}

    results = asyncio.run(bench.consume_loop(pop, lambda: _graph(_rb),
                                             lambda g: g["expected_decision"], until=3))
    assert len(results) == 3
    assert all(r.terminal in ("resolved", "rejected") for r in results)


def test_consume_loop_strict_refuses_silent_partial_pass():
    async def pop():
        return None  # source dried up immediately

    with pytest.raises(RuntimeError, match="silent partial pass refused"):
        asyncio.run(bench.consume_loop(pop, lambda: _graph(_rb), lambda g: g["expected_decision"],
                                       until=3, strict=True))


# --- NFR-2 / report / exit code ----------------------------------------------


def test_plan_ok_enforces_nfr2_schema():
    assert bench._plan_ok(_plan({"severity": "x", "root_cause_hypothesis": "r"}))
    assert not bench._plan_ok({"severity": "x"})
    assert not bench._plan_ok(None)
    assert not bench._plan_ok({"severity": "x", "affected_service": "s", "root_cause_hypothesis": "r",
                               "confidence": 0.9, "remediation_steps": None,
                               "requires_approval": True, "evidence": []})


def test_report_aggregates_and_passes():
    fixtures = bench.build_fixtures()
    sink = bench.MetricSink()
    sink.usage("openrouter/auto", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    results, reviews = [], []
    for f in fixtures:
        if not f.delivered:
            continue
        res = bench.IncidentResult(
            incident_id=f.alert["incident_id"], fixture=f,
            severity=f.ground["severity"], plan=_plan(f.ground), terminal="resolved",
            t_gate_ms=100.0, t_resolve_ms=120.0, escalated=False,
            manual_review_reason=None, decision={"decision": "approve"}, plan_ok=True,
        )
        results.append(res)
        reviews.append(asyncio.run(_fake_judge(res)))
    report = bench.build_report(results, fixtures, sink, reviews, mode="test")
    assert report["all_pass"] is True
    assert report["nfr1_slm_only_p95_ms"] == 100.0
    assert report["pass_rate"] == 1.0
    assert report["mttr_ms"] == 120.0
    assert report["tokens_per_model"]["openrouter/auto"]["total"] == 15
    assert report["tool_calls"]["rollback"] == 0  # sink untouched in this fixture
    assert bench.exit_code(report) == 0


def test_p95_breach_fails_run():
    fixtures = bench.build_fixtures()
    f = next(x for x in fixtures if x.delivered)
    res = bench.IncidentResult(
        incident_id=f.alert["incident_id"], fixture=f, severity=f.ground["severity"],
        plan=_plan(f.ground), terminal="resolved", t_gate_ms=9000.0, t_resolve_ms=9500.0,
        escalated=False, manual_review_reason=None, decision={"decision": "approve"}, plan_ok=True,
    )
    report = bench.build_report([res], fixtures, bench.MetricSink(), [], mode="test")
    assert report["all_pass"] is False
    assert bench.exit_code(report) == 1


def test_manual_review_listed_and_fails_run():
    fixtures = bench.build_fixtures()
    f = next(x for x in fixtures if x.delivered)
    res = bench.IncidentResult(
        incident_id=f.alert["incident_id"], fixture=f, severity=f.ground["severity"], plan=None,
        terminal="manual_review", t_gate_ms=None, t_resolve_ms=50.0, escalated=False,
        manual_review_reason="provider down", decision=None, plan_ok=False,
    )
    report = bench.build_report([res], fixtures, bench.MetricSink(), [], mode="test")
    assert report["manual_review"] == [f.alert["incident_id"]]
    assert report["all_pass"] is False


def test_all_escalated_fails_nfr1_unmeasured():
    """Vacuous NFR-1 is a fail: no SLM-only incident measured means the target
    was never exercised, so the run must not go green."""
    fixtures = bench.build_fixtures()
    f = next(x for x in fixtures if x.delivered)
    res = bench.IncidentResult(
        incident_id=f.alert["incident_id"], fixture=f, severity=f.ground["severity"],
        plan=_plan(f.ground), terminal="resolved", t_gate_ms=10.0, t_resolve_ms=15.0,
        escalated=True, manual_review_reason=None, decision={"decision": "approve"}, plan_ok=True,
    )
    report = bench.build_report([res], fixtures, bench.MetricSink(), [], mode="test")
    assert report["nfr1_measured"] is False
    assert report["all_pass"] is False
    assert bench.exit_code(report) == 1


def test_live_model_calls_feed_usage_sink(monkeypatch):
    """Bug regression: the live model fn must pass usage_sink, else
    tokens_per_model is silently empty."""
    sink = bench.MetricSink()
    seen = {}

    async def fake_complete_json(messages, *, model_env, timeout_s, json_schema, usage_sink=None):
        seen["usage_sink"] = usage_sink
        if usage_sink:
            usage_sink("openrouter/auto", {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        return {"ok": True}

    monkeypatch.setattr(bench, "complete_json", fake_complete_json)
    model = bench._instrumented_complete(sink)
    asyncio.run(model([], "NEXUSOPS_SLM_MODEL", {}))
    assert seen["usage_sink"].__self__ is sink
    assert sink.model_calls == [{"model": "openrouter/auto",
                                 "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}]


def test_judge_failure_marks_incident_failed_not_crash():
    """A judge outage must produce failing verdicts in the report, never throw
    (a 30-min run dying with zero output is the silent-loss class we refuse)."""

    async def bad_judge(*a, **k):
        raise ModelError("judge down")

    f = next(x for x in bench.build_fixtures() if x.delivered)
    res = bench.IncidentResult(
        incident_id=f.alert["incident_id"], fixture=f, severity="critical",
        plan=_plan(f.ground), terminal="resolved", t_gate_ms=1.0, t_resolve_ms=2.0,
        escalated=False, manual_review_reason=None, decision={"decision": "approve"}, plan_ok=True,
    )
    reviews = asyncio.run(bench._judge_all(bad_judge, [res]))
    assert all(r["severity_match"]["verdict"] is False for r in reviews)
    assert "judge failed" in reviews[0]["severity_match"]["reason"]


def test_retry_model_retries_transient_then_succeeds():
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ModelError("429 too many requests", status_code=429, retryable=True)
        return {"ok": True}

    wrapped = bench._retry_model(flaky, retries=3, base_sleep=0.01)
    assert asyncio.run(wrapped()) == {"ok": True}
    assert calls["n"] == 3


def test_retry_model_never_hides_hard_errors():
    calls = {"n": 0}

    async def hard(*a, **k):
        calls["n"] += 1
        raise ModelError("402 no credits", status_code=402, retryable=False)

    wrapped = bench._retry_model(hard, retries=3, base_sleep=0.01)
    with pytest.raises(ModelError, match="402 no credits"):
        asyncio.run(wrapped())
    assert calls["n"] == 1  # never retried a hard error


# --- determinism (NFR-5) -----------------------------------------------------


def test_smoke_fixtures_are_static_and_repeatable():
    first = bench.build_fixtures()
    bench._FIXTURES = None
    second = bench.build_fixtures()
    assert [(f.alert["incident_id"], f.ground) for f in second] == \
           [(f.alert["incident_id"], f.ground) for f in first]


def test_smoke_judge_contract():
    fixtures = bench.build_fixtures()
    f = next(x for x in fixtures if x.delivered)
    res = bench.IncidentResult(
        incident_id=f.alert["incident_id"], fixture=f, severity=f.ground["severity"],
        plan=_plan(f.ground), terminal="resolved", t_gate_ms=10.0, t_resolve_ms=15.0,
        escalated=False, manual_review_reason=None, decision={"decision": "approve"}, plan_ok=True,
    )
    out = asyncio.run(bench.judge_result(bench.smoke_judge, res))
    assert list(out) == ["severity_match", "root_cause_match", "plan_actionability", "gate_compliance"]
    for metric in out.values():
        assert set(metric) == {"verdict", "score", "reason"}
        assert isinstance(metric["verdict"], bool)
    assert all(m["verdict"] for m in out.values())