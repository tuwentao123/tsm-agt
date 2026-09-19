from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_final_acceptance import (
    RuleBasedFinalAcceptancePolicy,
)
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.cli import _chat
from tsm_agt.core import (
    AgentClarificationSuspended, AgentTurnResult, ClarificationReplyInput,
    ClarificationRequest, AcceptanceStatus, ClarificationTokenMismatch, TaskState,
)
from tsm_agt.ports import (
    AdapterDescriptor, EvidenceQuestion, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelUsage, ProviderCapabilities, RuntimeStorePort, TextBlock,
    ToolCall, ToolCallBlock, ToolResult, ToolResultBlock,
)


class ClarifyingModel(EchoModelProvider):
    descriptor = AdapterDescriptor(
        "fixture.clarifying-model", "1.0", "ModelProviderPort", "1.0",
        frozenset({"text", "tools"}),
    )
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next((
            block.result for message in reversed(request.messages)
            for block in message.content if isinstance(block, ToolResultBlock)
        ), None)
        if result is None:
            return ModelResponse(
                Message(
                    "clarification-call", MessageRole.ASSISTANT,
                    (
                        TextBlock("I need one material choice."),
                        ToolCallBlock(ToolCall(
                            "clarification-tool-call", "core.request_input",
                            {
                                "question": "Which theme should I implement?",
                                "choices": [
                                    {"value": "dark", "label": "Dark theme"},
                                    {"value": "light", "label": "Light theme"},
                                ],
                                "reason": "The choice changes generated files.",
                                "required": True,
                            },
                        )),
                    ),
                ), FinishReason.TOOL_CALL, ModelUsage(2, 2),
            )
        return ModelResponse(
            Message(
                "clarification-final", MessageRole.ASSISTANT,
                (TextBlock(f"selected: {result.data['answer']}"),),
            ), FinishReason.STOP, ModelUsage(2, 2),
        )


class ScriptedInput:
    def __init__(self, values: list[str]):
        self.values = iter(values)

    def __call__(self, prompt: str) -> str:
        try:
            return next(self.values)
        except StopIteration as error:
            raise EOFError from error


class ClarificationProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def _executing_task(self, application, root: Path, task_id: str):
        task = await application.kernel.create_task(
            "clarify material choice", root, task_id=task_id
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

    async def test_pauses_and_resumes_same_turn_with_bound_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = compose_fixture_application(
                model_adapter=ClarifyingModel(), tool_adapters=()
            )
            await application.registry.start_all()
            try:
                task = await self._executing_task(
                    application, Path(directory), "task-clarification"
                )
                suspended = await application.kernel.run_agent_turn(
                    task.task_id, "build it"
                )
                self.assertIsInstance(suspended, AgentClarificationSuspended)
                assert isinstance(suspended, AgentClarificationSuspended)
                waiting = await application.kernel.get_task(task.task_id)
                self.assertEqual(waiting.state, TaskState.AWAITING_USER)
                self.assertIsNotNone(waiting.pending_clarification)

                with self.assertRaises(ClarificationTokenMismatch):
                    await application.kernel.resolve_agent_clarification(
                        suspended.request_id, "wrong-token", "dark"
                    )
                still_waiting = await application.kernel.get_task(task.task_id)
                self.assertEqual(still_waiting.state, TaskState.AWAITING_USER)
                completed = await application.kernel.dispatch_input_event(
                    ClarificationReplyInput(
                        suspended.request_id, suspended.resume_token, answer="dark"
                    )
                )
                self.assertIsInstance(completed, AgentTurnResult)
                assert isinstance(completed, AgentTurnResult)
                self.assertEqual(completed.turn_id, suspended.turn_id)
                self.assertEqual(completed.assistant_message.text, "selected: dark")
                self.assertEqual(completed.model_calls, 2)
                self.assertEqual(completed.tool_calls, 1)
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                types = [event.event_type for event in events]
                self.assertIn("clarification.requested", types)
                self.assertIn("clarification.resolved", types)
                rendered_events = str([
                    event.payload for event in events
                    if event.event_type.startswith("clarification.")
                ])
                self.assertNotIn("Which theme should I implement?", rendered_events)
                self.assertNotIn("selected: dark", rendered_events)
                self.assertNotIn(suspended.resume_token, str(waiting.to_data()))
                questions = await application.kernel.get_evidence_questions(
                    task.task_id
                )
                self.assertEqual(questions.records, ())
            finally:
                await application.registry.stop_all()

    async def test_answered_interaction_is_not_final_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = compose_fixture_application(
                model_adapter=ClarifyingModel(), tool_adapters=(),
                require_evidence_questions=True,
                final_acceptance_policy_adapter=RuleBasedFinalAcceptancePolicy(),
            )
            await application.registry.start_all()
            try:
                task = await self._executing_task(
                    application, Path(directory), "task-interaction-acceptance"
                )
                suspended = await application.kernel.run_agent_turn(
                    task.task_id, "build it"
                )
                self.assertIsInstance(suspended, AgentClarificationSuspended)
                assert isinstance(suspended, AgentClarificationSuspended)
                completed = await application.kernel.resolve_agent_clarification(
                    suspended.request_id, suspended.resume_token, "dark"
                )
                self.assertIsInstance(completed, AgentTurnResult)
                await application.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify interaction"
                )
                verification = await application.kernel.verify_task_acceptance(
                    task.task_id
                )
                self.assertEqual(verification.status, AcceptanceStatus.PASSED)
                criteria = {
                    item.criterion_id: item for item in verification.criteria
                }
                self.assertEqual(
                    criteria["final-evidence-integrity"].status,
                    AcceptanceStatus.PASSED,
                )
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                self.assertFalse(any(
                    event.event_type == "evidence.question_bound"
                    for event in events
                ))
            finally:
                await application.registry.stop_all()

    async def test_legacy_interaction_question_does_not_block_verification(self) -> None:
        """Old Events remain readable after interaction semantics upgrade."""
        with tempfile.TemporaryDirectory() as directory:
            application = compose_fixture_application(
                model_adapter=ClarifyingModel(), tool_adapters=(),
                final_acceptance_policy_adapter=RuleBasedFinalAcceptancePolicy(),
            )
            await application.registry.start_all()
            try:
                task = await self._executing_task(
                    application, Path(directory), "task-legacy-interaction"
                )
                legacy_call = ToolCall(
                    "legacy-request-input", "core.request_input",
                    {"question": "Which option?", "reason": "material"},
                    EvidenceQuestion("Q-legacy-interaction", "Which option?"),
                )
                await application.kernel._bind_evidence_question(
                    task.task_id, "turn-legacy", legacy_call
                )
                await application.kernel._observe_evidence_question(
                    task.task_id, "turn-legacy", legacy_call,
                    ToolResult(
                        legacy_call.call_id, True, data={"answer": "one"}
                    ), None,
                )
                await application.kernel._append_events(task.task_id, (
                    ("llm.completed", {
                        "turn_id": "turn-legacy",
                        "message": {
                            "message_id": "legacy-answer",
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Done."}],
                        },
                        "finish_reason": "stop",
                    }),
                    ("turn.completed", {"turn_id": "turn-legacy"}),
                ))
                await application.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify legacy interaction"
                )
                verification = await application.kernel.verify_task_acceptance(
                    task.task_id
                )
                self.assertEqual(verification.status, AcceptanceStatus.PASSED)
                final_integrity = next(
                    item for item in verification.criteria
                    if item.criterion_id == "final-evidence-integrity"
                )
                self.assertEqual(final_integrity.status, AcceptanceStatus.PASSED)
            finally:
                await application.registry.stop_all()

    async def test_sqlite_restart_resumes_pending_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = compose_fixture_application(
                model_adapter=ClarifyingModel(), tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await first.registry.start_all()
            task = await self._executing_task(first, root, "task-restart-clarify")
            suspended = await first.kernel.run_agent_turn(task.task_id, "build it")
            assert isinstance(suspended, AgentClarificationSuspended)
            await first.registry.stop_all()

            second = compose_fixture_application(
                model_adapter=ClarifyingModel(), tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await second.registry.start_all()
            try:
                completed = await second.kernel.resolve_agent_clarification(
                    suspended.request_id, suspended.resume_token, "light"
                )
                assert isinstance(completed, AgentTurnResult)
                self.assertEqual(completed.assistant_message.text, "selected: light")
            finally:
                await second.registry.stop_all()

    async def test_chat_answers_before_creating_another_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = compose_fixture_application(
                model_adapter=ClarifyingModel(), tool_adapters=()
            )
            output: list[str] = []
            await _chat(
                Path(directory), input_fn=ScriptedInput([
                    "build it", "dark", "/exit",
                ]), output_fn=output.append, application_factory=lambda: application,
            )
            self.assertIn("input required", output)
            self.assertIn("question: Which theme should I implement?", output)
            self.assertIn("agent> selected: dark", output)
            self.assertEqual(
                sum(line.startswith("task: ") for line in output), 1
            )

    def test_expired_resume_token_is_rejected(self) -> None:
        now = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
        request = ClarificationRequest(
            "request", "task", "turn",
            ToolCall("call", "core.request_input", {}),
            "Question?", (), "Material choice", True,
            ClarificationRequest.hash_resume_token("token"),
            now, now + timedelta(seconds=1),
        )
        self.assertTrue(request.accepts_token("token", now))
        self.assertFalse(
            request.accepts_token("token", now + timedelta(seconds=2))
        )


if __name__ == "__main__":
    unittest.main()
