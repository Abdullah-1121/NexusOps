# Tasks — 2. MCP Server

Phase gates per AGENTS.md: 1 socratic-architect → 2 ponytail → 3 implementation → 4 adversarial-reviewer → 5 feynman. Task state: **todo → in-progress → done → blocked**. Update the board (`specs/tasks.md`) after each verified cycle.

| # | Task | Phases 1–5 | Verified |
|---|---|---|---|
| 2.1 | MCP server skeleton (stdlib reference server) | 1-5 ✅ | Pattern A (official SDK, stdio) approved; `MCPServer` + `@mcp.tool` |
| 2.2 | `fetch_service_logs` + `query_prometheus_metrics` handlers + deterministic mock data | 1-5 ✅ | 9 tests green (`tests/test_mcp.py`) incl. order-independence |
| 2.3 | Error semantics: empty-window vs unknown-service | 1-5 ✅ | `ToolError` vs empty `{"logs": []}`; `test_empty_vs_unknown_are_distinguishable` |
| 2.4 | `trigger_github_rollback` gate enforcement + test | 1-5 ✅ | rejected → `performed` after `approve_rollback()`; gate test green |
| 2.5 | Update board (`specs/tasks.md`) + live protocol smoke test | ✅ | `scripts/smoke_mcp.py`: real client handshake, list_tools, call_tool over stdio |

## Phase 1 decision (2026-09-08)
**APPROVED — Pattern A:** official `mcp` Python SDK (`MCPServer`), stdio transport. See `spec.md`.

## Phase 2 (ponytail) notes
- No tool base class / factory / registry — three `@mcp.tool()` functions.
- No config module — mock fixtures are module constants; deterministic RNG per request.
- Stdio chosen over env-driven multi-transport (one spawn path).

## Phase 3 context
`approve_rollback(commit_sha)` is a **non-MCP** internal hook. The MCP surface stays exactly 3 tools (FR-2); the state-machine feature (3) calls `approve_rollback()` after a §5.4 human decision, then the tool call itself enforces the gate.

## Phase 4 findings (2026-09-08)
1. **Fixture coupling bug → fixed:** empty-window rule keyed on `start.date() == _now().date()` was falsy for any window spanning yesterday; test caught it. Rule now pure window/timestamp logic.
2. **NFR-5 determinism violation → fixed:** module RNG was shared across tools, so payloads depended on *call order*. Now `random.Random(f"{service}:{start}")` per request — same request ⇒ same payload regardless of interleaving. Guarded by `test_mock_data_is_order_independent_and_repeatable`.

## Phase 5 — Feynman passed (2026-09-08)
Question: why keep "empty window" (`{"logs": []}`) distinct from "service doesn't exist" (`ToolError`/`is_error=True`)? Developer answered both halves: named the false belief (agent believes a nonexistent service is a real, healthy, quiet service) and the consequence (empty = "all clear, close it" vs error = trip-wire → `manual_review`, D-6). Final refinement recorded: the loud channel doesn't give the agent a better opinion — it makes concluding-from-broken-evidence structurally impossible, not just discouraged.

## Context7 stamps
- `VERIFIED mcp SDK (Model Context Protocol python-sdk, rev v1.12.4 / installed mcp 2.2.0) — MCPServer(name) + @mcp.tool() + ToolError (anticipated failure ⇒ is_error=True result) + mcp.run(transport="stdio" default)`. Verified against installed package API before code.

## Setup
`mcp` (pypi) added to `requirements.txt`. NOTE: install builds `cryptography` from source on first run (slow, ~2 min) — not an error. Live check: `.venv/bin/python scripts/smoke_mcp.py`.