from __future__ import annotations

import asyncio
import json
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tsm_agt.adapters.model_context_summary import ModelContextSummarizer
from tsm_agt.adapters.model_profiles import ModelProfileProvider
from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
from tsm_agt.adapters.anthropic_messages import AnthropicMessagesModelProvider
from tsm_agt.bootstrap.context_configuration import load_context_configuration, context_window_manager
from tsm_agt.core import ContextWindowManager, PromptTemplate
from tsm_agt.core.agent_loop import AgentTurnCheckpoint
from tsm_agt.core.context import ContextWindowExceeded
from tsm_agt.core.context_window import establish_window
from tsm_agt.core.conversation_loop.compaction import complete_window_compaction
from tsm_agt.core.summary_source import prepare_summary_source
from tsm_agt.ports import (Message, MessageRole, TextBlock, ToolCall, ToolCallBlock, ToolResult,
                           ToolResultBlock, ModelCallPurpose, FinishReason)
from tsm_agt.ports.model import ReasoningBlock
from tsm_agt.ports.context_summary import ContextSummarySettings
from tsm_agt.sdk.ui_events import _turn_progress_item
from tests.test_conversation_loop import ScriptedModel, text_response
from tests.test_context_compaction import tool_pair


def observations(body="large result " * 25000):
    messages = [Message("user", MessageRole.USER, (TextBlock("Keep API compatibility; next run the failed test."),))]
    for number in range(3):
        call = ToolCall(str(number), "core.read_file", {"path": "src/api.py"})
        messages.extend((Message("call-" + str(number), MessageRole.ASSISTANT,
                                 (ReasoningBlock("fixture", "thinking", text="PRIVATE REASONING" * 2000), ToolCallBlock(call))),
                         Message("result-" + str(number), MessageRole.TOOL,
                                 (ToolResultBlock(ToolResult(str(number), True, {"path": "src/api.py", "content": body})),))))
    return tuple(messages)


class SummaryAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_large_history_is_prepared_once_without_reasoning_and_does_not_mutate_source(self):
        source = observations()
        snapshot = json.dumps([m.to_data() for m in source])
        model = ScriptedModel([text_response("summary", "API compatibility remains required; rerun the failed test.")])
        progress = []
        result = await ModelContextSummarizer(model).summarize(source, context_window=128000,
                   model_profile_id=None, task_id="task", on_progress=progress.append)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(result.model_calls, 1)
        request = model.requests[0]
        payload = " ".join(m.text for m in request.messages)
        self.assertNotIn("PRIVATE REASONING", payload)
        self.assertIn("Keep API compatibility", payload)
        self.assertIn("same_observation_as_call_id", payload)
        self.assertIn("sha256", payload)
        self.assertLess(len(payload), 15000)
        self.assertEqual(json.dumps([m.to_data() for m in source]), snapshot)
        self.assertEqual(request.max_output_tokens, 2048)
        self.assertEqual(request.max_provider_attempts, 1)
        self.assertEqual(request.tools, ())
        self.assertFalse(request.allow_tool_calls)
        self.assertIs(request.purpose, ModelCallPurpose.COMPACTION)
        self.assertEqual(result.diagnostics["reasoning_blocks_excluded"], 3)
        self.assertEqual(result.diagnostics["duplicate_observations"], 2)
        self.assertEqual([p["stage"] for p in progress], ["input_prepared", "request_started"])

    async def test_failed_verifications_in_middle_of_large_lists_are_retained(self):
        rows = [{"name": str(i), "ok": True} for i in range(100)]
        rows[55] = {"name": "critical-test", "ok": False, "exit_code": 1, "error_code": "ASSERT_FAILED"}
        call = ToolCall("test", "core.run_command", {"argv": ["pytest"]})
        source = (Message("a", MessageRole.ASSISTANT, (ToolCallBlock(call),)),
                  Message("b", MessageRole.TOOL, (ToolResultBlock(ToolResult("test", True, {"tests": rows})),)))
        data, _ = prepare_summary_source(source)
        self.assertIn("ASSERT_FAILED", json.dumps(data))
        self.assertIn("critical-test", json.dumps(data))

    async def test_unknown_or_tiny_window_never_starts_an_oversized_request(self):
        model = ScriptedModel([])
        with self.assertRaisesRegex(ValueError, "window"):
            await ModelContextSummarizer(model).summarize(observations(), context_window=1024, model_profile_id=None, task_id="task")
        self.assertEqual(model.requests, [])

    async def test_retry_uses_remaining_deadline_without_nested_provider_retries(self):
        class SlowFirst(ScriptedModel):
            async def complete(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    await asyncio.sleep(10)
                return text_response("summary", "Keep compatibility.")
        model = SlowFirst([])
        settings = ContextSummarySettings(timeout_seconds=0.15, request_timeout_seconds=0.09)
        result = await ModelContextSummarizer(model, settings).summarize(observations("small"), context_window=32000,
                                                                     model_profile_id=None, task_id="task")
        self.assertEqual(result.model_calls, 2)
        self.assertLess(model.requests[1].timeout_seconds, model.requests[0].timeout_seconds)
        self.assertEqual([r.max_provider_attempts for r in model.requests], [1, 1])

    async def test_total_deadline_and_user_cancellation_cancel_the_active_await(self):
        cancelled = []
        class Hanging(ScriptedModel):
            async def complete(self, request):
                self.requests.append(request)
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.append(True)
        model = Hanging([])
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await ModelContextSummarizer(model, ContextSummarySettings(timeout_seconds=0.08, request_timeout_seconds=0.05)).summarize(
                observations("small"), context_window=32000, model_profile_id=None, task_id="task")
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(len(cancelled), 2)
        worker = asyncio.create_task(ModelContextSummarizer(model).summarize(observations("small"),
                    context_window=32000, model_profile_id=None, task_id="task"))
        while len(model.requests) < 3:
            await asyncio.sleep(0)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertEqual(len(model.requests), 3)

    async def test_preprocessing_is_included_in_deadline(self):
        model = ScriptedModel([])
        def slow_prepare(*args, **kwargs):
            time.sleep(0.08)
            return [], {}
        started = time.monotonic()
        with patch("tsm_agt.adapters.model_context_summary.prepare_summary_source", side_effect=slow_prepare):
            with self.assertRaises(TimeoutError):
                await ModelContextSummarizer(model, ContextSummarySettings(timeout_seconds=0.02)).summarize(
                    (), context_window=32000, model_profile_id=None, task_id="task")
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(model.requests, [])

    async def test_cancellable_provider_stream_is_closed_on_summary_timeout(self):
        closed = []
        class Streaming(ScriptedModel):
            capabilities = replace(ScriptedModel.capabilities, stream_cancel=True)
            async def stream_complete(self, request):
                self.requests.append(request)
                try:
                    await asyncio.sleep(10)
                    yield None
                finally:
                    closed.append(True)
        model = Streaming([])
        with self.assertRaises(TimeoutError):
            await ModelContextSummarizer(model, ContextSummarySettings(timeout_seconds=0.06, request_timeout_seconds=0.04)).summarize(
                (), context_window=32000, model_profile_id=None, task_id="task")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(len(closed), 2)

    async def test_invalid_or_incomplete_checkpoint_is_not_retried_or_accepted(self):
        for response in (text_response("empty", ""), replace(text_response("partial", "Partial"), finish_reason=FinishReason.LENGTH)):
            model = ScriptedModel([response])
            with self.assertRaises(ValueError):
                await ModelContextSummarizer(model).summarize((), context_window=32000, model_profile_id=None, task_id="task")
            self.assertEqual(len(model.requests), 1)

    def test_lowest_supported_effort_and_auxiliary_output_limits_on_both_protocols(self):
        provider = OpenAICompatibleModelProvider("https://example.invalid/v1", "test", "GPT-6.1-Sol", send_output_token_limit=False)
        routed = ModelProfileProvider({"primary": provider}, "primary", [{"id": "primary", "reasoning_efforts": ["high", "low", "xhigh"]}])
        self.assertEqual(routed.summary_reasoning_effort_for("primary"), "low")
        from tsm_agt.ports import ModelRequest
        request = ModelRequest("summary", (), purpose=ModelCallPurpose.COMPACTION, max_output_tokens=2048, reasoning_effort="low")
        payload = provider._build_payload(request)[0]
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertNotIn("max_tokens", provider._build_payload(replace(request, purpose=ModelCallPurpose.AGENT_TURN))[0])
        anthropic = AnthropicMessagesModelProvider("https://example.invalid", "test", "test")
        payload = anthropic._build_payload(replace(request, reasoning_effort=anthropic.summary_reasoning_effort_for()))[0]
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["max_tokens"], 2048)

    def test_settings_reach_composition_and_reject_invalid_values(self):
        cfg = load_context_configuration({"context": {"summary_timeout_seconds": 150, "summary_request_timeout_seconds": 60,
                     "summary_max_output_tokens": 1024, "summary_model_profile_id": "fast", "summary_failure_cooldown_seconds": 30}})
        settings = context_window_manager(cfg).summary_settings
        self.assertEqual(settings.timeout_seconds, 150)
        self.assertEqual(settings.max_output_tokens, 1024)
        self.assertEqual(settings.model_profile_id, "fast")
        for key, value in [("summary_timeout_seconds", 0), ("summary_request_timeout_seconds", float("inf")),
                           ("summary_max_output_tokens", True), ("summary_model_profile_id", "")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                load_context_configuration({"context": {key: value}})


class SummaryRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_preparation_failure_after_tool_result_is_recorded_as_recoverable(self):
        import tempfile
        from pathlib import Path
        from tsm_agt.bootstrap import compose_fixture_application
        from tsm_agt.core import TaskState
        from tsm_agt.core.conversation_loop.loop import ConversationAgentLoop
        from tests.test_conversation_loop import ScriptedToolProvider, tool_response, _executing
        model = ScriptedModel([tool_response("search", "search", "web.search", {"query": "fixture"})])
        app = compose_fixture_application(model_adapter=model, tool_adapters=(ScriptedToolProvider(),))
        await app.start()
        self.addAsyncCleanup(app.registry.stop_all)
        original = ConversationAgentLoop._prepare_model_call
        async def fail_after_tool(loop, state):
            if state.model_call_count:
                raise ValueError("fixture preparation failed")
            await original(loop, state)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            with patch.object(ConversationAgentLoop, "_prepare_model_call", fail_after_tool):
                with self.assertRaisesRegex(ValueError, "fixture preparation"):
                    await app.kernel.run_agent_turn(task.task_id, "Search once")
            current = await app.kernel.get_task(task.task_id)
            self.assertIs(current.state, TaskState.INTERRUPTED)
            self.assertEqual(current.active_agent_checkpoint["pending_tool_calls"], [])
            events = await app.kernel.dependencies.store.read_events(task.task_id)
            failed = [event for event in events if event.event_type == "turn.interrupted"]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0].payload["failure_kind"], "model_preparation")

    async def setup_runtime(self, summarizer, *, timeout=0.05, hard_overflow=False):
        template = PromptTemplate.default()
        baseline, window = establish_window(template, (), (), "identity")
        source = (*baseline, Message("current", MessageRole.USER, (TextBlock("Continue B"),)),
                  *tool_pair("old", "old observation " * 400), *tool_pair("recent", "recent"))
        manager = ContextWindowManager(trigger_ratio=0.5, recent_message_floor=2,
                    summary_settings=ContextSummarySettings(timeout_seconds=timeout))
        size = manager.estimate_input_tokens(source, (), template)
        prepared = manager.prepare(conversation=source, tools=(), prompt_template=template,
                    context_window=size + (-200 if hard_overflow else 400), max_output_tokens=128,
                    defer_hard_limit_for_summary=True)
        self.assertIsNotNone(prepared.compaction)
        events = []
        async def append(task_id, rows):
            events.extend(rows)
        async def get_task(task_id):
            return SimpleNamespace(tool_executions={})
        kernel = SimpleNamespace(_dependencies=SimpleNamespace(context_manager=manager, context_summarizer=summarizer, prompt_template=template),
                                 _append_events=append, get_task=get_task)
        checkpoint = AgentTurnCheckpoint("task", "turn", 1, source, (), (), 0, 0, 0, 0, 10, 10, 128, 10, context_window_state=window)
        state = SimpleNamespace(messages=list(source), task_id="task", turn_id="turn", visible_tool_by_name={}, checkpoint=checkpoint)
        return kernel, state, prepared, source, events

    async def test_timeout_preserves_history_emits_status_and_cools_down(self):
        calls = []
        class Hanging:
            async def summarize(self, *args, **kwargs):
                calls.append(True)
                await asyncio.sleep(10)
        kernel, state, prepared, source, events = await self.setup_runtime(Hanging())
        result = await complete_window_compaction(kernel, state, prepared, (), (), "identity")
        self.assertEqual(result.canonical_messages, source)
        self.assertIsNone(result.compaction)
        self.assertEqual([kind for kind, _ in events], ["context.summary_started", "context.summary_failed"])
        for kind, payload in events:
            projected = _turn_progress_item(SimpleNamespace(event_type=kind, payload=payload, sequence=1, occurred_at="now", event_id="event"))
            self.assertTrue(projected["activity"])
        await complete_window_compaction(kernel, state, prepared, (), (), "identity")
        self.assertEqual(len(calls), 1)
        self.assertGreater(state.checkpoint.context_window_state["summary_retry_after"], time.time())

    async def test_hard_overflow_does_not_commit_a_missing_or_incomplete_summary(self):
        class Broken:
            async def summarize(self, *args, **kwargs):
                raise OSError("offline")
        kernel, state, prepared, source, events = await self.setup_runtime(Broken(), hard_overflow=True)
        with self.assertRaises(ContextWindowExceeded):
            await complete_window_compaction(kernel, state, prepared, (), (), "identity")
        self.assertEqual(tuple(state.messages), source)
        self.assertEqual(state.checkpoint.messages, source)
        self.assertTrue(events[-1][1]["source_preserved"])

    async def test_local_tool_pruning_can_avoid_a_summary_request(self):
        class MustNotCall:
            async def summarize(self, *args, **kwargs):
                raise AssertionError("local pruning is sufficient")
        kernel, state, prepared, source, events = await self.setup_runtime(MustNotCall(), timeout=1)
        # Make only the old tool output large enough to trigger capacity pressure.
        source = (*source[:-4], *tool_pair("old", "old observation " * 4000), *source[-2:])
        state.messages = list(source)
        state.checkpoint = replace(state.checkpoint, messages=source)
        manager = replace(kernel._dependencies.context_manager, trigger_ratio=0.8)
        kernel._dependencies.context_manager = manager
        prepared = manager.prepare(conversation=source, tools=(), prompt_template=kernel._dependencies.prompt_template,
                    context_window=manager.estimate_input_tokens(source, (), kernel._dependencies.prompt_template) + 100, max_output_tokens=128,
                    defer_hard_limit_for_summary=True)
        result = await complete_window_compaction(kernel, state, prepared, (), (), "identity")
        self.assertTrue(any(kind == "context.tool_outputs_pruned" for kind, _ in events))
        self.assertFalse(any(kind == "context.summary_started" for kind, _ in events))
        self.assertIsNotNone(result.compaction)
        self.assertIn("Continue B", " ".join(m.text for m in result.messages))
