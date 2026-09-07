"""Read-only, redacted investigation status contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .adapter import RuntimeAdapter


@dataclass(frozen=True, slots=True)
class InvestigationStatusSignals:
    question_ref: str = ""
    evidence_counts: Mapping[str, int] = field(default_factory=dict)
    consecutive_zero_delta: int = 0
    scored_actions: int = 0
    budget_score: int | None = None
    value_band: str = ""
    low_value_streak: int = 0
    cumulative_tool_milliseconds: int = 0
    latest_decision: str = ""
    model_calls: int = 0
    tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class InvestigationStatusProjection:
    question_ref: str
    evidence_counts: Mapping[str, int]
    evidence_total: int
    consecutive_zero_delta: int
    scored_actions: int
    budget_score: int | None
    value_band: str
    low_value_streak: int
    cumulative_tool_milliseconds: int
    latest_decision: str
    model_calls: int
    tool_calls: int

    def to_data(self) -> dict[str, object]:
        return {
            "question_ref": self.question_ref,
            "evidence_counts": dict(self.evidence_counts),
            "evidence_total": self.evidence_total,
            "consecutive_zero_delta": self.consecutive_zero_delta,
            "scored_actions": self.scored_actions,
            "budget_score": self.budget_score,
            "value_band": self.value_band,
            "low_value_streak": self.low_value_streak,
            "cumulative_tool_milliseconds": self.cumulative_tool_milliseconds,
            "latest_decision": self.latest_decision,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
        }


class InvestigationStatusProjectorPort(RuntimeAdapter, Protocol):
    async def project(
        self, signals: InvestigationStatusSignals
    ) -> InvestigationStatusProjection: ...
