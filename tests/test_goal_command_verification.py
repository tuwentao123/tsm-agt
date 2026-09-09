from __future__ import annotations

import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AcceptanceStatus, ApprovalDecision, ApprovalRequired,
    ProjectTrustLevel, TaskState,
)
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    SandboxDecision, SandboxRequest, ToolCall,
)


class _AllowProcessSandbox:
    """Permit process tests while production policy remains unchanged."""

    descriptor = AdapterDescriptor(
        "fixture.goal-command-sandbox", "1.0", "SandboxPort", "1.0",
        frozenset({"workspace-process"}),
    )

    async def start(self, context: AdapterContext) -> None:
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY)

    async def stop(self, deadline) -> None:
        pass

    async def authorize(self, request: SandboxRequest) -> SandboxDecision:
        return SandboxDecision(True, "fixture permits exact command")


class GoalCommandVerificationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def _application(self, *, allow: bool):
        application = compose_fixture_application(
            tool_adapters=(CoreProcessToolProvider(),),
            sandbox_adapter=_AllowProcessSandbox() if allow else None,
        )
        await application.registry.start_all()
        await application.kernel.set_project_trust(
            self.root, ProjectTrustLevel.TRUSTED_BUILD
        )
        return application

    async def _executing_task(self, application, goal: str, task_id: str):
        task = await application.kernel.create_task(goal, self.root, task_id)
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, state, state.value
            )
        return task

    async def _approved_command(
        self, application, task_id: str, call_id: str, argv: list[str],
    ):
        try:
            return await application.kernel.invoke_tool(
                task_id, "turn-goal-command",
                ToolCall(call_id, "core.run_command", {"argv": argv}),
            )
        except ApprovalRequired as suspended:
            request = suspended.request
        return await application.kernel.resolve_approval(
            task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve exact direct command",
        )

    async def _verify(self, application, task_id: str):
        await application.kernel.transition_task(
            task_id, TaskState.VERIFYING, "verify direct command"
        )
        return await application.kernel.verify_task_acceptance(task_id)

    @staticmethod
    def _goal_criterion(verification):
        return next(
            item for item in verification.criteria
            if item.criterion_id == "goal-command-outcome"
        )

    async def test_permission_denied_direct_command_is_blocked(self) -> None:
        application = await self._application(allow=False)
        argv = ["tsm-agt", "doctor", "--workspace", ".", "--model-check"]
        try:
            task = await self._executing_task(
                application, shlex.join(argv), "task-goal-command-denied"
            )
            result = await self._approved_command(
                application, task.task_id, "call-denied", argv
            )
            self.assertEqual(result.error_code, "PERMISSION_DENIED")
            verification = await self._verify(application, task.task_id)
            self.assertEqual(
                self._goal_criterion(verification).status,
                AcceptanceStatus.BLOCKED,
            )
            self.assertEqual(verification.status, AcceptanceStatus.BLOCKED)
        finally:
            await application.registry.stop_all()

    async def test_nonzero_direct_command_is_failed(self) -> None:
        application = await self._application(allow=True)
        argv = [
            sys.executable, "-m", "unittest",
            "tsm_agt_fixture_module_that_does_not_exist",
        ]
        try:
            task = await self._executing_task(
                application, shlex.join(argv), "task-goal-command-nonzero"
            )
            result = await self._approved_command(
                application, task.task_id, "call-nonzero", argv
            )
            self.assertTrue(result.ok)
            self.assertNotEqual(result.data["exit_code"], 0)
            verification = await self._verify(application, task.task_id)
            self.assertEqual(
                self._goal_criterion(verification).status,
                AcceptanceStatus.FAILED,
            )
            self.assertEqual(verification.status, AcceptanceStatus.FAILED)
        finally:
            await application.registry.stop_all()

    async def test_latest_successful_retry_recovers_direct_command(self) -> None:
        application = await self._application(allow=True)
        script = self.root / "retry_once.py"
        script.write_text(
            "from pathlib import Path\n"
            "marker = Path('retry.marker')\n"
            "if marker.exists(): raise SystemExit(0)\n"
            "marker.write_text('first failed')\n"
            "raise SystemExit(9)\n",
            encoding="utf-8",
        )
        argv = [sys.executable, script.name]
        try:
            task = await self._executing_task(
                application, shlex.join(argv), "task-goal-command-retry"
            )
            first = await self._approved_command(
                application, task.task_id, "call-retry-one", argv
            )
            second = await self._approved_command(
                application, task.task_id, "call-retry-two", argv
            )
            self.assertEqual(first.data["exit_code"], 9)
            self.assertEqual(second.data["exit_code"], 0)
            verification = await self._verify(application, task.task_id)
            self.assertEqual(
                self._goal_criterion(verification).status,
                AcceptanceStatus.PASSED,
            )
            self.assertEqual(verification.status, AcceptanceStatus.PASSED)
        finally:
            await application.registry.stop_all()

    async def test_failed_helper_command_does_not_fail_prose_task(self) -> None:
        application = await self._application(allow=False)
        argv = ["tsm-agt", "doctor", "--workspace", ".", "--model-check"]
        try:
            task = await self._executing_task(
                application, "Analyze why the project command cannot run",
                "task-helper-command-denied",
            )
            result = await self._approved_command(
                application, task.task_id, "call-helper-denied", argv
            )
            self.assertEqual(result.error_code, "PERMISSION_DENIED")
            verification = await self._verify(application, task.task_id)
            self.assertFalse(any(
                item.criterion_id == "goal-command-outcome"
                for item in verification.criteria
            ))
            self.assertEqual(verification.status, AcceptanceStatus.PASSED)
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
