"""MCP server configuration loading.

This module provides a small helper for loading ``MCPServerConfig`` entries
from JSON configuration files. It is intentionally generic and does not wire
itself into any particular application composition function; callers are
expected to decide where MCP servers should be enabled.

The configuration format is deliberately close to the patterns used by other
MCP-enabled agent projects (for example a top-level ``"mcpServers"``
dictionary keyed by server identifier) while remaining minimal and
tsm-agt-specific in its defaults.

Configuration shape
--------------------

The loader understands JSON files with the following structure::

    {
      "version": 1,
      "mcpServers": {
        "demo": {
          "enabled": true,
          "kind": "inprocess",
          "command": ["python", "-m", "my.demo.server"],
          "url": null,
          "module": null,
          "options": {
            "root": "."
          }
        }
      }
    }

Only ``"mcpServers"`` and the per-server entries are required. Unknown
keys are ignored. Each server entry is mapped to :class:`MCPServerConfig` and
returned as a tuple.

The production composition root instead reads the ``mcp.servers`` mapping in
the single private YAML configuration file. JSON parsing remains available as
an explicit import/export helper only.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable, Mapping

from tsm_agt.ports import MCPServerConfig


@dataclass(frozen=True, slots=True)
class MCPServersFile:
    """Parsed representation of one MCP servers JSON file."""

    path: Path
    servers: tuple[MCPServerConfig, ...]


def _parse_server_entry(server_id: str, raw: dict) -> MCPServerConfig:
    """Convert one JSON object into :class:`MCPServerConfig`.

    Required transport values are validated later by the concrete client
    factory. Unknown extension keys are ignored; server-specific values belong
    under ``options``.
    """

    kind = str(raw.get("kind", "executable"))
    # ``command`` is represented in JSON as a list of strings. It is mapped to
    # the tuple form used by :class:`MCPServerConfig`.
    command_value = raw.get("command")
    command: tuple[str, ...] | None
    if isinstance(command_value, list) and all(
        isinstance(part, str) and part for part in command_value
    ):
        command = tuple(command_value)
    elif isinstance(command_value, str):
        # For convenience a single string may be supplied; it is split into
        # one-element tuple so callers can treat it uniformly.
        command = (command_value,)
    else:
        command = None

    url_value = raw.get("url")
    url = str(url_value) if isinstance(url_value, str) else None

    module_value = raw.get("module")
    module = str(module_value) if isinstance(module_value, str) else None

    options_value = raw.get("options")
    options: dict
    if isinstance(options_value, dict):
        options = dict(options_value)
    else:
        options = {}

    return MCPServerConfig(
        id=server_id,
        kind=kind,
        command=command,
        url=url,
        module=module,
        options=options,
        required=raw.get("required", False),
        startup_timeout_seconds=raw.get("startup_timeout_seconds", 30.0),
        description=raw.get("description", ""),
    )


def load_mcp_servers_from_configuration(
    document: Mapping[str, object],
) -> tuple[bool, tuple[MCPServerConfig, ...]]:
    """Read MCP enablement and servers from the private YAML document."""

    mcp = document.get("mcp", {})
    if not isinstance(mcp, Mapping) or any(not isinstance(key, str) for key in mcp):
        raise ValueError("mcp must be a mapping")
    enabled = mcp.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("mcp.enabled must be true or false")
    raw_servers = mcp.get("servers", {})
    if not isinstance(raw_servers, Mapping) or any(not isinstance(key, str) for key in raw_servers):
        raise ValueError("mcp.servers must be a mapping")
    servers: list[MCPServerConfig] = []
    for server_id, raw in raw_servers.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"mcp.servers.{server_id} must be a mapping")
        item = dict(raw)
        if item.get("enabled", True) is False:
            continue
        servers.append(_parse_server_entry(server_id, item))
    return enabled, tuple(servers)


def load_mcp_servers_file(path: Path) -> MCPServersFile:
    """Load one JSON MCP servers configuration file.

    The caller is responsible for choosing the file path. This helper only
    parses its contents and returns a structured representation suitable for
    constructing an :class:`MCPClientManager`.
    """

    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - trivial error path
        raise ValueError(f"invalid MCP servers JSON in {path}: {exc}") from exc

    raw_servers = data.get("mcpServers")
    if not isinstance(raw_servers, dict):
        return MCPServersFile(path=path, servers=())

    servers: list[MCPServerConfig] = []
    for server_id, raw in raw_servers.items():
        if not isinstance(server_id, str) or not isinstance(raw, dict):
            continue
        enabled = raw.get("enabled", True)
        if enabled is False or (
            isinstance(enabled, str) and enabled.strip().casefold() in {"0", "false", "no", "off"}
        ):
            # Respect explicit disable flags.
            continue
        servers.append(_parse_server_entry(server_id, raw))

    return MCPServersFile(path=path, servers=tuple(servers))


def merge_mcp_servers(files: Iterable[MCPServersFile]) -> tuple[MCPServerConfig, ...]:
    """Merge multiple files into a single server configuration tuple.

    Later files win for duplicate ``id`` entries. This mirrors typical
    "global + workspace" layering schemes used by other agents while keeping
    the implementation simple.
    """

    merged: dict[str, MCPServerConfig] = {}
    for file in files:
        for server in file.servers:
            merged[server.id] = server
    return tuple(merged.values())


__all__ = [
    "MCPServersFile",
    "load_mcp_servers_from_configuration",
    "load_mcp_servers_file",
    "merge_mcp_servers",
]

