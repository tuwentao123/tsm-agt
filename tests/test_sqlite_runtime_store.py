from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.ports import AdapterContext, RuntimeEvent, RuntimeUnitOfWork


def context() -> AdapterContext:
    return AdapterContext(config={}, emit_event=lambda _type, _payload: None)


class SQLiteRuntimeStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "runtime.db"
        self.store = SQLiteRuntimeStore(self.database)
        await self.store.start(context())

    async def asyncTearDown(self) -> None:
        await self.store.stop(datetime.now(timezone.utc))
        self.temp_dir.cleanup()

    async def test_state_and_events_survive_reopen(self) -> None:
        event = RuntimeEvent(
            "evt-1", "task-1", 1, "task.created", {"goal": "你好"}
        )
        await self.store.commit(
            RuntimeUnitOfWork(
                "task-1", 0, {"state": "CREATED", "goal": "你好"}, (event,)
            )
        )
        await self.store.stop(datetime.now(timezone.utc))

        reopened = SQLiteRuntimeStore(self.database)
        await reopened.start(context())
        try:
            stored = await reopened.load_task("task-1")
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.data["goal"], "你好")
            self.assertEqual(stored.version, 1)
            self.assertEqual(await reopened.read_events("task-1"), (event,))
        finally:
            await reopened.stop(datetime.now(timezone.utc))

    async def test_cursor_returns_only_later_events_in_order(self) -> None:
        events = (
            RuntimeEvent("evt-1", "task-1", 1, "task.created"),
            RuntimeEvent("evt-2", "task-1", 2, "turn.started"),
            RuntimeEvent("evt-3", "task-1", 3, "turn.completed"),
        )
        await self.store.commit(
            RuntimeUnitOfWork("task-1", 0, {"state": "EXECUTING"}, events)
        )

        later = await self.store.read_events("task-1", after_sequence=1)
        self.assertEqual([event.sequence for event in later], [2, 3])

    async def test_version_conflict_does_not_modify_state_or_events(self) -> None:
        first = RuntimeUnitOfWork(
            "task-1",
            0,
            {"state": "CREATED"},
            (RuntimeEvent("evt-1", "task-1", 1, "task.created"),),
        )
        await self.store.commit(first)

        with self.assertRaisesRegex(ValueError, "version conflict"):
            await self.store.commit(
                RuntimeUnitOfWork(
                    "task-1",
                    0,
                    {"state": "FAILED"},
                    (RuntimeEvent("evt-2", "task-1", 2, "task.failed"),),
                )
            )
        stored = await self.store.load_task("task-1")
        assert stored is not None
        self.assertEqual(stored.data["state"], "CREATED")
        self.assertEqual(len(await self.store.read_events("task-1")), 1)

    async def test_non_contiguous_events_rollback_entire_transaction(self) -> None:
        with self.assertRaisesRegex(ValueError, "event sequence"):
            await self.store.commit(
                RuntimeUnitOfWork(
                    "task-1",
                    0,
                    {"state": "CREATED"},
                    (RuntimeEvent("evt-2", "task-1", 2, "task.created"),),
                )
            )
        self.assertIsNone(await self.store.load_task("task-1"))
        self.assertEqual(await self.store.read_events("task-1"), ())

    async def test_duplicate_event_id_rolls_back_task_update(self) -> None:
        await self.store.commit(
            RuntimeUnitOfWork(
                "task-1",
                0,
                {"state": "CREATED"},
                (RuntimeEvent("evt-shared", "task-1", 1, "task.created"),),
            )
        )
        with self.assertRaisesRegex(ValueError, "integrity violation"):
            await self.store.commit(
                RuntimeUnitOfWork(
                    "task-2",
                    0,
                    {"state": "CREATED"},
                    (RuntimeEvent("evt-shared", "task-2", 1, "task.created"),),
                )
            )
        self.assertIsNone(await self.store.load_task("task-2"))

    async def test_non_json_state_fails_before_transaction(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON serializable"):
            await self.store.commit(
                RuntimeUnitOfWork(
                    "task-1", 0, {"bad": object()}, ()
                )
            )
        self.assertIsNone(await self.store.load_task("task-1"))


if __name__ == "__main__":
    unittest.main()
