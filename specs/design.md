# NexusOps — Design Specification

Status: **Phase 1 decision record — APPROVED** (v0.3, revised 2026-10-01: D-18 production-readiness audit recorded). Answers HOW, for the WHAT defined in `requirements.md`. Each major decision below compares options across **latency, cost, failure modes, complexity** and records the choice + reasoning. Deep per-feature design lives in `specs/features/<name>/spec.md`; this file owns the shared architecture and cross-cutting decisions.

## 1. Approved Decisions

### D-1 Streaming transport: **WebSocket** (chosen; not SSE)
| Axis | SSE | WebSocket (chosen) |
|---|---|---|
| Latency | sub-second | sub-second — no meaningful difference |
| Cost | less code | a bit more code: reconnect + ordering are ours |
| Failure modes | auto-reconnect by design (EventSource) | reconnection and catch-up replay must be implemented (FR-6) |
| Complexity | one-way only | matches reality: events **down**, operator decision **up** (§5.4), one socket |

**Why:** the dashboard operator clicks approve/reject on it — that is a second direction. A single persistent socket carries live events down *and* the §5.4 decision up, with the POST endpoint kept as the canonical API contract. Reconnect cost is owned explicitly: client reconnects, server replays missed per-incident events (catch-up already required by FR-6).

### D-2 Incident queue & dedupe: **Redis** (chosen; not in-process)
| Axis | asyncio.Queue | Redis (chosen) |
|---|---|---|
| Latency | none (same process) | ~1ms local round-trip — negligible |
| Cost | zero setup | one extra service to run (NFR-6) |
| Failure modes | tray + seen-set die with the process | durable: dedupe survives restarts (FR-1) |
| Complexity | ~1 line | queue adapter (LPUSH/BRPOP) + SET |

- **Queue:** Redis LIST — `LPUSH` on enqueue, `BRPOP` in the worker.
- **Dedupe set:** Redis SET keyed by `incident_id`; survives restarts, so re-delivered alerts are still deduped after a restart (stronger than the original in-memory promise).

**Why:** the seen-set surviving restarts upgrades FR-1. Running without Redis must be impossible by design → NFR-6 (refuse loudly with 503, no degraded mode). **Upgrade path:** ack/lease semantics for reprocessing incidents lost to a crash mid-triage — only when correctness demands it.

### D-3 Checkpointer: **LangGraph `MemorySaver`** (in-memory, not SQLite)
- **Why:** the save point only needs to survive *waiting for a human*, not a process crash. Losing an in-progress incident on restart is consistent with FR-1 (Redis dedupes re-delivered alerts, so the same alert is not re-tried; the abandoned one is simply gone and would need a *new* alert).
- **Upgrade path:** SQLite checkpointer when abandoning a human-waited incident on restart becomes unacceptable.

### D-4 SLM→frontier escalation: **deterministic rule**, not model-decided
The SLM must output `triage_confidence: float` (0..1) and `ambiguous: bool` on every classification. Escalate to the frontier model **iff any of**:
1. inbound `severity_hint == "critical"`, or
2. `triage_confidence < 0.6`, or
3. `ambiguous == true`.

**Why:** predictable, zero cost, unit-testable — the benchmark asserts exactly which fixtures escalate. Escalation must never be a fuzzy judgment call.

### D-5 Model providers (revised 2026-09-08: OpenRouter, not Ollama)
- **Both SLM and frontier** are open-source models served via **OpenRouter**, an OpenAI-compatible API. Env-gated: `NEXUSOPS_OPENROUTER_API_KEY`, `NEXUSOPS_SLM_MODEL` (small/cheap/fast), `NEXUSOPS_FRONTIER_MODEL` (large/capable). Exact model IDs pinned later in feature tasks after Context7 verification from the OpenRouter catalog.
- Committed same as before: cheap SLM for routine triage, expensive frontier only on escalation (D-4). The cascade contract (FR-3) does not depend on the vendor — a swap is a config change.
- **Why not Ollama:** user decision 2026-09-08 — no local model runtime; open-source models via hosted API keep the SLM/frontier asymmetry as a pure config difference (model ID), reproducibly benchmarkable (NFR-5) with no local-GPU assumption.

### D-6 Failure policy — the safe-degrade rule
If the SLM or frontier model errors or times out during the evidence/reasoning stages, the incident does **not** proceed to the gate with a partial plan. It transitions to **`state=manual_review`**, streams "AI unavailable — human review required" to the dashboard, and stops. No silent fallback, no half-baked plan — failure is always loud (NFR-4).

### D-7 LLM tracing & evaluation — OpenTelemetry GenAI (2026-09-16)
- **Chosen over OpenLLMetry and Langfuse:** user directive — industry-standard OpenTelemetry only. Instrument the single choke point `complete_json` in `app/models.py` (every SLM classify, frontier RCA/plan, and judge call passes through it) so one piece of plumbing traces all three.
- Span contract (GenAI semantic conventions, pinned literals — the Python `gen_ai` semconv module is `_incubating`): `llm.chat.completions` with `gen_ai.system` (base-URL hostname), `gen_ai.request.model`, `gen_ai.usage.input_tokens`/`output_tokens`, `gen_ai.response.model`/`id`; errors set ERROR status + `record_exception`. Negative-attribute rule withheld — response-model/id on success only.
- Internal-parent spans per incident: `nexusops.incident` around `drive_incident` (`app.incident.id`); the LLM judge opens `llm.judge` with `eval.<metric>` verdict attributes — **evaluation rides the trace** (evaluation-as-trace), so a trace backend doubles as the eval record.
- Exporter contract: standard env — `OTEL_EXPORTER_OTLP_ENDPOINT` set → OTLP/HTTP exporter; unset → `ConsoleSpanExporter` (harness runs with zero infra). `service.name` from `OTEL_SERVICE_NAME`, default `nexusops`. `init_tracing()` is idempotent; tests inject `InMemorySpanExporter` via a session provider in `tests/conftest.py` (OTel `set_tracer_provider` is one-shot — conftest installs first so the whole suite shares one exporter).
- Context7 VERIFIED `/open-telemetry/opentelemetry-python` 1.44.0 (2026-09-16) — provider/BatchSpanProcessor/exporter API signatures confirmed. Deps: `opentelemetry-sdk`, `opentelemetry-api`, `opentelemetry-exporter-otlp-proto-http`.
- **Wiring hardening (2026-09-21, live Jaeger collector):** two real bugs found while bringing up a local Jaeger.
  1. `_default_exporter` passed the raw base URL as the exporter's `endpoint=` kwarg — the SDK appends `/v1/traces` **only when the endpoint comes from its env fallback**, so a collector POST 404'd (`page not found`) against its own `/v1/traces`. Fixed: `app/tracing.py` resolves the signal path itself — `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (full URL, wins) vs `OTEL_EXPORTER_OTLP_ENDPOINT` (base, `/v1/traces` appended) — verified by monkeypatching `requests` and by a probe span round-tripping into Jaeger.
  2. `app/models.py` imported `app.tracing` *before* `_load_env_file()` ran — and the tracer provider is one-shot, so an OTLP endpoint set in `.env` would never be seen (console fallback silently wired instead). Moved `_load_env_file()` above the tracing import. Verified: bare-env process (`env -i`) wires `OTLPSpanExporter` from `.env` alone.
  Local stack: `scripts/start-jaeger.sh` (idempotent all-in-one, OTLP HTTP `:4318`, UI `:16686`), `.env` carries `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`; README documents `scripts/start-jaeger.sh` → `open http://localhost:16686`.

### D-8 Golden-replay eval — machinery proof independent of model quality (2026-09-22)
- **Problem:** live benchmark verdicts conflate two different claims — "does our state machine behave correctly?" vs "is the model smart?". With the free tier congested (503s), we couldn't get *any* clean measurement of the machinery itself.
- **Decision:** a `--golden` mode in `app/benchmark.py` that runs all 29 delivered fixtures through the **real** graph (classify → escalate → evidence → RCA → gate → approve/reject → rollback) using the deterministic `smoke_*` fakes as a "perfect model" — **zero network, zero tokens, fully repeatable**. It then computes a per-incident **machinery audit** (gate parked? decision matches expected? rollback executed exactly when approved and *never* when rejected? plan NFR-2 valid? severity matched?) and prints a human-readable evidence card with a `machinery: PASS` verdict.
- **Chosen over:** (a) routing live runs through more calls — remains model-gated and flaky; (b) separate standalone script — the fakes/graph/report already exist in `benchmark.py`; a CLI flag is the minimal delta (Ponytail).
- **Effect:** the thesis shows "NexusOps correctly listens, routes, gates, and executes" as a *provable, deterministic* claim; model quality stays its own separate, honestly-measured axis. Runs in CI, needs no Redis/Docker/models.

### D-9 Provider switch OpenRouter → Google Gemini native free tier (2026-09-22)
- **Problem:** the OpenRouter/Nvidia free tier kept failing the same way: `429 free-models-per-day` (50-call cap) and `503 provider_overloaded` on Nvidia free models, empirically verified over multiple days (Phases 4l, 4m). No *version* of retrying or probe-guarding fixes a provider daily cap — it is a quota, not a transient. We needed a free tier with a real daily allowance that could complete a 10-incident benchmark in one day.
- **Decision:** switch the OpenAI-compatible base URL to Google's **native Gemini free tier** — `https://generativelanguage.googleapis.com/v1beta/openai` — via the **existing env-gated config only, zero code changes** (this is exactly what D-5's "a swap is a config change" promised). Model split: **SLM = `gemini-3.5-flash-lite`** (500 free req/day) for classify + non-escalated RCA, **frontier = `gemini-3.7-flash`/`gemini-3.8-flash`** (20 free req/day, the reasoning tier for escalated RCA + judge). Key auth stays `Bearer` — the `AQ.*` AI Studio key works on the OpenAI-compat surface. Strict `response_format` json_schema is supported on both models (verified live: classify + full RCA schema round-trip; usage tokens returned).
- **Budget math (the reason this fits):** a 10-incident run = ~10 classify + ~6 SLM RCA + ~4 frontier RCA + 10 judge calls ⇒ ~16 of 500 SLM, ~14 of 20 frontier. Verified in the 2026-09-22 live run: 16 SLM + 2 frontier in the record phase.
- **Why not the Gemini `v1beta/interactions` surface the key provider also offers:** our harness already speaks OpenAI-compat chat completions; the interactions API would be a whole new code path (violates Ponytail "zero bloat, re-use"). The OpenAI-compat surface does the same job with a config delta.
- **Context7 VERIFIED** — Gemini OpenAI-compatible API (`generativelanguage`, `v1beta/openai/chat/completions`, strict `response_format` json_schema, `usage` in response) confirmed live by direct API calls on 2026-09-22 with the project key. No new Python library added (httpx already in place).
- **Postmortem lesson (the deep one):** free-tier retry *policy* interacts with free-tier *quota*. An in-process retry wrapper designed for a 1000+ req/day paid tier (3 tries × backoff) **multiplies the burn** on a 20/day quota: a congested judge pass retried 503s and 429s up to 3× per incident, silently exhausting the entire day's frontier quota (verified: `429 exceeded your current quota` on both flash models). Guardrails shipped with this decision:
  1. `_quota_exhausted()` in `app/models.py` classifies a **terminal daily-cap** response (Gemini "exceeded your current quota", OpenRouter "limit reached") as `retryable=False` — a quota is not transient, retrying it is waste + burn (4 guardrail tests).
  2. In-process retries capped at 2 (`_retry_model(..., retries=2)`) everywhere — one blip absorbed in-process, everything else owned by the probe-guarded *loop* level.
  3. `scripts/retry_judge.sh` — probe-guarded judge replay: 1-call frontier probe (response_format-free, `max_tokens:3`), only when it answers does a full `--judge` pass run; sleeps and retries otherwise. Never re-runs the pipeline — `judge_checkpoint` replays the saved record (`outG1.json`), so a clean pass costs ~11 frontier calls (10 judge + 1 probe ≈ 55% of the 20/day).

### D-10 Demo film — replay player over recorded checkpoints + real Git rollback (2026-09-23)
- **Problem:** the thesis needs a *showable* demo — someone watches NexusOps handle an incident, approves a rollback, and sees it really happen. But the recording (`outG1.json`) is a **terminal-state snapshot**, not a film reel: it holds final severity/plan/decision/timings per incident, not per-step stage events (the EventBus only carries decisions today, not transitions).
- **Playback source — Pattern B (checkpoint-derived player), rejected Pattern A (live re-run):**
  | Axis | A: re-run the graph live during the demo | B: player over the recorded checkpoint (chosen) |
  |---|---|---|
  | Latency | seconds-to-minutes live | instant (reads saved frames) |
  | Cost | real model quota per demo, or fake outputs with fakes | zero — offline, deterministic |
  | Failure modes | live system misbehaves mid-demo; non-deterministic | nothing live to break; only risk is a *flat* film (pacing is ours) |
  | Complexity | re-run graph + inject saved decisions | one player over `outG1.json`; stage order is graph-deterministic, recorded fields fill each beat |
  The stage sequence is fixed by the state-machine graph, so the film *derives* beats (alert → severity → evidence → plan → gate → decision → rollback → terminal) from the checkpoint without a new recorder in the benchmark pipeline.
- **Live approval gate:** at the gate beat the film **pauses and the operator approves live** via the existing §5.4 seam (`approve_rollback` + WS/POST decision). The human-in-the-loop moment is the film's thesis (NG-1) — it is *performed*, not replayed.
- **Real Git rollback, scoped (replaces the mock body only):** `trigger_github_rollback` stops returning `"performed (mock)"` and performs a **real GitHub REST call creating a rollback tag** (`refs/tags/rollback-<incident>-<ts>` at the last-known-good SHA) against an env-configured throwaway repo (`NEXUSOPS_GITHUB_REPO` = `owner/repo`, `NEXUSOPS_GITHUB_TOKEN` = fine-grained PAT scoped to that one repo, `NEXUSOPS_GITHUB_LAST_GOOD_SHA` optional overrides repo default-branch tip).
  | Axis | Stdlib `urllib` REST (chosen) | `PyGithub` package |
  |---|---|---|
  | Cost | zero new dependencies | +1 package to pin and maintain |
  | Complexity | ~30 lines, one function; raw HTTP codes surfaced | simpler syntax, but a whole abstraction layer for one call |
  | Failure visibility | 401/404/timeout surface directly, mapped by us | library-wrapped errors hide the raw shape |
  **Tag chosen over revert-commit:** a tag marks the recovery point without rewriting history (reversible, verifiable via API) — the standard CD "rollback marker" pattern. A revert *commit* (real code change) stays in the B roadmap — heavier, can conflict. **Gate invariant unchanged:** approval check stays *before* the API call; no approval ⇒ rejected (NG-1). Any API failure (401/404/timeout) is loud → D-6 manual_review, never a silent pass.
- **Key hygiene (parking-lot lesson applied):** token lives in `.env` only (gitignored), scoped to one throwaway repo, never logged/rendered; the film labels the target repo on screen so the claim "it actually rolled back" is honest and scoped.
- **Requirements impact:** NG-2 amended — the rollback tool's backend becomes real-but-scoped while everything else stays mock; requires editing the FR-2 tool table note (§5.2) and NG-2 wording.
- Context7 VERIFIED — GitHub REST refs API (`POST /repos/{owner}/{repo}/git/refs`, `GET .../git/ref/tags/{tag}`) confirmed via Context7 docs (2026-09-23); no new Python library.

### D-11 Live operations console — React/Vite frontend + true-live single-incident cycle (2026-09-23)
- **Problem:** the user rejected the film player as the *main* frontend ("very simple, does not look like a proper production thing") and asked for a **proper frontend** plus a **live system** that runs one complete cycle on a single incident while watched. The build must be the real machine (real models, real gate, real scoped rollback for D-10), not a replay.
- **Frontend — Pattern B: React/Vite SPA with Tailwind v4** (user choice; **explicit zero-bloat override, recorded**):
  | Axis | A: polished single-file HTML (recommended by engineer) | B: React/Vite SPA (chosen by user) |
  |---|---|---|
  | Cost | zero new deps | Node toolchain + ~200 packages (node_modules); permanent build/version-churn tax |
  | Looks "production"? | yes with craft | yes by default — framework signaling + Tailwind utility styling |
  | Failure modes | nothing to toolchain-break | npm install/Vite/Tailwind version drift; build step must pass before FastAPI can serve `dist/` |
  | Complexity | one crafted file | component tree + hooks + Vite config + TS config + WS client |
  Decision recorded with the trade-off made explicit: the user knowingly accepted the toolchain for the production look. The film (D-10) is **kept as a history tab**, mounted at `/film` inside the same serve process — not deleted (it stays the offline, zero-quota review path).
- **"Live" — true live, real models** (user choice): every cycle = real webhook enqueue → real LangGraph pipeline → real Gemini calls (SLM `gemini-3.5-flash-lite` classify/plain RCA, frontier `gemini-3.8-flash` on escalation, D-4/D-9) → **human-operated gate** → real scoped GitHub tag rollback on approve (D-10). Cost honest: ≈1–2 SLM + ≤1 frontier call per incident (≤5% of the 20/day frontier budget). If the frontier is down, the cycle **honestly degrades** to `manual_review` with the recorded reason (D-6) — the console renders that as a failed run, never fakes success.
- **The two missing pieces the build must add (verified absent in code, 2026-09-23):**
  1. **The machine has no voice.** The EventBus + WS (D-1/FR-6) exist, but nothing publishes stage events — the bus only carries decisions today (producer side never wired; `grep` over `app/` confirms zero pipeline publishers). The console needs `ingest → classified → escalating → tool_call → plan → gate_open → decision → rollback → done` emitted live.
  2. **The gate is operated by a robot today.** `consume_loop`/`drive_incident` (benchmark) auto-decides at the gate via a sync `operator` callable. A live demo needs *a human* — the graph must park and wait for the operator's click through the real §5.4 contract.
- **Voice mechanism — Pattern A: driver-level `astream(stream_mode="updates")`** (chosen over per-node hook injection):
  | Axis | A: driver-level astream (chosen) | B: inject publish hooks into every `make_*_node` |
  |---|---|---|
  | Complexity | one new driver; **zero changes to `state_machine.py`** | touches all node constructors + their tests |
  | Failure modes | depends on LangGraph stream semantics — **empirically probed** (LangGraph 1.2.11): each node chunk streams as `{node: update}`, the gate `interrupt()` **arrives as a real streamed `__interrupt__` chunk carrying the plan**, and `Command(resume=...)` cleanly resumes the same thread with the remaining chunks (`gate → rollback → resolved`). Local probe in `/tmp` (2026-09-23). | hook contract drift across 6 nodes; more surface to forget |
  | Cost | ~1 new module | edits across the file |
  Pattern A maps chunks to the FR-6 vocabulary: `classify→classified`, `escalate→escalating`, `evidence→tool_call`, `rca→plan` (or `manual_review` when the node carries `manual_review_reason`), `__interrupt__→gate_open` (plan from interrupt value), `rollback→rollback`, `resolved|rejected|manual_review→done`.
- **Human gate seam — Pattern A: asyncio-Future "waiting room"**:
  | Axis | A: GateAwaiter future registry (chosen) | B: resume graph from the socket directly (dashboard's current `resume_incident`) |
  |---|---|---|
  | Complexity | tiny registry: `wait(iid)`/`resolve(iid, body)` | already exists |
  | Concurrency correctness | **single-writer**: the driver is the only actor touching the graph thread — it publishes `gate_open`, awaits `wait(iid)`, then runs the phase-2 `astream(Command(resume=...))`. The WS/POST decision handler (same §5.4 validation as dashboard `_route_decision`, reused) only **resolves the future**; the driver wakes, resumes, emits `rollback`/`done`. No two tasks can resume the same checkpoint (no race). | two writers (socket + driver) on the same thread → resume race; decision event ordering fragile |
  | Failure modes | second decision → future already done → `already_decided` (loud, first-wins, matches §5.4); operator never decides → incident parks forever (correct — the gate is a real pause, D-3 accepts losing it on restart) | double-resume risk |
  The `_route_decision` validation + "decision" event publishing from `app/dashboard.py` is **imported, not duplicated**; only the injected `resume_incident` differs (resolves the future instead of invoking the graph — same seam the dashboard already accepts).
- **Serve process — one FastAPI app is the whole system** (`app/serve.py`): `POST /webhook/incident` (reuses the ingest helper, publishes `ingest`), `GET /api/fixtures`, `GET /api/status`, `WS /ws` (live events down, §5.4 up), `/film` mounted player, and `frontend/dist/` static at `/` (SPA served by FastAPI in prod; Vite dev server proxies `/ws|/webhook|/api` to FastAPI in dev, `ws:true`). Background consume loop (BRPOP → LiveDriver) runs inside the app lifespan. Incidents process one at a time (sequential loop — matches the "single incident cycle" ask; a parked gate blocks later work by design, documented).
- **Budget/constraints honored:** every pipeline stage still runs inside the existing OpenTelemetry spans (D-7); no model-logic changes; Redis stays a hard dependency (NFR-6); the rollback tool remains the real-but-scoped GitHub tag (D-10), github env missing → loud 502 (never a silent "performed").
- Context7 VERIFIED — React (`createRoot` from `react-dom/client`, 19.x), Vite (server.proxy incl. `ws:true`, `dist` build output), Tailwind CSS v4 (`@tailwindcss/vite` plugin + CSS `@import "tailwindcss"` — no config file), LangGraph 1.2.11 `astream(updates)`/`interrupt`/`Command(resume=...)` confirmed by **local empirical probe** (2026-09-23).
- **Requirements impact:** FR-10 (live operations console) + §5.5 (live gate notes) added; FR-6's "producer side" is finally wired (the console *is* the FR-6 dashboard's production form). NG-2/NG-4 unchanged.

### D-11 postmortems — serve-runtime hardening (2026-09-23/24, all live-found)

Six real defects found while bringing the live console up (and hardening the transparency work); each shipped with a guardrail test. The through-line: **the demo machine must never fake a human, never fake a rollback, never fake silence.**

1. **Stale gate → rollback with no new human decision (NG-1 class, 2026-09-23).** A re-fire of the same incident reused the *previous run's already-done* gate future: `GateAwaiter.resolve` returned `False` (`already_decided`), the driver resumed with the OLD decision, and the run went `#15 gate → #16 rollback` in **16.3 ms with no `Operator decision` event**. *Mental-model error:* a waiting room keyed by `incident_id` names a *place*, but its content belongs to a *run* — across runs the SAME key holds a stale, already-consumed future. **Guardrail:** `GateAwaiter.reset(iid)` at `LiveDriver.run_once` start parks a fresh future per run; `wait()` stays idempotent (returns the done future — the fast-click path and older tests resolve before the driver's `await wait()` resumes). Regression test `test_driver_rerun_of_same_incident_parks_fresh_no_stale_decision`; **reproved live 2026-09-24**: re-fired c-01 parked at a fresh gate and required a *new* human decision (`#15 approve` → `#16 rollback`, 26.5 s run) instead of replaying run 1's reject.
2. **Shutdown hang — "SIGTERM ignored / SIGKILL needed" (2026-09-23).** While a `brpop`'s `async_timeout` is expiring, redis-py **consumes** uvicorn's injected `CancelledError` and re-raises it as a redis `TimeoutError` (asyncio/timeouts.py `uncancel`+re-raise). The old generic `except` swallowed that and looped straight back into `brpop`, so the lifespan's `await task` never completed — confirmed on PIDs 7941/10308. **Guardrail:** the consume loop takes a `shutdown_event` and re-checks it every iteration; exception-containment still holds (postmortem in code), only `CancelledError` propagates; lifespan sets the event → cancels → `asyncio.wait_for(task, 10.0)`. Probe verified a clean cancel mid-`brpop` still raises `CancelledError` (the conversion only fires on timeout-expiry coincidence). Regression test `test_consume_loop_shutdown_event_graceful_stop_after_fetch_failure`.
3. **Film time-travel 404 — History tab crashes on mount (2026-09-24).** `PLAYER_HTML` (D-10) uses **absolute** `/api/films`-style fetches. When `create_player_app` is mounted under `/film` (D-11), those resolve at the server *root* → 404 → `films.map is not a function` in the History tab. *Mental-model error:* the player was built/tested standalone at root (`uvicorn app.player:app`), never through its production mount. **Guardrail:** relative fetch paths (`api/films`) — correct under `/film` *and* at root; new regression test `test_player_works_mounted_under_film_history_tab` mounts the player exactly as serve does and asserts the API is reachable at `/film/api/...`, the root `/api/films` 404s, and no absolute fetch remains in the HTML.
4. **Idle worker screams every 5 s — healthy logs look like a crash dump (2026-09-24).** async redis-py's `brpop(timeout=5)` **never returns `None`** — on a quiet boundary the read's `async_timeout` RAISES `TimeoutError`. So the `if item is None` branch was dead in practice and every idle 5 s hit the catch-all → `WARNING worker fetch failed (redis)` + a 20-line traceback. *Mental-model error:* sync-client semantics (`brpop` → `None` on timeout) applied to the async client, which raises. `continue`-ing on the natural timeout is the worker's heartbeat, not a failure — and the old code made "all clear" indistinguishable from "redis down". **Guardrail:** explicit `except RedisTimeoutError: continue` (silent idle), *before* the generic catch — genuinely abnormal failures (`ConnectionError`, redis down) still land in the loud warning branch. Rewritten regression test `test_consume_loop_idle_timeout_is_silent_and_graceful_stop_is_prompt` (caplog asserts zero `worker fetch failed` records over several idle boundaries + prompt graceful stop). Verified live: 109 server-log lines with **0** timeout warnings during idle.
5. **Silent queueing — the operator can't tell "queued" from "broken" (2026-09-28, user-found).** Firing an incident while a previous one is parked at the human gate enqueues it (correct, sequential-by-design), but the console showed only `#1 Incident received` and then silence — a new operator reads that as a dead pipeline. *The user's framing:* it's not a console-display problem, it's a **user-feedback** problem — the *acting operator* must be told, at the moment they act, that their fire is queued behind a human gate, not broken.
   | Axis | A: passive global counter only (StatusStrip already shows depth) | B: per-action + per-incident state-aware feedback (chosen) | C: refuse to queue from the console while a gate parks |
   |---|---|---|---|
   | Honesty | numbers exist but never speak to the acting user | every fire answers "ran or queued?" with the blocker named | loudest, but teaches the wrong model |
   | Failure modes | operator fires 7 incidents, still confused | needs the status snapshot at fire time (3 s poll — acceptable) | console ≠ webhook contract: real senders still enqueue, console blocks → divergence |
   | Complexity | zero | footer branch + a Timeline queued banner, no new deps | a 409 path that then needs un-blocking UX |
   **Chosen B:** the fire footer becomes state-aware — `accepted & queued c-02 — c-01 parked at the gate; decide it first (queue N)` in amber vs the green "watch it stream" when actually running — and the fired incident's timeline carries a persistent `QUEUED behind c-01` banner until its run starts. C rejected: queueing *is* the contract; blocking the console surface would contradict the webhook. B keeps the machine honest to the human who acts (NFR-4).
6. **Stage timings lie after the gate — "rollback took 22 minutes" (D-12/B1 discovery, 2026-09-28).** The new `stage_duration_ms` stamp measured wall-clock between astream chunk arrivals; after the human resolves the gate, `_t_chunk` was still the *pre-park* chunk time, so the resume's first chunk (rollback) claimed the **entire operator decision wait** as the rollback stage's duration. *Mental-model error:* a per-run stopwatch that pauses during the human pause, but it didn't — the gate wait is wall time on the same clock, and mis-attributing it is a lie the operator can see. **Guardrail:** `LiveDriver` restarts the stage clock at resume (`self._t_chunk = time.monotonic()` after `await awaiter.wait(iid)`), so post-gate durations are measured from the human's answer; the full-cycle regression test inserts a 0.5 s decision wait and asserts `rollback.stage_duration_ms < 200`. `done.t_resolve_ms` intentionally still includes the gate wait (it is end-to-end resolve latency, labeled as such, not a per-stage claim).

### D-12 Live console redesign — professional light UI + process transparency (2026-09-28, user-directed)

The operator direction for the console changed after the live demo: **readable, professional, industry-level** — the D-11 dark terminal palette is revoked for the operator surface (the feature-6 film player stays cinematic — it is a film). Same user session also approved scope **A→D** and option **B1** below. Key finding from a code pass: **most of the requested detail already flows through the events** (`tool_call` carries the full MCP evidence list from `gather_evidence`; `plan` carries steps + attribution; `rollback` carries the execution result) — the console was *hiding* it behind one-line summaries.

| Axis | A: open the payloads (expandable stages) | B1: live enriched milestones (chosen) | B2: token-level "see the model type" stream | C: gate feels real (instant decision results) |
|---|---|---|---|---|
| What the operator sees | click any stage → full detail: evidence rows, plan steps, model, timings | stage events carry `stage_duration_ms`, model env + per-tool evidence tagged A→D ships too | the model's raw token stream arrives mid-call | decision click → instant confirmation; approve streams rollback execution; reject confirms "no action" fast |
| Honesty | reveals data already produced — zero fabrication | timings/labels derived from real chunk arrival + real tool outputs | most honest to the moment, but noisy | removes the ~30 s post-click dead air that reads as a dead button |
| Failure modes / cost | none new (pure render) | needs serve to stamp duration + `app/evidence.py` tool tags (small, tested seams) | token echoes (cost), interrupt+memory risk, LangGraph version surface, noisy UI | needs smoke-latency cut + decision-result events |
| Complexity | small | medium | high | medium |
**Chosen: A→D, B1.** A first (visibility today), then B1 in serve+evidence (live honesty), C (both buttons fully functional), D (history depth). B2 recorded as a later optional toggle, not now — B1 already removes the black-box feel without token-stream cost. Design tokens: light canvas `#f4f5f7`, white cards, near-black text, seeded status colors holding AA-ish contrast on white; mono reserved for ids/JSON/timestamps; 13 px base type.

**Same-session addendum — live progress + explicit function calls + consolidated record (T-7.10, 2026-09-28, operator feedback after the first real-mode demo):** the demo ran real Gemini end-to-end, and the operator's next critique sharpened: *"show the function calls explicitly,"* *"show live pipeline progress,"* *"the UI is very weak,"* *"show complete incident details."* D-12 made detail *available on click*; the operator wanted it *in motion and in one place*. All fixes are client-side over the existing event vocabulary — zero new event types, zero new deps:

| Axis | A: richer per-stage expansion only | B: live stepper + explicit calls + consolidated record (chosen) | C: animated pipeline canvas |
|---|---|---|---|
| What the operator sees | detail still "somewhere behind a click" | a fixed six-node track (Ingest → Classify → Evidence → Plan → Gate → Resolve) with the active stage pulsing and a live elapsed ticker; evidence opens as one row per tool with the returned record count; one "Incident record" panel with every fact — alert payload, classification (model + duration), evidence by tool, plan (hypothesis, confidence, model, steps), gate wait/decision/actor, rollback, terminal, timings, raw JSON | a cinematic pipeline video |
| Honesty | unchanged | the live ticker is fed by `_arrived_ms` — a wall-clock **stamped by the client** at WS arrival (`useSocket`), so "waiting 42 s" is exactly what the operator observed, never a fabricated model delay; completed stages keep the server's real `stage_duration_ms`; an aborted run (`manual_review`) marks the offending step `!` and **dashes** the post-failure steps — never a green gate check over a gate that never opened | animation of timing we did not measure = fabricated evidence (rejected on the same principle as D-13 option C) |
| Failure modes / cost | none new | stepper state derivation must track the real event semantics (gate done ⟺ operator decision; resolve done ⟺ non-abort terminal) — pinned by code review + the terminal-honesty fix | idle motion on an operator tool; motion-doctrine violation; heavy |

**Chosen B.** The stepper's six steps derive solely from the existing event stream (`ingest/classified/tool_call/plan/gate_open/decision/done/manual_review`); the elapsed ticker is client-observed; the incident record reuses the same slice (no new selectors). The two honesty boundaries: (1) the live ticker is strictly client-observation (`_arrived_ms`) and never renders as a server timing claim, and (2) a step is `done` only when the corresponding event with real semantics arrived — a run that never reached the gate must render dashed, and a `done(manual_review)` must not light the Resolve node. (The latter was found and fixed during adversarial review of the first implementation: `terminal` used to force `Gate ✓` green.)

### D-13 Watchable rehearsal — smoke incidents must visibly stream (2026-09-28, user-found)

Post-demo operator feedback: *"when I click [fire] it seems predefined."* Root cause was not faked data — smoke (rehearsal) mode is deterministic by design (zero-quota sandbox, NFR-5) — **the problem was delivery speed**: the whole smoke run finished in <100 ms, every stage arriving in the same frame, which is indistinguishable from a pre-recorded script. The operator cannot watch a black-box that already finished, no matter how much detail it reveals on click.

| Option | What the operator sees | Failure modes / cost |
|---|---|---|
| Delay inside the smoke fakes (`app/benchmark.py`) | stage `stage_duration_ms` stamps become real wall-clock of the paced stream | **poisons the benchmark**: tests + golden-replay + run_live_benchmark all share those fakes and would slow by seconds-per-incident |
| Pace in the console's graph build only (`scripts/run_console.py`) via a small `paced_stage(seconds, pace)` wrapper | same visible stream; benchmark/test fakes untouched (instant) | none real: wrapper is pure `asyncio.sleep` before delegating; `NEXUSOPS_SMOKE_PACE=0` opts back to instant |
| Frontend-only arrival animation | stages "staged in" visually | **dishonest**: the backend already produced everything; animating arrival fabricates timing we did not measure |

**Chosen: pace in the console's graph build only.** `paced_stage` lives in `app/benchmark.py` (home of the smoke sinks) but is applied **only** by `run_console --smoke`; benchmark/tests never call it. Per-stage paces: classify 0.5 s, evidence 0.9 s (two `_tool`-tagged MCP record fetches), rca/plan 1.4 s, rollback 0.5 s → a fire visibly steps through ~3.4 s to the gate, then the human decision streams the tail. Honesty contracts unchanged: labels still SMOKE (header chip), evidence/plan still the deterministic fixtures, and `stage_duration_ms` now truthfully measures the paced stream's wall-clock. Data is never fabricated — only delivered more slowly. Regression guards: `paced_stage` must really sleep then delegate losslessly; zero pace is instant; and the benchmark's own fakes must stay unwrapped.

**Same-session addendum — real-mode transient retry (2026-09-28):** wiring the console for live Gemini runs exposed that the RPI plumbing calls each model exactly once (`build_graph`'s `_openrouter`). Free-tier Gemini is intermittently capacity-limited (`503 high demand` — measured 1-in-2 on `gemini-3.8-flash` during pre-flight), and a single hiccup would degrade an entire live incident to `manual_review` before the operator saw anything classify. The console's real graph (`scripts/run_console._real_make_graph`) now wraps classify/rca in the benchmark's existing, tested `_retry_model` (2 attempts, 1 s backoff) — confining the retry to the served console, exactly like the smoke pacing. Hard errors (bad key, broken contract, quota exhaustion) still surface immediately and honestly; the final attempt raises, so latency metrics include real retries.

### D-14 RCA model is operator config — `NEXUSOPS_RCA_MODEL` (2026-09-28, user-initiated)

After the real-mode demo the user asked whether the RCA stage — the only frontier consumer in the incident path — should simply run on the SLM. D-4 chose the frontier for escalated deep-dives; free-tier `gemini-3.8-flash` flaps intermittently even after D-13's retry (retry masks a queue ball; it cannot make the model cheap or the demo bulletproof). Decision: **the RCA depth-model is operator config, default preserves D-4.**

| Axis | A: keep D-4 fixed (frontier for escalated) | B: one env, two readers (chosen) | C: per-fire UI dropdown |
|---|---|---|---|
| Latency | frontier-with-retry worst case ≈ 10 s+ on flap days | `NEXUSOPS_RCA_MODEL=slm` → whole pipeline on flash-lite, ~1–2 s RCA, no retry tax | identical to B + a round-trip to pick |
| Cost | free-tier frontier is quota-strained; flaps cost retries | SLM mode has zero frontier calls | same as B |
| Failure modes | demo reliability hostage to frontier capacity on the critical path | two readers of one env could drift — pinned: the node, the stamp, and `/api/status` are regression-tested to agree under both override and default | a per-run choice needs the checkpoint/db to carry config; a console-only lie for a webhook-first system |
| Complexity | zero | one env read in `state_machine.make_rca_node` + one in `serve._inspect` (`_rca_label`) + `/api/status.rca_model` + `--slm-rca` flag on `run_console` | high |

**Chosen B, default `frontier` (D-4 semantics intact).** Honesty contract identical to classify's: the consuming node and the stamp read the **same** env, so the plan row can never name a model that didn't write the plan. `NEXUSOPS_RCA_MODEL=slm` (or `run_console --slm-rca`) drops the frontier out of the whole incident path — cheaper, faster, immune to the free-tier flapping, at the honest cost of shallower root-cause prose. The two readers are pinned by tests: the state-machine spy asserts which model env the node hands the model fn (escalated → frontier by default, SLM under override), and the driver test asserts `plan.model_env`, `escalating.depth_model`, and `/api/status.rca_model` all name the SLM under override — an aggregate one-line "rca_model" is the configuration snapshot; per-event stamps remain the runtime truth.

### D-15 Opaque model failures → typed, never-empty reasons (2026-09-28, user-found)

Live console report: *"we are getting the model error on every call."* Empirical diagnosis (two direct probes through the exact real path, `NEXUSOPS_SLM_MODEL` classify): call 1 failed with `ModelError("LLM request failed for gemini-3.5-flash-lite: ")` — **an empty message**; call 2 succeeded (critical/0.95). Conclusion: the failures are the provider's free-tier transient flapping (the D-13 retry window exists for exactly this), but the operator experience is broken for a different reason — **the failure text is empty**. `_chat` raises `f"...: {e}"` where the httpx cause produced an empty `str()`, and the state machine wraps it as `f"SLM unavailable: {exc!r}"` — so the console renders a bare "model error" that teaches nothing and reads as systemic. "Every call" was a flap burst (all calls land in `manual_review` while the provider is congested), amplified by an unreadable reason.

| Option | What the operator sees | Failure modes / cost |
|---|---|---|
| A: keep as-is | "model error" with an empty tail | honest but opaque — the demo reads as broken, the real class is invisible |
| B: typed, never-empty reasons (chosen) | `ModelError.kind ∈ {transport, http, quota, schema, empty, config}`; message construction falls back to the exception class name when `str(e)` is empty; the state machine composes `stage — kind — human detail`; UI tags the class (quota → red "provider daily cap", transport → amber "transient") | must thread `kind` through `_chat`/`ModelError`/nodes and pin both sides with tests |
| C: vendor SDK error parsing | vendor-native codes | overkill — one provider today, two tomorrow, none share an error model |

**Chosen B.** The printed reason always answers *what class of failure* (quota vs transient transport vs schema contract) and *what the operator can do* (fund the key / retry — the console already does / report a contract bug). Guardrail: a regression test injects an httpx-style cause whose `str()` is empty and asserts the composed ModelError message is non-empty AND kind-tagged; the D-6 nodes compose from `kind` so `manual_review` reasons are readable sentences, not exception reprs.

### D-16 Operator detail batch — problem facts, plan reasoning, decision reason, MCP results (2026-09-28, user-found)

Same live report, three UI gaps: *(1) "not enough details of the problem we are receiving in the incident phase,"* *(2) "not enough details in the reasoning section — we have to show the reasoning and why we take this decision,"* *(3) "there should be the result of the mcp calls to show."* Each is a small, honest data-flow fix — no new event types, no fabricated numbers:

| Gap | A: status quo | B: chosen fix | Rejected |
|---|---|---|---|
| Incident phase | alert row shows service/source/message/hint | the `ingest` event already drops `occurred_at` + `status_code` (they were on the alert, never published) → publish them; fixtures gain a `summary` (1–2 sentence problem statement) + `context` (structured facts: endpoints, error rates, recent deploy, signals); incident record renders a "Problem" section | C: model-generated narrative — unnecessary token cost |
| Reasoning section | plan has `root_cause_hypothesis` + per-step `reason` but no WHY-chain | `RCA_SCHEMA` gains required `reasoning` (the model explains: why this hypothesis, how each evidence item supports/refutes it, why these actions in this order); rendered prominently above the steps; the gate gains an optional operator-supplied `reason` ("why we take this decision") stored with the `decision` event | C: D-12 B2 token stream (already recorded as later toggle) |
| MCP results | evidence rows show per-tool record counts; records hidden behind a click | `tool_call.evidence` was always carried in full — the incident record renders each returned record inline (timestamp/level/message) by default | — |
| Cross-cutting | — | decision frame accepts optional `reason` (validated at the socket boundary); the console's Incident record + timeline render it | — |

**Chosen B everywhere.** The decision `reason` is strictly optional (the gate must never require prose — NG-1 is the decision itself), passes through the same `_route_decision` validation seam, and rides the resume body so the checkpoint stores *why* with *what*. `RCA_SCHEMA.reasoning` adds one required string; smoke/benchmark fixtures and `PLAN_REQUIRED_KEYS` follow (both are pinned by existing schema-consistency tests). The incident `context` is sender-provided data, not ground truth — safe to expose on `/api/fixtures` (the fixtures-API test's allowed-field set grows explicitly).

### D-18 Production-readiness audit — metric framework + baseline scorecard (2026-10-01, user-initiated)

**Problem:** the operator asked, plainly: *"is our production grade or not — and what are the metrics of a system that doesn't break in production?"* The answer must not be a vibe. Adopted definition (the framework that makes the audit a number, not an opinion): **a system is production-grade when it breaks *rarely, small, loudly, and heals fast* — and each of those four properties is provable by numbers.** Recording the grading criteria in the spec (not in a review comment) is what gives every future "is X production-ready?" question an owner.

**Decision 1 — the metric framework is part of the spec.** Two families, both required.

*Family 1 — runtime reliability (the numbers you report):*

| Metric | Definition |
|---|---|
| Availability + error budget | SLO (e.g. 99.9%) with a *spendable* error budget — budget burn alerts **before** customers feel it; a static promise with no budget is a plaque, not a metric |
| Error rate | per-operation failure rate, **classified** — the typed `ModelError.kind` taxonomy (D-15) is exactly the right shape; quota vs transport vs contract must be distinguishable in the number |
| Latency percentiles | p50/p95/p99; for an LLM pipeline, **per-stage** (classify vs evidence vs RCA) — the tail is vendor-bound and must be reported separately, not folded into one number (NFR-1 already does this) |
| Durability | events lost per N; delivery semantics — **at-least-once vs exactly-once vs effectively-once** must be *named*, not assumed |
| Freshness | alert → operator sees it, within a budget |
| Throughput + dead-letter | queue depth under load; incidents that can never be processed surface as a named count, not silence |

*Family 2 — failure-prevention engineering (whether it breaks at all):*

| Metric | Definition |
|---|---|
| MTTD | how fast you *know* it's down — alarms, not log-diving |
| MTTR + blast radius | how fast you heal, and how far a failure spreads |
| DORA-4 | deployment frequency, lead time for change, **change failure rate**, time to restore — CFR is the single best predictor of "breaks in production": most breakage is *introduced by change*, so the change pipeline is the highest-leverage place to measure |
| CI gating | tests that **block** a merge, not tests that pass after the fact |
| Reproducibility | replay a past incident and get the same answer — the anti-regression property |
| Fail-fast + degrade | failures announce themselves (loud, typed, contained) and degrade along designed paths — never silently |
| Auth/authz + state durability | least privilege per user; state survives process death |

**Decision 2 — the 2026-10-01 baseline audit (current state, graded against the framework).**

| Metric | Production target | NexusOps today (2026-10-01) |
|---|---|---|
| Availability | SLO + supervisor + auto-restart | ✗ **missing** — crash = manual relaunch (observed the day this was written: the console died with the server and was relaunched by hand) |
| Error rate | budgeted (~<0.1%) | ⚠ **flaky by design** — free-tier SLM 503 bursts; last real-mode run answered 2/6 with every outage an honest `manual_review` (D-6/D-9/D-14) — the degrade machinery is right, the budget is absent |
| Latency p95/stage | budgeted per stage | ✗ not measured systematically outside benchmark/golden runs |
| Durability | effectively-once, semantics named | ⚠ at-least-once + Redis dedupe (D-2) — but the served console flushes its queue + seen-set at startup, so a restart can re-deliver and re-open a gate; in-flight loss is accepted (D-3), restart *reprocessing* is an unowned risk (R-9) |
| Freshness | alert→gate SLO | ✗ unbudgeted |
| MTTD | auto-alert, minutes | ✗ **dead telemetry sink** — OTLP retry noise against a down collector (D-7 stack) is the only signal; breakage is found by reading logs (R-8) |
| MTTR | staged, human-gated rollback | ⚠ real scoped rollback + live gate exist (D-10/D-11, GitHub tag) — right shape, minimal blast scope; no app-level recovery path |
| DORA-4 | CI blocks bad changes, CFR measured | ✗ **zero CI** — 104 tests are a claim, not a gate; CFR/lead-time unmeasurable (R-7) |
| Replay determinism | reproduce past incidents | ✓ **strong** — golden replay (D-8), record/judge split, smoke(0-token)/real, judge `inconclusive` on failure (never a silent score) |
| Fail-fast + degrade | loud, typed, contained | ✓ **strong** — D-6 `manual_review`, D-15 typed never-empty kinds, D-16 mandatory reasoning, NG-1 pre-approval lockdown, honest queued feedback (D-11 postmortem #5) |
| Auth/authz | least privilege per user | ✗ none beyond NG-3's single shared key — anyone on the network can read incidents and press the gate (R-6) |
| State survives restart | durable ledger | ✗ gates/seed/dedupe held in memory + Redis queue; no incident ledger (future D-17) |

**Conclusion (verbal, defensible): NexusOps today is not production-grade as a system for a user base — its AI-specific discipline is production-grade, and the gap is exactly located.** The asymmetry is the natural shape of a demo-that-grew-machinery: the interesting layers (pipeline, honesty, eval) received real investment; the uninteresting-but-load-bearing layers (supervision, auth, CI, persistence, alarms) did not — which is precisely where production systems fail. Naming the framework in the spec is what gives these gaps owners.

**Decision 3 — closure plan, in leverage order.** Each item becomes its own spec + task file when a real tenant is named; none execute against a user base until then (read-only posture unchanged).

1. **Process supervision + watchdog** (launchd/systemd + `/api/status` probe restart) — kills the unmonitored-downtime class; ~30 min of work.
2. **Durable incident ledger** (future **D-17**) — fixes restart durability *and* dedupe persistence *and* becomes the eval dataset of the observe→improve loop: one build, three wins.
3. **Auth on console + API** — mandatory before any real user or real data, even read-only.
4. **CI on GitHub Actions** — pytest + `tsc -b` + Vite build + smoke benchmark gate on every merge; turns "104 tests" from a claim into a guardrail.
5. **Stand up the OTLP sink or rip it out** — a dead collector is a lie in the telemetry; restores real MTTD.
6. **Two SLOs once real data flows** — alert→gate freshness, quota-fallback rate. SLOs need the ledger (2) to be honest.

## 2. System Architecture

```
                 ┌────────────────────────────────────────────────┐
                 │            NexusOps (one process)              │
 alert ──POST──▶ │ FastAPI: validate (Pydantic v2, extra=forbid) │
 (webhook)       │           dedupe by incident_id (Redis SET)   │
                 │           enqueue ──▶ Redis LIST (LPUSH)      │
                 │                                                │
                 │   worker loop (per incident, isolated state)  │
                 │        │                                       │
                 │        ▼                                       │
                 │   ┌──────────────────────────────────────┐    │
                 │   │        LangGraph StateGraph          │    │
                 │   │  (each incident gets its own graph)  │    │
                 │   │  1. SLM classify ──▶ {severity, conf,│    │
                 │   │     affected_service, ambiguous}     │    │
                 │   │  2. escalate?  (rule D-4)            │    │
                 │   │  3. MCP tools ──▶ fetch_service_logs │    │
                 │   │     ──▶ query_prometheus_metrics     │    │
                 │   │  4. RCA ──▶ remediation plan (§5.3)  │    │
                 │   │  5. GATE ◀── decision (WS msg | POST │    │
                 │   │      §5.4) └ approve → rollback tool │    │
                 │   │             └ reject → state=rejected│    │
                 │   │  6. terminal state + events emitted  │    │
                 │   └──────────────────────────────────────┘    │
                 │        │           │           │              │
                 │        │           ▼           │              │
                 │   checkpointer        trace (Otel/Phoenix)   │
                 │   (MemorySaver)                              │
                 │        │                                       │
                 │        ▼                                       │
                 │   WebSocket server ──events down──▶ dashboard  │
                 │              ◀──decision (§5.4) up──           │
                 └────────────────────────────────────────────────┘
```

## 3. Component Responsibilities

| Component | Responsibility | Notes |
|---|---|---|
| **FastAPI receiver** | Accept, validate, dedupe, enqueue | 422 on malformed; dedupe key = `incident_id` (Redis SET) |
| **Redis queue + worker** | Hold + consume the incident tray | LPUSH/BRPOP; each incident → own graph instance (NFR-3) |
| **LangGraph StateGraph** | Enforce the 6-step flow, per incident | deterministic transitions; no loop paths |
| **SLM stage** | First-pass classification + confidence + ambiguous flag | OpenRouter SLM (D-5) |
| **Escalation rule** | Decide SLM→frontier per D-4 | plain function, unit-tested |
| **MCP stage** | Call the 3 mock tools; record args+results in trace | real MCP protocol, mocked backends (NG-2) |
| **RCA stage** | Produce remediation plan §5.3 | SLM or frontier; strict JSON or fail loud |
| **Gate + approval endpoint** | Pause; receive human decision §5.4 (POST or WS); resume or reject | first-decision-wins; store decision in checkpointer state |
| **MCP mock server** | `fetch_service_logs`, `query_prometheus_metrics`, `trigger_github_rollback` | distinct "no data" vs "doesn't exist" errors (§5.2) |
| **WebSocket stream** | Emit ordered per-incident events; accept §5.4 decision messages | events: ingest, classified, escalating, tool_call, plan, gate_open, decision, done, manual_review; reconnect catch-up (FR-6) |
| **Trace pipeline** | Record durations/models/tools per incident_id | OpenTelemetry → Phoenix |

## 4. End-to-End Walkthrough (one incident)

1. Alert POSTs → FastAPI validates & dedupes (Redis SET) → LPUSH to Redis queue → `202`.
2. Worker BRPOPs the incident and starts a fresh StateGraph for it.
3. **SLM stage** classifies → `{severity, affected_service, confidence, ambiguous}` (+ trace).
4. **Escalation rule** runs (D-4). Critical or unsure or low-confidence → frontier model takes over the RCA stage.
5. **MCP stage** calls `fetch_service_logs` + `query_prometheus_metrics`; results land in the trace and the graph state.
6. **RCA stage** writes a remediation plan (§5.3 strict JSON). A bad plan (schema violation) fails the run loud.
7. **Gate**: plan has destructive steps → pause. Dashboard shows *waiting for approval*. State preserved by checkpointer.
8. Operator sends the §5.4 decision as a WebSocket message (or POST). `approve` → mock rollback fires once, `state=resolved`. `reject` → `state=rejected`, no tool fires.
9. All events streamed live over the WebSocket; a reconnecting dashboard receives missed events (FR-6); full trace queryable by `incident_id`.

## 5. Failure Modes & Responses

| Failure | Behavior | Violates |
|---|---|---|
| Malformed alert payload | 422 strict JSON error; not enqueued | — (loud by design) |
| Duplicate `incident_id` | deduped by Redis SET, no re-triage — also after restart | — |
| Redis down | receiver → 503, loud; worker stalls; no degraded mode | NFR-6 (if run anyway) |
| Process crash mid-incident | progress lost; alert already seen → not re-reprocessed (abandoned incident needs a new alert) | accepted (FR-1/D-2/D-3) |
| SLM error/timeout | `state=manual_review`, loud, stop | NFR-4 if silent |
| Frontier error/timeout | `state=manual_review`, loud, stop | NFR-4 if silent |
| Tool returns structured error | passes into trace + graph state; RCA must handle or go manual_review | silent swallow |
| WS client disconnect | client reconnects; server replays missed per-incident events | FR-6 (if no catch-up) |
| Unauthorized approval attempt | rejected (single shared API key) | — |
| Second approval decision | `already_decided`; first wins | — |

## 6. Non-Decisions & Marked Upgrades

- **D-2:** ack/lease reprocessing of lost in-flight incidents — when crash survival of in-flight work is required.
- **D-3:** SQLite checkpointer — when losing a human-waited incident on restart is unacceptable.
- **D-5:** exact SLM/frontier model IDs on OpenRouter — pinned after a live API call in the first benchmark/triage run; env-gated so any catalog model is a config change.
- **NG-1:** no unattended remediation — permanent boundary, enforced in the gate.
- **NG-2:** mock tool backends — MCP protocol is real; only the backend data is fake, keeping the tool interface swap-for-real in place.

## 7. Open Risk Register

- **R-1:** SLM latency (hosted OpenRouter, D-5) must meet NFR-1 (P95 < 2.5s, SLM-only path). Mitigation: small/cheap model + short classifier prompt + structured output; measured in EVERY benchmark run (default mode).
- **R-2:** Frontier-call latency is vendor-bound. Mitigation: excluded from NFR-1; reported separately; timeouts route to `manual_review`.
- **R-3:** WebSocket ordering + reconnect catch-up must stay consistent — a reconnecting client may have missed events mid-drop. Mitigation: per-incident event replay on connect (FR-6); load-test at 30 incidents in the benchmark.
- **R-4:** Redis is an extra runtime dependency for every test/benchmark run. Mitigation: NFR-6 refuses degraded operation; install command in `specs/features/webhook-ingest/tasks.md`; `redis-server` started as part of any run instructions.
- **R-5 (D-18):** no process supervision — a crash or host restart leaves the console dead until a human relaunches (observed 2026-10-01). Mitigation: closure item 1 — launchd/systemd supervisor or container orchestration + `/api/status` watchdog restart.
- **R-6 (D-18):** no authz on the console or API beyond NG-3's single shared key — anyone on the network can read incidents and operate the gate (press approve → trigger a real rollback tag via D-10). Mitigation: closure item 3 — mandatory before any real user or real data, even read-only.
- **R-7 (D-18):** zero CI — the 104-test suite is a claim, not a gate; a breaking change can reach `main` unmeasured; CFR/lead-time are unmeasurable (DORA-4). Mitigation: closure item 4 — GitHub Actions running pytest + `tsc -b` + Vite build + smoke benchmark on every merge.
- **R-8 (D-18):** dead telemetry sink — OTLP retry noise against a down collector (D-7) is the only signal, so MTTD is log-diving, not alarms. Mitigation: closure item 5 — stand the collector up (Jaeger stack exists, `scripts/start-jaeger.sh`) or strip the exporter so silence is truthful.
- **R-9 (D-18):** served console's queue + seen-set are flushed at startup — a restart can re-deliver an alert and re-open a previously-decided gate (D-2's "dedupe survives restarts" holds for the ingest layer, not the console's own working set). Mitigation: closure item 2 — durable incident ledger (future D-17) + persistent dedupe.
- **R-10 (D-18):** free-tier quotas are a runtime dependency (SLM ~500 req/day with 503 bursts; frontier ~20 req/day, hard 429 cap) — acceptable for rehearsal by design (D-6/D-9/D-14 honest degrade), not for a production SLA. Mitigation: closure item 6 — SLOs with a quota-fallback budget once real data flows; funded tiers if a tenant signs.

## 8. Where Per-Feature Detail Lives

Each feature is decomposed into its own spec + task file (per AGENTS.md, never one giant app-wide task list). Shared contracts (§5 of `requirements.md`, decisions above) are **referenced, never duplicated**, in feature files.

| Feature | Spec / Tasks |
|---|---|
| 1. Webhook ingestion | `specs/features/webhook-ingest/` |
| 2. MCP server | `specs/features/mcp-server/` |
| 3. State machine | `specs/features/state-machine/` |
| 4. Streaming dashboard | `specs/features/streaming-dashboard/` |
| 5. Benchmark harness | `specs/features/benchmark/` |

Build order: 1 → 2 → 3 → 4 → 5 (later features depend on earlier ones).