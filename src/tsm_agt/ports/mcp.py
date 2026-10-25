"""Protocol-neutral MCP server configuration and client lifecycle contracts.

Concrete MCP SDK clients live in adapters.  The manager belongs here because
both the SDK transport adapter and the model-visible tool adapter need the
same server identity and lifecycle without depending on one another.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


_SERVER_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """One configured MCP server, independent of a particular SDK."""

    id: str
    kind: str
    command: tuple[str, ...] | None = None
    url: str | None = None
    module: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _SERVER_ID.fullmatch(self.id):
            raise ValueError(
                "MCP server id must contain only letters, numbers, '_' or '-'"
            )
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("MCP server kind must be a non-empty string")
        if self.command is not None and (
            not self.command
            or any(not isinstance(item, str) or not item for item in self.command)
        ):
            raise ValueError("MCP server command must contain non-empty strings")
        if not isinstance(self.options, Mapping):
            raise TypeError("MCP server options must be a mapping")


class MCPClient(Protocol):
    """The narrow SDK-independent surface needed by the MCP tool provider."""

    async def list_tools(self) -> Sequence[Any]: ...

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any: ...

    async def aclose(self) -> None: ...


MCPClientFactory = Callable[[MCPServerConfig], Awaitable[MCPClient]]


class MCPClientManager:
    """Create one client per configured server and close all clients safely."""

    def __init__(
        self, servers: tuple[MCPServerConfig, ...], client_factory: MCPClientFactory,
    ) -> None:
        server_map = {server.id: server for server in servers}
        if len(server_map) != len(servers):
            raise ValueError("MCP server ids must be unique")
        self._servers = server_map
        self._client_factory = client_factory
        self._clients: dict[str, MCPClient] = {}
        self._locks = {server_id: asyncio.Lock() for server_id in server_map}
        self._closed = False

    def server_ids(self) -> tuple[str, ...]:
        return tuple(self._servers)

    def server_config(self, server_id: str) -> MCPServerConfig:
        try:
            return self._servers[server_id]
        except KeyError as exc:
            raise KeyError(f"unknown MCP server id: {server_id}") from exc

    async def get_client(self, server_id: str) -> MCPClient:
        if self._closed:
            raise RuntimeError("MCP client manager is closed")
        client = self._clients.get(server_id)
        if client is not None:
            return client
        config = self.server_config(server_id)
        async with self._locks[server_id]:
            client = self._clients.get(server_id)
            if client is not None:
                return client
            if self._closed:
                raise RuntimeError("MCP client manager is closed")
            client = await self._client_factory(config)
            if self._closed:
                await client.aclose()
                raise RuntimeError("MCP client manager is closed")
            self._clients[server_id] = client
            return client

    async def ensure_all_started(self) -> None:
        for server_id in self.server_ids():
            await self.get_client(server_id)

    async def aclose(self) -> None:
        """Close every created transport exactly once, in reverse start order."""
        if self._closed:
            return
        self._closed = True
        clients = tuple(self._clients.values())
        self._clients.clear()
        errors: list[Exception] = []
        for client in reversed(clients):
            try:
                await client.aclose()
            except Exception as error:  # Close every peer before surfacing one.
                errors.append(error)
        if errors:
            raise RuntimeError("MCP client shutdown failed") from errors[0]


__all__ = [
    "MCPClient",
    "MCPClientFactory",
    "MCPClientManager",
    "MCPServerConfig",
]
