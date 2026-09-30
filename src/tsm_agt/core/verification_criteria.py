"""Acceptance-criteria seeding for ``Kernel.verify_task_acceptance``.

Sibling of ``core/verification.py`` (domain types) holding the pure stage that
turns already-computed verification projections into the initial
``AcceptanceResult`` list. It reads no Kernel state, which is why it lives here
rather than being a method: the "SPEC §7 P2 three-stage split" ordering comment
below is the whole contract.

Imported by ``kernel.py`` at module level; this module never imports ``kernel``.
"""

from __future__ import annotations

from .evidence_question import (
    EvidenceQuestionRecord,
)
from .task_spec import (
    TaskAcceptanceCriterion,
    TaskSpecSnapshot,
)
from .verification import (
    AcceptanceResult,
    AcceptanceStatus,
    Evidence,
)
from tsm_agt.ports import (
    FinalAcceptanceAction,
    FinalAcceptanceDecision,
)


def seed_acceptance_criteria(
    *,
    task_spec: TaskSpecSnapshot,
    mandatory_answer_completeness: bool,
    answer_result: AcceptanceResult | None,
    answer_evidence_result: AcceptanceResult | None,
    goal_command_result: AcceptanceResult | None,
    incomplete_questions: tuple[EvidenceQuestionRecord, ...],
    final_acceptance: FinalAcceptanceDecision | None,
) -> tuple[list[AcceptanceResult], list[TaskAcceptanceCriterion]]:
    """Seed the criteria list from the deterministic projections.

    Returns the criteria accumulated so far plus the (still empty) list that the
    later semantic stage appends deferred judgement criteria to.
    """
    criteria: list[AcceptanceResult] = []
    # Judgement criteria are *deferred* so the semantic stage always runs
    # after every deterministic criterion (SPEC §7 P2 three-stage split).
    # Two things fall out of that order, both required by the SPEC: a Task
    # that already fails deterministically never pays for a judge call, and
    # a read-only diagnostic caller can reuse the deterministic stage alone.
    deferred_semantic: list[TaskAcceptanceCriterion] = []
    if mandatory_answer_completeness:
        assert answer_result is not None
        criteria.append(answer_result)
    if task_spec.schema_version == 0:
        open_required = tuple(
            outcome for outcome in task_spec.outcomes
            if outcome.required and not outcome.status.is_closed
        )
        criteria.append(AcceptanceResult(
            "task-outcome-fulfillment",
            (AcceptanceStatus.BLOCKED if open_required
             else AcceptanceStatus.PASSED),
            tuple(Evidence(
                "task_outcome",
                "required Task outcome has durable fulfillment",
                f"{outcome.status.value}: {outcome.description}",
                outcome.outcome_id, False,
            ) for outcome in open_required) or (Evidence(
                "task_outcome",
                "required Task outcomes have durable fulfillment",
                "all required outcomes are closed",
                "task-spec", True,
            ),),
        ))
    if answer_evidence_result is not None:
        criteria.append(answer_evidence_result)
    if goal_command_result is not None:
        criteria.append(goal_command_result)
    if incomplete_questions:
        criteria.append(AcceptanceResult(
            "evidence-question-lifecycle", AcceptanceStatus.BLOCKED,
            tuple(Evidence(
                "evidence_question",
                "a tool-bound evidence question obtained a trustworthy result",
                (
                    f"question {record.question_ref} blocked: "
                    f"{record.blocking_reason or 'UNKNOWN'}"
                ),
                record.tool_call_ids[-1] if record.tool_call_ids else "verifier",
                False,
            ) for record in incomplete_questions),
        ))
    if final_acceptance is not None:
        criteria.append(AcceptanceResult(
            "final-evidence-integrity",
            (AcceptanceStatus.PASSED
             if final_acceptance.action is FinalAcceptanceAction.PASS
             else AcceptanceStatus.BLOCKED),
            tuple(
                Evidence(
                    "final_evidence_integrity",
                    "required final conclusions are backed by trusted, "
                    "in-scope persisted evidence",
                    violation.observed, violation.subject_ref, False,
                )
                for violation in final_acceptance.violations
            ) or (Evidence(
                "final_evidence_integrity",
                "required final conclusions are backed by trusted, "
                "in-scope persisted evidence",
                final_acceptance.reason, "final-acceptance-policy", True,
            ),),
        ))
    return criteria, deferred_semantic
