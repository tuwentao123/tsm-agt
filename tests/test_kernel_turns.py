from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import InvalidTurnState, ModelInvocationFailed, TaskState
from tsm_agt.ports import (
    FinishReason,
    AssistantConclusion,
    AdapterContext,
    ConclusionBlock,
    ConclusionProtocolMode,
    ConclusionClaim,
    ConclusionKind,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
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


class ConclusionModelProvider(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(Message(
            "conclusion-final", MessageRole.ASSISTANT,
            (
                TextBlock("A conclusion was recorded."),
                ConclusionBlock(AssistantConclusion(1, (ConclusionClaim(
                    "claim-1", ConclusionKind.HUMAN_CONFIRMATION_REQUIRED,
                    "human confirmation remains required", (), (), (),
                    "Runtime cannot decide this automatically",
                ),))),
            ),
        ), FinishReason.STOP)


class StructuredConclusionRecordingModel(EchoModelProvider):
    capabilities = ProviderCapabilities(
        tools=True, structured_conclusion=True, context_window=4096,
    )

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(Message(
            "structured-conclusion-final", MessageRole.ASSISTANT,
            (TextBlock("Visible fallback."),),
        ), FinishReason.STOP)


class PromptRecordingModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(Message(
            "prompt-recording-final", MessageRole.ASSISTANT,
            (TextBlock("ok"),),
        ), FinishReason.STOP)


class ProviderMetadataModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            Message(
                "provider-metadata-final", MessageRole.ASSISTANT,
                (TextBlock("ok"),),
            ),
            FinishReason.STOP,
            diagnostics={"provider": {
                "response_id": "chat-observed-1",
                "model": "served-alias",
                "system_fingerprint": "fp-kernel",
                "routing": {"serving_pipereplica": "replica-k"},
                "completion_tokens_details": {"reasoning_tokens": 3},
            }},
        )


class ControlledConclusionTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.requests.append({
            "url": url,
            "headers": dict(headers),
            "payload": dict(payload),
            "timeout_seconds": timeout_seconds,
        })
        return {
            "id": "controlled-kernel-response",
            "choices": [{
                "message": {"role": "assistant", "content": "Visible answer."},
                "finish_reason": "stop",
            }],
            "usage": {},
        }


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

    async def test_llm_completed_persists_provider_diagnostics(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            ProviderMetadataModel()
        )
        try:
            await application.kernel.run_text_turn(task.task_id, "hello agent")

            store = application.registry.require(RuntimeStorePort)
            events = await store.read_events(task.task_id)
            completed = next(
                event for event in events
                if event.event_type == "llm.completed"
            )
            provider = completed.payload["response_diagnostics"]["provider"]
            self.assertEqual(provider["response_id"], "chat-observed-1")
            self.assertEqual(provider["model"], "served-alias")
            self.assertEqual(provider["system_fingerprint"], "fp-kernel")
            self.assertEqual(
                provider["routing"]["serving_pipereplica"], "replica-k"
            )
            self.assertEqual(
                provider["completion_tokens_details"]["reasoning_tokens"], 3
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_model_prompt_is_anchored_to_the_current_date(self) -> None:
        model = PromptRecordingModel()
        application, temp_dir, task = await self._create_executing_task(model)
        try:
            await application.kernel.run_text_turn(task.task_id, "hello agent")

            self.assertTrue(model.requests, "the model must have been called")
            joined = "\n".join(
                block.text
                for request in model.requests
                for message in request.messages
                for block in message.content
                if isinstance(block, TextBlock)
            )
            self.assertIn("Today is ", joined)
            self.assertIn("never a future or simulated", joined)
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_agent_final_conclusion_and_session_result_commit_atomically(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            ConclusionModelProvider()
        )
        try:
            result = await application.kernel.run_agent_turn(task.task_id, "answer")
            self.assertEqual(result.assistant_message.text, "A conclusion was recorded.")
            store = application.registry.require(RuntimeStorePort)
            events = await store.read_events(task.task_id)
            validation = next(
                event for event in events
                if event.event_type == "conclusion.references_validated"
            )
            completed = next(event for event in events if event.event_type == "turn.completed")
            session_events = await store.read_session_events(task.session_id)
            recorded = next(
                event for event in session_events
                if event.event_type == "session.task_result_recorded"
            )
            self.assertEqual(recorded.payload["answer_event_ref"], events[-3].event_id)
            self.assertEqual(
                recorded.payload["conclusion_validation"], validation.payload["validation"]
            )
            self.assertGreater(completed.sequence, validation.sequence)
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_kernel_passes_structured_conclusion_protocol_to_capable_model(self) -> None:
        model = StructuredConclusionRecordingModel()
        application, temp_dir, task = await self._create_executing_task(model)
        try:
            result = await application.kernel.run_agent_turn(task.task_id, "answer")
            self.assertEqual(result.assistant_message.text, "Visible fallback.")
            self.assertEqual(
                model.requests[-1].conclusion_protocol_mode,
                ConclusionProtocolMode.REQUIRE_STRUCTURED,
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_kernel_requests_controlled_conclusion_protocol_when_native_is_unavailable(self) -> None:
        transport = ControlledConclusionTransport()
        model = OpenAICompatibleModelProvider(
            "https://models.example.test/v1", "test-model", "test-secret",
            streaming=False, native_structured_conclusion=False,
            transport=transport,
        )
        application, temp_dir, task = await self._create_executing_task(model)
        try:
            result = await application.kernel.run_agent_turn(task.task_id, "answer")
            self.assertEqual(result.assistant_message.text, "Visible answer.")
            self.assertTrue(model.capabilities.structured_conclusion)
            self.assertIn("structured-conclusion", model.descriptor.capabilities)
            request_messages = transport.requests[-1]["payload"]["messages"]
            self.assertEqual(request_messages[0]["role"], "system")
            self.assertIn(
                "end the response with exactly one",
                request_messages[0]["content"],
            )
            store = application.registry.require(RuntimeStorePort)
            events = await store.read_events(task.task_id)
            completed = next(
                event for event in events if event.event_type == "llm.completed"
            )
            self.assertEqual(
                completed.payload["response_diagnostics"]["conclusion_protocol"],
                {
                    "requested": ConclusionProtocolMode.REQUIRE_STRUCTURED.value,
                    "status": "plain_text_fallback",
                },
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_final_result_unique_constraint_does_not_fallback_to_split_commits(self) -> None:
        application, temp_dir, task = await self._create_executing_task()
        try:
            store = application.registry.require(RuntimeStorePort)
            original_commit = store.commit
            original_commit_session = store.commit_session
            store.commit = AsyncMock(wraps=original_commit)  # type: ignore[method-assign]
            store.commit_session = AsyncMock(wraps=original_commit_session)  # type: ignore[method-assign]
            store.commit_session_and_task = AsyncMock(  # type: ignore[method-assign]
                side_effect=ValueError(
                    "UNIQUE constraint failed: "
                    "session_task_commands.session_id, session_task_commands.task_id"
                )
            )
            with self.assertRaisesRegex(ValueError, "session_task_commands"):
                await application.kernel._commit_final_agent_result(
                    task_id=task.task_id,
                    turn_id="turn-atomic-failure",
                    user_message=Message(
                        "user-atomic", MessageRole.USER, (TextBlock("answer"),),
                    ),
                    assistant_message=Message(
                        "assistant-atomic", MessageRole.ASSISTANT,
                        (TextBlock("answer"),),
                    ),
                    llm_payload={"model_call": 1},
                )
            store.commit.assert_not_awaited()
            store.commit_session.assert_not_awaited()
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
