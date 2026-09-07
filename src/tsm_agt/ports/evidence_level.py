"""Provider-neutral evidence confidence assessment contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .adapter import RuntimeAdapter


class EvidenceLevel(StrEnum):
    NONE = "none"
    INDIRECT = "indirect"
    DIRECT = "direct"
    VERIFIED = "verified"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class EvidenceLevelSignals:
    evidence_counts: Mapping[str, int]
    successful_tool_calls: int
    verification_status: str
    passed_evidence_references: int = 0
    passed_post_mutation_commands: int = 0
    failed_criteria: int = 0
    blocked_criteria: int = 0


@dataclass(frozen=True, slots=True)
class EvidenceLevelAssessment:
    level: EvidenceLevel
    evidence_count: int
    verified_criteria: int
    reason_code: str

    def to_data(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "evidence_count": self.evidence_count,
            "verified_criteria": self.verified_criteria,
            "reason_code": self.reason_code,
        }


class EvidenceLevelEvaluatorPort(RuntimeAdapter, Protocol):
    async def assess(
        self, signals: EvidenceLevelSignals
    ) -> EvidenceLevelAssessment: ...
