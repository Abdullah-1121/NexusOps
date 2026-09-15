"""Live end-to-end benchmark driver.

Seeds Redis through the real Feature-1 webhook (dedupe + queue), then runs the
real worker against OpenRouter (SLM + frontier + LLM-judge). Requires Redis to be up
and NEXUSOPS_OPENROUTER_API_KEY set (`.env` is loaded by app.models).

Usage:  python -m scripts.run_live_benchmark
"""

from __future__ import annotations

import asyncio
import os

import redis
from fastapi.testclient import TestClient

from app.benchmark import _run, build_fixtures, exit_code
from app.ingest import DEDUPE_KEY, QUEUE_KEY, app as ingest_app

REDIS_URL = os.environ.get("NEXUSOPS_REDIS_URL", "redis://localhost:6379/0")


def seed() -> None:
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.delete(QUEUE_KEY, DEDUPE_KEY)

    with TestClient(ingest_app) as api:
        dup = build_fixtures()[-1]
        assert dup.alert["incident_id"] == "dup-01" and not dup.delivered

        # Simulate a pre-epoch delivery so the replayed webhook is a true dup.
        first = api.post("/webhook/incident", json=dup.alert)
        assert first.status_code == 202, first.text
        assert first.json()["duplicate"] is False
        replay = api.post("/webhook/incident", json=dup.alert)
        assert replay.json()["duplicate"] is True  # seen-set is real

        dup_raw = next(
            v for v in client.lrange(QUEUE_KEY, 0, -1) if '"dup-01"' in v
        )
        client.lrem(QUEUE_KEY, 0, dup_raw)  # pre-epoch entry -> out of this epoch

        for f in build_fixtures():
            if f.delivered:
                resp = api.post("/webhook/incident", json=f.alert)
                assert resp.status_code == 202, resp.text
    assert client.llen(QUEUE_KEY) == 29, client.llen(QUEUE_KEY)


def main() -> int:
    seed()
    report = asyncio.run(_run("live"))
    import json

    print(json.dumps(report, indent=2))
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())