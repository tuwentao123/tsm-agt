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
    """Decide whether a proposed final answer may be finalised.

    The verdict is a pure function of three persisted, inspectable facts: the
    required-gap set, the capabilities that are actually available, and the
    remaining turn budget.

    * no required gap                                          -> COMPLETE
    * the gap is closable by an available effect, budget left   -> CONTINUE
    * otherwise                                                -> EXHAUSTED

    It deliberately keeps **no in-turn "correction attempt" counter**. Bounding
    how many times the Runtime may tell the model that work remains measures the
    wrong thing: a model that keeps re-issuing a corrected tool call is making
    progress, not stalling. What is bounded is the *resource* (the caller owns
    ``remaining_model_calls``/``remaining_tool_calls``) and the *capability*
    (whether any available effect can close the gap at all). Repetition of the
    same unsatisfiable requirement across user continuations is bounded by
    ``stalled_continuations`` in the Runtime (INV-4), so no second, in-turn
    counter is needed.

    INV-13: ``COMPLETE`` is only ever returned when the gap set is empty, so a
    "success" verdict can never ride next to an outstanding requirement.
    """

    descriptor = AdapterDescriptor(
        "builtin.rule-based-completion-readiness", "2.0.0",
        "CompletionReadinessPolicyPort", "2.0",
        frozenset({"project-neutral", "checkpointed", "gap-determined"}),
    )

    def __init__(
        self, *, max_stalled_continuations: int = 2,
    ) -> None:
        if max_stalled_continuations < 0:
            raise ValueError("max_stalled_continuations must not be negative")
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
        # The same required gaps survived several user continuations with no
        # progress. That is the Runtime's bounded escape hatch (INV-4): stop
        # asking for an unchanged attempt and hand the decision to a human.
        if state.stalled_continuations >= self.max_stalled_continuations:
            return self._terminal(
                CompletionReadinessAction.EXHAUSTED,
                "required_gaps_unchanged_across_continuations", state, required,
            )
        if (
            recoverable
            and probe.remaining_model_calls > 0
            and probe.remaining_tool_calls > 0
            and not probe.forced_wrap_up
        ):
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
        # Either no available capability can close the gap, or the turn has no
        # resource left. Both are a bounded stop with requirements open: not a
        # success and not a product failure.
        reason = (
            "required_work_not_closable_with_available_effects"
            if not recoverable else
            "required_work_recoverable_but_turn_resources_exhausted"
        )
        return self._terminal(
            CompletionReadinessAction.EXHAUSTED, reason, state, required,
        )

    @staticmethod
    def _decision(
        action: CompletionReadinessAction, reason: str,
        state: CompletionReadinessState, probe: CompletionReadinessProbe,
    ) -> CompletionReadinessDecision:
        return CompletionReadinessDecision(action, reason, CompletionReadinessState(
            state.continue_attempts, state.disclosure_attempts,
            state.automatic_resume_attempts, action.value,
            state.schema_version, state.stalled_continuations,
        ), tuple(gap for gap in probe.gaps if gap.required))

    @staticmethod
    def _terminal(
        action: CompletionReadinessAction, reason: str,
        state: CompletionReadinessState, gaps: tuple,
    ) -> CompletionReadinessDecision:
        return CompletionReadinessDecision(action, reason, CompletionReadinessState(
            state.continue_attempts, state.disclosure_attempts,
            state.automatic_resume_attempts, action.value,
            state.schema_version, state.stalled_continuations,
        ), gaps)
