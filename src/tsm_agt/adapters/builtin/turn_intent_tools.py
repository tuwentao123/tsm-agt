from __future__ import annotations

from datetime import datetime
from typing import Any

from tsm_agt.core.single_loop.types import IntentAttemptStatus, TurnIntentKind
from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    ToolCall,
    ToolEffect,
    ToolIdempotency,
    ToolInvocationContext,
    ToolProtocol,
    ToolRecoveryKind,
    ToolResult,
    ToolResultAuthority,
    ToolRisk,
    ToolSpec,
)

_MAX_DEPENDENCIES = 20
_INTENT_VALUES = [item.value for item in TurnIntentKind]
_DEPENDENCY_KINDS = [
    "WORKSPACE_SOURCE",
    "RUNTIME_TASK",
    "RUNTIME_SESSION",
    "EXTERNAL",
]

_TURN_INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": _INTENT_VALUES},
        "dependencies": {
            "type": "array",
            "maxItems": _MAX_DEPENDENCIES,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": _DEPENDENCY_KINDS},
                    "reference": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["kind", "reference"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string", "maxLength": 500},
        "continuation_task_id": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "additionalProperties": False,
}


class CoreTurnIntentToolProvider:
    """Record bounded, auditable high-level intent for the current Agent turn."""

    descriptor = AdapterDescriptor(
        "builtin.core-turn-intent-tools",
        "0.1.0",
        "ToolProviderPort",
        "1.0",
        frozenset({"core.turn_intent"}),
    )

    _tools = (
        ToolSpec(
            "core.turn_intent",
            (
                "Optionally describe the current turn's high-level work before "
                "investigation, modification, verification, or runtime inspection. "
                "Do not declare effects: Runtime derives them from intent and "
                "actual tool execution."
            ),
            _TURN_INTENT_SCHEMA,
            ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=False,
            idempotency=ToolIdempotency.IDEMPOTENT,
            max_result_tokens=800,
            rollback=(
                "declare a corrected stronger intent; prior attempts remain "
                "auditable"
            ),
            is_internal_state=True,
            effect=ToolEffect.INTERNAL,
            result_authority=ToolResultAuthority.RUNTIME_FACT,
            protocol=ToolProtocol.IMMEDIATE,
        ),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "turn intent tools ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools

    async def invoke(
        self,
        call: ToolCall,
        context: ToolInvocationContext,
    ) -> ToolResult:
        if not self._started:
            raise RuntimeError("adapter is not started")
        if call.name != "core.turn_intent":
            return ToolResult(
                call.call_id,
                False,
                error_code="NOT_FOUND",
                message=f"unknown turn intent tool: {call.name}",
            )
        if context.turn_intent_control is None:
            return ToolResult(
                call.call_id,
                False,
                error_code="UNAVAILABLE",
                message="turn intent capability is unavailable",
            )
        attempt = await context.turn_intent_control.record_attempt(
            call.call_id,
            call.arguments,
        )
        if attempt["status"] == IntentAttemptStatus.REJECTED.value:
            validation_error = attempt.get("validation_error")
            error_data = (
                dict(validation_error)
                if isinstance(validation_error, dict) else {}
            )
            return ToolResult(
                call.call_id,
                False,
                data={
                    "status": "invalid_param",
                    "call_id": call.call_id,
                    **error_data,
                    "retry_safe": True,
                    "side_effects_started": False,
                    "obligation_status": "recoverable_intent_attempt_recorded",
                    "attempt_id": attempt["attempt_id"],
                },
                error_code="INVALID_PARAM",
                message=str(error_data.get("message") or "invalid turn intent"),
                hint="Correct the intent fields and call core.turn_intent again.",
                retryable=True,
                recovery_kind=ToolRecoveryKind.RETRY_SAME,
                recovery_action={"tool": "core.turn_intent"},
                meta={"runtime_internal_state": True},
            )
        return ToolResult(
            call.call_id,
            True,
            data=dict(attempt),
            meta={"runtime_internal_state": True},
        )
