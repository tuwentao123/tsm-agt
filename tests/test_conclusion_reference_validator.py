from __future__ import annotations

import unittest

from tsm_agt.core import ConclusionReferenceValidator, TaskSnapshot
from tsm_agt.core.execution import ToolCommitState, ToolExecutionRecord
from tsm_agt.ports import (
    AssistantConclusion,
    ConclusionClaim,
    ConclusionKind,
    ConclusionValidationStatus,
    FactReference,
    RuntimeEvent,
    ToolCall,
    ToolFactDescriptor,
    ToolIdempotency,
    ToolResult,
    ToolRisk,
)


class ConclusionReferenceValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.task = TaskSnapshot.create("task-1", "test", "/tmp")
        self.event = RuntimeEvent(
            "event-1", self.task.task_id, 1, "runtime.input_recorded", {}
        )

    def test_validates_runtime_event_identity_without_interpreting_summary(self) -> None:
        conclusion = AssistantConclusion(
            1,
            (ConclusionClaim(
                "claim-1", ConclusionKind.HUMAN_CONFIRMATION_REQUIRED,
                "arbitrary model summary", (), (), (), "needs a person",
            ),),
        )
        # A no-reference human confirmation claim is structurally valid; its
        # summary is deliberately outside Runtime's validation scope.
        validation = ConclusionReferenceValidator().validate(
            self.task, (self.event,), conclusion
        )
        self.assertEqual(validation.status, ConclusionValidationStatus.VALID)

    def test_rejects_mismatched_event_sequence(self) -> None:
        conclusion = AssistantConclusion(
            1,
            (ConclusionClaim(
                "claim-1", ConclusionKind.HUMAN_CONFIRMATION_REQUIRED,
                "needs confirmation", (), (FactReference(
                    self.task.task_id, self.event.event_id, 2,
                    "runtime_event", self.event.event_id,
                ),), (), "needs a person",
            ),),
        )
        validation = ConclusionReferenceValidator().validate(
            self.task, (self.event,), conclusion
        )
        self.assertEqual(validation.status, ConclusionValidationStatus.INVALID)
        self.assertIn("event_sequence_mismatch", validation.claims[0].reasons)

    def test_validates_committed_tool_descriptor_and_rejects_effect_mismatch(self) -> None:
        call = ToolCall("call-1", "fixture.read")
        result = ToolResult(
            "call-1", True, fact_descriptor=ToolFactDescriptor(
                authority="workspace_fact", scope=("src/",), content_hash="sha256:1",
            ),
        )
        execution = ToolExecutionRecord.start(
            task_id=self.task.task_id, turn_id="turn-1", invocation_id="inv-1",
            call=call, payload_hash="payload", policy_decision_id="policy",
            effective_risk=ToolRisk.R0, approval_request_id=None,
            idempotency=ToolIdempotency.IDEMPOTENT,
        ).finish(ToolCommitState.COMMITTED, result)
        task = self.task.with_tool_execution(execution)
        event = RuntimeEvent(
            "event-tool", task.task_id, 1, "tool.completed", {
                "execution_id": execution.execution_id,
                "result": result.to_data(),
                "tool": {"effect": "observe"},
            },
        )
        reference = FactReference(
            task.task_id, event.event_id, event.sequence, "tool_result",
            execution.execution_id, tool_call_id=call.call_id,
            execution_id=execution.execution_id,
            expected_authority="workspace_fact", expected_effect="observe",
            content_hash="sha256:1",
        )
        conclusion = AssistantConclusion(1, (ConclusionClaim(
            "claim-1", ConclusionKind.VERIFIED, "verified", ("src/",),
            (reference,),
        ),))
        self.assertEqual(
            ConclusionReferenceValidator().validate(task, (event,), conclusion).status,
            ConclusionValidationStatus.VALID,
        )
        wrong_effect = FactReference(
            task.task_id, event.event_id, event.sequence, "tool_result",
            execution.execution_id, expected_effect="mutate",
        )
        invalid = AssistantConclusion(1, (ConclusionClaim(
            "claim-2", ConclusionKind.CHECKED, "checked", (), (wrong_effect,),
        ),))
        self.assertEqual(
            ConclusionReferenceValidator().validate(task, (event,), invalid).status,
            ConclusionValidationStatus.INVALID,
        )


if __name__ == "__main__":
    unittest.main()
