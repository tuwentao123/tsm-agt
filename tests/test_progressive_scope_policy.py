from __future__ import annotations

import unittest
from datetime import datetime

from tsm_agt.adapters.rule_based_progressive_scope import RuleBasedProgressiveScopePolicy
from tsm_agt.adapters.rule_based_semantic_action import RuleBasedSemanticActionClassifier
from tsm_agt.ports import ProgressiveScopeAction, ProgressiveScopeState, ScopeProbe, ToolCall, ToolResult


class RuleBasedProgressiveScopePolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.policy = RuleBasedProgressiveScopePolicy()
        self.classifier = RuleBasedSemanticActionClassifier()
        await self.policy.start(None)
        await self.classifier.start(None)

    async def asyncTearDown(self) -> None:
        await self.policy.stop(datetime.now())
        await self.classifier.stop(datetime.now())

    async def test_distinct_scope_arguments_are_allowed_without_reason_envelope(self):
        first = ToolCall("first", "core.search_text", {"query": "Target", "path": "src"})
        first_action = await self.classifier.classify(first)
        state = (await self.policy.after_result(
            first, ToolResult(first.call_id, True, {"matches": [{"path": "src/a.py"}]}),
            first_action, ScopeProbe("src", ("workspace",), 1), ProgressiveScopeState(),
        )).state
        expanded = ToolCall("expanded", "core.search_text", {"query": "Target", "path": "."})
        decision = await self.policy.before_call(
            expanded, await self.classifier.classify(expanded), ScopeProbe("workspace", (), 0), state,
        )
        self.assertEqual(decision.action, ProgressiveScopeAction.ALLOW)
        self.assertEqual(decision.reason, "first_scope")


if __name__ == "__main__":
    unittest.main()
