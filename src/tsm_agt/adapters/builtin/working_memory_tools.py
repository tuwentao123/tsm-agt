"""Built-in Task scratchpad tools backed only by a narrow Kernel capability."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ToolCall,
    ToolIdempotency, ToolInvocationContext, ToolResult, ToolRisk, ToolSpec,
)


_STRING_ARRAY = {
    "type": "array", "items": {"type": "string"}, "maxItems": 50,
}
_STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "constraints": _STRING_ARRAY, "facts": _STRING_ARRAY,
        "decisions": _STRING_ARRAY,
        "hypotheses": _STRING_ARRAY, "open_questions": _STRING_ARRAY,
        "plan": {
            "type": "array", "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["PENDING", "IN_PROGRESS", "COMPLETED", "BLOCKED"],
                    },
                    "completion_criteria": {"type": "string"},
                },
                "required": [
                    "step_id", "description", "status", "completion_criteria"
                ],
                "additionalProperties": False,
            },
        },
        "completed_work": _STRING_ARRAY, "remaining_work": _STRING_ARRAY,
        "evidence": {
            "type": "array", "maxItems": 50,
            "items": {
                "type": "object",
                "properties": {
                    "evidence_id": {"type": "string"},
                    "kind": {"type": "string"},
                    "reference": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["evidence_id", "kind", "reference", "summary"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "goal", "constraints", "facts", "decisions", "hypotheses",
        "open_questions", "plan", "completed_work",
        "remaining_work", "evidence",
    ],
    "additionalProperties": False,
}


class CoreWorkingMemoryToolProvider:
    descriptor = AdapterDescriptor(
        "builtin.core-working-memory-tools", "0.1.0",
        "ToolProviderPort", "1.0",
        frozenset({"core.working_memory_read", "core.working_memory_update"}),
    )

    _tools = (
        ToolSpec(
            "core.working_memory_read",
            "Read the current Task scratchpad: goal, constraints, concise facts, "
            "decisions, hypotheses, open questions, plan, progress, and evidence references. "
            "This is inspectable temporary state, not durable user/project memory.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        ),
        ToolSpec(
            "core.working_memory_update",
            "Replace the current Task scratchpad using its revision. Keep only concise, "
            "user-inspectable conclusions and progress. Never write chain-of-thought, "
            "credentials, raw logs, source file bodies, or durable preferences. Facts "
            "are confirmed observations; uncertain ideas belong in hypotheses. Evidence "
            "must reference an existing event:<seq>, tool_call:<id>, or mutation:<id>."
            " A new Task starts at expected_revision=1; after any update, use the "
            "revision returned by this tool or core.working_memory_read.",
            {
                "type": "object",
                "properties": {
                    "expected_revision": {"type": "integer"},
                    "state": _STATE_SCHEMA,
                    "operation_id": {"type": "string"},
                },
                "required": ["expected_revision", "state", "operation_id"],
                "additionalProperties": False,
            },
            ToolRisk.R0, False, False, ToolIdempotency.KEYED,
            rollback="append a corrected working-memory revision; history remains auditable",
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
            "working memory tools ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext,
    ) -> ToolResult:
        if not self._started:
            raise RuntimeError("adapter is not started")
        control = context.working_memory_control
        if control is None:
            return ToolResult(
                call.call_id, False, error_code="UNAVAILABLE",
                message="working memory capability is unavailable",
            )
        try:
            if call.name == "core.working_memory_read":
                data = await control.read()
            elif call.name == "core.working_memory_update":
                state = call.arguments["state"]
                if not isinstance(state, Mapping):
                    raise ValueError("working memory state must be an object")
                data = await control.update(
                    int(call.arguments["expected_revision"]), state,
                    str(call.arguments["operation_id"]),
                )
            else:
                return ToolResult(
                    call.call_id, False, error_code="NOT_FOUND",
                    message=f"unknown working memory tool: {call.name}",
                )
            return ToolResult(
                call.call_id, True, data=data,
                meta={"runtime_internal_state": True},
            )
        except (KeyError, TypeError, ValueError) as error:
            return ToolResult(
                call.call_id, False, error_code="INVALID_PARAM", message=str(error)
            )
        except LookupError as error:
            return ToolResult(
                call.call_id, False, error_code="NOT_FOUND", message=str(error)
            )
