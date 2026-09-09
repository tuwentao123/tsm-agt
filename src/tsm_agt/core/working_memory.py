"""Task-scoped working memory made of concise, inspectable conclusions.

This is not hidden chain-of-thought and not durable user/project memory.  Every
revision is rebuilt from the Task event log and may contain only bounded,
user-inspectable state used to continue engineering work.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tsm_agt.ports import RuntimeEvent

from .configuration import canonical_hash
from .evidence_question import (
    EvidenceQuestionProjection, EvidenceQuestionStatus,
)


class WorkingPlanStepStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class WorkingPlanStep:
    step_id: str
    description: str
    status: WorkingPlanStepStatus
    completion_criteria: str

    def __post_init__(self) -> None:
        _validate_text(self.step_id, "plan step id", 120)
        _validate_text(self.description, "plan step description", 1000)
        _validate_text(self.completion_criteria, "completion criteria", 1000)

    def to_data(self) -> dict[str, str]:
        return {
            "step_id": self.step_id, "description": self.description,
            "status": self.status.value,
            "completion_criteria": self.completion_criteria,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkingPlanStep:
        return cls(
            str(data["step_id"]).strip(), str(data["description"]).strip(),
            WorkingPlanStepStatus(str(data["status"])),
            str(data["completion_criteria"]).strip(),
        )


@dataclass(frozen=True, slots=True)
class WorkingEvidenceReference:
    evidence_id: str
    kind: str
    reference: str
    summary: str

    def __post_init__(self) -> None:
        _validate_text(self.evidence_id, "evidence id", 120)
        _validate_text(self.kind, "evidence kind", 120)
        _validate_text(self.reference, "evidence reference", 1000)
        _validate_text(self.summary, "evidence summary", 1000)

    def to_data(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id, "kind": self.kind,
            "reference": self.reference, "summary": self.summary,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkingEvidenceReference:
        return cls(
            str(data["evidence_id"]).strip(), str(data["kind"]).strip(),
            str(data["reference"]).strip(), str(data["summary"]).strip(),
        )


@dataclass(frozen=True, slots=True)
class WorkingMemorySnapshot:
    task_id: str
    revision: int
    goal: str
    constraints: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    plan: tuple[WorkingPlanStep, ...] = ()
    completed_work: tuple[str, ...] = ()
    remaining_work: tuple[str, ...] = ()
    evidence: tuple[WorkingEvidenceReference, ...] = ()
    source_event_sequences: tuple[int, ...] = ()
    content_hash: str = ""

    def __post_init__(self) -> None:
        _validate_text(self.task_id, "working memory task id", 200)
        if self.revision < 1:
            raise ValueError("working memory revision must be positive")
        _validate_text(self.goal, "working memory goal", 2000)
        for name, values in (
            ("constraints", self.constraints), ("facts", self.facts),
            ("decisions", self.decisions),
            ("hypotheses", self.hypotheses),
            ("open_questions", self.open_questions),
            ("completed_work", self.completed_work),
            ("remaining_work", self.remaining_work),
        ):
            _validate_text_list(values, name)
        if len(self.plan) > 30 or len(self.evidence) > 50:
            raise ValueError("working memory plan/evidence exceeds its bounded limit")
        if len({step.step_id for step in self.plan}) != len(self.plan):
            raise ValueError("working memory plan step IDs must be unique")
        if sum(
            step.status is WorkingPlanStepStatus.IN_PROGRESS
            for step in self.plan
        ) > 1:
            raise ValueError(
                "working memory plan may have at most one IN_PROGRESS step"
            )
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("working memory evidence IDs must be unique")
        if tuple(sorted(set(self.source_event_sequences))) != self.source_event_sequences:
            raise ValueError("working memory sources must be sorted and unique")
        expected = canonical_hash(self.hash_source())
        if self.content_hash and self.content_hash != expected:
            raise ValueError("working memory content hash does not match")
        object.__setattr__(self, "content_hash", expected)

    @classmethod
    def initial(cls, task_id: str, goal: str) -> WorkingMemorySnapshot:
        return cls(task_id=task_id, revision=1, goal=goal.strip())

    @classmethod
    def from_update(
        cls, *, task_id: str, revision: int, state: Mapping[str, Any],
        source_event_sequences: tuple[int, ...],
    ) -> WorkingMemorySnapshot:
        return cls(
            task_id=task_id, revision=revision,
            goal=_required_string(state, "goal"),
            constraints=_string_tuple(state, "constraints"),
            facts=_string_tuple(state, "facts"),
            decisions=_string_tuple(state, "decisions"),
            hypotheses=_string_tuple(state, "hypotheses"),
            open_questions=_string_tuple(state, "open_questions"),
            plan=_object_tuple(state, "plan", WorkingPlanStep.from_data),
            completed_work=_string_tuple(state, "completed_work"),
            remaining_work=_string_tuple(state, "remaining_work"),
            evidence=_object_tuple(
                state, "evidence", WorkingEvidenceReference.from_data
            ),
            source_event_sequences=source_event_sequences,
        )

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkingMemorySnapshot:
        raw_sources = data.get("source_event_sequences", [])
        if not isinstance(raw_sources, list):
            raise ValueError("working memory sources must be a list")
        snapshot = cls.from_update(
            task_id=str(data["task_id"]), revision=int(data["revision"]),
            state=data,
            source_event_sequences=tuple(int(item) for item in raw_sources),
        )
        if data.get("content_hash") and data["content_hash"] != snapshot.content_hash:
            raise ValueError("working memory content hash does not match")
        return snapshot

    def state_data(self) -> dict[str, Any]:
        return {
            "goal": self.goal, "constraints": list(self.constraints),
            "facts": list(self.facts), "decisions": list(self.decisions),
            "hypotheses": list(self.hypotheses),
            "open_questions": list(self.open_questions),
            "plan": [step.to_data() for step in self.plan],
            "completed_work": list(self.completed_work),
            "remaining_work": list(self.remaining_work),
            "evidence": [item.to_data() for item in self.evidence],
        }

    def hash_source(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "task_id": self.task_id,
            "revision": self.revision, **self.state_data(),
            "source_event_sequences": list(self.source_event_sequences),
        }

    def to_data(self) -> dict[str, Any]:
        return {**self.hash_source(), "content_hash": self.content_hash}

    def session_state_data(self) -> dict[str, Any]:
        return {
            "goal": self.goal, "constraints": list(self.constraints),
            "decisions": list(self.decisions),
            "open_questions": list(self.open_questions),
            "completed_work": list(self.completed_work),
            "remaining_work": list(self.remaining_work),
        }


@dataclass(frozen=True, slots=True)
class EffectiveWorkingMemory:
    """Read-only Task state assembled from authored memory and Runtime facts.

    The model-authored snapshot remains revisioned and replaceable through the
    working-memory tool.  Runtime-derived items are projected independently from
    durable Evidence Question events, so a later model update cannot accidentally
    erase a still-open question or preserve a question that has been resolved.
    """

    snapshot: WorkingMemorySnapshot
    runtime_remaining_work: tuple[str, ...] = ()
    runtime_evidence: tuple[WorkingEvidenceReference, ...] = ()
    runtime_source_event_sequences: tuple[int, ...] = ()
    projection_hash: str = ""

    def __post_init__(self) -> None:
        if tuple(sorted(set(self.runtime_source_event_sequences))) != (
            self.runtime_source_event_sequences
        ):
            raise ValueError(
                "effective working memory sources must be sorted and unique"
            )
        expected = canonical_hash(self.hash_source())
        if self.projection_hash and self.projection_hash != expected:
            raise ValueError(
                "effective working memory projection hash does not match"
            )
        object.__setattr__(self, "projection_hash", expected)

    @property
    def remaining_work(self) -> tuple[str, ...]:
        return _merge_bounded(
            self.runtime_remaining_work, self.snapshot.remaining_work, limit=50
        )

    @property
    def evidence(self) -> tuple[WorkingEvidenceReference, ...]:
        merged: list[WorkingEvidenceReference] = []
        seen: set[str] = set()
        for item in self.runtime_evidence + self.snapshot.evidence:
            if item.evidence_id in seen:
                continue
            seen.add(item.evidence_id)
            merged.append(item)
            if len(merged) >= 50:
                break
        return tuple(merged)

    def hash_source(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "task_id": self.snapshot.task_id,
            "base_revision": self.snapshot.revision,
            "base_content_hash": self.snapshot.content_hash,
            "runtime_remaining_work": list(self.runtime_remaining_work),
            "runtime_evidence": [
                item.to_data() for item in self.runtime_evidence
            ],
            "runtime_source_event_sequences": list(
                self.runtime_source_event_sequences
            ),
        }

    def to_data(self) -> dict[str, Any]:
        """Keep the authored snapshot compatible and expose derived state."""
        return {
            **self.snapshot.to_data(),
            "runtime_derived": {
                "remaining_work": list(self.runtime_remaining_work),
                "evidence": [item.to_data() for item in self.runtime_evidence],
                "source_event_sequences": list(
                    self.runtime_source_event_sequences
                ),
                "projection_hash": self.projection_hash,
            },
            "effective_remaining_work": list(self.remaining_work),
            "effective_evidence": [item.to_data() for item in self.evidence],
        }

    def session_state_data(self) -> dict[str, Any]:
        data = self.snapshot.session_state_data()
        data["remaining_work"] = list(self.remaining_work)
        return data


@dataclass(frozen=True, slots=True)
class EffectiveWorkingMemoryProjector:
    """Derive minimum trusted progress from persisted Runtime events."""

    def project(
        self, snapshot: WorkingMemorySnapshot,
        questions: EvidenceQuestionProjection,
    ) -> EffectiveWorkingMemory:
        if snapshot.task_id != questions.task_id:
            raise ValueError(
                "working memory and evidence questions belong to different Tasks"
            )
        remaining: list[str] = []
        evidence: list[WorkingEvidenceReference] = []
        sources: set[int] = set()
        for record in questions.records:
            if record.status in {
                EvidenceQuestionStatus.OPEN, EvidenceQuestionStatus.BLOCKED,
            }:
                prefix = (
                    "Resolve evidence question"
                    if record.status is EvidenceQuestionStatus.OPEN
                    else "Unblock evidence question"
                )
                detail = f"{prefix}: {record.question}"
                if record.expected_scope:
                    detail += f" (scope: {record.expected_scope})"
                if record.blocking_reason:
                    detail += f" (reason: {record.blocking_reason})"
                remaining.append(detail[:1000])
            elif (
                record.status is EvidenceQuestionStatus.RESOLVED
                and record.evidence_references
            ):
                evidence.append(WorkingEvidenceReference(
                    evidence_id=f"runtime-question-{record.question_ref}",
                    kind="evidence_question",
                    reference=record.evidence_references[-1],
                    summary=f"Resolved evidence question: {record.question}"[:1000],
                ))
            if record.updated_event_sequence > 0:
                sources.add(record.updated_event_sequence)
        return EffectiveWorkingMemory(
            snapshot=snapshot,
            runtime_remaining_work=tuple(remaining[-20:]),
            runtime_evidence=tuple(evidence[-20:]),
            runtime_source_event_sequences=tuple(sorted(sources)),
        )


@dataclass(frozen=True, slots=True)
class WorkingMemoryProjector:
    def project(
        self, task_id: str, default_goal: str, events: Sequence[RuntimeEvent],
    ) -> WorkingMemorySnapshot:
        current = WorkingMemorySnapshot.initial(task_id, default_goal)
        seen_operations: set[str] = set()
        for event in sorted(events, key=lambda item: item.sequence):
            if event.task_id != task_id or event.event_type != "working_memory.updated":
                continue
            operation_id = str(event.payload.get("operation_id") or "")
            raw = event.payload.get("snapshot")
            if not operation_id or operation_id in seen_operations or not isinstance(raw, Mapping):
                continue
            candidate = WorkingMemorySnapshot.from_data(raw)
            if candidate.task_id != task_id:
                raise ValueError("working memory event belongs to a different Task")
            if candidate.revision != current.revision + 1:
                raise ValueError("working memory event revision is not contiguous")
            if candidate.source_event_sequences[-1:] != (event.sequence,):
                raise ValueError("working memory source does not match its event")
            current = candidate
            seen_operations.add(operation_id)
        return current


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(
        r"\b(?:api[_-]?key|password|passwd|secret|access[_-]?token)"
        r"\s*[:=]\s*[^\s,;]{6,}", re.I,
    ),
)


def _validate_text(value: str, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError("credentials and private key material cannot enter working memory")


def _validate_text_list(values: tuple[str, ...], name: str) -> None:
    if len(values) > 50:
        raise ValueError(f"working memory {name} exceeds 50 entries")
    if len(set(values)) != len(values):
        raise ValueError(f"working memory {name} must not contain duplicates")
    for value in values:
        _validate_text(value, f"working memory {name} item", 1000)


def _required_string(data: Mapping[str, Any], key: str) -> str:
    if key not in data:
        raise ValueError(f"working memory state is missing {key}")
    return str(data[key]).strip()


def _string_tuple(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw = data.get(key)
    if not isinstance(raw, list):
        raise ValueError(f"working memory {key} must be a list")
    return tuple(str(item).strip() for item in raw)


def _object_tuple(data: Mapping[str, Any], key: str, factory):
    raw = data.get(key)
    if not isinstance(raw, list):
        raise ValueError(f"working memory {key} must be a list")
    if any(not isinstance(item, Mapping) for item in raw):
        raise ValueError(f"working memory {key} entries must be objects")
    return tuple(factory(item) for item in raw)


def _merge_bounded(
    preferred: tuple[str, ...], authored: tuple[str, ...], *, limit: int,
) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in preferred + authored:
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
        if len(merged) >= limit:
            break
    return tuple(merged)
