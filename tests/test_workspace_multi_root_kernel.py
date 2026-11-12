"""Cross-root writes, reads and rollback on one workspace."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import InMemoryRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class WorkspaceMultiRootKernelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "app"
        self.workspace.mkdir()
        self.extra = Path(self.temporary.name) / "shared"
        self.extra.mkdir()
        self.application = compose_fixture_application(
            store_adapter=InMemoryRuntimeStore()
        )
        await self.application.registry.start_all()

    async def asyncTearDown(self) -> None:
        await self.application.registry.stop_all()
        self.temporary.cleanup()

    async def _executing_task(self):
        task = await self.application.kernel.create_task(
            "work across roots", self.workspace, "task-multi-root"
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await self.application.kernel.transition_task(
                task.task_id, state, state.value
            )
        return task

    async def test_write_read_and_rollback_inside_an_attached_root(self) -> None:
        target = self.extra / "notes" / "readme.md"
        target.parent.mkdir()
        target.write_text("before\n", encoding="utf-8")
        task = await self._executing_task()
        await self.application.kernel.set_workspace_roots(
            task.task_id, (str(self.extra),)
        )

        mutation = await self.application.kernel.write_workspace_text(
            task.task_id, "step-write", str(target), "after\n", sha256("before\n")
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
        self.assertEqual(mutation.path, "notes/readme.md")
        self.assertEqual(mutation.root, str(self.extra.resolve()))

        stored = await self.application.kernel.get_task(task.task_id)
        self.assertEqual(stored.workspace_roots, (str(self.extra.resolve()),))

        await self.application.kernel.rollback_workspace_mutation(
            task.task_id, "step-rollback", mutation.mutation_id
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")

    async def test_delete_inside_an_attached_root(self) -> None:
        target = self.extra / "obsolete.txt"
        target.write_text("gone\n", encoding="utf-8")
        task = await self._executing_task()
        await self.application.kernel.set_workspace_roots(
            task.task_id, (str(self.extra),)
        )

        mutation = await self.application.kernel.delete_workspace_file(
            task.task_id, "step-delete", str(target), sha256("gone\n")
        )
        self.assertFalse(target.exists())
        self.assertEqual(mutation.root, str(self.extra.resolve()))

        await self.application.kernel.rollback_workspace_mutation(
            task.task_id, "step-restore", mutation.mutation_id
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "gone\n")

    async def test_primary_root_write_records_no_root(self) -> None:
        target = self.workspace / "main.py"
        target.write_text("a = 1\n", encoding="utf-8")
        task = await self._executing_task()
        await self.application.kernel.set_workspace_roots(
            task.task_id, (str(self.extra),)
        )

        mutation = await self.application.kernel.write_workspace_text(
            task.task_id, "step-primary", str(target), "a = 2\n", sha256("a = 1\n")
        )
        self.assertIsNone(mutation.root)
        self.assertEqual(mutation.path, "main.py")
        self.assertEqual(target.read_text(encoding="utf-8"), "a = 2\n")

    async def test_path_outside_every_root_is_rejected(self) -> None:
        outside = Path(self.temporary.name) / "outside.txt"
        task = await self._executing_task()
        await self.application.kernel.set_workspace_roots(
            task.task_id, (str(self.extra),)
        )
        with self.assertRaises(PermissionError):
            await self.application.kernel.write_workspace_text(
                task.task_id, "step-outside", str(outside), "x\n", None
            )
        self.assertFalse(outside.exists())

    async def test_batch_write_keeps_to_the_primary_root(self) -> None:
        first = self.extra / "one.txt"
        second = self.extra / "two.txt"
        task = await self._executing_task()
        await self.application.kernel.set_workspace_roots(
            task.task_id, (str(self.extra),)
        )
        with self.assertRaises(ValueError):
            await self.application.kernel.write_workspace_text_batch(
                task.task_id, "step-batch",
                ((str(first), "1\n", None), (str(second), "2\n", None)),
            )
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    async def test_read_roots_include_attached_roots(self) -> None:
        task = await self._executing_task()
        updated = await self.application.kernel.set_workspace_roots(
            task.task_id, (str(self.extra),)
        )
        self.assertEqual(
            updated.all_workspace_roots(),
            (self.workspace.resolve(), self.extra.resolve()),
        )


if __name__ == "__main__":
    unittest.main()
