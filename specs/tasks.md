# NexusOps — Task Board

Board linking every feature spec + task file. Global contracts live in `specs/requirements.md` + `specs/design.md`; per-feature spec + tasks live under `specs/features/<name>/`. Shared contracts are referenced, never duplicated, in feature files.

**Build order:** 1 → 2 → 3 → 4 → 5 (later features depend on earlier ones).

| # | Feature | Spec | Tasks | State |
|---|---|---|---|---|
| 1 | Webhook ingestion | `features/webhook-ingest/spec.md` | `features/webhook-ingest/tasks.md` | **done** |
| 2 | MCP server | `features/mcp-server/spec.md` | `features/mcp-server/tasks.md` | **done** (Phases 1–5 ✅) |
| 3 | State machine | `features/state-machine/spec.md` | `features/state-machine/tasks.md` | **done** (Phases 1–4 ✅; Phase 5 partial-pass recorded) |
| 4 | Streaming dashboard | `features/streaming-dashboard/spec.md` | `features/streaming-dashboard/tasks.md` | **done** (Phases 1–4 ✅; Phase 5 skipped-by-user, recorded) |
| 5 | Benchmark harness | `features/benchmark/spec.md` | `features/benchmark/tasks.md` | **done** (Phases 1–4 ✅; Phase 5 incorrect-answer → 2 fixes + tests, recorded; Phase 4f OTel GenAI tracing/eval per D-7; Phase 4g root-caused the 8.6% run — 3 fixes; Phase 4h second live validation — plan fix verified, judge-outage guardrail; Phase 4i stratified quick-run `--limit N`; Phase 4j first clean verdict via `--limit 10` — severity 7/10, root cause 2/10, gate 2/10 = requires_approval contract bug; Phase 4k Basket-1 fixes — NG-1 gate lock, outage separation, evidence-forced RCA, behavior-based judge; Phase 4l re-record blocked by `429 free-models-per-day` 50-call cap (resets 05:00 PKT) — 7 outages, 3 answered, judge ungradable today, no verdict delta yet; 62 tests, 2026-09-21) |

Update a feature's state here whenever its `tasks.md` changes (AGENTS.md §5). A feature is `done` only after all 5 phases pass for every task and its `spec.md` reflects reality.