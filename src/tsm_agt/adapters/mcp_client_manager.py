"""Backward-compatible import location for protocol-neutral MCP contracts."""

from tsm_agt.ports.mcp import (
    MCPClient, MCPClientFactory, MCPClientManager, MCPServerConfig,
)

__all__ = [
    "MCPClient", "MCPClientFactory", "MCPClientManager", "MCPServerConfig",
]

