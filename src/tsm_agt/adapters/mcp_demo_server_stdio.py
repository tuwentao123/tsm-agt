"""Minimal stdio-based MCP demo server using the official Python SDK.

This module defines a tiny MCP server that exposes a couple of tools via the
``stdio`` transport. It is intended purely for local verification of the
``create_mcp_sdk_client`` integration and does not form part of the
tsm-agt public API.

Usage
-----

Run the server as a module:

    python -m tsm_agt.adapters.mcp_demo_server_stdio

The server exposes at least the following tools:

* ``echo(message: str) -> dict`` – echo the incoming message and a timestamp;
* ``add(a: int, b: int) -> int`` – add two integers.

The corresponding client-side demo lives in
``src/tsm_agt/adapters/mcp_sdk_demo_verify.py`` and uses
``create_mcp_sdk_client`` plus ``MCPServerConfig`` to perform an
end-to-end check.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict


try:  # pragma: no cover - demo server depends on optional MCP SDK
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover - exercised when SDK missing
    _SERVER_IMPORT_ERROR: Exception | None = exc
else:  # pragma: no cover - thin wrapper around SDK constructs
    _SERVER_IMPORT_ERROR = None


@dataclass(slots=True)
class EchoPayload:
    """Structured payload returned by the demo ``echo`` tool."""

    message: str
    server_time: str


def _build_server() -> Any:
    """Create and configure a FastMCP server instance.

    Raises a ``RuntimeError`` with a clear message if the MCP SDK is missing.
    """

    if _SERVER_IMPORT_ERROR is not None:
        raise RuntimeError(
            "The 'mcp' Python package (with fastmcp server support) is required "
            "to run the demo MCP stdio server. Install it with "
            "`pip install \"mcp[cli]\"` or add it to your project dependencies."
        ) from _SERVER_IMPORT_ERROR

    mcp = FastMCP(name="tsm-agt demo MCP stdio server")

    @mcp.tool()
    def echo(message: str) -> Dict[str, Any]:
        """Echo the incoming message along with a server-side timestamp."""

        payload = EchoPayload(
            message=message,
            server_time=datetime.now(timezone.utc).isoformat(),
        )
        return asdict(payload)

    @mcp.tool()
    def add(a: int, b: int) -> int:
        """Add two integers and return the sum."""

        return a + b

    return mcp


def main() -> None:
    """Entry point for ``python -m tsm_agt.adapters.mcp_demo_server_stdio``."""

    mcp = _build_server()
    # Use the stdio transport so ``create_mcp_sdk_client`` can launch this
    # server as a subprocess via ``stdio_client``.
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - module execution path
    main()

