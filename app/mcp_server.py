"""NexusOps — Feature 2: MCP server exposing three mock tools (NG-2).

Real MCP protocol, fake backends. stdio transport (Phase 1 decision: the state
machine spawns this as a local subprocess). Deterministic mock data (NFR-5).

Error semantics (§5.2): "service has no logs in window" -> empty result;
"service does not exist" -> raise ToolError (is_error=True), never a silent
empty. ToolError is the SDK's anticipated-failure channel: the caller (state
machine, Phase 3) sees is_error=True and can fail loud instead of trusting an
empty list that means "no data" when it actually meant "you asked for a ghost".
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer("nexusops")

# Mock backend data (deterministic per NFR-5: fixed seed, fixed fixtures).
SERVICES = {"auth", "payments", "catalog", "worker"}
METRICS = {"http_requests_total", "error_rate", "p99_latency_ms"}

# Tool-call telemetry: every call recorded, consumers (benchmark) read it.
CALL_LOG: list[dict] = []

# Gate: commit SHAs the human operator has approved (feature spec, rollback
# tool). Populated by state-machine feature via approve_rollback() after the
# §5.4 approval decision. NOT an MCP tool — MCP surface stays exactly 3 tools.
_APPROVED: set[str] = set()


def approve_rollback(commit_sha: str) -> None:
    """Non-MCP hook: record human gate approval. State machine calls after §5.4."""
    _APPROVED.add(commit_sha)


def _now() -> datetime:
    return datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def _synthesize_logs(
    service: str, start: datetime, end: datetime, count: int
) -> list[dict]:
    """Deterministic log lines for a service within [start, end).

    RNG is seeded per-service: the payload for (service, window) depends only
    on that request, never on what other tools were called before (NFR-5).
    """
    rng = random.Random(f"{service}:{start.isoformat()}")
    lines = []
    for i in range(count):
        ts = start + timedelta(seconds=i * 7)
        if ts >= end:
            break
        lines.append(
            {
                "timestamp": ts.isoformat(),
                "level": rng.choice(["info", "warning", "error"]),
                "message": f"{service} sample log {i}",
            }
        )
    return lines


def _parse_window(timestamp_window: dict) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(timestamp_window["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(timestamp_window["end"].replace("Z", "+00:00"))
    if not start.tzinfo or not end.tzinfo:
        raise ToolError("timestamp_window must use RFC3339 timezone-aware timestamps")
    if start >= end:
        raise ToolError(f"timestamp_window start {start} must be before end {end}")
    return start, end


@mcp.tool()
def fetch_service_logs(service_name: str, timestamp_window: dict) -> dict:
    """Fetch logs for a service within a time window.

    Returns {"logs": [...]}, empty list if the window has no data. Raises
    ToolError if the service does not exist — absence is not the same as empty.
    """
    CALL_LOG.append(
        {"tool": "fetch_service_logs", "args": {"service_name": service_name, "timestamp_window": timestamp_window}}
    )
    if service_name not in SERVICES:
        raise ToolError(f"service '{service_name}' does not exist")
    start, end = _parse_window(timestamp_window)
    # Mock fixture rule: catalog has no activity today (the "empty window" case);
    # every other service has deterministic logs. One rule, easy to read.
    if service_name == "catalog" and start.date() == _now().date():
        return {"logs": []}
    return {"logs": _synthesize_logs(service_name, start, end, count=12)}


@mcp.tool()
def query_prometheus_metrics(metric_name: str, duration: str) -> dict:
    """Query a metric series. Unknown metric -> ToolError (loud), never empty."""
    CALL_LOG.append({"tool": "query_prometheus_metrics", "args": {"metric_name": metric_name, "duration": duration}})
    if metric_name not in METRICS:
        raise ToolError(f"metric '{metric_name}' does not exist")
    rng = random.Random(f"metric:{metric_name}:{duration}")
    return {
        "series": [
            {"timestamp": (_now() - timedelta(minutes=n)).isoformat(), "value": round(rng.uniform(0, 100), 3)}
            for n in range(20)
        ]
    }


@mcp.tool()
def trigger_github_rollback(commit_sha: str) -> dict:
    """Gated rollback: rejected unless the incident is gate-approved (§5.2)."""
    CALL_LOG.append({"tool": "trigger_github_rollback", "args": {"commit_sha": commit_sha}})
    if commit_sha not in _APPROVED:
        return {"status": "rejected", "message": "gate not approved for this commit"}
    return {"status": "performed", "message": f"rollback to {commit_sha} performed (mock)"}


if __name__ == "__main__":
    mcp.run(transport="stdio")