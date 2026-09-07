"""A completed model turn exposed by the microkernel."""

from __future__ import annotations

from dataclasses import dataclass

from tsm_agt.ports import FinishReason, Message, ModelUsage


class InvalidTurnState(ValueError):
    pass


class ModelInvocationFailed(RuntimeError):
    def __init__(self, turn_id: str, message: str) -> None:
        super().__init__(f"model invocation failed for {turn_id}: {message}")
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
