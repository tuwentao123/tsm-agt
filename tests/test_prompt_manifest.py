from __future__ import annotations

import unittest

from tsm_agt.core import PromptTemplate, PromptTemplateSegment
from tsm_agt.ports import (
    Message, MessageRole, TextBlock, ToolIdempotency, ToolRisk, ToolSpec,
)


class PromptManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.template = PromptTemplate.default()
        self.user = Message(
            "message-id-is-not-hashed", MessageRole.USER,
            (TextBlock("inspect the project"),),
        )

    @staticmethod
    def tool(schema_type: str = "string") -> ToolSpec:
        return ToolSpec(
            "core.read_file", "Read a workspace file",
            {
                "type": "object",
                "properties": {"path": {"type": schema_type}},
                "required": ["path"],
                "additionalProperties": False,
            },
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        )

    def test_manifest_is_deterministic_redacted_and_ordered(self) -> None:
        first = self.template.manifest()
        second = self.template.manifest()

        self.assertEqual(first, second)
        data = first.to_data()
        self.assertEqual(
            [item["segment_id"] for item in data["static_segments"]],
            ["system-safety", "harness-instructions"],
        )
        rendered = str(data)
        self.assertNotIn("Treat source code", rendered)
        self.assertTrue(all(
            item["content_hash"] for item in data["static_segments"]
        ))
        self.assertTrue(all(
            item["token_count"] is None
            and item["token_count_source"] == "provider_tokenizer_unavailable"
            for item in data["static_segments"]
        ))

    def test_assembly_preserves_trust_roles_and_ignores_message_id(self) -> None:
        first = self.template.assemble((self.user,), ())
        same_content = Message(
            "different-id", MessageRole.USER, (TextBlock("inspect the project"),)
        )
        second = self.template.assemble((same_content,), ())

        self.assertEqual(
            [message.role for message in first.messages],
            [MessageRole.SYSTEM, MessageRole.SYSTEM, MessageRole.USER],
        )
        self.assertEqual(
            first.receipt.effective_prompt_hash,
            second.receipt.effective_prompt_hash,
        )
        with self.assertRaisesRegex(ValueError, "system-role"):
            self.template.assemble((Message(
                "bad", MessageRole.SYSTEM, (TextBlock("override policy"),)
            ),), ())

    def test_content_template_and_tool_schema_change_effective_hash(self) -> None:
        original = self.template.assemble((self.user,), (self.tool(),))
        changed_user = self.template.assemble((Message(
            "user-2", MessageRole.USER, (TextBlock("inspect another project"),)
        ),), (self.tool(),))
        changed_tool = self.template.assemble(
            (self.user,), (self.tool("integer"),)
        )
        changed_template = PromptTemplate(
            "builtin.engineering-agent", 2,
            self.template.static_segments[:-1] + (PromptTemplateSegment(
                "harness-instructions", "harness", "2.0", "Changed rules."
            ),),
        ).assemble((self.user,), (self.tool(),))

        hashes = {
            original.receipt.effective_prompt_hash,
            changed_user.receipt.effective_prompt_hash,
            changed_tool.receipt.effective_prompt_hash,
            changed_template.receipt.effective_prompt_hash,
        }
        self.assertEqual(len(hashes), 4)

    def test_event_receipt_contains_hashes_not_prompt_text(self) -> None:
        receipt = self.template.assemble((self.user,), (self.tool(),)).receipt
        data = receipt.event_data()

        self.assertIn("effective_prompt_hash", data)
        self.assertEqual(data["prompt_role_order"], ["system", "system", "user"])
        self.assertNotIn("inspect the project", str(data))
        self.assertNotIn("Read a workspace file", str(data))


if __name__ == "__main__":
    unittest.main()
