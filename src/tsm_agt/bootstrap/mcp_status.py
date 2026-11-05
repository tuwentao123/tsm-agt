"""Read-only MCP server status for the Web settings panel.

The settings panel needs two different answers about the private YAML
``mcp`` section: which servers are configured, and whether each one can
actually be reached right now.  Configuration parsing already lives in
:mod:`tsm_agt.bootstrap.mcp_configuration`; this module adds the second
answer by reusing the production SDK client factory, so the reported state
comes from a real ``initialize`` + ``tools/list`` round trip instead of a
guess based on the configuration shape.

Nothing here mutates configuration, registers tools, or keeps a client
alive: every probe client is closed before the report is returned.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from tsm_agt.ports import MCPServerConfig

from .mcp_configuration import load_mcp_servers_from_configuration


DEFAULT_PROBE_TIMEOUT_SECONDS = 8.0


def mcp_configuration_summary(
    document: Mapping[str, object],
) -> tuple[bool, tuple[MCPServerConfig, ...], tuple[str, ...]]:
    """Return ``(enabled, servers, issues)`` for a parsed private document.

    A malformed ``mcp`` section is reported as an issue instead of raising so
    the settings panel can show the reason inline.
    """

    try:
        enabled, servers = load_mcp_servers_from_configuration(document)
    except (TypeError, ValueError) as error:
        return False, (), (str(error),)
    return enabled, servers, ()


def _server_row(server: MCPServerConfig) -> dict[str, Any]:
    # Option values may hold credentials (for example an ``env`` map), so only
    # the option names are reported.
    return {
        "id": server.id,
        "kind": server.kind,
        "command": list(server.command) if server.command else None,
        "url": server.url,
        "module": server.module,
        "option_keys": sorted(str(key) for key in server.options),
    }


async def _probe_server(
    server: MCPServerConfig, timeout_seconds: float,
) -> tuple[bool, tuple[str, ...], str]:
    """Open one real client, list its tools, then close it again."""

    # Imported lazily: composition imports adapters, and this module is
    # imported by the web entry point.
    from .composition import mcp_sdk_client_factory

    client: Any = None
    try:
        client = await asyncio.wait_for(mcp_sdk_client_factory(server), timeout_seconds)
        tools = await asyncio.wait_for(client.list_tools(), timeout_seconds)
    except TimeoutError:
        return False, (), f"连接超时（{timeout_seconds:g} 秒内未完成握手或工具列表）"
    except Exception as error:  # Any transport/SDK failure means "not usable".
        detail = str(error).strip()
        return False, (), f"{type(error).__name__}: {detail}" if detail else type(error).__name__
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # A failed close must not hide the probe result.
                pass
    names = tuple(
        name for name in (getattr(tool, "name", None) for tool in tools)
        if isinstance(name, str) and name
    )
    return True, names, f"已连通，发现 {len(names)} 个工具"


async def describe_mcp_servers(
    enabled: bool,
    servers: Sequence[MCPServerConfig],
    *,
    probe: bool = True,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Describe every configured server, with real availability when probed.

    ``available`` is ``True``/``False`` once probed, and ``None`` when nothing
    was attempted (disabled globally, or probing was not requested).
    """

    results: Sequence[tuple[bool, tuple[str, ...], str]] | None = None
    if probe and enabled and servers:
        # Independent servers start in parallel so the panel is not serialised
        # behind the slowest transport.
        results = await asyncio.gather(
            *(_probe_server(server, timeout_seconds) for server in servers),
        )

    rows: list[dict[str, Any]] = []
    for index, server in enumerate(servers):
        row = _server_row(server)
        if not enabled:
            row.update(
                available=None,
                tools=[],
                detail="mcp.enabled = false，未注册到工具列表",
            )
        elif results is None:
            row.update(available=None, tools=[], detail="未检测（仅显示配置）")
        else:
            available, tools, detail = results[index]
            row.update(available=available, tools=list(tools), detail=detail)
        rows.append(row)
    return rows


__all__ = [
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "describe_mcp_servers",
    "mcp_configuration_summary",
]
