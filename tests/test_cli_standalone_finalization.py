from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.cli import _finalize_standalone_agent_result
from tsm_agt.core import AgentTurnResult, AgentTurnSuspended, TaskState
from tsm_agt.ports import (
    FinishReason, Message, MessageRole, ModelUsage, TextBlock,
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

    async def test_final_result_runs_verifier_and_ends_task(self) -> None:
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
            self.application, self.task.task_id, result, lines.append
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            (await self.application.kernel.get_task(self.task.task_id)).state,
            TaskState.SUCCEEDED,
        )
        self.assertTrue(any("verification: passed" in line for line in lines))

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


if __name__ == "__main__":
    unittest.main()
