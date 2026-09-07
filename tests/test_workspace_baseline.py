from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.ports import RuntimeStorePort


class WorkspaceBaselineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.application = compose_fixture_application()
        await self.application.registry.start_all()

    async def asyncTearDown(self) -> None:
        await self.application.registry.stop_all()
        self.temporary.cleanup()

    async def test_task_captures_existing_non_git_files_as_user_owned_start_state(self) -> None:
        (self.workspace / "existing.txt").write_text("already changed\n", encoding="utf-8")
        task = await self.application.kernel.create_task(
            "capture start state", self.workspace, "task-baseline"
        )
        baseline = task.workspace_baseline
        assert baseline is not None
        self.assertIn("existing.txt", baseline.files)
        self.assertIsNone(baseline.git_head)
        self.assertFalse(baseline.truncated)
        changes = await self.application.kernel.get_workspace_changes(task.task_id)
        self.assertFalse(changes.changed)

    async def test_detects_added_modified_and_deleted_by_content_hash(self) -> None:
        unchanged = self.workspace / "unchanged.txt"
        modified = self.workspace / "modified.txt"
        deleted = self.workspace / "deleted.txt"
        unchanged.write_text("same\n", encoding="utf-8")
        modified.write_text("before\n", encoding="utf-8")
        deleted.write_text("remove me\n", encoding="utf-8")
        task = await self.application.kernel.create_task(
            "compare workspace", self.workspace, "task-changes"
        )

        stat = unchanged.stat()
        os.utime(unchanged, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        modified.write_text("after\n", encoding="utf-8")
        deleted.unlink()
        (self.workspace / "added.txt").write_text("new\n", encoding="utf-8")

        changes = await self.application.kernel.get_workspace_changes(task.task_id)
        self.assertEqual(changes.added, ("added.txt",))
        self.assertEqual(changes.modified, ("modified.txt",))
        self.assertEqual(changes.deleted, ("deleted.txt",))
        self.assertNotIn("unchanged.txt", changes.modified)
        events = await self.application.registry.require(RuntimeStorePort).read_events(
            task.task_id
        )
        self.assertEqual(events[-1].event_type, "workspace.compared")

    async def test_skips_secrets_generated_directories_and_symlinks(self) -> None:
        (self.workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        (self.workspace / "signing.keystore").write_text("secret\n", encoding="utf-8")
        generated = self.workspace / "node_modules"
        generated.mkdir()
        (generated / "library.js").write_text("generated\n", encoding="utf-8")
        outside = self.workspace.parent / f"outside-{self.workspace.name}.txt"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.workspace / "outside-link.txt"
        try:
            os.symlink(outside, link)
            task = await self.application.kernel.create_task(
                "skip sensitive files", self.workspace, "task-sensitive"
            )
            baseline = task.workspace_baseline
            assert baseline is not None
            self.assertEqual(baseline.files, {})
            self.assertGreaterEqual(baseline.skipped_files, 3)
        finally:
            link.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    async def test_reads_safe_git_head_without_scanning_dot_git(self) -> None:
        git = self.workspace / ".git"
        reference = git / "refs" / "heads"
        reference.mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (reference / "main").write_text("a" * 40 + "\n", encoding="utf-8")
        task = await self.application.kernel.create_task(
            "track git identity", self.workspace, "task-git-head"
        )
        baseline = task.workspace_baseline
        assert baseline is not None
        self.assertEqual(baseline.git_head, "a" * 40)
        self.assertFalse(any(path.startswith(".git/") for path in baseline.files))
        (reference / "main").write_text("b" * 40 + "\n", encoding="utf-8")
        changes = await self.application.kernel.get_workspace_changes(task.task_id)
        self.assertTrue(changes.git_head_changed)
        self.assertEqual(changes.current_git_head, "b" * 40)

    async def test_linked_git_directory_is_not_used_as_project_identity(self) -> None:
        outside = self.workspace.parent / f"outside-git-{self.workspace.name}"
        outside.mkdir()
        (outside / "HEAD").write_text("outside-head\n", encoding="utf-8")
        link = self.workspace / ".git"
        try:
            os.symlink(outside, link)
            task = await self.application.kernel.create_task(
                "reject linked git identity", self.workspace, "task-linked-git"
            )
            assert task.workspace_baseline is not None
            self.assertIsNone(task.workspace_baseline.git_head)
        finally:
            link.unlink(missing_ok=True)
            (outside / "HEAD").unlink(missing_ok=True)
            outside.rmdir()

    async def test_linked_git_head_is_not_read_as_project_identity(self) -> None:
        git = self.workspace / ".git"
        git.mkdir()
        outside = self.workspace.parent / f"outside-head-{self.workspace.name}"
        outside.write_text("outside-head\n", encoding="utf-8")
        head = git / "HEAD"
        try:
            os.symlink(outside, head)
            task = await self.application.kernel.create_task(
                "reject linked git head", self.workspace, "task-linked-git-head"
            )
            assert task.workspace_baseline is not None
            self.assertIsNone(task.workspace_baseline.git_head)
        finally:
            head.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    async def test_baseline_survives_store_round_trip(self) -> None:
        (self.workspace / "src.py").write_text("print('hello')\n", encoding="utf-8")
        task = await self.application.kernel.create_task(
            "persist baseline", self.workspace, "task-persist-baseline"
        )
        restored = await self.application.kernel.get_task(task.task_id)
        self.assertEqual(restored.workspace_baseline, task.workspace_baseline)
        events = await self.application.registry.require(RuntimeStorePort).read_events(
            task.task_id
        )
        self.assertEqual(events[0].payload["baseline"]["file_count"], 1)


if __name__ == "__main__":
    unittest.main()
