# Feature 7 — Live Operations Console (task ledger)

Spec: `specs/features/live-console/spec.md` · Decision: D-11 (`specs/design.md`) · Board: `specs/tasks.md` row 7.

Every task runs the 5-phase pipeline (AGENTS.md §3). Phase-1 approvals live in `specs/design.md` (D-11). Context7 stamps recorded per task.

## T-7.1 — Pipeline voice: stage events from the live driver

- [x] **P1 Socratic** — driver-level `astream(updates)` vs per-node hooks; chosen astream (D-11). Empirically probed: LangGraph 1.2.11 streams `{node: update}` chunks; gate interrupt arrives as `{'__interrupt__': (Interrupt(value={incident_id, plan}),)}`; `Command(resume=...)` yields `gate → rollback → resolved` on the same thread. (Local probe, verified 2026-09-23.)
- [x] **P2 Ponytail** — one new module (`app/serve.py`), zero edits to `state_machine.py`; map: classify→classified, escalate→escalating, evidence→tool_call, rca→plan|manual_review, __interrupt__→gate_open, rollback→rollback, terminal→done. Reuse `app/tracing.get_tracer` (D-7 spans intact).
- [ ] **P3 Implement** — write the live driver (`run_once`) over `astream`, publish each stage to the EventBus.
- [ ] **P4 Adversarial** — challenge: chunk-shape drift vs the state machine's actual returned keys (verified against `state_machine.py` fields); manual_review-with-no-interrupt path (no gate_open, no wait); duplicate `gate_open` if resume behavior changes. Record findings below.
- [ ] **P5 Feynman** — scenario question recorded below.
- [ ] VERIFIED stamp: `/langchain-ai/langgraph` (v1.2.11, empirically probed 2026-09-23).

## T-7.2 — GateAwaiter: human-operated §5.4 seam

- [x] **P1** — Future registry (wait/resolve, first-wins `already_decided`) vs socket-driven direct resume; chosen registry (D-11): single-writer on the checkpoint, no resume race.
- [x] **P2** — reuses `app/dashboard._route_decision` (validation + decision event) with a different injected `resume_incident` (the resolve). ~30 lines new.
- [ ] **P3 Implement** — `GateAwaiter`: async resolve wrapper; idempotent (done future → False, caller reports `already_decided`).
- [ ] **P4 Adversarial** — challenge: decision arrives for an incident nobody is waiting on (resolve returns False, loudly); double-click; timeout policy (none — park forever, matches D-3). Record below.
- [ ] **P5 Feynman** — recorded below.

## T-7.3 — Serve process: one FastAPI app is the system

- [x] **P1** — serve composition (webhook + worker + WS + API + `/film` mount + static) vs one-app-per-concern; chosen compose (D-11). Incidents process sequentially (matches "single cycle" ask; parked gate blocks later work — documented).
- [x] **P2** — reuse ingest helper (`enqueue_alert` extracted in `app/ingest.py`, route behavior unchanged), `_route_decision`, `create_player_app`; new API is 3 small routes (`/api/fixtures`, `/api/status`, webhook wrapper that publishes `ingest`).
- [ ] **P3 Implement** — `create_serve_app(bus, awaiter)` + lifespan-owned background consume loop (BRPOP → `run_once`); serve built `frontend/dist` at `/`; guitar: `GET /api/status` = redis reachable / queue depth / waiting gates.
- [ ] **P4 Adversarial** — challenge: worker crash mid-incident (accepted, D-3), BRPOP timeout idle, Redis down at startup (loud), static-mount ordering vs API routes, WS reconnect catch-up (bus replay, FR-6). Record below.
- [ ] **P5 Feynman** — recorded below.

## T-7.4 — React/Vite/Tailwind operations console

- [x] **P1** — React/Vite SPA + Tailwind v4 chosen by user (D-11, zero-bloat override recorded); dev via Vite proxy (`ws:true`), prod via FastAPI static.
- [x] **P2** — minimal surface: no router (state tabs), no state library, ~5 components + 1 WS hook; TypeScript for type-safe event handling.
- [ ] **P3 Implement** — `frontend/` (package.json, vite.config.ts, tailwind v4 import, tsconfig, index.html, src: main/App/ws hook/picker/timeline/gate/status + history tab iframe to `/film`).
- [ ] **P4 Adversarial** — challenge: WS reconnect + catch-up rendering, event ordering, duplicate decision UI states, honest manual_review rendering, no ground-truth leaking to the picker. Record below.
- [ ] **P5 Feynman** — recorded below.
- [ ] VERIFIED stamps: `/reactjs/react.dev` (createRoot, 2026-09-23), `/vitejs/vite` (proxy incl. ws:true, dist output, 2026-09-23), `/tailwindlabs/tailwindcss.com` (v4 `@tailwindcss/vite` + `@import "tailwindcss"`, 2026-09-23).

## T-7.5 — Integration: one live cycle, verified in browser

- [ ] **P1** — accept: real webhook → real pipeline → real gate → real rollback path (env-gated), honest outage path. Sequenced, one cycle at a time.
- [ ] **P2** — reuse the smoke fakes for the no-quota test run; real-model run only when the user opts in (frontier budget).
- [ ] **P3 Implement** — integration test (TestClient + fakes): full event order emitted; approve→rollback; reject→no call; second decision `already_decided`; manual_review honest. Browser-verify the console.
- [ ] **P4 Adversarial** — challenge: the two safest lines in the whole diff (see records below).
- [ ] **P5 Feynman** — recorded below.

## Phase 4 findings (adversarial review — defense or change)

- (filled during T-7.1…T-7.5)

## Phase 5 verification — scenario questions & answers

- (filled during T-7.1…T-7.5)

## Definition of done

D-11 recorded; stamps above present; all 5 phases passed per task; `specs/tasks.md` row 7 reflects state; no silent exceptions; blocker guardrails (NG-1 pre-approval lockdown, honest manual_review, loud rollback 502) in place and tested.