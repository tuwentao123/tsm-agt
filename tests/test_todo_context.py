"""The model must receive the latest durable Todo, not a stale progress snapshot."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ContextWindowManager, PromptTemplate
from tsm_agt.ports import Message, MessageRole, TextBlock
from tests.test_context_compaction import tool_pair
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response


def snapshots(request):
    return [message for message in request.messages
            if message.message_id.startswith("working-memory-context-")]


class TodoContextTest(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(json.loads(before[0].text)["revision"], 2)
        data = json.loads(after[0].text)
        self.assertEqual(data["revision"], 3)
        self.assertEqual(data["plan_state"], {
            "active_item_id": "b", "next_pending_item_id": None, "open_item_ids": ["b"],
        })
        self.assertEqual([step["status"] for step in data["plan"]], ["COMPLETED", "IN_PROGRESS"])

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

            model = ScriptedModel([text_response("continue", "Remaining step identified.")])
            second = compose_fixture_application(
                model_adapter=model, tool_adapters=(), store_adapter=SQLiteRuntimeStore(database),
                enable_working_memory=True,
            )
            await second.registry.start_all()
            try:
                await second.kernel.run_agent_turn(task.task_id, "Continue the implementation")
                latest = snapshots(model.requests[0])
                self.assertEqual(len(latest), 1)
                data = json.loads(latest[0].text)
                self.assertEqual(data["revision"], 2)
                self.assertEqual(data["plan_state"]["next_pending_item_id"], "a")
                self.assertEqual(data["plan"][0]["description"], "Implement integration")
            finally:
                await second.registry.stop_all()
