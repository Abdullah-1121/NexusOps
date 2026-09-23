# Feature 7 — Live Operations Console

Status: **in-progress** (Phase 1 approved — D-11, 2026-09-23).
Owns: the serve-mode live server + the React/Vite/Tailwind operations console + the pipeline's stage-event voice. Shared contracts live in `specs/requirements.md` (§5, FR-5/6) and `specs/design.md` (D-1, D-3, D-4, D-6, D-7, D-9, D-10, D-11); this file references them, never duplicates them.

## 1. What this feature is

The thesis demo becomes a **real running system**: one FastAPI process serves the webhook, runs the real pipeline in the background (real models, D-9), streams every stage live over WebSocket, and hands the §5.4 gate to a human operator. One incident at a time: pick a fixture (or POST any valid alert), watch the machine think, approve or reject at the gate, and see a real scoped GitHub rollback execute on approve (D-10). A proper React/Vite/Tailwind console is the frontend; the film (feature 6) stays mounted as the history tab.

## 2. Requirements impact

- **FR-10 (new)** — see `specs/requirements.md`.
- **FR-6 (completed)** — the EventBus producer side is wired: the pipeline now emits the full event vocabulary live (`ingest, classified, escalating, tool_call, plan, gate_open, decision, rollback, done, manual_review`), satisfying the original FR-6 intent with the console as its production-grade client.
- No change to the graph, models, contracts §5.1–§5.4, NG-1/NG-2/NG-4.

## 3. Design references (D-11)

- **Voice:** driver-level `astream(stream_mode="updates")`; chunk → event mapping: classify→classified, escalate→escalating, evidence→tool_call, rca→plan (or manual_review), __interrupt__→gate_open, rollback→rollback, terminal→done. Zero changes to `state_machine.py` (empirically probed on LangGraph 1.2.11).
- **Human gate:** GateAwaiter asyncio-Future registry; the WS/POST decision handler resolves the future (same `_route_decision` validation as dashboard, imported), the driver owns the phase-2 resume → no two tasks resume one checkpoint.
- **Serve process:** one FastAPI app hosting webhook + `/api/fixtures` + `/api/status` + `/ws` + `/film` (player mount) + `frontend/dist` static.
- **Frontend:** React 19 + Vite + Tailwind v4 (`@tailwindcss/vite`, CSS `@import "tailwindcss"`), TypeScript; dev via Vite proxy (`ws:true`), prod via FastAPI static.

## 4. Acceptance

Given the serve app running with real models and Redis up:

1. `GET /api/fixtures` returns the synthetic catalog (alert fields only — no ground truth leaks to the operator).
2. POSTing a fixture's alert to `/webhook/incident` enqueues it and publishes an `ingest` event.
3. The pipeline picks it up and the WebSocket receives, in order: `classified` → (`escalating` when the D-4 rule fires) → `tool_call` → `plan` → `gate_open` (carrying the plan).
4. The console shows the gate; clicking **approve** resumes the parked graph and produces `rollback` (real scoped GitHub tag when env is configured, else a loud error rendered honestly) then `done(resolved)`. Clicking **reject** produces `done(rejected)` with **no** rollback/network call (NG-1).
5. A second decision on the same incident is refused: `already_decided`, first wins (§5.4).
6. A model-down run degrades honestly: `manual_review` event + `done(manual_review)` with the recorded reason; the console renders a failed run, no gate is shown.
7. The film is still reachable at `/film/` (history tab).
8. No stage event is ever emitted for an alert the operator never approved — the `rollback`/network path is unreachable pre-approval.

## 5. Out of scope (B roadmap, recorded for later)

- Multi-incident concurrency (sequential loop by design for the demo).
- Auth/production hardening (single operator, single API key accepted).
- Real monitoring connectors (Prometheus/PagerDuty) — the webhook contract already matches them; wiring them is a config+adapter task.
- Revert-commit rollback (D-10 already records this as the heavier B upgrade).