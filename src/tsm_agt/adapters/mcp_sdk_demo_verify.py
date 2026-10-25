"""End-to-end verification script for the MCP SDK integration.

This module uses :func:`create_mcp_sdk_client` together with a simple
``MCPServerConfig`` that launches the demo stdio MCP server defined in
``mcp_demo_server_stdio``. It then:

* lists available tools from the server; and
* calls ``echo`` and ``add`` once.

The script is intended for manual/local verification and is not part of the
public tsm-agt API surface.

Run it as a module from the repository root:

    python -m tsm_agt.adapters.mcp_sdk_demo_verify

It will print the observed tool names and the results of the demo tool
invocations. Any exception indicates a misconfiguration of the MCP SDK
integration or the demo server.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tsm_agt.ports import MCPClientManager, MCPServerConfig
from tsm_agt.bootstrap.composition import mcp_sdk_client_factory


async def _verify_once() -> None:
    """Run a single end-to-end MCP check using the demo stdio server."""

    # Configure a single executable MCP server that launches the demo
    # stdio-based FastMCP server within this project.
    demo_server = MCPServerConfig(
        id="demo_stdio",
        kind="executable",
        command=("python", "-m", "tsm_agt.adapters.mcp_demo_server_stdio"),
        url=None,
        module=None,
        options={},
    )

    manager = MCPClientManager(servers=(demo_server,), client_factory=mcp_sdk_client_factory)
    try:
        client: Any = await manager.get_client("demo_stdio")

        # 1. List tools and show their names.
        tools = await client.list_tools()
        tool_names = [getattr(t, "name", "<unknown>") for t in tools]
        print("MCP demo server tools:", tool_names)

        # 2. Call the echo tool.
        echo_result = await client.call_tool("echo", {"message": "hello from mcp-sdk-demo"})
        print("echo result:", echo_result)

        # 3. Call the add tool.
        add_result = await client.call_tool("add", {"a": 2, "b": 5})
        print("add result:", add_result)
    finally:
        await manager.aclose()


def main() -> None:
    """Entry point for ``python -m tsm_agt.adapters.mcp_sdk_demo_verify``."""

    asyncio.run(_verify_once())


if __name__ == "__main__":  # pragma: no cover - module execution path
    main()

