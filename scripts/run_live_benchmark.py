"""Live end-to-end benchmark driver.

Two-phase replay support so the classify phase and judge phase never share one
rate-limit window, and an expensive pipeline run is never lost to a flaky judge:

  Phase A (record):  python -m scripts.run_live_benchmark --record out.json
                     seeds Redis through the real webhook, runs the real worker
                     against OpenRouter, saves every incident's produced output,
                     and stops BEFORE judging.
  Phase B (judge):   python -m scripts.run_live_benchmark --judge out.json
                     grades the recorded outputs with the frontier judge — no
                     Redis, no pipeline work, infinitely replayable if 429'd.
  One pass:          python -m scripts.run_live_benchmark
                     (record + judge, for funded keys)

Requires Redis up for phase A; `.env` (loaded by app.models) carries the key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import redis
from fastapi.testclient import TestClient

from app.benchmark import _run, build_fixtures, exit_code, judge_checkpoint
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nexusops-live-benchmark")
    parser.add_argument("--record", metavar="PATH", help="phase A: pipeline only, save outputs, no judging")
    parser.add_argument("--judge", metavar="PATH", help="phase B: grade a recorded checkpoint, no Redis")
    args = parser.parse_args(argv)

    if args.judge and args.record:
        parser.error("--record and --judge are separate passes; pick one.")
    if args.judge:
        report = asyncio.run(judge_checkpoint(args.judge))
    else:
        seed()  # --record re-seeds exactly like a full live run
        report = asyncio.run(_run("live", record=args.record))

    print(json.dumps(report, indent=2))
    return exit_code(report) if not args.record else 0


if __name__ == "__main__":
    raise SystemExit(main())