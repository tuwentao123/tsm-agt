"""Build auditable active-context shadows without persisting prompt bodies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsm_agt.ports import RuntimeEvent

from ..agent_loop import AgentTurnCheckpoint
from ..configuration import canonical_hash
from ..evidence_question import EvidenceQuestionProjection
from ..session_context import SessionPromptProjection
from ..task import TaskSnapshot
from ..working_memory import EffectiveWorkingMemory
from .task_projection import TaskOrchestrationProjector
from .types import (
    ActiveContextProjection,
    IntentAttempt,
    TaskOrchestrationMetadata,
)


@dataclass(frozen=True, slots=True)
class ActiveContextShadow:
    """Redacted audit view of an :class:`ActiveContextProjection`.

    The in-memory projection retains the authoritative root goal needed to
    construct active context. The append-only event derived from this type
    retains only hashes, structured obligations, and references; it never
    copies model messages, rendered prompts, or assistant prose.
    """

    projection: ActiveContextProjection
    root_goal_hash: str
    projection_hash: str
    source_event_sequences: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.root_goal_hash or not self.projection_hash:
            raise ValueError("active context shadow hashes are required")
        if tuple(sorted(set(self.source_event_sequences))) != (
            self.source_event_sequences
        ):
            raise ValueError("active context shadow sources must be sorted and unique")

    @property
    def turn_id(self) -> str:
        return str(self.projection.turn["turn_id"])

    @property
    def model_call(self) -> int:
        return int(self.projection.turn["model_call"])

    def event_payload(self) -> dict[str, Any]:
        """Return a body-free payload for ``context.single_loop_shadow_projected``."""
        return {
            "schema_version": 1,
            "turn_id": self.turn_id,
            "model_call": self.model_call,
            "projection_hash": self.projection_hash,
            "root_goal_hash": self.root_goal_hash,
            "source_event_sequences": list(self.source_event_sequences),
            "active_context": self.audit_data(),
            "assistant_body_persisted": False,
            "prompt_body_persisted": False,
        }

    def compaction_event_payload(
        self, compaction: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return the shadow receipt paired with ``context.compacted``.

        ``ContextCompactionReceipt.event_data`` already deliberately excludes
        the generated summary body. Keeping that data nested here leaves the
        established ``context.compacted`` contract untouched.
        """
        return {
            "schema_version": 1,
            "turn_id": self.turn_id,
            "model_call": self.model_call,
            "projection_hash": self.projection_hash,
            "root_goal_hash": self.root_goal_hash,
            "source_event_sequences": list(self.source_event_sequences),
            "compaction": dict(compaction),
            "assistant_body_persisted": False,
            "prompt_body_persisted": False,
        }

    def audit_data(self) -> dict[str, Any]:
        """Keep only structured refs needed to review the shadow projection."""
        task = self.projection.task
        authoritative = self.projection.authoritative
        non_authoritative = self.projection.non_authoritative
        return {
            "authority": self.projection.to_data()["authority"],
            "task": {
                "task_id": task["task_id"],
                "state": task["state"],
                "orchestration_mode": task["orchestration_mode"],
                "binding_kind": task["binding_kind"],
                "root_goal_hash": self.root_goal_hash,
            },
            "turn": dict(self.projection.turn),
            "authoritative": {
                "working_memory": dict(authoritative["working_memory"]),
                "open_obligations": [
                    dict(item) for item in authoritative["open_obligations"]
                ],
                "evidence_refs": [
                    dict(item) for item in authoritative["evidence_refs"]
                ],
                "intent_attempts": [
                    dict(item) for item in authoritative["intent_attempts"]
                ],
            },
            "non_authoritative": {
                "session": dict(non_authoritative["session"]),
            },
        }


@dataclass(frozen=True, slots=True)
class ActiveContextProjector:
    """Project runtime-owned context identity for one pending model call."""

    @staticmethod
    def project(
        task: TaskSnapshot,
        checkpoint: AgentTurnCheckpoint,
        working_memory: EffectiveWorkingMemory,
        evidence_questions: EvidenceQuestionProjection,
        session_prompt: SessionPromptProjection,
        task_events: Sequence[RuntimeEvent],
        *,
        model_call: int,
    ) -> ActiveContextShadow:
        if checkpoint.task_id != task.task_id:
            raise ValueError("checkpoint belongs to a different Task")
        if working_memory.snapshot.task_id != task.task_id:
            raise ValueError("working memory belongs to a different Task")
        if evidence_questions.task_id != task.task_id:
            raise ValueError("evidence questions belong to a different Task")
        if model_call < 1:
            raise ValueError("model_call must be positive")

        metadata = TaskOrchestrationProjector.project(
            task.task_id, task.goal, task_events
        )
        source_sequences = ActiveContextProjector._source_event_sequences(
            task.task_id,
            checkpoint.turn_id,
            task_events,
            working_memory,
            evidence_questions,
            session_prompt,
        )
        projection = ActiveContextProjection(
            schema_version=1,
            task=ActiveContextProjector._task_data(task, metadata),
            turn={
                "turn_id": checkpoint.turn_id,
                "model_call": model_call,
                "checkpoint_revision": checkpoint.revision,
                "checkpoint_hash": checkpoint.checkpoint_hash,
            },
            authoritative={
                "working_memory": {
                    "base_revision": working_memory.snapshot.revision,
                    "base_content_hash": working_memory.snapshot.content_hash,
                    "effective_projection_hash": working_memory.projection_hash,
                    "source_event_sequences": list(
                        sorted(set(
                            working_memory.snapshot.source_event_sequences
                            + working_memory.runtime_source_event_sequences
                        ))
                    ),
                },
                "open_obligations": ActiveContextProjector._obligations(
                    evidence_questions
                ),
                "evidence_refs": ActiveContextProjector._evidence_refs(
                    working_memory
                ),
                "intent_attempts": ActiveContextProjector._intent_attempts(
                    task.task_id,
                    checkpoint.turn_id,
                    task_events,
                ),
            },
            non_authoritative={
                "session": {
                    "session_id": checkpoint.session_id or task.session_id,
                    "revision": session_prompt.revision,
                    "content_hash": session_prompt.content_hash,
                    "recent_message_count": session_prompt.recent_message_count,
                    "summarized_message_count": (
                        session_prompt.summarized_message_count
                    ),
                    "summary_revision": session_prompt.summary_revision,
                    "summary_hash": session_prompt.summary_hash,
                    "source_event_sequences": list(
                        session_prompt.source_event_sequences
                    ),
                    "summary_source_event_sequences": list(
                        session_prompt.summary_source_event_sequences
                    ),
                },
            },
        )
        root_goal_hash = canonical_hash(metadata.root_goal)
        projection_hash = canonical_hash({
            "projection": projection.to_data(),
            "root_goal_hash": root_goal_hash,
            "source_event_sequences": list(source_sequences),
        })
        return ActiveContextShadow(
            projection, root_goal_hash, projection_hash, source_sequences
        )

    @staticmethod
    def _task_data(
        task: TaskSnapshot, metadata: TaskOrchestrationMetadata,
    ) -> dict[str, str]:
        return {
            "task_id": task.task_id,
            "root_goal": metadata.root_goal,
            "state": task.state.value,
            "orchestration_mode": metadata.orchestration_mode.value,
            "binding_kind": metadata.binding_kind.value,
        }

    @staticmethod
    def _obligations(
        questions: EvidenceQuestionProjection,
    ) -> list[dict[str, Any]]:
        return [
            {
                "question_ref": record.question_ref,
                "status": record.status.value,
                "source_turn_id": record.source_turn_id,
                "expected_scope": record.expected_scope,
                "blocking_reason": record.blocking_reason,
                "evidence_refs": list(record.evidence_references),
                "updated_event_sequence": record.updated_event_sequence,
            }
            for record in questions.records
            if record.status.value in {"OPEN", "BLOCKED"}
        ][:50]

    @staticmethod
    def _evidence_refs(
        working_memory: EffectiveWorkingMemory,
    ) -> list[dict[str, str]]:
        return [
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "reference": item.reference,
            }
            for item in working_memory.evidence[:50]
        ]

    @staticmethod
    def _intent_attempts(
        task_id: str,
        turn_id: str,
        events: Sequence[RuntimeEvent],
    ) -> list[dict[str, Any]]:
        terminal_types = {
            "tool.intent_accepted",
            "tool.intent_normalized",
            "tool.intent_rejected",
        }
        attempts: list[dict[str, Any]] = []
        seen_call_ids: set[str] = set()
        for event in reversed(events):
            if event.task_id != task_id or event.event_type not in terminal_types:
                continue
            try:
                attempt = IntentAttempt.from_data(event.payload)
            except (KeyError, TypeError, ValueError):
                continue
            if attempt.turn_id != turn_id or attempt.tool_call_id in seen_call_ids:
                continue
            seen_call_ids.add(attempt.tool_call_id)
            validation_error = attempt.validation_error
            attempts.append({
                "attempt_id": attempt.attempt_id,
                "tool_call_id": attempt.tool_call_id,
                "requested_intent": attempt.requested_intent,
                "normalized_intent": (
                    attempt.normalized_intent.value
                    if attempt.normalized_intent is not None else None
                ),
                "status": attempt.status.value,
                "implied_effects": [
                    effect.value for effect in attempt.implied_effects
                ],
                "legacy_input": attempt.legacy_input,
                "normalized_effect_aliases": [
                    {"received": received, "canonical": canonical}
                    for received, canonical in attempt.normalized_effect_aliases
                ],
                "validation_error": (
                    {
                        "field": validation_error.field,
                        "expected": list(validation_error.expected),
                        "normalized_to": validation_error.normalized_to,
                    }
                    if validation_error is not None else None
                ),
            })
            if len(attempts) == 10:
                break
        return list(reversed(attempts))

    @staticmethod
    def _source_event_sequences(
        task_id: str,
        turn_id: str,
        task_events: Sequence[RuntimeEvent],
        working_memory: EffectiveWorkingMemory,
        evidence_questions: EvidenceQuestionProjection,
        session_prompt: SessionPromptProjection,
    ) -> tuple[int, ...]:
        sources = {
            event.sequence
            for event in task_events
            if event.task_id == task_id and event.event_type == "task.created"
        }
        sources.update(working_memory.snapshot.source_event_sequences)
        sources.update(working_memory.runtime_source_event_sequences)
        sources.update(
            record.updated_event_sequence
            for record in evidence_questions.records
            if record.updated_event_sequence > 0
        )
        sources.update(session_prompt.source_event_sequences)
        sources.update(session_prompt.summary_source_event_sequences)
        sources.update(
            event.sequence
            for event in task_events
            if event.task_id == task_id
            and event.event_type in {
                "tool.intent_accepted",
                "tool.intent_normalized",
                "tool.intent_rejected",
            }
            and event.payload.get("turn_id") == turn_id
        )
        return tuple(sorted(sources))
