"""Event-sourced completion contract owned by one Task."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tsm_agt.ports import RuntimeEvent

from .configuration import canonical_hash


class TaskCriterionKind(StrEnum):
    WORKSPACE_INTEGRITY = "workspace_integrity"
    POST_MUTATION_COMMAND = "post_mutation_command"
    EVIDENCE_REFERENCE = "evidence_reference"


@dataclass(frozen=True, slots=True)
class TaskAcceptanceCriterion:
    criterion_id: str
    description: str
    verification_kind: TaskCriterionKind
    evidence_reference: str | None = None

    def __post_init__(self) -> None:
        if not self.criterion_id.strip() or not self.description.strip():
            raise ValueError("Task SPEC criterion identity and description are required")
        if len(self.criterion_id) > 120 or len(self.description) > 1000:
            raise ValueError("Task SPEC criterion is too long")
        if (
            self.verification_kind is TaskCriterionKind.EVIDENCE_REFERENCE
            and not (self.evidence_reference or "").strip()
        ):
            raise ValueError("evidence_reference criterion requires a reference")
        if (
            self.verification_kind is not TaskCriterionKind.EVIDENCE_REFERENCE
            and self.evidence_reference is not None
        ):
            raise ValueError("only evidence_reference criteria accept a reference")

    def to_data(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "description": self.description,
            "verification_kind": self.verification_kind.value,
            "evidence_reference": self.evidence_reference,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TaskAcceptanceCriterion:
        return cls(
            str(data["criterion_id"]).strip(),
            str(data["description"]).strip(),
            TaskCriterionKind(str(data["verification_kind"])),
            (str(data["evidence_reference"]).strip()
             if data.get("evidence_reference") is not None else None),
        )


@dataclass(frozen=True, slots=True)
class TaskSpecSnapshot:
    task_id: str
    revision: int
    goal: str
    scope: tuple[str, ...]
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[TaskAcceptanceCriterion, ...]
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.goal.strip():
            raise ValueError("Task SPEC task identity and goal are required")
        if self.revision < 1:
            raise ValueError("Task SPEC revision must be positive")
        if len(self.goal) > 2000 or len(self.scope) > 50 or len(self.constraints) > 50:
            raise ValueError("Task SPEC exceeds its bounded limits")
        if not self.acceptance_criteria or len(self.acceptance_criteria) > 30:
            raise ValueError("Task SPEC requires 1-30 acceptance criteria")
        if len({item.criterion_id for item in self.acceptance_criteria}) != len(
            self.acceptance_criteria
        ):
            raise ValueError("Task SPEC criterion IDs must be unique")
        for value in self.scope + self.constraints:
            if not value.strip() or len(value) > 1000:
                raise ValueError("Task SPEC scope/constraint entries are invalid")
        expected = canonical_hash(self.hash_source())
        if self.content_hash and self.content_hash != expected:
            raise ValueError("Task SPEC content hash does not match")
        object.__setattr__(self, "content_hash", expected)

    @classmethod
    def initial(cls, task_id: str, goal: str) -> TaskSpecSnapshot:
        return cls(
            task_id, 1, goal.strip(), (), (),
            (TaskAcceptanceCriterion(
                "workspace-integrity",
                "Committed workspace effects still match the mutation journal",
                TaskCriterionKind.WORKSPACE_INTEGRITY,
            ),),
        )

    def hash_source(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "revision": self.revision,
            "goal": self.goal, "scope": list(self.scope),
            "constraints": list(self.constraints),
            "acceptance_criteria": [
                item.to_data() for item in self.acceptance_criteria
            ],
        }

    def to_data(self) -> dict[str, Any]:
        return {**self.hash_source(), "content_hash": self.content_hash}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TaskSpecSnapshot:
        raw_criteria = data.get("acceptance_criteria")
        if not isinstance(raw_criteria, list):
            raise ValueError("Task SPEC acceptance_criteria must be a list")
        return cls(
            str(data["task_id"]), int(data["revision"]), str(data["goal"]),
            tuple(str(item).strip() for item in data.get("scope", [])),
            tuple(str(item).strip() for item in data.get("constraints", [])),
            tuple(TaskAcceptanceCriterion.from_data(item) for item in raw_criteria),
            str(data.get("content_hash") or ""),
        )


class TaskSpecProjector:
    @staticmethod
    def project(task_id: str, goal: str, events: Sequence[RuntimeEvent]) -> TaskSpecSnapshot:
        current = TaskSpecSnapshot.initial(task_id, goal)
        for event in sorted(events, key=lambda item: item.sequence):
            if event.task_id != task_id:
                raise ValueError("Task SPEC event belongs to another Task")
            if event.event_type == "task.created":
                raw = event.payload.get("task_spec")
                if raw is None:
                    # Backward-compatible projection for pre-A10 Tasks.
                    continue
            elif event.event_type in {"task_spec.created", "task_spec.revised"}:
                raw = event.payload.get("snapshot")
            else:
                continue
            if not isinstance(raw, Mapping):
                raise ValueError("Task SPEC event snapshot is missing")
            candidate = TaskSpecSnapshot.from_data(raw)
            if candidate.task_id != task_id:
                raise ValueError("Task SPEC snapshot belongs to another Task")
            current = candidate
        return current
