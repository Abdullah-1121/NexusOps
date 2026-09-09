# Feature Spec — 5. Benchmark & Regression Harness

System context: `specs/requirements.md` FR-8, §6 (B-1..B-4), NFR-1/NFR-2/NFR-5.

## What this feature does (in scope)
- **30 synthetic incidents** (§6 B-1): critical (rollback-worthy), warning, info, malformed-log-source, ambiguous-severity (escalation path), duplicate delivery.
- DeepEval rubric scoring (§6 B-2): severity match, root-cause match, plan actionability, gate compliance.
- **Report** (§6 B-3, machine-readable): pass rate, P95 latency (SLM-only path per NFR-1), token cost per model, MTTR, tool-call inventory.
- **Exit non-zero** on any rubric failure or SLA breach (FR-8 acceptance).
- **Deterministic**: fixed mock MCP data, seeded randomness (NFR-5).

## Out of scope
- Live/production evals.
- The mock tool backends (owned by **mcp-server** feature).

## Acceptance
- `benchmark` command runs locally (Redis + Ollama running); emits the report.
- Any rubric failure or SLA breach fails the run (non-zero exit).