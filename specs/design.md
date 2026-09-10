# NexusOps — Design Specification

Status: **Phase 1 decision record — APPROVED** (v0.2, revised for WebSocket + Redis). Answers HOW, for the WHAT defined in `requirements.md`. Each major decision below compares options across **latency, cost, failure modes, complexity** and records the choice + reasoning. Deep per-feature design lives in `specs/features/<name>/spec.md`; this file owns the shared architecture and cross-cutting decisions.

## 1. Approved Decisions

### D-1 Streaming transport: **WebSocket** (chosen; not SSE)
| Axis | SSE | WebSocket (chosen) |
|---|---|---|
| Latency | sub-second | sub-second — no meaningful difference |
| Cost | less code | a bit more code: reconnect + ordering are ours |
| Failure modes | auto-reconnect by design (EventSource) | reconnection and catch-up replay must be implemented (FR-6) |
| Complexity | one-way only | matches reality: events **down**, operator decision **up** (§5.4), one socket |

**Why:** the dashboard operator clicks approve/reject on it — that is a second direction. A single persistent socket carries live events down *and* the §5.4 decision up, with the POST endpoint kept as the canonical API contract. Reconnect cost is owned explicitly: client reconnects, server replays missed per-incident events (catch-up already required by FR-6).

### D-2 Incident queue & dedupe: **Redis** (chosen; not in-process)
| Axis | asyncio.Queue | Redis (chosen) |
|---|---|---|
| Latency | none (same process) | ~1ms local round-trip — negligible |
| Cost | zero setup | one extra service to run (NFR-6) |
| Failure modes | tray + seen-set die with the process | durable: dedupe survives restarts (FR-1) |
| Complexity | ~1 line | queue adapter (LPUSH/BRPOP) + SET |

- **Queue:** Redis LIST — `LPUSH` on enqueue, `BRPOP` in the worker.
- **Dedupe set:** Redis SET keyed by `incident_id`; survives restarts, so re-delivered alerts are still deduped after a restart (stronger than the original in-memory promise).

**Why:** the seen-set surviving restarts upgrades FR-1. Running without Redis must be impossible by design → NFR-6 (refuse loudly with 503, no degraded mode). **Upgrade path:** ack/lease semantics for reprocessing incidents lost to a crash mid-triage — only when correctness demands it.

### D-3 Checkpointer: **LangGraph `MemorySaver`** (in-memory, not SQLite)
- **Why:** the save point only needs to survive *waiting for a human*, not a process crash. Losing an in-progress incident on restart is consistent with FR-1 (Redis dedupes re-delivered alerts, so the same alert is not re-tried; the abandoned one is simply gone and would need a *new* alert).
- **Upgrade path:** SQLite checkpointer when abandoning a human-waited incident on restart becomes unacceptable.

### D-4 SLM→frontier escalation: **deterministic rule**, not model-decided
The SLM must output `triage_confidence: float` (0..1) and `ambiguous: bool` on every classification. Escalate to the frontier model **iff any of**:
1. inbound `severity_hint == "critical"`, or
2. `triage_confidence < 0.6`, or
3. `ambiguous == true`.

**Why:** predictable, zero cost, unit-testable — the benchmark asserts exactly which fixtures escalate. Escalation must never be a fuzzy judgment call.

### D-5 Model providers (revised 2026-09-08: OpenRouter, not Ollama)
- **Both SLM and frontier** are open-source models served via **OpenRouter**, an OpenAI-compatible API. Env-gated: `NEXUSOPS_OPENROUTER_API_KEY`, `NEXUSOPS_SLM_MODEL` (small/cheap/fast), `NEXUSOPS_FRONTIER_MODEL` (large/capable). Exact model IDs pinned later in feature tasks after Context7 verification from the OpenRouter catalog.
- Committed same as before: cheap SLM for routine triage, expensive frontier only on escalation (D-4). The cascade contract (FR-3) does not depend on the vendor — a swap is a config change.
- **Why not Ollama:** user decision 2026-09-08 — no local model runtime; open-source models via hosted API keep the SLM/frontier asymmetry as a pure config difference (model ID), reproducibly benchmarkable (NFR-5) with no local-GPU assumption.

### D-6 Failure policy — the safe-degrade rule
If the SLM or frontier model errors or times out during the evidence/reasoning stages, the incident does **not** proceed to the gate with a partial plan. It transitions to **`state=manual_review`**, streams "AI unavailable — human review required" to the dashboard, and stops. No silent fallback, no half-baked plan — failure is always loud (NFR-4).

## 2. System Architecture

```
                 ┌────────────────────────────────────────────────┐
                 │            NexusOps (one process)              │
 alert ──POST──▶ │ FastAPI: validate (Pydantic v2, extra=forbid) │
 (webhook)       │           dedupe by incident_id (Redis SET)   │
                 │           enqueue ──▶ Redis LIST (LPUSH)      │
                 │                                                │
                 │   worker loop (per incident, isolated state)  │
                 │        │                                       │
                 │        ▼                                       │
                 │   ┌──────────────────────────────────────┐    │
                 │   │        LangGraph StateGraph          │    │
                 │   │  (each incident gets its own graph)  │    │
                 │   │  1. SLM classify ──▶ {severity, conf,│    │
                 │   │     affected_service, ambiguous}     │    │
                 │   │  2. escalate?  (rule D-4)            │    │
                 │   │  3. MCP tools ──▶ fetch_service_logs │    │
                 │   │     ──▶ query_prometheus_metrics     │    │
                 │   │  4. RCA ──▶ remediation plan (§5.3)  │    │
                 │   │  5. GATE ◀── decision (WS msg | POST │    │
                 │   │      §5.4) └ approve → rollback tool │    │
                 │   │             └ reject → state=rejected│    │
                 │   │  6. terminal state + events emitted  │    │
                 │   └──────────────────────────────────────┘    │
                 │        │           │           │              │
                 │        │           ▼           │              │
                 │   checkpointer        trace (Otel/Phoenix)   │
                 │   (MemorySaver)                              │
                 │        │                                       │
                 │        ▼                                       │
                 │   WebSocket server ──events down──▶ dashboard  │
                 │              ◀──decision (§5.4) up──           │
                 └────────────────────────────────────────────────┘
```

## 3. Component Responsibilities

| Component | Responsibility | Notes |
|---|---|---|
| **FastAPI receiver** | Accept, validate, dedupe, enqueue | 422 on malformed; dedupe key = `incident_id` (Redis SET) |
| **Redis queue + worker** | Hold + consume the incident tray | LPUSH/BRPOP; each incident → own graph instance (NFR-3) |
| **LangGraph StateGraph** | Enforce the 6-step flow, per incident | deterministic transitions; no loop paths |
| **SLM stage** | First-pass classification + confidence + ambiguous flag | local Ollama |
| **Escalation rule** | Decide SLM→frontier per D-4 | plain function, unit-tested |
| **MCP stage** | Call the 3 mock tools; record args+results in trace | real MCP protocol, mocked backends (NG-2) |
| **RCA stage** | Produce remediation plan §5.3 | SLM or frontier; strict JSON or fail loud |
| **Gate + approval endpoint** | Pause; receive human decision §5.4 (POST or WS); resume or reject | first-decision-wins; store decision in checkpointer state |
| **MCP mock server** | `fetch_service_logs`, `query_prometheus_metrics`, `trigger_github_rollback` | distinct "no data" vs "doesn't exist" errors (§5.2) |
| **WebSocket stream** | Emit ordered per-incident events; accept §5.4 decision messages | events: ingest, classified, escalating, tool_call, plan, gate_open, decision, done, manual_review; reconnect catch-up (FR-6) |
| **Trace pipeline** | Record durations/models/tools per incident_id | OpenTelemetry → Phoenix |

## 4. End-to-End Walkthrough (one incident)

1. Alert POSTs → FastAPI validates & dedupes (Redis SET) → LPUSH to Redis queue → `202`.
2. Worker BRPOPs the incident and starts a fresh StateGraph for it.
3. **SLM stage** classifies → `{severity, affected_service, confidence, ambiguous}` (+ trace).
4. **Escalation rule** runs (D-4). Critical or unsure or low-confidence → frontier model takes over the RCA stage.
5. **MCP stage** calls `fetch_service_logs` + `query_prometheus_metrics`; results land in the trace and the graph state.
6. **RCA stage** writes a remediation plan (§5.3 strict JSON). A bad plan (schema violation) fails the run loud.
7. **Gate**: plan has destructive steps → pause. Dashboard shows *waiting for approval*. State preserved by checkpointer.
8. Operator sends the §5.4 decision as a WebSocket message (or POST). `approve` → mock rollback fires once, `state=resolved`. `reject` → `state=rejected`, no tool fires.
9. All events streamed live over the WebSocket; a reconnecting dashboard receives missed events (FR-6); full trace queryable by `incident_id`.

## 5. Failure Modes & Responses

| Failure | Behavior | Violates |
|---|---|---|
| Malformed alert payload | 422 strict JSON error; not enqueued | — (loud by design) |
| Duplicate `incident_id` | deduped by Redis SET, no re-triage — also after restart | — |
| Redis down | receiver → 503, loud; worker stalls; no degraded mode | NFR-6 (if run anyway) |
| Process crash mid-incident | progress lost; alert already seen → not re-reprocessed (abandoned incident needs a new alert) | accepted (FR-1/D-2/D-3) |
| SLM error/timeout | `state=manual_review`, loud, stop | NFR-4 if silent |
| Frontier error/timeout | `state=manual_review`, loud, stop | NFR-4 if silent |
| Tool returns structured error | passes into trace + graph state; RCA must handle or go manual_review | silent swallow |
| WS client disconnect | client reconnects; server replays missed per-incident events | FR-6 (if no catch-up) |
| Unauthorized approval attempt | rejected (single shared API key) | — |
| Second approval decision | `already_decided`; first wins | — |

## 6. Non-Decisions & Marked Upgrades

- **D-2:** ack/lease reprocessing of lost in-flight incidents — when crash survival of in-flight work is required.
- **D-3:** SQLite checkpointer — when losing a human-waited incident on restart is unacceptable.
- **D-5:** exact SLM/frontier model IDs on OpenRouter — pinned after a live API call in the first benchmark/triage run; env-gated so any catalog model is a config change.
- **NG-1:** no unattended remediation — permanent boundary, enforced in the gate.
- **NG-2:** mock tool backends — MCP protocol is real; only the backend data is fake, keeping the tool interface swap-for-real in place.

## 7. Open Risk Register

- **R-1:** Local SLM on CPU must meet NFR-1 (P95 < 2.5s, SLM-only path). Mitigation: small model + short classifier prompt + structured output; measure in the FIRST benchmark run.
- **R-2:** Frontier-call latency is vendor-bound. Mitigation: excluded from NFR-1; reported separately; timeouts route to `manual_review`.
- **R-3:** WebSocket ordering + reconnect catch-up must stay consistent — a reconnecting client may have missed events mid-drop. Mitigation: per-incident event replay on connect (FR-6); load-test at 30 incidents in the benchmark.
- **R-4:** Redis is an extra runtime dependency for every test/benchmark run. Mitigation: NFR-6 refuses degraded operation; install command in `specs/features/webhook-ingest/tasks.md`; `redis-server` started as part of any run instructions.

## 8. Where Per-Feature Detail Lives

Each feature is decomposed into its own spec + task file (per AGENTS.md, never one giant app-wide task list). Shared contracts (§5 of `requirements.md`, decisions above) are **referenced, never duplicated**, in feature files.

| Feature | Spec / Tasks |
|---|---|
| 1. Webhook ingestion | `specs/features/webhook-ingest/` |
| 2. MCP server | `specs/features/mcp-server/` |
| 3. State machine | `specs/features/state-machine/` |
| 4. Streaming dashboard | `specs/features/streaming-dashboard/` |
| 5. Benchmark harness | `specs/features/benchmark/` |

Build order: 1 → 2 → 3 → 4 → 5 (later features depend on earlier ones).