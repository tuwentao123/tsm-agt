"""Simple in-process MCP demo wiring for local experiments.

This module provides a tiny, self-contained "MCP server" implementation that
stays entirely in-process.  It implements the minimal client protocol expected
by :class:`MCPToolProvider` so that the rest of the Runtime can exercise MCP
tool listing and invocation without any external dependencies or processes.

The goal is to demonstrate the end-to-end flow:

* configure one or more MCP servers via :class:`MCPServerConfig`;
* provide a ``client_factory`` that returns a client with ``list_tools`` and
  ``call_tool`` coroutine methods;
* wire everything into an :class:`Application` using
  :func:`compose_mcp_fixture_application`;
* call ``application.kernel.list_tools()`` and observe MCP tools; and
* invoke an ``mcp.*`` tool name and see the result propagate back as a
  :class:`ToolResult`.

This is intentionally small and deterministic so it can be used in tests,
notebooks or manual experiments without any additional configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Mapping

from tsm_agt.bootstrap.composition import compose_mcp_fixture_application
from tsm_agt.ports import MCPServerConfig


@dataclass(slots=True)
class _DemoToolDescription:
    """Minimal tool description used by :class:`_DemoMCPClient`.

    The :class:`MCPToolProvider` only relies on ``name``, ``description`` and
    ``parameters`` attributes, so this light-weight structure is sufficient for
    the demo client.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]


class _DemoMCPClient:
    """Very small in-process stand-in for an MCP client.

    It exposes two coroutine methods matching the protocol expected by
    :class:`MCPToolProvider`:

    * ``list_tools()`` returns demo tool descriptors; and
    * ``call_tool(name, arguments)`` returns a serialisable result object.
    """

    def __init__(self, server: MCPServerConfig) -> None:
        self._server = server

    async def list_tools(self) -> tuple[_DemoToolDescription, ...]:  # pragma: no cover - trivial
        """Return a small, deterministic set of demo tools.

        The concrete schema does not matter as long as it is JSON serialisable
        and consistent with :class:`ToolSpec.parameters`.
        """

        return (
            _DemoToolDescription(
                name="echo",
                description="Echo back the provided JSON arguments.",
                parameters={"type": "object", "properties": {}},
            ),
            _DemoToolDescription(
                name="server_info",
                description="Return static information about this demo MCP server.",
                parameters={"type": "object", "properties": {}},
            ),
        )

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:  # pragma: no cover - trivial
        """Execute a demo tool.

        The return value is deliberately simple so it is easy to inspect in
        traces or tests.
        """

        if name == "echo":
            return {
                "tool": "echo",
                "server_id": self._server.id,
                "arguments": dict(arguments),
            }
        if name == "server_info":
            return {
                "tool": "server_info",
                "server_id": self._server.id,
                "kind": self._server.kind,
                "options": dict(self._server.options),
            }
        return {
            "tool": name,
            "server_id": self._server.id,
            "error": "unknown demo MCP tool",
        }

    async def aclose(self) -> None:  # pragma: no cover - no resources to close
        return None


async def _demo_client_factory(config: MCPServerConfig) -> _DemoMCPClient:
    """Client factory compatible with :class:`MCPClientManager`.

    This function matches ``Callable[[MCPServerConfig], Awaitable[Any]]`` so it
    can be passed directly to :func:`compose_mcp_fixture_application`.
    """

    return _DemoMCPClient(config)


def compose_demo_mcp_application() -> tuple[Any, tuple[str, ...]]:
    """Compose a fixture :class:`Application` with a single demo MCP server.

    Returns a pair ``(application, expected_tool_prefixes)`` where
    ``application`` is a fully-wired Runtime :class:`Application` and
    ``expected_tool_prefixes`` contains the MCP tool name prefix for the demo
    server (for example ``("mcp.demo.echo", "mcp.demo.server_info")`` once
    tools are listed).

    Example
    -------
    >>> from tsm_agt.adapters.mcp_demo_fixture import compose_demo_mcp_application
    >>> application, prefixes = compose_demo_mcp_application()
    >>> kernel = application.kernel
    >>> visible_tools = asyncio.run(kernel.list_tools())  # doctest: +SKIP
    """

    from tsm_agt.bootstrap.composition import Application

    # A single logical MCP server identified as "demo".  The transport-specific
    # fields are left at defaults because this demo is fully in-process.
    servers: tuple[MCPServerConfig, ...] = (
        MCPServerConfig(id="demo", kind="inprocess"),
    )

    application: Application = compose_mcp_fixture_application(
        servers=servers,
        client_factory=_demo_client_factory,
    )

    tool_prefixes = (
        "mcp.demo.echo",
        "mcp.demo.server_info",
    )
    return application, tool_prefixes


__all__ = [
    "compose_demo_mcp_application",
]

