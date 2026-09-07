"""Built-in task-scoped background process management tools."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    ToolCall,
    ToolIdempotency,
    ToolInvocationContext,
    ToolResult,
    ToolRisk,
    ToolSpec,
)


class CoreProcessToolProvider:
    """Expose process management without depending on Kernel or OS details."""

    descriptor = AdapterDescriptor(
        adapter_id="builtin.core-process-tools",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset(
            {
                "core.run_command", "core.process_status",
                "core.process_logs", "core.process_stop",
            }
        ),
    )

    _tools = (
        ToolSpec(
            name="core.run_command",
            description=(
                "Run a command as a structured argv array inside the task workspace. "
                "No shell parses the arguments. Use foreground mode for bounded commands "
                "such as builds and tests; use background mode for servers, watchers, or "
                "log streams, then manage the returned process_id with process tools. "
                "The environment starts minimal and rejects credential-like variable names. "
                "This action requires approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "argv": {"type": "array"},
                    "cwd": {"type": "string"},
                    "environment": {"type": "object"},
                    "mode": {"type": "string"},
                    "timeout_seconds": {"type": "number"},
                    "termination_grace_seconds": {"type": "number"},
                    "max_output_bytes": {"type": "integer"},
                    "max_lifetime_seconds": {"type": "number"},
                    "stop_on_task_end": {"type": "boolean"},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R2,
            is_read_only=False,
            is_concurrency_safe=False,
            idempotency=ToolIdempotency.NON_IDEMPOTENT,
            rollback=(
                "stop a returned background process; foreground filesystem or external "
                "side effects require separate inspection and rollback"
            ),
        ),
        ToolSpec(
            name="core.process_status",
            description=(
                "Get the current state of one background process owned by this task. "
                "Returns RUNNING, EXITED, CANCELLED, TIMED_OUT, or ORPHANED plus "
                "lifecycle timestamps and exit information."
            ),
            parameters={
                "type": "object",
                "properties": {"process_id": {"type": "string"}},
                "required": ["process_id"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
        ),
        ToolSpec(
            name="core.process_logs",
            description=(
                "Read retained stdout and stderr from one task-owned background "
                "process. Pass the returned next cursors on the next call to receive "
                "only new output. Output is bounded and may report truncation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "process_id": {"type": "string"},
                    "stdout_cursor": {"type": "integer"},
                    "stderr_cursor": {"type": "integer"},
                },
                "required": ["process_id"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
        ),
        ToolSpec(
            name="core.process_stop",
            description=(
                "Stop one background process owned by this task. The supervisor sends "
                "TERM to its process group, waits for the grace period, and uses KILL "
                "only if needed. This action requires approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "process_id": {"type": "string"},
                    "grace_seconds": {"type": "number"},
                },
                "required": ["process_id"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R2,
            is_read_only=False,
            is_concurrency_safe=False,
            idempotency=ToolIdempotency.IDEMPOTENT,
            rollback="cannot restart the same process; start a new process if still needed",
        ),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "core process tools ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        if not self._started:
            raise RuntimeError("adapter is not started")
        control = context.process_control
        if control is None:
            return self._error(
                call, "CAPABILITY_UNAVAILABLE",
                "this runtime did not grant task-scoped process management",
            )
        try:
            if call.name == "core.run_command":
                data = await self._run_command(call.arguments, control)
            elif call.name == "core.process_status":
                process_id = self._non_empty_string(call.arguments, "process_id")
                data = await control.status(process_id)
            elif call.name == "core.process_logs":
                process_id = self._non_empty_string(call.arguments, "process_id")
                stdout_cursor = self._non_negative_int(
                    call.arguments, "stdout_cursor", 0
                )
                stderr_cursor = self._non_negative_int(
                    call.arguments, "stderr_cursor", 0
                )
                data = await control.logs(process_id, stdout_cursor, stderr_cursor)
            elif call.name == "core.process_stop":
                process_id = self._non_empty_string(call.arguments, "process_id")
                grace_seconds = self._non_negative_number(
                    call.arguments, "grace_seconds", 2.0
                )
                data = await control.stop(process_id, grace_seconds)
            else:
                return self._error(call, "NOT_FOUND", f"unknown process tool: {call.name}")
            return ToolResult(call.call_id, True, data=dict(data))
        except LookupError as error:
            return self._error(call, "NOT_FOUND", str(error))
        except PermissionError as error:
            return self._error(call, "PERMISSION_DENIED", str(error))
        except (TypeError, ValueError) as error:
            return self._error(call, "INVALID_PARAM", str(error))
        except RuntimeError as error:
            return self._error(call, "TOOL_FAILED", str(error), retryable=True)

    async def _run_command(self, arguments: Mapping[str, Any], control) -> Mapping[str, Any]:
        raw_argv = arguments.get("argv")
        if (
            not isinstance(raw_argv, (list, tuple))
            or not raw_argv
            or any(not isinstance(item, str) or not item or "\x00" in item for item in raw_argv)
        ):
            raise TypeError("argv must be a non-empty array of non-empty strings without NUL bytes")
        cwd = arguments.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd.strip():
            raise TypeError("cwd must be a non-empty workspace-relative string")
        raw_environment = arguments.get("environment", {})
        if not isinstance(raw_environment, Mapping) or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in raw_environment.items()
        ):
            raise TypeError("environment must be an object containing string values")
        mode = arguments.get("mode", "foreground")
        if mode not in {"foreground", "background"}:
            raise ValueError("mode must be foreground or background")
        timeout_seconds = self._bounded_number(
            arguments, "timeout_seconds", 300.0, 0.01, 3600.0
        )
        grace_seconds = self._bounded_number(
            arguments, "termination_grace_seconds", 2.0, 0.0, 30.0
        )
        max_output_bytes = self._bounded_int(
            arguments, "max_output_bytes", 1_000_000, 1, 5_000_000
        )
        max_lifetime_seconds = self._bounded_number(
            arguments, "max_lifetime_seconds", 3600.0, 0.01, 86_400.0
        )
        stop_on_task_end = arguments.get("stop_on_task_end", True)
        if not isinstance(stop_on_task_end, bool):
            raise TypeError("stop_on_task_end must be a boolean")
        return await control.run(
            tuple(raw_argv), cwd=cwd, environment=dict(raw_environment),
            background=mode == "background", timeout_seconds=timeout_seconds,
            termination_grace_seconds=grace_seconds,
            max_output_bytes=max_output_bytes,
            max_lifetime_seconds=max_lifetime_seconds,
            stop_on_task_end=stop_on_task_end,
        )

    @staticmethod
    def _non_empty_string(arguments: Mapping[str, Any], name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _non_negative_int(
        arguments: Mapping[str, Any], name: str, default: int
    ) -> int:
        value = arguments.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TypeError(f"{name} must be a non-negative integer")
        return value

    @staticmethod
    def _bounded_int(
        arguments: Mapping[str, Any], name: str, default: int, minimum: int, maximum: int
    ) -> int:
        value = arguments.get(name, default)
        if (
            not isinstance(value, int) or isinstance(value, bool)
            or value < minimum or value > maximum
        ):
            raise TypeError(f"{name} must be an integer in {minimum}..{maximum}")
        return value

    @staticmethod
    def _bounded_number(
        arguments: Mapping[str, Any], name: str, default: float,
        minimum: float, maximum: float,
    ) -> float:
        value = arguments.get(name, default)
        if (
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or value < minimum or value > maximum
        ):
            raise TypeError(f"{name} must be a number in {minimum:g}..{maximum:g}")
        return float(value)

    @staticmethod
    def _non_negative_number(
        arguments: Mapping[str, Any], name: str, default: float
    ) -> float:
        value = arguments.get(name, default)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < 0
        ):
            raise TypeError(f"{name} must be a non-negative number")
        return float(value)

    @staticmethod
    def _error(
        call: ToolCall, code: str, message: str, retryable: bool = False
    ) -> ToolResult:
        return ToolResult(
            call.call_id, False, error_code=code, message=message,
            retryable=retryable,
        )
