"""One vocabulary for "how did this turn end", plus the contradictions it forbids.

Modelled on the shape that works in practice (Hermes ``agent/turn_finalizer.py``):

    three booleans + one exit reason

``completed`` / ``failed`` / ``interrupted`` are mutually exclusive, and **all
false means "undecided"** -- a first-class slot, not a gap that every caller has
to fill by guessing. That single property is what this codebase kept losing: it
has ~20 ``TaskState`` values and four more vocabularies
(``CompletionReadinessAction``, ``AcceptanceStatus``, ``CoverageStatus``,
recovery actions), so "I cannot decide" had no home and each caller pushed it to
whichever side was nearest. 115 of 161 recorded failures in this workspace were
exactly that: an *undecidable* verdict written as FAILED.

This module is deliberately **additive and pure**: it does not change any
transition, it gives the eventual single finalizer something to decide *from*,
and it can already point at an existing terminal state whose reason contradicts
it (``outcome_inconsistencies``).
"""

from __future__ import annotations

from dataclasses import dataclass

from .task import TaskState


class ExitReason:
    """Typed reasons. A string carries no meaning unless it is from this set."""

    #: The goal was reached: every required criterion is satisfied.
    COMPLETED = "completed"
    #: Positive refutation: the delivered work is demonstrably wrong.
    REFUTED = "refuted"
    #: The turn ran out of its resource budget with work still open.
    BUDGET_EXHAUSTED = "budget_exhausted"
    #: Required work is open and the runtime stopped on purpose.
    REQUIREMENTS_OPEN = "requirements_open"
    #: Nothing could decide the question (no judge, unreadable evidence, ...).
    UNDECIDABLE = "undecidable"
    #: A human must look at it.
    NEEDS_REVIEW = "needs_review"
    #: Waiting on the user (input or approval).
    AWAITING_INPUT = "awaiting_input"
    #: The user cancelled.
    CANCELLED = "cancelled"
    #: Two authoritative facts disagree.
    CONFLICT = "conflict"


#: Reasons that may never justify ``completed=True``: completion is earned by
#: evidence, so a reason that describes missing/undecided work cannot ride next
#: to success ("``completed: true`` never rides next to ``partial: true``").
_NON_COMPLETION_REASONS = frozenset({
    ExitReason.BUDGET_EXHAUSTED, ExitReason.REQUIREMENTS_OPEN,
    ExitReason.UNDECIDABLE, ExitReason.NEEDS_REVIEW,
    ExitReason.AWAITING_INPUT, ExitReason.CONFLICT, ExitReason.CANCELLED,
})

#: Reasons that may never justify ``failed=True``: they say "we do not know",
#: and not knowing is not the same as being wrong (INV-14).
_NON_FAILURE_REASONS = frozenset({
    ExitReason.UNDECIDABLE, ExitReason.NEEDS_REVIEW, ExitReason.AWAITING_INPUT,
    ExitReason.BUDGET_EXHAUSTED, ExitReason.REQUIREMENTS_OPEN,
})


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """Mutually exclusive; all false means the turn has not concluded."""

    completed: bool = False
    failed: bool = False
    interrupted: bool = False
    exit_reason: str = ""
    detail: str = ""

    @property
    def decided(self) -> bool:
        return self.completed or self.failed or self.interrupted

    @property
    def resumable(self) -> bool:
        """An interrupted turn is a boundary to continue from, never a verdict."""

        return self.interrupted and self.exit_reason in {
            ExitReason.BUDGET_EXHAUSTED, ExitReason.REQUIREMENTS_OPEN,
            ExitReason.AWAITING_INPUT,
        }

    @property
    def contradictions(self) -> tuple[str, ...]:
        found: list[str] = []
        if sum((self.completed, self.failed, self.interrupted)) > 1:
            found.append("MULTIPLE_OUTCOMES")
        if self.completed and self.exit_reason in _NON_COMPLETION_REASONS:
            found.append("COMPLETED_WITH_OPEN_WORK")
        if self.failed and self.exit_reason in _NON_FAILURE_REASONS:
            found.append("FAILED_WITHOUT_REFUTATION")
        if self.decided and not self.exit_reason:
            found.append("DECIDED_WITHOUT_REASON")
        return tuple(found)

    def to_data(self) -> dict[str, object]:
        return {
            "completed": self.completed,
            "failed": self.failed,
            "interrupted": self.interrupted,
            "exit_reason": self.exit_reason,
            "detail": self.detail,
        }


#: How each ``TaskState`` reads on the outcome axis. Phases that mean "still
#: working" map to no outcome at all -- that is the shape that gives "undecided"
#: a home instead of forcing a binary choice.
_STATE_REASONS: dict[TaskState, str] = {
    TaskState.SUCCEEDED: ExitReason.COMPLETED,
    TaskState.FAILED: ExitReason.REFUTED,
    TaskState.NEEDS_REVIEW: ExitReason.NEEDS_REVIEW,
    TaskState.CANCELLED: ExitReason.CANCELLED,
    TaskState.CONFLICT: ExitReason.CONFLICT,
    TaskState.AWAITING_USER: ExitReason.AWAITING_INPUT,
    TaskState.AWAITING_APPROVAL: ExitReason.AWAITING_INPUT,
    TaskState.INTERRUPTING: ExitReason.REQUIREMENTS_OPEN,
    TaskState.INTERRUPTED: ExitReason.REQUIREMENTS_OPEN,
}

#: Transition reasons observed in real ledgers that actually mean "we could not
#: decide" or "we ran out of room". Mapping them to FAILED is the defect this
#: module exists to make visible.
_UNDECIDABLE_REASON_MARKERS = (
    "verifier blocked", "verifier incomplete", "undecidable",
    "needs_review", "needs review",
)
_BUDGET_REASON_MARKERS = (
    "max_model_calls", "max_tool_calls", "limit exceeded",
    "budget", "exhausted",
)


def classify_transition_reason(reason: str) -> str:
    """Read a free-form transition reason into the typed vocabulary.

    Reasons in the ledger are prose composed by whoever finished the turn
    (``"trusted verifier blocked"``, ``"SDK verifier blocked"``, ...). Reading
    them is *classification of our own diagnostics*, not interpretation of model
    output, so it is allowed here where AG-15 forbids parsing model text.
    """

    lowered = (reason or "").casefold()
    if any(marker in lowered for marker in _UNDECIDABLE_REASON_MARKERS):
        return ExitReason.UNDECIDABLE
    if any(marker in lowered for marker in _BUDGET_REASON_MARKERS):
        return ExitReason.BUDGET_EXHAUSTED
    return ""


def derive_turn_outcome(state: TaskState, *, reason: str = "") -> TurnOutcome:
    """Project one terminal/phased state onto the outcome axis.

    Total over every ``TaskState``. A state that is still working yields an
    undecided outcome (all three booleans false) rather than a guess.
    """

    typed = classify_transition_reason(reason)
    if state is TaskState.SUCCEEDED:
        return TurnOutcome(completed=True, exit_reason=ExitReason.COMPLETED)
    if state is TaskState.FAILED:
        # ``FAILED`` is only a refutation when the reason is a refutation. If
        # the reason says "we could not decide", the honest outcome is
        # interrupted -- and this is exactly the check that catches the
        # "blocked verifier wrote FAILED" class.
        if typed in _NON_FAILURE_REASONS:
            return TurnOutcome(
                interrupted=True, exit_reason=typed, detail=reason,
            )
        return TurnOutcome(failed=True, exit_reason=ExitReason.REFUTED,
                           detail=reason)
    if state in {TaskState.NEEDS_REVIEW, TaskState.CANCELLED, TaskState.CONFLICT,
                 TaskState.AWAITING_USER, TaskState.AWAITING_APPROVAL,
                 TaskState.INTERRUPTING, TaskState.INTERRUPTED}:
        return TurnOutcome(
            interrupted=True, exit_reason=_STATE_REASONS[state], detail=reason,
        )
    return TurnOutcome(detail=reason)


def outcome_inconsistencies(state: TaskState, *, reason: str = "") -> tuple[str, ...]:
    """Whether a state/reason pair contradicts the outcome vocabulary.

    A non-empty result is a defect in *our* transition, not in the model's work:
    the writer chose a terminal state that its own reason does not support.
    """

    outcome = derive_turn_outcome(state, reason=reason)
    found = list(outcome.contradictions)
    # The recorded state itself can contradict the reason that produced it: a
    # "we could not decide" reason written as FAILED is the class that produced
    # 115 false failures in this workspace.
    if state is TaskState.FAILED and outcome.interrupted:
        found.append("FAILED_STATE_WITH_UNDECIDABLE_REASON")
    if state is TaskState.SUCCEEDED and outcome.exit_reason != ExitReason.COMPLETED:
        found.append("SUCCEEDED_WITH_UNSUPPORTED_REASON")
    return tuple(found)
