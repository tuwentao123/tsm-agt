from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ModelInvocationFailed, TaskState
from tsm_agt.ports import (
    EvidenceQuestion, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ProviderCapabilities, RuntimeStorePort, TextBlock, ToolCall,
    ToolCallBlock, ToolResultBlock,
)


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
