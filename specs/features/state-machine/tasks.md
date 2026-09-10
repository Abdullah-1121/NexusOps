# Tasks — 3. LangGraph State Machine

Phase gates per AGENTS.md: 1 socratic-architect → 2 ponytail → 3 implementation → 4 adversarial-reviewer → 5 feynman. Task state: **todo → in-progress → done → blocked**. Update the board (`specs/tasks.md`) after each verified cycle.

| # | Task | Phases 1–5 | Verified |
|---|---|---|---|
| 3.1 | StateGraph skeleton + states/edges | 1-5 ✅ | `app/state_machine.py`; compile OK; Pattern B (thread per incident) |
| 3.2 | SLM classifier stage outputting confidence + ambiguous | 1-5 ✅ | `classify_node` + OpenRouter SLM (D-5 revised) |
| 3.3 | Escalation rule + unit tests (D-4) | 1-5 ✅ | 4-case parametrized matrix, all pass |
| 3.4 | MCP client stage calling feature-2 server | 1-5 ✅ | `app/evidence.py` real stdio MCP client; injectable for tests |
| 3.5 | RCA stage → strict-JSON plan + fail-loud schema check | 1-5 ✅ | `rca_node`; `_enforce_json` strict (no fence tolerance) |
| 3.6 | Gate + MemorySaver checkpointer + decision resume (§5.4) | 1-5 ✅ | `interrupt()` + `Command(resume=...)`; approve→rollback, reject→skip, invalid→ValueError |
| 3.7 | `manual_review` path test (AI-down fixture) | 1-5 ✅ | `Boom()` injectable classify/rca → terminal manual_review |
| 3.8 | Update board (`specs/tasks.md`) | ✅ | see board |

**Marked upgrade (not built — ponytail):** Redis queue consumer (BRPOP → per-incident graph). The graph is fully driven by tests; the queue-consumer is exercised by the benchmark (Feature 5) which is its first real pipeline consumer.

## Phase 1 decision (2026-09-08)
**APPROVED — Pattern B:** thread-per-incident via checkpointer; gate = `interrupt()`; resume = `Command`. See `spec.md`.

## D-5 revision (2026-09-08, user decision)
SLM via **OpenRouter** (open-source models over OpenAI-compatible API), **not Ollama**. Env: `NEXUSOPS_OPENROUTER_API_KEY`, `NEXUSOPS_SLM_MODEL`, `NEXUSOPS_FRONTIER_MODEL`. Spec + design updated.

## Phase 4 findings (2026-09-08)
1. **Gate decision unvalidated → fixed:** any non-"approve"/"reject" resume value was silently routed as reject (NFR-4 smell at the human gate). Now raises `ValueError` (fail loud). Guarded by `test_invalid_gate_decision_fails_loud`.
2. **Strict-JSON softened → fixed:** `_enforce_json` tolerated code-fenced output; deleted — OpenRouter `json_schema strict:true` contract means any non-JSON is a loud `ModelError` (FR-4/NFR-2 are maximalist here on purpose).

## Phase 5 — Feynman (2026-09-08)
**Partial pass, recorded honestly.** Question: MemorySaver placement, resume selector, and D-3 consistency. Developer got right: state content checkpointed before interrupt, crash-during-wait loses the incident. Missed: (1) MemorySaver lives in RAM (not Redis), (2) resume locator is `thread_id`, not the saver, (3) called D-3's deliberate trade a contradiction. Corrected in-session with full explanation (pause-vs-crash distinction; RAM for waits, Redis for crash-survival). Re-check declined — developer asked to close. Gap recorded; concept is the interview question for any successor session.

## Context7 stamps
- `VERIFIED langgraph 1.2.11 (Python) — StateGraph, MemorySaver, interrupt(), Command(resume=...), thread_id` — actual installed surface checked.
- `VERIFIED OpenRouter (docs) — POST /api/v1/chat/completions`, Bearer auth, `response_format json_schema strict`, `choices[0].message.content`.
- `VERIFIED httpx (async client.post(json=...), raise_for_status, timeout)`.

## Setup
`mcp`, `langgraph`, `httpx` added to requirements. Tests drive graph with injected fakes (no API key needed). Live call requires `NEXUSOPS_OPENROUTER_API_KEY`.