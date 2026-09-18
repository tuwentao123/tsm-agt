"""Conservative project-neutral final-answer readiness policy."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, CompletionReadinessAction,
    CompletionReadinessDecision, CompletionReadinessPolicyPort,
    CompletionReadinessProbe, CompletionReadinessState, HealthState, HealthStatus,
    ToolEffect,
)


class RuleBasedCompletionReadinessPolicy:
    """Allow one work correction and one blocker-disclosure correction.

    It does not understand Android, Web, backend, or natural-language phrases.
    Kernel supplies only persisted gaps and a capability inventory. Required
    gaps whose effects are available may continue once when tools and hard
    budget remain. Persistent or external gaps get one direct blocker-report
    correction, after which the Task must finish to avoid an infinite loop.
    """

    descriptor = AdapterDescriptor(
        "builtin.rule-based-completion-readiness", "1.0.0",
        "CompletionReadinessPolicyPort", "1.0",
        frozenset({"project-neutral", "checkpointed", "bounded-correction"}),
    )

    def __init__(
        self, *, max_continue_attempts: int = 1,
        max_stalled_continuations: int = 2,
    ) -> None:
        if max_continue_attempts < 0:
            raise ValueError("max_continue_attempts must not be negative")
        if max_stalled_continuations < 0:
            raise ValueError("max_stalled_continuations must not be negative")
        self.max_continue_attempts = max_continue_attempts
        self.max_stalled_continuations = max_stalled_continuations
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "completion readiness policy ready"
            if self._started else "not started",
        )

    async def evaluate(
        self, probe: CompletionReadinessProbe, state: CompletionReadinessState,
    ) -> CompletionReadinessDecision:
        if not self._started:
            raise RuntimeError("completion readiness policy is not started")
        required = tuple(gap for gap in probe.gaps if gap.required)
        if not required:
            return self._decision(
                CompletionReadinessAction.COMPLETE, "core_goal_has_no_known_gaps",
                state, probe,
            )
        available_effects = set(probe.available_effects)
        # Compatibility for old probes/adapters that predate ToolEffect.
        if probe.available_read_tools:
            available_effects.add(ToolEffect.OBSERVE)
        recoverable = tuple(
            gap for gap in required
            if gap.effective_required_effects
            and gap.effective_required_effects.issubset(available_effects)
        )
        # Answering "the same work is still required" with another identical
        # attempt is not recovery. Once several continuations have ended with
        # exactly the same required gaps and nothing completed, the honest
        # answer is that this requirement cannot be met here, not another
        # continuation of an unchanged attempt.
        if (
            state.stalled_continuations >= self.max_stalled_continuations
            and probe.remaining_model_calls > 0
            and state.disclosure_attempts == 0
        ):
            next_state = CompletionReadinessState(
                state.continue_attempts, 1, state.automatic_resume_attempts,
                CompletionReadinessAction.REPORT_BLOCKED.value,
                state.schema_version, state.stalled_continuations,
            )
            return CompletionReadinessDecision(
                CompletionReadinessAction.REPORT_BLOCKED,
                "required_gaps_unchanged_across_continuations",
                next_state, required,
            )
        can_continue = bool(
            recoverable
            and probe.remaining_model_calls > 0
            and probe.remaining_tool_calls > 0
            and not probe.forced_wrap_up
            and state.continue_attempts < self.max_continue_attempts
        )
        if can_continue:
            next_state = CompletionReadinessState(
                state.continue_attempts + 1, state.disclosure_attempts,
                state.automatic_resume_attempts,
                CompletionReadinessAction.CONTINUE.value,
                state.schema_version, state.stalled_continuations,
            )
            return CompletionReadinessDecision(
                CompletionReadinessAction.CONTINUE,
                "required_capability_is_available", next_state, recoverable,
            )
        if (
            recoverable and probe.remaining_model_calls > 0
            and state.disclosure_attempts == 0
        ):
            next_state = CompletionReadinessState(
                state.continue_attempts, state.disclosure_attempts + 1,
                state.automatic_resume_attempts,
                CompletionReadinessAction.REPORT_INCOMPLETE_RECOVERABLE.value,
                state.schema_version, state.stalled_continuations,
            )
            return CompletionReadinessDecision(
                CompletionReadinessAction.REPORT_INCOMPLETE_RECOVERABLE,
                "required_work_is_recoverable_but_turn_capacity_is_exhausted",
                next_state, recoverable,
            )
        if probe.remaining_model_calls > 0 and state.disclosure_attempts == 0:
            next_state = CompletionReadinessState(
                state.continue_attempts, 1, state.automatic_resume_attempts,
                CompletionReadinessAction.REPORT_BLOCKED.value,
                state.schema_version, state.stalled_continuations,
            )
            return CompletionReadinessDecision(
                CompletionReadinessAction.REPORT_BLOCKED,
                "required_work_is_blocked_or_correction_limit_reached",
                next_state, required,
            )
        return self._decision(
            CompletionReadinessAction.COMPLETE,
            "bounded_completion_corrections_exhausted", state, probe,
        )

    @staticmethod
    def _decision(action, reason, state, probe):
        return CompletionReadinessDecision(action, reason, CompletionReadinessState(
            state.continue_attempts, state.disclosure_attempts,
            state.automatic_resume_attempts, action.value,
            state.schema_version, state.stalled_continuations,
        ), tuple(gap for gap in probe.gaps if gap.required))
