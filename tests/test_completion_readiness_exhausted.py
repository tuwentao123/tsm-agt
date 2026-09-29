"""INV-13 / INV-14: completion is determined by the gap set, not by counters.

The readiness gate must never report COMPLETE while a required gap is open, and
a Runtime that stops itself with requirements outstanding must end in human
review rather than as a product failure.

It must also not stop early: a model that keeps re-issuing corrected work is
iterating, not stalling. Only an exhausted *resource* or an absent *capability*
legitimately ends the attempt.
"""

from __future__ import annotations

import unittest

from tsm_agt.adapters.rule_based_completion_readiness import (
    RuleBasedCompletionReadinessPolicy,
)
from tsm_agt.ports import (
    AdapterContext,
    CompletionGap,
    CompletionReadinessAction,
    CompletionReadinessProbe,
    CompletionReadinessState,
    ToolEffect,
)


def _required_gap(effect: ToolEffect = ToolEffect.MUTATE) -> CompletionGap:
    return CompletionGap(
        "task-spec:required-effect-delivery", "REQUIRED_DELIVERY_UNSATISFIED",
        "contract requires delivery but no durable record was produced",
        status="MISSING", required=True, recoverable=True,
        required_effects=(effect,), candidate_tools=("core.apply_patch",),
    )


def _probe(
    *, remaining_model_calls: int = 10, remaining_tool_calls: int = 10,
    forced_wrap_up: bool = False,
    available_effects: frozenset[ToolEffect] = frozenset(
        {ToolEffect.OBSERVE, ToolEffect.MUTATE}
    ),
) -> CompletionReadinessProbe:
    return CompletionReadinessProbe(
        goal="deliver the change",
        gaps=(_required_gap(),),
        remaining_model_calls=remaining_model_calls,
        remaining_tool_calls=remaining_tool_calls,
        available_read_tools=("core.read_file",),
        forced_wrap_up=forced_wrap_up,
        available_effects=available_effects,
        available_tools=("core.apply_patch",),
    )


def _spent_state(**overrides) -> CompletionReadinessState:
    values = {
        "continue_attempts": 5,
        "disclosure_attempts": 5,
        "automatic_resume_attempts": 0,
        "last_action": "REPORT_INCOMPLETE_RECOVERABLE",
        "schema_version": 1,
        "stalled_continuations": 0,
    }
    values.update(overrides)
    return CompletionReadinessState(**values)


class CompletionReadinessGapDeterminismTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.policy = RuleBasedCompletionReadinessPolicy()
        await self.policy.start(AdapterContext({}, lambda _type, _payload: None))

    async def test_recoverable_gap_with_resources_keeps_continuing(self) -> None:
        # The incident shape: the work is closable (a mutate tool is available)
        # and the turn has budget. Arbitrary numbers of earlier corrections must
        # NOT produce a stop -- that is iteration, not exhaustion.
        decision = await self.policy.evaluate(_probe(), _spent_state())

        self.assertIs(decision.action, CompletionReadinessAction.CONTINUE)
        self.assertEqual(decision.reason, "required_capability_is_available")

    async def test_resource_exhaustion_is_not_reported_as_complete(self) -> None:
        decision = await self.policy.evaluate(
            _probe(remaining_model_calls=0), _spent_state(),
        )

        self.assertIs(decision.action, CompletionReadinessAction.EXHAUSTED)
        self.assertEqual(
            decision.reason,
            "required_work_recoverable_but_turn_resources_exhausted",
        )
        self.assertTrue(decision.gaps)
        self.assertTrue(all(gap.required for gap in decision.gaps))

    async def test_unclosable_gap_is_not_reported_as_complete(self) -> None:
        decision = await self.policy.evaluate(
            _probe(available_effects=frozenset({ToolEffect.OBSERVE})),
            _spent_state(),
        )

        self.assertIs(decision.action, CompletionReadinessAction.EXHAUSTED)
        self.assertEqual(
            decision.reason, "required_work_not_closable_with_available_effects"
        )

    async def test_forced_wrap_up_is_not_reported_as_complete(self) -> None:
        decision = await self.policy.evaluate(
            _probe(forced_wrap_up=True), _spent_state(),
        )

        self.assertIs(decision.action, CompletionReadinessAction.EXHAUSTED)

    async def test_stalled_continuations_escalate_to_exhausted(self) -> None:
        decision = await self.policy.evaluate(
            _probe(), _spent_state(stalled_continuations=2),
        )

        self.assertIs(decision.action, CompletionReadinessAction.EXHAUSTED)
        self.assertEqual(
            decision.reason, "required_gaps_unchanged_across_continuations"
        )

    async def test_complete_always_implies_no_required_gap(self) -> None:
        for remaining_model, remaining_tool, forced in (
            (10, 10, False), (0, 0, False), (10, 10, True),
        ):
            for available in (
                frozenset({ToolEffect.OBSERVE, ToolEffect.MUTATE}),
                frozenset({ToolEffect.OBSERVE}),
            ):
                for stalled in (0, 2):
                    with self.subTest(
                        remaining_model=remaining_model,
                        remaining_tool=remaining_tool,
                        forced_wrap_up=forced,
                        available=sorted(item.value for item in available),
                        stalled=stalled,
                    ):
                        decision = await self.policy.evaluate(
                            _probe(
                                remaining_model_calls=remaining_model,
                                remaining_tool_calls=remaining_tool,
                                forced_wrap_up=forced,
                                available_effects=available,
                            ),
                            _spent_state(stalled_continuations=stalled),
                        )
                        if decision.action is (
                            CompletionReadinessAction.COMPLETE
                        ):
                            self.assertFalse(
                                [g for g in decision.gaps if g.required],
                                "COMPLETE carried a required gap",
                            )

    async def test_no_required_gap_is_still_complete(self) -> None:
        probe = CompletionReadinessProbe(
            goal="answer", gaps=(), remaining_model_calls=10,
            remaining_tool_calls=10, available_read_tools=(),
        )

        decision = await self.policy.evaluate(probe, CompletionReadinessState())

        self.assertIs(decision.action, CompletionReadinessAction.COMPLETE)
        self.assertEqual(decision.reason, "core_goal_has_no_known_gaps")


if __name__ == "__main__":
    unittest.main()
