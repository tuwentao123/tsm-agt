from __future__ import annotations

import unittest

from tsm_agt.ports import (
    AssistantConclusion,
    ClaimReferenceValidation,
    ConclusionClaim,
    ConclusionKind,
    ConclusionReferenceValidation,
    ConclusionValidationStatus,
    FactLifecycle,
    FactReference,
    Message,
    MessageRole,
    TextBlock,
    ToolFactDescriptor,
    ToolResult,
)
from tsm_agt.ports.model import ConclusionBlock


class ConclusionProtocolTest(unittest.TestCase):
    def _conclusion(self) -> AssistantConclusion:
        return AssistantConclusion(
            schema_version=1,
            claims=(ConclusionClaim(
                claim_id="claim-1",
                kind=ConclusionKind.VERIFIED,
                summary="test completed",
                scope=("tests",),
                fact_refs=(FactReference(
                    task_id="task-1",
                    event_id="event-1",
                    sequence=2,
                    source_type="tool_result",
                    source_id="execution-1",
                    lifecycle=FactLifecycle.ACTIVE,
                ),),
            ),),
            overall_scope=("tests",),
        )

    def test_message_round_trip_keeps_conclusion_out_of_visible_text(self) -> None:
        message = Message(
            "assistant-1", MessageRole.ASSISTANT,
            (TextBlock("Visible answer."), ConclusionBlock(self._conclusion())),
        )
        restored = Message.from_data(message.to_data())
        self.assertEqual(restored.text, "Visible answer.")
        self.assertEqual(restored.to_data(), message.to_data())

    def test_legacy_message_and_tool_result_remain_readable(self) -> None:
        legacy_message = {
            "message_id": "assistant-old", "role": "assistant",
            "content": [{"type": "text", "text": "old answer"}],
        }
        legacy_result = {"call_id": "call-old", "ok": True, "data": {"x": 1}}
        self.assertEqual(Message.from_data(legacy_message).text, "old answer")
        self.assertIsNone(ToolResult.from_data(legacy_result).fact_descriptor)

    def test_tool_fact_descriptor_round_trips_without_using_meta(self) -> None:
        descriptor = ToolFactDescriptor(
            authority="workspace_fact", scope=("src/",),
            artifact_refs=("artifact-1",), observed_at="2025-01-01T00:00:00Z",
            content_hash="sha256:abc",
        )
        result = ToolResult(
            "call-1", True, data={"ok": True}, meta={"legacy": "kept"},
            fact_descriptor=descriptor,
        )
        restored = ToolResult.from_data(result.to_data())
        self.assertEqual(restored.fact_descriptor, descriptor)
        self.assertEqual(restored.meta, {"legacy": "kept"})

    def test_claim_and_aggregate_validation_round_trip(self) -> None:
        validation = ConclusionReferenceValidation(
            ConclusionValidationStatus.VALID,
            (ClaimReferenceValidation(
                "claim-1", ConclusionValidationStatus.VALID, ("matched",)
            ),),
        )
        self.assertEqual(
            ConclusionReferenceValidation.from_data(validation.to_data()), validation
        )

    def test_strong_claims_and_unknown_blocks_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "verified claim requires"):
            ConclusionClaim(
                claim_id="claim-invalid", kind=ConclusionKind.VERIFIED,
                summary="unfounded", scope=(), fact_refs=(),
            )
        with self.assertRaisesRegex(ValueError, "unsupported message block"):
            Message.from_data({
                "message_id": "bad", "role": "assistant",
                "content": [{"type": "future", "value": 1}],
            })


if __name__ == "__main__":
    unittest.main()
