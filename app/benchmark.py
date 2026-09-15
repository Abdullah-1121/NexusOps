"""NexusOps — Feature 5: benchmark & regression harness.

Phase 1 decision (2026-09-08): LLM-as-judge with **structured scored output**
via the existing OpenRouter `complete_json` (strict JSON, frontier model), NOT
the DeepEval dependency. Structured shape is deterministic (NFR-5 inputs +
mechanical asserts fixed); semantic verdicts carry a `reason` and are
auditable in the report. NFR-2 (strict-schema plans) and the no-silent-failure
rule are hard deterministic asserts beneath the judge.

Modes:
  --smoke   injected deterministic model/evidence fakes (CI-safe, zero tokens)
  (default) real OpenRouter models + real MCP evidence over stdio (needs key)

The benchmark is the first real consumer of the Feature-3-deferred Redis
queue: it runs the worker (`consume_loop` over a `pop_alert` seam) with the
auto-operator resuming each parked gate with the fixture's expected decision.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from langgraph.types import Command

from app.models import ModelError, complete_json
from app.state_machine import build_graph

# --- B-1 fixtures -----------------------------------------------------------


@dataclass
class Fixture:
    alert: dict
    ground: dict  # severity, root_cause_hypothesis, requires_gate, expected_decision
    delivered: bool = True  # False => duplicate: deduped at ingest, never queued


def _mk(iid: str, service: str, message: str, hint: str, gsev: str, rc: str) -> Fixture:
    return Fixture(
        alert={
            "incident_id": iid,
            "occurred_at": "2026-09-08T12:00:00Z",
            "source": "prometheus",
            "service": service,
            "severity_hint": hint,
            "status_code": 0,
            "message": message,
        },
        ground={
            "severity": gsev,
            "root_cause_hypothesis": rc,
            "requires_gate": True,
            "expected_decision": "approve",
        },
    )


# 30 static fixtures covering B-1. No RNG — NFR-5 = fixed data.
_RAW = [
    # (id, service, message, hint, ground_severity, root_cause)
    ("c-01", "db", "connection pool exhausted", "critical", "critical", "connection pool leakage"),
    ("c-02", "auth", "5xx spike on sign-in", "critical", "critical", "bad auth deploy"),
    ("c-03", "search", "latency p99 >5s", "critical", "critical", "shard rebalance overload"),
    ("c-04", "payments", "transaction failures rising", "critical", "critical", "database deadlock storm"),
    ("c-05", "api", "memory RSS climbing", "critical", "critical", "leaky long-lived request handler"),
    ("c-06", "queue", "consumer lag high", "critical", "warning", "producer backpressure"),
    ("w-01", "db", "replication lag >10s", "warning", "warning", "secondary index rebuild"),
    ("w-02", "auth", "token refresh errors", "warning", "warning", "clock skew on node"),
    ("w-03", "search", "query timeout ratio up", "warning", "warning", "missing index on filter field"),
    ("w-04", "payments", "webhook retries doubled", "warning", "warning", "downstream API instability"),
    ("w-05", "api", "cpu utilization high", "warning", "warning", "burst traffic to hot partition"),
    ("w-06", "queue", "dead-letter count up", "warning", "warning", "malformed messages from client"),
    ("w-07", "db", "slow query count rising", "warning", "warning", "plan regression on join"),
    ("w-08", "auth", "federated login failures", "warning", "warning", "SAML metadata stale"),
    ("w-09", "worker", "agent pool saturation", "warning", "warning", "autoscaling threshold too low"),
    ("i-01", "db", "background vacuum running", "info", "info", "routine maintenance"),
    ("i-02", "search", "cache hit rate dipped", "info", "info", "cache warmup after rollout"),
    ("i-03", "api", "minor 429s on one route", "info", "info", "rate-limiter tuning"),
    ("i-04", "queue", "one retry observed", "info", "info", "transient upstream timeout"),
    ("i-05", "auth", "external IDP latency", "info", "info", "third-party slowness"),
    ("i-06", "payments", "partial digit failure", "info", "warning", "flaky PCI validation"),
    # ambiguous-severity -> deterministic escalation path (D-4, ambiguous flag)
    ("a-01", "db", "error rate 0.1% unclear", None, "warning", "unclear root cause held at gate"),
    ("a-02", "auth", "intermittent 401 odd pattern", None, "critical", "possible token introspection breach"),
    ("a-03", "payments", "refund flow under investigation", None, "warning", "reconciliation mismatch"),
    ("a-04", "api", "spiky latency unexplained", None, "warning", "noisy-neighbor on shared pod"),
    ("a-05", "search", "partial result gaps", None, "critical", "index slice corruption suspicion"),
    # malformed-log-source -> evidence comes back empty; plan must still be actionable
    ("m-01", "dangling", "log source rotated mid-stream", "warning", "warning", "evidence absent, suppress noise"),
    ("m-02", "orphan", "metrics source gone dark", "info", "info", "source decommissioning"),
    ("m-03", "ghost", "alert with no backing logs", "warning", "info", "stale alert rule"),
]

_FIXTURES: list[Fixture] | None = None


def build_fixtures() -> list[Fixture]:
    global _FIXTURES
    if _FIXTURES is None:
        _FIXTURES = [_mk(*row) for row in _RAW]
        dup = _mk("dup-01", "db", "connection pool exhausted", "critical", "critical", "connection pool leakage")
        dup.delivered = False
        _FIXTURES.append(dup)
        assert len(_FIXTURES) == 30
        # human realism: a share of incidents are rejected at the gate, so the
        # benchmark exercises both §5.4 branches (approve and reject)
        for f in _FIXTURES:
            if f.alert["incident_id"] in {"w-04", "w-07", "i-03", "i-05", "m-02"}:
                f.ground["expected_decision"] = "reject"
    return _FIXTURES


def _ground_for(incident_id: str) -> dict | None:
    for f in build_fixtures():
        if f.alert["incident_id"] == incident_id:
            return f.ground
    return None


# --- deterministic smoke sinks (mode --smoke) --------------------------------

async def smoke_classify(messages: list[dict], model_env: str, schema: dict) -> dict:
    alert = json.loads(messages[-1]["content"].split("QUERY: ", 1)[1])
    ground = _ground_for(alert["incident_id"]) or {}
    iid = alert["incident_id"]
    return {
        "severity": ground.get("severity", "info"),
        "affected_service": alert.get("service"),
        "triage_confidence": 0.95,
        "ambiguous": iid.startswith("a-"),  # ambiguous fixtures deterministically escalate (D-4)
    }


async def smoke_gather(incident: dict) -> list[dict]:
    if incident["incident_id"].startswith("m-"):  # malformed-log-source: empty evidence
        return []
    return [{"timestamp": "2026-09-08T11:59:00Z", "level": "error", "message": "fixture evidence"}]


async def smoke_rca(messages: list[dict], model_env: str, schema: dict) -> dict:
    alert = json.loads(messages[0]["content"].split("Incident: ", 1)[1].split(" Evidence:", 1)[0])
    ground = _ground_for(alert["incident_id"]) or {}
    iid = alert["incident_id"]
    return {
        "severity": ground.get("severity", "info"),
        "affected_service": alert.get("service"),
        "root_cause_hypothesis": ground.get("root_cause_hypothesis", "fixture cause"),
        "confidence": 0.9,
        "remediation_steps": [
            {
                "action": "rollback" if ground.get("expected_decision") == "approve" else "noop",
                "target": f"sha-{iid}",
                "reason": ground.get("root_cause_hypothesis", "fixture cause"),
            }
        ],
        "requires_approval": True,
        "evidence": ["ev-1"] if not iid.startswith("m-") else [],
    }


async def smoke_judge(messages: list[dict], model_env: str, schema: dict) -> dict:
    payload = json.loads(messages[-1]["content"])
    produced = payload["produced"]
    ground = payload["ground_truth"]
    severity_ok = produced.get("severity") == ground["severity"]
    plan_ok = bool(
        produced.get("plan") and produced["plan"].get("remediation_steps")
    )
    gate_ok = produced.get("terminal") in ("resolved", "rejected") and not produced.get("manual_review_reason")
    return {
        "severity_match": {"verdict": severity_ok, "score": 1.0 if severity_ok else 0.0, "reason": "deterministic smoke"},
        "root_cause_match": {
            "verdict": plan_ok and produced.get("plan", {}).get("root_cause_hypothesis") == ground["root_cause"],
            "score": 1.0,
            "reason": "deterministic smoke",
        },
        "plan_actionability": {"verdict": plan_ok, "score": 1.0, "reason": "deterministic smoke"},
        "gate_compliance": {"verdict": gate_ok, "score": 1.0, "reason": "deterministic smoke"},
    }


# --- instrumented sink -------------------------------------------------------


@dataclass
class MetricSink:
    model_calls: list[dict] = field(default_factory=list)
    rollback_calls: list[str] = field(default_factory=list)
    evidence_calls: list[dict] = field(default_factory=list)

    def usage(self, model: str, usage: dict) -> None:
        self.model_calls.append({"model": model, "usage": usage})

    async def rollback(self, sha: str) -> dict:  # awaitable recording tool
        self.rollback_calls.append(sha)
        return {"status": "performed"}


# --- worker ------------------------------------------------------------------


@dataclass
class IncidentResult:
    incident_id: str
    fixture: Fixture | None
    severity: str | None
    plan: dict | None
    terminal: str | None
    t_gate_ms: float | None  # alert -> parked at gate (None if manual_review)
    t_resolve_ms: float | None  # alert -> terminal
    escalated: bool
    manual_review_reason: str | None
    decision: dict | None
    plan_ok: bool  # NFR-2 strict-schema plan


async def drive_incident(graph, alert: dict, operator: Callable, fixture: Fixture | None) -> IncidentResult:
    """Run one incident to terminal: gate parks, auto-operator resumes with the
    fixture's expected decision, final state captured at the checkpointer."""
    t0 = time.monotonic()
    config = {"configurable": {"thread_id": alert["incident_id"]}}
    await graph.ainvoke({"incident_id": alert["incident_id"], "alert": alert}, config)
    final = graph.get_state(config).values

    t_gate = None
    if final.get("terminal") is None and not final.get("manual_review_reason"):
        t_gate = time.monotonic() - t0  # parked at gate (interrupt returned cleanly)

    decision = None
    if final.get("manual_review_reason"):
        decision = None
    elif final.get("terminal") is None:
        ground = fixture.ground if fixture else {}
        decision = {"decision": operator(ground), "actor": "benchmark"}
        await graph.ainvoke(Command(resume=decision), config)
        final = graph.get_state(config).values

    return IncidentResult(
        incident_id=alert["incident_id"],
        fixture=fixture,
        severity=final.get("severity"),
        plan=final.get("plan"),
        terminal=final.get("terminal"),
        t_gate_ms=(t_gate * 1000.0) if t_gate is not None else None,
        t_resolve_ms=(time.monotonic() - t0) * 1000.0,
        escalated=bool(final.get("escalated")),
        manual_review_reason=final.get("manual_review_reason"),
        decision=decision,
        plan_ok=_plan_ok(final.get("plan")),
    )


def _plan_ok(plan: dict | None) -> bool:
    """NFR-2: remediation plan is a strict §5.3 dict. Deterministic assert."""
    if not isinstance(plan, dict):
        return False
    required = {
        "severity", "affected_service", "root_cause_hypothesis", "confidence",
        "remediation_steps", "requires_approval", "evidence",
    }
    if not required <= set(plan):
        return False
    steps = plan["remediation_steps"]
    return (
        isinstance(steps, list)
        and all(isinstance(s, dict) and {"action", "target", "reason"} <= set(s) for s in steps)
    )


async def consume_loop(pop_alert: Callable, make_graph: Callable, operator: Callable,
                       until: int, strict: bool = False,
                       progress: Callable[[int, int], None] | None = None) -> list[IncidentResult]:
    """First real consumer of the Feature-3-deferred queue (over a `pop_alert`
    seam: real BRPOP live, fixture stream in smoke/tests). One graph per
    incident thread (NFR-3). Ends when `until` delivered incidents are done or
    the source dries up. `strict` (live mode): a source that dries up early is
    a loud failure, never a silent partial pass. `progress(i, until)` (live)
    reports each finished incident so a long run has an inspectable heartbeat."""
    results = []
    while len(results) < until:
        alert = await pop_alert()
        if alert is None:
            if strict:
                raise RuntimeError(
                    f"queue drained after {len(results)}/{until} incidents — silent partial pass refused"
                )
            break
        results.append(await drive_incident(make_graph(), alert, operator, _fixture_by_id(alert)))
        if progress is not None:
            progress(len(results), until)
    return results


def _fixture_by_id(alert: dict) -> Fixture | None:
    for f in build_fixtures():
        if f.alert["incident_id"] == alert["incident_id"]:
            return f
    return None


def _ground(fixture: Fixture | None) -> dict | None:
    return fixture.ground if fixture else None


def _live_pop(redis, queue_key: str):
    async def pop() -> dict | None:
        item = await redis.brpop([queue_key], timeout=30)
        return json.loads(item[1]) if item else None

    return pop


# --- judge -------------------------------------------------------------------


def _metric_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "verdict": {"type": "boolean"},
            "score": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["verdict", "score", "reason"],
        "additionalProperties": False,
    }


JUDGE_METRICS = ("severity_match", "root_cause_match", "plan_actionability", "gate_compliance")
"""Single source of truth for the judge's metric names — live schema, smoke
judge, aggregation, and the error fallback all read it, so a naming mismatch
(the bug that once lost a full live run) is structurally impossible."""


def _judge_schema() -> dict:
    return {
        "type": "object",
        "properties": {m: _metric_schema() for m in JUDGE_METRICS},
        "required": list(JUDGE_METRICS),
        "additionalProperties": False,
    }


JUDGE_SCHEMA = _judge_schema()


async def judge_result(judge: Callable, result: IncidentResult) -> dict:
    """One frontier-model call grading the produced incident vs ground truth.

    Structured output (strict JSON, temperature 0 via complete_json) is the
    determinism stance: same response shape every run; every verdict carries a
    reason the report ships for human review.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are an SRE benchmark judge. Grade the produced incident against "
                "ground truth. Respond with strict JSON per the schema: four metric "
                "objects, each {verdict: bool, score: 0..1, reason: str}."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "ground_truth": {
                        "severity": result.fixture.ground["severity"] if result.fixture else None,
                        "root_cause": result.fixture.ground["root_cause_hypothesis"] if result.fixture else None,
                        "requires_gate": result.fixture.ground["requires_gate"] if result.fixture else True,
                    },
                    "produced": {
                        "severity": result.severity,
                        "plan": result.plan,
                        "terminal": result.terminal,
                        "escalated": result.escalated,
                        "manual_review_reason": result.manual_review_reason,
                        "decision": result.decision,
                        "nfr2_plan_ok": result.plan_ok,
                    },
                }
            ),
        },
    ]
    return await judge(messages, "NEXUSOPS_FRONTIER_MODEL", JUDGE_SCHEMA)


async def _judge_all(judge: Callable, results: list[IncidentResult]) -> list[dict]:
    """Grade every incident; a judge outage fails that incident's verdicts but
    NEVER loses the report. A model error after a 30-minute run crashing without
    output would be the same silent-loss class we refuse elsewhere (D-6)."""
    reviews = []
    for r in results:
        try:
            reviews.append(await judge_result(judge, r))
        except ModelError as e:
            reviews.append(
                {m: {"verdict": False, "score": 0.0, "reason": f"judge failed: {e}"}
                 for m in JUDGE_METRICS}
            )
    return reviews


def _retry_model(fn: Callable, retries: int = 3, base_sleep: float = 1.0) -> Callable:
    """Wrap a model fn with bounded exponential-backoff retry on transient
    ModelErrors (429/5xx/timeout/empty-content). Hard errors surface
    immediately — an unfunded key or a broken JSON contract is never masked.
    The final attempt raises honestly, so latency metrics include real retries."""

    async def wrapped(*args, **kwargs):
        for attempt in range(retries - 1):
            try:
                return await fn(*args, **kwargs)
            except ModelError as e:
                if not e.retryable:
                    raise
                await asyncio.sleep(base_sleep * (2 ** attempt))
        return await fn(*args, **kwargs)

    return wrapped


def _instrumented_complete(sink: MetricSink) -> Callable:
    """Real OpenRouter model fn that feeds the token-cost metric. Without this,
    B-3's tokens_per_model is silently empty (the ledger was never plugged in)."""

    async def call(messages, model_env, schema):
        return await complete_json(
            messages, model_env=model_env, timeout_s=20.0, json_schema=schema,
            usage_sink=sink.usage,
        )

    return call


# --- report & exit code ------------------------------------------------------


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def build_report(results: list[IncidentResult], fixtures: list[Fixture], sink: MetricSink,
                 reviews: list[dict], mode: str, nfr1_ms: float = 2500.0) -> dict:
    slm_only = [r.t_gate_ms for r in results if not r.escalated and r.t_gate_ms is not None]
    escalated = [r.t_gate_ms for r in results if r.escalated and r.t_gate_ms is not None]
    resolves = [r.t_resolve_ms for r in results if r.t_resolve_ms is not None]
    manual = [r.incident_id for r in results if r.manual_review_reason]
    verdicts = [v for r in reviews for v in r.values()]
    p95 = _p95(slm_only)
    # NFR-1 must be MEASURED, not vacuously true: an empty SLM-only set means
    # the target was never exercised (e.g. everything escalated) -> fail loud.
    nfr1_measured = bool(slm_only)
    all_pass = (
        bool(verdicts)
        and all(v["verdict"] for v in verdicts)
        and not manual
        and all(r.plan_ok for r in results)
        and nfr1_measured
        and p95 <= nfr1_ms
    )
    tokens = {}
    for call in sink.model_calls:
        u = call["usage"] or {}
        key = call["model"]
        entry = tokens.setdefault(key, {"prompt": 0, "completion": 0, "total": 0})
        entry["prompt"] += u.get("prompt_tokens", 0)
        entry["completion"] += u.get("completion_tokens", 0)
        entry["total"] += u.get("total_tokens", 0)
    return {
        "run": {"mode": mode, "machine": platform.platform()},
        "incidents": {
            "total": len(fixtures),
            "delivered": len(results),
            "deduped_at_ingest": sum(1 for f in fixtures if not f.delivered),
        },
        "pass_rate": _pass_rate(reviews),
        "nfr1_measured": nfr1_measured,
        "nfr1_slm_only_p95_ms": p95,
        "escalated_p95_ms": _p95(escalated),
        "mttr_ms": (sum(resolves) / len(resolves)) if resolves else None,
        "tokens_per_model": tokens,
        "tool_calls": {"rollback": len(sink.rollback_calls), "evidence": len(sink.evidence_calls)},
        "manual_review": manual,
        "all_pass": all_pass,
        "metric_pass_counts": {
            m: sum(1 for r in reviews if r[m]["verdict"])
            for m in JUDGE_METRICS
        },
    }


def _pass_rate(reviews: list[dict]) -> float:
    total = sum(len(r) for r in reviews)
    passed = sum(1 for r in reviews for v in r.values() if v["verdict"])
    return (passed / total) if total else 0.0


def exit_code(report: dict) -> int:
    return 0 if report["all_pass"] else 1


# --- CLI ---------------------------------------------------------------------


async def _run(mode: str) -> dict:
    fixtures = build_fixtures()
    sink = MetricSink()

    if mode == "smoke":
        classify, rca, gather = smoke_classify, smoke_rca, smoke_gather
        judge = smoke_judge
        pop_alerts = _smoke_pop(fixtures)

        async def _gather(incident):
            sink.evidence_calls.append(incident["incident_id"])
            return await gather(incident)

        def make_graph():
            return build_graph(classify=classify, rca=rca, gather=_gather,
                               rollback_tool=sink.rollback)

    else:
        import redis.asyncio as aioredis

        from app.evidence import gather_evidence

        async def _gather(incident):
            sink.evidence_calls.append(incident["incident_id"])
            return await gather_evidence(incident)

        redis = aioredis.from_url(
            os.environ.get("NEXUSOPS_REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
        pop_alerts = _live_pop(redis, "nexusops:incidents:queue")

        async def judge(messages, model_env, schema):
            return await complete_json(messages, model_env=model_env, timeout_s=90.0, json_schema=schema)

        judge = _retry_model(judge)
        model = _retry_model(_instrumented_complete(sink))

        def make_graph():
            return build_graph(classify=model, rca=model, gather=_gather, rollback_tool=sink.rollback)

    until = sum(1 for f in fixtures if f.delivered)
    results = await consume_loop(
        pop_alerts,
        make_graph,
        lambda g: g["expected_decision"] if g else "reject",
        until,
        strict=(mode != "smoke"),
        progress=(lambda done, total: print(f"[live] {done}/{total} incidents done", file=sys.stderr, flush=True))
        if mode != "smoke" else None,
    )
    if mode != "smoke":
        deduped = await redis.sismember("nexusops:incidents:seen", "dup-01")
        if not deduped:
            raise RuntimeError("dup-01 not present in ingest seen-set — dedupe broken")
    reviews = await _judge_all(judge, results)
    return build_report(results, fixtures, sink, reviews, mode=mode)


def _smoke_pop(fixtures: list[Fixture]):
    alerts = [f.alert for f in fixtures if f.delivered]
    index = 0

    async def pop() -> dict | None:
        nonlocal index
        if index >= len(alerts):
            return None
        alert = alerts[index]
        index += 1
        return alert

    return pop


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="nexusops-benchmark")
    parser.add_argument("--smoke", action="store_true", help="deterministic fakes, zero tokens, CI-safe")
    args = parser.parse_args(argv)
    mode = "smoke" if args.smoke else "live"
    report = asyncio.run(_run(mode))
    print(json.dumps(report, indent=2))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())