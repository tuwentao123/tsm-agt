"""Provider-neutral contracts for evidence-aware exploration budgets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .adapter import RuntimeAdapter
from .evidence_delta import EvidenceDelta
from .semantic_action import SemanticAction
from .tool import ToolCall, ToolResult


class ExplorationBudgetAction(StrEnum):
    CONTINUE = "CONTINUE"
    FOCUS = "FOCUS"
    WRAP_UP = "WRAP_UP"


@dataclass(frozen=True, slots=True)
class ExplorationBudgetState:
    scored_actions: int = 0
    cumulative_tool_milliseconds: int = 0
    total_new_evidence: int = 0
    low_value_streak: int = 0
    broad_searches: int = 0
    repeated_semantic_actions: int = 0
    semantic_counts: Mapping[str, int] = field(default_factory=dict)
    focus_mode: bool = False
    focus_reason: str = ""

    def __post_init__(self) -> None:
        values = (
            self.scored_actions, self.cumulative_tool_milliseconds,
            self.total_new_evidence, self.low_value_streak,
            self.broad_searches, self.repeated_semantic_actions,
        )
        if any(value < 0 for value in values):
            raise ValueError("exploration budget counters must not be negative")
        if any(not key or value < 1 for key, value in self.semantic_counts.items()):
            raise ValueError("semantic budget counters must be positive")

    @classmethod
    def from_data(
        cls, data: Mapping[str, Any] | None
    ) -> ExplorationBudgetState:
        if not data:
            return cls()
        raw_counts = data.get("semantic_counts", {})
        if not isinstance(raw_counts, Mapping):
            raise ValueError("semantic_counts must be an object")
        return cls(
            scored_actions=int(data.get("scored_actions", 0)),
            cumulative_tool_milliseconds=int(
                data.get("cumulative_tool_milliseconds", 0)
            ),
            total_new_evidence=int(data.get("total_new_evidence", 0)),
            low_value_streak=int(data.get("low_value_streak", 0)),
            broad_searches=int(data.get("broad_searches", 0)),
            repeated_semantic_actions=int(
                data.get("repeated_semantic_actions", 0)
            ),
            semantic_counts={str(key): int(value) for key, value in raw_counts.items()},
            focus_mode=bool(data.get("focus_mode", False)),
            focus_reason=str(data.get("focus_reason", "")),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "scored_actions": self.scored_actions,
            "cumulative_tool_milliseconds": (
                self.cumulative_tool_milliseconds
            ),
            "total_new_evidence": self.total_new_evidence,
            "low_value_streak": self.low_value_streak,
            "broad_searches": self.broad_searches,
            "repeated_semantic_actions": self.repeated_semantic_actions,
            "semantic_counts": dict(sorted(self.semantic_counts.items())),
            "focus_mode": self.focus_mode,
            "focus_reason": self.focus_reason,
        }


@dataclass(frozen=True, slots=True)
class ExplorationBudgetProbe:
    remaining_model_calls: int
    remaining_tool_calls: int
    max_model_calls: int
    max_tool_calls: int


@dataclass(frozen=True, slots=True)
class ExplorationBudgetObservation:
    elapsed_milliseconds: int
    broad_search: bool = False
    scope_expanded: bool = False


@dataclass(frozen=True, slots=True)
class ExplorationBudgetDecision:
    action: ExplorationBudgetAction
    reason: str
    score: int = 0
    low_value_streak: int = 0
    cumulative_tool_milliseconds: int = 0
    scored_actions: int = 0
    used_tool_calls: int = 0
    max_scored_actions: int = 0
    max_total_tool_calls: int = 0
    max_cumulative_tool_milliseconds: int = 0
    max_low_value_streak: int = 0
    reserve_tool_calls: int = 0
    focus_allows_call: bool = False


@dataclass(frozen=True, slots=True)
class ExplorationBudgetUpdate:
    state: ExplorationBudgetState
    score: int
    value_band: str
    new_evidence: int
    semantic_repeat: bool
    elapsed_milliseconds: int
    max_scored_actions: int = 0
    max_total_tool_calls: int = 0
    max_cumulative_tool_milliseconds: int = 0
    max_low_value_streak: int = 0
    reserve_tool_calls: int = 0


class ExplorationBudgetPolicyPort(RuntimeAdapter, Protocol):
    async def before_call(
        self, call: ToolCall, semantic_action: SemanticAction | None,
        probe: ExplorationBudgetProbe, state: ExplorationBudgetState,
    ) -> ExplorationBudgetDecision: ...

    async def after_result(
        self, call: ToolCall, result: ToolResult,
        semantic_action: SemanticAction | None, evidence_delta: EvidenceDelta | None,
        observation: ExplorationBudgetObservation, state: ExplorationBudgetState,
    ) -> ExplorationBudgetUpdate: ...
