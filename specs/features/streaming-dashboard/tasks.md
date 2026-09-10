# Tasks — 4. WebSocket Dashboard Stream

Phase gates per AGENTS.md: 1 socratic-architect → 2 ponytail → 3 implementation → 4 adversarial-reviewer → 5 feynman. Task state: **todo → in-progress → done → blocked**. Update the board (`specs/tasks.md`) after each verified cycle.

| # | Task | Phases 1–5 | Verified |
|---|---|---|---|
| 4.1 | WebSocket endpoint + event schema | 1-4 ✅ | `app/dashboard.py` WS endpoint; plain-dict events with per-incident `seq` |
| 4.2 | Reconnect catch-up / replay per incident (FR-6) | 1-4 ✅ | `EventBus.subscribe()` seeds history atomically on connect; live resumes after; per-incident order tested |
| 4.3 | Decision messages over WS (mirrors §5.4) | 1-4 ✅ | `_route_decision` validates frame, error-frame on bad input, socket survives; `make_resume_incident` = same `Command(resume=...)` Feature 3 proved |
| 4.4 | Minimal dashboard page (single HTML + JS) | 1-4 ✅ | `DASHBOARD_HTML`: incident cards, approve/reject buttons, auto-reconnect |
| 4.5 | Update board (`specs/tasks.md`) | ✅ | see board |

## Phase 1 decision (2026-09-08) — Pattern A
In-process pub-sub bus + per-incident RAM ring buffer. See `spec.md`. Rejected B (Redis Streams — crash-survival D-3 doesn't need) and C (checkpointer diff — a film can't be derived from a portrait).

## Context7 stamps
- `VERIFIED FastAPI / starlette WebSocket — @app.websocket, accept, send_json, receive_json, WebSocketDisconnect, iter_json, TestClient.websocket_connect` (fastapi 0.141.1 / starlette 1.6.0 / websockets 16.1.1 — first real exercise of the downgraded websockets; passes).

## Phase 4 findings (2026-09-08)
1. **Unbounded incident-key memory — fixed:** `EventBus` kept one history deque per incident forever; long-lived process = unbounded growth. Added FIFO prune (`max_incidents`, default 1000), oldest incident evicted (catch-up only promised for in-flight per FR-6). Guarded by `test_history_is_bounded_incidents_fifo_prune`.
2. **Unbounded per-reader queue — accepted + flagged:** a stalled socket accumulates every event in RAM. Single-operator dashboard; marked with `ponytail:` upgrade note (bounded queue + drop-oldest) for multi-consumer.

## Phase 5 — Feynman
**Declined / closed on request (2026-09-08).** Question posed (double-approve on a parked gate; decision for a not-yet-parked thread; same decision re-sent across a reconnect). Developer chose to move on without answering. Feature closed with Phase 5 recorded as skipped-by-user; interview question retained in file for any successor session.

## Setup
No new dependency — FastAPI native WS + stdlib asyncio.Queue (ponytail rung 5).