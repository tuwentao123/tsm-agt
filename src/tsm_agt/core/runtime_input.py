"""Deterministic, inspectable routing for input received during a Task."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

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


@dataclass(frozen=True, slots=True)
class SessionInputDecision:
    action: SessionInputAction
    task_id: str | None
    confidence: float
    reason_code: str
    input_grounding: SessionInputGrounding
    clarification: str | None = None
    resolver_version: str = "runtime-default-v1"
    candidates: tuple[SessionResumeCandidate, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("Session input confidence must be between 0 and 1")
        if self.action is SessionInputAction.RESUME_TASK and not self.task_id:
            raise ValueError("RESUME_TASK requires task_id")


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
    CLARIFICATION_ANSWER = "CLARIFICATION_ANSWER"
    AMBIGUOUS = "AMBIGUOUS"


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

    Ordinary language is deliberately not interpreted here.  A replaceable
    RuntimeInputClassifierPort owns semantic understanding.
    """

    def route(
        self, text: str, context: RuntimeInputContext,
        explicit_intent: RuntimeInputIntent | None = None,
        fallback_intent: RuntimeInputIntent | None = None,
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
                RuntimeInputIntent.CLARIFICATION_ANSWER, 0.98,
                "pending_clarification", False,
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
