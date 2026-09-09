# Feature Spec — 4. WebSocket Dashboard Stream

System context: `specs/requirements.md` FR-6, §5.4; `specs/design.md` D-1.

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