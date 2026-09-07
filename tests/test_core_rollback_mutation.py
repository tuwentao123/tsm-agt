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


class CoreRollbackMutationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.application = compose_fixture_application(
            tool_adapters=(CoreWorkspaceMutationToolProvider(),)
        )
        await self.application.registry.start_all()
        task = await self.application.kernel.create_task(
            "rollback one reviewed mutation", self.workspace, "task-tool-rollback"
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
    def call(call_id: str, mutation_id: str) -> ToolCall:
        return ToolCall(call_id, "core.rollback_mutation", {
            "mutation_id": mutation_id,
        })

    async def approve(self, call: ToolCall, turn_id: str):
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, turn_id, call
            )
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve exact mutation rollback",
        )
        return request, result

    async def test_approval_precedes_rollback_and_result_is_journaled(self) -> None:
        target = self.workspace / "app.txt"
        target.write_text("before\n", encoding="utf-8")
        original = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-agent-change", target.name, "after\n",
            sha256("before\n"),
        )
        call = self.call("call-rollback", original.mutation_id)

        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-rollback", call
            )
        self.assertEqual(target.read_text(), "after\n")
        request = caught.exception.request
        self.assertEqual(
            request.target, f"mutation_id={original.mutation_id}"
        )
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "reviewed exact mutation id",
        )

        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(), "before\n")
        self.assertEqual(result.data["reverts_mutation_id"], original.mutation_id)
        self.assertNotIn("content", result.data)
        self.assertEqual(result.data["step_id"], request.invocation_id)

    async def test_user_change_during_approval_blocks_rollback(self) -> None:
        target = self.workspace / "shared.txt"
        target.write_text("before\n", encoding="utf-8")
        original = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-agent", target.name, "agent\n",
            sha256("before\n"),
        )
        call = self.call("call-stale-rollback", original.mutation_id)
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-stale-rollback", call
            )
        target.write_text("user\n", encoding="utf-8")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve previously reviewed mutation",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertEqual(target.read_text(), "user\n")

    async def test_unknown_and_already_rolled_back_are_structured_errors(self) -> None:
        _request, missing = await self.approve(
            self.call("call-missing", "mutation-missing"), "turn-missing"
        )
        self.assertFalse(missing.ok)
        self.assertEqual(missing.error_code, "INVALID_PARAM")

        original = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-create", "once.txt", "once\n", None
        )
        _request, first = await self.approve(
            self.call("call-first", original.mutation_id), "turn-first"
        )
        self.assertTrue(first.ok)
        _request, repeated = await self.approve(
            self.call("call-repeat", original.mutation_id), "turn-repeat"
        )
        self.assertFalse(repeated.ok)
        self.assertEqual(repeated.error_code, "INVALID_PARAM")

    async def test_completed_tool_call_reuses_result_without_second_rollback(self) -> None:
        original = await self.application.kernel.write_workspace_text(
            self.task.task_id, "step-create", "reuse.txt", "reuse\n", None
        )
        call = self.call("call-reuse", original.mutation_id)
        _request, first = await self.approve(call, "turn-reuse")
        second = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-reuse", call
        )
        self.assertEqual(second, first)
        self.assertEqual(
            len((await self.application.kernel.get_task(
                self.task.task_id
            )).mutation_journal),
            2,
        )

    async def test_readonly_composition_does_not_expose_rollback(self) -> None:
        application = compose_readonly_application()
        await application.registry.start_all()
        try:
            names = [tool.name for tool in await application.kernel.list_tools()]
            self.assertNotIn("core.rollback_mutation", names)
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
