"""Conversation containers carry runtime state, not a permanent work contract."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState
from tsm_agt.core.runtime_input import SessionResumeCandidate, SessionResumeSafety
from tsm_agt.core.session import SessionSnapshot
from tsm_agt.core.session_context import SessionActiveCheckpoint, SessionContextProjector
from tsm_agt.core.task_spec import TaskAcceptanceCriterion, TaskCriterionKind
from tsm_agt.ports import CompletionReviewMode, ToolResultBlock
from tests.test_conversation_loop import (
    ScriptedCompletionReviewer, ScriptedModel, _executing, text_response, tool_response,
)
from tests import test_session_context as session_context_fixtures


_CONTRACT_FIELDS = {
    "goal", "completed_work", "remaining_work", "verification_status",
    "task_spec", "task_spec_revision", "continuation_mode", "outcomes", "conclusion",
}


class ConversationRuntimeProjectionTest(unittest.TestCase):
    def test_legacy_contract_is_archived_but_not_a_model_context_contract(self):
        event = session_context_fixtures.SessionContextProjectorTest._event(
            1, "Inspect the service", "I can implement A and B.",
        )
        snapshot = SessionSnapshot.create("session-1", "uid:1", "chat").bump_context()
        projector = SessionContextProjector()
        projection = projector.project(snapshot, (event,))
        stored_before = projection.to_data()
        active = SessionActiveCheckpoint(
            task_id="task-1", turn_id="turn-1", task_state="AWAITING_APPROVAL",
            goal="old fixed goal", checkpoint_revision=1, checkpoint_hash="hash",
            continuation="await_explicit_approval", model_calls=1, max_model_calls=5,
            tool_calls=1, max_tool_calls=5, input_tokens=1, output_tokens=1,
            pending_tools=("core.apply_patch",), remaining_work=("old obligation",),
        )
        suspended = SessionResumeCandidate(
            "task-1", "old fixed goal", "AWAITING_APPROVAL", "/workspace",
            SessionResumeSafety.AWAIT_USER_ACTION, "approval_pending",
        )
        prompt = projector.for_prompt(
            projection, conversation_mode=True, active_checkpoint=active,
            suspended_tasks=(suspended,),
        )
        body = prompt.prompt_data
        for item in (body["work_state"], *body["task_index"],
                     body["active_checkpoint"], *body["suspended_tasks"]["items"]):
            self.assertTrue(_CONTRACT_FIELDS.isdisjoint(item), item)
        self.assertEqual(body["active_checkpoint"]["runtime_state"], "AWAITING_APPROVAL")
        self.assertEqual(body["active_checkpoint"]["pending_tools"], ["core.apply_patch"])
        self.assertEqual(body["recent_messages"][-1]["text"], "I can implement A and B.")
        self.assertEqual(projection.to_data(), stored_before)
        # Legacy APIs/UI keep their durable representation and hash.
        legacy = projector.for_prompt(projection).prompt_data
        self.assertEqual(legacy["work_state"]["goal"], "Inspect the service")
        self.assertIn("completed_work", legacy["recent_task_summaries"][0])

    def test_conversation_handoff_has_runtime_facts_without_acceptance_status(self):
        event = session_context_fixtures.SessionContextProjectorTest._event(1, "inspect", "proposal")
        projector = SessionContextProjector()
        projection = projector.project(
            SessionSnapshot.create("session-1", "uid:1", "chat").bump_context(),
            (event,),
        )
        body = projector.for_prompt(projection, conversation_mode=True).prompt_data
        summary = body["recent_task_summaries"][0]
        self.assertTrue(_CONTRACT_FIELDS.isdisjoint(summary))
        self.assertEqual(summary["runtime_state"], "EXECUTING")
        self.assertEqual(summary["tool_counts"], {"core.search_text": 1})


class ConversationRuntimeSemanticsTest(unittest.IsolatedAsyncioTestCase):
    async def test_todo_updates_without_reading_or_resubmitting_a_fixed_goal(self):
        model = ScriptedModel([
            tool_response("read", "todo-read", "core.todo_list", {}),
            tool_response("write", "todo-write", "core.todo_list", {
                "expected_revision": 1, "operation_id": "plan",
                "todos": [
                    {"id": "a", "content": "Inspect", "status": "in_progress"},
                    {"id": "b", "content": "Implement", "status": "pending"},
                ],
            }),
            text_response("done", "Plan recorded."),
        ])
        app = compose_fixture_application(
            model_adapter=model, tool_adapters=(), enable_working_memory=True,
        )
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            await app.kernel.run_agent_turn(task.task_id, "Record the plan")
            results = {
                block.result.call_id: block.result
                for message in model.requests[-1].messages for block in message.content
                if isinstance(block, ToolResultBlock)
            }
            self.assertTrue(results["todo-write"].ok, results["todo-write"].message)
            memory = await app.kernel.get_working_memory(task.task_id)
            self.assertEqual([step.step_id for step in memory.plan], ["a", "b"])
            self.assertEqual(memory.goal, task.goal)  # storage compatibility only

    async def test_old_spec_cannot_gate_followup_or_reenter_the_model_request(self):
        model = ScriptedModel([
            tool_response("read", "memory", "core.working_memory_read", {}),
            text_response("proposal", "I can implement A and B."),
            text_response("answer", "The current request was handled."),
        ])
        reviewer = ScriptedCompletionReviewer([])
        app = compose_fixture_application(
            model_adapter=model, tool_adapters=(), enable_working_memory=True,
            completion_reviewer_adapter=reviewer,
            completion_review_mode=CompletionReviewMode.REQUIRED,
        )
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            created = (await app.kernel._dependencies.store.read_events(task.task_id))[0]
            self.assertNotIn("task_spec", created.payload)
            # Simulate a pre-migration contract without deleting the audit data.
            await app.kernel.revise_task_spec(
                task.task_id, 1, scope=(), constraints=(),
                acceptance_criteria=(TaskAcceptanceCriterion(
                    "legacy-check", "Inspect the workspace", TaskCriterionKind.WORKSPACE_INTEGRITY,
                ),), operation_id="legacy-spec", writer="test",
            )
            # Updating a scratchpad no longer requires repeating a fixed goal.
            await app.kernel.update_working_memory(
                task.task_id, 1, {
                    "constraints": [], "facts": [], "decisions": [],
                    "hypotheses": [], "open_questions": [], "plan": [],
                    "completed_work": ["past progress"], "remaining_work": [], "evidence": [],
                }, "progress", "test",
            )
            await app.kernel.run_agent_turn(task.task_id, "What changes are needed?")
            await app.kernel.run_agent_turn(task.task_id, "Implement both changes.")
            self.assertEqual(reviewer.candidates, [])
            self.assertEqual((await app.kernel.get_task(task.task_id)).state, TaskState.AWAITING_INPUT)
            for request in model.requests:
                names = {tool.name for tool in request.tools}
                self.assertNotIn("core.task_spec_read", names)
                self.assertNotIn("core.task_spec_update", names)
                self.assertIn("core.todo_list", names)
                self.assertIn("core.working_memory_update", names)
                for message in request.messages:
                    if message.message_id.startswith("session-context-"):
                        data = json.loads(message.text)
                        self.assertTrue(_CONTRACT_FIELDS.isdisjoint(data["work_state"]))
                        for summary in data["recent_task_summaries"]:
                            self.assertTrue(_CONTRACT_FIELDS.isdisjoint(summary))
                    for block in message.content:
                        if isinstance(block, ToolResultBlock) and block.result.call_id == "memory":
                            self.assertNotIn("goal", block.result.data)
            from tests.history_helpers import session_history_view
            history = session_history_view(model.requests[-1])
            self.assertTrue(any(
                row["text"] == "I can implement A and B." for row in history["recent_messages"]
            ))
            self.assertEqual((await app.kernel.get_task_spec(task.task_id)).revision, 2)

