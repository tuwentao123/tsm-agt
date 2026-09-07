"""Provider-neutral contracts for choosing the next exploration move."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .adapter import RuntimeAdapter
from .tool import ToolCall


class StopOrPivotAction(StrEnum):
    CONTINUE = "CONTINUE"
    READ_HITS = "READ_HITS"
    NARROW_SCOPE = "NARROW_SCOPE"
    CHANGE_METHOD = "CHANGE_METHOD"
    ASK_USER = "ASK_USER"
    SUMMARIZE_WITH_EVIDENCE = "SUMMARIZE_WITH_EVIDENCE"
    STOP_NO_PROGRESS = "STOP_NO_PROGRESS"


@dataclass(frozen=True, slots=True)
class StopOrPivotSignals:
    semantic_family: str = ""
    semantic_signature: str = ""
    read_hits_required: bool = False
    candidate_count: int = 0
    scope_reason_required: bool = False
    scope_relation: str = "unknown"
    budget_wrap_up: bool = False
    budget_reason: str = ""
    low_value_streak: int = 0
    plan_should_stop: bool = False
    plan_finished: bool = False
    consecutive_no_progress: int = 0
    remaining_model_calls: int = 0
    remaining_tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class StopOrPivotState:
    last_decision: str = ""
    blocked_semantic_signature: str = ""
    change_method_attempts: int = 0

    @classmethod
    def from_data(cls, data: Mapping[str, Any] | None) -> StopOrPivotState:
        if not data:
            return cls()
        attempts = int(data.get("change_method_attempts", 0))
        if attempts < 0:
            raise ValueError("change_method_attempts must not be negative")
        return cls(
            str(data.get("last_decision", "")),
            str(data.get("blocked_semantic_signature", "")),
            attempts,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "last_decision": self.last_decision,
            "blocked_semantic_signature": self.blocked_semantic_signature,
            "change_method_attempts": self.change_method_attempts,
        }


@dataclass(frozen=True, slots=True)
class StopOrPivotDecision:
    action: StopOrPivotAction
    reason: str
    terminal: bool = False
    candidate_count: int = 0
    low_value_streak: int = 0


@dataclass(frozen=True, slots=True)
class StopOrPivotUpdate:
    decision: StopOrPivotDecision
    state: StopOrPivotState


class StopOrPivotPolicyPort(RuntimeAdapter, Protocol):
    async def decide(
        self, call: ToolCall, signals: StopOrPivotSignals,
        state: StopOrPivotState,
    ) -> StopOrPivotUpdate: ...
