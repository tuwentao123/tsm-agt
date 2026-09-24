from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    InvalidTaskTransition,
    SessionTaskRelation,
    TaskNotFound,
    TaskState,
)
from tsm_agt.core.configuration import canonical_hash
from tsm_agt.core.session_context import (
    SessionContextProjector,
    SessionConversationMessage,
    SessionConversationProjection,
    SessionTaskSummary,
    SessionWorkingState,
)
from tsm_agt.ports import MessageRole
from tsm_agt.ports import RuntimeStorePort


class KernelTaskTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.application = compose_fixture_application()
        await self.application.registry.start_all()
        self.temp_dir = tempfile.TemporaryDirectory()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()
        await self.application.registry.stop_all()

    async def test_create_and_transition_persist_state_with_ordered_events(self) -> None:
        task = await self.application.kernel.create_task(
            "  fix failing tests  ", Path(self.temp_dir.name), task_id="task-1"
        )
        updated = await self.application.kernel.transition_task(
            task.task_id, TaskState.INTAKE, "begin intake"
        )

        self.assertEqual(task.goal, "fix failing tests")
        self.assertEqual(updated.state, TaskState.INTAKE)
        self.assertEqual(await self.application.kernel.get_task("task-1"), updated)

        store = self.application.registry.require(RuntimeStorePort)
        events = await store.read_events("task-1")
        self.assertEqual([event.sequence for event in events], [1, 2])
        self.assertEqual(
            [event.event_type for event in events],
            ["task.created", "task.state_changed"],
        )
        self.assertEqual(events[1].payload["previous_state"], "CREATED")
        self.assertEqual(events[1].payload["next_state"], "INTAKE")

    async def test_illegal_transition_does_not_write_state_or_event(self) -> None:
        await self.application.kernel.create_task(
            "fix tests", Path(self.temp_dir.name), task_id="task-1"
        )

        with self.assertRaises(InvalidTaskTransition):
            await self.application.kernel.transition_task(
                "task-1", TaskState.EXECUTING, "skip required phases"
            )

        current = await self.application.kernel.get_task("task-1")
        store = self.application.registry.require(RuntimeStorePort)
        self.assertEqual(current.state, TaskState.CREATED)
        self.assertEqual(len(await store.read_events("task-1")), 1)

    async def test_unknown_task_is_reported(self) -> None:
        with self.assertRaises(TaskNotFound):
            await self.application.kernel.get_task("missing")

    async def test_invalid_input_is_rejected_before_persistence(self) -> None:
        with self.assertRaisesRegex(ValueError, "goal must not be empty"):
            await self.application.kernel.create_task(
                "   ", Path(self.temp_dir.name), task_id="task-1"
            )

        store = self.application.registry.require(RuntimeStorePort)
        self.assertIsNone(await store.load_task("task-1"))

    async def test_task_spec_planning_context_uses_follow_up_user_text(self) -> None:
        source = await self.application.kernel.create_task(
            "review the prior change", Path(self.temp_dir.name),
            task_id="task-source",
        )
        original_user_text = "检查敏感信息后，用中文提交并推送改动"
        follow_up = await self.application.kernel.create_task(
            "[session-follow-up]\nCurrent request:\n"
            + original_user_text,
            Path(self.temp_dir.name),
            task_id="task-follow-up",
            session_id=source.session_id,
            source_task_id=source.task_id,
            task_relation=SessionTaskRelation.FOLLOW_UP,
            original_user_text=original_user_text,
        )

        current_request, planning_context, session_revision, context_hash = (
            await self.application.kernel._task_spec_planning_context(follow_up)
        )

        self.assertNotEqual(follow_up.goal, original_user_text)
        self.assertEqual(current_request, original_user_text)
        self.assertGreater(session_revision, 0)
        self.assertEqual(context_hash, canonical_hash(planning_context))
        self.assertIn(
            {
                "message_id": "msg-session-input-task-follow-up",
                "role": "user",
                "text": original_user_text,
                "task_id": "task-follow-up",
                "turn_id": None,
                "source_event_sequence": 3,
            },
            planning_context["session"]["recent_messages"],
        )

    def test_for_prompt_preserves_pinned_task_summaries(self) -> None:
        projector = SessionContextProjector(
            recent_visible_message_limit=2,
            detailed_task_summary_limit=1,
        )
        messages = (
            SessionConversationMessage(
                "m1", MessageRole.USER, "latest", "task-new", None, 1
            ),
        )
        task_summaries = (
            SessionTaskSummary(
                task_id="task-old",
                turn_id="turn-old",
                goal="old goal",
                recorded_task_state="SUCCEEDED",
            ),
            SessionTaskSummary(
                task_id="task-new",
                turn_id="turn-new",
                goal="new goal",
                recorded_task_state="SUCCEEDED",
            ),
        )
        projection = SessionConversationProjection(
            session_id="session-1",
            revision=1,
            messages=messages,
            working_state=SessionWorkingState(),
            resource_catalog=(),
            question_catalog=(),
            task_summaries=task_summaries,
            source_event_sequences=(1,),
            content_hash=canonical_hash({
                "schema_version": 1,
                "session_id": "session-1",
                "revision": 1,
                "messages": [item.source_data() for item in messages],
                "working_state": SessionWorkingState().to_data(),
                "resource_catalog": [],
                "question_catalog": [],
                "task_summaries": [
                    item.to_data() for item in task_summaries
                ],
                "source_event_sequences": [1],
            }),
        )

        prompt = projector.for_prompt(
            projection,
            pinned_task_ids=("task-old",),
        )

        summaries = prompt.prompt_data["recent_task_summaries"]
        self.assertEqual([item["task_id"] for item in summaries], [
            "task-new",
            "task-old",
        ])


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
