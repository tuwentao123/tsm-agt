"""Conservative status projection using only safe taxonomies and counts."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, EVIDENCE_CATEGORIES, HealthState,
    HealthStatus, InvestigationStatusProjection, InvestigationStatusSignals,
)


class RuleBasedInvestigationStatusProjector:
    descriptor = AdapterDescriptor(
        "builtin.rule-based-investigation-status", "1.0.0",
        "InvestigationStatusProjectorPort", "1.0",
        frozenset({"read-only", "redacted", "project-neutral"}),
    )
    _DECISIONS = frozenset({
        "CONTINUE", "READ_HITS", "NARROW_SCOPE", "CHANGE_METHOD",
        "ASK_USER", "SUMMARIZE_WITH_EVIDENCE", "STOP_NO_PROGRESS",
    })
    _VALUE_BANDS = frozenset({"useful", "low", "ignored"})

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "investigation status projector ready"
            if self._started else "not started",
        )

    async def project(
        self, signals: InvestigationStatusSignals
    ) -> InvestigationStatusProjection:
        if not self._started:
            raise RuntimeError("investigation status projector is not started")
        raw = signals.evidence_counts or {}
        counts = {
            category: max(0, int(raw.get(category, 0)))
            for category in EVIDENCE_CATEGORIES
        }
        question_ref = (
            signals.question_ref[:12]
            if len(signals.question_ref) >= 12
            and all(character in "0123456789abcdef" for character in signals.question_ref[:12].lower())
            else ""
        )
        return InvestigationStatusProjection(
            question_ref=question_ref, evidence_counts=counts,
            evidence_total=sum(counts.values()),
            consecutive_zero_delta=max(0, signals.consecutive_zero_delta),
            scored_actions=max(0, signals.scored_actions),
            budget_score=signals.budget_score,
            value_band=(
                signals.value_band if signals.value_band in self._VALUE_BANDS else ""
            ),
            low_value_streak=max(0, signals.low_value_streak),
            cumulative_tool_milliseconds=max(
                0, signals.cumulative_tool_milliseconds
            ),
            latest_decision=(
                signals.latest_decision
                if signals.latest_decision in self._DECISIONS else ""
            ),
            model_calls=max(0, signals.model_calls),
            tool_calls=max(0, signals.tool_calls),
        )
