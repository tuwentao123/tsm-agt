"""MCP client management layer.

This module defines a small, self-contained manager for MCP clients.  It does
not hard-code any particular SDK; instead it works with an arbitrary
``client_factory`` so callers can supply the concrete Model Context Protocol
Python SDK integration.

The intent is that higher-level adapters (for example a ToolProvider
implementation) can depend on ``MCPClientManager`` to obtain and cache
connections to multiple MCP servers, without needing to know how those
connections are created.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """Configuration for one MCP server.

    This structure is intentionally generic and does not depend on a specific
    MCP SDK.  It can describe executable, HTTP, or Python-module servers,
    together with any additional configuration the concrete client factory
    requires.
    """

    #: Stable identifier used by callers to refer to this server.
    id: str
    #: Transport kind (for example: "executable", "http", "python").
    kind: str
    #: Command-line for an executable server, when applicable.
    command: tuple[str, ...] | None = None
    #: Base URL for an HTTP server, when applicable.
    url: str | None = None
    #: Python module path for an in-process server, when applicable.
    module: str | None = None
    #: Arbitrary structured configuration passed through to the client factory.
    #: This can include MCP roots, authentication parameters, or any other
    #: server-specific options.
    options: Mapping[str, Any] = field(default_factory=dict)


class MCPClientManager:
    """Create and cache MCP clients from declarative configuration.

    ``MCPClientManager`` is responsible for:

    * holding the set of configured servers;
    * lazily creating a client for a server on first use; and
    * caching that client for subsequent lookups.

    It delegates actual client construction to a ``client_factory`` callback so
    that the integration with the Model Context Protocol Python SDK can evolve
    independently of this manager's public API.
    """

    def __init__(
        self,
        servers: tuple[MCPServerConfig, ...],
        client_factory: Callable[[MCPServerConfig], Awaitable[Any]],
    ) -> None:
        self._servers: dict[str, MCPServerConfig] = {server.id: server for server in servers}
        self._client_factory = client_factory
        self._clients: dict[str, Any] = {}

    def server_ids(self) -> tuple[str, ...]:
        """Return the stable identifiers for all configured servers."""

        return tuple(self._servers.keys())

    def server_config(self, server_id: str) -> MCPServerConfig:
        """Look up configuration for a single server.

        Raises ``KeyError`` if the identifier is unknown.
        """

        try:
            return self._servers[server_id]
        except KeyError as exc:  # pragma: no cover - trivial error path
            raise KeyError(f"unknown MCP server id: {server_id}") from exc

    async def get_client(self, server_id: str) -> Any:
        """Return a client for ``server_id``, creating it on first use.

        The concrete type of the returned client depends on the
        ``client_factory`` supplied at construction time; this method preserves
        that type unchanged and does not impose any additional protocol.
        """

        if server_id in self._clients:
            return self._clients[server_id]

        config = self.server_config(server_id)
        client = await self._client_factory(config)
        self._clients[server_id] = client
        return client

    async def ensure_all_started(self) -> None:
        """Eagerly create clients for all configured servers.

        This is useful in environments where a caller prefers to fail fast if
        any server cannot be reached or started, rather than discovering
        connectivity problems later during tool execution.
        """

        for server_id in self._servers.keys():
            await self.get_client(server_id)

