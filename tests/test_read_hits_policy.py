from __future__ import annotations

import unittest
from datetime import datetime

from tsm_agt.adapters.rule_based_read_hits import RuleBasedReadHitsPolicy
from tsm_agt.adapters.rule_based_semantic_action import RuleBasedSemanticActionClassifier
from tsm_agt.ports import ReadHitsAction, ReadHitsState, ToolCall, ToolResult


class RuleBasedReadHitsPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.policy = RuleBasedReadHitsPolicy()
        self.classifier = RuleBasedSemanticActionClassifier()
        await self.policy.start(None)
        await self.classifier.start(None)

    async def asyncTearDown(self) -> None:
        await self.policy.stop(datetime.now())
        await self.classifier.stop(datetime.now())

    async def test_exact_duplicate_search_requires_candidate_read(self):
        first = ToolCall("s1", "core.search_text", {"query": "Target", "path": "src"})
        action = await self.classifier.classify(first)
        update = await self.policy.after_result(
            first, ToolResult(first.call_id, True, {
                "matches": [{"path": "src/a.py"}, {"path": "src/b.py"}],
            }), action, ReadHitsState(),
        )
        repeat = ToolCall("s2", "core.search_text", {"path": "src", "query": "Target"})
        decision = await self.policy.before_call(
            repeat, await self.classifier.classify(repeat), update.state,
        )
        self.assertEqual(decision.action, ReadHitsAction.REQUIRE_READ)

    async def test_different_search_arguments_are_not_duplicate(self):
        first = ToolCall("s1", "core.search_text", {"query": "Target", "path": "src"})
        update = await self.policy.after_result(
            first, ToolResult(first.call_id, True, {"matches": [{"path": "src/a.py"}]}),
            await self.classifier.classify(first), ReadHitsState(),
        )
        different = ToolCall("s2", "core.search_text", {"query": "Other", "path": "src"})
        decision = await self.policy.before_call(
            different, await self.classifier.classify(different), update.state,
        )
        self.assertEqual(decision.action, ReadHitsAction.ALLOW)


if __name__ == "__main__":
    unittest.main()
