"""Conservative, project-neutral evidence-level evaluation."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, EvidenceLevel, EvidenceLevelAssessment,
    EvidenceLevelSignals, HealthState, HealthStatus,
)


class RuleBasedEvidenceLevelEvaluator:
    descriptor = AdapterDescriptor(
        "builtin.rule-based-evidence-level", "1.0.0",
        "EvidenceLevelEvaluatorPort", "1.0",
        frozenset({"project-neutral", "deterministic", "redacted"}),
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
            "evidence level evaluator ready" if self._started else "not started",
        )

    async def assess(
        self, signals: EvidenceLevelSignals
    ) -> EvidenceLevelAssessment:
        if not self._started:
            raise RuntimeError("evidence level evaluator is not started")
        counts = {key: max(0, int(value)) for key, value in signals.evidence_counts.items()}
        total = sum(counts.values())
        verified = (
            max(0, signals.passed_evidence_references)
            + max(0, signals.passed_post_mutation_commands)
        )
        if (
            signals.verification_status in {"failed", "blocked"}
            or signals.failed_criteria > 0
            or signals.blocked_criteria > 0
        ):
            return EvidenceLevelAssessment(
                EvidenceLevel.BLOCKED, total, verified,
                "acceptance_not_satisfied",
            )
        if verified > 0:
            return EvidenceLevelAssessment(
                EvidenceLevel.VERIFIED, total, verified,
                "trusted_verifier_confirmed",
            )
        if (
            counts.get("new_facts", 0) > 0
            or counts.get("new_verification", 0) > 0
        ):
            return EvidenceLevelAssessment(
                EvidenceLevel.DIRECT, total, 0, "artifact_or_structured_fact_observed",
            )
        if any(counts.get(key, 0) > 0 for key in (
            "new_paths", "new_symbols", "new_relations",
            "new_exclusions", "resolved_questions",
        )):
            return EvidenceLevelAssessment(
                EvidenceLevel.INDIRECT, total, 0, "discovery_evidence_only",
            )
        return EvidenceLevelAssessment(
            EvidenceLevel.NONE, total, 0,
            "no_persisted_evidence" if signals.successful_tool_calls == 0
            else "tools_produced_no_usable_evidence",
        )
