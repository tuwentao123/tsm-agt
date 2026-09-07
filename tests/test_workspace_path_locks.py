from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import InMemoryRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.adapters.posix_path import PosixWorkspacePath
from tsm_agt.core import TaskState, WorkspaceMutationConflict
from tsm_agt.core.workspace import (
    workspace_mutation_lock_file, workspace_mutation_lock_key,
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hold_workspace_file_lock(
    workspace: str, relative_path: str, ready, release
) -> None:
    import fcntl
    from tsm_agt.ports import AdapterContext
    import asyncio as child_asyncio
    from tsm_agt.adapters.posix_filesystem import PosixWorkspaceFilesystem

    root = Path(workspace)
    path_service = PosixWorkspacePath()
    child_asyncio.run(path_service.start(
        AdapterContext(config={}, emit_event=lambda _type, _payload: None)
    ))
    filesystem = PosixWorkspaceFilesystem()
    child_asyncio.run(filesystem.start(
        AdapterContext(config={}, emit_event=lambda _type, _payload: None)
    ))
    key = workspace_mutation_lock_key(root, relative_path, path_service)
    lock_file = workspace_mutation_lock_file(
        root, key, path_service, filesystem
    )
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_file, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.set()
        release.wait(5)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class _BlockingMutationStore(InMemoryRuntimeStore):
    """Hold mutation commits so tests can observe path-lock concurrency."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.first_entered = asyncio.Event()
        self.two_entered = asyncio.Event()
        self.active_mutation_commits = 0

    async def commit(self, unit):
        is_mutation = any(
            event.event_type == "workspace.mutation_committed"
            for event in unit.events
        )
        if not is_mutation:
            return await super().commit(unit)
        self.active_mutation_commits += 1
        self.first_entered.set()
        if self.active_mutation_commits >= 2:
            self.two_entered.set()
        try:
            await self.release.wait()
            return await super().commit(unit)
        finally:
            self.active_mutation_commits -= 1


class WorkspacePathLockTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.store = _BlockingMutationStore()
        self.application = compose_fixture_application(store_adapter=self.store)
        await self.application.registry.start_all()
        self.first_task = await self._executing_task("task-lock-first")
        self.second_task = await self._executing_task("task-lock-second")

    async def asyncTearDown(self) -> None:
        self.store.release.set()
        await self.application.registry.stop_all()
        self.temporary.cleanup()

    async def _executing_task(self, task_id: str):
        task = await self.application.kernel.create_task(
            "coordinate workspace mutations", self.workspace, task_id
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

    async def test_same_path_is_serialized_and_second_writer_conflicts(self) -> None:
        target = self.workspace / "shared.txt"
        target.write_text("initial\n", encoding="utf-8")
        expected = sha256("initial\n")
        first = asyncio.create_task(
            self.application.kernel.write_workspace_text(
                self.first_task.task_id, "step-first", target.name,
                "first\n", expected,
            )
        )
        await asyncio.wait_for(self.store.first_entered.wait(), timeout=1)
        second = asyncio.create_task(
            self.application.kernel.write_workspace_text(
                self.second_task.task_id, "step-second", target.name,
                "second\n", expected,
            )
        )

        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(
                self.store.two_entered.wait(), timeout=0.05
            )
        self.store.release.set()
        first_record = await first
        with self.assertRaises(WorkspaceMutationConflict):
            await second

        self.assertEqual(target.read_text(), "first\n")
        self.assertEqual(first_record.after_hash, sha256("first\n"))
        self.assertEqual(
            len((await self.application.kernel.get_task(
                self.first_task.task_id
            )).mutation_journal),
            1,
        )
        self.assertEqual(
            (await self.application.kernel.get_task(
                self.second_task.task_id
            )).mutation_journal,
            (),
        )

    async def test_different_paths_can_commit_concurrently(self) -> None:
        first_path = self.workspace / "first.txt"
        second_path = self.workspace / "second.txt"
        first_path.write_text("one\n", encoding="utf-8")
        second_path.write_text("two\n", encoding="utf-8")
        first = asyncio.create_task(
            self.application.kernel.write_workspace_text(
                self.first_task.task_id, "step-first-file", first_path.name,
                "one changed\n", sha256("one\n"),
            )
        )
        await asyncio.wait_for(self.store.first_entered.wait(), timeout=1)
        second = asyncio.create_task(
            self.application.kernel.write_workspace_text(
                self.second_task.task_id, "step-second-file", second_path.name,
                "two changed\n", sha256("two\n"),
            )
        )

        await asyncio.wait_for(self.store.two_entered.wait(), timeout=1)
        self.store.release.set()
        first_record, second_record = await asyncio.gather(first, second)

        self.assertEqual(first_path.read_text(), "one changed\n")
        self.assertEqual(second_path.read_text(), "two changed\n")
        self.assertNotEqual(first_record.path, second_record.path)

    async def test_independent_process_lock_blocks_kernel_mutation(self) -> None:
        # This test uses a separate spawned interpreter, not another coroutine.
        self.store.release.set()
        target = self.workspace / "cross-process.txt"
        target.write_text("before\n", encoding="utf-8")
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_workspace_file_lock,
            args=(str(self.workspace), target.name, ready, release),
        )
        process.start()
        try:
            self.assertTrue(
                await asyncio.to_thread(ready.wait, 5),
                "child process did not acquire the workspace path lock",
            )
            mutation = asyncio.create_task(
                self.application.kernel.write_workspace_text(
                    self.first_task.task_id, "step-cross-process", target.name,
                    "after\n", sha256("before\n"),
                )
            )
            await asyncio.sleep(0.08)
            self.assertFalse(mutation.done())
            self.assertEqual(target.read_text(), "before\n")
            release.set()
            record = await asyncio.wait_for(mutation, timeout=5)
            self.assertEqual(record.after_hash, sha256("after\n"))
            self.assertEqual(target.read_text(), "after\n")
        finally:
            release.set()
            await asyncio.to_thread(process.join, 5)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
        self.assertEqual(process.exitcode, 0)

    async def test_runtime_lock_directory_cannot_be_a_symlink(self) -> None:
        linked_workspace = Path(self.temporary.name) / "linked-workspace"
        linked_workspace.mkdir()
        outside = Path(self.temporary.name) / "outside-runtime"
        outside.mkdir()
        os.symlink(outside, linked_workspace / ".agent")
        try:
            key = workspace_mutation_lock_key(
                linked_workspace, "file.txt",
                self.application.kernel.dependencies.workspace_path,
            )
            with self.assertRaises(PermissionError):
                workspace_mutation_lock_file(
                    linked_workspace, key,
                    self.application.kernel.dependencies.workspace_path,
                    self.application.kernel.dependencies.workspace_filesystem,
                )
        finally:
            (linked_workspace / ".agent").unlink()


if __name__ == "__main__":
    unittest.main()
