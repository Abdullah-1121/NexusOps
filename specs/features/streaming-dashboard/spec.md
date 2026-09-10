# Feature Spec — 4. WebSocket Dashboard Stream

System context: `specs/requirements.md` FR-6, §5.4; `specs/design.md` D-1.

## Phase 1 decision — **APPROVED** (2026-09-08)
**Pattern A: in-process pub-sub bus + per-incident ring buffer (RAM).** Rejected B (Redis Streams — pays for crash-survival of in-flight events that D-3 already accepts dies with the process; per-event write + cursor seam) and C (checkpointer-diff — derives a film from a portrait; can't reconstruct event order like "escalated then returned"). A: state machine publishes → WS connections subscribe; reconnect catch-up reads the per-incident deque for *in-flight* incidents, then resumes live. Failure mode (log dies with process) == D-3's accepted class. Transport: FastAPI native WebSocket (no new dependency). Decision-up seam: `resume_incident(thread_id, decision)` — carries §5.4 message to the parked gate; gate logic stays in Feature 3. Risk: FastAPI WS API to be Context7-stamped before code.

## What this feature does (in scope)
- WebSocket endpoint streaming **ordered per-incident events**: `ingest`, `classified`, `escalating`, `tool_call`, `plan`, `gate_open`, `decision`, `done`, `manual_review`.
- **Catch-up replay** on (re)connect: a client that missed events for an in-flight incident receives them (FR-6).
- **Decision up**: the same socket accepts the §5.4 JSON (`decision`, `actor`); POST stays the canonical API (same validation, same semantics).
- Minimal dashboard page (single HTML) rendering live incident cards.

## Out of scope
- Historical views / polished UI (requirements §8: minimal live view).
- Gate logic itself (owned by **state-machine**; this feature delivers the decision to it).

## Acceptance
- A synthetic incident produces all events in order over the socket.
- Disconnect → reconnect → missed events replayed.
- A `decision` message over the socket unblocks the gate.