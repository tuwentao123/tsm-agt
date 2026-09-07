from __future__ import annotations

import unittest

from tsm_agt.adapters.fixture import InMemoryRuntimeStore
from tsm_agt.ports import AdapterContext, RuntimeEvent, RuntimeUnitOfWork


class RuntimeStoreContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = InMemoryRuntimeStore()
        await self.store.start(AdapterContext(config={}, emit_event=lambda _type, _payload: None))

    async def test_state_and_event_commit_together(self) -> None:
        event = RuntimeEvent("evt-1", "task-1", 1, "task.created")
        result = await self.store.commit(
            RuntimeUnitOfWork("task-1", 0, {"state": "CREATED"}, (event,))
        )

        self.assertEqual(result.committed_version, 1)
        stored = await self.store.load_task("task-1")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.data, {"state": "CREATED"})
        self.assertEqual(stored.version, 1)
        self.assertEqual(stored.last_event_sequence, 1)
        self.assertEqual(await self.store.read_events("task-1"), (event,))

    async def test_optimistic_version_conflict_is_rejected(self) -> None:
        unit = RuntimeUnitOfWork("task-1", 0, {"state": "CREATED"}, ())
        await self.store.commit(unit)

        with self.assertRaises(ValueError):
            await self.store.commit(unit)

    async def test_non_contiguous_event_sequence_is_rejected_atomically(self) -> None:
        invalid = RuntimeEvent("evt-2", "task-1", 2, "task.created")

        with self.assertRaises(ValueError):
            await self.store.commit(
                RuntimeUnitOfWork("task-1", 0, {"state": "CREATED"}, (invalid,))
            )

        self.assertIsNone(await self.store.load_task("task-1"))
        self.assertEqual(await self.store.read_events("task-1"), ())


if __name__ == "__main__":
    unittest.main()
