"""Deterministic, inspectable routing for input received during a Task."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .approval import ApprovalDecision
from .configuration import canonical_hash


class SessionContinuationMode(StrEnum):
    """Safe execution result after a Session Task was explicitly selected."""

    RECOVER_TASK = "RECOVER_TASK"
    RESUME_CONTINUATION = "RESUME_CONTINUATION"
    CREATE_FOLLOW_UP = "CREATE_FOLLOW_UP"
    AWAIT_USER_ACTION = "AWAIT_USER_ACTION"
    BLOCKED = "BLOCKED"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    EMPTY = "EMPTY"


class SessionResumeSafety(StrEnum):
    """How Runtime may use one durable Task checkpoint."""

    EXACT_RESUME = "EXACT_RESUME"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    REBASE_REQUIRED = "REBASE_REQUIRED"
    AWAIT_USER_ACTION = "AWAIT_USER_ACTION"
    REQUIRES_VALIDATION = "REQUIRES_VALIDATION"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class SessionResumeCandidate:
    """Authority-free index entry for one unfinished Session Task.

    Runtime derives this view from the authoritative Task snapshot. It is safe
    to show to the model or CLI because it contains no Tool arguments/results,
    approvals, credentials, process handles, or permission grants.
    """

    task_id: str
    goal: str
    task_state: str
    workspace: str
    safety: SessionResumeSafety
    reason_code: str
    checkpoint_revision: int | None = None
    conflict_reasons: tuple[str, ...] = ()
    rebase_reasons: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "task_state": self.task_state,
            "workspace": self.workspace,
            "safety": self.safety.value,
            "reason_code": self.reason_code,
            "checkpoint_revision": self.checkpoint_revision,
            "conflict_reasons": list(self.conflict_reasons),
            "rebase_reasons": list(self.rebase_reasons),
        }


@dataclass(frozen=True, slots=True)
class SessionContinuationDecision:
    mode: SessionContinuationMode
    task_id: str | None
    task_state: str | None
    reason_code: str
    resume_safety: SessionResumeSafety | None = None
    candidates: tuple[SessionResumeCandidate, ...] = ()


class SessionInputAction(StrEnum):
    """Semantic action proposed for one ordinary Session input."""

    ANSWER = "ANSWER"
    NEW_TASK = "NEW_TASK"
    RESUME_TASK = "RESUME_TASK"
    CLARIFY = "CLARIFY"


class SessionInputGrounding(StrEnum):
    """Whether the current text can stand alone as a new Task goal.

    This is semantic evidence supplied by the resolver, not an execution
    decision.  Kernel uses it to prevent context-dependent utterances from
    being silently converted into unrelated new Tasks.
    """

    SELF_CONTAINED = "SELF_CONTAINED"
    CONTEXT_DEPENDENT = "CONTEXT_DEPENDENT"
    AMBIGUOUS = "AMBIGUOUS"


class SessionRouteDisposition(StrEnum):
    """What Runtime should do after validating a semantic proposal."""

    ANSWER = "ANSWER"
    CREATE_TASK = "CREATE_TASK"
    RESUME_TASK = "RESUME_TASK"
    CLARIFY = "CLARIFY"


class SessionTaskRelation(StrEnum):
    """Semantic relationship between the input and prior Session work."""

    INDEPENDENT = "INDEPENDENT"
    # A safe, isolated Task that receives ordinary Session context but is not
    # bound to, resumed from, or authorized by any particular historical Task.
    CONTEXTUAL = "CONTEXTUAL"
    CONTINUE = "CONTINUE"
    FOLLOW_UP = "FOLLOW_UP"
    BRANCH = "BRANCH"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class SessionTaskCatalogEntry:
    """Bounded, authority-free Task reference supplied to routing models.

    It is an index, not a checkpoint.  In particular it carries no approvals,
    permission grants, Tool arguments/results, process handles, or credentials.
    """

    task_id: str
    goal: str
    task_state: str
    workspace: str
    completed_work: tuple[str, ...] = ()
    remaining_work: tuple[str, ...] = ()
    verification_status: str | None = None
    outcome_summaries: tuple[str, ...] = ()
    resume_safety: SessionResumeSafety | None = None
    recency_index: int = 0
    is_conversation_anchor: bool = False
    phase1_state: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.task_state in {"SUCCEEDED", "CANCELLED", "FAILED"}

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "goal": self.goal,
            "task_state": self.task_state,
            "phase1_state": self.phase1_state,
            "workspace": self.workspace,
            "completed_work": list(self.completed_work),
            "remaining_work": list(self.remaining_work),
            "verification_status": self.verification_status,
            "outcome_summaries": list(self.outcome_summaries),
            "resume_safety": (
                self.resume_safety.value if self.resume_safety else None
            ),
            "recency_index": self.recency_index,
            "is_conversation_anchor": self.is_conversation_anchor,
        }


@dataclass(frozen=True, slots=True)
class SessionInputDecision:
    disposition: SessionRouteDisposition
    relation: SessionTaskRelation
    source_task_id: str | None
    resolved_goal: str | None
    confidence: float
    reason_code: str
    input_grounding: SessionInputGrounding
    clarification: str | None = None
    resolver_version: str = "runtime-route-v2"
    candidates: tuple[SessionResumeCandidate, ...] = ()
    task_catalog: tuple[SessionTaskCatalogEntry, ...] = ()
    candidate_task_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("Session input confidence must be between 0 and 1")
        if self.disposition is SessionRouteDisposition.ANSWER:
            if (
                self.relation is not SessionTaskRelation.INDEPENDENT
                or self.source_task_id is not None
            ):
                raise ValueError("ANSWER requires an independent relation")
        if self.disposition is SessionRouteDisposition.RESUME_TASK:
            if not self.source_task_id:
                raise ValueError("RESUME_TASK requires source_task_id")
            if self.relation is not SessionTaskRelation.CONTINUE:
                raise ValueError("RESUME_TASK requires CONTINUE relation")
        if (
            self.disposition is SessionRouteDisposition.CREATE_TASK
            and self.relation in {
                SessionTaskRelation.FOLLOW_UP, SessionTaskRelation.BRANCH
            }
            and not self.source_task_id
        ):
            raise ValueError("derived CREATE_TASK requires source_task_id")

    @property
    def action(self) -> SessionInputAction:
        """Compatibility view for callers using the v1 action names."""
        return {
            SessionRouteDisposition.ANSWER: SessionInputAction.ANSWER,
            SessionRouteDisposition.CREATE_TASK: SessionInputAction.NEW_TASK,
            SessionRouteDisposition.RESUME_TASK: SessionInputAction.RESUME_TASK,
            SessionRouteDisposition.CLARIFY: SessionInputAction.CLARIFY,
        }[self.disposition]

    @property
    def task_id(self) -> str | None:
        """Compatibility alias for the selected source Task."""
        return self.source_task_id


class FollowUpMode(StrEnum):
    AUTO = "AUTO"
    STEER = "STEER"
    QUEUE = "QUEUE"


@dataclass(frozen=True, slots=True)
class QueuedFollowUp:
    input_id: str
    session_id: str
    after_task_id: str
    text: str
    inbound_sequence: int


class RuntimeInputIntent(StrEnum):
    STEER = "STEER"
    REPLACE = "REPLACE"
    REVIEW_PENDING_ACTION = "REVIEW_PENDING_ACTION"
    STATUS_QUERY = "STATUS_QUERY"
    NEW_TASK_AFTER_CURRENT = "NEW_TASK_AFTER_CURRENT"
    #: Unified user-input entry only: start an independent Task now.
    NEW_TASK = "NEW_TASK"
    #: Unified user-input entry only: stop the running Task (recoverable).
    INTERRUPT = "INTERRUPT"
    AMBIGUOUS = "AMBIGUOUS"


class InputChannel(StrEnum):
    """The three ingress channels defined by the Phase 1 redesign."""

    STRUCTURED_EVENT = "STRUCTURED_EVENT"
    RUNTIME_TEXT = "RUNTIME_TEXT"
    TASK_TEXT = "TASK_TEXT"


@dataclass(frozen=True, slots=True)
class RuntimeTextInput:
    """Ordinary text for a selected active Task; never a protocol reply."""

    task_id: str
    text: str
    input_id: str
    explicit_intent: RuntimeInputIntent | None = None
    fallback_intent: RuntimeInputIntent | None = None

    @property
    def channel(self) -> InputChannel:
        return InputChannel.RUNTIME_TEXT


@dataclass(frozen=True, slots=True)
class ApprovalResolutionInput:
    """Explicit user approval bound to one pending approval request."""

    request_id: str
    decision: ApprovalDecision
    reason: str

    @property
    def channel(self) -> InputChannel:
        return InputChannel.STRUCTURED_EVENT


@dataclass(frozen=True, slots=True)
class ClarificationReplyInput:
    """Explicit user answer bound to one pending clarification request."""

    request_id: str
    resume_token: str
    answer: str | None = None
    selected_choice: str | None = None

    @property
    def channel(self) -> InputChannel:
        return InputChannel.STRUCTURED_EVENT


@dataclass(frozen=True, slots=True)
class InterruptTaskInput:
    """Explicit interruption command for one active Task."""

    task_id: str
    reason: str = "model invocation interrupted by user"

    @property
    def channel(self) -> InputChannel:
        return InputChannel.STRUCTURED_EVENT


@dataclass(frozen=True, slots=True)
class CancelTaskInput:
    """Explicit cancellation command for one non-terminal Task."""

    task_id: str
    reason: str = "task cancelled by user"

    @property
    def channel(self) -> InputChannel:
        return InputChannel.STRUCTURED_EVENT


@dataclass(frozen=True, slots=True)
class SessionTextInput:
    """Ordinary session text without a preselected active Task."""

    session_id: str
    text: str
    input_id: str
    workspace: str | None = None

    @property
    def channel(self) -> InputChannel:
        return InputChannel.TASK_TEXT


RuntimeInputEvent = (
    RuntimeTextInput | SessionTextInput | ApprovalResolutionInput
    | ClarificationReplyInput | InterruptTaskInput | CancelTaskInput
)


@dataclass(frozen=True, slots=True)
class RuntimeInputContext:
    task_state: str
    current_goal: str = ""
    awaiting_clarification: bool = False
    awaiting_approval: bool = False
    pending_approval_kind: str = ""
    pending_approval_action: str = ""
    pending_approval_target: str = ""
    pending_approval_risk: str = ""

    def to_classifier_data(self) -> dict[str, Any]:
        return {
            "task_state": self.task_state,
            "current_goal": self.current_goal,
            "awaiting_clarification": self.awaiting_clarification,
            "awaiting_approval": self.awaiting_approval,
            "pending_approval": ({
                "kind": self.pending_approval_kind,
                "action": self.pending_approval_action,
                "target": self.pending_approval_target,
                "risk": self.pending_approval_risk,
            } if self.awaiting_approval else None),
        }


@dataclass(frozen=True, slots=True)
class RuntimeInputRoute:
    intent: RuntimeInputIntent
    confidence: float
    reason_code: str
    requires_confirmation: bool
    applied: bool = False
    router_version: str = "deterministic-v1"

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("runtime input confidence must be between 0 and 1")

    def with_applied(self, applied: bool) -> RuntimeInputRoute:
        return replace(self, applied=applied)

    def to_data(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value, "confidence": self.confidence,
            "reason_code": self.reason_code,
            "requires_confirmation": self.requires_confirmation,
            "applied": self.applied, "router_version": self.router_version,
        }

    def to_event_data(self, text: str, input_id: str) -> dict[str, Any]:
        """Persist decision metadata, never the user's plaintext input."""
        return {
            **self.to_data(), "input_id": input_id,
            "text_hash": canonical_hash(text.strip()),
        }


class RuntimeInputRouter:
    """Apply only explicit UI choices and protocol-state safety gates.

    Ordinary language is deliberately not interpreted here. Callers must use
    an explicit protocol intent or a deterministic mode default.
    """

    def route(
        self, text: str, context: RuntimeInputContext,
        explicit_intent: RuntimeInputIntent | None = None,
        fallback_intent: RuntimeInputIntent | None = None,
        classified_intent: RuntimeInputIntent | None = None,
        classified_confidence: float | None = None,
    ) -> RuntimeInputRoute:
        normalized = text.strip()
        if not normalized:
            raise ValueError("runtime input must not be empty")
        if len(normalized) > 20_000:
            raise ValueError("runtime input exceeds 20000 characters")
        if explicit_intent is not None:
            if explicit_intent not in {
                RuntimeInputIntent.STEER, RuntimeInputIntent.REPLACE,
                RuntimeInputIntent.NEW_TASK_AFTER_CURRENT,
            }:
                raise ValueError("unsupported explicit runtime input intent")
            return RuntimeInputRoute(
                explicit_intent, 1.0, "explicit_override", False,
                router_version="explicit-v1",
            )
        if context.awaiting_approval:
            return RuntimeInputRoute(
                RuntimeInputIntent.AMBIGUOUS, 1.0,
                "approval_protected_semantic_routing", True,
            )
        if context.awaiting_clarification:
            return RuntimeInputRoute(
                RuntimeInputIntent.AMBIGUOUS, 1.0,
                "clarification_requires_structured_reply", True,
            )
        if classified_intent is not None:
            if classified_intent not in {
                RuntimeInputIntent.STEER, RuntimeInputIntent.REPLACE,
                RuntimeInputIntent.NEW_TASK_AFTER_CURRENT,
                RuntimeInputIntent.STATUS_QUERY,
                RuntimeInputIntent.REVIEW_PENDING_ACTION,
            }:
                raise ValueError("unsupported classified runtime input intent")
            confidence = classified_confidence if classified_confidence is not None else 0.0
            if not 0 <= confidence <= 1:
                raise ValueError("classified runtime input confidence is invalid")
            return RuntimeInputRoute(
                classified_intent, confidence, "semantic_runtime_routing", False,
                router_version="semantic-v1",
            )
        if fallback_intent is not None:
            if fallback_intent is not RuntimeInputIntent.STEER:
                raise ValueError("only STEER is supported as a fallback intent")
            return RuntimeInputRoute(
                RuntimeInputIntent.STEER, 1.0,
                "follow_up_mode_default", False,
                router_version="follow-up-mode-v1",
            )
        return RuntimeInputRoute(
            RuntimeInputIntent.AMBIGUOUS, 0.45, "no_decisive_rule", True,
        )
