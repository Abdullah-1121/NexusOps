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

## Phase 4i — stratified quick-run mode (`--limit N`) (2026-09-21)
Free-tier reality: a full 29-incident run needs ~90 calls/day (classify + frontier + judge) but the free cap is ~50/day, so the judge phase always 429s — that, not model quality, dominated the last two verdicts. Added `--limit N` to the live driver: `select_stratified()` picks a deterministic round-robin slice across the 5 fixture categories (critical/warning/info/ambiguous/malformed → 2/2/2/2/2 for N=10), `_run(until=N)` consumes exactly N, `seed(limit)` seeds only those through the real webhook. Judge grades whatever the checkpoint holds (no judge flag). A 10-incident run costs ~25 calls and can complete non-inconclusive on a free day. Guarded by `test_select_stratified_covers_every_category` (determinism, every-category coverage, no dup, oversized-n edge). Rationale: a naive `fixtures[:N]` would sample only criticals — same cost, dishonest signal; stratification keeps the sample representative within the budget. (Spec for feature 5, Phase 4j: results of the first limited run.)

## Phase 4j — first clean (non-inconclusive) verdict via stratified quick run (2026-09-21)
Ran `--limit 10` record + judge (2/2/2/2/2 across critical/warning/info/ambiguous/malformed). **`judge_failures: 0`, `inconclusive: False`** — the limited-run strategy beat the daily cap outright (all 10 judge calls graded; previously 22/29 failed on 429). `pass_rate 0.35` (14/40 metric slots).
- **severity 7/10** — and the breakdown says the model is better than the raw number: 2 misses are null output because the SLM itself was down (`c-01`, `a-01` → honest manual_review, correctly failed by judge); only **1 real classification miss** (`a-02`: ground critical, produced warning). Among incidents that produced an answer, severity was 7/8 (88%).
- **root_cause 2/10** — the genuine model weakness, now confirmed on a second sample: hypotheses are plausible-sounding but wrong (e.g. "routine maintenance" vs ground; "cache miss spike" vs ground). Not an outage artifact.
- **gate_compliance 2/10 — CONTRACT BUG, not model weakness (hypothesis from Phase 4h now proven).** The judge compares the plan's **model-chosen** `requires_approval` to ground truth `requires_gate=true`. NG-1 makes the human gate a **system invariant for every incident**, yet the plan schema lets the model write `requires_approval: false`; the judge then counts it a gate failure (5 of the misses are exactly this). Fix decided: lock `requires_approval=true` in the plan contract — a plan that claims a gate exemption contradicts the DNA; the field becomes system-enforced, not model-chosen.
- NFR-1 P95 505 s (~8.4 min) — free-tier SLM latency, mechanical not architectural; funded key only.
- Traces end-to-end inspected in Jaeger during this run: `llm.judge` → `llm.chat.completions` per incident (evaluation-as-trace, D-7) — 5 visible mid-run, full 10 after.

## Phase 4k — Basket-1 fixes: gate invariant, outage separation, evidence-forced RCA, behavior-based judge (2026-09-21)
Four free-space improvements over Phase 4j's clean verdict, all implemented + guardrailed:
1. **NG-1 gate lock.** `requires_approval` is a system invariant, not a model choice: `make_rca_node` forces it `True` at the single choke point where plans enter state, and `_plan_ok` rejects any plan claiming otherwise (`test_plan_ok_rejects_no_approval_plan`, `test_rca_node_locks_gate_invariant`). Attacked the 5/8 gate misses proven in Phase 4j.
2. **Outage separation in the report.** A `manual_review` is a model/tool OUTAGE (D-6), not a wrong answer. Report now ships `outages`/`outage_count`/`answered` plus `metric_pass_counts_answered` (skill over incidents the model actually answered), alongside the all-inclusive counts (`test_report_separates_outages_from_wrong_answers`).
3. **Evidence-forced RCA prompt.** The plan prompt now demands the hypothesis be consistent with each numbered evidence item, and each remediation step name the evidence that justifies it — attacking the root-cause guessing weakness directly.
4. **Behavior-based gate judgment.** The judge grades gate_compliance by BEHAVIOR (human in loop / nothing auto-applied → compliant, incl. manual_review), not by the plan's `requires_approval` text.
Bug caught in tests during implementation: rewriting the RCA prompt's `Evidence:` marker to `Evidence (numbered):` broke `smoke_rca`'s parse contract (IndexError → correctly routed to manual_review by D-6) — marker preserved, numbering moved inside the body. Net: 62 tests pass, smoke green. Phase 5 (2026-09-21): re-record + re-judge the same 10 fixtures to measure the delta — numbers in Phase 4l.

## Phase 4l — Re-record blocked by free-tier cap, not code (2026-09-21)
Re-recorded `--limit 10` with the Phase 4k fixes: 10 delivered, **7 manual_review (outages), 3 answered** (c-01, a-01, m-01) — the manual reviews are NOT Pipeline misses; they are `429 free-models-per-day` (OpenRouter free tier, 50 calls/day) + upstream Nvidia `503 provider_overloaded`, both hitting mid-run. Judge (frontier, also free) returned `judge_failures: 10` — verdicts impossible today. Verified via `X-RateLimit-Reset`: 50/day cap resets **05:00 PKT daily** (not hourly); the 429 literally offers "Add 10 credits to unlock 1000 free model requests per day." Guardrail work: `-u` added to record invocation so progress is visible live; one scheduled retry died silently after 2 calls (no error, empty report) — the fresh run then completed. **No verdict delta measured yet**: the new-pipeline records exist in `out10b.json` (gitignored) and only need a judge run once any quota (reset or funded) exists. DoD for this phase: re-run judge on `out10b.json`, record Phase 4m numbers.

## Phase 4m — first live Gemini run: record succeeded, judge quota-burned (2026-09-22)
Provider switch per D-9 (config-only): SLM `gemini-3.5-flash-lite` / frontier `gemini-3.7-flash`→`3.8-flash`, key `NEXUSOPS_LLM_API_KEY` = Gemini `AQ.*`, base URL `generativelanguage.googleapis.com/v1beta/openai` (OpenAI-compat surface, strict json_schema verified live incl. full RCA schema + usage tokens).
- **Record phase (the big win):** `--limit 10` on Gemini → **8/10 answered, 2 outages, 18 model calls (16 SLM + 2 frontier)**, all plans `plan_ok=True`, decisions approve/reject clean, 10/10 evidence, 1 rollback executed, m-02 correctly rejected. Best record result ever (vs 3/10 best on OpenRouter/Nvidia free). Budget math held exactly as D-9 predicted. The 2 outages were frontier `503 high demand` on the escalated-RCA path (c-01, a-01) — honestly reported, not averaged.
- **Judge phase (the lesson):** first pass on `gemini-3.7-flash` → 8/10 judge calls failed, then `429 exceeded your current quota` — the 3-retry wrapper had multiplied the burn on the 20/day quota until the day was gone. Re-judged on `gemini-3.8-flash` (own 20/day quota): 3 graded, 7 failures (503 + 429 again). **Root cause: retry policy vs tiny quota** — 3 tries × ~10 incidents on a congested day exceeds 20/day; a quota is terminal, not transient. Fixed and guardrailed (D-9): `_quota_exhausted()` non-retryable classification (4 tests), in-process retries capped at 2, `scripts/retry_judge.sh` probe-guarded replay.
- **Real quality signal in the graded set (2 + 3 incidents):** gate compliance held (2/2 then 2/3 answered), plans 2/2, severity 2/3, **root cause 0/3** — the model-IQ ceiling persists on Gemini too, as Phase 4l predicted. Not an outage artifact.
- **Clean full-run verdict still blocked:** frontier quota (both flash models) exhausted today by the earlier retry burn; resets midnight Pacific. `outG1.json` + `judgeG1_report.json` saved; `scripts/retry_judge.sh` will finish the 8-incident verdict on the next quota window with ~11 frontier calls (see D-9). DoD for closing: `bash scripts/retry_judge.sh outG1.json` → `judge_failures: 0`, `inconclusive: false`, record final Phase 4m table.
- 67 tests pass (63 + 4 `_quota_exhausted` guardrails), smoke + golden green.

## Phase 5 — Feynman (2026-09-08)
**Answer incorrect, lesson + two fixes captured.** Question: live report `all_pass:true` but zero SLM tokens and all `escalated:true`. Developer answered "the model threw a silent error." Corrected: a model error is never silent (D-6 → manual_review → all_pass false), so the silence was *harness instrumentation*, not the model. Led directly to findings 4 & 5 above. Feature closed with the fixed defects and their regression tests; concept gap recorded.

## Setup
`python -m app.benchmark --smoke` (CI-safe) / `python -m app.benchmark` (live: Redis + `NEXUSOPS_OPENROUTER_API_KEY`). Dependencies as of Phase 4f: `requirements.txt` (+ `opentelemetry-sdk`/`opentelemetry-api`/`opentelemetry-exporter-otlp-proto-http` for D-7 tracing).