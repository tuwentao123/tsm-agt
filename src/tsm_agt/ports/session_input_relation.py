"""Replaceable judgement of how one user input relates to Session history."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .adapter import RuntimeAdapter


class SessionInputRelation(StrEnum):
    """How one user input relates to the Session's existing work.

    The judgement only describes the relation; Runtime decides and applies the
    action. Implementations never perform, authorize, or mutate anything.
    """

    #: Continue / add to the Task that is currently running (append, no interrupt).
    SUPPLEMENT = "SUPPLEMENT"
    #: Replace the running Task's goal (re-sign its contract).
    REPLACE = "REPLACE"
    #: Unrelated: stop the running Task and start a new one.
    UNRELATED = "UNRELATED"
    #: Only asks about status; no work changes.
    STATUS_QUERY = "STATUS_QUERY"
    #: Standalone question that can be answered directly.
    ANSWER = "ANSWER"
    #: Cannot tell. Callers fall back to the safe default (SUPPLEMENT).
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SessionInputRelationJudgement:
    relation: SessionInputRelation
    confidence: float = 0.0
    reason_code: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("relation confidence must be within 0..1")

    def to_data(self) -> dict[str, Any]:
        return {
            "relation": self.relation.value,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
        }


class SessionInputRelationPort(RuntimeAdapter, Protocol):
    """Judge the relation between one input and Session history, nothing else."""

    async def judge_input_relation(
        self, text: str, context: Mapping[str, Any],
    ) -> SessionInputRelationJudgement: ...
