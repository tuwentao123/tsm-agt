from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.builtin import CoreWorkspaceMutationToolProvider
from tsm_agt.adapters.fixture import InMemoryRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application, compose_readonly_application
from tsm_agt.core import ApprovalDecision, ApprovalRequired, TaskState
from tsm_agt.ports import ToolCall
from tsm_agt.core import kernel as kernel_module


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _FailGroupedRollbackStore(InMemoryRuntimeStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_grouped = False

    async def commit(self, unit):
        if self.fail_grouped and len(unit.events) >= 3 and all(
            event.event_type == "workspace.rollback_committed"
            for event in unit.events
        ):
            self.fail_grouped = False
            raise RuntimeError("simulated grouped rollback journal failure")
        return await super().commit(unit)


class CoreRollbackMutationGroupsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.store = _FailGroupedRollbackStore()
        self.application = compose_fixture_application(
            store_adapter=self.store,
            tool_adapters=(CoreWorkspaceMutationToolProvider(),),
        )
        await self.application.registry.start_all()
        task = await self.application.kernel.create_task(
            "rollback grouped file histories", self.workspace,
            "task-rollback-groups",
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
    def call(call_id: str, groups: list[list[str]]) -> ToolCall:
        return ToolCall(call_id, "core.rollback_mutation_groups", {
            "mutation_groups": groups,
        })

    async def approve(self, call: ToolCall, turn_id: str):
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, turn_id, call
            )
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve exact grouped rollback",
        )
        return request, result

    async def histories(self):
        first = self.workspace / "first.txt"
        second = self.workspace / "second.txt"
        first.write_text("A\n", encoding="utf-8")
        second.write_text("X\n", encoding="utf-8")
        first_old = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-first-old", first.name, "B\n", sha256("A\n")
        )
        first_new = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-first-new", first.name, "C\n", sha256("B\n")
        )
        second_one = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-second", second.name, "Y\n", sha256("X\n")
        )
        return first, second, first_old, first_new, second_one

    async def test_mixed_depth_groups_restore_each_file_once_and_journal_every_id(self) -> None:
        first, second, first_old, first_new, second_one = await self.histories()
        groups = [
            [first_new.mutation_id, first_old.mutation_id],
            [second_one.mutation_id],
        ]
        call = self.call("call-groups", groups)
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-groups", call
            )
        self.assertEqual(first.read_text(), "C\n")
        self.assertEqual(second.read_text(), "Y\n")
        request = caught.exception.request
        self.assertEqual(request.target, f"mutation_groups={groups}")
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "reviewed grouped histories",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["rolled_back"], 3)
        self.assertEqual(first.read_text(), "A\n")
        self.assertEqual(second.read_text(), "X\n")
        journal = (await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal
        self.assertEqual(len(journal), 6)
        self.assertEqual(
            [item.reverts_mutation_id for item in journal[-3:]],
            [first_new.mutation_id, first_old.mutation_id, second_one.mutation_id],
        )
        self.assertTrue(all(item.step_id == request.invocation_id for item in journal[-3:]))

    async def test_wrong_group_order_rejects_every_file_before_write(self) -> None:
        first, second, first_old, first_new, second_one = await self.histories()
        _request, result = await self.approve(self.call(
            "call-wrong-order",
            [[first_old.mutation_id, first_new.mutation_id], [second_one.mutation_id]],
        ), "turn-wrong-order")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")
        self.assertEqual(first.read_text(), "C\n")
        self.assertEqual(second.read_text(), "Y\n")
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 3)

    async def test_two_groups_for_same_file_are_rejected(self) -> None:
        first, second, first_old, first_new, second_one = await self.histories()
        _request, result = await self.approve(self.call(
            "call-same-file",
            [[first_new.mutation_id], [first_old.mutation_id]],
        ), "turn-same-file")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")
        self.assertEqual(first.read_text(), "C\n")
        self.assertEqual(second.read_text(), "Y\n")
        self.assertIsNotNone(second_one.mutation_id)

    async def test_user_change_blocks_all_groups_before_write(self) -> None:
        first, second, first_old, first_new, second_one = await self.histories()
        call = self.call("call-user-change", [
            [first_new.mutation_id, first_old.mutation_id],
            [second_one.mutation_id],
        ])
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-user-change", call
            )
        second.write_text("USER\n", encoding="utf-8")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve stale groups",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertEqual(first.read_text(), "C\n")
        self.assertEqual(second.read_text(), "USER\n")
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 3)

    async def test_symlink_swap_during_approval_is_rejected_without_reading_target(self) -> None:
        first, second, first_old, first_new, second_one = await self.histories()
        outside = self.workspace.parent / f"outside-{self.workspace.name}.txt"
        outside.write_text("outside-secret\n", encoding="utf-8")
        call = self.call("call-symlink-swap", [
            [first_new.mutation_id, first_old.mutation_id],
            [second_one.mutation_id],
        ])
        try:
            with self.assertRaises(ApprovalRequired) as caught:
                await self.application.kernel.invoke_tool(
                    self.task.task_id, "turn-symlink-swap", call
                )
            second.unlink()
            os.symlink(outside, second)
            request = caught.exception.request
            result = await self.application.kernel.resolve_approval(
                self.task.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.APPROVE, "approve stale symlink groups",
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "PERMISSION_DENIED")
            self.assertEqual(first.read_text(), "C\n")
            self.assertEqual(outside.read_text(), "outside-secret\n")
            self.assertTrue(second.is_symlink())
            self.assertEqual(len((await self.application.kernel.get_task(
                self.task.task_id
            )).mutation_journal), 3)
        finally:
            second.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    async def test_create_chain_and_modify_chain_can_restore_together(self) -> None:
        created = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-create", "created.txt", "one\n", None
        )
        modified_created = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-created-modify", "created.txt", "two\n",
            sha256("one\n"),
        )
        other = self.workspace / "other.txt"
        other.write_text("before\n", encoding="utf-8")
        changed_other = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-other", other.name, "after\n",
            sha256("before\n"),
        )
        _request, result = await self.approve(self.call(
            "call-create-groups",
            [[modified_created.mutation_id, created.mutation_id],
             [changed_other.mutation_id]],
        ), "turn-create-groups")
        self.assertTrue(result.ok)
        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertEqual(other.read_text(), "before\n")

    async def test_journal_failure_compensates_all_file_states(self) -> None:
        first, second, first_old, first_new, second_one = await self.histories()
        self.store.fail_grouped = True
        _request, result = await self.approve(self.call(
            "call-store-failure",
            [[first_new.mutation_id, first_old.mutation_id],
             [second_one.mutation_id]],
        ), "turn-store-failure")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TOOL_FAILED")
        self.assertIn("simulated grouped rollback journal failure", result.message)
        self.assertEqual(first.read_text(), "C\n")
        self.assertEqual(second.read_text(), "Y\n")
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 3)

    async def test_error_after_second_restore_compensates_both_files(self) -> None:
        first, second, first_old, first_new, second_one = await self.histories()
        original_commit = kernel_module.commit_prepared_workspace_mutation
        calls = 0

        def fail_after_restore(prepared, filesystem):
            nonlocal calls
            calls += 1
            original_commit(prepared, filesystem)
            if calls == 2:
                raise OSError("simulated failure after grouped restore")

        with patch.object(
            kernel_module, "commit_prepared_workspace_mutation",
            side_effect=fail_after_restore,
        ):
            _request, result = await self.approve(self.call(
                "call-post-restore-failure",
                [[first_new.mutation_id, first_old.mutation_id],
                 [second_one.mutation_id]],
            ), "turn-post-restore-failure")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TOOL_FAILED")
        self.assertEqual(first.read_text(), "C\n")
        self.assertEqual(second.read_text(), "Y\n")
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 3)

    async def test_completed_call_reuses_result_without_second_execution(self) -> None:
        _first, _second, first_old, first_new, second_one = await self.histories()
        call = self.call("call-reuse", [
            [first_new.mutation_id, first_old.mutation_id],
            [second_one.mutation_id],
        ])
        _request, first_result = await self.approve(call, "turn-reuse")
        second_result = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-reuse", call
        )
        self.assertEqual(second_result, first_result)
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 6)

    async def test_readonly_composition_does_not_expose_grouped_rollback(self) -> None:
        application = compose_readonly_application()
        await application.registry.start_all()
        try:
            names = [tool.name for tool in await application.kernel.list_tools()]
            self.assertNotIn("core.rollback_mutation_groups", names)
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
