# Feature 7 — Live Operations Console (task ledger)

Spec: `specs/features/live-console/spec.md` · Decision: D-11 (`specs/design.md`) · Board: `specs/tasks.md` row 7.

Every task runs the 5-phase pipeline (AGENTS.md §3). Phase-1 approvals live in `specs/design.md` (D-11). Context7 stamps recorded per task. Serve-runtime postmortems (stale-gate, shutdown-hang, film-mount 404, idle-log spam) are recorded in D-11's postmortem subsection — this file references, never duplicates.

## T-7.1 — Pipeline voice: stage events from the live driver

- [x] **P1 Socratic** — driver-level `astream(updates)` vs per-node hooks; chosen astream (D-11). Empirically probed: LangGraph 1.2.11 streams `{node: update}` chunks; gate interrupt arrives as `{'__interrupt__': (Interrupt(value={incident_id, plan}),)}`; `Command(resume=...)` yields `gate → rollback → resolved` on the same thread. (Local probe, verified 2026-09-23.)
- [x] **P2 Ponytail** — one new module (`app/serve.py`), zero edits to `state_machine.py`; map: classify→classified, escalate→escalating, evidence→tool_call, rca→plan|manual_review, __interrupt__→gate_open, rollback→rollback, terminal→done. Reuse `app/tracing.get_tracer` (D-7 spans intact).
- [x] **P3 Implement** — `LiveDriver.run_once` over `astream`, publishes each stage to the EventBus; `GateAwaiter.reset(iid)` per run (D-11 postmortem #1).
- [x] **P4 Adversarial** — challenged chunk-shape drift vs state-machine keys (verified against `state_machine.py` fields), manual_review-with-no-interrupt (no gate_open, no wait), duplicate gate_open on resume, and — found live — the **stale-gate re-fire** reuse of a done future (postmortem #1; see Phase-4 findings).
- [x] **P5 Feynman** — scenario Q&A recorded below (stale-gate question).
- [x] VERIFIED stamp: `/langchain-ai/langgraph` (v1.2.11, empirically probed 2026-09-23).

## T-7.2 — GateAwaiter: human-operated §5.4 seam

- [x] **P1** — Future registry (wait/resolve, first-wins `already_decided`) vs socket-driven direct resume; chosen registry (D-11): single-writer on the checkpoint, no resume race.
- [x] **P2** — reuses `app/dashboard._route_decision` (validation + decision event) with a different injected `resume_incident` (the resolve). ~30 lines new.
- [x] **P3 Implement** — `GateAwaiter`: async resolve wrapper; idempotent (done future → False, caller reports `already_decided`); `reset(iid)` parks a fresh per-run future.
- [x] **P4 Adversarial** — decision arrives for an incident nobody is waiting on (resolve returns False, loudly); double-click (`already_decided`, first-wins — §5.4); no timeout policy by design (park forever, matches D-3). The stale-gate bug (postmortem #1) *was* this class: an `already_decided` future from a prior run resumed a new run.
- [x] **P5 Feynman** — scenario Q&A recorded below (GateAwaiter question).

## T-7.3 — Serve process: one FastAPI app is the system

- [x] **P1** — serve composition (webhook + worker + WS + API + `/film` mount + static) vs one-app-per-concern; chosen compose (D-11). Incidents process sequentially (matches "single cycle" ask; parked gate blocks later work — documented).
- [x] **P2** — reuse ingest helper (`enqueue_alert` extracted in `app/ingest.py`, route behavior unchanged), `_route_decision`, `create_player_app`; new API is 3 small routes (`/api/fixtures`, `/api/status`, webhook wrapper that publishes `ingest`).
- [x] **P3 Implement** — `create_serve_app(bus, awaiter)` + lifespan-owned background consume loop (BRPOP → `run_once`) with `shutdown_event` graceful stop (postmortem #2); serve built `frontend/dist` at `/`; `GET /api/status` = redis reachable / queue depth / waiting gates. Idle `brpop` timeout handled as silent heartbeat (postmortem #4).
- [x] **P4 Adversarial** — worker crash mid-incident (accepted, D-3), **BRPOP timeout idle** (found: async client RAISES instead of returning None — postmortem #4), Redis down at startup (loud), static-mount ordering vs API routes, **`/film` mount 404 vs absolute player fetches** (postmortem #3), WS reconnect catch-up (bus replay, FR-6), shutdown-hang on cancel conversion (postmortem #2).
- [x] **P5 Feynman** — scenario Q&A recorded below (shutdown-hang question).

## T-7.4 — React/Vite/Tailwind operations console

- [x] **P1** — React/Vite SPA + Tailwind v4 chosen by user (D-11, zero-bloat override recorded); dev via Vite proxy (`ws:true`), prod via FastAPI static.
- [x] **P2** — minimal surface: no router (state tabs), no state library, ~5 components + 1 WS hook; TypeScript for type-safe event handling.
- [x] **P3 Implement** — `frontend/` (package.json, vite.config.ts, tailwind v4 import, tsconfig, index.html, src: main/App/ws hook/picker/timeline/gate/status + history tab iframe to `/film`).
- [x] **P4 Adversarial** — WS reconnect + catch-up rendering, event ordering, duplicate decision UI states (GatePanel `submitting` lock), honest manual_review rendering, no ground-truth leaking to the picker. Human-gate path proven: the only `decide(incident_id, …)` call sites are the two GatePanel buttons — no auto-decision surface exists anywhere.
- [x] **P5 Feynman** — scenario Q&A recorded below (human-gate question).
- [x] VERIFIED stamps: `/reactjs/react.dev` (createRoot, 2026-09-23), `/vitejs/vite` (proxy incl. ws:true, dist output, 2026-09-23), `/tailwindlabs/tailwindcss.com` (v4 `@tailwindcss/vite` + `@import "tailwindcss"`, 2026-09-23).

## T-7.5 — Integration: one live cycle, verified in browser

- [x] **P1** — accept: real webhook → real pipeline → real gate → real rollback path (env-gated), honest outage path. Sequenced, one cycle at a time.
- [x] **P2** — reuse the smoke fakes for the no-quota test run; real-model run only when the user opts in (frontier budget).
- [x] **P3 Implement** — integration tests (TestClient + fakes): full event order; approve→rollback; reject→no call; second decision `already_decided`; manual_review honest. Browser-verified the console live.
- [x] **P4 Adversarial** — challenged the two weakest lines (see records below).
- [x] **P5 Feynman** — recorded below.
- [x] Live proof 2026-09-24 (console on :8137, smoke): c-01 fired → `#1…#6` streamed → **GATE OPEN** (fresh) → human decision → rollback → done. **Cycle 1: reject** (real human in the browser, no rollback, terminal `rejected`). **Cycle 2: re-fire same id → FRESH gate → new approve** → `#16 Rollback executing (rollback-sha-c-01)` → `#17 terminal=resolved` (26.5 s) — the stale-gate reset fix, reproved live. Honest-degrade record rendered in the `/film` history (manual_review, "never faked as resolved").

## Phase 4 findings (adversarial review — defense or change)

- **Two weakest lines, challenged and fixed:**
  1. `except aioredis.exceptions.TimeoutError: continue` (idle heartbeat) — the reviewer found `redis.asyncio` **exposes no `exceptions` attribute**: the except-clause expression raises `AttributeError` at runtime on the first idle boundary and kills the worker. Changed to the shared `redis.exceptions.TimeoutError` (`from redis.exceptions import TimeoutError as RedisTimeoutError`) — the async client raises exactly that class. The rewritten idle test caught it (calls stuck at 1 before the fix).
  2. `PLAYER_HTML`'s absolute `fetch("/api/films")` — the player was tested standalone-at-root only, so the mount-time 404 escaped the old suite. Fixed with relative paths + a test that mounts under `/film` exactly as serve does.
- **Stale-gate (postmortem #1):** `GateAwaiter.reset(iid)` at run start; regression test + live re-proof.
- **Shutdown-hang (postmortem #2):** `shutdown_event` re-checked every iteration, warning-only `?` branch (no phantom `"?"` incident event), 10 s `wait_for` cap. Regression test + probe.
- **Idle-log spam (postmortem #4):** natural `brpop` timeout must be silent (`continue`), never a WARNING traceback — an idle worker and a crashed redis are now distinguishable at a glance.

## Phase 5 verification — scenario questions & answers

- **Q (stale-gate):** "An incident you re-fired rolls back instantly with no new operator decision — walk me through the mechanism and the fix." **A:** the GateAwaiter keys futures by `incident_id`; a re-run found the previous run's *already-done* future, so `resolve` returned False (`already_decided`) and the driver resumed with the old decision — 16.3 ms gate→rollback, no `Operator decision` event. `run_once` now calls `reset(iid)` first, parking a fresh future per run, and `wait()` stays idempotent so the fast-click path and older tests still resolve before the driver resumes. Proof: live re-fire parks at GATE OPEN and requires a brand-new decision.
- **Q (shutdown-hang):** "Why did `SIGTERM` get ignored, and what specifically changed?" **A:** redis-py's `async_timeout` swallows uvicorn's injected `CancelledError` mid-`brpop` and re-raises it as `TimeoutError`; the old generic except spun right back into `brpop`, so the lifespan's `await task` never completed. The loop now owns a `shutdown_event` re-checked after every item *and* every fetch failure, `CancelledError` still propagates, and the lifespan caps the await at 10 s — measured clean stops on real servers since.
- **Q (human-gate NG-1):** "Prove the gate is a human, not a fallback." **A:** exactly two call sites send a decision — the GatePanel Approve/Reject buttons (`useSocket.decide`); no timeout/auto-decision exists anywhere in `app/` or the frontend. The driver *parks* (`await wait(iid)`), the §5.4 handler only resolves the future, and the driver stays the single writer on the checkpoint — no two tasks can resume the same run.

## Definition of done

D-11 recorded; postmortems #1–#4 recorded in `specs/design.md` with regression tests; stamps above present; all 5 phases passed per task; `specs/tasks.md` row 7 reflects state; no silent exceptions; blocker guardrails (NG-1 pre-approval lockdown, honest manual_review, loud rollback 502, silent idle heartbeat) in place and tested. Full suite: **90 passed** (2026-09-24).