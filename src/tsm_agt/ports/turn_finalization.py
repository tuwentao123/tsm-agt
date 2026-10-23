"""Ports and immutable values used at the end of an Agent turn."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .adapter import RuntimeAdapter
from .model import FinishReason, Message


class FinalizationAction(StrEnum):
    ACCEPT = "ACCEPT"
    CONTINUE = "CONTINUE"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class FinalizationDecision:
    action: FinalizationAction
    reason_code: str
    instruction: str = ""

    def __post_init__(self) -> None:
        if not self.reason_code.strip() or len(self.reason_code) > 64:
            raise ValueError("finalization reason_code must contain 1..64 characters")
        if len(self.instruction) > 2_000:
            raise ValueError("finalization instruction must not exceed 2,000 characters")


@dataclass(frozen=True, slots=True)
class ToolExecutionSummary:
    call_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    ok: bool
    error_code: str = ""
    truncated: bool = False
    result_excerpt: str = ""
    result_meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.result_excerpt) > 2_000:
            raise ValueError("tool result excerpt must not exceed 2,000 characters")

    def to_data(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "ok": self.ok,
            "error_code": self.error_code,
            "truncated": self.truncated,
            "result_excerpt": self.result_excerpt,
            "result_meta": dict(self.result_meta),
        }


@dataclass(frozen=True, slots=True)
class FinalizationCandidate:
    task_id: str
    turn_id: str
    source_user_message: Message
    assistant_message: Message
    finish_reason: FinishReason
    tool_executions: tuple[ToolExecutionSummary, ...]
    model_calls: int
    tool_calls: int
    review_continuations: int
    #: Actual workspace mutation tools registered for this turn. This is a
    #: Runtime fact for the reviewer, not a claim inferred from the answer.
    available_write_tools: tuple[str, ...] = ()


class CompletionReviewAction(StrEnum):
    ACCEPT = "ACCEPT"
    CONTINUE = "CONTINUE"


@dataclass(frozen=True, slots=True)
class CompletionReviewVerdict:
    action: CompletionReviewAction
    reason_code: str
    reason: str
    missing: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.reason_code.strip() or len(self.reason_code) > 64:
            raise ValueError("review reason_code must contain 1..64 characters")
        if not self.reason.strip() or len(self.reason) > 1_000:
            raise ValueError("review reason must contain 1..1000 characters")
        if len(self.missing) > 8 or any(len(item) > 300 for item in self.missing):
            raise ValueError("review missing must contain at most 8 short items")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("review token usage must not be negative")


class CompletionReviewMode(StrEnum):
    ALWAYS = "always"
    #: Only review a Turn whose Task SPEC declares machine-checkable delivery
    #: or evidence obligations.  Ordinary chat remains on the primary model
    #: loop and its deterministic Runtime gate.
    REQUIRED = "required"
    OFF = "off"


class CompletionReviewFailurePolicy(StrEnum):
    #: Commit the already-produced answer, mark it as non-replayable, and emit
    #: an audit event.  A quality reviewer is not an action-safety boundary.
    VISIBLE_UNVERIFIED = "visible_unverified"
    INCOMPLETE = "incomplete"
    ACCEPT = "accept"


class CompletionReviewerPort(RuntimeAdapter, Protocol):
    async def review(
        self, candidate: FinalizationCandidate,
    ) -> CompletionReviewVerdict: ...


class TurnStopPolicyPort(RuntimeAdapter, Protocol):
    async def evaluate(
        self, candidate: FinalizationCandidate,
    ) -> CompletionReviewVerdict | None: ...


class CompletionReviewError(RuntimeError):
    """The reviewer could not produce a valid bounded verdict."""

    def __init__(self, message: str, *, reason_code: str = "reviewer_failed") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class TurnFinalizationFailed(RuntimeError):
    """A Runtime finalization invariant was violated."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"turn finalization failed: {reason_code}")
