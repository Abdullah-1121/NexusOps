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
    import asyncio
    import os

    from app.benchmark import paced_stage, smoke_classify, smoke_gather, smoke_rca
    from app.state_machine import build_graph

    # Watchable rehearsal (2026-09-28, user-found): the smoke pipeline completes
    # in <100 ms — every stage arrives in the same frame, so a live fire reads as
    # "predefined". We pace ONLY the console's rehearsal fakes here (benchmark.py
    # and tests stay instant), so an incident visibly RUNS: stages arrive one at
    # a time, and the honest `stage_duration_ms` stamp now measures the paced
    # stream's real wall-clock (classify ~0.5 s, evidence ~0.9 s, rca ~1.4 s,
    # rollback ~0.5 s). The data is still deterministic smoke — the SMOKE header
    # chip says so — only the delivery is watchable. `NEXUSOPS_SMOKE_PACE=0`
    # restores instant mode; any multiplier scales the whole run.
    pace = float(os.environ.get("NEXUSOPS_SMOKE_PACE", "1.0"))

    async def fake_rollback(sha: str) -> dict:
        await asyncio.sleep(0.5 * pace)
        return {"ref": f"refs/tags/rollback-{sha[:10]}", "tag": f"rollback-{sha[:10]}"}

    def make_graph():
        return build_graph(
            classify=paced_stage(0.5, pace)(smoke_classify),
            gather=paced_stage(0.9, pace)(smoke_gather),
            rca=paced_stage(1.4, pace)(smoke_rca),
            rollback_tool=fake_rollback,
        )

    return make_graph


def _real_make_graph(model_fn=None, gather=None, retries: int = 2, base_sleep: float = 1.0):
    """Real-mode console graph (2026-09-28). Production wiring identical to
    `build_graph` defaults, but the served pipeline retries TRANSIENT provider
    errors (429/5xx) with the benchmark's existing, tested `_retry_model` —
    otherwise a momentary Gemini free-tier "high demand" 503 turns an entire
    live incident into `manual_review` before the operator even sees it
    classify. Hard errors (bad key, broken contract, quota exhaustion) still
    surface immediately and honestly. Confined to the console, like the smoke
    pacing: benchmark harness and tests keep their own wiring."""
    from app.benchmark import _retry_model
    from app.evidence import gather_evidence
    from app.state_machine import build_graph, _openrouter

    fn = model_fn or _openrouter
    retry = lambda f: _retry_model(f, retries=retries, base_sleep=base_sleep)
    gatherer = gather or gather_evidence

    def make_graph():
        return build_graph(classify=retry(fn), rca=retry(fn), gather=gatherer)

    return make_graph


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true",
                        help="use deterministic smoke fakes (zero quota)")
    parser.add_argument("--slm-rca", action="store_true",
                        help="write remediation plans with the SLM (all runs on "
                             "flash-lite — faster, cheaper, immune to free-tier "
                             "frontier 503 flapping; shallower root-cause prose)")
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument("--keep-redis", action="store_true",
                        help="do not flush demo queue + seen-set at startup")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    _flush_demo_redis(args.keep_redis)

    import os

    if args.smoke:
        # B1: the operator header must say which runtime is honest — smoke fakes
        # are NOT real model output, and the console labels it as such.
        os.environ["NEXUSOPS_MODE"] = "smoke"
    if args.slm_rca:
        # T-7.11: read by BOTH the rca graph node (state_machine) and the plan
        # stamp (serve._inspect) — one env, both sides, stamps always honest.
        os.environ["NEXUSOPS_RCA_MODEL"] = "slm"

    from app.serve import create_serve_app

    make_graph = _smoke_make_graph() if args.smoke else _real_make_graph()
    app = create_serve_app(make_graph=make_graph)

    import uvicorn

    mode = "SMOKE (zero quota)" if args.smoke else "REAL (Gemini, quota)"
    rca = " — RCA on SLM (--slm-rca)" if args.slm_rca else ""
    print(f"NexusOps console on http://127.0.0.1:{args.port} — mode: {mode}{rca}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()