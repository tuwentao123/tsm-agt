from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tsm_agt.core import ToolCommitState, ToolExecutionRecord
from tsm_agt.ports import (
    EvidenceQuestion,
    ToolCall,
    ToolIdempotency,
    ToolResult,
    ToolRisk,
    ToolSpec,
)


class ToolContractTest(unittest.TestCase):
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
