from __future__ import annotations

import hashlib
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


class _FailBatchRollbackStore(InMemoryRuntimeStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_batch = False

    async def commit(self, unit):
        if self.fail_batch and len(unit.events) > 1 and all(
            event.event_type == "workspace.rollback_committed"
            for event in unit.events
        ):
            self.fail_batch = False
            raise RuntimeError("simulated batch rollback journal failure")
        return await super().commit(unit)


class CoreRollbackMutationBatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.store = _FailBatchRollbackStore()
        self.application = compose_fixture_application(
            store_adapter=self.store,
            tool_adapters=(CoreWorkspaceMutationToolProvider(),),
        )
        await self.application.registry.start_all()
        task = await self.application.kernel.create_task(
            "rollback several files", self.workspace, "task-rollback-batch"
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
        return ToolCall(call_id, "core.rollback_mutation_batch", {
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
            ApprovalDecision.APPROVE, "approve exact cross-file rollback",
        )
        return request, result

    async def two_modifications(self):
        first = self.workspace / "first.txt"
        second = self.workspace / "second.txt"
        first.write_text("first-before\n", encoding="utf-8")
        second.write_text("second-before\n", encoding="utf-8")
        first_mutation = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-first", first.name, "first-after\n",
            sha256("first-before\n"),
        )
        second_mutation = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-second", second.name, "second-after\n",
            sha256("second-before\n"),
        )
        return first, second, first_mutation, second_mutation

    async def test_approval_precedes_cross_file_rollback_and_commits_together(self) -> None:
        first, second, first_mutation, second_mutation = (
            await self.two_modifications()
        )
        call = self.call(
            "call-rollback-batch", first_mutation.mutation_id,
            second_mutation.mutation_id,
        )
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-rollback-batch", call
            )
        self.assertEqual(first.read_text(), "first-after\n")
        self.assertEqual(second.read_text(), "second-after\n")
        request = caught.exception.request
        self.assertEqual(
            request.target,
            f"mutation_ids={[first_mutation.mutation_id, second_mutation.mutation_id]}",
        )
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "reviewed both rollback IDs",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["rolled_back"], 2)
        self.assertEqual(first.read_text(), "first-before\n")
        self.assertEqual(second.read_text(), "second-before\n")
        journal = (await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal
        self.assertEqual(len(journal), 4)
        self.assertEqual(
            [item.reverts_mutation_id for item in journal[-2:]],
            [first_mutation.mutation_id, second_mutation.mutation_id],
        )
        events = await self.store.read_events(self.task.task_id)
        rollback_events = [
            event for event in events
            if event.event_type == "workspace.rollback_committed"
        ]
        self.assertEqual(len(rollback_events), 2)
        self.assertEqual(rollback_events[1].sequence, rollback_events[0].sequence + 1)

    async def test_create_and_delete_are_reversed_in_one_batch(self) -> None:
        created = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-create", "created.txt", "new\n", None
        )
        deleted_path = self.workspace / "deleted.txt"
        deleted_path.write_text("old\n", encoding="utf-8")
        deleted = await self.application.kernel.delete_workspace_file(
            self.task.task_id, "step-delete", deleted_path.name, sha256("old\n")
        )
        _request, result = await self.approve(
            self.call("call-create-delete", created.mutation_id, deleted.mutation_id),
            "turn-create-delete",
        )
        self.assertTrue(result.ok)
        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertEqual(deleted_path.read_text(), "old\n")

    async def test_user_change_blocks_entire_batch_before_first_rollback(self) -> None:
        first, second, first_mutation, second_mutation = (
            await self.two_modifications()
        )
        call = self.call(
            "call-user-change", first_mutation.mutation_id,
            second_mutation.mutation_id,
        )
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-user-change", call
            )
        second.write_text("user\n", encoding="utf-8")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve stale rollback batch",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertEqual(first.read_text(), "first-after\n")
        self.assertEqual(second.read_text(), "user\n")
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 2)

    async def test_same_path_and_non_latest_mutation_are_rejected(self) -> None:
        target = self.workspace / "chain.txt"
        target.write_text("A\n", encoding="utf-8")
        older = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-older", target.name, "B\n", sha256("A\n")
        )
        newer = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-newer", target.name, "C\n", sha256("B\n")
        )
        _request, same_path = await self.approve(
            self.call("call-same-path", newer.mutation_id, older.mutation_id),
            "turn-same-path",
        )
        self.assertFalse(same_path.ok)
        self.assertEqual(same_path.error_code, "INVALID_PARAM")

        other = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-other", "other.txt", "other\n", None
        )
        _request, non_latest = await self.approve(
            self.call("call-non-latest", older.mutation_id, other.mutation_id),
            "turn-non-latest",
        )
        self.assertFalse(non_latest.ok)
        self.assertIn("newest active mutation", non_latest.message)
        self.assertEqual(target.read_text(), "C\n")
        self.assertTrue((self.workspace / "other.txt").exists())

    async def test_journal_failure_restores_every_rollback_change(self) -> None:
        first, second, first_mutation, second_mutation = (
            await self.two_modifications()
        )
        self.store.fail_batch = True
        _request, result = await self.approve(
            self.call("call-store-failure", first_mutation.mutation_id,
                      second_mutation.mutation_id),
            "turn-store-failure",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TOOL_FAILED")
        self.assertIn("simulated batch rollback journal failure", result.message)
        self.assertEqual(first.read_text(), "first-after\n")
        self.assertEqual(second.read_text(), "second-after\n")
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 2)

    async def test_error_after_second_delete_restores_both_created_files(self) -> None:
        first = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-create-first", "first-created.txt",
            "first\n", None,
        )
        second = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-create-second", "second-created.txt",
            "second\n", None,
        )
        original_commit = kernel_module.commit_prepared_workspace_deletion
        calls = 0

        def fail_after_delete(prepared, filesystem):
            nonlocal calls
            calls += 1
            original_commit(prepared, filesystem)
            if calls == 2:
                raise OSError("simulated failure after atomic delete")

        with patch.object(
            kernel_module, "commit_prepared_workspace_deletion",
            side_effect=fail_after_delete,
        ):
            _request, result = await self.approve(
                self.call("call-post-delete-failure", first.mutation_id,
                          second.mutation_id),
                "turn-post-delete-failure",
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TOOL_FAILED")
        self.assertEqual(
            (self.workspace / "first-created.txt").read_text(), "first\n"
        )
        self.assertEqual(
            (self.workspace / "second-created.txt").read_text(), "second\n"
        )
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 2)

    async def test_completed_call_reuses_result_without_second_rollback(self) -> None:
        _first, _second, first_mutation, second_mutation = (
            await self.two_modifications()
        )
        call = self.call(
            "call-reuse-batch", first_mutation.mutation_id,
            second_mutation.mutation_id,
        )
        _request, first_result = await self.approve(call, "turn-reuse-batch")
        second_result = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-reuse-batch", call
        )
        self.assertEqual(second_result, first_result)
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 4)

    async def test_readonly_composition_does_not_expose_batch_rollback(self) -> None:
        application = compose_readonly_application()
        await application.registry.start_all()
        try:
            names = [tool.name for tool in await application.kernel.list_tools()]
            self.assertNotIn("core.rollback_mutation_batch", names)
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
