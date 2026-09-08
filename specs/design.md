# NexusOps — Design Specification

Status: **Phase 1 decision record — APPROVED** (v0.1). Answers HOW, for the WHAT defined in `requirements.md`. Each major decision below compares options across **latency, cost, failure modes, complexity** and records the choice + reasoning. Open questions OQ-1..OQ-5 from `requirements.md` are resolved here.

## 1. Approved Decisions

### D-1 Streaming transport: **SSE** (not WebSocket)
| Axis | WebSocket | SSE (chosen) |
|---|---|---|
| Latency | sub-second | sub-second — no meaningful difference |
| Cost | more code; connection-upgrade protocol | plain HTTP, no extra machinery |
| Failure modes | manual reconnection logic is on us | auto-reconnects by design (EventSource) |
| Complexity | two-way channel we'd never use | matches reality: dashboard only *listens* |

**Why:** the dashboard is a one-way *watch*. The only human→machine action is approve/reject, already covered by the plain POST endpoint in requirements §5.4. A two-way channel for a one-way need is complexity bought with nothing. **Upgrade path:** WebSocket only if a future feature needs true two-way (e.g., live annotation).

### D-2 Incident queue: **in-process `asyncio.Queue`** (not Redis)
| Axis | Redis | asyncio.Queue (chosen) |
|---|---|---|
| Latency | adds network hop | none — same process |
| Cost | separate service to install/run | zero setup |
| Failure modes | another dependency to fall over | dies with the process — **already accepted** by FR-1 restart caveat |
| Complexity | broker wiring, connection handling | ~1 line of stdlib |

**Why:** the FR-1 restart caveat ("process restart may re-triage") already concedes crash survival, which is Redis's only real advantage. Single process, single worker → no shared-tray need. **Upgrade path:** Redis when we need multiple worker processes or crash-surviving the tray (swap the queue adapter; state machine unchanged).

### D-3 Checkpointer: **LangGraph `MemorySaver`** (in-memory, not SQLite)
- **Why:** the save point only needs to survive *waiting for a human*, not a process crash. Losing an in-progress incident on restart is consistent with the accepted FR-1 behavior.
- **Upgrade path:** SQLite checkpointer when abandoning a human-waited incident on restart becomes unacceptable.

### D-4 SLM→frontier escalation: **deterministic rule**, not model-decided
The SLM must output `triage_confidence: float` (0..1) and `ambiguous: bool` on every classification. Escalate to the frontier model **iff any of**:
1. inbound `severity_hint == "critical"`, or
2. `triage_confidence < 0.6`, or
3. `ambiguous == true`.

**Why:** predictable, zero cost, unit-testable — the benchmark asserts exactly which fixtures escalate. Escalation must never be a fuzzy judgment call.

### D-5 Model providers
- **SLM:** local Ollama (small model on the reference machine) — free, no network flake, reproducible benchmark (NFR-5).
- **Frontier:** vendor API behind `NEXUSOPS_FRONTIER_*` env vars. Exact model/version pinned in `tasks.md` after Context7 verification (per AGENTS.md §2).

### D-6 Failure policy — the safe-degrade rule
If the SLM or frontier model errors or times out during the evidence/reasoning stages, the incident does **not** proceed to the gate with a partial plan. It transitions to **`state=manual_review`**, streams "AI unavailable — human review required" to the dashboard, and stops. No silent fallback, no half-baked plan — failure is always loud (NFR-4).

## 2. System Architecture

```
                 ┌────────────────────────────────────────────────┐
                 │                NexusOps (one process)         │
                 │                                                │
 alert ──POST──▶ │ FastAPI: validate (Pydantic v2, extra=forbid) │
 (webhook)       │           dedupe by incident_id               │
                 │           enqueue ──▶ asyncio.Queue           │
                 │                                                │
                 │   worker loop (per incident, isolated state)  │
                 │        │                                       │
                 │        ▼                                       │
                 │   ┌──────────────────────────────────────┐    │
                 │   │        LangGraph StateGraph          │    │
                 │   │  (each incident gets its own graph)  │    │
                 │   │                                        │    │
                 │   │  1. SLM classify ──▶ {severity, conf, │    │
                 │   │     affected_service, ambiguous}      │    │
                 │   │  2. escalate?  (rule D-4)             │    │
                 │   │  3. MCP tools ──▶ fetch_service_logs  │    │
                 │   │     ──▶ query_prometheus_metrics      │    │
                 │   │  4. RCA ──▶ remediation plan (§5.3)   │    │
                 │   │  5. GATE ◀── POST /approval (§5.4)    │    │
                 │   │     └─ approve → trigger_github_rollback│  │
                 │   │     └─ reject  → state=rejected       │    │
                 │   │  6. terminal state + events emitted   │    │
                 │   └──────────────────────────────────────┘    │
                 │        │           │           │              │
                 │        │           ▼           │              │
                 │   checkpointer        trace (Otel/Phoenix)   │
                 │   (MemorySaver)                              │
                 │        ▼                                     │
                 │   SSE server ──events──▶ dashboard           │
                 └────────────────────────────────────────────────┘
```

## 3. Component Responsibilities

| Component | Responsibility | Notes |
|---|---|---|
| **FastAPI receiver** | Accept, validate, dedupe, enqueue | 422 on malformed; dedupe key = `incident_id` |
| **Queue + worker** | Sequential-ish processing of incidents | each incident → own graph instance; concurrency isolated (NFR-3) |
| **LangGraph StateGraph** | Enforce the 6-step flow, per incident | deterministic transitions; no loop paths |
| **SLM stage** | First-pass classification + confidence + ambiguous flag | local Ollama |
| **Escalation rule** | Decide SLM→frontier per D-4 | plain function, unit-tested |
| **MCP stage** | Call the 3 mock tools; record args+results in trace | real MCP protocol, mocked backends (NG-2) |
| **RCA stage** | Produce remediation plan §5.3 | SLM or frontier; tone: strict JSON or fail loud |
| **Gate + approval endpoint** | Pause; receive human decision §5.4; resume or reject | first-decision-wins; store decision in checkpointer state |
| **MCP mock server** | `fetch_service_logs`, `query_prometheus_metrics`, `trigger_github_rollback` | distinct "no data" vs "doesn't exist" errors (reqs §5.2) |
| **SSE stream** | Emit ordered per-incident events to dashboard | event types: ingest, classified, escalating, tool_call, plan, gate_open, decision, done, manual_review |
| **Trace pipeline** | Record durations/models/tools per incident_id | OpenTelemetry → Phoenix |

## 4. End-to-End Walkthrough (one incident)

1. Alert POSTs → FastAPI validates & dedupes → enqueued → `202`.
2. Worker starts a fresh StateGraph for this incident.
3. **SLM stage** classifies → `{severity, affected_service, confidence, ambiguous}` (+ trace).
4. **Escalation rule** runs. Critical or unsure or low-confidence → frontier model takes over the RCA stage.
5. **MCP stage** calls `fetch_service_logs` + `query_prometheus_metrics`; results land in the trace and the graph state.
6. **RCA stage** writes a remediation plan (§5.3 strict JSON). A bad plan (schema violation) fails the run loud.
7. **Gate**: plan has destructive steps → pause. Dashboard shows *waiting for approval*. State preserved by checkpointer.
8. Operator POSTs `/approval`. `approve` → mock rollback fires once, `state=resolved`. `reject` → `state=rejected`, no tool fires.
9. All events streamed live over SSE; full trace queryable by `incident_id`.

## 5. Failure Modes & Responses

| Failure | Behavior | Violates |
|---|---|---|
| Malformed alert payload | 422 strict JSON error; not enqueued | — (loud by design) |
| Duplicate `incident_id` in-process | deduped, no re-triage | — |
| Process crash mid-incident | progress lost; re-delivery re-triages | accepted (FR-1) |
| SLM error/timeout | `state=manual_review`, loud, stop | NFR-4 if silent |
| Frontier error/timeout | `state=manual_review`, loud, stop | NFR-4 if silent |
| Tool returns structured error | passes into trace + graph state; RCA must handle or go manual_review | silent swallow |
| Unauthorized approval attempt | rejected (single shared API key) | — |
| Second approval decision | `already_decided`; first wins | — |

## 6. Non-Decisions & Marked Upgrades

- **D-1:** WebSocket — only when a true two-way need appears.
- **D-2:** Redis queue — when multi-process/crash-surviving tray is required.
- **D-3:** SQLite checkpointer — when losing a human-waited incident on restart is unacceptable.
- **NG-1:** no unattended remediation — permanent boundary, enforced in the gate.
- **NG-2:** mock tool backends — MCP protocol is real; only the backend data is fake, keeping the tool interface swap-for-real in place.

## 7. Open Risk Register

- **R-1:** Local SLM on CPU must meet NFR-1 (P95 < 2.5s, SLM-only path). Mitigation: small model + short classifier prompt + structured output; measure in the FIRST benchmark run.
- **R-2:** Frontier-call latency is vendor-bound. Mitigation: excluded from NFR-1; reported separately; timeouts route to `manual_review`.
- **R-3:** SSE with many simultaneous incidents must not block the event loop. Mitigation: async writes; load-test at 30 incidents in the benchmark.