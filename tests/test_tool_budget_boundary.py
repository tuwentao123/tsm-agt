"""Tool-budget exhaustion must end the UI turn, including approval resumes."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import AgentTurnIncomplete, AgentTurnResult, AgentTurnSuspended, ApprovalDecision
from tsm_agt.ports import RuntimeStorePort, ToolCall, ToolCallBlock
from tsm_agt.sdk import EngineeringAgentClient
from tsm_agt.sdk.ui_events import ui_snapshot
from tests.test_conversation_loop import ScriptedModel, ScriptedToolProvider, _executing, text_response, tool_response
from tests.test_sdk_runtime import ApprovalTool


def batch(message_id, *call_ids):
    response = tool_response(message_id, call_ids[0], "web.search", {"query": call_ids[0]})
    return replace(response, message=replace(response.message, content=tuple(
        ToolCallBlock(ToolCall(call_id, "web.search", {"query": call_id})) for call_id in call_ids
    )))


class ToolBudgetBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_batch_is_not_partially_dispatched_and_is_durably_incomplete(self):
        for initial in (False, True):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                responses = ([] if initial else [batch("first", "accepted")]) + [batch("overflow", "over-1", "over-2")]
                provider, model = ScriptedToolProvider(), ScriptedModel(responses)
                app = compose_fixture_application(model_adapter=model, tool_adapters=(provider,),
                    store_adapter=SQLiteRuntimeStore(root / "runtime.db"))
                async with EngineeringAgentClient(root, application_factory=lambda: app) as client:
                    task = await _executing(app, root)
                    outcome = await app.kernel.run_agent_turn(task.task_id, "inspect", max_tool_calls=1 if initial else 2)
                    self.assertIsInstance(outcome, AgentTurnIncomplete)
                    self.assertEqual(outcome.reason, "tool_call_budget_exhausted")
                    self.assertEqual(len(provider.calls), 0 if initial else 1)
                    snapshot = await ui_snapshot(client, task.task_id)
                    self.assertEqual(snapshot["status"], "incomplete")
                    self.assertTrue(snapshot["final"])
                    self.assertIn("超额批次未执行", snapshot["failure_reason"])
                    checkpoint = (await app.kernel.get_task(task.task_id)).active_agent_checkpoint
                    self.assertEqual(checkpoint["pending_tool_calls"], [])
                    self.assertEqual(checkpoint["model_calls"], len(model.requests))
                    serialized_ids = {message["message_id"] for message in checkpoint["messages"]}
                    self.assertNotIn("overflow", serialized_ids)
                    events = await app.registry.require(RuntimeStorePort).read_events(task.task_id)
                    boundary = next(event.payload for event in events if event.event_type == "turn.incomplete_boundary")
                    self.assertEqual(boundary["requested_tool_calls"], 2)
                    self.assertEqual(boundary["remaining_tool_calls"], 1)
                    self.assertFalse(any(event.event_type == "turn.failed" for event in events))

    async def test_exact_fit_still_allows_the_model_to_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedToolProvider()
            app = compose_fixture_application(tool_adapters=(provider,), model_adapter=ScriptedModel([
                batch("fits", "one", "two"), text_response("done", "Inspection complete."),
            ]))
            await app.start()
            try:
                task = await _executing(app, Path(directory))
                result = await app.kernel.run_agent_turn(task.task_id, "inspect", max_tool_calls=2)
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(result.assistant_message.text, "Inspection complete.")
                self.assertEqual(len(provider.calls), 2)
            finally:
                await app.registry.stop_all()

    async def test_approval_resume_finishes_command_and_sse_at_budget_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = ScriptedToolProvider()
            app = compose_fixture_application(tool_adapters=(ApprovalTool(), provider),
                store_adapter=SQLiteRuntimeStore(root / "runtime.db"), model_adapter=ScriptedModel([
                    tool_response("approve", "approved", "fixture.sdk_approval", {}),
                    batch("overflow", "over-1", "over-2"),
                ]))
            async with EngineeringAgentClient(root, application_factory=lambda: app) as client:
                task = await _executing(app, root)
                waiting = await app.kernel.run_agent_turn(task.task_id, "inspect", max_tool_calls=2)
                self.assertIsInstance(waiting, AgentTurnSuspended)
                result = await client.resolve_approval(waiting.approval_request_id,
                    ApprovalDecision.APPROVE, "approve fixture", command_id="approve")
                self.assertEqual(result.result["status"], "incomplete")
                self.assertEqual(provider.calls, [])
                packets = [packet async for packet in client.subscribe_task_ui(task.task_id)]
                self.assertTrue(packets[-1]["final"])
                self.assertIn("已使用 1/2 次", packets[-1]["failure_reason"])
                interrupted = [packet for packet in packets if packet.get("progress", {}).get("kind") == "turn_interrupted"]
                self.assertTrue(interrupted)
                replay = await client.resolve_approval(waiting.approval_request_id,
                    ApprovalDecision.APPROVE, "approve fixture", command_id="approve")
                self.assertTrue(replay.replayed)
                self.assertEqual(replay.result["status"], "incomplete")
