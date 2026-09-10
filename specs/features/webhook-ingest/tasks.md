# Tasks — 1. Webhook Ingestion

Phase gates per AGENTS.md: 1 socratic-architect → 2 ponytail → 3 implementation → 4 adversarial-reviewer → 5 feynman. Task state: **todo → in-progress → done → blocked**. Update the board (`specs/tasks.md`) after each verified cycle.

| # | Task | Phases 1–5 | Verified |
|---|---|---|---|
| 1.1 | FastAPI route + Pydantic v2 alert model (`extra="forbid"`) | 1-5 ✅ | 5 tests green (`tests/test_ingest.py`) |
| 1.2 | Redis client + atomic SADD→LPUSH dedupe script | 1-5 ✅ | Lua `DEDUPE_AND_ENQUEUE`; atomicity verified by duplicate test |
| 1.3 | 202 / 422 / 503 behavior | 1-5 ✅ | 5 tests green incl. 503 via dead-port env |
| 1.4 | Duplicate-id test + seen-set durability | 1-5 ✅ | set lives in Redis → survives restart by construction (tested via dedicated test DB) |
| 1.5 | Update board (`specs/tasks.md`) | ✅ | see board |

## Phase 4 findings (2026-09-08)
1. **Catch too narrow → fixed:** `except ConnectionError` only covered connect-time failure. Timeouts/Redis-side errors would surface as 500, which tells the alert sender "your payload is broken" (drop) instead of "retry later". Now `except RedisError` → 503 + exception log.
2. **Unbounded dedupe SET → flagged (ponytail:), not fixed:** every seen `incident_id` lives forever in Redis. Fine for demo; upgrade = ZSET keyed by timestamp + retention window when the incident universe grows.

## Phase 5 note (2026-09-08)
Passed with gap: atomicity + crash-window concept solid. CTO-defense ("dedupe at door vs worker") answered shallowly, closed by mentor: worker memory is process-private and dies on restart; door uses shared Redis. Gap recorded, not elapsed. Moving on explicitly per user.
- `VERIFIED FastAPI 0.115+ via Context7 — lifespan asynccontextmanager, Pydantic body → auto-422`
- `VERIFIED redis-py 8.x via Context7 — redis.asyncio, register_script/AsyncScript, RedisError`
- `VERIFIED Pydantic v2 via Context7 — ConfigDict(extra='forbid') → extra_forbidden`

## Setup commands (for future sessions)
```bash
brew install redis python@3.12
/usr/local/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
brew services start redis   # or: redis-server --daemonize yes
.venv/bin/python -m pytest tests/ -v   # requires Redis up
.venv/bin/uvicorn app.ingest:app --reload   # run the API
```
Env: `NEXUSOPS_REDIS_URL` (default `redis://localhost:6379/0`); tests use `redis://localhost:6379/15` and flush per test.