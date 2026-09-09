"""Inspectable lifecycle for the concrete questions behind tool calls.

The lifecycle is rebuilt only from Runtime Events and structured Tool results.
Model prose cannot silently mark a question resolved, and the projection carries
no tool authority or workspace permission.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from tsm_agt.ports import EvidenceDelta, EvidenceQuestion, RuntimeEvent, ToolCall, ToolResult

from .configuration import canonical_hash


class EvidenceQuestionStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    BLOCKED = "BLOCKED"
    DROPPED = "DROPPED"


class EvidenceObservationKind(StrEnum):
    ARTIFACT_READ = "ARTIFACT_READ"
    NON_EMPTY_RESULT = "NON_EMPTY_RESULT"
    EMPTY_RESULT = "EMPTY_RESULT"
    STRUCTURED_RESULT = "STRUCTURED_RESULT"
    RECOVERABLE_FAILURE = "RECOVERABLE_FAILURE"
    FAILURE = "FAILURE"
    DROPPED_BY_GOAL_REPLACEMENT = "DROPPED_BY_GOAL_REPLACEMENT"
    ACTION_REPLACED = "ACTION_REPLACED"
    ACTION_CANCELLED = "ACTION_CANCELLED"
    ACTION_DENIED = "ACTION_DENIED"
    WAITING_FOR_USER = "WAITING_FOR_USER"


class ToolActionDisposition(StrEnum):
    """Runtime disposition for a bound Tool Action that did not run."""

    REPLACE = "REPLACE"
    CANCEL = "CANCEL"
    WAIT_FOR_USER = "WAIT_FOR_USER"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class EvidenceQuestionRecord:
    """One question plus trustworthy, bounded lifecycle metadata."""

    question_id: str
    question: str
    status: EvidenceQuestionStatus
    source_task_id: str
    source_turn_id: str
    tool_call_ids: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()
    observation_kind: EvidenceObservationKind | None = None
    blocking_reason: str | None = None
    revision: int = 1
    updated_event_sequence: int = 0
    expected_scope: str = ""

    def __post_init__(self) -> None:
        EvidenceQuestion(
            self.question_id, self.question,
            expected_scope=self.expected_scope,
        )
        if not self.source_task_id or not self.source_turn_id:
            raise ValueError("evidence question source identity is required")
        if self.revision < 1 or self.updated_event_sequence < 0:
            raise ValueError("evidence question revision/sequence is invalid")
        if len(set(self.tool_call_ids)) != len(self.tool_call_ids):
            raise ValueError("evidence question tool calls must be unique")
        if len(set(self.evidence_references)) != len(self.evidence_references):
            raise ValueError("evidence question references must be unique")

    @property
    def question_ref(self) -> str:
        return canonical_hash(self.question_id)[:12]

    def to_data(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "status": self.status.value,
            "source_task_id": self.source_task_id,
            "source_turn_id": self.source_turn_id,
            "tool_call_ids": list(self.tool_call_ids),
            "evidence_references": list(self.evidence_references),
            "observation_kind": (
                self.observation_kind.value if self.observation_kind else None
            ),
            "blocking_reason": self.blocking_reason,
            "revision": self.revision,
            "updated_event_sequence": self.updated_event_sequence,
            "expected_scope": self.expected_scope,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> EvidenceQuestionRecord:
        raw_calls = data.get("tool_call_ids", [])
        raw_refs = data.get("evidence_references", [])
        if not isinstance(raw_calls, list) or not isinstance(raw_refs, list):
            raise ValueError("evidence question calls/references must be lists")
        kind = data.get("observation_kind")
        return cls(
            question_id=str(data["question_id"]),
            question=str(data["question"]),
            status=EvidenceQuestionStatus(str(data["status"])),
            source_task_id=str(data["source_task_id"]),
            source_turn_id=str(data["source_turn_id"]),
            tool_call_ids=tuple(str(item) for item in raw_calls),
            evidence_references=tuple(str(item) for item in raw_refs),
            observation_kind=(
                EvidenceObservationKind(str(kind)) if kind is not None else None
            ),
            blocking_reason=(
                str(data["blocking_reason"])
                if data.get("blocking_reason") is not None else None
            ),
            revision=int(data.get("revision", 1)),
            updated_event_sequence=int(data.get("updated_event_sequence", 0)),
            expected_scope=str(data.get("expected_scope", "")),
        )


@dataclass(frozen=True, slots=True)
class EvidenceQuestionProjection:
    task_id: str
    records: tuple[EvidenceQuestionRecord, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("evidence question projection task_id is required")
        if self.schema_version != 1:
            raise ValueError("unsupported evidence question schema version")
        ids = [record.question_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence question IDs must be unique")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "records": [record.to_data() for record in self.records],
        }

    @classmethod
    def from_data(
        cls, task_id: str, data: Mapping[str, Any] | None,
    ) -> EvidenceQuestionProjection:
        if not data:
            return cls(task_id)
        raw = data.get("records", [])
        if not isinstance(raw, list):
            raise ValueError("evidence question records must be a list")
        stored_task = str(data.get("task_id", task_id))
        if stored_task != task_id:
            raise ValueError("evidence question projection belongs to another task")
        return cls(
            task_id, tuple(EvidenceQuestionRecord.from_data(item) for item in raw),
            int(data.get("schema_version", 1)),
        )

    def get(self, question_id: str) -> EvidenceQuestionRecord | None:
        return next(
            (record for record in self.records if record.question_id == question_id),
            None,
        )

    def bind(
        self, question: EvidenceQuestion, turn_id: str, tool_call_id: str,
        *, event_sequence: int = 0,
    ) -> EvidenceQuestionProjection:
        current = self.get(question.question_id)
        if current is None:
            updated = EvidenceQuestionRecord(
                question.question_id, question.question, EvidenceQuestionStatus.OPEN,
                self.task_id, turn_id, (tool_call_id,), (), None, None, 1,
                event_sequence, question.expected_scope.strip(),
            )
            return replace(self, records=self.records + (updated,))
        calls = current.tool_call_ids
        if tool_call_id not in calls:
            calls += (tool_call_id,)
        updated = replace(
            current, status=EvidenceQuestionStatus.OPEN, tool_call_ids=calls,
            observation_kind=None, blocking_reason=None,
            revision=current.revision + 1, updated_event_sequence=event_sequence,
        )
        return self._replace(updated)

    def observe(
        self, call: ToolCall, result: ToolResult, delta: EvidenceDelta | None,
        *, event_sequence: int = 0,
    ) -> tuple[EvidenceQuestionProjection, EvidenceQuestionRecord]:
        question = call.evidence_question
        if question is None:
            raise ValueError("cannot observe an unbound evidence question")
        current = self.get(question.question_id)
        if current is None:
            raise ValueError("evidence question must be bound before observation")
        recoverable = bool(
            result.retryable or result.meta.get("recoverable_input")
        )
        if not result.ok:
            status = (
                EvidenceQuestionStatus.OPEN
                if recoverable else EvidenceQuestionStatus.BLOCKED
            )
            kind = (
                EvidenceObservationKind.RECOVERABLE_FAILURE
                if recoverable else EvidenceObservationKind.FAILURE
            )
            reason = result.error_code or "TOOL_FAILED"
        else:
            status = EvidenceQuestionStatus.RESOLVED
            reason = None
            if call.name == "core.read_file":
                kind = EvidenceObservationKind.ARTIFACT_READ
            elif delta is not None and delta.counts.get("new_exclusions", 0):
                kind = EvidenceObservationKind.EMPTY_RESULT
            elif delta is not None and delta.total_new:
                kind = EvidenceObservationKind.NON_EMPTY_RESULT
            else:
                kind = EvidenceObservationKind.STRUCTURED_RESULT
        refs = list(current.evidence_references)
        call_ref = f"tool_call:{call.call_id}"
        if call_ref not in refs:
            refs.append(call_ref)
        if delta is not None:
            for item in delta.items:
                ref = f"evidence:{item.category}:{item.fingerprint}"
                if ref not in refs:
                    refs.append(ref)
                if len(refs) >= 50:
                    break
        updated = replace(
            current, status=status, evidence_references=tuple(refs[:50]),
            observation_kind=kind, blocking_reason=reason,
            revision=current.revision + 1, updated_event_sequence=event_sequence,
        )
        return self._replace(updated), updated

    def drop_open(
        self, *, event_sequence: int = 0, reason: str = "goal_replaced",
    ) -> tuple[EvidenceQuestionProjection, tuple[EvidenceQuestionRecord, ...]]:
        projection = self
        dropped: list[EvidenceQuestionRecord] = []
        for current in self.records:
            if current.status is not EvidenceQuestionStatus.OPEN:
                continue
            updated = replace(
                current, status=EvidenceQuestionStatus.DROPPED,
                observation_kind=EvidenceObservationKind.DROPPED_BY_GOAL_REPLACEMENT,
                blocking_reason=reason, revision=current.revision + 1,
                updated_event_sequence=event_sequence + len(dropped),
            )
            projection = projection._replace(updated)
            dropped.append(updated)
        return projection, tuple(dropped)

    def dispose(
        self, call: ToolCall, disposition: ToolActionDisposition, reason: str,
        *, event_sequence: int = 0,
    ) -> tuple[EvidenceQuestionProjection, EvidenceQuestionRecord | None]:
        """Record why a bound Action will not execute, without faking evidence."""
        question = call.evidence_question
        if question is None:
            return self, None
        current = self.get(question.question_id)
        if current is None:
            raise ValueError("evidence question must be bound before disposition")
        if call.call_id not in current.tool_call_ids:
            raise ValueError("disposition does not belong to the bound tool action")
        status, kind = {
            ToolActionDisposition.REPLACE: (
                EvidenceQuestionStatus.DROPPED,
                EvidenceObservationKind.ACTION_REPLACED,
            ),
            ToolActionDisposition.CANCEL: (
                EvidenceQuestionStatus.DROPPED,
                EvidenceObservationKind.ACTION_CANCELLED,
            ),
            ToolActionDisposition.WAIT_FOR_USER: (
                EvidenceQuestionStatus.OPEN,
                EvidenceObservationKind.WAITING_FOR_USER,
            ),
            ToolActionDisposition.DENY: (
                EvidenceQuestionStatus.BLOCKED,
                EvidenceObservationKind.ACTION_DENIED,
            ),
        }[disposition]
        updated = replace(
            current, status=status, observation_kind=kind,
            blocking_reason=reason, revision=current.revision + 1,
            updated_event_sequence=event_sequence,
        )
        return self._replace(updated), updated

    def _replace(self, updated: EvidenceQuestionRecord) -> EvidenceQuestionProjection:
        return replace(
            self, records=tuple(
                updated if item.question_id == updated.question_id else item
                for item in self.records
            ),
        )


class EvidenceQuestionProjector:
    """Rebuild lifecycle state deterministically from Runtime Events."""

    @staticmethod
    def project(
        task_id: str, events: Sequence[RuntimeEvent],
    ) -> EvidenceQuestionProjection:
        projection = EvidenceQuestionProjection(task_id)
        for event in sorted(events, key=lambda item: item.sequence):
            if event.task_id != task_id:
                raise ValueError("evidence question event belongs to another task")
            if event.event_type == "evidence.question_bound":
                projection = projection.bind(
                    EvidenceQuestion(
                        str(event.payload["question_id"]),
                        str(event.payload["question"]),
                        expected_scope=str(event.payload.get("expected_scope", "")),
                    ),
                    str(event.payload["turn_id"]),
                    str(event.payload["tool_call_id"]),
                    event_sequence=event.sequence,
                )
            elif event.event_type == "evidence.question_state_changed":
                raw = event.payload.get("record")
                if not isinstance(raw, Mapping):
                    raise ValueError("evidence question state event lost its record")
                record = EvidenceQuestionRecord.from_data(raw)
                if record.source_task_id != task_id:
                    raise ValueError("evidence question state belongs to another task")
                if projection.get(record.question_id) is None:
                    raise ValueError("evidence question state changed before binding")
                projection = projection._replace(record)
        return projection
