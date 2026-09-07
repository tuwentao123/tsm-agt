from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreWorkspaceMutationToolProvider
from tsm_agt.bootstrap import compose_fixture_application, compose_readonly_application
from tsm_agt.core import ApprovalDecision, ApprovalRequired, MutationOperation, TaskState
from tsm_agt.ports import ToolCall


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CoreDeleteFileTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.application = compose_fixture_application(
            tool_adapters=(CoreWorkspaceMutationToolProvider(),)
        )
        await self.application.registry.start_all()
        task = await self.application.kernel.create_task(
            "delete one reviewed file", self.workspace, "task-delete-file"
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await self.application.kernel.transition_task(
                task.task_id, state, state.value
            )
        self.task = task

    async def asyncTearDown(self) -> None:
        await self.application.registry.stop_all()
        self.temporary.cleanup()

    @staticmethod
    def call(call_id: str, path: str, expected_hash: str) -> ToolCall:
        return ToolCall(call_id, "core.delete_file", {
            "path": path, "expected_hash": expected_hash,
        })

    async def request_and_approve(
        self, call: ToolCall, turn_id: str = "turn-delete"
    ):
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, turn_id, call
            )
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve deleting this exact file version",
        )
        return request, result

    async def test_delete_waits_for_approval_then_journals(self) -> None:
        target = self.workspace / "obsolete.txt"
        target.write_text("obsolete\n", encoding="utf-8")
        call = self.call(
            "call-delete", target.name, sha256("obsolete\n")
        )

        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-delete", call
            )
        self.assertTrue(target.exists(), "approval must happen before deletion")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "reviewed exact path and hash",
        )

        self.assertTrue(result.ok)
        self.assertFalse(target.exists())
        journal = (
            await self.application.kernel.get_task(self.task.task_id)
        ).mutation_journal
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0].operation, MutationOperation.DELETE)
        self.assertEqual(result.data, journal[0].to_data())
        self.assertIsNone(result.data["after_hash"])
        self.assertNotIn("content", result.data)
        self.assertEqual(journal[0].step_id, request.invocation_id)

    async def test_user_change_while_approval_waits_is_preserved(self) -> None:
        target = self.workspace / "shared.txt"
        target.write_text("agent-read\n", encoding="utf-8")
        call = self.call(
            "call-stale-delete", target.name, sha256("agent-read\n")
        )
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-stale-delete", call
            )
        target.write_text("user-change\n", encoding="utf-8")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve previously reviewed deletion",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertEqual(target.read_text(), "user-change\n")
        self.assertEqual(
            (await self.application.kernel.get_task(self.task.task_id)).mutation_journal, ()
        )

    async def test_missing_sensitive_and_symlink_targets_are_not_deleted(self) -> None:
        outside = self.workspace.parent / f"outside-tool-{self.workspace.name}.txt"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.workspace / "link.txt"
        os.symlink(outside, link)
        try:
            cases = (
                ("missing", "missing.txt", "NOT_FOUND"),
                ("env", ".env", "PERMISSION_DENIED"),
                ("escape", "../escape.txt", "INVALID_PARAM"),
                ("symlink", link.name, "PERMISSION_DENIED"),
            )
            for suffix, path, error_code in cases:
                with self.subTest(path=path):
                    _request, result = await self.request_and_approve(
                        self.call(
                            f"call-{suffix}", path, sha256("outside\n")
                        ),
                        f"turn-{suffix}",
                    )
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error_code, error_code)
            self.assertEqual(outside.read_text(), "outside\n")
        finally:
            link.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    async def test_completed_delete_call_reuses_result(self) -> None:
        target = self.workspace / "once.txt"
        target.write_text("delete once\n", encoding="utf-8")
        call = self.call(
            "call-delete-once", target.name, sha256("delete once\n")
        )
        _request, first = await self.request_and_approve(call, "turn-delete-once")
        second = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-delete-once", call
        )
        self.assertEqual(second, first)
        self.assertFalse(target.exists())
        self.assertEqual(
            len((await self.application.kernel.get_task(self.task.task_id)).mutation_journal),
            1,
        )

    async def test_readonly_composition_does_not_expose_delete(self) -> None:
        application = compose_readonly_application()
        await application.registry.start_all()
        try:
            names = [tool.name for tool in await application.kernel.list_tools()]
            self.assertNotIn("core.delete_file", names)
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
