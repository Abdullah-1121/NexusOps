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

## Phase 5 — Feynman (2026-09-08)
**Answer incorrect, lesson + two fixes captured.** Question: live report `all_pass:true` but zero SLM tokens and all `escalated:true`. Developer answered "the model threw a silent error." Corrected: a model error is never silent (D-6 → manual_review → all_pass false), so the silence was *harness instrumentation*, not the model. Led directly to findings 4 & 5 above. Feature closed with the fixed defects and their regression tests; concept gap recorded.

## Setup
`python -m app.benchmark --smoke` (CI-safe) / `python -m app.benchmark` (live: Redis + `NEXUSOPS_OPENROUTER_API_KEY`). No new dependency added to `requirements.txt`.