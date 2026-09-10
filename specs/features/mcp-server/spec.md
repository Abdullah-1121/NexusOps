# Feature Spec — 2. MCP Server (mock backends)

System context: `specs/requirements.md` §5.2 (tool contracts), FR-2; `specs/design.md` §3 (MCP stage), NG-2 (real protocol, mock backends).

## Phase 1 decision — **APPROVED** (2026-09-08)
**Pattern A: official `mcp` Python SDK (`FastMCP`), stdio transport.** Compared vs B (hand-rolled JSON-RPC/stdio) and C (plain LangGraph `@tool`, no protocol). C rejected by contract (NG-2, §8 "real MCP server"); B rejected (owning spec version negotiation forever = 3am debt). Motivating considerations: vendor-managed handshake/version negotiation, typed structured errors out of the box (needed for §5.2 "no data" vs "doesn't exist"), and the MCP boundary keeps the mock→real backend swap viable (design §6 NG-2). Stdio because the state machine spawns the server as a local subprocess — no network/auth surface. Risk recorded: FastMCP API verified via Context7 before any code (AGENTS.md §2).

## What this feature does (in scope)
- Standalone MCP server exposing exactly three tools:
  - `fetch_service_logs(service_name, timestamp_window)`
  - `query_prometheus_metrics(metric_name, duration)`
  - `trigger_github_rollback(commit_sha)` — **gated**: returns `rejected` unless the incident is gate-approved; after approval returns `performed`.
- Strict error semantics (§5.2): "no data in window" (empty result) vs "service does not exist" (structured error). Never silent.
- Every call is trace-visible (FR-2 acceptance).

## Out of scope
- Real backends/APIs (NG-2).
- Deciding *when* to call tools (owned by **state-machine** feature).

## Acceptance
- Each tool callable with §5.2-conformant input → §5.2-conformant output.
- Unknown service → structured `{error}`, not a silent empty.
- Rollback before approval → `rejected`. After approval → `performed`.