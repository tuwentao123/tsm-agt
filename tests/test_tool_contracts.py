from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tsm_agt.core import ToolCommitState, ToolExecutionRecord
from tsm_agt.ports import (
    EvidenceQuestion,
    SourceArtifact,
    ToolCall,
    ToolEffect,
    ToolFactDescriptor,
    ToolIdempotency,
    ToolProtocol,
    ToolResult,
    ToolResultAuthority,
    ToolRisk,
    ToolSpec,
)


class ToolContractTest(unittest.TestCase):
    def test_tool_semantics_separate_evidence_from_user_interaction(self) -> None:
        evidence_tool = ToolSpec(
            "fixture.observe", "Observe one fact.",
            {"type": "object", "properties": {}}, ToolRisk.R0,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.WORKSPACE_FACT,
        )
        interaction_tool = ToolSpec(
            "fixture.interact", "Ask the user.",
            {"type": "object", "properties": {}}, ToolRisk.R0,
            effect=ToolEffect.INTERACT,
            result_authority=ToolResultAuthority.USER_INTENT,
            protocol=ToolProtocol.WAIT_USER,
        )
        legacy_tool = ToolSpec(
            "fixture.legacy", "Legacy adapter.",
            {"type": "object", "properties": {}}, ToolRisk.R0,
        )

        self.assertTrue(evidence_tool.requires_evidence_question)
        self.assertFalse(interaction_tool.requires_evidence_question)
        self.assertTrue(legacy_tool.requires_evidence_question)
        self.assertEqual(interaction_tool.to_data()["protocol"], "wait_user")

    def test_tool_contracts_serialize_provider_neutral_data(self) -> None:
        spec = ToolSpec(
            name="fixture.echo",
            description="Echo text.",
            parameters={"type": "object"},
            risk=ToolRisk.R0,
            is_read_only=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
        )
        call = ToolCall("call-1", spec.name, {"text": "hello"})
        result = ToolResult(call.call_id, True, data={"text": "hello"})

        self.assertEqual(spec.to_data()["risk"], "R0")
        self.assertEqual(call.to_data()["arguments"], {"text": "hello"})
        self.assertTrue(result.to_data()["ok"])

    def test_tool_call_preserves_its_evidence_question_across_storage(self) -> None:
        call = ToolCall(
            "call-evidence", "core.read_file", {"path": "README.md"},
            EvidenceQuestion(
                "E1", "Does README.md identify the project's entry point?"
            ),
        )

        self.assertEqual(ToolCall.from_data(call.to_data()), call)
        self.assertEqual(
            call.to_data()["evidence_question"],
            {
                "question_id": "E1",
                "question": "Does README.md identify the project's entry point?",
            },
        )

    def test_source_artifact_round_trips_with_tool_fact_descriptor(self) -> None:
        artifact = SourceArtifact(
            source_kind="github",
            repository="owner/repo",
            commit_sha="a" * 40,
            path="src/main.py",
            blob_sha="b" * 40,
            canonical_url=(
                "https://github.com/owner/repo/blob/"
                + "a" * 40
                + "/src/main.py"
            ),
            content_hash="sha256:" + "c" * 64,
        )

        descriptor = ToolFactDescriptor(
            "github_source",
            source_artifacts=(artifact,),
        )

        self.assertEqual(ToolFactDescriptor.from_data(descriptor.to_data()), descriptor)

    def test_failed_result_requires_error_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "error_code"):
            ToolResult("call-1", False)

    def test_tool_parameters_must_be_object_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "object schema"):
            ToolSpec("fixture.bad", "Bad schema.", {"type": "string"}, ToolRisk.R0)

    def test_tool_execution_record_serialization_round_trip(self) -> None:
        call = ToolCall("call-1", "fixture.echo", {"text": "hello"})
        record = ToolExecutionRecord.start(
            task_id="task-1",
            turn_id="turn-1",
            invocation_id="inv-1",
            call=call,
            payload_hash="payload-hash",
            policy_decision_id="policy-1",
            effective_risk=ToolRisk.R0,
            approval_request_id=None,
            idempotency=ToolIdempotency.KEYED,
        ).mark_running().finish(
            ToolCommitState.COMMITTED,
            ToolResult(call.call_id, True, data={"text": "hello"}),
        )

        self.assertEqual(ToolExecutionRecord.from_data(record.to_data()), record)
        self.assertIsNotNone(record.idempotency_key)


if __name__ == "__main__":
    unittest.main()
