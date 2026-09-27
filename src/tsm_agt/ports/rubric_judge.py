"""Replaceable, bounded judge for a judgement (`rubric`) acceptance criterion.

A ``rubric`` criterion has no machine proof, so it can never be verified by the
reference verifier or closed by a tool. It is evaluated here instead, under a
bounded attempt budget, and it always terminates: an unavailable or unparseable
judge is fail-closed, never an infinite wait.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .adapter import RuntimeAdapter


class JudgeVerdict(StrEnum):
    """Outcome of one rubric judgement."""

    SATISFIED = "satisfied"          # 满足
    NEEDS_REVISION = "needs_revision"  # 不满足，可返工
    UNDECIDABLE = "undecidable"      # 判不了（量表不可评估）
    JUDGE_ERROR = "judge_error"      # 评审员自身出错（fail-closed）


@dataclass(frozen=True, slots=True)
class RubricJudgement:
    criterion_id: str
    verdict: JudgeVerdict
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict is JudgeVerdict.SATISFIED

    def to_data(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "verdict": self.verdict.value,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RubricEvidence:
    """One bounded piece of proof a judge may read.

    A bare reference such as ``event:123`` is opaque to a judge, so judging on
    references alone is blind. The excerpt carries the bounded body the judge
    needs while the reference keeps the claim traceable to durable state.
    """

    reference: str
    excerpt: str

    def to_data(self) -> dict[str, Any]:
        return {"reference": self.reference, "excerpt": self.excerpt}


class RubricJudgePort(RuntimeAdapter, Protocol):
    """Judge one subjective criterion without executing tools or changing state."""

    async def judge(
        self, criterion_id: str, assertion: str,
        evidence: tuple[RubricEvidence, ...],
    ) -> RubricJudgement: ...
