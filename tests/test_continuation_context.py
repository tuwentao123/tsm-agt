"""Historical state provenance must survive rendering without becoming intent."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ContextWindowManager, PromptTemplate, SessionContextProjector, SessionSnapshot
from tsm_agt.core.configuration import canonical_hash
from tsm_agt.core.session_context import working_goal_source
from tsm_agt.ports import Message, MessageRole, SessionEvent, TextBlock
from tests.test_conversation_loop import ScriptedModel, _executing, text_response
from tests.test_turn_history import result_event


class ContinuationContextTest(unittest.TestCase):
    def project(self, events):
        projector = SessionContextProjector()
        snapshot = SessionSnapshot.create("session-1", "uid:1", "chat")
        projection = projector.project(snapshot, events)
        prompt = projector.for_prompt(
            projection, conversation_mode=True,
            goal_source=working_goal_source(events, projection.working_state.goal),
        )
        return projection, prompt

    def recorded(self, sequence, turn, user, *, revision=1):
        event = result_event(sequence, turn, user, "I will implement the remaining changes.")
        return replace(event, payload={
            **event.payload,
            "working_state": {"goal": "Earlier configuration request", "remaining_work": []},
            "working_memory_revision": revision,
            "working_memory_hash": f"memory-{revision}",
        })

    def test_repeated_copy_keeps_memory_identity_and_does_not_imply_completion(self):
        first = self.recorded(1, "turn-1", "Earlier configuration request")
        second = self.recorded(2, "turn-2", "Implement the skill system")
        projection, prompt = self.project((first, second))
        state = prompt.prompt_data["work_state"]
        source = state["recorded_goal"]["source"]

        self.assertEqual(source["memory_revision"], 1)
        self.assertEqual(source["memory_hash"], "memory-1")
        self.assertEqual(source["recorded_in_turn_id"], "turn-2")
        self.assertNotIn("updated_in_turn_id", source)
        self.assertEqual(state["recorded_goal"]["text"], "Earlier configuration request")
        self.assertEqual(state["recorded_goal"]["relationship_to_current_request"], "not_assessed")
        self.assertEqual(state["remaining_work_record_status"], "no_items_recorded")
        self.assertEqual(state["request_completion_status"], "not_assessed")
        self.assertNotIn("goal", state)
        self.assertNotIn("remaining_work", state)
        # Durable state, legacy API and its hash keep their original representation.
        legacy = SessionContextProjector().for_prompt(projection).prompt_data["work_state"]
        self.assertEqual(legacy["goal"], "Earlier configuration request")
        self.assertEqual(legacy["remaining_work"], [])
        self.assertEqual(projection.content_hash, canonical_hash(projection.hash_source()))

    def test_explicit_update_and_legacy_record_do_not_invent_memory_identity(self):
        legacy = self.recorded(1, "turn-1", "Continue")
        legacy = replace(legacy, payload={
            key: value for key, value in legacy.payload.items()
            if not key.startswith("working_memory_")
        })
        _, prompt = self.project((legacy,))
        source = prompt.prompt_data["work_state"]["recorded_goal"]["source"]
        self.assertEqual(source["kind"], "recorded_working_state")
        self.assertNotIn("memory_revision", source)

        explicit = SessionEvent("explicit", "session-1", 2, "session.context_state_updated", {
            "working_state": {"goal": "New explicitly recorded goal", "remaining_work": ["check wiring"]},
        })
        _, prompt = self.project((explicit, legacy))
        state = prompt.prompt_data["work_state"]
        self.assertEqual(state["recorded_goal"]["text"], "New explicitly recorded goal")
        self.assertEqual(state["recorded_goal"]["source"]["kind"], "explicit_session_state")
        self.assertEqual(state["remaining_work_record_status"], "has_recorded_items")
        self.assertNotIn("memory_revision", state["recorded_goal"]["source"])

    def test_fallback_user_sentence_is_not_duplicated_as_a_historical_goal(self):
        current = Message("live", MessageRole.USER, (TextBlock("继续实施到完整可跑"),))
        events = (
            result_event(1, "turn-1", "Implement A and B without editing tests", "I can do both."),
            SessionEvent("input", "session-1", 2, "session.turn_input_recorded", {
                "task_id": "task-1", "turn_id": "turn-2", "user_message": current.to_data(),
            }),
        )
        projector = SessionContextProjector()
        snapshot = SessionSnapshot.create("session-1", "uid:1", "chat")
        projection = projector.project(snapshot, events)
        prompt = projector.for_prompt(
            projection, conversation_mode=True, exclude_task_id="task-1", exclude_turn_id="turn-2",
            goal_source=working_goal_source(events, projection.working_state.goal),
        )
        messages = PromptTemplate.default().assemble((prompt.message, current), ()).messages

        self.assertIsNone(prompt.prompt_data["work_state"]["recorded_goal"]["text"])
        self.assertEqual(sum(message.text.count(current.text) for message in messages), 1)
        self.assertIn("Implement A and B without editing tests", [message.text for message in messages])
        self.assertEqual(messages[-1], current)

    def test_compaction_preserves_recorded_goal_provenance_and_current_input(self):
        events = [self.recorded(n, f"turn-{n}", "Prior request " * 30) for n in range(1, 12)]
        _, prompt = self.project(events)
        current = Message("live", MessageRole.USER, (TextBlock("Continue implementing"),))
        manager = ContextWindowManager(history_soft_tokens=3000)
        template = PromptTemplate.default()
        before = json.loads(prompt.message.text)["work_state"]
        prepared = manager.prepare(
            conversation=(prompt.message, current), tools=(), prompt_template=template,
            context_window=128000, max_output_tokens=8192,
        )

        self.assertIsNotNone(prepared.compaction)
        body = next(json.loads(message.text) for message in prepared.messages
                    if message.message_id.startswith("session-context-"))
        self.assertEqual(body["work_state"], before)
        self.assertEqual(template.assemble(prepared.messages, ()).messages[-1], current)

    def test_cleared_goal_cannot_resurrect_an_older_equal_record(self):
        prior = self.recorded(1, "turn-1", "Earlier configuration request")
        cleared = SessionEvent("clear", "session-1", 2, "session.context_state_updated", {
            "working_state": {"goal": None},
        })
        projection, prompt = self.project((prior, cleared))

        # The fallback happens to equal an older goal; it is still dialogue.
        self.assertEqual(projection.working_state.goal, "Earlier configuration request")
        self.assertEqual(working_goal_source((prior, cleared), projection.working_state.goal),
                         {"kind": "unknown"})
        self.assertIsNone(prompt.prompt_data["work_state"]["recorded_goal"]["text"])

    def test_live_evaluation_accepts_real_patch_many_schema_and_protects_tests(self):
        from scripts.evaluate_continuation_context import permitted
        from tsm_agt.ports import ToolCall

        call = ToolCall("batch", "core.apply_patches", {"patches": [
            {"path": "web.py", "expected_hash": "hash", "edits": []},
            {"path": "runtime.py", "expected_hash": "hash", "edits": []},
        ]})
        self.assertTrue(permitted(call, "implementation"))
        self.assertFalse(permitted(call, "analysis"))
        self.assertFalse(permitted(call, "restricted"))
        protected = replace(call, arguments={"patches": [{"path": "test_flow.py"}]})
        self.assertFalse(permitted(protected, "implementation"))


class ContinuationContextRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_kernel_attaches_provenance_from_real_session_records(self):
        app = compose_fixture_application(model_adapter=ScriptedModel([
            text_response("first", "Analysis recorded."),
            text_response("second", "Follow-up recorded."),
        ]), tool_adapters=())
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            await app.kernel.run_agent_turn(task.task_id, "Analyze the configuration")
            await app.kernel.run_agent_turn(task.task_id, "Now implement the skill system")
            prompt = await app.kernel.get_session_prompt_projection(task.session_id)
            source = prompt.prompt_data["work_state"]["recorded_goal"]["source"]
            memory = await app.kernel.get_working_memory(task.task_id)

            self.assertEqual(source["task_id"], task.task_id)
            self.assertEqual(source["memory_revision"], memory.revision)
            self.assertEqual(source["memory_hash"], memory.content_hash)
            self.assertEqual(prompt.prompt_data["work_state"]["request_completion_status"], "not_assessed")
