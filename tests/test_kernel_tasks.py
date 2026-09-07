from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import InvalidTaskTransition, TaskNotFound, TaskState
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


if __name__ == "__main__":
    unittest.main()
