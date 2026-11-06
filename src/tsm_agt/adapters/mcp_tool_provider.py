"""Expose configured MCP tools as normal model-visible Runtime tools."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    MCPCallNotSentError,
    MCPClientManager,
    MCPServerStartupError,
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
from .mcp_errors import mcp_failure_detail
from .mcp_discovery import DISCOVERY_TOOL, discover, discovery_spec


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
        self._startup_tasks: dict[str, asyncio.Task[None]] = {}
        self._server_tools: dict[str, tuple[ToolSpec, ...]] = {}
        self._server_status: dict[str, dict[str, Any]] = {}
        self._stop_task: asyncio.Task[None] | None = None

    async def start(self, context: AdapterContext) -> None:
        del context
        if self._started:
            return
        self._started = True
        # Optional integrations must not gate the core Runtime. Only publish a
        # server's tools after its complete initialization/discovery succeeds.
        for server_id in self._manager.server_ids():
            config = self._manager.server_config(server_id)
            self._server_status[server_id] = {
                "id": server_id, "required": config.required,
                "state": "connecting", "tool_count": 0, "detail": "MCP 正在后台连接",
            }
            self._startup_tasks[server_id] = asyncio.create_task(
                self._start_server(server_id), name=f"mcp-startup:{server_id}",
            )
        try:
            await asyncio.gather(*(
                task for server_id, task in self._startup_tasks.items()
                if self._manager.server_config(server_id).required
            ))
        except BaseException as error:
            try:
                await self.stop(datetime.now())
            except Exception as cleanup_error:
                error.add_note(f"MCP startup cleanup also failed: {type(cleanup_error).__name__}")
            raise

    async def _start_server(self, server_id: str) -> None:
        config = self._manager.server_config(server_id)
        stage = "initialization"
        try:
            async with asyncio.timeout(config.startup_timeout_seconds):
                await self._manager.get_client(server_id)
                stage = "tool discovery"
                tools = await self._discover_tools((server_id,))
            if not self._started:
                return
            self._server_tools[server_id] = tools
            self._tools = tuple(
                spec for name in self._manager.server_ids()
                for spec in self._server_tools.get(name, ())
            )
            self._server_status[server_id].update(
                state="ready", tool_count=len(tools), detail=f"已连通，发现 {len(tools)} 个工具",
            )
        except asyncio.CancelledError:
            self._server_status[server_id].update(state="stopped", detail="MCP 连接已停止")
            raise
        except Exception as error:
            detail = (
                str(error) if isinstance(error, MCPServerStartupError)
                else f"MCP server '{server_id}' failed during {stage}: " + (
                    f"timeout ({config.startup_timeout_seconds:g}s)"
                    if isinstance(error, TimeoutError) else mcp_failure_detail(error)
                )
            )
            self._server_status[server_id].update(state="failed", detail=detail)
            try:
                await self._manager.close_server(server_id)
            except Exception as cleanup_error:
                self._server_status[server_id]["cleanup_error"] = type(cleanup_error).__name__
            if config.required:
                if isinstance(error, MCPServerStartupError):
                    raise
                raise MCPServerStartupError(
                    server_id, stage,
                    f"timeout ({config.startup_timeout_seconds:g}s)"
                    if isinstance(error, TimeoutError) else mcp_failure_detail(error),
                ) from error

    def startup_status(self) -> tuple[dict[str, Any], ...]:
        """Read startup state without creating new transports or disclosing config."""
        return tuple(dict(row) for row in self._server_status.values())

    async def wait_ready(self) -> None:
        """Explicitly wait for the configured discovery attempts (e.g. in tests)."""
        await asyncio.gather(*self._startup_tasks.values())

    async def health(self) -> HealthStatus:
        if not self._started:
            return HealthStatus(HealthState.UNHEALTHY, "MCP tools not started")
        degraded = any(row["state"] != "ready" for row in self._server_status.values())
        return HealthStatus(
            HealthState.DEGRADED if degraded else HealthState.HEALTHY,
            "Some MCP servers are connecting or unavailable" if degraded else "MCP tools ready",
        )

    async def stop(self, deadline: datetime) -> None:
        del deadline
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        cancelled = False
        while not self._stop_task.done():
            try:
                await asyncio.shield(self._stop_task)
            except asyncio.CancelledError:
                cancelled = True
        self._stop_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _stop(self) -> None:
        self._started = False
        try:
            for task in self._startup_tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._startup_tasks.values(), return_exceptions=True)
            await self._manager.aclose()
        finally:
            self._tools = ()
            self._server_tools.clear()
            for row in self._server_status.values():
                if row["state"] != "failed":
                    row.update(state="stopped", tool_count=0, detail="MCP 连接已停止")

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        if not self._started:
            raise RuntimeError("MCP tool provider is not started")
        if not self._manager.server_ids():
            return self._tools
        return (discovery_spec(self._manager, self._server_status), *self._tools)

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
        if call.name == DISCOVERY_TOOL:
            return await discover(
                call, self._manager, self._server_status,
                self._startup_tasks, lambda: self._tools,
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
        except KeyError:
            return _error(
                call, "MCP_SERVER_NOT_CONFIGURED",
                f"MCP server '{server_id}' is not configured",
                retryable=False,
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            )
        except Exception as error:
            return _error(
                call, "MCP_CONNECTION_ERROR",
                f"MCP server '{server_id}' connection is unavailable; '{tool_name}' was not dispatched ({type(error).__name__})",
                retryable=True,
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            )

        try:
            raw_result = await client.call_tool(tool_name, call.arguments)
            result_data = _json_data(raw_result)
            is_error = _mcp_result_is_error(raw_result, result_data)
        except MCPCallNotSentError:
            return _error(
                call, "MCP_CALL_NOT_SENT",
                f"MCP server '{server_id}' connection is unavailable; '{tool_name}' was not dispatched",
                retryable=True,
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            )
        except Exception as error:
            # SDK exceptions include read timeouts, lost responses and output
            # validation failures *after* a server may have executed the call.
            # All currently advertised MCP tools are non-idempotent. Only an
            # explicit not-sent guarantee can avoid uncertainty here.
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                error_code="UNKNOWN_OUTCOME",
                message=(
                    f"MCP tool '{call.name}' may have executed, but no trustworthy "
                    "final result was received. Do not repeat the operation "
                    "before checking the target system."
                ),
                hint="Inspect the target system before deciding whether to retry.",
                retryable=False,
                recovery_kind=ToolRecoveryKind.UNKNOWN_OUTCOME,
                meta={"untrusted_data": True, "error_type": type(error).__name__},
            )
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

    async def _discover_tools(self, server_ids: tuple[str, ...]) -> tuple[ToolSpec, ...]:
        specs: list[ToolSpec] = []
        names: set[str] = set()
        for server_id in server_ids:
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
                            str(description) if isinstance(description, str) and description.strip()
                            else f"Call MCP server '{server_id}' tool '{tool_name}' using its advertised input schema."
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
