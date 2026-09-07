"""Project-neutral next-move selection from existing runtime signals."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    StopOrPivotAction, StopOrPivotDecision, StopOrPivotSignals,
    StopOrPivotState, StopOrPivotUpdate, ToolCall,
)


class RuleBasedStopOrPivotPolicy:
    descriptor = AdapterDescriptor(
        "builtin.rule-based-stop-or-pivot", "1.0.0",
        "StopOrPivotPolicyPort", "1.0",
        frozenset({"project-neutral", "checkpointed", "redacted-events"}),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "stop-or-pivot policy ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def decide(
        self, call: ToolCall, signals: StopOrPivotSignals,
        state: StopOrPivotState,
    ) -> StopOrPivotUpdate:
        self._require_started()
        if signals.semantic_family == "REQUEST_CLARIFICATION":
            return self._update(StopOrPivotAction.ASK_USER, "explicit_clarification")
        if signals.read_hits_required:
            return self._update(
                StopOrPivotAction.READ_HITS, "bounded_candidates_available",
                candidate_count=signals.candidate_count,
            )
        if signals.scope_reason_required:
            return self._update(
                StopOrPivotAction.NARROW_SCOPE, "unjustified_scope_expansion"
            )
        if signals.plan_finished:
            return self._update(
                StopOrPivotAction.SUMMARIZE_WITH_EVIDENCE,
                "plan_finished", terminal=True,
            )
        if signals.plan_should_stop:
            return self._update(
                StopOrPivotAction.STOP_NO_PROGRESS, "plan_no_progress",
                terminal=True, low_value_streak=signals.low_value_streak,
            )
        if signals.budget_wrap_up:
            if signals.budget_reason == "consecutive_low_value":
                if (
                    state.change_method_attempts > 0
                    and signals.semantic_signature
                    == state.blocked_semantic_signature
                ):
                    return self._update(
                        StopOrPivotAction.STOP_NO_PROGRESS,
                        "method_change_not_observed", terminal=True,
                        low_value_streak=signals.low_value_streak,
                        state=state,
                    )
                if state.change_method_attempts > 0:
                    next_state = StopOrPivotState(
                        StopOrPivotAction.CONTINUE.value,
                        signals.semantic_signature, state.change_method_attempts,
                    )
                    return StopOrPivotUpdate(
                        StopOrPivotDecision(
                            StopOrPivotAction.CONTINUE, "method_changed",
                            low_value_streak=signals.low_value_streak,
                        ), next_state,
                    )
                next_state = StopOrPivotState(
                    StopOrPivotAction.CHANGE_METHOD.value,
                    signals.semantic_signature, 1,
                )
                return StopOrPivotUpdate(
                    StopOrPivotDecision(
                        StopOrPivotAction.CHANGE_METHOD,
                        "consecutive_low_value",
                        low_value_streak=signals.low_value_streak,
                    ), next_state,
                )
            return self._update(
                StopOrPivotAction.SUMMARIZE_WITH_EVIDENCE,
                signals.budget_reason or "budget_wrap_up", terminal=True,
                low_value_streak=signals.low_value_streak,
            )
        return StopOrPivotUpdate(
            StopOrPivotDecision(StopOrPivotAction.CONTINUE, "healthy_progress"),
            StopOrPivotState(),
        )

    @staticmethod
    def _update(
        action: StopOrPivotAction, reason: str, *, terminal: bool = False,
        candidate_count: int = 0, low_value_streak: int = 0,
        state: StopOrPivotState | None = None,
    ) -> StopOrPivotUpdate:
        return StopOrPivotUpdate(
            StopOrPivotDecision(
                action, reason, terminal, candidate_count, low_value_streak
            ),
            state or StopOrPivotState(action.value),
        )

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("stop-or-pivot policy is not started")
