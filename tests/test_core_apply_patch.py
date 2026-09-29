from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreWorkspaceMutationToolProvider
from tsm_agt.bootstrap import compose_fixture_application, compose_readonly_application
from tsm_agt.core import ApprovalDecision, ApprovalRequired, TaskState
from tsm_agt.ports import ToolRecoveryKind
from tsm_agt.ports import ToolCall


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CoreApplyPatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.application = compose_fixture_application(
            tool_adapters=(CoreWorkspaceMutationToolProvider(),)
        )
        await self.application.registry.start_all()
        task = await self.application.kernel.create_task(
            "apply an exact patch", self.workspace, "task-apply-patch"
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

    def call(
        self, call_id: str, path: str, expected_hash: str | None,
        edits: list[dict[str, str]],
    ) -> ToolCall:
        return ToolCall(call_id, "core.apply_patch", {
            "path": path, "expected_hash": expected_hash, "edits": edits,
        })

    async def approve(self, call: ToolCall, turn_id: str = "turn-patch"):
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, turn_id, call
            )
        request = caught.exception.request
        return await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve this exact file patch",
        )

    async def test_modify_waits_for_approval_then_journals_exact_change(self) -> None:
        target = self.workspace / "app.py"
        target.write_text("name = 'old'\n", encoding="utf-8")
        call = self.call(
            "call-modify", "app.py", sha256("name = 'old'\n"),
            [{"old_text": "'old'", "new_text": "'new'"}],
        )

        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-modify", call
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "name = 'old'\n")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "reviewed exact replacement",
        )

        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "name = 'new'\n")
        journal = (await self.application.kernel.get_task(self.task.task_id)).mutation_journal
        self.assertEqual(len(journal), 1)
        self.assertEqual(result.data, journal[0].to_data())
        self.assertNotIn("content", result.data)
        self.assertEqual(journal[0].step_id, request.invocation_id)

    async def test_creates_file_with_null_hash_and_empty_old_text(self) -> None:
        result = await self.approve(self.call(
            "call-create", "new.txt", None,
            [{"old_text": "", "new_text": "created\n"}],
        ))
        self.assertTrue(result.ok)
        self.assertEqual((self.workspace / "new.txt").read_text(), "created\n")
        self.assertEqual(result.data["operation"], "create")
        self.assertIsNone(result.data["backup_ref"])

    async def test_user_change_after_read_is_never_overwritten(self) -> None:
        target = self.workspace / "shared.txt"
        target.write_text("agent-read\n", encoding="utf-8")
        call = self.call(
            "call-stale", "shared.txt", sha256("agent-read\n"),
            [{"old_text": "agent-read", "new_text": "agent-write"}],
        )
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-stale", call
            )
        target.write_text("user-write\n", encoding="utf-8")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve previously reviewed patch",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertIs(
            result.effective_recovery_kind,
            ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
        )
        self.assertEqual(
            result.recovery_action["required_change"],
            "refresh_resource_then_retry",
        )
        self.assertFalse(result.recovery_action["same_call_safe"])
        self.assertEqual(target.read_text(encoding="utf-8"), "user-write\n")
        self.assertEqual(
            (await self.application.kernel.get_task(self.task.task_id)).mutation_journal, ()
        )

    async def test_missing_and_ambiguous_exact_text_are_recoverable_conflicts(self) -> None:
        for suffix, content, old_text, message in (
            ("missing", "alpha\n", "beta", "not found"),
            ("ambiguous", "same same\n", "same", "ambiguous"),
        ):
            with self.subTest(case=suffix):
                target = self.workspace / f"{suffix}.txt"
                target.write_text(content, encoding="utf-8")
                result = await self.approve(
                    self.call(
                        f"call-{suffix}", target.name, sha256(content),
                        [{"old_text": old_text, "new_text": "changed"}],
                    ),
                    f"turn-{suffix}",
                )
                self.assertFalse(result.ok)
                # A stale or ambiguous hunk is a correctable input error: the
                # caller re-reads the file and retries instead of the Task dying
                # on a terminal failure.
                self.assertEqual(result.error_code, "PATCH_CONFLICT")
                self.assertIs(
                    result.effective_recovery_kind,
                    ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
                )
                self.assertEqual(
                    result.recovery_action["required_change"],
                    "re_read_file_then_retry",
                )
                self.assertEqual(result.recovery_action["resource"], target.name)
                self.assertFalse(result.recovery_action["same_call_safe"])
                self.assertIn(message, result.message)
                self.assertEqual(target.read_text(encoding="utf-8"), content)

    async def test_empty_old_text_for_existing_file_is_recoverable_conflict(self) -> None:
        target = self.workspace / "existing.txt"
        target.write_text("already here\n", encoding="utf-8")

        result = await self.approve(
            self.call(
                "call-empty-old", target.name, sha256("already here\n"),
                [{"old_text": "", "new_text": "created"}],
            ),
            "turn-empty-old",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PATCH_CONFLICT")
        self.assertEqual(
            result.recovery_action["required_change"], "re_read_file_then_retry"
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "already here\n")

    async def test_multiple_edits_are_applied_sequentially(self) -> None:
        target = self.workspace / "sequence.txt"
        target.write_text("one two\n", encoding="utf-8")
        result = await self.approve(self.call(
            "call-sequence", target.name, sha256("one two\n"),
            [
                {"old_text": "one", "new_text": "first"},
                {"old_text": "first two", "new_text": "done"},
            ],
        ))
        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "done\n")

    async def test_forbidden_paths_are_rejected_without_journal(self) -> None:
        outside = self.workspace.parent / f"outside-{self.workspace.name}.txt"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.workspace / "link.txt"
        os.symlink(outside, link)
        try:
            cases = (
                ("absolute", str(self.workspace / "absolute.txt"), None),
                ("escape", "../escape.txt", None),
                ("env", ".env", None),
                ("runtime", ".agent/data.txt", None),
                ("symlink", "link.txt", sha256("outside\n")),
            )
            for suffix, path, expected in cases:
                with self.subTest(path=path):
                    result = await self.approve(
                        self.call(
                            f"call-{suffix}", path, expected,
                            [{"old_text": "", "new_text": "bad"}],
                        ),
                        f"turn-{suffix}",
                    )
                    self.assertFalse(result.ok)
            self.assertEqual(outside.read_text(), "outside\n")
            self.assertEqual(
                (await self.application.kernel.get_task(self.task.task_id)).mutation_journal, ()
            )
        finally:
            link.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    async def test_completed_call_is_reused_without_applying_again(self) -> None:
        target = self.workspace / "once.txt"
        target.write_text("before\n", encoding="utf-8")
        call = self.call(
            "call-once", target.name, sha256("before\n"),
            [{"old_text": "before", "new_text": "after"}],
        )
        first = await self.approve(call, "turn-once")
        second = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-once", call
        )
        self.assertEqual(second, first)
        self.assertEqual(target.read_text(), "after\n")
        self.assertEqual(
            len((await self.application.kernel.get_task(self.task.task_id)).mutation_journal), 1
        )

    async def test_readonly_composition_does_not_expose_apply_patch(self) -> None:
        application = compose_readonly_application()
        await application.registry.start_all()
        try:
            names = [tool.name for tool in await application.kernel.list_tools()]
            self.assertNotIn("core.apply_patch", names)
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()




class PatchConflictRecoveryWiringTest(unittest.IsolatedAsyncioTestCase):
    """A recoverable patch conflict must actually open a recovery barrier."""

    async def test_patch_conflict_enters_the_recoverable_batch(self) -> None:
        from tsm_agt.core.kernel import Kernel
        from tsm_agt.ports import (
            Message, MessageRole, ToolCallBlock, ToolResult, ToolResultBlock,
        )

        result = ToolResult(
            "call-stale-hunk", False, error_code="PATCH_CONFLICT",
            message="edits[0].old_text was not found in the current file",
            recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            recovery_action={"required_change": "re_read_file_then_retry"},
        )
        messages = (
            Message("assistant", MessageRole.ASSISTANT, (ToolCallBlock(
                ToolCall(
                    "call-stale-hunk", "core.apply_patch", {"path": "app.py"},
                ),
            ),)),
            Message("results", MessageRole.TOOL, (ToolResultBlock(result),)),
        )

        batch = Kernel._latest_recoverable_tool_batch(messages)

        self.assertEqual([item.call_id for item in batch], ["call-stale-hunk"])
        self.assertEqual(
            batch[0].recovery_action["required_change"],
            "re_read_file_then_retry",
        )


class ToolSchemaBoundsTest(unittest.IsolatedAsyncioTestCase):
    """Enforced argument bounds must be advertised in the tool schema."""

    async def asyncSetUp(self) -> None:
        from datetime import datetime
        from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
        from tsm_agt.ports import AdapterContext

        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        provider = CoreReadOnlyToolProvider()
        await provider.start(AdapterContext({}, lambda _type, _payload: None))
        self.addAsyncCleanup(provider.stop, datetime.now())
        self.specs = {spec.name: spec for spec in await provider.list_tools()}

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_search_tools_advertise_context_and_match_bounds(self) -> None:
        for name in ("core.search_text", "core.grep_search"):
            with self.subTest(tool=name):
                properties = self.specs[name].parameters["properties"]
                self.assertEqual(properties["after_context"]["minimum"], 0)
                self.assertEqual(properties["after_context"]["maximum"], 20)
                self.assertEqual(properties["before_context"]["minimum"], 0)
                self.assertEqual(properties["before_context"]["maximum"], 20)
                self.assertEqual(properties["max_matches"]["minimum"], 1)
                self.assertEqual(properties["max_matches"]["maximum"], 500)

    def test_read_and_list_tools_advertise_bounds(self) -> None:
        read = self.specs["core.read_file"].parameters["properties"]
        self.assertEqual(read["start_line"]["minimum"], 1)
        self.assertEqual(read["max_lines"]["minimum"], 1)
        self.assertEqual(read["max_lines"]["maximum"], 1000)

        listing = self.specs["core.list_files"].parameters["properties"]
        self.assertEqual(listing["limit"]["minimum"], 1)
        self.assertEqual(listing["limit"]["maximum"], 1000)

        find = self.specs["core.find_files"].parameters["properties"]
        self.assertEqual(find["limit"]["minimum"], 1)
        self.assertEqual(find["limit"]["maximum"], 500)
