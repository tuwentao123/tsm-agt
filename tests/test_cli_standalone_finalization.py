from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.cli import _finalize_standalone_agent_result
from tsm_agt.core import AgentTurnResult, AgentTurnSuspended, TaskState
from tsm_agt.ports import (
    CompletionReadinessMode, FinishReason, Message, MessageRole, ModelUsage,
    TextBlock,
)


class StandaloneFinalizationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.application = compose_fixture_application(tool_adapters=())
        await self.application.registry.start_all()
        task = await self.application.kernel.create_task(
            "finish one-shot continuation", self.root
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

    async def test_final_result_ends_task_without_consulting_verification(self) -> None:
        lines: list[str] = []
        result = AgentTurnResult(
            "turn-final", self.task.task_id,
            Message(
                "assistant-final", MessageRole.ASSISTANT,
                (TextBlock("finished"),),
            ),
            FinishReason.STOP, ModelUsage(1, 1), 1, 0, (),
        )

        exit_code = await _finalize_standalone_agent_result(
            self.application, self.task.task_id, result, lines.append,
            verbose=True,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            (await self.application.kernel.get_task(self.task.task_id)).state,
            TaskState.SUCCEEDED,
        )
        self.assertTrue(any(line.startswith("task completed:") for line in lines))
        self.assertFalse(any("verification" in line for line in lines))
        self.assertTrue(any(line.startswith("执行状态:") for line in lines))
        # No domain check runs any more, so the row is suppressed rather than
        # printed as "not_started": the Task completed mechanically and nothing
        # wrote ``verify.completed``.
        self.assertFalse(any(line.startswith("领域检查:") for line in lines))
        self.assertTrue(any(line.startswith("模型结论:") for line in lines))
        self.assertTrue(any(line.startswith("引用校验:") for line in lines))

    async def test_readiness_mode_does_not_change_standalone_outcome(self) -> None:
        lines: list[str] = []
        application = compose_fixture_application(
            tool_adapters=(),
            completion_readiness_mode=CompletionReadinessMode.AGENT_DECIDES,
        )
        await application.registry.start_all()
        try:
            task = await application.kernel.create_task(
                "observe standalone completion", self.root
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await application.kernel.transition_task(
                    task.task_id, state, state.value
                )
            result = AgentTurnResult(
                "turn-observe", task.task_id,
                Message("assistant-observe", MessageRole.ASSISTANT,
                        (TextBlock("observed"),)),
                FinishReason.STOP, ModelUsage(1, 1), 1, 0, (),
            )
            exit_code = await _finalize_standalone_agent_result(
                application, task.task_id, result, lines.append, verbose=True,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state,
                TaskState.SUCCEEDED,
            )
            self.assertFalse(any("verification" in line for line in lines))
            self.assertTrue(any(line.startswith("执行状态:") for line in lines))
            # No domain check runs any more, so the row is suppressed rather than
            # printed as "not_started": the Task completed mechanically and nothing
            # wrote ``verify.completed``.
            self.assertFalse(any(line.startswith("领域检查:") for line in lines))
            self.assertTrue(any(line.startswith("模型结论:") for line in lines))
            self.assertTrue(any(line.startswith("引用校验:") for line in lines))
        finally:
            await application.registry.stop_all()

    async def test_suspended_result_does_not_finalize_task(self) -> None:
        result = AgentTurnSuspended(
            self.task.task_id, "turn-waiting", 1, "approval-1",
            "payload", "write", "file", "preview", "R1",
            "none", "none", "restore",
        )

        exit_code = await _finalize_standalone_agent_result(
            self.application, self.task.task_id, result
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            (await self.application.kernel.get_task(self.task.task_id)).state,
            TaskState.EXECUTING,
        )

    async def test_domain_check_row_reappears_once_a_verdict_exists(self) -> None:
        """The row is conditional, not deleted.

        Suppressing ``not_started`` must not hide a real verdict. The only
        remaining source of one is the human-review accept path, which writes
        ``verify.completed`` with ``manual_verified`` -- so replay that event and
        assert the row comes back.
        """

        from tsm_agt.cli import _print_standalone_protocol_status
        from tsm_agt.core import AcceptanceStatus

        await self.application.kernel.complete_task(
            self.task.task_id, "standalone turn completed",
        )
        await self.application.kernel._append_events(self.task.task_id, ((
            "verify.completed",
            {
                "status": AcceptanceStatus.PASSED.value,
                "manual_verified": True, "actor": "user", "criterion_count": 0,
            },
        ),))

        lines: list[str] = []
        await _print_standalone_protocol_status(
            self.application, self.task.task_id, lines.append,
        )

        self.assertTrue(
            any(line.startswith("领域检查: passed") for line in lines), lines,
        )


if __name__ == "__main__":
    unittest.main()
