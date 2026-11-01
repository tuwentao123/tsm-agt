"""Timing and lifecycle checks: text must arrive before a model completes."""

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.fixture import EchoModelProvider, EchoToolProvider
from tsm_agt.adapters.openai_compatible.responses import OpenAIResponsesModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import SteeringKind
from tsm_agt.ports import (
    AssistantMessagePhase, CompletionReviewAction, CompletionReviewVerdict, CompletionReviewMode,
    FinishReason, Message, MessageRole, ModelResponse, ModelStreamCompleted,
    ModelTextDelta, ProviderCapabilities, RuntimeStorePort, TextBlock,
    ToolCall, ToolCallBlock, AdapterContext, ModelRequest,
)
from tsm_agt.sdk import EngineeringAgentClient
from tests.test_conversation_loop import _executing, ScriptedCompletionReviewer, text_response


class GatedTextModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, stream_cancel=True, context_window=32768)

    def __init__(self, responses):
        super().__init__()
        self.responses = responses
        self.requests = []
        self.entered = asyncio.Queue()
        self.second = asyncio.Queue()
        self.release = [asyncio.Event() for _ in responses]
        self.finish = [asyncio.Event() for _ in responses]
        for gate in self.finish:
            gate.set()

    async def stream_complete(self, request):
        index = len(self.requests)
        self.requests.append(request)
        response = self.responses[index]
        text = response.message.text
        midpoint = max(1, len(text) // 2)
        yield ModelTextDelta(text[:midpoint], phase=response.message.phase)
        self.entered.put_nowait(index)
        await self.release[index].wait()
        yield ModelTextDelta(text[midpoint:], phase=response.message.phase)
        self.second.put_nowait(index)
        await self.finish[index].wait()
        yield ModelStreamCompleted(response)


class TextStreamingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    async def application(self, model, **options):
        app = compose_fixture_application(model_adapter=model, **options)
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing(app, self.root)
        return app, task

    async def test_text_is_visible_while_model_is_blocked_without_committing_partial_answer(self):
        model = GatedTextModel([text_response("final", "hello world")])
        app, task = await self.application(model, tool_adapters=())
        updates, deltas = [], []
        running = asyncio.create_task(app.kernel.run_agent_turn(
            task.task_id, "say hello", on_progress=updates.append, on_text_delta=deltas.append,
        ))
        await asyncio.wait_for(model.entered.get(), 2)
        self.assertEqual(deltas, ["hello"])
        self.assertFalse(running.done())
        events = await app.registry.require(RuntimeStorePort).read_events(task.task_id)
        self.assertFalse(any(e.event_type == "llm.completed" for e in events))
        model.release[0].set()
        result = await asyncio.wait_for(running, 2)
        self.assertEqual("".join(deltas), result.assistant_message.text)
        states = [p for p in updates if p.kind.value == "text_state"]
        self.assertEqual([p.text_status for p in states], ["reviewing", "committed"])
        self.assertTrue(all(p.phase == "final_answer" for p in states))

    async def test_commentary_stream_precedes_tool_and_final_has_its_own_identity(self):
        note = ModelResponse(Message(
            "note", MessageRole.ASSISTANT,
            (TextBlock("checking inputs"), ToolCallBlock(ToolCall("echo", "fixture.echo", {"text": "input"}))),
            phase=AssistantMessagePhase.COMMENTARY,
        ), FinishReason.TOOL_CALL)
        model = GatedTextModel([note, text_response("final", "final answer")])
        app, task = await self.application(model, tool_adapters=(EchoToolProvider(),))
        updates = []
        running = asyncio.create_task(app.kernel.run_agent_turn(task.task_id, "check inputs", on_progress=updates.append))
        await asyncio.wait_for(model.entered.get(), 2)
        self.assertEqual(updates[-1].kind.value, "text_delta")
        self.assertEqual(updates[-1].phase, "commentary")
        self.assertFalse(any(p.kind.value == "tool_started" for p in updates))
        model.release[0].set()
        await asyncio.wait_for(model.entered.get(), 2)
        self.assertTrue(any(p.kind.value == "tool_completed" for p in updates))
        model.release[1].set()
        await asyncio.wait_for(running, 2)
        committed = [p for p in updates if p.text_status == "committed"]
        self.assertEqual([p.phase for p in committed], ["commentary", "final_answer"])
        self.assertNotEqual(committed[0].text_id, committed[1].text_id)

    async def test_rejected_candidate_is_retracted_before_the_next_candidate_commits(self):
        reviewer = ScriptedCompletionReviewer([
            CompletionReviewVerdict(CompletionReviewAction.CONTINUE, "needs_detail", "continue"),
            CompletionReviewVerdict(CompletionReviewAction.ACCEPT, "accepted", "done"),
        ])
        model = GatedTextModel([text_response("draft", "first candidate"), text_response("final", "accepted answer")])
        app, task = await self.application(
            model, tool_adapters=(), completion_reviewer_adapter=reviewer,
            completion_review_mode=CompletionReviewMode.ALWAYS,
        )
        updates = []
        running = asyncio.create_task(app.kernel.run_agent_turn(task.task_id, "answer", on_progress=updates.append))
        await asyncio.wait_for(model.entered.get(), 2)
        model.release[0].set()
        await asyncio.wait_for(model.entered.get(), 2)
        self.assertTrue(any(p.text_status == "discarded" for p in updates))
        self.assertFalse(any(p.text_status == "committed" for p in updates))
        model.release[1].set()
        result = await asyncio.wait_for(running, 2)
        self.assertEqual(result.assistant_message.text, "accepted answer")
        self.assertEqual(sum(p.text_status == "committed" for p in updates), 1)

    async def test_steering_retracts_the_old_visible_candidate(self):
        model = GatedTextModel([text_response("old", "old candidate"), text_response("new", "new answer")])
        app, task = await self.application(model, tool_adapters=())
        updates = []
        running = asyncio.create_task(app.kernel.run_agent_turn(task.task_id, "answer", on_progress=updates.append))
        await asyncio.wait_for(model.entered.get(), 2)
        await app.kernel.queue_steering(task.task_id, SteeringKind.STEER, "use the new request", "steer")
        model.release[0].set()
        await asyncio.wait_for(model.entered.get(), 2)
        self.assertTrue(any(p.text_status == "discarded" for p in updates))
        model.release[1].set()
        await asyncio.wait_for(running, 2)

    async def test_cancel_closes_the_provisional_stream_without_committing_it(self):
        model = GatedTextModel([text_response("partial", "unfinished answer")])
        app, task = await self.application(model, tool_adapters=())
        updates = []
        running = asyncio.create_task(app.kernel.run_agent_turn(task.task_id, "answer", on_progress=updates.append))
        await asyncio.wait_for(model.entered.get(), 2)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(updates[-1].text_status, "interrupted")
        self.assertFalse(any(p.text_status == "committed" for p in updates))

    async def test_sdk_sse_reconnect_restores_draft_and_pushes_later_text_before_completion(self):
        model = GatedTextModel([text_response("final", "hello world")])
        model.finish[0].clear()
        app = compose_fixture_application(model_adapter=model, tool_adapters=())
        client = EngineeringAgentClient(self.root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        result = await client.submit_task("say hello", command_id="start")
        await asyncio.wait_for(model.entered.get(), 2)
        stream = client.subscribe_task_ui(result.task_id)
        self.addAsyncCleanup(stream.aclose)
        packets = []
        async for packet in stream:
            packets.append(packet)
            if packet["type"] == "text.snapshot":
                break
        draft = packets[-1]["streams"][0]
        self.assertEqual(draft["text"], "hello")
        self.assertNotIn("sequence", packets[-1])
        await stream.aclose()

        reconnected = client.subscribe_task_ui(result.task_id)
        self.addAsyncCleanup(reconnected.aclose)
        async for packet in reconnected:
            if packet["type"] == "text.snapshot":
                self.assertEqual(packet["streams"][0]["text"], "hello")
                break
        model.release[0].set()
        await asyncio.wait_for(model.second.get(), 2)
        async for packet in reconnected:
            if packet.get("progress", {}).get("kind") == "text_delta":
                self.assertEqual(packet["progress"]["text"], " world")
                self.assertNotIn("sequence", packet)
                self.assertEqual(packet["progress"]["text_sequence"], 2)
                break
        self.assertFalse(model.finish[0].is_set())
        model.finish[0].set()
        remaining = [packet async for packet in reconnected]
        self.assertEqual(remaining[-1]["assistant_text"], "hello world")
        self.assertEqual(client.text_stream_snapshot(result.task_id)[1], [])

    async def test_live_cli_displays_a_fragment_before_newline_or_completion(self):
        from tsm_agt.cli import _chat

        class LiveInput:
            supports_live_input = True

            def __init__(self):
                self.count = 0
                self.fragments = []

            async def read(self, prompt):
                if prompt == "control> ":
                    await asyncio.Event().wait()
                self.count += 1
                return "say hello" if self.count == 1 else "/exit"

            def set_stream_text(self, text, label="agent"):
                self.fragments.append(text)

        model = GatedTextModel([text_response("final", "hello world")])
        app = compose_fixture_application(model_adapter=model, tool_adapters=())
        terminal = LiveInput()
        output = io.StringIO()
        with patch("tsm_agt.cli.platform_line_input", return_value=terminal), redirect_stdout(output):
            running = asyncio.create_task(_chat(self.root, application_factory=lambda: app))
            await asyncio.wait_for(model.entered.get(), 2)
            self.assertIn("hello", terminal.fragments)
            self.assertFalse(running.done())
            model.release[0].set()
            self.assertEqual(await asyncio.wait_for(running, 2), 0)
        self.assertEqual(output.getvalue().count("agent> hello world"), 1)

    async def test_slow_subscriber_recovers_when_fragment_history_is_trimmed(self):
        class BurstModel(GatedTextModel):
            async def stream_complete(self, request):
                yield ModelTextDelta("start")
                self.entered.put_nowait(0)
                await self.release[0].wait()
                for _ in range(2005):
                    yield ModelTextDelta("字")
                self.second.put_nowait(0)
                await self.finish[0].wait()
                yield ModelStreamCompleted(self.responses[0])

        full_text = "start" + "字" * 2005
        model = BurstModel([text_response("final", full_text)])
        model.finish[0].clear()
        app = compose_fixture_application(model_adapter=model, tool_adapters=())
        client = EngineeringAgentClient(self.root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        task = await client.submit_task("answer", command_id="burst")
        await asyncio.wait_for(model.entered.get(), 2)
        stream = client.subscribe_task_ui(task.task_id)
        self.addAsyncCleanup(stream.aclose)
        async for packet in stream:
            if packet["type"] == "task.snapshot":
                break
        model.release[0].set()
        await asyncio.wait_for(model.second.get(), 2)
        recovered = await asyncio.wait_for(anext(stream), 2)
        self.assertEqual(recovered["type"], "text.snapshot")
        self.assertEqual(recovered["streams"][0]["text"], full_text)
        self.assertEqual(recovered["streams"][0]["text_sequence"], 2006)
        model.finish[0].set()
        final = [packet async for packet in stream]
        self.assertEqual(final[-1]["assistant_text"], full_text)


class ResponsesTextStreamingTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def final(text):
        return {"type": "response.completed", "response": {
            "id": "response", "status": "completed", "output": [{
                "id": "message", "type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": text}],
            }],
        }}

    async def test_responses_body_deltas_arrive_before_completed_and_keep_phase(self):
        entered, release = asyncio.Event(), asyncio.Event()
        final = self.final("hello world")

        class Transport:
            async def stream_sse(self, *args, **kwargs):
                yield json.dumps({"type": "response.output_item.added", "item": {
                    "id": "message", "type": "message", "phase": "final_answer",
                }})
                yield json.dumps({"type": "response.output_text.delta", "item_id": "message", "delta": "hello"})
                entered.set()
                await release.wait()
                yield json.dumps({"type": "response.output_text.delta", "item_id": "message", "delta": " world"})
                yield json.dumps(final)

        provider = OpenAIResponsesModelProvider("https://example.test/v1", "model", "test-key", transport=Transport())
        await provider.start(AdapterContext({}, lambda *_: None))
        events = []

        async def consume():
            async for event in provider.stream_complete(ModelRequest("turn", ())):
                events.append(event)

        running = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(events, [ModelTextDelta("hello", phase=AssistantMessagePhase.FINAL_ANSWER)])
        self.assertFalse(running.done())
        release.set()
        await asyncio.wait_for(running, 2)
        self.assertEqual([e.text for e in events if isinstance(e, ModelTextDelta)], ["hello", " world"])
        self.assertIsInstance(events[-1], ModelStreamCompleted)

    async def test_responses_internal_conclusion_fragments_never_reach_visible_text(self):
        text = "visible<tsm-conclusion-v1>{broken}</tsm-conclusion-v1>"
        events = [
            {"type": "response.output_text.delta", "delta": "visible<tsm-conclu"},
            {"type": "response.output_text.delta", "delta": "sion-v1>{broken}</tsm-conclusion-v1>"},
            self.final(text),
        ]

        class Transport:
            async def stream_sse(self, *args, **kwargs):
                for event in events:
                    yield json.dumps(event)

        provider = OpenAIResponsesModelProvider("https://example.test/v1", "model", "test-key", transport=Transport())
        await provider.start(AdapterContext({}, lambda *_: None))
        output = [event async for event in provider.stream_complete(ModelRequest("turn", ()))]
        self.assertEqual("".join(e.text for e in output if isinstance(e, ModelTextDelta)), "visible")
        self.assertEqual(output[-1].response.message.text, "visible")
