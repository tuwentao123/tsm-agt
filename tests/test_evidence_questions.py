from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    EvidenceObservationKind, EvidenceQuestionProjection,
    EvidenceQuestionStatus, ModelInvocationFailed, TaskState,
    ToolActionDisposition,
)
from tsm_agt.ports import (
    EvidenceDelta, EvidenceItem, EvidenceQuestion, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ProviderCapabilities, RuntimeStorePort, TextBlock, ToolCall,
    ToolCallBlock, ToolResult, ToolResultBlock,
)


class EvidenceQuestionLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.question = EvidenceQuestion("Q1", "Where is the behavior defined?")
        self.call = ToolCall(
            "call-1", "core.search_text", {"query": "Target"},
            self.question,
        )
        self.bound = EvidenceQuestionProjection("task-1").bind(
            self.question, "turn-1", self.call.call_id, event_sequence=1
        )

    def test_success_and_failure_are_distinct_observations(self) -> None:
        exclusion = EvidenceDelta(
            "Q1", (EvidenceItem(
                "new_exclusions", "empty-hash", "no-results:search"
            ),), "ok", 0,
        )
        succeeded, success = self.bound.observe(
            self.call, ToolResult(
                self.call.call_id, True, {"matches": []}
            ), exclusion, event_sequence=2,
        )
        self.assertEqual(success.status, EvidenceQuestionStatus.RESOLVED)
        self.assertEqual(
            success.observation_kind, EvidenceObservationKind.EMPTY_RESULT
        )
        failed, failure = self.bound.observe(
            self.call, ToolResult(
                self.call.call_id, False, error_code="PERMISSION_DENIED"
            ), None, event_sequence=2,
        )
        self.assertEqual(failure.status, EvidenceQuestionStatus.BLOCKED)
        self.assertEqual(failure.blocking_reason, "PERMISSION_DENIED")
        self.assertNotEqual(succeeded.to_data(), failed.to_data())

    def test_recoverable_failure_stays_open_and_redirect_drops_it(self) -> None:
        recoverable, record = self.bound.observe(
            self.call, ToolResult(
                self.call.call_id, False, error_code="PATH_CONTEXT_REQUIRED",
                retryable=True, meta={"recoverable_input": True},
            ), None, event_sequence=2,
        )
        self.assertEqual(record.status, EvidenceQuestionStatus.OPEN)
        dropped, records = recoverable.drop_open(event_sequence=3)
        self.assertEqual(len(records), 1)
        dropped_record = dropped.get("Q1")
        self.assertIsNotNone(dropped_record)
        assert dropped_record is not None
        self.assertEqual(dropped_record.status, EvidenceQuestionStatus.DROPPED)

    def test_rephrased_stable_id_keeps_original_question_definition(self) -> None:
        rebound = self.bound.bind(
            EvidenceQuestion("Q1", "Where exactly is that behavior implemented?"),
            "turn-1", "call-2",
        )
        record = rebound.get("Q1")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.question, "Where is the behavior defined?")
        self.assertEqual(record.tool_call_ids, ("call-1", "call-2"))

    def test_unexecuted_action_disposition_closes_or_blocks_without_evidence(self):
        replaced, replaced_record = self.bound.dispose(
            self.call, ToolActionDisposition.REPLACE, "change_method",
            event_sequence=2,
        )
        self.assertIsNotNone(replaced_record)
        assert replaced_record is not None
        self.assertEqual(replaced_record.status, EvidenceQuestionStatus.DROPPED)
        self.assertEqual(
            replaced_record.observation_kind,
            EvidenceObservationKind.ACTION_REPLACED,
        )
        self.assertEqual(replaced_record.evidence_references, ())

        denied, denied_record = self.bound.dispose(
            self.call, ToolActionDisposition.DENY, "policy_denied",
            event_sequence=2,
        )
        self.assertIsNotNone(denied_record)
        assert denied_record is not None
        self.assertEqual(denied_record.status, EvidenceQuestionStatus.BLOCKED)
        self.assertEqual(denied_record.evidence_references, ())
        self.assertNotEqual(replaced.to_data(), denied.to_data())


class EvidenceQuestionModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self, *, bind_question: bool = True) -> None:
        super().__init__()
        self.bind_question = bind_question

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next((
            block.result for message in request.messages
            for block in message.content if isinstance(block, ToolResultBlock)
        ), None)
        if result is not None:
            return ModelResponse(Message(
                "final", MessageRole.ASSISTANT, (TextBlock("Evidence collected."),)
            ))
        question = (
            EvidenceQuestion("E1", "What text does the fixture return?")
            if self.bind_question else None
        )
        return ModelResponse(
            Message(
                "tool-call", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "call-evidence", "fixture.echo", {"text": "observed"},
                    question,
                )),),
            ),
            FinishReason.TOOL_CALL,
        )


class EvidenceQuestionKernelTest(unittest.IsolatedAsyncioTestCase):
    async def _executing_task(self, application, root: Path):
        task = await application.kernel.create_task(
            "collect evidence", root, task_id="task-evidence-question"
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, state, state.value
            )
        return task

    async def test_bound_question_is_persisted_before_tool_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = compose_fixture_application(
                model_adapter=EvidenceQuestionModel(),
                require_evidence_questions=True,
            )
            await application.registry.start_all()
            try:
                task = await self._executing_task(application, Path(directory))
                result = await application.kernel.run_agent_turn(
                    task.task_id, "inspect"
                )
                self.assertEqual(result.assistant_message.text, "Evidence collected.")
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                event_types = [event.event_type for event in events]
                bound_index = event_types.index("evidence.question_bound")
                self.assertLess(bound_index, event_types.index("tool.requested"))
                payload = events[bound_index].payload
                self.assertEqual(payload["question_id"], "E1")
                self.assertEqual(
                    payload["question"], "What text does the fixture return?"
                )
                requested = next(
                    event for event in events if event.event_type == "tool.requested"
                )
                self.assertEqual(
                    requested.payload["call"]["evidence_question"]["question_id"],
                    "E1",
                )
                lifecycle = await application.kernel.get_evidence_questions(
                    task.task_id
                )
                record = lifecycle.get("E1")
                self.assertIsNotNone(record)
                assert record is not None
                self.assertEqual(record.status, EvidenceQuestionStatus.RESOLVED)
                self.assertEqual(record.tool_call_ids, ("call-evidence",))
                changed = next(
                    event for event in events
                    if event.event_type == "evidence.question_state_changed"
                )
                self.assertEqual(changed.payload["previous_status"], "OPEN")
                self.assertEqual(changed.payload["next_status"], "RESOLVED")
                flow = await application.kernel.get_flow_projection(task.task_id)
                rendered_flow = str(flow.to_data())
                self.assertIn("evidence_question", rendered_flow)
                self.assertNotIn(
                    "What text does the fixture return?", rendered_flow
                )
            finally:
                await application.registry.stop_all()

    async def test_unbound_model_tool_call_is_rejected_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = compose_fixture_application(
                model_adapter=EvidenceQuestionModel(bind_question=False),
                require_evidence_questions=True,
            )
            await application.registry.start_all()
            try:
                task = await self._executing_task(application, Path(directory))
                with self.assertRaisesRegex(
                    ModelInvocationFailed, "must bind one evidence_question"
                ):
                    await application.kernel.run_agent_turn(task.task_id, "inspect")
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                self.assertFalse(any(
                    event.event_type == "tool.started" for event in events
                ))
            finally:
                await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
