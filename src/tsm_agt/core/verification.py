"""Trusted, payload-minimal acceptance evidence for completed Agent work."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tsm_agt.ports import EvidenceLevelAssessment

from .task import TaskState


class AcceptanceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    # A criterion whose precondition does not hold in this Task. Unlike BLOCKED
    # it is not an outstanding debt: nobody has to act on it, so it must never
    # become a required completion gap.
    NOT_APPLICABLE = "not_applicable"


#: The single verdict -> terminal-state mapping (AG-16).
#:
#: Binding a verdict to a terminal state is a Runtime decision, not a client
#: one. If each entry point maps for itself, the same verdict is reported as
#: "failed" on one surface and "needs review" on another -- which is exactly the
#: divergence that existed between the CLI (everything not passed -> FAILED) and
#: the SDK (BLOCKED -> NEEDS_REVIEW).
TERMINAL_STATE_BY_VERDICT: Mapping[AcceptanceStatus, TaskState] = {
    AcceptanceStatus.PASSED: TaskState.SUCCEEDED,
    # "Cannot decide" is not "the work failed": a human decides.
    AcceptanceStatus.BLOCKED: TaskState.NEEDS_REVIEW,
    # Positive refutation: the delivered work is demonstrably wrong.
    AcceptanceStatus.FAILED: TaskState.FAILED,
    # Not expected as a Task-level verdict (the aggregate is computed with
    # FAILED > BLOCKED > PASSED precedence). Mapped for totality, fail-closed.
    AcceptanceStatus.NOT_APPLICABLE: TaskState.NEEDS_REVIEW,
}


def terminal_state_for(status: AcceptanceStatus) -> TaskState:
    """Return the one terminal state every entry point must use for ``status``.

    Clients must never write their own ``if passed ... else FAILED``: that is
    how a blocked (undecided) verdict gets reported as a product failure.
    """
    try:
        return TERMINAL_STATE_BY_VERDICT[status]
    except KeyError as error:  # pragma: no cover - the enum is closed
        raise ValueError(
            f"no terminal state mapped for acceptance status {status!r}"
        ) from error


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
