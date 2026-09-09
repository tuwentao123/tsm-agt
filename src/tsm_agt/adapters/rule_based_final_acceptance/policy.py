"""Conservative final verification over persisted structured facts."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, FinalAcceptanceAction,
    FinalAcceptanceDecision, FinalAcceptancePolicyPort, FinalAcceptanceProbe,
    FinalAcceptanceViolation, HealthState, HealthStatus,
)


class RuleBasedFinalAcceptancePolicy:
    """Reject incomplete or untraceable required work.

    The policy does not parse user prose, infer a project type, resolve paths,
    or grant authority. Kernel supplies normalized facts from the Task ledger.
    """

    descriptor = AdapterDescriptor(
        "builtin.rule-based-final-acceptance", "1.0.0",
        "FinalAcceptancePolicyPort", "1.0",
        frozenset({"project-neutral", "evidence-integrity", "read-only"}),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "final acceptance policy ready" if self._started else "not started",
        )

    async def evaluate(
        self, probe: FinalAcceptanceProbe,
    ) -> FinalAcceptanceDecision:
        if not self._started:
            raise RuntimeError("final acceptance policy is not started")
        violations: list[FinalAcceptanceViolation] = []
        for question in probe.questions:
            if question.status in {"OPEN", "BLOCKED"}:
                violations.append(FinalAcceptanceViolation(
                    f"{question.status}_QUESTION", question.question_ref,
                    question.blocking_reason or question.status.casefold(),
                ))
                if question.wrong_scope_source_references:
                    violations.append(FinalAcceptanceViolation(
                        "WRONG_SCOPE_EVIDENCE", question.question_ref,
                        "only wrong-scope source attempts were recorded",
                    ))
                continue
            if question.status == "RESOLVED" and not (
                question.trusted_source_references
            ):
                violations.append(FinalAcceptanceViolation(
                    "UNTRUSTED_QUESTION_EVIDENCE", question.question_ref,
                    "resolved question has no committed successful in-scope source",
                ))
        violations.extend(
            FinalAcceptanceViolation(
                "INCOMPLETE_REQUIRED_PLAN", step_ref,
                "required plan step is still pending or in progress",
            )
            for step_ref in probe.required_plan_step_refs
        )
        violations.extend(
            FinalAcceptanceViolation(
                "COMPLETION_GAP_REMAINS", gap_ref,
                "completion readiness still reported a required gap",
            )
            for gap_ref in probe.completion_gap_refs
        )
        if violations:
            return FinalAcceptanceDecision(
                FinalAcceptanceAction.BLOCK,
                "required_final_evidence_is_incomplete_or_untrusted",
                tuple(violations),
            )
        return FinalAcceptanceDecision(
            FinalAcceptanceAction.PASS, "required_final_evidence_is_traceable"
        )
