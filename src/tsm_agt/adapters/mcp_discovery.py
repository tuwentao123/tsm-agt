"""Bounded, credential-free discovery of the active MCP provider's catalog."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from tsm_agt.ports import (
    MCPClientManager, ToolCall, ToolEffect, ToolIdempotency,
    ToolResult, ToolResultAuthority, ToolRisk, ToolSpec,
)

DISCOVERY_TOOL = "core.mcp_discover"
_OVERVIEW_LIMIT = 20
_DESCRIPTION_LIMIT = 256


def _server_entries(
    manager: MCPClientManager, statuses: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    # Explicit projection: transport configuration and authentication never
    # enter this surface, even while a server is still connecting or failed.
    return [
        {
            "kind": "server", "id": server_id,
            "description": manager.server_config(server_id).description[:_DESCRIPTION_LIMIT],
            "state": statuses[server_id]["state"],
            "tool_count": statuses[server_id]["tool_count"],
        }
        for server_id in manager.server_ids()
    ]


def discovery_spec(
    manager: MCPClientManager, statuses: Mapping[str, Mapping[str, Any]],
) -> ToolSpec:
    entries = _server_entries(manager, statuses)
    return ToolSpec(
        name=DISCOVERY_TOOL,
        description=(
            "Discover configured MCP integrations, their connection state, and tool names. "
            "Use this entry point when an integration may provide the capability you need, "
            "including tools missing from the current tool list. Do not search private "
            "configuration files for connection details. Successful discovery refreshes "
            "the callable tool definitions for the next model request. "
            "Optionally wait up to 5 seconds for existing background initialization; "
            "waiting does not restart or cancel a connection. Use next_offset for more entries. "
            "This only observes the catalog; it does not invoke a remote business tool."
        ),
        parameters={
            "type": "object", "properties": {
                "server_id": {"type": "string", "minLength": 1,
                              "description": "Optional exact configured server id."},
                "query": {"type": "string", "maxLength": 256,
                          "description": "Optional case-insensitive substring of id, tool name, or description."},
                "wait_seconds": {"type": "number", "minimum": 0, "maximum": 5,
                                 "description": "Bounded observation wait; omitted means return immediately."},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }, "additionalProperties": False,
        },
        risk=ToolRisk.R0, is_read_only=True, is_concurrency_safe=True,
        idempotency=ToolIdempotency.IDEMPOTENT, max_result_tokens=6000,
        effect=ToolEffect.OBSERVE, result_authority=ToolResultAuthority.RUNTIME_FACT,
        refreshes_tool_catalog=True,
        model_context={
            "kind": "mcp_catalog", "servers": entries[:_OVERVIEW_LIMIT],
            "total_servers": len(entries), "has_more": len(entries) > _OVERVIEW_LIMIT,
            "discovery_tool": DISCOVERY_TOOL,
        },
    )


async def discover(
    call: ToolCall, manager: MCPClientManager,
    statuses: Mapping[str, Mapping[str, Any]],
    startup_tasks: Mapping[str, asyncio.Task[None]],
    tools: Callable[[], tuple[ToolSpec, ...]],
) -> ToolResult:
    server_id = call.arguments.get("server_id")
    if server_id is not None and server_id not in manager.server_ids():
        return ToolResult(
            call.call_id, False, error_code="MCP_SERVER_NOT_CONFIGURED",
            message="Unknown MCP server id; choose one from the configured server catalog.",
        )
    pending = {
        task for name, task in startup_tasks.items()
        if not task.done() and (server_id is None or name == server_id)
    }
    wait_seconds = call.arguments.get("wait_seconds", 0)
    if pending and wait_seconds:
        # asyncio.wait observes existing tasks; timeout/caller cancellation
        # cannot cancel the provider-owned connection attempts.
        await asyncio.wait(pending, timeout=wait_seconds)
    entries = _server_entries(manager, statuses)
    entries.extend({
        "kind": "tool", "name": spec.name,
        "server_id": spec.name.split(".", 2)[1],
        "description": spec.description[:_DESCRIPTION_LIMIT],
    } for spec in tools())
    if server_id is not None:
        entries = [entry for entry in entries if entry.get("server_id", entry.get("id")) == server_id]
    query = call.arguments.get("query", "").strip().casefold()
    if query:
        entries = [entry for entry in entries if query in " ".join(
            str(entry.get(key, "")) for key in ("id", "server_id", "name", "description")
        ).casefold()]
    revision = hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    offset, limit = call.arguments.get("offset", 0), call.arguments.get("limit", 20)
    page = entries[offset:offset + limit]
    # Only bounded, sanitized diagnostics for the server rows actually shown.
    for entry in page:
        if entry["kind"] == "server":
            entry["detail"] = statuses[entry["id"]]["detail"][:512]
    return ToolResult(call.call_id, True, data={
        "entries": page, "catalog_revision": revision,
        "total": len(entries), "offset": offset,
        "next_offset": offset + limit if offset + limit < len(entries) else None,
    }, meta={"untrusted_data": True})
