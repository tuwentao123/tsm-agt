"""Provider-neutral redacted progress projections for user interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .adapter import RuntimeAdapter


@dataclass(frozen=True, slots=True)
class AgentProgressSignals:
    phase: str
    semantic_family: str = ""
    question_ref: str = ""
    scope_kind: str = "unknown"
    scope_relation: str = "unknown"
    scope_depth: int = 0
    evidence_delta: int | None = None
    consecutive_zero_delta: int = 0
    budget_score: int | None = None
    value_band: str = ""
    next_action: str = ""
    decision_reason: str = ""


@dataclass(frozen=True, slots=True)
class AgentProgressProjection:
    phase: str
    activity: str = ""
    question_ref: str = ""
    scope: str = "unknown"
    scope_change: str = "unknown"
    evidence_delta: int | None = None
    consecutive_zero_delta: int = 0
    budget_score: int | None = None
    value_band: str = ""
    next_action: str = ""
    reason: str = ""


class AgentProgressProjectorPort(RuntimeAdapter, Protocol):
    async def project(
        self, signals: AgentProgressSignals
    ) -> AgentProgressProjection: ...
