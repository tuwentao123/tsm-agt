from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import InvalidTurnState, ModelInvocationFailed, TaskState
from tsm_agt.ports import (
    FinishReason,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    RuntimeStorePort,
    TextBlock,
)


class FailingModelProvider(EchoModelProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise RuntimeError("provider unavailable")


class InvalidRoleModelProvider(EchoModelProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            message=Message(
                message_id="invalid",
                role=MessageRole.USER,
                content=(TextBlock("wrong role"),),
            )
        )


class KernelTurnTest(unittest.IsolatedAsyncioTestCase):
    async def _create_executing_task(self, model: EchoModelProvider | None = None):
        application = compose_fixture_application(model)
        await application.registry.start_all()
        temp_dir = tempfile.TemporaryDirectory()
        task = await application.kernel.create_task(
            "answer user", Path(temp_dir.name), task_id="task-1"
        )
        for state in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, state, f"move to {state.value}"
            )
        return application, temp_dir, task

    async def test_text_turn_records_started_model_and_completed_events(self) -> None:
        application, temp_dir, task = await self._create_executing_task()
        try:
            result = await application.kernel.run_text_turn(task.task_id, "hello agent")

            self.assertEqual(result.assistant_message.role, MessageRole.ASSISTANT)
            self.assertEqual(result.assistant_message.text, "hello agent")
            self.assertEqual(result.finish_reason, FinishReason.STOP)
            self.assertGreater(result.usage.input_tokens, 2)
            self.assertEqual(result.usage.output_tokens, 2)

            store = application.registry.require(RuntimeStorePort)
            events = await store.read_events(task.task_id)
            self.assertEqual(
                [event.event_type for event in events[-3:]],
                ["turn.started", "llm.completed", "turn.completed"],
            )
            self.assertEqual(events[-3].payload["turn_id"], result.turn_id)
            self.assertEqual(events[-2].payload["turn_id"], result.turn_id)
            self.assertEqual(events[-2].payload["message"]["role"], "assistant")
            self.assertNotIn("input", events[-3].payload)
            self.assertNotIn("input_content_hash", events[-3].payload)
            self.assertEqual(events[-3].payload["input_role"], "user")
            self.assertIn("effective_prompt_hash", events[-2].payload)
            self.assertIn("prompt_manifest_hash", events[-2].payload)
            diagnostics = events[-2].payload["response_diagnostics"]
            self.assertIsNone(
                diagnostics["checks"]["transport_matches_adapter"]
            )
            self.assertTrue(
                diagnostics["checks"]["kernel_matches_persisted"]
            )
            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state,
                TaskState.EXECUTING,
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_model_failure_records_events_and_fails_task(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            FailingModelProvider()
        )
        try:
            with self.assertRaisesRegex(ModelInvocationFailed, "provider unavailable"):
                await application.kernel.run_text_turn(task.task_id, "hello")

            store = application.registry.require(RuntimeStorePort)
            events = await store.read_events(task.task_id)
            self.assertEqual(
                [event.event_type for event in events[-4:]],
                [
                    "turn.started",
                    "llm.failed",
                    "turn.failed",
                    "task.state_changed",
                ],
            )
            self.assertEqual(events[-3].payload["error_type"], "RuntimeError")
            self.assertIn("effective_prompt_hash", events[-3].payload)
            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state,
                TaskState.FAILED,
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_invalid_provider_response_fails_closed(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            InvalidRoleModelProvider()
        )
        try:
            with self.assertRaisesRegex(ModelInvocationFailed, "assistant role"):
                await application.kernel.run_text_turn(task.task_id, "hello")

            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state, TaskState.FAILED
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_turn_requires_executing_task(self) -> None:
        application = compose_fixture_application()
        await application.registry.start_all()
        temp_dir = tempfile.TemporaryDirectory()
        try:
            task = await application.kernel.create_task(
                "answer user", Path(temp_dir.name), task_id="task-1"
            )

            with self.assertRaisesRegex(InvalidTurnState, "must be EXECUTING"):
                await application.kernel.run_text_turn(task.task_id, "hello")

            store = application.registry.require(RuntimeStorePort)
            self.assertEqual(len(await store.read_events(task.task_id)), 1)
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
