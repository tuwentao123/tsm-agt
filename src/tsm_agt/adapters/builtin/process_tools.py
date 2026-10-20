"""Built-in task-scoped background process management tools."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import isfinite
from typing import Any

from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    BackgroundProcessStartResult,
    HealthState,
    HealthStatus,
    ModelToolContract,
    ToolCall,
    ToolEffect,
    ToolFactCapability,
    ToolFactDescriptor,
    ToolIdempotency,
    ToolInvocationContext,
    ToolRecoveryKind,
    ToolResult,
    ToolResultAuthority,
    ToolResultMaterialization,
    ToolRisk,
    ToolSpec,
)


class _BoundedNumberError(ValueError):
    """A mechanical argument constraint that the caller can safely correct."""

    def __init__(self, name: str, minimum: float, maximum: float, value: Any) -> None:
        super().__init__(f"{name} must be a number in {minimum:g}..{maximum:g}")
        self.recovery_action: dict[str, Any] = {
            "field": name,
            "minimum": minimum,
            "maximum": maximum,
        }
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.recovery_action["received"] = value
        else:
            self.recovery_action["received_type"] = type(value).__name__


class CoreProcessToolProvider:
    """Expose process management without depending on Kernel or OS details."""

    descriptor = AdapterDescriptor(
        adapter_id="builtin.core-process-tools",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset(
            {
                "core.run_command", "core.process_status", "core.process_list",
                "core.process_logs", "core.process_stop", "core.process_wait",
            }
        ),
    )

    _tools = (
        ToolSpec(
            name="core.run_command",
            description=(
                "Run a command as a structured argv array inside the task workspace. "
                "No shell parses the arguments. Every command is started as a Runtime-"
                "managed process and returns promptly with a process_id and its current "
                "state. Use core.process_wait only when a later step needs a bounded "
                "wait for exit; use status or logs to observe a running process. "
                "Prefer web.search and web.fetch_markdown for public web pages. "
                "Reading another project's source is a normal use of this tool. "
                "With the gh CLI, gh api -H 'Accept: application/vnd.github.raw' "
                "repos/<owner>/<repo>/contents/<path> reads one file in place, and "
                "git clone brings a whole repository in; prefer either over guessing "
                "repository paths through the web tools. A shell executable is "
                "denied, so pass the program and its arguments directly and inspect "
                "files with core.list_files, core.read_file, or core.search_files. "
                "The environment starts minimal and rejects credential-like variable names. "
                "A successful ToolResult proves only that Runtime started and supervises "
                "the process. data.state='RUNNING' is neither service readiness nor a "
                "terminal command result. If data.state is terminal, inspect exit_code "
                "and succeeded. Runtime, not this call, assigns the process resource "
                "lease and reports its deadline_at. This lease is not a wait duration. "
                "Bringing a public repository in with git clone, git fetch, or "
                "git ls-remote is allowed without extra approval under a trusted "
                "project; other commands require approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": "Executable followed by literal arguments; no shell syntax."},
                    "cwd": {"type": "string", "description": "Required workspace-relative working directory."},
                    "environment": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Optional non-secret environment overrides."},
                    "max_output_bytes": {"type": "integer", "minimum": 1, "maximum": 5000000, "description": "Retained stdout/stderr byte limit; defaults to 1000000."},
                },
                # Directory is part of the command identity: it must be visible
                # to the model, approval flow, execution record, and process
                # launcher.  Do not silently turn an omitted directory into the
                # workspace root.
                "required": ["argv", "cwd"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R2,
            is_read_only=False,
            is_concurrency_safe=False,
            idempotency=ToolIdempotency.NON_IDEMPOTENT,
            rollback=(
                "stop a returned managed process; filesystem or external side effects "
                "require separate inspection and rollback"
            ),
            effect=ToolEffect.EXECUTE,
            result_authority=ToolResultAuthority.PROCESS_FACT,
            model_contract=ModelToolContract(
                purpose="Start one structured command under Runtime process supervision.",
                use_when=("a local command is needed to inspect, build, test, or operate the current task",),
                avoid_when=("you only need public web content; use web tools", "you need shell parsing; it is not available"),
                returns=("a managed process receipt and current state", "RUNNING proves neither service readiness nor command success"),
            ),
        ),
        ToolSpec(
            name="core.process_list",
            description=("List managed processes accessible to the current session or task, including their origin turn, working directory, current state and control availability. By default lists RUNNING and ORPHANED; pass an empty states array to include all states. Results use an offset cursor and may shift while processes change. Use process IDs to inspect logs or request a stop. RUNNING does not prove service readiness. ORPHANED means the runtime cannot prove control, not that the OS process exited."),
            parameters={"type": "object", "properties": {
                "scope": {"type": "string", "enum": ["session", "task"]},
                "states": {"type": "array", "items": {"type": "string", "enum": ["RUNNING", "EXITED", "CANCELLED", "TIMED_OUT", "ORPHANED"]}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "cursor": {"type": "integer", "minimum": 0}}, "additionalProperties": False},
            risk=ToolRisk.R0, is_read_only=True, is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT, effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.PROCESS_FACT,
        ),
        ToolSpec(
            name="core.process_wait",
            description=(
                "Wait once, for a bounded number of seconds, for one session-owned "
                "managed process. wait_seconds must be from 0.01 through 30 and is "
                "one observation window, not the process resource lease. For a longer "
                "exit-bound task, make later wait calls with windows no greater than 30. "
                "If it has not exited at the end of the window, it "
                "remains running and this tool returns its current logs and state; it "
                "never terminates the process on wait expiry. Use this only when a "
                "subsequent step needs the exit result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "process_id": {"type": "string"},
                    "wait_seconds": {
                        "type": "number", "minimum": 0.01, "maximum": 30,
                    },
                },
                "required": ["process_id", "wait_seconds"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.PROCESS_FACT,
            model_contract=ModelToolContract(
                purpose="Observe one managed process through a bounded wait window.",
                use_when=("a later step needs to know whether a process has exited",),
                avoid_when=("the process is not managed by this session",),
                returns=("current state and bounded logs", "a timeout leaves the process running and is not a failure by itself"),
            ),
        ),
        ToolSpec(
            name="core.process_status",
            description=(
                "Get the current state of one managed process owned by this session. "
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
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.PROCESS_FACT,
        ),
        ToolSpec(
            name="core.process_logs",
            description=(
                "Read retained stdout and stderr from one session-owned managed "
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
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.PROCESS_FACT,
        ),
        ToolSpec(
            name="core.process_stop",
            description=(
                "Stop one managed process owned by this session. The supervisor sends "
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
            effect=ToolEffect.CONTROL,
            result_authority=ToolResultAuthority.RUNTIME_FACT,
        ),
    )

    def __init__(
        self, *, managed_process_max_lifetime_seconds: float = 3600.0,
        stop_managed_process_on_task_end: bool = True,
    ) -> None:
        if (
            not isinstance(managed_process_max_lifetime_seconds, (int, float))
            or isinstance(managed_process_max_lifetime_seconds, bool)
            or not isfinite(managed_process_max_lifetime_seconds)
            or not 0 < float(managed_process_max_lifetime_seconds) <= 86_400
        ):
            raise ValueError(
                "managed_process_max_lifetime_seconds must be finite and in 0..86400"
            )
        if not isinstance(stop_managed_process_on_task_end, bool):
            raise TypeError("stop_managed_process_on_task_end must be a boolean")
        self._started = False
        self._managed_process_max_lifetime_seconds = float(
            managed_process_max_lifetime_seconds
        )
        self._stop_managed_process_on_task_end = stop_managed_process_on_task_end

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
                "this runtime did not grant process management",
            )
        try:
            if call.name == "core.run_command":
                command = await self._run_command(call.arguments, control)
                data = command.to_data()
            elif call.name == "core.process_list":
                scope = call.arguments.get("scope", "session")
                states = call.arguments.get("states", ["RUNNING", "ORPHANED"])
                if scope not in ("session", "task"):
                    raise ValueError("scope must be session or task")
                allowed = {"RUNNING", "EXITED", "CANCELLED", "TIMED_OUT", "ORPHANED"}
                if not isinstance(states, list) or any(not isinstance(s, str) or s not in allowed for s in states):
                    raise ValueError("states must contain valid process states")
                limit = self._bounded_int(call.arguments, "limit", 50, 1, 100)
                cursor = self._non_negative_int(call.arguments, "cursor", 0)
                data = await control.list_processes(scope, tuple(states), limit, cursor)
            elif call.name == "core.process_status":
                process_id = self._non_empty_string(call.arguments, "process_id")
                data = await control.status(process_id)
            elif call.name == "core.process_wait":
                process_id = self._non_empty_string(call.arguments, "process_id")
                wait_seconds = self._bounded_number(
                    call.arguments, "wait_seconds", 0.0, 0.01, 30.0
                )
                data = (await control.wait(process_id, wait_seconds)).to_data()
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
            return ToolResult(
                call.call_id,
                True,
                data=dict(data),
                fact_descriptor=self._fact_descriptor(call, context, data),
            )
        except LookupError as error:
            return self._error(call, "NOT_FOUND", str(error))
        except PermissionError as error:
            return self._error(call, "PERMISSION_DENIED", str(error))
        except _BoundedNumberError as error:
            return self._error(
                call, "INVALID_PARAM", str(error), retryable=True,
                recovery_kind=ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS,
                recovery_action=error.recovery_action,
            )
        except (TypeError, ValueError) as error:
            return self._error(call, "INVALID_PARAM", str(error))
        except RuntimeError as error:
            return self._error(call, "TOOL_FAILED", str(error), retryable=True)
        except FileNotFoundError as error:
            return self._error(
                call, "EXECUTABLE_NOT_FOUND", str(error),
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
                recovery_action={"execution_state": "not_started"},
            )
        except OSError as error:
            return self._error(
                call, "PROCESS_START_FAILED", str(error),
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
                recovery_action={"execution_state": "not_started"},
            )

    async def _run_command(
        self, arguments: Mapping[str, Any], control,
    ) -> BackgroundProcessStartResult:
        raw_argv = arguments.get("argv")
        if (
            not isinstance(raw_argv, (list, tuple))
            or not raw_argv
            or any(not isinstance(item, str) or not item or "\x00" in item for item in raw_argv)
        ):
            raise TypeError("argv must be a non-empty array of non-empty strings without NUL bytes")
        cwd = arguments.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            raise TypeError("cwd must be a non-empty workspace-relative string")
        raw_environment = arguments.get("environment", {})
        if not isinstance(raw_environment, Mapping) or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in raw_environment.items()
        ):
            raise TypeError("environment must be an object containing string values")
        max_output_bytes = self._bounded_int(
            arguments, "max_output_bytes", 1_000_000, 1, 5_000_000
        )
        return await control.run(
            tuple(raw_argv), cwd=cwd, environment=dict(raw_environment),
            max_output_bytes=max_output_bytes,
            max_lifetime_seconds=self._managed_process_max_lifetime_seconds,
            stop_on_task_end=self._stop_managed_process_on_task_end,
        )

    @staticmethod
    def _fact_descriptor(
        call: ToolCall,
        context: ToolInvocationContext,
        data: Mapping[str, Any],
    ) -> ToolFactDescriptor:
        process_id = data.get("process_id")
        process_ref = (
            f"process:{process_id}"
            if isinstance(process_id, str) and process_id else None
        )
        if call.name == "core.run_command":
            capabilities: tuple[ToolFactCapability, ...] = (
                ToolFactCapability.REPORT_ACTION,
            )
            # A fast command may have exited before the start result reached
            # the model.  That additional state is observable, but still is
            # not a test/acceptance verification without an explicit contract.
            if data.get("execution_state") in {"succeeded", "failed_known"}:
                capabilities += (ToolFactCapability.REPORT_RUNTIME_STATE,)
            materialization = ToolResultMaterialization.ACTION_RECEIPT
        elif call.name == "core.process_stop":
            materialization = ToolResultMaterialization.ACTION_RECEIPT
            capabilities = (ToolFactCapability.REPORT_ACTION,)
        else:
            materialization = ToolResultMaterialization.RUNTIME_OBSERVATION
            capabilities = (ToolFactCapability.REPORT_RUNTIME_STATE,)
        return ToolFactDescriptor(
            authority="managed_process",
            scope=tuple(
                value for value in (
                    f"task:{context.task_id}",
                    f"turn:{context.turn_id}",
                    process_ref,
                ) if value is not None
            ),
            artifact_refs=((process_ref,) if process_ref is not None else ()),
            materialization=materialization,
            capabilities=capabilities,
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
            or not isfinite(float(value))
            or value < minimum or value > maximum
        ):
            raise _BoundedNumberError(name, minimum, maximum, value)
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
        call: ToolCall, code: str, message: str, retryable: bool = False,
        recovery_kind: ToolRecoveryKind = ToolRecoveryKind.NONE,
        recovery_action: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return ToolResult(
            call.call_id, False, error_code=code, message=message,
            retryable=retryable, recovery_kind=recovery_kind,
            recovery_action=dict(recovery_action or {}),
        )
