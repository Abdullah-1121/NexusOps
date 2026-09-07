# NexusOps — Requirements Specification

Status: **Draft v0.1** — authoritative WHAT for the system. `design.md` answers HOW; `tasks.md` tracks execution. Every requirement here is written to be **testable** — an implicit "we know we're done when…" hangs off each one.

## 1. Purpose & Scope

NexusOps is an autonomous Site Reliability / incident triaging agent. When a production incident alert fires via webhook, it parses the alert, gathers evidence from system telemetry and logs, classifies severity, produces a root-cause hypothesis with a remediation plan, and — **only after human approval** — triggers the remediation.

It is a **triage and proposal system**, not an unattended auto-healing system. A human-in-the-loop gate is mandatory for all destructive actions.

### In scope
- Alert webhook ingestion.
- Evidence gathering (logs, metrics) via a controlled tool layer.
- Tiered model routing (SLM → frontier) for cost/latency optimization.
- Deterministic state-machine execution with checkpoints.
- Real-time diagnostics streaming to a dashboard.
- Human-in-the-loop approval gate before any remediation.
- Automated benchmark/regression harness over synthetic incidents.
- Full-step tracing and observability.

### Out of scope (see §7 Non-Goals)
- Unattended remediation, retry, or failover actions.
- Multi-tenant/multi-customer isolation.
- Production authN/authZ beyond a single API key.
- Historical incident analytics or dashboards beyond live diagnostics.

## 2. Core Concepts & Glossary

| Term | Meaning |
|---|---|
| **Incident** | A production anomaly signaled by an alert. Represented by the `IncidentAlert` payload (§5.1). |
| **Triage** | The pipeline from alert receipt → classified severity + initial evidence, ending at the human gate. |
| **SLM** | Small language model (cheap, fast) used for classification and evidence extraction. |
| **Frontier model** | Large, expensive model used only for root-cause synthesis and remediation planning. |
| **MCP** | Model Context Protocol — the standard interface the agent uses to call system tools. |
| **Human-in-the-loop gate** | A mandatory, state-machine-enforced pause awaiting explicit human approval before any remediation tool runs. |
| **Checkpointer** | LangGraph mechanism persisting the state machine mid-flight so execution can resume after human review. |
| **MTTR** | Mean Time To Resolution — alert receipt to approved remediation. |

## 3. Functional Requirements

Numbering is `FR-<n>`. Each carries an **acceptance check** (how we prove it's satisfied).

### FR-1 — Webhook ingestion
- The service exposes an HTTP endpoint that accepts an incident alert payload (§5.1) via POST.
- Unknown/malformed payloads are rejected with `4xx` and a strict-JSON error; they are never silently dropped.
- Valid payloads are enqueued for processing exactly once. Duplicate delivery of the same incident (same `incident_id`) must not create duplicate work.

**Acceptance:** Post a valid payload → 202 + `incident_id`. Post a malformed payload → 4xx + JSON error. Re-post the same `incident_id` → no re-triage.

### FR-2 — Evidence gathering via MCP tools
- The agent can call exactly three tools, defined in §5.2:
  - `fetch_service_logs(service_name, timestamp_window)`
  - `query_prometheus_metrics(metric_name, duration)`
  - `trigger_github_rollback(commit_sha)` — remediation tool, gated (§FR-5).
- All tool calls and their results are recorded in the trace.

**Acceptance:** Running the benchmark records every tool call, arguments, and result; missing/gated tool calls are visible in the trace.

### FR-3 — Tiered model routing (model cascade)
- A cheap SLM performs the first-pass triage: severity classification, affected service, error-type extraction from the alert.
- The frontier model is engaged **only** when the SLM's classification is ambiguous/high-impact, or for root-cause synthesis — not for routine triage.
- The routing decision (which model, why, token counts, latency) is part of the trace.

**Acceptance:** The benchmark reports per-incident model usage, token cost, and P95 latency. Routine incidents must not invoke the frontier model.

### FR-4 — Root-cause hypothesis + remediation plan (strict JSON)
- Output of the reasoning stage is a structured remediation plan conforming to §5.3.
- Output is **strict JSON 100% of the time**; a parse failure is a system bug (fail loud), not a soft warning.

**Acceptance:** Every benchmark run parses; any parse failure fails the run and is raised as a defect.

### FR-5 — Human-in-the-loop gate
- Execution of `trigger_github_rollback` (and any future destructive tool) is blocked until an explicit human approval is recorded against the incident's checkpointer state.
- No code path may auto-approve. Rejection terminates the pipeline for that incident cleanly.

**Acceptance:** A state-machine test drives an incident to the gate and asserts the rollback tool is never invoked before approval; a rejected incident records `state=rejected`.

### FR-6 — Real-time streaming diagnostics
- The dashboard receives live pipeline events (ingest, classification, tool calls, gate state, final plan) over a real-time transport (WebSocket or SSE — decided in `design.md`).
- Events are ordered per incident; the dashboard can open with a partially complete incident and catch up.

**Acceptance:** Streaming test observes all pipeline events for a synthetic incident on the wire, in order, without polling.

### FR-7 — Observability & tracing
- Every pipeline step is traced (OpenTelemetry / Arize Phoenix) with duration, model, tool, and outcome.
- Traces are queryable per `incident_id`.

**Acceptance:** A single incident produces a complete trace with all steps and durations.

### FR-8 — Automated benchmark (regression harness)
- A DeepEval-based harness runs **30 synthetic incidents**, scores each against a ground-truth rubric (severity, root cause, plan actionability), and reports aggregate pass rate, P95 latency, token cost, and MTTR.

**Acceptance:** `benchmark` command exits non-zero on any rubric failure or SLA breach; outputs a machine-readable report.

## 4. Non-Functional Requirements — SLAs & Guarantees

- **NFR-1 Latency:** P95 triage time (alert received → staged at human gate) < **2.5 seconds** on the reference machine.
- **NFR-2 Output integrity:** 100% of remediation plans are strict, schema-valid JSON (§5.3).
- **NFR-3 Concurrency safety:** Concurrent incidents do not deadlock, interleave state, or share mutable state. Each incident's state machine is isolated and checkpointer-guarded.
- **NFR-4 No silent failures:** Every failure is either propagated, logged with error code, or triggers a loud circuit-break into the trace. No `except: pass`.
- **NFR-5 Reproducibility:** Benchmark runs are deterministic on fixed MCP mock data (fixed fixture set, seeded randomness).

## 5. Data Contracts

### 5.1 Incident alert payload (ingest contract)
Strict JSON (Pydantic v2 model in implementation). Unknown fields are rejected by default (`extra="forbid"`).

```json
{
  "incident_id": "uuid-v4",
  "occurred_at": "RFC3339 timestamp",
  "source": "string (alert source, e.g. pagerduty/prometheus)",
  "severity_hint": "critical | warning | info | null",
  "service": "string (affected service name)",
  "status_code": "int (HTTP status, 0 if n/a)",
  "error_type": "string (e.g. 500, timeout, resource_exhausted) | null",
  "stack_trace": "string | null",
  "message": "string — human-readable alert summary",
  "metadata": "object (free-form extra context)"
}
```

Validation rules:
- Required: `incident_id`, `occurred_at`, `source`, `service`, `message`.
- `status_code` must be integer ≥ 0; `severity_hint` one of the enumerated values.
- Any violation → `422` with strict-JSON error body; incident is **not** enqueued.

### 5.2 MCP tool contracts

| Tool | Inputs | Output |
|---|---|---|
| `fetch_service_logs` | `service_name: str`, `timestamp_window: {start, end}` | `{logs: [{timestamp, level, message}]}` — empty list if none |
| `query_prometheus_metrics` | `metric_name: str`, `duration: str` (e.g. `"30m"`) | `{series: [{timestamp, value}]}` |
| `trigger_github_rollback` | `commit_sha: str` | `{status: "approved"\|"rejected"\|"performed", message}` |

- Tools **MUST fail loudly**: a fetch for an unknown service returns a structured `{error}` result, never a silent empty for a genuinely missing thing masking as success. (Distinguish "service has no logs in window" from "service does not exist".)
- `trigger_github_rollback` is a **mock** in this phase: it validates the gate and returns `performed` only after approval; it never touches a real repo.

### 5.3 Remediation plan output contract
Strict JSON, produced by the analysis stage, consumed by the human gate + dashboard.

```json
{
  "severity": "critical | warning | info",
  "affected_service": "string",
  "root_cause_hypothesis": "string",
  "confidence": "float 0..1",
  "remediation_steps": [
    {"action": "rollback | scale | noop", "target": "string", "reason": "string"}
  ],
  "requires_approval": "boolean — true if any step is destructive",
  "evidence": ["string — log/metric evidence ids or summaries"]
}
```

Validation rules:
- `confidence` in `[0,1]`; enum fields strictly enumerated; at least one remediation step.
- Schema violation → the plan is rejected and the run fails loud (defect, not warning).

## 6. Benchmark & Acceptance Suite

- **B-1:** 30 synthetic incidents covering: critical (rollback-worthy), warning, info, malformed-log-source (empty evidence), ambiguous severity (frontier escalation path), duplicate delivery.
- **B-2:** Ground-truth rubric per incident scores: severity match, root-cause match, plan actionability, gate compliance.
- **B-3:** Report: pass rate, P95 latency, token cost per model, MTTR, tool-call inventory.
- **B-4:** Full pipeline only passes when all of: all rubric checks pass, NFR-1 and NFR-2 hold, no silent failures in trace.

## 7. Non-Goals (explicitly NOT building — yet)

- **NG-1:** Unattended auto-healing. No destructive action executes without prior human approval. This is a hard boundary, not a v1 simplification.
- **NG-2:** Real production access. All tools are mocks; MCP is real, backends are fake.
- **NG-3:** Multi-tenancy / user authN beyond a single shared API key.
- **NG-4:** Post-mortem analytics, trend reports, or historical incident dashboards.
- **NG-5:** On-call paging, escalation, or incident communication tools.

## 8. Constraints & Assumptions

- Language/runtime: Python 3.12+, async-first; implementation uses Pydantic v2, FastAPI, LangGraph, a real MCP server (stdlib reference), OpenTelemetry — **all versions pinned after Context7 verification**.
- Model providers: SLM via local Ollama or Groq; frontier via API. Exact providers are a `design.md` decision; the cascade contract (FR-3) does not depend on the vendor.
- The dashboard is a minimal live view (streaming focus), not a full UI product.
- Reference machine for NFR-1 is a single local dev machine (Mac/Linux, no GPU assumption); benchmark reports the machine spec.

## 9. Open Questions (resolved in `design.md`)

1. Transport: **WebSocket vs SSE** for FR-6. Implies different backpressure/persistence choices.
2. Queue: single-process in-memory queue vs. Redis. (Description says Redis; concurrency needs justify it — NFR-3.)
3. SLM escalation trigger: exact threshold/rule for "ambiguous or high-impact".
4. Checkpointer persistence: in-memory (SQLite) vs. external store for resume-after-human-review.
5. Reference frontier model + fallback policy if the frontier model times out (does NFR-1 include model outage?).

## 10. Definition of Done (system level)

The project is complete when: all FR-1..FR-8 pass their acceptance checks on the 30-incident benchmark; NFR-1..NFR-5 hold; every non-goal is respected by the implementation; design decisions OQ-1..OQ-5 are recorded and implemented; traces are queryable per incident; and `specs/design.md` + `specs/tasks.md` reflect reality.