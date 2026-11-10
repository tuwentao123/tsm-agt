"""Request-level invariants across tools, user turns, restart and compaction."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tsm_agt.adapters.anthropic_messages import AnthropicMessagesModelProvider
from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
from tsm_agt.adapters.model_context_summary import ModelContextSummarizer
from tsm_agt.adapters.request_prefix import observe_request_prefix
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ContextWindowManager, PromptTemplate
from tsm_agt.core.context_window import establish_window, window_identity
from tsm_agt.core.conversation_loop.compaction import complete_window_compaction
from tsm_agt.core.conversation_loop.loop import select_model_tools
from tsm_agt.core.mcp_call import mcp_call_spec
from tsm_agt.ports.context_summary import ContextSummary
from tsm_agt.ports import Message, MessageRole, SessionUnitOfWork, TextBlock, ToolResult, ToolRisk, ToolSpec, ModelCallPurpose, ModelUsage
from tests.test_conversation_loop import ScriptedModel, ScriptedToolProvider, _executing, text_response, tool_response
from tests.test_context_compaction import tool_pair


def history_message(message_id: str, body: dict) -> Message:
    return Message(message_id, MessageRole.USER, (TextBlock(json.dumps(body)),))


def historical_read(reference: str) -> Message:
    return history_message(f"session-facts-{reference}", {
        "boundary": "historical_runtime_facts",
        "summary": {"execution_events": [{
            "result": {"observation": {"read_payload_ref": reference}},
        }]},
    })


class WindowRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_committed_compaction_survives_interruption_before_final_answer(self):
        class LargeTool(ScriptedToolProvider):
            async def invoke(self, call, context):
                self.calls.append(call.call_id)
                return ToolResult(call.call_id, True, data={
                    "observation": "large history " * 3500 if call.call_id == "large" else "recent result",
                })

        class InterruptibleModel(ScriptedModel):
            async def complete(self, request):
                if request.context_window_id.endswith(":2"):
                    compacted.set()
                    await asyncio.Future()
                return await super().complete(request)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            compacted = asyncio.Event()
            model = InterruptibleModel([
                tool_response("read-large", "large", "web.search", {"query": "old"}),
                tool_response("read-recent", "recent", "web.search", {"query": "recent"}),
            ])
            manager = ContextWindowManager(trigger_ratio=0.35, recent_message_floor=2)
            first = compose_fixture_application(
                model_adapter=model, tool_adapters=(LargeTool(),),
                store_adapter=SQLiteRuntimeStore(database), context_manager=manager,
            )
            await first.start()
            try:
                task = await _executing(first, root)
                running = asyncio.create_task(first.kernel.run_agent_turn(
                    task.task_id, "Keep the first constraint", max_output_tokens=128,
                ))
                try:
                    done, _ = await asyncio.wait(
                        {running, entered := asyncio.create_task(compacted.wait())},
                        return_when=asyncio.FIRST_COMPLETED, timeout=5,
                    )
                    if running in done:
                        self.fail(f"Turn ended before reaching its compacted request: {running.result()}")
                    self.assertIn(entered, done)
                    session = await first.kernel.get_session(task.session_id)
                    saved = session.model_context_window
                    self.assertEqual(saved["epoch"], 2)
                    self.assertTrue(saved["task_event_sequence"])
                    await first.kernel.interrupt_agent_turn(task.task_id, "fixture interruption")
                finally:
                    entered.cancel()
                    running.cancel()
                    await asyncio.gather(running, entered, return_exceptions=True)
            finally:
                await first.registry.stop_all()

            model = ScriptedModel([
                text_response("resumed", "Kept both constraints."),
                text_response("changed", "Using the earlier constraints."),
            ])
            tools = LargeTool()
            second = compose_fixture_application(
                model_adapter=model, tool_adapters=(tools,),
                store_adapter=SQLiteRuntimeStore(database), context_manager=manager,
            )
            await second.start()
            try:
                await second.kernel.start_fresh_conversation_turn(task.task_id)
                with patch.object(second.kernel, "_session_context_message", wraps=second.kernel._session_context_message) as project:
                    await second.kernel.run_agent_turn(task.task_id, "Add a second constraint", max_output_tokens=128)
                    project.assert_not_awaited()
                request = model.requests[0]
                texts = [message.text for message in request.messages if message.role is MessageRole.USER]
                self.assertEqual(texts.count("Keep the first constraint"), 1)
                self.assertEqual(texts.count("Add a second constraint"), 1)
                self.assertEqual(request.context_window_id, f'{saved["identity"]}:2')
                await second.kernel.update_session_reasoning_effort(task.session_id, "high")
                with patch.object(second.kernel, "_session_context_message", wraps=second.kernel._session_context_message) as project:
                    await second.kernel.run_agent_turn(task.task_id, "Apply the constraints", max_output_tokens=128)
                    project.assert_not_awaited()
                changed = model.requests[1]
                self.assertNotEqual(changed.context_window_id, request.context_window_id)
                self.assertEqual([message.text for message in changed.messages].count("Keep the first constraint"), 1)
                self.assertEqual([message.text for message in changed.messages].count("Kept both constraints."), 1)
                self.assertFalse(any(message.role is MessageRole.TOOL for message in changed.messages))
                self.assertEqual(tools.calls, [])
            finally:
                await second.registry.stop_all()

    async def test_both_wire_protocols_keep_prior_messages_through_tool_and_next_user(self):
        model = ScriptedModel([
            tool_response("search", "search", "web.search", {"query": "fixture"}),
            text_response("answer", "Observed result."), text_response("followup", "Follow-up answer."),
        ])
        app = compose_fixture_application(model_adapter=model, tool_adapters=(ScriptedToolProvider(),))
        await app.start()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            with patch.object(app.kernel, "_session_context_message", wraps=app.kernel._session_context_message) as project:
                await app.kernel.run_agent_turn(task.task_id, "Search once")
                await app.kernel.run_agent_turn(task.task_id, "Explain the result")
                self.assertEqual(project.await_count, 1)
        providers = (
            OpenAICompatibleModelProvider("https://example.invalid/v1", "test", "test"),
            AnthropicMessagesModelProvider("https://example.invalid", "test", "test"),
        )
        for provider in providers:
            with self.subTest(protocol=type(provider).__name__):
                payloads = [provider._build_payload(request)[0] for request in model.requests]
                for before, after in zip(payloads, payloads[1:]):
                    self.assertEqual(before.get("system"), after.get("system"))
                    self.assertEqual(before.get("tools"), after.get("tools"))
                    self.assertEqual(before["messages"], after["messages"][:len(before["messages"])])

    async def test_restart_restores_window_and_records_latest_user_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first_model = ScriptedModel([text_response("first-answer", "Keep the original choice.")])
            first = compose_fixture_application(model_adapter=first_model, store_adapter=SQLiteRuntimeStore(database), tool_adapters=())
            await first.start()
            try:
                task = await _executing(first, root)
                await first.kernel.run_agent_turn(task.task_id, "Choose A")
                saved = (await first.kernel.get_session(task.session_id)).model_context_window
                self.assertTrue(saved["identity"])
                # A legacy compacted window still carries bodies for facts it dropped.
                fact = historical_read("referenced")
                payloads = {"referenced": {"content": "historical read"},
                            "embedded": {"entries": ["observed directory entry"]},
                            "orphan": {"matches": ["old search output " * 5000]}}
                metadata = history_message("session-context-legacy", {
                    "boundary": "session_conversation_projection",
                    "history_format": "native", "history_budget": "turn-token-v2",
                    "unattributed_execution_events": [{
                        "result": {"observation": {"read_payload_ref": "embedded"}},
                    }],
                    "recent_messages": [], "read_payloads": payloads,
                })
                store = first.kernel.dependencies.store
                stored = await store.load_session(task.session_id)
                self.assertIsNotNone(stored)
                await store.commit_session(SessionUnitOfWork(task.session_id, stored.version, {
                    **stored.data, "model_context_window": {
                        **saved, "messages": [*saved["messages"], fact.to_data(), metadata.to_data()],
                    },
                }, ()))
            finally:
                await first.registry.stop_all()
            model = ScriptedModel([text_response("second-answer", "A remains selected.")])
            second = compose_fixture_application(model_adapter=model, store_adapter=SQLiteRuntimeStore(database), tool_adapters=())
            await second.start()
            try:
                await second.kernel.run_agent_turn(task.task_id, "Continue with A")
                request = model.requests[0]
                self.assertEqual([m.text for m in request.messages].count("Choose A"), 1)
                self.assertEqual([m.text for m in request.messages].count("Continue with A"), 1)
                self.assertIn(fact, request.messages)
                restored = next(m for m in request.messages if m.message_id == metadata.message_id)
                self.assertEqual(json.loads(restored.text)["read_payloads"], {
                    key: payloads[key] for key in ("referenced", "embedded")
                })
                events = await second.kernel.dependencies.store.read_events(task.task_id)
                self.assertFalse(any(e.event_type in {"context.summary_started", "context.compacted"} for e in events))
                history = await second.kernel.get_session_conversation(task.session_id)
                self.assertEqual([m.text for m in history.messages if m.role is MessageRole.USER], ["Choose A", "Continue with A"])
            finally:
                await second.registry.stop_all()

    def test_two_hundred_native_tools_do_not_expand_model_tool_definitions(self):
        native = tuple(ToolSpec(f"mcp.demo.tool_{i}", "Purpose", {"type": "object", "properties": {"value": {"type": "string"}}}, ToolRisk.R1)
                       for i in range(200))
        exposed = select_model_tools((mcp_call_spec(), *native), user_text="query", model_call=1)
        self.assertEqual([tool.name for tool in exposed], ["core.mcp_call"])
        self.assertLess(len(json.dumps([tool.to_data() for tool in exposed])),
                        len(json.dumps([tool.to_data() for tool in native])) / 10)

    async def test_semantic_summary_is_tool_free_and_batches_small_messages(self):
        model = ScriptedModel([text_response("handoff", "Decision A; verification failed; implement B next.")])
        adapter = ModelContextSummarizer(model)
        messages = tuple(Message(str(i), MessageRole.ASSISTANT, (TextBlock("Decision fragment " + str(i)),)) for i in range(20))
        summary = await adapter.summarize(messages, context_window=32000, model_profile_id=None, task_id="task")
        self.assertIn("implement B", summary.text)
        self.assertEqual(summary.model_calls, 1)
        self.assertEqual(len(model.requests), 1)
        request = model.requests[0]
        self.assertIs(request.purpose, ModelCallPurpose.COMPACTION)
        self.assertFalse(request.allow_tool_calls)
        self.assertEqual(request.tools, ())

    async def test_summary_failure_keeps_original_and_success_rebuilds_window(self):
        template = PromptTemplate.default()
        baseline, window = establish_window(template, (), (), "identity")
        metadata = history_message("session-context-previous", {
            "boundary": "session_conversation_projection", "recent_messages": [],
            "history_format": "native", "history_budget": "turn-token-v2",
            "read_payloads": {"old": {"content": "old read"}, "recent": {"content": "recent read"}},
        })
        old_fact, recent_fact = historical_read("old"), historical_read("recent")
        source = (*baseline, metadata, old_fact, Message("current", MessageRole.USER, (TextBlock("Continue B"),)),
                  *tool_pair("old", "observation " * 500),
                  Message("decision", MessageRole.ASSISTANT, (TextBlock("Use A because of the constraint."),)),
                  *tool_pair("recent", "recent observation"), recent_fact)
        manager = ContextWindowManager(trigger_ratio=0.60, recent_message_floor=2)
        size = manager.estimate_input_tokens(source, (), template)
        prepared = manager.prepare(conversation=source, tools=(), prompt_template=template,
                                   context_window=size + 400, max_output_tokens=128)
        self.assertIsNotNone(prepared.compaction)
        class Summarizer:
            fail = True
            async def summarize(self, messages, **kwargs):
                if self.fail:
                    raise OSError("summary failed")
                return ContextSummary("Use A; B remains unfinished.", ModelUsage(13, 7), 1)
        summarizer = Summarizer()
        events = []
        async def append_events(task_id, rows):
            events.extend(rows)
        async def get_task(task_id):
            return SimpleNamespace(tool_executions={})
        kernel = SimpleNamespace(_dependencies=SimpleNamespace(
            context_summarizer=summarizer, context_manager=manager, prompt_template=template),
            _append_events=append_events, get_task=get_task)
        from tsm_agt.core.agent_loop import AgentTurnCheckpoint
        state = SimpleNamespace(messages=list(source), task_id="task", turn_id="turn", visible_tool_by_name={},
                                checkpoint=AgentTurnCheckpoint("task", "turn", 1, source, (), (), 0, 0, 0, 0, 10, 10, 128, 10,
                                                               context_window_state=window))
        failed = await complete_window_compaction(kernel, state, prepared, (), (), "identity")
        self.assertEqual(failed.canonical_messages, source)
        self.assertIsNone(failed.compaction)
        self.assertEqual(events[0][0], "context.summary_started")
        self.assertTrue(any(kind == "context.summary_failed" for kind, _ in events))
        summarizer.fail = False
        # A real checkpoint supports replace; use one from a fixture run.
        from tsm_agt.core.agent_loop import AgentTurnCheckpoint
        state.checkpoint = AgentTurnCheckpoint("task", "turn", 1, source, (), (), 0, 0, 0, 0, 10, 10, 128, 10,
                                               context_window_state=window)
        memory = Message("working-memory-context-current", MessageRole.USER, (TextBlock(json.dumps({
            "task_id": "task", "revision": 7, "plan": [
                {"step_id": "next", "status": "IN_PROGRESS", "description": "Implement B"},
            ],
        })),))
        compacted = await complete_window_compaction(kernel, state, prepared, (), (memory,), "identity")
        self.assertEqual(state.checkpoint.context_window_state["epoch"], 2)
        self.assertTrue(any("Use A; B remains unfinished." in message.text for message in compacted.messages))
        self.assertTrue(any(message.message_id == "tool-recent" for message in compacted.messages))
        self.assertIn(recent_fact, compacted.canonical_messages)
        self.assertNotIn(old_fact, compacted.canonical_messages)
        retained = next(m for m in compacted.canonical_messages if m.message_id == metadata.message_id)
        self.assertEqual(json.loads(retained.text)["read_payloads"], {"recent": {"content": "recent read"}})
        progress = next(json.loads(message.text) for message in compacted.messages
                        if message.message_id.startswith("execution-context-"))
        self.assertEqual(progress["plan_record"]["revision"], 7)
        self.assertEqual(progress["plan_record"]["active_item_id"], "next")
        self.assertEqual(state.input_tokens, 13)
        self.assertEqual(state.checkpoint.output_tokens, 7)

    async def test_wire_observation_contains_hashes_and_counts_only(self):
        events = []
        request = SimpleNamespace(context_window_id="window", on_transport_progress=events.append)
        provider = SimpleNamespace()
        payload = {"model": "test", "tools": [], "messages": [{"role": "user", "content": "private-value"}]}
        await observe_request_prefix(provider, request, payload)
        await observe_request_prefix(provider, request, {**payload, "messages": [*payload["messages"], {"role": "assistant", "content": "done"}]})
        data = events[-1].diagnostics
        self.assertEqual(data["unchanged_leading_messages"], 1)
        self.assertTrue(data["prefix_settings_unchanged"])
        self.assertNotIn("private-value", json.dumps(data))
