"""MCP ToolProvider adapter.

This module adapts Model Context Protocol (MCP) servers to tsm-agt's
ToolProviderPort so that MCP tools appear as ordinary model-visible tools.

It deliberately does *not* hard-code any particular MCP Python SDK. Instead it
depends on ``MCPClientManager`` plus a very small client protocol:

* ``await client.list_tools()`` → iterable of tool descriptors;
* ``await client.call_tool(name, arguments)`` → result object.

Callers are responsible for constructing :class:`MCPClientManager` with a
``client_factory`` that creates concrete SDK clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from tsm_agt.ports import (
    ToolProviderPort,
    ToolSpec,
    ToolCall,
    ToolResult,
    ToolInvocationContext,
    ModelToolContract,
    ToolResultMaterialization,
    ToolFactDescriptor,
    ToolFactCapability,
    ToolResultAuthority,
    ToolRisk,
    ToolIdempotency,
    ToolRecoveryKind,
)

from .mcp_client_manager import MCPClientManager, MCPServerConfig


@dataclass(frozen=True, slots=True)
class MCPToolDescription:
    """Minimal MCP tool metadata used by this adapter.

    The concrete MCP client is expected to expose tool descriptions that can
    be mapped into this structure.  For example, an SDK may provide a
    ``Tool`` type with ``name``, ``description`` and ``input_schema`` fields.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]


class MCPToolProvider(ToolProviderPort):
    """Expose tools from one or more MCP servers as Runtime tools.

    Each configured MCP server contributes a set of tools.  At the Runtime
    boundary those are presented as ordinary :class:`ToolSpec` instances with
    names of the form ``"mcp.<server_id>.<tool_name>"``.

    The provider itself remains agnostic of the concrete MCP SDK; it only
    assumes that a client returned by :class:`MCPClientManager` implements two
    coroutine methods:

    * ``list_tools()`` → iterable of :class:`MCPToolDescription`-like objects;
    * ``call_tool(name: str, arguments: Mapping[str, Any])``.
    """

    def __init__(self, manager: MCPClientManager) -> None:
        self._manager = manager

    @property
    def descriptor(self):  # type: ignore[override]
        # AdapterDescriptor is defined in the ports package; importing it here
        # would introduce a circular reference for type-checkers.  To keep the
        # dependency surface small, this property is defined lazily.
        from tsm_agt.ports.adapter import AdapterDescriptor

        return AdapterDescriptor(
            adapter_id="mcp.tool_provider",
            port_name="ToolProviderPort",
            description="Expose Model Context Protocol tools as Runtime tools",
            health_check=None,
        )

    async def start(self, context):  # type: ignore[override]
        # ``context`` is an AdapterContext; the MCP client factory wiring is
        # handled by the caller that constructs ``MCPClientManager``.
        del context
        # For now there is no eager startup work beyond allowing callers to
        # opt into ``ensure_all_started`` if they want fail-fast semantics.

    async def stop(self, deadline):  # type: ignore[override]
        del deadline
        # Concrete MCP clients may offer explicit close/shutdown semantics. If
        # the client_factory wires those in, callers can extend this method to
        # invoke them.  The generic adapter does not assume such a protocol.

    async def list_tools(self) -> tuple[ToolSpec, ...]:  # type: ignore[override]
        specs: list[ToolSpec] = []
        for server_id in self._manager.server_ids():
            client = await self._manager.get_client(server_id)
            # The client is expected to return tool descriptors; we only rely
            # on ``name``, ``description`` and ``parameters``.
            mcp_tools: Sequence[Any] = await client.list_tools()
            for tool in mcp_tools:
                name = getattr(tool, "name", None)
                description = getattr(tool, "description", "")
                parameters = getattr(tool, "parameters", {})
                if not isinstance(name, str) or not name:
                    continue
                full_name = f"mcp.{server_id}.{name}"
                specs.append(
                    ToolSpec(
                        name=full_name,
                        description=str(description),
                        parameters=dict(parameters),
                        risk=ToolRisk.R2,
                        is_read_only=False,
                        is_concurrency_safe=True,
                        idempotency=ToolIdempotency.NON_IDEMPOTENT,
                    )
                )
        return tuple(specs)

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:  # type: ignore[override]
        del context
        # Tool names are of the form "mcp.<server_id>.<tool_name>".
        if not call.name.startswith("mcp."):
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                data=None,
                error_code="UNSUPPORTED_TOOL",
                message=f"MCPToolProvider only handles mcp.* tools (got {call.name})",
                hint="Select a tool whose name starts with 'mcp.' to use the MCP adapter.",
                retryable=False,
                truncated=False,
                meta={
                    "untrusted_data": True,
                    "result_governance": {
                        "schema_version": 2,
                        "profile": "tool_error",
                        "redacted": False,
                        "provider_truncated": False,
                        "original_content_bytes": 0,
                        "visible_content_bytes": 0,
                        "content_hash": "",
                    },
                },
                recovery_kind=ToolRecoveryKind.RECOVERABLE,
                recovery_action={},
                fact_descriptor=None,
            )

        try:
            _, server_id, tool_name = call.name.split(".", 2)
        except ValueError:
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                data=None,
                error_code="INVALID_MCP_TOOL_NAME",
                message=f"Invalid MCP tool name: {call.name}",
                hint="Expected name of the form 'mcp.<server_id>.<tool_name>'.",
                retryable=False,
                truncated=False,
                meta={
                    "untrusted_data": True,
                    "result_governance": {
                        "schema_version": 2,
                        "profile": "tool_error",
                        "redacted": False,
                        "provider_truncated": False,
                        "original_content_bytes": 0,
                        "visible_content_bytes": 0,
                        "content_hash": "",
                    },
                },
                recovery_kind=ToolRecoveryKind.RECOVERABLE,
                recovery_action={},
                fact_descriptor=None,
            )

        client = await self._manager.get_client(server_id)
        result_data = await client.call_tool(tool_name, call.arguments)

        fact = ToolFactDescriptor(
            authority=ToolResultAuthority.EXTERNAL_SERVICE,
            scope=(f"mcp_server:{server_id}", f"mcp_tool:{tool_name}"),
            artifact_refs=(),
            source_artifacts=(),
            materialization=ToolResultMaterialization.CONTENT,
            capabilities=(ToolFactCapability.DESCRIBE_CONTENT,),
            content_hash=None,
        )

        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data=result_data,
            error_code=None,
            message=None,
            hint=None,
            retryable=False,
            truncated=False,
            meta={
                "untrusted_data": True,
                "result_governance": {
                    "schema_version": 2,
                    "profile": "external_service",
                    "redacted": False,
                    "provider_truncated": False,
                    "original_content_bytes": 0,
                    "visible_content_bytes": 0,
                    "content_hash": "",
                },
            },
            recovery_kind=ToolRecoveryKind.RECOVERABLE,
            recovery_action={},
            fact_descriptor=fact,
        )

