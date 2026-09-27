"""INV-8: Task context is selected by provenance, never by recency."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    SessionSnapshot, SessionTaskRelation,
)
from tsm_agt.core.session_context import (
    ContextAuthority,
    ContextScope,
    SessionContextProjector,
    SessionConversationMessage,
    select_messages_by_scope,
)
from tsm_agt.ports import Message, MessageRole, SessionEvent, TextBlock


def _event(
    sequence: int, task_id: str, user: str, assistant: str,
) -> SessionEvent:
    return SessionEvent(
        f"event-{sequence}", "session-1", sequence,
        "session.task_result_recorded",
        {
            "task_id": task_id,
            "turn_id": f"turn-{sequence}",
            "user_message": Message(
                f"user-{sequence}", MessageRole.USER, (TextBlock(user),),
            ).to_data(),
            "assistant_message": Message(
                f"assistant-{sequence}", MessageRole.ASSISTANT,
                (TextBlock(assistant),),
            ).to_data(),
            "task_summary": {
                "task_id": task_id,
                "turn_id": f"turn-{sequence}",
                "goal": user,
                "recorded_task_state": "EXECUTING",
                "tool_counts": {},
                "important_actions": [],
                "confirmed": [],
                "completed_work": [],
                "remaining_work": [],
                "workspace_roots": [],
                "mutations": [],
                "verification_status": None,
            },
        },
    )


def _projection() -> tuple[SessionSnapshot, tuple[SessionEvent, ...]]:
    snapshot = SessionSnapshot.create(
        "session-1", "uid:1", "scope",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    for _ in range(4):
        snapshot = snapshot.bump_context()
    events = (
        _event(1, "task-a", "alpha question", "alpha answer"),
        _event(2, "task-a", "alpha follow up", "alpha follow answer"),
        _event(3, "task-b", "beta question", "beta answer"),
        _event(4, "task-b", "beta follow up", "beta follow answer"),
    )
    return snapshot, events


class ContextScopeSelectionTest(unittest.TestCase):
    def test_scope_excludes_other_task_messages(self) -> None:
        projection = SessionContextProjector().project(*_projection())
        scope = ContextScope(
            "session-1", task_id="task-b", lineage_task_ids=("task-b",),
        )

        selected = select_messages_by_scope(projection.messages, scope)

        self.assertTrue(selected)
        self.assertEqual({item.task_id for item in selected}, {"task-b"})

    def test_scope_includes_explicit_event_sequence(self) -> None:
        projection = SessionContextProjector().project(*_projection())
        # sequence 1 is task-a's first message; name it explicitly.
        scope = ContextScope(
            "session-1", task_id="task-b", lineage_task_ids=("task-b",),
            explicit_event_sequences=(1,),
        )

        selected = select_messages_by_scope(projection.messages, scope)
        task_ids = {item.task_id for item in selected}

        self.assertEqual(task_ids, {"task-a", "task-b"})
        # An explicit event reference pulls in that event's whole visible pair.
        self.assertEqual(
            {item.text for item in selected if item.source_event_sequence == 1},
            {"alpha question", "alpha answer"},
        )

    def test_unattributed_message_is_not_selected(self) -> None:
        unattributed = SessionConversationMessage(
            "msg-orphan", MessageRole.USER, "orphan", None, None, 99,
        )
        scope = ContextScope(
            "session-1", task_id="task-b", lineage_task_ids=("task-b",),
        )

        self.assertFalse(scope.allows(unattributed))
        self.assertEqual(select_messages_by_scope((unattributed,), scope), ())

    def test_for_prompt_scope_labels_authority(self) -> None:
        projection = SessionContextProjector().project(*_projection())
        scope = ContextScope(
            "session-1", task_id="task-b", lineage_task_ids=("task-b",),
        )

        prompt = SessionContextProjector().for_prompt(
            projection, scope=scope, include_background=False,
        )
        assert prompt.message is not None
        body = json.loads(prompt.message.text)

        self.assertEqual(
            {item["task_id"] for item in body["scoped_messages"]}, {"task-b"}
        )
        self.assertTrue(all(
            item["authority"] == ContextAuthority.AUTHORITATIVE.value
            for item in body["scoped_messages"]
        ))
        self.assertEqual(body["background_messages"], [])
        self.assertEqual(body["selection"]["background_message_count"], 0)
        self.assertGreater(body["selection"]["dropped_message_count"], 0)
        self.assertEqual(body["scope"]["lineage_task_ids"], ["task-b"])

    def test_for_prompt_without_scope_is_unchanged(self) -> None:
        projection = SessionContextProjector().project(*_projection())

        prompt = SessionContextProjector().for_prompt(projection)
        assert prompt.message is not None
        body = json.loads(prompt.message.text)

        # Legacy behaviour: the tail window still drives recent_messages, and no
        # scoped/background duplicate is emitted.
        self.assertEqual(len(body["recent_messages"]), 8)
        self.assertEqual(body["scoped_messages"], [])
        self.assertEqual(body["background_messages"], [])

    def test_include_background_false_drops_background(self) -> None:
        projection = SessionContextProjector().project(*_projection())
        scope = ContextScope(
            "session-1", task_id="task-b", lineage_task_ids=("task-b",),
        )

        prompt = SessionContextProjector().for_prompt(
            projection, scope=scope, include_background=True,
        )
        assert prompt.message is not None
        body = json.loads(prompt.message.text)

        self.assertTrue(body["background_messages"])
        self.assertTrue(any(
            item["task_id"] == "task-a"
            for item in body["background_messages"]
        ))

    def test_property_selection_is_provenance_only(self) -> None:
        snapshot = SessionSnapshot.create(
            "session-1", "uid:1", "scope",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        task_ids = tuple(f"task-{index}" for index in range(10))
        events = tuple(
            _event(index, task_id, f"q{index} {task_id}", f"a{index}")
            for index, task_id in enumerate(task_ids, start=1)
        )
        for _ in events:
            snapshot = snapshot.bump_context()
        projection = SessionContextProjector().project(snapshot, events)
        projector = SessionContextProjector()

        for task_id in task_ids:
            scope = ContextScope(
                "session-1", task_id=task_id, lineage_task_ids=(task_id,),
            )
            prompt = projector.for_prompt(
                projection, scope=scope, include_background=False,
            )
            assert prompt.message is not None
            body = json.loads(prompt.message.text)
            self.assertEqual(
                {item["task_id"] for item in body["scoped_messages"]},
                {task_id},
                msg=task_id,
            )
            self.assertEqual(body["background_messages"], [], msg=task_id)


class PlannerScopeIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.application = compose_fixture_application()
        await self.application.registry.start_all()
        self.temp_dir = tempfile.TemporaryDirectory()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()
        await self.application.registry.stop_all()

    async def test_planner_context_has_zero_unrelated_messages(self) -> None:
        kernel = self.application.kernel
        workspace = Path(self.temp_dir.name)
        first = await kernel.create_task(
            "alpha task", workspace, task_id="task-alpha",
            original_user_text="alpha task",
        )
        second = await kernel.create_task(
            "beta task", workspace, task_id="task-beta",
            session_id=first.session_id,
            source_task_id=first.task_id,
            task_relation=SessionTaskRelation.FOLLOW_UP,
            original_user_text="beta task",
        )

        _, planning_context, _, _ = await kernel._task_spec_planning_context(
            second
        )

        scoped = planning_context["session"]["scoped_messages"]
        self.assertTrue(scoped)
        self.assertEqual(
            {item["task_id"] for item in scoped}, {"task-beta"}
        )
        self.assertEqual(
            planning_context["session"]["background_messages"], []
        )
        self.assertNotIn(
            "recent_messages", planning_context["session"]
        )
        self.assertEqual(
            planning_context["authority"]["scoped_messages"],
            ContextAuthority.AUTHORITATIVE.value,
        )
        self.assertEqual(
            planning_context["related_task"]["authority"],
            ContextAuthority.SCOPED_BACKGROUND.value,
        )


if __name__ == "__main__":
    unittest.main()
