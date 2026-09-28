# NexusOps

**Autonomous incident triage, root-cause analysis, and remediation for SRE teams.**

NexusOps takes raw alert payloads at a webhook, classifies them with a lightweight
model, escalates ambiguous or critical incidents, drafts a remediation plan, and
enforces a **human-in-the-loop approval gate** before any change hits a live
system — with a streaming dashboard for operators and a benchmark harness that
grades the whole pipeline end-to-end.

```
  alert ──▶ webhook ingest ──▶ Redis queue (atomic dedupe)
                                   │
                                   ▼
                        SLM classify (severity / confidence)
                                   │
                ┌── critical · low confidence · ambiguous ──┐
                │                 │                          │
                ▼                 ▼                          ▼
        frontier RCA     remediation plan            manual_review
                │                 │                  (loud fail, D-6)
                ▼                 ▼
            ┌─── approve/reject gate (human-in-the-loop) ───┐
            ▼                                                ▼
      remediate · rollback · resolve               ────────────────
                                   │
                                   ▼
                 streaming dashboard (WebSocket) + benchmark judge
```

All requirements, design decisions, and per-feature tasks live in **`specs/`**,
which is the source of truth (spec-driven development; no code without a spec).

---

## Highlights

- **Spec-driven from day one.** `specs/requirements.md` (WHAT), `specs/design.md`
  (HOW, incl. decision log), and per-feature spec + task files. Shared contracts
  (schemas, SLAs, tool signatures) live once in the root files and are referenced
  by feature files — never duplicated.
- **Five features, each through a strict 5-phase pipeline.** Socratic architecture
  review → minimalism → typed implementation → adversarial review → conceptual
  validation. Every closed task records *what was verified*.
- **No silent failures (NFR-4).** Any model/provider failure routes loudly to a
  `manual_review` state rather than failing quietly or mid-flight (design decision
  D-6). This was exercised live against rate-limited and unfunded providers.
- **Provider-agnostic model layer.** Any OpenAI-compatible endpoint works via a
  single env var; both OpenRouter and alternative providers are verified.
- **Deterministic, CJ-safe regression harness.** A smoke benchmark with injected
  fakes runs in seconds — zero tokens, reproducible, exit-coded for CI.
- **Live LLM-as-judge benchmark.** 30 curated synthetic incidents (B-1 coverage),
  graded by a frontier model through strict structured output: severity, root
  cause, plan actionability, gate compliance, plus NFR latency/token/MTTR metrics.

---

## Repository layout

```
specs/                        # Source of truth: requirements, design, tasks
├── requirements.md          # Global WHAT + acceptance criteria
├── design.md                # Global HOW, SLAs, architecture decisions (D-1..D-6)
├── tasks.md                 # Task board linking every feature
└── features/
    ├── webhook-ingest/      # Feature 1: Redis-backed ingestion
    ├── mcp-server/          # Feature 2: evidence/rollback tool server (MCP)
    ├── state-machine/       # Feature 3: LangGraph triage + gate state machine
    ├── streaming-dashboard/ # Feature 4: WebSocket operator dashboard
    └── benchmark/           # Feature 5: regression + LLM-judge harness

app/
├── ingest.py                # FastAPI webhook → atomic dedupe + enqueue (Lua)
├── models.py                # OpenAI-compatible client (strict JSON contract)
├── state_machine.py         # LangGraph: classify → RCA → plan → gate → remediate
├── evidence.py              # MCP evidence client (logs, metrics)
├── mcp_server.py            # 3 MCP tools + approve/rollback hook
├── bus.py                   # In-process pub/sub (Feature 4)
├── dashboard.py             # WebSocket dashboard app (approve/reject)
└── benchmark.py             # Fixtures, worker, LLM judge, report, CLI

scripts/
├── run_live_benchmark.py    # Seeds Redis via the real webhook, then live run
└── smoke_mcp.py             # Deterministic MCP tool smoke test

tests/                       # 51 tests across the 5 features
```

---

## Technology stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| API / sockets | FastAPI, uvicorn, starlette WebSockets |
| Orchestration | LangGraph (StateGraph + MemorySaver checkpointer) |
| Provider client | httpx (provider-agnostic, OpenAI-compatible chat completions) |
| Tooling protocol | MCP (`mcp` SDK 2.x) |
| Queue / dedupe | Redis (atomic Lua `SADD`+`LPUSH`) |
| Validation | Pydantic v2 (strict JSON, unknown fields rejected) |
| Testing | pytest, starlette TestClient |

---

## Getting started

### Prerequisites

- Python 3.12
- Redis (`redis-server`), listening on `localhost:6379` by default
- A model-provider API key for **live** model calls (OpenRouter by default)

### Install

```bash
git clone <repo-url> NexusOps
cd NexusOps
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

### Configure

Set the model provider in `.env` (copy from `.env.example`):

| Variable | Purpose | Default |
|---|---|---|
| `NEXUSOPS_LLM_API_KEY` | Provider API key (required for live) | — |
| `NEXUSOPS_LLM_API_URL` | Any OpenAI-compatible base URL | `https://openrouter.ai/api/v1` |
| `NEXUSOPS_SLM_MODEL` | Small/cheap/fast classifier | `openrouter/auto` |
| `NEXUSOPS_FRONTIER_MODEL` | Heavy lifting (RCA, judge) | `anthropic/claude-sonnet-4` |
| `NEXUSOPS_RCA_MODEL` | Root-cause stage model: `frontier` (D-4: deep-dives escalated incidents) or `slm` (whole pipeline on the SLM — cheaper, faster, no free-tier frontier 503s; shallower root-cause prose, operator-chosen) | `frontier` |
| `NEXUSOPS_REDIS_URL` | Redis connection | `redis://localhost:6379/0` |

`.env` is gitignored and loaded automatically (stdlib, no extra dependency).
Set the same variables in a real environment to override `.env`.

> **Note on unfunded keys.** Free-tier (`:free`) OpenRouter models are heavily
> rate-limited; paid models return `402` on an unfunded key. For a meaningful
> live benchmark, fund the key and use paid models. It is a *provider* property,
> not a NexusOps one.

---

## Running the components

### 1. Ingestion webhook

```bash
.venv/bin/uvicorn app.ingest:app --host 0.0.0.0 --port 8000
```

```
POST /webhook/incident
Content-Type: application/json
{"incident_id": "c-01", "occurred_at": "...", "service": "db",
 "message": "connection pool exhausted", "severity_hint": "critical"}
```

→ `202` and an atomic dedupe decision (`duplicate: true|false`). Strict Pydantic
validation rejects unknown fields; `503` when Redis is unavailable (sender
retries, payload was never accepted).

### 2. Evidence & rollback server (MCP)

```bash
.venv/bin/python -m app.mcp_server
```

Three tools over the MCP protocol: `fetch_service_logs`, `query_prometheus_metrics`,
`trigger_github_rollback`. Data is deterministic/synthetic in this build
(ponytail-deferred real integrations — see `app/mcp_server.py`). The internal
`approve_rollback()` hook is what the state machine actually calls.

### 3. Streaming dashboard

```python
# compose_modules.py
from app.bus import EventBus
from app.state_machine import build_graph
from app.dashboard import create_dashboard_app, make_resume_incident

bus = EventBus()
graph = build_graph()  # OpenRouter classify/RCA + MCP evidence
dashboard = create_dashboard_app(bus, make_resume_incident(graph))
```

```bash
.venv/bin/uvicorn compose_modules:dashboard --port 8080   # open http://localhost:8080
```

Incidents stream over `/ws`; operators approve or reject the gate decision, which
resumes the correct parked state-machine thread.

### 4. Benchmark (the pipeline driver)

```bash
# Deterministic — zero tokens, CI-safe, seconds
.venv/bin/python -m app.benchmark --smoke

# Live — real models against real Redis (see scripts/run_live_benchmark.py)
.venv/bin/python -m scripts.run_live_benchmark

# Two-phase replay (rate-limit escape hatch): record the pipeline, judge later
.venv/bin/python -m scripts.run_live_benchmark --record out.json   # phase A: pipeline only
.venv/bin/python -m scripts.run_live_benchmark --judge out.json    # phase B: grade the recording

# Quick run — stratified slice, fits a free-tier day (~25 calls instead of ~90):
.venv/bin/python -m scripts.run_live_benchmark --record out10.json --limit 10
.venv/bin/python -m scripts.run_live_benchmark --judge out10.json # grades exactly the 10 recorded
```

The live driver seeds 30 synthetic incidents *through the real ingestion
webhook* (dedupe included), consumes the queue with the real worker, and grades
every incident with an LLM judge. Emits a JSON report (latency P95, MTTR, token
cost, per-metric pass counts, tool inventory) and a non-zero exit code on any
failure: verdict miss, NFR-2 plan violation, NFR-1 budget breach, or a
starvation-loud `manual_review`.

### 5. Tracing (Jaeger + OpenTelemetry)

Every LLM call opens a `gen_ai.*` OpenTelemetry span (SLM classify, frontier
RCA/plan, and the LLM judge). No collector → spans print to stderr (zero-infra
fallback). With the collector up, they flow over OTLP/HTTP and you get the
waterfall in a browser:

```bash
scripts/start-jaeger.sh          # idempotent; starts the all-in-one container
open http://localhost:16686      # service: nexusops -> incident -> LLM-call spans
```

`.env` already sets `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`, which
every run honors (the provider is wired from `.env` before the tracer SDK boots).
Override per-run with `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` for a full URL, or
unset the var to fall back to stderr.

### 6. Live operations console (the demo machine)

One process IS the live system — webhook, pipeline worker, WebSocket stream,
human gate, real scoped rollback, and the frontend:

```bash
# Rehearsal mode — deterministic fakes, ZERO model quota; stages are paced
# (~0.5 s classify / ~0.9 s evidence / ~1.4 s plan) so you can watch an
# incident run step-by-step. NEXUSOPS_SMOKE_PACE=0 restores instant mode.
.venv/bin/python -m scripts.run_console --smoke            # serves on :8137

# Real mode — live Gemini calls when .env carries the key (D-9), default;
# transient provider hiccups (free-tier "high demand" 503s) are retried
# once before any honest degrade
.venv/bin/python -m scripts.run_console --port 8137
# --slm-rca     run EVERYTHING (incl. the root-cause plan) on the SLM —
#               faster, cheaper, no frontier 503s; shallower root-cause prose
# --keep-redis  do NOT flush the queue + seen-set at startup (console sandbox)
```

Then open **http://localhost:8137** (professional light UI — the console is an
operator's tool, not a terminal):

1. **Live tab** — pick a fixture ("fire live") or POST any valid alert to
   `/webhook/incident`. The pipeline streams every stage: classified →
   escalating → evidence → plan → **GATE OPEN**, each with its real duration
   (ms), and the header shows **SMOKE/REAL** plus the active **RCA model** so
   you always know whose arithmetic you're watching. A **pipeline stepper**
   (Ingest → Classify → Evidence → Plan → Gate → Resolve) pulses the live stage
   with elapsed-now ticking (client-observed wall clock — an observation, never
   a fabricated model delay); if a run aborts, the failing step goes `!` and the
   steps after it are dashed — a gate that never opened is never shown as done.
2. **Click any stage** to open its full detail: evidence cards with their origin
   tool (`fetch_service_logs` / `query_prometheus_metrics` — now also rendered
   as one visible function-call row per tool with its record count), the RCA
   plan with confidence + steps, the model that ran per stage, raw JSON —
   everything the event actually carried. Below the gate, the **Incident
   record** consolidates the whole run — alert payload, classification,
   evidence-by-tool, plan, gate wait/decision/actor, rollback tag, terminal
   state + resolve total, and a raw-JSON toggle. No fabricated numbers:
   `stage_duration_ms` is the measured time between pipeline steps, and
   post-gate stages restart their clock at your decision so "rollback" never
   claims the minutes you spent thinking.
3. **The gate is a human (NG-1).** The pipeline parks; you click **Approve
   rollback** (real scoped GitHub tag rollback when `NEXUSOPS_GITHUB_REPO`/
   `TOKEN` are set, D-10) or **Reject** (nothing fires). The instant you click,
   the gate answers: "Decision recorded — approve/reject (EXECUTING/CLOSING)",
   then streams the tail live. A second decision on the same gate is refused
   (`already_decided`, first wins). Re-firing the same fixture opens a
   **fresh** gate — each run needs its own human decision.
4. **History tab** — the feature-6 film (offline replay of a recorded run,
   honest `manual_review` included) mounted at `/film`.

Console-only rehearsal details: the picker fires with `?force=1` (bypasses the
dedupe seen-set so you can re-run a fixture; plain webhook POSTs stay dedupe-
by-default). Incidents run one at a time (a parked gate blocks later work by
design) — and the console says so: firing while a gate is parked shows
`queued <id> — <gate> parked at the gate; decide it first` in amber, and the
queued run carries a `QUEUED behind <gate>` banner until its run starts (so a
blocked queue never reads as a dead machine).

### Tests

```bash
.venv/bin/python -m pytest tests/
# → 96 passed
```

---

## Design principles worth knowing

- **The gate is mandatory (NG-1).** Automating *remediation* is the point; a
  human approves each rollback/remediation via the dashboard or operator tooling.
  Incidents never remediate autonomously in this build.
- **Loud manual fallback (D-6).** Model or provider failure is a *first-class
  state* (`manual_review`), not a swallowed exception. This has been battle-
  tested against real 402/429/empty-response providers.
- **Strict JSON contract (FR-4).** The model is pinned to structured output and
  the parser enforces it; a transport-level fence is unwrapped, but schema shape
  and content are never forgiven.
- **Dedupe is atomic.** `SADD` + `LPUSH` run in one Lua script — a crash can
  never mark an alert seen without enqueueing it (would silently swallow alerts).
- **Honest metrics.** The benchmark is sequential so NFR latencies are real
  measurements, not parallelism. A target that was never exercised fails, not
  vacuously passes (an empty SLM-only set => `nfr1_measured: false` => fail).

---

## Status

All seven features are closed through the full 5-phase pipeline (architecture →
minimalism → implementation → adversarial review → conceptual validation). The
live console runs one complete incident cycle end-to-end — real webhook → real
pipeline → human gate → real scoped rollback — with honest `manual_review` on
model/provider downtime. The benchmark ran live on free-tier providers; an
LLM-judge semantic verdict is one funded key away
(`python -m scripts.run_live_benchmark`).

Detailed, authoritative records live in `specs/` — feature acceptance criteria,
decision log, and per-feature task histories including live-run findings.