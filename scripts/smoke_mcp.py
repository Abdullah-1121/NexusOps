"""Live smoke test: real MCP client <-> app.mcp_server over stdio.

Proves the server boots and serves tools end-to-end (initialize handshake,
tools/list, tools/call) through the actual protocol, not just imported
functions. Feature 2 task 2.5 exit criteria.
"""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            assert names == [
                "fetch_service_logs",
                "query_prometheus_metrics",
                "trigger_github_rollback",
            ], names
            res = await session.call_tool(
                "fetch_service_logs",
                {
                    "service_name": "auth",
                    "timestamp_window": {
                        "start": "2026-09-08T10:00:00Z",
                        "end": "2026-09-08T11:59:00Z",
                    },
                },
            )
            assert not res.is_error
            logs = json.loads(res.content[0].text)["logs"]
            assert len(logs) > 0
            rejected = await session.call_tool("trigger_github_rollback", {"commit_sha": "abc"})
            assert json.loads(rejected.content[0].text)["status"] == "rejected"
            print("LIVE PROTOCOL OK: handshake, list_tools, call_tool, gate")


if __name__ == "__main__":
    asyncio.run(main())