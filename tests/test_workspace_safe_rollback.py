from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import InMemoryRuntimeStore
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import MutationOperation, TaskState, WorkspaceMutationConflict


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _FailNextMutationCommitStore(InMemoryRuntimeStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_mutation_commit = False

    async def commit(self, unit):
        if self.fail_next_mutation_commit and any(
            event.event_type == "workspace.rollback_committed"
            for event in unit.events
        ):
            self.fail_next_mutation_commit = False
            raise RuntimeError("simulated rollback journal failure")
        return await super().commit(unit)


class WorkspaceSafeRollbackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.store = _FailNextMutationCommitStore()
        self.application = compose_fixture_application(store_adapter=self.store)
        await self.application.registry.start_all()
        self.task = await self._executing_task("task-safe-rollback")

    async def asyncTearDown(self) -> None:
        await self.application.registry.stop_all()
        self.temporary.cleanup()

    async def _executing_task(self, task_id: str):
        task = await self.application.kernel.create_task(
            "rollback one owned mutation safely", self.workspace, task_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await self.application.kernel.transition_task(
                task.task_id, state, state.value
            )
        return task

    async def test_rolls_back_created_file_by_deleting_it(self) -> None:
        original = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-create", "created.txt", "created\n", None
        )
        rollback = await self.application.kernel.rollback_workspace_mutation(
            self.task.task_id, "step-rollback-create", original.mutation_id
        )

        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertEqual(rollback.operation, MutationOperation.DELETE)
        self.assertEqual(rollback.before_hash, original.after_hash)
        self.assertIsNone(rollback.after_hash)
        self.assertEqual(rollback.reverts_mutation_id, original.mutation_id)
        journal = (await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal
        self.assertEqual(journal, (original, rollback))

    async def test_rolls_back_modified_file_content_and_mode(self) -> None:
        target = self.workspace / "modified.txt"
        target.write_text("before\n", encoding="utf-8")
        os.chmod(target, 0o640)
        original = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-modify", target.name, "after\n",
            sha256("before\n"),
        )
        os.chmod(target, 0o600)

        rollback = await self.application.kernel.rollback_workspace_mutation(
            self.task.task_id, "step-rollback-modify", original.mutation_id
        )

        self.assertEqual(target.read_text(), "before\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(rollback.operation, MutationOperation.MODIFY)
        self.assertEqual(rollback.after_hash, original.before_hash)
        self.assertEqual(rollback.reverts_mutation_id, original.mutation_id)

    async def test_rolls_back_deleted_file_from_verified_backup(self) -> None:
        target = self.workspace / "deleted.txt"
        target.write_text("restore me\n", encoding="utf-8")
        os.chmod(target, 0o640)
        original = await self.application.kernel.delete_workspace_file(
            self.task.task_id, "step-delete", target.name,
            sha256("restore me\n"),
        )

        rollback = await self.application.kernel.rollback_workspace_mutation(
            self.task.task_id, "step-rollback-delete", original.mutation_id
        )

        self.assertEqual(target.read_text(), "restore me\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(rollback.operation, MutationOperation.CREATE)
        self.assertIsNone(rollback.before_hash)
        self.assertEqual(rollback.after_hash, original.before_hash)

    async def test_user_change_after_mutation_blocks_rollback(self) -> None:
        target = self.workspace / "user-owned.txt"
        target.write_text("before\n", encoding="utf-8")
        original = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-agent", target.name, "agent\n",
            sha256("before\n"),
        )
        target.write_text("user\n", encoding="utf-8")

        with self.assertRaises(WorkspaceMutationConflict):
            await self.application.kernel.rollback_workspace_mutation(
                self.task.task_id, "step-conflict", original.mutation_id
            )
        self.assertEqual(target.read_text(), "user\n")
        self.assertEqual(
            len((await self.application.kernel.get_task(
                self.task.task_id
            )).mutation_journal),
            1,
        )

    async def test_recreated_deleted_path_blocks_rollback(self) -> None:
        target = self.workspace / "recreated.txt"
        target.write_text("agent deletes\n", encoding="utf-8")
        original = await self.application.kernel.delete_workspace_file(
            self.task.task_id, "step-delete", target.name,
            sha256("agent deletes\n"),
        )
        target.write_text("user recreated\n", encoding="utf-8")

        with self.assertRaises(WorkspaceMutationConflict):
            await self.application.kernel.rollback_workspace_mutation(
                self.task.task_id, "step-conflict", original.mutation_id
            )
        self.assertEqual(target.read_text(), "user recreated\n")

    async def test_tampered_backup_is_rejected(self) -> None:
        target = self.workspace / "tampered.txt"
        target.write_text("before\n", encoding="utf-8")
        original = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-agent", target.name, "after\n",
            sha256("before\n"),
        )
        backup = self.workspace / str(original.backup_ref)
        backup.write_text("attacker content\n", encoding="utf-8")

        with self.assertRaises(WorkspaceMutationConflict):
            await self.application.kernel.rollback_workspace_mutation(
                self.task.task_id, "step-tampered", original.mutation_id
            )
        self.assertEqual(target.read_text(), "after\n")

    async def test_same_mutation_cannot_be_rolled_back_twice(self) -> None:
        original = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-create", "once.txt", "one\n", None
        )
        await self.application.kernel.rollback_workspace_mutation(
            self.task.task_id, "step-rollback", original.mutation_id
        )
        with self.assertRaisesRegex(ValueError, "already rolled back"):
            await self.application.kernel.rollback_workspace_mutation(
                self.task.task_id, "step-again", original.mutation_id
            )

    async def test_journal_failure_restores_pre_rollback_state(self) -> None:
        target = self.workspace / "journal-failure.txt"
        target.write_text("before\n", encoding="utf-8")
        original = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-agent", target.name, "after\n",
            sha256("before\n"),
        )
        self.store.fail_next_mutation_commit = True

        with self.assertRaisesRegex(RuntimeError, "simulated rollback journal"):
            await self.application.kernel.rollback_workspace_mutation(
                self.task.task_id, "step-db-failure", original.mutation_id
            )
        self.assertEqual(target.read_text(), "after\n")
        self.assertEqual(
            (await self.application.kernel.get_task(
                self.task.task_id
            )).mutation_journal,
            (original,),
        )

    async def test_rollback_record_survives_sqlite_restart(self) -> None:
        await self.application.registry.stop_all()
        database = self.workspace / "rollback.db"
        first = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(database)
        )
        await first.registry.start_all()
        try:
            task = await first.kernel.create_task(
                "persist rollback", self.workspace, "task-sqlite-rollback"
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await first.kernel.transition_task(
                    task.task_id, state, state.value
                )
            original = await first.kernel.write_workspace_text(
                task.task_id, "step-create", "sqlite.txt", "saved\n", None
            )
            rollback = await first.kernel.rollback_workspace_mutation(
                task.task_id, "step-rollback", original.mutation_id
            )
        finally:
            await first.registry.stop_all()
        second = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(database)
        )
        await second.registry.start_all()
        try:
            restored = await second.kernel.get_task("task-sqlite-rollback")
            self.assertEqual(restored.mutation_journal, (original, rollback))
            self.assertEqual(
                restored.mutation_journal[-1].reverts_mutation_id,
                original.mutation_id,
            )
        finally:
            await second.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
