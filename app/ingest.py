"""NexusOps — Feature 1: Webhook ingestion (validate -> Redis queue -> 202).

Pattern B (approved, Phase 1): validate + dedupe + enqueue immediately, return
202; a worker picks items up later (Feature 3 owns the consuming side).

Dedupe + enqueue are ONE atomic Lua script: SADD then LPUSH cannot be
interrupted between steps, so a crash can never put an alert in the "seen" set
without enqueueing it (that would silently swallow alerts — NFR-4).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("nexusops.ingest")

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
QUEUE_KEY = "nexusops:incidents:queue"
# ponytail: unbounded SET — every seen incident_id lives forever. Fine for demo,
# add ZSET-by-timestamp + retention when the incident universe grows unbounded.
DEDUPE_KEY = "nexusops:incidents:seen"

# Atomic dedupe + enqueue. SADD returns 1 if newly added, 0 if already seen.
# LPUSH happens only for new alerts; the whole script runs without yielding.
DEDUPE_AND_ENQUEUE = """
local added = redis.call('SADD', KEYS[1], ARGV[1])
if added == 1 then
    redis.call('LPUSH', KEYS[2], ARGV[2])
end
return added
"""


class IncidentAlert(BaseModel):
    """Contract requirements.md §5.1 — strict JSON, unknown fields rejected."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    occurred_at: str
    source: str
    service: str
    message: str
    status_code: int = Field(default=0, ge=0)
    severity_hint: Literal["critical", "warning", "info", None] = None
    error_type: str | None = None
    stack_trace: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Read from env at startup so tests can point at a dead port for the 503 test.
    redis_client = aioredis.from_url(
        os.environ.get("NEXUSOPS_REDIS_URL", DEFAULT_REDIS_URL),
        decode_responses=True,
    )
    app.state.redis = redis_client
    app.state.dedupe_script = redis_client.register_script(DEDUPE_AND_ENQUEUE)
    yield
    await redis_client.aclose()


app = FastAPI(title="NexusOps", lifespan=lifespan)


@app.post("/webhook/incident", status_code=202)
async def ingest_incident(alert: IncidentAlert):
    try:
        added = await app.state.dedupe_script(
            keys=[DEDUPE_KEY, QUEUE_KEY],
            args=[alert.incident_id, alert.model_dump_json()],
        )
    except aioredis.RedisError:
        # NFR-6: refuse loudly, and 503 (not 500) so the sender treats it as
        # "retry later", not "your payload is broken". Catching the base class
        # covers connect-failure, operation timeouts, and Redis-side errors.
        logger.exception("webhook rejected: redis unavailable")
        return JSONResponse(status_code=503, content={"error": "redis unavailable"})
    return {"incident_id": alert.incident_id, "duplicate": added != 1}