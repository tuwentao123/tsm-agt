from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreWorkspaceMutationToolProvider
from tsm_agt.bootstrap import compose_fixture_application, compose_readonly_application
from tsm_agt.core import ApprovalDecision, ApprovalRequired, TaskState
from tsm_agt.ports import ToolCall


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CoreRollbackMutationsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.application = compose_fixture_application(
            tool_adapters=(CoreWorkspaceMutationToolProvider(),)
        )
        await self.application.registry.start_all()
        task = await self.application.kernel.create_task(
            "cascade reviewed mutations", self.workspace, "task-cascade-rollback"
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
    def call(call_id: str, *mutation_ids: str) -> ToolCall:
        return ToolCall(call_id, "core.rollback_mutations", {
            "mutation_ids": list(mutation_ids),
        })

    async def approve(self, call: ToolCall, turn_id: str):
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, turn_id, call
            )
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve exact ordered cascade",
        )
        return request, result

    async def two_modifications(self, name: str = "app.txt"):
        target = self.workspace / name
        target.write_text("A\n", encoding="utf-8")
        first = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-one", name, "B\n", sha256("A\n")
        )
        second = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-two", name, "C\n", sha256("B\n")
        )
        return target, first, second

    async def test_approval_precedes_reverse_cascade_and_journals_each_item(self) -> None:
        target, first, second = await self.two_modifications()
        call = self.call("call-cascade", second.mutation_id, first.mutation_id)

        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-cascade", call
            )
        self.assertEqual(target.read_text(), "C\n")
        request = caught.exception.request
        self.assertEqual(
            request.target,
            f"mutation_ids={list(call.arguments['mutation_ids'])}",
        )
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "reviewed newest-to-oldest IDs",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["rolled_back"], 2)
        self.assertEqual(target.read_text(), "A\n")
        journal = (await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal
        self.assertEqual(len(journal), 4)
        self.assertEqual(
            [item.reverts_mutation_id for item in journal[-2:]],
            [second.mutation_id, first.mutation_id],
        )
        self.assertTrue(all(
            item["step_id"] == request.invocation_id
            for item in result.data["mutations"]
        ))

    async def test_wrong_order_is_rejected_before_any_mutation(self) -> None:
        target, first, second = await self.two_modifications("wrong-order.txt")
        _request, result = await self.approve(
            self.call("call-wrong-order", first.mutation_id, second.mutation_id),
            "turn-wrong-order",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")
        self.assertEqual(target.read_text(), "C\n")
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 2)

    async def test_non_contiguous_and_different_paths_are_rejected(self) -> None:
        target, first, second = await self.two_modifications("chain.txt")
        third = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-three", target.name, "D\n", sha256("C\n")
        )
        _request, skipped = await self.approve(
            self.call("call-skipped", third.mutation_id, first.mutation_id),
            "turn-skipped",
        )
        self.assertFalse(skipped.ok)
        self.assertEqual(target.read_text(), "D\n")

        other = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-other", "other.txt", "other\n", None
        )
        _request, mixed = await self.approve(
            self.call("call-mixed", other.mutation_id, third.mutation_id),
            "turn-mixed",
        )
        self.assertFalse(mixed.ok)
        self.assertEqual(mixed.error_code, "INVALID_PARAM")
        self.assertEqual(target.read_text(), "D\n")
        self.assertEqual((self.workspace / "other.txt").read_text(), "other\n")
        self.assertEqual(second.after_hash, third.before_hash)

    async def test_duplicate_already_rolled_back_and_rollback_record_are_rejected(self) -> None:
        target, first, second = await self.two_modifications("invalid-ids.txt")
        _request, duplicate = await self.approve(
            self.call("call-duplicate", second.mutation_id, second.mutation_id),
            "turn-duplicate",
        )
        self.assertFalse(duplicate.ok)
        self.assertEqual(duplicate.error_code, "INVALID_PARAM")

        rollback = await self.application.kernel.rollback_workspace_mutation(
            self.task.task_id, "step-direct-rollback", second.mutation_id
        )
        _request, already = await self.approve(
            self.call("call-already", second.mutation_id, first.mutation_id),
            "turn-already",
        )
        self.assertFalse(already.ok)
        self.assertIn("already rolled back", already.message)
        _request, record = await self.approve(
            self.call("call-record", rollback.mutation_id, first.mutation_id),
            "turn-record",
        )
        self.assertFalse(record.ok)
        self.assertIn("rollback records", record.message)
        self.assertEqual(target.read_text(), "B\n")

    async def test_user_change_during_approval_blocks_entire_cascade(self) -> None:
        target, first, second = await self.two_modifications("user-change.txt")
        call = self.call("call-user-change", second.mutation_id, first.mutation_id)
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-user-change", call
            )
        target.write_text("USER\n", encoding="utf-8")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve stale cascade",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertEqual(target.read_text(), "USER\n")
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 2)

    async def test_all_backups_are_checked_before_first_rollback(self) -> None:
        target, first, second = await self.two_modifications("backup.txt")
        (self.workspace / str(first.backup_ref)).write_text(
            "tampered\n", encoding="utf-8"
        )
        _request, result = await self.approve(
            self.call("call-backup", second.mutation_id, first.mutation_id),
            "turn-backup",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertEqual(target.read_text(), "C\n")
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 2)

    async def test_completed_call_reuses_result_without_second_cascade(self) -> None:
        _target, first, second = await self.two_modifications("reuse.txt")
        call = self.call("call-reuse-many", second.mutation_id, first.mutation_id)
        _request, first_result = await self.approve(call, "turn-reuse-many")
        second_result = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-reuse-many", call
        )
        self.assertEqual(second_result, first_result)
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 4)

    async def test_create_then_modify_cascade_ends_with_absent_file(self) -> None:
        created = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-create", "created.txt", "A\n", None
        )
        modified = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-modify", "created.txt", "B\n",
            sha256("A\n"),
        )
        _request, result = await self.approve(
            self.call("call-create-chain", modified.mutation_id, created.mutation_id),
            "turn-create-chain",
        )
        self.assertTrue(result.ok)
        self.assertFalse((self.workspace / "created.txt").exists())

    async def test_readonly_composition_does_not_expose_cascade_rollback(self) -> None:
        application = compose_readonly_application()
        await application.registry.start_all()
        try:
            names = [tool.name for tool in await application.kernel.list_tools()]
            self.assertNotIn("core.rollback_mutations", names)
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
