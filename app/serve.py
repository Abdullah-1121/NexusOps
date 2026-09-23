"""NexusOps — Feature 7 (D-11): serve mode — one process that IS the live system.

The thesis demo becomes a real running machine: this FastAPI app hosts the
ingest webhook, the background pipeline worker (real models), the WebSocket
stream, the fixture catalog + status APIs, the feature-6 film (history tab),
and the React build when present. Incidents process sequentially — one complete
cycle at a time (the demo ask); a parked gate blocks later work by design, and
D-3 already accepts losing it on restart.

Two pieces the dashboard-era system never had, built here:

1. The pipeline's VOICE (producer side of the FR-6 EventBus, wired 2026-09-23):
   `LiveDriver` runs the real graph via `astream(stream_mode="updates")` and
   maps each node chunk onto the stage vocabulary (classified, escalating,
   tool_call, plan, gate_open, rollback, done, manual_review) — zero changes
   to `state_machine.py` (D-11, empirically probed on LangGraph 1.2.11).

2. The operator's WHEEL: `GateAwaiter` is an asyncio "waiting room". The driver
   publishes gate_open and awaits `wait(iid)`; the §5.4 socket handler (the
   same validation as `app/dashboard._route_decision`) resolves the future. The
   driver remains the single writer on the incident's checkpoint — it alone
   resumes with `Command(resume=...)` — so no two tasks can race a resume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command

from app.benchmark import build_fixtures
from app.bus import EventBus
from app.dashboard import _route_decision
from app.ingest import (
    DEDUPE_AND_ENQUEUE,
    DEDUPE_KEY,
    DEFAULT_REDIS_URL,
    QUEUE_KEY,
    IncidentAlert,
    enqueue_alert,
)
from app.player import create_player_app
from app.state_machine import build_graph
from app.tracing import get_tracer

logger = logging.getLogger("nexusops.serve")

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
CHECKPOINT_DEFAULT = Path(__file__).resolve().parent.parent / "outG1.json"


class GateAwaiter:
    """asyncio 'waiting room' for parked gates (D-11).

    The driver awaits `wait(iid)` after streaming `gate_open`; the §5.4 handler
    resolves it with the validated decision body. First decision wins; a second
    or an unknown incident resolves to False so the caller surfaces a loud
    `already_decided` (requirements §5.4) instead of a silent ignore.
    """

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}

    def reset(self, incident_id: str) -> None:
        """Clear the previous run's gate so THIS run parks at a fresh one
        (postmortem 2026-09-23). Without it, a forced re-run of the same
        incident would `await` the previous run's already-resolved future and
        silently resume with the stale decision — rollback with no human
        decision that run (NG-1). Called by the driver at RUN START; `wait`
        then hands the new run a brand-new pending future. The sequential
        worker model (D-11) guarantees the old gate is done at this point: a
        parked incident blocks later work until it is decided."""
        self._futures.pop(incident_id, None)

    def wait(self, incident_id: str) -> Awaitable[dict]:
        # Intentionally idempotent: hand back whichever future exists,
        # including a DONE one. The §5.4 decision can legitimately resolve the
        # gate before the driver's `await wait(...)` resumes (the tests do this
        # deterministically; a fast click can too), and awaiting a done future
        # returns instantly. Freshness across RUNS is `reset`'s job at run
        # start — never a done-check here.
        fut = self._futures.get(incident_id)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self._futures[incident_id] = fut
        return fut

    def resolve(self, incident_id: str, body: dict) -> bool:
        fut = self._futures.get(incident_id)
        if fut is None or fut.done():
            return False  # unknown or already_decided — first wins (§5.4)
        fut.set_result(body)
        return True

    def pending(self) -> list[str]:
        return [iid for iid, fut in self._futures.items() if not fut.done()]


def _make_resume(awaiter: GateAwaiter) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
    """§5.4 seam injected into `_route_decision`: resolve the waiting room. On
    already-decided/unknown, raise so `_route_decision` turns it into a loud
    socket error and publishes NO misleading `decision` event."""

    async def resume(incident_id: str, body: dict[str, Any]) -> None:
        if not awaiter.resolve(incident_id, body):
            raise ValueError(f"already_decided or not awaiting: {incident_id}")

    return resume


class LiveDriver:
    """Runs one incident through a fresh graph, streaming every stage to the bus.

    Phase 1 `astream(updates)` streams each node's state delta. When the gate
    node calls `interrupt()`, LangGraph emits an `__interrupt__` chunk carrying
    the plan -> we publish `gate_open` and await the human. Phase 2 resumes the
    same thread with `Command(resume=body)` and streams the tail. The driver is
    the ONLY actor touching this graph (single-writer, D-11) — the socket only
    resolves the awaiter.
    """

    def __init__(self, bus: EventBus, awaiter: GateAwaiter, make_graph: Callable = build_graph):
        self.bus = bus
        self.awaiter = awaiter
        self.make_graph = make_graph

    async def run_once(self, alert: dict) -> dict:
        iid = alert["incident_id"]
        # Fresh gate for THIS run (postmortem 2026-09-23): a forced re-run must
        # not inherit the previous run's resolved decision. The sequential
        # worker guarantees any old gate is `done` here, so this only ever drops
        # a completed future — never a parked one.
        self.awaiter.reset(iid)
        graph = self.make_graph()
        cfg = {"configurable": {"thread_id": iid}}
        t0 = time.monotonic()
        parked = False
        with get_tracer().start_as_current_span(
            "nexusops.incident", attributes={"app.incident.id": iid}
        ):  # D-7: same parent span the benchmark opens around an incident
            async for chunk in graph.astream(
                {"incident_id": iid, "alert": alert}, cfg, stream_mode="updates"
            ):
                parked |= self._inspect(iid, chunk)
            if parked:
                body = await self.awaiter.wait(iid)  # human §5.4 click lands here
                async for chunk in graph.astream(Command(resume=body), cfg, stream_mode="updates"):
                    self._inspect(iid, chunk)
            final = graph.get_state(cfg).values
        t_resolve_ms = (time.monotonic() - t0) * 1000.0
        self.bus.publish(
            iid,
            "done",
            terminal=final.get("terminal"),
            t_resolve_ms=round(t_resolve_ms, 1),
            manual_review_reason=final.get("manual_review_reason"),
        )
        return final

    def _inspect(self, incident_id: str, chunk: dict[str, Any]) -> bool:
        """Map one astream chunk onto bus events. Returns True for the interrupt
        (signals the driver to park and await the human)."""
        parked = False
        for node, update in chunk.items():
            if node == "__interrupt__":
                parked = True
                value = update[0].value if update else {}
                self.bus.publish(
                    incident_id, "gate_open", plan=value.get("plan"), waiting=True
                )
                continue
            if not isinstance(update, dict):
                continue
            if node == "classify":
                if "manual_review_reason" in update:
                    self.bus.publish(incident_id, "manual_review",
                                     reason=update["manual_review_reason"], stage="classify")
                else:
                    self.bus.publish(
                        incident_id, "classified",
                        severity=update.get("severity"),
                        affected_service=update.get("affected_service"),
                        triage_confidence=update.get("triage_confidence"),
                        ambiguous=update.get("ambiguous"),
                    )
            elif node == "escalate":
                self.bus.publish(incident_id, "escalating")
            elif node == "evidence":
                self.bus.publish(incident_id, "tool_call", evidence=update.get("evidence"))
            elif node == "rca":
                if "manual_review_reason" in update:
                    self.bus.publish(incident_id, "manual_review",
                                     reason=update["manual_review_reason"], stage="rca")
                elif update.get("plan"):
                    self.bus.publish(
                        incident_id, "plan",
                        plan=update["plan"],
                        requires_approval=bool(update["plan"].get("requires_approval")),
                    )
            elif node == "rollback":
                self.bus.publish(incident_id, "rollback",
                                 rollback_result=update.get("rollback_result"))
            # gate resume chunk: the `decision` event is already published by
            # the §5.4 handler (`_route_decision`); terminal chunk: `done` is
            # published by run_once with the resolve metric — both skipped here.
        return parked


async def consume_loop(
    redis_client,
    driver: LiveDriver,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Serve worker: BRPOP the real queue, drive one incident at a time.

    Sequential by design (D-11): the demo is one complete cycle at a time and a
    parked gate blocks later work (D-3 accepts losing it on restart). EVERY
    iteration is exception-contained (postmortem 2026-09-23): one malformed
    queue item used to raise inside the loop, `asyncio.create_task` never
    retrieves the result, and the worker died with no traceback and no log —
    the queue silently stopped draining. A bad item now costs one loud failed
    attempt, never the worker. Only CancelledError (shutdown) propagates.

    `shutdown_event` is the graceful-stop guardrail (postmortem 2026-09-23,
    shutdown log): while a brpop's async_timeout is expiring, redis-py can
    CONSUME uvicorn's injected CancelledError and convert it to a redis
    TimeoutError (asyncio/timeouts.py uncancel+re-raise). The generic except
    then looped straight back into brpop, so the lifespan's `await task` never
    completed — the observed "SIGTERM ignored, needed SIGKILL" on two servers.
    The loop re-checks the event after every item and after any fetch failure,
    so graceful shutdown always terminates promptly.
    """
    shutdown_event = shutdown_event or asyncio.Event()
    while not shutdown_event.is_set():
        iid = "?"
        try:
            item = await redis_client.brpop([QUEUE_KEY], timeout=5)
            if item is None:
                continue
            _, raw = item
            alert = json.loads(raw)
            iid = alert.get("incident_id", "?")
            await driver.run_once(alert)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Two failure classes, handled differently (postmortem 2026-09-23,
            # shutdown log). With `iid == "?"` the exception happened while
            # FETCHING the next item (brpop read timeout, incl. uvicorn's
            # cancel converted to TimeoutError by redis-py) — no incident was
            # in flight, so no incident-level `error` event and no phantom
            # "?" incident in WS catch-up history; just a warning. Only a
            # real incident id is a pipeline failure worth a loud error event.
            if iid == "?":
                logger.warning("worker fetch failed (redis)", exc_info=True)
                continue
            logger.exception("live incident failed: %s", iid)
            driver.bus.publish(iid, "error", error="pipeline driver failure")


def create_serve_app(
    bus: EventBus | None = None,
    awaiter: GateAwaiter | None = None,
    run_worker: bool = True,
    make_graph: Callable = build_graph,
    fixtures: list | None = None,
    checkpoint_path: str | None = None,
) -> FastAPI:
    """Build the serve app. `run_worker=False` + injected fakes support the
    zero-quota test suite; fixtures/checkpoint are injectable for the same
    reason (Ponytail: tests never need network or Redis state to assert shape).
    """
    bus = bus or EventBus()
    awaiter = awaiter or GateAwaiter()
    driver = LiveDriver(bus, awaiter, make_graph)
    catalog = fixtures if fixtures is not None else [f for f in build_fixtures() if f.delivered]

    @asynccontextmanager
    async def _lifespan(a: FastAPI):
        redis_client = aioredis.from_url(
            os.environ.get("NEXUSOPS_REDIS_URL", DEFAULT_REDIS_URL), decode_responses=True
        )
        dedupe_script = redis_client.register_script(DEDUPE_AND_ENQUEUE)
        a.state.redis = redis_client
        a.state.dedupe_script = dedupe_script
        task: asyncio.Task | None = None
        shutdown_event = asyncio.Event()
        if run_worker:
            task = asyncio.create_task(consume_loop(redis_client, driver, shutdown_event))

            def _log_worker_death(t: asyncio.Task) -> None:
                # Postmortem 2026-09-23: an unretrieved create_task exception
                # leaves NO log and the queue silently stops draining. If the
                # loop dies despite the contained iteration, the done-callback
                # makes it loud the instant it happens.
                if not t.cancelled() and t.exception() is not None:
                    logger.critical("consume_loop died: %s", t.exception())

            task.add_done_callback(_log_worker_death)
        try:
            yield
        finally:
            if task is not None:
                # Graceful stop (postmortem 2026-09-23, shutdown log): signal
                # the loop FIRST so it exits cleanly at its next boundary; the
                # cancel below is the backstop for a brpop that redis-py may
                # convert into a swallowed TimeoutError. Await with a hard
                # deadline so a stuck worker can never wedge the stop.
                shutdown_event.set()
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=10.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            await redis_client.aclose()

    app = FastAPI(title="NexusOps — Live Operations Console", lifespan=_lifespan)
    app.state.bus = bus
    app.state.awaiter = awaiter

    @app.post("/webhook/incident", status_code=202)
    async def ingest_live(alert: IncidentAlert, force: bool = False):
        # `force=1` is the operator-initiated rehearsal path (D-11 console's
        # "fire live / fire again"): the dedupe seen-set is BYPASSED so a
        # fixture can re-run in one session. The Feature-1 contract
        # (app/ingest.py) stays dedupe-by-default — an alert sender never
        # passes force; only the console surface opens this door (postmortem
        # 2026-09-23: earlier benchmark/retry runs had pre-seeded every fixture
        # id, so demo fires returned duplicate:true, enqueued NOTHING and the
        # demo silently never ran; force is the loud fix).
        try:
            if force:
                await app.state.redis.lpush(QUEUE_KEY, alert.model_dump_json())
                added = 1
            else:
                added = await enqueue_alert(app.state.redis, app.state.dedupe_script, alert)
        except aioredis.RedisError:
            # NFR-6: loud 503, never a degraded mode — same contract as Feature 1.
            logger.exception("webhook rejected: redis unavailable")
            return JSONResponse(status_code=503, content={"error": "redis unavailable"})
        if added == 1:
            bus.publish(
                alert.incident_id, "ingest",
                source=alert.source, service=alert.service,
                message=alert.message, severity_hint=alert.severity_hint,
            )
        return {"incident_id": alert.incident_id, "duplicate": added != 1}

    @app.get("/api/fixtures")
    async def fixtures_api() -> list[dict]:
        # Alert fields only — ground truth never reaches the operator (NG-1:
        # the human decides, the demo cannot be "read" from the UI).
        return [
            {
                "incident_id": f.alert["incident_id"],
                "occurred_at": f.alert["occurred_at"],
                "source": f.alert["source"],
                "service": f.alert["service"],
                "message": f.alert["message"],
                "severity_hint": f.alert.get("severity_hint"),
            }
            for f in catalog
        ]

    @app.get("/api/status")
    async def status_api() -> dict:
        redis_ok, depth = False, None
        try:
            depth = await app.state.redis.llen(QUEUE_KEY)
            redis_ok = True
        except aioredis.RedisError:
            logger.exception("status: redis unreachable")
        return {
            "redis": "ok" if redis_ok else "unreachable",
            "queue_depth": depth,
            "gates_waiting": awaiter.pending(),
        }

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        reader = bus.subscribe()
        resume = _make_resume(awaiter)

        async def stream() -> None:
            try:
                while True:
                    await ws.send_json(await reader.get())
            except WebSocketDisconnect:
                pass

        async def listen() -> None:
            try:
                while True:
                    await _route_decision(ws, bus, resume, await ws.receive_json())
            except WebSocketDisconnect:
                pass

        await asyncio.gather(stream(), listen())
        bus.unsubscribe(reader)

    # Feature 6: the film stays reachable as the history tab. If no recording
    # is available, mount a stub that says so — serve must never crash on a
    # missing consumable (honest, loud).
    cp_path = checkpoint_path or os.environ.get("NEXUSOPS_CHECKPOINT")
    film_available = bool(cp_path) or Path(cp_path or CHECKPOINT_DEFAULT).exists()
    if film_available:
        try:
            film_app = create_player_app(checkpoint=_load_checkpoint_data(cp_path))
            app.mount("/film", film_app)
        except Exception:
            logger.exception("film mount failed — history tab disabled")
    else:
        app.mount("/film", _film_unavailable_app())

    # React build, served last so API/webhook/ws routes win (Starlette matches
    # in registration order).
    if FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="static")

    return app


def _load_checkpoint_data(path: str | None) -> dict | None:
    from app.player import load_checkpoint

    return load_checkpoint(path)


def _film_unavailable_app() -> FastAPI:
    from fastapi.responses import HTMLResponse

    stub = FastAPI(title="NexusOps — Film (unavailable)")

    @stub.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>No recording</title>"
            "<style>body{font-family:system-ui;background:#0f1115;color:#e8eaf0;"
            "display:grid;place-items:center;height:100vh;margin:0}"
            "div{max-width:34rem;text-align:center;line-height:1.7}"
            "code{color:#8ab4f8}</style></head><body><div>"
            "<h1>No recorded checkpoint yet</h1>"
            "<p>The history tab plays recorded benchmark runs from "
            "<code>outG1.json</code> (or <code>NEXUSOPS_CHECKPOINT</code>). "
            "Record a run first, then come back.</p></div></body></html>"
        )

    return stub


app = create_serve_app()