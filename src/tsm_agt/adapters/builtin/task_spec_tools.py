"""Built-in tools for an inspectable, revisioned Task SPEC."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ToolCall,
    ToolIdempotency, ToolInvocationContext, ToolResult, ToolRisk, ToolSpec,
)


class CoreTaskSpecToolProvider:
    descriptor = AdapterDescriptor(
        "builtin.core-task-spec-tools", "0.1.0",
        "ToolProviderPort", "1.0",
        frozenset({"core.task_spec_read", "core.task_spec_update"}),
    )
    _tools = (
        ToolSpec(
            "core.task_spec_read",
            "Read the current Task completion contract: immutable user goal, "
            "scope, constraints, and verifiable acceptance criteria.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        ),
        ToolSpec(
            "core.task_spec_update",
            "Revise scope, constraints, and acceptance criteria using the current "
            "revision. This cannot change the user goal. Use only verification kinds "
            "the Runtime can prove: workspace_integrity, post_mutation_command, or "
            "evidence_reference with an existing event:/tool_call:/mutation: reference.",
            {
                "type": "object",
                "properties": {
                    "expected_revision": {"type": "integer"},
                    "scope": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                    "constraints": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                    "acceptance_criteria": {
                        "type": "array", "minItems": 1, "maxItems": 30,
                        "items": {
                            "type": "object",
                            "properties": {
                                "criterion_id": {"type": "string"},
                                "description": {"type": "string"},
                                "verification_kind": {
                                    "type": "string",
                                    "enum": ["workspace_integrity", "post_mutation_command", "evidence_reference"],
                                },
                                "evidence_reference": {"type": ["string", "null"]},
                            },
                            "required": ["criterion_id", "description", "verification_kind"],
                            "additionalProperties": False,
                        },
                    },
                    "operation_id": {"type": "string"},
                },
                "required": ["expected_revision", "scope", "constraints", "acceptance_criteria", "operation_id"],
                "additionalProperties": False,
            },
            ToolRisk.R0, False, False, ToolIdempotency.KEYED,
            rollback="append a corrected Task SPEC revision",
            is_internal_state=True,
        ),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "Task SPEC tools ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools

    async def invoke(self, call: ToolCall, context: ToolInvocationContext) -> ToolResult:
        control = context.task_spec_control
        if control is None:
            return ToolResult(call.call_id, False, error_code="UNAVAILABLE", message="Task SPEC capability is unavailable")
        try:
            if call.name == "core.task_spec_read":
                data = await control.read()
            elif call.name == "core.task_spec_update":
                raw = call.arguments["acceptance_criteria"]
                if not isinstance(raw, (list, tuple)) or not all(isinstance(item, Mapping) for item in raw):
                    raise ValueError("acceptance_criteria must be an object array")
                data = await control.update(
                    int(call.arguments["expected_revision"]),
                    tuple(str(item) for item in call.arguments["scope"]),
                    tuple(str(item) for item in call.arguments["constraints"]),
                    tuple(raw), str(call.arguments["operation_id"]),
                )
            else:
                return ToolResult(call.call_id, False, error_code="NOT_FOUND", message="unknown Task SPEC tool")
            return ToolResult(call.call_id, True, data=data, meta={"runtime_internal_state": True})
        except (KeyError, TypeError, ValueError) as error:
            return ToolResult(call.call_id, False, error_code="INVALID_PARAM", message=str(error))
