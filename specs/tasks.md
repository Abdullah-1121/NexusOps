# NexusOps — Task Board

Board linking every feature spec + task file. Global contracts live in `specs/requirements.md` + `specs/design.md`; per-feature spec + tasks live under `specs/features/<name>/`. Shared contracts are referenced, never duplicated, in feature files.

**Build order:** 1 → 2 → 3 → 4 → 5 (later features depend on earlier ones).

| # | Feature | Spec | Tasks | State |
|---|---|---|---|---|
| 1 | Webhook ingestion | `features/webhook-ingest/spec.md` | `features/webhook-ingest/tasks.md` | **done** |
| 2 | MCP server | `features/mcp-server/spec.md` | `features/mcp-server/tasks.md` | **done** (Phases 1–5 ✅) |
| 3 | State machine | `features/state-machine/spec.md` | `features/state-machine/tasks.md` | **done** (Phases 1–4 ✅; Phase 5 partial-pass recorded) |
| 4 | Streaming dashboard | `features/streaming-dashboard/spec.md` | `features/streaming-dashboard/tasks.md` | **done** (Phases 1–4 ✅; Phase 5 skipped-by-user, recorded) |
| 5 | Benchmark harness | `features/benchmark/spec.md` | `features/benchmark/tasks.md` | todo |

Update a feature's state here whenever its `tasks.md` changes (AGENTS.md §5). A feature is `done` only after all 5 phases pass for every task and its `spec.md` reflects reality.