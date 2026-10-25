"""The model must receive the latest durable Todo, not a stale progress snapshot."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.adapters.builtin.core_tools import CoreReadOnlyToolProvider
from tsm_agt.adapters.rule_based_stop_or_pivot import RuleBasedStopOrPivotPolicy
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ContextWindowManager, PromptTemplate
from tsm_agt.ports import Message, MessageRole, TextBlock, ToolResultBlock
from tests.test_context_compaction import tool_pair
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response


def snapshots(request):
    return [message for message in request.messages
            if message.message_id.startswith("execution-context-")]


class TodoContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_completed_plan_allows_remaining_four_file_read_batch(self):
        from dataclasses import replace
        response = tool_response("reads", "read-0", "core.read_file", {"path": "file-0.txt"})
        response = replace(response, message=replace(response.message, content=tuple(
            tool_response("reads", f"read-{i}", "core.read_file", {"path": f"file-{i}.txt"})
            .message.content[0] for i in range(4)
        )))
        for policy in (None, RuleBasedStopOrPivotPolicy()):
            with self.subTest(policy=type(policy).__name__):
                model = ScriptedModel([
                    tool_response("complete", "complete", "core.todo_list", {
                        "expected_revision": 1, "operation_id": "complete",
                        "todos": [{"id": "old", "content": "Previous work", "status": "completed"}],
                    }),
                    response, text_response("done", "Read all four files."),
                ])
                app = compose_fixture_application(
                    model_adapter=model, tool_adapters=(CoreReadOnlyToolProvider(),),
                    enable_working_memory=True, stop_or_pivot_policy_adapter=policy,
                )
                await app.registry.start_all()
                try:
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        for i in range(4):
                            (root / f"file-{i}.txt").write_text(f"content-{i}\n")
                        task = await _executing(app, root)
                        result = await app.kernel.run_agent_turn(task.task_id, "Complete the work, then inspect all four files")
                        self.assertEqual(result.tool_calls, 5)
                        results = [b.result for m in model.requests[-1].messages for b in m.content
                                   if isinstance(b, ToolResultBlock) and b.result.call_id.startswith("read-")]
                        self.assertEqual(len(results), 4)
                        self.assertTrue(all(r.ok for r in results))
                        events = await app.kernel.dependencies.store.read_events(task.task_id)
                        self.assertFalse(any(e.event_type == "plan.no_progress_stopped" for e in events))
                finally:
                    await app.registry.stop_all()

    async def test_sqlite_restored_closed_plan_allows_read_and_reopen_next_turn(self):
        for status in ("completed", "blocked"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "config.txt").write_text("configuration\n")
                database = root / "runtime.db"
                first = compose_fixture_application(
                    model_adapter=ScriptedModel([
                        tool_response("close", "close", "core.todo_list", {
                            "expected_revision": 1, "operation_id": "close",
                            "todos": [{"id": "old", "content": "Previous work", "status": status}],
                        }), text_response("first-done", "Recorded previous state."),
                    ]), tool_adapters=(), store_adapter=SQLiteRuntimeStore(database),
                    enable_working_memory=True,
                )
                await first.registry.start_all()
                try:
                    task = await _executing(first, root)
                    await first.kernel.run_agent_turn(task.task_id, "Record the previous state")
                finally:
                    await first.registry.stop_all()
                model = ScriptedModel([
                    tool_response("read", "read", "core.read_file", {"path": "config.txt"}),
                    tool_response("reopen", "reopen", "core.todo_list", {
                        "expected_revision": 2, "operation_id": "reopen", "merge": True,
                        "todos": [{"id": "old", "status": "in_progress"}],
                    }), text_response("started", "Follow-up work started."),
                ])
                second = compose_fixture_application(
                    model_adapter=model, tool_adapters=(CoreReadOnlyToolProvider(),),
                    store_adapter=SQLiteRuntimeStore(database), enable_working_memory=True,
                    stop_or_pivot_policy_adapter=RuleBasedStopOrPivotPolicy(),
                )
                await second.registry.start_all()
                try:
                    result = await second.kernel.run_agent_turn(task.task_id, "Inspect configuration and resume the follow-up work")
                    self.assertEqual(result.tool_calls, 2)
                    memory = await second.kernel.get_working_memory(task.task_id)
                    self.assertEqual(memory.revision, 3)
                    self.assertEqual(memory.plan[0].status.value, "IN_PROGRESS")
                    results = [b.result for m in model.requests[-1].messages for b in m.content
                               if isinstance(b, ToolResultBlock) and b.result.call_id in {"read", "reopen"}]
                    self.assertEqual(len(results), 2)
                    self.assertTrue(all(r.ok for r in results))
                finally:
                    await second.registry.stop_all()

    async def test_full_item_identifier_and_latest_write_recovery_reach_model(self):
        identifier = "a" * 120
        model = ScriptedModel([
            tool_response("create", "create", "core.todo_list", {
                "expected_revision": 1, "operation_id": "create",
                "todos": [{"id": identifier, "content": "Implement integration", "status": "pending"}],
            }),
            tool_response("stale", "stale", "core.todo_list", {
                "expected_revision": 1, "operation_id": "stale", "merge": True,
                "todos": [{"id": identifier, "status": "in_progress"}],
            }),
            tool_response("reconcile", "reconcile", "core.todo_list", {
                "expected_revision": 2, "operation_id": "reconcile", "merge": True,
                "todos": [{"id": identifier, "status": "in_progress"}],
            }),
            text_response("stop", "Started; implementation remains open."),
        ])
        app = compose_fixture_application(model_adapter=model, tool_adapters=(), enable_working_memory=True)
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            await app.kernel.run_agent_turn(task.task_id, "Continue the integration")
            memory = await app.kernel.get_working_memory(task.task_id)
        self.assertEqual(len(memory.plan), 1)
        self.assertEqual(memory.plan[0].step_id, identifier)
        self.assertEqual(memory.plan[0].status.value, "IN_PROGRESS")
        for request in model.requests[1:]:
            data = json.loads(snapshots(request)[0].text)["plan_record"]
            self.assertEqual(data["items"][0]["step_id"], identifier)
            runtime = [m.text for m in request.messages if m.message_id.startswith("prompt-runtime-")]
            self.assertEqual(len(runtime), 1)
            self.assertEqual(runtime[0].count("The current execution state includes a plan."), 1)
        failed_note = next(m.text for m in model.requests[2].messages if m.message_id.startswith("prompt-runtime-"))
        recovered_note = next(m.text for m in model.requests[3].messages if m.message_id.startswith("prompt-runtime-"))
        self.assertIn("Inspect the latest plan write outcome", failed_note)
        self.assertNotIn("Inspect the latest plan write outcome", recovered_note)

    async def test_updated_plan_replaces_snapshot_and_survives_compaction(self):
        model = ScriptedModel([
            tool_response("create", "create", "core.todo_list", {
                "expected_revision": 1, "operation_id": "create",
                "todos": [
                    {"id": "a", "content": "Inspect configuration", "status": "in_progress"},
                    {"id": "b", "content": "Implement integration", "status": "pending"},
                ],
            }),
            tool_response("advance", "advance", "core.todo_list", {
                "expected_revision": 2, "operation_id": "advance", "merge": True,
                "todos": [
                    {"id": "a", "status": "completed"},
                    {"id": "b", "status": "in_progress"},
                ],
            }),
            text_response("stop", "Implementation remains open."),
        ])
        app = compose_fixture_application(
            model_adapter=model, tool_adapters=(), enable_working_memory=True,
        )
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            await app.kernel.run_agent_turn(task.task_id, "Implement both steps using Todo")
        self.assertEqual(len(model.requests), 3)  # No new completion gate.
        before = snapshots(model.requests[1])
        after = snapshots(model.requests[2])
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 1)
        self.assertEqual(json.loads(before[0].text)["plan_record"]["revision"], 2)
        data = json.loads(after[0].text)["plan_record"]
        self.assertEqual(data["revision"], 3)
        self.assertEqual(data["open_count"], 1)
        self.assertEqual(data["active_item_id"], "b")
        self.assertEqual(data["maintenance_this_turn"]["successful_write_count"], 2)
        self.assertTrue(data["maintenance_this_turn"]["updated_this_turn"])
        self.assertEqual([step["status"] for step in data["items"]], ["IN_PROGRESS", "COMPLETED"])
        self.assertEqual(model.requests[2].messages[-1], after[0])
        self.assertIs(after[0].role, MessageRole.USER)
        head = next(m for m in model.requests[2].messages if m.message_id.startswith("working-memory-context-"))
        self.assertNotIn("plan", json.loads(head.text))

        template = PromptTemplate.default()
        static_tokens = ContextWindowManager.estimate_input_tokens((), (), template)
        current = Message("current", MessageRole.USER, (TextBlock("Continue implementation"),))
        prepared = ContextWindowManager(trigger_ratio=0.50, recent_message_floor=1).prepare(
            conversation=(
                current, after[0], *tool_pair("old", "old observation " * 200),
                Message("bridge", MessageRole.ASSISTANT, (TextBlock("Continue checking"),)),
                *tool_pair("recent", "recent observation"),
            ),
            tools=(), prompt_template=template, context_window=static_tokens + 1500,
            max_output_tokens=128,
        )
        self.assertIsNotNone(prepared.compaction)
        self.assertIn(after[0], prepared.messages)

    async def test_new_application_restores_latest_plan_into_next_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = compose_fixture_application(
                model_adapter=ScriptedModel([
                    tool_response("create", "create", "core.todo_list", {
                        "expected_revision": 1, "operation_id": "create",
                        "todos": [{"id": "a", "content": "Implement integration", "status": "pending"}],
                    }),
                    text_response("plan", "Plan recorded."),
                ]), tool_adapters=(), store_adapter=SQLiteRuntimeStore(database),
                enable_working_memory=True,
            )
            await first.registry.start_all()
            try:
                task = await _executing(first, root)
                await first.kernel.run_agent_turn(task.task_id, "List an implementation plan only")
            finally:
                await first.registry.stop_all()

            model = ScriptedModel([
                tool_response("start", "start", "core.todo_list", {
                    "expected_revision": 2, "operation_id": "start", "merge": True,
                    "todos": [{"id": "a", "status": "in_progress"}],
                }),
                text_response("continue", "Remaining step started."),
            ])
            second = compose_fixture_application(
                model_adapter=model, tool_adapters=(), store_adapter=SQLiteRuntimeStore(database),
                enable_working_memory=True,
            )
            await second.registry.start_all()
            try:
                await second.kernel.run_agent_turn(task.task_id, "Continue the implementation")
                latest = snapshots(model.requests[0])
                self.assertEqual(len(latest), 1)
                data = json.loads(latest[0].text)["plan_record"]
                self.assertEqual(data["revision"], 2)
                self.assertEqual(data["open_count"], 1)
                self.assertEqual(data["maintenance_this_turn"]["write_attempt_count"], 0)
                self.assertFalse(data["maintenance_this_turn"]["updated_this_turn"])
                self.assertEqual(data["items"][0]["description"], "Implement integration")
                reminder = [m.text for m in model.requests[0].messages
                            if m.message_id.startswith("prompt-runtime-")]
                self.assertEqual(len(reminder), 1)
                self.assertIn("synchronize observed completion", reminder[0])
                after = json.loads(snapshots(model.requests[1])[0].text)["plan_record"]
                self.assertEqual(after["revision"], 3)
                self.assertEqual(after["maintenance_this_turn"]["last_write"]["status"], "succeeded")
                self.assertEqual(after["items"][0]["status"], "IN_PROGRESS")
            finally:
                await second.registry.stop_all()
