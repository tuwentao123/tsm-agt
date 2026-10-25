"""Expose configured MCP tools as normal model-visible Runtime tools."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    MCPClientManager,
    ModelToolContract,
    ToolCall,
    ToolEffect,
    ToolFactCapability,
    ToolFactDescriptor,
    ToolIdempotency,
    ToolInvocationContext,
    ToolProviderPort,
    ToolRecoveryKind,
    ToolResult,
    ToolResultAuthority,
    ToolResultMaterialization,
    ToolRisk,
    ToolSpec,
)


class MCPToolProvider(ToolProviderPort):
    """Bridge initialized MCP sessions into the Runtime tool registry.

    The provider carries no task semantics: it advertises the server's input
    schemas, invokes the selected tool after Kernel policy/approval, and
    returns a JSON-shaped transport observation for the next model sample.
    """

    descriptor = AdapterDescriptor(
        adapter_id="mcp.tool-provider",
        adapter_version="1.0.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"mcp"}),
    )

    def __init__(self, manager: MCPClientManager) -> None:
        self._manager = manager
        self._started = False
        self._tools: tuple[ToolSpec, ...] = ()

    async def start(self, context: AdapterContext) -> None:
        del context
        # Fail during application startup, before a model is shown a tool that
        # cannot actually be reached. This also executes MCP initialize once.
        try:
            await self._manager.ensure_all_started()
            self._tools = await self._discover_tools()
            self._started = True
        except BaseException:
            await self._manager.aclose()
            raise

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "MCP tools ready" if self._started else "MCP tools not started",
        )

    async def stop(self, deadline: datetime) -> None:
        del deadline
        try:
            await self._manager.aclose()
        finally:
            self._started = False
            self._tools = ()

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        if not self._started:
            raise RuntimeError("MCP tool provider is not started")
        return self._tools

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext,
    ) -> ToolResult:
        del context
        if not self._started:
            return _error(
                call, "MCP_NOT_STARTED", "MCP tool provider is not started",
                retryable=True,
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            )
        parsed = _parse_tool_name(call.name)
        if parsed is None:
            return _error(
                call, "INVALID_MCP_TOOL_NAME",
                "MCP tool name must have the form 'mcp.<server_id>.<tool_name>'",
                retryable=False,
                recovery_kind=ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS,
            )
        server_id, tool_name = parsed
        if not any(spec.name == call.name for spec in self._tools):
            return _error(
                call, "MCP_TOOL_NOT_ADVERTISED",
                f"MCP tool '{call.name}' is not advertised by server '{server_id}'",
                retryable=False,
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            )
        try:
            client = await self._manager.get_client(server_id)
            raw_result = await client.call_tool(tool_name, call.arguments)
        except KeyError:
            return _error(
                call, "MCP_SERVER_NOT_CONFIGURED",
                f"MCP server '{server_id}' is not configured",
                retryable=False,
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            )
        except Exception as error:
            return _error(
                call, "MCP_TRANSPORT_ERROR",
                f"MCP server '{server_id}' could not run '{tool_name}': {type(error).__name__}: {error}",
                retryable=True,
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            )

        result_data = _json_data(raw_result)
        is_error = _mcp_result_is_error(raw_result, result_data)
        fact = ToolFactDescriptor(
            authority=ToolResultAuthority.EXTERNAL_SERVICE,
            scope=(f"mcp_server:{server_id}", f"mcp_tool:{tool_name}"),
            artifact_refs=(),
            source_artifacts=(),
            materialization=ToolResultMaterialization.CONTENT,
            capabilities=(ToolFactCapability.DESCRIBE_CONTENT,),
            content_hash=None,
        )
        if is_error:
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                data=result_data,
                error_code="MCP_TOOL_ERROR",
                message=f"MCP tool '{call.name}' reported an error",
                retryable=False,
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
                fact_descriptor=fact,
            )
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data=result_data,
            fact_descriptor=fact,
            meta={"untrusted_data": True},
        )

    async def _discover_tools(self) -> tuple[ToolSpec, ...]:
        specs: list[ToolSpec] = []
        names: set[str] = set()
        for server_id in self._manager.server_ids():
            client = await self._manager.get_client(server_id)
            raw_tools: Sequence[Any] = await client.list_tools()
            for raw_tool in raw_tools:
                tool_name = getattr(raw_tool, "name", None)
                if not isinstance(tool_name, str) or not tool_name:
                    raise RuntimeError(
                        f"MCP server '{server_id}' advertised a tool without a name"
                    )
                full_name = f"mcp.{server_id}.{tool_name}"
                if full_name in names:
                    raise RuntimeError(f"MCP server advertised duplicate tool '{full_name}'")
                schema = _tool_schema(raw_tool)
                description = getattr(raw_tool, "description", None)
                specs.append(ToolSpec(
                    name=full_name,
                    description=(
                        str(description) if isinstance(description, str) and description.strip()
                        else f"Tool '{tool_name}' from configured MCP server '{server_id}'."
                    ),
                    parameters=schema,
                    risk=ToolRisk.R2,
                    is_read_only=False,
                    is_concurrency_safe=False,
                    idempotency=ToolIdempotency.NON_IDEMPOTENT,
                    requires_network=False,
                    data_transmission="configured MCP server",
                    rollback="MCP server defines side effects; inspect its result before further action",
                    effect=ToolEffect.UNSPECIFIED,
                    result_authority=ToolResultAuthority.EXTERNAL_SERVICE,
                    model_contract=ModelToolContract(
                        purpose=(
                            f"Call MCP server '{server_id}' tool '{tool_name}' using its "
                            "advertised input schema."
                        ),
                        returns=(
                            "untrusted content returned by the configured MCP server",
                        ),
                    ),
                ))
                names.add(full_name)
        return tuple(specs)


def _parse_tool_name(name: str) -> tuple[str, str] | None:
    prefix, separator, rest = name.partition("mcp.")
    if prefix or not separator:
        return None
    server_id, separator, tool_name = rest.partition(".")
    if not separator or not server_id or not tool_name:
        return None
    return server_id, tool_name


def _tool_schema(raw_tool: Any) -> dict[str, Any]:
    """Accept official SDK ``inputSchema`` and fixture ``parameters``."""
    schema = getattr(raw_tool, "inputSchema", None)
    if schema is None:
        schema = getattr(raw_tool, "input_schema", None)
    if schema is None:
        schema = getattr(raw_tool, "parameters", None)
    result = _json_data(schema)
    if not isinstance(result, dict) or result.get("type") != "object":
        raise RuntimeError("MCP tool input schema must be a JSON object schema")
    return result


def _mcp_result_is_error(raw_result: Any, result_data: Any) -> bool:
    for name in ("isError", "is_error"):
        value = getattr(raw_result, name, None)
        if isinstance(value, bool):
            return value
    if isinstance(result_data, Mapping):
        for name in ("isError", "is_error"):
            if isinstance(result_data.get(name), bool):
                return result_data[name]
    return False


def _json_data(value: Any) -> Any:
    """Convert SDK/Pydantic result objects into checkpoint-safe JSON values."""
    if hasattr(value, "model_dump"):
        return _json_data(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_data(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _error(
    call: ToolCall, code: str, message: str, *, retryable: bool,
    recovery_kind: ToolRecoveryKind,
) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        ok=False,
        error_code=code,
        message=message,
        retryable=retryable,
        recovery_kind=recovery_kind,
        meta={"untrusted_data": True},
    )


__all__ = ["MCPToolProvider"]
