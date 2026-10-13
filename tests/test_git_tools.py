from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tsm_agt.adapters.builtin.git_tools import GitReadOnlyToolProvider
from tsm_agt.ports import AdapterContext, ToolCall, ToolInvocationContext


class GitReadOnlyToolProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        for args in (("init",), ("config", "user.name", "Test User"), ("config", "user.email", "test@example.com")):
            subprocess.run(["git", *args], cwd=self.workspace, check=True, capture_output=True)
        (self.workspace / "example.txt").write_text("one\ntwo\n", encoding="utf-8")
        subprocess.run(["git", "add", "example.txt"], cwd=self.workspace, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.workspace, check=True, capture_output=True)
        (self.workspace / "example.txt").write_text("one\nchanged\n", encoding="utf-8")
        self.provider = GitReadOnlyToolProvider()
        await self.provider.start(AdapterContext(config={}, emit_event=lambda *_: None))
        self.context = ToolInvocationContext("inv", "task", "turn", self.workspace, datetime.now(timezone.utc) + timedelta(seconds=30))

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_status_diff_and_log_are_structured_and_read_only(self) -> None:
        status = await self.provider.invoke(ToolCall("status", "git.status", {}), self.context)
        diff = await self.provider.invoke(ToolCall("diff", "git.diff", {"path": "example.txt"}), self.context)
        log = await self.provider.invoke(ToolCall("log", "git.log", {"limit": 5}), self.context)
        show = await self.provider.invoke(ToolCall("show", "git.show", {"revision": "HEAD", "path": "example.txt"}), self.context)
        self.assertTrue(status.ok)
        self.assertIn("example.txt", "\n".join(status.data["status"]))
        self.assertTrue(diff.ok)
        self.assertIn("-two", diff.data["diff"])
        self.assertTrue(log.ok)
        self.assertEqual(log.data["commits"][0]["subject"], "initial")
        self.assertTrue(show.ok)
        self.assertIn("two", show.data["content"])
        self.assertEqual((self.workspace / "example.txt").read_text(encoding="utf-8"), "one\nchanged\n")

    async def test_path_escape_and_non_repository_are_rejected(self) -> None:
        escaped = await self.provider.invoke(ToolCall("diff", "git.diff", {"path": "../secret"}), self.context)
        self.assertFalse(escaped.ok)
        self.assertEqual(escaped.error_code, "INVALID_PARAM")
        option = await self.provider.invoke(ToolCall("show-option", "git.show", {"revision": "--textconv"}), self.context)
        self.assertFalse(option.ok)
        self.assertEqual(option.error_code, "INVALID_PARAM")
        other = Path(tempfile.mkdtemp())
        try:
            missing = await self.provider.invoke(ToolCall("status2", "git.status", {}), ToolInvocationContext("inv2", "task", "turn", other, datetime.now(timezone.utc) + timedelta(seconds=30)))
            self.assertFalse(missing.ok)
            self.assertEqual(missing.error_code, "NOT_REPOSITORY")
        finally:
            other.rmdir()
