"""Trusted, payload-minimal acceptance evidence for completed Agent work."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tsm_agt.ports import EvidenceLevelAssessment


class AcceptanceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    assertion: str
    observed: str
    source_step_id: str
    passed: bool
    artifact_path: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "assertion": self.assertion,
            "observed": self.observed, "source_step_id": self.source_step_id,
            "artifact_path": self.artifact_path, "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    criterion_id: str
    status: AcceptanceStatus
    evidence: tuple[Evidence, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id, "status": self.status.value,
            "evidence": [item.to_data() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class TaskVerificationResult:
    task_id: str
    status: AcceptanceStatus
    criteria: tuple[AcceptanceResult, ...]
    evidence_level: EvidenceLevelAssessment | None = None

    @property
    def passed(self) -> bool:
        return self.status is AcceptanceStatus.PASSED

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "status": self.status.value,
            "criteria": [item.to_data() for item in self.criteria],
            "evidence_level": (
                self.evidence_level.to_data() if self.evidence_level else None
            ),
        }
