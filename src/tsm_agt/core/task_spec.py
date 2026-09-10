"""Event-sourced completion contract owned by one Task."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from tsm_agt.ports import RuntimeEvent, ToolEffect

from .configuration import canonical_hash


class TaskCriterionKind(StrEnum):
    WORKSPACE_INTEGRITY = "workspace_integrity"
    POST_MUTATION_COMMAND = "post_mutation_command"
    EVIDENCE_REFERENCE = "evidence_reference"


class TaskOutcomeKind(StrEnum):
    """Project-neutral result shapes a Task may be required to deliver."""

    ANSWER = "ANSWER"
    EVIDENCE = "EVIDENCE"
    WORKSPACE_DELIVERY = "WORKSPACE_DELIVERY"
    COMMAND_RESULT = "COMMAND_RESULT"
    PROCESS_STATE = "PROCESS_STATE"
    ARTIFACT_DELIVERY = "ARTIFACT_DELIVERY"
    USER_DECISION = "USER_DECISION"


class TaskOutcomeStatus(StrEnum):
    """Runtime-owned lifecycle; a model proposal cannot set these values."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DELIVERED = "DELIVERED"
    ALREADY_SATISFIED = "ALREADY_SATISFIED"
    WAITING_USER = "WAITING_USER"
    BLOCKED = "BLOCKED"
    WAIVED = "WAIVED"
    REPLACED = "REPLACED"

    @property
    def is_closed(self) -> bool:
        return self in {
            self.DELIVERED, self.ALREADY_SATISFIED,
            self.WAIVED, self.REPLACED,
        }


class TaskContinuationMode(StrEnum):
    NONE = "NONE"
    WHEN_USER_DECISION_REQUIRED = "WHEN_USER_DECISION_REQUIRED"
    AFTER_COMPLETED_UNIT = "AFTER_COMPLETED_UNIT"


@dataclass(frozen=True, slots=True)
class TaskOutcomeProposal:
    """Untrusted model proposal; contains no Runtime authority fields."""

    outcome_id: str
    description: str
    kind: TaskOutcomeKind
    required_effects: tuple[ToolEffect, ...]
    required: bool = True

    def __post_init__(self) -> None:
        if not self.outcome_id.strip() or not self.description.strip():
            raise ValueError("Task outcome proposal identity and description are required")
        if len(self.outcome_id) > 120 or len(self.description) > 1000:
            raise ValueError("Task outcome proposal exceeds bounded lengths")
        if (
            not self.required_effects
            and self.kind not in {
                TaskOutcomeKind.ANSWER, TaskOutcomeKind.USER_DECISION,
            }
        ):
            raise ValueError(
                "non-conversational Task outcome requires at least one effect"
            )
        if any(effect in {ToolEffect.UNSPECIFIED, ToolEffect.INTERNAL}
               for effect in self.required_effects):
            raise ValueError(
                "Task outcome cannot require unspecified or internal effects"
            )
        if len(set(self.required_effects)) != len(self.required_effects):
            raise ValueError("Task outcome required effects must be unique")

    def to_data(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id.strip(),
            "description": self.description.strip(),
            "kind": self.kind.value,
            "required_effects": [item.value for item in self.required_effects],
            "required": self.required,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TaskOutcomeProposal:
        _reject_unknown_fields(data, {
            "outcome_id", "description", "kind", "required_effects", "required",
        }, "Task outcome proposal")
        raw_effects = data.get("required_effects")
        if not isinstance(raw_effects, list):
            raise ValueError("Task outcome proposal required_effects must be a list")
        if not isinstance(data.get("required", True), bool):
            raise ValueError("Task outcome proposal required must be boolean")
        return cls(
            str(data.get("outcome_id", "")).strip(),
            str(data.get("description", "")).strip(),
            TaskOutcomeKind(str(data.get("kind", ""))),
            tuple(ToolEffect(str(item)) for item in raw_effects),
            bool(data.get("required", True)),
        )


@dataclass(frozen=True, slots=True)
class TaskSpecProposal:
    """Versioned form submitted by a planner model for Runtime validation."""

    schema_version: int
    goal: str
    scope: tuple[str, ...]
    constraints: tuple[str, ...]
    outcomes: tuple[TaskOutcomeProposal, ...]
    continuation_mode: TaskContinuationMode = TaskContinuationMode.NONE

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported Task SPEC proposal schema version")
        if not self.goal.strip() or len(self.goal) > 2000:
            raise ValueError("Task SPEC proposal goal is required and bounded")
        if not self.outcomes or len(self.outcomes) > 30:
            raise ValueError("Task SPEC proposal requires 1-30 outcomes")
        if len({item.outcome_id for item in self.outcomes}) != len(self.outcomes):
            raise ValueError("Task SPEC proposal outcome IDs must be unique")
        if len(self.scope) > 50 or len(self.constraints) > 50:
            raise ValueError("Task SPEC proposal scope or constraints exceed limits")
        for value in self.scope + self.constraints:
            if not value.strip() or len(value) > 1000:
                raise ValueError("Task SPEC proposal scope/constraint is invalid")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal": self.goal.strip(),
            "scope": list(self.scope),
            "constraints": list(self.constraints),
            "outcomes": [item.to_data() for item in self.outcomes],
            "continuation_policy": {"mode": self.continuation_mode.value},
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TaskSpecProposal:
        _reject_unknown_fields(data, {
            "schema_version", "goal", "scope", "constraints", "outcomes",
            "continuation_policy",
        }, "Task SPEC proposal")
        raw_scope = data.get("scope")
        raw_constraints = data.get("constraints")
        raw_outcomes = data.get("outcomes")
        raw_continuation = data.get("continuation_policy")
        if not isinstance(raw_scope, list) or not isinstance(raw_constraints, list):
            raise ValueError("Task SPEC proposal scope and constraints must be lists")
        if not isinstance(raw_outcomes, list) or not all(
            isinstance(item, Mapping) for item in raw_outcomes
        ):
            raise ValueError("Task SPEC proposal outcomes must be an object list")
        if not isinstance(raw_continuation, Mapping):
            raise ValueError("Task SPEC proposal continuation_policy must be an object")
        _reject_unknown_fields(raw_continuation, {"mode"}, "continuation policy")
        return cls(
            int(data.get("schema_version", 0)), str(data.get("goal", "")).strip(),
            tuple(str(item).strip() for item in raw_scope),
            tuple(str(item).strip() for item in raw_constraints),
            tuple(TaskOutcomeProposal.from_data(item) for item in raw_outcomes),
            TaskContinuationMode(str(raw_continuation.get("mode", ""))),
        )


@dataclass(frozen=True, slots=True)
class TaskOutcomeSnapshot:
    """Runtime-authoritative outcome state reconstructed from durable events."""

    outcome_id: str
    description: str
    kind: TaskOutcomeKind
    required_effects: tuple[ToolEffect, ...]
    status: TaskOutcomeStatus = TaskOutcomeStatus.PENDING
    required: bool = True
    fulfillment_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        TaskOutcomeProposal(
            self.outcome_id, self.description, self.kind,
            self.required_effects, self.required,
        )
        if any(not value.strip() for value in self.fulfillment_refs):
            raise ValueError("Task outcome fulfillment references must not be empty")
        if len(set(self.fulfillment_refs)) != len(self.fulfillment_refs):
            raise ValueError("Task outcome fulfillment references must be unique")

    @classmethod
    def from_proposal(cls, proposal: TaskOutcomeProposal) -> TaskOutcomeSnapshot:
        return cls(
            proposal.outcome_id, proposal.description, proposal.kind,
            proposal.required_effects, TaskOutcomeStatus.PENDING,
            proposal.required, (),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            **TaskOutcomeProposal(
                self.outcome_id, self.description, self.kind,
                self.required_effects, self.required,
            ).to_data(),
            "status": self.status.value,
            "fulfillment_refs": list(self.fulfillment_refs),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TaskOutcomeSnapshot:
        _reject_unknown_fields(data, {
            "outcome_id", "description", "kind", "required_effects", "required",
            "status", "fulfillment_refs",
        }, "Task outcome snapshot")
        proposal = TaskOutcomeProposal.from_data({
            key: data[key] for key in (
                "outcome_id", "description", "kind", "required_effects", "required"
            ) if key in data
        })
        raw_refs = data.get("fulfillment_refs", [])
        if not isinstance(raw_refs, list):
            raise ValueError("Task outcome fulfillment_refs must be a list")
        return cls(
            proposal.outcome_id, proposal.description, proposal.kind,
            proposal.required_effects,
            TaskOutcomeStatus(str(data.get("status", "PENDING"))),
            proposal.required, tuple(str(item) for item in raw_refs),
        )


def _reject_unknown_fields(
    data: Mapping[str, Any], allowed: set[str], label: str,
) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {', '.join(sorted(unknown))}")


TASK_SPEC_PROPOSAL_SCHEMA_V1: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "enum": [1]},
        "goal": {"type": "string", "minLength": 1, "maxLength": 2000},
        "scope": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
        "constraints": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
        "outcomes": {
            "type": "array", "minItems": 1, "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "outcome_id": {"type": "string", "minLength": 1, "maxLength": 120},
                    "description": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "kind": {"type": "string", "enum": [item.value for item in TaskOutcomeKind]},
                    "required_effects": {
                        "type": "array", "minItems": 0, "uniqueItems": True,
                        "items": {"type": "string", "enum": [
                            item.value for item in ToolEffect
                            if item not in {ToolEffect.UNSPECIFIED, ToolEffect.INTERNAL}
                        ]},
                    },
                    "required": {"type": "boolean"},
                },
                "required": [
                    "outcome_id", "description", "kind", "required_effects", "required",
                ],
                "additionalProperties": False,
            },
        },
        "continuation_policy": {
            "type": "object",
            "properties": {"mode": {
                "type": "string", "enum": [item.value for item in TaskContinuationMode],
            }},
            "required": ["mode"], "additionalProperties": False,
        },
    },
    "required": [
        "schema_version", "goal", "scope", "constraints", "outcomes",
        "continuation_policy",
    ],
    "additionalProperties": False,
}


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
    outcomes: tuple[TaskOutcomeSnapshot, ...] = ()
    continuation_mode: TaskContinuationMode = TaskContinuationMode.NONE
    schema_version: int = 1
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
        if self.schema_version not in {0, 1}:
            raise ValueError("unsupported Task SPEC schema version")
        if self.schema_version == 0 and (
            self.outcomes or self.continuation_mode is not TaskContinuationMode.NONE
        ):
            raise ValueError("legacy Task SPEC cannot contain outcomes or continuation")
        if len(self.outcomes) > 30:
            raise ValueError("Task SPEC supports at most 30 outcomes")
        if len({item.outcome_id for item in self.outcomes}) != len(self.outcomes):
            raise ValueError("Task SPEC outcome IDs must be unique")
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

    @classmethod
    def from_proposal(
        cls, task_id: str, revision: int, proposal: TaskSpecProposal,
        acceptance_criteria: tuple[TaskAcceptanceCriterion, ...],
    ) -> TaskSpecSnapshot:
        """Create Runtime-owned state; all outcomes start pending."""
        return cls(
            task_id=task_id, revision=revision, goal=proposal.goal,
            scope=proposal.scope, constraints=proposal.constraints,
            acceptance_criteria=acceptance_criteria,
            outcomes=tuple(
                TaskOutcomeSnapshot.from_proposal(item)
                for item in proposal.outcomes
            ),
            continuation_mode=proposal.continuation_mode,
            schema_version=1,
        )

    def hash_source(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "task_id": self.task_id, "revision": self.revision,
            "goal": self.goal, "scope": list(self.scope),
            "constraints": list(self.constraints),
            "acceptance_criteria": [
                item.to_data() for item in self.acceptance_criteria
            ],
        }
        if self.schema_version >= 1:
            data.update({
                "schema_version": self.schema_version,
                "outcomes": [item.to_data() for item in self.outcomes],
                "continuation_policy": {"mode": self.continuation_mode.value},
            })
        return data

    def to_data(self) -> dict[str, Any]:
        return {**self.hash_source(), "content_hash": self.content_hash}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TaskSpecSnapshot:
        raw_criteria = data.get("acceptance_criteria")
        if not isinstance(raw_criteria, list):
            raise ValueError("Task SPEC acceptance_criteria must be a list")
        schema_version = int(data.get("schema_version", 0))
        raw_outcomes = data.get("outcomes", [])
        raw_continuation = data.get("continuation_policy", {"mode": "NONE"})
        if not isinstance(raw_outcomes, list) or not all(
            isinstance(item, Mapping) for item in raw_outcomes
        ):
            raise ValueError("Task SPEC outcomes must be an object list")
        if not isinstance(raw_continuation, Mapping):
            raise ValueError("Task SPEC continuation_policy must be an object")
        return cls(
            task_id=str(data["task_id"]), revision=int(data["revision"]),
            goal=str(data["goal"]),
            scope=tuple(str(item).strip() for item in data.get("scope", [])),
            constraints=tuple(
                str(item).strip() for item in data.get("constraints", [])
            ),
            acceptance_criteria=tuple(
                TaskAcceptanceCriterion.from_data(item) for item in raw_criteria
            ),
            outcomes=tuple(TaskOutcomeSnapshot.from_data(item) for item in raw_outcomes),
            continuation_mode=TaskContinuationMode(
                str(raw_continuation.get("mode", "NONE"))
            ),
            schema_version=schema_version,
            content_hash=str(data.get("content_hash") or ""),
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
            elif event.event_type == "task_outcome.state_changed":
                outcome_id = str(event.payload.get("outcome_id", ""))
                status = TaskOutcomeStatus(str(event.payload.get("status", "")))
                reference = str(event.payload.get("fulfillment_ref", "")).strip()
                matched = False
                outcomes: list[TaskOutcomeSnapshot] = []
                for outcome in current.outcomes:
                    if outcome.outcome_id != outcome_id:
                        outcomes.append(outcome)
                        continue
                    refs = outcome.fulfillment_refs
                    if reference and reference not in refs:
                        refs += (reference,)
                    outcomes.append(replace(
                        outcome, status=status, fulfillment_refs=refs
                    ))
                    matched = True
                if not matched:
                    raise ValueError("Task outcome event references unknown outcome")
                current = replace(
                    current, outcomes=tuple(outcomes), content_hash=""
                )
                continue
            else:
                continue
            if not isinstance(raw, Mapping):
                raise ValueError("Task SPEC event snapshot is missing")
            candidate = TaskSpecSnapshot.from_data(raw)
            if candidate.task_id != task_id:
                raise ValueError("Task SPEC snapshot belongs to another Task")
            current = candidate
        return current
