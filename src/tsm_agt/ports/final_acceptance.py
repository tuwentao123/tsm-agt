"""Replaceable contract for final evidence-integrity verification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .adapter import RuntimeAdapter


class FinalAcceptanceAction(StrEnum):
    PASS = "PASS"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class FinalQuestionEvidence:
    """One tool-bound question and the trustworthiness of its sources."""

    question_ref: str
    status: str
    expected_scope: str = ""
    trusted_source_references: tuple[str, ...] = ()
    wrong_scope_source_references: tuple[str, ...] = ()
    blocking_reason: str = ""


@dataclass(frozen=True, slots=True)
class FinalAcceptanceProbe:
    """Persisted facts only; it contains no model reasoning or source text."""

    questions: tuple[FinalQuestionEvidence, ...] = ()
    required_plan_step_refs: tuple[str, ...] = ()
    completion_gap_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FinalAcceptanceViolation:
    code: str
    subject_ref: str
    observed: str

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.subject_ref.strip():
            raise ValueError("final acceptance violation identity is required")
        if not self.observed.strip():
            raise ValueError("final acceptance violation observation is required")


@dataclass(frozen=True, slots=True)
class FinalAcceptanceDecision:
    action: FinalAcceptanceAction
    reason: str
    violations: tuple[FinalAcceptanceViolation, ...] = ()


class FinalAcceptancePolicyPort(RuntimeAdapter, Protocol):
    """Judge final evidence integrity without executing tools or changing state."""

    async def evaluate(
        self, probe: FinalAcceptanceProbe,
    ) -> FinalAcceptanceDecision: ...
