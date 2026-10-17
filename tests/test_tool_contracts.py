from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tsm_agt.core import ToolCommitState, ToolExecutionRecord
from tsm_agt.ports import (
    SourceArtifact,
    ModelToolContract,
    ToolCall,
    ToolEffect,
    ToolFactDescriptor,
    ToolFactCapability,
    ToolIdempotency,
    ToolProtocol,
    ToolResult,
    ToolResultMaterialization,
    ToolResultAuthority,
    ToolRisk,
    ToolSpec,
)


class ToolContractTest(unittest.TestCase):
    def test_tool_semantics_keep_protocol_separate_from_results(self) -> None:
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

        self.assertEqual(interaction_tool.to_data()["protocol"], "wait_user")
        self.assertNotIn("requires_evidence_question", evidence_tool.to_data())
        self.assertNotIn("requires_evidence_question", legacy_tool.to_data())

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

    def test_model_tool_contract_is_separate_from_runtime_description(self) -> None:
        spec = ToolSpec(
            "fixture.inspect", "Verbose operator documentation.",
            {"type": "object"}, ToolRisk.R0,
            model_contract=ModelToolContract(
                purpose="Inspect one fixture.",
                use_when=("a fixture must be examined",),
                avoid_when=("a mutation is needed",),
                returns=("bounded fixture content",),
            ),
        )
        self.assertEqual(spec.model_description().splitlines()[0], "Inspect one fixture.")
        self.assertEqual(spec.to_data()["description"], "Verbose operator documentation.")
        self.assertEqual(spec.to_data()["model_contract"]["purpose"], "Inspect one fixture.")

    def test_tool_fact_materialization_and_capabilities_round_trip(self) -> None:
        descriptor = ToolFactDescriptor(
            "workspace_read",
            materialization=ToolResultMaterialization.CONTENT,
            capabilities=(ToolFactCapability.DESCRIBE_CONTENT,),
        )
        self.assertEqual(ToolFactDescriptor.from_data(descriptor.to_data()), descriptor)

    def test_legacy_tool_fact_descriptor_defaults_to_unknown_semantics(self) -> None:
        descriptor = ToolFactDescriptor.from_data({
            "authority": "legacy_workspace_read",
            "scope": [],
            "artifact_refs": [],
            "source_artifacts": [],
        })
        self.assertIs(descriptor.materialization, ToolResultMaterialization.UNKNOWN)
        self.assertEqual(descriptor.capabilities, ())

    def test_tool_call_v2_has_only_protocol_fields(self) -> None:
        call = ToolCall("call-read", "core.read_file", {"path": "README.md"})

        stored = call.to_data()
        self.assertEqual(stored, {
            "call_id": "call-read",
            "name": "core.read_file",
            "arguments": {"path": "README.md"},
        })
        self.assertEqual(ToolCall.from_data(stored), call)

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
