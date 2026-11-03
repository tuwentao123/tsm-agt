"""Local MCP test process: pagination, explicit failure and lost write response."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server


async def serve(args) -> None:
    if args.pid:
        Path(args.pid).write_text(str(os.getpid()), encoding="utf-8")
    if args.stall:
        await asyncio.Future()
    server = Server("tsm-mcp-p0-test")
    tools = [types.Tool(
        name=name, description=name,
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        outputSchema=({"type": "object", "properties": {"number": {"type": "integer"}},
                       "required": ["number"]} if name == "bad_output" else None),
    ) for name in ("echo", "write", "fail", "bad_output")]

    async def list_tools(request):
        cursor = request.params.cursor if request.params else None
        page, next_cursor = (tools[:1], "rest") if cursor is None else (tools[1:], None)
        return types.ServerResult(types.ListToolsResult(tools=page, nextCursor=next_cursor))

    async def call_tool(request):
        name = request.params.name
        if name in {"write", "bad_output"}:
            with Path(args.receipt).open("a", encoding="utf-8") as receipt:
                receipt.write(f"{name}\n")
            if name == "write":
                os._exit(0)  # Applied, but deliberately no RPC response.
        return types.ServerResult(types.CallToolResult(
            content=[types.TextContent(type="text", text="explicit failure" if name == "fail" else name)],
            structuredContent=({"number": "invalid"} if name == "bad_output" else None),
            isError=name == "fail",
        ))

    server.request_handlers[types.ListToolsRequest] = list_tools
    server.request_handlers[types.CallToolRequest] = call_tool
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt")
    parser.add_argument("--pid")
    parser.add_argument("--stall", action="store_true")
    asyncio.run(serve(parser.parse_args()))
