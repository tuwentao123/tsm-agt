from __future__ import annotations

import unittest

from tsm_agt.adapters.rule_based_semantic_action import (
    RuleBasedSemanticActionClassifier,
)
from tsm_agt.ports import (
    AdapterContext, SemanticActionFamily, SemanticScopeKind, ToolCall,
)


class RuleBasedSemanticActionClassifierTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.classifier = RuleBasedSemanticActionClassifier()
        await self.classifier.start(
            AdapterContext(config={}, emit_event=lambda *_: None)
        )

    async def test_same_tool_and_arguments_have_same_signature(self) -> None:
        first = await self.classifier.classify(ToolCall(
            "one", "core.search_text",
            {"query": "class CallConfirmDialog", "path": "modules"},
        ))
        second = await self.classifier.classify(ToolCall(
            "two", "core.search_text",
            {"path": "modules", "query": "class CallConfirmDialog"},
        ))
        self.assertEqual(first.family, SemanticActionFamily.SEARCH_DEFINITION)
        self.assertEqual(first.semantic_signature, second.semantic_signature)
        self.assertEqual(first.scope_kind, SemanticScopeKind.DIRECTORY)

    async def test_different_tool_or_arguments_change_signature(self) -> None:
        definition = await self.classifier.classify(ToolCall(
            "definition", "code.definition", {"symbol": "Service"},
        ))
        references = await self.classifier.classify(ToolCall(
            "references", "code.references", {"symbol": "Service"},
        ))
        self.assertEqual(definition.family, SemanticActionFamily.SEARCH_DEFINITION)
        self.assertEqual(references.family, SemanticActionFamily.SEARCH_REFERENCES)
        self.assertNotEqual(
            definition.semantic_signature, references.semantic_signature
        )

    async def test_events_remain_redacted(self) -> None:
        action = await self.classifier.classify(ToolCall(
            "read", "core.read_file", {"path": "private/customer-name.py"},
        ))
        rendered = str(action.event_data())
        self.assertNotIn("customer-name", rendered)
        self.assertEqual(action.family, SemanticActionFamily.READ_ARTIFACT)
        self.assertEqual(action.scope_kind, SemanticScopeKind.FILE)


if __name__ == "__main__":
    unittest.main()
