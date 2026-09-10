# Feature Spec — 3. LangGraph State Machine

System context: `specs/requirements.md` FR-3/FR-4/FR-5, §5.3; `specs/design.md` D-3/D-4/D-6, NFR-3/NFR-4.

## Phase 1 decision — **APPROVED** (2026-09-08)
**Pattern B: thread-per-incident via the checkpointer** — one compiled LangGraph StateGraph, each incident under `thread_id = incident_id`, MemorySaver checkpointer stores per-thread state, gate is an `interrupt()`. Rejected A (instance-per-incident: manual resume dict plumbing after the gate) and C (hand-rolled pipeline: reinvents checkpointing, conflicts with approved D-3). B is the SDK-native human-in-the-loop pattern: state isolation (NFR-3) via thread key, resume = one invoke with same thread_id. Risk: interrupt()/MemorySaver API verified via Context7 before code.

## What this feature does (in scope)
- LangGraph StateGraph running the 6-step flow **per incident** (isolated state — NFR-3):
  1. **SLM classify** (local Ollama): severity, affected_service, `triage_confidence`, `ambiguous`.
  2. **Escalation rule** (D-4): critical hint OR confidence < 0.6 OR ambiguous → frontier.
  3. **MCP evidence gathering** (calls feature-2 server).
  4. **RCA** → remediation plan (§5.3) — strict JSON or fail loud.
  5. **GATE** (FR-5): pause; resume on §5.4 decision; approve → rollback tool, reject → `state=rejected`.
  6. Terminal states: `resolved` / `rejected` / `manual_review`.
- Checkpointer: MemorySaver (D-3).
- AI failure (SLM/frontier error or timeout) → `state=manual_review`, loud (D-6).

## Out of scope
- Transport to the dashboard (owned by **streaming-dashboard**; this feature emits events for it).
- Alert entry (owned by **webhook-ingest**).

## Acceptance
- An incident walks through all states in order; there is **no direct SLM→rollback path**.
- An AI-down fixture → `manual_review`, never reaches the gate.
- Escalation rule fires exactly on D-4 conditions (unit-tested).