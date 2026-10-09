from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tsm_agt.adapters.rule_based_evidence_gate import RuleBasedEvidenceGate
from tsm_agt.core import (
    MutationOperation,
    MutationRecord,
    TaskSnapshot,
    ToolCommitState,
    ToolExecutionRecord,
)
from tsm_agt.core.single_loop import (
    IntentAttempt,
    IntentAttemptStatus,
    IntentValidationErrorData,
    OrchestrationMode,
    TaskBindingKind,
    TaskOrchestrationMetadata,
    TurnIntentKind,
    validate_completion,
    validate_completion_shadow,
)
from tsm_agt.ports import (
    Message,
    MessageRole,
    RuntimeEvent,
    TextBlock,
    ToolCall,
    ToolEffect,
    ToolIdempotency,
    ToolResult,
    ToolResultAuthority,
    ToolRisk,
)


class CompletionShadowTest(unittest.TestCase):
    def _metadata(self, binding: TaskBindingKind = TaskBindingKind.NEW):
        return TaskOrchestrationMetadata(
            schema_version=1,
            orchestration_mode=OrchestrationMode.SINGLE_LOOP,
            root_goal="answer",
            root_input_event_ref="session-event:input-1",
            latest_input_event_ref="session-event:input-1",
            binding_kind=binding,
        )

    @staticmethod
    def _assistant() -> Message:
        return Message("assistant-1", MessageRole.ASSISTANT, (TextBlock("done"),))

    def _execution(
        self,
        *,
        effect: ToolEffect,
        authority: ToolResultAuthority,
        data: dict[str, object],
    ) -> ToolExecutionRecord:
        call = ToolCall("call-1", "test.tool", {})
        return ToolExecutionRecord.start(
            task_id="task-1",
            turn_id="turn-1",
            invocation_id="inv-1",
            call=call,
            payload_hash="payload",
            policy_decision_id="policy",
            effective_risk=ToolRisk.R0,
            approval_request_id=None,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=effect,
            result_authority=authority,
        ).finish(ToolCommitState.COMMITTED, ToolResult(call.call_id, True, data=data))

    def _task(
        self,
        *,
        mutations: tuple[MutationRecord, ...] = (),
        executions: tuple[ToolExecutionRecord, ...] = (),
    ) -> TaskSnapshot:
        task = TaskSnapshot.create("task-1", "answer", "/tmp/work")
        for mutation in mutations:
            task = task.with_mutation(mutation)
        for execution in executions:
            task = task.with_tool_execution(execution)
        return task

    @staticmethod
    def _attempt(
        *,
        status: IntentAttemptStatus,
        effects: tuple[ToolEffect, ...],
        continuation_task_id: str | None = None,
    ) -> IntentAttempt:
        return IntentAttempt(
            schema_version=1,
            attempt_id="intent-attempt:turn-1:intent-call",
            turn_id="turn-1",
            tool_call_id="intent-call",
            requested_intent="MODIFY",
            normalized_intent=TurnIntentKind.MODIFY,
            status=status,
            implied_effects=effects,
            continuation_task_id=continuation_task_id,
            validation_error=(
                IntentValidationErrorData(
                    field="unexpected",
                    message="unknown field",
                )
                if status is IntentAttemptStatus.REJECTED else None
            ),
        )

    def _validate_shadow(
        self,
        task: TaskSnapshot,
        events: tuple[RuntimeEvent, ...],
        metadata: TaskOrchestrationMetadata | None = None,
    ):
        completion = validate_completion(
            task=task,
            metadata=metadata or self._metadata(),
            events=events,
            turn_id="turn-1",
            assistant_message=self._assistant(),
            evidence_gate=RuleBasedEvidenceGate(),
        )
        return completion, validate_completion_shadow(
            task=task,
            metadata=metadata or self._metadata(),
            events=events,
            turn_id="turn-1",
            completion=completion,
        )

    def test_direct_response_has_no_shadow_gap_or_divergence(self) -> None:
        completion, shadow = self._validate_shadow(self._task(), ())

        self.assertTrue(completion.complete)
        self.assertEqual(shadow.gaps, ())
        self.assertEqual(shadow.divergences, ())

    def test_rejected_modify_attempt_is_a_shadow_gap_without_changing_legacy_completion(self) -> None:
        attempt = self._attempt(
            status=IntentAttemptStatus.REJECTED,
            effects=(ToolEffect.MUTATE,),
        )
        events = (RuntimeEvent(
            "intent-rejected",
            "task-1",
            1,
            "tool.intent_rejected",
            attempt.to_data(),
        ),)

        completion, shadow = self._validate_shadow(self._task(), events)

        self.assertTrue(completion.complete)
        self.assertEqual(
            [(gap.code, gap.effect) for gap in shadow.gaps],
            [("SHADOW_INTENT_EFFECT_UNSATISFIED", ToolEffect.MUTATE)],
        )
        self.assertTrue(shadow.to_data()["diagnostic_only"])

    def test_recorded_mutation_and_net_delta_satisfy_shadow_modify_effect(self) -> None:
        mutation = MutationRecord(
            mutation_id="mutation-1",
            path="src/main.py",
            operation=MutationOperation.MODIFY,
            before_hash="before",
            after_hash="after",
            step_id="step-1",
            backup_ref=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        attempt = self._attempt(
            status=IntentAttemptStatus.ACCEPTED,
            effects=(ToolEffect.MUTATE,),
        )
        events = (RuntimeEvent(
            "intent-accepted",
            "task-1",
            1,
            "tool.intent_accepted",
            attempt.to_data(),
        ),)
        task = self._task(
            mutations=(mutation,),
            executions=(self._execution(
                effect=ToolEffect.MUTATE,
                authority=ToolResultAuthority.MUTATION_FACT,
                data={"mutation_id": "mutation-1"},
            ),),
        )

        _, shadow = self._validate_shadow(task, events)

        self.assertEqual(shadow.gaps, ())
        self.assertEqual(shadow.claim["net_mutation_ids"], ["mutation-1"])
        self.assertEqual(shadow.claim["succeeded_effects"], ["mutate"])

    def test_unrecorded_mutation_effect_is_reported_as_divergence(self) -> None:
        attempt = self._attempt(
            status=IntentAttemptStatus.ACCEPTED,
            effects=(ToolEffect.MUTATE,),
        )
        events = (RuntimeEvent(
            "intent-accepted",
            "task-1",
            1,
            "tool.intent_accepted",
            attempt.to_data(),
        ),)
        task = self._task(executions=(self._execution(
            effect=ToolEffect.MUTATE,
            authority=ToolResultAuthority.MUTATION_FACT,
            data={"preview": "only"},
        ),))

        _, shadow = self._validate_shadow(task, events)

        self.assertIn(
            "MUTATION_EFFECT_NOT_RECORDED",
            [item.code for item in shadow.divergences],
        )

    def test_delivered_mutation_that_was_rolled_back_is_reported(self) -> None:
        original = MutationRecord(
            mutation_id="mutation-1",
            path="src/main.py",
            operation=MutationOperation.MODIFY,
            before_hash="before",
            after_hash="after",
            step_id="step-1",
            backup_ref=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        rollback = MutationRecord(
            mutation_id="mutation-2",
            path="src/main.py",
            operation=MutationOperation.MODIFY,
            before_hash="after",
            after_hash="before",
            step_id="step-2",
            backup_ref=None,
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            reverts_mutation_id="mutation-1",
        )
        attempt = self._attempt(
            status=IntentAttemptStatus.ACCEPTED,
            effects=(ToolEffect.MUTATE,),
            continuation_task_id="other-task",
        )
        events = (RuntimeEvent(
            "intent-accepted",
            "task-1",
            1,
            "tool.intent_accepted",
            attempt.to_data(),
        ),)
        task = self._task(
            mutations=(original, rollback),
            executions=(self._execution(
                effect=ToolEffect.MUTATE,
                authority=ToolResultAuthority.MUTATION_FACT,
                data={"mutation_id": "mutation-1"},
            ),),
        )

        _, shadow = self._validate_shadow(
            task,
            events,
            self._metadata(TaskBindingKind.ACTIVE_CONTINUATION),
        )

        codes = [item.code for item in shadow.divergences]
        self.assertEqual(shadow.claim["net_mutation_ids"], [])
        self.assertIn("DELIVERED_MUTATION_ROLLED_BACK", codes)
        self.assertIn("CONTINUATION_TASK_MISMATCH", codes)

if __name__ == "__main__":
    unittest.main()
