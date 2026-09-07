"""Replaceable, project-neutral contracts for evidence-guided exploration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .adapter import RuntimeAdapter
from .evidence_delta import EvidenceDelta, EvidenceInventory
from .semantic_action import SemanticAction
from .tool import ToolCall, ToolResult


class EvidenceRelationKind(StrEnum):
    """Project-neutral ways a proposed action may relate to known evidence."""
    SAME_QUESTION = "SAME_QUESTION"
    SAME_TARGET = "SAME_TARGET"
    KNOWN_EVIDENCE = "KNOWN_EVIDENCE"
    NEW_QUESTION = "NEW_QUESTION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class EvidenceRelation:
    """One non-authoritative clue connecting a proposed call to known evidence.

    Providers return only hashes/identifiers and a confidence.  A relation is not
    permission: Sandbox, approval, workspace and tool-risk checks still run later.
    """

    kind: EvidenceRelationKind
    source_id: str
    subject_hash: str = ""
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("evidence relation source_id must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("evidence relation confidence must be within 0..1")


@dataclass(frozen=True, slots=True)
class EvidenceRelationState:
    """Checkpointed evidence route history; it stores no raw source content."""

    question_ids: tuple[str, ...] = ()
    target_hashes: tuple[str, ...] = ()
    successful_calls: int = 0
    schema_version: int = 1

    @classmethod
    def from_data(cls, data: Mapping[str, Any] | None) -> "EvidenceRelationState":
        if not data:
            return cls()
        questions = data.get("question_ids", ())
        targets = data.get("target_hashes", ())
        if not isinstance(questions, (list, tuple)) or not all(
            isinstance(item, str) and item for item in questions
        ):
            raise ValueError("evidence relation question_ids must be strings")
        if not isinstance(targets, (list, tuple)) or not all(
            isinstance(item, str) and item for item in targets
        ):
            raise ValueError("evidence relation target_hashes must be strings")
        successful_calls = int(data.get("successful_calls", 0))
        if successful_calls < 0:
            raise ValueError("successful_calls must not be negative")
        schema_version = int(data.get("schema_version", 1))
        if schema_version != 1:
            raise ValueError("unsupported evidence relation state schema version")
        return cls(tuple(questions), tuple(targets), successful_calls, schema_version)

    def to_data(self) -> dict[str, Any]:
        return {
            "question_ids": list(self.question_ids),
            "target_hashes": list(self.target_hashes),
            "successful_calls": self.successful_calls,
            "schema_version": self.schema_version,
        }


class EvidenceRelationAssessmentKind(StrEnum):
    """Soft relation outcomes; none of them represent security authority."""
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    NEEDS_BOUNDED_PROBE = "NEEDS_BOUNDED_PROBE"


@dataclass(frozen=True, slots=True)
class EvidenceRelationAssessment:
    """Singleton policy output consumed by the Core coordinator."""
    kind: EvidenceRelationAssessmentKind
    reason: str
    relation_kind: str = "UNKNOWN"


class RejectionRecoveryAction(StrEnum):
    """Standard recovery actions a replaceable rejection policy may request."""
    RETURN_FEEDBACK = "RETURN_FEEDBACK"
    REWRITE = "REWRITE"
    ALLOW_BOUNDED_PROBE = "ALLOW_BOUNDED_PROBE"
    STOP_ROUTE = "STOP_ROUTE"
    ASK_USER = "ASK_USER"


@dataclass(frozen=True, slots=True)
class RejectionLoopState:
    """Counts repeated soft rejections by question+action identity."""

    rejection_counts: Mapping[str, int] = None  # type: ignore[assignment]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.rejection_counts is None:
            object.__setattr__(self, "rejection_counts", {})
        if any(not key or int(value) < 0 for key, value in self.rejection_counts.items()):
            raise ValueError("rejection counts require non-empty keys and positive values")
        if self.schema_version != 1:
            raise ValueError("unsupported rejection loop state schema version")

    @classmethod
    def from_data(cls, data: Mapping[str, Any] | None) -> "RejectionLoopState":
        if not data:
            return cls({}, 1)
        raw = data.get("rejection_counts", {})
        if not isinstance(raw, Mapping):
            raise ValueError("rejection_counts must be an object")
        return cls(
            {str(key): int(value) for key, value in raw.items()},
            int(data.get("schema_version", 1)),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "rejection_counts": dict(self.rejection_counts),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class RejectionRecoveryDecision:
    """Recovery action plus auditable collision identity and next state."""
    action: RejectionRecoveryAction
    reason: str
    rejection_identity: str
    occurrence: int
    state: RejectionLoopState


class ExplorationOutcomeAction(StrEnum):
    """Standard next-route actions selected after a tool result."""
    CONTINUE = "CONTINUE"
    CHANGE_METHOD = "CHANGE_METHOD"
    STOP_ROUTE = "STOP_ROUTE"
    WRAP_UP = "WRAP_UP"
    ASK_USER = "ASK_USER"


@dataclass(frozen=True, slots=True)
class ExplorationOutcomeState:
    """Checkpointed count of consecutive tool results with no new evidence."""

    consecutive_no_evidence: int = 0
    last_action: str = ""
    schema_version: int = 1

    @classmethod
    def from_data(cls, data: Mapping[str, Any] | None) -> "ExplorationOutcomeState":
        if not data:
            return cls()
        count = int(data.get("consecutive_no_evidence", 0))
        if count < 0:
            raise ValueError("consecutive_no_evidence must not be negative")
        schema_version = int(data.get("schema_version", 1))
        if schema_version != 1:
            raise ValueError("unsupported exploration outcome state schema version")
        return cls(count, str(data.get("last_action", "")), schema_version)

    def to_data(self) -> dict[str, Any]:
        return {
            "consecutive_no_evidence": self.consecutive_no_evidence,
            "last_action": self.last_action,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ExplorationOutcomeDecision:
    """Outcome-policy result and its checkpoint-ready state."""
    action: ExplorationOutcomeAction
    reason: str
    state: ExplorationOutcomeState


class EvidenceRelationProviderPort(RuntimeAdapter, Protocol):
    """Discovers project-neutral clues; it never grants tool authority.

    Multiple providers may be registered and their outputs are combined. Inputs
    are a proposed call plus redacted/checkpointed evidence state; output is a
    tuple of relation clues. Replacing a provider requires Bootstrap registration
    only. Security checks remain owned by Kernel/Sandbox, not this Port.
    """

    async def discover(
        self, call: ToolCall, semantic_action: SemanticAction | None,
        state: EvidenceRelationState, inventory: EvidenceInventory,
    ) -> tuple[EvidenceRelation, ...]: ...


class EvidenceRelationPolicyPort(RuntimeAdapter, Protocol):
    """Chooses whether evidence supports an exploration step.

    Exactly one implementation is registered. It consumes clues, not project-type
    names, and returns a soft exploration decision. It cannot bypass workspace,
    Sandbox, approval or risk checks. Replacement is a Bootstrap profile choice.
    """

    async def assess(
        self, call: ToolCall, semantic_action: SemanticAction | None,
        relations: tuple[EvidenceRelation, ...], state: EvidenceRelationState,
    ) -> EvidenceRelationAssessment: ...


class RejectionLoopPolicyPort(RuntimeAdapter, Protocol):
    """Selects recovery after a soft exploration rejection repeats.

    Exactly one implementation is registered. It receives a stable rejection
    identity and returns feedback/probe/stop behavior plus checkpoint state. It
    owns no tool execution and grants no permissions.
    """

    async def decide(
        self, call: ToolCall, assessment: EvidenceRelationAssessment,
        rejection_identity: str, state: RejectionLoopState,
    ) -> RejectionRecoveryDecision: ...


class ExplorationOutcomePolicyPort(RuntimeAdapter, Protocol):
    """Chooses the next route after a tool result.

    Exactly one implementation is registered. It maps result/evidence progress to
    continue, change-method, stop, wrap-up or ask-user. It does not execute tools,
    inspect project types, or alter authority.
    """

    async def evaluate(
        self, call: ToolCall, result: ToolResult, evidence_delta: EvidenceDelta | None,
        state: ExplorationOutcomeState,
    ) -> ExplorationOutcomeDecision: ...
