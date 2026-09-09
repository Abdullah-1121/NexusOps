# Feature Spec — 1. Webhook Ingestion

System context: NexusOps (`specs/requirements.md` §5.1, FR-1; `specs/design.md` D-2, NFR-6). This feature is the alert **doorbell** — the only place alerts enter the system.

## Phase 1 — Approved Decision (2026-09-08)
Pattern **B** (validate → Redis queue → async worker), vs. A (process fully in-request: loses alerts on crash, blocks senders) and C (separate worker process: needs supervisor tooling on a single machine). Dedupe is **one atomic Lua script** (`SADD` → `LPUSH` in a single call) — no check-then-add race, one round-trip. Records in `specs/design.md` D-2.

## What this feature does (in scope)
1. `POST /webhook/incident` endpoint.
2. Validate with Pydantic v2 (`extra="forbid"`); malformed → 422 + strict-JSON error, never enqueued.
3. Dedupe: Redis SET keyed by `incident_id` (durable across restarts — FR-1).
4. Enqueue: Redis LIST via `LPUSH`; return 202 + `incident_id`.
5. Redis unreachable → 503, loud, no degraded mode (NFR-6).

## Out of scope
- Any triage/reasoning (owned by **state-machine** feature).
- Approval logic (owned by **state-machine** / **streaming-dashboard**).

## Acceptance (how we know it's done)
- Valid POST → 202 + `incident_id`.
- Malformed → 422 strict JSON; nothing enqueued.
- Duplicate `incident_id` → no re-triage, even after a process restart (seen-set in Redis).
- Redis stopped → 503.