"""A completed model turn exposed by the microkernel."""

from __future__ import annotations

from dataclasses import dataclass

from tsm_agt.ports import (
    FinishReason, Message, ModelFailureCategory, ModelRecoveryAction,
    ModelRetrySafety, ModelUsage,
)


class InvalidTurnState(ValueError):
    pass


class ModelInvocationFailed(RuntimeError):
    def __init__(
        self, turn_id: str, message: str, *, failure_kind: str = "provider",
        failure_category: ModelFailureCategory = ModelFailureCategory.UNKNOWN,
        retry_safety: ModelRetrySafety = ModelRetrySafety.NEVER,
        recovery_action: ModelRecoveryAction = ModelRecoveryAction.FAIL_TERMINAL,
        diagnostic_detail: str = "",
    ) -> None:
        self.failure_kind = failure_kind
        self.failure_category = failure_category
        self.retry_safety = retry_safety
        self.recovery_action = recovery_action
        self.diagnostic_detail = diagnostic_detail
        label = (
            "model tool protocol failed"
            if failure_kind == "tool_protocol"
            else "model invocation failed"
        )
        super().__init__(f"{label} for {turn_id}: {message}")
        self.turn_id = turn_id


class InvalidModelResponse(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TurnResult:
    turn_id: str
    task_id: str
    assistant_message: Message
    finish_reason: FinishReason
    usage: ModelUsage
