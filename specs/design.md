# NexusOps — Design Specification

Status: **Phase 1 decision record — APPROVED** (v0.2, revised for WebSocket + Redis). Answers HOW, for the WHAT defined in `requirements.md`. Each major decision below compares options across **latency, cost, failure modes, complexity** and records the choice + reasoning. Deep per-feature design lives in `specs/features/<name>/spec.md`; this file owns the shared architecture and cross-cutting decisions.

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