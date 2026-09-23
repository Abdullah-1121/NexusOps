"""Run the live operations console (Feature 7, D-11).

  python -m scripts.run_console             # real models (needs .env, quota)
  python -m scripts.run_console --smoke     # deterministic smoke fakes — zero
                                            # quota, CI/demo-safe fallback

One FastAPI process on one port (default 8137) serves everything: the React
console (built frontend or Vite proxy target), /webhook, /ws, /api, and the
feature-6 film at /film. Redis must be up (NFR-6).
"""

from __future__ import annotations

import argparse
import logging

from app.ingest import DEDUPE_KEY, DEFAULT_REDIS_URL, QUEUE_KEY


def _flush_demo_redis(keep: bool) -> None:
    """Postmortem 2026-09-23 guardrail: the console shares Redis db 0 with
    benchmark/retry sessions, whose leftover seen-set entries silently made
    every fixture fire a duplicate. Default = flush the two demo keys at
    startup so each console run is a deterministic sandbox. `--keep-redis`
    opts out for anyone actively debugging queue state."""
    if keep:
        return
    import os

    import redis as redis_sync

    url = os.environ.get("NEXUSOPS_REDIS_URL", DEFAULT_REDIS_URL)
    r = redis_sync.from_url(url)
    try:
        r.delete(QUEUE_KEY, DEDUPE_KEY)
    finally:
        r.close()
    print("console sandbox: flushed queue + seen-set (dedupe reset)")


def _smoke_make_graph():
    from app.benchmark import smoke_classify, smoke_gather, smoke_rca
    from app.state_machine import build_graph

    async def fake_rollback(sha: str) -> dict:
        return {"ref": f"refs/tags/rollback-{sha[:10]}", "tag": f"rollback-{sha[:10]}"}

    def make_graph():
        return build_graph(
            classify=smoke_classify,
            gather=smoke_gather,
            rca=smoke_rca,
            rollback_tool=fake_rollback,
        )

    return make_graph


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true",
                        help="use deterministic smoke fakes (zero quota)")
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument("--keep-redis", action="store_true",
                        help="do not flush demo queue + seen-set at startup")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    _flush_demo_redis(args.keep_redis)

    from app.serve import create_serve_app

    make_graph = _smoke_make_graph() if args.smoke else None
    app = create_serve_app(make_graph=make_graph) if make_graph else create_serve_app()

    import uvicorn

    mode = "SMOKE (zero quota)" if args.smoke else "REAL (Gemini, quota)"
    print(f"NexusOps console on http://127.0.0.1:{args.port} — mode: {mode}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()