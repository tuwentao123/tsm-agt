from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.cli import _chat
from tsm_agt.core import TaskState
from tsm_agt.ports import (
    AdapterContext, Message, MessageRole, ModelRequest, ModelResponse, ModelStreamCompleted,
    ModelTextDelta, ModelUsage, ProviderCapabilities, RuntimeStorePort, TextBlock,
)


class StreamingEchoModel(EchoModelProvider):
    capabilities = ProviderCapabilities(stream_cancel=True, context_window=4096)

    async def stream_complete(self, request: ModelRequest):
        text = next(
            message.text for message in request.messages
            if message.role is MessageRole.USER
        )
        midpoint = max(1, len(text) // 2)
        yield ModelTextDelta(text[:midpoint])
        yield ModelTextDelta(text[midpoint:])
        yield ModelStreamCompleted(ModelResponse(
            Message(
                f"stream-{request.turn_id}", MessageRole.ASSISTANT,
                (TextBlock(text),),
            ),
            usage=ModelUsage(2, 2),
        ))


class InterruptOnceModel(StreamingEchoModel):
    def __init__(self) -> None:
        super().__init__()
        self.interrupt_next = True

    async def stream_complete(self, request: ModelRequest):
        if self.interrupt_next:
            self.interrupt_next = False
            yield ModelTextDelta("partial")
            raise asyncio.CancelledError
        async for event in super().stream_complete(request):
            yield event


class AtomicCompletionModel(StreamingEchoModel):
    """Expose a non-streaming/fallback completion through the stream port."""

    async def stream_complete(self, request: ModelRequest):
        yield ModelStreamCompleted(await self.complete(request))


class MismatchedStreamingModel(StreamingEchoModel):
    async def stream_complete(self, request: ModelRequest):
        yield ModelTextDelta("visible answer")
        yield ModelStreamCompleted(ModelResponse(
            Message(
                f"mismatch-{request.turn_id}", MessageRole.ASSISTANT,
                (TextBlock("different committed answer"),),
            )
        ))


class DiagnosticStreamingTransport:
    async def stream_sse(self, url, headers, payload, timeout_seconds):
        yield json.dumps({
            "id": "chat-diagnostic",
            "choices": [{
                "delta": {"content": "private complete answer"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        })
        yield "[DONE]"


async def executing_task(application, root: Path, *, session_id=None):
    task = await application.kernel.create_task(
        "streaming request", root, session_id=session_id
    )
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await application.kernel.transition_task(
            task.task_id, state, f"move to {state.value}"
        )
    return task


class ModelStreamingTest(unittest.IsolatedAsyncioTestCase):
    async def test_openai_stream_diagnostics_survive_sqlite_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            provider = OpenAICompatibleModelProvider(
                "https://models.example.test/v1", "test-model", "secret",
                transport=DiagnosticStreamingTransport(),
            )
            application = compose_fixture_application(
                model_adapter=provider, store_adapter=SQLiteRuntimeStore(database),
                tool_adapters=(),
            )
            await application.registry.start_all()
            try:
                task = await executing_task(application, root)
                result = await application.kernel.run_agent_turn(
                    task.task_id, "diagnose response"
                )
                self.assertEqual(
                    result.assistant_message.text, "private complete answer"
                )
            finally:
                await application.registry.stop_all()

            reopened = SQLiteRuntimeStore(database)
            await reopened.start(AdapterContext(
                config={}, emit_event=lambda _type, _payload: None
            ))
            try:
                events = await reopened.read_events(task.task_id)
            finally:
                await reopened.stop(datetime.now(timezone.utc))
            completed = next(
                event for event in events if event.event_type == "llm.completed"
            )
            diagnostics = completed.payload["response_diagnostics"]
            self.assertTrue(
                diagnostics["checks"]["transport_matches_adapter"]
            )
            self.assertTrue(
                diagnostics["checks"]["adapter_matches_kernel"]
            )
            self.assertTrue(
                diagnostics["checks"]["kernel_matches_persisted"]
            )
            self.assertTrue(diagnostics["transport"]["saw_done"])
            serialized = json.dumps(diagnostics, ensure_ascii=False)
            self.assertNotIn("private complete answer", serialized)
            self.assertNotIn("secret", serialized)

    async def test_kernel_accepts_atomic_completion_without_text_deltas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=AtomicCompletionModel(), tool_adapters=()
            )
            await application.registry.start_all()
            try:
                task = await executing_task(application, root)
                deltas: list[str] = []
                result = await application.kernel.run_agent_turn(
                    task.task_id, "atomic answer", on_text_delta=deltas.append
                )
                self.assertEqual(deltas, [])
                self.assertEqual(result.assistant_message.text, "atomic answer")
            finally:
                await application.registry.stop_all()

    async def test_kernel_rejects_mismatch_after_visible_text_deltas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=MismatchedStreamingModel(), tool_adapters=()
            )
            await application.registry.start_all()
            try:
                task = await executing_task(application, root)
                with self.assertRaisesRegex(
                    Exception, "stream text does not match"
                ):
                    await application.kernel.run_agent_turn(
                        task.task_id, "mismatch", on_text_delta=lambda _text: None
                    )
            finally:
                await application.registry.stop_all()

    async def test_kernel_delivers_deltas_and_commits_only_complete_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=StreamingEchoModel(), tool_adapters=()
            )
            await application.registry.start_all()
            try:
                task = await executing_task(application, root)
                deltas: list[str] = []
                result = await application.kernel.run_agent_turn(
                    task.task_id, "hello stream", on_text_delta=deltas.append
                )
                self.assertEqual("".join(deltas), "hello stream")
                self.assertEqual(result.assistant_message.text, "hello stream")
                store = application.registry.require(RuntimeStorePort)
                events = await store.read_events(task.task_id)
                completed = [
                    event for event in events if event.event_type == "llm.completed"
                ]
                self.assertEqual(len(completed), 1)
                self.assertEqual(
                    completed[0].payload["message"]["content"][0]["text"],
                    "hello stream",
                )
                diagnostics = completed[0].payload["response_diagnostics"]
                self.assertIsNone(
                    diagnostics["checks"]["transport_matches_adapter"]
                )
                self.assertIsNone(
                    diagnostics["checks"]["adapter_matches_kernel"]
                )
                self.assertTrue(
                    diagnostics["checks"]["kernel_matches_persisted"]
                )
            finally:
                await application.registry.stop_all()

    async def test_cancel_preserves_checkpoint_and_resume_resamples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = InterruptOnceModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            await application.registry.start_all()
            try:
                session = await application.kernel.create_session("stream test")
                task = await executing_task(
                    application, root, session_id=session.session_id
                )
                deltas: list[str] = []
                with self.assertRaises(asyncio.CancelledError):
                    await application.kernel.run_agent_turn(
                        task.task_id, "recover me", on_text_delta=deltas.append
                    )
                self.assertEqual(deltas, ["partial"])
                interrupted = await application.kernel.interrupt_agent_turn(
                    task.task_id
                )
                self.assertEqual(interrupted.state, TaskState.INTERRUPTED)
                self.assertIsNotNone(interrupted.active_agent_checkpoint)
                store = application.registry.require(RuntimeStorePort)
                event_types = [
                    event.event_type for event in await store.read_events(task.task_id)
                ]
                self.assertIn("turn.interrupted", event_types)
                self.assertNotIn("llm.completed", event_types)
                self.assertNotIn("turn.completed", event_types)
                session_events = await store.read_session_events(session.session_id)
                self.assertFalse(any(
                    event.event_type == "session.task_result_recorded"
                    for event in session_events
                ))

                resumed_deltas: list[str] = []
                result = await application.kernel.resume_checkpointed_agent_turn(
                    task.task_id, on_text_delta=resumed_deltas.append
                )
                self.assertEqual("".join(resumed_deltas), "recover me")
                self.assertEqual(result.assistant_message.text, "recover me")
                self.assertEqual(
                    (await application.kernel.get_task(task.task_id)).state,
                    TaskState.EXECUTING,
                )
            finally:
                await application.registry.stop_all()

    async def test_chat_streams_once_without_reprinting_final_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=StreamingEchoModel(), tool_adapters=()
            )
            values = iter(["visible chunks", "/exit"])
            output: list[str] = []
            result = await _chat(
                root, input_fn=lambda _prompt: next(values),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertEqual(result, 0)
            self.assertEqual(output.count("agent> visible chunks"), 1)

    async def test_chat_cancel_marks_task_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=InterruptOnceModel(), tool_adapters=()
            )
            output: list[str] = []
            result = await _chat(
                root, input_fn=lambda _prompt: "stop me", output_fn=output.append,
                application_factory=lambda: application,
            )
            self.assertEqual(result, 130)
            task_id = next(
                line.removeprefix("task: ") for line in output
                if line.startswith("task: ")
            )
            await application.registry.start_all()
            try:
                task = await application.kernel.get_task(task_id)
                self.assertEqual(task.state, TaskState.INTERRUPTED)
                self.assertTrue(any("tsm-agt resume" in line for line in output))
            finally:
                await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
