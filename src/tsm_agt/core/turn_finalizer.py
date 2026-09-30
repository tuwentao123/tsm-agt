"""The one place that decides how a turn ended.

Today five vocabularies and dozens of call sites decide a terminal state, so an
undecidable verdict gets pushed to whichever side is nearest -- 115 of 161
recorded failures in this workspace were exactly that. This module is the single
decision point they converge on.

It is a **pure function of typed facts**, deliberately not a bag of strings:

* a positive refutation (``AcceptanceStatus.FAILED``) -- and nothing else -- may
  produce ``failed``;
* an undecidable verdict, a missing judge, or an exhausted budget produces
  ``interrupted`` with a typed reason, which is a resumable boundary and never a
  product failure (INV-14);
* ``completed`` requires that no required gap is open (INV-13), so a caller
  claiming completion while requirements remain is downgraded, not trusted.

Nothing here reads model prose; every input is an enum or a count.
"""

from __future__ import annotations

from dataclasses import dataclass

from tsm_agt.ports import CompletionReadinessAction

from .turn_outcome import ExitReason, TurnOutcome
from .verification import AcceptanceStatus


@dataclass(frozen=True, slots=True)
class TurnFacts:
    """Everything the decision is allowed to depend on."""

    #: Deterministic + semantic acceptance verdict, when verification ran.
    verification: AcceptanceStatus | None = None
    #: The per-turn readiness decision, when the readiness gate ran.
    readiness: CompletionReadinessAction | None = None
    #: Required coverage/readiness gaps still open at the end of the turn.
    open_required_gaps: int = 0
    #: The turn ran out of its model/tool budget.
    budget_exhausted: bool = False
    #: A judged criterion could not be decided (no judge, judge error, ...).
    undecidable: bool = False
    #: The user cancelled.
    cancelled: bool = False
    #: Two authoritative facts disagree.
    conflict: bool = False
    #: Free-form detail carried through for the audit trail.
    detail: str = ""


def finalize_turn(facts: TurnFacts) -> TurnOutcome:
    """Decide the turn outcome. Precedence is deliberate and documented.

    Order: cancel > conflict > refutation > completion > undecidable >
    budget/open requirements > undecided. Refutation outranks completion because
    a proven-wrong delivery must never be reported as success; cancellation
    outranks everything because the user's decision ends the question.
    """

    if facts.cancelled:
        return TurnOutcome(interrupted=True, exit_reason=ExitReason.CANCELLED)
    if facts.conflict:
        return TurnOutcome(interrupted=True, exit_reason=ExitReason.CONFLICT,
                           detail=facts.detail)
    if facts.verification is AcceptanceStatus.FAILED:
        return TurnOutcome(failed=True, exit_reason=ExitReason.REFUTED,
                           detail=facts.detail)
    if _eligible_for_completion(facts):
        return TurnOutcome(completed=True, exit_reason=ExitReason.COMPLETED)
    if facts.undecidable or facts.verification is AcceptanceStatus.BLOCKED:
        return TurnOutcome(interrupted=True, exit_reason=ExitReason.UNDECIDABLE,
                           detail=facts.detail)
    if facts.budget_exhausted:
        return TurnOutcome(interrupted=True,
                           exit_reason=ExitReason.BUDGET_EXHAUSTED,
                           detail=facts.detail)
    if facts.readiness is CompletionReadinessAction.EXHAUSTED:
        return TurnOutcome(interrupted=True,
                           exit_reason=ExitReason.REQUIREMENTS_OPEN,
                           detail=facts.detail)
    if facts.open_required_gaps:
        return TurnOutcome(interrupted=True,
                           exit_reason=ExitReason.REQUIREMENTS_OPEN,
                           detail=facts.detail)
    if facts.verification is AcceptanceStatus.NOT_APPLICABLE:
        return TurnOutcome(interrupted=True, exit_reason=ExitReason.NEEDS_REVIEW,
                           detail=facts.detail)
    # Nothing decided: the turn is still in flight and has no outcome yet. That
    # is a legitimate answer, not a gap to fill with a guess.
    return TurnOutcome(detail=facts.detail)


def _eligible_for_completion(facts: TurnFacts) -> bool:
    """Completion is earned: no open requirement, and nothing undecided.

    A caller that claims completion while a required gap is open is *downgraded*
    here rather than trusted -- INV-13 is a property of the decision, not of the
    caller's honesty.
    """

    if facts.open_required_gaps:
        return False
    if facts.budget_exhausted or facts.undecidable:
        return False
    if facts.verification is not AcceptanceStatus.PASSED:
        return False
    if facts.readiness is not None and (
        facts.readiness is not CompletionReadinessAction.COMPLETE
    ):
        return False
    return True
