# Feature Spec — 2. MCP Server (mock backends)

System context: `specs/requirements.md` §5.2 (tool contracts), FR-2; `specs/design.md` §3 (MCP stage), NG-2 (real protocol, mock backends).

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