from __future__ import annotations

import unittest

from tsm_agt.adapters.rule_based_semantic_action import (
    RuleBasedSemanticActionClassifier,
)
from tsm_agt.ports import (
    AdapterContext, EvidenceQuestion, SemanticActionFamily, SemanticScopeKind,
    ToolCall,
)


class RuleBasedSemanticActionClassifierTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.classifier = RuleBasedSemanticActionClassifier()
        await self.classifier.start(
            AdapterContext(config={}, emit_event=lambda *_: None)
        )

    async def test_same_definition_search_across_scopes_has_one_semantic_signature(self):
        question = EvidenceQuestion(
            "E1", "Where is CallConfirmDialog defined?"
        )
        calls = (
            ToolCall(
                "one", "core.search_text",
                {"query": "CallConfirmDialog", "path": "."}, question,
            ),
            ToolCall(
                "two", "core.search_text",
                {"query": "class CallConfirmDialog", "path": "modules"},
                question,
            ),
            ToolCall(
                "three", "code.definition",
                {"symbol": "CallConfirmDialog", "path": "business/im"},
                question,
            ),
        )

        actions = [await self.classifier.classify(call) for call in calls]

        self.assertEqual(
            {action.family for action in actions},
            {SemanticActionFamily.SEARCH_DEFINITION},
        )
        self.assertEqual(len({action.semantic_signature for action in actions}), 1)
        self.assertEqual(
            [action.scope_kind for action in actions],
            [
                SemanticScopeKind.WORKSPACE,
                SemanticScopeKind.DIRECTORY,
                SemanticScopeKind.DIRECTORY,
            ],
        )
        self.assertEqual(len({action.scope_hash for action in actions}), 3)

    async def test_reference_search_is_a_real_method_change(self):
        definition = await self.classifier.classify(ToolCall(
            "definition", "code.definition", {"symbol": "Service"},
            EvidenceQuestion("E1", "Where is Service defined?"),
        ))
        references = await self.classifier.classify(ToolCall(
            "references", "code.references", {"symbol": "Service"},
            EvidenceQuestion("E1", "Where is Service used?"),
        ))

        self.assertEqual(definition.family, SemanticActionFamily.SEARCH_DEFINITION)
        self.assertEqual(references.family, SemanticActionFamily.SEARCH_REFERENCES)
        self.assertNotEqual(
            definition.semantic_signature, references.semantic_signature
        )

    async def test_text_reference_question_and_code_reference_share_family(self):
        text = await self.classifier.classify(ToolCall(
            "text", "core.search_text",
            {"query": "newInstance", "path": "src"},
            EvidenceQuestion("E-callers", "Which callers use CallConfirmDialog?"),
        ))
        code = await self.classifier.classify(ToolCall(
            "code", "code.references", {"symbol": "CallConfirmDialog"},
            EvidenceQuestion("E-callers", "Which callers use CallConfirmDialog?"),
        ))

        self.assertEqual(text.family, SemanticActionFamily.SEARCH_REFERENCES)
        self.assertEqual(text.semantic_signature, code.semantic_signature)

    async def test_events_can_use_classification_without_raw_target_or_scope(self):
        action = await self.classifier.classify(ToolCall(
            "read", "core.read_file",
            {"path": "private/customer-name.py"},
            EvidenceQuestion("E-private", "What does the private file establish?"),
        ))

        rendered = str(action.event_data())
        self.assertNotIn("customer-name", rendered)
        self.assertNotIn("private file", rendered)
        self.assertEqual(action.family, SemanticActionFamily.READ_ARTIFACT)
        self.assertEqual(action.scope_kind, SemanticScopeKind.FILE)


if __name__ == "__main__":
    unittest.main()
