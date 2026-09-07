from __future__ import annotations

import unittest
from datetime import datetime

from tsm_agt.adapters.evidence_guided import (
    BuiltinEvidenceRelationProvider, BoundedRejectionLoopPolicy,
    RuleBasedEvidenceRelationPolicy, RuleBasedExplorationOutcomePolicy,
)
from tsm_agt.core.exploration_coordinator import (
    ExplorationCoordinator, ExplorationCoordinatorAction,
)
from tsm_agt.ports import (
    AdapterDescriptor, EvidenceInventory, EvidenceQuestion, EvidenceRelation,
    EvidenceRelationKind, EvidenceRelationState, HealthState, HealthStatus,
    ExplorationOutcomeState, RejectionLoopState, ToolCall, ToolResult,
)
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.composition import _kernel_dependencies
from tsm_agt.ports import EvidenceRelationPolicyPort


class SecondRelationProvider(BuiltinEvidenceRelationProvider):
    descriptor = AdapterDescriptor(
        "fixture.second-evidence-relations", "1",
        "EvidenceRelationProviderPort", "1",
    )

    async def discover(self, call, semantic_action, state, inventory):
        self._require_started()
        return (EvidenceRelation(
            EvidenceRelationKind.SAME_TARGET, self.descriptor.adapter_id,
            "fixture-target", 1.0,
        ),)


class SecondRelationPolicy(RuleBasedEvidenceRelationPolicy):
    descriptor = AdapterDescriptor(
        "fixture.second-evidence-relation-policy", "1",
        "EvidenceRelationPolicyPort", "1",
    )


class EvidenceGuidedExplorationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = BuiltinEvidenceRelationProvider()
        self.relation = RuleBasedEvidenceRelationPolicy()
        self.rejection = BoundedRejectionLoopPolicy()
        self.outcome = RuleBasedExplorationOutcomePolicy()
        self.adapters = (
            self.provider, self.relation, self.rejection, self.outcome
        )
        for adapter in self.adapters:
            await adapter.start(None)
        self.coordinator = ExplorationCoordinator(
            (self.provider,), self.relation, self.rejection, self.outcome
        )

    async def asyncTearDown(self) -> None:
        for adapter in reversed(self.adapters):
            await adapter.stop(datetime.now())

    @staticmethod
    def call(call_id: str, path: str, question: str = "Q1") -> ToolCall:
        return ToolCall(
            call_id, "core.search_text",
            {"query": "Target", "path": path, "max_matches": 500},
            EvidenceQuestion(question, "Where is Target used?"),
        )

    async def test_same_question_can_follow_evidence_across_sibling_directories(self):
        state = EvidenceRelationState(("Q1",), ("target-a",), 2)
        decision = await self.coordinator.before_call(
            self.call("sibling", "other-module/resources"), None, state,
            RejectionLoopState(), EvidenceInventory(),
        )
        self.assertEqual(decision.action, ExplorationCoordinatorAction.EXECUTE)
        self.assertEqual(decision.reason, "same_evidence_question")

    async def test_multiple_relation_providers_are_combined(self):
        second = SecondRelationProvider()
        await second.start(None)
        try:
            coordinator = ExplorationCoordinator(
                (self.provider, second), self.relation, self.rejection, self.outcome
            )
            decision = await coordinator.before_call(
                self.call("multi", "module-c"), None,
                EvidenceRelationState(("Q1",), (), 2), RejectionLoopState(),
                EvidenceInventory(),
            )
            self.assertEqual(decision.action, ExplorationCoordinatorAction.EXECUTE)
            self.assertEqual(decision.relation_count, 2)
        finally:
            await second.stop(datetime.now())

    async def test_unknown_relation_gets_bounded_probe_without_broadening_path(self):
        call = ToolCall(
            "probe", "core.search_text",
            {"query": "Target", "path": "module-b", "max_matches": 500},
        )
        decision = await self.coordinator.before_call(
            call, None, EvidenceRelationState(("Q1",), (), 2),
            RejectionLoopState(), EvidenceInventory(),
        )
        self.assertEqual(
            decision.action, ExplorationCoordinatorAction.ALLOW_BOUNDED_PROBE
        )
        self.assertEqual(decision.call.arguments["path"], "module-b")
        self.assertEqual(decision.call.arguments["max_matches"], 50)

    async def test_different_question_is_not_counted_as_same_rejection(self):
        state = EvidenceRelationState(("Q-old",), (), 3)
        first = await self.coordinator.before_call(
            self.call("one", "a", "Q-new-one"), None, state,
            RejectionLoopState(), EvidenceInventory(),
        )
        second = await self.coordinator.before_call(
            self.call("two", "b", "Q-new-two"), None, state,
            first.rejection_state, EvidenceInventory(),
        )
        self.assertEqual(
            first.action, ExplorationCoordinatorAction.ALLOW_BOUNDED_PROBE
        )
        self.assertEqual(
            second.action, ExplorationCoordinatorAction.ALLOW_BOUNDED_PROBE
        )
        self.assertEqual(first.rejection_occurrence, 1)
        self.assertEqual(second.rejection_occurrence, 1)

    async def test_same_soft_collision_is_bounded_once_then_stopped(self):
        call = ToolCall(
            "collision", "core.search_text",
            {"query": "Target", "path": "module-b"},
        )
        first = await self.coordinator.before_call(
            call, None, EvidenceRelationState(("Q1",), (), 2),
            RejectionLoopState(), EvidenceInventory(),
        )
        second = await self.coordinator.before_call(
            call, None, EvidenceRelationState(("Q1",), (), 2),
            first.rejection_state, EvidenceInventory(),
        )
        self.assertEqual(
            first.action, ExplorationCoordinatorAction.ALLOW_BOUNDED_PROBE
        )
        self.assertEqual(second.action, ExplorationCoordinatorAction.STOP_ROUTE)
        self.assertEqual(second.rejection_occurrence, 2)

    async def test_new_evidence_resets_rejection_collision_state(self):
        update = await self.coordinator.after_result(
            self.call("done", "src"), ToolResult("done", True, {}), None,
            type("Delta", (), {"has_progress": True})(),
            EvidenceRelationState(), RejectionLoopState({"collision": 2}),
            ExplorationOutcomeState(),
        )
        self.assertEqual(update.rejection_state.rejection_counts, {})
        self.assertEqual(update.relation_state.successful_calls, 1)

    async def test_state_is_framework_neutral(self):
        encoded = str(EvidenceRelationState(("Q1",), ("hash",), 1).to_data())
        for framework in ("android", "ios", "web", "backend"):
            self.assertNotIn(framework, encoded.casefold())

    async def test_web_and_backend_sibling_routes_use_same_generic_rule(self):
        for first_path, related_path in (
            ("src/components/PhoneDialog.tsx", "src/locales/zh-CN.json"),
            ("server/controllers/order.py", "db/sql/order_query.sql"),
        ):
            with self.subTest(first_path=first_path):
                state = EvidenceRelationState(("Q-route",), (), 1)
                decision = await self.coordinator.before_call(
                    self.call("related", related_path, "Q-route"), None, state,
                    RejectionLoopState(), EvidenceInventory(),
                )
                self.assertEqual(
                    decision.action, ExplorationCoordinatorAction.EXECUTE
                )
                self.assertEqual(decision.reason, "same_evidence_question")

    def test_state_schema_version_is_explicit_and_rejects_unknown_version(self):
        self.assertEqual(EvidenceRelationState().to_data()["schema_version"], 1)
        with self.assertRaisesRegex(ValueError, "schema version"):
            EvidenceRelationState.from_data({"schema_version": 2})

    def test_more_than_one_singleton_relation_policy_fails_composition(self):
        application = compose_fixture_application(
            evidence_relation_policy_adapter=self.relation,
            rejection_loop_policy_adapter=self.rejection,
            exploration_outcome_policy_adapter=self.outcome,
            evidence_relation_provider_adapters=(self.provider,),
        )
        application.registry.register(
            EvidenceRelationPolicyPort, SecondRelationPolicy()
        )
        with self.assertRaisesRegex(ValueError, "at most one EvidenceRelationPolicy"):
            _kernel_dependencies(application.registry, {})


if __name__ == "__main__":
    unittest.main()
