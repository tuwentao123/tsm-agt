import json
import unittest
from datetime import UTC, datetime

from tsm_agt.core.agent_loop import AgentTurnCheckpoint
from tsm_agt.core.configuration import canonical_hash
from tsm_agt.core.evidence_question import (
    EvidenceQuestionProjection,
    EvidenceQuestionRecord,
    EvidenceQuestionStatus,
)
from tsm_agt.core.session_context import SessionPromptProjection
from tsm_agt.core.single_loop.context_projection import ActiveContextProjector
from tsm_agt.core.single_loop.types import (
    IntentAttempt,
    IntentAttemptStatus,
    IntentValidationErrorData,
)
from tsm_agt.core.task import TaskSnapshot
from tsm_agt.core.working_memory import (
    EffectiveWorkingMemory,
    WorkingEvidenceReference,
    WorkingMemorySnapshot,
)
from tsm_agt.ports import Message, MessageRole, RuntimeEvent, TextBlock


class ActiveContextProjectorTest(unittest.TestCase):
    def _task(self) -> TaskSnapshot:
        return TaskSnapshot.create(
            "task-1", "mutable current goal", "/workspace",
            datetime(2025, 1, 1, tzinfo=UTC), session_id="session-1",
        )

    @staticmethod
    def _checkpoint() -> AgentTurnCheckpoint:
        return AgentTurnCheckpoint(
            task_id="task-1",
            turn_id="turn-1",
            revision=2,
            messages=(Message(
                "assistant-1", MessageRole.ASSISTANT,
                (TextBlock("OLD_ASSISTANT_BODY_MUST_NOT_BE_PERSISTED"),),
            ),),
            pending_tool_calls=(),
            seen_call_ids=(),
            model_calls=0,
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
            max_model_calls=4,
            max_tool_calls=4,
            max_output_tokens=512,
            tool_timeout_seconds=30.0,
            session_id="session-1",
        )

    @staticmethod
    def _session_prompt() -> SessionPromptProjection:
        return SessionPromptProjection(
            None, 3, "session-hash", (2, 3), 1, 4, 2, "summary-hash",
            (1,), ((1, 1),), {
                "assistant_prior_text": "OLD_ASSISTANT_BODY_MUST_NOT_BE_PERSISTED",
            },
        )

    def test_preserves_authoritative_obligations_and_evidence_refs(self) -> None:
        task = self._task()
        events = (
            RuntimeEvent("evt-1", "task-1", 1, "task.created", {
                "goal": "root goal",
                "single_loop": {
                    "schema_version": 1,
                    "orchestration_mode": "SINGLE_LOOP",
                    "root_goal": "root goal",
                    "root_input_event_ref": "input:1",
                    "latest_input_event_ref": "input:1",
                    "binding_kind": "NEW",
                },
            }),
        )
        memory = EffectiveWorkingMemory(
            WorkingMemorySnapshot.initial("task-1", "root goal"),
            runtime_evidence=(WorkingEvidenceReference(
                "evidence-1", "runtime", "tool_call:call-1", "not persisted",
            ),),
            runtime_source_event_sequences=(6,),
        )
        questions = EvidenceQuestionProjection("task-1", (EvidenceQuestionRecord(
            "question-1", "inspect the source", EvidenceQuestionStatus.OPEN,
            "task-1", "turn-1", evidence_references=("tool_call:call-1",),
            updated_event_sequence=7, expected_scope="src/module.py",
        ),))

        shadow = ActiveContextProjector.project(
            task, self._checkpoint(), memory, questions, self._session_prompt(),
            events, model_call=1,
        )
        payload = shadow.event_payload()

        self.assertEqual(
            shadow.projection.to_data()["authority"]["authoritative"],
            "AUTHORITATIVE",
        )
        self.assertEqual(
            shadow.projection.to_data()["authority"]["non_authoritative"],
            "NON_AUTHORITATIVE",
        )
        self.assertEqual(payload["root_goal_hash"], canonical_hash("root goal"))
        self.assertEqual(
            payload["active_context"]["authoritative"]["open_obligations"],
            [{
                "question_ref": questions.records[0].question_ref,
                "status": "OPEN",
                "source_turn_id": "turn-1",
                "expected_scope": "src/module.py",
                "blocking_reason": None,
                "evidence_refs": ["tool_call:call-1"],
                "updated_event_sequence": 7,
            }],
        )
        self.assertEqual(
            payload["active_context"]["authoritative"]["evidence_refs"],
            [{
                "evidence_id": "evidence-1",
                "kind": "runtime",
                "reference": "tool_call:call-1",
            }],
        )
        self.assertEqual(payload["source_event_sequences"], [1, 2, 3, 6, 7])
        encoded = json.dumps(payload)
        self.assertNotIn("OLD_ASSISTANT_BODY_MUST_NOT_BE_PERSISTED", encoded)
        self.assertNotIn("root goal", encoded)
        self.assertNotIn("not persisted", encoded)
        self.assertFalse(payload["assistant_body_persisted"])
        self.assertFalse(payload["prompt_body_persisted"])

    def test_projects_bounded_current_turn_intent_attempt(self) -> None:
        task = self._task()
        attempt = IntentAttempt(
            schema_version=1,
            attempt_id="intent-attempt:turn-1:call-1",
            turn_id="turn-1",
            tool_call_id="call-1",
            requested_intent="MODIFY",
            normalized_intent=None,
            status=IntentAttemptStatus.REJECTED,
            implied_effects=(),
            validation_error=IntentValidationErrorData(
                field="unexpected",
                message="unknown turn intent field",
                expected=("intent",),
            ),
        )
        events = (
            RuntimeEvent("evt-1", "task-1", 1, "task.created", {
                "goal": "root goal",
                "single_loop": {
                    "schema_version": 1,
                    "orchestration_mode": "SINGLE_LOOP",
                    "root_goal": "root goal",
                    "root_input_event_ref": "input:1",
                    "latest_input_event_ref": "input:1",
                    "binding_kind": "NEW",
                },
            }),
            RuntimeEvent(
                "evt-2",
                "task-1",
                2,
                "tool.intent_rejected",
                attempt.to_data(),
            ),
        )
        shadow = ActiveContextProjector.project(
            task,
            self._checkpoint(),
            EffectiveWorkingMemory(WorkingMemorySnapshot.initial("task-1", "root goal")),
            EvidenceQuestionProjection("task-1"),
            self._session_prompt(),
            events,
            model_call=1,
        )

        attempts = shadow.projection.authoritative["intent_attempts"]
        self.assertEqual(attempts[0]["tool_call_id"], "call-1")
        self.assertEqual(attempts[0]["status"], "rejected")
        self.assertEqual(attempts[0]["validation_error"]["field"], "unexpected")
        self.assertEqual(shadow.source_event_sequences, (1, 2, 3))

    def test_legacy_metadata_uses_task_created_root_goal(self) -> None:
        task = self._task()
        events = (RuntimeEvent("evt-1", "task-1", 1, "task.created", {
            "goal": "legacy durable root goal",
        }),)
        memory = EffectiveWorkingMemory(
            WorkingMemorySnapshot.initial("task-1", task.goal)
        )

        shadow = ActiveContextProjector.project(
            task, self._checkpoint(), memory,
            EvidenceQuestionProjection("task-1"), self._session_prompt(), events,
            model_call=1,
        )

        self.assertEqual(
            shadow.projection.task["root_goal"], "legacy durable root goal"
        )
        self.assertEqual(shadow.projection.task["orchestration_mode"], "LEGACY")
        self.assertEqual(
            shadow.root_goal_hash, canonical_hash("legacy durable root goal")
        )
        self.assertNotIn(
            "legacy durable root goal", json.dumps(shadow.event_payload())
        )


if __name__ == "__main__":
    unittest.main()
