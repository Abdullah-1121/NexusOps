"""NexusOps — Feature 4: WebSocket stream + minimal live dashboard.

Pattern A: an EventBus in the middle. Each WS client subscribes at connect,
gets a catch-up replay of in-flight incidents, then live events. Decisions
arrive on the same socket and are carried to the parked gate through the
injected `resume_incident` (gate logic stays 100% in Feature 3). POST remains
the canonical API; this socket only mirrors its §5.4 semantics.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from langgraph.types import Command

from app.bus import EventBus

logger = logging.getLogger("nexusops.dashboard")

ResumeFn = Callable[[str, dict[str, Any]], Awaitable[None]]

DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>NexusOps</title>
<style>
  body{font-family:system-ui,sans-serif;background:#0f1115;color:#e8eaf0;margin:0;padding:2rem}
  h1{font-size:1.2rem;letter-spacing:.1em;color:#8ab4f8}
  .card{background:#171b24;border-radius:8px;border-left:4px solid #5b6b8c;padding:1rem;margin-bottom:.75rem}
  .card h3{margin:0 0 .4rem;font-size:1rem}
  .card ul{list-style:none;margin:0 0 .5rem;padding:0}
  .card li{font:12px/1.6 ui-monospace,monospace;color:#9aa4b8}
  button{margin-right:.5rem;border:0;border-radius:4px;padding:.35rem .8rem;cursor:pointer}
  .approve{background:#2e7d32;color:#fff}.reject{background:#c62828;color:#fff}
</style>
</head>
<body>
<h1>NexusOps — live incidents</h1>
<div id="incidents"></div>
<script>
  const cards = {};
  function paint(ev, showButtons) {
    const id = ev.incident_id;
    let card = cards[id];
    if (!card) {
      card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = '<h3></h3><ul></ul><span class="buttons"></span>';
      document.getElementById('incidents').prepend(card);
      cards[id] = card;
    }
    card.querySelector('h3').textContent = id;
    const li = document.createElement('li');
    const details = Object.entries(ev).filter(([k]) => !['type','incident_id','seq'].includes(k))
      .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`).join(' ');
    li.textContent = `[${ev.seq}] ${ev.type} ${details}`;
    card.querySelector('ul').appendChild(li);
    if (showButtons && ev.type === 'gate_open') {
      const wrap = card.querySelector('.buttons');
      const mk = (d, cls) => { const b = document.createElement('button'); b.className = cls; b.textContent = d;
        b.onclick = () => ws.send(JSON.stringify({incident_id: id, decision: d, actor: 'ops'})); wrap.appendChild(b); };
      mk('approve', 'approve'); mk('reject', 'reject');
    }
  }
  let ws;
  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = (m) => { try { paint(JSON.parse(m.data), true); } catch (e) {} };
    ws.onclose = () => setTimeout(connect, 1000);
    ws.onerror = () => ws.close();
  }
  connect();
</script>
</body>
</html>"""


def make_resume_incident(graph) -> ResumeFn:
    """The decision-up seam: carry §5.4 to the parked gate thread.

    Composition rule (proven in tests): the *socket* guarantees the correct
    §5.4 body reaches this function; this function performs the exact
    `Command(resume=...)` call Feature 3's tests already prove unblocks the
    gate. Together they compose acceptance §3.
    """

    async def resume(incident_id: str, body: dict[str, Any]) -> None:
        await graph.ainvoke(
            Command(resume=body),
            {"configurable": {"thread_id": incident_id}},
        )

    return resume


def create_dashboard_app(bus: EventBus, resume_incident: ResumeFn) -> FastAPI:
    app = FastAPI(title="NexusOps Dashboard")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return DASHBOARD_HTML

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        reader = bus.subscribe()

        async def stream() -> None:
            try:
                while True:
                    await ws.send_json(await reader.get())
            except WebSocketDisconnect:
                pass

        async def listen() -> None:
            try:
                while True:
                    await _route_decision(ws, bus, resume_incident, await ws.receive_json())
            except WebSocketDisconnect:
                pass

        await asyncio.gather(stream(), listen())
        bus.unsubscribe(reader)

    return app


async def _route_decision(ws: WebSocket, bus: EventBus, resume_incident: ResumeFn, data: Any) -> None:
    """Validate one §5.4 decision frame at the socket boundary.

    Trust boundary: bad frames get a loud error back but keep the socket alive;
    an invalid decision that slips validation is caught by the gate's own
    strict check and surfaced the same way (fail loud, NFR-4).
    """
    if not isinstance(data, dict):
        await ws.send_json({"type": "error", "error": "message must be a JSON object"})
        return
    incident_id = data.get("incident_id")
    decision = data.get("decision")
    actor = data.get("actor")
    if not all(isinstance(v, str) and v for v in (incident_id, decision, actor)):
        await ws.send_json(
            {"type": "error", "error": "incident_id, decision, actor are required strings"}
        )
        return
    try:
        await resume_incident(incident_id, {"decision": decision, "actor": actor})
    except Exception:
        logger.exception("decision resume failed for %s", incident_id)
        await ws.send_json({"type": "error", "error": f"resume failed for {incident_id}"})
        return
    bus.publish(incident_id, "decision", decision=decision, actor=actor)