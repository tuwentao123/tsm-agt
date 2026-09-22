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


class SessionAnswerRequiresTask(RuntimeError):
    """A direct Session answer cannot be produced without Tool capability.

    Routing decides ANSWER before anything is attempted, so the decision rests
    on the router's reading of one message. This is the opposite: the model has
    already seen the full request, holds no tools, and reports that the request
    cannot be answered that way. That is execution-time evidence the route was
    wrong, so the caller must re-route the same message into a Task instead of
    handing the user an apology.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason.strip()
        super().__init__(
            "session answer requires an Agent Task"
            + (f": {self.reason}" if self.reason else "")
        )


@dataclass(frozen=True, slots=True)
class TurnResult:
    turn_id: str
    task_id: str
    assistant_message: Message
    finish_reason: FinishReason
    usage: ModelUsage
