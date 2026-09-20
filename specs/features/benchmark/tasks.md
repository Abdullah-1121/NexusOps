# Tasks — 5. Benchmark & Regression Harness

Phase gates per AGENTS.md: 1 socratic-architect → 2 ponytail → 3 implementation → 4 adversarial-reviewer → 5 feynman. Task state: **todo → in-progress → done → blocked**. Update the board (`specs/tasks.md`) after each verified cycle.

| # | Task | Phases 1–5 | Verified |
|---|---|---|---|
| 5.1 | 30 fixtures (B-1 coverage) | 1-4 ✅ | `_RAW`/`build_fixtures`: critical×7, warning×9, info×6, ambiguous×5, malformed×3, dup×1 = 30 |
| 5.2 | LLM-judge scoring (B-2) | 1-4 ✅ | `judge_result` + `JUDGE_SCHEMA`: structured per-metric `{verdict, score, reason}` |
| 5.3 | Latency / token-cost / MTTR metrics + report (B-3) | 1-4 ✅ | `build_report`: P95 (SLM-only), escalated P95, MTTR, tokens/model (usage_sink), tool inventory |
| 5.4 | Non-zero exit + determinism (seeded, fixed mock data) | 1-4 ✅ | exit 0 iff all verdicts + NFR-2 + P95 + no manual_review; `--smoke` fully deterministic |
| 5.5 | Update board (`specs/tasks.md`) | ✅ | see board |

## Phase 1 decision (2026-09-08)
**APPROVED:** LLM-as-judge with structured scored output (via existing OpenRouter `complete_json`, temperature 0), NOT the DeepEval dependency. Determinism stance: structured shape fixed (NFR-5 inputs+mechanical asserts), semantic verdicts shipped with `reason`. DeepEval dropped (ponytail — duplicated model integration). Hard deterministic asserts beneath the judge: NFR-2 strict-schema plans, no-silent-failure. **The benchmark is Feature 3's deferred Redis consumer — add-when arrived.**

## Context7 stamps
- `VERIFIED FastAPI/starlette WS (Feature 4)` — reused here (no new external lib in Feature 5; judge reuses stamped `complete_json`, worker uses stamped `app.state_machine`). No new stamps required.

## Phase 4 findings (2026-09-08)
1. **Silent partial pass — fixed:** live worker that dried up early would end normally and report a pass over partial results. `consume_loop(strict=True)` (live) raises — a drained queue before N incidents is a loud failure; smoke still ends cleanly. Guarded by `test_consume_loop_strict_refuses_silent_partial_pass`.
2. **Dedupe claim unverified — fixed:** report asserted `deduped_at_ingest` without checking. Live mode now asserts `dup-01` ∈ ingest seen-set after the run, else raises. 
3. **NFR-2 too weak — strengthened:** `_plan_ok` now requires `remediation_steps` to be a structural list (each step has `action/target/reason`), not just key presence.

## Known couplings (recorded, deliberate)
- `smoke_classify`/`smoke_rca` parse the state machine's prompt strings (`"QUERY: "`, `"Incident: "`). A prompt change breaks smoke loudly (fakes raise in CI) — flagged, not hidden.

## Phase 4b findings — surfaced by the Phase-5 question (2026-09-08)
4. **Token ledger never plugged in — fixed:** `_openrouter` (and the live graph) never passed `usage_sink`, so B-3's `tokens_per_model` was silently empty in live mode. Added `_instrumented_complete(sink)`; live graph now uses it. Guarded by `test_live_model_calls_feed_usage_sink`.
5. **Vacuous NFR-1 pass — fixed:** with every incident escalated, the SLM-only set was empty, `p95=None`, and `(None or 0.0) <= 2500` passed. Now NFR-1 must be *measured* (`nfr1_measured`); empty set ⇒ fail. Guarded by `test_all_escalated_fails_nfr1_unmeasured`.

## Phase 4c — live-run findings (2026-09-14, free-tier)
Three bugs surfaced only by real traffic; all fixed, each with a regression test:
1. **Stale model ID:** `anthropic/claude-3.5-sonnet` no longer exists on OpenRouter (404). Verified live → default is now `anthropic/claude-sonnet-4`.
2. **HTTP 200-with-error body** → raw `KeyError: 'choices'` in `complete_json` (free-tier models answer 200 with an `error` body). Now a typed `ModelError` (D-6 routable), never a silent/raw crash.
3. **Judge metric-name mismatch** lost a full run building the report (`_judge_all`'s fallback used different keys than `JUDGE_SCHEMA`). Fixed structurally: one `JUDGE_METRICS` tuple feeds the schema, smoke judge, aggregation, and the error fallback.

**Live result (5th run, free judge `nvidia/nemotron-3-ultra-550b-a55b:free`):** all 29 → `manual_review` (free-tier 429 rate-limit on classify → D-6, as designed). `all_pass`: false, honest. Harness + pipeline proven live end-to-end; the model boundary (unfunded key) is the failure. **Re-run with a funded key for a meaningful semantic verdict:** `scripts/run_live_benchmark.py` (also: live `judge` timeout raised 20→90s, single-incident judge schema round-trip verified OK).
Entry point added: `scripts/run_live_benchmark.py` (webhook-seeds Redis → real worker → OpenRouter → judge).

## Phase 4e — two-phase offline replay (2026-09-15)
Free-tier judge bursts collide with classify traffic in one rate-limit window → a long wall of 429s makes the run report all-fail even though the pipeline succeeded. Split the benchmark:
- **Phase A `--record PATH`** — pipeline + evidence + gate only; saves every `IncidentResult` (+ fixtures + token/call ledger) to a JSON checkpoint and stops before judging.
- **Phase B `--judge PATH`** — loads a checkpoint and grades it with the frontier judge; no Redis, no pipeline work, infinitely replayable until the provider cooperates.
Checkpoint round-trip is guarded by a test (`test_checkpoint_round_trips_without_losing_fields`). Nothing about the single-pass default changed (`--record`/`--judge` are the free-tier escape hatch; a funded key just runs `scripts/run_live_benchmark` as before).

## Phase 4d — live-run findings (2026-09-14, second free-tier run, SLM fixed)
Full pipeline **proven live for real**: 29/29 consumed `200 OK`, gate auto-operator resumed every parked gate, 3 rollbacks + 14 evidence calls executed, token accounting live (28,560 SLM tokens), honest NFR-1 measured. Two defects from this run:
1. **F2↔F5 service-name contract drift:** fixtures run on `db/search/queue/api/ghost/orphan/dangling`; the synthetic evidence store only knew `{auth,payments,catalog,worker}` → every live `fetch_service_logs` loud-failed and starved RCA. Fixed: `SERVICES` union + `SILENT_SERVICES` (ghost/orphan/dangling exist-but-silent, honoring their "gone dark" stories), plus a guardrail test asserting every fixture service is queryable.
2. **`openrouter/auto` is not free:** routing the SLM through it selects a paid model → `402` storm, entire run to `manual_review`. SLM + judge now pinned to explicit `:free` models in `.env`.
Open provider caveats (not code): free-tier judge call phase rate-limits (`429` storm → judge default-fails everything, `pass_rate 0` — that's a *judge failure*, not a model-quality verdict), and free-tier SLM P95 ≈ 345 s vs NFR-1 2500 ms budget — latency is a provider property; a funded key is the only cure for both.

## Phase 4f — OTel GenAI tracing & evaluation (2026-09-16)
Implemented per D-7 (decision recorded in `specs/design.md`): `app/tracing.py` (idempotent init, OTLP/HTTP via `OTEL_EXPORTER_OTLP_ENDPOINT` else console, pinned GenAI semconv literals), `complete_json` instrumented as `llm.chat.completions` (system/request-model/usage/response attrs; ERROR + `record_exception` on failure), `nexusops.incident` parent span around `drive_incident`, and `llm.judge` with `eval.<metric>` verdicts per metric (evaluation-as-trace). Tests (`tests/test_tracing.py`, 3) run against a session-wide `InMemorySpanExporter` installed in `tests/conftest.py` — required because OTel `set_tracer_provider` is one-shot and conftest imports before every test module. Deps added: `opentelemetry-sdk`/`opentelemetry-api`/`opentelemetry-exporter-otlp-proto-http` (1.44.0).
**Verified:** 56/56 tests pass (was 53 + 3 tracing tests), MCP smoke green (handshake/list/call/gate), `--help` unchanged, live model probing unaffected. Guardrails: tracing failure must never break the call — spans wrap the HTTP round-trip inside `complete_json`, never raised.

## Phase 4g — root-caused the 8.6% free-tier run (2026-09-16)
Post-mortem on `--record out.json` + `--judge out.json` (29 delivered, 1 deduped): SLM predicted the **mechanically correct** severity in 17/29, but the judge credited only 4. The gap is not one thing — three defects split it, two of them harness, one judge-attributable:
1. **Deterministic harness bug (fixed):** `RCA_SCHEMA` (app/models.py) required 5 keys while `_plan_ok` (§5.3) requires 7 (`severity`, `affected_service`), and `additionalProperties: False` made the missing 2 *structurally unproduceable* → `plan_ok=False` for 100% of plans → the judge was handed `nfr2_plan_ok: false` every time → `plan_actionability`/`gate_compliance` poisoned regardless of model quality. Fixed RCA_SCHEMA per §5.3 (adds both keys), mirrored severity/affected_service into the rca prompt, unified the checker onto `PLAN_REQUIRED_KEYS`, and added guardrail `test_plan_ok_and_rca_schema_cannot_drift` (57 tests pass, smoke green).
2. **Console spans corrupted the CLI report (fixed):** `ConsoleSpanExporter` defaulted to stdout — a judge run interleaved span JSON into the machine-readable report. Now exports to **stderr**.
3. **Judge reasons were never shipped (fixed):** the judge docstring promised reasons "the report ships," but `build_report` dropped `reviews`. `reviews` (per-incident verdicts + reasons) now ride in the report output — the severity/root_cause gap becomes self-explanatory instead of a blind percentage.

Remaining unknowns (not code): the free-model daily cap (429 `free-models-per-day`, 50 calls/day no-credits) throttled judge replay before reasons could be verified; several of the 17 mechanically-correct severities were still judged wrong, which is a free-tier judge reliability question pending reasons capture on the next funded/un-throttled pass.

## Phase 4h — second live validation + judge-outage guardrail (2026-09-16)
Reran `--record` + `--judge` on the fixed build. Three results:
1. **Plan-contract fix verified in the wild:** 16/16 produced plans are now NFR-2-valid with all 7 §5.3 keys (was 0/29) — the RCA_SCHEMA drift is genuinely closed.
2. **The judge run was mostly an outage, and the report said so:** of 29 judge calls, **22 failed** (21 = `free-models-per-day` daily free-tier cap, 1 empty completion). Their all-false defaults were silently dragging `pass_rate` toward 0 — i.e. a grader outage masqueraded as a bad model. **Guardrail added:** `JUDGE_FAILURE_PREFIX` + `_judge_failed`, report now carries `judge_failures` / `graded` / `inconclusive`, `pass_rate` is computed over **graded incidents only**, and any judge outage forces `all_pass=False`. Test: `test_judge_outage_is_inconclusive_not_a_low_score` (58 tests pass, smoke green).
3. **Corrected an earlier inference (honest):** with reasons now captured, the 7 incidents that *did* grade show the judge agrees with ground truth on severity (5/7; the 2 misses are genuine model no-output/manual-review cases). The Phase-4g guess that "the judge is harsh/unreliable" was itself under-informed — the real cause was outage defaults. Real model findings that survive: `root_cause_match` 0 on the graded pair (plausible-but-wrong hypotheses, e.g. "replication lag" vs ground "secondary index rebuild"), and `gate_compliance` fails where the produced plan's `requires_approval=false` contradicts the mandatory gate (NG-1 is a system invariant, not a model-chosen field — a judge-criteria/contract ambiguity to settle next).

Blocked on the **daily free-model cap** (unrecoverable by retry — resets daily or lifts with 10 credits): a full, non-inconclusive verdict needs a funded key or a fresh day.

## Phase 5 — Feynman (2026-09-08)
**Answer incorrect, lesson + two fixes captured.** Question: live report `all_pass:true` but zero SLM tokens and all `escalated:true`. Developer answered "the model threw a silent error." Corrected: a model error is never silent (D-6 → manual_review → all_pass false), so the silence was *harness instrumentation*, not the model. Led directly to findings 4 & 5 above. Feature closed with the fixed defects and their regression tests; concept gap recorded.

## Setup
`python -m app.benchmark --smoke` (CI-safe) / `python -m app.benchmark` (live: Redis + `NEXUSOPS_OPENROUTER_API_KEY`). Dependencies as of Phase 4f: `requirements.txt` (+ `opentelemetry-sdk`/`opentelemetry-api`/`opentelemetry-exporter-otlp-proto-http` for D-7 tracing).