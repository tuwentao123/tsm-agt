"""R0 model tools backed by the replaceable CodeIntelligencePort."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, CodeIntelligencePort, HealthState,
    HealthStatus, ToolCall, ToolIdempotency, ToolInvocationContext, ToolResult,
    ToolRisk, ToolSpec,
)


class CodeIntelligenceToolProvider:
    descriptor = AdapterDescriptor(
        "builtin.code-intelligence-tools", "0.1.0", "ToolProviderPort", "1.0",
        frozenset({
            "code.definition", "code.diagnostics", "code.implementations",
            "code.references", "code.rename_preview", "code.symbol_overview",
            "code.workspace_symbols",
        }),
    )

    def __init__(self, provider: CodeIntelligencePort) -> None:
        self._provider = provider
        self._started = False

    @staticmethod
    def _spec(
        name: str, description: str, properties: Mapping[str, Any],
        required: tuple[str, ...] = (),
    ) -> ToolSpec:
        schema: dict[str, Any] = {
            "type": "object", "properties": dict(properties),
            "additionalProperties": False,
        }
        if required:
            schema["required"] = list(required)
        return ToolSpec(
            name, description, schema, ToolRisk.R0, is_read_only=True,
            is_concurrency_safe=True, idempotency=ToolIdempotency.IDEMPOTENT,
        )

    _tools = (
        _spec.__func__(
            "code.symbol_overview",
            "List declarations in one source file. Returns symbol kind, name, source "
            "location, signature, workspace fingerprint and index version.",
            {"path": {"type": "string"}, "limit": {"type": "integer"}},
            ("path",),
        ),
        _spec.__func__(
            "code.definition",
            "Find declarations of an exact symbol, optionally below a file or directory.",
            {"symbol": {"type": "string"}, "path": {"type": "string"},
             "limit": {"type": "integer"}}, ("symbol",),
        ),
        _spec.__func__(
            "code.references",
            "Find identifier references with path, line and column. This is a bounded "
            "lexical fallback and reports whether a match is a known declaration.",
            {"symbol": {"type": "string"}, "path": {"type": "string"},
             "include_declaration": {"type": "boolean"},
             "limit": {"type": "integer"}}, ("symbol",),
        ),
        _spec.__func__(
            "code.implementations",
            "Find class-like declarations whose signature extends, implements or "
            "otherwise names an exact base symbol.",
            {"symbol": {"type": "string"}, "limit": {"type": "integer"}},
            ("symbol",),
        ),
        _spec.__func__(
            "code.workspace_symbols",
            "Search indexed declarations by case-insensitive symbol-name substring.",
            {"query": {"type": "string"}, "limit": {"type": "integer"}},
            ("query",),
        ),
        _spec.__func__(
            "code.diagnostics",
            "Return available static diagnostics. The built-in fallback currently "
            "provides Python syntax diagnostics and explicitly reports coverage.",
            {"path": {"type": "string"}, "limit": {"type": "integer"}},
        ),
        _spec.__func__(
            "code.rename_preview",
            "Preview identifier rename edits and conflicts without writing files. "
            "A preview is not authority to apply changes.",
            {"symbol": {"type": "string"}, "new_name": {"type": "string"},
             "path": {"type": "string"}, "limit": {"type": "integer"}},
            ("symbol", "new_name"),
        ),
    )

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        provider = await self._provider.health()
        healthy = self._started and provider.state is HealthState.HEALTHY
        return HealthStatus(
            HealthState.HEALTHY if healthy else HealthState.UNHEALTHY,
            "code intelligence tools ready" if healthy else "provider unavailable",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        if not self._started:
            raise RuntimeError("code intelligence tools are not started")
        try:
            data = await self._dispatch(call, context)
            return ToolResult(
                call.call_id, True, data, truncated=bool(data.get("truncated")),
                meta={"untrusted_data": True, "read_only": True},
            )
        except (KeyError, TypeError, ValueError, PermissionError) as error:
            return ToolResult(
                call.call_id, False, error_code="INVALID_PARAM", message=str(error),
                hint="Use a workspace-relative path and the advertised bounded schema.",
                meta={"untrusted_data": True},
            )
        except OSError as error:
            return ToolResult(
                call.call_id, False, error_code="TOOL_FAILED", message=str(error),
                retryable=True, meta={"untrusted_data": True},
            )

    async def _dispatch(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> Mapping[str, Any]:
        args = call.arguments
        limit = self._limit(args)
        path = self._optional_string(args, "path")
        if call.name == "code.symbol_overview":
            if path is None:
                raise ValueError("path is required")
            return await self._provider.symbol_overview(context.workspace, path, limit=limit)
        if call.name == "code.definition":
            return await self._provider.definition(
                context.workspace, self._required_string(args, "symbol"),
                path=path, limit=limit,
            )
        if call.name == "code.references":
            include = args.get("include_declaration", True)
            if not isinstance(include, bool):
                raise TypeError("include_declaration must be a boolean")
            return await self._provider.references(
                context.workspace, self._required_string(args, "symbol"),
                path=path, include_declaration=include, limit=limit,
            )
        if call.name == "code.implementations":
            return await self._provider.implementations(
                context.workspace, self._required_string(args, "symbol"), limit=limit
            )
        if call.name == "code.workspace_symbols":
            return await self._provider.workspace_symbols(
                context.workspace, self._required_string(args, "query"), limit=limit
            )
        if call.name == "code.diagnostics":
            return await self._provider.diagnostics(
                context.workspace, path=path, limit=limit
            )
        if call.name == "code.rename_preview":
            return await self._provider.rename_preview(
                context.workspace, self._required_string(args, "symbol"),
                self._required_string(args, "new_name"), path=path, limit=limit,
            )
        raise ValueError(f"unknown code intelligence tool: {call.name}")

    @staticmethod
    def _required_string(arguments: Mapping[str, Any], name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _optional_string(arguments: Mapping[str, Any], name: str) -> str | None:
        value = arguments.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string when provided")
        return value.strip()

    @staticmethod
    def _limit(arguments: Mapping[str, Any]) -> int:
        value = arguments.get("limit", 200)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 500:
            raise ValueError("limit must be an integer from 1 to 500")
        return value
