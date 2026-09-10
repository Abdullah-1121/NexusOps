"""NexusOps — Feature 3: MCP evidence gathering stage.

Calls the Feature-2 MCP server over the REAL protocol (NG-2) via stdio, one
subprocess per incident, both evidence tools inside the same session. Results
feed the RCA stage; the tool arguments + results land in state so they are
trace-visible (FR-2).

The gather function is injected into the state machine so tests can substitute
a deterministic fake (no subprocess, no network — NFR-5).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EvidenceFn = Callable[[dict], Awaitable[list]]


async def gather_evidence(incident: dict) -> list:
    """Default gatherer: real MCP client -> feature-2 server (stdio subprocess).

    window is last 15 minutes ending now; query the error-rate metric.
    """
    now = datetime.now(timezone.utc)
    window = {
        "start": (now - timedelta(minutes=15)).isoformat(),
        "end": now.isoformat(),
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            logs_res = await session.call_tool(
                "fetch_service_logs",
                {"service_name": incident["service"], "timestamp_window": window},
            )
            metrics_res = await session.call_tool(
                "query_prometheus_metrics", {"metric_name": "error_rate", "duration": "15m"}
            )
    records = []
    if not logs_res.is_error:
        records.extend(json.loads(logs_res.content[0].text).get("logs", []))
    if not metrics_res.is_error:
        records.extend(json.loads(metrics_res.content[0].text).get("series", []))
    # A structured error from the mock is evidence too — never dropped silently.
    if logs_res.is_error or metrics_res.is_error:
        records.append({"error": logs_res.content[0].text if logs_res.is_error else ""})
        records.append({"error": metrics_res.content[0].text if metrics_res.is_error else ""})
    return records