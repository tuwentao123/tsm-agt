from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import InMemoryRuntimeStore
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    MutationOperation, TaskState, WorkspaceMutationConflict,
)
from tsm_agt.core.workspace import (
    commit_prepared_workspace_deletion, commit_prepared_workspace_mutation,
    prepare_workspace_file_delete, prepare_workspace_text_write,
)
from tsm_agt.ports import (
    RuntimeStorePort, WorkspaceFilesystemPort, WorkspacePathPort,
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _FailNextCommitStore(InMemoryRuntimeStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    async def commit(self, unit):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated journal database failure")
        return await super().commit(unit)


class WorkspaceMutationJournalTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        (self.workspace / "src").mkdir()
        self.store = _FailNextCommitStore()
        self.application = compose_fixture_application(store_adapter=self.store)
        await self.application.registry.start_all()
        self.filesystem = self.application.registry.require(
            WorkspaceFilesystemPort
        )
        self.path_service = self.application.registry.require(WorkspacePathPort)

    async def asyncTearDown(self) -> None:
        await self.application.registry.stop_all()
        self.temporary.cleanup()

    async def _executing_task(self, task_id: str = "task-mutation"):
        task = await self.application.kernel.create_task(
            "mutate workspace safely", self.workspace, task_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await self.application.kernel.transition_task(
                task.task_id, state, state.value
            )
        return task

    async def test_modify_is_atomic_backed_up_and_journaled(self) -> None:
        target = self.workspace / "app.py"
        target.write_text("before\n", encoding="utf-8")
        os.chmod(target, 0o640)
        task = await self._executing_task()

        record = await self.application.kernel.write_workspace_text(
            task.task_id, "step-fix-app", "app.py", "after\n",
            sha256("before\n"),
        )

        self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(record.operation, MutationOperation.MODIFY)
        self.assertEqual(record.before_hash, sha256("before\n"))
        self.assertEqual(record.after_hash, sha256("after\n"))
        self.assertEqual(record.step_id, "step-fix-app")
        self.assertIsNotNone(record.backup_ref)
        backup = self.workspace / str(record.backup_ref)
        self.assertEqual(backup.read_text(encoding="utf-8"), "before\n")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

        persisted = await self.application.kernel.get_task(task.task_id)
        self.assertEqual(persisted.mutation_journal, (record,))
        events = await self.store.read_events(task.task_id)
        self.assertEqual(events[-1].event_type, "workspace.mutation_committed")
        self.assertEqual(events[-1].payload["mutation_id"], record.mutation_id)

    async def test_create_requires_missing_precondition_and_has_no_backup(self) -> None:
        task = await self._executing_task()
        record = await self.application.kernel.write_workspace_text(
            task.task_id, "step-create", "new.txt", "created\n", None
        )
        self.assertEqual(record.operation, MutationOperation.CREATE)
        self.assertIsNone(record.before_hash)
        self.assertIsNone(record.backup_ref)
        self.assertEqual(
            (self.workspace / "new.txt").read_text(encoding="utf-8"),
            "created\n",
        )

    async def test_delete_requires_hash_is_backed_up_and_journaled(self) -> None:
        target = self.workspace / "obsolete.txt"
        target.write_text("remove me\n", encoding="utf-8")
        os.chmod(target, 0o640)
        task = await self._executing_task()

        record = await self.application.kernel.delete_workspace_file(
            task.task_id, "step-delete-obsolete", target.name,
            sha256("remove me\n"),
        )

        self.assertFalse(target.exists())
        self.assertEqual(record.operation, MutationOperation.DELETE)
        self.assertEqual(record.before_hash, sha256("remove me\n"))
        self.assertIsNone(record.after_hash)
        self.assertIsNotNone(record.backup_ref)
        backup = self.workspace / str(record.backup_ref)
        self.assertEqual(backup.read_text(encoding="utf-8"), "remove me\n")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        persisted = await self.application.kernel.get_task(task.task_id)
        self.assertEqual(persisted.mutation_journal, (record,))

    async def test_stale_delete_hash_preserves_user_change(self) -> None:
        target = self.workspace / "keep-user-change.txt"
        target.write_text("agent-read\n", encoding="utf-8")
        task = await self._executing_task()
        target.write_text("user-change\n", encoding="utf-8")

        with self.assertRaises(WorkspaceMutationConflict):
            await self.application.kernel.delete_workspace_file(
                task.task_id, "step-stale-delete", target.name,
                sha256("agent-read\n"),
            )
        self.assertEqual(target.read_text(), "user-change\n")
        self.assertEqual(
            (await self.application.kernel.get_task(task.task_id)).mutation_journal, ()
        )

    async def test_change_between_delete_prepare_and_commit_is_detected(self) -> None:
        target = self.workspace / "delete-race.txt"
        target.write_text("initial\n", encoding="utf-8")
        prepared = prepare_workspace_file_delete(
            self.workspace, target.name, sha256("initial\n"),
            self.path_service,
        )
        target.write_text("user-raced\n", encoding="utf-8")

        with self.assertRaises(WorkspaceMutationConflict):
            commit_prepared_workspace_deletion(prepared, self.filesystem)
        self.assertEqual(target.read_text(), "user-raced\n")

    async def test_delete_rejects_missing_escape_sensitive_and_symlink(self) -> None:
        task = await self._executing_task()
        outside = self.workspace.parent / f"outside-delete-{self.workspace.name}.txt"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.workspace / "delete-link.txt"
        os.symlink(outside, link)
        try:
            cases = (
                ("missing.txt", FileNotFoundError),
                ("../outside.txt", ValueError),
                (".env", PermissionError),
                (".agent/runtime.db", PermissionError),
                (link.name, PermissionError),
            )
            for index, (path, error_type) in enumerate(cases):
                with self.subTest(path=path):
                    with self.assertRaises(error_type):
                        await self.application.kernel.delete_workspace_file(
                            task.task_id, f"step-delete-denied-{index}", path,
                            sha256("outside\n"),
                        )
            self.assertEqual(outside.read_text(), "outside\n")
        finally:
            link.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    async def test_stale_expected_hash_does_not_overwrite_user_change(self) -> None:
        target = self.workspace / "shared.txt"
        target.write_text("read-by-agent\n", encoding="utf-8")
        task = await self._executing_task()
        target.write_text("edited-by-user\n", encoding="utf-8")

        with self.assertRaises(WorkspaceMutationConflict) as caught:
            await self.application.kernel.write_workspace_text(
                task.task_id, "step-conflict", "shared.txt",
                "agent-version\n", sha256("read-by-agent\n"),
            )
        self.assertEqual(caught.exception.actual_hash, sha256("edited-by-user\n"))
        self.assertEqual(target.read_text(encoding="utf-8"), "edited-by-user\n")
        self.assertEqual(
            (await self.application.kernel.get_task(task.task_id)).mutation_journal, ()
        )

    async def test_change_between_prepare_and_atomic_replace_is_detected(self) -> None:
        target = self.workspace / "race.txt"
        target.write_text("initial\n", encoding="utf-8")
        prepared = prepare_workspace_text_write(
            self.workspace, "race.txt", "agent\n", sha256("initial\n"),
            self.path_service,
        )
        target.write_text("user-raced\n", encoding="utf-8")
        with self.assertRaises(WorkspaceMutationConflict):
            commit_prepared_workspace_mutation(prepared, self.filesystem)
        self.assertEqual(target.read_text(encoding="utf-8"), "user-raced\n")
        self.assertEqual(list(self.workspace.glob(".race.txt.tsm-agt-*")), [])

    async def test_rejects_escape_sensitive_runtime_and_symlink_paths(self) -> None:
        task = await self._executing_task()
        outside = self.workspace.parent / f"outside-{self.workspace.name}.txt"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.workspace / "link.txt"
        os.symlink(outside, link)
        try:
            cases = (
                ("../escape.txt", ValueError),
                (".env", PermissionError),
                (".agent/state.json", PermissionError),
                ("src/../.agent/state.json", PermissionError),
                ("src/../.git/config", PermissionError),
                ("link.txt", PermissionError),
            )
            for index, (path, error_type) in enumerate(cases):
                with self.subTest(path=path):
                    with self.assertRaises(error_type):
                        await self.application.kernel.write_workspace_text(
                            task.task_id, f"step-denied-{index}", path, "bad\n", None
                        )
        finally:
            link.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    async def test_store_failure_restores_old_file_and_does_not_add_journal(self) -> None:
        target = self.workspace / "restore.txt"
        target.write_text("original\n", encoding="utf-8")
        task = await self._executing_task()
        before_events = await self.store.read_events(task.task_id)
        self.store.fail_next = True

        with self.assertRaisesRegex(RuntimeError, "simulated journal"):
            await self.application.kernel.write_workspace_text(
                task.task_id, "step-db-fail", "restore.txt", "changed\n",
                sha256("original\n"),
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(await self.store.read_events(task.task_id), before_events)
        self.assertEqual(
            (await self.application.kernel.get_task(task.task_id)).mutation_journal, ()
        )

    async def test_delete_store_failure_restores_content_and_mode(self) -> None:
        target = self.workspace / "restore-delete.txt"
        target.write_text("original\n", encoding="utf-8")
        os.chmod(target, 0o640)
        task = await self._executing_task()
        before_events = await self.store.read_events(task.task_id)
        self.store.fail_next = True

        with self.assertRaisesRegex(RuntimeError, "simulated journal"):
            await self.application.kernel.delete_workspace_file(
                task.task_id, "step-delete-db-fail", target.name,
                sha256("original\n"),
            )
        self.assertEqual(target.read_text(), "original\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(await self.store.read_events(task.task_id), before_events)
        self.assertEqual(
            (await self.application.kernel.get_task(task.task_id)).mutation_journal, ()
        )

    async def test_mutation_journal_survives_sqlite_restart(self) -> None:
        await self.application.registry.stop_all()
        database = self.workspace / "runtime.db"
        first = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(database)
        )
        await first.registry.start_all()
        try:
            task = await first.kernel.create_task(
                "persist mutation journal", self.workspace, "task-sqlite-mutation"
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await first.kernel.transition_task(
                    task.task_id, state, state.value
                )
            record = await first.kernel.write_workspace_text(
                task.task_id, "step-persist", "persisted.txt", "saved\n", None
            )
            deleted = await first.kernel.delete_workspace_file(
                task.task_id, "step-persist-delete", "persisted.txt",
                sha256("saved\n"),
            )
        finally:
            await first.registry.stop_all()
        second = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(database)
        )
        await second.registry.start_all()
        try:
            restored = await second.kernel.get_task("task-sqlite-mutation")
            self.assertEqual(restored.mutation_journal, (record, deleted))
            self.assertEqual(
                restored.mutation_journal[-1].operation, MutationOperation.DELETE
            )
            self.assertIsNone(restored.mutation_journal[-1].after_hash)
        finally:
            await second.registry.stop_all()
        # asyncTearDown calls stop_all again; the Registry permits an idle stop.


if __name__ == "__main__":
    unittest.main()
